"""Volatility regime — dispersion against an instrument's OWN recent history.

The panel's fourth member, and the one that decides whether an edge survives contact
with costs. A mean-reversion signal can be statistically real and still unprofitable in
a wide-spread, high-volatility tape, so a panel that only measured direction would
route confidently into exactly the conditions that lose money.

**Self-referential by design, unlike the trend classifier.** "High volatility" has no
absolute meaning across an NSE universe spanning a Rs 20 microcap and RELIANCE — the
measured cross-sectional spread in this project's own depth data was 130x. So the
classifier compares realised volatility against **this instrument's own** recent
distribution, via a streaming quantile. Nothing is thresholded at a typed-in sigma.

The maturity ladder matters here more than elsewhere: a quantile estimated from twenty
observations is noise wearing a number's clothes, so the classifier reports itself
immature until it has enough, and the brain discounts it (`R.04`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from river import stats

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)

QUIET_QUANTILE = 0.25
VOLATILE_QUANTILE = 0.75
"""The instrument's own quartiles. Quartiles are a definition of 'unusual for this
name', not a tuned cutoff — and being quantiles they adapt to whatever scale the
instrument actually trades on."""

MINIMUM_OBSERVATIONS_FOR_MATURITY = 40
"""Enough for quartiles to mean something. Below this the algorithm still runs in full
and simply declares itself immature (`R.04`)."""

RETURN_WINDOW = 20

MINIMUM_RETURNS_FOR_VARIANCE = 2
"""A sample variance needs two observations; with one there is nothing to disperse."""


@dataclass
class VolatilityRegimeClassifier:
    """Streaming realised-volatility regime, judged against the instrument's own past."""

    name: str = "volatility"
    _returns: list[float] = field(default_factory=list)
    _quiet_quantile: stats.Quantile = field(default_factory=lambda: stats.Quantile(QUIET_QUANTILE))
    _volatile_quantile: stats.Quantile = field(
        default_factory=lambda: stats.Quantile(VOLATILE_QUANTILE)
    )
    _observations: int = 0
    _previous_close: float | None = None
    _latest_volatility: float = 0.0

    @property
    def is_mature(self) -> bool:
        return self._observations >= MINIMUM_OBSERVATIONS_FOR_MATURITY

    @property
    def latest_realised_volatility(self) -> float:
        return self._latest_volatility

    def observe(self, close: float) -> None:
        """Take one close. Realised volatility is the rolling standard deviation of
        log returns — the same quantity an option desk would call realised vol."""
        if self._previous_close is not None and self._previous_close > 0.0 and close > 0.0:
            self._returns.append(math.log(close / self._previous_close))
            if len(self._returns) > RETURN_WINDOW:
                self._returns = self._returns[-RETURN_WINDOW:]
            if len(self._returns) >= MINIMUM_RETURNS_FOR_VARIANCE:
                mean_return = sum(self._returns) / len(self._returns)
                variance = sum((value - mean_return) ** 2 for value in self._returns) / (
                    len(self._returns) - 1
                )
                self._latest_volatility = math.sqrt(max(0.0, variance))
                # Learn the instrument's own distribution AFTER using the current
                # estimate, so a reading can never be the reason it is judged normal.
                self._quiet_quantile.update(self._latest_volatility)  # type: ignore[no-untyped-call]
                self._volatile_quantile.update(self._latest_volatility)  # type: ignore[no-untyped-call]
                self._observations += 1
        self._previous_close = close

    def observe_closes(self, closes: Sequence[float]) -> None:
        for close in closes:
            self.observe(close)

    def opinion(self, observed_at: datetime) -> RegimeOpinion:
        quiet_level = self._quiet_quantile.get()  # type: ignore[no-untyped-call]
        volatile_level = self._volatile_quantile.get()  # type: ignore[no-untyped-call]
        current = self._latest_volatility

        if not self.is_mature or quiet_level is None or volatile_level is None:
            distribution = RegimeDistribution(MarketRegime.uniform_probabilities())
            evidence = f"immature: {self._observations} observations"
        else:
            span = max(float(volatile_level) - float(quiet_level), 0.0)
            if span <= 0.0:
                # Zero dispersion in the instrument's own history — genuinely quiet.
                distribution = RegimeDistribution.from_scores_over_axis(
                    {MarketRegime.QUIET: 1.0, MarketRegime.VOLATILE: 0.0},
                    silent_on=(MarketRegime.TRENDING, MarketRegime.RANGING),
                )
            else:
                position = (current - float(quiet_level)) / span
                # Dispersion says nothing about direction, so this classifier is
                # UNIFORM on the trend axis rather than claiming zero there.
                distribution = RegimeDistribution.from_scores_over_axis(
                    {
                        MarketRegime.VOLATILE: max(0.0, position),
                        MarketRegime.QUIET: max(0.0, 1.0 - position),
                    },
                    silent_on=(MarketRegime.TRENDING, MarketRegime.RANGING),
                )
            evidence = (
                f"vol={current:.6f} q25={float(quiet_level):.6f} q75={float(volatile_level):.6f}"
            )

        return RegimeOpinion(
            classifier_name=self.name,
            distribution=distribution,
            observed_at=observed_at,
            is_mature=self.is_mature,
            observations_seen=self._observations,
            evidence=evidence,
        )
