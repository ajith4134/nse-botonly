"""`L0.08` — ISIN as the stable key across renames, so a symbol is never an identity.

**The failure this exists to prevent.** A backtest keyed on `SYMBOL` silently splices two
different things together. Measured on this project's own 43,215 real bhavcopy rows:
**166 ISINs have traded under more than one symbol** (`ADANIGAS` became `ATGL`,
`ADANITRANS` became `ADANIENSOL`), and **199 symbols have referred to more than one
ISIN**. Key on the symbol and the first group's history is truncated at the rename while
the second group's is welded together across a security that changed underneath it.
Neither error announces itself — both produce a clean, plausible price series.

**What this store adjudicates, and what it deliberately does not.**

- *Same ISIN, different symbols over time* → a **rename**. Unambiguous, and it needs no
  knowledge of what an ISIN means internally.
- *Same symbol, different ISINs over time* → the symbol is **not a stable key** over that
  interval. Recorded as a conflict and resolved point-in-time.

It does NOT decide whether two ISINs belong to the same legal issuer. That was tried and
abandoned on evidence: classifying by a shared 8-character prefix called `TATASTEEL` a
different company, because `IN9081A01010` and `INE081A01012` differ at position 3 — which
is the ISSUER TYPE (`9` partly-paid, `E` equity), not the issuer. It also split
`ECLFINANCE`'s fifteen debt series across "different issuers". Every one of the eight hits
was a false positive produced by guessing at a numbering standard rather than reading one.
Deciding issuer identity needs an issuer registry, which this project does not yet hold —
recorded as a blocker rather than faked with string surgery (`R.16`).

**What changes behaviour.** `resolve_isin_for_symbol` refuses to guess when a symbol is
ambiguous on a date, so a caller keyed on symbols is forced to confront it instead of
receiving a confident wrong answer.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from itertools import pairwise
from pathlib import Path
from types import TracebackType

ISIN_LENGTH = 12
"""ISO 6166 fixes an ISIN at twelve characters. A regulatory fact, not a tuning knob —
and verified against the corpus: all 4,727 distinct ISINs observed are exactly 12."""


class SecurityIdentityError(RuntimeError):
    """The store could not answer honestly."""


class AmbiguousSymbolError(SecurityIdentityError):
    """A symbol referred to more than one security on the date asked about.

    Raised rather than resolved. A store that picked one would be choosing which company's
    price history to return, silently, on exactly the dates where it matters most.
    """


class UnknownIdentityError(SecurityIdentityError):
    """Nothing was observed for this identity on or before the date asked about."""


class IdentityEventKind(Enum):
    """What changed between two observations of the same thing."""

    SYMBOL_RENAMED = "symbol renamed"
    """One ISIN began trading under a new symbol. The security is continuous."""

    SYMBOL_REASSIGNED = "symbol reassigned to a different ISIN"
    """One symbol began referring to a different ISIN. The security is NOT continuous,
    and any series keyed on the symbol spans two different things."""


@dataclass(frozen=True)
class SecurityIdentityObservation:
    """One (isin, symbol) pairing seen on one date, from a real published file."""

    isin: str
    symbol: str
    observed_on: date

    def __post_init__(self) -> None:
        if len(self.isin) != ISIN_LENGTH:
            raise SecurityIdentityError(
                f"{self.isin!r} is not a {ISIN_LENGTH}-character ISIN"
            )
        if not self.symbol:
            raise SecurityIdentityError("symbol may not be empty")


@dataclass(frozen=True)
class IdentityEvent:
    """A change in the symbol-to-ISIN mapping, with the dates that bracket it.

    Both dates are carried because the change happened SOMEWHERE between them and this
    store must not pretend to know where. Observation is sparse — the corpus holds 13
    distinct dates across six years — so `first_seen_after` is an upper bound on when the
    rename took effect, never the effective date itself.
    """

    kind: IdentityEventKind
    isin: str
    symbol: str
    previous_value: str
    new_value: str
    last_seen_before: date
    first_seen_after: date


@dataclass(frozen=True)
class SymbolAmbiguity:
    """A symbol that referred to more than one ISIN across the observed history."""

    symbol: str
    isins: tuple[str, ...]

    @property
    def is_ambiguous(self) -> bool:
        return len(self.isins) > 1


def derive_identity_events(
    observations: Iterable[SecurityIdentityObservation],
) -> list[IdentityEvent]:
    """Every rename and reassignment implied by a set of observations.

    Walks each identity's own timeline in date order and emits an event wherever the
    counterpart changed between consecutive observations. Consecutive matters: comparing
    only first-to-last would collapse `A -> B -> A` into no event at all, and a symbol
    that returned to a previous ISIN is precisely the case that corrupts a series.
    """
    events: list[IdentityEvent] = []

    by_isin: dict[str, list[SecurityIdentityObservation]] = defaultdict(list)
    by_symbol: dict[str, list[SecurityIdentityObservation]] = defaultdict(list)
    for observation in observations:
        by_isin[observation.isin].append(observation)
        by_symbol[observation.symbol].append(observation)

    for isin, seen in by_isin.items():
        ordered = sorted(set(seen), key=lambda o: (o.observed_on, o.symbol))
        for earlier, later in pairwise(ordered):
            if earlier.symbol != later.symbol:
                events.append(
                    IdentityEvent(
                        kind=IdentityEventKind.SYMBOL_RENAMED,
                        isin=isin,
                        symbol=later.symbol,
                        previous_value=earlier.symbol,
                        new_value=later.symbol,
                        last_seen_before=earlier.observed_on,
                        first_seen_after=later.observed_on,
                    )
                )

    for symbol, seen in by_symbol.items():
        ordered = sorted(set(seen), key=lambda o: (o.observed_on, o.isin))
        for earlier, later in pairwise(ordered):
            if earlier.isin != later.isin:
                events.append(
                    IdentityEvent(
                        kind=IdentityEventKind.SYMBOL_REASSIGNED,
                        isin=later.isin,
                        symbol=symbol,
                        previous_value=earlier.isin,
                        new_value=later.isin,
                        last_seen_before=earlier.observed_on,
                        first_seen_after=later.observed_on,
                    )
                )

    return sorted(events, key=lambda e: (e.first_seen_after, e.isin, e.symbol))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS security_identity_observation (
    isin        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    PRIMARY KEY (isin, symbol, observed_on)
);
CREATE INDEX IF NOT EXISTS security_identity_by_symbol
    ON security_identity_observation (symbol, observed_on);
CREATE INDEX IF NOT EXISTS security_identity_by_isin
    ON security_identity_observation (isin, observed_on);
"""


class SecurityIdentityRecordStore:
    """Persisted identity observations, and point-in-time resolution over them.

    Append-only by construction: the primary key makes a repeated observation a no-op
    rather than an update, so re-ingesting a file can never rewrite what was believed on
    an earlier date.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def __enter__(self) -> SecurityIdentityRecordStore:
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

    def record(self, observations: Iterable[SecurityIdentityObservation]) -> int:
        """Persist observations. Returns how many were genuinely new."""
        rows = [(o.isin, o.symbol, o.observed_on.isoformat()) for o in observations]
        if not rows:
            return 0
        before = self._connection.total_changes
        self._connection.executemany(
            "INSERT OR IGNORE INTO security_identity_observation "
            "(isin, symbol, observed_on) VALUES (?, ?, ?)",
            rows,
        )
        self._connection.commit()
        return self._connection.total_changes - before

    def observations(self) -> list[SecurityIdentityObservation]:
        return [
            SecurityIdentityObservation(isin, symbol, date.fromisoformat(observed_on))
            for isin, symbol, observed_on in self._connection.execute(
                "SELECT isin, symbol, observed_on FROM security_identity_observation"
            )
        ]

    def resolve_isin_for_symbol(self, symbol: str, on: date) -> str:
        """The ISIN this symbol referred to as of a date.

        Uses the most recent observation ON OR BEFORE the date — never a later one, which
        would be lookahead. Raises when the symbol referred to more than one security on
        that date rather than choosing between them.
        """
        rows = self._connection.execute(
            "SELECT isin FROM security_identity_observation "
            "WHERE symbol = ? AND observed_on <= ? AND observed_on = ("
            "  SELECT MAX(observed_on) FROM security_identity_observation"
            "  WHERE symbol = ? AND observed_on <= ?)",
            (symbol, on.isoformat(), symbol, on.isoformat()),
        ).fetchall()
        if not rows:
            raise UnknownIdentityError(f"no observation of {symbol!r} on or before {on}")
        candidates: set[str] = {str(row[0]) for row in rows}
        if len(candidates) > 1:
            raise AmbiguousSymbolError(
                f"{symbol!r} referred to {len(candidates)} securities on {on}: "
                f"{sorted(candidates)}"
            )
        return next(iter(candidates))

    def resolve_symbol_for_isin(self, isin: str, on: date) -> str:
        """The symbol this security traded under as of a date.

        An ISIN under two symbols on ONE date is a data defect rather than an identity
        question, so the symbols are reported together instead of one being picked.
        """
        rows = self._connection.execute(
            "SELECT symbol FROM security_identity_observation "
            "WHERE isin = ? AND observed_on <= ? AND observed_on = ("
            "  SELECT MAX(observed_on) FROM security_identity_observation"
            "  WHERE isin = ? AND observed_on <= ?)",
            (isin, on.isoformat(), isin, on.isoformat()),
        ).fetchall()
        if not rows:
            raise UnknownIdentityError(f"no observation of {isin!r} on or before {on}")
        symbols: set[str] = {str(row[0]) for row in rows}
        if len(symbols) > 1:
            raise SecurityIdentityError(
                f"{isin!r} observed under {sorted(symbols)} on the same date"
            )
        return next(iter(symbols))

    def identity_events(self) -> list[IdentityEvent]:
        """Every rename and reassignment in the stored history."""
        return derive_identity_events(self.observations())

    def ambiguous_symbols(self) -> list[SymbolAmbiguity]:
        """Symbols that have referred to more than one ISIN, worst first."""
        by_symbol: dict[str, set[str]] = defaultdict(set)
        for isin, symbol in self._connection.execute(
            "SELECT isin, symbol FROM security_identity_observation"
        ):
            by_symbol[symbol].add(isin)
        ambiguities = [
            SymbolAmbiguity(symbol=symbol, isins=tuple(sorted(isins)))
            for symbol, isins in by_symbol.items()
            if len(isins) > 1
        ]
        return sorted(ambiguities, key=lambda a: (-len(a.isins), a.symbol))

    def describe(self) -> str:
        """One line for the daily report."""
        events = self.identity_events()
        renames = sum(
            1 for e in events if e.kind is IdentityEventKind.SYMBOL_RENAMED
        )
        reassignments = sum(
            1 for e in events if e.kind is IdentityEventKind.SYMBOL_REASSIGNED
        )
        total = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM security_identity_observation"
            ).fetchone()[0]
        )
        distinct_isins = int(
            self._connection.execute(
                "SELECT COUNT(DISTINCT isin) FROM security_identity_observation"
            ).fetchone()[0]
        )
        return (
            f"{total:,} identity observations · {distinct_isins:,} ISINs · "
            f"{renames} renames · {reassignments} symbol reassignments · "
            f"{len(self.ambiguous_symbols())} symbols not safe as a key"
        )


def observations_from_bhavcopy_rows(
    rows: Sequence[tuple[str, Mapping[str, object]]],
) -> list[SecurityIdentityObservation]:
    """Identity observations out of stored cash-bhavcopy rows.

    Takes `(effective_date_iso, row_values)` pairs. Rows missing either field are skipped
    rather than raised on: the bhavcopy schema changed across the corpus (`SYMBOL` in the
    older layout, `TckrSymb` in the UDiFF one), and one unparseable row must not cost the
    other forty thousand.
    """
    observations: list[SecurityIdentityObservation] = []
    for effective_date, values in rows:
        isin = values.get("ISIN") or values.get("isin_code")
        symbol = values.get("SYMBOL") or values.get("TckrSymb")
        if not isinstance(isin, str) or not isinstance(symbol, str):
            continue
        isin, symbol = isin.strip(), symbol.strip()
        if len(isin) != ISIN_LENGTH or not symbol:
            continue
        observations.append(
            SecurityIdentityObservation(
                isin=isin, symbol=symbol, observed_on=date.fromisoformat(effective_date)
            )
        )
    return observations
