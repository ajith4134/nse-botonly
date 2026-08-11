"""`index_constituents_weights` (`L0.29`), certified on real captured payloads.

Two independently real payload kinds live under one adapter: the membership CSV
(`archives.nseindia.com`, verified in `research/207` §8) and the free-float
weight-methodology JSON (`liveindexsa.niftyindices.com`, discovered while building this
adapter — see its module docstring). NIFTY 50 and NIFTY BANK were chosen because NIFTY 50's
weight feed carries the real `DUMMYHDLVR` placeholder sentinel found live on 2026-08-11,
and NIFTY BANK's does not — between them the fixtures exercise both branches of the
dummy-row handling without a single hand-written byte standing in for NSE's own format.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.index_constituents_adapter import (
    INDEX_UNIVERSE,
    IndexCatalogEntry,
    IndexConstituentsAdapter,
    membership_url,
    weight_methodology_url,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

SOURCE_NAME = "index_constituents_weights"

MEMBERSHIP_CLAIMED_DATE = "2026-08-11"
"""The date these membership snapshots are claimed as of — the day they were fetched.
The file carries no date of its own (see the adapter's module docstring), so this is
supplied by the caller/test exactly as `fetch_targets` would supply it in production."""

WEIGHT_REAL_PAYLOAD_DATE = "2026-01-08"
"""The date genuinely embedded inside the real weight-JSON fixtures' `time` field —
independently confirmed by the HTTP `Last-Modified` header on the live fetch. Not today:
that staleness is the whole point of one of the tests below."""

NIFTY_50_MEMBERSHIP_ROWS = 50
NIFTY_BANK_MEMBERSHIP_ROWS = 14
NIFTY_50_WEIGHT_ROWS = 50
"""51 raw JSON records minus the one `DUMMYHDLVR` placeholder sentinel."""
NIFTY_BANK_WEIGHT_ROWS = 14
INDEX_UNIVERSE_SIZE = 33
TARGETS_PER_INDEX = 2


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _nifty50_entry() -> IndexCatalogEntry:
    return next(e for e in INDEX_UNIVERSE if e.index_name == "NIFTY 50")


def _niftybank_entry() -> IndexCatalogEntry:
    return next(e for e in INDEX_UNIVERSE if e.index_name == "NIFTY BANK")


class TestIndexConstituentsAdapterCertifies(NseIngestAdapterConformance):
    """Both payload kinds, all four real fixtures, in one conformance run."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return IndexConstituentsAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        nifty50 = _nifty50_entry()
        niftybank = _niftybank_entry()
        return [
            # Weight-methodology sample goes first: it is the one payload kind that
            # self-dates (a `time` field per record), so it is the sample the shared
            # `test_a_payload_for_the_wrong_date_is_detected` uses to prove a wrong
            # claimed date is actually caught. The membership CSV cannot self-date at
            # all (see module docstring) — that limitation is proven separately by
            # this file's own `test_the_membership_content_check_can_only_validate_*`.
            (
                FetchTarget(
                    url=weight_methodology_url(nifty50),
                    source_name=SOURCE_NAME,
                    expects=WEIGHT_REAL_PAYLOAD_DATE,
                ),
                _fixture("index_weight_inputs_nifty50.json"),
                NIFTY_50_WEIGHT_ROWS,
            ),
            (
                FetchTarget(
                    url=weight_methodology_url(niftybank),
                    source_name=SOURCE_NAME,
                    expects=WEIGHT_REAL_PAYLOAD_DATE,
                ),
                _fixture("index_weight_inputs_niftybank.json"),
                NIFTY_BANK_WEIGHT_ROWS,
            ),
            (
                FetchTarget(
                    url=membership_url(nifty50),
                    source_name=SOURCE_NAME,
                    expects=MEMBERSHIP_CLAIMED_DATE,
                ),
                _fixture("index_membership_nifty50.csv"),
                NIFTY_50_MEMBERSHIP_ROWS,
            ),
            (
                FetchTarget(
                    url=membership_url(niftybank),
                    source_name=SOURCE_NAME,
                    expects=MEMBERSHIP_CLAIMED_DATE,
                ),
                _fixture("index_membership_niftybank.csv"),
                NIFTY_BANK_MEMBERSHIP_ROWS,
            ),
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_the_index_universe_covers_broad_sectoral_and_thematic_indices() -> None:
    """`R.09`: not a NIFTY-50 sample. Every URL this catalog builds must be unique."""
    assert len(INDEX_UNIVERSE) == INDEX_UNIVERSE_SIZE
    names = {entry.index_name for entry in INDEX_UNIVERSE}
    assert len(names) == INDEX_UNIVERSE_SIZE, "duplicate index name in the catalog"
    membership_urls = {membership_url(entry) for entry in INDEX_UNIVERSE}
    weight_urls = {weight_methodology_url(entry) for entry in INDEX_UNIVERSE}
    assert len(membership_urls) == INDEX_UNIVERSE_SIZE
    assert len(weight_urls) == INDEX_UNIVERSE_SIZE
    for must_have in ("NIFTY 50", "NIFTY BANK", "NIFTY MIDCAP 50", "NIFTY SMALLCAP 250"):
        assert must_have in names


@pytest.mark.unit
def test_special_characters_in_index_names_are_url_encoded_as_verified_live() -> None:
    """Regression guard on the two names that are not `NAME -> %20`-only: `&` and `/`
    both round-trip exactly as verified live against `liveindexsa.niftyindices.com`."""
    oil_and_gas = next(e for e in INDEX_UNIVERSE if e.index_name == "NIFTY OIL & GAS")
    financial_25_50 = next(
        e for e in INDEX_UNIVERSE if e.index_name == "NIFTY FINANCIAL SERVICES 25/50"
    )
    assert weight_methodology_url(oil_and_gas).endswith(
        "FinalHeatmapNIFTY%20OIL%20%26%20GAS.json"
    )
    assert weight_methodology_url(financial_25_50).endswith(
        "FinalHeatmapNIFTY%20FINANCIAL%20SERVICES%2025/50.json"
    )


@pytest.mark.unit
def test_healthcare_index_uses_its_corrected_weight_feed_name() -> None:
    """The one name where the obvious guess (`"...INDEX"`) 404s and the real feed name
    differs from the membership display name."""
    healthcare = next(e for e in INDEX_UNIVERSE if e.index_name == "NIFTY HEALTHCARE INDEX")
    assert weight_methodology_url(healthcare).endswith("FinalHeatmapNIFTY%20HEALTHCARE.json")


@pytest.mark.unit
def test_fetch_targets_covers_every_index_twice_and_claims_the_latest_date() -> None:
    adapter = IndexConstituentsAdapter()
    targets = adapter.fetch_targets([date(2026, 8, 9), date(2026, 8, 11), date(2026, 8, 10)])
    assert len(targets) == INDEX_UNIVERSE_SIZE * TARGETS_PER_INDEX
    assert {t.expects for t in targets} == {"2026-08-11"}, "must claim the latest date asked"
    urls = {t.url for t in targets}
    assert len(urls) == len(targets), "every target must be a distinct URL"


@pytest.mark.unit
def test_fetch_targets_is_empty_for_no_dates() -> None:
    assert IndexConstituentsAdapter().fetch_targets([]) == []


@pytest.mark.unit
def test_the_dummy_placeholder_row_is_dropped_not_stored() -> None:
    """The real, live `DUMMYHDLVR` sentinel inside the NIFTY 50 weight feed, captured
    2026-08-11 — dropped silently, not raised on, because a placeholder slot is not
    corruption (see module docstring)."""
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(
        url=weight_methodology_url(nifty50),
        source_name=SOURCE_NAME,
        expects=WEIGHT_REAL_PAYLOAD_DATE,
    )
    rows = adapter.parse(_fixture("index_weight_inputs_nifty50.json"), target)
    symbols = {row.values["symbol"] for row in rows}
    assert "DUMMYHDLVR" not in symbols
    assert len(rows) == NIFTY_50_WEIGHT_ROWS


@pytest.mark.unit
def test_weight_rows_carry_the_raw_free_float_methodology_fields_not_a_computed_percent() -> None:
    """The adapter parses; it does not model. `weight_pct` is a downstream computation
    over `index_free_float_mcap_today`, never done here."""
    adapter = IndexConstituentsAdapter()
    niftybank = _niftybank_entry()
    target = FetchTarget(
        url=weight_methodology_url(niftybank),
        source_name=SOURCE_NAME,
        expects=WEIGHT_REAL_PAYLOAD_DATE,
    )
    rows = adapter.parse(_fixture("index_weight_inputs_niftybank.json"), target)
    hdfc = next(row for row in rows if row.values["symbol"] == "HDFCBANK")
    assert hdfc.values["record_kind"] == "free_float_weight_inputs"
    assert hdfc.values["index_free_float_mcap_today"] is not None
    assert hdfc.values["shares_outstanding"] is not None
    assert "weight_pct" not in hdfc.values
    assert "weight" not in hdfc.values


@pytest.mark.adversarial
def test_a_stale_weight_snapshot_is_rejected_by_the_content_check() -> None:
    """The measured fact this adapter's docstring documents: as of 2026-08-11 the real
    feed is frozen on 2026-01-08. Asking for today must be refused, not silently served
    January's numbers."""
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    stale_as_today = FetchTarget(
        url=weight_methodology_url(nifty50), source_name=SOURCE_NAME, expects="2026-08-11"
    )
    reason = adapter.content_mismatch_reason(
        _fixture("index_weight_inputs_nifty50.json"), stale_as_today
    )
    assert reason is not None
    assert "2026-01-08" in reason


@pytest.mark.unit
def test_the_real_weight_payload_passes_its_own_content_check() -> None:
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    honest_target = FetchTarget(
        url=weight_methodology_url(nifty50),
        source_name=SOURCE_NAME,
        expects=WEIGHT_REAL_PAYLOAD_DATE,
    )
    assert (
        adapter.content_mismatch_reason(
            _fixture("index_weight_inputs_nifty50.json"), honest_target
        )
        is None
    )


@pytest.mark.adversarial
def test_a_membership_row_is_dated_from_the_targets_claim_never_the_payload() -> None:
    """The membership file has no date field at all — every row's `effective_date` must
    equal exactly what the target claimed, since there is nothing else to derive it from."""
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(
        url=membership_url(nifty50), source_name=SOURCE_NAME, expects="2019-03-04"
    )
    rows = adapter.parse(_fixture("index_membership_nifty50.csv"), target)
    assert {row.effective_date for row in rows} == {date(2019, 3, 4)}


@pytest.mark.adversarial
def test_a_membership_target_with_no_claimed_date_is_refused() -> None:
    """Unlike `fo_ban_list`, this payload cannot state its own date — an unclaimed
    target cannot be dated at all, and guessing today would misattribute history."""
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(url=membership_url(nifty50), source_name=SOURCE_NAME, expects="")
    with pytest.raises(IngestAdapterError, match="no claimed date"):
        adapter.parse(_fixture("index_membership_nifty50.csv"), target)


@pytest.mark.unit
def test_the_membership_content_check_can_only_validate_the_claim_not_the_payload() -> None:
    """The documented, disclosed gap: this check can catch a malformed claim but can
    never catch NSE serving a stale membership list, because nothing inside the payload
    says which day it is for."""
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    payload = _fixture("index_membership_nifty50.csv")
    well_formed = FetchTarget(
        url=membership_url(nifty50), source_name=SOURCE_NAME, expects="1999-01-01"
    )
    assert adapter.content_mismatch_reason(payload, well_formed) is None, (
        "an impossible-but-well-formed date must still pass — the payload cannot refute it"
    )
    malformed_claim = FetchTarget(
        url=membership_url(nifty50), source_name=SOURCE_NAME, expects="not-a-real-date"
    )
    assert adapter.content_mismatch_reason(payload, malformed_claim) is not None


@pytest.mark.adversarial
def test_an_unrecognised_url_is_refused_even_with_a_well_formed_payload() -> None:
    """A real, valid membership CSV fetched from a URL this adapter's catalog never
    constructed cannot be attributed to an index — refused rather than guessed."""
    adapter = IndexConstituentsAdapter()
    rogue_target = FetchTarget(
        url="https://archives.nseindia.com/content/indices/ind_doesnotexistlist.csv",
        source_name=SOURCE_NAME,
        expects=MEMBERSHIP_CLAIMED_DATE,
    )
    with pytest.raises(IngestAdapterError, match="not a URL this adapter's catalog"):
        adapter.parse(_fixture("index_membership_nifty50.csv"), rogue_target)


@pytest.mark.adversarial
def test_a_weight_payload_of_only_placeholder_rows_raises() -> None:
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(
        url=weight_methodology_url(nifty50),
        source_name=SOURCE_NAME,
        expects=WEIGHT_REAL_PAYLOAD_DATE,
    )
    all_dummy = (
        b'[{"symbol": "DUMMYFOO", "time": "0", "sector": "", "sharesOutstanding": 0.0, '
        b'"investableWeightFactor": 0.0, "cappingFactor": 0.0, "dayEndClose": 0.0, '
        b'"Indexmcap_today": 0.0, "Indexmcap_yst": 0.0}]'
    )
    with pytest.raises(IngestAdapterError, match="placeholder/dummy"):
        adapter.parse(all_dummy, target)


@pytest.mark.adversarial
def test_a_weight_record_missing_its_symbol_raises() -> None:
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(
        url=weight_methodology_url(nifty50),
        source_name=SOURCE_NAME,
        expects=WEIGHT_REAL_PAYLOAD_DATE,
    )
    no_symbol = b'[{"time": "Jan 08, 2026 16:00:29", "sector": "Financial Services"}]'
    with pytest.raises(IngestAdapterError, match="no symbol"):
        adapter.parse(no_symbol, target)


@pytest.mark.adversarial
def test_a_membership_file_with_the_wrong_header_raises() -> None:
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    target = FetchTarget(
        url=membership_url(nifty50), source_name=SOURCE_NAME, expects=MEMBERSHIP_CLAIMED_DATE
    )
    wrong_header = b"Symbol,Company Name\nADANIENT,Adani Enterprises Ltd.\n"
    with pytest.raises(IngestAdapterError, match="unexpected membership header"):
        adapter.parse(wrong_header, target)


@pytest.mark.unit
def test_natural_key_identifies_one_symbol_within_one_index() -> None:
    adapter = IndexConstituentsAdapter()
    nifty50 = _nifty50_entry()
    niftybank = _niftybank_entry()
    membership_target = FetchTarget(
        url=membership_url(nifty50), source_name=SOURCE_NAME, expects=MEMBERSHIP_CLAIMED_DATE
    )
    rows = adapter.parse(_fixture("index_membership_nifty50.csv"), membership_target)
    assert ("NIFTY 50", "ADANIENT") in {row.natural_key for row in rows}

    bank_target = FetchTarget(
        url=membership_url(niftybank), source_name=SOURCE_NAME, expects=MEMBERSHIP_CLAIMED_DATE
    )
    bank_rows = adapter.parse(_fixture("index_membership_niftybank.csv"), bank_target)
    assert ("NIFTY BANK", "HDFCBANK") in {row.natural_key for row in bank_rows}
    # The same symbol in two different indices is two different facts, never a collision.
    assert ("NIFTY 50", "HDFCBANK") != ("NIFTY BANK", "HDFCBANK")
