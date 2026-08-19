"""`L1.01`'s carried state — the engine measuring its own error against real contract notes.

The property under test is not "does it store rows". It is that the ledger REFUSES to claim
agreement until the data can distinguish agreement from rounding noise, and that it never
turns a residual into a correction factor.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
    ChargeObservation,
    ChargeReconciliationLedger,
    ReconciliationVerdict,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    ChargeComponent,
    RoundingRule,
    TradeLeg,
)

TODAY = date(2026, 8, 12)


def observation(reference: str, modelled: str, actual: str) -> ChargeObservation:
    return ChargeObservation(
        broker="zerodha",
        order_reference=reference,
        leg=TradeLeg.SELL,
        component=ChargeComponent.SECURITIES_TRANSACTION_TAX,
        segment=ChargeableSegment.EQUITY_OPTIONS,
        trade_date=TODAY,
        modelled_paise=Decimal(modelled),
        actual_paise=Decimal(actual),
        recorded_at=TODAY,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> ChargeReconciliationLedger:
    return ChargeReconciliationLedger(tmp_path / "reconciliation.sqlite3")


@pytest.mark.unit
def test_an_empty_ledger_reports_nothing_rather_than_agreement(
    ledger: ChargeReconciliationLedger,
) -> None:
    """`R.04`: thin data gates the verdict, never the algorithm."""
    assert ledger.observation_count() == 0
    assert ledger.reconcile() == ()
    assert ledger.drifting_components() == ()


@pytest.mark.unit
def test_a_single_observation_is_not_enough_to_claim_agreement(
    ledger: ChargeReconciliationLedger,
) -> None:
    ledger.record([observation("order-1", "1700", "1700")])
    (reconciliation,) = ledger.reconcile()
    assert reconciliation.verdict is ReconciliationVerdict.UNVERIFIED
    assert reconciliation.observation_count == 1


@pytest.mark.unit
def test_agreement_is_claimed_once_the_interval_is_narrower_than_the_rounding(
    ledger: ChargeReconciliationLedger,
) -> None:
    """The threshold is DERIVED — no `n >= 30` anywhere.

    Twenty observations that agree exactly give a zero-width interval, which is narrower than
    any rounding floor, so agreement becomes claimable on the evidence rather than on a count
    somebody chose.
    """
    ledger.record([observation(f"order-{index}", "1700", "1700") for index in range(20)])
    (reconciliation,) = ledger.reconcile()
    assert reconciliation.verdict is ReconciliationVerdict.AGREES
    assert reconciliation.mean_residual_paise == Decimal(0)


@pytest.mark.unit
def test_a_consistent_offset_is_reported_as_drift_and_names_the_worst_order(
    ledger: ChargeReconciliationLedger,
) -> None:
    """A rate that is wrong shows up as a mean residual rounding cannot explain."""
    ledger.record(
        [observation(f"order-{index}", "1700", "1600") for index in range(15)]
        + [observation("order-worst", "1700", "1000")]
    )
    (reconciliation,) = ledger.reconcile()
    assert reconciliation.verdict is ReconciliationVerdict.DRIFTS
    assert reconciliation.mean_residual_paise > Decimal(50)
    assert reconciliation.worst_order_reference == "order-worst"
    assert "RATE is wrong" in reconciliation.explanation


@pytest.mark.unit
def test_noisy_residuals_around_zero_stay_unverified_rather_than_agreeing(
    ledger: ChargeReconciliationLedger,
) -> None:
    """Wide scatter means the mean is not yet known, and saying AGREES would be a claim."""
    ledger.record(
        [
            observation(f"order-{index}", "1700", str(1700 + offset))
            for index, offset in enumerate((-900, 900, -850, 880, -910, 870))
        ]
    )
    (reconciliation,) = ledger.reconcile()
    assert reconciliation.verdict is ReconciliationVerdict.UNVERIFIED
    assert reconciliation.confidence_half_width_paise > Decimal(1)


@pytest.mark.unit
def test_a_rupee_rounding_broker_is_allowed_a_wider_residual(
    ledger: ChargeReconciliationLedger,
) -> None:
    """Half a rupee of residual is rounding when a broker rounds to the rupee, drift when not."""
    ledger.record([observation(f"order-{index}", "1700", "1660") for index in range(12)])
    coarse = ledger.reconcile(
        rounding_by_component={
            ChargeComponent.SECURITIES_TRANSACTION_TAX: RoundingRule.NEAREST_RUPEE
        }
    )
    fine = ledger.reconcile(
        rounding_by_component={
            ChargeComponent.SECURITIES_TRANSACTION_TAX: RoundingRule.NEAREST_PAISA
        }
    )
    assert coarse[0].verdict is ReconciliationVerdict.AGREES
    assert fine[0].verdict is ReconciliationVerdict.DRIFTS


@pytest.mark.unit
def test_recording_the_same_order_twice_changes_nothing(ledger: ChargeReconciliationLedger) -> None:
    """A daily runner that re-reads yesterday's notes must not double-count them."""
    ledger.record([observation("order-1", "1700", "1690")])
    ledger.record([observation("order-1", "1700", "1690")])
    assert ledger.observation_count() == 1


@pytest.mark.unit
def test_the_ledger_never_offers_a_correction_factor(ledger: ChargeReconciliationLedger) -> None:
    """The public surface has no way to feed a residual back into a rate, by design.

    Asserted structurally rather than by comment: if a `correct`/`adjust`/`calibrate` method
    ever appears, this fails, and whoever added it has to argue for it.
    """
    forbidden = {"correct", "adjust", "calibrate", "apply_correction", "corrected_rate"}
    assert not forbidden & set(dir(ledger))


@pytest.mark.adversarial
def test_an_impossible_confidence_level_is_refused(tmp_path: Path) -> None:
    for confidence in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(ValueError, match="confidence"):
            ChargeReconciliationLedger(tmp_path / "x.sqlite3", confidence=confidence)


@pytest.mark.adversarial
def test_residual_sign_says_which_way_the_model_is_wrong(
    ledger: ChargeReconciliationLedger,
) -> None:
    """Positive means the engine OVERSTATES — the safe direction, but still a defect."""
    ledger.record([observation("over", "1700", "1600"), observation("under", "1600", "1700")])
    (reconciliation,) = ledger.reconcile()
    assert reconciliation.mean_residual_paise == Decimal(0)
    assert observation("over", "1700", "1600").residual_paise > 0
    assert observation("under", "1600", "1700").residual_paise < 0


# ------------------------------------------- regressions from the adversarial review (A.93)


@pytest.mark.unit
def test_the_actionable_subset_knows_the_brokers_rounding(tmp_path: Path) -> None:
    """`drifting_components()` had no way to be told the rounding, so it reported rounding.

    For the default broker — which rounds STT to the whole rupee — every reconciliation was
    measured against a half-paisa band, so pure rounding residuals came back as evidence that
    a statutory RATE was wrong. Two callers of the same ledger disagreed, because only the
    dashboard passed the map.
    """
    ledger = ChargeReconciliationLedger(
        tmp_path / "aware.sqlite3",
        rounding_by_component={
            ChargeComponent.SECURITIES_TRANSACTION_TAX: RoundingRule.NEAREST_RUPEE
        },
    )
    ledger.record(
        [
            observation(f"order-{index}", "1700", str(1700 - (12 if index % 2 else 13)))
            for index in range(20)
        ]
    )
    assert ledger.reconcile()[0].verdict is ReconciliationVerdict.AGREES
    assert ledger.drifting_components() == ()


@pytest.mark.unit
def test_two_identical_observations_do_not_decide_a_borderline_drift(tmp_path: Path) -> None:
    """A zero-width interval from two samples is a claim the data cannot support.

    An algorithm trading the same size repeatedly produces identical residuals by
    construction, so the sample standard deviation is exactly zero and the confidence interval
    collapsed — letting two orders establish a drift. The dispersion is now floored at the
    billing granularity, which is the smallest spread the process itself can produce.
    """
    ledger = ChargeReconciliationLedger(tmp_path / "pair.sqlite3")
    ledger.record([observation("a", "1001", "1000"), observation("b", "1001", "1000")])
    borderline = ledger.reconcile()[0]
    assert borderline.residual_dispersion_paise == Decimal(0)
    assert borderline.confidence_half_width_paise > 0
    assert borderline.verdict is ReconciliationVerdict.UNVERIFIED

    # The same residual, once enough of it has accrued, IS drift.
    ledger.record([observation(f"c{index}", "1001", "1000") for index in range(20)])
    assert ledger.reconcile()[0].verdict is ReconciliationVerdict.DRIFTS


@pytest.mark.unit
def test_the_residual_statistics_are_exact(tmp_path: Path) -> None:
    """Routing residuals through `float` lost the last bits of a value compared with `<=`."""
    ledger = ChargeReconciliationLedger(tmp_path / "exact.sqlite3")
    ledger.record(
        [
            observation("p", "1.1", "1"),
            observation("q", "1.2", "1"),
            observation("r", "1.3", "1"),
        ]
    )
    assert ledger.reconcile()[0].mean_residual_paise == Decimal("0.2")
