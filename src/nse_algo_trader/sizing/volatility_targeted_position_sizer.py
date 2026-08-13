"""How many shares — a volatility budget, a Kelly cap, and a whole number of lots (`L1.10`, `L7.01`,
`L1.09`).

Specification: `docs/research/227_position_sizing_and_risk_gate_spec.md` §3.

This is the first module in the rebuild that answers **how much**, and the first real caller of
`capital_configuration` — which has held the operator's rupees, and refused to guess them, with
nothing on the other end since the day it was written (todo `0.8`).

**Three numbers, combined by minimum, then made whole.**

1. **The volatility budget** sizes the position so its expected rupee movement over the decision's
   horizon hits a target. `notional = risk_budget / sigma`, where `sigma` is the instrument's own
   realised volatility as a fraction of price. A quiet instrument therefore gets a larger position
   than a violent one for the same risk, which is the entire point of targeting risk rather than
   notional.
2. **The Kelly cap** asks the different question — is the measured edge worth that much risk — and
   caps rather than replaces, because Kelly's error points the wrong way.
3. **The lot** makes it an order. Always rounded DOWN.

**`target_risk_fraction` is not a constant** (`R.03`). It is `1 / concurrent_position_capacity`:
the number of positions the configured segments can carry at once under `R.10`'s equal-by-default
rule. Six segments that may each hold one position means each may risk a sixth of the book. Change
the segment set and the fraction moves with it; nothing here is edited, and no number in this module
is a preference.

**Rounding is always down, and the boundary case is the interesting one.** A rounded-up lot is a
position larger than the risk budget justified, and "just one more lot" is how a discretisation
quietly becomes a leverage decision. When rounding down yields zero lots — a ₹1 lakh book against a
1,000-unit contract at ₹2,500 — the answer is zero **with a stated reason**. That state is real and
common, and it must be legible on the surface rather than appear as a silent no-trade.

**A hardcoded lot size is a defect, not a fallback.** The archived system carried a lot-size map
that went stale — NIFTY is 65, not 75 — and every position it sized in that contract was wrong by
15%. The lot size arrives from the live daily instrument dump or the sizer refuses; it never
assumes 1, because assuming 1 sizes a derivative like a share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import ReversionCapture
from nse_algo_trader.sizing.kelly_edge_scaler import (
    KellyEdgeScaler,
    KellyScalingError,
    ScaledKellyFraction,
)
from nse_algo_trader.sizing.realised_volatility_estimator import (
    BASIS_POINTS_IN_ONE,
    RealisedVolatility,
    RealisedVolatilityEstimator,
    VolatilityEstimationError,
)


class PositionSizingError(Exception):
    """A size cannot be computed, and any substituted number would become a real order."""


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Everything the sizer needs, gathered by the caller so this module opens nothing."""

    trading_symbol: str
    deployable_rupees: Decimal
    reference_price_rupees: Decimal
    lot_size: int
    concurrent_position_capacity: int
    horizon_bars: int
    calibration: ReversionCapture
    recent_closes: Sequence[tuple[datetime, Decimal]]


@dataclass(frozen=True, slots=True)
class SizedPosition:
    """The quantity, and every intermediate figure that produced it.

    The intermediates are not diagnostics. A size an operator cannot reconstruct is a size they
    cannot argue with, and `L13.29` requires the reasoning to be recoverable afterwards rather than
    reconstructed from memory.
    """

    trading_symbol: str
    quantity: int
    lots: int
    lot_size: int
    reference_price_rupees: Decimal
    notional_rupees: Decimal
    """What the ORDER actually costs — `quantity x reference_price`, not the budget.

    These were the same field once, and the gate recovered a price from `notional / quantity` to
    check its collar. Because the budget includes the part-lot that was rounded away, that quotient
    was the true price inflated by up to a whole lot: on a lot of 22 it recovered Rs 1,942.50 for a
    Rs 1,000 instrument, refusing a limit price AT the market and allowing one at double it
    (`A.105`). The budget is kept as `risk_justified_notional_rupees`.
    """

    risk_justified_notional_rupees: Decimal
    deployable_rupees: Decimal
    target_risk_fraction: Decimal
    risk_budget_rupees: Decimal
    volatility_target_notional_rupees: Decimal
    kelly_notional_rupees: Decimal
    volatility: RealisedVolatility
    kelly: ScaledKellyFraction
    binding_bound: str
    refusal_reason: str | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.quantity > 0

    def describe(self) -> str:
        if self.refusal_reason is not None:
            return f"{self.trading_symbol}: no position — {self.refusal_reason}"
        return (
            f"{self.trading_symbol}: {self.lots} lot(s) of {self.lot_size} = {self.quantity} "
            f"units, "
            f"Rs {self.notional_rupees.quantize(Decimal('0.01'))} notional. "
            f"Volatility budget Rs "
            f"{self.volatility_target_notional_rupees.quantize(Decimal('0.01'))} "
            f"({self.target_risk_fraction.quantize(Decimal('0.0001'))} of Rs "
            f"{self.deployable_rupees} against {self.volatility.describe()}); "
            f"Kelly cap Rs {self.kelly_notional_rupees.quantize(Decimal('0.01'))} "
            f"({self.kelly.describe()}). Bound by {self.binding_bound}."
        )


@dataclass(frozen=True, slots=True)
class VolatilityTargetedPositionSizer:
    """Risk budget, Kelly cap, whole lots. The first real caller of `capital_configuration`."""

    volatility_estimator: RealisedVolatilityEstimator = field(
        default_factory=RealisedVolatilityEstimator
    )
    kelly_scaler: KellyEdgeScaler = field(default_factory=KellyEdgeScaler)

    def size(self, inputs: SizingInputs) -> SizedPosition:
        """How many units to trade, or zero with the reason.

        Raises:
            PositionSizingError: the inputs cannot produce a size at all — an impossible lot size or
                capacity, a non-positive price, a history too thin to measure, or a zero-volatility
                instrument (an unbounded position). These are refusals rather than zero-sizes,
                because a caller that passed them has a defect and must hear about it.
        """
        self._validate(inputs)
        volatility = self._measure_volatility(inputs)
        risk_fraction = Decimal(1) / Decimal(inputs.concurrent_position_capacity)
        risk_budget = inputs.deployable_rupees * risk_fraction

        # sigma arrives in bps of price; the budget is rupees of expected movement, so the notional
        # that moves by `risk_budget` is `risk_budget / sigma_fraction`.
        sigma_fraction = volatility.sigma_bps / BASIS_POINTS_IN_ONE
        volatility_notional = risk_budget / sigma_fraction

        kelly = self._scale_edge(inputs, volatility)
        kelly_notional = inputs.deployable_rupees * kelly.kelly_fraction

        notional = min(volatility_notional, kelly_notional)
        binding = "kelly_cap" if kelly_notional < volatility_notional else "volatility_target"
        # There is deliberately no third `deployable_capital` bound. `kelly_notional` is
        # `deployable x kelly_fraction` with the fraction capped at 1, so the `min` above already
        # bounds the answer by the book. The branch that used to sit here was unreachable, and its
        # comment claimed otherwise — dead code that asserted a falsehood (`A.105` finding 8).

        lot_notional = Decimal(inputs.lot_size) * inputs.reference_price_rupees
        # Floor division, not `int(a / b)`: true division rounds to the Decimal context's 28
        # significant digits FIRST, which rounded 3.9999999999999999999999999996 up to 4 and put the
        # order over the capital that sized it (`A.105` finding 9). `//` truncates exactly.
        lots = int(notional // lot_notional)
        quantity = lots * inputs.lot_size
        order_notional = Decimal(quantity) * inputs.reference_price_rupees

        refusal = self._refusal_for(
            inputs=inputs, kelly=kelly, lots=lots, notional=notional, lot_notional=lot_notional
        )
        return SizedPosition(
            trading_symbol=inputs.trading_symbol,
            quantity=quantity if refusal is None else 0,
            lots=lots if refusal is None else 0,
            lot_size=inputs.lot_size,
            reference_price_rupees=inputs.reference_price_rupees,
            notional_rupees=order_notional if refusal is None else Decimal(0),
            risk_justified_notional_rupees=notional,
            deployable_rupees=inputs.deployable_rupees,
            target_risk_fraction=risk_fraction,
            risk_budget_rupees=risk_budget,
            volatility_target_notional_rupees=volatility_notional,
            kelly_notional_rupees=kelly_notional,
            volatility=volatility,
            kelly=kelly,
            binding_bound=binding,
            refusal_reason=refusal,
        )

    def _measure_volatility(self, inputs: SizingInputs) -> RealisedVolatility:
        try:
            volatility = self.volatility_estimator.estimate(
                inputs.recent_closes, horizon_bars=inputs.horizon_bars
            )
        except VolatilityEstimationError as failure:
            raise PositionSizingError(
                f"{inputs.trading_symbol} cannot be sized: {failure}"
            ) from failure
        if volatility.is_degenerate:
            raise PositionSizingError(
                f"{inputs.trading_symbol} has not moved at all over the measured window, so a "
                "volatility-targeted size is unbounded. An instrument that does not move is not an "
                "infinitely attractive one; it is one this sizer will not size"
            )
        return volatility

    def _scale_edge(
        self, inputs: SizingInputs, volatility: RealisedVolatility
    ) -> ScaledKellyFraction:
        """Kelly against the instrument's OWN measured dispersion, over the same horizon.

        The dispersion is the realised volatility this sizer just measured, not anything read off
        the calibration — see `kelly_edge_scaler`'s docstring for what reading it off the
        calibration did to every position size.
        """
        try:
            return self.kelly_scaler.scale(
                inputs.calibration,
                return_dispersion=volatility.sigma_bps / BASIS_POINTS_IN_ONE,
            )
        except KellyScalingError as failure:
            raise PositionSizingError(
                f"{inputs.trading_symbol} cannot be sized from its calibration: {failure}"
            ) from failure

    @staticmethod
    def _refusal_for(
        *,
        inputs: SizingInputs,
        kelly: ScaledKellyFraction,
        lots: int,
        notional: Decimal,
        lot_notional: Decimal,
    ) -> str | None:
        """The single place a zero size gets its reason, so the two can never disagree."""
        if kelly.refusal_reason is not None:
            return kelly.refusal_reason
        if inputs.deployable_rupees <= 0:
            return (
                f"there is no deployable capital (Rs {inputs.deployable_rupees}); a book with "
                "nothing in it trades nothing, in paper as in live"
            )
        if lots < 1:
            return (
                f"the smallest tradeable unit costs Rs {lot_notional} "
                f"({inputs.lot_size} x Rs {inputs.reference_price_rupees}) and the risk budget "
                f"supports Rs {notional.quantize(Decimal('0.01'))}. Rounding down gives no lots, "
                "and a part lot is not an order"
            )
        return None

    @staticmethod
    def _validate(inputs: SizingInputs) -> None:
        for name, value in (
            ("deployable capital", inputs.deployable_rupees),
            ("reference price", inputs.reference_price_rupees),
        ):
            if not isinstance(value, Decimal):
                raise PositionSizingError(
                    f"{name} must be a Decimal; a float in the money path puts binary rounding "
                    "between the operator's capital and an order"
                )
        if inputs.reference_price_rupees <= 0:
            raise PositionSizingError(
                f"a reference price of {inputs.reference_price_rupees} cannot price a lot; the "
                "quantity would be unbounded"
            )
        if inputs.lot_size <= 0:
            raise PositionSizingError(
                f"lot size {inputs.lot_size} is impossible. It is READ from the live instrument "
                "dump and never assumed: the archived system's hardcoded map went stale (NIFTY is "
                "65, not 75), and defaulting to 1 sizes a derivative like a share"
            )
        if inputs.concurrent_position_capacity <= 0:
            raise PositionSizingError(
                f"concurrent position capacity {inputs.concurrent_position_capacity} is "
                "impossible; it divides the book into per-position risk budgets and a zero "
                "capacity means no position may be taken, which is a decision for the gate rather "
                "than a division by zero here"
            )
