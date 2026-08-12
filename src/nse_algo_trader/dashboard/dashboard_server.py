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
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockOffsetObservationStore,
)
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import read_chrony_tracking
from nse_algo_trader.clock_integrity.timestamp_trust_budget import TimestampTrustBudget
from nse_algo_trader.dashboard.clock_integrity_surface_renderer import (
    ClockIntegritySurfaceState,
    render_clock_integrity_page,
)
from nse_algo_trader.dashboard.market_rule_coverage_surface_renderer import (
    render_market_rule_coverage_page,
)
from nse_algo_trader.dashboard.module_surface_catalogue import build_module_catalogue
from nse_algo_trader.dashboard.operations_wall_renderer import render_operations_wall
from nse_algo_trader.dashboard.order_book_replay_surface_renderer import (
    render_order_book_replay_page,
)
from nse_algo_trader.dashboard.regime_brain_read_model import (
    RegimeReadModelError,
    measure_regime_brain,
)
from nse_algo_trader.dashboard.regime_brain_surface_renderer import (
    render_regime_brain_page,
)
from nse_algo_trader.market_depth.market_depth_tape_store import (
    DepthTapeStoreError,
    MarketDepthTapeReader,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    InstrumentCoverageReport,
    OrderBookReplayError,
    OrderBookSnapshotReplayEngine,
)
from nse_algo_trader.market_rules.nse_market_rule_history import (
    seeded_nse_market_rule_store,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
"""Where the recorder writes. Outside the repository, like every other data root here —
the tape is hundreds of megabytes a session and has no business in a git tree."""

ACCESS_TOKEN_PATH = Path("~/.nse_algo_trader/dashboard_access_token.txt").expanduser()
"""The dashboard binds to a PUBLIC interface, so it is token-gated. The token lives in a
gitignored file outside the repo and is never committed (`R.02`). This is a read-only
surface over non-secret state, so the token is a gate against casual discovery rather
than a security boundary — it is stated plainly so nobody mistakes it for one."""


def read_access_token() -> str | None:
    """The configured token, or None when no file exists (then the gate is open)."""
    if not ACCESS_TOKEN_PATH.exists():
        return None
    token = ACCESS_TOKEN_PATH.read_text().strip()
    return token or None


ACCESS_COOKIE_NAME = "nse_dashboard_key"
"""The token is carried in a cookie AFTER the first authorised request, so it never
appears in a link again.

This exists because of a real defect, not a preference. The first version threaded the
key through every link as `?key=...`, reflecting an attacker-controlled string into
`href` and `<meta refresh>` attributes. With a token file present auth happened to block
it — but the no-token path is a configuration this server deliberately supports, and
there a raw `<script>` tag reached the response body. Proven, then fixed by removing the
reflection entirely rather than escaping it in two places and hoping a third is never
added."""


def _is_authorised(request: Request) -> bool:
    """Cookie first, then the query parameter that sets it."""
    expected = read_access_token()
    if expected is None:
        return True
    import hmac

    # Constant-time both ways: a length-or-prefix leak on a token is free to avoid.
    return any(
        supplied and hmac.compare_digest(supplied, expected)
        for supplied in (
            request.cookies.get(ACCESS_COOKIE_NAME, ""),
            request.query_params.get("key", ""),
        )
    )


def _unauthorised_html() -> HTMLResponse:
    """A fixed string. Nothing from the request is echoed back, ever."""
    return HTMLResponse("<h1>401</h1><p>access key required</p>", status_code=401)


def _remember_key(response: HTMLResponse | RedirectResponse, request: Request) -> None:
    """Store a VALIDATED key so later links need no query string.

    Only ever called after `_is_authorised`, so the value written is the configured token
    and not attacker input. `httponly` keeps it away from scripts and `SameSite=Lax`
    blunts cross-site use. It is deliberately NOT marked Secure — the server speaks plain
    HTTP, so a Secure cookie would simply never be sent. That is a real limitation of
    running without TLS, recorded rather than papered over.
    """
    supplied = request.query_params.get("key")
    if supplied:
        response.set_cookie(ACCESS_COOKIE_NAME, supplied, httponly=True, samesite="lax", path="/")


SURFACED_MODULES: frozenset[str] = frozenset(
    {
        "nse_algo_trader.regime.trend_strength_regime_classifier",
        "nse_algo_trader.regime.markov_switching_regime_model",
        "nse_algo_trader.regime.volatility_regime_classifier",
        "nse_algo_trader.regime.session_phase_regime_classifier",
        "nse_algo_trader.regime.soft_regime_weighting_brain",
        "nse_algo_trader.regime.market_regime_state",
        "nse_algo_trader.strategy.intraday_mean_reversion_engine",
        "nse_algo_trader.market_depth.order_book_snapshot_replay_engine",
        "nse_algo_trader.dashboard.order_book_replay_surface_renderer",
        "nse_algo_trader.market_rules.point_in_time_market_rule_store",
        "nse_algo_trader.market_rules.nse_market_rule_history",
        "nse_algo_trader.dashboard.market_rule_coverage_surface_renderer",
        "nse_algo_trader.clock_integrity.exchange_clock_offset_estimator",
        "nse_algo_trader.clock_integrity.exchange_feed_delay_observation",
        "nse_algo_trader.clock_integrity.depth_tape_delay_sampler",
        "nse_algo_trader.clock_integrity.reference_clock_ntp_sampler",
        "nse_algo_trader.clock_integrity.clock_offset_observation_store",
        "nse_algo_trader.clock_integrity.clock_drift_change_detector",
        "nse_algo_trader.clock_integrity.timestamp_trust_budget",
        "nse_algo_trader.clock_integrity.clock_integrity_session_runner",
        "nse_algo_trader.dashboard.clock_integrity_surface_renderer",
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


def _measure_latest_depth_session(
    instrument_limit: int, staleness_quantile: float
) -> InstrumentCoverageReport:
    """Replay the most recent recorded session and report what it could say.

    The latest session rather than a configured date: the recorder writes forward and a
    dashboard pinned to a date silently goes stale, which is the same failure as a
    hand-authored status.
    """
    reader = MarketDepthTapeReader(DEPTH_TAPE_ROOT)
    sessions = reader.session_dates()
    if not sessions:
        raise OrderBookReplayError(f"no depth tape sessions under {DEPTH_TAPE_ROOT}")
    engine = OrderBookSnapshotReplayEngine(
        reader, session_date=max(sessions), staleness_quantile=staleness_quantile
    )
    return engine.coverage_report(instrument_limit=instrument_limit)


def build_dashboard_app() -> FastAPI:
    """The app. Constructed by a function so tests get a fresh instance."""
    app = FastAPI(title="nse-algo-trader dashboard", docs_url=None, redoc_url=None)

    @app.get("/", response_model=None)
    def index(request: Request) -> HTMLResponse | RedirectResponse:
        """Trade the key for a cookie, then send the browser to a CLEAN url.

        Nothing from the request is interpolated into the response, which is what makes
        reflected XSS impossible here rather than merely escaped.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        redirect = RedirectResponse("/wall", status_code=303)
        _remember_key(redirect, request)
        return redirect

    @app.get("/wall", response_class=HTMLResponse)
    def operations_wall(request: Request) -> HTMLResponse:
        """`L13.06` — every module, measured. New engines appear here with no edit."""
        if not _is_authorised(request):
            return _unauthorised_html()
        summary = build_module_catalogue(REPOSITORY_ROOT, SURFACED_MODULES)
        response = HTMLResponse(render_operations_wall(summary))
        _remember_key(response, request)
        return response

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        """Unauthenticated liveness only — reports nothing about the system."""
        return PlainTextResponse("ok")

    @app.get("/regime", response_class=HTMLResponse)
    def regime_surface(request: Request, instrument_token: int | None = None) -> HTMLResponse:
        if not _is_authorised(request):
            return _unauthorised_html()
        try:
            snapshot = measure_regime_brain(instrument_token=instrument_token)
        except RegimeReadModelError as failure:
            # Fail visibly. A placeholder page would be exactly the hand-authored status
            # R.08 exists to forbid — it would show green while measuring nothing.
            return HTMLResponse(
                f"<h1>Cannot measure regime brain</h1><p>{failure}</p>", status_code=503
            )
        response = HTMLResponse(render_regime_brain_page(snapshot))
        _remember_key(response, request)
        return response

    @app.get("/microstructure", response_class=HTMLResponse)
    def order_book_replay_surface(
        request: Request, instrument_limit: int = 25, staleness_quantile: float = 0.99
    ) -> HTMLResponse:
        """`L0.22`'s surface, measured by replaying the real tape on request.

        Bounded by `instrument_limit` because replaying 9,000 instruments per page load
        would make this a batch job rather than a page. The bound is a query parameter and
        is printed on the page, so the sample is visible rather than implied away.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        try:
            report = _measure_latest_depth_session(instrument_limit, staleness_quantile)
        except (OrderBookReplayError, DepthTapeStoreError) as failure:
            # Same discipline as /regime: fail visibly rather than render a page that
            # measures nothing and looks identical to one that measured everything.
            return HTMLResponse(
                f"<h1>Cannot replay the depth tape</h1><p>{failure}</p>", status_code=503
            )
        response = HTMLResponse(render_order_book_replay_page(report))
        _remember_key(response, request)
        return response

    @app.get("/rules", response_class=HTMLResponse)
    def market_rule_coverage_surface(request: Request) -> HTMLResponse:
        """`L0.31`'s surface: which eras this project can price and which it refuses.

        Measured from the seeded store itself, so a family compiled tomorrow appears here
        with no edit, and one whose facts are removed turns red by itself.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        response = HTMLResponse(
            render_market_rule_coverage_page(seeded_nse_market_rule_store().coverage())
        )
        _remember_key(response, request)
        return response

    @app.get("/clock", response_class=HTMLResponse)
    def clock_integrity_surface(request: Request) -> HTMLResponse:
        """`L0.32`'s surface: the timestamp error budget, read off the recorded history.

        The page renders whatever the store holds — no session is fitted on request. A
        clock fit takes seconds of parquet reading, and a dashboard that silently refits on
        every page load would report a different number to two readers refreshing at once.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        store = ClockOffsetObservationStore()
        latest_fit = store.latest_fit()
        consensus = store.latest_reference_consensus()
        assessment = TimestampTrustBudget(store).assess(
            fit=latest_fit.fit if latest_fit else None,
            consensus=consensus,
            alerts=store.alerts(session_date=latest_fit.session_date) if latest_fit else (),
            at=datetime.now(UTC),
        )
        response = HTMLResponse(
            render_clock_integrity_page(
                ClockIntegritySurfaceState(
                    assessment=assessment,
                    fits=store.fits(),
                    alerts=store.alerts(),
                    consensus=consensus,
                    chrony=read_chrony_tracking(),
                )
            )
        )
        _remember_key(response, request)
        return response

    @app.get("/manifest")
    def manifest(request: Request) -> JSONResponse:
        if not _is_authorised(request):
            return JSONResponse({"error": "access key required"}, status_code=401)
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
