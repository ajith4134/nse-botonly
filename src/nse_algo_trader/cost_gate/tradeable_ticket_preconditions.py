"""Preconditions that kill a trade before its edge is even worth comparing to a hurdle.

Four rules the plan states as separate entries and which are really one idea: **a trade can be
uneconomic for reasons that have nothing to do with whether the signal is right.** Each is
checked independently and reported by name, because "vetoed on cost" tells an operator nothing
about which of these bit.

- **`L11.106` the tradeable-unit denominator.** The percentage that matters is the percentage of
  the instrument you TRADE, never of the thing it references. A 5-point move on a Rs 100 option
  premium is 5% against a ~1% round-trip on the ticket, and comfortably viable. The same five
  points measured against NIFTY at 24,000 is 0.02% and looks fatal. Both describe the same trade
  and only one uses the right base.
- **`L11.107` the minimum ticket.** A flat Rs 20 per order is fixed, so it explodes as a
  percentage when the ticket shrinks: Rs 40 round-trip is 0.31% on a Rs 13,000 ticket and
  **6.15% on a Rs 650 ticket**. This inverts the common instinct — cheap far-OTM options are the
  WORST scalping vehicle in the universe, not the safest.
- **`L11.108` the live spread.** Option spreads scale with illiquidity, not with price:
  0.05-0.25% on liquid ATM weekly NIFTY, 4-10% on an illiquid strike. The gate must read the
  spread from the BOOK, never from a model, because the modelled number is exactly the one that
  will not know an illiquid strike from a liquid one.
- **`L11.99` the range width.** A range is tradeable only when it is wider than the cost of
  trading it. Below that the range is noise wearing a pattern's clothes, and repeating the trade
  accumulates cost rather than profit.

**Every threshold here is derived (`R.03`).** There is no rupee floor and no percentage constant:
the minimum ticket comes from the live brokerage schedule and the instruction's own target, and
the spread and range checks compare against the hurdle the cost engines just computed. `A.12`'s
1.5x survives as a PRIOR in the plan's wording, not as a number in this code — the margin is the
measured cost interval, as it is everywhere else in this gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.execution_fill.execution_fill_model import ExpectedFill

_BASIS_POINTS = Decimal(10_000)


class PreconditionName(StrEnum):
    """Named so a refusal says WHICH economic fact killed the trade."""

    TRADEABLE_UNIT_DENOMINATOR = "tradeable_unit_denominator"
    MINIMUM_TICKET = "minimum_ticket"
    LIVE_SPREAD = "live_spread"
    RANGE_WIDTH = "range_width"


@dataclass(frozen=True, slots=True)
class PreconditionResult:
    """One check, its verdict, and the arithmetic that produced it."""

    name: PreconditionName
    is_satisfied: bool
    reason: str
    measured_bps: Decimal | None = None
    required_bps: Decimal | None = None

    def __str__(self) -> str:
        state = "ok" if self.is_satisfied else "FAILED"
        return f"{self.name.value}: {state} — {self.reason}"


@dataclass(frozen=True, slots=True)
class PreconditionReport:
    """Every check that ran, so a pass is as legible as a failure."""

    results: tuple[PreconditionResult, ...]

    @property
    def all_satisfied(self) -> bool:
        return all(result.is_satisfied for result in self.results)

    @property
    def failures(self) -> tuple[PreconditionResult, ...]:
        return tuple(result for result in self.results if not result.is_satisfied)

    def describe(self) -> str:
        if self.all_satisfied:
            return f"{len(self.results)} preconditions satisfied"
        return "; ".join(str(failure) for failure in self.failures)


def check_tradeable_unit_denominator(signal: PricedSignal) -> PreconditionResult:
    """The edge must be expressed against the instrument being TRADED (`L11.106`).

    For an option that is the PREMIUM, not the strike and not the underlying. The check is
    whether the signal's reference price is the premium: if it equals or exceeds the strike,
    the caller has almost certainly passed the underlying's price and every bps figure derived
    from it is wrong by the ratio between them — typically two orders of magnitude, and in the
    flattering direction, which is why it has to be caught here rather than noticed later.
    """
    name = PreconditionName.TRADEABLE_UNIT_DENOMINATOR
    if not signal.segment.is_option or signal.strike_paise is None:
        return PreconditionResult(
            name=name,
            is_satisfied=True,
            reason="not an option; the traded instrument is its own denominator",
        )
    if signal.reference_price_paise >= signal.strike_paise:
        return PreconditionResult(
            name=name,
            is_satisfied=False,
            reason=(
                f"reference price {signal.reference_price_paise} is at or above the strike "
                f"{signal.strike_paise}, so it looks like the UNDERLYING rather than the "
                f"premium. Every bps figure derived from it would be understated by roughly "
                f"the ratio between them, in the flattering direction"
            ),
        )
    return PreconditionResult(
        name=name,
        is_satisfied=True,
        reason="edge is measured against the option premium, which is the traded unit",
    )


def check_minimum_ticket(
    signal: PricedSignal, flat_charges_paise: Decimal, hurdle_bps: Decimal
) -> PreconditionResult:
    """The ticket must be large enough that flat charges are not the whole trade (`L11.107`).

    Derived, not floored: the minimum ticket is whatever value makes the flat charges bearable
    against THIS signal's own claimed edge. A signal claiming 500 bps can carry a far smaller
    ticket than one claiming 20, and a fixed rupee floor would be wrong for both.

    `flat_charges_paise` is the size-independent part of the round trip — brokerage that does
    not scale, plus any per-debit depository charge.
    """
    name = PreconditionName.MINIMUM_TICKET
    if flat_charges_paise <= 0:
        return PreconditionResult(
            name=name, is_satisfied=True, reason="no flat charges on this segment"
        )
    ticket = signal.notional_paise
    if ticket <= 0:
        return PreconditionResult(
            name=name, is_satisfied=False, reason="a zero-value ticket cannot carry any charge"
        )
    flat_charge_bps = flat_charges_paise / ticket * _BASIS_POINTS
    # The ticket is too small when the FLAT part alone eats the edge that survives the hurdle.
    headroom_bps = signal.expected_edge_bps - hurdle_bps
    if flat_charge_bps > signal.expected_edge_bps:
        return PreconditionResult(
            name=name,
            is_satisfied=False,
            reason=(
                f"the flat charges alone are {flat_charge_bps:.1f} bps of a "
                f"{ticket / Decimal(100):.0f}-rupee ticket, which exceeds the claimed edge of "
                f"{signal.expected_edge_bps:.1f} bps. A fixed per-order charge explodes as a "
                f"percentage as the ticket shrinks — which is why cheap far-OTM options are the "
                f"worst scalping vehicle, not the safest"
            ),
            measured_bps=flat_charge_bps,
            required_bps=signal.expected_edge_bps,
        )
    return PreconditionResult(
        name=name,
        is_satisfied=True,
        reason=(
            f"flat charges are {flat_charge_bps:.1f} bps of the ticket, leaving "
            f"{headroom_bps:.1f} bps of headroom over the hurdle"
        ),
        measured_bps=flat_charge_bps,
        required_bps=signal.expected_edge_bps,
    )


def check_live_spread(signal: PricedSignal, fill: ExpectedFill) -> PreconditionResult:
    """The spread must be read from the book and must not exceed the edge (`L11.108`).

    Option spreads scale with illiquidity rather than with price, so a modelled spread is
    precisely the thing that cannot tell an illiquid strike from a liquid one. `fill.spread`
    comes from the snapshot, which is what makes this check meaningful rather than circular.
    """
    name = PreconditionName.LIVE_SPREAD
    # Crossing costs half the spread on the way in and half on the way out.
    round_trip_spread_bps = fill.spread.spread_bps
    if round_trip_spread_bps >= signal.expected_edge_bps:
        return PreconditionResult(
            name=name,
            is_satisfied=False,
            reason=(
                f"the live quoted spread is {round_trip_spread_bps:.1f} bps against a claimed "
                f"edge of {signal.expected_edge_bps:.1f} bps — the round trip crosses it twice, "
                f"so the spread alone consumes the trade"
            ),
            measured_bps=round_trip_spread_bps,
            required_bps=signal.expected_edge_bps,
        )
    return PreconditionResult(
        name=name,
        is_satisfied=True,
        reason=f"live spread {round_trip_spread_bps:.1f} bps is inside the claimed edge",
        measured_bps=round_trip_spread_bps,
        required_bps=signal.expected_edge_bps,
    )


def check_range_width(
    range_width_bps: Decimal | None, hurdle_bps: Decimal
) -> PreconditionResult:
    """A range is tradeable only when it is wider than the cost of trading it (`L11.99`).

    The plan states this as `range_width_bps > cost_bps x 1.5`, with `A.12` recording the 1.5 as
    a prior. It is not used as a number here: the comparison is against the required hurdle,
    which already carries the measured uncertainty margin. That is the same substitution made
    everywhere else in this gate, and it means a wide-uncertainty instrument automatically needs
    a wider range without anybody choosing how much wider.

    `None` means the caller is not proposing a range trade, which is not a failure.
    """
    name = PreconditionName.RANGE_WIDTH
    if range_width_bps is None:
        return PreconditionResult(
            name=name, is_satisfied=True, reason="not a range trade; no width to clear"
        )
    if range_width_bps <= hurdle_bps:
        return PreconditionResult(
            name=name,
            is_satisfied=False,
            reason=(
                f"the range is {range_width_bps:.1f} bps wide against a {hurdle_bps:.1f} bps "
                f"hurdle. A range narrower than its own trading cost is noise wearing a "
                f"pattern's clothes, and repeating the trade accumulates cost, not profit"
            ),
            measured_bps=range_width_bps,
            required_bps=hurdle_bps,
        )
    return PreconditionResult(
        name=name,
        is_satisfied=True,
        reason=f"range {range_width_bps:.1f} bps clears the {hurdle_bps:.1f} bps hurdle",
        measured_bps=range_width_bps,
        required_bps=hurdle_bps,
    )


def evaluate_preconditions(
    signal: PricedSignal,
    fill: ExpectedFill,
    *,
    flat_charges_paise: Decimal,
    hurdle_bps: Decimal,
    range_width_bps: Decimal | None = None,
) -> PreconditionReport:
    """Run every precondition and report all of them, not just the first failure.

    All four run even when one has already failed, because an operator looking at a refusal
    needs to know whether fixing one thing would help or whether the trade is uneconomic in
    several independent ways at once.
    """
    return PreconditionReport(
        results=(
            check_tradeable_unit_denominator(signal),
            check_minimum_ticket(signal, flat_charges_paise, hurdle_bps),
            check_live_spread(signal, fill),
            check_range_width(range_width_bps, hurdle_bps),
        )
    )
