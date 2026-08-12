"""Offset and skew, fitted as the LOWER ENVELOPE of the observed delay cloud.

The measurement problem, restated from `docs/research/216` §2: what is observed is
`lag = theta + skew*t + d + u`, where `d > 0` is the one-way network delay and `u in [0, 1)`
the residue the SDK's one-second truncation throws away. Averaging `lag` estimates
`theta + E[d] + 0.5` — biased by half a second by construction, which on the real tape is the
entire difference between the median lag (0.791 s) and the delay floor (0.263 s).

So the estimator fits the line that lies **below every observation and as close to them as
possible** — the linear-programming estimator of Moon, Skelly & Towsley (1999), the
standard published treatment for one-way delay data:

    minimise    sum_i ( lag_i - (alpha*t_i + beta) )
    subject to  alpha*t_i + beta  <=  lag_i         for every i

`d + u >= 0` makes the constraint physics rather than a modelling choice, and it makes the
fit **asymmetric on purpose**: an unusually LATE packet carries no information (delay can
always grow) while an unusually EARLY one is direct evidence about the offset. That is why
the real tape's 4,931-second outlier cannot move this fit and a millisecond-early packet
can.

**The LP is solved over the lower convex hull, and that is exact, not a sample.** The
objective is linear and the feasible set is the polyhedron
`{(alpha, beta) : alpha*t_i + beta <= lag_i}`, whose optimum is attained with two active
constraints — and any constraint that is not a vertex of the lower convex hull of
`(t_i, lag_i)` is implied by the two that bracket it.
Reducing 15.9 million tape rows to a few dozen hull vertices therefore changes nothing
about the answer, and turns an intractable LP into one that solves in milliseconds.

**What the fit is honest about.** `beta` estimates `theta + inf(d + u)`, so it is an UPPER bound
on the true offset and nothing more; there is no rigorous lower bound available from feed
data alone, because an arbitrarily large minimum delay is indistinguishable from a host
clock that is behind. That gap is exactly why `reference_clock_ntp_sampler` exists: NTP
measures a bounded round trip and therefore brackets the host's error from both sides.
The skew, by contrast, is a DIFFERENCE of offsets and so is free of the unknown constant —
which is why drift is measurable to ppm here while absolute offset is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from scipy.optimize import linprog  # type: ignore[import-untyped]

MAXIMUM_PHYSICAL_CRYSTAL_SKEW_PPM = 500.0
"""A hardware fact, sourced, and therefore a permitted constant under `R.23(e)`.

A quartz oscillator in a computer is specified in the tens of ppm and a badly-aged or
thermally-stressed one reaches the low hundreds; NTP's own `MAXFREQ` clamp is 500 ppm
(RFC 5905 s.7.2), which is the widest anyone designs for. A FITTED skew beyond this is not
a clock — it is the network's delay floor moving, or a session too short to separate rate
from noise. Reported rather than clamped: the number is real, its INTERPRETATION as a clock
is what fails."""

POINTS_NEEDED_FOR_A_LINE = 2
"""Two points define a line; one point and a slope is an invented slope. Used both for the
hull's turn test and for refusing a fit whose observations share a single instant."""


class ClockFitInfeasibleError(RuntimeError):
    """The observations cannot define a line, or the solver refused them.

    Never softened into a mean: a fit that falls back to averaging silently reintroduces
    the half-second truncation bias the envelope exists to remove, and reports it with the
    same confidence as a real fit.
    """


@dataclass(frozen=True, slots=True)
class ClockOffsetFit:
    """A fitted envelope line, with the slack it is entitled to claim."""

    apparent_offset_seconds: float
    """`beta` at `fitted_from` — the offset PLUS the smallest delay+residue observed."""

    skew_ppm: float
    """`alpha x 1e6`: host seconds gained per exchange second. Sign is host-minus-exchange."""

    offset_upper_bound_seconds: float
    """Rigorous: the true offset cannot exceed this, because delay and residue are >= 0."""

    sample_count: int
    hull_vertex_count: int
    fitted_from: datetime
    fitted_to: datetime
    split_half_offset_disagreement_seconds: float
    """Two independent fits over the two halves, compared at the session midpoint.

    This is the maturity signal (`R.04`): not a sample-count constant, but the estimator's
    own account of whether this session's data determines a line. A quiet session with
    three usable packets disagrees with itself and says so.
    """

    @property
    def skew_is_physically_plausible(self) -> bool:
        """Whether the fitted rate is one a crystal oscillator could actually have.

        The offset bound is conditional on the rate estimate (see `_deskewed_minimum_lag`),
        so a rate no hardware could produce invalidates the bound as a statement about a
        CLOCK, even though it remains a correct statement about the observed delays.
        Adversarial review found the case: a delay floor rising at 1 s/s produced a fitted
        skew of 1,000,000 ppm and an offset bound of -99.999 s against a true offset of 0.
        """
        return abs(self.skew_ppm) <= MAXIMUM_PHYSICAL_CRYSTAL_SKEW_PPM

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["fitted_from"] = self.fitted_from.isoformat()
        row["fitted_to"] = self.fitted_to.isoformat()
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ClockOffsetFit:
        fields = dict(row)
        fields["fitted_from"] = datetime.fromisoformat(str(fields["fitted_from"]))
        fields["fitted_to"] = datetime.fromisoformat(str(fields["fitted_to"]))
        return cls(**fields)


def lower_convex_hull_indices(times: Sequence[float], lags: Sequence[float]) -> list[int]:
    """Indices of the points on the lower convex hull, left to right.

    Andrew's monotone chain, lower half only. Ties on `t` keep the smallest lag, since a
    higher point at the same instant is dominated by it for every candidate line.
    """
    if len(times) != len(lags):
        raise ValueError("times and lags must be the same length")
    order = sorted(range(len(times)), key=lambda index: (times[index], lags[index]))
    hull: list[int] = []
    for index in order:
        if hull and times[hull[-1]] == times[index]:
            continue  # the sort put the lowest lag for this instant first
        while len(hull) >= POINTS_NEEDED_FOR_A_LINE:
            first, second = hull[-2], hull[-1]
            cross = (times[second] - times[first]) * (lags[index] - lags[first]) - (
                lags[second] - lags[first]
            ) * (times[index] - times[first])
            if cross <= 0:  # above OR on the chord: not a vertex, and not needed
                hull.pop()
            else:
                break
        hull.append(index)
    return hull


def _deskewed_minimum_lag(
    times: Sequence[float], lags: Sequence[float], alpha: float
) -> float:
    """The tightest intercept the DATA supports, which is the honest upper bound.

    The LP's intercept is not one. Adversarial review built a session whose delay floor
    rises steeply late in the window and got `beta = -99.999` against a true offset of zero:
    the LP bounds the SUM of residuals, and nothing in that objective pins the intercept
    pointwise. `min_i(lag_i - alpha*t_i)` does: every observation satisfies
    `lag_i >= theta + alpha_true*t_i`, so with the fitted rate removed the smallest remaining
    lag is an upper bound on `theta` - conditional on the rate estimate, which is exactly
    what `ClockOffsetFit.skew_is_physically_plausible` exists to qualify.
    """
    return min(lag - alpha * time for time, lag in zip(times, lags, strict=True))


class ExchangeClockOffsetEstimator:
    """Fits `(offset, skew)` from delay observations, and projects the line."""

    def __init__(self, *, solver_method: str = "highs") -> None:
        self._solver_method = solver_method

    def fit(self, observations: Sequence[Any]) -> ClockOffsetFit:
        """Solve the envelope LP over the hull of `observations`.

        `observations` are `ExchangeFeedDelayObservation`s; the type is loose here only to
        keep this module free of an import cycle with the observation boundary.
        """
        if not observations:
            raise ClockFitInfeasibleError(
                "no observations: an unfitted clock is reported as unfitted, never as zero drift"
            )
        ordered = sorted(observations, key=lambda o: o.exchange_second)
        origin = ordered[0].exchange_second
        times = [(o.exchange_second - origin).total_seconds() for o in ordered]
        lags = [o.apparent_lag_seconds for o in ordered]
        if not all(math.isfinite(value) for value in (*times, *lags)):
            # A NaN comparison is false in every direction, so the hull sweep would accept a
            # NaN silently and the solver would raise something from inside scipy. Surfaced
            # here, in this engine's own vocabulary. Found by adversarial review.
            raise ClockFitInfeasibleError(
                "a non-finite lag or timestamp reached the fit; a clock estimate built on "
                "NaN is not a weaker estimate, it is not one"
            )
        if len({round(t, 9) for t in times}) < POINTS_NEEDED_FOR_A_LINE:
            raise ClockFitInfeasibleError(
                f"{len(observations)} observations span fewer than 2 distinct instants; "
                f"a line through one instant is an offset guess with an invented slope"
            )
        alpha, beta, hull_size = self._solve_envelope(times, lags)
        # The observations are sorted by time BEFORE the halves are cut. Slicing an
        # unsorted list by position mixes the two halves and makes the maturity signal
        # 11x more optimistic than it should be - measured by adversarial review on a
        # shuffled copy of the same 20,000 points.
        disagreement = self._split_half_disagreement(times, lags, len(ordered) // 2)
        return ClockOffsetFit(
            apparent_offset_seconds=beta,
            skew_ppm=alpha * 1e6,
            offset_upper_bound_seconds=_deskewed_minimum_lag(times, lags, alpha),
            sample_count=len(ordered),
            hull_vertex_count=hull_size,
            fitted_from=origin,
            fitted_to=ordered[-1].exchange_second,
            split_half_offset_disagreement_seconds=disagreement,
        )

    def fit_rolling_windows(
        self, observations: Sequence[Any], *, window: timedelta, step: timedelta
    ) -> list[ClockOffsetFit]:
        """Successive fits over a sliding time window — the online-tracking path.

        This is what replaces a Kalman servo (`docs/research/216` §9). A servo would need a
        noise model, and one-way delay is non-negative and heavy-tailed to the right, so the
        model would be wrong in exactly the direction that matters. Refitting the same
        envelope over a window keeps the estimator's asymmetry and needs no second model;
        the cost is trivial because each window still collapses to its own small hull.

        Windows that cannot be fitted are SKIPPED rather than filled: an unfittable window
        is a quiet stretch of feed, and interpolating across it would invent an offset the
        session never showed.
        """
        if not observations:
            return []
        ordered = sorted(observations, key=lambda o: o.exchange_second)
        start = ordered[0].exchange_second
        last = ordered[-1].exchange_second
        fits: list[ClockOffsetFit] = []
        while start <= last:
            stop = start + window
            in_window = [o for o in ordered if start <= o.exchange_second < stop]
            if len(in_window) >= POINTS_NEEDED_FOR_A_LINE:
                with suppress(ClockFitInfeasibleError):
                    fits.append(self.fit(in_window))
            start += step
        return fits

    def offset_at(self, fit: ClockOffsetFit, instant: datetime) -> float:
        """The fitted line evaluated at `instant` — the offset the estimator projects."""
        elapsed = (instant - fit.fitted_from).total_seconds()
        return fit.apparent_offset_seconds + fit.skew_ppm * 1e-6 * elapsed

    # -- the solver ------------------------------------------------------------------

    def _solve_envelope(
        self, times: Sequence[float], lags: Sequence[float]
    ) -> tuple[float, float, int]:
        """`(alpha, beta, hull_vertex_count)` for the tightest line under every point."""
        hull = lower_convex_hull_indices(times, lags)
        hull_times = np.array([times[index] for index in hull], dtype=float)
        hull_lags = np.array([lags[index] for index in hull], dtype=float)
        # **The objective is summed over the HULL, not over every point, and adversarial
        # review is why.** Summing over all points makes the objective a function of
        # `mean(t)` across the whole cloud, so a cluster of absurdly late packets can tip
        # which hull edge is optimal: three copies of a 4,931-second outlier moved a fitted
        # offset from 1.000 s to -0.515 s and flipped the skew's sign. Late packets carry no
        # information about the floor, so they get no vote — which makes "an upward outlier
        # cannot move this fit" true rather than merely intended.
        objective = np.array([-float(np.sum(hull_times)), -float(hull_times.size)], dtype=float)
        constraint_matrix = np.column_stack([hull_times, np.ones_like(hull_times)])
        solution = linprog(
            c=objective,
            A_ub=constraint_matrix,
            b_ub=hull_lags,
            bounds=[(None, None), (None, None)],
            method=self._solver_method,
        )
        if not solution.success:
            raise ClockFitInfeasibleError(
                f"envelope LP did not solve ({solution.message}); refusing to substitute a "
                f"mean, which would carry the truncation bias the envelope exists to remove"
            )
        alpha, beta = float(solution.x[0]), float(solution.x[1])
        if not (math.isfinite(alpha) and math.isfinite(beta)):
            raise ClockFitInfeasibleError("envelope LP returned a non-finite line")
        # HiGHS solves to a feasibility TOLERANCE, so the returned line can sit a few
        # microseconds above a constraint it is supposed to be under - measured at 1.5e-05 s
        # on a 20,000-point session, which is not nothing on a host whose chrony RMS offset
        # is 3.9e-05 s. Lowering the intercept by the largest violation restores the
        # defining property exactly, and can only make the bound more conservative.
        violation = float(np.max(alpha * hull_times + beta - hull_lags))
        if violation > 0.0:
            beta -= violation
        return alpha, beta, len(hull)

    def _split_half_disagreement(
        self, times: Sequence[float], lags: Sequence[float], half: int
    ) -> float:
        """How far apart the two halves' lines are at the session midpoint.

        Returns `inf` when a half cannot be fitted at all — an honest "this session does
        not determine a line", which the trust budget reads as IMMATURE.
        """
        if half < POINTS_NEEDED_FOR_A_LINE or len(times) - half < POINTS_NEEDED_FOR_A_LINE:
            return math.inf
        midpoint = (min(times) + max(times)) / 2.0
        projections = []
        for start, end in ((0, half), (half, len(times))):
            part_times, part_lags = times[start:end], lags[start:end]
            if len({round(t, 9) for t in part_times}) < POINTS_NEEDED_FOR_A_LINE:
                return math.inf
            try:
                alpha, beta, _ = self._solve_envelope(part_times, part_lags)
            except ClockFitInfeasibleError:
                return math.inf
            projections.append(alpha * midpoint + beta)
        return abs(projections[0] - projections[1])
