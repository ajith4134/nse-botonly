"""The dashboard surface — and the property that makes it worth having.

`R.08` demands status MEASURED from real code and server state, never hand-authored. The
tests that matter here are the ones that would fail if someone replaced a measurement
with a plausible-looking constant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nse_algo_trader.dashboard.dashboard_server import (
    SURFACED_MODULES,
    discover_engine_modules,
)
from nse_algo_trader.dashboard.regime_brain_read_model import (
    DEFAULT_MARKET_DATA_PATH,
    ClassifierPanel,
    RegimeBrainSnapshot,
    RegimeReadModelError,
    measure_regime_brain,
)
from nse_algo_trader.dashboard.regime_brain_surface_renderer import (
    REGIME_SLOT_ORDER,
    render_regime_brain_page,
)
from nse_algo_trader.regime.market_regime_state import (
    MarketRegime,
    RegimeDistribution,
)
from nse_algo_trader.regime.soft_regime_weighting_brain import RegimeBelief
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    MeanReversionAction,
    MeanReversionDecision,
)

REAL_DATA = DEFAULT_MARKET_DATA_PATH.exists()


def _snapshot() -> RegimeBrainSnapshot:
    now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    return RegimeBrainSnapshot(
        instrument_token=408065,
        bars_used=2289,
        measured_at=now,
        panels=(
            ClassifierPanel("trend_strength", True, True, 2289,
                            dict(
                                RegimeDistribution.from_scores(
                                    {
                                        MarketRegime.RANGING: 3.0,
                                        MarketRegime.TRENDING: 1.0,
                                        MarketRegime.VOLATILE: 1.0,
                                        MarketRegime.QUIET: 1.0,
                                    }
                                ).probabilities
                            ),
                            0.4, 1.0, "ADX=12.3"),
            ClassifierPanel("volatility", False, True, 2200,
                            MarketRegime.uniform_probabilities(), 0.25, 0.0, "vol=0.01"),
        ),
        belief=RegimeBelief(
            RegimeDistribution.from_scores(
                {MarketRegime.RANGING: 3.0, MarketRegime.TRENDING: 1.0,
                 MarketRegime.VOLATILE: 1.0, MarketRegime.QUIET: 1.0}),
            ("trend_strength",), 0.0, {"trend_strength": 1.0}, now,
        ),
        decision=MeanReversionDecision(
            MeanReversionAction.ABSTAIN, 0.0, "regime not actionable", -0.4, 1.9,
            MarketRegime.RANGING,
        ),
        deviation_band=1.9,
    )


# ------------------------------------------------------ the R.08 property


@pytest.mark.unit
def test_the_manifest_is_walked_from_the_real_tree_not_listed() -> None:
    """A hand-maintained inventory is correct the day it is written and wrong after.
    Adding a module must make it appear here with no edit to any list."""
    entries = discover_engine_modules()
    names = {entry.module_name for entry in entries}
    assert "nse_algo_trader.regime.soft_regime_weighting_brain" in names
    assert "nse_algo_trader.nse_ingest.bitemporal_ingest_store" in names
    assert len(entries) > len(SURFACED_MODULES)


@pytest.mark.unit
def test_unsurfaced_engines_are_reported_rather_than_hidden() -> None:
    """`L13.06`: a feature with no panel must FAIL the audit visibly. Reporting zero
    unsurfaced modules while most have no panel is the lie the rule exists to prevent."""
    entries = discover_engine_modules()
    unsurfaced = [entry for entry in entries if not entry.is_surfaced]
    assert unsurfaced, "every module claims a surface — that cannot be true"
    assert any("nse_ingest" in entry.module_name for entry in unsurfaced)


@pytest.mark.unit
def test_every_surfaced_module_actually_exists() -> None:
    """Guards the other direction: a declared surface for a deleted module would
    over-report coverage."""
    real = {entry.module_name for entry in discover_engine_modules()}
    assert real >= SURFACED_MODULES, SURFACED_MODULES - real


# --------------------------------------------------------------- rendering


@pytest.mark.unit
def test_the_page_renders_every_regime_with_a_visible_value_label() -> None:
    """The light-mode palette returned a contrast WARN, which obligates relief. Direct
    labels ARE that relief — without them the chart fails accessibility."""
    page = render_regime_brain_page(_snapshot())
    for regime in REGIME_SLOT_ORDER:
        assert regime.value in page
    assert page.count("bar-value") >= len(REGIME_SLOT_ORDER)


@pytest.mark.unit
def test_status_is_carried_by_a_word_not_only_a_colour() -> None:
    page = render_regime_brain_page(_snapshot())
    assert "armed" in page
    assert "unarmed" in page


@pytest.mark.unit
def test_dark_mode_is_selected_under_both_the_os_setting_and_the_toggle() -> None:
    page = render_regime_brain_page(_snapshot())
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page
    assert "#3987e5" in page, "dark palette must be its own steps, not an inverted light one"


@pytest.mark.unit
def test_a_table_view_exists_so_the_page_reads_without_colour() -> None:
    assert "<table" in render_regime_brain_page(_snapshot())


@pytest.mark.adversarial
def test_engine_evidence_is_escaped_into_the_page() -> None:
    """Evidence strings come from engines and land in HTML."""
    snapshot = _snapshot()
    hostile = ClassifierPanel(
        "x", True, True, 1, MarketRegime.uniform_probabilities(), 0.5, 1.0,
        "<script>alert(1)</script>",
    )
    page = render_regime_brain_page(
        RegimeBrainSnapshot(
            snapshot.instrument_token, snapshot.bars_used, snapshot.measured_at,
            (hostile,), snapshot.belief, snapshot.decision, snapshot.deviation_band,
        )
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ------------------------------------------------------------------ server


@pytest.mark.unit
def test_the_manifest_route_reports_real_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: None)
    client = TestClient(server_module.build_dashboard_app())
    payload = client.get("/manifest").json()
    assert payload["modules_total"] > 0
    assert payload["unsurfaced_count"] == len(payload["unsurfaced"])
    assert payload["surfaced"] + payload["unsurfaced_count"] == payload["modules_total"]


@pytest.mark.adversarial
def test_a_failed_measurement_returns_503_rather_than_a_pretty_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placeholder page showing green while measuring nothing is precisely the
    hand-authored status R.08 forbids."""
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: None)
    monkeypatch.setattr(
        server_module,
        "measure_regime_brain",
        lambda **_kwargs: (_ for _ in ()).throw(RegimeReadModelError("no data")),
    )
    response = TestClient(server_module.build_dashboard_app()).get("/regime")
    assert response.status_code == 503
    assert "Cannot measure" in response.text


@pytest.mark.adversarial
def test_measuring_against_a_missing_store_raises(tmp_path: Path) -> None:
    with pytest.raises(RegimeReadModelError, match="no market data"):
        measure_regime_brain(database_path=tmp_path / "absent.sqlite3")


@pytest.mark.real_data
@pytest.mark.skipif(not REAL_DATA, reason="retained market data not present")
def test_the_surface_measures_real_engines_over_real_bars() -> None:
    """`R.05`: the page's numbers must come from engines run on real bars."""
    snapshot = measure_regime_brain()
    assert snapshot.bars_used > 0
    assert len(snapshot.panels) >= 3
    assert snapshot.armed_count >= 1, "A.08: all built, one armed"
    unarmed = [panel for panel in snapshot.panels if not panel.is_armed]
    assert all(panel.weight_in_belief == 0.0 for panel in unarmed), (
        "an unarmed classifier must contribute zero weight"
    )
    assert "<html" in render_regime_brain_page(snapshot)


# ------------------------------- reflected XSS, found by security review and fixed


@pytest.mark.adversarial
def test_no_request_input_is_reflected_when_the_gate_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proven defect: with NO token file the gate is open by design, and the first
    version echoed the `key` parameter into an href and a meta-refresh — a raw script tag
    reached the body. Fixed by removing the reflection, so this asserts absence of the
    input rather than presence of escaping."""
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: None)
    client = TestClient(server_module.build_dashboard_app(), follow_redirects=False)
    payload = '"><script>alert(1)</script>'
    for path in ("/", "/wall"):
        response = client.get(path, params={"key": payload})
        assert "<script>alert(1)</script>" not in response.text
        assert "alert(1)" not in response.text, f"{path} still reflects request input"


@pytest.mark.adversarial
def test_the_401_page_echoes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: "realtoken")
    client = TestClient(server_module.build_dashboard_app())
    response = client.get("/wall", params={"key": "<script>alert(1)</script>"})
    assert response.status_code == 401
    assert "alert(1)" not in response.text


@pytest.mark.unit
def test_a_valid_key_is_traded_for_a_cookie_so_links_carry_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: "realtoken")
    client = TestClient(server_module.build_dashboard_app(), follow_redirects=False)
    response = client.get("/", params={"key": "realtoken"})
    assert response.status_code == 303
    assert response.headers["location"] == "/wall"
    cookie = response.headers.get("set-cookie", "")
    assert server_module.ACCESS_COOKIE_NAME in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


@pytest.mark.unit
def test_the_cookie_alone_authorises_later_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which is what lets every link drop its query string."""
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: "realtoken")
    client = TestClient(server_module.build_dashboard_app())
    client.cookies.set(server_module.ACCESS_COOKIE_NAME, "realtoken")
    assert client.get("/wall").status_code == 200
    client.cookies.set(server_module.ACCESS_COOKIE_NAME, "wrong")
    assert client.get("/wall").status_code == 401


@pytest.mark.unit
def test_the_wall_emits_no_key_bearing_links(monkeypatch: pytest.MonkeyPatch) -> None:
    import nse_algo_trader.dashboard.dashboard_server as server_module

    monkeypatch.setattr(server_module, "read_access_token", lambda: "realtoken")
    client = TestClient(server_module.build_dashboard_app())
    client.cookies.set(server_module.ACCESS_COOKIE_NAME, "realtoken")
    body = client.get("/wall").text
    assert "?key=" not in body, "a link still carries the token"
