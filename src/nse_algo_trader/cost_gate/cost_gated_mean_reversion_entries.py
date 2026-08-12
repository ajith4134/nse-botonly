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

from nse_algo_trader.cost_gate.pre_trade_cost_gate import (
    GateDecision,
    GateVerdict,
    PreTradeCostGate,
)
from nse_algo_trader.cost_gate.priced_signal import (
    EdgeBasis,
    PricedSignal,
    PricedSignalError,
    edge_from_mean_reversion_decision,
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
) -> PricedSignal:
    """Turn a scale-free decision into a claim about money.

    `dispersion_paise` is the engine's own `rolling_dispersion()`, fed in the same units as the
    closes it observed. Passing it explicitly rather than reaching into the engine keeps this
    honest about the one thing that must not be got wrong: if the engine was fed rupees and
    this is handed paise, every edge is off by a hundred and every verdict with it.

    Raises:
        MeanReversionEntryError: the decision is not actionable, or claims no capturable
            deviation. Both are ordinary outcomes, not failures.
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
        edge_bps = edge_from_mean_reversion_decision(
            deviation=decision.deviation,
            deviation_band=decision.deviation_band,
            conviction=decision.conviction,
            reference_price_paise=reference_price_paise,
            band_width_paise=dispersion_paise,
        )
    except PricedSignalError as error:
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
        # Stamped as a hypothesis, not a fact. Nothing has established that this family pays
        # the distance it travels; `L2` exists to find out, and until it has, the gate treats
        # the claim as the weakest admissible kind.
        edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
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
    trade_date: date | None = None,
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
        )
    except (MeanReversionEntryError, PricedSignalError) as error:
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
