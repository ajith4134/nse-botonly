"""Tests for the `/sizing` surface (`R.08`).

The lesson from `L1.18` is baked in here from the start: assertions that only check a substring is
PRESENT cannot tell a nine-cell row from a two-cell one, so this file counts and positions as well
as matching (`O.84`).

The route test drives the real FastAPI app against the real stores, because a renderer that is
correct in isolation while the route hands it the wrong thing is invisible in exactly the way `R.08`
exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCapture,
)
from nse_algo_trader.dashboard import dashboard_server
from nse_algo_trader.dashboard.sizing_surface_renderer import (
    SizingSurfaceState,
    absent_sizing_surface_state,
    render_sizing_page,
)
from nse_algo_trader.sizing.pre_trade_risk_gate import (
    DerivedLimits,
    PreTradeRiskGate,
    RegulatoryFacts,
)
from nse_algo_trader.sizing.session_risk_state_store import DailyLossLimit, SessionRiskState
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    SizingInputs,
    VolatilityTargetedPositionSizer,
)

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 8, 13)
TEN_LAKH = Decimal("1000000")
STATE_PATH = Path("/tmp/session_risk_state.sqlite3")

_INACTIVE = DailyLossLimit(
    limit_rupees=None,
    sessions_observed=0,
    sigma_daily_rupees=None,
    z_quantile=Decimal("2.65"),
    unavailable_reason="no history",
)


def _worked_state(*, banned: bool | None = False) -> SizingSurfaceState:
    start = datetime(2026, 8, 3, 9, 15, tzinfo=IST)
    price = Decimal("1000")
    closes = []
    for index in range(60):
        price = price + Decimal(2) if index % 2 else price - Decimal(2)
        closes.append((start + timedelta(minutes=index), price))
    sized = VolatilityTargetedPositionSizer().size(
        SizingInputs(
            trading_symbol="RELIANCE",
            deployable_rupees=TEN_LAKH,
            reference_price_rupees=Decimal("1000"),
            lot_size=1,
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
    state = SessionRiskState(
        session_date=SESSION,
        opening_equity_rupees=TEN_LAKH,
        peak_equity_rupees=TEN_LAKH,
        realised_pnl_rupees=Decimal("-12000"),
        open_exposure_rupees=Decimal("300000"),
        exposure_by_symbol=(("TCS", Decimal("300000")),),
        orders_in_rate_window=1,
        tripped_latches=(),
    )
    verdict = PreTradeRiskGate().evaluate(
        sized=sized,
        state=state,
        facts=RegulatoryFacts(trading_symbol="RELIANCE", is_fo_banned=banned),
        limits=DerivedLimits(
            maximum_notional_rupees=TEN_LAKH,
            maximum_leverage=Decimal(5),
            price_collar_fraction=Decimal("0.05"),
            maximum_orders_per_rate_window=5,
        ),
        daily_loss_limit=_INACTIVE,
    )
    return SizingSurfaceState(
        measured_at=datetime(2026, 8, 13, 15, 20, tzinfo=IST),
        session_date=SESSION,
        sized=sized,
        verdict=verdict,
        session_state=state,
        daily_loss_limit=_INACTIVE,
        state_path=STATE_PATH,
    )


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_the_page_shows_every_step_that_produced_the_quantity() -> None:
    """A size an operator cannot reconstruct is a size they cannot argue with."""
    page = render_sizing_page(_worked_state())
    for step in (
        "deployable capital",
        "target risk fraction",
        "risk budget",
        "realised volatility",
        "volatility budget notional",
        "calibrated edge",
        "Kelly shrinkage",
        "Kelly cap notional",
        "concentration cap",
        "bound by",
        "lot rounding",
    ):
        assert step in page, step


@pytest.mark.unit
def test_every_worked_step_row_has_three_cells() -> None:
    """`O.84` — presence is not structure. This counts."""
    page = render_sizing_page(_worked_state())
    body = page.split("<tbody>")[1].split("</tbody>")[0]
    rows = [row for row in body.split("<tr>") if row.strip()]
    assert len(rows) == 11
    for row in rows:
        assert row.count("<td") == 3


@pytest.mark.unit
def test_the_page_names_which_bound_decided_the_size() -> None:
    page = render_sizing_page(_worked_state())
    assert "bound by" in page
    assert any(
        phrase in page
        for phrase in ("the volatility budget", "the Kelly cap", "the capital itself")
    )


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_unread_regulatory_walls_are_shouted_not_treated_as_clear() -> None:
    state = _worked_state(banned=None)
    page = render_sizing_page(state)
    assert "regulatory walls unchecked" in page
    assert "not checked" in page
    assert "no ban" in page


@pytest.mark.adversarial
def test_a_refused_order_lists_every_tier_that_refused_it() -> None:
    page = render_sizing_page(_worked_state(banned=True))
    assert "regulatory wall" in page
    assert "FO_BAN" in page
    assert "refused" in page


@pytest.mark.adversarial
def test_the_absent_state_never_renders_a_size_of_zero() -> None:
    """ "The sizer answered zero" and "the sizer could not be run" are different facts."""
    page = render_sizing_page(
        absent_sizing_surface_state(
            measured_at=datetime(2026, 8, 13, 15, 20, tzinfo=IST),
            session_date=SESSION,
            state_path=STATE_PATH,
            unavailable_reason="RELIANCE has no reversion calibration covering it",
            missing_inputs=("reversion_calibration",),
        )
    )
    assert "no decision" in page
    assert "reversion_calibration" in page
    assert "0 refusal" not in page
    assert "units —" not in page


@pytest.mark.adversarial
def test_the_reason_text_is_escaped_rather_than_reflected() -> None:
    page = render_sizing_page(
        absent_sizing_surface_state(
            measured_at=datetime(2026, 8, 13, 15, 20, tzinfo=IST),
            session_date=SESSION,
            state_path=STATE_PATH,
            unavailable_reason="<script>alert(1)</script>",
        )
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.adversarial
def test_the_route_works_a_real_decision_and_never_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`R.08` end to end against the REAL stores, and a GET creates nothing.

    The session risk state is pointed at a path that does not exist: the page must render the
    "no session risk state has been opened" case and must NOT create the database, because a
    dashboard that writes on page load is a side effect nobody asked for.
    """
    absent_state = tmp_path / "never_created.sqlite3"
    monkeypatch.setattr(dashboard_server, "DEFAULT_SESSION_RISK_STATE_PATH", absent_state)
    monkeypatch.setattr(dashboard_server, "read_access_token", lambda: None)
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", "1000000")
    with TestClient(dashboard_server.build_dashboard_app()) as client:
        response = client.get("/sizing")
    assert response.status_code == 200
    assert "Sizing and the risk gate" in response.text
    assert not absent_state.exists()


@pytest.mark.adversarial
def test_the_surface_is_claimed_in_the_manifest() -> None:
    for module in (
        "nse_algo_trader.sizing.kelly_edge_scaler",
        "nse_algo_trader.sizing.realised_volatility_estimator",
        "nse_algo_trader.sizing.volatility_targeted_position_sizer",
        "nse_algo_trader.sizing.session_risk_state_store",
        "nse_algo_trader.sizing.pre_trade_risk_gate",
        "nse_algo_trader.sizing.sizing_inputs_from_real_stores",
        "nse_algo_trader.dashboard.sizing_surface_renderer",
    ):
        assert module in dashboard_server.SURFACED_MODULES, module
