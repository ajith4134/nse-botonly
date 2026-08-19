"""The variance-risk-premium engine — what the two option bots (`L5.27`, `L5.28`) decide on.

**The claim being tested.** An option's premium embeds a forecast of how much the underlying will
move. That forecast is systematically richer than what the underlying subsequently delivers
— sellers are paid for carrying gap risk — but not always, and not by the same amount. The
tradeable question
is therefore never "is implied volatility high" but **"is implied volatility high RELATIVE TO WHAT
THIS UNDERLYING'S OWN PREMIUM USUALLY IS"**, a per-underlying self-calibrating quantity and
never a threshold anyone types (`R.03`).

**Three quantities, each measured:**

1. **Implied volatility**, inverted from the traded premium by `BlackScholesOptionAnalyticsEngine`
   using trading-session time to expiry. Never taken from a vendor field:
   `atm_implied_volatility_daily` exists in this project's store and holds 1,492 rows over nine
   days, which is a useful cross-check
   and far too thin to decide on.
2. **Realised volatility** of the underlying, as an exponentially-weighted standard deviation of log
   returns, annualised by the exchange's own session count. EWMA rather than a flat window because
   the quantity being forecast is the volatility over the option's REMAINING life, and a flat window
   weights a shock from forty sessions ago exactly as heavily as yesterday's.
3. **The premium**, `implied - realised`, standardised by that underlying's own history of the same
   difference. An online mean and variance, so a name whose options habitually trade five points
   over realised is not perpetually "rich".

**Why the standardisation is the engine rather than a detail.** Raw variance risk premium is not
comparable across underlyings: an index option at 12% implied against 10% realised and a midcap at
45% against 38% differ by 2 and 7 points respectively, and the second is the *less* unusual of the
two. Comparing them by raw difference allocates capital by volatility level, which is a bug that
looks like a strategy.

**What it deliberately does NOT do.** It never decides direction, never sizes, never clears its own
cost hurdle, and never proposes a structure. It answers one question — how unusual is this premium,
for this underlying, right now — and the bots turn that into a proposal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

OBSERVATIONS_NEEDED_TO_STANDARDISE = 5
"""Below this the standardising distribution is noise standing in for a distribution.

Five rather than two because the quantity standardised here is itself a difference of two estimates,
so its variance is the sum of theirs and converges more slowly than a raw price series.
"""

OBSERVATIONS_NEEDED_FOR_A_VARIANCE = 2
"""One observation has no deviation from its own mean; a variance needs a second point.

A definition of the statistic, not a tuning knob.
"""

RETURNS_NEEDED_FOR_REALISED_VOLATILITY = 3
"""Two log returns produce a standard deviation that is exactly half their difference — a number
with no information in it, which would then be compared against implied volatility as though it
were a measurement."""

DEFAULT_EWMA_HALF_LIFE_SESSIONS = 10.0
"""How fast the realised-volatility estimate forgets, in trading sessions.

The one modelling choice here, and it is stated rather than buried: ten sessions is two trading
weeks, which is the horizon of the weekly options this engine mostly sees. It is exposed as a
constructor argument so a bot on a different expiry cycle can carry a different memory, and the
half-life is converted to a decay rather than a decay being typed directly — a half-life is a
quantity a human can check against the instrument's expiry, and a decay of 0.9330 is not.
"""


class PremiumRichness(StrEnum):
    """What the standardised premium says, as a classification the bots can act on.

    `UNMEASURABLE` is a first-class answer and not an error: an underlying with no premium history,
    an option at expiry, or a quote below intrinsic all produce it, and every one of those occurs
    daily in the NSE chain.
    """

    RICH = "rich"
    FAIR = "fair"
    CHEAP = "cheap"
    UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True, slots=True)
class VarianceRiskPremiumReading:
    """One underlying's premium state, with every input kept visible.

    Every field is carried because a card that says only "RICH" cannot be argued with, and the
    decision trace contract (`L13.29`) requires the reasoning to be reconstructible afterwards.
    """

    implied_volatility: float | None
    realised_volatility: float | None
    premium: float | None
    standardised_premium: float | None
    richness: PremiumRichness
    observations: int

    @property
    def is_actionable(self) -> bool:
        return self.richness in (PremiumRichness.RICH, PremiumRichness.CHEAP)


@dataclass(slots=True)
class _OnlineMoments:
    """Welford's online mean and variance — numerically stable, and O(1) in memory.

    Welford rather than accumulating sums of squares: the naive form subtracts two large,
    nearly-equal numbers and can return a NEGATIVE variance on a long series of similar values,
    which is exactly
    the shape a premium series has.
    """

    count: int = 0
    mean: float = 0.0
    sum_of_squared_deviations: float = 0.0

    def update(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.sum_of_squared_deviations += delta * (value - self.mean)

    def standard_deviation(self) -> float | None:
        if self.count < OBSERVATIONS_NEEDED_FOR_A_VARIANCE:
            return None
        variance = self.sum_of_squared_deviations / (self.count - 1)
        if not math.isfinite(variance) or variance <= 0.0:
            return None
        return math.sqrt(variance)


@dataclass(slots=True)
class _ExponentiallyWeightedVolatility:
    """EWMA variance of log returns, annualised by the caller's session count.

    Seeded from the first observations rather than from zero: an EWMA started at zero reports a
    volatility climbing out of nothing for its first half-life, which reads as a calm market and is
    an artefact of the initial condition.
    """

    decay: float
    variance: float | None = None
    returns_seen: int = 0
    _seed: list[float] = field(default_factory=list)

    def update(self, log_return: float) -> None:
        if not math.isfinite(log_return):
            return
        self.returns_seen += 1
        if self.variance is None:
            self._seed.append(log_return)
            if len(self._seed) < RETURNS_NEEDED_FOR_REALISED_VOLATILITY:
                return
            mean = sum(self._seed) / len(self._seed)
            self.variance = sum((value - mean) ** 2 for value in self._seed) / (len(self._seed) - 1)
            return
        self.variance = self.decay * self.variance + (1.0 - self.decay) * log_return * log_return

    def annualised(self, sessions_per_year: int) -> float | None:
        if self.variance is None or self.variance <= 0.0 or sessions_per_year <= 0:
            return None
        annual = math.sqrt(self.variance * sessions_per_year)
        return annual if math.isfinite(annual) and annual > 0.0 else None


class VarianceRiskPremiumEngine:
    """Carries per-underlying realised volatility and premium history; reports how unusual today is.

    **SOTA analog:** the variance-risk-premium literature's standard construction (Carr-Wu), with
    the model-free VIX-style integration replaced by the at-the-money implied volatility this
    project can actually observe — NSE publishes no free variance swap rate, and integrating a
    synthetic strip over a chain with 208,191 option rows per ingest is a batch job, not a
    decision-path computation. Stated as a KNOWN approximation rather than presented as the real
    thing: the at-the-money implied volatility understates the premium when the skew is steep, which
    biases this engine toward calling things FAIR. That direction is the safe one for a seller.
    """

    def __init__(self, *, half_life_sessions: float = DEFAULT_EWMA_HALF_LIFE_SESSIONS) -> None:
        if not math.isfinite(half_life_sessions) or half_life_sessions <= 0.0:
            raise ValueError(
                f"half-life {half_life_sessions!r} is not a memory; a non-positive one either "
                f"forgets everything instantly or never forgets, and neither is an estimator"
            )
        self._decay = 0.5 ** (1.0 / half_life_sessions)
        self._realised: dict[str, _ExponentiallyWeightedVolatility] = {}
        self._premium_moments: dict[str, _OnlineMoments] = {}
        self._last_underlying_price: dict[str, float] = {}

    @property
    def decay(self) -> float:
        """The EWMA decay implied by the half-life. Exposed so a test can check the conversion."""
        return float(self._decay)

    def observe_underlying(self, underlying_symbol: str, price: float) -> None:
        """Advance the realised-volatility estimate on one new underlying price.

        Log returns rather than simple ones: volatility is modelled as the diffusion coefficient of
        a log-normal process, which is the same assumption `BlackScholesOptionAnalyticsEngine`
        prices under. Mixing return conventions between the realised and implied sides of a
        difference is a bias, not a rounding difference.
        """
        if not math.isfinite(price) or price <= 0.0:
            return
        previous = self._last_underlying_price.get(underlying_symbol)
        self._last_underlying_price[underlying_symbol] = price
        if previous is None or previous <= 0.0:
            return
        estimator = self._realised.get(underlying_symbol)
        if estimator is None:
            estimator = _ExponentiallyWeightedVolatility(decay=self._decay)
            self._realised[underlying_symbol] = estimator
        estimator.update(math.log(price / previous))

    def observe_premium(self, underlying_symbol: str, premium: float) -> None:
        """Add one observed `implied - realised` to the underlying's standardising distribution."""
        if not math.isfinite(premium):
            return
        moments = self._premium_moments.get(underlying_symbol)
        if moments is None:
            moments = _OnlineMoments()
            self._premium_moments[underlying_symbol] = moments
        moments.update(premium)

    def realised_volatility_for(
        self, underlying_symbol: str, *, sessions_per_year: int
    ) -> float | None:
        estimator = self._realised.get(underlying_symbol)
        return None if estimator is None else estimator.annualised(sessions_per_year)

    def read(
        self,
        underlying_symbol: str,
        *,
        implied_volatility: float | None,
        sessions_per_year: int,
        richness_cut: float,
    ) -> VarianceRiskPremiumReading:
        """How unusual this underlying's premium is right now.

        `richness_cut` is supplied by the caller and is expected to be a CROSS-SECTIONAL quantity —
        the dispersion of today's standardised premia across the universe — for the same reason the
        cash bot takes its cut from the cross-section: a fixed z-score names nothing on a calm day
        and the whole chain on a violent one.
        """
        realised = self.realised_volatility_for(underlying_symbol,
        sessions_per_year=sessions_per_year)
        if implied_volatility is None or realised is None:
            return VarianceRiskPremiumReading(
                implied_volatility=implied_volatility,
                realised_volatility=realised,
                premium=None,
                standardised_premium=None,
                richness=PremiumRichness.UNMEASURABLE,
                observations=self._observation_count(underlying_symbol),
            )
        premium = implied_volatility - realised
        moments = self._premium_moments.get(underlying_symbol)
        observations = 0 if moments is None else moments.count
        spread = None if moments is None else moments.standard_deviation()
        if (
            moments is None
            or spread is None
            or observations < OBSERVATIONS_NEEDED_TO_STANDARDISE
        ):
            # Measured, and deliberately NOT classified. The premium is real and its context is
            # missing, which is a different statement from "this premium is fair" — collapsing the
            # two is how a bot with no history ends up trading its first observation.
            return VarianceRiskPremiumReading(
                implied_volatility=implied_volatility,
                realised_volatility=realised,
                premium=premium,
                standardised_premium=None,
                richness=PremiumRichness.UNMEASURABLE,
                observations=observations,
            )
        standardised = (premium - moments.mean) / spread
        if not math.isfinite(standardised):
            richness = PremiumRichness.UNMEASURABLE
        elif standardised >= richness_cut:
            richness = PremiumRichness.RICH
        elif standardised <= -richness_cut:
            richness = PremiumRichness.CHEAP
        else:
            richness = PremiumRichness.FAIR
        return VarianceRiskPremiumReading(
            implied_volatility=implied_volatility,
            realised_volatility=realised,
            premium=premium,
            standardised_premium=standardised,
            richness=richness,
            observations=observations,
        )

    def _observation_count(self, underlying_symbol: str) -> int:
        moments = self._premium_moments.get(underlying_symbol)
        return 0 if moments is None else moments.count

    @property
    def underlyings_tracked(self) -> int:
        return len(self._realised)

    def underlyings_with_a_premium_distribution(self) -> int:
        """How many underlyings can actually be classified — the engine's own maturity."""
        return sum(
            1
            for moments in self._premium_moments.values()
            if moments.count >= OBSERVATIONS_NEEDED_TO_STANDARDISE
            and moments.standard_deviation() is not None
        )
