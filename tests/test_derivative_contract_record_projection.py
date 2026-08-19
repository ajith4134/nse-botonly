"""Tests for the F&O contract record projection (`L0.23`), written BEFORE the engine.

The defect these exist to prevent is the one that opened this slice: `fo_bhavcopy_contracts`
stopped at 2026-08-03 while the ingest store held 2026-08-18, and nothing in the repository wrote
the table, so five of six segment bots assembled their universes from contracts that had already
expired. Staleness with no error is the failure mode; every test here is about the projection being
current, idempotent and honest about what it could not project.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.derivative_contract_record_projection import (
    F_AND_O_INGEST_SOURCE_NAME,
    DerivativeContractProjectionError,
    DerivativeContractRecordProjection,
    contract_record_from_observation,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import IngestRow

# ---------------------------------------------------------------------------------------------
# Payloads shaped exactly like the real ones, copied field-for-field from a live observation so a
# schema drift in NSE's own file shows up here rather than in a bot's universe.
# ---------------------------------------------------------------------------------------------

REAL_STOCK_FUTURE_PAYLOAD = {
    "BizDt": "2026-08-18",
    "ChngInOpnIntrst": "-41500",
    "ClsPric": "1169.70",
    "FinInstrmId": "58074",
    "FinInstrmNm": "360ONE26AUGFUT",
    "FinInstrmTp": "STF",
    "FininstrmActlXpryDt": "2026-08-25",
    "HghPric": "1185.30",
    "LwPric": "1155.00",
    "NewBrdLotQty": "500",
    "OpnIntrst": "3966000",
    "OpnPric": "1176.40",
    "OptnTp": "",
    "SttlmPric": "1169.70",
    "StrkPric": "",
    "TckrSymb": "360ONE",
    "TradDt": "2026-08-18",
    "TtlNbOfTxsExctd": "996",
    "TtlTradgVol": "1174",
    "TtlTrfVal": "686918100.00",
    "UndrlygPric": "1163.60",
    "XpryDt": "2026-08-25",
}

REAL_INDEX_OPTION_PAYLOAD = {
    "BizDt": "2026-08-18",
    "ChngInOpnIntrst": "18600",
    "ClsPric": "1.65",
    "FinInstrmId": "67233",
    "FinInstrmNm": "NIFTY26AUG24800CE",
    "FinInstrmTp": "IDO",
    "FininstrmActlXpryDt": "2026-08-27",
    "HghPric": "1.80",
    "LwPric": "1.50",
    "NewBrdLotQty": "75",
    "OpnIntrst": "93000",
    "OpnPric": "1.50",
    "OptnTp": "CE",
    "SttlmPric": "1.65",
    "StrkPric": "24800.00",
    "TckrSymb": "NIFTY",
    "TradDt": "2026-08-18",
    "TtlNbOfTxsExctd": "7",
    "TtlTradgVol": "7",
    "TtlTrfVal": "866.25",
    "UndrlygPric": "24773.15",
    "XpryDt": "2026-08-27",
}

OBSERVED_AT = datetime(2026, 8, 19, 3, 43, 24, tzinfo=UTC)


def _observation(payload: dict[str, str], *, observed_at: datetime = OBSERVED_AT):
    """Wrap a payload the way `BitemporalIngestStore.rows_for` hands it back."""
    from nse_algo_trader.nse_ingest.bitemporal_ingest_store import StoredObservation

    return StoredObservation(
        source_name=F_AND_O_INGEST_SOURCE_NAME,
        natural_key=(
            payload["TckrSymb"],
            payload["StrkPric"],
            payload["FinInstrmTp"],
            payload["XpryDt"],
            payload["OptnTp"],
            "",
        ),
        effective_date=date.fromisoformat(payload["TradDt"]),
        observed_at=observed_at,
        content_hash="deadbeef",
        values=payload,
        fetch_id=1,
    )


def _ingest_row(payload: dict[str, str]) -> IngestRow:
    return IngestRow(
        values=payload,
        effective_date=date.fromisoformat(payload["TradDt"]),
        natural_key=(
            payload["TckrSymb"],
            payload["StrkPric"],
            payload["FinInstrmTp"],
            payload["XpryDt"],
            payload["OptnTp"],
        ),
    )


@pytest.fixture
def ingest_database(tmp_path: Path) -> Path:
    path = tmp_path / "nse_ingest.sqlite3"
    with BitemporalIngestStore(path) as store:
        fetch_id = store.record_fetch(
            source_name=F_AND_O_INGEST_SOURCE_NAME,
            url="https://example.invalid/fo.zip",
            fetched_at=OBSERVED_AT,
            fetch_status="ok",
            evidence="test fixture",
            attempts=1,
            http_status=200,
        )
        store.ingest_rows(
            F_AND_O_INGEST_SOURCE_NAME,
            [_ingest_row(REAL_STOCK_FUTURE_PAYLOAD), _ingest_row(REAL_INDEX_OPTION_PAYLOAD)],
            observed_at=OBSERVED_AT,
            fetch_id=fetch_id,
        )
    return path


@pytest.fixture
def market_database(tmp_path: Path) -> Path:
    """A market database carrying the LEGACY table, so the migration path is what is tested."""
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE fo_bhavcopy_contracts (
            trade_date TEXT NOT NULL, contract_type TEXT NOT NULL,
            nse_instrument_id INTEGER NOT NULL, underlying_symbol TEXT NOT NULL,
            expiry_date TEXT NOT NULL, strike_price REAL, option_right_code TEXT,
            open_price REAL NOT NULL, high_price REAL NOT NULL,
            low_price REAL NOT NULL, close_price REAL NOT NULL,
            settlement_price REAL NOT NULL, underlying_price REAL NOT NULL,
            open_interest INTEGER NOT NULL, change_in_open_interest INTEGER NOT NULL,
            total_traded_volume INTEGER NOT NULL,
            PRIMARY KEY (trade_date, nse_instrument_id))
        """
    )
    connection.execute(
        """
        INSERT INTO fo_bhavcopy_contracts VALUES
        ('2026-07-22','STO',67233,'ABCAPITAL','2026-08-25',350.0,'PE',
         1.5,1.8,1.5,1.65,1.65,400.6,93000,18600,7)
        """
    )
    connection.commit()
    connection.close()
    return path


def _projection(market_database: Path, ingest_database: Path) -> DerivativeContractRecordProjection:
    return DerivativeContractRecordProjection(
        market_database=market_database, ingest_database=ingest_database
    )


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------


def test_a_future_parses_with_no_strike_and_no_right() -> None:
    record = contract_record_from_observation(_observation(REAL_STOCK_FUTURE_PAYLOAD))
    assert record.contract_type == "STF"
    assert record.nse_instrument_id == 58074
    assert record.underlying_symbol == "360ONE"
    assert record.expiry_date == date(2026, 8, 25)
    assert record.strike_price is None
    assert record.option_right_code is None
    assert record.close_price == pytest.approx(1169.70)
    assert record.total_traded_value == pytest.approx(686918100.00)
    assert record.lot_size == 500
    assert record.instrument_name == "360ONE26AUGFUT"


def test_an_option_parses_with_its_strike_and_right() -> None:
    record = contract_record_from_observation(_observation(REAL_INDEX_OPTION_PAYLOAD))
    assert record.contract_type == "IDO"
    assert record.strike_price == pytest.approx(24800.0)
    assert record.option_right_code == "CE"
    assert record.underlying_price == pytest.approx(24773.15)


def test_the_availability_time_is_the_instant_this_project_observed_the_row() -> None:
    record = contract_record_from_observation(_observation(REAL_INDEX_OPTION_PAYLOAD))
    assert record.availability_time == OBSERVED_AT
    assert record.availability_time.date() >= record.trade_date


def test_a_row_with_no_close_price_is_refused_rather_than_zeroed() -> None:
    payload = dict(REAL_INDEX_OPTION_PAYLOAD, ClsPric="")
    with pytest.raises(DerivativeContractProjectionError, match="ClsPric"):
        contract_record_from_observation(_observation(payload))


def test_a_non_numeric_strike_is_refused_rather_than_silently_zeroed() -> None:
    payload = dict(REAL_INDEX_OPTION_PAYLOAD, StrkPric="ATM")
    with pytest.raises(DerivativeContractProjectionError, match="StrkPric"):
        contract_record_from_observation(_observation(payload))


def test_an_observation_from_another_source_is_refused() -> None:
    """A cash row projected into the derivatives table would be invisible and wrong."""
    observation = _observation(REAL_INDEX_OPTION_PAYLOAD)
    cash = type(observation)(
        source_name="nse_bhavcopy_cash",
        natural_key=observation.natural_key,
        effective_date=observation.effective_date,
        observed_at=observation.observed_at,
        content_hash=observation.content_hash,
        values=observation.values,
        fetch_id=observation.fetch_id,
    )
    with pytest.raises(DerivativeContractProjectionError, match="nse_bhavcopy_cash"):
        contract_record_from_observation(cash)


def test_a_trade_date_disagreeing_with_the_effective_date_is_refused() -> None:
    """The two are the same fact; a disagreement means the file's shape changed under us."""
    payload = dict(REAL_INDEX_OPTION_PAYLOAD, TradDt="2026-08-17")
    observation = _observation(REAL_INDEX_OPTION_PAYLOAD)
    drifted = type(observation)(
        source_name=observation.source_name,
        natural_key=observation.natural_key,
        effective_date=date(2026, 8, 18),
        observed_at=observation.observed_at,
        content_hash=observation.content_hash,
        values=payload,
        fetch_id=observation.fetch_id,
    )
    with pytest.raises(DerivativeContractProjectionError, match="TradDt"):
        contract_record_from_observation(drifted)


# ---------------------------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------------------------


def test_the_migration_adds_the_columns_the_slice_needs_and_keeps_the_legacy_rows(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.ensure_schema()
    connection = sqlite3.connect(market_database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(fo_bhavcopy_contracts)")}
    assert {
        "total_traded_value",
        "trades_executed",
        "lot_size",
        "instrument_name",
        "availability_time",
        "content_hash",
    } <= columns
    assert connection.execute("SELECT COUNT(*) FROM fo_bhavcopy_contracts").fetchone()[0] == 1
    connection.close()


def test_the_migration_is_idempotent(market_database: Path, ingest_database: Path) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.ensure_schema()
        projection.ensure_schema()


def test_the_migration_builds_the_table_when_it_does_not_exist_at_all(
    tmp_path: Path, ingest_database: Path
) -> None:
    empty = tmp_path / "fresh.sqlite3"
    with _projection(empty, ingest_database) as projection:
        projection.ensure_schema()
        outcome = projection.project()
    assert outcome.rows_written == 2


# ---------------------------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------------------------


def test_projecting_writes_the_ingested_contracts(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        outcome = projection.project()
    assert outcome.rows_written == 2
    assert outcome.sessions_projected == 1
    assert outcome.latest_session == date(2026, 8, 18)
    connection = sqlite3.connect(market_database)
    rows = connection.execute(
        "SELECT contract_type, total_traded_value, lot_size FROM fo_bhavcopy_contracts "
        "WHERE trade_date = '2026-08-18' ORDER BY contract_type"
    ).fetchall()
    connection.close()
    assert rows == [("IDO", 866.25, 75), ("STF", 686918100.0, 500)]


def test_a_second_projection_writes_nothing(market_database: Path, ingest_database: Path) -> None:
    """Idempotence on real data, not on a fixture: the cursor and the content hash both hold."""
    with _projection(market_database, ingest_database) as projection:
        first = projection.project()
        second = projection.project()
    assert first.rows_written == 2
    assert second.rows_written == 0
    assert second.sessions_projected == 0


def test_a_revised_observation_updates_the_row_rather_than_duplicating_it(
    market_database: Path, ingest_database: Path
) -> None:
    """NSE republishes corrected bhavcopies; a correction must revise, never accumulate."""
    with _projection(market_database, ingest_database) as projection:
        projection.project()
    corrected = dict(REAL_INDEX_OPTION_PAYLOAD, ClsPric="1.95", SttlmPric="1.95")
    later = datetime(2026, 8, 19, 6, 0, 0, tzinfo=UTC)
    with BitemporalIngestStore(ingest_database) as store:
        fetch_id = store.record_fetch(
            source_name=F_AND_O_INGEST_SOURCE_NAME,
            url="https://example.invalid/fo.zip",
            fetched_at=later,
            fetch_status="ok",
            evidence="correction",
            attempts=1,
            http_status=200,
        )
        store.ingest_rows(
            F_AND_O_INGEST_SOURCE_NAME,
            [_ingest_row(corrected)],
            observed_at=later,
            fetch_id=fetch_id,
        )
    with _projection(market_database, ingest_database) as projection:
        outcome = projection.project(since=date(2026, 8, 18))
    assert outcome.rows_written == 1
    connection = sqlite3.connect(market_database)
    close_price, availability = connection.execute(
        "SELECT close_price, availability_time FROM fo_bhavcopy_contracts "
        "WHERE nse_instrument_id = 67233 AND trade_date = '2026-08-18'"
    ).fetchone()
    count = connection.execute(
        "SELECT COUNT(*) FROM fo_bhavcopy_contracts "
        "WHERE nse_instrument_id = 67233 AND trade_date = '2026-08-18'"
    ).fetchone()[0]
    connection.close()
    assert close_price == pytest.approx(1.95)
    assert count == 1
    assert availability.startswith("2026-08-19T06:00")


def test_the_cursor_advances_and_a_later_run_reads_only_new_sessions(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.project()
        assert projection.last_projected_session() == date(2026, 8, 18)
        outcome = projection.project()
    assert outcome.sessions_read == 0


def test_projecting_an_explicit_session_ignores_the_cursor(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.project()
        outcome = projection.project(only=(date(2026, 8, 18),))
    assert outcome.sessions_read == 1
    assert outcome.rows_written == 0
    assert outcome.rows_unchanged == 2


def test_an_unprojectable_row_is_counted_by_reason_and_never_silently_dropped(
    market_database: Path, tmp_path: Path
) -> None:
    ingest_database = tmp_path / "broken_ingest.sqlite3"
    broken = dict(REAL_INDEX_OPTION_PAYLOAD, ClsPric="")
    with BitemporalIngestStore(ingest_database) as store:
        fetch_id = store.record_fetch(
            source_name=F_AND_O_INGEST_SOURCE_NAME,
            url="https://example.invalid/fo.zip",
            fetched_at=OBSERVED_AT,
            fetch_status="ok",
            evidence="test fixture",
            attempts=1,
            http_status=200,
        )
        store.ingest_rows(
            F_AND_O_INGEST_SOURCE_NAME,
            [_ingest_row(REAL_STOCK_FUTURE_PAYLOAD), _ingest_row(broken)],
            observed_at=OBSERVED_AT,
            fetch_id=fetch_id,
        )
    with _projection(market_database, ingest_database) as projection:
        outcome = projection.project()
    assert outcome.rows_written == 1
    assert sum(outcome.rows_refused_by_reason.values()) == 1
    assert any("ClsPric" in reason for reason in outcome.rows_refused_by_reason)


# ---------------------------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------------------------


def test_no_projected_contract_expires_before_the_session_it_traded_in(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.project()
    connection = sqlite3.connect(market_database)
    violations = connection.execute(
        "SELECT COUNT(*) FROM fo_bhavcopy_contracts "
        "WHERE availability_time IS NOT NULL AND expiry_date < trade_date"
    ).fetchone()[0]
    connection.close()
    assert violations == 0


def test_every_projected_row_carries_an_availability_time_at_or_after_its_session(
    market_database: Path, ingest_database: Path
) -> None:
    with _projection(market_database, ingest_database) as projection:
        projection.project()
    connection = sqlite3.connect(market_database)
    violations = connection.execute(
        "SELECT COUNT(*) FROM fo_bhavcopy_contracts "
        "WHERE availability_time IS NOT NULL AND SUBSTR(availability_time, 1, 10) < trade_date"
    ).fetchone()[0]
    connection.close()
    assert violations == 0


def test_the_legacy_rows_keep_a_null_availability_time_rather_than_an_invented_one(
    market_database: Path, ingest_database: Path
) -> None:
    """A guessed availability time on a row we never timestamped would leak the future silently."""
    with _projection(market_database, ingest_database) as projection:
        projection.project()
    connection = sqlite3.connect(market_database)
    availability = connection.execute(
        "SELECT availability_time FROM fo_bhavcopy_contracts WHERE trade_date = '2026-07-22'"
    ).fetchone()[0]
    connection.close()
    assert availability is None


LEGACY_PRE_UDIFF_PAYLOAD = {
    "": "",
    "CHG_IN_OI": "18600",
    "CLOSE": "1.65",
    "CONTRACTS": "7",
    "EXPIRY_DT": "30-Jan-2020",
    "HIGH": "1.80",
    "INSTRUMENT": "OPTIDX",
    "LOW": "1.50",
    "OPEN": "1.50",
    "OPEN_INT": "93000",
    "OPTION_TYP": "CE",
    "SETTLE_PR": "1.65",
    "STRIKE_PR": "12000.00",
    "SYMBOL": "NIFTY",
    "TIMESTAMP": "02-Jan-2020",
    "VAL_INLAKH": "8.66",
}


def test_the_pre_udiff_layout_is_refused_by_name_rather_than_half_populated() -> None:
    """Measured on the real store: 2020-01-02 carries 31,835 rows in the OLD NSE layout.

    That layout publishes no underlying price and no contract id. A row projected without an
    underlying price would be read by `FuturesBasisCarryEngine` and `VarianceRiskPremiumEngine`,
    both of which need exactly that field — so a partial row is worse than no row, and the refusal
    names the layout so the count is diagnosable.
    """
    observation = _observation(REAL_INDEX_OPTION_PAYLOAD)
    legacy = type(observation)(
        source_name=observation.source_name,
        natural_key=observation.natural_key,
        effective_date=date(2020, 1, 2),
        observed_at=observation.observed_at,
        content_hash=observation.content_hash,
        values=LEGACY_PRE_UDIFF_PAYLOAD,
        fetch_id=observation.fetch_id,
    )
    with pytest.raises(DerivativeContractProjectionError, match="pre-UDiFF"):
        contract_record_from_observation(legacy)
