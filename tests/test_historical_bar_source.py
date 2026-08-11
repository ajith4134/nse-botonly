"""`L0.14` — the interface, and the broker quirks it exists to absorb.

Each adapter is tested against a fake that reproduces its REAL broker's shape, taken from
the live responses measured on 2026-08-11: Kite's dicts with `date`, Angel's positional
lists with a `status` flag. A fake that returned a convenient shape would test nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.historical_bars.angel_one_historical_bar_source import (
    AngelOneHistoricalBarSource,
)
from nse_algo_trader.historical_bars.historical_bar_source import (
    BarInstrument,
    BarInterval,
    BarRequest,
    BrokerAuthenticationError,
    HistoricalBarSource,
    HistoricalBarSourceError,
    UnsupportedIntervalError,
    first_source_supporting,
)
from nse_algo_trader.historical_bars.kite_historical_bar_source import (
    KiteHistoricalBarSource,
)

IST = ZoneInfo("Asia/Kolkata")

RELIANCE = BarInstrument(
    exchange="NSE",
    segment="CASH",
    tradingsymbol="RELIANCE",
    broker_identifiers={
        BrokerName.ZERODHA_KITE: "738561",
        BrokerName.ANGEL_ONE: "2885",
    },
)


def _request(interval: BarInterval = BarInterval.ONE_DAY) -> BarRequest:
    return BarRequest(RELIANCE, interval, date(2026, 8, 10), date(2026, 8, 11))


class FakeKite:
    """Kite's real shape: list of dicts, `date` key, float prices."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self._rows = rows or [
            {
                "date": datetime(2026, 8, 11, tzinfo=IST),
                "open": 1326.6,
                "high": 1328.7,
                "low": 1314.1,
                "close": 1320.6,
                "volume": 8508600,
            }
        ]
        self._error = error
        self.calls: list[tuple[Any, ...]] = []

    def historical_data(self, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(args)
        if self._error:
            raise self._error
        return self._rows


class FakeAngel:
    """Angel's real shape: positional lists, and refusal INSIDE a 200 response."""

    def __init__(self, response: dict[str, Any] | None = None):
        self._response = response or {
            "status": True,
            "data": [["2026-08-11T09:15:00+05:30", 1326.6, 1328.7, 1314.1, 1320.6, 8701285]],
        }
        self.calls: list[dict[str, Any]] = []

    def getCandleData(self, parameters: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        self.calls.append(parameters)
        return self._response


@pytest.mark.unit
def test_both_adapters_satisfy_the_one_interface() -> None:
    assert isinstance(KiteHistoricalBarSource(FakeKite()), HistoricalBarSource)
    assert isinstance(AngelOneHistoricalBarSource(FakeAngel()), HistoricalBarSource)


@pytest.mark.unit
def test_prices_become_decimal_without_inheriting_float_error() -> None:
    """`Decimal(1320.6)` carries the binary error; `Decimal(str(...))` does not."""
    (bar,) = KiteHistoricalBarSource(FakeKite()).fetch_bars(_request())
    assert bar.close_price == Decimal("1320.6")
    assert str(bar.close_price) == "1320.6"


@pytest.mark.unit
def test_available_from_is_the_bar_close_not_the_fetch_time() -> None:
    """A bar becomes actionable when it FINISHES forming.

    Its start would be a one-bar lookahead; the fetch time would make every historical
    bar unavailable during replay. Both are invisible in summary statistics.
    """
    (bar,) = KiteHistoricalBarSource(FakeKite()).fetch_bars(_request())
    assert bar.available_from > bar.bar_timestamp
    assert (bar.available_from - bar.bar_timestamp).total_seconds() == 1440 * 60


@pytest.mark.unit
def test_each_broker_is_asked_in_its_own_vocabulary() -> None:
    """The whole point: one request, two dialects."""
    kite, angel = FakeKite(), FakeAngel()
    KiteHistoricalBarSource(kite).fetch_bars(_request(BarInterval.FIVE_MINUTES))
    AngelOneHistoricalBarSource(angel).fetch_bars(_request(BarInterval.FIVE_MINUTES))
    assert kite.calls[0][0] == 738561
    assert kite.calls[0][3] == "5minute"
    assert angel.calls[0]["symboltoken"] == "2885"
    assert angel.calls[0]["interval"] == "FIVE_MINUTE"


@pytest.mark.unit
def test_kite_uses_minute_not_one_minute() -> None:
    """Kite breaks its own naming pattern; a plausible `1minute` is rejected by the API."""
    kite = FakeKite()
    KiteHistoricalBarSource(kite).fetch_bars(_request(BarInterval.ONE_MINUTE))
    assert kite.calls[0][3] == "minute"


@pytest.mark.unit
def test_angel_refusal_inside_a_200_is_an_error_not_an_empty_result() -> None:
    """The quirk that matters most.

    Angel signals failure with `status: false` in a successful HTTP response. Reading
    that as 'no data' turns a broken feed into a silent hole in history.
    """
    angel = AngelOneHistoricalBarSource(
        FakeAngel({"status": False, "message": "Invalid symbol token"})
    )
    with pytest.raises(HistoricalBarSourceError) as raised:
        angel.fetch_bars(_request())
    assert "Invalid symbol token" in str(raised.value)


@pytest.mark.unit
def test_an_angel_session_failure_is_distinguishable_from_bad_data() -> None:
    angel = AngelOneHistoricalBarSource(
        FakeAngel({"status": False, "message": "Invalid session token"})
    )
    with pytest.raises(BrokerAuthenticationError):
        angel.fetch_bars(_request())


@pytest.mark.unit
def test_a_kite_credential_failure_is_not_reported_as_missing_data() -> None:
    source = KiteHistoricalBarSource(FakeKite(error=RuntimeError("Invalid access token")))
    with pytest.raises(BrokerAuthenticationError):
        source.fetch_bars(_request())


@pytest.mark.unit
def test_a_missing_broker_identifier_names_the_component_that_would_supply_it() -> None:
    unmapped = BarInstrument("NSE", "CASH", "INFY", {BrokerName.ZERODHA_KITE: "408065"})
    source = AngelOneHistoricalBarSource(FakeAngel())
    with pytest.raises(HistoricalBarSourceError) as raised:
        source.fetch_bars(
            BarRequest(unmapped, BarInterval.ONE_DAY, date(2026, 8, 10), date(2026, 8, 11))
        )
    assert "L0.17" in str(raised.value)


@pytest.mark.unit
def test_a_backwards_window_is_refused_at_construction() -> None:
    with pytest.raises(HistoricalBarSourceError):
        BarRequest(RELIANCE, BarInterval.ONE_DAY, date(2026, 8, 11), date(2026, 8, 10))


@pytest.mark.unit
def test_routing_raises_rather_than_returning_none_when_nothing_supports_it() -> None:
    """'No broker can serve this' must not look like 'the window was empty'."""

    class NoIntervals:
        broker = BrokerName.UPSTOX

        def supports(self, interval: BarInterval) -> bool:
            return False

        def fetch_bars(self, request: BarRequest) -> list[Any]:
            return []

    with pytest.raises(UnsupportedIntervalError):
        first_source_supporting([NoIntervals()], BarInterval.ONE_DAY)


@pytest.mark.unit
def test_open_interest_absent_is_none_not_zero() -> None:
    """No open interest reported is a different claim from open interest of zero."""
    (bar,) = KiteHistoricalBarSource(FakeKite()).fetch_bars(_request())
    assert bar.open_interest is None
