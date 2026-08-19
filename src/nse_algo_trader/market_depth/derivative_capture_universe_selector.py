"""Choose the F&O contracts the depth capture subscribes (`A.142`, cadence set by `A.146`).

**Why this exists.** `A.142` measured it exactly: today's depth tape holds **2,135,786 ticks over
1,845 instrument tokens, and every one of them is `NSE` cash**. Zero `NFO-OPT`, `NFO-FUT` or `MCX`
tokens have ever been subscribed. *"So the option and future bots are not short of data because the
data does not exist — they are short of it because nothing ever asked for it."* This module is what
asks. Re-measured on 2026-08-19 before building: still 2,295 tokens, still all cash.

**The band around the money is MEASURED, never asserted.** The obvious selection — "the nearest
expiry, ATM plus or minus ten strikes" — is the magic number `R.03` forbids, and it is also wrong on
its own terms: a NIFTY 50-point ladder and a stock option's 20-rupee ladder do not contain the same
amount of the distribution, and an expiry-week pin moves the traded band away from the spot. So the
strikes are ranked by **the contract's own traded value** from the most recent projected session.
Liquidity concentrates around the money as a market fact, so the band emerges from the measurement —
and where it does not, the measurement is right and the assertion would have been wrong.

The cash side has ranked on exactly this field (`TtlTrfVal`) since the capture was built; the
derivative side simply had no such column until `derivative_contract_record_projection` added it.

**What this module does NOT do.** It does not decide how many contracts fit.
`DepthCaptureAdmissionController` already solves a token budget against measured packets per second
and a disk fraction, and it does so over one ranked population — so the cash and derivative
candidates are merged before admission and the widening cannot silently evict cash coverage.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

DEFAULT_MARKET_DATA_PATH: Final = Path.home() / ".nse_algo_trader" / "market_data.sqlite3"

CONTRACT_TABLE: Final = "fo_bhavcopy_contracts"

FUTURE_CONTRACT_TYPES: Final = frozenset({"IDF", "STF"})
OPTION_CONTRACT_TYPES: Final = frozenset({"IDO", "STO"})

NFO_EXCHANGE: Final = "NFO"
"""What `LiveDepthFeedSeam` keys a token's exchange by. An unset exchange subscribes nothing, and
the failure is silent — the socket simply never delivers a packet for that token."""

NFO_MASTER_SEGMENTS: Final = ("NFO-OPT", "NFO-FUT")


class DerivativeCaptureSelectionError(Exception):
    """The selection could not be made at all — a missing table, not a thin result.

    An empty universe is ANSWERABLE and is returned as an empty universe with a note. A market
    database with no contract table is a different thing: it means the projection never ran, and
    returning "no contracts" would read as "the market has no contracts".
    """


@dataclass(frozen=True)
class CaptureCandidateContract:
    """One F&O contract that could be subscribed, with the liquidity it was ranked on."""

    instrument_token: int
    trading_symbol: str
    exchange: str
    contract_type: str
    underlying_symbol: str
    expiry: date
    strike_price: float | None
    option_right_code: str | None
    traded_value: float
    open_interest: int
    lot_size: int

    @property
    def is_an_option(self) -> bool:
        return self.option_right_code is not None


@dataclass(frozen=True)
class DerivativeCaptureUniverse:
    """What was selected, from which session, and what was dropped and why.

    `dropped_by_reason` is not decoration. A selection that reports only what it kept cannot
    distinguish "NSE listed fewer contracts" from "the instrument master stopped resolving them",
    and the second is how a subscription list quietly empties.
    """

    contracts: tuple[CaptureCandidateContract, ...]
    source_session: date | None
    note: str
    dropped_by_reason: Mapping[str, int] = field(default_factory=dict)

    @property
    def option_count(self) -> int:
        return sum(1 for contract in self.contracts if contract.is_an_option)

    @property
    def future_count(self) -> int:
        return sum(1 for contract in self.contracts if not contract.is_an_option)

    @property
    def underlyings(self) -> frozenset[str]:
        return frozenset(contract.underlying_symbol for contract in self.contracts)

    @property
    def tokens(self) -> tuple[int, ...]:
        return tuple(contract.instrument_token for contract in self.contracts)


def select_derivative_capture_universe(
    *,
    as_of: date,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    option_contract_limit: int | None = None,
) -> DerivativeCaptureUniverse:
    """Every live future, plus each underlying's nearest live option expiry ranked by traded value.

    `option_contract_limit` bounds only the OPTION side and is a read bound rather than a policy —
    the real cut belongs to the admission controller, which knows the packet rates and the disk. It
    exists so a caller probing the selection does not have to materialise the whole chain, and when
    it binds it says so in the note (`R.11`: a silent truncation reads as full coverage).
    """
    connection = _connect(market_data)
    try:
        _require_contract_table(connection, market_data)
        session = _latest_session(connection, as_of)
        if session is None:
            return DerivativeCaptureUniverse(
                (), None, f"no F&O contract rows at or before {as_of.isoformat()}"
            )
        rows = connection.execute(
            """
            SELECT contract_type, underlying_symbol, expiry_date, strike_price,
                   option_right_code, instrument_name, total_traded_value, open_interest,
                   lot_size
            FROM fo_bhavcopy_contracts
            WHERE trade_date = ?
            """,
            (session.isoformat(),),
        ).fetchall()
        token_by_symbol = _nfo_token_index(connection)
    finally:
        connection.close()

    dropped: Counter[str] = Counter()
    futures: list[CaptureCandidateContract] = []
    options_by_underlying: dict[str, list[CaptureCandidateContract]] = {}
    nearest_live_expiry: dict[str, date] = {}

    for row in rows:
        candidate = _candidate_from_row(
            row, as_of=as_of, token_by_symbol=token_by_symbol, dropped=dropped
        )
        if candidate is None:
            continue
        if candidate.is_an_option:
            options_by_underlying.setdefault(candidate.underlying_symbol, []).append(candidate)
            known = nearest_live_expiry.get(candidate.underlying_symbol)
            if known is None or candidate.expiry < known:
                nearest_live_expiry[candidate.underlying_symbol] = candidate.expiry
        else:
            futures.append(candidate)

    selected_options: list[CaptureCandidateContract] = []
    for underlying, candidates in options_by_underlying.items():
        front = nearest_live_expiry[underlying]
        for candidate in candidates:
            if candidate.expiry == front:
                selected_options.append(candidate)
            else:
                dropped["a nearer expiry is alive"] += 1

    # Traded value descending, then the symbol, so the order is total and a re-run is identical.
    selected_options.sort(key=lambda contract: (-contract.traded_value, contract.trading_symbol))
    futures.sort(key=lambda contract: (-contract.traded_value, contract.trading_symbol))

    bounded = option_contract_limit is not None and len(selected_options) > option_contract_limit
    if bounded:
        assert option_contract_limit is not None  # narrowed by `bounded`
        dropped["beyond the read bound"] += len(selected_options) - option_contract_limit
        selected_options = selected_options[:option_contract_limit]

    contracts = tuple(futures) + tuple(selected_options)
    note = (
        f"{len(futures):,} live future(s) and {len(selected_options):,} option(s) across "
        f"{len(nearest_live_expiry):,} underlying front expiry/expiries, ranked by their own "
        f"traded value from the {session.isoformat()} session"
    )
    if bounded:
        note += (
            f" (BOUNDED at {option_contract_limit:,} options for this read — not the full chain)"
        )
    if dropped:
        worst = ", ".join(
            f"{reason} x{count:,}"
            for reason, count in sorted(dropped.items(), key=lambda item: -item[1])
        )
        note += f"; dropped {sum(dropped.values()):,} ({worst})"

    return DerivativeCaptureUniverse(
        contracts=contracts,
        source_session=session,
        note=note,
        dropped_by_reason=dict(dropped),
    )


# ---------------------------------------------------------------------------------------------


def _candidate_from_row(
    row: tuple[Any, ...],
    *,
    as_of: date,
    token_by_symbol: Mapping[str, tuple[int, str]],
    dropped: Counter[str],
) -> CaptureCandidateContract | None:
    (
        contract_type_raw,
        underlying_raw,
        expiry_raw,
        strike_raw,
        right_raw,
        symbol_raw,
        traded_value_raw,
        open_interest_raw,
        lot_size_raw,
    ) = row

    contract_type = str(contract_type_raw or "").upper()
    if contract_type not in FUTURE_CONTRACT_TYPES | OPTION_CONTRACT_TYPES:
        dropped["not an NSE F&O contract type"] += 1
        return None

    symbol = str(symbol_raw or "").strip().upper()
    if not symbol:
        # Legacy rows predate the projection and carry no instrument name; they cannot be
        # subscribed because there is nothing to resolve a token by.
        dropped["no instrument name"] += 1
        return None

    try:
        expiry = date.fromisoformat(str(expiry_raw))
    except (TypeError, ValueError):
        dropped["unreadable expiry"] += 1
        return None
    if expiry < as_of:
        dropped["already settled"] += 1
        return None

    resolved = token_by_symbol.get(symbol)
    if resolved is None:
        dropped["no instrument token"] += 1
        return None
    token, segment = resolved

    right = str(right_raw or "").strip().upper() or None
    is_an_option = contract_type in OPTION_CONTRACT_TYPES
    if is_an_option != (right is not None):
        dropped["contract type disagrees with the option right"] += 1
        return None
    if is_an_option != (segment == "NFO-OPT"):
        dropped["contract type disagrees with the instrument master segment"] += 1
        return None

    traded_value = float(traded_value_raw) if traded_value_raw is not None else 0.0

    return CaptureCandidateContract(
        instrument_token=int(token),
        trading_symbol=symbol,
        exchange=NFO_EXCHANGE,
        contract_type=contract_type,
        underlying_symbol=str(underlying_raw or "").strip().upper(),
        expiry=expiry,
        strike_price=float(strike_raw) if strike_raw is not None else None,
        option_right_code=right,
        traded_value=traded_value,
        open_interest=int(open_interest_raw or 0),
        lot_size=max(int(lot_size_raw or 1), 1),
    )


def _connect(market_data: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{market_data}?mode=ro", uri=True)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _require_contract_table(connection: sqlite3.Connection, market_data: Path) -> None:
    present = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
        (CONTRACT_TABLE,),
    ).fetchone()[0]
    if not present:
        raise DerivativeCaptureSelectionError(
            f"{market_data} holds no {CONTRACT_TABLE} table — the derivative contract projection "
            f"has never run, and reporting 'no contracts' would read as a claim about the market"
        )


def _latest_session(connection: sqlite3.Connection, as_of: date) -> date | None:
    row = connection.execute(
        "SELECT MAX(trade_date) FROM fo_bhavcopy_contracts WHERE trade_date <= ?",
        (as_of.isoformat(),),
    ).fetchone()
    if not row or not row[0]:
        return None
    return date.fromisoformat(str(row[0]))


def _nfo_token_index(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Trading symbol to (token, segment) from the LATEST instrument master ingest only.

    The master is append-only per ingest day, so a symbol appears once per day it existed. Joining
    without this filter multiplies every contract by the number of days held — measured on the real
    store as 36,692 index-option rows where the session holds 5,354.
    """
    latest = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[0]
    if latest is None:
        return {}
    placeholders = ",".join("?" for _ in NFO_MASTER_SEGMENTS)
    rows = connection.execute(
        "SELECT tradingsymbol, instrument_token, segment FROM instrument_master "  # noqa: S608
        f"WHERE ingested_on = ? AND segment IN ({placeholders})",
        (latest, *NFO_MASTER_SEGMENTS),
    ).fetchall()
    index: dict[str, tuple[int, str]] = {}
    for symbol, token, segment in rows:
        if token is None:
            continue
        index[str(symbol).strip().upper()] = (int(token), str(segment))
    return index
