"""`mwpl_position_limits` (`L0.24`), certified through the shared conformance suite plus
tests for what is peculiar to this source: the undiscovered `combineoi_` successor feed,
the broken-XML-so-CSV-is-authoritative decision, and the `Limit for Next Day` ban marker.

Every payload here is real bytes fetched live from `nsearchives.nseindia.com` on
2026-08-11 and stored under `tests/fixtures_nse_ingest/` — never hand-written, per the
conformance suite's own requirement that a fixture prove the parser reads what NSE
actually sends rather than what was imagined.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ElementTree

# fixture bytes fetched from NSE ourselves (never attacker-supplied input); used solely
# to prove the XML sibling is malformed, which is exactly why the adapter under test
# never parses XML in production. No new dependency added per the wave-2 brief's hard
# rule 3 — `defusedxml` is not installed and this path carries no XXE exposure.
import zipfile
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.mwpl_position_limits_adapter import (
    MWPL_COVERAGE_FLOOR,
    MwplPositionLimitsAdapter,
    combineoi_mwpl_url,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

CURRENT_DAY = date(2026, 8, 10)
EARLIEST_DAY = date(2011, 1, 3)
CURRENT_ROWS = 208
EARLIEST_ROWS = 224
BANNED_SYMBOLS = {"BANDHANBNK", "SAIL"}
BAN_MARKER = "No Fresh Positions"


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(day: date) -> FetchTarget:
    return FetchTarget(
        url=combineoi_mwpl_url(day),
        source_name="mwpl_position_limits",
        expects=day.isoformat(),
    )


class TestMwplPositionLimitsAdapterCertifies(NseIngestAdapterConformance):
    """Certified on real files from both ends of the schema range: the 2011-era 7-column
    file and the current 8-column file (`Future Equivalent Open Interest` added)."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return MwplPositionLimitsAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(CURRENT_DAY),
                _fixture("mwpl_combineoi_10082026.zip"),
                CURRENT_ROWS,
            ),
            (
                _target(EARLIEST_DAY),
                _fixture("mwpl_combineoi_03012011.zip"),
                EARLIEST_ROWS,
            ),
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_only_combineoi_is_offered_not_the_dead_end_nseoi_name() -> None:
    """The recon's reported gap was that no successor to `nseoi_` was found. This adapter
    found one at the same path under a different filename (`combineoi_`) and uses it
    exclusively: offering both names as separate targets would make the runner fetch and
    ingest both, and the store would record the second as a spurious "revision" of a fact
    that had not actually changed (module docstring)."""
    adapter = MwplPositionLimitsAdapter()
    targets = adapter.fetch_targets([CURRENT_DAY])
    assert len(targets) == 1
    assert "combineoi_" in targets[0].url
    assert "nseoi_" not in targets[0].url
    assert (
        targets[0].url
        == "https://nsearchives.nseindia.com/archives/nsccl/mwpl/combineoi_10082026.zip"
    )


@pytest.mark.unit
def test_dates_before_the_verified_floor_are_not_requested() -> None:
    adapter = MwplPositionLimitsAdapter()
    assert adapter.fetch_targets([MWPL_COVERAGE_FLOOR - timedelta(days=1)]) == []
    assert len(adapter.fetch_targets([MWPL_COVERAGE_FLOOR])) == 1


@pytest.mark.unit
def test_every_row_is_dated_from_inside_the_file_both_schema_eras() -> None:
    adapter = MwplPositionLimitsAdapter()
    current_rows = adapter.parse(_fixture("mwpl_combineoi_10082026.zip"), _target(CURRENT_DAY))
    earliest_rows = adapter.parse(_fixture("mwpl_combineoi_03012011.zip"), _target(EARLIEST_DAY))
    assert {row.effective_date for row in current_rows} == {CURRENT_DAY}
    assert {row.effective_date for row in earliest_rows} == {EARLIEST_DAY}


@pytest.mark.unit
def test_the_2011_era_file_has_no_future_equivalent_open_interest_column() -> None:
    """Direct evidence the schema really did change: the 2011 CSV has 7 columns, the
    current one has 8. `csv.DictReader` reading whatever header a payload carries (rather
    than a hard-coded column list) is what makes both eras parse without a schema flag."""
    adapter = MwplPositionLimitsAdapter()
    rows = adapter.parse(_fixture("mwpl_combineoi_03012011.zip"), _target(EARLIEST_DAY))
    assert "Future Equivalent Open Interest" not in rows[0].values
    assert "Limit for Next Day" in rows[0].values

    current_rows = adapter.parse(_fixture("mwpl_combineoi_10082026.zip"), _target(CURRENT_DAY))
    assert "Future Equivalent Open Interest" in current_rows[0].values


@pytest.mark.unit
def test_the_ban_marker_is_stored_verbatim_and_matches_the_real_ban_list() -> None:
    """`Limit for Next Day` reads the literal string "No Fresh Positions" instead of a
    number for exactly the symbols the independent `fo_ban_list` source names as banned
    the next trading day (BANDHANBNK, SAIL, 2026-08-11) — cross-source agreement between
    two different NSE files. The adapter stores the string as-is; it does not interpret,
    gate, or branch on it (`R.03`: the 95% threshold this encodes is a comment, not code)."""
    adapter = MwplPositionLimitsAdapter()
    rows = adapter.parse(_fixture("mwpl_combineoi_10082026.zip"), _target(CURRENT_DAY))
    marked_symbols = {
        row.values["NSE Symbol"]
        for row in rows
        if row.values.get("Limit for Next Day") == BAN_MARKER
    }
    assert marked_symbols == BANNED_SYMBOLS


@pytest.mark.unit
def test_the_real_monday_file_passes_its_own_content_check() -> None:
    adapter = MwplPositionLimitsAdapter()
    assert (
        adapter.content_mismatch_reason(
            _fixture("mwpl_combineoi_10082026.zip"), _target(CURRENT_DAY)
        )
        is None
    )


@pytest.mark.adversarial
def test_a_payload_claiming_a_different_date_is_rejected() -> None:
    adapter = MwplPositionLimitsAdapter()
    payload = _fixture("mwpl_combineoi_10082026.zip")
    impostor = _target(date(2026, 8, 9))
    reason = adapter.content_mismatch_reason(payload, impostor)
    assert reason is not None
    assert "2026-08-10" in reason
    assert "2026-08-09" in reason


@pytest.mark.adversarial
def test_a_zip_missing_a_required_column_raises_rather_than_yielding_rows() -> None:
    """A payload that is a real ZIP with a real CSV but the wrong schema — as opposed to
    the conformance suite's already-corrupt-at-the-byte-level malformed payloads — must
    still be refused, not silently parsed with missing fields."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("wrong.csv", "Some,Other,Columns\n1,2,3\n")
    adapter = MwplPositionLimitsAdapter()
    with pytest.raises(IngestAdapterError, match="missing required column"):
        adapter.parse(buffer.getvalue(), _target(CURRENT_DAY))


@pytest.mark.unit
def test_the_zip_carries_a_real_but_malformed_xml_sibling_csv_is_used_instead() -> None:
    """Documents the CSV-vs-XML decision with a live assertion rather than only a
    docstring claim: the real current-era XML member is not well-formed XML (a tag name
    containing spaces), while the adapter's own parse of the same ZIP succeeds because it
    never opens the XML member at all."""
    payload = _fixture("mwpl_combineoi_10082026.zip")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml_member = next(name for name in archive.namelist() if name.endswith(".xml"))
        xml_text = archive.read(xml_member).decode("utf-8", "replace")
    with pytest.raises(ElementTree.ParseError):
        ElementTree.fromstring(xml_text)

    adapter = MwplPositionLimitsAdapter()
    rows = adapter.parse(payload, _target(CURRENT_DAY))
    assert len(rows) == CURRENT_ROWS


@pytest.mark.unit
def test_natural_key_is_the_underlying_symbol_alone() -> None:
    """`natural_key` must identify one underlying on one date; `effective_date` already
    scopes the day, so the symbol alone is both necessary and sufficient here — verified
    unique across the real 208-row and 224-row files by the conformance suite's own
    within-payload uniqueness check."""
    adapter = MwplPositionLimitsAdapter()
    rows = adapter.parse(_fixture("mwpl_combineoi_10082026.zip"), _target(CURRENT_DAY))
    sample = next(row for row in rows if row.values["NSE Symbol"] == "SAIL")
    assert sample.natural_key == ("SAIL",)
