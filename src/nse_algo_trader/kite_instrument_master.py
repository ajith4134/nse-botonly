"""The authoritative record of every tradeable contract, keyed for stable identity.

**This is an ingest and store, not an engine** (R.23b) — it fetches a file,
validates it, and persists it. There is no solver. Named for what it is.

It is decision-path all the same: it defines what the whole system believes is
tradeable, and every universe scan, subscription and order references it.

**Identity — corrected by measurement (A.34).** Kite reuses ``instrument_token``
once a contract expires, so the token cannot be the identity. The plan originally
specified ``(exchange, tradingsymbol)``; measured across all 113,955 rows of the
real dump, that pair **collides**::

    BSE:INFRA  token 139444228  "MIRAE ASSET MUTUAL FUND"  segment=BSE
    BSE:INFRA  token    282377  "BSE INDEX INFRA"          segment=INDICES

The identity is therefore ``(exchange, segment, tradingsymbol)`` — collision-free
across the real dump.

**Rewritten 2026-08-10 after adversarial review found 24 defects.** The three that
mattered most, all reproduced against the real dump:

* the token-reassignment guard consulted only the *immediately preceding* ingest,
  and Kite drops an expired contract before reusing its token — so the token is
  absent from yesterday at the moment of reuse and the guard never fired. It now
  searches the **full history**.
* a same-day re-ingest bypassed the truncation guard entirely (it compared
  against a strictly-earlier date, absent on a first ingest or after a gap) and
  the DELETE then wiped the universe. Reproduced: 94 rows → 1 row, no error.
* the shrinkage threshold was a magic ``0.30`` whose stated rationale — "delisting
  moves single-digit percentages" — is contradicted by the file itself: real
  expiry cohorts are 12.5% and 10.9% of the universe. It is now **derived from
  the baseline dump's own expiry-cohort distribution**.

Design record: ``docs/research/202_kite_instrument_master_spec.md``.
SOTA analogs: Zipline's adjustments database, LEAN's Security Master.
"""

from __future__ import annotations

import csv
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from types import TracebackType
from typing import Final

# The raw dump. Measured 2026-08-10: HTTP 200, 9,289,898 bytes, 113,955 rows,
# **no authentication**. Deliberately not routed through the authenticated
# KiteConnect client (L3.28): the universe must stay readable when the daily TOTP
# token has lapsed, which is the failure L13.10 exists to prevent.
INSTRUMENT_DUMP_URL: Final[str] = "https://api.kite.trade/instruments"

EXPECTED_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)
REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(EXPECTED_COLUMNS)

# Legitimate one-day churn is bounded by the largest expiring cohort, so the
# tolerance is DERIVED from the baseline dump's own expiry distribution rather
# than guessed (R.03). The multiple below is the only judgement left: it allows
# one cohort to expire while a second, smaller one is delisted the same day.
_EXPIRY_COHORT_SAFETY_MULTIPLE: Final[Decimal] = Decimal(2)
# Floor for the derived tolerance, used when the baseline carries no expiries at
# all (a cash-only universe), where cohort analysis says nothing.
_MINIMUM_SHRINKAGE_TOLERANCE: Final[Decimal] = Decimal("0.05")

_FETCH_ATTEMPTS: Final[int] = 3
_FETCH_BACKOFF_SECONDS: Final[float] = 2.0
# A body far smaller than this is a truncated download, not a quiet market: the
# NFO exchange alone is ~35,000 contracts, and the real dump is 9.3 MB.
_MINIMUM_PLAUSIBLE_DUMP_BYTES: Final[int] = 1_000_000

InstrumentIdentity = tuple[str, str, str]


class InstrumentMasterError(Exception):
    """Base class for every instrument-master failure.

    Every error this module raises derives from it, including ones originating in
    sqlite or urllib — a caller writing ``except InstrumentMasterError`` must not
    be surprised by a raw ``sqlite3.IntegrityError`` (review defect 10).
    """


class InstrumentDumpValidationError(InstrumentMasterError):
    """The dump is unusable: missing columns, empty, unparseable, or truncated.

    Raised rather than partially ingested. A dump that parses but is truncated is
    more dangerous than one that fails outright, because it silently deletes the
    universe instead of announcing it.
    """


class InstrumentDumpFetchError(InstrumentMasterError):
    """The dump could not be retrieved after retries."""


class InstrumentStoreError(InstrumentMasterError):
    """The store could not be read or written."""


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

    def __post_init__(self) -> None:
        # An empty identity field produces the degenerate key ('', '', '') and
        # collapses unrelated instruments together (review defect 15).
        for part, value in zip(
            ("exchange", "segment", "tradingsymbol"), self.identity, strict=True
        ):
            if not value:
                raise InstrumentDumpValidationError(
                    f"{part} is empty; an instrument identity cannot be blank"
                )

    @property
    def identity(self) -> InstrumentIdentity:
        """The stable key. Never the token — see the module docstring."""
        return (self.exchange, self.segment, self.tradingsymbol)

    @property
    def contract_terms(self) -> tuple[str, str, str]:
        """What the contract *is*, independent of what it is called.

        Distinguishes a symbol rename from genuine token reuse: a rename keeps
        the same expiry, strike and type; a reused token points at a different
        contract entirely (review defect 13).
        """
        return (
            self.expiry.isoformat() if self.expiry else "",
            str(self.strike),
            self.instrument_type,
        )


@dataclass(frozen=True, slots=True)
class TokenReassignment:
    """A token now pointing at a genuinely different contract than before.

    A normal lifecycle event once a contract expires — but it must be *visible*.
    Silently accepting it merges an expired contract's history into a new one.
    Symbol renames are excluded: same contract, new name, not reuse.
    """

    instrument_token: int
    previous_identity: InstrumentIdentity
    current_identity: InstrumentIdentity
    previous_seen_on: date
    observed_on: date


def _require_finite_decimal(raw: str, column: str, row_number: int) -> Decimal:
    """Parse a decimal, refusing blanks, NaN and Infinity.

    ``Decimal("NaN")`` does not raise ``InvalidOperation``, so the obvious
    implementation lets it through and every later comparison against it silently
    returns False (review defect 7). A blank is refused rather than coerced to
    zero, because a missing lot size that becomes ``0`` is an order-sizing hazard
    indistinguishable from an index's legitimate zero (review defect 9).
    """
    text = (raw or "").strip()
    if not text:
        raise InstrumentDumpValidationError(
            f"row {row_number}: {column} is blank; a missing numeric is not zero"
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InstrumentDumpValidationError(
            f"row {row_number}: {column}={raw!r} is not a valid decimal"
        ) from exc
    if not value.is_finite():
        raise InstrumentDumpValidationError(f"row {row_number}: {column}={raw!r} is not finite")
    return value


def _require_integer(raw: str, column: str, row_number: int) -> int:
    """Parse an integer, refusing the shapes ``int()`` silently accepts.

    ``int()`` tolerates a leading ``+``, underscores and Unicode digits —
    ``int("١٢٣") == 123``. None of those belong in an exchange feed, and each is a
    silent mis-read (review defect 9). Surrounding whitespace is stripped rather
    than refused: it is a formatting artefact, not a value that could be
    misread by an order of magnitude.
    """
    text = (raw or "").strip()
    if not text or not (text.isascii() and text.isdigit()):
        raise InstrumentDumpValidationError(
            f"row {row_number}: {column}={raw!r} must be plain ASCII digits"
        )
    return int(text)


def _expiry_or_none(raw: str, row_number: int) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None  # cash rows carry no expiry — the shape that breaks naive parsers
    try:
        # date.fromisoformat, not strptime: an expiry is a calendar date with no
        # time or zone, and strptime would manufacture a naive datetime.
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InstrumentDumpValidationError(
            f"row {row_number}: expiry={raw!r} is not an ISO date"
        ) from exc


def fetch_instrument_dump(
    *, url: str = INSTRUMENT_DUMP_URL, attempts: int = _FETCH_ATTEMPTS
) -> str:
    """Retrieve the instrument dump over plain HTTPS, with retries.

    The spec promised this and the module did not have it (review defect 16) —
    which mattered, because the truncation guard exists to catch exactly the
    partial downloads this function must avoid producing.

    No authentication: measured 2026-08-10, the raw endpoint returns HTTP 200
    unauthenticated. Keeping it off the authenticated client means the universe
    survives a lapsed daily token (L3.28).

    Raises:
        InstrumentDumpFetchError: every attempt failed, or the body is too small
            to be a real dump.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                body: str = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
            last_error = exc
        else:
            if len(body.encode("utf-8")) >= _MINIMUM_PLAUSIBLE_DUMP_BYTES:
                return body
            last_error = InstrumentDumpFetchError(
                f"the dump is implausibly small ({len(body)} bytes) — a truncated download"
            )
        if attempt < attempts:
            time.sleep(_FETCH_BACKOFF_SECONDS * attempt)
    raise InstrumentDumpFetchError(
        f"could not fetch the instrument dump from {url} after {attempts} attempts"
    ) from last_error


def parse_instrument_dump(dump_text: str) -> list[InstrumentRecord]:
    """Parse the Kite instrument CSV into typed records.

    Every row must parse. A row that cannot is an error, never a skip: skipping
    loses an instrument silently, and a missing instrument is indistinguishable
    from one that was never listed.

    Raises:
        InstrumentDumpValidationError: empty, header-only, missing a required
            column, carrying *surplus* columns, containing an unparseable row, or
            duplicating an identity or a token within the file.
    """
    if not dump_text.strip():
        raise InstrumentDumpValidationError("the instrument dump is empty")

    reader = csv.DictReader(StringIO(dump_text), restkey="__surplus__")
    present = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise InstrumentDumpValidationError(
            f"the instrument dump is missing required column(s): {sorted(missing)}"
        )

    records: list[InstrumentRecord] = []
    for row_number, row in enumerate(reader, start=2):
        # Short rows leave None values; long rows pile the surplus under restkey.
        # The original checked only the first, so an extra column was silently
        # discarded (review defect 8).
        if row.get("__surplus__") is not None:
            raise InstrumentDumpValidationError(
                f"row {row_number} has more fields than the header declares: "
                f"surplus={row['__surplus__']!r}"
            )
        if any(row.get(column) is None for column in REQUIRED_COLUMNS):
            raise InstrumentDumpValidationError(
                f"row {row_number} is malformed: expected {len(EXPECTED_COLUMNS)} columns"
            )
        records.append(
            InstrumentRecord(
                instrument_token=_require_integer(
                    row["instrument_token"], "instrument_token", row_number
                ),
                exchange_token=_require_integer(
                    row["exchange_token"], "exchange_token", row_number
                ),
                tradingsymbol=row["tradingsymbol"].strip(),
                name=row["name"].strip(),
                last_price=_require_finite_decimal(row["last_price"], "last_price", row_number),
                expiry=_expiry_or_none(row["expiry"], row_number),
                strike=_require_finite_decimal(row["strike"], "strike", row_number),
                tick_size=_require_finite_decimal(row["tick_size"], "tick_size", row_number),
                lot_size=_require_integer(row["lot_size"], "lot_size", row_number),
                instrument_type=row["instrument_type"].strip(),
                segment=row["segment"].strip(),
                exchange=row["exchange"].strip(),
            )
        )

    if not records:
        raise InstrumentDumpValidationError("the instrument dump contains no rows")

    duplicate_identities = [
        identity for identity, count in Counter(r.identity for r in records).items() if count > 1
    ]
    if duplicate_identities:
        raise InstrumentDumpValidationError(
            f"the dump contains duplicate identities: {sorted(duplicate_identities)[:5]}"
        )
    duplicate_tokens = [
        token for token, count in Counter(r.instrument_token for r in records).items() if count > 1
    ]
    if duplicate_tokens:
        # The whole design rests on token uniqueness within a dump; it was
        # measured once and never asserted at runtime (review defect 11).
        raise InstrumentDumpValidationError(
            f"the dump reuses instrument_token within one file: {sorted(duplicate_tokens)[:5]}"
        )
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
CREATE UNIQUE INDEX IF NOT EXISTS instrument_master_one_token_per_day
    ON instrument_master (ingested_on, instrument_token);
CREATE INDEX IF NOT EXISTS instrument_master_token_history
    ON instrument_master (instrument_token, ingested_on);
CREATE TABLE IF NOT EXISTS instrument_token_reassignment (
    observed_on        TEXT NOT NULL,
    instrument_token   INTEGER NOT NULL,
    previous_seen_on   TEXT NOT NULL,
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
    is queryable *as of a date* — the property L0.05 and L0.06 build on.

    Opened in WAL mode with a busy timeout and cross-thread access permitted, so
    the dashboard can read while an ingest writes (review defect 14). Usable as a
    context manager; ``close()`` releases the connection.
    """

    database_path: Path
    busy_timeout_seconds: float = 30.0
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_seconds,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        """Release the connection. The original leaked it for the object's life."""
        self._connection.close()

    def __enter__(self) -> InstrumentMasterStore:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def ingest(
        self, records: list[InstrumentRecord], *, ingested_on: date
    ) -> list[TokenReassignment]:
        """Persist a day's universe, returning any genuine token reassignments.

        Idempotent for a given date: a retried fetch replaces that day's rows and
        that day's reassignment records, rather than doubling or stranding them.

        Raises:
            InstrumentDumpValidationError: the dump is empty, has lost more than
                the derived tolerance of the baseline universe, or has lost an
                entire previously-populated exchange.
            InstrumentStoreError: the write failed.
        """
        if not records:
            raise InstrumentDumpValidationError("refusing to ingest an empty universe")

        stamp = ingested_on.isoformat()
        # Compare against the most recent OTHER ingest, in either direction. The
        # original looked only strictly backwards, so a same-day retry and a
        # historical backfill both found nothing and skipped the guard, after
        # which the DELETE wiped the day (review defects 2 and 3).
        baseline = self._baseline_ingest_for(stamp)
        if baseline is not None:
            self._refuse_if_implausibly_small(records, baseline)
            self._refuse_if_an_exchange_vanished(records, baseline)

        reassignments = self._detect_token_reassignments(records, ingested_on)

        try:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM instrument_master WHERE ingested_on = ?", (stamp,)
                )
                # Stale reassignments from a superseded ingest of the same date
                # would otherwise survive and contradict the master (defect 12).
                self._connection.execute(
                    "DELETE FROM instrument_token_reassignment WHERE observed_on = ?", (stamp,)
                )
                self._connection.executemany(
                    "INSERT INTO instrument_master (ingested_on, exchange, segment,"
                    " tradingsymbol, instrument_token, exchange_token, name, last_price, expiry,"
                    " strike, tick_size, lot_size, instrument_type)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            stamp,
                            r.exchange,
                            r.segment,
                            r.tradingsymbol,
                            r.instrument_token,
                            r.exchange_token,
                            r.name,
                            str(r.last_price),
                            r.expiry.isoformat() if r.expiry else None,
                            str(r.strike),
                            str(r.tick_size),
                            r.lot_size,
                            r.instrument_type,
                        )
                        for r in records
                    ],
                )
                self._connection.executemany(
                    "INSERT INTO instrument_token_reassignment (observed_on, instrument_token,"
                    " previous_seen_on, previous_exchange, previous_segment, previous_symbol,"
                    " current_exchange, current_segment, current_symbol)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            stamp,
                            r.instrument_token,
                            r.previous_seen_on.isoformat(),
                            *r.previous_identity,
                            *r.current_identity,
                        )
                        for r in reassignments
                    ],
                )
        except sqlite3.Error as exc:
            # A raw sqlite error would escape `except InstrumentMasterError` and
            # crash every caller written against the documented contract
            # (review defects 10 and 14).
            raise InstrumentStoreError(f"failed to ingest {stamp}: {exc}") from exc
        return reassignments

    def instruments_as_of(
        self, as_of: date, *, exchange: str | None = None
    ) -> list[InstrumentRecord]:
        """The universe as it stood on the most recent ingest at or before ``as_of``."""
        effective = self._connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on <= ?",
            (as_of.isoformat(),),
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
                instrument_token=row[0],
                exchange_token=row[1],
                tradingsymbol=row[2],
                name=row[3],
                last_price=Decimal(row[4]),
                expiry=date.fromisoformat(row[5]) if row[5] else None,
                strike=Decimal(row[6]),
                tick_size=Decimal(row[7]),
                lot_size=row[8],
                instrument_type=row[9],
                segment=row[10],
                exchange=row[11],
            )
            for row in self._connection.execute(query, parameters)
        ]

    def recorded_reassignments(self, observed_on: date) -> list[TokenReassignment]:
        """Reassignments persisted for a date, so callers can audit rather than react."""
        return [
            TokenReassignment(
                instrument_token=row[0],
                previous_identity=(row[2], row[3], row[4]),
                current_identity=(row[5], row[6], row[7]),
                previous_seen_on=date.fromisoformat(row[1]),
                observed_on=observed_on,
            )
            for row in self._connection.execute(
                "SELECT instrument_token, previous_seen_on, previous_exchange, previous_segment,"
                " previous_symbol, current_exchange, current_segment, current_symbol"
                " FROM instrument_token_reassignment WHERE observed_on = ?",
                (observed_on.isoformat(),),
            )
        ]

    # ------------------------------------------------------------------ guards

    def _baseline_ingest_for(self, stamp: str) -> str | None:
        """The ingest a new dump is judged against.

        **The date being replaced is its own baseline when it already has rows.**
        Anything else leaves the most dangerous case unguarded: a first-ever
        ingest followed by a truncated retry of the *same* date has no other date
        to compare with, so the original returned early and the DELETE wiped it
        (review defect 2, and the incomplete first fix for it).

        Only when the date is new does it fall back to the most recent other
        ingest — in either direction, so a historical backfill is guarded too
        (defect 3).
        """
        same_day = self._connection.execute(
            "SELECT COUNT(*) FROM instrument_master WHERE ingested_on = ?", (stamp,)
        ).fetchone()[0]
        if same_day:
            return stamp
        baseline: str | None = self._connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on != ?", (stamp,)
        ).fetchone()[0]
        return baseline

    def derive_shrinkage_tolerance(self, baseline: str) -> Decimal:
        """How much of the universe may legitimately vanish in one step.

        Derived, not chosen (R.03): the largest single expiry cohort in the
        baseline dump is the biggest drop a normal expiry can cause, so the
        tolerance is that cohort's share times a safety multiple. On the real
        dump the largest cohort is 12.5% — the figure that disproved the original
        hand-picked 0.30 and its "single-digit percentages" rationale.

        Public so the derivation can be asserted rather than trusted.
        """
        total = self._connection.execute(
            "SELECT COUNT(*) FROM instrument_master WHERE ingested_on = ?", (baseline,)
        ).fetchone()[0]
        if not total:
            return _MINIMUM_SHRINKAGE_TOLERANCE
        largest_cohort = self._connection.execute(
            "SELECT COUNT(*) c FROM instrument_master WHERE ingested_on = ? AND expiry IS NOT NULL"
            " GROUP BY expiry ORDER BY c DESC LIMIT 1",
            (baseline,),
        ).fetchone()
        if largest_cohort is None:
            return _MINIMUM_SHRINKAGE_TOLERANCE
        # Multiply before dividing: (cohort / total) * multiple rounds twice and
        # lands a digit away from the shrinkage figure it is compared against,
        # which makes the boundary untestable and the comparison subtly wrong.
        derived = (Decimal(largest_cohort[0]) * _EXPIRY_COHORT_SAFETY_MULTIPLE) / Decimal(total)
        return max(derived, _MINIMUM_SHRINKAGE_TOLERANCE)

    def _refuse_if_implausibly_small(self, records: list[InstrumentRecord], baseline: str) -> None:
        previous_count = self._connection.execute(
            "SELECT COUNT(*) FROM instrument_master WHERE ingested_on = ?", (baseline,)
        ).fetchone()[0]
        if not previous_count:
            return
        shrinkage = Decimal(previous_count - len(records)) / Decimal(previous_count)
        tolerance = self.derive_shrinkage_tolerance(baseline)
        # >= not >: an exactly-at-tolerance loss was accepted, and the boundary
        # was untested (review defect 6).
        if shrinkage >= tolerance:
            raise InstrumentDumpValidationError(
                f"refusing a truncated dump: {len(records)} rows against {previous_count} on "
                f"{baseline} ({shrinkage:.1%} smaller, tolerance {tolerance:.1%} derived from "
                "that dump's largest expiry cohort)"
            )

    def _refuse_if_an_exchange_vanished(
        self, records: list[InstrumentRecord], baseline: str
    ) -> None:
        """An entire exchange disappearing is a truncated download, not delisting.

        Required by the spec and absent from the code: dropping all of NSE
        (8.8% of the real dump) or NSE+BSE (20.1%) passed the size check, and
        every downstream scan then read "this exchange has no instruments" as a
        normal answer (review defect 4).
        """
        previous_exchanges = {
            row[0]
            for row in self._connection.execute(
                "SELECT DISTINCT exchange FROM instrument_master WHERE ingested_on = ?",
                (baseline,),
            )
        }
        vanished = previous_exchanges - {r.exchange for r in records}
        if vanished:
            raise InstrumentDumpValidationError(
                f"refusing a dump that lost entire exchange(s) present on {baseline}: "
                f"{sorted(vanished)}. An exchange does not delist overnight."
            )

    def _detect_token_reassignments(
        self, records: list[InstrumentRecord], ingested_on: date
    ) -> list[TokenReassignment]:
        """Find tokens now pointing at a genuinely different contract (L0.02).

        Searches the **full history**, not just the previous ingest. Kite drops an
        expired contract from the dump and reuses its token later, so at the
        moment of reuse the token is absent from yesterday — which is precisely
        why the original never fired (review defect 1).

        A symbol rename is *not* a reassignment: same contract terms, new name.
        Conflating the two made the log untrustworthy for the thing it exists to
        detect (review defect 13).
        """
        stamp = ingested_on.isoformat()
        reassignments: list[TokenReassignment] = []
        for record in records:
            previous = self._connection.execute(
                "SELECT exchange, segment, tradingsymbol, ingested_on, expiry, strike,"
                " instrument_type FROM instrument_master"
                " WHERE instrument_token = ? AND ingested_on != ?"
                " ORDER BY ingested_on DESC LIMIT 1",
                (record.instrument_token, stamp),
            ).fetchone()
            if previous is None:
                continue
            previous_identity = (previous[0], previous[1], previous[2])
            if previous_identity == record.identity:
                continue
            previous_terms = (previous[4] or "", previous[5], previous[6])
            if previous_terms == record.contract_terms:
                continue  # a rename of the same contract, not token reuse
            reassignments.append(
                TokenReassignment(
                    instrument_token=record.instrument_token,
                    previous_identity=previous_identity,
                    current_identity=record.identity,
                    previous_seen_on=date.fromisoformat(previous[3]),
                    observed_on=ingested_on,
                )
            )
        return reassignments
