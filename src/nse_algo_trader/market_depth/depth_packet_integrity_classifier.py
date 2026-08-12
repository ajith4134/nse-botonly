"""Per-packet integrity classification, carrying per-instrument state.

Every finding is recorded on the row and counted per instrument; nothing is dropped.
A flagged row is evidence — it is how the session report can later say *why* an
instrument-session is unusable. A discarded row can only say that something is
missing.

The staleness threshold is **derived per instrument from its own observed
distribution**, never set to a constant. A stock quoting twice a second and one
quoting every eight seconds have legitimately different notions of "stale", and the
measured spread across instruments was 130x (0.013 to 1.72 packets/s). The estimator
is river's `Quantile`, a streaming P² implementation with O(1) memory per instrument
— which matters when the capture universe is in the thousands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from river import stats

from nse_algo_trader.market_depth.depth_tape_schema import (
    DEPTH_LEVELS_PER_SIDE,
    DepthPacket,
    IntegrityFlag,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

NSE_QUOTING_WINDOW_OPENS_IST = time(9, 0)
"""Pre-open call auction begins. A regulatory fact published by the exchange, so a
constant is permitted under `R.23(e)`; it is not a tunable. NSE has moved this in the
past (continuous trading began at 09:55 before 2010), so historical replay must not
reuse today's window — recorded as a limitation in the session report."""

NSE_QUOTING_WINDOW_CLOSES_IST = time(15, 30)
"""Continuous trading ends. Same standing as the open."""

STALENESS_EXTREME_QUANTILE = 0.999
"""What counts as extreme for an instrument: its own thousandth-percentile staleness.
A quantile is a definition of extremity, not a magic threshold — the value it resolves
to is learned per instrument and differs by orders of magnitude across the universe."""

MINIMUM_SAMPLES_FOR_STALENESS_MATURITY = round(1 / (1 - STALENESS_EXTREME_QUANTILE))
"""Arithmetic, not a guess: estimating a 1-in-1000 quantile needs at least ~1000
observations before the estimate means anything. Below this the flag is withheld and
the instrument is reported immature (`R.04` — the algorithm is full-strength, only its
activation is gated by a maturity ladder)."""


@dataclass
class InstrumentIntegrityState:
    """What must be remembered between packets to classify the next one."""

    last_exchange_time_micros: int | None = None
    last_book_fingerprint: tuple[int, ...] | None = None
    staleness_quantile: stats.Quantile = field(
        default_factory=lambda: stats.Quantile(STALENESS_EXTREME_QUANTILE)
    )
    staleness_sample_count: int = 0
    packets_seen: int = 0
    flag_counts: dict[IntegrityFlag, int] = field(default_factory=dict)

    @property
    def staleness_threshold_is_mature(self) -> bool:
        return self.staleness_sample_count >= MINIMUM_SAMPLES_FOR_STALENESS_MATURITY

    def derived_staleness_threshold_micros(self) -> float | None:
        """The instrument's own extreme-staleness threshold, or None while immature."""
        if not self.staleness_threshold_is_mature:
            return None
        # river ships no annotations on its stats API; the boundary is narrow and
        # both values are re-typed immediately.
        threshold = self.staleness_quantile.get()  # type: ignore[no-untyped-call]
        return None if threshold is None else float(threshold)


class DepthPacketIntegrityClassifier:
    """Classifies packets against per-instrument history and the trading calendar.

    Wired to the calendar engine (`1.30`) rather than reimplementing a session test:
    a packet arriving on a non-session date is a real anomaly, and the calendar is
    already the project's single answer to what a session is.
    """

    def __init__(
        self,
        calendar: NseTradingSessionCalendar | None = None,
        depth_levels_per_side: int = DEPTH_LEVELS_PER_SIDE,
        host_clock_error_seconds: float = 0.0,
    ) -> None:
        self._calendar = calendar or NseTradingSessionCalendar()
        self._depth_levels_per_side = depth_levels_per_side
        self._state_by_token: dict[int, InstrumentIntegrityState] = {}
        self._session_date_cache: dict[date, bool] = {}
        self._host_clock_error_micros = host_clock_error_seconds * 1_000_000

    def state_for(self, instrument_token: int) -> InstrumentIntegrityState:
        return self._state_by_token.setdefault(instrument_token, InstrumentIntegrityState())

    @property
    def instruments_seen(self) -> int:
        return len(self._state_by_token)

    def _is_trading_session(self, day: date) -> bool:
        cached = self._session_date_cache.get(day)
        if cached is None:
            cached = self._calendar.is_trading_session(day)
            self._session_date_cache[day] = cached
        return cached

    def classify(self, packet: DepthPacket) -> IntegrityFlag:
        """The findings for one packet, updating the instrument's carried state."""
        state = self.state_for(packet.instrument_token)
        flags = IntegrityFlag.NONE

        if packet.exchange_time is None:
            flags |= IntegrityFlag.EXCHANGE_TIME_ABSENT
        else:
            exchange_micros = int(packet.exchange_time.timestamp() * 1_000_000)
            if (
                state.last_exchange_time_micros is not None
                and exchange_micros < state.last_exchange_time_micros
            ):
                flags |= IntegrityFlag.EXCHANGE_TIME_NOT_MONOTONIC
            # Advance the watermark rather than assigning, so an out-of-order packet
            # cannot drag the reference backwards and mask the packets after it.
            state.last_exchange_time_micros = max(
                exchange_micros, state.last_exchange_time_micros or exchange_micros
            )
            flags |= self._classify_staleness(packet, state)

        flags |= self._classify_book_shape(packet)
        flags |= self._classify_duplicate(packet, state)
        flags |= self._classify_session_window(packet)

        state.packets_seen += 1
        for flag in IntegrityFlag:
            if flag is not IntegrityFlag.NONE and flag & flags:
                state.flag_counts[flag] = state.flag_counts.get(flag, 0) + 1
        return flags

    def _classify_staleness(
        self, packet: DepthPacket, state: InstrumentIntegrityState
    ) -> IntegrityFlag:
        staleness = packet.staleness_micros()
        if staleness is None:
            return IntegrityFlag.NONE
        # NOT corrected for the host clock error, and the reason is arithmetic rather than
        # oversight: the threshold is a QUANTILE of the same series, and quantiles are
        # shift-equivariant. Subtracting a constant from every observation moves the
        # threshold by exactly that constant, so the flag is invariant to a constant clock
        # error. Measured while wiring `L0.32` in: a classifier told the host runs 300 ms
        # fast produced flag-for-flag identical output. The correction belongs where an
        # ABSOLUTE instant is compared against an external boundary — `_classify_session_
        # window` — and it is applied there.
        threshold = state.derived_staleness_threshold_micros()
        # Learn from this observation only after testing against the existing
        # estimate, so a packet can never be the reason it is judged normal.
        state.staleness_quantile.update(float(staleness))  # type: ignore[no-untyped-call]
        state.staleness_sample_count += 1
        if threshold is None or staleness <= threshold:
            return IntegrityFlag.NONE
        return IntegrityFlag.STALE_BEYOND_DERIVED_THRESHOLD

    def _classify_book_shape(self, packet: DepthPacket) -> IntegrityFlag:
        flags = IntegrityFlag.NONE
        if (
            len(packet.bids) != self._depth_levels_per_side
            or len(packet.asks) != self._depth_levels_per_side
        ):
            flags |= IntegrityFlag.DEPTH_SHAPE_UNEXPECTED

        for level in (*packet.bids, *packet.asks):
            if level.price_paise < 0 or level.quantity < 0 or level.orders < 0:
                flags |= IntegrityFlag.MALFORMED_LEVEL_VALUE
                break

        best_bid, best_ask = packet.best_bid_paise, packet.best_ask_paise
        # A zero price means "no order at this level", which is normal in an illiquid
        # name and at the pre-open — it is emptiness, not a crossed book.
        if best_bid and best_ask and best_bid >= best_ask:
            flags |= IntegrityFlag.BOOK_CROSSED
        return flags

    def _classify_duplicate(
        self, packet: DepthPacket, state: InstrumentIntegrityState
    ) -> IntegrityFlag:
        fingerprint = packet.book_fingerprint()
        is_duplicate = state.last_book_fingerprint == fingerprint
        state.last_book_fingerprint = fingerprint
        return IntegrityFlag.DUPLICATE_OF_PREVIOUS_BOOK if is_duplicate else IntegrityFlag.NONE

    def _classify_session_window(self, packet: DepthPacket) -> IntegrityFlag:
        """Where the host's clock error genuinely changes the answer.

        This is an ABSOLUTE comparison — a host instant against the exchange's published
        session boundary — so a host running fast pushes packets across the 15:30 edge and
        flags real in-session data as outside it. Unlike the staleness quantile, this is not
        shift-invariant: subtracting the error measured by `L0.32` moves packets back across
        the boundary they never actually crossed.
        """
        corrected_receipt = packet.receipt_time - timedelta(
            microseconds=self._host_clock_error_micros
        )
        receipt_ist = corrected_receipt.astimezone(INDIA_MARKET_TIMEZONE)
        if not self._is_trading_session(receipt_ist.date()):
            return IntegrityFlag.OUTSIDE_SESSION_WINDOW
        if not (
            NSE_QUOTING_WINDOW_OPENS_IST <= receipt_ist.time() <= NSE_QUOTING_WINDOW_CLOSES_IST
        ):
            return IntegrityFlag.OUTSIDE_SESSION_WINDOW
        return IntegrityFlag.NONE
