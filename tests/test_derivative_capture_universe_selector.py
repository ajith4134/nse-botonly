"""Tests for the F&O capture universe selector (`A.142` / `A.146`), written BEFORE the engine.

`A.142` measured the reason five of six segment bots have no intraday tape: *"they are not short of
data because the data does not exist — they are short of it because nothing ever asked for it."*
This selector is what asks. Every test here defends one of two properties: the selection contains
only contracts that can actually be subscribed and are actually still alive, and the band around the
money is MEASURED from traded value rather than asserted as a strike count (`R.03`).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.market_depth.derivative_capture_universe_selector import (
    DerivativeCaptureSelectionError,
    select_derivative_capture_universe,
)

TRADE_DATE = "2026-08-18"
AS_OF = date(2026, 8, 19)


def _contract(
    connection: sqlite3.Connection,
    *,
    contract_type: str,
    underlying: str,
    expiry: str,
    instrument_name: str,
    strike: float | None = None,
    right: str | None = None,
    traded_value: float = 0.0,
    open_interest: int = 0,
    trade_date: str = TRADE_DATE,
    close_price: float = 100.0,
) -> None:
    connection.execute(
        """
        INSERT INTO fo_bhavcopy_contracts (
            trade_date, contract_type, nse_instrument_id, underlying_symbol, expiry_date,
            strike_price, option_right_code, open_price, high_price, low_price, close_price,
            settlement_price, underlying_price, open_interest, change_in_open_interest,
            total_traded_volume, total_traded_value, trades_executed, lot_size, instrument_name,
            availability_time, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_date,
            contract_type,
            abs(hash((trade_date, instrument_name))) % 10_000_000,
            underlying,
            expiry,
            strike,
            right,
            close_price,
            close_price,
            close_price,
            close_price,
            close_price,
            24_800.0,
            open_interest,
            0,
            1,
            traded_value,
            1,
            75,
            instrument_name,
            "2026-08-19T03:43:24+00:00",
            instrument_name,
        ),
    )


def _master(
    connection: sqlite3.Connection, symbol: str, token: int, segment: str = "NFO-OPT"
) -> None:
    connection.execute(
        """
        INSERT INTO instrument_master (ingested_on, exchange, segment, tradingsymbol,
                                       instrument_token, exchange_token, name, last_price,
                                       expiry, strike, tick_size, lot_size, instrument_type)
        VALUES ('2026-08-19', 'NFO', ?, ?, ?, ?, '', 0, '', 0, 5, 75, 'CE')
        """,
        (segment, symbol, token, token),
    )


@pytest.fixture
def market_data(tmp_path: Path) -> Path:
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE fo_bhavcopy_contracts (
            trade_date TEXT NOT NULL, contract_type TEXT NOT NULL,
            nse_instrument_id INTEGER NOT NULL, underlying_symbol TEXT NOT NULL,
            expiry_date TEXT NOT NULL, strike_price REAL, option_right_code TEXT,
            open_price REAL NOT NULL, high_price REAL NOT NULL, low_price REAL NOT NULL,
            close_price REAL NOT NULL, settlement_price REAL NOT NULL,
            underlying_price REAL NOT NULL, open_interest INTEGER NOT NULL,
            change_in_open_interest INTEGER NOT NULL, total_traded_volume INTEGER NOT NULL,
            total_traded_value REAL, trades_executed INTEGER, lot_size INTEGER,
            instrument_name TEXT, availability_time TEXT, content_hash TEXT,
            PRIMARY KEY (trade_date, nse_instrument_id));
        CREATE TABLE instrument_master (
            ingested_on TEXT, exchange TEXT, segment TEXT, tradingsymbol TEXT,
            instrument_token INTEGER, exchange_token INTEGER, name TEXT, last_price REAL,
            expiry TEXT, strike REAL, tick_size REAL, lot_size INTEGER, instrument_type TEXT);
        """
    )
    connection.commit()
    return path


def _populate_a_realistic_chain(market_data: Path) -> None:
    """One index, two expiries, a stock future, and a settled contract that must be dropped."""
    connection = sqlite3.connect(market_data)
    # Near expiry: liquidity concentrated at 24800, thinning away from it.
    for strike, value in ((24_600, 5_000.0), (24_800, 900_000.0), (25_000, 40_000.0)):
        for right in ("CE", "PE"):
            symbol = f"NIFTY26AUG{strike}{right}"
            _contract(
                connection,
                contract_type="IDO",
                underlying="NIFTY",
                expiry="2026-08-27",
                instrument_name=symbol,
                strike=float(strike),
                right=right,
                traded_value=value,
            )
            _master(connection, symbol, token=abs(hash(symbol)) % 900_000 + 1)
    # A LATER expiry on the same underlying — must not be selected while a nearer one is alive.
    far = "NIFTY26SEP24800CE"
    _contract(
        connection,
        contract_type="IDO",
        underlying="NIFTY",
        expiry="2026-09-24",
        instrument_name=far,
        strike=24_800.0,
        right="CE",
        traded_value=2_000_000.0,
    )
    _master(connection, far, token=777_001)
    # A contract that settled on the bhavcopy session itself.
    settled = "NIFTY26AUG1824800CE"
    _contract(
        connection,
        contract_type="IDO",
        underlying="NIFTY",
        expiry="2026-08-18",
        instrument_name=settled,
        strike=24_800.0,
        right="CE",
        traded_value=9_000_000.0,
    )
    _master(connection, settled, token=777_002)
    # Two futures, both alive.
    for symbol, expiry in (("RELIANCE26AUGFUT", "2026-08-25"), ("RELIANCE26SEPFUT", "2026-09-24")):
        _contract(
            connection,
            contract_type="STF",
            underlying="RELIANCE",
            expiry=expiry,
            instrument_name=symbol,
            traded_value=500_000.0,
        )
        _master(connection, symbol, token=abs(hash(symbol)) % 900_000 + 1, segment="NFO-FUT")
    connection.commit()
    connection.close()


# ---------------------------------------------------------------------------------------------


def test_a_settled_contract_is_never_selected(market_data: Path) -> None:
    """It carried the HIGHEST traded value in the fixture — a rank-only cut would have picked it."""
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    names = {contract.trading_symbol for contract in universe.contracts}
    assert "NIFTY26AUG1824800CE" not in names
    assert universe.dropped_by_reason.get("already settled") == 1


def test_only_the_nearest_live_option_expiry_per_underlying_is_selected(market_data: Path) -> None:
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    option_expiries = {
        contract.expiry for contract in universe.contracts if contract.option_right_code
    }
    assert option_expiries == {date(2026, 8, 27)}


def test_every_live_future_expiry_is_selected(market_data: Path) -> None:
    """Futures are ~640 contracts in total; there is no budget reason to cut them by expiry."""
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    future_expiries = {
        contract.expiry for contract in universe.contracts if contract.option_right_code is None
    }
    assert future_expiries == {date(2026, 8, 25), date(2026, 9, 24)}


def test_options_are_ranked_by_their_own_traded_value(market_data: Path) -> None:
    """The band around the money EMERGES from liquidity; it is never an asserted strike count."""
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    options = [c for c in universe.contracts if c.option_right_code]
    values = [c.traded_value for c in options]
    assert values == sorted(values, reverse=True)
    assert options[0].strike_price == pytest.approx(24_800.0)


def test_a_contract_with_no_subscribable_token_is_dropped_and_counted(market_data: Path) -> None:
    """`segment_universe_assembler` invents a negative token for its own use. A token that cannot
    be subscribed must never reach a subscription list, so this selector drops instead."""
    _populate_a_realistic_chain(market_data)
    connection = sqlite3.connect(market_data)
    _contract(
        connection,
        contract_type="IDO",
        underlying="NIFTY",
        expiry="2026-08-27",
        instrument_name="NIFTY26AUG25200CE",
        strike=25_200.0,
        right="CE",
        traded_value=10_000.0,
    )
    connection.commit()
    connection.close()
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    names = {contract.trading_symbol for contract in universe.contracts}
    assert "NIFTY26AUG25200CE" not in names
    assert universe.dropped_by_reason.get("no instrument token") == 1


def test_the_selection_is_bounded_when_a_bound_is_given(market_data: Path) -> None:
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(
        as_of=AS_OF, market_data=market_data, option_contract_limit=2
    )
    options = [c for c in universe.contracts if c.option_right_code]
    assert len(options) == 2
    assert "BOUNDED" in universe.note


def test_the_bound_never_touches_the_futures(market_data: Path) -> None:
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(
        as_of=AS_OF, market_data=market_data, option_contract_limit=1
    )
    futures = [c for c in universe.contracts if c.option_right_code is None]
    assert len(futures) == 2


def test_every_selected_contract_carries_the_exchange_the_feed_needs(market_data: Path) -> None:
    """The websocket seam keys the exchange per token; an unset exchange subscribes nothing."""
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    assert {contract.exchange for contract in universe.contracts} == {"NFO"}


def test_an_empty_contract_table_is_answerable_rather_than_fatal(market_data: Path) -> None:
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    assert universe.contracts == ()
    assert "no F&O contract rows" in universe.note


def test_a_market_database_without_the_table_is_refused_by_name(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    with pytest.raises(DerivativeCaptureSelectionError, match="fo_bhavcopy_contracts"):
        select_derivative_capture_universe(as_of=AS_OF, market_data=empty)


def test_selection_is_deterministic(market_data: Path) -> None:
    _populate_a_realistic_chain(market_data)
    first = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    second = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    assert [c.instrument_token for c in first.contracts] == [
        c.instrument_token for c in second.contracts
    ]


def test_no_token_appears_twice(market_data: Path) -> None:
    """A duplicate subscription is a wasted slot in a budget the controller solves exactly."""
    _populate_a_realistic_chain(market_data)
    universe = select_derivative_capture_universe(as_of=AS_OF, market_data=market_data)
    tokens = [contract.instrument_token for contract in universe.contracts]
    assert len(tokens) == len(set(tokens))
