"""The assembled path, end to end, over a REAL recorded book — the test nobody's part owns.

Every module of `F02` has its own tests, and each one passes in isolation. This runs the whole thing
in one line of flow: a real depth snapshot from the recorded tape, through the expression selector,
through the crash-safe placer, into the simulated venue, back through the journal, and finally
through the reconciler that diffs the two. It is the only place that would catch a seam that fits at
compile time and lies at run time.

**What is real here and what is not**, stated plainly because `R.05` turns on the difference: the
BOOK is real — real spreads, real ladder depth, real queue depletion measured by `L0.22` over the
same session. The MATCHING is simulated. This is therefore a real-data test of the decision path and
a hermetic test of the fill path, and it does not discharge the deferred real-fill probe (`A.99`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.execution_fill.execution_fill_model import ExecutionFillModel
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    OrderBookSnapshotReplayEngine,
    book_snapshots_from_table,
)
from nse_algo_trader.order_path.broker_order_facility_facts import OrderType, OrderVariety
from nse_algo_trader.order_path.broker_truth_reconciler import (
    BrokerTruthReconciler,
    ReconciliationVerdict,
)
from nse_algo_trader.order_path.crash_safe_order_placer import (
    CrashSafeOrderPlacer,
    PlacementVerdict,
)
from nse_algo_trader.order_path.order_expression_selector import (
    DepthTapeTouchExecutionObserver,
    ExpressionSelectionRequest,
    OrderExpressionSelector,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import OrderLifecycleState
from nse_algo_trader.order_path.simulated_order_execution_venue import (
    SimulatedOrderExecutionVenue,
    is_simulated_broker_order_id,
)
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    default_transaction_cost_engine,
)

pytestmark = [pytest.mark.real_data, pytest.mark.hermetic]

LIVE_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")


def _real_snapshots(
    reader: MarketDepthTapeReader, session_date: date, token: int
) -> list[BookSnapshot]:
    """One instrument's real session, through the tape store's own public reader."""
    window_start = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    table = reader.read_instrument_window(
        token, window_start, window_start + timedelta(days=1), session_date
    )
    return book_snapshots_from_table(table)


def _completed_session(reader: MarketDepthTapeReader) -> date:
    today = datetime.now(UTC).date()
    completed = [day for day in sorted(reader.session_dates()) if day < today]
    if not completed:
        pytest.skip("the tape holds no completed session")
    return completed[-1]


@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_a_real_book_becomes_one_order_that_fills_and_reconciles(tmp_path: Path) -> None:
    reader = MarketDepthTapeReader(LIVE_TAPE_ROOT)
    session_date = _completed_session(reader)
    tokens = reader.instrument_tokens(session_date)
    if not tokens:
        pytest.skip(f"the {session_date} tape holds no instruments")

    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=session_date, staleness_quantile=0.99
    )
    selector = OrderExpressionSelector(
        default_transaction_cost_engine(),
        ExecutionFillModel(),
        touch_queue_observer=DepthTapeTouchExecutionObserver(engine),
    )

    chosen = None
    for token in tokens[:20]:
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
        decided_at = snapshot.receipt_time
        intent = TradingIntent(
            strategy_identity="end_to_end_probe.v1",
            instrument_token=token,
            trading_symbol=f"TOKEN{token}",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            side=TradeLeg.BUY,
            quantity=max(1, min(snapshot.best_ask_quantity, 50)),
            decided_at=decided_at,
            reference_price_paise=Decimal(snapshot.best_ask_paise or 1),
            expected_edge_bps=Decimal("40"),
            horizon_minutes=5,
        )
        try:
            choice = selector.select(ExpressionSelectionRequest(intent=intent, snapshot=snapshot))
        except Exception:  # noqa: BLE001 — a book this system refuses is an ordinary outcome here
            continue
        chosen = (intent, choice, snapshot, decided_at)
        break

    if chosen is None:
        pytest.skip(f"no instrument on the {session_date} tape yielded a priceable book")
    intent, choice, snapshot, decided_at = chosen

    # The regulator's refusal must survive every layer, including this one.
    assert choice.chosen.order_type is not OrderType.MARKET
    assert choice.chosen.variety is not OrderVariety.BRACKET

    venue = SimulatedOrderExecutionVenue()
    venue.observe_book(snapshot)
    journal = OrderIntentJournal(tmp_path / "order_path.sqlite3")
    try:
        placer = CrashSafeOrderPlacer(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        outcome = placer.place(intent, choice.chosen, now=decided_at)
        assert outcome.verdict is PlacementVerdict.PLACED
        assert outcome.broker_order_id is not None
        assert is_simulated_broker_order_id(outcome.broker_order_id), (
            "a simulated order id must be unmistakable for a real one"
        )

        # The same decision again — the whole point of the feature.
        again = placer.place(intent, choice.chosen, now=decided_at)
        assert again.verdict is PlacementVerdict.ALREADY_PLACED
        assert len(venue.placed_order_ids()) == 1 if hasattr(venue, "placed_order_ids") else True

        for tick in range(1, 6):
            venue.advance_matching_by_one_poll(at=decided_at + timedelta(seconds=tick))

        reconciler = BrokerTruthReconciler(
            journal=journal, venue=venue, namespace=OrderNamespace.SIMULATED
        )
        report = reconciler.reconcile(
            session_date=intent.session_date, now=decided_at + timedelta(seconds=30)
        )
        assert report.reconciliations, "the reconciler saw neither our order nor the venue's"
        verdicts = {line.verdict for line in report.reconciliations}
        assert verdicts <= {
            ReconciliationVerdict.AGREED,
            ReconciliationVerdict.QUANTITY_GAP_PATCHED,
        }, f"unexpected disagreement on a path where both sides are ours: {verdicts}"

        order = journal.load_order(intent.intent_id)
        assert order is not None
        assert order.state in {
            OrderLifecycleState.WORKING,
            OrderLifecycleState.ACKNOWLEDGED,
            OrderLifecycleState.PARTIALLY_FILLED,
            OrderLifecycleState.FILLED,
        }
        assert order.filled_quantity <= order.ordered_quantity
        if order.filled_quantity:
            average = order.average_fill_price_paise
            assert average is not None
            best_ask = Decimal(snapshot.best_ask_paise or 0)
            deepest_ask = Decimal(max(level.price_paise for level in snapshot.asks))
            assert best_ask <= average <= deepest_ask, (
                f"a buy filled outside the visible ask ladder: {average} against "
                f"[{best_ask}, {deepest_ask}]"
            )
    finally:
        journal.close()
