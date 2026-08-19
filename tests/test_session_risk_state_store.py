"""Tests for the session risk state and its latches (`L7.03`).

The property that matters most is the one a test suite almost never checks: **a restart must not
clear a halt.** Process bounces are exactly what follows a bad morning, and a latch that forgets
itself when the process comes back is a delay rather than a halt. It has its own test, and so does
the operator-only clear (`R.22`).

The concurrency tests exist because `O.85` says the criticals cluster in whichever axis the file
holds constant, and `L1.18`'s review proved it by finding a double-spend behind 39 single-threaded
tests.

`docs/research/227` §5 is the specification.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.sizing.session_risk_state_store import (
    MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT,
    RiskLatch,
    SessionRiskStateError,
    SessionRiskStateStore,
    false_halt_quantile,
)

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 8, 13)
TEN_LAKH = Decimal("1000000")


def _at(second: int) -> datetime:
    return datetime(2026, 8, 13, 9, 15, tzinfo=IST) + timedelta(seconds=second)


@pytest.fixture(name="state_path")
def _state_path(tmp_path: Path) -> Path:
    path = tmp_path / "session_risk_state.sqlite3"
    with SessionRiskStateStore(path) as store:
        store.open_session(session_date=SESSION, opening_equity_rupees=TEN_LAKH, occurred_at=_at(0))
    return path


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_fresh_session_starts_flat_at_its_opening_equity(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store:
        state = store.state_for(session_date=SESSION, now=_at(1))
    assert state.current_equity_rupees == TEN_LAKH
    assert state.realised_pnl_rupees == Decimal(0)
    assert state.drawdown_rupees == Decimal(0)
    assert state.is_halted is False


@pytest.mark.unit
def test_realised_losses_move_equity_and_open_a_drawdown(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store:
        store.record_realised_pnl(
            Decimal("20000"),
            session_date=SESSION,
            trading_symbol="RELIANCE",
            occurred_at=_at(1),
            reason="winner",
        )
        state = store.record_realised_pnl(
            Decimal("-50000"),
            session_date=SESSION,
            trading_symbol="TCS",
            occurred_at=_at(2),
            reason="loser",
        )
    assert state.current_equity_rupees == TEN_LAKH - Decimal("30000")
    assert state.peak_equity_rupees == TEN_LAKH + Decimal("20000")
    assert state.drawdown_rupees == Decimal("50000")


@pytest.mark.unit
def test_exposure_accumulates_per_symbol_and_clears_when_it_returns(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store:
        store.record_exposure_change(
            Decimal("300000"),
            session_date=SESSION,
            trading_symbol="RELIANCE",
            occurred_at=_at(1),
            reason="entry",
        )
        store.record_exposure_change(
            Decimal("200000"),
            session_date=SESSION,
            trading_symbol="TCS",
            occurred_at=_at(2),
            reason="entry",
        )
        store.record_exposure_change(
            Decimal("-300000"),
            session_date=SESSION,
            trading_symbol="RELIANCE",
            occurred_at=_at(3),
            reason="exit",
        )
        state = store.state_for(session_date=SESSION, now=_at(4))
    assert state.open_exposure_rupees == Decimal("200000")
    assert state.exposure_by_symbol == (("TCS", Decimal("200000")),)


@pytest.mark.unit
def test_the_rate_window_counts_only_recent_orders(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store:
        for second in (0, 1, 2, 10):
            store.record_order_sent(
                session_date=SESSION, trading_symbol="RELIANCE", occurred_at=_at(second)
            )
        recent = store.state_for(
            session_date=SESSION, now=_at(10), rate_window=timedelta(seconds=1)
        )
        wider = store.state_for(
            session_date=SESSION, now=_at(10), rate_window=timedelta(seconds=30)
        )
    # The order sent AT `now` is inside the window; the three older ones are not.
    assert recent.orders_in_rate_window == 1
    assert wider.orders_in_rate_window == 4


@pytest.mark.unit
def test_the_false_halt_frequency_implies_the_quantile_rather_than_the_reverse() -> None:
    """`R.03` — one states how often a spurious halt is tolerable; `z` follows."""
    z = false_halt_quantile(sessions_per_year=250, false_halts_per_year=Decimal(1))
    assert Decimal("2.64") < z < Decimal("2.67")  # phi-inverse(1 - 1/250)
    stricter = false_halt_quantile(sessions_per_year=250, false_halts_per_year=Decimal("0.25"))
    assert stricter > z


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_restart_does_not_clear_a_halt(state_path: Path) -> None:
    """The single most important property here. A latch that a process bounce clears is a delay.

    Process bounces are exactly what follows a bad morning: a supervisor restart, an operator
    restart, a crash on the same bad tick that caused the loss.
    """
    with SessionRiskStateStore(state_path) as store:
        store.trip_latch(
            RiskLatch.DAILY_LOSS,
            session_date=SESSION,
            occurred_at=_at(5),
            reason="daily loss limit breached",
        )
    with SessionRiskStateStore(state_path) as reopened:
        state = reopened.state_for(session_date=SESSION, now=_at(6))
    assert state.is_halted is True
    assert RiskLatch.DAILY_LOSS in state.tripped_latches


@pytest.mark.adversarial
def test_reopening_a_session_does_not_reset_the_loss_it_already_took(state_path: Path) -> None:
    """`open_session` is idempotent, and that is a safety property rather than a convenience.

    A second call that overwrote the opening mark would erase the day's loss by arithmetic and
    release a latch that was correctly tripped.
    """
    with SessionRiskStateStore(state_path) as store:
        store.record_realised_pnl(
            Decimal("-70000"),
            session_date=SESSION,
            trading_symbol="TCS",
            occurred_at=_at(1),
            reason="loss",
        )
    with SessionRiskStateStore(state_path) as restarted:
        restarted.open_session(
            session_date=SESSION, opening_equity_rupees=TEN_LAKH, occurred_at=_at(2)
        )
        state = restarted.state_for(session_date=SESSION, now=_at(3))
    assert state.realised_pnl_rupees == Decimal("-70000")
    assert state.current_equity_rupees == TEN_LAKH - Decimal("70000")


@pytest.mark.adversarial
def test_only_a_named_operator_can_clear_a_latch(state_path: Path) -> None:
    """`R.22` — the system may halt itself and may never un-halt itself."""
    with SessionRiskStateStore(state_path) as store:
        store.trip_latch(
            RiskLatch.DRAWDOWN, session_date=SESSION, occurred_at=_at(5), reason="drawdown"
        )
        with pytest.raises(SessionRiskStateError):
            store.clear_latch(
                RiskLatch.DRAWDOWN, session_date=SESSION, occurred_at=_at(6), cleared_by="  "
            )
        store.clear_latch(
            RiskLatch.DRAWDOWN, session_date=SESSION, occurred_at=_at(7), cleared_by="ajith"
        )
        state = store.state_for(session_date=SESSION, now=_at(8))
    assert state.is_halted is False


@pytest.mark.adversarial
def test_a_latch_tripped_twice_keeps_its_original_reason(state_path: Path) -> None:
    """The first explanation is the true one; the second is the consequence of the first."""
    with SessionRiskStateStore(state_path) as store:
        store.trip_latch(
            RiskLatch.DAILY_LOSS, session_date=SESSION, occurred_at=_at(5), reason="the real cause"
        )
        store.trip_latch(
            RiskLatch.DAILY_LOSS, session_date=SESSION, occurred_at=_at(6), reason="a later echo"
        )
    connection = sqlite3.connect(state_path)
    reasons = [
        row[0]
        for row in connection.execute(
            "SELECT reason FROM session_risk_latch WHERE latch = 'DAILY_LOSS'"
        )
    ]
    connection.close()
    assert reasons == ["the real cause"]


@pytest.mark.adversarial
def test_an_unreasoned_latch_is_refused(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store, pytest.raises(SessionRiskStateError):
        store.trip_latch(
            RiskLatch.DAILY_LOSS, session_date=SESSION, occurred_at=_at(5), reason="   "
        )


@pytest.mark.adversarial
def test_recording_against_an_unopened_session_is_refused(tmp_path: Path) -> None:
    """'Not opened' and 'opened flat' are the same number only to a system that permits a trade."""
    with SessionRiskStateStore(tmp_path / "empty.sqlite3") as store:
        with pytest.raises(SessionRiskStateError):
            store.state_for(session_date=SESSION, now=_at(1))
        with pytest.raises(SessionRiskStateError):
            store.record_realised_pnl(
                Decimal("-1"),
                session_date=SESSION,
                trading_symbol="TCS",
                occurred_at=_at(1),
                reason="loss",
            )


@pytest.mark.adversarial
def test_the_daily_loss_limit_is_not_active_until_enough_sessions_exist(state_path: Path) -> None:
    """`R.04` — the algorithm is complete from day one; its ACTIVATION waits for evidence."""
    with SessionRiskStateStore(state_path) as store:
        limit = store.daily_loss_limit(as_of=SESSION)
    assert limit.is_active is False
    assert limit.limit_rupees is None
    assert "NOT ACTIVE" in limit.describe()


@pytest.mark.adversarial
def test_the_daily_loss_limit_activates_and_is_derived_from_the_books_own_volatility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    with SessionRiskStateStore(path) as store:
        for index in range(MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT):
            session = date(2026, 7, 1) + timedelta(days=index)
            store.open_session(
                session_date=session,
                opening_equity_rupees=TEN_LAKH,
                occurred_at=datetime(2026, 7, 1, 9, 15, tzinfo=IST) + timedelta(days=index),
            )
            store.record_realised_pnl(
                Decimal(5_000 if index % 2 else -4_000),
                session_date=session,
                trading_symbol="RELIANCE",
                occurred_at=datetime(2026, 7, 1, 15, 20, tzinfo=IST) + timedelta(days=index),
                reason="session result",
            )
            store.close_session(session_date=session)
        limit = store.daily_loss_limit(as_of=SESSION)
    assert limit.is_active is True
    assert limit.sessions_observed == MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT
    assert limit.limit_rupees is not None
    assert limit.sigma_daily_rupees is not None
    # Pinned to an INDEPENDENTLY computed sigma, not to the implementation's own output.
    # `limit == z * sigma` alone is a tautology over two of its own fields, and `A.105` proved it:
    # a mutant using n instead of n-1 in the variance passed the whole suite.
    results = [
        Decimal(5_000 if index % 2 else -4_000)
        for index in range(MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT)
    ]
    mean = sum(results, Decimal(0)) / Decimal(len(results))
    expected_sigma = (
        sum(((value - mean) ** 2 for value in results), Decimal(0)) / Decimal(len(results) - 1)
    ).sqrt()
    assert abs(limit.sigma_daily_rupees - expected_sigma) < Decimal("0.01")
    assert limit.limit_rupees == limit.z_quantile * limit.sigma_daily_rupees


@pytest.mark.adversarial
def test_a_book_whose_sessions_all_returned_the_same_figure_gets_no_limit(tmp_path: Path) -> None:
    """`A.105` finding 6 — the ordinary state of a paper book's first month.

    Twenty closed sessions in which nothing traded give a variance of zero, hence a limit of Rs 0,
    which is ACTIVE and tripped by the first paisa. That halt is a latch only an operator can clear
    (`R.22`): a permanent stop earned by rounding. The limit must be NOT ACTIVE instead.
    """
    path = tmp_path / "flat.sqlite3"
    with SessionRiskStateStore(path) as store:
        for index in range(MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT):
            session = date(2026, 7, 1) + timedelta(days=index)
            store.open_session(
                session_date=session,
                opening_equity_rupees=TEN_LAKH,
                occurred_at=datetime(2026, 7, 1, 9, 15, tzinfo=IST) + timedelta(days=index),
            )
            store.close_session(session_date=session)
        limit = store.daily_loss_limit(as_of=SESSION)
    assert limit.is_active is False
    assert limit.sessions_observed == MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT
    assert "same result" in (limit.unavailable_reason or "")


@pytest.mark.adversarial
def test_the_rate_window_is_correct_from_any_timezone(state_path: Path) -> None:
    """`A.105` finding 5 — the window compared ISO strings whose OFFSETS differed.

    From Asia/Tokyo the window reported zero orders, so the rate limit never fired and the 10/sec
    registration threshold was unguarded; from UTC it counted five-hour-old orders as current.
    """
    from zoneinfo import ZoneInfo as _Zone

    with SessionRiskStateStore(state_path) as store:
        for offset in range(9):
            store.record_order_sent(
                session_date=SESSION, trading_symbol="RELIANCE", occurred_at=_at(offset)
            )
        counts = {
            zone: store.state_for(
                session_date=SESSION,
                now=_at(9).astimezone(_Zone(zone)),
                rate_window=timedelta(seconds=30),
            ).orders_in_rate_window
            for zone in ("Asia/Kolkata", "UTC", "Asia/Tokyo", "America/New_York")
        }
    assert set(counts.values()) == {9}, counts


@pytest.mark.adversarial
def test_concurrent_realisations_do_not_lose_a_loss(state_path: Path) -> None:
    """Two decisions racing the same session state. `O.85`'s held-constant axis, tested.

    Each thread books a Rs 10,000 loss; all eight must land, because a lost loss is a book that
    believes it is above a limit it has already breached.
    """
    barrier = threading.Barrier(8)

    def book(index: int) -> None:
        with SessionRiskStateStore(state_path) as store:
            barrier.wait()
            store.record_realised_pnl(
                Decimal("-10000"),
                session_date=SESSION,
                trading_symbol=f"SYM{index}",
                occurred_at=_at(10 + index),
                reason="concurrent loss",
            )

    threads = [threading.Thread(target=book, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with SessionRiskStateStore(state_path) as store:
        state = store.state_for(session_date=SESSION, now=_at(30))
    assert state.realised_pnl_rupees == Decimal("-80000")


@pytest.mark.adversarial
def test_a_float_amount_is_refused(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store, pytest.raises(SessionRiskStateError):
        store.record_realised_pnl(
            -10000.0,  # type: ignore[arg-type]
            session_date=SESSION,
            trading_symbol="TCS",
            occurred_at=_at(1),
            reason="float",
        )


@pytest.mark.adversarial
def test_a_naive_timestamp_is_refused(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store, pytest.raises(SessionRiskStateError):
        store.record_order_sent(
            session_date=SESSION,
            trading_symbol="TCS",
            occurred_at=datetime(2026, 8, 13, 9, 15),  # noqa: DTZ001 — the naive clock is the input
        )


@pytest.mark.adversarial
def test_a_book_above_its_peak_is_not_in_drawdown(state_path: Path) -> None:
    with SessionRiskStateStore(state_path) as store:
        state = store.record_realised_pnl(
            Decimal("40000"),
            session_date=SESSION,
            trading_symbol="RELIANCE",
            occurred_at=_at(1),
            reason="winner",
        )
    assert state.drawdown_rupees == Decimal(0)
    assert state.drawdown_fraction == Decimal(0)


@pytest.mark.adversarial
def test_a_zero_tolerance_for_false_halts_is_refused_rather_than_infinite() -> None:
    with pytest.raises(SessionRiskStateError):
        false_halt_quantile(false_halts_per_year=Decimal(0))
