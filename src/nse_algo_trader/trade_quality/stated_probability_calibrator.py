"""What a bot's stated win probability is actually worth — `L5.31`, spec `docs/research/260`.

**The measurement this exists for.** Across the 3,481 retained closed trades the mean stated win
probability was 0.4585 against a realised win rate of 0.3689, and the mean Brier contribution was
0.2855 — *worse than the 0.25 a forecaster scores by saying "50%" to everything*
(`docs/research/254`). A stated probability that scores worse than a constant is not a weak signal
to be discounted; as a calibrated quantity it carries **negative information**, and any gate that
multiplies by it directly is being actively misled rather than merely under-informed.

Per strategy the overconfidence differed by a factor of four:

    opening_range_breakout_v1   stated 0.416  realised 0.350   +6.6 points
    directional_option_orb_v1   stated 0.775  realised 0.514  +26.1 points
    credit_spread_v1            stated 0.715  realised 0.463  +25.2 points

A single global correction would under-correct two of those by twenty points and over-correct the
third, so the fit is **per bot**, borrowing from its segment and then from the pooled record only as
far as its own evidence is thin.

**Isotonic, not Platt.** The correction needed here is not a logistic squeeze; the retained
strategies were miscalibrated by different amounts in different parts of the range, and isotonic
regression is the standard non-parametric answer — it assumes only that a higher stated probability
should not map to a lower realised one, which is the single property a forecast must have to be
worth correcting at all. `scikit-learn`'s `IsotonicRegression` is the same estimator underneath
`CalibratedClassifierCV(method="isotonic")` and is already a project dependency.

**Out-of-fold, by session, and that is not a detail.** A trade calibrated by a fit that saw its own
outcome would report a correction it will never achieve in production. Folds are **whole session
dates in time order** — the first session has no prior sessions and is therefore excluded from the
diagnostic and reported as excluded, rather than quietly folded in. The number of folds is the
number of sessions in the record; there is no fold count to choose (`R.03`).

**`R.04`: thin data changes which parent the estimate borrows from, never whether it is made.**
A bot with no record of its own is calibrated against its segment; a segment with no record is held
to the pooled base rate. The shrinkage weight is empirical-Bayes — the concentration implied by how
much sibling bots' win rates actually disperse — not a chosen constant.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import numpy
from sklearn.isotonic import IsotonicRegression

from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    CalibratedProbability,
    CalibrationMethod,
    TradeQualityError,
)

UNIFORM_PRIOR_PSEUDO_OBSERVATIONS = 1.0
"""The pseudo-count a uniform `Beta(1, 1)` prior contributes per class.

A fact about the conjugate prior, not a tuning knob: `Beta(1, 1)` is one pseudo-win and one
pseudo-loss. It is the shrinkage weight used when the sibling dispersion that empirical Bayes needs
cannot be measured, so the fallback is the weakest defensible prior rather than an invented one.
"""

CLASSES_NEEDED_FOR_A_FIT = 2
"""Isotonic regression on a record with only wins, or only losses, has nothing to order."""

DISTINCT_FORECASTS_NEEDED_FOR_A_FIT = 2
"""A forecaster that says the same number every time has no ordering to correct.

This is the `p = 0.99` gaming case: a constant input carries zero resolution, so there is no
monotone map to fit and the estimate falls through to the base rate — which is exactly the answer a
constant forecast deserves.
"""

SESSIONS_NEEDED_FOR_OUT_OF_FOLD = 2
"""One session cannot be scored out of fold, because there is no earlier session to fit on."""


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    """One resolved forecast: what was claimed, and what happened.

    `occurred_at` is what orders the folds, and `session_date` is what blocks them. Both are carried
    because a fold boundary drawn on the instant rather than the session would let a morning trade
    calibrate an afternoon one on the same day — information no live decision has.
    """

    bot_identity: str
    trading_segment: TradingSegment
    occurred_at: datetime
    session_date: date
    stated_probability: float
    was_win: bool

    def __post_init__(self) -> None:
        if not self.bot_identity.strip():
            raise TradeQualityError("a forecast with no bot identity calibrates nobody")
        if self.occurred_at.tzinfo is None:
            raise TradeQualityError(
                f"{self.bot_identity} recorded a forecast at a naive {self.occurred_at}; fold "
                f"boundaries are drawn in time and a naive instant cannot be ordered against an "
                f"aware one"
            )
        if not math.isfinite(self.stated_probability):
            raise TradeQualityError(
                f"{self.bot_identity} stated a non-finite probability {self.stated_probability!r}; "
                f"it would propagate through the fit and silently make every later comparison False"
            )
        if not 0.0 <= self.stated_probability <= 1.0:
            raise TradeQualityError(
                f"{self.bot_identity} stated a probability of {self.stated_probability}, which is "
                f"outside [0, 1]"
            )


@dataclass(frozen=True, slots=True)
class BrierDecomposition:
    """Murphy's decomposition of the Brier score: `reliability - resolution + uncertainty`.

    * **reliability** — how far the forecasts sit from the outcome frequencies they claim. Lower is
      better; it is the part calibration fixes.
    * **resolution** — how far those frequencies sit from the base rate. Higher is better; it is the
      part calibration CANNOT create, because a monotone map cannot invent discrimination.
    * **uncertainty** — the base rate's own variance, `o(1-o)`. A property of the outcomes,
      identical for every forecaster scored on the same record.

    `reliability > resolution` is the formal statement of "worse than always saying the base rate",
    which is what the retained record's 0.2855 against a 0.25 baseline amounts to.
    """

    reliability: float
    resolution: float
    uncertainty: float
    scored_forecasts: int
    excluded_first_session_forecasts: int

    @property
    def brier_score(self) -> float:
        return self.reliability - self.resolution + self.uncertainty

    @property
    def beats_the_base_rate(self) -> bool:
        return self.resolution > self.reliability


@dataclass(frozen=True, slots=True)
class BinnedCalibration:
    """An isotonic map fitted over equal-count BINS of the record, not over its raw points.

    **This is the repair for `docs/research/261` CRITICAL-1, and the binning is the whole point.**
    Stated probabilities are continuous, so isotonic fitted on raw points interpolates every one of
    them exactly: the map has zero training error and the calibrated value at a bot's own top stated
    value converges to 1.0 **for a bot with no skill at all** (measured: 0.9993 at n=1000, 30% true
    win rate). Fitted over bins that each hold many observations, no single trade can define a step,
    and that same zero-skill bot calibrates to its base rate.

    **The bin count is derived, not chosen** (`R.03`): `floor(sqrt(n))` bins over quantile edges, so
    each bin holds about `sqrt(n)` observations. That is the classic histogram rule — the count that
    balances resolution against per-bin variance as `n` grows — and it is a property of the sample
    size rather than a tuning knob. Quantile edges rather than equal-width ones because a forecaster
    that states 0.9 four hundred times and 0.1 twice has almost all its evidence in one place.

    `support_at` is what makes the SECOND critical fixable: the number of observations actually
    standing behind a prediction, which is a per-bin quantity and not the bot's whole trade count.
    """

    edges: tuple[float, ...]
    bin_rates: tuple[float, ...]
    bin_counts: tuple[int, ...]
    pooled_support: tuple[int, ...]
    model: IsotonicRegression
    observations: int

    def predict(self, stated: float) -> float:
        """The calibrated probability, clipped into `[0, 1]`."""
        predicted = self.model.predict(numpy.asarray([stated], dtype=float))
        return float(min(max(float(predicted[0]), 0.0), 1.0))

    def support_at(self, stated: float) -> int:
        """How many observations stand behind this prediction — the POOLED level set, not one bin.

        Using the bot's whole trade count is `docs/research/261` CRITICAL-2: every added trade,
        win or loss, tightened the posterior, so six added losses turned a REFUSE into an ADMIT.
        Using a single bin was the over-correction the re-review measured: bins are
        `floor(sqrt(n))` wide, so the evidence count was pinned at `sqrt(n)` — **316 at 100,000
        trades** — and the posterior narrowed as `n^-1/4` instead of `n^-1/2`. That was one of
        the two independent reasons nothing was ever admitted.

        The right quantity is the isotonic **level set**: the run of adjacent bins that pooled
        to one fitted value under PAVA. Those observations are exactly the ones the fit used to
        produce this number, and the count grows with `n` because a longer record supports the
        same block with more data. For a forecaster with no resolution the whole record pools
        into one block and the support is `n` — correct, because "the base rate" really is
        supported by every trade.

        Empty bins are KEPT with a count of zero rather than skipped. Skipping them left
        `bin_counts` shorter than `edges`, so `searchsorted` indexed the wrong slot: 2.88% of
        queries wrong and evidence overstated by up to **18.7x** — a defect introduced by this
        method's own first version (`docs/research/261` MAJOR-A).
        """
        index = int(numpy.searchsorted(numpy.asarray(self.edges), stated, side="right")) - 1
        index = min(max(index, 0), len(self.pooled_support) - 1)
        return self.pooled_support[index]


def _fit_binned_isotonic(
    stated: Sequence[float], outcomes: Sequence[bool]
) -> BinnedCalibration | None:
    """Fit the monotone map over equal-count bins, or `None` when the record cannot support one.

    `None` rather than an identity map, on purpose: an identity map is a *claim* that the forecasts
    were already calibrated, and a record with one class or one distinct forecast is evidence of
    nothing. The caller falls back to a parent, which is a different and honest answer.
    """
    if len(stated) != len(outcomes):
        raise TradeQualityError("the forecast and outcome series have different lengths")
    if len(set(outcomes)) < CLASSES_NEEDED_FOR_A_FIT:
        return None
    if len(set(stated)) < DISTINCT_FORECASTS_NEEDED_FOR_A_FIT:
        return None
    values = numpy.asarray(stated, dtype=float)
    won = numpy.asarray(outcomes, dtype=float)
    total = values.size
    bins = max(CLASSES_NEEDED_FOR_A_FIT, math.isqrt(total))
    quantiles = numpy.linspace(0.0, 1.0, bins + 1)
    edges = numpy.unique(numpy.quantile(values, quantiles))
    if edges.size < CLASSES_NEEDED_FOR_A_FIT + 1:
        # Quantile edges collapsed — a forecaster using very few distinct values. Bin on the
        # DISTINCT values themselves rather than giving up. `docs/research/261` CRITICAL-D: the
        # previous fallback built two edges, clipped every assignment into bin zero and then failed
        # its own guard, so it always returned `None` and such a bot silently inherited its
        # segment's calibration — measured turning a 28.3% base rate into 86.4%.
        distinct = numpy.unique(values)
        if distinct.size < CLASSES_NEEDED_FOR_A_FIT:
            return None
        step = numpy.diff(distinct).min() / 2.0
        edges = numpy.concatenate([distinct - step, distinct[-1:] + step])
    assignment = numpy.clip(
        numpy.searchsorted(edges, values, side="right") - 1, 0, max(edges.size - 2, 0)
    )
    centres: list[float] = []
    rates: list[float] = []
    counts: list[int] = []
    for index in range(max(edges.size - 1, 1)):
        members = won[assignment == index]
        counts.append(int(members.size))
        if members.size == 0:
            # Kept, so `bin_counts` stays index-aligned with `edges` (MAJOR-A). The centre is the
            # bin's own midpoint and the rate is carried from the previous bin, so an empty bin
            # never invents an outcome frequency of its own.
            low = float(edges[index])
            high = float(edges[min(index + 1, edges.size - 1)])
            centres.append((low + high) / 2.0)
            rates.append(rates[-1] if rates else 0.0)
            continue
        centres.append(float(values[assignment == index].mean()))
        rates.append(float(members.mean()))
    if sum(1 for count in counts if count) < CLASSES_NEEDED_FOR_A_FIT:
        return None
    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip", increasing=True)
    model.fit(
        numpy.asarray(centres, dtype=float),
        numpy.asarray(rates, dtype=float),
        sample_weight=numpy.asarray(counts, dtype=float),
    )
    fitted = model.predict(numpy.asarray(centres, dtype=float))
    return BinnedCalibration(
        edges=tuple(float(edge) for edge in edges),
        bin_rates=tuple(rates),
        bin_counts=tuple(counts),
        pooled_support=_pooled_level_set_support(fitted, counts),
        model=model,
        observations=total,
    )


def _pooled_level_set_support(
    fitted: numpy.typing.NDArray[numpy.float64], counts: list[int]
) -> tuple[int, ...]:
    """Total observations in each bin's isotonic LEVEL SET — the run PAVA pooled to one value.

    A prediction is supported by every observation the fit used to produce it, and PAVA produces one
    value per pooled run. Reporting a single bin's count instead pinned the evidence at `sqrt(n)`
    (`docs/research/261` MAJOR-B).
    """
    support = [0] * len(counts)
    start = 0
    for index in range(1, len(counts) + 1):
        if index == len(counts) or not math.isclose(
            float(fitted[index]), float(fitted[start]), rel_tol=0.0, abs_tol=1e-12
        ):
            block = sum(counts[start:index])
            for position in range(start, index):
                support[position] = block
            start = index
    return tuple(support)


def _own_support_near(own: Sequence[ForecastOutcome], stated: float) -> int:
    """How many of this bot's OWN forecasts sit near the queried value.

    The evidence count on the fallback paths, where no own fit exists. `len(own)` is wrong for the
    same reason it was wrong on the fitted path: it counts trades made at stated values this
    proposal never touches, and every one of them tightens the posterior.

    "Near" is the bot's own inter-quartile spread of stated values — derived from its record rather
    than chosen — and at least the two observations a Beta needs to have a shape.
    """
    if not own:
        return 0
    values = numpy.asarray([row.stated_probability for row in own], dtype=float)
    spread = float(numpy.subtract(*numpy.percentile(values, [75, 25]))) / 2.0
    if not math.isfinite(spread) or spread <= 0.0:
        return int(values.size)
    return int(numpy.count_nonzero(numpy.abs(values - stated) <= spread))


def _empirical_bayes_concentration(sibling_rates: Sequence[float]) -> float:
    """How strongly to pull a thin bot toward its parent, measured from sibling dispersion.

    Method of moments on a Beta: with sibling win rates of mean `m` and variance `v`, the implied
    concentration is `m(1-m)/v - 1`. Tightly clustered siblings mean the parent predicts a new bot
    well and should dominate; widely dispersed siblings mean it predicts little and should not.

    Falls back to the uniform prior's single pseudo-observation when there are too few siblings, or
    when the dispersion is degenerate — the weakest defensible pull rather than an invented one.
    """
    if len(sibling_rates) < CLASSES_NEEDED_FOR_A_FIT:
        return UNIFORM_PRIOR_PSEUDO_OBSERVATIONS
    rates = numpy.asarray(sibling_rates, dtype=float)
    mean = float(rates.mean())
    variance = float(rates.var(ddof=1))
    if variance <= 0.0 or not 0.0 < mean < 1.0:
        return UNIFORM_PRIOR_PSEUDO_OBSERVATIONS
    concentration = mean * (1.0 - mean) / variance - 1.0
    if not math.isfinite(concentration) or concentration <= 0.0:
        return UNIFORM_PRIOR_PSEUDO_OBSERVATIONS
    return concentration


class StatedProbabilityCalibrator:
    """Carried calibration state: per-bot monotone maps, their parents, and their diagnostics.

    Construction fits everything from the observation record it is handed; `calibrate` performs no
    fitting and no I/O, so it is safe on the decision path and replayable (`L5.29`'s discipline).

    **SOTA analog:** `sklearn.calibration.CalibratedClassifierCV(method="isotonic")` for the
    estimator, with the fold structure replaced by a time-blocked expanding window because financial
    outcomes are ordered and its `StratifiedKFold` default would leak the future into the past.
    """

    def __init__(self, observations: Sequence[ForecastOutcome]) -> None:
        self._observations = tuple(sorted(observations, key=lambda row: row.occurred_at))
        self._by_bot: dict[str, tuple[ForecastOutcome, ...]] = {}
        self._by_segment: dict[TradingSegment, tuple[ForecastOutcome, ...]] = {}
        grouped_by_bot: dict[str, list[ForecastOutcome]] = defaultdict(list)
        grouped_by_segment: dict[TradingSegment, list[ForecastOutcome]] = defaultdict(list)
        for row in self._observations:
            grouped_by_bot[row.bot_identity].append(row)
            grouped_by_segment[row.trading_segment].append(row)
        self._by_bot = {key: tuple(rows) for key, rows in grouped_by_bot.items()}
        self._by_segment = {key: tuple(rows) for key, rows in grouped_by_segment.items()}
        self._bot_models = {
            identity: _fit_binned_isotonic(
                [row.stated_probability for row in rows], [row.was_win for row in rows]
            )
            for identity, rows in self._by_bot.items()
        }
        self._segment_models = {
            segment: _fit_binned_isotonic(
                [row.stated_probability for row in rows], [row.was_win for row in rows]
            )
            for segment, rows in self._by_segment.items()
        }
        self._brier_by_bot: dict[str, BrierDecomposition | None] = {
            identity: self._decompose_out_of_fold(identity) for identity in self._by_bot
        }
        """Fitted ONCE, at construction. `docs/research/261` MAJOR-D: `calibrate()` called
        `brier_decomposition_for()` on every invocation, which refits one isotonic model per session
        boundary — **2.5 seconds per call at n=10,000** inside a pre-trade gate, for three
        diagnostic floats on the card. The module docstring claimed `calibrate` performs no fitting;
        that became false the moment the diagnostic was wired in."""
        self._pooled_base_rate = (
            sum(1 for row in self._observations if row.was_win) / len(self._observations)
            if self._observations
            else None
        )

    @property
    def observations_fitted(self) -> int:
        return len(self._observations)

    def bot_identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_bot))

    def base_rate_for(self, bot_identity: str) -> float | None:
        """This bot's realised win rate. `None` when it has no record."""
        rows = self._by_bot.get(bot_identity, ())
        if not rows:
            return None
        return sum(1 for row in rows if row.was_win) / len(rows)

    def calibrate(
        self,
        stated_probability: float,
        *,
        bot_identity: str,
        trading_segment: TradingSegment,
    ) -> CalibratedProbability:
        """Correct one stated probability against the record, and say what the correction rests on.

        The fallback chain is bot → segment → pooled → uncalibrated, and the method that was used is
        carried on the result rather than inferred by the caller, because "corrected by its own
        3,049 trades" and "held to a pooled base rate" are different claims that must not look alike
        on a card.
        """
        if not math.isfinite(stated_probability) or not 0.0 <= stated_probability <= 1.0:
            raise TradeQualityError(
                f"{bot_identity} asked to calibrate {stated_probability!r}, which is not a "
                f"probability"
            )
        own = self._by_bot.get(bot_identity, ())
        own_model = self._bot_models.get(bot_identity)
        decomposition = self._brier_by_bot.get(bot_identity)
        if own_model is not None:
            corrected = own_model.predict(stated_probability)
            support = own_model.support_at(stated_probability)
            shrunk, method = self._shrink_toward_parent(
                corrected,
                own_trades=support,
                bot_identity=bot_identity,
                trading_segment=trading_segment,
            )
            return CalibratedProbability(
                stated=stated_probability,
                calibrated=shrunk,
                method=method,
                fitted_on_trades=support,
                brier_reliability=None if decomposition is None else decomposition.reliability,
                brier_resolution=None if decomposition is None else decomposition.resolution,
                brier_uncertainty=None if decomposition is None else decomposition.uncertainty,
            )
        segment_model = self._segment_models.get(trading_segment)
        if segment_model is not None:
            return CalibratedProbability(
                stated=stated_probability,
                calibrated=segment_model.predict(stated_probability),
                method=CalibrationMethod.SHRUNK_TO_SEGMENT,
                # The BOT's own trades, and only those that actually sit near this stated value.
                # `docs/research/261` CRITICAL-E: `len(own)` here kept the original CRITICAL-2 alive
                # on every path the binning repair did not cover — added losses still raised
                # admission as lifetime P&L collapsed from +6,800 to -13,200.
                fitted_on_trades=_own_support_near(own, stated_probability),
                brier_reliability=None,
                brier_resolution=None,
                brier_uncertainty=None,
            )
        if self._pooled_base_rate is not None:
            return CalibratedProbability(
                stated=stated_probability,
                calibrated=self._pooled_base_rate,
                method=CalibrationMethod.POOLED_BASE_RATE,
                fitted_on_trades=_own_support_near(own, stated_probability),
                brier_reliability=None,
                brier_resolution=None,
                brier_uncertainty=None,
            )
        return CalibratedProbability(
            stated=stated_probability,
            calibrated=stated_probability,
            method=CalibrationMethod.UNCALIBRATED,
            fitted_on_trades=0,
            brier_reliability=None,
            brier_resolution=None,
            brier_uncertainty=None,
        )

    def _shrink_toward_parent(
        self,
        corrected: float,
        *,
        own_trades: int,
        bot_identity: str,
        trading_segment: TradingSegment,
    ) -> tuple[float, CalibrationMethod]:
        """Pull a thin bot's own correction toward its segment, by empirical-Bayes weight.

        With `n` own trades, a parent estimate `q` and a concentration `k` measured from how much
        sibling bots actually differ, the shrunk estimate is `(n*p + k*q) / (n + k)`. At `n` large
        the bot's own record dominates and the parent vanishes; at `n = 0` the parent is all there
        is. Nothing switches over at a threshold, which is the point — a hard minimum-trade cutoff
        would make the estimate jump discontinuously at whatever number was chosen for it.
        """
        # Any row in the segment, not just the bot's first (`docs/research/261` MINOR-4: a bot that
        # traded two segments was assigned by its earliest trade alone).
        siblings = [
            identity
            for identity, rows in self._by_bot.items()
            if identity != bot_identity
            and any(row.trading_segment is trading_segment for row in rows)
        ]
        sibling_rates = [
            rate for identity in siblings if (rate := self.base_rate_for(identity)) is not None
        ]
        # The parent EXCLUDES this bot. `docs/research/261` MINOR-3: shrinking a bot toward a
        # population containing itself is a no-op dressed as regularisation, and for the sole bot in
        # a segment it was shrinkage toward its own base rate.
        segment_rows = tuple(
            row
            for row in self._by_segment.get(trading_segment, ())
            if row.bot_identity != bot_identity
        )
        parent_rows = segment_rows or tuple(
            row for row in self._observations if row.bot_identity != bot_identity
        )
        if not parent_rows:
            return corrected, CalibrationMethod.ISOTONIC_BINNED
        parent_rate = sum(1 for row in parent_rows if row.was_win) / len(parent_rows)
        concentration = _empirical_bayes_concentration(sibling_rates)
        weight = float(own_trades)
        shrunk = (weight * corrected + concentration * parent_rate) / (weight + concentration)
        # The label names where MOST of the estimate came from. `docs/research/261` MINOR-2: the
        # old test was `weight >= concentration` with a concentration that falls back to 1.0, so any
        # bot with a single trade was labelled as fitted on its own record while being two-thirds
        # its parent's.
        method = (
            CalibrationMethod.ISOTONIC_BINNED
            if weight > concentration * 3.0
            else CalibrationMethod.SHRUNK_TO_SEGMENT
        )
        return min(max(shrunk, 0.0), 1.0), method

    def brier_decomposition_for(self, bot_identity: str) -> BrierDecomposition | None:
        """The cached out-of-fold diagnostic. Fitting happens once, in `__init__`."""
        return self._brier_by_bot.get(bot_identity)

    def _decompose_out_of_fold(self, bot_identity: str) -> BrierDecomposition | None:
        """Score this bot's forecasts out of fold, by expanding window over whole sessions.

        `None` when the record spans fewer than two sessions, or when no fold could be fitted — an
        absence of diagnostic, which is not the same as a diagnostic saying the forecaster is fine.
        """
        rows = self._by_bot.get(bot_identity, ())
        sessions = sorted({row.session_date for row in rows})
        if len(sessions) < SESSIONS_NEEDED_FOR_OUT_OF_FOLD:
            return None
        predicted: list[float] = []
        realised: list[bool] = []
        excluded = sum(1 for row in rows if row.session_date == sessions[0])
        for boundary in sessions[1:]:
            history = [row for row in rows if row.session_date < boundary]
            fold = [row for row in rows if row.session_date == boundary]
            model = _fit_binned_isotonic(
                [row.stated_probability for row in history], [row.was_win for row in history]
            )
            if model is None:
                excluded += len(fold)
                continue
            for row in fold:
                predicted.append(model.predict(row.stated_probability))
                realised.append(row.was_win)
        if not predicted:
            return None
        return _decompose_brier(predicted, realised, excluded_first_session=excluded)


def _decompose_brier(
    predicted: Sequence[float], realised: Sequence[bool], *, excluded_first_session: int
) -> BrierDecomposition:
    """Murphy's three terms, binned on the DISTINCT predicted values.

    Isotonic output is a step function, so its distinct values are the natural bins — the pooled
    adjacent violators that produced them. Choosing a bin count instead would be a constant
    governing a measurement (`R.03`), and a poorly chosen one moves reliability by more than the
    effect being measured.
    """
    if len(predicted) != len(realised):
        raise TradeQualityError("the prediction and outcome series have different lengths")
    total = len(predicted)
    base_rate = sum(1 for outcome in realised if outcome) / total
    bins: dict[float, list[bool]] = defaultdict(list)
    for forecast, outcome in zip(predicted, realised, strict=True):
        bins[forecast].append(outcome)
    reliability = 0.0
    resolution = 0.0
    for forecast, outcomes in bins.items():
        share = len(outcomes) / total
        observed = sum(1 for outcome in outcomes if outcome) / len(outcomes)
        reliability += share * (forecast - observed) ** 2
        resolution += share * (observed - base_rate) ** 2
    return BrierDecomposition(
        reliability=reliability,
        resolution=resolution,
        uncertainty=base_rate * (1.0 - base_rate),
        scored_forecasts=total,
        excluded_first_session_forecasts=excluded_first_session,
    )
