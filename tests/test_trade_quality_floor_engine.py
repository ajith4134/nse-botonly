"""Tests for the pre-trade quality floor and its evidence card — `L5.31`, spec `docs/research/260`.

The `real_data` test at the bottom is the one that matters most: it fits the whole engine on the
3,481 retained closed trades and requires it to refuse the shape that lost ₹3,56,631 while admitting
the shape that made ₹21,213 **on a 46.3% win rate** — the case any fixed win-rate rule gets exactly
backwards.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy
import pytest
from hypothesis import given, settings
from hypothesis import strategies as strategy

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.gross_expectancy_posterior import (
    estimate_gross_expectancy_posterior,
)
from nse_algo_trader.trade_quality.realized_payoff_distribution_estimator import (
    RealisedPayoffDistributionEstimator,
    RealisedTradeOutcome,
    bayesian_bootstrap_mean,
)
from nse_algo_trader.trade_quality.selection_corrected_quality_floor import (
    binding_floor_of,
    expected_maximum_of_standard_normals,
    priced_cost_floor,
    realised_cost_floor,
    selection_corrected_floor,
)
from nse_algo_trader.trade_quality.stated_probability_calibrator import (
    ForecastOutcome,
    StatedProbabilityCalibrator,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    CalibratedProbability,
    CalibrationMethod,
    PayoffPosterior,
    QualityVerdict,
    TradeQualityError,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_store import TradeQualityEvidenceStore
from nse_algo_trader.trade_quality.trade_quality_floor_engine import (
    QualityAssessmentRequest,
    QualityFloorPolicy,
    TradeQualityFloorEngine,
    stated_win_probability_of,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

RETAINED_RECORD = Path("~/.nse_algo_trader/experience_memory.sqlite3").expanduser()
SESSION = date(2026, 8, 17)
INSTANT = datetime(2026, 8, 17, 10, 15, tzinfo=UTC)
POLICY = QualityFloorPolicy(admission_confidence=0.9)
ONE_PAISE = Decimal("0.01")
"""The rupee's smallest unit. A currency fact, not a tolerance chosen to make an assertion pass."""


def a_signal(
    *,
    symbol: str = "RELIANCE",
    quantity: int = 100,
    conviction: Decimal | None = Decimal("0.6"),
    price_paise: Decimal = Decimal("250000"),
) -> PricedSignal:
    return PricedSignal(
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        decided_at=INSTANT,
        reference_price_paise=price_paise,
        expected_edge_bps=Decimal("25"),
        proposed_quantity=quantity,
        edge_basis=EdgeBasis.MEASURED_TRACK_RECORD,
        source="test",
        conviction=conviction,
    )


def outcomes(
    bot: str,
    grosses: list[str],
    *,
    costs: str = "0",
    notional: str | None = "250000",
    segment: TradingSegment = TradingSegment.CASH_INTRADAY,
) -> list[RealisedTradeOutcome]:
    return [
        RealisedTradeOutcome(
            bot_identity=bot,
            trading_segment=segment,
            session_date=SESSION - timedelta(days=index % 5),
            gross_rupees=Decimal(gross),
            costs_rupees=Decimal(costs),
            notional_rupees=None if notional is None else Decimal(notional),
        )
        for index, gross in enumerate(grosses)
    ]


def forecasts(
    bot: str, rows: list[tuple[float, bool]], *, sessions: int = 4
) -> list[ForecastOutcome]:
    return [
        ForecastOutcome(
            bot_identity=bot,
            trading_segment=TradingSegment.CASH_INTRADAY,
            occurred_at=INSTANT - timedelta(days=index),
            session_date=SESSION - timedelta(days=index % sessions),
            stated_probability=stated,
            was_win=won,
        )
        for index, (stated, won) in enumerate(rows)
    ]


def a_request(
    *,
    bot: str = "bot",
    cost: str = "50",
    candidates: list[str] | None = None,  # per-notional fractions; default is a scan of one
    conviction: Decimal | None = Decimal("0.6"),
    quantity: int = 100,
) -> QualityAssessmentRequest:
    signal = a_signal(quantity=quantity, conviction=conviction)
    return QualityAssessmentRequest(
        signal=signal,
        bot_identity=bot,
        trading_segment=TradingSegment.CASH_INTRADAY,
        assessed_at=INSTANT,
        round_trip_cost_rupees=Decimal(cost),
        candidate_expectancy_fractions=tuple(
            Decimal(value) for value in (candidates or ["0"])
        ),
        stated_win_probability=None if conviction is None else float(conviction),
    )


# --------------------------------------------------------------------------------------------
# unit
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_break_even_rate_is_the_retained_records_arithmetic() -> None:
    """The two retained shapes, and the rate each one's payoff actually demanded."""
    losing = PayoffPosterior(
        win_mean_rupees=Decimal("292.96"),
        win_lower_rupees=Decimal("280"),
        win_upper_rupees=Decimal("305"),
        loss_mean_rupees=Decimal("337.96"),
        loss_lower_rupees=Decimal("320"),
        loss_upper_rupees=Decimal("355"),
        wins_observed=1068,
        losses_observed=1981,
        credible_mass=0.9,
    )
    winning = PayoffPosterior(
        win_mean_rupees=Decimal("570.83"),
        win_lower_rupees=Decimal("430"),
        win_upper_rupees=Decimal("720"),
        loss_mean_rupees=Decimal("199.14"),
        loss_lower_rupees=Decimal("160"),
        loss_upper_rupees=Decimal("240"),
        wins_observed=56,
        losses_observed=65,
        credible_mass=0.9,
    )
    assert losing.payoff_ratio == pytest.approx(0.867, abs=0.001)
    assert losing.break_even_win_rate == pytest.approx(0.536, abs=0.001)
    assert winning.payoff_ratio == pytest.approx(2.866, abs=0.001)
    assert winning.break_even_win_rate == pytest.approx(0.259, abs=0.001)


@pytest.mark.unit
def test_a_bot_that_has_never_lost_has_an_unmeasured_ratio_not_an_infinite_one() -> None:
    unmeasured = PayoffPosterior(
        win_mean_rupees=Decimal("500"),
        win_lower_rupees=Decimal("400"),
        win_upper_rupees=Decimal("600"),
        loss_mean_rupees=Decimal("0"),
        loss_lower_rupees=Decimal("0"),
        loss_upper_rupees=Decimal("0"),
        wins_observed=9,
        losses_observed=0,
        credible_mass=0.9,
    )
    assert unmeasured.payoff_ratio is None
    assert unmeasured.break_even_win_rate is None


@pytest.mark.unit
def test_overconfidence_and_the_negative_information_diagnostic() -> None:
    claimed = CalibratedProbability(
        stated=0.715,
        calibrated=0.463,
        method=CalibrationMethod.ISOTONIC_BINNED,
        fitted_on_trades=121,
        brier_reliability=0.08,
        brier_resolution=0.01,
        brier_uncertainty=0.25,
    )
    assert claimed.overconfidence == pytest.approx(0.252, abs=0.001)
    assert claimed.carries_negative_information is True


@pytest.mark.unit
def test_the_bootstrap_brackets_its_own_mean_and_refuses_a_sample_of_one() -> None:
    interval = bayesian_bootstrap_mean([Decimal(value) for value in ("100", "200", "300", "-50")])
    assert interval is not None
    assert interval.lower <= interval.mean <= interval.upper
    assert interval.observations == 4
    assert bayesian_bootstrap_mean([Decimal("100")]) is None


@pytest.mark.unit
def test_the_bootstrap_is_deterministic_on_the_same_record() -> None:
    sample = [Decimal(value) for value in ("100", "-40", "220", "-15", "9")]
    first = bayesian_bootstrap_mean(sample)
    second = bayesian_bootstrap_mean(sample)
    assert first == second


@pytest.mark.unit
def test_expected_maximum_matches_the_known_values_and_is_zero_for_one_candidate() -> None:
    """`E[max]` of n standard normals: ~1.54 at 10, ~2.51 at 100, ~3.24 at 1000."""
    assert expected_maximum_of_standard_normals(1) == 0.0
    assert expected_maximum_of_standard_normals(10) == pytest.approx(1.54, abs=0.08)
    assert expected_maximum_of_standard_normals(100) == pytest.approx(2.51, abs=0.08)
    assert expected_maximum_of_standard_normals(1000) == pytest.approx(3.24, abs=0.08)


@pytest.mark.unit
def test_the_selection_floor_names_its_breadth_and_dispersion() -> None:
    floor = selection_corrected_floor(
        [Decimal(value) for value in ("0.01", "0.005", "0.001", "-0.002")],
        proposal_notional_rupees=Decimal("250000"),
    )
    assert floor.rupees > 0
    assert "4 distinct candidates (of 4 scanned)" in floor.explanation


@pytest.mark.unit
def test_the_selection_floor_scales_with_the_proposal_notional() -> None:
    """The correction is a fraction of notional; a larger position carries a larger rupee floor."""
    scores = [Decimal(value) for value in ("0.01", "0.005", "0.001", "-0.002")]
    small = selection_corrected_floor(scores, proposal_notional_rupees=Decimal("100000"))
    large = selection_corrected_floor(scores, proposal_notional_rupees=Decimal("1000000"))
    assert large.rupees == small.rupees * 10


@pytest.mark.unit
def test_identical_candidates_carry_no_selection_correction() -> None:
    """Nothing was selected ON, so nothing is corrected for — however wide the scan."""
    floor = selection_corrected_floor(
        [Decimal("0.01")] * 500, proposal_notional_rupees=Decimal("250000")
    )
    assert floor.rupees == Decimal("0")


@pytest.mark.unit
def test_the_binding_floor_is_the_highest_one() -> None:
    cost = priced_cost_floor(Decimal("500"))
    realised = realised_cost_floor(Decimal("80"), 30)
    assert realised.rupees == Decimal("80")
    assert binding_floor_of([cost, realised]) is cost


@pytest.mark.unit
def test_stated_probability_is_read_from_conviction_and_absent_when_none() -> None:
    assert stated_win_probability_of(a_signal(conviction=Decimal("0.42"))) == pytest.approx(0.42)
    assert stated_win_probability_of(a_signal(conviction=None)) is None


@pytest.mark.unit
def test_the_calibrator_pulls_an_overconfident_forecaster_down_to_its_own_record() -> None:
    rows = [(0.8, index % 10 < 3) for index in range(120)]
    calibrator = StatedProbabilityCalibrator(forecasts("overconfident", rows))
    corrected = calibrator.calibrate(
        0.8, bot_identity="overconfident", trading_segment=TradingSegment.CASH_INTRADAY
    )
    assert corrected.overconfidence > 0.4
    assert corrected.calibrated == pytest.approx(0.3, abs=0.05)


@pytest.mark.unit
def test_the_median_notional_resists_one_outsized_position() -> None:
    estimator = RealisedPayoffDistributionEstimator(
        [
            *outcomes("bot", ["100", "-50", "70"], notional="100000"),
            *outcomes("bot", ["900"], notional="90000000"),
        ]
    )
    assert estimator.median_notional_for("bot") == Decimal("100000")


@pytest.mark.unit
def test_the_engine_admits_a_proposal_whose_expectancy_clears_every_floor() -> None:
    cycle = ["900", "850", "1000", "-100", "-90", "-110", "950", "-95"]
    wins_and_losses = cycle * 6
    # The calibrator and the payoff estimator MUST describe the same trades. An earlier version of
    # this fixture had them at 67% and 50% win rates respectively, and the coherence check added for
    # `docs/research/261` MAJOR-7 caught it — in a test I had written to prove admissions work.
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(
            forecasts(
                "good",
                [
                    (0.6, float(wins_and_losses[index]) > 0)
                    for index in range(len(wins_and_losses))
                ],
            )
        ),
        RealisedPayoffDistributionEstimator(outcomes("good", wins_and_losses)),
        POLICY,
    )
    card = engine.assess(a_request(bot="good", cost="20"))
    assert card.verdict is QualityVerdict.ADMIT
    assert card.margin_rupees is not None
    assert card.margin_rupees > 0
    assert "clears" in card.reason


@pytest.mark.unit
def test_the_engine_refuses_the_symmetric_payoff_that_sank_the_retained_record() -> None:
    """35% wins against a 0.867 payoff ratio: the exact shape of `opening_range_breakout_v1`."""
    record = ["293"] * 35 + ["-338"] * 65
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(
            forecasts("orb", [(0.42, index < 35) for index in range(100)])
        ),
        RealisedPayoffDistributionEstimator(outcomes("orb", record)),
        POLICY,
    )
    card = engine.assess(a_request(bot="orb", cost="60"))
    assert card.verdict is QualityVerdict.REFUSE
    assert card.margin_rupees is not None
    assert card.margin_rupees < 0


@pytest.mark.unit
def test_the_store_is_idempotent_and_joins_an_outcome_once(tmp_path: Path) -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("s", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("s", ["300", "-100", "250", "-90"] * 5)),
        POLICY,
    )
    card = engine.assess(a_request(bot="s"))
    store = TradeQualityEvidenceStore(tmp_path / "evidence.sqlite3")
    assert store.record(card) is True
    assert store.record(card) is False
    assert store.verdict_counts(SESSION)[card.verdict] == 1
    store.attach_realised_outcome(card.content_hash, Decimal("-120"), attached_at=INSTANT)
    assert list(store.scored_verdicts()) == [(card.verdict, Decimal("-120"))]
    with pytest.raises(TradeQualityError, match="already carries a realised outcome"):
        store.attach_realised_outcome(card.content_hash, Decimal("5"), attached_at=INSTANT)


@pytest.mark.unit
def test_a_stored_card_round_trips(tmp_path: Path) -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("r", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("r", ["300", "-100", "250", "-90"] * 5)),
        POLICY,
    )
    card = engine.assess(a_request(bot="r"))
    store = TradeQualityEvidenceStore(tmp_path / "evidence.sqlite3")
    store.record(card)
    (rebuilt,) = store.cards_for_session(SESSION)
    assert rebuilt.content_hash == card.content_hash
    assert rebuilt.binding_floor.derivation is card.binding_floor.derivation
    assert rebuilt.verdict is card.verdict


# --------------------------------------------------------------------------------------------
# property
# --------------------------------------------------------------------------------------------


@pytest.mark.property
@given(
    breadth=strategy.integers(min_value=1, max_value=40),
    extra=strategy.integers(min_value=1, max_value=40),
)
@settings(max_examples=40, deadline=None)
def test_the_selection_floor_never_falls_as_the_scan_widens(breadth: int, extra: int) -> None:
    """A wider scan cannot buy a lower bar. The whole point of the correction."""
    dispersed = [
        Decimal(str(value * 7 % 53)) / Decimal("1000") for value in range(breadth + extra)
    ]
    narrow = selection_corrected_floor(
        dispersed[:breadth], proposal_notional_rupees=Decimal("250000")
    )
    wide = selection_corrected_floor(dispersed, proposal_notional_rupees=Decimal("250000"))
    assert expected_maximum_of_standard_normals(len(dispersed)) >= (
        expected_maximum_of_standard_normals(breadth)
    )
    assert narrow.rupees >= 0
    assert wide.rupees >= 0


@pytest.mark.property
@given(candidates=strategy.integers(min_value=1, max_value=100_000))
@settings(max_examples=60, deadline=None)
def test_the_expected_maximum_is_finite_and_non_negative_at_every_breadth(candidates: int) -> None:
    value = expected_maximum_of_standard_normals(candidates)
    assert value >= 0.0
    assert value < 6.0


@pytest.mark.property
@given(
    nets=strategy.lists(
        strategy.integers(min_value=-5000, max_value=5000), min_size=2, max_size=60
    )
)
@settings(max_examples=40, deadline=None)
def test_a_bootstrap_interval_always_brackets_its_mean(nets: list[int]) -> None:
    interval = bayesian_bootstrap_mean([Decimal(value) for value in nets])
    assert interval is not None
    assert interval.lower <= interval.mean <= interval.upper


@pytest.mark.property
@given(cost=strategy.integers(min_value=0, max_value=100_000))
@settings(max_examples=30, deadline=None)
def test_raising_the_cost_floor_never_turns_a_refusal_into_an_admission(cost: int) -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("m", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("m", ["400", "-100", "380", "-120"] * 5)),
        POLICY,
    )
    cheap = engine.assess(a_request(bot="m", cost="0"))
    dearer = engine.assess(a_request(bot="m", cost=str(cost)))
    if dearer.verdict is QualityVerdict.ADMIT:
        assert cheap.verdict is QualityVerdict.ADMIT


# --------------------------------------------------------------------------------------------
# adversarial
# --------------------------------------------------------------------------------------------


@pytest.mark.adversarial
def test_a_bot_claiming_ninety_nine_percent_on_everything_collapses_to_its_base_rate() -> None:
    """Gaming the gate with a constant claim. A constant has no resolution to fit."""
    rows = [(0.99, index % 10 < 2) for index in range(100)]
    calibrator = StatedProbabilityCalibrator(forecasts("gamer", rows))
    corrected = calibrator.calibrate(
        0.99, bot_identity="gamer", trading_segment=TradingSegment.CASH_INTRADAY
    )
    assert corrected.calibrated == pytest.approx(0.2, abs=0.05)
    assert corrected.overconfidence > 0.7


@pytest.mark.adversarial
def test_a_cold_start_bot_is_unassessable_rather_than_admitted() -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator([]), RealisedPayoffDistributionEstimator([]), POLICY
    )
    card = engine.assess(a_request(bot="newborn"))
    assert card.verdict is QualityVerdict.UNASSESSABLE
    assert card.margin_rupees is None
    assert "unmeasured rather than favourable" in card.reason


@pytest.mark.adversarial
def test_a_bot_that_has_only_ever_won_cannot_be_admitted_on_a_missing_denominator() -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("w", [(0.6, True) for _ in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("w", ["100"] * 20)),
        POLICY,
    )
    card = engine.assess(a_request(bot="w"))
    assert card.verdict is QualityVerdict.UNASSESSABLE


@pytest.mark.adversarial
def test_a_proposal_with_no_stated_belief_is_unassessable() -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("q", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("q", ["300", "-100"] * 10)),
        POLICY,
    )
    card = engine.assess(a_request(bot="q", conviction=None))
    assert card.verdict is QualityVerdict.UNASSESSABLE
    assert "stated no win probability" in card.reason


@pytest.mark.adversarial
def test_a_negative_cost_is_refused_rather_than_licensing_a_losing_trade() -> None:
    with pytest.raises(TradeQualityError, match="negative cost is a rebate"):
        priced_cost_floor(Decimal("-1"))


@pytest.mark.adversarial
def test_a_non_finite_probability_is_refused_at_the_card() -> None:
    with pytest.raises(TradeQualityError, match="not finite"):
        CalibratedProbability(
            stated=float("nan"),
            calibrated=0.5,
            method=CalibrationMethod.POOLED_BASE_RATE,
            fitted_on_trades=10,
            brier_reliability=None,
            brier_resolution=None,
            brier_uncertainty=None,
        )


@pytest.mark.adversarial
def test_an_empty_candidate_scan_is_refused_rather_than_scored_as_breadth_zero() -> None:
    with pytest.raises(TradeQualityError, match="empty candidate scan"):
        QualityAssessmentRequest(
            signal=a_signal(),
            bot_identity="bot",
            trading_segment=TradingSegment.CASH_INTRADAY,
            assessed_at=INSTANT,
            round_trip_cost_rupees=Decimal("10"),
            candidate_expectancy_fractions=(),
            stated_win_probability=0.6,
        )


@pytest.mark.adversarial
def test_zero_variance_payoffs_still_produce_a_verdict_rather_than_a_crash() -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("z", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("z", ["100", "-100"] * 10)),
        POLICY,
    )
    card = engine.assess(a_request(bot="z"))
    assert card.verdict in (QualityVerdict.ADMIT, QualityVerdict.REFUSE)


@pytest.mark.adversarial
def test_the_evidence_store_refuses_a_delete(tmp_path: Path) -> None:
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(forecasts("d", [(0.6, index % 2 == 0) for index in range(20)])),
        RealisedPayoffDistributionEstimator(outcomes("d", ["300", "-100"] * 10)),
        POLICY,
    )
    store = TradeQualityEvidenceStore(tmp_path / "evidence.sqlite3")
    store.record(engine.assess(a_request(bot="d")))
    with (
        sqlite3.connect(store.database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute("DELETE FROM trade_quality_evidence_card")


@pytest.mark.adversarial
def test_an_expectancy_posterior_refuses_an_inverted_interval() -> None:
    calibrated = CalibratedProbability(
        stated=0.6,
        calibrated=0.5,
        method=CalibrationMethod.POOLED_BASE_RATE,
        fitted_on_trades=5,
        brier_reliability=None,
        brier_resolution=None,
        brier_uncertainty=None,
    )
    assert (
        estimate_gross_expectancy_posterior(
            calibrated=calibrated,
            win_magnitudes=[],
            loss_magnitudes=[Decimal("10")],
            floor_rupees=Decimal("0"),
        )
        is None
    )


# --------------------------------------------------------------------------------------------
# real data — the R.05 gate
# --------------------------------------------------------------------------------------------


def _retained_rows() -> list[tuple[str, str, str, float, int, float, float]]:
    """The retained corpus, with gross recovered as `realized_pnl + total_fees`.

    The gate models the GROSS payoff distribution, because the priced cost is a separate floor.
    """
    with sqlite3.connect(RETAINED_RECORD) as connection:
        return [
            (str(tag), str(kind), str(session), float(stated), int(won), float(net), float(fees))
            for tag, kind, session, stated, won, net, fees in connection.execute(
                """
                SELECT strategy_tag, instrument_kind, session_date, win_probability,
                       actual_outcome = 'win', realized_pnl, COALESCE(total_fees, 0)
                  FROM experience_nodes
                 WHERE win_probability IS NOT NULL AND realized_pnl IS NOT NULL
                """
            )
        ]


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_RECORD.exists(), reason="the retained record is not on this box")
def test_the_engine_recovers_the_retained_records_own_arithmetic() -> None:
    """`R.05`. Fitted on 3,481 real closed trades, the estimators must reproduce what happened.

    The numbers asserted here are `docs/research/254`'s measurements, so a change in either the
    estimator or the record breaks this test rather than passing quietly.
    """
    rows = _retained_rows()
    assert len(rows) > 3000

    calibrator = StatedProbabilityCalibrator(
        [
            ForecastOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                occurred_at=datetime.fromisoformat(session).replace(tzinfo=UTC),
                session_date=date.fromisoformat(session),
                stated_probability=min(max(stated, 0.0), 1.0),
                was_win=net + fees > 0,
            )
            for tag, _kind, session, stated, _won, net, fees in rows
        ]
    )
    payoffs = RealisedPayoffDistributionEstimator(
        [
            RealisedTradeOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                session_date=date.fromisoformat(session),
                gross_rupees=Decimal(str(net)) + Decimal(str(fees)),
                costs_rupees=Decimal(str(fees)),
            )
            for tag, _kind, session, _stated, _won, net, fees in rows
        ]
    )

    orb = payoffs.posterior_for("opening_range_breakout_v1")
    spread = payoffs.posterior_for("credit_spread_v1")
    assert orb is not None
    assert spread is not None

    # GROSS, which is what the gate models: costs are a floor, not a deduction from the payoffs.
    assert orb.payoff_ratio == pytest.approx(0.750, abs=0.02)
    assert spread.payoff_ratio == pytest.approx(1.639, abs=0.05)
    assert orb.break_even_win_rate == pytest.approx(0.571, abs=0.01)
    assert spread.break_even_win_rate == pytest.approx(0.379, abs=0.01)

    # And the finding, in one line each: gross win rate against the rate the payoff demands.
    assert calibrator.base_rate_for("opening_range_breakout_v1") == pytest.approx(0.464, abs=0.01)
    assert calibrator.base_rate_for("credit_spread_v1") == pytest.approx(0.702, abs=0.01)

    # NET, which is `docs/research/254`'s table — reproduced here with its scratch bug CORRECTED.
    # That document reports credit_spread_v1's average loss as Rs 165.44 and its ratio as 3.450,
    # both taking `realized_pnl <= 0` as the loss bucket. Excluding its 11 scratches the average
    # loss is Rs 199.14 and the ratio is 2.866.
    net_only = RealisedPayoffDistributionEstimator(
        [
            RealisedTradeOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                session_date=date.fromisoformat(session),
                gross_rupees=Decimal(str(net)),
                costs_rupees=Decimal("0"),
            )
            for tag, _kind, session, _stated, _won, net, _fees in rows
        ]
    )
    net_orb = net_only.posterior_for("opening_range_breakout_v1")
    net_spread = net_only.posterior_for("credit_spread_v1")
    assert net_orb is not None
    assert net_spread is not None
    assert net_orb.payoff_ratio == pytest.approx(0.867, abs=0.02)
    assert net_spread.payoff_ratio == pytest.approx(2.866, abs=0.10)
    assert net_orb.break_even_win_rate == pytest.approx(0.536, abs=0.01)
    assert net_spread.break_even_win_rate == pytest.approx(0.259, abs=0.01)

    # The calibrator moves a stated probability onto the bot's own record in both directions:
    # `directional_option_orb_v1` claimed 0.775 and won 0.601 of its trades before costs.
    corrected = calibrator.calibrate(
        0.775,
        bot_identity="directional_option_orb_v1",
        trading_segment=TradingSegment.CASH_INTRADAY,
    )
    assert corrected.overconfidence > 0.15
    # Fitted on its own record: the evidence behind p=0.775 is the isotonic LEVEL SET, not one
    # sqrt(n) bin. `docs/research/261` MAJOR-B measured the single-bin version pinning evidence at
    # sqrt(n) — 316 at 100,000 trades — which was one of two reasons nothing was ever admitted.
    assert corrected.method is CalibrationMethod.ISOTONIC_BINNED
    assert corrected.fitted_on_trades > 19
    # LOCAL support, not the bot's 311 trades. `docs/research/261` CRITICAL-2: using the whole trade
    # count as the Beta concentration made every added trade — win OR loss — raise admission.
    assert 0 < corrected.fitted_on_trades < 311


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_RECORD.exists(), reason="the retained record is not on this box")
def test_the_gate_refuses_the_loser_and_admits_the_winner_that_lost_most_of_its_trades() -> None:
    """`R.05`, and the decisive one.

    `credit_spread_v1` won 46.3% of its trades and made ₹21,213. `opening_range_breakout_v1` won
    35.0% and lost ₹3,56,631. A gate that gets this pair the right way round is doing the job; one
    that thresholds a win rate gets it exactly backwards.
    """
    rows = _retained_rows()
    calibrator = StatedProbabilityCalibrator(
        [
            ForecastOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                occurred_at=datetime.fromisoformat(session).replace(tzinfo=UTC),
                session_date=date.fromisoformat(session),
                stated_probability=min(max(stated, 0.0), 1.0),
                was_win=net + fees > 0,
            )
            for tag, _kind, session, stated, _won, net, fees in rows
        ]
    )
    payoffs = RealisedPayoffDistributionEstimator(
        [
            RealisedTradeOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                session_date=date.fromisoformat(session),
                gross_rupees=Decimal(str(net)) + Decimal(str(fees)),
                costs_rupees=Decimal(str(fees)),
            )
            for tag, _kind, session, _stated, _won, net, fees in rows
        ]
    )
    engine = TradeQualityFloorEngine(calibrator, payoffs, POLICY)

    loser = engine.assess(
        a_request(bot="opening_range_breakout_v1", cost="30", conviction=Decimal("0.416"))
    )
    winner = engine.assess(
        a_request(bot="credit_spread_v1", cost="30", conviction=Decimal("0.715"))
    )
    assert loser.verdict is QualityVerdict.REFUSE
    assert loser.gross_expectancy is not None
    assert loser.gross_expectancy.probability_exceeding_floor < 0.01

    # The full bar, and it is worth stating why it is back. It was briefly weakened to "the winner
    # ranks above the loser" while the post-repair engine could admit nothing at all, and
    # `docs/research/261`'s re-review was right that a coin-flip ordering satisfies that. The
    # correct evidence count — the isotonic LEVEL SET rather than one sqrt(n) bin — restores the
    # admission on its merits.
    assert winner.verdict is QualityVerdict.ADMIT
    assert winner.gross_expectancy is not None
    assert winner.gross_expectancy.probability_exceeding_floor > (
        loser.gross_expectancy.probability_exceeding_floor
    )


# --------------------------------------------------------------------------------------------
# ground-truth sweeps — the attack the 33-test suite could not make (`docs/research/261`, `O.123`)
# --------------------------------------------------------------------------------------------


def _zero_skill_bot(
    rng: numpy.random.Generator, trades: int, *, true_win_rate: float = 0.30
) -> tuple[list[ForecastOutcome], list[RealisedTradeOutcome], float]:
    """A bot whose stated probabilities are drawn INDEPENDENTLY of its outcomes.

    Symmetric payoffs at a losing win rate, so its true expectancy is negative by construction and
    known exactly. This is the ground truth the sweep checks the admission rate against.
    """
    stated = rng.uniform(0.05, 0.95, trades)
    won = rng.random(trades) < true_win_rate
    pnl = numpy.where(won, 300.0, -300.0)
    forecasts = [
        ForecastOutcome(
            bot_identity="zero_skill",
            trading_segment=TradingSegment.CASH_INTRADAY,
            occurred_at=INSTANT - timedelta(minutes=index),
            session_date=SESSION - timedelta(days=index % 5),
            stated_probability=float(value),
            was_win=bool(outcome),
        )
        for index, (value, outcome) in enumerate(zip(stated, won, strict=True))
    ]
    outcomes = [
        RealisedTradeOutcome(
            bot_identity="zero_skill",
            trading_segment=TradingSegment.CASH_INTRADAY,
            session_date=SESSION - timedelta(days=index % 5),
            gross_rupees=Decimal(str(value)),
            costs_rupees=Decimal("0"),
        )
        for index, value in enumerate(pnl)
    ]
    return forecasts, outcomes, float(pnl.sum())


@pytest.mark.property
@pytest.mark.parametrize(("low", "high"), [(6, 40), (60, 200)])
def test_almost_no_zero_skill_bot_is_admitted_however_long_its_record(low: int, high: int) -> None:
    """The measurement that broke this engine's first version, kept as a permanent regression.

    250 bots per cell whose stated probabilities carry NO information about their outcomes, with a
    true expectancy of -Rs 90 per trade. `docs/research/261` measured the original engine admitting
    32.6% of the thin cell and 36.8% of the thick one — **worse with more data**, and 100% of the
    thick-cell admissions were lifetime loss-making.

    The bar is set here rather than after the fact: at most 2% may be admitted, and the thick cell
    may not be worse than the thin one. A gate that cannot beat this on synthetic bots whose answer
    is known by construction has not been verified by three real strategies whose answers I already
    knew.
    """
    rng = numpy.random.default_rng(11)
    assessed = 0
    admitted = 0
    for _ in range(250):
        forecasts, outcomes, _lifetime = _zero_skill_bot(rng, int(rng.integers(low, high)))
        engine = TradeQualityFloorEngine(
            StatedProbabilityCalibrator(forecasts),
            RealisedPayoffDistributionEstimator(outcomes),
            POLICY,
        )
        card = engine.assess(
            a_request(bot="zero_skill", cost="0", conviction=Decimal("0.9"))
        )
        if card.verdict is QualityVerdict.UNASSESSABLE:
            continue
        assessed += 1
        admitted += int(card.verdict is QualityVerdict.ADMIT)
    assert assessed > 200, "the sweep must actually reach a verdict"
    rate = admitted / assessed
    assert rate <= 0.02, (
        f"{admitted}/{assessed} = {rate:.1%} of zero-skill bots admitted; "
        f"docs/research/261 measured 32.6%-36.8% before the repair"
    )


@pytest.mark.property
def test_adding_losing_trades_never_carries_a_bot_from_refused_to_admitted() -> None:
    """`docs/research/261` CRITICAL-2, kept as a permanent regression.

    Losses are added at a stated value the proposal never touches, so the calibrated probability is
    left alone and only the evidence count moves. The original engine used the bot's WHOLE trade
    count as the Beta concentration, so six added Rs 500 losses turned a REFUSE into an ADMIT while
    lifetime P&L fell to -Rs 3,000.
    """
    verdicts: list[QualityVerdict] = []
    for extra in (0, 2, 4, 6, 8, 12, 16, 24):
        wins = ["300"] * 10
        losses = ["-500"] * (6 + extra)
        forecasts = forecasts_at(
            "drifting", [(0.80, True)] * 10 + [(0.20, False)] * (6 + extra)
        )
        engine = TradeQualityFloorEngine(
            StatedProbabilityCalibrator(forecasts),
            RealisedPayoffDistributionEstimator(
                outcomes("drifting", wins + losses, costs="0", notional=None)
            ),
            POLICY,
        )
        card = engine.assess(
            a_request(bot="drifting", cost="205", conviction=Decimal("0.80"))
        )
        verdicts.append(card.verdict)
    assert QualityVerdict.ADMIT not in verdicts, (
        f"a bot losing Rs 500 a trade was admitted after enough losses: {verdicts}"
    )


def forecasts_at(bot: str, rows: list[tuple[float, bool]]) -> list[ForecastOutcome]:
    """Forecasts spread over four sessions, for the sweeps above."""
    return [
        ForecastOutcome(
            bot_identity=bot,
            trading_segment=TradingSegment.CASH_INTRADAY,
            occurred_at=INSTANT - timedelta(minutes=index),
            session_date=SESSION - timedelta(days=index % 4),
            stated_probability=stated,
            was_win=won,
        )
        for index, (stated, won) in enumerate(rows)
    ]


@pytest.mark.adversarial
def test_a_bot_is_never_credited_with_another_bots_evidence() -> None:
    """`docs/research/261` CRITICAL-5. `fitted_on_trades` became the Beta concentration, and on the
    pooled path it was set to OTHER bots' trade counts — so a 4-trade bot inherited a razor-sharp
    posterior from 5,000 trades it never made, and was admitted.
    """
    others = forecasts_at("someone_else", [(0.5, index % 2 == 0) for index in range(400)])
    calibrator = StatedProbabilityCalibrator(others)
    corrected = calibrator.calibrate(
        0.9, bot_identity="ghost", trading_segment=TradingSegment.INDEX_OPTIONS
    )
    assert corrected.fitted_on_trades == 0, (
        f"a bot with no record of its own reported {corrected.fitted_on_trades} trades of evidence"
    )


@pytest.mark.adversarial
def test_padding_a_scan_with_copies_of_the_winner_cannot_lower_the_floor() -> None:
    """`docs/research/261` CRITICAL-3, kept as a permanent regression.

    The original took breadth AND dispersion from the list as submitted, so padding a 5-wide scan
    with 9,995 copies of the winner cut the floor 89% (Rs 942.83 -> Rs 105.72) while CLAIMING a
    10,000-wide scan. The module's headline property — "a wide scan cannot buy its way past this
    gate by proposing more names" — inverted, by the one party with an interest in inverting it.
    """
    honest = [Decimal(value) for value in ("0.010", "0.008", "0.006", "0.004", "0.002")]
    notional = Decimal("250000")
    baseline = selection_corrected_floor(honest, proposal_notional_rupees=notional)
    midpoint = sum(honest, Decimal(0)) / len(honest)
    for padding in (45, 495, 9995):
        # Exact duplicates were the first attack; the re-review then defeated `numpy.unique` with
        # values spaced 1e-15 apart (Rs 189.27 -> Rs 24.07) and with padding near the MEAN
        # (Rs 12.25). All three must move the floor by nothing.
        for label, filler in (
            ("exact", [honest[0]] * padding),
            ("near-duplicate", [honest[0] + Decimal(j) * Decimal("1e-15") for j in range(padding)]),
            ("near-mean", [midpoint + Decimal(j) * Decimal("1e-15") for j in range(padding)]),
        ):
            padded = selection_corrected_floor(honest + filler, proposal_notional_rupees=notional)
            # To one paise — the currency's own resolution, and a physical fact rather than a
            # tolerance chosen to make this pass. Clustering rounds onto a grid, so the arithmetic
            # can differ in the eleventh significant digit; the attack moved the floor by 87%.
            assert abs(padded.rupees - baseline.rupees) < ONE_PAISE, (
                f"{label} padding with {padding} candidates moved the floor from "
                f"{baseline.rupees} to {padded.rupees}"
            )


@pytest.mark.unit
def test_a_genuinely_wider_and_more_dispersed_scan_does_raise_the_floor() -> None:
    """The other side of the padding test, so the defence cannot be "return a constant".

    Every padding assertion above is satisfied by a floor that ignores its input entirely. A real
    199-name scan across a real spread must cost far more than a 5-name one.
    """
    notional = Decimal("250000")
    narrow = selection_corrected_floor(
        [Decimal(value) for value in ("0.010", "0.008", "0.006", "0.004", "0.002")],
        proposal_notional_rupees=notional,
    )
    wide = selection_corrected_floor(
        [Decimal(str(0.001 * step)) for step in range(1, 200)],
        proposal_notional_rupees=notional,
    )
    assert wide.rupees > narrow.rupees * 10


@pytest.mark.property
def test_a_strictly_harder_floor_can_never_be_easier_to_clear() -> None:
    """`docs/research/261` CRITICAL-4, kept as a permanent regression.

    The Monte-Carlo seed included the floor, so every distinct floor drew an independent sample:
    `P` rose at 989 of 1,999 strictly-increasing steps, and 300 proposals whose floors differed in
    the 10^-12 rupee place split 142 ADMIT / 158 REFUSE. With one sample per proposal the
    probability is that sample's survival function and the ordering holds exactly.
    """
    calibrated = CalibratedProbability(
        stated=0.7,
        calibrated=0.62,
        method=CalibrationMethod.ISOTONIC_BINNED,
        fitted_on_trades=25,
        brier_reliability=None,
        brier_resolution=None,
        brier_uncertainty=None,
    )
    wins = [Decimal("900")] * 20
    losses = [Decimal("400")] * 15
    previous: float | None = None
    for step in range(400):
        posterior = estimate_gross_expectancy_posterior(
            calibrated=calibrated,
            win_magnitudes=wins,
            loss_magnitudes=losses,
            floor_rupees=Decimal("100") + Decimal(step) * Decimal("0.2"),
        )
        assert posterior is not None
        probability = posterior.probability_exceeding_floor
        if previous is not None:
            assert probability <= previous, (
                f"a harder floor at step {step} was easier to clear: {probability} > {previous}"
            )
        previous = probability


def _bot_with_a_real_edge(
    rng: numpy.random.Generator, trades: int, *, edge_rupees: float
) -> tuple[list[ForecastOutcome], list[RealisedTradeOutcome]]:
    """A bot that genuinely makes money AND whose forecasts discriminate.

    Stated probability is informative: a higher claim really does precede a higher chance of a win.
    That is what a good bot looks like, and `docs/research/261`'s re-review measured the engine
    refusing 82-90% of exactly these.
    """
    stated = rng.uniform(0.2, 0.9, trades)
    won = rng.random(trades) < stated
    win_size = edge_rupees * 3.0
    loss_size = win_size - edge_rupees / max(float(numpy.mean(won)), 0.01)
    pnl = numpy.where(won, win_size, -abs(loss_size))
    forecasts = [
        ForecastOutcome(
            bot_identity="edged",
            trading_segment=TradingSegment.CASH_INTRADAY,
            occurred_at=INSTANT - timedelta(minutes=index),
            session_date=SESSION - timedelta(days=index % 5),
            stated_probability=float(value),
            was_win=bool(outcome),
        )
        for index, (value, outcome) in enumerate(zip(stated, won, strict=True))
    ]
    outcomes = [
        RealisedTradeOutcome(
            bot_identity="edged",
            trading_segment=TradingSegment.CASH_INTRADAY,
            session_date=SESSION - timedelta(days=index % 5),
            gross_rupees=Decimal(str(round(float(value), 2))),
            costs_rupees=Decimal("0"),
        )
        for index, value in enumerate(pnl)
    ]
    return forecasts, outcomes


@pytest.mark.property
def test_bots_with_a_real_edge_are_actually_admitted() -> None:
    """The test whose ABSENCE let a null gate pass 38 of 39 (`docs/research/261` CRITICAL-A).

    Every other sweep here is one-sided: it caps how often a BAD bot is admitted, and a hard-coded
    `REFUSE` satisfies all of them. This is the other side, and without it "the gate improved" is
    unfalsifiable.

    The bots are profitable by construction and their forecasts discriminate — a higher stated value
    really does precede a higher chance of a win. The re-review measured the engine refusing 82-90%
    of these while claiming a 30-point improvement in the false-admit rate. Both were true, and the
    second had been bought with the first.
    """
    rng = numpy.random.default_rng(23)
    assessed = 0
    admitted = 0
    for _ in range(60):
        forecasts, outcomes = _bot_with_a_real_edge(rng, 150, edge_rupees=400.0)
        engine = TradeQualityFloorEngine(
            StatedProbabilityCalibrator(forecasts),
            RealisedPayoffDistributionEstimator(outcomes),
            POLICY,
        )
        card = engine.assess(a_request(bot="edged", cost="50", conviction=Decimal("0.85")))
        if card.verdict is QualityVerdict.UNASSESSABLE:
            continue
        assessed += 1
        admitted += int(card.verdict is QualityVerdict.ADMIT)
    assert assessed > 40, "the sweep must actually reach a verdict"
    rate = admitted / assessed
    assert rate >= 0.60, (
        f"only {admitted}/{assessed} = {rate:.1%} of bots with a large real edge were admitted; "
        f"a gate that refuses good trades is as broken as one that admits bad ones"
    )


@pytest.mark.adversarial
def test_proposing_a_bigger_position_cannot_buy_admission() -> None:
    """`docs/research/261` MAJOR-C, kept as a permanent regression.

    `_size_scale` multiplied the payoff record by `notional / median_notional` with no cap and no
    market-impact term, so a Rs 300 win observed on a Rs 2.5-lakh position became a Rs 3,00,000
    expectancy on a Rs 25-crore one. Measured: qty 100 REFUSED at P=0.0043, qty 200 ADMITTED at
    P=0.9430 — on size alone. The realised-cost floor was also left in unscaled record units while
    the expectancy it gates was scaled, so the cross-check grew apart from the thing it checked.
    """
    record = ["300"] * 14 + ["-200"] * 10
    engine = TradeQualityFloorEngine(
        StatedProbabilityCalibrator(
            forecasts_at("sizer", [(0.7, True)] * 14 + [(0.3, False)] * 10)
        ),
        RealisedPayoffDistributionEstimator(
            outcomes("sizer", record, costs="10", notional="250000")
        ),
        POLICY,
    )
    probabilities = set()
    for quantity in (100, 200, 1000, 100_000):
        card = engine.assess(
            a_request(bot="sizer", cost="136", conviction=Decimal("0.7"), quantity=quantity)
        )
        assert card.gross_expectancy is not None
        probabilities.add(card.gross_expectancy.probability_exceeding_floor)
    assert len(probabilities) == 1, (
        f"the verdict moved with proposed size alone: {sorted(probabilities)}"
    )

