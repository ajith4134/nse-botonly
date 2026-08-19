"""What the venue and the regulator actually permit, carried as dated fact rather than assumption.

Two of the constraints this module holds are not preferences and cannot be argued with:

* **MARKET orders are not permitted for algo-originated flow** (NSE/MSD/67753, `docs/research/223`
  §5). The operator's instruction for `F02` was to build the whole taxonomy and use each member
  where it is best; this is the one member that has no such place, and the refusal names the
  circular rather than quietly omitting the type.
* **Bracket Orders were withdrawn in March 2020** (`docs/research/222` §6). The type still exists in
  the taxonomy, dated and refused, so that a system reading its own vocabulary can tell "we never
  modelled this" apart from "this was taken away".
"""

from __future__ import annotations

from datetime import date

import pytest

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderFacilityError,
    OrderOrigin,
    OrderType,
    OrderVariety,
    RateLimitScope,
    availability_of_order_type,
    availability_of_variety,
    order_rate_limits,
    varieties_available_to,
)

pytestmark = pytest.mark.unit

_TODAY = date(2026, 8, 13)


class TestTheRegulatorsRefusals:
    def test_a_market_order_is_refused_for_algo_flow_and_the_refusal_cites_its_circular(
        self,
    ) -> None:
        fact = availability_of_order_type(OrderType.MARKET, OrderOrigin.ALGO_API, on=_TODAY)
        assert not fact.available
        assert "NSE/MSD/67753" in fact.source
        assert fact.effective_from <= _TODAY

    def test_the_same_order_type_is_permitted_to_a_human_clicking_a_button(self) -> None:
        """The prohibition is on the FLOW, not on the instrument, and conflating them would make
        the system unable to describe the market it trades in."""
        assert availability_of_order_type(OrderType.MARKET, OrderOrigin.MANUAL, on=_TODAY).available

    def test_the_permitted_types_remain_permitted(self) -> None:
        for order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT, OrderType.STOP_LOSS_MARKET):
            assert availability_of_order_type(order_type, OrderOrigin.ALGO_API, on=_TODAY).available


class TestWhatTheBrokerWithdrew:
    def test_a_bracket_order_is_refused_with_its_withdrawal_date(self) -> None:
        fact = availability_of_variety(OrderVariety.BRACKET, on=_TODAY)
        assert not fact.available
        assert fact.effective_from == date(2020, 3, 1)
        assert "bracket" in fact.reason.lower()

    def test_a_bracket_order_was_available_before_it_was_withdrawn(self) -> None:
        """Point-in-time, so a replay of 2019 does not inherit 2026's facilities (`R.03`)."""
        assert availability_of_variety(OrderVariety.BRACKET, on=date(2019, 6, 1)).available

    def test_cover_orders_survived_and_are_still_offered(self) -> None:
        assert availability_of_variety(OrderVariety.COVER, on=_TODAY).available

    def test_the_available_set_excludes_what_was_withdrawn(self) -> None:
        available = varieties_available_to(OrderOrigin.ALGO_API, on=_TODAY)
        assert OrderVariety.BRACKET not in available
        assert {
            OrderVariety.REGULAR,
            OrderVariety.ICEBERG,
            OrderVariety.AFTER_MARKET,
        } <= available

    def test_an_unknown_facility_is_refused_rather_than_assumed_present(self) -> None:
        with pytest.raises(OrderFacilityError, match="no recorded fact"):
            availability_of_variety(OrderVariety.REGULAR, on=date(1990, 1, 1))


class TestTheRateLimitsBindTogether:
    def test_every_published_ceiling_is_present_with_its_source(self) -> None:
        limits = order_rate_limits(on=_TODAY)
        assert limits, "the limiter cannot derive a budget from an empty fact set"
        for limit in limits:
            assert limit.source.strip()
            assert limit.maximum_orders > 0
            assert limit.window_seconds > 0

    def test_the_regulatory_second_and_the_broker_second_are_separate_facts(self) -> None:
        """They happen to agree at ten today. Merging them would hide the day they diverge, and
        one is a broker's policy while the other decides a regulatory category."""
        one_second = [limit for limit in order_rate_limits(on=_TODAY) if limit.window_seconds == 1]
        scopes = {limit.scope for limit in one_second}
        assert RateLimitScope.REGULATORY_THRESHOLD in scopes
        assert RateLimitScope.BROKER_CEILING in scopes

    def test_the_daily_ceiling_is_the_one_the_collision_bound_was_computed_against(self) -> None:
        daily = [
            limit for limit in order_rate_limits(on=_TODAY) if limit.window_seconds == 24 * 60 * 60
        ]
        assert [limit.maximum_orders for limit in daily] == [5_000]

    def test_limits_are_point_in_time(self) -> None:
        assert order_rate_limits(on=date(2015, 1, 1)) == ()
