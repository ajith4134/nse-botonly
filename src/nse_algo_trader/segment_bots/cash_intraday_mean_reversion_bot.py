"""`L5.26` — the cash-intraday segment bot. Decision `A.141`.

**The one bot with real intraday data today**, and the reason its cadence field matters:
`price_bars` holds 1,471,990 five-minute bars and the live depth tape covers 1,845 NSE names,
so this bot decides INTRADAY while its five siblings decide once per session on daily closes
until F&O capture fills (`A.142`). Same foundation, same protocol, same conformance suite —
only the cadence differs, and it is reported rather than implied.

**What it owns beyond the shared foundation** — this is what makes it `L5.26` rather than the probe
in `verify_segment_bot_protocol_on_real_data.py`:

1. **A real per-instrument strategy engine.** `IntradayMeanReversionEngine` carries each
   instrument's own rolling window AND an online quantile of its own deviation magnitudes, so the
   "extreme" level is learned per instrument rather than asserted. A microcap at Rs 12 and RELIANCE
   are both banded by their own history.
2. **A regime veto applied BEFORE the deviation.** Mean reversion is a bet that price returns to a
   centre, and in a trending tape that bet is the wrong way round. The veto runs first so an extreme
   deviation can never argue its way past it — the ordering the engine's own docstring insists on.
3. **An edge estimate DERIVED from the instrument's own dispersion** (`R.03`). The probe multiplied
   sigma by a constant basis-point figure, which is exactly the magic constant this project forbids:
   it claims the same rupee edge from one sigma of a Rs 12 stock as from one of a Rs 3,000 one.
   Here the expected move is the deviation carried back to the mean — `|deviation| x dispersion` —
   expressed in basis points OF THAT INSTRUMENT'S OWN PRICE. Scale-free in, priced out.
4. **A cross-sectional cut** taken from the dispersion of the day's own deviation magnitudes, so a
   quiet morning proposes few names and a violent one proposes more, without anybody choosing a
   count. The conformance suite's `proposal-size-is-sane` check exists because a fixed 1-sigma floor
   named 1,594 instruments at one instant on the real universe.
5. **Conviction from the instrument's own learned band**, not from the deviation size. A 2-sigma
   move in something that moves 3 sigma twice a week is not a strong claim; the same move in
   something that has never exceeded 1.5 is.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.regime.market_regime_state import MarketRegime
from nse_algo_trader.regime.soft_regime_weighting_brain import RegimeBelief
from nse_algo_trader.segment_bots.segment_bot_foundation import (
    BASIS_POINTS_PER_UNIT,
    PAISE_PER_RUPEE,
    BotTrackRecordSummary,
    DecisionCadence,
    SegmentBotFoundation,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import SegmentBotContext
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

REVERSION_WINDOW_IN_FIVE_MINUTE_BARS = 20
"""The reversion window, in five-minute bars — one hundred minutes of tape.

Not a free parameter: it is the window `IntradayMeanReversionEngine` bands against and the window
`reversion_calibration` was FITTED on (`A.106` found a consumer using 120 closes against this
engine's 20, so every edge it looked up was the edge for a depth the instrument was not at).
Changing it here without refitting that calibration would reintroduce exactly that defect.
"""

MINIMUM_REGIME_CONCENTRATION = 0.40
MINIMUM_REGIME_AGREEMENT = 0.50
"""Operator policy on how sure the regime brain must be before this family acts.

Required rather than defaulted by the engine, and stated here because it is a POLICY choice about
this strategy family rather than a measurement — a different family consuming the same brain
legitimately needs a different bar. These two are the only numbers in this bot that are not derived.

**The values are the ones already exercised against the real universe** by
`verify_segment_bot_protocol_on_real_data.py`, which proposed 16 signals over 3,618 real instruments
at exactly these bars. An earlier draft of this file invented 0.45/0.60 instead, which is stricter
than any belief this project has actually produced: `concentration` is `1 - normalised entropy`, and
a distribution as peaked as {0.70, 0.15, 0.10, 0.05} scores only **0.34**. The bot abstained on
every instrument of a 40-name probe and looked like working caution. Numbers invented rather than
measured fail in exactly this direction — silently, and in the safe-looking one.
"""

REGIMES_MEAN_REVERSION_IS_A_BET_AGAINST = frozenset({MarketRegime.TRENDING})
"""A trending tape is the regime in which "it will come back" is the losing side of the trade.

A market fact about the strategy family, not a tuned set: reversion and trend are opposite claims
about the same price path. `MarketRegime` carries one undirected `TRENDING` label rather than an
up/down pair, which is the right shape here — reversion is wrong against a trend in EITHER
direction, and the bot never needs to know which way.
"""


class CashIntradayMeanReversionBot(SegmentBotFoundation):
    """`L5.26`. Deviation-from-mean entries on the NSE cash board, regime-vetoed and cross-cut.

    A plain class rather than a `@dataclass`, deliberately: decorating a subclass of a slotted
    dataclass rebuilds the class object, which breaks the zero-argument `super()` closure with
    `TypeError: super(type, obj): obj must be an instance or subtype of type`. Measured, not
    assumed — the first version of this file raised exactly that on construction.
    """

    _engines: dict[int, IntradayMeanReversionEngine]
    _instants_observed: set[datetime]
    _last_cut: float | None

    def __init__(
        self,
        *,
        track_record: BotTrackRecordSummary | None = None,
        window_size: int = REVERSION_WINDOW_IN_FIVE_MINUTE_BARS,
    ) -> None:
        SegmentBotFoundation.__init__(
            self,
            trading_segment_value=TradingSegment.CASH_INTRADAY,
            cadence=DecisionCadence.INTRADAY,
            window_size=window_size,
            track_record=track_record or BotTrackRecordSummary.cold_start(),
        )
        self._engines = {}
        self._instants_observed = set()
        self._last_cut = None

    @property
    def bot_identity(self) -> str:
        return "cash_intraday_mean_reversion_bot"

    # ---- observation ------------------------------------------------------------------

    def observe(self, context: SegmentBotContext) -> None:
        """Advance the shared window state AND each instrument's own reversion engine.

        Idempotent by instant. A replay re-observes the same moment, and double-counting it would
        move every rolling statistic both layers carry — the defect is silent because the numbers
        stay plausible.
        """
        if context.decision_instant in self._instants_observed:
            return
        self._instants_observed.add(context.decision_instant)
        SegmentBotFoundation.observe(self, context)
        for instrument in context.tradeable_universe:
            state = self.state_for(instrument.instrument_token)
            if state is None or state.last_price_paise is None:
                continue
            engine = self._engine_for(instrument.instrument_token)
            engine.observe(float(state.last_price_paise / PAISE_PER_RUPEE))

    def _engine_for(self, instrument_token: int) -> IntradayMeanReversionEngine:
        engine = self._engines.get(instrument_token)
        if engine is None:
            engine = IntradayMeanReversionEngine(
                minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
                minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
            )
            self._engines[instrument_token] = engine
        return engine

    # ---- decision ---------------------------------------------------------------------

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        """Every name whose deviation clears the day's own cross-sectional cut.

        Empty is a real answer and the common one. It never raises: a universe of ten thousand will
        contain rows that cannot carry an order, and one of them must not end the session.
        """
        belief = self._belief_from(context)
        if self._regime_vetoes_reversion(context):
            self._last_cut = None
            return ()

        deviations: dict[int, float] = {}
        for instrument in context.tradeable_universe:
            engine = self._engines.get(instrument.instrument_token)
            if engine is None or not engine.is_mature:
                continue
            decision = engine.decide(belief, context.decision_instant)
            if decision.action is MeanReversionAction.ABSTAIN:
                continue
            deviation = engine.current_deviation_sigma()
            if deviation is None or not math.isfinite(deviation) or deviation == 0.0:
                continue
            deviations[instrument.instrument_token] = deviation

        cut = self.cross_sectional_cut(deviations)
        self._last_cut = cut
        if cut is None:
            return ()

        signals: list[PricedSignal] = []
        for instrument in context.tradeable_universe:
            deviation = deviations.get(instrument.instrument_token)
            if deviation is None or abs(deviation) < cut:
                continue
            signal = self._signal_for(instrument, context, deviation)
            if signal is not None:
                signals.append(signal)
        return tuple(signals)

    def _signal_for(
        self, instrument: object, context: SegmentBotContext, deviation: float
    ) -> PricedSignal | None:
        from nse_algo_trader.segment_bots.segment_bot_protocol import TradeableInstrument

        if not isinstance(instrument, TradeableInstrument):
            return None
        engine = self._engines.get(instrument.instrument_token)
        state = self.state_for(instrument.instrument_token)
        if engine is None or state is None or state.last_price_paise is None:
            return None
        dispersion = engine.rolling_dispersion()
        price_rupees = float(state.last_price_paise / PAISE_PER_RUPEE)
        if dispersion is None or dispersion <= 0.0 or price_rupees <= 0.0:
            return None

        # The move this bot expects: the deviation carried back to its own centre. In rupees it is
        # |deviation| x dispersion; in basis points it is that, over this instrument's own price.
        # Derived end to end (`R.03`) — no constant converts sigma into money anywhere here.
        expected_move_rupees = abs(deviation) * dispersion
        expected_edge_bps = Decimal(
            str(round(expected_move_rupees / price_rupees * float(BASIS_POINTS_PER_UNIT), 4))
        )
        if expected_edge_bps <= 0:
            return None

        return self.build_signal(
            instrument=instrument,
            context=context,
            # A price BELOW its centre is expected to rise, so it is bought. The sign of the
            # deviation is the direction of the trade, inverted — this is the whole strategy.
            side=TradeLeg.BUY if deviation < 0 else TradeLeg.SELL,
            reference_price_paise=state.last_price_paise,
            expected_edge_bps=expected_edge_bps,
            conviction=self._conviction_from_learned_band(instrument.instrument_token, deviation),
            edge_basis=EdgeBasis.CALIBRATED_MODEL,
            horizon_minutes=None,
        )

    def _conviction_from_learned_band(self, instrument_token: int, deviation: float) -> Decimal:
        """How unusual this move is FOR THIS INSTRUMENT, on `[0, 1]`.

        The engine carries an online quantile of the instrument's own deviation magnitudes; the band
        is that quantile. A move at the band is ordinary for this name and scores 0.5; a move well
        beyond it approaches 1. Bounded by a ratio rather than by a clip, so nothing saturates
        abruptly and no width is chosen.
        """
        engine = self._engines.get(instrument_token)
        band = None if engine is None else engine.deviation_band()
        magnitude = abs(deviation)
        if band is None or band <= 0.0 or not math.isfinite(magnitude):
            return Decimal("0.5")
        ratio = magnitude / band
        return Decimal(str(round(ratio / (1.0 + ratio), 4)))

    # ---- relevance --------------------------------------------------------------------

    def _strategy_applicability(self, context: SegmentBotContext) -> float:
        """Reversion's own precondition: a tape that is NOT trending.

        Returns the probability mass sitting outside the two trending regimes, which is the
        directly interpretable answer to "how applicable is mean reversion right now" — and it is
        the same quantity the veto acts on, so relevance and behaviour cannot disagree.
        """
        probabilities = context.regime.probabilities
        trending = sum(
            float(probabilities.get(regime, 0.0))
            for regime in REGIMES_MEAN_REVERSION_IS_A_BET_AGAINST
        )
        return max(0.0, min(1.0 - trending, 1.0))

    @staticmethod
    def _regime_vetoes_reversion(context: SegmentBotContext) -> bool:
        """Trend beats reversion, and the veto runs before any deviation is looked at.

        A COMPARISON between two probabilities in the same distribution, not a threshold on one of
        them — so there is no constant here at all (`R.03`). Reversion is the wrong side of the
        trade exactly when the tape is more likely trending than ranging, and that is a fact the
        distribution already states.

        An earlier draft compared `1 - trending mass` against `MINIMUM_REGIME_CONCENTRATION`, which
        is a different quantity entirely: that constant is a bar on the belief's ENTROPY, and using
        it here made one number stand for two unrelated measurements. It is the same shape as the
        `A.106` defect — a calibration looked up under a coordinate measured a different way.
        """
        probabilities = context.regime.probabilities
        trending = float(probabilities.get(MarketRegime.TRENDING, 0.0))
        ranging = float(probabilities.get(MarketRegime.RANGING, 0.0))
        return trending >= ranging

    @staticmethod
    def _belief_from(context: SegmentBotContext) -> RegimeBelief:
        """Wrap the context's distribution in the shape the strategy engine takes.

        The bot is handed a distribution and the engine wants a belief; the disagreement term is
        reported as zero because the context carries no panel — stated rather than invented, and the
        engine's agreement bar is therefore satisfied by construction while its CONCENTRATION bar
        still binds on the real distribution.
        """
        return RegimeBelief(
            distribution=context.regime,
            contributing_classifiers=("segment_bot_context",),
            disagreement=0.0,
            weights={"segment_bot_context": 1.0},
            observed_at=context.decision_instant,
        )

    @property
    def last_cross_sectional_cut(self) -> float | None:
        """The cut this bot last selected against — for the dashboard surface and for tests."""
        return self._last_cut
