"""`L10.01` — the loop that never stops. Spec `docs/research/265`, todo `4.9`, blocker `B33`.

**What was actually missing, in the task's own words:** *"only the scheduler and the multi-day state
that carries between sessions, not the session itself."* `F04` already produces a whole trading day
(`4.10`, real-data passed on 2,882 instruments and 218,936 decisions). Nothing ran it between the
daily timer's two firings, which is why the operator saw *"the prices are stuck"* during an open
market — measured at 13:06 IST on 2026-08-18 with the depth capture **active**, **7,135,786 ticks**
over **1,845 tokens** already on disk that day, and the newest tick **eleven seconds old**. The data
was arriving the whole time. Nothing was reading it.

**Three things this owns, and they are the three that were missing:**

1. **Carried state.** The bots live for the life of this object, not for the life of a request. That
   is what makes a rolling statistic possible at all: `/bots` reports 0.0% universe readiness on
   every render precisely because one page load is one observation and a dispersion needs three.
2. **Phase, from the exchange calendar.** Not a weekday check — `NseTradingSessionCalendar` is this
   project's sourced authority, and it was cross-checked against `pandas_market_calendars` 5.4.0 on
   the real dates 2026-08-10..20: **nine sessions, exact agreement**, with the library independently
   confirming the 09:15/15:30 boundaries this module carries.
3. **A liveness record the dashboard READS.** `R.08`: a loop whose only evidence is that a process
   is running is a loop nobody can audit. Every iteration appends what it saw and what it decided,
   so a stalled loop is visibly stalled rather than merely quiet.

**`step` never reads the wall clock.** It takes the instant. That is what makes a whole session
replayable through it in milliseconds in a test while production passes `datetime.now`; `run_until`
is the only thing that sleeps. The same discipline `ReplaySessionClock` already imposes.

**It never raises.** A scheduler that dies on one bad iteration is worse than none: the failure is
recorded on the iteration and the loop continues. That is not defensive coding, it is the whole
point of a supervisor — `A.143` is what a silent stop costs.
"""

from __future__ import annotations

import sqlite3
import time as wall_clock
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import duckdb

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.market_depth.live_tick_to_bar_aggregator import (
    PAISE_PER_RUPEE,
    CompletedBar,
    LiveTickToBarAggregator,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar
from nse_algo_trader.paper_loop.live_paper_book import BookMark, LivePaperBook
from nse_algo_trader.paper_loop.paper_session_signal_source import (
    AvailableBar,
    InstrumentSignalState,
)
from nse_algo_trader.paper_loop.walk_forward_archive_replay import (
    WalkForwardArchiveReplay,
    WalkForwardProgress,
)
from nse_algo_trader.portfolio.portfolio_proposal_supervisor import (
    BotProposal,
    PortfolioPlan,
    PortfolioProposalSupervisor,
)
from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)
from nse_algo_trader.regime.session_phase_regime_classifier import SessionPhaseRegimeClassifier
from nse_algo_trader.regime.soft_regime_weighting_brain import SoftRegimeWeightingBrain
from nse_algo_trader.regime.trend_strength_regime_classifier import TrendStrengthRegimeClassifier
from nse_algo_trader.regime.volatility_regime_classifier import VolatilityRegimeClassifier
from nse_algo_trader.replay_session_clock import IST, session_for
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    MINIMUM_REGIME_AGREEMENT,
    MINIMUM_REGIME_CONCENTRATION,
)
from nse_algo_trader.segment_bots.segment_bot_foundation import SegmentBotFoundation
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.strategy.intraday_mean_reversion_engine import IntradayMeanReversionEngine

DEFAULT_LIVENESS_PATH = Path("~/.nse_algo_trader/scheduler_liveness.sqlite3").expanduser()

TICKS_READ_PER_ITERATION = 400_000
"""How many ticks one iteration folds into bars.

Bounded rather than unbounded: the live tape carried **8,534,670 ticks by 13:54 IST** on
2026-08-18, and a loop starting mid-session must catch up over several iterations instead of
pulling the whole day into memory at once. Sized from that measurement — roughly twenty iterations
to absorb a full session, which is under two hours at the five-minute cadence and under a minute
when the loop is merely behind.
"""

ARMED_CLASSIFIERS = ("trend_strength", "volatility", "session_phase")
"""Which classifiers get real influence over a decision.

**Deliberately the SAME three `scripts/verify_paper_session_on_real_data.py` already arms**, not a
set chosen here. `A.08` made arming a flag flip rather than a code change precisely so that giving a
classifier influence is a decision somebody makes rather than a side effect of importing it — and
this set is the one the daily paper session has been running armed, real-data verified on 2,882
instruments and 218,936 decisions (`4.10`).

Matching it rather than inventing one matters for a reason beyond consistency: two armed sets would
mean the live loop and the daily session form DIFFERENT beliefs from the same bars, and their track
records would then be measuring two strategies while being filed under one name.

Left unarmed, `weight_for` returns 0.0 for every opinion and the brain returns honest ignorance —
which is exactly what the loop did on its first live run, and why it proposed nothing.
"""

BARS_BEFORE_THE_PANEL_IS_ASKED = 3
"""Bars an instrument needs before its opinions are collected at all.

Not a maturity threshold — the classifiers carry their OWN maturity and the brain down-weights an
immature opinion rather than the classifier degrading itself (`R.04`). This is the weaker statement
that a panel asked after one bar has nothing to disagree about, and three is the minimum at which a
range and a change both exist.
"""

SQUARE_OFF_WINDOW = timedelta(minutes=15)
"""How long before the close the loop stops opening and starts flattening.

`R.01` makes square-off the failure mode, so this is a WINDOW rather than an instant: a single
square-off attempt at 15:29:59 has one chance to work, and the one thing this project knows about
its own scheduled code is that the single-chance path is the one that silently does not run
(`A.129`, `A.143`, `O.112`). Fifteen minutes is the operator's stated risk tolerance for how long
flattening may take, and it is the only number in this module that is a policy rather than a
measurement.
"""


def _as_available_bar(bar: CompletedBar) -> AvailableBar:
    """Cross a closed live bar into the shape the verified panel already consumes.

    Converted EXPLICITLY rather than relied on by duck typing, and mypy was right to insist. The
    conversion is where `availability_time` is asserted — the bar's own close, never the instant it
    was built — so the one field that decides whether this is look-ahead is set in a single place a
    reader can find.
    """
    return AvailableBar(
        bar_timestamp=bar.bar_timestamp,
        open_price=bar.open_price,
        high_price=bar.high_price,
        low_price=bar.low_price,
        close_price=bar.close_price,
        volume=bar.volume,
        availability_time=bar.availability_time,
    )


class SchedulerError(Exception):
    """The scheduler cannot be constructed. Never raised from inside the loop itself."""


class SessionPhase(StrEnum):
    """Where the loop is in the trading day.

    `SQUARING_OFF` is a first-class phase and not a branch inside `TRADING`, deliberately: it must
    run when the tape has gone quiet, when no bot proposes anything, and when the previous iteration
    errored. A state that exists only as a condition inside another state is a state that gets
    skipped, and `R.01` calls the skip the failure mode.
    """

    BEFORE_OPEN = "before_open"
    TRADING = "trading"
    SQUARING_OFF = "squaring_off"
    AFTER_CLOSE = "after_close"

    @property
    def observes_the_market(self) -> bool:
        return self in (SessionPhase.TRADING, SessionPhase.SQUARING_OFF)

    @property
    def may_open_a_position(self) -> bool:
        """Only `TRADING`.

        Opening inside the square-off window is how a position survives the close.
        """
        return self is SessionPhase.TRADING


@dataclass(frozen=True, slots=True)
class SchedulerIteration:
    """One tick of the loop, recorded whether it did anything or not.

    A quiet iteration is still written. The alternative — recording only interesting ticks — makes a
    stalled loop and an uneventful market look identical, which is exactly the confusion that let a
    SIGKILLed daily run go unnoticed for days (`A.143`).
    """

    observed_at: datetime
    phase: SessionPhase
    session_date: date
    instruments_observed: int
    proposals: int
    tape_lag_seconds: float | None
    note: str
    failure: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.failure is None

    def describe(self) -> str:
        lag = "unknown" if self.tape_lag_seconds is None else f"{self.tape_lag_seconds:.1f}s"
        state = f"FAILED: {self.failure}" if self.failure else self.note
        return (
            f"{self.observed_at.isoformat(timespec='seconds')} [{self.phase.value}] "
            f"{self.session_date} · {self.instruments_observed:,} observed · "
            f"{self.proposals} proposal(s) · tape lag {lag} · {state}"
        )


class MarketObservationSource(Protocol):
    """What the loop needs from the live tape. A seam, so a test drives it with no parquet at all.

    Deliberately narrow: the scheduler does not want a tape, it wants *the latest price per token
    and how stale that is*. Handing it a reader would let it grow a dependency on the storage
    format, which is the coupling `L5.29` forbids for bots and which is no better here.
    """

    def latest_prices(self, session_date: date) -> dict[int, Decimal]: ...

    def latest_exchange_time(self, session_date: date) -> datetime | None: ...

    def ticks_since(
        self, session_date: date, after_sequence: int, limit: int
    ) -> tuple[tuple[int, datetime | None, Decimal, int, int], ...]:
        """`(token, exchange_time, last_price_paise, volume, sequence)` in stream order.

        Needed because a five-minute BAR is not a five-minute SAMPLE: taking the latest price once
        per cadence gives a close-only series whose high and low are whatever happened to be
        sampled, and the volatility classifier reads exactly those. Real OHLC needs the ticks.
        """
        ...


class SchedulerLivenessStore:
    """Append-only record of every iteration, so the dashboard reads rather than re-derives.

    SQLite rather than a log file for one reason: `A.143`'s failure was a job whose only output was
    a log nobody opened. A table can be rendered, counted and alerted on; a log line cannot without
    someone deciding to parse it.
    """

    def __init__(self, path: Path = DEFAULT_LIVENESS_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_iteration (
                observed_at TEXT NOT NULL,
                phase TEXT NOT NULL,
                session_date TEXT NOT NULL,
                instruments_observed INTEGER NOT NULL,
                proposals INTEGER NOT NULL,
                tape_lag_seconds REAL,
                note TEXT NOT NULL,
                failure TEXT,
                PRIMARY KEY (observed_at, session_date)
            )
            """
        )

    def append(self, iteration: SchedulerIteration) -> None:
        """Record one tick.

        `INSERT OR REPLACE` on the instant, so a replay does not double a row.
        """
        self._connection.execute(
            "INSERT OR REPLACE INTO scheduler_iteration VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iteration.observed_at.isoformat(),
                iteration.phase.value,
                iteration.session_date.isoformat(),
                iteration.instruments_observed,
                iteration.proposals,
                iteration.tape_lag_seconds,
                iteration.note,
                iteration.failure,
            ),
        )

    def recent(self, limit: int = 50) -> tuple[SchedulerIteration, ...]:
        rows = self._connection.execute(
            "SELECT observed_at, phase, session_date, instruments_observed, proposals,"
            " tape_lag_seconds, note, failure FROM scheduler_iteration"
            " ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            SchedulerIteration(
                observed_at=datetime.fromisoformat(row[0]),
                phase=SessionPhase(row[1]),
                session_date=date.fromisoformat(row[2]),
                instruments_observed=int(row[3]),
                proposals=int(row[4]),
                tape_lag_seconds=None if row[5] is None else float(row[5]),
                note=str(row[6]),
                failure=row[7],
            )
            for row in rows
        )

    def last_healthy_at(self) -> datetime | None:
        """When the loop last ticked without failing — the one number an operator wants."""
        row = self._connection.execute(
            "SELECT MAX(observed_at) FROM scheduler_iteration WHERE failure IS NULL"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def close(self) -> None:
        self._connection.close()


class ContinuousPaperTradingScheduler:
    """Owns the bots, the panel, the phase and the record. Steps once per call and never sleeps.

    **SOTA analog:** the session supervisor in NautilusTrader's live node — a state machine over the
    venue calendar that owns strategy instances for the process lifetime and drives them on a clock,
    rather than a job that constructs a strategy per invocation. The difference that matters here is
    the same one: state that survives between decisions is what turns a sequence of snapshots into a
    strategy.
    """

    def __init__(
        self,
        *,
        bots: Sequence[SegmentBotFoundation],
        observations: MarketObservationSource,
        liveness: SchedulerLivenessStore,
        universe: Sequence[TradeableInstrument] = (),
        armed_classifiers: Sequence[str] = ARMED_CLASSIFIERS,
        calendar: NseTradingSessionCalendar | None = None,
        square_off_window: timedelta = SQUARE_OFF_WINDOW,
        archive_replay: WalkForwardArchiveReplay | None = None,
        book: LivePaperBook | None = None,
        supervisor: PortfolioProposalSupervisor | None = None,
        lot_size_by_token: Mapping[int, int] | None = None,
        universe_by_segment: Mapping[TradingSegment, Sequence[TradeableInstrument]] | None = None,
    ) -> None:
        if not bots:
            raise SchedulerError(
                "a scheduler with no bots would tick forever recording that it decided nothing, "
                "which is indistinguishable from a broken loop and worse than not starting"
            )
        self._bots = tuple(bots)
        self._observations = observations
        self._liveness = liveness
        self._calendar = calendar or NseTradingSessionCalendar()
        self._square_off_window = square_off_window
        self._universe = tuple(universe)
        self._iterations = 0
        # `L4.27` + `B40`: the panel is fitted on five-minute bars and the tape ticks, so the loop
        # folds ticks into bars itself rather than handing a bar-fitted classifier a tape-derived
        # observation (`A.106`). One aggregator for the whole universe, five numbers per instrument.
        self._aggregator = LiveTickToBarAggregator()
        self._signal_state: dict[int, InstrumentSignalState] = {}
        self._brain = SoftRegimeWeightingBrain()
        for classifier_name in armed_classifiers:
            self._brain.arm(classifier_name)
        self._tick_cursor = 0
        self._bars_built = 0
        self._bar_closes_by_instant: dict[datetime, dict[int, Decimal]] = {}
        # `A.146`: what the loop does with the other eighteen and a half hours of the day. `None`
        # keeps the old behaviour — alive and deciding nothing — so a caller that has not been
        # given an archive is not silently trading one.
        self._archive_replay = archive_replay
        # `A.146`: the loop EXECUTES. `None` for either of these keeps the observe-only behaviour,
        # so a caller that has not been given a book cannot silently start trading one.
        self._book = book
        self._supervisor = supervisor
        self._lot_size_by_token: dict[int, int] = dict(lot_size_by_token or {})
        self._last_plan: PortfolioPlan | None = None
        self._last_mark: BookMark | None = None
        self._last_proposals_by_segment: dict[str, int] = {}
        self._segment_failures: dict[str, str] = {}
        self._prices_by_segment: dict[TradingSegment, dict[int, Decimal]] = {}
        self._underlying_prices_by_segment: dict[TradingSegment, dict[str, Decimal]] = {}
        # `A.146`: each bot decides over ITS OWN segment's universe. Before this, the loop assembled
        # the cash universe and handed it to all six, then skipped five of them because a cash
        # instrument is not a contract they can trade — which read as five quiet bots rather than as
        # five bots that were never given anything.
        self._universe_by_segment: dict[TradingSegment, tuple[TradeableInstrument, ...]] = {
            segment: tuple(instruments)
            for segment, instruments in (universe_by_segment or {}).items()
        }

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def bars_built(self) -> int:
        """Closed bars folded and fed to the panel — the `B40` progress number."""
        return self._bars_built

    @property
    def instruments_with_a_panel(self) -> int:
        return sum(
            1
            for state in self._signal_state.values()
            if state.bars_observed >= BARS_BEFORE_THE_PANEL_IS_ASKED
        )

    def replace_universe(self, universe: Sequence[TradeableInstrument]) -> None:
        """Swap the tradeable set WITHOUT discarding carried state.

        The instrument master changes at most once a session while the bots' rolling statistics take
        many ticks to mature, so rebuilding the scheduler to pick up a new universe would throw away
        the state that is the whole point of this class.
        """
        self._universe = tuple(universe)

    def replace_segment_universe(
        self,
        segment: TradingSegment,
        universe: Sequence[TradeableInstrument],
        last_price_paise_by_token: Mapping[int, Decimal] | None = None,
        underlying_price_paise_by_symbol: Mapping[str, Decimal] | None = None,
    ) -> None:
        """Swap one segment's tradeable set and its own last prices, leaving carried state intact.

        The prices matter as much as the instruments. A derivative contract's price comes from the
        projected bhavcopy close unless the live tape happens to carry it, and a bot handed a
        universe with no prices proposes nothing while looking perfectly healthy — which is what the
        first six-segment tick did.
        """
        self._universe_by_segment[segment] = tuple(universe)
        if last_price_paise_by_token is not None:
            self._prices_by_segment[segment] = dict(last_price_paise_by_token)
        if underlying_price_paise_by_symbol is not None:
            self._underlying_prices_by_segment[segment] = dict(underlying_price_paise_by_symbol)
        if segment is TradingSegment.CASH_INTRADAY:
            self._universe = tuple(universe)

    def universe_for(self, segment: TradingSegment) -> tuple[TradeableInstrument, ...]:
        """What this segment's bot decides over. Cash falls back to the loop's own universe."""
        if segment in self._universe_by_segment:
            return self._universe_by_segment[segment]
        return self._universe if segment is TradingSegment.CASH_INTRADAY else ()

    @property
    def archive_progress(self) -> WalkForwardProgress | None:
        """How far the closed-market walk has got, or `None` when no archive was given."""
        if self._archive_replay is None:
            return None
        return self._archive_replay.progress(as_of=datetime.now(IST).date())

    @property
    def bots(self) -> tuple[SegmentBotFoundation, ...]:
        """The SAME instances across every tick — that is the point of this class."""
        return self._bots

    # ---- phase ------------------------------------------------------------------------

    def phase_at(self, instant: datetime) -> SessionPhase:
        """Where the day is, from the exchange calendar rather than from the weekday.

        A holiday is `AFTER_CLOSE` all day: there is no session to be before the open of. The
        alternative — `BEFORE_OPEN` on a holiday — would have the loop waiting all day for an open
        that never arrives, which is how a loop looks alive while doing nothing.
        """
        if instant.tzinfo is None:
            raise SchedulerError(
                f"the scheduler was asked about a naive {instant}; a naive instant is read as UTC "
                f"and moves every session boundary in this project by five and a half hours"
            )
        local = instant.astimezone(IST)
        if not self._calendar.is_trading_session(local.date()):
            return SessionPhase.AFTER_CLOSE
        session = session_for(local.date())
        if local < session.opens_at:
            return SessionPhase.BEFORE_OPEN
        if local >= session.closes_at:
            return SessionPhase.AFTER_CLOSE
        if local >= session.closes_at - self._square_off_window:
            return SessionPhase.SQUARING_OFF
        return SessionPhase.TRADING

    # ---- one tick ---------------------------------------------------------------------

    def step(self, instant: datetime) -> SchedulerIteration:
        """Advance the loop by one tick. Records the outcome and NEVER raises."""
        self._iterations += 1
        local = instant.astimezone(IST)
        phase = self.phase_at(instant)
        session_date = local.date()
        try:
            iteration = self._observe_and_decide(local, phase, session_date)
        except Exception as failure:  # noqa: BLE001 — a failed tick is recorded, never fatal
            iteration = SchedulerIteration(
                observed_at=local,
                phase=phase,
                session_date=session_date,
                instruments_observed=0,
                proposals=0,
                tape_lag_seconds=None,
                note="iteration failed",
                failure=f"{type(failure).__name__}: {failure}",
            )
        self._liveness.append(iteration)
        return iteration

    def _observe_and_decide(
        self, local: datetime, phase: SessionPhase, session_date: date
    ) -> SchedulerIteration:
        if not phase.observes_the_market:
            return self._walk_the_archive(local, phase, session_date)

        self._fold_new_ticks_into_bars(session_date)
        prices = self._observations.latest_prices(session_date)
        latest = self._observations.latest_exchange_time(session_date)
        lag = None if latest is None else (local - latest.astimezone(IST)).total_seconds()

        # Replay each CLOSED bar instant to the bots, in order, so their carried state advances on
        # the same five-minute clock the panel and the fitted engine both use.
        self._advance_bots_over_closed_bars()

        priced_universe = tuple(
            instrument for instrument in self._universe if instrument.instrument_token in prices
        )
        context = SegmentBotContext(
            decision_instant=local,
            tradeable_universe=priced_universe,
            regime=self._belief_from_the_panel(local),
            carried_memory={"last_price_paise_by_token": prices},
        )
        proposals = 0
        proposals_by_segment: dict[str, int] = {}
        collected: list[BotProposal] = []
        for bot in self._bots:
            if bot.trading_segment is TradingSegment.CASH_INTRADAY:
                if phase.may_open_a_position:
                    signals = bot.propose(context)
                    proposals += len(signals)
                    proposals_by_segment[bot.trading_segment.value] = len(signals)
                    collected.extend(self._as_proposals(bot, signals))
                continue
            # The other five decide on the cadence their own data supports (`A.141`). They are
            # OBSERVED every tick against their own universe so the surface can show what each one
            # is looking at, and they PROPOSE only in the trading phase, priced off the live tape
            # where it covers their contracts and off the projected close where it does not.
            segment_universe = self.universe_for(bot.trading_segment)
            if not segment_universe:
                proposals_by_segment[bot.trading_segment.value] = 0
                continue
            if not phase.may_open_a_position:
                proposals_by_segment[bot.trading_segment.value] = 0
                continue
            # The tape is preferred where it reaches, and the projected close is what stands where
            # it does not (`A.141`). Merged in this order on purpose: a live price is always more
            # current than a settlement price, and a settlement price is always better than none.
            segment_prices: dict[int, Decimal] = dict(
                self._prices_by_segment.get(bot.trading_segment, {})
            )
            segment_prices.update(
                {
                    token: price
                    for token, price in prices.items()
                    if token in {i.instrument_token for i in segment_universe}
                }
            )
            priced_segment = tuple(
                instrument
                for instrument in segment_universe
                if instrument.instrument_token in segment_prices
            )
            carried: dict[str, object] = {"last_price_paise_by_token": segment_prices}
            underlying = self._underlying_prices_by_segment.get(bot.trading_segment)
            if underlying:
                carried["underlying_price_paise_by_symbol"] = underlying
            segment_context = SegmentBotContext(
                decision_instant=local,
                tradeable_universe=priced_segment or segment_universe,
                regime=context.regime,
                carried_memory=carried,
            )
            try:
                signals = bot.propose(segment_context)
            except Exception as failure:  # noqa: BLE001 — one bot must not stop the other five
                proposals_by_segment[bot.trading_segment.value] = 0
                self._segment_failures[bot.trading_segment.value] = (
                    f"{type(failure).__name__}: {failure}"
                )
                continue
            proposals += len(signals)
            proposals_by_segment[bot.trading_segment.value] = len(signals)
            collected.extend(self._as_proposals(bot, signals))
        self._last_proposals_by_segment = proposals_by_segment
        traded = self._trade(collected, prices, local, session_date, phase)

        note = (
            f"{len(priced_universe):,} of {len(self._universe):,} priced; "
            f"{self._bars_built:,} bars built, {self.instruments_with_a_panel:,} panels mature"
            if phase.may_open_a_position
            else "square-off window — flattening only, no new positions (`R.01`)"
        )
        if traded:
            note = f"{note} | {traded}"
        return SchedulerIteration(
            observed_at=local,
            phase=phase,
            session_date=session_date,
            instruments_observed=len(priced_universe),
            proposals=proposals,
            tape_lag_seconds=lag,
            note=note,
        )

    def _as_proposals(
        self, bot: SegmentBotFoundation, signals: Sequence[PricedSignal]
    ) -> list[BotProposal]:
        """Wrap a bot's signals for the supervisor, carrying the lot size the exchange enforces.

        `margin_rupees` is left `None` here and the supervisor then bounds on NOTIONAL. That is
        deliberate and it is why the futures bots stay unsized on this path: `B36`'s SPAN file has
        not arrived, and a leverage multiple guessed here would be exactly the invented number
        `R.03` forbids (`A.145` measured what guessing costs — Rs 1,98,600 in one session).
        """
        return [
            BotProposal(
                bot_identity=bot.bot_identity,
                signal=signal,
                margin_rupees=None,
                lot_size=self._lot_size_by_token.get(signal.instrument_token, 1),
            )
            for signal in signals
        ]

    def _trade(
        self,
        proposals: Sequence[BotProposal],
        prices: Mapping[int, Decimal],
        local: datetime,
        session_date: date,
        phase: SessionPhase,
    ) -> str:
        """Turn this tick's proposals into positions, and flatten the book in the square-off window.

        Returns a one-line description for the liveness record, or an empty string when the loop is
        observing only — a caller with no book and no supervisor behaves exactly as it did before.
        """
        if self._book is None:
            return ""

        parts: list[str] = []
        if phase is SessionPhase.SQUARING_OFF:
            # `R.01`: square-off is the failure mode, so it happens before anything else and is
            # never conditional on the book looking healthy.
            outcome = self._book.square_off(prices, at=local)
            if outcome.closed or outcome.unclosable:
                parts.append(outcome.describe())
        elif proposals and self._supervisor is not None:
            plan = self._supervisor.supervise(
                proposals, book_net_rupees=self._book.net_exposure_rupees()
            )
            self._last_plan = plan
            opened = self._book.admit(plan, at=local, session_date=session_date)
            parts.append(f"{plan.describe()} · {opened} opened")

        mark = self._book.mark(prices, at=local)
        self._last_mark = mark
        if mark.open_positions:
            parts.append(mark.describe())
        return " | ".join(parts)

    @property
    def last_plan(self) -> PortfolioPlan | None:
        """The most recent allocation, for the surface to read rather than re-derive."""
        return self._last_plan

    @property
    def last_mark(self) -> BookMark | None:
        return self._last_mark

    def _walk_the_archive(
        self, local: datetime, phase: SessionPhase, session_date: date
    ) -> SchedulerIteration:
        """What the loop does while the exchange is shut (`A.146`).

        One archived session per tick, never the same one twice. One per tick rather than the whole
        archive in one call because the loop must stay responsive to the open — a driver that walked
        the entire archive in a single step would hold the tick for as long as the archive is long
        and miss the bell.
        """
        if self._archive_replay is None:
            return SchedulerIteration(
                observed_at=local,
                phase=phase,
                session_date=session_date,
                instruments_observed=0,
                proposals=0,
                tape_lag_seconds=None,
                note=(
                    "market closed and no archive was given — the loop is alive and deciding "
                    "nothing"
                ),
            )

        outcome = self._archive_replay.replay_next(as_of=local.date())
        progress = self._archive_replay.progress(as_of=local.date())
        if outcome is None:
            return SchedulerIteration(
                observed_at=local,
                phase=phase,
                session_date=session_date,
                instruments_observed=0,
                proposals=0,
                tape_lag_seconds=None,
                note=f"market closed · {progress.describe()}",
            )
        return SchedulerIteration(
            observed_at=local,
            phase=phase,
            session_date=session_date,
            instruments_observed=0,
            proposals=outcome.trades_accrued,
            tape_lag_seconds=None,
            note=f"walked the archive · {outcome.describe()} · {progress.describe()}",
            failure=outcome.failure,
        )

    def _fold_new_ticks_into_bars(self, session_date: date) -> None:
        """Read the ticks arrived since the cursor and advance the panel on every CLOSED bar.

        Bounded per iteration so a loop that has fallen behind catches up over several ticks rather
        than pulling a whole session into memory at once. Never raises: a tape that cannot be read
        leaves the panel where it was, which the iteration's tape lag already reports.
        """
        try:
            ticks = self._observations.ticks_since(
                session_date, self._tick_cursor, TICKS_READ_PER_ITERATION
            )
        except Exception:  # noqa: BLE001 — reported through tape lag, never fatal to the loop
            return
        for token, stamp, price, volume, sequence in ticks:
            self._tick_cursor = max(self._tick_cursor, sequence)
            closed = self._aggregator.observe(
                instrument_token=token,
                exchange_time=stamp,
                last_price_paise=price,
                volume=volume,
            )
            if closed is None:
                continue
            state = self._signal_state.get(token)
            if state is None:
                state = InstrumentSignalState(
                    trend=TrendStrengthRegimeClassifier(),
                    volatility=VolatilityRegimeClassifier(),
                    session_phase=SessionPhaseRegimeClassifier(),
                    strategy=IntradayMeanReversionEngine(
                        minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
                        minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
                    ),
                )
                self._signal_state[token] = state
            try:
                state.observe(_as_available_bar(closed))
            except Exception:  # noqa: BLE001, S112 — one refused bar is a skip, not a dead loop
                continue
            self._bars_built += 1
            # The BOTS learn from the same closed bars as the panel, keyed on the bar's own
            # timestamp. Feeding them a per-tick price snapshot instead — which the first version
            # did — gave the panel 30,059 observations and the bots 6, so the panel matured while
            # every bot's own reversion engine stayed immature and proposed nothing. One clock for
            # everything, or the two halves of a decision disagree about what an observation is.
            self._bar_closes_by_instant.setdefault(closed.bar_timestamp, {})[token] = Decimal(
                str(round(closed.close_price * float(PAISE_PER_RUPEE)))
            )

    def _advance_bots_over_closed_bars(self) -> None:
        """Hand the bots one observation per CLOSED bar, oldest first, then forget it.

        Drained rather than accumulated for the same reason the aggregator drains: a map of every
        bar of the session is the history `L4.27` forbids retaining. The cash bot's `observe` is
        idempotent by instant, so a bar replayed twice is ignored rather than double-counted.
        """
        if not self._bar_closes_by_instant:
            return
        universe_by_token = {
            instrument.instrument_token: instrument for instrument in self._universe
        }
        # ONE belief for the whole batch, and this is a stated approximation rather than a hidden
        # one. Recomputing it per bar instant meant ~1,843 signal states x 3 opinions x every bar
        # instant in the batch — measured: a catch-up tick did not finish inside two minutes.
        #
        # What it costs: while the loop is CATCHING UP, historical bars are replayed against the
        # panel's CURRENT belief rather than the belief that stood when each bar closed. That is
        # already true of the panel itself, which carries one cumulative state and cannot be asked
        # what it thought an hour ago. Once caught up a tick carries about one bar instant, so the
        # distinction disappears — a warm-up property, not a live one.
        belief = self._belief_from_the_panel(max(self._bar_closes_by_instant))
        for bar_instant in sorted(self._bar_closes_by_instant):
            closes = self._bar_closes_by_instant[bar_instant]
            observed = tuple(
                universe_by_token[token] for token in closes if token in universe_by_token
            )
            if not observed:
                continue
            context = SegmentBotContext(
                decision_instant=bar_instant,
                tradeable_universe=observed,
                regime=belief,
                carried_memory={"last_price_paise_by_token": dict(closes)},
            )
            for bot in self._bots:
                if bot.trading_segment is TradingSegment.CASH_INTRADAY:
                    bot.observe(context)
        self._bar_closes_by_instant.clear()

    def _belief_from_the_panel(self, at_instant: datetime) -> RegimeDistribution:
        """The panel's combined belief, or honest ignorance while it is still warming up.

        The opinions come from the instruments the loop has actually built bars for, which is the
        whole point of `B40`: the classifiers now see the five-minute bars they were fitted on
        rather than a tape they were not.

        Falls back to the uninformative belief when no instrument has enough bars — a REAL state on
        a fresh process start, not an error. A maximum-entropy belief makes the cash bot's regime
        veto abstain, which is the honest answer while the panel is immature.
        """
        opinions: list[RegimeOpinion] = []
        for state in self._signal_state.values():
            if state.bars_observed < BARS_BEFORE_THE_PANEL_IS_ASKED:
                continue
            opinions.extend(state.opinions(at_instant))
        if not opinions:
            return self._uninformative_regime()
        return self._brain.combine(opinions, at_instant).distribution

    @staticmethod
    def _uninformative_regime() -> RegimeDistribution:
        """A maximum-entropy belief — used only while the panel has nothing to say."""
        return RegimeDistribution.from_scores(dict.fromkeys(MarketRegime, 1.0))

    # ---- the only thing that sleeps ---------------------------------------------------

    def run_until(
        self,
        stop_at: datetime,
        *,
        cadence_seconds: float,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> tuple[SchedulerIteration, ...]:
        """Tick until `stop_at`. The clock and the sleep are injected so a test runs instantly."""
        if cadence_seconds <= 0:
            raise SchedulerError(
                f"a cadence of {cadence_seconds}s would spin without advancing the market"
            )
        read_clock = now or (lambda: datetime.now(IST))
        pause = sleep or wall_clock.sleep
        history: list[SchedulerIteration] = []
        while True:
            instant = read_clock()
            if instant >= stop_at:
                return tuple(history)
            history.append(self.step(instant))
            pause(cadence_seconds)


class DepthTapeObservationSource:
    """The live tape, read as the capture writes it. The production `MarketObservationSource`.

    **Reads through `duckdb` over the completed parquet parts**, which is what makes a read
    concurrent with a live capture safe: the writer's in-progress file begins with a dot and is not
    matched, so a read returns a consistent prefix rather than a torn row group. The same property
    `MarketDepthTapeReader` documents, relied on here rather than re-derived.

    Measured against the real capture on 2026-08-18 at 13:06 IST: 410 completed parts, 7,135,786
    ticks, 1,845 tokens, newest tick eleven seconds old.
    """

    def __init__(self, tape_root: Path) -> None:
        self._tape_root = tape_root

    def _partition(self, session_date: date) -> str:
        partition = self._tape_root / f"session_date={session_date.isoformat()}"
        return str(partition / "**" / "*.parquet")

    def latest_prices(self, session_date: date) -> dict[int, Decimal]:
        """The most recent traded price per instrument, in paise.

        One row per token via a window over `(capture_run, receipt_sequence)`, and the capture_run
        half is load-bearing rather than decorative.

        **`receipt_sequence` RESTARTS AT 1 for every capture run** — measured on the real tape:
        `session_date=2026-08-18` holds run `000017` numbered 1..1,116 and run `080533` numbered
        1..8,534,670. Ordering on the sequence alone therefore interleaves two runs as though they
        were one stream, and it picked correctly here only by the accident that the live run happens
        to have far more ticks. On a morning where a short catch-up run started AFTER the main one,
        the same query would serve a stale price with total confidence (`B42`).

        Ordering on `exchange_time` instead is not the fix either: it ties for every packet the
        exchange stamped in the same millisecond, and a tie broken arbitrarily is a price chosen
        arbitrarily.
        """
        connection = duckdb.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT instrument_token, last_price_paise FROM (
                    SELECT instrument_token, last_price_paise,
                           ROW_NUMBER() OVER (
                               PARTITION BY instrument_token
                               ORDER BY capture_run DESC, receipt_sequence DESC
                           ) AS recency
                    FROM read_parquet('{self._partition(session_date)}')
                    WHERE last_price_paise > 0
                ) WHERE recency = 1
                """  # noqa: S608 — path from a Path and a date, never from user text
            ).fetchall()
        except Exception:  # noqa: BLE001 — an unreadable tape is an empty observation, not a crash
            return {}
        finally:
            connection.close()
        return {int(token): Decimal(str(price)) for token, price in rows if price}

    def ticks_since(
        self, session_date: date, after_sequence: int, limit: int
    ) -> tuple[tuple[int, datetime | None, Decimal, int, int], ...]:
        """Ticks after a cursor, in stream order, bounded.

        **The cursor is the raw `receipt_sequence` and NOT a computed row number**, which is a
        performance decision as much as a correctness one. The first version numbered rows with
        `ROW_NUMBER() OVER (ORDER BY capture_run, receipt_sequence)` and paid a full sort of the
        whole partition on every read — 8,534,670 rows by 13:54 IST — so a single iteration did not
        finish inside two minutes. A plain predicate needs no window function.
        """
        connection = duckdb.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT instrument_token, exchange_time, last_price_paise, volume_traded,
                       receipt_sequence
                FROM read_parquet('{self._partition(session_date)}')
                WHERE last_price_paise > 0 AND receipt_sequence > ?
                ORDER BY capture_run, receipt_sequence
                LIMIT ?
                """,  # noqa: S608 — path from a Path and a date, never from user text
                (after_sequence, limit),
            ).fetchall()
        except Exception:  # noqa: BLE001 — an unreadable tape yields no ticks, not a crash
            return ()
        finally:
            connection.close()
        return tuple(
            (int(token), stamp, Decimal(str(price)), int(volume or 0), int(sequence))
            for token, stamp, price, volume, sequence in rows
        )

    def latest_exchange_time(self, session_date: date) -> datetime | None:
        """The newest tick the exchange stamped — the numerator of the loop's own staleness."""
        connection = duckdb.connect()
        try:
            row = connection.execute(
                f"SELECT MAX(exchange_time) FROM read_parquet('{self._partition(session_date)}')"  # noqa: S608
            ).fetchone()
        except Exception:  # noqa: BLE001 — same reasoning as above
            return None
        finally:
            connection.close()
        return row[0] if row and row[0] else None
