"""Wave 1 adapters certified through the conformance suite, on REAL captured payloads.

Every payload here is bytes fetched from the live NSE archive on 2026-08-11 and stored
under `tests/fixtures_nse_ingest/`. The conformance suite demands real bytes rather than
hand-written fixtures for a reason: a fixture proves the parser reads what I imagined,
and the whole risk is that NSE sends something else.

Two adapters, chosen to stress opposite ends of the contract — the bhavcopy has two
schema eras and 33,601 rows in a single F&O file, the ban list has no archive at all and
dates itself in a prose header.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.fo_ban_list_adapter import (
    FO_BAN_LIST_URL,
    FoBanListAdapter,
    parse_ban_list_trade_date,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_bhavcopy_adapter import (
    CASH_MARKET,
    FO_MARKET,
    LEGACY_LAST_DAY,
    UDIFF_FIRST_DAY,
    NseBhavcopyAdapter,
    legacy_bhavcopy_url,
    udiff_bhavcopy_url,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

UDIFF_CASH_DAY = date(2026, 8, 10)
LEGACY_CASH_DAY = date(2020, 1, 2)
UDIFF_CASH_ROWS = 3564
UDIFF_FO_ROWS = 33601
LEGACY_CASH_ROWS = 1956
BAN_LIST_ROWS = 2
BAN_LIST_DAY = date(2026, 8, 11)
NATURAL_KEY_PARTS_UDIFF = 6
EXPECTED_OVERLAP_TARGETS = 2
MINIMUM_NIFTY_CONTRACTS = 100


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(url: str, expects: str, source: str) -> FetchTarget:
    return FetchTarget(url=url, source_name=source, expects=expects)


class TestCashBhavcopyAdapterCertifies(NseIngestAdapterConformance):
    """Cash bhavcopy, certified on both eras' real files."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return NseBhavcopyAdapter(CASH_MARKET)

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(
                    udiff_bhavcopy_url(CASH_MARKET, UDIFF_CASH_DAY),
                    UDIFF_CASH_DAY.isoformat(),
                    "nse_bhavcopy_cash",
                ),
                _fixture("udiff_cash_20260810.csv.zip"),
                UDIFF_CASH_ROWS,
            ),
            (
                _target(
                    legacy_bhavcopy_url(CASH_MARKET, LEGACY_CASH_DAY),
                    LEGACY_CASH_DAY.isoformat(),
                    "nse_bhavcopy_cash",
                ),
                _fixture("legacy_cash_02JAN2020.csv.zip"),
                LEGACY_CASH_ROWS,
            ),
        ]


class TestFoBhavcopyAdapterCertifies(NseIngestAdapterConformance):
    """F&O bhavcopy — 33,601 real rows, where the natural key must separate contracts."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return NseBhavcopyAdapter(FO_MARKET)

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(
                    udiff_bhavcopy_url(FO_MARKET, UDIFF_CASH_DAY),
                    UDIFF_CASH_DAY.isoformat(),
                    "nse_bhavcopy_fo",
                ),
                _fixture("udiff_fo_20260810.csv.zip"),
                UDIFF_FO_ROWS,
            )
        ]


class TestFoBanListAdapterCertifies(NseIngestAdapterConformance):
    """The rolling ban list, certified on the real 66-byte file."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return FoBanListAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(FO_BAN_LIST_URL, "", "fo_ban_list"),
                _fixture("fo_secban.csv"),
                BAN_LIST_ROWS,
            )
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_the_era_boundary_offers_both_urls_only_in_the_measured_overlap() -> None:
    """Legacy 404s from 2024-07-08 and UDiFF 404s before 2024-07-01, so the eras really
    do overlap — measured while acquiring the archive, not assumed."""
    adapter = NseBhavcopyAdapter(CASH_MARKET)
    before = adapter.fetch_targets([date(2020, 1, 2)])
    inside = adapter.fetch_targets([date(2024, 7, 3)])
    after = adapter.fetch_targets([date(2026, 8, 10)])
    assert len(before) == 1 and "historical" in before[0].url
    assert len(inside) == EXPECTED_OVERLAP_TARGETS, "the measured overlap must offer both eras"
    assert len(after) == 1 and "BhavCopy_NSE_CM" in after[0].url
    assert UDIFF_FIRST_DAY <= LEGACY_LAST_DAY, "the overlap is real, not a gap"


@pytest.mark.unit
def test_dates_before_the_coverage_floor_are_not_requested() -> None:
    assert NseBhavcopyAdapter(FO_MARKET).fetch_targets([date(1999, 1, 4)]) == []


@pytest.mark.adversarial
def test_a_stale_file_is_rejected_by_the_content_check() -> None:
    """The measured trap, applied to the real Monday file: presented as Sunday's, it
    must be refused on the date inside the ZIP."""
    adapter = NseBhavcopyAdapter(CASH_MARKET)
    payload = _fixture("udiff_cash_20260810.csv.zip")
    sunday = _target(
        udiff_bhavcopy_url(CASH_MARKET, date(2026, 8, 9)), "2026-08-09", "nse_bhavcopy_cash"
    )
    reason = adapter.content_mismatch_reason(payload, sunday)
    assert reason is not None
    assert "2026-08-10" in reason


@pytest.mark.unit
def test_the_real_monday_file_passes_its_own_content_check() -> None:
    adapter = NseBhavcopyAdapter(CASH_MARKET)
    target = _target(
        udiff_bhavcopy_url(CASH_MARKET, UDIFF_CASH_DAY),
        UDIFF_CASH_DAY.isoformat(),
        "nse_bhavcopy_cash",
    )
    assert adapter.content_mismatch_reason(_fixture("udiff_cash_20260810.csv.zip"), target) is None


@pytest.mark.adversarial
def test_option_contracts_do_not_collapse_into_one_row() -> None:
    """One underlying has hundreds of contracts in a day. A symbol-only natural key
    would collapse an entire option chain into one row and discard the rest as
    duplicates — silently, since the store treats a repeated key as a revision."""
    adapter = NseBhavcopyAdapter(FO_MARKET)
    target = _target(
        udiff_bhavcopy_url(FO_MARKET, UDIFF_CASH_DAY),
        UDIFF_CASH_DAY.isoformat(),
        "nse_bhavcopy_fo",
    )
    rows = adapter.parse(_fixture("udiff_fo_20260810.csv.zip"), target)
    keys = {row.natural_key for row in rows}
    assert len(keys) == len(rows), "natural keys collide — contracts would be lost"

    nifty_keys = {key for key in keys if key[0] == "NIFTY"}
    assert len(nifty_keys) > MINIMUM_NIFTY_CONTRACTS, "NIFTY's option chain collapsed"
    assert all(len(key) == NATURAL_KEY_PARTS_UDIFF for key in keys)


@pytest.mark.unit
def test_every_bhavcopy_row_is_dated_from_inside_the_file() -> None:
    adapter = NseBhavcopyAdapter(CASH_MARKET)
    target = _target(
        udiff_bhavcopy_url(CASH_MARKET, UDIFF_CASH_DAY),
        UDIFF_CASH_DAY.isoformat(),
        "nse_bhavcopy_cash",
    )
    rows = adapter.parse(_fixture("udiff_cash_20260810.csv.zip"), target)
    assert {row.effective_date for row in rows} == {UDIFF_CASH_DAY}


@pytest.mark.unit
def test_the_legacy_era_dates_and_keys_correctly() -> None:
    adapter = NseBhavcopyAdapter(CASH_MARKET)
    target = _target(
        legacy_bhavcopy_url(CASH_MARKET, LEGACY_CASH_DAY),
        LEGACY_CASH_DAY.isoformat(),
        "nse_bhavcopy_cash",
    )
    rows = adapter.parse(_fixture("legacy_cash_02JAN2020.csv.zip"), target)
    assert {row.effective_date for row in rows} == {LEGACY_CASH_DAY}
    assert ("20MICRONS", "EQ") in {row.natural_key for row in rows}


# ---------------------------------------------------------------- ban list


@pytest.mark.unit
def test_the_ban_list_dates_itself_from_its_prose_header() -> None:
    """The URL carries no date, so this header is the only evidence of which day a
    snapshot belongs to."""
    assert parse_ban_list_trade_date(_fixture("fo_secban.csv")) == BAN_LIST_DAY


@pytest.mark.adversarial
def test_an_undated_ban_list_is_refused_rather_than_dated_today() -> None:
    """Guessing would silently attribute one day's bans to another — and bans change
    what may be traded, so a misattributed list is a wrong trading decision."""
    with pytest.raises(IngestAdapterError, match="dates itself"):
        parse_ban_list_trade_date(b"1,BANDHANBNK\n2,SAIL\n")


@pytest.mark.unit
def test_an_empty_ban_list_is_valid_not_broken() -> None:
    """No stock in ban is a real and common state. Judged on the header, never on the
    row count — otherwise a quiet day looks identical to a broken parser."""
    adapter = FoBanListAdapter()
    header_only = b"Securities in Ban For Trade Date 11-AUG-2026:\n"
    rows = adapter.parse(header_only, _target(FO_BAN_LIST_URL, "", "fo_ban_list"))
    assert rows == []


@pytest.mark.unit
def test_the_real_ban_list_names_the_real_securities() -> None:
    adapter = FoBanListAdapter()
    rows = adapter.parse(_fixture("fo_secban.csv"), _target(FO_BAN_LIST_URL, "", "fo_ban_list"))
    assert {row.values["symbol"] for row in rows} == {"BANDHANBNK", "SAIL"}
    assert all(row.effective_date == BAN_LIST_DAY for row in rows)


@pytest.mark.adversarial
def test_a_garbled_ban_list_row_raises(tmp_path: Path) -> None:
    adapter = FoBanListAdapter()
    payload = b"Securities in Ban For Trade Date 11-AUG-2026:\nthis is not a row\n"
    with pytest.raises(IngestAdapterError, match="unparseable ban-list row"):
        adapter.parse(payload, _target(FO_BAN_LIST_URL, "", "fo_ban_list"))


@pytest.mark.unit
def test_the_rolling_source_cannot_serve_a_past_date() -> None:
    """Constructing a plausible URL for a past date would return TODAY's file labelled
    as that date. Refusing keeps the impossibility visible."""
    adapter = FoBanListAdapter()
    targets = adapter.fetch_targets([date(2026, 8, 1), date(2026, 8, 2)])
    assert len(targets) == 1
    assert targets[0].url == FO_BAN_LIST_URL
    reason = adapter.content_mismatch_reason(
        _fixture("fo_secban.csv"), _target(FO_BAN_LIST_URL, "2026-08-01", "fo_ban_list")
    )
    assert reason is not None
    assert "rolling file" in reason
