"""`L0.21` — the persisted depth tape: append-only Parquet, atomic parts, read-back.

Volume drives the format. The measured aggregate was 54.4 packets/s for 120
instruments, so a thousand-instrument capture is ~450 rows/s and ~10M rows a session.
At that rate a row store's per-row overhead and index maintenance dominate, while
depth data is unusually compressible — within one instrument the five price levels
are near-constant between consecutive packets, so dictionary and delta encoding do
most of the work. Parquet with zstd is the fit, and pyarrow's `ParquetWriter` gives
incremental row-group appends without holding the session in memory.

**Durability under `kill -9` is a design requirement, not a nicety.** A capture runs
unattended for six hours; the process will eventually be killed mid-flush. Every part
file is therefore written to a temporary name *in its destination directory* and moved
into place with `os.replace`, which is atomic within a filesystem. A reader can never
observe a partially written part: it either exists complete or does not exist.

`realized_bytes_per_row` is the reason the store reports on itself. The admission
controller sizes the capture universe from it, and a figure taken from documentation
rather than from this tape's own compressed output would size the universe wrongly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType

import pyarrow as pa
import pyarrow.dataset as arrow_dataset
import pyarrow.parquet as parquet

from nse_algo_trader.market_depth.depth_tape_schema import (
    DEPTH_TAPE_ARROW_SCHEMA,
    DepthPacket,
    IntegrityFlag,
    packet_to_row,
)

TAPE_COMPRESSION = "zstd"
"""Verified available in this host's pyarrow build before being chosen."""


class DepthTapeStoreError(Exception):
    """Raised when the tape cannot be written or read consistently."""


@dataclass(frozen=True)
class TapeFlushRecord:
    """What one flush actually produced — the store's evidence about itself."""

    part_path: Path
    row_count: int
    compressed_bytes: int
    flushed_at: datetime

    @property
    def bytes_per_row(self) -> float:
        return self.compressed_bytes / self.row_count if self.row_count else 0.0


class MarketDepthTapeStore:
    """One shard's writer for one session date.

    A shard is owned by exactly one thread, which is what lets the hot path stay
    lock-free. Sharding matches the recorder's connection shards, so two writers never
    contend for a file.
    """

    def __init__(
        self,
        tape_root: Path,
        session_date: date,
        shard_index: int,
        max_buffered_rows: int,
        max_seconds_between_flushes: float,
        capture_run_id: str,
        compression: str = TAPE_COMPRESSION,
    ) -> None:
        if max_buffered_rows <= 0:
            raise DepthTapeStoreError("max_buffered_rows must be positive")
        if not capture_run_id or "/" in capture_run_id or "=" in capture_run_id:
            raise DepthTapeStoreError(
                f"capture_run_id must be a non-empty path-safe token, got {capture_run_id!r}"
            )
        self._tape_root = tape_root
        self._session_date = session_date
        self._capture_run_id = capture_run_id
        self._shard_index = shard_index
        self._max_buffered_rows = max_buffered_rows
        self._max_seconds_between_flushes = max_seconds_between_flushes
        self._compression = compression
        self._buffer: list[dict[str, object]] = []
        self._last_flush_monotonic = time.monotonic()
        self._closed = False
        self.flush_records: list[TapeFlushRecord] = []
        self.rows_written = 0
        self._shard_directory.mkdir(parents=True, exist_ok=True)
        self._part_index = self._next_free_part_index()

    def _next_free_part_index(self) -> int:
        """Resume numbering after whatever this shard already holds.

        A capture restarted mid-session — to widen the universe, or after a crash —
        would otherwise begin again at `part-000000` and **overwrite the morning's
        tape**. The existing parts are the evidence of what number to use next, so the
        directory is asked rather than assumed empty.
        """
        existing = [
            int(path.stem.rsplit("-", 1)[1])
            for path in self._shard_directory.glob("part-*.parquet")
            if path.stem.rsplit("-", 1)[1].isdigit()
        ]
        return max(existing) + 1 if existing else 0

    @property
    def _shard_directory(self) -> Path:
        """`session_date=… / capture_run=… / shard=…`.

        The `capture_run` level exists because of a real fault on 2026-08-11: a capture
        was widened by launching a second recorder while the first was still alive, and
        both wrote into `shard=01` with **independent part counters**, so each could
        overwrite the other's parts. Resuming the part index protects a sequential
        restart but does nothing against a concurrent one. Scoping the directory to the
        run makes the collision impossible rather than unlikely — two recorders cannot
        share a path, whatever the operator does.
        """
        return (
            self._tape_root
            / f"session_date={self._session_date.isoformat()}"
            / f"capture_run={self._capture_run_id}"
            / f"shard={self._shard_index:02d}"
        )

    @property
    def realized_bytes_per_row(self) -> float | None:
        """Compressed bytes per row across everything this shard has flushed.

        None until the first flush — the admission controller must treat an unmeasured
        tape as unmeasured rather than substituting an assumed figure.
        """
        total_rows = sum(record.row_count for record in self.flush_records)
        if not total_rows:
            return None
        total_bytes = sum(record.compressed_bytes for record in self.flush_records)
        return total_bytes / total_rows

    def append(self, packet: DepthPacket, integrity_flags: IntegrityFlag) -> None:
        """Buffer one classified packet, flushing when a bound is reached."""
        if self._closed:
            raise DepthTapeStoreError("append after close")
        self._buffer.append(packet_to_row(packet, integrity_flags))
        if self._flush_is_due():
            self.flush()

    def append_many(
        self, classified_packets: Iterable[tuple[DepthPacket, IntegrityFlag]]
    ) -> None:
        for packet, integrity_flags in classified_packets:
            self.append(packet, integrity_flags)

    def _flush_is_due(self) -> bool:
        if len(self._buffer) >= self._max_buffered_rows:
            return True
        elapsed = time.monotonic() - self._last_flush_monotonic
        return bool(self._buffer) and elapsed >= self._max_seconds_between_flushes

    def flush_if_due(self) -> TapeFlushRecord | None:
        """Flush only when a bound has been reached — the time bound's only caller.

        `_flush_is_due` used to be consulted exclusively from `append`, so the
        `max_seconds_between_flushes` bound could not fire on a shard whose feed had
        gone quiet: no packet meant no check, and the buffer sat in RAM indefinitely
        past the bound the operator configured. Under the `kill -9` this module treats
        as the expected end of an unattended capture, that whole buffer was lost. The
        writer thread now calls this whenever its queue poll times out.
        """
        if self._closed or not self._flush_is_due():
            return None
        return self.flush()

    def flush(self) -> TapeFlushRecord | None:
        """Write the buffer as one atomic part file. Returns None if nothing buffered."""
        if not self._buffer:
            self._last_flush_monotonic = time.monotonic()
            return None

        table = pa.Table.from_pylist(self._buffer, schema=DEPTH_TAPE_ARROW_SCHEMA)
        final_path = self._shard_directory / f"part-{self._part_index:06d}.parquet"
        # Deliberately in the destination directory: os.replace is atomic only within
        # a filesystem, and a temp dir elsewhere could be a different mount.
        temporary_path = self._shard_directory / f".part-{self._part_index:06d}.parquet.tmp"

        try:
            with temporary_path.open("wb") as sink:
                with parquet.ParquetWriter(
                    sink, DEPTH_TAPE_ARROW_SCHEMA, compression=self._compression
                ) as writer:
                    writer.write_table(table)
                # Force the bytes to disk before the rename, so the atomic move
                # publishes durable data rather than page cache.
                sink.flush()
                os.fsync(sink.fileno())
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        compressed_bytes = temporary_path.stat().st_size
        temporary_path.replace(final_path)
        self._fsync_directory()

        record = TapeFlushRecord(
            part_path=final_path,
            row_count=len(self._buffer),
            compressed_bytes=compressed_bytes,
            flushed_at=datetime.now(UTC),
        )
        self.flush_records.append(record)
        self.rows_written += len(self._buffer)
        self._part_index += 1
        self._buffer.clear()
        self._last_flush_monotonic = time.monotonic()
        return record

    def _fsync_directory(self) -> None:
        """Persist the rename itself — without this the file can survive while the
        directory entry naming it does not."""
        directory_descriptor = os.open(self._shard_directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True

    def __enter__(self) -> MarketDepthTapeStore:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class MarketDepthTapeReader:
    """Read-back over a whole tape root, across sessions and shards.

    Reads through `pyarrow.dataset`, which sees only completed part files — the
    writer's temp files begin with a dot and are excluded, so a read concurrent with a
    capture returns a consistent prefix rather than a torn row group.
    """

    def __init__(self, tape_root: Path) -> None:
        self._tape_root = tape_root

    def _dataset(self, session_date: date | None = None) -> arrow_dataset.Dataset:
        root = self._tape_root
        if session_date is not None:
            root = root / f"session_date={session_date.isoformat()}"
        if not root.exists():
            raise DepthTapeStoreError(f"no tape at {root}")
        # pyarrow ignores names beginning with '.' or '_' by default, which is exactly
        # why in-flight parts are named with a leading dot: a read concurrent with a
        # capture sees only completed files.
        return arrow_dataset.dataset(root, format="parquet", partitioning="hive")

    def session_dates(self) -> list[date]:
        return sorted(
            date.fromisoformat(child.name.split("=", 1)[1])
            for child in self._tape_root.iterdir()
            if child.is_dir() and child.name.startswith("session_date=")
        )

    def read_instrument_window(
        self,
        instrument_token: int,
        window_start: datetime,
        window_end: datetime,
        session_date: date | None = None,
        time_column: str = "receipt_time",
    ) -> pa.Table:
        """Every row for one instrument in a half-open time window.

        `time_column` chooses the clock, and the choice is the caller's to make
        deliberately: `receipt_time` answers "what did I know by then", which is what a
        backtest must ask, while `exchange_time` answers "what was true then". The
        measured 11-minute stale packet is why these are not interchangeable.
        """
        if time_column not in ("receipt_time", "exchange_time"):
            raise DepthTapeStoreError(f"unknown time column {time_column!r}")
        field = arrow_dataset.field
        return self._dataset(session_date).to_table(
            filter=(
                (field("instrument_token") == instrument_token)
                & (field(time_column) >= window_start)
                & (field(time_column) < window_end)
            )
        )

    def book_at(
        self,
        instrument_token: int,
        as_of: datetime,
        session_date: date,
        time_column: str = "receipt_time",
    ) -> dict[str, object] | None:
        """The last known book at or before `as_of`, or None if nothing precedes it.

        This is the reconstruction primitive `1.22` will build on.

        **Ordered by time first, sequence second.** `receipt_sequence` counts within one
        FEED, not within a session: a recorder restarted mid-session — or a calibration
        pass followed by a full session — starts a fresh feed whose counter begins again
        at zero. Ordering by sequence alone therefore lets an older run's high sequence
        beat a newer run's low one and return a stale book. Measured on the 2026-08-11
        tape: two runs with overlapping ranges 1..132,313 across 4,236 shared
        instruments. `receipt_time` is the cross-run truth; the sequence only breaks
        ties inside a single feed's sub-second batches, which is exactly what it can do.
        """
        session_start = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
        rows = self.read_instrument_window(
            instrument_token, session_start, as_of, session_date, time_column
        )
        if rows.num_rows == 0:
            return None
        ordered = rows.sort_by(
            [(time_column, "ascending"), ("receipt_sequence", "ascending")]
        )
        return {name: ordered.column(name)[-1].as_py() for name in ordered.column_names}

    def instrument_tokens(self, session_date: date) -> list[int]:
        table = self._dataset(session_date).to_table(columns=["instrument_token"])
        return sorted(set(table.column("instrument_token").to_pylist()))

    def iter_instrument_row_counts(
        self, session_date: date
    ) -> Iterator[tuple[int, int]]:
        """(token, row count) for a session — the session report's raw input."""
        table = self._dataset(session_date).to_table(columns=["instrument_token"])
        counts: dict[int, int] = {}
        for token in table.column("instrument_token").to_pylist():
            counts[token] = counts.get(token, 0) + 1
        yield from sorted(counts.items())

    def instrument_packet_rates(self, session_date: date) -> dict[int, float]:
        """Token -> packets/s, each measured over that instrument's OWN observed span.

        Dividing by a nominal observation window instead would be wrong whenever the
        tape holds more or less than that window — which is the normal case, since a
        capture restarted mid-session reads back a tape covering everything captured so
        far, not just the calibration slice. An instrument seen once has no span and so
        no rate, and is reported as absent rather than as zero.
        """
        table = self._dataset(session_date).to_table(
            columns=["instrument_token", "receipt_time"]
        )
        tokens = table.column("instrument_token").to_pylist()
        receipts = table.column("receipt_time").to_pylist()
        first_seen: dict[int, datetime] = {}
        last_seen: dict[int, datetime] = {}
        counts: dict[int, int] = {}
        for token, receipt in zip(tokens, receipts, strict=True):
            counts[token] = counts.get(token, 0) + 1
            if token not in first_seen or receipt < first_seen[token]:
                first_seen[token] = receipt
            if token not in last_seen or receipt > last_seen[token]:
                last_seen[token] = receipt
        rates: dict[int, float] = {}
        for token, count in counts.items():
            span_seconds = (last_seen[token] - first_seen[token]).total_seconds()
            if span_seconds > 0:
                rates[token] = count / span_seconds
        return rates

    def total_bytes_on_disk(self) -> int:
        return sum(
            path.stat().st_size for path in self._tape_root.rglob("*.parquet")
        )

    def measured_bytes_per_row(self, session_dates: Sequence[date] | None = None) -> float | None:
        """Realized compressed bytes per row across the tape, or None if empty.

        The admission controller's single most important input, and the reason it is
        computed from the tape's own files rather than estimated.
        """
        dates = list(session_dates) if session_dates is not None else self.session_dates()
        total_rows = 0
        total_bytes = 0
        for session_date in dates:
            directory = self._tape_root / f"session_date={session_date.isoformat()}"
            if not directory.exists():
                continue
            for part_path in directory.rglob("*.parquet"):
                total_bytes += part_path.stat().st_size
                total_rows += parquet.ParquetFile(part_path).metadata.num_rows
        return total_bytes / total_rows if total_rows else None
