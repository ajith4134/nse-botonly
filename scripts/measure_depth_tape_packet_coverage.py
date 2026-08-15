"""Why a paper entry waits an hour for its first fill — packets, buckets, and what was served.

`BACKLOG` `M21` / `O.98`. The median entry on 2026-08-12 did not fill for 60 minutes, and there are
two candidate explanations that point at opposite fixes:

1. **The tape is sparse.** The capture recorded no depth for that instrument through the gap, so no
   order could have filled and the latency is a property of the RECORDING (or of a genuinely quiet
   scrip). Nothing in the fill path is wrong.
2. **The staleness threshold is discarding packets the tape holds.** `SteppedRecordedBookSource`
   serves a snapshot only while it is fresh by that instrument's own gap quantile (`A.110`), so a
   scrip that ticks once every ten minutes has a threshold of about ten minutes and any decision
   instant landing further from its last packet gets NO book — even though a book exists. Then the
   latency is ours, and the fix is cheap.

This measures both against the same session, per instrument:

* **packets** — how many depth rows the tape holds;
* **buckets covered** — how many of the session's 76 five-minute decision instants have at least
  one packet at or before them within the bucket;
* **instants served** — how many of those instants the book source actually handed to the venue,
  after the staleness rule;
* **served / covered** — the discard ratio. Near 1.0 says the tape is the constraint; well below
  says the threshold is.

Usage:
    python scripts/measure_depth_tape_packet_coverage.py 2026-08-12
    python scripts/measure_depth_tape_packet_coverage.py 2026-08-12 --traded-only
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    OrderBookSnapshotReplayEngine,
)
from nse_algo_trader.paper_loop.replayed_depth_book_source import SteppedRecordedBookSource
from nse_algo_trader.replay_session_clock import ReplaySessionClock, session_for

DEFAULT_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
DEFAULT_SESSION_ROOT = Path("~/.nse_algo_trader/paper_verification").expanduser()
DECISION_STEP = timedelta(minutes=5)
STALENESS_QUANTILE = 0.95

STARVED_RATIO = 0.5
"""Below this share of its covered buckets, an instrument is being discarded more than it is served,
and the threshold rather than the tape is deciding whether it can be traded."""


def traded_instruments(session_date: date, session_root: Path) -> set[int]:
    """Every instrument the paper session actually sent an order for."""
    journal = session_root / session_date.isoformat() / "journal.sqlite3"
    if not journal.exists():
        return set()
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as connection:
        return {
            int(row[0])
            for row in connection.execute("SELECT DISTINCT instrument_token FROM order_intent")
        }


def packets_per_instrument(
    session_date: date, tape_root: Path, decision_instants: list[date]
) -> tuple[dict[int, int], dict[int, set[int]]]:
    """One streamed pass: packet counts, and which decision buckets each instrument has depth in."""
    reader = MarketDepthTapeReader(tape_root)
    window_start = session_for(session_date).opens_at - timedelta(hours=4)
    window_end = session_for(session_date).closes_at + timedelta(hours=4)
    counts: dict[int, int] = defaultdict(int)
    buckets: dict[int, set[int]] = defaultdict(set)
    first = decision_instants[0]
    dataset = reader._dataset(session_date)
    scanner = dataset.scanner(columns=["instrument_token", "receipt_time"], batch_size=200_000)
    for batch in scanner.to_batches():
        if not batch.num_rows:
            continue
        tokens = batch.column("instrument_token").to_pylist()
        times = batch.column("receipt_time").to_pylist()
        for token, moment in zip(tokens, times, strict=True):
            if moment is None or moment < window_start or moment >= window_end:
                continue
            key = int(token)
            counts[key] += 1
            index = int((moment - first).total_seconds() // DECISION_STEP.total_seconds())
            if 0 <= index < len(decision_instants):
                buckets[key].add(index)
    return dict(counts), dict(buckets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_date")
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_TAPE_ROOT)
    parser.add_argument("--session-root", type=Path, default=DEFAULT_SESSION_ROOT)
    parser.add_argument(
        "--traded-only",
        action="store_true",
        help="restrict to the instruments the paper session actually ordered",
    )
    arguments = parser.parse_args()
    session_date = date.fromisoformat(arguments.session_date)

    clock = ReplaySessionClock(session_for(session_date), {})
    instants = list(clock.step_through_session(DECISION_STEP))
    print(f"{session_date.isoformat()}: {len(instants)} decision instants, five minutes apart")

    counts, buckets = packets_per_instrument(session_date, arguments.tape_root, instants)
    print(f"tape holds {sum(counts.values()):,} packets across {len(counts):,} instruments")

    chosen = set(counts)
    if arguments.traded_only:
        traded = traded_instruments(session_date, arguments.session_root)
        chosen = {token for token in counts if token in traded}
        print(f"restricted to the {len(chosen)} instrument(s) the session ordered")
    if not chosen:
        print("no instrument to measure")
        return 1

    engine = OrderBookSnapshotReplayEngine(
        MarketDepthTapeReader(arguments.tape_root),
        session_date=session_date,
        staleness_quantile=STALENESS_QUANTILE,
    )
    source = SteppedRecordedBookSource(engine, instants, staleness_quantile=STALENESS_QUANTILE)
    source.preload(
        MarketDepthTapeReader(arguments.tape_root),
        sorted(chosen),
        session_date=session_date,
        window_start=session_for(session_date).opens_at - timedelta(hours=4),
        window_end=session_for(session_date).closes_at + timedelta(hours=4),
    )

    rows: list[tuple[int, int, int, int]] = []
    for token in sorted(chosen):
        served = sum(1 for instant in instants if source.book_at(token, instant) is not None)
        rows.append((token, counts.get(token, 0), len(buckets.get(token, set())), served))

    packets = [row[1] for row in rows]
    covered = [row[2] for row in rows]
    served_counts = [row[3] for row in rows]
    ratios = [row[3] / row[2] for row in rows if row[2] > 0]

    print("\nper instrument, over the session:")
    print(
        f"  packets          median {statistics.median(packets):>8,.0f}   "
        f"min {min(packets):,}   max {max(packets):,}"
    )
    print(
        f"  buckets covered  median {statistics.median(covered):>8,.0f} / {len(instants)}   "
        f"min {min(covered)}   max {max(covered)}"
    )
    print(
        f"  instants served  median {statistics.median(served_counts):>8,.0f} / {len(instants)}   "
        f"min {min(served_counts)}   max {max(served_counts)}"
    )
    if ratios:
        print(
            f"  served / covered median {statistics.median(ratios):>8.3f}   "
            f"mean {statistics.mean(ratios):.3f}   "
            f"instruments below {STARVED_RATIO}: {sum(1 for r in ratios if r < STARVED_RATIO)}"
        )

    starved = sorted(rows, key=lambda row: (row[3], -row[1]))[:10]
    print("\nthe ten least-served instruments (token, packets, buckets covered, instants served):")
    for token, packet_count, bucket_count, served in starved:
        print(f"  {token:>10}  {packet_count:>9,}  {bucket_count:>4}  {served:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
