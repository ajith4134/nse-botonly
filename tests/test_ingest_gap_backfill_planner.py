"""Gap classification and backfill — the distinction the whole module exists for.

Every test here is really the same question asked from a different angle: does the
planner tell apart "we never asked", "we asked and it does not exist", and "we asked and
were blocked"? Collapsing those produces a backfill that retries holidays nightly and
gives up on real outages.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.ingest_gap_backfill_planner import (
    MissingDateReason,
    backfill_missing_dates,
    dates_needing_human_attention,
    find_missing_dates,
    measure_backfill_provenance,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import IngestRow
from nse_algo_trader.nse_ingest.nse_source_fetcher import (
    FetchOutcome,
    FetchStatus,
    FetchTarget,
    NseSourceFetcher,
    PayloadContentCheck,
)
from nse_algo_trader.nse_ingest.nse_source_ingest_runner import NseSourceIngestRunner
from tests.test_nse_ingest_core import SOURCE, WellBehavedAdapter, _outcome, _payload_for

WINDOW_START = date(2026, 8, 3)
WINDOW_END = date(2026, 8, 7)
PRESENT_DAY = date(2026, 8, 3)
ABSENT_DAY = date(2026, 8, 4)
BLOCKED_DAY = date(2026, 8, 5)
SESSIONS_IN_WINDOW = 5
PERSISTENT_ATTEMPTS = 3
BACKFILL_LAG_DAYS = 40
BACKFILL_BATCH_CAP = 2


@pytest.fixture
def store(tmp_path: Path) -> Iterator[BitemporalIngestStore]:
    with BitemporalIngestStore(tmp_path / "gaps.sqlite3") as opened:
        yield opened


def _url_for(day: date) -> str:
    return f"https://archives.nseindia.com/x_{day.isoformat()}.csv"


def _store_a_day(store: BitemporalIngestStore, day: date, observed: datetime) -> None:
    fetch_id = store.record_fetch(SOURCE, _url_for(day), observed, "retrieved", "ok", 1)
    store.ingest_rows(SOURCE, [IngestRow({"x": 1}, day, ("A",))], observed, fetch_id)


class _ScriptedNseSourceFetcher(NseSourceFetcher):
    """A genuine `NseSourceFetcher` that returns scripted outcomes instead of fetching.

    `NseSourceIngestRunner` is typed against the concrete `NseSourceFetcher` class, not a
    protocol, so a stand-in that only duck-types `fetch` (like `test_nse_ingest_core`'s
    `StubFetcher`) fails the type check even though it behaves correctly at runtime.
    Subclassing is what makes "genuinely satisfies the type" and "runs correctly" the same
    fact, with no cast standing between them.
    """

    def __init__(self, outcomes_by_url: dict[str, FetchOutcome]) -> None:
        super().__init__()
        self._outcomes_by_url = outcomes_by_url

    def fetch(
        self, target: FetchTarget, content_check: PayloadContentCheck | None = None
    ) -> FetchOutcome:
        return self._outcomes_by_url[target.url]


@pytest.mark.unit
def test_a_date_never_asked_for_is_distinguished_from_one_that_failed(
    store: BitemporalIngestStore,
) -> None:
    now = datetime.now(UTC)
    _store_a_day(store, PRESENT_DAY, now)
    store.record_fetch(SOURCE, _url_for(ABSENT_DAY), now, "not_found", "HTTP 404", 1)
    store.record_fetch(SOURCE, _url_for(BLOCKED_DAY), now, "bot_blocked", "HTTP 403", 4)

    report = find_missing_dates(store, SOURCE, WINDOW_START, WINDOW_END)
    assert report.trading_sessions == SESSIONS_IN_WINDOW
    assert report.dates_present == 1
    reasons = {entry.effective_date: entry.reason for entry in report.missing}
    assert reasons[ABSENT_DAY] is MissingDateReason.ESTABLISHED_ABSENT
    assert reasons[BLOCKED_DAY] is MissingDateReason.RECOVERABLE_FAILURE
    assert reasons[date(2026, 8, 6)] is MissingDateReason.NEVER_ATTEMPTED


@pytest.mark.unit
def test_an_established_absence_is_not_refetched(store: BitemporalIngestStore) -> None:
    """An archive that answered 404 will answer 404 again. Retrying it nightly buries
    the failures that CAN be fixed."""
    now = datetime.now(UTC)
    for day in (ABSENT_DAY, BLOCKED_DAY):
        status = "not_found" if day == ABSENT_DAY else "bot_blocked"
        store.record_fetch(SOURCE, _url_for(day), now, status, "e", 1)
    report = find_missing_dates(store, SOURCE, ABSENT_DAY, BLOCKED_DAY)
    refetchable = {entry.effective_date for entry in report.refetchable}
    assert ABSENT_DAY not in refetchable
    assert BLOCKED_DAY in refetchable


@pytest.mark.adversarial
def test_a_successful_fetch_that_stored_nothing_is_a_defect_not_an_absence(
    store: BitemporalIngestStore,
) -> None:
    """Fetched fine, parsed to nothing: that is a broken parser, and treating it as an
    absence would hide it forever behind 'the file does not exist'."""
    now = datetime.now(UTC)
    store.record_fetch(SOURCE, _url_for(ABSENT_DAY), now, "retrieved", "HTTP 200", 1)
    report = find_missing_dates(store, SOURCE, ABSENT_DAY, ABSENT_DAY)
    entry = report.missing[0]
    assert entry.reason is MissingDateReason.RECOVERABLE_FAILURE
    assert "stored no rows" in entry.last_evidence


@pytest.mark.unit
def test_weekends_are_not_reported_as_gaps(store: BitemporalIngestStore) -> None:
    """Measured against real trading sessions. Calendar days would report ~115 false
    gaps a year and drown the real ones."""
    report = find_missing_dates(store, SOURCE, date(2026, 8, 8), date(2026, 8, 9))
    assert report.trading_sessions == 0
    assert report.missing == ()


@pytest.mark.unit
def test_a_recoverable_failure_that_persists_is_escalated(
    store: BitemporalIngestStore,
) -> None:
    """`R.21` three strikes, applied to acquisition: grinding silently is the failure."""
    now = datetime.now(UTC)
    for _attempt in range(PERSISTENT_ATTEMPTS):
        store.record_fetch(SOURCE, _url_for(BLOCKED_DAY), now, "bot_blocked", "HTTP 403", 4)
    store.record_fetch(SOURCE, _url_for(ABSENT_DAY), now, "bot_blocked", "HTTP 403", 4)

    report = find_missing_dates(store, SOURCE, ABSENT_DAY, BLOCKED_DAY)
    flagged = dates_needing_human_attention(
        [report], persistent_failure_attempts=PERSISTENT_ATTEMPTS
    )
    flagged_dates = {entry.effective_date for _source, entry in flagged}
    assert BLOCKED_DAY in flagged_dates
    assert ABSENT_DAY not in flagged_dates


@pytest.mark.unit
def test_backfill_refetches_only_what_is_worth_refetching(
    store: BitemporalIngestStore,
) -> None:
    now = datetime.now(UTC)
    store.record_fetch(SOURCE, _url_for(ABSENT_DAY), now, "not_found", "HTTP 404", 1)
    store.record_fetch(SOURCE, _url_for(BLOCKED_DAY), now, "bot_blocked", "HTTP 403", 4)
    report = find_missing_dates(store, SOURCE, ABSENT_DAY, BLOCKED_DAY)

    adapter = WellBehavedAdapter()
    targets = {
        target.url: _outcome(target, FetchStatus.RETRIEVED, _payload_for(BLOCKED_DAY))
        for target in adapter.fetch_targets([BLOCKED_DAY])
    }
    run = backfill_missing_dates(
        NseSourceIngestRunner(_ScriptedNseSourceFetcher(targets), store),
        adapter,
        report,
    )
    assert run is not None
    assert run.rows_inserted > 0
    assert {outcome.target.expects for outcome in run.outcomes} == {BLOCKED_DAY.isoformat()}


@pytest.mark.unit
def test_backfill_returns_none_when_there_is_nothing_to_do(
    store: BitemporalIngestStore,
) -> None:
    now = datetime.now(UTC)
    for day in (date(2026, 8, 3), date(2026, 8, 4)):
        _store_a_day(store, day, now)
    report = find_missing_dates(store, SOURCE, date(2026, 8, 3), date(2026, 8, 4))
    assert (
        backfill_missing_dates(
            NseSourceIngestRunner(_ScriptedNseSourceFetcher({}), store),
            WellBehavedAdapter(),
            report,
        )
        is None
    )


@pytest.mark.adversarial
def test_a_backfill_is_bounded_so_it_cannot_become_a_scrape(
    store: BitemporalIngestStore,
) -> None:
    """A source years behind must not turn one nightly run into an unbounded crawl of a
    host that bot-blocks."""
    report = find_missing_dates(store, SOURCE, date(2026, 7, 1), date(2026, 8, 7))
    assert len(report.refetchable) > BACKFILL_BATCH_CAP
    adapter = WellBehavedAdapter()
    targets = {
        target.url: _outcome(target, FetchStatus.NOT_FOUND, None)
        for target in adapter.fetch_targets([entry.effective_date for entry in report.refetchable])
    }
    run = backfill_missing_dates(
        NseSourceIngestRunner(_ScriptedNseSourceFetcher(targets), store),
        adapter,
        report,
        maximum_dates=BACKFILL_BATCH_CAP,
    )
    assert run is not None
    assert len(run.outcomes) == BACKFILL_BATCH_CAP


@pytest.mark.unit
def test_backfill_provenance_is_measured_from_the_two_clocks(
    store: BitemporalIngestStore,
) -> None:
    """Not a flag somebody must remember to set — a lag that is true by construction."""
    effective = date(2026, 8, 3)
    late = datetime(2026, 9, 12, 9, tzinfo=UTC)
    fetch_id = store.record_fetch(SOURCE, _url_for(effective), late, "retrieved", "ok", 1)
    store.ingest_rows(SOURCE, [IngestRow({"x": 1}, effective, ("A",))], late, fetch_id)

    provenance = measure_backfill_provenance(store, SOURCE, effective)
    assert len(provenance) == 1
    assert provenance[0].lag_days == BACKFILL_LAG_DAYS
    assert provenance[0].was_backfilled


@pytest.mark.unit
def test_a_next_day_fetch_is_not_a_backfill(store: BitemporalIngestStore) -> None:
    """A bhavcopy for Monday fetched on Tuesday is normal operation, not backfill."""
    effective = date(2026, 8, 3)
    next_day = datetime(2026, 8, 4, 9, tzinfo=UTC)
    fetch_id = store.record_fetch(SOURCE, _url_for(effective), next_day, "retrieved", "ok", 1)
    store.ingest_rows(SOURCE, [IngestRow({"x": 1}, effective, ("A",))], next_day, fetch_id)
    assert not measure_backfill_provenance(store, SOURCE, effective)[0].was_backfilled


@pytest.mark.unit
def test_the_report_describes_itself_in_all_three_classes(
    store: BitemporalIngestStore,
) -> None:
    now = datetime.now(UTC)
    _store_a_day(store, PRESENT_DAY, now)
    store.record_fetch(SOURCE, _url_for(ABSENT_DAY), now, "not_found", "HTTP 404", 1)
    store.record_fetch(SOURCE, _url_for(BLOCKED_DAY), now, "bot_blocked", "HTTP 403", 4)
    description = find_missing_dates(store, SOURCE, WINDOW_START, WINDOW_END).describe()
    assert "never attempted" in description
    assert "established absent" in description
    assert "recoverable failures" in description


@pytest.mark.unit
def test_a_rolling_source_with_undated_urls_reports_never_attempted(
    store: BitemporalIngestStore,
) -> None:
    """A rolling file's URL carries no date, so no per-date attempt history can exist.
    Reporting those sessions as never-attempted is honest: they never were asked for
    individually, and the source cannot serve them."""
    now = datetime.now(UTC)
    store.record_fetch(
        SOURCE, "https://archives.nseindia.com/rolling.csv", now, "retrieved", "ok", 1
    )
    report = find_missing_dates(store, SOURCE, WINDOW_START, WINDOW_END)
    assert all(entry.reason is MissingDateReason.NEVER_ATTEMPTED for entry in report.missing)
