"""`L0.14` — Angel One (SmartAPI) as a historical bar source.

Angel differs from Kite in every dimension the interface exists to hide, which makes it
the adapter that proves the interface is real rather than Kite-shaped:

- one **dict** argument, not positional parameters;
- its own interval words (`ONE_DAY`, `FIVE_MINUTE` — singular, no trailing S);
- `"YYYY-MM-DD HH:MM"` strings rather than dates;
- its own instrument tokens (RELIANCE is `"2885"`, where Kite says `738561`);
- rows as positional **lists** `[timestamp, o, h, l, c, v]`, not dicts;
- failure by `{"status": false}` in a 200 response rather than by raising.

That last one matters most. A caller that only catches exceptions treats an Angel refusal
as an empty result, which is how a broken feed becomes a quiet gap in history rather than
an error. Measured against real data: fetching RELIANCE 2026-08-07..11, Angel returned 2
bars where Kite returned 3 — it had no 08-07 — while the closes agreed exactly where both
had a bar. Interchangeable in meaning, not in coverage.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

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

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
"""Angel wants an explicit intraday window rather than bare dates."""

ANGEL_INTERVAL_NAMES = {
    BarInterval.ONE_MINUTE: "ONE_MINUTE",
    BarInterval.THREE_MINUTES: "THREE_MINUTE",
    BarInterval.FIVE_MINUTES: "FIVE_MINUTE",
    BarInterval.FIFTEEN_MINUTES: "FIFTEEN_MINUTE",
    BarInterval.THIRTY_MINUTES: "THIRTY_MINUTE",
    BarInterval.ONE_HOUR: "ONE_HOUR",
    BarInterval.ONE_DAY: "ONE_DAY",
}
"""Singular MINUTE throughout — `FIVE_MINUTES` is rejected."""

TIMESTAMP_FIELD, OPEN_FIELD, HIGH_FIELD, LOW_FIELD, CLOSE_FIELD, VOLUME_FIELD = range(6)
"""Angel rows are positional lists. Named here so the parsing reads as data access rather
than as magic indices."""


class AngelOneHistoricalBarSource:
    """Bars from Angel One SmartAPI, normalised to `BarRecord`."""

    def __init__(self, smart_connect_client: Any) -> None:
        self._client = smart_connect_client

    @property
    def broker(self) -> BrokerName:
        return BrokerName.ANGEL_ONE

    def supports(self, interval: BarInterval) -> bool:
        return interval in ANGEL_INTERVAL_NAMES

    def fetch_bars(self, request: BarRequest) -> list[BarRecord]:
        if not self.supports(request.interval):
            raise UnsupportedIntervalError(f"angel one cannot express {request.interval.value}")
        parameters = {
            "exchange": request.instrument.exchange,
            "symboltoken": request.instrument.identifier_for(BrokerName.ANGEL_ONE),
            "interval": ANGEL_INTERVAL_NAMES[request.interval],
            "fromdate": f"{request.start_date} {SESSION_OPEN:%H:%M}",
            "todate": f"{request.end_date} {SESSION_CLOSE:%H:%M}",
        }
        try:
            response = self._client.getCandleData(parameters)
        except Exception as failure:
            raise HistoricalBarSourceError(
                f"angel one getCandleData failed: {failure}"
            ) from failure

        # Angel signals refusal INSIDE a successful response. Treating a falsy status as
        # "no data" would turn an auth failure into a silent hole in history.
        if not isinstance(response, dict):
            raise HistoricalBarSourceError(f"angel one returned {type(response).__name__}")
        if not response.get("status", False):
            message = str(response.get("message", "")) or "no message"
            if "token" in message.lower() or "session" in message.lower():
                raise BrokerAuthenticationError(f"angel one rejected session: {message}")
            raise HistoricalBarSourceError(f"angel one refused: {message}")

        return [
            to_bar_record(
                instrument=request.instrument,
                interval=request.interval,
                broker=BrokerName.ANGEL_ONE,
                bar_timestamp=_parse_angel_timestamp(row[TIMESTAMP_FIELD]),
                open_price=row[OPEN_FIELD],
                high_price=row[HIGH_FIELD],
                low_price=row[LOW_FIELD],
                close_price=row[CLOSE_FIELD],
                volume=row[VOLUME_FIELD],
            )
            for row in (response.get("data") or [])
        ]


def _parse_angel_timestamp(raw: str) -> datetime:
    """Angel stamps `2026-08-11T09:15:00+05:30`, already offset-aware.

    Parsed rather than assumed: a naive datetime here would compare wrongly against the
    project's timezone-aware bars and silently reorder a session.
    """
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
