"""A capture that stops must say so on disk, at the moment it stops (`A.118`).

The depth capture already writes a full session report — coverage per instrument, usability
verdicts, bytes per row — and it writes it exactly once, at the very end, after a scan of the whole
tape. Every one of those properties is right except the timing, and the 2026-08-13 session shows
what the timing costs: the capture was stopped by a signal at 12:15, wrote its packet totals to its
log, and died before the report was built. **The tape carries no statement at all that it covers
three and a quarter hours of a six and a quarter hour session.**

Everything downstream read that silence as a full day. The paper loop replayed 76 decision instants
against 38 with depth, placed orders where no fill was possible, and reported the P&L
(`docs/research/233`). `A.116` taught the loop to derive the covered window by inspecting the
packets themselves, which works and is the wrong place for the fact to live: a consumer should be
able to ask the CAPTURE what it captured.

**This is a heartbeat, not a report.** It is rewritten on every flush — atomically, in a few
hundred bytes, next to the shard it describes — so the answer survives any death the process can
suffer, including `SIGKILL`, a full disk on another mount, or the host going down. It says when the
capture started, when it last wrote, how many rows and instruments it holds, and whether it reached
the session close under its own power or stopped early. It does NOT replace the session report: the
report judges USABILITY per instrument and this judges only LIVENESS, which is the question nobody
could answer on 2026-08-13.

**The distinction that matters to a consumer:** `ended_at_session_close` is true only when the
recorder ran out its own clock. A capture stopped at 12:15 leaves it false forever, because nothing
ever rewrites the file afterwards — the absence of an ending IS the ending.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

LIVENESS_FILENAME = "capture_liveness.json"
"""One per capture run, beside the session's shards. Named for the question it answers."""


class CaptureLivenessError(Exception):
    """The liveness record could not be written or read, and a silent capture is the alternative."""


@dataclass(frozen=True, slots=True)
class CaptureLiveness:
    """What one capture run has done so far, as of its last flush."""

    session_date: date
    capture_run_id: str
    started_at: datetime
    last_flush_at: datetime
    first_packet_at: datetime | None
    last_packet_at: datetime | None
    rows_written: int
    instruments_admitted: int
    session_ends_at: datetime
    ended_at_session_close: bool
    stop_reason: str = ""

    @property
    def covered_seconds(self) -> float:
        """How much wall time the packets themselves span. Zero when nothing was captured."""
        if self.first_packet_at is None or self.last_packet_at is None:
            return 0.0
        return (self.last_packet_at - self.first_packet_at).total_seconds()

    @property
    def stopped_early(self) -> bool:
        """Whether the capture stopped before the session it was recording did.

        A minute of slack is not granted: the recorder writes this record on its way out, and a run
        that reached the close sets `ended_at_session_close` itself. Anything else is early, and a
        consumer that guesses otherwise is guessing about missing data.
        """
        return not self.ended_at_session_close

    def describe(self) -> str:
        window = (
            f"{self.first_packet_at:%H:%M} to {self.last_packet_at:%H:%M}"
            if self.first_packet_at and self.last_packet_at
            else "no packets"
        )
        ending = (
            "ran to the session close"
            if self.ended_at_session_close
            else f"STOPPED EARLY at {self.last_flush_at:%H:%M}"
            + (f" ({self.stop_reason})" if self.stop_reason else "")
        )
        return (
            f"{self.session_date.isoformat()} run {self.capture_run_id}: {window}, "
            f"{self.rows_written:,} rows over {self.instruments_admitted:,} instruments — {ending}"
        )

    def as_json(self) -> str:
        return json.dumps(
            {
                "session_date": self.session_date.isoformat(),
                "capture_run_id": self.capture_run_id,
                "started_at": self.started_at.isoformat(),
                "last_flush_at": self.last_flush_at.isoformat(),
                "first_packet_at": (
                    self.first_packet_at.isoformat() if self.first_packet_at else None
                ),
                "last_packet_at": (
                    self.last_packet_at.isoformat() if self.last_packet_at else None
                ),
                "rows_written": self.rows_written,
                "instruments_admitted": self.instruments_admitted,
                "session_ends_at": self.session_ends_at.isoformat(),
                "ended_at_session_close": self.ended_at_session_close,
                "stop_reason": self.stop_reason,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, payload: str) -> CaptureLiveness:
        try:
            fields = json.loads(payload)
            return cls(
                session_date=date.fromisoformat(fields["session_date"]),
                capture_run_id=str(fields["capture_run_id"]),
                started_at=datetime.fromisoformat(fields["started_at"]),
                last_flush_at=datetime.fromisoformat(fields["last_flush_at"]),
                first_packet_at=(
                    datetime.fromisoformat(fields["first_packet_at"])
                    if fields.get("first_packet_at")
                    else None
                ),
                last_packet_at=(
                    datetime.fromisoformat(fields["last_packet_at"])
                    if fields.get("last_packet_at")
                    else None
                ),
                rows_written=int(fields["rows_written"]),
                instruments_admitted=int(fields["instruments_admitted"]),
                session_ends_at=datetime.fromisoformat(fields["session_ends_at"]),
                ended_at_session_close=bool(fields["ended_at_session_close"]),
                stop_reason=str(fields.get("stop_reason", "")),
            )
        except (KeyError, TypeError, ValueError) as unreadable:
            raise CaptureLivenessError(
                f"the liveness record is unreadable ({unreadable}); a capture that cannot say what "
                "it covered is indistinguishable from one that covered everything, which is the "
                "failure this record exists to prevent"
            ) from unreadable


def liveness_path(tape_root: Path, session_date: date, capture_run_id: str) -> Path:
    """Beside the session's shards, one file per run — runs do not overwrite each other."""
    return (
        tape_root
        / f"session_date={session_date.isoformat()}"
        / f"{capture_run_id}_{LIVENESS_FILENAME}"
    )


def write_liveness(liveness: CaptureLiveness, tape_root: Path) -> Path:
    """Rewrite the record atomically. Called on every flush, so it must be cheap and total.

    Atomic because a consumer may read it at any instant, including while the capture is writing
    it: a torn record would be read as a corrupt capture rather than a live one.
    """
    destination = liveness_path(tape_root, liveness.session_date, liveness.capture_run_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        with temporary.open("w") as sink:
            sink.write(liveness.as_json())
            sink.flush()
            os.fsync(sink.fileno())
        temporary.replace(destination)
    except OSError as unwritable:
        temporary.unlink(missing_ok=True)
        raise CaptureLivenessError(
            f"the liveness record could not be written to {destination}: {unwritable}"
        ) from unwritable
    return destination


def read_liveness_records(tape_root: Path, session_date: date) -> tuple[CaptureLiveness, ...]:
    """Every run's record for one session, oldest run first. Empty when the capture predates this.

    An empty result is NOT a statement that the session is complete. Tapes recorded before this
    existed carry no record, and a consumer must treat their coverage as unknown rather than whole
    — which is exactly what `A.116` derives from the packets.
    """
    directory = tape_root / f"session_date={session_date.isoformat()}"
    if not directory.exists():
        return ()
    records: list[CaptureLiveness] = []
    for path in sorted(directory.glob(f"*_{LIVENESS_FILENAME}")):
        records.append(CaptureLiveness.from_json(path.read_text()))
    return tuple(records)


def session_was_fully_captured(tape_root: Path, session_date: date) -> bool | None:
    """`True`, `False`, or `None` when no run left a record at all.

    Three-valued on purpose. "No record" and "recorded and stopped early" are different facts, and
    collapsing them is how a three-hour tape came to be read as a full session.
    """
    records = read_liveness_records(tape_root, session_date)
    if not records:
        return None
    return any(record.ended_at_session_close for record in records)


def liveness_now(
    *,
    session_date: date,
    capture_run_id: str,
    started_at: datetime,
    first_packet_at: datetime | None,
    last_packet_at: datetime | None,
    rows_written: int,
    instruments_admitted: int,
    session_ends_at: datetime,
    ended_at_session_close: bool,
    stop_reason: str = "",
) -> CaptureLiveness:
    """Build the record for this instant. `last_flush_at` is now, because that is what it means."""
    return CaptureLiveness(
        session_date=session_date,
        capture_run_id=capture_run_id,
        started_at=started_at,
        last_flush_at=datetime.now(UTC),
        first_packet_at=first_packet_at,
        last_packet_at=last_packet_at,
        rows_written=rows_written,
        instruments_admitted=instruments_admitted,
        session_ends_at=session_ends_at,
        ended_at_session_close=ended_at_session_close,
        stop_reason=stop_reason,
    )
