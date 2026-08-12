"""The raw pipeline for `L0.33`: what each broker said about the same instrument, when.

A consolidated feed cannot be built from one broker's tape, and the disagreement between
brokers is not recoverable after the session closes — it exists only while both are quoting.
So this is the capture layer, kept deliberately separate from the consolidation engine that
reads it: the engine can be rebuilt from the tape a hundred times, the tape cannot.

**What is stored is each broker's OWN answer, never a merged one.** Merging at capture time
would destroy the evidence the cross-check needs — once two prices become one, no later
analysis can tell a broker that lags from a broker that is wrong. Every row therefore
carries the broker that produced it, the instant this host received it, and the exchange
stamp where the broker supplies one.

**Both request instants are stored, not one.** Two brokers polled in sequence are not
sampled at the same moment, and at NSE tick rates a 200 ms gap is a real price difference
rather than a disagreement. `requested_at` and `received_at` bracket each observation so
the engine can tell those apart instead of attributing polling lag to the broker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_CROSS_BROKER_TAPE_PATH = Path(
    "~/.nse_algo_trader/cross_broker_quotes.sqlite3"
).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_quote (
    session_date TEXT NOT NULL,
    broker TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    exchange_time TEXT,
    last_price_paise INTEGER,
    best_bid_paise INTEGER,
    best_ask_paise INTEGER,
    best_bid_quantity INTEGER,
    best_ask_quantity INTEGER,
    total_buy_quantity INTEGER,
    total_sell_quantity INTEGER,
    volume_traded INTEGER,
    failure TEXT,
    PRIMARY KEY (broker, trading_symbol, requested_at)
);
CREATE INDEX IF NOT EXISTS broker_quote_by_symbol
    ON broker_quote (session_date, trading_symbol, requested_at);
CREATE INDEX IF NOT EXISTS broker_quote_by_broker
    ON broker_quote (session_date, broker, requested_at);
"""


@dataclass(frozen=True, slots=True)
class BrokerQuoteObservation:
    """One broker's answer about one instrument at one instant.

    Prices are integer paise for the same reason the depth tape uses them: a float rupee
    price makes two brokers quoting the identical price compare unequal, which would
    manufacture disagreement out of binary representation.
    """

    broker: str
    trading_symbol: str
    exchange: str
    requested_at: datetime
    received_at: datetime
    exchange_time: datetime | None = None
    last_price_paise: int | None = None
    best_bid_paise: int | None = None
    best_ask_paise: int | None = None
    best_bid_quantity: int | None = None
    best_ask_quantity: int | None = None
    total_buy_quantity: int | None = None
    total_sell_quantity: int | None = None
    volume_traded: int | None = None
    failure: str | None = None
    """Why this broker produced nothing. A failed poll is an observation about the broker,
    so it is STORED rather than dropped — a source that fails often is exactly what a
    reliability weight needs to learn from."""

    def __post_init__(self) -> None:
        """Naive timestamps are refused at the boundary, not deep inside an arithmetic.

        Adversarial review killed a whole session walk with one naive `exchange_time`:
        subtracting it raised `TypeError` inside the engine, and every comparison the run
        had learned was lost with it. A boundary that accepts only aware instants makes
        that failure impossible rather than caught.
        """
        for name, value in (
            ("requested_at", self.requested_at),
            ("received_at", self.received_at),
            ("exchange_time", self.exchange_time),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(
                    f"{name} has no timezone; a naive instant is read as UTC on this host "
                    f"and as IST on another, which silently shifts every comparison"
                )

    @property
    def is_usable(self) -> bool:
        """A price of zero is not a price. A broker returning 0 for an untraded instrument
        would otherwise fold a ~-20,000 bps difference into the pooled pair variance."""
        return (
            self.failure is None
            and self.last_price_paise is not None
            and self.last_price_paise > 0
        )

    @property
    def round_trip_seconds(self) -> float:
        return (self.received_at - self.requested_at).total_seconds()

    @property
    def has_valid_book(self) -> bool:
        """Whether this quote's own two sides can both be true at once.

        **A quote whose bid exceeds its own ask is not a book**, and no threshold is needed
        to say so — it is internally impossible. Measured on the 2026-08-12 capture: 25,761
        such rows, 10.1% of both Kite's and Angel One's (the identical rate on two
        independent brokers is what showed it was the DATA, not a parser). Their shape gives
        them away: the bid sits at last +3.03% and the ask at last -2.95%, with 40,407 shares
        at the touch against a normal 281. Those are the resting orders parked at the +/-3%
        dynamic price band, surfacing as top-of-book when the real touch is thin near the
        close. Treating them as a touch published a midpoint of an impossible book; treating
        them as a cross blamed a broker for the exchange's own band.
        """
        return (
            self.best_bid_paise is not None
            and self.best_ask_paise is not None
            and self.best_bid_paise > 0
            and self.best_ask_paise > 0
            and self.best_bid_paise <= self.best_ask_paise
        )

    @property
    def midpoint_paise(self) -> float | None:
        """The touch midpoint, or `None` when there is no valid book.

        Used rather than the last traded price wherever a real book exists: a last price is
        a historical fact that can be seconds old on an illiquid name, while the midpoint is
        what the book says right now. When the book is invalid the caller falls back to the
        last price, which is still a real trade.
        """
        if not self.has_valid_book:
            return None
        return (float(self.best_bid_paise or 0) + float(self.best_ask_paise or 0)) / 2.0


class CrossBrokerQuoteTape:
    """SQLite store of per-broker quotes, one row per broker per instrument per poll."""

    def __init__(self, database_path: Path = DEFAULT_CROSS_BROKER_TAPE_PATH) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def record(
        self, observations: Sequence[BrokerQuoteObservation], *, session_date: date
    ) -> int:
        """Store a poll's observations. Returns how many rows were written."""
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO broker_quote VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        session_date.isoformat(),
                        observation.broker,
                        observation.trading_symbol,
                        observation.exchange,
                        observation.requested_at.isoformat(),
                        observation.received_at.isoformat(),
                        observation.exchange_time.isoformat()
                        if observation.exchange_time
                        else None,
                        observation.last_price_paise,
                        observation.best_bid_paise,
                        observation.best_ask_paise,
                        observation.best_bid_quantity,
                        observation.best_ask_quantity,
                        observation.total_buy_quantity,
                        observation.total_sell_quantity,
                        observation.volume_traded,
                        observation.failure,
                    )
                    for observation in observations
                ],
            )
        return len(observations)

    def observations(
        self, *, session_date: date | None = None, trading_symbol: str | None = None
    ) -> tuple[BrokerQuoteObservation, ...]:
        """Everything recorded, oldest first, filtered as asked."""
        conditions, parameters = [], []
        if session_date is not None:
            conditions.append("session_date = ?")
            parameters.append(session_date.isoformat())
        if trading_symbol is not None:
            conditions.append("trading_symbol = ?")
            parameters.append(trading_symbol)
        query = "SELECT * FROM broker_quote"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY requested_at, broker"
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_observation_from_row(row) for row in rows)

    def session_dates(self) -> tuple[date, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_date FROM broker_quote ORDER BY session_date"
            ).fetchall()
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def coverage(self, *, session_date: date) -> dict[str, int]:
        """Rows per broker for one session — the first thing to check before trusting a fit."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT broker, COUNT(*) FROM broker_quote WHERE session_date = ? "
                "GROUP BY broker",
                (session_date.isoformat(),),
            ).fetchall()
        return {str(broker): int(count) for broker, count in rows}


def _observation_from_row(row: sqlite3.Row) -> BrokerQuoteObservation:
    return BrokerQuoteObservation(
        broker=row["broker"],
        trading_symbol=row["trading_symbol"],
        exchange=row["exchange"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        received_at=datetime.fromisoformat(row["received_at"]),
        exchange_time=datetime.fromisoformat(row["exchange_time"])
        if row["exchange_time"]
        else None,
        last_price_paise=row["last_price_paise"],
        best_bid_paise=row["best_bid_paise"],
        best_ask_paise=row["best_ask_paise"],
        best_bid_quantity=row["best_bid_quantity"],
        best_ask_quantity=row["best_ask_quantity"],
        total_buy_quantity=row["total_buy_quantity"],
        total_sell_quantity=row["total_sell_quantity"],
        volume_traded=row["volume_traded"],
        failure=row["failure"],
    )


def rupees_to_paise(rupees: float | None) -> int | None:
    """Broker APIs speak rupee floats; this tape speaks paise, and rounds once, here."""
    if rupees is None:
        return None
    return round(float(rupees) * 100)


def now_utc() -> datetime:
    return datetime.now(UTC)
