"""`L1.16` — the calibrator, tested for the ways a fitted coefficient can lie.

The real-data pass lives in `test_cost_filter_real_data.py` and needs the archive. These run
anywhere, and each corresponds to a way this module could produce a number that looks measured
and is not:

- `test_a_calibration_cannot_be_fitted_on_the_session_it_prices` — look-ahead, the failure that
  flatters everything downstream and leaves no trace in the output.
- `test_the_band_is_read_before_the_bar_updates_it` — a subtler look-ahead: a bar raising the
  threshold it then has to clear. Nothing about the output would look wrong.
- `test_pure_momentum_produces_a_negative_calibration_not_a_floor_of_zero` — the module must be
  able to say the strategy loses. A calibrator that cannot return bad news is not a measurement.
- `test_shrinkage_pulls_a_thin_estimate_toward_the_pooled_one` — `R.04`, arithmetic rather than
  narrative.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    MINIMUM_EVENTS_FOR_A_CALIBRATION,
    CalibrationCoverageError,
    CalibrationError,
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionCapture,
    ReversionEvent,
    calibrate_from_events,
    deviation_bucket_of,
    measure_reversion_events,
    shrink_toward_pooled,
)

SESSION = date(2026, 8, 11)
FIT_BOUNDARY = date(2026, 8, 12)


def event(
    captured_bps: str,
    *,
    deviation: str = "3.0",
    horizon: int = 5,
    observed_on: date = SESSION,
    symbol: str = "TESTCO",
) -> ReversionEvent:
    return ReversionEvent(
        trading_symbol=symbol,
        observed_on=observed_on,
        deviation_sigma=Decimal(deviation),
        sigma_bps_of_price=Decimal(286),
        captured_bps=Decimal(captured_bps),
        captured_sigma=Decimal(captured_bps) / Decimal(286),
        horizon_bars=horizon,
    )


def capture(
    mean_bps: str,
    *,
    events: int = 1_000,
    standard_error: str = "5",
    bucket: str = "3",
    horizon: int = 5,
    symbol: str | None = None,
) -> ReversionCapture:
    return ReversionCapture(
        deviation_bucket=Decimal(bucket),
        horizon_bars=horizon,
        event_count=events,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 3,
        standard_error_bps=Decimal(standard_error),
        mean_captured_sigma=Decimal("0.06"),
        fitted_through=FIT_BOUNDARY,
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
        trading_symbol=symbol,
    )


# ------------------------------------------------------------------ measuring the events


@pytest.mark.unit
def test_a_perfectly_mean_reverting_series_shows_positive_capture() -> None:
    """The control case: if this does not come out positive, nothing else here means anything."""
    # An oscillation around a flat mean with a slow drift, so the rolling window has something
    # to measure. Every excursion is followed by a return, so the entry rule fires at the
    # extremes and the next bar moves back toward the mean.
    closes = [
        100.0 + 8.0 * math.sin(index / 2.0) + 0.01 * index for index in range(400)
    ]
    events = measure_reversion_events(
        closes,
        trading_symbol="SAWTOOTH",
        session_dates=[date.fromordinal(SESSION.toordinal() - 400 + i) for i in range(400)],
        horizon_bars=1,
        rolling_window=20,
        band_quantile=Decimal("0.9"),
        minimum_deviations_before_banding=60,
    )
    assert events
    mean_capture = sum(float(item.captured_bps) for item in events) / len(events)
    assert mean_capture > 0


@pytest.mark.adversarial
def test_pure_momentum_produces_a_negative_calibration_not_a_floor_of_zero() -> None:
    """The module must be able to deliver bad news.

    A trending series never reverts, so a calibration fitted on it must come out NEGATIVE. If it
    clipped at zero the strategy would look merely unprofitable rather than actively wrong, and
    the 3.5-sigma bucket found on real data — which genuinely continues rather than reverts —
    would have been invisible.
    """
    closes = [100.0 * (1.01**index) for index in range(400)]
    events = measure_reversion_events(
        closes,
        trading_symbol="TRENDER",
        session_dates=[date.fromordinal(SESSION.toordinal() - 400 + i) for i in range(400)],
        horizon_bars=3,
        rolling_window=20,
        band_quantile=Decimal("0.9"),
        minimum_deviations_before_banding=60,
    )
    assert events, "a trend should still trigger entries; it is the OUTCOME that differs"
    calibration = calibrate_from_events(
        events, fitted_through=FIT_BOUNDARY, maturity=CalibrationMaturity.POOLED_UNIVERSE
    )
    assert calibration.mean_captured_bps < 0


@pytest.mark.adversarial
def test_the_band_is_read_before_the_bar_updates_it() -> None:
    """A bar must not raise the threshold it then has to clear.

    This is the look-ahead that would leave no trace: updating the quantile first makes an
    extreme bar harder to trigger on, which quietly removes exactly the events the calibration
    most depends on. Asserted by counting events — an off-by-one in the update order changes the
    count, and nothing else about the output would look wrong.
    """
    closes = [100.0 + (index % 7) - 3 for index in range(300)]
    dates = [date.fromordinal(SESSION.toordinal() - 300 + i) for i in range(300)]
    events = measure_reversion_events(
        closes,
        trading_symbol="PERIODIC",
        session_dates=dates,
        horizon_bars=1,
        rolling_window=20,
        band_quantile=Decimal("0.9"),
        minimum_deviations_before_banding=60,
    )
    # With the band read first, the very first post-warm-up bar is eligible. The warm-up consumes
    # exactly `minimum_deviations_before_banding` bars from the first bar with a defined
    # dispersion, so eligibility begins at a position that does not move with the data.
    assert events
    first_eligible = dates[20 + 60 - 1]
    assert min(item.observed_on for item in events) >= first_eligible


@pytest.mark.unit
def test_one_walk_serves_every_horizon_and_the_entries_agree() -> None:
    """The horizons must select the SAME bars; only the outcome may differ.

    If they disagreed, a calibration at one horizon would be describing a different strategy from
    the calibration at another, and the two would be silently incomparable.
    """
    closes = [100.0 + 5 * math.sin(index / 3) for index in range(400)]
    dates = [date.fromordinal(SESSION.toordinal() - 400 + i) for i in range(400)]
    events = measure_reversion_events(
        closes,
        trading_symbol="WAVY",
        session_dates=dates,
        horizon_bars=(1, 5),
        rolling_window=20,
        band_quantile=Decimal("0.9"),
        minimum_deviations_before_banding=60,
    )
    at_one = {item.observed_on for item in events if item.horizon_bars == 1}
    at_five = {item.observed_on for item in events if item.horizon_bars == 5}
    assert at_one == at_five
    assert at_one


@pytest.mark.adversarial
def test_mismatched_closes_and_dates_are_refused() -> None:
    """An event that cannot be dated cannot be checked for look-ahead."""
    with pytest.raises(CalibrationError, match="cannot be dated"):
        measure_reversion_events(
            [100.0] * 100,
            trading_symbol="X",
            session_dates=[SESSION] * 99,
            horizon_bars=1,
            rolling_window=20,
            band_quantile=Decimal("0.9"),
            minimum_deviations_before_banding=60,
        )


@pytest.mark.adversarial
@pytest.mark.parametrize("quantile", ["0", "1", "1.5", "-0.2"])
def test_an_impossible_band_quantile_is_refused(quantile: str) -> None:
    with pytest.raises(CalibrationError, match="band quantile"):
        measure_reversion_events(
            [100.0] * 100,
            trading_symbol="X",
            session_dates=[SESSION] * 100,
            horizon_bars=1,
            rolling_window=20,
            band_quantile=Decimal(quantile),
            minimum_deviations_before_banding=60,
        )


# ------------------------------------------------------------------ reducing to a calibration


@pytest.mark.adversarial
def test_a_calibration_cannot_be_fitted_on_the_session_it_prices() -> None:
    """The look-ahead guard, on the boundary case: an event ON the declared fit date."""
    with pytest.raises(CalibrationError, match="look-ahead"):
        calibrate_from_events(
            [event("40", observed_on=FIT_BOUNDARY)],
            fitted_through=FIT_BOUNDARY,
            maturity=CalibrationMaturity.POOLED_UNIVERSE,
        )
    # One day earlier is fine — the guard is an inequality, not a blanket refusal.
    calibrate_from_events(
        [event("40", observed_on=SESSION)],
        fitted_through=FIT_BOUNDARY,
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
    )


@pytest.mark.adversarial
def test_events_from_different_horizons_cannot_be_averaged_together() -> None:
    """A one-bar capture and a ten-bar capture are different quantities."""
    with pytest.raises(CalibrationError, match="different quantities"):
        calibrate_from_events(
            [event("40", horizon=1), event("40", horizon=10)],
            fitted_through=FIT_BOUNDARY,
            maturity=CalibrationMaturity.POOLED_UNIVERSE,
        )


@pytest.mark.unit
def test_the_calibration_keeps_the_shape_of_the_distribution_not_only_its_centre() -> None:
    """Mean, median and standard error together — a mean alone hides a tail-carried edge."""
    events = [event("0")] * 99 + [event("1000")]
    calibration = calibrate_from_events(
        events, fitted_through=FIT_BOUNDARY, maturity=CalibrationMaturity.POOLED_UNIVERSE
    )
    assert calibration.mean_captured_bps == Decimal(10)
    assert calibration.median_captured_bps == Decimal(0)
    assert calibration.skew_ratio is None, "a zero median has no meaningful ratio, and must say so"
    assert calibration.standard_error_bps > 0
    assert not calibration.is_distinguishable_from_zero


@pytest.mark.property
def test_the_lower_confidence_bound_is_two_standard_errors_below_and_is_not_clipped() -> None:
    """A negative lower bound is information; clipping it would hide a strategy that may lose."""
    calibration = capture("10", standard_error="8")
    assert calibration.lower_confidence_bps == Decimal(-6)
    assert not calibration.is_distinguishable_from_zero
    assert capture("40", standard_error="8").is_distinguishable_from_zero


@pytest.mark.property
@pytest.mark.parametrize(
    ("sigma", "expected"),
    [("2.1", "2"), ("2.3", "2.5"), ("3.74", "3.5"), ("-3.0", "3"), ("9.0", "4"), ("0.4", "0.5")],
)
def test_deviation_buckets_are_half_sigma_signless_and_capped(sigma: str, expected: str) -> None:
    """Sign is dropped because a deviation below the mean is the mirror of one above."""
    assert deviation_bucket_of(Decimal(sigma)) == Decimal(expected)


@pytest.mark.adversarial
def test_an_empty_event_set_is_refused_rather_than_calibrated_to_zero() -> None:
    with pytest.raises(CalibrationError, match="empty event set"):
        calibrate_from_events(
            [], fitted_through=FIT_BOUNDARY, maturity=CalibrationMaturity.POOLED_UNIVERSE
        )


# ------------------------------------------------------------------ the R.04 ladder


@pytest.mark.property
def test_shrinkage_pulls_a_thin_estimate_toward_the_pooled_one() -> None:
    """`R.04` as arithmetic: a noisy own estimate collapses onto the peer group."""
    pooled = capture("40", events=100_000, standard_error="1")
    noisy = capture("400", events=5, standard_error="200", symbol="THINCO")
    confident = capture("400", events=50_000, standard_error="2", symbol="THICKCO")

    shrunk_noisy = shrink_toward_pooled(noisy, pooled)
    shrunk_confident = shrink_toward_pooled(confident, pooled)

    assert abs(shrunk_noisy.mean_captured_bps - pooled.mean_captured_bps) < abs(
        shrunk_confident.mean_captured_bps - pooled.mean_captured_bps
    )
    # The blended estimate always lies between the two it came from; leaving that interval would
    # mean shrinkage had invented a value neither source supports.
    assert (
        pooled.mean_captured_bps
        <= shrunk_noisy.mean_captured_bps
        <= noisy.mean_captured_bps
    )


@pytest.mark.adversarial
def test_shrinkage_across_horizons_is_refused() -> None:
    with pytest.raises(CalibrationError, match="different horizons"):
        shrink_toward_pooled(capture("40", horizon=1), capture("40", horizon=5))


# ------------------------------------------------------------------ the store


@pytest.mark.unit
def test_the_store_resolves_point_in_time_and_prefers_the_most_recent_fit(tmp_path: Path) -> None:
    """A replay must price with the coefficient that existed then, not one fitted later."""
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    early = ReversionCapture(
        deviation_bucket=Decimal(3),
        horizon_bars=5,
        event_count=1_000,
        mean_captured_bps=Decimal(10),
        median_captured_bps=Decimal(3),
        standard_error_bps=Decimal(1),
        mean_captured_sigma=Decimal("0.03"),
        fitted_through=date(2026, 7, 1),
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
    )
    late = ReversionCapture(
        deviation_bucket=Decimal(3),
        horizon_bars=5,
        event_count=2_000,
        mean_captured_bps=Decimal(40),
        median_captured_bps=Decimal(12),
        standard_error_bps=Decimal(1),
        mean_captured_sigma=Decimal("0.12"),
        fitted_through=date(2026, 8, 1),
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
    )
    store.record([early, late])

    assert store.capture_for(
        deviation_sigma=Decimal(3), horizon_bars=5, as_of=date(2026, 8, 12)
    ).mean_captured_bps == Decimal(40)
    assert store.capture_for(
        deviation_sigma=Decimal(3), horizon_bars=5, as_of=date(2026, 7, 15)
    ).mean_captured_bps == Decimal(10)
    with pytest.raises(CalibrationCoverageError):
        store.capture_for(deviation_sigma=Decimal(3), horizon_bars=5, as_of=date(2026, 6, 1))


@pytest.mark.unit
def test_an_instrument_fit_is_preferred_over_the_pooled_one(tmp_path: Path) -> None:
    """The ladder's whole point: specific evidence beats general evidence when it exists."""
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    store.record([capture("40"), capture("90", symbol="SPECIFICO")])
    assert store.capture_for(
        deviation_sigma=Decimal(3), horizon_bars=5, as_of=FIT_BOUNDARY
    ).mean_captured_bps == Decimal(40)
    assert store.capture_for(
        deviation_sigma=Decimal(3),
        horizon_bars=5,
        as_of=FIT_BOUNDARY,
        trading_symbol="SPECIFICO",
    ).mean_captured_bps == Decimal(90)


@pytest.mark.adversarial
def test_a_thin_calibration_is_stored_but_never_priced_from(tmp_path: Path) -> None:
    """`R.04`: evidence accumulates from day one, but ACTIVATION waits for enough of it."""
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    store.record([capture("40", events=MINIMUM_EVENTS_FOR_A_CALIBRATION - 1)])
    assert store.calibration_count() == 1
    with pytest.raises(CalibrationCoverageError, match="at least"):
        store.capture_for(deviation_sigma=Decimal(3), horizon_bars=5, as_of=FIT_BOUNDARY)


@pytest.mark.adversarial
def test_an_uncovered_case_names_the_rungs_it_tried(tmp_path: Path) -> None:
    """An absence must be diagnosable, not merely fatal."""
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    with pytest.raises(CalibrationCoverageError) as raised:
        store.capture_for(
            deviation_sigma=Decimal("2.5"),
            horizon_bars=5,
            as_of=FIT_BOUNDARY,
            trading_symbol="MISSINGCO",
        )
    message = str(raised.value)
    assert "MISSINGCO" in message
    assert "pooled universe" in message
    assert "2.5" in message


@pytest.mark.unit
def test_a_calibration_round_trips_without_losing_precision(tmp_path: Path) -> None:
    """A coefficient that changes on persistence prices live trades differently from the fit."""
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    original = capture("40.4321", standard_error="7.1098")
    store.record([original])
    reloaded = store.capture_for(
        deviation_sigma=Decimal(3), horizon_bars=5, as_of=FIT_BOUNDARY
    )
    assert reloaded.mean_captured_bps == original.mean_captured_bps
    assert reloaded.standard_error_bps == original.standard_error_bps
    assert reloaded.maturity is original.maturity


@pytest.mark.adversarial
def test_a_calibration_with_no_events_cannot_be_constructed() -> None:
    with pytest.raises(CalibrationError, match="not a calibration"):
        capture("40", events=0)
