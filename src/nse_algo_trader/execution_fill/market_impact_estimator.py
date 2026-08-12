"""What size costs beyond the visible book — anchored to data, not to a borrowed constant.

Above the visible ladder the book-walk stops being evidence (`order_book_walk_calculator`
explains why: it saturates rather than growing, so it reads cheap exactly where cost runs
away). Something has to take over, and the standard answer is the square-root law:

    impact = Y * sigma * (Q / V) ** delta        with delta near 1/2

The usual practice is to take `Y` from the literature as an O(1) constant. **This module does
not**, for two reasons. First, `R.03`: a coefficient copied out of a paper is a hardcoded value
wearing a citation, and the published range is wide enough (Y roughly 0.3 to 1.5, delta measured
anywhere from 0.4 to 0.7) that the choice would silently dominate every answer. Second, and
more usefully, **it is unnecessary** — the curve can be pinned to real data instead.

**The anchor.** At exactly the visible depth, two things describe the same order: the book-walk
(which is arithmetic, and exact there) and the impact law (which is an extrapolation, and starts
there). Requiring them to AGREE at that point determines `Y` outright:

    Y = book_walk_cost_at_visible_depth / (sigma * (visible_depth / whole_book) ** delta)

So the impact curve leaves the book-walk exactly where the book-walk stops being true, and its
level is set by this instrument's own measured cost at that boundary. Nothing is imported.

**The exponent stays uncertain, and the output says so.** `delta` cannot be fitted from this
tape — measured R^2 of 0.010 to 0.070 for the square-root relation intraday (`research/220`
§2.1), and the honest reading of the literature is that one-half is a mid-range regime rather
than a law. So the estimate is returned as an INTERVAL spanned by the plausible exponent range,
with the half-way exponent as the point estimate. A wide interval is the correct output when
the data cannot narrow it, and it is what lets a gate downstream refuse on uncertainty rather
than act on a false point.

**Empirical arming (`R.04`).** When real fills exist, `delta` becomes fittable per bucket and
then per instrument, and the interval narrows on evidence. Until then the full estimator runs
with the anchored `Y` and the literature-width interval — the algorithm is complete from day
one, and only its precision is gated.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.execution_fill.instrument_liquidity_buckets import LiquidityBucket
from nse_algo_trader.execution_fill.order_book_walk_calculator import OrderBookWalk

_BASIS_POINTS = Decimal(10_000)


class MarketImpactError(Exception):
    """Impact cannot be estimated for this order, and a number would be an invention."""


class ImpactEstimateMaturity(StrEnum):
    """How much evidence stands behind an impact estimate.

    The ladder `R.04` requires. The ALGORITHM is identical at every stage — what changes is
    where the exponent comes from and therefore how wide the interval is.
    """

    ANCHORED_PRIOR = "anchored_prior"
    BUCKET_FITTED = "bucket_fitted"
    INSTRUMENT_FITTED = "instrument_fitted"


PLAUSIBLE_EXPONENT_RANGE = (Decimal("0.4"), Decimal("0.7"))
"""The empirical span of the impact exponent across the published record.

Not a tuning parameter and not a fact about NSE: it is a statement about how much the WORLD
disagrees, and it is carried explicitly so that disagreement shows up as interval width in
every answer rather than being resolved by whoever picked a number. The Tokyo Stock Exchange
survey anchors near one-half; other large-sample work finds the exponent rising toward linear
at high participation. Both are inside this range.
"""


@dataclass(frozen=True, slots=True)
class ImpactEstimate:
    """Expected impact for one order, as an interval, with its provenance."""

    quantity: int
    participation_of_whole_book: Decimal
    anchor_cost_bps: Decimal
    anchor_participation: Decimal
    lower_bps: Decimal
    point_bps: Decimal
    upper_bps: Decimal
    maturity: ImpactEstimateMaturity
    bucket: LiquidityBucket | None
    observation_count: int

    def __post_init__(self) -> None:
        if not self.lower_bps <= self.point_bps <= self.upper_bps:
            raise MarketImpactError(
                f"impact interval is not ordered: {self.lower_bps} / {self.point_bps} / "
                f"{self.upper_bps}"
            )

    @property
    def interval_width_bps(self) -> Decimal:
        """How much is not known. A gate may refuse on this alone."""
        return self.upper_bps - self.lower_bps

    @property
    def is_extrapolated(self) -> bool:
        """True when the order is larger than the book that anchored the estimate."""
        return self.participation_of_whole_book > self.anchor_participation


def _power(base: Decimal, exponent: Decimal) -> Decimal:
    """`base ** exponent` for a positive base, via `Decimal`'s exp/ln so it stays exact-ish.

    `Decimal.__pow__` refuses a non-integer exponent on a Decimal base, and going through
    `float` here would put binary error into the one number the whole estimate scales by.
    """
    if base <= 0:
        raise MarketImpactError(f"cannot raise a non-positive base {base} to a power")
    return (exponent * base.ln()).exp()


def estimate_impact_from_walk(
    walk: OrderBookWalk,
    quantity: int,
    *,
    bucket: LiquidityBucket | None = None,
    exponent_range: tuple[Decimal, Decimal] = PLAUSIBLE_EXPONENT_RANGE,
    maturity: ImpactEstimateMaturity = ImpactEstimateMaturity.ANCHORED_PRIOR,
    observation_count: int = 0,
) -> ImpactEstimate:
    """Impact for `quantity`, anchored to what the visible book cost at its own depth.

    `walk` must be a walk of the FULL visible depth — that is the anchor point, the last
    quantity for which the cost is arithmetic rather than extrapolation.

    Raises:
        MarketImpactError: the walk cannot anchor anything (no visible quantity, no whole-book
            quantity, or a non-positive cost), or the requested quantity is not positive.
    """
    if quantity <= 0:
        raise MarketImpactError(f"quantity must be positive, got {quantity}")
    if walk.visible_quantity <= 0:
        raise MarketImpactError("a walk with no visible quantity cannot anchor an impact curve")
    if walk.whole_book_quantity <= 0:
        raise MarketImpactError(
            "no whole-book quantity on this side; participation is undefined, and measuring "
            "participation against the visible ladder alone overstates it by ~300x"
        )
    anchor_cost = walk.impact_cost_bps
    if anchor_cost <= 0:
        raise MarketImpactError(
            f"anchor cost is {anchor_cost} bps; a non-positive cost at the touch means the "
            f"book was crossed or the walk consumed nothing"
        )
    anchor_participation = Decimal(walk.visible_quantity) / Decimal(walk.whole_book_quantity)
    participation = Decimal(quantity) / Decimal(walk.whole_book_quantity)

    lower_exponent, upper_exponent = exponent_range
    if not 0 < lower_exponent <= upper_exponent:
        raise MarketImpactError(f"exponent range {exponent_range} is not ordered and positive")
    midpoint_exponent = (lower_exponent + upper_exponent) / Decimal(2)

    def scaled(exponent: Decimal) -> Decimal:
        # cost(Q) = anchor * (Q / Q_anchor) ** exponent, which is the square-root law with Y
        # eliminated by the anchoring condition — sigma and the whole-book scale cancel.
        return anchor_cost * _power(participation / anchor_participation, exponent)

    candidates = sorted((scaled(lower_exponent), scaled(upper_exponent)))
    return ImpactEstimate(
        quantity=quantity,
        participation_of_whole_book=participation,
        anchor_cost_bps=anchor_cost,
        anchor_participation=anchor_participation,
        lower_bps=candidates[0],
        point_bps=scaled(midpoint_exponent),
        upper_bps=candidates[1],
        maturity=maturity,
        bucket=bucket,
        observation_count=observation_count,
    )


def shrinkage_weight(
    own_observations: int, own_variance: Decimal, between_instrument_variance: Decimal
) -> Decimal:
    """The empirical-Bayes weight on an instrument's OWN estimate: `tau^2 / (tau^2 + se^2)`.

    Returns 0 with no observations (use the peer group entirely) and approaches 1 as the
    standard error of the own estimate shrinks against the spread between instruments.

    Measured caution, recorded because it is counter-intuitive: for the SPREAD term this weight
    came out at 0.99 even with five observations, and pooling actively hurt. It is kept here,
    for the IMPACT coefficient, where per-instrument observations really will be near zero —
    which is the case shrinkage exists for.
    """
    if own_observations <= 0:
        return Decimal(0)
    if between_instrument_variance < 0 or own_variance < 0:
        raise MarketImpactError("variances cannot be negative")
    standard_error_squared = own_variance / Decimal(own_observations)
    denominator = between_instrument_variance + standard_error_squared
    if denominator <= 0:
        return Decimal(0)
    return between_instrument_variance / denominator


def blend_toward_peers(
    own_estimate_bps: Decimal, peer_estimate_bps: Decimal, weight: Decimal
) -> Decimal:
    """Shrink an own estimate toward its peer group by `weight` (1 = keep own entirely)."""
    if not 0 <= weight <= 1:
        raise MarketImpactError(f"shrinkage weight must be in [0, 1], got {weight}")
    return weight * own_estimate_bps + (Decimal(1) - weight) * peer_estimate_bps
