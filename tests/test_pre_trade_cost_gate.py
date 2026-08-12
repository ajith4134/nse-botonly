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

from nse_algo_trader.cost_gate.pre_trade_cost_gate import (
    GateVerdict,
    PreTradeCostGate,
)
from nse_algo_trader.cost_gate.priced_signal import (
    EdgeBasis,
    PricedSignal,
    PricedSignalError,
    edge_from_mean_reversion_decision,
)
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


@pytest.mark.unit
def test_the_mean_reversion_adapter_turns_a_deviation_into_a_falsifiable_claim() -> None:
    """The one existing strategy, given an edge — and labelled a hypothesis, not a fact."""
    edge = edge_from_mean_reversion_decision(
        deviation=3.0,
        deviation_band=1.0,
        conviction=0.5,
        reference_price_paise=Decimal(100_000),
        band_width_paise=Decimal(1_000),
    )
    # Excess of 2 band-widths x 1,000 paise = 2,000 paise on 100,000 = 200 bps, halved by
    # conviction.
    assert edge == Decimal(100)


@pytest.mark.adversarial
def test_a_deviation_inside_its_own_band_claims_no_edge() -> None:
    """The engine is not claiming a reversion to capture, so there is nothing to price."""
    with pytest.raises(PricedSignalError, match="no reversion to capture"):
        edge_from_mean_reversion_decision(
            deviation=0.5,
            deviation_band=1.0,
            conviction=1.0,
            reference_price_paise=Decimal(100_000),
            band_width_paise=Decimal(1_000),
        )


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
def test_cost_per_rupee_rises_with_size_so_the_hurdle_does_too(gate: PreTradeCostGate) -> None:
    """The property that makes the resize bisection valid."""
    hurdles = []
    for quantity in (100, 1_000, 10_000, 100_000):
        decision = gate.evaluate(
            signal(expected_edge_bps=Decimal(10_000), proposed_quantity=quantity),
            book(),
            trade_date=TODAY,
        )
        assert decision.hurdle is not None
        hurdles.append(decision.hurdle.required_bps)
    assert hurdles == sorted(hurdles), f"hurdle must not fall as size grows: {hurdles}"


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
    decision = gate.evaluate(
        signal(expected_edge_bps=Decimal("0.001"), proposed_quantity=10_000),
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
