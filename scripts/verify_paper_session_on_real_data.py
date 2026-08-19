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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.broker_credentials import load_env_file_into_environ
from nse_algo_trader.capital_configuration import load_trading_capital_from_environment
from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import ReversionCalibrationStore
from nse_algo_trader.cost_gate.per_instrument_reversion_horizon_selector import (
    PerInstrumentReversionHorizonSelector,
)
from nse_algo_trader.decision_trace.decision_trace_record import DecisionTraceStore
from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    admit_on_traded_price_basis,
)
from nse_algo_trader.market_depth.bar_tape_join_verdict_store import (
    instruments_not_cleared_for,
    price_basis_divergences_for,
    refuted_instruments_for,
    verification_coverage_for,
)
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
from nse_algo_trader.paper_loop.bot_maturity_ladder import (
    ClosedPaperTrade,
    PaperTrackRecordStore,
)
from nse_algo_trader.paper_loop.paper_session_signal_source import (
    MeanReversionPaperSignalSource,
)
from nse_algo_trader.paper_loop.paper_trading_session_runner import (
    PaperInstrument,
    PaperSessionPolicy,
    PaperSessionReport,
    PaperTradingSessionRunner,
)
from nse_algo_trader.paper_loop.replayed_depth_book_source import SteppedRecordedBookSource
from nse_algo_trader.paper_loop.simulated_time_submission_rate_gate import (
    SimulatedTimeSubmissionRateGate,
)
from nse_algo_trader.replay_session_clock import ReplaySessionClock, session_for
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.sizing.session_risk_state_store import SessionRiskStateStore
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    DEFAULT_MARKET_DATA_PATH,
    RealStorePaths,
)
from nse_algo_trader.trade_quality.realized_payoff_distribution_estimator import (
    RealisedPayoffDistributionEstimator,
    RealisedTradeOutcome,
)
from nse_algo_trader.trade_quality.stated_probability_calibrator import (
    SESSIONS_NEEDED_FOR_OUT_OF_FOLD,
    ForecastOutcome,
    StatedProbabilityCalibrator,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_store import TradeQualityEvidenceStore
from nse_algo_trader.trade_quality.trade_quality_floor_engine import (
    QualityFloorPolicy,
    TradeQualityFloorEngine,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    TradeSpecification,
)

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

# The collar quantile: a price further from the reference than the scrip travels in 19 of 20
# horizons is not a price, it is a hope. Policy, stated here rather than defaulted anywhere.
PRICE_COLLAR_QUANTILE = Decimal("0.95")


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


@dataclass(frozen=True, slots=True)
class JoinAdmission:
    """What the bar/tape verification lets this run trade, and what it held back.

    Extracted from `main` by the `A.125` review (`M31`), which found the whole consumer change
    untested: three separate mutations of the inline version — reverting the filter to refusals
    only, mis-counting the split, and gutting the unrun-session warning — all survived the suite,
    because no test imports this script.
    """

    instruments: list[PaperInstrument]
    refused: int
    undecided: int
    coverage: object | None
    inconsistent: bool

    @property
    def withheld(self) -> int:
        return self.refused + self.undecided


def admit_instruments_for(
    instruments: list[PaperInstrument],
    *,
    refusals: frozenset[int],
    uncleared: frozenset[int],
    coverage: object | None,
    internally_consistent: bool,
) -> JoinAdmission:
    """Decide which instruments this run may trade, given the join verification's answer.

    **An UNRUN verification withholds nothing** — `uncleared` is empty for a session nobody
    verified, so an empty set means "no information", never "everything is fine" (`A.41`). The
    caller must branch on `coverage is None` and say so out loud; that is asserted by a test rather
    than left to the reader.
    """
    if coverage is None:
        return JoinAdmission(list(instruments), 0, 0, None, False)
    if not internally_consistent:
        return JoinAdmission([], 0, 0, coverage, True)
    refused = sum(1 for one in instruments if one.instrument_token in refusals)
    kept = [one for one in instruments if one.instrument_token not in uncleared]
    return JoinAdmission(kept, refused, len(instruments) - len(kept) - refused, coverage, False)


def _accrue_track_record(
    bot_identity: str,
    report: PaperSessionReport,
    segment: ChargeableSegment,
) -> int:
    """Persist this session's CLOSED positions as evidence under one bot identity (`L5.30`).

    Only closed positions count. A position still open at the close has no realised outcome, and
    counting it would let a bot bank a paper gain it has not taken — the shape `R.13` catches.

    **Costs are priced per trade through the real cost engine, not split from the session total.**
    The first version apportioned `report.costs_rupees` across positions by notional, and the
    track record's own collision guard caught it on the first real daily run: the same `ABB`
    position recorded a different cost in a 200-instrument run than in the full-universe run,
    because its share depended on which OTHER trades happened to be in the session. A trade's cost
    is a property of that trade — its segment, quantity and both leg prices — and break-even is a
    per-trade quantity, so an apportioned cost is not merely imprecise, it is the wrong kind of
    number. `NseTransactionCostEngine` already prices exactly this and is what the gate uses.
    """
    store = PaperTrackRecordStore()
    pricer = NseTransactionCostEngine(seeded_nse_market_rule_store())
    closed = [position for position in report.positions if position.closed_at is not None]

    written = 0
    for position in closed:
        entry = position.average_entry_paise or Decimal(0)
        exit_price = position.average_exit_paise or entry
        quantity = position.exit_filled_quantity or position.filled_quantity
        if quantity <= 0 or entry <= 0:
            continue  # nothing filled is nothing to learn from
        direction = Decimal(1) if position.side == TradeLeg.BUY else Decimal(-1)
        gross_rupees = (direction * (exit_price - entry) * Decimal(quantity)) / Decimal(100)
        priced = pricer.price_round_trip(
            TradeSpecification(
                segment=segment,
                quantity=quantity,
                entry_price_paise=entry,
                exit_price_paise=exit_price,
                trade_date=report.session_date,
                is_short_first=position.side is not TradeLeg.BUY,
            )
        )
        written += store.append(
            ClosedPaperTrade(
                bot_identity=bot_identity,
                session_date=report.session_date,
                position_key=position.position_key,
                instrument_token=position.instrument.instrument_token,
                trading_symbol=position.instrument.trading_symbol,
                side=str(position.side),
                filled_quantity=quantity,
                opened_at=position.opened_at,
                closed_at=position.closed_at or position.opened_at,
                close_reason=position.close_reason,
                gross_rupees=gross_rupees,
                costs_rupees=priced.total_rupees,
                stated_win_probability=position.stated_win_probability,
            )
        )
    return written


DEFAULT_QUALITY_BOT_IDENTITY = "cash_intraday_mean_reversion_bot"
"""Whose record `L5.31` is fitted on when the run is not recording under a named bot.

The same identity `PaperTradingSessionRunner.trace_bot_identity` defaults to, so a verification run
and the daily run judge the same bot rather than two that happen to share a loop.
"""

QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS: bool = True
"""One switch, named for exactly why it is off — `A.140`, `docs/research/261`.

`R.23c`'s adversarial review found six CRITICAL defects in `L5.31`, the worst of which admits about
one in three money-losing bots and does not improve with data. `B23` had already wired the gate into
the entry loop, so this seam is where an unsafe gate would reach a real session. It is held off HERE
rather than by deleting the wiring, because the wiring is correct and the engine is not: the loop
runs exactly as it did before `L5.31` landed, and flipping this back is the last step of the repair,
not the first.
"""

ADMISSION_CONFIDENCE = 0.9
"""Operator policy (`R.03`): how much posterior mass must sit above the binding floor to admit.

Stated here rather than defaulted inside the engine, and deliberately the same shape as
`LadderPolicy.promotion_confidence` so the per-trade gate and the per-bot ladder are answerable in
one currency.
"""


def _quality_gate_if_the_record_supports_one(
    bot_identity: str,
) -> tuple[TradeQualityFloorEngine | None, str]:
    """Attach `L5.31`'s floor only once this bot's own record can support it — `R.04`, not a switch.

    **The deadlock this avoids.** The gate refuses a proposal whose calibrated posterior is too
    wide, and a bot with no forecasts on record has the widest posterior there is. Placed in
    front of the only thing that BUILDS the record, it would refuse every trade forever and
    re-create `B15` — the cold-start deadlock that took twelve daily steps and zero trades to
    notice. So activation climbs the maturity ladder rather than being switched on: the floor
    attaches when the bot has both won and lost (a payoff ratio exists) and has forecasts
    written down before their outcomes.

    The full algorithm is built and unchanged either way (`R.04`); only its ACTIVATION is gated, and
    the reason is printed so a run never silently has no floor.
    """
    if QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS:
        return None, (
            "L5.31 quality floor HELD OFF by A.140: the adversarial review (docs/research/261) "
            "found SIX critical defects, including a calibrator that is in-sample despite its "
            "name and admits ~1 in 3 money-losing bots. Activation is blocked at this seam until "
            "every CRITICAL is fixed and the review re-run. The loop trades as it did before."
        )

    trades = PaperTrackRecordStore().closed_trades_for(bot_identity)
    forecasts = [trade for trade in trades if trade.stated_win_probability is not None]
    payoffs = RealisedPayoffDistributionEstimator(
        [
            RealisedTradeOutcome(
                bot_identity=trade.bot_identity,
                trading_segment=TradingSegment.CASH_INTRADAY,
                session_date=trade.session_date,
                gross_rupees=trade.gross_rupees,
                costs_rupees=trade.costs_rupees,
            )
            for trade in trades
        ]
    )
    if payoffs.posterior_for(bot_identity) is None:
        return None, (
            f"L5.31 quality floor NOT attached: {bot_identity} has {len(trades)} closed trades but "
            f"has not yet both won and lost, so its payoff ratio is unmeasured rather than "
            f"favourable. Running without a floor and recording the evidence that will attach one."
        )
    sessions = {trade.session_date for trade in forecasts}
    if len(sessions) < SESSIONS_NEEDED_FOR_OUT_OF_FOLD:
        return None, (
            f"L5.31 quality floor NOT attached: only {len(forecasts)} of {len(trades)} closed "
            f"trades carry a stated win probability, across {len(sessions)} session(s). The "
            f"calibrator needs forecasts recorded before their outcomes, over at least two "
            f"sessions, or every proposal is held to an uncalibrated posterior and nothing trades."
        )
    calibrator = StatedProbabilityCalibrator(
        [
            ForecastOutcome(
                bot_identity=trade.bot_identity,
                trading_segment=TradingSegment.CASH_INTRADAY,
                occurred_at=trade.opened_at,
                session_date=trade.session_date,
                stated_probability=float(trade.stated_win_probability or 0.0),
                was_win=trade.gross_rupees > 0,
            )
            for trade in forecasts
        ]
    )
    return (
        TradeQualityFloorEngine(
            calibrator, payoffs, QualityFloorPolicy(admission_confidence=ADMISSION_CONFIDENCE)
        ),
        f"L5.31 quality floor ATTACHED: fitted on {len(trades)} closed trades, "
        f"{len(forecasts)} of them carrying a stated probability across {len(sessions)} sessions.",
    )


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
    parser.add_argument(
        "--trace-to",
        type=Path,
        default=None,
        help=(
            "record a point-in-time DECISION TRACE per decision to this store (`L13.29`). "
            "Emitted from inside the runner at the instant, never assembled from the report "
            "afterwards — `A.29` calls that a confident fiction."
        ),
    )
    parser.add_argument(
        "--record-as",
        default=None,
        help=(
            "bot identity to accrue this session's closed trades under, in the PERSISTENT "
            "paper track record (`L5.30`). Omitted, the run verifies and records nothing — "
            "which is what this script did exclusively until 2026-08-17, and is why no bot "
            "had a track record to climb the R.04 ladder with (`B15`)."
        ),
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

    # `M14`'s gate, and it is a behaviour change rather than a diagnostic: an instrument whose
    # bar store and depth tape were MEASURED to describe different markets is one where the
    # signal and the fill come from unrelated series, so the P&L it produces is not evidence
    # about the strategy. Refused here rather than filtered inside the loop so the count is
    # printed and the exclusion is visible (`R.11`).
    join_refusals = refuted_instruments_for(session_date)
    # `A.125`/`M31`. The loop used to filter on REFUSALS alone, so an instrument the verification
    # could not decide about was treated exactly like one it cleared — and after `A.124` demoted
    # the thin ones, that was 411 of 3,327 candidates on 2026-08-11. An unverified join is not a
    # verified join (`A.41`), and a P&L produced over one is not evidence about the strategy.
    join_uncleared = instruments_not_cleared_for(session_date)
    join_coverage = verification_coverage_for(session_date)
    if join_coverage is None:
        print(
            f"WARNING: the bar/tape join has NOT been verified for {session_date.isoformat()} — "
            f"run scripts/verify_bar_tape_join_on_real_data.py. Proceeding UNVERIFIED (`R.05`)."
        )
    elif not join_coverage.is_internally_consistent:
        # An interrupted sweep re-run at a different policy leaves rows from BOTH, and `B6` proved
        # the threshold changes the verdicts. Two incomparable measurements are not one
        # verification, so this refuses rather than averaging them.
        print(
            f"the join verdicts for {session_date.isoformat()} mix POLICIES — staleness quantiles "
            f"{join_coverage.staleness_quantiles}, significances {join_coverage.significances}, "
            f"nulls {join_coverage.null_models}, detectable-rate claims "
            f"{join_coverage.minimum_detectable_disagreement_rates} "
            f"({join_coverage.rows_without_a_staleness_quantile} / "
            f"{join_coverage.rows_without_a_null_model} / "
            f"{join_coverage.rows_without_a_minimum_detectable_rate} rows unrecorded). "
            f"Two runs at two policies. Re-run "
            f"scripts/verify_bar_tape_join_on_real_data.py for this session before relying on it."
        )
        return 1
    else:
        before = len(instruments)
        admission = admit_instruments_for(
            instruments,
            refusals=join_refusals,
            uncleared=join_uncleared,
            coverage=join_coverage,
            internally_consistent=True,
        )
        instruments = admission.instruments
        print(
            f"bar/tape join ({session_date.isoformat()}): "
            f"{join_coverage.instruments_verified} verified, "
            f"{join_coverage.instruments_refuted} refuted, "
            f"{join_coverage.instruments_unverifiable} unverifiable — "
            f"{admission.withheld} of this run's {before} instruments withheld "
            f"({admission.refused} refused, {admission.undecided} undecided)"
        )
        # `M26`'s repair factors, surfaced rather than left in the store (`R.06` — the adversarial
        # review found this reader had no consumer at all, and it carries the entire point of the
        # addition: WHICH series is rescaled, and by what).
        rescaled = price_basis_divergences_for(session_date)
        if rescaled:
            print(
                f"  of those, {len(rescaled)} have a RESCALED bar series (`M26`) — the signal and "
                f"the fill are denominated differently:"
            )
            for token, factor in sorted(rescaled.items(), key=lambda item: item[1]):
                print(f"    token {token}: bar_close = {factor:.5f} x traded price")
        if not instruments:
            print("no instrument in this run has a VERIFIED bar/tape join")
            return 1

    # `L0.37`. The signal comes from `price_bars` and every fill from the depth tape; if the bars
    # are on a LATER adjusted basis the strategy is being measured against a market that did not
    # happen. Armed only on evidence (`R.04`) — with no bar recording a basis the rule cannot
    # distinguish anything, and it says so rather than withholding the universe.
    basis_admission = admit_on_traded_price_basis(
        session_date, [one.instrument_token for one in instruments], arguments.market_data
    )
    print(basis_admission.describe())
    if basis_admission.armed:
        allowed = set(basis_admission.instruments)
        instruments = [one for one in instruments if one.instrument_token in allowed]
        if not instruments:
            print("no instrument in this run has bars known to be on the traded price basis")
            return 1

    capital = load_trading_capital_from_environment()
    state_directory = arguments.state_directory / session_date.isoformat()
    state_directory.mkdir(parents=True, exist_ok=True)
    for stale in ("journal.sqlite3", "ledger.sqlite3", "risk.sqlite3", "rate.sqlite3"):
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
    clock = ReplaySessionClock(session_for(session_date), {})
    # The grid the loop will actually ask about, taken from a clock stepped the same way, so the
    # book source holds exactly those instants and reads each instrument's tape exactly once.
    grid_clock = ReplaySessionClock(session_for(session_date), {})
    decision_instants = list(grid_clock.step_through_session(DECISION_STEP))
    book_source = SteppedRecordedBookSource(
        replay_engine, decision_instants, staleness_quantile=STALENESS_QUANTILE
    )
    # One streamed pass over the session for the whole universe. Per-instrument reads are correct
    # and put the first full-universe attempt on course for six hours of scanning.
    session_window_start = session_for(session_date).opens_at - timedelta(hours=4)
    session_window_end = session_for(session_date).closes_at + timedelta(hours=4)
    print(f"reading the depth tape for {len(instruments)} instruments in one pass...")
    with_rows = book_source.preload(
        MarketDepthTapeReader(arguments.tape_root),
        [instrument.instrument_token for instrument in instruments],
        session_date=session_date,
        window_start=session_window_start,
        window_end=session_window_end,
    )
    print(f"  {with_rows} instrument(s) had recorded depth on this session")
    tradeable_window = book_source.covered_window()
    if tradeable_window is None:
        print("the tape covers none of this session's decision instants")
        return 1
    covered = sum(
        1 for instant in decision_instants if tradeable_window[0] <= instant <= tradeable_window[1]
    )
    print(
        f"  the capture covers {tradeable_window[0].strftime('%H:%M')}-"
        f"{tradeable_window[1].strftime('%H:%M')} — {covered} of {len(decision_instants)} "
        f"decision instants; entries are considered only inside it (`A.116`)"
    )
    rate_gate = SimulatedTimeSubmissionRateGate(
        clock=clock, store_path=state_directory / "rate.sqlite3"
    )
    quality_gate, quality_gate_reason = _quality_gate_if_the_record_supports_one(
        arguments.record_as or DEFAULT_QUALITY_BOT_IDENTITY
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
            price_collar_quantile=PRICE_COLLAR_QUANTILE,
            tradeable_window=tradeable_window,
        ),
        instruments=instruments,
        decision_trace_store=(
            DecisionTraceStore(arguments.trace_to) if arguments.trace_to else None
        ),
        clock=clock,
        signal_source=MeanReversionPaperSignalSource(
            minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
            minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
            armed_classifiers=ARMED_CLASSIFIERS,
            market_data=arguments.market_data,
        ),
        book_source=book_source,
        journal=OrderIntentJournal(state_directory / "journal.sqlite3"),
        venue=SimulatedOrderExecutionVenue(),
        ledger=ledger,
        risk_store=SessionRiskStateStore(state_directory / "risk.sqlite3"),
        capital=capital,
        store_paths=RealStorePaths(market_data=arguments.market_data),
        # Without this the report's costs are zero, and a zero cost beside a non-zero gross is the
        # comparison graduation reads being silently skipped.
        cost_pricer=NseTransactionCostEngine(seeded_nse_market_rule_store()),
        rate_gate=rate_gate,
        # One horizon for every instrument synchronises the exits and the wire refuses the wave
        # (`A.115`); this chooses the holding time from the fitted grid, per deviation.
        horizon_selector=PerInstrumentReversionHorizonSelector(ReversionCalibrationStore()),
        trade_quality_gate=quality_gate,
        trade_quality_store=TradeQualityEvidenceStore() if quality_gate else None,
    )

    print(quality_gate_reason)
    print(
        f"replaying {session_date.isoformat()} over {len(instruments)} instruments "
        f"against Rs {capital.total_rupees}"
    )
    report = runner.run()
    print(report.describe())
    print(rate_gate.describe())
    horizons = Counter(
        record.detail.split(" bars")[0]
        for record in report.decisions
        if record.outcome == "placed" and " bars" in record.detail
    )
    if horizons:
        print("horizons chosen: " + ", ".join(f"{h}b x{n}" for h, n in sorted(horizons.items())))
    print(
        f"books: {book_source.instruments_loaded} instrument(s) read from the tape, "
        f"{len(book_source.instruments_with_no_tape)} never recorded"
    )

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

    if arguments.record_as:
        # The segment the session was configured with, so a cost is priced under the
        # scope that actually applied rather than one inferred after the fact.
        recorded = _accrue_track_record(
            arguments.record_as, report, ChargeableSegment.EQUITY_INTRADAY
        )
        print(
            f"\ntrack record: {recorded} closed trade(s) accrued for {arguments.record_as!r} "
            f"(re-running this session records 0 more — the store is idempotent per position)"
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
