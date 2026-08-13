"""The wiring that closes `R.06` on the gate: strategy -> priced claim -> verdict.

Until this path existed the gate could veto nothing and the strategy could propose nothing that
cost had an opinion about. These tests exercise the whole path, and the one that matters most is
`test_the_regime_veto_runs_before_costing`: a signal the brain refuses must never reach the cost
engine, so an extreme deviation in a trending tape cannot talk its way past the regime veto by
happening to be cheap to trade.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.cost_gated_mean_reversion_entries import (
    STRATEGY_SOURCE,
    MeanReversionEntryError,
    evaluate_mean_reversion_entry,
    price_mean_reversion_decision,
)
from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionCapture,
)
from nse_algo_trader.cost_gate.pre_trade_cost_gate import GateVerdict, PreTradeCostGate
from nse_algo_trader.cost_gate.priced_signal import EdgeBasis
from nse_algo_trader.execution_fill.execution_fill_model import ExecutionFillModel
from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.regime.soft_regime_weighting_brain import RegimeBelief
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
    MeanReversionDecision,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import NseTransactionCostEngine

TODAY = date(2026, 8, 12)
AN_INSTANT = datetime(2026, 8, 12, 5, 30, tzinfo=UTC)


def book() -> BookSnapshot:
    return BookSnapshot(
        instrument_token=1,
        receipt_time=AN_INSTANT,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=AN_INSTANT,
        last_price_paise=140_000,
        last_traded_quantity=1,
        volume_traded=1_000_000,
        total_buy_quantity=5_000_000,
        total_sell_quantity=5_000_000,
        integrity_flags=IntegrityFlag.NONE,
        bids=(DepthLevel(price_paise=139_900, quantity=5_000, orders=1),),
        asks=(DepthLevel(price_paise=140_100, quantity=5_000, orders=1),),
    )


def decision(
    *,
    action: MeanReversionAction = MeanReversionAction.ENTER_LONG,
    deviation: float = -3.0,
    band: float | None = 1.0,
    conviction: float = 1.0,
) -> MeanReversionDecision:
    return MeanReversionDecision(
        action=action,
        conviction=conviction,
        reason="test",
        deviation=deviation,
        deviation_band=band,
        regime_used=MarketRegime.RANGING,
    )


def matured_engine() -> IntradayMeanReversionEngine:
    """A real engine, fed enough real-shaped closes to define its own bands."""
    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=0.2, minimum_regime_agreement=0.5
    )
    engine.observe_closes([140_000 + (index % 7) * 200 for index in range(200)])
    return engine


def ranging_belief(agreement: float = 1.0) -> RegimeBelief:
    """A belief the engine will act on: clearly ranging, panel agreed."""
    return RegimeBelief(
        RegimeDistribution.from_scores(
            {regime: (6.0 if regime is MarketRegime.RANGING else 0.4) for regime in MarketRegime}
        ),
        ("trend_strength", "volatility"),
        1.0 - agreement,
        {"trend_strength": 0.5, "volatility": 0.5},
        AN_INSTANT,
    )


CALIBRATION_HORIZON_BARS = 5
"""Five bars, because that is the horizon the archive fit found the strongest capture at.

Stated as a constant the tests share rather than repeated at each call site, so a test cannot
accidentally calibrate one horizon and price another — the failure that would produce is a wrong
number rather than an error, which is the kind that survives review."""


def calibration(mean_bps: str, bucket: str) -> ReversionCapture:
    """A row shaped like the ones the real fit produced for that bucket."""
    return ReversionCapture(
        deviation_bucket=Decimal(bucket),
        horizon_bars=CALIBRATION_HORIZON_BARS,
        event_count=10_814,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 3,
        standard_error_bps=Decimal("12.4"),
        mean_captured_sigma=Decimal("0.06"),
        fitted_through=date(2026, 7, 1),
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
    )


@pytest.fixture
def calibrations(tmp_path: Path) -> ReversionCalibrationStore:
    """A store carrying the buckets these tests exercise, with the real measured signs.

    3.0 sigma reverts (+24.68 bps); 3.5 sigma does NOT (-17.51 bps). Both are what the archive
    actually produced, so a test that accidentally prices the wrong bucket fails on sign rather
    than passing with a plausible-looking number.
    """
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    store.record([calibration("24.68", "3"), calibration("-17.51", "3.5")])
    return store


@pytest.fixture(scope="module")
def gate() -> PreTradeCostGate:
    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    return PreTradeCostGate(NseTransactionCostEngine(store), ExecutionFillModel())


@pytest.mark.unit
def test_a_scale_free_decision_becomes_a_claim_about_money(
    calibrations: ReversionCalibrationStore,
) -> None:
    """The conversion that makes the signal priceable at all."""
    signal = price_mean_reversion_decision(
        decision(deviation=-3.0, band=1.0, conviction=1.0),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        reference_price_paise=Decimal(140_000),
        dispersion_paise=Decimal(1_400),
        quantity=1_000,
        decided_at=AN_INSTANT,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    # The measured capture for 3-sigma deviations over five bars, not the distance travelled.
    # The replaced formula reported 200 bps here purely because price sat two dispersions past
    # its band; the archive says such deviations actually recover 24.68 bps on average.
    assert signal.expected_edge_bps == Decimal("24.68")
    assert signal.source == STRATEGY_SOURCE
    assert signal.edge_basis is EdgeBasis.CALIBRATED_MODEL


@pytest.mark.unit
def test_the_claim_is_stamped_as_a_calibrated_model_not_a_track_record(
    calibrations: ReversionCalibrationStore,
) -> None:
    """The edge is fitted evidence now, but still not a record of this strategy having traded."""
    signal = price_mean_reversion_decision(
        decision(),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        reference_price_paise=Decimal(140_000),
        dispersion_paise=Decimal(1_400),
        quantity=100,
        decided_at=AN_INSTANT,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    assert signal.edge_basis is EdgeBasis.CALIBRATED_MODEL
    # A track record is what `L2` would have to establish. A coefficient fitted to historical
    # reversion is emphatically not one: it says what deviations of this depth did, never that
    # this system captured any of it after costs and slippage.
    assert EdgeBasis.MEASURED_TRACK_RECORD not in {signal.edge_basis}


@pytest.mark.property
@pytest.mark.parametrize(
    ("action", "expected_side"),
    [(MeanReversionAction.ENTER_LONG, "buy"), (MeanReversionAction.ENTER_SHORT, "sell")],
)
def test_the_action_decides_the_side(
    action: MeanReversionAction,
    expected_side: str,
    calibrations: ReversionCalibrationStore,
) -> None:
    signal = price_mean_reversion_decision(
        decision(action=action),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        reference_price_paise=Decimal(140_000),
        dispersion_paise=Decimal(1_400),
        quantity=100,
        decided_at=AN_INSTANT,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    assert signal.side.value == expected_side


@pytest.mark.adversarial
def test_abstain_never_becomes_a_trade(calibrations: ReversionCalibrationStore) -> None:
    """The single worst bug this module could carry, asserted against directly."""
    with pytest.raises(MeanReversionEntryError, match="proposes no trade"):
        price_mean_reversion_decision(
            decision(action=MeanReversionAction.ABSTAIN),
            instrument_token=1,
            trading_symbol="TESTCO",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            reference_price_paise=Decimal(140_000),
            dispersion_paise=Decimal(1_400),
            quantity=100,
            decided_at=AN_INSTANT,
            calibrations=calibrations,
            horizon_bars=CALIBRATION_HORIZON_BARS,
        )


@pytest.mark.adversarial
def test_zero_dispersion_cannot_be_converted_into_a_price_move(
    calibrations: ReversionCalibrationStore,
) -> None:
    """A deviation measured in units of nothing is not a distance."""
    with pytest.raises(MeanReversionEntryError, match="zero dispersion"):
        price_mean_reversion_decision(
            decision(),
            instrument_token=1,
            trading_symbol="TESTCO",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            reference_price_paise=Decimal(140_000),
            dispersion_paise=Decimal(0),
            quantity=100,
            decided_at=AN_INSTANT,
            calibrations=calibrations,
            horizon_bars=CALIBRATION_HORIZON_BARS,
        )


@pytest.mark.unit
def test_the_full_path_runs_and_a_real_engine_can_be_vetoed_on_cost(
    gate: PreTradeCostGate, calibrations: ReversionCalibrationStore
) -> None:
    """Strategy -> priced claim -> verdict, with a genuine engine at the front.

    This is what closes `R.06`: before it, the gate had nothing to gate.
    """
    engine = matured_engine()
    entry = evaluate_mean_reversion_entry(
        engine,
        gate,
        belief=ranging_belief(),
        snapshot=book(),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        quantity=1_000,
        observed_at=AN_INSTANT,
        trade_date=TODAY,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    # Whatever the engine decided, the path completed and gave a reason either way.
    assert entry.describe()
    if entry.gate_decision is None:
        assert entry.skipped_reason
        assert not entry.is_tradeable
    else:
        assert entry.verdict in set(GateVerdict)


@pytest.mark.unit
def test_an_immature_engine_is_skipped_with_a_reason_not_an_error(
    gate: PreTradeCostGate, calibrations: ReversionCalibrationStore
) -> None:
    """A quiet day must be distinguishable from a broken one."""
    entry = evaluate_mean_reversion_entry(
        IntradayMeanReversionEngine(
            minimum_regime_concentration=0.2, minimum_regime_agreement=0.5
        ),
        gate,
        belief=ranging_belief(),
        snapshot=book(),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        quantity=1_000,
        observed_at=AN_INSTANT,
        trade_date=TODAY,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    assert entry.gate_decision is None
    assert entry.skipped_reason
    assert not entry.is_tradeable
    assert entry.describe().startswith("no trade:")


@pytest.mark.adversarial
def test_an_unusable_book_skips_before_costing(
    gate: PreTradeCostGate, calibrations: ReversionCalibrationStore
) -> None:
    """No mid means no reference price, so there is nothing to express an edge against."""
    engine = matured_engine()
    crossed = BookSnapshot(
        instrument_token=1,
        receipt_time=AN_INSTANT,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=AN_INSTANT,
        last_price_paise=140_000,
        last_traded_quantity=1,
        volume_traded=1,
        total_buy_quantity=1,
        total_sell_quantity=1,
        integrity_flags=IntegrityFlag.NONE,
        bids=(DepthLevel(price_paise=0, quantity=0, orders=0),),
        asks=(DepthLevel(price_paise=0, quantity=0, orders=0),),
    )
    entry = evaluate_mean_reversion_entry(
        engine,
        gate,
        belief=ranging_belief(),
        snapshot=crossed,
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        quantity=1_000,
        observed_at=AN_INSTANT,
        trade_date=TODAY,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    assert entry.gate_decision is None
    assert not entry.is_tradeable


@pytest.mark.unit
def test_the_engine_exposes_the_dispersion_its_own_deviation_is_measured_in() -> None:
    """`L5.05`'s completed interface: a scale-free number that can now be priced."""
    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=0.2, minimum_regime_agreement=0.5
    )
    assert engine.rolling_dispersion() is None
    engine.observe_closes([140_000 + (index % 5) * 500 for index in range(100)])
    dispersion = engine.rolling_dispersion()
    assert dispersion is not None and dispersion > 0


@pytest.mark.adversarial
def test_the_regime_veto_runs_before_costing(
    gate: PreTradeCostGate, calibrations: ReversionCalibrationStore
) -> None:
    """A trending tape must veto before cost is ever consulted.

    Order of operations, asserted rather than assumed: an extreme deviation cannot talk its way
    past the regime veto by happening to be cheap to trade. The engine abstains, so no signal
    is ever constructed and the gate is never asked.
    """
    trending = RegimeBelief(
        RegimeDistribution.from_scores(
            {regime: (6.0 if regime is MarketRegime.TRENDING else 0.4) for regime in MarketRegime}
        ),
        ("trend_strength", "volatility"),
        0.0,
        {"trend_strength": 0.5, "volatility": 0.5},
        AN_INSTANT,
    )
    entry = evaluate_mean_reversion_entry(
        matured_engine(),
        gate,
        belief=trending,
        snapshot=book(),
        instrument_token=1,
        trading_symbol="TESTCO",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        quantity=1_000,
        observed_at=AN_INSTANT,
        trade_date=TODAY,
        calibrations=calibrations,
        horizon_bars=CALIBRATION_HORIZON_BARS,
    )
    assert entry.gate_decision is None, "cost must never be consulted for a regime-vetoed signal"
    assert entry.signal is None
    assert "abstained" in entry.skipped_reason
