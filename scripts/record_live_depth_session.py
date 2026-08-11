"""Run a live order-book depth capture for the rest of today's session.

Two phases, because on a fresh tape neither the compressed row width nor the
per-instrument packet rates exist yet and `R.03` forbids substituting a guess:

1. **Calibration** — a short capture of the most liquid cohort, whose only job is to
   let the tape measure its own bytes/row and each instrument's own packet rate.
2. **Full session** — the admission controller solves the capture universe from those
   measurements and the live free disk, and the recorder runs to the close.

Liquidity ranking comes from the most recent bhavcopy in the local archive — real
traded value, not a hand-ordered list of favourites.

Usage:
    python scripts/record_live_depth_session.py --retention-sessions 30 \
        --disk-budget-fraction 0.40 --calibration-minutes 5
"""

from __future__ import annotations

import argparse
import csv
import io
import queue
import signal
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

from nse_algo_trader.broker_credentials import (
    BrokerName,
    load_broker_api_credentials,
    load_env_file_into_environ,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    KiteAccessTokenFileStore,
)
from nse_algo_trader.market_depth.depth_capture_admission_controller import (
    DepthCaptureAdmissionController,
    InstrumentCaptureCandidate,
)
from nse_algo_trader.market_depth.depth_packet_integrity_classifier import (
    NSE_QUOTING_WINDOW_CLOSES_IST,
    DepthPacketIntegrityClassifier,
)
from nse_algo_trader.market_depth.live_depth_feed_seam import KiteLiveDepthFeed
from nse_algo_trader.market_depth.live_order_book_depth_recorder import (
    LiveOrderBookDepthRecorder,
    ShardCaptureStatistics,
    ShardRuntime,
)
from nse_algo_trader.market_depth.market_depth_tape_store import (
    MarketDepthTapeReader,
    MarketDepthTapeStore,
)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")
BHAVCOPY_CASH_ARCHIVE = Path("/home/opc/nse_archive/cash")
KITE_MAX_WEBSOCKET_CONNECTIONS_PER_API_KEY = 3
"""Kite's documented per-key socket ceiling — an external constraint, not a choice.
With 3,000 instruments per socket this caps any single-key capture at 9,000."""

QUEUE_CAPACITY_PER_SHARD = 20_000
"""Bounded so a slow disk cannot grow into an out-of-memory kill six hours in.
Overflow past this is counted per instrument and reported, never silent."""


def log(message: str) -> None:
    print(f"[{datetime.now(IST):%H:%M:%S}] {message}", flush=True)


def latest_cash_bhavcopy() -> Path | None:
    candidates = sorted(BHAVCOPY_CASH_ARCHIVE.rglob("cash_*.csv.zip"))
    return candidates[-1] if candidates else None


def traded_value_by_symbol(bhavcopy_path: Path) -> dict[str, float]:
    """Symbol -> total traded value from a bhavcopy — the liquidity ranking's source."""
    with zipfile.ZipFile(bhavcopy_path) as archive:
        text = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
    values: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("TckrSymb") or "").strip()
        series = (row.get("SctySrs") or "").strip()
        if not symbol or series != "EQ":
            continue
        try:
            values[symbol] = max(values.get(symbol, 0.0), float(row.get("TtlTrfVal") or 0))
        except ValueError:
            continue
    return values


def build_candidates(
    kite: KiteConnect,
) -> tuple[list[InstrumentCaptureCandidate], dict[int, str]]:
    """Every NSE equity Kite will quote, valued by yesterday's real traded value."""
    instruments = kite.instruments("NSE")
    equities = [
        instrument
        for instrument in instruments
        if instrument["segment"] == "NSE" and instrument["instrument_type"] == "EQ"
    ]
    bhavcopy_path = latest_cash_bhavcopy()
    if bhavcopy_path is None:
        raise SystemExit("no cash bhavcopy in the archive — cannot rank liquidity")
    log(f"liquidity ranking from {bhavcopy_path.name}")
    turnover = traded_value_by_symbol(bhavcopy_path)
    log(f"{len(turnover)} symbols carry a traded value")

    exchange_by_token: dict[int, str] = {}
    candidates: list[InstrumentCaptureCandidate] = []
    for instrument in equities:
        token = int(instrument["instrument_token"])
        exchange_by_token[token] = "NSE"
        candidates.append(
            InstrumentCaptureCandidate(
                instrument_token=token,
                liquidity_value=turnover.get(instrument["tradingsymbol"], 0.0),
            )
        )
    ranked = sorted(candidates, key=lambda c: -c.liquidity_value)
    log(f"{len(ranked)} NSE equities are candidates; top value {ranked[0].liquidity_value:,.0f}")
    return ranked, exchange_by_token


def measured_rates_from_tape(tape_root: Path, session_date: datetime) -> dict[int, float]:
    """Per-instrument packets/s, each over its own observed span in today's tape."""
    reader = MarketDepthTapeReader(tape_root)
    try:
        return reader.instrument_packet_rates(session_date.date())
    except Exception as read_failure:  # noqa: BLE001 — reported, then treated as no data
        log(f"could not read rates back from the tape: {read_failure}")
        return {}


def make_shards(
    tokens: list[int],
    kite_api_key: str,
    access_token: str,
    exchange_by_token: dict[int, str],
    tape_root: Path,
    session_date: datetime,
    first_shard_index: int,
    max_buffered_rows: int,
    max_seconds_between_flushes: float,
    capture_run_id: str,
) -> list[ShardRuntime]:
    """One feed, store and queue per Kite connection's worth of instruments."""
    per_connection = KiteLiveDepthFeed.KITE_MAX_INSTRUMENTS_PER_CONNECTION
    shards: list[ShardRuntime] = []
    for offset in range(0, len(tokens), per_connection):
        shard_tokens = tokens[offset : offset + per_connection]
        shard_index = first_shard_index + len(shards)
        feed = KiteLiveDepthFeed(
            kite_api_key=kite_api_key,
            kite_access_token=access_token,
            exchange_by_token=exchange_by_token,
        )
        feed.subscribe(shard_tokens)
        shards.append(
            ShardRuntime(
                shard_index=shard_index,
                feed=feed,
                store=MarketDepthTapeStore(
                    tape_root=tape_root,
                    session_date=session_date.date(),
                    shard_index=shard_index,
                    max_buffered_rows=max_buffered_rows,
                    max_seconds_between_flushes=max_seconds_between_flushes,
                    capture_run_id=capture_run_id,
                ),
                packet_queue=queue.Queue(maxsize=QUEUE_CAPACITY_PER_SHARD),
                statistics=ShardCaptureStatistics(),
            )
        )
    return shards


def report(recorder: LiveOrderBookDepthRecorder, label: str) -> None:
    written = recorder.total_packets_written()
    dropped = recorder.total_packets_dropped()
    log(f"{label}: {written:,} packets written, {dropped:,} dropped to overflow")
    for shard_index, statistics in recorder.capture_statistics_by_shard().items():
        flags = ", ".join(
            f"{flag.name}={count:,}" for flag, count in sorted(
                statistics.flag_counts.items(), key=lambda item: -item[1]
            )
        )
        log(
            f"  shard {shard_index:02d}: {statistics.packets_written:,} rows"
            f" | {flags or 'no flags'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_TAPE_ROOT)
    parser.add_argument(
        "--retention-sessions",
        type=int,
        required=True,
        help="how many sessions of tape to keep on disk — the one genuine policy input",
    )
    parser.add_argument(
        "--disk-budget-fraction",
        type=float,
        required=True,
        help="fraction of free disk the tape may occupy",
    )
    parser.add_argument("--calibration-minutes", type=float, default=5.0)
    parser.add_argument("--calibration-cohort-size", type=int, default=300)
    parser.add_argument("--max-buffered-rows", type=int, default=20_000)
    parser.add_argument("--max-seconds-between-flushes", type=float, default=60.0)
    arguments = parser.parse_args()

    load_env_file_into_environ()
    credentials = load_broker_api_credentials(BrokerName.ZERODHA_KITE)
    token_record = KiteAccessTokenFileStore().load_if_still_valid()
    if token_record is None:
        log("no valid Kite access token — run the daily TOTP login first")
        return 2

    kite = KiteConnect(api_key=credentials.api_key)
    kite.set_access_token(token_record.access_token)

    now_ist = datetime.now(IST)
    # Every recorder gets its own partition. Two captures can then never share a
    # directory, whatever the operator does — the 2026-08-11 fault made unlikely
    # impossible instead of merely unlikely.
    capture_run_id = now_ist.strftime("%H%M%S")
    session_close = datetime.combine(
        now_ist.date(), NSE_QUOTING_WINDOW_CLOSES_IST, IST
    )
    if now_ist >= session_close:
        log(f"session already closed at {session_close:%H:%M} IST — nothing to capture")
        return 1
    log(f"session closes at {session_close:%H:%M} IST, {session_close - now_ist} remaining")
    log(f"capture run id {capture_run_id}")

    candidates, exchange_by_token = build_candidates(kite)
    controller = DepthCaptureAdmissionController(
        tape_root=arguments.tape_root,
        retention_sessions=arguments.retention_sessions,
        disk_budget_fraction=arguments.disk_budget_fraction,
        calibration_cohort_size=arguments.calibration_cohort_size,
    )
    arguments.tape_root.mkdir(parents=True, exist_ok=True)
    classifier = DepthPacketIntegrityClassifier()

    # ---------------------------------------------------------------- calibration
    reader = MarketDepthTapeReader(arguments.tape_root)
    already_used_bytes = reader.total_bytes_on_disk() if arguments.tape_root.exists() else 0
    bytes_per_row = reader.measured_bytes_per_row() if already_used_bytes else None
    calibration_seconds = arguments.calibration_minutes * 60

    if bytes_per_row is None:
        log(f"tape is unmeasured — calibrating on {arguments.calibration_cohort_size} instruments")
        calibration = controller.solve(
            candidates, (session_close - now_ist).total_seconds(), None
        )
        calibration_shards = make_shards(
            list(calibration.admitted_tokens),
            credentials.api_key,
            token_record.access_token,
            exchange_by_token,
            arguments.tape_root,
            now_ist,
            first_shard_index=0,
            max_buffered_rows=arguments.max_buffered_rows,
            max_seconds_between_flushes=arguments.max_seconds_between_flushes,
            capture_run_id=capture_run_id,
        )
        calibration_recorder = LiveOrderBookDepthRecorder(
            session_date=now_ist.date(),
            shards=calibration_shards,
            classifier=classifier,
            admission_decision=calibration,
            session_ends_at=min(
                datetime.now(UTC) + timedelta(seconds=calibration_seconds),
                session_close.astimezone(UTC),
            ),
        )
        calibration_recorder.start()
        calibration_recorder.run_until_session_end(poll_seconds=2.0)
        calibration_recorder.stop()
        report(calibration_recorder, "calibration")
        bytes_per_row = reader.measured_bytes_per_row()
        log(f"measured bytes/row = {bytes_per_row}")

    if bytes_per_row is None:
        log("calibration produced no rows — the feed delivered nothing; aborting")
        return 3

    # -------------------------------------------------------------- full session
    observed_rates = measured_rates_from_tape(arguments.tape_root, now_ist)
    log(f"{len(observed_rates)} instruments have a measured packet rate")
    valued = [
        InstrumentCaptureCandidate(
            instrument_token=candidate.instrument_token,
            liquidity_value=candidate.liquidity_value,
            measured_packets_per_second=observed_rates.get(candidate.instrument_token),
        )
        for candidate in candidates
    ]

    now_ist = datetime.now(IST)
    remaining_seconds = (session_close - now_ist).total_seconds()
    if remaining_seconds <= 0:
        log("session closed during calibration")
        return 0

    connection_ceiling = (
        KITE_MAX_WEBSOCKET_CONNECTIONS_PER_API_KEY
        * KiteLiveDepthFeed.KITE_MAX_INSTRUMENTS_PER_CONNECTION
    )
    decision = controller.solve(
        valued,
        remaining_seconds,
        bytes_per_row,
        already_used_bytes=reader.total_bytes_on_disk(),
        connection_ceiling=connection_ceiling,
    )
    log(
        f"admitted {decision.admitted_count:,} of {len(valued):,} instruments | "
        f"projected {decision.projected_session_bytes / 1024**3:.2f} GiB of a "
        f"{decision.budget_bytes / 1024**3:.2f} GiB budget "
        f"({decision.budget_utilization:.0%})"
    )
    for note in decision.notes:
        log(f"  note: {note}")

    shards = make_shards(
        list(decision.admitted_tokens),
        credentials.api_key,
        token_record.access_token,
        exchange_by_token,
        arguments.tape_root,
        now_ist,
        first_shard_index=1,
        max_buffered_rows=arguments.max_buffered_rows,
        max_seconds_between_flushes=arguments.max_seconds_between_flushes,
        capture_run_id=capture_run_id,
    )
    log(f"{len(shards)} shard(s), {decision.admitted_count:,} instruments")

    recorder = LiveOrderBookDepthRecorder(
        session_date=now_ist.date(),
        shards=shards,
        classifier=classifier,
        admission_decision=decision,
        session_ends_at=session_close.astimezone(UTC),
    )

    def handle_signal(signal_number: int, _frame: FrameType | None) -> None:
        log(f"signal {signal_number} — ending the session cleanly")
        recorder.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    recorder.start()
    log("capturing")
    try:
        recorder.run_until_session_end(poll_seconds=30.0)
    finally:
        recorder.stop()
    report(recorder, "session")
    log(f"tape now holds {reader.total_bytes_on_disk() / 1024**3:.2f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
