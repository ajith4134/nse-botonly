"""Guards the guard: proof that the conformance suite fails deliberately broken adapters.

`A.46` makes this suite the done-rule for nine adapters written by parallel agents. A
done-rule that has never been seen to reject anything is worth nothing — the same
lesson as `O.15` and `O.30`, applied to the device the whole fan-out depends on.

So `WellBehavedAdapter` certifies as the positive control, and each broken adapter below
breaks exactly one clause of the contract and is shown to be caught by exactly that
check. The build order in `research/209` requires this before any real adapter exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestRow,
    NseIngestSourceAdapter,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance
from tests.test_nse_ingest_core import WellBehavedAdapter, _payload_for

SAMPLE_DAY = date(2026, 8, 10)
EXPECTED_SAMPLE_ROWS = 2


def _sample_target() -> FetchTarget:
    return FetchTarget(
        url=f"https://archives.nseindia.com/x_{SAMPLE_DAY.isoformat()}.csv",
        source_name="conformance_probe",
        expects=SAMPLE_DAY.isoformat(),
    )


class TestWellBehavedAdapterCertifies(NseIngestAdapterConformance):
    """The positive control: a compliant adapter passes every clause."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return WellBehavedAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [(_sample_target(), _payload_for(SAMPLE_DAY), EXPECTED_SAMPLE_ROWS)]


# --------------------------------------------------------- the negative controls


class SilentOnGarbageAdapter(WellBehavedAdapter):
    """Returns zero rows for a block page instead of raising — the failure that lets a
    broken parser masquerade as a quiet holiday."""

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        try:
            return super().parse(payload, target)
        except Exception:  # noqa: BLE001 — deliberately the anti-pattern under test
            return []


class BlindToTheWrongDateAdapter(WellBehavedAdapter):
    """Accepts any payload as correct — the Sunday-returns-Friday trap, unguarded."""

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        return None


class UnevidencedFloorAdapter(WellBehavedAdapter):
    """Asserts a coverage floor with no evidence behind it (`R.17`)."""

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(date(2000, 1, 1), established_by="   ")


class KeylessRowAdapter(WellBehavedAdapter):
    """Emits rows with no natural key, so revisions cannot be detected at all."""

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        rows = super().parse(payload, target)
        return [
            IngestRow(values=row.values, effective_date=row.effective_date, natural_key=("",))
            for row in rows
        ]


class _ConformanceHarness(NseIngestAdapterConformance):
    """Runs the suite's clauses against an arbitrary adapter, outside pytest collection."""

    def __init__(self, adapter: NseIngestSourceAdapter) -> None:
        self._adapter = adapter

    def build_adapter(self) -> NseIngestSourceAdapter:
        return self._adapter

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [(_sample_target(), _payload_for(SAMPLE_DAY), EXPECTED_SAMPLE_ROWS)]


@pytest.mark.adversarial
def test_the_suite_rejects_an_adapter_that_swallows_garbage() -> None:
    harness = _ConformanceHarness(SilentOnGarbageAdapter())
    with pytest.raises(AssertionError, match="instead of raising"):
        harness.test_malformed_payloads_raise_rather_than_yielding_rows(harness.build_adapter())


@pytest.mark.adversarial
def test_the_suite_rejects_an_adapter_blind_to_the_wrong_date() -> None:
    harness = _ConformanceHarness(BlindToTheWrongDateAdapter())
    with pytest.raises(AssertionError, match="wrong date was accepted"):
        harness.test_a_payload_for_the_wrong_date_is_detected(harness.build_adapter())


@pytest.mark.adversarial
def test_the_suite_rejects_an_unevidenced_coverage_floor() -> None:
    harness = _ConformanceHarness(UnevidencedFloorAdapter())
    with pytest.raises(AssertionError, match="no evidence"):
        harness.test_coverage_floor_is_evidenced_not_asserted(harness.build_adapter())


@pytest.mark.adversarial
def test_the_suite_rejects_rows_without_a_natural_key() -> None:
    """An empty-string key is not caught by `IngestRow`'s own guard, so the suite must
    catch it — otherwise every row of a source collides into one and revisions are lost."""
    harness = _ConformanceHarness(KeylessRowAdapter())
    with pytest.raises(AssertionError, match="uniquely identifiable"):
        harness.test_parsed_rows_are_well_formed(harness.build_adapter())


@pytest.mark.adversarial
def test_the_suite_rejects_an_adapter_that_fails_its_own_sample() -> None:
    """A miscounted sample means the adapter is not parsing what it claims to."""

    class MiscountingHarness(_ConformanceHarness):
        def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
            return [(_sample_target(), _payload_for(SAMPLE_DAY), 99)]

    harness = MiscountingHarness(WellBehavedAdapter())
    with pytest.raises(AssertionError, match="parsed 2 rows"):
        harness.test_real_payloads_parse_to_the_expected_row_count(harness.build_adapter())


@pytest.mark.adversarial
def test_the_suite_rejects_an_adapter_with_no_sample_payload() -> None:
    """Certifying against nothing is the easiest way to pass and the least meaningful."""

    class EmptyHarness(_ConformanceHarness):
        def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
            return []

    harness = EmptyHarness(WellBehavedAdapter())
    with pytest.raises(AssertionError, match="at least one real payload"):
        harness.test_real_payloads_parse_to_the_expected_row_count(harness.build_adapter())


@pytest.mark.unit
def test_the_storage_clauses_run_against_a_real_store(tmp_path: Path) -> None:
    """The storage clauses need a store fixture, so they are exercised here directly to
    prove they are not silently skipped when a subclass certifies."""
    harness = _ConformanceHarness(WellBehavedAdapter())
    adapter = harness.build_adapter()
    with BitemporalIngestStore(tmp_path / "ingest.sqlite3") as store:
        harness.test_re_ingesting_identical_content_is_a_no_op(adapter, store)
    with BitemporalIngestStore(tmp_path / "revision.sqlite3") as store:
        harness.test_changed_content_is_retained_as_a_revision(adapter, store)
    with BitemporalIngestStore(tmp_path / "atomic.sqlite3") as store:
        harness.test_a_failed_ingest_leaves_the_store_untouched(adapter, store)
    with BitemporalIngestStore(tmp_path / "provenance.sqlite3") as store:
        harness.test_every_stored_row_traces_to_a_recorded_fetch(adapter, store)
    with BitemporalIngestStore(tmp_path / "floor.sqlite3") as store:
        harness.test_a_row_before_the_coverage_floor_is_refused(adapter, store)


@pytest.mark.unit
def test_an_as_of_read_is_the_leakage_guard(tmp_path: Path) -> None:
    """Restated here because it is the clause that matters most for a backtest: a
    revision published later must be invisible to an earlier question."""
    adapter = WellBehavedAdapter()
    monday = datetime(2026, 8, 10, 12, tzinfo=UTC)
    with BitemporalIngestStore(tmp_path / "asof.sqlite3") as store:
        fetch_id = store.record_fetch(
            adapter.source_name, "u", monday, "retrieved", "e", 1
        )
        store.ingest_rows(
            adapter.source_name,
            [IngestRow({"close": 100}, SAMPLE_DAY, ("RELIANCE",))],
            monday,
            fetch_id,
        )
        store.ingest_rows(
            adapter.source_name,
            [IngestRow({"close": 999}, SAMPLE_DAY, ("RELIANCE",))],
            monday + timedelta(days=3),
            fetch_id,
        )
        known_on_monday = store.rows_for(adapter.source_name, SAMPLE_DAY, known_by=monday)
        assert [row.values["close"] for row in known_on_monday] == [100]
