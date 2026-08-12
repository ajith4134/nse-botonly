"""Bitemporal storage for every ingested NSE source: what was true, and when we knew it.

Two clocks, never one. **`effective_date`** is the date a row is *about*; **`observed_at`**
is when this system learned it. They differ constantly and the difference is load-bearing:

- A bhavcopy for 2026-08-10 fetched on 2026-08-11 is a fact about Monday learned on
  Tuesday. A backtest asking "what could I have known at Monday's close" must not see it.
- NSE **revises** files. A corrected bhavcopy published days later is a *second
  observation* of the same effective date, not a correction that overwrites the first.
  Overwriting destroys the only evidence that the original was ever different, and with
  it any chance of reproducing a decision made from the original.
- For a rolling source (the ban list, bulk deals) the URL and often the content are
  identical day to day, so `observed_at` is the *only* thing distinguishing one day's
  snapshot from the next.

**Nothing is ever updated or deleted.** Re-ingesting identical content is a no-op keyed
on `(source, natural_key, effective_date, content_hash)`. Re-ingesting *different*
content for the same key appends a new observation and leaves the old one visible. The
store is therefore append-only in the same way the depth tape and the bar store are, so
the whole `L0` layer answers point-in-time questions with one set of semantics.

SQLite rather than Delta or Parquet, deliberately: the volumes are modest (the largest
source is roughly 18M rows over 25 years), a single transaction gives the all-or-nothing
atomicity the conformance suite demands, and Delta's time travel is *version*-scoped —
"what did the table look like at commit N" — which is not the per-row question being
asked here and would have to be reimplemented on top anyway.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from nse_algo_trader.nse_ingest.ingest_source_adapter import IngestRow

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ingested_row (
        source_name       TEXT    NOT NULL,
        natural_key       TEXT    NOT NULL,
        effective_date    TEXT    NOT NULL,
        observed_at       TEXT    NOT NULL,
        content_hash      TEXT    NOT NULL,
        row_values_json   TEXT    NOT NULL,
        fetch_id          INTEGER NOT NULL REFERENCES source_fetch(fetch_id),
        PRIMARY KEY (source_name, natural_key, effective_date, content_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ingested_row_as_of
        ON ingested_row (source_name, effective_date, observed_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS source_fetch (
        fetch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name     TEXT NOT NULL,
        url             TEXT NOT NULL,
        fetched_at      TEXT NOT NULL,
        http_status     INTEGER,
        fetch_status    TEXT NOT NULL,
        payload_sha256  TEXT,
        payload_bytes   INTEGER NOT NULL,
        attempts        INTEGER NOT NULL,
        evidence        TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS source_fetch_by_source
        ON source_fetch (source_name, fetched_at)
    """,
)


class BitemporalIngestStoreError(Exception):
    """Raised when the store is asked for something it cannot answer correctly."""


@dataclass(frozen=True)
class StoredObservation:
    """One row as stored, with both clocks and its provenance link."""

    source_name: str
    natural_key: tuple[str, ...]
    effective_date: date
    observed_at: datetime
    content_hash: str
    values: Mapping[str, Any]
    fetch_id: int


@dataclass(frozen=True)
class IngestResult:
    """What one ingest actually changed — the caller never has to infer it."""

    source_name: str
    fetch_id: int
    rows_presented: int
    rows_inserted: int
    rows_already_present: int
    revisions_recorded: int

    @property
    def was_a_no_op(self) -> bool:
        return self.rows_inserted == 0 and self.rows_presented > 0


def content_hash_of(values: Mapping[str, Any]) -> str:
    """A stable hash of a row's content, independent of key ordering.

    `sort_keys` matters: without it two identical rows whose dicts were built in
    different orders hash differently, and every re-ingest would look like a revision.
    """
    encoded = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class BitemporalIngestStore:
    """Append-only bitemporal storage shared by every source adapter."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        # WAL so a reader during an ingest sees the last committed state rather than
        # blocking or observing a partial transaction.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA_STATEMENTS:
            self._connection.execute(statement)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BitemporalIngestStore:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def record_fetch(
        self,
        source_name: str,
        url: str,
        fetched_at: datetime,
        fetch_status: str,
        evidence: str,
        attempts: int,
        http_status: int | None = None,
        payload_sha256: str | None = None,
        payload_bytes: int = 0,
    ) -> int:
        """Record one fetch attempt — successful or not — and return its id.

        Failed fetches are recorded too. A source that has been bot-blocked for a week
        is a fact worth having, and it is invisible if only successes are written down.
        """
        cursor = self._connection.execute(
            """
            INSERT INTO source_fetch (
                source_name, url, fetched_at, http_status, fetch_status,
                payload_sha256, payload_bytes, attempts, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_name,
                url,
                fetched_at.astimezone(UTC).isoformat(),
                http_status,
                fetch_status,
                payload_sha256,
                payload_bytes,
                attempts,
                evidence,
            ),
        )
        fetch_id = cursor.lastrowid
        if fetch_id is None:
            raise BitemporalIngestStoreError("sqlite returned no id for the fetch record")
        return fetch_id

    def ingest_rows(
        self,
        source_name: str,
        rows: Iterable[IngestRow],
        observed_at: datetime,
        fetch_id: int,
        coverage_floor: date | None = None,
    ) -> IngestResult:
        """Store rows atomically. Identical content is a no-op; changed content revises.

        The whole batch commits or none of it does. A partial ingest would leave a
        source's effective date half-populated with no marker saying so, and every later
        reader would treat the fragment as the whole truth.
        """
        materialized = list(rows)
        today = datetime.now(UTC).date()
        prepared: list[tuple[str, str, str, str, str, str, int]] = []

        for row in materialized:
            if row.effective_date > today:
                raise BitemporalIngestStoreError(
                    f"{source_name}: effective date {row.effective_date} is in the future"
                )
            if coverage_floor is not None and row.effective_date < coverage_floor:
                raise BitemporalIngestStoreError(
                    f"{source_name}: effective date {row.effective_date} precedes the "
                    f"verified coverage floor {coverage_floor}"
                )
            prepared.append(
                (
                    source_name,
                    json.dumps(list(row.natural_key)),
                    row.effective_date.isoformat(),
                    observed_at.astimezone(UTC).isoformat(),
                    content_hash_of(row.values),
                    json.dumps(row.values, sort_keys=True, default=str),
                    fetch_id,
                )
            )

        keys_present_before = self._existing_content_hashes(
            source_name, {(entry[1], entry[2]) for entry in prepared}
        )

        inserted = 0
        with closing(self._connection.cursor()) as cursor:
            cursor.execute("BEGIN")
            try:
                for entry in prepared:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO ingested_row (
                            source_name, natural_key, effective_date, observed_at,
                            content_hash, row_values_json, fetch_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        entry,
                    )
                    inserted += cursor.rowcount
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

        revisions = sum(
            1
            for entry in prepared
            if (entry[1], entry[2]) in keys_present_before
            and entry[4] not in keys_present_before[(entry[1], entry[2])]
        )
        return IngestResult(
            source_name=source_name,
            fetch_id=fetch_id,
            rows_presented=len(prepared),
            rows_inserted=inserted,
            rows_already_present=len(prepared) - inserted,
            revisions_recorded=revisions,
        )

    def _existing_content_hashes(
        self, source_name: str, keys: set[tuple[str, str]]
    ) -> dict[tuple[str, str], set[str]]:
        if not keys:
            return {}
        found: dict[tuple[str, str], set[str]] = {}
        for natural_key, effective_date in keys:
            rows = self._connection.execute(
                """
                SELECT content_hash FROM ingested_row
                WHERE source_name = ? AND natural_key = ? AND effective_date = ?
                """,
                (source_name, natural_key, effective_date),
            ).fetchall()
            if rows:
                found[(natural_key, effective_date)] = {row[0] for row in rows}
        return found

    def rows_for(
        self,
        source_name: str,
        effective_date: date,
        known_by: datetime | None = None,
    ) -> list[StoredObservation]:
        """The best-known rows for a date, optionally as they were known at an instant.

        `known_by` is what makes a leakage-free backtest possible: it returns the latest
        observation of each key **that had already been observed** by that moment, so a
        revision published later cannot reach back into a decision it postdates.
        """
        parameters: list[Any] = [source_name, effective_date.isoformat()]
        as_of_clause = ""
        if known_by is not None:
            as_of_clause = "AND observed_at <= ?"
            parameters.append(known_by.astimezone(UTC).isoformat())

        query = f"""
            SELECT source_name, natural_key, effective_date, observed_at,
                   content_hash, row_values_json, fetch_id
            FROM ingested_row
            WHERE source_name = ? AND effective_date = ? {as_of_clause}
            ORDER BY natural_key, observed_at DESC, content_hash
        """  # noqa: S608 — as_of_clause is a fixed literal, the value is parameterized

        latest_by_key: dict[str, StoredObservation] = {}
        for record in self._connection.execute(query, parameters):
            natural_key = record["natural_key"]
            if natural_key in latest_by_key:
                continue  # ordered newest-first, so the first seen is the latest
            latest_by_key[natural_key] = self._observation_from(record)
        return list(latest_by_key.values())

    def all_observations_for(
        self, source_name: str, effective_date: date, natural_key: Sequence[str]
    ) -> list[StoredObservation]:
        """Every observation of one key, oldest first — the revision history itself."""
        records = self._connection.execute(
            """
            SELECT source_name, natural_key, effective_date, observed_at,
                   content_hash, row_values_json, fetch_id
            FROM ingested_row
            WHERE source_name = ? AND effective_date = ? AND natural_key = ?
            ORDER BY observed_at, content_hash
            """,
            (source_name, effective_date.isoformat(), json.dumps(list(natural_key))),
        ).fetchall()
        return [self._observation_from(record) for record in records]

    @staticmethod
    def _observation_from(record: sqlite3.Row) -> StoredObservation:
        return StoredObservation(
            source_name=record["source_name"],
            natural_key=tuple(json.loads(record["natural_key"])),
            effective_date=date.fromisoformat(record["effective_date"]),
            observed_at=datetime.fromisoformat(record["observed_at"]),
            content_hash=record["content_hash"],
            values=json.loads(record["row_values_json"]),
            fetch_id=record["fetch_id"],
        )

    def effective_dates_present(self, source_name: str) -> list[date]:
        records = self._connection.execute(
            """
            SELECT DISTINCT effective_date FROM ingested_row
            WHERE source_name = ? ORDER BY effective_date
            """,
            (source_name,),
        ).fetchall()
        return [date.fromisoformat(record[0]) for record in records]

    def publication_observations(self) -> list[tuple[str, date, datetime]]:
        """Every row's `(source, effective_date, observed_at)`, for lag derivation.

        A projection rather than a row fetch: `L0.11` needs three columns from every row
        in the store, and materialising 720,000 full observations to read them would cost
        minutes to answer what SQL answers directly.
        """
        return [
            (source_name, date.fromisoformat(effective), datetime.fromisoformat(observed))
            for source_name, effective, observed in self._connection.execute(
                "SELECT source_name, effective_date, observed_at FROM ingested_row"
            )
        ]

    def row_count(self, source_name: str) -> int:
        record = self._connection.execute(
            "SELECT COUNT(*) FROM ingested_row WHERE source_name = ?", (source_name,)
        ).fetchone()
        return int(record[0])

    def fetch_history(self, source_name: str, successful_only: bool = False) -> list[sqlite3.Row]:
        """Every fetch attempt for a source, including the failures.

        A source blocked for a week is a fact the coverage check needs; recording only
        successes makes an outage look identical to never having asked.
        """
        clause = "AND fetch_status = 'retrieved'" if successful_only else ""
        return self._connection.execute(
            f"""
            SELECT * FROM source_fetch WHERE source_name = ? {clause}
            ORDER BY fetched_at
            """,  # noqa: S608 — clause is a fixed literal, the value is parameterized
            (source_name,),
        ).fetchall()
