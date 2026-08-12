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
care where it came from. `edge_from_mean_reversion_decision` is ONE way to produce it, from the
one strategy that exists, and it is deliberately simple and deliberately marked
`STRATEGY_HYPOTHESIS`: a mean-reversion engine that says price is N band-widths from the mean is
claiming the reversion is worth roughly that distance. That claim may be worth nothing. It is
written down so it can be measured rather than assumed, and every other strategy will bring its
own adapter with its own basis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

_BASIS_POINTS = Decimal(10_000)


class PricedSignalError(Exception):
    """The signal cannot be priced, and guessing the missing part would fabricate an edge."""


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


def edge_from_mean_reversion_decision(
    *,
    deviation: float,
    deviation_band: float | None,
    conviction: float,
    reference_price_paise: Decimal,
    band_width_paise: Decimal,
) -> Decimal:
    """Turn the one existing strategy's output into an edge claim, in basis points.

    **The claim:** a mean-reversion engine that reports price sitting `deviation` band-widths
    away from its mean is asserting that reversion is worth approximately that distance. The
    expected move is therefore the excess beyond the band — the part that has to unwind for the
    signal to be right — scaled by the engine's own conviction.

        edge_bps = (|deviation| - band) * band_width / reference_price * 10,000 * conviction

    **Why conviction scales rather than gates.** The engine already refuses to emit below its own
    threshold, so conviction arriving here is a magnitude, and a half-convinced signal claiming a
    full move would be overstating exactly where it is least sure.

    **This is a hypothesis and is labelled one.** Nothing has established that mean reversion on
    this universe pays the distance it travels, or pays at all. The value of writing it down is
    that it becomes measurable: `L2`'s gatekeeper compares claimed edge against realised outcome,
    and a strategy whose claims do not survive that comparison is refused promotion. An edge that
    stays in someone's head cannot be refuted.

    Raises:
        PricedSignalError: the deviation does not exceed its own band, so the engine is not
            claiming any reversion to capture.
    """
    if band_width_paise <= 0:
        raise PricedSignalError(f"band width must be positive, got {band_width_paise}")
    if reference_price_paise <= 0:
        raise PricedSignalError(f"reference price must be positive, got {reference_price_paise}")
    band = Decimal(str(deviation_band)) if deviation_band is not None else Decimal(0)
    excess = abs(Decimal(str(deviation))) - band
    if excess <= 0:
        raise PricedSignalError(
            f"deviation {deviation} does not exceed its band {deviation_band}; the engine is "
            f"claiming no reversion to capture, so there is no edge to price"
        )
    move_paise = excess * band_width_paise
    return move_paise / reference_price_paise * _BASIS_POINTS * Decimal(str(conviction))
