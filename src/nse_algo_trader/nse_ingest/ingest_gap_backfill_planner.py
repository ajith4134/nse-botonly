"""`L0.10` — which dates are missing, WHY, and which of them are worth fetching again.

A list of dates a source lacks is nearly useless on its own, because the three reasons a
date can be absent demand opposite responses:

- **Never attempted.** We simply have not asked. Worth fetching, and the only class that
  represents work outstanding.
- **Attempted, genuinely absent.** We asked and the archive answered `404`. For a
  trading calendar this is usually an exchange holiday that the calendar disagrees
  about, or a file NSE never published. Retrying it every night forever is pure waste,
  and worse, it makes a real outage indistinguishable from routine noise in the logs.
- **Attempted, blocked or failed.** We asked and were bot-walled, timed out, or served a
  file for the wrong date. The data probably exists; we could not get it. This is the
  class that must keep being retried, and the class that must be surfaced to a human if
  it persists — an outage nobody notices is an outage that silently truncates history.

Collapsing these into "missing" produces a backfill that hammers holidays and gives up
on outages. So this planner reads the fetch history the store already records — which is
why failed fetches are written down and not only successes — and classifies before it
plans.

**Backfilled rows are marked by construction, not by a flag.** The store is bitemporal,
so a row fetched months after its effective date already carries that distance in
`observed_at`. `BackfillProvenance` measures it rather than adding a boolean somebody
would forget to set: a row with a large lag was not knowable when it was dated, and a
point-in-time reader filtering on `observed_at` excludes it automatically.

Same shape as the absence classifier at `1.5a`, applied one layer down: `UNKNOWN` is a
first-class answer and evidence travels with every verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.ingest_source_adapter import NseIngestSourceAdapter
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchStatus
from nse_algo_trader.nse_ingest.nse_source_ingest_runner import (
    NseSourceIngestRunner,
    SourceIngestRun,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

_ESTABLISHED_ABSENCE_STATUSES = frozenset({FetchStatus.NOT_FOUND.value})
_RECOVERABLE_FAILURE_STATUSES = frozenset(
    {
        FetchStatus.BOT_BLOCKED.value,
        FetchStatus.TRANSPORT_FAILURE.value,
        FetchStatus.CONTENT_MISMATCH.value,
        FetchStatus.JAVASCRIPT_SHELL.value,
        FetchStatus.EMPTY_PAYLOAD.value,
    }
)


class MissingDateReason(Enum):
    """Why a trading session has no rows — the distinction the plan depends on."""

    NEVER_ATTEMPTED = "never_attempted"
    ESTABLISHED_ABSENT = "established_absent"
    RECOVERABLE_FAILURE = "recoverable_failure"


@dataclass(frozen=True)
class MissingDate:
    """One absent session, its reason, and the evidence for that reason."""

    effective_date: date
    reason: MissingDateReason
    attempts: int
    last_evidence: str

    @property
    def is_worth_refetching(self) -> bool:
        """Established absences are not retried; everything else is.

        An archive that answered 404 will answer 404 again. Retrying it nightly buries
        the failures that CAN be fixed under noise that cannot.
        """
        return self.reason is not MissingDateReason.ESTABLISHED_ABSENT


@dataclass(frozen=True)
class SourceGapReport:
    """Every session a source lacks in a window, classified."""

    source_name: str
    window_start: date
    window_end: date
    trading_sessions: int
    dates_present: int
    missing: tuple[MissingDate, ...]

    @property
    def refetchable(self) -> tuple[MissingDate, ...]:
        return tuple(entry for entry in self.missing if entry.is_worth_refetching)

    def by_reason(self, reason: MissingDateReason) -> tuple[MissingDate, ...]:
        return tuple(entry for entry in self.missing if entry.reason is reason)

    def describe(self) -> str:
        counts = {
            reason.value: len(self.by_reason(reason)) for reason in MissingDateReason
        }
        return (
            f"{self.source_name} {self.window_start}..{self.window_end}: "
            f"{self.dates_present}/{self.trading_sessions} present · "
            f"{counts['never_attempted']} never attempted · "
            f"{counts['established_absent']} established absent · "
            f"{counts['recoverable_failure']} recoverable failures"
        )


@dataclass(frozen=True)
class BackfillProvenance:
    """How far after the fact a stored row was learned.

    Not a flag on the row — a measurement of the two clocks the store already keeps. A
    boolean would have to be set correctly by every writer forever; the lag is true by
    construction and cannot be forgotten.
    """

    effective_date: date
    observed_at: datetime
    lag_days: int

    @property
    def was_backfilled(self) -> bool:
        """Learned materially after the session it describes.

        One day of lag is normal — a bhavcopy for Monday is fetched on Tuesday. Beyond
        that the row was not knowable at the time it describes, which is exactly what a
        point-in-time reader must exclude.
        """
        return self.lag_days > 1


def find_missing_dates(
    store: BitemporalIngestStore,
    source_name: str,
    window_start: date,
    window_end: date,
    calendar: NseTradingSessionCalendar | None = None,
) -> SourceGapReport:
    """Classify every trading session the source lacks in the window.

    Measured against real trading sessions, not calendar days: a source is not missing a
    Sunday, and reporting it as a gap would drown the real ones.
    """
    trading_calendar = calendar or NseTradingSessionCalendar()
    sessions = list(trading_calendar.sessions_between(window_start, window_end))
    present = set(store.effective_dates_present(source_name))

    attempts_by_date: dict[str, list[tuple[str, str]]] = {}
    for record in store.fetch_history(source_name):
        # The URL is the only per-attempt link back to a date for most sources, so the
        # date is matched by its presence in the URL. A source whose URL carries no date
        # (a rolling file) simply has no per-date attempt history, and its dates fall to
        # NEVER_ATTEMPTED — which is correct: they were never individually asked for.
        attempts_by_date.setdefault(record["url"], []).append(
            (record["fetch_status"], record["evidence"])
        )

    missing: list[MissingDate] = []
    for session in sessions:
        if session in present:
            continue
        stamps = (session.isoformat(), session.strftime("%d%m%Y"), session.strftime("%Y%m%d"),
                  session.strftime("%d%b%Y").upper())
        matched: list[tuple[str, str]] = []
        for url, records in attempts_by_date.items():
            if any(stamp in url for stamp in stamps):
                matched.extend(records)

        if not matched:
            missing.append(
                MissingDate(session, MissingDateReason.NEVER_ATTEMPTED, 0, "no fetch recorded")
            )
            continue

        statuses = {status for status, _evidence in matched}
        last_status, last_evidence = matched[-1]
        if statuses & _RECOVERABLE_FAILURE_STATUSES:
            reason = MissingDateReason.RECOVERABLE_FAILURE
        elif statuses & _ESTABLISHED_ABSENCE_STATUSES:
            reason = MissingDateReason.ESTABLISHED_ABSENT
        else:
            # Fetched successfully yet nothing stored — a parse failure, and a defect
            # rather than an absence. Treated as recoverable so it stays visible.
            reason = MissingDateReason.RECOVERABLE_FAILURE
            last_evidence = f"fetched {last_status} but stored no rows: {last_evidence}"
        missing.append(MissingDate(session, reason, len(matched), last_evidence))

    return SourceGapReport(
        source_name=source_name,
        window_start=window_start,
        window_end=window_end,
        trading_sessions=len(sessions),
        dates_present=len(present & set(sessions)),
        missing=tuple(missing),
    )


def backfill_missing_dates(
    runner: NseSourceIngestRunner,
    adapter: NseIngestSourceAdapter,
    gap_report: SourceGapReport,
    maximum_dates: int | None = None,
) -> SourceIngestRun | None:
    """Re-drive the ingest over the dates worth re-fetching. None if there are none.

    `maximum_dates` bounds one run against a source that is behind by years, so a
    backfill cannot turn into an unbounded scrape of a host that bot-blocks. When it
    truncates, the caller can see it did: the report names how many were refetchable and
    the run names how many were attempted.
    """
    candidates = [entry.effective_date for entry in gap_report.refetchable]
    if not candidates:
        return None
    if maximum_dates is not None:
        candidates = candidates[:maximum_dates]
    return runner.ingest(adapter, candidates)


def measure_backfill_provenance(
    store: BitemporalIngestStore,
    source_name: str,
    effective_date: date,
) -> tuple[BackfillProvenance, ...]:
    """The observation lag of every row stored for one effective date."""
    return tuple(
        BackfillProvenance(
            effective_date=observation.effective_date,
            observed_at=observation.observed_at,
            lag_days=(observation.observed_at.date() - observation.effective_date).days,
        )
        for observation in store.rows_for(source_name, effective_date)
    )


def dates_needing_human_attention(
    gap_reports: Sequence[SourceGapReport], persistent_failure_attempts: int
) -> tuple[tuple[str, MissingDate], ...]:
    """Recoverable failures that have been retried enough to stop being routine.

    `R.21`'s three-strikes rule applied to acquisition: a date that has failed this many
    times is not going to fix itself, and grinding silently is the failure mode. The
    threshold is a policy input rather than a constant because how much patience is
    right depends on how often the backfill runs.
    """
    flagged: list[tuple[str, MissingDate]] = []
    for report in gap_reports:
        for entry in report.by_reason(MissingDateReason.RECOVERABLE_FAILURE):
            if entry.attempts >= persistent_failure_attempts:
                flagged.append((report.source_name, entry))
    return tuple(flagged)
