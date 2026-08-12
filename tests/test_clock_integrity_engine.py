"""Tests for the reference arm, the store, the detectors and the decision.

The estimator's own tests live in `test_exchange_clock_offset_estimator.py`; this file
covers everything that turns a fit into a decision, plus the two failure modes the real
data actually produced (a two-server disagreement, and a delay floor that is not a clock).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.clock_integrity.clock_drift_change_detector import (
    ClockDriftChangeDetector,
    DriftSeriesPoint,
)
from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockDriftAlert,
    ClockOffsetObservationStore,
)
from nse_algo_trader.clock_integrity.exchange_clock_offset_estimator import ClockOffsetFit
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
    ChronyTracking,
    NoReferenceConsensusError,
    ReferenceClockConsensus,
    ReferenceClockNtpSampler,
    ReferenceClockSample,
    marzullo_intersection,
    read_chrony_tracking,
)
from nse_algo_trader.clock_integrity.timestamp_trust_budget import (
    TimestampTrustBudget,
    TrustVerdict,
)

SAMPLED_AT = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
SESSION_DAY = date(2026, 8, 11)


def _sample(server: str, offset: float, round_trip: float) -> ReferenceClockSample:
    return ReferenceClockSample(
        server=server,
        offset_seconds=offset,
        round_trip_seconds=round_trip,
        stratum=3,
        root_dispersion_seconds=0.0001,
        sampled_at=SAMPLED_AT,
    )


def _consensus(
    low: float = -0.001,
    high: float = 0.001,
    *,
    falsetickers: tuple[str, ...] = (),
) -> ReferenceClockConsensus:
    return ReferenceClockConsensus(
        lower_bound_seconds=low,
        upper_bound_seconds=high,
        agreeing_servers=("a", "b"),
        falsetickers=falsetickers,
        unreachable_servers=(),
        sampled_at=SAMPLED_AT,
    )


def _fit(
    *,
    offset: float = 0.03,
    skew_ppm: float = 1.0,
    disagreement: float = 0.002,
    span_hours: float = 6.0,
) -> ClockOffsetFit:
    start = datetime(2026, 8, 11, 3, 45, tzinfo=UTC)
    return ClockOffsetFit(
        apparent_offset_seconds=offset,
        skew_ppm=skew_ppm,
        offset_upper_bound_seconds=offset,
        sample_count=20_000,
        hull_vertex_count=15,
        fitted_from=start,
        fitted_to=start + timedelta(hours=span_hours),
        split_half_offset_disagreement_seconds=disagreement,
    )


# -- Marzullo -------------------------------------------------------------------------


def test_agreeing_servers_intersect_to_the_tightest_interval() -> None:
    samples = [
        _sample("a", offset=0.010, round_trip=0.020),  # [0.000, 0.020]
        _sample("b", offset=0.012, round_trip=0.008),  # [0.008, 0.016]
        _sample("c", offset=0.011, round_trip=0.012),  # [0.005, 0.017]
    ]
    low, high, agreeing, falsetickers = marzullo_intersection(samples)
    assert (low, high) == pytest.approx((0.008, 0.016))
    assert set(agreeing) == {"a", "b", "c"}
    assert falsetickers == ()


def test_a_falseticker_is_named_and_excluded_while_the_majority_holds() -> None:
    samples = [
        _sample("a", offset=0.010, round_trip=0.004),  # [0.008, 0.012]
        _sample("b", offset=0.011, round_trip=0.004),  # [0.009, 0.013]
        _sample("liar", offset=0.900, round_trip=0.004),  # far away
    ]
    _, _, agreeing, falsetickers = marzullo_intersection(samples)
    assert set(agreeing) == {"a", "b"}
    assert falsetickers == ("liar",)


def test_two_servers_that_disagree_produce_no_consensus_at_all() -> None:
    """The real 2026-08-12 case: OCI said +0.19ms, Cloudflare said -9ms, no overlap."""
    samples = [
        _sample("169.254.169.254", offset=0.000192, round_trip=0.00085),
        _sample("time.cloudflare.com", offset=-0.009, round_trip=0.0013),
    ]
    with pytest.raises(NoReferenceConsensusError, match="majority"):
        marzullo_intersection(samples)


def test_one_responder_is_a_claim_not_a_measurement() -> None:
    with pytest.raises(NoReferenceConsensusError):
        marzullo_intersection([_sample("a", 0.001, 0.002)])


def test_the_sampler_omits_a_failing_server_rather_than_inventing_a_sample() -> None:
    class OneServerFails:
        def request(self, host: str, **_: object) -> object:
            if host == "broken":
                raise OSError("unreachable")

            class Response:
                offset, delay, stratum, root_dispersion = 0.001, 0.002, 2, 0.0001

            return Response()

    sampler = ReferenceClockNtpSampler(
        ("good-one", "broken", "good-two"), client=OneServerFails()
    )
    consensus = sampler.consensus(now=SAMPLED_AT)
    assert consensus.unreachable_servers == ("broken",)
    assert set(consensus.agreeing_servers) == {"good-one", "good-two"}


def test_chrony_is_read_from_the_real_host_or_reported_absent() -> None:
    tracking = read_chrony_tracking()
    if tracking is None:
        pytest.skip("chrony is not installed on this host")
    assert tracking.stratum >= 0
    assert math.isfinite(tracking.frequency_ppm)


def test_a_slow_chrony_frequency_keeps_its_sign() -> None:
    """`6.917 ppm slow` and `6.917 ppm fast` must not parse to the same number."""
    from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
        _signed_frequency_ppm,
    )

    assert _signed_frequency_ppm("6.917 ppm slow") == pytest.approx(-6.917)
    assert _signed_frequency_ppm("6.917 ppm fast") == pytest.approx(6.917)


def test_chrony_without_a_reference_is_not_disciplined() -> None:
    tracking = ChronyTracking(
        reference_id="00000000",
        stratum=0,
        system_time_offset_seconds=0.0,
        last_offset_seconds=0.0,
        rms_offset_seconds=0.0,
        frequency_ppm=0.0,
        skew_ppm=0.0,
    )
    assert not tracking.is_disciplined


# -- the store ------------------------------------------------------------------------


def test_the_store_round_trips_a_fit_with_its_session_and_counts(tmp_path: Path) -> None:
    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    store.record_fit(
        SESSION_DAY,
        _fit(),
        raw_row_count=11_447_680,
        absent_exchange_stamp_count=10_518,
        negative_lag_count=0,
        recorded_at=SAMPLED_AT,
    )
    stored = store.fits()
    assert len(stored) == 1
    assert stored[0].session_date == SESSION_DAY
    assert stored[0].raw_row_count == 11_447_680
    assert stored[0].absent_exchange_stamp_count == 10_518
    assert stored[0].fit == _fit()


def test_a_second_fit_for_the_same_session_is_a_new_belief_not_an_overwrite(
    tmp_path: Path,
) -> None:
    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    for index, offset in enumerate((0.03, 0.04)):
        store.record_fit(
            SESSION_DAY,
            _fit(offset=offset),
            raw_row_count=1,
            absent_exchange_stamp_count=0,
            negative_lag_count=0,
            recorded_at=SAMPLED_AT + timedelta(hours=index),
        )
    assert [round(stored.fit.apparent_offset_seconds, 3) for stored in store.fits()] == [
        0.03,
        0.04,
    ]


def test_reference_consensus_and_chrony_are_stored_together(tmp_path: Path) -> None:
    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    store.record_reference_consensus(
        _consensus(falsetickers=("liar",)),
        chrony=ChronyTracking("A9FEA9FE", 4, 5e-06, -6e-06, 3.7e-05, -6.918, 0.003),
    )
    latest = store.latest_reference_consensus()
    assert latest is not None
    assert latest.falsetickers == ("liar",)
    assert latest.midpoint_seconds == pytest.approx(0.0)


def test_alerts_are_stored_with_the_instant_the_change_began(tmp_path: Path) -> None:
    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    alert = ClockDriftAlert(
        detected_at=SAMPLED_AT,
        change_began_at=SAMPLED_AT - timedelta(minutes=30),
        detector="page_hinkley",
        series="feed_delay_floor_seconds",
        before_value=0.18,
        after_value=0.14,
        session_date=SESSION_DAY,
    )
    store.record_alerts([alert])
    assert store.alerts(session_date=SESSION_DAY) == (alert,)


# -- change detection ------------------------------------------------------------------


def _series(values: list[float]) -> list[DriftSeriesPoint]:
    start = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
    return [
        DriftSeriesPoint(at=start + timedelta(minutes=index), value=value)
        for index, value in enumerate(values)
    ]


def test_a_step_change_is_detected_and_dated_near_where_it_happened() -> None:
    series = _series([0.20] * 80 + [0.60] * 80)
    alerts = ClockDriftChangeDetector().detect(
        series, series_name="floor", session_date=SESSION_DAY
    )
    assert {alert.detector for alert in alerts} == {"adwin", "page_hinkley"}
    dated = {
        alert.detector: (alert.change_began_at - series[0].at).total_seconds() / 60
        for alert in alerts
    }
    # Measured, and the difference is why both detectors run rather than one: Page-Hinkley
    # dates the change to minute 79 of a change at minute 80, while ADWIN — whose estimate
    # is its own window width, a coarser instrument — puts it at 47. ADWIN is kept for the
    # distributional changes Page-Hinkley's one-sided sum can sit through; the DATE comes
    # from Page-Hinkley.
    assert abs(dated["page_hinkley"] - 80) <= 5
    assert dated["adwin"] < dated["page_hinkley"]


def test_a_stationary_series_produces_no_alert() -> None:
    import random

    random.seed(20260812)
    series = _series([0.25 + random.gauss(0.0, 0.004) for _ in range(300)])
    assert (
        ClockDriftChangeDetector().detect(
            series, series_name="floor", session_date=SESSION_DAY
        )
        == ()
    )


def test_a_series_too_short_to_judge_returns_nothing_rather_than_guessing() -> None:
    assert (
        ClockDriftChangeDetector().detect(
            _series([0.2] * 10), series_name="floor", session_date=SESSION_DAY
        )
        == ()
    )


def test_the_page_hinkley_threshold_is_calibrated_from_the_series_itself() -> None:
    import random

    random.seed(7)
    quiet = [0.25 + random.gauss(0.0, 0.002) for _ in range(200)]
    noisy = [0.25 + random.gauss(0.0, 0.05) for _ in range(200)]
    detector = ClockDriftChangeDetector(permutations=60)
    # A noisier series must earn a higher bar; a fixed threshold could not express that.
    assert (
        detector.calibrate_page_hinkley(noisy).threshold
        > detector.calibrate_page_hinkley(quiet).threshold
    )


def test_a_constant_series_has_no_null_distribution_and_so_no_alert() -> None:
    assert (
        ClockDriftChangeDetector(permutations=20).detect(
            _series([0.25] * 100), series_name="floor", session_date=SESSION_DAY
        )
        == ()
    )


# -- the decision ----------------------------------------------------------------------


def _store_with_history(
    tmp_path: Path, worst_cases: list[float]
) -> ClockOffsetObservationStore:
    """A store holding PAST VERDICTS, which is what the thresholds are derived from.

    Deliberately not a history of fits: the threshold and the live comparison must be the
    same quantity, and building the history from fits is exactly the mismatch adversarial
    review found (`A.83`).
    """
    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    for index, worst_case in enumerate(worst_cases):
        day_offset = len(worst_cases) - index
        store.record_fit(
            SESSION_DAY - timedelta(days=day_offset),
            _fit(offset=worst_case),
            raw_row_count=1_000,
            absent_exchange_stamp_count=0,
            negative_lag_count=0,
            recorded_at=SAMPLED_AT - timedelta(days=day_offset),
        )
        store.record_assessment(
            SESSION_DAY - timedelta(days=day_offset),
            verdict="trusted",
            worst_case_error_seconds=worst_case,
            assessed_at=SAMPLED_AT - timedelta(days=day_offset),
        )
    return store


def test_without_history_the_verdict_is_immature_not_trusted(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(ClockOffsetObservationStore(tmp_path / "clock.sqlite3"))
    assessment = budget.assess(fit=_fit(), consensus=_consensus(), alerts=(), at=SAMPLED_AT)
    assert assessment.verdict is TrustVerdict.IMMATURE
    assert "distribution" in assessment.reason


def test_with_nothing_measured_at_all_the_verdict_is_immature(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(ClockOffsetObservationStore(tmp_path / "clock.sqlite3"))
    assessment = budget.assess(fit=None, consensus=None, alerts=(), at=SAMPLED_AT)
    assert assessment.verdict is TrustVerdict.IMMATURE
    assert not assessment.is_actionable


def test_a_missing_reference_bracket_degrades_rather_than_passing(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(_store_with_history(tmp_path, [0.03, 0.031, 0.029, 0.03]))
    assessment = budget.assess(fit=_fit(), consensus=None, alerts=(), at=SAMPLED_AT)
    assert assessment.verdict is TrustVerdict.DEGRADED
    assert "no reference bracket" in assessment.reason


def test_a_usual_day_against_its_own_history_is_trusted(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    assessment = budget.assess(
        fit=_fit(offset=0.03, skew_ppm=0.5, disagreement=0.001),
        consensus=_consensus(),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.TRUSTED
    assert assessment.is_actionable


def test_an_unusual_day_against_the_same_history_is_refused(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    assessment = budget.assess(
        fit=_fit(offset=0.03, skew_ppm=90.0, disagreement=0.9),
        consensus=_consensus(low=-0.4, high=0.4),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.REFUSE


def test_an_impossible_negative_feed_floor_is_refused(tmp_path: Path) -> None:
    """Packets cannot arrive before they were stamped once the host error is removed."""
    budget = TimestampTrustBudget(_store_with_history(tmp_path, [0.03, 0.031, 0.029, 0.03]))
    assessment = budget.assess(
        fit=_fit(offset=0.01),
        consensus=_consensus(low=0.49, high=0.51),  # host is 0.5s ahead of UTC
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.REFUSE
    assert "impossible" in assessment.reason


def test_a_page_hinkley_alert_alone_refuses(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    alert = ClockDriftAlert(
        detected_at=SAMPLED_AT,
        change_began_at=SAMPLED_AT,
        detector="page_hinkley",
        series="feed_delay_floor_seconds",
        before_value=0.2,
        after_value=0.4,
        session_date=SESSION_DAY,
    )
    assessment = budget.assess(
        fit=_fit(), consensus=_consensus(), alerts=(alert,), at=SAMPLED_AT
    )
    assert assessment.verdict is TrustVerdict.REFUSE


def test_a_fit_whose_halves_disagree_is_immature_however_small_the_offset(
    tmp_path: Path,
) -> None:
    budget = TimestampTrustBudget(_store_with_history(tmp_path, [0.03, 0.03, 0.03, 0.03]))
    assessment = budget.assess(
        fit=replace(_fit(), split_half_offset_disagreement_seconds=math.inf),
        consensus=_consensus(),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.IMMATURE


def test_correction_moves_a_host_stamp_onto_the_reference_timeline() -> None:
    budget = TimestampTrustBudget(ClockOffsetObservationStore(Path("/tmp/unused-clock.db")))
    consensus = _consensus(low=0.290, high=0.310)  # host runs 0.3s fast
    corrected = budget.corrected(SAMPLED_AT, consensus=consensus)
    assert (SAMPLED_AT - corrected).total_seconds() == pytest.approx(0.3)


def test_corrected_staleness_removes_the_host_error_from_the_measurement() -> None:
    budget = TimestampTrustBudget(ClockOffsetObservationStore(Path("/tmp/unused-clock.db")))
    consensus = _consensus(low=0.290, high=0.310)
    # A packet that looks 0.8s stale on a host 0.3s fast is really 0.5s stale.
    assert budget.corrected_staleness_seconds(0.8, consensus=consensus) == pytest.approx(0.5)


def test_the_worst_case_budget_grows_with_time_since_the_fit(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(_store_with_history(tmp_path, [0.03, 0.03, 0.03, 0.03]))
    fit = _fit(skew_ppm=100.0)
    near = budget.assess(fit=fit, consensus=_consensus(), alerts=(), at=fit.fitted_to)
    far = budget.assess(
        fit=fit, consensus=_consensus(), alerts=(), at=fit.fitted_to + timedelta(days=2)
    )
    assert far.worst_case_error_seconds > near.worst_case_error_seconds


# -- defects found by adversarial review, pinned so they cannot return -------------------


def test_the_calibrated_threshold_measures_the_statistic_that_actually_fires() -> None:
    """The critical defect: calibration and detection ran different arithmetic.

    river's `PageHinkley` applies a forgetting factor and tracks both directions; the
    calibration measured a one-sided plain cumulative sum. Against a threshold claiming a
    1-in-100 false-alarm rate, 100 of 100 null series fired. Both roles now run the same
    function, and this test measures the rate rather than trusting the claim.
    """
    import numpy as np

    generator = np.random.default_rng(20260812)
    detector = ClockDriftChangeDetector(permutations=100)
    fired = 0
    for _ in range(40):
        values = (0.263 + generator.normal(0.0, 0.004, 375)).tolist()
        alerts = detector.detect(
            _series(values), series_name="floor", session_date=SESSION_DAY
        )
        fired += any(alert.detector == "page_hinkley" for alert in alerts)
    assert fired <= 4  # a 1% design rate; 4/40 is generous headroom, 100% was the defect


def test_a_change_at_the_very_end_of_a_series_does_not_crash() -> None:
    """`fmean([])` on the after-slice killed the whole session run."""
    import numpy as np

    generator = np.random.default_rng(0)
    values = (0.263 + generator.normal(0.0, 0.002, 300)).tolist()
    for index in range(288, 300):
        values[index] -= 0.02 * (index - 287)
    alerts = ClockDriftChangeDetector(permutations=60).detect(
        _series(values), series_name="floor", session_date=SESSION_DAY
    )
    assert alerts
    assert all(math.isfinite(alert.after_value) for alert in alerts)


def test_impossible_physics_is_refused_even_when_a_falseticker_was_discarded(
    tmp_path: Path,
) -> None:
    """The falseticker branch used to answer first and hide this."""
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    # Host bracketed 1.0s ahead of UTC while the feed floor is 0.263s: once the host's own
    # error is removed the packets arrive before they were stamped, which is impossible.
    assessment = budget.assess(
        fit=_fit(offset=0.263),
        consensus=_consensus(low=0.9, high=1.1, falsetickers=("liar",)),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.REFUSE
    assert "impossible" in assessment.reason


def test_a_session_cannot_raise_its_own_refuse_line(tmp_path: Path) -> None:
    """Thresholds read only assessments recorded BEFORE the one being made."""
    store = _store_with_history(tmp_path, [0.030, 0.034, 0.036])
    budget = TimestampTrustBudget(store)
    _, refuse_before = budget.derived_thresholds(before=SAMPLED_AT)
    store.record_assessment(
        SESSION_DAY,
        verdict="refuse",
        worst_case_error_seconds=50.0,
        assessed_at=SAMPLED_AT,
    )
    _, refuse_at_same_instant = budget.derived_thresholds(before=SAMPLED_AT)
    assert refuse_before == refuse_at_same_instant
    _, refuse_tomorrow = budget.derived_thresholds(before=SAMPLED_AT + timedelta(days=1))
    assert refuse_tomorrow is not None and refuse_before is not None
    assert refuse_tomorrow > refuse_before  # it counts, but only for LATER sessions


def test_the_threshold_history_is_the_same_quantity_the_verdict_compares(
    tmp_path: Path,
) -> None:
    """The second critical defect: two different formulas, so REFUSE was unreachable."""
    store = _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031])
    budget = TimestampTrustBudget(store)
    degraded_above, refuse_above = budget.derived_thresholds(before=SAMPLED_AT)
    assert degraded_above is not None and refuse_above is not None
    # The live worst case for an ordinary day must land in the same range as the history,
    # not two orders of magnitude below it.
    ordinary = budget.assess(
        fit=_fit(offset=0.03, skew_ppm=1.0, disagreement=0.002),
        consensus=_consensus(),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert 0.1 < ordinary.worst_case_error_seconds / refuse_above < 10.0


def test_a_drift_series_too_short_to_judge_is_immature_not_trusted(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    assessment = budget.assess(
        fit=_fit(),
        consensus=_consensus(),
        alerts=(),
        at=SAMPLED_AT,
        drift_series_points=19,
    )
    assert assessment.verdict is TrustVerdict.IMMATURE


def test_a_physically_impossible_skew_is_not_reported_as_a_clock(tmp_path: Path) -> None:
    budget = TimestampTrustBudget(
        _store_with_history(tmp_path, [0.030, 0.034, 0.036, 0.031, 0.033])
    )
    assessment = budget.assess(
        fit=_fit(skew_ppm=1_000_000.0),
        consensus=_consensus(),
        alerts=(),
        at=SAMPLED_AT,
    )
    assert assessment.verdict is TrustVerdict.IMMATURE
    assert "crystal oscillator" in assessment.reason


def test_a_stale_reference_bracket_is_not_applied_as_todays_correction(
    tmp_path: Path,
) -> None:
    """A month-old bracket used to be handed to the recorder as if it were current."""
    from nse_algo_trader.clock_integrity.timestamp_trust_budget import (
        measured_host_clock_error,
    )

    store = ClockOffsetObservationStore(tmp_path / "clock.sqlite3")
    store.record_reference_consensus(_consensus(low=0.29, high=0.31))
    fresh = measured_host_clock_error(store, now=SAMPLED_AT + timedelta(hours=1))
    stale = measured_host_clock_error(store, now=SAMPLED_AT + timedelta(days=30))
    assert fresh.is_measured and fresh.seconds == pytest.approx(0.3)
    assert not stale.is_measured
    assert stale.seconds == 0.0
    assert "old" in stale.reason


def test_an_unmeasured_correction_is_distinguishable_from_a_measured_zero(
    tmp_path: Path,
) -> None:
    from nse_algo_trader.clock_integrity.timestamp_trust_budget import (
        measured_host_clock_error,
    )

    empty = ClockOffsetObservationStore(tmp_path / "empty.sqlite3")
    unmeasured = measured_host_clock_error(empty, now=SAMPLED_AT)
    assert unmeasured.seconds == 0.0
    assert not unmeasured.is_measured  # the whole point: 0.0 alone said nothing
