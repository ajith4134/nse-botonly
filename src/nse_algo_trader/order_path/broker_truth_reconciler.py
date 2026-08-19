"""On restart, and on demand: believe the broker, and record every place the two disagreed.

`L3.03`. The procedure is copied in shape from the only implementation in the surveyed set that
actually does it — NautilusTrader's `reconciliation/orders.rs` (`docs/research/224` §4). Hummingbot
and freqtrade only re-poll orders they already know about, which means an order placed just before a
crash and never journalled is **permanently invisible** to them; LEAN adopts the broker's snapshot
blindly with fresh local ids, which is an import rather than a diff.

Every order in the session falls into exactly one bucket:

* **AGREED** — same state, same filled quantity. Nothing to do.
* **BROKER_ONLY** — the broker has an order this system did not place. Usually real: a human order
  in the Kite app. It is ADOPTED as external and never confused with this system's own, because the
  intent tag on the wire is what identifies ours (`trading_intent.broker_tag_for`).
* **LOCAL_ONLY_UNRESOLVED / ABANDONED** — an in-flight or ambiguous submission the broker has never
  shown. Resolved by the **visibility horizon**, not by a guess.
* **QUANTITY_GAP** — the broker has filled more than this system knows about. An **inferred fill**
  is emitted for the difference, priced at the broker's own average, and flagged as inferred for the
  rest of its life.
* **STATE_CONFLICT** — the two disagree about the state. The broker wins, and the disagreement is
  written down rather than smoothed away.

**The visibility horizon is measured, not assumed.** Kite publishes no propagation-latency figure at
all (`docs/research/222` §7), so "how long must an order be absent before absence means absence" can
only come from this system's own observations of how long its orders took to appear. Until enough of
those exist, the reconciler **refuses to declare anything abandoned** and reports it as unresolved.
NautilusTrader has the same problem and documents leaving such orders unresolved indefinitely
(`docs/concepts/reconciliation.md:220-224`); the difference here is that the wait has a measured
end.

**A failed fetch is a refusal, not an empty result.** If broker state cannot be read, this returns
nothing and raises. A reconciler that treats "I could not ask" as "there is nothing there" is how a
position gets doubled.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.order_path.order_execution_venue import (
    OrderExecutionVenue,
    VenueOrderReport,
    VenueOutcomeUnknownError,
    VenueRejectedError,
    VenueTradeReport,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    TERMINAL_STATES,
    EventSource,
    IllegalOrderTransitionError,
    LifecycleEvent,
    OrderLifecycleState,
    UnmappedBrokerStatusError,
    lifecycle_event_for_broker_status,
)
from nse_algo_trader.order_path.order_record import FillRecord, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, OrderPathError

# The horizon is a high quantile of observed appearance delays, widened by the spread of those
# observations. Both numbers below are properties of the ESTIMATOR, not of the market: the quantile
# says "cover all but the slowest one in twenty", and the multiplier says "and then allow that many
# standard deviations of what has actually been seen". Neither is a threshold about orders.
_HORIZON_QUANTILE = 0.95
_HORIZON_DISPERSION_MULTIPLIER = 3.0
# Below this many observations a quantile is not an estimate, it is the largest number seen so far.
# Set from the arithmetic of the quantile itself: 0.95 cannot be resolved by fewer than 20 points.
_MINIMUM_OBSERVATIONS_FOR_A_HORIZON = round(1 / (1 - _HORIZON_QUANTILE))


class ReconciliationRefusedError(OrderPathError):
    """Broker state could not be read. Proceeding on the local view would risk doubling."""


class ReconciliationVerdict(StrEnum):
    """Which bucket an order fell into."""

    AGREED = "agreed"
    BROKER_ONLY = "broker_only"
    LOCAL_ONLY_UNRESOLVED = "local_only_unresolved"
    LOCAL_ONLY_ABANDONED = "local_only_abandoned"
    QUANTITY_GAP_PATCHED = "quantity_gap_patched"
    STATE_CONFLICT_BROKER_WON = "state_conflict_broker_won"
    UNMAPPABLE = "unmappable"


@dataclass(frozen=True, slots=True)
class OrderReconciliation:
    """What was decided about one order, and on what evidence."""

    verdict: ReconciliationVerdict
    intent_id: str | None
    broker_order_id: str | None
    trading_symbol: str
    local_state: OrderLifecycleState | None
    broker_status: str
    local_filled_quantity: int
    broker_filled_quantity: int
    detail: str
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class VisibilityHorizon:
    """How long an order must be unseen before absence is evidence of absence."""

    seconds: float | None
    observations: int
    detail: str

    @property
    def is_established(self) -> bool:
        return self.seconds is not None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The whole session's verdict, in a shape a dashboard can render without interpreting."""

    session_date: date
    ran_at: datetime
    horizon: VisibilityHorizon
    reconciliations: tuple[OrderReconciliation, ...]

    def counts_by_verdict(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for reconciliation in self.reconciliations:
            counts[reconciliation.verdict.value] += 1
        return dict(counts)

    @property
    def disagreements(self) -> tuple[OrderReconciliation, ...]:
        return tuple(
            reconciliation
            for reconciliation in self.reconciliations
            if reconciliation.verdict is not ReconciliationVerdict.AGREED
        )

    @property
    def has_unresolved(self) -> bool:
        return any(
            reconciliation.verdict is ReconciliationVerdict.LOCAL_ONLY_UNRESOLVED
            for reconciliation in self.reconciliations
        )


def visibility_horizon_from_observed_delays(delays: tuple[float, ...]) -> VisibilityHorizon:
    """Derive how long an order must be unseen before absence is evidence of absence.

    A module-level function rather than a method because the dashboard needs the same number
    without holding a reconciler, and two implementations of it would eventually disagree.
    """
    if len(delays) < _MINIMUM_OBSERVATIONS_FOR_A_HORIZON:
        return VisibilityHorizon(
            seconds=None,
            observations=len(delays),
            detail=(
                f"{len(delays)} observed appearance delays; a {_HORIZON_QUANTILE:.0%} quantile "
                f"cannot be resolved by fewer than {_MINIMUM_OBSERVATIONS_FOR_A_HORIZON}, so no "
                f"order will be declared abandoned yet — they are reported unresolved instead, "
                f"which is the honest answer rather than a convenient one"
            ),
        )
    ordered = sorted(delays)
    index = min(len(ordered) - 1, int(_HORIZON_QUANTILE * len(ordered)))
    quantile = ordered[index]
    dispersion = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    return VisibilityHorizon(
        seconds=quantile + _HORIZON_DISPERSION_MULTIPLIER * dispersion,
        observations=len(delays),
        detail=(
            f"{_HORIZON_QUANTILE:.0%} of {len(delays)} observed delays is {quantile:.2f}s, "
            f"widened by {_HORIZON_DISPERSION_MULTIPLIER}x the observed dispersion "
            f"({dispersion:.2f}s)"
        ),
    )


@dataclass(slots=True)
class BrokerTruthReconciler:
    """Converge the local journal onto the broker's account of the session."""

    journal: OrderIntentJournal
    venue: OrderExecutionVenue
    namespace: OrderNamespace = OrderNamespace.LIVE
    _horizon_cache: VisibilityHorizon | None = field(default=None, repr=False)
    _over_fills: list[str] = field(default_factory=list, repr=False)

    def visibility_horizon(self) -> VisibilityHorizon:
        """Derive the horizon from this system's own observed appearance delays (`R.03`)."""
        return visibility_horizon_from_observed_delays(self.journal.visibility_delays_seconds())

    def reconcile(self, *, session_date: date, now: datetime) -> ReconciliationReport:
        """Read the broker, diff, patch what can be patched, and report every disagreement."""
        try:
            broker_orders = self.venue.fetch_orders(session_date=session_date)
            broker_trades = self.venue.fetch_trades(session_date=session_date)
        except (VenueUnavailableError, VenueOutcomeUnknownError, VenueRejectedError) as unavailable:
            raise ReconciliationRefusedError(
                f"broker state could not be read ({unavailable}); this system will not act on its "
                f"own view of a session it has not been able to check, because 'I could not ask' "
                f"and 'there is nothing there' are the same answer only to a system that doubles "
                f"positions"
            ) from unavailable

        horizon = self.visibility_horizon()
        trades_by_order: dict[str, list[VenueTradeReport]] = defaultdict(list)
        for trade in broker_trades:
            trades_by_order[trade.broker_order_id].append(trade)

        # Only this reconciler's OWN namespace. The journal holds paper and live orders side by
        # side, and the R.23(c) review reproduced the live reconciler writing a terminal inferred
        # event onto a SIMULATED order. The namespace is the first character of the wire tag, so
        # the filter needs no extra state.
        local_orders = {
            order.intent_id: order
            for order in self.journal.orders_for_session(session_date)
            if order.namespace is self.namespace
        }
        seen_intents: set[str] = set()
        reconciliations: list[OrderReconciliation] = []

        for report in broker_orders:
            intent_id = self.journal.intent_id_for_tag(report.tag) if report.tag else None
            if intent_id is None or intent_id not in local_orders:
                reconciliations.append(_adopt_external(report))
                continue
            seen_intents.add(intent_id)
            reconciliations.append(
                self._reconcile_one(
                    local_orders[intent_id],
                    report,
                    trades_by_order.get(report.broker_order_id, []),
                    now=now,
                )
            )

        for intent_id, order in local_orders.items():
            if intent_id in seen_intents or order.is_terminal:
                continue
            reconciliations.append(self._resolve_absence(order, horizon, now=now))

        return ReconciliationReport(
            session_date=session_date,
            ran_at=now,
            horizon=horizon,
            reconciliations=tuple(reconciliations),
        )

    # --- one order at a time ---------------------------------------------------------------------

    def _reconcile_one(
        self,
        order: OrderRecord,
        report: VenueOrderReport,
        trades: list[VenueTradeReport],
        *,
        now: datetime,
    ) -> OrderReconciliation:
        self._record_first_sighting(order, report)
        order = self._apply_trades(order, report, trades)

        if report.filled_quantity > order.filled_quantity:
            return self._patch_quantity_gap(order, report, now=now)

        try:
            broker_event = lifecycle_event_for_broker_status(report.status)
        except UnmappedBrokerStatusError as unmapped:
            return OrderReconciliation(
                verdict=ReconciliationVerdict.UNMAPPABLE,
                intent_id=order.intent_id,
                broker_order_id=report.broker_order_id,
                trading_symbol=report.trading_symbol,
                local_state=order.state,
                broker_status=report.status,
                local_filled_quantity=order.filled_quantity,
                broker_filled_quantity=report.filled_quantity,
                detail=str(unmapped),
            )

        conflict = self._apply_broker_event(order, report, broker_event, now=now)
        verdict = (
            ReconciliationVerdict.STATE_CONFLICT_BROKER_WON
            if conflict
            else ReconciliationVerdict.AGREED
        )
        return OrderReconciliation(
            verdict=verdict,
            intent_id=order.intent_id,
            broker_order_id=report.broker_order_id,
            trading_symbol=report.trading_symbol,
            local_state=order.state,
            broker_status=report.status,
            local_filled_quantity=order.filled_quantity,
            broker_filled_quantity=report.filled_quantity,
            detail=conflict or "local and broker agree",
        )

    def _record_first_sighting(self, order: OrderRecord, report: VenueOrderReport) -> None:
        """Feed the horizon estimator with how long this order took to become visible."""
        if order.broker_order_id is not None:
            return
        submitted = next(
            (
                transition.occurred_at
                for transition in order.transitions
                if transition.event is LifecycleEvent.SUBMITTED
            ),
            None,
        )
        if submitted is not None and report.exchange_timestamp is not None:
            # Only the EXCHANGE's own timestamp measures an appearance delay. Substituting `now`
            # measured the order's age instead, and on any path that returns before the broker id
            # is journalled it did so on every pass — 25 passes over one order inflated the horizon
            # to 11.4 hours (R.23(c) review). The schema now also permits one observation per
            # intent, so this is belt and braces on purpose.
            first_seen = report.exchange_timestamp
            if first_seen >= submitted:
                self.journal.record_visibility_delay(
                    order.intent_id, submitted_at=submitted, first_seen_at=first_seen
                )

    def _apply_trades(
        self, order: OrderRecord, report: VenueOrderReport, trades: list[VenueTradeReport]
    ) -> OrderRecord:
        """Apply the broker's real executions, retiring any inference they now account for.

        **The order of these two things is the whole fix.** `orders().filled_quantity` moves before
        `trades()` lists the executions, so a first pass legitimately infers a fill for the gap.
        When the real trades appear on a later pass they carry different ids, so nothing
        de-duplicates them against the inference — the `R.23(c)` review reproduced the position
        running 30 units past the ordered quantity, and the resulting over-fill exception poisoned
        every later read. So the inference is SUPERSEDED first, the order is re-folded without it,
        and only then do the real trades apply.
        """
        unseen = [
            trade
            for trade in sorted(trades, key=lambda candidate: candidate.filled_at)
            if trade.broker_trade_id not in {fill.broker_trade_id for fill in order.fills}
        ]
        if not unseen:
            return order
        if order.has_inferred_events and self.journal.inferred_fill_count(order.intent_id):
            superseded = self.journal.supersede_inferred_fills(
                order.intent_id,
                superseded_by=(
                    f"the broker reported {len(unseen)} real trade(s) for order "
                    f"{report.broker_order_id}, which account for the quantity this system had "
                    f"inferred; the inference is withdrawn rather than added to"
                ),
            )
            if superseded:
                refolded = self.journal.load_order(order.intent_id)
                if refolded is not None:
                    order = refolded
        for trade in unseen:
            fill = FillRecord(
                broker_trade_id=trade.broker_trade_id,
                quantity=trade.quantity,
                price_paise=trade.price_paise,
                occurred_at=trade.filled_at,
                source=EventSource.BROKER_REPORTED,
            )
            if order.filled_quantity + fill.quantity > order.ordered_quantity:
                # A real over-fill is a disagreement for a human, not an exception that has already
                # written its own row. Nothing is recorded and the conflict surfaces on `/orders`.
                self._over_fills.append(
                    f"{report.broker_order_id}: trade {fill.broker_trade_id} of {fill.quantity} "
                    f"would take the order past its ordered {order.ordered_quantity} "
                    f"(already {order.filled_quantity})"
                )
                continue
            if not self.journal.record_fill(order.intent_id, fill):
                continue
            order.apply_fill(fill)
        return order

    def _patch_quantity_gap(
        self, order: OrderRecord, report: VenueOrderReport, *, now: datetime
    ) -> OrderReconciliation:
        """Emit an inferred fill for a quantity the broker has and no trade explains."""
        missing = report.filled_quantity - order.filled_quantity
        price = _residual_price_paise(order, report, missing)
        detail = (
            f"broker reports {report.filled_quantity} filled against {order.filled_quantity} "
            f"locally, and no trade accounts for the difference; {missing} units are INFERRED at "
            f"the broker's own average of {price}"
        )
        fill = FillRecord(
            # Deterministic so a replay of this reconciliation produces the same fill rather than a
            # second one — NautilusTrader keys its inferred fills the same way.
            broker_trade_id=f"inferred:{report.broker_order_id}:{report.filled_quantity}",
            quantity=missing,
            price_paise=price,
            occurred_at=report.exchange_timestamp or now,
            source=EventSource.INFERRED,
            inferred_from=detail,
        )
        if self.journal.record_fill(order.intent_id, fill):
            order.apply_fill(fill)
        return OrderReconciliation(
            verdict=ReconciliationVerdict.QUANTITY_GAP_PATCHED,
            intent_id=order.intent_id,
            broker_order_id=report.broker_order_id,
            trading_symbol=report.trading_symbol,
            local_state=order.state,
            broker_status=report.status,
            local_filled_quantity=order.filled_quantity,
            broker_filled_quantity=report.filled_quantity,
            detail=detail,
            inferred=True,
        )

    def _apply_broker_event(
        self,
        order: OrderRecord,
        report: VenueOrderReport,
        event: LifecycleEvent,
        *,
        now: datetime,
    ) -> str:
        """Move the local order onto the broker's account of it. Returns the conflict, if any."""
        if order.state in TERMINAL_STATES:
            expected = _terminal_state_for(event)
            if expected is not None and expected is not order.state:
                return (
                    f"local order is {order.state.value} but the broker reports "
                    f"{report.status!r}; the broker's account is authoritative and this "
                    f"disagreement is recorded rather than resolved away"
                )
            return ""
        if order.transitions and order.transitions[-1].event is event:
            # The broker is repeating itself. Reconciliation runs as often as it likes, and a
            # status that has not changed since the last pass is not news — re-applying it would
            # manufacture a transition that never happened.
            order.broker_order_id = report.broker_order_id
            return ""
        try:
            # Applied to the in-memory order FIRST, because that is what validates the move. The
            # journal is written only once the transition is accepted: an earlier version wrote
            # first, and an illegal event then sat in the log poisoning every future fold of that
            # order. The stateful property test found it; no review had.
            order.apply_event(
                event,
                EventSource.BROKER_REPORTED,
                at=report.exchange_timestamp or now,
                note=report.status_message_raw or report.status,
            )
        except IllegalOrderTransitionError as illegal:
            # The local view cannot accept what the broker reports. The broker is still right —
            # this is recorded as a conflict for a human and for `/orders`, and the local order is
            # deliberately left where it is rather than forced, because forcing it would erase the
            # evidence that the two ever disagreed.
            return (
                f"the broker reports {report.status!r} for an order this system believes is "
                f"{order.state.value}: {illegal}"
            )
        self.journal.record_event(
            order.intent_id,
            event,
            EventSource.BROKER_REPORTED,
            at=report.exchange_timestamp or now,
            note=report.status_message_raw or report.status,
            broker_order_id=report.broker_order_id,
        )
        order.broker_order_id = report.broker_order_id
        return ""

    def _resolve_absence(
        self, order: OrderRecord, horizon: VisibilityHorizon, *, now: datetime
    ) -> OrderReconciliation:
        """An order the broker is not showing. Time is the only evidence available — sometimes.

        **Absence means different things at different points in an order's life, and conflating
        them was a real defect** found by the stateful property test rather than by review. An
        order that was never acknowledged may legitimately be declared abandoned once the
        visibility horizon has passed. An order the broker HAS acknowledged and has now stopped
        listing is something else entirely: `orders()` returns the whole day's book, so a
        previously-seen order going missing is a disagreement about a live order, and inferring
        "abandoned" would quietly delete an order that may still be working. It is reported as a
        conflict for a human instead.
        """
        age_seconds = (now - order.created_at).total_seconds()
        never_acknowledged = order.state in (
            OrderLifecycleState.INTENT_RECORDED,
            OrderLifecycleState.SUBMISSION_IN_FLIGHT,
            OrderLifecycleState.AMBIGUOUS,
        )
        if not never_acknowledged:
            detail = (
                f"the broker acknowledged this order and its day book no longer lists it, while "
                f"this system believes it is {order.state.value}. That is not evidence of "
                f"abandonment — the order may still be live — so it is recorded as a conflict "
                f"rather than inferred away"
            )
            return OrderReconciliation(
                verdict=ReconciliationVerdict.STATE_CONFLICT_BROKER_WON,
                intent_id=order.intent_id,
                broker_order_id=order.broker_order_id,
                trading_symbol=order.trading_symbol,
                local_state=order.state,
                broker_status="absent from the day book",
                local_filled_quantity=order.filled_quantity,
                broker_filled_quantity=0,
                detail=detail,
            )
        if horizon.seconds is not None and age_seconds > horizon.seconds:
            detail = (
                f"absent from the broker's order book {age_seconds:.1f}s after it was recorded, "
                f"beyond the measured visibility horizon of {horizon.seconds:.1f}s "
                f"({horizon.detail}); the order is abandoned rather than resent, because resending "
                f"is the one action that can double a position"
            )
            self.journal.record_event(
                order.intent_id,
                LifecycleEvent.PROVEN_ABSENT,
                EventSource.INFERRED,
                at=now,
                note=detail,
            )
            order.apply_event(
                LifecycleEvent.PROVEN_ABSENT, EventSource.INFERRED, at=now, note=detail
            )
            verdict = ReconciliationVerdict.LOCAL_ONLY_ABANDONED
        else:
            detail = (
                f"absent from the broker's order book {age_seconds:.1f}s after it was recorded, "
                f"which is not yet evidence of absence ({horizon.detail}); held unresolved"
            )
            verdict = ReconciliationVerdict.LOCAL_ONLY_UNRESOLVED
        return OrderReconciliation(
            verdict=verdict,
            intent_id=order.intent_id,
            broker_order_id=None,
            trading_symbol=order.trading_symbol,
            local_state=order.state,
            broker_status="",
            local_filled_quantity=order.filled_quantity,
            broker_filled_quantity=0,
            detail=detail,
            inferred=verdict is ReconciliationVerdict.LOCAL_ONLY_ABANDONED,
        )


def _adopt_external(report: VenueOrderReport) -> OrderReconciliation:
    """An order at the broker that this system did not place — usually a human, in the app."""
    return OrderReconciliation(
        verdict=ReconciliationVerdict.BROKER_ONLY,
        intent_id=None,
        broker_order_id=report.broker_order_id,
        trading_symbol=report.trading_symbol,
        local_state=None,
        broker_status=report.status,
        local_filled_quantity=0,
        broker_filled_quantity=report.filled_quantity,
        detail=(
            "the broker has an order this system did not place; it is adopted as EXTERNAL and its "
            "position counts, but it is never attributed to a strategy"
        ),
    )


def _residual_price_paise(order: OrderRecord, report: VenueOrderReport, missing: int) -> Decimal:
    """Price the units this system is inventing — not the units the broker already averaged.

    `average_price_paise` is the broker's average over ALL filled units. The inferred fill covers
    only the MISSING ones, so reusing the overall average books a cost basis that provably
    contradicts the number the reconciler just read. The R.23(c) review measured 2,500 paise per
    unit of error on a 2,850-rupee stock — about 0.9%. The residual solves out of the two averages:

        residual = (broker_filled x broker_average - local_filled x local_average) / missing

    and if that comes out non-positive the reconciler refuses rather than booking a nonsense price.
    """
    broker_average = report.average_price_paise
    if broker_average is None:
        return _average_or_reference(order)
    local_filled = Decimal(order.filled_quantity)
    local_average = order.average_fill_price_paise or Decimal(0)
    residual_value = Decimal(report.filled_quantity) * broker_average - local_filled * local_average
    residual = residual_value / Decimal(missing)
    if residual <= 0:
        raise ReconciliationRefusedError(
            f"order {order.intent_id}: the broker's average of {broker_average} over "
            f"{report.filled_quantity} units cannot be reconciled with {local_filled} local units "
            f"at {local_average} — the {missing} missing units solve to {residual} paise, which is "
            f"not a price. One of the two numbers is wrong and inventing a third would hide which"
        )
    return residual


def _average_or_reference(order: OrderRecord) -> Decimal:
    average = order.average_fill_price_paise
    if average is not None:
        return average
    limit = order.expression.limit_price_paise
    if limit is not None:
        return limit
    raise ReconciliationRefusedError(
        f"order {order.intent_id} has a quantity gap and no price anywhere to value it at — "
        f"neither the broker's average, nor a previous fill, nor a limit price. Inventing one "
        f"would put a fabricated cost basis into the book"
    )


def _terminal_state_for(event: LifecycleEvent) -> OrderLifecycleState | None:
    return {
        LifecycleEvent.FULLY_FILLED: OrderLifecycleState.FILLED,
        LifecycleEvent.CANCELLED: OrderLifecycleState.CANCELLED,
        LifecycleEvent.REJECTED: OrderLifecycleState.REJECTED,
        LifecycleEvent.EXPIRED: OrderLifecycleState.EXPIRED,
    }.get(event)
