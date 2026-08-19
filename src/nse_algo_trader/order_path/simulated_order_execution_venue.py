"""The simulated half of the venue seam — the other implementation of `L9.03`'s one interface.

`L9.03` calls paper/live parity "the architectural decision the whole system rests on". Parity is
not achieved by this module being *similar* to `kite_order_execution_venue`; it is achieved by both
satisfying `OrderExecutionVenue` exactly, so that every line above the seam — the placer, the
journal, the reconciler, the state machine — is literally the same code in paper and in live. A
simulator that returned a different shape would force a second code path, and the second path is
the one that is never exercised on a bad morning.

**What is deliberately NOT the same, so it is never mistaken for a defect:**

* **The order id namespace.** Every id this venue issues begins with `SIM-ORD-` and every trade id
  with `SIM-TRD-`. Zerodha's ids are decimal digit strings, so a simulated id cannot be mistaken
  for a real one by a human, a log grep, or `is_simulated_broker_order_id`. This is the same
  reasoning as the namespace character in the broker tag (`trading_intent.OrderNamespace`), applied
  to the other identifier. The rest of the id is a digest of the ORDER'S OWN CONTENT rather than a
  position in a counter, so that a restarted paper session cannot reissue an id it already used —
  see `SimulatedOrderExecutionVenue._broker_order_id_for` for the full argument.
* **Margin is not modelled, and this venue says so instead of pretending.** It has no funds view,
  no span/exposure calculation and no MTF ledger, so it CANNOT produce the margin rejection a real
  account would. Every position report it emits is stamped with `SIMULATED_POSITION_VIEW`, which
  names that absence in the data rather than in a comment nobody reads.
* **It refuses to fill when no book has been supplied.** A simulator that invents a price is worse
  than no simulator: it produces a full, confident, plausible fill history that a strategy is then
  tuned against. Without a snapshot for the instrument this venue acknowledges the order, leaves it
  OPEN, and records WHY it did not fill — readable through `refusal_to_fill_reason_for`.

**Where the fills come from.** `execution_fill/order_book_walk_calculator.walk_order_book` — the
same engine `F01` prices with, which is NSE's own impact-cost arithmetic. Each poll consumes one
rung of the visible ladder, at that rung's real price, for that rung's real quantity. Two
consequences are deliberate:

* partial fills happen naturally, because a rung is usually smaller than an order, and they arrive
  across successive polls exactly as they do from a real venue;
* when the visible ladder for the current snapshot is exhausted, filling STOPS until a new snapshot
  arrives. The extrapolated part of `execution_fill_model` — the impact estimate beyond visible
  depth — is used to record what the fill was EXPECTED to cost (`expected_fill_for`), and is never
  used to manufacture a fill, because an extrapolation is an estimate of a cost and not evidence
  that liquidity existed.

**Determinism.** No clocks, no randomness, no wall-time. Ids are derived from the ORDER'S OWN
CONTENT (see `_broker_order_id_for`), times come from the caller or from the snapshot, and prices
come from the tape. The same sequence of calls against the same tape produces byte-identical
results, which is what makes a paper run reproducible and a regression in the strategy attributable
to the strategy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import astuple, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from nse_algo_trader.execution_fill.execution_fill_model import (
    ExecutionFillError,
    ExecutionFillModel,
    ExpectedFill,
)
from nse_algo_trader.execution_fill.order_book_walk_calculator import (
    LevelConsumption,
    OrderBookWalkError,
    walk_order_book,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.order_path.broker_order_facility_facts import OrderType
from nse_algo_trader.order_path.order_execution_venue import (
    VenueOrderReport,
    VenuePositionReport,
    VenueRejectedError,
    VenueTradeReport,
)
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import (
    INDIA_MARKET_TIMEZONE,
    OrderNamespace,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

SIMULATED_VENUE_NAME = "simulated_venue"

# The id namespace. Zerodha's order ids are decimal digit strings ("250813000012345"), so a prefix
# that is not a digit makes the two sets provably disjoint rather than merely unlikely to collide.
SIMULATED_ORDER_ID_PREFIX = "SIM-ORD-"
SIMULATED_TRADE_ID_PREFIX = "SIM-TRD-"

# Sixty-four bits of BLAKE2b over the order's own content. Wide enough that two DIFFERENT orders
# colliding is not something that happens inside a session (a birthday collision needs ~4 billion
# orders against a per-day ceiling of 5,000), narrow enough that a human can read the id off a log
# line and match it by eye against another one. Derived from the digest, never typed as a length.
SIMULATED_ORDER_DIGEST_BYTES = 8
SIMULATED_ORDER_DIGEST_HEX_LENGTH = SIMULATED_ORDER_DIGEST_BYTES * 2

# ASCII UNIT SEPARATOR, joining the content fields before they are hashed. A character no trading
# symbol, tag, reason string or timestamp can contain, so no two different orders can be flattened
# into the same byte string by a field boundary landing in a different place.
_CONTENT_FIELD_SEPARATOR = "\x1f"

# Stamped on every position this venue reports. It is a sentence rather than a word because it is
# the mechanism by which "this account models no margin and no settlement" reaches a reader who is
# looking at data instead of at code.
SIMULATED_POSITION_VIEW = "simulated.fills_only_no_margin_or_settlement_modelled"

# Kite's own status vocabulary (`docs/research/222` §2), reused verbatim so that the identical
# `lifecycle_event_for_broker_status` mapping serves both venues. A simulator with its own words
# would need its own translation table, and the translation table is where a status becomes a
# state — the single most consequential mapping in the order path.
STATUS_ACKNOWLEDGED = "PUT ORDER REQ RECEIVED"
STATUS_OPEN = "OPEN"
STATUS_TRIGGER_PENDING = "TRIGGER PENDING"
STATUS_COMPLETE = "COMPLETE"
STATUS_CANCELLED = "CANCELLED"
STATUS_REJECTED = "REJECTED"

_TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_CANCELLED, STATUS_REJECTED})

# `docs/research/222` §4. The same cap the real venue enforces, so a strategy that repeatedly
# reprices discovers the ceiling in paper rather than in production.
MAXIMUM_MODIFICATIONS_PER_ORDER = 25

_ORDER_TYPES_WITHOUT_A_PRICE_LIMIT = frozenset({OrderType.MARKET, OrderType.STOP_LOSS_MARKET})
_ORDER_TYPES_WITH_A_TRIGGER = frozenset({OrderType.STOP_LOSS_LIMIT, OrderType.STOP_LOSS_MARKET})


class FreezeQuantityExceededError(VenueRejectedError):
    """The order is larger than the configured freeze quantity for its instrument.

    Shaped after the real thing: `docs/research/222` §8 records the verbatim pattern
    `"RMS:Rule: Check freeze quantity for NSE CASH"`, and this venue reproduces that wording so a
    handler written against paper output is written against the string live output will carry.
    """


class UnknownSimulatedOrderError(VenueRejectedError):
    """An order id this venue never issued, or one from another venue's namespace."""


class SimulatedVenueRefusedLiveOrderError(VenueRejectedError):
    """A LIVE-namespace order was handed to the simulator.

    The mirror image of the live venue's refusal of a SIMULATED order. An order named LIVE that is
    "filled" here has a fill history no money stands behind, and the whole point of the namespace
    is that the question "did real money move" is answerable from the order alone.
    """


def is_simulated_broker_order_id(broker_order_id: str) -> bool:
    """Whether an id came from this venue. Cheap, total, and usable anywhere in the system."""
    return broker_order_id.startswith(SIMULATED_ORDER_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class SimulatedVenueLimits:
    """The rejection surface this venue models — all of it configured, none of it assumed.

    NSE revises freeze quantities periodically (`docs/research/222` §8), and they are per
    underlying, so a number compiled into this module would be wrong within a quarter and wrong
    silently. An instrument absent from the mapping is NOT given a default limit: this venue then
    models no freeze rejection for it, and says so, rather than inventing a ceiling that would
    reject an order the real venue would have accepted.
    """

    freeze_quantity_by_trading_symbol: Mapping[str, int] = field(default_factory=dict)
    single_order_quantity_cap: int | None = None
    """`docs/research/222` §8 records 100,000 shares as a single-order cap (VERIFIED-secondary).
    Left as `None` — unmodelled — until the operator supplies it, because a secondary-source number
    hardcoded here would be indistinguishable from a primary-source one."""

    def rejection_for(self, trading_symbol: str, quantity: int) -> str | None:
        """The RMS string this order would be rejected with, or `None` if it passes."""
        freeze_quantity = self.freeze_quantity_by_trading_symbol.get(trading_symbol)
        if freeze_quantity is not None and quantity > freeze_quantity:
            return (
                f"RMS:Rule: Check freeze quantity for {trading_symbol}: "
                f"{quantity} exceeds {freeze_quantity}"
            )
        if self.single_order_quantity_cap is not None and quantity > self.single_order_quantity_cap:
            return (
                f"RMS:Rule: single order quantity {quantity} exceeds the configured cap "
                f"{self.single_order_quantity_cap}"
            )
        return None


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One execution this venue produced, and the rung of the real book it came from."""

    trade_id: str
    quantity: int
    price_paise: Decimal
    occurred_at: datetime
    from_book_at: datetime
    rung_index: int


@dataclass(slots=True)
class SimulatedOrder:
    """This venue's own copy of an order. The caller's `OrderRecord` is never mutated.

    A venue that wrote into the record it was handed would make the journal's account of an order
    and the venue's account of it the same object, and the entire reconciliation design exists
    because those two are separate claims that have to be compared.
    """

    broker_order_id: str
    broker_tag: str
    trading_symbol: str
    instrument_token: int
    product: str
    side: TradeLeg
    ordered_quantity: int
    expression: OrderExpression
    placed_at: datetime
    status: str
    fills: tuple[SimulatedFill, ...] = ()
    history: tuple[VenueOrderReport, ...] = ()
    modifications: int = 0
    status_message: str = ""
    refusal_to_fill_reason: str = ""
    expected_fill: ExpectedFill | None = None
    consumed_rungs: int = 0
    consumed_book_key: tuple[datetime, int] | None = None

    @property
    def filled_quantity(self) -> int:
        return sum(fill.quantity for fill in self.fills)

    @property
    def pending_quantity(self) -> int:
        return self.ordered_quantity - self.filled_quantity

    @property
    def average_price_paise(self) -> Decimal | None:
        filled = self.filled_quantity
        if filled == 0:
            return None
        total = sum((fill.price_paise * Decimal(fill.quantity) for fill in self.fills), Decimal(0))
        return total / Decimal(filled)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def session_date(self) -> date:
        return self.placed_at.astimezone(INDIA_MARKET_TIMEZONE).date()

    def as_report(self) -> VenueOrderReport:
        return VenueOrderReport(
            broker_order_id=self.broker_order_id,
            tag=self.broker_tag,
            status=self.status,
            trading_symbol=self.trading_symbol,
            quantity=self.ordered_quantity,
            filled_quantity=self.filled_quantity,
            pending_quantity=self.pending_quantity,
            average_price_paise=self.average_price_paise,
            status_message=self.status_message,
            status_message_raw=self.status_message,
            exchange_timestamp=self.fills[-1].occurred_at if self.fills else self.placed_at,
            placed_by=SIMULATED_VENUE_NAME,
        )


class SimulatedOrderExecutionVenue:
    """A deterministic venue that fills from the recorded depth tape, or refuses to fill at all.

    **Variety is carried, not simulated.** `variety` changes how an order is ROUTED at a real
    broker — an AMO is queued to the open, an iceberg is sliced into legs, a cover order carries a
    mandatory stop leg that cannot be cancelled alone — and none of that routing is modelled here.
    Every variety fills the same way: rung by rung off the tape. A paper run of an iceberg
    therefore measures the strategy and not the slicing, and its fills are not evidence about
    iceberg mechanics.
    """

    def __init__(
        self,
        *,
        limits: SimulatedVenueLimits | None = None,
        fill_model: ExecutionFillModel | None = None,
    ) -> None:
        self._limits = limits or SimulatedVenueLimits()
        self._fill_model = fill_model or ExecutionFillModel()
        self._orders: dict[str, SimulatedOrder] = {}
        self._trades: list[VenueTradeReport] = []
        self._books: dict[int, BookSnapshot] = {}
        # Placement order within THIS process, for readability of the ids it issues. Uniqueness
        # across a restart rests on the content digest, never on this (`_broker_order_id_for`).
        self._orders_placed = 0

    @property
    def venue_name(self) -> str:
        return SIMULATED_VENUE_NAME

    # --- the tape this venue fills from ---------------------------------------------------------

    def observe_book(self, snapshot: BookSnapshot) -> None:
        """Supply the current book for one instrument. Fills come from this and nowhere else."""
        self._books[snapshot.instrument_token] = snapshot

    def forget_book(self, instrument_token: int) -> bool:
        """Drop the book for one instrument, so nothing fills from a snapshot that has expired.

        Without this, a book supplied once is filled against forever: a replay whose tape stops
        mid-session — a capture that dropped, an instrument that stopped ticking — would keep
        filling orders from the last snapshot it ever saw, at prices that no longer had a
        counterparty behind them. That is exactly the invented fill history this venue exists to
        refuse, arriving through the back door of a stale cache rather than a missing one.

        Returns whether a book was actually dropped, so a caller can record the moment liquidity
        stopped being observable rather than inferring it later.
        """
        return self._books.pop(instrument_token, None) is not None

    def has_book_for(self, instrument_token: int) -> bool:
        return instrument_token in self._books

    # --- placement --------------------------------------------------------------------------

    def place(self, order: OrderRecord) -> str:
        """Acknowledge the order and return a `SIM-ORD-` id. Nothing fills until a poll."""
        if order.namespace is OrderNamespace.LIVE:
            raise SimulatedVenueRefusedLiveOrderError(
                f"order {order.intent_id} carries the LIVE namespace and was handed to the "
                f"simulator; an order named LIVE with a simulated fill history is a position no "
                f"money stands behind, and the namespace exists precisely so that cannot happen"
            )
        rejection = self._limits.rejection_for(order.trading_symbol, order.ordered_quantity)
        if rejection is not None:
            raise FreezeQuantityExceededError(rejection, raw_message=rejection)

        self._orders_placed += 1
        broker_order_id = self._broker_order_id_for(order, placement_ordinal=self._orders_placed)
        simulated = SimulatedOrder(
            broker_order_id=broker_order_id,
            broker_tag=order.broker_tag,
            trading_symbol=order.trading_symbol,
            instrument_token=order.instrument_token,
            product=order.expression.product.value,
            side=order.side,
            ordered_quantity=order.ordered_quantity,
            expression=order.expression,
            placed_at=order.created_at,
            status=STATUS_ACKNOWLEDGED,
            expected_fill=self._expectation_for(order),
        )
        simulated.history = (simulated.as_report(),)
        self._orders[broker_order_id] = simulated
        return broker_order_id

    def _broker_order_id_for(self, order: OrderRecord, *, placement_ordinal: int) -> str:
        """`SIM-ORD-<session>-<content digest>-<placement>` — an id a restart cannot reissue.

        **The defect this shape exists to prevent** (found by the `M/4` adversarial review). The
        earlier id was the session date and an in-memory placement counter, so the FIRST order of
        every process was `SIM-ORD-20260813-000001`. Restart a paper session at 10:00 — a crash,
        a redeploy, an operator stopping the loop — and the next order placed reissues the id the
        morning's first order already holds. Two different orders then share an identifier, which
        is the one thing an identifier exists to make impossible: the journal keys fills, history
        and reconciliation verdicts on it, so the second order inherits the first one's fills.

        **Why a content digest rather than a persisted counter.** A counter that survived a restart
        would need a file, and a file is a second thing that can be missing, stale, shared between
        two concurrent sessions, or restored from a backup taken before the orders it counted. The
        order's own content is already durable, already unique — `intent_id` names the decision and
        the journal refuses a duplicate of it on disk (`order_intent_journal.record_intent`) — and
        it needs no coordination at all. Two orders that differ in anything a broker would care
        about differ in the digest; two placements of the SAME order record differ in the trailing
        placement ordinal, which is the only case where a repeat is a repeat rather than a
        collision.

        **Why the placement ordinal is still there.** It is not load-bearing for uniqueness across
        a restart — the digest is — but it keeps the ids of one process in the order they were
        placed, which is what makes a log readable and a paper session inspectable by eye. It is
        also what separates two placements of a byte-identical order record within one process,
        which the venue must allow because the venue is not the idempotency guard; the journal is.

        **Determinism is preserved.** BLAKE2b of a canonical field join is a pure function of the
        order, so the same tape driven through the same decisions still produces byte-identical
        ids, which is the property that makes a paper run reproducible.
        """
        content = _CONTENT_FIELD_SEPARATOR.join(
            str(field_value)
            for field_value in (
                order.namespace,
                order.intent_id,
                order.broker_tag,
                order.trading_symbol,
                order.instrument_token,
                order.side,
                order.ordered_quantity,
                order.created_at.isoformat(),
                # The whole expression, field by field, rather than a chosen few: an order that
                # differs only in a field added to `OrderExpression` next year is still a different
                # order, and a hand-listed subset would silently stop covering it.
                *astuple(order.expression),
            )
        )
        digest = hashlib.blake2b(
            content.encode("utf-8"), digest_size=SIMULATED_ORDER_DIGEST_BYTES
        ).hexdigest()
        return (
            f"{SIMULATED_ORDER_ID_PREFIX}"
            f"{order.created_at.astimezone(INDIA_MARKET_TIMEZONE):%Y%m%d}-"
            f"{digest}-"
            f"{placement_ordinal:06d}"
        )

    def _expectation_for(self, order: OrderRecord) -> ExpectedFill | None:
        """What `F01`'s model expects this order to cost, recorded at placement.

        Recorded, never acted on. Its extrapolated arm is an estimate of a cost beyond visible
        depth; using it to fill would be treating an estimate as evidence that liquidity existed.
        Keeping it alongside the realised fills is what lets a paper session measure its own
        execution against what was predicted — which is the only way the model gets better.
        """
        snapshot = self._books.get(order.instrument_token)
        if snapshot is None:
            return None
        try:
            return self._fill_model.price_fill(snapshot, order.side, order.ordered_quantity)
        except ExecutionFillError:
            return None

    def expected_fill_for(self, broker_order_id: str) -> ExpectedFill | None:
        """The cost the fill model predicted at placement, or `None` when it could not price it."""
        return self._order(broker_order_id).expected_fill

    def refusal_to_fill_reason_for(self, broker_order_id: str) -> str:
        """Why the last poll produced no fill. Empty when the last poll did fill.

        This is the honest counterpart of inventing a price: the order stays OPEN and the reason it
        did not trade is readable, so a paper run that produced no fills can be told apart from a
        strategy that produced no signals.
        """
        return self._order(broker_order_id).refusal_to_fill_reason

    # --- amendment --------------------------------------------------------------------------

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None:
        """Reprice or resize a live order, under the same 25-modification cap as the real venue."""
        simulated = self._order(broker_order_id)
        if simulated.is_terminal:
            raise VenueRejectedError(
                f"order {broker_order_id} is {simulated.status} and cannot be modified",
                raw_message=f"order is {simulated.status}",
            )
        if simulated.modifications >= MAXIMUM_MODIFICATIONS_PER_ORDER:
            raise VenueRejectedError(
                f"order {broker_order_id} has been modified {simulated.modifications} times and "
                f"the cap is {MAXIMUM_MODIFICATIONS_PER_ORDER} (docs/research/222 §4)",
                raw_message="modification cap reached",
            )
        if quantity < simulated.filled_quantity:
            raise VenueRejectedError(
                f"order {broker_order_id} has already filled {simulated.filled_quantity} and "
                f"cannot be resized to {quantity}",
                raw_message="quantity below filled quantity",
            )
        simulated.expression = expression
        simulated.ordered_quantity = quantity
        simulated.modifications += 1
        # A reprice re-opens the ladder: the previous rung pointer belonged to the previous price
        # limit, and keeping it would silently skip rungs the new limit can now reach.
        simulated.consumed_rungs = 0
        simulated.consumed_book_key = None
        self._record_history(simulated)

    def cancel(self, broker_order_id: str, *, variety: str) -> None:
        """Cancel, or refuse because the order is already terminal — as the real venue would."""
        simulated = self._order(broker_order_id)
        if simulated.expression.variety.value != variety:
            raise VenueRejectedError(
                f"order {broker_order_id} was placed as variety "
                f"{simulated.expression.variety.value} and cancellation was asked for as "
                f"{variety}; Kite routes cancellation by variety, so the mismatch would cancel "
                f"nothing",
                raw_message="variety mismatch on cancel",
            )
        if simulated.is_terminal:
            raise VenueRejectedError(
                f"order {broker_order_id} is already {simulated.status} and cannot be cancelled",
                raw_message=f"order is {simulated.status}",
            )
        simulated.status = STATUS_CANCELLED
        simulated.status_message = "cancelled at the caller's request"
        self._record_history(simulated)

    # --- the matching engine ------------------------------------------------------------------

    def advance_matching_by_one_poll(self, *, at: datetime) -> tuple[VenueTradeReport, ...]:
        """Advance every live order by exactly one step, and return the trades that step produced.

        One step, not "until done", and that is the whole reason partial fills are testable here:
        an acknowledged order becomes OPEN on one poll and starts consuming the ladder on the next,
        one rung at a time, exactly as a real order arrives in pieces. Time in a simulator only
        passes when someone asks it to, so this is the tick — the `fetch_*` methods stay pure reads
        and never move the world underneath a reconciler that is trying to observe it.
        """
        produced: list[VenueTradeReport] = []
        for simulated in self._orders.values():
            if simulated.is_terminal:
                continue
            if simulated.status == STATUS_ACKNOWLEDGED:
                simulated.status = self._status_after_acknowledgement(simulated)
                self._record_history(simulated)
                continue
            if simulated.status == STATUS_TRIGGER_PENDING:
                if not self._trigger_is_crossed(simulated):
                    continue
                simulated.status = STATUS_OPEN
                self._record_history(simulated)
                continue
            trade = self._fill_one_rung(simulated, at=at)
            if trade is not None:
                produced.append(trade)
        self._trades.extend(produced)
        return tuple(produced)

    def _status_after_acknowledgement(self, simulated: SimulatedOrder) -> str:
        if simulated.expression.order_type in _ORDER_TYPES_WITH_A_TRIGGER:
            return STATUS_TRIGGER_PENDING
        return STATUS_OPEN

    def _trigger_is_crossed(self, simulated: SimulatedOrder) -> bool:
        """A stop arms when the last traded price reaches the trigger from the right side."""
        snapshot = self._books.get(simulated.instrument_token)
        trigger = simulated.expression.trigger_price_paise
        if snapshot is None or trigger is None:
            simulated.refusal_to_fill_reason = (
                "no book has been supplied for this instrument, so this venue cannot tell whether "
                "the trigger has been crossed; it will not guess"
            )
            return False
        last_price = Decimal(snapshot.last_price_paise)
        if simulated.side is TradeLeg.BUY:
            return last_price >= trigger
        return last_price <= trigger

    def _fill_one_rung(self, simulated: SimulatedOrder, *, at: datetime) -> VenueTradeReport | None:
        snapshot = self._books.get(simulated.instrument_token)
        if snapshot is None:
            simulated.refusal_to_fill_reason = (
                f"no book has been supplied for instrument {simulated.instrument_token}; this "
                f"venue fills from the recorded depth tape and refuses to invent a price, because "
                f"an invented fill history is worse than no fill history"
            )
            return None
        book_key = (snapshot.receipt_time, snapshot.receipt_sequence)
        if simulated.consumed_book_key != book_key:
            simulated.consumed_book_key = book_key
            simulated.consumed_rungs = 0
        remaining = simulated.pending_quantity
        if remaining <= 0:
            return None
        try:
            probe = walk_order_book(snapshot, simulated.side, remaining)
            ladder = walk_order_book(snapshot, simulated.side, probe.visible_quantity)
        except OrderBookWalkError as unusable:
            simulated.refusal_to_fill_reason = (
                f"the book for instrument {simulated.instrument_token} cannot be walked: {unusable}"
            )
            return None
        marketable = tuple(
            rung for rung in ladder.consumptions if self._rung_satisfies_limit(simulated, rung)
        )
        if simulated.consumed_rungs >= len(marketable):
            simulated.refusal_to_fill_reason = (
                f"the visible ladder in the book at {snapshot.receipt_time} is exhausted for this "
                f"order ({len(marketable)} rung(s) reachable at its price); filling further would "
                f"mean inventing depth the tape does not record, so the order stays open until a "
                f"newer book arrives"
            )
            return None
        rung = marketable[simulated.consumed_rungs]
        simulated.consumed_rungs += 1
        quantity = min(rung.available_quantity, remaining)
        # The trade id is derived from the ORDER's id and this fill's position within that order,
        # for the reason `_broker_order_id_for` gives: a process-local counter reissues its low
        # numbers after a restart, and a trade id that repeats attaches a fill to the wrong order.
        # Naming the order it belongs to also makes a trade greppable back to its parent, which a
        # bare sequence number never was (`M/4` adversarial review).
        trade_id = (
            f"{SIMULATED_TRADE_ID_PREFIX}"
            f"{simulated.broker_order_id.removeprefix(SIMULATED_ORDER_ID_PREFIX)}-"
            f"{len(simulated.fills) + 1:03d}"
        )
        fill = SimulatedFill(
            trade_id=trade_id,
            quantity=quantity,
            price_paise=Decimal(rung.price_paise),
            occurred_at=at,
            from_book_at=snapshot.receipt_time,
            rung_index=simulated.consumed_rungs - 1,
        )
        simulated.fills = (*simulated.fills, fill)
        simulated.refusal_to_fill_reason = ""
        simulated.status = STATUS_COMPLETE if simulated.pending_quantity == 0 else STATUS_OPEN
        # Kite has no partially-filled status: a part-filled order reads OPEN with a non-zero
        # filled_quantity (`docs/research/222` §2), and the fill ledger derives the partial state
        # from the quantities. Emitting a status Kite does not have would be the one difference
        # capable of making the shared lifecycle map behave differently in paper and in live.
        self._record_history(simulated)
        return VenueTradeReport(
            broker_trade_id=trade_id,
            broker_order_id=simulated.broker_order_id,
            trading_symbol=simulated.trading_symbol,
            quantity=quantity,
            price_paise=fill.price_paise,
            filled_at=at,
        )

    def _rung_satisfies_limit(self, simulated: SimulatedOrder, rung: LevelConsumption) -> bool:
        """A limit order only takes rungs at or better than its price. A stop-market takes any."""
        if simulated.expression.order_type in _ORDER_TYPES_WITHOUT_A_PRICE_LIMIT:
            return True
        limit = simulated.expression.limit_price_paise
        if limit is None:
            return False
        if simulated.side is TradeLeg.BUY:
            return Decimal(rung.price_paise) <= limit
        return Decimal(rung.price_paise) >= limit

    def _record_history(self, simulated: SimulatedOrder) -> None:
        simulated.history = (*simulated.history, simulated.as_report())

    # --- reconciliation surface ---------------------------------------------------------------

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        """Every order placed in that session. A pure read — the poll is a separate method."""
        return tuple(
            simulated.as_report()
            for simulated in self._orders.values()
            if simulated.session_date == session_date
        )

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        """Every status hop of one order, oldest first — the shape `order_history()` returns."""
        return self._order(broker_order_id).history

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        return tuple(
            trade
            for trade in self._trades
            if trade.filled_at.astimezone(INDIA_MARKET_TIMEZONE).date() == session_date
        )

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]:
        """Positions implied by the simulated fills — and NOTHING else, which the `view` says.

        There is exactly one view here, and it is stamped `SIMULATED_POSITION_VIEW`. The real venue
        returns three (`positions.day`, `positions.net`, `holdings`) because a real account has
        three, distinguished by settlement — and settlement is precisely what this venue does not
        model, along with margin, MTF, and the T+1 unsettled quantity that makes a sell fail after
        a buy. Emitting a `holdings` view here would be claiming a settlement state this venue has
        no basis for, so `unsettled_quantity` stays zero and the view name carries the caveat into
        every downstream reader instead of leaving it in a docstring.
        """
        net_quantity: dict[tuple[str, str], int] = {}
        signed_notional: dict[tuple[str, str], Decimal] = {}
        for simulated in self._orders.values():
            key = (simulated.trading_symbol, simulated.product)
            direction = 1 if simulated.side is TradeLeg.BUY else -1
            for fill in simulated.fills:
                net_quantity[key] = net_quantity.get(key, 0) + direction * fill.quantity
                signed_notional[key] = signed_notional.get(key, Decimal(0)) + (
                    Decimal(direction * fill.quantity) * fill.price_paise
                )
        reports: list[VenuePositionReport] = []
        for (trading_symbol, product), quantity in sorted(net_quantity.items()):
            # Signed notional over signed quantity, so a long and a short both report the price
            # the position was built at. A flat position (quantity zero) has no average price and
            # gets `None` rather than a division by zero dressed up as a number.
            average = (
                signed_notional[(trading_symbol, product)] / Decimal(quantity) if quantity else None
            )
            reports.append(
                VenuePositionReport(
                    trading_symbol=trading_symbol,
                    product=product,
                    quantity=quantity,
                    average_price_paise=average,
                    view=SIMULATED_POSITION_VIEW,
                )
            )
        return tuple(reports)

    # --- internals ----------------------------------------------------------------------------

    def _order(self, broker_order_id: str) -> SimulatedOrder:
        try:
            return self._orders[broker_order_id]
        except KeyError:
            raise UnknownSimulatedOrderError(
                f"{broker_order_id!r} was not issued by this venue; simulated ids begin with "
                f"{SIMULATED_ORDER_ID_PREFIX!r}, and an id from another namespace reaching here "
                f"means a live order and a simulated one have been confused",
                raw_message="unknown order id",
            ) from None
