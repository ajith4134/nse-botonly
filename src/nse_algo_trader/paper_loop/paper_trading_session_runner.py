"""A trading day that runs itself, on paper, through the real order path (`F04`).

Specification: `docs/research/228_paper_trading_loop_and_simulated_venue_spec.md` ·
Operator decisions: `A.108`.

Everything this rebuild has produced so far is correct and inert. `F02`'s order path has never
carried an order it did not manufacture in a test; `F03`'s sizer answers "how much" for nobody; the
paper capital ledger has had one commit/release pair written by a probe; `SessionRiskStateStore`'s
whole write surface has never run outside a fixture. This module is the named consumer that makes
all four load-bearing at once, and the first honest answer to *does any of this work together*.

**The clock is replayed, and the leakage guard is the load-bearing part** (`A.108` decision 1).
Every read is `availability_time <= decision_instant`, never `bar_timestamp`, and every book comes
from the recorded tape at that same instant. That one predicate is all that separates a replay from
a backtest that sees the future, so it is exercised on every step rather than asserted once.

**A paper order becomes a fill by going THROUGH the order path** (`A.108` decision 2). The loop
builds a real `TradingIntent`, writes it to the real write-ahead journal, and hands it to the real
`CrashSafeOrderPlacer`, which sends it to a venue that simulates the exchange from the recorded
book. The intent journal, the lifecycle state machine, the broker-truth reconciler and the crash
recovery are the real ones: **the code that will one day carry real money is the code being tested
now.** `R.13` is the argument — a paper record produced by code that gets thrown away before live
proves only that the allocation was right, never that the execution was.

**Capital is reserved BEFORE the order is sent, and released when it does not go.** The ledger is
the funding authority, not a scoreboard written afterwards: an order the book cannot fund must be
refused at the moment of decision, because that is the discipline live trading will impose and a
paper record that skips it overstates how often a signal was actually taken.

**Squaring off is the failure mode, not the strategy** (`R.01`). Intraday by default: every open
position is closed at the session's last decision instant, through the same path, against the same
recorded book. A position that survives the close in this loop is a defect, and the report says so
rather than carrying it silently into the next session.

**What this loop does NOT do, recorded so it is never assumed** (`R.11`):

* it does not model margin — the simulated venue has no funds view and stamps every position report
  it emits as such, so a live-margin rejection cannot be reproduced here;
* it does not model the live tick path — latency, gaps and mid-session disconnects stay unverified
  until the live clock lands, which `A.108` records as an open blocker from the day this ships;
* it does not model the queue that forms AFTER a snapshot, so a resting limit fills only from the
  visible ladder of a book that has already arrived.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.cost_gate.per_instrument_reversion_horizon_selector import (
    NoHorizonHasEvidenceError,
    PerInstrumentReversionHorizonSelector,
    SelectedHorizon,
)
from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.decision_trace.decision_trace_record import (
    CandidateAction,
    ConsultedInput,
    DecisionKind,
    DecisionTrace,
    DecisionTraceError,
    DecisionTraceStore,
    GateEvaluation,
    GateOutcome,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.broker_truth_reconciler import BrokerTruthReconciler
from nse_algo_trader.order_path.crash_safe_order_placer import (
    AlwaysPermits,
    CrashSafeOrderPlacer,
    PlacementOutcome,
    PlacementVerdict,
    SubmissionRateGate,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.simulated_order_execution_venue import (
    SimulatedOrderExecutionVenue,
)
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.paper_capital_ledger import (
    PaperCapitalError,
    PaperCapitalLedger,
)
from nse_algo_trader.paper_loop.paper_session_signal_source import (
    PaperSignal,
    PaperSignalSource,
)
from nse_algo_trader.replay_session_clock import ReplaySessionClock
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.sizing.pre_trade_risk_gate import (
    DerivedLimits,
    PreTradeRiskGate,
    PriceCollarUnavailableError,
    RegulatoryFacts,
    RiskGateVerdict,
    derive_limits,
    realised_move_quantile_from_closes,
)
from nse_algo_trader.sizing.session_risk_state_store import (
    DailyLossLimit,
    RiskLatch,
    SessionRiskState,
    SessionRiskStateError,
    SessionRiskStateStore,
)
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    RealStorePaths,
    SizingInputAssemblyError,
    assemble_sizing_inputs,
    deviation_and_dispersion,
    recent_closes_available_at,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    PositionSizingError,
    SizedPosition,
    VolatilityTargetedPositionSizer,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    QualityVerdict,
    TradeQualityEvidenceCard,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_store import TradeQualityEvidenceStore
from nse_algo_trader.trade_quality.trade_quality_floor_engine import (
    QualityAssessmentRequest,
    TradeQualityFloorEngine,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    TradeSpecification,
    TransactionCostError,
)

PAISE_PER_RUPEE = Decimal(100)

BASIS_POINTS_PER_UNIT = Decimal(10_000)
"""A unit conversion: `expected_move_fraction` is a fraction, `PricedSignal` carries bps."""


class PaperSessionError(Exception):
    """The session cannot proceed, and continuing would record a day that did not happen."""


class RecordedBookSource(Protocol):
    """The recorded depth tape, as this loop needs to see it.

    A protocol rather than the concrete replay engine, because it is the seam a hermetic harness
    injects a synthetic book through (`R.J`). The production implementation is
    `OrderBookSnapshotReplayEngine`, which satisfies this structurally.
    """

    def book_at(self, instrument_token: int, as_of: datetime) -> BookSnapshot | None: ...


class RoundTripCostPricer(Protocol):
    """`F01`'s cost engine, as this loop needs to see it."""

    def price_round_trip(self, trade: TradeSpecification) -> object: ...


class RegulatoryFactsSource(Protocol):
    """Tier-1 walls for one instrument, known by one instant (`L0.24`/`L0.25`/`L0.28`)."""

    def facts_for(self, trading_symbol: str, *, known_by: datetime) -> RegulatoryFacts: ...


class UncheckedRegulatoryFacts:
    """Every wall unread — the honest source when no ingest store is attached.

    It exists so a caller cannot accidentally get "not banned" out of a store that was never
    opened: every field stays `None`, `unread_sources` names all three, and the gate reports them
    as unchecked on every verdict it issues. Absence is carried into the record rather than
    resolved into permission.
    """

    def facts_for(self, trading_symbol: str, *, known_by: datetime) -> RegulatoryFacts:
        del known_by
        return RegulatoryFacts(trading_symbol=trading_symbol)


@dataclass(frozen=True, slots=True)
class PaperInstrument:
    """One tradeable instrument, with the lot size that makes a quantity legal."""

    instrument_token: int
    trading_symbol: str
    lot_size: int

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise PaperSessionError(
                f"{self.trading_symbol} has a lot size of {self.lot_size}; a substituted lot size "
                "is worse than an absent one (`L1.09`), so this instrument is not tradeable"
            )


@dataclass(frozen=True, slots=True)
class PaperSessionPolicy:
    """Everything the operator owns about how a paper day is run.

    Every field is required. A default here would be a risk setting nobody chose — the exact shape
    `R.03` forbids — and the two regime thresholds in particular decide whether the system trades
    at all.
    """

    session_date: date
    decision_step: timedelta
    horizon_bars: int
    concurrent_position_capacity: int
    segment: ChargeableSegment
    segment_margin_fraction: Decimal
    registration_threshold_orders_per_second: int
    minimum_regime_concentration: float
    minimum_regime_agreement: float
    armed_classifiers: tuple[str, ...]
    price_collar_quantile: Decimal
    tradeable_window: tuple[datetime, datetime] | None = None
    """The part of the session the recorded book actually covers, or `None` for all of it.

    Derived from the tape by the caller, never chosen (`A.116`). The depth capture starts late and
    can stop early — on 2026-08-13 it ran 09:51 to 12:15 against a session that closes at 15:30 —
    and a decision taken where no book was ever recorded cannot fill, so replaying it measures the
    gap in the capture rather than the strategy. Instants outside the window are STEPPED (the clock
    and the estimators keep advancing on real bars) and no entry is considered at them."""
    """Which quantile of this instrument's own realised move counts as "unusually far".

    Operator policy, required like the rest: a collar at the median refuses half of all normal
    prices, and one at the maximum refuses nothing.
    """

    def __post_init__(self) -> None:
        if self.decision_step <= timedelta(0):
            raise PaperSessionError(
                f"a decision step of {self.decision_step} does not advance the clock"
            )
        if self.horizon_bars <= 0:
            raise PaperSessionError(f"a horizon of {self.horizon_bars} bars is not a horizon")
        if self.concurrent_position_capacity <= 0:
            raise PaperSessionError(
                "a capacity of zero concurrent positions cannot hold the position the sizer is "
                "about to divide the book by"
            )
        if not Decimal(0) < self.price_collar_quantile < Decimal(1):
            raise PaperSessionError(
                f"a collar quantile of {self.price_collar_quantile} is not a quantile"
            )
        if not Decimal(0) < self.segment_margin_fraction <= Decimal(1):
            raise PaperSessionError(
                f"a segment margin fraction of {self.segment_margin_fraction} is not a fraction "
                "of notional; leverage is its reciprocal and would be meaningless"
            )

    @property
    def horizon(self) -> timedelta:
        """How long a position is held before the edge it was taken on has expired."""
        return self.decision_step * self.horizon_bars


@dataclass(slots=True)
class OpenPaperPosition:
    """A position this session opened, and everything needed to close and account for it."""

    position_key: str
    entry_intent_id: str
    instrument: PaperInstrument
    side: TradeLeg
    ordered_quantity: int
    filled_quantity: int
    average_entry_paise: Decimal | None
    committed_rupees: Decimal
    opened_at: datetime
    expires_at: datetime
    horizon_bars: int = 0
    stated_win_probability: float | None = None
    """The bot's own claim at the moment it decided, carried so the close can record it.

    `L5.31`'s calibrator can only be fitted on forecasts that were written down BEFORE the outcome.
    Reconstructing this afterwards would not be a forecast, so it travels with the position from the
    decision that made it.
    """
    """The holding time CHOSEN for this position, which is no longer one number for the session."""

    exit_intent_ids: tuple[str, ...] = ()
    """Every square-off sent for this position, in order.

    More than one is normal rather than exceptional: an entry that is still filling when its
    horizon expires is exited for what has filled SO FAR, and the quantity that arrives afterwards
    needs its own exit. A single exit id would have left that residual carried past the close —
    the exact `R.01` failure this loop exists to make impossible.
    """

    exit_ordered_quantity: int = 0
    exit_filled_quantity: int = 0
    average_exit_paise: Decimal | None = None
    closed_at: datetime | None = None
    close_reason: str = ""
    square_off_attempts: int = 0
    """How many exit DECISIONS this position has taken, including refused ones.

    Squaring off genuinely takes more than one decision at one instant: a round that exits what has
    filled so far draws further fills out of the venue, and the residual needs its own exit. Those
    are separate decisions about different quantities, so each gets its own trace ordinal — with
    the instant alone as the key the second was refused as "already recorded" and dropped, and the
    record then understated the exited quantity while blaming a duplicate.
    """

    @property
    def open_quantity(self) -> int:
        """What is still at risk: filled on the entry and not yet filled back on an exit."""
        return max(self.filled_quantity - self.exit_filled_quantity, 0)

    @property
    def is_open(self) -> bool:
        """Derived from the quantities, never latched by `closed_at`.

        It WAS latched, and the first real session showed why that is wrong: a position was
        squared off for the 377 units that had filled, marked closed, and then its entry order
        kept filling — to 5,232 — with nothing left willing to exit the rest, because every exit
        path tested `closed_at`. A position is open exactly while quantity is open, and
        `closed_at` records when it last went flat rather than deciding whether it is.
        """
        return self.open_quantity > 0

    @property
    def unexited_quantity(self) -> int:
        """Filled and not yet SENT for exit — what a square-off still has to sell or buy back."""
        return max(self.filled_quantity - self.exit_ordered_quantity, 0)

    @property
    def exposure_rupees(self) -> Decimal:
        """What is actually at risk — filled quantity at the price it was filled at."""
        if self.average_entry_paise is None:
            return Decimal(0)
        return (Decimal(self.filled_quantity) * self.average_entry_paise) / PAISE_PER_RUPEE

    def realised_rupees(self) -> Decimal:
        """Gross result of the round trip, before costs. Zero while either leg is unpriced."""
        if self.average_entry_paise is None or self.average_exit_paise is None:
            return Decimal(0)
        quantity = Decimal(min(self.filled_quantity, self.exit_filled_quantity))
        direction = Decimal(1) if self.side is TradeLeg.BUY else Decimal(-1)
        move = (self.average_exit_paise - self.average_entry_paise) * direction
        return (move * quantity) / PAISE_PER_RUPEE


@dataclass(frozen=True, slots=True)
class PaperDecisionRecord:
    """One instrument, one instant, and every reason the loop did or did not act.

    `L13.29`: the reasoning cannot be reconstructed afterwards, so it is recorded as it happens.
    """

    at: datetime
    trading_symbol: str
    signal: PaperSignal
    sized: SizedPosition | None
    verdict: RiskGateVerdict | None
    placement: PlacementOutcome | None
    outcome: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PaperSessionReport:
    """What the day did, folded from the records rather than accumulated as it went."""

    session_date: date
    started_at: datetime
    ended_at: datetime
    steps_taken: int
    instruments_considered: int
    decisions: tuple[PaperDecisionRecord, ...]
    positions: tuple[OpenPaperPosition, ...]
    gross_realised_rupees: Decimal
    costs_rupees: Decimal
    opening_balance_rupees: Decimal
    closing_balance_rupees: Decimal
    latches_tripped: tuple[RiskLatch, ...]
    unfilled_at_close: tuple[str, ...]
    open_at_close: tuple[str, ...]
    halted_reason: str = ""
    tradeable_steps: int = 0
    """How many of `steps_taken` the recorded book actually covered (`A.116`)."""

    decision_trace_skip_reasons: tuple[str, ...] = ()
    """WHY traces were skipped, deduplicated with counts — not merely how many."""

    decision_traces_skipped: int = 0
    """Decisions whose trace could not be recorded (`L13.29`).

    Counted and reported rather than swallowed: evidence about a decision must not be able to lose
    the decision, but a loss nobody sees is indistinguishable from a quiet session. A non-zero
    number here means the emitter and the contract disagree about what a decision looks like, and
    that is a defect in one of them.
    """

    @property
    def net_realised_rupees(self) -> Decimal:
        return self.gross_realised_rupees - self.costs_rupees

    @property
    def orders_placed(self) -> int:
        return sum(
            1
            for record in self.decisions
            if record.placement is not None
            and record.placement.verdict
            in (PlacementVerdict.PLACED, PlacementVerdict.ALREADY_PLACED)
        )

    @property
    def refusals_by_reason(self) -> tuple[tuple[str, int], ...]:
        """Why the loop declined, most common first — the histogram an operator reads first."""
        counts: dict[str, int] = {}
        for record in self.decisions:
            if record.outcome == "placed":
                continue
            counts[record.outcome] = counts.get(record.outcome, 0) + 1
        return tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))

    def describe(self) -> str:
        return (
            f"{self.session_date.isoformat()}: {self.steps_taken} steps "
            f"({self.tradeable_steps} with a recorded book) over "
            f"{self.instruments_considered} instruments, {self.orders_placed} order(s) placed, "
            f"{len(self.positions)} position(s), gross Rs {self.gross_realised_rupees} less costs "
            f"Rs {self.costs_rupees} = net Rs {self.net_realised_rupees}; balance Rs "
            f"{self.opening_balance_rupees} -> Rs {self.closing_balance_rupees}"
            + (
                f"; {self.decision_traces_skipped} decision trace(s) SKIPPED"
                + (
                    f" [{'; '.join(self.decision_trace_skip_reasons)}]"
                    if self.decision_trace_skip_reasons
                    else ""
                )
                if self.decision_traces_skipped
                else ""
            )
            + (f"; HALTED: {self.halted_reason}" if self.halted_reason else "")
        )


@dataclass(slots=True)
class PaperTradingSessionRunner:
    """One paper session, end to end, through the real order path.

    Every collaborator is injected. That is not test scaffolding: the venue seam is what makes
    paper and live the same code above it (`L9.03`), and the book source is what lets a hermetic
    harness verify the loop's control flow without a tape (`R.J`) while production passes the real
    replay engine through the identical parameter.
    """

    policy: PaperSessionPolicy
    instruments: Sequence[PaperInstrument]
    clock: ReplaySessionClock
    signal_source: PaperSignalSource
    book_source: RecordedBookSource
    journal: OrderIntentJournal
    venue: SimulatedOrderExecutionVenue
    ledger: PaperCapitalLedger
    risk_store: SessionRiskStateStore
    capital: TradingCapital
    store_paths: RealStorePaths = field(default_factory=RealStorePaths)
    """Where the bar and calibration stores live. A value rather than a fixed path so a
    verification run can point the identical code at a copy (`R.03`)."""

    horizon_selector: PerInstrumentReversionHorizonSelector | None = None
    """Chooses the holding time per deviation from the fitted grid (`A.115`).

    `None` falls back to `policy.horizon_bars` for every entry, which is what the loop did before
    and is kept only so a hermetic test can hold the horizon still while it varies something else.
    Production passes a selector: one horizon for every instrument synchronises the exits, and the
    rate gate refuses a wave."""

    rate_gate: SubmissionRateGate = field(default_factory=AlwaysPermits)
    """The wire's own ceiling (`L3.06`). Defaults to the null gate so a hermetic test can isolate
    the loop, and `SimulatedTimeSubmissionRateGate` is what production passes — without it the
    paper record assumes an order flow the wire would not have accepted (`A.114`)."""

    cost_pricer: RoundTripCostPricer | None = None
    regulatory_facts: RegulatoryFactsSource = field(default_factory=UncheckedRegulatoryFacts)
    sizer: VolatilityTargetedPositionSizer = field(default_factory=VolatilityTargetedPositionSizer)
    gate: PreTradeRiskGate = field(default_factory=PreTradeRiskGate)
    decision_trace_store: DecisionTraceStore | None = None
    """Where this session's reasoning is recorded, at the instant, or `None` to record none.

    `A.29`/`L13.29`: reasoning cannot be reconstructed after the fact, so the trace is emitted from
    `_record` — the one place every decision passes through, with the signal, the sizing and the
    verdicts still in hand. Assembling traces from `PaperSessionReport.decisions` afterwards would
    produce exactly the confident fiction that entry forbids, even though the fields would look the
    same.
    """

    trade_quality_gate: TradeQualityFloorEngine | None = None
    """`L5.31`'s pre-trade quality floor, or `None` to run the loop without one.

    `None` is the honest default for a hermetic test that is isolating something else, and it is
    NOT a permissive gate: with no gate attached no proposal is quality-assessed and the session
    report says so. Production passes a real one, fitted on the paper track record.
    """

    trade_quality_store: TradeQualityEvidenceStore | None = None
    """Where each verdict is recorded, refusals included — they are the control group (`L5.31`)."""

    trade_quality_segment: TradingSegment = TradingSegment.CASH_INTRADAY
    """Which segment the gate attributes these proposals to. One holon per runner, so one value."""

    trace_bot_identity: str = "cash_intraday_mean_reversion_bot"
    trace_exit_null_action: str = "hold"
    """What this bot calls "keep holding" — the null action on the way OUT.

    Separate from the entry null action because the two vocabularies genuinely differ: an entry
    chooses between abstaining and entering, an exit between holding and exiting. One field for
    both would have forced an exit trace to call its null action `abstain`, which describes a
    decision that was never in front of it.
    """

    trace_null_action: str = "abstain"
    """What this bot's engine calls "did not act" — configured, not assumed.

    Written out in three places before, so a segment bot whose engine calls it `NO_TRADE` or `FLAT`
    would have had every one of its traces refused by the vocabulary check and dropped as a skip:
    the check would have disabled the tracing it exists to strengthen. It sits beside the bot
    identity because it is the same kind of fact — a property of this bot, not of the runner.
    """
    """Whose reasoning this is.

    One identity per bot, for the same reason the track record has one.
    """

    _placer: CrashSafeOrderPlacer | None = field(default=None, repr=False)
    _reconciler: BrokerTruthReconciler | None = field(default=None, repr=False)
    _positions: dict[str, OpenPaperPosition] = field(default_factory=dict, repr=False)
    _decisions: list[PaperDecisionRecord] = field(default_factory=list, repr=False)
    _halted_reason: str = field(default="", repr=False)
    _stepped: list[datetime] = field(default_factory=list, repr=False)
    _traces_skipped: int = field(default=0, repr=False)
    _trace_skip_reasons: Counter[str] = field(default_factory=Counter, repr=False)

    def __post_init__(self) -> None:
        if not self.instruments:
            raise PaperSessionError(
                "a session over no instruments is not a session; the universe is assembled by the "
                "caller so that `R.09`'s full-universe obligation is visible at the call site"
            )
        self._placer = CrashSafeOrderPlacer(
            journal=self.journal,
            venue=self.venue,
            namespace=OrderNamespace.SIMULATED,
            rate_gate=self.rate_gate,
        )
        self._reconciler = BrokerTruthReconciler(
            journal=self.journal,
            venue=self.venue,
            namespace=OrderNamespace.SIMULATED,
        )

    # --- the session ------------------------------------------------------------------------

    def run(self) -> PaperSessionReport:
        """Step the whole session, then square off. The one entry point.

        Raises:
            PaperSessionError: the session could not be opened or the ledger is unseeded — both
                cases where continuing would record a day against capital nobody stated.
        """
        started_at = self.clock.now
        opening_balance = self._ledger_balance(started_at)
        try:
            self.risk_store.open_session(
                session_date=self.policy.session_date,
                opening_equity_rupees=opening_balance,
                occurred_at=started_at,
            )
        except SessionRiskStateError as unopened:
            raise PaperSessionError(
                f"the session risk state could not be opened: {unopened}"
            ) from unopened

        steps = 0
        last_moment = started_at
        self._stepped = []
        for moment in self.clock.step_through_session(self.policy.decision_step):
            steps += 1
            last_moment = moment
            self._stepped.append(moment)
            self._observe_books(moment)
            self._advance_matching(moment)
            self._apply_fills(moment)
            if self._session_is_halted(moment):
                break
            self._close_expired_or_reversed(moment)
            if self._is_tradeable(moment):
                self._consider_entries(moment)
            self._advance_matching(moment)
            self._apply_fills(moment)

        closing_moment = last_moment
        # A halted session squares off through the same call, and passing the close's reason for it
        # made the trace record a `session_clock` gate REFUSED with "the intraday deadline arrived"
        # — at 10:15, with the deadline five hours away and the position's own horizon still
        # holding 5,400 seconds. A refusing gate asserting a false fact is the defect the gate
        # vocabulary exists to prevent. The order's `strategy_identity` and `close_reason` are
        # written from this same string, so all three now agree on the truth rather than on a
        # falsehood.
        self._square_off_everything(
            closing_moment,
            reason=(
                f"halted: {self._halted_reason}"
                if self._halted_reason
                else "session close (`R.01`, intraday)"
            ),
        )
        gross, costs = self._account_for_closed_positions(closing_moment)
        self.risk_store.close_session(session_date=self.policy.session_date)
        state = self._session_state(closing_moment)
        return PaperSessionReport(
            session_date=self.policy.session_date,
            started_at=started_at,
            ended_at=closing_moment,
            steps_taken=steps,
            instruments_considered=len(self.instruments),
            tradeable_steps=sum(1 for moment in self._stepped if self._is_tradeable(moment)),
            decision_traces_skipped=self._traces_skipped,
            decision_trace_skip_reasons=tuple(
                f"{count}x {reason}" for reason, count in self._trace_skip_reasons.most_common()
            ),
            decisions=tuple(self._decisions),
            positions=tuple(self._positions.values()),
            gross_realised_rupees=gross,
            costs_rupees=costs,
            opening_balance_rupees=opening_balance,
            closing_balance_rupees=self._ledger_balance(closing_moment),
            latches_tripped=state.tripped_latches if state is not None else (),
            unfilled_at_close=tuple(
                position.position_key
                for position in self._positions.values()
                if position.filled_quantity < position.ordered_quantity
            ),
            open_at_close=tuple(
                position.position_key for position in self._positions.values() if position.is_open
            ),
            halted_reason=self._halted_reason,
        )

    # --- the tape ---------------------------------------------------------------------------

    def _is_tradeable(self, moment: datetime) -> bool:
        """Whether the recorded book covers this instant at all.

        Outside the window an entry could never fill, so taking one would put a position in the
        record that the tape cannot account for — and every P&L drawn from it would be measuring
        the capture's gaps (`A.116`).
        """
        window = self.policy.tradeable_window
        if window is None:
            return True
        return window[0] <= moment <= window[1]

    def _observe_books(self, moment: datetime) -> None:
        """Give the venue the book that was recorded at this instant, and nothing newer."""
        for instrument in self.instruments:
            book = self.book_source.book_at(instrument.instrument_token, moment)
            if book is not None:
                self.venue.observe_book(book)
                continue
            # No book at this instant: the venue must FORGET the one it has, or it keeps filling
            # from a snapshot whose counterparty is long gone. An absent book is a refusal to
            # fill, and a cached one turns that refusal into an invented fill.
            self.venue.forget_book(instrument.instrument_token)

    def _advance_matching(self, moment: datetime) -> None:
        """Poll the venue until nothing about its order book changes any more.

        Bounded by the venue's own progress rather than by a chosen poll count, and the
        termination condition is the venue's STATE rather than the trades a poll returns: the
        first poll after a placement only moves the order from acknowledged to open and returns
        no trade at all, so "poll while trades arrive" would stop one step before the first fill
        and every paper session would record an order that never filled. Each poll advances a
        status or consumes at most one rung, both monotone and finite, so a poll that changes
        nothing is the end of what this book can do.
        """
        previous = self._venue_state_signature()
        while True:
            self.venue.advance_matching_by_one_poll(at=moment)
            current = self._venue_state_signature()
            if current == previous:
                return
            previous = current

    def _venue_state_signature(self) -> tuple[tuple[str, str, int], ...]:
        """Every order the venue holds, as (id, status, filled) — what "nothing changed" means."""
        return tuple(
            (report.broker_order_id, report.status, report.filled_quantity)
            for report in self.venue.fetch_orders(session_date=self.policy.session_date)
        )

    def _apply_fills(self, moment: datetime) -> None:
        """Reconcile against the venue, then fold whatever filled into the positions.

        The reconciler is the only thing allowed to write a fill into the journal — the loop reads
        the journal's view afterwards. A loop that recorded its own fills would be asserting what
        the venue did rather than observing it, which is the half `R.13` says proves nothing.
        """
        assert self._reconciler is not None
        self._reconciler.reconcile(session_date=self.policy.session_date, now=moment)
        for position in self._positions.values():
            entry = self.journal.load_order(position.entry_intent_id)
            if entry is not None:
                filled, average = _filled_and_average(entry)
                if filled != position.filled_quantity:
                    self._record_exposure_delta(position, filled, average, moment)
                position.filled_quantity = filled
                position.average_entry_paise = average
            if not position.exit_intent_ids:
                continue
            exit_orders = [
                order
                for order in (
                    self.journal.load_order(intent_id) for intent_id in position.exit_intent_ids
                )
                if order is not None
            ]
            position.exit_filled_quantity, position.average_exit_paise = _filled_and_average_across(
                exit_orders
            )
            if (
                position.filled_quantity > 0
                and position.exit_filled_quantity >= position.filled_quantity
                and position.closed_at is None
            ):
                position.closed_at = moment

    def _record_exposure_delta(
        self,
        position: OpenPaperPosition,
        filled: int,
        average: Decimal | None,
        moment: datetime,
    ) -> None:
        """Move the session's exposure by what actually filled, never by what was ordered."""
        if average is None:
            return
        added = Decimal(filled - position.filled_quantity) * average / PAISE_PER_RUPEE
        if added == 0:
            return
        self.risk_store.record_exposure_change(
            added,
            session_date=self.policy.session_date,
            trading_symbol=position.instrument.trading_symbol,
            occurred_at=moment,
            reason=f"{position.side} fill on {position.position_key}",
        )

    # --- entries ----------------------------------------------------------------------------

    def _consider_entries(self, moment: datetime) -> None:
        """One decision per instrument, in universe order, until the book cannot fund another.

        **Two passes, and the first one is what makes `L5.31`'s selection correction possible.**
        A proposal that wins a scan of one thousand names is not the same evidence as one that wins
        a scan of two, and the size of that difference was measured at 2.51% of notional over a real
        500-name cross-section. Acting instrument-by-instrument in a single pass never assembles the
        candidate SET, so the gate could only be told the breadth was one — silently zeroing the
        largest of its three floors. The first pass therefore asks every instrument, collects the
        actionable ones with their per-notional scores, and only then acts.
        """
        candidates: list[tuple[PaperInstrument, PaperSignal]] = []
        for instrument in self.instruments:
            if self._has_open_position(instrument.trading_symbol):
                continue
            signal = self.signal_source.signal_for(
                instrument_token=instrument.instrument_token,
                trading_symbol=instrument.trading_symbol,
                at=moment,
            )
            if not signal.is_actionable:
                self._record(moment, instrument, signal, outcome="abstained")
                continue
            candidates.append((instrument, signal))

        scan = tuple(
            signal.expected_move_fraction
            for _instrument, signal in candidates
            if signal.expected_move_fraction is not None
        )
        for instrument, signal in candidates:
            self._act_on(moment, instrument, signal, scan)

    def _act_on(
        self,
        moment: datetime,
        instrument: PaperInstrument,
        signal: PaperSignal,
        scan: tuple[Decimal, ...] = (),
    ) -> None:
        """Size it, gate it, fund it, send it — refusing at the first step that says no."""
        balance = self._ledger_balance(moment)
        try:
            selected = self._horizon_for(instrument, moment)
        except NoHorizonHasEvidenceError as unevidenced:
            self._record(
                moment, instrument, signal, outcome="no_evidenced_horizon", detail=str(unevidenced)
            )
            return
        horizon_bars = selected.horizon_bars if selected is not None else self.policy.horizon_bars
        try:
            inputs = assemble_sizing_inputs(
                instrument_token=instrument.instrument_token,
                trading_symbol=instrument.trading_symbol,
                lot_size=instrument.lot_size,
                deployable_rupees=balance,
                concurrent_position_capacity=self.policy.concurrent_position_capacity,
                horizon_bars=horizon_bars,
                as_of=moment,
                paths=self.store_paths,
            )
            sized = self.sizer.size(inputs)
        except (SizingInputAssemblyError, PositionSizingError) as unsizable:
            self._record(moment, instrument, signal, outcome="unsizable", detail=str(unsizable))
            return

        state = self._session_state(moment)
        if state is None:
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                outcome="no_session_state",
                detail="the session has no equity mark, so no loss can be measured against it",
            )
            return
        # The collar is measured from THIS instrument's own closes. It was the calibration's
        # `mean_captured_sigma` for one afternoon, which is a mean CAPTURE and is legitimately
        # negative for a bucket where the strategy lost — the real-data run threw out of the middle
        # of a decision on the first negative one (`A.110`).
        try:
            collar = realised_move_quantile_from_closes(
                inputs.recent_closes,
                horizon_bars=horizon_bars,
                quantile=self.policy.price_collar_quantile,
            )
        except PriceCollarUnavailableError as uncollared:
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                outcome="no_price_collar",
                detail=str(uncollared),
            )
            return
        limits = derive_limits(
            deployable_rupees=balance,
            traded_value_percentile_rupees=balance,
            realised_move_percentile_fraction=collar,
            segment_margin_fraction=self.policy.segment_margin_fraction,
            registration_threshold_orders_per_second=(
                self.policy.registration_threshold_orders_per_second
            ),
        )
        verdict = self.gate.evaluate(
            sized=sized,
            state=state,
            facts=self.regulatory_facts.facts_for(instrument.trading_symbol, known_by=moment),
            limits=limits,
            daily_loss_limit=self._daily_loss_limit(),
        )
        if not verdict.is_allowed:
            refusal = verdict.first_refusal
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                verdict=verdict,
                outcome="refused_by_gate",
                detail=refusal.describe() if refusal is not None else "",
            )
            return

        side = signal.side
        if side is None:  # unreachable: an actionable signal always names a side
            raise PaperSessionError(
                f"{instrument.trading_symbol} produced an actionable decision with no side"
            )

        card = self._assess_trade_quality(moment, instrument, signal, sized, side, scan)
        if card is not None and card.verdict is not QualityVerdict.ADMIT:
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                verdict=verdict,
                outcome="refused_by_quality_floor",
                detail=card.describe(),
            )
            return

        intent = TradingIntent(
            strategy_identity=self.signal_source.strategy_identity_for(instrument.instrument_token),
            instrument_token=instrument.instrument_token,
            trading_symbol=instrument.trading_symbol,
            segment=self.policy.segment,
            side=side,
            quantity=sized.quantity,
            decided_at=moment,
            reference_price_paise=sized.reference_price_rupees * PAISE_PER_RUPEE,
            expected_edge_bps=inputs.calibration.mean_captured_sigma,
            horizon_minutes=max(
                int((self.policy.decision_step * horizon_bars).total_seconds() // 60), 1
            ),
        )
        position_key = f"{instrument.trading_symbol}-{intent.intent_id[:12]}"
        try:
            self.ledger.commit_to_position(
                sized.notional_rupees,
                position_key=position_key,
                occurred_at=moment,
                reason=(
                    f"{side} {sized.quantity} {instrument.trading_symbol} ({verdict.describe()})"
                ),
            )
        except PaperCapitalError as unfunded:
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                verdict=verdict,
                outcome="unfunded",
                detail=str(unfunded),
            )
            return

        placement = self._place(intent, moment, entry=True)
        if placement.verdict not in (PlacementVerdict.PLACED, PlacementVerdict.ALREADY_PLACED):
            self.ledger.release_position(
                position_key=position_key,
                occurred_at=moment,
                reason=f"order not sent: {placement.reason}",
            )
            self._record(
                moment,
                instrument,
                signal,
                sized=sized,
                verdict=verdict,
                placement=placement,
                outcome=f"placement_{placement.verdict.value}",
                detail=placement.reason,
            )
            return

        self.risk_store.record_order_sent(
            session_date=self.policy.session_date,
            trading_symbol=instrument.trading_symbol,
            occurred_at=moment,
        )
        self._positions[position_key] = OpenPaperPosition(
            position_key=position_key,
            entry_intent_id=intent.intent_id,
            instrument=instrument,
            side=side,
            ordered_quantity=sized.quantity,
            filled_quantity=0,
            average_entry_paise=None,
            committed_rupees=sized.notional_rupees,
            opened_at=moment,
            expires_at=moment + self.policy.decision_step * horizon_bars,
            horizon_bars=horizon_bars,
            stated_win_probability=signal.decision.conviction,
        )
        self._record(
            moment,
            instrument,
            signal,
            sized=sized,
            verdict=verdict,
            placement=placement,
            outcome="placed",
            detail=selected.describe() if selected is not None else "",
        )

    def _horizon_for(self, instrument: PaperInstrument, moment: datetime) -> SelectedHorizon | None:
        """This instrument's own holding time at this instant, or `None` when no selector is set.

        The deviation is measured from the SAME availability-filtered closes the sizer will read,
        so the horizon and the size are decided on one view of what was knowable.
        """
        if self.horizon_selector is None:
            return None
        closes = recent_closes_available_at(
            instrument_token=instrument.instrument_token,
            as_of=moment,
            market_data=self.store_paths.market_data,
        )
        if not closes:
            raise NoHorizonHasEvidenceError(
                f"{instrument.trading_symbol} has no closes available at {moment.isoformat()}, so "
                "its deviation cannot be measured and no horizon can be evidenced"
            )
        deviation, _dispersion = deviation_and_dispersion(closes)
        return self.horizon_selector.horizon_for(
            deviation_sigma=deviation,
            trading_symbol=instrument.trading_symbol,
            as_of=moment.date(),
        )

    def _place(self, intent: TradingIntent, moment: datetime, *, entry: bool) -> PlacementOutcome:
        """Send one order through the real placer, expressed as this segment permits.

        MARKET/MIS/DAY: an intraday cash order that must be certain of going, because the
        alternative to a fill here is a position carried past the close, and `R.01` makes that the
        failure mode rather than a strategy.
        """
        assert self._placer is not None
        expression = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.MARKET,
            validity=OrderValidity.DAY,
            chosen_because=(
                "intraday entry against the recorded book"
                if entry
                else "square-off before the close (`R.01`)"
            ),
        )
        return self._placer.place(intent, expression, now=moment)

    # --- exits ------------------------------------------------------------------------------

    def _close_expired_or_reversed(self, moment: datetime) -> None:
        """Close a position whose edge has expired or whose signal has turned against it.

        The horizon is the calibration's own: the reversion capture was measured over
        `horizon_bars`, so holding past it is holding on an edge nobody measured.
        """
        for position in list(self._positions.values()):
            if position.unexited_quantity <= 0:
                continue
            if moment >= position.expires_at:
                self._square_off(position, moment, reason="horizon expired")
                continue
            signal = self.signal_source.signal_for(
                instrument_token=position.instrument.instrument_token,
                trading_symbol=position.instrument.trading_symbol,
                at=moment,
            )
            if signal.side is not None and signal.side is not position.side:
                self._square_off(
                    position, moment, reason="signal reversed", opposing_signal=signal
                )

    def _square_off_everything(self, moment: datetime, *, reason: str) -> None:
        """The close. Everything filled goes flat, through the same path, on the same book."""
        # Rounds, not one pass: squaring off draws more fills out of the venue, and an entry that
        # was still filling when its exit went in leaves a residual that needs its own exit. The
        # loop stops when nothing is left unexited or when a round changes nothing — never on a
        # chosen number of attempts, and what is left is REPORTED rather than assumed away.
        previous_unexited = -1
        while True:
            unexited = sum(position.unexited_quantity for position in self._positions.values())
            if unexited == 0 or unexited == previous_unexited:
                break
            previous_unexited = unexited
            for position in list(self._positions.values()):
                if position.unexited_quantity > 0:
                    self._square_off(position, moment, reason=reason)
            self._observe_books(moment)
            self._advance_matching(moment)
            self._apply_fills(moment)

    def _square_off(
        self,
        position: OpenPaperPosition,
        moment: datetime,
        *,
        reason: str,
        opposing_signal: PaperSignal | None = None,
    ) -> None:
        """Send the opposite leg for exactly what filled — never for what was ordered."""
        exit_side = TradeLeg.SELL if position.side is TradeLeg.BUY else TradeLeg.BUY
        reference = position.average_entry_paise or Decimal(1)
        quantity = position.unexited_quantity
        if quantity <= 0:
            return
        intent = TradingIntent(
            strategy_identity=f"square_off:{reason}",
            instrument_token=position.instrument.instrument_token,
            trading_symbol=position.instrument.trading_symbol,
            segment=self.policy.segment,
            side=exit_side,
            quantity=quantity,
            decided_at=moment,
            reference_price_paise=reference,
            expected_edge_bps=Decimal(0),
        )
        placement = self._place(intent, moment, entry=False)
        sent = placement.verdict in (PlacementVerdict.PLACED, PlacementVerdict.ALREADY_PLACED)
        # AFTER the verdict, never before. Emitting first recorded `chosen_action="exit_long"` for
        # a square-off the rate gate had just refused — a position that shed nothing, asserting
        # nine times that it chose to exit. That is the `A.29` fiction the record exists to
        # prevent, and it bites hardest at the close, where a synchronised exit wave is exactly
        # when the rate gate refuses.
        self._emit_exit_decision_trace(
            position,
            moment,
            reason=reason,
            exit_side=exit_side,
            sent=sent,
            refusal=None if sent else str(placement.verdict),
            opposing_signal=opposing_signal,
        )
        position.square_off_attempts += 1
        position.close_reason = reason
        if sent:
            position.exit_intent_ids = (*position.exit_intent_ids, intent.intent_id)
            position.exit_ordered_quantity += quantity
            self.risk_store.record_order_sent(
                session_date=self.policy.session_date,
                trading_symbol=position.instrument.trading_symbol,
                occurred_at=moment,
            )

    def _emit_exit_decision_trace(
        self,
        position: OpenPaperPosition,
        moment: datetime,
        *,
        reason: str,
        exit_side: TradeLeg,
        sent: bool,
        refusal: str | None,
        opposing_signal: PaperSignal | None = None,
    ) -> None:
        """`L13.29` for the way OUT — spec `docs/research/259`, gap `M25`.

        Every exit cause funnels through `_square_off`, so the emitter goes here and nowhere else,
        the same chokepoint discipline the entry emitter follows at `_record`. Until this existed
        the store answered "why did it get in?" and "why did it stay out?" and could not answer
        "why did it get out THERE?" — the question a reader asks about a position that lost money.

        The exit cause is recorded as a signed, normalised GATE rather than as prose, so
        `binding_constraint()` ranks it against the others exactly as it does on an entry. The
        `reason` string is the one already carried into the order's `strategy_identity`, so the
        trace and the order agree by construction rather than by a second author writing prose.
        """
        if self.decision_trace_store is None:
            return
        horizon_seconds = Decimal(str((position.expires_at - position.opened_at).total_seconds()))
        remaining_seconds = Decimal(str((position.expires_at - moment).total_seconds()))
        gates = [
            GateEvaluation(
                gate="holding_horizon",
                outcome=(
                    GateOutcome.REFUSED if moment >= position.expires_at else GateOutcome.PASSED
                ),
                margin=remaining_seconds,
                # A zero-length horizon has no scale to normalise against, and the contract refuses
                # a zero threshold rather than dividing by it.
                threshold=horizon_seconds if horizon_seconds != 0 else None,
                detail=(
                    f"held {position.horizon_bars} bar(s) to {position.expires_at:%H:%M:%S}; "
                    f"{remaining_seconds} second(s) remained at the exit"
                ),
            ),
            GateEvaluation(
                gate="halt_latch",
                outcome=(
                    GateOutcome.REFUSED if self._halted_reason else GateOutcome.PASSED
                ),
                margin=None,
                threshold=None,
                detail=self._halted_reason or "the session was not halted",
            ),
        ]
        # A boolean gate that did not fire must not be recorded as the reason. `signal reversed` is
        # the only cause carrying a fresh opposing signal, so the gate is materialised only there —
        # inventing a PASSED alignment gate on a horizon expiry would put a gate in the record that
        # nothing evaluated.
        if reason.startswith("session close") and not self._halted_reason:
            gates.append(
                GateEvaluation(
                    gate="session_clock",
                    outcome=GateOutcome.REFUSED,
                    margin=None,
                    threshold=None,
                    detail=(
                        "the intraday deadline arrived (`R.01`); nothing survives the close, "
                        "whatever the position's own horizon still allowed"
                    ),
                )
            )
        if reason == "signal reversed":
            gates.append(
                GateEvaluation(
                    gate="signal_alignment",
                    outcome=GateOutcome.REFUSED,
                    margin=None,
                    threshold=None,
                    detail=f"a fresh signal opposed the open {position.side.value} position",
                )
            )
        # The order sent is a gate too. Recording only the reasons to WANT out, and never whether
        # getting out was permitted, let the trace assert an exit that the venue or the rate gate
        # had just refused.
        gates.append(
            GateEvaluation(
                gate="exit_order_accepted",
                outcome=GateOutcome.PASSED if sent else GateOutcome.REFUSED,
                margin=None,
                threshold=None,
                detail=(
                    f"the exit order was accepted for {position.unexited_quantity} share(s)"
                    if sent
                    else f"the exit order was NOT sent: {refusal}"
                ),
            )
        )
        # An exit that was refused did not happen, so the record must not say it did. The bot still
        # WANTED out — that is what the gates above say — but the action it took was to keep
        # holding, because nothing left the book.
        action = (
            ("exit_long" if position.side is TradeLeg.BUY else "exit_short")
            if sent
            else self.trace_exit_null_action
        )
        entry_paise = position.average_entry_paise
        # `as_of` is the instant a VALUE became knowable, and both of these move as fills arrive:
        # at `opened_at` the entry order had only just been sent, so the fill price was `None` and
        # the unexited quantity was zero. Stamping them with the open backdated values produced
        # hours later — the mirror image of look-ahead, and one no refusal can catch, since the
        # contract only bounds `as_of` from above.
        inputs = [
            ConsultedInput(
                name="average_entry_paise",
                value="unfilled" if entry_paise is None else f"{entry_paise}",
                source="paper_loop.open_paper_position",
                as_of=moment,
            ),
            ConsultedInput(
                name="unexited_quantity",
                value=str(position.unexited_quantity),
                source="paper_loop.open_paper_position",
                as_of=moment,
            ),
            ConsultedInput(
                name="expires_at",
                value=position.expires_at.isoformat(),
                source="paper_loop.open_paper_position",
                as_of=position.opened_at,
            ),
        ]
        # The one exit cause driven by FRESH evidence recorded none of it: a reversal trace named
        # `signal_alignment` as the binding constraint with nothing in `inputs` describing the
        # signal that reversed — no side, no value, no provenance, no timestamp — leaving the one
        # checkable exit unfalsifiable.
        if opposing_signal is not None and opposing_signal.side is not None:
            inputs.append(
                ConsultedInput(
                    name="opposing_signal_side",
                    value=opposing_signal.side.value,
                    source="strategy.intraday_mean_reversion_engine",
                    as_of=opposing_signal.observed_at,
                )
            )
        try:
            self.decision_trace_store.record(
                DecisionTrace(
                    decided_at=moment,
                    kind=DecisionKind.EXIT,
                    bot_identity=self.trace_bot_identity,
                    instrument_token=position.instrument.instrument_token,
                    trading_symbol=position.instrument.trading_symbol,
                    inputs=tuple(inputs),
                    candidates=(
                        CandidateAction(
                            action=self.trace_exit_null_action,
                            why_considered="keep holding until the horizon or the close",
                        ),
                        CandidateAction(action=action, why_considered=reason),
                    ),
                    permitted_actions=frozenset(
                        {self.trace_exit_null_action, "exit_long", "exit_short"}
                    ),
                    null_action=self.trace_exit_null_action,
                    gates=tuple(gates),
                    chosen_action=action,
                    decision_ordinal=position.square_off_attempts,
                    # An exit is not a conviction-weighted opinion; it is a rule firing. A number
                    # here would be invented, and `None` is the field's honest state.
                    confidence=None,
                    mechanism=f"square_off:{reason} ({exit_side.value})",
                )
            )
        except DecisionTraceError as refusal:
            self._traces_skipped += 1
            self._trace_skip_reasons[str(refusal).split(".")[0][:160]] += 1

    # --- accounting -------------------------------------------------------------------------

    def _account_for_closed_positions(self, moment: datetime) -> tuple[Decimal, Decimal]:
        """Realise every round trip that completed, debit its costs, release its capital."""
        gross = Decimal(0)
        costs = Decimal(0)
        for position in self._positions.values():
            if position.average_entry_paise is None or position.average_exit_paise is None:
                continue
            realised = position.realised_rupees()
            gross += realised
            # A round trip that closes at exactly its entry price realises NOTHING, and the ledger
            # refuses a zero movement for a good reason: an event of zero rupees is a statement
            # about the book rather than a movement in it. Costs are still debited and capital is
            # still released below — a breakeven trade is not a free one. Found on the 2026-08-13
            # replay, where an illiquid scrip entered and exited on the same untouched book
            # (`A.112`).
            if realised > 0:
                self.ledger.record_realised_profit(
                    realised,
                    position_key=position.position_key,
                    occurred_at=moment,
                    reason=f"{position.position_key} closed: {position.close_reason}",
                )
            elif realised < 0:
                self.ledger.record_realised_loss(
                    -realised,
                    position_key=position.position_key,
                    occurred_at=moment,
                    reason=f"{position.position_key} closed: {position.close_reason}",
                )
            cost = self._cost_of(position)
            if cost > 0:
                costs += cost
                self.ledger.record_cost_debit(
                    cost,
                    position_key=position.position_key,
                    occurred_at=moment,
                    reason=f"round-trip charges on {position.position_key}",
                )
            self.risk_store.record_realised_pnl(
                realised - cost,
                session_date=self.policy.session_date,
                trading_symbol=position.instrument.trading_symbol,
                occurred_at=moment,
                reason=f"{position.position_key}: {position.close_reason}",
            )
            self.risk_store.record_exposure_change(
                -position.exposure_rupees,
                session_date=self.policy.session_date,
                trading_symbol=position.instrument.trading_symbol,
                occurred_at=moment,
                reason=f"{position.position_key} closed",
            )
            self.ledger.release_position(
                position_key=position.position_key,
                occurred_at=moment,
                reason=f"{position.position_key} closed: {position.close_reason}",
            )
        return gross, costs

    def _assess_trade_quality(
        self,
        moment: datetime,
        instrument: PaperInstrument,
        signal: PaperSignal,
        sized: SizedPosition,
        side: TradeLeg,
        scan: tuple[Decimal, ...],
    ) -> TradeQualityEvidenceCard | None:
        """`L5.31` on this proposal. `None` when no gate is attached — never a silent ADMIT.

        The distinction matters: a loop running without a gate has not been told these trades are
        fine, it has not asked. The caller acts only on a card that exists and says `ADMIT`, so an
        absent gate leaves behaviour as it was, and an attached one can only ever refuse more.

        The card is recorded whether it admits or refuses. Refusals are the control group `L5.31`
        exists to keep: without them the floor can only ever be told it was right.
        """
        if self.trade_quality_gate is None:
            return None
        fraction = signal.expected_move_fraction
        if fraction is None or fraction <= 0:
            return None
        cost = self._price_entry_round_trip(sized, side)
        if cost is None:
            return None
        priced_signal = PricedSignal(
            instrument_token=instrument.instrument_token,
            trading_symbol=instrument.trading_symbol,
            segment=self.policy.segment,
            side=side,
            decided_at=moment,
            reference_price_paise=sized.reference_price_rupees * PAISE_PER_RUPEE,
            expected_edge_bps=fraction * BASIS_POINTS_PER_UNIT,
            proposed_quantity=sized.quantity,
            edge_basis=EdgeBasis.CALIBRATED_MODEL,
            source=self.trace_bot_identity,
            conviction=Decimal(str(signal.decision.conviction)),
        )
        card = self.trade_quality_gate.assess(
            QualityAssessmentRequest(
                signal=priced_signal,
                bot_identity=self.trace_bot_identity,
                trading_segment=self.trade_quality_segment,
                assessed_at=moment,
                round_trip_cost_rupees=cost,
                candidate_expectancy_fractions=scan or (fraction,),
                stated_win_probability=signal.decision.conviction,
            )
        )
        if self.trade_quality_store is not None:
            self.trade_quality_store.record(card)
        return card

    def _price_entry_round_trip(self, sized: SizedPosition, side: TradeLeg) -> Decimal | None:
        """What this proposal would cost to open and close, priced BEFORE it is sent.

        `_cost_of` prices a round trip after the fact from the fills it got; this prices the one
        being proposed, at the reference price, because a floor applied after the trade is not a
        floor. `None` — never zero — when no pricer is attached or the trade cannot be priced: a
        zero cost floor licenses exactly the marginal trade `D.01` calls decisively negative.
        """
        if self.cost_pricer is None:
            return None
        reference_paise = sized.reference_price_rupees * PAISE_PER_RUPEE
        if reference_paise <= 0 or sized.quantity <= 0:
            return None
        try:
            priced = self.cost_pricer.price_round_trip(
                TradeSpecification(
                    segment=self.policy.segment,
                    quantity=sized.quantity,
                    entry_price_paise=reference_paise,
                    exit_price_paise=reference_paise,
                    trade_date=self.policy.session_date,
                    is_short_first=side is TradeLeg.SELL,
                )
            )
        except TransactionCostError:
            return None
        total = getattr(priced, "total_rupees", None)
        return None if total is None else Decimal(total)

    def _cost_of(self, position: OpenPaperPosition) -> Decimal:
        """What `F01` says this round trip cost, or zero with the absence recorded.

        Zero is NOT a claim that the trade was free: without a pricer the loop has no cost model
        attached, and `PaperSessionReport.costs_rupees` of zero next to a non-zero gross is the
        signal that the comparison graduation reads has not been made.
        """
        if self.cost_pricer is None:
            return Decimal(0)
        if position.average_entry_paise is None or position.average_exit_paise is None:
            return Decimal(0)
        quantity = min(position.filled_quantity, position.exit_filled_quantity)
        if quantity <= 0:
            return Decimal(0)
        try:
            priced = self.cost_pricer.price_round_trip(
                TradeSpecification(
                    segment=self.policy.segment,
                    quantity=quantity,
                    entry_price_paise=position.average_entry_paise,
                    exit_price_paise=position.average_exit_paise,
                    trade_date=self.policy.session_date,
                    is_short_first=position.side is TradeLeg.SELL,
                )
            )
        except TransactionCostError:
            return Decimal(0)
        total = getattr(priced, "total_rupees", None)
        return Decimal(total) if total is not None else Decimal(0)

    # --- session state ----------------------------------------------------------------------

    def _session_is_halted(self, moment: datetime) -> bool:
        """A tripped latch stops the day. The loop never clears one — that is an operator act."""
        state = self._session_state(moment)
        if state is None or not state.is_halted:
            return False
        self._halted_reason = ", ".join(latch.value for latch in state.tripped_latches)
        return True

    def _session_state(self, moment: datetime) -> SessionRiskState | None:
        try:
            return self.risk_store.state_for(session_date=self.policy.session_date, now=moment)
        except SessionRiskStateError:
            return None

    def _daily_loss_limit(self) -> DailyLossLimit:
        return self.risk_store.daily_loss_limit(as_of=self.policy.session_date)

    def _ledger_balance(self, moment: datetime) -> Decimal:
        """Free capital, replayed from the ledger's own events rather than read from a cache."""
        snapshot = self.ledger.snapshot(measured_at=moment, live_ceiling=self.capital)
        return snapshot.free_rupees

    def _has_open_position(self, trading_symbol: str) -> bool:
        return any(
            position.instrument.trading_symbol == trading_symbol and position.is_open
            for position in self._positions.values()
        )

    def _record(
        self,
        moment: datetime,
        instrument: PaperInstrument,
        signal: PaperSignal,
        *,
        sized: SizedPosition | None = None,
        verdict: RiskGateVerdict | None = None,
        placement: PlacementOutcome | None = None,
        outcome: str,
        detail: str = "",
    ) -> None:
        self._decisions.append(
            PaperDecisionRecord(
                at=moment,
                trading_symbol=instrument.trading_symbol,
                signal=signal,
                sized=sized,
                verdict=verdict,
                placement=placement,
                outcome=outcome,
                detail=detail,
            )
        )
        self._emit_decision_trace(
            moment, instrument, signal, sized=sized, verdict=verdict, outcome=outcome
        )

    def _emit_decision_trace(
        self,
        moment: datetime,
        instrument: PaperInstrument,
        signal: PaperSignal,
        *,
        sized: SizedPosition | None,
        verdict: RiskGateVerdict | None,
        outcome: str,
    ) -> None:
        """Record WHY, at the instant, alongside the what (`L13.29`).

        Everything here is already in hand at this point in the loop — the deviation the engine
        measured, the regime belief it consulted, the gate verdicts it received. That is the whole
        reason the emission lives here rather than in a later pass over the report.

        A trace that cannot be built is skipped rather than allowed to fail the session: reasoning
        is evidence about a decision, and losing the evidence must not also lose the trade.
        """
        if self.decision_trace_store is None:
            return
        gates: list[GateEvaluation] = []
        if sized is not None and sized.refusal_reason is None:
            # The sizer already computes WHICH bound bound and the rupee value of each. Emitting
            # them as gates is what finally gives the trace's unit-normalisation something to
            # compare: these margins are rupees while `deviation_band` is in sigma, and until now
            # every real trace carried at most one comparable gate, so the normalisation had never
            # been exercised on production data (`B17`).
            for bound_name, bound_rupees in (
                ("risk_budget", sized.risk_justified_notional_rupees),
                ("volatility_target", sized.volatility_target_notional_rupees),
                ("kelly_cap", sized.kelly_notional_rupees),
                ("concentration_cap", sized.concentration_cap_rupees),
                ("deployable_capital", sized.deployable_rupees),
            ):
                if bound_rupees <= 0:
                    continue  # an absent bound is not a bound of zero
                gates.append(
                    GateEvaluation(
                        gate=f"sizing_{bound_name}",
                        outcome=GateOutcome.PASSED,
                        # Signed headroom: how much notional was left under this bound.
                        margin=bound_rupees - sized.notional_rupees,
                        threshold=bound_rupees,
                        detail=(
                            f"notional Rs {sized.notional_rupees:,.2f} against a {bound_name} "
                            f"of Rs {bound_rupees:,.2f}"
                            + (" — THE BINDING BOUND" if sized.binding_bound == bound_name else "")
                        ),
                    )
                    if bound_rupees - sized.notional_rupees >= 0
                    else GateEvaluation(
                        gate=f"sizing_{bound_name}",
                        outcome=GateOutcome.REFUSED,
                        margin=bound_rupees - sized.notional_rupees,
                        threshold=bound_rupees,
                        detail=(
                            f"notional Rs {sized.notional_rupees:,.2f} exceeds {bound_name} "
                            f"of Rs {bound_rupees:,.2f}"
                        ),
                    )
                )

        if verdict is not None:
            # `RiskGateVerdict` has NO `.verdict` attribute. The first version read
            # `getattr(verdict, "verdict", verdict)` and stringified the whole dataclass repr, so
            # this gate could never register as refusing — which made 380 traces from the real
            # 2026-08-17 session blame `deviation_band` for abstains the order-rate limiter had
            # actually caused. `is_allowed` is the real predicate.
            if verdict.is_allowed:
                gates.append(
                    GateEvaluation(
                        gate="pre_trade_risk_gate",
                        outcome=GateOutcome.PASSED,
                        margin=None,
                        threshold=None,
                        detail="no tier refused",
                    )
                )
            else:
                # One gate per refusing RULE, not one collapsed boolean. Which rule refused is the
                # thing a reader wants, and collapsing five tiers into "the risk gate said no"
                # discards it.
                gates.extend(
                    GateEvaluation(
                        gate=f"risk_{refusal.rule.lower()}",
                        outcome=GateOutcome.REFUSED,
                        margin=None,
                        threshold=None,
                        detail=f"{refusal.tier}: {refusal.detail}",
                    )
                    for refusal in verdict.refusals
                )
        deviation = signal.decision.deviation
        # `if band` treated a band of exactly 0.0 as absent and silently discarded a real margin.
        band = getattr(signal.decision, "deviation_band", None)
        actionable = signal.is_actionable
        margin = None
        if band is not None and float(band) != 0.0:
            # Signed so it agrees with the outcome: positive when the deviation cleared its band.
            margin = Decimal(str(abs(deviation) - float(band)))
            if actionable and margin < 0:
                margin = None  # incoherent with the verdict; report as boolean rather than lie
            elif not actionable and margin > 0:
                margin = None
        gates.append(
            GateEvaluation(
                gate="deviation_band",
                outcome=GateOutcome.PASSED if actionable else GateOutcome.REFUSED,
                margin=margin,
                threshold=Decimal(str(band)) if margin is not None else None,
                detail=signal.decision.reason,
            )
        )
        # The vocabulary comes from the deciding engine's OWN action enum, read off the decision
        # itself — not a list written out here. So an action this bot cannot take is unrecordable,
        # a newly added one is covered without an edit, and the six segment bots each get their own
        # vocabulary through the same runner. The live store agreed with the enum: 21,270 traces
        # used exactly {abstain, enter_long, enter_short}.
        # The vocabulary comes from the deciding engine's OWN action enum, read off the decision
        # itself — not a list written out here — so an action this bot cannot take is unrecordable,
        # a newly added one is covered without an edit, and each segment bot gets its own
        # vocabulary through the same runner.
        #
        # Defensive because the derivation must never be able to cost a trade: a decision whose
        # action is a plain string, or an enum over non-string values, would otherwise raise here,
        # OUTSIDE the guard below and AFTER the order was already recorded as sent — losing the
        # trade to save the evidence, which inverts this method's whole contract. An underivable
        # vocabulary means unchecked, and says so, rather than raising.
        action_type = type(signal.decision.action)
        try:
            permitted: frozenset[str] | None = frozenset(
                member.value for member in action_type if isinstance(member.value, str)
            ) or None
        except TypeError:
            permitted = None
        null_action = self.trace_null_action
        candidates = [CandidateAction(action=null_action, why_considered="always available")]
        if signal.side is not None:
            candidates.append(
                CandidateAction(
                    action=signal.decision.action.value,
                    why_considered=signal.decision.reason,
                )
            )
        chosen = signal.decision.action.value if outcome == "placed" else null_action
        # The first version back-filled `chosen` into `candidates` when it was missing, which made
        # the contract's load-bearing "chose something it never considered" refusal unreachable at
        # the only place traces are emitted. The refusal now fires and the trace is skipped.
        conviction = signal.decision.conviction
        confidence = (
            min(max(float(conviction), 0.0), 1.0) if math.isfinite(float(conviction)) else None
        )
        try:
            self.decision_trace_store.record(
                DecisionTrace(
                    decided_at=moment,
                    bot_identity=self.trace_bot_identity,
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    inputs=(
                        ConsultedInput(
                            name="deviation_sigma",
                            value=f"{deviation:.4f}",
                            source="strategy.intraday_mean_reversion_engine",
                            as_of=signal.observed_at,
                        ),
                        ConsultedInput(
                            name="regime_disagreement",
                            value=f"{signal.belief.disagreement:.4f}",
                            source="regime.soft_regime_weighting_brain",
                            as_of=signal.belief.observed_at,
                        ),
                    ),
                    candidates=tuple(candidates),
                    permitted_actions=permitted,
                    null_action=null_action,
                    gates=tuple(gates),
                    chosen_action=chosen,
                    confidence=confidence,
                    mechanism=signal.decision.reason,
                )
            )
        except DecisionTraceError as refusal:
            # Evidence about a decision must not be able to lose the decision — but a loss nobody
            # counts is indistinguishable from a quiet session, so it is counted here and reported.
            self._traces_skipped += 1
            # A bare count made every distinct refusal — vocabulary, look-ahead, a naive timestamp,
            # a duplicate instant — arrive as the same unlabelled integer, so "counted rather than
            # swallowed" bought a number nobody could act on. The reasons are kept, deduplicated,
            # so the report says WHICH contract the emitter is failing.
            self._trace_skip_reasons[str(refusal).split(".")[0][:160]] += 1
            return


def _filled_and_average(order: OrderRecord) -> tuple[int, Decimal | None]:
    """Quantity filled and the volume-weighted price it filled at, from the journal's own fills."""
    return _filled_and_average_across([order])


def _filled_and_average_across(orders: Sequence[OrderRecord]) -> tuple[int, Decimal | None]:
    """The same fold over several legs — a position exited in pieces has one average, not many."""
    fills = [
        fill for order in orders for fill in order.fills if not getattr(fill, "superseded_by", "")
    ]
    quantity = sum(fill.quantity for fill in fills)
    if quantity <= 0:
        return 0, None
    value = sum(Decimal(fill.quantity) * fill.price_paise for fill in fills)
    return quantity, value / Decimal(quantity)


def limits_for(
    *,
    deployable_rupees: Decimal,
    realised_move_percentile_fraction: Decimal,
    policy: PaperSessionPolicy,
) -> DerivedLimits:
    """The gate's tier-2 limits for this policy — exported so a surface shows the same numbers."""
    return derive_limits(
        deployable_rupees=deployable_rupees,
        traded_value_percentile_rupees=deployable_rupees,
        realised_move_percentile_fraction=realised_move_percentile_fraction,
        segment_margin_fraction=policy.segment_margin_fraction,
        registration_threshold_orders_per_second=(policy.registration_threshold_orders_per_second),
    )
