"""The order as a record of what happened to it, and the fill arithmetic underneath.

Two defects in `docs/research/224` are being designed out here, both of them real code in shipped
systems:

* **Absolute-overwrite fill quantities** (OctoBot `order.py:1090`, and the same pattern in
  Hummingbot and freqtrade): a stale or out-of-order poll response makes `filled_quantity` go
  BACKWARDS. Kite publishes no ordering guarantee for postbacks (`docs/research/222` §3), so
  out-of-order delivery is not hypothetical here.
* **No dedup on re-delivered fills** (LEAN, whose `OrderEvent.Id` is self-generated rather than
  keyed on the broker's execution id): the same fill applies twice and the position is wrong.

NautilusTrader gets both right — saturating arithmetic (`orders/mod.rs:1231-1272`) and a trade-id
check BEFORE the transition (`:833-840`). These tests are that behaviour, stated as claims.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    IllegalOrderTransitionError,
    LifecycleEvent,
    OrderLifecycleState,
)
from nse_algo_trader.order_path.order_record import (
    DuplicateFillRejectedError,
    FillRecord,
    OrderExpression,
    OrderQuantityError,
    OrderRecord,
)
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = pytest.mark.unit

_DECIDED_AT = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)


def _intent(quantity: int = 100) -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=738561,
        trading_symbol="RELIANCE",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_DECIDED_AT,
        reference_price_paise=Decimal("142350"),
        expected_edge_bps=Decimal("34.1"),
    )


def _expression() -> OrderExpression:
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal("142350"),
    )


def _order(quantity: int = 100) -> OrderRecord:
    return OrderRecord.from_intent(
        _intent(quantity), _expression(), OrderNamespace.SIMULATED, at=_DECIDED_AT
    )


def _fill(quantity: int, price: str, trade_id: str, second: int = 0) -> FillRecord:
    return FillRecord(
        broker_trade_id=trade_id,
        quantity=quantity,
        price_paise=Decimal(price),
        occurred_at=datetime(2026, 8, 13, 10, 16, second, tzinfo=UTC),
        source=EventSource.BROKER_REPORTED,
    )


class TestTheOrderStartsWhereTheIntentDoes:
    def test_a_new_order_carries_its_intents_name(self) -> None:
        order = _order()
        assert order.intent_id == _intent().intent_id
        assert order.state is OrderLifecycleState.INTENT_RECORDED
        assert order.filled_quantity == 0
        assert order.leaves_quantity == 100

    def test_the_tag_on_the_wire_belongs_to_the_namespace_it_was_built_for(self) -> None:
        assert _order().broker_tag.startswith(OrderNamespace.SIMULATED.value)


class TestFillsAccumulateAndNeverGoBackwards:
    def test_a_partial_fill_moves_the_order_and_leaves_the_rest(self) -> None:
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_event(LifecycleEvent.ACKNOWLEDGED, EventSource.BROKER_REPORTED, at=_DECIDED_AT)
        order.apply_fill(_fill(40, "142300", "T1"))
        assert order.state is OrderLifecycleState.PARTIALLY_FILLED
        assert order.filled_quantity == 40
        assert order.leaves_quantity == 60

    def test_the_last_fill_completes_the_order_without_being_told_to(self) -> None:
        """The order knows it is finished from its own arithmetic. A system that waits to be told
        depends on a status field that Kite does not have for partial fills."""
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_event(LifecycleEvent.ACKNOWLEDGED, EventSource.BROKER_REPORTED, at=_DECIDED_AT)
        order.apply_fill(_fill(40, "142300", "T1"))
        order.apply_fill(_fill(60, "142400", "T2", second=5))
        assert order.state is OrderLifecycleState.FILLED
        assert order.leaves_quantity == 0

    def test_the_same_trade_reported_twice_is_applied_once(self) -> None:
        """Kite's postbacks carry no delivery guarantee, so at-least-once must be assumed."""
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_fill(_fill(40, "142300", "T1"))
        order.apply_fill(_fill(40, "142300", "T1"))
        assert order.filled_quantity == 40

    def test_the_same_trade_id_with_a_different_quantity_is_refused_loudly(self) -> None:
        """Idempotence is not the same as tolerance. Two different facts under one id means the
        broker's data or our matching is wrong, and neither may be silently absorbed."""
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_fill(_fill(40, "142300", "T1"))
        with pytest.raises(DuplicateFillRejectedError, match="T1"):
            order.apply_fill(_fill(41, "142300", "T1", second=9))

    def test_fills_cannot_exceed_the_ordered_quantity(self) -> None:
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_fill(_fill(90, "142300", "T1"))
        with pytest.raises(OrderQuantityError, match="exceed"):
            order.apply_fill(_fill(20, "142300", "T2", second=3))

    def test_fills_arriving_out_of_order_reach_the_same_place(self) -> None:
        earlier = _fill(40, "142300", "T1", second=1)
        later = _fill(60, "142400", "T2", second=9)
        forwards, backwards = _order(), _order()
        for order, sequence in ((forwards, (earlier, later)), (backwards, (later, earlier))):
            order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
            for fill in sequence:
                order.apply_fill(fill)
        assert forwards.filled_quantity == backwards.filled_quantity == 100
        assert forwards.average_fill_price_paise == backwards.average_fill_price_paise


class TestTheAveragePriceIsWeightedNotMeaned:
    def test_the_average_is_quantity_weighted(self) -> None:
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_fill(_fill(90, "100000", "T1"))
        order.apply_fill(_fill(10, "200000", "T2", second=2))
        assert order.average_fill_price_paise == Decimal("110000")

    def test_an_unfilled_order_has_no_average_rather_than_a_zero(self) -> None:
        """Zero is a price. `None` is the absence of one, and the two must not be confused by
        anything that later computes slippage against it."""
        assert _order().average_fill_price_paise is None


class TestInferenceIsMarkedForever:
    def test_an_inferred_fill_is_visible_as_inferred(self) -> None:
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_fill(
            FillRecord(
                broker_trade_id="inferred:RELIANCE:1",
                quantity=25,
                price_paise=Decimal("142380"),
                occurred_at=datetime(2026, 8, 13, 10, 20, tzinfo=UTC),
                source=EventSource.INFERRED,
                inferred_from="broker filled_quantity 25 exceeded local 0 with no trade reported",
            )
        )
        assert order.has_inferred_events
        assert order.fills[0].source is EventSource.INFERRED

    def test_an_inferred_fill_must_say_what_it_was_inferred_from(self) -> None:
        with pytest.raises(ValueError, match="inferred"):
            FillRecord(
                broker_trade_id="inferred:X",
                quantity=1,
                price_paise=Decimal("1"),
                occurred_at=_DECIDED_AT,
                source=EventSource.INFERRED,
            )


class TestTheHistoryIsKept:
    def test_every_transition_records_its_source(self) -> None:
        order = _order()
        order.apply_event(LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT)
        order.apply_event(LifecycleEvent.ACKNOWLEDGED, EventSource.BROKER_REPORTED, at=_DECIDED_AT)
        assert [transition.source for transition in order.transitions] == [
            EventSource.LOCAL,
            EventSource.BROKER_REPORTED,
        ]

    def test_an_illegal_event_leaves_the_order_untouched(self) -> None:
        order = _order()
        with pytest.raises(IllegalOrderTransitionError):
            order.apply_event(
                LifecycleEvent.FULLY_FILLED, EventSource.BROKER_REPORTED, at=_DECIDED_AT
            )
        assert order.state is OrderLifecycleState.INTENT_RECORDED
        assert order.transitions == ()
