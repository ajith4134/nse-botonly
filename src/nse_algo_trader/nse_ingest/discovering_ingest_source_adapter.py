"""`L0.35` — two-phase discover-then-fetch, so an adapter can ASK before it guesses.

Some NSE sources need a parameter that cannot be known before fetching something else.
The ATM-IV chain is the measured case: `/api/option-chain-v3` requires an exact `expiry`
query param, and the adapter contract deliberately forbids I/O inside `fetch_targets`,
so `L0.27` had no way to learn one. It compensated with a bounded calendar-derived
candidate ladder — **~2,700 requests per run at full universe** against
`www.nseindia.com`, the one inconsistently-gated host in this project.

Worse than the volume: **a wrong guess returns HTTP 200 with a validly-shaped, empty
`data: []`**, not a 404. So the ladder cannot even be cheaply pruned by status code, and
every wrong rung costs a full request against a host that bot-walls.

This module adds the missing phase. An adapter may declare a **discovery** step whose
result parameterises its real fetch targets:

    discovery_targets()  ->  fetch  ->  parse_discovery()  ->  fetch_targets_from()

Two things make it worth building rather than living with the ladder:

**Discovered parameters are remembered, with derived validity.** A discovery is stored
and reused until it genuinely expires, so the second run of a day costs one request
rather than thousands. The validity horizon is **derived from the discovered data
itself** — a list of option expiries is valid until its own nearest expiry passes — never
a typed-in TTL (`R.03`). A constant TTL would be wrong in both directions: too long and
it serves a dead expiry, too short and it rediscovers for nothing.

**A discovery miss is a distinct outcome.** "I asked and the answer was empty" is not
"the fetch failed" and not "there is no data" — it is a source that answered, honestly,
that the thing does not exist. Conflating it with a failure would drive endless retries;
conflating it with an absence would silently narrow the universe.

Falling back to the ladder is allowed and recorded rather than silent: when discovery is
blocked, the adapter still works, just expensively, and the run says so.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovered_parameter (
    source_name     TEXT NOT NULL,
    parameter_kind  TEXT NOT NULL,
    parameter_value TEXT NOT NULL,
    discovered_at   TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    evidence        TEXT NOT NULL,
    PRIMARY KEY (source_name, parameter_kind, parameter_value)
)
"""


class DiscoveryOutcome(Enum):
    """What a discovery attempt established. Three states, never two."""

    DISCOVERED = "discovered"
    ANSWERED_EMPTY = "answered_empty"
    """The source responded correctly and the thing does not exist. Not a failure — an
    answer. Retrying it is waste; treating it as absence would narrow the universe."""

    UNAVAILABLE = "unavailable"
    """Blocked, timed out, or malformed. The answer is unknown, so a fallback is
    justified and the run must say it fell back."""


@dataclass(frozen=True)
class DiscoveredParameter:
    """One learned parameter and the horizon over which it stays true."""

    parameter_kind: str
    parameter_value: str
    valid_until: date
    """Derived from the parameter's own meaning — an expiry list is valid until its
    nearest expiry passes — never a fixed time-to-live."""

    evidence: str = ""

    def is_valid_on(self, day: date) -> bool:
        return day <= self.valid_until


@dataclass(frozen=True)
class DiscoveryResult:
    """What one discovery phase produced, including when it produced nothing."""

    outcome: DiscoveryOutcome
    parameters: tuple[DiscoveredParameter, ...] = ()
    evidence: str = ""

    @property
    def is_usable(self) -> bool:
        return self.outcome is DiscoveryOutcome.DISCOVERED and bool(self.parameters)


@runtime_checkable
class DiscoveringNseIngestSourceAdapter(NseIngestSourceAdapter, Protocol):
    """An adapter that must ask the source something before it can fetch.

    Extends the base contract rather than replacing it: a discovering adapter is still a
    normal adapter, so everything in the core and the conformance suite applies to it
    unchanged. Adapters that need no discovery are unaffected and stay simpler.
    """

    def discovery_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """What to fetch in order to learn the parameter. Usually one cheap request."""
        ...

    def parse_discovery(self, payload: bytes, target: FetchTarget) -> DiscoveryResult:
        """Read the discovered parameters, or report that the source answered empty.

        Must distinguish an empty answer from a malformed one. The measured NSE case is
        an HTTP 200 carrying `data: []`, which is a real answer and not a failure.
        """
        ...

    def fetch_targets_from_discovery(
        self, for_dates: Sequence[date], discovered: Sequence[DiscoveredParameter]
    ) -> Sequence[FetchTarget]:
        """The real targets, now that the parameter is known."""
        ...


class DiscoveredParameterStore:
    """Remembers discovered parameters so a second run costs one request, not thousands.

    Shares the ingest database rather than opening its own: a discovery belongs to the
    same provenance record as the rows it made reachable.
    """

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DiscoveredParameterStore:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def remember(
        self,
        source_name: str,
        parameters: Sequence[DiscoveredParameter],
        discovered_at: datetime,
    ) -> int:
        """Store parameters, replacing any earlier value for the same key.

        Replacement rather than revision, deliberately: unlike ingested rows, a
        discovered parameter is a fact about *right now* with no historical meaning. The
        expiry list as it stood last Tuesday is of no use to anyone, and keeping it would
        make `valid_for` pick between stale candidates.
        """
        written = 0
        for parameter in parameters:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO discovered_parameter (
                    source_name, parameter_kind, parameter_value,
                    discovered_at, valid_until, evidence
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    parameter.parameter_kind,
                    parameter.parameter_value,
                    discovered_at.astimezone(UTC).isoformat(),
                    parameter.valid_until.isoformat(),
                    parameter.evidence,
                ),
            )
            written += 1
        return written

    def valid_for(
        self, source_name: str, on_day: date, parameter_kind: str | None = None
    ) -> tuple[DiscoveredParameter, ...]:
        """Everything still true on `on_day`. Expired entries are simply not returned."""
        query = """
            SELECT parameter_kind, parameter_value, valid_until, evidence
            FROM discovered_parameter
            WHERE source_name = ? AND valid_until >= ?
        """
        parameters: list[object] = [source_name, on_day.isoformat()]
        if parameter_kind is not None:
            query += " AND parameter_kind = ?"
            parameters.append(parameter_kind)
        query += " ORDER BY parameter_kind, parameter_value"
        return tuple(
            DiscoveredParameter(
                parameter_kind=record["parameter_kind"],
                parameter_value=record["parameter_value"],
                valid_until=date.fromisoformat(record["valid_until"]),
                evidence=record["evidence"],
            )
            for record in self._connection.execute(query, parameters)
        )

    def forget_expired(self, before_day: date) -> int:
        """Drop parameters that can no longer be true. Returns how many went."""
        cursor = self._connection.execute(
            "DELETE FROM discovered_parameter WHERE valid_until < ?",
            (before_day.isoformat(),),
        )
        return cursor.rowcount


def validity_horizon_from_dates(
    candidate_dates: Sequence[date], discovered_on: date
) -> date:
    """The last day a set of forward-dated parameters can still be trusted.

    A discovered list of option expiries stops being current the moment its nearest
    expiry passes, because the exchange will have added a new far expiry by then. So the
    horizon is the **earliest** future date in the set, not the latest — using the latest
    would keep serving a list missing its front month for weeks.

    With no future date in the set there is nothing to trust beyond today, and the caller
    is told so by receiving `discovered_on` itself.
    """
    future_dates = sorted(day for day in candidate_dates if day > discovered_on)
    if not future_dates:
        return discovered_on
    return future_dates[0]


@dataclass(frozen=True)
class DiscoveryPlan:
    """The targets to fetch, and how they were arrived at.

    `used_fallback` is the honesty flag: a run that guessed its way to targets is not the
    same as one that asked, and the request cost differs by orders of magnitude.
    """

    targets: tuple[FetchTarget, ...]
    used_fallback: bool
    outcome: DiscoveryOutcome | None
    reused_memo: bool
    evidence: str

    @property
    def request_count(self) -> int:
        return len(self.targets)


def plan_targets_with_discovery(
    adapter: DiscoveringNseIngestSourceAdapter,
    for_dates: Sequence[date],
    memo: DiscoveredParameterStore,
    discovery_results: Mapping[str, DiscoveryResult] | None = None,
    today: date | None = None,
) -> DiscoveryPlan:
    """Build fetch targets from memory, then discovery, then the adapter's own fallback.

    The order is the whole point. Memory first because a valid remembered parameter costs
    zero requests. Discovery second because one request beats thousands of guesses. The
    adapter's `fetch_targets` ladder last, because it works but is the expensive path —
    and when it is used, `used_fallback` says so rather than hiding it behind an
    identical-looking result.
    """
    on_day = today or datetime.now(UTC).date()

    remembered = memo.valid_for(adapter.source_name, on_day)
    if remembered:
        return DiscoveryPlan(
            targets=tuple(adapter.fetch_targets_from_discovery(for_dates, remembered)),
            used_fallback=False,
            outcome=DiscoveryOutcome.DISCOVERED,
            reused_memo=True,
            evidence=f"{len(remembered)} remembered parameter(s) still valid on {on_day}",
        )

    if discovery_results:
        merged: list[DiscoveredParameter] = []
        outcomes = {result.outcome for result in discovery_results.values()}
        for result in discovery_results.values():
            merged.extend(result.parameters)
        if merged:
            memo.remember(adapter.source_name, merged, datetime.now(UTC))
            return DiscoveryPlan(
                targets=tuple(adapter.fetch_targets_from_discovery(for_dates, merged)),
                used_fallback=False,
                outcome=DiscoveryOutcome.DISCOVERED,
                reused_memo=False,
                evidence=f"discovered {len(merged)} parameter(s)",
            )
        if outcomes == {DiscoveryOutcome.ANSWERED_EMPTY}:
            # The source answered and there is nothing. Guessing past a clear answer
            # would be the ladder's worst case for no reason.
            return DiscoveryPlan(
                targets=(),
                used_fallback=False,
                outcome=DiscoveryOutcome.ANSWERED_EMPTY,
                reused_memo=False,
                evidence="source answered with no parameters; nothing to fetch",
            )

    fallback_targets = tuple(adapter.fetch_targets(for_dates))
    return DiscoveryPlan(
        targets=fallback_targets,
        used_fallback=True,
        outcome=DiscoveryOutcome.UNAVAILABLE if discovery_results else None,
        reused_memo=False,
        evidence=(
            f"discovery unavailable; fell back to the adapter's candidate ladder "
            f"({len(fallback_targets)} requests)"
        ),
    )


def discovery_result_from_json_list(
    payload: bytes,
    json_key: str,
    parameter_kind: str,
    discovered_on: date,
    date_format: str | None = None,
) -> DiscoveryResult:
    """A discovery result from a JSON payload carrying a list under `json_key`.

    Shared because the shape recurs across NSE's JSON endpoints. An empty list is
    reported as `ANSWERED_EMPTY` rather than as a parse failure — the measured NSE
    behaviour is an HTTP 200 carrying `data: []`, which is a real answer.
    """
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as failure:
        raise IngestAdapterError(f"discovery payload is not JSON: {failure}") from failure
    if not isinstance(document, dict):
        raise IngestAdapterError("discovery payload is not a JSON object")

    values = document.get(json_key)
    if values is None:
        raise IngestAdapterError(f"discovery payload has no {json_key!r} key")
    if not isinstance(values, list):
        raise IngestAdapterError(f"{json_key!r} is not a list")
    if not values:
        return DiscoveryResult(
            outcome=DiscoveryOutcome.ANSWERED_EMPTY,
            evidence=f"{json_key!r} was present and empty",
        )

    parsed_dates: list[date] = []
    if date_format is not None:
        for value in values:
            try:
                parsed_dates.append(datetime.strptime(str(value), date_format).date())  # noqa: DTZ007
            except ValueError as failure:
                raise IngestAdapterError(
                    f"discovered value {value!r} is not a date in {date_format!r}"
                ) from failure
    horizon = (
        validity_horizon_from_dates(parsed_dates, discovered_on)
        if parsed_dates
        else discovered_on
    )
    return DiscoveryResult(
        outcome=DiscoveryOutcome.DISCOVERED,
        parameters=tuple(
            DiscoveredParameter(
                parameter_kind=parameter_kind,
                parameter_value=str(value),
                valid_until=horizon,
                evidence=f"from {json_key!r}, {len(values)} value(s)",
            )
            for value in values
        ),
        evidence=f"discovered {len(values)} {parameter_kind} value(s), valid to {horizon}",
    )
