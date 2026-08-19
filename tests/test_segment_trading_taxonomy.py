"""Tests for the six-segment taxonomy — `L5.29`, written before the implementation.

`A.01` names six segment holons; `ChargeableSegment` names eight CHARGE scopes and deliberately
refuses to split index from stock derivatives. These tests pin the relationship between the two
axes, because conflating them causes one of two specific errors: giving index and stock derivatives
different tax rates (what `ChargeableSegment`'s docstring was written to prevent), or giving them
the same settlement obligation (what `A.01`'s index/stock split exists to prevent).
"""

from __future__ import annotations

import pytest

from nse_algo_trader.segment_bots.segment_trading_taxonomy import (
    SEGMENT_INSTRUMENT_FACTS,
    SegmentInstrumentFacts,
    SegmentTaxonomyError,
    SettlementStyle,
    TradeableUnitDenominator,
    TradingSegment,
    VenueCalendar,
    instrument_facts_for,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment


def test_there_are_exactly_the_six_segments_that_a01_names() -> None:
    """`A.01` struck five and fixed six.

    A seventh member is a plan change, not a code change.
    """
    assert {segment.value for segment in TradingSegment} == {
        "cash_intraday",
        "index_options",
        "stock_options",
        "index_futures",
        "stock_futures",
        "commodity_mcx",
    }


def test_every_segment_has_facts_and_the_mapping_is_total() -> None:
    """A segment with no facts cannot be gated, sized, or refused — it must not be constructible."""
    assert set(SEGMENT_INSTRUMENT_FACTS) == set(TradingSegment)
    for segment in TradingSegment:
        assert isinstance(instrument_facts_for(segment), SegmentInstrumentFacts)


def test_every_segment_maps_onto_a_chargeable_segment_the_cost_engine_prices() -> None:
    """The cost engine already prices all eight scopes; the six must land inside them."""
    for segment in TradingSegment:
        assert instrument_facts_for(segment).chargeable_segment in set(ChargeableSegment)


def test_index_and_stock_derivatives_share_a_charge_scope_but_not_a_segment() -> None:
    """The exact distinction `ChargeableSegment`'s docstring refuses to encode, encoded here.

    Same statutory rate — so the same charge scope — and different risk shapes, so different
    segments. Both halves must hold or one of the two errors has been committed.
    """
    index_options = instrument_facts_for(TradingSegment.INDEX_OPTIONS)
    stock_options = instrument_facts_for(TradingSegment.STOCK_OPTIONS)
    assert index_options.chargeable_segment == stock_options.chargeable_segment
    # Same charge scope, different risk shape — settlement is where the split earns itself.
    assert index_options.settlement != stock_options.settlement

    index_futures = instrument_facts_for(TradingSegment.INDEX_FUTURES)
    stock_futures = instrument_facts_for(TradingSegment.STOCK_FUTURES)
    assert index_futures.chargeable_segment == stock_futures.chargeable_segment


def test_stock_derivatives_settle_physically_and_index_derivatives_do_not() -> None:
    """SEBI has mandated physical delivery for stock F&O since 2018; index F&O is cash-settled.

    This is the risk-shape difference that justifies the index/stock split existing at all.
    """
    assert (
        instrument_facts_for(TradingSegment.STOCK_OPTIONS).settlement
        == SettlementStyle.PHYSICAL_DELIVERY
    )
    assert (
        instrument_facts_for(TradingSegment.STOCK_FUTURES).settlement
        == SettlementStyle.PHYSICAL_DELIVERY
    )
    assert (
        instrument_facts_for(TradingSegment.INDEX_OPTIONS).settlement
        == SettlementStyle.CASH_SETTLED
    )
    assert (
        instrument_facts_for(TradingSegment.INDEX_FUTURES).settlement
        == SettlementStyle.CASH_SETTLED
    )


def test_no_option_segment_may_carry_overnight() -> None:
    """`R.01`, without exception: options — index AND stock — never carry.

    Enforced on the segment rather than by an operator remembering, which is the whole point of
    putting it here.
    """
    # Review finding m16: this loop used to guard its assert on `denominator is PREMIUM`, so the
    # commodity_mcx iteration asserted nothing at all. Every non-cash segment is now asserted
    # unconditionally, which is what R.01 actually says.
    for segment in TradingSegment:
        if segment is TradingSegment.CASH_INTRADAY:
            continue
        assert not instrument_facts_for(segment).overnight_carry_permitted, segment


def test_options_are_premium_denominated_and_cash_is_price_denominated() -> None:
    """`D.01` records that conflating the two denominators was an error. `L11.106` is the rule."""
    assert (
        instrument_facts_for(TradingSegment.INDEX_OPTIONS).denominator
        is TradeableUnitDenominator.PREMIUM
    )
    assert (
        instrument_facts_for(TradingSegment.STOCK_OPTIONS).denominator
        is TradeableUnitDenominator.PREMIUM
    )
    assert (
        instrument_facts_for(TradingSegment.CASH_INTRADAY).denominator
        is TradeableUnitDenominator.PRICE
    )
    for segment in (TradingSegment.INDEX_FUTURES, TradingSegment.STOCK_FUTURES):
        assert instrument_facts_for(segment).denominator is (
            TradeableUnitDenominator.CONTRACT_NOTIONAL
        )


def test_only_option_segments_require_a_strike() -> None:
    """Mirrors `PricedSignal.__post_init__`: SEBI turnover fee is charged on notional."""
    for segment in TradingSegment:
        facts = instrument_facts_for(segment)
        assert facts.strike_required == (facts.denominator is TradeableUnitDenominator.PREMIUM)


def test_only_option_segments_need_greeks() -> None:
    """`B1` gates arming on this flag: no IV surface exists yet."""
    needing = {
        segment for segment in TradingSegment if instrument_facts_for(segment).requires_greeks
    }
    assert TradingSegment.INDEX_OPTIONS in needing
    assert TradingSegment.STOCK_OPTIONS in needing
    assert TradingSegment.CASH_INTRADAY not in needing


def test_cash_is_the_only_segment_without_an_expiry() -> None:
    """Roll and square-off obligations exist only where an expiry does."""
    without_expiry = {
        segment for segment in TradingSegment if not instrument_facts_for(segment).has_expiry
    }
    assert without_expiry == {TradingSegment.CASH_INTRADAY}


def test_mcx_is_the_only_segment_on_a_different_venue_calendar() -> None:
    """`A.01`: MCX is a second VENUE, not merely a sixth segment. Its evening session runs later."""
    non_nse = {
        segment
        for segment in TradingSegment
        if instrument_facts_for(segment).venue_calendar is not VenueCalendar.NSE
    }
    assert non_nse == {TradingSegment.COMMODITY_MCX}
    assert instrument_facts_for(TradingSegment.COMMODITY_MCX).venue_calendar is VenueCalendar.MCX


def test_cash_intraday_is_the_only_segment_permitted_to_carry_overnight() -> None:
    """`R.01`: overnight carry is CASH ONLY, and only on explicit supervisor promotion."""
    carrying = {
        segment
        for segment in TradingSegment
        if instrument_facts_for(segment).overnight_carry_permitted
    }
    assert carrying == {TradingSegment.CASH_INTRADAY}


def test_every_fact_record_cites_the_source_of_its_regulatory_claim() -> None:
    """`R.23e`: a constant in an engine is a defect unless it is a physical or regulatory fact.

    These records are entirely regulatory facts, which is the one exemption — so each must name
    where it comes from, or it is an undocumented magic value wearing a dataclass.
    """
    for segment in TradingSegment:
        facts = instrument_facts_for(segment)
        assert len(facts.regulatory_source) > 20, f"{segment} states no source"


def test_an_unknown_segment_is_refused_rather_than_defaulted() -> None:
    """A missing fact record must raise, never fall back to a plausible default.

    A default here would silently give a segment somebody else's settlement style or carry rule,
    which is the most expensive possible wrong answer in this table.
    """
    with pytest.raises(SegmentTaxonomyError, match="no instrument facts"):
        instrument_facts_for("not_a_segment")  # type: ignore[arg-type]


# ------------------------------------------------------ regressions from the 2026-08-17 review


EXPECTED_CHARGE_SCOPES = {
    TradingSegment.CASH_INTRADAY: ChargeableSegment.EQUITY_INTRADAY,
    TradingSegment.INDEX_OPTIONS: ChargeableSegment.EQUITY_OPTIONS,
    TradingSegment.STOCK_OPTIONS: ChargeableSegment.EQUITY_OPTIONS,
    TradingSegment.INDEX_FUTURES: ChargeableSegment.EQUITY_FUTURES,
    TradingSegment.STOCK_FUTURES: ChargeableSegment.EQUITY_FUTURES,
    TradingSegment.COMMODITY_MCX: ChargeableSegment.COMMODITY_FUTURES,
}


@pytest.mark.parametrize(("segment", "expected"), sorted(EXPECTED_CHARGE_SCOPES.items()))
def test_each_segment_maps_to_its_exact_charge_scope(
    segment: TradingSegment, expected: ChargeableSegment
) -> None:
    """Review finding M10: asserting only enum MEMBERSHIP left every mapping unpinned.

    Mutating `COMMODITY_MCX` to `CURRENCY_FUTURES` survived the whole suite, and it is silent as
    well as wrong — CDS attracts no transaction tax, so an MCX trade priced as currency simply comes
    out cheaper (₹48.36 against ₹64.40 per ₹1,00,000 round trip, through this project's own engine).
    """
    assert instrument_facts_for(segment).chargeable_segment is expected


def test_a_carried_cash_position_is_charged_as_delivery_not_intraday() -> None:
    """Review finding C3: the flat mapping understated a carried position's cost roughly 4x.

    STT follows the settlement type, not the MIS/CNC order tag. `R.01` itself says cash is
    promotable net of the DELIVERY cost structure, so the intraday scope was contradicting the rule
    it encoded.
    """
    cash = instrument_facts_for(TradingSegment.CASH_INTRADAY)
    intraday = cash.chargeable_segment_when(carried_overnight=False)
    carried = cash.chargeable_segment_when(carried_overnight=True)
    assert intraday is ChargeableSegment.EQUITY_INTRADAY
    assert carried is ChargeableSegment.EQUITY_DELIVERY


@pytest.mark.parametrize(
    "segment", [seg for seg in TradingSegment if seg is not TradingSegment.CASH_INTRADAY]
)
def test_asking_a_non_carrying_segment_for_its_carried_scope_is_refused(
    segment: TradingSegment,
) -> None:
    """Answering with the intraday scope would hand back a number the caller must not have."""
    with pytest.raises(SegmentTaxonomyError, match="may not carry overnight"):
        instrument_facts_for(segment).chargeable_segment_when(carried_overnight=True)


def test_carry_permission_and_carried_scope_cannot_drift_apart() -> None:
    """The two facts are one fact; separating them is how a carried position gets mispriced."""
    with pytest.raises(SegmentTaxonomyError, match="must name the charge scope"):
        SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_INTRADAY,
            carried_chargeable_segment=None,
            denominator=TradeableUnitDenominator.PRICE,
            settlement=SettlementStyle.CASH_SETTLED,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=False,
            strike_required=False,
            requires_greeks=False,
            overnight_carry_permitted=True,
            regulatory_source="a source long enough to satisfy the R.23e sourcing requirement",
        )


def test_the_index_options_source_no_longer_cites_the_stock_options_circular() -> None:
    """Review finding M7: CIR/DNPD/6/2010 is titled 'European Style Stock Options'.

    NSE index options launched European and cash-settled on 2001-06-04, nine years earlier, so the
    circular never governed them.
    """
    index_source = instrument_facts_for(TradingSegment.INDEX_OPTIONS).regulatory_source
    assert "2001-06-04" in index_source
    stock_source = instrument_facts_for(TradingSegment.STOCK_OPTIONS).regulatory_source
    assert "CIR/DNPD/6/2010" in stock_source, "the circular belongs on the STOCK options record"


def test_the_mcx_source_admits_that_settlement_is_not_uniform() -> None:
    """Review finding M8: crude oil, natural gas and the MCX index futures are cash-settled.

    The single value stays — it is the conservative one — but a simplification that does not say it
    is a simplification is indistinguishable from a fact.
    """
    source = instrument_facts_for(TradingSegment.COMMODITY_MCX).regulatory_source
    assert "CASH-SETTLED" in source
    assert "B8" in source, "the open blocker must be named where the simplification is made"
