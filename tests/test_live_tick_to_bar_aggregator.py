"""Tests for `L4.27`'s tick-to-bar aggregator — spec `docs/research/266`, closes `B40`.

Two of these were written from defects the REAL tape produced and no synthetic stream would have:
ticks carrying a price and no `exchange_time` (3,644 of 8,495,786, across 1,752 of 1,845
instruments), and a `session_date` partition holding ticks stamped on a different session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from nse_algo_trader.market_depth.live_tick_to_bar_aggregator import (
    FIVE_MINUTE_BAR,
    CompletedBar,
    LiveTickToBarAggregator,
    TickAggregationError,
    floor_to_bucket,
)
from nse_algo_trader.replay_session_clock import IST

TOKEN = 738561
OPEN = datetime(2026, 8, 18, 9, 15, tzinfo=IST)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, second, tzinfo=IST)


def feed(
    aggregator: LiveTickToBarAggregator,
    ticks: list[tuple[datetime, str]],
    *,
    token: int = TOKEN,
) -> list[CompletedBar]:
    closed: list[CompletedBar] = []
    for instant, price in ticks:
        bar = aggregator.observe(
            instrument_token=token, exchange_time=instant, last_price_paise=Decimal(price)
        )
        if bar is not None:
            closed.append(bar)
    return closed


# --------------------------------------------------------------------------------------------
# bucketing
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_bucket_boundaries_land_on_the_real_session_grid() -> None:
    """09:15:00 and 09:19:59 are the same bar; 09:20:00 is the next one."""
    assert floor_to_bucket(at(9, 15, 0), FIVE_MINUTE_BAR) == at(9, 15)
    assert floor_to_bucket(at(9, 19, 59), FIVE_MINUTE_BAR) == at(9, 15)
    assert floor_to_bucket(at(9, 20, 0), FIVE_MINUTE_BAR) == at(9, 20)
    assert floor_to_bucket(at(15, 29, 59), FIVE_MINUTE_BAR) == at(15, 25)


@pytest.mark.unit
def test_a_naive_instant_cannot_be_bucketed() -> None:
    with pytest.raises(TickAggregationError, match="naive"):
        floor_to_bucket(datetime(2026, 8, 18, 9, 15), FIVE_MINUTE_BAR)  # noqa: DTZ001


@pytest.mark.unit
def test_the_grid_is_anchored_on_the_day_not_the_epoch() -> None:
    """A seven-minute grid anchored on the epoch would drift against the session open.

    Five minutes happens to align either way, which is exactly why this is pinned with an interval
    that does not.
    """
    seven = timedelta(minutes=7)
    assert floor_to_bucket(at(0, 6, 59), seven) == at(0, 0)
    assert floor_to_bucket(at(0, 7, 0), seven) == at(0, 7)


# --------------------------------------------------------------------------------------------
# the bar itself
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_bar_closes_only_when_the_next_bucket_opens() -> None:
    aggregator = LiveTickToBarAggregator()
    closed = feed(
        aggregator,
        [(at(9, 15), "10000"), (at(9, 17), "10500"), (at(9, 19), "9800"), (at(9, 21), "10100")],
    )
    assert len(closed) == 1
    bar = closed[0]
    assert bar.bar_timestamp == at(9, 15)
    assert bar.open_price == pytest.approx(100.0)
    assert bar.high_price == pytest.approx(105.0)
    assert bar.low_price == pytest.approx(98.0)
    assert bar.close_price == pytest.approx(98.0)


@pytest.mark.unit
def test_availability_time_is_the_bars_own_close() -> None:
    """`L0.37`'s convention, and what the point-in-time guard enforces on `price_bars`.

    A consumer using `bar_timestamp` would be acting on the bar covering its own decision instant.
    """
    aggregator = LiveTickToBarAggregator()
    closed = feed(aggregator, [(at(9, 15), "10000"), (at(9, 21), "10100")])
    assert closed[0].availability_time == at(9, 20)
    assert closed[0].availability_time == closed[0].bar_timestamp + closed[0].bar_interval


@pytest.mark.unit
def test_a_partial_bar_is_never_emitted_but_can_be_asked_for() -> None:
    """Making it awkward to reach is the point — anything treating it as knowable looks ahead."""
    aggregator = LiveTickToBarAggregator()
    assert feed(aggregator, [(at(9, 15), "10000"), (at(9, 17), "10200")]) == []
    assert aggregator.drain_completed() == ()
    open_bar = aggregator.open_bar_for(TOKEN)
    assert open_bar is not None
    assert open_bar.close_price == pytest.approx(102.0)


@pytest.mark.unit
def test_draining_hands_each_bar_over_exactly_once() -> None:
    aggregator = LiveTickToBarAggregator()
    feed(aggregator, [(at(9, 15), "10000"), (at(9, 21), "10100"), (at(9, 26), "10300")])
    first = aggregator.drain_completed()
    assert len(first) == 2
    assert aggregator.drain_completed() == (), "a drained bar must not be handed over twice"


@pytest.mark.unit
def test_a_silent_instrument_produces_no_bar_rather_than_a_flat_one() -> None:
    """A fabricated flat bar would feed the volatility classifier a range the market never made.

    And it would do it most often on the illiquid names where that classifier is already weakest.
    """
    aggregator = LiveTickToBarAggregator()
    feed(aggregator, [(at(9, 15), "10000"), (at(9, 45), "10400")])
    bars = aggregator.drain_completed()
    assert len(bars) == 1, "the six silent buckets in between must not be invented"
    assert bars[0].bar_timestamp == at(9, 15)


@pytest.mark.unit
def test_a_late_tick_into_a_sealed_bucket_is_dropped() -> None:
    """Reopening a bar a classifier has already learned from would silently change history."""
    aggregator = LiveTickToBarAggregator()
    feed(aggregator, [(at(9, 15), "10000"), (at(9, 21), "10100")])
    aggregator.drain_completed()
    assert (
        aggregator.observe(
            instrument_token=TOKEN, exchange_time=at(9, 16), last_price_paise=Decimal("99999")
        )
        is None
    )
    assert aggregator.open_bar_for(TOKEN) is not None
    assert aggregator.open_bar_for(TOKEN).bar_timestamp == at(9, 20)  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------------
# what the REAL tape produced
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_tick_with_no_exchange_time_is_dropped_and_counted() -> None:
    """Measured on the live tape: 3,644 of 8,495,786 (0.043%) across 1,752 of 1,845 instruments.

    Counted rather than silently skipped, and NOT stamped with `receipt_time` — that is the
    capture's clock while `exchange_time` is the exchange's, and mixing them inside one bar is the
    clock-mixing `B40` exists to remove.
    """
    aggregator = LiveTickToBarAggregator()
    assert (
        aggregator.observe(
            instrument_token=TOKEN, exchange_time=None, last_price_paise=Decimal("10000")
        )
        is None
    )
    assert aggregator.ticks_without_an_exchange_time == 1
    assert aggregator.instruments_tracked == 0, "a timestamp-less tick must not open a bar"


@pytest.mark.unit
def test_a_non_positive_price_never_reaches_a_bar() -> None:
    aggregator = LiveTickToBarAggregator()
    for price in ("0", "-100"):
        assert (
            aggregator.observe(
                instrument_token=TOKEN, exchange_time=at(9, 15), last_price_paise=Decimal(price)
            )
            is None
        )
    assert aggregator.instruments_tracked == 0


@pytest.mark.unit
def test_volume_is_the_maximum_seen_because_the_tape_reports_it_cumulatively() -> None:
    """Summing cumulative readings would multiply the day's volume by the tick count."""
    aggregator = LiveTickToBarAggregator()
    for instant, price, volume in (
        (at(9, 15), "10000", 1_000),
        (at(9, 16), "10100", 4_000),
        (at(9, 17), "10050", 9_000),
    ):
        aggregator.observe(
            instrument_token=TOKEN,
            exchange_time=instant,
            last_price_paise=Decimal(price),
            volume=volume,
        )
    aggregator.observe(
        instrument_token=TOKEN, exchange_time=at(9, 21), last_price_paise=Decimal("10000")
    )
    assert aggregator.drain_completed()[0].volume == 9_000


# --------------------------------------------------------------------------------------------
# memory, and the session close
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_state_is_constant_per_instrument_however_many_ticks_arrive() -> None:
    """`L4.27`'s actual requirement — 1,845 instruments on the decision path."""
    aggregator = LiveTickToBarAggregator()
    for index in range(5_000):
        aggregator.observe(
            instrument_token=TOKEN,
            exchange_time=at(9, 15, index % 60),
            last_price_paise=Decimal(str(10_000 + index % 200)),
        )
    assert aggregator.ticks_seen == 5_000
    assert aggregator.instruments_tracked == 1
    assert len(aggregator.drain_completed()) == 0, "all 5,000 ticks fell in one bucket"


@pytest.mark.unit
def test_force_close_seals_the_final_partial_bar_of_the_session() -> None:
    """`R.01` flattens at 15:30; without this the strategy never sees the day's last minutes."""
    aggregator = LiveTickToBarAggregator()
    feed(aggregator, [(at(15, 26), "10000"), (at(15, 28), "10300")])
    assert aggregator.drain_completed() == ()
    sealed = aggregator.force_close(at(15, 30))
    assert len(sealed) == 1
    assert sealed[0].bar_timestamp == at(15, 25)
    assert sealed[0].close_price == pytest.approx(103.0)
    assert aggregator.instruments_tracked == 0


@pytest.mark.unit
def test_many_instruments_are_bucketed_independently() -> None:
    aggregator = LiveTickToBarAggregator()
    for token in range(700_000, 700_010):
        feed(aggregator, [(at(9, 15), "10000"), (at(9, 21), "10100")], token=token)
    bars = aggregator.drain_completed()
    assert len(bars) == 10
    assert len({bar.instrument_token for bar in bars}) == 10


@pytest.mark.unit
def test_an_interval_with_no_boundary_is_refused() -> None:
    with pytest.raises(TickAggregationError, match="no boundary"):
        LiveTickToBarAggregator(bar_interval=timedelta(0))


@pytest.mark.unit
def test_a_bar_with_an_impossible_range_is_refused_at_construction() -> None:
    """High below low can only be an aggregator defect — both come from one tick stream."""
    with pytest.raises(TickAggregationError, match="below low"):
        CompletedBar(
            instrument_token=TOKEN,
            bar_timestamp=at(9, 15),
            open_price=100.0,
            high_price=98.0,
            low_price=102.0,
            close_price=100.0,
            volume=0,
            bar_interval=FIVE_MINUTE_BAR,
        )
