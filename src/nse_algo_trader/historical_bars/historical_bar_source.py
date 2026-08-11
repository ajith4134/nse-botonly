"""`L0.14` — one interface over brokers that agree on nothing.

Five brokers, five incompatible ways to ask the same question. Measured, not assumed —
these are the real signatures as installed:

    kite    historical_data(instrument_token, from_date, to_date, interval, ...)
    breeze  get_historical_data_v2(interval, from_date, to_date, stock_code, ...)
    upstox  get_historical_candle_data1(instrument_key, interval, to_date, from_date, ...)
    angel   getCandleData(historicDataParams)          # one dict, its own vocabulary
    groww   get_historical_candle_data(trading_symbol, exchange, segment, start_time, ...)

They disagree on the instrument identifier (integer token / stock code / instrument key /
symbol token / trading symbol), on the interval vocabulary (`"day"` vs `"ONE_DAY"` vs
`interval_in_minutes=1440`), on date formats, and on response shape. A caller that spoke
any one of them directly would be welded to that broker, which is the failure `L0.15`
cross-source failover cannot be built on top of.

**They also disagree on the DATA**, which is the real reason this matters. Fetching
RELIANCE daily bars for 2026-08-07..11: Kite returned 3 bars, Angel One returned 2 — Angel
had no 2026-08-07. Where both had a bar the closes agreed exactly (1320.6 on 08-11). So
brokers are interchangeable in what they mean and NOT in what they hold, and only a common
interface makes that difference measurable rather than invisible.

**Prices stay `Decimal`.** A broker hands back `1334.8` as a float; the project's
`BarRecord` stores `Decimal` and the depth tape stores integer paise, because binary
floating point cannot represent a rupee price exactly and an accumulated tick error is
indistinguishable from a real one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from nse_algo_trader.bitemporal_bar_store import BarRecord
from nse_algo_trader.broker_credentials import BrokerName


class HistoricalBarSourceError(RuntimeError):
    """A source could not answer. Never a silently empty list."""


class BrokerAuthenticationError(HistoricalBarSourceError):
    """The broker refused the credentials. Distinct from 'no data for that window'."""


class UnsupportedIntervalError(HistoricalBarSourceError):
    """This broker cannot express the requested interval."""


class BarInterval(Enum):
    """Intervals in the project's own vocabulary, translated per broker.

    Deliberately not a raw string: `"day"` is Kite's word, `"ONE_DAY"` is Angel's, and a
    caller passing either directly is a caller that has picked a broker.
    """

    ONE_MINUTE = "1minute"
    THREE_MINUTES = "3minute"
    FIVE_MINUTES = "5minute"
    FIFTEEN_MINUTES = "15minute"
    THIRTY_MINUTES = "30minute"
    ONE_HOUR = "60minute"
    ONE_DAY = "day"

    @property
    def minutes(self) -> int:
        """Length in minutes. A trading day is 375 minutes (09:15-15:30), an exchange
        fact — but for a DAILY bar the broker vocabularies mean 'one session', which some
        express as 1440. The mapping lives in each adapter, not here."""
        return {
            BarInterval.ONE_MINUTE: 1,
            BarInterval.THREE_MINUTES: 3,
            BarInterval.FIVE_MINUTES: 5,
            BarInterval.FIFTEEN_MINUTES: 15,
            BarInterval.THIRTY_MINUTES: 30,
            BarInterval.ONE_HOUR: 60,
            BarInterval.ONE_DAY: 1440,
        }[self]


@dataclass(frozen=True)
class BarInstrument:
    """One instrument, carrying every broker's name for it.

    Carried together rather than resolved per call because the identifiers are genuinely
    unrelated — Kite's 738561, Angel's "2885" and Breeze's "RELIND" are not derivable
    from one another. Resolving them is `L0.17`'s job; holding them is this one's.
    """

    exchange: str
    segment: str
    tradingsymbol: str
    broker_identifiers: dict[BrokerName, str]

    def identifier_for(self, broker: BrokerName) -> str:
        try:
            return self.broker_identifiers[broker]
        except KeyError as missing:
            raise HistoricalBarSourceError(
                f"no {broker.value} identifier for {self.tradingsymbol}; "
                "symbology resolution is L0.17"
            ) from missing


@dataclass(frozen=True)
class BarRequest:
    """What to fetch. Dates inclusive, in exchange local terms."""

    instrument: BarInstrument
    interval: BarInterval
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise HistoricalBarSourceError(
                f"end_date {self.end_date} precedes start_date {self.start_date}"
            )


@runtime_checkable
class HistoricalBarSource(Protocol):
    """What every broker adapter must be, so callers never name one."""

    @property
    def broker(self) -> BrokerName: ...

    def supports(self, interval: BarInterval) -> bool:
        """Whether this broker can express the interval at all."""
        ...

    def fetch_bars(self, request: BarRequest) -> list[BarRecord]: ...


def to_bar_record(
    *,
    instrument: BarInstrument,
    interval: BarInterval,
    broker: BrokerName,
    bar_timestamp: datetime,
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    volume: object,
    open_interest: object = None,
) -> BarRecord:
    """Normalise one broker's row into the project's bar type.

    `available_from` is the bar's own close, not the fetch time: a 09:15 one-minute bar
    became actionable at 09:16, whenever we happened to download it. Using fetch time
    would make every historical bar look unavailable during replay, and using the bar's
    START would make it readable before it had finished forming — a one-bar lookahead
    that is invisible in every summary statistic.
    """
    return BarRecord(
        exchange=instrument.exchange,
        segment=instrument.segment,
        tradingsymbol=instrument.tradingsymbol,
        instrument_token=_token_for(instrument, broker),
        bar_interval=interval.value,
        bar_timestamp=bar_timestamp,
        available_from=bar_timestamp + _interval_duration(interval),
        open_price=_price(open_price),
        high_price=_price(high_price),
        low_price=_price(low_price),
        close_price=_price(close_price),
        volume=_whole_number(volume) or 0,
        open_interest=_whole_number(open_interest),
    )


def _whole_number(value: object) -> int | None:
    """A broker's count as an int, or None when it genuinely did not report one.

    `None` and `0` are different claims: no open interest reported is not the same as
    open interest of zero, and collapsing them would invent a fact about every cash bar.
    """
    if value is None or value == "":
        return None
    return int(float(value))  # type: ignore[arg-type]  # broker values are str|int|float


def _token_for(instrument: BarInstrument, broker: BrokerName) -> int:
    """The store keys on an integer token; only some brokers have one.

    Non-numeric identifiers become 0 rather than a hash: a fabricated token would collide
    silently with a real one, and the tradingsymbol already identifies the row.
    """
    identifier = instrument.broker_identifiers.get(broker, "")
    return int(identifier) if identifier.isdigit() else 0


def _interval_duration(interval: BarInterval) -> timedelta:
    return timedelta(minutes=interval.minutes)


def _price(value: object) -> Decimal:
    """Broker floats to `Decimal`, via `str`.

    `Decimal(1334.8)` captures the binary error exactly; `Decimal(str(1334.8))` is 1334.8.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def first_source_supporting(
    sources: Sequence[HistoricalBarSource], interval: BarInterval
) -> HistoricalBarSource:
    """The first source that can express this interval.

    Raises rather than returning None: 'no broker can serve this' is a condition a caller
    must handle, and an empty result would be indistinguishable from an empty window.
    """
    for source in sources:
        if source.supports(interval):
            return source
    raise UnsupportedIntervalError(
        f"no configured source supports {interval.value}; "
        f"tried {[source.broker.value for source in sources]}"
    )
