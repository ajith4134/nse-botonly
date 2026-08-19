"""The journal's obligations: write before the call, never lose an attempt, and fold back exactly.

The claim under test is `L3.02`'s reason for existing — that a process killed at any instant can be
told apart from one that never started. freqtrade and LEAN cannot make that distinction
(`docs/research/224` §3), and the cost of not making it is a retry that duplicates a live order.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.order_intent_journal import (
    OrderIntentJournal,
    SubmissionOutcome,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    LifecycleEvent,
    OrderLifecycleState,
)
from nse_algo_trader.order_path.order_record import FillRecord, OrderExpression
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = pytest.mark.unit

_AT = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)


def _intent(quantity: int = 100, symbol: str = "RELIANCE") -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_AT,
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
        chosen_because="marketable limit: MARKET is barred for algo flow (research/223 §5)",
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[OrderIntentJournal]:
    with OrderIntentJournal(tmp_path / "order_path.sqlite3") as opened:
        yield opened


class TestTheIntentIsDurableBeforeAnythingElse:
    def test_an_intent_is_recorded_once_and_the_duplicate_is_reported_not_raised(
        self, journal: OrderIntentJournal
    ) -> None:
        """The duplicate is an ordinary event — a retry, a restart, two engines agreeing — and the
        journal is the place it is supposed to collide."""
        assert journal.record_intent(_intent(), _expression(), OrderNamespace.SIMULATED, at=_AT)
        assert not journal.record_intent(_intent(), _expression(), OrderNamespace.SIMULATED, at=_AT)

    def test_a_different_decision_is_not_a_duplicate(self, journal: OrderIntentJournal) -> None:
        journal.record_intent(_intent(), _expression(), OrderNamespace.SIMULATED, at=_AT)
        assert journal.record_intent(
            _intent(quantity=50), _expression(), OrderNamespace.SIMULATED, at=_AT
        )

    def test_the_wire_tag_leads_back_to_the_decision(self, journal: OrderIntentJournal) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert journal.intent_id_for_tag(order.broker_tag) == intent.intent_id

    def test_a_tag_that_is_not_ours_leads_nowhere(self, journal: OrderIntentJournal) -> None:
        assert journal.intent_id_for_tag("someoneelsestag12345") is None


class TestACrashIsRecoverable:
    def test_an_attempt_with_no_outcome_is_what_a_restart_finds(
        self, journal: OrderIntentJournal
    ) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_submission_started(intent.intent_id, at=_AT)
        # the process dies here — nothing else is written
        in_flight = journal.in_flight_submissions(session_date=_SESSION)
        assert [submission.intent_id for submission in in_flight] == [intent.intent_id]
        assert in_flight[0].attempt == 1

    def test_a_settled_attempt_is_not_in_flight(self, journal: OrderIntentJournal) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        submission_id = journal.record_submission_started(intent.intent_id, at=_AT)
        journal.record_submission_outcome(
            submission_id, SubmissionOutcome.ACKNOWLEDGED, at=_AT, broker_order_id="250813000001"
        )
        assert journal.in_flight_submissions(session_date=_SESSION) == ()

    def test_an_unknown_outcome_settles_the_attempt_but_not_the_question(
        self, journal: OrderIntentJournal
    ) -> None:
        """`UNKNOWN` is written like any other outcome — the attempt is over, the order's fate is
        not, and that is the distinction `AMBIGUOUS` exists to carry."""
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        submission_id = journal.record_submission_started(intent.intent_id, at=_AT)
        journal.record_submission_outcome(
            submission_id, SubmissionOutcome.UNKNOWN, at=_AT, detail="read timeout after 7s"
        )
        journal.record_event(intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT)
        journal.record_event(
            intent.intent_id, LifecycleEvent.OUTCOME_UNKNOWN, EventSource.LOCAL, at=_AT
        )
        assert journal.state_of(intent.intent_id) is OrderLifecycleState.AMBIGUOUS

    def test_a_second_attempt_is_numbered_not_overwritten(
        self, journal: OrderIntentJournal
    ) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        first = journal.record_submission_started(intent.intent_id, at=_AT)
        journal.record_submission_outcome(first, SubmissionOutcome.UNKNOWN, at=_AT)
        journal.record_submission_started(intent.intent_id, at=_AT)
        assert journal.in_flight_submissions(session_date=_SESSION)[0].attempt == 2


class TestTheFoldReproducesTheOrder:
    def test_events_and_fills_rebuild_the_same_order(self, journal: OrderIntentJournal) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_event(intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT)
        journal.record_event(
            intent.intent_id,
            LifecycleEvent.ACKNOWLEDGED,
            EventSource.BROKER_REPORTED,
            at=_AT,
            broker_order_id="250813000001",
        )
        journal.record_fill(
            intent.intent_id,
            FillRecord(
                broker_trade_id="T1",
                quantity=40,
                price_paise=Decimal("142300"),
                occurred_at=_AT,
                source=EventSource.BROKER_REPORTED,
            ),
        )
        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert order.state is OrderLifecycleState.PARTIALLY_FILLED
        assert order.filled_quantity == 40
        assert order.leaves_quantity == 60
        assert order.broker_order_id == "250813000001"
        assert order.expression.chosen_because.startswith("marketable limit")

    def test_the_fold_is_stable_across_reopening_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "order_path.sqlite3"
        intent = _intent()
        with OrderIntentJournal(path) as first:
            first.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
            first.record_event(
                intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT
            )
            before = first.load_order(intent.intent_id)
        with OrderIntentJournal(path) as second:
            after = second.load_order(intent.intent_id)
        assert before is not None
        assert after is not None
        assert (before.state, before.filled_quantity) == (after.state, after.filled_quantity)

    def test_the_same_trade_recorded_twice_is_stored_once(
        self, journal: OrderIntentJournal
    ) -> None:
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        fill = FillRecord(
            broker_trade_id="T1",
            quantity=40,
            price_paise=Decimal("142300"),
            occurred_at=_AT,
            source=EventSource.BROKER_REPORTED,
        )
        assert journal.record_fill(intent.intent_id, fill)
        assert not journal.record_fill(intent.intent_id, fill)
        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert order.filled_quantity == 40

    def test_a_fill_with_no_submission_event_infers_the_submission_and_says_so(
        self, journal: OrderIntentJournal
    ) -> None:
        """The crash window this journal exists to survive: the intent committed, the process died
        before the submission event was appended, and the broker filled the order anyway."""
        intent = _intent()
        journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
        journal.record_fill(
            intent.intent_id,
            FillRecord(
                broker_trade_id="T9",
                quantity=100,
                price_paise=Decimal("142300"),
                occurred_at=_AT,
                source=EventSource.BROKER_REPORTED,
            ),
        )
        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert order.state is OrderLifecycleState.FILLED
        assert order.has_inferred_events
        assert "inferred from the existence of fill T9" in order.transitions[0].note

    def test_an_unknown_order_folds_to_nothing_rather_than_an_empty_order(
        self, journal: OrderIntentJournal
    ) -> None:
        assert journal.load_order("not-an-intent") is None


class TestTheSessionView:
    def test_open_orders_exclude_the_finished_ones(self, journal: OrderIntentJournal) -> None:
        open_intent, done_intent = _intent(symbol="RELIANCE"), _intent(symbol="INFY")
        for intent in (open_intent, done_intent):
            journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_AT)
            journal.record_event(
                intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_AT
            )
        journal.record_event(
            done_intent.intent_id,
            LifecycleEvent.REJECTED,
            EventSource.BROKER_REPORTED,
            at=_AT,
            note="RMS:Rule: Check freeze quantity for NSE CASH",
        )
        open_ids = {order.intent_id for order in journal.open_orders(_SESSION)}
        assert open_ids == {open_intent.intent_id}

    def test_another_session_is_not_this_session(self, journal: OrderIntentJournal) -> None:
        journal.record_intent(_intent(), _expression(), OrderNamespace.SIMULATED, at=_AT)
        assert journal.orders_for_session(date(2026, 8, 12)) == ()
