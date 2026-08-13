"""One test per defect the `R.23(c)` adversarial review reproduced, 2026-08-13.

The review found five criticals and four majors behind a fully green suite of 367 tests, and its
most valuable finding was that two of those tests asserted a defect outright. Everything here is
written from its reproductions, so each test fails against the code as it stood before the fix.

The pattern in the criticals is worth stating once: **four of the five were cases where an
exceptional answer was quietly turned into an ordinary one** — an unreadable payload read as "no
orders", a dropped connection read as "never sent", an illegal transition read as a conflict to be
logged, an inference left standing when the fact arrived. The fifth was an ordering bug in the fold.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.broker_truth_reconciler import (
    BrokerTruthReconciler,
    ReconciliationRefusedError,
    ReconciliationVerdict,
)
from nse_algo_trader.order_path.crash_safe_order_placer import (
    CrashSafeOrderPlacer,
    PlacementVerdict,
)
from nse_algo_trader.order_path.kite_order_execution_venue import classify_kite_exception
from nse_algo_trader.order_path.order_execution_venue import (
    VenueOrderReport,
    VenueOutcomeUnknownError,
    VenuePositionReport,
    VenueTradeReport,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    LifecycleEvent,
    OrderLifecycleState,
    next_state,
)
from nse_algo_trader.order_path.order_record import FillRecord, OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = [pytest.mark.adversarial, pytest.mark.hermetic]

_AT = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)


def _intent(quantity: int = 100, symbol: str = "RELIANCE") -> TradingIntent:
    return TradingIntent(
        strategy_identity="review_regression.v1",
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_AT,
        reference_price_paise=Decimal("282500"),
        expected_edge_bps=Decimal("34.1"),
    )


def _expression() -> OrderExpression:
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal("282500"),
    )


def _fill(trade_id: str, quantity: int, price: str, second: int) -> FillRecord:
    return FillRecord(
        broker_trade_id=trade_id,
        quantity=quantity,
        price_paise=Decimal(price),
        occurred_at=_AT + timedelta(seconds=second),
        source=EventSource.BROKER_REPORTED,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[OrderIntentJournal]:
    with OrderIntentJournal(tmp_path / "order_path.sqlite3") as opened:
        yield opened


class VenueUnderTest:
    """A venue whose answers can be moved between reconciliation passes, as a real one's are."""

    def __init__(self) -> None:
        self.orders: list[VenueOrderReport] = []
        self.trades: list[VenueTradeReport] = []
        self.placed: list[OrderRecord] = []
        self.answer_orders_with: object | None = None

    @property
    def venue_name(self) -> str:
        return "under_test"

    def place(self, order: OrderRecord) -> str:
        self.placed.append(order)
        return f"25081300{len(self.placed):04d}"

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None: ...

    def cancel(self, broker_order_id: str, *, variety: str) -> None: ...

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        del session_date
        if self.answer_orders_with is not None:
            raise VenueOutcomeUnknownError("the broker's answer was not a list of rows")
        return tuple(self.orders)

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        return tuple(o for o in self.orders if o.broker_order_id == broker_order_id)

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        del session_date
        return tuple(self.trades)

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]:
        return ()


def _report(
    tag: str,
    *,
    status: str,
    filled: int,
    average_paise: Decimal | None = None,
    order_id: str = "250813000001",
) -> VenueOrderReport:
    return VenueOrderReport(
        broker_order_id=order_id,
        tag=tag,
        status=status,
        trading_symbol="RELIANCE",
        quantity=100,
        filled_quantity=filled,
        pending_quantity=100 - filled,
        average_price_paise=average_paise,
        status_message="",
        status_message_raw="",
        exchange_timestamp=_AT + timedelta(seconds=1),
    )


class TestC1TheFoldSurvivesAFillFollowedByATerminalEvent:
    def test_a_part_filled_order_that_is_cancelled_can_still_be_read_back(
        self, journal: OrderIntentJournal
    ) -> None:
        """The most ordinary end-of-day event in an intraday book.

        The fold used to replay every event and only then apply the fills, so it reached CANCELLED
        and then tried to apply a fill out of a terminal state. The exception escaped `load_order`,
        and with it `orders_for_session`, `open_orders`, `state_of`, `reconcile` and the whole
        `/orders` page — permanently, for a session with a real fill on disk.
        """
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_event(intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT)
        journal.record_event(
            intent.intent_id,
            LifecycleEvent.ACKNOWLEDGED,
            EventSource.BROKER_REPORTED,
            at=_AT + timedelta(seconds=1),
        )
        journal.record_fill(intent.intent_id, _fill("T1", 40, "282400", second=2))
        journal.record_event(
            intent.intent_id,
            LifecycleEvent.CANCELLED,
            EventSource.BROKER_REPORTED,
            at=_AT + timedelta(seconds=3),
            note="cancelled at the close",
        )

        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert order.state is OrderLifecycleState.CANCELLED
        assert order.filled_quantity == 40, "the fill happened, and it is still on the book"
        assert journal.open_orders(_SESSION) == ()

    def test_the_same_holds_for_a_rejection_after_a_fill(
        self, journal: OrderIntentJournal
    ) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_event(intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT)
        journal.record_fill(intent.intent_id, _fill("T1", 10, "282400", second=2))
        journal.record_event(
            intent.intent_id,
            LifecycleEvent.REJECTED,
            EventSource.BROKER_REPORTED,
            at=_AT + timedelta(seconds=3),
        )
        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert (order.state, order.filled_quantity) == (OrderLifecycleState.REJECTED, 10)


class TestC2ADroppedConnectionIsNotProofTheOrderWasNeverSent:
    @pytest.mark.parametrize(
        "error",
        [
            requests.exceptions.ConnectionError("connection reset by peer"),
            ConnectionResetError("reset by peer"),
        ],
    )
    def test_a_reset_connection_is_unknown_and_therefore_never_resent(
        self, error: BaseException
    ) -> None:
        """`requests` raises ConnectionError for "refused" AND for a server that took the request
        and then died. The class cannot tell them apart, and only one of the two is safe to retry.
        """
        assert isinstance(
            classify_kite_exception(error, attempted="place"), VenueOutcomeUnknownError
        )

    def test_one_decision_reaches_the_venue_once_when_the_connection_drops(
        self, journal: OrderIntentJournal
    ) -> None:
        """The review reproduced THREE broker calls for one intent through this path."""

        class DropsTheConnection(VenueUnderTest):
            def place(self, order: OrderRecord) -> str:
                self.placed.append(order)
                raise classify_kite_exception(
                    requests.exceptions.ConnectionError("reset by peer"), attempted="place"
                )

        venue = DropsTheConnection()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(_intent(), _expression(), now=_AT)
        assert outcome.verdict is PlacementVerdict.AMBIGUOUS
        assert len(venue.placed) == 1, "a request that may have landed must never be sent again"


class TestC3AnInferenceIsWithdrawnWhenTheFactArrives:
    def test_the_real_trades_supersede_the_inference_instead_of_adding_to_it(
        self, journal: OrderIntentJournal
    ) -> None:
        """`orders().filled_quantity` moves before `trades()` lists the executions, so the first
        pass legitimately infers. The second pass must not then double-count."""
        intent = _intent()
        venue = VenueUnderTest()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(intent, _expression(), now=_AT)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        broker_order_id = outcome.broker_order_id or ""

        venue.orders = [
            _report(
                order.broker_tag,
                status="OPEN",
                filled=60,
                average_paise=Decimal("282520"),
                order_id=broker_order_id,
            )
        ]
        reconciler = BrokerTruthReconciler(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        first = reconciler.reconcile(session_date=_SESSION, now=_AT + timedelta(seconds=30))
        assert first.reconciliations[0].verdict is ReconciliationVerdict.QUANTITY_GAP_PATCHED
        inferred = journal.load_order(outcome.intent_id)
        assert inferred is not None
        assert (inferred.filled_quantity, inferred.has_inferred_events) == (60, True)

        venue.trades = [
            VenueTradeReport(
                broker_trade_id="T-real-1",
                broker_order_id=broker_order_id,
                trading_symbol="RELIANCE",
                quantity=60,
                price_paise=Decimal("282520"),
                filled_at=_AT + timedelta(seconds=20),
            )
        ]
        reconciler.reconcile(session_date=_SESSION, now=_AT + timedelta(seconds=60))

        settled = journal.load_order(outcome.intent_id)
        assert settled is not None
        assert settled.filled_quantity == 60, (
            "the units were counted once, by the broker's own trade"
        )
        assert not settled.has_inferred_events, "the inference must be withdrawn, not left standing"
        assert journal.inferred_fill_count(outcome.intent_id) == 0
        assert journal.inferred_fill_count(outcome.intent_id, include_superseded=True) == 1, (
            "the withdrawn inference stays on disk — it is part of the audit trail"
        )


class TestC4AnUnreadableAnswerIsNotAnEmptyBook:
    def test_reconciliation_refuses_when_the_broker_cannot_be_read(
        self, journal: OrderIntentJournal
    ) -> None:
        """The review reproduced an AMBIGUOUS order — possibly live at Zerodha — being written off
        as ABANDONED because an unparseable payload read as "no orders"."""
        for index in range(25):
            journal.record_visibility_delay(
                f"observed-{index}",
                submitted_at=_AT,
                first_seen_at=_AT + timedelta(seconds=0.5),
            )
        venue = VenueUnderTest()
        venue.answer_orders_with = "not a list of rows"
        with pytest.raises(ReconciliationRefusedError, match="could not be read"):
            BrokerTruthReconciler(
                journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
            ).reconcile(session_date=_SESSION, now=_AT + timedelta(hours=1))


class TestC5TheCommonestStateIsNotADisagreement:
    def test_a_part_filled_order_the_broker_still_calls_open_is_agreement(
        self, journal: OrderIntentJournal
    ) -> None:
        """Kite has no partially-filled status: a part-filled order reads OPEN with a non-zero
        filled quantity. Reporting that as a conflict buried the real conflicts."""
        intent = _intent()
        venue = VenueUnderTest()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(intent, _expression(), now=_AT)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        broker_order_id = outcome.broker_order_id or ""
        venue.orders = [
            _report(order.broker_tag, status="OPEN", filled=40, order_id=broker_order_id)
        ]
        venue.trades = [
            VenueTradeReport(
                broker_trade_id="T1",
                broker_order_id=broker_order_id,
                trading_symbol="RELIANCE",
                quantity=40,
                price_paise=Decimal("282400"),
                filled_at=_AT + timedelta(seconds=5),
            )
        ]
        reconciler = BrokerTruthReconciler(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        for pass_number in range(1, 4):
            report = reconciler.reconcile(
                session_date=_SESSION, now=_AT + timedelta(seconds=30 * pass_number)
            )
            assert report.reconciliations[0].verdict is ReconciliationVerdict.AGREED, (
                f"pass {pass_number} reported the ordinary case as a disagreement: "
                f"{report.reconciliations[0].detail}"
            )

    def test_the_transition_exists_in_the_table_itself(self) -> None:
        assert (
            next_state(OrderLifecycleState.PARTIALLY_FILLED, LifecycleEvent.OPENED)
            is OrderLifecycleState.PARTIALLY_FILLED
        )
        assert (
            next_state(OrderLifecycleState.WORKING, LifecycleEvent.OPENED)
            is OrderLifecycleState.WORKING
        )


class TestM1ADuplicateDecisionDoesNotWedgeTheJournal:
    def test_the_write_lock_is_released_after_a_duplicate(self, tmp_path: Path) -> None:
        """Python's sqlite3 issues BEGIN before the failing INSERT. Without a rollback the WAL
        write lock was held for the life of the connection, and any other writer — a concurrent
        reconciler recording a real fill — blocked for the full busy timeout and then failed."""
        path = tmp_path / "order_path.sqlite3"
        with OrderIntentJournal(path) as first:
            assert first.record_intent(_intent(), _expression(), OrderNamespace.SIMULATED, at=_AT)
            assert not first.record_intent(
                _intent(), _expression(), OrderNamespace.SIMULATED, at=_AT
            )
            other = sqlite3.connect(path, timeout=1.0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()


class TestM2TheGapIsPricedAtTheUnitsBeingInvented:
    def test_the_inferred_price_solves_the_residual_not_the_average(
        self, journal: OrderIntentJournal
    ) -> None:
        """The broker's average covers ALL filled units; the inference covers only the missing
        ones. Reusing the average booked a cost basis that contradicted the broker's own number —
        measured at ~0.9% on a 2,850-rupee stock."""
        intent = _intent()
        venue = VenueUnderTest()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(intent, _expression(), now=_AT)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        broker_order_id = outcome.broker_order_id or ""
        journal.record_fill(outcome.intent_id, _fill("T1", 50, "282500", second=2))

        venue.orders = [
            _report(
                order.broker_tag,
                status="COMPLETE",
                filled=100,
                average_paise=Decimal("285000"),
                order_id=broker_order_id,
            )
        ]
        BrokerTruthReconciler(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        ).reconcile(session_date=_SESSION, now=_AT + timedelta(seconds=30))

        patched = journal.load_order(outcome.intent_id)
        assert patched is not None
        assert patched.filled_quantity == 100
        # 100 x 285000 - 50 x 282500 = 28,500,000 - 14,125,000 = 14,375,000 over 50 units.
        inferred = [fill for fill in patched.fills if fill.source is EventSource.INFERRED]
        assert len(inferred) == 1
        assert inferred[0].price_paise == Decimal("287500")
        assert patched.average_fill_price_paise == Decimal("285000"), (
            "the local average must now agree with the broker's own number"
        )


class TestM3TheHorizonRestsOnRealObservations:
    def test_one_intent_contributes_one_observation(self, journal: OrderIntentJournal) -> None:
        """A single order whose status Kite adds was re-observed on every pass, and each pass
        recorded the order's AGE — 25 passes over one order inflated the horizon to 11.4 hours."""
        for _ in range(25):
            journal.record_visibility_delay(
                "one-intent", submitted_at=_AT, first_seen_at=_AT + timedelta(seconds=1)
            )
        assert journal.visibility_delays_seconds() == (1.0,)


class TestM4TheLiveReconcilerDoesNotJudgePaperOrders:
    def test_a_simulated_order_is_invisible_to_a_live_reconciliation(
        self, journal: OrderIntentJournal
    ) -> None:
        """The review reproduced the live reconciler writing a terminal inferred event onto a
        SIMULATED order."""
        simulated = _intent(symbol="INFY")
        journal.record_intent(simulated, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_event(
            simulated.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT
        )
        for index in range(25):
            journal.record_visibility_delay(
                f"observed-{index}", submitted_at=_AT, first_seen_at=_AT + timedelta(seconds=0.5)
            )
        report = BrokerTruthReconciler(
            journal=journal, venue=VenueUnderTest(), namespace=OrderNamespace.LIVE
        ).reconcile(session_date=_SESSION, now=_AT + timedelta(hours=1))
        assert report.reconciliations == ()
        assert journal.state_of(simulated.intent_id) is OrderLifecycleState.SUBMISSION_IN_FLIGHT


class TestMinor3EveryDispatchAttemptIsOnTheRecord:
    def test_three_calls_leave_three_attempt_rows(self, journal: OrderIntentJournal) -> None:
        """The crash record has to be able to say "I may have created more than one order".

        One row covering three calls could not, and the restart that reads it would look for a
        single order. Each attempt now opens and closes its own row, and a failure that provably
        never reached the venue is recorded as NOT_SENT rather than as an unknown.
        """

        class NeverReachesTheVenue(VenueUnderTest):
            def place(self, order: OrderRecord) -> str:
                self.placed.append(order)
                raise VenueUnavailableError("connection refused before anything was sent")

        venue = NeverReachesTheVenue()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(_intent(), _expression(), now=_AT)
        assert outcome.verdict is PlacementVerdict.AMBIGUOUS
        assert len(venue.placed) == 3
        assert outcome.attempts == 3
        assert journal.in_flight_submissions(session_date=_SESSION) == (), (
            "every attempt must be closed out, or a restart re-opens a question already answered"
        )
