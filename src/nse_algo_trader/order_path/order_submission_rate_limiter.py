"""The thing that waits, so that the broker never has to silently throw an order away.

`L3.06`. Every ceiling on this system's order flow is published by somebody else — one by the
exchange, three by the broker — and they are measured over different windows by different clocks.
The engine's job is to make all of them true at once, and to do it *locally*, before the order is
on the wire.

**Why the limiter blocks instead of dropping.** NSE/INVG/67858 para B.5 (`docs/research/223` §7)
obliges the broker to *"reject/not accept/not process any orders exceeding the OPS limit"*. That is
not an error this system can reason about: the order simply never exists, and the only evidence of
it is a hole in a reconciliation that runs later. `docs/research/221` §8 therefore fixes the default
as **block and queue, never drop and never burst**. The alternative — discovering the limit by
having orders disappear at the broker — is the failure mode this whole feature exists to prevent.

**Why the safety margin is against the REGULATORY window specifically.** Crossing the broker's
ceiling costs an HTTP 429 and an order. Crossing the exchange's Threshold Order Per Second changes
the *regulatory category of the flow*: below it this is a self-developed white-box algo that needs
no registration, above it registration through the broker is required (`docs/research/223` §2). The
two happen to agree at ten per second today, and they are still budgeted separately, because the
consequences of breaching them are not of the same kind.

**Where the margin comes from, since it may not be a typed number (`R.03`).** The threshold is
applied *"basis the calendar clock second of the broker server"* (§3) — a clock this process does
not own. Between `acquire()` returning here and the order being stamped there lies a transport and
scheduling delay. A *constant* delay is harmless: it shifts the whole dispatch pattern, and a
pattern in which no one-second interval holds more than N orders still holds no more than N after a
uniform shift. What can hurt is the *variation* in that delay — jitter — because it can move an
order across a calendar-second boundary and land it in a second that is already full. At the
maximum permitted release density of `N / W` orders per second, the number of orders sitting within
`J` seconds of a boundary is at most `ceil(N * J / W)`, so that many are reserved and the local
budget becomes `N - ceil(N * J / W)`. `J` is *measured*, never asserted: it is the observed spread
(max minus min) of the dispatch latencies the caller reports back through
`record_dispatch_latency`, over a retained sample one full window of the sustained-dispatch ceiling
long. Before any dispatch has been observed, `J` is bootstrapped from a measurement of this host's
own scheduling spread, which is a genuine lower bound on dispatch jitter and is replaced by real
observations as soon as any exist. The reserve is floored so that at least one order always remains
possible: a budget of zero is not a safety property, it is an outage.

**Why the buckets are per exchange.** The threshold is *"10 OPS per exchange"* (§3), and para F
widens that to "per exchange / segment". The stricter reading is taken — accounting is separate per
exchange, which is also what a dashboard needs to show — because being wrong in the loose direction
changes the regulatory category of the flow rather than merely costing an order.

**Why the counts are on disk.** The day ceiling is 5,000 orders per day. A limiter whose day
counter lives in memory tells the truth exactly until the first restart, after which it believes
the day is young. The counters are therefore held in SQLite, in a table named for the session, so a
process that comes back up mid-session resumes the count it left. The table name carries the
session date rather than the rows carrying a session column, so the calendar-day reset that the
exchange's day window implies is a change of table rather than a deletion — and the sliding
24-hour window inside one session table is exactly the day count, because a session is shorter than
a day.

**Why the clock ratchets.** The buckets compare timestamps, so a wall clock that steps backwards —
NTP correction, a hand-edited system time, a VM resuming from a snapshot — would make already-spent
budget look unspent, which is a way of minting orders out of nothing. The clock here advances on
`monotonic` and takes the wall clock only when it is *ahead*, never when it is behind, and is
floored on construction by the largest timestamp already in the store so that the ratchet survives a
restart too. A forward jump is not defended against here and cannot be: it is indistinguishable
from time having passed. That is the `clock_integrity` engine's problem, not this one's.

**What this module is not.** It holds no opinion about whether an order *should* be sent — that is
the gate's and the kill switch's business (`L3.07`). It only decides when it may be, and refuses to
let a stale one go late: an intent whose own validity expires while it is queued is expired with
that reason, because a stale order is worse than no order.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from pyrate_limiter import (
    AbstractBucket,
    BucketFactory,
    Limiter,
    Rate,
    RateItem,
    SQLiteBucket,
)

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderRateLimit,
    RateLimitScope,
    order_rate_limits,
)
from nse_algo_trader.order_path.trading_intent import OrderPathError

MILLISECONDS_PER_SECOND = 1_000

CLOCK_TICK_MILLISECONDS = 1
"""The resolution of every clock in this module — a property of the millisecond timestamp itself.

Not a tuned margin: it is the smallest amount by which two correct readings of the same instant can
differ, and `RateLimitWindow.enforced_milliseconds` widens by exactly it so that neither reading can
place an order outside a window the other places inside."""
NANOSECONDS_PER_MILLISECOND = 1_000_000

DEFAULT_RATE_LIMIT_STORE_PATH = (
    Path.home() / ".nse_algo_trader" / "order_submission_rate_limits.sqlite3"
)

# The exchange name becomes part of a SQLite table name, and a table name cannot be a bound
# parameter. Rather than quote-escape it, the accepted shape is narrowed to something that cannot
# carry SQL at all — which every real exchange mnemonic (NSE, BSE, MCX, NFO, BFO, CDS) already is.
_EXCHANGE_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")

_TABLE_NAME_PREFIX = "order_submission_rate"


class OrderRateLimitError(OrderPathError):
    """The limiter cannot be built or asked, and guessing would put orders on the wire uncounted."""


class RateLimitOutcomeKind(StrEnum):
    """How an acquisition ended — the axis a caller actually branches on.

    A refusal and an expiry are not the same event and must not be reported as one. A refusal means
    the system is over its own back-pressure bound and the caller should shed load upstream; an
    expiry means this particular intent outlived its usefulness while waiting its turn, and the
    correct response is to re-decide rather than to retry.
    """

    GRANTED_IMMEDIATELY = "granted_immediately"
    GRANTED_AFTER_WAIT = "granted_after_wait"
    EXPIRED_BEFORE_QUEUEING = "expired_before_queueing"
    EXPIRED_WHILE_QUEUED = "expired_while_queued"
    REFUSED_QUEUE_FULL = "refused_queue_full"


@dataclass(frozen=True, slots=True)
class RateLimitWindow:
    """One window this limiter enforces, with the published fact it was derived from.

    `permitted_orders` is what the limiter will actually let through and is always
    `published_maximum_orders - reserved_for_safety_margin`. Both numbers are carried because a
    dashboard that shows only the first cannot explain why the system stopped one order short of
    the published ceiling, and an operator who cannot explain that will eventually raise the ceiling
    by hand.
    """

    window_seconds: int
    permitted_orders: int
    published_maximum_orders: int
    reserved_for_safety_margin: int
    scope: RateLimitScope
    source: str
    aligned_to_calendar_window: bool

    @property
    def window_milliseconds(self) -> int:
        """The window as PUBLISHED, in the unit the buckets count in."""
        return self.window_seconds * MILLISECONDS_PER_SECOND

    @property
    def enforced_milliseconds(self) -> int:
        """The window this limiter actually counts over: published, plus one clock tick.

        **Why it is not the published window** (`A.113`). Two clocks stamp the same instant here
        and they agree only to the millisecond: the ratcheted dispatch clock stamps the item, and
        whatever observes the flow afterwards — an exchange, a regulator, this project's own
        dashboard — stamps it from the wall clock. When the ratchet reads one tick ahead, its
        window sits one tick later than the observer's, the item that fell out of ITS window is
        still inside the observer's, and the limiter admits one more order than the observer will
        count as permitted. Hypothesis found exactly that: 8 orders inside a 5-second window
        permitting 7, with the bucket internally consistent throughout.

        Widening by one tick makes the boundary item count in every view of it. It costs nothing —
        the window is 0.02% longer at five seconds — and it converts a rule that was true on the
        limiter's clock into one that is true on anybody's.
        """
        return self.window_milliseconds + CLOCK_TICK_MILLISECONDS


@dataclass(frozen=True, slots=True)
class RateLimitOutcome:
    """What happened to one request for permission to submit."""

    granted: bool
    waited_seconds: float
    blocking_window: RateLimitWindow | None
    reason: str
    kind: RateLimitOutcomeKind
    exchange: str


@dataclass(frozen=True, slots=True)
class WindowBudget:
    """What is left in one window right now — the shape a dashboard renders."""

    exchange: str
    window: RateLimitWindow
    orders_used: int
    orders_remaining: int
    observed_at: datetime

    @property
    def utilisation_fraction(self) -> float:
        """How much of the window is spent, on zero-to-one, for a gauge."""
        if self.window.permitted_orders <= 0:
            return 1.0
        return min(1.0, self.orders_used / self.window.permitted_orders)


@dataclass(frozen=True, slots=True)
class DispatchJitterMeasurement:
    """The measured variation in dispatch delay that the safety margin is computed from."""

    jitter_seconds: float
    sample_size: int
    provenance: str


class DispatchJitterEstimator:
    """The spread of observed dispatch delays, which is what can displace an order across a second.

    Non-parametric on purpose. A quantile would need a quantile *level*, and there is no measurement
    that produces one — it would be a typed constant wearing a statistician's coat. The spread over
    a bounded, rolling sample is a real observable: it is the worst displacement this host has
    actually shown over the retained horizon, and it shrinks again when the host calms down.
    """

    __slots__ = ("_calibration", "_lock", "_monotonic_nanoseconds", "_observed")

    def __init__(
        self,
        retained_samples: int,
        monotonic_nanoseconds: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if retained_samples <= 0:
            raise OrderRateLimitError(
                f"the jitter estimator must retain at least one sample, got {retained_samples}"
            )
        self._observed: deque[float] = deque(maxlen=retained_samples)
        self._calibration: deque[float] = deque(maxlen=retained_samples)
        self._monotonic_nanoseconds = monotonic_nanoseconds
        self._lock = threading.Lock()

    def observe_dispatch_latency(self, latency_seconds: float) -> None:
        """Record how long one real dispatch took, end to end."""
        if latency_seconds < 0:
            raise OrderRateLimitError(
                f"a dispatch cannot take negative time, got {latency_seconds} seconds: a negative "
                f"latency means the two ends were measured on clocks that disagree"
            )
        with self._lock:
            self._observed.append(latency_seconds)

    def calibrate_from_host_scheduling(self, sample_count: int) -> None:
        """Bootstrap from this host's own scheduling spread, before any dispatch has happened.

        A thread that yields and is immediately rescheduled measures the floor of every delay this
        process can impose on an outbound order. It is an underestimate of true network jitter, and
        it is deliberately kept only as a floor under the observed estimate until enough real
        dispatches have been seen to replace it outright.
        """
        samples: list[float] = []
        for _ in range(max(sample_count, 1)):
            before = self._monotonic_nanoseconds()
            time.sleep(0)
            after = self._monotonic_nanoseconds()
            samples.append((after - before) / 1_000_000_000)
        with self._lock:
            self._calibration.clear()
            self._calibration.extend(samples)

    @property
    def measurement(self) -> DispatchJitterMeasurement:
        """The current jitter estimate, and where it came from.

        A spread computed from a handful of samples is systematically an *under*estimate of the
        spread — the extremes have not been seen yet — and an underestimated jitter silently shrinks
        the safety margin, which is the one direction that must never happen by accident. So the
        calibration spread stays underneath the observed one as a floor until the retained sample is
        full of real dispatches, at which point the observations stand on their own.
        """
        with self._lock:
            observed = list(self._observed)
            calibration = list(self._calibration)
            retained = self._observed.maxlen or 0
        observed_spread = (max(observed) - min(observed)) if observed else 0.0
        calibration_spread = (max(calibration) - min(calibration)) if calibration else 0.0
        if len(observed) >= retained and observed:
            return DispatchJitterMeasurement(
                observed_spread, len(observed), "observed dispatch latencies"
            )
        if observed:
            return DispatchJitterMeasurement(
                max(observed_spread, calibration_spread),
                len(observed),
                "observed dispatch latencies, floored by the host scheduling calibration",
            )
        if calibration:
            return DispatchJitterMeasurement(
                calibration_spread,
                len(calibration),
                "host scheduling calibration (no dispatch observed yet)",
            )
        return DispatchJitterMeasurement(0.0, 0, "no samples")


class MonotonicallyRatchetedDispatchClock:
    """A millisecond clock that can be read backwards by nobody, including the operating system.

    Epoch-based so that timestamps written by one process are comparable to those read by the next,
    monotonic-driven so that a wall-clock step backwards cannot make spent budget look unspent, and
    floored on construction by whatever the store already holds so that the ratchet is not reset by
    a restart.
    """

    __slots__ = (
        "_anchor_monotonic_ns",
        "_anchor_wall_ms",
        "_last_ms",
        "_lock",
        "_monotonic",
        "_wall",
    )

    def __init__(
        self,
        wall_clock_epoch_seconds: Callable[[], float] = time.time,
        monotonic_nanoseconds: Callable[[], int] = time.monotonic_ns,
        floor_milliseconds: int = 0,
    ) -> None:
        self._wall = wall_clock_epoch_seconds
        self._monotonic = monotonic_nanoseconds
        self._anchor_wall_ms = int(self._wall() * MILLISECONDS_PER_SECOND)
        self._anchor_monotonic_ns = self._monotonic()
        self._last_ms = max(self._anchor_wall_ms, floor_milliseconds)
        self._lock = threading.Lock()

    def now_milliseconds(self) -> int:
        """The current instant, never earlier than the last one this clock reported."""
        with self._lock:
            wall_ms = int(self._wall() * MILLISECONDS_PER_SECOND)
            elapsed_ns = self._monotonic() - self._anchor_monotonic_ns
            projected_ms = self._anchor_wall_ms + elapsed_ns // NANOSECONDS_PER_MILLISECOND
            if wall_ms > projected_ms:
                # A forward step, or ordinary drift: re-anchor so the projection keeps tracking.
                self._anchor_wall_ms = wall_ms
                self._anchor_monotonic_ns = self._monotonic()
                projected_ms = wall_ms
            self._last_ms = max(wall_ms, projected_ms, self._last_ms)
            return self._last_ms


class PerExchangeRateBucketFactory(BucketFactory):
    """Routes each acquisition to the bucket that belongs to its exchange.

    The default factory pyrate-limiter installs puts every key in one bucket, which would merge
    NSE's and BSE's counts into a single ceiling and refuse orders on one exchange because of flow
    on another. The routing lives here rather than in several limiters because one bucket holding
    several rates is what makes the windows bind *simultaneously* — the bucket's own algorithm
    checks every rate on every put and reports the first that fails, which, since rates are sorted
    by ascending interval, is always the tightest one.
    """

    def __init__(
        self,
        buckets: dict[str, AbstractBucket],
        clock: MonotonicallyRatchetedDispatchClock,
    ) -> None:
        self._buckets = buckets
        self._clock = clock

    def wrap_item(self, name: str, weight: int = 1) -> RateItem:
        """Stamp the request with this limiter's ratcheted clock rather than the bucket's."""
        return RateItem(name, self._clock.now_milliseconds(), weight=weight)

    def get(self, item: RateItem) -> AbstractBucket:
        """The bucket for this exchange; unknown exchanges never reach here."""
        bucket = self._buckets.get(item.name)
        if bucket is None:
            raise OrderRateLimitError(
                f"no rate bucket exists for exchange {item.name!r}: the limiter refuses to admit "
                f"flow it is not counting"
            )
        return bucket


def _reserved_orders_for(published_limit: OrderRateLimit, jitter_seconds: float) -> int:
    """How many orders of a published ceiling are held back as the derived safety margin.

    Zero for anything that is not the regulatory threshold: a broker ceiling breached costs one
    order and an HTTP 429, which is recoverable, and spending budget to insure against a recoverable
    event is a worse trade than taking it.
    """
    if published_limit.scope is not RateLimitScope.REGULATORY_THRESHOLD:
        return 0
    displaced_by_jitter = math.ceil(
        published_limit.maximum_orders * jitter_seconds / published_limit.window_seconds
    )
    return min(max(displaced_by_jitter, 0), published_limit.maximum_orders - 1)


def _tightest_window_per_duration(
    published_limits: Sequence[OrderRateLimit], jitter_seconds: float
) -> list[RateLimitWindow]:
    """Collapse several facts about the same window into the one that binds."""
    tightest: dict[int, RateLimitWindow] = {}
    for published_limit in published_limits:
        if published_limit.maximum_orders <= 0 or published_limit.window_seconds <= 0:
            raise OrderRateLimitError(
                f"a rate limit fact must permit a positive number of orders over a positive "
                f"window, got {published_limit.maximum_orders} over "
                f"{published_limit.window_seconds}s from {published_limit.source}"
            )
        reserved = _reserved_orders_for(published_limit, jitter_seconds)
        window = RateLimitWindow(
            window_seconds=published_limit.window_seconds,
            permitted_orders=published_limit.maximum_orders - reserved,
            published_maximum_orders=published_limit.maximum_orders,
            reserved_for_safety_margin=reserved,
            scope=published_limit.scope,
            source=published_limit.source,
            aligned_to_calendar_window=published_limit.aligned_to_calendar_window,
        )
        incumbent = tightest.get(window.window_seconds)
        if incumbent is None or window.permitted_orders < incumbent.permitted_orders:
            tightest[window.window_seconds] = window
    return [tightest[duration] for duration in sorted(tightest)]


def binding_windows_from(
    published_limits: Sequence[OrderRateLimit], jitter_seconds: float
) -> tuple[RateLimitWindow, ...]:
    """Every window that can actually bind, in ascending window order.

    Two kinds of published fact are dropped, and both are dropped because they can never be the
    reason an order is refused rather than because they are inconvenient:

    * a shorter window whose ceiling is at least as large as a longer window's — the longer window
      caps the shorter one below its own limit, so the shorter one is unreachable;
    * a longer window whose *rate* is no lower than a shorter window's — the shorter window already
      forbids ever accumulating enough orders to reach it.

    Dropping them is also what makes the surviving list well-formed for pyrate-limiter, which
    requires strictly increasing intervals, strictly increasing limits, and non-increasing density
    and rejects anything else outright.
    """
    if not published_limits:
        raise OrderRateLimitError(
            "no rate limit facts were supplied: a limiter with no ceiling is not a limiter, and "
            "the facts are dated, so an era before the first recorded one is a refusal (R.03)"
        )
    binding: list[RateLimitWindow] = []
    for candidate in _tightest_window_per_duration(published_limits, jitter_seconds):
        while binding and candidate.permitted_orders <= binding[-1].permitted_orders:
            binding.pop()
        if binding:
            incumbent = binding[-1]
            candidate_is_no_tighter = (
                candidate.permitted_orders * incumbent.window_seconds
                >= incumbent.permitted_orders * candidate.window_seconds
            )
            if candidate_is_no_tighter:
                continue
        binding.append(candidate)
    if not binding:
        raise OrderRateLimitError("every supplied rate limit fact was dominated by another")
    return tuple(binding)


def queue_capacity_from(binding: Sequence[RateLimitWindow]) -> int:
    """How many intents may wait at once — derived from the windows, never chosen.

    The bound is the number of orders the limiter may dispatch inside the longest window that
    *cycles* during a session. The longest window of all is the day, and a day window does not cycle
    within a session — it is a session budget, not a drain rate — so the bound comes from the
    longest window below it. A queue deeper than that cannot be drained inside the window that
    governs it and is pure memory growth serving no order.

    Staleness is deliberately not what this bound is for. An intent that waits too long is expired
    by its own deadline, which is a property of that intent; the depth bound is back-pressure, which
    is a property of the system.
    """
    if not binding:
        raise OrderRateLimitError("cannot derive a queue capacity with no binding windows")
    if len(binding) == 1:
        return binding[0].permitted_orders
    return max(window.permitted_orders for window in binding[:-1])


class OrderSubmissionRateLimiter:
    """Permission to put one order on the wire, or a reason why not.

    Every acquisition passes every binding window at once; the tightest one is the one that makes
    the caller wait, and it is named in the outcome so an operator never has to guess which
    authority stopped the flow.
    """

    def __init__(
        self,
        session_date: date,
        *,
        published_limits: Sequence[OrderRateLimit] | None = None,
        store_path: Path = DEFAULT_RATE_LIMIT_STORE_PATH,
        wall_clock_epoch_seconds: Callable[[], float] = time.time,
        monotonic_nanoseconds: Callable[[], int] = time.monotonic_ns,
        prune_other_sessions: bool = True,
    ) -> None:
        """Build the buckets for one session from the facts in force on that session's date.

        `published_limits` exists as an injection seam for hermetic verification (`R.05`/Rule J):
        left alone it is `order_rate_limits(on=session_date)` and nothing else, so the production
        path can only ever be the dated, sourced fact table. The clock callables are the same kind
        of seam — a test that has to wait a real day to prove a day window binds is a test nobody
        runs.
        """
        self._session_date = session_date
        self._published_limits = tuple(
            published_limits if published_limits is not None else order_rate_limits(on=session_date)
        )
        self._store_path = store_path
        self._lock = threading.RLock()
        self._store_lock = threading.RLock()
        self._queue_depth: dict[str, int] = {}
        self._buckets: dict[str, AbstractBucket] = {}

        # The margin needs a jitter estimate, the estimate needs a retention length, and the
        # retention length is derived from the windows — so the windows are derived once with a
        # zero margin purely to size the estimator, then rebuilt with the measured margin. The zero
        # -margin pass can only ever widen the windows, so the sizing is never an underestimate.
        provisional = binding_windows_from(self._published_limits, jitter_seconds=0.0)
        self._queue_capacity = queue_capacity_from(provisional)
        self._jitter_estimator = DispatchJitterEstimator(
            retained_samples=self._queue_capacity,
            monotonic_nanoseconds=monotonic_nanoseconds,
        )
        self._jitter_estimator.calibrate_from_host_scheduling(self._queue_capacity)
        self._windows = binding_windows_from(
            self._published_limits, self._jitter_estimator.measurement.jitter_seconds
        )

        self._connection = self._open_store()
        if prune_other_sessions:
            self._drop_tables_of_other_sessions()
        self._clock = MonotonicallyRatchetedDispatchClock(
            wall_clock_epoch_seconds=wall_clock_epoch_seconds,
            monotonic_nanoseconds=monotonic_nanoseconds,
            floor_milliseconds=self._highest_recorded_timestamp_ms(),
        )
        self._bucket_factory = PerExchangeRateBucketFactory(self._buckets, self._clock)
        self._limiter = Limiter(self._bucket_factory)

    # --- the surface an operator and a dashboard read ------------------------------------------

    @property
    def session_date(self) -> date:
        """The session these counters belong to."""
        return self._session_date

    @property
    def binding_windows(self) -> tuple[RateLimitWindow, ...]:
        """Every window in force, ascending, after the derived margin has been applied."""
        with self._lock:
            return self._windows

    @property
    def queue_capacity(self) -> int:
        """How many intents may be waiting at once before the limiter applies back-pressure."""
        return self._queue_capacity

    @property
    def dispatch_jitter(self) -> DispatchJitterMeasurement:
        """The measurement the safety margin is currently derived from."""
        return self._jitter_estimator.measurement

    def queue_depth(self, exchange: str) -> int:
        """How many intents are waiting on this exchange right now."""
        with self._lock:
            return self._queue_depth.get(_validated_exchange(exchange), 0)

    def budget_remaining(self, exchange: str) -> tuple[WindowBudget, ...]:
        """What is left in each window for this exchange, ascending by window."""
        name = _validated_exchange(exchange)
        self._bucket_for(name)
        now_ms = self._clock.now_milliseconds()
        observed_at = datetime.fromtimestamp(now_ms / MILLISECONDS_PER_SECOND, tz=UTC)
        budgets: list[WindowBudget] = []
        for window in self.binding_windows:
            used = self._orders_within(name, window.enforced_milliseconds, now_ms)
            budgets.append(
                WindowBudget(
                    exchange=name,
                    window=window,
                    orders_used=used,
                    orders_remaining=max(0, window.permitted_orders - used),
                    observed_at=observed_at,
                )
            )
        return tuple(budgets)

    def record_dispatch_latency(self, latency_seconds: float) -> None:
        """Feed back how long a real dispatch took, so the safety margin keeps calibrating itself.

        Re-deriving the margin here rather than at construction is the whole point of measuring it:
        a host that starts calm and later contends should give the regulatory window more room, not
        keep the margin it was born with.
        """
        self._jitter_estimator.observe_dispatch_latency(latency_seconds)
        self._reapply_safety_margin()

    # --- the surface the order path calls ------------------------------------------------------

    def acquire(self, exchange: str, deadline: datetime) -> RateLimitOutcome:
        """Wait for permission to submit one order on this exchange, or expire trying.

        `deadline` is the intent's own validity, not a patience setting. Reaching it while queued
        expires the intent with that reason instead of sending it late.
        """
        name = _validated_exchange(exchange)
        deadline_ms = _deadline_milliseconds(deadline)
        self._bucket_for(name)

        if deadline_ms <= self._clock.now_milliseconds():
            return RateLimitOutcome(
                granted=False,
                waited_seconds=0.0,
                blocking_window=None,
                reason=(
                    f"the intent's validity ({deadline.isoformat()}) had already passed when it "
                    f"reached the limiter, so no budget was spent on it"
                ),
                kind=RateLimitOutcomeKind.EXPIRED_BEFORE_QUEUEING,
                exchange=name,
            )

        if bool(self._limiter.try_acquire(name, blocking=False)):
            return RateLimitOutcome(
                granted=True,
                waited_seconds=0.0,
                blocking_window=None,
                reason="every binding window had room",
                kind=RateLimitOutcomeKind.GRANTED_IMMEDIATELY,
                exchange=name,
            )

        blocking_window = self._window_that_refused(name)
        with self._lock:
            waiting = self._queue_depth.get(name, 0)
            if waiting >= self._queue_capacity:
                return RateLimitOutcome(
                    granted=False,
                    waited_seconds=0.0,
                    blocking_window=blocking_window,
                    reason=(
                        f"{waiting} intents are already queued for {name}, which is the whole "
                        f"queue: a deeper queue could not be drained inside the window that "
                        f"governs it, so the load is refused here instead of aging silently"
                    ),
                    kind=RateLimitOutcomeKind.REFUSED_QUEUE_FULL,
                    exchange=name,
                )
            self._queue_depth[name] = waiting + 1

        started_at = time.perf_counter()
        try:
            remaining_seconds = max(
                0.0,
                (deadline_ms - self._clock.now_milliseconds()) / MILLISECONDS_PER_SECOND,
            )
            granted = bool(
                self._limiter.try_acquire(name, blocking=True, timeout=remaining_seconds)
            )
        finally:
            with self._lock:
                self._queue_depth[name] = max(0, self._queue_depth.get(name, 1) - 1)
        waited_seconds = time.perf_counter() - started_at

        if granted:
            return RateLimitOutcome(
                granted=True,
                waited_seconds=waited_seconds,
                blocking_window=blocking_window,
                reason=(
                    f"queued behind the {_window_description(blocking_window)} ceiling and "
                    f"released when it had room"
                ),
                kind=RateLimitOutcomeKind.GRANTED_AFTER_WAIT,
                exchange=name,
            )
        return RateLimitOutcome(
            granted=False,
            waited_seconds=waited_seconds,
            blocking_window=blocking_window,
            reason=(
                f"the intent's validity ({deadline.isoformat()}) expired while it was queued "
                f"behind the {_window_description(blocking_window)} ceiling; a stale order is "
                f"worse than no order, so it was not sent late"
            ),
            kind=RateLimitOutcomeKind.EXPIRED_WHILE_QUEUED,
            exchange=name,
        )

    def close(self) -> None:
        """Release the store. The counts stay on disk; that is the point of them."""
        with self._store_lock:
            self._connection.close()

    def __enter__(self) -> OrderSubmissionRateLimiter:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # --- the store -----------------------------------------------------------------------------

    def _open_store(self) -> sqlite3.Connection:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self._store_path), check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _table_for(self, exchange: str) -> str:
        return f"{_TABLE_NAME_PREFIX}_{exchange}_{self._session_date:%Y%m%d}"

    def _session_table_names(self) -> list[str]:
        with self._store_lock:
            cursor = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                (f"{_TABLE_NAME_PREFIX}_%",),
            )
            names = [str(row[0]) for row in cursor.fetchall()]
            cursor.close()
        return names

    def _drop_tables_of_other_sessions(self) -> None:
        """Yesterday's counters are not this session's, and keeping them grows the file forever."""
        suffix = f"_{self._session_date:%Y%m%d}"
        with self._store_lock:
            for name in self._session_table_names():
                if not name.endswith(suffix):
                    self._connection.execute(f'DROP TABLE IF EXISTS "{name}"')
            self._connection.commit()

    def _highest_recorded_timestamp_ms(self) -> int:
        """The furthest-forward instant the store has seen, which the clock is floored to."""
        highest = 0
        with self._store_lock:
            for name in self._session_table_names():
                cursor = self._connection.execute(
                    # `name` came from sqlite_master itself, so it is already a real
                    # table name in this file rather than anything a caller supplied.
                    f'SELECT MAX(item_timestamp) FROM "{name}"'  # noqa: S608
                )
                row = cursor.fetchone()
                cursor.close()
                if row is not None and row[0] is not None:
                    highest = max(highest, int(row[0]))
        return highest

    def _bucket_for(self, exchange: str) -> AbstractBucket:
        with self._lock:
            existing = self._buckets.get(exchange)
            if existing is not None:
                return existing
            table = self._table_for(exchange)
            with self._store_lock:
                self._connection.execute(
                    f'CREATE TABLE IF NOT EXISTS "{table}" '
                    f"(name VARCHAR, item_timestamp INTEGER)"
                )
                self._connection.execute(
                    f'CREATE INDEX IF NOT EXISTS "{table}_by_timestamp" '
                    f'ON "{table}" (item_timestamp)'
                )
                self._connection.commit()
            bucket = SQLiteBucket(
                _rates_from(self._windows), self._connection, table, lock=self._store_lock
            )
            self._buckets[exchange] = bucket
            return bucket

    def _orders_within(self, exchange: str, window_milliseconds: int, now_ms: int) -> int:
        table = self._table_for(exchange)
        with self._store_lock:
            cursor = self._connection.execute(
                # The predicate is pyrate-limiter's own, verbatim: an item counts while its
                # timestamp is at or after `now - interval`. A different inequality here would make
                # the dashboard disagree with the limiter about the same instant.
                f'SELECT COUNT(*) FROM "{table}" WHERE item_timestamp >= ?',  # noqa: S608
                (now_ms - window_milliseconds,),
            )
            row = cursor.fetchone()
            cursor.close()
        return int(row[0]) if row is not None else 0

    # --- the margin ----------------------------------------------------------------------------

    def _reapply_safety_margin(self) -> None:
        rebuilt = binding_windows_from(
            self._published_limits, self._jitter_estimator.measurement.jitter_seconds
        )
        with self._lock:
            if rebuilt == self._windows:
                return
            self._windows = rebuilt
            rates = _rates_from(rebuilt)
            for bucket in self._buckets.values():
                bucket.rates = rates

    def _window_that_refused(self, exchange: str) -> RateLimitWindow | None:
        """Which window the bucket blamed for the refusal it just reported."""
        failing = self._bucket_for(exchange).failing_rate
        if failing is None:
            return None
        # Integer division absorbs the one-tick widening: 5,001 ms is still the 5-second window.
        window_seconds = failing.interval // MILLISECONDS_PER_SECOND
        for window in self.binding_windows:
            if window.window_seconds == window_seconds:
                return window
        return None


def _rates_from(windows: Sequence[RateLimitWindow]) -> list[Rate]:
    # `enforced_milliseconds`, not `window_milliseconds`: the bucket must count the boundary item
    # that a differently-anchored clock would count (`A.113`).
    return [Rate(window.permitted_orders, window.enforced_milliseconds) for window in windows]


def _window_description(window: RateLimitWindow | None) -> str:
    if window is None:
        return "unattributed"
    return (
        f"{window.permitted_orders}-orders-per-{window.window_seconds}s {window.scope.value}"
    )


def _validated_exchange(exchange: str) -> str:
    name = exchange.strip().upper()
    if not _EXCHANGE_NAME_PATTERN.match(name):
        raise OrderRateLimitError(
            f"{exchange!r} is not a usable exchange mnemonic: the threshold is measured per "
            f"exchange, so the name partitions the counters and has to be a plain uppercase "
            f"mnemonic such as NSE, BSE or MCX"
        )
    return name


def _deadline_milliseconds(deadline: datetime) -> int:
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise OrderRateLimitError(
            "the deadline must carry a timezone: a naive deadline is read differently by the host "
            "and by the exchange, and the difference is the size of a session"
        )
    return int(deadline.timestamp() * MILLISECONDS_PER_SECOND)
