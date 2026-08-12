"""`L13.01` read model — what the regime brain ACTUALLY is, measured, never described.

`R.08` says a feature's dashboard status must be **measured from real code and server
state, never hand-authored**. That rules out the obvious implementation, which is a
template listing the four classifiers with a hardcoded "armed" badge beside each. A
hand-typed status is a claim about the system that drifts the moment the system changes,
and a dashboard that lies is worse than no dashboard: it converts "nobody knows" into
"everybody believes something false".

So this read model **instantiates the real engines and runs them over real bars**. Every
number the page shows is the value the engine actually holds:

- armed/unarmed comes from `SoftRegimeWeightingBrain.armed_classifiers`, not a config file
- maturity comes from each classifier's own `is_mature`, so an engine that has not seen
  enough data says so itself
- the combined belief is the real pooled distribution
- the decision is what `IntradayMeanReversionEngine` would genuinely return

If an engine is deleted or renamed, this fails loudly rather than rendering a stale row —
which is `L13.06`'s auto-appear-or-fail-the-audit property applied to one surface.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeOpinion
from nse_algo_trader.regime.markov_switching_regime_model import MarkovSwitchingRegimeModel
from nse_algo_trader.regime.session_phase_regime_classifier import (
    SessionPhaseRegimeClassifier,
)
from nse_algo_trader.regime.soft_regime_weighting_brain import (
    RegimeBelief,
    SoftRegimeWeightingBrain,
)
from nse_algo_trader.regime.trend_strength_regime_classifier import (
    TrendStrengthRegimeClassifier,
)
from nse_algo_trader.regime.volatility_regime_classifier import VolatilityRegimeClassifier
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionDecision,
)

DEFAULT_MARKET_DATA_PATH = Path("/home/opc/.nse_algo_trader/market_data.sqlite3")

TRAINING_FRACTION = 0.6
"""The Markov model is fitted on a PREFIX and applied forward, so the dashboard shows
the same leakage-free inference the engine performs in production rather than a
flattering in-sample fit."""


class RegimeReadModelError(RuntimeError):
    """Raised when the read model cannot measure real state.

    Deliberately fatal. Rendering a page with a placeholder where a measurement failed
    would produce exactly the hand-authored status `R.08` forbids.
    """


@dataclass(frozen=True)
class ClassifierPanel:
    """One classifier's measured state — every field read off the live object."""

    name: str
    is_armed: bool
    is_mature: bool
    observations_seen: int
    probabilities: dict[MarketRegime, float]
    confidence: float
    weight_in_belief: float
    evidence: str

    @property
    def status_label(self) -> str:
        """Never colour alone: the badge carries a word, per the accessibility pass."""
        if not self.is_mature:
            return "immature"
        return "armed" if self.is_armed else "unarmed"


@dataclass(frozen=True)
class RegimeBrainSnapshot:
    """Everything the surface renders, all of it measured."""

    instrument_token: int
    bars_used: int
    measured_at: datetime
    panels: tuple[ClassifierPanel, ...]
    belief: RegimeBelief
    decision: MeanReversionDecision
    deviation_band: float | None

    @property
    def armed_count(self) -> int:
        return sum(1 for panel in self.panels if panel.is_armed)

    @property
    def contributing_count(self) -> int:
        return len(self.belief.contributing_classifiers)


def _load_bars(
    database_path: Path, instrument_token: int | None, minimum_bars: int
) -> tuple[int, list[tuple[str, float, float, float]]]:
    """Real bars from the retained store. Raises rather than fabricating a series."""
    if not database_path.exists():
        raise RegimeReadModelError(f"no market data at {database_path}")
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        if instrument_token is None:
            row = connection.execute(
                "SELECT instrument_token FROM price_bars GROUP BY instrument_token "
                "HAVING COUNT(*) > ? ORDER BY COUNT(*) DESC LIMIT 1",
                (minimum_bars,),
            ).fetchone()
            if row is None:
                raise RegimeReadModelError(f"no instrument has more than {minimum_bars} bars")
            instrument_token = int(row[0])
        bars = connection.execute(
            "SELECT bar_timestamp, high_price, low_price, close_price FROM price_bars "
            "WHERE instrument_token = ? ORDER BY bar_timestamp",
            (instrument_token,),
        ).fetchall()
    finally:
        connection.close()
    if not bars:
        raise RegimeReadModelError(f"instrument {instrument_token} has no bars")
    return instrument_token, [(str(t), float(h), float(low), float(c)) for t, h, low, c in bars]


def measure_regime_brain(
    database_path: Path = DEFAULT_MARKET_DATA_PATH,
    instrument_token: int | None = None,
    armed_classifiers: Sequence[str] = ("trend_strength",),
    minimum_regime_concentration: float = 0.15,
    minimum_regime_agreement: float = 0.5,
    minimum_bars: int = 400,
) -> RegimeBrainSnapshot:
    """Run the real engines over real bars and report exactly what they hold.

    `armed_classifiers` defaults to one, per `A.08`: all four are built, one is armed,
    and the surface shows the other three computing and earning their reliability while
    contributing zero weight — which is the state the rule describes, made visible.
    """
    instrument_token, bars = _load_bars(database_path, instrument_token, minimum_bars)
    closes = [close for _t, _h, _l, close in bars]
    returns = [
        math.log(closes[index] / closes[index - 1])
        for index in range(1, len(closes))
        if closes[index] > 0 and closes[index - 1] > 0
    ]

    trend = TrendStrengthRegimeClassifier()
    volatility = VolatilityRegimeClassifier()
    session = SessionPhaseRegimeClassifier()
    markov = MarkovSwitchingRegimeModel()
    markov.fit_on_history(returns[: int(len(returns) * TRAINING_FRACTION)])

    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=minimum_regime_concentration,
        minimum_regime_agreement=minimum_regime_agreement,
    )
    brain = SoftRegimeWeightingBrain()
    for classifier_name in armed_classifiers:
        brain.arm(classifier_name)

    latest_moment = datetime.now(UTC)
    for timestamp, high, low, close in bars:
        moment = _parse_moment(timestamp)
        trend.observe(high, low, close)
        volatility.observe(close)
        session.observe(moment, high, low, close)
        engine.observe(close)
        latest_moment = moment

    opinions: list[RegimeOpinion] = [
        trend.opinion(latest_moment),
        volatility.opinion(latest_moment),
        session.opinion(latest_moment),
    ]
    if markov.is_mature:
        opinions.append(markov.opinion(returns, latest_moment))

    belief = brain.combine(opinions, latest_moment)
    decision = engine.decide(belief, latest_moment)

    panels = tuple(
        ClassifierPanel(
            name=opinion.classifier_name,
            is_armed=brain.is_armed(opinion.classifier_name),
            is_mature=opinion.is_mature,
            observations_seen=opinion.observations_seen,
            probabilities=dict(opinion.distribution.probabilities),
            confidence=opinion.confidence,
            weight_in_belief=belief.weights.get(opinion.classifier_name, 0.0),
            evidence=opinion.evidence,
        )
        for opinion in opinions
    )

    return RegimeBrainSnapshot(
        instrument_token=instrument_token,
        bars_used=len(bars),
        measured_at=datetime.now(UTC),
        panels=panels,
        belief=belief,
        decision=decision,
        deviation_band=engine.deviation_band(),
    )


def _parse_moment(timestamp: str) -> datetime:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        moment = datetime.fromtimestamp(float(timestamp), UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
