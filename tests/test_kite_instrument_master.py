"""Tests for the Kite instrument master — written before the implementation (R.23 step 3).

The fixture is a **trimmed real dump** (94 rows drawn from the live 113,955-row
file), not synthetic data — R.05 prefers real samples, and the shapes that break
parsers are the real ones: blank expiries on cash rows, `0` strikes, `0` tick
sizes on indices, and the measured `BSE:INFRA` key collision.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.kite_instrument_master import (
    INSTRUMENT_DUMP_URL,
    InstrumentDumpValidationError,
    InstrumentMasterStore,
    InstrumentRecord,
    parse_instrument_dump,
)

REAL_SAMPLE = Path(__file__).parent / "fixtures_instrument_dump" / "real_instrument_sample.csv"

# The fixture is a fixed, checked-in slice of the real dump; its size is a fact
# about the file, not a tuning parameter.
FIXTURE_ROW_COUNT = 94
EXPECTED_INFRA_ROWS = 2
PARTIAL_INGEST_ROW_COUNT = 40


@pytest.fixture
def real_dump_text() -> str:
    return REAL_SAMPLE.read_text(encoding="utf-8")


@pytest.fixture
def real_records(real_dump_text: str) -> list[InstrumentRecord]:
    return parse_instrument_dump(real_dump_text)


# --------------------------------------------------------------------------- parsing


@pytest.mark.unit
def test_the_dump_endpoint_needs_no_authentication() -> None:
    """Measured 2026-08-10: the raw endpoint returns 200 unauthenticated.

    This is why the master is decoupled from the daily TOTP login — the universe
    stays available even when the token has lapsed (L3.28, L13.10).
    """
    assert INSTRUMENT_DUMP_URL == "https://api.kite.trade/instruments"
    assert "api_key" not in INSTRUMENT_DUMP_URL


@pytest.mark.unit
def test_every_real_row_parses(real_records: list[InstrumentRecord]) -> None:
    assert len(real_records) == FIXTURE_ROW_COUNT


@pytest.mark.unit
def test_prices_are_decimal_never_float(real_records: list[InstrumentRecord]) -> None:
    """Strike and tick size are money; binary floats do not represent them exactly."""
    for record in real_records:
        assert isinstance(record.strike, Decimal)
        assert isinstance(record.tick_size, Decimal)


@pytest.mark.unit
def test_derivative_rows_carry_an_expiry_and_cash_rows_do_not(
    real_records: list[InstrumentRecord],
) -> None:
    """Blank expiry on cash rows is the shape that breaks naive date parsing."""
    options = [r for r in real_records if r.instrument_type in ("CE", "PE")]
    cash = [r for r in real_records if r.instrument_type == "EQ"]
    assert options and all(isinstance(r.expiry, date) for r in options)
    assert cash and all(r.expiry is None for r in cash)


@pytest.mark.unit
def test_all_five_index_option_underlyings_are_present(
    real_records: list[InstrumentRecord],
) -> None:
    underlyings = {r.name for r in real_records if r.instrument_type in ("CE", "PE")}
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"} <= underlyings


# --------------------------------------------------------------------------- the key


@pytest.mark.unit
def test_the_measured_key_collision_survives_as_two_rows(
    real_records: list[InstrumentRecord],
) -> None:
    """A.34 — the finding that corrected the plan.

    `BSE:INFRA` is both a Mirae ETF (segment BSE) and a BSE index (segment
    INDICES). Keying on (exchange, tradingsymbol) would silently drop one of them
    on every ingest, with no error.
    """
    infra = [r for r in real_records if r.exchange == "BSE" and r.tradingsymbol == "INFRA"]
    assert len(infra) == EXPECTED_INFRA_ROWS
    assert {r.segment for r in infra} == {"BSE", "INDICES"}
    assert len({r.identity for r in infra}) == EXPECTED_INFRA_ROWS, (
        'the identity key must separate them'
    )


@pytest.mark.unit
def test_identity_is_exchange_segment_tradingsymbol(
    real_records: list[InstrumentRecord],
) -> None:
    record = real_records[0]
    assert record.identity == (record.exchange, record.segment, record.tradingsymbol)


@pytest.mark.unit
def test_identity_is_unique_across_the_real_sample(
    real_records: list[InstrumentRecord],
) -> None:
    assert len({r.identity for r in real_records}) == len(real_records)


@pytest.mark.unit
def test_instrument_token_is_stored_but_is_not_the_identity(
    real_records: list[InstrumentRecord],
) -> None:
    """Kite reuses the token after expiry; it is a transient handle, not a name."""
    record = real_records[0]
    assert record.instrument_token > 0
    assert str(record.instrument_token) not in record.identity
    assert record.identity == (record.exchange, record.segment, record.tradingsymbol)


# --------------------------------------------------------------------------- validation


@pytest.mark.adversarial
def test_a_dump_missing_a_required_column_is_rejected(real_dump_text: str) -> None:
    lines = real_dump_text.splitlines()
    lines[0] = lines[0].replace("segment,", "")
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump("\n".join(lines))


@pytest.mark.adversarial
def test_an_empty_dump_is_rejected() -> None:
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump("")


@pytest.mark.adversarial
def test_a_header_only_dump_is_rejected(real_dump_text: str) -> None:
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump(real_dump_text.splitlines()[0])


@pytest.mark.adversarial
def test_an_unparseable_row_is_rejected_not_skipped(real_dump_text: str) -> None:
    """Silently skipping a bad row loses an instrument without saying so."""
    lines = real_dump_text.splitlines()
    lines[1] = lines[1].replace(",EQ,", ",EQ,").replace('"', "")
    broken = [*lines[:1], "not,a,valid,row", *lines[1:]]
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump("\n".join(broken))


# --------------------------------------------------------------------------- store


@pytest.mark.unit
def test_the_store_round_trips_every_record(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 10))) == len(real_records)


@pytest.mark.unit
def test_the_store_is_queryable_as_of_a_date(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    """Point-in-time is what L0.05 and L0.06 build on."""
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    store.ingest(real_records[:PARTIAL_INGEST_ROW_COUNT], ingested_on=date(2026, 8, 9))
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 9))) == PARTIAL_INGEST_ROW_COUNT
    assert len(store.instruments_as_of(date(2026, 8, 10))) == len(real_records)


@pytest.mark.unit
def test_reingesting_the_same_day_is_idempotent(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    """A retried fetch must not double the universe."""
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    for _ in range(3):
        store.ingest(real_records, ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 10))) == len(real_records)


@pytest.mark.adversarial
def test_a_token_reassignment_is_detected_and_recorded(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    """L0.02 — the guard. Kite reuses tokens once a contract expires.

    Silently accepting the reassignment would merge an expired contract's history
    into a new instrument under one identity.
    """
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    store.ingest(real_records, ingested_on=date(2026, 8, 10))

    # A realistic next-day dump: the full universe, with ONE contract's token now
    # pointing at a different instrument. Ingesting a single record instead would
    # (correctly) trip the truncation guard — a real ingest is always the whole file.
    original, other = real_records[0], real_records[1]
    next_day = [replace(other, instrument_token=original.instrument_token), *real_records[2:]]
    reassignments = store.ingest(next_day, ingested_on=date(2026, 8, 11))

    assert len(reassignments) == 1
    assert reassignments[0].instrument_token == original.instrument_token
    assert reassignments[0].previous_identity == original.identity
    assert reassignments[0].current_identity == other.identity


@pytest.mark.adversarial
def test_a_shrunken_dump_is_refused(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    """A truncated dump that parses is more dangerous than one that fails.

    Accepting it would silently delete most of the tradeable universe.
    """
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    with pytest.raises(InstrumentDumpValidationError):
        store.ingest(real_records[:5], ingested_on=date(2026, 8, 11))


@pytest.mark.unit
def test_the_tradeable_universe_can_be_read_per_exchange(
    tmp_path: Path, real_records: list[InstrumentRecord]
) -> None:
    """The six holons ask the master rather than hand-rolling contract lists."""
    store = InstrumentMasterStore(tmp_path / "master.sqlite3")
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    nfo = store.instruments_as_of(date(2026, 8, 10), exchange="NFO")
    assert nfo and all(r.exchange == "NFO" for r in nfo)
