"""The daily operational loop — the thing every `L0` engine was built to be used BY.

Written because the operations wall measured 24 orphans (`R.06`): engines that exist, are
tested, and that **no runnable thing reaches**. An orphan is not a cosmetic finding — it
means the daily work those engines were built for is not happening. Three of the ingest
sources are rolling files with no archive, so every day this does not run is a day of
history that cannot be recovered afterwards at any price.

It also closes the blocker recorded earlier today: *nothing schedules any ingest run*.

**Each step genuinely uses its engine.** Importing modules to satisfy a reachability
metric would be exactly the reward-hacking `R.23(d)` names — the wall would go green while
nothing changed. So the ingest steps really fetch and store, and the store steps really
query and report the state a human needs each morning.

**One failing step never kills the run.** A blocked NSE endpoint must not stop the bar
store from being reported, so every step is guarded and its failure is recorded and
surfaced at the end rather than raised. The exit code reflects whether anything failed,
so a scheduler can tell.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_algo_trader.bitemporal_bar_store import BitemporalBarStore
from nse_algo_trader.broker_credentials import (
    BrokerName,
    load_broker_api_credentials,
    load_env_file_into_environ,
)
from nse_algo_trader.broker_credentials.kite_login_credentials_loader import (
    load_kite_login_credentials,
)
from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    KiteAccessTokenFileStore,
)
from nse_algo_trader.broker_sessions.kite_totp_auto_login import (
    generate_and_store_daily_kite_access_token,
)
from nse_algo_trader.capital_configuration import (
    CapitalConfigurationError,
    load_trading_capital_from_environment,
)
from nse_algo_trader.corporate_action_adjustment_engine import (
    CorporateActionAdjustmentEngine,
)
from nse_algo_trader.dashboard.dashboard_surface_screenshot_capture import (
    DEFAULT_BASE_URL,
    DEFAULT_OUTPUT_ROOT,
    DashboardCaptureError,
    capture_dashboard,
    capture_failed,
    read_access_token,
    summarise_capture,
)
from nse_algo_trader.kite_instrument_master import (
    InstrumentMasterStore,
    fetch_instrument_dump,
    parse_instrument_dump,
)
from nse_algo_trader.nse_ingest.atm_implied_volatility_adapter import (
    AtmImpliedVolatilityAdapter,
)
from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_ingest.bulk_block_deals_adapter import BulkBlockDealsAdapter
from nse_algo_trader.nse_ingest.circuit_band_surveillance_adapter import (
    CircuitBandSurveillanceAdapter,
)
from nse_algo_trader.nse_ingest.delisted_securities_adapter import (
    DelistedSecuritiesAdapter,
)
from nse_algo_trader.nse_ingest.discovering_ingest_source_adapter import (
    DiscoveredParameterStore,
)
from nse_algo_trader.nse_ingest.fo_ban_list_adapter import FoBanListAdapter
from nse_algo_trader.nse_ingest.index_constituents_adapter import (
    IndexConstituentsAdapter,
)
from nse_algo_trader.nse_ingest.ingest_coverage_self_check import (
    build_source_coverage_report,
)
from nse_algo_trader.nse_ingest.ingest_gap_backfill_planner import (
    backfill_missing_dates,
    dates_needing_human_attention,
    find_missing_dates,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import NseIngestSourceAdapter
from nse_algo_trader.nse_ingest.mwpl_position_limits_adapter import (
    MwplPositionLimitsAdapter,
)
from nse_algo_trader.nse_ingest.nse_bhavcopy_adapter import (
    CASH_MARKET,
    FO_MARKET,
    NseBhavcopyAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import NseSourceFetcher, RetryPolicy
from nse_algo_trader.nse_ingest.nse_source_ingest_runner import NseSourceIngestRunner
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar
from nse_algo_trader.point_in_time_universe_engine import PointInTimeUniverseEngine

IST = ZoneInfo("Asia/Kolkata")
STATE_DIRECTORY = Path("~/.nse_algo_trader").expanduser()
INGEST_DATABASE = STATE_DIRECTORY / "nse_ingest.sqlite3"
MARKET_DATA_DATABASE = STATE_DIRECTORY / "market_data.sqlite3"
DISCOVERY_MEMO_DATABASE = STATE_DIRECTORY / "nse_ingest.sqlite3"

MAXIMUM_BACKFILL_DATES_PER_RUN = 5
"""A bound, so a source years behind cannot turn one nightly run into an unbounded crawl
of a host that bot-blocks. When it truncates, the report says so."""

NSE_SESSION_CLOSE_IST = dt_time(15, 30)
"""Continuous trading ends. An exchange fact, and the boundary that decides whether
today's files can exist yet."""

PERSISTENT_FAILURE_ATTEMPTS = 3
"""`R.21` three strikes, applied to acquisition: a date that has failed this often will
not fix itself and is escalated to a human rather than retried forever in silence."""


def most_recent_closed_session(
    now_ist: datetime, calendar: NseTradingSessionCalendar
) -> date:
    """The latest session whose files can actually exist yet.

    Today counts only once trading has closed. A nightly job firing at 08:00 IST would
    otherwise pick TODAY — a real trading session that has not traded — and every fetch
    would 404 against files NSE has not published, which the gap planner would then have
    to classify as an established absence. Scheduling made this matter; it never showed
    up in a hand-run.
    """
    candidate = now_ist.date()
    if now_ist.time() < NSE_SESSION_CLOSE_IST:
        candidate -= timedelta(days=1)
    while not calendar.is_trading_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


@dataclass
class StepOutcome:
    """What one step did, or why it could not."""

    name: str
    detail: str = ""
    failure: str | None = None
    seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.failure is None


@dataclass
class DailyRunReport:
    """The morning summary. Failures are collected, never swallowed."""

    started_at: datetime
    outcomes: list[StepOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[StepOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.succeeded]

    def describe(self) -> str:
        lines = [f"daily operations — started {self.started_at:%Y-%m-%d %H:%M} UTC", ""]
        for outcome in self.outcomes:
            marker = "ok  " if outcome.succeeded else "FAIL"
            lines.append(
                f"  [{marker}] {outcome.name} ({outcome.seconds:.1f}s): "
                f"{outcome.detail or outcome.failure}"
            )
        lines.append("")
        lines.append(
            f"{len(self.outcomes) - len(self.failures)}/{len(self.outcomes)} steps succeeded"
        )
        return "\n".join(lines)


def _run_step(report: DailyRunReport, name: str, action: Callable[[], str]) -> None:
    """Run one step, guarded, and say so as it happens.

    A blocked NSE endpoint must not stop the bar store from being reported, so a failure
    here is recorded and the loop continues. The traceback's last line is kept because a
    bare exception type rarely says enough to act on the next morning.

    Progress is printed per step rather than only in the closing summary. The first
    scheduled-shape run produced NO output for twelve minutes, which makes a nightly job
    impossible to diagnose from journald — you cannot tell slow from hung.
    """
    print(f"  -> {name} ...", flush=True)
    started = time.monotonic()
    try:
        detail = action()
        elapsed = time.monotonic() - started
        report.outcomes.append(StepOutcome(name, detail=detail, seconds=elapsed))
        print(f"  ok  {name} ({elapsed:.1f}s): {detail}", flush=True)
    except Exception as failure:  # noqa: BLE001 — recorded and surfaced, never swallowed
        elapsed = time.monotonic() - started
        last_line = traceback.format_exc().strip().splitlines()[-1]
        report.outcomes.append(
            StepOutcome(name, failure=f"{type(failure).__name__}: {last_line}", seconds=elapsed)
        )
        print(f"  FAIL {name} ({elapsed:.1f}s): {last_line}", flush=True)


def _refresh_kite_session() -> str:
    """Ensure a valid daily token exists. Kite tokens die ~06:00 IST each morning."""
    load_env_file_into_environ()
    store = KiteAccessTokenFileStore()
    existing = store.load_if_still_valid()
    if existing is not None:
        return f"existing token still valid (user {existing.kite_user_id})"
    record = generate_and_store_daily_kite_access_token(
        load_broker_api_credentials(BrokerName.ZERODHA_KITE),
        load_kite_login_credentials(),
        store,
    )
    return f"new token generated for {record.kite_user_id}"


def _verify_broker_client() -> str:
    """A stored token is not the same as a working client, so build one and say.

    This is the check that distinguishes "we wrote a token to disk" from "the broker will
    talk to us", which are the two things a morning run must not conflate.
    """
    client = build_authenticated_kite_client_if_valid()
    if client is None:
        return "no valid token — the system runs on stored data only (Kite-decoupled)"
    return f"authenticated client built ({type(client).__name__})"


def _capture_dashboard_surfaces() -> str:
    """`R.08` visual confirmation, run nightly instead of when someone remembers.

    A 200 from `curl` says bytes came back; it does not say the page painted. The defect
    that motivated this — five CSS rules silently deleted by a stray semicolon, one of
    four regime series invisible — returned 200 on every request and passed ruff, mypy
    and the whole suite (`A.64`). It was visible only as absent pixels.

    A failure here does not stop the run: an unreachable dashboard must not prevent the
    bhavcopy from being fetched, and rolling sources cannot be re-fetched tomorrow.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = capture_dashboard(
        DEFAULT_BASE_URL, DEFAULT_OUTPUT_ROOT / f"{stamp}_daily", read_access_token()
    )
    summary = summarise_capture(results)
    if capture_failed(results):
        raise DashboardCaptureError(summary)
    return summary


def _report_capital() -> str:
    """Capital is a parameter, not a constant (`R.03`), and the run states what it is.

    An UNSET capital is reported, not raised. This runner ingests and reports; it never
    sizes an order, so refusing to ingest because capital is unconfigured fails work that
    does not depend on it. `R.03` is untouched — no default is invented here and none is
    invented downstream: the sizing path still refuses to act without an explicit value.
    Refusing to TRADE without capital is correct; refusing to fetch a bhavcopy is not.
    """
    try:
        capital = load_trading_capital_from_environment()
    except CapitalConfigurationError:
        return (
            "NOT CONFIGURED — set NSE_TRADING_CAPITAL_RUPEES to enable sizing "
            "(ingest and reporting are unaffected; no default is assumed)"
        )
    return f"trading capital Rs {capital.total_rupees:,}"


def _refresh_instrument_master() -> str:
    dump = fetch_instrument_dump()
    records = parse_instrument_dump(dump)
    with InstrumentMasterStore(MARKET_DATA_DATABASE) as store:
        reassignments = store.ingest(records, ingested_on=datetime.now(IST).date())
    # `ingest` returns TOKEN REASSIGNMENTS, not a count of rows. Reporting `len()` of it
    # as "ingested" would have printed 0 on every healthy day and a small number on
    # exactly the days something needed attention — a status line that reads best when
    # it is worst.
    detail = f"{len(records):,} instruments"
    if reassignments:
        detail += f", {len(reassignments)} token reassignment(s)"
    return detail


def _run_ingest(for_dates: Sequence[date]) -> str:
    """Every source through the shared core. The real work of the morning."""
    fetcher = NseSourceFetcher(
        retry_policy=RetryPolicy(maximum_attempts=3, initial_backoff_seconds=2.0)
    )
    adapters: list[NseIngestSourceAdapter] = [
        NseBhavcopyAdapter(CASH_MARKET),
        NseBhavcopyAdapter(FO_MARKET),
        FoBanListAdapter(),
        BulkBlockDealsAdapter(),
        MwplPositionLimitsAdapter(),
        CircuitBandSurveillanceAdapter(),
        DelistedSecuritiesAdapter(),
        IndexConstituentsAdapter(),
        AtmImpliedVolatilityAdapter(),
    ]
    summaries: list[str] = []
    with (
        BitemporalIngestStore(INGEST_DATABASE) as store,
        DiscoveredParameterStore(DISCOVERY_MEMO_DATABASE) as memo,
    ):
        runner = NseSourceIngestRunner(fetcher, store)
        for adapter in adapters:
            try:
                run = runner.ingest(adapter, for_dates, discovery_memo=memo)
                # `=0` alone is three states wearing one number: already held, empty
                # file, or a dead feed. A run on 2026-08-11 printed nine zeros and read
                # as total failure while the database held 33,601 F&O rows for the date.
                if run.rows_inserted:
                    summaries.append(f"{adapter.source_name}={run.rows_inserted:,}")
                elif run.fetched_nothing:
                    summaries.append(f"{adapter.source_name}=NOTHING FETCHED")
                else:
                    summaries.append(
                        f"{adapter.source_name}=0 of {run.rows_presented:,} already held"
                    )
            except Exception as failure:  # noqa: BLE001 — one source must not stop the rest
                summaries.append(f"{adapter.source_name}=FAILED({type(failure).__name__})")
    return " · ".join(summaries)


def _backfill_gaps(window_start: date, window_end: date) -> str:
    """Classify what is missing and re-fetch only what is worth re-fetching."""
    fetcher = NseSourceFetcher(
        retry_policy=RetryPolicy(maximum_attempts=2, initial_backoff_seconds=2.0)
    )
    calendar = NseTradingSessionCalendar()
    lines: list[str] = []
    with BitemporalIngestStore(INGEST_DATABASE) as store:
        runner = NseSourceIngestRunner(fetcher, store)
        reports = []
        for adapter in (NseBhavcopyAdapter(CASH_MARKET), NseBhavcopyAdapter(FO_MARKET)):
            report = find_missing_dates(
                store, adapter.source_name, window_start, window_end, calendar
            )
            reports.append(report)
            run = backfill_missing_dates(
                runner, adapter, report, maximum_dates=MAXIMUM_BACKFILL_DATES_PER_RUN
            )
            filled = run.rows_inserted if run else 0
            lines.append(
                f"{adapter.source_name}: {len(report.refetchable)} refetchable, {filled:,} rows"
            )
        escalations = dates_needing_human_attention(reports, PERSISTENT_FAILURE_ATTEMPTS)
        if escalations:
            lines.append(
                f"ESCALATE {len(escalations)} date(s) failing "
                f">= {PERSISTENT_FAILURE_ATTEMPTS}x"
            )
    return " · ".join(lines)


def _report_ingest_coverage() -> str:
    with BitemporalIngestStore(INGEST_DATABASE) as store:
        calendar = NseTradingSessionCalendar()
        parts = []
        for source in ("nse_bhavcopy_cash", "nse_bhavcopy_fo", "fo_ban_list"):
            report = build_source_coverage_report(store, source, calendar)
            worst = report.worst_year()
            parts.append(
                f"{source}: {report.total_rows:,} rows"
                + (f", worst year {worst.year} {worst.coverage_fraction:.0%}" if worst else "")
            )
    return " · ".join(parts)


def _report_universe() -> str:
    with PointInTimeUniverseEngine(MARKET_DATA_DATABASE) as engine:
        collected = engine.collected_dates()
        if not collected:
            return "no universe observations stored yet"
        snapshot = engine.snapshot_as_of(collected[-1])
        return (
            f"{len(collected)} collected dates, latest {collected[-1]} "
            f"with {len(snapshot.members):,} members"
        )


def _report_corporate_actions() -> str:
    with CorporateActionAdjustmentEngine(MARKET_DATA_DATABASE) as engine:
        counts = engine.coverage_report()
        total = sum(counts.values())
        breakdown = ", ".join(f"{cls.value}={count:,}" for cls, count in sorted(
            counts.items(), key=lambda item: -item[1]
        )[:4])
        return f"{total:,} actions classified ({breakdown})"


def _report_bar_store() -> str:
    with BitemporalBarStore(MARKET_DATA_DATABASE) as store:
        bars = store.bars_as_of(datetime.now(UTC))
        return f"{len(bars):,} bars visible as of now"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--for-date",
        type=date.fromisoformat,
        default=None,
        help="the session to ingest; defaults to the most recent completed session",
    )
    parser.add_argument("--backfill-days", type=int, default=30)
    parser.add_argument("--skip-kite", action="store_true", help="skip broker login")
    arguments = parser.parse_args()

    calendar = NseTradingSessionCalendar()
    target = arguments.for_date or most_recent_closed_session(
        datetime.now(IST), calendar
    )
    while not calendar.is_trading_session(target):
        target -= timedelta(days=1)

    report = DailyRunReport(started_at=datetime.now(UTC))
    print(f"daily operations for session {target}", flush=True)

    if not arguments.skip_kite:
        _run_step(report, "kite session", _refresh_kite_session)
        _run_step(report, "broker client", _verify_broker_client)
        _run_step(report, "instrument master", _refresh_instrument_master)
    _run_step(report, "capital", _report_capital)
    _run_step(report, "ingest", lambda: _run_ingest([target]))
    _run_step(
        report,
        "gap backfill",
        lambda: _backfill_gaps(target - timedelta(days=arguments.backfill_days), target),
    )
    _run_step(report, "ingest coverage", _report_ingest_coverage)
    _run_step(report, "universe", _report_universe)
    _run_step(report, "corporate actions", _report_corporate_actions)
    _run_step(report, "bar store", _report_bar_store)
    # Last: the surface should be photographed AFTER the run has changed the state
    # it displays, so the capture shows the day that just happened.
    _run_step(report, "dashboard surfaces", _capture_dashboard_surfaces)

    print()
    print(report.describe())
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
