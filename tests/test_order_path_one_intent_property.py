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
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

_NOW = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)


class _RecordingVenue:
    """A venue that remembers every order it was given and can fail in each way a real one does.

    A timeout is the dangerous case: the caller cannot tell whether the order landed, so the venue
    RECORDS it as landed — which is exactly what a real broker does when the response is lost on
    the way back. If the system under test resends, the count goes to two and the invariant fails.

    **Widened after the `R.23(c)` review**, which showed the earlier fake could not reach the states
    where the real defects lived: it raised only `VenueOutcomeUnknownError` (so the retry path — the
    only path that resends — was unreachable), it never reported a fill (so no terminal-after-fill
    fold, no quantity gap), and it always listed every order it had accepted (so absence and
    abandonment were unreachable too). The invariant was true of the fake rather than of the system.
    It can now also drop the connection, part-fill, complete, and forget an order.
    """

    def __init__(self) -> None:
        self.accepted: list[OrderRecord] = []
        self.timeout_next = False
        self.unavailable_next = False
        self.filled: dict[str, int] = {}
        self.hidden: set[str] = set()
        self.trades: list[VenueTradeReport] = []

    @property
    def venue_name(self) -> str:
        return "recording"

    def place(self, order: OrderRecord) -> str:
        if self.unavailable_next:
            # A failure that provably PRECEDES transmission: nothing is recorded, because nothing
            # reached the venue. This is the path the placer is allowed to retry, and the invariant
            # is what proves the retry cannot become a second order.
            self.unavailable_next = False
            raise VenueUnavailableError("the connection was refused before anything was sent")
        self.accepted.append(order)
        if self.timeout_next:
            self.timeout_next = False
            raise VenueOutcomeUnknownError("the response was lost on the way back")
        return f"25081300{len(self.accepted):04d}"

    def _broker_order_id(self, index: int) -> str:
        return f"25081300{index + 1:04d}"

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None: ...

    def cancel(self, broker_order_id: str, *, variety: str) -> None: ...

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        del session_date
        reports = []
        for index, order in enumerate(self.accepted):
            broker_order_id = self._broker_order_id(index)
            if broker_order_id in self.hidden:
                continue
            filled = min(self.filled.get(broker_order_id, 0), order.ordered_quantity)
            reports.append(
                VenueOrderReport(
                    broker_order_id=broker_order_id,
                    tag=order.broker_tag,
                    status="COMPLETE" if filled >= order.ordered_quantity else "OPEN",
                    trading_symbol=order.trading_symbol,
                    quantity=order.ordered_quantity,
                    filled_quantity=filled,
                    pending_quantity=order.ordered_quantity - filled,
                    average_price_paise=Decimal("142350") if filled else None,
                    status_message="",
                    status_message_raw="",
                    exchange_timestamp=_NOW,
                )
            )
        return tuple(reports)

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        del broker_order_id
        return ()

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        del session_date
        return tuple(self.trades)

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

    @rule(quantity=integers(min_value=1, max_value=3), symbol=sampled_from(["RELIANCE", "INFY"]))
    def submit_through_a_refused_connection(self, quantity: int, symbol: str) -> None:
        """The one failure the placer may retry. If a retry can duplicate, this finds it."""
        self.venue.unavailable_next = True
        self._placer().place(_intent(quantity, symbol), _EXPRESSION, now=self.clock)

    @rule(units=integers(min_value=1, max_value=3))
    def the_broker_fills_something(self, units: int) -> None:
        """Fills move `orders().filled_quantity` before `trades()` lists them, as Kite's do."""
        if not self.venue.accepted:
            return
        index = len(self.venue.accepted) - 1
        broker_order_id = self.venue._broker_order_id(index)
        self.venue.filled[broker_order_id] = self.venue.filled.get(broker_order_id, 0) + units

    @rule()
    def the_trade_book_catches_up(self) -> None:
        """The real executions appear later, and must supersede anything inferred meanwhile."""
        for index, order in enumerate(self.venue.accepted):
            broker_order_id = self.venue._broker_order_id(index)
            filled = min(self.venue.filled.get(broker_order_id, 0), order.ordered_quantity)
            already = sum(
                trade.quantity
                for trade in self.venue.trades
                if trade.broker_order_id == broker_order_id
            )
            if filled > already:
                self.venue.trades.append(
                    VenueTradeReport(
                        broker_trade_id=f"T-{broker_order_id}-{filled}",
                        broker_order_id=broker_order_id,
                        trading_symbol=order.trading_symbol,
                        quantity=filled - already,
                        price_paise=Decimal("142350"),
                        filled_at=self.clock,
                    )
                )

    @rule()
    def the_broker_stops_listing_an_order(self) -> None:
        """Absence, which is what the visibility horizon exists to interpret."""
        if self.venue.accepted:
            self.venue.hidden.add(self.venue._broker_order_id(0))

    @rule()
    def crash_and_restart(self) -> None:
        """The process dies and comes back with no memory beyond what is on disk."""
        self.journal.close()
        self.journal = OrderIntentJournal(self._path)

    @rule()
    def reconcile(self) -> None:
        self.clock += timedelta(seconds=30)
        BrokerTruthReconciler(
            journal=self.journal, venue=self.venue, namespace=OrderNamespace.SIMULATED
        ).reconcile(
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

    @invariant()
    def every_order_can_always_be_read_back(self) -> None:
        """The fold must never raise. When it did, one cancelled part-fill took the whole session
        — and every page that reads it — down permanently."""
        for order in self.journal.orders_for_session(_SESSION):
            assert self.journal.load_order(order.intent_id) is not None

    @invariant()
    def no_order_ever_holds_more_than_it_asked_for(self) -> None:
        for order in self.journal.orders_for_session(_SESSION):
            assert order.filled_quantity <= order.ordered_quantity, (
                f"{order.intent_id} holds {order.filled_quantity} against an ordered "
                f"{order.ordered_quantity}"
            )


TestOneIntentOneOrder = OneIntentOneOrder.TestCase
TestOneIntentOneOrder.settings = settings(
    max_examples=60,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
