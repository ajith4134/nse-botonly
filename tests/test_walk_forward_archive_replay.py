"""Tests for the walk-forward archive replay driver (`A.146`), written BEFORE the engine.

The operator's decision was explicit: while the exchange is closed the six bots **walk forward
through the archive, never repeating a session**. The rejected alternative — re-replaying the most
recent closed session — accrues no new evidence after its first pass, so the maturity ladder stops
moving while the loop looks busy. That is `O.134`'s exact failure shape, and the cursor is the thing
that prevents it. Every test here is about the cursor being real.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nse_algo_trader.paper_loop.walk_forward_archive_replay import (
    ReplayedSessionOutcome,
    WalkForwardArchiveReplay,
    WalkForwardReplayCursorStore,
    WalkForwardReplayError,
)

SESSIONS = (
    date(2026, 8, 10),
    date(2026, 8, 11),
    date(2026, 8, 12),
    date(2026, 8, 13),
    date(2026, 8, 14),
)


class FakeSessionRunner:
    """Stands in for `SegmentBotPaperSession`, recording what it was asked to replay."""

    def __init__(self, *, accrued_per_session: int = 3, fails_on: date | None = None) -> None:
        self.asked: list[date] = []
        self._accrued = accrued_per_session
        self._fails_on = fails_on

    def run(self, entry_session: date) -> object:
        self.asked.append(entry_session)
        if entry_session == self._fails_on:
            raise RuntimeError(f"no exit session after {entry_session}")

        class _Report:
            entry = entry_session
            accrued = self._accrued
            trades: tuple[object, ...] = ()
            proposals_by_bot = {"cash_bot": 2}  # noqa: RUF012

            def describe(self) -> str:
                return f"{entry_session}: {self.accrued} accrued"

        return _Report()


@pytest.fixture
def cursor_store(tmp_path: Path) -> WalkForwardReplayCursorStore:
    return WalkForwardReplayCursorStore(tmp_path / "walk_forward_replay.sqlite3")


def _replay(
    cursor_store: WalkForwardReplayCursorStore,
    runner: FakeSessionRunner,
    sessions: tuple[date, ...] = SESSIONS,
) -> WalkForwardArchiveReplay:
    return WalkForwardArchiveReplay(
        session_runner=runner,
        replayable_sessions=lambda as_of: tuple(s for s in sessions if s < as_of),
        cursor_store=cursor_store,
    )


# -- the cursor -------------------------------------------------------------------------------


def test_the_first_replay_takes_the_earliest_available_session(cursor_store) -> None:
    runner = FakeSessionRunner()
    outcome = _replay(cursor_store, runner).replay_next(as_of=date(2026, 8, 19))
    assert outcome is not None
    assert outcome.session == date(2026, 8, 10)
    assert runner.asked == [date(2026, 8, 10)]


def test_each_replay_advances_forward_and_never_repeats(cursor_store) -> None:
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner)
    for _ in range(4):
        replay.replay_next(as_of=date(2026, 8, 19))
    assert runner.asked == list(SESSIONS[:4])
    assert len(set(runner.asked)) == 4


def test_the_cursor_survives_a_restart(cursor_store) -> None:
    """The loop is a long-lived process that gets killed; a cursor in memory is not a cursor."""
    runner = FakeSessionRunner()
    _replay(cursor_store, runner).replay_next(as_of=date(2026, 8, 19))
    reopened = WalkForwardReplayCursorStore(cursor_store.path)
    second = FakeSessionRunner()
    WalkForwardArchiveReplay(
        session_runner=second,
        replayable_sessions=lambda as_of: tuple(s for s in SESSIONS if s < as_of),
        cursor_store=reopened,
    ).replay_next(as_of=date(2026, 8, 19))
    assert second.asked == [date(2026, 8, 11)]


def test_when_the_archive_is_exhausted_it_answers_none_rather_than_repeating(cursor_store) -> None:
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner)
    for _ in range(len(SESSIONS)):
        assert replay.replay_next(as_of=date(2026, 8, 19)) is not None
    assert replay.replay_next(as_of=date(2026, 8, 19)) is None
    assert len(runner.asked) == len(SESSIONS)


def test_a_session_newly_arriving_in_the_archive_is_picked_up(cursor_store) -> None:
    """Tomorrow's session becomes replayable the moment it is closed and ingested."""
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner, sessions=SESSIONS[:2])
    replay.replay_next(as_of=date(2026, 8, 19))
    replay.replay_next(as_of=date(2026, 8, 19))
    assert replay.replay_next(as_of=date(2026, 8, 19)) is None

    grown = WalkForwardArchiveReplay(
        session_runner=runner,
        replayable_sessions=lambda as_of: tuple(s for s in SESSIONS if s < as_of),
        cursor_store=cursor_store,
    )
    outcome = grown.replay_next(as_of=date(2026, 8, 19))
    assert outcome is not None
    assert outcome.session == date(2026, 8, 12)


def test_a_session_at_or_after_the_as_of_instant_is_never_replayed(cursor_store) -> None:
    """Replaying today would mark out an exit against a session that has not happened."""
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner)
    replay.replay_next(as_of=date(2026, 8, 11))
    assert runner.asked == [date(2026, 8, 10)]
    assert replay.replay_next(as_of=date(2026, 8, 11)) is None


# -- failure --------------------------------------------------------------------------------


def test_a_failing_session_is_recorded_and_the_cursor_still_advances(cursor_store) -> None:
    """Otherwise one unreplayable session blocks the whole archive behind it, for ever."""
    runner = FakeSessionRunner(fails_on=date(2026, 8, 10))
    replay = _replay(cursor_store, runner)
    outcome = replay.replay_next(as_of=date(2026, 8, 19))
    assert outcome is not None
    assert outcome.failure is not None
    assert "no exit session" in outcome.failure
    assert outcome.trades_accrued == 0
    next_outcome = replay.replay_next(as_of=date(2026, 8, 19))
    assert next_outcome is not None
    assert next_outcome.session == date(2026, 8, 11)


def test_a_failure_never_escapes_to_the_caller(cursor_store) -> None:
    """The continuous loop must not die because one archived session is malformed."""
    runner = FakeSessionRunner(fails_on=date(2026, 8, 10))
    _replay(cursor_store, runner).replay_next(as_of=date(2026, 8, 19))


# -- the record -------------------------------------------------------------------------------


def test_the_store_reports_which_sessions_were_replayed(cursor_store) -> None:
    """The surface needs to tell a replayed trade from a live one, and this is how."""
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner)
    replay.replay_next(as_of=date(2026, 8, 19))
    replay.replay_next(as_of=date(2026, 8, 19))
    assert cursor_store.replayed_sessions() == (date(2026, 8, 10), date(2026, 8, 11))


def test_the_outcome_carries_what_was_accrued(cursor_store) -> None:
    runner = FakeSessionRunner(accrued_per_session=7)
    outcome = _replay(cursor_store, runner).replay_next(as_of=date(2026, 8, 19))
    assert isinstance(outcome, ReplayedSessionOutcome)
    assert outcome.trades_accrued == 7
    assert "7" in outcome.describe()


def test_progress_reports_how_much_archive_is_left(cursor_store) -> None:
    runner = FakeSessionRunner()
    replay = _replay(cursor_store, runner)
    replay.replay_next(as_of=date(2026, 8, 19))
    progress = replay.progress(as_of=date(2026, 8, 19))
    assert progress.replayed == 1
    assert progress.remaining == len(SESSIONS) - 1
    assert progress.next_session == date(2026, 8, 11)


def test_progress_on_an_untouched_archive_is_answerable(cursor_store) -> None:
    progress = _replay(cursor_store, FakeSessionRunner()).progress(as_of=date(2026, 8, 19))
    assert progress.replayed == 0
    assert progress.next_session == date(2026, 8, 10)


def test_the_store_refuses_a_session_recorded_twice(cursor_store) -> None:
    cursor_store.remember(date(2026, 8, 10), trades_accrued=1, at=datetime.now(UTC), failure=None)
    with pytest.raises(WalkForwardReplayError, match="2026-08-10"):
        cursor_store.remember(
            date(2026, 8, 10), trades_accrued=1, at=datetime.now(UTC), failure=None
        )


def test_the_store_creates_its_own_schema(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "walk_forward_replay.sqlite3"
    store = WalkForwardReplayCursorStore(path)
    assert store.replayed_sessions() == ()
    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "replayed_archive_session" in tables
