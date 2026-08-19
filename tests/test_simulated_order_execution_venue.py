"""The simulator's obligations: fill from the tape, refuse without it, and never look real.

Three claims carry the weight here.

* **A simulated id can never be mistaken for a real one.** Everything downstream — the journal, the
  reconciler, a human reading a log at 09:20 — depends on being able to answer "did real money
  move" from the identifier alone.
* **No book, no fill.** A simulator that invents a price produces a complete, confident and
  entirely fictional fill history, and a strategy tuned against it is tuned against nothing.
* **Partial fills arrive across polls**, because that is how they arrive from a real venue, and a
  simulator that always fills in one shot hides every partial-fill defect in the code above it.

No network, no clock, no randomness: the venue is driven entirely by injected book snapshots
(`R.05`'s hermetic seam), which is also why this file can assert exact prices.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.order_execution_venue import (
    OrderExecutionVenue,
    VenueRejectedError,
    VenueTradeReport,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    lifecycle_event_for_broker_status,
)
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.simulated_order_execution_venue import (
    MAXIMUM_MODIFICATIONS_PER_ORDER,
    SIMULATED_ORDER_DIGEST_HEX_LENGTH,
    SIMULATED_ORDER_ID_PREFIX,
    SIMULATED_POSITION_VIEW,
    SIMULATED_TRADE_ID_PREFIX,
    STATUS_ACKNOWLEDGED,
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_OPEN,
    STATUS_TRIGGER_PENDING,
    FreezeQuantityExceededError,
    SimulatedOrderExecutionVenue,
    SimulatedVenueLimits,
    SimulatedVenueRefusedLiveOrderError,
    UnknownSimulatedOrderError,
    is_simulated_broker_order_id,
)
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = [pytest.mark.unit, pytest.mark.hermetic]

_AN_INSTANT = datetime(2026, 8, 13, 4, 15, tzinfo=UTC)  # 09:45 IST
_SESSION = date(2026, 8, 13)
_INSTRUMENT = 738561

# A book whose ask ladder is 100 @ 10,100 / 200 @ 10,200 / 400 @ 10,300 — three rungs, so an order
# of 300 must take three polls and two prices before it completes.
_ASK_LADDER = ((10_100, 100), (10_200, 200), (10_300, 400))
_BID_LADDER = ((9_900, 100), (9_800, 200), (9_700, 400))


def book(
    *,
    bids: tuple[tuple[int, int], ...] = _BID_LADDER,
    asks: tuple[tuple[int, int], ...] = _ASK_LADDER,
    last_price_paise: int = 10_000,
    receipt_time: datetime = _AN_INSTANT,
    receipt_sequence: int = 1,
    instrument_token: int = _INSTRUMENT,
) -> BookSnapshot:
    return BookSnapshot(
        instrument_token=instrument_token,
        receipt_time=receipt_time,
        receipt_sequence=receipt_sequence,
        capture_run="test",
        exchange_time=receipt_time,
        last_price_paise=last_price_paise,
        last_traded_quantity=1,
        volume_traded=1_000,
        total_buy_quantity=100_000,
        total_sell_quantity=100_000,
        integrity_flags=IntegrityFlag.NONE,
        bids=tuple(DepthLevel(price_paise=price, quantity=size, orders=1) for price, size in bids),
        asks=tuple(DepthLevel(price_paise=price, quantity=size, orders=1) for price, size in asks),
    )


def _intent(
    *,
    quantity: int = 300,
    symbol: str = "RELIANCE",
    side: TradeLeg = TradeLeg.BUY,
    decided_at: datetime = _AN_INSTANT,
) -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=_INSTRUMENT,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=side,
        quantity=quantity,
        decided_at=decided_at,
        reference_price_paise=Decimal(10_000),
        expected_edge_bps=Decimal("34.1"),
    )


def _limit(price_paise: int = 10_300) -> OrderExpression:
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal(price_paise),
        chosen_because="marketable limit; MARKET is barred for algo flow",
    )


def _order(
    expression: OrderExpression | None = None,
    *,
    quantity: int = 300,
    side: TradeLeg = TradeLeg.BUY,
    symbol: str = "RELIANCE",
    namespace: OrderNamespace = OrderNamespace.SIMULATED,
    at: datetime = _AN_INSTANT,
) -> OrderRecord:
    return OrderRecord.from_intent(
        _intent(quantity=quantity, symbol=symbol, side=side, decided_at=at),
        expression or _limit(),
        namespace,
        at=at,
    )


def _poll(
    venue: SimulatedOrderExecutionVenue, times: int = 1, *, start: datetime = _AN_INSTANT
) -> list[VenueTradeReport]:
    """Advance the venue `times` polls, one simulated second apart, returning every trade."""
    produced: list[VenueTradeReport] = []
    for step in range(times):
        produced.extend(venue.advance_matching_by_one_poll(at=start + timedelta(seconds=step + 1)))
    return produced


class TestTheSimulatorSatisfiesTheSameInterface:
    def test_it_is_an_order_execution_venue(self) -> None:
        assert isinstance(SimulatedOrderExecutionVenue(), OrderExecutionVenue)

    def test_every_status_it_emits_maps_through_the_shared_lifecycle_table(self) -> None:
        """Parity's sharpest edge: the simulator speaks Kite's own status vocabulary, so the one
        translation table serves both venues and neither has a private path through it."""
        for status in (
            STATUS_ACKNOWLEDGED,
            STATUS_OPEN,
            STATUS_TRIGGER_PENDING,
            STATUS_COMPLETE,
            STATUS_CANCELLED,
        ):
            assert lifecycle_event_for_broker_status(status) is not None


class TestTheIdNamespaceIsUnmistakable:
    def test_a_simulated_order_id_announces_itself(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        assert order_id.startswith(SIMULATED_ORDER_ID_PREFIX)
        assert is_simulated_broker_order_id(order_id)

    def test_a_real_kite_order_id_is_never_taken_for_a_simulated_one(self) -> None:
        """Zerodha's ids are decimal digit strings, so the two sets are provably disjoint rather
        than merely unlikely to collide."""
        assert not is_simulated_broker_order_id("250813001234567")
        assert not is_simulated_broker_order_id("")

    def test_ids_are_unique_and_deterministic_across_a_session(self) -> None:
        """Three placements of a byte-identical order record still get three distinct ids.

        The venue is not the idempotency guard — the journal is — so it must be able to issue an
        id for the same order record twice. Asserted on the SHAPE rather than on three literal
        ids: `SIM-ORD-<session>-<content digest>-<placement>`, where the digest is what makes a
        restart safe and the placement ordinal is what separates two placements within one
        process. A literal expectation here would have to be rewritten every time the content
        hashed changes, which is precisely the assertion that stops catching anything.
        """
        venue = SimulatedOrderExecutionVenue()
        ids = [venue.place(_order()) for _ in range(3)]
        assert len(set(ids)) == 3
        for placement, order_id in enumerate(ids, start=1):
            prefix, session_stamp, digest, ordinal = (
                SIMULATED_ORDER_ID_PREFIX,
                *order_id.removeprefix(SIMULATED_ORDER_ID_PREFIX).split("-"),
            )
            assert prefix == SIMULATED_ORDER_ID_PREFIX
            assert session_stamp == _SESSION.strftime("%Y%m%d")
            assert len(digest) == SIMULATED_ORDER_DIGEST_HEX_LENGTH
            assert int(digest, 16) >= 0, "the digest half must be readable hexadecimal"
            assert int(ordinal) == placement
        # The digest is a function of the order's content, so identical content hashes identically
        # and only the placement ordinal moves. That is the whole design, stated as an assertion.
        digests = {order_id.rsplit("-", maxsplit=1)[0] for order_id in ids}
        assert len(digests) == 1

    def test_two_venues_driven_identically_produce_identical_ids(self) -> None:
        """Determinism, stated as a test: no clock and no randomness anywhere in the path."""
        first, second = SimulatedOrderExecutionVenue(), SimulatedOrderExecutionVenue()
        assert [first.place(_order()) for _ in range(2)] == [
            second.place(_order()) for _ in range(2)
        ]

    @pytest.mark.adversarial
    def test_a_restarted_session_cannot_reissue_an_id_it_already_used(self) -> None:
        """`M/4` — the defect: ids were keyed on an in-memory counter plus the session date.

        The FIRST order of every process was `SIM-ORD-<date>-000001`. Restart a paper session
        mid-morning — a crash, a redeploy, an operator stopping the loop — and the next order
        placed reissued the id the morning's first order already held. Two DIFFERENT orders then
        shared an identifier, which is the one thing an identifier exists to make impossible: the
        journal keys fills, order history and reconciliation verdicts on it, so the second order
        would inherit the first one's fills.

        Reproduced exactly as a restart is: a second venue instance with no memory of the first,
        on the same session date, placing different orders. Every id from both processes must be
        distinct, and the assertion is on the whole set rather than on one pair so that "the
        counters happen to have diverged" cannot pass for "the ids are unique".
        """
        before_restart_venue = SimulatedOrderExecutionVenue()
        before_restart = [
            before_restart_venue.place(_order(symbol=symbol))
            for symbol in ("RELIANCE", "INFY", "TCS")
        ]

        # The process dies here. Nothing of the venue survives — that is what makes it a restart.
        after_restart_venue = SimulatedOrderExecutionVenue()
        after_restart = [
            after_restart_venue.place(_order(symbol=symbol))
            for symbol in ("HDFCBANK", "ITC", "SBIN")
        ]

        assert set(before_restart).isdisjoint(after_restart), (
            "a restarted paper session reissued an order id from before the restart"
        )
        assert len(set(before_restart + after_restart)) == len(before_restart + after_restart)
        # The placement ordinals DO repeat across the restart — they are for readability, not for
        # uniqueness — which is why the content digest has to be the part that carries identity.
        assert [order_id.rsplit("-", maxsplit=1)[1] for order_id in before_restart] == [
            order_id.rsplit("-", maxsplit=1)[1] for order_id in after_restart
        ]

    @pytest.mark.adversarial
    def test_a_restarted_session_cannot_reissue_a_trade_id_either(self) -> None:
        """The trade id carried the identical defect, and a repeated one mis-attributes a fill.

        A trade id is how a fill finds its order in the journal. Keyed on a process-local counter,
        the first fill after a restart reused the first fill of the morning, which attaches real
        filled quantity to the wrong order — the same class of damage as a repeated order id, one
        level down. Deriving it from the ORDER's id plus the fill's position within that order
        makes it unique wherever the order id is, and greppable back to its parent besides.
        """
        before_restart_venue = SimulatedOrderExecutionVenue()
        before_restart_venue.observe_book(book())
        before_restart_venue.place(_order(symbol="RELIANCE"))
        before_restart = [trade.broker_trade_id for trade in _poll(before_restart_venue, 3)]

        after_restart_venue = SimulatedOrderExecutionVenue()
        after_restart_venue.observe_book(book())
        after_restart_venue.place(_order(symbol="INFY"))
        after_restart = [trade.broker_trade_id for trade in _poll(after_restart_venue, 3)]

        assert before_restart and after_restart
        assert set(before_restart).isdisjoint(after_restart)

    def test_trade_ids_carry_their_own_prefix(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order())
        trades = _poll(venue, 2)
        assert trades
        assert all(trade.broker_trade_id.startswith(SIMULATED_TRADE_ID_PREFIX) for trade in trades)

    @pytest.mark.adversarial
    def test_an_id_from_another_namespace_is_refused_rather_than_answered(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        with pytest.raises(UnknownSimulatedOrderError):
            venue.cancel("250813001234567", variety="regular")


class TestItRefusesToFillWithoutABook:
    def test_an_order_with_no_book_is_acknowledged_opened_and_left_alone(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        assert venue.fetch_order_history(order_id)[0].status == STATUS_ACKNOWLEDGED
        trades = _poll(venue, 5)
        assert trades == []
        report = venue.fetch_orders(session_date=_SESSION)[0]
        assert report.status == STATUS_OPEN
        assert report.filled_quantity == 0

    def test_the_reason_it_did_not_fill_is_readable(self) -> None:
        """A paper run that produced no fills must be distinguishable from a strategy that produced
        no signals."""
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        _poll(venue, 3)
        reason = venue.refusal_to_fill_reason_for(order_id)
        assert "no book has been supplied" in reason
        assert "refuses to invent a price" in reason

    def test_no_book_means_no_expectation_either(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        assert venue.expected_fill_for(order_id) is None

    @pytest.mark.adversarial
    def test_a_book_for_a_different_instrument_does_not_fill_this_order(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book(instrument_token=_INSTRUMENT + 1))
        order_id = venue.place(_order())
        assert _poll(venue, 3) == []
        assert "no book has been supplied" in venue.refusal_to_fill_reason_for(order_id)


class TestPartialFillsArriveAcrossPolls:
    def test_the_ladder_is_consumed_one_rung_per_poll_at_its_real_prices(self) -> None:
        """300 units against 100 @ 10,100 / 200 @ 10,200 — the first poll opens the order, then one
        rung per poll, and the prices are the book's own rather than a model's."""
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=300))

        assert _poll(venue) == []  # the acknowledgement becomes OPEN
        assert venue.fetch_orders(session_date=_SESSION)[0].status == STATUS_OPEN

        first = _poll(venue)
        assert [(trade.quantity, trade.price_paise) for trade in first] == [(100, Decimal(10_100))]
        partial = venue.fetch_orders(session_date=_SESSION)[0]
        assert partial.filled_quantity == 100
        assert partial.pending_quantity == 200
        # Kite has no partially-filled status (`docs/research/222` §2): a part-filled order reads
        # OPEN with a non-zero filled quantity, and the fill ledger derives the rest.
        assert partial.status == STATUS_OPEN

        second = _poll(venue)
        assert [(trade.quantity, trade.price_paise) for trade in second] == [(200, Decimal(10_200))]
        complete = venue.fetch_orders(session_date=_SESSION)[0]
        assert complete.status == STATUS_COMPLETE
        assert complete.filled_quantity == 300
        assert complete.average_price_paise == Decimal(10_200 * 200 + 10_100 * 100) / Decimal(300)
        assert venue.fetch_order_history(order_id)[-1].status == STATUS_COMPLETE

    def test_a_completed_order_stops_producing_trades(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(quantity=300))
        _poll(venue, 3)
        assert _poll(venue, 3) == []

    def test_the_trades_it_reports_reconcile_with_the_order_it_reports(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=300))
        _poll(venue, 3)
        trades = venue.fetch_trades(session_date=_SESSION)
        assert {trade.broker_order_id for trade in trades} == {order_id}
        assert sum(trade.quantity for trade in trades) == 300

    @pytest.mark.adversarial
    def test_a_limit_price_inside_the_book_stops_the_fill_where_the_limit_stops(self) -> None:
        """A buy limited to 10,100 may lift only the touch. The remaining 200 stay open, and the
        venue says why rather than reaching for a rung the order cannot pay for."""
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(_limit(10_100), quantity=300))
        _poll(venue, 4)
        report = venue.fetch_orders(session_date=_SESSION)[0]
        assert report.filled_quantity == 100
        assert report.status == STATUS_OPEN
        assert "exhausted" in venue.refusal_to_fill_reason_for(order_id)

    @pytest.mark.adversarial
    def test_an_exhausted_ladder_waits_for_a_new_book_rather_than_inventing_depth(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        thin = book(asks=((10_100, 50),))
        venue.observe_book(thin)
        order_id = venue.place(_order(quantity=300))
        _poll(venue, 4)
        assert venue.fetch_orders(session_date=_SESSION)[0].filled_quantity == 50
        assert "inventing depth" in venue.refusal_to_fill_reason_for(order_id)

        venue.observe_book(
            book(
                asks=((10_150, 250),),
                receipt_time=_AN_INSTANT + timedelta(seconds=30),
                receipt_sequence=2,
            )
        )
        _poll(venue, 1, start=_AN_INSTANT + timedelta(minutes=1))
        assert venue.fetch_orders(session_date=_SESSION)[0].filled_quantity == 300

    def test_a_sell_walks_the_bid_ladder(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(_limit(9_700), quantity=300, side=TradeLeg.SELL))
        trades = _poll(venue, 3)
        assert [(trade.quantity, trade.price_paise) for trade in trades] == [
            (100, Decimal(9_900)),
            (200, Decimal(9_800)),
        ]

    def test_the_expected_cost_is_recorded_at_placement_but_never_used_to_fill(self) -> None:
        """`F01`'s model prices the order when it is placed; the fills still come from the ladder.
        Keeping both is what lets a paper session measure realised against expected."""
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=300))
        expectation = venue.expected_fill_for(order_id)
        assert expectation is not None
        assert expectation.quantity == 300
        _poll(venue, 3)
        realised = venue.fetch_orders(session_date=_SESSION)[0].average_price_paise
        assert realised == Decimal(10_100 * 100 + 10_200 * 200) / Decimal(300)
        # Inside the visible book the two AGREE exactly, because `F01` prices an uncensored order
        # by walking the same ladder this venue fills from — which is the strongest available
        # evidence that the simulator is not making up its own execution arithmetic. They diverge
        # only once the order exceeds visible depth, where the model extrapolates and this venue
        # refuses to.
        assert expectation.expected_price_paise == realised


class TestTriggersAndCancellation:
    def test_a_stop_order_waits_at_trigger_pending_until_the_price_reaches_it(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book(last_price_paise=10_000))
        stop = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.STOP_LOSS_LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=Decimal(10_300),
            trigger_price_paise=Decimal(10_250),
        )
        venue.place(_order(stop, quantity=100))
        _poll(venue, 3)
        assert venue.fetch_orders(session_date=_SESSION)[0].status == STATUS_TRIGGER_PENDING

        venue.observe_book(
            book(
                last_price_paise=10_260,
                receipt_time=_AN_INSTANT + timedelta(seconds=30),
                receipt_sequence=2,
            )
        )
        _poll(venue, 2)
        assert venue.fetch_orders(session_date=_SESSION)[0].filled_quantity == 100

    def test_a_cancellation_is_terminal_and_recorded(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=300))
        _poll(venue, 2)
        venue.cancel(order_id, variety="regular")
        report = venue.fetch_orders(session_date=_SESSION)[0]
        assert report.status == STATUS_CANCELLED
        assert report.filled_quantity == 100  # the part that filled before the cancel survives
        assert _poll(venue, 3) == []

    @pytest.mark.adversarial
    def test_cancelling_a_completed_order_is_refused(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=100))
        _poll(venue, 2)
        with pytest.raises(VenueRejectedError):
            venue.cancel(order_id, variety="regular")

    @pytest.mark.adversarial
    def test_cancelling_under_the_wrong_variety_is_refused_because_kite_routes_by_variety(
        self,
    ) -> None:
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        with pytest.raises(VenueRejectedError):
            venue.cancel(order_id, variety="amo")


class TestModification:
    def test_a_reprice_reopens_the_ladder_the_new_price_can_reach(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(_limit(10_100), quantity=300))
        _poll(venue, 3)
        assert venue.fetch_orders(session_date=_SESSION)[0].filled_quantity == 100
        venue.modify(order_id, _limit(10_300), 300)
        _poll(venue, 2)
        assert venue.fetch_orders(session_date=_SESSION)[0].filled_quantity == 300

    @pytest.mark.adversarial
    def test_the_modification_cap_is_the_same_twenty_five_the_real_venue_enforces(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        order_id = venue.place(_order())
        for _ in range(MAXIMUM_MODIFICATIONS_PER_ORDER):
            venue.modify(order_id, _limit(), 300)
        with pytest.raises(VenueRejectedError):
            venue.modify(order_id, _limit(), 300)

    @pytest.mark.adversarial
    def test_an_order_cannot_be_resized_below_what_it_has_already_filled(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        order_id = venue.place(_order(quantity=300))
        _poll(venue, 2)
        with pytest.raises(VenueRejectedError):
            venue.modify(order_id, _limit(), 50)


class TestTheRejectionsItDoesAndDoesNotModel:
    def test_a_quantity_above_the_configured_freeze_limit_is_rejected(self) -> None:
        venue = SimulatedOrderExecutionVenue(
            limits=SimulatedVenueLimits(freeze_quantity_by_trading_symbol={"RELIANCE": 250})
        )
        with pytest.raises(FreezeQuantityExceededError) as rejection:
            venue.place(_order(quantity=300))
        assert "RMS:Rule: Check freeze quantity" in str(rejection.value)
        assert rejection.value.raw_message.startswith("RMS:Rule:")

    def test_a_quantity_at_the_limit_is_accepted(self) -> None:
        venue = SimulatedOrderExecutionVenue(
            limits=SimulatedVenueLimits(freeze_quantity_by_trading_symbol={"RELIANCE": 300})
        )
        assert venue.place(_order(quantity=300))

    @pytest.mark.adversarial
    def test_an_unconfigured_instrument_models_no_freeze_limit_rather_than_a_guessed_one(
        self,
    ) -> None:
        """NSE revises freeze quantities per underlying and periodically; a default compiled in
        here would reject orders the real venue accepts, silently."""
        venue = SimulatedOrderExecutionVenue(
            limits=SimulatedVenueLimits(freeze_quantity_by_trading_symbol={"INFY": 10})
        )
        assert venue.place(_order(quantity=1_000_000))

    def test_the_single_order_cap_is_configured_and_off_by_default(self) -> None:
        assert SimulatedVenueLimits().single_order_quantity_cap is None
        capped = SimulatedVenueLimits(single_order_quantity_cap=100_000)
        assert capped.rejection_for("RELIANCE", 100_001) is not None
        assert capped.rejection_for("RELIANCE", 100_000) is None

    @pytest.mark.adversarial
    def test_a_live_namespace_order_is_refused_by_the_simulator(self) -> None:
        """The mirror of the live venue's refusal of a simulated order: an order named LIVE with a
        simulated fill history is a position no money stands behind."""
        venue = SimulatedOrderExecutionVenue()
        with pytest.raises(SimulatedVenueRefusedLiveOrderError):
            venue.place(_order(namespace=OrderNamespace.LIVE))


class TestPositionsSayWhatTheyAreNot:
    def test_a_position_is_derived_from_the_fills_and_labelled_as_simulated(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(quantity=300))
        _poll(venue, 3)
        positions = venue.fetch_positions()
        assert len(positions) == 1
        assert positions[0].trading_symbol == "RELIANCE"
        assert positions[0].quantity == 300
        assert positions[0].product == "MIS"

    def test_the_view_states_that_margin_and_settlement_are_not_modelled(self) -> None:
        """Stated in the DATA, not only in a docstring: every downstream reader of a position sees
        the caveat, including one that never opens this module."""
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(quantity=100))
        _poll(venue, 2)
        position = venue.fetch_positions()[0]
        assert position.view == SIMULATED_POSITION_VIEW
        assert "no_margin" in position.view
        assert position.unsettled_quantity == 0

    def test_a_round_trip_nets_to_flat_with_no_average_price(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(quantity=100))
        _poll(venue, 2)
        venue.place(_order(_limit(9_900), quantity=100, side=TradeLeg.SELL))
        _poll(venue, 3)
        position = venue.fetch_positions()[0]
        assert position.quantity == 0
        assert position.average_price_paise is None

    @pytest.mark.adversarial
    def test_an_empty_venue_reports_no_positions_rather_than_a_zero_row(self) -> None:
        assert SimulatedOrderExecutionVenue().fetch_positions() == ()


class TestSessionScoping:
    def test_orders_and_trades_are_scoped_to_the_session_they_belong_to(self) -> None:
        venue = SimulatedOrderExecutionVenue()
        venue.observe_book(book())
        venue.place(_order(quantity=100))
        _poll(venue, 2)
        assert len(venue.fetch_orders(session_date=_SESSION)) == 1
        assert len(venue.fetch_trades(session_date=_SESSION)) == 1
        assert venue.fetch_orders(session_date=date(2026, 8, 12)) == ()
        assert venue.fetch_trades(session_date=date(2026, 8, 12)) == ()

    def test_a_session_is_the_ist_date_of_the_order_not_the_utc_one(self) -> None:
        """An order decided at 20:00 UTC is 01:30 IST the NEXT day; filing it under the UTC date
        would put it in a session whose reconciliation never looks at it."""
        venue = SimulatedOrderExecutionVenue()
        venue.place(_order(at=datetime(2026, 8, 13, 20, 0, tzinfo=UTC)))
        assert venue.fetch_orders(session_date=date(2026, 8, 14))
        assert venue.fetch_orders(session_date=date(2026, 8, 13)) == ()
