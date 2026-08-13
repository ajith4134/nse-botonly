"""The identity of an intent, which is the whole of `L3.01`.

Kite Connect accepts no client-supplied order id (`docs/research/222` §1), so "the same intent can
never become two orders" cannot be delegated to the broker. It has to be a property of a name this
system computes for itself, and the name has to be recomputable after a crash by a process that
remembers nothing — otherwise recovery has to guess.

These tests are written before the module exists and are the specification of that name.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nse_algo_trader.order_path.trading_intent import (
    IntentIdentityError,
    OrderNamespace,
    TradingIntent,
    broker_tag_for,
    session_and_namespace_from_tag,
    tag_collision_probability,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = pytest.mark.unit


def _intent(**overrides: object) -> TradingIntent:
    fields: dict[str, object] = {
        "strategy_identity": "calibrated_mean_reversion.v1",
        "instrument_token": 738561,
        "trading_symbol": "RELIANCE",
        "segment": ChargeableSegment.EQUITY_INTRADAY,
        "side": TradeLeg.BUY,
        "quantity": 40,
        "decided_at": datetime(2026, 8, 13, 10, 15, 30, 250_000, tzinfo=UTC),
        "reference_price_paise": Decimal("142350"),
        "expected_edge_bps": Decimal("34.1"),
    }
    fields.update(overrides)
    return TradingIntent(**fields)  # type: ignore[arg-type]


class TestTheNameIsComputedFromTheDecision:
    def test_the_same_decision_names_itself_the_same_way_twice(self) -> None:
        assert _intent().intent_id == _intent().intent_id

    def test_the_name_survives_a_process_that_remembers_nothing(self) -> None:
        """No store is consulted: recovery recomputes the name from the decision alone."""
        first = _intent().intent_id
        second = TradingIntent(
            strategy_identity="calibrated_mean_reversion.v1",
            instrument_token=738561,
            trading_symbol="RELIANCE",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            side=TradeLeg.BUY,
            quantity=40,
            decided_at=datetime(2026, 8, 13, 10, 15, 30, 250_000, tzinfo=UTC),
            reference_price_paise=Decimal("142350"),
            expected_edge_bps=Decimal("34.1"),
        ).intent_id
        assert first == second

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("strategy_identity", "calibrated_mean_reversion.v2"),
            ("instrument_token", 738562),
            ("trading_symbol", "RELIANC"),
            ("segment", ChargeableSegment.EQUITY_DELIVERY),
            ("side", TradeLeg.SELL),
            ("quantity", 41),
            ("decided_at", datetime(2026, 8, 13, 10, 15, 30, 250_001, tzinfo=UTC)),
            ("decided_at", datetime(2026, 8, 14, 10, 15, 30, 250_000, tzinfo=UTC)),
        ],
    )
    def test_every_field_that_makes_the_decision_changes_the_name(
        self, field: str, value: object
    ) -> None:
        assert _intent().intent_id != _intent(**{field: value}).intent_id

    def test_the_claimed_edge_does_not_change_the_name(self) -> None:
        """Re-pricing the same decision is not a different decision.

        The edge is a claim ABOUT the trade, and letting it into the identity would mean a
        recalibration between the crash and the recovery renamed an order that is already at the
        broker — which is exactly how one intent becomes two.
        """
        assert _intent().intent_id == _intent(expected_edge_bps=Decimal("40.4")).intent_id

    def test_the_reference_price_does_not_change_the_name(self) -> None:
        """Same reason: the price moved, the decision did not."""
        assert _intent().intent_id == _intent(reference_price_paise=Decimal("142400")).intent_id


class TestTheNameRefusesRatherThanGuesses:
    def test_a_naive_decision_time_is_refused(self) -> None:
        with pytest.raises(IntentIdentityError, match="timezone"):
            _intent(decided_at=datetime(2026, 8, 13, 10, 15, 30, 250_000))  # noqa: DTZ001

    def test_the_session_is_derived_from_the_decision_time_in_ist(self) -> None:
        """A decision at 20:00 UTC is the NEXT IST session day, and the tag must say so."""
        assert _intent(decided_at=datetime(2026, 8, 13, 20, 0, tzinfo=UTC)).session_date == date(
            2026, 8, 14
        )

    def test_a_strategy_with_no_identity_is_refused(self) -> None:
        with pytest.raises(IntentIdentityError, match="attribut"):
            _intent(strategy_identity="   ")

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_refused(self, quantity: int) -> None:
        with pytest.raises(IntentIdentityError, match="quantity"):
            _intent(quantity=quantity)


class TestTheTagThatRidesOnTheOrder:
    def test_the_tag_fits_the_broker_field_exactly(self) -> None:
        """20 characters, alphanumeric only — `docs/research/222` §1."""
        tag = broker_tag_for(_intent(), OrderNamespace.LIVE)
        assert len(tag) == 20
        assert tag.isalnum()
        assert tag.isascii()

    def test_the_tag_is_deterministic_in_the_intent(self) -> None:
        assert broker_tag_for(_intent(), OrderNamespace.LIVE) == broker_tag_for(
            _intent(), OrderNamespace.LIVE
        )

    def test_two_different_intents_get_different_tags(self) -> None:
        assert broker_tag_for(_intent(), OrderNamespace.LIVE) != broker_tag_for(
            _intent(quantity=41), OrderNamespace.LIVE
        )

    def test_a_simulated_order_can_never_be_mistaken_for_a_live_one(self) -> None:
        live = broker_tag_for(_intent(), OrderNamespace.LIVE)
        simulated = broker_tag_for(_intent(), OrderNamespace.SIMULATED)
        assert live != simulated
        read_live = session_and_namespace_from_tag(live)
        read_simulated = session_and_namespace_from_tag(simulated)
        assert read_live is not None
        assert read_simulated is not None
        assert read_live[1] is OrderNamespace.LIVE
        assert read_simulated[1] is OrderNamespace.SIMULATED

    def test_the_session_is_readable_back_out_of_the_tag(self) -> None:
        """Reconciliation filters the day's order book by OUR tags before matching anything."""
        tag = broker_tag_for(_intent(), OrderNamespace.LIVE)
        read_back = session_and_namespace_from_tag(tag)
        assert read_back is not None
        assert read_back[0] == date(2026, 8, 13)

    def test_a_foreign_tag_is_reported_as_foreign_not_parsed_into_nonsense(self) -> None:
        """A manual order placed in the Kite app carries someone else's tag, or none."""
        assert session_and_namespace_from_tag("") is None
        assert session_and_namespace_from_tag("manualbuy") is None
        assert session_and_namespace_from_tag("X" * 20) is None

    def test_the_collision_probability_is_computed_and_small(self) -> None:
        """`R.03`: the number is derived from the charset and the broker's own daily cap.

        Kite's documented ceiling is 5,000 orders/day, so the birthday bound over one session is
        what matters, and it must be small enough that a collision is not a design risk.
        """
        probability = tag_collision_probability(orders_in_session=5_000)
        assert 0 < probability < Decimal("1e-15")
        assert tag_collision_probability(orders_in_session=1) == 0


class TestTheIntentKnowsWhatItIsWorth:
    def test_the_notional_is_derived_not_stored(self) -> None:
        intent = _intent(quantity=40, reference_price_paise=Decimal("142350"))
        assert intent.notional_paise == Decimal("5694000")
