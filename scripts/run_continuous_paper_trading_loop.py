#!/usr/bin/env python
"""`L10.01`'s entry point — the loop that never stops. Spec `docs/research/265`, todo `4.9`.

Runs under `nse-continuous-loop.service`. It does NOT decide when to run: systemd starts it and it
stays up, changing what it does as the exchange calendar moves through the day. That is the
difference between this and the daily timer — a timer fires and exits, and between two firings the
system is blind, which is exactly what the operator saw on 2026-08-18 with a live tape on disk and a
dashboard showing yesterday.

**The cadence is derived, not chosen** (`R.03`): the cash bot bands on five-minute bars, so deciding
faster re-reads a book that has not moved and deciding slower drops bars the engine was fitted on.
It is imported from the strategy rather than typed here.

Usage:
    python scripts/run_continuous_paper_trading_loop.py
    python scripts/run_continuous_paper_trading_loop.py --iterations 5   # a bounded probe
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from nse_algo_trader.nse_ingest.derivative_contract_record_projection import (
    DerivativeContractRecordProjection,
)
from nse_algo_trader.paper_loop.bot_maturity_ladder import PaperTrackRecordStore
from nse_algo_trader.paper_loop.continuous_paper_trading_scheduler import (
    ContinuousPaperTradingScheduler,
    DepthTapeObservationSource,
    SchedulerLivenessStore,
)
from nse_algo_trader.paper_loop.segment_bot_paper_session import (
    WARMUP_SESSIONS,
    SegmentBotPaperSession,
)
from nse_algo_trader.paper_loop.segment_bot_warm_start_seeding import (
    WarmStartSeedingOutcome,
    seed_bots_from_prior_instants,
    seed_bots_from_prior_sessions,
)
from nse_algo_trader.paper_loop.walk_forward_archive_replay import (
    WalkForwardArchiveReplay,
    WalkForwardReplayCursorStore,
)
from nse_algo_trader.replay_session_clock import IST
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    REVERSION_WINDOW_IN_FIVE_MINUTE_BARS,
)
from nse_algo_trader.segment_bots.segment_bot_registry import build_all_segment_bots
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.segment_bots.segment_universe_assembler import (
    AssembledSegmentUniverse,
    assemble_for,
)
from nse_algo_trader.sizing.futures_margin_estimator import margin_estimator_for
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    MINIMUM_OBSERVATIONS_FOR_BANDS,
)

DEFAULT_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
DEFAULT_LIVENESS = Path("~/.nse_algo_trader/scheduler_liveness.sqlite3").expanduser()

FIVE_MINUTE_BAR_SECONDS = 5 * 60
"""The cash strategy's own bar. A property of the fitted engine, not a scheduling preference —
`REVERSION_WINDOW_IN_FIVE_MINUTE_BARS` counts bars of exactly this length."""

BARS_BEFORE_A_CASH_ENGINE_CAN_BAND = (
    REVERSION_WINDOW_IN_FIVE_MINUTE_BARS + MINIMUM_OBSERVATIONS_FOR_BANDS
)
"""`B43`'s arithmetic, imported rather than typed (`R.03`).

The engine fills a 20-bar rolling window before it counts a single deviation, then needs 60 of
them — 80 bars against a 75-bar NSE session, so a cold process can never band inside one session
and the seed depth has to be at least this deep to be worth taking.
"""

UNIVERSE_REFRESH = timedelta(hours=1)
"""How often the tradeable set is re-assembled.

The universe is the instrument MASTER's answer and it changes at most once a session; the tape
supplies prices continuously. Re-assembling every tick would run a 3,835-row join every five
minutes to learn nothing.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_TAPE_ROOT)
    parser.add_argument("--liveness", type=Path, default=DEFAULT_LIVENESS)
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop after this many ticks — a bounded probe, not the service mode",
    )
    parser.add_argument(
        "--cadence-seconds",
        type=float,
        default=float(FIVE_MINUTE_BAR_SECONDS),
        help="derived default: the strategy's own bar interval",
    )
    arguments = parser.parse_args()

    now = datetime.now(IST)
    _project_derivative_contracts()
    universe_by_segment = _assemble_every_segment(now)
    bots = build_all_segment_bots()
    scheduler = ContinuousPaperTradingScheduler(
        bots=bots,
        observations=DepthTapeObservationSource(arguments.tape_root),
        liveness=SchedulerLivenessStore(arguments.liveness),
        universe=universe_by_segment[TradingSegment.CASH_INTRADAY].instruments,
        universe_by_segment={
            segment: assembled.instruments for segment, assembled in universe_by_segment.items()
        },
        archive_replay=_build_archive_replay(bots),
    )
    for segment, assembled in universe_by_segment.items():
        scheduler.replace_segment_universe(
            segment,
            assembled.instruments,
            assembled.last_price_paise_by_token,
            assembled.underlying_price_paise_by_symbol,
        )
    seeding = _seed_the_bots(bots, now)
    print(f"warm start: {seeding.describe()}", flush=True)
    sizes = " · ".join(
        f"{segment.value} {len(assembled.instruments):,}"
        for segment, assembled in sorted(universe_by_segment.items(), key=lambda i: i[0].value)
    )
    print(
        f"continuous loop up: {len(scheduler.bots)} bot(s), cadence "
        f"{arguments.cadence_seconds:.0f}s ({REVERSION_WINDOW_IN_FIVE_MINUTE_BARS}-bar reversion "
        f"window) · universes: {sizes}",
        flush=True,
    )

    ticks = 0
    universe_refreshed_at = now
    while arguments.iterations is None or ticks < arguments.iterations:
        instant = datetime.now(IST)
        iteration = scheduler.step(instant)
        print(iteration.describe(), flush=True)
        ticks += 1
        if arguments.iterations is not None and ticks >= arguments.iterations:
            break
        if instant - universe_refreshed_at >= UNIVERSE_REFRESH:
            # Re-assembled rather than rebuilt: a NEW scheduler would discard the carried state that
            # is the entire point of this process. The projection runs first because a universe
            # assembled from a stale contract table is how five bots spent sixteen days deciding
            # over contracts that had already settled.
            _project_derivative_contracts()
            for segment, assembled in _assemble_every_segment(instant).items():
                scheduler.replace_segment_universe(
                    segment,
                    assembled.instruments,
                    assembled.last_price_paise_by_token,
                    assembled.underlying_price_paise_by_symbol,
                )
            universe_refreshed_at = instant
            print(
                "universes refreshed: "
                + " · ".join(
                    f"{segment.value} {len(scheduler.universe_for(segment)):,}"
                    for segment in TradingSegment
                ),
                flush=True,
            )
        _sleep(arguments.cadence_seconds)
    return 0


def _project_derivative_contracts() -> None:
    """Bring the F&O contract table up to what the ingest store holds, before assembling from it.

    Cheap by construction: a session whose observations have not changed writes nothing, so this
    costs one cursor read on every tick where nothing new has landed.
    """
    try:
        with DerivativeContractRecordProjection() as projection:
            outcome = projection.project()
        if outcome.sessions_read:
            print(f"derivative contracts: {outcome.describe()}", flush=True)
    except Exception as failure:  # noqa: BLE001 — a stale universe still trades; a dead loop does not
        print(f"derivative projection FAILED: {type(failure).__name__}: {failure}", flush=True)


def _assemble_every_segment(as_of: datetime) -> dict[TradingSegment, AssembledSegmentUniverse]:
    """One universe per segment, so each bot decides over instruments it can actually trade.

    The whole assembled object is returned rather than just the instruments, because a derivative
    universe carries its own last prices and underlying spots and a bot handed instruments without
    prices proposes nothing while looking healthy.
    """
    universes: dict[TradingSegment, AssembledSegmentUniverse] = {}
    for segment in TradingSegment:
        try:
            universes[segment] = assemble_for(segment, as_of=as_of, limit=None)
        except Exception as failure:  # noqa: BLE001 — one segment's store must not stop the rest
            print(
                f"{segment.value} universe FAILED: {type(failure).__name__}: {failure}",
                flush=True,
            )
            universes[segment] = AssembledSegmentUniverse(
                segment, (), {}, {}, {}, None, f"assembly failed: {failure}"
            )
    return universes


def _build_archive_replay(bots: Sequence[object]) -> WalkForwardArchiveReplay:
    """The closed-market walk (`A.146`), driven by the real six-bot paper session.

    `SegmentBotPaperSession` is the engine that was built for `6.4d` and then imported by nothing.
    This is the consumer it was missing — not a second implementation of it.
    """
    market_data = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
    today = datetime.now(IST).date()
    session = SegmentBotPaperSession(
        bots=bots,  # type: ignore[arg-type]
        track_record=PaperTrackRecordStore(),
        margin_estimator=margin_estimator_for(
            volatility_file=_latest_published_volatility_file(),
            market_data=market_data,
            as_of=today,
        ),
    )
    return WalkForwardArchiveReplay(
        session_runner=session,
        replayable_sessions=_replayable_archive_sessions,
        cursor_store=WalkForwardReplayCursorStore(),
    )


def _seed_the_bots(bots: Sequence[object], now: datetime) -> WarmStartSeedingOutcome:
    """Warm every bot before the first tick, each on its own clock (`B43`).

    The cash bot is seeded on FIVE-MINUTE instants because that is the clock its bands are fitted
    on; the five derivative bots are seeded on session CLOSES because that is the cadence their data
    supports. Mixing the two inside one rolling window would build a dispersion from observations
    spaced a day apart and observations spaced five minutes apart, which is `A.106`'s defect.
    """
    sessions = _replayable_archive_sessions(now.date())
    cash_bots = [
        bot
        for bot in bots
        if getattr(bot, "trading_segment", None) is TradingSegment.CASH_INTRADAY
    ]
    daily_bots = [bot for bot in bots if bot not in cash_bots]

    observations: dict[str, int] = {}
    failures: dict[str, str] = {}
    empty = 0
    offered = 0

    if daily_bots and sessions:
        daily = seed_bots_from_prior_sessions(
            daily_bots,  # type: ignore[arg-type]
            sessions=sessions,
            universe_for=_universe_at,
            maximum_sessions=WARMUP_SESSIONS,
        )
        observations |= dict(daily.observations_by_bot)
        failures |= dict(daily.failures_by_bot)
        empty += daily.empty_sessions
        offered = daily.sessions_offered

    if cash_bots:
        instants = _recent_five_minute_instants(now, sessions)
        if instants:
            cash = seed_bots_from_prior_instants(
                cash_bots,  # type: ignore[arg-type]
                instants=instants,
                universe_for=_universe_at,
                maximum_observations=BARS_BEFORE_A_CASH_ENGINE_CAN_BAND,
            )
            observations |= dict(cash.observations_by_bot)
            failures |= dict(cash.failures_by_bot)
            empty += cash.empty_sessions
            offered = max(offered, cash.sessions_offered)

    return WarmStartSeedingOutcome(
        observations_by_bot=observations,
        empty_sessions=empty,
        failures_by_bot=failures,
        sessions_offered=offered,
    )


def _universe_at(segment: TradingSegment, instant: datetime) -> AssembledSegmentUniverse:
    return assemble_for(segment, as_of=instant, limit=None)


def _recent_five_minute_instants(
    now: datetime, sessions: Sequence[date]
) -> tuple[datetime, ...]:
    """The last `BARS_BEFORE_A_CASH_ENGINE_CAN_BAND` five-minute bar closes actually in the store.

    Read from `price_bars` rather than generated from a clock: a generated ladder walks straight
    through lunch-less NSE sessions, holidays and gaps in the tape, and every instant that lands on
    nothing is an observation the engine does not get.
    """
    del sessions
    market_data = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
    if not market_data.exists():
        return ()
    connection = sqlite3.connect(f"file:{market_data}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT bar_timestamp FROM price_bars "
            "WHERE bar_timestamp <= ? AND availability_time <= ? "
            "ORDER BY bar_timestamp DESC LIMIT ?",
            (now.isoformat(), now.isoformat(), BARS_BEFORE_A_CASH_ENGINE_CAN_BAND),
        ).fetchall()
    finally:
        connection.close()
    instants: list[datetime] = []
    for (raw,) in rows:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        instants.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=IST))
    return tuple(sorted(instants))


def _latest_published_volatility_file() -> Path | None:
    """NSE's own `CMVOLT` file, newest first. `None` leaves the estimator on computed index sigmas.

    Not fatal when absent: `margin_estimator_for` still returns a working estimator for the index
    products, and a stock future with no published sigma is REFUSED rather than sized on a guess.
    """
    state = Path("~/.nse_algo_trader").expanduser()
    files = sorted(state.glob("CMVOLT_*.CSV"))
    return files[-1] if files else None


def _replayable_archive_sessions(as_of: date) -> tuple[date, ...]:
    """Every session the stores can support an entry on, oldest first.

    A session qualifies when the cash bar store holds bars for it — that is the store every bot's
    warm-up reads, and a session with no bars produces a universe of nothing rather than a decision.
    The F&O contract table is deliberately NOT required: a session where only cash has data is still
    a session the cash bot can be judged on, and demanding both would silently skip it.
    """
    market_data = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
    if not market_data.exists():
        return ()
    connection = sqlite3.connect(f"file:{market_data}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT DATE(bar_timestamp) FROM price_bars "
            "WHERE DATE(bar_timestamp) < ? AND availability_time <= ? ORDER BY 1",
            (as_of.isoformat(), as_of.isoformat()),
        ).fetchall()
    finally:
        connection.close()
    return tuple(date.fromisoformat(row[0]) for row in rows if row[0])


def _sleep(seconds: float) -> None:
    """Isolated so the loop body stays testable and the sleep has exactly one call site."""
    import time

    time.sleep(seconds)


if __name__ == "__main__":
    raise SystemExit(main())
