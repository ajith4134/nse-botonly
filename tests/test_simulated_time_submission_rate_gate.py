"""The rate gate the paper loop runs with: real limiter, simulated clock, never blocking (`A.114`).

Three behaviours decide whether this is honest: it must count in the REPLAY's time rather than the
host's, it must never sleep (a replay clock does not advance while a limiter waits, so a queued
order would hang the process for as long as its validity), and a refusal must name the ceiling that
refused it.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderRateLimit,
    RateLimitScope,
)
from nse_algo_trader.paper_loop.simulated_time_submission_rate_gate import (
    SimulatedTimeSubmissionRateGate,
)
from nse_algo_trader.replay_session_clock import ReplaySessionClock, session_for

SESSION_DATE = date(2026, 8, 11)


def _ceiling(maximum_orders: int, window_seconds: int) -> OrderRateLimit:
    return OrderRateLimit(
        maximum_orders=maximum_orders,
        window_seconds=window_seconds,
        scope=RateLimitScope.BROKER_CEILING,
        effective_from=date(2016, 1, 1),
        source="synthetic fact for hermetic verification",
        aligned_to_calendar_window=False,
    )


def _gate(tmp_path: Path, clock: ReplaySessionClock) -> SimulatedTimeSubmissionRateGate:
    return SimulatedTimeSubmissionRateGate(
        clock=clock,
        store_path=tmp_path / "rate.sqlite3",
        published_limits=(_ceiling(3, 1), _ceiling(7, 5)),
    )


def test_a_burst_at_one_simulated_instant_is_truncated_to_the_tightest_ceiling(
    tmp_path: Path,
) -> None:
    """The loop decides many entries at one instant; the wire would not carry them all."""
    clock = ReplaySessionClock(session_for(SESSION_DATE), {})
    gate = _gate(tmp_path, clock)
    try:
        verdicts = [gate.acquire("NSE", clock.now) for _ in range(10)]
        granted = [permitted for permitted, _ in verdicts if permitted]
        assert len(granted) == 3, "the 1-second ceiling of 3 is the tightest at one instant"
        assert gate.refused == 7
        assert gate.refusals_by_window, "a refusal must name the ceiling that refused it"
        assert "1s ceiling of 3" in gate.refusals_by_window[0][0]
    finally:
        gate.close()


def test_the_budget_refills_as_the_replay_clock_advances_not_as_the_host_waits(
    tmp_path: Path,
) -> None:
    """Simulated time is the only time here: no real seconds pass, and the window still reopens."""
    clock = ReplaySessionClock(session_for(SESSION_DATE), {})
    gate = _gate(tmp_path, clock)
    try:
        started = time.perf_counter()
        for _ in range(3):
            assert gate.acquire("NSE", clock.now)[0]
        assert not gate.acquire("NSE", clock.now)[0], "the 1-second ceiling is spent"

        clock.advance_by(timedelta(minutes=5))
        assert gate.acquire("NSE", clock.now)[0], (
            "five simulated minutes later every window has room again"
        )
        assert time.perf_counter() - started < 1.0, (
            "the gate must never sleep: a replay's clock does not advance while a limiter waits"
        )
    finally:
        gate.close()


def test_the_gate_never_blocks_even_when_the_placers_deadline_is_far_away(
    tmp_path: Path,
) -> None:
    """The placer passes an intent validity in SIMULATED time; honouring it would sleep for real."""
    clock = ReplaySessionClock(session_for(SESSION_DATE), {})
    gate = _gate(tmp_path, clock)
    try:
        for _ in range(3):
            gate.acquire("NSE", clock.now + timedelta(minutes=25))
        started = time.perf_counter()
        permitted, reason = gate.acquire("NSE", clock.now + timedelta(minutes=25))
        elapsed = time.perf_counter() - started
        assert not permitted
        assert elapsed < 1.0, f"the gate waited {elapsed:.1f}s against a 25-minute deadline"
        assert reason, "a refusal must carry the limiter's own words"
    finally:
        gate.close()


def test_the_gate_describes_itself_for_the_session_report(tmp_path: Path) -> None:
    clock = ReplaySessionClock(session_for(SESSION_DATE), {})
    gate = _gate(tmp_path, clock)
    try:
        assert "never asked" in gate.describe()
        for _ in range(5):
            gate.acquire("NSE", clock.now)
        described = gate.describe()
        assert "3 granted" in described
        assert "2 refused" in described
        assert "40.0%" in described
    finally:
        gate.close()
