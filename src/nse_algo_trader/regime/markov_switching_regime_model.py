"""`L11.02` — Markov-switching regime model with FILTERED, leakage-free beliefs.

The probabilistic member of the panel. Where the trend classifier measures the geometry
of the path, this one posits that an unobservable state generates the returns and infers
`P(state | data)` from the data itself. Neither method can be derived from the other,
which is the point of running both.

**Two separate lookahead traps, and both are avoided explicitly.**

*Filtered, never smoothed.* `P(state_t | data up to t)` is filtered; `P(state_t | ALL
data)` is smoothed. Smoothed probabilities are what most examples reach for, and they
condition on the future — using them in a backtest is straightforward lookahead. This
model uses `filtered_marginal_probabilities` only. `hmmlearn`'s `predict_proba` returns
the smoothed quantity, which is one reason `statsmodels` is used here instead; the
other is that statsmodels exposes the filtered series directly.

*Parameters fitted on a PREFIX, then applied forward.* A subtler trap: even filtered
probabilities leak if the transition matrix and emission parameters were estimated over
the whole series, because the parameters then encode the future. So `fit_on_history`
takes a training prefix, and `filtered_beliefs` applies those frozen parameters to later
data via `MarkovRegression.filter`. The separation is the whole reason this is a model
rather than a curve fit.

**State carried:** the fitted parameters persist across calls, and the filter carries the
belief vector forward bar by bar.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)

REGIME_COUNT = 2
"""Two latent states: the model separates a calm generating process from a turbulent one.
More states divide the same history into thinner slices and estimate worse — and the
panel already carries direction and session structure on other axes."""

MINIMUM_RETURNS_TO_FIT = 120
"""Below this a two-state switching model is fitting noise. The engine still runs in
full and reports itself immature rather than shrinking the algorithm (`R.04`)."""


class MarkovRegimeModelError(RuntimeError):
    """Raised when the model is asked for a belief it has not earned the right to give."""


@dataclass
class MarkovSwitchingRegimeModel:
    """A two-state Markov-switching model over bar returns."""

    name: str = "markov_switching"
    _fitted_parameters: np.ndarray | None = None
    _training_returns: int = 0
    _high_variance_state: int = 0
    _latest_filtered: tuple[float, float] | None = None

    @property
    def is_fitted(self) -> bool:
        return self._fitted_parameters is not None

    @property
    def is_mature(self) -> bool:
        return self.is_fitted and self._training_returns >= MINIMUM_RETURNS_TO_FIT

    def fit_on_history(self, returns: Sequence[float]) -> None:
        """Estimate parameters on a TRAINING PREFIX only.

        Deliberately separate from inference: fitting over data the decision will later
        be evaluated on encodes the future into the parameters, which no amount of
        filtered inference afterwards can undo.
        """
        series = np.asarray(list(returns), dtype=float)
        if series.size < MINIMUM_RETURNS_TO_FIT:
            self._training_returns = int(series.size)
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = MarkovRegression(
                series, k_regimes=REGIME_COUNT, trend="c", switching_variance=True
            )
            result = model.fit(disp=False)
        self._fitted_parameters = np.asarray(result.params, dtype=float)
        self._training_returns = int(series.size)
        # Which latent state is the turbulent one is an OUTPUT of fitting, never an
        # assumption — the labels are arbitrary and swap between runs.
        # Locate the variance parameters BY NAME rather than by position. With a numpy
        # input `result.params` is a bare ndarray whose layout is a statsmodels internal
        # detail, and hardcoding indices would silently mislabel the states if that
        # layout ever changed — which would invert every belief this model emits.
        parameter_names = list(result.model.param_names)
        variance_positions = [
            index for index, name in enumerate(parameter_names) if name.startswith("sigma2")
        ]
        if len(variance_positions) != REGIME_COUNT:
            raise MarkovRegimeModelError(
                f"expected {REGIME_COUNT} variance parameters, found {variance_positions} "
                f"in {parameter_names}"
            )
        variances = [float(result.params[index]) for index in variance_positions]
        self._high_variance_state = int(np.argmax(variances))

    def filtered_beliefs(self, returns: Sequence[float]) -> np.ndarray:
        """`P(state | data up to each point)` using the FROZEN training parameters."""
        if self._fitted_parameters is None:
            raise MarkovRegimeModelError(
                "model has no parameters — call fit_on_history on a training prefix first"
            )
        series = np.asarray(list(returns), dtype=float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = MarkovRegression(
                series, k_regimes=REGIME_COUNT, trend="c", switching_variance=True
            )
            filtered = model.filter(self._fitted_parameters)
        probabilities = np.asarray(filtered.filtered_marginal_probabilities, dtype=float)
        if probabilities.ndim == 1:
            probabilities = probabilities.reshape(-1, REGIME_COUNT)
        self._latest_filtered = (
            float(probabilities[-1][0]),
            float(probabilities[-1][1]),
        )
        return probabilities

    def opinion(self, returns: Sequence[float], observed_at: datetime) -> RegimeOpinion:
        """The panel-facing belief: turbulent state -> volatile, calm state -> quiet.

        The model speaks only on the dispersion axis, because that is what a
        switching-variance model actually estimates. Claiming a view on trend as well
        would be asserting something the likelihood never measured.
        """
        if not self.is_fitted:
            return RegimeOpinion(
                classifier_name=self.name,
                distribution=RegimeDistribution(MarketRegime.uniform_probabilities()),
                observed_at=observed_at,
                is_mature=False,
                observations_seen=self._training_returns,
                evidence=f"unfitted: {self._training_returns} training returns",
            )

        probabilities = self.filtered_beliefs(returns)
        turbulent = float(probabilities[-1][self._high_variance_state])
        calm = 1.0 - turbulent
        return RegimeOpinion(
            classifier_name=self.name,
            distribution=RegimeDistribution.from_scores_over_axis(
                {MarketRegime.VOLATILE: turbulent, MarketRegime.QUIET: calm},
                silent_on=(MarketRegime.TRENDING, MarketRegime.RANGING),
            ),
            observed_at=observed_at,
            is_mature=self.is_mature,
            observations_seen=self._training_returns,
            evidence=(
                f"filtered P(turbulent)={turbulent:.3f} "
                f"(fitted on {self._training_returns} returns, applied forward)"
            ),
        )
