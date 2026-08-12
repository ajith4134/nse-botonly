"""`L0.11` — makes look-ahead leakage structurally impossible, not merely discouraged.

A replay at virtual time T must see what a trader could have seen at T, and nothing else.
One leaked future row turns honest replay into cheating, and every lesson learnt from it
is worthless — silently, because a leaked backtest looks *better*, not broken.

**Why this is a rebuild rather than the archived firewall restored.** The pre-reset version
guarded a single `timestamp` field. That is the wrong model for this project, and the
project's own data says so: measured across 533,920 real ingested rows, **90.6% were first
observed more than a day after the event they describe** (F&O bhavcopy median 22 days, max
2,413). A guard on the event date alone admits every one of those as though it had been
available on the day it describes.

**But the obvious fix is also wrong, and that matters more.** `observed_at` is when *we
fetched* a row, not when it became public. Guarding on it would block all legitimate
backfill — a 2020 bhavcopy fetched in 2026 is still a faithful record of what NSE published
in 2020. So the firewall guards on when a row became **knowable**:

    knowable_from = effective_date + the source's publication lag

and the lag is **derived, never declared** (`R.03`): the minimum lag ever observed for that
source is a lower bound on how fast it becomes available. Measured here: bhavcopy 1 day,
ATM IV and circuit bands 0, bulk/block deals and MWPL 1.

**Where that derivation breaks, it says so.** `delisted_securities_master` yields a minimum
lag of 2,101 days, because it is a static historical master this project has never observed
close to its events. A minimum-observed lag is only a publication lag if the source has
been seen fresh at least once. When it has not, the lag is `UNKNOWN` and rows from that
source are BLOCKED by default rather than admitted under a fabricated 2,101-day schedule.
Refusing to guess is the whole point of the component.

**Nothing is dropped silently.** The archived version `continue`d past out-of-window data,
which is a silent skip (`R.11`). Every block here is counted by reason and reportable, so
a replay that quietly sees nothing is distinguishable from one that legitimately had
nothing to see.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

UNKNOWN_LAG_SENTINEL = None
"""A source whose publication schedule has never been observed. Distinct from a lag of
zero, and treated as a refusal rather than a permissive default."""

MAXIMUM_CREDIBLE_PUBLICATION_LAG_DAYS = 30
"""Beyond this, a 'minimum observed lag' is evidence the source has never been seen fresh
rather than evidence of a slow publisher. Derived from the corpus, not chosen: every
source observed on its own schedule lands at 0-1 days, while the one static historical
master sits at 2,101. Any threshold inside that gap separates them identically, so the
value is not load-bearing — it is a gap detector, and the gap is three orders wide."""


class LeakageReason(Enum):
    """Why a row was refused. Counted, never silently swallowed."""

    EVENT_IN_THE_FUTURE = "event date is after virtual now"
    NOT_YET_PUBLISHED = "event had not been published by virtual now"
    PUBLICATION_SCHEDULE_UNKNOWN = "source has never been observed fresh"
    REVISED_AFTER_DECISION = "this value was only known from a later observation"


class CausalLeakageError(RuntimeError):
    """Base for every refusal this firewall makes."""


class FutureLeakageError(CausalLeakageError):
    """A row that could not have been known at virtual-now was observed."""

    def __init__(self, reason: LeakageReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


class CausalOrderingError(CausalLeakageError):
    """The virtual clock was asked to move backward, re-revealing the past out of order."""


@dataclass(frozen=True)
class SourcePublicationLag:
    """How long after an event this source's record becomes knowable.

    `days` is None when the schedule could not be derived — an explicit absence of
    knowledge, never a permissive default.
    """

    source_name: str
    days: int | None
    observations_used: int

    @property
    def is_derivable(self) -> bool:
        return self.days is not None

    def knowable_from(self, effective_date: date) -> date | None:
        if self.days is None:
            return None
        return effective_date + timedelta(days=self.days)


def derive_publication_lags(
    observations: Iterable[tuple[str, date, datetime]],
) -> dict[str, SourcePublicationLag]:
    """Per-source publication lag, from `(source, effective_date, observed_at)` triples.

    The MINIMUM lag is used, not the median: backfill inflates every other statistic, and
    the fastest a row was ever seen is the only honest lower bound on when it could have
    been seen. A minimum beyond `MAXIMUM_CREDIBLE_PUBLICATION_LAG_DAYS` means the source
    has never been observed fresh, so no schedule is claimed.
    """
    minimum_lag: dict[str, int] = {}
    counts: dict[str, int] = {}
    for source_name, effective_date, observed_at in observations:
        lag_days = (observed_at.date() - effective_date).days
        counts[source_name] = counts.get(source_name, 0) + 1
        current = minimum_lag.get(source_name)
        if current is None or lag_days < current:
            minimum_lag[source_name] = lag_days

    lags: dict[str, SourcePublicationLag] = {}
    for source_name, lag_days in minimum_lag.items():
        credible = 0 <= lag_days <= MAXIMUM_CREDIBLE_PUBLICATION_LAG_DAYS
        lags[source_name] = SourcePublicationLag(
            source_name=source_name,
            days=lag_days if credible else None,
            observations_used=counts[source_name],
        )
    return lags


@dataclass(frozen=True)
class ObservableRow:
    """The minimum a row must state about itself to be judged.

    Deliberately not the ingest store's row type: the firewall guards depth packets, bars
    and news records too, and coupling it to one store would leave the others unguarded.
    """

    source_name: str
    effective_date: date
    observed_at: datetime


@dataclass
class LeakageLedger:
    """What the firewall admitted and refused, by reason."""

    admitted: int = 0
    blocked: dict[LeakageReason, int] = field(default_factory=dict)

    def record_block(self, reason: LeakageReason) -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1

    @property
    def total_blocked(self) -> int:
        return sum(self.blocked.values())

    def describe(self) -> str:
        if not self.blocked:
            return f"{self.admitted:,} admitted, nothing blocked"
        reasons = ", ".join(
            f"{reason.value}={count:,}"
            for reason, count in sorted(self.blocked.items(), key=lambda item: -item[1])
        )
        return f"{self.admitted:,} admitted, {self.total_blocked:,} blocked ({reasons})"


class CausalLeakageFirewall:
    """A virtual clock plus the refusals that keep replay honest.

    Carries state deliberately: the clock only moves forward, and the ledger accumulates
    across a session so a replay can be audited afterwards rather than trusted.
    """

    def __init__(
        self,
        session_open: datetime,
        session_close: datetime,
        publication_lags: dict[str, SourcePublicationLag],
        *,
        block_unknown_sources: bool = True,
    ) -> None:
        if session_close < session_open:
            raise ValueError(f"session_close {session_close} precedes session_open {session_open}")
        self._session_open = session_open
        self._session_close = session_close
        self._virtual_now = session_open
        self._publication_lags = publication_lags
        # Default CLOSED. A source whose schedule is unknown is the case most likely to
        # leak, so admitting it by default would put the failure mode behind the flag.
        self._block_unknown_sources = block_unknown_sources
        self.ledger = LeakageLedger()

    @property
    def virtual_now(self) -> datetime:
        return self._virtual_now

    def advance_to(self, moment: datetime) -> None:
        """Move the clock forward. Never rewinds; clamps at the session close."""
        if moment < self._virtual_now:
            raise CausalOrderingError(
                f"virtual clock cannot rewind from {self._virtual_now} to {moment}"
            )
        self._virtual_now = min(moment, self._session_close)

    def refusal_for(self, row: ObservableRow) -> LeakageReason | None:
        """Why this row may not be observed at virtual-now, or None if it may."""
        virtual_date = self._virtual_now.date()

        if row.effective_date > virtual_date:
            return LeakageReason.EVENT_IN_THE_FUTURE

        lag = self._publication_lags.get(row.source_name)
        if lag is None or not lag.is_derivable:
            return (
                LeakageReason.PUBLICATION_SCHEDULE_UNKNOWN if self._block_unknown_sources else None
            )

        knowable_from = lag.knowable_from(row.effective_date)
        if knowable_from is not None and knowable_from > virtual_date:
            return LeakageReason.NOT_YET_PUBLISHED

        return None

    def assert_observable(self, row: ObservableRow) -> None:
        """Guard a direct read. Raises rather than returning a value that leaked."""
        reason = self.refusal_for(row)
        if reason is not None:
            self.ledger.record_block(reason)
            raise FutureLeakageError(
                reason,
                f"{row.source_name} effective {row.effective_date} "
                f"at virtual-now {self._virtual_now}",
            )
        self.ledger.admitted += 1

    def filter_observable(self, rows: Iterable[ObservableRow]) -> Iterator[ObservableRow]:
        """Yield only rows knowable at virtual-now, COUNTING every refusal.

        Does not raise: a replay legitimately encounters rows it may not yet see, and the
        distinction between 'blocked' and 'absent' is preserved in the ledger rather than
        thrown away by a bare `continue`.
        """
        for row in rows:
            reason = self.refusal_for(row)
            if reason is not None:
                self.ledger.record_block(reason)
                continue
            self.ledger.admitted += 1
            yield row

    def stream_causally(self, chronological: Iterable[ObservableRow]) -> Iterator[ObservableRow]:
        """Release rows in order, advancing the clock to each row as it is revealed.

        At the instant anything is observed, virtual-now equals the moment that row became
        knowable — so no later row has been seen. Out-of-order input raises, because a
        stream that is not chronological cannot be replayed honestly at all.
        """
        for row in chronological:
            lag = self._publication_lags.get(row.source_name)
            knowable_from = lag.knowable_from(row.effective_date) if lag else None
            if knowable_from is None:
                self.ledger.record_block(LeakageReason.PUBLICATION_SCHEDULE_UNKNOWN)
                continue
            reveal_at = datetime.combine(
                knowable_from, self._session_open.time(), tzinfo=self._session_open.tzinfo
            )
            if reveal_at < self._session_open or reveal_at > self._session_close:
                self.ledger.record_block(LeakageReason.NOT_YET_PUBLISHED)
                continue
            self.advance_to(reveal_at)
            self.ledger.admitted += 1
            yield row
