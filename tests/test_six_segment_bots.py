"""Tests for the six segment bots and the three engines behind them — spec `docs/research/263`.

The tests that matter most here are the ones written from defects that actually occurred rather than
from the happy path, because every one of them passed a reading of the code:

- `source` must equal `bot_identity` EXACTLY — it was `"{identity}:{cadence}"` and the conformance
  suite rejected all five non-cash bots at once;
- the regime veto must be a COMPARISON, not a threshold — a constant meant as a bar on the belief's
  entropy was reused as a bar on trending mass, two unrelated quantities;
- an invented `0.45` concentration bar sits above what real beliefs reach, so the bot abstained on
  everything and looked like working caution;
- registration must not contaminate the bots that then trade;
- MCX must return an EMPTY universe rather than an approximation.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.option_analytics.black_scholes_option_analytics_engine import (
    BlackScholesOptionAnalyticsEngine,
    Moneyness,
    OptionAnalyticsError,
    OptionContractTerms,
    OptionRight,
)
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    CashIntradayMeanReversionBot,
)
from nse_algo_trader.segment_bots.futures_basis_carry_bots import (
    CommodityMcxBasisCarryBot,
    IndexFutureBasisCarryBot,
    StockFutureBasisCarryBot,
)
from nse_algo_trader.segment_bots.option_volatility_premium_bots import (
    IndexOptionVolatilityPremiumBot,
    StockOptionVolatilityPremiumBot,
)
from nse_algo_trader.segment_bots.segment_bot_foundation import (
    DecisionCadence,
    SegmentBotFoundation,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotMaturityRung,
    SegmentBot,
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_bot_registry import (
    build_all_segment_bots,
    conformance_violations_by_identity,
    registration_contexts,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.strategy.futures_basis_carry_engine import (
    BasisDislocation,
    FuturesBasisCarryEngine,
    OpenInterestBuildup,
    implied_carry_rate,
)
from nse_algo_trader.strategy.volatility_risk_premium_engine import (
    PremiumRichness,
    VarianceRiskPremiumEngine,
)

INSTANT = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
EXPIRY = date(2026, 9, 24)
SESSIONS_PER_YEAR = 246
"""NSE's measured session count for 2026 — the annualisation denominator, not the folklore 252."""

RANGING = RegimeDistribution.from_scores(
    {
        MarketRegime.RANGING: 0.90,
        MarketRegime.TRENDING: 0.03,
        MarketRegime.VOLATILE: 0.04,
        MarketRegime.QUIET: 0.03,
    }
)
TRENDING = RegimeDistribution.from_scores(
    {
        MarketRegime.RANGING: 0.05,
        MarketRegime.TRENDING: 0.85,
        MarketRegime.VOLATILE: 0.07,
        MarketRegime.QUIET: 0.03,
    }
)


def cash_instruments(count: int = 40) -> tuple[TradeableInstrument, ...]:
    return tuple(
        TradeableInstrument(700_000 + index, f"CASH{index:03d}", 1, 5) for index in range(count)
    )


def option_instruments() -> tuple[TradeableInstrument, ...]:
    return tuple(
        TradeableInstrument(
            900_000 + index,
            f"NIFTY26SEP{24000 + index * 100}CE",
            75,
            5,
            strike_paise=Decimal(str((24000 + index * 100) * 100)),
            expiry=EXPIRY,
        )
        for index in range(6)
    )


def future_instruments() -> tuple[TradeableInstrument, ...]:
    return (TradeableInstrument(920_001, "NIFTY26SEPFUT", 75, 5, expiry=EXPIRY),)


def context_for(
    instruments: tuple[TradeableInstrument, ...],
    memory: dict[str, object],
    *,
    step: int = 0,
    regime: RegimeDistribution = RANGING,
) -> SegmentBotContext:
    return SegmentBotContext(
        decision_instant=INSTANT + timedelta(minutes=5 * step),
        tradeable_universe=instruments,
        regime=regime,
        carried_memory=memory,
    )


ALL_BOT_FACTORIES = (
    CashIntradayMeanReversionBot,
    IndexOptionVolatilityPremiumBot,
    StockOptionVolatilityPremiumBot,
    IndexFutureBasisCarryBot,
    StockFutureBasisCarryBot,
    CommodityMcxBasisCarryBot,
)


# --------------------------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("factory", ALL_BOT_FACTORIES)
def test_every_bot_satisfies_the_protocol_and_starts_at_the_bottom(factory: type) -> None:
    bot = factory()
    assert isinstance(bot, SegmentBot)
    assert isinstance(bot, SegmentBotFoundation)
    assert bot.maturity().rung is not BotMaturityRung.GRADUATED
    assert bot.maturity().is_armable_without_operator is False


@pytest.mark.unit
def test_all_six_pass_the_shared_conformance_suite() -> None:
    """The `L5.29` gate, run exactly as the registry runs it.

    Six bots, one suite. This is the test that caught `source` being decorated with the cadence —
    it failed on five bots at once, and the cash bot alone would have shipped it.
    """
    violations = conformance_violations_by_identity(at=INSTANT)
    assert len(violations) == len(ALL_BOT_FACTORIES)
    for identity, failures in violations.items():
        assert not failures, f"{identity}: {[item.describe() for item in failures]}"


@pytest.mark.unit
def test_a_signals_source_is_exactly_the_bot_identity() -> None:
    """The track record is filed under this string; a decorated one files one bot under two names.

    Written from the defect: `source` was `"{identity}:{cadence}"`, which reads as harmless extra
    provenance and silently splits a bot's history the day the decoration changes.
    """
    bot = CashIntradayMeanReversionBot()
    instruments = cash_instruments()
    signals: tuple[PricedSignal, ...] = ()
    for step in range(200):
        prices = {
            instrument.instrument_token: Decimal(
                str(100_000 + index * 5_000 + (step * 37 + index * 11) % 900)
            )
            for index, instrument in enumerate(instruments)
        }
        context = context_for(instruments, {"last_price_paise_by_token": prices}, step=step)
        bot.observe(context)
        signals = bot.propose(context)
        if signals:
            break
    assert signals, "the cash bot never proposed over 200 synthetic five-minute bars"
    for signal in signals:
        assert signal.source == bot.bot_identity


@pytest.mark.unit
@pytest.mark.parametrize("factory", ALL_BOT_FACTORIES)
def test_an_empty_universe_is_answered_with_nothing_rather_than_a_raise(factory: type) -> None:
    """A bot that cannot say "nothing today" takes the session down on a quiet morning."""
    bot = factory()
    context = context_for((), {})
    bot.observe(context)
    assert bot.propose(context) == ()
    assert 0.0 <= bot.relevance(context).applicability <= 1.0


# --------------------------------------------------------------------------------------------
# the regime veto — a comparison, not a threshold
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_reversion_veto_is_a_comparison_between_two_probabilities() -> None:
    """Reversion is wrong exactly when trend outweighs range, which needs no constant at all.

    Written from the defect: `MINIMUM_REGIME_CONCENTRATION` — a bar on the belief's ENTROPY — was
    reused as a bar on `1 - trending mass`, an unrelated quantity. One constant standing for two
    different measurements is the `A.106` shape.
    """
    bot = CashIntradayMeanReversionBot()
    assert bot._regime_vetoes_reversion(context_for((), {}, regime=TRENDING)) is True
    assert bot._regime_vetoes_reversion(context_for((), {}, regime=RANGING)) is False


@pytest.mark.unit
def test_a_trending_tape_produces_no_reversion_proposals_however_extreme_the_move() -> None:
    """The veto runs BEFORE the deviation, so an extreme move cannot argue its way past it."""
    bot = CashIntradayMeanReversionBot()
    instruments = cash_instruments(8)
    for step in range(120):
        shock = 40_000 if step == 119 else (step * 53) % 700
        prices = {
            instrument.instrument_token: Decimal(str(100_000 + index * 5_000 + shock))
            for index, instrument in enumerate(instruments)
        }
        context = context_for(
            instruments, {"last_price_paise_by_token": prices}, step=step, regime=TRENDING
        )
        bot.observe(context)
        assert bot.propose(context) == ()


@pytest.mark.unit
def test_the_concentration_bar_is_reachable_by_a_real_belief() -> None:
    """An invented bar that no real belief clears refuses everything and looks like caution.

    `concentration` is `1 - normalised entropy`. A belief as peaked as {0.70, 0.15, 0.10, 0.05}
    scores about 0.34, so the 0.45 an earlier draft invented was unreachable. This pins the property
    rather than the number: whatever the bar is, a strongly-held belief must clear it.
    """
    from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
        MINIMUM_REGIME_AGREEMENT,
        MINIMUM_REGIME_CONCENTRATION,
    )

    bot = CashIntradayMeanReversionBot()
    belief = bot._belief_from(context_for((), {}, regime=RANGING))
    assert belief.is_actionable(MINIMUM_REGIME_CONCENTRATION, MINIMUM_REGIME_AGREEMENT)


# --------------------------------------------------------------------------------------------
# option analytics
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_year_fraction_counts_trading_sessions_not_calendar_days() -> None:
    """`R.03`: the annualisation denominator is the exchange's own session count, never 252."""
    engine = BlackScholesOptionAnalyticsEngine(risk_free_rate=0.065)
    sessions = engine.sessions_per_year(2026)
    assert 200 < sessions < 260
    assert sessions != 252, "252 is folklore; this must come from the real calendar"
    to_expiry = engine.trading_sessions_to_expiry(INSTANT, EXPIRY)
    assert to_expiry > 0
    assert engine.year_fraction_to_expiry(INSTANT, EXPIRY) == pytest.approx(
        to_expiry / sessions, rel=1e-12
    )


@pytest.mark.unit
def test_a_premium_round_trips_through_the_implied_volatility_solver() -> None:
    engine = BlackScholesOptionAnalyticsEngine(risk_free_rate=0.065)
    terms = OptionContractTerms(
        underlying_price_paise=Decimal("2480000"),
        strike_paise=Decimal("2480000"),
        expiry=EXPIRY,
        right=OptionRight.CALL,
        lot_size=75,
    )
    price = engine.price_rupees(terms, volatility=0.12, decision_instant=INSTANT)
    solved = engine.implied_volatility_of(
        terms,
        market_premium_paise=Decimal(str(round(price * 100, 2))),
        decision_instant=INSTANT,
    )
    assert solved is not None
    assert solved == pytest.approx(0.12, abs=1e-6)
    assert terms.moneyness() is Moneyness.AT_THE_MONEY


@pytest.mark.unit
def test_an_unpriceable_quote_is_none_rather_than_a_number() -> None:
    """Three real daily cases, each an ABSENCE of information rather than a cheap option."""
    engine = BlackScholesOptionAnalyticsEngine(risk_free_rate=0.065)
    in_the_money = OptionContractTerms(
        underlying_price_paise=Decimal("2480000"),
        strike_paise=Decimal("2400000"),
        expiry=EXPIRY,
        right=OptionRight.CALL,
        lot_size=75,
    )
    # below intrinsic — a stale or crossed quote
    assert (
        engine.implied_volatility_of(
            in_the_money, market_premium_paise=Decimal("100"), decision_instant=INSTANT
        )
        is None
    )
    # at expiry — no time value left to invert
    assert (
        engine.implied_volatility_of(
            in_the_money,
            market_premium_paise=Decimal("900000"),
            decision_instant=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        )
        is None
    )
    # zero premium
    assert (
        engine.implied_volatility_of(
            in_the_money, market_premium_paise=Decimal("0"), decision_instant=INSTANT
        )
        is None
    )


@pytest.mark.unit
def test_zero_volatility_is_refused_rather_than_collapsing_every_greek_to_zero() -> None:
    engine = BlackScholesOptionAnalyticsEngine(risk_free_rate=0.065)
    terms = OptionContractTerms(
        underlying_price_paise=Decimal("2480000"),
        strike_paise=Decimal("2480000"),
        expiry=EXPIRY,
        right=OptionRight.CALL,
        lot_size=75,
    )
    with pytest.raises(OptionAnalyticsError):
        engine.greeks_of(terms, volatility=0.0, decision_instant=INSTANT)


# --------------------------------------------------------------------------------------------
# variance risk premium
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_premium_with_no_history_is_unmeasurable_rather_than_fair() -> None:
    """"No context for this number" and "this number is ordinary" are different claims."""
    engine = VarianceRiskPremiumEngine()
    for price in (100.0, 101.0, 99.5, 100.5):
        engine.observe_underlying("NIFTY", price)
    reading = engine.read(
        "NIFTY", implied_volatility=0.20, sessions_per_year=SESSIONS_PER_YEAR, richness_cut=1.0
    )
    assert reading.richness is PremiumRichness.UNMEASURABLE
    assert reading.premium is not None, "the premium itself is measured even when unclassifiable"


@pytest.mark.unit
def test_richness_is_relative_to_that_underlyings_own_premium_history() -> None:
    """The whole engine: a habitually rich name is not perpetually RICH."""
    engine = VarianceRiskPremiumEngine()
    for step in range(30):
        engine.observe_underlying("NIFTY", 100.0 + (step % 3) * 0.4)
    for _ in range(20):
        engine.observe_premium("NIFTY", 0.05)
    for _ in range(10):
        engine.observe_premium("NIFTY", 0.06)
    realised = engine.realised_volatility_for("NIFTY", sessions_per_year=SESSIONS_PER_YEAR)
    assert realised is not None
    ordinary = engine.read(
        "NIFTY",
        implied_volatility=realised + 0.055,
        sessions_per_year=SESSIONS_PER_YEAR,
        richness_cut=1.5,
    )
    unusual = engine.read(
        "NIFTY",
        implied_volatility=realised + 0.30,
        sessions_per_year=SESSIONS_PER_YEAR,
        richness_cut=1.5,
    )
    assert ordinary.richness is PremiumRichness.FAIR
    assert unusual.richness is PremiumRichness.RICH


@pytest.mark.unit
def test_the_half_life_becomes_a_decay_rather_than_a_typed_constant() -> None:
    engine = VarianceRiskPremiumEngine(half_life_sessions=10.0)
    assert engine.decay == pytest.approx(0.5 ** (1 / 10), rel=1e-12)
    with pytest.raises(ValueError, match="not a memory"):
        VarianceRiskPremiumEngine(half_life_sessions=0.0)


# --------------------------------------------------------------------------------------------
# futures basis and carry
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_same_basis_at_two_horizons_is_two_different_carry_rates() -> None:
    """Annualising IS the engine — comparing raw bases across expiries compares two quantities."""
    near = implied_carry_rate(
        future_price=24_820.0, spot_price=24_800.0, sessions_to_expiry=8,
        sessions_per_year=SESSIONS_PER_YEAR,
    )
    far = implied_carry_rate(
        future_price=24_820.0, spot_price=24_800.0, sessions_to_expiry=40,
        sessions_per_year=SESSIONS_PER_YEAR,
    )
    assert near is not None and far is not None
    assert near > far * 4.9, "an eight-session basis annualises to five times a forty-session one"


@pytest.mark.unit
def test_carry_is_none_at_expiry_rather_than_a_very_large_number() -> None:
    assert (
        implied_carry_rate(
            future_price=24_820.0, spot_price=24_800.0, sessions_to_expiry=0,
            sessions_per_year=SESSIONS_PER_YEAR,
        )
        is None
    )


@pytest.mark.unit
def test_the_open_interest_taxonomy_reads_price_and_interest_together() -> None:
    engine = FuturesBasisCarryEngine()
    engine.observe(
        "NIFTY26SEPFUT", future_price=24_800.0, spot_price=24_780.0, sessions_to_expiry=20,
        sessions_per_year=SESSIONS_PER_YEAR, open_interest=100_000,
    )
    rising = engine.read(
        "NIFTY26SEPFUT", future_price=24_900.0, spot_price=24_780.0, sessions_to_expiry=20,
        sessions_per_year=SESSIONS_PER_YEAR, dislocation_cut=1.0, open_interest=110_000,
    )
    assert rising.buildup is OpenInterestBuildup.LONG_BUILDUP
    falling = engine.read(
        "NIFTY26SEPFUT", future_price=24_700.0, spot_price=24_780.0, sessions_to_expiry=20,
        sessions_per_year=SESSIONS_PER_YEAR, dislocation_cut=1.0, open_interest=110_000,
    )
    assert falling.buildup is OpenInterestBuildup.SHORT_BUILDUP


@pytest.mark.unit
def test_a_contract_with_no_carry_history_is_unmeasurable() -> None:
    engine = FuturesBasisCarryEngine()
    reading = engine.read(
        "NEW26SEPFUT", future_price=100.0, spot_price=99.0, sessions_to_expiry=20,
        sessions_per_year=SESSIONS_PER_YEAR, dislocation_cut=1.0,
    )
    assert reading.dislocation is BasisDislocation.UNMEASURABLE
    assert reading.basis == pytest.approx(1.0)


# --------------------------------------------------------------------------------------------
# cadence, data blockers and registration hygiene
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_only_cash_decides_intraday_and_the_rest_say_so() -> None:
    """`A.141`: cadence is a property of what data EXISTS, and it is reported, never implied."""
    bots = build_all_segment_bots()
    by_segment = {bot.trading_segment: bot for bot in bots}
    assert by_segment[TradingSegment.CASH_INTRADAY].cadence is DecisionCadence.INTRADAY
    for segment, bot in by_segment.items():
        if segment is TradingSegment.CASH_INTRADAY:
            continue
        assert bot.cadence is DecisionCadence.ONCE_PER_SESSION
        assert bot.cadence.value in bot.maturity().evidence


@pytest.mark.unit
def test_the_mcx_bot_is_a_whole_bot_with_no_data_rather_than_a_stub() -> None:
    """`R.04`: what is gated is ACTIVATION, never the algorithm.

    The MCX bot must be the same class as its NSE siblings and answer every protocol member. `B30`
    is a data blocker; a stub would be a scope reduction.
    """
    mcx = CommodityMcxBasisCarryBot()
    assert isinstance(mcx, StockFutureBasisCarryBot.__mro__[1])
    assert mcx.trading_segment is TradingSegment.COMMODITY_MCX
    context = context_for((), {})
    mcx.observe(context)
    assert mcx.propose(context) == ()
    assert mcx.maturity().rung is BotMaturityRung.COLD_START


@pytest.mark.unit
def test_registration_does_not_contaminate_the_bots_that_then_trade() -> None:
    """The probe feeds synthetic prices, and `observe` is idempotent BY INSTANT.

    Written from the defect: a bot probed at `now` silently ignored a real universe observed at the
    same `now`, and the dashboard reported two instruments per bot while looking entirely healthy.
    """
    probed = build_all_segment_bots()[0]
    for context in registration_contexts(INSTANT):
        probed.observe(context)
    assert probed.instruments_tracked > 0, "the probe does touch carried state — that is the point"

    fresh = build_all_segment_bots()[0]
    instruments = cash_instruments(12)
    prices = {
        instrument.instrument_token: Decimal(str(100_000 + index * 5_000))
        for index, instrument in enumerate(instruments)
    }
    fresh.observe(context_for(instruments, {"last_price_paise_by_token": prices}))
    assert fresh.instruments_tracked == len(instruments)


@pytest.mark.unit
def test_the_cross_sectional_cut_refuses_a_section_too_thin_to_have_one() -> None:
    """`None` is a refusal to act, never a permissive default."""
    bot = CashIntradayMeanReversionBot()
    assert bot.cross_sectional_cut({}) is None
    assert bot.cross_sectional_cut({1: 2.0, 2: 3.0}) is None
    cut = bot.cross_sectional_cut({1: 1.0, 2: 2.0, 3: 3.0})
    assert cut is not None and math.isfinite(cut) and cut > 0


@pytest.mark.unit
def test_option_bots_refuse_a_barred_underlying() -> None:
    """The exchange's own F&O ban list, carried in rather than inferred."""
    bot = StockOptionVolatilityPremiumBot()
    instruments = option_instruments()
    memory: dict[str, object] = {
        "last_price_paise_by_token": {
            instrument.instrument_token: Decimal("20000") for instrument in instruments
        },
        "underlying_price_paise_by_symbol": {"NIFTY": Decimal("2480000")},
        "fo_ban_list_symbols": {"NIFTY"},
    }
    for step in range(30):
        bot.observe(context_for(instruments, memory, step=step))
    assert bot.propose(context_for(instruments, memory, step=30)) == ()


@pytest.mark.unit
def test_every_bot_refuses_to_report_itself_graduated() -> None:
    """`R.22`'s two-key rule, made unavailable in the type rather than discouraged in prose."""
    for factory in ALL_BOT_FACTORIES:
        assert factory().maturity().rung is not BotMaturityRung.GRADUATED
