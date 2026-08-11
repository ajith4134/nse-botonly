"""The shared ingest core: failure classification, bitemporality, and the runner.

Every classification case here was observed on a real NSE endpoint (`research/207`,
`research/210`). The most important is the one that looks like success: a request for
Sunday 2026-08-09 that returned Friday's file with HTTP 200.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import (
    BitemporalIngestStore,
    BitemporalIngestStoreError,
    content_hash_of,
)
from nse_algo_trader.nse_ingest.ingest_coverage_self_check import (
    build_source_coverage_report,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import (
    FetchOutcome,
    FetchStatus,
    FetchTarget,
    NseSourceFetcher,
    NseSourceFetchError,
    RetryPolicy,
    classify_payload,
)
from nse_algo_trader.nse_ingest.nse_source_ingest_runner import NseSourceIngestRunner

SOURCE = "conformance_probe"
FLOOR = date(2000, 1, 1)
HTTP_OK = 200
EXPECTED_ROW_COUNT = 2
EXPECTED_TWO_OBSERVATIONS = 2
SAMPLE_YEAR = 2026
BACKOFF_SECOND_ATTEMPT = 3.0
BACKOFF_CEILING = 10.0
BACKOFF_HALF_JITTER = 1.5
RETRY_ATTEMPTS = 3
SLEEPS_BETWEEN_RETRIES = 2


def _target(url: str = "https://archives.nseindia.com/x_2026-08-10.csv") -> FetchTarget:
    return FetchTarget(url=url, source_name=SOURCE)


def _payload_for(day: date, symbols: Sequence[str] = ("RELIANCE", "TCS")) -> bytes:
    rows = [{"TradDt": day.isoformat(), "TckrSymb": symbol, "ClsPric": 100} for symbol in symbols]
    return json.dumps(rows).encode()


class WellBehavedAdapter:
    """A reference adapter that honours the contract — the suite's positive control."""

    @property
    def source_name(self) -> str:
        return SOURCE

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(FLOOR, established_by="fetched 2000-01-03 successfully")

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        return [
            FetchTarget(
                url=f"https://archives.nseindia.com/x_{day.isoformat()}.csv",
                source_name=SOURCE,
                expects=day.isoformat(),
            )
            for day in for_dates
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        try:
            records = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as failure:
            raise IngestAdapterError(f"not the expected JSON: {failure}") from failure
        if not isinstance(records, list) or not records:
            raise IngestAdapterError("payload carried no records")
        rows = []
        for record in records:
            if not isinstance(record, dict) or "TckrSymb" not in record:
                raise IngestAdapterError("record is missing TckrSymb")
            rows.append(
                IngestRow(
                    values=record,
                    effective_date=date.fromisoformat(record["TradDt"]),
                    natural_key=(record["TckrSymb"],),
                )
            )
        return rows

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        try:
            records = json.loads(payload)
            payload_date = records[0]["TradDt"]
        except (ValueError, KeyError, IndexError, UnicodeDecodeError):
            return "payload could not be dated"
        if target.expects and payload_date != target.expects:
            return f"payload is dated {payload_date}, requested {target.expects}"
        return None


class StubFetcher:
    """Returns scripted outcomes so the runner is testable with no network."""

    def __init__(self, outcomes: dict[str, FetchOutcome]) -> None:
        self._outcomes = outcomes

    def fetch(self, target: FetchTarget, content_check: object = None) -> FetchOutcome:
        return self._outcomes[target.url]


def _outcome(
    target: FetchTarget, status: FetchStatus, payload: bytes | None, evidence: str = "test"
) -> FetchOutcome:
    return FetchOutcome(
        target=target,
        status=status,
        payload=payload,
        http_status=HTTP_OK,
        attempts=1,
        fetched_at=datetime.now(UTC),
        evidence=evidence,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[BitemporalIngestStore]:
    with BitemporalIngestStore(tmp_path / "ingest.sqlite3") as opened:
        yield opened


# ------------------------------------------------------------- classification


@pytest.mark.unit
def test_a_real_payload_is_retrieved() -> None:
    status, evidence = classify_payload(_payload_for(date(2026, 8, 10)), HTTP_OK, _target())
    assert status is FetchStatus.RETRIEVED
    assert "bytes" in evidence


@pytest.mark.adversarial
def test_the_sunday_returning_fridays_data_case_is_caught() -> None:
    """The measured trap: HTTP 200, a well-formed file, and the wrong day's data. No
    retry library treats this as a failure because nothing about it is an error."""
    adapter = WellBehavedAdapter()
    sunday = FetchTarget(
        url="https://x/x_2026-08-09.csv", source_name=SOURCE, expects="2026-08-09"
    )
    fridays_file = _payload_for(date(2026, 8, 7))
    status, evidence = classify_payload(
        fridays_file, HTTP_OK, sunday, adapter.content_mismatch_reason
    )
    assert status is FetchStatus.CONTENT_MISMATCH
    assert "2026-08-07" in evidence


@pytest.mark.unit
def test_a_content_mismatch_is_never_retried() -> None:
    """The server answered; it answered wrongly. Asking again returns the same wrong
    file, so retrying converts a clean detection into a slow one and nothing else."""
    assert not FetchStatus.CONTENT_MISMATCH.is_worth_retrying
    assert FetchStatus.TRANSPORT_FAILURE.is_worth_retrying
    assert FetchStatus.BOT_BLOCKED.is_worth_retrying
    assert not FetchStatus.NOT_FOUND.is_worth_retrying
    assert not FetchStatus.JAVASCRIPT_SHELL.is_worth_retrying


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("body", "http_status", "expected"),
    [
        (b"<!DOCTYPE html><body>Access Denied</body>", 403, FetchStatus.BOT_BLOCKED),
        (b"<html>Unable to process your request</html>", 503, FetchStatus.BOT_BLOCKED),
        (b"<!DOCTYPE html>Access Denied Reference #18.abc", 200, FetchStatus.BOT_BLOCKED),
        (b"", 200, FetchStatus.EMPTY_PAYLOAD),
        (b"anything", 404, FetchStatus.NOT_FOUND),
        (b"<!doctype html><div id=\"__next\"></div>", 200, FetchStatus.JAVASCRIPT_SHELL),
        (b"<!doctype html><table>real page</table>", 200, FetchStatus.BOT_BLOCKED),
        (b"anything", 500, FetchStatus.TRANSPORT_FAILURE),
    ],
)
def test_every_measured_failure_shape_is_classified(
    body: bytes, http_status: int, expected: FetchStatus
) -> None:
    status, _evidence = classify_payload(body, http_status, _target())
    assert status is expected


@pytest.mark.unit
def test_require_payload_names_why_there_is_none() -> None:
    outcome = _outcome(_target(), FetchStatus.BOT_BLOCKED, None, evidence="HTTP 403")
    with pytest.raises(NseSourceFetchError, match="bot_blocked"):
        outcome.require_payload()


@pytest.mark.unit
def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(
        initial_backoff_seconds=1.0, backoff_multiplier=3.0, maximum_backoff_seconds=10.0
    )
    assert policy.backoff_for(1, jitter=1.0) == 1.0
    assert policy.backoff_for(2, jitter=1.0) == BACKOFF_SECOND_ATTEMPT
    assert policy.backoff_for(5, jitter=1.0) == BACKOFF_CEILING
    assert policy.backoff_for(2, jitter=0.5) == BACKOFF_HALF_JITTER


@pytest.mark.adversarial
def test_a_transport_failure_is_retried_then_reported() -> None:
    import requests

    class ExplodingSession:
        headers: ClassVar[dict[str, str]] = {}
        attempts = 0

        def get(self, *_args: object, **_kwargs: object) -> object:
            ExplodingSession.attempts += 1
            raise requests.ConnectionError("no route to host")

    slept: list[float] = []
    fetcher = NseSourceFetcher(
        http_session=ExplodingSession(),  # type: ignore[arg-type]
        retry_policy=RetryPolicy(maximum_attempts=3, initial_backoff_seconds=0.0),
        sleep=slept.append,
        jitter_source=lambda: 1.0,
    )
    outcome = fetcher.fetch(_target())
    assert outcome.status is FetchStatus.TRANSPORT_FAILURE
    assert outcome.attempts == RETRY_ATTEMPTS
    assert len(slept) == SLEEPS_BETWEEN_RETRIES
    assert "ConnectionError" in outcome.evidence


# -------------------------------------------------------------- bitemporality


@pytest.mark.unit
def test_identical_content_is_stored_once(store: BitemporalIngestStore) -> None:
    adapter = WellBehavedAdapter()
    rows = adapter.parse(_payload_for(date(2026, 8, 10)), _target())
    observed = datetime.now(UTC)
    fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
    first = store.ingest_rows(SOURCE, rows, observed, fetch_id)
    second = store.ingest_rows(SOURCE, rows, observed + timedelta(days=1), fetch_id)
    assert first.rows_inserted == EXPECTED_ROW_COUNT
    assert second.rows_inserted == 0
    assert second.was_a_no_op


@pytest.mark.unit
def test_a_revision_is_retained_alongside_the_original(store: BitemporalIngestStore) -> None:
    """NSE republishes corrected files. Overwriting destroys the only evidence that a
    decision was made from the original."""
    observed = datetime.now(UTC)
    fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
    original = IngestRow({"close": 100}, date(2026, 8, 10), ("RELIANCE",))
    corrected = IngestRow({"close": 101}, date(2026, 8, 10), ("RELIANCE",))
    store.ingest_rows(SOURCE, [original], observed, fetch_id)
    result = store.ingest_rows(SOURCE, [corrected], observed + timedelta(days=2), fetch_id)
    assert result.revisions_recorded == 1
    history = store.all_observations_for(SOURCE, date(2026, 8, 10), ("RELIANCE",))
    assert len(history) == EXPECTED_TWO_OBSERVATIONS
    assert [record.values["close"] for record in history] == [100, 101]


@pytest.mark.unit
def test_as_of_reads_cannot_see_a_later_revision(store: BitemporalIngestStore) -> None:
    """The leakage guard: a correction published on Wednesday must be invisible to a
    question about what was knowable on Monday."""
    monday = datetime(2026, 8, 10, 12, tzinfo=UTC)
    wednesday = monday + timedelta(days=2)
    fetch_id = store.record_fetch(SOURCE, "u", monday, "retrieved", "e", 1)
    store.ingest_rows(
        SOURCE, [IngestRow({"close": 100}, date(2026, 8, 10), ("RELIANCE",))], monday, fetch_id
    )
    store.ingest_rows(
        SOURCE, [IngestRow({"close": 101}, date(2026, 8, 10), ("RELIANCE",))], wednesday, fetch_id
    )

    as_of_monday = store.rows_for(SOURCE, date(2026, 8, 10), known_by=monday)
    assert [record.values["close"] for record in as_of_monday] == [100]
    latest = store.rows_for(SOURCE, date(2026, 8, 10))
    assert [record.values["close"] for record in latest] == [101]


@pytest.mark.adversarial
def test_a_future_effective_date_is_refused(store: BitemporalIngestStore) -> None:
    observed = datetime.now(UTC)
    fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    with pytest.raises(BitemporalIngestStoreError, match="future"):
        store.ingest_rows(SOURCE, [IngestRow({"x": 1}, tomorrow, ("A",))], observed, fetch_id)


@pytest.mark.adversarial
def test_a_rejected_batch_stores_nothing(store: BitemporalIngestStore) -> None:
    """All-or-nothing: a half-populated effective date has nothing marking it partial."""
    observed = datetime.now(UTC)
    fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
    good = IngestRow({"x": 1}, date(2026, 8, 10), ("A",))
    bad = IngestRow({"x": 2}, datetime.now(UTC).date() + timedelta(days=400), ("B",))
    with pytest.raises(BitemporalIngestStoreError):
        store.ingest_rows(SOURCE, [good, bad], observed, fetch_id)
    assert store.row_count(SOURCE) == 0


@pytest.mark.unit
def test_content_hash_ignores_key_ordering() -> None:
    """Without stable ordering every re-ingest would look like a revision."""
    assert content_hash_of({"a": 1, "b": 2}) == content_hash_of({"b": 2, "a": 1})


@pytest.mark.unit
def test_failed_fetches_are_recorded_too(store: BitemporalIngestStore) -> None:
    """A source blocked for a week is invisible if only successes are written down."""
    now = datetime.now(UTC)
    store.record_fetch(SOURCE, "u", now, "retrieved", "ok", 1)
    store.record_fetch(SOURCE, "u", now, "bot_blocked", "HTTP 403", 4)
    assert len(store.fetch_history(SOURCE)) == EXPECTED_TWO_OBSERVATIONS
    assert len(store.fetch_history(SOURCE, successful_only=True)) == 1


# --------------------------------------------------------------------- runner


@pytest.mark.unit
def test_the_runner_stores_a_clean_fetch(store: BitemporalIngestStore) -> None:
    adapter = WellBehavedAdapter()
    day = date(2026, 8, 10)
    target = adapter.fetch_targets([day])[0]
    runner = NseSourceIngestRunner(
        StubFetcher(  # type: ignore[arg-type]
            {target.url: _outcome(target, FetchStatus.RETRIEVED, _payload_for(day))}
        ),
        store,
    )
    run = runner.ingest(adapter, [day])
    assert run.rows_inserted == EXPECTED_ROW_COUNT
    assert not run.blocked_targets
    assert store.row_count(SOURCE) == EXPECTED_ROW_COUNT


@pytest.mark.adversarial
def test_a_blocked_fetch_is_never_handed_to_the_parser(store: BitemporalIngestStore) -> None:
    """Parsing a block page yields either a crash or zero rows indistinguishable from a
    holiday. The second is worse, and it is what a naive pipeline produces."""
    adapter = WellBehavedAdapter()
    day = date(2026, 8, 10)
    target = adapter.fetch_targets([day])[0]
    runner = NseSourceIngestRunner(
        StubFetcher(  # type: ignore[arg-type]
            {target.url: _outcome(target, FetchStatus.BOT_BLOCKED, b"<html>Access Denied</html>")}
        ),
        store,
    )
    run = runner.ingest(adapter, [day])
    assert run.rows_inserted == 0
    assert len(run.blocked_targets) == 1
    assert store.row_count(SOURCE) == 0
    assert len(store.fetch_history(SOURCE)) == 1, "the blocked attempt was not recorded"


@pytest.mark.adversarial
def test_a_content_mismatch_is_surfaced_not_ingested(store: BitemporalIngestStore) -> None:
    adapter = WellBehavedAdapter()
    day = date(2026, 8, 9)
    target = adapter.fetch_targets([day])[0]
    runner = NseSourceIngestRunner(
        StubFetcher(  # type: ignore[arg-type]
            {
                target.url: _outcome(
                    target, FetchStatus.CONTENT_MISMATCH, _payload_for(date(2026, 8, 7))
                )
            }
        ),
        store,
    )
    run = runner.ingest(adapter, [day])
    assert len(run.content_mismatches) == 1
    assert store.row_count(SOURCE) == 0


@pytest.mark.adversarial
def test_a_parse_failure_is_recorded_and_does_not_abort_the_run(
    store: BitemporalIngestStore,
) -> None:
    adapter = WellBehavedAdapter()
    good_day, bad_day = date(2026, 8, 10), date(2026, 8, 11)
    targets = adapter.fetch_targets([good_day, bad_day])
    runner = NseSourceIngestRunner(
        StubFetcher(  # type: ignore[arg-type]
            {
                targets[0].url: _outcome(targets[0], FetchStatus.RETRIEVED, _payload_for(good_day)),
                targets[1].url: _outcome(targets[1], FetchStatus.RETRIEVED, b"{not json"),
            }
        ),
        store,
    )
    run = runner.ingest(adapter, [good_day, bad_day])
    assert run.rows_inserted == EXPECTED_ROW_COUNT
    assert len(run.parse_failures) == 1
    assert "not the expected JSON" in (run.parse_failures[0].parse_error or "")


# ------------------------------------------------------------------- coverage


@pytest.mark.unit
def test_coverage_is_measured_against_real_trading_sessions(
    store: BitemporalIngestStore,
) -> None:
    """A source is not missing Republic Day. Coverage against calendar days would
    condemn every source for the ~115 non-sessions in a year."""
    observed = datetime.now(UTC)
    fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
    sessions = [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)]
    for day in sessions:
        store.ingest_rows(SOURCE, [IngestRow({"x": 1}, day, ("A",))], observed, fetch_id)

    report = build_source_coverage_report(store, SOURCE)
    assert report.has_any_data
    year = report.years[0]
    assert year.year == SAMPLE_YEAR
    assert year.effective_dates_present == len(sessions)
    assert year.trading_sessions_in_year > len(sessions)
    assert 0.0 < year.coverage_fraction < 1.0
    assert year.missing_dates


@pytest.mark.unit
def test_coverage_reports_a_source_with_nothing(store: BitemporalIngestStore) -> None:
    report = build_source_coverage_report(store, "never_fetched")
    assert not report.has_any_data
    assert report.years == ()
    assert report.worst_year() is None


# ------------------------------------ gaps found by mutation testing (ingest core)


@pytest.mark.adversarial
def test_a_not_found_is_not_retried() -> None:
    """A 404 means the file does not exist — usually a holiday. Retrying it turns every
    non-session day into four pointless requests against a host that bot-blocks."""

    class NotFoundSession:
        headers: ClassVar[dict[str, str]] = {}
        calls = 0

        def get(self, *_args: object, **_kwargs: object) -> object:
            NotFoundSession.calls += 1

            class Response:
                status_code = 404
                content = b"not found"

            return Response()

    fetcher = NseSourceFetcher(
        http_session=NotFoundSession(),  # type: ignore[arg-type]
        retry_policy=RetryPolicy(maximum_attempts=4, initial_backoff_seconds=0.0),
        sleep=lambda _seconds: None,
    )
    outcome = fetcher.fetch(_target())
    assert outcome.status is FetchStatus.NOT_FOUND
    assert outcome.attempts == 1
    assert NotFoundSession.calls == 1


@pytest.mark.unit
def test_require_payload_returns_the_bytes_on_success() -> None:
    """The happy path of the guard. Inverting its condition would raise on every good
    fetch and hand back bytes for every bad one."""
    payload = _payload_for(date(2026, 8, 10))
    outcome = _outcome(_target(), FetchStatus.RETRIEVED, payload)
    assert outcome.require_payload() == payload


@pytest.mark.unit
def test_todays_effective_date_is_accepted() -> None:
    """The rolling ban list is dated TODAY. A future-date guard written as `>=` would
    reject the one source that most needs storing, every single day."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as scratch,
        BitemporalIngestStore(Path(scratch) / "today.sqlite3") as store,
    ):
        observed = datetime.now(UTC)
        fetch_id = store.record_fetch(SOURCE, "u", observed, "retrieved", "e", 1)
        today = datetime.now(UTC).date()
        result = store.ingest_rows(
            SOURCE, [IngestRow({"x": 1}, today, ("A",))], observed, fetch_id
        )
        assert result.rows_inserted == 1


@pytest.mark.unit
def test_an_outcome_only_succeeds_when_fetched_and_parsed(
    store: BitemporalIngestStore,
) -> None:
    """`succeeded` must require BOTH. An `or` would report a blocked fetch as a success
    and a run would look clean while storing nothing."""
    from nse_algo_trader.nse_ingest.nse_source_ingest_runner import TargetIngestOutcome

    clean = TargetIngestOutcome(_target(), FetchStatus.RETRIEVED, "ok")
    blocked = TargetIngestOutcome(_target(), FetchStatus.BOT_BLOCKED, "HTTP 403")
    unparsed = TargetIngestOutcome(
        _target(), FetchStatus.RETRIEVED, "ok", parse_error="bad csv"
    )
    assert clean.succeeded
    assert not blocked.succeeded
    assert not unparsed.succeeded


@pytest.mark.unit
def test_the_coverage_report_counts_failed_fetches_separately(
    store: BitemporalIngestStore,
) -> None:
    """A source blocked for a week must be visible as failures, not folded into the
    successes or subtracted the wrong way."""
    now = datetime.now(UTC)
    store.record_fetch(SOURCE, "u", now, "retrieved", "ok", 1)
    store.record_fetch(SOURCE, "u", now, "bot_blocked", "HTTP 403", 4)
    store.record_fetch(SOURCE, "u", now, "not_found", "HTTP 404", 1)
    report = build_source_coverage_report(store, SOURCE)
    assert report.successful_fetches == 1
    assert report.failed_fetches == EXPECTED_TWO_OBSERVATIONS
