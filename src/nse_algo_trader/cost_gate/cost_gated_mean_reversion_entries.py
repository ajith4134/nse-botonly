"""Where a signal meets its cost — the wiring that makes the gate stop being an orphan.

`R.06` says nothing exists without a consumer. The gate's consumer is this: the one strategy
in the tree, its decision turned into a priced claim and put through the hurdle. Until this
module existed the gate could veto nothing, and the strategy could propose nothing that cost
had an opinion about.

**The conversion, and its honest weakness.** The engine reports a deviation in units of its own
rolling dispersion — scale-free on purpose, so one engine serves a Rs 20 microcap and RELIANCE
without a per-symbol parameter. That is exactly what makes it unpriceable as it stands: a
z-score has no rupees in it. Multiplying back by the dispersion the engine already computes
turns the signal into a claim about money, and that claim is what the gate can then refuse.

**What this module does NOT claim.** That mean reversion pays the distance it travels. That
claim is stamped `STRATEGY_HYPOTHESIS` on every signal it produces, and `L2`'s gatekeeper exists
to find out whether it survives contact with realised outcomes. The value of routing it through
here is that the hypothesis becomes measurable — a claimed edge, a modelled cost, and a verdict,
all recorded per decision.

**Order of operations matters.** The regime veto runs first, inside the engine, then the cost
gate. A signal the brain refuses never reaches costing, so an extreme deviation in a trending
tape cannot talk its way past the regime veto by being cheap to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationError,
    ReversionCalibrationStore,
)
from nse_algo_trader.cost_gate.pre_trade_cost_gate import (
    GateDecision,
    GateVerdict,
    PreTradeCostGate,
)
from nse_algo_trader.cost_gate.priced_signal import (
    EdgeBasis,
    EdgeConfidence,
    PricedSignal,
    PricedSignalError,
    edge_from_calibrated_reversion,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
    MeanReversionDecision,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

STRATEGY_SOURCE = "intraday_mean_reversion_engine"


class MeanReversionEntryError(Exception):
    """The decision cannot be turned into a priced signal."""


@dataclass(frozen=True, slots=True)
class CostGatedEntry:
    """One strategy decision, priced, and what the gate said about it."""

    decision: MeanReversionDecision
    signal: PricedSignal | None
    gate_decision: GateDecision | None
    skipped_reason: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.gate_decision is not None and self.gate_decision.is_tradeable

    @property
    def verdict(self) -> GateVerdict | None:
        return None if self.gate_decision is None else self.gate_decision.verdict

    def describe(self) -> str:
        if self.gate_decision is None:
            return f"no trade: {self.skipped_reason}"
        return self.gate_decision.describe()


_LARGEST_CREDIBLE_PRICE_RATIO = Decimal(10)
"""How far the engine's last close may sit from the book's mid before the two are not the same
number in the same units.

A price moves; between the engine's last observation and this snapshot it may have moved a lot.
It does not move by a factor of TEN intraday — but a rupees-versus-paise mix-up is a factor of a
hundred, so any bound between the two separates the cases cleanly. Ten is the loose end of that
range on purpose: this check exists to catch a unit error, not to second-guess a volatile
instrument."""


def _units_agree(engine_close: Decimal, book_mid: Decimal) -> bool:
    """Whether the engine and the book are quoting the same instrument in the same units."""
    if engine_close <= 0 or book_mid <= 0:
        return False
    ratio = engine_close / book_mid
    return Decimal(1) / _LARGEST_CREDIBLE_PRICE_RATIO <= ratio <= _LARGEST_CREDIBLE_PRICE_RATIO


_SIDE_BY_ACTION: dict[MeanReversionAction, TradeLeg] = {
    MeanReversionAction.ENTER_LONG: TradeLeg.BUY,
    MeanReversionAction.ENTER_SHORT: TradeLeg.SELL,
}
"""`ABSTAIN` is deliberately absent rather than mapped to a default side.

The engine treats abstention as a first-class decision, and a lookup that quietly turned it
into a buy would be the single worst bug this module could carry."""


def price_mean_reversion_decision(
    decision: MeanReversionDecision,
    *,
    instrument_token: int,
    trading_symbol: str,
    segment: ChargeableSegment,
    reference_price_paise: Decimal,
    dispersion_paise: Decimal,
    quantity: int,
    decided_at: datetime,
    calibrations: ReversionCalibrationStore,
    horizon_bars: int,
    edge_confidence: EdgeConfidence = EdgeConfidence.EXPECTED,
) -> PricedSignal:
    """Turn a scale-free decision into a claim about money, from measured reversion.

    `dispersion_paise` is the engine's own `rolling_dispersion()`, fed in the same units as the
    closes it observed. Passing it explicitly rather than reaching into the engine keeps this
    honest about the one thing that must not be got wrong: if the engine was fed rupees and
    this is handed paise, every edge is off by a hundred and every verdict with it.

    `calibrations` is REQUIRED and has no default. That is the point of the 2026-08-12 correction:
    the edge used to be derivable from the decision alone, because the formula silently assumed an
    exit rule. It is not derivable from the decision alone — it depends on what deviations of this
    depth have historically recovered — so the dependency is now in the signature where it cannot
    be forgotten. An instrument with no calibration produces no signal, rather than a signal
    carrying an invented number.

    `horizon_bars` must match the holding period the position will actually be given. A five-bar
    calibration applied to a position closed at the end of the day is measuring a different trade.

    Raises:
        MeanReversionEntryError: the decision is not actionable, no calibration covers the
            deviation's depth, or the measured reversion implies no positive edge. All three are
            ordinary outcomes, not failures — and the third is a finding worth reading.
    """
    if not decision.is_actionable:
        raise MeanReversionEntryError(
            f"decision is {decision.action.value}, which proposes no trade: {decision.reason}"
        )
    side = _SIDE_BY_ACTION.get(decision.action)
    if side is None:
        raise MeanReversionEntryError(f"{decision.action} does not map to a trade side")
    if dispersion_paise <= 0:
        raise MeanReversionEntryError(
            "the engine reports zero dispersion, so a deviation expressed in units of it "
            "cannot be converted into a price move"
        )
    try:
        capture = calibrations.capture_for(
            deviation_sigma=Decimal(str(decision.deviation)),
            horizon_bars=horizon_bars,
            as_of=decided_at.date(),
            trading_symbol=trading_symbol,
        )
        edge_bps = edge_from_calibrated_reversion(
            capture,
            deviation=decision.deviation,
            conviction=decision.conviction,
            price_uncertainty=edge_confidence,
        )
    except (PricedSignalError, CalibrationError) as error:
        raise MeanReversionEntryError(str(error)) from error
    return PricedSignal(
        instrument_token=instrument_token,
        trading_symbol=trading_symbol,
        segment=segment,
        side=side,
        decided_at=decided_at,
        reference_price_paise=reference_price_paise,
        expected_edge_bps=edge_bps,
        proposed_quantity=quantity,
        # Promoted from STRATEGY_HYPOTHESIS on 2026-08-12: the edge is no longer the strategy's
        # assertion about itself, it is a coefficient fitted to what deviations of this depth
        # historically recovered. That is a calibrated model and is stamped as one. It is NOT a
        # MEASURED_TRACK_RECORD — nothing here has traded — and the distance between those two
        # rungs is the whole of `L2`'s remaining job.
        edge_basis=EdgeBasis.CALIBRATED_MODEL,
        source=STRATEGY_SOURCE,
        conviction=Decimal(str(decision.conviction)),
    )


def evaluate_mean_reversion_entry(
    engine: IntradayMeanReversionEngine,
    gate: PreTradeCostGate,
    *,
    belief: object,
    snapshot: BookSnapshot,
    instrument_token: int,
    trading_symbol: str,
    segment: ChargeableSegment,
    quantity: int,
    observed_at: datetime,
    calibrations: ReversionCalibrationStore,
    horizon_bars: int,
    trade_date: date | None = None,
    edge_confidence: EdgeConfidence = EdgeConfidence.EXPECTED,
) -> CostGatedEntry:
    """Ask the strategy, price what it says, and let the gate decide. The full path.

    Returns a `CostGatedEntry` in every case rather than raising: a decision not to trade is
    the most common outcome of a trading system and is not an error condition. The reason is
    always carried, so a quiet day is distinguishable from a broken one.
    """
    decision = engine.decide(belief, observed_at)  # type: ignore[arg-type]
    if not decision.is_actionable:
        return CostGatedEntry(
            decision=decision,
            signal=None,
            gate_decision=None,
            skipped_reason=f"strategy abstained: {decision.reason}",
        )

    mid = snapshot.mid_paise
    if mid is None or mid <= 0:
        return CostGatedEntry(
            decision=decision,
            signal=None,
            gate_decision=None,
            skipped_reason="no usable mid on the book, so the signal cannot be priced",
        )
    dispersion = engine.rolling_dispersion()
    if dispersion is None:
        return CostGatedEntry(
            decision=decision,
            signal=None,
            gate_decision=None,
            skipped_reason="the engine is still immature; its dispersion is not yet defined",
        )
    latest_close = engine.latest_close()
    if latest_close is None or not _units_agree(Decimal(str(latest_close)), Decimal(mid)):
        return CostGatedEntry(
            decision=decision,
            signal=None,
            gate_decision=None,
            skipped_reason=(
                f"the engine's last close ({latest_close}) and the book's mid ({mid}) are not "
                f"the same instrument in the same units — feeding the engine rupees and pairing "
                f"it with a paise book yields an edge wrong by a hundredfold, silently, and "
                f"flips the verdict"
            ),
        )

    try:
        signal = price_mean_reversion_decision(
            decision,
            instrument_token=instrument_token,
            trading_symbol=trading_symbol,
            segment=segment,
            reference_price_paise=Decimal(mid),
            dispersion_paise=Decimal(str(dispersion)),
            quantity=quantity,
            decided_at=observed_at,
            calibrations=calibrations,
            horizon_bars=horizon_bars,
            edge_confidence=edge_confidence,
        )
    except (MeanReversionEntryError, PricedSignalError, CalibrationError) as error:
        return CostGatedEntry(
            decision=decision,
            signal=None,
            gate_decision=None,
            skipped_reason=str(error),
        )

    return CostGatedEntry(
        decision=decision,
        signal=signal,
        gate_decision=gate.evaluate(signal, snapshot, trade_date=trade_date),
    )
