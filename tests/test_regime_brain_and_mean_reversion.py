"""The four-regime brain and its first consumer.

The property that matters most, tested from several angles: the brain must CHANGE the
decision. A regime engine that produces a number nobody acts on differently is a
labelling layer, not an engine (`R.23a`).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeDistributionError,
    RegimeOpinion,
)
from nse_algo_trader.regime.markov_switching_regime_model import (
    MarkovRegimeModelError,
    MarkovSwitchingRegimeModel,
)
from nse_algo_trader.regime.session_phase_regime_classifier import (
    SessionPhaseRegimeClassifier,
)
from nse_algo_trader.regime.soft_regime_weighting_brain import (
    ClassifierReliability,
    RegimeBelief,
    SoftRegimeWeightingBrain,
    total_variation_distance,
)
from nse_algo_trader.regime.trend_strength_regime_classifier import (
    TrendStrengthRegimeClassifier,
)
from nse_algo_trader.regime.volatility_regime_classifier import VolatilityRegimeClassifier
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 11, 6, 30, tzinfo=UTC)


def _opinion(name: str, regime: MarketRegime, mature: bool = True) -> RegimeOpinion:
    return RegimeOpinion(
        name,
        RegimeDistribution.from_scores({r: (6.0 if r is regime else 0.4) for r in MarketRegime}),
        NOW,
        mature,
        200,
    )


def _belief(regime: MarketRegime, agreement: float = 1.0) -> RegimeBelief:
    return RegimeBelief(
        RegimeDistribution.from_scores({r: (6.0 if r is regime else 0.4) for r in MarketRegime}),
        ("trend_strength", "volatility"),
        1.0 - agreement,
        {"trend_strength": 0.5, "volatility": 0.5},
        NOW,
    )


def _matured_engine(seed: int = 3) -> IntradayMeanReversionEngine:
    rng = np.random.default_rng(seed)
    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=0.2, minimum_regime_agreement=0.5
    )
    price = 100.0
    for _ in range(200):
        price *= 1 + rng.normal(0, 0.004)
        engine.observe(price)
    engine.observe(price * 0.94)
    return engine


# ------------------------------------------------------------ distributions


@pytest.mark.unit
def test_a_distribution_must_actually_be_one() -> None:
    with pytest.raises(RegimeDistributionError):
        RegimeDistribution(
            {
                MarketRegime.TRENDING: 0.5,
                MarketRegime.RANGING: 0.2,
                MarketRegime.VOLATILE: 0.1,
                MarketRegime.QUIET: 0.1,
            }
        )
    with pytest.raises(RegimeDistributionError, match="missing"):
        RegimeDistribution({MarketRegime.TRENDING: 1.0})
    with pytest.raises(RegimeDistributionError):
        RegimeDistribution(
            {
                MarketRegime.TRENDING: math.nan,
                MarketRegime.RANGING: 0.0,
                MarketRegime.VOLATILE: 0.0,
                MarketRegime.QUIET: 0.0,
            }
        )


@pytest.mark.adversarial
def test_all_zero_scores_become_uniform_not_an_error() -> None:
    """A classifier legitimately has no opinion on flat data. No opinion is maximum
    entropy, not a failure."""
    distribution = RegimeDistribution.from_scores(dict.fromkeys(MarketRegime, 0.0))
    assert distribution.normalised_entropy == pytest.approx(1.0)


@pytest.mark.property
def test_entropy_is_zero_when_certain_and_one_when_uniform() -> None:
    assert RegimeDistribution.certain(MarketRegime.RANGING).normalised_entropy == 0.0
    assert RegimeDistribution(
        MarketRegime.uniform_probabilities()
    ).normalised_entropy == pytest.approx(1.0)


# -------------------------------------------------------------- classifiers


@pytest.mark.unit
def test_the_trend_classifier_separates_a_trend_from_chop() -> None:
    trending, choppy = TrendStrengthRegimeClassifier(), TrendStrengthRegimeClassifier()
    rng = np.random.default_rng(0)
    for index in range(60):
        price = 100 + index * 0.5
        trending.observe(price + 0.3, price - 0.3, price)
        chop = 100 + rng.uniform(-1, 1)
        choppy.observe(chop + 0.3, chop - 0.3, chop)
    assert trending.reading().distribution.most_likely is MarketRegime.TRENDING
    assert choppy.reading().distribution.most_likely is MarketRegime.RANGING


@pytest.mark.adversarial
def test_a_flat_tape_does_not_read_as_a_trend() -> None:
    """Zero movement has no direction. A classifier that calls it trending would veto
    mean reversion exactly when mean reversion is safest."""
    classifier = TrendStrengthRegimeClassifier()
    for _ in range(60):
        classifier.observe(100.0, 100.0, 100.0)
    assert classifier.reading().distribution.most_likely is MarketRegime.RANGING


@pytest.mark.unit
def test_wilder_state_is_carried_not_recomputed() -> None:
    """Streaming must equal replay: a rolling-window version would silently change its
    answer for a past bar once later bars arrived, which is lookahead by another name."""
    bars = [(100 + i * 0.4 + 0.3, 100 + i * 0.4 - 0.3, 100 + i * 0.4) for i in range(50)]
    streamed = TrendStrengthRegimeClassifier()
    for bar in bars:
        streamed.observe(*bar)
    replayed = TrendStrengthRegimeClassifier()
    replayed.observe_bars(bars)
    assert streamed.reading().average_directional_index == pytest.approx(
        replayed.reading().average_directional_index
    )


@pytest.mark.unit
def test_volatility_is_judged_against_the_instruments_own_history() -> None:
    rng = np.random.default_rng(1)
    classifier = VolatilityRegimeClassifier()
    price = 100.0
    for index in range(80):
        price *= 1 + rng.normal(0, 0.0008 if index < 60 else 0.02)
        classifier.observe(price)
    assert classifier.opinion(NOW).distribution.most_likely is MarketRegime.VOLATILE


@pytest.mark.unit
def test_an_immature_volatility_classifier_says_it_does_not_know() -> None:
    classifier = VolatilityRegimeClassifier()
    for price in (100.0, 101.0, 100.5):
        classifier.observe(price)
    opinion = classifier.opinion(NOW)
    assert not opinion.is_mature
    assert opinion.confidence == 0.0
    assert opinion.distribution.normalised_entropy == pytest.approx(1.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 20, MarketRegime.VOLATILE),
        (12, 0, MarketRegime.RANGING),
        (15, 20, MarketRegime.VOLATILE),
    ],
)
def test_the_session_classifier_knows_the_clock_with_certainty(
    hour: int, minute: int, expected: MarketRegime
) -> None:
    classifier = SessionPhaseRegimeClassifier()
    moment = datetime(2026, 8, 11, hour, minute, tzinfo=IST)
    classifier.observe(moment, 101, 99, 100.5)
    assert classifier.opinion(moment).distribution.most_likely is expected


# ------------------------------------------------------------ markov model


@pytest.mark.unit
def test_the_markov_model_separates_calm_from_turbulent() -> None:
    rng = np.random.default_rng(0)
    training = list(np.concatenate([rng.normal(0, 0.004, 200), rng.normal(0, 0.02, 200)]))
    model = MarkovSwitchingRegimeModel()
    model.fit_on_history(training)
    assert model.is_mature
    calm = model.opinion([*training, *rng.normal(0, 0.004, 40)], NOW)
    wild = model.opinion([*training, *rng.normal(0, 0.03, 40)], NOW)
    assert calm.distribution.most_likely is MarketRegime.QUIET
    assert wild.distribution.most_likely is MarketRegime.VOLATILE


@pytest.mark.adversarial
def test_the_markov_model_refuses_to_infer_before_it_is_fitted() -> None:
    with pytest.raises(MarkovRegimeModelError, match="no parameters"):
        MarkovSwitchingRegimeModel().filtered_beliefs([0.01, -0.01])


@pytest.mark.adversarial
def test_too_little_history_leaves_the_model_unfitted_rather_than_overfitted() -> None:
    """`R.04`: the algorithm is full strength; only ACTIVATION waits for data."""
    model = MarkovSwitchingRegimeModel()
    model.fit_on_history([0.001] * 20)
    assert not model.is_fitted
    assert not model.opinion([0.001] * 20, NOW).is_mature


# -------------------------------------------------------------------- brain


@pytest.mark.unit
def test_nothing_armed_means_abstain_not_a_guess() -> None:
    brain = SoftRegimeWeightingBrain()
    belief = brain.combine([_opinion("trend_strength", MarketRegime.RANGING)], NOW)
    assert belief.contributing_classifiers == ()
    assert belief.most_likely is None, "a uniform belief must not name a winner"
    assert belief.distribution.normalised_entropy == pytest.approx(1.0)


@pytest.mark.unit
def test_arming_is_a_flag_and_changes_the_belief() -> None:
    """`A.08`: all four built, one armed, and arming later is not a code change."""
    brain = SoftRegimeWeightingBrain()
    opinions = [_opinion("trend_strength", MarketRegime.RANGING)]
    assert brain.combine(opinions, NOW).most_likely is None
    brain.arm("trend_strength")
    assert brain.combine(opinions, NOW).most_likely is MarketRegime.RANGING


@pytest.mark.unit
def test_an_unarmed_classifier_still_earns_reliability() -> None:
    """How an unarmed engine qualifies to be armed — it is scored all along."""
    brain = SoftRegimeWeightingBrain()
    opinion = _opinion("session_phase", MarketRegime.RANGING)
    brain.record_outcome(opinion, MarketRegime.RANGING)
    assert brain.reliability["session_phase"].scored_opinions == 1
    assert brain.weight_for(opinion) == 0.0


@pytest.mark.unit
def test_an_immature_classifier_contributes_nothing() -> None:
    brain = SoftRegimeWeightingBrain()
    brain.arm("volatility")
    assert brain.weight_for(_opinion("volatility", MarketRegime.QUIET, mature=False)) == 0.0


@pytest.mark.unit
def test_disagreement_is_reported_rather_than_averaged_away() -> None:
    brain = SoftRegimeWeightingBrain()
    brain.arm("trend_strength")
    brain.arm("volatility")
    agreed = brain.combine(
        [
            _opinion("trend_strength", MarketRegime.RANGING),
            _opinion("volatility", MarketRegime.RANGING),
        ],
        NOW,
    )
    split = brain.combine(
        [
            _opinion("trend_strength", MarketRegime.RANGING),
            _opinion("volatility", MarketRegime.TRENDING),
        ],
        NOW,
    )
    assert agreed.agreement == pytest.approx(1.0)
    assert split.agreement < 0.5
    assert not split.is_actionable(0.3, 0.6)


@pytest.mark.unit
def test_a_classifier_with_no_skill_loses_its_weight() -> None:
    """A systematically wrong engine removes itself without anyone intervening."""
    brain = SoftRegimeWeightingBrain()
    brain.arm("trend_strength")
    wrong = _opinion("trend_strength", MarketRegime.TRENDING)
    for _ in range(30):
        brain.record_outcome(wrong, MarketRegime.RANGING)
    assert brain.reliability["trend_strength"].skill == 0.0
    assert brain.weight_for(wrong) == 0.0


@pytest.mark.unit
def test_reliability_with_no_history_is_equal_footing() -> None:
    assert ClassifierReliability().skill == 1.0
    assert ClassifierReliability().mean_brier_score is None


@pytest.mark.property
def test_total_variation_is_zero_for_identical_and_one_for_disjoint() -> None:
    ranging = RegimeDistribution.certain(MarketRegime.RANGING)
    trending = RegimeDistribution.certain(MarketRegime.TRENDING)
    assert total_variation_distance(ranging, ranging) == pytest.approx(0.0)
    assert total_variation_distance(ranging, trending) == pytest.approx(1.0)


# ---------------------------------------------- the decision actually changes


@pytest.mark.unit
def test_the_same_deviation_produces_opposite_decisions_by_regime() -> None:
    """THE test for this slice. If the regime does not flip the decision, the brain is
    a labelling layer and not an engine (`R.23a`)."""
    engine = _matured_engine()
    ranging = engine.decide(_belief(MarketRegime.RANGING))
    trending = engine.decide(_belief(MarketRegime.TRENDING))
    assert ranging.action is MeanReversionAction.ENTER_LONG
    assert trending.action is MeanReversionAction.ABSTAIN
    assert ranging.deviation == trending.deviation, "same price, same deviation"


@pytest.mark.adversarial
def test_a_split_panel_vetoes_even_an_extreme_deviation() -> None:
    engine = _matured_engine()
    decision = engine.decide(_belief(MarketRegime.RANGING, agreement=0.2))
    assert decision.action is MeanReversionAction.ABSTAIN
    assert "not actionable" in decision.reason


@pytest.mark.unit
def test_an_immature_engine_abstains_rather_than_using_an_unearned_band() -> None:
    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=0.2, minimum_regime_agreement=0.5
    )
    engine.observe_closes([100.0, 101.0, 99.0])
    decision = engine.decide(_belief(MarketRegime.RANGING))
    assert decision.action is MeanReversionAction.ABSTAIN
    assert decision.deviation_band is None


@pytest.mark.unit
def test_a_deviation_inside_the_band_is_not_traded() -> None:
    engine = _matured_engine()
    inside = engine._current_deviation()
    assert inside is not None
    engine.observe(engine._closes[-1])  # nudge back toward the mean
    decision = engine.decide(_belief(MarketRegime.RANGING))
    if abs(engine._current_deviation() or 0) < (engine.deviation_band() or 0):
        assert decision.action is MeanReversionAction.ABSTAIN


@pytest.mark.unit
def test_conviction_scales_with_agreement() -> None:
    """A huge deviation in an uncertain regime is not a strong trade."""
    engine = _matured_engine()
    strong = engine.decide(_belief(MarketRegime.RANGING, agreement=1.0))
    weaker = engine.decide(_belief(MarketRegime.RANGING, agreement=0.7))
    assert strong.conviction > weaker.conviction > 0.0


@pytest.mark.adversarial
def test_a_deviation_above_the_mean_goes_short_not_long() -> None:
    """Sign errors in a reversion engine are silent and expensive."""
    engine = _matured_engine()
    engine.observe(engine._closes[-1] * 1.15)
    decision = engine.decide(_belief(MarketRegime.RANGING))
    if decision.is_actionable:
        assert decision.action is MeanReversionAction.ENTER_SHORT
