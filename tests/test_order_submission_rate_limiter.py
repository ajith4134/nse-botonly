"""`L3.06` — the limiter must be the thing that waits, and it must never mint budget.

The behavioural tests run on an injected clock (`R.05`/Rule J: a fake behind a DI seam, never a
substitute for a real-data sign-off) because the alternative to a fake day is a real one, and a test
that takes a day to prove the day window binds is a test that is never run. The two claims that
cannot be faked — that the limiter *genuinely blocks* wall-clock time, and that the real dated fact
table produces all three windows — are asserted against the real clock and the real facts.
"""

from __future__ import annotations

import itertools
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as strategy_for

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderRateLimit,
    RateLimitScope,
    order_rate_limits,
)
from nse_algo_trader.order_path.order_submission_rate_limiter import (
    DispatchJitterEstimator,
    OrderRateLimitError,
    OrderSubmissionRateLimiter,
    RateLimitOutcomeKind,
    binding_windows_from,
    queue_capacity_from,
)

SESSION = date(2026, 8, 13)
FAR_FUTURE = datetime(2026, 8, 13, 23, 0, tzinfo=UTC)

# A validity of one millisecond: long enough that the intent is not already stale when it arrives,
# short enough that a refusal comes back as an expiry almost immediately instead of blocking the
# test. It exercises the whole path — probe, queue, wait, expire — rather than short-circuiting it.
IMMEDIATE_VALIDITY = timedelta(milliseconds=1)


class SteerableSessionClock:
    """A wall clock and a monotonic clock that only move when a test says so.

    Both are driven from one cursor so that the limiter's ratchet sees a coherent pair; a test that
    advanced only the wall clock would be testing the ratchet's fallback rather than its behaviour.
    """

    def __init__(self, epoch_seconds: float) -> None:
        self._epoch_seconds = epoch_seconds
        self._monotonic_ns = 1_000_000_000

    def wall_clock_epoch_seconds(self) -> float:
        return self._epoch_seconds

    def monotonic_nanoseconds(self) -> int:
        return self._monotonic_ns

    def advance(self, seconds: float) -> None:
        self._epoch_seconds += seconds
        self._monotonic_ns += int(seconds * 1_000_000_000)

    def step_wall_clock_backwards(self, seconds: float) -> None:
        """Only the wall clock moves — which is exactly what an NTP correction looks like."""
        self._epoch_seconds -= seconds

    @property
    def now(self) -> datetime:
        return datetime.fromtimestamp(self._epoch_seconds, tz=UTC)


_PROPERTY_STORE_ORDINAL = itertools.count()
"""One fresh store per property CALL, replays included. See the comment at its use."""


def broker_ceiling(maximum_orders: int, window_seconds: int) -> OrderRateLimit:
    """A synthetic broker ceiling, shaped exactly like the real dated facts."""
    return OrderRateLimit(
        maximum_orders=maximum_orders,
        window_seconds=window_seconds,
        scope=RateLimitScope.BROKER_CEILING,
        effective_from=date(2016, 1, 1),
        source="synthetic fact for hermetic verification",
        aligned_to_calendar_window=False,
    )


def regulatory_threshold(maximum_orders: int, window_seconds: int) -> OrderRateLimit:
    return replace(
        broker_ceiling(maximum_orders, window_seconds),
        scope=RateLimitScope.REGULATORY_THRESHOLD,
    )


def limiter_on(
    store_path: Path,
    limits: list[OrderRateLimit],
    clock: SteerableSessionClock | None = None,
    session: date = SESSION,
) -> OrderSubmissionRateLimiter:
    if clock is None:
        return OrderSubmissionRateLimiter(session, published_limits=limits, store_path=store_path)
    return OrderSubmissionRateLimiter(
        session,
        published_limits=limits,
        store_path=store_path,
        wall_clock_epoch_seconds=clock.wall_clock_epoch_seconds,
        monotonic_nanoseconds=clock.monotonic_nanoseconds,
    )


# --- the windows themselves -------------------------------------------------------------------


@pytest.mark.unit
def test_the_windows_are_derived_from_the_dated_fact_table_not_from_literals(
    tmp_path: Path,
) -> None:
    """Nothing in the limiter may know 10, 400 or 5,000 — it must read them (`R.03`)."""
    facts = order_rate_limits(on=SESSION)
    with OrderSubmissionRateLimiter(SESSION, store_path=tmp_path / "rate.sqlite3") as limiter:
        windows = limiter.binding_windows
        assert [window.window_seconds for window in windows] == [1, 60, 86_400]
        published = {window.window_seconds: window.published_maximum_orders for window in windows}
        assert published == {
            fact.window_seconds: min(
                other.maximum_orders
                for other in facts
                if other.window_seconds == fact.window_seconds
            )
            for fact in facts
        }


@pytest.mark.unit
def test_all_three_windows_bind_simultaneously_and_the_tightest_one_wins(tmp_path: Path) -> None:
    """Every window is checked on every acquisition, and the refusal names the tightest."""
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(2, 1), broker_ceiling(3, 60), broker_ceiling(4, 3_600)]
    with limiter_on(tmp_path / "rate.sqlite3", limits, clock) as limiter:
        assert [window.window_seconds for window in limiter.binding_windows] == [1, 60, 3_600]

        # Two fit in the one-second window; the third is refused by it, not by anything longer.
        granted = RateLimitOutcomeKind.GRANTED_IMMEDIATELY
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).kind is granted
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).kind is granted
        refused_by_second = limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY)
        assert refused_by_second.granted is False
        assert refused_by_second.blocking_window is not None
        assert refused_by_second.blocking_window.window_seconds == 1

        # A second later the one-second window has room and the minute window becomes the binder.
        clock.advance(1.1)
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        refused_by_minute = limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY)
        assert refused_by_minute.granted is False
        assert refused_by_minute.blocking_window is not None
        assert refused_by_minute.blocking_window.window_seconds == 60

        # A minute later the hour window is the only one left with nothing to give.
        clock.advance(60.0)
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        refused_by_hour = limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY)
        assert refused_by_hour.granted is False
        assert refused_by_hour.blocking_window is not None
        assert refused_by_hour.blocking_window.window_seconds == 3_600


@pytest.mark.unit
def test_the_safety_margin_is_reserved_only_against_the_regulatory_threshold(
    tmp_path: Path,
) -> None:
    """A broker ceiling costs an order when breached; the threshold changes the category of flow."""
    limits = [regulatory_threshold(10, 1), broker_ceiling(400, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits) as limiter:
        per_second, per_minute = limiter.binding_windows
        assert per_second.scope is RateLimitScope.REGULATORY_THRESHOLD
        assert per_second.reserved_for_safety_margin >= 1
        assert per_second.permitted_orders == 10 - per_second.reserved_for_safety_margin
        assert per_minute.reserved_for_safety_margin == 0
        # The margin is a measurement, not a number someone typed.
        measurement = limiter.dispatch_jitter
        assert measurement.sample_size > 0
        assert measurement.jitter_seconds > 0


@pytest.mark.unit
def test_a_widening_jitter_measurement_widens_the_margin(tmp_path: Path) -> None:
    """The margin self-calibrates: a host that starts contending gets more room, not less."""
    limits = [regulatory_threshold(10, 1), broker_ceiling(400, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits) as limiter:
        before = limiter.binding_windows[0].reserved_for_safety_margin
        # A quarter of a second of spread at ten orders a second displaces two and a half orders.
        limiter.record_dispatch_latency(0.01)
        limiter.record_dispatch_latency(0.26)
        after = limiter.binding_windows[0].reserved_for_safety_margin
        assert after == 3
        assert after > before
        assert limiter.binding_windows[0].permitted_orders == 7


@pytest.mark.unit
def test_the_queue_bound_is_derived_from_the_windows(tmp_path: Path) -> None:
    """The bound is one full window of the longest ceiling that cycles inside a session."""
    with OrderSubmissionRateLimiter(SESSION, store_path=tmp_path / "rate.sqlite3") as limiter:
        assert limiter.queue_capacity == queue_capacity_from(limiter.binding_windows)
        assert limiter.queue_capacity == 400  # the minute ceiling, never the day one


@pytest.mark.unit
def test_a_window_that_can_never_bind_is_dropped_rather_than_enforced() -> None:
    """A minute ceiling of 5 makes a per-second ceiling of 10 unreachable, and vice versa."""
    dominated_short_window = binding_windows_from(
        [broker_ceiling(10, 1), broker_ceiling(5, 60)], jitter_seconds=0.0
    )
    assert [window.window_seconds for window in dominated_short_window] == [60]

    unreachable_long_window = binding_windows_from(
        [broker_ceiling(10, 1), broker_ceiling(1_200, 60)], jitter_seconds=0.0
    )
    assert [window.window_seconds for window in unreachable_long_window] == [1]


# --- blocking, queueing and expiry ------------------------------------------------------------


@pytest.mark.unit
def test_it_genuinely_waits_rather_than_dropping_the_order(tmp_path: Path) -> None:
    """Measured against the real clock: the limiter is the thing that blocks (`221` §8)."""
    limits = [broker_ceiling(2, 1), broker_ceiling(1_000, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits) as limiter:
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        started_at = time.perf_counter()
        outcomes = [limiter.acquire("NSE", deadline) for _ in range(3)]
        elapsed = time.perf_counter() - started_at

        assert [outcome.granted for outcome in outcomes] == [True, True, True]
        assert outcomes[2].kind is RateLimitOutcomeKind.GRANTED_AFTER_WAIT
        assert elapsed >= 1.0, (
            "three orders through a 2-per-second window cannot take under a second"
        )
        assert outcomes[2].waited_seconds >= 1.0
        assert outcomes[2].blocking_window is not None
        assert outcomes[2].blocking_window.window_seconds == 1


@pytest.mark.unit
def test_an_intent_that_ages_past_its_validity_while_queued_is_expired_not_sent_late(
    tmp_path: Path,
) -> None:
    """A stale order is worse than no order, so the wait ends at the intent's own deadline."""
    limits = [broker_ceiling(1, 30), broker_ceiling(1_000, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits) as limiter:
        assert limiter.acquire("NSE", datetime.now(UTC) + timedelta(seconds=5)).granted is True

        started_at = time.perf_counter()
        expired = limiter.acquire("NSE", datetime.now(UTC) + timedelta(seconds=0.4))
        elapsed = time.perf_counter() - started_at

        assert expired.granted is False
        assert expired.kind is RateLimitOutcomeKind.EXPIRED_WHILE_QUEUED
        assert "expired while it was queued" in expired.reason
        assert expired.blocking_window is not None
        assert expired.blocking_window.window_seconds == 30
        assert elapsed < 25, "it waited past the deadline instead of expiring the intent"

        # And the expiry must not have spent budget it never used.
        spent = {b.window.window_seconds: b.orders_used for b in limiter.budget_remaining("NSE")}
        assert spent[30] == 1


@pytest.mark.unit
def test_an_already_expired_intent_never_reaches_the_queue(tmp_path: Path) -> None:
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(5, 1), broker_ceiling(50, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits, clock) as limiter:
        outcome = limiter.acquire("NSE", clock.now - timedelta(seconds=1))
        assert outcome.granted is False
        assert outcome.kind is RateLimitOutcomeKind.EXPIRED_BEFORE_QUEUEING
        assert outcome.waited_seconds == 0.0
        assert all(budget.orders_used == 0 for budget in limiter.budget_remaining("NSE"))


@pytest.mark.unit
def test_the_queue_is_bounded_and_refuses_rather_than_growing(tmp_path: Path) -> None:
    """Back-pressure, not warehousing: past the derived depth the load is refused at the door."""
    limits = [broker_ceiling(1, 5), broker_ceiling(2, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits) as limiter:
        assert limiter.queue_capacity == 1
        assert limiter.acquire("NSE", datetime.now(UTC) + timedelta(seconds=1)).granted is True

        import threading

        def occupy_the_queue() -> None:
            limiter.acquire("NSE", datetime.now(UTC) + timedelta(seconds=2))

        waiter = threading.Thread(target=occupy_the_queue)
        waiter.start()
        try:
            deadline = time.perf_counter() + 2
            while limiter.queue_depth("NSE") == 0 and time.perf_counter() < deadline:
                time.sleep(0.01)
            assert limiter.queue_depth("NSE") == 1
            refused = limiter.acquire("NSE", datetime.now(UTC) + timedelta(seconds=2))
            assert refused.granted is False
            assert refused.kind is RateLimitOutcomeKind.REFUSED_QUEUE_FULL
            assert "already queued" in refused.reason
        finally:
            waiter.join(timeout=10)
        assert limiter.queue_depth("NSE") == 0


# --- per-exchange accounting and carried state --------------------------------------------------


@pytest.mark.unit
def test_each_exchange_is_counted_separately(tmp_path: Path) -> None:
    """The threshold is per exchange (`223` §3), so NSE's flow must not refuse a BSE order."""
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(2, 1), broker_ceiling(20, 60)]
    with limiter_on(tmp_path / "rate.sqlite3", limits, clock) as limiter:
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is False

        assert limiter.acquire("BSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        assert limiter.acquire("BSE", clock.now + IMMEDIATE_VALIDITY).granted is True

        nse = {b.window.window_seconds: b.orders_used for b in limiter.budget_remaining("NSE")}
        bse = {b.window.window_seconds: b.orders_used for b in limiter.budget_remaining("BSE")}
        assert nse[60] == 2
        assert bse[60] == 2
        assert limiter.acquire("MCX", clock.now + IMMEDIATE_VALIDITY).granted is True


@pytest.mark.unit
def test_the_day_counter_survives_re_instantiating_the_limiter(tmp_path: Path) -> None:
    """A day bucket that forgets on restart is not a day bucket (`221` §8)."""
    store = tmp_path / "rate.sqlite3"
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(2, 1), broker_ceiling(4, 86_400)]

    first = limiter_on(store, limits, clock)
    assert first.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
    assert first.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
    first.close()

    clock.advance(2.0)
    second = limiter_on(store, limits, clock)
    try:
        carried = {b.window.window_seconds: b.orders_used for b in second.budget_remaining("NSE")}
        assert carried[86_400] == 2
        # Spaced out, so that the window with nothing left to give is the day one and not the
        # one-second one — the point of the test is the count that crossed the restart.
        assert second.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        clock.advance(1.1)
        assert second.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        clock.advance(1.1)
        exhausted = second.acquire("NSE", clock.now + IMMEDIATE_VALIDITY)
        assert exhausted.granted is False
        assert exhausted.blocking_window is not None
        assert exhausted.blocking_window.window_seconds == 86_400
    finally:
        second.close()


@pytest.mark.unit
def test_a_different_session_starts_from_zero_and_prunes_the_old_counters(tmp_path: Path) -> None:
    store = tmp_path / "rate.sqlite3"
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(2, 1), broker_ceiling(4, 86_400)]

    yesterday = limiter_on(store, limits, clock, session=date(2026, 8, 12))
    assert yesterday.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
    yesterday.close()

    today = limiter_on(store, limits, clock, session=SESSION)
    try:
        fresh = {b.window.window_seconds: b.orders_used for b in today.budget_remaining("NSE")}
        assert fresh[86_400] == 0
    finally:
        today.close()

    inspection = sqlite3.connect(str(store))
    try:
        tables = [
            str(row[0])
            for row in inspection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                ("order_submission_rate_%",),
            ).fetchall()
        ]
    finally:
        inspection.close()
    assert all(name.endswith("20260813") for name in tables)


# --- property ------------------------------------------------------------------------------------


@pytest.mark.property
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    gaps_milliseconds=strategy_for.lists(
        strategy_for.integers(min_value=0, max_value=1_500), min_size=1, max_size=60
    )
)
def test_no_arrival_pattern_can_put_more_than_the_limit_in_any_window(
    tmp_path: Path, gaps_milliseconds: list[int]
) -> None:
    """Whatever the arrivals look like, every window's own count is never exceeded.

    Checked the way the exchange would check it — over every window ending at a granted order —
    rather than by trusting the limiter's own bookkeeping.
    """
    # A counter, NOT a hash of the example. Hypothesis replays an example to confirm a failure,
    # and a store named after the example is the SAME store on the replay — already full of the
    # first call's orders, so the limiter grants fewer, the property passes, and the failure is
    # reported as flaky instead of as the defect it was. That is how `A.113` stayed hidden.
    store = tmp_path / f"property_{next(_PROPERTY_STORE_ORDINAL)}.sqlite3"
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(3, 1), broker_ceiling(7, 5), broker_ceiling(11, 30)]
    limiter = limiter_on(store, limits, clock)
    try:
        granted_at_milliseconds: list[int] = []
        for gap in gaps_milliseconds:
            clock.advance(gap / 1_000)
            # The deadline is the current instant, so nothing ever blocks and the arrival pattern
            # is exactly the one the property generated.
            if limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted:
                granted_at_milliseconds.append(int(clock.wall_clock_epoch_seconds() * 1_000))

        for window in limiter.binding_windows:
            for index, stamp in enumerate(granted_at_milliseconds):
                inside = sum(
                    1
                    for earlier in granted_at_milliseconds[: index + 1]
                    if earlier >= stamp - window.window_milliseconds
                )
                assert inside <= window.permitted_orders, (
                    f"{inside} orders inside a {window.window_seconds}s window that permits "
                    f"{window.permitted_orders}"
                )
    finally:
        limiter.close()


# --- adversarial ---------------------------------------------------------------------------------


@pytest.mark.adversarial
def test_a_backwards_clock_jump_cannot_mint_extra_budget(tmp_path: Path) -> None:
    """Spent budget stays spent even when the operating system says the second never happened."""
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(3, 60), broker_ceiling(10, 86_400)]
    with limiter_on(tmp_path / "rate.sqlite3", limits, clock) as limiter:
        for _ in range(3):
            assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
        assert limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is False

        # The deadline is taken from the instant BEFORE the step, because an intent decided before
        # the jump is exactly what a real caller would still be holding across one.
        instant_before_the_step = clock.now
        clock.step_wall_clock_backwards(600.0)

        refused = limiter.acquire("NSE", instant_before_the_step + IMMEDIATE_VALIDITY)
        assert refused.granted is False, "a backwards clock step reissued budget already spent"
        assert refused.blocking_window is not None
        assert refused.blocking_window.window_seconds == 60
        used = {b.window.window_seconds: b.orders_used for b in limiter.budget_remaining("NSE")}
        assert used[60] == 3


@pytest.mark.adversarial
def test_a_backwards_clock_jump_across_a_restart_cannot_mint_extra_budget(tmp_path: Path) -> None:
    """The ratchet's floor is the store, so restarting into the past does not help either."""
    store = tmp_path / "rate.sqlite3"
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limits = [broker_ceiling(3, 60), broker_ceiling(10, 86_400)]

    first = limiter_on(store, limits, clock)
    for _ in range(3):
        assert first.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted is True
    first.close()

    instant_before_the_step = clock.now
    clock.step_wall_clock_backwards(600.0)
    second = limiter_on(store, limits, clock)
    try:
        refused = second.acquire("NSE", instant_before_the_step + IMMEDIATE_VALIDITY)
        assert refused.granted is False
        assert refused.blocking_window is not None
        assert refused.blocking_window.window_seconds == 60
    finally:
        second.close()


@pytest.mark.adversarial
def test_a_name_that_is_not_an_exchange_is_refused_rather_than_given_its_own_bucket(
    tmp_path: Path,
) -> None:
    """The name partitions the counters, so anything unusable must never open a bucket."""
    with limiter_on(tmp_path / "rate.sqlite3", [broker_ceiling(2, 1)]) as limiter:
        for hostile in ("", "NSE; DROP TABLE x", "nse-eq", "N" * 20):
            with pytest.raises(OrderRateLimitError, match="usable exchange mnemonic"):
                limiter.acquire(hostile, datetime.now(UTC) + timedelta(seconds=1))


@pytest.mark.adversarial
def test_a_naive_deadline_is_refused(tmp_path: Path) -> None:
    with (
        limiter_on(tmp_path / "rate.sqlite3", [broker_ceiling(2, 1)]) as limiter,
        pytest.raises(OrderRateLimitError, match="must carry a timezone"),
    ):
        limiter.acquire("NSE", datetime(2026, 8, 13, 10, 0))  # noqa: DTZ001 — that is the point


@pytest.mark.adversarial
def test_an_empty_or_nonsensical_fact_table_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(OrderRateLimitError, match="no rate limit facts"):
        binding_windows_from([], jitter_seconds=0.0)
    with pytest.raises(OrderRateLimitError, match="positive number of orders"):
        binding_windows_from([broker_ceiling(0, 1)], jitter_seconds=0.0)


@pytest.mark.adversarial
def test_a_negative_dispatch_latency_is_refused() -> None:
    estimator = DispatchJitterEstimator(retained_samples=4)
    with pytest.raises(OrderRateLimitError, match="negative time"):
        estimator.observe_dispatch_latency(-0.001)


@pytest.mark.adversarial
def test_the_margin_never_closes_the_window_entirely() -> None:
    """A jitter estimate longer than the window itself must not stop the system trading."""
    windows = binding_windows_from([regulatory_threshold(10, 1)], jitter_seconds=5.0)
    assert windows[0].permitted_orders == 1
    assert windows[0].reserved_for_safety_margin == 9


# --- the boundary defect Hypothesis found, and could not hold still (`A.113`) ------------------

FALSIFYING_GAPS_MILLISECONDS = [
    794,
    451,
    264,
    0,
    1305,
    86,
    0,
    276,
    327,
    1237,
    400,
    1,
    846,
    259,
    264,
    1,
    451,
]
"""The arrival pattern that put 8 orders inside a 5-second window permitting 7.

Kept as a literal rather than left to the property to rediscover: it took a shrink to find, the
property masked it on replay through a shared store, and a regression that only reappears when a
generator happens to walk the same path is not a regression test.
"""


@pytest.mark.unit
def test_the_arrival_pattern_that_breached_a_five_second_window_no_longer_does(
    tmp_path: Path,
) -> None:
    """Counted the way an EXCHANGE would count it — by the wall clock, not the limiter's own stamp.

    The limiter's bucket was internally consistent when this failed: it stamped each item with its
    ratcheted dispatch clock, which can read one millisecond ahead of the wall clock it is anchored
    to, so its window sat one tick later than an observer's and admitted one more order at the
    boundary. A limiter is only correct if it is correct against the observer's clock (`A.113`).
    """
    clock = SteerableSessionClock(FAR_FUTURE.timestamp())
    limiter = limiter_on(
        tmp_path / "boundary.sqlite3",
        [broker_ceiling(3, 1), broker_ceiling(7, 5), broker_ceiling(11, 30)],
        clock,
    )
    try:
        granted_at_milliseconds: list[int] = []
        for gap in FALSIFYING_GAPS_MILLISECONDS:
            clock.advance(gap / 1_000)
            if limiter.acquire("NSE", clock.now + IMMEDIATE_VALIDITY).granted:
                granted_at_milliseconds.append(int(clock.wall_clock_epoch_seconds() * 1_000))

        for window in limiter.binding_windows:
            for index, stamp in enumerate(granted_at_milliseconds):
                inside = sum(
                    1
                    for earlier in granted_at_milliseconds[: index + 1]
                    if earlier >= stamp - window.window_milliseconds
                )
                assert inside <= window.permitted_orders, (
                    f"{inside} orders inside a {window.window_seconds}s window that permits "
                    f"{window.permitted_orders}"
                )
    finally:
        limiter.close()


@pytest.mark.unit
def test_the_enforced_window_is_one_clock_tick_wider_than_the_published_one() -> None:
    """The guard is a tick, and it is derived from the clock's resolution rather than chosen."""
    window = broker_ceiling(7, 5)
    derived = binding_windows_from([window], jitter_seconds=0.0)[0]
    assert derived.window_milliseconds == 5_000
    assert derived.enforced_milliseconds == 5_001, (
        "an item stamped exactly one window ago must still be counted; the two clocks that "
        "produce those stamps agree only to the millisecond"
    )
    assert derived.enforced_milliseconds // 1_000 == derived.window_seconds, (
        "the widening must not change which window a refusal is attributed to"
    )
