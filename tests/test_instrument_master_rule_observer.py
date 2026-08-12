"""`L0.31` observed-fact source — rule history derived from what the exchange published.

`A.80` set the grade ladder with `OBSERVED_FROM_EXCHANGE_DATA` at the top and then shipped
nothing that produced it. This is that source, and the tests are the ways a snapshot-diff
change detector quietly lies: closing a run at the wrong boundary, extrapolating before the
first capture, or answering a per-symbol question with a segment-wide value.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.market_rules.instrument_master_rule_observer import (
    InstrumentMasterRuleObserver,
    InstrumentMasterUnavailableError,
)
from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    PointInTimeMarketRuleStore,
    RuleCoverageError,
    RuleFamily,
    RuleScope,
)

LIVE_MARKET_DATA = Path("/home/opc/.nse_algo_trader/market_data.sqlite3")


def _snapshot_database(tmp_path: Path, rows: list[tuple[str, str, str, str, int]]) -> Path:
    """(ingested_on, exchange, segment, tradingsymbol, ...) written as the real schema does."""
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE instrument_master (ingested_on TEXT, exchange TEXT, segment TEXT,"
        " tradingsymbol TEXT, tick_size TEXT, lot_size INTEGER)"
    )
    connection.executemany(
        "INSERT INTO instrument_master VALUES (?,?,?,?,?,?)",
        [(d, "NSE", seg, sym, tick, lot) for d, seg, sym, tick, lot in rows],
    )
    connection.commit()
    connection.close()
    return path


@pytest.mark.hermetic
def test_an_unchanged_value_is_one_open_ended_run(tmp_path: Path) -> None:
    """Three identical snapshots are one fact, not three."""
    path = _snapshot_database(
        tmp_path,
        [
            ("2026-08-10", "NSE", "RELIANCE", "0.10", 1),
            ("2026-08-11", "NSE", "RELIANCE", "0.10", 1),
            ("2026-08-12", "NSE", "RELIANCE", "0.10", 1),
        ],
    )
    records = InstrumentMasterRuleObserver(path).records_for(
        RuleFamily.TICK_SIZE, RuleScope(symbol="RELIANCE")
    )
    assert len(records) == 1
    assert records[0].value == "0.10"
    assert records[0].effective_from == date(2026, 8, 10)
    assert records[0].effective_to is None, "the latest observation is still in force"
    assert records[0].grade is EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA


@pytest.mark.hermetic
def test_a_change_splits_the_run_at_the_day_the_new_value_appeared(tmp_path: Path) -> None:
    """The closing boundary is the first day of the NEW value, not the last of the old.

    The change happened somewhere between the two captures. Closing at the later boundary
    can only ever shorten the old rule's claimed life; closing at the earlier one would
    leave a day uncovered that the store would then refuse, or — worse, if it were closed
    at the old value's last day plus one — assert the old rule on a day it may not have
    held.
    """
    path = _snapshot_database(
        tmp_path,
        [
            ("2026-08-10", "NSE", "ACME", "0.05", 1),
            ("2026-08-11", "NSE", "ACME", "0.05", 1),
            ("2026-08-12", "NSE", "ACME", "0.01", 1),
        ],
    )
    records = InstrumentMasterRuleObserver(path).records_for(
        RuleFamily.TICK_SIZE, RuleScope(symbol="ACME")
    )
    assert [r.value for r in records] == ["0.05", "0.01"]
    assert records[0].effective_from == date(2026, 8, 10)
    assert records[0].effective_to == date(2026, 8, 12)
    assert records[1].effective_from == date(2026, 8, 12)
    assert records[1].effective_to is None


@pytest.mark.hermetic
def test_lot_size_is_derived_from_the_same_snapshots(tmp_path: Path) -> None:
    path = _snapshot_database(
        tmp_path,
        [
            ("2026-08-10", "NFO", "NIFTY26AUGFUT", "0.05", 50),
            ("2026-08-12", "NFO", "NIFTY26AUGFUT", "0.05", 75),
        ],
    )
    records = InstrumentMasterRuleObserver(path).records_for(
        RuleFamily.LOT_SIZE, RuleScope(symbol="NIFTY26AUGFUT")
    )
    assert [r.value for r in records] == ["50", "75"]


@pytest.mark.adversarial
def test_a_scope_with_no_symbol_yields_nothing_rather_than_one_symbols_value(
    tmp_path: Path,
) -> None:
    """Tick size is per instrument. Answering a segment-wide question would fabricate."""
    path = _snapshot_database(tmp_path, [("2026-08-10", "NSE", "RELIANCE", "0.10", 1)])
    observer = InstrumentMasterRuleObserver(path)
    assert observer.records_for(RuleFamily.TICK_SIZE, RuleScope(segment="NSE")) == ()
    assert observer.records_for(RuleFamily.TICK_SIZE, RuleScope()) == ()


@pytest.mark.adversarial
def test_a_family_this_source_cannot_speak_to_yields_nothing(tmp_path: Path) -> None:
    path = _snapshot_database(tmp_path, [("2026-08-10", "NSE", "RELIANCE", "0.10", 1)])
    observer = InstrumentMasterRuleObserver(path)
    assert RuleFamily.SECURITIES_TRANSACTION_TAX not in observer.families()
    assert (
        observer.records_for(RuleFamily.SECURITIES_TRANSACTION_TAX, RuleScope(symbol="RELIANCE"))
        == ()
    )


@pytest.mark.adversarial
def test_a_missing_database_raises_rather_than_reporting_no_facts(tmp_path: Path) -> None:
    """ "No snapshots" and "no such file" are different, and the store must not merge them."""
    observer = InstrumentMasterRuleObserver(tmp_path / "absent.sqlite3")
    assert observer.observation_window() is None
    with pytest.raises(InstrumentMasterUnavailableError):
        observer.records_for(RuleFamily.TICK_SIZE, RuleScope(symbol="RELIANCE"))


@pytest.mark.adversarial
def test_the_store_still_refuses_dates_before_the_first_capture(tmp_path: Path) -> None:
    """A snapshot source must never be extrapolated backwards.

    This is the whole reason `L0.31` exists: the tick size observed in 2026 says nothing
    about 2019, and answering with it would be exactly the silent substitution the store
    was built to refuse.
    """
    path = _snapshot_database(tmp_path, [("2026-08-10", "NSE", "RELIANCE", "0.10", 1)])
    store = PointInTimeMarketRuleStore()
    store.register_source(InstrumentMasterRuleObserver(path))

    assert (
        store.resolve(
            RuleFamily.TICK_SIZE, date(2026, 8, 11), scope=RuleScope(symbol="RELIANCE")
        ).value
        == "0.10"
    )
    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.TICK_SIZE, date(2019, 6, 1), scope=RuleScope(symbol="RELIANCE"))


@pytest.mark.unit
def test_an_observed_fact_outranks_a_circular_in_the_store(tmp_path: Path) -> None:
    """The ladder `A.80` declared, now actually exercised end to end."""
    from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
        MarketRuleRecord,
        RuleValueKind,
    )

    path = _snapshot_database(tmp_path, [("2026-08-10", "NSE", "RELIANCE", "0.10", 1)])
    store = PointInTimeMarketRuleStore()
    store.add(
        MarketRuleRecord(
            family=RuleFamily.TICK_SIZE,
            scope=RuleScope(symbol="RELIANCE"),
            value="0.05",
            value_kind=RuleValueKind.DECIMAL_FRACTION,
            effective_from=date(2024, 6, 10),
            effective_to=None,
            source_reference="NSE Master Circular 3.3 — price-linked tick tiers",
            source_date=date(2024, 6, 1),
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
            recorded_at=date(2026, 8, 12),
        )
    )
    store.register_source(InstrumentMasterRuleObserver(path))

    resolution = store.resolve(
        RuleFamily.TICK_SIZE, date(2026, 8, 11), scope=RuleScope(symbol="RELIANCE")
    )
    assert resolution.value == "0.10", "what the exchange published beats what was announced"
    assert resolution.is_conflicted
    assert resolution.superseded_records[0].value == "0.05"


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_MARKET_DATA.exists(), reason="no retained market-data store")
def test_real_tick_and_lot_sizes_are_derived_from_the_live_instrument_master() -> None:
    """R.05 — against the 227,535 real rows, and it must be fast enough to serve a query.

    RELIANCE's tick size is 0.10, not the 0.05 a flat-rate assumption would use: NSE moved
    to price-linked tick tiers in Jun-2024, which is precisely the kind of change a
    hardcoded constant gets wrong and an observed fact gets right.
    """
    import time

    observer = InstrumentMasterRuleObserver(LIVE_MARKET_DATA)
    window = observer.observation_window()
    assert window is not None, "the live store has no instrument-master snapshots"

    started = time.monotonic()
    records = observer.records_for(
        RuleFamily.TICK_SIZE, RuleScope(segment="NSE", symbol="RELIANCE")
    )
    elapsed = time.monotonic() - started

    assert records, "RELIANCE has no observed tick size"
    assert elapsed < 2.0, f"a single-symbol lookup took {elapsed:.2f}s — too slow to serve"
    assert records[-1].effective_to is None, "the latest observation is still in force"
    assert records[-1].effective_from >= window[0]
    assert records[-1].grade is EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA

    store = PointInTimeMarketRuleStore()
    store.register_source(observer)
    resolution = store.resolve(
        RuleFamily.TICK_SIZE, window[1], scope=RuleScope(segment="NSE", symbol="RELIANCE")
    )
    assert resolution.as_decimal() > 0
    assert "instrument_master" in resolution.record.source_reference


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_MARKET_DATA.exists(), reason="no retained market-data store")
def test_the_observed_source_covers_many_real_symbols_not_just_one() -> None:
    """R.09 in miniature: the source must work across the universe, not on a favourite."""
    observer = InstrumentMasterRuleObserver(LIVE_MARKET_DATA)
    connection = sqlite3.connect(f"file:{LIVE_MARKET_DATA}?mode=ro", uri=True)
    symbols = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT tradingsymbol FROM instrument_master WHERE segment='NSE' "
            "ORDER BY tradingsymbol LIMIT 40"
        )
    ]
    connection.close()
    assert len(symbols) == 40

    derived = 0
    for symbol in symbols:
        records = observer.records_for(
            RuleFamily.TICK_SIZE, RuleScope(segment="NSE", symbol=symbol)
        )
        if records:
            derived += 1
            assert all(r.source_reference.strip() for r in records)
    assert derived >= 35, f"only {derived}/40 symbols yielded an observed tick size"


@pytest.mark.adversarial
def test_a_symbol_listed_on_two_exchanges_is_ambiguous_not_a_daily_change(
    tmp_path: Path,
) -> None:
    """The defect the first real-data run found.

    RELIANCE is on NSE at a 0.10 tick and on BSE at 0.05, in every snapshot. Read
    symbol-only, that is two values stamped with one date — which the change detector saw
    as a rule changing and changing back on the same day, producing a zero-length interval
    the store rejected outright. Two values for a day is an ambiguous question, not a
    change, so the source declines it and the caller pins the segment.
    """
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE instrument_master (ingested_on TEXT, exchange TEXT, segment TEXT,"
        " tradingsymbol TEXT, tick_size TEXT, lot_size INTEGER)"
    )
    connection.executemany(
        "INSERT INTO instrument_master VALUES (?,?,?,?,?,?)",
        [
            ("2026-08-11", "NSE", "NSE", "RELIANCE", "0.10", 1),
            ("2026-08-11", "BSE", "BSE", "RELIANCE", "0.05", 1),
            ("2026-08-12", "NSE", "NSE", "RELIANCE", "0.10", 1),
            ("2026-08-12", "BSE", "BSE", "RELIANCE", "0.05", 1),
        ],
    )
    connection.commit()
    connection.close()

    observer = InstrumentMasterRuleObserver(path)
    assert observer.records_for(RuleFamily.TICK_SIZE, RuleScope(symbol="RELIANCE")) == ()

    pinned = observer.records_for(RuleFamily.TICK_SIZE, RuleScope(segment="NSE", symbol="RELIANCE"))
    assert len(pinned) == 1
    assert pinned[0].value == "0.10"
