"""Choosing how long to hold, from the horizons the calibration was actually fitted on (`A.115`).

The selector decides a real thing — when a position leaves — so the tests below are about the two
ways it could decide wrongly: preferring a horizon on an optimistic reading of thin evidence, and
substituting a number when the evidence is absent.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionCapture,
)
from nse_algo_trader.cost_gate.per_instrument_reversion_horizon_selector import (
    NoHorizonHasEvidenceError,
    PerInstrumentReversionHorizonSelector,
)

AS_OF = date(2026, 8, 11)
FITTED_THROUGH = AS_OF - timedelta(days=1)


def _capture(
    *,
    bucket: int,
    horizon: int,
    mean_bps: str,
    standard_error_bps: str,
    symbol: str | None = None,
) -> ReversionCapture:
    return ReversionCapture(
        deviation_bucket=Decimal(bucket),
        horizon_bars=horizon,
        event_count=5_000,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 2,
        standard_error_bps=Decimal(standard_error_bps),
        mean_captured_sigma=Decimal("0.05"),
        fitted_through=FITTED_THROUGH,
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
        trading_symbol=symbol,
    )


def _store(tmp_path: Path, captures: list[ReversionCapture]) -> ReversionCalibrationStore:
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    store.record(captures)
    return store


def test_the_horizon_with_the_best_lower_confidence_capture_per_bar_wins(tmp_path: Path) -> None:
    """40 bps over 10 bars is 4/bar; 20 over 3 is ~6.7/bar. The shorter one is the better trade."""
    store = _store(
        tmp_path,
        [
            _capture(bucket=2, horizon=3, mean_bps="24", standard_error_bps="2"),
            _capture(bucket=2, horizon=10, mean_bps="44", standard_error_bps="2"),
        ],
    )
    selected = PerInstrumentReversionHorizonSelector(store).horizon_for(
        deviation_sigma=Decimal("2.1"), trading_symbol="RELIANCE", as_of=AS_OF
    )
    assert selected.horizon_bars == 3
    assert selected.score_bps_per_bar == Decimal(20) / Decimal(3)
    assert [horizon for horizon, _ in selected.considered] == [3, 10]


def test_the_pessimistic_end_of_the_claim_decides_not_the_mean(tmp_path: Path) -> None:
    """A wide error bar loses to a tight one even when its MEAN is higher — thin evidence pays less.

    Both claim a mean of 30 bps. On the mean alone the 3-bar horizon wins outright, 10/bar against
    6/bar. On the pessimistic end the wide error bar collapses: 30-2*9=12 over 3 bars is 4/bar,
    while 30-2*2=26 over 5 bars is 5.2/bar, so the tighter claim takes it.
    """
    store = _store(
        tmp_path,
        [
            _capture(bucket=2, horizon=3, mean_bps="30", standard_error_bps="9"),
            _capture(bucket=2, horizon=5, mean_bps="30", standard_error_bps="2"),
        ],
    )
    selected = PerInstrumentReversionHorizonSelector(store).horizon_for(
        deviation_sigma=Decimal("2.1"), trading_symbol="RELIANCE", as_of=AS_OF
    )
    assert selected.horizon_bars == 5, "the tighter claim wins despite the lower mean"
    assert selected.is_distinguishable_from_zero


def test_a_tie_is_broken_by_the_shorter_horizon(tmp_path: Path) -> None:
    """Holding longer for the same measured capture occupies capacity and buys nothing."""
    store = _store(
        tmp_path,
        [
            _capture(bucket=2, horizon=3, mean_bps="18", standard_error_bps="1.5"),
            _capture(bucket=2, horizon=10, mean_bps="60", standard_error_bps="5"),
        ],
    )
    selected = PerInstrumentReversionHorizonSelector(store).horizon_for(
        deviation_sigma=Decimal("2.1"), trading_symbol="RELIANCE", as_of=AS_OF
    )
    assert selected.score_bps_per_bar == Decimal(15) / Decimal(3)
    assert selected.horizon_bars == 3


def test_different_deviations_select_different_horizons(tmp_path: Path) -> None:
    """The dispersion the whole change exists for: two instruments, two buckets, two horizons."""
    store = _store(
        tmp_path,
        [
            _capture(bucket=1, horizon=10, mean_bps="40", standard_error_bps="2"),
            _capture(bucket=1, horizon=3, mean_bps="6", standard_error_bps="1"),
            _capture(bucket=3, horizon=3, mean_bps="36", standard_error_bps="2"),
            _capture(bucket=3, horizon=10, mean_bps="40", standard_error_bps="2"),
        ],
    )
    selector = PerInstrumentReversionHorizonSelector(store)
    shallow = selector.horizon_for(
        deviation_sigma=Decimal("1.2"), trading_symbol="SHALLOW", as_of=AS_OF
    )
    deep = selector.horizon_for(deviation_sigma=Decimal("3.1"), trading_symbol="DEEP", as_of=AS_OF)
    assert shallow.horizon_bars == 10
    assert deep.horizon_bars == 3
    assert shallow.horizon_bars != deep.horizon_bars


def test_no_evidence_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    """The policy horizon must not creep back in through a fallback on unfitted instruments."""
    store = _store(tmp_path, [_capture(bucket=2, horizon=3, mean_bps="24", standard_error_bps="2")])
    with pytest.raises(NoHorizonHasEvidenceError, match="no fitted horizon covers"):
        PerInstrumentReversionHorizonSelector(store).horizon_for(
            deviation_sigma=Decimal("9.5"), trading_symbol="UNFITTED", as_of=AS_OF
        )


def test_an_empty_store_is_refused_with_the_reason(tmp_path: Path) -> None:
    store = ReversionCalibrationStore(tmp_path / "empty.sqlite3")
    with pytest.raises(NoHorizonHasEvidenceError, match="no horizon fitted"):
        PerInstrumentReversionHorizonSelector(store).horizon_for(
            deviation_sigma=Decimal("2.0"), trading_symbol="ANY", as_of=AS_OF
        )


def test_a_calibration_fitted_after_the_decision_is_invisible(tmp_path: Path) -> None:
    """Point-in-time applies to the CHOICE of horizon, not only to the capture it looks up."""
    future = ReversionCapture(
        deviation_bucket=Decimal(2),
        horizon_bars=3,
        event_count=5_000,
        mean_captured_bps=Decimal(24),
        median_captured_bps=Decimal(12),
        standard_error_bps=Decimal(2),
        mean_captured_sigma=Decimal("0.05"),
        fitted_through=AS_OF + timedelta(days=30),
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
    )
    store = _store(tmp_path, [future])
    with pytest.raises(NoHorizonHasEvidenceError):
        PerInstrumentReversionHorizonSelector(store).horizon_for(
            deviation_sigma=Decimal("2.1"), trading_symbol="RELIANCE", as_of=AS_OF
        )


def test_the_selection_reads_back_with_every_horizon_it_weighed(tmp_path: Path) -> None:
    """`L13.29`: a holding time an operator cannot reconstruct is one they cannot argue with."""
    store = _store(
        tmp_path,
        [
            _capture(bucket=2, horizon=3, mean_bps="24", standard_error_bps="2"),
            _capture(bucket=2, horizon=10, mean_bps="44", standard_error_bps="2"),
        ],
    )
    described = (
        PerInstrumentReversionHorizonSelector(store)
        .horizon_for(deviation_sigma=Decimal("2.1"), trading_symbol="RELIANCE", as_of=AS_OF)
        .describe()
    )
    assert "3 bars" in described
    assert "3b=" in described and "10b=" in described
