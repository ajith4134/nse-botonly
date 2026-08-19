#!/usr/bin/env python
"""`R.05` real-data verification for the F&O capture universe (`A.142` / `A.146c`).

Answers three questions with measurements rather than assertions:

1. **Does the selection produce subscribable F&O tokens at all?** — the thing that has never been
   true on this box: measured 2026-08-19, the depth tape held 2,295 tokens and every one was cash.
2. **Does the within-population merge keep cash coverage?** — the failure the merger exists to
   prevent. An option's turnover is notional exposure and a cash trade's is money changing hands, so
   a raw rupee sort puts every option above every equity.
3. **What would the admission controller actually admit, per segment?** — run against the REAL
   measured packet rates and the REAL byte width from the tape, not a guess.

Reads only. Writes nothing, subscribes nothing, and never contacts the broker: the instrument master
is read from the local store, so this runs with the market open or closed.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_algo_trader.market_depth.capture_candidate_population_merger import (  # noqa: E402
    CaptureCandidateEntry,
    CaptureCandidatePopulation,
    merge_capture_candidate_populations,
)
from nse_algo_trader.market_depth.depth_capture_admission_controller import (  # noqa: E402
    DepthCaptureAdmissionController,
    InstrumentCaptureCandidate,
)
from nse_algo_trader.market_depth.derivative_capture_universe_selector import (  # noqa: E402
    select_derivative_capture_universe,
)
from nse_algo_trader.market_depth.market_depth_tape_store import (  # noqa: E402
    MarketDepthTapeReader,
)

IST = ZoneInfo("Asia/Kolkata")
MARKET_DATA = Path.home() / ".nse_algo_trader" / "market_data.sqlite3"
TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")

KITE_MAX_WEBSOCKET_CONNECTIONS_PER_API_KEY = 3
KITE_MAX_INSTRUMENTS_PER_CONNECTION = 3000

DERIVATIVE_POPULATION_BY_CONTRACT_TYPE = {
    "IDO": "index_options",
    "STO": "stock_options",
    "IDF": "index_futures",
    "STF": "stock_futures",
}


def cash_entries_from_the_local_master() -> list[CaptureCandidateEntry]:
    """Every NSE equity in the latest instrument master, valued by its own last traded value.

    The live capture reads turnover from the cash bhavcopy archive; this reads the same quantity
    from the store so the probe needs no broker session. The RANK is what the merge consumes and
    both sources rank the same population the same way.
    """
    connection = sqlite3.connect(f"file:{MARKET_DATA}?mode=ro", uri=True)
    try:
        latest = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[0]
        rows = connection.execute(
            "SELECT DISTINCT instrument_token FROM instrument_master "
            "WHERE ingested_on = ? AND segment = 'NSE' AND instrument_type = 'EQ'",
            (latest,),
        ).fetchall()
        turnover = _cash_turnover_by_token(connection)
    finally:
        connection.close()
    return [
        CaptureCandidateEntry(
            instrument_token=int(token),
            liquidity_in_its_own_units=float(turnover.get(int(token), 0.0)),
        )
        for (token,) in rows
    ]


def _cash_turnover_by_token(connection: sqlite3.Connection) -> dict[int, float]:
    """Traded value per equity token from `price_bars` — the store's own record of cash activity.

    `price_bars` is keyed by instrument token, so no symbol join is needed. Five sessions of bars,
    because one session's volume on a single name is a sample of one.
    """
    rows = connection.execute(
        """
        SELECT instrument_token, SUM(volume * close_price)
        FROM price_bars
        WHERE bar_timestamp >= DATE('now', '-5 days')
        GROUP BY instrument_token
        """
    ).fetchall()
    return {int(token): float(value or 0.0) for token, value in rows}


def main() -> int:
    now = datetime.now(IST)
    print(f"as of {now.isoformat()}\n")

    print("== 1. what the tape holds today, by segment ==")
    _report_tape_segments(now)

    print("\n== 2. the F&O selection ==")
    derivatives = select_derivative_capture_universe(as_of=now.date())
    print(f"  {derivatives.note}")
    print(
        f"  futures {derivatives.future_count:,} · options {derivatives.option_count:,} · "
        f"underlyings {len(derivatives.underlyings):,} · session {derivatives.source_session}"
    )
    if not derivatives.contracts:
        print("  FAIL — the selection is empty; the derivative bots cannot get an intraday tape")
        return 1

    print("\n== 3. the merge, and whether cash survives it ==")
    cash = cash_entries_from_the_local_master()
    derivative_entries: dict[str, list[CaptureCandidateEntry]] = {
        name: [] for name in DERIVATIVE_POPULATION_BY_CONTRACT_TYPE.values()
    }
    cash_tokens = {entry.instrument_token for entry in cash}
    for contract in derivatives.contracts:
        if contract.instrument_token in cash_tokens:
            continue
        derivative_entries[DERIVATIVE_POPULATION_BY_CONTRACT_TYPE[contract.contract_type]].append(
            CaptureCandidateEntry(contract.instrument_token, contract.traded_value)
        )
    populations = [CaptureCandidatePopulation("cash", cash)] + [
        CaptureCandidatePopulation(name, entries)
        for name, entries in sorted(derivative_entries.items())
    ]
    merged = merge_capture_candidate_populations(populations)
    print(f"  {merged.describe()}")

    raw_sort_top = sorted(
        [("cash", entry.liquidity_in_its_own_units) for entry in cash]
        + [
            (DERIVATIVE_POPULATION_BY_CONTRACT_TYPE[c.contract_type], c.traded_value)
            for c in derivatives.contracts
        ],
        key=lambda item: -item[1],
    )[:1000]
    cash_in_a_raw_sort = sum(1 for population, _ in raw_sort_top if population == "cash")
    cash_in_the_merge = sum(
        1 for candidate in merged.candidates[:1000] if merged.population_by_token[
            candidate.instrument_token
        ] == "cash"
    )
    print(
        f"  cash instruments in the top 1,000: raw rupee sort {cash_in_a_raw_sort:,} · "
        f"within-population merge {cash_in_the_merge:,}"
    )

    print("\n== 4. what the real admission controller would admit ==")
    _report_admission(merged, now)
    return 0


def _report_tape_segments(now: datetime) -> None:
    import duckdb

    shards = sorted((TAPE_ROOT / f"session_date={now.date().isoformat()}").rglob("*.parquet"))
    if not shards:
        print("  no shards for today yet")
        return
    connection = duckdb.connect()
    connection.execute("INSTALL sqlite; LOAD sqlite;")
    connection.execute(f"ATTACH '{MARKET_DATA}' AS md (TYPE sqlite, READ_ONLY)")
    rows = connection.execute(
        f"""
        SELECT COALESCE(m.segment, 'UNRESOLVED') AS segment, COUNT(DISTINCT t.instrument_token)
        FROM (SELECT DISTINCT instrument_token FROM read_parquet({[str(s) for s in shards]!r})) AS t
        LEFT JOIN (SELECT DISTINCT instrument_token, segment FROM md.instrument_master) AS m
          ON m.instrument_token = t.instrument_token
        GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    for segment, count in rows:
        print(f"  {segment:14s} {count:,} token(s)")


def _report_admission(merged: object, now: datetime) -> None:
    reader = MarketDepthTapeReader(TAPE_ROOT)
    bytes_per_row = reader.measured_bytes_per_row()
    if bytes_per_row is None:
        print("  the tape has never been written — the controller would calibrate, not solve")
        return
    close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    remaining = max((close - now).total_seconds(), 3600.0)
    controller = DepthCaptureAdmissionController(
        tape_root=TAPE_ROOT,
        retention_sessions=10,
        disk_budget_fraction=0.30,
        calibration_cohort_size=300,
    )
    rates = _measured_rates()
    candidates = [
        InstrumentCaptureCandidate(
            instrument_token=candidate.instrument_token,
            liquidity_value=candidate.liquidity_value,
            measured_packets_per_second=rates.get(candidate.instrument_token),
        )
        for candidate in merged.candidates  # type: ignore[attr-defined]
    ]
    decision = controller.solve(
        candidates,
        remaining,
        bytes_per_row,
        already_used_bytes=reader.total_bytes_on_disk(),
        connection_ceiling=(
            KITE_MAX_WEBSOCKET_CONNECTIONS_PER_API_KEY * KITE_MAX_INSTRUMENTS_PER_CONNECTION
        ),
    )
    admitted = set(decision.admitted_tokens)
    print(
        f"  admitted {decision.admitted_count:,} of {len(candidates):,} · projected "
        f"{decision.projected_session_bytes / 1024**3:.2f} GiB of "
        f"{decision.budget_bytes / 1024**3:.2f} GiB ({decision.budget_utilization:.0%})"
    )
    for note in decision.notes:
        print(f"  note: {note}")
    for population, size in sorted(merged.size_by_population.items()):  # type: ignore[attr-defined]
        if size == 0:
            print(f"  {population:16s} 0 of 0 — nothing to capture")
            continue
        kept = sum(
            1
            for token in merged.tokens_of(population)  # type: ignore[attr-defined]
            if token in admitted
        )
        print(f"  {population:16s} {kept:,} of {size:,} ({kept / size:.0%})")


def _measured_rates() -> dict[int, float]:
    """Per-instrument packets/s from the most recent session that has a tape."""
    import duckdb

    for back in range(0, 12):
        day = (datetime.now(IST) - timedelta(days=back)).date()
        shards = sorted((TAPE_ROOT / f"session_date={day.isoformat()}").rglob("*.parquet"))
        if not shards:
            continue
        connection = duckdb.connect()
        rows = connection.execute(
            f"""
            SELECT instrument_token,
                   COUNT(*) / GREATEST(
                       EXTRACT(EPOCH FROM (MAX(receipt_time) - MIN(receipt_time))), 1) AS rate
            FROM read_parquet({[str(s) for s in shards]!r})
            GROUP BY instrument_token
            """
        ).fetchall()
        if rows:
            print(f"  packet rates measured from {day.isoformat()} ({len(rows):,} instruments)")
            return {int(token): float(rate) for token, rate in rows}
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
