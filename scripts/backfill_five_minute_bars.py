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
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pyarrow.compute as compute
import pyarrow.dataset as arrow_dataset

from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)
from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    FIVE_MINUTE_BAR_INTERVAL,
    ensure_price_basis_column,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

DEFAULT_MARKET_DATA_PATH = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
DEFAULT_DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
INDIA_STANDARD_TIME = ZoneInfo("Asia/Kolkata")
"""The exchange's clock. The basis is a calendar date in the market's timezone, never in the
host's — this box runs GMT (`H1`)."""

BAR_INTERVAL = FIVE_MINUTE_BAR_INTERVAL
"""One definition, imported from the provenance module, so the label this script WRITES and the
label its readers QUERY cannot diverge (`L0.37`)."""
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
    # Enumerate the parquet parts rather than handing pyarrow the directory. The live recorder
    # writes a `<hhmmss>_capture_liveness.json` sidecar into the session partition, and pyarrow
    # treats every file under a directory as parquet — so this raised
    # `Parquet magic bytes not found in footer` for any session where a capture had run, which is
    # every session this function exists to serve.
    #
    # The identical defect was fixed in `MarketDepthTapeReader` earlier the same day (2026-08-17)
    # and NOT looked for anywhere else. It is scheduled code: the daily-operations five-minute
    # backfill step would have failed tonight for exactly this reason. Fixing an instance is not
    # fixing the class, and the class here is "a directory handed to pyarrow".
    parts = sorted(
        part
        for part in partition.rglob("*.parquet")
        if part.is_file() and not part.name.startswith((".", "_"))
    )
    if not parts:
        raise SystemExit(f"no completed parquet parts under {partition}")
    table = arrow_dataset.dataset(parts, format="parquet").to_table(columns=["instrument_token"])
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


def cash_equity_universe(market_data: Path) -> tuple[int, ...]:
    """Every NSE CASH equity in the instrument master's latest ingest.

    **The universe a five-minute backfill owes, and it must not be the depth tape's** (`A.127`).
    The tape's instrument set is chosen by a DISK BUDGET — `admitted 652 of 9,891 instruments |
    projected 0.33 GiB of a 0.33 GiB budget` — so making it the backfill's universe made bar
    coverage hostage to how much disk the capture happened to get, and to whether the capture ran
    at all. 2026-08-14 was a trading Friday with no capture, and `price_bars` holds **zero** rows
    for it; the daily run would have reported "no universe to fetch" for ever.

    Measured: 10,197 tokens, roughly 113 minutes at the script's own paced rate — comfortable for
    an evening run against a closed market, and the derived timeout already allows for it.

    Filtered by exchange and instrument type rather than by name pattern (`R.14`, and the same
    discriminator `_bar_instruments_due` settled on): `EQ` on `NSE` is the cash board.
    """
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        effective = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[
            0
        ]
        rows = connection.execute(
            "SELECT instrument_token FROM instrument_master "
            "WHERE ingested_on = ? AND exchange = 'NSE' AND instrument_type = 'EQ'",
            (effective,),
        ).fetchall()
    return tuple(sorted(int(row[0]) for row in rows))


def universe_for(
    session_date: date,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT,
) -> tuple[int, ...]:
    """What this session's backfill should fetch: the cash board, plus anything the tape recorded.

    The union matters in both directions. The cash board alone would miss an instrument the
    capture recorded that is not `NSE`/`EQ`; the tape alone misses every instrument the capture had
    no disk for, which is most of them. Neither is a superset of the other by construction, so the
    honest universe is both.
    """
    universe = set(cash_equity_universe(market_data))
    # No tape partition for this session is a fact about the CAPTURE, and it must not decide
    # whether the bar store gets a session at all — the defect `A.127` fixes.
    with suppress(SystemExit):
        universe.update(instrument_tokens_recorded_in_the_depth_tape(session_date, tape_root))
    return tuple(sorted(universe))


class HistoricalBarFetcher(Protocol):
    """The one call this script makes of a broker — named so a test can answer it."""

    def historical_data(
        self,
        instrument_token: int,
        from_date: date,
        to_date: date,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]: ...


def backfill(
    *,
    session_date: date,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT,
    limit: int | None = None,
    kite: HistoricalBarFetcher | None = None,
    tokens: Sequence[int] | None = None,
    fetched_on: date | None = None,
) -> BackfillOutcome:
    """Fetch and store every 5-minute bar for the instruments the tape recorded that day.

    `kite`, `tokens` and `fetched_on` are DI SEAMS, added by the `A.126` review (`R.J`). Three
    mutations of this function's own write — stamping the session date instead of the fetch
    date, stamping `NULL`, and omitting the basis column — survived the whole suite, because
    nothing could call it without a live broker and a recorded depth tape. Each of those makes
    the feature above it a rubber stamp. A hermetic harness here does NOT satisfy `R.05`; it
    makes the writer testable at all.
    """
    if kite is None:
        kite = build_authenticated_kite_client_if_valid()
    if kite is None:
        raise SystemExit(
            "no valid Kite session; run the TOTP login first — this script will not invent bars"
        )
    if tokens is None:
        tokens = list(universe_for(session_date, market_data, tape_root))
    tokens = list(tokens)
    if limit is not None:
        tokens = tokens[:limit]

    connection = sqlite3.connect(market_data)
    # `L0.37`. Record WHAT the prices mean, not only what they are. Kite adjusts its historical
    # series as of the moment it is asked, so the fetch date IS the adjustment basis — knowable
    # here, at write time, with no corporate-action feed. Without it a bar backfilled after an
    # ex-date is indistinguishable from one fetched on the day, and `HINDPETRO` sat 4.9% away from
    # the traded price on 150 of 150 comparable bars with nothing in the schema able to say so.
    ensure_price_basis_column(connection)
    # **IST, not the machine's clock** (`H1` of the `A.126` review). This box runs GMT and the
    # market runs IST, so `date.today()` is the UTC date; a run between 18:30 and 24:00 UTC would
    # stamp tomorrow's date as the basis of today's session and the feature would mislabel its own
    # writes as adjusted-after-the-session.
    fetched_on_date = fetched_on or datetime.now(INDIA_STANDARD_TIME).date()
    fetched_on_iso = fetched_on_date.isoformat()
    if fetched_on_date != session_date:
        # The daily timer fires twice — 19:00 IST the same day, and 08:15 IST the NEXT morning as a
        # catch-up. The morning firing targets yesterday's session, so its writes are genuinely on
        # a later basis. Said out loud rather than discovered from a dashboard tile, because the
        # spec's "traded basis by construction" holds only for the same-day run.
        print(
            f"NOTE: fetching {session_date.isoformat()} on {fetched_on_iso} — these bars are NOT "
            f"on the traded basis and are recorded as adjusted-after-the-session",
            file=sys.stderr,
        )
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
                "availability_time, adjustment_basis_as_of) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
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
                    fetched_on_iso,
                ),
            )
            if cursor.rowcount:
                written += 1
            else:
                already += 1
                # `H4`. `INSERT OR IGNORE` touches nothing when the bar already exists, so a
                # re-run could never annotate a row written before `L0.37` — the three sessions
                # that have a depth tape are exactly those rows, and they would have stayed
                # UNKNOWN for ever. Annotating is safe ONLY when this run fetched the session on
                # its own day: the source served the traded series today, and this row is that
                # series. On any other day the basis genuinely is later and the NULL stands.
                if fetched_on_date == session_date:
                    connection.execute(
                        "UPDATE price_bars SET adjustment_basis_as_of = ? "
                        "WHERE instrument_token = ? AND bar_interval = ? AND bar_timestamp = ? "
                        "AND adjustment_basis_as_of IS NULL",
                        (fetched_on_iso, token, BAR_INTERVAL, stamp.isoformat()),
                    )
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


NSE_SESSION_CLOSE_INDIA_STANDARD_TIME = dt_time(15, 30)
"""When the cash session closes. A market fact, sourced from the NSE equity timings circular."""


def most_recent_closed_session(
    now_india_standard_time: datetime, calendar: NseTradingSessionCalendar
) -> date:
    """The latest session whose bars can actually exist yet.

    Duplicated in shape from `run_daily_operations.py` on purpose rather than imported: this script
    is the CHILD in that relationship — the runner imports `universe_for` from here — and importing
    back would make the pair circular. The rule itself is one line and is tested here.

    Today counts only once trading has closed; before that the scheduled unit would ask Kite for a
    session that has not traded.
    """
    candidate = now_india_standard_time.date()
    if now_india_standard_time.time() < NSE_SESSION_CLOSE_INDIA_STANDARD_TIME:
        candidate -= timedelta(days=1)
    while not calendar.is_trading_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "session_date",
        nargs="?",
        default=None,
        help=(
            "the trading day to backfill, YYYY-MM-DD. Omitted, it derives the most recent CLOSED "
            "session, which is what the scheduled unit passes — a timer that had to be told the "
            "date would be wrong on the first holiday"
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="probe only this many instruments")
    parser.add_argument("--market-data", type=Path, default=DEFAULT_MARKET_DATA_PATH)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_DEPTH_TAPE_ROOT)
    arguments = parser.parse_args()
    session_date = (
        date.fromisoformat(arguments.session_date)
        if arguments.session_date
        else most_recent_closed_session(
            datetime.now(INDIA_STANDARD_TIME), NseTradingSessionCalendar()
        )
    )
    outcome = backfill(
        session_date=session_date,
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
