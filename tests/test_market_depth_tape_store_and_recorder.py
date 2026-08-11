"""Tape-store durability and read-back, admission control, and the session loop.

The recorder is exercised end to end through a deterministic fake feed behind the
`LiveDepthFeed` seam (`Rule J`), so the session lifecycle is verified with no socket
and no open market. That verification is functional only — it never substitutes for
the `R.05` real-data pass against the live socket.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as parquet
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as strategy

from nse_algo_trader.market_depth.depth_capture_admission_controller import (
    AdmissionControlError,
    DepthCaptureAdmissionController,
    InstrumentCaptureCandidate,
)
from nse_algo_trader.market_depth.depth_packet_integrity_classifier import (
    DepthPacketIntegrityClassifier,
)
from nse_algo_trader.market_depth.depth_tape_schema import (
    DepthLevel,
    DepthPacket,
    IntegrityFlag,
)
from nse_algo_trader.market_depth.live_depth_feed_seam import DepthPacketHandler
from nse_algo_trader.market_depth.live_order_book_depth_recorder import (
    DepthRecorderError,
    LiveOrderBookDepthRecorder,
    ShardCaptureStatistics,
    ShardRuntime,
)
from nse_algo_trader.market_depth.market_depth_tape_store import (
    DepthTapeStoreError,
    MarketDepthTapeReader,
    MarketDepthTapeStore,
)

SESSION_DATE = date(2026, 8, 11)
BASE_TIME = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)

ROUND_TRIP_PACKET_COUNT = 50
BOOK_AT_EXPECTED_SEQUENCE = 5
BOOK_AT_EXPECTED_PRICE_PAISE = 125_005
SIGKILL_RETURN_CODE = -9
SURVIVING_ROW_COUNT = 30
PARTS_WRITTEN = 5
ROWS_ACROSS_PARTS = 45
CALIBRATION_COHORT_SIZE = 5
COHORT_MEDIAN_RATE = 0.5
KITE_CONNECTION_CEILING = 3_000
SESSION_PACKET_COUNT = 200
LEAST_LIQUID_TOKEN = 30
MOST_LIQUID_TOKEN = 20
NEWER_RUN_PRICE_PAISE = 200_000
IN_FLIGHT_PACKET_COUNT = 100
MINIMUM_ATOMIC_RENAMES = 2
MINIMUM_FSYNC_CALLS = 2
SINGLE_SIGHTING_TOKEN = 999


def _packet(token: int = 738561, sequence: int = 1, price: int = 125_000) -> DepthPacket:
    return DepthPacket(
        instrument_token=token,
        exchange="NSE",
        exchange_time=BASE_TIME + timedelta(seconds=sequence),
        receipt_time=BASE_TIME + timedelta(seconds=sequence, milliseconds=40),
        receipt_sequence=sequence,
        last_price_paise=price,
        last_traded_quantity=10,
        average_traded_price_paise=price - 10,
        volume_traded=1_000 + sequence,
        total_buy_quantity=5_000,
        total_sell_quantity=4_000,
        open_interest=0,
        bids=tuple(DepthLevel(price - 5 - i * 5, 100 + i, 3) for i in range(5)),
        asks=tuple(DepthLevel(price + 5 + i * 5, 100 + i, 3) for i in range(5)),
    )


class DeterministicDepthFeed:
    """The `Rule J` fake: replays a scripted packet list, no socket involved."""

    def __init__(self, packets: Sequence[DepthPacket], tokens: Iterable[int] = ()) -> None:
        self._packets = list(packets)
        self._subscribed = set(tokens)
        self._handler: DepthPacketHandler | None = None
        self.stopped = False
        self.start_count = 0

    @property
    def subscribed_tokens(self) -> frozenset[int]:
        return frozenset(self._subscribed)

    def start(self, handler: DepthPacketHandler) -> None:
        self._handler = handler
        self.start_count += 1

    def emit_all(self, batch_size: int = 1) -> None:
        for index in range(0, len(self._packets), batch_size):
            self.deliver(self._packets[index : index + batch_size])

    def deliver(self, packets: Sequence[DepthPacket]) -> None:
        """Push one batch through the handler, as a real socket thread would.

        Exposed deliberately: several tests need to deliver frames at a moment
        `emit_all` cannot express — after `stop()`, or once a writer has died.
        """
        assert self._handler is not None
        self._handler(packets)

    def subscribe(self, instrument_tokens: Iterable[int]) -> None:
        self._subscribed.update(instrument_tokens)

    def unsubscribe(self, instrument_tokens: Iterable[int]) -> None:
        self._subscribed.difference_update(instrument_tokens)

    def stop(self) -> None:
        self.stopped = True


def _store(tmp_path: Path, **overrides: object) -> MarketDepthTapeStore:
    settings_for_store: dict[str, object] = {
        "tape_root": tmp_path,
        "session_date": SESSION_DATE,
        "shard_index": 0,
        "max_buffered_rows": 100,
        "max_seconds_between_flushes": 3600.0,
        "capture_run_id": "testrun",
    }
    settings_for_store.update(overrides)
    return MarketDepthTapeStore(**settings_for_store)  # type: ignore[arg-type]


# ------------------------------------------------------------------ tape store


@pytest.mark.unit
def test_rows_round_trip_through_the_tape(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        for sequence in range(1, ROUND_TRIP_PACKET_COUNT + 1):
            store.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(hours=1), SESSION_DATE
    )
    assert table.num_rows == ROUND_TRIP_PACKET_COUNT
    assert sorted(table.column("receipt_sequence").to_pylist()) == list(
        range(1, ROUND_TRIP_PACKET_COUNT + 1)
    )


@pytest.mark.unit
def test_close_flushes_the_buffered_tail(tmp_path: Path) -> None:
    """A tail lost at close is a tail lost every single session."""
    store = _store(tmp_path, max_buffered_rows=1_000_000)
    store.append(_packet(sequence=1), IntegrityFlag.NONE)
    assert store.rows_written == 0
    store.close()
    assert store.rows_written == 1


@pytest.mark.unit
def test_append_after_close_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.close()
    with pytest.raises(DepthTapeStoreError, match="append after close"):
        store.append(_packet(), IntegrityFlag.NONE)


@pytest.mark.unit
def test_bytes_per_row_is_unmeasured_until_the_first_flush(tmp_path: Path) -> None:
    """The admission controller must see None and calibrate, never a stand-in figure."""
    store = _store(tmp_path, max_buffered_rows=1_000_000)
    store.append(_packet(), IntegrityFlag.NONE)
    assert store.realized_bytes_per_row is None
    store.close()
    assert (store.realized_bytes_per_row or 0) > 0


@pytest.mark.unit
def test_integrity_flags_survive_the_round_trip(tmp_path: Path) -> None:
    flags = IntegrityFlag.BOOK_CROSSED | IntegrityFlag.EXCHANGE_TIME_ABSENT
    with _store(tmp_path) as store:
        store.append(_packet(), flags)
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(hours=1), SESSION_DATE
    )
    assert IntegrityFlag(table.column("integrity_flags")[0].as_py()) == flags


@pytest.mark.unit
def test_book_at_returns_the_last_row_at_or_before_the_instant(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        for sequence in range(1, 11):
            store.append(
                _packet(sequence=sequence, price=125_000 + sequence), IntegrityFlag.NONE
            )
    book = MarketDepthTapeReader(tmp_path).book_at(
        738561, BASE_TIME + timedelta(seconds=5, milliseconds=500), SESSION_DATE
    )
    assert book is not None
    assert book["receipt_sequence"] == BOOK_AT_EXPECTED_SEQUENCE
    assert book["last_price_paise"] == BOOK_AT_EXPECTED_PRICE_PAISE


@pytest.mark.unit
def test_book_at_is_none_before_the_first_packet(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.append(_packet(sequence=10), IntegrityFlag.NONE)
    assert (
        MarketDepthTapeReader(tmp_path).book_at(738561, BASE_TIME, SESSION_DATE) is None
    )


@pytest.mark.unit
def test_the_two_clocks_are_selectable_and_differ(tmp_path: Path) -> None:
    """A backtest filters on receipt time; an analysis filters on exchange time. The
    measured 11-minute stale packet is why these cannot be the same query."""
    stale = DepthPacket(
        instrument_token=1,
        exchange="NSE",
        exchange_time=BASE_TIME - timedelta(minutes=11),
        receipt_time=BASE_TIME + timedelta(seconds=1),
        receipt_sequence=1,
        last_price_paise=100,
        last_traded_quantity=1,
        average_traded_price_paise=100,
        volume_traded=1,
        total_buy_quantity=1,
        total_sell_quantity=1,
        open_interest=0,
        bids=tuple(DepthLevel(95 - i, 1, 1) for i in range(5)),
        asks=tuple(DepthLevel(105 + i, 1, 1) for i in range(5)),
    )
    with _store(tmp_path) as store:
        store.append(stale, IntegrityFlag.NONE)
    reader = MarketDepthTapeReader(tmp_path)
    by_receipt = reader.read_instrument_window(
        1, BASE_TIME, BASE_TIME + timedelta(minutes=1), SESSION_DATE, "receipt_time"
    )
    by_exchange = reader.read_instrument_window(
        1, BASE_TIME, BASE_TIME + timedelta(minutes=1), SESSION_DATE, "exchange_time"
    )
    assert by_receipt.num_rows == 1
    assert by_exchange.num_rows == 0


@pytest.mark.unit
def test_unknown_time_column_is_refused(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.append(_packet(), IntegrityFlag.NONE)
    with pytest.raises(DepthTapeStoreError, match="unknown time column"):
        MarketDepthTapeReader(tmp_path).read_instrument_window(
            738561, BASE_TIME, BASE_TIME + timedelta(hours=1), SESSION_DATE, "wall_time"
        )


# -------------------------------------------------------------- durability


@pytest.mark.adversarial
def test_in_flight_parts_are_invisible_to_a_concurrent_reader(tmp_path: Path) -> None:
    """A reader must see a consistent prefix, never a torn row group."""
    with _store(tmp_path) as store:
        store.append(_packet(sequence=1), IntegrityFlag.NONE)
    shard_directory = (
        tmp_path / f"session_date={SESSION_DATE.isoformat()}" / "capture_run=testrun" / "shard=00"
    )
    (shard_directory / ".part-000009.parquet.tmp").write_bytes(b"garbage not parquet")
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(hours=1), SESSION_DATE
    )
    assert table.num_rows == 1


@pytest.mark.adversarial
def test_a_failed_flush_leaves_no_partial_part_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, max_buffered_rows=1_000_000)
    store.append(_packet(), IntegrityFlag.NONE)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "nse_algo_trader.market_depth.market_depth_tape_store.parquet.ParquetWriter",
        explode,
    )
    with pytest.raises(OSError, match="disk full"):
        store.flush()
    shard_directory = (
        tmp_path / f"session_date={SESSION_DATE.isoformat()}" / "capture_run=testrun" / "shard=00"
    )
    assert list(shard_directory.glob("*.parquet")) == []
    assert list(shard_directory.glob(".*.tmp")) == []


@pytest.mark.adversarial
def test_a_killed_capture_keeps_every_already_flushed_part(tmp_path: Path) -> None:
    """`kill -9` mid-session is the expected end of an unattended six-hour capture,
    not an exotic case. Everything flushed before the kill must survive it."""
    script = f"""
import os, signal
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_market_depth_tape_store_and_recorder import _packet
from nse_algo_trader.market_depth.depth_tape_schema import IntegrityFlag
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeStore

store = MarketDepthTapeStore(
    tape_root=Path({str(tmp_path)!r}),
    session_date=date(2026, 8, 11),
    shard_index=0,
    max_buffered_rows=10,
    max_seconds_between_flushes=3600.0,
    capture_run_id="testrun",
)
for sequence in range(1, 36):
    store.append(_packet(sequence=sequence), IntegrityFlag.NONE)
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=120, check=False
    )
    assert result.returncode == SIGKILL_RETURN_CODE, result.stderr.decode()[-2000:]
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(hours=1), SESSION_DATE
    )
    # Three complete parts of ten rows each were flushed; the five buffered rows died
    # with the process. What survived must be complete and readable.
    assert table.num_rows == SURVIVING_ROW_COUNT
    assert sorted(table.column("receipt_sequence").to_pylist()) == list(range(1, 31))


@pytest.mark.adversarial
def test_every_flushed_part_is_a_complete_readable_parquet_file(tmp_path: Path) -> None:
    with _store(tmp_path, max_buffered_rows=10) as store:
        for sequence in range(1, 46):
            store.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    shard_directory = (
        tmp_path / f"session_date={SESSION_DATE.isoformat()}" / "capture_run=testrun" / "shard=00"
    )
    parts = sorted(shard_directory.glob("*.parquet"))
    assert len(parts) == PARTS_WRITTEN
    assert sum(parquet.ParquetFile(part).metadata.num_rows for part in parts) == ROWS_ACROSS_PARTS


@pytest.mark.property
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    packet_count=strategy.integers(min_value=1, max_value=200),
    buffer_bound=strategy.integers(min_value=1, max_value=50),
)
def test_every_appended_packet_appears_exactly_once(
    tmp_path_factory: pytest.TempPathFactory, packet_count: int, buffer_bound: int
) -> None:
    tape_root = tmp_path_factory.mktemp("tape")
    with _store(tape_root, max_buffered_rows=buffer_bound) as store:
        for sequence in range(1, packet_count + 1):
            store.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    table = MarketDepthTapeReader(tape_root).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(days=1), SESSION_DATE
    )
    assert sorted(table.column("receipt_sequence").to_pylist()) == list(
        range(1, packet_count + 1)
    )


# --------------------------------------------------------- admission control


def _controller(tmp_path: Path, **overrides: object) -> DepthCaptureAdmissionController:
    settings_for_controller: dict[str, object] = {
        "tape_root": tmp_path,
        "retention_sessions": 20,
        "disk_budget_fraction": 0.25,
        "calibration_cohort_size": 5,
    }
    settings_for_controller.update(overrides)
    return DepthCaptureAdmissionController(**settings_for_controller)  # type: ignore[arg-type]


def _candidates(count: int, rate: float | None = 0.5) -> list[InstrumentCaptureCandidate]:
    return [
        InstrumentCaptureCandidate(
            instrument_token=1000 + index,
            liquidity_value=float(count - index),
            measured_packets_per_second=rate,
        )
        for index in range(count)
    ]


@pytest.mark.unit
def test_an_unmeasured_tape_yields_a_calibration_cohort_not_a_guess(tmp_path: Path) -> None:
    decision = _controller(tmp_path).solve(_candidates(50), 22_500.0, None)
    assert decision.is_calibration_cohort
    assert decision.admitted_count == CALIBRATION_COHORT_SIZE
    assert decision.admitted_tokens == (1000, 1001, 1002, 1003, 1004)


@pytest.mark.unit
def test_the_calibration_cohort_takes_the_most_liquid_instruments(tmp_path: Path) -> None:
    shuffled = [
        InstrumentCaptureCandidate(instrument_token=token, liquidity_value=value)
        for token, value in ((1, 5.0), (2, 100.0), (3, 50.0), (4, 1.0), (5, 75.0), (6, 2.0))
    ]
    decision = _controller(tmp_path, calibration_cohort_size=3).solve(shuffled, 100.0, None)
    assert decision.admitted_tokens == (2, 5, 3)


@pytest.mark.unit
def test_admission_is_bounded_by_the_measured_byte_budget(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    session_seconds = 22_500.0
    bytes_per_row = 40.0
    decision = controller.solve(_candidates(20_000), session_seconds, bytes_per_row)
    assert not decision.is_calibration_cohort
    assert decision.projected_session_bytes <= decision.budget_bytes
    per_instrument_bytes = 0.5 * session_seconds * bytes_per_row
    assert decision.admitted_count == int(decision.budget_bytes // per_instrument_bytes)


@pytest.mark.unit
def test_a_cheaper_row_admits_strictly_more_instruments(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    lean = controller.solve(_candidates(20_000), 22_500.0, 20.0)
    fat = controller.solve(_candidates(20_000), 22_500.0, 80.0)
    assert lean.admitted_count > fat.admitted_count


@pytest.mark.unit
def test_a_busier_instrument_costs_more_and_is_admitted_later(tmp_path: Path) -> None:
    """The measured cross-sectional rate spread was 130x, so cost must track rate."""
    candidates = [
        InstrumentCaptureCandidate(1, liquidity_value=1.0, measured_packets_per_second=0.01),
        InstrumentCaptureCandidate(2, liquidity_value=1.0, measured_packets_per_second=1.70),
    ]
    decision = _controller(tmp_path).solve(candidates, 22_500.0, 40.0)
    assert decision.admitted_tokens[0] == 1


@pytest.mark.unit
def test_unmeasured_instruments_are_costed_at_the_cohort_median_and_reported(
    tmp_path: Path,
) -> None:
    candidates = [
        InstrumentCaptureCandidate(1, 10.0, 0.4),
        InstrumentCaptureCandidate(2, 10.0, 0.6),
        InstrumentCaptureCandidate(3, 10.0, None),
    ]
    decision = _controller(tmp_path).solve(candidates, 100.0, 40.0)
    assert decision.fallback_rate_used == COHORT_MEDIAN_RATE
    assert decision.unmeasured_token_count == 1
    assert any("median" in note for note in decision.notes)


@pytest.mark.unit
def test_the_connection_ceiling_caps_admission(tmp_path: Path) -> None:
    decision = _controller(tmp_path).solve(
        _candidates(20_000), 22_500.0, 1.0, connection_ceiling=3_000
    )
    assert decision.admitted_count == KITE_CONNECTION_CEILING


@pytest.mark.unit
def test_existing_tape_counts_against_the_retention_budget(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    empty_budget = controller.budget_bytes_per_session(already_used_bytes=0)
    used_budget = controller.budget_bytes_per_session(already_used_bytes=10 * 1024**3)
    assert used_budget > empty_budget


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("retention_sessions", 0),
        ("disk_budget_fraction", 0.0),
        ("disk_budget_fraction", 1.5),
        ("calibration_cohort_size", 0),
    ],
)
def test_an_inconsistent_budget_is_refused(tmp_path: Path, field_name: str, value: object) -> None:
    with pytest.raises(AdmissionControlError):
        _controller(tmp_path, **{field_name: value})


@pytest.mark.adversarial
def test_a_negative_rate_or_value_is_refused() -> None:
    with pytest.raises(AdmissionControlError):
        InstrumentCaptureCandidate(1, liquidity_value=-1.0)
    with pytest.raises(AdmissionControlError):
        InstrumentCaptureCandidate(1, liquidity_value=1.0, measured_packets_per_second=-0.5)


@pytest.mark.unit
def test_shedding_drops_the_least_liquid_first(tmp_path: Path) -> None:
    values = {10: 5.0, 20: 100.0, 30: 1.0, 40: 50.0}
    shed = _controller(tmp_path).tokens_to_shed(
        admitted_tokens=[10, 20, 30, 40],
        liquidity_value_by_token=values,
        projected_remaining_bytes=1000.0,
        remaining_budget_bytes=400.0,
    )
    assert shed[0] == LEAST_LIQUID_TOKEN
    assert MOST_LIQUID_TOKEN not in shed


@pytest.mark.unit
def test_nothing_is_shed_when_the_budget_holds(tmp_path: Path) -> None:
    assert (
        _controller(tmp_path).tokens_to_shed([1, 2], {1: 1.0, 2: 2.0}, 100.0, 500.0) == ()
    )


# ------------------------------------------------------------------ recorder


def _recorder(
    tmp_path: Path,
    packets: Sequence[DepthPacket],
    queue_size: int = 1000,
    session_ends_at: datetime | None = None,
) -> tuple[LiveOrderBookDepthRecorder, DeterministicDepthFeed, ShardRuntime]:
    feed = DeterministicDepthFeed(packets, tokens={738561})
    shard = ShardRuntime(
        shard_index=0,
        feed=feed,
        store=_store(tmp_path, max_buffered_rows=10),
        packet_queue=queue.Queue(maxsize=queue_size),
        statistics=ShardCaptureStatistics(),
    )
    from nse_algo_trader.market_depth.depth_capture_admission_controller import (
        AdmissionDecision,
    )

    recorder = LiveOrderBookDepthRecorder(
        session_date=SESSION_DATE,
        shards=[shard],
        classifier=DepthPacketIntegrityClassifier(),
        admission_decision=AdmissionDecision(
            admitted_tokens=(738561,),
            rejected_tokens=(),
            projected_session_bytes=0.0,
            budget_bytes=1.0,
            bytes_per_row_used=40.0,
            session_seconds=22_500.0,
            is_calibration_cohort=False,
            unmeasured_token_count=0,
            fallback_rate_used=0.5,
        ),
        session_ends_at=session_ends_at or (datetime.now(UTC) + timedelta(seconds=30)),
    )
    return recorder, feed, shard


@pytest.mark.hermetic
def test_a_whole_session_lands_on_the_tape(tmp_path: Path) -> None:
    packets = [_packet(sequence=s) for s in range(1, SESSION_PACKET_COUNT + 1)]
    recorder, feed, shard = _recorder(tmp_path, packets)
    recorder.start()
    feed.emit_all(batch_size=7)
    recorder.stop()
    assert shard.statistics.packets_written == SESSION_PACKET_COUNT
    assert shard.statistics.packets_dropped_to_overflow == 0
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(days=1), SESSION_DATE
    )
    assert table.num_rows == SESSION_PACKET_COUNT


@pytest.mark.hermetic
def test_stop_disconnects_the_feed_before_draining(tmp_path: Path) -> None:
    recorder, feed, _ = _recorder(tmp_path, [_packet()])
    recorder.start()
    feed.emit_all()
    recorder.stop()
    assert feed.stopped


@pytest.mark.hermetic
def test_stop_is_idempotent(tmp_path: Path) -> None:
    recorder, feed, _ = _recorder(tmp_path, [_packet()])
    recorder.start()
    feed.emit_all()
    recorder.stop()
    recorder.stop()


@pytest.mark.adversarial
def test_queue_overflow_is_counted_per_instrument_never_silent(tmp_path: Path) -> None:
    """An undocumented gap makes the whole tape unusable; a counted one does not."""
    packets = [_packet(token=900 + (sequence % 3), sequence=sequence) for sequence in range(1, 51)]
    recorder, feed, shard = _recorder(tmp_path, packets, queue_size=1)
    # Never start the writer thread, so nothing drains and the queue stays full.
    recorder._started = True
    for shard_runtime in recorder.shards:
        shard_runtime.feed.start(recorder._handler_for(shard_runtime))
    feed.emit_all()
    assert shard.statistics.packets_dropped_to_overflow > 0
    assert (
        sum(shard.statistics.drops_by_token.values())
        == shard.statistics.packets_dropped_to_overflow
    )
    assert set(shard.statistics.drops_by_token) <= {900, 901, 902}


@pytest.mark.adversarial
def test_a_dead_writer_is_surfaced_at_stop_not_swallowed(tmp_path: Path) -> None:
    """A writer dying silently would let the session look healthy while its tape
    stopped growing."""
    recorder, feed, shard = _recorder(tmp_path, [_packet()])

    class ExplodingStore:
        def append(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("tape writer exploded")

        def close(self) -> None:
            return None

    object.__setattr__(shard, "store", ExplodingStore())
    recorder.start()
    feed.emit_all()
    with pytest.raises(DepthRecorderError, match="tape writer died"):
        recorder.stop()


@pytest.mark.adversarial
def test_starting_twice_is_refused(tmp_path: Path) -> None:
    recorder, _, _ = _recorder(tmp_path, [_packet()])
    recorder.start()
    with pytest.raises(DepthRecorderError, match="already started"):
        recorder.start()
    recorder.stop()


@pytest.mark.adversarial
def test_a_recorder_with_no_shards_is_refused() -> None:
    from nse_algo_trader.market_depth.depth_capture_admission_controller import (
        AdmissionDecision,
    )

    with pytest.raises(DepthRecorderError, match="at least one shard"):
        LiveOrderBookDepthRecorder(
            session_date=SESSION_DATE,
            shards=[],
            classifier=DepthPacketIntegrityClassifier(),
            admission_decision=AdmissionDecision(
                (), (), 0.0, 1.0, 1.0, 1.0, False, 0, None
            ),
            session_ends_at=datetime.now(UTC),
        )


@pytest.mark.hermetic
def test_shedding_unsubscribes_only_the_named_tokens(tmp_path: Path) -> None:
    recorder, feed, _ = _recorder(tmp_path, [])
    feed.subscribe([111, 222, 333])
    recorder.start()
    recorder.shed([222])
    assert feed.subscribed_tokens == frozenset({738561, 111, 333})
    recorder.stop()


@pytest.mark.hermetic
def test_run_until_session_end_returns_at_the_close(tmp_path: Path) -> None:
    recorder, _, _ = _recorder(
        tmp_path, [], session_ends_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    recorder.start()
    recorder.run_until_session_end(poll_seconds=0.01)
    recorder.stop()


@pytest.mark.hermetic
def test_request_stop_ends_the_session_early(tmp_path: Path) -> None:
    recorder, _, _ = _recorder(
        tmp_path, [], session_ends_at=datetime.now(UTC) + timedelta(hours=6)
    )
    recorder.start()
    threading.Timer(0.05, recorder.request_stop).start()
    recorder.run_until_session_end(poll_seconds=0.01)
    recorder.stop()


@pytest.mark.hermetic
def test_integrity_flags_are_tallied_across_the_session(tmp_path: Path) -> None:
    crossed = DepthPacket(
        instrument_token=738561,
        exchange="NSE",
        exchange_time=BASE_TIME,
        receipt_time=BASE_TIME,
        receipt_sequence=1,
        last_price_paise=100,
        last_traded_quantity=1,
        average_traded_price_paise=100,
        volume_traded=1,
        total_buy_quantity=1,
        total_sell_quantity=1,
        open_interest=0,
        bids=tuple(DepthLevel(200, 1, 1) for _ in range(5)),
        asks=tuple(DepthLevel(100, 1, 1) for _ in range(5)),
    )
    recorder, feed, shard = _recorder(tmp_path, [crossed])
    recorder.start()
    feed.emit_all()
    recorder.stop()
    assert shard.statistics.flag_counts.get(IntegrityFlag.BOOK_CROSSED) == 1


@pytest.mark.unit
def test_the_fake_feed_satisfies_the_seam_protocol() -> None:
    """Guards the guard: a fake that has drifted from the Protocol verifies nothing."""
    from nse_algo_trader.market_depth.live_depth_feed_seam import LiveDepthFeed

    feed: LiveDepthFeed = DeterministicDepthFeed([])
    assert feed.subscribed_tokens == frozenset()


@pytest.mark.unit
def test_the_host_can_fsync_a_directory() -> None:
    """The atomic-rename durability argument depends on this syscall working here."""
    descriptor = os.open(Path.cwd(), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.adversarial
def test_a_restarted_capture_does_not_overwrite_the_morning_tape(tmp_path: Path) -> None:
    """Restarting mid-session — to widen the universe, or after a crash — must append
    to the day's tape, not begin again at part-000000 on top of it."""
    with _store(tmp_path, max_buffered_rows=5) as first_run:
        for sequence in range(1, 11):
            first_run.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    with _store(tmp_path, max_buffered_rows=5) as second_run:
        for sequence in range(11, 21):
            second_run.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(days=1), SESSION_DATE
    )
    assert sorted(table.column("receipt_sequence").to_pylist()) == list(range(1, 21))


@pytest.mark.unit
def test_packet_rates_are_measured_over_each_instruments_own_span(tmp_path: Path) -> None:
    """Dividing by a nominal window instead would misstate every rate whenever the
    tape holds more than that window — the normal case after a mid-session restart."""
    fast_token, slow_token = 111, 222
    with _store(tmp_path, max_buffered_rows=1000) as store:
        for sequence in range(1, 101):
            store.append(_packet(token=fast_token, sequence=sequence), IntegrityFlag.NONE)
        for sequence in range(1, 11):
            store.append(
                _packet(token=slow_token, sequence=sequence * 10), IntegrityFlag.NONE
            )
    rates = MarketDepthTapeReader(tmp_path).instrument_packet_rates(SESSION_DATE)
    # 100 packets one second apart spans 99s; 10 packets ten seconds apart spans 90s.
    assert rates[fast_token] == pytest.approx(100 / 99, rel=1e-6)
    assert rates[slow_token] == pytest.approx(10 / 90, rel=1e-6)


@pytest.mark.adversarial
def test_an_instrument_seen_once_has_no_rate_rather_than_zero(tmp_path: Path) -> None:
    with _store(tmp_path, max_buffered_rows=1000) as store:
        store.append(_packet(token=SINGLE_SIGHTING_TOKEN, sequence=1), IntegrityFlag.NONE)
    rates = MarketDepthTapeReader(tmp_path).instrument_packet_rates(SESSION_DATE)
    assert SINGLE_SIGHTING_TOKEN not in rates


@pytest.mark.adversarial
def test_two_concurrent_captures_cannot_share_a_shard_directory(tmp_path: Path) -> None:
    """The 2026-08-11 fault: two recorders wrote into one shard with independent part
    counters and could overwrite each other. Run-scoped paths make that impossible."""
    first = _store(tmp_path, capture_run_id="run-a", max_buffered_rows=1)
    second = _store(tmp_path, capture_run_id="run-b", max_buffered_rows=1)
    first.append(_packet(sequence=1), IntegrityFlag.NONE)
    second.append(_packet(sequence=2), IntegrityFlag.NONE)
    first.close()
    second.close()
    written = sorted(path.parent.parent.name for path in tmp_path.rglob("part-*.parquet"))
    assert written == ["capture_run=run-a", "capture_run=run-b"]
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, BASE_TIME + timedelta(days=1), SESSION_DATE
    )
    assert sorted(table.column("receipt_sequence").to_pylist()) == [1, 2]


@pytest.mark.adversarial
@pytest.mark.parametrize("bad_run_id", ["", "a/b", "capture_run=x"])
def test_an_unsafe_capture_run_id_is_refused(tmp_path: Path, bad_run_id: str) -> None:
    with pytest.raises(DepthTapeStoreError, match="path-safe"):
        _store(tmp_path, capture_run_id=bad_run_id)


@pytest.mark.adversarial
def test_only_the_flags_actually_set_are_tallied(tmp_path: Path) -> None:
    """Asserting the expected flag is present does not prove the others are absent —
    an `and` -> `or` in the tally loop counts every flag on every packet and still
    satisfies a test that only checks the one it expects."""
    crossed = DepthPacket(
        instrument_token=738561,
        exchange="NSE",
        exchange_time=BASE_TIME,
        receipt_time=BASE_TIME,
        receipt_sequence=1,
        last_price_paise=100,
        last_traded_quantity=1,
        average_traded_price_paise=100,
        volume_traded=1,
        total_buy_quantity=1,
        total_sell_quantity=1,
        open_interest=0,
        bids=tuple(DepthLevel(200, 1, 1) for _ in range(5)),
        asks=tuple(DepthLevel(100, 1, 1) for _ in range(5)),
    )
    recorder, feed, shard = _recorder(tmp_path, [crossed])
    recorder.start()
    feed.emit_all()
    recorder.stop()
    assert shard.statistics.flag_counts == {IntegrityFlag.BOOK_CROSSED: 1}


@pytest.mark.adversarial
def test_a_zero_length_session_is_refused_by_admission(tmp_path: Path) -> None:
    """Zero seconds makes every instrument free, so the budget would admit everything."""
    with pytest.raises(AdmissionControlError, match="session_seconds"):
        _controller(tmp_path).solve(_candidates(10), 0.0, 40.0)


@pytest.mark.adversarial
def test_a_row_exactly_at_the_window_start_is_included(tmp_path: Path) -> None:
    """The read window is half-open [start, end) and consumers rely on that: adjacent
    windows must partition the tape without dropping or double-counting a row."""
    with _store(tmp_path) as store:
        store.append(_packet(sequence=1), IntegrityFlag.NONE)
    exact_start = BASE_TIME + timedelta(seconds=1, milliseconds=40)
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, exact_start, exact_start + timedelta(hours=1), SESSION_DATE
    )
    assert table.num_rows == 1


@pytest.mark.adversarial
def test_a_row_exactly_at_the_window_end_is_excluded(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.append(_packet(sequence=1), IntegrityFlag.NONE)
    exact_receipt = BASE_TIME + timedelta(seconds=1, milliseconds=40)
    table = MarketDepthTapeReader(tmp_path).read_instrument_window(
        738561, BASE_TIME, exact_receipt, SESSION_DATE
    )
    assert table.num_rows == 0


# ------------------------------- defects found by adversarial review (C1-C5, T1-T3)


@pytest.mark.adversarial
def test_stop_does_not_hang_when_a_writer_died_and_the_queue_is_full(tmp_path: Path) -> None:
    """C1. `stop()` runs in the script's `finally`, so a hang there means the session
    must be `kill -9`'d — taking every other shard's buffered tail with it. The trigger
    is the disk-full case the spec explicitly lists."""
    recorder, feed, shard = _recorder(tmp_path, [], queue_size=2)

    class ExplodingStore:
        def append(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("No space left on device")

        def flush_if_due(self) -> None:
            return None

        def close(self) -> None:
            return None

    object.__setattr__(shard, "store", ExplodingStore())
    recorder.start()
    feed.deliver([_packet(sequence=1)])
    deadline = time.monotonic() + 20.0
    while shard.statistics.writer_exception is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert shard.statistics.writer_exception is not None
    # Fill the queue now that nothing is draining it.
    for sequence in range(10):
        with contextlib.suppress(queue.Full):
            shard.packet_queue.put_nowait([_packet(sequence=sequence)])
    assert shard.packet_queue.full()

    finished = threading.Event()

    def stop_in_background() -> None:
        with contextlib.suppress(DepthRecorderError):
            recorder.stop()
        finished.set()

    threading.Thread(target=stop_in_background, daemon=True).start()
    assert finished.wait(timeout=30.0), "stop() blocked with a dead writer and a full queue"


@pytest.mark.adversarial
def test_book_at_prefers_the_newest_row_across_capture_runs(tmp_path: Path) -> None:
    """C2. `receipt_sequence` restarts at zero for every feed, so an older run's high
    sequence must not beat a newer run's low one. Measured on the 2026-08-11 tape:
    two runs with overlapping ranges over 4,236 shared instruments."""
    with _store(tmp_path, capture_run_id="calibration") as first_run:
        for sequence in range(300, 400):
            first_run.append(_packet(sequence=sequence, price=100_000), IntegrityFlag.NONE)
    with _store(tmp_path, capture_run_id="fullsession") as second_run:
        for sequence in range(1, 50):
            second_run.append(
                DepthPacket(
                    instrument_token=738561,
                    exchange="NSE",
                    exchange_time=BASE_TIME + timedelta(hours=2),
                    receipt_time=BASE_TIME + timedelta(hours=2, seconds=sequence),
                    receipt_sequence=sequence,
                    last_price_paise=200_000,
                    last_traded_quantity=1,
                    average_traded_price_paise=200_000,
                    volume_traded=sequence,
                    total_buy_quantity=1,
                    total_sell_quantity=1,
                    open_interest=0,
                    bids=tuple(DepthLevel(199_990 - i, 1, 1) for i in range(5)),
                    asks=tuple(DepthLevel(200_010 + i, 1, 1) for i in range(5)),
                ),
                IntegrityFlag.NONE,
            )
    book = MarketDepthTapeReader(tmp_path).book_at(
        738561, BASE_TIME + timedelta(hours=3), SESSION_DATE
    )
    assert book is not None
    assert book["last_price_paise"] == NEWER_RUN_PRICE_PAISE, "stale cross-run book"


@pytest.mark.adversarial
def test_packets_arriving_after_stop_are_counted_not_silently_dropped(
    tmp_path: Path,
) -> None:
    """C3. A socket close is asynchronous, so in-flight frames still arrive. Queuing
    them behind the sentinel loses them without any counter moving — the one thing the
    module docstring promises never happens."""
    recorder, feed, shard = _recorder(tmp_path, [])
    recorder.start()
    recorder.stop()
    feed.deliver([_packet(sequence=n) for n in range(IN_FLIGHT_PACKET_COUNT)])
    assert shard.statistics.packets_arriving_after_stop == IN_FLIGHT_PACKET_COUNT
    assert recorder.total_packets_after_stop() == IN_FLIGHT_PACKET_COUNT


@pytest.mark.adversarial
def test_one_shards_failing_close_does_not_destroy_another_shards_tail(
    tmp_path: Path,
) -> None:
    """C4. An unguarded close loop lets a disk-full shard abort the loop, so every
    healthy shard after it never flushes and loses its whole buffer."""
    healthy_store = _store(tmp_path, shard_index=9, max_buffered_rows=1_000_000)

    class UnclosableStore:
        def append(self, *_args: object, **_kwargs: object) -> None:
            return None

        def flush_if_due(self) -> None:
            return None

        def close(self) -> None:
            raise OSError("No space left on device")

    from nse_algo_trader.market_depth.depth_capture_admission_controller import (
        AdmissionDecision,
    )

    shards = [
        ShardRuntime(
            0,
            DeterministicDepthFeed([]),
            UnclosableStore(),  # type: ignore[arg-type]
            queue.Queue(),
            ShardCaptureStatistics(),
        ),
        ShardRuntime(
            9,
            DeterministicDepthFeed([]),
            healthy_store,
            queue.Queue(),
            ShardCaptureStatistics(),
        ),
    ]
    recorder = LiveOrderBookDepthRecorder(
        session_date=SESSION_DATE,
        shards=shards,
        classifier=DepthPacketIntegrityClassifier(),
        admission_decision=AdmissionDecision((), (), 0.0, 1.0, 1.0, 1.0, False, 0, None),
        session_ends_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    recorder.start()
    healthy_store.append(_packet(sequence=1), IntegrityFlag.NONE)
    with pytest.raises(DepthRecorderError, match="failed to close"):
        recorder.stop()
    assert healthy_store.rows_written == 1, "the healthy shard's tail was lost"


@pytest.mark.adversarial
def test_a_quiet_shard_still_flushes_on_the_time_bound(tmp_path: Path) -> None:
    """C5. `_flush_is_due` was consulted only from `append`, so a shard whose feed went
    quiet held its buffer past the configured bound — and lost it on `kill -9`."""
    store = _store(tmp_path, max_buffered_rows=1_000_000, max_seconds_between_flushes=0.2)
    store.append(_packet(sequence=1), IntegrityFlag.NONE)
    assert store.rows_written == 0
    time.sleep(0.3)
    assert store.flush_if_due() is not None
    assert store.rows_written == 1


@pytest.mark.adversarial
def test_a_part_is_published_only_by_an_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1. The atomicity claim was entirely untested: the old test hand-created a
    `.tmp` file and so verified pyarrow's dot-file filtering, not the writer's naming.
    This asserts the final path never exists until a rename publishes it."""
    renames: list[tuple[str, str]] = []
    original_replace = Path.replace

    def recording_replace(self: Path, target: object) -> Path:
        assert self.name.startswith("."), "part was written under its final name"
        assert not Path(str(target)).exists(), "final path existed before the rename"
        renames.append((self.name, Path(str(target)).name))
        return original_replace(self, target)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "replace", recording_replace)
    with _store(tmp_path, max_buffered_rows=2) as store:
        for sequence in range(1, 5):
            store.append(_packet(sequence=sequence), IntegrityFlag.NONE)
    assert len(renames) >= MINIMUM_ATOMIC_RENAMES
    assert all(temp.endswith(".tmp") and final.endswith(".parquet") for temp, final in renames)


@pytest.mark.adversarial
def test_a_flush_fsyncs_both_the_part_and_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2. `kill -9` never drops page cache, so the kill test could not possibly have
    exercised fsync — deleting both fsync calls left the suite green. Only power loss
    would show it, so the calls are asserted directly."""
    fsync_calls: list[int] = []
    original_fsync = os.fsync

    def counting_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    with _store(tmp_path, max_buffered_rows=1) as store:
        store.append(_packet(sequence=1), IntegrityFlag.NONE)
    # One for the part file's contents, one for the directory entry naming it.
    assert len(fsync_calls) >= MINIMUM_FSYNC_CALLS
