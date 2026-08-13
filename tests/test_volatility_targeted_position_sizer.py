"""Tests for the position sizer — written before the implementation (`R.23` step 3).

This is the module that turns "we like this trade" into a number of shares, so its adversarial
block is the list of ways a sizer produces a confident wrong number rather than an obvious failure:
a lot size that does not divide the budget, a lot size that is absent, a zero-volatility instrument,
an edge that cannot be distinguished from noise, capital that is not there, and rounding that goes
the wrong way at the boundary.

`docs/research/227` §3 is the specification.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCapture,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    PositionSizingError,
    SizingInputs,
    VolatilityTargetedPositionSizer,
)

IST = ZoneInfo("Asia/Kolkata")
TEN_LAKH = Decimal("1000000")


def _capture(*, mean_bps: str = "40", se_bps: str = "8", sigma: str = "0.30") -> ReversionCapture:
    return ReversionCapture(
        deviation_bucket=Decimal("2.0"),
        horizon_bars=5,
        event_count=5_000,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 2,
        standard_error_bps=Decimal(se_bps),
        mean_captured_sigma=Decimal(sigma),
        fitted_through=date(2026, 8, 12),
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
    )


def _closes(count: int = 60, *, step: str = "2") -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 8, 3, 9, 15, tzinfo=IST)
    price = Decimal("1000")
    series: list[tuple[datetime, Decimal]] = []
    for index in range(count):
        price = price + Decimal(step) if index % 2 else price - Decimal(step)
        series.append((start + timedelta(minutes=index), price))
    return series


def _inputs(
    *,
    deployable: Decimal = TEN_LAKH,
    lot_size: int = 1,
    price: str = "1000",
    capacity: int = 6,
    capture: ReversionCapture | None = None,
    closes: list[tuple[datetime, Decimal]] | None = None,
) -> SizingInputs:
    return SizingInputs(
        trading_symbol="RELIANCE",
        deployable_rupees=deployable,
        reference_price_rupees=Decimal(price),
        lot_size=lot_size,
        concurrent_position_capacity=capacity,
        horizon_bars=5,
        calibration=capture or _capture(),
        recent_closes=closes if closes is not None else _closes(),
    )


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_the_size_is_the_smaller_of_the_volatility_budget_and_the_kelly_cap() -> None:
    """§3.3 — `min` of the two, and the sizer says WHICH bound bound it."""
    sized = VolatilityTargetedPositionSizer().size(_inputs())
    assert sized.quantity > 0
    assert sized.notional_rupees == min(
        sized.volatility_target_notional_rupees, sized.kelly_notional_rupees
    )
    assert sized.binding_bound in {"volatility_target", "kelly_cap"}


@pytest.mark.unit
def test_the_risk_fraction_is_one_over_the_concurrent_capacity_and_not_a_constant() -> None:
    """`R.03` — the target risk fraction is a structural fact of the segment set.

    Six segments that may each hold a position means each may risk a sixth. Change the capacity and
    the fraction moves with it; nothing is edited.
    """
    sizer = VolatilityTargetedPositionSizer()
    six = sizer.size(_inputs(capacity=6))
    three = sizer.size(_inputs(capacity=3))
    assert six.target_risk_fraction == Decimal(1) / Decimal(6)
    assert three.target_risk_fraction == Decimal(1) / Decimal(3)
    assert three.volatility_target_notional_rupees > six.volatility_target_notional_rupees


@pytest.mark.unit
def test_a_more_volatile_instrument_is_sized_smaller() -> None:
    """The whole point of volatility targeting, asserted directly."""
    sizer = VolatilityTargetedPositionSizer()
    calm = sizer.size(_inputs(closes=_closes(step="1")))
    wild = sizer.size(_inputs(closes=_closes(step="8")))
    assert wild.volatility_target_notional_rupees < calm.volatility_target_notional_rupees


@pytest.mark.unit
def test_a_worse_measured_edge_is_sized_smaller_through_the_kelly_cap() -> None:
    sizer = VolatilityTargetedPositionSizer()
    sharp = sizer.size(_inputs(capture=_capture(mean_bps="40", se_bps="4")))
    fuzzy = sizer.size(_inputs(capture=_capture(mean_bps="40", se_bps="40")))
    assert fuzzy.kelly_notional_rupees < sharp.kelly_notional_rupees


@pytest.mark.unit
def test_the_quantity_is_a_whole_number_of_lots(  ) -> None:
    """`L1.09` — an order is lots, not shares, and a part lot is not an order."""
    sized = VolatilityTargetedPositionSizer().size(_inputs(lot_size=65, price="200"))
    assert sized.quantity % 65 == 0
    assert sized.lots * 65 == sized.quantity


@pytest.mark.unit
def test_the_sizer_explains_itself_in_the_terms_that_produced_the_number() -> None:
    described = VolatilityTargetedPositionSizer().size(_inputs()).describe()
    for fragment in ("volatility", "Kelly", "lot"):
        assert fragment.lower() in described.lower()


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_rounding_is_always_down_at_the_lot_boundary() -> None:
    """One more lot is a leverage decision wearing the costume of a rounding convention.

    Constructed so the budget buys 2.9 lots: the answer is 2, never 3.
    """
    sizer = VolatilityTargetedPositionSizer()
    sized = sizer.size(_inputs(lot_size=100, price="1000"))
    affordable_lots = sized.notional_rupees / (Decimal(100) * Decimal(1000))
    assert sized.lots == int(affordable_lots)
    assert sized.quantity * Decimal(1000) <= sized.notional_rupees


@pytest.mark.adversarial
def test_a_budget_smaller_than_one_lot_sizes_zero_and_says_why() -> None:
    """A real and common state for a small book against a large-lot contract.

    It must be legible rather than mysterious: the answer is zero WITH a reason, not a silent
    zero and not a part lot.
    """
    sized = VolatilityTargetedPositionSizer().size(
        _inputs(deployable=Decimal("50000"), lot_size=1000, price="2500")
    )
    assert sized.quantity == 0
    assert sized.refusal_reason is not None
    assert "lot" in sized.refusal_reason.lower()


@pytest.mark.adversarial
def test_a_zero_volatility_instrument_is_refused_rather_than_sized_infinitely() -> None:
    """Zero in the denominator is an unbounded position, which is the worst available answer."""
    flat = [
        (datetime(2026, 8, 3, 9, 15, tzinfo=IST) + timedelta(minutes=index), Decimal("500"))
        for index in range(40)
    ]
    with pytest.raises(PositionSizingError):
        VolatilityTargetedPositionSizer().size(_inputs(closes=flat))


@pytest.mark.adversarial
def test_a_negative_calibrated_edge_sizes_zero_rather_than_reversing_the_trade() -> None:
    sized = VolatilityTargetedPositionSizer().size(
        _inputs(capture=_capture(mean_bps="-20", se_bps="10"))
    )
    assert sized.quantity == 0
    assert sized.refusal_reason is not None
    assert "negative" in sized.refusal_reason


@pytest.mark.adversarial
def test_no_deployable_capital_sizes_zero_and_never_a_hopeful_one_lot() -> None:
    """The `-88` case. A book with nothing in it trades nothing, including in paper."""
    sized = VolatilityTargetedPositionSizer().size(_inputs(deployable=Decimal(0)))
    assert sized.quantity == 0
    assert sized.refusal_reason is not None


@pytest.mark.adversarial
@pytest.mark.parametrize("lot_size", [0, -1])
def test_an_impossible_lot_size_is_refused_rather_than_defaulted_to_one(lot_size: int) -> None:
    """`L1.09` — a hardcoded fallback lot size is the defect, not the safety net.

    The archived system's hardcoded map went stale (NIFTY 65, not 75). Assuming 1 when the
    instrument master has no lot size would size a derivative like a share.
    """
    with pytest.raises(PositionSizingError):
        VolatilityTargetedPositionSizer().size(_inputs(lot_size=lot_size))


@pytest.mark.adversarial
@pytest.mark.parametrize("capacity", [0, -3])
def test_an_impossible_capacity_is_refused_because_it_divides_the_book(capacity: int) -> None:
    with pytest.raises(PositionSizingError):
        VolatilityTargetedPositionSizer().size(_inputs(capacity=capacity))


@pytest.mark.adversarial
def test_a_non_positive_reference_price_is_refused() -> None:
    with pytest.raises(PositionSizingError):
        VolatilityTargetedPositionSizer().size(_inputs(price="0"))


@pytest.mark.adversarial
def test_thin_price_history_is_refused_rather_than_sized_from_noise() -> None:
    """`R.04` — activation is gated, the algorithm is not reduced."""
    with pytest.raises(PositionSizingError):
        VolatilityTargetedPositionSizer().size(_inputs(closes=_closes(count=10)))


@pytest.mark.adversarial
def test_a_float_anywhere_in_the_money_path_is_refused() -> None:
    with pytest.raises((PositionSizingError, TypeError)):
        VolatilityTargetedPositionSizer().size(_inputs(deployable=1000000.0))  # type: ignore[arg-type]


# -------------------------------------------------------------------------- property


@pytest.mark.property
@settings(max_examples=120, deadline=None)
@given(
    deployable=hypothesis_strategies.decimals(
        min_value=Decimal("100000"), max_value=Decimal("10000000"), places=2, allow_nan=False
    ),
    lot_size=hypothesis_strategies.integers(min_value=1, max_value=1000),
    price=hypothesis_strategies.decimals(
        min_value=Decimal("1"), max_value=Decimal("5000"), places=2, allow_nan=False
    ),
)
def test_the_position_never_costs_more_than_the_capital_it_was_sized_against(
    deployable: Decimal, lot_size: int, price: Decimal
) -> None:
    """The one property that must never fail: a sizer may be wrong, it may not be unfundable."""
    sized = VolatilityTargetedPositionSizer().size(
        _inputs(deployable=deployable, lot_size=lot_size, price=str(price))
    )
    assert sized.quantity >= 0
    assert sized.quantity * price <= deployable


@pytest.mark.property
@settings(max_examples=120, deadline=None)
@given(
    deployable=hypothesis_strategies.decimals(
        min_value=Decimal("100000"), max_value=Decimal("10000000"), places=2, allow_nan=False
    ),
    lot_size=hypothesis_strategies.integers(min_value=1, max_value=500),
)
def test_quantity_is_zero_exactly_when_a_reason_is_given(
    deployable: Decimal, lot_size: int
) -> None:
    """Zero and refused must agree — one is computed from the other, never checked against it."""
    sized = VolatilityTargetedPositionSizer().size(
        _inputs(deployable=deployable, lot_size=lot_size)
    )
    assert (sized.quantity == 0) == (sized.refusal_reason is not None)


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    extra=hypothesis_strategies.decimals(
        min_value=Decimal("0"), max_value=Decimal("5000000"), places=2, allow_nan=False
    )
)
def test_more_capital_never_produces_a_smaller_position(extra: Decimal) -> None:
    sizer = VolatilityTargetedPositionSizer()
    smaller = sizer.size(_inputs(deployable=TEN_LAKH))
    larger = sizer.size(_inputs(deployable=TEN_LAKH + extra))
    assert larger.quantity >= smaller.quantity
