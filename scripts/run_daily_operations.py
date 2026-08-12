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
import os
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
from nse_algo_trader.broker_sessions.angel_one_session_store import (
    AngelOneSessionError,
    AngelOneSessionFileStore,
    session_record_from_login,
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
from nse_algo_trader.broker_symbology.angel_one_symbology_resolver import (
    AngelOneSymbologyResolver,
)
from nse_algo_trader.broker_symbology.broker_symbology_resolver import (
    BrokerSymbolStore,
)
from nse_algo_trader.capital_configuration import (
    CapitalConfigurationError,
    load_trading_capital_from_environment,
)
from nse_algo_trader.causal_leakage_firewall import (
    ObservableRow,
    derive_publication_lags,
)
from nse_algo_trader.clock_integrity.clock_integrity_session_runner import (
    ClockIntegritySessionRunner,
)
from nse_algo_trader.consolidated_feed.consolidated_feed_session_runner import (
    ConsolidatedFeedSessionRunner,
)
from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import CrossBrokerQuoteTape
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
from nse_algo_trader.historical_bars.angel_one_historical_bar_source import (
    AngelOneHistoricalBarSource,
)
from nse_algo_trader.historical_bars.cross_source_bar_reconciler import (
    ReconciliationReport,
    fetch_and_reconcile,
)
from nse_algo_trader.historical_bars.historical_bar_source import (
    BarInstrument,
    BarInterval,
    BarRequest,
    HistoricalBarSource,
    HistoricalBarSourceError,
)
from nse_algo_trader.historical_bars.kite_historical_bar_source import (
    KiteHistoricalBarSource,
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
from nse_algo_trader.replay_session_clock import replay_sessions
from nse_algo_trader.security_identity_record_store import (
    SecurityIdentityRecordStore,
    observations_from_bhavcopy_rows,
)

IST = ZoneInfo("Asia/Kolkata")
STATE_DIRECTORY = Path("~/.nse_algo_trader").expanduser()
INGEST_DATABASE = STATE_DIRECTORY / "nse_ingest.sqlite3"
MARKET_DATA_DATABASE = STATE_DIRECTORY / "market_data.sqlite3"
BROKER_SYMBOLOGY_DATABASE = STATE_DIRECTORY / "broker_symbology.sqlite3"
SECURITY_IDENTITY_DATABASE = STATE_DIRECTORY / "security_identity.sqlite3"
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


def _refresh_security_identity() -> str:
    """`L0.08` — keep the ISIN-to-symbol history current, so a symbol is never an identity.

    Reads the cash bhavcopy rows already in the ingest store rather than re-fetching: the
    identity history IS the accumulated bhavcopy, and re-downloading it to learn something
    already held would be a second request against a host that bot-walls.
    """
    with (
        BitemporalIngestStore(INGEST_DATABASE) as ingest,
        SecurityIdentityRecordStore(SECURITY_IDENTITY_DATABASE) as identities,
    ):
        # Every date present, not just today's: a rename is only visible ACROSS dates,
        # and the store is append-only so re-reading history is idempotent.
        rows = [
            (row.effective_date.isoformat(), row.values)
            for effective_date in ingest.effective_dates_present("nse_bhavcopy_cash")
            for row in ingest.rows_for("nse_bhavcopy_cash", effective_date)
        ]
        recorded = identities.record(observations_from_bhavcopy_rows(rows))
        return f"{recorded:,} new * {identities.describe()}"


def _report_publication_schedules() -> str:
    """`L0.11` — re-derive every source's publication lag and surface anything unknowable.

    Run daily because a publication schedule is a fact about NSE that can CHANGE, and the
    firewall's guarantee is only as good as the lag it was derived from. A source that
    starts publishing a day later would silently begin admitting rows a trader could not
    have held, and nothing else in the system would notice.
    """
    with BitemporalIngestStore(INGEST_DATABASE) as store:
        triples = store.publication_observations()
    lags = derive_publication_lags(triples)
    unknown = sorted(name for name, lag in lags.items() if not lag.is_derivable)
    known = ", ".join(
        f"{name}={lag.days}d" for name, lag in sorted(lags.items()) if lag.is_derivable
    )
    detail = f"{len(lags)} sources * {known}"
    if unknown:
        # Not a failure: a static historical master legitimately has no schedule. It is
        # surfaced because the firewall BLOCKS these by default, so a silent one would
        # look like a source that simply had no data.
        detail += f" * UNKNOWN (blocked in replay): {', '.join(unknown)}"
    return detail


def _verify_replay_leakage_guard(target_session: date) -> str:
    """`L0.13` — replay the session just closed and prove the leakage guard still holds.

    Runs daily because the guarantee is only as good as the derived publication lags, and
    those come from a corpus that grows every night. A source that starts publishing later
    would silently begin admitting rows a trader could not have held; replaying a real
    session against the real corpus is what would catch it.

    The assertion is directional, not a fixed count: replaying a PAST session must block
    strictly more than replaying the newest data, because the future has not happened yet.
    A guard that stopped blocking anything would satisfy every unit test and be useless.
    """
    with BitemporalIngestStore(INGEST_DATABASE) as store:
        triples = store.publication_observations()
    lags = derive_publication_lags(triples)
    rows = [ObservableRow(source, effective, observed) for source, effective, observed in triples]

    clocks = list(
        replay_sessions(NseTradingSessionCalendar(), lags, target_session, target_session)
    )
    if not clocks:
        return f"{target_session} is not a trading session — nothing to replay"
    clock = clocks[0]
    clock.observable_now(rows)
    ledger = clock.firewall.ledger
    if ledger.total_blocked == 0:
        raise RuntimeError(
            f"replaying {target_session} blocked NOTHING across {len(rows):,} rows — "
            "the leakage guard is not guarding"
        )
    return f"replayed {target_session} over {len(rows):,} rows * {ledger.describe()}"


KITE_HISTORICAL_REQUESTS_PER_SECOND = 3
"""Kite's documented historical-data rate limit. A published API fact, sourced rather than
tuned — and confirmed the hard way: an unpaced run of 25 instruments was refused with
"Too many requests" after the first few."""

THROTTLE_BACKOFF_SECONDS = 2.0
"""Waited once on a throttle before giving that instrument up for the night. Rotation
means a deferred instrument is first in line tomorrow, so grinding against a rate limit
buys nothing and risks the credentials the rest of the run depends on."""

DAILY_BAR_INSTRUMENT_BUDGET = 25
"""Instruments reconciled per run. A RATE budget, not a sample (`R.09`).

Both live brokers throttle, and the universe is thousands of instruments: fetching all of
them nightly would be blocked within minutes and would poison the credentials the rest of
the run depends on. The universe is covered by ROTATION — least-recently-updated first —
so coverage grows every night and no instrument is permanently skipped. What was deferred
is reported, never silently dropped (`R.11`).
"""


def _refresh_broker_symbology() -> str:
    """`L0.17` — re-read each broker's published master so the reconciler can name things.

    Daily because listings change: a newly listed symbol the reconciler cannot name is
    silently single-sourced, which looks exactly like agreement.
    """
    with BrokerSymbolStore(BROKER_SYMBOLOGY_DATABASE) as store:
        resolver = AngelOneSymbologyResolver(store)
        mapped = resolver.refresh()
        return (
            f"angel_one {mapped:,} NSE equity mappings "
            f"(refreshed {store.last_refreshed(BrokerName.ANGEL_ONE)})"
        )


def _build_angel_one_client() -> object | None:
    """An authenticated SmartAPI client, or None when Angel will not talk to us.

    Tries the CACHED session first, so a run that wants a client more than once logs in
    once. (An earlier version of this docstring claimed Angel throttles repeated logins.
    That was WRONG — the repeated failures were this function being called before `.env`
    was loaded, and a direct login succeeded immediately once that was ruled out. Caching
    is still right, because a login per component is waste, but it is not a workaround for
    a limit Angel was never imposing.)

    None rather than raising: Angel is a SECOND source, so failing to reach it must
    degrade the run to single-source reconciliation rather than stop it. The reconciler
    records the absence, so a broken broker cannot be mistaken for one with no data.
    """
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError:
        return None
    # Load credentials here rather than relying on an earlier step having done it. This
    # function returning None is ambiguous by design — "Angel will not talk to us" — and
    # an unloaded .env produced exactly that answer for a reason that had nothing to do
    # with Angel. It cost a wrong conclusion: repeated Nones were read as Angel throttling
    # logins when the real cause was this function being called before the environment was
    # populated. `load_env_file_into_environ` is idempotent, so calling it is free.
    load_env_file_into_environ()
    if "ANGEL_ONE_API_KEY" not in os.environ:
        return None

    api_key = os.environ["ANGEL_ONE_API_KEY"]
    store = AngelOneSessionFileStore()
    try:
        cached = store.load_for_today()
    except AngelOneSessionError:
        cached = None  # A corrupt cache is replaced by a fresh login, not a dead run.
    if cached is not None:
        rehydrated: object = SmartConnect(
            api_key=api_key,
            access_token=cached.jwt_token,
            refresh_token=cached.refresh_token,
            feed_token=cached.feed_token,
        )
        return rehydrated

    required = ("ANGEL_ONE_CLIENT_CODE", "ANGEL_ONE_PIN", "ANGEL_ONE_TOTP_SECRET")
    if any(name not in os.environ for name in required):
        return None
    try:
        client = SmartConnect(api_key=api_key)
        login = client.generateSession(
            os.environ["ANGEL_ONE_CLIENT_CODE"],
            os.environ["ANGEL_ONE_PIN"],
            pyotp.TOTP(os.environ["ANGEL_ONE_TOTP_SECRET"]).now(),
        )
        record = session_record_from_login(login)
    # `AngelOneSessionError` (a refused or malformed login) is deliberately inside this
    # net rather than beside it: every path here means "no Angel client", and separating
    # them would imply a caller that can act on the difference. None is that answer.
    except Exception:  # noqa: BLE001 — SmartAPI raises assorted transport types
        return None
    store.store(record)
    authenticated: object = client
    return authenticated


def _fetch_with_throttle_backoff(
    sources: list[HistoricalBarSource], request: BarRequest
) -> ReconciliationReport:
    """Reconcile, retrying ONCE if the brokers throttled us.

    One retry, not a loop: a throttle means the budget for this second is spent, and
    hammering it is how an API key gets suspended. Rotation puts a deferred instrument
    first in line tomorrow, so the cost of giving up is one night of staleness.
    """
    try:
        return fetch_and_reconcile(sources, request)
    except HistoricalBarSourceError as failure:
        if "too many requests" not in str(failure).lower():
            raise
        time.sleep(THROTTLE_BACKOFF_SECONDS)
        return fetch_and_reconcile(sources, request)


def _reconcile_daily_bars(target_session: date) -> str:
    """`L0.14`/`L0.15` — fetch bars from every live broker, reconcile, and store.

    This is the step that fills a store every previous run reported as empty. Bars are
    written with the reconciled value and the disagreements are surfaced, so a price
    disagreement between brokers reaches a human instead of being resolved into silence.
    """
    sources: list[HistoricalBarSource] = []
    failures: list[str] = []
    kite_client = build_authenticated_kite_client_if_valid()
    if kite_client is None:
        failures.append("kite has no valid token")
    else:
        sources.append(KiteHistoricalBarSource(kite_client))
    angel_client = _build_angel_one_client()
    if angel_client is None:
        failures.append("angel one session could not be generated")
    else:
        sources.append(AngelOneHistoricalBarSource(angel_client))
    if not sources:
        raise RuntimeError("no broker could be authenticated: " + "; ".join(failures))

    instruments = _bar_instruments_due(DAILY_BAR_INSTRUMENT_BUDGET)
    if not instruments:
        return "no instruments mapped to broker identifiers yet (L0.17 supplies them)"

    written = 0
    price_disagreements: list[str] = []
    volume_disagreements = 0
    for instrument in instruments:
        request = BarRequest(instrument, BarInterval.ONE_DAY, target_session, target_session)
        try:
            report = _fetch_with_throttle_backoff(sources, request)
        except HistoricalBarSourceError as failure:
            failures.append(f"{instrument.tradingsymbol}: {str(failure)[:90]}")
            continue
        # Pace deliberately rather than sprinting into a refusal.
        time.sleep(1.0 / KITE_HISTORICAL_REQUESTS_PER_SECOND)
        volume_disagreements += len(report.volume_disagreements)
        price_disagreements.extend(
            f"{instrument.tradingsymbol}@{bar.bar.bar_timestamp:%Y-%m-%d}"
            for bar in report.price_disagreements
        )
        with BitemporalBarStore(MARKET_DATA_DATABASE) as store:
            written += store.write([bar.bar for bar in report.bars])

    detail = (
        f"{written:,} bars written from {len(instruments)} instruments "
        f"via {len(sources)} broker(s)"
    )
    if volume_disagreements:
        detail += f" * {volume_disagreements} volume-only disagreements"
    if price_disagreements:
        detail += f" * PRICE DISAGREEMENTS: {', '.join(price_disagreements[:5])}"
    if failures:
        detail += f" * {len(failures)} source issue(s): {failures[0]}"
    return detail


def _bar_instruments_due(budget: int) -> list[BarInstrument]:
    """Instruments to refresh, least-recently-stored first.

    **Which instruments count as tradeable is decided by DATA, not by a name pattern.**
    Kite's `instrument_type` says `EQ` for listed bonds as well as equities, so filtering
    on it selected `0ABCL31-N0` and friends — debt series that return zero bars from every
    broker. The honest discriminator is the cash bhavcopy: a symbol NSE published a cash
    trade for is, by construction, a security that trades. That also keeps this aligned
    with `R.09` — the pool is the whole traded universe, and the budget only paces how
    fast it is walked.

    **Rotation uses STORED bars, not visible ones.** An earlier version compared against
    `bars_as_of(now)`, which is always empty for same-day daily bars, so the same 25
    instruments were re-fetched every night and the universe never advanced — a sample
    wearing a budget's clothes, which is precisely what the budget exists not to be.
    """
    with BitemporalIngestStore(INGEST_DATABASE) as ingest:
        traded_symbols: set[str] = set()
        for effective_date in ingest.effective_dates_present("nse_bhavcopy_cash")[-3:]:
            for row in ingest.rows_for("nse_bhavcopy_cash", effective_date):
                symbol = row.values.get("SYMBOL") or row.values.get("TckrSymb")
                if isinstance(symbol, str) and symbol:
                    traded_symbols.add(symbol.strip())

    with InstrumentMasterStore(MARKET_DATA_DATABASE) as master:
        universe = master.instruments_as_of(datetime.now(IST).date(), exchange="NSE")
    tradeable = [
        record
        for record in universe
        if record.segment == "NSE" and record.tradingsymbol in traded_symbols
    ]
    if not tradeable:
        return []

    # Far-future as_of: what has been STORED, regardless of when it becomes actionable.
    with BitemporalBarStore(MARKET_DATA_DATABASE) as store:
        already_stored = {
            bar.tradingsymbol
            for bar in store.bars_as_of(datetime.now(UTC) + timedelta(days=365))
        }
    never_stored = [r for r in tradeable if r.tradingsymbol not in already_stored]
    chosen = sorted(never_stored or tradeable, key=lambda r: r.tradingsymbol)[:budget]
    # Kite's token comes from the instrument master; every OTHER broker's comes from
    # `L0.17`. Without this the reconciler was handed instruments only Kite could name, so
    # "cross-source reconciliation" was single-source with extra steps.
    with BrokerSymbolStore(BROKER_SYMBOLOGY_DATABASE) as symbology:
        instruments = []
        for record in chosen:
            identifiers = symbology.identifiers_for_all_brokers(record.tradingsymbol)
            identifiers[BrokerName.ZERODHA_KITE] = str(record.instrument_token)
            instruments.append(
                BarInstrument(
                    exchange=record.exchange,
                    segment="CASH",
                    tradingsymbol=record.tradingsymbol,
                    broker_identifiers=identifiers,
                )
            )
    return instruments


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
    return " * ".join(summaries)


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
    return " * ".join(lines)


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
    return " * ".join(parts)


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
    """Visible-now AND stored-total, because they are different facts.

    A daily bar for today becomes actionable tomorrow, so a store holding today's bars
    correctly shows zero visible. Reporting only the visible count made a working store
    indistinguishable from an empty one — measured, after the first reconciliation wrote
    3 bars and this line still read "0 bars visible".
    """
    with BitemporalBarStore(MARKET_DATA_DATABASE) as store:
        visible = store.bars_as_of(datetime.now(UTC))
        stored = store.bars_as_of(datetime.now(UTC) + timedelta(days=365))
    if not stored:
        return "empty"
    return (
        f"{len(visible):,} bars visible as of now * {len(stored):,} stored "
        f"({len(stored) - len(visible):,} not yet actionable)"
    )


def _assess_clock_integrity() -> str:
    """`L0.32`: fit the day's offset and skew, sample the reference, record the verdict.

    Runs on the latest session the depth tape holds rather than on `target`: the tape is
    written by the live recorder, so the newest session it has is the newest measurement
    available, and asking for a session it never captured would report a clock failure
    where there was only a day the recorder did not run.
    """
    result = ClockIntegritySessionRunner().run_latest_session()
    if result is None:
        return "no depth tape sessions — nothing to fit"
    if result.fit is None:
        return f"{result.session_date}: {result.unavailable_reason or 'no fit'}"
    chrony_note = (
        f" * {result.skew_disagreement_with_chrony_ppm:+.2f} ppm from chrony"
        if result.skew_disagreement_with_chrony_ppm is not None
        else ""
    )
    return (
        f"{result.session_date}: {result.assessment.verdict.value} * "
        f"offset ≤ {result.fit.apparent_offset_seconds * 1000:.1f}ms * "
        f"skew {result.fit.skew_ppm:+.2f} ppm{chrony_note} * "
        f"{len(result.alerts)} change point(s) * "
        f"worst case {result.assessment.worst_case_error_seconds * 1000:.1f}ms"
    )


def _consolidate_broker_feeds() -> str:
    """`L0.33`: fuse the day's cross-broker quotes, learn from them, store the summary.

    Runs on the newest session the quote tape holds rather than on `target`, for the same
    reason the clock step does: the tape is written by a live capture, so its newest session
    is the newest measurement, and asking for a day it never captured would report a feed
    failure where there was only a capture that did not run.
    """
    runner = ConsolidatedFeedSessionRunner()
    sessions = CrossBrokerQuoteTape().session_dates()
    if not sessions:
        return "no cross-broker capture yet — nothing to consolidate"
    report = runner.run(sessions[-1])
    if not report.quotes_consolidated:
        return f"{report.session_date}: capture present but no alignable groups"
    return (
        f"{report.session_date}: {report.quotes_consolidated:,} groups from "
        f"{', '.join(report.brokers)} · {report.resolved_fraction:.1%} resolved · "
        f"{report.crossed:,} crossed · {report.single_source:,} single-source · "
        f"{report.inadmissible_instrument_sessions} inadmissible instrument-session(s) · "
        f"worst {report.worst_instrument}"
    )


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
    _run_step(report, "security identity", _refresh_security_identity)
    _run_step(report, "publication schedules", _report_publication_schedules)
    _run_step(
        report, "replay leakage guard", lambda: _verify_replay_leakage_guard(target)
    )
    _run_step(report, "universe", _report_universe)
    _run_step(report, "corporate actions", _report_corporate_actions)
    _run_step(report, "broker symbology", _refresh_broker_symbology)
    _run_step(report, "bar reconciliation", lambda: _reconcile_daily_bars(target))
    _run_step(report, "bar store", _report_bar_store)
    _run_step(report, "clock integrity", _assess_clock_integrity)
    _run_step(report, "consolidated feed", _consolidate_broker_feeds)
    # Last: the surface should be photographed AFTER the run has changed the state
    # it displays, so the capture shows the day that just happened.
    _run_step(report, "dashboard surfaces", _capture_dashboard_surfaces)

    print()
    print(report.describe())
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
