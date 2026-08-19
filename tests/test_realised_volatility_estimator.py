"""Tests for the realised-volatility estimator — written before the implementation.

This number is the denominator of every position size this system will ever take, so a wrong one is
not a wrong statistic, it is a wrong order. The adversarial block therefore concentrates on the
inputs that make a volatility estimate silently wrong rather than loudly absent: a flat series, a
series with one enormous gap, a series short enough that the estimate is noise, and a series whose
bars arrive out of order.

`docs/research/227` §3.1 is the specification.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.sizing.realised_volatility_estimator import (
    MINIMUM_CLOSES_FOR_AN_ESTIMATE,
    RealisedVolatilityEstimator,
    ThinVolatilityHistoryError,
    VolatilityEstimationError,
)

IST = ZoneInfo("Asia/Kolkata")


def _closes(values: list[str]) -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 8, 3, 9, 15, tzinfo=IST)
    return [
        (start + timedelta(minutes=index), Decimal(value)) for index, value in enumerate(values)
    ]


def _steady_series(
    count: int, *, start: str = "1000", step: str = "1"
) -> list[tuple[datetime, Decimal]]:
    price = Decimal(start)
    values: list[str] = []
    for index in range(count):
        price = price + Decimal(step) if index % 2 else price - Decimal(step)
        values.append(str(price))
    return _closes(values)


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_series_that_never_moves_has_zero_volatility_and_says_so() -> None:
    """Zero is the correct answer here and a dangerous one downstream, so it is FLAGGED.

    A zero denominator makes the volatility-targeted size infinite. The estimator's job is to
    report the truth; refusing to divide by it is the sizer's.
    """
    estimate = RealisedVolatilityEstimator().estimate(_closes(["100"] * 40))
    assert estimate.sigma_bps == Decimal(0)
    assert estimate.is_degenerate is True


@pytest.mark.unit
def test_a_moving_series_produces_a_positive_estimate_in_basis_points() -> None:
    estimate = RealisedVolatilityEstimator().estimate(_steady_series(60))
    assert estimate.sigma_bps > 0
    assert estimate.is_degenerate is False
    assert estimate.observation_count == 59  # returns, not closes


@pytest.mark.unit
def test_doubling_every_move_doubles_the_estimate() -> None:
    """Scale invariance in the right direction — the estimate is a rate, not a level."""
    calm = RealisedVolatilityEstimator().estimate(_steady_series(60, step="1"))
    wild = RealisedVolatilityEstimator().estimate(_steady_series(60, step="2"))
    ratio = wild.sigma_bps / calm.sigma_bps
    assert Decimal("1.9") < ratio < Decimal("2.1")


@pytest.mark.unit
def test_the_estimate_scales_with_the_square_root_of_the_horizon() -> None:
    """A four-bar horizon carries twice the one-bar risk, not four times.

    Sizing against a horizon-scaled sigma is the whole reason the horizon is an argument; getting
    the exponent wrong doubles or halves every position in the book.
    """
    estimator = RealisedVolatilityEstimator()
    series = _steady_series(80)
    one_bar = estimator.estimate(series, horizon_bars=1)
    four_bars = estimator.estimate(series, horizon_bars=4)
    ratio = four_bars.sigma_bps / one_bar.sigma_bps
    assert Decimal("1.95") < ratio < Decimal("2.05")


@pytest.mark.unit
def test_recent_moves_weigh_more_than_old_ones() -> None:
    """EWMA, not a flat window — a calm month must not hide this morning."""
    calm_then_wild = _closes(["1000"] * 40 + ["1000", "1100", "1000", "1100", "1000", "1100"])
    wild_then_calm = _closes(["1000", "1100"] * 3 + ["1000"] * 40)
    estimator = RealisedVolatilityEstimator()
    assert (
        estimator.estimate(calm_then_wild).sigma_bps > estimator.estimate(wild_then_calm).sigma_bps
    )


@pytest.mark.unit
def test_the_estimate_describes_itself_in_terms_an_operator_can_check() -> None:
    described = RealisedVolatilityEstimator().estimate(_steady_series(60)).describe()
    assert "bps" in described
    assert "59" in described


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_too_few_closes_is_refused_rather_than_estimated_from_noise() -> None:
    """`R.04` in its correct form: the ALGORITHM is not reduced, its ACTIVATION is gated.

    An estimate from three closes is not a small estimate, it is a number with no information in
    it, and it would size a real order exactly as confidently as a good one.
    """
    with pytest.raises(ThinVolatilityHistoryError):
        RealisedVolatilityEstimator().estimate(_steady_series(MINIMUM_CLOSES_FOR_AN_ESTIMATE - 1))


@pytest.mark.adversarial
def test_exactly_the_minimum_is_accepted_because_the_bound_is_a_bound() -> None:
    estimate = RealisedVolatilityEstimator().estimate(
        _steady_series(MINIMUM_CLOSES_FOR_AN_ESTIMATE)
    )
    assert estimate.sigma_bps > 0


@pytest.mark.adversarial
def test_closes_arriving_out_of_order_are_refused_rather_than_sorted() -> None:
    """Silently sorting would hide a real defect in whatever produced the series.

    Returns computed across a mis-ordered series are not merely noisy; they are the wrong sign as
    often as not, and an estimator that quietly repairs its input removes the only evidence that
    its caller is broken.
    """
    series = _steady_series(40)
    scrambled = [series[5], *series[:5], *series[6:]]
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(scrambled)


@pytest.mark.adversarial
def test_a_duplicated_timestamp_is_refused() -> None:
    series = _steady_series(40)
    duplicated = [*series, series[-1]]
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(duplicated)


@pytest.mark.adversarial
def test_a_non_positive_close_is_refused_because_a_log_return_needs_a_ratio() -> None:
    series = _steady_series(40)
    with_zero = [*series[:20], (series[20][0], Decimal(0)), *series[21:]]
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(with_zero)


@pytest.mark.adversarial
def test_a_single_enormous_move_does_not_dominate_the_estimate_without_warning() -> None:
    """A 40% gap on a corporate action is not volatility, and it must be VISIBLE if it is included.

    The estimator does not silently winsorise — that would hide an adjustment defect — but it
    reports the largest single return so the caller can see what is driving the number.
    """
    # A STEP, not a spike: an unadjusted split or bonus moves the price level permanently, which is
    # one large return. A spike would be two, each roughly half the variation, and would correctly
    # NOT trip a one-move dominance test.
    series = _steady_series(60)
    stepped = [
        *series[:30],
        *[(timestamp, close * Decimal("1.4")) for timestamp, close in series[30:]],
    ]
    estimate = RealisedVolatilityEstimator().estimate(stepped)
    assert estimate.largest_absolute_return_bps > Decimal(1000)
    assert estimate.is_dominated_by_one_move is True


@pytest.mark.adversarial
def test_a_float_close_is_refused_because_prices_are_decimal_here() -> None:
    series = _steady_series(40)
    # A float where a Decimal belongs is exactly what this test injects, so the sequence is typed
    # as what a careless caller would actually hand over rather than as what the signature wants.
    with_float: list[tuple[datetime, Any]] = [
        *series[:10],
        (series[10][0], 1000.5),
        *series[11:],
    ]
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(with_float)


@pytest.mark.adversarial
def test_a_naive_timestamp_is_refused() -> None:
    naive = [
        (datetime(2026, 8, 3, 9, 15 + index), Decimal(1000 + index))  # noqa: DTZ001 — the point
        for index in range(40)
    ]
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(naive)


@pytest.mark.adversarial
@pytest.mark.parametrize("horizon", [0, -1])
def test_a_non_positive_horizon_is_refused(horizon: int) -> None:
    with pytest.raises(VolatilityEstimationError):
        RealisedVolatilityEstimator().estimate(_steady_series(40), horizon_bars=horizon)


# -------------------------------------------------------------------------- property


@pytest.mark.property
@settings(max_examples=80, deadline=None)
@given(
    closes=hypothesis_strategies.lists(
        hypothesis_strategies.decimals(
            min_value=Decimal("10"), max_value=Decimal("10000"), places=2, allow_nan=False
        ),
        min_size=MINIMUM_CLOSES_FOR_AN_ESTIMATE,
        max_size=90,
    )
)
def test_the_estimate_is_never_negative_and_never_infinite(closes: list[Decimal]) -> None:
    estimate = RealisedVolatilityEstimator().estimate(_closes([str(value) for value in closes]))
    assert estimate.sigma_bps >= 0
    assert estimate.sigma_bps.is_finite()
    assert estimate.observation_count == len(closes) - 1


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(
    closes=hypothesis_strategies.lists(
        hypothesis_strategies.decimals(
            min_value=Decimal("10"), max_value=Decimal("10000"), places=2, allow_nan=False
        ),
        min_size=MINIMUM_CLOSES_FOR_AN_ESTIMATE,
        max_size=60,
    ),
    horizon=hypothesis_strategies.integers(min_value=1, max_value=25),
)
def test_a_longer_horizon_never_produces_a_smaller_estimate(
    closes: list[Decimal], horizon: int
) -> None:
    """Risk over more time is never less. A sizer that got this backwards would size UP on a
    longer hold, which is the exact inversion that turns a horizon into leverage."""
    estimator = RealisedVolatilityEstimator()
    series = _closes([str(value) for value in closes])
    shorter = estimator.estimate(series, horizon_bars=horizon)
    longer = estimator.estimate(series, horizon_bars=horizon + 1)
    assert longer.sigma_bps >= shorter.sigma_bps


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(
    closes=hypothesis_strategies.lists(
        hypothesis_strategies.decimals(
            min_value=Decimal("10"), max_value=Decimal("10000"), places=2, allow_nan=False
        ),
        min_size=MINIMUM_CLOSES_FOR_AN_ESTIMATE,
        max_size=60,
    ),
    scale=hypothesis_strategies.integers(min_value=2, max_value=50),
)
def test_multiplying_every_price_leaves_the_estimate_unchanged(
    closes: list[Decimal], scale: int
) -> None:
    """A stock split must not change how much risk the book thinks it is taking."""
    estimator = RealisedVolatilityEstimator()
    plain = estimator.estimate(_closes([str(value) for value in closes]))
    scaled = estimator.estimate(_closes([str(value * scale) for value in closes]))
    assert abs(scaled.sigma_bps - plain.sigma_bps) < Decimal("0.5")
