"""A capture must state its own coverage while it runs, not only if it survives to the end.

The 2026-08-13 session is the case these tests exist for: stopped by a signal at 12:15, killed
before its session report could be built, leaving a tape covering three and a quarter hours of a
six and a quarter hour session with nothing on disk saying so — and every consumer read the silence
as a full day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.market_depth.capture_liveness_record import (
    CaptureLivenessError,
    liveness_now,
    read_liveness_records,
    session_was_fully_captured,
    write_liveness,
)

SESSION = date(2026, 8, 13)
OPENED = datetime(2026, 8, 13, 3, 45, tzinfo=UTC)
CLOSES = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


def _record(
    *,
    run_id: str = "094926",
    last_packet: datetime | None = None,
    ended: bool = False,
    rows: int = 2_373_256,
    stop_reason: str = "",
):
    return liveness_now(
        session_date=SESSION,
        capture_run_id=run_id,
        started_at=OPENED,
        first_packet_at=OPENED + timedelta(minutes=6),
        last_packet_at=last_packet or datetime(2026, 8, 13, 6, 45, tzinfo=UTC),
        rows_written=rows,
        instruments_admitted=652,
        session_ends_at=CLOSES,
        ended_at_session_close=ended,
        stop_reason=stop_reason,
    )


def test_a_capture_stopped_early_says_so_on_disk(tmp_path: Path) -> None:
    write_liveness(_record(stop_reason="signal 15"), tmp_path)
    records = read_liveness_records(tmp_path, SESSION)
    assert len(records) == 1
    assert records[0].stopped_early
    assert records[0].stop_reason == "signal 15"
    assert session_was_fully_captured(tmp_path, SESSION) is False
    assert "STOPPED EARLY" in records[0].describe()


def test_a_capture_that_ran_to_the_close_says_that_instead(tmp_path: Path) -> None:
    write_liveness(_record(last_packet=CLOSES, ended=True), tmp_path)
    assert session_was_fully_captured(tmp_path, SESSION) is True
    assert not read_liveness_records(tmp_path, SESSION)[0].stopped_early


def test_no_record_is_unknown_and_never_read_as_complete(tmp_path: Path) -> None:
    """The three-valued answer: a tape recorded before this existed is unknown, not whole."""
    assert session_was_fully_captured(tmp_path, SESSION) is None
    assert read_liveness_records(tmp_path, SESSION) == ()


def test_several_runs_each_keep_their_own_record(tmp_path: Path) -> None:
    """2026-08-11 had four capture runs; the fourth is the one that reached the close."""
    write_liveness(_record(run_id="100058", rows=144_469), tmp_path)
    write_liveness(_record(run_id="101224", rows=1_001_395), tmp_path)
    write_liveness(
        _record(run_id="104035", rows=10_212_041, last_packet=CLOSES, ended=True), tmp_path
    )
    records = read_liveness_records(tmp_path, SESSION)
    assert [record.capture_run_id for record in records] == ["100058", "101224", "104035"]
    assert session_was_fully_captured(tmp_path, SESSION) is True, (
        "one run reaching the close is enough"
    )


def test_the_record_is_rewritten_in_place_by_the_same_run(tmp_path: Path) -> None:
    write_liveness(_record(rows=1_000), tmp_path)
    write_liveness(_record(rows=2_000), tmp_path)
    records = read_liveness_records(tmp_path, SESSION)
    assert len(records) == 1, "a run updates its own record rather than accumulating files"
    assert records[0].rows_written == 2_000


def test_the_covered_span_comes_from_the_packets_themselves(tmp_path: Path) -> None:
    write_liveness(_record(), tmp_path)
    covered = read_liveness_records(tmp_path, SESSION)[0].covered_seconds
    assert covered == pytest.approx(timedelta(hours=2, minutes=54).total_seconds())


def test_an_unreadable_record_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    directory = tmp_path / f"session_date={SESSION.isoformat()}"
    directory.mkdir(parents=True)
    (directory / "094926_capture_liveness.json").write_text('{"session_date": "not-a-date"}')
    with pytest.raises(CaptureLivenessError, match="unreadable"):
        read_liveness_records(tmp_path, SESSION)
