"""The carried state: every fit, every reference sample, every alert, kept across days.

A single session's fit cannot tell a drifting clock from a slow route — both tilt the delay
floor. The distinction only appears across sessions, by comparing the feed-derived skew
with what the reference clock said on the same day. That comparison is the reason this
store exists: the engine's memory is the measurement.

Three tables, and the split is deliberate.

- `clock_offset_fit` — one row per session per fit window, the envelope's answer.
- `reference_clock_consensus` — one row per NTP sampling round, with the Marzullo bounds,
  the falsetickers named, and chrony's own account of itself alongside.
- `clock_drift_alert` — one row per change point the detectors emitted, dated to WHEN the
  change began rather than when it was noticed.

Nothing is overwritten. A corrected estimate is a new row with a later `recorded_at`, so
the history of what the engine believed remains readable — the same belief-time discipline
the rule store applies to exchange rules (`A.80`), for the same reason: a replay must be
able to ask what was known then, not only what is known now.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from nse_algo_trader.clock_integrity.exchange_clock_offset_estimator import ClockOffsetFit
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
    ChronyTracking,
    ReferenceClockConsensus,
)

DEFAULT_CLOCK_INTEGRITY_PATH = Path("~/.nse_algo_trader/clock_integrity.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_offset_fit (
    session_date TEXT NOT NULL,
    fitted_from TEXT NOT NULL,
    fitted_to TEXT NOT NULL,
    apparent_offset_seconds REAL NOT NULL,
    offset_upper_bound_seconds REAL NOT NULL,
    skew_ppm REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    hull_vertex_count INTEGER NOT NULL,
    split_half_offset_disagreement_seconds REAL NOT NULL,
    raw_row_count INTEGER NOT NULL,
    absent_exchange_stamp_count INTEGER NOT NULL,
    negative_lag_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (session_date, fitted_from, fitted_to, recorded_at)
);
CREATE INDEX IF NOT EXISTS clock_offset_fit_by_session
    ON clock_offset_fit (session_date, recorded_at);

CREATE TABLE IF NOT EXISTS reference_clock_consensus (
    sampled_at TEXT NOT NULL PRIMARY KEY,
    lower_bound_seconds REAL NOT NULL,
    upper_bound_seconds REAL NOT NULL,
    agreeing_servers TEXT NOT NULL,
    falsetickers TEXT NOT NULL,
    unreachable_servers TEXT NOT NULL,
    chrony_reference_id TEXT,
    chrony_stratum INTEGER,
    chrony_frequency_ppm REAL,
    chrony_rms_offset_seconds REAL
);

CREATE TABLE IF NOT EXISTS timestamp_trust_assessment (
    assessed_at TEXT NOT NULL PRIMARY KEY,
    session_date TEXT NOT NULL,
    verdict TEXT NOT NULL,
    worst_case_error_seconds REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS timestamp_trust_assessment_by_session
    ON timestamp_trust_assessment (session_date, assessed_at);

CREATE TABLE IF NOT EXISTS clock_drift_alert (
    detected_at TEXT NOT NULL,
    change_began_at TEXT NOT NULL,
    detector TEXT NOT NULL,
    series TEXT NOT NULL,
    before_value REAL NOT NULL,
    after_value REAL NOT NULL,
    session_date TEXT NOT NULL,
    PRIMARY KEY (detected_at, detector, series)
);
CREATE INDEX IF NOT EXISTS clock_drift_alert_by_session
    ON clock_drift_alert (session_date, change_began_at);
"""


@dataclass(frozen=True, slots=True)
class StoredClockOffsetFit:
    """A fit as it came back out, with the session it belongs to and when it was believed."""

    session_date: date
    fit: ClockOffsetFit
    raw_row_count: int
    absent_exchange_stamp_count: int
    negative_lag_count: int
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ClockDriftAlert:
    """A dated change point. `change_began_at` is the detector's estimate, not the wall clock."""

    detected_at: datetime
    change_began_at: datetime
    detector: str
    series: str
    before_value: float
    after_value: float
    session_date: date


class ClockOffsetObservationStore:
    """SQLite-backed history of what this host knew about its own clock, and when."""

    def __init__(self, database_path: Path = DEFAULT_CLOCK_INTEGRITY_PATH) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    # -- fits --------------------------------------------------------------------------

    def record_fit(
        self,
        session_date: date,
        fit: ClockOffsetFit,
        *,
        raw_row_count: int,
        absent_exchange_stamp_count: int,
        negative_lag_count: int,
        recorded_at: datetime | None = None,
    ) -> None:
        stamped_at = recorded_at or datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO clock_offset_fit VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_date.isoformat(),
                    fit.fitted_from.isoformat(),
                    fit.fitted_to.isoformat(),
                    fit.apparent_offset_seconds,
                    fit.offset_upper_bound_seconds,
                    fit.skew_ppm,
                    fit.sample_count,
                    fit.hull_vertex_count,
                    fit.split_half_offset_disagreement_seconds,
                    raw_row_count,
                    absent_exchange_stamp_count,
                    negative_lag_count,
                    stamped_at.isoformat(),
                ),
            )

    def fits(self, *, session_date: date | None = None) -> tuple[StoredClockOffsetFit, ...]:
        """Every fit, newest last. Filtered to one session when asked."""
        query = "SELECT * FROM clock_offset_fit"
        parameters: tuple[str, ...] = ()
        if session_date is not None:
            query += " WHERE session_date = ?"
            parameters = (session_date.isoformat(),)
        query += " ORDER BY session_date, recorded_at"
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._stored_fit_from_row(row) for row in rows)

    def latest_fit(self) -> StoredClockOffsetFit | None:
        fits = self.fits()
        return fits[-1] if fits else None

    @staticmethod
    def _stored_fit_from_row(row: sqlite3.Row) -> StoredClockOffsetFit:
        return StoredClockOffsetFit(
            session_date=date.fromisoformat(row["session_date"]),
            fit=ClockOffsetFit(
                apparent_offset_seconds=row["apparent_offset_seconds"],
                skew_ppm=row["skew_ppm"],
                offset_upper_bound_seconds=row["offset_upper_bound_seconds"],
                sample_count=row["sample_count"],
                hull_vertex_count=row["hull_vertex_count"],
                fitted_from=datetime.fromisoformat(row["fitted_from"]),
                fitted_to=datetime.fromisoformat(row["fitted_to"]),
                split_half_offset_disagreement_seconds=row[
                    "split_half_offset_disagreement_seconds"
                ],
            ),
            raw_row_count=row["raw_row_count"],
            absent_exchange_stamp_count=row["absent_exchange_stamp_count"],
            negative_lag_count=row["negative_lag_count"],
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
        )

    # -- reference samples ---------------------------------------------------------------

    def record_reference_consensus(
        self, consensus: ReferenceClockConsensus, *, chrony: ChronyTracking | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO reference_clock_consensus VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    consensus.sampled_at.isoformat(),
                    consensus.lower_bound_seconds,
                    consensus.upper_bound_seconds,
                    ",".join(consensus.agreeing_servers),
                    ",".join(consensus.falsetickers),
                    ",".join(consensus.unreachable_servers),
                    chrony.reference_id if chrony else None,
                    chrony.stratum if chrony else None,
                    chrony.frequency_ppm if chrony else None,
                    chrony.rms_offset_seconds if chrony else None,
                ),
            )

    def reference_consensuses(self, *, limit: int = 200) -> tuple[ReferenceClockConsensus, ...]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM reference_clock_consensus ORDER BY sampled_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            ReferenceClockConsensus(
                lower_bound_seconds=row["lower_bound_seconds"],
                upper_bound_seconds=row["upper_bound_seconds"],
                agreeing_servers=_split(row["agreeing_servers"]),
                falsetickers=_split(row["falsetickers"]),
                unreachable_servers=_split(row["unreachable_servers"]),
                sampled_at=datetime.fromisoformat(row["sampled_at"]),
            )
            for row in reversed(rows)
        )

    def latest_reference_consensus(self) -> ReferenceClockConsensus | None:
        consensuses = self.reference_consensuses(limit=1)
        return consensuses[-1] if consensuses else None

    # -- alerts ---------------------------------------------------------------------------

    def record_alerts(self, alerts: Sequence[ClockDriftAlert]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO clock_drift_alert VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        alert.detected_at.isoformat(),
                        alert.change_began_at.isoformat(),
                        alert.detector,
                        alert.series,
                        alert.before_value,
                        alert.after_value,
                        alert.session_date.isoformat(),
                    )
                    for alert in alerts
                ],
            )

    def alerts(self, *, session_date: date | None = None) -> tuple[ClockDriftAlert, ...]:
        query = "SELECT * FROM clock_drift_alert"
        parameters: tuple[str, ...] = ()
        if session_date is not None:
            query += " WHERE session_date = ?"
            parameters = (session_date.isoformat(),)
        query += " ORDER BY change_began_at"
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            ClockDriftAlert(
                detected_at=datetime.fromisoformat(row["detected_at"]),
                change_began_at=datetime.fromisoformat(row["change_began_at"]),
                detector=row["detector"],
                series=row["series"],
                before_value=row["before_value"],
                after_value=row["after_value"],
                session_date=date.fromisoformat(row["session_date"]),
            )
            for row in rows
        )

    # -- assessments ------------------------------------------------------------------------

    def record_assessment(
        self,
        session_date: date,
        *,
        verdict: str,
        worst_case_error_seconds: float,
        assessed_at: datetime,
    ) -> None:
        """One verdict as it was reached, so later thresholds have a like-for-like history.

        Adversarial review found the alternative broken: thresholds were being derived from
        a per-session formula over the FITS while the live comparison used a different
        formula over fit + NTP bracket. The two shared one term, and the REFUSE line came
        out 15x above anything the live path could produce. Storing the number that was
        actually judged removes the possibility of the two drifting apart again.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO timestamp_trust_assessment VALUES (?,?,?,?)",
                (
                    assessed_at.isoformat(),
                    session_date.isoformat(),
                    verdict,
                    worst_case_error_seconds,
                ),
            )

    def worst_case_history_seconds(self, *, before: datetime | None = None) -> tuple[float, ...]:
        """Past worst-case errors, oldest first, STRICTLY before `before`.

        The exclusion matters: recording today's fit and then deriving today's threshold
        from a history containing it lets a bad session raise its own bar. Measured by
        adversarial review — a 50-second-offset session moved its own REFUSE line from
        0.46 s to 48.7 s.
        """
        query = "SELECT worst_case_error_seconds FROM timestamp_trust_assessment"
        parameters: tuple[str, ...] = ()
        if before is not None:
            query += " WHERE assessed_at < ?"
            parameters = (before.isoformat(),)
        query += " ORDER BY assessed_at"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(float(row[0]) for row in rows)

    # -- derived history ------------------------------------------------------------------

    def skew_history_ppm(self) -> tuple[float, ...]:
        """Every session's fitted skew, oldest first — the series thresholds are derived from."""
        return tuple(stored.fit.skew_ppm for stored in self.fits())

    def offset_history_seconds(self) -> tuple[float, ...]:
        return tuple(stored.fit.apparent_offset_seconds for stored in self.fits())


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split(",") if part)
