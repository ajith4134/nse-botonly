"""`L9.14` — WHICH member of the order taxonomy expresses this intent, decided by optimisation.

`L9.02` models the taxonomy and `broker_order_facility_facts` says what is permitted on it. Neither
chooses. This does, and it chooses by **maximising expected net edge in basis points**, not by
consulting a preference table:

    expected_net_edge = claimed_edge_after_delay
                      - expected_execution_cost      (the book, walked by L1.05/L1.06)
                      - expected_statutory_cost      (the circulars, priced by L1.01)
                      - P(not filled) x edge_forgone (the queue, measured off the depth tape)

Every term is read from something that was measured. Nothing in the objective is a coefficient
somebody liked the look of, and the four candidate families do not carry rankings — they carry
prices, and the arithmetic ranks them.

**MARKET is not a candidate, and the refusal is data rather than an `if`.** NSE/MSD/67753
(2025-04-29) and the NSE retail-algo FAQ: *"Algo orders with order type as Market Order are not
permitted."* Every order this system places is algo-originated by construction, so the selector
asks `availability_of_order_type(OrderType.MARKET, OrderOrigin.ALGO_API, on=...)` and records the
refusal **with the circular attached** in its own output. The substitute is a marketable limit
priced through the touch by a tolerance DERIVED from the book walk — the terminal price the walk
actually reaches, or, where the walk is censored, the price the impact estimate implies. A typed
"cross by 0.1%" would be the exact defect `R.03` exists to catch, and it would also be wrong in
both directions at once: too tight on a wide book, too loose on a tight one.

**The pessimistic end decides, exactly as in `L1.02`.** `ExpectedFill` reports an interval whose
width IS the measurement's honesty: it widens when the clip exceeds the visible ladder, when the
instrument is thinly measured, when no fills have accrued. Scoring on `upper_cost_bps` gives the
safety margin for free and gives it the right shape — and it is what makes an iceberg beat a single
sweep on a thin book without anybody encoding a preference for icebergs. A clip inside the visible
book is priced EXACTLY; a clip beyond it is an extrapolation with a blown-up upper bound. Splitting
the clip into legs that each stay inside the visible book buys a tight price at the cost of paying
per-order brokerage `legs` times, and the arithmetic decides which of those two is worse today.

**A resting order's fill probability is measured or the candidate is dropped.** The one thing this
engine must never do is invent a number for "will my passive order fill". That number decides
whether the cheapest expression is also the one that never trades, and a plausible default would be
undetectable in every backtest. So a resting candidate — a passive limit at the touch, the residual
of an aggressive DAY order, the second through Nth legs of an iceberg — is scored only when a
`TouchQueueExecutionObserver` supplies a measured execution rate for that side's touch queue.
Without one, the passive candidate is EXCLUDED and the exclusion is carried in the output where an
operator can see it. `DepthTapeTouchExecutionObserver` provides the measurement from `L0.22`'s
queue-depletion decomposition, which bounds how much of a queue's shrinkage was execution rather
than cancellation — the two are indistinguishable in a five-level feed and only the first one fills
anybody's order.

**Urgency is derived from the intent's own horizon, not from a lookup table.** `horizon_minutes` is
the strategy's own statement of how long its claimed edge lasts, so it is the natural time constant
of that edge's decay, and it is also the window inside which a resting order has to fill to be
worth anything. Both fall out of one exponential model with no free parameters:

    mean wait      m = (queue ahead + our clip) / executed quantity per minute
    P(fill by H)   = 1 - exp(-H / m)
    E[wait | fill] = m - H exp(-H/m) / (1 - exp(-H/m))
    edge surviving = claimed edge x exp(-E[wait | fill] / H)

A five-minute horizon and a ninety-minute horizon therefore price the same passive order completely
differently, and nothing had to be tabulated for that to happen.

**A negative-edge intent gets the expression LEAST likely to trade, and that is not a bug.**
`expected_net_edge = P(fill) x (edge - costs)` is an expectation, so a negative bracket is
maximised by making the fill unlikely. It reads oddly and it is right: an order that never
trades costs nothing, and not trading a loser genuinely beats trading it. Whether the trade is worth
taking at all is `L1.02`'s question and it is answered before this engine is reached; this engine is
told to express a decision, and it expresses a bad one as reluctantly as the arithmetic allows.

**Refusal beats a guess.** A crossed book (bid >= ask — 955 of them were measured in one real
session by `L0.33`), a one-sided book, or a side with no populated level cannot price anything, so
the selector raises `BookUnpriceableError` rather than returning the cheapest-looking candidate. A
crossed book yields a negative spread, which enters a cost model as a rebate and reads as free
money.

**What this engine deliberately does not decide.** Whether to trade at all (`L1.02`'s gate), how
much (its RESIZE solve), and whether the risk shape of a cover order suits the strategy. The
objective here is expected net edge in bps of the intended notional; leverage and forced-stop
effects live outside it, which is why a `CO` ties with the aggressive DAY limit it is built on and
loses on the tie-break rather than being suppressed by a rule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from nse_algo_trader.execution_fill.execution_fill_model import (
    ExecutionFillError,
    ExecutionFillModel,
    ExpectedFill,
)
from nse_algo_trader.execution_fill.order_book_walk_calculator import (
    OrderBookWalk,
    OrderBookWalkError,
    walk_order_book,
)
from nse_algo_trader.execution_fill.quoted_spread_observer import (
    QuotedSpreadError,
    QuotedSpreadObservation,
    observe_quoted_spread,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    MicrostructureFeatureRow,
)
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderFacilityError,
    OrderOrigin,
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
    availability_of_order_type,
    availability_of_variety,
    order_types_available_to,
    varieties_available_to,
)
from nse_algo_trader.order_path.order_record import OrderExpression
from nse_algo_trader.order_path.trading_intent import (
    INDIA_MARKET_TIMEZONE,
    OrderPathError,
    TradingIntent,
)
from nse_algo_trader.regime.session_phase_regime_classifier import SessionPhaseRegimeClassifier
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    TradeSpecification,
    TransactionCostError,
)

_BASIS_POINTS = Decimal(10_000)
_ONE = Decimal(1)
_ZERO = Decimal(0)
_SECONDS_PER_MINUTE = Decimal(60)

# Broker facility facts, sourced and dated — the one category of constant `R.03` admits, and only
# with its citation attached (`docs/research/223` preamble).
ICEBERG_MINIMUM_LEGS = 2
ICEBERG_MAXIMUM_LEGS = 50
ICEBERG_LEG_SOURCE = (
    "kite.trade/docs/connect/v3/orders — an iceberg discloses one leg at a time and takes 2 to 50 "
    "legs; recorded as a facility fact in broker_order_facility_facts.OrderVariety.ICEBERG"
)

# Which product carries a position in each segment. A property of the segment rather than a
# preference: equity delivery is CNC, equity intraday is MIS, and every derivative segment carries
# on NRML. MTF is deliberately absent — it is a financing decision about the same expression, not a
# different expression, and it belongs to whatever decides funding.
_PRODUCT_BY_SEGMENT: dict[ChargeableSegment, OrderProduct] = {
    ChargeableSegment.EQUITY_DELIVERY: OrderProduct.DELIVERY,
    ChargeableSegment.EQUITY_INTRADAY: OrderProduct.INTRADAY,
    ChargeableSegment.EQUITY_FUTURES: OrderProduct.NORMAL,
    ChargeableSegment.EQUITY_OPTIONS: OrderProduct.NORMAL,
    ChargeableSegment.CURRENCY_FUTURES: OrderProduct.NORMAL,
    ChargeableSegment.CURRENCY_OPTIONS: OrderProduct.NORMAL,
    ChargeableSegment.COMMODITY_FUTURES: OrderProduct.NORMAL,
    ChargeableSegment.COMMODITY_OPTIONS: OrderProduct.NORMAL,
}

# The phases of `L11.06`'s session classifier in which the continuous market is actually matching
# orders. Read from that module rather than re-typed here, so a change to NSE's window moves both.
_CONTINUOUS_TRADING_PHASES = frozenset({"opening", "midday", "closing"})


class OrderExpressionSelectionError(OrderPathError):
    """The selector cannot answer, and answering anyway would put a guess on the wire."""


class BookUnpriceableError(OrderExpressionSelectionError):
    """The book cannot price anything — crossed, one-sided, or empty on the side being taken.

    Refused rather than worked around. Every fallback available here (use the last good book,
    assume a typical spread, take the mid of a crossed quote) produces a number that looks
    ordinary and is not.
    """


class NoFeasibleExpressionError(OrderExpressionSelectionError):
    """Every member of the taxonomy was refused, and the refusals say by whom."""


class ExpressionFamily(StrEnum):
    """The candidate shapes, named for what they DO rather than for the broker's field names.

    The order of declaration is the last tie-break and nothing else: it is consulted only when two
    candidates score identically to the paisa AND rest on the same number of unmeasured things.
    """

    AGGRESSIVE_LIMIT_IMMEDIATE = "aggressive_limit_immediate"
    AGGRESSIVE_LIMIT_DAY = "aggressive_limit_day"
    ICEBERG_AGGRESSIVE = "iceberg_aggressive"
    PASSIVE_LIMIT_AT_TOUCH = "passive_limit_at_touch"
    COVER_ORDER = "cover_order"
    AFTER_MARKET = "after_market"
    MARKET_TAKE = "market_take"
    """Enumerated so that its refusal is a recorded, sourced fact rather than an absence.

    A taxonomy member that is silently never generated is indistinguishable from one nobody
    thought of. This one is generated, checked against the facts module, and excluded with the
    circular that bars it — every session, in the output an operator reads.
    """


class SelectionBasis(StrEnum):
    """How the choice was reached, because the two are not equally strong claims."""

    OPTIMISED = "optimised"
    """Two or more candidates were priced and the arithmetic separated them."""

    SOLE_FEASIBLE = "sole_feasible"
    """Exactly one candidate survived the facility and session filters, so nothing was compared.

    The honest reading of a closed session: an after-market order is not the best expression, it is
    the only one, and its execution cost against tomorrow's open is not knowable from tonight's
    book.
    """


@dataclass(frozen=True, slots=True)
class TouchExecutionRate:
    """How fast the queue at one side's touch is drained BY EXECUTION, measured.

    Cancellation drains a queue without filling anybody's order, and a five-level feed cannot tell
    the two apart at the packet level — which is why `L0.22` reports the split as an interval and
    why this carries `is_upper_bound`. Using the interval's upper end makes every passive fill
    probability an upper bound too, and that direction is recorded on the choice rather than
    quietly assumed.
    """

    quantity_per_minute: Decimal
    observed_minutes: Decimal
    transition_count: int
    executed_quantity: int
    is_upper_bound: bool
    evidence: str

    def __post_init__(self) -> None:
        if self.quantity_per_minute < 0:
            raise OrderExpressionSelectionError(
                f"a touch queue cannot be drained at {self.quantity_per_minute} units a minute"
            )
        if self.observed_minutes <= 0:
            raise OrderExpressionSelectionError(
                "an execution rate measured over a zero-length window is not a measurement"
            )


class TouchQueueExecutionObserver(Protocol):
    """The seam through which a MEASURED resting-fill rate enters, or does not.

    Returning `None` is a first-class answer and the whole reason this is a protocol rather than a
    parameter: an instrument with no usable depth history has no fill rate, and the selector's
    correct response is to drop every resting candidate and say so — not to substitute a plausible
    figure that no test would ever catch.
    """

    def observe_touch_execution_rate(
        self, *, instrument_token: int, resting_side: TradeLeg, as_of: datetime
    ) -> TouchExecutionRate | None:
        """Executed quantity per minute out of the touch queue an order on `resting_side` joins."""
        ...


class MicrostructureRowSource(Protocol):
    """What `DepthTapeTouchExecutionObserver` needs from `L0.22` — one method, not the engine."""

    def replay_instrument(self, instrument_token: int) -> Iterator[MicrostructureFeatureRow]: ...


class DepthTapeTouchExecutionObserver:
    """A measured execution rate, from `L0.22`'s queue-depletion decomposition of the real tape.

    **Causal by construction.** Only transitions at or before `as_of` are counted. A rate computed
    from the whole session would let the afternoon decide the morning's order, which is precisely
    the leak `causal_leakage_firewall` exists to prevent, and it would flatter every passive
    candidate in a backtest.

    The rate is `sum(maximum_executed) / elapsed minutes` over those transitions. `maximum_executed`
    is the upper end of `L0.22`'s interval — the most of the queue's shrinkage that the inferred
    aggressive volume on the other side can account for — so the rate, and every fill probability
    derived from it, is an upper bound. The lower end is structurally zero (a queue can always have
    been cancelled rather than hit), so an interval-honest engine that used it would refuse every
    passive order forever, which is not a measurement either. The bound is therefore carried
    explicitly to the choice instead of being hidden in a point estimate.
    """

    def __init__(self, rows: MicrostructureRowSource) -> None:
        self._rows = rows
        self._cache: dict[int, tuple[MicrostructureFeatureRow, ...]] = {}

    def _rows_for(self, instrument_token: int) -> tuple[MicrostructureFeatureRow, ...]:
        cached = self._cache.get(instrument_token)
        if cached is None:
            cached = tuple(self._rows.replay_instrument(instrument_token))
            self._cache[instrument_token] = cached
        return cached

    def observe_touch_execution_rate(
        self, *, instrument_token: int, resting_side: TradeLeg, as_of: datetime
    ) -> TouchExecutionRate | None:
        rows = [row for row in self._rows_for(instrument_token) if row.receipt_time <= as_of]
        if len(rows) < ICEBERG_MINIMUM_LEGS:  # two rows bound one interval of elapsed time
            return None
        elapsed_seconds = Decimal((rows[-1].receipt_time - rows[0].receipt_time).total_seconds())
        if elapsed_seconds <= 0:
            return None
        executed = 0
        counted = 0
        for row in rows:
            depletion = (
                row.bid_queue_depletion if resting_side is TradeLeg.BUY else row.ask_queue_depletion
            )
            if depletion is None:
                # The touch price moved: that queue was replaced rather than drained, and a
                # decomposition of a queue that no longer exists is meaningless.
                continue
            executed += depletion.maximum_executed
            counted += 1
        if counted == 0:
            return None
        minutes = elapsed_seconds / _SECONDS_PER_MINUTE
        return TouchExecutionRate(
            quantity_per_minute=Decimal(executed) / minutes,
            observed_minutes=minutes,
            transition_count=counted,
            executed_quantity=executed,
            is_upper_bound=True,
            evidence=(
                f"L0.22 queue depletion on the {resting_side.value} touch of instrument "
                f"{instrument_token}: {executed} units over {counted} same-price transitions in "
                f"{minutes:.1f} minutes ending {as_of.isoformat()}; upper bound, because the "
                f"execution/cancellation split is an interval and this takes its top"
            ),
        )


class TradingSessionPhaseSource(Protocol):
    """Whether the continuous market is matching orders at an instant."""

    def is_continuous_session_open(self, at: datetime) -> bool: ...


class NseContinuousSessionPhase:
    """The exchange's own window, composed from `L11.06` and the NSE session calendar.

    Nothing about the window is re-typed here. `SessionPhaseRegimeClassifier.phase_of` owns the
    clock boundaries (and the fact that NSE has moved them historically), the calendar owns which
    days are sessions at all, and this joins the two. A weekend, a holiday, 08:00 and 16:00 are all
    the same answer for the selector's purposes: nothing but an after-market order can be placed.
    """

    def __init__(self, is_trading_session: Callable[[date], bool] | None = None) -> None:
        self._is_trading_session = is_trading_session

    def _session_predicate(self) -> Callable[[date], bool]:
        """The calendar is built lazily: it costs a `pandas_market_calendars` import."""
        if self._is_trading_session is None:
            from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

            self._is_trading_session = NseTradingSessionCalendar().is_trading_session
        return self._is_trading_session

    def is_continuous_session_open(self, at: datetime) -> bool:
        moment = at.astimezone(INDIA_MARKET_TIMEZONE)
        if not self._session_predicate()(moment.date()):
            return False
        return SessionPhaseRegimeClassifier.phase_of(moment) in _CONTINUOUS_TRADING_PHASES


@dataclass(frozen=True, slots=True)
class RestingFillForecast:
    """What a resting clip is expected to achieve inside the intent's own horizon.

    Derived from one exponential waiting-time model with no free parameter: the volume that must
    trade through before the clip is done is `queue ahead + clip`, it arrives at the measured rate,
    and the horizon is the intent's own statement of how long its edge lasts.
    """

    fill_probability: Decimal
    mean_wait_minutes: Decimal
    expected_wait_given_fill_minutes: Decimal
    edge_survival_fraction: Decimal
    queue_ahead_quantity: int
    rate: TouchExecutionRate


def forecast_resting_fill(
    *,
    queue_ahead_quantity: int,
    clip_quantity: int,
    horizon_minutes: int,
    rate: TouchExecutionRate,
) -> RestingFillForecast:
    """Fill probability, expected delay and the edge that survives it — one model, no constants.

    Executions out of a touch queue are treated as a memoryless arrival process, so the time to
    clear `queue ahead + clip` units at `rate` units a minute is exponential with mean
    `m = (queue ahead + clip) / rate`. Then:

        P(fill within H) = 1 - exp(-H/m)
        E[wait | fill]   = m - H exp(-H/m) / (1 - exp(-H/m))

    and the edge decays with the intent's own horizon as its time constant, because a strategy
    stating a five-minute horizon has said that its edge is worth `1/e` of itself five minutes
    later. Nothing here is fitted, and nothing is looked up.
    """
    if clip_quantity <= 0:
        raise OrderExpressionSelectionError(f"clip quantity must be positive, got {clip_quantity}")
    if horizon_minutes <= 0:
        raise OrderExpressionSelectionError(f"a horizon of {horizon_minutes} minutes is not one")
    if queue_ahead_quantity < 0:
        raise OrderExpressionSelectionError(
            f"a queue cannot hold {queue_ahead_quantity} units ahead"
        )
    horizon = Decimal(horizon_minutes)
    volume_needed = Decimal(queue_ahead_quantity + clip_quantity)
    if rate.quantity_per_minute <= 0:
        # Measured, and the measurement says nothing executes here. That is a real answer: the
        # clip never fills, so it forgoes the whole edge.
        return RestingFillForecast(
            fill_probability=_ZERO,
            mean_wait_minutes=Decimal("Infinity"),
            expected_wait_given_fill_minutes=horizon,
            edge_survival_fraction=_ZERO,
            queue_ahead_quantity=queue_ahead_quantity,
            rate=rate,
        )
    mean_wait = volume_needed / rate.quantity_per_minute
    ratio = horizon / mean_wait
    survival_at_horizon = (-ratio).exp()
    fill_probability = _ONE - survival_at_horizon
    if fill_probability <= 0:
        expected_wait = horizon
    else:
        expected_wait = mean_wait - horizon * survival_at_horizon / fill_probability
    edge_survival = (-(expected_wait / horizon)).exp()
    return RestingFillForecast(
        fill_probability=fill_probability,
        mean_wait_minutes=mean_wait,
        expected_wait_given_fill_minutes=expected_wait,
        edge_survival_fraction=edge_survival,
        queue_ahead_quantity=queue_ahead_quantity,
        rate=rate,
    )


@dataclass(frozen=True, slots=True)
class ScoredExpression:
    """One candidate, priced. Every term names where it came from.

    The score is `expected_net_edge_bps` and the arg-max over it is the choice. The decomposition
    is kept because a number nobody can take apart is a number nobody can argue with, and this one
    decides which order goes on the wire.
    """

    family: ExpressionFamily
    expression: OrderExpression
    claimed_edge_bps: Decimal
    edge_after_delay_bps: Decimal
    execution_cost_bps: Decimal
    execution_cost_point_bps: Decimal
    execution_cost_interval_width_bps: Decimal
    statutory_cost_bps: Decimal
    fill_probability: Decimal
    edge_forgone_bps: Decimal
    expected_net_edge_bps: Decimal
    expected_filled_quantity: int
    order_count: int
    is_fill_probability_an_upper_bound: bool
    caveats: tuple[str, ...]
    evidence: str

    @property
    def non_fill_probability(self) -> Decimal:
        return _ONE - self.fill_probability

    @property
    def gross_net_edge_bps(self) -> Decimal:
        """What this expression earns IF it fills — the quantity `edge_forgone_bps` mirrors."""
        return self.edge_after_delay_bps - self.execution_cost_bps - self.statutory_cost_bps

    def describe(self) -> str:
        return (
            f"{self.family.value}: net {self.expected_net_edge_bps:.2f} bps = edge "
            f"{self.edge_after_delay_bps:.2f} - execution {self.execution_cost_bps:.2f} - "
            f"statutory {self.statutory_cost_bps:.2f} - non-fill "
            f"{self.non_fill_probability:.3f} x {self.edge_forgone_bps:.2f}"
        )


@dataclass(frozen=True, slots=True)
class ExcludedExpression:
    """A member of the taxonomy that was considered and refused, with who refused it.

    Present in the output on every call. An exclusion an operator cannot see is a decision the
    system made on its own authority, and the passive-fill case is the one where that would be
    invisible AND expensive.
    """

    family: ExpressionFamily
    reason: str
    source: str = ""

    def describe(self) -> str:
        return f"{self.family.value}: {self.reason}" + (f" [{self.source}]" if self.source else "")


@dataclass(frozen=True, slots=True)
class ExpressionChoice:
    """The chosen expression, everything it beat, and everything that never got to compete."""

    chosen: OrderExpression
    chosen_family: ExpressionFamily
    basis: SelectionBasis
    scored: tuple[ScoredExpression, ...]
    excluded: tuple[ExcludedExpression, ...]
    runner_up_family: ExpressionFamily | None
    advantage_bps: Decimal | None
    spread: QuotedSpreadObservation

    @property
    def winner(self) -> ScoredExpression | None:
        for candidate in self.scored:
            if candidate.family is self.chosen_family:
                return candidate
        return None

    @property
    def excluded_families(self) -> frozenset[ExpressionFamily]:
        return frozenset(entry.family for entry in self.excluded)

    def describe(self) -> str:
        lines = [
            f"chose {self.chosen_family.value} ({self.basis.value})",
            self.chosen.chosen_because,
        ]
        lines.extend(f"  scored  {candidate.describe()}" for candidate in self.scored)
        lines.extend(f"  refused {entry.describe()}" for entry in self.excluded)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ExpressionSelectionRequest:
    """Everything the choice depends on that is not the intent or the book.

    Optional members are optional in the strict sense that their absence removes candidates rather
    than being papered over: no stop price removes the cover order, no tick size falls back to the
    tape's own unit of whole paise, no strike is a refusal on an option segment because the SEBI
    turnover fee is charged on notional and defaulting it to the premium understates it ~100x.
    """

    intent: TradingIntent
    snapshot: BookSnapshot
    option_strike_paise: Decimal | None = None
    protective_stop_price_paise: Decimal | None = None
    tick_size_paise: Decimal | None = None
    known_as_of: date | None = None


class OrderExpressionSelector:
    """Prices every permitted expression of an intent against the real book and picks the best.

    Holds no thresholds and no preferences. It holds two engines and two optional observers, and
    everything it compares is computed at the moment of the decision from what those return — which
    is what lets a choice from last Tuesday be reproduced exactly by replaying that Tuesday's book,
    that Tuesday's rates and that Tuesday's facility facts.
    """

    def __init__(
        self,
        cost_engine: NseTransactionCostEngine,
        fill_model: ExecutionFillModel,
        *,
        touch_queue_observer: TouchQueueExecutionObserver | None = None,
        session_phase: TradingSessionPhaseSource | None = None,
    ) -> None:
        self._cost_engine = cost_engine
        self._fill_model = fill_model
        self._touch_queue_observer = touch_queue_observer
        self._session_phase = session_phase or NseContinuousSessionPhase()

    # ------------------------------------------------------------------ the decision

    def select(self, request: ExpressionSelectionRequest) -> ExpressionChoice:
        """Choose the expression, or refuse.

        Raises:
            BookUnpriceableError: the book is crossed, one-sided, or empty on the side being
                taken. No candidate can be priced against it, and the cheapest-looking one would
                be the one whose arithmetic broke first.
            NoFeasibleExpressionError: every member of the taxonomy was refused. The exclusions
                are on the exception's message, so the refusal says by whose authority.
            OrderExpressionSelectionError: the request contradicts itself — a non-positive
                quantity, an option without a strike, an unmapped segment.
        """
        intent = request.intent
        snapshot = request.snapshot
        self._require_coherent(request)
        spread = self._observe_or_refuse(snapshot)
        session_date = intent.session_date
        excluded: list[ExcludedExpression] = []
        scored: list[ScoredExpression] = []

        permitted_varieties = varieties_available_to(OrderOrigin.ALGO_API, on=session_date)
        permitted_types = order_types_available_to(OrderOrigin.ALGO_API, on=session_date)
        excluded.extend(self._market_order_refusal(session_date))
        session_open = self._session_phase.is_continuous_session_open(intent.decided_at)

        if OrderType.LIMIT not in permitted_types:
            fact = availability_of_order_type(
                OrderType.LIMIT, OrderOrigin.ALGO_API, on=session_date
            )
            raise NoFeasibleExpressionError(
                f"the limit order is not available to algo flow on {session_date}: {fact.reason} "
                f"[{fact.source}]. Every expression this engine can build is a limit order, so "
                f"there is nothing left to choose between"
            )

        for builder in (
            self._score_aggressive_immediate,
            self._score_aggressive_day,
            self._score_iceberg,
            self._score_passive,
            self._score_cover,
            self._score_after_market,
        ):
            outcome = builder(request, spread, session_open, permitted_varieties)
            if isinstance(outcome, ExcludedExpression):
                excluded.append(outcome)
            else:
                scored.append(outcome)

        if not scored:
            raise NoFeasibleExpressionError(
                "no expression survived the facility, session and measurement filters: "
                + "; ".join(entry.describe() for entry in excluded)
            )

        ranked = sorted(scored, key=_ranking_key, reverse=True)
        winner = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        basis = SelectionBasis.OPTIMISED if runner_up is not None else SelectionBasis.SOLE_FEASIBLE
        advantage = (
            winner.expected_net_edge_bps - runner_up.expected_net_edge_bps
            if runner_up is not None
            else None
        )
        chosen = _with_reason(
            winner.expression,
            _explain(winner, runner_up, tuple(excluded), basis),
        )
        return ExpressionChoice(
            chosen=chosen,
            chosen_family=winner.family,
            basis=basis,
            scored=tuple(ranked),
            excluded=tuple(excluded),
            runner_up_family=None if runner_up is None else runner_up.family,
            advantage_bps=advantage,
            spread=spread,
        )

    # ------------------------------------------------------------------ preconditions

    def _require_coherent(self, request: ExpressionSelectionRequest) -> None:
        intent = request.intent
        if intent.quantity <= 0:  # pragma: no cover - TradingIntent refuses this at construction
            raise OrderExpressionSelectionError(f"quantity must be positive, got {intent.quantity}")
        if intent.segment not in _PRODUCT_BY_SEGMENT:  # pragma: no cover - the map covers the enum
            raise OrderExpressionSelectionError(
                f"segment {intent.segment} has no product mapping, so no order can be built for it"
            )
        if intent.segment.is_option and request.option_strike_paise is None:
            raise OrderExpressionSelectionError(
                f"{intent.segment} needs a strike before any expression can be priced: the SEBI "
                f"turnover fee is charged on notional turnover (strike x quantity), and defaulting "
                f"it to the premium understates the statutory term by roughly a hundredfold"
            )
        if request.snapshot.instrument_token != intent.instrument_token:
            raise OrderExpressionSelectionError(
                f"the book is for instrument {request.snapshot.instrument_token} and the intent is "
                f"for {intent.instrument_token}; pricing one against the other would be a silent "
                f"cross-instrument error"
            )

    def _observe_or_refuse(self, snapshot: BookSnapshot) -> QuotedSpreadObservation:
        try:
            return observe_quoted_spread(snapshot)
        except QuotedSpreadError as failure:
            raise BookUnpriceableError(
                f"this book cannot price any expression: {failure}. A crossed or one-sided book "
                f"yields a negative or undefined spread, which enters a cost model as a rebate — "
                f"so the selector refuses rather than returning the candidate whose arithmetic "
                f"broke most gracefully"
            ) from failure

    def _market_order_refusal(self, session_date: date) -> list[ExcludedExpression]:
        """The MARKET refusal, read off the facts module every session — never an `if` here."""
        try:
            fact = availability_of_order_type(
                OrderType.MARKET, OrderOrigin.ALGO_API, on=session_date
            )
        except OrderFacilityError as failure:
            return [
                ExcludedExpression(
                    family=ExpressionFamily.MARKET_TAKE,
                    reason=(
                        f"no recorded fact covers the market order for algo flow on "
                        f"{session_date}, and an unevidenced facility is not a facility: {failure}"
                    ),
                )
            ]
        if fact.available:  # pragma: no cover - no dated fact permits this today
            return [
                ExcludedExpression(
                    family=ExpressionFamily.MARKET_TAKE,
                    reason=(
                        "a market order's cost is unknowable in advance by construction, so it "
                        "cannot enter an objective measured in basis points of expected net edge"
                    ),
                    source=fact.source,
                )
            ]
        return [
            ExcludedExpression(
                family=ExpressionFamily.MARKET_TAKE,
                reason=fact.reason,
                source=fact.source,
            )
        ]

    # ------------------------------------------------------------------ candidates

    def _score_aggressive_immediate(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """A marketable limit through the touch, IOC: take what is there, cancel the rest.

        The expression with the fewest unmeasured things in it — what the visible ladder can fill
        is arithmetic, and what it cannot is cancelled rather than left to a fill model.
        """
        family = ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE
        blocked = self._variety_refusal(family, OrderVariety.REGULAR, varieties, request)
        if blocked is not None:
            return blocked
        if not session_open:
            return _session_closed(family)
        priced = self._price_aggressive(request, spread, request.intent.quantity)
        if isinstance(priced, ExcludedExpression):
            return _rebadge(priced, family)
        walk, fill, limit_price = priced
        expression = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=_PRODUCT_BY_SEGMENT[request.intent.segment],
            order_type=OrderType.LIMIT,
            validity=OrderValidity.IMMEDIATE_OR_CANCEL,
            limit_price_paise=limit_price,
        )
        filled = walk.filled_quantity
        caveats: list[str] = []
        if walk.is_censored:
            caveats.append(
                f"the visible ladder fills only {filled} of {request.intent.quantity}; an IOC "
                f"cancels the rest, so this candidate is scored on the part that is arithmetic"
            )
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=fill.upper_cost_bps,
            execution_cost_point_bps=fill.point_cost_bps,
            execution_cost_interval_width_bps=fill.cost_interval_width_bps,
            expected_filled_quantity=filled,
            fill_probability=Decimal(filled) / Decimal(request.intent.quantity),
            edge_survival=_ONE,
            order_count=1,
            fill_price_paise=fill.expected_price_paise,
            is_fill_probability_an_upper_bound=False,
            caveats=tuple(caveats),
            evidence=(
                f"execution from ExecutionFillModel walking the real book "
                f"({fill.maturity.value}, {'censored' if fill.is_censored else 'exact'}); limit "
                f"{limit_price} paise derived from the walk's own terminal price against a touch "
                f"of {spread.crossing_price_paise(request.intent.side)}"
            ),
        )

    def _score_aggressive_day(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """The same marketable limit, left to rest for the day if the book cannot fill it.

        Its residual becomes the best quote at its own price, so it joins the queue at the FRONT —
        nothing is ahead of it, which is a real and measurable advantage over the passive
        candidate and the only thing separating the two when the clip exceeds the visible book.
        """
        family = ExpressionFamily.AGGRESSIVE_LIMIT_DAY
        blocked = self._variety_refusal(family, OrderVariety.REGULAR, varieties, request)
        if blocked is not None:
            return blocked
        if not session_open:
            return _session_closed(family)
        priced = self._price_aggressive(request, spread, request.intent.quantity)
        if isinstance(priced, ExcludedExpression):
            return _rebadge(priced, family)
        walk, fill, limit_price = priced
        expression = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=_PRODUCT_BY_SEGMENT[request.intent.segment],
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=limit_price,
        )
        immediate = walk.filled_quantity
        residual = request.intent.quantity - immediate
        caveats: list[str] = []
        fill_probability = Decimal(immediate) / Decimal(request.intent.quantity)
        edge_survival = _ONE
        evidence_tail = ""
        if residual > 0:
            forecast = self._forecast_residual(request, queue_ahead=0, clip=residual)
            if forecast is None:
                caveats.append(
                    f"the {residual}-unit residual would rest at the front of its own queue, but "
                    f"no measured execution rate is available for this touch, so it is scored as "
                    f"unfilled rather than given an invented fill probability"
                )
            else:
                filled_fraction = Decimal(immediate) / Decimal(request.intent.quantity)
                residual_fraction = Decimal(residual) / Decimal(request.intent.quantity)
                fill_probability = filled_fraction + residual_fraction * forecast.fill_probability
                edge_survival = (
                    filled_fraction + residual_fraction * forecast.edge_survival_fraction
                ) / (filled_fraction + residual_fraction)
                evidence_tail = (
                    f"; residual P(fill)={forecast.fill_probability:.3f} at "
                    f"{forecast.rate.quantity_per_minute:.1f} units/min with nothing ahead"
                )
                if forecast.rate.is_upper_bound:
                    caveats.append(
                        "the residual's fill probability rests on L0.22's UPPER bound for "
                        "execution versus cancellation and is therefore optimistic"
                    )
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=fill.upper_cost_bps,
            execution_cost_point_bps=fill.point_cost_bps,
            execution_cost_interval_width_bps=fill.cost_interval_width_bps,
            expected_filled_quantity=int(
                (Decimal(request.intent.quantity) * fill_probability).to_integral_value()
            ),
            fill_probability=fill_probability,
            edge_survival=edge_survival,
            order_count=1,
            fill_price_paise=fill.expected_price_paise,
            is_fill_probability_an_upper_bound=residual > 0,
            caveats=tuple(caveats),
            evidence=(
                f"execution from ExecutionFillModel against the real book; limit {limit_price} "
                f"paise derived from the walk{evidence_tail}"
            ),
        )

    def _score_iceberg(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """Split the clip so that every leg stays INSIDE the visible book — and DIVIDES the clip.

        The leg size is not chosen, it is read: it is the largest clip this ladder can fill without
        the cost estimate becoming an extrapolation, which is `visible_quantity` on the side being
        taken. The leg COUNT follows from the clip. That is the whole derivation, and it is why an
        iceberg appears at all only when the clip exceeds what is visible — below that, splitting
        buys nothing and pays per-order brokerage `legs` times for the privilege.

        The second half of the derivation is the venue's, and it was missing until the `M/2`
        adversarial review reproduced its consequence. Kite takes a per-LEG quantity, so a leg
        count that does not divide the clip exactly cannot be encoded at all:
        `kite_order_execution_venue._iceberg_quantity_for` raises `OrderExpressionNotEncodableError`
        rather than rounding a leg off its own initiative, and rightly so — the rounding changes
        the size actually sent. A plain `ceil(quantity / visible)` divides exactly on roughly one
        leg count in three, so this selector was proposing an expression the wire provably could
        not carry, and the order died at submission after every cost in it had been priced. The
        constraint therefore belongs HERE, where the candidate is built: the leg count is the
        smallest one that both keeps a leg inside the visible ladder and divides the clip exactly,
        and where no such count exists the family is EXCLUDED with that reason rather than emitted
        and refused later.
        """
        family = ExpressionFamily.ICEBERG_AGGRESSIVE
        blocked = self._variety_refusal(family, OrderVariety.ICEBERG, varieties, request)
        if blocked is not None:
            return blocked
        if not session_open:
            return _session_closed(family)
        quantity = request.intent.quantity
        try:
            probe = walk_order_book(request.snapshot, request.intent.side, quantity)
        except OrderBookWalkError as failure:
            return ExcludedExpression(family=family, reason=f"the book cannot be walked: {failure}")
        visible = probe.visible_quantity
        if visible <= 0:  # pragma: no cover - walk_order_book refuses an empty side first
            return ExcludedExpression(
                family=family, reason="no visible depth on the side being taken"
            )
        smallest_leg_count_that_fits = -(-quantity // visible)  # one leg per visible-book-worth
        if smallest_leg_count_that_fits < ICEBERG_MINIMUM_LEGS:
            return ExcludedExpression(
                family=family,
                reason=(
                    f"the {quantity}-unit clip fits inside the {visible} units visible, so an "
                    f"iceberg would split a ladder that does not need splitting and pay per-order "
                    f"brokerage {ICEBERG_MINIMUM_LEGS} times instead of once"
                ),
                source=ICEBERG_LEG_SOURCE,
            )
        legs = _iceberg_leg_count_the_venue_can_encode(
            quantity, smallest_leg_count_that_fits=smallest_leg_count_that_fits
        )
        if legs is None:
            return ExcludedExpression(
                family=family,
                reason=(
                    f"no leg count between {smallest_leg_count_that_fits} and "
                    f"{ICEBERG_MAXIMUM_LEGS} divides a {quantity}-unit clip exactly, so every "
                    f"iceberg this ladder could carry would need a fractional leg; Kite takes a "
                    f"per-leg quantity and the venue refuses to round one off its own initiative, "
                    f"so the order would be lost at the wire rather than merely mis-sized"
                ),
                source=ICEBERG_LEG_SOURCE,
            )
        caveats = [
            "each leg after the first meets a book this snapshot cannot see; the cost carried here "
            "assumes the ladder presents comparable depth to every leg, which one snapshot cannot "
            "verify"
        ]
        leg_quantity = quantity // legs  # exact by construction — see the leg-count derivation
        priced = self._price_aggressive(request, spread, leg_quantity)
        if isinstance(priced, ExcludedExpression):
            return _rebadge(priced, family)
        _leg_walk, leg_fill, limit_price = priced
        expression = OrderExpression(
            variety=OrderVariety.ICEBERG,
            product=_PRODUCT_BY_SEGMENT[request.intent.segment],
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=limit_price,
            disclosed_quantity=leg_quantity,
            iceberg_legs=legs,
        )
        fill_probability = _ONE
        edge_survival = _ONE
        evidence_tail = ""
        forecast = self._forecast_residual(request, queue_ahead=0, clip=quantity - leg_quantity)
        if forecast is None:
            fill_probability = Decimal(leg_quantity) / Decimal(quantity)
            caveats.append(
                f"no measured execution rate is available, so only the first {leg_quantity}-unit "
                f"leg is scored as filling; the remaining legs are not given an invented "
                f"completion probability"
            )
        else:
            first = Decimal(leg_quantity) / Decimal(quantity)
            rest = _ONE - first
            fill_probability = first + rest * forecast.fill_probability
            edge_survival = first + rest * forecast.edge_survival_fraction
            evidence_tail = (
                f"; completion P={forecast.fill_probability:.3f} over "
                f"{forecast.mean_wait_minutes:.1f} expected minutes at "
                f"{forecast.rate.quantity_per_minute:.1f} units/min"
            )
            if forecast.rate.is_upper_bound:
                caveats.append(
                    "the completion probability rests on L0.22's UPPER bound for execution versus "
                    "cancellation and is therefore optimistic"
                )
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=leg_fill.upper_cost_bps,
            execution_cost_point_bps=leg_fill.point_cost_bps,
            execution_cost_interval_width_bps=leg_fill.cost_interval_width_bps,
            expected_filled_quantity=int(
                (Decimal(quantity) * fill_probability).to_integral_value()
            ),
            fill_probability=fill_probability,
            edge_survival=edge_survival,
            order_count=legs,
            fill_price_paise=leg_fill.expected_price_paise,
            is_fill_probability_an_upper_bound=forecast is not None,
            caveats=tuple(caveats),
            evidence=(
                f"{legs} legs of {leg_quantity} derived from {visible} units visible on the "
                f"{request.intent.side.value} ladder — the smallest leg count that both stays "
                f"inside that ladder and divides {quantity} exactly, which is what the venue can "
                f"encode; per-leg execution priced exactly by ExecutionFillModel "
                f"({leg_fill.maturity.value}){evidence_tail}"
            ),
        )

    def _score_passive(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """Rest at the near touch and be paid the half-spread instead of paying it.

        **This is the candidate the whole refusal rule exists for.** Its cost is the most
        attractive of any expression — a maker earns the half-spread the taker pays — and its value
        depends entirely on a probability that a five-level feed can only bound. Scoring it with a
        default would make the cheapest expression look like the best one in every backtest ever
        run against this engine.
        """
        family = ExpressionFamily.PASSIVE_LIMIT_AT_TOUCH
        blocked = self._variety_refusal(family, OrderVariety.REGULAR, varieties, request)
        if blocked is not None:
            return blocked
        if not session_open:
            return _session_closed(family)
        intent = request.intent
        if intent.horizon_minutes is None:
            return ExcludedExpression(
                family=family,
                reason=(
                    "the intent states no horizon, so the cost of NOT filling cannot be derived; "
                    "a resting order priced without a deadline is scored as though waiting were "
                    "free"
                ),
            )
        queue_ahead = (
            request.snapshot.best_bid_quantity
            if intent.side is TradeLeg.BUY
            else request.snapshot.best_ask_quantity
        )
        forecast = self._forecast_residual(request, queue_ahead=queue_ahead, clip=intent.quantity)
        if forecast is None:
            return ExcludedExpression(
                family=family,
                reason=(
                    f"no measured fill probability is available for the {intent.side.value} touch "
                    f"of instrument {intent.instrument_token}: a resting order's value IS its "
                    f"probability of filling, and this engine will not substitute a plausible "
                    f"number for a measured one"
                ),
                source=(
                    "L0.22 queue-depletion decomposition; no TouchQueueExecutionObserver reading"
                ),
            )
        resting_price = Decimal(
            spread.best_bid_paise if intent.side is TradeLeg.BUY else spread.best_ask_paise
        )
        adverse = _adverse_selection_bps(spread, intent.side)
        execution_cost = -spread.half_spread_bps + adverse
        caveats = [
            "adverse selection is proxied by the micro-price's skew against the resting side, "
            "which is one snapshot's evidence; a realised-passive-fill measurement would replace it"
        ]
        if forecast.rate.is_upper_bound:
            caveats.append(
                "the fill probability rests on L0.22's UPPER bound for execution versus "
                "cancellation and is therefore optimistic"
            )
        expression = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=_PRODUCT_BY_SEGMENT[intent.segment],
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=resting_price,
        )
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=execution_cost,
            execution_cost_point_bps=execution_cost,
            execution_cost_interval_width_bps=adverse,
            expected_filled_quantity=int(
                (Decimal(intent.quantity) * forecast.fill_probability).to_integral_value()
            ),
            fill_probability=forecast.fill_probability,
            edge_survival=forecast.edge_survival_fraction,
            order_count=1,
            fill_price_paise=resting_price,
            is_fill_probability_an_upper_bound=forecast.rate.is_upper_bound,
            caveats=tuple(caveats),
            evidence=(
                f"resting at {resting_price} paise behind {queue_ahead} units; "
                f"P(fill in {intent.horizon_minutes}m)={forecast.fill_probability:.3f}, mean wait "
                f"{forecast.mean_wait_minutes:.1f}m, edge surviving "
                f"{forecast.edge_survival_fraction:.3f}. {forecast.rate.evidence}. Execution cost "
                f"is minus the observed half-spread {spread.half_spread_bps:.2f} bps plus "
                f"{adverse:.2f} bps of micro-price skew against the resting side"
            ),
        )

    def _score_cover(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """The cover order — an aggressive entry welded to a stop that cannot be cancelled.

        Generated where and only where it is genuinely available: NSE equity intraday, with the
        variety permitted on the session's date, and with a protective stop the CALLER supplied.
        The last of those is the real gate: a cover order imposes a stop the intent never asked
        for, and inventing one would be the selector changing the DECISION rather than expressing
        it.
        """
        family = ExpressionFamily.COVER_ORDER
        blocked = self._variety_refusal(family, OrderVariety.COVER, varieties, request)
        if blocked is not None:
            return blocked
        intent = request.intent
        if intent.segment is not ChargeableSegment.EQUITY_INTRADAY:
            return ExcludedExpression(
                family=family,
                reason=(
                    f"the cover order is NSE equity intraday only, and this intent is "
                    f"{intent.segment}"
                ),
                source=availability_of_variety(OrderVariety.COVER, on=intent.session_date).source,
            )
        if not session_open:
            return _session_closed(family)
        stop = request.protective_stop_price_paise
        if stop is None:
            return ExcludedExpression(
                family=family,
                reason=(
                    "a cover order's stop-loss leg is mandatory and cannot be cancelled once "
                    "armed; the intent carries no protective stop, and choosing one here would "
                    "change the decision rather than express it"
                ),
                source=availability_of_variety(OrderVariety.COVER, on=intent.session_date).source,
            )
        priced = self._price_aggressive(request, spread, intent.quantity)
        if isinstance(priced, ExcludedExpression):
            return _rebadge(priced, family)
        walk, fill, limit_price = priced
        if not _stop_is_protective(stop, limit_price, intent.side):
            return ExcludedExpression(
                family=family,
                reason=(
                    f"a stop at {stop} paise does not protect a {intent.side.value} entered at "
                    f"{limit_price} paise; the exchange would reject it, and a rejection at 09:20 "
                    f"is a worse way to discover this than a refusal now"
                ),
            )
        expression = OrderExpression(
            variety=OrderVariety.COVER,
            product=OrderProduct.COVER,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=limit_price,
            trigger_price_paise=stop,
        )
        filled = walk.filled_quantity
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=fill.upper_cost_bps,
            execution_cost_point_bps=fill.point_cost_bps,
            execution_cost_interval_width_bps=fill.cost_interval_width_bps,
            expected_filled_quantity=filled,
            fill_probability=Decimal(filled) / Decimal(intent.quantity),
            edge_survival=_ONE,
            order_count=1,
            fill_price_paise=fill.expected_price_paise,
            is_fill_probability_an_upper_bound=False,
            caveats=(
                "the entry prices exactly like the aggressive DAY limit it is built on; the "
                "mandatory stop's effect on the strategy's own risk and on intraday leverage is "
                "outside an objective measured in basis points of expected net edge, so this "
                "candidate ties with that one and loses on the tie-break rather than on price",
            ),
            evidence=(
                f"aggressive entry at {limit_price} paise with the caller's protective stop at "
                f"{stop} paise"
            ),
        )

    def _score_after_market(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        session_open: bool,
        varieties: frozenset[OrderVariety],
    ) -> ScoredExpression | ExcludedExpression:
        """Queue it for the open, which is the only thing a closed session permits.

        Priced against the touch that was last quoted, and scored with the fill probability the
        measurement supports rather than an assumption that tomorrow's open will oblige. When the
        session is closed this is normally the sole survivor, and the choice then reports itself
        as `SOLE_FEASIBLE` — "the only one" is a weaker claim than "the best one", and the two must
        not be printed the same way.
        """
        family = ExpressionFamily.AFTER_MARKET
        blocked = self._variety_refusal(family, OrderVariety.AFTER_MARKET, varieties, request)
        if blocked is not None:
            return blocked
        if session_open:
            return ExcludedExpression(
                family=family,
                reason=(
                    "the continuous session is open, so an after-market order would sit unmatched "
                    "until the next one for no reason a cost model can express"
                ),
            )
        intent = request.intent
        limit_price = spread.crossing_price_paise(intent.side)
        expression = OrderExpression(
            variety=OrderVariety.AFTER_MARKET,
            product=_PRODUCT_BY_SEGMENT[intent.segment],
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=limit_price,
        )
        return self._score(
            request=request,
            family=family,
            expression=expression,
            execution_cost_bps=spread.half_spread_bps,
            execution_cost_point_bps=spread.half_spread_bps,
            execution_cost_interval_width_bps=_ZERO,
            expected_filled_quantity=intent.quantity,
            fill_probability=_ONE,
            edge_survival=_ONE,
            order_count=1,
            fill_price_paise=limit_price,
            is_fill_probability_an_upper_bound=True,
            caveats=(
                "this order meets the NEXT session's book, and tonight's book cannot price that "
                "one; the execution term is the last observed half-spread and is a reference, not "
                "a forecast",
            ),
            evidence=(
                f"queued against the last quoted touch {limit_price} paise, half-spread "
                f"{spread.half_spread_bps:.2f} bps"
            ),
        )

    # ------------------------------------------------------------------ shared pricing

    def _variety_refusal(
        self,
        family: ExpressionFamily,
        variety: OrderVariety,
        permitted: frozenset[OrderVariety],
        request: ExpressionSelectionRequest,
    ) -> ExcludedExpression | None:
        """No candidate is scored against a facility the facts module refuses."""
        if variety in permitted:
            return None
        try:
            fact = availability_of_variety(variety, on=request.intent.session_date)
        except OrderFacilityError as failure:
            return ExcludedExpression(
                family=family,
                reason=f"no recorded fact covers {variety} on that date: {failure}",
            )
        return ExcludedExpression(family=family, reason=fact.reason, source=fact.source)

    def _price_aggressive(
        self,
        request: ExpressionSelectionRequest,
        spread: QuotedSpreadObservation,
        quantity: int,
    ) -> tuple[OrderBookWalk, ExpectedFill, Decimal] | ExcludedExpression:
        """Walk the real book for `quantity` and derive the price that reaches through it.

        The tolerance is the walk's own terminal price — the deepest rung the order actually has to
        consume — and where the walk is censored, the price the impact estimate implies at its
        pessimistic end. Both come from `L1.05`/`L1.06`; neither is a percentage somebody typed.
        """
        side = request.intent.side
        try:
            walk = walk_order_book(request.snapshot, side, quantity)
            fill = self._fill_model.price_fill(request.snapshot, side, quantity)
        except (OrderBookWalkError, ExecutionFillError) as failure:
            return ExcludedExpression(
                family=ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE,
                reason=f"the book cannot price {quantity} units: {failure}",
            )
        deepest_consumed = Decimal(
            max(entry.price_paise for entry in walk.consumptions)
            if side is TradeLeg.BUY
            else min(entry.price_paise for entry in walk.consumptions)
        )
        implied = fill.pessimistic_price_paise
        raw = (
            max(deepest_consumed, implied)
            if side is TradeLeg.BUY
            else min(deepest_consumed, implied)
        )
        limit_price = _round_away_from_mid(raw, side, request.tick_size_paise)
        if limit_price <= 0:
            # The extrapolated impact has left the domain in which a price exists. A limit priced
            # at or through zero does not bound anything: it fills at whatever the book offers,
            # which is a MARKET order wearing a limit's name, and algo flow may not carry one.
            return ExcludedExpression(
                family=ExpressionFamily.AGGRESSIVE_LIMIT_IMMEDIATE,
                reason=(
                    f"{quantity} units against this book implies a limit at {limit_price} paise — "
                    f"a limit priced that far through the touch bounds nothing and is a market "
                    f"order in disguise, which algo-originated flow may not carry"
                ),
                source=(
                    "NSE/MSD/67753 (2025-04-29): 'Algo orders with order type as Market Order are "
                    "not permitted'"
                ),
            )
        touch = spread.crossing_price_paise(side)
        # A marketable limit that does not reach the touch is a passive order wearing the wrong
        # name, and would be scored with a taker's cost while sitting in a maker's queue.
        limit_price = max(limit_price, touch) if side is TradeLeg.BUY else min(limit_price, touch)
        return walk, fill, limit_price

    def _forecast_residual(
        self, request: ExpressionSelectionRequest, *, queue_ahead: int, clip: int
    ) -> RestingFillForecast | None:
        """The measured forecast for a resting clip, or `None` — never a substitute."""
        if clip <= 0:
            return None
        horizon = request.intent.horizon_minutes
        if horizon is None or self._touch_queue_observer is None:
            return None
        rate = self._touch_queue_observer.observe_touch_execution_rate(
            instrument_token=request.intent.instrument_token,
            resting_side=request.intent.side,
            as_of=request.intent.decided_at,
        )
        if rate is None:
            return None
        return forecast_resting_fill(
            queue_ahead_quantity=queue_ahead,
            clip_quantity=clip,
            horizon_minutes=horizon,
            rate=rate,
        )

    def _statutory_bps(
        self,
        request: ExpressionSelectionRequest,
        *,
        fill_price_paise: Decimal,
        order_count: int,
    ) -> Decimal:
        """`L1.01` on the ENTRY leg, at the price this expression expects and its order count.

        One leg, not a round trip: the selector compares expressions of ONE order, and the exit is
        the same trade whichever way the entry is expressed. The order count is what actually
        differs — brokerage is charged per order, so an iceberg's legs pay it `legs` times, and
        that is frequently the entire reason a split loses.
        """
        intent = request.intent
        trade = TradeSpecification(
            segment=intent.segment,
            quantity=intent.quantity,
            entry_price_paise=fill_price_paise,
            exit_price_paise=fill_price_paise,
            trade_date=intent.session_date,
            strike_paise=request.option_strike_paise,
            is_short_first=intent.side is TradeLeg.SELL,
            orders_per_leg=order_count,
        )
        try:
            leg = self._cost_engine.price_leg(
                trade, intent.side, fill_price_paise, known_as_of=request.known_as_of
            )
        except TransactionCostError as failure:
            raise OrderExpressionSelectionError(
                f"the statutory term cannot be priced for {intent.segment} on "
                f"{intent.session_date}: {failure}. A cost model missing its statutory half is a "
                f"floor pretending to be a hurdle, so the selector refuses rather than scoring "
                f"the execution term alone"
            ) from failure
        notional = fill_price_paise * Decimal(intent.quantity)
        if notional <= 0:  # pragma: no cover - a non-positive touch is refused far earlier
            raise BookUnpriceableError("a zero notional has no basis points")
        return leg.exact_total_paise / notional * _BASIS_POINTS

    def _score(
        self,
        *,
        request: ExpressionSelectionRequest,
        family: ExpressionFamily,
        expression: OrderExpression,
        execution_cost_bps: Decimal,
        execution_cost_point_bps: Decimal,
        execution_cost_interval_width_bps: Decimal,
        expected_filled_quantity: int,
        fill_probability: Decimal,
        edge_survival: Decimal,
        order_count: int,
        fill_price_paise: Decimal,
        is_fill_probability_an_upper_bound: bool,
        caveats: tuple[str, ...],
        evidence: str,
    ) -> ScoredExpression:
        """The objective, applied once, identically, to every candidate.

            edge_after_delay = claimed_edge x edge_survival
            gross_net        = edge_after_delay - execution - statutory
            expected_net     = gross_net - P(not filled) x gross_net

        The last line is the task's formula with `edge_forgone` named: what a candidate forgoes by
        not filling is exactly the net edge it would have earned by filling — no costs are paid on
        an order that never trades, so an unfilled expression is worth nothing rather than being
        worth a loss. Two candidates are therefore separated by the product of what they earn and
        how likely they are to earn it, which is the only combination that does not let a cheap
        expression that never fills beat an expensive one that always does.
        """
        claimed = request.intent.expected_edge_bps
        edge_after_delay = claimed * edge_survival
        statutory = self._statutory_bps(
            request, fill_price_paise=fill_price_paise, order_count=order_count
        )
        gross_net = edge_after_delay - execution_cost_bps - statutory
        expected_net = gross_net - (_ONE - fill_probability) * gross_net
        return ScoredExpression(
            family=family,
            expression=expression,
            claimed_edge_bps=claimed,
            edge_after_delay_bps=edge_after_delay,
            execution_cost_bps=execution_cost_bps,
            execution_cost_point_bps=execution_cost_point_bps,
            execution_cost_interval_width_bps=execution_cost_interval_width_bps,
            statutory_cost_bps=statutory,
            fill_probability=fill_probability,
            edge_forgone_bps=gross_net,
            expected_net_edge_bps=expected_net,
            expected_filled_quantity=expected_filled_quantity,
            order_count=order_count,
            is_fill_probability_an_upper_bound=is_fill_probability_an_upper_bound,
            caveats=caveats,
            evidence=evidence,
        )


# ------------------------------------------------------------------------ free functions


def _ranking_key(candidate: ScoredExpression) -> tuple[Decimal, int, Decimal, int]:
    """Arg-max on the score; ties broken by how much of the score was measured.

    A tie on expected net edge is not a coin flip. The candidate resting on FEWER unmeasured things
    is the better answer to the same question, and after that the one whose cost interval is
    narrower — because a tie between a tight estimate and a wide one is a tie only in the middle of
    the wide one. The family's declaration order is the last resort, so the function is total and
    the choice is reproducible.
    """
    order = list(ExpressionFamily)
    return (
        candidate.expected_net_edge_bps,
        -len(candidate.caveats),
        -candidate.execution_cost_interval_width_bps,
        -order.index(candidate.family),
    )


def _with_reason(expression: OrderExpression, reason: str) -> OrderExpression:
    """Re-stamp the chosen expression with why it won. Frozen, so it is rebuilt, not mutated."""
    return OrderExpression(
        variety=expression.variety,
        product=expression.product,
        order_type=expression.order_type,
        validity=expression.validity,
        limit_price_paise=expression.limit_price_paise,
        trigger_price_paise=expression.trigger_price_paise,
        disclosed_quantity=expression.disclosed_quantity,
        iceberg_legs=expression.iceberg_legs,
        validity_minutes=expression.validity_minutes,
        market_protection_percent=expression.market_protection_percent,
        chosen_because=reason,
    )


def _explain(
    winner: ScoredExpression,
    runner_up: ScoredExpression | None,
    excluded: tuple[ExcludedExpression, ...],
    basis: SelectionBasis,
) -> str:
    """The sentence `chosen_because` carries: what it beat, by how much, and what never competed."""
    if runner_up is None:
        head = (
            f"{winner.family.value} was the only expression to survive the facility, session and "
            f"measurement filters, so it was not compared against anything"
        )
    else:
        head = (
            f"{winner.family.value} beat {runner_up.family.value} by "
            f"{winner.expected_net_edge_bps - runner_up.expected_net_edge_bps:.2f} bps of expected "
            f"net edge ({winner.expected_net_edge_bps:.2f} against "
            f"{runner_up.expected_net_edge_bps:.2f})"
        )
    body = (
        f"edge {winner.edge_after_delay_bps:.2f} - execution {winner.execution_cost_bps:.2f} - "
        f"statutory {winner.statutory_cost_bps:.2f}, filling with probability "
        f"{winner.fill_probability:.3f} across {winner.order_count} order(s)"
    )
    refusals = "; ".join(entry.describe() for entry in excluded)
    tail = f" Refused: {refusals}." if refusals else ""
    caveats = " Caveats: " + "; ".join(winner.caveats) + "." if winner.caveats else ""
    return f"{head} [{basis.value}]. {body}.{tail}{caveats}"


def _adverse_selection_bps(spread: QuotedSpreadObservation, side: TradeLeg) -> Decimal:
    """What the book's own imbalance says a resting order on `side` is about to be run over by.

    The micro-price is the size-weighted fair value: a bid stacked ten deep against a thin ask says
    the next trade is more likely to happen above the mid. A resting BUY is adversely selected when
    that lean is DOWNWARD — the sellers are the heavy side, and the order that fills you is the one
    that keeps going. Only the adverse direction is charged; crediting the favourable one would
    book a profit for standing in a queue, which is a claim this engine cannot support.
    """
    skew = spread.micro_price_skew_bps
    if skew is None:
        return _ZERO
    adverse = -skew if side is TradeLeg.BUY else skew
    return max(_ZERO, adverse)


def _round_away_from_mid(
    price_paise: Decimal, side: TradeLeg, tick_size_paise: Decimal | None
) -> Decimal:
    """Round a derived limit price AWAY from the mid, onto the tick the instrument really trades on.

    Away, always: rounding a buy's protective cap down by half a tick is how a marketable order
    silently becomes an unmarketable one. Without a stated tick the tape's own unit — whole paise —
    is used, which is the finest grid any NSE instrument quotes on.
    """
    tick = tick_size_paise if tick_size_paise is not None and tick_size_paise > 0 else _ONE
    steps = price_paise / tick
    rounded = steps.to_integral_value(
        rounding="ROUND_CEILING" if side is TradeLeg.BUY else "ROUND_FLOOR"
    )
    return rounded * tick


def _stop_is_protective(stop_paise: Decimal, entry_paise: Decimal, side: TradeLeg) -> bool:
    """A stop below a long and above a short. The other way round is an instant exit."""
    return stop_paise < entry_paise if side is TradeLeg.BUY else stop_paise > entry_paise


def _iceberg_leg_count_the_venue_can_encode(
    quantity: int, *, smallest_leg_count_that_fits: int
) -> int | None:
    """The smallest leg count that keeps a leg inside the ladder AND divides the clip exactly.

    Two constraints, and both are hard:

    * **A leg must fit the visible ladder.** `quantity / legs <= visible` is exactly
      `legs >= ceil(quantity / visible)`, which is why the caller's `smallest_leg_count_that_fits`
      is the floor of the search rather than a separate test inside it. Below it, a leg reaches
      past what the snapshot can see and its cost becomes an extrapolation.
    * **A leg count must divide the clip.** Kite takes a per-leg quantity and
      `kite_order_execution_venue._iceberg_quantity_for` refuses a clip that does not divide
      evenly, so a count failing this is not a worse candidate — it is an unsendable one.

    Searching UPWARDS from the smallest count that fits gives the largest encodable leg, which is
    the fewest orders, the least brokerage and the least signalling — the same direction the rest of
    this scorer prefers. `None` means the two constraints have no common solution for this clip
    against this ladder (a prime clip larger than the ladder is the ordinary case), and the caller
    must then emit no iceberg candidate at all rather than one the wire would refuse.
    """
    for legs in range(
        max(smallest_leg_count_that_fits, ICEBERG_MINIMUM_LEGS), ICEBERG_MAXIMUM_LEGS + 1
    ):
        if quantity % legs == 0:
            return legs
    return None


def _session_closed(family: ExpressionFamily) -> ExcludedExpression:
    return ExcludedExpression(
        family=family,
        reason=(
            "the continuous session is not open at the decision instant, so this variety cannot "
            "reach a matching engine"
        ),
        source="L11.06 session phase over the NSE trading calendar",
    )


def _rebadge(excluded: ExcludedExpression, family: ExpressionFamily) -> ExcludedExpression:
    """Carry a shared pricing refusal back under the family that actually asked for it."""
    return ExcludedExpression(family=family, reason=excluded.reason, source=excluded.source)
