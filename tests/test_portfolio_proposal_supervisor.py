"""Tests for the cross-bot portfolio supervisor (`L3.05`/`L7.02`, `B39`), written first.

`B39` is measured, not hypothetical. On 2026-07-31 the per-bot net-directional rule was working
exactly as written and the PORTFOLIO still ended at **43.1%** directional against a 25% bound,
because `index_futures` was a book of one at **100%** and `stock_futures` at 0.0% — and nothing
aggregated across the six. The per-bot rule cannot fix that; only a supervisor over all of them can.

The second measured failure this defends against is `docs/research/261`'s: a net-directional bound
stated as a fraction of GROSS cannot be met by the first position, so it refused 23 of 23 real
proposals. The bound here is on the DIRECTION OF TRAVEL — a trade on the lighter side is always
admitted and the book converges.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.portfolio.portfolio_proposal_supervisor import (
    BotProposal,
    PortfolioProposalSupervisor,
    PortfolioSupervisionError,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

DECIDED_AT = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


def _signal(
    symbol: str,
    side: TradeLeg,
    *,
    price_rupees: str,
    quantity: int,
    edge_bps: str = "25",
    conviction: str | None = "0.6",
) -> PricedSignal:
    return PricedSignal(
        instrument_token=abs(hash(symbol)) % 1_000_000,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=side,
        decided_at=DECIDED_AT,
        reference_price_paise=Decimal(price_rupees) * 100,
        expected_edge_bps=Decimal(edge_bps),
        proposed_quantity=quantity,
        edge_basis=EdgeBasis.CALIBRATED_MODEL,
        source="test",
        conviction=None if conviction is None else Decimal(conviction),
    )


def _proposal(
    bot: str, symbol: str, side: TradeLeg, price: str, quantity: int, **kw
) -> BotProposal:
    return BotProposal(
        bot_identity=bot,
        signal=_signal(symbol, side, price_rupees=price, quantity=quantity, **kw),
        margin_rupees=None,
    )


def _supervisor(**kw) -> PortfolioProposalSupervisor:
    settings = {
        "deployable_rupees": Decimal("1000000"),
        "net_directional_fraction": Decimal("0.25"),
    }
    settings.update(kw)
    return PortfolioProposalSupervisor(**settings)  # type: ignore[arg-type]


# -- the bound --------------------------------------------------------------------------------


def test_the_gross_budget_is_never_exceeded() -> None:
    proposals = [
        _proposal(f"bot_{index}", f"SYM{index}", TradeLeg.BUY, "1000", 500) for index in range(6)
    ]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("0"))
    assert plan.gross_admitted_rupees <= Decimal("1000000")


def test_the_net_directional_bound_holds_across_bots_not_only_within_one() -> None:
    """`B39`: a book of one at 100% directional inside its own bot is what broke the old rule."""
    proposals = [
        _proposal("index_futures_bot", "NIFTYFUT", TradeLeg.BUY, "1000", 400),
        _proposal("stock_futures_bot", "RELIANCEFUT", TradeLeg.BUY, "1000", 400),
    ]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("0"))
    bound = Decimal("1000000") * Decimal("0.25")
    assert abs(plan.net_after_rupees) <= bound + Decimal("1")


def test_a_trade_on_the_lighter_side_is_always_admitted_in_full() -> None:
    """`docs/research/261`: a bound as a fraction of gross refused 23 of 23 real proposals."""
    proposals = [_proposal("bot", "SYM", TradeLeg.SELL, "1000", 100)]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("900000"))
    admitted = plan.admitted_by_identity["bot"][0]
    assert admitted.quantity == 100


def test_the_first_position_in_an_empty_book_is_never_refused() -> None:
    proposals = [_proposal("bot", "SYM", TradeLeg.BUY, "1000", 50)]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("0"))
    assert plan.admitted_by_identity["bot"][0].quantity == 50


def test_a_book_already_beyond_the_bound_admits_only_converging_trades() -> None:
    heavy = Decimal("900000")
    adding = [_proposal("bot", "SYM", TradeLeg.BUY, "1000", 100)]
    converging = [_proposal("bot", "SYM", TradeLeg.SELL, "1000", 100)]
    supervisor = _supervisor()
    assert supervisor.supervise(adding, book_net_rupees=heavy).admitted_count == 0
    assert supervisor.supervise(converging, book_net_rupees=heavy).admitted_count == 1


# -- per-bot fairness -------------------------------------------------------------------------


def test_no_bot_takes_more_than_its_share_of_the_book() -> None:
    """`R.10`: the six segments are equal by default, so one loud bot cannot take the book."""
    greedy = [
        _proposal("greedy_bot", f"G{index}", TradeLeg.BUY, "1000", 200) for index in range(20)
    ]
    modest = [_proposal("modest_bot", "M0", TradeLeg.BUY, "1000", 10)]
    plan = _supervisor().supervise(greedy + modest, book_net_rupees=Decimal("0"))
    share = plan.deployed_by_identity["greedy_bot"]
    assert share <= Decimal("1000000") / 2 + Decimal("1")


def test_a_bot_with_no_proposals_consumes_none_of_the_book() -> None:
    plan = _supervisor().supervise(
        [_proposal("only_bot", "SYM", TradeLeg.BUY, "1000", 10)], book_net_rupees=Decimal("0")
    )
    assert set(plan.deployed_by_identity) == {"only_bot"}


# -- lots -------------------------------------------------------------------------------------


def test_an_admitted_quantity_is_always_a_whole_number_of_lots() -> None:
    proposals = [
        BotProposal(
            bot_identity="option_bot",
            signal=_signal("NIFTY24800CE", TradeLeg.SELL, price_rupees="100", quantity=750),
            margin_rupees=None,
            lot_size=75,
        )
    ]
    plan = _supervisor(deployable_rupees=Decimal("100000")).supervise(
        proposals, book_net_rupees=Decimal("0")
    )
    for admitted in plan.admitted_by_identity.get("option_bot", ()):
        assert admitted.quantity % 75 == 0


def test_a_proposal_that_cannot_afford_one_whole_lot_is_refused_with_a_reason() -> None:
    proposals = [
        BotProposal(
            bot_identity="option_bot",
            signal=_signal("NIFTY24800CE", TradeLeg.SELL, price_rupees="1000", quantity=750),
            margin_rupees=None,
            lot_size=750,
        )
    ]
    # The directional bound is opened up so the LOT constraint is what is being exercised here
    # and not the exposure one — two different refusals with two different reasons.
    plan = _supervisor(
        deployable_rupees=Decimal("1000"), net_directional_fraction=Decimal("1")
    ).supervise(proposals, book_net_rupees=Decimal("0"))
    assert plan.admitted_count == 0
    assert any("lot" in reason for reason in plan.refusals_by_reason)


# -- margin -----------------------------------------------------------------------------------


def test_capital_consumed_is_margin_when_a_margin_is_given_not_notional() -> None:
    """A stock-futures lot at NOTIONAL consumes half a Rs 10,00,000 book; at margin, a tenth.

    Two longs and two shorts, so the book's net exposure is zero and the DIRECTIONAL bound cannot
    be what binds — this test is about capital and nothing else. `B36`/`A.145`: bounding futures by
    notional refused 23 of 23 real proposals on 2026-07-31.
    """
    sides = (TradeLeg.BUY, TradeLeg.BUY, TradeLeg.SELL, TradeLeg.SELL)
    at_notional = [
        _proposal("futures_bot", f"FUT{index}", side, "1000", 100)
        for index, side in enumerate(sides)
    ]
    at_margin = [
        BotProposal(
            bot_identity=proposal.bot_identity,
            signal=proposal.signal,
            margin_rupees=Decimal("10000"),
        )
        for proposal in at_notional
    ]
    supervisor = _supervisor(deployable_rupees=Decimal("200000"))

    on_notional = supervisor.supervise(at_notional, book_net_rupees=Decimal("0"))
    on_margin = supervisor.supervise(at_margin, book_net_rupees=Decimal("0"))

    assert on_notional.gross_admitted_rupees <= Decimal("200000")
    assert any(admitted.was_trimmed for admitted in on_notional.admitted)
    assert on_margin.admitted_count == 4
    assert not any(admitted.was_trimmed for admitted in on_margin.admitted)
    assert on_margin.gross_admitted_rupees == Decimal("40000")


# -- refusals ---------------------------------------------------------------------------------


def test_no_proposals_is_answerable_rather_than_fatal() -> None:
    plan = _supervisor().supervise([], book_net_rupees=Decimal("0"))
    assert plan.admitted_count == 0
    assert "nothing proposed" in plan.describe()


def test_a_non_positive_book_is_refused() -> None:
    with pytest.raises(PortfolioSupervisionError, match="deployable"):
        PortfolioProposalSupervisor(
            deployable_rupees=Decimal("0"), net_directional_fraction=Decimal("0.25")
        )


def test_a_bound_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(PortfolioSupervisionError, match="net_directional_fraction"):
        PortfolioProposalSupervisor(
            deployable_rupees=Decimal("1000000"), net_directional_fraction=Decimal("1.5")
        )


def test_every_refusal_carries_a_reason() -> None:
    """Oversized proposals are TRIMMED, not refused — refusal is for what cannot fit at all."""
    proposals = [
        _proposal(f"bot_{index}", f"SYM{index}", TradeLeg.BUY, "1000", 5000) for index in range(6)
    ]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("0"))
    assert all(admitted.was_trimmed for admitted in plan.admitted)
    assert sum(plan.refusals_by_reason.values()) == plan.refused_count

    # A proposal the bound leaves no room for IS refused, and says so.
    beyond = [_proposal("bot", "SYM", TradeLeg.BUY, "1000", 100)]
    refused = _supervisor().supervise(beyond, book_net_rupees=Decimal("900000"))
    assert refused.refused_count == 1
    assert all(reason for reason in refused.refusals_by_reason)


def test_the_plan_is_deterministic() -> None:
    proposals = [
        _proposal(f"bot_{index % 3}", f"SYM{index}", TradeLeg.BUY, "1000", 100)
        for index in range(9)
    ]
    supervisor = _supervisor()
    first = supervisor.supervise(proposals, book_net_rupees=Decimal("0"))
    second = supervisor.supervise(proposals, book_net_rupees=Decimal("0"))
    assert [a.signal.trading_symbol for a in first.admitted] == [
        a.signal.trading_symbol for a in second.admitted
    ]
    assert [a.quantity for a in first.admitted] == [a.quantity for a in second.admitted]


def test_an_admitted_quantity_never_exceeds_what_was_proposed() -> None:
    proposals = [_proposal("bot", "SYM", TradeLeg.BUY, "10", 100)]
    plan = _supervisor().supervise(proposals, book_net_rupees=Decimal("0"))
    assert plan.admitted_by_identity["bot"][0].quantity <= 100
