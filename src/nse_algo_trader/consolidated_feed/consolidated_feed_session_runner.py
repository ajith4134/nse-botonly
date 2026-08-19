"""One session's captured quotes turned into a consolidated tape, a ranking and a gate.

This is the composition root: the tape holds what each broker said, the engine knows how to
fuse and learn from it, and this walks one session's rows through both in order and persists
a summary.

**The summary is persisted rather than recomputed on request, and that is a lesson rather
than a preference.** `/microstructure` replays its tape on every page load and had grown to
31 seconds by 2026-08-12, which broke the screenshot capture (`A.83`). A session of
cross-broker quotes is tens of thousands of rows and growing daily; the dashboard reads a
stored summary, and this runner is what writes it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from nse_algo_trader.consolidated_feed.broker_reliability_store import (
    BrokerReliabilityStore,
)
from nse_algo_trader.consolidated_feed.consolidated_feed_engine import (
    BrokerRanking,
    ConsolidatedFeedEngine,
    ConsolidatedQuote,
    FeedResolution,
)
from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import CrossBrokerQuoteTape

_LOGGER = logging.getLogger(__name__)

DEFAULT_SESSION_REPORT_PATH = Path(
    "~/.nse_algo_trader/consolidated_feed_reports.sqlite3"
).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidated_feed_session (
    session_date TEXT NOT NULL PRIMARY KEY,
    quotes_consolidated INTEGER NOT NULL,
    resolved INTEGER NOT NULL,
    unresolved INTEGER NOT NULL,
    single_source INTEGER NOT NULL,
    crossed INTEGER NOT NULL,
    instruments INTEGER NOT NULL,
    brokers TEXT NOT NULL,
    median_dispersion_paise REAL NOT NULL,
    worst_instrument TEXT,
    worst_instrument_dispersion_paise REAL,
    inadmissible_instrument_sessions INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ConsolidatedFeedSessionReport:
    """What one session of cross-broker capture amounted to."""

    session_date: date
    quotes_consolidated: int
    resolved: int
    unresolved: int
    single_source: int
    crossed: int
    instruments: int
    brokers: tuple[str, ...]
    median_dispersion_paise: float
    worst_instrument: str | None
    worst_instrument_dispersion_paise: float | None
    inadmissible_instrument_sessions: int
    computed_at: datetime
    groups_failed: int = 0

    @property
    def resolved_fraction(self) -> float:
        return self.resolved / self.quotes_consolidated if self.quotes_consolidated else 0.0


class ConsolidatedFeedSessionRunner:
    """Walks a session's tape through the engine and stores the summary."""

    def __init__(
        self,
        *,
        tape: CrossBrokerQuoteTape | None = None,
        engine: ConsolidatedFeedEngine | None = None,
        report_path: Path = DEFAULT_SESSION_REPORT_PATH,
    ) -> None:
        self._tape = tape or CrossBrokerQuoteTape()
        self._engine = engine or ConsolidatedFeedEngine(BrokerReliabilityStore())
        self._report_path = report_path
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @property
    def engine(self) -> ConsolidatedFeedEngine:
        return self._engine

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._report_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def run(
        self, session_date: date, *, now: datetime | None = None
    ) -> ConsolidatedFeedSessionReport:
        """Consolidate every aligned group of the session, learn from each, and store."""
        observations = self._tape.observations(session_date=session_date)
        groups = self._engine.align(observations)
        consolidated: list[ConsolidatedQuote] = []
        failed_groups: list[str] = []
        for group in groups:
            # One malformed group must not cost a session's learning. The walk holds an
            # uncommitted transaction for tens of thousands of groups, so an exception
            # escaping here discarded ALL of it — measured by adversarial review, which
            # killed a whole run with a single naive timestamp. The boundary now refuses
            # those at construction; this is the belt to that pair of braces, and it COUNTS
            # what it caught rather than swallowing it.
            try:
                consolidated.append(self._engine.consolidate(group))
                self._engine.learn(group, session_date=session_date)
            except (ValueError, TypeError, ArithmeticError) as failure:
                failed_groups.append(
                    f"{group.trading_symbol}@{group.at.isoformat()}: "
                    f"{type(failure).__name__}: {failure}"
                )
        if failed_groups:
            _LOGGER.warning(
                "%d of %d groups could not be consolidated; first: %s",
                len(failed_groups),
                len(groups),
                failed_groups[0],
            )
        report = self._report_from(
            session_date,
            consolidated,
            now=now or datetime.now(UTC),
            failed=len(failed_groups),
        )
        # The engine has been learning into an uncommitted transaction for the whole walk;
        # this is where a session's learning becomes durable.
        self._engine.reliability_store.flush()
        self._store(report)
        return report

    def rankings(self, session_date: date) -> tuple[BrokerRanking, ...]:
        return self._engine.rank_brokers(session_date)

    def _report_from(
        self,
        session_date: date,
        consolidated: Sequence[ConsolidatedQuote],
        *,
        now: datetime,
        failed: int = 0,
    ) -> ConsolidatedFeedSessionReport:
        by_resolution = dict.fromkeys(FeedResolution, 0)
        dispersion_by_instrument: dict[str, list[float]] = {}
        brokers: set[str] = set()
        for quote in consolidated:
            by_resolution[quote.resolution] += 1
            brokers.update(quote.contributing_brokers)
            dispersion_by_instrument.setdefault(quote.trading_symbol, []).append(
                quote.dispersion_paise
            )
        all_dispersions = sorted(
            value for values in dispersion_by_instrument.values() for value in values
        )
        median_dispersion = all_dispersions[len(all_dispersions) // 2] if all_dispersions else 0.0
        worst = max(
            dispersion_by_instrument.items(),
            key=lambda item: sum(item[1]) / len(item[1]),
            default=None,
        )
        admissibility = self._engine.admissibility(session_date)
        return ConsolidatedFeedSessionReport(
            session_date=session_date,
            quotes_consolidated=len(consolidated),
            resolved=by_resolution[FeedResolution.RESOLVED],
            unresolved=by_resolution[FeedResolution.UNRESOLVED],
            single_source=by_resolution[FeedResolution.SINGLE_SOURCE],
            crossed=sum(1 for quote in consolidated if quote.is_crossed),
            instruments=len(dispersion_by_instrument),
            brokers=tuple(sorted(brokers)),
            median_dispersion_paise=float(median_dispersion),
            worst_instrument=worst[0] if worst else None,
            worst_instrument_dispersion_paise=(sum(worst[1]) / len(worst[1]) if worst else None),
            inadmissible_instrument_sessions=sum(
                1 for admissible in admissibility.values() if not admissible
            ),
            computed_at=now,
            groups_failed=failed,
        )

    def _store(self, report: ConsolidatedFeedSessionReport) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO consolidated_feed_session VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.session_date.isoformat(),
                    report.quotes_consolidated,
                    report.resolved,
                    report.unresolved,
                    report.single_source,
                    report.crossed,
                    report.instruments,
                    ",".join(report.brokers),
                    report.median_dispersion_paise,
                    report.worst_instrument,
                    report.worst_instrument_dispersion_paise,
                    report.inadmissible_instrument_sessions,
                    report.computed_at.isoformat(),
                ),
            )

    def stored_reports(self) -> tuple[ConsolidatedFeedSessionReport, ...]:
        """Every session summarised, oldest first — what `/feed` renders."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM consolidated_feed_session ORDER BY session_date"
            ).fetchall()
        return tuple(
            ConsolidatedFeedSessionReport(
                session_date=date.fromisoformat(row["session_date"]),
                quotes_consolidated=row["quotes_consolidated"],
                resolved=row["resolved"],
                unresolved=row["unresolved"],
                single_source=row["single_source"],
                crossed=row["crossed"],
                instruments=row["instruments"],
                brokers=tuple(part for part in row["brokers"].split(",") if part),
                median_dispersion_paise=row["median_dispersion_paise"],
                worst_instrument=row["worst_instrument"],
                worst_instrument_dispersion_paise=row["worst_instrument_dispersion_paise"],
                inadmissible_instrument_sessions=row["inadmissible_instrument_sessions"],
                computed_at=datetime.fromisoformat(row["computed_at"]),
            )
            for row in rows
        )
