"""The one definition of a depth-tape row, shared by the writer and every reader.

Two decisions here are load-bearing and were taken from measurements on the live
feed rather than from the SDK's convenience types (see `docs/research/206`).

**Prices are integer paise, never floats.** `kiteconnect` hands back
`raw_integer / divisor` as a Python float. For NSE and NFO the divisor is 100 and
the raw wire value is already an exact count of paise, so the float is a lossy
round-trip of an exactly-representable integer. Storing the float would bake
binary-float error into every spread and every level comparison downstream. The
tape stores the integer and records the scale.

**Exchange time is reconstructed from the epoch, not taken from the SDK.** The SDK
calls `datetime.fromtimestamp(seconds)` with no timezone, producing a naive value
in the *host's* local zone. This host runs UTC so that value is correct today; on
an IST host the identical code would be 5h30m wrong with nothing raised. Depth
packets therefore carry the raw epoch and this module attaches UTC explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntFlag

import pyarrow as pa

DEPTH_LEVELS_PER_SIDE = 5
"""Kite's full mode carries exactly five levels a side. Measured on 4,084 of 4,084
probe packets; asserted by `IntegrityFlag.DEPTH_SHAPE_UNEXPECTED` rather than assumed."""

_PRICE_DIVISOR_BY_EXCHANGE: dict[str, int] = {
    "NSE": 100,
    "NFO": 100,
    "BSE": 100,
    "BFO": 100,
}
"""Kite's wire divisor per exchange. NSE-family quotes carry two decimals, so the
raw integer is already paise. Currency (CDS) and commodity segments use different
divisors and are deliberately absent — capture refuses an exchange it has not been
told the scale for, rather than silently mis-scaling it."""


class UnsupportedExchangeScaleError(ValueError):
    """Raised when a packet arrives for an exchange whose price scale is unknown.

    Deliberately fatal rather than defaulted. A wrong divisor produces prices that
    are wrong by a factor of ten thousand yet entirely plausible-looking, which is
    exactly the class of defect that survives to production.
    """


class IntegrityFlag(IntFlag):
    """Per-row integrity findings. A flagged row is still stored — a flagged row is
    evidence, a discarded row is not."""

    NONE = 0
    EXCHANGE_TIME_ABSENT = 1 << 0
    BOOK_CROSSED = 1 << 1
    DEPTH_SHAPE_UNEXPECTED = 1 << 2
    EXCHANGE_TIME_NOT_MONOTONIC = 1 << 3
    STALE_BEYOND_DERIVED_THRESHOLD = 1 << 4
    DUPLICATE_OF_PREVIOUS_BOOK = 1 << 5
    MALFORMED_LEVEL_VALUE = 1 << 6
    OUTSIDE_SESSION_WINDOW = 1 << 7


def exchange_time_from_epoch_seconds(epoch_seconds: int | None) -> datetime | None:
    """A UTC-aware instant, or None when the feed sent no usable timestamp.

    Epoch 0 is *absent*, not 1970 — measured at roughly one in six sampled packets.
    Stored as 1970 it would poison every time-ordered read and every staleness
    statistic in the tape.
    """
    if epoch_seconds is None or epoch_seconds <= 0:
        return None
    return datetime.fromtimestamp(epoch_seconds, UTC)


def price_to_paise(price_in_rupees: float, exchange: str) -> int:
    """The exact paise count behind a price the SDK already divided into a float.

    `round` recovers the original wire integer exactly for every price NSE can
    quote: the SDK computed `raw / 100`, and the float nearest to `raw / 100` is
    always within half a paise of it at these magnitudes.
    """
    divisor = _PRICE_DIVISOR_BY_EXCHANGE.get(exchange.upper())
    if divisor is None:
        raise UnsupportedExchangeScaleError(
            f"no price scale known for exchange {exchange!r} — refusing to guess a "
            f"divisor; add it to _PRICE_DIVISOR_BY_EXCHANGE once verified on the wire"
        )
    return round(price_in_rupees * divisor)


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One price level of one side of the book."""

    price_paise: int
    quantity: int
    orders: int


@dataclass(frozen=True, slots=True)
class DepthPacket:
    """One normalized full-mode packet, before integrity classification.

    `receipt_time` and `receipt_sequence` are this process's clock and counter. They
    exist because **the feed carries no sequence number and `exchange_time` has only
    one-second resolution**, while the measured p10 inter-packet gap is 0.25s — so
    several packets per instrument per exchange-second are routine and exchange time
    alone cannot order them.

    `receipt_sequence` is monotonic **within one feed**, NOT across a session: a
    restarted recorder, or a calibration pass followed by a full session, starts a
    fresh counter at zero. Readers must order by `receipt_time` first and use the
    sequence only to break ties within one feed's sub-second batches.
    """

    instrument_token: int
    exchange: str
    exchange_time: datetime | None
    receipt_time: datetime
    receipt_sequence: int
    last_price_paise: int
    last_traded_quantity: int
    average_traded_price_paise: int
    volume_traded: int
    total_buy_quantity: int
    total_sell_quantity: int
    open_interest: int
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]

    @property
    def best_bid_paise(self) -> int | None:
        return self.bids[0].price_paise if self.bids else None

    @property
    def best_ask_paise(self) -> int | None:
        return self.asks[0].price_paise if self.asks else None

    def staleness_micros(self) -> int | None:
        """How far behind the receipt the exchange stamp was, or None if absent."""
        if self.exchange_time is None:
            return None
        return int((self.receipt_time - self.exchange_time).total_seconds() * 1_000_000)

    def book_fingerprint(self) -> tuple[int, ...]:
        """Every book field, for duplicate detection. Excludes both timestamps and the
        sequence, so a re-sent identical book compares equal to its predecessor."""
        return (
            self.last_price_paise,
            self.last_traded_quantity,
            self.average_traded_price_paise,
            self.volume_traded,
            self.total_buy_quantity,
            self.total_sell_quantity,
            self.open_interest,
            *(
                value
                for level in self.bids
                for value in (level.price_paise, level.quantity, level.orders)
            ),
            *(
                value
                for level in self.asks
                for value in (level.price_paise, level.quantity, level.orders)
            ),
        )


def _level_columns() -> list[tuple[str, pa.DataType]]:
    columns: list[tuple[str, pa.DataType]] = []
    for side in ("bid", "ask"):
        for level_index in range(DEPTH_LEVELS_PER_SIDE):
            columns.append((f"{side}_price_paise_{level_index}", pa.int64()))
            columns.append((f"{side}_quantity_{level_index}", pa.int64()))
            columns.append((f"{side}_orders_{level_index}", pa.int32()))
    return columns


DEPTH_TAPE_ARROW_SCHEMA = pa.schema(
    [
        ("instrument_token", pa.uint32()),
        ("exchange_time", pa.timestamp("us", tz="UTC")),
        ("receipt_time", pa.timestamp("us", tz="UTC")),
        ("receipt_sequence", pa.uint64()),
        ("last_price_paise", pa.int64()),
        ("last_traded_quantity", pa.int64()),
        ("average_traded_price_paise", pa.int64()),
        ("volume_traded", pa.int64()),
        ("total_buy_quantity", pa.int64()),
        ("total_sell_quantity", pa.int64()),
        ("open_interest", pa.int64()),
        *_level_columns(),
        ("staleness_micros", pa.int64()),
        ("integrity_flags", pa.uint16()),
    ],
    metadata={
        b"price_scale": b"paise",
        b"depth_levels_per_side": str(DEPTH_LEVELS_PER_SIDE).encode(),
        b"receipt_sequence_semantics": (
            b"monotonic within ONE FEED, not across a session: a restarted recorder "
            b"or a calibration pass starts a fresh counter at zero. Order by "
            b"receipt_time first and use this only to break ties inside one feed's "
            b"sub-second batches, since exchange_time is second-resolution"
        ),
    },
)

DEPTH_TAPE_COLUMN_NAMES: tuple[str, ...] = tuple(DEPTH_TAPE_ARROW_SCHEMA.names)


def packet_to_row(packet: DepthPacket, integrity_flags: IntegrityFlag) -> dict[str, object]:
    """One packet as a column-name-keyed row matching `DEPTH_TAPE_ARROW_SCHEMA`."""
    row: dict[str, object] = {
        "instrument_token": packet.instrument_token,
        "exchange_time": packet.exchange_time,
        "receipt_time": packet.receipt_time,
        "receipt_sequence": packet.receipt_sequence,
        "last_price_paise": packet.last_price_paise,
        "last_traded_quantity": packet.last_traded_quantity,
        "average_traded_price_paise": packet.average_traded_price_paise,
        "volume_traded": packet.volume_traded,
        "total_buy_quantity": packet.total_buy_quantity,
        "total_sell_quantity": packet.total_sell_quantity,
        "open_interest": packet.open_interest,
        "staleness_micros": packet.staleness_micros(),
        "integrity_flags": int(integrity_flags),
    }
    for side_name, levels in (("bid", packet.bids), ("ask", packet.asks)):
        for level_index in range(DEPTH_LEVELS_PER_SIDE):
            level = levels[level_index] if level_index < len(levels) else None
            row[f"{side_name}_price_paise_{level_index}"] = level.price_paise if level else None
            row[f"{side_name}_quantity_{level_index}"] = level.quantity if level else None
            row[f"{side_name}_orders_{level_index}"] = level.orders if level else None
    return row
