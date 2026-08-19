"""Tests for the paper track record and the maturity ladder — `L5.30`, spec `docs/research/255`.

The adversarial test that matters most is the last one: the ladder is fed the **real** 3,049-trade
record of `opening_range_breakout_v1`, which won 36.9% of the time with a symmetric payoff and lost
₹3.3 lakh, and must refuse to promote it. A ladder that would have promoted the known loser is wrong
however elegant its arithmetic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nse_algo_trader.paper_loop.bot_maturity_ladder import (
    BotMaturityLadder,
    ClosedPaperTrade,
    LadderPolicy,
    PaperTrackRecordError,
    PaperTrackRecordStore,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import BotMaturityRung

INDIA = ZoneInfo("Asia/Kolkata")
BOT = "cash_intraday_reference_bot"

POLICY = LadderPolicy(
    promotion_confidence=0.90,
    sustained_sessions_required=3,
    minimum_trades_for_a_posterior=20,
)


def _trade(
    *,
    session: date,
    key: str,
    gross_rupees: Decimal,
    costs_rupees: Decimal = Decimal("10"),
) -> ClosedPaperTrade:
    """One closed trade specified by its GROSS move, with costs applied on top.

    Specifying the NET was the first version and it quietly defeated the point: a +₹400 net win
    against a -₹400 net loss is symmetric, so break-even came out at exactly 50% and the test that
    was meant to prove costs move it proved nothing. Costs bite BOTH sides — they shrink the winner
    and deepen the loser — which is why equal gross moves need more than half the trades to win.
    """
    opened = datetime.combine(session, datetime.min.time(), tzinfo=INDIA).replace(hour=10)
    return ClosedPaperTrade(
        bot_identity=BOT,
        session_date=session,
        position_key=key,
        instrument_token=738561,
        trading_symbol="RELIANCE",
        side="buy",
        filled_quantity=1,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=30),
        close_reason="horizon expired",
        gross_rupees=gross_rupees,
        costs_rupees=costs_rupees,
    )


def _record(
    store: PaperTrackRecordStore,
    *,
    wins: int,
    losses: int,
    sessions: int = 4,
    win_size: Decimal = Decimal("400"),
    loss_size: Decimal = Decimal("400"),
) -> None:
    """Spread `wins` winners and `losses` losers evenly over `sessions` distinct days."""
    outcomes = [win_size] * wins + [-loss_size] * losses
    for index, outcome in enumerate(outcomes):
        session = date(2026, 8, 3) + timedelta(days=index % sessions)
        store.append(_trade(session=session, key=f"pos-{index}", gross_rupees=outcome))


@pytest.fixture
def store(tmp_path: Path) -> PaperTrackRecordStore:
    return PaperTrackRecordStore(tmp_path / "paper_track_record.sqlite3")


@pytest.fixture
def ladder(store: PaperTrackRecordStore) -> BotMaturityLadder:
    return BotMaturityLadder(store)


# ------------------------------------------------------------------ the store


def test_a_bot_with_no_record_is_cold_start_and_says_why(ladder: BotMaturityLadder) -> None:
    """An unknown bot is answered, never raised at — the board has to render something."""
    assessment = ladder.assess("index_options_unknown_bot", POLICY)
    assert assessment.rung is BotMaturityRung.COLD_START
    assert assessment.closed_trades == 0
    assert "no closed paper trades" in assessment.reason


def test_recording_the_same_trade_twice_does_not_double_the_evidence(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """A re-run of a session must not promote a bot for work it did once (`R.13`)."""
    trade = _trade(session=date(2026, 8, 3), key="pos-1", gross_rupees=Decimal("400"))
    assert store.append(trade) == 1
    assert store.append(trade) == 0
    assert ladder.assess(BOT, POLICY).closed_trades == 1


def test_only_this_bots_trades_count_toward_this_bots_rung(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """`R.22`: a bot must not graduate on evidence another bot earned.

    This is the rule the 3,481 retained trades violate if borrowed — they belong to three other
    strategies (`docs/research/254`).
    """
    _record(store, wins=40, losses=5)
    other = _trade(session=date(2026, 8, 3), key="other-1", gross_rupees=Decimal("-9999"))
    store.append(replace(other, bot_identity="stock_options_other_bot"))
    assert ladder.assess(BOT, POLICY).closed_trades == 45


def test_sessions_are_counted_distinctly(store: PaperTrackRecordStore) -> None:
    _record(store, wins=6, losses=6, sessions=3)
    assert len(store.sessions_for(BOT)) == 3


# ------------------------------------------------------------------ the inference


def test_too_few_trades_is_cold_start_not_a_verdict(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """Below the sample the posterior needs, the honest answer is "no opinion"."""
    _record(store, wins=5, losses=1)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung is BotMaturityRung.COLD_START
    assert "too few" in assessment.reason


def test_a_clearly_winning_bot_reaches_paper_qualified_or_better(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    _record(store, wins=60, losses=15, sessions=4)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung in (
        BotMaturityRung.PAPER_QUALIFIED,
        BotMaturityRung.GRADUATION_CANDIDATE,
    )
    assert assessment.posterior_above_break_even > POLICY.promotion_confidence


def test_a_marginal_bot_observes_rather_than_promotes(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """Enough trades to have an opinion, not enough separation to act on it."""
    _record(store, wins=26, losses=24)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung is BotMaturityRung.OBSERVING


def test_break_even_is_derived_from_the_bots_own_payoffs_and_costs_not_assumed(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """`R.03`: 50% is not break-even, and the gap is where the costs live.

    A bot whose winners are the same size as its losers needs MORE than half its trades to win,
    because every trade pays costs. That is the arithmetic the 3,481 retained trades demonstrate.
    """
    _record(store, wins=30, losses=30, win_size=Decimal("400"), loss_size=Decimal("400"))
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.break_even_win_rate > 0.5
    assert assessment.observed_win_rate == pytest.approx(0.5)
    assert assessment.rung is not BotMaturityRung.PAPER_QUALIFIED


def test_a_bot_with_larger_winners_needs_a_lower_win_rate(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """Break-even follows the payoff ratio, which is why it cannot be a constant."""
    _record(store, wins=20, losses=40, win_size=Decimal("1200"), loss_size=Decimal("300"))
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.break_even_win_rate < 0.5


def test_one_lucky_session_cannot_reach_graduation_candidate(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """Sustained across distinct sessions, or a single good morning promotes a bot."""
    _record(store, wins=60, losses=5, sessions=1)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung is BotMaturityRung.PAPER_QUALIFIED
    assert "1 session" in assessment.reason or "sustained" in assessment.reason


def test_a_decisively_losing_bot_is_named_as_such_not_left_observing(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """A ladder that can only promote is not a ladder."""
    _record(store, wins=10, losses=60)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung is BotMaturityRung.RETIRED
    assert assessment.is_decisively_below_break_even
    assert "evidence AGAINST" in assessment.reason
    # RETIRED sits BELOW cold start: "evidence against" and "no evidence" are opposite states.
    assert assessment.rung.ladder_position < BotMaturityRung.COLD_START.ladder_position


# ------------------------------------------------------------------ adversarial


@pytest.mark.adversarial
def test_no_evidence_whatsoever_can_produce_graduated(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """`R.22`: the system can NEVER self-promote to live. Not for any record, at any size."""
    _record(store, wins=5000, losses=1, sessions=200)
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.rung is not BotMaturityRung.GRADUATED
    assert assessment.rung is BotMaturityRung.GRADUATION_CANDIDATE
    assert not assessment.to_bot_maturity().is_armable_without_operator


@pytest.mark.adversarial
def test_the_assessment_converts_to_a_bot_maturity_the_protocol_accepts(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """The ladder feeds `SegmentBot.maturity()`, so its output must satisfy that contract."""
    _record(store, wins=60, losses=15)
    maturity = ladder.assess(BOT, POLICY).to_bot_maturity()
    assert maturity.closed_trades_observed == 75
    assert len(maturity.evidence) > 20


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(scale=st.integers(min_value=2, max_value=500))
def test_the_verdict_is_invariant_to_the_scale_of_the_rupee_amounts(
    tmp_path: Path, scale: int
) -> None:
    """Ten times the position size is the same evidence about the same edge.

    The property this suite ORIGINALLY asserted — "adding a winner never lowers the rung" — was
    vacuous, and as the adversarial review pointed out it is not even true of any expectancy-aware
    estimator: only a pure win-rate counter can satisfy it, which is the thing this engine exists
    not to be. The old test hard-coded `win_size=400`, so every added winner was exactly the current
    average and the threshold never moved. Scale invariance is a property the estimator genuinely
    has.
    """
    base = PaperTrackRecordStore(tmp_path / f"base_{scale}.sqlite3")
    _record(base, wins=30, losses=20, win_size=Decimal("400"), loss_size=Decimal("300"))

    scaled = PaperTrackRecordStore(tmp_path / f"scaled_{scale}.sqlite3")
    _record(
        scaled,
        wins=30,
        losses=20,
        win_size=Decimal(400 * scale),
        loss_size=Decimal(300 * scale),
    )
    assert (
        BotMaturityLadder(scaled).assess(BOT, POLICY).rung
        is BotMaturityLadder(base).assess(BOT, POLICY).rung
    )


@pytest.mark.adversarial
def test_a_record_whose_profit_is_one_lucky_trade_is_not_treated_like_a_steady_one(
    tmp_path: Path,
) -> None:
    """Magnitude concentration must be visible. To a win-rate test it was not.

    The review showed `30 wins of Rs 400` and `29 wins of Rs 13.79 plus one of Rs 11,600` — same
    counts, same total, same break-even — were byte-identical to the first version.
    """
    steady = PaperTrackRecordStore(tmp_path / "steady.sqlite3")
    _record(steady, wins=30, losses=20, win_size=Decimal("400"), loss_size=Decimal("400"))

    lucky = PaperTrackRecordStore(tmp_path / "lucky.sqlite3")
    for index in range(29):
        lucky.append(
            _trade(
                session=date(2026, 8, 3) + timedelta(days=index % 4),
                key=f"small-{index}",
                gross_rupees=Decimal("13.79"),
                costs_rupees=Decimal("0"),
            )
        )
    lucky.append(
        _trade(
            session=date(2026, 8, 3),
            key="jackpot",
            gross_rupees=Decimal("11600"),
            costs_rupees=Decimal("0"),
        )
    )
    for index in range(20):
        lucky.append(
            _trade(
                session=date(2026, 8, 3) + timedelta(days=index % 4),
                key=f"loss-{index}",
                gross_rupees=Decimal("-400"),
                costs_rupees=Decimal("0"),
            )
        )

    assert (
        BotMaturityLadder(lucky).assess(BOT, POLICY).posterior_above_break_even
        < BotMaturityLadder(steady).assess(BOT, POLICY).posterior_above_break_even
    )


@pytest.mark.adversarial
def test_a_single_enormous_winner_cannot_promote_a_four_percent_win_rate(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """The review's sharpest case: one Rs 1e9 win against 24 losses was a promotion."""
    store.append(
        _trade(
            session=date(2026, 8, 3),
            key="jackpot",
            gross_rupees=Decimal("1000000000"),
            costs_rupees=Decimal("0"),
        )
    )
    for index in range(24):
        store.append(
            _trade(
                session=date(2026, 8, 3) + timedelta(days=index % 4),
                key=f"loss-{index}",
                gross_rupees=Decimal("-400"),
                costs_rupees=Decimal("0"),
            )
        )
    assert ladder.assess(BOT, POLICY).rung is not BotMaturityRung.GRADUATION_CANDIDATE


@pytest.mark.adversarial
def test_losing_days_cannot_pad_the_sustained_session_count(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """The review padded three losing days with one big winning day and got a promotion."""
    for day in (3, 4, 5):
        for trade_index in range(8):
            store.append(
                _trade(
                    session=date(2026, 8, day),
                    key=f"lose-{day}-{trade_index}",
                    gross_rupees=Decimal("-400"),
                    costs_rupees=Decimal("0"),
                )
            )
    for trade_index in range(60):
        store.append(
            _trade(
                session=date(2026, 8, 6),
                key=f"win-{trade_index}",
                gross_rupees=Decimal("400"),
                costs_rupees=Decimal("0"),
            )
        )
    assessment = ladder.assess(BOT, POLICY)
    assert assessment.sessions == 4
    assert assessment.rung is not BotMaturityRung.GRADUATION_CANDIDATE


@pytest.mark.adversarial
def test_a_trade_with_no_bot_identity_is_refused() -> None:
    """It was accepted, and a trade nobody owns accrues to nobody."""
    good = _trade(session=date(2026, 8, 3), key="pos-1", gross_rupees=Decimal("400"))
    with pytest.raises(PaperTrackRecordError, match="no bot identity"):
        replace(good, bot_identity="  ")


@pytest.mark.adversarial
def test_a_trade_that_filled_nothing_is_refused() -> None:
    """`filled_quantity=-99` was accepted; a position that filled nothing has no outcome."""
    good = _trade(session=date(2026, 8, 3), key="pos-1", gross_rupees=Decimal("400"))
    with pytest.raises(PaperTrackRecordError, match="filled quantity"):
        replace(good, filled_quantity=-99)


@pytest.mark.adversarial
def test_negative_costs_are_refused() -> None:
    """The review's sharpest validation gap: negative costs turn a gross LOSS into a net win.

    Fifty trades with a total gross of -Rs 5,000 became a GRADUATION_CANDIDATE on this alone.
    """
    good = _trade(session=date(2026, 8, 3), key="pos-1", gross_rupees=Decimal("400"))
    with pytest.raises(PaperTrackRecordError, match="costs must not be negative"):
        replace(good, costs_rupees=Decimal("-500"))


@pytest.mark.adversarial
def test_a_session_label_that_disagrees_with_the_open_is_refused() -> None:
    """Unchecked, one day could be spread across three to earn a sustained-sessions promotion."""
    good = _trade(session=date(2026, 8, 3), key="pos-1", gross_rupees=Decimal("400"))
    with pytest.raises(PaperTrackRecordError, match="labelled session"):
        replace(good, session_date=date(2026, 8, 4))


@pytest.mark.adversarial
def test_a_different_trade_under_the_same_key_is_refused_not_silently_dropped(
    store: PaperTrackRecordStore,
) -> None:
    """`INSERT OR IGNORE` hid 196 of 260 real trades and promoted a Rs 56,000 loser."""
    first = _trade(session=date(2026, 8, 3), key="same-key", gross_rupees=Decimal("400"))
    assert store.append(first) == 1
    assert store.append(first) == 0
    with pytest.raises(PaperTrackRecordError, match="DIFFERENT trade"):
        store.append(replace(first, gross_rupees=Decimal("-9999")))


def test_a_scratch_trade_does_not_dilute_the_break_even_rate(
    store: PaperTrackRecordStore, ladder: BotMaturityLadder
) -> None:
    """200 zero-P&L trades moved break-even from 50.0% to 8.3% with total P&L unchanged."""
    _record(store, wins=20, losses=20, win_size=Decimal("1000"), loss_size=Decimal("1000"))
    before = ladder.assess(BOT, POLICY).break_even_win_rate
    for index in range(200):
        store.append(
            _trade(
                session=date(2026, 8, 3) + timedelta(days=index % 4),
                key=f"scratch-{index}",
                gross_rupees=Decimal("0"),
                costs_rupees=Decimal("0"),
            )
        )
    assert ladder.assess(BOT, POLICY).break_even_win_rate == pytest.approx(before)


@pytest.mark.adversarial
def test_a_policy_demanding_impossible_confidence_is_refused() -> None:
    with pytest.raises(PaperTrackRecordError, match="confidence"):
        LadderPolicy(
            promotion_confidence=1.0,
            sustained_sessions_required=3,
            minimum_trades_for_a_posterior=20,
        )


# ------------------------------------------------------------------ R.05 real data


RETAINED_TRADES = Path("~/.nse_algo_trader/experience_memory.sqlite3").expanduser()


@pytest.mark.adversarial
def test_the_ladder_refuses_to_promote_the_strategy_that_really_lost_33_lakh(
    tmp_path: Path,
) -> None:
    """`R.05`, and the strongest test available: replay the REAL retained record.

    `opening_range_breakout_v1` traded 3,049 times live, won 36.9% with a symmetric payoff and lost
    ₹3.56 lakh (`docs/research/254`). A ladder that would have promoted it is wrong, whatever its
    arithmetic looks like on synthetic data.

    Skips when the retained store is absent so the suite still runs on a fresh machine; on this
    server it is a genuine assertion against 3,049 real outcomes.
    """
    if not RETAINED_TRADES.exists():
        pytest.skip("the retained experience store is not on this machine")

    connection = sqlite3.connect(f"file:{RETAINED_TRADES}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT session_date, experiment_id, realized_pnl, total_fees FROM experience_nodes "
        "WHERE strategy_tag = 'opening_range_breakout_v1'"
    ).fetchall()
    assert len(rows) > 3000, f"expected the full retained slice, got {len(rows)}"

    store = PaperTrackRecordStore(tmp_path / "retained.sqlite3")
    for session_text, experiment_id, realised, fees in rows:
        store.append(
            _trade(
                session=date.fromisoformat(session_text),
                key=experiment_id,
                gross_rupees=Decimal(str(realised)) + Decimal(str(fees or 0)),
                costs_rupees=Decimal(str(fees or 0)),
            )
        )

    assessment = BotMaturityLadder(store).assess(BOT, POLICY)
    assert assessment.closed_trades == len(rows)
    assert assessment.rung is BotMaturityRung.RETIRED
    assert assessment.is_decisively_below_break_even
    assert assessment.observed_win_rate < assessment.break_even_win_rate
