"""The dependency seam between the recorder and a live socket (`Rule J`).

The recorder is written against `LiveDepthFeed` and never against `kiteconnect`, so
the whole session lifecycle — sharding, backpressure, shedding, flush-on-close — is
verifiable with no socket and no open market. `KiteLiveDepthFeed` is the only class
in this package that imports the SDK, keeping Kite a bounded adapter rather than a
dependency the rest of the system inherits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from nse_algo_trader.market_depth.depth_tape_schema import (
    DepthLevel,
    DepthPacket,
    exchange_time_from_epoch_seconds,
    price_to_paise,
)

DepthPacketHandler = Callable[[Sequence[DepthPacket]], None]


class LiveDepthFeedError(Exception):
    """Raised when the feed cannot be established or a subscription is refused."""


class LiveDepthFeed(Protocol):
    """A source of full-depth packets for a set of instrument tokens.

    Implementations must call the handler from a single thread; the recorder relies
    on that to keep `receipt_sequence` a genuine total order without locking the hot
    path.
    """

    def start(self, handler: DepthPacketHandler) -> None:
        """Connect and begin delivering packets. Returns once delivery has begun."""
        ...

    def subscribe(self, instrument_tokens: Iterable[int]) -> None:
        """Add tokens to the full-depth subscription."""
        ...

    def unsubscribe(self, instrument_tokens: Iterable[int]) -> None:
        """Drop tokens — used by mid-session shedding under budget pressure."""
        ...

    def stop(self) -> None:
        """Disconnect. Must be safe to call more than once."""
        ...

    @property
    def subscribed_tokens(self) -> frozenset[int]:
        """What the feed believes it is currently subscribed to."""
        ...


def epoch_seconds_from_sdk_timestamp(sdk_timestamp: datetime | None) -> int | None:
    """The true epoch behind the SDK's naive datetime, on a host of any timezone.

    `kiteconnect` produces its value with `datetime.fromtimestamp(seconds)` and no
    timezone, which yields the instant expressed in the *host's* local zone with the
    zone then discarded. `naive.astimezone()` interprets a naive value as local time,
    so it inverts that construction exactly — on a UTC host and on an IST host alike.

    Reading `.replace(tzinfo=UTC)` instead would be correct only by accident of this
    host running UTC, and would be silently 5h30m wrong if it ever moved to IST.
    """
    if sdk_timestamp is None:
        return None
    if sdk_timestamp.tzinfo is None:
        return int(sdk_timestamp.astimezone().timestamp())
    return int(sdk_timestamp.timestamp())


def depth_packet_from_kite_tick(
    kite_tick: dict[str, Any],
    exchange: str,
    receipt_time: datetime,
    receipt_sequence: int,
) -> DepthPacket:
    """One SDK tick dict, normalized. Raises `KeyError` on a non-full-mode tick.

    Errors are deliberately not swallowed: a quote-mode tick reaching this function
    means the subscription mode was wrong, which is a defect to surface loudly rather
    than a row to silently drop.
    """
    depth = kite_tick["depth"]
    return DepthPacket(
        instrument_token=int(kite_tick["instrument_token"]),
        exchange=exchange,
        exchange_time=exchange_time_from_epoch_seconds(
            epoch_seconds_from_sdk_timestamp(kite_tick.get("exchange_timestamp"))
        ),
        receipt_time=receipt_time,
        receipt_sequence=receipt_sequence,
        last_price_paise=price_to_paise(kite_tick["last_price"], exchange),
        last_traded_quantity=int(kite_tick.get("last_traded_quantity") or 0),
        average_traded_price_paise=price_to_paise(
            kite_tick.get("average_traded_price") or 0.0, exchange
        ),
        volume_traded=int(kite_tick.get("volume_traded") or 0),
        total_buy_quantity=int(kite_tick.get("total_buy_quantity") or 0),
        total_sell_quantity=int(kite_tick.get("total_sell_quantity") or 0),
        open_interest=int(kite_tick.get("oi") or 0),
        bids=tuple(
            DepthLevel(
                price_paise=price_to_paise(level["price"], exchange),
                quantity=int(level["quantity"]),
                orders=int(level["orders"]),
            )
            for level in depth["buy"]
        ),
        asks=tuple(
            DepthLevel(
                price_paise=price_to_paise(level["price"], exchange),
                quantity=int(level["quantity"]),
                orders=int(level["orders"]),
            )
            for level in depth["sell"]
        ),
    )


class KiteLiveDepthFeed:
    """The real feed: one `KiteTicker` websocket in full mode.

    Kite permits 3,000 instruments per connection, so a capture wider than that runs
    several of these, one per shard. Resubscription after a reconnect is handled here
    rather than by the recorder — the SDK drops the subscription on a dropped socket
    and the recorder should not have to know that.
    """

    KITE_MAX_INSTRUMENTS_PER_CONNECTION = 3000

    KITE_MAX_INSTRUMENTS_ACROSS_ALL_CONNECTIONS = 3000
    """The subscription cap is per ACCOUNT, not per connection — opening more sockets adds
    no capacity, and exceeding it starves subscribers silently rather than erroring.

    Measured on 2026-08-19, three runs on the same account, session and box:

    | subscribed | connections | cash instruments | cash ticks | per cash instrument |
    |---|---|---|---|---|
    | 2,295 | 1 | 2,295 | 1,153,998 in 42 min | ~503 |
    | 9,000 | 3 (mixed) | 2,444 | 3,170 in 11 min | 1.3 |
    | 9,000 | 3 (cash alone on two) | 6,000 | 12,496 in 7 min | 2.1 |

    Isolating cash onto its own sockets did NOT recover it, and the recorder reported **0
    packets dropped to overflow** in every run — so the client was keeping up and the packets
    were never sent. Subscribing 9,000 does not fail loudly; it serves roughly one connection
    worth and leaves the rest at a trickle, which is the quietest failure this feed can have."""
    """Kite's documented per-connection ceiling. An exchange-imposed fact."""

    def __init__(
        self,
        kite_api_key: str,
        kite_access_token: str,
        exchange_by_token: dict[int, str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._kite_api_key = kite_api_key
        self._kite_access_token = kite_access_token
        self._exchange_by_token = exchange_by_token
        self._clock = clock
        self._ticker: Any | None = None
        self._handler: DepthPacketHandler | None = None
        self._subscribed: set[int] = set()
        self._sequence = 0
        self._lock = threading.Lock()
        self.reconnect_count = 0
        self.unknown_exchange_token_count = 0

    @property
    def subscribed_tokens(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._subscribed)

    def start(self, handler: DepthPacketHandler) -> None:
        from kiteconnect import KiteTicker

        if len(self._subscribed) > self.KITE_MAX_INSTRUMENTS_PER_CONNECTION:
            raise LiveDepthFeedError(
                f"{len(self._subscribed)} tokens exceeds Kite's per-connection ceiling "
                f"of {self.KITE_MAX_INSTRUMENTS_PER_CONNECTION} — shard across feeds"
            )
        self._handler = handler
        ticker = KiteTicker(self._kite_api_key, self._kite_access_token)
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_reconnect = self._on_reconnect
        self._ticker = ticker
        ticker.connect(threaded=True)

    def _on_connect(self, ticker: Any, _response: Any) -> None:
        tokens = sorted(self.subscribed_tokens)
        if tokens:
            ticker.subscribe(tokens)
            ticker.set_mode(ticker.MODE_FULL, tokens)

    def _on_reconnect(self, _ticker: Any, _attempts: Any) -> None:
        # The socket dropped; the subscription died with it. `_on_connect` will
        # re-establish it. Counted so the session report can show the disruption.
        self.reconnect_count += 1

    def _on_ticks(self, _ticker: Any, kite_ticks: list[dict[str, Any]]) -> None:
        handler = self._handler
        if handler is None:
            return
        receipt_time = datetime.fromtimestamp(self._clock(), UTC)
        packets: list[DepthPacket] = []
        for kite_tick in kite_ticks:
            if "depth" not in kite_tick:
                continue  # an LTP/quote-mode packet, which full mode also emits at times
            token = int(kite_tick["instrument_token"])
            exchange = self._exchange_by_token.get(token)
            if exchange is None:
                # Never guess a price scale — a wrong divisor is off by orders of
                # magnitude while looking entirely plausible.
                self.unknown_exchange_token_count += 1
                continue
            self._sequence += 1
            packets.append(
                depth_packet_from_kite_tick(kite_tick, exchange, receipt_time, self._sequence)
            )
        if packets:
            handler(packets)

    def subscribe(self, instrument_tokens: Iterable[int]) -> None:
        tokens = [int(token) for token in instrument_tokens]
        with self._lock:
            self._subscribed.update(tokens)
        if self._ticker is not None and tokens:
            self._ticker.subscribe(tokens)
            self._ticker.set_mode(self._ticker.MODE_FULL, tokens)

    def unsubscribe(self, instrument_tokens: Iterable[int]) -> None:
        tokens = [int(token) for token in instrument_tokens]
        with self._lock:
            self._subscribed.difference_update(tokens)
        if self._ticker is not None and tokens:
            self._ticker.unsubscribe(tokens)

    def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.close()
            self._ticker = None
