"""Tests for the intraday live paper book (`L5.30`, `A.146`).

The loop counted 376 proposals on a real tick and produced zero positions, zero fills and zero P&L,
because nothing between the bots and the track record existed. These tests defend the four things
that book has to get right: it carries positions across ticks, it survives a restart, it never
closes a position at a price it did not observe, and it accrues each closed trade exactly once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.paper_loop.bot_maturity_ladder import PaperTrackRecordStore
from nse_algo_trader.paper_loop.live_paper_book import LivePaperBook
from nse_algo_trader.portfolio.portfolio_proposal_supervisor import (
    AdmittedProposal,
    PortfolioPlan,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

SESSION = date(2026, 8, 19)
OPENED_AT = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 8, 19, 15, 20, tzinfo=UTC)


def _admitted(
    bot: str, token: int, side: TradeLeg, price_rupees: str, quantity: int
) -> AdmittedProposal:
    signal = PricedSignal(
        instrument_token=token,
        trading_symbol=f"SYM{token}",
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=side,
        decided_at=OPENED_AT,
        reference_price_paise=Decimal(price_rupees) * 100,
        expected_edge_bps=Decimal("30"),
        proposed_quantity=quantity,
        edge_basis=EdgeBasis.CALIBRATED_MODEL,
        source="test",
        conviction=Decimal("0.6"),
    )
    return AdmittedProposal(
        bot_identity=bot,
        signal=signal,
        quantity=quantity,
        capital_rupees=Decimal(price_rupees) * quantity,
        signed_notional_rupees=(
            Decimal(price_rupees) * quantity * (1 if side is TradeLeg.BUY else -1)
        ),
    )


def _plan(*admitted: AdmittedProposal) -> PortfolioPlan:
    return PortfolioPlan(
        admitted=tuple(admitted),
        refusals_by_reason={},
        book_net_before_rupees=Decimal("0"),
        net_after_rupees=Decimal("0"),
        gross_admitted_rupees=Decimal("0"),
        deployed_by_identity={},
        proposals_seen=len(admitted),
    )


@pytest.fixture
def book(tmp_path: Path) -> LivePaperBook:
    return LivePaperBook(database=tmp_path / "live_paper_book.sqlite3")


# -- carrying ---------------------------------------------------------------------------------


def test_an_admitted_proposal_becomes_an_open_position(book: LivePaperBook) -> None:
    opened = book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    assert opened == 1
    positions = book.open_positions()
    assert len(positions) == 1
    assert positions[0].bot_identity == "cash_bot"
    assert positions[0].quantity == 10


def test_the_same_bot_and_instrument_is_not_doubled_on_a_later_tick(book: LivePaperBook) -> None:
    """A deviation that has not closed yet is proposed again every five minutes. That is normal;
    doubling the position because of it is not."""
    plan = _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10))
    book.admit(plan, at=OPENED_AT, session_date=SESSION)
    again = book.admit(plan, at=OPENED_AT, session_date=SESSION)
    assert again == 0
    assert len(book.open_positions()) == 1


def test_two_bots_may_hold_the_same_instrument(book: LivePaperBook) -> None:
    """They are different bots with different records; netting them would hide both."""
    book.admit(
        _plan(
            _admitted("cash_bot", 1, TradeLeg.BUY, "100", 10),
            _admitted("options_bot", 1, TradeLeg.SELL, "100", 10),
        ),
        at=OPENED_AT,
        session_date=SESSION,
    )
    assert len(book.open_positions()) == 2


def test_the_book_survives_a_restart(tmp_path: Path) -> None:
    """A loop that forgets its book on restart never squares off what it opened before."""
    path = tmp_path / "live_paper_book.sqlite3"
    first = LivePaperBook(database=path)
    first.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    first.close()
    reopened = LivePaperBook(database=path)
    assert len(reopened.open_positions()) == 1


# -- marking ----------------------------------------------------------------------------------


def test_a_long_marks_up_when_the_price_rises(book: LivePaperBook) -> None:
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    mark = book.mark({1: Decimal("11000")}, at=CLOSED_AT)
    assert mark.gross_unrealised_rupees == Decimal("100")


def test_a_short_marks_up_when_the_price_falls(book: LivePaperBook) -> None:
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.SELL, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    mark = book.mark({1: Decimal("9000")}, at=CLOSED_AT)
    assert mark.gross_unrealised_rupees == Decimal("100")


def test_an_unpriced_position_is_counted_not_marked_at_zero(book: LivePaperBook) -> None:
    """Marking an unobserved position at zero move reads as flat when it is simply unobserved."""
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    mark = book.mark({}, at=CLOSED_AT)
    assert mark.unpriced_positions == 1
    assert mark.gross_unrealised_rupees == Decimal("0")
    assert "unpriced" in mark.describe()


def test_the_mark_is_attributed_per_bot(book: LivePaperBook) -> None:
    book.admit(
        _plan(
            _admitted("cash_bot", 1, TradeLeg.BUY, "100", 10),
            _admitted("options_bot", 2, TradeLeg.BUY, "100", 10),
        ),
        at=OPENED_AT,
        session_date=SESSION,
    )
    mark = book.mark({1: Decimal("11000"), 2: Decimal("9000")}, at=CLOSED_AT)
    assert mark.unrealised_by_identity["cash_bot"] == Decimal("100")
    assert mark.unrealised_by_identity["options_bot"] == Decimal("-100")


def test_net_exposure_nets_longs_against_shorts(book: LivePaperBook) -> None:
    book.admit(
        _plan(
            _admitted("cash_bot", 1, TradeLeg.BUY, "100", 10),
            _admitted("cash_bot", 2, TradeLeg.SELL, "100", 10),
        ),
        at=OPENED_AT,
        session_date=SESSION,
    )
    assert book.net_exposure_rupees() == Decimal("0")


# -- squaring off ------------------------------------------------------------------------------


def test_squaring_off_closes_the_position_and_reports_the_net(book: LivePaperBook) -> None:
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    outcome = book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert outcome.closed == 1
    assert outcome.gross_rupees == Decimal("100")
    assert book.open_positions() == ()


def test_a_position_the_tape_cannot_price_stays_open_rather_than_closing_flat(
    book: LivePaperBook,
) -> None:
    """Closing at entry books a zero P&L that reads as a flat trade, and the ladder counts it."""
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    outcome = book.square_off({}, at=CLOSED_AT)
    assert outcome.closed == 0
    assert outcome.unclosable == 1
    assert len(book.open_positions()) == 1
    assert "stay OPEN" in outcome.describe()


def test_costs_are_subtracted_per_trade(tmp_path: Path) -> None:
    def costs(position, exit_price_paise):  # noqa: ANN001, ANN202
        del position, exit_price_paise
        return Decimal("12.50")

    book = LivePaperBook(
        database=tmp_path / "live_paper_book.sqlite3", costs_rupees_for=costs
    )
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    outcome = book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert outcome.costs_rupees == Decimal("12.50")
    assert outcome.net_rupees == Decimal("87.50")


def test_a_trade_whose_costs_cannot_be_priced_stays_open(tmp_path: Path) -> None:
    """`R.03`: a trade closed at an invented cost is worse evidence than one left open."""

    def costs(position, exit_price_paise):  # noqa: ANN001, ANN202
        del position, exit_price_paise
        raise RuntimeError("no rule for this segment on this date")

    book = LivePaperBook(
        database=tmp_path / "live_paper_book.sqlite3", costs_rupees_for=costs
    )
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    outcome = book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert outcome.closed == 0
    assert outcome.unclosable == 1
    assert len(book.open_positions()) == 1


# -- accrual ----------------------------------------------------------------------------------


def test_a_closed_trade_reaches_the_track_record_under_its_own_bot(tmp_path: Path) -> None:
    store = PaperTrackRecordStore(tmp_path / "paper_track_record.sqlite3")
    book = LivePaperBook(database=tmp_path / "live_paper_book.sqlite3", track_record=store)
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    outcome = book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert outcome.accrued == 1
    trades = store.closed_trades_for("cash_bot")
    assert len(trades) == 1
    assert trades[0].gross_rupees == Decimal("100")


def test_accrual_is_idempotent_across_a_re_square_off(tmp_path: Path) -> None:
    """`R.13`: doubling a bot's evidence promotes it for work it did once."""
    store = PaperTrackRecordStore(tmp_path / "paper_track_record.sqlite3")
    book = LivePaperBook(database=tmp_path / "live_paper_book.sqlite3", track_record=store)
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    second = book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert second.accrued == 0
    assert len(store.closed_trades_for("cash_bot")) == 1


def test_the_stated_conviction_is_carried_into_the_record(tmp_path: Path) -> None:
    """`L5.31`'s calibrator has nothing to fit without it, and a probability reconstructed after
    the outcome is known is not a forecast."""
    store = PaperTrackRecordStore(tmp_path / "paper_track_record.sqlite3")
    book = LivePaperBook(database=tmp_path / "live_paper_book.sqlite3", track_record=store)
    book.admit(
        _plan(_admitted("cash_bot", 1, TradeLeg.BUY, "100", 10)),
        at=OPENED_AT,
        session_date=SESSION,
    )
    book.square_off({1: Decimal("11000")}, at=CLOSED_AT)
    assert store.closed_trades_for("cash_bot")[0].stated_win_probability == pytest.approx(0.6)
