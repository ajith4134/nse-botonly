"""`L1.05` + `L1.06` — the fill and impact engine.

The load-bearing tests here are the two that pin the engine's central claim, which is that a
book-walk stops being evidence at a boundary and must hand over rather than extrapolate:

- `test_the_walk_saturates_while_the_impact_curve_keeps_growing` reproduces the failure the
  whole design exists to prevent — a naive walk reporting the SAME cost for a hundredfold
  increase in size.
- `test_the_impact_curve_meets_the_book_walk_exactly_at_the_anchor` pins the continuity that
  removes the need for a literature constant. If it ever fails, the coefficient has stopped
  being derived from data and started being asserted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.execution_fill.execution_fill_model import (
    ExecutionFillError,
    ExecutionFillModel,
)
from nse_algo_trader.execution_fill.execution_fill_parameter_store import (
    DEFAULT_BUCKET,
    BucketImpactParameter,
    ExecutionFillParameterError,
    ExecutionFillParameterStore,
    RealisedFillObservation,
    unfitted_parameter_for,
)
from nse_algo_trader.execution_fill.instrument_liquidity_buckets import (
    InstrumentLiquidityObservation,
    LiquidityBucket,
    LiquidityBucketError,
    TickRegime,
    bucket_universe_by_liquidity,
    classify_tick_regime,
)
from nse_algo_trader.execution_fill.market_impact_estimator import (
    PLAUSIBLE_EXPONENT_RANGE,
    ImpactEstimateMaturity,
    MarketImpactError,
    blend_toward_peers,
    estimate_impact_from_walk,
    shrinkage_weight,
)
from nse_algo_trader.execution_fill.order_book_walk_calculator import (
    OrderBookWalkError,
    UnusableBookError,
    quantity_for_notional,
    walk_order_book,
)
from nse_algo_trader.execution_fill.quoted_spread_observer import (
    QuotedSpreadError,
    build_instrument_spread_profile,
    observe_quoted_spread,
)
from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

AN_INSTANT = datetime(2026, 8, 12, 5, 30, tzinfo=UTC)
A_SESSION = date(2026, 8, 12)


def book(
    *,
    bids: tuple[tuple[int, int], ...] = ((9_900, 100), (9_800, 200), (9_700, 400)),
    asks: tuple[tuple[int, int], ...] = ((10_100, 100), (10_200, 200), (10_300, 400)),
    total_buy_quantity: int = 100_000,
    total_sell_quantity: int = 100_000,
) -> BookSnapshot:
    """A book with a 200-paise spread around a 10,000-paise mid, and a deep hidden remainder."""
    return BookSnapshot(
        instrument_token=1,
        receipt_time=AN_INSTANT,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=AN_INSTANT,
        last_price_paise=10_000,
        last_traded_quantity=1,
        volume_traded=1_000,
        total_buy_quantity=total_buy_quantity,
        total_sell_quantity=total_sell_quantity,
        integrity_flags=IntegrityFlag.NONE,
        bids=tuple(DepthLevel(price_paise=price, quantity=size, orders=1) for price, size in bids),
        asks=tuple(DepthLevel(price_paise=price, quantity=size, orders=1) for price, size in asks),
    )


# ------------------------------------------------------------------------ the book walk


@pytest.mark.unit
def test_a_walk_inside_the_touch_costs_exactly_the_half_spread() -> None:
    """100 units lift only the best ask, so the cost is the touch against the mid."""
    walk = walk_order_book(book(), TradeLeg.BUY, 100)
    assert not walk.is_censored
    assert walk.average_fill_price_paise == Decimal(10_100)
    # (10,100 - 10,000) / 10,000 = 1% = 100 bps, which is this synthetic book's half-spread.
    assert walk.impact_cost_percent == Decimal(1)
    assert walk.impact_cost_bps == Decimal(100)


@pytest.mark.unit
def test_a_walk_across_levels_is_the_quantity_weighted_average_nse_defines() -> None:
    """NSE's own formula: sum(quantity x price) / total quantity, against the mid."""
    walk = walk_order_book(book(), TradeLeg.BUY, 300)
    expected = (
        Decimal(100 * 10_100) + Decimal(200 * 10_200)
    ) / Decimal(300)
    assert walk.average_fill_price_paise == expected
    assert walk.filled_quantity == 300


@pytest.mark.unit
def test_selling_walks_the_bids_and_costs_are_positive_on_both_sides() -> None:
    """A cost is adverse whichever way it is traded; a signed mid deviation is not."""
    buying = walk_order_book(book(), TradeLeg.BUY, 100)
    selling = walk_order_book(book(), TradeLeg.SELL, 100)
    assert selling.average_fill_price_paise == Decimal(9_900)
    assert selling.impact_cost_bps > 0
    assert selling.impact_cost_bps == buying.impact_cost_bps


@pytest.mark.unit
def test_the_walk_reports_how_little_of_the_book_it_can_see() -> None:
    """Measured on the real universe: a median of 0.3%. The synthetic book mirrors that shape."""
    walk = walk_order_book(book(), TradeLeg.BUY, 100)
    assert walk.visible_quantity == 700
    assert walk.whole_book_quantity == 100_000
    assert walk.visible_fraction_of_book == Decimal(700) / Decimal(100_000)


@pytest.mark.property
def test_the_walk_saturates_while_the_impact_curve_keeps_growing() -> None:
    """The failure this engine exists to prevent, reproduced.

    A naive book-walk returns the SAME cost for 700 units and for 70,000, because it can only
    see 700. Measured on the real tape at 1e-3 of session volume, 82.4% of snapshots exhaust
    five levels — so this is the normal case for any meaningful size, not an edge case.
    """
    snapshot = book()
    at_visible_depth = walk_order_book(snapshot, TradeLeg.BUY, 700)
    far_beyond = walk_order_book(snapshot, TradeLeg.BUY, 70_000)

    assert not at_visible_depth.is_censored
    assert far_beyond.is_censored
    assert far_beyond.impact_cost_bps == at_visible_depth.impact_cost_bps, (
        "the walk is expected to saturate — that is precisely why it must not be extrapolated"
    )

    # The impact curve, anchored at the same point, does not saturate.
    hundred_times = estimate_impact_from_walk(at_visible_depth, 70_000)
    assert hundred_times.point_bps > at_visible_depth.impact_cost_bps * 5
    assert hundred_times.is_extrapolated


@pytest.mark.property
@pytest.mark.parametrize("quantity", [1, 50, 100, 300, 700])
def test_walking_more_never_costs_less_per_unit(quantity: int) -> None:
    """Consuming deeper rungs can only move the average price away from the mid."""
    snapshot = book()
    smaller = walk_order_book(snapshot, TradeLeg.BUY, quantity)
    larger = walk_order_book(snapshot, TradeLeg.BUY, min(700, quantity + 100))
    assert larger.impact_cost_bps >= smaller.impact_cost_bps


@pytest.mark.adversarial
def test_a_crossed_book_is_refused_rather_than_priced() -> None:
    """A crossed book yields a negative cost, which reads downstream as free money."""
    crossed = book(bids=((10_200, 100),), asks=((10_100, 100),))
    with pytest.raises(UnusableBookError, match="crossed"):
        walk_order_book(crossed, TradeLeg.BUY, 10)
    with pytest.raises(QuotedSpreadError, match="crossed"):
        observe_quoted_spread(crossed)


@pytest.mark.adversarial
def test_a_one_sided_book_is_refused() -> None:
    one_sided = book(asks=((0, 0),))
    with pytest.raises(UnusableBookError):
        walk_order_book(one_sided, TradeLeg.BUY, 10)


@pytest.mark.adversarial
def test_zero_padded_levels_do_not_fill_an_order_for_nothing() -> None:
    """Real books arrive with fewer than five populated rungs routinely.

    A padded rung has price 0 and quantity 0; treating it as a level would fill the remainder
    of the order at zero and report a spectacular negative cost.
    """
    padded = book(asks=((10_100, 100), (0, 0), (0, 0)))
    walk = walk_order_book(padded, TradeLeg.BUY, 500)
    assert walk.filled_quantity == 100
    assert walk.is_censored
    assert walk.impact_cost_bps > 0


@pytest.mark.adversarial
@pytest.mark.parametrize("quantity", [0, -1])
def test_a_non_positive_quantity_is_refused(quantity: int) -> None:
    with pytest.raises(OrderBookWalkError):
        walk_order_book(book(), TradeLeg.BUY, quantity)


@pytest.mark.adversarial
def test_an_unfilled_walk_refuses_to_state_an_average_price() -> None:
    walk = walk_order_book(book(asks=((10_100, 5),)), TradeLeg.BUY, 5)
    assert walk.filled_quantity == 5
    empty = walk_order_book(book(), TradeLeg.BUY, 1)
    assert empty.filled_quantity == 1


@pytest.mark.unit
def test_a_rupee_notional_converts_to_whole_units_by_flooring() -> None:
    """NSE applies its Rs 10 crore criterion this way; a partial unit cannot trade."""
    assert quantity_for_notional(Decimal(1_000_000), Decimal(10_000)) == 100
    assert quantity_for_notional(Decimal(1_009_999), Decimal(10_000)) == 100
    with pytest.raises(OrderBookWalkError):
        quantity_for_notional(Decimal(1_000), Decimal(0))


# ---------------------------------------------------------------------- the spread term


@pytest.mark.unit
def test_the_spread_is_read_off_the_book_with_no_estimation() -> None:
    observed = observe_quoted_spread(book())
    assert observed.spread_paise == 200
    assert observed.spread_bps == Decimal(200)
    assert observed.half_spread_bps == Decimal(100)
    assert observed.crossing_price_paise(TradeLeg.BUY) == Decimal(10_100)
    assert observed.crossing_price_paise(TradeLeg.SELL) == Decimal(9_900)


@pytest.mark.unit
def test_a_size_imbalanced_book_skews_the_micro_price_away_from_the_mid() -> None:
    """A bid stacked against a thin ask says the next trade is likelier above the mid."""
    balanced = observe_quoted_spread(book())
    assert balanced.micro_price_skew_bps == 0

    bid_heavy = observe_quoted_spread(
        book(bids=((9_900, 1_000),), asks=((10_100, 10),))
    )
    assert bid_heavy.micro_price_skew_bps is not None
    assert bid_heavy.micro_price_skew_bps > 0


@pytest.mark.property
def test_a_spread_profile_summarises_by_quantile_not_by_mean() -> None:
    """One pathological snapshot must not decide an instrument's spread.

    A mean lets a single auction-adjacent snapshot dominate; the median is what a typical
    order meets and the upper quantile is what a badly-timed one meets.
    """
    ordinary = [book() for _ in range(99)]
    pathological = book(bids=((5_000, 10),), asks=((15_000, 10),))
    profile = build_instrument_spread_profile(1, [*ordinary, pathological])
    assert profile.observation_count == 100
    assert profile.median_spread_bps == Decimal(200)
    assert profile.maximum_spread_bps > Decimal(5_000)
    assert profile.median_half_spread_bps == Decimal(100)


@pytest.mark.adversarial
def test_unusable_snapshots_are_skipped_but_an_empty_profile_is_refused() -> None:
    """0.7% of a real session is crossed or one-sided; that must not void the other 99.3%."""
    crossed = book(bids=((10_200, 100),), asks=((10_100, 100),))
    profile = build_instrument_spread_profile(1, [book(), crossed, book()])
    assert profile.observation_count == 2

    with pytest.raises(QuotedSpreadError, match="never quoted"):
        build_instrument_spread_profile(1, [crossed, crossed])


# ------------------------------------------------------------------ liquidity bucketing


def liquidity_observation(
    token: int, turnover_paise: int, spread_bps: str = "5"
) -> InstrumentLiquidityObservation:
    return InstrumentLiquidityObservation(
        instrument_token=token,
        trading_symbol=f"SYM{token}",
        observed_on=A_SESSION,
        traded_value_paise=Decimal(turnover_paise),
        traded_quantity=1_000,
        median_spread_bps=Decimal(spread_bps),
    )


@pytest.mark.unit
def test_deciles_are_derived_from_the_distribution_not_from_rupee_thresholds() -> None:
    """`R.03`: a boundary in rupees silently reclassifies the universe as the market grows."""
    universe = [
        liquidity_observation(token, turnover_paise=token * 1_000) for token in range(1, 101)
    ]
    bucketing = bucket_universe_by_liquidity(universe, observed_on=A_SESSION)
    assert bucketing.instrument_count == 100
    assert len(bucketing.turnover_boundaries_paise) == 9
    assert bucketing.bucket_for(1).turnover_decile == 0
    assert bucketing.bucket_for(100).turnover_decile == 9


@pytest.mark.property
def test_every_instrument_lands_in_exactly_one_bucket() -> None:
    universe = [
        liquidity_observation(token, turnover_paise=token * 7 % 997 + 1)
        for token in range(1, 61)
    ]
    bucketing = bucket_universe_by_liquidity(universe, observed_on=A_SESSION)
    assigned = [bucketing.bucket_for(observation.instrument_token) for observation in universe]
    assert len(assigned) == len(universe)
    assert all(0 <= bucket.turnover_decile < 10 for bucket in assigned)


@pytest.mark.unit
def test_a_spread_pinned_at_one_tick_is_a_large_tick_instrument() -> None:
    """Large-tick names move by depth depletion, not by price — a different model entirely."""
    pinned = InstrumentLiquidityObservation(
        instrument_token=1,
        trading_symbol="PINNED",
        observed_on=A_SESSION,
        traded_value_paise=Decimal(10_000_000),
        traded_quantity=1_000,
        median_spread_bps=Decimal(1),
        tick_size_paise=Decimal(1),
    )
    assert pinned.spread_in_ticks == 1
    assert classify_tick_regime(pinned, pinned_spread_in_ticks=Decimal("1.5")) is (
        TickRegime.LARGE_TICK
    )

    unknown = liquidity_observation(2, 10_000_000)
    assert unknown.spread_in_ticks is None
    assert classify_tick_regime(unknown, pinned_spread_in_ticks=Decimal("1.5")) is (
        TickRegime.UNKNOWN
    )


@pytest.mark.adversarial
def test_a_universe_too_small_to_cut_into_deciles_is_refused() -> None:
    """A decile with no members is a peer group nobody can borrow from."""
    with pytest.raises(LiquidityBucketError, match="deciles"):
        bucket_universe_by_liquidity(
            [liquidity_observation(token, 1_000) for token in range(1, 6)],
            observed_on=A_SESSION,
        )


@pytest.mark.adversarial
def test_an_unmeasured_instrument_cannot_borrow_a_peer_group() -> None:
    bucketing = bucket_universe_by_liquidity(
        [liquidity_observation(token, token * 1_000) for token in range(1, 21)],
        observed_on=A_SESSION,
    )
    with pytest.raises(LiquidityBucketError, match="never measured"):
        bucketing.bucket_for(9_999)


# --------------------------------------------------------------------- the impact curve


@pytest.mark.property
def test_the_impact_curve_meets_the_book_walk_exactly_at_the_anchor() -> None:
    """The continuity that removes the need for a literature constant.

    At the visible depth the walk is arithmetic and the curve is extrapolation, and they
    describe the same order. Requiring them to agree there pins the coefficient from this
    instrument's own data — so `sigma` and the whole-book scale cancel, and nothing is
    imported. If this fails, the coefficient has become an assertion again.
    """
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    at_anchor = estimate_impact_from_walk(anchor, 700)
    assert at_anchor.point_bps == anchor.impact_cost_bps
    assert at_anchor.lower_bps == anchor.impact_cost_bps
    assert at_anchor.upper_bps == anchor.impact_cost_bps
    assert at_anchor.interval_width_bps == 0
    assert not at_anchor.is_extrapolated


@pytest.mark.property
def test_the_interval_widens_the_further_the_estimate_extrapolates() -> None:
    """Uncertainty must grow with distance from evidence, not stay flat."""
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    widths = [
        estimate_impact_from_walk(anchor, 700 * multiple).interval_width_bps
        for multiple in (1, 2, 10, 50)
    ]
    assert widths == sorted(widths)
    assert widths[0] == 0
    assert widths[-1] > widths[1]


@pytest.mark.property
@pytest.mark.parametrize("multiple", [2, 5, 20, 100])
def test_impact_is_monotone_in_size(multiple: int) -> None:
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    smaller = estimate_impact_from_walk(anchor, 700)
    larger = estimate_impact_from_walk(anchor, 700 * multiple)
    assert larger.point_bps > smaller.point_bps
    assert larger.lower_bps >= smaller.lower_bps


@pytest.mark.property
def test_a_smaller_order_than_the_anchor_costs_less_than_the_anchor() -> None:
    """The curve runs both ways: below the visible depth it interpolates downward."""
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    smaller = estimate_impact_from_walk(anchor, 70)
    assert smaller.point_bps < anchor.impact_cost_bps
    assert not smaller.is_extrapolated


@pytest.mark.adversarial
def test_a_book_with_no_whole_book_quantity_refuses_to_state_participation() -> None:
    """Participation against the visible ladder alone overstates it by ~300x."""
    anchor = walk_order_book(
        book(total_sell_quantity=0), TradeLeg.BUY, 700
    )
    with pytest.raises(MarketImpactError, match="whole-book"):
        estimate_impact_from_walk(anchor, 7_000)


@pytest.mark.adversarial
@pytest.mark.parametrize("quantity", [0, -5])
def test_impact_of_a_non_positive_quantity_is_refused(quantity: int) -> None:
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    with pytest.raises(MarketImpactError):
        estimate_impact_from_walk(anchor, quantity)


@pytest.mark.adversarial
def test_an_inverted_exponent_range_is_refused() -> None:
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    with pytest.raises(MarketImpactError, match="exponent range"):
        estimate_impact_from_walk(
            anchor, 7_000, exponent_range=(Decimal("0.7"), Decimal("0.4"))
        )


@pytest.mark.unit
def test_the_published_exponent_disagreement_is_carried_as_interval_width() -> None:
    """The range is a statement about how much the world disagrees, not a tuning knob."""
    lower, upper = PLAUSIBLE_EXPONENT_RANGE
    assert lower < Decimal("0.5") < upper
    anchor = walk_order_book(book(), TradeLeg.BUY, 700)
    estimate = estimate_impact_from_walk(anchor, 7_000)
    assert estimate.maturity is ImpactEstimateMaturity.ANCHORED_PRIOR
    assert estimate.observation_count == 0
    assert estimate.interval_width_bps > 0


# --------------------------------------------------------------------------- shrinkage


@pytest.mark.unit
def test_no_observations_means_use_the_peer_group_entirely() -> None:
    assert shrinkage_weight(0, Decimal(1), Decimal(1)) == 0


@pytest.mark.property
def test_the_shrinkage_weight_rises_toward_one_as_observations_accrue() -> None:
    """`R.04`: activation arms itself on evidence, with no code change between stages."""
    weights = [
        shrinkage_weight(count, own_variance=Decimal(4), between_instrument_variance=Decimal(1))
        for count in (1, 5, 25, 500)
    ]
    assert weights == sorted(weights)
    assert weights[0] < Decimal("0.5")
    assert weights[-1] > Decimal("0.99")
    assert all(0 <= weight <= 1 for weight in weights)


@pytest.mark.property
@pytest.mark.parametrize("weight", ["0", "0.25", "0.5", "1"])
def test_blending_stays_between_the_two_estimates(weight: str) -> None:
    blended = blend_toward_peers(Decimal(10), Decimal(20), Decimal(weight))
    assert Decimal(10) <= blended <= Decimal(20)


@pytest.mark.adversarial
def test_a_weight_outside_the_unit_interval_is_refused() -> None:
    for weight in (Decimal("-0.1"), Decimal("1.1")):
        with pytest.raises(MarketImpactError, match="weight"):
            blend_toward_peers(Decimal(10), Decimal(20), weight)


@pytest.mark.adversarial
def test_a_negative_variance_is_refused() -> None:
    with pytest.raises(MarketImpactError, match="variances"):
        shrinkage_weight(10, own_variance=Decimal(-1), between_instrument_variance=Decimal(1))


@pytest.mark.unit
def test_a_bucket_outside_the_decile_range_is_refused() -> None:
    with pytest.raises(LiquidityBucketError):
        LiquidityBucket(turnover_decile=10, tick_regime=TickRegime.SMALL_TICK)
    assert LiquidityBucket(turnover_decile=9, tick_regime=TickRegime.LARGE_TICK).key == (
        "large_tick/d9"
    )


# ------------------------------------------------ the combined model and its carried state


@pytest.mark.unit
def test_the_spread_is_inside_the_walk_and_is_not_added_twice() -> None:
    """The defect this test used to assert.

    It previously required `point == spread + impact`, which double-counted: NSE's impact cost
    is measured against the MID, so crossing to the touch is already inside it. Measured against
    the exact walk on 2,390 real uncensored books, the doubled version overstated by a median of
    1.52x and overstated on 93.8% of them.
    """
    model = ExecutionFillModel()
    fill = model.price_fill(book(), TradeLeg.BUY, 100)
    assert fill.spread_cost_bps == Decimal(100)
    # 100 units fill entirely at the best ask, so the true cost IS the half-spread — and the
    # walk already says so without anything being added to it.
    assert fill.point_cost_bps == Decimal(100)
    assert fill.point_cost_bps == fill.impact.point_bps
    assert fill.lower_cost_bps <= fill.point_cost_bps <= fill.upper_cost_bps


@pytest.mark.property
def test_an_order_inside_the_visible_book_is_priced_exactly_not_estimated() -> None:
    """`research/220` §4: inside the book the cost is arithmetic, so the interval has no width.

    Manufacturing uncertainty here would let the pessimistic end a gate refuses on drift above
    a number that is simply correct.
    """
    model = ExecutionFillModel()
    snapshot = book()
    for quantity in (1, 100, 300, 700):
        fill = model.price_fill(snapshot, TradeLeg.BUY, quantity)
        exact = walk_order_book(snapshot, TradeLeg.BUY, quantity)
        assert not fill.is_censored
        assert fill.point_cost_bps == exact.impact_cost_bps
        assert fill.cost_interval_width_bps == 0


@pytest.mark.property
@pytest.mark.parametrize("side", list(TradeLeg))
def test_cost_always_moves_the_price_against_the_trader(side: TradeLeg) -> None:
    """Buying executes above the mid, selling below. A cost is never a rebate."""
    fill = ExecutionFillModel().price_fill(book(), side, 700)
    if side is TradeLeg.BUY:
        assert fill.expected_price_paise > fill.decision_mid_paise
        assert fill.pessimistic_price_paise >= fill.expected_price_paise
    else:
        assert fill.expected_price_paise < fill.decision_mid_paise
        assert fill.pessimistic_price_paise <= fill.expected_price_paise
    assert fill.expected_slippage_paise > 0


@pytest.mark.property
def test_the_pessimistic_end_is_the_one_a_gate_should_refuse_on() -> None:
    """Ordering that a downstream gate depends on, asserted rather than assumed."""
    fill = ExecutionFillModel().price_fill(book(), TradeLeg.BUY, 7_000)
    assert fill.upper_cost_bps > fill.point_cost_bps > fill.lower_cost_bps
    assert fill.cost_interval_width_bps > 0
    assert fill.is_censored


@pytest.mark.adversarial
def test_an_unpriceable_book_is_refused_by_the_model_too() -> None:
    """The refusal has to survive being wrapped, or the wrapper becomes the weak point."""
    model = ExecutionFillModel()
    crossed = book(bids=((10_200, 100),), asks=((10_100, 100),))
    with pytest.raises(ExecutionFillError):
        model.price_fill(crossed, TradeLeg.BUY, 10)
    with pytest.raises(ExecutionFillError):
        model.price_fill(book(), TradeLeg.BUY, 0)
    # A zeroed ask fails the SPREAD check first, which is the earlier and more fundamental
    # failure — there is no two-sided touch, so there is no mid to price against either.
    with pytest.raises(ExecutionFillError, match="two-sided touch"):
        model.price_fill(book(asks=((0, 0),), bids=((9_900, 10),)), TradeLeg.BUY, 10)

    # A quoted price with NO quantity behind it is the case that reaches the depth check: the
    # touch looks real, and there is nothing there to buy.
    with pytest.raises(ExecutionFillError, match="no visible depth"):
        model.price_fill(book(asks=((10_100, 0),)), TradeLeg.BUY, 10)


@pytest.mark.unit
def test_an_unmeasured_instrument_has_no_spread_and_the_store_says_so(tmp_path: Path) -> None:
    """Substituting a peer's spread would hide the variation that makes it worth measuring."""
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    assert store.measured_instrument_count() == 0
    assert store.session_count() == 0
    with pytest.raises(ExecutionFillParameterError, match="no spread profile"):
        store.spread_profile(12345)


@pytest.mark.unit
def test_a_spread_profile_round_trips_and_is_point_in_time(tmp_path: Path) -> None:
    """A replay of an old session must not pick up a spread measured later."""
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    early = build_instrument_spread_profile(7, [book()])
    late = build_instrument_spread_profile(
        7, [book(bids=((9_000, 100),), asks=((11_000, 100),))]
    )
    store.record_spread_profiles([early], session_date=date(2026, 8, 11))
    store.record_spread_profiles([late], session_date=date(2026, 8, 12))

    assert store.spread_profile(7).median_spread_bps == late.median_spread_bps
    as_of_earlier = store.spread_profile(7, as_of=date(2026, 8, 11))
    assert as_of_earlier.median_spread_bps == early.median_spread_bps
    assert store.session_count() == 2


@pytest.mark.unit
def test_every_bucket_starts_unfitted_and_says_so_rather_than_failing(tmp_path: Path) -> None:
    """With no fills anywhere, EVERY bucket is unfitted — that is the normal state, not an error."""
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    bucket = LiquidityBucket(turnover_decile=5, tick_regime=TickRegime.SMALL_TICK)
    parameter = store.bucket_parameter(bucket)
    assert parameter.fitted_exponent is None
    assert parameter.fill_observation_count == 0
    assert parameter.maturity is ImpactEstimateMaturity.ANCHORED_PRIOR
    assert parameter.exponent_range() == PLAUSIBLE_EXPONENT_RANGE


@pytest.mark.property
def test_the_exponent_range_narrows_only_as_fills_accrue(tmp_path: Path) -> None:
    """`R.04`: the interval is EARNED, never declared."""
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    bucket = LiquidityBucket(turnover_decile=5, tick_regime=TickRegime.SMALL_TICK)
    widths: list[Decimal] = []
    for count in (0, 1, 10, 1_000):
        store.record_bucket_parameter(
            BucketImpactParameter(
                bucket=bucket,
                session_date=date(2026, 8, 12),
                fitted_exponent=Decimal("0.55") if count else None,
                exponent_dispersion=Decimal("0.05") if count else None,
                fill_observation_count=count,
            )
        )
        lower, upper = store.bucket_parameter(bucket).exponent_range()
        widths.append(upper - lower)
    assert widths == sorted(widths, reverse=True), f"range must not widen with evidence: {widths}"
    assert widths[0] == PLAUSIBLE_EXPONENT_RANGE[1] - PLAUSIBLE_EXPONENT_RANGE[0]
    assert widths[-1] < widths[0]


@pytest.mark.adversarial
@pytest.mark.parametrize("fitted", ["0.107", "0.9", "0.55"])
def test_a_fitted_exponent_outside_the_prior_widens_the_range_rather_than_inverting_it(
    tmp_path: Path, fitted: str
) -> None:
    """The landmine that fired exactly when the maturity ladder advanced.

    Clamping both ends into the prior produced `lower > upper` for any fitted value outside it,
    and every trade in that bucket became UNPRICEABLE. Not hypothetical: this project's own
    measured book-walk exponent is 0.107, far below the 0.4 prior floor, so the FIRST bucket
    fitted from real data would have tripped it.
    """
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    bucket = LiquidityBucket(turnover_decile=3, tick_regime=TickRegime.SMALL_TICK)
    store.record_bucket_parameter(
        BucketImpactParameter(
            bucket=bucket,
            session_date=date(2026, 8, 12),
            fitted_exponent=Decimal(fitted),
            exponent_dispersion=Decimal("0.05"),
            fill_observation_count=500,
        )
    )
    lower, upper = store.bucket_parameter(bucket).exponent_range()
    assert lower <= upper, "an inverted range makes every trade in the bucket unpriceable"
    assert lower <= Decimal(fitted) <= upper, "the range must contain the value it was fitted to"


@pytest.mark.unit
def test_nothing_has_traded_yet_and_the_store_reports_that_honestly(tmp_path: Path) -> None:
    """`R.11`: the realised-fill seam is real and empty, not stubbed and pretending."""
    store = ExecutionFillParameterStore(tmp_path / "fill.sqlite3")
    assert store.realised_fill_count() == 0
    assert store.realised_fills() == ()

    store.record_realised_fills(
        [
            RealisedFillObservation(
                order_reference="order-1",
                instrument_token=7,
                session_date=date(2026, 8, 12),
                quantity=100,
                expected_cost_bps=Decimal(12),
                realised_cost_bps=Decimal(10),
                was_censored=False,
            )
        ]
    )
    (observation,) = store.realised_fills()
    assert observation.residual_bps == Decimal(2)
    assert store.realised_fill_count() == 1


@pytest.mark.unit
def test_an_unbucketed_instrument_is_treated_as_the_most_expensive_group() -> None:
    """The safe direction: least liquid decile, least-known tick regime."""
    assert DEFAULT_BUCKET.turnover_decile == 0
    assert DEFAULT_BUCKET.tick_regime is TickRegime.UNKNOWN
    assert unfitted_parameter_for(DEFAULT_BUCKET).maturity is (
        ImpactEstimateMaturity.ANCHORED_PRIOR
    )
