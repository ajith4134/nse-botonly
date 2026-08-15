"""Where a paper session's decisions come from — real classifiers, fed bar by bar (`F04`).

Specification: `docs/research/228_paper_trading_loop_and_simulated_venue_spec.md` §3.

`F04`'s loop needs something to trade on, and the honest options were two: a signal fixture, or
this project's own regime brain and mean-reversion engine driven by the same bars the replay clock
releases. A fixture would have produced a paper record that measures the plumbing and nothing else,
which is precisely the `R.13` failure — correctness of execution mistaken for correctness of
allocation.

**The state is the point, and it is carried per instrument.** Each instrument owns its own
`TrendStrengthRegimeClassifier`, `VolatilityRegimeClassifier`, `SessionPhaseRegimeClassifier` and
`IntradayMeanReversionEngine`. Those are streaming estimators: a classifier that has seen four bars
of RELIANCE holds a different belief from one that has seen four hundred, and re-feeding the same
bar twice would double-count it into every quantile it maintains. So this source remembers the last
bar timestamp it fed to each instrument and advances strictly forward — the same discipline the
replay clock applies to time, applied to evidence.

**No bar is fed before it was knowable.** Bars are read `availability_time <= at`, never on
`bar_timestamp` (`L0.11`). That single predicate is what separates this from a backtest that sees
the future, and it lives in `bars_available_at` rather than in the caller, so a caller cannot forget
it.

**Immaturity is a decision, not an error.** A classifier below its own maturity floor produces an
`ABSTAIN` with the reason stated, and the brain's `weight_for` already collapses an unarmed or
immature panel to a uniform belief that the strategy refuses to trade on. `R.04`: the algorithm is
whole from the first bar and only its ACTIVATION waits for evidence.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from nse_algo_trader.regime.market_regime_state import RegimeOpinion
from nse_algo_trader.regime.session_phase_regime_classifier import SessionPhaseRegimeClassifier
from nse_algo_trader.regime.soft_regime_weighting_brain import (
    RegimeBelief,
    SoftRegimeWeightingBrain,
)
from nse_algo_trader.regime.trend_strength_regime_classifier import TrendStrengthRegimeClassifier
from nse_algo_trader.regime.volatility_regime_classifier import VolatilityRegimeClassifier
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    BAR_INTERVAL,
    DEFAULT_MARKET_DATA_PATH,
)
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
    MeanReversionDecision,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg


class SignalSourceError(Exception):
    """The signal could not be formed, and a decision taken anyway would be an invention."""


class PaperSignalSource(Protocol):
    """What the paper loop needs from whatever decides. The seam a harness injects through.

    Two methods, because an intent must be attributable to the strategy that asked for it
    (`R.14`, and the journal refuses an intent with no identity) as well as carry the decision.
    """

    def signal_for(
        self, *, instrument_token: int, trading_symbol: str, at: datetime
    ) -> PaperSignal: ...

    def strategy_identity_for(self, instrument_token: int) -> str: ...


@dataclass(frozen=True, slots=True)
class AvailableBar:
    """One bar as it was knowable at a decision instant — never as it was stamped."""

    bar_timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    availability_time: datetime


def bars_available_at(
    *,
    instrument_token: int,
    at: datetime,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    bar_interval: str = BAR_INTERVAL,
    since: datetime | None = None,
) -> tuple[AvailableBar, ...]:
    """Every bar published at or before `at`, oldest first — the only read this module makes.

    `since` narrows to bars stamped strictly after a timestamp already consumed, which is how a
    stepping caller avoids re-feeding a streaming estimator the history it has already learnt from.

    Args:
        instrument_token: the instrument's Kite token, as the store keys it.
        at: the decision instant. Bars published after it are invisible, whatever they are stamped.
        market_data: the store. A parameter so a test can point at a copy (`R.03`, no fixed path).
        bar_interval: the interval to read. The store holds one today; the column exists because
            it will not always.
        since: exclusive lower bound on `bar_timestamp`.

    Raises:
        SignalSourceError: `at` is naive, or the store could not be read. A naive instant would be
            compared against stored offsets and silently shift the cutoff by hours.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise SignalSourceError(
            "the decision instant must carry a timezone; a naive cutoff is compared against "
            "stored offsets and moves the availability boundary by the size of a session"
        )
    query = (
        "SELECT bar_timestamp, open_price, high_price, low_price, close_price, volume, "
        "availability_time FROM price_bars "
        "WHERE instrument_token = ? AND bar_interval = ? AND availability_time <= ?"
    )
    parameters: list[object] = [instrument_token, bar_interval, at.isoformat()]
    if since is not None:
        query += " AND bar_timestamp > ?"
        parameters.append(since.isoformat())
    query += " ORDER BY bar_timestamp"
    try:
        with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error as unreadable:
        raise SignalSourceError(
            f"the bar store at {market_data} could not be read: {unreadable}"
        ) from unreadable
    return tuple(
        AvailableBar(
            bar_timestamp=datetime.fromisoformat(str(stamp)),
            open_price=float(open_price),
            high_price=float(high_price),
            low_price=float(low_price),
            close_price=float(close_price),
            volume=int(volume),
            availability_time=datetime.fromisoformat(str(available)),
        )
        for stamp, open_price, high_price, low_price, close_price, volume, available in rows
    )


@dataclass(slots=True)
class InstrumentSignalState:
    """One instrument's carried estimators, and how far through the tape they have been fed."""

    trend: TrendStrengthRegimeClassifier
    volatility: VolatilityRegimeClassifier
    session_phase: SessionPhaseRegimeClassifier
    strategy: IntradayMeanReversionEngine
    bars_observed: int = 0
    last_bar_timestamp: datetime | None = None
    last_close_price: Decimal | None = None

    def observe(self, bar: AvailableBar) -> None:
        """Advance every estimator by exactly one bar, strictly forward in time.

        Refuses a bar it has already consumed rather than skipping it quietly: a repeated bar
        would be counted twice into every streaming quantile below, and the resulting band would
        be tighter than the instrument's history justifies.
        """
        if self.last_bar_timestamp is not None and bar.bar_timestamp <= self.last_bar_timestamp:
            raise SignalSourceError(
                f"bar {bar.bar_timestamp.isoformat()} is not after the last one consumed "
                f"({self.last_bar_timestamp.isoformat()}); feeding a streaming estimator the same "
                "observation twice tightens its own bands against evidence it never saw"
            )
        self.trend.observe(bar.high_price, bar.low_price, bar.close_price)
        self.volatility.observe(bar.close_price)
        self.session_phase.observe(
            bar.bar_timestamp, bar.high_price, bar.low_price, bar.close_price
        )
        self.strategy.observe(bar.close_price)
        self.bars_observed += 1
        self.last_bar_timestamp = bar.bar_timestamp
        self.last_close_price = Decimal(str(bar.close_price))

    def opinions(self, observed_at: datetime) -> tuple[RegimeOpinion, ...]:
        """The panel's three votes at this instant, each carrying its own maturity."""
        return (
            self.trend.opinion(observed_at),
            self.volatility.opinion(observed_at),
            self.session_phase.opinion(observed_at),
        )


@dataclass(frozen=True, slots=True)
class PaperSignal:
    """One instrument's decision at one instant, with everything that produced it."""

    instrument_token: int
    trading_symbol: str
    observed_at: datetime
    decision: MeanReversionDecision
    belief: RegimeBelief
    bars_observed: int
    bars_consumed_this_step: int
    latest_close_rupees: Decimal | None

    @property
    def is_actionable(self) -> bool:
        return self.decision.is_actionable

    @property
    def side(self) -> TradeLeg | None:
        """Which way the decision trades, or `None` when it abstains."""
        if self.decision.action is MeanReversionAction.ENTER_LONG:
            return TradeLeg.BUY
        if self.decision.action is MeanReversionAction.ENTER_SHORT:
            return TradeLeg.SELL
        return None

    def describe(self) -> str:
        return (
            f"{self.trading_symbol} @ {self.observed_at.isoformat()}: "
            f"{self.decision.action.value} (conviction {self.decision.conviction:.2f}) — "
            f"{self.decision.reason} [{self.bars_observed} bars]"
        )


@dataclass(slots=True)
class MeanReversionPaperSignalSource:
    """The panel and the strategy, per instrument, advanced only by bars that were knowable.

    The brain is SHARED across instruments on purpose: `SoftRegimeWeightingBrain` weights a
    classifier FAMILY by its measured skill, and skill is a property of the classifier rather than
    of the scrip it was pointed at. The classifiers themselves are per instrument, because their
    state is that instrument's own history.
    """

    minimum_regime_concentration: float
    """How concentrated the belief must be before the strategy will act. Operator policy, required
    rather than defaulted — a default here would be a hidden risk setting (`R.03`)."""

    minimum_regime_agreement: float
    """How much the panel must agree. Same argument."""

    armed_classifiers: Sequence[str]
    """Which classifiers carry influence (`A.08`). An unarmed panel yields a uniform belief and the
    strategy abstains — which is the correct behaviour on day one, not a defect."""

    market_data: Path = DEFAULT_MARKET_DATA_PATH
    bar_interval: str = BAR_INTERVAL
    brain: SoftRegimeWeightingBrain = field(default_factory=SoftRegimeWeightingBrain)
    _states: dict[int, InstrumentSignalState] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for classifier_name in self.armed_classifiers:
            self.brain.arm(classifier_name)

    def strategy_identity_for(self, instrument_token: int) -> str:
        """Who owns a decision about this instrument — carried onto the intent it produces."""
        return self.state_for(instrument_token).strategy.name

    def state_for(self, instrument_token: int) -> InstrumentSignalState:
        """This instrument's carried estimators, created on first sight."""
        state = self._states.get(instrument_token)
        if state is None:
            state = InstrumentSignalState(
                trend=TrendStrengthRegimeClassifier(),
                volatility=VolatilityRegimeClassifier(),
                session_phase=SessionPhaseRegimeClassifier(),
                strategy=IntradayMeanReversionEngine(
                    minimum_regime_concentration=self.minimum_regime_concentration,
                    minimum_regime_agreement=self.minimum_regime_agreement,
                ),
            )
            self._states[instrument_token] = state
        return state

    def signal_for(
        self, *, instrument_token: int, trading_symbol: str, at: datetime
    ) -> PaperSignal:
        """Advance this instrument to `at` and ask the strategy what it now thinks.

        Every bar published since the last call is fed in order — a step that skips bars would
        leave the estimators behind the tape, and a paper session whose brain is stale is not the
        system that would have traded that day.
        """
        state = self.state_for(instrument_token)
        fresh = bars_available_at(
            instrument_token=instrument_token,
            at=at,
            market_data=self.market_data,
            bar_interval=self.bar_interval,
            since=state.last_bar_timestamp,
        )
        for bar in fresh:
            state.observe(bar)
        belief = self.brain.combine(state.opinions(at), at)
        decision = state.strategy.decide(belief, at)
        return PaperSignal(
            instrument_token=instrument_token,
            trading_symbol=trading_symbol,
            observed_at=at,
            decision=decision,
            belief=belief,
            bars_observed=state.bars_observed,
            bars_consumed_this_step=len(fresh),
            latest_close_rupees=state.last_close_price,
        )
