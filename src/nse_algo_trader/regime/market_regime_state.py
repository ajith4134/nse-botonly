"""What a regime IS, shared by every classifier and by the brain that combines them.

Four classifiers answer "what kind of market is this?" from four different mathematical
directions. They can only be combined if they answer in the same currency, so every one
of them emits a **probability distribution over the same regime labels** — never a bare
label. That is the design decision this module exists to enforce.

A bare label discards exactly the information that matters most. "Ranging" and "ranging
at 51% against trending at 49%" lead to opposite decisions, and a classifier that returns
only the winner has already thrown away the fact that it was nearly a coin flip. At
regime transitions — which is precisely when a strategy is most likely to be wrong — that
discarded uncertainty is the whole signal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

PROBABILITY_SUM_TOLERANCE = 1e-9


class MarketRegime(Enum):
    """The shared vocabulary. Deliberately small.

    Four labels, not twelve: each additional regime divides the same finite history into
    thinner slices, and a regime with too few observations to estimate is a regime that
    contributes noise rather than knowledge. These four are the ones the corpus's own
    strategy families actually condition on.
    """

    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    QUIET = "quiet"

    @classmethod
    def uniform_probabilities(cls) -> dict[MarketRegime, float]:
        """Maximum ignorance — what a classifier says when it genuinely does not know."""
        share = 1.0 / len(cls)
        return dict.fromkeys(cls, share)


class RegimeDistributionError(ValueError):
    """Raised when a distribution is not one — a defect, never something to normalise
    away silently, because a classifier that cannot sum to one is miscomputing."""


@dataclass(frozen=True)
class RegimeDistribution:
    """A probability distribution over `MarketRegime`, validated at construction."""

    probabilities: Mapping[MarketRegime, float]

    def __post_init__(self) -> None:
        missing = set(MarketRegime) - set(self.probabilities)
        if missing:
            raise RegimeDistributionError(
                f"distribution is missing {sorted(regime.value for regime in missing)}"
            )
        for regime, probability in self.probabilities.items():
            if not math.isfinite(probability) or probability < 0.0:
                raise RegimeDistributionError(
                    f"{regime.value} has a non-finite or negative probability {probability!r}"
                )
        total = sum(self.probabilities.values())
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise RegimeDistributionError(f"probabilities sum to {total!r}, not 1.0")

    @classmethod
    def from_scores(cls, scores: Mapping[MarketRegime, float]) -> RegimeDistribution:
        """Normalise non-negative scores into a distribution.

        All-zero scores become uniform rather than raising: a classifier legitimately
        has no opinion on flat, featureless data, and "no opinion" is maximum entropy,
        not an error.
        """
        cleaned = {regime: max(0.0, float(scores.get(regime, 0.0))) for regime in MarketRegime}
        total = sum(cleaned.values())
        if total <= 0.0:
            return cls(MarketRegime.uniform_probabilities())
        return cls({regime: value / total for regime, value in cleaned.items()})

    @classmethod
    def from_scores_over_axis(
        cls, scores: Mapping[MarketRegime, float], silent_on: Sequence[MarketRegime]
    ) -> RegimeDistribution:
        """Scores over the regimes a classifier MEASURES, uniform over the rest.

        The distinction is load-bearing and cost a full real-data pass to find. A
        classifier that measures direction has no basis for an opinion about dispersion,
        but assigning those regimes **zero** does not say "no opinion" — zero is the
        strongest possible claim, that the market is *definitely not* volatile. Blending
        four such distributions drags every combined belief toward uniform, and the brain
        could then never clear any confidence bar: measured on real bars, 100% abstain.

        Silence is therefore spread as uniform mass over the unmeasured regimes, which is
        what "I do not know about this axis" actually means. The pooling in `L11.03` then
        lets the classifier that DOES measure that axis carry it.
        """
        silent = set(silent_on)
        measured = {
            regime: max(0.0, float(scores.get(regime, 0.0)))
            for regime in MarketRegime
            if regime not in silent
        }
        measured_total = sum(measured.values())
        if not silent:
            return cls.from_scores(measured)
        # Half the mass to the measured axis, half spread over the silent one: an equal
        # split is the neutral choice when a classifier speaks to one axis of two.
        measured_share = 0.5 if measured_total > 0.0 else 0.0
        silent_share = 1.0 - measured_share
        probabilities = (
            {regime: (measured[regime] / measured_total) * measured_share for regime in measured}
            if measured_total > 0.0
            else {}
        )
        for regime in silent:
            probabilities[regime] = silent_share / len(silent)
        for regime in MarketRegime:
            probabilities.setdefault(regime, 0.0)
        return cls(probabilities)

    @classmethod
    def certain(cls, regime: MarketRegime) -> RegimeDistribution:
        """A degenerate distribution — used only where a classifier is genuinely certain,
        such as a structural fact like the time of day."""
        return cls({member: (1.0 if member is regime else 0.0) for member in MarketRegime})

    def probability_of(self, regime: MarketRegime) -> float:
        return self.probabilities[regime]

    @property
    def most_likely(self) -> MarketRegime:
        return max(self.probabilities.items(), key=lambda item: (item[1], item[0].value))[0]

    @property
    def shannon_entropy(self) -> float:
        """Uncertainty in bits. Zero when certain, 2.0 when uniform over four regimes.

        The brain uses this rather than "confidence" because entropy is comparable
        across classifiers that arrive at their beliefs by entirely different means.
        """
        return -sum(
            probability * math.log2(probability)
            for probability in self.probabilities.values()
            if probability > 0.0
        )

    @property
    def normalised_entropy(self) -> float:
        """Entropy on [0, 1] — 1.0 means "I have no idea"."""
        maximum = math.log2(len(MarketRegime))
        return self.shannon_entropy / maximum if maximum else 0.0


@dataclass(frozen=True)
class RegimeOpinion:
    """One classifier's answer at one instant, with everything needed to judge it later.

    `is_mature` is the `R.04` maturity ladder made explicit: an engine computes its full
    algorithm from the first bar, but says plainly when it has not yet seen enough data
    for the answer to mean anything. The brain down-weights immature opinions rather than
    the classifier silently degrading itself.
    """

    classifier_name: str
    distribution: RegimeDistribution
    observed_at: datetime
    is_mature: bool
    observations_seen: int
    evidence: str = ""

    @property
    def confidence(self) -> float:
        """1 - normalised entropy. An immature opinion reports zero confidence rather
        than a number it has not earned."""
        return 0.0 if not self.is_mature else 1.0 - self.distribution.normalised_entropy
