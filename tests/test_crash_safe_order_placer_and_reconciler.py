"""F02's two claims, tested against a fake venue that can fail in each of the three real ways.

Claim 1 — **one intent becomes exactly one order**, across retries, restarts and a timeout that
hides the broker's answer.
Claim 2 — **the broker is believed**, and every disagreement is recorded rather than smoothed away.

The fake venue exists because the real one cannot be made to time out on demand. Per `R.05` this is
functional verification only: the real-fill probe is deferred by operator decision (`A.99`) and is
carried in `BACKLOG.md` as an open blocker.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

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
from nse_algo_trader.order_path.order_execution_venue import (
    VenueOrderReport,
    VenueOutcomeUnknownError,
    VenuePositionReport,
    VenueRejectedError,
    VenueTradeReport,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import OrderLifecycleState
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = [pytest.mark.unit, pytest.mark.hermetic]

_NOW = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)


@dataclass(slots=True)
class FakeVenue:
    """A venue that can be told exactly how to fail, which the real one cannot be."""

    behaviour: str = "accept"
    placed: list[OrderRecord] = field(default_factory=list)
    orders: list[VenueOrderReport] = field(default_factory=list)
    trades: list[VenueTradeReport] = field(default_factory=list)
    unavailable_before_success: int = 0
    fetch_fails: bool = False

    @property
    def venue_name(self) -> str:
        return "fake"

    def place(self, order: OrderRecord) -> str:
        self.placed.append(order)
        if self.unavailable_before_success > 0:
            self.unavailable_before_success -= 1
            raise VenueUnavailableError("connection refused before dispatch")
        if self.behaviour == "timeout":
            raise VenueOutcomeUnknownError("read timeout after 7s")
        if self.behaviour == "reject":
            raise VenueRejectedError(
                "rejected", raw_message="RMS:Rule: Check freeze quantity for NSE CASH"
            )
        if self.behaviour == "unavailable":
            raise VenueUnavailableError("connection refused before dispatch")
        return f"2508130000{len(self.placed):02d}"

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None: ...

    def cancel(self, broker_order_id: str, *, variety: str) -> None: ...

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        if self.fetch_fails:
            raise VenueUnavailableError("the broker did not answer")
        del session_date
        return tuple(self.orders)

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        return tuple(order for order in self.orders if order.broker_order_id == broker_order_id)

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        del session_date
        return tuple(self.trades)

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]:
        return ()


def _intent(quantity: int = 100, symbol: str = "RELIANCE") -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_NOW,
        reference_price_paise=Decimal("142350"),
        expected_edge_bps=Decimal("34.1"),
        horizon_minutes=5,
    )


def _expression() -> OrderExpression:
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal("142350"),
    )


def _report(
    tag: str,
    *,
    status: str = "OPEN",
    filled: int = 0,
    quantity: int = 100,
    order_id: str = "250813000001",
    average_paise: Decimal | None = None,
) -> VenueOrderReport:
    return VenueOrderReport(
        broker_order_id=order_id,
        tag=tag,
        status=status,
        trading_symbol="RELIANCE",
        quantity=quantity,
        filled_quantity=filled,
        pending_quantity=quantity - filled,
        average_price_paise=average_paise,
        status_message="",
        status_message_raw="",
        exchange_timestamp=_NOW,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[OrderIntentJournal]:
    with OrderIntentJournal(tmp_path / "order_path.sqlite3") as opened:
        yield opened


def _reconciler(journal: OrderIntentJournal, venue: FakeVenue) -> BrokerTruthReconciler:
    """Reconciling the SIMULATED namespace, because that is the namespace these orders carry."""
    return BrokerTruthReconciler(journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED)


def _placer(journal: OrderIntentJournal, venue: FakeVenue) -> CrashSafeOrderPlacer:
    return CrashSafeOrderPlacer(journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED)


class TestOneIntentBecomesOneOrder:
    def test_the_second_attempt_at_the_same_decision_never_reaches_the_broker(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue()
        placer = _placer(journal, venue)
        first = placer.place(_intent(), _expression(), now=_NOW)
        second = placer.place(_intent(), _expression(), now=_NOW)
        assert first.verdict is PlacementVerdict.PLACED
        assert second.verdict is PlacementVerdict.ALREADY_PLACED
        assert len(venue.placed) == 1

    def test_a_restart_does_not_resend_what_is_already_at_the_broker(self, tmp_path: Path) -> None:
        """The journal is on disk and the intent names itself, so a process with no memory still
        recognises its own decision."""
        venue = FakeVenue()
        path = tmp_path / "order_path.sqlite3"
        with OrderIntentJournal(path) as first_life:
            _placer(first_life, venue).place(_intent(), _expression(), now=_NOW)
        with OrderIntentJournal(path) as second_life:
            outcome = _placer(second_life, venue).place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.ALREADY_PLACED
        assert len(venue.placed) == 1

    def test_a_timeout_is_not_retried_and_leaves_the_order_ambiguous(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue(behaviour="timeout")
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.AMBIGUOUS
        assert outcome.needs_reconciliation
        assert len(venue.placed) == 1, "a timeout must never be retried"
        assert journal.state_of(outcome.intent_id) is OrderLifecycleState.AMBIGUOUS

    def test_an_outage_before_dispatch_is_retried_because_it_cannot_have_landed(
        self, journal: OrderIntentJournal
    ) -> None:
        """Three broker calls for one intent is correct ONLY for a failure that provably preceded
        transmission — a refused TCP handshake, an unresolved name, a TLS failure.

        The `R.23(c)` review was right that this test blesses the number without exercising the
        classification that earns it. The classification is now tested where it belongs, on real
        exception types, in `test_kite_order_execution_venue.py` and in
        `test_order_path_review_regressions.py::TestC2...` — which asserts the opposite outcome
        for a dropped connection, the case that used to land here and duplicate the order.
        """
        venue = FakeVenue(unavailable_before_success=2)
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.PLACED
        assert len(venue.placed) == 3

    def test_an_outage_that_outlasts_the_retries_becomes_ambiguous_not_failed(
        self, journal: OrderIntentJournal
    ) -> None:
        """After the last attempt the question is the same one a timeout leaves behind."""
        venue = FakeVenue(behaviour="unavailable")
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.AMBIGUOUS

    def test_a_rejection_is_terminal_and_carries_the_brokers_own_words(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue(behaviour="reject")
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.REJECTED_BY_VENUE
        assert "freeze quantity" in outcome.reason
        assert journal.state_of(outcome.intent_id) is OrderLifecycleState.REJECTED


class TestTheGatesComeFirst:
    def test_a_latched_switch_stops_the_order_before_anything_is_written(
        self, journal: OrderIntentJournal
    ) -> None:
        class Latched:
            def permits_submission(self, namespace: OrderNamespace) -> tuple[bool, str]:
                del namespace
                return False, "halted by the operator at 09:20"

        venue = FakeVenue()
        placer = CrashSafeOrderPlacer(
            journal=journal,
            venue=venue,
            namespace=OrderNamespace.SIMULATED,
            control_gate=Latched(),
        )
        outcome = placer.place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.REFUSED_BY_LATCH
        assert venue.placed == []
        assert not journal.has_intent(outcome.intent_id)

    def test_an_intent_that_ages_out_in_the_rate_queue_expires_instead_of_being_sent_late(
        self, journal: OrderIntentJournal
    ) -> None:
        class TooBusy:
            def acquire(self, exchange: str, deadline: datetime) -> tuple[bool, str]:
                del exchange, deadline
                return False, "the per-second budget could not be met before the intent expired"

        venue = FakeVenue()
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED, rate_gate=TooBusy()
        )
        outcome = placer.place(_intent(), _expression(), now=_NOW)
        assert outcome.verdict is PlacementVerdict.REFUSED_BY_RATE_GATE
        assert venue.placed == []
        assert journal.state_of(outcome.intent_id) is OrderLifecycleState.EXPIRED


class TestTheBrokerIsBelieved:
    def test_an_order_the_broker_does_not_know_about_is_left_unresolved_without_evidence(
        self, journal: OrderIntentJournal
    ) -> None:
        """With too few observed appearance delays there is no horizon, so nothing is declared
        abandoned. Refusing to conclude is the honest answer."""
        venue = FakeVenue(behaviour="timeout")
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        report = _reconciler(journal, venue).reconcile(
            session_date=_SESSION, now=_NOW + timedelta(minutes=30)
        )
        assert report.has_unresolved
        assert not report.horizon.is_established
        assert report.reconciliations[0].intent_id == outcome.intent_id

    def test_once_the_horizon_is_measured_an_absent_order_is_abandoned_not_resent(
        self, journal: OrderIntentJournal
    ) -> None:
        for index in range(25):
            journal.record_visibility_delay(
                f"observed-{index}",
                submitted_at=_NOW,
                first_seen_at=_NOW + timedelta(seconds=0.5),
            )
        venue = FakeVenue(behaviour="timeout")
        _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        report = _reconciler(journal, venue).reconcile(
            session_date=_SESSION, now=_NOW + timedelta(minutes=30)
        )
        assert report.horizon.is_established
        assert report.reconciliations[0].verdict is ReconciliationVerdict.LOCAL_ONLY_ABANDONED
        assert report.reconciliations[0].inferred
        assert journal.state_of(report.reconciliations[0].intent_id or "") is (
            OrderLifecycleState.ABANDONED
        )

    def test_an_ambiguous_order_found_at_the_broker_is_adopted_not_duplicated(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue(behaviour="timeout")
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        venue.orders = [_report(order.broker_tag, status="OPEN")]
        report = _reconciler(journal, venue).reconcile(
            session_date=_SESSION, now=_NOW + timedelta(seconds=30)
        )
        assert report.reconciliations[0].verdict is ReconciliationVerdict.AGREED
        assert journal.state_of(outcome.intent_id) is OrderLifecycleState.WORKING

    def test_a_quantity_the_broker_has_and_we_do_not_is_patched_with_an_inferred_fill(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue()
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        venue.orders = [
            _report(
                order.broker_tag,
                status="COMPLETE",
                filled=100,
                average_paise=Decimal("142310"),
                order_id=outcome.broker_order_id or "",
            )
        ]
        report = _reconciler(journal, venue).reconcile(
            session_date=_SESSION, now=_NOW + timedelta(seconds=30)
        )
        assert report.reconciliations[0].verdict is ReconciliationVerdict.QUANTITY_GAP_PATCHED
        patched = journal.load_order(outcome.intent_id)
        assert patched is not None
        assert patched.filled_quantity == 100
        assert patched.has_inferred_events, "an invented fill must stay marked as invented"

    def test_a_real_trade_arriving_in_the_same_pass_is_used_directly(
        self, journal: OrderIntentJournal
    ) -> None:
        """RENAMED after the `R.23(c)` review. The old name promised more than the body tested.

        This is the EASY ordering — the report and the trade arrive together, so `_apply_trades`
        runs before the gap check and no inference is made. The hard ordering, where an inference
        is already committed and the real trade arrives on a later pass, is the defect the review
        reproduced and now lives in `test_order_path_review_regressions.py::TestC3...`.
        """
        venue = FakeVenue()
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        broker_order_id = outcome.broker_order_id or ""
        venue.orders = [
            _report(order.broker_tag, status="COMPLETE", filled=100, order_id=broker_order_id)
        ]
        venue.trades = [
            VenueTradeReport(
                broker_trade_id="T77",
                broker_order_id=broker_order_id,
                trading_symbol="RELIANCE",
                quantity=100,
                price_paise=Decimal("142320"),
                filled_at=_NOW + timedelta(seconds=2),
            )
        ]
        _reconciler(journal, venue).reconcile(
            session_date=_SESSION, now=_NOW + timedelta(seconds=30)
        )
        filled = journal.load_order(outcome.intent_id)
        assert filled is not None
        assert filled.filled_quantity == 100
        assert not filled.has_inferred_events
        assert filled.state is OrderLifecycleState.FILLED

    def test_an_order_placed_by_a_human_is_adopted_as_external_not_attributed(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue(orders=[_report("manualtag", status="COMPLETE", filled=50)])
        report = _reconciler(journal, venue).reconcile(session_date=_SESSION, now=_NOW)
        assert report.reconciliations[0].verdict is ReconciliationVerdict.BROKER_ONLY
        assert report.reconciliations[0].intent_id is None

    def test_a_status_the_system_cannot_map_is_surfaced_not_guessed(
        self, journal: OrderIntentJournal
    ) -> None:
        venue = FakeVenue()
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        venue.orders = [_report(order.broker_tag, status="SUPER PENDING")]
        report = _reconciler(journal, venue).reconcile(session_date=_SESSION, now=_NOW)
        assert report.reconciliations[0].verdict is ReconciliationVerdict.UNMAPPABLE

    def test_a_broker_that_cannot_be_read_stops_reconciliation_entirely(
        self, journal: OrderIntentJournal
    ) -> None:
        """'I could not ask' and 'there is nothing there' are the same answer only to a system
        that doubles positions."""
        venue = FakeVenue(fetch_fails=True)
        with pytest.raises(ReconciliationRefusedError, match="could not be read"):
            _reconciler(journal, venue).reconcile(session_date=_SESSION, now=_NOW)

    def test_reconciling_twice_does_not_apply_the_same_inference_twice(
        self, journal: OrderIntentJournal
    ) -> None:
        """The inferred trade id is deterministic, so a replay of the SAME report is idempotent.

        Narrowed after the `R.23(c)` review pointed out what this does not establish: the inferred
        id embeds the broker's filled quantity, so this holds only while that quantity is unchanged
        between passes — which in a live session is the one thing it never is. The moving case is
        `TestC3...` in `test_order_path_review_regressions.py`, where the real trades supersede the
        inference instead of adding to it.
        """
        venue = FakeVenue()
        outcome = _placer(journal, venue).place(_intent(), _expression(), now=_NOW)
        order = journal.load_order(outcome.intent_id)
        assert order is not None
        venue.orders = [
            _report(
                order.broker_tag,
                status="OPEN",
                filled=40,
                average_paise=Decimal("142310"),
                order_id=outcome.broker_order_id or "",
            )
        ]
        reconciler = _reconciler(journal, venue)
        reconciler.reconcile(session_date=_SESSION, now=_NOW + timedelta(seconds=30))
        reconciler.reconcile(session_date=_SESSION, now=_NOW + timedelta(seconds=60))
        patched = journal.load_order(outcome.intent_id)
        assert patched is not None
        assert patched.filled_quantity == 40
