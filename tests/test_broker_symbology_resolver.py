"""`L0.17` — symbology, and the refusals that stop it mapping to the wrong company.

A resolver that guesses is worse than one that fails: a wrong token fetches a different
company's price history and nothing downstream can tell.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.broker_symbology.angel_one_symbology_resolver import (
    MINIMUM_CREDIBLE_EQUITY_ROWS,
    AngelOneSymbologyResolver,
    parse_equity_symbols,
)
from nse_algo_trader.broker_symbology.broker_symbology_resolver import (
    BrokerSymbol,
    BrokerSymbologyResolver,
    BrokerSymbolStore,
    SymbologyError,
    coverage_against,
)

TODAY = date(2026, 8, 11)


def _angel_row(name: str, token: str, series: str = "EQ", segment: str = "NSE") -> dict[str, Any]:
    """Angel's real row shape, from the live master."""
    return {
        "token": token,
        "symbol": f"{name}-{series}" if series else name,
        "name": name,
        "exch_seg": segment,
        "lotsize": "1",
    }


def _credible_master(count: int = MINIMUM_CREDIBLE_EQUITY_ROWS + 10) -> list[dict[str, Any]]:
    return [_angel_row(f"SYM{index:04d}", str(index + 1000)) for index in range(count)]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[BrokerSymbolStore]:
    with BrokerSymbolStore(tmp_path / "symbology.sqlite3") as opened:
        yield opened


@pytest.mark.unit
def test_the_resolver_satisfies_the_interface(store: BrokerSymbolStore) -> None:
    assert isinstance(AngelOneSymbologyResolver(store), BrokerSymbologyResolver)


@pytest.mark.unit
def test_equity_series_are_mapped_and_everything_else_is_not() -> None:
    """The NSE segment also carries 4,295 government securities and 972 bonds."""
    parsed = parse_equity_symbols(
        [
            _angel_row("RELIANCE", "2885", "EQ"),
            _angel_row("SOMEBE", "111", "BE"),
            _angel_row("SOMESME", "222", "SM"),
            _angel_row("GOVTSEC", "333", "SG"),
            _angel_row("SOMEBOND", "444", "N0"),
            _angel_row("SOMEFUND", "555", "MF"),
        ],
        refreshed_on=TODAY,
    )
    assert {symbol.tradingsymbol for symbol in parsed} == {"RELIANCE", "SOMEBE", "SOMESME"}


@pytest.mark.unit
def test_other_exchanges_are_ignored() -> None:
    parsed = parse_equity_symbols(
        [_angel_row("RELIANCE", "2885", "EQ", segment="BSE")], refreshed_on=TODAY
    )
    assert parsed == []


@pytest.mark.unit
def test_a_symbol_without_a_series_suffix_is_skipped_not_guessed() -> None:
    parsed = parse_equity_symbols(
        [{"token": "1", "symbol": "NOSUFFIX", "name": "NOSUFFIX", "exch_seg": "NSE"}],
        refreshed_on=TODAY,
    )
    assert parsed == []


@pytest.mark.unit
def test_a_colliding_name_is_refused_rather_than_first_wins() -> None:
    """Measured as impossible today — 2,485 -EQ rows, 2,485 distinct names.

    Kept as a refusal because if Angel ever does publish a collision, picking one maps a
    symbol to a different company with no signal at all.
    """
    with pytest.raises(SymbologyError):
        parse_equity_symbols(
            [_angel_row("RELIANCE", "2885"), _angel_row("RELIANCE", "9999")],
            refreshed_on=TODAY,
        )


@pytest.mark.unit
def test_a_truncated_master_never_replaces_good_symbology(store: BrokerSymbolStore) -> None:
    """The failure that would silently unname the universe."""
    resolver = AngelOneSymbologyResolver(store, fetch_master=_credible_master)
    assert resolver.refresh(today=TODAY) > MINIMUM_CREDIBLE_EQUITY_ROWS

    truncated = AngelOneSymbologyResolver(
        store, fetch_master=lambda: [_angel_row("RELIANCE", "2885")]
    )
    with pytest.raises(SymbologyError):
        truncated.refresh(today=TODAY)
    # The good mapping survived the bad download.
    assert store.count_for(BrokerName.ANGEL_ONE) > MINIMUM_CREDIBLE_EQUITY_ROWS


@pytest.mark.unit
def test_an_empty_master_is_a_fetch_failure_not_an_empty_exchange(
    store: BrokerSymbolStore,
) -> None:
    with pytest.raises(SymbologyError):
        store.replace_for(BrokerName.ANGEL_ONE, [])


@pytest.mark.unit
def test_refresh_replaces_rather_than_accumulates(store: BrokerSymbolStore) -> None:
    """Yesterday's token beside today's would reintroduce the ambiguity this removes."""
    first = _credible_master()
    AngelOneSymbologyResolver(store, fetch_master=lambda: first).refresh(today=TODAY)
    second = [
        _angel_row(f"OTHER{i:04d}", str(i + 5000)) for i in range(MINIMUM_CREDIBLE_EQUITY_ROWS + 10)
    ]
    AngelOneSymbologyResolver(store, fetch_master=lambda: second).refresh(today=TODAY)
    assert store.identifier_for(BrokerName.ANGEL_ONE, "SYM0000") is None
    assert store.identifier_for(BrokerName.ANGEL_ONE, "OTHER0000") == "5000"


@pytest.mark.unit
def test_an_unknown_symbol_resolves_to_none_rather_than_a_guess(
    store: BrokerSymbolStore,
) -> None:
    resolver = AngelOneSymbologyResolver(store, fetch_master=_credible_master)
    resolver.refresh(today=TODAY)
    assert resolver.resolve("DEFINITELY_NOT_LISTED") is None


@pytest.mark.unit
def test_every_broker_that_can_name_an_instrument_is_returned_together(
    store: BrokerSymbolStore,
) -> None:
    """What a `BarInstrument` needs: one lookup, every broker's identifier."""
    store.replace_for(
        BrokerName.ANGEL_ONE,
        [BrokerSymbol(BrokerName.ANGEL_ONE, "NSE", "RELIANCE", "2885", "RELIANCE-EQ", "EQ", TODAY)],
    )
    store.replace_for(
        BrokerName.UPSTOX,
        [
            BrokerSymbol(
                BrokerName.UPSTOX, "NSE", "RELIANCE", "INE002A01018", "RELIANCE", "EQ", TODAY
            )
        ],
    )
    identifiers = store.identifiers_for_all_brokers("RELIANCE")
    assert identifiers == {
        BrokerName.ANGEL_ONE: "2885",
        BrokerName.UPSTOX: "INE002A01018",
    }


@pytest.mark.unit
def test_an_empty_identifier_is_refused_at_construction() -> None:
    with pytest.raises(SymbologyError):
        BrokerSymbol(BrokerName.ANGEL_ONE, "NSE", "RELIANCE", "", "RELIANCE-EQ", "EQ", TODAY)


@pytest.mark.unit
def test_coverage_makes_single_sourced_reconciliation_visible(
    store: BrokerSymbolStore,
) -> None:
    """Reconciliation is only cross-source for instruments every broker can name."""
    store.replace_for(
        BrokerName.ANGEL_ONE,
        [BrokerSymbol(BrokerName.ANGEL_ONE, "NSE", "RELIANCE", "2885", "RELIANCE-EQ", "EQ", TODAY)],
    )
    coverage = coverage_against(store, [BrokerName.ANGEL_ONE], ["RELIANCE", "INFY", "TCS"])
    assert coverage[BrokerName.ANGEL_ONE] == (1, 3)
