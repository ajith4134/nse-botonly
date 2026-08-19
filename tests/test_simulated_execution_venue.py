"""Tests for the simulated venue (`F04`, `A.108` decision 2).

`A.108` recorded that this component is the risk in the whole feature: a venue that fills at the
touch teaches the system that slippage does not exist. So the tests here are mostly about REFUSING
to be optimistic — a resting order that has not filled, an order larger than the ladder, a book that
is not there.

`docs/research/228` §4 is the specification.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, DepthPacket
from nse_algo_trader.paper_loop.simulated_execution_venue import (
    PaperFillOutcome,
    PaperOrderSide,
    SimulatedExecutionVenue,
    SimulatedVenueError,
)

IST = ZoneInfo("Asia/Kolkata")


def _book(
    *,
    bids: list[tuple[int, int]] | None = None,
    asks: list[tuple[int, int]] | None = None,
) -> DepthPacket:
    """A book with the ladders given, as `(price_paise, quantity)` from the touch outward."""
    return DepthPacket(
        instrument_token=738561,
        exchange="NSE",
        exchange_time=datetime(2026, 8, 11, 10, 0, tzinfo=IST),
        receipt_time=datetime(2026, 8, 11, 10, 0, 0, 250_000, tzinfo=IST),
        receipt_sequence=1,
        last_price_paise=100_000,
        last_traded_quantity=10,
        average_traded_price_paise=100_000,
        volume_traded=100_000,
        total_buy_quantity=5_000,
        total_sell_quantity=5_000,
        open_interest=0,
        bids=tuple(DepthLevel(price_paise=p, quantity=q, orders=1) for p, q in (bids or [])),
        asks=tuple(DepthLevel(price_paise=p, quantity=q, orders=1) for p, q in (asks or [])),
    )


_FIVE_DEEP = _book(
    bids=[(99_900, 100), (99_800, 200), (99_700, 300), (99_600, 400), (99_500, 500)],
    asks=[(100_100, 100), (100_200, 200), (100_300, 300), (100_400, 400), (100_500, 500)],
)


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_small_market_buy_fills_at_the_touch() -> None:
    fill = SimulatedExecutionVenue().fill_market_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=50
    )
    assert fill.outcome is PaperFillOutcome.FILLED
    assert fill.average_price_paise == Decimal(100_100)
    assert fill.slippage_paise == Decimal(0)
    assert len(fill.levels_consumed) == 1


@pytest.mark.unit
def test_a_larger_buy_walks_the_ladder_and_pays_the_volume_weighted_price() -> None:
    """The whole point: the walk IS the impact, arithmetic checked by hand.

    250 shares against 100@100,100 + 200@100,200 takes 100 then 150:
    (100*100100 + 150*100200) / 250 = 100,160 paise.
    """
    fill = SimulatedExecutionVenue().fill_market_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=250
    )
    assert fill.outcome is PaperFillOutcome.FILLED
    assert fill.average_price_paise == Decimal(100_160)
    assert fill.slippage_paise == Decimal(60)
    assert [(lvl.price_paise, lvl.quantity) for lvl in fill.levels_consumed] == [
        (100_100, 100),
        (100_200, 150),
    ]


@pytest.mark.unit
def test_a_sell_takes_from_the_bids_and_pays_away_from_the_touch() -> None:
    fill = SimulatedExecutionVenue().fill_market_order(
        book=_FIVE_DEEP, side=PaperOrderSide.SELL, quantity=250
    )
    assert fill.average_price_paise == Decimal(99_840)  # (100*99900 + 150*99800)/250
    assert fill.slippage_paise == Decimal(-60)  # a seller receives LESS than the touch


@pytest.mark.unit
def test_a_marketable_limit_walks_the_ladder_and_stops_at_its_price() -> None:
    """A limit through the touch takes liquidity, but never past the price it stated."""
    fill = SimulatedExecutionVenue().fill_limit_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=1000, limit_price_paise=100_200
    )
    assert fill.outcome is PaperFillOutcome.PARTIALLY_FILLED
    assert fill.filled_quantity == 300  # 100 + 200, then the ladder is through the limit
    assert fill.unfilled_quantity == 700
    assert max(level.price_paise for level in fill.levels_consumed) == 100_200


@pytest.mark.unit
def test_the_fill_describes_the_walk_that_produced_it() -> None:
    described = (
        SimulatedExecutionVenue()
        .fill_market_order(book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=250)
        .describe()
    )
    assert "100@100100" in described
    assert "150@100200" in described
    assert "from the touch" in described


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_limit_away_from_the_touch_rests_behind_the_size_already_there() -> None:
    """The single most common way a paper book flatters itself.

    A buy at 99,800 does NOT fill because the price appears in the book — it joins a queue behind
    the 200 already showing there, and with a snapshot rather than a trade stream the truthful
    answer is that it has not filled.
    """
    fill = SimulatedExecutionVenue().fill_limit_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=50, limit_price_paise=99_800
    )
    assert fill.outcome is PaperFillOutcome.RESTING
    assert fill.filled_quantity == 0
    assert fill.queue_ahead_quantity == 200
    assert fill.average_price_paise is None
    assert "behind 200" in fill.describe()


@pytest.mark.adversarial
def test_an_order_larger_than_the_whole_ladder_partially_fills() -> None:
    """Never a full fill on invisible depth. The visible ladder IS the participation cap."""
    fill = SimulatedExecutionVenue().fill_market_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=5_000
    )
    assert fill.outcome is PaperFillOutcome.PARTIALLY_FILLED
    assert fill.filled_quantity == 1_500  # 100+200+300+400+500, the entire visible ask side
    assert fill.unfilled_quantity == 3_500


@pytest.mark.adversarial
def test_an_instrument_with_no_recorded_depth_is_refused_not_filled_at_the_last_price() -> None:
    """Filling here would invent a counterparty. `L1.09`'s lesson one layer out."""
    empty = _book(bids=[(99_900, 100)], asks=[])
    fill = SimulatedExecutionVenue().fill_market_order(
        book=empty, side=PaperOrderSide.BUY, quantity=10
    )
    assert fill.outcome is PaperFillOutcome.REFUSED
    assert fill.filled_quantity == 0
    assert fill.average_price_paise is None
    assert fill.refusal_reason is not None
    assert "inventing a counterparty" in fill.refusal_reason


@pytest.mark.adversarial
def test_slippage_is_none_rather_than_zero_when_nothing_filled() -> None:
    """A slippage of zero claims the order paid the best price, which an unfilled order did not."""
    resting = SimulatedExecutionVenue().fill_limit_order(
        book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=50, limit_price_paise=99_800
    )
    assert resting.slippage_paise is None


@pytest.mark.adversarial
@pytest.mark.parametrize("quantity", [0, -10])
def test_a_non_positive_quantity_never_reaches_the_venue(quantity: int) -> None:
    """`F03`'s zero is a REFUSAL, and a refusal that reaches a venue is a defect upstream."""
    with pytest.raises(SimulatedVenueError):
        SimulatedExecutionVenue().fill_market_order(
            book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=quantity
        )


@pytest.mark.adversarial
def test_a_limit_price_of_zero_is_refused() -> None:
    with pytest.raises(SimulatedVenueError):
        SimulatedExecutionVenue().fill_limit_order(
            book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=10, limit_price_paise=0
        )


@pytest.mark.adversarial
def test_a_one_level_book_fills_only_what_that_level_holds() -> None:
    thin = _book(bids=[(99_900, 5)], asks=[(100_100, 5)])
    fill = SimulatedExecutionVenue().fill_market_order(
        book=thin, side=PaperOrderSide.BUY, quantity=100
    )
    assert fill.filled_quantity == 5
    assert fill.outcome is PaperFillOutcome.PARTIALLY_FILLED


@pytest.mark.adversarial
def test_a_buy_never_fills_better_than_the_touch() -> None:
    """The property that makes the record admissible: no order may beat the best visible price."""
    venue = SimulatedExecutionVenue()
    for quantity in (1, 50, 100, 101, 500, 1_500, 9_999):
        fill = venue.fill_market_order(book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=quantity)
        assert fill.average_price_paise is not None
        assert fill.average_price_paise >= Decimal(100_100), quantity


@pytest.mark.adversarial
def test_a_sell_never_fills_better_than_its_touch_either() -> None:
    venue = SimulatedExecutionVenue()
    for quantity in (1, 50, 100, 101, 500, 1_500, 9_999):
        fill = venue.fill_market_order(book=_FIVE_DEEP, side=PaperOrderSide.SELL, quantity=quantity)
        assert fill.average_price_paise is not None
        assert fill.average_price_paise <= Decimal(99_900), quantity


@pytest.mark.adversarial
def test_filled_quantity_never_exceeds_the_visible_depth() -> None:
    """`R.13`'s discipline applied to fills: the record may not claim liquidity that was not
    there."""
    venue = SimulatedExecutionVenue()
    visible = sum(level.quantity for level in _FIVE_DEEP.asks)
    for quantity in (1, 250, 1_500, 100_000):
        fill = venue.fill_market_order(book=_FIVE_DEEP, side=PaperOrderSide.BUY, quantity=quantity)
        assert fill.filled_quantity <= visible
        assert fill.filled_quantity <= quantity
        assert sum(level.quantity for level in fill.levels_consumed) == fill.filled_quantity
