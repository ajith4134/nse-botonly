"""The minimum a proposal must clear, derived three ways — `L5.31`, spec `docs/research/260`.

**No floor here is chosen.** Each is computed from something measured: this trade's own priced
costs, this bot's own outcome dispersion, and the breadth of the scan this proposal won. `R.03` is
not satisfied by "the constant is configurable"; it is satisfied by there being no constant.

The three, and what each one is defending against:

1. **`PRICED_ROUND_TRIP_COST`** — the statutory and brokerage cost of getting in and out, priced by
   `NseTransactionCostEngine`. The retained record lost Rs 3,31,314 net on a gross of roughly
   minus Rs 1,54,725: **costs more than doubled the loss** (`docs/research/254`, `D.01`). A
   gross-expectancy estimate that does not clear this is not a marginal trade, it is a known loser.

2. **`REALISED_COST_PER_TRADE`** — what this bot has actually PAID per round trip, averaged over
   its own record. It exists to catch an under-priced cost model rather than to restate floor 1: if
   the cost engine quotes Rs 30 for this trade while the bot's own 121 closed trades averaged
   Rs 41.9, the higher number is the honest hurdle, and `D.01` is exactly the failure of believing
   the lower one. A first version of this floor was a dispersion half-width instead; it was removed
   because comparing it against a lower credible bound charged the same uncertainty twice. The
   uncertainty now lives in `P(expectancy > floor)` and nowhere else.

   The break-even *rate* the payoff ratio implies — 53.6% for `opening_range_breakout_v1` against an
   achieved 35.0% — stays on the card as evidence and explanation. It is deliberately not a
   threshold: `docs/research/256` measured a plug-in break-even compared against a plug-in rate from
   the same sample moving in **both** directions, promoting a bot two rungs on one added loss.

3. **`SELECTION_CORRECTED_NULL`** — the edge the *best of n* candidates shows under a null of no
   edge at all. When a bot scans a universe and proposes the winner, the winner is the maximum of
   `n` noisy draws and is biased upward by construction. `L5.29`'s real-data pass measured this
   concretely: a one-sigma cross-sectional cut named **1,594 instruments at a single instant**. The
   candidates are scored as FRACTIONS of their own notional so the draws are exchangeable; see
   `selection_corrected_floor` for the real-data failure that requirement came from. The correction
   is the expected maximum of `n` standard normals,

       E[max_n] ~ (1 - g) * Phi^-1(1 - 1/n) + g * Phi^-1(1 - 1/(n*e))

   with `g` the Euler-Mascheroni constant — the same expression underneath the Deflated Sharpe Ratio
   (Bailey and Lopez de Prado) — scaled by the dispersion of the candidate expectancies actually
   scanned. **It rises with breadth**, which is the property no fixed threshold has and the reason
   a wide scan cannot buy its way past this gate by proposing more names.

The binding floor is the maximum of whichever could be derived, and it is named on the card so that
a floor which never binds is visible as one doing nothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

import numpy
from scipy.stats import norm

from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    FloorDerivation,
    QualityFloor,
    TradeQualityError,
)

EULER_MASCHERONI = 0.5772156649015329
"""`g` in the expected-maximum expansion. A mathematical constant, sourced, not a parameter."""

CANDIDATES_BEFORE_SELECTION_BITES = 2
"""With one candidate there was no selection, so the correction is exactly zero.

`Phi^-1(1 - 1/1) = Phi^-1(0)` is negative infinity, so this is a domain boundary rather than a
tuning choice: the formula has nothing to say about a scan of one.
"""

ZERO_RUPEES = Decimal("0")
"""Not a threshold. The floor below which a minimum in rupees is not a minimum."""


def expected_maximum_of_standard_normals(candidates: int) -> float:
    """`E[max]` of `n` independent standard normals, to the usual two-term expansion.

    Exact for no finite `n`. Measured against 400,000-replication Monte Carlo
    (`docs/research/261` MINOR-1): **-7.96% at n=2, +2.47% at n=5, +2.38% at n=10, +0.96% at n=100,
    +0.41% at n=1000**. So it is accurate to well under a percent only from about `n = 100` — an
    earlier version of this docstring claimed that from `n = 5`, which was wrong. The regime that
    matters is exactly where it is accurate: a real cross-sectional scan is hundreds to thousands of
    names wide. At the small `n` where the error is largest the floor itself is small, so the
    absolute error is smaller still. Zero at `n = 1`, where there was no selection to correct for.
    """
    if candidates < 1:
        raise TradeQualityError(
            f"a scan of {candidates} candidates produced a proposal, which cannot have happened"
        )
    if candidates < CANDIDATES_BEFORE_SELECTION_BITES:
        return 0.0
    first = float(norm.ppf(1.0 - 1.0 / candidates))
    second = float(norm.ppf(1.0 - 1.0 / (candidates * math.e)))
    expected = (1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second
    if not math.isfinite(expected):
        raise TradeQualityError(
            f"the expected maximum over {candidates} candidates is not finite; a non-finite floor "
            f"compares False against every expectancy and would admit everything"
        )
    return max(expected, 0.0)


def priced_cost_floor(round_trip_cost_rupees: Decimal) -> QualityFloor:
    """Floor 1: the trade's own cost of doing business, exactly as priced."""
    if not round_trip_cost_rupees.is_finite():
        raise TradeQualityError("a non-finite round-trip cost cannot be a floor")
    if round_trip_cost_rupees < 0:
        raise TradeQualityError(
            f"round-trip cost is {round_trip_cost_rupees}; a negative cost is a rebate this "
            f"project does not model, and as a floor it would license a negative-expectancy trade"
        )
    return QualityFloor(
        derivation=FloorDerivation.PRICED_ROUND_TRIP_COST,
        rupees=round_trip_cost_rupees,
        explanation=(
            f"the priced statutory and brokerage cost of one round trip is Rs "
            f"{round_trip_cost_rupees}; the retained record's costs of Rs 1,76,589 turned a gross "
            f"loss of about Rs 1,54,725 into a net Rs 3,31,314 one"
        ),
    )


def realised_cost_floor(mean_cost_rupees: Decimal, observations: int) -> QualityFloor:
    """Floor 2: what this bot has actually paid per round trip, from its own record.

    A cross-check on floor 1 rather than a restatement of it. The priced cost is a model of this
    trade; this is a measurement of that bot's trades, and where the measurement is higher the model
    is understating what trading actually costs.
    """
    if observations < 0:
        raise TradeQualityError("an observation count cannot be negative")
    if not mean_cost_rupees.is_finite():
        raise TradeQualityError("a non-finite realised cost cannot be a floor")
    if mean_cost_rupees < 0:
        raise TradeQualityError(
            f"this bot's record reports a mean cost of {mean_cost_rupees} per trade; a negative "
            f"cost is a rebate this project does not model"
        )
    return QualityFloor(
        derivation=FloorDerivation.REALISED_COST_PER_TRADE,
        rupees=mean_cost_rupees,
        explanation=(
            f"across its own {observations} closed trades this bot actually paid Rs "
            f"{mean_cost_rupees} per round trip, which is what trading has cost it rather than "
            f"what a model says it should"
        ),
    )


def selection_corrected_floor(
    candidate_expectancy_fractions: Sequence[Decimal],
    *,
    proposal_notional_rupees: Decimal,
) -> QualityFloor:
    """Floor 3: the edge the winner of this scan would show even if no candidate had any.

    **The scores must be FRACTIONS of each candidate's own notional, not rupee amounts**, and that
    requirement is load-bearing rather than a convenience. The correction is the expected maximum of
    `n` *exchangeable* draws. Candidates of different position sizes are not exchangeable: their
    rupee expectancies differ mostly by how large the position would be, so the dispersion of rupee
    scores measures the spread of the universe's share prices and the floor explodes with it. This
    engine's own first real-data run produced exactly that — a Rs 1,582 floor over 500 real NSE
    instruments, dominating every other term and refusing everything. Standardising each candidate
    onto its own notional makes the draws comparable; the winner's notional then converts the
    correction back into rupees.

    A wide scan over near-identical candidates is barely corrected; a wide scan over widely
    dispersed ones is corrected hard, which is the right way round — dispersion is what makes a
    maximum climb.
    """
    if not candidate_expectancy_fractions:
        raise TradeQualityError(
            "a proposal was made from a scan of no candidates, which cannot have happened"
        )
    if proposal_notional_rupees <= 0:
        raise TradeQualityError(
            f"the proposal's notional is {proposal_notional_rupees}; it is what converts a "
            f"standardised correction back into rupees, and a non-positive one erases the floor"
        )
    submitted = numpy.asarray(
        [float(value) for value in candidate_expectancy_fractions], dtype=float
    )
    if not numpy.all(numpy.isfinite(submitted)):
        raise TradeQualityError(
            "a scanned candidate carries a non-finite score; its dispersion, and therefore this "
            "floor, would be non-finite too"
        )
    # CLUSTERED scores, not merely distinct ones. `docs/research/261` CRITICAL-3 and its re-review:
    # the first repair used `numpy.unique`, which is exact equality against an attack needing one
    # bit — padding with the winner plus `j * 1e-15` still cut the floor 87.3%, and padding near the
    # mean cut it 93.5%. The expansion counts INDEPENDENT draws, and two candidates differing in
    # the fifteenth decimal are not two draws.
    #
    # The resolution is DERIVED from the scan and cannot be set by the caller (`R.03`): scores are
    # rounded to a grid of the sample's own spread over its size, so a dispersed scan keeps its
    # resolution while a padded one collapses to the levels it actually contains.
    values = _clustered(submitted)
    breadth = int(values.size)
    dispersion = float(values.std(ddof=1)) if breadth > 1 else 0.0
    expected_maximum = expected_maximum_of_standard_normals(breadth)
    floor = max(
        Decimal(str(dispersion * expected_maximum)) * proposal_notional_rupees, ZERO_RUPEES
    )
    return QualityFloor(
        derivation=FloorDerivation.SELECTION_CORRECTED_NULL,
        rupees=floor,
        explanation=(
            f"chosen from {breadth} distinct candidates (of {submitted.size} scanned) whose "
            f"per-notional scores disperse by "
            f"{dispersion:.6f}; the best of {breadth} draws from a zero-edge null would show "
            f"{dispersion * expected_maximum:.6f} of notional on that dispersion alone "
            f"(E[max] = {expected_maximum:.3f} sigma), which on this proposal's Rs "
            f"{proposal_notional_rupees} is Rs {floor}"
        ),
    )


def _clustered(
    scores: numpy.typing.NDArray[numpy.float64],
) -> numpy.typing.NDArray[numpy.float64]:
    """The scan's genuinely distinct levels, at a resolution the proposing bot cannot choose.

    Two scores closer than `span * sqrt(machine epsilon)` are one level. That tolerance is a
    NUMERICAL FACT rather than a tuning knob: at double precision, values differing by less than
    about 1.5e-8 of their own range are not distinguishable measurements, so calling them two
    independent draws is arithmetic fiction. The 1e-15 spacing the re-review used to defeat the
    previous `numpy.unique` sits eight orders of magnitude below it and collapses; a real
    cross-sectional scan's levels sit far above it and are untouched.

    An earlier version derived the grid from the sample's own SIZE, which was worse than useless: it
    made an honest 5-wide scan coarser than a 10,000-wide padded one, so padding improved the
    attacker's resolution. The tolerance must not depend on how many names were submitted, because
    that is exactly the quantity the caller controls.

    Degenerate ranges (every score identical) collapse to one level, which is correct: nothing was
    selected on, so there is nothing to correct for.
    """
    span = float(scores.max() - scores.min())
    if not math.isfinite(span) or span <= 0.0:
        return numpy.unique(scores)
    tolerance = span * math.sqrt(float(numpy.finfo(numpy.float64).eps))
    if tolerance <= 0.0:
        return numpy.unique(scores)
    return numpy.unique(numpy.round(scores / tolerance) * tolerance)


def binding_floor_of(floors: Sequence[QualityFloor]) -> QualityFloor:
    """The one a proposal actually has to clear: the highest derived minimum.

    Raises on an empty set rather than answering zero. "No floor could be derived" is an
    `UNASSESSABLE` verdict for the engine to reach explicitly, and a zero floor returned here would
    read downstream as a trade that cleared every test.
    """
    if not floors:
        raise TradeQualityError(
            "no floor could be derived, so there is nothing for a proposal to clear; the caller "
            "must return UNASSESSABLE rather than treat an absent floor as a cleared one"
        )
    return max(floors, key=lambda floor: floor.rupees)
