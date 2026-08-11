"""`L11.01` — trend strength from ADX, Choppiness and Kaufman efficiency, streaming.

The deterministic member of the four-regime panel. It answers "is price going somewhere
or going nowhere?" from the geometry of the path itself, with no distributional
assumptions — which is exactly why it belongs alongside the Markov model rather than
being replaced by it. When a latent-state model and a path-geometry measure disagree,
that disagreement is information (`L11.03` consumes it); if both engines shared a method
they could only ever agree redundantly.

**Three measures, because each fails differently.**

- **ADX (Wilder)** measures directional movement against total range. It is slow and
  lags turns, but it is the one that distinguishes a *sustained* move from a spike.
- **Choppiness Index** compares summed true range to the range of the whole window. It
  is scale-free and fast, but says nothing about direction.
- **Kaufman Efficiency Ratio** is net displacement over total travelled distance — how
  much of the path went somewhere. Cheapest of the three and the most direct statement
  of the question, but noisy on short windows.

Agreement between three measures that fail in different ways is worth far more than any
one of them tuned harder.

**State is carried, which is what makes this an engine rather than a calculation.**
Wilder's smoothing is recursive by construction: today's smoothed true range is a
function of yesterday's, not of a window recomputed from scratch. The classifier holds
those accumulators across bars, so feeding it a session one bar at a time gives the same
answer as the exchange's own running computation — and, unlike a rolling-window version,
it cannot accidentally see a bar that had not yet happened.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)

WILDER_PERIOD = 14
"""Wilder's own period from *New Concepts in Technical Trading Systems* (1978). A
convention with a source rather than a tuned parameter — and the classifier never
thresholds on it, so nothing downstream depends on the choice being optimal."""

CHOPPINESS_PERIOD = 14

MINIMUM_BARS_FOR_A_RANGE = 2
"""A true range needs a previous close, so nothing can be measured from one bar."""

MINIMUM_BARS_FOR_MATURITY = WILDER_PERIOD * 2
"""Wilder smoothing needs roughly two periods before its recursion stops being dominated
by its own seed. Below this the classifier still computes — `R.04` — but reports itself
immature so the brain can discount it."""


@dataclass
class WilderDirectionalState:
    """The recursive accumulators Wilder's method carries between bars.

    Holding these is the difference between a streaming engine and a window recompute.
    A rolling window would also silently change its answer for a past bar once later
    bars arrived, which is lookahead in everything but name.
    """

    smoothed_true_range: float = 0.0
    smoothed_positive_movement: float = 0.0
    smoothed_negative_movement: float = 0.0
    smoothed_directional_index: float = 0.0
    previous_high: float | None = None
    previous_low: float | None = None
    previous_close: float | None = None
    bars_seen: int = 0

    def update(self, high: float, low: float, close: float) -> None:
        """Advance one bar. Wilder's smoothing, not a simple moving average."""
        if self.previous_close is None:
            self.previous_high, self.previous_low, self.previous_close = high, low, close
            self.bars_seen = 1
            return

        true_range = max(
            high - low,
            abs(high - self.previous_close),
            abs(low - self.previous_close),
        )
        up_move = high - (self.previous_high if self.previous_high is not None else high)
        down_move = (self.previous_low if self.previous_low is not None else low) - low
        # Only the LARGER of the two counts, and only if positive. A bar that expands in
        # both directions is an expansion of range, not directional movement.
        positive_movement = up_move if (up_move > down_move and up_move > 0) else 0.0
        negative_movement = down_move if (down_move > up_move and down_move > 0) else 0.0

        if self.bars_seen < WILDER_PERIOD:
            self.smoothed_true_range += true_range
            self.smoothed_positive_movement += positive_movement
            self.smoothed_negative_movement += negative_movement
        else:
            decay = (WILDER_PERIOD - 1) / WILDER_PERIOD
            self.smoothed_true_range = self.smoothed_true_range * decay + true_range
            self.smoothed_positive_movement = (
                self.smoothed_positive_movement * decay + positive_movement
            )
            self.smoothed_negative_movement = (
                self.smoothed_negative_movement * decay + negative_movement
            )

        self.previous_high, self.previous_low, self.previous_close = high, low, close
        self.bars_seen += 1
        self._update_directional_index()

    def _update_directional_index(self) -> None:
        if self.smoothed_true_range <= 0.0:
            return
        positive_indicator = 100.0 * self.smoothed_positive_movement / self.smoothed_true_range
        negative_indicator = 100.0 * self.smoothed_negative_movement / self.smoothed_true_range
        indicator_sum = positive_indicator + negative_indicator
        if indicator_sum <= 0.0:
            return
        directional_index = (
            100.0 * abs(positive_indicator - negative_indicator) / indicator_sum
        )
        if self.bars_seen <= WILDER_PERIOD + 1:
            self.smoothed_directional_index = directional_index
        else:
            self.smoothed_directional_index = (
                self.smoothed_directional_index * (WILDER_PERIOD - 1) + directional_index
            ) / WILDER_PERIOD

    @property
    def average_directional_index(self) -> float:
        return self.smoothed_directional_index


@dataclass(frozen=True)
class TrendStrengthReading:
    """The three measures and the distribution they imply, all reported.

    The components are exposed rather than hidden because when the panel disagrees, the
    first question is always *which measure dissented and by how much*.
    """

    average_directional_index: float
    choppiness_index: float
    efficiency_ratio: float
    distribution: RegimeDistribution


@dataclass
class TrendStrengthRegimeClassifier:
    """Streaming trend-vs-range classifier over real bars."""

    name: str = "trend_strength"
    _wilder: WilderDirectionalState = field(default_factory=WilderDirectionalState)
    _closes: list[float] = field(default_factory=list)
    _highs: list[float] = field(default_factory=list)
    _lows: list[float] = field(default_factory=list)
    _bars_seen: int = 0

    @property
    def bars_seen(self) -> int:
        return self._bars_seen

    @property
    def is_mature(self) -> bool:
        return self._bars_seen >= MINIMUM_BARS_FOR_MATURITY

    def observe(self, high: float, low: float, close: float) -> None:
        """Take one bar. Bounded history: only what the window needs is retained."""
        self._wilder.update(high, low, close)
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        window = CHOPPINESS_PERIOD + 1
        if len(self._closes) > window:
            self._highs = self._highs[-window:]
            self._lows = self._lows[-window:]
            self._closes = self._closes[-window:]
        self._bars_seen += 1

    def _choppiness_index(self) -> float:
        """100 = maximum chop, 0 = a perfectly efficient move.

        Summed true range against the window's own high-low span: when price covers a lot
        of distance inside a narrow band, the ratio is large and the market is chopping.
        """
        if len(self._closes) < MINIMUM_BARS_FOR_A_RANGE:
            return 50.0  # no evidence either way
        true_ranges = [
            max(
                self._highs[index] - self._lows[index],
                abs(self._highs[index] - self._closes[index - 1]),
                abs(self._lows[index] - self._closes[index - 1]),
            )
            for index in range(1, len(self._closes))
        ]
        summed_true_range = sum(true_ranges)
        window_span = max(self._highs) - min(self._lows)
        if window_span <= 0.0 or summed_true_range <= 0.0:
            return 100.0  # no span at all is the most range-bound state possible
        periods = len(true_ranges)
        raw = 100.0 * math.log10(summed_true_range / window_span) / math.log10(periods)
        return min(100.0, max(0.0, raw))

    def _efficiency_ratio(self) -> float:
        """Net displacement over total travelled distance, on [0, 1]."""
        if len(self._closes) < MINIMUM_BARS_FOR_A_RANGE:
            return 0.0
        net_change = abs(self._closes[-1] - self._closes[0])
        total_travel = sum(
            abs(self._closes[index] - self._closes[index - 1])
            for index in range(1, len(self._closes))
        )
        return net_change / total_travel if total_travel > 0.0 else 0.0

    def _regime_scores(self) -> dict[MarketRegime, float]:
        """Evidence for each regime, on comparable scales.

        Scores, never thresholds. A cutoff like "ADX > 25 means trending" would be an
        unearned constant (`R.03`) and would throw away the magnitude of agreement, which
        is exactly what the brain needs in order to weigh this classifier against the
        others. Each measure is mapped to [0, 1] by its own natural scale — ADX and
        Choppiness are defined on 0-100, efficiency on 0-1 — so the divisors describe the
        measures rather than tune them.
        """
        adx = self._wilder.average_directional_index
        choppiness = self._choppiness_index()
        efficiency = self._efficiency_ratio()
        return {
            MarketRegime.TRENDING: (adx / 100.0) + efficiency + (1.0 - choppiness / 100.0),
            MarketRegime.RANGING: (1.0 - adx / 100.0)
            + (1.0 - efficiency)
            + (choppiness / 100.0),
        }

    def opinion(self, observed_at: datetime) -> RegimeOpinion:
        """The classifier's current belief, as a distribution over all four regimes."""
        reading = self.reading()
        return RegimeOpinion(
            classifier_name=self.name,
            distribution=reading.distribution,
            observed_at=observed_at,
            is_mature=self.is_mature,
            observations_seen=self._bars_seen,
            evidence=(
                f"ADX={reading.average_directional_index:.1f} "
                f"choppiness={reading.choppiness_index:.1f} "
                f"efficiency={reading.efficiency_ratio:.3f}"
            ),
        )

    def reading(self) -> TrendStrengthReading:
        """Every component measure plus the distribution they imply.

        Components are exposed because when the panel disagrees the first question is
        always which measure dissented, and by how much.
        """
        return TrendStrengthReading(
            average_directional_index=self._wilder.average_directional_index,
            choppiness_index=self._choppiness_index(),
            efficiency_ratio=self._efficiency_ratio(),
            # Silent on the dispersion axis — uniform there, never zero. Zero would be
            # a claim this classifier cannot support, and four such claims blend to mush.
            distribution=RegimeDistribution.from_scores_over_axis(
                self._regime_scores(),
                silent_on=(MarketRegime.VOLATILE, MarketRegime.QUIET),
            ),
        )

    def observe_bars(
        self, bars: Sequence[tuple[float, float, float]]
    ) -> None:
        """Convenience for replaying a session: (high, low, close) in time order."""
        for high, low, close in bars:
            self.observe(high, low, close)
