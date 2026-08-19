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
from datetime import datetime, timedelta
from pathlib import Path

from nse_algo_trader.paper_loop.continuous_paper_trading_scheduler import (
    ContinuousPaperTradingScheduler,
    DepthTapeObservationSource,
    SchedulerLivenessStore,
)
from nse_algo_trader.replay_session_clock import IST
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    REVERSION_WINDOW_IN_FIVE_MINUTE_BARS,
)
from nse_algo_trader.segment_bots.segment_bot_registry import build_all_segment_bots
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.segment_bots.segment_universe_assembler import assemble_for

DEFAULT_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()
DEFAULT_LIVENESS = Path("~/.nse_algo_trader/scheduler_liveness.sqlite3").expanduser()

FIVE_MINUTE_BAR_SECONDS = 5 * 60
"""The cash strategy's own bar. A property of the fitted engine, not a scheduling preference —
`REVERSION_WINDOW_IN_FIVE_MINUTE_BARS` counts bars of exactly this length."""

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
    universe = assemble_for(TradingSegment.CASH_INTRADAY, as_of=now, limit=None)
    scheduler = ContinuousPaperTradingScheduler(
        bots=build_all_segment_bots(),
        observations=DepthTapeObservationSource(arguments.tape_root),
        liveness=SchedulerLivenessStore(arguments.liveness),
        universe=universe.instruments,
    )
    print(
        f"continuous loop up: {len(universe.instruments):,} cash instruments, "
        f"{len(scheduler.bots)} bot(s), cadence {arguments.cadence_seconds:.0f}s "
        f"({REVERSION_WINDOW_IN_FIVE_MINUTE_BARS}-bar reversion window)",
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
            # is the entire point of this process.
            refreshed = assemble_for(TradingSegment.CASH_INTRADAY, as_of=instant, limit=None)
            scheduler.replace_universe(refreshed.instruments)
            universe_refreshed_at = instant
            print(f"universe refreshed: {len(refreshed.instruments):,} instruments", flush=True)
        _sleep(arguments.cadence_seconds)
    return 0


def _sleep(seconds: float) -> None:
    """Isolated so the loop body stays testable and the sleep has exactly one call site."""
    import time

    time.sleep(seconds)


if __name__ == "__main__":
    raise SystemExit(main())
