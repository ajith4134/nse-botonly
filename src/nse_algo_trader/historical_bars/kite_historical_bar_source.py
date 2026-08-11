"""`L0.14` — Kite as a historical bar source.

Kite is the reference implementation because it is the broker this project authenticates
against every morning, so it is the one whose failures are observable daily.

Its limits are real and worth stating: `historical_data` is capped per request by
interval (a minute-interval request spanning years is refused by the API, not truncated),
and Kite exposes markedly less intraday history than the deep-history brokers `L0.18`
targets. Windowing is the caller's concern via `L0.15`; this adapter reports what it got
and does not pretend a short answer was a complete one.
"""

from __future__ import annotations

from typing import Any

from nse_algo_trader.bitemporal_bar_store import BarRecord
from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.historical_bars.historical_bar_source import (
    BarInterval,
    BarRequest,
    BrokerAuthenticationError,
    HistoricalBarSourceError,
    UnsupportedIntervalError,
    to_bar_record,
)

KITE_INTERVAL_NAMES = {
    BarInterval.ONE_MINUTE: "minute",
    BarInterval.THREE_MINUTES: "3minute",
    BarInterval.FIVE_MINUTES: "5minute",
    BarInterval.FIFTEEN_MINUTES: "15minute",
    BarInterval.THIRTY_MINUTES: "30minute",
    BarInterval.ONE_HOUR: "60minute",
    BarInterval.ONE_DAY: "day",
}
"""Kite's own vocabulary. Note `"minute"`, not `"1minute"` — the one place its naming
breaks its own pattern, and a plausible-looking `"1minute"` is rejected by the API."""


class KiteHistoricalBarSource:
    """Bars from Kite Connect, normalised to `BarRecord`."""

    def __init__(self, kite_client: Any) -> None:
        self._kite = kite_client

    @property
    def broker(self) -> BrokerName:
        return BrokerName.ZERODHA_KITE

    def supports(self, interval: BarInterval) -> bool:
        return interval in KITE_INTERVAL_NAMES

    def fetch_bars(self, request: BarRequest) -> list[BarRecord]:
        if not self.supports(request.interval):
            raise UnsupportedIntervalError(f"kite cannot express {request.interval.value}")
        token = request.instrument.identifier_for(BrokerName.ZERODHA_KITE)
        try:
            rows = self._kite.historical_data(
                int(token),
                request.start_date,
                request.end_date,
                KITE_INTERVAL_NAMES[request.interval],
            )
        except Exception as failure:
            message = str(failure).lower()
            if "token" in message or "api_key" in message or "access" in message:
                raise BrokerAuthenticationError(
                    f"kite rejected credentials: {failure}"
                ) from failure
            raise HistoricalBarSourceError(
                f"kite historical_data failed: {failure}"
            ) from failure

        return [
            to_bar_record(
                instrument=request.instrument,
                interval=request.interval,
                broker=BrokerName.ZERODHA_KITE,
                bar_timestamp=row["date"],
                open_price=row["open"],
                high_price=row["high"],
                low_price=row["low"],
                close_price=row["close"],
                volume=row.get("volume", 0),
            )
            for row in rows
        ]
