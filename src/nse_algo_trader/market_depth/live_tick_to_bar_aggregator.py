"""`L4.27` — folds a live tick stream into closed OHLCV bars. Spec `docs/research/266`.

**The link that was missing.** The whole chain from a bar to a decision already exists and is
real-data verified: `InstrumentSignalState.observe(AvailableBar)` produces classifier opinions,
`SoftRegimeWeightingBrain.combine` turns those into a belief, and `IntradayMeanReversionEngine`
decides on it. The live loop had ticks and no bars, so it passed a maximum-entropy belief, the cash
bot's regime veto abstained, and `/loop` recorded **0 proposals on fifteen consecutive live
iterations** (`B40`). That was correct behaviour on a clock mismatch — a bar-fitted classifier fed a
tape-derived observation is the `A.106` defect — and this is the link that removes the mismatch.

**O(1) per tick, no history retained.** `L4.27`'s stated requirement, and a hard constraint rather
than an aspiration: the live universe is 1,845 instruments, and anything that recomputed a bar from
stored ticks would re-read the tape once per instrument per tick. The carried state per instrument
is five numbers and a bucket timestamp — never a list of ticks.

**A bar is emitted only once it is CLOSED**, and its `availability_time` is the bucket start plus
the interval — the same convention `L0.37` writes into the bar store, and the same one
`tests/test_bar_reads_are_point_in_time.py` guards. A bar covering the current instant is not
knowable yet, and handing a partial one to a classifier is the look-ahead that makes every backtest
look good.

**No gap filling, deliberately.** An instrument that did not trade in a bucket emits no bar for it.
A fabricated flat bar would hand the volatility classifier a zero-range observation the market never
produced, and it would do so most often on exactly the illiquid names where that classifier is
already weakest — manufacturing false confidence precisely where there is least.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

PAISE_PER_RUPEE = Decimal("100")

FIVE_MINUTE_BAR = timedelta(minutes=5)
"""The interval the regime panel and `IntradayMeanReversionEngine` are both fitted on.

Not a default chosen here: `REVERSION_WINDOW_IN_FIVE_MINUTE_BARS` counts bars of exactly this
length, and `price_bars` stores them under `bar_interval = '5m'`. Changing it without refitting both
is the `A.106` defect again.
"""


class TickAggregationError(Exception):
    """A tick cannot be folded into a bar, and guessing would fabricate a price."""


@dataclass(frozen=True, slots=True)
class CompletedBar:
    """One CLOSED bar. Never handed out while still open.

    Carries `instrument_token` because the aggregator serves the whole universe at once and a bar
    that cannot say which instrument it belongs to is a bar that gets attributed by position in a
    list — which is how a RELIANCE bar ends up teaching an ASHOKLEY classifier.
    """

    instrument_token: int
    bar_timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    bar_interval: timedelta

    def __post_init__(self) -> None:
        if self.high_price < self.low_price:
            raise TickAggregationError(
                f"{self.instrument_token} produced a bar with high {self.high_price} below low "
                f"{self.low_price}; the two are maintained from the same tick stream, so this is a "
                f"defect in the aggregator rather than a strange market"
            )
        if not all(
            math.isfinite(value)
            for value in (self.open_price, self.high_price, self.low_price, self.close_price)
        ):
            raise TickAggregationError(
                f"{self.instrument_token} produced a non-finite bar; it would propagate into every "
                f"classifier that consumed it and silently make each comparison False"
            )

    @property
    def availability_time(self) -> datetime:
        """When this bar became KNOWABLE — its own close, never the instant it was stamped.

        `L0.37`'s convention, and the one the point-in-time guard enforces on every reader of
        `price_bars`. A consumer that used `bar_timestamp` instead would be acting on the bar
        covering its own decision instant.
        """
        return self.bar_timestamp + self.bar_interval

    @property
    def range_fraction(self) -> float:
        """High-low as a fraction of the open — scale-free, for a volatility read."""
        if self.open_price <= 0:
            return 0.0
        return (self.high_price - self.low_price) / self.open_price


@dataclass(slots=True)
class _OpenBar:
    """The five running numbers. This is the entire per-instrument state (`L4.27`)."""

    bucket: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int

    def update(self, price: float, volume: int) -> None:
        self.close_price = price
        if price > self.high_price:
            self.high_price = price
        if price < self.low_price:
            self.low_price = price
        # Volume arrives CUMULATIVE per session on the NSE tape, so the bar takes the maximum seen
        # rather than a sum: adding cumulative readings would multiply the day's volume by the tick
        # count. Taking the max keeps the bar's volume the session total AT its close, and the
        # caller differences consecutive bars if it wants per-bar volume.
        if volume > self.volume:
            self.volume = volume

    def sealed(self, instrument_token: int, bar_interval: timedelta) -> CompletedBar:
        return CompletedBar(
            instrument_token=instrument_token,
            bar_timestamp=self.bucket,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume=self.volume,
            bar_interval=bar_interval,
        )


def floor_to_bucket(instant: datetime, interval: timedelta) -> datetime:
    """The start of the bar this instant falls in.

    Floors against the instant's own DAY rather than against the epoch: a five-minute grid anchored
    on the epoch happens to align with 09:15, but a seven-minute one would not, and a bar boundary
    that drifts relative to the session open is a bar the strategy was never fitted on.
    """
    if instant.tzinfo is None:
        raise TickAggregationError(
            f"a naive {instant} cannot be bucketed; a naive instant is read as UTC and moves every "
            f"bar boundary in this project by five and a half hours"
        )
    seconds = int(interval.total_seconds())
    if seconds <= 0:
        raise TickAggregationError("a bar interval must be positive to have a boundary at all")
    midnight = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((instant - midnight).total_seconds())
    return midnight + timedelta(seconds=(elapsed // seconds) * seconds)


class LiveTickToBarAggregator:
    """Turns a live tick stream into closed bars, in constant memory per instrument.

    **SOTA analog:** the bar-builder stage of a streaming feed handler — NautilusTrader's
    `BarAggregator`, or the `resample` step every backtest library performs in batch. The difference
    that matters is the one `L4.27` names: this runs ON the decision path across 1,845 instruments,
    so it holds five numbers per instrument rather than a frame of ticks.
    """

    def __init__(self, *, bar_interval: timedelta = FIVE_MINUTE_BAR) -> None:
        if bar_interval <= timedelta(0):
            raise TickAggregationError(
                f"a bar interval of {bar_interval} has no boundary, so no bar could ever close"
            )
        self._interval = bar_interval
        self._open: dict[int, _OpenBar] = {}
        self._completed: list[CompletedBar] = []
        self._ticks_seen = 0
        self._ticks_without_an_exchange_time = 0

    @property
    def bar_interval(self) -> timedelta:
        return self._interval

    @property
    def instruments_tracked(self) -> int:
        return len(self._open)

    @property
    def ticks_seen(self) -> int:
        return self._ticks_seen

    @property
    def ticks_without_an_exchange_time(self) -> int:
        """How many real ticks were DROPPED for having no exchange timestamp.

        Counted rather than silently skipped, because it is a property of the real tape and not a
        hypothetical: measured on 2026-08-18, **3,644 of 8,495,786 ticks (0.043%)** carry a price
        and no `exchange_time`, spread across **1,752 of 1,845 instruments** — a systematic sprinkle
        rather than one broken feed.

        They are dropped rather than stamped with `receipt_time`. Receipt time is the CAPTURE's
        clock and exchange time is the EXCHANGE's; mixing the two inside one bar is precisely the
        clock-mixing this whole slice exists to remove, and it would do it invisibly.
        """
        return self._ticks_without_an_exchange_time

    def open_bar_for(self, instrument_token: int) -> CompletedBar | None:
        """The IN-PROGRESS bar, clearly named so a consumer has to ask for it.

        Returned as a `CompletedBar` shape for convenience and NOT emitted anywhere automatically —
        anything that treats this as knowable is looking ahead, and making it awkward to reach is
        the point.
        """
        state = self._open.get(instrument_token)
        return None if state is None else state.sealed(instrument_token, self._interval)

    def observe(
        self,
        *,
        instrument_token: int,
        exchange_time: datetime | None,
        last_price_paise: Decimal,
        volume: int = 0,
    ) -> CompletedBar | None:
        """Fold one tick in. Returns the bar this tick CLOSED, or `None`.

        Returning the closed bar directly means a caller learns about completion at the moment it
        happens rather than by polling — and the same bar is also queued for `drain_completed`, so a
        batch consumer and a streaming one can coexist without either missing a bar.

        A tick that arrives OUT OF ORDER, into a bucket already sealed, is dropped rather than
        applied: the tape is written in receipt order and a late tick would reopen a bar a
        classifier has already learned from, silently changing history.
        """
        if exchange_time is None:
            # Real, and measured at 0.043% of the live tape. See `ticks_without_an_exchange_time`.
            self._ticks_without_an_exchange_time += 1
            return None
        if last_price_paise <= 0:
            return None
        price = float(last_price_paise / PAISE_PER_RUPEE)
        if not math.isfinite(price):
            return None
        bucket = floor_to_bucket(exchange_time, self._interval)
        self._ticks_seen += 1

        state = self._open.get(instrument_token)
        if state is None:
            self._open[instrument_token] = _OpenBar(
                bucket=bucket,
                open_price=price,
                high_price=price,
                low_price=price,
                close_price=price,
                volume=max(volume, 0),
            )
            return None

        if bucket == state.bucket:
            state.update(price, max(volume, 0))
            return None
        if bucket < state.bucket:
            # Late tick into a sealed bucket. Dropped — see the docstring.
            return None

        sealed = state.sealed(instrument_token, self._interval)
        self._completed.append(sealed)
        self._open[instrument_token] = _OpenBar(
            bucket=bucket,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=max(volume, 0),
        )
        return sealed

    def drain_completed(self) -> tuple[CompletedBar, ...]:
        """Every bar closed since the last drain, then forget them.

        Draining rather than accumulating is what keeps this O(1): a queue nobody empties is a list
        of every bar of the session, which is exactly the history `L4.27` forbids retaining.
        """
        drained = tuple(self._completed)
        self._completed.clear()
        return drained

    def force_close(self, at: datetime) -> tuple[CompletedBar, ...]:
        """Seal every open bar. For the SESSION CLOSE and nothing else.

        `R.01` flattens at 15:30, and without this the final partial bar of the day is never
        emitted — so the strategy would never see the last minutes of the session it just traded,
        and the record of the day would end before the day did.

        Bars whose bucket has not yet elapsed at `at` are still sealed, because the session ending
        IS the thing that makes them final. That is the one case where a short bar is honest.
        """
        del at
        sealed = tuple(
            state.sealed(token, self._interval) for token, state in sorted(self._open.items())
        )
        self._open.clear()
        self._completed.clear()
        return sealed
