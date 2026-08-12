"""The gate: no signal reaches capital without clearing what it actually costs to trade.

This is the point of the whole `L1` layer. `L1.01` prices the statutory and broker charges,
`L1.05`/`L1.06` price the spread and the impact, and neither changes a single decision on its
own. This does.

    hurdle = statutory round-trip cost + execution cost
    verdict = PASS if edge clears the hurdle, RESIZE if a smaller size would, else VETO

**The safety margin is DERIVED, not chosen.** The plan carries "fire only if edge exceeds cost by
1.5-2x" as a prior, and `A.12` is explicit that the multiplier is a prior rather than a constant.
`R.03` forbids shipping it as a magic number, so this gate does not use a multiplier at all.
Instead it requires the edge to clear the **pessimistic end of the cost interval** — and that
interval is measured, widening exactly when the estimate is least trustworthy: when the order
exceeds the visible book, when the instrument is thinly measured, when no fills have accrued.

That gives the margin for free and gives it the right shape. A liquid instrument at small size
has a tight interval and a hurdle barely above its point cost. An illiquid one at size has an
interval spanning hundreds of basis points, and the same rule demands proportionally more edge —
without anyone choosing a number, and without the multiplier being wrong in both directions at
once, which a single constant applied to both cases necessarily is.

**Why RESIZE is a solve and not a suggestion.** Cost per rupee traded RISES with size, because
impact does. So an edge that fails at the proposed quantity may clear at a smaller one, and the
largest quantity that still clears is a real boundary worth finding: it is the most capital this
signal can carry while remaining worth taking. The gate finds it by bisection over the same
engines that produced the verdict, so a resized signal has been priced, not estimated.

**Refusal beats a guess, everywhere.** If the book cannot be read, if the date has no compiled
rates, if the instrument was never measured — the gate returns `UNPRICEABLE`, which is not a
veto. A veto says "this trade is not worth taking". `UNPRICEABLE` says "I do not know", and
those must never be the same verdict, because the second one silently becomes the first in every
system that conflates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.cost_gate.tradeable_ticket_preconditions import (
    PreconditionReport,
    evaluate_preconditions,
)
from nse_algo_trader.execution_fill.execution_fill_model import (
    ExecutionFillError,
    ExecutionFillModel,
    ExpectedFill,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.transaction_cost.chargeable_market_segments import TaxableBase, TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    RoundTripCost,
    TradeSpecification,
    TransactionCostError,
)


class CostGateError(Exception):
    """The gate itself is misconfigured. Distinct from a trade being unpriceable."""


class GateVerdict(StrEnum):
    """What the gate decided, and the distinction that matters most is the last one.

    `VETO` is a judgement: the trade was priced and is not worth taking. `UNPRICEABLE` is an
    absence of judgement: something could not be read, so no opinion exists. Conflating them
    turns every data outage into a silent flood of confident rejections, and — far worse in the
    other direction — invites a caller to treat "I do not know" as "it is fine".
    """

    PASS = "pass"  # noqa: S105 - a verdict, not a password
    RESIZE = "resize"
    VETO = "veto"
    UNPRICEABLE = "unpriceable"


@dataclass(frozen=True, slots=True)
class CostHurdle:
    """Everything standing between a signal and a profit, decomposed."""

    statutory_bps: Decimal
    execution_point_bps: Decimal
    execution_upper_bps: Decimal
    is_execution_censored: bool

    @property
    def point_bps(self) -> Decimal:
        """The expected hurdle — used for sizing and reporting, never for the decision."""
        return self.statutory_bps + self.execution_point_bps

    @property
    def required_bps(self) -> Decimal:
        """The hurdle a signal must actually clear: statutory plus the PESSIMISTIC execution end.

        This is the derived safety margin. It is not a multiple of anything; it is the cost
        under the least favourable execution the measurement supports, so uncertainty raises the
        bar exactly in proportion to how uncertain it is.
        """
        return self.statutory_bps + self.execution_upper_bps

    @property
    def uncertainty_bps(self) -> Decimal:
        """How much of the required hurdle is uncertainty rather than expected cost."""
        return self.required_bps - self.point_bps


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The verdict, the arithmetic behind it, and what would have changed it."""

    verdict: GateVerdict
    signal: PricedSignal
    hurdle: CostHurdle | None
    approved_quantity: int
    reason: str
    round_trip_cost: RoundTripCost | None = None
    expected_fill: ExpectedFill | None = None
    preconditions: PreconditionReport | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.verdict in (GateVerdict.PASS, GateVerdict.RESIZE)

    @property
    def net_edge_bps(self) -> Decimal | None:
        """Edge after the required hurdle. Negative on a veto, by definition."""
        if self.hurdle is None:
            return None
        return self.signal.expected_edge_bps - self.hurdle.required_bps

    @property
    def shortfall_bps(self) -> Decimal | None:
        """How much more edge this signal would have needed. `None` unless vetoed."""
        net = self.net_edge_bps
        if net is None or net >= 0:
            return None
        return -net

    def describe(self) -> str:
        if self.hurdle is None:
            return f"{self.verdict.value.upper()} {self.signal.trading_symbol}: {self.reason}"
        return (
            f"{self.verdict.value.upper()} {self.signal.trading_symbol} "
            f"qty {self.signal.proposed_quantity}->{self.approved_quantity}: edge "
            f"{self.signal.expected_edge_bps:.1f} bps vs hurdle {self.hurdle.required_bps:.1f} "
            f"(statutory {self.hurdle.statutory_bps:.1f} + execution "
            f"{self.hurdle.execution_upper_bps:.1f}) — {self.reason}"
        )


class PreTradeCostGate:
    """Prices a signal and decides whether it may become an order.

    Holds no thresholds. Everything it compares against is computed from the two cost engines
    at the moment of the decision, which is what makes it re-derivable: a decision from last
    Tuesday can be reproduced exactly by replaying that Tuesday's book and rates.
    """

    def __init__(
        self,
        cost_engine: NseTransactionCostEngine,
        fill_model: ExecutionFillModel,
        *,
        minimum_edge_basis: EdgeBasis | None = None,
    ) -> None:
        self._cost_engine = cost_engine
        self._fill_model = fill_model
        self._minimum_edge_basis = minimum_edge_basis

    def evaluate(
        self,
        signal: PricedSignal,
        snapshot: BookSnapshot,
        *,
        trade_date: date | None = None,
        known_as_of: date | None = None,
        range_width_bps: Decimal | None = None,
    ) -> GateDecision:
        """Decide. Never raises for an unpriceable trade — it returns `UNPRICEABLE`.

        `range_width_bps` is supplied only by a caller proposing a RANGE trade, and is checked
        against the hurdle by `L11.99`'s precondition. Omitting it is not a failure.
        """
        if self._minimum_edge_basis is not None and not _basis_is_at_least(
            signal.edge_basis, self._minimum_edge_basis
        ):
            return GateDecision(
                verdict=GateVerdict.VETO,
                signal=signal,
                hurdle=None,
                approved_quantity=0,
                reason=(
                    f"edge basis {signal.edge_basis.value} is weaker than the required "
                    f"{self._minimum_edge_basis.value}"
                ),
            )

        as_of = trade_date or signal.decided_at.date()
        try:
            hurdle, cost, fill = self._hurdle_for(
                signal, snapshot, signal.proposed_quantity, as_of, known_as_of
            )
        except (TransactionCostError, ExecutionFillError) as failure:
            return GateDecision(
                verdict=GateVerdict.UNPRICEABLE,
                signal=signal,
                hurdle=None,
                approved_quantity=0,
                reason=(
                    f"cannot price this trade, so there is no opinion to give: {failure}. This "
                    f"is NOT a veto — it must not be read as one"
                ),
            )

        preconditions = evaluate_preconditions(
            signal,
            fill,
            flat_charges_paise=_flat_charges_paise(cost),
            hurdle_bps=hurdle.required_bps,
            range_width_bps=range_width_bps,
        )
        if not preconditions.all_satisfied:
            # These are economic facts about the TICKET, independent of whether the signal is
            # right. Resizing cannot fix a spread that exceeds the edge or a denominator that
            # was wrong, so this is a veto rather than a resize.
            return GateDecision(
                verdict=GateVerdict.VETO,
                signal=signal,
                hurdle=hurdle,
                approved_quantity=0,
                reason=f"precondition failed — {preconditions.describe()}",
                round_trip_cost=cost,
                expected_fill=fill,
                preconditions=preconditions,
            )

        if signal.expected_edge_bps >= hurdle.required_bps:
            return GateDecision(
                verdict=GateVerdict.PASS,
                signal=signal,
                hurdle=hurdle,
                approved_quantity=signal.proposed_quantity,
                reason="edge clears the hurdle at the proposed size",
                round_trip_cost=cost,
                expected_fill=fill,
                preconditions=preconditions,
            )

        viable = self._largest_viable_quantity(signal, snapshot, as_of, known_as_of)
        if viable is None:
            return GateDecision(
                verdict=GateVerdict.VETO,
                signal=signal,
                hurdle=hurdle,
                approved_quantity=0,
                reason=(
                    "no size clears the hurdle; the flat charges alone exceed the edge, so this "
                    "signal is not tradeable at any quantity"
                ),
                round_trip_cost=cost,
                expected_fill=fill,
                preconditions=preconditions,
            )

        resized_hurdle, resized_cost, resized_fill = self._hurdle_for(
            signal, snapshot, viable, as_of, known_as_of
        )
        return GateDecision(
            verdict=GateVerdict.RESIZE,
            signal=signal,
            hurdle=resized_hurdle,
            approved_quantity=viable,
            reason=(
                f"the proposed size does not clear the hurdle but {viable} units does; impact "
                f"rises with size, so this is the most capital the signal can carry"
            ),
            round_trip_cost=resized_cost,
            expected_fill=resized_fill,
            preconditions=preconditions,
        )

    # ------------------------------------------------------------------ internals

    def _hurdle_for(
        self,
        signal: PricedSignal,
        snapshot: BookSnapshot,
        quantity: int,
        as_of: date,
        known_as_of: date | None,
    ) -> tuple[CostHurdle, RoundTripCost, ExpectedFill]:
        """Both cost engines at one size. Raises if either cannot price it."""
        trade = TradeSpecification(
            segment=signal.segment,
            quantity=quantity,
            entry_price_paise=signal.reference_price_paise,
            exit_price_paise=signal.reference_price_paise,
            trade_date=as_of,
            strike_paise=signal.strike_paise,
            is_short_first=signal.side is not None and signal.side.value == "sell",
        )
        cost = self._cost_engine.price_round_trip(trade, known_as_of=known_as_of)
        # Both legs are priced against their OWN ladder. A buy entry lifts asks; its exit is a
        # sell that hits bids, and those are different ladders with different depth.
        #
        # Doubling one side instead was measured against `entry + exit` on 2,398 real books:
        # median error +0.6% but a p5 of -29.7% and a worst case of -83.1%, with 25.8% of books
        # UNDERSTATED by more than 10% — the direction that lets a losing trade through. The
        # contemporaneous asymmetry is measurable right now, so discarding it was a choice to
        # be wrong rather than a limit of the data.
        entry_fill = self._fill_model.price_fill(snapshot, signal.side, quantity)
        exit_fill = self._fill_model.price_fill(snapshot, _opposite_of(signal.side), quantity)
        return (
            CostHurdle(
                statutory_bps=cost.exact_bps_of_turnover,
                execution_point_bps=entry_fill.point_cost_bps + exit_fill.point_cost_bps,
                execution_upper_bps=entry_fill.upper_cost_bps + exit_fill.upper_cost_bps,
                is_execution_censored=entry_fill.is_censored or exit_fill.is_censored,
            ),
            cost,
            entry_fill,
        )

    def _clears(
        self,
        signal: PricedSignal,
        snapshot: BookSnapshot,
        quantity: int,
        as_of: date,
        known_as_of: date | None,
    ) -> bool:
        try:
            hurdle, _, _ = self._hurdle_for(signal, snapshot, quantity, as_of, known_as_of)
        except (TransactionCostError, ExecutionFillError):
            return False
        return signal.expected_edge_bps >= hurdle.required_bps

    def _largest_viable_quantity(
        self,
        signal: PricedSignal,
        snapshot: BookSnapshot,
        as_of: date,
        known_as_of: date | None,
    ) -> int | None:
        """The biggest size that clears, or `None` if none does.

        **The hurdle is U-SHAPED in quantity, not monotone**, and an earlier version of this
        method assumed otherwise. Execution cost rises with size, but STATUTORY cost per rupee
        FALLS — a flat per-order brokerage divided by a growing turnover — so the total has a
        minimum somewhere in the middle. Measured on real books, 35% of instruments have that
        minimum at a size above one unit.

        The consequence of assuming monotonicity was not a rounding error. The method tested
        one unit, and on failure returned `None`, which the gate reports as "no size clears the
        hurdle ... not tradeable at any quantity" — a statement of fact that was simply false.
        On a sweep of the real universe it refused real tickets of Rs 2.6 lakh to Rs 20 lakh
        that demonstrably cleared at larger sizes, and the reason string gave an operator no
        hint that the gate was wrong.

        So the search is a coarse geometric scan followed by a local refinement, which makes no
        monotonicity assumption at all. It costs a bounded number of pricings — roughly twenty
        against a bisection's dozen — to stop asserting something false about a third of the
        universe.
        """
        proposed = signal.proposed_quantity
        candidates = _scan_quantities(proposed)
        clearing = [
            quantity
            for quantity in candidates
            if self._clears(signal, snapshot, quantity, as_of, known_as_of)
        ]
        if not clearing:
            return None
        best = max(clearing)
        if best == proposed:
            return best
        # Refine upward between the largest clearing grid point and the next one that did not,
        # so the answer is the real boundary rather than the nearest point on a coarse grid.
        upper = min(
            (quantity for quantity in candidates if quantity > best),
            default=proposed,
        )
        low, high = best, upper
        while low < high:
            middle = (low + high + 1) // 2
            if self._clears(signal, snapshot, middle, as_of, known_as_of):
                low = middle
            else:
                high = middle - 1
        return low


def _scan_quantities(proposed: int, *, steps_per_decade: int = 4) -> tuple[int, ...]:
    """A geometric ladder from one unit to the proposed size, plus both endpoints.

    Geometric rather than linear because the hurdle's shape is driven by ratios — a flat charge
    diluting over turnover — so equal ratio steps sample the curve evenly where it actually
    bends. Bounded by construction: the ladder has a fixed number of points per decade, so a
    signal proposing a million units costs the same number of pricings as one proposing a
    thousand.
    """
    if proposed <= 1:
        return (1,)
    quantities = {1, proposed}
    step = Decimal(10) ** (Decimal(1) / Decimal(steps_per_decade))
    current = Decimal(1)
    while current < proposed:
        current *= step
        quantity = int(current)
        if 1 < quantity < proposed:
            quantities.add(quantity)
    return tuple(sorted(quantities))


def _opposite_of(side: TradeLeg) -> TradeLeg:
    """The leg that closes a position opened on `side`, and therefore the other ladder."""
    return TradeLeg.SELL if side is TradeLeg.BUY else TradeLeg.BUY

_BASIS_STRENGTH: dict[EdgeBasis, int] = {
    EdgeBasis.MEASURED_TRACK_RECORD: 3,
    EdgeBasis.CALIBRATED_MODEL: 2,
    EdgeBasis.STRATEGY_HYPOTHESIS: 1,
    EdgeBasis.OPERATOR_ASSERTION: 0,
}


def _basis_is_at_least(actual: EdgeBasis, required: EdgeBasis) -> bool:
    return _BASIS_STRENGTH[actual] >= _BASIS_STRENGTH[required]


def _flat_charges_paise(cost: RoundTripCost) -> Decimal:
    """The size-independent part of the round trip — what `L11.107`'s ticket rule bites on.

    Brokerage that has reached its flat cap, plus any per-debit depository charge. These are
    the charges whose PERCENTAGE explodes as the ticket shrinks, which is the whole reason a
    minimum ticket exists.
    """
    flat = Decimal(0)
    for line in cost.lines:
        if line.taxable_base in (TaxableBase.PER_ORDER, TaxableBase.PER_DEBIT_TRANSACTION):
            flat += line.exact_paise
    return flat
