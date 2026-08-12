"""`L5.05` — intraday mean reversion, conditioned on the regime brain.

The panel's first consumer, and the reason `L11.03` is not an orphan (`R.06`). Per `A.07`
this is the first armed strategy family: intraday mean-reversion on cash equity.

**Mean reversion is regime-conditional by nature, which is the whole point of the
wiring.** The same two-sigma deviation is an opportunity in a ranging tape and a warning
in a trending one: in a trend, "unusually far from the mean" is just what a trend
looks like early. So the brain's output does not decorate the decision, it *inverts* it:

- ranging weight high, panel agreed  ->  entries permitted at the derived band
- trending weight high              ->  **abstain**, however extreme the deviation
- panel split (low agreement)       ->  **abstain**, because an unknown regime is not
                                        a tradeable one

**Every band is derived (`R.03`).** The deviation threshold is a quantile of *this
instrument's own* realised deviations, not a typed-in sigma — a two-sigma move means something
different on a Rs 20 microcap than on RELIANCE, and this project has already measured a
130x cross-sectional spread. The only typed-in numbers are the operator's policy inputs,
which are arguments rather than constants.

**What this does NOT do**, so it is not mistaken for finished: it emits a decision and a
conviction, never an order size. `L1` (costs, net-EV gate, position sizing) is unbuilt,
and `R.13` is explicit that a correct signal is not a correct allocation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from river import stats

from nse_algo_trader.regime.market_regime_state import MarketRegime
from nse_algo_trader.regime.soft_regime_weighting_brain import RegimeBelief

MINIMUM_OBSERVATIONS_FOR_BANDS = 60
"""Below this a deviation quantile is noise. The algorithm runs in full and gates only
ACTIVATION, per `R.04` — thin data never shrinks the engine."""

DEVIATION_BAND_QUANTILE = 0.9
"""'Unusually far for this instrument' defined as its own 90th percentile deviation. A
quantile is a definition of extremity, not a tuned cutoff, and it adapts to whatever
scale the instrument trades on."""


class MeanReversionAction(Enum):
    """What the engine decided. Abstain is a first-class decision, not a null."""

    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class MeanReversionDecision:
    """A decision, its conviction, and WHY — including why it declined."""

    action: MeanReversionAction
    conviction: float
    reason: str
    deviation: float
    deviation_band: float | None
    regime_used: MarketRegime | None

    @property
    def is_actionable(self) -> bool:
        return self.action is not MeanReversionAction.ABSTAIN


@dataclass
class IntradayMeanReversionEngine:
    """Deviation-from-mean entries, permitted or vetoed by the regime brain."""

    minimum_regime_concentration: float
    minimum_regime_agreement: float
    """Operator policy, required rather than defaulted: how sure the brain must be
    before this engine will act. Not a constant, because a different strategy family
    consuming the same brain legitimately needs a different bar."""

    name: str = "intraday_mean_reversion"
    _closes: list[float] = field(default_factory=list)
    _deviation_quantile: stats.Quantile = field(
        default_factory=lambda: stats.Quantile(DEVIATION_BAND_QUANTILE)
    )
    _observations: int = 0
    _rolling_window: int = 20

    @property
    def is_mature(self) -> bool:
        return self._observations >= MINIMUM_OBSERVATIONS_FOR_BANDS

    def observe(self, close: float) -> None:
        """Take one close and learn this instrument's own deviation distribution."""
        self._closes.append(close)
        if len(self._closes) > self._rolling_window:
            self._closes = self._closes[-self._rolling_window :]
        if len(self._closes) >= self._rolling_window:
            deviation = self._current_deviation()
            if deviation is not None and math.isfinite(deviation):
                self._deviation_quantile.update(abs(deviation))  # type: ignore[no-untyped-call]
                self._observations += 1

    def observe_closes(self, closes: Sequence[float]) -> None:
        for close in closes:
            self.observe(close)

    def _current_deviation(self) -> float | None:
        """Deviation from the rolling mean, in units of the rolling standard deviation.

        Scale-free on purpose: expressing the deviation in the instrument's own units of
        dispersion is what lets one engine serve a Rs 20 microcap and RELIANCE without a
        per-symbol parameter.
        """
        if len(self._closes) < self._rolling_window:
            return None
        mean_close = sum(self._closes) / len(self._closes)
        variance = sum((value - mean_close) ** 2 for value in self._closes) / (
            len(self._closes) - 1
        )
        dispersion = math.sqrt(max(0.0, variance))
        if dispersion <= 0.0:
            return 0.0
        return (self._closes[-1] - mean_close) / dispersion

    def deviation_band(self) -> float | None:
        """The instrument's own extreme-deviation level, or None while immature."""
        if not self.is_mature:
            return None
        band = self._deviation_quantile.get()  # type: ignore[no-untyped-call]
        return None if band is None else float(band)

    def decide(
        self, belief: RegimeBelief, observed_at: datetime | None = None
    ) -> MeanReversionDecision:
        """The decision the brain actually changes.

        Order matters: the regime veto is applied BEFORE the deviation is considered, so
        an extreme deviation in a trending tape can never talk its way past the veto.

        `observed_at` is accepted for symmetry with the classifiers and for future
        journalling; the decision itself depends only on carried state and the belief.
        """
        del observed_at
        deviation = self._current_deviation()
        band = self.deviation_band()

        if deviation is None or band is None:
            return MeanReversionDecision(
                MeanReversionAction.ABSTAIN,
                0.0,
                f"immature: {self._observations} deviation observations",
                deviation or 0.0,
                band,
                None,
            )

        if not belief.is_actionable(
            self.minimum_regime_concentration, self.minimum_regime_agreement
        ):
            return MeanReversionDecision(
                MeanReversionAction.ABSTAIN,
                0.0,
                f"regime not actionable: concentration={belief.concentration:.2f} "
                f"agreement={belief.agreement:.2f} — an unknown regime is not tradeable",
                deviation,
                band,
                belief.most_likely,
            )

        trending = belief.distribution.probability_of(MarketRegime.TRENDING)
        ranging = belief.distribution.probability_of(MarketRegime.RANGING)
        if trending >= ranging:
            return MeanReversionDecision(
                MeanReversionAction.ABSTAIN,
                0.0,
                f"trending weight {trending:.2f} >= ranging {ranging:.2f} — in a trend an "
                f"extreme deviation is the trend, not a reversion",
                deviation,
                band,
                belief.most_likely,
            )

        if abs(deviation) < band:
            return MeanReversionDecision(
                MeanReversionAction.ABSTAIN,
                0.0,
                f"deviation {deviation:.2f} inside this instrument's own band {band:.2f}",
                deviation,
                band,
                belief.most_likely,
            )

        # Conviction combines how far past its own band price is with how sure the panel
        # is. Both matter: a huge deviation in an uncertain regime is not a strong trade.
        excess = (abs(deviation) - band) / band if band > 0 else 0.0
        conviction = min(1.0, excess) * ranging * belief.agreement
        action = (
            MeanReversionAction.ENTER_LONG if deviation < 0 else MeanReversionAction.ENTER_SHORT
        )
        return MeanReversionDecision(
            action,
            conviction,
            f"deviation {deviation:.2f} beyond band {band:.2f} in a ranging regime "
            f"(P(ranging)={ranging:.2f}, agreement={belief.agreement:.2f})",
            deviation,
            band,
            belief.most_likely,
        )
