"""One adapter per broker, each answering the same question in its own dialect.

Every broker returns a different shape for "what is this instrument worth right now" —
Kite nests depth under `depth.buy[0]`, Angel One returns a flat record with `depth` only in
its FULL mode, and each names its instrument differently. This module flattens all of that
into `BrokerQuoteObservation` so the consolidation engine never learns a broker's dialect.

**A failure is an observation, not an exception.** A poller that raises stops the capture; a
poller that returns a row carrying its failure keeps the tape complete and lets the engine
learn which source is unreliable — which is precisely what a liquidity- and reliability-
weighted consolidation needs. The only thing never done here is inventing a price.

**Rate limits are the adapters' own business.** Angel One's market-data endpoint accepts one
request per second and 50 instruments per request; Kite's quote endpoint takes 500 symbols
in one call. Each poller therefore declares its own batch size, and the recorder asks rather
than assuming they are alike.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import (
    BrokerQuoteObservation,
    now_utc,
    rupees_to_paise,
)

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
"""Angel One stamps `exchFeedTime` in IST with no zone attached. Parsing it as UTC would
shift every exchange timestamp by 5h30m and make the broker look catastrophically stale."""


@dataclass(frozen=True, slots=True)
class PolledInstrument:
    """One instrument, addressed the way each broker needs it addressed."""

    trading_symbol: str
    exchange: str
    kite_symbol: str
    angel_symbol_token: str | None = None
    angel_trading_symbol: str | None = None
    upstox_instrument_key: str | None = None


class BrokerQuotePoller(Protocol):
    """What the recorder needs from any broker."""

    @property
    def broker(self) -> str: ...

    @property
    def instruments_per_request(self) -> int: ...

    def poll(
        self, instruments: Sequence[PolledInstrument]
    ) -> list[BrokerQuoteObservation]: ...


def _observation_with_failure(
    broker: str,
    instrument: PolledInstrument,
    requested_at: datetime,
    reason: str,
) -> BrokerQuoteObservation:
    return BrokerQuoteObservation(
        broker=broker,
        trading_symbol=instrument.trading_symbol,
        exchange=instrument.exchange,
        requested_at=requested_at,
        received_at=now_utc(),
        failure=reason,
    )


class KiteQuotePoller:
    """Zerodha Kite's `quote()` — full depth, 500 instruments per call."""

    broker = "kite"
    instruments_per_request = 200

    def __init__(self, kite_client: Any) -> None:
        self._kite = kite_client

    def poll(
        self, instruments: Sequence[PolledInstrument]
    ) -> list[BrokerQuoteObservation]:
        requested_at = now_utc()
        keys = [instrument.kite_symbol for instrument in instruments]
        try:
            quotes = self._kite.quote(keys)
        except Exception as failure:  # noqa: BLE001 - a broker outage is data, not a crash
            return [
                _observation_with_failure(
                    self.broker, instrument, requested_at, f"{type(failure).__name__}: {failure}"
                )
                for instrument in instruments
            ]
        received_at = now_utc()
        observations = []
        for instrument in instruments:
            quote = quotes.get(instrument.kite_symbol)
            if quote is None:
                observations.append(
                    _observation_with_failure(
                        self.broker, instrument, requested_at, "instrument absent from response"
                    )
                )
                continue
            observations.append(
                self._observation_from_quote(instrument, quote, requested_at, received_at)
            )
        return observations

    def _observation_from_quote(
        self,
        instrument: PolledInstrument,
        quote: dict[str, Any],
        requested_at: datetime,
        received_at: datetime,
    ) -> BrokerQuoteObservation:
        depth = quote.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        best_bid = bids[0] if bids else {}
        best_ask = asks[0] if asks else {}
        return BrokerQuoteObservation(
            broker=self.broker,
            trading_symbol=instrument.trading_symbol,
            exchange=instrument.exchange,
            requested_at=requested_at,
            received_at=received_at,
            exchange_time=_as_utc(quote.get("timestamp")),
            last_price_paise=rupees_to_paise(quote.get("last_price")),
            best_bid_paise=rupees_to_paise(best_bid.get("price")),
            best_ask_paise=rupees_to_paise(best_ask.get("price")),
            best_bid_quantity=_as_int(best_bid.get("quantity")),
            best_ask_quantity=_as_int(best_ask.get("quantity")),
            total_buy_quantity=_as_int(quote.get("buy_quantity")),
            total_sell_quantity=_as_int(quote.get("sell_quantity")),
            volume_traded=_as_int(quote.get("volume")),
        )


class AngelOneQuotePoller:
    """Angel One SmartAPI `getMarketData` in FULL mode — 50 instruments per request.

    The FULL mode is used rather than LTP deliberately: an LTP-only cross-check can compare
    last traded prices, which on an illiquid name may be minutes old on one broker and
    seconds old on another, and the engine would read stale data as disagreement. FULL
    returns the touch, which both brokers can be held to at the same instant.
    """

    broker = "angel_one"
    instruments_per_request = 50

    def __init__(self, smart_api_client: Any) -> None:
        self._client = smart_api_client

    def poll(
        self, instruments: Sequence[PolledInstrument]
    ) -> list[BrokerQuoteObservation]:
        requested_at = now_utc()
        addressable = [
            instrument for instrument in instruments if instrument.angel_symbol_token
        ]
        unaddressable = [
            _observation_with_failure(
                self.broker, instrument, requested_at, "no Angel One symbol token"
            )
            for instrument in instruments
            if not instrument.angel_symbol_token
        ]
        if not addressable:
            return unaddressable
        by_exchange: dict[str, list[str]] = {}
        for instrument in addressable:
            token = instrument.angel_symbol_token
            if token is not None:
                by_exchange.setdefault(instrument.exchange, []).append(token)
        try:
            response = self._client.getMarketData("FULL", by_exchange)
        except Exception as failure:  # noqa: BLE001 - a broker outage is data, not a crash
            return unaddressable + [
                _observation_with_failure(
                    self.broker, instrument, requested_at, f"{type(failure).__name__}: {failure}"
                )
                for instrument in addressable
            ]
        received_at = now_utc()
        fetched = {
            str(row.get("symbolToken")): row
            for row in ((response.get("data") or {}).get("fetched") or [])
        }
        observations = list(unaddressable)
        for instrument in addressable:
            row = fetched.get(str(instrument.angel_symbol_token))
            if row is None:
                observations.append(
                    _observation_with_failure(
                        self.broker,
                        instrument,
                        requested_at,
                        f"not in fetched set (status {response.get('status')})",
                    )
                )
                continue
            observations.append(
                self._observation_from_row(instrument, row, requested_at, received_at)
            )
        return observations

    def _observation_from_row(
        self,
        instrument: PolledInstrument,
        row: dict[str, Any],
        requested_at: datetime,
        received_at: datetime,
    ) -> BrokerQuoteObservation:
        depth = row.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        best_bid = bids[0] if bids else {}
        best_ask = asks[0] if asks else {}
        return BrokerQuoteObservation(
            broker=self.broker,
            trading_symbol=instrument.trading_symbol,
            exchange=instrument.exchange,
            requested_at=requested_at,
            received_at=received_at,
            exchange_time=_as_utc(row.get("exchFeedTime")),
            last_price_paise=rupees_to_paise(row.get("ltp")),
            best_bid_paise=rupees_to_paise(best_bid.get("price")),
            best_ask_paise=rupees_to_paise(best_ask.get("price")),
            best_bid_quantity=_as_int(best_bid.get("quantity")),
            best_ask_quantity=_as_int(best_ask.get("quantity")),
            total_buy_quantity=_as_int(row.get("totBuyQuan")),
            total_sell_quantity=_as_int(row.get("totSellQuan")),
            volume_traded=_as_int(row.get("tradeVolume")),
        )


class UpstoxQuotePoller:
    """Upstox `market-quote/quotes` — full depth, addressed by `NSE_EQ|<ISIN>`.

    The THIRD feed, and the one that changes what the engine can know: with two brokers the
    per-source noise variances are unidentifiable (`var(a-b)` is one number shared by both),
    and the three-cornered hat needs three. Upstox was recorded as blocked on an expired
    token until 2026-08-12, when the stored ANALYTICS token turned out to be live — the
    trading token had expired and the two were being confused.

    Prices arrive as rupee floats and depth is a flat `buy`/`sell` list, like Kite's; the
    timestamp is epoch MILLISECONDS, unlike either of the others.
    """

    broker = "upstox"
    instruments_per_request = 100
    """Upstox documents 500 instrument keys per request; 100 keeps the URL well inside
    server limits, since the keys go in the query string rather than a body."""

    def __init__(self, access_token: str, session: Any) -> None:
        self._access_token = access_token
        self._session = session

    def poll(
        self, instruments: Sequence[PolledInstrument]
    ) -> list[BrokerQuoteObservation]:
        requested_at = now_utc()
        addressable = [
            instrument for instrument in instruments if instrument.upstox_instrument_key
        ]
        observations = [
            _observation_with_failure(
                self.broker, instrument, requested_at, "no Upstox instrument key"
            )
            for instrument in instruments
            if not instrument.upstox_instrument_key
        ]
        if not addressable:
            return observations
        try:
            response = self._session.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                params={
                    "instrument_key": ",".join(
                        str(instrument.upstox_instrument_key) for instrument in addressable
                    )
                },
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json().get("data") or {}
        except Exception as failure:  # noqa: BLE001 - a broker outage is data, not a crash
            return observations + [
                _observation_with_failure(
                    self.broker, instrument, requested_at, f"{type(failure).__name__}: {failure}"
                )
                for instrument in addressable
            ]
        received_at = now_utc()
        # Upstox keys its RESPONSE by `EXCHANGE:SYMBOL` while the REQUEST is by
        # `EXCHANGE_SEGMENT|ISIN`, so the reply is matched back through the echoed
        # `instrument_token` rather than by reconstructing a key from the symbol.
        by_key = {
            str(row.get("instrument_token")): row for row in payload.values() if row
        }
        for instrument in addressable:
            row = by_key.get(str(instrument.upstox_instrument_key))
            if row is None:
                observations.append(
                    _observation_with_failure(
                        self.broker, instrument, requested_at, "instrument absent from response"
                    )
                )
                continue
            observations.append(
                self._observation_from_row(instrument, row, requested_at, received_at)
            )
        return observations

    def _observation_from_row(
        self,
        instrument: PolledInstrument,
        row: dict[str, Any],
        requested_at: datetime,
        received_at: datetime,
    ) -> BrokerQuoteObservation:
        depth = row.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        best_bid = bids[0] if bids else {}
        best_ask = asks[0] if asks else {}
        return BrokerQuoteObservation(
            broker=self.broker,
            trading_symbol=instrument.trading_symbol,
            exchange=instrument.exchange,
            requested_at=requested_at,
            received_at=received_at,
            exchange_time=_epoch_milliseconds_to_utc(row.get("last_trade_time")),
            last_price_paise=rupees_to_paise(row.get("last_price")),
            best_bid_paise=rupees_to_paise(best_bid.get("price")),
            best_ask_paise=rupees_to_paise(best_ask.get("price")),
            best_bid_quantity=_as_int(best_bid.get("quantity")),
            best_ask_quantity=_as_int(best_ask.get("quantity")),
            total_buy_quantity=_as_int(row.get("total_buy_quantity")),
            total_sell_quantity=_as_int(row.get("total_sell_quantity")),
            volume_traded=_as_int(row.get("volume")),
        )


def _epoch_milliseconds_to_utc(value: Any) -> datetime | None:
    """Upstox stamps in epoch MILLISECONDS — a third convention, hence a third converter.

    Zero and the SDK's other empty values mean absent, exactly as epoch 0 does on the Kite
    depth tape; kept as `None` so a missing stamp never becomes 1970.
    """
    if value in (None, "", 0):
        return None
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    if seconds < ABSENT_STAMP_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


ABSENT_STAMP_EPOCH_SECONDS = 1_600_000_000.0
"""Any stamp older than 2020-09 is a zero value dressed as a timestamp, not a quote."""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_utc(value: Any) -> datetime | None:
    """Broker exchange stamps, all of them naive IST, converted once and here.

    **Measured on the live feed, and it is the reason this function exists.** Kite's REST
    `quote()` returns `datetime(2026, 8, 12, 14, 39, 32)` with `tzinfo=None` while the host
    clock reads 09:09:32 UTC — the value is IST wall-clock with its zone discarded. Angel One
    does the same with `exchFeedTime`, as a string. Reading either as UTC (which
    `datetime.astimezone()` does on a UTC host) shifts it by 5h30m, and the first capture run
    duly stored Kite at 14:31 and Angel One at 09:01 for the same instant.

    Note this is the OPPOSITE convention to `epoch_seconds_from_sdk_timestamp`, which handles
    the WEBSOCKET tick: there `kiteconnect` builds the datetime with `fromtimestamp()`, so the
    naive value is in the host's zone. Same SDK, two paths, two meanings — which is exactly
    why each conversion lives next to the measurement that established it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return value.replace(tzinfo=INDIA_MARKET_TIMEZONE).astimezone(UTC)
    if isinstance(value, str):
        for layout in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(value, layout)  # noqa: DTZ007 - zone applied below
            except ValueError:
                continue
            return (
                parsed.astimezone(UTC)
                if parsed.tzinfo
                else parsed.replace(tzinfo=INDIA_MARKET_TIMEZONE).astimezone(UTC)
            )
    return None
