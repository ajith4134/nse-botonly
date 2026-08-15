"""`R.05` for `F04` — replay one REAL session end to end and report exactly what happened.

Not a test. The suite is hermetic by design (a synthetic book behind the `RecordedBookSource` seam
and a scripted signal), so `R.05` counts it as functional verification only. This is the pass that
puts the real bar store, the real regime panel, the real mean-reversion engine, the real order path
and the real recorded depth tape through the same code, on one real trading day, over the full
universe that day recorded — and reports the answer honestly, including every refusal.

`R.09`: every instrument the depth tape recorded that day and the bar store can price, not a chosen
few. A loop that works on RELIANCE and divides by zero on an illiquid scrip is not verified.

What a PASS looks like — stated before running, so the bar cannot move afterwards:

1. **No unhandled exception across the whole session.** A refusal is a result; a traceback is a
   defect.
2. **Every decision carries a reason**, whether it acted or declined.
3. **No fill prices better than the touch of the book it filled from**, and no filled quantity
   exceeds the depth that was actually recorded — the two ways a paper record flatters itself.
4. **Nothing survives the close** (`R.01`), or every position that does is named in the report with
   the reason it could not be exited.
5. **The closing balance equals the fold of the ledger's own events.** The balance is never
   anything but a replay of the log.

Usage:
    python scripts/verify_paper_session_on_real_data.py 2026-08-11
    python scripts/verify_paper_session_on_real_data.py 2026-08-11 --instrument-limit 200
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.broker_credentials import load_env_file_into_environ
from nse_algo_trader.capital_configuration import load_trading_capital_from_environment
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    OrderBookSnapshotReplayEngine,
)
from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.simulated_order_execution_venue import (
    SimulatedOrderExecutionVenue,
)
from nse_algo_trader.paper_capital_ledger import PaperCapitalLedger
from nse_algo_trader.paper_loop.paper_session_signal_source import (
    MeanReversionPaperSignalSource,
)
from nse_algo_trader.paper_loop.paper_trading_session_runner import (
    PaperInstrument,
    PaperSessionPolicy,
    PaperTradingSessionRunner,
)
from nse_algo_trader.replay_session_clock import ReplaySessionClock, session_for
from nse_algo_trader.sizing.session_risk_state_store import SessionRiskStateStore
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    DEFAULT_MARKET_DATA_PATH,
    RealStorePaths,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import NseTransactionCostEngine

DEFAULT_DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()

# The six segments are equal by default (`R.10`), which is what makes the concentration cap a
# structural fact rather than a chosen number.
EQUAL_SEGMENT_COUNT = 6
CASH_INTRADAY_MARGIN_FRACTION = Decimal("0.20")
REGISTRATION_THRESHOLD_ORDERS_PER_SECOND = 10
HORIZON_BARS = 5
DECISION_STEP = timedelta(minutes=5)
STALENESS_QUANTILE = 0.95

# The panel is armed in full for a verification run: an unarmed panel yields a uniform belief and
# every decision would be an abstain, which verifies nothing about the path below the strategy.
ARMED_CLASSIFIERS = ("trend_strength", "volatility", "session_phase")
MINIMUM_REGIME_CONCENTRATION = 0.30
MINIMUM_REGIME_AGREEMENT = 0.50


def instruments_priced_on(
    session_date: date, market_data: Path, *, limit: int | None
) -> list[PaperInstrument]:
    """Every instrument with a lot size and bars recorded on this exact session."""
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        effective = connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on <= ?",
            (session_date.isoformat(),),
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT m.instrument_token, m.tradingsymbol, m.lot_size FROM instrument_master m "
            "WHERE m.ingested_on = ? AND m.lot_size > 0 AND EXISTS ("
            "  SELECT 1 FROM price_bars b WHERE b.instrument_token = m.instrument_token "
            "  AND substr(b.bar_timestamp, 1, 10) = ?) ORDER BY m.tradingsymbol",
            (effective, session_date.isoformat()),
        ).fetchall()
    instruments = [
        PaperInstrument(
            instrument_token=int(token), trading_symbol=str(symbol), lot_size=int(lot_size)
        )
        for token, symbol, lot_size in rows
    ]
    return instruments[:limit] if limit is not None else instruments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_date", help="the trading day to replay, YYYY-MM-DD")
    parser.add_argument("--instrument-limit", type=int, default=None)
    parser.add_argument("--market-data", type=Path, default=DEFAULT_MARKET_DATA_PATH)
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_DEPTH_TAPE_ROOT)
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=Path("~/.nse_algo_trader/paper_verification").expanduser(),
        help="where this run's journal, ledger and risk state are written",
    )
    arguments = parser.parse_args()
    load_env_file_into_environ()

    session_date = date.fromisoformat(arguments.session_date)
    instruments = instruments_priced_on(
        session_date, arguments.market_data, limit=arguments.instrument_limit
    )
    if not instruments:
        print(f"no instrument has both a lot size and bars on {session_date.isoformat()}")
        return 1

    capital = load_trading_capital_from_environment()
    state_directory = arguments.state_directory / session_date.isoformat()
    state_directory.mkdir(parents=True, exist_ok=True)
    for stale in ("journal.sqlite3", "ledger.sqlite3", "risk.sqlite3"):
        (state_directory / stale).unlink(missing_ok=True)

    ledger = PaperCapitalLedger(state_directory / "ledger.sqlite3")
    opens_at = session_for(session_date).opens_at
    # Seeded one minute before the open: the ledger refuses an event earlier than its last, and a
    # ledger seeded at the wall clock could never be written by a session replayed in the past.
    ledger.seed_from_ceiling(
        capital, occurred_at=opens_at - timedelta(minutes=1), reason=f"{session_date} paper replay"
    )

    replay_engine = OrderBookSnapshotReplayEngine(
        MarketDepthTapeReader(arguments.tape_root),
        session_date=session_date,
        staleness_quantile=STALENESS_QUANTILE,
    )
    runner = PaperTradingSessionRunner(
        policy=PaperSessionPolicy(
            session_date=session_date,
            decision_step=DECISION_STEP,
            horizon_bars=HORIZON_BARS,
            concurrent_position_capacity=EQUAL_SEGMENT_COUNT,
            segment=ChargeableSegment.EQUITY_INTRADAY,
            segment_margin_fraction=CASH_INTRADAY_MARGIN_FRACTION,
            registration_threshold_orders_per_second=REGISTRATION_THRESHOLD_ORDERS_PER_SECOND,
            minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
            minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
            armed_classifiers=ARMED_CLASSIFIERS,
        ),
        instruments=instruments,
        clock=ReplaySessionClock(session_for(session_date), {}),
        signal_source=MeanReversionPaperSignalSource(
            minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
            minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
            armed_classifiers=ARMED_CLASSIFIERS,
            market_data=arguments.market_data,
        ),
        book_source=replay_engine,
        journal=OrderIntentJournal(state_directory / "journal.sqlite3"),
        venue=SimulatedOrderExecutionVenue(),
        ledger=ledger,
        risk_store=SessionRiskStateStore(state_directory / "risk.sqlite3"),
        capital=capital,
        store_paths=RealStorePaths(market_data=arguments.market_data),
        # Without this the report's costs are zero, and a zero cost beside a non-zero gross is the
        # comparison graduation reads being silently skipped.
        cost_pricer=NseTransactionCostEngine(seeded_nse_market_rule_store()),
    )

    print(
        f"replaying {session_date.isoformat()} over {len(instruments)} instruments "
        f"against Rs {capital.total_rupees}"
    )
    report = runner.run()
    print(report.describe())

    outcomes = Counter(record.outcome for record in report.decisions)
    print("\ndecision outcomes:")
    for outcome, count in outcomes.most_common():
        print(f"  {outcome:24s} {count}")

    absent_reasons = Counter(
        record.detail.split(":")[0][:80]
        for record in report.decisions
        if record.outcome == "unsizable"
    )
    if absent_reasons:
        print("\nwhy inputs could not be assembled (top 10):")
        for reason, count in absent_reasons.most_common(10):
            print(f"  {count:6d}  {reason}")

    print("\npositions:")
    for position in report.positions:
        print(
            f"  {position.position_key}: {position.side} {position.filled_quantity}/"
            f"{position.ordered_quantity} @ {position.average_entry_paise} paise, exited "
            f"{position.exit_filled_quantity} @ {position.average_exit_paise} — "
            f"{position.close_reason or 'never exited'}"
        )

    fold = ledger.fold_from_events()
    balance_agrees = fold.balance_rupees - fold.committed_rupees == report.closing_balance_rupees
    print(
        f"\nledger fold: balance Rs {fold.balance_rupees}, committed Rs {fold.committed_rupees}, "
        f"agrees with report: {balance_agrees}"
    )
    if report.open_at_close:
        print(f"OPEN AT CLOSE (`R.01` breach unless explained): {report.open_at_close}")
    if report.unfilled_at_close:
        print(f"partially filled at close: {report.unfilled_at_close}")
    return 0 if balance_agrees else 1


if __name__ == "__main__":
    raise SystemExit(main())
