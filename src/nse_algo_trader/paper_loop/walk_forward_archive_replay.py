"""Walk the six bots forward through the archive while the exchange is closed (`L10.02`, `A.146`).

**What this replaces.** The continuous loop's closed-market branch read, in full:
*"market closed — the loop is alive and deliberately deciding nothing"*. For roughly eighteen and a
half hours of every weekday and all of every weekend, the six bots did nothing at all, while the
archive held sessions none of them had ever decided over.

**The operator's decision (`A.146`), and why the alternative was rejected.** The loop walks FORWARD
through the archive and never replays a session twice. Re-replaying the most recent closed session
was considered and rejected on the evidence: it accrues no new closed trades after its first pass,
so the maturity ladder stops moving while the loop looks busy — `O.134`'s exact shape, an iteration
record that looked perfect while the loop learned nothing. A persisted cursor is what makes the
difference, so the cursor is the engine's state rather than a convenience.

**What it does NOT own.** It does not decide, size, price or accrue anything.
`SegmentBotPaperSession` does all of that — warming up over prior sessions, proposing at the close
of `T`, exiting at the close of `T+1`, pricing both legs through `NseTransactionCostEngine` and
accruing to
`PaperTrackRecordStore` under each bot's own identity. That engine was built for `6.4d` and then
imported by nothing (`R.06`); this is the consumer it was missing, not a second implementation.

**Provenance without a schema change.** A trade accrued from a replayed session and one accrued
live are told apart by their `session_date`: this store knows exactly which sessions were replayed,
so the surface joins rather than guesses. Adding a "was this live" column to the track record would
have meant inventing the value for every row already in it.

**Failure is recorded and stepped over, never raised.** A single malformed archived session that
threw would otherwise sit at the head of the queue for ever and block every session behind it. The
outcome carries the failure text and the cursor still advances.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

DEFAULT_CURSOR_DATABASE: Final = (
    Path.home() / ".nse_algo_trader" / "walk_forward_replay.sqlite3"
)


class WalkForwardReplayError(Exception):
    """The replay record is inconsistent — a session recorded twice, or an unreadable store."""


class ArchivedSessionRunner(Protocol):
    """The one thing this driver asks of `SegmentBotPaperSession`, named so a test can answer it."""

    def run(self, entry_session: date) -> Any: ...


@dataclass(frozen=True)
class ReplayedSessionOutcome:
    """What one archived session produced when the six bots decided over it."""

    session: date
    trades_accrued: int
    detail: str
    failure: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure is None

    def describe(self) -> str:
        if self.failure is not None:
            return f"{self.session} FAILED and was stepped over: {self.failure}"
        return f"{self.session} replayed · {self.trades_accrued} trade(s) accrued · {self.detail}"


@dataclass(frozen=True)
class WalkForwardProgress:
    """How much of the archive has been walked, and what is next.

    Reported rather than inferred, because "the loop is busy" and "the loop is making progress" are
    different claims and only the second one moves a maturity rung.
    """

    replayed: int
    remaining: int
    next_session: date | None
    last_replayed: date | None

    def describe(self) -> str:
        if self.next_session is None:
            return f"archive walked to the end · {self.replayed} session(s) replayed"
        return (
            f"{self.replayed} session(s) replayed · {self.remaining} remaining · "
            f"next {self.next_session.isoformat()}"
        )


class WalkForwardReplayCursorStore:
    """Append-only record of every archived session replayed, and the cursor implied by it.

    The cursor is DERIVED from the record rather than stored beside it. A separate cursor column can
    disagree with the rows it summarises, and when it does the loop either repeats work or skips it
    silently — both of which look like success.
    """

    def __init__(self, path: Path = DEFAULT_CURSOR_DATABASE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS replayed_archive_session (
                session_date TEXT PRIMARY KEY,
                replayed_at TEXT NOT NULL,
                trades_accrued INTEGER NOT NULL,
                failure TEXT
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def remember(
        self, session: date, *, trades_accrued: int, at: datetime, failure: str | None
    ) -> None:
        try:
            self._connection.execute(
                "INSERT INTO replayed_archive_session "
                "(session_date, replayed_at, trades_accrued, failure) VALUES (?, ?, ?, ?)",
                (session.isoformat(), at.isoformat(), trades_accrued, failure),
            )
        except sqlite3.IntegrityError as clash:
            raise WalkForwardReplayError(
                f"{session.isoformat()} is already recorded as replayed; recording it twice would "
                f"double-count its trades against the ladder that reads them"
            ) from clash
        self._connection.commit()

    def replayed_sessions(self) -> tuple[date, ...]:
        rows = self._connection.execute(
            "SELECT session_date FROM replayed_archive_session ORDER BY session_date"
        ).fetchall()
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def last_replayed_session(self) -> date | None:
        row = self._connection.execute(
            "SELECT MAX(session_date) FROM replayed_archive_session"
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def total_trades_accrued(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(trades_accrued), 0) FROM replayed_archive_session"
        ).fetchone()
        return int(row[0]) if row else 0


@dataclass
class WalkForwardArchiveReplay:
    """Replay the next unreplayed archived session, one call at a time.

    One call is one session on purpose. The continuous loop ticks on a cadence and must stay
    responsive to the open; a driver that replayed the whole archive in one call would hold the loop
    for as long as the archive is long and miss the bell.
    """

    session_runner: ArchivedSessionRunner
    replayable_sessions: Callable[[date], Sequence[date]]
    """Every session the archive can support an entry on, as of a date. Injected because what is
    replayable is a question about the STORES, and this engine is about the order they are walked
    in (`R.J`: the seam is what lets a hermetic harness drive the identical code)."""

    cursor_store: WalkForwardReplayCursorStore = field(
        default_factory=WalkForwardReplayCursorStore
    )

    def next_session_to_replay(self, as_of: date) -> date | None:
        """The earliest replayable session that has not been replayed, strictly before `as_of`.

        Strictly before, because entering at the close of a session that has not happened would mark
        the position out against an exit nobody has observed.
        """
        already = set(self.cursor_store.replayed_sessions())
        for session in sorted(self.replayable_sessions(as_of)):
            if session >= as_of:
                break
            if session not in already:
                return session
        return None

    def replay_next(self, as_of: date) -> ReplayedSessionOutcome | None:
        """Replay one session, or answer `None` when the archive has been walked to its end."""
        session = self.next_session_to_replay(as_of)
        if session is None:
            return None

        failure: str | None = None
        accrued = 0
        detail = ""
        try:
            report = self.session_runner.run(session)
        except Exception as thrown:  # noqa: BLE001 — one bad session must not stop the walk
            failure = f"{type(thrown).__name__}: {thrown}"
        else:
            accrued = int(getattr(report, "accrued", 0) or 0)
            describe = getattr(report, "describe", None)
            detail = describe() if callable(describe) else str(report)

        self.cursor_store.remember(
            session, trades_accrued=accrued, at=datetime.now(UTC), failure=failure
        )
        return ReplayedSessionOutcome(
            session=session, trades_accrued=accrued, detail=detail, failure=failure
        )

    def progress(self, as_of: date) -> WalkForwardProgress:
        already = set(self.cursor_store.replayed_sessions())
        available = [session for session in self.replayable_sessions(as_of) if session < as_of]
        remaining = [session for session in sorted(available) if session not in already]
        return WalkForwardProgress(
            replayed=len(already),
            remaining=len(remaining),
            next_session=remaining[0] if remaining else None,
            last_replayed=self.cursor_store.last_replayed_session(),
        )
