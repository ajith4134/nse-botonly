"""`L11.06` — where in the session we are, and what kind of session it has been.

The structural member of the panel. An NSE trading day is not homogeneous and pretending
it is costs real money: the opening auction and the first half hour carry the overnight
gap and the widest spreads, midday is the quietest and most mean-reverting stretch, and
the last half hour carries closing-auction positioning and squaring-off flow. A
mean-reversion engine in particular is known to fail into the close, when a deviation
that would normally revert is instead someone with an hour left to flatten a position.

Unlike the other three, this classifier knows something with **certainty** — the clock is
not estimated. That is why it can emit a confident distribution from the first bar while
the others are still immature, and it is exactly the kind of independent footing that
makes a panel worth more than its best member.

Session phase boundaries are exchange facts, not tuned parameters, and are sourced from
the same NSE quoting window the depth recorder already uses (`R.23(e)`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

CONTINUOUS_TRADING_OPENS = time(9, 15)
FIRST_PHASE_ENDS = time(9, 45)
LAST_PHASE_BEGINS = time(15, 0)
CONTINUOUS_TRADING_CLOSES = time(15, 30)
"""Exchange facts. NSE has moved these historically — continuous trading began at 09:55
before 2010 — so a historical replay must not reuse today's window. That limitation is
carried in the opinion's evidence rather than hidden."""


@dataclass
class SessionPhaseRegimeClassifier:
    """Session position plus the character the session has actually shown so far."""

    name: str = "session_phase"
    _session_high: float | None = None
    _session_low: float | None = None
    _session_open: float | None = None
    _latest_close: float | None = None
    _bars_seen: int = 0
    _phase_counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_mature(self) -> bool:
        """The clock is known from the first bar; only the session's CHARACTER needs
        history, and one bar is enough to place us in the day."""
        return self._bars_seen >= 1

    def observe(self, moment: datetime, high: float, low: float, close: float) -> None:
        ist = moment.astimezone(INDIA_MARKET_TIMEZONE)
        if self._session_open is None:
            self._session_open = close
        self._session_high = high if self._session_high is None else max(self._session_high, high)
        self._session_low = low if self._session_low is None else min(self._session_low, low)
        self._latest_close = close
        self._bars_seen += 1
        phase = self.phase_of(ist)
        self._phase_counts[phase] = self._phase_counts.get(phase, 0) + 1

    @staticmethod
    def phase_of(moment_ist: datetime) -> str:
        clock = moment_ist.time()
        if clock < CONTINUOUS_TRADING_OPENS:
            return "pre_open"
        if clock < FIRST_PHASE_ENDS:
            return "opening"
        if clock < LAST_PHASE_BEGINS:
            return "midday"
        if clock <= CONTINUOUS_TRADING_CLOSES:
            return "closing"
        return "post_close"

    def session_travel_efficiency(self) -> float:
        """How much of the session's range the close actually captured.

        Near 1 means the day went somewhere and stayed — a trend day. Near 0 means it
        travelled and came back — a range day. This is the session's CHARACTER, measured
        from its own realised path rather than assumed from its phase.
        """
        if self._session_high is None or self._session_low is None:
            return 0.0
        span = self._session_high - self._session_low
        if span <= 0.0 or self._session_open is None or self._latest_close is None:
            return 0.0
        return abs(self._latest_close - self._session_open) / span

    def opinion(self, observed_at: datetime) -> RegimeOpinion:
        phase = self.phase_of(observed_at.astimezone(INDIA_MARKET_TIMEZONE))
        travel = self.session_travel_efficiency()

        # Phase sets the PRIOR; the session's realised path updates it. Opening and
        # closing carry gap and squaring-off flow, so they lean volatile; midday is the
        # quiet, mean-reverting stretch. These are structural, not fitted.
        if phase in ("opening", "closing"):
            scores = {
                MarketRegime.VOLATILE: 1.0 + travel,
                MarketRegime.TRENDING: travel,
                MarketRegime.RANGING: 1.0 - travel,
                MarketRegime.QUIET: 0.0,
            }
        elif phase == "midday":
            scores = {
                MarketRegime.RANGING: 1.0 + (1.0 - travel),
                MarketRegime.QUIET: 1.0 - travel,
                MarketRegime.TRENDING: travel,
                MarketRegime.VOLATILE: 0.0,
            }
        else:
            # Outside continuous trading nothing can be claimed about the tape.
            scores = dict.fromkeys(MarketRegime, 1.0)

        return RegimeOpinion(
            classifier_name=self.name,
            distribution=RegimeDistribution.from_scores(scores),
            observed_at=observed_at,
            is_mature=self.is_mature,
            observations_seen=self._bars_seen,
            evidence=(
                f"phase={phase} session_travel={travel:.3f} "
                f"(window is TODAY's; NSE moved it pre-2010)"
            ),
        )
