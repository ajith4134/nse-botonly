"""Project ingested F&O bhavcopy observations into the queryable contract table (`L0.23`).

**Why this exists.** `L0.23` says "NSE bhavcopy ingest — daily cash delivery + F&O contract-level
settlement data". The ingest half was built: `BitemporalIngestStore` holds every `nse_bhavcopy_fo`
observation, versioned and hash-keyed. The *queryable* half was not. `fo_bhavcopy_contracts` — the
table every derivative segment bot assembles its universe from — was populated once by something
that no longer exists in this repository, and then stopped: measured on 2026-08-19 it held sessions
through **2026-08-03** while the ingest store held **2026-08-18**. Five of six bots had been
deciding over contracts that had already settled, and nothing was red.

Staleness with no error is the failure mode this module exists to end. It is the derivative-side
counterpart of what `price_bars` is for cash: the layer between "what the source said" and "what a
bot can ask a question of".

**What it carries that the legacy table did not**, because the rest of the continuous-trading slice
(`docs/research/267`) needs both:

- `total_traded_value` — NSE's own `TtlTrfVal`. The cash capture universe already ranks liquidity on
  exactly this field; the derivative side had no equivalent, so an option chain could only be cut by
  an asserted strike count, which is the magic number `R.03` forbids.
- `availability_time` — the instant this project first OBSERVED the row. Without it a walk-forward
  replay cannot ask "what did we know at time T" and every replayed session leaks the future.

Legacy rows keep `NULL` in both. A guessed availability time on a row that was never timestamped is
worse than no timestamp: it reads as point-in-time evidence and is not.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import (
    BitemporalIngestStore,
    StoredObservation,
)

F_AND_O_INGEST_SOURCE_NAME: Final = "nse_bhavcopy_fo"
"""The one source name this projection accepts. A cash row projected into the derivatives table
would be invisible and wrong, so the source is checked rather than assumed."""

CONTRACT_TABLE: Final = "fo_bhavcopy_contracts"
CURSOR_TABLE: Final = "derivative_contract_projection_cursor"

DEFAULT_MARKET_DATABASE: Final = Path.home() / ".nse_algo_trader" / "market_data.sqlite3"
DEFAULT_INGEST_DATABASE: Final = Path.home() / ".nse_algo_trader" / "nse_ingest.sqlite3"

KNOWN_CONTRACT_TYPES: Final = frozenset({"IDF", "IDO", "STF", "STO"})
"""Index future · index option · stock future · stock option. NSE's own `FinInstrmTp` codes.
MCX is absent from this source entirely, which is `B30` and not something this module can fix."""

OPTION_CONTRACT_TYPES: Final = frozenset({"IDO", "STO"})

_ADDED_COLUMNS: Final = (
    ("total_traded_value", "REAL"),
    ("trades_executed", "INTEGER"),
    ("lot_size", "INTEGER"),
    ("instrument_name", "TEXT"),
    ("availability_time", "TEXT"),
    ("content_hash", "TEXT"),
)


class DerivativeContractProjectionError(Exception):
    """A row could not be projected. Raised per row and counted by the projector.

    Deliberately not swallowed at the parse site: the projector decides what to do with a refusal
    and REPORTS it, so a file whose shape changed shows up as a refusal count rather than as a
    universe that quietly got smaller.
    """


@dataclass(frozen=True)
class ProjectedContractRecord:
    """One F&O contract's settlement record for one session, typed.

    `strike_price` and `option_right_code` are `None` for futures rather than empty strings, because
    a future has no strike — an empty string would sort and compare as a value.
    """

    trade_date: date
    contract_type: str
    nse_instrument_id: int
    underlying_symbol: str
    expiry_date: date
    strike_price: float | None
    option_right_code: str | None
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    settlement_price: float
    underlying_price: float
    open_interest: int
    change_in_open_interest: int
    total_traded_volume: int
    total_traded_value: float
    trades_executed: int
    lot_size: int
    instrument_name: str
    availability_time: datetime
    content_hash: str

    @property
    def is_an_option(self) -> bool:
        return self.contract_type in OPTION_CONTRACT_TYPES

    def as_row(self) -> tuple[Any, ...]:
        """The tuple the upsert binds, in the column order `_INSERT_STATEMENT` declares."""
        return (
            self.trade_date.isoformat(),
            self.contract_type,
            self.nse_instrument_id,
            self.underlying_symbol,
            self.expiry_date.isoformat(),
            self.strike_price,
            self.option_right_code,
            self.open_price,
            self.high_price,
            self.low_price,
            self.close_price,
            self.settlement_price,
            self.underlying_price,
            self.open_interest,
            self.change_in_open_interest,
            self.total_traded_volume,
            self.total_traded_value,
            self.trades_executed,
            self.lot_size,
            self.instrument_name,
            self.availability_time.isoformat(),
            self.content_hash,
        )


@dataclass(frozen=True)
class ProjectionOutcome:
    """What one projection run did, in the terms a `R.11` sign-off needs.

    `rows_refused_by_reason` is the important field. A projection that reports only successes cannot
    distinguish "NSE published fewer contracts" from "our parser stopped understanding the file".
    """

    sessions_read: int
    sessions_projected: int
    rows_written: int
    rows_unchanged: int
    rows_refused_by_reason: Mapping[str, int] = field(default_factory=dict)
    earliest_session: date | None = None
    latest_session: date | None = None

    @property
    def rows_refused(self) -> int:
        return sum(self.rows_refused_by_reason.values())

    def describe(self) -> str:
        if self.sessions_read == 0:
            return "nothing new to project"
        span = (
            f"{self.earliest_session}..{self.latest_session}"
            if self.earliest_session != self.latest_session
            else f"{self.latest_session}"
        )
        parts = [
            f"{self.sessions_projected} of {self.sessions_read} session(s) changed ({span})",
            f"{self.rows_written:,} row(s) written",
            f"{self.rows_unchanged:,} unchanged",
        ]
        if self.rows_refused:
            worst = sorted(self.rows_refused_by_reason.items(), key=lambda item: -item[1])[:3]
            detail = ", ".join(f"{reason} x{count}" for reason, count in worst)
            parts.append(f"{self.rows_refused:,} REFUSED ({detail})")
        return " · ".join(parts)


# ---------------------------------------------------------------------------------------------
# Parsing — NSE's strings into typed values, refusing rather than coercing
# ---------------------------------------------------------------------------------------------


def _required_text(values: Mapping[str, Any], key: str) -> str:
    raw = str(values.get(key, "")).strip()
    if not raw:
        raise DerivativeContractProjectionError(f"{key} is empty")
    return raw


def _required_float(values: Mapping[str, Any], key: str) -> float:
    raw = _required_text(values, key)
    try:
        return float(raw)
    except ValueError as failure:
        raise DerivativeContractProjectionError(f"{key} is not a number: {raw!r}") from failure


def _required_int(values: Mapping[str, Any], key: str) -> int:
    raw = _required_text(values, key)
    try:
        return int(float(raw))
    except ValueError as failure:
        raise DerivativeContractProjectionError(f"{key} is not a number: {raw!r}") from failure


def _optional_float(values: Mapping[str, Any], key: str) -> float | None:
    raw = str(values.get(key, "")).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as failure:
        raise DerivativeContractProjectionError(f"{key} is not a number: {raw!r}") from failure


def _required_date(values: Mapping[str, Any], key: str) -> date:
    raw = _required_text(values, key)
    try:
        return date.fromisoformat(raw)
    except ValueError as failure:
        raise DerivativeContractProjectionError(f"{key} is not a date: {raw!r}") from failure


def contract_record_from_observation(observation: StoredObservation) -> ProjectedContractRecord:
    """Turn one stored observation into a typed contract record, or refuse it with a reason.

    Every refusal names the FIELD that failed, because the reason is what the projector counts and
    a count of "malformed" tells an operator nothing about which part of NSE's file moved.
    """
    if observation.source_name != F_AND_O_INGEST_SOURCE_NAME:
        raise DerivativeContractProjectionError(
            f"source {observation.source_name!r} is not {F_AND_O_INGEST_SOURCE_NAME!r} — "
            f"projecting it into {CONTRACT_TABLE} would file a non-derivative row where every "
            f"reader assumes derivatives"
        )

    values = observation.values
    if "TradDt" not in values and "TIMESTAMP" in values:
        raise DerivativeContractProjectionError(
            "pre-UDiFF bhavcopy layout (SYMBOL/INSTRUMENT/TIMESTAMP) — refused rather than "
            "half-populated: that layout publishes no underlying price and no contract id, and "
            "both the basis-carry and variance-premium engines read the underlying price"
        )
    trade_date = _required_date(values, "TradDt")
    if trade_date != observation.effective_date:
        raise DerivativeContractProjectionError(
            f"TradDt {trade_date} disagrees with the effective date {observation.effective_date}; "
            f"the two are the same fact and a disagreement means the file's shape changed"
        )

    contract_type = _required_text(values, "FinInstrmTp").upper()
    if contract_type not in KNOWN_CONTRACT_TYPES:
        raise DerivativeContractProjectionError(
            f"FinInstrmTp {contract_type!r} is not one of {sorted(KNOWN_CONTRACT_TYPES)}"
        )

    strike_price = _optional_float(values, "StrkPric")
    right_raw = str(values.get("OptnTp", "")).strip().upper()
    option_right_code = right_raw or None

    is_an_option = contract_type in OPTION_CONTRACT_TYPES
    if is_an_option and strike_price is None:
        raise DerivativeContractProjectionError(f"StrkPric is empty on an option ({contract_type})")
    if is_an_option and option_right_code not in ("CE", "PE"):
        raise DerivativeContractProjectionError(
            f"OptnTp {right_raw!r} is not CE or PE on an option ({contract_type})"
        )
    if not is_an_option and option_right_code is not None:
        raise DerivativeContractProjectionError(
            f"OptnTp {right_raw!r} is set on a future ({contract_type})"
        )

    expiry_date = _required_date(values, "XpryDt")
    if expiry_date < trade_date:
        raise DerivativeContractProjectionError(
            f"XpryDt {expiry_date} precedes TradDt {trade_date} — a contract cannot trade after it "
            f"has settled"
        )

    return ProjectedContractRecord(
        trade_date=trade_date,
        contract_type=contract_type,
        nse_instrument_id=_required_int(values, "FinInstrmId"),
        underlying_symbol=_required_text(values, "TckrSymb"),
        expiry_date=expiry_date,
        strike_price=strike_price,
        option_right_code=option_right_code,
        open_price=_required_float(values, "OpnPric"),
        high_price=_required_float(values, "HghPric"),
        low_price=_required_float(values, "LwPric"),
        close_price=_required_float(values, "ClsPric"),
        settlement_price=_required_float(values, "SttlmPric"),
        underlying_price=_required_float(values, "UndrlygPric"),
        open_interest=_required_int(values, "OpnIntrst"),
        change_in_open_interest=_required_int(values, "ChngInOpnIntrst"),
        total_traded_volume=_required_int(values, "TtlTradgVol"),
        total_traded_value=_required_float(values, "TtlTrfVal"),
        trades_executed=_required_int(values, "TtlNbOfTxsExctd"),
        lot_size=_required_int(values, "NewBrdLotQty"),
        instrument_name=_required_text(values, "FinInstrmNm"),
        availability_time=observation.observed_at,
        content_hash=observation.content_hash,
    )


# ---------------------------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------------------------

_CREATE_STATEMENT: Final = f"""
CREATE TABLE IF NOT EXISTS {CONTRACT_TABLE} (
    trade_date TEXT NOT NULL, contract_type TEXT NOT NULL,
    nse_instrument_id INTEGER NOT NULL, underlying_symbol TEXT NOT NULL,
    expiry_date TEXT NOT NULL, strike_price REAL, option_right_code TEXT,
    open_price REAL NOT NULL, high_price REAL NOT NULL,
    low_price REAL NOT NULL, close_price REAL NOT NULL,
    settlement_price REAL NOT NULL, underlying_price REAL NOT NULL,
    open_interest INTEGER NOT NULL, change_in_open_interest INTEGER NOT NULL,
    total_traded_volume INTEGER NOT NULL,
    PRIMARY KEY (trade_date, nse_instrument_id))
"""

_INSERT_STATEMENT: Final = """
INSERT INTO fo_bhavcopy_contracts (
    trade_date, contract_type, nse_instrument_id, underlying_symbol, expiry_date,
    strike_price, option_right_code, open_price, high_price, low_price, close_price,
    settlement_price, underlying_price, open_interest, change_in_open_interest,
    total_traded_volume, total_traded_value, trades_executed, lot_size, instrument_name,
    availability_time, content_hash
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (trade_date, nse_instrument_id) DO UPDATE SET
    contract_type = excluded.contract_type,
    underlying_symbol = excluded.underlying_symbol,
    expiry_date = excluded.expiry_date,
    strike_price = excluded.strike_price,
    option_right_code = excluded.option_right_code,
    open_price = excluded.open_price,
    high_price = excluded.high_price,
    low_price = excluded.low_price,
    close_price = excluded.close_price,
    settlement_price = excluded.settlement_price,
    underlying_price = excluded.underlying_price,
    open_interest = excluded.open_interest,
    change_in_open_interest = excluded.change_in_open_interest,
    total_traded_volume = excluded.total_traded_volume,
    total_traded_value = excluded.total_traded_value,
    trades_executed = excluded.trades_executed,
    lot_size = excluded.lot_size,
    instrument_name = excluded.instrument_name,
    availability_time = excluded.availability_time,
    content_hash = excluded.content_hash
"""


class DerivativeContractRecordProjection:
    """Materialise `nse_bhavcopy_fo` observations into the table the bots query.

    Incremental by a persisted cursor and idempotent by content hash: a re-run over a session whose
    observations have not changed writes nothing, which is what makes it safe to call from the
    continuous loop at every universe refresh as well as from the daily run.
    """

    def __init__(
        self,
        market_database: Path = DEFAULT_MARKET_DATABASE,
        ingest_database: Path = DEFAULT_INGEST_DATABASE,
    ) -> None:
        self._market_database = Path(market_database)
        self._ingest_database = Path(ingest_database)
        self._market_database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._market_database)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._schema_is_ready = False

    def __enter__(self) -> DerivativeContractRecordProjection:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    # -- schema ------------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the table if absent and add the columns this slice needs if they are missing.

        `ALTER TABLE ... ADD COLUMN` rather than a rebuild: the legacy table holds 36 effective
        dates that the ingest store does not, and a rebuild would silently lose their union.
        """
        self._connection.execute(_CREATE_STATEMENT)
        self._connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_fo_contracts_by_underlying "
            f"ON {CONTRACT_TABLE} (underlying_symbol, trade_date)"
        )
        existing = {
            row[1] for row in self._connection.execute(f"PRAGMA table_info({CONTRACT_TABLE})")
        }
        for column, column_type in _ADDED_COLUMNS:
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {CONTRACT_TABLE} ADD COLUMN {column} {column_type}"
                )
        self._connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_fo_contracts_by_expiry "
            f"ON {CONTRACT_TABLE} (contract_type, expiry_date, trade_date)"
        )
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CURSOR_TABLE} (
                source_name TEXT PRIMARY KEY,
                last_effective_date TEXT NOT NULL,
                last_projected_at TEXT NOT NULL,
                rows_written INTEGER NOT NULL
            )
            """
        )
        self._connection.commit()
        self._schema_is_ready = True

    # -- cursor ------------------------------------------------------------------------------

    def last_projected_session(self) -> date | None:
        self._ensure_schema_once()
        row = self._connection.execute(
            f"SELECT last_effective_date FROM {CURSOR_TABLE} WHERE source_name = ?",  # noqa: S608
            (F_AND_O_INGEST_SOURCE_NAME,),
        ).fetchone()
        return date.fromisoformat(row[0]) if row else None

    def latest_contract_session(self) -> date | None:
        """The most recent session the CONTRACT TABLE holds — the number that was stale."""
        self._ensure_schema_once()
        row = self._connection.execute(
            f"SELECT MAX(trade_date) FROM {CONTRACT_TABLE}"  # noqa: S608
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def _remember_cursor(self, session: date, rows_written: int, at: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO derivative_contract_projection_cursor (
                source_name, last_effective_date, last_projected_at, rows_written)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (source_name) DO UPDATE SET
                last_effective_date = MAX(
                    excluded.last_effective_date,
                    derivative_contract_projection_cursor.last_effective_date),
                last_projected_at = excluded.last_projected_at,
                rows_written = excluded.rows_written
            """,
            (F_AND_O_INGEST_SOURCE_NAME, session.isoformat(), at.isoformat(), rows_written),
        )

    # -- projection --------------------------------------------------------------------------

    def project(
        self, *, since: date | None = None, only: Sequence[date] | None = None
    ) -> ProjectionOutcome:
        """Project the sessions that need it and report what happened.

        `only` names sessions explicitly and ignores the cursor — the escape hatch for reprojecting
        a session whose parser changed. `since` lowers the cursor for one run. Neither invents work:
        a session whose observations are byte-identical to what is already stored writes nothing.
        """
        self._ensure_schema_once()
        cursor = self.last_projected_session()
        refusals: Counter[str] = Counter()
        rows_written = 0
        rows_unchanged = 0
        sessions_projected = 0
        touched: list[date] = []

        with BitemporalIngestStore(self._ingest_database) as ingest:
            available = sorted(ingest.effective_dates_present(F_AND_O_INGEST_SOURCE_NAME))
            wanted = self._sessions_to_project(available, cursor=cursor, since=since, only=only)
            for session in wanted:
                observations = ingest.rows_for(F_AND_O_INGEST_SOURCE_NAME, session)
                written, unchanged = self._project_one_session(observations, refusals)
                rows_written += written
                rows_unchanged += unchanged
                touched.append(session)
                if written:
                    sessions_projected += 1
                self._remember_cursor(session, written, _now())

        self._connection.commit()
        return ProjectionOutcome(
            sessions_read=len(touched),
            sessions_projected=sessions_projected,
            rows_written=rows_written,
            rows_unchanged=rows_unchanged,
            rows_refused_by_reason=dict(refusals),
            earliest_session=min(touched) if touched else None,
            latest_session=max(touched) if touched else None,
        )

    def _sessions_to_project(
        self,
        available: Sequence[date],
        *,
        cursor: date | None,
        since: date | None,
        only: Sequence[date] | None,
    ) -> list[date]:
        if only is not None:
            wanted = set(only)
            missing = wanted - set(available)
            if missing:
                raise DerivativeContractProjectionError(
                    f"asked to project {sorted(missing)}, which the ingest store does not hold"
                )
            return sorted(wanted)
        if since is not None:
            # An explicit floor is INCLUSIVE: "reproject from here" must reproject the day named.
            return [session for session in available if session >= since]
        if cursor is None:
            return list(available)
        # The cursor is EXCLUSIVE: it names the last session already done.
        return [session for session in available if session > cursor]

    def _project_one_session(
        self, observations: Iterable[StoredObservation], refusals: Counter[str]
    ) -> tuple[int, int]:
        """Upsert one session's rows. Returns (written, unchanged).

        "Unchanged" is decided by the stored `content_hash`, not by comparing every column: the hash
        is what the ingest store already versions on, so the two layers cannot disagree about
        whether a fact moved.
        """
        known_hashes = self._known_content_hashes(observations)
        pending: list[tuple[Any, ...]] = []
        unchanged = 0
        for observation in observations:
            try:
                record = contract_record_from_observation(observation)
            except DerivativeContractProjectionError as refusal:
                refusals[str(refusal)] += 1
                continue
            key = (record.trade_date.isoformat(), record.nse_instrument_id)
            if known_hashes.get(key) == record.content_hash:
                unchanged += 1
                continue
            pending.append(record.as_row())
        if pending:
            self._connection.executemany(_INSERT_STATEMENT, pending)
        return len(pending), unchanged

    def _known_content_hashes(
        self, observations: Iterable[StoredObservation]
    ) -> dict[tuple[str, int], str | None]:
        sessions = {observation.effective_date.isoformat() for observation in observations}
        if not sessions:
            return {}
        placeholders = ",".join("?" for _ in sessions)
        rows = self._connection.execute(
            f"SELECT trade_date, nse_instrument_id, content_hash FROM {CONTRACT_TABLE} "  # noqa: S608
            f"WHERE trade_date IN ({placeholders})",
            tuple(sorted(sessions)),
        ).fetchall()
        return {(row[0], int(row[1])): row[2] for row in rows}

    def _ensure_schema_once(self) -> None:
        if not self._schema_is_ready:
            self.ensure_schema()


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
