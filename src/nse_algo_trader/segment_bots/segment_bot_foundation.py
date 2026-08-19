"""The shared machinery all six segment bots are built on — `L5.25`, spec `docs/research/263`.

**Why a foundation rather than six independent bots.** `A.130` settled that the decision path is
segment-blind and built once, and that what differs per segment is the instrument facts —
denominator, lot and tick, expiry, settlement, cost row, Greeks, venue calendar and carry rule. The
conformance suite (`L5.29`) already enforces the protocol; this is what stops six authors from each
inventing their own answer to the same four questions:

1. **carried state** — a bot that only learns when it trades learns from a sample its own past
   decisions selected, so `observe` advances state on every instrument every session whether or not
   anything is proposed;
2. **maturity** — read from the bot's OWN closed record through `L5.30`'s ladder, never asserted,
   and `GRADUATED` unreachable by construction (`R.22`);
3. **relevance** — a bounded `[0, 1]` score the supervisor can compare across six bots, derived from
   the bot's own state rather than stated;
4. **cadence** — whether this bot decides intraday or once per session, which today is a property of
   what data exists for its segment and NOT of the algorithm (`R.04`, decision `A.141`).

**The cadence field is the honest part of this slice.** Only cash has intraday data: 1,471,990
five-minute bars and a live depth tape over 1,845 names. Every other segment has daily bhavcopy
only. So the option and future bots decide once per session on real closes and say so on every
signal they emit, and when F&O depth capture starts filling (`A.142`) they lift to intraday with no
algorithm changing. A bot that quietly presented a daily decision as an intraday one would make its
own track record unreadable.

**No I/O anywhere.** A bot opens no socket and no database; the spine hands it a `SegmentBotContext`
and it answers. That is what makes six bots safe to build concurrently and their decisions
replayable.
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotMaturity,
    BotMaturityRung,
    SegmentBotContext,
    SegmentBotProtocolError,
    SegmentRelevance,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import (
    SegmentInstrumentFacts,
    TradingSegment,
    instrument_facts_for,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

PAISE_PER_RUPEE = Decimal("100")

BASIS_POINTS_PER_UNIT = Decimal("10000")
"""One basis point is one ten-thousandth. A definition, not a parameter."""

OBSERVATIONS_NEEDED_FOR_A_DISPERSION = 3
"""A standard deviation needs three points to be anything other than the gap between two.

Two observations always produce a dispersion, and it is always exactly half their difference — a
number with no information in it, which then divides a deviation and manufactures a signal.
"""


class DecisionCadence(StrEnum):
    """How often this bot can honestly decide, given the data its segment actually has.

    Carried on every proposal and reported by `relevance`, so a track record can never mix the two.
    `A.141`: this gates ACTIVATION cadence only — the algorithm is identical either way (`R.04`).
    """

    INTRADAY = "intraday"
    ONCE_PER_SESSION = "once_per_session"


@dataclass(slots=True)
class InstrumentObservationState:
    """What a bot remembers about one instrument, and nothing more.

    A bounded deque rather than a growing list on purpose: this state is carried across a session of
    thousands of instruments, and an unbounded window would make the bot's memory a function of how
    long the process has been up rather than of how much history the estimate needs.
    """

    window: deque[float]
    last_price_paise: Decimal | None = None
    observations_seen: int = 0

    def observe(self, price_paise: Decimal) -> None:
        if price_paise <= 0:
            return
        self.last_price_paise = price_paise
        self.window.append(float(price_paise / PAISE_PER_RUPEE))
        self.observations_seen += 1

    @property
    def is_mature(self) -> bool:
        return len(self.window) >= OBSERVATIONS_NEEDED_FOR_A_DISPERSION

    def mean(self) -> float | None:
        return statistics.fmean(self.window) if self.is_mature else None

    def dispersion(self) -> float | None:
        """Sample standard deviation of the window, or `None` when it cannot be one."""
        if not self.is_mature:
            return None
        spread = statistics.stdev(self.window)
        return spread if math.isfinite(spread) and spread > 0.0 else None

    def deviation_sigma(self) -> float | None:
        """How far the latest price sits from its own window mean, in that window's own sigma.

        The one quantity every one of the six bots ultimately reasons about, whatever it calls it:
        a standardised departure from a self-measured centre. Self-calibrating by construction
        (`R.03`) — nothing here is a rupee threshold, so it transfers unchanged from a Rs 12 penny
        stock to a Rs 24,800 index.
        """
        centre = self.mean()
        spread = self.dispersion()
        if centre is None or spread is None or self.last_price_paise is None:
            return None
        latest = float(self.last_price_paise / PAISE_PER_RUPEE)
        return (latest - centre) / spread


@dataclass(frozen=True, slots=True)
class BotTrackRecordSummary:
    """What a bot's own closed record says — supplied by the spine, never read by the bot.

    The bot receives this rather than fetching it because `L5.29` forbids I/O on the decision path,
    and because the same summary has to be comparable across all six.
    """

    closed_trades: int
    sessions: int
    posterior_above_break_even: float
    rung: BotMaturityRung

    def __post_init__(self) -> None:
        if self.closed_trades < 0 or self.sessions < 0:
            raise SegmentBotProtocolError(
                f"a track record cannot hold {self.closed_trades} trades over {self.sessions} "
                f"sessions; a negative count is a defect in the store, not a cautious bot"
            )
        if not 0.0 <= self.posterior_above_break_even <= 1.0:
            raise SegmentBotProtocolError(
                f"posterior {self.posterior_above_break_even} is not a probability; the ladder "
                f"compares six of these directly"
            )

    @classmethod
    def cold_start(cls) -> BotTrackRecordSummary:
        """A bot that has never closed a trade. The honest default, and the lowest rung."""
        return cls(
            closed_trades=0,
            sessions=0,
            posterior_above_break_even=0.0,
            rung=BotMaturityRung.COLD_START,
        )


@dataclass(slots=True)
class SegmentBotFoundation(ABC):
    """Everything a segment bot shares. Subclasses supply the strategy and the relevance model.

    Deliberately a dataclass with a real `__post_init__` rather than a bare base class: the segment
    facts are looked up once at construction and frozen onto the bot, so a bot cannot be asked
    mid-session what its own settlement style is and get a different answer than it started with.
    """

    trading_segment_value: TradingSegment
    cadence: DecisionCadence
    window_size: int
    track_record: BotTrackRecordSummary = field(default_factory=BotTrackRecordSummary.cold_start)
    _state: dict[int, InstrumentObservationState] = field(default_factory=dict, init=False)
    _facts: SegmentInstrumentFacts = field(init=False)
    _sessions_observed: set[date] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.window_size < OBSERVATIONS_NEEDED_FOR_A_DISPERSION:
            raise SegmentBotProtocolError(
                f"a window of {self.window_size} cannot support a dispersion; "
                f"{OBSERVATIONS_NEEDED_FOR_A_DISPERSION} is the minimum at which a standard "
                f"deviation carries information rather than restating a difference"
            )
        self._facts = instrument_facts_for(self.trading_segment_value)

    # ---- protocol surface -------------------------------------------------------------

    @property
    def trading_segment(self) -> TradingSegment:
        return self.trading_segment_value

    @property
    @abstractmethod
    def bot_identity(self) -> str:
        """Stable and self-describing (`R.14`) — the key its track record is filed under."""

    @property
    def instrument_facts(self) -> SegmentInstrumentFacts:
        """The regulatory facts of this bot's segment, frozen at construction."""
        return self._facts

    def observe(self, context: SegmentBotContext) -> None:
        """Advance carried state on every instrument in the universe, traded or not.

        Every instrument, not only the ones that produced a signal last time: a bot that updates
        only what it acted on is fitting a model to a sample it selected itself, which is the
        cleanest way to build something that confirms its own past decisions.
        """
        self._sessions_observed.add(context.decision_instant.date())
        for instrument in context.tradeable_universe:
            price = self._observable_price_paise(instrument, context)
            if price is None:
                continue
            state = self._state.get(instrument.instrument_token)
            if state is None:
                state = InstrumentObservationState(window=deque(maxlen=self.window_size))
                self._state[instrument.instrument_token] = state
            state.observe(price)

    def maturity(self) -> BotMaturity:
        """Read off the bot's own closed record. Never asserted, and never `GRADUATED` (`R.22`)."""
        return BotMaturity(
            rung=self.track_record.rung,
            closed_trades_observed=self.track_record.closed_trades,
            evidence=(
                f"{self.track_record.closed_trades} closed trade(s) over "
                f"{self.track_record.sessions} session(s), P(expectancy>0)="
                f"{self.track_record.posterior_above_break_even:.3f}; deciding "
                f"{self.cadence.value}"
            ),
        )

    def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
        """How applicable this bot is right now, in `[0, 1]`, derived from its own state.

        Two factors, multiplied because both are necessary and neither is sufficient: how much of
        the universe this bot has enough history to say anything about, and how strong its
        strategy's own conditions currently are. A bot with a rich universe and no signal is as
        inapplicable as one with a signal it cannot support.
        """
        mature = sum(1 for state in self._state.values() if state.is_mature)
        universe = max(len(context.tradeable_universe), 1)
        coverage = min(mature / universe, 1.0)
        strength = self._strategy_applicability(context)
        applicability = max(0.0, min(coverage * strength, 1.0))
        return SegmentRelevance(
            applicability=applicability,
            reason=(
                f"{mature} of {universe} instruments carry enough history for a dispersion "
                f"(coverage {coverage:.3f}); the strategy's own conditions score {strength:.3f}; "
                f"cadence {self.cadence.value}"
            ),
        )

    @abstractmethod
    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        """This bot's decisions, already priced. May be empty — 'nothing today' is a real answer."""

    # ---- shared helpers for subclasses ------------------------------------------------

    @abstractmethod
    def _strategy_applicability(self, context: SegmentBotContext) -> float:
        """How strong this strategy's own preconditions are right now, in `[0, 1]`."""

    def _observable_price_paise(
        self, instrument: TradeableInstrument, context: SegmentBotContext
    ) -> Decimal | None:
        """The price this bot learns from, read out of the context's carried memory.

        `SegmentBotContext.carried_memory` is the spine's channel for whatever the segment's data
        source produced — a five-minute close for cash, a settlement close for a future, a premium
        for an option. Keyed by instrument token so one map serves a universe of thousands.
        """
        prices = context.carried_memory.get("last_price_paise_by_token")
        if not isinstance(prices, Mapping):
            return None
        raw = prices.get(instrument.instrument_token)
        if raw is None:
            return None
        try:
            price = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            return None
        return price if price > 0 else None

    def state_for(self, instrument_token: int) -> InstrumentObservationState | None:
        """Carried state for one instrument, for the spine's surfaces and for tests."""
        return self._state.get(instrument_token)

    @property
    def instruments_tracked(self) -> int:
        return len(self._state)

    @property
    def instruments_mature(self) -> int:
        return sum(1 for state in self._state.values() if state.is_mature)

    def cross_sectional_cut(self, deviations: Mapping[int, float]) -> float | None:
        """The dispersion threshold this session, measured ACROSS the universe rather than chosen.

        `R.03` in its sharpest form. A fixed "two sigma" rule proposes nothing on a quiet day and
        the entire universe on a violent one, because the number of names beyond two sigma is a
        property of the day and not of the opportunity. Taking the cut from the cross-section's own
        dispersion keeps the proposal count stable across regimes without anybody choosing a count.

        Returns `None` when the cross-section is too thin to have a dispersion of its own, which is
        a refusal to act rather than a permissive default.
        """
        magnitudes = [abs(value) for value in deviations.values() if math.isfinite(value)]
        if len(magnitudes) < OBSERVATIONS_NEEDED_FOR_A_DISPERSION:
            return None
        centre = statistics.fmean(magnitudes)
        spread = statistics.stdev(magnitudes)
        if not math.isfinite(spread) or spread <= 0.0:
            return None
        return centre + spread

    def build_signal(
        self,
        *,
        instrument: TradeableInstrument,
        context: SegmentBotContext,
        side: TradeLeg,
        reference_price_paise: Decimal,
        expected_edge_bps: Decimal,
        conviction: Decimal,
        edge_basis: EdgeBasis,
        horizon_minutes: int | None = None,
    ) -> PricedSignal | None:
        """Assemble one proposal with this segment's own instrument facts attached.

        Returns `None` rather than raising when the instrument cannot carry a legal order, because a
        universe of thousands will contain some that cannot and one bad row must not take the
        session down. The refusals are the ones `PricedSignal` would raise on anyway — this catches
        them at the point where the bot still knows which instrument it was.
        """
        if reference_price_paise <= 0 or expected_edge_bps <= 0:
            return None
        if self._facts.strike_required and instrument.strike_paise is None:
            return None
        quantity = instrument.lot_size
        if quantity <= 0:
            return None
        try:
            return PricedSignal(
                instrument_token=instrument.instrument_token,
                trading_symbol=instrument.trading_symbol,
                # Intraday by default (`R.01`); the carry rule is the segment's own fact and only a
                # supervisor promotion can change it, which no bot can perform.
                segment=self._facts.chargeable_segment_when(carried_overnight=False),
                side=side,
                decided_at=context.decision_instant,
                reference_price_paise=reference_price_paise,
                expected_edge_bps=expected_edge_bps,
                proposed_quantity=quantity,
                edge_basis=edge_basis,
                # EXACTLY the bot identity, nothing appended. The conformance suite's
                # `signal-is-attributable-to-its-bot` check requires equality, and it is right to:
                # the source is the key a track record is filed under, and a decorated one files
                # the same bot's trades under two names the moment the decoration changes. The
                # cadence travels on the bot (`bot.cadence`) and is recorded by the spine on the
                # decision trace, which is where a per-decision fact belongs. Caught by the suite
                # on the first run of all six bots, having been written the other way.
                source=self.bot_identity,
                conviction=conviction,
                strike_paise=instrument.strike_paise,
                horizon_minutes=horizon_minutes,
            )
        except Exception:  # noqa: BLE001 — one unorderable instrument is a skip, never a session kill
            return None

    @staticmethod
    def deviations_by_token(
        states: Mapping[int, InstrumentObservationState],
    ) -> dict[int, float]:
        """Every instrument's standardised departure, for the cross-sectional cut."""
        measured: dict[int, float] = {}
        for token, state in states.items():
            deviation = state.deviation_sigma()
            if deviation is not None and math.isfinite(deviation):
                measured[token] = deviation
        return measured

    @property
    def observation_states(self) -> Mapping[int, InstrumentObservationState]:
        return self._state


def group_instruments_by_underlying(
    universe: Sequence[TradeableInstrument],
) -> dict[str, list[TradeableInstrument]]:
    """Group a derivative universe by its underlying symbol.

    Derivative trading symbols carry the underlying as their leading alphabetic run — `NIFTY26AUG`,
    `RELIANCE26AUG2800CE` — so the underlying is recoverable without a second lookup. Extracted here
    rather than in each bot because three of the six need exactly this and would otherwise each
    write it slightly differently.
    """
    grouped: dict[str, list[TradeableInstrument]] = defaultdict(list)
    for instrument in universe:
        symbol = instrument.trading_symbol.strip().upper()
        underlying = ""
        for character in symbol:
            if character.isalpha():
                underlying += character
            else:
                break
        grouped[underlying or symbol].append(instrument)
    return dict(grouped)
