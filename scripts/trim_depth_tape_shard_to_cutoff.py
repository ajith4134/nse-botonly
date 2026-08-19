"""Trim a depth-tape shard to rows strictly before a cutoff, quarantining the originals.

Written for a real operational fault on 2026-08-11: a capture was widened by launching
a second recorder, and the first recorder kept running for two minutes afterwards
because the kill targeted the wrapper PID rather than the Python process. Both wrote,
so the 649 instruments common to both universes have duplicated rows in the overlap.

Duplicated rows are worse than missing ones. A gap is visible and the session report
will say so; a duplicate is invisible and silently double-counts every rate, every
volume delta and every microstructure statistic derived from the tape.

The tape is append-only by design, so this rewrites nothing in place: each affected
part is rewritten to a new file and the original is MOVED to a quarantine directory,
where it stays. Nothing is deleted.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.compute as compute
import pyarrow.parquet as parquet

from nse_algo_trader.market_depth.depth_tape_schema import DEPTH_TAPE_ARROW_SCHEMA

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


def trim_shard(shard_directory: Path, cutoff: datetime, quarantine_root: Path) -> int:
    """Keep rows with `receipt_time` strictly before `cutoff`. Returns rows dropped."""
    quarantine_root.mkdir(parents=True, exist_ok=True)
    dropped_total = 0
    for part_path in sorted(shard_directory.glob("part-*.parquet")):
        table = parquet.read_table(part_path)
        keep_mask = compute.less(table.column("receipt_time"), cutoff)
        kept = table.filter(keep_mask)
        dropped = table.num_rows - kept.num_rows
        if dropped == 0:
            continue
        dropped_total += dropped
        print(f"  {part_path.name}: {table.num_rows:,} -> {kept.num_rows:,} (-{dropped:,})")

        temporary_path = part_path.with_suffix(".parquet.trimmed.tmp")
        if kept.num_rows:
            with temporary_path.open("wb") as sink:
                with parquet.ParquetWriter(
                    sink, DEPTH_TAPE_ARROW_SCHEMA, compression="zstd"
                ) as writer:
                    writer.write_table(kept.select(DEPTH_TAPE_ARROW_SCHEMA.names))
                sink.flush()
        part_path.replace(quarantine_root / part_path.name)
        if kept.num_rows:
            temporary_path.replace(part_path)
    return dropped_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-directory", type=Path, required=True)
    parser.add_argument(
        "--cutoff-ist",
        required=True,
        help="ISO timestamp in IST; rows at or after this are removed",
    )
    parser.add_argument("--quarantine-root", type=Path, required=True)
    arguments = parser.parse_args()

    cutoff = datetime.fromisoformat(arguments.cutoff_ist).replace(tzinfo=INDIA_MARKET_TIMEZONE)
    print(f"trimming {arguments.shard_directory} to rows before {cutoff:%Y-%m-%d %H:%M:%S %Z}")
    dropped = trim_shard(arguments.shard_directory, cutoff, arguments.quarantine_root)
    print(f"dropped {dropped:,} rows; originals quarantined in {arguments.quarantine_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
