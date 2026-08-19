"""`L9.14` — the expression selector, tested as an optimiser rather than as a preference table.

The defects these tests exist for are all silent ones. A MARKET order that slips through is a
regulatory category change, not an error message. A passive candidate scored with an invented fill
probability is the cheapest expression winning every backtest ever run against this engine. A
crossed book divided by zero is a negative cost, which reads as free money. None of those crash.

Where a test asserts on a *direction* rather than on a number, that is deliberate (`R.03`): the
engine derives its own answers from the book and the tape, so an expected constant would only be
re-asserting whatever the implementation happened to compute today. What must hold is the SHAPE —
a short horizon must move the choice toward aggression, a thin book toward a smaller footprint.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nse_algo_trader.execution_fill.execution_fill_model import ExecutionFillModel
from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderType,
    OrderVariety,
)
from nse_algo_trader.order_path.order_expression_selector import (
    BookUnpriceableError,
    DepthTapeTouchExecutionObserver,
    ExpressionFamily,
    ExpressionSelectionRequest,
    NoFeasibleExpressionError,
    OrderExpressionSelector,
    ScoredExpression,
    TouchExecutionRate,
    _ranking_key,
    forecast_resting_fill,
)
from nse_algo_trader.order_path.order_record import OrderExpression
from nse_algo_trader.order_path.trading_intent import TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    default_transaction_cost_engine,
)

LIVE_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")

# 11:00 IST on a real 2026 trading Wednesday — inside the continuous session by every phase rule.
MIDDAY = datetime(2026, 8, 12, 5, 30, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 8, 12, 13, 30, tzinfo=UTC)
A_TOKEN = 738561


# --------------------------------------------------------------------------- fixtures


def book(
    *,
    bids: tuple[tuple[int, int], ...] = ((9_990, 500), (9_980, 800), (9_970, 1_200)),
    asks: tuple[tuple[int, int], ...] = ((10_010, 500), (10_020, 800), (10_030, 1_200)),
    total_buy_quantity: int = 400_000,
    total_sell_quantity: int = 400_000,
    token: int = A_TOKEN,
) -> BookSnapshot:
    """A 20-bps spread around a 10,000-paise mid, deep enough to fill an ordinary clip."""
    return BookSnapshot(
        instrument_token=token,
        receipt_time=MIDDAY,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=MIDDAY,
        last_price_paise=10_000,
        last_traded_quantity=1,
        volume_traded=100_000,
        total_buy_quantity=total_buy_quantity,
        total_sell_quantity=total_sell_quantity,
        integrity_flags=IntegrityFlag.NONE,
        bids=tuple(DepthLevel(price_paise=price, quantity=size, orders=3) for price, size in bids),
        asks=tuple(DepthLevel(price_paise=price, quantity=size, orders=3) for price, size in asks),
    )


def thin_book() -> BookSnapshot:
    """Twenty units a rung. Any real clip runs off the end of this ladder."""
    return book(
        bids=((9_990, 20), (9_980, 20), (9_970, 20)),
        asks=((10_010, 20), (10_020, 20), (10_030, 20)),
        total_buy_quantity=4_000,
        total_sell_quantity=4_000,
    )


def intent(
    *,
    quantity: int = 200,
    horizon_minutes: int | None = 30,
    side: TradeLeg = TradeLeg.BUY,
    edge_bps: str = "60",
    decided_at: datetime = MIDDAY,
    segment: ChargeableSegment = ChargeableSegment.EQUITY_INTRADAY,
) -> TradingIntent:
    return TradingIntent(
        strategy_identity="intraday_mean_reversion",
        instrument_token=A_TOKEN,
        trading_symbol="TESTSYM",
        segment=segment,
        side=side,
        quantity=quantity,
        decided_at=decided_at,
        reference_price_paise=Decimal(10_000),
        expected_edge_bps=Decimal(edge_bps),
        horizon_minutes=horizon_minutes,
    )


class FixedSessionPhase:
    """A hermetic session clock (`R.10`): the calendar is not what these tests are about."""

    def __init__(self, *, is_open: bool) -> None:
        self._is_open = is_open

    def is_continuous_session_open(self, at: datetime) -> bool:
        return self._is_open


class FixedTouchRate:
    """A measured-rate stand-in, so tests can vary the ONE quantity passive fills depend on."""

    def __init__(self, quantity_per_minute: str, *, is_upper_bound: bool = True) -> None:
        self._rate = TouchExecutionRate(
            quantity_per_minute=Decimal(quantity_per_minute),
            observed_minutes=Decimal(120),
            transition_count=5_000,
            executed_quantity=1,
            is_upper_bound=is_upper_bound,
            evidence="hermetic fixture standing in for L0.22's queue-depletion decomposition",
        )

    def observe_touch_execution_rate(
        self, *, instrument_token: int, resting_side: TradeLeg, as_of: datetime
    ) -> TouchExecutionRate:
        return self._rate


def selector(
    *,
    touch_rate: str | None = None,
    session_open: bool = True,
) -> OrderExpressionSelector:
    return OrderExpressionSelector(
        default_transaction_cost_engine(),
        ExecutionFillModel(),
        touch_queue_observer=None if touch_rate is None else FixedTouchRate(touch_rate),
        session_phase=FixedSessionPhase(is_open=session_open),
    )


def request(**overrides: object) -> ExpressionSelectionRequest:
    fields: dict[str, object] = {"intent": intent(), "snapshot": book()}
    fields.update(overrides)
    return ExpressionSelectionRequest(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the bar


@pytest.mark.unit
def test_no_market_candidate_is_ever_produced_and_the_refusal_names_the_circular() -> None:
    """`docs/research/223` §5 — the one fact in the file that changes what may be built."""
    choice = selector().select(request())
    for candidate in choice.scored:
        assert candidate.expression.order_type is not OrderType.MARKET, (
            f"{candidate.family} produced a market order, which no algo-originated flow may carry"
        )
    assert choice.chosen.order_type is not OrderType.MARKET

    refusals = [entry for entry in choice.excluded if entry.family is ExpressionFamily.MARKET_TAKE]
    assert len(refusals) == 1, "the market order must be refused explicitly, not merely absent"
    refusal = refusals[0]
    assert "NSE/MSD/67753" in refusal.source
    assert "Market Order are not permitted" in refusal.source
    # The refusal reaches the operator through the chosen order itself, not only through a field.
    assert "market_take" in choice.chosen.chosen_because


@pytest.mark.unit
def test_the_refusal_survives_into_the_chosen_orders_own_reason_string() -> None:
    """`chosen_because` is what an audit reads. A refusal only in a sibling field is invisible."""
    choice = selector(touch_rate="200").select(request())
    assert choice.chosen.chosen_because
    assert "beat" in choice.chosen.chosen_because or "only expression" in (
        choice.chosen.chosen_because
    )
    assert "Refused:" in choice.chosen.chosen_because


@pytest.mark.unit
def test_a_large_clip_against_a_thin_book_avoids_the_single_deep_sweep() -> None:
    """The clip is ten times the visible ladder, so a whole-clip sweep is an extrapolation.

    The engine is not told to prefer icebergs. It scores on the pessimistic end of a cost interval
    that BLOWS UP when the walk is censored, and an iceberg's legs are sized to stay inside the
    visible book — so the split wins on arithmetic, or a smaller-footprint expression does.
    """
    thin = thin_book()
    choice = selector(touch_rate="40").select(
        ExpressionSelectionRequest(intent=intent(quantity=600), snapshot=thin)
    )
    assert choice.chosen_family in (
        ExpressionFamily.ICEBERG_AGGRESSIVE,
        ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH,
        ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE,
    ), f"a 600-unit clip against a 60-unit ladder chose {choice.chosen_family}"
    aggressive_day = _by_family(choice.scored, ExpressionFamily.AGGRESSIVE_LIMIT_DAY)
    assert aggressive_day is not None
    winner = choice.winner
    assert winner is not None
    assert winner.expected_net_edge_bps >= aggressive_day.expected_net_edge_bps


@pytest.mark.unit
def test_a_small_clip_against_a_deep_book_is_not_split_into_an_iceberg() -> None:
    """Splitting a clip the ladder can already absorb only multiplies per-order brokerage."""
    choice = selector(touch_rate="500").select(
        ExpressionSelectionRequest(intent=intent(quantity=100), snapshot=book())
    )
    assert choice.chosen_family is not ExpressionFamily.ICEBERG_AGGRESSIVE
    assert choice.chosen.iceberg_legs is None
    excluded = {entry.family: entry for entry in choice.excluded}
    assert ExpressionFamily.ICEBERG_AGGRESSIVE in excluded
    assert "fits inside" in excluded[ExpressionFamily.ICEBERG_AGGRESSIVE].reason


@pytest.mark.unit
def test_the_horizon_moves_the_choice_between_aggression_and_patience() -> None:
    """Urgency is DERIVED from `horizon_minutes`; nothing about it is tabulated.

    Asserted as a direction on the same book and the same measured queue rate: only the intent's
    own statement of how long its edge lasts changes, and the ranking of patience against
    aggression must move with it.
    """
    deep = book()
    patient = selector(touch_rate="60").select(
        ExpressionSelectionRequest(intent=intent(horizon_minutes=240), snapshot=deep)
    )
    hurried = selector(touch_rate="60").select(
        ExpressionSelectionRequest(intent=intent(horizon_minutes=2), snapshot=deep)
    )

    patient_passive = _by_family(patient.scored, ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH)
    hurried_passive = _by_family(hurried.scored, ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH)
    assert patient_passive is not None and hurried_passive is not None

    assert patient_passive.fill_probability > hurried_passive.fill_probability, (
        "a longer horizon must make a resting order MORE likely to fill inside it"
    )
    assert patient_passive.expected_net_edge_bps > hurried_passive.expected_net_edge_bps, (
        "patience must be worth more when there is time for it"
    )
    assert patient.chosen_family is ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH, (
        "with four hours and a measured queue, resting and earning the half-spread wins"
    )
    assert hurried.chosen_family is not ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH, (
        "with two minutes, an order that probably will not fill cannot be the best expression"
    )


@pytest.mark.unit
def test_passive_candidates_are_excluded_visibly_when_no_fill_estimate_exists() -> None:
    """The defect this rule exists to stop: a made-up fill probability nobody can see."""
    choice = selector(touch_rate=None).select(request())
    assert ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH not in {
        candidate.family for candidate in choice.scored
    }
    excluded = {entry.family: entry for entry in choice.excluded}
    assert ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH in excluded
    reason = excluded[ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH].reason
    assert "no measured fill probability" in reason
    assert "will not substitute" in reason
    assert "passive_limit_at_touch" in choice.chosen.chosen_because


@pytest.mark.unit
def test_a_horizonless_intent_also_loses_its_passive_candidate() -> None:
    """Without a deadline, waiting is scored as free — so it is refused, never defaulted."""
    choice = selector(touch_rate="500").select(
        ExpressionSelectionRequest(intent=intent(horizon_minutes=None), snapshot=book())
    )
    excluded = {entry.family: entry.reason for entry in choice.excluded}
    assert "states no horizon" in excluded[ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH]


@pytest.mark.unit
def test_the_chosen_expression_always_validates_as_a_constructible_order_expression() -> None:
    """`OrderExpression.__post_init__` IS the wire contract; a choice failing it is unsendable."""
    for touch_rate in (None, "10", "5000"):
        for quantity in (10, 200, 5_000):
            for side in (TradeLeg.BUY, TradeLeg.SELL):
                choice = selector(touch_rate=touch_rate).select(
                    ExpressionSelectionRequest(
                        intent=intent(quantity=quantity, side=side), snapshot=book()
                    )
                )
                rebuilt = OrderExpression(
                    variety=choice.chosen.variety,
                    product=choice.chosen.product,
                    order_type=choice.chosen.order_type,
                    validity=choice.chosen.validity,
                    limit_price_paise=choice.chosen.limit_price_paise,
                    trigger_price_paise=choice.chosen.trigger_price_paise,
                    disclosed_quantity=choice.chosen.disclosed_quantity,
                    iceberg_legs=choice.chosen.iceberg_legs,
                )
                assert rebuilt.order_type is OrderType.LIMIT
                assert rebuilt.limit_price_paise is not None


@pytest.mark.unit
def test_the_marketable_limit_reaches_through_the_touch_on_both_sides() -> None:
    """A "marketable" limit that does not cross is a passive order wearing the wrong name."""
    for side, touch in ((TradeLeg.BUY, 10_010), (TradeLeg.SELL, 9_990)):
        choice = selector().select(
            ExpressionSelectionRequest(intent=intent(side=side, quantity=400), snapshot=book())
        )
        aggressive = _by_family(choice.scored, ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE)
        assert aggressive is not None
        price = aggressive.expression.limit_price_paise
        assert price is not None
        if side is TradeLeg.BUY:
            assert price >= touch
        else:
            assert price <= touch


@pytest.mark.unit
def test_the_derived_tolerance_widens_with_the_clip_rather_than_being_a_typed_percentage() -> None:
    """`R.03`: the cross-through distance comes from the walk, so it must move with the walk."""
    small = selector().select(
        ExpressionSelectionRequest(intent=intent(quantity=100), snapshot=book())
    )
    large = selector().select(
        ExpressionSelectionRequest(intent=intent(quantity=1_500), snapshot=book())
    )
    small_price = _price_of(small, ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE)
    large_price = _price_of(large, ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE)
    assert large_price > small_price, "a bigger clip must reach further into the book"


@pytest.mark.unit
def test_an_iceberg_leg_count_comes_from_the_ladder_and_the_expression_carries_it() -> None:
    """Leg count is `ceil(clip / visible)`; sixty visible units and a 600 clip means ten legs."""
    choice = selector(touch_rate="40").select(
        ExpressionSelectionRequest(intent=intent(quantity=600), snapshot=thin_book())
    )
    iceberg = _by_family(choice.scored, ExpressionFamily.ICEBERG_AGGRESSIVE)
    assert iceberg is not None
    assert iceberg.expression.iceberg_legs == 10
    assert iceberg.expression.disclosed_quantity == 60
    assert iceberg.order_count == 10
    assert iceberg.statutory_cost_bps > 0


@pytest.mark.unit
@pytest.mark.adversarial
def test_every_iceberg_leg_count_divides_its_clip_so_the_venue_can_encode_it() -> None:
    """`M/2` — the selector used to propose icebergs the wire provably could not carry.

    The reproduction, verbatim: a 100-unit clip against a 45-unit visible ladder gave
    `ceil(100 / 45) = 3` legs, and `100 % 3 == 1`. Kite takes a per-LEG quantity, so
    `kite_order_execution_venue._iceberg_quantity_for` refuses the order outright rather than
    rounding a leg off its own initiative — correctly, because the rounding changes the size
    actually sent. The order therefore died AT SUBMISSION, after every cost in it had been priced
    and after it had won the ranking, on roughly two leg counts in three.

    Asserted as the venue's own arithmetic (`quantity % legs == 0`) rather than as an expected leg
    count, so this test keeps meaning the same thing if the ladder, the scorer or the leg search
    changes. It is swept over a range of clips and depths because the defect was arithmetic and a
    single pair of numbers would only prove that one pair is now safe.
    """
    for visible_per_rung in (15, 20, 45):
        for quantity in (100, 120, 175, 200, 360, 600, 1_000):
            thin = book(
                bids=tuple((price, visible_per_rung) for price in (9_990, 9_980, 9_970)),
                asks=tuple((price, visible_per_rung) for price in (10_010, 10_020, 10_030)),
                total_buy_quantity=visible_per_rung * 100,
                total_sell_quantity=visible_per_rung * 100,
            )
            choice = selector(touch_rate="40").select(
                ExpressionSelectionRequest(intent=intent(quantity=quantity), snapshot=thin)
            )
            iceberg = _by_family(choice.scored, ExpressionFamily.ICEBERG_AGGRESSIVE)
            if iceberg is None:
                # No candidate at all is the OTHER half of the fix: where no leg count both fits
                # the ladder and divides the clip, the family is excluded WITH ITS REASON rather
                # than emitted and refused at the wire.
                excluded = {entry.family: entry for entry in choice.excluded}
                assert ExpressionFamily.ICEBERG_AGGRESSIVE in excluded, (
                    f"clip {quantity} against {visible_per_rung * 3} visible produced neither an "
                    f"iceberg candidate nor a recorded exclusion — a silently dropped family"
                )
                assert excluded[ExpressionFamily.ICEBERG_AGGRESSIVE].reason
                continue
            legs = iceberg.expression.iceberg_legs
            assert legs is not None
            assert quantity % legs == 0, (
                f"a {quantity}-unit clip across {legs} legs does not divide evenly, which is "
                f"exactly what the venue refuses to encode"
            )
            assert iceberg.expression.disclosed_quantity == quantity // legs
            # And the leg must still fit inside what the snapshot can actually see, which is the
            # constraint the leg count existed for in the first place.
            assert quantity // legs <= visible_per_rung * 3


@pytest.mark.unit
@pytest.mark.adversarial
def test_a_clip_no_leg_count_can_divide_yields_no_iceberg_rather_than_an_unsendable_one() -> None:
    """A prime clip larger than the ladder has no encodable split at all, and must say so.

    101 units is prime, so no leg count between two and the facility's fifty divides it; every
    conceivable iceberg for it needs a fractional leg. The right answer is no candidate plus a
    recorded reason — not a candidate that wins the ranking and is then refused at submission.
    """
    choice = selector(touch_rate="40").select(
        ExpressionSelectionRequest(intent=intent(quantity=101), snapshot=thin_book())
    )
    assert _by_family(choice.scored, ExpressionFamily.ICEBERG_AGGRESSIVE) is None
    excluded = {entry.family: entry for entry in choice.excluded}
    assert ExpressionFamily.ICEBERG_AGGRESSIVE in excluded
    refusal = excluded[ExpressionFamily.ICEBERG_AGGRESSIVE]
    assert "divides a 101-unit clip exactly" in refusal.reason
    assert refusal.source, "an exclusion on a facility fact must carry the facility's source"
    assert choice.chosen.iceberg_legs is None


@pytest.mark.unit
def test_a_cover_order_appears_only_on_equity_intraday_and_only_with_a_stop() -> None:
    """`CO` is NSE equity intraday only, and it imposes a stop the intent never asked for."""
    without_stop = selector().select(request())
    excluded = {entry.family: entry.reason for entry in without_stop.excluded}
    assert "carries no protective stop" in excluded[ExpressionFamily.COVER_ORDER]

    on_options = selector().select(
        ExpressionSelectionRequest(
            intent=intent(segment=ChargeableSegment.EQUITY_OPTIONS),
            snapshot=book(),
            option_strike_paise=Decimal(10_000),
            protective_stop_price_paise=Decimal(9_500),
        )
    )
    options_excluded = {entry.family: entry.reason for entry in on_options.excluded}
    assert "equity intraday only" in options_excluded[ExpressionFamily.COVER_ORDER]

    with_stop = selector().select(
        request(protective_stop_price_paise=Decimal(9_500)),
    )
    cover = _by_family(with_stop.scored, ExpressionFamily.COVER_ORDER)
    assert cover is not None
    assert cover.expression.variety is OrderVariety.COVER
    assert cover.expression.trigger_price_paise == Decimal(9_500)


@pytest.mark.unit
def test_an_upside_down_cover_stop_is_refused_rather_than_sent() -> None:
    """A stop above a long entry is an instant exit; the exchange rejects it at 09:20."""
    choice = selector().select(request(protective_stop_price_paise=Decimal(10_500)))
    excluded = {entry.family: entry.reason for entry in choice.excluded}
    assert "does not protect" in excluded[ExpressionFamily.COVER_ORDER]


@pytest.mark.unit
def test_a_closed_session_yields_an_after_market_order_and_says_it_was_the_only_one() -> None:
    """ "The only one" is a weaker claim than "the best one" and must not print the same way."""
    from nse_algo_trader.order_path.order_expression_selector import SelectionBasis

    choice = selector(session_open=False).select(
        ExpressionSelectionRequest(intent=intent(decided_at=AFTER_CLOSE), snapshot=book())
    )
    assert choice.chosen_family is ExpressionFamily.AFTER_MARKET
    assert choice.chosen.variety is OrderVariety.AFTER_MARKET
    assert choice.basis is SelectionBasis.SOLE_FEASIBLE
    assert choice.runner_up_family is None
    assert choice.advantage_bps is None
    assert "only expression" in choice.chosen.chosen_because


@pytest.mark.unit
def test_an_open_session_refuses_the_after_market_order() -> None:
    choice = selector().select(request())
    excluded = {entry.family: entry.reason for entry in choice.excluded}
    assert "session is open" in excluded[ExpressionFamily.AFTER_MARKET]


@pytest.mark.unit
def test_the_statutory_term_penalises_an_iceberg_once_the_brokerage_cap_binds() -> None:
    """Brokerage is per ORDER (`L1.01`), and the split is free until the flat cap bites.

    Measured rather than assumed, and the shape is worth stating: Zerodha's intraday brokerage is
    a percentage capped at a flat figure per order, so splitting a SMALL ticket changes nothing —
    ten legs of a tenth each pay the same percentage. It is only above the cap that the whole clip
    would have paid the flat figure once while the legs each pay the percentage, and the split
    becomes genuinely expensive. An engine that assumed "legs always cost more" would be wrong on
    every small ticket, and one that assumed "legs are free" would be wrong on every large one.
    """
    small = selector(touch_rate="40").select(
        ExpressionSelectionRequest(intent=intent(quantity=600), snapshot=thin_book())
    )
    small_iceberg = _by_family(small.scored, ExpressionFamily.ICEBERG_AGGRESSIVE)
    small_single = _by_family(small.scored, ExpressionFamily.AGGRESSIVE_LIMIT_DAY)
    assert small_iceberg is not None and small_single is not None
    assert small_iceberg.statutory_cost_bps == small_single.statutory_cost_bps

    large = selector(touch_rate="40").select(
        ExpressionSelectionRequest(intent=intent(quantity=2_000), snapshot=thin_book())
    )
    large_iceberg = _by_family(large.scored, ExpressionFamily.ICEBERG_AGGRESSIVE)
    large_single = _by_family(large.scored, ExpressionFamily.AGGRESSIVE_LIMIT_DAY)
    assert large_iceberg is not None and large_single is not None
    assert large_iceberg.order_count > 1
    assert large_iceberg.statutory_cost_bps > large_single.statutory_cost_bps


@pytest.mark.unit
def test_an_option_intent_without_a_strike_is_refused_before_anything_is_priced() -> None:
    """SEBI's turnover fee is charged on notional; defaulting it to premium understates it ~100x."""
    from nse_algo_trader.order_path.order_expression_selector import (
        OrderExpressionSelectionError,
    )

    with pytest.raises(OrderExpressionSelectionError, match="needs a strike"):
        selector().select(
            ExpressionSelectionRequest(
                intent=intent(segment=ChargeableSegment.EQUITY_OPTIONS), snapshot=book()
            )
        )


@pytest.mark.unit
def test_a_book_for_the_wrong_instrument_is_a_refusal_not_a_silent_cross_price() -> None:
    from nse_algo_trader.order_path.order_expression_selector import (
        OrderExpressionSelectionError,
    )

    with pytest.raises(OrderExpressionSelectionError, match="cross-instrument"):
        selector().select(
            ExpressionSelectionRequest(intent=intent(), snapshot=book(token=A_TOKEN + 1))
        )


# --------------------------------------------------------------------------- the model


@pytest.mark.unit
def test_the_resting_forecast_is_monotone_in_the_horizon_and_in_the_rate() -> None:
    """The waiting-time model has no free parameter, so its two inputs must both bite."""
    slow = TouchExecutionRate(
        quantity_per_minute=Decimal(10),
        observed_minutes=Decimal(60),
        transition_count=100,
        executed_quantity=600,
        is_upper_bound=True,
        evidence="fixture",
    )
    fast = TouchExecutionRate(
        quantity_per_minute=Decimal(1_000),
        observed_minutes=Decimal(60),
        transition_count=100,
        executed_quantity=60_000,
        is_upper_bound=True,
        evidence="fixture",
    )
    short = forecast_resting_fill(
        queue_ahead_quantity=500, clip_quantity=100, horizon_minutes=5, rate=slow
    )
    long_horizon = forecast_resting_fill(
        queue_ahead_quantity=500, clip_quantity=100, horizon_minutes=500, rate=slow
    )
    quick_queue = forecast_resting_fill(
        queue_ahead_quantity=500, clip_quantity=100, horizon_minutes=5, rate=fast
    )
    assert long_horizon.fill_probability > short.fill_probability
    assert quick_queue.fill_probability > short.fill_probability
    assert long_horizon.edge_survival_fraction > short.edge_survival_fraction
    for forecast in (short, long_horizon, quick_queue):
        assert 0 <= forecast.fill_probability <= 1
        assert 0 <= forecast.edge_survival_fraction <= 1


@pytest.mark.unit
def test_a_measured_rate_of_zero_means_never_fills_rather_than_dividing_by_zero() -> None:
    """A touch queue that demonstrably never executes is a real measurement, not an error."""
    dead = TouchExecutionRate(
        quantity_per_minute=Decimal(0),
        observed_minutes=Decimal(60),
        transition_count=100,
        executed_quantity=0,
        is_upper_bound=True,
        evidence="fixture",
    )
    forecast = forecast_resting_fill(
        queue_ahead_quantity=100, clip_quantity=10, horizon_minutes=30, rate=dead
    )
    assert forecast.fill_probability == 0
    assert forecast.edge_survival_fraction == 0


@pytest.mark.unit
def test_the_depth_tape_observer_is_causal_and_reports_its_own_bound() -> None:
    """A rate computed from the whole session would let the afternoon decide the morning."""

    class Rows:
        def __init__(self, rows: tuple[object, ...]) -> None:
            self._rows = rows

        def replay_instrument(self, instrument_token: int) -> object:
            return iter(self._rows)

    from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
        QueueDepletionInterval,
    )

    class Row:
        def __init__(self, minute: int, executed: int) -> None:
            self.receipt_time = MIDDAY + timedelta(minutes=minute)
            self.bid_queue_depletion = QueueDepletionInterval(
                depleted_quantity=executed,
                minimum_executed=0,
                maximum_executed=executed,
                minimum_cancelled=0,
                maximum_cancelled=executed,
            )
            self.ask_queue_depletion = None

    rows = tuple(Row(minute, 10) for minute in range(0, 60, 10))
    observer = DepthTapeTouchExecutionObserver(Rows(rows))  # type: ignore[arg-type]
    early = observer.observe_touch_execution_rate(
        instrument_token=1, resting_side=TradeLeg.BUY, as_of=MIDDAY + timedelta(minutes=20)
    )
    whole = observer.observe_touch_execution_rate(
        instrument_token=1, resting_side=TradeLeg.BUY, as_of=MIDDAY + timedelta(hours=2)
    )
    assert early is not None and whole is not None
    assert early.transition_count < whole.transition_count, "the early view saw fewer transitions"
    assert early.is_upper_bound and whole.is_upper_bound
    # The ask side never has a same-price transition here, so there is nothing to measure.
    assert (
        observer.observe_touch_execution_rate(
            instrument_token=1, resting_side=TradeLeg.SELL, as_of=MIDDAY + timedelta(hours=2)
        )
        is None
    )


# --------------------------------------------------------------------------- property


@pytest.mark.property
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    quantity=st.integers(min_value=1, max_value=20_000),
    horizon=st.one_of(st.none(), st.integers(min_value=1, max_value=375)),
    edge=st.integers(min_value=-50, max_value=500),
    rate=st.sampled_from([None, "1", "50", "5000"]),
    side=st.sampled_from([TradeLeg.BUY, TradeLeg.SELL]),
    touch_depth=st.integers(min_value=1, max_value=5_000),
)
def test_the_chosen_candidate_is_always_the_arg_max_of_the_scored_list(
    quantity: int,
    horizon: int | None,
    edge: int,
    rate: str | None,
    side: TradeLeg,
    touch_depth: int,
) -> None:
    """The choice IS the optimisation. Any gap between them is a preference hiding in the code."""
    ladder = (
        (9_900, touch_depth),
        (9_800, touch_depth * 2),
        (9_700, touch_depth * 3),
    )
    asks = (
        (10_100, touch_depth),
        (10_200, touch_depth * 2),
        (10_300, touch_depth * 3),
    )
    try:
        choice = selector(touch_rate=rate).select(
            ExpressionSelectionRequest(
                intent=intent(
                    quantity=quantity, horizon_minutes=horizon, side=side, edge_bps=str(edge)
                ),
                snapshot=book(bids=ladder, asks=asks),
            )
        )
    except NoFeasibleExpressionError as refusal:
        # A legitimate outcome, not a gap in the property: a clip far beyond the ladder implies a
        # limit price that bounds nothing, and every candidate that would have carried it is
        # refused by name. What must never happen is a silent fallback.
        assert "market_take" in str(refusal)
        return
    best = max(candidate.expected_net_edge_bps for candidate in choice.scored)
    winner = choice.winner
    assert winner is not None, "the chosen family must appear in the scored list"
    assert winner.expected_net_edge_bps == best
    assert winner is max(choice.scored, key=_ranking_key)
    assert choice.scored == tuple(sorted(choice.scored, key=_ranking_key, reverse=True))
    if choice.runner_up_family is not None:
        assert choice.advantage_bps is not None
        assert choice.advantage_bps >= 0


@pytest.mark.property
@settings(max_examples=40, deadline=None)
@given(
    queue_ahead=st.integers(min_value=0, max_value=1_000_000),
    clip=st.integers(min_value=1, max_value=100_000),
    horizon=st.integers(min_value=1, max_value=375),
    rate=st.integers(min_value=0, max_value=100_000),
)
def test_every_forecast_is_a_probability_and_a_survival_fraction(
    queue_ahead: int, clip: int, horizon: int, rate: int
) -> None:
    """Nothing in the waiting-time model may leave [0, 1], at any input the market can produce."""
    forecast = forecast_resting_fill(
        queue_ahead_quantity=queue_ahead,
        clip_quantity=clip,
        horizon_minutes=horizon,
        rate=TouchExecutionRate(
            quantity_per_minute=Decimal(rate),
            observed_minutes=Decimal(60),
            transition_count=10,
            executed_quantity=rate * 60,
            is_upper_bound=True,
            evidence="fixture",
        ),
    )
    assert 0 <= forecast.fill_probability <= 1
    assert 0 <= forecast.edge_survival_fraction <= 1
    assert forecast.expected_wait_given_fill_minutes >= 0


# --------------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_crossed_book_is_refused_rather_than_divided_by() -> None:
    """955 crossed books were measured in one real session (`L0.33`). This is not hypothetical."""
    crossed = book(bids=((10_200, 100),), asks=((10_100, 100),))
    with pytest.raises(BookUnpriceableError, match="crossed"):
        selector(touch_rate="500").select(
            ExpressionSelectionRequest(intent=intent(), snapshot=crossed)
        )


@pytest.mark.adversarial
def test_a_locked_book_is_refused_too() -> None:
    """Bid == ask is a zero spread, which enters a cost model as a free trade."""
    locked = book(bids=((10_000, 100),), asks=((10_000, 100),))
    with pytest.raises(BookUnpriceableError):
        selector().select(ExpressionSelectionRequest(intent=intent(), snapshot=locked))


@pytest.mark.adversarial
def test_zero_visible_depth_is_refused_rather_than_filled_for_nothing() -> None:
    """Kite pads absent levels with price 0, quantity 0. A pad treated as a rung fills for free."""
    empty_asks = book(asks=())
    with pytest.raises(BookUnpriceableError):
        selector().select(ExpressionSelectionRequest(intent=intent(), snapshot=empty_asks))

    empty_bids = book(bids=())
    with pytest.raises(BookUnpriceableError):
        selector().select(
            ExpressionSelectionRequest(intent=intent(side=TradeLeg.SELL), snapshot=empty_bids)
        )


@pytest.mark.adversarial
def test_a_one_paisa_book_with_a_gigantic_clip_still_refuses_or_answers_sanely() -> None:
    """The pathological end of the real universe: DHARAN closed at 16 paise on 2026-06-30."""
    penny = book(
        bids=((1, 1),),
        asks=((2, 1),),
        total_buy_quantity=1,
        total_sell_quantity=1,
    )
    choice = selector(touch_rate="1").select(
        ExpressionSelectionRequest(
            intent=TradingIntent(
                strategy_identity="s",
                instrument_token=A_TOKEN,
                trading_symbol="PENNY",
                segment=ChargeableSegment.EQUITY_INTRADAY,
                side=TradeLeg.BUY,
                quantity=1_000_000,
                decided_at=MIDDAY,
                reference_price_paise=Decimal(2),
                expected_edge_bps=Decimal(200),
                horizon_minutes=15,
            ),
            snapshot=penny,
        )
    )
    winner = choice.winner
    assert winner is not None
    assert winner.expected_net_edge_bps == max(
        candidate.expected_net_edge_bps for candidate in choice.scored
    )
    assert choice.chosen.limit_price_paise is not None
    assert choice.chosen.limit_price_paise > 0


@pytest.mark.adversarial
def test_a_negative_edge_never_becomes_positive_by_choosing_an_expression() -> None:
    """No expression creates edge. The best of a bad set is still bad, and must read that way."""
    choice = selector(touch_rate="500").select(
        ExpressionSelectionRequest(intent=intent(edge_bps="-30"), snapshot=book())
    )
    winner = choice.winner
    assert winner is not None
    assert winner.expected_net_edge_bps < 0


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_the_selector_runs_over_real_books_from_the_recorded_tape() -> None:
    """`R.05` — real depth, real spreads, real crossed quotes, real queue depletion.

    The fill probability comes from `L0.22`'s own replay of the same session, so the passive
    candidate is scored on measured queue depletion rather than on anything this test supplies.
    Every instrument is priced as equity intraday: the tape carries tokens, not segments, and the
    statutory term needs a segment — the claim under test is that a REAL book yields a valid,
    feasible, never-barred expression, not that the token is genuinely a cash equity.
    """
    from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
    from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
        OrderBookSnapshotReplayEngine,
    )
    from nse_algo_trader.order_path.order_expression_selector import SelectionBasis

    reader = MarketDepthTapeReader(LIVE_TAPE_ROOT)
    completed = [day for day in sorted(reader.session_dates()) if day < date(2026, 8, 13)]
    if not completed:
        pytest.skip("the tape holds no completed session")
    session_date = completed[-1]
    tokens = reader.instrument_tokens(session_date)
    if not tokens:
        pytest.skip(f"the {session_date} tape holds no instruments")

    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=session_date, staleness_quantile=0.99
    )
    observer = DepthTapeTouchExecutionObserver(engine)
    chooser = OrderExpressionSelector(
        default_transaction_cost_engine(),
        ExecutionFillModel(),
        touch_queue_observer=observer,
        session_phase=FixedSessionPhase(is_open=True),
    )

    barred_varieties = {OrderVariety.BRACKET}
    chosen_families: dict[str, int] = {}
    priced = 0
    refused = 0
    for token in tokens[:12]:
        snapshots = [
            snapshot
            for snapshot in _real_snapshots(reader, session_date, token)
            if snapshot.best_bid_paise
            and snapshot.best_ask_paise
            and snapshot.best_bid_paise < snapshot.best_ask_paise
        ]
        if not snapshots:
            continue
        snapshot = snapshots[len(snapshots) // 2]
        real_intent = TradingIntent(
            strategy_identity="l9_14_real_data_probe",
            instrument_token=token,
            trading_symbol=f"TOKEN{token}",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            side=TradeLeg.BUY,
            quantity=max(1, snapshot.best_ask_quantity),
            decided_at=snapshot.receipt_time,
            reference_price_paise=Decimal(snapshot.best_ask_paise or 1),
            expected_edge_bps=Decimal(75),
            horizon_minutes=30,
        )
        try:
            choice = chooser.select(
                ExpressionSelectionRequest(intent=real_intent, snapshot=snapshot)
            )
        except BookUnpriceableError:  # pragma: no cover - depends on the recorded tape
            refused += 1
            continue
        priced += 1
        chosen_families[choice.chosen_family.value] = (
            chosen_families.get(choice.chosen_family.value, 0) + 1
        )

        assert choice.chosen.order_type is not OrderType.MARKET, (
            "a barred order type reached a real book"
        )
        assert choice.chosen.variety not in barred_varieties
        assert choice.chosen.limit_price_paise is not None
        assert choice.chosen.limit_price_paise > 0
        assert choice.chosen.chosen_because
        assert ExpressionFamily.MARKET_TAKE in choice.excluded_families
        assert choice.basis in (SelectionBasis.OPTIMISED, SelectionBasis.SOLE_FEASIBLE)
        winner = choice.winner
        assert winner is not None
        assert winner.expected_net_edge_bps == max(
            candidate.expected_net_edge_bps for candidate in choice.scored
        )
        # The expression must be constructible exactly as chosen — this is the wire contract.
        OrderExpression(
            variety=choice.chosen.variety,
            product=choice.chosen.product,
            order_type=choice.chosen.order_type,
            validity=choice.chosen.validity,
            limit_price_paise=choice.chosen.limit_price_paise,
            trigger_price_paise=choice.chosen.trigger_price_paise,
            disclosed_quantity=choice.chosen.disclosed_quantity,
            iceberg_legs=choice.chosen.iceberg_legs,
        )

    assert priced >= 3, (
        f"only {priced} real instruments were priceable on {session_date} "
        f"({refused} refused) — too thin to count as a real-data pass"
    )
    print(f"\nL9.14 real data {session_date}: {priced} instruments -> {chosen_families}")


def _real_snapshots(reader: object, session_date: date, token: int) -> list[BookSnapshot]:
    """One instrument's real session, typed, through the tape store's own public reader."""
    from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
    from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
        book_snapshots_from_table,
    )

    assert isinstance(reader, MarketDepthTapeReader)
    window_start = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    table = reader.read_instrument_window(
        token, window_start, window_start + timedelta(days=1), session_date
    )
    return book_snapshots_from_table(table)


# --------------------------------------------------------------------------- helpers


def _by_family(
    scored: tuple[ScoredExpression, ...], family: ExpressionFamily
) -> ScoredExpression | None:
    for candidate in scored:
        if candidate.family is family:
            return candidate
    return None


def _price_of(choice: object, family: ExpressionFamily) -> Decimal:
    from nse_algo_trader.order_path.order_expression_selector import ExpressionChoice

    assert isinstance(choice, ExpressionChoice)
    candidate = _by_family(choice.scored, family)
    assert candidate is not None
    price = candidate.expression.limit_price_paise
    assert price is not None
    return price
