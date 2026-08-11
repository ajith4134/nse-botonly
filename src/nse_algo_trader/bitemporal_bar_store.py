"""Price bars stored so that a reader can only see what was knowable at the time.

**This is an ingest and store, not an engine** (R.23b) — no solver, no inference.
Decision-path, because every indicator, signal and backtest reads from it.

**The property it exists to guarantee.** A bar carries two times that matter:

* **event time** (``bar_timestamp``) — when the bar's interval began;
* **availability time** (``available_from``) — when this system could first have
  *acted* on it.

Reads filter on **availability**, never on event time. The 09:15 five-minute bar
is not a tradeable fact at 09:17; a bar backfilled three days late was never
actionable on the day it describes. **Filtering on event time is the most common
way a backtest lies about itself, and it lies optimistically** — which is the
direction that costs money.

**Times are stored normalised to UTC, and compared only in that form.** The first
version stored ``datetime.isoformat()`` with whatever offset the caller supplied
and compared those strings in SQL. Adversarial review reproduced the consequence:
a bar available at ``09:20+05:30`` was returned when asked for ``09:30+05:45`` —
the *same instant as 09:15 IST*, five minutes before the bar existed. TEXT order
is not chronological order across differing offsets, so the module's one guarantee
failed in the optimistic direction, by string comparison alone. Every comparison
column is now a UTC instant; the caller's original offset is stored beside it and
restored on read, so nothing is lost but the ambiguity.

**Two corrections to the retained schema**, measured before designing
(``docs/research/203``). The 659,990 surviving bars were keyed on
``instrument_token`` — the transient handle Kite reuses once a contract expires —
so the schema permitted exactly the corruption the instrument master exists to
prevent, one layer down. And prices were stored as ``real``. Identity here is the
stable ``(exchange, segment, tradingsymbol)`` triple, and prices are exact.

The bitemporal work itself was sound and is carried forward: the retained data has
zero rows where availability precedes the event.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Final

InstrumentIdentity = tuple[str, str, str]
StorageKey = tuple[str, str, str, str, str]


class BarStoreError(Exception):
    """Base class for every bar-store failure, including wrapped sqlite errors."""


class BarValidationError(BarStoreError):
    """A bar is internally inconsistent and is refused rather than stored.

    Refused, not repaired: a corrupt bar that is silently corrected is
    indistinguishable from a real one downstream, and every indicator built on it
    inherits the error without any signal that something was wrong.
    """


def _utc_key(moment: datetime) -> str:
    """The sortable, comparable form of an instant.

    Normalising to a single offset is what makes ``<=`` in SQL mean "earlier
    than" rather than "alphabetically before". Microseconds are padded so the
    strings are fixed-width, because ``isoformat()`` omits them when zero and a
    shorter string sorts before a longer one that shares its prefix.
    """
    instant = moment.astimezone(UTC)
    return f"{instant:%Y-%m-%dT%H:%M:%S}.{instant.microsecond:06d}Z"


def _offset_seconds(moment: datetime) -> int:
    """The caller's original offset, kept so the read gives back what was written."""
    offset = moment.utcoffset()
    if offset is None:  # unreachable after validation; kept so the type is honest
        raise BarValidationError("cannot record the offset of a naive datetime")
    return int(offset.total_seconds())


def _restore_offset(utc_text: str, offset_seconds: int) -> datetime:
    instant = datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return instant.astimezone(timezone(timedelta(seconds=offset_seconds)))


def _require_positive_integer(value: object, label: str) -> int:
    """Reject bools and floats explicitly: ``True`` is an ``int`` and ``10.5`` is not.

    A float volume survived the original checks and only failed later, on read,
    as a raw ``ValueError`` — a corrupt row that writes cleanly and explodes in
    the reader is the worst arrangement of the two.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise BarValidationError(f"{label} must be an int, not {type(value).__name__}")
    if value <= 0:
        raise BarValidationError(f"{label} must be positive; got {value}")
    return value


@dataclass(frozen=True, slots=True)
class BarRecord:
    """One OHLCV bar, with the time it became actionable."""

    exchange: str
    segment: str
    tradingsymbol: str
    instrument_token: int
    bar_interval: str
    bar_timestamp: datetime
    available_from: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    open_interest: int | None

    def __post_init__(self) -> None:
        for label, text in (
            ("exchange", self.exchange), ("segment", self.segment),
            ("tradingsymbol", self.tradingsymbol), ("bar_interval", self.bar_interval),
        ):
            if not isinstance(text, str) or not text.strip():
                raise BarValidationError(
                    f"{label} must be a non-empty string; an empty identity component "
                    "produces a row no downstream reader can ever address"
                )

        _require_positive_integer(self.instrument_token, "instrument_token")

        for label, moment in (
            ("bar_timestamp", self.bar_timestamp),
            ("available_from", self.available_from),
        ):
            if not isinstance(moment, datetime):
                raise BarValidationError(f"{label} must be a datetime, not {type(moment).__name__}")
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise BarValidationError(
                    f"{label} must carry a timezone. A naive datetime cannot be compared "
                    "across venues, and IST/UTC confusion is a money bug, not a formatting one."
                )
        # Inclusive: a tick-derived bar can be actionable the instant it closes.
        if self.available_from < self.bar_timestamp:
            raise BarValidationError(
                f"available_from {self.available_from.isoformat()} precedes bar_timestamp "
                f"{self.bar_timestamp.isoformat()}; a bar cannot have been actionable "
                "before it existed"
            )

        for label, price in (
            ("open_price", self.open_price), ("high_price", self.high_price),
            ("low_price", self.low_price), ("close_price", self.close_price),
        ):
            if not isinstance(price, Decimal):
                raise BarValidationError(
                    f"{label} must be an exact Decimal, not {type(price).__name__}: a float "
                    "cannot represent a tick exactly and the error compounds across every "
                    "indicator that reads it"
                )
            if not price.is_finite():
                raise BarValidationError(f"{label} must be finite; got {price}")
            if price <= 0:
                raise BarValidationError(
                    f"{label} must be positive; got {price}. No NSE instrument trades at or "
                    "below zero, and a negative price silently inverts every return computed "
                    "from it"
                )

        if self.high_price < self.low_price:
            raise BarValidationError(
                f"high {self.high_price} is below low {self.low_price}; a corrupt bar "
                "silently breaks every range and volatility calculation built on it"
            )
        # The same reasoning the high/low check exists for: an open or close outside
        # the bar's own range is a corrupt bar, and every breakout rule reads it.
        for label, price in (("open_price", self.open_price), ("close_price", self.close_price)):
            if not self.low_price <= price <= self.high_price:
                raise BarValidationError(
                    f"{label} {price} lies outside the bar's range "
                    f"[{self.low_price}, {self.high_price}]"
                )

        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise BarValidationError(f"volume must be an int, not {type(self.volume).__name__}")
        if self.volume < 0:
            raise BarValidationError(f"volume cannot be negative; got {self.volume}")

        if self.open_interest is not None:
            if isinstance(self.open_interest, bool) or not isinstance(self.open_interest, int):
                raise BarValidationError(
                    f"open_interest must be an int or None, not {type(self.open_interest).__name__}"
                )
            if self.open_interest < 0:
                raise BarValidationError(
                    f"open_interest cannot be negative; got {self.open_interest}"
                )

    @property
    def identity(self) -> InstrumentIdentity:
        """The stable instrument key. Never the token — Kite reuses it (L0.02)."""
        return (self.exchange, self.segment, self.tradingsymbol)

    @property
    def storage_key(self) -> StorageKey:
        """What makes a bar unique: an instrument, an interval, and an *instant*.

        The instant, not its spelling — the same moment written ``09:15+05:30``
        and ``03:45+00:00`` is one bar, and re-ingesting it must replace rather
        than duplicate.
        """
        return (*self.identity, self.bar_interval, _utc_key(self.bar_timestamp))


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS price_bar (
    exchange                TEXT NOT NULL,
    segment                 TEXT NOT NULL,
    tradingsymbol           TEXT NOT NULL,
    bar_interval            TEXT NOT NULL,
    -- Comparison columns. Always UTC, always fixed width, so SQL TEXT order is
    -- chronological order. Never store a caller-supplied offset spelling here.
    bar_timestamp_utc       TEXT NOT NULL,
    available_from_utc      TEXT NOT NULL,
    -- The caller's original offsets, so a read returns what a write was given.
    bar_timestamp_offset    INTEGER NOT NULL,
    available_from_offset   INTEGER NOT NULL,
    instrument_token        INTEGER NOT NULL,
    open_price              TEXT NOT NULL,
    high_price              TEXT NOT NULL,
    low_price               TEXT NOT NULL,
    close_price             TEXT NOT NULL,
    volume                  INTEGER NOT NULL,
    open_interest           INTEGER,
    PRIMARY KEY (exchange, segment, tradingsymbol, bar_interval, bar_timestamp_utc)
);
-- Availability leads every read, so it leads the index.
CREATE INDEX IF NOT EXISTS price_bar_by_availability
    ON price_bar (available_from_utc, exchange, segment, tradingsymbol);
CREATE INDEX IF NOT EXISTS price_bar_by_token
    ON price_bar (instrument_token, bar_timestamp_utc);
"""

_COLUMNS: Final = (
    "exchange, segment, tradingsymbol, bar_interval, bar_timestamp_utc, available_from_utc,"
    " bar_timestamp_offset, available_from_offset, instrument_token, open_price, high_price,"
    " low_price, close_price, volume, open_interest"
)


@dataclass(slots=True)
class BitemporalBarStore:
    """Bars, readable only as of a moment in time.

    WAL with cross-thread access, so a dashboard or scanner can read while an
    ingest writes. Usable as a context manager.
    """

    database_path: Path
    busy_timeout_seconds: float = 30.0
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database_path, timeout=self.busy_timeout_seconds, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BitemporalBarStore:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def write(self, bars: list[BarRecord]) -> int:
        """Persist bars, replacing any already stored for the same instant.

        Replacement rather than rejection is deliberate: exchanges revise bars,
        and carrying both versions would leave readers to guess which is current.

        Returns:
            The number of bars written.

        Raises:
            BarStoreError: the write failed.
        """
        if not bars:
            return 0
        try:
            with self._connection:
                self._connection.executemany(
                    f"INSERT OR REPLACE INTO price_bar ({_COLUMNS})"  # noqa: S608
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            bar.exchange, bar.segment, bar.tradingsymbol, bar.bar_interval,
                            _utc_key(bar.bar_timestamp), _utc_key(bar.available_from),
                            _offset_seconds(bar.bar_timestamp),
                            _offset_seconds(bar.available_from),
                            bar.instrument_token, str(bar.open_price), str(bar.high_price),
                            str(bar.low_price), str(bar.close_price), bar.volume,
                            bar.open_interest,
                        )
                        for bar in bars
                    ],
                )
        except sqlite3.Error as exc:
            raise BarStoreError(f"failed to write {len(bars)} bars: {exc}") from exc
        return len(bars)

    def bars_as_of(
        self,
        as_of: datetime,
        *,
        identity: InstrumentIdentity | None = None,
        bar_interval: str | None = None,
    ) -> list[BarRecord]:
        """Every bar that was **available** at ``as_of``, oldest event first.

        The safe read is the only read: there is deliberately no way to ask for
        bars by event time alone, because that is the query that lets a backtest
        see the future.

        ``as_of`` may carry any offset — it is normalised to the same UTC form
        the rows are stored in, so the comparison is between instants rather
        than between strings that merely look like instants.

        Raises:
            BarValidationError: ``as_of`` is naive. Comparing a naive moment
                against stored offsets would silently shift the cutoff.
            BarStoreError: the read failed, or a stored row is malformed.
        """
        if not isinstance(as_of, datetime):
            raise BarValidationError(f"as_of must be a datetime, not {type(as_of).__name__}")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise BarValidationError("as_of must carry a timezone")

        query = f"SELECT {_COLUMNS} FROM price_bar WHERE available_from_utc <= ?"  # noqa: S608
        parameters: list[object] = [_utc_key(as_of)]
        if identity is not None:
            query += " AND exchange = ? AND segment = ? AND tradingsymbol = ?"
            parameters.extend(identity)
        if bar_interval is not None:
            query += " AND bar_interval = ?"
            parameters.append(bar_interval)
        query += " ORDER BY bar_timestamp_utc, exchange, segment, tradingsymbol"

        try:
            rows = self._connection.execute(query, parameters).fetchall()
            # Inside the guard deliberately: a malformed stored row must surface as
            # BarStoreError like any other store failure, not as a raw ValueError
            # from int()/Decimal() escaping the documented contract.
            return [self._to_record(row) for row in rows]
        except (sqlite3.Error, ValueError, ArithmeticError, BarValidationError) as exc:
            raise BarStoreError(f"failed to read bars as of {as_of.isoformat()}: {exc}") from exc

    @staticmethod
    def _to_record(row: tuple[object, ...]) -> BarRecord:
        return BarRecord(
            exchange=str(row[0]), segment=str(row[1]), tradingsymbol=str(row[2]),
            bar_interval=str(row[3]),
            bar_timestamp=_restore_offset(str(row[4]), int(str(row[6]))),
            available_from=_restore_offset(str(row[5]), int(str(row[7]))),
            instrument_token=int(str(row[8])),
            open_price=Decimal(str(row[9])), high_price=Decimal(str(row[10])),
            low_price=Decimal(str(row[11])), close_price=Decimal(str(row[12])),
            volume=int(str(row[13])),
            open_interest=None if row[14] is None else int(str(row[14])),
        )
