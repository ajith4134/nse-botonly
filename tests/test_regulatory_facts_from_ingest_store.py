"""Tests for the regulatory-facts assembler (`L7.06`).

One distinction carries this whole module and every test here exists to protect it: **an unread
source is `None`, an absent symbol in a source that WAS read is `False`.** Collapsing them is how a
system trades a banned scrip confidently on the morning the ban file failed to download.

`docs/research/227` §4 tier 1 is the specification.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.ingest_source_adapter import IngestRow
from nse_algo_trader.sizing.regulatory_facts_from_ingest_store import (
    MWPL_BAN_THRESHOLD_FRACTION,
    assemble_regulatory_facts,
)

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 8, 13)


def _store(tmp_path: Path, rows: list[tuple[str, tuple[str, ...], dict[str, object]]]):
    """A store holding exactly the rows given, written through the store's OWN write path.

    Deliberately not raw SQL: writing through `record_fetch` + `ingest_rows` means the fixture
    exercises the same provenance and content-hash machinery a real adapter does, so a schema
    change breaks this loudly instead of leaving it testing a shape nothing produces.
    """
    store = BitemporalIngestStore(tmp_path / "ingest.sqlite3")
    observed_at = datetime(2026, 8, 13, 9, 0, tzinfo=IST)
    by_source: dict[str, list[IngestRow]] = {}
    for source, key, values in rows:
        by_source.setdefault(source, []).append(
            IngestRow(values=values, effective_date=SESSION, natural_key=key)
        )
    for source, source_rows in by_source.items():
        fetch_id = store.record_fetch(
            source_name=source,
            url=f"https://example.invalid/{source}",
            fetched_at=observed_at,
            fetch_status="ok",
            evidence="fixture",
            attempts=1,
        )
        store.ingest_rows(source, source_rows, observed_at=observed_at, fetch_id=fetch_id)
    return store


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_symbol_on_the_ban_list_is_reported_banned(tmp_path: Path) -> None:
    with _store(
        tmp_path, [("fo_ban_list", ("SAIL",), {"symbol": "SAIL", "serial_number": 4})]
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="SAIL", ingest_store=store, effective_date=SESSION
        )
    assert facts.is_fo_banned is True


@pytest.mark.unit
def test_mwpl_utilisation_is_open_interest_over_the_limit(tmp_path: Path) -> None:
    with _store(
        tmp_path,
        [
            (
                "mwpl_position_limits",
                ("ZYDUSLIFE",),
                {
                    "NSE Symbol": "ZYDUSLIFE",
                    "Future Equivalent Open Interest": "11652564.14496774",
                    "MWPL": "36152910",
                },
            )
        ],
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="ZYDUSLIFE", ingest_store=store, effective_date=SESSION
        )
    assert facts.mwpl_utilisation_fraction is not None
    assert Decimal("0.32") < facts.mwpl_utilisation_fraction < Decimal("0.33")
    assert facts.mwpl_breach_threshold_fraction == MWPL_BAN_THRESHOLD_FRACTION


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_an_unread_source_is_none_and_never_not_banned(tmp_path: Path) -> None:
    """The property the whole module exists for.

    A store with no ban rows for this date must NOT report "not banned" — it must report that
    nobody looked, so the gate shouts UNCHECKED rather than letting the order through.
    """
    with _store(tmp_path, []) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="SAIL", ingest_store=store, effective_date=SESSION
        )
    assert facts.is_fo_banned is None
    assert "fo_ban_list" in facts.unread_sources


@pytest.mark.adversarial
def test_a_source_that_was_read_reports_an_absent_symbol_as_not_banned(tmp_path: Path) -> None:
    """The other half. A ban list that exists and does not name this scrip is a real clearance."""
    with _store(
        tmp_path, [("fo_ban_list", ("SAIL",), {"symbol": "SAIL"})]
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="RELIANCE", ingest_store=store, effective_date=SESSION
        )
    assert facts.is_fo_banned is False
    assert "fo_ban_list" not in facts.unread_sources


@pytest.mark.adversarial
def test_a_cash_only_scrip_does_not_claim_an_unchecked_mwpl_wall(tmp_path: Path) -> None:
    """A scrip outside the F&O segment has no MWPL row, and that is ABSENCE, not a failure to look.

    `unread_sources` keys off the threshold — set whenever the source was read — rather than the
    utilisation, which is legitimately missing for every cash-only name.
    """
    with _store(
        tmp_path,
        [
            (
                "mwpl_position_limits",
                ("ZYDUSLIFE",),
                {"NSE Symbol": "ZYDUSLIFE", "Future Equivalent Open Interest": "1", "MWPL": "2"},
            )
        ],
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="SOMECASHONLY", ingest_store=store, effective_date=SESSION
        )
    assert facts.mwpl_utilisation_fraction is None
    assert "mwpl_position_limits" not in facts.unread_sources


@pytest.mark.adversarial
def test_circuit_prices_stay_absent_because_the_feed_does_not_carry_them(tmp_path: Path) -> None:
    """Stated rather than left to be discovered: the ASM/GSM feed has stages, not price bands."""
    with _store(
        tmp_path,
        [
            (
                "circuit_band_asm_gsm",
                ("XPROINDIA", "asm_short_term", "INE445C01015"),
                {"asm_stage": "Stage I", "asm_term": "asm_short_term"},
            )
        ],
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="XPROINDIA", ingest_store=store, effective_date=SESSION
        )
    assert facts.surveillance_stage == "Stage I"
    assert facts.upper_circuit_price_rupees is None
    assert facts.lower_circuit_price_rupees is None
    assert "circuit_bands" in facts.unread_sources


@pytest.mark.adversarial
def test_a_malformed_mwpl_figure_is_absent_and_never_zero(tmp_path: Path) -> None:
    """Zero utilisation reads as "nowhere near the ban threshold", which is the opposite of
    unknown."""
    with _store(
        tmp_path,
        [
            (
                "mwpl_position_limits",
                ("BADROW",),
                {"NSE Symbol": "BADROW", "Future Equivalent Open Interest": "n/a", "MWPL": "0"},
            )
        ],
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="BADROW", ingest_store=store, effective_date=SESSION
        )
    assert facts.mwpl_utilisation_fraction is None


@pytest.mark.adversarial
def test_the_symbol_match_is_case_and_whitespace_insensitive(tmp_path: Path) -> None:
    with _store(
        tmp_path, [("fo_ban_list", ("SAIL",), {"symbol": " sail "})]
    ) as store:
        facts = assemble_regulatory_facts(
            trading_symbol="  Sail ", ingest_store=store, effective_date=SESSION
        )
    assert facts.is_fo_banned is True


@pytest.mark.real_data
def test_the_real_ban_list_names_real_symbols_today() -> None:
    """`R.05` — against the operator's own ingest store, not a fixture.

    Eight symbols are on the ban list for 2026-08-13. If this ever fails because the file moved on,
    that is the test doing its job: it asserts the SHAPE of a real answer, not a frozen roster.
    """
    real = Path("~/.nse_algo_trader/nse_ingest.sqlite3").expanduser()
    if not real.exists():  # pragma: no cover — the operator's box always has it
        pytest.skip("the real ingest store is not present on this machine")
    with BitemporalIngestStore(real) as store:
        banned = assemble_regulatory_facts(
            trading_symbol="SAIL", ingest_store=store, effective_date=date(2026, 8, 13)
        )
        ancient = assemble_regulatory_facts(
            trading_symbol="RELIANCE", ingest_store=store, effective_date=date(2020, 1, 1)
        )
    assert banned.is_fo_banned is True
    # A date the ingest never covered must report UNREAD, never "clear".
    assert ancient.is_fo_banned is None
    assert "fo_ban_list" in ancient.unread_sources
