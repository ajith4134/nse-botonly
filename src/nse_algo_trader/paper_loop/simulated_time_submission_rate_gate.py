"""The order-rate limiter, counting in SIMULATED time, for the paper loop (`A.114`).

`F02`'s `OrderSubmissionRateLimiter` is what keeps this system under the 10-orders-per-second
registration threshold (`A.101`), and until now the paper loop did not have it: the placer was built
with `AlwaysPermits`, so every paper P&L assumed an order flow the wire would not have accepted.
Replaying all three sessions' recorded arrivals through a real limiter measured the gap — **335
arrivals, 70 refused (21%)** — which is the "second code path, never exercised" failure `A.108`
decision 2 exists to prevent.

**Two things had to be decided before it could be wired in, and both are recorded in `A.114`.**

*It counts in SIMULATED time.* The limiter's clock seams are driven from the replay clock, so a
session replayed at 3 p.m. on a Saturday is rate-limited exactly as the session itself would have
been. Wall time is meaningless here — a day's orders arrive within seconds of it — and using it
would make the gate either inert or arbitrarily punitive depending on how fast the host is.

*It never queues.* `OrderSubmissionRateLimiter.acquire` waits for budget by SLEEPING, in real
seconds, and a replay's clock does not advance while it sleeps: an order queued behind a five-second
window would block the process for five real seconds and still be refused, and one queued against a
twenty-five-minute horizon would block for twenty-five real minutes. So this gate asks only whether
there is room AT the decision instant. That is also the honest semantics for a five-minute step:
there is no "later" inside one simulated instant, and the next opportunity is the next step.

**What that means for the paper record, stated plainly:** a burst of entries decided at one instant
is now truncated to what the tightest window permits, and the rest are refused with the window that
refused them named. Refusals are a RESULT, not an error — a paper session that sends more orders
than the wire would carry is measuring a system that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from nse_algo_trader.order_path.broker_order_facility_facts import OrderRateLimit
from nse_algo_trader.order_path.order_submission_rate_limiter import (
    CLOCK_TICK_MILLISECONDS,
    OrderSubmissionRateLimiter,
    RateLimitOutcomeKind,
)
from nse_algo_trader.replay_session_clock import ReplaySessionClock

NANOSECONDS_PER_SECOND = 1_000_000_000
INTENT_VALIDITY = timedelta(milliseconds=CLOCK_TICK_MILLISECONDS)
"""How long a request stays valid inside the gate: one clock tick.

Not a patience setting — the smallest amount that is not ALREADY expired when the limiter reads its
own clock. Anything longer would make the limiter sleep in real seconds against a clock that only
moves when the replay says so."""


@dataclass(slots=True)
class ReplayDrivenDispatchClock:
    """Both of the limiter's clock seams, driven by the replay clock and nothing else.

    Monotonic and wall are derived from the SAME simulated instant on purpose: the limiter's ratchet
    exists to survive the two disagreeing, and a replay in which they disagree would be testing the
    ratchet rather than the rate limit.
    """

    clock: ReplaySessionClock

    def wall_clock_epoch_seconds(self) -> float:
        return self.clock.now.timestamp()

    def monotonic_nanoseconds(self) -> int:
        return int(self.clock.now.timestamp() * NANOSECONDS_PER_SECOND)


@dataclass(slots=True)
class SimulatedTimeSubmissionRateGate:
    """`SubmissionRateGate` for a replay: real limiter, simulated clock, never blocking.

    Satisfies the protocol `CrashSafeOrderPlacer` expects, so the placer above it is the identical
    code the live path will run — which is the whole argument of `A.108` decision 2.
    """

    clock: ReplaySessionClock
    session_date: datetime | None = None
    store_path: Path | None = None
    published_limits: tuple[OrderRateLimit, ...] | None = None
    _limiter: OrderSubmissionRateLimiter | None = field(default=None, repr=False)
    _granted: int = field(default=0, repr=False)
    _refused: int = field(default=0, repr=False)
    _refusals_by_window: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        dispatch = ReplayDrivenDispatchClock(self.clock)
        session = self.clock.session.trading_day
        store = self.store_path or Path(f"~/.nse_algo_trader/paper_rate_{session}.sqlite3")
        self._limiter = OrderSubmissionRateLimiter(
            session,
            published_limits=(
                list(self.published_limits) if self.published_limits is not None else None
            ),
            store_path=store.expanduser(),
            wall_clock_epoch_seconds=dispatch.wall_clock_epoch_seconds,
            monotonic_nanoseconds=dispatch.monotonic_nanoseconds,
        )

    @property
    def limiter(self) -> OrderSubmissionRateLimiter:
        assert self._limiter is not None
        return self._limiter

    @property
    def granted(self) -> int:
        return self._granted

    @property
    def refused(self) -> int:
        return self._refused

    @property
    def refusals_by_window(self) -> tuple[tuple[str, int], ...]:
        """Which ceiling did the refusing, most often first — what an operator reads first."""
        return tuple(
            sorted(self._refusals_by_window.items(), key=lambda pair: (-pair[1], pair[0]))
        )

    def acquire(self, exchange: str, deadline: datetime) -> tuple[bool, str]:
        """Room at THIS instant, or a refusal naming the window that had none.

        The placer's own `deadline` is deliberately ignored: it is the intent's validity in
        SIMULATED time, and honouring it would ask the limiter to sleep that long in real time.
        """
        del deadline
        outcome = self.limiter.acquire(exchange, self.clock.now + INTENT_VALIDITY)
        if outcome.granted:
            self._granted += 1
            return True, outcome.reason
        self._refused += 1
        window = outcome.blocking_window
        key = (
            f"{window.window_seconds}s ceiling of {window.permitted_orders}"
            if window is not None
            else outcome.kind.value
        )
        self._refusals_by_window[key] = self._refusals_by_window.get(key, 0) + 1
        return False, outcome.reason

    def describe(self) -> str:
        if not self._granted and not self._refused:
            return "the rate gate was never asked"
        share = self._refused / (self._granted + self._refused)
        breakdown = ", ".join(f"{key} x{count}" for key, count in self.refusals_by_window)
        return (
            f"rate gate: {self._granted} granted, {self._refused} refused "
            f"({share:.1%})" + (f" — {breakdown}" if breakdown else "")
        )

    def close(self) -> None:
        if self._limiter is not None:
            self._limiter.close()


def is_rate_refusal(kind: RateLimitOutcomeKind) -> bool:
    """Whether an outcome means the WIRE refused, rather than the intent expiring on its own."""
    return kind in (
        RateLimitOutcomeKind.EXPIRED_WHILE_QUEUED,
        RateLimitOutcomeKind.EXPIRED_BEFORE_QUEUEING,
        RateLimitOutcomeKind.REFUSED_QUEUE_FULL,
    )
