#!/usr/bin/env python
"""Backfill 5-minute bars for the sessions the depth tape covers (`R.16`, for `F04`'s `R.05`).

**The gap this closes.** The bar store held 659,990 five-minute bars ending 2026-08-05; the
recorded depth tape starts 2026-08-11. No single date had both, so a paper session could be
verified on real bars OR against a real book, never both at once — and `F04`'s whole claim is that
a signal taken from a real tape is filled against the book that was actually there. Acquiring the
missing bars is the fix; scoping the verification down to what happened to be on disk is not
(`R.16`).

**Availability time is derived, not stamped from the wall.** A five-minute bar is knowable only
once its interval has closed, so `availability_time = bar_timestamp + interval`, matching the
convention already in the store (a 15:10 bar became available at 15:15). Writing the fetch time
instead would make every backfilled bar appear knowable months before it existed, which is the one
mistake that turns this store into a machine for seeing the future.

**Idempotent by construction.** `INSERT OR IGNORE`: a bar already recorded is point-in-time truth
and a re-run must not restate it. What the run actually wrote is counted and reported.

Usage:
    python scripts/backfill_five_minute_bars.py 2026-08-11
    python scripts/backfill_five_minute_bars.py 2026-08-11 --limit 50   # a probe, reported as one
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.compute as compute
import pyarrow.dataset as arrow_dataset

from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)

DEFAULT_MARKET_DATA_PATH = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
DEFAULT_DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
BAR_INTERVAL = "5m"
KITE_INTERVAL_NAME = "5minute"
INTERVAL = timedelta(minutes=5)

# Kite publishes 3 requests/second for the historical endpoint. Sitting at half of it is the same
# reasoning `derive_limits` applies to the regulatory order-rate threshold: a limit set AT a
# published ceiling is breached by any burst.
REQUESTS_PER_SECOND = 1.5


@dataclass
class BackfillOutcome:
    """What the run actually did, so a partial run is never read as a complete one."""

    session_date: date
    instruments_attempted: int
    instruments_answered: int
    bars_written: int
    bars_already_present: int
    failures: list[tuple[int, str]]

    def describe(self) -> str:
        return (
            f"{self.session_date.isoformat()}: {self.instruments_answered}/"
            f"{self.instruments_attempted} instruments answered, {self.bars_written} bars written, "
            f"{self.bars_already_present} already present, {len(self.failures)} failed"
        )


def instrument_tokens_recorded_in_the_depth_tape(
    session_date: date, tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT
) -> tuple[int, ...]:
    """Every instrument whose book was recorded that day — the universe a fill can be checked on."""
    partition = tape_root / f"session_date={session_date.isoformat()}"
    if not partition.exists():
        raise SystemExit(f"no depth tape partition at {partition}")
    table = arrow_dataset.dataset(partition, format="parquet").to_table(
        columns=["instrument_token"]
    )
    unique_tokens = compute.unique(table["instrument_token"]).to_pylist()
    return tuple(sorted(int(token) for token in unique_tokens))


def tradeable_names(market_data: Path) -> dict[int, str]:
    """The instrument master's latest ingest — token to trading symbol."""
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        effective = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[
            0
        ]
        rows = connection.execute(
            "SELECT instrument_token, tradingsymbol FROM instrument_master WHERE ingested_on = ?",
            (effective,),
        ).fetchall()
    return {int(token): str(symbol) for token, symbol in rows}


def backfill(
    *,
    session_date: date,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT,
    limit: int | None = None,
) -> BackfillOutcome:
    """Fetch and store every 5-minute bar for the instruments the tape recorded that day."""
    kite = build_authenticated_kite_client_if_valid()
    if kite is None:
        raise SystemExit(
            "no valid Kite session; run the TOTP login first — this script will not invent bars"
        )
    names = tradeable_names(market_data)
    tokens = [
        token
        for token in instrument_tokens_recorded_in_the_depth_tape(session_date, tape_root)
        if token in names
    ]
    if limit is not None:
        tokens = tokens[:limit]

    connection = sqlite3.connect(market_data)
    written = 0
    already = 0
    answered = 0
    failures: list[tuple[int, str]] = []
    interval_seconds = 1.0 / REQUESTS_PER_SECOND
    for index, token in enumerate(tokens, start=1):
        try:
            rows = kite.historical_data(
                token, session_date, session_date, KITE_INTERVAL_NAME, continuous=False, oi=False
            )
        except Exception as failure:  # noqa: BLE001 — every broker failure is reported, never raised
            failures.append((token, str(failure)[:200]))
            time.sleep(interval_seconds)
            continue
        if rows:
            answered += 1
        for row in rows:
            stamp: datetime = row["date"]
            cursor = connection.execute(
                "INSERT OR IGNORE INTO price_bars (instrument_token, bar_interval, bar_timestamp, "
                "open_price, high_price, low_price, close_price, volume, open_interest, "
                "availability_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    token,
                    BAR_INTERVAL,
                    stamp.isoformat(),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["volume"]),
                    (stamp + INTERVAL).isoformat(),
                ),
            )
            if cursor.rowcount:
                written += 1
            else:
                already += 1
        connection.commit()
        if index % 100 == 0:
            print(
                f"  {index}/{len(tokens)} instruments, {written} bars written",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(interval_seconds)
    connection.close()
    return BackfillOutcome(
        session_date=session_date,
        instruments_attempted=len(tokens),
        instruments_answered=answered,
        bars_written=written,
        bars_already_present=already,
        failures=failures,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_date", help="the trading day to backfill, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=None, help="probe only this many instruments")
    parser.add_argument("--market-data", type=Path, default=DEFAULT_MARKET_DATA_PATH)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_DEPTH_TAPE_ROOT)
    arguments = parser.parse_args()
    outcome = backfill(
        session_date=date.fromisoformat(arguments.session_date),
        market_data=arguments.market_data,
        tape_root=arguments.tape_root,
        limit=arguments.limit,
    )
    print(outcome.describe())
    for token, reason in outcome.failures[:20]:
        print(f"  FAILED {token}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
