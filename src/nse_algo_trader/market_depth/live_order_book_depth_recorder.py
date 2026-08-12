"""`L0.20` — the session-long capture loop: feed → classify → tape, with control.

The recorder owns the session, not the socket. It decides who is subscribed (from the
admission controller), absorbs bursts without blocking the socket thread, sheds
instruments when the disk budget contracts, and flushes what it has when the session
ends or the process is asked to stop.

**Nothing is dropped silently.** The queue between the socket thread and the writer
thread is bounded, because an unbounded one converts a slow disk into an out-of-memory
kill six hours in. When it does fill, the overflow is counted per instrument and
surfaced in the session report — a tape whose gaps are documented is usable, and a tape
with undocumented gaps is not, which is the whole point of `L0.12`'s provenance rule.

**The writer thread is the only thread that touches a store.** That is what lets the
store stay lock-free, and it is why shards map one-to-one onto both feeds and stores.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from nse_algo_trader.market_depth.depth_capture_admission_controller import (
    AdmissionDecision,
)
from nse_algo_trader.market_depth.depth_packet_integrity_classifier import (
    DepthPacketIntegrityClassifier,
)
from nse_algo_trader.market_depth.depth_tape_schema import DepthPacket, IntegrityFlag
from nse_algo_trader.market_depth.live_depth_feed_seam import LiveDepthFeed
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeStore


class DepthRecorderError(Exception):
    """Raised when the recorder cannot start or is misconfigured."""


@dataclass
class ShardCaptureStatistics:
    """Per-shard evidence about what the capture actually managed to do."""

    packets_enqueued: int = 0
    packets_written: int = 0
    packets_dropped_to_overflow: int = 0
    packets_arriving_after_stop: int = 0
    drops_by_token: dict[int, int] = field(default_factory=dict)
    flag_counts: dict[IntegrityFlag, int] = field(default_factory=dict)
    first_packet_at: datetime | None = None
    last_packet_at: datetime | None = None
    writer_exception: BaseException | None = None

    def record_drop(self, instrument_token: int) -> None:
        self.packets_dropped_to_overflow += 1
        self.drops_by_token[instrument_token] = self.drops_by_token.get(instrument_token, 0) + 1


@dataclass(frozen=True)
class ShardRuntime:
    """One shard: a feed, its store, and the queue joining their threads."""

    shard_index: int
    feed: LiveDepthFeed
    store: MarketDepthTapeStore
    packet_queue: queue.Queue[Sequence[DepthPacket] | None]
    statistics: ShardCaptureStatistics


class LiveOrderBookDepthRecorder:
    """Runs one capture session across one or more shards."""

    def __init__(
        self,
        session_date: date,
        shards: Sequence[ShardRuntime],
        classifier: DepthPacketIntegrityClassifier,
        admission_decision: AdmissionDecision,
        session_ends_at: datetime,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        writer_poll_seconds: float = 0.5,
        writer_join_timeout_seconds: float = 60.0,
    ) -> None:
        if not shards:
            raise DepthRecorderError("a capture needs at least one shard")
        self._session_date = session_date
        self._shards = tuple(shards)
        self._classifier = classifier
        self._admission_decision = admission_decision
        self._session_ends_at = session_ends_at
        self._clock = clock
        self._writer_threads: list[threading.Thread] = []
        self._stop_requested = threading.Event()
        self._started = False
        self._stopped = False
        self._writer_poll_seconds = writer_poll_seconds
        self._writer_join_timeout_seconds = writer_join_timeout_seconds

    @property
    def shards(self) -> tuple[ShardRuntime, ...]:
        return self._shards

    @property
    def session_date(self) -> date:
        return self._session_date

    @property
    def admission_decision(self) -> AdmissionDecision:
        return self._admission_decision

    def total_packets_written(self) -> int:
        return sum(shard.statistics.packets_written for shard in self._shards)

    def total_packets_dropped(self) -> int:
        return sum(shard.statistics.packets_dropped_to_overflow for shard in self._shards)

    def total_packets_after_stop(self) -> int:
        return sum(shard.statistics.packets_arriving_after_stop for shard in self._shards)

    def _handler_for(self, shard: ShardRuntime) -> Callable[[Sequence[DepthPacket]], None]:
        """The socket thread's entry point. Must never block and never raise."""

        def handle(packets: Sequence[DepthPacket]) -> None:
            statistics = shard.statistics
            if self._stop_requested.is_set():
                # The socket was told to close but frames are still in flight. Queuing
                # these would put them behind the sentinel to be dropped unrecorded,
                # which is the one thing this module promises never to do.
                statistics.packets_arriving_after_stop += len(packets)
                return
            try:
                shard.packet_queue.put_nowait(packets)
                statistics.packets_enqueued += len(packets)
            except queue.Full:
                # Counted per instrument, never silent: the session report has to be
                # able to say which instruments have holes and how big.
                for packet in packets:
                    statistics.record_drop(packet.instrument_token)

        return handle

    def _writer_loop(self, shard: ShardRuntime) -> None:
        """Drain, classify and store until asked to stop AND the queue is empty.

        Polling with a timeout rather than blocking forever on `get()` does three jobs
        at once: it lets a time-based flush actually fire on a shard whose feed has gone
        quiet, it lets the writer notice a stop request without depending on a sentinel
        reaching it, and it removes the interleaving where `stop()` blocks forever
        putting a sentinel into a full queue that no live consumer will ever drain.
        """
        statistics = shard.statistics
        try:
            while True:
                try:
                    batch = shard.packet_queue.get(timeout=self._writer_poll_seconds)
                except queue.Empty:
                    # Nothing arrived. This is exactly when a quiet shard's buffered
                    # tail would otherwise sit in RAM past its configured flush bound.
                    shard.store.flush_if_due()
                    if self._stop_requested.is_set():
                        break
                    continue
                if batch is None:
                    break
                for packet in batch:
                    integrity_flags = self._classifier.classify(packet)
                    shard.store.append(packet, integrity_flags)
                    statistics.packets_written += 1
                    if statistics.first_packet_at is None:
                        statistics.first_packet_at = packet.receipt_time
                    statistics.last_packet_at = packet.receipt_time
                    for flag in IntegrityFlag:
                        if flag is not IntegrityFlag.NONE and flag & integrity_flags:
                            statistics.flag_counts[flag] = statistics.flag_counts.get(flag, 0) + 1
        except BaseException as writer_exception:  # noqa: BLE001 — recorded, then re-surfaced at stop
            # A writer dying silently would let the session look healthy while its tape
            # stopped growing. It is captured here and re-raised by `stop`.
            statistics.writer_exception = writer_exception

    def start(self) -> None:
        if self._started:
            raise DepthRecorderError("recorder already started")
        self._started = True
        for shard in self._shards:
            writer_thread = threading.Thread(
                target=self._writer_loop,
                args=(shard,),
                name=f"depth-tape-writer-{shard.shard_index:02d}",
                daemon=True,
            )
            writer_thread.start()
            self._writer_threads.append(writer_thread)
            shard.feed.start(self._handler_for(shard))

    def run_until_session_end(self, poll_seconds: float = 1.0) -> None:
        """Block until the session's close, a stop request, or a writer death."""
        while not self._stop_requested.is_set():
            if self._clock() >= self._session_ends_at:
                break
            if any(shard.statistics.writer_exception for shard in self._shards):
                break
            time.sleep(poll_seconds)

    def request_stop(self) -> None:
        """Ask the session to end early — safe from a signal handler."""
        self._stop_requested.set()

    def shed(self, instrument_tokens: Sequence[int]) -> None:
        """Unsubscribe instruments mid-session under budget pressure."""
        remaining = set(instrument_tokens)
        for shard in self._shards:
            in_this_shard = remaining & shard.feed.subscribed_tokens
            if in_this_shard:
                shard.feed.unsubscribe(sorted(in_this_shard))
                remaining -= in_this_shard

    def stop(self) -> None:
        """Disconnect, drain every queue, flush every store. Idempotent.

        Ordering is deliberate: stop the feeds first so nothing new arrives, then let
        the writers drain what is queued, then close. Flushing before draining would
        leave the buffered tail in memory to be lost.

        Three failure paths are handled explicitly because each was found by adversarial
        review, and each loses data silently:

        - The sentinel is offered **without blocking**. A dead writer plus a full queue
          would otherwise hang `stop()` forever, and since `stop()` runs in the script's
          `finally`, the session would need `kill -9` — taking every other shard's
          buffer with it. The stop event, not the sentinel, is what the writers obey.
        - Every store is closed inside its own guard. An unguarded loop lets one shard's
          disk-full `close()` abort the loop and destroy the *healthy* shards' tails.
        - Writers that do not exit in time are reported rather than assumed finished.
        """
        if self._stopped:
            return
        self._stopped = True

        for shard in self._shards:
            shard.feed.stop()
        # A socket close is asynchronous; in-flight frames can still arrive. Setting
        # this first means late packets are counted as late instead of vanishing.
        self._stop_requested.set()

        for shard, writer_thread in zip(self._shards, self._writer_threads, strict=False):
            if writer_thread.is_alive():
                # The stop event is what ends the loop; the sentinel only makes it
                # prompt. A full queue is therefore not a problem to handle, it is a
                # case where the shortcut is simply unavailable.
                with contextlib.suppress(queue.Full):
                    shard.packet_queue.put_nowait(None)

        unfinished_writers = []
        for shard, writer_thread in zip(self._shards, self._writer_threads, strict=False):
            writer_thread.join(timeout=self._writer_join_timeout_seconds)
            if writer_thread.is_alive():
                unfinished_writers.append(shard.shard_index)

        close_failures: list[tuple[int, BaseException]] = []
        for shard in self._shards:
            try:
                shard.store.close()
            except BaseException as close_failure:  # noqa: BLE001 — all shards get their chance
                close_failures.append((shard.shard_index, close_failure))

        first_exception = next(
            (
                shard.statistics.writer_exception
                for shard in self._shards
                if shard.statistics.writer_exception is not None
            ),
            None,
        )
        if first_exception is not None:
            raise DepthRecorderError(
                "a tape writer died during the session; the tape is incomplete"
            ) from first_exception
        if close_failures:
            shard_indexes = ", ".join(str(index) for index, _ in close_failures)
            raise DepthRecorderError(
                f"shard(s) {shard_indexes} failed to close; their buffered tail is lost"
            ) from close_failures[0][1]
        if unfinished_writers:
            raise DepthRecorderError(
                f"writer thread(s) {unfinished_writers} did not finish draining within "
                f"{self._writer_join_timeout_seconds}s; the tape may be incomplete"
            )

    def __enter__(self) -> LiveOrderBookDepthRecorder:
        self.start()
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.stop()

    def capture_statistics_by_shard(self) -> Mapping[int, ShardCaptureStatistics]:
        return {shard.shard_index: shard.statistics for shard in self._shards}
