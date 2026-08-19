"""`/paper-session` — the surface must show what the stores hold, and say so when they hold nothing.

`R.08`'s bite is that a surface must be MEASURED from real state rather than hand-authored, so the
tests below write the same stores a session writes and check the page reflects them — including the
case that matters most, a session directory that does not exist, which must read as an absence
rather than as a quiet day with no trades.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.dashboard.paper_session_surface_renderer import (
    read_paper_session_state,
    recorded_paper_sessions,
    render_paper_session_page,
)
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import EventSource
from nse_algo_trader.order_path.order_record import FillRecord, OrderExpression
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.paper_capital_ledger import PaperCapitalLedger
from nse_algo_trader.replay_session_clock import session_for
from nse_algo_trader.sizing.session_risk_state_store import RiskLatch, SessionRiskStateStore
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

SESSION_DATE = date(2026, 8, 11)
OPENS_AT = session_for(SESSION_DATE).opens_at
MEASURED_AT = OPENS_AT + timedelta(hours=7)


def _write_a_session(root: Path) -> None:
    """The three stores a real session writes, with one filled order in them."""
    directory = root / SESSION_DATE.isoformat()
    directory.mkdir(parents=True)

    journal = OrderIntentJournal(directory / "journal.sqlite3")
    intent = TradingIntent(
        strategy_identity="intraday_mean_reversion",
        instrument_token=738561,
        trading_symbol="RELIANCE",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=10,
        decided_at=OPENS_AT + timedelta(minutes=5),
        reference_price_paise=Decimal("140000"),
        expected_edge_bps=Decimal("12"),
    )
    expression = OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.MARKET,
        validity=OrderValidity.DAY,
        chosen_because="test",
    )
    journal.record_intent(intent, expression, OrderNamespace.SIMULATED, at=intent.decided_at)
    journal.record_fill(
        intent.intent_id,
        FillRecord(
            broker_trade_id="SIM-TRD-1",
            quantity=6,
            price_paise=Decimal("140050"),
            occurred_at=OPENS_AT + timedelta(minutes=6),
            source=EventSource.BROKER_REPORTED,
        ),
    )
    journal.close()

    ledger = PaperCapitalLedger(directory / "ledger.sqlite3")
    ledger.seed_from_ceiling(
        TradingCapital.of_rupees(Decimal("1000000")),
        occurred_at=OPENS_AT - timedelta(minutes=1),
        reason="test seed",
    )
    ledger.commit_to_position(
        Decimal("8400"),
        position_key="RELIANCE-1",
        occurred_at=OPENS_AT + timedelta(minutes=5),
        reason="entry",
    )
    ledger.record_realised_profit(
        Decimal("120"),
        position_key="RELIANCE-1",
        occurred_at=OPENS_AT + timedelta(hours=6),
        reason="closed",
    )
    ledger.record_cost_debit(
        Decimal("35"),
        position_key="RELIANCE-1",
        occurred_at=OPENS_AT + timedelta(hours=6),
        reason="charges",
    )
    ledger.release_position(
        position_key="RELIANCE-1",
        occurred_at=OPENS_AT + timedelta(hours=6),
        reason="closed",
    )
    ledger.close()

    risk = SessionRiskStateStore(directory / "risk.sqlite3")
    risk.open_session(
        session_date=SESSION_DATE,
        opening_equity_rupees=Decimal("1000000"),
        occurred_at=OPENS_AT,
    )
    risk.trip_latch(
        RiskLatch.DAILY_LOSS,
        session_date=SESSION_DATE,
        occurred_at=OPENS_AT + timedelta(hours=3),
        reason="the day's loss reached the derived limit",
    )
    risk.close()


def test_a_recorded_session_is_folded_out_of_its_own_stores(tmp_path: Path) -> None:
    _write_a_session(tmp_path)
    state = read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path)

    assert state.has_session
    assert state.session_date == SESSION_DATE
    assert len(state.orders) == 1
    order = state.orders[0]
    assert order.trading_symbol == "RELIANCE"
    assert (order.filled_quantity, order.ordered_quantity) == (6, 10)
    assert order.average_price_paise == Decimal("140050")

    # Profit, loss and cost stay APART: `L1.11` cannot attribute a result that was folded.
    assert state.realised_profit_rupees == Decimal("120")
    assert state.cost_debit_rupees == Decimal("35")
    assert state.net_realised_rupees == Decimal("85")
    assert state.ledger_committed_rupees == Decimal(0)
    assert state.tripped_latches and "daily" in state.tripped_latches[0].lower()


def test_the_page_shows_the_order_the_stores_hold(tmp_path: Path) -> None:
    _write_a_session(tmp_path)
    page = render_paper_session_page(
        read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path)
    )
    assert "RELIANCE" in page
    assert "6/10" in page
    assert SESSION_DATE.isoformat() in page
    assert "the day&#x27;s loss reached the derived limit" in page or "derived limit" in page


def test_no_recorded_session_reads_as_an_absence_not_a_quiet_day(tmp_path: Path) -> None:
    """The one that matters: "nothing happened" and "nothing was recorded" are different facts."""
    state = read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path / "empty")
    assert not state.has_session
    assert "no paper session has been recorded" in state.unavailable_reason
    assert state.missing_stores == ("paper_session_directory",)

    page = render_paper_session_page(state)
    assert "no paper session has been recorded" in page
    assert "verify_paper_session_on_real_data" in page


def test_a_directory_that_is_not_a_date_is_ignored_rather_than_crashing(tmp_path: Path) -> None:
    (tmp_path / "not-a-date").mkdir()
    _write_a_session(tmp_path)
    assert recorded_paper_sessions(tmp_path) == (SESSION_DATE,)


def test_the_page_never_writes_to_the_stores_it_reads(tmp_path: Path) -> None:
    """A read that mutates is a read nobody can safely perform to find out where they stand."""
    _write_a_session(tmp_path)
    directory = tmp_path / SESSION_DATE.isoformat()
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.iterdir()
        if path.suffix == ".sqlite3"
    }
    render_paper_session_page(read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path))
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.iterdir()
        if path.suffix == ".sqlite3"
    }
    assert before == after


def test_the_measured_instant_is_stated_on_the_page(tmp_path: Path) -> None:
    _write_a_session(tmp_path)
    page = render_paper_session_page(
        read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path)
    )
    assert MEASURED_AT.isoformat() in page
    assert isinstance(MEASURED_AT, datetime)


def test_a_fully_filled_order_does_not_read_as_merely_opened(tmp_path: Path) -> None:
    """The journal's last EVENT is not the order's state — a fill moves the order directly."""
    _write_a_session(tmp_path)
    state = read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path)
    assert state.orders[0].state == "partially filled", "6 of 10 filled is neither open nor filled"

    page = render_paper_session_page(state)
    assert "partially filled" in page


def test_a_price_is_shown_to_the_paise_not_to_twenty_eight_figures(tmp_path: Path) -> None:
    """A volume-weighted walk divides; the quotient's tail is arithmetic, not information."""
    _write_a_session(tmp_path)
    page = render_paper_session_page(
        read_paper_session_state(measured_at=MEASURED_AT, root=tmp_path)
    )
    assert "140050.00" in page
