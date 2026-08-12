"""`L0.15` — reconciliation, tested on the shapes real brokers actually produced.

The measured case on 2026-08-11 is the backbone: Kite held 2026-08-07 and Angel did not,
and on 08-11 both agreed on every price while differing on volume by 192,685.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.bitemporal_bar_store import BarRecord
from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.historical_bars.cross_source_bar_reconciler import (
    Disagreement,
    ReconciliationError,
    classify_disagreement,
    fetch_and_reconcile,
    reconcile_bars,
)
from nse_algo_trader.historical_bars.historical_bar_source import (
    BarInstrument,
    BarInterval,
    BarRequest,
    HistoricalBarSourceError,
)

IST = ZoneInfo("Asia/Kolkata")
AUGUST_SEVENTH = datetime(2026, 8, 7, tzinfo=IST)
AUGUST_ELEVENTH = datetime(2026, 8, 11, tzinfo=IST)

RELIANCE = BarInstrument(
    "NSE",
    "CASH",
    "RELIANCE",
    {BrokerName.ZERODHA_KITE: "738561", BrokerName.ANGEL_ONE: "2885"},
)


def _bar(moment: datetime, close: str = "1320.6", volume: int = 8_508_600) -> BarRecord:
    return BarRecord(
        exchange="NSE",
        segment="CASH",
        tradingsymbol="RELIANCE",
        instrument_token=738561,
        bar_interval="day",
        bar_timestamp=moment,
        available_from=moment + timedelta(days=1),
        open_price=Decimal("1326.6"),
        high_price=Decimal("1328.7"),
        low_price=Decimal("1314.1"),
        close_price=Decimal(close),
        volume=volume,
        open_interest=None,
    )


@pytest.mark.unit
def test_the_union_is_taken_so_a_single_source_bar_survives() -> None:
    """Intersecting would discard exactly the bar failover exists to recover."""
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_SEVENTH), _bar(AUGUST_ELEVENTH)],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH)],
        }
    )
    assert [bar.bar.bar_timestamp for bar in report.bars] == [AUGUST_SEVENTH, AUGUST_ELEVENTH]
    assert [bar.bar.bar_timestamp for bar in report.gap_filled_bars] == [AUGUST_SEVENTH]


@pytest.mark.unit
def test_agreeing_prices_with_differing_volume_is_not_reported_as_agreement() -> None:
    """The measured 08-11 case, and the dangerous one.

    Anything that checked `close` alone would conclude the sources matched and import a
    volume it never verified.
    """
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_ELEVENTH, volume=8_508_600)],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH, volume=8_701_285)],
        }
    )
    (reconciled,) = report.bars
    assert reconciled.disagreement is Disagreement.VOLUME_ONLY
    assert not reconciled.disagreement.is_integrity_alarm
    assert len(report.volume_disagreements) == 1
    assert report.price_disagreements == []


@pytest.mark.unit
def test_the_rejected_alternative_is_kept_not_discarded() -> None:
    """When a disagreement is investigated later, the rejected value IS the investigation."""
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_ELEVENTH, volume=8_508_600)],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH, volume=8_701_285)],
        }
    )
    (reconciled,) = report.bars
    (alternative_broker, alternative_bar) = reconciled.alternatives[0]
    assert alternative_broker is not reconciled.chosen_source
    assert {reconciled.bar.volume, alternative_bar.volume} == {8_508_600, 8_701_285}


@pytest.mark.unit
def test_a_price_disagreement_is_an_integrity_alarm() -> None:
    """Two brokers reporting different traded prices means at least one is wrong."""
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_ELEVENTH, close="1320.6")],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH, close="1319.0")],
        }
    )
    (reconciled,) = report.bars
    assert reconciled.disagreement is Disagreement.PRICE
    assert reconciled.disagreement.is_integrity_alarm
    assert len(report.price_disagreements) == 1
    assert "PRICE DISAGREEMENTS" in report.describe()


@pytest.mark.unit
def test_preference_is_earned_by_coverage_not_declared() -> None:
    """Kite wins here because it held more bars, not because it is named Kite."""
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_SEVENTH), _bar(AUGUST_ELEVENTH)],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH)],
        }
    )
    shared = next(bar for bar in report.bars if bar.bar.bar_timestamp == AUGUST_ELEVENTH)
    assert shared.chosen_source is BrokerName.ZERODHA_KITE
    assert report.reliability[BrokerName.ZERODHA_KITE].bars_offered == 2
    assert report.reliability[BrokerName.ANGEL_ONE].bars_offered == 1


@pytest.mark.unit
def test_reconciliation_is_stable_regardless_of_source_order() -> None:
    """A result that depended on dict ordering would drift between runs for no reason."""
    kite_bars = [_bar(AUGUST_SEVENTH), _bar(AUGUST_ELEVENTH)]
    angel_bars = [_bar(AUGUST_ELEVENTH, volume=8_701_285)]
    forward = reconcile_bars({BrokerName.ZERODHA_KITE: kite_bars, BrokerName.ANGEL_ONE: angel_bars})
    reversed_order = reconcile_bars(
        {BrokerName.ANGEL_ONE: angel_bars, BrokerName.ZERODHA_KITE: kite_bars}
    )
    assert [b.chosen_source for b in forward.bars] == [b.chosen_source for b in reversed_order.bars]
    assert [b.bar.volume for b in forward.bars] == [b.bar.volume for b in reversed_order.bars]


@pytest.mark.unit
def test_being_the_only_source_does_not_count_as_agreement() -> None:
    """Agreeing with nobody is not evidence of accuracy."""
    report = reconcile_bars({BrokerName.ZERODHA_KITE: [_bar(AUGUST_SEVENTH)]})
    reliability = report.reliability[BrokerName.ZERODHA_KITE]
    assert reliability.bars_offered == 1
    assert reliability.bars_agreeing == 0


@pytest.mark.unit
def test_one_source_failing_does_not_fail_the_call_but_is_recorded() -> None:
    """A permanently broken broker must not masquerade as one with no data."""

    class Failing:
        broker = BrokerName.ANGEL_ONE

        def supports(self, interval: BarInterval) -> bool:
            return True

        def fetch_bars(self, request: BarRequest) -> list[BarRecord]:
            raise HistoricalBarSourceError("angel one refused: Invalid session token")

    class Working:
        broker = BrokerName.ZERODHA_KITE

        def supports(self, interval: BarInterval) -> bool:
            return True

        def fetch_bars(self, request: BarRequest) -> list[BarRecord]:
            return [_bar(AUGUST_ELEVENTH)]

    request = BarRequest(
        RELIANCE, BarInterval.ONE_DAY, AUGUST_SEVENTH.date(), AUGUST_ELEVENTH.date()
    )
    report = fetch_and_reconcile([Working(), Failing()], request)
    assert len(report.bars) == 1
    assert BrokerName.ANGEL_ONE in report.source_failures
    assert "FAILED" in report.describe()


@pytest.mark.unit
def test_every_source_failing_raises_rather_than_returning_empty() -> None:
    """Zero bars because everything broke must not look like zero bars in the window."""

    class Failing:
        def __init__(self, broker: BrokerName) -> None:
            self.broker = broker

        def supports(self, interval: BarInterval) -> bool:
            return True

        def fetch_bars(self, request: BarRequest) -> list[BarRecord]:
            raise HistoricalBarSourceError("down")

    request = BarRequest(
        RELIANCE, BarInterval.ONE_DAY, AUGUST_SEVENTH.date(), AUGUST_ELEVENTH.date()
    )
    with pytest.raises(ReconciliationError):
        fetch_and_reconcile(
            [Failing(BrokerName.ZERODHA_KITE), Failing(BrokerName.ANGEL_ONE)],
            request,
        )


@pytest.mark.unit
def test_no_sources_configured_is_an_error() -> None:
    request = BarRequest(
        RELIANCE, BarInterval.ONE_DAY, AUGUST_SEVENTH.date(), AUGUST_ELEVENTH.date()
    )
    with pytest.raises(ReconciliationError):
        fetch_and_reconcile([], request)


@pytest.mark.unit
def test_a_lone_candidate_cannot_disagree() -> None:
    assert classify_disagreement([_bar(AUGUST_ELEVENTH)]) is Disagreement.NONE
    assert classify_disagreement([]) is Disagreement.NONE


@pytest.mark.unit
def test_identical_bars_from_both_sources_read_as_agreement() -> None:
    report = reconcile_bars(
        {
            BrokerName.ZERODHA_KITE: [_bar(AUGUST_ELEVENTH)],
            BrokerName.ANGEL_ONE: [_bar(AUGUST_ELEVENTH)],
        }
    )
    (reconciled,) = report.bars
    assert reconciled.disagreement is Disagreement.NONE
    assert not reconciled.was_single_sourced
