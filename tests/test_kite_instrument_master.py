"""Tests for the Kite instrument master.

The fixture is a **trimmed real dump** (94 rows drawn from the live 113,955-row
file) — R.05 prefers real samples, and the shapes that break parsers are the real
ones: blank expiries on cash rows, `0` strikes, `0` tick sizes on indices, and the
measured `BSE:INFRA` key collision.

**Rewritten 2026-08-10 after adversarial review found that 10 of 20 mutants
survived this suite** and that three tests were tautologies. Each test that exists
to kill a specific mutant says which one.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.kite_instrument_master import (
    INSTRUMENT_DUMP_URL,
    InstrumentDumpFetchError,
    InstrumentDumpValidationError,
    InstrumentMasterError,
    InstrumentMasterStore,
    InstrumentRecord,
    fetch_instrument_dump,
    parse_instrument_dump,
)

REAL_SAMPLE = Path(__file__).parent / "fixtures_instrument_dump" / "real_instrument_sample.csv"
# The full live dump, cached outside the repo (9.3 MB). The real-data tests skip
# cleanly without it rather than pretending to pass.
FULL_REAL_DUMP = Path(
    "/tmp/claude-1000/-home-opc/04751fbf-7696-4bb8-bc1d-292b7edd410f/scratchpad/inst.csv"
)

FIXTURE_ROW_COUNT = 94
EXPECTED_INFRA_ROWS = 2
FULL_DUMP_ROW_COUNT = 113_955
FULL_DUMP_NFO_UNDERLYINGS = 213
SAFETY_MULTIPLE = Decimal(2)
MINIMUM_TOLERANCE = Decimal("0.05")
INDEX_OPTION_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})


@pytest.fixture
def real_dump_text() -> str:
    return REAL_SAMPLE.read_text(encoding="utf-8")


@pytest.fixture
def real_records(real_dump_text: str) -> list[InstrumentRecord]:
    return parse_instrument_dump(real_dump_text)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentMasterStore]:
    with InstrumentMasterStore(tmp_path / "master.sqlite3") as opened:
        yield opened


def _one_row_with(dump_text: str, **overrides: str) -> str:
    """Rebuild a one-row dump with the named fields overridden."""
    lines = dump_text.splitlines()
    header = lines[0].split(",")
    values = next(row for row in lines[1:] if '"' not in row).split(",")
    for column, value in overrides.items():
        values[header.index(column)] = value
    return "\n".join([lines[0], ",".join(values)])


def _reused_token(inheritor: InstrumentRecord, retired: InstrumentRecord) -> InstrumentRecord:
    """The inheritor wearing the retired contract's token, with different terms."""
    return replace(
        inheritor,
        instrument_token=retired.instrument_token,
        expiry=date(2031, 12, 25),
        strike=Decimal("99999"),
    )


# --------------------------------------------------------------------------- parsing


@pytest.mark.unit
def test_every_real_row_parses(real_records: list[InstrumentRecord]) -> None:
    assert len(real_records) == FIXTURE_ROW_COUNT


@pytest.mark.unit
def test_every_price_field_is_decimal_never_float(
    real_records: list[InstrumentRecord],
) -> None:
    """Kills the `last_price` float mutant.

    The original checked `strike` and `tick_size` but not `last_price` — the one
    field actually named "price" — so parsing it as a float survived (defect 20).
    """
    for record in real_records:
        assert isinstance(record.last_price, Decimal)
        assert isinstance(record.strike, Decimal)
        assert isinstance(record.tick_size, Decimal)


@pytest.mark.unit
def test_identity_fields_are_stripped(real_dump_text: str) -> None:
    """Kills the `.strip()`-removed mutant."""
    record = parse_instrument_dump(
        _one_row_with(real_dump_text, tradingsymbol="  PADDED  ", exchange=" NSE ")
    )[0]
    assert record.tradingsymbol == "PADDED"
    assert record.exchange == "NSE"


@pytest.mark.unit
def test_derivative_rows_carry_an_expiry_and_cash_rows_do_not(
    real_records: list[InstrumentRecord],
) -> None:
    options = [r for r in real_records if r.instrument_type in ("CE", "PE")]
    cash = [r for r in real_records if r.instrument_type == "EQ"]
    assert options and all(isinstance(r.expiry, date) for r in options)
    assert cash and all(r.expiry is None for r in cash)


@pytest.mark.unit
def test_all_five_index_option_underlyings_are_present(
    real_records: list[InstrumentRecord],
) -> None:
    underlyings = {r.name for r in real_records if r.instrument_type in ("CE", "PE")}
    assert underlyings >= INDEX_OPTION_UNDERLYINGS


# --------------------------------------------------------------------------- the key


@pytest.mark.unit
def test_the_measured_key_collision_survives_as_two_rows(
    real_records: list[InstrumentRecord],
) -> None:
    """A.34 — the finding that corrected the plan."""
    infra = [r for r in real_records if r.exchange == "BSE" and r.tradingsymbol == "INFRA"]
    assert len(infra) == EXPECTED_INFRA_ROWS
    assert {r.segment for r in infra} == {"BSE", "INDICES"}
    assert len({r.identity for r in infra}) == EXPECTED_INFRA_ROWS


@pytest.mark.unit
def test_the_narrower_key_would_have_lost_a_row(
    real_records: list[InstrumentRecord],
) -> None:
    """Kills the "key on (exchange, tradingsymbol)" mutant.

    The original suite could not tell the two keys apart (defect 18); this asserts
    the *difference* between them rather than the current key's behaviour.
    """
    narrow = {(r.exchange, r.tradingsymbol) for r in real_records}
    assert len(narrow) < len(real_records)
    assert len({r.identity for r in real_records}) == len(real_records)


# --------------------------------------------------------------------------- validation


@pytest.mark.adversarial
def test_a_genuinely_column_dropped_dump_is_rejected(real_dump_text: str) -> None:
    """Kills the "required-column check deleted" mutant.

    The original removed the header name only, leaving 11 names against 12 fields,
    so the *malformed-row* check fired instead and deleting the column check still
    passed (defect 19). This drops the column from the header and every row.
    """
    lines = [row for row in real_dump_text.splitlines() if '"' not in row]
    index = lines[0].split(",").index("segment")
    rebuilt = [
        ",".join(value for position, value in enumerate(line.split(",")) if position != index)
        for line in lines
    ]
    with pytest.raises(InstrumentDumpValidationError, match="missing required column"):
        parse_instrument_dump("\n".join(rebuilt))


@pytest.mark.adversarial
def test_a_row_with_surplus_columns_is_rejected(real_dump_text: str) -> None:
    """Extra fields were silently discarded under DictReader's restkey (defect 8)."""
    lines = real_dump_text.splitlines()
    line = next(row for row in lines[1:] if '"' not in row)
    with pytest.raises(InstrumentDumpValidationError, match="more fields"):
        parse_instrument_dump("\n".join([lines[0], line + ",EXTRA_JUNK"]))


@pytest.mark.adversarial
@pytest.mark.parametrize("poison", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_prices_are_rejected(real_dump_text: str, poison: str) -> None:
    """`Decimal("NaN")` does not raise InvalidOperation, so it sailed through (defect 7)."""
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump(_one_row_with(real_dump_text, strike=poison))


@pytest.mark.adversarial
@pytest.mark.parametrize("column", ["last_price", "strike", "tick_size", "lot_size"])
def test_a_blank_numeric_is_rejected_not_coerced_to_zero(real_dump_text: str, column: str) -> None:
    """Kills the "blank becomes Decimal(1)" mutant, and defect 9.

    A blank lot size silently becoming 0 is an order-sizing hazard, and a blank
    tick size becoming 0 is indistinguishable from an index's legitimate zero.
    """
    with pytest.raises(InstrumentDumpValidationError, match=r"blank|ASCII digits"):
        parse_instrument_dump(_one_row_with(real_dump_text, **{column: ""}))


@pytest.mark.adversarial
@pytest.mark.parametrize("sneaky", ["+4242", "4_242", "١٢٣", "42.0", "4 242"])
def test_integers_python_would_silently_accept_are_rejected(
    real_dump_text: str, sneaky: str
) -> None:
    """`int("١٢٣") == 123`. Unicode digits do not belong in an exchange feed."""
    with pytest.raises(InstrumentDumpValidationError, match="ASCII digits"):
        parse_instrument_dump(_one_row_with(real_dump_text, lot_size=sneaky))


@pytest.mark.adversarial
def test_an_empty_identity_field_is_rejected(real_dump_text: str) -> None:
    """('', '', '') would collapse unrelated instruments together (defect 15)."""
    with pytest.raises(InstrumentDumpValidationError, match="cannot be blank"):
        parse_instrument_dump(_one_row_with(real_dump_text, tradingsymbol=""))


@pytest.mark.adversarial
def test_a_dump_reusing_a_token_within_one_file_is_rejected(real_dump_text: str) -> None:
    """Token uniqueness was measured once and never asserted at runtime (defect 11)."""
    lines = real_dump_text.splitlines()
    clean = [row for row in lines[1:] if '"' not in row][:2]
    second = clean[1].split(",")
    second[0] = clean[0].split(",")[0]
    with pytest.raises(InstrumentDumpValidationError, match="reuses instrument_token"):
        parse_instrument_dump("\n".join([lines[0], clean[0], ",".join(second)]))


@pytest.mark.adversarial
def test_an_empty_dump_is_rejected() -> None:
    """Kills the "if not records: return []" mutant."""
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump("")


@pytest.mark.adversarial
def test_a_header_only_dump_is_rejected(real_dump_text: str) -> None:
    with pytest.raises(InstrumentDumpValidationError):
        parse_instrument_dump(real_dump_text.splitlines()[0])


# --------------------------------------------------------------------------- fetch


@pytest.mark.unit
def test_the_fetch_url_carries_no_credentials() -> None:
    assert INSTRUMENT_DUMP_URL.startswith("https://")
    assert "api_key" not in INSTRUMENT_DUMP_URL


@pytest.mark.adversarial
def test_a_failing_fetch_raises_rather_than_returning_a_partial_body() -> None:
    """The truncation guard catches partial downloads; fetch must not create them."""
    with pytest.raises(InstrumentDumpFetchError):
        fetch_instrument_dump(url="https://127.0.0.1:9/instruments", attempts=1)


# --------------------------------------------------------------------------- store


@pytest.mark.unit
def test_the_store_round_trips_every_record(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 10))) == len(real_records)


@pytest.mark.unit
def test_the_store_is_queryable_as_of_a_date(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    store.ingest(real_records, ingested_on=date(2026, 8, 9))
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 9))) == len(real_records)
    assert len(store.instruments_as_of(date(2026, 8, 11))) == len(real_records)
    assert store.instruments_as_of(date(2026, 8, 1)) == []


@pytest.mark.unit
def test_reingesting_the_same_day_replaces_rather_than_appends(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Kills the "DELETE removed, INSERT OR REPLACE used" mutant.

    The original re-ingested an identical list, so replacement and upsert looked
    the same. This re-ingests a *smaller* list for the same date, which an upsert
    would leave the dropped rows behind for.
    """
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    subset = real_records[:90]
    store.ingest(subset, ingested_on=date(2026, 8, 10))
    stored = store.instruments_as_of(date(2026, 8, 10))
    assert len(stored) == len(subset)
    assert {r.identity for r in stored} == {r.identity for r in subset}


@pytest.mark.unit
def test_the_tradeable_universe_can_be_read_per_exchange(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    nfo = store.instruments_as_of(date(2026, 8, 10), exchange="NFO")
    assert nfo and all(r.exchange == "NFO" for r in nfo)


# --------------------------------------------------------------------------- guards


@pytest.mark.adversarial
def test_a_same_day_reingest_cannot_destroy_the_universe(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 2 — the most destructive path in the module.

    The guard compared against a strictly-earlier date, absent on a first ingest,
    so it returned early and the DELETE wiped the day. Reproduced at the time:
    94 rows became 1, with no error raised.
    """
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    with pytest.raises(InstrumentDumpValidationError):
        store.ingest(real_records[:1], ingested_on=date(2026, 8, 10))
    assert len(store.instruments_as_of(date(2026, 8, 10))) == len(real_records)


@pytest.mark.adversarial
def test_a_backfill_is_guarded_too(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 3 — same root cause, opposite direction in time."""
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    with pytest.raises(InstrumentDumpValidationError):
        store.ingest(real_records[:2], ingested_on=date(2026, 8, 9))


@pytest.mark.adversarial
def test_losing_an_entire_exchange_is_refused(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 4 — required by the spec, absent from the code.

    Dropping one whole exchange can sit inside the size tolerance, after which
    every downstream scan reads "this exchange has no instruments" as normal.
    """
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    without_nse = [r for r in real_records if r.exchange != "NSE"]
    with pytest.raises(InstrumentDumpValidationError, match="lost entire exchange"):
        store.ingest(without_nse, ingested_on=date(2026, 8, 11))


@pytest.mark.unit
def test_the_shrinkage_tolerance_is_derived_from_expiry_cohorts(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 5 — the threshold was a magic 0.30 dressed as derived.

    Asserts the derivation itself, so changing the safety multiple changes a
    computed value rather than a hand-picked one.
    """
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    dated = [r for r in real_records if r.expiry is not None]
    largest_cohort = max(
        sum(1 for r in dated if r.expiry == expiry) for expiry in {r.expiry for r in dated}
    )
    expected = (Decimal(largest_cohort) * SAFETY_MULTIPLE) / Decimal(len(real_records))
    assert store.derive_shrinkage_tolerance("2026-08-10") == max(expected, MINIMUM_TOLERANCE)


@pytest.mark.adversarial
def test_the_shrinkage_boundary_refuses_at_exactly_the_tolerance(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Kills the `>` vs `>=` mutant, which survived the original suite (defect 6).

    The boundary can only be pinned by a drop that lands EXACTLY on the tolerance,
    where the two operators disagree. Since tolerance = (cohort / total) x 2,
    dropping exactly ``2 x cohort`` rows makes shrinkage equal it precisely — a
    ceil-based approximation lands just above and both operators then agree.
    """
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    dated = [r for r in real_records if r.expiry is not None]
    largest_cohort = max(
        sum(1 for r in dated if r.expiry == expiry) for expiry in {r.expiry for r in dated}
    )
    must_drop = 2 * largest_cohort
    keep_count = len(real_records) - must_drop

    # Keep one record per exchange first, so the exchange-vanished guard cannot
    # fire and mask the boundary this test exists to pin.
    one_per_exchange = list({r.exchange: r for r in real_records}.values())
    remainder = [r for r in real_records if r not in one_per_exchange]
    survivors = [*one_per_exchange, *remainder[: keep_count - len(one_per_exchange)]]
    assert len(survivors) == keep_count
    shrinkage = Decimal(must_drop) / Decimal(len(real_records))
    assert shrinkage == store.derive_shrinkage_tolerance("2026-08-10"), "must sit ON the boundary"

    with pytest.raises(InstrumentDumpValidationError, match="truncated"):
        store.ingest(survivors, ingested_on=date(2026, 8, 11))


@pytest.mark.unit
def test_a_dump_within_tolerance_is_accepted(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """The other half of the boundary: a legitimate expiry must not be refused."""
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    survivors = real_records[:-1]
    store.ingest(survivors, ingested_on=date(2026, 8, 11))
    assert len(store.instruments_as_of(date(2026, 8, 11))) == len(survivors)


# --------------------------------------------------------------------------- reassignment


@pytest.mark.adversarial
def test_a_token_reassignment_is_detected_across_a_gap(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 1 — the failure that made the guard useless.

    Kite DROPS an expired contract and reuses its token later, so at the moment of
    reuse the token is absent from the previous day. The original consulted only
    the previous ingest and detected nothing. This reproduces the real pattern:
    present on day 1, gone on day 2, token reused on day 3.
    """
    retired, inheritor = real_records[0], real_records[1]
    store.ingest(real_records, ingested_on=date(2026, 8, 10))

    without_retired = [r for r in real_records if r.identity != retired.identity]
    store.ingest(without_retired, ingested_on=date(2026, 8, 11))

    reused = _reused_token(inheritor, retired)
    day_three = [reused, *[r for r in without_retired if r.identity != inheritor.identity]]
    reassignments = store.ingest(day_three, ingested_on=date(2026, 8, 12))

    assert len(reassignments) == 1
    assert reassignments[0].instrument_token == retired.instrument_token
    assert reassignments[0].previous_identity == retired.identity
    assert reassignments[0].previous_seen_on == date(2026, 8, 10)


@pytest.mark.adversarial
def test_a_reassignment_is_actually_persisted(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Kills the "reassignment INSERT removed" mutant.

    The original asserted only on the returned list and never queried the table,
    so deleting the INSERT entirely still passed (defect 23).
    """
    retired, inheritor = real_records[0], real_records[1]
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    reused = _reused_token(inheritor, retired)
    day_two = [
        reused,
        *[r for r in real_records if r.identity not in (inheritor.identity, retired.identity)],
    ]
    store.ingest(day_two, ingested_on=date(2026, 8, 11))

    persisted = store.recorded_reassignments(date(2026, 8, 11))
    assert len(persisted) == 1
    assert persisted[0].instrument_token == retired.instrument_token
    assert persisted[0].previous_identity == retired.identity


@pytest.mark.adversarial
def test_a_symbol_rename_is_not_reported_as_token_reuse(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 13 — corporate-action renames are routine on NSE.

    Flagging them as token reuse makes the log untrustworthy for the one thing it
    exists to detect. Same token and same contract terms with a new name is a
    rename, not reuse.
    """
    original = real_records[0]
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    renamed = replace(original, tradingsymbol=original.tradingsymbol + "RE")
    assert store.ingest([renamed, *real_records[1:]], ingested_on=date(2026, 8, 11)) == []


@pytest.mark.adversarial
def test_stale_reassignments_are_cleared_on_a_corrected_reingest(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 12 — idempotency covered one of the two tables."""
    retired, inheritor = real_records[0], real_records[1]
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    reused = _reused_token(inheritor, retired)
    bad_day = [
        reused,
        *[r for r in real_records if r.identity not in (inheritor.identity, retired.identity)],
    ]
    store.ingest(bad_day, ingested_on=date(2026, 8, 11))
    assert store.recorded_reassignments(date(2026, 8, 11))

    store.ingest(real_records, ingested_on=date(2026, 8, 11))
    assert store.recorded_reassignments(date(2026, 8, 11)) == []


# --------------------------------------------------------------------------- robustness


@pytest.mark.adversarial
def test_a_store_failure_raises_an_instrument_master_error(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 10 — a raw sqlite3.IntegrityError escaped the documented contract."""
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    with pytest.raises(InstrumentMasterError):
        store.ingest([*real_records, real_records[0]], ingested_on=date(2026, 8, 11))


@pytest.mark.unit
def test_the_store_is_readable_from_another_thread(
    store: InstrumentMasterStore, real_records: list[InstrumentRecord]
) -> None:
    """Defect 14 — the default connection raised ProgrammingError off-thread,
    which would block the dashboard reading while an ingest writes."""
    store.ingest(real_records, ingested_on=date(2026, 8, 10))
    seen: list[int] = []

    def read() -> None:
        seen.append(len(store.instruments_as_of(date(2026, 8, 10))))

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    assert seen == [len(real_records)]


@pytest.mark.unit
def test_the_store_uses_write_ahead_logging(store: InstrumentMasterStore) -> None:
    """WAL lets a reader proceed during a write instead of hitting a locked database."""
    with sqlite3.connect(store.database_path) as probe:
        mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


@pytest.mark.unit
def test_the_store_closes_cleanly(tmp_path: Path) -> None:
    """The original leaked its connection for the object's lifetime."""
    opened = InstrumentMasterStore(tmp_path / "closeable.sqlite3")
    opened.close()
    with pytest.raises(sqlite3.ProgrammingError):
        opened.instruments_as_of(date(2026, 8, 10))


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
@pytest.mark.skipif(not FULL_REAL_DUMP.exists(), reason="the full live dump is not cached")
def test_the_full_real_dump_ingests_end_to_end(tmp_path: Path) -> None:
    """Spec acceptance criteria 1 and 6, previously asserted in prose only (defect 17)."""
    records = parse_instrument_dump(FULL_REAL_DUMP.read_text(encoding="utf-8"))
    assert len(records) == FULL_DUMP_ROW_COUNT

    with InstrumentMasterStore(tmp_path / "full.sqlite3") as full_store:
        full_store.ingest(records, ingested_on=date(2026, 8, 10))
        stored = full_store.instruments_as_of(date(2026, 8, 10))

    assert len(stored) == FULL_DUMP_ROW_COUNT
    nfo = [r for r in stored if r.exchange == "NFO"]
    assert len({r.name for r in nfo}) == FULL_DUMP_NFO_UNDERLYINGS
    assert {r.name for r in nfo} >= INDEX_OPTION_UNDERLYINGS
    infra = [r for r in stored if r.exchange == "BSE" and r.tradingsymbol == "INFRA"]
    assert len(infra) == EXPECTED_INFRA_ROWS
