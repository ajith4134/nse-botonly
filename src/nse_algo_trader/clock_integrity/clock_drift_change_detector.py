"""Drift is a CHANGE POINT, not a threshold crossing.

A fixed "alert if the offset exceeds X ms" bound answers the wrong question twice: it stays
silent while a clock slides steadily inside the bound, and it screams when a slower network
route lifts the whole delay floor without any clock having moved. What matters is *when the
series stopped behaving as it had been*, so this module runs two online detectors over the
per-second delay-floor series and reports dated change points.

**ADWIN** (adaptive windowing) keeps two sub-windows and cuts when their means differ by
more than a Hoeffding bound — it finds a distributional shift and, because it shrinks its
window at the cut, its post-cut width localises *when* the change began. **Page-Hinkley**
accumulates one-sided deviations from the running mean and fires when the cumulative excess
exceeds a threshold — it catches a slow, persistent slide that ADWIN's variance test can sit
through. They are complementary, which is why both run.

**Neither detector's parameters are constants here (`R.03`).**
- ADWIN's `delta` is a false-positive *rate*, so it is set to `1 / n` for the series being
  scanned: about one false alarm per series, whatever its length, rather than 0.002 chosen
  because it is river's default.
- Page-Hinkley's `delta` (the drift magnitude it ignores) is the series' own median absolute
  deviation, and its `threshold` is **calibrated by permutation**: the series is shuffled to
  destroy time order — which is exactly the null hypothesis "no change point" — the detector
  statistic is run over many such shuffles, and the threshold is the upper quantile of what
  the statistic reaches under that null. A threshold derived this way means what a threshold
  should mean: this is the level chance alone does not reach.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
from river import drift

from nse_algo_trader.clock_integrity.clock_offset_observation_store import ClockDriftAlert

MINIMUM_POINTS_TO_JUDGE_A_SERIES = 20
"""Below this a "change" and the series itself are the same object: with fewer points than
this the permutation null has too few orderings to separate a shift from an arrangement."""

PERMUTATION_NULL_QUANTILE = 0.99
"""The permutation quantile a real change must beat. A stated false-alarm rate (1 in 100
shuffles), not a magic number: it IS the meaning of the threshold it produces."""


@dataclass(frozen=True, slots=True)
class DriftSeriesPoint:
    """One point of a monitored series, carrying the instant it belongs to."""

    at: datetime
    value: float


@dataclass(frozen=True, slots=True)
class CalibratedPageHinkleyParameters:
    """What the permutation null produced, kept so the calibration is auditable."""

    magnitude_delta: float
    threshold: float
    permutations: int
    null_statistic_quantile: float


class ClockDriftChangeDetector:
    """ADWIN + a permutation-calibrated Page-Hinkley over a dated series."""

    def __init__(self, *, permutations: int = 200, random_seed: int = 20260812) -> None:
        self._permutations = permutations
        self._random_seed = random_seed

    def detect(
        self,
        series: Sequence[DriftSeriesPoint],
        *,
        series_name: str,
        session_date: date,
        detected_at: datetime | None = None,
    ) -> tuple[ClockDriftAlert, ...]:
        """Every change point both detectors find, dated to when the change began."""
        if len(series) < MINIMUM_POINTS_TO_JUDGE_A_SERIES:
            return ()  # Too short to distinguish a change from the series itself.
        stamped_at = detected_at or datetime.now(UTC)
        values = [point.value for point in series]
        alerts: list[ClockDriftAlert] = []
        alerts.extend(self._adwin_alerts(series, values, series_name, session_date, stamped_at))
        alerts.extend(
            self._page_hinkley_alerts(series, values, series_name, session_date, stamped_at)
        )
        return tuple(alerts)

    def calibrate_page_hinkley(self, values: Sequence[float]) -> CalibratedPageHinkleyParameters:
        """Derive `(delta, threshold)` from the series' own permutation null."""
        magnitude_delta = _median_absolute_deviation(values)
        generator = np.random.default_rng(self._random_seed)
        shuffled = np.array(values, dtype=float)
        null_statistics = []
        for _ in range(self._permutations):
            generator.shuffle(shuffled)
            null_statistics.append(_page_hinkley_excursion(shuffled.tolist(), magnitude_delta))
        threshold = float(np.quantile(null_statistics, PERMUTATION_NULL_QUANTILE))
        return CalibratedPageHinkleyParameters(
            magnitude_delta=magnitude_delta,
            threshold=threshold,
            permutations=self._permutations,
            null_statistic_quantile=PERMUTATION_NULL_QUANTILE,
        )

    # -- detectors ---------------------------------------------------------------------

    def _adwin_alerts(
        self,
        series: Sequence[DriftSeriesPoint],
        values: Sequence[float],
        series_name: str,
        session_date: date,
        detected_at: datetime,
    ) -> list[ClockDriftAlert]:
        # river ships no annotations on its drift API; the boundary is narrow and every
        # value crossing it is a float this module produced.
        detector = drift.ADWIN(delta=1.0 / len(values))  # type: ignore[no-untyped-call]
        alerts: list[ClockDriftAlert] = []
        for index, value in enumerate(values):
            detector.update(value)  # type: ignore[no-untyped-call]
            if not detector.drift_detected:
                continue
            # After a cut ADWIN retains only the post-change window, so `index - width` is
            # its own estimate of where the change began — a date, not just a flag.
            began_index = max(0, index - int(detector.width))
            alerts.append(
                ClockDriftAlert(
                    detected_at=detected_at,
                    change_began_at=series[began_index].at,
                    detector="adwin",
                    series=series_name,
                    before_value=float(statistics.fmean(values[:began_index] or values[:1])),
                    after_value=float(detector.estimation),
                    session_date=session_date,
                )
            )
        return alerts

    def _page_hinkley_alerts(
        self,
        series: Sequence[DriftSeriesPoint],
        values: Sequence[float],
        series_name: str,
        session_date: date,
        detected_at: datetime,
    ) -> list[ClockDriftAlert]:
        """Two-sided Page-Hinkley, run by THIS module rather than by river.

        **This is the fix for the defect adversarial review found, and it is the whole
        reason river's `PageHinkley` is not used here.** The threshold is calibrated by
        permuting the series and measuring how far a statistic travels under the null. That
        is only meaningful if the statistic being MEASURED is the statistic that later
        FIRES. river's detector applies a forgetting factor (`alpha=0.9999`) and tracks both
        directions; the calibration measured a plain one-sided cumulative sum. Measured on a
        21,600-point null series with no change point in it: the calibrated threshold came
        out at 0.0354 while river's own statistic reached 0.102 up and 0.072 down, so
        **100 of 100 shuffled null series fired** against a threshold that claimed a 1-in-100
        false-alarm rate.

        Running the same function for both roles makes that class of error impossible: the
        detector below and `_page_hinkley_excursion` share their arithmetic exactly.
        """
        calibration = self.calibrate_page_hinkley(values)
        if calibration.threshold <= 0.0:
            return []  # A constant series has no null distribution to beat.
        alerts: list[ClockDriftAlert] = []
        state = _PageHinkleyState(magnitude_delta=calibration.magnitude_delta)
        for index, value in enumerate(values):
            excursion, change_index = state.update(value, index)
            if excursion < calibration.threshold:
                continue
            alerts.append(
                self._page_hinkley_alert(
                    series, values, change_index, series_name, session_date, detected_at
                )
            )
            state = _PageHinkleyState(magnitude_delta=calibration.magnitude_delta)
        return alerts

    @staticmethod
    def _page_hinkley_alert(
        series: Sequence[DriftSeriesPoint],
        values: Sequence[float],
        change_index: int,
        series_name: str,
        session_date: date,
        detected_at: datetime,
    ) -> ClockDriftAlert:
        """One alert, dated to where the statistic turned.

        `before`/`after` are guarded against an empty slice: a detector that fires ON the
        final point has nothing after the change, and `fmean([])` raises. Measured by the
        adversarial pass — a 300-point series with a ramp in its last twelve points crashed
        the whole session run.
        """
        before = values[: change_index + 1] or values[:1]
        after = values[change_index + 1 :] or values[-1:]
        return ClockDriftAlert(
            detected_at=detected_at,
            # The change point is where the cumulative statistic turned, which is the last
            # instant the series was still behaving as it had been.
            change_began_at=series[change_index].at,
            detector="page_hinkley",
            series=series_name,
            before_value=float(statistics.fmean(before)),
            after_value=float(statistics.fmean(after)),
            session_date=session_date,
        )


def _median_absolute_deviation(values: Sequence[float]) -> float:
    """The series' own noise scale — what a detector should agree to ignore."""
    if not values:
        return 0.0
    median = statistics.median(values)
    return float(statistics.median([abs(value - median) for value in values]))


@dataclass
class _PageHinkleyState:
    """The two-sided Page-Hinkley statistic, carried point by point.

    Both directions are tracked because a clock can slide either way, and both share the
    running mean so a drift in one direction cannot be hidden by the other's reset.
    """

    magnitude_delta: float
    _mean: float = 0.0
    _seen: int = 0
    _cumulative_up: float = 0.0
    _cumulative_down: float = 0.0
    _minimum_up: float = 0.0
    _maximum_down: float = 0.0
    _turn_index_up: int = 0
    _turn_index_down: int = 0

    def update(self, value: float, index: int) -> tuple[float, int]:
        """`(largest excursion so far, the index where that excursion started)`."""
        self._seen += 1
        self._mean += (value - self._mean) / self._seen
        self._cumulative_up += value - self._mean - self.magnitude_delta
        self._cumulative_down += value - self._mean + self.magnitude_delta
        if self._cumulative_up < self._minimum_up:
            self._minimum_up, self._turn_index_up = self._cumulative_up, index
        if self._cumulative_down > self._maximum_down:
            self._maximum_down, self._turn_index_down = self._cumulative_down, index
        upward = self._cumulative_up - self._minimum_up
        downward = self._maximum_down - self._cumulative_down
        if upward >= downward:
            return upward, self._turn_index_up
        return downward, self._turn_index_down


def _page_hinkley_excursion(values: Sequence[float], magnitude_delta: float) -> float:
    """The largest excursion the two-sided statistic reaches over a series.

    Run over shuffled copies this traces the null distribution the threshold comes from —
    and it is the SAME arithmetic the detector runs, which is the point.
    """
    state = _PageHinkleyState(magnitude_delta=magnitude_delta)
    largest = 0.0
    for index, value in enumerate(values):
        excursion, _ = state.update(value, index)
        largest = max(largest, excursion)
    return largest
