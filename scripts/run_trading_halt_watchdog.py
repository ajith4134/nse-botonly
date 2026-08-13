"""Run the trading halt watchdog — the process whose only job is to be able to stop the trader.

`L7.10`. This is the entry point for `deploy/nse-trading-halt-watchdog.service`, a systemd **user**
unit with `Restart=always`. It must be a separate process from the trader for the reason
`docs/research/221` §9 gives: a trader that is wedged or looping cannot stop itself, so the thing
that stops it cannot share its interpreter, its GIL, its event loop or its fate.

Four modes, one binary:

    --supervise            the service mode: reconcile on start, then poll forever
    --once                 a single supervision cycle, exit non-zero if trading is halted
    --status               print the latch, the heartbeat evidence and the derived tolerance
    --operator-halt        stop trading now, on a human's say-so, no evidence required
    --operator-release     clear the halt — needs the arming key AND the typed phrase (R.22)

On start, in every mode that supervises, the watchdog RECONCILES rather than resumes: it re-reads
the durable latch and the heartbeat history and decides from that evidence alone. It never restores
what it believed before it died, and it can only ever close the latch — opening it is an operator
act that this program can carry but not authorise.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nse_algo_trader.order_path.trading_control_latch import (
    DEFAULT_OPERATOR_ARMING_KEY_PATH,
    DEFAULT_TRADING_CONTROL_LATCH_PATH,
    OperatorAuthorityScope,
    TradingControlError,
    TradingControlLatchStore,
    authorize_operator_action,
)
from nse_algo_trader.order_path.trading_halt_watchdog import (
    DEFAULT_HEARTBEAT_HISTORY_WINDOW,
    DEFAULT_TRADER_HEARTBEAT_PATH,
    TraderHeartbeatJournal,
    TradingHaltWatchdog,
    WatchdogSupervisionCycle,
    supervise_until_stopped,
)

# The ONE number a human supplies. It is used only while the heartbeat journal holds too little
# history to derive a tolerance from data (R.03); from the second beat onward the poll interval is
# half the derived staleness tolerance and this value stops being consulted.
_DEFAULT_BOOTSTRAP_POLL_SECONDS = 1.0


def _print_cycle(cycle: WatchdogSupervisionCycle) -> None:
    print(cycle.describe(), flush=True)
    if cycle.did_halt_trading:
        print(
            f"!! TRADING HALTED by the watchdog: {cycle.disposition_after.reason}",
            flush=True,
        )


def _build_watchdog(arguments: argparse.Namespace) -> TradingHaltWatchdog:
    return TradingHaltWatchdog(
        latch_store=TradingControlLatchStore(database_path=arguments.latch_database),
        heartbeat_journal=TraderHeartbeatJournal(database_path=arguments.heartbeat_database),
        monitored_component=arguments.component,
        heartbeat_history_window=arguments.heartbeat_history_window,
    )


def _report_status(watchdog: TradingHaltWatchdog) -> int:
    disposition = watchdog.latch_store.read_disposition()
    assessment = watchdog.assess_heartbeat_freshness()
    print(f"latch      : {disposition.describe()}")
    chain_state = "verified" if watchdog.latch_store.verify_transition_chain() else "BROKEN"
    print(f"chain      : {chain_state}")
    print(f"heartbeat  : {assessment.verdict.value} — {assessment.detail}")
    if assessment.tolerance is not None:
        print(f"tolerance  : {assessment.tolerance.describe()}")
    print(f"submission : {'permitted' if disposition.is_submission_permitted else 'REFUSED'}")
    return 0 if disposition.is_submission_permitted else 1


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_trading_halt_watchdog",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--component",
        default="order_path_trader",
        help="the component whose heartbeat is being watched (default: %(default)s)",
    )
    parser.add_argument(
        "--latch-database",
        type=Path,
        default=DEFAULT_TRADING_CONTROL_LATCH_PATH,
        help="the durable trading control latch (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-database",
        type=Path,
        default=DEFAULT_TRADER_HEARTBEAT_PATH,
        help="the trader's heartbeat journal (default: %(default)s)",
    )
    parser.add_argument(
        "--arming-key",
        type=Path,
        default=DEFAULT_OPERATOR_ARMING_KEY_PATH,
        help="the operator's arming key file, mode 600 (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-history-window",
        type=int,
        default=DEFAULT_HEARTBEAT_HISTORY_WINDOW,
        help="how many recent beats the tolerance is derived from (default: %(default)s)",
    )
    parser.add_argument(
        "--bootstrap-poll-seconds",
        type=float,
        default=_DEFAULT_BOOTSTRAP_POLL_SECONDS,
        help=(
            "poll interval used ONLY until the heartbeat history can derive one; afterwards the "
            "interval is half the derived staleness tolerance (default: %(default)s)"
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--supervise", action="store_true", help="run forever (the service mode)")
    mode.add_argument("--once", action="store_true", help="one supervision cycle, then exit")
    mode.add_argument("--status", action="store_true", help="print the current control state")
    mode.add_argument("--operator-halt", action="store_true", help="halt trading now, by hand")
    mode.add_argument(
        "--operator-release",
        action="store_true",
        help="clear the halt; requires --operator, --reason and --confirmation (R.22)",
    )
    parser.add_argument("--operator", default="", help="the human's name, recorded in the log")
    parser.add_argument("--reason", default="", help="why — recorded in the tamper-evident log")
    parser.add_argument(
        "--confirmation",
        default="",
        help="the typed confirmation phrase; it embeds this host's arming-key secret",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    watchdog = _build_watchdog(arguments)

    if arguments.status:
        return _report_status(watchdog)

    if arguments.operator_halt:
        if not arguments.operator.strip() or not arguments.reason.strip():
            print("--operator-halt needs --operator and --reason", file=sys.stderr)
            return 2
        cycle = watchdog.halt_on_operator_command(
            operator_name=arguments.operator, reason=arguments.reason
        )
        _print_cycle(cycle)
        return 0

    if arguments.operator_release:
        if not arguments.operator.strip() or not arguments.reason.strip():
            print("--operator-release needs --operator and --reason", file=sys.stderr)
            return 2
        try:
            authorization = authorize_operator_action(
                operator_name=arguments.operator,
                authority_scope=OperatorAuthorityScope.RELEASE_TRADING_HALT,
                typed_confirmation=arguments.confirmation,
                arming_key_path=arguments.arming_key,
            )
            disposition = watchdog.release_after_operator_review(
                authorization=authorization, reason=arguments.reason
            )
        except TradingControlError as refusal:
            print(f"release REFUSED: {refusal}", file=sys.stderr)
            return 3
        print(f"released: {disposition.describe()}")
        return 0

    if arguments.once:
        cycle = watchdog.reconcile_on_start()
        _print_cycle(cycle)
        return 0 if cycle.disposition_after.is_submission_permitted else 1

    return _supervise_forever(watchdog, arguments)


def _supervise_forever(watchdog: TradingHaltWatchdog, arguments: argparse.Namespace) -> int:
    stop_requested = False

    def _request_stop(signal_number: int, frame: types.FrameType | None) -> None:  # noqa: ARG001
        nonlocal stop_requested
        stop_requested = True
        print(f"stop requested by signal {signal_number}", flush=True)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    started_at = datetime.now(UTC)
    print(
        f"trading halt watchdog started {started_at.isoformat()} watching "
        f"{arguments.component!r}; latch={arguments.latch_database} "
        f"heartbeat={arguments.heartbeat_database}",
        flush=True,
    )
    cycles = supervise_until_stopped(
        watchdog,
        stop_requested=lambda: stop_requested,
        sleep=time.sleep,
        bootstrap_poll_interval=timedelta(seconds=arguments.bootstrap_poll_seconds),
        report=_print_cycle,
    )
    print(f"trading halt watchdog stopped after {cycles} cycles", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
