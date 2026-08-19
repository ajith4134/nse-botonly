"""What one proposal is worth before costs, with the uncertainty kept — `L5.31`.

Spec `docs/research/260`.

**The quantity.** Per trade, gross expectancy is `p_win*W - p_loss*L`, where `p_loss` is the non-win
mass NET of scratches — trades that closed at exactly zero. All of `p_win`, `W` and `L` are
*estimates*, and the whole point of this module is that they are propagated as distributions rather
than collapsed to point values before the arithmetic.

Writing `(1 - p_win)` for `p_loss` is only right when nothing ever scratches, and
`docs/research/261` CRITICAL-6 measured what that costs: a bot genuinely earning +Rs 83 a trade was
REFUSED on an estimate of -Rs 63 once 30 scratches sat in its record, and -Rs 237 with 200 of them.

**Gross, because the cost is a FLOOR rather than a subtraction.** The priced round-trip cost is one
of the three minimums this quantity must clear in `selection_corrected_quality_floor`. Netting it
off here as well would charge it twice, and it would also hide which constraint actually bound —
the thing the evidence card exists to name.

**The Monte-Carlo sample does NOT depend on the floor, and that is a correctness property rather
than an optimisation.** Seeding on the floor drew an independent sample per floor, so a floor
differing in the 10^-12 rupee place redrew everything: `docs/research/261` CRITICAL-4 found 214
cases where a STRICTLY HARDER floor was ADMITTED where a softer one was REFUSED. With one sample per
proposal, `P(expectancy > floor)` is that sample's empirical survival function — monotone
non-increasing in the floor by construction, so a harder floor can never admit more.

**The output is a posterior probability against the floor, and uncertainty is charged once.**
`P(gross expectancy > floor)` is the whole verdict statistic. An earlier version returned only an
interval and let the caller compare its lower bound against a floor that was itself a dispersion
half-width — which subtracts the same uncertainty twice, since a lower bound has already subtracted
it once. The floor is passed IN here so that the probability is computed against the exact number
the decision turns on. It is the same statistic `bot_maturity_ladder` promotes on, so the per-trade
gate and the per-bot ladder cannot mean different things by "the evidence supports this".

Eleven trades and eleven hundred can produce the same expectancy mean; only the posterior mass knows
the difference, which is what makes thin evidence self-limiting without a separate rule (`R.04`).

**Where each distribution comes from.**

* `p` — a `Beta(1 + c*n, 1 + (1-c)*n)` posterior around the calibrated probability `c`, where `n` is
  the number of trades the calibration was fitted on. At `n = 0` this is `Beta(1, 1)`, the uniform
  prior: a bot with no record has a probability posterior spanning the whole interval, its lower
  bound is near zero, and it will not clear a floor. That is the correct answer, and it arrives from
  the arithmetic rather than from a cold-start special case.
* `W` and `L` — Bayesian-bootstrap resamples of the bot's own win and loss magnitudes, drawn jointly
  with `p` so that a bot with few wins and many losses carries that asymmetry into the interval.
`docs/research/254` measured costs at more than half the retained loss, so they are never
approximated anywhere in this engine — they arrive priced, in rupees, from
`NseTransactionCostEngine`, and they enter as the floor named `PRICED_ROUND_TRIP_COST`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal

import numpy

from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    CalibratedProbability,
    GrossExpectancyPosterior,
    TradeQualityError,
)

UNIFORM_BETA_PSEUDO_COUNT = 1.0
"""Both parameters of `Beta(1, 1)`, the uniform prior a bot with no record starts from.

A property of the conjugate prior, not a tuning knob.
"""

MAGNITUDES_NEEDED_PER_SIDE = 1
"""A side with no observations cannot be resampled, and the caller must say `UNASSESSABLE`."""

DEFAULT_MONTE_CARLO_DRAWS = 60_000
"""Joint draws of `(p, W, L)`. Numerical resolution, not a decision threshold.

Raised from 8,000 after `docs/research/261` CRITICAL-4 measured `P(exceed floor)` carrying a
standard deviation of 0.0034 — enough to split 300 economically identical proposals 142 ADMIT / 158
REFUSE at a 0.90 threshold. The seed no longer depends on the floor (see `_seed_from`'s caller), so
the sample is now shared across floors and the probability is monotone in the floor by construction;
this raises the resolution of the remaining estimate rather than fixing the ordering, which the
shared sample fixes exactly.
"""

DEFAULT_CREDIBLE_MASS = 0.9
"""The central mass of the reported interval, carried on the result so a bound is never bare."""


def _seed_from(parts: Sequence[str]) -> int:
    """A data-derived seed, so the same proposal against the same record always replays the same."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


def estimate_gross_expectancy_posterior(
    *,
    calibrated: CalibratedProbability,
    win_magnitudes: Sequence[Decimal],
    loss_magnitudes: Sequence[Decimal],
    floor_rupees: Decimal,
    scratch_share_among_non_wins: float = 0.0,
    draws: int = DEFAULT_MONTE_CARLO_DRAWS,
    credible_mass: float = DEFAULT_CREDIBLE_MASS,
) -> GrossExpectancyPosterior | None:
    """Posterior over gross expected rupees per trade. `None` when a payoff side is empty.

    `None` is an absence of judgement, not a permissive one — the engine turns it into
    `UNASSESSABLE`, never into an admission.
    """
    if draws <= 0:
        raise TradeQualityError("a posterior with no draws is not a posterior")
    if not 0.0 < credible_mass < 1.0:
        raise TradeQualityError(
            f"credible mass must lie strictly inside (0, 1), got {credible_mass}"
        )
    if not 0.0 <= scratch_share_among_non_wins <= 1.0:
        raise TradeQualityError(
            f"the scratch share among non-wins is {scratch_share_among_non_wins}, which is not a "
            f"fraction; it splits the non-win mass and a value outside [0, 1] makes the loss "
            f"probability negative or larger than the mass it comes from"
        )
    if not floor_rupees.is_finite():
        raise TradeQualityError(
            "a non-finite floor makes P(expectancy > floor) meaningless, and the comparison would "
            "evaluate False for every draw — reading as a refusal reached by arithmetic accident"
        )
    if len(win_magnitudes) < MAGNITUDES_NEEDED_PER_SIDE:
        return None
    if len(loss_magnitudes) < MAGNITUDES_NEEDED_PER_SIDE:
        return None

    wins = numpy.asarray([float(value) for value in win_magnitudes], dtype=float)
    losses = numpy.asarray([float(value) for value in loss_magnitudes], dtype=float)
    if not (numpy.all(numpy.isfinite(wins)) and numpy.all(numpy.isfinite(losses))):
        raise TradeQualityError("a payoff magnitude is non-finite; the posterior would be too")
    if numpy.any(wins < 0) or numpy.any(losses < 0):
        raise TradeQualityError(
            "payoff magnitudes are carried unsigned; a negative one flips the expectancy arithmetic"
        )

    effective_sample = float(calibrated.fitted_on_trades)
    alpha = UNIFORM_BETA_PSEUDO_COUNT + calibrated.calibrated * effective_sample
    beta = UNIFORM_BETA_PSEUDO_COUNT + (1.0 - calibrated.calibrated) * effective_sample
    generator = numpy.random.default_rng(
        _seed_from(
            (
                f"{calibrated.calibrated:.12f}",
                f"{calibrated.fitted_on_trades}",
                repr(wins.tolist()),
                repr(losses.tolist()),
            )
        )
    )
    probability_draws = generator.beta(alpha, beta, size=draws)
    win_weights = generator.dirichlet(numpy.ones(wins.size), size=draws)
    loss_weights = generator.dirichlet(numpy.ones(losses.size), size=draws)
    win_draws = win_weights @ wins
    loss_draws = loss_weights @ losses
    # `p_loss` is the non-win mass NET of scratches, not the whole of it (`docs/research/261`
    # CRITICAL-6). A scratch closes at zero: it is neither a win to be counted nor a loss to be
    # charged a full magnitude for.
    loss_probability = (1.0 - probability_draws) * (1.0 - scratch_share_among_non_wins)
    expectancy = probability_draws * win_draws - loss_probability * loss_draws

    tail = (1.0 - credible_mass) / 2.0
    lower, upper = numpy.quantile(expectancy, [tail, 1.0 - tail])
    mean = float(expectancy.mean())
    return GrossExpectancyPosterior(
        mean_rupees=Decimal(str(mean)),
        lower_rupees=Decimal(str(float(lower))),
        upper_rupees=Decimal(str(float(upper))),
        credible_mass=credible_mass,
        draws=draws,
        floor_rupees=floor_rupees,
        probability_exceeding_floor=float(numpy.mean(expectancy > float(floor_rupees))),
    )
