"""`L1.02` + `L1.03` — the gate, and the contract that finally gives it something to gate.

The tests that carry the design:

- `test_the_safety_margin_grows_with_uncertainty_and_nobody_chose_it` — the reason there is no
  1.5x multiplier anywhere. The margin IS the measured interval, so it scales itself.
- `test_unpriceable_is_not_a_veto` — the distinction that turns a data outage into silence
  rather than into a flood of confident rejections.
- `test_resize_finds_the_largest_size_that_still_clears` — RESIZE is a solve over the same
  engines that produced the verdict, not a suggestion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationMaturity,
    ReversionCapture,
)
from nse_algo_trader.cost_gate.pre_trade_cost_gate import (
    GateVerdict,
    PreTradeCostGate,
)
from nse_algo_trader.cost_gate.priced_signal import (
    EdgeBasis,
    EdgeConfidence,
    PricedSignal,
    PricedSignalError,
    edge_from_calibrated_reversion,
)
from nse_algo_trader.cost_gate.tradeable_ticket_preconditions import PreconditionName
from nse_algo_trader.execution_fill.execution_fill_model import ExecutionFillModel
from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import NseTransactionCostEngine

TODAY = date(2026, 8, 12)
AN_INSTANT = datetime(2026, 8, 12, 5, 30, tzinfo=UTC)


def book(
    *,
    bids: tuple[tuple[int, int], ...] = ((139_900, 5_000), (139_800, 10_000)),
    asks: tuple[tuple[int, int], ...] = ((140_100, 5_000), (140_200, 10_000)),
    total_buy_quantity: int = 5_000_000,
    total_sell_quantity: int = 5_000_000,
) -> BookSnapshot:
    """A liquid book around a 1,400-rupee mid with a tight spread."""
    return BookSnapshot(
        instrument_token=1,
        receipt_time=AN_INSTANT,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=AN_INSTANT,
        last_price_paise=140_000,
        last_traded_quantity=1,
        volume_traded=1_000_000,
        total_buy_quantity=total_buy_quantity,
        total_sell_quantity=total_sell_quantity,
        integrity_flags=IntegrityFlag.NONE,
        bids=tuple(DepthLevel(price_paise=p, quantity=q, orders=1) for p, q in bids),
        asks=tuple(DepthLevel(price_paise=p, quantity=q, orders=1) for p, q in asks),
    )


def signal(**overrides: object) -> PricedSignal:
    defaults = {
        "instrument_token": 1,
        "trading_symbol": "TESTCO",
        "segment": ChargeableSegment.EQUITY_INTRADAY,
        "side": TradeLeg.BUY,
        "decided_at": AN_INSTANT,
        "reference_price_paise": Decimal(140_000),
        "expected_edge_bps": Decimal(50),
        "proposed_quantity": 1_000,
        "edge_basis": EdgeBasis.STRATEGY_HYPOTHESIS,
        "source": "test-strategy",
    }
    return PricedSignal(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def gate() -> PreTradeCostGate:
    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    return PreTradeCostGate(NseTransactionCostEngine(store), ExecutionFillModel())


# ------------------------------------------------------------------- the priced signal


@pytest.mark.unit
def test_a_signal_must_state_an_edge_and_who_is_claiming_it() -> None:
    """An edge nobody can attribute is an edge nobody can disprove."""
    with pytest.raises(PricedSignalError, match="no source"):
        signal(source="  ")
    with pytest.raises(PricedSignalError, match="expected edge"):
        signal(expected_edge_bps=Decimal(0))
    with pytest.raises(PricedSignalError, match="expected edge"):
        signal(expected_edge_bps=Decimal(-5))


@pytest.mark.unit
def test_a_zero_edge_signal_is_refused_up_front_rather_than_vetoed_as_too_costly() -> None:
    """Two different failures that must not be reported as one.

    A signal expecting nothing cannot clear any hurdle, but saying "cost vetoed it" would blame
    the cost engine for a strategy that never made a claim.
    """
    with pytest.raises(PricedSignalError, match="not a signal"):
        signal(expected_edge_bps=Decimal(0))


@pytest.mark.unit
def test_an_option_signal_without_a_strike_cannot_be_priced() -> None:
    with pytest.raises(PricedSignalError, match="notional"):
        signal(segment=ChargeableSegment.EQUITY_OPTIONS, strike_paise=None)


@pytest.mark.property
def test_resizing_keeps_the_claim_and_changes_only_the_size() -> None:
    """Edge in bps is size-independent; cost is not. That asymmetry is the whole resize."""
    original = signal(proposed_quantity=1_000)
    smaller = original.resized_to(250)
    assert smaller.expected_edge_bps == original.expected_edge_bps
    assert smaller.proposed_quantity == 250
    assert smaller.notional_paise == original.notional_paise / 4
    assert smaller.edge_basis is original.edge_basis


def capture(mean_bps: str, *, bucket: str = "3", horizon: int = 5, events: int = 5_000,
            standard_error: str = "5") -> ReversionCapture:
    """A calibration row shaped like the ones the archive actually produced."""
    return ReversionCapture(
        deviation_bucket=Decimal(bucket),
        horizon_bars=horizon,
        event_count=events,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(mean_bps) / 3,
        standard_error_bps=Decimal(standard_error),
        mean_captured_sigma=Decimal("0.06"),
        fitted_through=date(2026, 7, 1),
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
    )


@pytest.mark.unit
def test_the_mean_reversion_adapter_claims_the_measured_reversion_scaled_by_conviction() -> None:
    """The edge is now a fitted coefficient, not a restatement of how far price travelled."""
    edge = edge_from_calibrated_reversion(
        capture("24.68"), deviation=3.0, conviction=0.5
    )
    assert edge == Decimal("12.34")


@pytest.mark.adversarial
def test_a_negative_measured_reversion_is_refused_rather_than_priced() -> None:
    """Real buckets came out negative — 3.5 sigma continues rather than reverts at short horizons.

    Refused at the adapter rather than returned as a negative edge, so it surfaces as "this
    strategy has no measurable edge here" instead of being vetoed downstream as though the
    transaction cost were what killed it. Those are different facts and lead to different fixes.
    """
    with pytest.raises(PricedSignalError, match="no edge to price"):
        edge_from_calibrated_reversion(
            capture("-17.51", bucket="3.5"), deviation=3.5, conviction=1.0
        )


@pytest.mark.adversarial
def test_a_calibration_from_another_deviation_bucket_is_refused() -> None:
    """Capture is non-monotone and changes sign, so a neighbouring bucket is not evidence."""
    with pytest.raises(PricedSignalError, match="non-linear in depth"):
        edge_from_calibrated_reversion(capture("24.68", bucket="3"), deviation=2.5, conviction=1.0)


@pytest.mark.property
def test_the_conservative_claim_never_exceeds_the_expected_one() -> None:
    """If it did, every gate downstream would be reading the interval backwards."""
    row = capture("24.68", standard_error="9.8")
    expected = edge_from_calibrated_reversion(row, deviation=3.0, conviction=1.0)
    conservative = edge_from_calibrated_reversion(
        row, deviation=3.0, conviction=1.0, price_uncertainty=EdgeConfidence.CONSERVATIVE
    )
    assert conservative < expected
    assert conservative == row.lower_confidence_bps


# ------------------------------------------------------------------------- the gate


@pytest.mark.unit
def test_a_generous_edge_passes_at_the_proposed_size(gate: PreTradeCostGate) -> None:
    decision = gate.evaluate(signal(expected_edge_bps=Decimal(200)), book(), trade_date=TODAY)
    assert decision.verdict is GateVerdict.PASS
    assert decision.is_tradeable
    assert decision.approved_quantity == 1_000
    assert decision.net_edge_bps is not None and decision.net_edge_bps > 0
    assert decision.shortfall_bps is None


@pytest.mark.unit
def test_a_thin_edge_is_vetoed_and_says_how_much_it_was_short(gate: PreTradeCostGate) -> None:
    decision = gate.evaluate(signal(expected_edge_bps=Decimal(1)), book(), trade_date=TODAY)
    assert decision.verdict is GateVerdict.VETO
    assert not decision.is_tradeable
    assert decision.approved_quantity == 0
    assert decision.shortfall_bps is not None and decision.shortfall_bps > 0
    assert decision.hurdle is not None


@pytest.mark.property
def test_the_safety_margin_grows_with_uncertainty_and_nobody_chose_it(
    gate: PreTradeCostGate,
) -> None:
    """Why there is no 1.5x multiplier anywhere in this engine.

    The margin IS the measured cost interval. A small order against a deep book barely widens
    it; a large order that exhausts the visible book widens it a lot. One rule, two very
    different hurdles, and no constant that has to be wrong for one of them.
    """
    small = gate.evaluate(
        signal(expected_edge_bps=Decimal(500), proposed_quantity=100), book(), trade_date=TODAY
    )
    large = gate.evaluate(
        signal(expected_edge_bps=Decimal(500), proposed_quantity=200_000),
        book(),
        trade_date=TODAY,
    )
    assert small.hurdle is not None and large.hurdle is not None
    assert large.hurdle.uncertainty_bps > small.hurdle.uncertainty_bps
    assert large.hurdle.required_bps > small.hurdle.required_bps
    assert large.hurdle.is_execution_censored
    assert not small.hurdle.is_execution_censored


@pytest.mark.property
def test_the_required_hurdle_is_never_below_the_expected_one(gate: PreTradeCostGate) -> None:
    for quantity in (10, 1_000, 50_000):
        decision = gate.evaluate(
            signal(expected_edge_bps=Decimal(1_000), proposed_quantity=quantity),
            book(),
            trade_date=TODAY,
        )
        assert decision.hurdle is not None
        assert decision.hurdle.required_bps >= decision.hurdle.point_bps
        assert decision.hurdle.uncertainty_bps >= 0


@pytest.mark.property
def test_resize_finds_the_largest_size_that_still_clears(gate: PreTradeCostGate) -> None:
    """RESIZE is a solve over the same engines, so a resized signal has been priced."""
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal(30), proposed_quantity=400_000),
        book(),
        trade_date=TODAY,
    )
    if decision.verdict is GateVerdict.RESIZE:
        assert 0 < decision.approved_quantity < 400_000
        # The approved size genuinely clears...
        approved = gate.evaluate(
            signal(expected_edge_bps=Decimal(30), proposed_quantity=decision.approved_quantity),
            book(),
            trade_date=TODAY,
        )
        assert approved.verdict is GateVerdict.PASS
        # ...and it really is close to the boundary: much more does not.
        bigger = gate.evaluate(
            signal(
                expected_edge_bps=Decimal(30),
                proposed_quantity=decision.approved_quantity * 4,
            ),
            book(),
            trade_date=TODAY,
        )
        assert bigger.verdict is not GateVerdict.PASS
    else:
        assert decision.verdict in (GateVerdict.PASS, GateVerdict.VETO)


@pytest.mark.property
def test_the_hurdle_is_u_shaped_in_size_and_the_resize_does_not_assume_otherwise(
    gate: PreTradeCostGate,
) -> None:
    """This test previously asserted the hurdle rises monotonically. That is FALSE.

    Execution cost rises with size, but STATUTORY cost per rupee FALLS — a flat per-order
    brokerage divided by a growing turnover — so the total has a minimum in the middle.
    Measured on real books, 35% of instruments have that minimum above one unit.

    The old assertion is why the defect survived: the resize bisected on a premise its own test
    certified, and on the real universe the gate refused tickets of Rs 2.6 lakh to Rs 20 lakh
    with the reason "not tradeable at any quantity" — a false statement of fact.
    """
    hurdles = []
    for quantity in (1, 100, 10_000, 1_000_000):
        decision = gate.evaluate(
            signal(expected_edge_bps=Decimal(100_000), proposed_quantity=quantity),
            book(),
            trade_date=TODAY,
        )
        assert decision.hurdle is not None
        hurdles.append(decision.hurdle.required_bps)
    assert hurdles != sorted(hurdles), (
        f"the hurdle is expected to FALL then rise; a monotone reading is the premise that "
        f"produced the wrong-veto defect: {hurdles}"
    )
    assert min(hurdles) < hurdles[0], "cost per rupee must fall as the flat charge dilutes"


@pytest.mark.adversarial
def test_unpriceable_is_not_a_veto(gate: PreTradeCostGate) -> None:
    """The distinction that stops a data outage becoming a flood of confident rejections."""
    crossed = book(bids=((140_200, 100),), asks=((140_100, 100),))
    decision = gate.evaluate(signal(), crossed, trade_date=TODAY)
    assert decision.verdict is GateVerdict.UNPRICEABLE
    # The two are separate members precisely so a caller cannot treat "I do not know" as a
    # judgement. mypy proves any direct comparison redundant — which is itself the guarantee —
    # so what is asserted here is the behaviour that depends on them differing.
    assert len({GateVerdict.UNPRICEABLE, GateVerdict.VETO}) == 2
    assert not decision.is_tradeable
    assert "NOT a veto" in decision.reason
    assert decision.hurdle is None
    assert decision.net_edge_bps is None


@pytest.mark.adversarial
def test_a_date_with_no_compiled_rates_is_unpriceable_not_vetoed(gate: PreTradeCostGate) -> None:
    """`L0.31` refuses the pre-2024 slab era; the gate must pass that refusal through as-is."""
    decision = gate.evaluate(signal(), book(), trade_date=date(2019, 6, 1))
    assert decision.verdict is GateVerdict.UNPRICEABLE
    assert decision.hurdle is None


@pytest.mark.adversarial
def test_no_size_clears_when_the_edge_is_hopeless(gate: PreTradeCostGate) -> None:
    """A veto at every size is a different statement from a resize, and says so."""
    # An edge above the live spread (so the preconditions pass) but below the hurdle at every
    # size. A vanishingly small edge would now be caught earlier and more usefully by the
    # ticket and spread preconditions, which is the correct behaviour — this test exists for
    # the OTHER branch, where the trade is economic in shape and simply not worth taking.
    # An edge below the hurdle at EVERY size on the scan, not merely at one unit. The old
    # version of this test used an edge that the shape-agnostic scan can now place, which is
    # the defect the scan was written to fix.
    # Above the live spread (14.3 bps) so the preconditions pass, but below the hurdle at
    # every size on the scan — the hurdle bottoms out near 17.9 bps on this book.
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal(16), proposed_quantity=10_000),
        book(),
        trade_date=TODAY,
    )
    assert decision.verdict is GateVerdict.VETO
    assert decision.approved_quantity == 0
    assert "any quantity" in decision.reason


@pytest.mark.adversarial
def test_a_weak_edge_basis_can_be_refused_before_any_pricing_happens() -> None:
    """Cheap refusal first: an unbelievable claim need not be priced to be declined."""
    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    strict = PreTradeCostGate(
        NseTransactionCostEngine(store),
        ExecutionFillModel(),
        minimum_edge_basis=EdgeBasis.CALIBRATED_MODEL,
    )
    decision = strict.evaluate(
        signal(expected_edge_bps=Decimal(10_000), edge_basis=EdgeBasis.OPERATOR_ASSERTION),
        book(),
        trade_date=TODAY,
    )
    assert decision.verdict is GateVerdict.VETO
    assert "weaker than" in decision.reason
    assert decision.hurdle is None


@pytest.mark.unit
def test_the_decision_explains_itself_in_one_line(gate: PreTradeCostGate) -> None:
    """An operator reading a veto needs the arithmetic, not just the word."""
    described = gate.evaluate(
        signal(expected_edge_bps=Decimal(1)), book(), trade_date=TODAY
    ).describe()
    assert "VETO" in described
    assert "TESTCO" in described
    assert "hurdle" in described
    assert "statutory" in described


# ------------------------------- the ticket preconditions (L11.99, L11.106-L11.108)


@pytest.mark.unit
def test_an_option_priced_against_the_underlying_is_caught(gate: PreTradeCostGate) -> None:
    """`L11.106`: the denominator must be the instrument you TRADE.

    Five points on a Rs 100 premium is 5% and viable; the same five points against NIFTY at
    24,000 is 0.02% and looks fatal. Both describe the same trade. Passing the underlying's
    price as the reference understates every bps figure by roughly the ratio between them, and
    it does so in the FLATTERING direction, which is why it must be caught rather than noticed.
    """
    underlying_priced = signal(
        segment=ChargeableSegment.EQUITY_OPTIONS,
        reference_price_paise=Decimal(2_400_000),
        strike_paise=Decimal(2_400_000),
        expected_edge_bps=Decimal(500),
    )
    decision = gate.evaluate(underlying_priced, book(), trade_date=TODAY)
    assert decision.verdict is GateVerdict.VETO
    assert decision.preconditions is not None
    failures = {failure.name for failure in decision.preconditions.failures}
    assert PreconditionName.TRADEABLE_UNIT_DENOMINATOR in failures


@pytest.mark.unit
def test_a_tiny_ticket_is_refused_because_the_flat_charge_swamps_it(
    gate: PreTradeCostGate,
) -> None:
    """`L11.107`: a fixed per-order charge explodes as a percentage as the ticket shrinks.

    The inversion worth remembering: cheap far-OTM options are the WORST scalping vehicle in
    the universe, not the safest.
    """
    tiny = signal(
        segment=ChargeableSegment.EQUITY_OPTIONS,
        reference_price_paise=Decimal(500),
        strike_paise=Decimal(2_400_000),
        proposed_quantity=75,
        expected_edge_bps=Decimal(200),
    )
    decision = gate.evaluate(tiny, book(), trade_date=TODAY)
    assert decision.verdict is GateVerdict.VETO
    assert decision.preconditions is not None
    failures = {failure.name for failure in decision.preconditions.failures}
    assert PreconditionName.MINIMUM_TICKET in failures


@pytest.mark.property
def test_the_minimum_ticket_is_derived_from_the_edge_not_a_rupee_floor(
    gate: PreTradeCostGate,
) -> None:
    """`R.03`: a signal claiming more edge can carry a smaller ticket. No fixed floor exists."""
    small_ticket = {
        "segment": ChargeableSegment.EQUITY_OPTIONS,
        "reference_price_paise": Decimal(2_000),
        "strike_paise": Decimal(2_400_000),
        "proposed_quantity": 75,
    }
    thin = gate.evaluate(
        signal(**small_ticket, expected_edge_bps=Decimal(20)), book(), trade_date=TODAY
    )
    generous = gate.evaluate(
        signal(**small_ticket, expected_edge_bps=Decimal(5_000)), book(), trade_date=TODAY
    )
    assert thin.preconditions is not None and generous.preconditions is not None
    thin_failed = PreconditionName.MINIMUM_TICKET in {f.name for f in thin.preconditions.failures}
    generous_failed = PreconditionName.MINIMUM_TICKET in {
        f.name for f in generous.preconditions.failures
    }
    # The same ticket passes or fails depending on what the signal claims — which is what
    # "derived, not floored" means.
    assert thin_failed and not generous_failed


@pytest.mark.unit
def test_a_spread_wider_than_the_edge_is_refused_from_the_live_book(
    gate: PreTradeCostGate,
) -> None:
    """`L11.108`: read the spread from the BOOK. A modelled spread cannot tell an illiquid
    strike from a liquid one, which is the only case where the check matters."""
    illiquid = book(bids=((100_000, 100),), asks=((180_000, 100),))
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal(50), reference_price_paise=Decimal(140_000)),
        illiquid,
        trade_date=TODAY,
    )
    assert decision.verdict is GateVerdict.VETO
    assert decision.preconditions is not None
    failures = {failure.name for failure in decision.preconditions.failures}
    assert PreconditionName.LIVE_SPREAD in failures


@pytest.mark.unit
def test_a_range_narrower_than_its_own_cost_is_refused(gate: PreTradeCostGate) -> None:
    """`L11.99`: below its trading cost a range is noise wearing a pattern's clothes."""
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal(500)),
        book(),
        trade_date=TODAY,
        range_width_bps=Decimal(2),
    )
    assert decision.verdict is GateVerdict.VETO
    assert decision.preconditions is not None
    assert PreconditionName.RANGE_WIDTH in {f.name for f in decision.preconditions.failures}


@pytest.mark.unit
def test_a_wide_enough_range_does_not_block_the_trade(gate: PreTradeCostGate) -> None:
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal(500)),
        book(),
        trade_date=TODAY,
        range_width_bps=Decimal(400),
    )
    assert decision.preconditions is not None
    assert PreconditionName.RANGE_WIDTH not in {
        f.name for f in decision.preconditions.failures
    }


@pytest.mark.property
def test_every_precondition_runs_even_after_one_fails(gate: PreTradeCostGate) -> None:
    """An operator needs to know whether fixing one thing helps or the trade is dead severally."""
    doomed = signal(
        segment=ChargeableSegment.EQUITY_OPTIONS,
        reference_price_paise=Decimal(2_400_000),
        strike_paise=Decimal(2_400_000),
        proposed_quantity=1,
        expected_edge_bps=Decimal(1),
    )
    decision = gate.evaluate(doomed, book(), trade_date=TODAY, range_width_bps=Decimal(1))
    assert decision.preconditions is not None
    assert len(decision.preconditions.results) == len(PreconditionName)
    assert len(decision.preconditions.failures) > 1


@pytest.mark.unit
def test_a_healthy_trade_reports_every_precondition_satisfied(gate: PreTradeCostGate) -> None:
    """A pass must be as legible as a failure."""
    decision = gate.evaluate(signal(expected_edge_bps=Decimal(500)), book(), trade_date=TODAY)
    assert decision.preconditions is not None
    assert decision.preconditions.all_satisfied
    assert "satisfied" in decision.preconditions.describe()


@pytest.mark.property
def test_each_leg_is_priced_against_its_own_ladder(gate: PreTradeCostGate) -> None:
    """A buy entry lifts asks; its exit is a sell that hits bids. Different ladders.

    Doubling one side was measured against the true `entry + exit` on 2,398 real books: a
    median error of +0.6% hid a p5 of -29.7% and a worst case of -83.1%, with 25.8% of books
    UNDERSTATED by more than 10% — the direction that lets a losing trade through.

    Asserted on a deliberately asymmetric book, where doubling either side alone gives an answer
    that cannot equal the true sum.
    """
    lopsided = book(
        bids=((139_900, 100), (139_000, 200)),
        asks=((140_100, 100_000),),
    )
    buying = gate.evaluate(
        signal(expected_edge_bps=Decimal(100_000), proposed_quantity=5_000), lopsided,
        trade_date=TODAY,
    )
    selling = gate.evaluate(
        signal(expected_edge_bps=Decimal(100_000), proposed_quantity=5_000, side=TradeLeg.SELL),
        lopsided,
        trade_date=TODAY,
    )
    assert buying.hurdle is not None and selling.hurdle is not None
    # Both directions must see the SAME round-trip execution cost: a round trip crosses both
    # ladders whichever end it starts from.
    assert buying.hurdle.execution_point_bps == selling.hurdle.execution_point_bps
    # And it must not be twice either single side, which is what the old code computed.
    assert buying.expected_fill is not None
    assert (
        buying.hurdle.execution_point_bps
        != buying.expected_fill.point_cost_bps * Decimal(2)
    )
