"""A signal that states what it expects to earn — the input the cost gate has been missing.

Every gate above `L1` needs one number the strategy layer has never produced: **expected edge in
basis points**. The only strategy module in the tree emits an action, a conviction in [0,1] and a
unitless deviation, and its own docstring says so. Conviction is not edge — a confident signal
about a two-basis-point move is not a trade, and a hesitant signal about a two-hundred-basis-point
move usually is. A gate fed conviction instead of edge cannot tell those apart.

**The edge is a CLAIM, not a measurement, and this module is built to keep it labelled as one.**
Every `PricedSignal` carries the basis on which its edge was estimated, and `EdgeBasis` names the
weakest link. Nothing here validates the claim: that is `L2`'s gatekeeper, whose entire purpose is
to establish how much a strategy's own estimates can be believed. What this module guarantees is
that the claim is explicit, attributable and falsifiable — an edge nobody can trace is an edge
nobody can disprove.

**Why the adapter is separate from the contract.** `PricedSignal` demands an edge and does not
care where it came from. `edge_from_calibrated_reversion` is ONE way to produce it, from the one
strategy that exists, and every other strategy will bring its own adapter with its own basis.

**Corrected 2026-08-12.** That adapter was previously `edge_from_mean_reversion_decision`, which
derived the edge from the decision alone: expected move = the deviation's excess beyond its own
entry band. The claim it encoded was never a market hypothesis — it was an unstated exit rule, and
`R.03` forbids a policy constant living inside a valuation formula. It is replaced by a coefficient
measured on 38,456 real reversion events. The measurement moved the claim by roughly seven-fold at
a five-bar horizon, which changed the verdict rather than the decimal place, so the original is
recorded here rather than quietly dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    ReversionCapture,
    deviation_bucket_of,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

_BASIS_POINTS = Decimal(10_000)


class PricedSignalError(Exception):
    """The signal cannot be priced, and guessing the missing part would fabricate an edge."""


class EdgeConfidence(StrEnum):
    """Which end of a calibrated estimate the signal claims.

    Separated from `EdgeBasis` because they answer different questions. `EdgeBasis` says how the
    estimate was ARRIVED AT; this says how much of the estimate's own uncertainty the claim
    absorbs. A calibrated model claimed at its optimistic end and a calibrated model claimed at
    its lower bound share a basis and are not the same claim.
    """

    EXPECTED = "expected"
    CONSERVATIVE = "conservative"


class EdgeBasis(StrEnum):
    """How the expected edge was arrived at — the weakest link in the claim.

    Ordered from most to least trustworthy. A gate may demand a stronger basis for larger size,
    and `L2`'s gatekeeper will eventually attach measured reliability to each.
    """

    MEASURED_TRACK_RECORD = "measured_track_record"
    CALIBRATED_MODEL = "calibrated_model"
    STRATEGY_HYPOTHESIS = "strategy_hypothesis"
    OPERATOR_ASSERTION = "operator_assertion"


@dataclass(frozen=True, slots=True)
class PricedSignal:
    """An intent to trade, carrying what it expects to earn and why that is believed."""

    instrument_token: int
    trading_symbol: str
    segment: ChargeableSegment
    side: TradeLeg
    decided_at: datetime
    reference_price_paise: Decimal
    expected_edge_bps: Decimal
    proposed_quantity: int
    edge_basis: EdgeBasis
    source: str
    conviction: Decimal | None = None
    strike_paise: Decimal | None = None
    horizon_minutes: int | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise PricedSignalError(
                "a signal with no source cannot be held to a track record, and an edge nobody "
                "can attribute is an edge nobody can disprove"
            )
        if self.reference_price_paise <= 0:
            raise PricedSignalError(
                f"reference price must be positive, got {self.reference_price_paise}"
            )
        if self.proposed_quantity <= 0:
            raise PricedSignalError(
                f"proposed quantity must be positive, got {self.proposed_quantity}"
            )
        if self.expected_edge_bps <= 0:
            raise PricedSignalError(
                f"expected edge must be positive, got {self.expected_edge_bps} — a signal that "
                f"expects to lose is not a signal, and a signal that expects nothing cannot "
                f"clear any hurdle, so both are refused here rather than vetoed later as though "
                f"cost were the reason"
            )
        if self.conviction is not None and not 0 <= self.conviction <= 1:
            raise PricedSignalError(f"conviction must be in [0, 1], got {self.conviction}")
        if self.segment.is_option and self.strike_paise is None:
            raise PricedSignalError(
                f"{self.segment} needs a strike: the SEBI turnover fee is charged on notional, "
                f"so the cost of this signal cannot be computed without it"
            )

    @property
    def expected_edge_paise(self) -> Decimal:
        """The per-unit move the signal expects, in paise."""
        return self.reference_price_paise * self.expected_edge_bps / _BASIS_POINTS

    @property
    def expected_gross_profit_paise(self) -> Decimal:
        """Before any cost. The number that has to survive the hurdle."""
        return self.expected_edge_paise * Decimal(self.proposed_quantity)

    @property
    def notional_paise(self) -> Decimal:
        return self.reference_price_paise * Decimal(self.proposed_quantity)

    def resized_to(self, quantity: int) -> PricedSignal:
        """The same claim at a different size. Edge in bps is size-independent; cost is not."""
        if quantity <= 0:
            raise PricedSignalError(f"resized quantity must be positive, got {quantity}")
        return PricedSignal(
            instrument_token=self.instrument_token,
            trading_symbol=self.trading_symbol,
            segment=self.segment,
            side=self.side,
            decided_at=self.decided_at,
            reference_price_paise=self.reference_price_paise,
            expected_edge_bps=self.expected_edge_bps,
            proposed_quantity=quantity,
            edge_basis=self.edge_basis,
            source=self.source,
            conviction=self.conviction,
            strike_paise=self.strike_paise,
            horizon_minutes=self.horizon_minutes,
        )


def edge_from_calibrated_reversion(
    capture: ReversionCapture,
    *,
    deviation: float,
    conviction: float,
    price_uncertainty: EdgeConfidence = EdgeConfidence.EXPECTED,
) -> Decimal:
    """Turn the one existing strategy's output into an edge claim, from measured reversion.

    **What replaced what, and why (corrected 2026-08-12).** This function previously computed the
    expected move as `(|deviation| - band) * band_width`: the excess beyond the entry trigger. That
    was never a measurement. It is an **exit rule** — "the position unwinds to the band edge and
    stops" — and nothing established it. Exiting at the mean instead makes the identical signal
    claim 5.9x more, and the formula offered no way to choose. Under `R.03` a policy constant
    hiding inside a valuation formula is exactly the defect to remove, so it was removed.

    The edge is now the **measured** expected reversion for a deviation of this depth, from
    `mean_reversion_edge_calibrator`, which walked 38,456 real events across 600 symbols of the
    deep-history archive and recorded what actually came back. The old formula understated it by
    roughly seven-fold at a five-bar horizon (5.6 bps against a measured 40.4), which is not a
    rounding difference — it is the difference between a strategy that fails the intraday floor and
    one that clears it by four times.

    **Conviction still scales, and still does not gate.** The engine refuses below its own
    threshold, so conviction arriving here is a magnitude. A half-convinced signal claiming the
    full calibrated move would be overstating precisely where it is least sure.

    **`price_uncertainty` chooses which end of the estimate to claim.** `EXPECTED` uses the fitted
    mean. `CONSERVATIVE` uses the lower confidence bound, so the claim survives the calibration
    being two standard errors optimistic — which is what a gate should demand before committing
    real capital, and why the choice is explicit at the call site rather than buried here.

    Raises:
        PricedSignalError: the calibration does not cover this deviation's depth, or the edge it
            implies is not positive. A non-positive edge is refused rather than returned so it
            surfaces as "this strategy has no measurable edge here" rather than being vetoed
            downstream as though transaction cost were the reason.
    """
    if not 0 <= conviction <= 1:
        raise PricedSignalError(f"conviction must be in [0, 1], got {conviction}")
    expected_bucket = deviation_bucket_of(Decimal(str(deviation)))
    if capture.deviation_bucket != expected_bucket:
        raise PricedSignalError(
            f"calibration is for {capture.deviation_bucket} sigma deviations but this signal is "
            f"at {expected_bucket}; reversion is measurably non-linear in depth, so a calibration "
            f"from another bucket is not a substitute"
        )
    basis = (
        capture.mean_captured_bps
        if price_uncertainty is EdgeConfidence.EXPECTED
        else capture.lower_confidence_bps
    )
    edge = basis * Decimal(str(conviction))
    if edge <= 0:
        raise PricedSignalError(
            f"the measured reversion for {expected_bucket} sigma deviations over "
            f"{capture.horizon_bars} bars implies an edge of {edge} bps "
            f"({capture.describe()}); there is no edge to price, and this is a finding about the "
            f"strategy rather than a cost verdict"
        )
    return edge
