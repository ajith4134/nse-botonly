"""How much this instrument actually moves — the denominator of every position size (`L7.01`).

Specification: `docs/research/227_position_sizing_and_risk_gate_spec.md` §3.1.

Volatility targeting sizes a position so that its expected rupee movement hits a budget, which means
this number divides into the budget. A volatility estimate that is half the truth doubles every
order it touches, so this module's failures are order-sizing failures and it is built to refuse
rather than approximate.

**EWMA over close-to-close log returns.** Exponentially weighted, because a flat-window estimate
lets a calm month hide this morning: the risk that matters to an intraday book is the risk it is in
now. The decay is expressed as a **half-life in bars** rather than a lambda, because a half-life is
a fact an operator can check against the session ("half the weight sits in the last N bars") while
`lambda = 0.94` is a number from somebody else's paper.

**Log returns, not simple ones.** They add across bars, which is what makes the square-root-of-time
scaling to a horizon correct rather than approximately correct, and they are symmetric in a way
simple returns are not — a 50% fall and a 100% rise are the same move backwards, and an estimator
that disagrees will systematically under-size after a crash.

**What this module refuses.** Out-of-order or duplicated timestamps (it never sorts: silently
repairing its input would erase the evidence that its caller is broken), non-positive closes, naive
timestamps, floats, and a history too thin to carry information. It does NOT refuse a series
dominated by one enormous move — a corporate-action gap that survived adjustment is a real fact
about the data — but it reports `is_dominated_by_one_move` so the caller can see what is driving the
number instead of discovering it in a position size.

**Zero is a legitimate answer.** A series that never moved has no volatility. It is reported with
`is_degenerate`, and refusing to divide by it belongs to the sizer, not here: this module's contract
is to say what the instrument did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise

BASIS_POINTS_IN_ONE = Decimal(10_000)
"""A basis point is one ten-thousandth. Definitional, not a threshold."""

MINIMUM_CLOSES_FOR_AN_ESTIMATE = 30
"""Below this the estimate carries no information, and it would size an order anyway.

`R.04` in its correct form: the algorithm is not reduced for thin data, its ACTIVATION is gated. The
figure is the conventional floor for a second moment to be worth quoting — the standard error of a
volatility estimate is roughly `sigma / sqrt(2n)`, so thirty observations already carry a ~13%
relative error and fewer than that is guesswork wearing a decimal point. Stated here rather than
buried so that raising it is a visible decision.
"""

VOLATILITY_HALF_LIFE_BARS = 20
"""How many bars back the EWMA gives half its weight.

A half-life rather than a decay constant, because an operator can check a half-life against the
session ("half the weight is in the last twenty bars") and cannot check `lambda = 0.94` against
anything. Twenty bars is one trading hour on five-minute bars and roughly a month on daily bars,
which is the horizon over which an intraday book's risk regime actually persists. This is the one
number here that is a modelling choice rather than a measurement, so it is named, explained, and
overridable per call.
"""

_DOMINANCE_SHARE = Decimal("0.5")
"""When one return contributes more than half the total squared variation, the estimate IS that
move. Reported, never corrected — see the module docstring."""


class VolatilityEstimationError(Exception):
    """The series cannot produce an estimate, and a substituted one would size a real order."""


class ThinVolatilityHistoryError(VolatilityEstimationError):
    """There is history, and there is not enough of it for a second moment to mean anything."""


@dataclass(frozen=True, slots=True)
class RealisedVolatility:
    """What the instrument did, over the horizon asked for, with its own reliability attached."""

    sigma_bps: Decimal
    horizon_bars: int
    observation_count: int
    largest_absolute_return_bps: Decimal
    largest_move_share_of_variation: Decimal
    half_life_bars: int

    @property
    def is_degenerate(self) -> bool:
        """No movement at all. A true answer, and one nothing may divide by."""
        return self.sigma_bps == 0

    @property
    def is_dominated_by_one_move(self) -> bool:
        """One return carries most of the variation — usually an unadjusted corporate action."""
        return self.largest_move_share_of_variation > _DOMINANCE_SHARE

    def describe(self) -> str:
        return (
            f"{self.sigma_bps.quantize(Decimal('0.01'))} bps over {self.horizon_bars} bar(s), "
            f"from {self.observation_count} returns (half-life {self.half_life_bars} bars); "
            f"largest single move {self.largest_absolute_return_bps.quantize(Decimal('0.01'))} bps "
            f"= {(self.largest_move_share_of_variation * 100).quantize(Decimal('0.1'))}% of the "
            "variation"
            + (" — DOMINATED BY ONE MOVE" if self.is_dominated_by_one_move else "")
        )


@dataclass(frozen=True, slots=True)
class RealisedVolatilityEstimator:
    """EWMA close-to-close volatility, in basis points, scaled to a decision horizon."""

    half_life_bars: int = VOLATILITY_HALF_LIFE_BARS
    minimum_closes: int = MINIMUM_CLOSES_FOR_AN_ESTIMATE

    def estimate(
        self,
        closes: Sequence[tuple[datetime, Decimal]],
        *,
        horizon_bars: int = 1,
    ) -> RealisedVolatility:
        """Volatility in bps of price over `horizon_bars`.

        Args:
            closes: `(timestamp, close)` in ascending time order. Timezone-aware, `Decimal` prices.
            horizon_bars: how many bars the position is expected to be held. Scaled by
                `sqrt(horizon)`, which is correct for log returns and is the difference between
                sizing a five-bar hold like a one-bar hold and sizing it like a twenty-five-bar one.

        Raises:
            ThinVolatilityHistoryError: fewer than `minimum_closes` closes.
            VolatilityEstimationError: the series is unusable — mis-ordered, duplicated,
                non-positive, naive-timestamped, or not `Decimal`.
        """
        if horizon_bars <= 0:
            raise VolatilityEstimationError(
                f"horizon must be a positive number of bars, got {horizon_bars}; a zero or "
                "negative horizon has no risk to scale"
            )
        self._validate(closes)
        if len(closes) < self.minimum_closes:
            raise ThinVolatilityHistoryError(
                f"{len(closes)} closes is below the {self.minimum_closes} this estimator requires. "
                "An estimate from fewer carries no information and would size a real order exactly "
                "as confidently as a good one; the caller must wait or widen its window rather "
                "than receive a number"
            )

        returns = [
            _log_return(previous_close, close)
            for (_, previous_close), (_, close) in pairwise(closes)
        ]
        weights = self._weights(len(returns))
        weighted_variance = sum(
            (weight * value * value for weight, value in zip(weights, returns, strict=True)),
            Decimal(0),
        )
        sigma_per_bar = weighted_variance.sqrt()
        sigma_horizon = sigma_per_bar * Decimal(horizon_bars).sqrt()

        squared = [value * value for value in returns]
        total_variation = sum(squared, Decimal(0))
        largest = max(squared) if squared else Decimal(0)
        share = largest / total_variation if total_variation > 0 else Decimal(0)

        return RealisedVolatility(
            sigma_bps=sigma_horizon * BASIS_POINTS_IN_ONE,
            horizon_bars=horizon_bars,
            observation_count=len(returns),
            largest_absolute_return_bps=largest.sqrt() * BASIS_POINTS_IN_ONE,
            largest_move_share_of_variation=share,
            half_life_bars=self.half_life_bars,
        )

    def _weights(self, count: int) -> list[Decimal]:
        """Exponential weights summing to one, heaviest on the most recent return.

        `decay ** age`, with `decay` derived from the half-life rather than stated, so the two can
        never disagree: `decay = 2 ** (-1 / half_life)` gives a weight of exactly one half at
        `age == half_life`, which is what the constant's name promises.
        """
        decay = Decimal(2) ** (Decimal(-1) / Decimal(self.half_life_bars))
        raw = [decay ** Decimal(age) for age in range(count - 1, -1, -1)]
        total = sum(raw, Decimal(0))
        return [weight / total for weight in raw]

    @staticmethod
    def _validate(closes: Sequence[tuple[datetime, Decimal]]) -> None:
        if not closes:
            raise ThinVolatilityHistoryError("an empty series has no volatility to estimate")
        previous_timestamp: datetime | None = None
        for timestamp, close in closes:
            if not isinstance(close, Decimal):
                raise VolatilityEstimationError(
                    f"close {close!r} is {type(close).__name__} and not Decimal; a float price "
                    "puts "
                    "binary rounding between the market and an order size"
                )
            if close <= 0:
                raise VolatilityEstimationError(
                    f"close {close} is not positive, and a log return needs a ratio of two prices; "
                    "a zero or negative close is a data defect and must not be averaged away"
                )
            if timestamp.tzinfo is None:
                raise VolatilityEstimationError(
                    "a naive timestamp cannot be ordered against a stamped one, and this series is "
                    "only meaningful in order"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise VolatilityEstimationError(
                    f"closes must ascend in time: {timestamp.isoformat()} does not follow "
                    f"{previous_timestamp.isoformat()}. This refuses rather than sorting, because "
                    "a mis-ordered series is evidence of a defect in whatever produced it and "
                    "quietly repairing it destroys that evidence"
                )
            previous_timestamp = timestamp


def _log_return(previous_close: Decimal, close: Decimal) -> Decimal:
    """`ln(close / previous)`, in `Decimal` throughout.

    Log rather than simple returns because they ADD across bars, which is what makes the
    square-root-of-time scaling to a horizon exact rather than approximate, and because they are
    symmetric: a halving and a doubling are the same magnitude in opposite directions, so an
    estimator built on them does not systematically under-size after a fall.
    """
    try:
        return (close / previous_close).ln()
    except (InvalidOperation, ValueError) as failure:  # pragma: no cover — guarded by _validate
        raise VolatilityEstimationError(
            f"a log return of {close} over {previous_close} could not be formed: {failure}"
        ) from failure
