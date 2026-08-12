"""`L0.31` — the point-in-time market-rule store.

Spec: `docs/research/215`. Dated facts: `docs/research/61` §2.

**The defect this exists to prevent does not raise.** A replay of 2019 that applies 2026's
STT rate returns a plausible number and a wrong conclusion. So the central design choice is
that this store would rather refuse than substitute: a date outside what has actually been
compiled raises `RuleCoverageError`, and `research/61` establishes that most of Indian
market history is outside it — pre-2003 tick sizes, pre-2020 stamp duty, the 2004-2013
options-STT path, and the entire pre-Oct-2024 exchange-charge slab schedule are all
documented, permanent gaps.

**Two time axes, as everywhere else in this project.** *Effective* time is when the rule was
in force at the exchange; *recorded* time is when this project learned it. `known_as_of`
replays a past belief, so a study can be re-run against the facts it actually had and the
difference attributed to the correction rather than to the strategy.

**Why constants are legal here and nowhere else.** `R.03` bans hardcoded values; `R.23(e)`
exempts a "physical or regulatory fact, sourced in a comment". An STT rate is not
derivable, calibratable or tunable — it is what the Finance Act says. The exemption is
carried by the CITATION, not by the type, so a record without a source reference is
rejected at construction. That check is the whole difference between a regulatory fact and
the constant `R.03` forbids.

**The solver.** Facts arrive out of order, at different evidence grades, over months, and
they contradict each other. `timeline()` reconciles them into non-overlapping intervals by
splitting at every boundary and choosing a winner per segment; `conflicts()` reports what
lost. Nothing is silently dropped, and a genuine tie is returned UNRESOLVED rather than
decided by an arbitrary rule the store invented.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Protocol

_WEEKDAY_NUMBER_BY_NAME = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}
"""Python's own `date.weekday()` convention, so a caller never has to translate."""

_BOTH_INTERVALS_CLOSED = 2
"""Two closed ends means the overlap ends at the earlier of them; one means it runs on."""


class MarketRuleError(Exception):
    """Base for every refusal this store makes."""


class RuleCitationError(MarketRuleError):
    """A rule arrived without provenance.

    Raised at construction rather than at query time: a rate with no source is the
    hardcoded constant `R.03` bans, and letting it into the store means it is already in
    somebody's backtest before anyone notices.
    """


class RuleIntervalError(MarketRuleError):
    """The validity interval is empty or inverted."""


class RuleCoverageError(MarketRuleError):
    """No compiled fact covers this date.

    The most important error in this module. Returning the nearest known rule instead
    would answer every historical question with today's regime, plausibly and wrongly.
    """


class RuleFamily(StrEnum):
    """The sixteen rule families of `research/61` §2.8 — the full space, not a sample.

    Enumerated as a closed set so `coverage()` can report the ones nobody has compiled
    yet. A family that is simply absent from a report reads as "nothing to worry about".
    """

    EXPIRY_CYCLE = "expiry_cycle"
    LOT_SIZE = "lot_size"
    MINIMUM_CONTRACT_VALUE = "minimum_contract_value"
    TICK_SIZE = "tick_size"
    MARKET_WIDE_CIRCUIT_BREAKER = "market_wide_circuit_breaker"
    PER_STOCK_PRICE_BAND = "per_stock_price_band"
    DYNAMIC_PRICE_BAND = "dynamic_price_band"
    SECURITIES_TRANSACTION_TAX = "securities_transaction_tax"
    COMMODITIES_TRANSACTION_TAX = "commodities_transaction_tax"
    STAMP_DUTY = "stamp_duty"
    EXCHANGE_TRANSACTION_CHARGE = "exchange_transaction_charge"
    INVESTOR_PROTECTION_FUND_CONTRIBUTION = "investor_protection_fund_contribution"
    SEBI_TURNOVER_FEE = "sebi_turnover_fee"
    GOODS_AND_SERVICES_TAX = "goods_and_services_tax"
    DEPOSITORY_PARTICIPANT_CHARGE = "depository_participant_charge"
    PRE_OPEN_AUCTION = "pre_open_auction"
    SESSION_HOURS = "session_hours"
    SPAN_MARGIN = "span_margin"
    VALUE_AT_RISK_MARGIN = "value_at_risk_margin"
    PEAK_MARGIN = "peak_margin"
    PENALTY_FRAMEWORK = "penalty_framework"


class EvidenceGrade(StrEnum):
    """How well a fact is known. Ranked by `_GRADE_RANK`, not by declaration order."""

    OBSERVED_FROM_EXCHANGE_DATA = "observed_from_exchange_data"
    PRIMARY_CIRCULAR = "primary_circular"
    SECONDARY_TRIANGULATED = "secondary_triangulated"
    UNVERIFIED_SNIPPET = "unverified_snippet"


_GRADE_RANK: Mapping[EvidenceGrade, int] = {
    EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA: 3,
    EvidenceGrade.PRIMARY_CIRCULAR: 2,
    EvidenceGrade.SECONDARY_TRIANGULATED: 1,
    EvidenceGrade.UNVERIFIED_SNIPPET: 0,
}
"""**Observed outranks documentary, and that is deliberate.** A circular says what the rule
was announced to be; the exchange's own published instrument master says what was actually
in force that day. Where the two disagree the market traded on the second one."""


def evidence_grade_rank(grade: EvidenceGrade) -> int:
    """How much a grade is worth, for callers that must combine several.

    Public because a consumer assembling one answer out of several facts — a cost built from
    six levies, say — can only be as trustworthy as its worst-sourced input, and there is no
    way to say that without ordering the grades.
    """
    return _GRADE_RANK[grade]


class RuleValueKind(StrEnum):
    """What a rule's value means, so a caller cannot read a weekday as a rate."""

    DECIMAL_FRACTION = "decimal_fraction"
    INTEGER = "integer"
    WEEKDAY = "weekday"
    TIME_RANGE = "time_range"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class RuleScope:
    """Who a rule applies to. All-`None` means everyone.

    Specificity is the count of pinned fields, and a more specific rule wins outright
    rather than competing on evidence: a BANKNIFTY expiry rule is not a rival claim about
    every index's expiry, it is a statement about a different subject.
    """

    segment: str | None = None
    index_or_underlying: str | None = None
    symbol: str | None = None

    @property
    def specificity(self) -> int:
        return sum(
            1
            for value in (self.segment, self.index_or_underlying, self.symbol)
            if value is not None
        )

    def covers(self, query: RuleScope) -> bool:
        """True when this rule speaks to `query` — every pinned field must match exactly."""
        return all(
            mine is None or mine == theirs
            for mine, theirs in (
                (self.segment, query.segment),
                (self.index_or_underlying, query.index_or_underlying),
                (self.symbol, query.symbol),
            )
        )


EVERYTHING = RuleScope()
"""The unscoped rule — applies wherever nothing more specific does."""


@dataclass(frozen=True, slots=True)
class MarketRuleRecord:
    """One dated rule fact, with the provenance that makes it admissible.

    `effective_to` is EXCLUSIVE and `None` means still in force. Half-open because rule
    changes are announced as "with effect from" a date: the new rule's first day is the old
    rule's last day plus one, and modelling it closed forces every consumer to subtract a
    day and half of them to forget.
    """

    family: RuleFamily
    scope: RuleScope
    value: str
    value_kind: RuleValueKind
    effective_from: date
    effective_to: date | None
    source_reference: str
    source_date: date
    grade: EvidenceGrade
    recorded_at: date

    def __post_init__(self) -> None:
        if not self.source_reference.strip():
            raise RuleCitationError(
                f"{self.family} rule effective {self.effective_from} has an empty "
                f"source_reference — an uncited rate is the hardcoded constant R.03 bans, "
                f"not the regulatory-fact exemption in R.23(e)"
            )
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise RuleIntervalError(
                f"{self.family} interval [{self.effective_from}, {self.effective_to}) is "
                f"empty or inverted; effective_to is exclusive, so it must be strictly later"
            )

    def covers_date(self, as_of: date) -> bool:
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of < self.effective_to


@dataclass(frozen=True, slots=True)
class RuleConflict:
    """Two facts claiming different values over the same window."""

    family: RuleFamily
    scope: RuleScope
    overlap_start: date
    overlap_end: date | None
    records: tuple[MarketRuleRecord, ...]
    resolved_by: str


@dataclass(frozen=True, slots=True)
class RuleResolution:
    """The answer, with everything a caller needs to judge how much to trust it."""

    family: RuleFamily
    scope: RuleScope
    as_of: date
    record: MarketRuleRecord
    superseded_records: tuple[MarketRuleRecord, ...]
    is_unresolved: bool

    @property
    def value(self) -> str:
        return self.record.value

    @property
    def grade(self) -> EvidenceGrade:
        return self.record.grade

    @property
    def is_conflicted(self) -> bool:
        return bool(self.superseded_records)

    def _require(self, kind: RuleValueKind) -> str:
        if self.record.value_kind is not kind:
            raise ValueError(
                f"{self.family} value {self.record.value!r} is a "
                f"{self.record.value_kind.name}, not a {kind.name} — reading it as one "
                f"would produce a number, and numbers get used"
            )
        return self.record.value

    def as_decimal(self) -> Decimal:
        """Exact. A rate is multiplied by every trade, and 0.0625 is not a float."""
        return Decimal(self._require(RuleValueKind.DECIMAL_FRACTION))

    def as_integer(self) -> int:
        return int(self._require(RuleValueKind.INTEGER))

    def as_weekday(self) -> int:
        """Python's convention: Monday is 0."""
        return _WEEKDAY_NUMBER_BY_NAME[self._require(RuleValueKind.WEEKDAY).upper()]


@dataclass(frozen=True, slots=True)
class FamilyCoverage:
    """What this store can and cannot answer for one family — measured, for `R.08`."""

    family: RuleFamily
    earliest: date | None
    latest: date | None
    is_open_ended: bool
    holes: tuple[tuple[date, date], ...]
    record_count: int
    grade_counts: Mapping[EvidenceGrade, int]
    observed_window: tuple[date, date] | None = None
    observed_by: str = ""

    @property
    def has_any_source(self) -> bool:
        """Compiled facts OR a live observational source counts as coverage.

        Without this a family answered entirely by observation reads as uncovered on the
        dashboard, which is the opposite of true — and it is the exact failure mode the
        coverage report exists to prevent, just pointed the other way.
        """
        return self.record_count > 0 or self.observed_window is not None


def _sort_key(record: MarketRuleRecord) -> tuple[int, int, date, str]:
    """Winner ordering, strongest last, and every component is a stated policy.

    Specificity outranks grade because a narrower rule is about a different subject, not a
    better-sourced claim about the same one. Grade then decides genuine rivals, and a later
    compilation supersedes an equal-grade earlier one. `source_reference` is the final
    tiebreak purely so the choice is DETERMINISTIC — it carries no authority, which is why
    a tie that reaches it is reported unresolved rather than treated as decided.
    """
    return (
        record.scope.specificity,
        _GRADE_RANK[record.grade],
        record.recorded_at,
        record.source_reference,
    )


class ObservationalRuleSource(Protocol):
    """A source that DERIVES rule facts from data the exchange actually published.

    Registered rather than imported in bulk, and consulted lazily per (family, scope).
    The reason is size: the instrument master holds 227,535 dated rows across ~105,000
    symbols, and materialising a record per symbol per family would put hundreds of
    thousands of objects in a list that every query then scans. A source is asked only
    about the symbol being resolved, which is one indexed lookup.
    """

    def families(self) -> frozenset[RuleFamily]:
        """Which families this source can speak to at all."""
        ...

    def records_for(self, family: RuleFamily, scope: RuleScope) -> tuple[MarketRuleRecord, ...]:
        """Every fact this source can derive for one family and scope."""
        ...

    def observation_window(self) -> tuple[date, date] | None:
        """The dates this source observed, for the coverage report. None when it saw none."""
        ...

    def describe(self) -> str:
        """One line naming the source, for provenance in the coverage report."""
        ...


@dataclass
class PointInTimeMarketRuleStore:
    """Compiled rule facts, and the reconciliation that makes them answerable."""

    _records: list[MarketRuleRecord] = field(default_factory=list)
    _sources: list[ObservationalRuleSource] = field(default_factory=list)

    def register_source(self, source: ObservationalRuleSource) -> None:
        """Add a derived-fact source, consulted lazily on every matching query."""
        self._sources.append(source)

    def add(self, record: MarketRuleRecord) -> None:
        """Facts arrive out of order and over months; nothing here depends on the order."""
        self._records.append(record)

    def extend(self, records: Iterable[MarketRuleRecord]) -> None:
        for record in records:
            self.add(record)

    def __len__(self) -> int:
        return len(self._records)

    # -- selection --------------------------------------------------------------

    def _applicable(
        self, family: RuleFamily, scope: RuleScope, known_as_of: date | None
    ) -> list[MarketRuleRecord]:
        candidates = list(self._records)
        for source in self._sources:
            if family in source.families():
                candidates.extend(source.records_for(family, scope))
        return [
            record
            for record in candidates
            if record.family is family
            and record.scope.covers(scope)
            and (known_as_of is None or record.recorded_at <= known_as_of)
        ]

    def resolve(
        self,
        family: RuleFamily,
        as_of: date,
        *,
        scope: RuleScope = EVERYTHING,
        known_as_of: date | None = None,
    ) -> RuleResolution:
        """The rule in force on `as_of`, or a refusal that names the date.

        `known_as_of` replays a past belief by hiding facts recorded after it.
        """
        applicable = self._applicable(family, scope, known_as_of)
        if not applicable:
            raise RuleCoverageError(
                f"no facts compiled for {family} at scope {scope} "
                f"{'as known on ' + known_as_of.isoformat() if known_as_of else ''}".strip()
            )

        covering = [record for record in applicable if record.covers_date(as_of)]
        if not covering:
            known = sorted(record.effective_from for record in applicable)
            raise RuleCoverageError(
                f"{family} is not covered on {as_of.isoformat()} — compiled intervals begin "
                f"{known[0].isoformat()} and leave this date outside them. Returning the "
                f"nearest known rule would answer with the wrong era, silently"
            )

        best_specificity = max(record.scope.specificity for record in covering)
        rivals = [record for record in covering if record.scope.specificity == best_specificity]
        ranked = sorted(rivals, key=_sort_key, reverse=True)
        winner = ranked[0]
        superseded = tuple(record for record in ranked[1:] if record.value != winner.value)

        is_unresolved = any(
            _sort_key(record)[:3] == _sort_key(winner)[:3] and record.value != winner.value
            for record in ranked[1:]
        )
        return RuleResolution(
            family=family,
            scope=scope,
            as_of=as_of,
            record=winner,
            superseded_records=superseded,
            is_unresolved=is_unresolved,
        )

    # -- reconciliation ---------------------------------------------------------

    def timeline(
        self,
        family: RuleFamily,
        *,
        scope: RuleScope = EVERYTHING,
        known_as_of: date | None = None,
    ) -> tuple[MarketRuleRecord, ...]:
        """One non-overlapping, ordered series of what was in force when.

        Built by splitting at every interval boundary and resolving each segment, then
        merging neighbours that resolve to the same fact. This is what makes the answer
        independent of the order the facts were compiled in, which matters because they are
        compiled over months from PDFs read in whatever sequence they were found.
        """
        applicable = self._applicable(family, scope, known_as_of)
        if not applicable:
            return ()

        boundaries = sorted(
            {record.effective_from for record in applicable}
            | {record.effective_to for record in applicable if record.effective_to is not None}
        )
        segments: list[MarketRuleRecord] = []
        for index, segment_start in enumerate(boundaries):
            segment_end = boundaries[index + 1] if index + 1 < len(boundaries) else None
            covering = [record for record in applicable if record.covers_date(segment_start)]
            if not covering:
                continue  # a hole: reported by `coverage()`, never bridged here
            best_specificity = max(record.scope.specificity for record in covering)
            winner = max(
                (r for r in covering if r.scope.specificity == best_specificity), key=_sort_key
            )
            clipped = MarketRuleRecord(
                family=winner.family,
                scope=winner.scope,
                value=winner.value,
                value_kind=winner.value_kind,
                effective_from=segment_start,
                effective_to=segment_end,
                source_reference=winner.source_reference,
                source_date=winner.source_date,
                grade=winner.grade,
                recorded_at=winner.recorded_at,
            )
            if (
                segments
                and segments[-1].value == clipped.value
                and segments[-1].source_reference == clipped.source_reference
                and segments[-1].effective_to == clipped.effective_from
            ):
                merged = segments.pop()
                clipped = MarketRuleRecord(
                    family=merged.family,
                    scope=merged.scope,
                    value=merged.value,
                    value_kind=merged.value_kind,
                    effective_from=merged.effective_from,
                    effective_to=segment_end,
                    source_reference=merged.source_reference,
                    source_date=merged.source_date,
                    grade=merged.grade,
                    recorded_at=merged.recorded_at,
                )
            segments.append(clipped)
        return tuple(segments)

    def conflicts(self) -> tuple[RuleConflict, ...]:
        """Every pair of same-scope facts claiming different values over a shared window.

        Reported rather than resolved away: a contradiction between two sources is a fact
        about the compilation, and hiding it means the next person re-derives it.
        """
        found: list[RuleConflict] = []
        for index, first in enumerate(self._records):
            for second in self._records[index + 1 :]:
                if first.family is not second.family or first.scope != second.scope:
                    continue
                if first.value == second.value:
                    continue
                overlap_start = max(first.effective_from, second.effective_from)
                ends = [end for end in (first.effective_to, second.effective_to) if end is not None]
                overlap_end = (
                    min(ends)
                    if len(ends) == _BOTH_INTERVALS_CLOSED
                    else (ends[0] if ends else None)
                )
                if overlap_end is not None and overlap_end <= overlap_start:
                    continue
                ranked = sorted((first, second), key=_sort_key, reverse=True)
                resolved_by = (
                    "unresolved tie"
                    if _sort_key(ranked[0])[:3] == _sort_key(ranked[1])[:3]
                    else f"{ranked[0].grade} recorded {ranked[0].recorded_at.isoformat()}"
                )
                found.append(
                    RuleConflict(
                        family=first.family,
                        scope=first.scope,
                        overlap_start=overlap_start,
                        overlap_end=overlap_end,
                        records=(first, second),
                        resolved_by=resolved_by,
                    )
                )
        return tuple(found)

    # -- coverage ---------------------------------------------------------------

    def coverage(self) -> Mapping[RuleFamily, FamilyCoverage]:
        """Every family, including the ones nobody has compiled yet.

        An absent row on a dashboard reads as "fine". An uncovered family must read as
        uncovered, which is why this iterates the enum rather than the records.
        """
        return {family: self._coverage_for(family) for family in RuleFamily}

    def _observation_for(self, family: RuleFamily) -> tuple[tuple[date, date] | None, str]:
        """The widest window any registered source observed for this family."""
        windows: list[tuple[date, date]] = []
        describers: list[str] = []
        for source in self._sources:
            if family not in source.families():
                continue
            window = source.observation_window()
            if window is not None:
                windows.append(window)
                describers.append(source.describe())
        if not windows:
            return None, ""
        return (
            (min(start for start, _ in windows), max(end for _, end in windows)),
            "; ".join(describers),
        )

    def _coverage_for(self, family: RuleFamily) -> FamilyCoverage:
        records = [record for record in self._records if record.family is family]
        observed_window, observed_by = self._observation_for(family)
        if not records:
            return FamilyCoverage(
                family=family,
                earliest=observed_window[0] if observed_window else None,
                latest=observed_window[1] if observed_window else None,
                is_open_ended=observed_window is not None,
                holes=(),
                record_count=0,
                grade_counts={},
                observed_window=observed_window,
                observed_by=observed_by,
            )
        # Sorted with the open-ended intervals LAST within a start date. Sorting the raw
        # tuples raises `TypeError: '<' not supported between 'date' and 'NoneType'` the
        # moment two records share an `effective_from` and either is still in force — which
        # is the normal shape of a rate that applies to several scopes from one circular, and
        # was simply absent from the seeded data until `L1.01` added it.
        intervals = sorted(
            ((record.effective_from, record.effective_to) for record in records),
            key=lambda interval: (interval[0], interval[1] is None, interval[1] or interval[0]),
        )
        merged: list[tuple[date, date | None]] = []
        for start, end in intervals:
            if not merged:
                merged.append((start, end))
                continue
            previous_start, previous_end = merged[-1]
            if previous_end is None:
                # Still in force, and the sort guarantees `start` is not earlier, so this
                # interval is already inside it. Appending would leave `merged` unmerged and
                # make `latest` disagree with `is_open_ended`.
                continue
            if start <= previous_end:
                merged[-1] = (previous_start, None if end is None else max(previous_end, end))
            else:
                merged.append((start, end))
        holes = tuple(
            (earlier[1], later[0])
            for earlier, later in pairwise(merged)
            if earlier[1] is not None and earlier[1] < later[0]
        )
        latest_ends = [end for _, end in merged if end is not None]
        return FamilyCoverage(
            family=family,
            earliest=min(merged[0][0], observed_window[0]) if observed_window else merged[0][0],
            latest=max(latest_ends) if latest_ends else None,
            is_open_ended=any(end is None for _, end in merged) or observed_window is not None,
            holes=holes,
            record_count=len(records),
            grade_counts=Counter(record.grade for record in records),
            observed_window=observed_window,
            observed_by=observed_by,
        )

    def families_with_facts(self) -> tuple[RuleFamily, ...]:
        return tuple(sorted({record.family for record in self._records}))

    def records(self) -> Sequence[MarketRuleRecord]:
        return tuple(self._records)
