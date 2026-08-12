"""One session, end to end: sample, fit, detect, record, decide.

This is the composition root the daily runner calls. It exists so that the ordering
constraints between the parts live in ONE readable place rather than being re-derived by
every caller: the reference consensus must be sampled before the feed fit is interpreted
(otherwise the host's own error cannot be removed from the feed floor), and both must be
recorded before the trust budget derives its thresholds (which read the store's history).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from nse_algo_trader.clock_integrity.clock_drift_change_detector import (
    ClockDriftChangeDetector,
    DriftSeriesPoint,
)
from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockDriftAlert,
    ClockOffsetObservationStore,
)
from nse_algo_trader.clock_integrity.depth_tape_delay_sampler import (
    DEFAULT_DEPTH_TAPE_ROOT,
    DepthTapeUnavailableError,
    available_session_dates,
    sample_session_delays,
)
from nse_algo_trader.clock_integrity.exchange_clock_offset_estimator import (
    ClockOffsetFit,
    ExchangeClockOffsetEstimator,
)
from nse_algo_trader.clock_integrity.exchange_feed_delay_observation import (
    ExchangeFeedDelayObservation,
)
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
    ChronyTracking,
    NoReferenceConsensusError,
    ReferenceClockConsensus,
    ReferenceClockNtpSampler,
    read_chrony_tracking,
)
from nse_algo_trader.clock_integrity.timestamp_trust_budget import (
    TimestampTrustAssessment,
    TimestampTrustBudget,
)
from nse_algo_trader.replay_session_clock import session_for


@dataclass(frozen=True, slots=True)
class ClockIntegritySessionResult:
    """Everything one session produced, including what could not be produced."""

    session_date: date
    fit: ClockOffsetFit | None
    consensus: ReferenceClockConsensus | None
    chrony: ChronyTracking | None
    alerts: tuple[ClockDriftAlert, ...]
    assessment: TimestampTrustAssessment
    raw_row_count: int
    absent_exchange_stamp_count: int
    negative_lag_count: int
    unavailable_reason: str | None
    delay_floor_points: int | None = None

    @property
    def skew_disagreement_with_chrony_ppm(self) -> float | None:
        """Feed-derived skew minus chrony's own frequency correction.

        The single most informative number the engine produces: chrony reports what it is
        already correcting for, so a feed skew that matches it is this host's clock, and one
        that does not is the network or the exchange.
        """
        if self.fit is None or self.chrony is None:
            return None
        return self.fit.skew_ppm - self.chrony.frequency_ppm


class ClockIntegritySessionRunner:
    """Runs the whole clock-integrity pass for one session date."""

    def __init__(
        self,
        *,
        store: ClockOffsetObservationStore | None = None,
        sampler: ReferenceClockNtpSampler | None = None,
        estimator: ExchangeClockOffsetEstimator | None = None,
        detector: ClockDriftChangeDetector | None = None,
        tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT,
    ) -> None:
        self._store = store or ClockOffsetObservationStore()
        self._sampler = sampler or ReferenceClockNtpSampler()
        self._estimator = estimator or ExchangeClockOffsetEstimator()
        self._detector = detector or ClockDriftChangeDetector()
        self._tape_root = tape_root

    @property
    def store(self) -> ClockOffsetObservationStore:
        return self._store

    def run(
        self, session_date: date, *, now: datetime | None = None
    ) -> ClockIntegritySessionResult:
        """Sample the reference, fit the feed, detect changes, record, and decide."""
        at = now or datetime.now(UTC)
        consensus, chrony = self._reference_arm(at)
        fit: ClockOffsetFit | None = None
        alerts: tuple[ClockDriftAlert, ...] = ()
        raw_rows = absent = negative = 0
        floor_points: int | None = None
        unavailable_reason: str | None = None

        try:
            sample = sample_session_delays(session_date, tape_root=self._tape_root)
        except DepthTapeUnavailableError as error:
            unavailable_reason = str(error)
        else:
            raw_rows = sample.raw_row_count
            absent = sample.extraction.absent_exchange_stamp
            negative = sample.extraction.negative_lag
            fit = self._estimator.fit(sample.observations)
            self._store.record_fit(
                session_date,
                fit,
                raw_row_count=raw_rows,
                absent_exchange_stamp_count=absent,
                negative_lag_count=negative,
                recorded_at=at,
            )
            floor_series = self._delay_floor_series(sample.observations, fit)
            floor_points = len(floor_series)
            alerts = self._detector.detect(
                floor_series,
                series_name="feed_delay_floor_seconds",
                session_date=session_date,
                detected_at=at,
            )
            if alerts:
                self._store.record_alerts(alerts)

        assessment = TimestampTrustBudget(self._store).assess(
            fit=fit,
            consensus=consensus,
            alerts=alerts,
            at=at,
            drift_series_points=floor_points,
        )
        # Recorded AFTER the verdict is reached and read only by later runs: the threshold
        # history must not contain the assessment it is judging (`A.83`).
        self._store.record_assessment(
            session_date,
            verdict=assessment.verdict.value,
            worst_case_error_seconds=assessment.worst_case_error_seconds,
            assessed_at=at,
        )
        return ClockIntegritySessionResult(
            session_date=session_date,
            fit=fit,
            consensus=consensus,
            chrony=chrony,
            alerts=alerts,
            assessment=assessment,
            raw_row_count=raw_rows,
            absent_exchange_stamp_count=absent,
            negative_lag_count=negative,
            unavailable_reason=unavailable_reason,
            delay_floor_points=floor_points,
        )

    def run_latest_session(
        self, *, now: datetime | None = None
    ) -> ClockIntegritySessionResult | None:
        """The most recent session the tape holds, or `None` when it holds none."""
        sessions = available_session_dates(tape_root=self._tape_root)
        return self.run(sessions[-1], now=now) if sessions else None

    def _delay_floor_series(
        self, observations: Sequence[ExchangeFeedDelayObservation], fit: ClockOffsetFit
    ) -> list[DriftSeriesPoint]:
        """Per-MINUTE minimum residual from the fitted line — the floor, not the cloud.

        Measured, and the first real-data run is why this exists: feeding raw per-second
        lags to ADWIN produced nine "drift" alerts on 2026-08-11, every one of them the
        session-open transient in which snapshot packets carry exchange stamps thousands of
        seconds old. Those are queueing and replay artefacts; a clock cannot move a
        timestamp by 2,984 seconds and back inside a minute.

        A clock error shifts the whole FLOOR of the delay cloud, while congestion only lifts
        its upper tail — so the floor is the series a drift detector should watch, and the
        residual (observation minus the fitted envelope) removes the skew the fit already
        explained, leaving what the fit did not.
        """
        session = session_for(fit.fitted_from.date())
        floor_by_minute: dict[datetime, tuple[datetime, float]] = {}
        for observation in observations:
            if not (session.opens_at <= observation.exchange_second <= session.closes_at):
                continue  # Outside the session the feed is not measuring a path at all.
            minute = observation.received_at.replace(second=0, microsecond=0)
            if observation.exchange_second < minute:
                # **A minute is only measurable by a packet GENERATED in it.** Kite stamps
                # each packet with its instrument's last trade, so an illiquid name arriving
                # now carries an hours-old stamp and reports its own illiquidity as delay.
                # Measured on 2026-08-11: the first 35 minutes of the tape have per-minute
                # floors near 3,000 s for exactly this reason, and feeding them to ADWIN
                # produced "drift" alerts about a clock that had not moved. A minute with no
                # freshly-stamped packet yields no measurement rather than a stale one.
                continue
            residual = observation.apparent_lag_seconds - self._estimator.offset_at(
                fit, observation.exchange_second
            )
            current = floor_by_minute.get(minute)
            if current is None or residual < current[1]:
                floor_by_minute[minute] = (observation.exchange_second, residual)
        return [
            DriftSeriesPoint(at=instant, value=residual)
            for minute, (instant, residual) in sorted(floor_by_minute.items())
        ]

    def _reference_arm(
        self, at: datetime
    ) -> tuple[ReferenceClockConsensus | None, ChronyTracking | None]:
        """NTP consensus + chrony's self-report. Either may be absent; neither is invented."""
        chrony = read_chrony_tracking()
        try:
            consensus = self._sampler.consensus(now=at)
        except NoReferenceConsensusError:
            return None, chrony
        self._store.record_reference_consensus(consensus, chrony=chrony)
        return consensus, chrony
