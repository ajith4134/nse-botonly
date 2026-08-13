"""Tests for the shrunk-Kelly scaler — written before the implementation (`R.23` step 3).

The arithmetic here decides how large a real order is, so the adversarial block carries the cases
where Kelly is known to be dangerous: a large edge with a large standard error, an edge
indistinguishable from zero, a negative lower bound, and a variance that rounds to nothing.

`docs/research/227` §3.2 is the specification. The shrinkage table in it is reproduced exactly by
`test_the_shrinkage_table_in_the_spec_is_reproduced`, so the document and the code cannot drift.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCapture,
)
from nse_algo_trader.sizing.kelly_edge_scaler import (
    KellyEdgeScaler,
    KellyScalingError,
    shrinkage_weight,
)

BASIS_POINTS_IN_ONE = Decimal(10_000)


def _capture(
    *,
    mean_bps: str,
    standard_error_bps: str,
    sigma: str = "0.30",
    events: int = 5_000,
) -> ReversionCapture:
    return ReversionCapture(
        deviation_bucket=Decimal("2.0"),
        horizon_bars=5,
        event_count=events,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 2,
        standard_error_bps=Decimal(standard_error_bps),
        mean_captured_sigma=Decimal(sigma),
        fitted_through=date(2026, 8, 12),
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
    )


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_the_shrinkage_table_in_the_spec_is_reproduced() -> None:
    """`docs/research/227` §3.2 — the document and the code must not drift apart.

    Shrinkage is `edge² / (edge² + se²)`, which is `t² / (t² + 1)`. The table is stated in the spec
    in terms an operator reads, and it is asserted here in the same terms.
    """
    expected = {
        Decimal(5): Decimal("0.96"),
        Decimal(2): Decimal("0.80"),
        Decimal(1): Decimal("0.50"),
        Decimal("0.5"): Decimal("0.20"),
    }
    for t_statistic, weight in expected.items():
        computed = shrinkage_weight(
            edge_bps=t_statistic * Decimal(10), standard_error_bps=Decimal(10)
        )
        assert round(computed, 2) == weight, f"t={t_statistic}"


@pytest.mark.unit
def test_a_well_measured_edge_is_barely_shrunk_and_a_noisy_one_is_nearly_erased() -> None:
    well_measured = shrinkage_weight(edge_bps=Decimal(50), standard_error_bps=Decimal(5))
    noise = shrinkage_weight(edge_bps=Decimal(50), standard_error_bps=Decimal(200))
    assert well_measured > Decimal("0.98")
    assert noise < Decimal("0.06")


@pytest.mark.unit
def test_the_kelly_fraction_is_edge_over_variance_shrunk_by_reliability() -> None:
    """The whole scaler in one assertion, against arithmetic done by hand."""
    capture = _capture(mean_bps="40", standard_error_bps="10", sigma="0.25")
    scaler = KellyEdgeScaler()
    scaled = scaler.scale(capture)

    edge_fraction = Decimal(40) / BASIS_POINTS_IN_ONE
    variance = Decimal("0.25") ** 2
    raw = edge_fraction / variance
    weight = Decimal(40) ** 2 / (Decimal(40) ** 2 + Decimal(10) ** 2)

    assert scaled.raw_kelly_fraction == pytest.approx(raw, rel=Decimal("1e-9"))
    assert scaled.shrinkage == pytest.approx(weight, rel=Decimal("1e-9"))
    assert scaled.kelly_fraction == pytest.approx(raw * weight, rel=Decimal("1e-9"))


@pytest.mark.unit
def test_the_scaled_fraction_is_always_smaller_than_the_raw_one() -> None:
    """Shrinkage is a cap, never an amplifier — there is no input that makes it size UP."""
    scaler = KellyEdgeScaler()
    for mean, error in (("40", "1"), ("40", "20"), ("5", "5"), ("300", "0.5")):
        scaled = scaler.scale(_capture(mean_bps=mean, standard_error_bps=error))
        assert scaled.kelly_fraction <= scaled.raw_kelly_fraction
        assert Decimal(0) < scaled.shrinkage <= Decimal(1)


@pytest.mark.unit
def test_the_scaler_reports_what_an_operator_needs_to_argue_with_it() -> None:
    scaled = KellyEdgeScaler().scale(_capture(mean_bps="40", standard_error_bps="10"))
    described = scaled.describe()
    assert "40" in described
    assert "shrunk" in described
    assert str(scaled.t_statistic.quantize(Decimal("0.01"))) in described


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_an_edge_that_cannot_be_distinguished_from_zero_is_halved_or_worse() -> None:
    """`t <= 1` is the regime where Kelly is most dangerous and most confident."""
    scaled = KellyEdgeScaler().scale(_capture(mean_bps="10", standard_error_bps="10"))
    assert scaled.shrinkage == Decimal("0.5")
    assert scaled.is_distinguishable_from_zero is False


@pytest.mark.adversarial
def test_a_negative_edge_yields_no_position_rather_than_a_short_one() -> None:
    """A calibration whose mean is negative is not an invitation to reverse the trade.

    The strategy this edge belongs to is a mean-reversion LONG; a negative measured capture says
    the edge is absent, not that its mirror image works. Sizing a short off it would be inventing a
    strategy nobody fitted.
    """
    scaled = KellyEdgeScaler().scale(_capture(mean_bps="-15", standard_error_bps="10"))
    assert scaled.kelly_fraction == Decimal(0)
    assert scaled.refusal_reason is not None
    assert "negative" in scaled.refusal_reason


@pytest.mark.adversarial
def test_a_zero_variance_calibration_is_refused_rather_than_dividing_by_it() -> None:
    """Zero dispersion means infinite Kelly, which is the single worst answer available."""
    with pytest.raises(KellyScalingError):
        KellyEdgeScaler().scale(_capture(mean_bps="40", standard_error_bps="10", sigma="0"))


@pytest.mark.adversarial
def test_a_zero_standard_error_does_not_claim_perfect_knowledge() -> None:
    """`se == 0` makes the shrinkage formula read 1.0 — certainty — which no finite sample earns.

    The calibrator permits `standard_error_bps == 0` (it only refuses negatives), and a sample of
    one identical value would produce it. Treating that as a perfectly measured edge is how a
    degenerate sample becomes maximum leverage.
    """
    with pytest.raises(KellyScalingError):
        KellyEdgeScaler().scale(_capture(mean_bps="40", standard_error_bps="0"))


@pytest.mark.adversarial
def test_the_kelly_fraction_is_capped_at_the_whole_book() -> None:
    """A huge edge over a tiny variance produces `f > 1`, which means borrowing.

    Full Kelly on a 300 bps edge with 2% dispersion is 7.5-fold capital. This engine sizes a paper
    and
    cash-intraday book; leverage is the risk gate's decision (`L7.02`), never a side effect of the
    sizer's arithmetic overflowing past one.
    """
    scaled = KellyEdgeScaler().scale(_capture(mean_bps="300", standard_error_bps="5", sigma="0.02"))
    assert scaled.raw_kelly_fraction > Decimal(1)
    assert scaled.kelly_fraction == Decimal(1)
    assert scaled.was_capped_at_full_capital is True


@pytest.mark.adversarial
def test_a_float_edge_is_refused_because_money_arithmetic_is_decimal_here() -> None:
    with pytest.raises((KellyScalingError, TypeError)):
        shrinkage_weight(edge_bps=40.0, standard_error_bps=Decimal(10))  # type: ignore[arg-type]


# -------------------------------------------------------------------------- property


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    mean=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("500"), places=2, allow_nan=False
    ),
    error=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("500"), places=2, allow_nan=False
    ),
    sigma=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("2"), places=2, allow_nan=False
    ),
)
def test_shrinkage_is_monotone_in_measurement_quality(
    mean: Decimal, error: Decimal, sigma: Decimal
) -> None:
    """More standard error must never produce MORE size. This is the property Kelly gets wrong."""
    scaler = KellyEdgeScaler()
    tighter = scaler.scale(_capture(mean_bps=str(mean), standard_error_bps=str(error),
    sigma=str(sigma)))
    looser = scaler.scale(
        _capture(mean_bps=str(mean), standard_error_bps=str(error * 2), sigma=str(sigma))
    )
    assert looser.shrinkage <= tighter.shrinkage
    assert looser.kelly_fraction <= tighter.kelly_fraction


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    mean=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("500"), places=2, allow_nan=False
    ),
    error=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("500"), places=2, allow_nan=False
    ),
    sigma=hypothesis_strategies.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("2"), places=2, allow_nan=False
    ),
)
def test_the_result_is_always_a_usable_fraction_of_capital(
    mean: Decimal, error: Decimal, sigma: Decimal
) -> None:
    scaled = KellyEdgeScaler().scale(
        _capture(mean_bps=str(mean), standard_error_bps=str(error), sigma=str(sigma))
    )
    assert Decimal(0) <= scaled.kelly_fraction <= Decimal(1)
    assert Decimal(0) < scaled.shrinkage <= Decimal(1)
