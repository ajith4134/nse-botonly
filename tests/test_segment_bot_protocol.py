"""Tests for the segment-bot protocol and its conformance suite — `L5.29`.

The conformance suite is the artifact that makes six concurrently-authored bots safe (`A.130`), so
the tests that matter most here are the ones proving it can FAIL. A suite that passes everything
converts "did the author understand the contract?" from a review question into a false reassurance,
which is worse than not having it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.segment_bots.segment_bot_conformance import (
    ConformanceViolation,
    run_segment_bot_conformance,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotMaturity,
    BotMaturityRung,
    SegmentBotContext,
    SegmentBotProtocolError,
    SegmentRelevance,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

INDIA = ZoneInfo("Asia/Kolkata")
MOMENT = datetime(2026, 8, 17, 10, 30, tzinfo=INDIA)


def _uniform_regime() -> RegimeDistribution:
    return RegimeDistribution(MarketRegime.uniform_probabilities())


def _cash_instrument() -> TradeableInstrument:
    return TradeableInstrument(
        instrument_token=738561,
        trading_symbol="RELIANCE",
        lot_size=1,
        tick_size_paise=5,
    )


def _option_instrument() -> TradeableInstrument:
    return TradeableInstrument(
        instrument_token=9001,
        trading_symbol="NIFTY26AUG24500CE",
        lot_size=75,
        tick_size_paise=5,
        strike_paise=Decimal("2450000"),
        expiry=date(2026, 8, 27),
    )


def _context(
    *,
    instruments: tuple[TradeableInstrument, ...],
    moment: datetime = MOMENT,
) -> SegmentBotContext:
    return SegmentBotContext(
        decision_instant=moment,
        tradeable_universe=instruments,
        regime=_uniform_regime(),
        carried_memory={},
    )


# ------------------------------------------------------------------ the value types


def test_a_bot_may_not_report_itself_graduated() -> None:
    """`R.22` two-key rule: the system can NEVER self-promote to live capital.

    Made unavailable in the type rather than merely discouraged in a docstring — this is the single
    most consequential refusal in the protocol.
    """
    with pytest.raises(SegmentBotProtocolError, match="cannot report itself GRADUATED"):
        BotMaturity(
            rung=BotMaturityRung.GRADUATED,
            closed_trades_observed=500,
            evidence="a track record I like the look of",
        )


def test_the_rung_below_graduated_is_reportable() -> None:
    """The ladder must be climbable up to the point where a human takes over."""
    maturity = BotMaturity(
        rung=BotMaturityRung.GRADUATION_CANDIDATE,
        closed_trades_observed=500,
        evidence="200 sessions, edge above the segment null after costs",
    )
    assert maturity.rung is BotMaturityRung.GRADUATION_CANDIDATE
    assert not maturity.is_armable_without_operator


def test_the_ladder_is_ordered_so_two_bots_can_be_compared() -> None:
    """A supervisor allocating across six bots needs the rungs to be comparable."""
    rungs = list(BotMaturityRung)
    assert rungs == sorted(rungs, key=lambda rung: rung.ladder_position)
    assert BotMaturityRung.COLD_START.ladder_position < BotMaturityRung.OBSERVING.ladder_position
    assert (
        BotMaturityRung.PAPER_QUALIFIED.ladder_position
        < BotMaturityRung.GRADUATION_CANDIDATE.ladder_position
    )


def test_negative_closed_trades_are_refused() -> None:
    with pytest.raises(SegmentBotProtocolError, match="closed trades"):
        BotMaturity(rung=BotMaturityRung.OBSERVING, closed_trades_observed=-1, evidence="x" * 30)


def test_maturity_without_evidence_is_refused() -> None:
    """A rung claimed with no evidence is the shape `R.13` exists to catch."""
    with pytest.raises(SegmentBotProtocolError, match="evidence"):
        BotMaturity(rung=BotMaturityRung.PAPER_QUALIFIED, closed_trades_observed=10, evidence="  ")


@pytest.mark.parametrize("applicability", [-0.01, 1.01, float("nan")])
def test_relevance_outside_the_unit_interval_is_refused(applicability: float) -> None:
    """Six bots' scores are compared against each other; an unbounded one wins by scale."""
    with pytest.raises(SegmentBotProtocolError, match="applicability"):
        SegmentRelevance(applicability=applicability, reason="because")


def test_relevance_without_a_reason_is_refused() -> None:
    with pytest.raises(SegmentBotProtocolError, match="reason"):
        SegmentRelevance(applicability=0.5, reason="")


def test_an_option_instrument_without_a_strike_is_refused() -> None:
    with pytest.raises(SegmentBotProtocolError, match="lot size"):
        TradeableInstrument(instrument_token=1, trading_symbol="X", lot_size=0, tick_size_paise=5)


def test_a_context_whose_instant_is_naive_is_refused() -> None:
    """A naive instant silently becomes UTC and moves every decision by five and a half hours."""
    with pytest.raises(SegmentBotProtocolError, match="timezone"):
        SegmentBotContext(
            decision_instant=datetime(2026, 8, 17, 10, 30),  # noqa: DTZ001 — naive is the point
            tradeable_universe=(),
            regime=_uniform_regime(),
            carried_memory={},
        )


# ------------------------------------------------------------------ reference bots


class ConformingCashBot:
    """A minimal bot that honours the contract — the suite must pass it."""

    trading_segment = TradingSegment.CASH_INTRADAY
    bot_identity = "cash_intraday_reference_bot"

    def __init__(self) -> None:
        self.observed_instants: list[datetime] = []

    def observe(self, context: SegmentBotContext) -> None:
        if context.decision_instant not in self.observed_instants:
            self.observed_instants.append(context.decision_instant)

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        return tuple(
            PricedSignal(
                instrument_token=instrument.instrument_token,
                trading_symbol=instrument.trading_symbol,
                segment=ChargeableSegment.EQUITY_INTRADAY,
                side=TradeLeg.BUY,
                decided_at=context.decision_instant,
                reference_price_paise=Decimal("240000"),
                expected_edge_bps=Decimal("12"),
                proposed_quantity=instrument.lot_size,
                edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                source=self.bot_identity,
            )
            for instrument in context.tradeable_universe
        )

    def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
        return SegmentRelevance(applicability=0.5, reason="uniform regime, no view")

    def maturity(self) -> BotMaturity:
        return BotMaturity(
            rung=BotMaturityRung.COLD_START,
            closed_trades_observed=0,
            evidence="no closed trades yet; activation gated by the maturity ladder",
        )


class BotProposingAnotherSegment(ConformingCashBot):
    """Emits a signal whose charge scope belongs to a different segment."""

    bot_identity = "cash_bot_emitting_options"

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        return (
            PricedSignal(
                instrument_token=9001,
                trading_symbol="NIFTY26AUG24500CE",
                segment=ChargeableSegment.EQUITY_OPTIONS,
                side=TradeLeg.BUY,
                decided_at=context.decision_instant,
                reference_price_paise=Decimal("12000"),
                expected_edge_bps=Decimal("40"),
                proposed_quantity=75,
                edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                source=self.bot_identity,
                strike_paise=Decimal("2450000"),
            ),
        )


class BotRaisingOnAnEmptyUniverse(ConformingCashBot):
    """An empty proposal is a real answer; raising instead kills the whole session."""

    bot_identity = "cash_bot_that_raises_when_idle"

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        if not context.tradeable_universe:
            raise RuntimeError("nothing to trade")
        return super().propose(context)


class NondeterministicBot(ConformingCashBot):
    """Same context, different answer — makes every replay and every review meaningless."""

    bot_identity = "cash_bot_with_a_hidden_counter"

    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        self._calls += 1
        if self._calls % 2 == 0:
            return ()
        return super().propose(context)


class SelfGraduatingBot(ConformingCashBot):
    """Attempts the one thing `R.22` forbids absolutely."""

    bot_identity = "cash_bot_that_arms_itself"

    def maturity(self) -> BotMaturity:
        maturity = BotMaturity(
            rung=BotMaturityRung.GRADUATION_CANDIDATE,
            closed_trades_observed=900,
            evidence="a long and flattering track record",
        )
        object.__setattr__(maturity, "rung", BotMaturityRung.GRADUATED)
        return maturity


# ------------------------------------------------------------------ the conformance suite


def _contexts() -> tuple[SegmentBotContext, ...]:
    return (
        _context(instruments=(_cash_instrument(),)),
        _context(instruments=()),
        _context(instruments=(_cash_instrument(),), moment=MOMENT + timedelta(minutes=5)),
    )


def test_the_reference_bot_passes_the_suite() -> None:
    assert run_segment_bot_conformance(ConformingCashBot(), _contexts()) == []


@pytest.mark.adversarial
def test_a_bot_proposing_another_segments_instrument_fails() -> None:
    """Six bots, one order path: a bot reaching outside its segment is the cross-talk failure."""
    violations = run_segment_bot_conformance(BotProposingAnotherSegment(), _contexts())
    assert any(violation.check == "signal-matches-own-segment" for violation in violations)


@pytest.mark.adversarial
def test_a_bot_that_raises_on_an_empty_universe_fails() -> None:
    violations = run_segment_bot_conformance(BotRaisingOnAnEmptyUniverse(), _contexts())
    assert any(violation.check == "empty-universe-is-answerable" for violation in violations)


@pytest.mark.adversarial
def test_a_nondeterministic_bot_fails() -> None:
    """Determinism is what makes a decision trace reconstructable and a review possible."""
    violations = run_segment_bot_conformance(NondeterministicBot(), _contexts())
    checks = {violation.check for violation in violations}
    assert "deterministic-given-the-same-context" in checks


@pytest.mark.adversarial
def test_a_bot_reporting_itself_graduated_fails() -> None:
    """The `R.22` refusal, checked at the boundary as well as in the constructor."""
    violations = run_segment_bot_conformance(SelfGraduatingBot(), _contexts())
    assert any(violation.check == "never-self-graduates" for violation in violations)


@pytest.mark.adversarial
def test_a_bot_with_an_empty_identity_fails() -> None:
    """`R.14`: a bot nobody can name is a track record nobody can file."""

    class AnonymousBot(ConformingCashBot):
        bot_identity = "   "

    violations = run_segment_bot_conformance(AnonymousBot(), _contexts())
    assert any(violation.check == "identity-is-self-describing" for violation in violations)


@pytest.mark.adversarial
def test_a_bot_whose_identity_does_not_name_its_segment_fails() -> None:
    """`R.14` again: `bot_a` filed against a track record tells a reader nothing."""

    class VaguelyNamedBot(ConformingCashBot):
        bot_identity = "bot_a"

    violations = run_segment_bot_conformance(VaguelyNamedBot(), _contexts())
    assert any(violation.check == "identity-is-self-describing" for violation in violations)


def test_the_suite_refuses_to_run_with_no_contexts() -> None:
    """A suite run over zero contexts passes vacuously — the exact false reassurance to avoid."""
    with pytest.raises(SegmentBotProtocolError, match="at least one context"):
        run_segment_bot_conformance(ConformingCashBot(), ())


def test_the_suite_requires_an_empty_universe_context() -> None:
    """The cheapest real bug this suite catches is a bot that cannot answer "nothing today"."""
    with pytest.raises(SegmentBotProtocolError, match="EMPTY tradeable universe"):
        run_segment_bot_conformance(
            ConformingCashBot(), (_context(instruments=(_cash_instrument(),)),)
        )


def test_a_violation_names_the_bot_the_check_and_what_to_do() -> None:
    """A finding a reader cannot act on gets triaged as a lint nit."""
    violations = run_segment_bot_conformance(BotProposingAnotherSegment(), _contexts())
    assert violations
    described = violations[0].describe()
    assert "cash_bot_emitting_options" in described
    assert len(violations[0].detail) > 40


def test_an_option_bot_must_carry_a_strike_on_every_signal() -> None:
    """Mirrors the taxonomy's `strike_required`; the cost of a strikeless option is uncomputable."""

    class OptionBotWithoutStrikes:
        trading_segment = TradingSegment.INDEX_OPTIONS
        bot_identity = "index_options_bot_without_strikes"

        def observe(self, context: SegmentBotContext) -> None:
            return None

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            # PricedSignal itself refuses a strikeless option, so the failure surfaces as a raised
            # error rather than a malformed signal — the suite must report that, not crash.
            return tuple(
                PricedSignal(
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    segment=ChargeableSegment.EQUITY_OPTIONS,
                    side=TradeLeg.BUY,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal("12000"),
                    expected_edge_bps=Decimal("40"),
                    proposed_quantity=instrument.lot_size,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                    strike_paise=instrument.strike_paise,
                )
                for instrument in context.tradeable_universe
            )

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            return SegmentRelevance(applicability=0.4, reason="expiry week")

        def maturity(self) -> BotMaturity:
            return BotMaturity(
                rung=BotMaturityRung.COLD_START,
                closed_trades_observed=0,
                evidence="no closed trades yet; blocked on the B1 Greeks surface",
            )

    strikeless = TradeableInstrument(
        instrument_token=9001, trading_symbol="NIFTY26AUG24500CE", lot_size=75, tick_size_paise=5
    )
    contexts = (
        _context(instruments=(strikeless,)),
        _context(instruments=()),
    )
    violations = run_segment_bot_conformance(OptionBotWithoutStrikes(), contexts)
    assert any(violation.check == "propose-does-not-raise" for violation in violations)


def test_an_option_bot_with_strikes_passes() -> None:
    """The positive control for the test above — otherwise it proves only that something broke."""

    class ProperIndexOptionBot:
        trading_segment = TradingSegment.INDEX_OPTIONS
        bot_identity = "index_options_reference_bot"

        def observe(self, context: SegmentBotContext) -> None:
            return None

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            return tuple(
                PricedSignal(
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    segment=ChargeableSegment.EQUITY_OPTIONS,
                    side=TradeLeg.BUY,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal("12000"),
                    expected_edge_bps=Decimal("80"),
                    proposed_quantity=instrument.lot_size,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                    strike_paise=instrument.strike_paise,
                )
                for instrument in context.tradeable_universe
            )

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            return SegmentRelevance(applicability=0.4, reason="expiry week")

        def maturity(self) -> BotMaturity:
            return BotMaturity(
                rung=BotMaturityRung.COLD_START,
                closed_trades_observed=0,
                evidence="no closed trades yet; blocked on the B1 Greeks surface",
            )

    contexts = (_context(instruments=(_option_instrument(),)), _context(instruments=()))
    assert run_segment_bot_conformance(ProperIndexOptionBot(), contexts) == []


def test_a_violation_is_hashable_so_findings_can_be_deduplicated() -> None:
    violation = ConformanceViolation(bot_identity="x_bot", check="some-check", detail="a" * 50)
    assert len({violation, violation}) == 1


# ------------------------------------------------------ regressions from the 2026-08-17 review
#
# An adversarial review wrote 18 non-conforming bots; 15 of them passed the suite clean. Each test
# below is one of those bots. They are the proof that the fixes landed: every one must now FAIL.


class _CashBotBase(ConformingCashBot):
    """Shared base so each attack differs in exactly one way."""


def _run(bot: object) -> set[str]:
    violations = run_segment_bot_conformance(bot, _contexts())  # type: ignore[arg-type]
    return {violation.check for violation in violations}


@pytest.mark.adversarial
def test_a_bot_that_mutates_the_context_fails() -> None:
    """Review C1: the context is replayed and shared; writing into it poisons both, invisibly."""

    class ContextMutatingBot(_CashBotBase):
        bot_identity = "cash_intraday_context_mutating_bot"

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            context.carried_memory["poison"] = True  # type: ignore[index]
            return super().propose(context)

    assert "context-is-not-mutated" in _run(ContextMutatingBot())


@pytest.mark.adversarial
def test_a_bot_emitting_the_same_signal_twice_fails() -> None:
    """Review C1: one conviction filled several times."""

    class DuplicateSignalBot(_CashBotBase):
        bot_identity = "cash_intraday_duplicate_signal_bot"

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            signals = super().propose(context)
            return signals * 3

    assert "signals-are-distinct" in _run(DuplicateSignalBot())


@pytest.mark.adversarial
def test_a_bot_proposing_a_quantity_that_is_not_a_whole_lot_fails() -> None:
    """Review C1: an option bot proposed 1 against a lot size of 75 and passed."""

    class OddLotBot(_CashBotBase):
        bot_identity = "cash_intraday_odd_lot_bot"

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            return tuple(
                PricedSignal(
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    segment=ChargeableSegment.EQUITY_INTRADAY,
                    side=TradeLeg.BUY,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal("240000"),
                    expected_edge_bps=Decimal("12"),
                    proposed_quantity=instrument.lot_size * 3 + 1,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                )
                for instrument in context.tradeable_universe
            )

    lotted = TradeableInstrument(
        instrument_token=738561, trading_symbol="RELIANCE", lot_size=75, tick_size_paise=5
    )
    contexts = (_context(instruments=(lotted,)), _context(instruments=()))
    violations = run_segment_bot_conformance(OddLotBot(), contexts)
    assert any(v.check == "quantity-is-a-whole-number-of-lots" for v in violations)


@pytest.mark.adversarial
def test_a_bot_inventing_instruments_outside_its_universe_fails() -> None:
    """Review C1: it passed even on an EMPTY universe, defeating R.09 and per-segment arming."""

    class UniverseInventingBot(_CashBotBase):
        bot_identity = "cash_intraday_universe_inventing_bot"

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            return (
                PricedSignal(
                    instrument_token=999_999,
                    trading_symbol="NOT_IN_UNIVERSE",
                    segment=ChargeableSegment.EQUITY_INTRADAY,
                    side=TradeLeg.BUY,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal("240000"),
                    expected_edge_bps=Decimal("12"),
                    proposed_quantity=1,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                ),
            )

    assert "signal-is-inside-the-given-universe" in _run(UniverseInventingBot())


@pytest.mark.adversarial
def test_a_bot_flooding_the_proposal_fails() -> None:
    """Review C1: 5,000 signals per context passed, including when idle."""

    class FloodingBot(_CashBotBase):
        bot_identity = "cash_intraday_flooding_bot"

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            base = super().propose(context)
            if not base:
                return base
            return tuple(
                PricedSignal(
                    instrument_token=base[0].instrument_token,
                    trading_symbol=base[0].trading_symbol,
                    segment=base[0].segment,
                    side=base[0].side,
                    decided_at=base[0].decided_at + timedelta(microseconds=index),
                    reference_price_paise=base[0].reference_price_paise,
                    expected_edge_bps=base[0].expected_edge_bps,
                    proposed_quantity=base[0].proposed_quantity,
                    edge_basis=base[0].edge_basis,
                    source=self.bot_identity,
                )
                for index in range(600)
            )

    assert "proposal-size-is-sane" in _run(FloodingBot())


@pytest.mark.adversarial
def test_a_bot_whose_relevance_moves_between_reads_fails() -> None:
    """Review C1: the supervisor allocates on this number."""

    class DriftingRelevanceBot(_CashBotBase):
        bot_identity = "cash_intraday_drifting_relevance_bot"

        def __init__(self) -> None:
            super().__init__()
            self._reads = 0

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            self._reads += 1
            return SegmentRelevance(
                applicability=min(self._reads / 10, 1.0), reason="moves on every read"
            )

    assert "relevance-is-deterministic" in _run(DriftingRelevanceBot())


@pytest.mark.adversarial
def test_a_bot_writing_past_the_relevance_bound_fails() -> None:
    """Review C1/7b: `object.__setattr__` gave applicability=10000 and took the whole allocation."""

    class UnboundedRelevanceBot(_CashBotBase):
        bot_identity = "cash_intraday_unbounded_relevance_bot"

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            relevance = SegmentRelevance(applicability=0.5, reason="looks innocent")
            object.__setattr__(relevance, "applicability", 10_000.0)
            return relevance

    assert "relevance-is-a-bounded-score" in _run(UnboundedRelevanceBot())


@pytest.mark.adversarial
def test_a_bot_whose_relevance_raises_is_rejected_not_crashed() -> None:
    """Review M5: it CRASHED the suite, defeating the runtime-gate purpose."""

    class RelevanceRaisingBot(_CashBotBase):
        bot_identity = "cash_intraday_relevance_raising_bot"

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            raise RuntimeError("relevance model not loaded")

    assert "relevance-does-not-raise" in _run(RelevanceRaisingBot())


@pytest.mark.adversarial
def test_a_bot_whose_maturity_raises_is_not_accused_of_self_graduating() -> None:
    """Review M6: an unrelated bug raised the loudest alarm the suite has."""

    class MaturityRaisingBot(_CashBotBase):
        bot_identity = "cash_intraday_maturity_raising_bot"

        def maturity(self) -> BotMaturity:
            SegmentRelevance(applicability=1.4, reason="unrelated bug inside maturity()")
            raise AssertionError("unreachable")

    checks = _run(MaturityRaisingBot())
    assert "maturity-does-not-raise" in checks
    assert "never-self-graduates" not in checks, "the R.22 alarm must keep its meaning"


@pytest.mark.adversarial
def test_a_bot_whose_identity_changes_during_the_run_fails() -> None:
    """Review C1: records would file under a second name."""

    class ShiftingIdentityBot(ConformingCashBot):
        """Its name changes on every read, so records file under a second name."""

        def __init__(self) -> None:
            super().__init__()
            self._reads = 0

        def __getattribute__(self, name: str) -> object:
            if name == "bot_identity":
                reads = object.__getattribute__(self, "_reads") + 1
                object.__setattr__(self, "_reads", reads)
                return f"cash_intraday_shifting_bot_{reads}"
            return object.__getattribute__(self, name)

    assert "identity-is-stable" in _run(ShiftingIdentityBot())


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("segment", "identity"),
    [
        (TradingSegment.INDEX_FUTURES, "index_options_greek_scalper"),
        (TradingSegment.STOCK_OPTIONS, "stock_futures_carry_bot"),
        (TradingSegment.CASH_INTRADAY, "cashew_nut_arbitrage_bot"),
    ],
)
def test_a_bot_named_for_the_wrong_segment_fails(segment: TradingSegment, identity: str) -> None:
    """Review M4: first-word matching licensed exactly the mislabelling R.14 forbids."""

    class MisnamedBot(ConformingCashBot):
        trading_segment = segment
        bot_identity = identity

    assert "identity-is-self-describing" in _run(MisnamedBot())


@pytest.mark.adversarial
def test_an_option_bot_claiming_an_overnight_horizon_fails() -> None:
    """Review C2/C1: R.01's one absolute refusal had zero enforcement against any bot."""

    class OvernightOptionBot:
        trading_segment = TradingSegment.INDEX_OPTIONS
        bot_identity = "index_options_overnight_carry_bot"

        def observe(self, context: SegmentBotContext) -> None:
            return None

        def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
            return tuple(
                PricedSignal(
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    segment=ChargeableSegment.EQUITY_OPTIONS,
                    side=TradeLeg.BUY,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal("12000"),
                    expected_edge_bps=Decimal("80"),
                    proposed_quantity=instrument.lot_size,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                    strike_paise=instrument.strike_paise,
                    horizon_minutes=5 * 24 * 60,
                )
                for instrument in context.tradeable_universe
            )

        def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
            return SegmentRelevance(applicability=0.4, reason="expiry week")

        def maturity(self) -> BotMaturity:
            return BotMaturity(
                rung=BotMaturityRung.COLD_START,
                closed_trades_observed=0,
                evidence="no closed trades yet; blocked on the B1 Greeks surface",
            )

    contexts = (_context(instruments=(_option_instrument(),)), _context(instruments=()))
    violations = run_segment_bot_conformance(OvernightOptionBot(), contexts)
    assert any(v.check == "honours-the-segment-carry-rule" for v in violations)
