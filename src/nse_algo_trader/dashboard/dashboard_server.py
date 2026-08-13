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
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from nse_algo_trader.capital_configuration import (
    CapitalConfigurationError,
    load_trading_capital_from_environment,
)
from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockOffsetObservationStore,
)
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import read_chrony_tracking
from nse_algo_trader.clock_integrity.timestamp_trust_budget import TimestampTrustBudget
from nse_algo_trader.consolidated_feed.broker_reliability_store import (
    BrokerReliabilityStore,
)
from nse_algo_trader.consolidated_feed.consolidated_feed_session_runner import (
    ConsolidatedFeedSessionRunner,
)
from nse_algo_trader.cost_gate.gate_decision_log import GateDecisionLog
from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationCoverageError,
    ReversionCalibrationStore,
)
from nse_algo_trader.cost_gate.per_segment_edge_floor import (
    EdgeFloorError,
    SegmentEdgeFloorStore,
)
from nse_algo_trader.cost_gate.reversion_calibration_fitter import (
    DEFAULT_CALIBRATION_HORIZONS,
)
from nse_algo_trader.dashboard.clock_integrity_surface_renderer import (
    ClockIntegritySurfaceState,
    render_clock_integrity_page,
)
from nse_algo_trader.dashboard.consolidated_feed_surface_renderer import (
    ConsolidatedFeedSurfaceState,
    render_consolidated_feed_page,
)
from nse_algo_trader.dashboard.deep_history_surface_renderer import (
    DeepHistoryMarketCoverage,
    DeepHistorySurfaceState,
    render_deep_history_page,
)
from nse_algo_trader.dashboard.market_rule_coverage_surface_renderer import (
    render_market_rule_coverage_page,
)
from nse_algo_trader.dashboard.module_surface_catalogue import build_module_catalogue
from nse_algo_trader.dashboard.operations_wall_renderer import render_operations_wall
from nse_algo_trader.dashboard.order_book_replay_surface_renderer import (
    render_order_book_replay_page,
)
from nse_algo_trader.dashboard.order_path_surface_renderer import (
    build_order_path_surface_state,
    empty_order_path_surface_state,
    render_order_path_page,
)
from nse_algo_trader.dashboard.paper_capital_surface_renderer import (
    PaperCapitalSurfaceState,
    absent_paper_capital_surface_state,
    build_paper_capital_surface_state,
    render_paper_capital_page,
    with_refusal,
)
from nse_algo_trader.dashboard.regime_brain_read_model import (
    RegimeReadModelError,
    measure_regime_brain,
)
from nse_algo_trader.dashboard.regime_brain_surface_renderer import (
    render_regime_brain_page,
)
from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
    SegmentPricingAssumption,
    build_transaction_cost_surface_state,
    render_transaction_cost_page,
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
from nse_algo_trader.order_path.kite_order_execution_venue import (
    exchange_algo_identifier_from_environment,
)
from nse_algo_trader.order_path.order_intent_journal import (
    DEFAULT_JOURNAL_PATH,
    OrderIntentJournal,
)
from nse_algo_trader.paper_capital_ledger import (
    DEFAULT_PAPER_CAPITAL_LEDGER_PATH,
    PaperCapitalError,
    PaperCapitalLedger,
)
from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
    ChargeReconciliationLedger,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

MARKET_DATA_DATABASE = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

DEPTH_TAPE_BROKER = "kite"
"""Which broker's quotes the depth tape is recorded from — a deployment fact, and the one
that decides whose `L0.33` verdict gates the microstructure replay."""

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


IST = ZoneInfo("Asia/Kolkata")
"""The exchange's own timezone. A cost page dated by UTC would show yesterday's session
after 18:30 IST, which is exactly when the operator is looking at it."""

TRANSACTION_COST_DISPLAY_ASSUMPTIONS: tuple[SegmentPricingAssumption, ...] = (
    SegmentPricingAssumption(
        segment=ChargeableSegment.EQUITY_INTRADAY,
        price_paise=Decimal(140_000),
        lot_size=100,
        representative_lots=10,
        maximum_lots=40,
    ),
    SegmentPricingAssumption(
        segment=ChargeableSegment.EQUITY_DELIVERY,
        price_paise=Decimal(140_000),
        lot_size=100,
        representative_lots=10,
        maximum_lots=40,
    ),
    SegmentPricingAssumption(
        segment=ChargeableSegment.EQUITY_OPTIONS,
        price_paise=Decimal(15_000),
        lot_size=75,
        representative_lots=2,
        maximum_lots=40,
        strike_paise=Decimal(2_400_000),
    ),
    SegmentPricingAssumption(
        segment=ChargeableSegment.EQUITY_FUTURES,
        price_paise=Decimal(2_400_000),
        lot_size=75,
        representative_lots=1,
        maximum_lots=20,
    ),
)
"""What each segment is DISPLAYED at. Assumptions, and labelled as such on the page.

Cash uses a 100-share block as its ladder step rather than a single share. The staircase is
computed one priced round trip per tread, so a ladder of 20,000 single shares is 20,000 pricing
calls and an SVG with 20,000 points — measured at 10.5 MB of HTML, which is not a page.

Four of the eight segments, because these are the ones being traded first; currency and
commodity price correctly and are simply not shown until a holon trades them."""

TRANSACTION_COST_DISPLAY_CEILING_BPS = Decimal(50)
"""The cost ceiling the minimum-viable-quantity column solves against.

A DISPLAY choice, not a policy: `L1.04` derives the real per-segment floor from data. This
only decides what the column asks, and it is here rather than in the engine so nobody mistakes
it for a measurement."""

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
        "nse_algo_trader.consolidated_feed.cross_broker_quote_tape",
        "nse_algo_trader.consolidated_feed.broker_quote_pollers",
        "nse_algo_trader.consolidated_feed.broker_reliability_store",
        "nse_algo_trader.consolidated_feed.consolidated_feed_engine",
        "nse_algo_trader.consolidated_feed.consolidated_feed_session_runner",
        "nse_algo_trader.dashboard.consolidated_feed_surface_renderer",
        "nse_algo_trader.deep_history.bhavcopy_variant_resolver",
        "nse_algo_trader.deep_history.deep_history_bhavcopy_reader",
        "nse_algo_trader.deep_history.deep_history_archive_loader",
        "nse_algo_trader.dashboard.deep_history_surface_renderer",
        "nse_algo_trader.transaction_cost.chargeable_market_segments",
        "nse_algo_trader.transaction_cost.charge_structure_history",
        "nse_algo_trader.transaction_cost.broker_fee_schedules",
        "nse_algo_trader.transaction_cost.nse_transaction_cost_engine",
        "nse_algo_trader.transaction_cost.breakeven_move_solver",
        "nse_algo_trader.transaction_cost.quantity_cost_economics",
        "nse_algo_trader.transaction_cost.charge_reconciliation_ledger",
        "nse_algo_trader.dashboard.transaction_cost_surface_renderer",
        # `F02` — everything `/orders` actually draws. The placer, the limiter, the latch and the
        # watchdog are deliberately NOT claimed here: they have no panel yet, and the manifest is
        # only worth reading if an over-claim is impossible.
        "nse_algo_trader.order_path.order_intent_journal",
        "nse_algo_trader.order_path.order_record",
        "nse_algo_trader.order_path.order_lifecycle_state_machine",
        "nse_algo_trader.order_path.broker_truth_reconciler",
        "nse_algo_trader.dashboard.order_path_surface_renderer",
        # `L1.18` — the paper trading book's virtual money, drawn in full by `/paper-capital`.
        "nse_algo_trader.paper_capital_ledger",
        "nse_algo_trader.dashboard.paper_capital_surface_renderer",
        "nse_algo_trader.dashboard.dashboard_service_entrypoint",
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
    session_date = max(sessions)
    engine = OrderBookSnapshotReplayEngine(
        reader,
        session_date=session_date,
        staleness_quantile=staleness_quantile,
        inadmissible_instruments=inadmissible_depth_instruments(session_date),
    )
    return engine.coverage_report(instrument_limit=instrument_limit)


def inadmissible_depth_instruments(session_date: date) -> frozenset[int]:
    """Instrument tokens `L0.33` measured the DEPTH BROKER as unreliable on, that session.

    This is where the consolidated feed's verdict becomes a behaviour change: the depth tape
    is recorded from Kite, so Kite's own admissibility decides which instruments the
    microstructure replay may build features from. The mapping from the feed's trading
    symbols to the tape's instrument tokens comes from the stored instrument master — the
    same table the recorder subscribed by — so a symbol the master does not know is simply
    not gated rather than silently dropped.
    """
    admissibility = ConsolidatedFeedSessionRunner().engine.admissibility(session_date)
    unreliable_symbols = {
        symbol
        for (broker, symbol), admissible in admissibility.items()
        if broker == DEPTH_TAPE_BROKER and not admissible
    }
    if not unreliable_symbols:
        return frozenset()
    with sqlite3.connect(f"file:{MARKET_DATA_DATABASE}?mode=ro", uri=True) as connection:
        placeholders = ",".join("?" for _ in unreliable_symbols)
        rows = connection.execute(
            f"SELECT DISTINCT instrument_token FROM instrument_master "  # noqa: S608 - the
            # placeholders are generated from the set's LENGTH; every value is bound
            f"WHERE tradingsymbol IN ({placeholders}) AND segment = 'NSE'",
            tuple(unreliable_symbols),
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


def build_dashboard_app() -> FastAPI:
    """The app. Constructed by a function so tests get a fresh instance.

    This deliberately does NOT read the project `.env`. The server needs it — without it
    `NSE_TRADING_CAPITAL_RUPEES` sat correctly in the file and every surface reported it unset — but
    loading it here injected roughly fifty real credentials into any process that built an app,
    pytest included, where `monkeypatch.setenv` cannot undo them. `dashboard_service_entrypoint` is
    the module systemd runs and the one place that load happens.
    """
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

    @app.get("/costs", response_class=HTMLResponse)
    def transaction_cost_surface(request: Request) -> HTMLResponse:
        """`L1.01`'s surface: what trading each segment costs, and what it refuses to price.

        The pricing assumptions are declared HERE rather than inside the renderer, because a
        representative premium is a display choice and freezing one in the engine's own module
        would turn it into a policy every reader would mistake for a measurement.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        rule_store = seeded_nse_market_rule_store()
        # Read the floors and the logged verdicts the daily runner produced. Both are stored
        # rather than recomputed on request: deriving a floor walks hundreds of real books, and
        # a page that silently refits on every load would report a different number to two
        # readers refreshing at once.
        floor_store = SegmentEdgeFloorStore()
        edge_floors = []
        for segment in ChargeableSegment:
            try:
                edge_floors.append(floor_store.floor_for(segment))
            except EdgeFloorError:
                continue
        decision_log = GateDecisionLog()
        latest = decision_log.latest_session()
        logged_decisions = decision_log.decisions_for(latest) if latest else ()
        # The measured edge, read from the nightly fit for the same reason as the floors: the
        # walk that produces it is 150,000 events long, and refitting per page load would show
        # two readers different numbers. An uncalibrated cell is simply absent from the table
        # rather than defaulted, so the page can never imply an edge nobody measured.
        calibration_store = ReversionCalibrationStore()
        today = datetime.now(IST).date()
        calibrations = []
        for horizon in DEFAULT_CALIBRATION_HORIZONS:
            for bucket in calibration_store.calibrated_buckets(
                horizon_bars=horizon, as_of=today
            ):
                try:
                    calibrations.append(
                        calibration_store.capture_for(
                            deviation_sigma=bucket, horizon_bars=horizon, as_of=today
                        )
                    )
                except CalibrationCoverageError:
                    # Below the evidence threshold. The row exists and is accumulating; it is
                    # not yet something a reader should price from, so it is not shown as one.
                    continue
        state = build_transaction_cost_surface_state(
            NseTransactionCostEngine(rule_store),
            ChargeReconciliationLedger(),
            TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
            priced_on=datetime.now(IST).date(),
            cost_bps_ceiling=TRANSACTION_COST_DISPLAY_CEILING_BPS,
            rule_store=rule_store,
            edge_floors=edge_floors,
            logged_decisions=logged_decisions,
            calibrations=calibrations,
        )
        response = HTMLResponse(render_transaction_cost_page(state))
        _remember_key(response, request)
        return response

    @app.get("/orders", response_class=HTMLResponse)
    def order_path_surface(request: Request) -> HTMLResponse:
        """`F02`'s surface: every intent, its order, its fills, and where the broker disagreed.

        Read from the write-ahead journal and nothing else. No broker session is opened and no
        reconciliation is run here: reconciling is a WRITE — it patches quantity gaps and can
        declare an order abandoned — and a page refresh must never be able to move an order. The
        report the order path's own reconciler produced at start-up is what belongs here, which is
        why the surface accepts one rather than making one.

        The journal is opened read-only in effect: if the file does not exist yet the page renders
        the empty state rather than creating it, because opening a journal creates it and a
        dashboard that writes a database on page load is a side effect nobody asked for.

        The exchange's algo audit-trail identifier is read from the environment here — the same
        source the live venue reads it from, so the page reports what orders will actually carry
        rather than a second opinion. Reading it costs nothing and opens nothing; it is a fact
        about this host's configuration (`docs/research/223` §4), and until the `M6` review it had
        no reader anywhere in the system, so an unset identifier was discoverable only by asking
        the exchange months later.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        now = datetime.now(IST)
        algo_identifier = exchange_algo_identifier_from_environment()
        if not DEFAULT_JOURNAL_PATH.exists():
            state = empty_order_path_surface_state(
                session_date=now.date(),
                measured_at=now,
                journal_path=DEFAULT_JOURNAL_PATH,
                exchange_algo_identifier=algo_identifier,
            )
        else:
            with OrderIntentJournal(DEFAULT_JOURNAL_PATH) as journal:
                state = build_order_path_surface_state(
                    journal,
                    measured_at=now,
                    journal_path=DEFAULT_JOURNAL_PATH,
                    exchange_algo_identifier=algo_identifier,
                )
        response = HTMLResponse(render_order_path_page(state))
        _remember_key(response, request)
        return response

    def _paper_capital_state(measured_at: datetime) -> PaperCapitalSurfaceState:
        """Measure the paper book WITHOUT creating anything.

        A GET must not seed the ledger. `PaperCapitalLedger.__init__` creates the file, which is
        exactly the side effect `/orders` refuses on page load, so the file's absence is answered
        here rather than by opening it.
        """
        try:
            ceiling = load_trading_capital_from_environment().total_rupees
        except CapitalConfigurationError as failure:
            return absent_paper_capital_surface_state(
                measured_at=measured_at,
                ledger_path=DEFAULT_PAPER_CAPITAL_LEDGER_PATH,
                live_ceiling_rupees=None,
                unavailable_reason=(
                    "the operator ceiling cannot be read, so the paper book cannot state what it "
                    f"would be measured against: {failure}"
                ),
            )
        if not DEFAULT_PAPER_CAPITAL_LEDGER_PATH.exists():
            return absent_paper_capital_surface_state(
                measured_at=measured_at,
                ledger_path=DEFAULT_PAPER_CAPITAL_LEDGER_PATH,
                live_ceiling_rupees=ceiling,
                unavailable_reason=(
                    "the paper capital ledger has never been created. Set a figure below to seed "
                    "it — rendering this page deliberately does not, because a dashboard that "
                    "writes a database on page load is a side effect nobody asked for."
                ),
            )
        with PaperCapitalLedger(DEFAULT_PAPER_CAPITAL_LEDGER_PATH) as ledger:
            return build_paper_capital_surface_state(
                ledger,
                measured_at=measured_at,
                live_ceiling_rupees=ceiling,
                ledger_path=DEFAULT_PAPER_CAPITAL_LEDGER_PATH,
            )

    @app.get("/paper-capital", response_class=HTMLResponse)
    def paper_capital_surface(request: Request) -> HTMLResponse:
        """`L1.18`'s surface: the paper trading book's virtual money and its whole history.

        Read-only. The balance shown is folded from the event log on every request and checked
        against the stored checkpoint, so a figure that has drifted from its own log refuses to
        render rather than rendering the cheaper of the two.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        response = HTMLResponse(render_paper_capital_page(_paper_capital_state(datetime.now(IST))))
        _remember_key(response, request)
        return response

    @app.post("/paper-capital", response_model=None)
    def set_paper_capital(
        request: Request,
        balance_rupees: str = Form(...),
        reason: str = Form(...),
    ) -> HTMLResponse | RedirectResponse:
        """The one write on this surface — the operator states the virtual capital they want to run.

        Seeds the ledger on first use, from the operator ceiling, then applies the stated figure. It
        is deliberately NOT capped at that ceiling: `R.03` requires every engine to be exercisable
        from a lakh to a crore, and the surface stamps an over-ceiling book rather than refusing it.

        A refusal from the ledger is rendered as a refusal, not swallowed into a redirect that would
        look identical to success — and it is rendered OVER THE BOOK AS IT STANDS. The first version
        built the failure page from the empty state, so one typo in the amount field told the
        operator their funded ledger had no capital, no history and an unreadable ceiling. A refusal
        must not erase the thing it refused to change.

        Timestamps are the ledger's to assign. This route deliberately does NOT pass `occurred_at`:
        two operators posting in the same instant each captured `now` before the other committed,
        and the second edit was rejected for being "earlier than the last event" — a race reported
        as a backdated event. Under the write lock the ledger stamps monotonically by construction.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        now = datetime.now(IST)

        def refused(failure_text: str) -> HTMLResponse:
            return HTMLResponse(
                render_paper_capital_page(
                    with_refusal(_paper_capital_state(now), failure_text)
                ),
                status_code=400,
            )

        try:
            figure = Decimal(balance_rupees.strip().replace(",", ""))
        except (ArithmeticError, ValueError):
            return refused(
                f"{balance_rupees!r} is not a number this ledger will interpret, so nothing was "
                "written."
            )
        try:
            ceiling = load_trading_capital_from_environment()
            with PaperCapitalLedger(DEFAULT_PAPER_CAPITAL_LEDGER_PATH) as ledger:
                if not ledger.events():
                    ledger.seed_from_ceiling(
                        ceiling,
                        reason="seeded from the operator ceiling on the first dashboard write",
                    )
                ledger.set_balance(figure, reason=reason)
        except (PaperCapitalError, CapitalConfigurationError) as failure:
            return refused(f"the ledger refused this edit: {failure}")
        redirect = RedirectResponse("/paper-capital", status_code=303)
        _remember_key(redirect, request)
        return redirect

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

    @app.get("/feed", response_class=HTMLResponse)
    def consolidated_feed_surface(request: Request) -> HTMLResponse:
        """`L0.33`'s surface, rendered from stored summaries rather than a live walk.

        Consolidating a session takes ~10 seconds over 70,000 captured quotes; doing that
        per page load is the mistake `/microstructure` is still carrying. The daily runner
        writes the summary and this reads it.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        runner = ConsolidatedFeedSessionRunner()
        reports = runner.stored_reports()
        latest = reports[-1] if reports else None
        engine = runner.engine
        rankings = engine.rank_brokers(latest.session_date) if latest else ()
        response = HTMLResponse(
            render_consolidated_feed_page(
                ConsolidatedFeedSurfaceState(
                    latest=latest,
                    reports=reports,
                    rankings=rankings,
                    reliabilities=BrokerReliabilityStore().all_reliabilities(),
                    noise_variances=engine.broker_noise_variances(),
                )
            )
        )
        _remember_key(response, request)
        return response

    @app.get("/history", response_class=HTMLResponse)
    def deep_history_surface(request: Request) -> HTMLResponse:
        """`L0.34`'s surface: how much of 33 years is actually queryable, and what was refused."""
        if not _is_authorised(request):
            return _unauthorised_html()
        from nse_algo_trader.deep_history.deep_history_archive_loader import (
            DeepHistoryArchiveLoader,
        )

        try:
            loader = DeepHistoryArchiveLoader(read_only=True)
        except Exception as failure:  # noqa: BLE001 - a load in progress holds the lock
            # Fail VISIBLY rather than render an empty page that looks like an empty
            # archive: DuckDB is single-writer, so a load in progress locks this out.
            return HTMLResponse(
                f"<h1>Deep history is being loaded</h1><p>{escape(str(failure))}</p>",
                status_code=503,
            )
        with loader:
            archive_counts = loader.archive_file_counts()
            coverage = [
                DeepHistoryMarketCoverage(
                    market=entry.market,
                    files=entry.files,
                    rows=entry.rows,
                    earliest=entry.earliest.isoformat() if entry.earliest else "—",
                    latest=entry.latest.isoformat() if entry.latest else "—",
                    distinct_symbols=entry.distinct_symbols,
                    archive_files=archive_counts.get(entry.market, 0),
                )
                for entry in loader.coverage()
            ]
            state = DeepHistorySurfaceState(
                coverage=coverage,
                rows_per_year=loader.rows_per_year(),
                quarantine_by_reason=(("all reasons", loader.quarantined_total()),),
                total_rows=sum(entry.rows for entry in coverage),
                total_quarantined=loader.quarantined_total(),
            )
        response = HTMLResponse(render_deep_history_page(state))
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
