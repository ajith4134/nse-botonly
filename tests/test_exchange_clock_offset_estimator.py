"""Tests for the offset/skew estimator, written before the estimator (`R.23(c)`).

The property tests are the load-bearing ones: an estimator of a quantity nobody can
observe directly is only checkable by generating data with a KNOWN offset and skew and
asking whether it comes back.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nse_algo_trader.clock_integrity.exchange_clock_offset_estimator import (
    ClockFitInfeasibleError,
    ClockOffsetFit,
    ExchangeClockOffsetEstimator,
    lower_convex_hull_indices,
)
from nse_algo_trader.clock_integrity.exchange_feed_delay_observation import (
    AbsentExchangeStampError,
    DelayObservationExtraction,
    ExchangeFeedDelayObservation,
    observations_from_depth_packets,
)

SESSION_START = datetime(2026, 8, 11, 3, 45, tzinfo=UTC)


def _observation(second_offset: float, lag_seconds: float) -> ExchangeFeedDelayObservation:
    """A packet whose exchange stamp is truncated to the second, as Kite delivers it."""
    generated_at = SESSION_START + timedelta(seconds=second_offset)
    return ExchangeFeedDelayObservation(
        instrument_token=738561,
        exchange_second=generated_at.replace(microsecond=0),
        received_at=generated_at + timedelta(seconds=lag_seconds),
    )


def _synthetic_session(
    *,
    offset_seconds: float,
    skew_ppm: float,
    delays: list[float],
    spacing_seconds: float = 1.0,
) -> list[ExchangeFeedDelayObservation]:
    """Packets generated with a KNOWN offset and skew, truncated to whole seconds.

    The generative model is the one the spec states: the exchange stamps truth, the host
    stamps `truth + offset + skew*t + delay`, and the exchange's stamp loses its
    sub-second part on the way through the SDK.
    """
    observations = []
    for index, delay in enumerate(delays):
        elapsed = index * spacing_seconds
        true_instant = SESSION_START + timedelta(seconds=elapsed)
        host_instant = true_instant + timedelta(
            seconds=offset_seconds + skew_ppm * 1e-6 * elapsed + delay
        )
        observations.append(
            ExchangeFeedDelayObservation(
                instrument_token=738561,
                exchange_second=true_instant.replace(microsecond=0),
                received_at=host_instant,
            )
        )
    return observations


# -- the observation boundary --------------------------------------------------------


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ExchangeFeedDelayObservation(
            instrument_token=1,
            exchange_second=datetime(2026, 8, 11, 3, 45),  # noqa: DTZ001 — the point
            received_at=datetime.now(UTC),
        )


def test_apparent_lag_is_the_difference_and_carries_the_truncation() -> None:
    observation = _observation(second_offset=0.75, lag_seconds=0.30)
    # Generated 0.75s into the second, stamped at the second boundary: the lag observed
    # is the true delay PLUS the 0.75s the exchange stamp threw away.
    assert observation.apparent_lag_seconds == pytest.approx(1.05, abs=1e-6)


def test_an_absent_exchange_stamp_is_counted_not_dropped_silently() -> None:
    packets = [
        {"instrument_token": 1, "exchange_time": None, "receipt_time": SESSION_START},
        {
            "instrument_token": 1,
            "exchange_time": SESSION_START,
            "receipt_time": SESSION_START + timedelta(milliseconds=300),
        },
    ]
    extraction = observations_from_depth_packets(packets)
    assert isinstance(extraction, DelayObservationExtraction)
    assert extraction.considered == 2
    assert extraction.absent_exchange_stamp == 1
    assert len(extraction.observations) == 1


def test_an_epoch_zero_stamp_is_treated_as_absent_not_as_1970() -> None:
    packets = [
        {
            "instrument_token": 1,
            "exchange_time": datetime(1970, 1, 1, tzinfo=UTC),
            "receipt_time": SESSION_START,
        }
    ]
    extraction = observations_from_depth_packets(packets)
    assert extraction.absent_exchange_stamp == 1
    assert extraction.observations == ()


def test_a_receipt_before_the_exchange_second_is_impossible_and_surfaced() -> None:
    packets = [
        {
            "instrument_token": 1,
            "exchange_time": SESSION_START,
            "receipt_time": SESSION_START - timedelta(seconds=2),
        }
    ]
    extraction = observations_from_depth_packets(packets)
    # Not silently kept: a negative lag means the host clock is behind the exchange by
    # more than a second, which is the very condition this engine exists to report.
    assert extraction.negative_lag == 1
    assert extraction.observations == ()


def test_extraction_from_an_unusable_batch_raises_rather_than_returning_nothing() -> None:
    with pytest.raises(AbsentExchangeStampError):
        observations_from_depth_packets(
            [{"instrument_token": 1, "exchange_time": None, "receipt_time": SESSION_START}],
            require_any=True,
        )


# -- the hull reduction --------------------------------------------------------------


def test_the_hull_keeps_only_the_points_a_lower_bound_line_can_touch() -> None:
    # A V shape: the two ends and the trough are on the lower hull, the point above is not.
    times = [0.0, 1.0, 2.0, 3.0]
    lags = [1.0, 0.4, 5.0, 1.2]
    kept = lower_convex_hull_indices(times, lags)
    assert 1 in kept  # the trough
    assert 2 not in kept  # the spike cannot bound anything from below
    assert kept[0] == 0 and kept[-1] == 3


def test_collinear_points_are_not_vertices_so_the_count_means_something() -> None:
    """Adversarial review: with `cross < 0` the sweep kept collinear points and
    `hull_vertex_count` counted them, so eleven points on one line reported eleven
    vertices. Exactness was never affected — the reported count was."""
    times = [0.0, 1.0, 2.0, 3.0]
    lags = [0.5, 0.5, 0.5, 0.5]
    assert lower_convex_hull_indices(times, lags) == [0, 3]


# -- the fit -------------------------------------------------------------------------


def test_the_fit_recovers_a_known_offset_when_the_delay_floor_is_reached() -> None:
    # Many packets, some arriving with almost no delay, so the envelope sits on the offset.
    delays = [0.002 if index % 7 == 0 else 0.35 for index in range(400)]
    observations = _synthetic_session(offset_seconds=0.25, skew_ppm=0.0, delays=delays)
    fit = ExchangeClockOffsetEstimator().fit(observations)
    # The truncation means the envelope reports offset + min(delay + residue); with
    # one-second spacing the residue is zero, so the recovery should be tight.
    assert fit.apparent_offset_seconds == pytest.approx(0.252, abs=0.01)
    assert fit.offset_upper_bound_seconds == pytest.approx(fit.apparent_offset_seconds)
    assert fit.sample_count == 400
    assert fit.hull_vertex_count <= 400


def test_a_single_enormous_outlier_cannot_move_the_fit() -> None:
    """The real tape has a 4,931-second lag in it. The envelope must ignore it."""
    delays = [0.01 if index % 5 == 0 else 0.4 for index in range(200)]
    clean = _synthetic_session(offset_seconds=0.2, skew_ppm=0.0, delays=delays)
    poisoned = list(clean)
    late = clean[100]
    poisoned[100] = ExchangeFeedDelayObservation(
        instrument_token=late.instrument_token,
        exchange_second=late.exchange_second,
        received_at=late.received_at + timedelta(seconds=4931.0),
    )
    estimator = ExchangeClockOffsetEstimator()
    assert estimator.fit(poisoned).apparent_offset_seconds == pytest.approx(
        estimator.fit(clean).apparent_offset_seconds, abs=1e-6
    )


def test_a_downward_outlier_does_move_the_fit_because_it_is_physically_informative() -> None:
    """Asymmetry is the design, not a bug: a LOW lag is evidence, a high one is noise."""
    delays = [0.4] * 200
    baseline = _synthetic_session(offset_seconds=0.2, skew_ppm=0.0, delays=delays)
    informative = list(baseline)
    early = baseline[100]
    informative[100] = ExchangeFeedDelayObservation(
        instrument_token=early.instrument_token,
        exchange_second=early.exchange_second,
        received_at=early.received_at - timedelta(seconds=0.35),
    )
    estimator = ExchangeClockOffsetEstimator()
    informative_fit = estimator.fit(informative)
    baseline_fit = estimator.fit(baseline)
    at = early.exchange_second
    assert estimator.offset_at(informative_fit, at) < estimator.offset_at(baseline_fit, at)
    # And the estimator says so about itself: one early sample mid-session pivots the line,
    # which the two halves then disagree about. That disagreement is the maturity signal —
    # a fit this sensitive must not be reported as a settled offset.
    assert informative_fit.split_half_offset_disagreement_seconds > 0.1
    assert baseline_fit.split_half_offset_disagreement_seconds == pytest.approx(0.0, abs=1e-9)


def test_the_fit_recovers_a_known_skew_over_a_long_session() -> None:
    # 3 hours at one packet per second, 40 ppm fast — 0.43s of accumulated error.
    delays = [0.003 if index % 11 == 0 else 0.30 for index in range(10_800)]
    observations = _synthetic_session(offset_seconds=0.1, skew_ppm=40.0, delays=delays)
    fit = ExchangeClockOffsetEstimator().fit(observations)
    assert fit.skew_ppm == pytest.approx(40.0, abs=3.0)


def test_offset_at_projects_the_fitted_line_forward() -> None:
    delays = [0.002 if index % 9 == 0 else 0.3 for index in range(1_200)]
    observations = _synthetic_session(offset_seconds=0.15, skew_ppm=100.0, delays=delays)
    estimator = ExchangeClockOffsetEstimator()
    fit = estimator.fit(observations)
    projected = estimator.offset_at(fit, fit.fitted_from + timedelta(hours=1))
    assert projected - fit.apparent_offset_seconds == pytest.approx(3600 * 100e-6, abs=1e-3)


def test_fewer_than_two_distinct_instants_cannot_define_a_line() -> None:
    observations = [_observation(0.0, 0.3), _observation(0.0, 0.4)]
    with pytest.raises(ClockFitInfeasibleError, match="distinct"):
        ExchangeClockOffsetEstimator().fit(observations)


def test_an_empty_batch_raises_rather_than_returning_a_zero_offset() -> None:
    with pytest.raises(ClockFitInfeasibleError):
        ExchangeClockOffsetEstimator().fit([])


def test_the_split_half_agreement_is_reported_so_maturity_can_be_judged() -> None:
    delays = [0.004 if index % 6 == 0 else 0.28 for index in range(600)]
    fit = ExchangeClockOffsetEstimator().fit(
        _synthetic_session(offset_seconds=0.3, skew_ppm=5.0, delays=delays)
    )
    # Two halves of a well-behaved session must agree far better than a second.
    assert fit.split_half_offset_disagreement_seconds < 0.05


def test_the_fit_never_sits_above_any_observation() -> None:
    """The defining constraint. If it is violated the estimate is not a lower bound."""
    delays = [0.01, 0.9, 0.05, 0.6, 0.02, 0.7, 0.3, 0.15]
    observations = _synthetic_session(offset_seconds=0.4, skew_ppm=-20.0, delays=delays)
    estimator = ExchangeClockOffsetEstimator()
    fit = estimator.fit(observations)
    for observation in observations:
        line = estimator.offset_at(fit, observation.exchange_second)
        assert line <= observation.apparent_lag_seconds + 1e-9


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    offset_seconds=st.floats(min_value=-2.0, max_value=2.0),
    skew_ppm=st.floats(min_value=-200.0, max_value=200.0),
    floor_delay=st.floats(min_value=0.001, max_value=0.05),
)
def test_property_a_known_offset_and_skew_come_back_within_the_reported_bound(
    offset_seconds: float, skew_ppm: float, floor_delay: float
) -> None:
    delays = [floor_delay if index % 8 == 0 else floor_delay + 0.4 for index in range(800)]
    observations = _synthetic_session(
        offset_seconds=offset_seconds, skew_ppm=skew_ppm, delays=delays
    )
    fit = ExchangeClockOffsetEstimator().fit(observations)
    # The envelope estimates offset + floor_delay; it can never be BELOW the true offset.
    assert fit.apparent_offset_seconds >= offset_seconds - 1e-6
    assert fit.apparent_offset_seconds == pytest.approx(offset_seconds + floor_delay, abs=0.02)
    assert fit.skew_ppm == pytest.approx(skew_ppm, abs=25.0)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    lags=st.lists(
        st.floats(min_value=0.0, max_value=3.0, allow_nan=False), min_size=3, max_size=200
    )
)
def test_property_the_fitted_line_is_always_a_lower_bound(lags: list[float]) -> None:
    observations = [
        ExchangeFeedDelayObservation(
            instrument_token=1,
            exchange_second=SESSION_START + timedelta(seconds=index),
            received_at=SESSION_START + timedelta(seconds=index + lag),
        )
        for index, lag in enumerate(lags)
    ]
    estimator = ExchangeClockOffsetEstimator()
    fit = estimator.fit(observations)
    assert all(
        estimator.offset_at(fit, observation.exchange_second)
        <= observation.apparent_lag_seconds + 1e-6
        for observation in observations
    )
    assert math.isfinite(fit.skew_ppm)


def test_the_fit_is_serialisable_to_the_store_shape() -> None:
    delays = [0.01 if index % 4 == 0 else 0.5 for index in range(120)]
    fit = ExchangeClockOffsetEstimator().fit(
        _synthetic_session(offset_seconds=0.2, skew_ppm=1.0, delays=delays)
    )
    row = fit.as_row()
    assert row["sample_count"] == 120
    assert isinstance(row["fitted_from"], str) and row["fitted_from"].endswith("+00:00")
    assert ClockOffsetFit.from_row(row) == fit


def test_rolling_windows_track_an_offset_that_moves_mid_session() -> None:
    """The online-tracking path: a step in the offset shows up as a step across windows."""
    first_half = _synthetic_session(
        offset_seconds=0.20, skew_ppm=0.0, delays=[0.004 if i % 5 == 0 else 0.3 for i in range(600)]
    )
    second_start = first_half[-1].exchange_second + timedelta(seconds=1)
    second_half = [
        ExchangeFeedDelayObservation(
            instrument_token=738561,
            exchange_second=second_start + timedelta(seconds=index),
            received_at=second_start
            + timedelta(seconds=index + 0.60 + (0.004 if index % 5 == 0 else 0.3)),
        )
        for index in range(600)
    ]
    fits = ExchangeClockOffsetEstimator().fit_rolling_windows(
        [*first_half, *second_half], window=timedelta(minutes=5), step=timedelta(minutes=5)
    )
    assert len(fits) >= 4
    offsets = [fit.apparent_offset_seconds for fit in fits]
    assert min(offsets[:2]) == pytest.approx(0.204, abs=0.02)
    assert max(offsets[-2:]) == pytest.approx(0.604, abs=0.02)


def test_rolling_windows_skip_a_stretch_that_cannot_be_fitted() -> None:
    """A quiet window yields no fit rather than an interpolated one."""
    sparse = [
        _observation(second_offset=0.0, lag_seconds=0.3),
        _observation(second_offset=1.0, lag_seconds=0.3),
        _observation(second_offset=3600.0, lag_seconds=0.3),
    ]
    fits = ExchangeClockOffsetEstimator().fit_rolling_windows(
        sparse, window=timedelta(minutes=5), step=timedelta(minutes=5)
    )
    # The first window has two instants and fits; the empty middle windows and the
    # single-point last window produce nothing.
    assert len(fits) == 1
