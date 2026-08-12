"""`L1.01` — the charge engine, its breakeven solve and its quantity economics.

The load-bearing test in this file is `test_solved_breakeven_nets_exactly_zero`: it solves for
the exit price and then PRICES that exit through the ordinary path, asserting the round trip
nets zero to the paise. It closes the loop between the two halves of the engine, so a solver
that agrees with a mistaken cost function fails it just as loudly as one that is wrong on its
own — which is the failure mode `O.68` recorded from `L0.34`, where each half was checked
against itself and the pipeline between them was not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    PointInTimeMarketRuleStore,
    RuleFamily,
    RuleScope,
)
from nse_algo_trader.transaction_cost.breakeven_move_solver import (
    solve_breakeven_move,
)
from nse_algo_trader.transaction_cost.broker_fee_schedules import (
    BrokeragePiece,
    BrokerFeeSchedule,
    BrokerFeeScheduleCoverageError,
    BrokerFeeScheduleError,
    broker_fee_schedule,
    known_brokers,
)
from nse_algo_trader.transaction_cost.charge_structure_history import (
    ChargeStructureCoverageError,
    nse_charge_structure_history,
    option_exercise_structure,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    GST_BEARING_COMPONENTS,
    ChargeableSegment,
    ChargeComponent,
    Depository,
    LegApplicability,
    RoundingRule,
    TaxableBase,
    TradeLeg,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    CostCoverageError,
    NseTransactionCostEngine,
    OptionRight,
    TradeSpecification,
    TradeSpecificationError,
    _round_to,
    default_transaction_cost_engine,
)
from nse_algo_trader.transaction_cost.quantity_cost_economics import (
    QuantityCostCurve,
    QuantityEconomicsError,
    capital_bounded_lots,
    cost_curve,
    minimum_viable_quantity,
    segment_access_verdict,
)

TODAY = date(2026, 8, 12)
RUPEE = Decimal(100)


@pytest.fixture(scope="module")
def rule_store() -> PointInTimeMarketRuleStore:
    return seeded_nse_market_rule_store(observe_instrument_master=False)


def exact_rounding_schedule(base: BrokerFeeSchedule) -> BrokerFeeSchedule:
    """A broker that rounds NOTHING, so exact identities can be asserted as identities.

    Every real schedule rounds something, so without this seam the round-trip identity could
    only ever be asserted to a tolerance — and a tolerance wide enough to absorb rupee
    rounding is wide enough to absorb a real defect.
    """
    return replace(
        base,
        broker="exact-rounding-test-broker",
        statutory_rounding=RoundingRule.EXACT,
        brokerage_rounding=RoundingRule.EXACT,
    )


@pytest.fixture(scope="module")
def exact_engine(rule_store: PointInTimeMarketRuleStore) -> NseTransactionCostEngine:
    schedule = exact_rounding_schedule(broker_fee_schedule("zerodha", TODAY))
    return NseTransactionCostEngine(rule_store, schedule=schedule)


@pytest.fixture(scope="module")
def zerodha_engine(rule_store: PointInTimeMarketRuleStore) -> NseTransactionCostEngine:
    return NseTransactionCostEngine(rule_store, broker="zerodha")


def option_trade(**overrides: object) -> TradeSpecification:
    """A NIFTY option round trip: 65 lots, premium 150 -> 170, strike 24000."""
    defaults = {
        "segment": ChargeableSegment.EQUITY_OPTIONS,
        "quantity": 65,
        "entry_price_paise": Decimal(15_000),
        "exit_price_paise": Decimal(17_000),
        "trade_date": TODAY,
        "strike_paise": Decimal(2_400_000),
    }
    return TradeSpecification(**{**defaults, **overrides})  # type: ignore[arg-type]


def cash_trade(**overrides: object) -> TradeSpecification:
    defaults = {
        "segment": ChargeableSegment.EQUITY_INTRADAY,
        "quantity": 100,
        "entry_price_paise": Decimal(140_000),
        "exit_price_paise": Decimal(141_000),
        "trade_date": TODAY,
    }
    return TradeSpecification(**{**defaults, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------ unit: known figures


@pytest.mark.unit
def test_option_round_trip_matches_the_hand_computed_contract_note(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Every line checked against the arithmetic, not against the engine's own total."""
    priced = zerodha_engine.price_round_trip(option_trade())
    by_component = priced.by_component()

    # Brokerage: flat Rs 20 per order, two orders.
    assert by_component[ChargeComponent.BROKERAGE] == Decimal(4_000)
    # STT: 0.15% of the SELL premium turnover (170 x 65 = 11,050), rounded to the rupee.
    assert by_component[ChargeComponent.SECURITIES_TRANSACTION_TAX] == Decimal(1_700)
    # SEBI fee: Rs 10/crore of NOTIONAL (24,000 x 65 = 15.6 lakh), both legs.
    assert by_component[ChargeComponent.SEBI_TURNOVER_FEE] == Decimal(312)
    # Stamp duty: 0.003% of the BUY premium turnover only (29.25 paise exact), billed to
    # the whole paisa — 29, NOT the 0 that nearest-rupee rounding would have made of it.
    assert by_component[ChargeComponent.STAMP_DUTY] == Decimal(29)
    stamp = next(
        line for line in priced.lines if line.component is ChargeComponent.STAMP_DUTY
    )
    assert stamp.exact_paise == Decimal("29.25")


@pytest.mark.unit
def test_the_sebi_fee_is_charged_on_notional_not_premium(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The correction `research/164` and `b28` both got wrong, pinned so it cannot regress."""
    priced = zerodha_engine.price_round_trip(option_trade())
    fee_lines = [
        line for line in priced.lines if line.component is ChargeComponent.SEBI_TURNOVER_FEE
    ]
    assert fee_lines, "the SEBI turnover fee must appear on an option trade"
    for line in fee_lines:
        assert line.taxable_base is TaxableBase.OPTION_NOTIONAL_TURNOVER
        # 24,000 x 65 = 15.6 lakh rupees = 156,000,000 paise. Premium turnover is ~1/160th.
        assert line.taxable_amount_paise == Decimal(156_000_000)


@pytest.mark.unit
def test_gst_excludes_the_transaction_tax_and_the_stamp_duty(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Taxing the whole stack is the commonest error in Indian cost models."""
    for leg_cost in (
        zerodha_engine.price_round_trip(option_trade()).entry,
        zerodha_engine.price_round_trip(option_trade()).exit,
    ):
        gst = next(
            line
            for line in leg_cost.lines
            if line.component is ChargeComponent.GOODS_AND_SERVICES_TAX
        )
        expected_base = sum(
            (
                line.exact_paise
                for line in leg_cost.lines
                if line.component in GST_BEARING_COMPONENTS
            ),
            Decimal(0),
        )
        assert gst.taxable_amount_paise == expected_base
        assert gst.rate == Decimal("0.18")
        excluded = {
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeComponent.COMMODITIES_TRANSACTION_TAX,
            ChargeComponent.STAMP_DUTY,
        }
        assert not (excluded & set(GST_BEARING_COMPONENTS))


@pytest.mark.unit
def test_stamp_duty_falls_on_the_buy_leg_and_stt_on_the_sell_leg(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    priced = zerodha_engine.price_round_trip(option_trade())
    stamp_legs = {
        line.leg for line in priced.lines if line.component is ChargeComponent.STAMP_DUTY
    }
    tax_legs = {
        line.leg
        for line in priced.lines
        if line.component is ChargeComponent.SECURITIES_TRANSACTION_TAX
    }
    assert stamp_legs == {TradeLeg.BUY}
    assert tax_legs == {TradeLeg.SELL}


@pytest.mark.unit
def test_delivery_stt_falls_on_both_legs_but_intraday_only_on_the_sell(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    delivery = zerodha_engine.price_round_trip(
        cash_trade(segment=ChargeableSegment.EQUITY_DELIVERY)
    )
    intraday = zerodha_engine.price_round_trip(cash_trade())
    delivery_legs = {
        line.leg
        for line in delivery.lines
        if line.component is ChargeComponent.SECURITIES_TRANSACTION_TAX
    }
    intraday_legs = {
        line.leg
        for line in intraday.lines
        if line.component is ChargeComponent.SECURITIES_TRANSACTION_TAX
    }
    assert delivery_legs == {TradeLeg.BUY, TradeLeg.SELL}
    assert intraday_legs == {TradeLeg.SELL}


@pytest.mark.unit
def test_intraday_stamp_duty_is_not_the_delivery_rate(
    zerodha_engine: NseTransactionCostEngine,
    rule_store: PointInTimeMarketRuleStore,
) -> None:
    """The five-times-overstated defect `A.90` found, pinned as a regression test."""
    intraday = rule_store.resolve(
        RuleFamily.STAMP_DUTY, TODAY, scope=RuleScope(segment="NSE-MIS")
    ).as_decimal()
    delivery = rule_store.resolve(
        RuleFamily.STAMP_DUTY, TODAY, scope=RuleScope(segment="NSE-CNC")
    ).as_decimal()
    assert intraday == Decimal("0.00003")
    assert delivery == Decimal("0.00015")
    assert delivery == intraday * 5


@pytest.mark.unit
def test_the_options_exchange_charge_is_not_the_cash_rate(
    rule_store: PointInTimeMarketRuleStore,
) -> None:
    """The ~12x defect `A.90` found: an options scope carrying a cash-market rate."""
    options = rule_store.resolve(
        RuleFamily.EXCHANGE_TRANSACTION_CHARGE, TODAY, scope=RuleScope(segment="NFO-OPT")
    ).as_decimal()
    cash = rule_store.resolve(
        RuleFamily.EXCHANGE_TRANSACTION_CHARGE, TODAY, scope=RuleScope(segment="NSE-MIS")
    ).as_decimal()
    assert options > cash * 10
    assert options == Decimal("0.000355299")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("segment", "as_of", "expected_total_per_crore"),
    [
        (ChargeableSegment.EQUITY_INTRADAY, date(2025, 1, 1), Decimal(307)),
        (ChargeableSegment.EQUITY_INTRADAY, date(2026, 8, 12), Decimal(307)),
        (ChargeableSegment.EQUITY_OPTIONS, date(2025, 1, 1), Decimal(3553)),
        (ChargeableSegment.EQUITY_OPTIONS, date(2026, 8, 12), Decimal(3553)),
        (ChargeableSegment.EQUITY_FUTURES, date(2025, 1, 1), Decimal(183)),
        (ChargeableSegment.EQUITY_FUTURES, date(2026, 8, 12), Decimal(183)),
    ],
)
def test_the_ipft_rollback_moved_money_without_changing_the_total(
    rule_store: PointInTimeMarketRuleStore,
    segment: ChargeableSegment,
    as_of: date,
    expected_total_per_crore: Decimal,
) -> None:
    """NSE/FA/73061's own claim, checked: the March-2026 change costs a member nothing.

    Both lines are seeded separately precisely so this can be asserted. A model carrying only
    the total would pass this trivially and would have nothing to say about either change.
    """
    charge = rule_store.resolve(
        RuleFamily.EXCHANGE_TRANSACTION_CHARGE, as_of, scope=segment.rule_scope
    ).as_decimal()
    ipft = rule_store.resolve(
        RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION, as_of, scope=segment.rule_scope
    ).as_decimal()
    assert (charge + ipft) * Decimal(10_000_000) == expected_total_per_crore


@pytest.mark.unit
def test_the_depository_charge_is_flat_in_quantity(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Selling 100 shares and 10,000 shares costs the same at the depository."""
    small = zerodha_engine.price_round_trip(
        cash_trade(
            segment=ChargeableSegment.EQUITY_DELIVERY, quantity=100, depository=Depository.CDSL
        )
    )
    large = zerodha_engine.price_round_trip(
        cash_trade(
            segment=ChargeableSegment.EQUITY_DELIVERY, quantity=10_000, depository=Depository.CDSL
        )
    )
    charge = ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE
    assert small.by_component()[charge] == large.by_component()[charge]
    # Rs 3.50 CDSL + Rs 9.50 Zerodha markup, before GST.
    assert small.by_component()[charge] == Decimal(1_300)


@pytest.mark.unit
def test_the_depository_charge_never_touches_an_intraday_trade(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """An intraday position never reaches the depository, so it cannot be debited."""
    priced = zerodha_engine.price_round_trip(cash_trade(depository=Depository.CDSL))
    assert ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE not in priced.by_component()


@pytest.mark.unit
def test_an_exercised_option_is_taxed_on_intrinsic_value_and_on_the_buyer(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The 121x trap: the pre-2019 rule taxed the full notional, not the intrinsic value."""
    trade = option_trade(
        is_option_exercise=True,
        settlement_price_paise=Decimal(2_420_000),
        option_right=OptionRight.CALL,
    )
    priced = zerodha_engine.price_round_trip(trade)
    tax_lines = [
        line
        for line in priced.lines
        if line.component is ChargeComponent.SECURITIES_TRANSACTION_TAX
    ]
    assert len(tax_lines) == 1
    line = tax_lines[0]
    assert line.leg is TradeLeg.SELL, "the exercise settles the position the buyer opened"
    assert line.taxable_base is TaxableBase.OPTION_INTRINSIC_VALUE
    # Intrinsic 24,200 - 24,000 = 200 rupees, x 65 = 13,000 rupees of base.
    assert line.taxable_amount_paise == Decimal(1_300_000)
    assert line.rate == Decimal("0.0015")


@pytest.mark.unit
def test_an_exercise_is_settled_by_the_exchange_not_traded_out(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """No order is sent at exercise, so no order-driven charge may appear.

    Pricing a full exit leg charged brokerage for an order that never existed and the SEBI
    notional fee a second time — Rs 25.72 of phantom cost on a 75-lot NIFTY exercise.
    """
    exercised = zerodha_engine.price_round_trip(
        option_trade(
            is_option_exercise=True,
            settlement_price_paise=Decimal(2_420_000),
            option_right=OptionRight.CALL,
        )
    )
    settlement_components = {line.component for line in exercised.exit.lines}
    assert settlement_components == {ChargeComponent.SECURITIES_TRANSACTION_TAX}
    for absent in (
        ChargeComponent.BROKERAGE,
        ChargeComponent.SEBI_TURNOVER_FEE,
        ChargeComponent.EXCHANGE_TRANSACTION_CHARGE,
        ChargeComponent.GOODS_AND_SERVICES_TAX,
    ):
        assert absent not in settlement_components


@pytest.mark.unit
def test_an_assigned_writer_pays_nothing_at_settlement_and_keeps_the_sale_tax(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Two parties, one taxpayer. The writer already paid on the premium when they wrote it."""
    assigned = zerodha_engine.price_round_trip(
        option_trade(
            is_short_first=True,
            is_option_exercise=True,
            settlement_price_paise=Decimal(2_420_000),
            option_right=OptionRight.CALL,
        )
    )
    assert assigned.exit.lines == ()
    entry_tax = [
        line
        for line in assigned.entry.lines
        if line.component is ChargeComponent.SECURITIES_TRANSACTION_TAX
    ]
    assert len(entry_tax) == 1, "the writer's sell-side premium STT must survive"
    assert entry_tax[0].taxable_base is TaxableBase.OPTION_PREMIUM_TURNOVER


@pytest.mark.adversarial
def test_a_commodity_exercise_is_taxed_under_its_own_statute(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """CTT, not STT — a different Act, a different rate, and two settlement modes."""
    def commodity_exercise(*, physical: bool) -> TradeSpecification:
        return TradeSpecification(
            segment=ChargeableSegment.COMMODITY_OPTIONS,
            quantity=100,
            entry_price_paise=Decimal(5_000),
            exit_price_paise=Decimal(0),
            trade_date=TODAY,
            strike_paise=Decimal(700_000),
            settlement_price_paise=Decimal(710_000),
            option_right=OptionRight.CALL,
            is_option_exercise=True,
            is_physically_settled=physical,
        )

    cash_settled = zerodha_engine.price_round_trip(commodity_exercise(physical=False))
    (cash_line,) = cash_settled.exit.lines
    assert cash_line.component is ChargeComponent.COMMODITIES_TRANSACTION_TAX
    assert cash_line.taxable_base is TaxableBase.OPTION_INTRINSIC_VALUE
    assert cash_line.rate == Decimal("0.00125")

    delivered = zerodha_engine.price_round_trip(commodity_exercise(physical=True))
    (physical_line,) = delivered.exit.lines
    assert physical_line.taxable_base is TaxableBase.SETTLEMENT_VALUE
    assert physical_line.rate == Decimal("0.000001")
    # The two differ by more than a thousandfold; collapsing them is the common error.
    assert cash_line.exact_paise > physical_line.exact_paise * 10


@pytest.mark.adversarial
def test_a_currency_option_exercise_is_refused_rather_than_taxed(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Currency derivatives attract no transaction tax — including on the exercise path."""
    with pytest.raises(CostCoverageError):
        zerodha_engine.price_round_trip(
            TradeSpecification(
                segment=ChargeableSegment.CURRENCY_OPTIONS,
                quantity=1_000,
                entry_price_paise=Decimal(50),
                exit_price_paise=Decimal(0),
                trade_date=TODAY,
                strike_paise=Decimal(8_750),
                settlement_price_paise=Decimal(8_800),
                option_right=OptionRight.CALL,
                is_option_exercise=True,
            )
        )


@pytest.mark.unit
def test_the_exercise_basis_changed_in_2019_not_2024() -> None:
    """`b28` recorded 2024; the basis moved five years earlier."""
    equity = ChargeableSegment.EQUITY_OPTIONS
    assert (
        option_exercise_structure(equity, date(2019, 8, 31)).taxable_base
        is TaxableBase.SETTLEMENT_VALUE
    )
    for as_of in (date(2019, 9, 1), date(2024, 10, 1)):
        assert (
            option_exercise_structure(equity, as_of).taxable_base
            is TaxableBase.OPTION_INTRINSIC_VALUE
        )


@pytest.mark.unit
def test_currency_derivatives_carry_no_transaction_tax() -> None:
    """A structural absence, not a zero rate — the two must stay distinguishable."""
    history = nse_charge_structure_history()
    components = {
        record.component
        for record in history.components_for(ChargeableSegment.CURRENCY_FUTURES, TODAY)
    }
    assert ChargeComponent.SECURITIES_TRANSACTION_TAX not in components
    assert ChargeComponent.COMMODITIES_TRANSACTION_TAX not in components
    assert ChargeComponent.STAMP_DUTY in components
    with pytest.raises(ChargeStructureCoverageError):
        history.resolve(
            ChargeComponent.SECURITIES_TRANSACTION_TAX, ChargeableSegment.CURRENCY_FUTURES, TODAY
        )


# ------------------------------------------------------------------------ property


@pytest.mark.property
@pytest.mark.parametrize(
    "trade_builder",
    [option_trade, cash_trade],
    ids=["options", "cash-intraday"],
)
def test_solved_breakeven_nets_exactly_zero(
    exact_engine: NseTransactionCostEngine,
    trade_builder: Callable[[],
    TradeSpecification],
) -> None:
    """Solve for the exit, then price that exit: the round trip must net exactly zero.

    Asserted as an identity rather than to a tolerance, which is only possible because the
    injected broker rounds nothing. This is the test that ties the solver to the cost function
    it claims to invert.
    """
    trade = trade_builder()
    solved = solve_breakeven_move(exact_engine, trade)
    assert solved.is_exact
    priced = exact_engine.price_round_trip(
        replace(trade, exit_price_paise=solved.breakeven_price_paise)
    )
    gross = Decimal(trade.quantity) * solved.move_paise
    assert solved.is_within_arithmetic_noise
    assert gross - priced.total_paise == solved.residual_paise


@pytest.mark.property
def test_a_short_first_trade_breaks_even_below_its_entry(
    exact_engine: NseTransactionCostEngine,
) -> None:
    """Direction is not cosmetic: a short pays to buy back, so breakeven is DOWN."""
    trade = option_trade(is_short_first=True, exit_price_paise=Decimal(14_000))
    solved = solve_breakeven_move(exact_engine, trade)
    assert solved.breakeven_price_paise < trade.entry_price_paise
    assert solved.move_paise > 0
    assert solved.is_within_arithmetic_noise


@pytest.mark.property
def test_round_trip_costs_at_least_as_much_as_one_leg(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    trade = option_trade()
    priced = zerodha_engine.price_round_trip(trade)
    assert priced.total_paise >= priced.entry.total_paise
    assert priced.total_paise >= priced.exit.total_paise


@pytest.mark.property
def test_every_charge_line_is_non_negative(zerodha_engine: NseTransactionCostEngine) -> None:
    delivery = cash_trade(segment=ChargeableSegment.EQUITY_DELIVERY)
    for trade in (option_trade(), cash_trade(), delivery):
        for line in zerodha_engine.price_round_trip(trade).lines:
            assert line.exact_paise >= 0
            assert line.billed_paise >= 0


@pytest.mark.property
@pytest.mark.parametrize("quantity", [65, 130, 650, 6_500])
def test_total_cost_never_falls_as_quantity_rises(
    zerodha_engine: NseTransactionCostEngine,
    quantity: int,
) -> None:
    smaller = zerodha_engine.price_round_trip(option_trade(quantity=quantity)).total_paise
    larger = zerodha_engine.price_round_trip(option_trade(quantity=quantity * 2)).total_paise
    assert larger >= smaller


@pytest.mark.property
def test_cost_in_bps_never_rises_as_quantity_rises(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The staircase only ever descends. A rise here means a schedule is malformed."""
    curve = cost_curve(zerodha_engine, option_trade(), lot_size=65, maximum_lots=20)
    assert curve.is_monotonically_cheaper
    assert curve.cheapest.quantity == curve.points[-1].quantity


@pytest.mark.property
def test_cash_delivery_wobbles_by_rounding_and_that_is_told_apart_from_a_defect(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The curve rises slightly on real data, and the cause has to be identifiable.

    Zerodha rounds STT to the whole rupee, so one quantity step can cost a hundredth of a
    basis point more per rupee than the step below it. A boolean "must never rise" would
    either fail on this or be relaxed until it could no longer catch an overlapping brokerage
    piece — which is a rise orders of magnitude larger.
    """
    curve = cost_curve(
        zerodha_engine,
        cash_trade(segment=ChargeableSegment.EQUITY_DELIVERY, depository=Depository.CDSL),
        lot_size=100,
        maximum_lots=20,
    )
    # Whatever rise the real schedule produces here, rounding must be able to explain it.
    assert curve.largest_cost_rise_bps <= curve.rounding_explainable_rise_bps
    assert curve.is_monotonically_cheaper

    # And the discriminator must still catch a rise that rounding CANNOT explain, which is
    # what an overlapping brokerage piece would look like. Asserted on a constructed curve
    # rather than hoping the real data supplies one.
    malformed = QuantityCostCurve(
        segment_value=curve.segment_value,
        price_paise=curve.price_paise,
        lot_size=curve.lot_size,
        points=(
            curve.points[0],
            replace(
                curve.points[1],
                round_trip_cost_paise=curve.points[1].round_trip_cost_paise * 3,
            ),
        ),
    )
    assert not malformed.is_monotonically_cheaper
    assert malformed.largest_cost_rise_bps > malformed.rounding_explainable_rise_bps


@pytest.mark.property
def test_the_minimum_viable_quantity_is_the_smallest_that_clears(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """Bisection over a curve that is monotone only up to rounding must still be exact."""
    template = cash_trade(segment=ChargeableSegment.EQUITY_DELIVERY, depository=Depository.CDSL)
    ceiling = Decimal(30)
    found = minimum_viable_quantity(
        zerodha_engine, template, cost_bps_ceiling=ceiling, lot_size=100, maximum_lots=40
    )
    assert found is not None
    assert (
        zerodha_engine.price_round_trip(
            replace(template, quantity=found)
        ).total_bps_of_turnover
        <= ceiling
    )
    if found > 100:
        below = zerodha_engine.price_round_trip(replace(template, quantity=found - 100))
        assert below.total_bps_of_turnover > ceiling


@pytest.mark.property
def test_belief_time_replay_reproduces_the_earlier_answer(
    rule_store: PointInTimeMarketRuleStore,
) -> None:
    """`known_as_of` before a fact was compiled must hide it, not merely re-sort it."""
    engine = NseTransactionCostEngine(rule_store, broker="zerodha")
    trade = option_trade()
    engine.price_round_trip(trade, known_as_of=date(2026, 8, 12))
    with pytest.raises(CostCoverageError):
        engine.price_round_trip(trade, known_as_of=date(2026, 8, 11))


@pytest.mark.property
def test_the_weakest_grade_governs_the_whole_cost(zerodha_engine: NseTransactionCostEngine) -> None:
    priced = zerodha_engine.price_round_trip(option_trade())
    grades = {line.evidence_grade for line in priced.lines}
    assert priced.weakest_evidence_grade in grades
    assert priced.weakest_evidence_grade is EvidenceGrade.SECONDARY_TRIANGULATED


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_zero_quantity_trade_is_refused() -> None:
    with pytest.raises(TradeSpecificationError, match="quantity must be positive"):
        option_trade(quantity=0)


@pytest.mark.adversarial
def test_an_option_without_a_strike_is_refused() -> None:
    """Defaulting the notional base to the premium would understate the fee ~100x."""
    with pytest.raises(TradeSpecificationError, match="NOTIONAL"):
        TradeSpecification(
            segment=ChargeableSegment.EQUITY_OPTIONS,
            quantity=65,
            entry_price_paise=Decimal(15_000),
            exit_price_paise=Decimal(17_000),
            trade_date=TODAY,
        )


@pytest.mark.adversarial
def test_an_exercise_without_a_settlement_price_is_refused() -> None:
    with pytest.raises(TradeSpecificationError, match="intrinsic value"):
        option_trade(is_option_exercise=True, option_right=OptionRight.CALL)


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("as_of", "should_price"),
    [
        (date(2024, 9, 30), False),
        (date(2024, 10, 1), True),
        (date(2026, 2, 28), True),
        (date(2026, 3, 1), True),
    ],
)
def test_the_slab_era_is_refused_rather_than_flattened(
    rule_store: PointInTimeMarketRuleStore,
    as_of: date,
    should_price: bool,
) -> None:
    """A flat rate applied to the pre-2024 slab schedule is wrong by construction."""
    engine = NseTransactionCostEngine(rule_store, broker="zerodha")
    trade = option_trade(trade_date=as_of)
    if should_price:
        assert engine.price_round_trip(trade).total_paise > 0
    else:
        with pytest.raises(CostCoverageError):
            engine.price_round_trip(trade)


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("as_of", "expected_rate"),
    [
        (date(2026, 3, 31), Decimal("0.001")),
        (date(2026, 4, 1), Decimal("0.0015")),
        (date(2024, 9, 30), Decimal("0.000625")),
        (date(2024, 10, 1), Decimal("0.001")),
    ],
)
def test_rate_changes_take_effect_on_the_right_day_in_both_directions(
    rule_store: PointInTimeMarketRuleStore, as_of: date, expected_rate: Decimal
) -> None:
    """`effective_to` is EXCLUSIVE — the new rule's first day is the old rule's last plus one."""
    resolved = rule_store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, as_of, scope=RuleScope(segment="NFO-OPT")
    )
    assert resolved.as_decimal() == expected_rate


@pytest.mark.adversarial
def test_a_flat_charge_swamps_a_tiny_option_position(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """One unit of an option pays the same flat Rs 20 twice as a full 65-lot does.

    Asserted as a RATIO against the normal size rather than against an absolute figure: the
    ratio is what the flat charge does, and it stays meaningful when the rates change.
    """
    tiny = zerodha_engine.price_round_trip(option_trade(quantity=1))
    normal = zerodha_engine.price_round_trip(option_trade())
    assert tiny.total_bps_of_turnover > normal.total_bps_of_turnover * 40
    # Roughly a third of the premium, round trip, on a single unit.
    assert tiny.total_bps_of_turnover > Decimal(3_000)


@pytest.mark.adversarial
def test_a_one_rupee_cash_trade_costs_almost_nothing_and_that_is_correct(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The opposite case, kept because the intuition it corrects is a real one.

    Zerodha's cash intraday brokerage is a pure percentage below Rs 66,666 of turnover and
    there is no floor, so a Rs 1 trade attracts a fraction of a paisa in every line and bills
    as zero. The cost of trading small in CASH is not the charges — it is the spread, which
    this engine does not model and `L1.05` will.
    """
    trade = cash_trade(quantity=1, entry_price_paise=RUPEE, exit_price_paise=RUPEE)
    priced = zerodha_engine.price_round_trip(trade)
    assert priced.total_paise == Decimal(0)
    assert all(line.exact_paise < Decimal(1) for line in priced.lines)


@pytest.mark.adversarial
def test_a_tiny_option_position_needs_a_move_larger_than_the_premium(
    exact_engine: NseTransactionCostEngine,
) -> None:
    """Not a refusal — a real and useful answer: the premium must rise by about a third.

    A signal promising a few percent cannot be traded at this size, and the engine says so in
    the same units the signal is expressed in.
    """
    solved = solve_breakeven_move(exact_engine, option_trade(quantity=1))
    normal = solve_breakeven_move(exact_engine, option_trade())
    assert solved.move_bps > Decimal(3_000)
    assert solved.move_bps > normal.move_bps * 40


@pytest.mark.adversarial
def test_an_unknown_broker_is_refused_rather_than_defaulted(
    rule_store: PointInTimeMarketRuleStore,
) -> None:
    engine = NseTransactionCostEngine(rule_store, broker="a-broker-that-does-not-exist")
    with pytest.raises(CostCoverageError, match="no fee schedule"):
        engine.price_round_trip(option_trade())


@pytest.mark.adversarial
def test_a_brokerage_schedule_with_a_hole_is_rejected_on_load() -> None:
    """A gap prices nothing and an overlap prices twice; both are caught at construction."""
    with pytest.raises(BrokerFeeScheduleError):
        BrokeragePiece(
            turnover_from_paise=Decimal(100),
            turnover_to_paise=Decimal(50),
            rate=Decimal(0),
            flat_paise=Decimal(0),
        )


@pytest.mark.adversarial
def test_a_broker_schedule_before_its_effective_date_is_refused() -> None:
    with pytest.raises(BrokerFeeScheduleCoverageError):
        broker_fee_schedule("zerodha", date(2001, 1, 1))


@pytest.mark.adversarial
def test_the_engine_refuses_a_date_its_injected_schedule_does_not_cover(
    rule_store: PointInTimeMarketRuleStore,
) -> None:
    schedule = broker_fee_schedule("zerodha", TODAY)
    engine = NseTransactionCostEngine(rule_store, schedule=schedule)
    with pytest.raises(CostCoverageError, match="does not cover"):
        engine.price_round_trip(option_trade(trade_date=date(2015, 1, 1)))


@pytest.mark.adversarial
def test_brokerage_is_charged_per_order_not_per_leg(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """A leg split across three orders pays the flat charge three times."""
    single = zerodha_engine.price_round_trip(option_trade())
    split = zerodha_engine.price_round_trip(option_trade(orders_per_leg=3))
    brokerage = ChargeComponent.BROKERAGE
    assert split.by_component()[brokerage] == single.by_component()[brokerage] * 3


@pytest.mark.adversarial
def test_leg_applicability_covers_exactly_one_leg_each_way() -> None:
    assert LegApplicability.BUY_ONLY.covers(TradeLeg.BUY)
    assert not LegApplicability.BUY_ONLY.covers(TradeLeg.SELL)
    assert LegApplicability.SELL_ONLY.covers(TradeLeg.SELL)
    assert not LegApplicability.SELL_ONLY.covers(TradeLeg.BUY)
    assert all(LegApplicability.BOTH_LEGS.covers(leg) for leg in TradeLeg)


@pytest.mark.adversarial
@pytest.mark.parametrize("segment", list(ChargeableSegment))
def test_a_segment_either_prices_or_names_the_rate_it_is_missing(
    zerodha_engine: NseTransactionCostEngine, segment: ChargeableSegment
) -> None:
    """`R.10`: no segment may be quietly unpriceable — it prices, or it says what it lacks.

    All eight price as of this slice. Commodity refused until the MCX transaction charge was
    sourced (MCX/F&A/631/2024) rather than being priced as if the exchange took nothing —
    which is the behaviour this test would have pinned had the rate stayed unavailable.
    """
    trade = TradeSpecification(
        segment=segment,
        quantity=10,
        entry_price_paise=Decimal(10_000),
        exit_price_paise=Decimal(10_100),
        trade_date=TODAY,
        strike_paise=Decimal(1_000_000) if segment.is_option else None,
    )
    assert zerodha_engine.price_round_trip(trade).exact_total_paise > 0


@pytest.mark.adversarial
def test_every_broker_publishes_a_price_for_every_segment() -> None:
    for broker in known_brokers():
        schedule = broker_fee_schedule(broker, TODAY)
        for segment in ChargeableSegment:
            assert schedule.pieces_for(segment)


# ------------------------------------------------------------- quantity economics


@pytest.mark.unit
def test_minimum_viable_quantity_finds_the_first_clearing_size(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    ceiling = Decimal(50)
    found = minimum_viable_quantity(
        zerodha_engine, option_trade(), cost_bps_ceiling=ceiling, lot_size=65, maximum_lots=40
    )
    assert found is not None
    at_found = zerodha_engine.price_round_trip(option_trade(quantity=found))
    assert at_found.total_bps_of_turnover <= ceiling
    one_lot_smaller = found - 65
    if one_lot_smaller > 0:
        below = zerodha_engine.price_round_trip(option_trade(quantity=one_lot_smaller))
        assert below.total_bps_of_turnover > ceiling


@pytest.mark.unit
def test_an_unreachable_ceiling_returns_none_rather_than_the_least_bad_size(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    assert (
        minimum_viable_quantity(
            zerodha_engine,
            option_trade(),
            cost_bps_ceiling=Decimal("0.001"),
            lot_size=65,
            maximum_lots=10,
        )
        is None
    )


@pytest.mark.unit
def test_a_segment_is_closed_when_costs_need_more_size_than_the_capital_affords(
    zerodha_engine: NseTransactionCostEngine,
) -> None:
    """The arithmetic that decides whether a small account can trade a segment at all."""
    verdict = segment_access_verdict(
        zerodha_engine,
        option_trade(),
        capital_paise=Decimal(1_000_000),
        cost_bps_ceiling=Decimal(20),
        lot_size=65,
        maximum_lots=60,
    )
    assert not verdict.is_open
    assert "affords" in verdict.reason or "no quantity" in verdict.reason


@pytest.mark.adversarial
@pytest.mark.parametrize(("lot_size", "maximum_lots"), [(0, 5), (65, 0), (-1, 5)])
def test_a_nonsense_lot_ladder_is_refused(
    zerodha_engine: NseTransactionCostEngine,
    lot_size: int,
    maximum_lots: int,
) -> None:
    with pytest.raises(QuantityEconomicsError):
        cost_curve(zerodha_engine, option_trade(), lot_size=lot_size, maximum_lots=maximum_lots)


@pytest.mark.adversarial
def test_capital_bounded_lots_floors_rather_than_rounds() -> None:
    assert capital_bounded_lots(Decimal(999), Decimal(10), 10) == 9
    assert capital_bounded_lots(Decimal(1_000), Decimal(10), 10) == 10
    with pytest.raises(QuantityEconomicsError):
        capital_bounded_lots(Decimal(1_000), Decimal(0), 10)


@pytest.mark.adversarial
def test_the_default_engine_builds_and_prices() -> None:
    assert default_transaction_cost_engine().price_round_trip(option_trade()).total_paise > 0


# ------------------------------------------- regressions from the adversarial review (A.93)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("segment", "rupees_per_crore"),
    [
        (ChargeableSegment.EQUITY_INTRADAY, Decimal("306.99")),
        (ChargeableSegment.EQUITY_FUTURES, Decimal("182.99")),
        (ChargeableSegment.EQUITY_OPTIONS, Decimal("3552.99")),
        (ChargeableSegment.CURRENCY_FUTURES, Decimal(35)),
        (ChargeableSegment.CURRENCY_OPTIONS, Decimal(3110)),
        # MCX publishes per LAKH: Rs 2.10 and Rs 41.80 are Rs 210 and Rs 4,180 per crore.
        (ChargeableSegment.COMMODITY_FUTURES, Decimal(210)),
        (ChargeableSegment.COMMODITY_OPTIONS, Decimal(4180)),
    ],
)
def test_every_exchange_charge_matches_the_rupees_per_crore_its_own_citation_states(
    rule_store: PointInTimeMarketRuleStore,
    segment: ChargeableSegment,
    rupees_per_crore: Decimal,
) -> None:
    """Two currency rates were seeded 100x and 10x below the figure in their own source string.

    Nothing downstream could detect it: the record resolved, reported itself covered, and
    carried a citation that contradicted its own value. This test reads the value and the
    stated rupees-per-crore against each other for every segment.
    """
    resolved = rule_store.resolve(
        RuleFamily.EXCHANGE_TRANSACTION_CHARGE, TODAY, scope=segment.rule_scope
    )
    assert resolved.as_decimal() * Decimal(10_000_000) == rupees_per_crore


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exact_paise", "expected_billed"),
    [(Decimal(150), Decimal(200)), (Decimal(250), Decimal(300)), (Decimal(1850), Decimal(1900))],
)
def test_a_half_rupee_charge_rounds_up_as_the_cited_rule_says(
    exact_paise: Decimal, expected_billed: Decimal
) -> None:
    """`Decimal.quantize` defaults to banker's rounding; the cited rule is 'fifty paise up'.

    Half-rupee STT is not exotic — intraday at 0.025% lands on it at every odd multiple of
    Rs 2,000 of turnover — and banker's rounding billed Rs 2.50 as Rs 2.00.
    """
    assert _round_to(exact_paise, RoundingRule.NEAREST_RUPEE) == expected_billed


@pytest.mark.property
@pytest.mark.parametrize("price_paise", [Decimal(1_000), Decimal(15_000), Decimal(140_000)])
def test_the_exact_cost_curve_never_rises(
    zerodha_engine: NseTransactionCostEngine, price_paise: Decimal
) -> None:
    """The real invariant, with no tolerance to argue about.

    Exact cost per rupee is (ad-valorem rates) + (flat charges / turnover), so it is strictly
    decreasing in quantity. A rise here cannot be rounding — it means a schedule overlaps or a
    flat charge is being scaled by quantity.
    """
    curve = cost_curve(
        zerodha_engine,
        cash_trade(entry_price_paise=price_paise, exit_price_paise=price_paise),
        lot_size=1,
        maximum_lots=120,
    )
    assert curve.largest_exact_cost_rise_bps == 0
    assert curve.is_monotonically_cheaper


@pytest.mark.property
@pytest.mark.parametrize(
    ("price_paise", "ceiling_bps"),
    [
        (Decimal(140_000), Decimal(5)),
        (Decimal(140_000), Decimal(8)),
        (Decimal(15_000), Decimal(10)),
        (Decimal(1_000), Decimal(20)),
    ],
)
def test_the_minimum_viable_quantity_agrees_with_a_brute_force_scan(
    zerodha_engine: NseTransactionCostEngine, price_paise: Decimal, ceiling_bps: Decimal
) -> None:
    """Bisection must find the SAME answer an exhaustive scan does, not merely a clearing one.

    Bisecting the BILLED cost produced three distinct wrong answers on real schedules — a
    non-minimal quantity, a quantity in the wrong basin, and `None` where the true answer was
    one lot whose charges all rounded away. The earlier tests missed all three because they
    only checked that the answer cleared and that one lot smaller did not, which is exactly
    the local check a rounding sawtooth defeats.
    """
    template = cash_trade(entry_price_paise=price_paise, exit_price_paise=price_paise)
    maximum_lots = 400
    solved = minimum_viable_quantity(
        zerodha_engine,
        template,
        cost_bps_ceiling=ceiling_bps,
        lot_size=1,
        maximum_lots=maximum_lots,
    )
    scanned: int | None = None
    for quantity in range(1, maximum_lots + 1):
        priced = zerodha_engine.price_round_trip(replace(template, quantity=quantity))
        if priced.exact_bps_of_turnover <= ceiling_bps:
            scanned = quantity
            break
    assert solved == scanned
