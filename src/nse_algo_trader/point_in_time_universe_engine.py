"""What was tradeable on a date, using only what was knowable on that date.

**This is an engine** (R.23b). Per-date exchange files in; a temporal-resolution
procedure plus a cross-sectional absence classifier as the solver; accumulated
validity intervals and belief revisions as carried state; a frozen universe out
that gates every downstream scan, signal and order.

**SOTA analogs** (R.23a): Zipline's ``AssetFinder.lifetimes()`` and Qlib's
instrument universe with ``start``/``end`` bounds. Both *consume* known listing
intervals. Neither reconstructs them from sparse daily observations, and neither
classifies **why** an instrument disappeared — the whole difficulty when the input
is a daily file rather than a curated master.

**The problem.** Absence from a file is ambiguous: did not trade, never collected,
ran off and left the segment, renamed, delisted. Measured (``docs/research/204``):
324 of 3,419 cash symbols miss at least one of five days and are mostly government
securities that do not trade daily, while four July trading days were never
collected. A classifier that resolves this by guessing would delist most of the
G-Sec universe in a week and read an outage as 216 simultaneous exits. Following
``A.41``, ``UNKNOWN`` is a first-class outcome carrying its evidence.

**Direction, not depth — the correction that matters.** NSE stops listing new
expiries before an underlying leaves F&O, so its ladder shortens as contracts run
off. But a *newly listed* underlying ramping up to a full ladder has a short ladder
too, and on any single date the two are indistinguishable. Adversarial review
reproduced it: a symbol ramping ``1→1→2→3`` was flagged with exactly the confidence
``EXIDEIND`` was. The discriminator is the **furthest expiry**, not the count:

* running off — the furthest expiry stands still while the near ones expire;
* ramping up — the furthest expiry advances as the exchange lists new months.

Depth alone cannot separate them, and it fails worst exactly where it matters: the
underlyings already mid-exit when the collection window opens have no earlier full
ladder to compare against. A short ladder is therefore reported with its
**direction**, and only a non-advancing one is treated as an exit warning.

The ladder norm is the cross-sectional mode of that date's own file (R.03e);
writing ``3`` down would freeze a regulatory choice that has already changed.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Final

_TUKEY_FENCE_MULTIPLE: Final = 1.5  # standard outlier fence, not a tuned value
_QUARTILE_DIVISIONS: Final = 4
_MINIMUM_GAPS_FOR_A_DISTRIBUTION: Final = 4  # statistics.quantiles needs n > divisions


class UniverseSnapshotError(Exception):
    """The engine was asked something the evidence cannot support."""


class ObservationSource(StrEnum):
    """Which exchange file an observation came from.

    Explicit because the sources mean different things: presence in the F&O
    bhavcopy is evidence of *eligibility*, presence in the cash bhavcopy only of
    *trading*.
    """

    FO_BHAVCOPY = "fo_bhavcopy"
    CASH_BHAVCOPY = "cash_bhavcopy"
    MWPL = "mwpl"
    BAN_LIST = "ban_list"


class AbsenceClass(StrEnum):
    """Why a symbol is missing. ``UNKNOWN`` is an answer, not a failure."""

    UNOBSERVED = "unobserved"
    NOT_TRADED = "not_traded"
    EXITED_DERIVATIVES = "exited_derivatives"
    RENAMED = "renamed"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class LadderDirection(StrEnum):
    """Which way a short expiry ladder is moving — the exit/new-listing discriminator."""

    RUNNING_OFF = "running_off"
    RAMPING_UP = "ramping_up"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class UniverseObservation:
    """One symbol seen in one file on one date."""

    symbol: str
    trade_date: date
    segment: str
    source: ObservationSource
    expiry_ladder_depth: int | None
    isin: str | None
    furthest_expiry: date | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise UniverseSnapshotError("symbol must be a non-empty string")
        if not isinstance(self.trade_date, date):
            raise UniverseSnapshotError(
                f"trade_date must be a date, not {type(self.trade_date).__name__}"
            )
        if self.expiry_ladder_depth is not None and self.expiry_ladder_depth < 0:
            raise UniverseSnapshotError(
                f"expiry_ladder_depth cannot be negative; got {self.expiry_ladder_depth}"
            )


@dataclass(frozen=True, slots=True)
class AbsenceVerdict:
    """Why a symbol is missing on a date, with the evidence that decided it.

    Evidence travels with the verdict: a classification a caller cannot audit is
    indistinguishable from a guess.
    """

    symbol: str
    trade_date: date
    classification: AbsenceClass
    evidence: tuple[tuple[str, str], ...]

    def evidence_summary(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.evidence)


@dataclass(frozen=True, slots=True)
class LadderTruncation:
    """An underlying whose expiry ladder is short against its own date's norm."""

    symbol: str
    trade_date: date
    observed_depth: int
    cross_sectional_norm: int
    direction: LadderDirection
    furthest_expiry: date | None

    @property
    def missing_expiries(self) -> int:
        return self.cross_sectional_norm - self.observed_depth

    @property
    def is_exit_warning(self) -> bool:
        """A short ladder warns unless it is positively known to be ramping up.

        Deliberately asymmetric. An underlying already mid-exit when the window
        opens is ``INDETERMINATE`` — there is no earlier observation to compare —
        and treating that as safe would suppress the warning in exactly the case
        the retained data proves is real (``EXIDEIND``, ``NUVAMA``).
        """
        return self.direction is not LadderDirection.RAMPING_UP


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """The universe as of one date, frozen. ``L0.06``.

    Immutable by construction: a revision writes a new belief rather than editing
    an old one, so a backtest re-run against the same ``(trade_date, known_as_of)``
    pair gets the universe it got before.
    """

    trade_date: date
    known_as_of: date
    members: frozenset[str]
    absences: tuple[AbsenceVerdict, ...]
    ladder_norm: int | None
    truncations: tuple[LadderTruncation, ...]

    @property
    def unknown_count(self) -> int:
        return sum(1 for verdict in self.absences if verdict.classification is AbsenceClass.UNKNOWN)

    @property
    def is_fully_resolved(self) -> bool:
        """Whether every absence on this date has an evidenced explanation."""
        return self.unknown_count == 0

    @property
    def exit_warnings(self) -> tuple[LadderTruncation, ...]:
        return tuple(item for item in self.truncations if item.is_exit_warning)


@dataclass(frozen=True, slots=True)
class _DateContext:
    """Everything a date's classifications need, derived once instead of per symbol.

    Built because review measured ``snapshot_as_of`` at 10.78 s for 2,500 departed
    symbols: the ladder norm and truncation set are identical for every symbol on a
    date, and were being re-derived from SQL once per symbol.
    """

    collected: tuple[date, ...]
    members: frozenset[str]
    last_seen: dict[str, date]
    next_seen: dict[str, date]
    truncation_at_last_seen: dict[str, LadderTruncation]
    rename_candidates: dict[str, tuple[str, ...]]
    ordinary_gap_ceiling: int | None


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS universe_observation (
    symbol              TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    segment             TEXT NOT NULL,
    source              TEXT NOT NULL,
    expiry_ladder_depth INTEGER,
    isin                TEXT,
    furthest_expiry     TEXT,
    known_as_of         TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date, segment, source, known_as_of)
);
CREATE INDEX IF NOT EXISTS universe_observation_by_date
    ON universe_observation (trade_date, known_as_of);
CREATE INDEX IF NOT EXISTS universe_observation_by_symbol
    ON universe_observation (symbol, trade_date);
CREATE INDEX IF NOT EXISTS universe_observation_by_isin
    ON universe_observation (isin, trade_date);
"""

# Every read must resolve to the LATEST belief at or before `known_as_of` for each
# (symbol, date, segment, source). Review reproduced the alternative: aggregating
# all matching beliefs let a stale depth-3 row mask a corrected depth-1 revision,
# so a genuine later-learned truncation vanished. Storage keeps every belief; reads
# take one.
_LATEST_BELIEF: Final = """
SELECT o.symbol, o.trade_date, o.expiry_ladder_depth, o.isin, o.furthest_expiry
FROM universe_observation AS o
JOIN (
    SELECT symbol, trade_date, segment, source, MAX(known_as_of) AS believed
    FROM universe_observation
    WHERE known_as_of <= :known_as_of
    GROUP BY symbol, trade_date, segment, source
) AS latest
  ON o.symbol = latest.symbol AND o.trade_date = latest.trade_date
 AND o.segment = latest.segment AND o.source = latest.source
 AND o.known_as_of = latest.believed
"""

_FAR_FUTURE_BELIEF: Final = "9999-12-31"


@dataclass(slots=True)
class PointInTimeUniverseEngine:
    """Reconstructs the tradeable universe as of a date, survivorship-free."""

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

    def __enter__(self) -> PointInTimeUniverseEngine:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _belief_bound(known_as_of: date | None) -> str:
        return known_as_of.isoformat() if known_as_of else _FAR_FUTURE_BELIEF

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        observations: list[UniverseObservation],
        *,
        known_as_of: date | None = None,
    ) -> int:
        """Record observations, optionally stamped with when they became knowable.

        Without ``known_as_of`` an observation is believed from its own trade date:
        a file is knowable on the day it describes, and assuming later would make
        every historical read pessimistic for no reason.

        Raises:
            UniverseSnapshotError: the write failed.
        """
        if not observations:
            return 0
        try:
            with self._connection:
                self._connection.executemany(
                    "INSERT OR REPLACE INTO universe_observation (symbol, trade_date, segment,"
                    " source, expiry_ladder_depth, isin, furthest_expiry, known_as_of)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            item.symbol,
                            item.trade_date.isoformat(),
                            item.segment,
                            str(item.source),
                            item.expiry_ladder_depth,
                            item.isin,
                            item.furthest_expiry.isoformat() if item.furthest_expiry else None,
                            (known_as_of or item.trade_date).isoformat(),
                        )
                        for item in observations
                    ],
                )
        except sqlite3.Error as error:
            raise UniverseSnapshotError(
                f"failed to ingest {len(observations)} observations: {error}"
            ) from error
        return len(observations)

    # ------------------------------------------------------------------ queries

    def collected_dates(self, *, known_as_of: date | None = None) -> tuple[date, ...]:
        """Every date for which a file was actually ingested, oldest first."""
        rows = self._connection.execute(
            "SELECT DISTINCT trade_date FROM universe_observation"
            " WHERE known_as_of <= ? ORDER BY trade_date",
            (self._belief_bound(known_as_of),),
        )
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def _require_collected(self, day: date, collected: tuple[date, ...]) -> None:
        if day not in collected:
            raise UniverseSnapshotError(
                f"{day.isoformat()} was not collected; with no file there is no fact about "
                "it, and answering anyway would be indistinguishable from 'did not trade'"
            )

    def _symbols_on(self, day: date, known_as_of: date | None = None) -> frozenset[str]:
        rows = self._connection.execute(
            f"SELECT DISTINCT symbol FROM ({_LATEST_BELIEF}) WHERE trade_date = :day",  # noqa: S608
            {"known_as_of": self._belief_bound(known_as_of), "day": day.isoformat()},
        )
        return frozenset(str(row[0]) for row in rows)

    def traded_on(self, symbol: str, day: date, *, known_as_of: date | None = None) -> bool:
        """Whether ``symbol`` appears in a file for ``day``.

        The only directly observed fact of the three the engine answers.

        Raises:
            UniverseSnapshotError: ``day`` was not collected.
        """
        self._require_collected(day, self.collected_dates(known_as_of=known_as_of))
        return symbol in self._symbols_on(day, known_as_of)

    # ------------------------------------------------------------- expiry ladder

    def _ladder_rows(
        self, day: date, known_as_of: date | None
    ) -> list[tuple[str, int, date | None]]:
        rows = self._connection.execute(
            f"SELECT symbol, MAX(expiry_ladder_depth), MAX(furthest_expiry)"  # noqa: S608
            f" FROM ({_LATEST_BELIEF})"
            " WHERE trade_date = :day AND expiry_ladder_depth IS NOT NULL GROUP BY symbol",
            {"known_as_of": self._belief_bound(known_as_of), "day": day.isoformat()},
        ).fetchall()
        return [
            (str(symbol), int(depth), date.fromisoformat(furthest) if furthest else None)
            for symbol, depth, furthest in rows
        ]

    def ladder_norm_on(self, day: date, *, known_as_of: date | None = None) -> int | None:
        """The cross-sectional mode of expiry-ladder depth for ``day``.

        Measured from that date's own file, never declared (R.03e). Observations
        carrying no ladder — cash, MWPL — are excluded rather than counted as zero,
        which would drag the mode to zero the moment cash outnumbers F&O.

        **Ties break toward the deeper ladder**, deterministically. Review found
        the previous ``Counter.most_common`` tie-break was decided by SQLite's row
        order, so a 5-5 split between depth 2 and 3 flipped with ingest order — and
        a flipped norm flips the truncation set. Deeper is also the conservative
        choice: it can only produce more warnings, never fewer.

        Returns ``None`` when the date carries no ladder-bearing observations at
        all, because a mode over nothing is undefined, not zero.
        """
        return self._mode_depth([depth for _, depth, _ in self._ladder_rows(day, known_as_of)])

    @staticmethod
    def _mode_depth(depths: list[int]) -> int | None:
        """The cross-sectional mode, with a deterministic tie-break toward depth."""
        if not depths:
            return None
        counts = Counter(depths)
        return max(counts.items(), key=lambda item: (item[1], item[0]))[0]

    def _ladder_direction(
        self, symbol: str, day: date, furthest: date | None, known_as_of: date | None
    ) -> LadderDirection:
        """Whether this symbol's furthest expiry is advancing or standing still.

        The count of expiries cannot tell a new listing from an exit; the horizon
        can. A ramping listing pushes its furthest expiry out as the exchange lists
        new months; a running-off underlying's furthest expiry is fixed while the
        near ones expire beneath it.
        """
        if furthest is None:
            return LadderDirection.INDETERMINATE
        row = self._connection.execute(
            f"SELECT MAX(furthest_expiry) FROM ({_LATEST_BELIEF})"  # noqa: S608
            " WHERE symbol = :symbol AND trade_date < :day AND furthest_expiry IS NOT NULL",
            {
                "known_as_of": self._belief_bound(known_as_of),
                "symbol": symbol,
                "day": day.isoformat(),
            },
        ).fetchone()
        if row is None or row[0] is None:
            return LadderDirection.INDETERMINATE
        return (
            LadderDirection.RAMPING_UP
            if furthest > date.fromisoformat(str(row[0]))
            else LadderDirection.RUNNING_OFF
        )

    def ladder_truncations_on(
        self, day: date, *, known_as_of: date | None = None
    ) -> tuple[LadderTruncation, ...]:
        """Underlyings whose ladder is short against that date's own norm.

        Each carries its direction, so a caller can tell a new listing ramping up
        from an underlying running off. Use :meth:`exit_warnings_on` for the
        forward-looking subset.
        """
        rows = self._ladder_rows(day, known_as_of)
        # The norm comes from the one implementation, never a second copy of the
        # tie-break: mutation testing found the duplicate was uncovered, so the two
        # could silently diverge and only the untested one would be wrong.
        norm = self._mode_depth([depth for _, depth, _ in rows])
        if norm is None:
            return ()
        return tuple(
            LadderTruncation(
                symbol=symbol,
                trade_date=day,
                observed_depth=depth,
                cross_sectional_norm=norm,
                direction=self._ladder_direction(symbol, day, furthest, known_as_of),
                furthest_expiry=furthest,
            )
            for symbol, depth, furthest in sorted(rows)
            if depth < norm
        )

    def exit_warnings_on(
        self, day: date, *, known_as_of: date | None = None
    ) -> tuple[LadderTruncation, ...]:
        """Short ladders that are not positively known to be ramping up.

        The behaviour-changing output: an underlying here must not be given new
        multi-expiry positions.
        """
        return tuple(
            item
            for item in self.ladder_truncations_on(day, known_as_of=known_as_of)
            if item.is_exit_warning
        )

    # --------------------------------------------------------------- absence

    def classify_absence(
        self, symbol: str, day: date, *, known_as_of: date | None = None
    ) -> AbsenceVerdict:
        """Why ``symbol`` is missing on ``day``, with the evidence that decided it."""
        return self._classify(symbol, day, self._build_context(day, known_as_of))

    def _build_context(self, day: date, known_as_of: date | None) -> _DateContext:
        bound = self._belief_bound(known_as_of)
        collected = self.collected_dates(known_as_of=known_as_of)
        members = self._symbols_on(day, known_as_of) if day in collected else frozenset()

        appearances: dict[str, list[date]] = {}
        for symbol, stamp in self._connection.execute(
            f"SELECT DISTINCT symbol, trade_date FROM ({_LATEST_BELIEF})",  # noqa: S608
            {"known_as_of": bound},
        ):
            appearances.setdefault(str(symbol), []).append(date.fromisoformat(str(stamp)))

        last_seen: dict[str, date] = {}
        next_seen: dict[str, date] = {}
        gap_lengths: list[int] = []
        collected_index = {stamp: position for position, stamp in enumerate(collected)}
        for symbol, seen in appearances.items():
            seen.sort()
            before = [stamp for stamp in seen if stamp < day]
            after = [stamp for stamp in seen if stamp > day]
            if before:
                last_seen[symbol] = before[-1]
            if after:
                next_seen[symbol] = after[0]
            # Ordinary-illiquidity gaps, measured in collected sessions rather than
            # calendar days so an outage does not inflate them.
            for earlier, later in pairwise(seen):
                if earlier in collected_index and later in collected_index:
                    gap_lengths.append(collected_index[later] - collected_index[earlier] - 1)

        truncations = {
            item.symbol: item
            for stamp in set(last_seen.values())
            for item in self.ladder_truncations_on(stamp, known_as_of=known_as_of)
        }

        isin_groups: dict[str, set[str]] = {}
        for symbol, isin in self._connection.execute(
            f"SELECT DISTINCT symbol, isin FROM ({_LATEST_BELIEF}) WHERE isin IS NOT NULL",  # noqa: S608
            {"known_as_of": bound},
        ):
            isin_groups.setdefault(str(isin), set()).add(str(symbol))
        rename_candidates: dict[str, tuple[str, ...]] = {}
        for shared in isin_groups.values():
            for symbol in shared:
                others = tuple(sorted(shared - {symbol}))
                if others:
                    rename_candidates[symbol] = others

        return _DateContext(
            collected=collected,
            members=members,
            last_seen=last_seen,
            next_seen=next_seen,
            truncation_at_last_seen=truncations,
            rename_candidates=rename_candidates,
            ordinary_gap_ceiling=self._ordinary_gap_ceiling(gap_lengths),
        )

    @staticmethod
    def _ordinary_gap_ceiling(gap_lengths: list[int]) -> int | None:
        """How long an absence can run before it stops looking like illiquidity.

        Tukey's upper fence over the observed gap lengths (R.03) — derived from how
        this universe actually behaves, not a declared number of days. Review found
        the unbounded version: a symbol absent 44 consecutive sessions and then
        relisted was confidently called ordinary illiquidity.

        ``None`` when there are too few gaps to describe a distribution, in which
        case no gap is treated as extraordinary rather than guessing a bound.
        """
        observed = [length for length in gap_lengths if length > 0]
        if len(observed) <= _MINIMUM_GAPS_FOR_A_DISTRIBUTION:
            return None
        quartiles = statistics.quantiles(sorted(observed), n=_QUARTILE_DIVISIONS)
        first, third = quartiles[0], quartiles[2]
        return int(third + _TUKEY_FENCE_MULTIPLE * (third - first))

    def _classify(self, symbol: str, day: date, context: _DateContext) -> AbsenceVerdict:
        """The classifier. Checks run in order of certainty.

        An uncollected date settles the question before anything else is consulted,
        because no other evidence means anything without a file.
        """
        if day not in context.collected:
            return AbsenceVerdict(
                symbol,
                day,
                AbsenceClass.UNOBSERVED,
                (("reason", "no file was collected for this date"),),
            )

        last_seen = context.last_seen.get(symbol)
        next_seen = context.next_seen.get(symbol)
        if last_seen is None and next_seen is None:
            return AbsenceVerdict(
                symbol,
                day,
                AbsenceClass.UNKNOWN,
                (("reason", "the symbol has never been observed in any collected file"),),
            )

        if next_seen is not None:
            return self._classify_returning_symbol(symbol, day, context, last_seen, next_seen)

        assert last_seen is not None
        candidates = context.rename_candidates.get(symbol, ())
        if len(candidates) == 1:
            return AbsenceVerdict(
                symbol,
                day,
                AbsenceClass.RENAMED,
                (
                    ("successor", candidates[0]),
                    ("shared_isin", "yes"),
                    ("last_seen", last_seen.isoformat()),
                ),
            )
        if len(candidates) > 1:
            # An ISIN shared by several symbols is a data-quality artefact, not a
            # rename. Review reproduced the old behaviour: `fetchone()` picked one
            # arbitrarily and discarded the rest without disclosing the ambiguity.
            return AbsenceVerdict(
                symbol,
                day,
                AbsenceClass.UNKNOWN,
                (
                    ("shared_isin_with", ", ".join(candidates)),
                    (
                        "reason",
                        "several symbols share this ISIN, so a rename cannot be "
                        "identified without disambiguating the duplicate",
                    ),
                ),
            )

        truncation = context.truncation_at_last_seen.get(symbol)
        if truncation is not None and truncation.symbol == symbol and truncation.is_exit_warning:
            return AbsenceVerdict(
                symbol,
                day,
                AbsenceClass.EXITED_DERIVATIVES,
                (
                    ("last_seen", last_seen.isoformat()),
                    ("ladder_depth", str(truncation.observed_depth)),
                    ("cross_sectional_norm", str(truncation.cross_sectional_norm)),
                    ("ladder_direction", str(truncation.direction)),
                    (
                        "reason",
                        "expiry ladder was short against its own date's norm and not "
                        "advancing, then ran off without reappearing",
                    ),
                ),
            )

        return AbsenceVerdict(
            symbol,
            day,
            AbsenceClass.UNKNOWN,
            (
                ("last_seen", last_seen.isoformat()),
                (
                    "reason",
                    "absent with a full or advancing ladder, no successor ISIN and no "
                    "later appearance; exit and end-of-collection are indistinguishable",
                ),
            ),
        )

    @staticmethod
    def _classify_returning_symbol(
        symbol: str,
        day: date,
        context: _DateContext,
        last_seen: date | None,
        next_seen: date,
    ) -> AbsenceVerdict:
        """A symbol that comes back was not gone — unless the gap is extraordinary."""
        index = {stamp: position for position, stamp in enumerate(context.collected)}
        ceiling = context.ordinary_gap_ceiling
        if last_seen is not None and ceiling is not None:
            sessions_absent = index[next_seen] - index[last_seen] - 1
            if sessions_absent > ceiling:
                return AbsenceVerdict(
                    symbol,
                    day,
                    AbsenceClass.UNKNOWN,
                    (
                        ("sessions_absent", str(sessions_absent)),
                        ("ordinary_gap_ceiling", str(ceiling)),
                        ("returns_on", next_seen.isoformat()),
                        (
                            "reason",
                            "the absence is far longer than this universe's ordinary "
                            "gaps, so illiquidity and a delist-then-relist cannot be "
                            "told apart",
                        ),
                    ),
                )
        return AbsenceVerdict(
            symbol,
            day,
            AbsenceClass.NOT_TRADED,
            (
                ("observed_after", next_seen.isoformat()),
                ("reason", "present on a later collected date, so it was still listed"),
            ),
        )

    # -------------------------------------------------------------- snapshot

    def snapshot_as_of(
        self, trade_date: date, *, known_as_of: date | None = None
    ) -> UniverseSnapshot:
        """The frozen universe for ``trade_date`` as believed at ``known_as_of``.

        Every symbol ever seen before this date and absent from it is classified —
        not merely those absent since the previous collected date. Review
        reproduced the narrower version: a symbol missing for two consecutive dates
        vanished from ``absences`` after the first, and ``is_fully_resolved``
        reported ``True`` while its fate was entirely unaccounted for.

        Raises:
            UniverseSnapshotError: ``trade_date`` was not collected.
        """
        context = self._build_context(trade_date, known_as_of)
        self._require_collected(trade_date, context.collected)

        departed = sorted(set(context.last_seen) - context.members)
        return UniverseSnapshot(
            trade_date=trade_date,
            known_as_of=known_as_of or trade_date,
            members=context.members,
            absences=tuple(self._classify(symbol, trade_date, context) for symbol in departed),
            ladder_norm=self.ladder_norm_on(trade_date, known_as_of=known_as_of),
            truncations=self.ladder_truncations_on(trade_date, known_as_of=known_as_of),
        )
