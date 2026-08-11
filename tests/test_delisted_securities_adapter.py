"""`L0.09` — delisted-securities master, certified on the real 328-row live payload.

The payload here is bytes fetched from `archives.nseindia.com/content/equities/
delisted.csv` on 2026-08-11 and stored under `tests/fixtures_nse_ingest/
delisted_securities_master.csv`. A second real fixture, NSE's live-listed-securities
file (`EQUITY_L.csv` — a genuinely similar neighbour on the same archive host), backs
the adapter's own test that `content_mismatch_reason` catches the "right host, wrong
file" case that a URL-only check could never see.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.delisted_securities_adapter import (
    DELISTED_SECURITIES_COVERAGE_FLOOR,
    DELISTED_SECURITIES_MASTER_URL,
    DelistedSecuritiesAdapter,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

DELISTED_MASTER_ROWS = 328
EARLIEST_DELISTING_IN_LIVE_FILE = date(2002, 4, 15)
LATEST_DELISTING_IN_LIVE_FILE = date(2020, 11, 9)
REDELISTED_SYMBOL_OBSERVATION_COUNT = 2


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(expects: str = "") -> FetchTarget:
    return FetchTarget(
        url=DELISTED_SECURITIES_MASTER_URL,
        source_name="delisted_securities_master",
        expects=expects,
    )


class TestDelistedSecuritiesAdapterCertifies(NseIngestAdapterConformance):
    """The shared eight-property suite, run against the real 328-row live master."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return DelistedSecuritiesAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(),
                _fixture("delisted_securities_master.csv"),
                DELISTED_MASTER_ROWS,
            )
        ]


# ---------------------------------------------------------------- beyond the contract


@pytest.mark.unit
def test_the_coverage_floor_matches_a_full_column_scan_of_the_real_file() -> None:
    assert DELISTED_SECURITIES_COVERAGE_FLOOR == EARLIEST_DELISTING_IN_LIVE_FILE


@pytest.mark.unit
def test_every_row_carries_its_own_historical_delisting_date_not_a_fetch_date() -> None:
    """The load-bearing structural fact about this source: `effective_date` is read
    per-row, and the whole file's dates span decades in one payload."""
    adapter = DelistedSecuritiesAdapter()
    rows = adapter.parse(_fixture("delisted_securities_master.csv"), _target())
    dates = {row.effective_date for row in rows}
    assert min(dates) == EARLIEST_DELISTING_IN_LIVE_FILE
    assert max(dates) == LATEST_DELISTING_IN_LIVE_FILE
    assert len(dates) > 1, "a single-file source spanning one date would defeat the point"


@pytest.mark.unit
def test_trailing_empty_columns_are_ignored() -> None:
    """The real header is nine comma-separated fields; only the first four carry data."""
    adapter = DelistedSecuritiesAdapter()
    rows = adapter.parse(_fixture("delisted_securities_master.csv"), _target())
    hexaware = next(row for row in rows if row.values["symbol"] == "HEXAWARE")
    assert hexaware.values["company"] == "Hexaware Technologies Limited"
    assert hexaware.effective_date == date(2020, 11, 9)
    assert set(hexaware.values) == {"symbol", "company", "delisted_date", "type_of_delisting"}


@pytest.mark.adversarial
def test_a_row_with_a_newline_embedded_inside_a_quoted_field_still_parses() -> None:
    """Row 204 of the real file is `"EMTEXIND\\n",Emtex Industries...` — NSE's own
    generator left a raw newline inside a quoted Symbol field. A naive line-split
    parser would shred this into two garbage rows; `csv.reader` must reassemble it."""
    adapter = DelistedSecuritiesAdapter()
    rows = adapter.parse(_fixture("delisted_securities_master.csv"), _target())
    symbols = {row.values["symbol"] for row in rows}
    assert "EMTEXIND" in symbols
    emtex = next(row for row in rows if row.values["symbol"] == "EMTEXIND")
    assert emtex.values["company"].startswith("Emtex Industries")
    assert emtex.effective_date == date(2018, 3, 26)


@pytest.mark.unit
def test_two_digit_years_resolve_into_the_correct_century() -> None:
    adapter = DelistedSecuritiesAdapter()
    rows = adapter.parse(_fixture("delisted_securities_master.csv"), _target())
    cabot = next(row for row in rows if row.values["symbol"] == "CABOTINDIA")
    assert cabot.effective_date == date(2002, 4, 15)


@pytest.mark.adversarial
def test_a_different_real_nse_file_on_the_same_host_is_rejected_as_a_mismatch() -> None:
    """`EQUITY_L.csv` — the currently-LISTED securities master — lives on the very same
    archive host and is a real, well-formed, HTTP-200 CSV. A check that only asked "did
    I get 200 and some CSV back" would wave this through; the header check must not."""
    adapter = DelistedSecuritiesAdapter()
    wrong_file = _fixture("equity_l_listed_securities.csv")
    reason = adapter.content_mismatch_reason(wrong_file, _target())
    assert reason is not None
    assert "delisted-securities header" in reason
    with pytest.raises(IngestAdapterError):
        adapter.parse(wrong_file, _target())


@pytest.mark.unit
def test_the_real_payload_passes_its_own_content_check() -> None:
    adapter = DelistedSecuritiesAdapter()
    assert (
        adapter.content_mismatch_reason(_fixture("delisted_securities_master.csv"), _target())
        is None
    )


@pytest.mark.unit
def test_a_caller_naming_a_specific_date_is_honestly_refused_not_guessed() -> None:
    """This source cannot confirm or deny content for any one day — it is one
    cumulative file. Silently answering `None` for a date-specific ask would imply a
    per-date guarantee that does not exist."""
    adapter = DelistedSecuritiesAdapter()
    reason = adapter.content_mismatch_reason(
        _fixture("delisted_securities_master.csv"), _target(expects="2020-11-09")
    )
    assert reason is not None
    assert "cannot confirm or deny" in reason


@pytest.mark.unit
def test_fetch_targets_returns_the_one_url_regardless_of_how_many_dates_are_asked() -> None:
    adapter = DelistedSecuritiesAdapter()
    targets = adapter.fetch_targets([date(2020, 1, 1), date(2021, 1, 1), date(2022, 1, 1)])
    assert len(targets) == 1
    assert targets[0].url == DELISTED_SECURITIES_MASTER_URL
    assert targets[0].expects == ""
    assert adapter.fetch_targets([]) == []


@pytest.mark.unit
def test_no_duplicate_symbols_in_the_real_live_file_today() -> None:
    """Documents a real, currently-true fact about the live data (`research/207`'s own
    scan did not check this exhaustively; this build's full scan did): as of this
    fetch, no symbol appears twice. The adapter does not *rely* on this staying true —
    see `test_two_delistings_of_the_same_symbol_on_different_dates_do_not_collide` —
    but it is worth pinning as a fact about today's archive."""
    adapter = DelistedSecuritiesAdapter()
    rows = adapter.parse(_fixture("delisted_securities_master.csv"), _target())
    symbols = [row.values["symbol"] for row in rows]
    assert len(symbols) == len(set(symbols))


@pytest.mark.unit
def test_two_delistings_of_the_same_symbol_on_different_dates_do_not_collide() -> None:
    """A symbol delisted, later relisted, and delisted again is two real, distinct
    facts. The natural key is scoped to `(symbol,)` deliberately — uniqueness is
    required only within one `effective_date`, matching every other adapter and the
    store's own key shape `(source, natural_key, effective_date, content_hash)`."""
    adapter = DelistedSecuritiesAdapter()
    payload = (
        b"Symbol,Company,Delisted Date,Type of Delisting,,,,,\r\n"
        b"REDELIST,Redelist Ltd first time,01-Jan-10,Compulsory Delisting ,,,,,\r\n"
        b"REDELIST,Redelist Ltd second time,01-Jan-15,Compulsory Delisting ,,,,,\r\n"
    )
    rows = adapter.parse(payload, _target())
    assert len(rows) == REDELISTED_SYMBOL_OBSERVATION_COUNT
    assert {row.effective_date for row in rows} == {date(2010, 1, 1), date(2015, 1, 1)}
    assert {row.natural_key for row in rows} == {("REDELIST",)}


@pytest.mark.adversarial
def test_a_true_duplicate_row_within_one_date_raises_rather_than_silently_colliding() -> None:
    payload = (
        b"Symbol,Company,Delisted Date,Type of Delisting,,,,,\r\n"
        b"DUPETICK,Dupe Ltd,01-Jan-10,Compulsory Delisting ,,,,,\r\n"
        b"DUPETICK,Dupe Ltd,01-Jan-10,Compulsory Delisting ,,,,,\r\n"
    )
    adapter = DelistedSecuritiesAdapter()
    with pytest.raises(IngestAdapterError, match="repeats within"):
        adapter.parse(payload, _target())


@pytest.mark.adversarial
def test_a_header_only_payload_is_refused_not_accepted_as_a_quiet_history() -> None:
    """Unlike `fo_ban_list`, where zero rows is a normal daily state, this source has
    never been observed with zero delistings and never legitimately would be — a
    header with no data rows is far more likely a truncated fetch than real history."""
    adapter = DelistedSecuritiesAdapter()
    header_only = b"Symbol,Company,Delisted Date,Type of Delisting,,,,,\r\n"
    with pytest.raises(IngestAdapterError, match="zero data rows"):
        adapter.parse(header_only, _target())


@pytest.mark.unit
def test_source_name_is_self_describing_and_lowercase() -> None:
    adapter = DelistedSecuritiesAdapter()
    assert adapter.source_name == "delisted_securities_master"
