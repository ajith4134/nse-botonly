"""Certification for `circuit_band_asm_gsm` — circuit band, GSM stage, and ASM stage.

Payloads are REAL bytes fetched live from NSE on 2026-08-11 and stored under
`tests/fixtures_nse_ingest/`:

- `sec_list.csv` — 3,335 real securities, verified reachable per `research/207` §7.
- `report_asm.json` — the live `reportASM` payload this adapter discovered by reading
  the real `asm.js` bundle (see the adapter's module docstring). `research/207` reported
  ASM as blocked; this fixture is the evidence that it is not.

Beyond the shared conformance suite, this file tests what is peculiar to this source:
the two payloads' very different ability to self-date, the raw (non-normalised) circuit
band values including the literal `"No Band"`, GSM stage extraction from free text, and
a real duplicate-row anomaly found in the live ASM payload (`DCI` listed twice under
short-term ASM with a casing-only company-name difference).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.circuit_band_surveillance_adapter import (
    REPORT_ASM_URL,
    SEC_LIST_URL,
    CircuitBandSurveillanceAdapter,
    _circuit_band_percent,
    _extract_gsm_stage,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import MALFORMED_PAYLOADS, NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

FIXTURE_DATE = date(2026, 8, 11)
SEC_LIST_ROW_COUNT = 3335
ASM_ROW_COUNT = 189  # 128 long-term + 61 short-term entries, including the real DCI duplicate
ASM_LONG_TERM_SYMBOLS = 128
ASM_SHORT_TERM_ENTRIES = 61
ASM_SHORT_TERM_UNIQUE_SYMBOLS = 60


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(url: str, expects: str) -> FetchTarget:
    return FetchTarget(url=url, source_name="circuit_band_asm_gsm", expects=expects)


class TestCircuitBandAsmGsmAdapterCertifies(NseIngestAdapterConformance):
    """Both real payloads, certified through the shared contract."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return CircuitBandSurveillanceAdapter()

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        # ASM first: the shared suite's wrong-date test only exercises `samples[0]`, and
        # only the ASM half can prove it via its self-declared `asmTime` — see module
        # docstring for why the circuit-band/GSM half structurally cannot, which is
        # certified separately by this file's own adversarial tests below instead.
        return [
            (
                _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat()),
                _fixture("report_asm.json"),
                ASM_ROW_COUNT,
            ),
            (
                _target(SEC_LIST_URL, FIXTURE_DATE.isoformat()),
                _fixture("sec_list.csv"),
                SEC_LIST_ROW_COUNT,
            ),
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_fetch_targets_offers_both_sources_for_the_requested_date() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    targets = adapter.fetch_targets([date(2026, 8, 1), FIXTURE_DATE])
    urls = {target.url for target in targets}
    assert urls == {SEC_LIST_URL, REPORT_ASM_URL}
    # The most recent requested date is what both targets claim — neither source can
    # serve a specific past date, so "most recent asked" is the only sensible choice.
    assert all(target.expects == FIXTURE_DATE.isoformat() for target in targets)


@pytest.mark.unit
def test_fetch_targets_returns_nothing_for_no_dates() -> None:
    assert CircuitBandSurveillanceAdapter().fetch_targets([]) == []


@pytest.mark.unit
def test_fetch_targets_returns_nothing_before_the_coverage_floor() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    assert adapter.fetch_targets([date(1990, 1, 1)]) == []


# ---------------------------------------------------------- circuit band + GSM


@pytest.mark.unit
def test_circuit_band_percent_parses_numeric_bands_and_preserves_no_band() -> None:
    assert _circuit_band_percent("20") == 20
    assert _circuit_band_percent("2") == 2
    assert _circuit_band_percent("No Band") is None


@pytest.mark.unit
def test_no_band_rows_keep_the_raw_text_and_a_none_percent() -> None:
    """`research/207`/the brief: 'No Band' must never be normalised into a number."""
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(SEC_LIST_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("sec_list.csv"), target)
    by_symbol = {row.values["symbol"]: row for row in rows}
    abb = by_symbol["ABB"]
    assert abb.values["circuit_band_raw"] == "No Band"
    assert abb.values["circuit_band_percent"] is None


@pytest.mark.unit
def test_numeric_band_rows_carry_both_the_raw_text_and_the_parsed_percent() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(SEC_LIST_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("sec_list.csv"), target)
    by_symbol = {row.values["symbol"]: row for row in rows}
    ansalapi = by_symbol["ANSALAPI"]
    assert ansalapi.values["circuit_band_raw"] == "2"
    assert ansalapi.values["circuit_band_percent"] == 2


@pytest.mark.unit
def test_gsm_stage_is_extracted_from_the_free_text_remarks_column() -> None:
    assert _extract_gsm_stage("-") is None
    assert _extract_gsm_stage("GSM STAGE - 0") == "0"
    assert _extract_gsm_stage("GSM STAGE - I") == "I"
    assert _extract_gsm_stage("GSM STAGE - II") == "II"


@pytest.mark.unit
def test_real_gsm_stage_rows_are_dated_and_keyed_correctly() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(SEC_LIST_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("sec_list.csv"), target)
    by_symbol = {row.values["symbol"]: row for row in rows}
    assert by_symbol["ANSALAPI"].values["gsm_stage"] == "I"
    assert by_symbol["CBAZAAR"].values["gsm_stage"] == "II"
    assert by_symbol["360ONE"].values["gsm_stage"] is None
    assert by_symbol["ANSALAPI"].natural_key == ("ANSALAPI", "BZ", "circuit_band_gsm")
    assert by_symbol["ANSALAPI"].effective_date == FIXTURE_DATE


@pytest.mark.unit
def test_circuit_band_gsm_rows_are_all_dated_from_the_requested_date() -> None:
    """There is no in-payload date, so every row is stamped with what was requested."""
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(SEC_LIST_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("sec_list.csv"), target)
    assert {row.effective_date for row in rows} == {FIXTURE_DATE}


@pytest.mark.adversarial
def test_circuit_band_gsm_cannot_detect_a_wrong_date_by_design() -> None:
    """Documents the known blind spot: unlike `fo_ban_list`, `sec_list.csv` carries no
    date at all, so a structurally-valid payload for the WRONG day passes silently."""
    adapter = CircuitBandSurveillanceAdapter()
    payload = _fixture("sec_list.csv")
    definitely_wrong_date = _target(SEC_LIST_URL, "1999-01-01")
    assert adapter.content_mismatch_reason(payload, definitely_wrong_date) is None


@pytest.mark.adversarial
def test_circuit_band_gsm_without_a_requested_date_refuses_to_guess() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(SEC_LIST_URL, "")
    with pytest.raises(IngestAdapterError, match="no in-payload date"):
        adapter.parse(_fixture("sec_list.csv"), target)


@pytest.mark.adversarial
def test_a_wrong_schema_csv_is_rejected_by_the_structural_check() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    reason = adapter.content_mismatch_reason(
        b"Symbol,Series\nFOO,EQ\n", _target(SEC_LIST_URL, FIXTURE_DATE.isoformat())
    )
    assert reason is not None
    assert "header" in reason


# ---------------------------------------------------------------------------- ASM


@pytest.mark.unit
def test_real_asm_payload_covers_both_terms_with_the_right_counts() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("report_asm.json"), target)
    long_term = [row for row in rows if row.values["asm_term"] == "asm_long_term"]
    short_term = [row for row in rows if row.values["asm_term"] == "asm_short_term"]
    assert len(long_term) == ASM_LONG_TERM_SYMBOLS
    assert len(short_term) == ASM_SHORT_TERM_ENTRIES
    assert len({row.values["symbol"] for row in short_term}) == ASM_SHORT_TERM_UNIQUE_SYMBOLS
    assert all(row.effective_date == FIXTURE_DATE for row in rows)
    assert all(row.values["asm_stage"].startswith("Stage") for row in rows)


@pytest.mark.adversarial
def test_the_real_duplicate_dci_row_is_preserved_not_dropped() -> None:
    """The live short-term list names `DCI` twice (casing-only company-name variant,
    same ISIN). Both must survive with distinct natural keys — see module docstring."""
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat())
    rows = adapter.parse(_fixture("report_asm.json"), target)
    dci_rows = [row for row in rows if row.values["symbol"] == "DCI"]
    assert len(dci_rows) == 2
    assert len({row.natural_key for row in dci_rows}) == 2
    assert dci_rows[0].natural_key == ("DCI", "asm_short_term", "INE0A1101019")
    assert dci_rows[1].natural_key == (
        "DCI",
        "asm_short_term",
        "INE0A1101019",
        "duplicate-occurrence-2",
    )


@pytest.mark.unit
def test_asm_content_check_accepts_its_own_real_payload() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat())
    assert adapter.content_mismatch_reason(_fixture("report_asm.json"), target) is None


@pytest.mark.adversarial
def test_asm_content_check_detects_a_wrong_date_via_the_self_declared_as_on_date() -> None:
    """Unlike the circuit-band half, `reportASM`'s `asmTime` makes this detectable —
    the same defence `fo_ban_list` has via its own trade-date header."""
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(REPORT_ASM_URL, "1999-01-01")
    reason = adapter.content_mismatch_reason(_fixture("report_asm.json"), target)
    assert reason is not None
    assert "2026-08-11" in reason


@pytest.mark.unit
def test_an_empty_but_well_formed_asm_report_is_valid_not_broken() -> None:
    """No security under ASM today is a legitimate (if fortunate) state — judged on
    structure, never on row count, the same principle `fo_ban_list` applies."""
    adapter = CircuitBandSurveillanceAdapter()
    payload = b'{"longterm": {"data": []}, "shortterm": {"data": []}}'
    rows = adapter.parse(payload, _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat()))
    assert rows == []


@pytest.mark.adversarial
@pytest.mark.parametrize("name,payload", MALFORMED_PAYLOADS)
def test_malformed_payloads_on_the_asm_url_also_raise(name: str, payload: bytes) -> None:
    """The shared conformance suite's malformed-payload test dispatches every payload
    through the CSV path (its targets all use a `test://` URL). This certifies the ASM
    JSON path independently against the same adversarial payload set."""
    adapter = CircuitBandSurveillanceAdapter()
    target = _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat())
    with pytest.raises(IngestAdapterError):
        adapter.parse(payload, target)


@pytest.mark.adversarial
def test_asm_payload_missing_a_section_raises() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    payload = b'{"longterm": {"data": []}}'  # no "shortterm" at all
    with pytest.raises(IngestAdapterError, match="shortterm"):
        adapter.parse(payload, _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat()))


@pytest.mark.adversarial
def test_asm_entry_missing_as_on_date_raises() -> None:
    adapter = CircuitBandSurveillanceAdapter()
    payload = (
        b'{"longterm": {"data": [{"symbol": "FOO", "isin": "INE000A01001"}]}, '
        b'"shortterm": {"data": []}}'
    )
    with pytest.raises(IngestAdapterError, match="asmTime"):
        adapter.parse(payload, _target(REPORT_ASM_URL, FIXTURE_DATE.isoformat()))
