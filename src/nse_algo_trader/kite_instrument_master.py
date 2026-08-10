"""The authoritative record of every tradeable contract, keyed for stable identity.

**This is an ingest and store, not an engine** (R.23b) — it fetches a file,
validates it, and persists it. There is no solver. Named for what it is.

It is decision-path all the same: it defines what the whole system believes is
tradeable, and every universe scan, subscription and order references it.

**Identity — the correction that came from measurement (A.34).** Kite reuses
``instrument_token`` once a contract expires, so the token cannot be the identity.
The plan originally specified ``(exchange, tradingsymbol)``; measured across all
113,955 rows of the real dump, that pair **collides**::

    BSE:INFRA  token 139444228  "MIRAE ASSET MUTUAL FUND"  segment=BSE
    BSE:INFRA  token    282377  "BSE INDEX INFRA"          segment=INDICES

Two instruments, one key. Building on it would have silently dropped a row on
every ingest while looking healthy. The identity is therefore
``(exchange, segment, tradingsymbol)`` — measured collision-free.

Design record: ``docs/research/202_kite_instrument_master_spec.md``.
SOTA analogs: Zipline's adjustments database, LEAN's Security Master.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Final

# The raw dump. Measured 2026-08-10: HTTP 200, 9,289,898 bytes, 113,955 rows,
# **no authentication**. Deliberately not routed through the authenticated
# KiteConnect client (L3.28): the universe must stay readable when the daily TOTP
# token has lapsed, which is the failure L13.10 exists to prevent.
INSTRUMENT_DUMP_URL: Final[str] = "https://api.kite.trade/instruments"

REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "instrument_token", "exchange_token", "tradingsymbol", "name", "last_price",
        "expiry", "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange",
    }
)

# A dump that loses more than this share of the previous day's universe is
# treated as truncated rather than as mass delisting. Expressed as a fraction, so
# it holds at any universe size (R.03); a real delisting wave moves single-digit
# percentages, never a third of the file.
_MAXIMUM_TOLERATED_SHRINKAGE = Decimal("0.30")

InstrumentIdentity = tuple[str, str, str]


class InstrumentMasterError(Exception):
    """Base class for instrument-master failures."""


class InstrumentDumpValidationError(InstrumentMasterError):
    """The dump is unusable: missing columns, empty, unparseable, or truncated.

    Raised rather than partially ingested. A dump that parses but is truncated is
    more dangerous than one that fails outright, because it silently deletes the
    universe instead of announcing it.
    """


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """One tradeable contract, as Kite describes it."""

    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    last_price: Decimal
    expiry: date | None
    strike: Decimal
    tick_size: Decimal
    lot_size: int
    instrument_type: str
    segment: str
    exchange: str

    @property
    def identity(self) -> InstrumentIdentity:
        """The stable key. Never the token — see the module docstring."""
        return (self.exchange, self.segment, self.tradingsymbol)


@dataclass(frozen=True, slots=True)
class TokenReassignment:
    """A token now pointing at a different contract than it did before.

    A normal lifecycle event once a contract expires — but it must be *visible*.
    Silently accepting it merges an expired contract's history into a new one.
    """

    instrument_token: int
    previous_identity: InstrumentIdentity
    current_identity: InstrumentIdentity
    observed_on: date


def _decimal_or_zero(raw: str, column: str, row_number: int) -> Decimal:
    text = (raw or "").strip()
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise InstrumentDumpValidationError(
            f"row {row_number}: {column}={raw!r} is not a valid decimal"
        ) from exc


def _expiry_or_none(raw: str, row_number: int) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None  # cash rows carry no expiry — the shape that breaks naive parsers
    try:
        # date.fromisoformat, not strptime: an expiry is a calendar date with no
        # time or zone, and strptime would manufacture a naive datetime that then
        # has to be discarded. Naive datetimes in a financial path are a defect
        # class of their own (the DTZ ruff rules exist for it).
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InstrumentDumpValidationError(
            f"row {row_number}: expiry={raw!r} is not an ISO date"
        ) from exc


def parse_instrument_dump(dump_text: str) -> list[InstrumentRecord]:
    """Parse the Kite instrument CSV into typed records.

    Every row must parse. A row that cannot is an error, never a skip: skipping
    loses an instrument silently, and a missing instrument is indistinguishable
    from one that was never listed.

    Raises:
        InstrumentDumpValidationError: empty, header-only, missing a required
            column, or containing a row that will not parse.
    """
    if not dump_text.strip():
        raise InstrumentDumpValidationError("the instrument dump is empty")

    reader = csv.DictReader(StringIO(dump_text))
    present = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise InstrumentDumpValidationError(
            f"the instrument dump is missing required column(s): {sorted(missing)}"
        )

    records: list[InstrumentRecord] = []
    for row_number, row in enumerate(reader, start=2):
        if any(row.get(column) is None for column in REQUIRED_COLUMNS):
            raise InstrumentDumpValidationError(
                f"row {row_number} is malformed: expected {len(REQUIRED_COLUMNS)} columns"
            )
        try:
            records.append(
                InstrumentRecord(
                    instrument_token=int(row["instrument_token"]),
                    exchange_token=int(row["exchange_token"]),
                    tradingsymbol=row["tradingsymbol"].strip(),
                    name=row["name"].strip(),
                    last_price=_decimal_or_zero(row["last_price"], "last_price", row_number),
                    expiry=_expiry_or_none(row["expiry"], row_number),
                    strike=_decimal_or_zero(row["strike"], "strike", row_number),
                    tick_size=_decimal_or_zero(row["tick_size"], "tick_size", row_number),
                    lot_size=int(row["lot_size"] or 0),
                    instrument_type=row["instrument_type"].strip(),
                    segment=row["segment"].strip(),
                    exchange=row["exchange"].strip(),
                )
            )
        except ValueError as exc:
            raise InstrumentDumpValidationError(
                f"row {row_number} could not be parsed: {exc}"
            ) from exc

    if not records:
        raise InstrumentDumpValidationError("the instrument dump contains no rows")
    return records


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS instrument_master (
    ingested_on      TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    segment          TEXT NOT NULL,
    tradingsymbol    TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    exchange_token   INTEGER NOT NULL,
    name             TEXT NOT NULL,
    last_price       TEXT NOT NULL,
    expiry           TEXT,
    strike           TEXT NOT NULL,
    tick_size        TEXT NOT NULL,
    lot_size         INTEGER NOT NULL,
    instrument_type  TEXT NOT NULL,
    PRIMARY KEY (ingested_on, exchange, segment, tradingsymbol)
);
CREATE INDEX IF NOT EXISTS instrument_master_by_token
    ON instrument_master (instrument_token, ingested_on);
CREATE TABLE IF NOT EXISTS instrument_token_reassignment (
    observed_on        TEXT NOT NULL,
    instrument_token   INTEGER NOT NULL,
    previous_exchange  TEXT NOT NULL,
    previous_segment   TEXT NOT NULL,
    previous_symbol    TEXT NOT NULL,
    current_exchange   TEXT NOT NULL,
    current_segment    TEXT NOT NULL,
    current_symbol     TEXT NOT NULL,
    PRIMARY KEY (observed_on, instrument_token)
);
"""


@dataclass(slots=True)
class InstrumentMasterStore:
    """Point-in-time store of the instrument universe.

    Keyed on ``(ingested_on, exchange, segment, tradingsymbol)`` so the universe
    is queryable *as of a date* — the property L0.05 and L0.06 build on, and the
    reason a backtest can be told what was listed at the time rather than what is
    listed now.
    """

    database_path: Path
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def ingest(
        self, records: list[InstrumentRecord], *, ingested_on: date
    ) -> list[TokenReassignment]:
        """Persist a day's universe, returning any token reassignments observed.

        Idempotent for a given date: a retried fetch replaces the day's rows
        rather than doubling them.

        Raises:
            InstrumentDumpValidationError: the dump has lost more than the
                tolerated share of the previous universe, which means truncation
                rather than delisting.
        """
        if not records:
            raise InstrumentDumpValidationError("refusing to ingest an empty universe")

        self._refuse_if_implausibly_small(records, ingested_on)
        reassignments = self._detect_token_reassignments(records, ingested_on)

        stamp = ingested_on.isoformat()
        with self._connection:
            self._connection.execute(
                "DELETE FROM instrument_master WHERE ingested_on = ?", (stamp,)
            )
            self._connection.executemany(
                "INSERT INTO instrument_master (ingested_on, exchange, segment, tradingsymbol,"
                " instrument_token, exchange_token, name, last_price, expiry, strike, tick_size,"
                " lot_size, instrument_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        stamp, r.exchange, r.segment, r.tradingsymbol, r.instrument_token,
                        r.exchange_token, r.name, str(r.last_price),
                        r.expiry.isoformat() if r.expiry else None,
                        str(r.strike), str(r.tick_size), r.lot_size, r.instrument_type,
                    )
                    for r in records
                ],
            )
            self._connection.executemany(
                "INSERT OR REPLACE INTO instrument_token_reassignment (observed_on,"
                " instrument_token, previous_exchange, previous_segment, previous_symbol,"
                " current_exchange, current_segment, current_symbol) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (stamp, r.instrument_token, *r.previous_identity, *r.current_identity)
                    for r in reassignments
                ],
            )
        return reassignments

    def instruments_as_of(
        self, as_of: date, *, exchange: str | None = None
    ) -> list[InstrumentRecord]:
        """The universe as it stood on the most recent ingest at or before ``as_of``."""
        stamp = as_of.isoformat()
        effective = self._connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on <= ?", (stamp,)
        ).fetchone()[0]
        if effective is None:
            return []
        query = (
            "SELECT instrument_token, exchange_token, tradingsymbol, name, last_price, expiry,"
            " strike, tick_size, lot_size, instrument_type, segment, exchange"
            " FROM instrument_master WHERE ingested_on = ?"
        )
        parameters: list[object] = [effective]
        if exchange is not None:
            query += " AND exchange = ?"
            parameters.append(exchange)
        return [
            InstrumentRecord(
                instrument_token=row[0], exchange_token=row[1], tradingsymbol=row[2],
                name=row[3], last_price=Decimal(row[4]),
                expiry=date.fromisoformat(row[5]) if row[5] else None,
                strike=Decimal(row[6]), tick_size=Decimal(row[7]), lot_size=row[8],
                instrument_type=row[9], segment=row[10], exchange=row[11],
            )
            for row in self._connection.execute(query, parameters)
        ]

    def _previous_ingest_before(self, ingested_on: date) -> str | None:
        previous: str | None = self._connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on < ?",
            (ingested_on.isoformat(),),
        ).fetchone()[0]
        return previous

    def _refuse_if_implausibly_small(
        self, records: list[InstrumentRecord], ingested_on: date
    ) -> None:
        previous = self._previous_ingest_before(ingested_on)
        if previous is None:
            return
        previous_count = self._connection.execute(
            "SELECT COUNT(*) FROM instrument_master WHERE ingested_on = ?", (previous,)
        ).fetchone()[0]
        if not previous_count:
            return
        shrinkage = Decimal(previous_count - len(records)) / Decimal(previous_count)
        if shrinkage > _MAXIMUM_TOLERATED_SHRINKAGE:
            raise InstrumentDumpValidationError(
                f"refusing a truncated dump: {len(records)} rows against {previous_count} on "
                f"{previous} ({shrinkage:.0%} smaller). A real delisting wave moves single-digit "
                "percentages; this is a partial download."
            )

    def _detect_token_reassignments(
        self, records: list[InstrumentRecord], ingested_on: date
    ) -> list[TokenReassignment]:
        """Find tokens now pointing at a different contract than before (L0.02)."""
        previous = self._previous_ingest_before(ingested_on)
        if previous is None:
            return []
        known = {
            row[0]: (row[1], row[2], row[3])
            for row in self._connection.execute(
                "SELECT instrument_token, exchange, segment, tradingsymbol"
                " FROM instrument_master WHERE ingested_on = ?",
                (previous,),
            )
        }
        return [
            TokenReassignment(
                instrument_token=record.instrument_token,
                previous_identity=known[record.instrument_token],
                current_identity=record.identity,
                observed_on=ingested_on,
            )
            for record in records
            if record.instrument_token in known
            and known[record.instrument_token] != record.identity
        ]
