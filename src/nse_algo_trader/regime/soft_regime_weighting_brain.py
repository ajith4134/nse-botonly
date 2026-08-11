"""`L11.03` — soft weighting over four classifiers, and arming that costs no code change.

The brain does **not** pick a winner. Hard switching throws away ambiguity precisely
when ambiguity matters most: at a regime transition, which is exactly when a strategy is
most likely to be wrong. A brain that reports "ranging" at 51% and one that reports
"ranging" at 99% should not produce the same trade, and a hard switch makes them
identical.

**Three things it does that a weighted average would not:**

1. **Reliability is measured, not assigned.** Each classifier's weight is its own
   track record — the Brier skill of its past opinions against what actually happened.
   With no history every classifier is equal, which is the honest prior. `R.03`: nothing
   here is a typed-in weight.
2. **Disagreement is a first-class output, not noise to smooth.** When the panel splits,
   that is the brain saying *I do not know*, and a consumer is expected to abstain on it.
   Averaging four confident, contradictory opinions produces a confident-looking mush
   that has lost the only signal that mattered.
3. **Arming is a flag, never a code path.** Per `A.08` all four engines are built and
   ONE is armed; unarmed engines still compute and still record opinions — that is how
   they earn the reliability estimate that qualifies them — but contribute **zero
   weight**. Arming later changes a boolean, not a line of logic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
    RegimeOpinion,
)

_FLOOR = 1e-12
"""Keeps `log(0)` finite. A classifier that assigns a regime exactly zero is making an
absolute claim, and one such claim would otherwise veto that regime for the whole panel
however many other engines disagreed."""

IMMATURE_CLASSIFIER_WEIGHT = 0.0
"""An immature classifier contributes nothing. Not a small weight — nothing. A number
computed from too little data is not a weak opinion, it is an unfounded one."""


@dataclass
class ClassifierReliability:
    """A classifier's earned trust, from the Brier score of its own past opinions.

    Brier rather than accuracy, because these classifiers emit distributions: a
    classifier that says 60% and is right deserves less credit than one that says 95%
    and is right, and accuracy cannot see the difference.
    """

    total_brier_score: float = 0.0
    scored_opinions: int = 0

    def record_outcome(
        self, distribution: RegimeDistribution, actual: MarketRegime
    ) -> None:
        """Score one past opinion against what the market turned out to be."""
        self.total_brier_score += sum(
            (probability - (1.0 if regime is actual else 0.0)) ** 2
            for regime, probability in distribution.probabilities.items()
        )
        self.scored_opinions += 1

    @property
    def mean_brier_score(self) -> float | None:
        if not self.scored_opinions:
            return None
        return self.total_brier_score / self.scored_opinions

    @property
    def skill(self) -> float:
        """Skill on [0, 1] against the uninformed baseline.

        A uniform guess over four regimes scores 0.75. Anything at or worse than that is
        no skill at all, and gets no weight — which is how a classifier that has been
        systematically wrong removes itself without anyone intervening.
        """
        mean_score = self.mean_brier_score
        if mean_score is None:
            return 1.0  # no history: equal footing, the honest prior
        uninformed_baseline = 1.0 - 1.0 / len(MarketRegime)
        if uninformed_baseline <= 0.0:
            return 1.0
        return max(0.0, (uninformed_baseline - mean_score) / uninformed_baseline)


@dataclass(frozen=True)
class RegimeBelief:
    """The brain's combined answer, with the panel's disagreement kept visible."""

    distribution: RegimeDistribution
    contributing_classifiers: tuple[str, ...]
    disagreement: float
    """Mean pairwise total-variation distance between contributing opinions, on [0, 1].
    High disagreement means the panel is split — a consumer should size down or abstain,
    and this is deliberately NOT folded into the distribution where it would vanish."""

    weights: Mapping[str, float]
    observed_at: datetime

    @property
    def most_likely(self) -> MarketRegime | None:
        """The leading regime, or **None** when no classifier contributed.

        None rather than a label, deliberately. With nothing armed and mature the
        distribution is uniform, and asking a uniform distribution for its maximum
        returns whichever label wins an arbitrary tie-break — "volatile", as it happens.
        A caller that acted on that would be trading on alphabetical order. Returning
        None makes the abstain state impossible to mistake for an opinion.
        """
        if not self.contributing_classifiers:
            return None
        return self.distribution.most_likely

    @property
    def concentration(self) -> float:
        """How peaked the combined belief is, on [0, 1]. 1 = certain, 0 = no idea."""
        return 1.0 - self.distribution.normalised_entropy

    @property
    def agreement(self) -> float:
        """How much the panel agreed, on [0, 1]. 1 = unanimous, 0 = fully split."""
        return 1.0 - self.disagreement

    def is_actionable(self, minimum_concentration: float, minimum_agreement: float) -> bool:
        """Whether a consumer should act, against ITS OWN thresholds.

        The thresholds are arguments, not constants, because how much certainty a
        decision needs is a property of the decision — a mean-reversion entry and a
        risk-off veto do not need the same bar, and baking one number here would impose
        the wrong one on both (`R.03`).

        Both conditions are required: four classifiers can each be individually certain
        and mutually contradictory, which is a confident-looking average built on a
        panel that has no idea.
        """
        return (
            self.concentration >= minimum_concentration
            and self.agreement >= minimum_agreement
        )


def total_variation_distance(
    left: RegimeDistribution, right: RegimeDistribution
) -> float:
    """Half the L1 distance — 0 when identical, 1 when disjoint."""
    return 0.5 * sum(
        abs(left.probability_of(regime) - right.probability_of(regime))
        for regime in MarketRegime
    )


@dataclass
class SoftRegimeWeightingBrain:
    """Combines the panel into one belief, weighted by measured reliability."""

    armed_classifiers: set[str] = field(default_factory=set)
    reliability: dict[str, ClassifierReliability] = field(default_factory=dict)

    def arm(self, classifier_name: str) -> None:
        """Give a classifier real influence. A flag flip, per `A.08` — no code change."""
        self.armed_classifiers.add(classifier_name)

    def disarm(self, classifier_name: str) -> None:
        self.armed_classifiers.discard(classifier_name)

    def is_armed(self, classifier_name: str) -> bool:
        return classifier_name in self.armed_classifiers

    def record_outcome(self, opinion: RegimeOpinion, actual: MarketRegime) -> None:
        """Let a classifier earn (or lose) trust. Unarmed engines are scored too — that
        is precisely how an unarmed engine qualifies to be armed."""
        self.reliability.setdefault(
            opinion.classifier_name, ClassifierReliability()
        ).record_outcome(opinion.distribution, actual)

    def weight_for(self, opinion: RegimeOpinion) -> float:
        """A classifier's influence: zero unless armed AND mature, else skill x confidence."""
        if not self.is_armed(opinion.classifier_name):
            return 0.0
        if not opinion.is_mature:
            return IMMATURE_CLASSIFIER_WEIGHT
        skill = self.reliability.get(
            opinion.classifier_name, ClassifierReliability()
        ).skill
        # Confidence scales the vote within a classifier; skill scales it between
        # classifiers. A trusted engine that is unsure this bar should not shout.
        return skill * max(opinion.confidence, 0.0)

    def combine(
        self, opinions: Sequence[RegimeOpinion], observed_at: datetime
    ) -> RegimeBelief:
        """The panel's combined belief, or honest ignorance when nothing qualifies."""
        weighted = [
            (opinion, self.weight_for(opinion))
            for opinion in opinions
            if self.weight_for(opinion) > 0.0
        ]

        if not weighted:
            # Nothing armed, mature and confident. Uniform is the correct answer — an
            # abstain signal a consumer can act on, not a guess dressed as a belief.
            return RegimeBelief(
                distribution=RegimeDistribution(MarketRegime.uniform_probabilities()),
                contributing_classifiers=(),
                disagreement=0.0,
                weights={},
                observed_at=observed_at,
            )

        # LOG-LINEAR pooling (a weighted product of experts), not a linear average.
        #
        # This was changed after a real-data pass measured 100% abstain. Linear averaging
        # of experts that each speak to a different axis can only ever move the result
        # TOWARD uniform: averaging a direction-only opinion with a dispersion-only one
        # gives something flatter than either. The brain could then never be confident,
        # by construction rather than because the market was ambiguous.
        #
        # A product of experts sharpens where independent experts AGREE and flattens
        # where they conflict — which is exactly the behaviour the panel exists for, and
        # it is also the correct combination rule for genuinely independent evidence.
        total_weight = sum(weight for _opinion, weight in weighted)
        log_scores = {
            regime: sum(
                weight * math.log(max(opinion.distribution.probability_of(regime), _FLOOR))
                for opinion, weight in weighted
            )
            / total_weight
            for regime in MarketRegime
        }
        # Subtract the max before exponentiating: without it a confident panel underflows
        # to all-zeros and the belief silently becomes uniform — the very failure this
        # pooling rule was introduced to fix.
        largest = max(log_scores.values())
        blended_scores = {
            regime: math.exp(score - largest) for regime, score in log_scores.items()
        }
        blended_total = sum(blended_scores.values())
        blended = {
            regime: score / blended_total for regime, score in blended_scores.items()
        }

        contributing = [opinion for opinion, _weight in weighted]
        pairwise = [
            total_variation_distance(left.distribution, right.distribution)
            for index, left in enumerate(contributing)
            for right in contributing[index + 1 :]
        ]
        return RegimeBelief(
            distribution=RegimeDistribution(blended),
            contributing_classifiers=tuple(
                opinion.classifier_name for opinion in contributing
            ),
            disagreement=sum(pairwise) / len(pairwise) if pairwise else 0.0,
            weights={
                opinion.classifier_name: weight / total_weight
                for opinion, weight in weighted
            },
            observed_at=observed_at,
        )
