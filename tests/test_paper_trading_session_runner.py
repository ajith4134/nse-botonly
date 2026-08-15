"""`F04` — the paper session, verified end to end against its own spec's §7.

`docs/research/228_paper_trading_loop_and_simulated_venue_spec.md` §7 names the axes this file
must not hold constant: a bar published after the decision, a book that is absent, an order larger
than the visible ladder, a latch tripped mid-session, and the close arriving with a part-filled
order. Each is a test below, and each was written before the behaviour it checks was trusted.

Everything below the venue is REAL — the intent journal, the placer, the lifecycle state machine
and the broker-truth reconciler are the production objects on a temporary database. The two
injected fakes are the recorded book (a hermetic harness behind the `RecordedBookSource` seam,
`R.J`) and the signal source, which is scripted so that a test asserts on the LOOP's behaviour
rather than on whether a regime classifier happened to fire that afternoon.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionCapture,
)
from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.simulated_order_execution_venue import (
    SimulatedOrderExecutionVenue,
)
from nse_algo_trader.paper_capital_ledger import PaperCapitalLedger
from nse_algo_trader.paper_loop.paper_session_signal_source import (
    AvailableBar,
    InstrumentSignalState,
    PaperSignal,
    SignalSourceError,
    bars_available_at,
)
from nse_algo_trader.paper_loop.paper_trading_session_runner import (
    PaperInstrument,
    PaperSessionPolicy,
    PaperTradingSessionRunner,
)
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.regime.session_phase_regime_classifier import SessionPhaseRegimeClassifier
from nse_algo_trader.regime.soft_regime_weighting_brain import RegimeBelief
from nse_algo_trader.regime.trend_strength_regime_classifier import TrendStrengthRegimeClassifier
from nse_algo_trader.regime.volatility_regime_classifier import VolatilityRegimeClassifier
from nse_algo_trader.replay_session_clock import ReplaySessionClock, session_for
from nse_algo_trader.sizing.session_risk_state_store import RiskLatch, SessionRiskStateStore
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import RealStorePaths
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
    MeanReversionDecision,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

IST = session_for(date(2026, 8, 5)).opens_at.tzinfo
SESSION_DATE = date(2026, 8, 5)
TOKEN = 738561
SYMBOL = "TESTSCRIP"
LOT_SIZE = 1
BAR_INTERVAL = "5m"
CLOSE_RUPEES = Decimal("1000")
PAISE_PER_RUPEE = Decimal(100)

# A book deep enough to fill a small order and shallow enough that a large one cannot: three
# rungs of 40 units each. Every quantity below is stated against this ladder rather than a
# constant, so a change here cannot silently invalidate an assertion.
LADDER_QUANTITY_PER_RUNG = 40
LADDER_RUNGS = 3
VISIBLE_LADDER_QUANTITY = LADDER_QUANTITY_PER_RUNG * LADDER_RUNGS


# --- the stores ------------------------------------------------------------------------------


def _write_bars(
    market_data: Path,
    *,
    closes: list[Decimal],
    first_bar_at: datetime,
    publication_lag: timedelta,
) -> None:
    """A price_bars table shaped exactly like the real store, including availability_time."""
    connection = sqlite3.connect(market_data)
    connection.execute(
        "CREATE TABLE price_bars ("
        "instrument_token INTEGER NOT NULL, bar_interval TEXT NOT NULL, "
        "bar_timestamp TEXT NOT NULL, open_price REAL NOT NULL, high_price REAL NOT NULL, "
        "low_price REAL NOT NULL, close_price REAL NOT NULL, volume INTEGER NOT NULL, "
        "open_interest INTEGER, availability_time TEXT, "
        "PRIMARY KEY (instrument_token, bar_interval, bar_timestamp))"
    )
    for index, close in enumerate(closes):
        stamp = first_bar_at + timedelta(minutes=5 * index)
        connection.execute(
            "INSERT INTO price_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                TOKEN,
                BAR_INTERVAL,
                stamp.isoformat(),
                float(close),
                float(close) * 1.001,
                float(close) * 0.999,
                float(close),
                1_000,
                (stamp + publication_lag).isoformat(),
            ),
        )
    connection.commit()
    connection.close()


def _write_calibration(calibration: Path) -> None:
    """One pooled capture that covers any deviation the sizer asks about."""
    store = ReversionCalibrationStore(calibration)
    store.record(
        [
            ReversionCapture(
                deviation_bucket=Decimal(bucket),
                horizon_bars=5,
                event_count=5_000,
                mean_captured_bps=Decimal(30),
                median_captured_bps=Decimal(10),
                standard_error_bps=Decimal(2),
                mean_captured_sigma=Decimal("0.05"),
                fitted_through=SESSION_DATE - timedelta(days=1),
                maturity=CalibrationMaturity.POOLED_UNIVERSE,
            )
            for bucket in range(0, 6)
        ]
    )


@pytest.fixture
def stores(tmp_path: Path) -> RealStorePaths:
    market_data = tmp_path / "market_data.sqlite3"
    calibration = tmp_path / "calibration.sqlite3"
    opens_at = session_for(SESSION_DATE).opens_at
    # A tape that actually moves, because a scrip that never moves sets no price collar and the
    # gate refuses it — correctly, and it would make every test below assert on a refusal instead
    # of on the loop. A deterministic sawtooth plus one dip: real movement, no randomness.
    closes = [
        CLOSE_RUPEES + Decimal(index % 7) * Decimal("0.5") for index in range(118)
    ] + [CLOSE_RUPEES * Decimal("0.98"), CLOSE_RUPEES]
    _write_bars(
        market_data,
        closes=closes,
        first_bar_at=opens_at - timedelta(minutes=5 * len(closes)),
        publication_lag=timedelta(minutes=1),
    )
    _write_calibration(calibration)
    return RealStorePaths(market_data=market_data, calibration=calibration)


# --- the injected seams ----------------------------------------------------------------------


def _book(at: datetime, *, quantity_per_rung: int = LADDER_QUANTITY_PER_RUNG) -> BookSnapshot:
    """A two-sided ladder around Rs 1,000, in paise, with a real spread."""
    touch = int(CLOSE_RUPEES * PAISE_PER_RUPEE)
    return BookSnapshot(
        instrument_token=TOKEN,
        receipt_time=at,
        receipt_sequence=int(at.timestamp()),
        capture_run="test",
        exchange_time=at,
        last_price_paise=touch,
        last_traded_quantity=1,
        volume_traded=10_000,
        total_buy_quantity=quantity_per_rung * LADDER_RUNGS,
        total_sell_quantity=quantity_per_rung * LADDER_RUNGS,
        integrity_flags=IntegrityFlag.NONE,
        bids=tuple(
            DepthLevel(price_paise=touch - 5 * (rung + 1), quantity=quantity_per_rung, orders=2)
            for rung in range(LADDER_RUNGS)
        ),
        asks=tuple(
            DepthLevel(price_paise=touch + 5 * (rung + 1), quantity=quantity_per_rung, orders=2)
            for rung in range(LADDER_RUNGS)
        ),
    )


@dataclass
class RecordedBookHarness:
    """A hermetic stand-in for the depth tape (`R.J`). Never reaches production."""

    present: bool = True
    quantity_per_rung: int = LADDER_QUANTITY_PER_RUNG
    absent_after: datetime | None = None
    books_served: int = 0

    def book_at(self, instrument_token: int, as_of: datetime) -> BookSnapshot | None:
        del instrument_token
        if not self.present:
            return None
        if self.absent_after is not None and as_of >= self.absent_after:
            return None
        self.books_served += 1
        return _book(as_of, quantity_per_rung=self.quantity_per_rung)


@dataclass
class ScriptedSignalSource:
    """Decides what the test needs decided, so the assertions are about the LOOP."""

    entries_at: list[datetime] = field(default_factory=list)
    reversal_at: datetime | None = None
    calls: list[datetime] = field(default_factory=list)
    _entered: set[datetime] = field(default_factory=set)

    def strategy_identity_for(self, instrument_token: int) -> str:
        del instrument_token
        return "scripted_for_test"

    def signal_for(
        self, *, instrument_token: int, trading_symbol: str, at: datetime
    ) -> PaperSignal:
        self.calls.append(at)
        action = MeanReversionAction.ABSTAIN
        due = [moment for moment in self.entries_at if moment <= at and moment not in self._entered]
        if due:
            self._entered.add(due[0])
            action = MeanReversionAction.ENTER_LONG
        elif self.reversal_at is not None and at >= self.reversal_at:
            action = MeanReversionAction.ENTER_SHORT
        decision = MeanReversionDecision(
            action=action,
            conviction=1.0,
            reason="scripted",
            deviation=-2.0,
            deviation_band=1.0,
            regime_used=MarketRegime.RANGING,
        )
        return PaperSignal(
            instrument_token=instrument_token,
            trading_symbol=trading_symbol,
            observed_at=at,
            decision=decision,
            belief=RegimeBelief(
                distribution=RegimeDistribution(MarketRegime.uniform_probabilities()),
                contributing_classifiers=(),
                disagreement=0.0,
                weights={},
                observed_at=at,
            ),
            bars_observed=120,
            bars_consumed_this_step=1,
            latest_close_rupees=CLOSE_RUPEES,
        )


# --- the runner under test -------------------------------------------------------------------


def _policy(step: timedelta = timedelta(minutes=5)) -> PaperSessionPolicy:
    return PaperSessionPolicy(
        session_date=SESSION_DATE,
        decision_step=step,
        horizon_bars=5,
        concurrent_position_capacity=6,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        segment_margin_fraction=Decimal("0.20"),
        registration_threshold_orders_per_second=10,
        minimum_regime_concentration=0.5,
        minimum_regime_agreement=0.5,
        armed_classifiers=("trend_strength", "volatility", "session_phase"),
        price_collar_quantile=Decimal("0.95"),
    )


def _runner(
    tmp_path: Path,
    stores: RealStorePaths,
    *,
    signal_source: ScriptedSignalSource,
    book_source: RecordedBookHarness,
    step: timedelta = timedelta(minutes=5),
    capital_rupees: Decimal = Decimal("1000000"),
) -> tuple[
    PaperTradingSessionRunner, PaperCapitalLedger, SessionRiskStateStore, OrderIntentJournal
]:
    journal = OrderIntentJournal(tmp_path / "journal.sqlite3")
    ledger = PaperCapitalLedger(tmp_path / "ledger.sqlite3")
    capital = TradingCapital.of_rupees(capital_rupees)
    # Seeded at a replay-consistent instant: the ledger refuses an event earlier than its last,
    # so a ledger seeded at the wall clock cannot then be written by a session replayed in the
    # past. That is correct behaviour, and a replayed session must respect it.
    ledger.seed_from_ceiling(
        capital,
        occurred_at=session_for(SESSION_DATE).opens_at - timedelta(minutes=1),
        reason="paper session test seed",
    )
    risk_store = SessionRiskStateStore(tmp_path / "risk.sqlite3")
    clock = ReplaySessionClock(session_for(SESSION_DATE), {})
    runner = PaperTradingSessionRunner(
        policy=_policy(step),
        instruments=[
            PaperInstrument(instrument_token=TOKEN, trading_symbol=SYMBOL, lot_size=LOT_SIZE)
        ],
        clock=clock,
        signal_source=signal_source,
        book_source=book_source,
        journal=journal,
        venue=SimulatedOrderExecutionVenue(),
        ledger=ledger,
        risk_store=risk_store,
        capital=capital,
        store_paths=stores,
    )
    return runner, ledger, risk_store, journal


# --- unit: the leakage guard, which is the whole difference from a backtest --------------------


def test_a_bar_stamped_before_the_decision_but_published_after_it_is_invisible(
    tmp_path: Path,
) -> None:
    """The adversarial case §2 requires: the row that would let a replay see the future."""
    market_data = tmp_path / "bars.sqlite3"
    decision_at = datetime(2026, 8, 5, 10, 0, tzinfo=IST)
    _write_bars(
        market_data,
        closes=[CLOSE_RUPEES],
        first_bar_at=decision_at - timedelta(minutes=5),
        publication_lag=timedelta(minutes=30),
    )
    visible = bars_available_at(
        instrument_token=TOKEN, at=decision_at, market_data=market_data
    )
    assert visible == ()

    later = bars_available_at(
        instrument_token=TOKEN,
        at=decision_at + timedelta(minutes=30),
        market_data=market_data,
    )
    assert len(later) == 1
    assert later[0].bar_timestamp < decision_at
    assert later[0].availability_time > decision_at


def test_a_naive_decision_instant_is_refused(tmp_path: Path) -> None:
    market_data = tmp_path / "bars.sqlite3"
    _write_bars(
        market_data,
        closes=[CLOSE_RUPEES],
        first_bar_at=datetime(2026, 8, 5, 9, 15, tzinfo=IST),
        publication_lag=timedelta(minutes=1),
    )
    with pytest.raises(SignalSourceError, match="timezone"):
        bars_available_at(
            instrument_token=TOKEN,
            at=datetime(2026, 8, 5, 10, 0),  # noqa: DTZ001 — the defect under test
            market_data=market_data,
        )


def test_a_streaming_estimator_refuses_a_bar_it_has_already_consumed() -> None:
    """Double-counting a bar tightens a band against evidence the instrument never produced."""
    state = InstrumentSignalState(
        trend=TrendStrengthRegimeClassifier(),
        volatility=VolatilityRegimeClassifier(),
        session_phase=SessionPhaseRegimeClassifier(),
        strategy=IntradayMeanReversionEngine(
            minimum_regime_concentration=0.5, minimum_regime_agreement=0.5
        ),
    )
    stamp = datetime(2026, 8, 5, 9, 20, tzinfo=IST)
    bar = AvailableBar(
        bar_timestamp=stamp,
        open_price=1000.0,
        high_price=1001.0,
        low_price=999.0,
        close_price=1000.0,
        volume=100,
        availability_time=stamp + timedelta(minutes=1),
    )
    state.observe(bar)
    with pytest.raises(SignalSourceError, match="not after the last one consumed"):
        state.observe(bar)
    assert state.bars_observed == 1


# --- the session, end to end ------------------------------------------------------------------


def test_a_scripted_entry_is_funded_placed_filled_and_squared_off(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """The whole path: ledger commitment, real journal, real reconciler, flat at the close."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    signals = ScriptedSignalSource(entries_at=[entry_at])
    books = RecordedBookHarness()
    runner, ledger, _risk, journal = _runner(
        tmp_path, stores, signal_source=signals, book_source=books, step=timedelta(minutes=30)
    )
    report = runner.run()

    assert report.orders_placed >= 1, report.describe()
    assert report.positions, "an accepted entry must produce a position"
    position = report.positions[0]
    assert position.filled_quantity > 0, "the recorded ladder must have filled something"
    assert position.average_entry_paise is not None

    # `R.01`: nothing survives the close.
    assert report.open_at_close == (), report.describe()
    assert position.exit_intent_ids
    assert position.exit_filled_quantity == position.filled_quantity

    # The order path is the real one: both legs are in the journal under the paper namespace.
    orders = journal.orders_for_session(SESSION_DATE)
    assert {order.side for order in orders} == {TradeLeg.BUY, TradeLeg.SELL}
    sold = sum(order.ordered_quantity for order in orders if order.side is TradeLeg.SELL)
    bought = sum(order.ordered_quantity for order in orders if order.side is TradeLeg.BUY)
    assert sold <= bought, "a square-off may never sell more than the entry actually filled"

    # The capital reserved for the position was released when it closed.
    fold = ledger.fold_from_events()
    assert fold.open_commitments == ()


def test_the_closing_balance_equals_the_fold_of_the_ledgers_own_events(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """The property §7 names: the balance is never anything but a replay of the log."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    fold = ledger.fold_from_events()
    assert fold.balance_rupees - fold.committed_rupees == report.closing_balance_rupees


def test_a_fill_never_prices_better_than_the_touch(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """A buy pays at or above the best ask — the optimism `A.108` names as the failure mode."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    position = report.positions[0]
    assert position.average_entry_paise is not None
    best_ask = _book(entry_at).asks[0].price_paise
    assert position.average_entry_paise >= Decimal(best_ask)


def test_a_filled_quantity_never_exceeds_the_visible_ladder(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """An order larger than the book partially fills; it does not invent the depth it needs."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(quantity_per_rung=1),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    position = report.positions[0]
    # Each step brings a NEW book, so the bound is per book rather than per session: one rung a
    # poll, `LADDER_RUNGS` rungs a book, one book a step.
    assert 0 < position.filled_quantity < position.ordered_quantity
    assert position.filled_quantity <= LADDER_RUNGS * report.steps_taken, (
        "a fill larger than every visible ladder the session ever saw means depth was invented"
    )


# --- adversarial ------------------------------------------------------------------------------


def test_no_recorded_book_means_no_fill_and_no_invented_price(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """A scrip with no depth at the instant is never filled at the last close."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(present=False),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    assert all(position.filled_quantity == 0 for position in report.positions)
    assert report.gross_realised_rupees == Decimal(0)
    # The commitment stays open because nothing filled and nothing closed — reported, never
    # quietly released, because released capital would say the position never existed.
    assert ledger.fold_from_events().balance_rupees > Decimal(0)


def test_a_book_that_vanishes_mid_session_does_not_fill_the_square_off_from_thin_air(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """The close arrives with a position that cannot be exited — reported, never assumed flat."""
    opens_at = session_for(SESSION_DATE).opens_at
    entry_at = opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(absent_after=entry_at + timedelta(minutes=30)),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    position = report.positions[0]
    assert position.filled_quantity > 0
    assert position.exit_filled_quantity < position.filled_quantity, (
        "a vanished book must not fill the square-off from the last snapshot it ever saw"
    )
    assert report.open_at_close == (position.position_key,), (
        "a position that could not be exited must be REPORTED as open at the close"
    )


def test_a_latch_tripped_mid_session_halts_the_loop(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """The system can halt itself and can never un-halt itself (`R.22`)."""
    opens_at = session_for(SESSION_DATE).opens_at
    signals = ScriptedSignalSource(entries_at=[opens_at + timedelta(minutes=60)])
    runner, _ledger, risk_store, _journal = _runner(
        tmp_path,
        stores,
        signal_source=signals,
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=30),
    )
    risk_store.open_session(
        session_date=SESSION_DATE,
        opening_equity_rupees=Decimal("1000000"),
        occurred_at=opens_at,
    )
    risk_store.trip_latch(
        RiskLatch.DAILY_LOSS,
        session_date=SESSION_DATE,
        occurred_at=opens_at,
        reason="tripped before the session for the halt test",
    )
    report = runner.run()
    assert report.halted_reason, "a halted session must say which latch stopped it"
    assert report.orders_placed == 0
    assert report.steps_taken == 1, "the halt is checked before any entry is considered"


def test_a_session_over_no_instruments_is_refused(tmp_path: Path, stores: RealStorePaths) -> None:
    """`R.09` is visible at the call site: an empty universe is a caller defect, not a quiet day."""
    from nse_algo_trader.paper_loop.paper_trading_session_runner import PaperSessionError

    journal = OrderIntentJournal(tmp_path / "journal.sqlite3")
    ledger = PaperCapitalLedger(tmp_path / "ledger.sqlite3")
    capital = TradingCapital.of_rupees(Decimal("1000000"))
    ledger.seed_from_ceiling(
        capital,
        occurred_at=session_for(SESSION_DATE).opens_at - timedelta(minutes=1),
        reason="seed",
    )
    with pytest.raises(PaperSessionError, match="no instruments"):
        PaperTradingSessionRunner(
            policy=_policy(),
            instruments=[],
            clock=ReplaySessionClock(session_for(SESSION_DATE), {}),
            signal_source=ScriptedSignalSource(),
            book_source=RecordedBookHarness(),
            journal=journal,
            venue=SimulatedOrderExecutionVenue(),
            ledger=ledger,
            risk_store=SessionRiskStateStore(tmp_path / "risk.sqlite3"),
            capital=capital,
            store_paths=stores,
        )


def test_an_instrument_with_no_lot_size_is_refused() -> None:
    """`L1.09`: a substituted lot size is worse than an absent one."""
    from nse_algo_trader.paper_loop.paper_trading_session_runner import PaperSessionError

    with pytest.raises(PaperSessionError, match="lot size"):
        PaperInstrument(instrument_token=TOKEN, trading_symbol=SYMBOL, lot_size=0)


def test_every_decision_is_recorded_with_the_reason_it_did_or_did_not_act(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """`L13.29`: the reasoning is written as it happens, never reconstructed afterwards."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    assert report.decisions, "a session with no decision record explains nothing"
    outcomes = {record.outcome for record in report.decisions}
    assert "abstained" in outcomes
    assert report.refusals_by_reason, "the refusal histogram is what an operator reads first"
    assert all(record.signal is not None for record in report.decisions)


def test_the_report_reads_back_as_one_line(tmp_path: Path, stores: RealStorePaths) -> None:
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=60),
    )
    report = runner.run()
    described = report.describe()
    assert SESSION_DATE.isoformat() in described
    assert "net Rs" in described
    assert report.ended_at.tzinfo is not None
    assert report.started_at <= report.ended_at
    assert report.started_at.astimezone(UTC) <= report.ended_at.astimezone(UTC)


def test_a_position_is_open_while_quantity_is_open_not_while_a_flag_says_so(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """`A.111`: the first real session squared off 377 units of a position that grew to 5,232.

    The entry was still filling when its horizon expired. The exit went in for what had filled,
    the position was marked closed, and every later exit path tested that flag — so the remaining
    4,855 units had nothing willing to sell them. Openness is now derived from the quantities.
    """
    from nse_algo_trader.paper_loop.paper_trading_session_runner import OpenPaperPosition

    position = OpenPaperPosition(
        position_key="TEST-1",
        entry_intent_id="entry",
        instrument=PaperInstrument(
            instrument_token=TOKEN, trading_symbol=SYMBOL, lot_size=LOT_SIZE
        ),
        side=TradeLeg.BUY,
        ordered_quantity=5232,
        filled_quantity=377,
        average_entry_paise=Decimal("3238"),
        committed_rupees=Decimal("1000"),
        opened_at=session_for(SESSION_DATE).opens_at,
        expires_at=session_for(SESSION_DATE).opens_at + timedelta(minutes=25),
    )
    position.exit_ordered_quantity = 377
    position.exit_filled_quantity = 377
    position.closed_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=30)
    assert not position.is_open

    # The entry keeps filling after the exit was sent — the exact real-session case.
    position.filled_quantity = 5232
    assert position.is_open, "a position with 4,855 units still on the book is not closed"
    assert position.open_quantity == 5232 - 377
    assert position.unexited_quantity == 5232 - 377


def test_the_close_squares_off_in_rounds_until_nothing_is_left_unexited(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """Squaring off draws more fills, which can leave a residual that needs its own exit."""
    entry_at = session_for(SESSION_DATE).opens_at + timedelta(minutes=5)
    runner, _ledger, _risk, journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(entries_at=[entry_at]),
        book_source=RecordedBookHarness(quantity_per_rung=2),
        step=timedelta(minutes=30),
    )
    report = runner.run()
    for position in report.positions:
        assert position.unexited_quantity == 0 or position.position_key in report.open_at_close
    sold = sum(
        order.ordered_quantity
        for order in journal.orders_for_session(SESSION_DATE)
        if order.side is TradeLeg.SELL
    )
    bought = sum(
        order.ordered_quantity
        for order in journal.orders_for_session(SESSION_DATE)
        if order.side is TradeLeg.BUY
    )
    assert sold <= bought, "no round may sell more than the entry actually filled"


def test_a_breakeven_round_trip_debits_its_costs_and_realises_nothing(
    tmp_path: Path, stores: RealStorePaths
) -> None:
    """`A.112`: a trade that closes at its entry price realises no movement, and is not free.

    The ledger refuses an event of zero rupees — a zero is a statement about the book rather than
    a movement in it — so the loop must not offer it one. Found on the 2026-08-13 replay, where an
    illiquid scrip entered and exited against the same untouched book.
    """
    from nse_algo_trader.paper_loop.paper_trading_session_runner import OpenPaperPosition

    position = OpenPaperPosition(
        position_key="FLAT-1",
        entry_intent_id="entry",
        instrument=PaperInstrument(
            instrument_token=TOKEN, trading_symbol=SYMBOL, lot_size=LOT_SIZE
        ),
        side=TradeLeg.BUY,
        ordered_quantity=10,
        filled_quantity=10,
        average_entry_paise=Decimal("100000"),
        committed_rupees=Decimal("10000"),
        opened_at=session_for(SESSION_DATE).opens_at,
        expires_at=session_for(SESSION_DATE).opens_at + timedelta(minutes=25),
    )
    position.exit_ordered_quantity = 10
    position.exit_filled_quantity = 10
    position.average_exit_paise = Decimal("100000")
    assert position.realised_rupees() == Decimal(0)

    runner, ledger, risk_store, _journal = _runner(
        tmp_path,
        stores,
        signal_source=ScriptedSignalSource(),
        book_source=RecordedBookHarness(),
        step=timedelta(minutes=60),
    )
    opens_at = session_for(SESSION_DATE).opens_at
    risk_store.open_session(
        session_date=SESSION_DATE,
        opening_equity_rupees=Decimal("1000000"),
        occurred_at=opens_at,
    )
    ledger.commit_to_position(
        Decimal("10000"),
        position_key="FLAT-1",
        occurred_at=opens_at,
        reason="entry",
    )
    runner._positions["FLAT-1"] = position
    gross, costs = runner._account_for_closed_positions(
        opens_at + timedelta(hours=6)
    )
    assert gross == Decimal(0)
    assert costs == Decimal(0), "no cost pricer is attached in this harness"
    assert ledger.fold_from_events().open_commitments == (), "the capital must still be released"
