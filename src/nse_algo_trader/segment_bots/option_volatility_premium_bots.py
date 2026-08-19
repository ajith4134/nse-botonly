"""`L5.27` and `L5.28` — the index-option and stock-option segment bots. Decision `A.141`.

Both trade the same claim through the same engine: an option whose implied volatility is unusual
**for its own underlying** is mispriced relative to what that underlying's premium normally is. They
are separate bots rather than one parameterised bot because `L5.29` requires each to own its
relevance model, its risk sub-limits and its own track record judged against its own null — an index
option and a stock option have different liquidity, different event exposure and different lot
economics, and pooling their records would let one subsidise the other's evidence.

**Cadence: once per session, today.** The depth tape has never subscribed an NFO token — measured
2026-08-18, 2,135,786 ticks across 1,845 instruments, every one of them `NSE` cash. So these bots
decide on real daily bhavcopy closes and say so on every signal. `A.142` extends the capture to F&O;
when that tape fills these lift to intraday with **no algorithm change** (`R.04`).

**What the bots add on top of `VarianceRiskPremiumEngine`:**

- selecting one contract per underlying — the nearest-to-the-money strike on the nearest expiry,
  because that is where the at-the-money implied volatility the engine reads actually lives;
- direction: a RICH premium is SOLD and a CHEAP premium is BOUGHT, which is the only thing the
  reading can mean;
- an expected edge in basis points derived from **vega**, so the claim is priced in the units the
  mispricing is actually expressed in rather than converted by a constant;
- a cross-sectional cut across the day's own standardised premia, so the proposal count is a
  property of the day and not of a threshold.

**What they refuse to do.** No naked short option position is proposed on a stock the exchange has
banned (`fo_ban_list`), and no contract at or past its expiry session is priced at all — both are
`None` paths in the analytics engine rather than judgement calls here.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.option_analytics.black_scholes_option_analytics_engine import (
    BlackScholesOptionAnalyticsEngine,
    OptionContractTerms,
    OptionRight,
)
from nse_algo_trader.segment_bots.segment_bot_foundation import (
    BASIS_POINTS_PER_UNIT,
    PAISE_PER_RUPEE,
    BotTrackRecordSummary,
    DecisionCadence,
    SegmentBotFoundation,
    group_instruments_by_underlying,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.strategy.volatility_risk_premium_engine import (
    PremiumRichness,
    VarianceRiskPremiumEngine,
    VarianceRiskPremiumReading,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

DEFAULT_RISK_FREE_RATE = 0.065
"""India's short-term risk-free rate, used only to discount to expiry.

Stated rather than derived because this project ingests no yield curve, and it is the single most
inert input in the whole engine: at a seven-session horizon a 100 basis point error in the rate
moves an at-the-money premium by roughly a rupee on an index at 24,800. Logged in
`docs/BACKLOG.md` as `B34` so it is replaced by a real curve rather than forgotten.
"""

VOLATILITY_POINTS_PER_UNIT = 100.0
"""`vollib` quotes vega per one PERCENTAGE POINT of volatility while volatilities here are decimals.

Measured against a numerical derivative, not read off a README — see the analytics engine's
docstring.
"""


class OptionVolatilityPremiumBot(SegmentBotFoundation):
    """Shared body of the two option bots: read the premium, sell rich, buy cheap.

    A plain class rather than a `@dataclass` for the same measured reason as the cash bot —
    decorating a subclass of a slotted dataclass breaks the zero-argument `super()` closure.
    """

    _analytics: BlackScholesOptionAnalyticsEngine
    _premium: VarianceRiskPremiumEngine
    _instants_observed: set[datetime]
    _last_readings: dict[str, VarianceRiskPremiumReading]
    _last_cut: float | None

    def __init__(
        self,
        *,
        trading_segment: TradingSegment,
        window_size: int,
        track_record: BotTrackRecordSummary | None = None,
        analytics: BlackScholesOptionAnalyticsEngine | None = None,
        premium_engine: VarianceRiskPremiumEngine | None = None,
    ) -> None:
        SegmentBotFoundation.__init__(
            self,
            trading_segment_value=trading_segment,
            # `A.141`: gated by what data exists, never by what the algorithm can do.
            cadence=DecisionCadence.ONCE_PER_SESSION,
            window_size=window_size,
            track_record=track_record or BotTrackRecordSummary.cold_start(),
        )
        self._analytics = analytics or BlackScholesOptionAnalyticsEngine(
            risk_free_rate=DEFAULT_RISK_FREE_RATE
        )
        self._premium = premium_engine or VarianceRiskPremiumEngine()
        self._instants_observed = set()
        self._last_readings = {}
        self._last_cut = None

    # ---- observation ------------------------------------------------------------------

    def observe(self, context: SegmentBotContext) -> None:
        """Advance realised volatility on the underlyings, and premium history on the chain.

        Idempotent by instant: a replayed session must not feed the same return into the EWMA twice.
        """
        if context.decision_instant in self._instants_observed:
            return
        self._instants_observed.add(context.decision_instant)
        SegmentBotFoundation.observe(self, context)

        underlying_prices = self._underlying_prices(context)
        for symbol, price_paise in underlying_prices.items():
            self._premium.observe_underlying(symbol, float(price_paise / PAISE_PER_RUPEE))

        sessions_per_year = self._sessions_per_year(context)
        for symbol, contract in self._chosen_contracts(context).items():
            reading_inputs = self._implied_volatility_for(contract, context)
            realised = self._premium.realised_volatility_for(
                symbol, sessions_per_year=sessions_per_year
            )
            if reading_inputs is None or realised is None:
                continue
            self._premium.observe_premium(symbol, reading_inputs - realised)

    # ---- decision ---------------------------------------------------------------------

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        """One proposal per underlying whose premium is unusual enough to clear the day's cut."""
        sessions_per_year = self._sessions_per_year(context)
        contracts = self._chosen_contracts(context)

        # Pass one: read every underlying at a cut of zero, purely to learn today's cross-section.
        # The classification is redone at the real cut below; reading twice is cheap and reading
        # once at a chosen cut is the magic constant this avoids.
        standardised: dict[int, float] = {}
        readings: dict[str, VarianceRiskPremiumReading] = {}
        for symbol, contract in contracts.items():
            implied = self._implied_volatility_for(contract, context)
            reading = self._premium.read(
                symbol,
                implied_volatility=implied,
                sessions_per_year=sessions_per_year,
                richness_cut=0.0,
            )
            readings[symbol] = reading
            if reading.standardised_premium is not None and math.isfinite(
                reading.standardised_premium
            ):
                standardised[contract.instrument_token] = reading.standardised_premium
        self._last_readings = readings

        cut = self.cross_sectional_cut(standardised)
        self._last_cut = cut
        if cut is None:
            return ()

        signals: list[PricedSignal] = []
        for symbol, contract in contracts.items():
            selected = readings.get(symbol)
            if selected is None or selected.standardised_premium is None:
                continue
            if abs(selected.standardised_premium) < cut:
                continue
            signal = self._signal_for(symbol, contract, context, selected)
            if signal is not None:
                signals.append(signal)
        return tuple(signals)

    def _signal_for(
        self,
        underlying_symbol: str,
        contract: TradeableInstrument,
        context: SegmentBotContext,
        reading: VarianceRiskPremiumReading,
    ) -> PricedSignal | None:
        if self._is_barred(underlying_symbol, context):
            return None
        state = self.state_for(contract.instrument_token)
        if state is None or state.last_price_paise is None or state.last_price_paise <= 0:
            return None
        terms = self._terms_for(contract, context)
        if terms is None or reading.implied_volatility is None:
            return None
        greeks = self._analytics.greeks_of(
            terms,
            volatility=reading.implied_volatility,
            decision_instant=context.decision_instant,
        )
        if greeks is None or greeks.vega_per_volatility_point <= 0.0:
            return None

        # The mispricing, in the units it is expressed in: volatility POINTS away from this
        # underlying's own normal premium. Multiplied by vega it becomes rupees of expected move
        # per contract — no constant converts one into the other (`R.03`).
        premium_deviation_points = (
            abs(reading.standardised_premium or 0.0)
            * self._premium_spread_for(underlying_symbol)
            * VOLATILITY_POINTS_PER_UNIT
        )
        expected_move_rupees = greeks.vega_per_volatility_point * premium_deviation_points
        premium_rupees = float(state.last_price_paise / PAISE_PER_RUPEE)
        if premium_rupees <= 0.0 or expected_move_rupees <= 0.0:
            return None
        expected_edge_bps = Decimal(
            str(round(expected_move_rupees / premium_rupees * float(BASIS_POINTS_PER_UNIT), 4))
        )

        return self.build_signal(
            instrument=contract,
            context=context,
            # Rich premium is sold, cheap premium is bought. The reading has exactly one meaning.
            side=TradeLeg.SELL if reading.richness is PremiumRichness.RICH else TradeLeg.BUY,
            reference_price_paise=state.last_price_paise,
            expected_edge_bps=expected_edge_bps,
            conviction=self._conviction_from(reading),
            edge_basis=EdgeBasis.CALIBRATED_MODEL,
        )

    def _premium_spread_for(self, underlying_symbol: str) -> float:
        """This underlying's own premium dispersion, recovered from the reading.

        Recovered rather than re-derived: `standardised x spread` reconstructs the raw premium
        deviation exactly, and computing it a second way is how two numbers that must agree stop
        agreeing.
        """
        reading = self._last_readings.get(underlying_symbol)
        if (
            reading is None
            or reading.premium is None
            or reading.standardised_premium in (None, 0.0)
        ):
            return 0.0
        standardised = reading.standardised_premium or 0.0
        if standardised == 0.0:
            return 0.0
        return abs(reading.premium / standardised) if math.isfinite(standardised) else 0.0

    @staticmethod
    def _conviction_from(reading: VarianceRiskPremiumReading) -> Decimal:
        """Bounded by a ratio rather than a clip, so nothing saturates and no width is chosen."""
        magnitude = abs(reading.standardised_premium or 0.0)
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            return Decimal("0.5")
        return Decimal(str(round(magnitude / (1.0 + magnitude), 4)))

    # ---- relevance --------------------------------------------------------------------

    def _strategy_applicability(self, context: SegmentBotContext) -> float:  # noqa: ARG002
        """How much of the chain can actually be classified, and how unusual it currently is.

        The context is unused on purpose and the signature is the protocol's: this bot's
        applicability is a property of its own CARRIED state — how many underlyings it has enough
        premium history to classify, and how unusual the strongest of them currently is. Reading the
        universe again here would score the chain's size rather than the bot's readiness.
        """
        tracked = max(self._premium.underlyings_tracked, 1)
        classifiable = self._premium.underlyings_with_a_premium_distribution() / tracked
        magnitudes = [
            abs(reading.standardised_premium)
            for reading in self._last_readings.values()
            if reading.standardised_premium is not None
            and math.isfinite(reading.standardised_premium)
        ]
        if not magnitudes:
            return max(0.0, min(classifiable, 1.0))
        strongest = max(magnitudes)
        unusualness = strongest / (1.0 + strongest)
        return max(0.0, min(classifiable * unusualness, 1.0))

    # ---- chain handling ---------------------------------------------------------------

    def _chosen_contracts(self, context: SegmentBotContext) -> dict[str, TradeableInstrument]:
        """One contract per underlying: nearest expiry, then nearest strike to spot.

        Nearest expiry because that is where the volume is and where an at-the-money implied
        volatility is a measurement rather than an extrapolation; nearest strike because the
        at-the-money point is the one the engine's realised-volatility comparison is about.
        """
        underlying_prices = self._underlying_prices(context)
        chosen: dict[str, TradeableInstrument] = {}
        for symbol, instruments in group_instruments_by_underlying(
            context.tradeable_universe
        ).items():
            spot = underlying_prices.get(symbol)
            if spot is None:
                continue
            priced = [
                instrument
                for instrument in instruments
                if instrument.strike_paise is not None and instrument.expiry is not None
            ]
            if not priced:
                continue
            nearest_expiry = min(
                instrument.expiry for instrument in priced if instrument.expiry is not None
            )
            front = [instrument for instrument in priced if instrument.expiry == nearest_expiry]
            chosen[symbol] = min(
                front,
                key=lambda instrument: abs((instrument.strike_paise or Decimal(0)) - spot),
            )
        return chosen

    def _terms_for(
        self, contract: TradeableInstrument, context: SegmentBotContext
    ) -> OptionContractTerms | None:
        underlying_prices = self._underlying_prices(context)
        symbol = self._underlying_of(contract)
        spot = underlying_prices.get(symbol)
        if spot is None or contract.strike_paise is None or contract.expiry is None:
            return None
        try:
            return OptionContractTerms(
                underlying_price_paise=spot,
                strike_paise=contract.strike_paise,
                expiry=contract.expiry,
                right=self._right_of(contract),
                lot_size=contract.lot_size,
            )
        except Exception:  # noqa: BLE001 — one malformed chain row is a skip, never a session kill
            return None

    def _implied_volatility_for(
        self, contract: TradeableInstrument, context: SegmentBotContext
    ) -> float | None:
        state = self.state_for(contract.instrument_token)
        terms = self._terms_for(contract, context)
        if state is None or state.last_price_paise is None or terms is None:
            return None
        return self._analytics.implied_volatility_of(
            terms,
            market_premium_paise=state.last_price_paise,
            decision_instant=context.decision_instant,
        )

    @staticmethod
    def _right_of(contract: TradeableInstrument) -> OptionRight:
        """NSE option symbols end in `CE` or `PE`. A naming fact, not a heuristic."""
        return (
            OptionRight.PUT
            if contract.trading_symbol.strip().upper().endswith("PE")
            else OptionRight.CALL
        )

    @staticmethod
    def _underlying_of(contract: TradeableInstrument) -> str:
        symbol = contract.trading_symbol.strip().upper()
        underlying = ""
        for character in symbol:
            if character.isalpha():
                underlying += character
            else:
                break
        return underlying or symbol

    def _underlying_prices(self, context: SegmentBotContext) -> dict[str, Decimal]:
        raw = context.carried_memory.get("underlying_price_paise_by_symbol")
        if not isinstance(raw, dict):
            return {}
        prices: dict[str, Decimal] = {}
        for symbol, value in raw.items():
            try:
                price = Decimal(str(value))
            except (ArithmeticError, ValueError):
                continue
            if price > 0:
                prices[str(symbol).strip().upper()] = price
        return prices

    def _sessions_per_year(self, context: SegmentBotContext) -> int:
        return self._analytics.sessions_per_year(context.decision_instant.year)

    def _is_barred(self, underlying_symbol: str, context: SegmentBotContext) -> bool:
        """The exchange's own F&O ban list. A regulatory fact carried in, never inferred."""
        barred = context.carried_memory.get("fo_ban_list_symbols")
        if not isinstance(barred, (set, frozenset, list, tuple)):
            return False
        return underlying_symbol in {str(symbol).strip().upper() for symbol in barred}

    @property
    def last_cross_sectional_cut(self) -> float | None:
        return self._last_cut

    def last_reading_for(self, underlying_symbol: str) -> VarianceRiskPremiumReading | None:
        """The most recent premium reading — for the dashboard surface and for tests."""
        return self._last_readings.get(underlying_symbol.strip().upper())

    @property
    def underlyings_classifiable(self) -> int:
        return self._premium.underlyings_with_a_premium_distribution()


INDEX_OPTION_WINDOW = 20
STOCK_OPTION_WINDOW = 20
"""The premium window, in sessions. Same length for both because both read the same quantity; kept
as two names so a future refit can move one without silently moving the other."""


class IndexOptionVolatilityPremiumBot(OptionVolatilityPremiumBot):
    """`L5.27`. Index options — NIFTY, BANKNIFTY and the other index underlyings.

    Its own bot rather than a configuration of the stock one because its null is different: index
    options are the most liquid contracts on the exchange, they carry no single-name event risk, and
    their premium is dominated by macro rather than by earnings. A record pooled with single stocks
    would let index liquidity vouch for midcap fills.
    """

    def __init__(self, **keywords: object) -> None:
        super().__init__(
            trading_segment=TradingSegment.INDEX_OPTIONS,
            window_size=INDEX_OPTION_WINDOW,
            **keywords,  # type: ignore[arg-type]
        )

    @property
    def bot_identity(self) -> str:
        return "index_options_volatility_premium_bot"


class StockOptionVolatilityPremiumBot(OptionVolatilityPremiumBot):
    """`L5.28`. Single-stock options.

    Differs from its index sibling in exactly the two ways single names differ: it is subject to the
    F&O ban list, which the shared body already enforces, and its relevance is discounted by how
    much of its chain is classifiable at all — the stock chain is 1,220,678 rows against the index
    chain's 192,789, and most of it never trades.
    """

    def __init__(self, **keywords: object) -> None:
        super().__init__(
            trading_segment=TradingSegment.STOCK_OPTIONS,
            window_size=STOCK_OPTION_WINDOW,
            **keywords,  # type: ignore[arg-type]
        )

    @property
    def bot_identity(self) -> str:
        return "stock_options_volatility_premium_bot"


def nearest_expiry_on_or_after(candidates: list[date], reference: date) -> date | None:
    """The front expiry from a set. Exposed for the spine's universe assembly and for tests."""
    future = sorted(day for day in candidates if day >= reference)
    return future[0] if future else None
