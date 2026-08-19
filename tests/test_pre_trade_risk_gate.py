"""Tests for the pre-trade risk gate (`L3.05`, `L7.02`).

The gate's job is to say no, so the tests are mostly about the ways a gate says yes when it should
not: an unread ban list read as "no ban", an operator override that widens a limit, a latch that a
per-order check runs ahead of, and a refusal that hides behind whichever rule happened to be
evaluated first.

`docs/research/227` §4 is the specification.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCapture,
)
from nse_algo_trader.sizing.pre_trade_risk_gate import (
    DerivedLimits,
    GateTier,
    OperatorLimitOverrides,
    PreTradeRiskGate,
    RegulatoryFacts,
    RiskGateError,
    derive_limits,
    latches_to_trip,
)
from nse_algo_trader.sizing.session_risk_state_store import (
    DailyLossLimit,
    RiskLatch,
    SessionRiskState,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    SizedPosition,
    SizingInputs,
    VolatilityTargetedPositionSizer,
)

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 8, 13)
TEN_LAKH = Decimal("1000000")


def _sized(
    *, deployable: Decimal = TEN_LAKH, price: str = "1000", lot_size: int = 1
) -> SizedPosition:
    start = datetime(2026, 8, 3, 9, 15, tzinfo=IST)
    value = Decimal("1000")
    closes = []
    for index in range(60):
        value = value + Decimal(2) if index % 2 else value - Decimal(2)
        closes.append((start + timedelta(minutes=index), value))
    return VolatilityTargetedPositionSizer().size(
        SizingInputs(
            trading_symbol="RELIANCE",
            deployable_rupees=deployable,
            reference_price_rupees=Decimal(price),
            lot_size=lot_size,
            concurrent_position_capacity=6,
            horizon_bars=5,
            calibration=ReversionCapture(
                deviation_bucket=Decimal("2.0"),
                horizon_bars=5,
                event_count=5_000,
                mean_captured_bps=Decimal("40"),
                median_captured_bps=Decimal("20"),
                standard_error_bps=Decimal("8"),
                mean_captured_sigma=Decimal("0.30"),
                fitted_through=date(2026, 8, 12),
                maturity=CalibrationMaturity.POOLED_UNIVERSE,
            ),
            recent_closes=closes,
        )
    )


def _state(
    *,
    realised: Decimal = Decimal(0),
    exposure: Decimal = Decimal(0),
    orders: int = 0,
    latches: tuple[RiskLatch, ...] = (),
    peak: Decimal = TEN_LAKH,
) -> SessionRiskState:
    return SessionRiskState(
        session_date=SESSION,
        opening_equity_rupees=TEN_LAKH,
        peak_equity_rupees=peak,
        realised_pnl_rupees=realised,
        open_exposure_rupees=exposure,
        exposure_by_symbol=(("RELIANCE", exposure),) if exposure else (),
        orders_in_rate_window=orders,
        tripped_latches=latches,
    )


def _limits(**overrides: object) -> DerivedLimits:
    base = {
        "maximum_notional_rupees": TEN_LAKH,
        "maximum_leverage": Decimal(5),
        "price_collar_fraction": Decimal("0.05"),
        "maximum_orders_per_rate_window": 5,
    }
    base.update(overrides)
    return DerivedLimits(**base)  # type: ignore[arg-type]


_INACTIVE_LIMIT = DailyLossLimit(
    limit_rupees=None,
    sessions_observed=0,
    sigma_daily_rupees=None,
    z_quantile=Decimal("2.65"),
    unavailable_reason="no history",
)
_ACTIVE_LIMIT = DailyLossLimit(
    limit_rupees=Decimal("40000"),
    sessions_observed=40,
    sigma_daily_rupees=Decimal("15094"),
    z_quantile=Decimal("2.65"),
)
_CLEAR = RegulatoryFacts(
    trading_symbol="RELIANCE",
    is_fo_banned=False,
    mwpl_utilisation_fraction=Decimal("0.10"),
    mwpl_breach_threshold_fraction=Decimal("0.95"),
    lower_circuit_price_rupees=Decimal("900"),
    upper_circuit_price_rupees=Decimal("1100"),
)


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_clean_order_against_clear_facts_is_allowed() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert verdict.is_allowed is True
    assert verdict.refusals == ()
    assert "ALLOWED" in verdict.describe()


@pytest.mark.unit
def test_the_derived_limits_are_computed_from_evidence_not_chosen() -> None:
    """`R.03` — leverage is the reciprocal of the segment's own margin, not a chosen multiple."""
    limits = derive_limits(
        deployable_rupees=TEN_LAKH,
        traded_value_percentile_rupees=Decimal("500000"),
        realised_move_percentile_fraction=Decimal("0.03"),
        segment_margin_fraction=Decimal("0.20"),
        registration_threshold_orders_per_second=10,
    )
    assert limits.maximum_notional_rupees == Decimal("500000")
    assert limits.maximum_leverage == Decimal(5)
    assert limits.price_collar_fraction == Decimal("0.03")
    assert limits.maximum_orders_per_rate_window == 5


@pytest.mark.unit
def test_the_notional_cap_never_exceeds_the_capital_behind_it() -> None:
    limits = derive_limits(
        deployable_rupees=Decimal("200000"),
        traded_value_percentile_rupees=Decimal("9000000"),
        realised_move_percentile_fraction=Decimal("0.03"),
        segment_margin_fraction=Decimal("0.20"),
        registration_threshold_orders_per_second=10,
    )
    assert limits.maximum_notional_rupees == Decimal("200000")


@pytest.mark.unit
def test_the_rate_limit_sits_below_the_registration_threshold_with_visible_margin() -> None:
    """`A.101` decision 2 keeps this system registration-free by MEASUREMENT, not hope."""
    limits = derive_limits(
        deployable_rupees=TEN_LAKH,
        traded_value_percentile_rupees=TEN_LAKH,
        realised_move_percentile_fraction=Decimal("0.03"),
        segment_margin_fraction=Decimal("0.20"),
        registration_threshold_orders_per_second=10,
    )
    assert limits.maximum_orders_per_rate_window < 10


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_an_operator_override_that_widens_a_limit_is_refused_at_construction() -> None:
    """Tier 3 turns one way. A limit that can be loosened is not a limit."""
    with pytest.raises(RiskGateError) as failure:
        OperatorLimitOverrides(maximum_leverage=Decimal(20)).tighten(_limits())
    assert "only tightens" in str(failure.value)


@pytest.mark.adversarial
def test_an_operator_override_that_tightens_is_applied() -> None:
    tightened = OperatorLimitOverrides(
        maximum_notional_rupees=Decimal("250000"), maximum_orders_per_rate_window=2
    ).tighten(_limits())
    assert tightened.maximum_notional_rupees == Decimal("250000")
    assert tightened.maximum_orders_per_rate_window == 2
    assert tightened.maximum_leverage == Decimal(5)


@pytest.mark.adversarial
def test_a_banned_scrip_is_refused_and_no_lower_tier_can_release_it() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(),
        facts=RegulatoryFacts(
            trading_symbol="RELIANCE",
            is_fo_banned=True,
            mwpl_utilisation_fraction=Decimal("0.10"),
            mwpl_breach_threshold_fraction=Decimal("0.95"),
            lower_circuit_price_rupees=Decimal("900"),
            upper_circuit_price_rupees=Decimal("1100"),
        ),
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert verdict.is_allowed is False
    assert verdict.first_refusal is not None
    assert verdict.first_refusal.tier is GateTier.REGULATORY
    assert verdict.first_refusal.rule == "FO_BAN"


@pytest.mark.adversarial
def test_an_unread_ban_list_is_reported_as_unchecked_and_never_as_no_ban() -> None:
    """`None` and `False` are the same value only to a system that then trades on the difference."""
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(),
        facts=RegulatoryFacts(trading_symbol="RELIANCE"),
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert "fo_ban_list" in verdict.unchecked_regulatory_sources
    assert "mwpl_position_limits" in verdict.unchecked_regulatory_sources
    assert "circuit_bands" in verdict.unchecked_regulatory_sources
    assert "UNCHECKED" in verdict.describe()


@pytest.mark.adversarial
def test_a_tripped_latch_refuses_before_any_per_order_opinion_is_formed() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(latches=(RiskLatch.DAILY_LOSS,)),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert verdict.is_allowed is False
    assert verdict.first_refusal is not None
    assert verdict.first_refusal.tier is GateTier.SESSION_LATCH
    assert "R.22" in verdict.first_refusal.detail


@pytest.mark.adversarial
def test_every_breached_rule_is_reported_not_only_the_first() -> None:
    """ "Why was this refused" must not depend on evaluation order.

    Fixing only the rule that happened to be checked first leaves the operator surprised the second
    time, which is how a gate teaches people to distrust it.
    """
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(orders=99, exposure=Decimal("9000000")),
        facts=RegulatoryFacts(
            trading_symbol="RELIANCE",
            is_fo_banned=True,
            mwpl_utilisation_fraction=Decimal("0.99"),
            mwpl_breach_threshold_fraction=Decimal("0.95"),
            lower_circuit_price_rupees=Decimal("900"),
            upper_circuit_price_rupees=Decimal("1100"),
        ),
        limits=_limits(maximum_notional_rupees=Decimal("1")),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    rules = {refusal.rule for refusal in verdict.refusals}
    assert {"FO_BAN", "MWPL", "MAX_NOTIONAL", "MAX_LEVERAGE", "ORDER_RATE"} <= rules


@pytest.mark.adversarial
def test_a_limit_price_outside_the_circuit_band_is_refused() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
        limit_price_rupees=Decimal("1200"),
    )
    assert any(refusal.rule == "CIRCUIT_BAND" for refusal in verdict.refusals)


@pytest.mark.adversarial
def test_a_limit_price_beyond_the_derived_collar_is_refused() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(),
        facts=_CLEAR,
        limits=_limits(price_collar_fraction=Decimal("0.001")),
        daily_loss_limit=_INACTIVE_LIMIT,
        limit_price_rupees=Decimal("1050"),
    )
    assert any(refusal.rule == "PRICE_COLLAR" for refusal in verdict.refusals)


@pytest.mark.adversarial
def test_the_order_rate_refuses_rather_than_queueing() -> None:
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(orders=5),
        facts=_CLEAR,
        limits=_limits(maximum_orders_per_rate_window=5),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    refusal = next(item for item in verdict.refusals if item.rule == "ORDER_RATE")
    assert "different trade" in refusal.detail


@pytest.mark.adversarial
def test_a_breached_daily_loss_limit_refuses_even_before_the_latch_is_tripped() -> None:
    """The latch is tripped by the caller; the gate must not permit trades in the gap."""
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(realised=Decimal("-50000")),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_ACTIVE_LIMIT,
    )
    assert verdict.is_allowed is False
    assert any(refusal.rule == "DAILY_LOSS_BREACHED" for refusal in verdict.refusals)


@pytest.mark.adversarial
def test_an_inactive_daily_limit_never_refuses_on_a_limit_it_does_not_have() -> None:
    """`R.04` — an unavailable limit is not a limit of zero."""
    verdict = PreTradeRiskGate().evaluate(
        sized=_sized(),
        state=_state(realised=Decimal("-900000")),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert not any(refusal.rule == "DAILY_LOSS_BREACHED" for refusal in verdict.refusals)


@pytest.mark.adversarial
def test_the_sizers_zero_and_the_gates_refusal_are_the_same_answer() -> None:
    """`A.104` decision 1 — one decision with one output, not two that must be reconciled."""
    sized = _sized(deployable=Decimal("50000"), price="2500", lot_size=1000)
    verdict = PreTradeRiskGate().evaluate(
        sized=sized,
        state=_state(),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert sized.quantity == 0
    assert verdict.is_allowed is False
    assert any(refusal.tier is GateTier.SIZER for refusal in verdict.refusals)


@pytest.mark.adversarial
def test_the_drawdown_latch_is_earned_by_state_and_tripped_by_the_caller() -> None:
    """Evaluating a gate must never halt a session as a side effect.

    A read that halts is a read nobody can safely perform to find out where they stand.
    """
    earned = latches_to_trip(
        state=_state(realised=Decimal("-160000"), peak=TEN_LAKH),
        daily_loss_limit=_ACTIVE_LIMIT,
        maximum_drawdown_fraction=Decimal("0.10"),
    )
    assert RiskLatch.DRAWDOWN in earned
    assert RiskLatch.DAILY_LOSS in earned


@pytest.mark.adversarial
def test_the_leverage_check_counts_the_new_position_and_not_only_the_open_one() -> None:
    """`A.105` proved by mutation that dropping the new position from the leverage sum passed.

    Existing exposure alone sits inside the limit; adding this order crosses it. A leverage check
    that ignores the order it is checking is not a leverage check.
    """
    sized = _sized(price="1000")
    limits = _limits(maximum_leverage=Decimal("0.5"))
    just_inside = PreTradeRiskGate().evaluate(
        sized=sized,
        state=_state(exposure=Decimal("400000")),
        facts=_CLEAR,
        limits=limits,
        daily_loss_limit=_INACTIVE_LIMIT,
    )
    assert Decimal("400000") / sized.deployable_rupees <= limits.maximum_leverage
    assert (Decimal("400000") + sized.notional_rupees) / sized.deployable_rupees > (
        limits.maximum_leverage
    )
    assert any(refusal.rule == "MAX_LEVERAGE" for refusal in just_inside.refusals)


@pytest.mark.adversarial
def test_an_operator_override_of_zero_is_the_tightest_key_and_is_honoured() -> None:
    """`Decimal(0)` is FALSY, and an `or` silently discarded it (`A.105` finding 4).

    An operator turning the key all the way to "stop" received the full derived permission and no
    error at all.
    """
    with pytest.raises(RiskGateError):
        DerivedLimits(
            maximum_notional_rupees=Decimal(0),
            maximum_leverage=Decimal(5),
            price_collar_fraction=Decimal("0.05"),
            maximum_orders_per_rate_window=5,
        )
    tightened = OperatorLimitOverrides(maximum_orders_per_rate_window=0).tighten(_limits())
    assert tightened.maximum_orders_per_rate_window == 0


@pytest.mark.adversarial
def test_the_collar_measures_from_the_price_the_size_was_computed_at() -> None:
    """`A.105` critical 2: the reference price was recovered as `notional / quantity`.

    Because the notional was the BUDGET, that quotient was the true price inflated by the part lot
    that had been rounded away — Rs 1,942.50 recovered for a Rs 1,000 instrument on a lot of 22. It
    refused a limit price AT the market and allowed one at double it.
    """
    sized = _sized(price="1000", lot_size=22)
    assert sized.reference_price_rupees == Decimal("1000")
    at_market = PreTradeRiskGate().evaluate(
        sized=sized,
        state=_state(),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
        limit_price_rupees=Decimal("1000"),
    )
    far_out = PreTradeRiskGate().evaluate(
        sized=sized,
        state=_state(),
        facts=_CLEAR,
        limits=_limits(),
        daily_loss_limit=_INACTIVE_LIMIT,
        limit_price_rupees=Decimal("1900"),
    )
    assert not any(refusal.rule == "PRICE_COLLAR" for refusal in at_market.refusals)
    assert any(refusal.rule == "PRICE_COLLAR" for refusal in far_out.refusals)


@pytest.mark.adversarial
def test_a_float_limit_is_refused_at_construction() -> None:
    """The sizer and the store refuse floats; the gate was the hole (`A.105` finding 7)."""
    with pytest.raises(RiskGateError):
        DerivedLimits(
            maximum_notional_rupees=1000000.0,  # type: ignore[arg-type]
            maximum_leverage=Decimal(5),
            price_collar_fraction=Decimal("0.05"),
            maximum_orders_per_rate_window=5,
        )


@pytest.mark.adversarial
def test_an_impossible_margin_fraction_is_refused_rather_than_inverted() -> None:
    for fraction in (Decimal(0), Decimal("-0.2"), Decimal("1.5")):
        with pytest.raises(RiskGateError):
            derive_limits(
                deployable_rupees=TEN_LAKH,
                traded_value_percentile_rupees=TEN_LAKH,
                realised_move_percentile_fraction=Decimal("0.03"),
                segment_margin_fraction=fraction,
                registration_threshold_orders_per_second=10,
            )


@pytest.mark.adversarial
def test_limits_cannot_be_derived_against_no_capital() -> None:
    with pytest.raises(RiskGateError):
        derive_limits(
            deployable_rupees=Decimal(0),
            traded_value_percentile_rupees=TEN_LAKH,
            realised_move_percentile_fraction=Decimal("0.03"),
            segment_margin_fraction=Decimal("0.20"),
            registration_threshold_orders_per_second=10,
        )


@pytest.mark.adversarial
def test_the_segment_margin_table_is_empty_on_purpose() -> None:
    """A hardcoded margin table would be the `L1.09` lot-size defect one layer up."""
    from nse_algo_trader.sizing.pre_trade_risk_gate import segment_margin_fractions

    assert segment_margin_fractions() == {}


# --- the price collar, measured from the instrument's own history (`A.110`) ------------------


def _closes(prices: list[str]) -> list[tuple[datetime, Decimal]]:
    start = datetime(2026, 8, 11, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    return [
        (start + timedelta(minutes=5 * index), Decimal(price)) for index, price in enumerate(prices)
    ]


def test_the_collar_is_a_quantile_of_the_instruments_own_move() -> None:
    """Nearest-rank on the sample: the collar is a move this scrip actually made."""
    from nse_algo_trader.sizing.pre_trade_risk_gate import realised_move_quantile_from_closes

    # Two-bar moves off this tape: 1/100, 2/100, 2/101, 2/102 — the largest is 0.02, and the
    # collar is that observation rather than anything interpolated between two of them.
    closes = _closes(["100", "100", "101", "102", "103", "104"])
    collar = realised_move_quantile_from_closes(closes, horizon_bars=2, quantile=Decimal("0.95"))
    assert collar == Decimal("0.02")
    median = realised_move_quantile_from_closes(closes, horizon_bars=2, quantile=Decimal("0.5"))
    assert median < collar


def test_an_instrument_that_never_moved_sets_no_collar() -> None:
    """Refused rather than defaulted: a limit price cannot be checked against a range of zero."""
    from nse_algo_trader.sizing.pre_trade_risk_gate import (
        PriceCollarUnavailableError,
        realised_move_quantile_from_closes,
    )

    with pytest.raises(PriceCollarUnavailableError, match="did not move"):
        realised_move_quantile_from_closes(
            _closes(["100"] * 10), horizon_bars=2, quantile=Decimal("0.95")
        )


def test_too_few_closes_for_the_horizon_is_refused_not_shortened() -> None:
    from nse_algo_trader.sizing.pre_trade_risk_gate import (
        PriceCollarUnavailableError,
        realised_move_quantile_from_closes,
    )

    with pytest.raises(PriceCollarUnavailableError, match="cannot measure a move"):
        realised_move_quantile_from_closes(
            _closes(["100", "101"]), horizon_bars=5, quantile=Decimal("0.95")
        )


def test_a_negative_mean_capture_can_never_reach_the_collar_again() -> None:
    """The defect `A.110` records: a mean CAPTURE is not a move, and it can be negative."""
    from nse_algo_trader.sizing.pre_trade_risk_gate import realised_move_quantile_from_closes

    falling = _closes(["100", "99", "98", "97", "96", "95"])
    collar = realised_move_quantile_from_closes(falling, horizon_bars=2, quantile=Decimal("0.95"))
    assert collar > 0, "a collar is a DISTANCE; a falling instrument still has one"
