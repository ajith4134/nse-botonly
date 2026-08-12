"""Why cost in basis points is a STAIRCASE in quantity, and what that closes off.

Per-order charges do not scale. A flat Rs 20 brokerage and a flat DP debit cost the same on
one lot as on fifty, so cost expressed as a fraction of turnover FALLS as quantity rises —
and it falls in steps, because the brokerage schedule itself is piecewise.

That staircase is the arithmetic that decides whether a segment is open to an account at all.
A Rs 1 lakh account cannot take a position large enough to dilute a flat Rs 20 across two legs
in a segment whose edge is a few basis points, and no amount of signal quality changes it.
This module answers that question directly rather than leaving it to be discovered through
losses:

- `cost_curve` — the whole step function, for the dashboard and for the minimum-edge floor
  that `L1.04` derives from it.
- `minimum_viable_quantity` — the smallest position whose round-trip cost clears a ceiling,
  solved at the piece boundaries rather than scanned.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise

from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import PAISE_PER_RUPEE
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    TradeSpecification,
    TransactionCostError,
)

_BASIS_POINTS = Decimal(10_000)


class QuantityEconomicsError(TransactionCostError):
    """The quantity question has no answer under these inputs."""


@dataclass(frozen=True, slots=True)
class CostAtQuantity:
    """One tread of the staircase."""

    quantity: int
    round_trip_cost_paise: Decimal
    turnover_paise: Decimal

    @property
    def cost_bps(self) -> Decimal:
        if self.turnover_paise == 0:
            raise QuantityEconomicsError("cost in bps of a zero turnover is undefined")
        return self.round_trip_cost_paise / self.turnover_paise * _BASIS_POINTS


@dataclass(frozen=True, slots=True)
class QuantityCostCurve:
    """The staircase, and the two facts a sizing decision actually wants from it."""

    segment_value: str
    price_paise: Decimal
    lot_size: int
    points: tuple[CostAtQuantity, ...]

    @property
    def cheapest(self) -> CostAtQuantity:
        return min(self.points, key=lambda point: point.cost_bps)

    @property
    def largest_cost_rise_bps(self) -> Decimal:
        """The biggest rise in cost-per-rupee as quantity grows. Should be ~0, and is not.

        Economically the curve can only fall: a flat charge diluting over a larger base, and a
        percentage leg that only ever gives way to a cap. Two different things can make it
        rise, and they need telling apart:

        - **Rounding**, which produces rises of the order of one rupee spread over the
          turnover — for a Rs 1 lakh cash trade that is well under a hundredth of a basis
          point. Real, harmless, and unavoidable while a broker rounds STT to the rupee.
        - **A malformed schedule** — an overlapping brokerage piece, or a flat charge
          accidentally multiplied by quantity — which produces rises orders of magnitude
          larger, and which shows up HERE and nowhere else, because the total cost still looks
          entirely sensible at every individual size.

        So the size is reported rather than a boolean asserted, and `is_monotonically_cheaper`
        compares it against what rounding could actually explain.
        """
        rises = [
            later.cost_bps - earlier.cost_bps
            for earlier, later in pairwise(self.points)
            if later.cost_bps > earlier.cost_bps
        ]
        return max(rises) if rises else Decimal(0)

    @property
    def rounding_explainable_rise_bps(self) -> Decimal:
        """The largest rise a whole-rupee rounding step could account for, at the smallest size.

        Derived from the curve's own turnover rather than declared: one rupee of rounding on
        the smallest position, expressed in bps of that position's turnover.
        """
        smallest = self.points[0]
        if smallest.turnover_paise == 0:
            return Decimal(0)
        return PAISE_PER_RUPEE / smallest.turnover_paise * _BASIS_POINTS

    @property
    def is_monotonically_cheaper(self) -> bool:
        """True when nothing bigger than rounding makes the curve rise."""
        return self.largest_cost_rise_bps <= self.rounding_explainable_rise_bps


def _quantity_ladder(lot_size: int, maximum_lots: int) -> tuple[int, ...]:
    if lot_size <= 0 or maximum_lots <= 0:
        raise QuantityEconomicsError(
            f"lot size and lot count must be positive, got {lot_size} and {maximum_lots}"
        )
    return tuple(lot_size * lots for lots in range(1, maximum_lots + 1))


def cost_curve(
    engine: NseTransactionCostEngine,
    template: TradeSpecification,
    *,
    lot_size: int,
    maximum_lots: int,
    known_as_of: date | None = None,
) -> QuantityCostCurve:
    """Round-trip cost at every whole number of lots from one to `maximum_lots`.

    The lot size is a parameter rather than an assumption because it is a live fact: NSE
    resizes contracts, and `L0.31`'s observed `LOT_SIZE` family is where a caller gets the one
    in force on the date. Hardcoding 50 or 75 here would silently misprice every date on the
    wrong side of a resize wave.
    """
    points = []
    for quantity in _quantity_ladder(lot_size, maximum_lots):
        priced = engine.price_round_trip(
            replace(template, quantity=quantity), known_as_of=known_as_of
        )
        points.append(
            CostAtQuantity(
                quantity=quantity,
                round_trip_cost_paise=priced.total_paise,
                turnover_paise=priced.entry_turnover_paise,
            )
        )
    return QuantityCostCurve(
        segment_value=template.segment.value,
        price_paise=template.entry_price_paise,
        lot_size=lot_size,
        points=tuple(points),
    )


def minimum_viable_quantity(
    engine: NseTransactionCostEngine,
    template: TradeSpecification,
    *,
    cost_bps_ceiling: Decimal,
    lot_size: int,
    maximum_lots: int,
    known_as_of: date | None = None,
) -> int | None:
    """The smallest whole-lot quantity whose round-trip cost is within the ceiling.

    `None` when no quantity up to `maximum_lots` clears it — which is the useful answer, not a
    failure: it says this segment is closed to this account at this price, and the caller
    should be told that rather than handed the least-bad size.

    Solved by bisection over LOTS rather than by scanning every size. Cost in bps is
    non-increasing in quantity (a flat charge diluting over a larger base, plus a brokerage
    percentage that only ever gives way to a cap), so the predicate "clears the ceiling" is
    monotone and bisection is valid. `cost_curve().is_monotonically_cheaper` is the assertion
    that keeps that assumption honest against a malformed schedule.
    """
    if cost_bps_ceiling <= 0:
        raise QuantityEconomicsError(f"cost ceiling must be positive, got {cost_bps_ceiling}")

    def clears(lots: int) -> bool:
        priced = engine.price_round_trip(
            replace(template, quantity=lot_size * lots), known_as_of=known_as_of
        )
        return priced.total_bps_of_turnover <= cost_bps_ceiling

    if not clears(maximum_lots):
        return None
    low, high = 1, maximum_lots
    while low < high:
        middle = (low + high) // 2
        if clears(middle):
            high = middle
        else:
            low = middle + 1
    # Bisection is valid on a monotone predicate, and the curve is monotone only up to
    # rounding: a broker rounding STT to the whole rupee can make one step cost a hundredth
    # of a basis point MORE per rupee than the step below it. That is enough to leave
    # bisection one lot high. So the answer is walked down while a smaller lot also clears —
    # bounded by construction, since the walk stops the moment one does not.
    while low > 1 and clears(low - 1):
        low -= 1
    return lot_size * low


def capital_bounded_lots(
    capital_paise: Decimal, price_paise: Decimal, lot_size: int
) -> int:
    """How many whole lots the capital can actually pay for.

    The other half of the same question: `minimum_viable_quantity` says what size costs allow,
    this says what the account allows, and a segment is only open when the first is not larger
    than the second.
    """
    if price_paise <= 0 or lot_size <= 0:
        raise QuantityEconomicsError(
            f"price and lot size must be positive, got {price_paise} and {lot_size}"
        )
    affordable = capital_paise / (price_paise * lot_size)
    return int(affordable.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True, slots=True)
class SegmentAccessVerdict:
    """Whether an account can trade a segment at all, and why not when it cannot."""

    segment_value: str
    minimum_viable_quantity: int | None
    affordable_quantity: int
    cost_bps_ceiling: Decimal

    @property
    def is_open(self) -> bool:
        return (
            self.minimum_viable_quantity is not None
            and self.minimum_viable_quantity <= self.affordable_quantity
        )

    @property
    def reason(self) -> str:
        if self.minimum_viable_quantity is None:
            return (
                f"no quantity within the tested range brings round-trip cost within "
                f"{self.cost_bps_ceiling} bps"
            )
        if not self.is_open:
            return (
                f"costs need {self.minimum_viable_quantity} units to dilute below "
                f"{self.cost_bps_ceiling} bps, but the capital affords {self.affordable_quantity}"
            )
        return (
            f"open: {self.minimum_viable_quantity} units clears {self.cost_bps_ceiling} bps and "
            f"the capital affords {self.affordable_quantity}"
        )


def segment_access_verdict(
    engine: NseTransactionCostEngine,
    template: TradeSpecification,
    *,
    capital_paise: Decimal,
    cost_bps_ceiling: Decimal,
    lot_size: int,
    maximum_lots: int,
    known_as_of: date | None = None,
) -> SegmentAccessVerdict:
    """Put the two halves together: is this segment arithmetically open to this account?"""
    needed = minimum_viable_quantity(
        engine,
        template,
        cost_bps_ceiling=cost_bps_ceiling,
        lot_size=lot_size,
        maximum_lots=maximum_lots,
        known_as_of=known_as_of,
    )
    affordable_lots = capital_bounded_lots(capital_paise, template.entry_price_paise, lot_size)
    return SegmentAccessVerdict(
        segment_value=template.segment.value,
        minimum_viable_quantity=needed,
        affordable_quantity=affordable_lots * lot_size,
        cost_bps_ceiling=cost_bps_ceiling,
    )


def cheapest_quantities(curves: Iterable[QuantityCostCurve]) -> Sequence[tuple[str, Decimal]]:
    """Each segment's best achievable cost, for the dashboard's per-segment comparison."""
    return tuple((curve.segment_value, curve.cheapest.cost_bps) for curve in curves)
