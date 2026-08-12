"""`L0.22` — order-book snapshot replay and microstructure inference.

Spec: `docs/research/214`. The engine estimates unobservable quantities (order flow,
trade direction, execution vs cancellation) from observable 5-level snapshots. Every
test here is a way an estimator can be confidently wrong, because none of these
quantities can be checked against a ground truth this project can buy (`research/72`) —
so the invariants ARE the verification.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    OrderBookSnapshotReplayEngine,
    TradeSideRule,
    UnknownInstrumentError,
    book_snapshots_from_table,
    classify_trade_side,
    depth_imbalance_by_level,
    micro_price_paise,
    order_flow_imbalance_by_level,
    queue_depletion_interval,
)

LIVE_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")
BASE_TIME = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)


def _book(
    bid_price: int,
    bid_quantity: int,
    ask_price: int,
    ask_quantity: int,
    *,
    levels: int = 1,
) -> tuple[tuple[DepthLevel, ...], tuple[DepthLevel, ...]]:
    """One book, deepened by a fixed tick so multi-level tests have something to chew."""
    bids = tuple(
        DepthLevel(bid_price - step * 5, bid_quantity + step, 1 + step) for step in range(levels)
    )
    asks = tuple(
        DepthLevel(ask_price + step * 5, ask_quantity + step, 1 + step) for step in range(levels)
    )
    return bids, asks


# ------------------------------------------------------------------ OFI, by hand


@pytest.mark.unit
@pytest.mark.parametrize(
    ("previous", "current", "expected", "reading"),
    [
        # (bid_price, bid_qty, ask_price, ask_qty)
        ((100, 10, 101, 10), (100, 10, 101, 10), 0, "nothing moved"),
        ((100, 10, 101, 10), (100, 15, 101, 10), 5, "bid grew at the same price"),
        ((100, 10, 101, 10), (100, 5, 101, 10), -5, "bid shrank at the same price"),
        ((100, 10, 101, 10), (100, 10, 101, 15), -5, "ask grew: selling pressure"),
        ((100, 10, 101, 10), (100, 10, 101, 5), 5, "ask shrank: selling pressure left"),
        ((100, 10, 101, 10), (101, 8, 102, 10), 8 + 10, "bid stepped up, ask stepped up"),
        ((100, 10, 101, 10), (99, 8, 101, 10), -10, "bid fell away entirely"),
        # The old ask does NOT also count as leaving: 1{P^a_n ≥ P^a_{n-1}} is false when
        # the ask improves, so only the newly aggressive size enters. Getting this wrong
        # double-counts every price improvement.
        ((100, 10, 101, 10), (100, 10, 100, 7), -7, "ask stepped down onto the bid"),
        ((100, 10, 101, 10), (100, 10, 102, 7), 10, "ask stepped up and away"),
    ],
)
def test_order_flow_imbalance_matches_the_hand_computed_cases(
    previous: tuple[int, int, int, int],
    current: tuple[int, int, int, int],
    expected: int,
    reading: str,
) -> None:
    """Cont-Kukanov-Stoikov, level 1, every sign case enumerated (`research/214` §2.1)."""
    previous_bids, previous_asks = _book(*previous)
    current_bids, current_asks = _book(*current)
    imbalance = order_flow_imbalance_by_level(
        previous_bids, previous_asks, current_bids, current_asks
    )
    assert imbalance[0] == expected, reading


@pytest.mark.unit
def test_order_flow_imbalance_is_computed_per_level_independently() -> None:
    """Deeper levels move for different reasons; collapsing them early destroys that."""
    previous_bids, previous_asks = _book(100, 10, 101, 10, levels=5)
    current_bids = (
        DepthLevel(100, 10, 1),
        DepthLevel(95, 40, 2),  # level 2 bid grew by 29
        *previous_bids[2:],
    )
    imbalance = order_flow_imbalance_by_level(
        previous_bids, previous_asks, current_bids, previous_asks
    )
    assert imbalance[0] == 0, "the touch did not move"
    assert imbalance[1] == 29, "level 2 grew by 29"
    assert imbalance[2:] == (0, 0, 0)


@pytest.mark.unit
def test_a_vanished_side_is_not_read_as_a_price_improvement() -> None:
    """An empty book side is absence, not a zero-priced level at the top of the book."""
    previous_bids, previous_asks = _book(100, 10, 101, 10)
    imbalance = order_flow_imbalance_by_level(previous_bids, previous_asks, (), previous_asks)
    assert imbalance[0] == -10, "the whole resting bid left"


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    previous_bid=st.integers(1, 10_000),
    previous_ask_gap=st.integers(1, 500),
    previous_bid_quantity=st.integers(0, 100_000),
    previous_ask_quantity=st.integers(0, 100_000),
    current_bid=st.integers(1, 10_000),
    current_ask_gap=st.integers(1, 500),
    current_bid_quantity=st.integers(0, 100_000),
    current_ask_quantity=st.integers(0, 100_000),
)
def test_order_flow_imbalance_is_antisymmetric_under_swapping_the_sides(
    previous_bid: int,
    previous_ask_gap: int,
    previous_bid_quantity: int,
    previous_ask_quantity: int,
    current_bid: int,
    current_ask_gap: int,
    current_bid_quantity: int,
    current_ask_quantity: int,
) -> None:
    """Mirror the book and the imbalance must negate — the defining symmetry of OFI.

    A sign error in one of the four indicator terms survives every example test that
    happens to exercise the other three. This is the test that catches it.
    """
    previous_bids, previous_asks = _book(
        previous_bid, previous_bid_quantity, previous_bid + previous_ask_gap, previous_ask_quantity
    )
    current_bids, current_asks = _book(
        current_bid, current_bid_quantity, current_bid + current_ask_gap, current_ask_quantity
    )
    forward = order_flow_imbalance_by_level(
        previous_bids, previous_asks, current_bids, current_asks
    )

    # Mirroring a book means reflecting prices about zero as well as swapping sides,
    # so that "bid improved" maps to "ask improved" rather than to its opposite.
    def mirror(levels: tuple[DepthLevel, ...]) -> tuple[DepthLevel, ...]:
        return tuple(
            DepthLevel(-level.price_paise, level.quantity, level.orders) for level in levels
        )

    mirrored = order_flow_imbalance_by_level(
        mirror(previous_asks), mirror(previous_bids), mirror(current_asks), mirror(current_bids)
    )
    assert mirrored[0] == -forward[0]


# -------------------------------------------------------------- micro-price


@pytest.mark.unit
def test_micro_price_leans_toward_the_side_with_less_size() -> None:
    """Stoikov: a thin ask with a heavy bid means the next trade prints nearer the ask."""
    heavy_bid = micro_price_paise(
        best_bid_paise=10_000, best_bid_quantity=900, best_ask_paise=10_010, best_ask_quantity=100
    )
    assert heavy_bid is not None, "both sides are present and uncrossed: a price must exist"
    assert heavy_bid > 10_005, "pressure is upward, so the fair price sits above the mid"


@pytest.mark.unit
def test_micro_price_equals_the_mid_when_both_sides_are_equal() -> None:
    assert (
        micro_price_paise(
            best_bid_paise=10_000,
            best_bid_quantity=500,
            best_ask_paise=10_010,
            best_ask_quantity=500,
        )
        == 10_005
    )


@pytest.mark.adversarial
def test_micro_price_is_undefined_on_a_crossed_book() -> None:
    """Measured: 757 of 135,401 real transitions crossed, all flagged `BOOK_CROSSED`.

    The formula still returns a number there — one that sits below every bid in the book.
    A fair price derived from a contradiction is worse than no price, because a consumer
    cannot tell it apart from a good one.
    """
    assert (
        micro_price_paise(
            best_bid_paise=781_900,
            best_bid_quantity=100,
            best_ask_paise=770_000,
            best_ask_quantity=100,
        )
        is None
    )


@pytest.mark.adversarial
def test_micro_price_is_undefined_rather_than_zero_when_a_side_is_empty() -> None:
    """Returning 0, or the lone side's price, would be a number a consumer would trust."""
    assert (
        micro_price_paise(
            best_bid_paise=10_000, best_bid_quantity=0, best_ask_paise=10_010, best_ask_quantity=0
        )
        is None
    )
    assert (
        micro_price_paise(
            best_bid_paise=None, best_bid_quantity=0, best_ask_paise=10_010, best_ask_quantity=5
        )
        is None
    )


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    bid=st.integers(1, 10_000_000),
    spread=st.integers(1, 1_000),
    bid_quantity=st.integers(1, 1_000_000),
    ask_quantity=st.integers(1, 1_000_000),
)
def test_micro_price_always_lies_inside_the_spread(
    bid: int, spread: int, bid_quantity: int, ask_quantity: int
) -> None:
    """A fair price outside the quotes is arbitrage, i.e. an implementation error."""
    price = micro_price_paise(
        best_bid_paise=bid,
        best_bid_quantity=bid_quantity,
        best_ask_paise=bid + spread,
        best_ask_quantity=ask_quantity,
    )
    assert price is not None
    assert bid <= price <= bid + spread


# --------------------------------------------------------- trade classification


@pytest.mark.unit
def test_a_trade_at_the_ask_is_buyer_initiated_by_the_quote_rule() -> None:
    attribution = classify_trade_side(
        traded_quantity=50,
        trade_price_paise=10_010,
        previous_best_bid_paise=10_000,
        previous_best_ask_paise=10_010,
        previous_trade_price_paise=None,
        last_signed_direction=0,
    )
    assert attribution.buyer_initiated_quantity == 50
    assert attribution.seller_initiated_quantity == 0
    assert attribution.rule is TradeSideRule.QUOTE_RULE


@pytest.mark.unit
def test_a_trade_at_the_bid_is_seller_initiated_by_the_quote_rule() -> None:
    attribution = classify_trade_side(
        traded_quantity=50,
        trade_price_paise=10_000,
        previous_best_bid_paise=10_000,
        previous_best_ask_paise=10_010,
        previous_trade_price_paise=None,
        last_signed_direction=0,
    )
    assert attribution.seller_initiated_quantity == 50
    assert attribution.rule is TradeSideRule.QUOTE_RULE


@pytest.mark.unit
def test_a_trade_inside_the_spread_falls_back_to_the_tick_rule() -> None:
    """The quote rule cannot speak here; the tick rule can, and says so."""
    attribution = classify_trade_side(
        traded_quantity=40,
        trade_price_paise=10_005,
        previous_best_bid_paise=10_000,
        previous_best_ask_paise=10_010,
        previous_trade_price_paise=10_002,
        last_signed_direction=0,
    )
    assert attribution.buyer_initiated_quantity == 40, "an up-tick is a buy"
    assert attribution.rule is TradeSideRule.TICK_RULE


@pytest.mark.unit
def test_a_zero_tick_inherits_the_last_known_direction() -> None:
    """Lee-Ready's zero-tick handling: inherit rather than guess or discard."""
    attribution = classify_trade_side(
        traded_quantity=25,
        trade_price_paise=10_005,
        previous_best_bid_paise=10_000,
        previous_best_ask_paise=10_010,
        previous_trade_price_paise=10_005,
        last_signed_direction=-1,
    )
    assert attribution.seller_initiated_quantity == 25
    assert attribution.rule is TradeSideRule.ZERO_TICK_INHERITED


@pytest.mark.adversarial
def test_the_first_trade_of_a_session_is_unclassified_not_a_coin_flip() -> None:
    """No prior quote, no prior trade: assigning a side here would fabricate order flow."""
    attribution = classify_trade_side(
        traded_quantity=100,
        trade_price_paise=10_005,
        previous_best_bid_paise=None,
        previous_best_ask_paise=None,
        previous_trade_price_paise=None,
        last_signed_direction=0,
    )
    assert attribution.rule is TradeSideRule.UNCLASSIFIED
    assert attribution.buyer_initiated_quantity == 0
    assert attribution.seller_initiated_quantity == 0
    assert attribution.unclassified_quantity == 100


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    traded_quantity=st.integers(0, 1_000_000),
    trade_price=st.integers(1, 100_000),
    bid=st.integers(1, 100_000),
    spread=st.integers(1, 1_000),
    previous_trade_price=st.one_of(st.none(), st.integers(1, 100_000)),
    last_direction=st.sampled_from([-1, 0, 1]),
)
def test_every_traded_share_is_accounted_for_exactly_once(
    traded_quantity: int,
    trade_price: int,
    bid: int,
    spread: int,
    previous_trade_price: int | None,
    last_direction: int,
) -> None:
    """The conservation law. A classifier that loses or duplicates volume is worse than
    one that admits it does not know, because the error is invisible in aggregate."""
    attribution = classify_trade_side(
        traded_quantity=traded_quantity,
        trade_price_paise=trade_price,
        previous_best_bid_paise=bid,
        previous_best_ask_paise=bid + spread,
        previous_trade_price_paise=previous_trade_price,
        last_signed_direction=last_direction,
    )
    assert (
        attribution.buyer_initiated_quantity
        + attribution.seller_initiated_quantity
        + attribution.unclassified_quantity
        == traded_quantity
    )


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(
    cases=st.lists(
        st.tuples(
            st.integers(1, 50_000),  # bid
            st.integers(1, 200),  # spread
            st.integers(-300, 300),  # trade price offset from the bid
            st.one_of(st.none(), st.integers(-300, 300)),  # previous trade offset
        ),
        min_size=1,
        max_size=25,
    )
)
def test_classification_agrees_with_the_maintained_reference_implementation(
    cases: list[tuple[int, int, int, int | None]],
) -> None:
    """Differential test against `tclf`, the one part of this engine OSS actually covers.

    The sourcing pass (`research/214` §6) found no installable implementation of
    multi-level OFI or the micro-price, but `tclf` is a maintained, tested
    implementation of Lee-Ready and its relatives. It is not adopted as the classifier —
    it is sklearn/pandas batch-shaped and this engine classifies inside a streaming
    replay that carries state — so it is used the way a second implementation is most
    valuable: as an oracle.

    It earned its place immediately: it found that this engine's quote rule compared
    against the TOUCH rather than the MIDPOINT, so every trade inside the spread fell
    through to the weaker tick rule. `tclf` called `bid=1 ask=4 trade=2` a sell; this
    engine called it unclassified. The engine was wrong and now matches.

    `last_signed_direction` is pinned to 0 so the one deliberate divergence — this engine
    inherits the last non-zero direction on a zero tick, as Lee-Ready prescribes, where
    `tclf`'s tick layer abstains — collapses to the same answer and cannot mask a real
    disagreement.
    """
    pandas = pytest.importorskip("pandas")
    numpy = pytest.importorskip("numpy")
    classical_classifier = pytest.importorskip("tclf.classical_classifier")

    frame = pandas.DataFrame(
        {
            "trade_price": [float(bid + offset) for bid, _, offset, _ in cases],
            "bid_ex": [float(bid) for bid, _, _, _ in cases],
            "ask_ex": [float(bid + spread) for bid, spread, _, _ in cases],
            "price_ex_lag": [
                numpy.nan if previous is None else float(bid + previous)
                for bid, _, _, previous in cases
            ],
        }
    )
    reference = classical_classifier.ClassicalClassifier(layers=[("lr", "ex")], strategy="const")
    reference.fit(frame)
    reference_signs = reference.predict(frame)

    for index, (bid, spread, offset, previous) in enumerate(cases):
        trade_price = bid + offset
        previous_trade_price = None if previous is None else bid + previous
        attribution = classify_trade_side(
            traded_quantity=100,
            trade_price_paise=trade_price,
            previous_best_bid_paise=bid,
            previous_best_ask_paise=bid + spread,
            previous_trade_price_paise=previous_trade_price,
            last_signed_direction=0,
        )
        ours = (
            1
            if attribution.buyer_initiated_quantity
            else (-1 if attribution.seller_initiated_quantity else 0)
        )
        assert ours == reference_signs[index], (
            f"disagreed with tclf on bid={bid} ask={bid + spread} trade={trade_price} "
            f"previous_trade={previous_trade_price}: ours={ours}, tclf={reference_signs[index]}"
        )


# ------------------------------------------------------------ queue depletion


@pytest.mark.unit
def test_queue_depletion_bounds_executions_by_the_inferred_sell_volume() -> None:
    """20 shares left the bid queue and at most 8 of them traded: 12 were cancelled."""
    interval = queue_depletion_interval(
        previous_quantity=100,
        current_quantity=80,
        price_unchanged=True,
        inferred_executed_quantity=8,
    )
    assert interval is not None, "the price held: a depletion decomposition is defined"
    assert interval.minimum_executed == 0
    assert interval.maximum_executed == 8
    assert interval.minimum_cancelled == 12
    assert interval.maximum_cancelled == 20


@pytest.mark.unit
def test_a_growing_queue_has_no_depletion_to_decompose() -> None:
    interval = queue_depletion_interval(
        previous_quantity=100,
        current_quantity=140,
        price_unchanged=True,
        inferred_executed_quantity=0,
    )
    assert interval is not None, "the price held: a depletion decomposition is defined"
    assert interval.maximum_executed == 0
    assert interval.maximum_cancelled == 0


@pytest.mark.adversarial
def test_a_moved_price_makes_the_decomposition_undefined_rather_than_wrong() -> None:
    """When the touch price changes, the old queue did not deplete — it was replaced."""
    assert (
        queue_depletion_interval(
            previous_quantity=100,
            current_quantity=5,
            price_unchanged=False,
            inferred_executed_quantity=50,
        )
        is None
    )


@pytest.mark.adversarial
def test_more_inferred_executions_than_depletion_is_clamped_not_negative() -> None:
    """Signed-volume inference is an estimate and can exceed what the queue lost."""
    interval = queue_depletion_interval(
        previous_quantity=100,
        current_quantity=95,
        price_unchanged=True,
        inferred_executed_quantity=40,
    )
    assert interval is not None, "the price held: a depletion decomposition is defined"
    assert interval.maximum_executed == 5
    assert interval.minimum_cancelled == 0


# --------------------------------------------------------------- depth imbalance


@pytest.mark.unit
def test_depth_imbalance_is_signed_and_bounded() -> None:
    bids, asks = _book(100, 300, 101, 100, levels=1)
    imbalance = depth_imbalance_by_level(bids, asks)
    assert imbalance[0] == pytest.approx(0.5), "(300-100)/(300+100)"


@pytest.mark.adversarial
def test_depth_imbalance_of_an_empty_level_is_none_not_zero() -> None:
    """Zero means balanced. Absent means unknown. They must not share a value."""
    imbalance = depth_imbalance_by_level((), ())
    assert imbalance[0] is None


# ------------------------------------------------------------------ replay


class _DepthTapeRow(TypedDict):
    """One synthetic snapshot handed to `_write_tape`.

    Only the touch prices/quantities are mandatory; everything else defaults the way a
    fresh session would (no trade yet, zero cumulative volume, one shared token).
    """

    bid_price: int
    bid_quantity: int
    ask_price: int
    ask_quantity: int
    token: NotRequired[int]
    last_price: NotRequired[int]
    last_quantity: NotRequired[int]
    volume: NotRequired[int]


def _write_tape(tmp_path: Path, rows: list[_DepthTapeRow]) -> MarketDepthTapeReader:
    """Build a real Parquet tape with the production writer, not a mock."""
    from nse_algo_trader.market_depth.depth_tape_schema import DepthPacket
    from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeStore

    store = MarketDepthTapeStore(
        tape_root=tmp_path,
        session_date=BASE_TIME.date(),
        shard_index=1,
        capture_run_id="test",
        max_buffered_rows=10_000,
        max_seconds_between_flushes=3600.0,
    )
    for index, row in enumerate(rows):
        bids, asks = _book(
            int(row["bid_price"]),
            int(row["bid_quantity"]),
            int(row["ask_price"]),
            int(row["ask_quantity"]),
            levels=5,
        )
        store.append(
            DepthPacket(
                instrument_token=int(row.get("token", 111)),
                exchange="NSE",
                exchange_time=BASE_TIME + timedelta(seconds=index),
                receipt_time=BASE_TIME + timedelta(seconds=index),
                receipt_sequence=index,
                last_price_paise=int(row.get("last_price", row["bid_price"])),
                last_traded_quantity=int(row.get("last_quantity", 0)),
                average_traded_price_paise=int(row["bid_price"]),
                volume_traded=int(row.get("volume", 0)),
                total_buy_quantity=int(row["bid_quantity"]),
                total_sell_quantity=int(row["ask_quantity"]),
                open_interest=0,
                bids=bids,
                asks=asks,
            ),
            IntegrityFlag.NONE,
        )
    store.close()
    return MarketDepthTapeReader(tmp_path)


@pytest.mark.hermetic
def test_replay_emits_one_row_per_transition_not_per_snapshot(tmp_path: Path) -> None:
    """N snapshots give N-1 transitions: OFI is undefined without a predecessor."""
    reader = _write_tape(
        tmp_path,
        [
            {"bid_price": 10_000, "bid_quantity": 100, "ask_price": 10_010, "ask_quantity": 100},
            {"bid_price": 10_000, "bid_quantity": 150, "ask_price": 10_010, "ask_quantity": 100},
            {"bid_price": 10_000, "bid_quantity": 120, "ask_price": 10_010, "ask_quantity": 100},
        ],
    )
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    rows = list(engine.replay_instrument(111))
    assert len(rows) == 2
    assert rows[0].order_flow_imbalance_level_one == 50
    assert rows[1].order_flow_imbalance_level_one == -30


@pytest.mark.hermetic
def test_signed_volume_comes_from_the_volume_delta_not_the_cumulative_total(
    tmp_path: Path,
) -> None:
    """`volume_traded` is cumulative for the session; using it raw inflates every row."""
    reader = _write_tape(
        tmp_path,
        [
            {
                "bid_price": 10_000,
                "bid_quantity": 100,
                "ask_price": 10_010,
                "ask_quantity": 100,
                "volume": 1_000,
            },
            {
                "bid_price": 10_000,
                "bid_quantity": 100,
                "ask_price": 10_010,
                "ask_quantity": 100,
                "volume": 1_060,
                "last_price": 10_010,
            },
        ],
    )
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    row = next(iter(engine.replay_instrument(111)))
    assert row.signed_volume.traded_quantity == 60
    assert row.signed_volume.buyer_initiated_quantity == 60


@pytest.mark.adversarial
def test_an_unknown_instrument_raises_rather_than_yielding_nothing(tmp_path: Path) -> None:
    """ "No data" and "wrong token" are different bugs; an empty iterator merges them."""
    reader = _write_tape(
        tmp_path,
        [{"bid_price": 10_000, "bid_quantity": 100, "ask_price": 10_010, "ask_quantity": 100}],
    )
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    with pytest.raises(UnknownInstrumentError, match="999"):
        list(engine.replay_instrument(999))


@pytest.mark.adversarial
def test_a_duplicate_book_is_emitted_with_its_run_length_not_dropped(tmp_path: Path) -> None:
    """27% of the first live capture was duplicate books. Dropping them silently would
    make the gap to the next real change look like a fast market."""
    identical: _DepthTapeRow = {
        "bid_price": 10_000,
        "bid_quantity": 100,
        "ask_price": 10_010,
        "ask_quantity": 100,
    }
    reader = _write_tape(
        tmp_path, [identical, identical, identical, {**identical, "bid_quantity": 120}]
    )
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    rows = list(engine.replay_instrument(111))
    assert [row.duplicate_run_length for row in rows] == [1, 2, 0]
    assert rows[-1].order_flow_imbalance_level_one == 20


@pytest.mark.adversarial
def test_the_feeds_zero_padding_is_not_read_as_a_quote_at_zero(tmp_path: Path) -> None:
    """The defect the first real-data run found (`A.79`).

    Kite always sends five levels and pads the absent ones with price 0 / quantity 0.
    Reading a padded level as a quote makes the best ask zero for a one-sided book, and
    the micro-price then lands below every real price in the book.
    """
    from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeStore

    store = MarketDepthTapeStore(
        tape_root=tmp_path,
        session_date=BASE_TIME.date(),
        shard_index=1,
        capture_run_id="pad",
        max_buffered_rows=100,
        max_seconds_between_flushes=3600.0,
    )
    from nse_algo_trader.market_depth.depth_tape_schema import DepthPacket

    for index in range(2):
        store.append(
            DepthPacket(
                instrument_token=222,
                exchange="NSE",
                exchange_time=BASE_TIME + timedelta(seconds=index),
                receipt_time=BASE_TIME + timedelta(seconds=index),
                receipt_sequence=index,
                last_price_paise=640,
                last_traded_quantity=0,
                average_traded_price_paise=640,
                volume_traded=0,
                total_buy_quantity=10,
                total_sell_quantity=0,
                open_interest=0,
                bids=(DepthLevel(640, 10 + index, 1), DepthLevel(0, 0, 0)),
                asks=(DepthLevel(0, 0, 0),),  # a one-sided book: no ask exists at all
            ),
            IntegrityFlag.NONE,
        )
    store.close()

    engine = OrderBookSnapshotReplayEngine(
        MarketDepthTapeReader(tmp_path), session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    row = next(iter(engine.replay_instrument(222)))
    assert row.best_bid_paise == 640
    assert row.best_ask_paise is None, "padding is absence, not a quote at zero"
    assert row.micro_price_paise is None, "no fair price exists without two sides"
    assert row.spread_paise is None
    assert row.order_flow_imbalance_level_one == 1, "the real bid grew by one"


@pytest.mark.hermetic
def test_every_row_carries_the_gap_that_produced_it(tmp_path: Path) -> None:
    """A feature from a 4-second gap is not the same object as one from 250ms."""
    reader = _write_tape(
        tmp_path,
        [
            {"bid_price": 10_000, "bid_quantity": 100, "ask_price": 10_010, "ask_quantity": 100},
            {"bid_price": 10_000, "bid_quantity": 110, "ask_price": 10_010, "ask_quantity": 100},
        ],
    )
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=BASE_TIME.date(), staleness_quantile=0.99
    )
    row = next(iter(engine.replay_instrument(111)))
    assert row.gap_milliseconds == 1_000


# ------------------------------------------------------------------ real data


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_replay_holds_its_invariants_on_the_real_tape() -> None:
    """R.05 — the invariants ARE the verification.

    There is no ground truth to compare against: nobody sells the order-by-order data
    that would settle whether a given trade was buyer-initiated (`research/72`). What
    can be checked is that every row the engine emits obeys the laws the estimators are
    defined by, on real books with real crossed quotes, real epoch-0 timestamps and real
    duplicate runs.
    """
    from datetime import date as date_type

    reader = MarketDepthTapeReader(LIVE_TAPE_ROOT)
    session_date = sorted(reader.session_dates())[0]
    assert isinstance(session_date, date_type)
    tokens = reader.instrument_tokens(session_date)
    assert tokens, "the tape holds no instruments"

    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=session_date, staleness_quantile=0.99
    )
    inspected_rows = 0
    for token in tokens[:25]:
        for row in engine.replay_instrument(token):
            inspected_rows += 1
            assert (
                row.signed_volume.buyer_initiated_quantity
                + row.signed_volume.seller_initiated_quantity
                + row.signed_volume.unclassified_quantity
                == row.signed_volume.traded_quantity
            ), "volume conservation"
            if row.micro_price_paise is not None:
                assert row.best_bid_paise is not None and row.best_ask_paise is not None
                assert row.best_bid_paise <= row.micro_price_paise <= row.best_ask_paise, (
                    "micro-price left the spread"
                )
            assert row.gap_milliseconds >= 0, "time ran backwards"
            assert len(row.order_flow_imbalance_by_level) == 5

    assert inspected_rows > 1_000, f"only {inspected_rows} rows replayed — too thin to trust"


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_the_coverage_report_measures_the_real_session() -> None:
    """R.08 — the dashboard surface reads this, so it is measured, never hand-authored."""
    reader = MarketDepthTapeReader(LIVE_TAPE_ROOT)
    session_date = sorted(reader.session_dates())[0]
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=session_date, staleness_quantile=0.99
    )
    report = engine.coverage_report(instrument_limit=10)
    assert report.instruments_replayed == 10
    assert report.feature_rows_emitted > 0
    assert 0.0 <= report.quote_rule_fraction <= 1.0
    assert 0.0 <= report.unclassified_fraction <= 1.0
    assert (
        report.quote_rule_fraction
        + report.tick_rule_fraction
        + report.zero_tick_fraction
        + report.unclassified_fraction
        == pytest.approx(1.0)
    )


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_snapshots_read_from_the_real_tape_are_ordered_by_time_then_sequence() -> None:
    """The 2026-08-11 tape has two capture runs whose sequences both start at zero.

    Ordering by sequence alone lets the older run's high counter beat the newer run's
    low one, which returns a stale book while looking perfectly sorted.
    """
    reader = MarketDepthTapeReader(LIVE_TAPE_ROOT)
    session_date = sorted(reader.session_dates())[0]
    token = reader.instrument_tokens(session_date)[0]
    window_start = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    table = reader.read_instrument_window(
        token, window_start, window_start + timedelta(days=1), session_date
    )
    snapshots = book_snapshots_from_table(table)
    assert len(snapshots) > 1
    receipt_times = [snapshot.receipt_time for snapshot in snapshots]
    assert receipt_times == sorted(receipt_times), "replay order is not time-ordered"
