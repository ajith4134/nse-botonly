"""The claim, under arbitrary interleaving: one intent never becomes two orders.

Only NautilusTrader, of the five systems read in `docs/research/224`, property-fuzzes its order and
reconciliation logic. `hypothesis` is already a dependency here, and `RuleBasedStateMachine` is the
equivalent instrument: it explores sequences of submit / crash / restart / reconcile that no
hand-written test would think to write, and shrinks any violation to its smallest repro.

The invariant is checked after EVERY step, not at the end, so a violation that a later step would
have repaired is still caught.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from hypothesis.strategies import integers, sampled_from

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.broker_truth_reconciler import BrokerTruthReconciler
from nse_algo_trader.order_path.crash_safe_order_placer import CrashSafeOrderPlacer
from nse_algo_trader.order_path.order_execution_venue import (
    VenueOrderReport,
    VenueOutcomeUnknownError,
    VenuePositionReport,
    VenueTradeReport,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

_NOW = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)


class _RecordingVenue:
    """A venue that remembers every order it was ever given, and can time out on command.

    A timeout is the dangerous case: the caller cannot tell whether the order landed, so the venue
    RECORDS it as landed — which is exactly what a real broker does when the response is lost on
    the way back. If the system under test resends, the count goes to two and the invariant fails.
    """

    def __init__(self) -> None:
        self.accepted: list[OrderRecord] = []
        self.timeout_next = False

    @property
    def venue_name(self) -> str:
        return "recording"

    def place(self, order: OrderRecord) -> str:
        self.accepted.append(order)
        if self.timeout_next:
            self.timeout_next = False
            raise VenueOutcomeUnknownError("the response was lost on the way back")
        return f"25081300{len(self.accepted):04d}"

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None: ...

    def cancel(self, broker_order_id: str, *, variety: str) -> None: ...

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        del session_date
        return tuple(
            VenueOrderReport(
                broker_order_id=f"25081300{index + 1:04d}",
                tag=order.broker_tag,
                status="OPEN",
                trading_symbol=order.trading_symbol,
                quantity=order.ordered_quantity,
                filled_quantity=0,
                pending_quantity=order.ordered_quantity,
                average_price_paise=None,
                status_message="",
                status_message_raw="",
                exchange_timestamp=_NOW,
            )
            for index, order in enumerate(self.accepted)
        )

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        del broker_order_id
        return ()

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        del session_date
        return ()

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]:
        return ()


def _intent(quantity: int, symbol: str) -> TradingIntent:
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


_EXPRESSION = OrderExpression(
    variety=OrderVariety.REGULAR,
    product=OrderProduct.INTRADAY,
    order_type=OrderType.LIMIT,
    validity=OrderValidity.DAY,
    limit_price_paise=Decimal("142350"),
)


class OneIntentOneOrder(RuleBasedStateMachine):
    """Submit, crash, restart, reconcile — in any order hypothesis can think of."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = TemporaryDirectory()
        self._path = Path(self._directory.name) / "order_path.sqlite3"
        self.venue = _RecordingVenue()
        self.journal = OrderIntentJournal(self._path)
        self.clock = _NOW

    @initialize()
    def start(self) -> None:
        self.venue = _RecordingVenue()

    def teardown(self) -> None:
        self.journal.close()
        self._directory.cleanup()

    def _placer(self) -> CrashSafeOrderPlacer:
        return CrashSafeOrderPlacer(
            journal=self.journal, venue=self.venue, namespace=OrderNamespace.SIMULATED
        )

    @rule(quantity=integers(min_value=1, max_value=3), symbol=sampled_from(["RELIANCE", "INFY"]))
    def submit(self, quantity: int, symbol: str) -> None:
        """The same decision, offered again and again."""
        self._placer().place(_intent(quantity, symbol), _EXPRESSION, now=self.clock)

    @rule(quantity=integers(min_value=1, max_value=3), symbol=sampled_from(["RELIANCE", "INFY"]))
    def submit_through_a_lost_response(self, quantity: int, symbol: str) -> None:
        self.venue.timeout_next = True
        self._placer().place(_intent(quantity, symbol), _EXPRESSION, now=self.clock)

    @rule()
    def crash_and_restart(self) -> None:
        """The process dies and comes back with no memory beyond what is on disk."""
        self.journal.close()
        self.journal = OrderIntentJournal(self._path)

    @rule()
    def reconcile(self) -> None:
        self.clock += timedelta(seconds=30)
        BrokerTruthReconciler(journal=self.journal, venue=self.venue).reconcile(
            session_date=_SESSION, now=self.clock
        )

    @invariant()
    def no_intent_ever_becomes_two_orders(self) -> None:
        tags = [order.broker_tag for order in self.venue.accepted]
        assert len(tags) == len(set(tags)), (
            f"the same intent reached the venue more than once: {sorted(tags)}"
        )

    @invariant()
    def the_journal_never_holds_two_orders_for_one_intent(self) -> None:
        orders = self.journal.orders_for_session(_SESSION)
        intent_ids = [order.intent_id for order in orders]
        assert len(intent_ids) == len(set(intent_ids))


TestOneIntentOneOrder = OneIntentOneOrder.TestCase
TestOneIntentOneOrder.settings = settings(
    max_examples=60,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
