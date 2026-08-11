"""The conformance suite every NSE source adapter must pass, and may not edit.

`A.46` parallelizes the nine adapters across concurrent agents. This file is the device
that makes that safe: an adapter author does not get to define what "done" means, and
cannot negotiate with a test they did not write. Eight properties are checked, each one
a failure mode that would otherwise be discovered months later in a backtest.

**How an adapter joins.** Subclass `NseIngestAdapterConformance`, implement the two
fixtures, and the whole suite runs against your adapter:

    class TestFooBarAdapter(NseIngestAdapterConformance):
        def build_adapter(self): return FooBarAdapter()
        def sample_payloads(self): return [(target, payload_bytes, expected_row_count)]

**This file is core, not test scaffolding.** Editing it to make an adapter pass is
editing the contract, and is exactly what the arrangement exists to prevent.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import (
    BitemporalIngestStore,
    BitemporalIngestStoreError,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestRow,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

EXPECTED_OBSERVATIONS_AFTER_A_REVISION = 2

MALFORMED_PAYLOADS: tuple[tuple[str, bytes], ...] = (
    ("empty", b""),
    ("truncated_text", b"TradDt,TckrSymb\n2026-08-10"),
    ("wrong_schema", b"totally,unrelated,columns\n1,2,3"),
    ("html_error_page", b"<!DOCTYPE html><html><body>Access Denied</body></html>"),
    ("binary_noise", bytes(range(256))),
)
"""Every one of these has been served by a real endpoint at some point. An adapter that
returns rows for any of them will one day ingest a block page as market data."""


class NseIngestAdapterConformance(abc.ABC):
    """Subclass this to certify an adapter. Do not modify it to make one pass."""

    @abc.abstractmethod
    def build_adapter(self) -> NseIngestSourceAdapter:
        """A fresh adapter instance."""

    @abc.abstractmethod
    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        """(target, real payload bytes, expected row count) — at least one.

        The payload must be REAL bytes captured from the source, not a hand-written
        fixture: a fixture proves the parser reads what you imagined, and the point is
        to prove it reads what NSE actually sends.
        """

    @pytest.fixture
    def adapter(self) -> NseIngestSourceAdapter:
        return self.build_adapter()

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[BitemporalIngestStore]:
        with BitemporalIngestStore(tmp_path / "ingest.sqlite3") as opened:
            yield opened

    # ------------------------------------------------------------------ contract

    @pytest.mark.unit
    def test_adapter_satisfies_the_protocol_at_runtime(
        self, adapter: NseIngestSourceAdapter
    ) -> None:
        """A bare `Protocol` annotation is erased at runtime and checks nothing — a
        mistake already made once in this project and caught by review."""
        assert isinstance(adapter, NseIngestSourceAdapter)
        assert adapter.source_name
        assert adapter.source_name == adapter.source_name.lower()
        assert " " not in adapter.source_name

    @pytest.mark.unit
    def test_coverage_floor_is_evidenced_not_asserted(
        self, adapter: NseIngestSourceAdapter
    ) -> None:
        """`R.17`: a floor is a claim, and a claim needs its provenance beside it."""
        floor = adapter.coverage_floor
        assert floor.earliest_date <= datetime.now(UTC).date()
        assert floor.established_by.strip(), "coverage floor carries no evidence"

    # -------------------------------------------------------------------- parsing

    @pytest.mark.unit
    def test_real_payloads_parse_to_the_expected_row_count(
        self, adapter: NseIngestSourceAdapter
    ) -> None:
        samples = self.sample_payloads()
        assert samples, "an adapter must certify against at least one real payload"
        for target, payload, expected_rows in samples:
            rows = adapter.parse(payload, target)
            assert len(rows) == expected_rows, f"{target.url} parsed {len(rows)} rows"

    @pytest.mark.unit
    def test_parsed_rows_are_well_formed(self, adapter: NseIngestSourceAdapter) -> None:
        for target, payload, _ in self.sample_payloads():
            rows = adapter.parse(payload, target)
            for row in rows:
                assert isinstance(row, IngestRow)
                assert row.natural_key, "a row must be uniquely identifiable"
                # At least one part must carry information. Deliberately NOT "every
                # part": a composite key across a segment with optional dimensions has
                # legitimately empty components — a cash equity has no expiry, strike or
                # option type, while an option in the same schema has all three. The
                # property that matters is informativeness plus uniqueness, checked
                # below, and an earlier version of this clause demanded every part be
                # non-empty and so rejected a correct adapter.
                assert any(str(part).strip() for part in row.natural_key), (
                    "a natural key with no non-empty part identifies nothing — every "
                    f"row of {adapter.source_name} would collide into one"
                )
                assert row.values, "a row with no values carries no information"
                assert row.effective_date >= adapter.coverage_floor.earliest_date

            # The real guarantee: within one payload no two rows share a key. A key too
            # coarse to separate them makes the store treat siblings as revisions of one
            # another and silently keep only the last — which for an option chain means
            # discarding hundreds of contracts per underlying per day.
            keys_by_date: dict[object, set[tuple[str, ...]]] = {}
            for row in rows:
                seen = keys_by_date.setdefault(row.effective_date, set())
                assert row.natural_key not in seen, (
                    f"{adapter.source_name}: natural key {row.natural_key} repeats "
                    f"within {row.effective_date} — rows would be lost as revisions"
                )
                seen.add(row.natural_key)

    @pytest.mark.adversarial
    def test_malformed_payloads_raise_rather_than_yielding_rows(
        self, adapter: NseIngestSourceAdapter
    ) -> None:
        """Zero rows is a legitimate answer for a holiday and NEVER for a corrupt file.
        An adapter that conflates them lets a broken parser pass as a quiet day."""
        for name, payload in MALFORMED_PAYLOADS:
            target = FetchTarget(url=f"test://{name}", source_name=adapter.source_name)
            # Deliberately NOT `pytest.raises`: raising the assertion inside that block
            # would be caught by the very context manager meant to detect the adapter
            # raising, so the clause would pass whatever the adapter did. Guarding the
            # guard caught this in the suite itself.
            try:
                rows = adapter.parse(payload, target)
            except Exception:  # noqa: BLE001 — any refusal is acceptable; silence is not
                continue
            raise AssertionError(
                f"{adapter.source_name}: malformed payload '{name}' returned "
                f"{len(rows)} rows instead of raising"
            )

    @pytest.mark.adversarial
    def test_a_payload_for_the_wrong_date_is_detected(
        self, adapter: NseIngestSourceAdapter
    ) -> None:
        """The measured case: a request for Sunday 2026-08-09 returned Friday's file
        with HTTP 200. Nothing about the response says so, so only the adapter can tell."""
        samples = self.sample_payloads()
        target, payload, _ = samples[0]
        assert adapter.content_mismatch_reason(payload, target) is None, (
            "the adapter rejects its own real payload"
        )
        if not target.expects:
            # A source that cannot be addressed by date — a rolling file, or a master
            # list spanning decades — has nothing to disagree with, so there is no
            # wrong-date case to detect. Skipping is correct here, and stated rather
            # than silent.
            return

        impostor = FetchTarget(
            url=target.url,
            source_name=adapter.source_name,
            expects="1999-01-04",
        )
        assert adapter.content_mismatch_reason(payload, impostor) is not None, (
            "a payload for the wrong date was accepted as correct"
        )

    # ------------------------------------------------------------------- storage

    @pytest.mark.unit
    def test_re_ingesting_identical_content_is_a_no_op(
        self, adapter: NseIngestSourceAdapter, store: BitemporalIngestStore
    ) -> None:
        target, payload, _ = self.sample_payloads()[0]
        rows = adapter.parse(payload, target)
        observed = datetime.now(UTC)
        fetch_id = self._record_fetch(store, adapter, observed)

        first = store.ingest_rows(adapter.source_name, rows, observed, fetch_id)
        second = store.ingest_rows(
            adapter.source_name, rows, observed + timedelta(hours=1), fetch_id
        )
        assert first.rows_inserted == len(rows)
        assert second.rows_inserted == 0, "identical content was stored twice"
        assert store.row_count(adapter.source_name) == len(rows)

    @pytest.mark.unit
    def test_changed_content_is_retained_as_a_revision(
        self, adapter: NseIngestSourceAdapter, store: BitemporalIngestStore
    ) -> None:
        """NSE revises files. A correction is a second observation, not an overwrite —
        overwriting destroys the evidence that a decision was made from the original."""
        target, payload, _ = self.sample_payloads()[0]
        rows = list(adapter.parse(payload, target))
        observed = datetime.now(UTC)
        fetch_id = self._record_fetch(store, adapter, observed)
        store.ingest_rows(adapter.source_name, rows, observed, fetch_id)

        original = rows[0]
        revised = IngestRow(
            values={**dict(original.values), "__conformance_revision__": "changed"},
            effective_date=original.effective_date,
            natural_key=original.natural_key,
        )
        later = observed + timedelta(days=1)
        result = store.ingest_rows(adapter.source_name, [revised], later, fetch_id)
        assert result.rows_inserted == 1
        assert result.revisions_recorded == 1

        history = store.all_observations_for(
            adapter.source_name, original.effective_date, original.natural_key
        )
        assert len(history) == EXPECTED_OBSERVATIONS_AFTER_A_REVISION, (
            "the original observation was destroyed"
        )

        as_known_before = store.rows_for(
            adapter.source_name, original.effective_date, known_by=observed
        )
        assert all(
            "__conformance_revision__" not in observation.values
            for observation in as_known_before
        ), "a later revision leaked into an earlier point in time"

    @pytest.mark.adversarial
    def test_a_failed_ingest_leaves_the_store_untouched(
        self, adapter: NseIngestSourceAdapter, store: BitemporalIngestStore
    ) -> None:
        """A partial ingest leaves an effective date half-populated with nothing saying
        so, and every later reader treats the fragment as the whole truth."""
        target, payload, _ = self.sample_payloads()[0]
        rows = list(adapter.parse(payload, target))
        observed = datetime.now(UTC)
        fetch_id = self._record_fetch(store, adapter, observed)
        store.ingest_rows(adapter.source_name, rows, observed, fetch_id)
        count_before = store.row_count(adapter.source_name)

        impossible = IngestRow(
            values={"anything": 1},
            effective_date=datetime.now(UTC).date() + timedelta(days=365),
            natural_key=("future",),
        )
        with pytest.raises(BitemporalIngestStoreError):
            store.ingest_rows(
                adapter.source_name, [*rows, impossible], observed, fetch_id
            )
        assert store.row_count(adapter.source_name) == count_before

    @pytest.mark.unit
    def test_every_stored_row_traces_to_a_recorded_fetch(
        self, adapter: NseIngestSourceAdapter, store: BitemporalIngestStore
    ) -> None:
        """Provenance completeness: a row whose origin is unknown is a row that cannot
        be re-derived, audited, or trusted after the fact (`L0.12`)."""
        target, payload, _ = self.sample_payloads()[0]
        rows = adapter.parse(payload, target)
        observed = datetime.now(UTC)
        fetch_id = self._record_fetch(store, adapter, observed)
        store.ingest_rows(adapter.source_name, rows, observed, fetch_id)

        recorded_ids = {record["fetch_id"] for record in store.fetch_history(adapter.source_name)}
        for effective_date in store.effective_dates_present(adapter.source_name):
            for observation in store.rows_for(adapter.source_name, effective_date):
                assert observation.fetch_id in recorded_ids
                assert observation.observed_at.tzinfo is not None, "naive timestamp stored"

    @pytest.mark.adversarial
    def test_a_row_before_the_coverage_floor_is_refused(
        self, adapter: NseIngestSourceAdapter, store: BitemporalIngestStore
    ) -> None:
        floor = adapter.coverage_floor.earliest_date
        observed = datetime.now(UTC)
        fetch_id = self._record_fetch(store, adapter, observed)
        impossible = IngestRow(
            values={"anything": 1},
            effective_date=floor - timedelta(days=1),
            natural_key=("too-early",),
        )
        with pytest.raises(BitemporalIngestStoreError, match="coverage floor"):
            store.ingest_rows(
                adapter.source_name, [impossible], observed, fetch_id, coverage_floor=floor
            )

    @staticmethod
    def _record_fetch(
        store: BitemporalIngestStore,
        adapter: NseIngestSourceAdapter,
        observed: datetime,
    ) -> int:
        return store.record_fetch(
            source_name=adapter.source_name,
            url="test://conformance",
            fetched_at=observed,
            fetch_status="retrieved",
            evidence="conformance suite",
            attempts=1,
        )
