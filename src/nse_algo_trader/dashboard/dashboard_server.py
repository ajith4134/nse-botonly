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
from nse_algo_trader.dashboard.bot_maturity_surface_renderer import render_bot_maturity_page
from nse_algo_trader.dashboard.clock_integrity_surface_renderer import (
    ClockIntegritySurfaceState,
    render_clock_integrity_page,
)
from nse_algo_trader.dashboard.consolidated_feed_surface_renderer import (
    ConsolidatedFeedSurfaceState,
    render_consolidated_feed_page,
)
from nse_algo_trader.dashboard.continuous_loop_surface_renderer import (
    ContinuousLoopSurfaceState,
    render_continuous_loop_page,
)
from nse_algo_trader.dashboard.decision_trace_surface_renderer import (
    render_decision_trace_page,
)
from nse_algo_trader.dashboard.deep_history_surface_renderer import (
    DeepHistoryMarketCoverage,
    DeepHistorySurfaceState,
    render_deep_history_page,
)
from nse_algo_trader.dashboard.live_trading_surface_renderer import (
    LivePositionRow,
    LiveTradingBotRow,
    LiveTradingSurface,
    render_live_trading_page,
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
from nse_algo_trader.dashboard.paper_session_surface_renderer import (
    read_paper_session_state,
    render_paper_session_page,
)
from nse_algo_trader.dashboard.regime_brain_read_model import (
    RegimeReadModelError,
    measure_regime_brain,
)
from nse_algo_trader.dashboard.regime_brain_surface_renderer import (
    render_regime_brain_page,
)
from nse_algo_trader.dashboard.segment_bot_surface_renderer import (
    SegmentBotSurfaceRow,
    render_segment_bot_page,
)
from nse_algo_trader.dashboard.sizing_surface_renderer import (
    SizingSurfaceState,
    absent_sizing_surface_state,
    render_sizing_page,
)
from nse_algo_trader.dashboard.trade_quality_surface_renderer import render_trade_quality_page
from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
    SegmentPricingAssumption,
    build_transaction_cost_surface_state,
    render_transaction_cost_page,
)
from nse_algo_trader.dashboard.trial_registry_surface_renderer import render_trial_registry_page
from nse_algo_trader.decision_trace.decision_trace_record import DecisionTraceStore
from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    price_basis_coverage_for,
)
from nse_algo_trader.market_depth.bar_tape_join_verdict_store import (
    refuted_instruments_for,
    verification_coverage_for,
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
from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
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
from nse_algo_trader.paper_loop.bot_maturity_ladder import (
    BotMaturityLadder,
    LadderPolicy,
    PaperTrackRecordStore,
)
from nse_algo_trader.paper_loop.continuous_paper_trading_scheduler import (
    DepthTapeObservationSource,
    SchedulerLivenessStore,
)
from nse_algo_trader.paper_loop.live_paper_book import LivePaperBook
from nse_algo_trader.paper_loop.segment_bot_paper_session import SegmentBotCapitalPolicy
from nse_algo_trader.paper_loop.walk_forward_archive_replay import (
    WalkForwardReplayCursorStore,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    SegmentRelevance,
)
from nse_algo_trader.segment_bots.segment_bot_registry import (
    BALANCED_REGISTRATION_REGIME,
    build_all_segment_bots,
    conformance_violations_by_identity,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.segment_bots.segment_universe_assembler import assemble_for
from nse_algo_trader.sizing.pre_trade_risk_gate import (
    PreTradeRiskGate,
    PriceCollarUnavailableError,
    RegulatoryFacts,
    derive_limits,
    realised_move_quantile_from_closes,
)
from nse_algo_trader.sizing.regulatory_facts_from_ingest_store import (
    assemble_regulatory_facts,
)
from nse_algo_trader.sizing.session_risk_state_store import (
    DEFAULT_SESSION_RISK_STATE_PATH,
    DailyLossLimit,
    SessionRiskState,
    SessionRiskStateError,
    SessionRiskStateStore,
)
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    SizingInputAssemblyError,
    assemble_sizing_inputs,
    tradeable_symbols,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    PositionSizingError,
    VolatilityTargetedPositionSizer,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_store import TradeQualityEvidenceStore
from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
    ChargeReconciliationLedger,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
)
from nse_algo_trader.validation.honest_trial_registry import HonestTrialRegistry

EQUAL_SEGMENT_COUNT = 6
"""`R.10` — the six segments are equal by default, so each may carry one concurrent position."""

SIZING_HORIZON_BARS = 5
"""The horizon this surface works a decision over. The same five bars `F01`'s calibration is fitted
across, so the edge and the volatility are measured over the same window rather than two."""

CASH_INTRADAY_MARGIN_FRACTION = Decimal("0.20")
"""What this surface assumes for illustration ONLY, and says so on the page.

The real per-segment margin regime has no source in this system yet — `segment_margin_fractions()`
returns `{}` on purpose, with the margin ingest (`L7.08`) as its named consumer. This figure exists
so the page can DRAW a leverage limit; nothing trades on it, and when `L7.08` lands this constant
goes away rather than being tuned."""

REGISTRATION_THRESHOLD_ORDERS_PER_SECOND = 10
"""NSE/INVG/67858 para B.5 — a regulatory fact, sourced (`A.101` decision 2)."""

SIZING_CANDIDATES_TRIED = 600

PRICE_COLLAR_QUANTILE = Decimal("0.95")
"""Which quantile of an instrument's own realised move counts as "unusually far" — the same policy
figure the paper loop runs with, so the page shows the limit the loop would actually apply."""
"""How many instruments the page walks before reporting that none could be sized.

Bounded because a page load must not scan the whole universe, and stated because `R.11` says a
bounded search reports what it skipped rather than reading as exhaustive.

Raised from 40 to 600 on 2026-08-13: after `A.106` corrected the deviation coordinate, 2,116 of
2,400 instruments have no calibration covering them, and the first forty alphabetically all fail.
The page rendered an honest "no decision" that told the operator nothing about a sizer that works.
This is a symptom of the coverage gap, not a fix for it — `F01`'s fitter needs more buckets."""

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

MARKET_DATA_DATABASE = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

NSE_INGEST_DATABASE = Path("~/.nse_algo_trader/nse_ingest.sqlite3").expanduser()
"""Where the exchange's own published facts land — the F&O ban list, MWPL, ASM/GSM surveillance.

Tier 1 of the risk gate reads from here. Eight symbols are on the ban list today, and until
`regulatory_facts_from_ingest_store` existed the gate reported every wall UNCHECKED."""

DEPTH_TAPE_BROKER = "kite"
"""Which broker's quotes the depth tape is recorded from — a deployment fact, and the one
that decides whose `L0.33` verdict gates the microstructure replay."""

DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
SCHEDULER_LIVENESS_PATH = Path("~/.nse_algo_trader/scheduler_liveness.sqlite3").expanduser()
"""The continuous loop's own liveness record. Read, never written, by this process."""
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
        "nse_algo_trader.validation.honest_trial_registry",
        "nse_algo_trader.dashboard.trial_registry_surface_renderer",
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
        # `F03` — everything `/sizing` actually draws.
        "nse_algo_trader.sizing.kelly_edge_scaler",
        "nse_algo_trader.sizing.realised_volatility_estimator",
        "nse_algo_trader.sizing.volatility_targeted_position_sizer",
        "nse_algo_trader.sizing.session_risk_state_store",
        "nse_algo_trader.sizing.pre_trade_risk_gate",
        "nse_algo_trader.sizing.sizing_inputs_from_real_stores",
        "nse_algo_trader.sizing.regulatory_facts_from_ingest_store",
        "nse_algo_trader.dashboard.sizing_surface_renderer",
        # `A.146` — everything `/trading` actually draws.
        "nse_algo_trader.paper_loop.live_paper_book",
        "nse_algo_trader.portfolio.portfolio_proposal_supervisor",
        "nse_algo_trader.paper_loop.walk_forward_archive_replay",
        "nse_algo_trader.paper_loop.segment_bot_warm_start_seeding",
        "nse_algo_trader.dashboard.live_trading_surface_renderer",
        # `A.146` — the pipeline the derivative universes are assembled from.
        "nse_algo_trader.nse_ingest.derivative_contract_record_projection",
        "nse_algo_trader.market_depth.derivative_capture_universe_selector",
        "nse_algo_trader.market_depth.capture_candidate_population_merger",
        "nse_algo_trader.market_depth.capture_shard_population_planner",
        # `F04` — everything `/paper-session` actually draws.
        "nse_algo_trader.paper_loop.paper_trading_session_runner",
        "nse_algo_trader.paper_loop.paper_session_signal_source",
        "nse_algo_trader.paper_loop.simulated_execution_venue",
        "nse_algo_trader.order_path.simulated_order_execution_venue",
        "nse_algo_trader.paper_loop.replayed_depth_book_source",
        "nse_algo_trader.paper_loop.simulated_time_submission_rate_gate",
        "nse_algo_trader.order_path.order_submission_rate_limiter",
        "nse_algo_trader.market_depth.capture_liveness_record",
        "nse_algo_trader.dashboard.paper_session_surface_renderer",
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


PORTFOLIO_NET_DIRECTIONAL_FRACTION = SegmentBotCapitalPolicy(
    deployable_rupees=Decimal("1000000")
).maximum_net_directional_fraction
"""The bound the supervisor actually applies, imported from the policy that owns it rather than
retyped here — a page that states a different bound from the one enforced is worse than no page."""

_LIVE_TRADING_BLOCKERS: dict[str, str] = {
    "commodity_mcx_basis_carry_bot": (
        "B30 — no MCX data anywhere, so this bot is built whole and activates on nothing"
    ),
    "index_futures_basis_carry_bot": (
        "B38 — index-future margin is understated by roughly half against broker quotes, so it "
        "stays unsized until the SPAN file arrives (B36, one manual download)"
    ),
    "stock_futures_basis_carry_bot": (
        "B36 — futures consume SPAN margin, not notional; sized on the estimator until the "
        "risk-parameter file is downloaded"
    ),
}
"""Named blockers, per bot. `R.11`: a bot holding nothing because it is blocked and a bot holding
nothing because it saw no opportunity look identical, and they need completely different things."""

_LIVE_TRADING_NOTES: tuple[str, ...] = (
    "B47 — an intraday entry is filled at the tape's last traded price, not by walking the "
    "recorded L2 ladder as the replay path does, so fills are optimistic where the book is thin.",
)


def _registered_identities() -> tuple[str, ...]:
    """Every bot the registry builds, so a bot that has never traded still gets a row."""
    try:
        return tuple(bot.bot_identity for bot in build_all_segment_bots())
    except Exception:  # noqa: BLE001 — a page that cannot list the six is still worth rendering
        return ()


def _segment_of(bot_identity: str) -> str:
    for bot in _registered_bots_cached():
        if bot.bot_identity == bot_identity:
            return bot.trading_segment.value
    return "unknown"


def _registered_bots_cached() -> tuple[object, ...]:
    try:
        return tuple(build_all_segment_bots())
    except Exception:  # noqa: BLE001
        return ()


def _live_prices_for(positions: tuple[object, ...]) -> dict[int, Decimal]:
    """The live tape's latest price for each held instrument, or nothing when it cannot answer.

    Nothing is invented when the tape is silent: `LivePositionRow` renders the position as UNPRICED,
    which is a different claim from "it has not moved".
    """
    if not positions:
        return {}
    try:
        source = DepthTapeObservationSource(DEPTH_TAPE_ROOT)
        return source.latest_prices(datetime.now(IST).date())
    except Exception:  # noqa: BLE001 — a silent tape is a rendered "unpriced", never a failed page
        return {}


def _latest_scheduler_iteration() -> object | None:
    try:
        recent = SchedulerLivenessStore(SCHEDULER_LIVENESS_PATH).recent(limit=1)
    except Exception:  # noqa: BLE001
        return None
    return recent[0] if recent else None


def _walk_forward_progress() -> str:
    """What the closed-market walk has covered, read from its own cursor."""
    try:
        store = WalkForwardReplayCursorStore()
        sessions = store.replayed_sessions()
        accrued = store.total_trades_accrued()
    except Exception:  # noqa: BLE001
        return ""
    if not sessions:
        return "no archived session has been replayed yet"
    return (
        f"{len(sessions):,} session(s) replayed, {sessions[0].isoformat()} to "
        f"{sessions[-1].isoformat()}, {accrued:,} trade(s) accrued"
    )


def _paper_book_capital_rupees() -> Decimal | None:
    """What the paper ledger actually holds, or `None` when it cannot say.

    `None` rather than a typed default (`R.03`): a page that asserts Rs 10,00,000 because the ledger
    was unreadable states a capital nothing measured, and every fraction shown against it — the
    exposure bar most of all — would then be a ratio of a real number to an invented one.
    """
    try:
        fold = PaperCapitalLedger().fold_from_events()
    except Exception:  # noqa: BLE001 — an unreadable ledger is reported, never guessed around
        return None
    return fold.balance_rupees if fold.balance_rupees > 0 else None


def _measure_live_trading() -> LiveTradingSurface:
    """Fold the live book, the track record and the loop's own liveness into one page.

    Reads three stores and derives everything else. The loop process owns the book; this process
    only reads it, which is why the numbers cannot drift apart — there is one book and one reader
    of it per request.
    """
    book = LivePaperBook()
    track_record = PaperTrackRecordStore()
    try:
        positions = book.open_positions()
        prices = _live_prices_for(positions)
        today = datetime.now(IST).date()

        by_identity: dict[str, list[LivePositionRow]] = {}
        for position in positions:
            by_identity.setdefault(position.bot_identity, []).append(
                LivePositionRow(
                    trading_symbol=position.trading_symbol,
                    side=str(position.side),
                    quantity=position.quantity,
                    entry_price_paise=position.entry_price_paise,
                    last_price_paise=prices.get(position.instrument_token),
                    opened_at=position.opened_at,
                )
            )

        rows: list[LiveTradingBotRow] = []
        identities = sorted(
            set(by_identity) | set(track_record.bot_identities()) | set(_registered_identities())
        )
        for identity in identities:
            closed = track_record.closed_trades_for(identity)
            closed_today = [trade for trade in closed if trade.session_date == today]
            rows.append(
                LiveTradingBotRow(
                    bot_identity=identity,
                    trading_segment=_segment_of(identity),
                    open_positions=tuple(by_identity.get(identity, ())),
                    closed_today=len(closed_today),
                    realised_today_rupees=sum(
                        (trade.net_rupees for trade in closed_today), Decimal("0")
                    ),
                    closed_all_time=len(closed),
                    realised_all_time_rupees=sum(
                        (trade.net_rupees for trade in closed), Decimal("0")
                    ),
                    proposals_last_tick=None,
                    blocker=_LIVE_TRADING_BLOCKERS.get(identity, ""),
                )
            )

        iteration = _latest_scheduler_iteration()
        capital = _paper_book_capital_rupees()
        return LiveTradingSurface(
            rows=tuple(rows),
            session_date=iteration.session_date if iteration else today,
            phase=str(iteration.phase) if iteration else "unknown",
            observed_at=iteration.observed_at if iteration else None,
            deployable_rupees=capital,
            net_directional_bound_rupees=(
                None if capital is None else capital * PORTFOLIO_NET_DIRECTIONAL_FRACTION
            ),
            tape_lag_seconds=iteration.tape_lag_seconds if iteration else None,
            archive_progress=_walk_forward_progress(),
            notes=_LIVE_TRADING_NOTES,
        )
    finally:
        book.close()
        track_record.close()


def _measure_segment_bots() -> tuple[SegmentBotSurfaceRow, ...]:
    """Build all six bots, drive them over their REAL universes, and report what the code just said.

    **Measured, never maintained** (`R.08`). The rung comes off each bot's own closed trades, the
    conformance count is the shared `L5.29` suite actually executed here against a probe that
    includes an EMPTY universe, and the readiness columns come from each bot observing the same
    universe the paper loop would hand it — assembled by `segment_universe_assembler` from
    `price_bars` for cash and `fo_bhavcopy_contracts` for the derivatives.

    **The universe is real and the read is bounded.** A page load cannot observe 25,785 stock-option
    contracts and stay a page, so the read is capped and the cap is PRINTED in the assembler's own
    note rather than applied silently — `R.09`'s failure mode is a sample that looks like a board.
    An earlier version of this function reported the registration probe's two synthetic instruments
    instead, which made every bot look equally ready and made `commodity_mcx` — which has no data at
    all — indistinguishable from the five that do. That is precisely the confusion this page exists
    to prevent, so it is recorded here rather than quietly corrected.

    Never raises. A dashboard that 500s because one bot could not be built tells the operator less
    than a page that says which one.
    """
    now = datetime.now(IST)
    try:
        # The gate runs on throwaway instances; these bots are FRESH, so nothing they carry came
        # from the probe's synthetic prices.
        violations_by_identity = conformance_violations_by_identity(at=now)
        bots = build_all_segment_bots()
    except Exception:  # noqa: BLE001 — the page reports a broken registry, it does not become one
        return ()

    rows: list[SegmentBotSurfaceRow] = []
    for bot in bots:
        try:
            universe = assemble_for(
                bot.trading_segment, as_of=now, limit=SEGMENT_SURFACE_INSTRUMENT_LIMIT
            )
            context = SegmentBotContext(
                decision_instant=now,
                tradeable_universe=universe.instruments,
                regime=BALANCED_REGISTRATION_REGIME,
                carried_memory=universe.carried_memory(),
            )
            bot.observe(context)
            relevance = bot.relevance(context)
            note = universe.note
        except Exception as failure:  # noqa: BLE001 — one unreadable store is one row, not the page
            relevance = SegmentRelevance(
                applicability=0.0, reason=f"universe could not be assembled: {failure}"
            )
            note = str(failure)
        maturity = bot.maturity()
        blocker = SEGMENT_DATA_BLOCKERS.get(bot.trading_segment, "")
        rows.append(
            SegmentBotSurfaceRow(
                bot_identity=bot.bot_identity,
                trading_segment=bot.trading_segment,
                cadence=bot.cadence,
                rung=maturity.rung,
                closed_trades=maturity.closed_trades_observed,
                sessions=bot.track_record.sessions,
                instruments_tracked=bot.instruments_tracked,
                instruments_mature=bot.instruments_mature,
                relevance=relevance.applicability,
                relevance_reason=f"{relevance.reason} — {note}",
                conformance_violations=len(violations_by_identity.get(bot.bot_identity, ())),
                data_blocker=blocker,
            )
        )
    return tuple(rows)


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
    """Every instrument this session's evidence refuses, from BOTH gates.

    Two independent refusals, unioned because they answer different questions and either one is
    disqualifying:

    - `L0.33` — the consolidated feed measured the DEPTH BROKER as divergent or frozen on this
      instrument, so its recorded book describes the feed rather than the market;
    - `M14` — the bar store and the depth tape were measured to describe different markets on
      this instrument, so a signal taken from one and filled against the other is a join of two
      unrelated series (`docs/research/236`).

    This is where the consolidated feed's verdict becomes a behaviour change: the depth tape
    is recorded from Kite, so Kite's own admissibility decides which instruments the
    microstructure replay may build features from. The mapping from the feed's trading
    symbols to the tape's instrument tokens comes from the stored instrument master — the
    same table the recorder subscribed by — so a symbol the master does not know is simply
    not gated rather than silently dropped.
    """
    join_refusals = refuted_instruments_for(session_date)
    admissibility = ConsolidatedFeedSessionRunner().engine.admissibility(session_date)
    unreliable_symbols = {
        symbol
        for (broker, symbol), admissible in admissibility.items()
        if broker == DEPTH_TAPE_BROKER and not admissible
    }
    if not unreliable_symbols:
        return join_refusals
    with sqlite3.connect(f"file:{MARKET_DATA_DATABASE}?mode=ro", uri=True) as connection:
        placeholders = ",".join("?" for _ in unreliable_symbols)
        rows = connection.execute(
            f"SELECT DISTINCT instrument_token FROM instrument_master "  # noqa: S608 - the
            # placeholders are generated from the set's LENGTH; every value is bound
            f"WHERE tradingsymbol IN ({placeholders}) AND segment = 'NSE'",
            tuple(unreliable_symbols),
        ).fetchall()
    return join_refusals | frozenset(int(row[0]) for row in rows)


SEGMENT_SURFACE_INSTRUMENT_LIMIT = 1500
"""How many instruments this PAGE observes per bot. A rendering bound, never a trading one.

The stock-option chain alone is 25,785 contracts; observing all of them on every page load would
make this a batch job rather than a surface. The paper loop passes `limit=None` and sees the whole
board (`R.09`). The assembler prints the bound in its own note whenever it bites, so a reader is
never shown a sample that looks like a board.
"""

SEGMENT_DATA_BLOCKERS: dict[TradingSegment, str] = {
    TradingSegment.INDEX_OPTIONS: (
        "no intraday tape: the depth capture has never subscribed an NFO token "
        "(2,135,786 ticks today, all NSE cash). Daily bhavcopy only until A.142 fills."
    ),
    TradingSegment.STOCK_OPTIONS: (
        "no intraday tape: same NFO gap as index options. 1,220,678 STO rows of daily history."
    ),
    TradingSegment.INDEX_FUTURES: (
        "no intraday tape, and only 540 IDF rows of daily history — B31. A low rung here is the "
        "ladder working, not the bot failing."
    ),
    TradingSegment.STOCK_FUTURES: (
        "no intraday tape; 22,561 STF rows of daily history — the healthiest of the "
        "derivative three."
    ),
    TradingSegment.COMMODITY_MCX: (
        "NO DATA AT ALL — B30. Zero MCX rows in fo_bhavcopy_contracts and no MCX token has ever "
        "been in the depth tape. The bot is built whole and activates on nothing until MCX "
        "ingestion lands (operator deferred it, A.142)."
    ),
}
"""Why a bot cannot act, per segment, when the reason is DATA rather than judgement.

Every figure was measured on 2026-08-18 and is quoted so a reader can re-check it. Kept beside the
page rather than inside the bots because a bot must not know about the ingestion pipeline — that is
the spine's business, and `L5.29` forbids a bot reading anything at all.
"""

DASHBOARD_LADDER_POLICY = LadderPolicy(
    promotion_confidence=0.95,
    sustained_sessions_required=20,
    minimum_trades_for_a_posterior=30,
)
"""The same policy the daily run applies, so the board and the report cannot disagree.

`R.08` says status is MEASURED, never hand-authored — a surface computing a rung under a laxer
policy than the one that governs would be hand-authoring the most consequential number on it.
"""


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
        response = HTMLResponse(
            render_order_book_replay_page(
                report,
                join_coverage=verification_coverage_for(report.session_date),
                price_basis=price_basis_coverage_for(report.session_date, MARKET_DATA_DATABASE),
            )
        )
        _remember_key(response, request)
        return response

    @app.get("/trials", response_class=HTMLResponse)
    def trial_registry_surface(request: Request) -> HTMLResponse:
        """`L2.01`'s surface: how large the search really was, and whether the record still holds.

        Every number is measured from the registry itself, so a trial recorded tomorrow appears
        here with no edit, and a chain broken by a removed trial turns this page red by itself.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        registry = HonestTrialRegistry()
        response = HTMLResponse(
            render_trial_registry_page(
                exists=registry.exists(),
                cumulative=registry.cumulative_trials(),
                by_outcome=registry.trials_by_outcome(),
                broken_at=registry.verify_chain(),
            )
        )
        _remember_key(response, request)
        return response

    @app.get("/ladder", response_class=HTMLResponse)
    def bot_maturity_surface(request: Request) -> HTMLResponse:
        """`L5.30`'s surface: where each paper-trading bot stands on the activation ladder.

        Measured from the track record on every request, so a trade recorded by tonight's paper
        session moves this page with no edit. The rung is the FIRST of `R.22`'s two keys, and a
        gate nobody looks at is a gate nobody notices going wrong.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        store = PaperTrackRecordStore()
        ladder = BotMaturityLadder(store)
        assessments = tuple(
            ladder.assess(identity, DASHBOARD_LADDER_POLICY)
            for identity in store.bot_identities()
        )
        response = HTMLResponse(render_bot_maturity_page(assessments))
        _remember_key(response, request)
        return response

    @app.get("/loop", response_class=HTMLResponse)
    def continuous_loop_surface(request: Request) -> HTMLResponse:
        """`L10.01`'s surface: is the loop alive, what is it seeing, how stale is its data.

        Read from the loop's own append-only record on every request. The two numbers that matter
        are the time since the last HEALTHY tick — a process can be up and failing every iteration —
        and the tape lag, which catches the failure that looks most like success: a live loop
        reading a dead capture, where every iteration succeeds and nothing is true.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        try:
            store = SchedulerLivenessStore()
            state = ContinuousLoopSurfaceState(
                measured_at=datetime.now(IST),
                iterations=store.recent(),
                last_healthy_at=store.last_healthy_at(),
            )
        except Exception:  # noqa: BLE001 — an unreadable record renders as "never ran", not a 500
            state = ContinuousLoopSurfaceState(
                measured_at=datetime.now(IST), iterations=(), last_healthy_at=None
            )
        response = HTMLResponse(render_continuous_loop_page(state))
        _remember_key(response, request)
        return response

    @app.get("/bots", response_class=HTMLResponse)
    def segment_bot_surface(request: Request) -> HTMLResponse:
        """`L5.26`-`L5.28` and the three futures bots: all six holons, measured on this request.

        Everything is produced by running the code, not read from a status anybody maintains: the
        six are built by the registry, each carrying its own track record off `BotMaturityLadder`;
        the `L5.29` conformance suite is re-run against a probe that includes an EMPTY universe; and
        each bot's relevance is its own carried state answering.

        The column that matters is `universe readiness` against the data blocker beside it. A bot
        with nothing to trade and a bot that has not proven itself both sit at `COLD_START`, and
        they need completely different things (`A.141`, `A.142`).
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        response = HTMLResponse(render_segment_bot_page(_measure_segment_bots()))
        _remember_key(response, request)
        return response

    @app.get("/trading", response_class=HTMLResponse)
    def live_trading_surface(request: Request) -> HTMLResponse:
        """`A.146`: what the six bots are holding RIGHT NOW, and what it is worth.

        The page the operator asked for by name — *"where can I see these six bots open, closed or
        trading"*. `/bots` says which of the six CAN act and `/ladder` says which have EARNED
        anything; neither could show a position, because until `A.146` the loop opened none.

        Folded from `live_paper_book` and `PaperTrackRecordStore` on this request. Nothing cached,
        nothing hand-authored (`R.08`), and a position the tape cannot price is counted as unpriced
        rather than marked flat.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        response = HTMLResponse(render_live_trading_page(_measure_live_trading()))
        _remember_key(response, request)
        return response

    @app.get("/quality", response_class=HTMLResponse)
    def trade_quality_surface(request: Request) -> HTMLResponse:
        """`L5.31`'s surface: every proposal the quality floor judged, admitted and refused alike.

        Measured from the evidence store on every request, so a verdict recorded by tonight's run
        moves this page with no edit. The refusals are the point: they are the only record that can
        ever show the floor was too high.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        store = TradeQualityEvidenceStore()
        sessions = store.sessions_recorded()
        cards = store.cards_for_session(sessions[-1]) if sessions else ()
        response = HTMLResponse(render_trade_quality_page(cards))
        _remember_key(response, request)
        return response

    @app.get("/traces", response_class=HTMLResponse)
    def decision_trace_surface(request: Request) -> HTMLResponse:
        """`L13.29`'s surface: what the bot was thinking, from records written at the instant.

        `A.29` required the trace to exist BEFORE this page, so that nothing here is reconstructed
        from what was traded afterwards.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        store = DecisionTraceStore()
        sessions = store.sessions_recorded()
        summary = store.session_summary(sessions[0]) if sessions else None
        response = HTMLResponse(render_decision_trace_page(summary))
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
            for bucket in calibration_store.calibrated_buckets(horizon_bars=horizon, as_of=today):
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
                render_paper_capital_page(with_refusal(_paper_capital_state(now), failure_text)),
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

    def _regulatory_facts_for(symbol: str, session_date: date) -> RegulatoryFacts:
        """Tier 1's real facts, or every wall honestly UNCHECKED when the store is unreachable.

        Read-only and point-in-time. The fallback is deliberately the CONSERVATIVE one: an
        unreadable store yields `None` for every wall, which the gate shouts as UNCHECKED, rather
        than a `False` that would read as "not banned" on the morning the ban file failed.
        """
        if not NSE_INGEST_DATABASE.exists():
            return RegulatoryFacts(trading_symbol=symbol)
        try:
            with BitemporalIngestStore(NSE_INGEST_DATABASE) as ingest_store:
                return assemble_regulatory_facts(
                    trading_symbol=symbol,
                    ingest_store=ingest_store,
                    effective_date=session_date,
                )
        except sqlite3.Error:
            return RegulatoryFacts(trading_symbol=symbol)

    def _sizing_state(now: datetime, requested_symbol: str) -> SizingSurfaceState:
        """Work one real decision, or say exactly which store could not answer.

        Picks the first instrument the calibration ladder actually covers when no symbol is asked
        for, because a default page that always reads "no calibration" teaches nobody anything —
        and 2,086 of 2,400 instruments have no calibration today (`R.05` run, 2026-08-13).
        """
        session_date = now.date()
        session_state: SessionRiskState | None = None
        daily_limit: DailyLossLimit | None = None
        if DEFAULT_SESSION_RISK_STATE_PATH.exists():
            with SessionRiskStateStore(DEFAULT_SESSION_RISK_STATE_PATH) as risk_store:
                try:
                    session_state = risk_store.state_for(session_date=session_date, now=now)
                except SessionRiskStateError:
                    session_state = None
                daily_limit = risk_store.daily_loss_limit(as_of=session_date)

        try:
            capital = load_trading_capital_from_environment()
        except CapitalConfigurationError as failure:
            return absent_sizing_surface_state(
                measured_at=now,
                session_date=session_date,
                state_path=DEFAULT_SESSION_RISK_STATE_PATH,
                unavailable_reason=f"the operator ceiling cannot be read: {failure}",
                missing_inputs=("capital_configuration",),
            )

        try:
            universe = tradeable_symbols(as_of=session_date)
        except SizingInputAssemblyError as failure:
            return absent_sizing_surface_state(
                measured_at=now,
                session_date=session_date,
                state_path=DEFAULT_SESSION_RISK_STATE_PATH,
                unavailable_reason=str(failure),
                missing_inputs=failure.missing,
            )

        candidates = [row for row in universe if not requested_symbol or row[1] == requested_symbol]
        if not candidates:
            return absent_sizing_surface_state(
                measured_at=now,
                session_date=session_date,
                state_path=DEFAULT_SESSION_RISK_STATE_PATH,
                unavailable_reason=(
                    f"{requested_symbol or 'no instrument'} is not in the sizable universe — it "
                    "needs both a lot size in the instrument master and recorded bars"
                ),
                missing_inputs=("instrument_master", "price_bars"),
            )

        last_failure = "no instrument could be sized"
        last_missing: tuple[str, ...] = ()
        for token, symbol_name, lot_size in candidates[:SIZING_CANDIDATES_TRIED]:
            try:
                inputs = assemble_sizing_inputs(
                    instrument_token=token,
                    trading_symbol=symbol_name,
                    lot_size=lot_size,
                    deployable_rupees=capital.total_rupees,
                    concurrent_position_capacity=EQUAL_SEGMENT_COUNT,
                    horizon_bars=SIZING_HORIZON_BARS,
                    as_of=now,
                )
                sized = VolatilityTargetedPositionSizer().size(inputs)
            except (SizingInputAssemblyError, PositionSizingError) as failure:
                last_failure = str(failure)
                last_missing = getattr(failure, "missing", ())
                continue

            # The collar is a quantile of THIS instrument's own realised move over the horizon.
            # It read the calibration's `mean_captured_sigma` until 2026-08-15, which is a mean
            # CAPTURE and is legitimately negative for a losing bucket — this page would have
            # raised out of the middle of a decision on the first such scrip (`A.110`).
            try:
                collar = realised_move_quantile_from_closes(
                    inputs.recent_closes,
                    horizon_bars=SIZING_HORIZON_BARS,
                    quantile=PRICE_COLLAR_QUANTILE,
                )
            except PriceCollarUnavailableError as uncollared:
                last_failure = str(uncollared)
                last_missing = ("price_bars",)
                continue

            limits = derive_limits(
                deployable_rupees=capital.total_rupees,
                traded_value_percentile_rupees=capital.total_rupees,
                realised_move_percentile_fraction=collar,
                segment_margin_fraction=CASH_INTRADAY_MARGIN_FRACTION,
                registration_threshold_orders_per_second=(REGISTRATION_THRESHOLD_ORDERS_PER_SECOND),
            )
            resting = session_state or SessionRiskState(
                session_date=session_date,
                opening_equity_rupees=capital.total_rupees,
                peak_equity_rupees=capital.total_rupees,
                realised_pnl_rupees=Decimal(0),
                open_exposure_rupees=Decimal(0),
                exposure_by_symbol=(),
                orders_in_rate_window=0,
                tripped_latches=(),
            )
            limit = daily_limit or DailyLossLimit(
                limit_rupees=None,
                sessions_observed=0,
                sigma_daily_rupees=None,
                z_quantile=Decimal(0),
                unavailable_reason="no session history has been recorded",
            )
            verdict = PreTradeRiskGate().evaluate(
                sized=sized,
                state=resting,
                facts=_regulatory_facts_for(symbol_name, session_date),
                limits=limits,
                daily_loss_limit=limit,
            )
            return SizingSurfaceState(
                measured_at=now,
                session_date=session_date,
                sized=sized,
                verdict=verdict,
                session_state=session_state,
                daily_loss_limit=daily_limit,
                state_path=DEFAULT_SESSION_RISK_STATE_PATH,
            )

        return absent_sizing_surface_state(
            measured_at=now,
            session_date=session_date,
            state_path=DEFAULT_SESSION_RISK_STATE_PATH,
            unavailable_reason=last_failure,
            missing_inputs=last_missing,
        )

    @app.get("/sizing", response_class=HTMLResponse)
    def sizing_surface(request: Request, symbol: str = "") -> HTMLResponse:
        """`F03`'s surface: a WORKED sizing decision against the real stores, plus the verdict.

        The decision is computed on request from the real bar store, the real instrument master and
        the real reversion calibration, point-in-time. That is deliberate: a page that showed a
        cached size would be showing what the sizer said once, and the number an operator needs to
        trust is the one it says now.

        Read-only in every sense. No store is written, and the session's latches are READ rather
        than evaluated — a page load must never halt a session, because a read that halts is a read
        nobody can safely perform to find out where they stand.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        now = datetime.now(IST)
        state = _sizing_state(now, symbol.strip().upper())
        response = HTMLResponse(render_sizing_page(state))
        _remember_key(response, request)
        return response

    @app.get("/paper-session", response_class=HTMLResponse)
    def paper_session_surface(request: Request, session: str = "") -> HTMLResponse:
        """`F04`'s surface: the day the paper loop ran, folded out of the stores it wrote.

        Nothing is replayed on request. A paper session is a run, not a query — re-running it on a
        page load would take minutes and would show a different day to two readers refreshing at
        once — so this page reads what the last run recorded and says plainly when there is none.
        """
        if not _is_authorised(request):
            return _unauthorised_html()
        now = datetime.now(IST)
        requested: date | None = None
        if session.strip():
            try:
                requested = date.fromisoformat(session.strip())
            except ValueError:
                requested = None
        state = read_paper_session_state(measured_at=now, session_date=requested)
        response = HTMLResponse(render_paper_session_page(state))
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
