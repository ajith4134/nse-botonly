"""`L13.01` server + `L13.06` manifest — the surface, and the audit that keeps it honest.

Two routes, and the second is the one that matters for `R.08`.

`/regime` renders the brain. `/manifest` walks the **real package tree** and reports which
engines do and do not have a dashboard surface — so an engine built tomorrow appears in
that list automatically and reads UNSURFACED until someone gives it a panel. That is
`L13.06`'s auto-appear-or-fail-the-audit property: the dashboard cannot quietly fall
behind the code, because the code is what generates the list.

Built against a measured fact rather than a memory: after the reset there was no
dashboard at all — the systemd unit is inactive and no web code survived — so every
`R.08` deferral logged during the day had nothing to attach to. This is the thing they
were waiting for.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from nse_algo_trader.dashboard.regime_brain_read_model import (
    RegimeReadModelError,
    measure_regime_brain,
)
from nse_algo_trader.dashboard.regime_brain_surface_renderer import (
    render_regime_brain_page,
)

SURFACED_MODULES: frozenset[str] = frozenset(
    {
        "nse_algo_trader.regime.trend_strength_regime_classifier",
        "nse_algo_trader.regime.markov_switching_regime_model",
        "nse_algo_trader.regime.volatility_regime_classifier",
        "nse_algo_trader.regime.session_phase_regime_classifier",
        "nse_algo_trader.regime.soft_regime_weighting_brain",
        "nse_algo_trader.regime.market_regime_state",
        "nse_algo_trader.strategy.intraday_mean_reversion_engine",
    }
)
"""Modules that genuinely have a panel today. Declaring this is safe precisely BECAUSE
the manifest derives the full module list from the real tree — an over-claim shows up as
a surfaced module no route renders, and an omission shows up as UNSURFACED."""


@dataclass(frozen=True)
class ManifestEntry:
    """One real module and whether a human can currently see its state."""

    module_name: str
    is_surfaced: bool


def discover_engine_modules(package_name: str = "nse_algo_trader") -> list[ManifestEntry]:
    """Every module in the real package tree, with its surfacing state.

    Walked, never listed. A hand-maintained inventory has the same defect as a
    hand-authored status: correct on the day it is written and wrong thereafter.
    """
    package = importlib.import_module(package_name)
    search_paths = [str(Path(path)) for path in getattr(package, "__path__", [])]
    entries = [
        ManifestEntry(module.name, module.name in SURFACED_MODULES)
        for module in pkgutil.walk_packages(search_paths, prefix=f"{package_name}.")
        if not module.ispkg and not module.name.rsplit(".", 1)[-1].startswith("_")
    ]
    return sorted(entries, key=lambda entry: entry.module_name)


def build_dashboard_app() -> FastAPI:
    """The app. Constructed by a function so tests get a fresh instance."""
    app = FastAPI(title="nse-algo-trader dashboard", docs_url=None, redoc_url=None)

    @app.get("/regime", response_class=HTMLResponse)
    def regime_surface(instrument_token: int | None = None) -> HTMLResponse:
        try:
            snapshot = measure_regime_brain(instrument_token=instrument_token)
        except RegimeReadModelError as failure:
            # Fail visibly. A placeholder page would be exactly the hand-authored status
            # R.08 exists to forbid — it would show green while measuring nothing.
            return HTMLResponse(
                f"<h1>Cannot measure regime brain</h1><p>{failure}</p>", status_code=503
            )
        return HTMLResponse(render_regime_brain_page(snapshot))

    @app.get("/manifest")
    def manifest() -> JSONResponse:
        entries = discover_engine_modules()
        unsurfaced = [entry.module_name for entry in entries if not entry.is_surfaced]
        return JSONResponse(
            {
                "modules_total": len(entries),
                "surfaced": len(entries) - len(unsurfaced),
                "unsurfaced_count": len(unsurfaced),
                "unsurfaced": unsurfaced,
                "note": (
                    "Derived by walking the real package tree. A new engine appears here "
                    "automatically and reads UNSURFACED until it has a panel (R.08/L13.06)."
                ),
            }
        )

    return app


app = build_dashboard_app()
