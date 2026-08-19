"""Tests for the activation-ladder surface — `L5.30`'s `R.08` panel.

The page's job is to make the FIRST of `R.22`'s two keys visible. So the tests that matter are the
ones asserting it cannot flatter: an empty store must not render as a clean slate, a RETIRED bot
must be legible as evidence-against rather than as absence-of-evidence, and no rung may be carried
by colour alone.
"""

from __future__ import annotations

from nse_algo_trader.dashboard.bot_maturity_surface_renderer import (
    _RUNG_COLOUR,
    _RUNG_MEANING,
    render_bot_maturity_page,
)
from nse_algo_trader.paper_loop.bot_maturity_ladder import LadderAssessment
from nse_algo_trader.segment_bots.segment_bot_protocol import BotMaturityRung


def _assessment(
    *,
    identity: str = "cash_intraday_reference_bot",
    rung: BotMaturityRung = BotMaturityRung.OBSERVING,
    trades: int = 40,
    sessions: int = 4,
    observed: float = 0.55,
    break_even: float = 0.50,
    posterior: float = 0.70,
    below: bool = False,
) -> LadderAssessment:
    return LadderAssessment(
        bot_identity=identity,
        rung=rung,
        closed_trades=trades,
        sessions=sessions,
        observed_win_rate=observed,
        break_even_win_rate=break_even,
        posterior_above_break_even=posterior,
        is_decisively_below_break_even=below,
        reason="a reason long enough to be worth reading on the page",
    )


def test_an_empty_store_says_not_recorded_rather_than_a_clean_slate() -> None:
    """"No record" and "a record of no failures" are opposite facts.

    Rendering the first as the second is the most flattering mistake this page could make, and it
    was the project's permanent state until 2026-08-17 (`B15`).
    """
    page = render_bot_maturity_page(())
    assert "NOT RECORDED" in page
    assert "B15" in page


def test_every_rung_carries_its_name_in_text_not_only_a_colour() -> None:
    """Reserved-status rule: the dot is recognition, the word is the fact.

    Also the relief the palette validator requires — the amber and red steps fall below 3:1 against
    their surface, so they may not be the sole carrier of anything.
    """
    for rung in BotMaturityRung:
        page = render_bot_maturity_page((_assessment(rung=rung),))
        assert rung.label in page, f"{rung} renders without its name"
        assert _RUNG_MEANING[rung] in page


def test_a_retired_bot_is_described_as_evidence_against_not_absence_of_evidence() -> None:
    page = render_bot_maturity_page(
        (_assessment(rung=BotMaturityRung.RETIRED, below=True, posterior=0.0),)
    )
    assert "evidence AGAINST" in page
    assert "loses money" in page


def test_the_page_states_that_no_bot_on_it_is_armed() -> None:
    """`R.22`: the rung is the first key, never both. A reader must not infer otherwise."""
    page = render_bot_maturity_page((_assessment(),))
    assert "no bot on this page is armed" in page
    assert "cannot arm" in page or "none can arm" in page


def test_the_win_rate_mark_is_green_above_its_own_break_even_and_red_below() -> None:
    """The mark's only job is showing WHICH SIDE of its own threshold the bot sits on."""
    ahead = render_bot_maturity_page((_assessment(observed=0.60, break_even=0.50),))
    behind = render_bot_maturity_page((_assessment(observed=0.40, break_even=0.50),))
    assert "#0ca30c" in ahead
    assert "#b3261e" in behind


def test_the_mark_is_labelled_in_text_so_it_is_never_the_only_carrier() -> None:
    page = render_bot_maturity_page((_assessment(observed=0.55, break_even=0.50),))
    assert "55.0%" in page
    assert "50.0%" in page
    assert "aria-label" in page


def test_a_rate_beyond_the_track_is_clamped_rather_than_overflowing_the_bar() -> None:
    """A break-even of 1.0 is real — it is what a bot with no winners at all reports."""
    page = render_bot_maturity_page((_assessment(observed=0.0, break_even=1.0),))
    assert "width:0.0%" in page
    assert "left:100.0%" in page


def test_bots_are_ranked_with_the_furthest_along_first() -> None:
    page = render_bot_maturity_page(
        (
            _assessment(identity="stock_options_retired_bot", rung=BotMaturityRung.RETIRED),
            _assessment(
                identity="cash_intraday_candidate_bot",
                rung=BotMaturityRung.GRADUATION_CANDIDATE,
            ),
        )
    )
    assert page.index("cash_intraday_candidate_bot") < page.index("stock_options_retired_bot")


def test_cold_start_is_the_neutral_step_and_retired_is_the_critical_one() -> None:
    """"No evidence" must not wear a status hue; "evidence against" must not wear the neutral."""
    assert _RUNG_COLOUR[BotMaturityRung.COLD_START] == "#8a8a80"
    assert _RUNG_COLOUR[BotMaturityRung.RETIRED] == "#b3261e"
    assert _RUNG_COLOUR[BotMaturityRung.PAPER_QUALIFIED] == "#0ca30c"


def test_a_bot_identity_is_escaped_rather_than_injected() -> None:
    page = render_bot_maturity_page((_assessment(identity="<script>alert(1)</script>"),))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_footer_explains_why_break_even_is_not_fifty_percent() -> None:
    """The single most counterintuitive thing on the page, so it is stated rather than implied."""
    page = render_bot_maturity_page((_assessment(),))
    assert "never assumed at 50%" in page
    assert "46.3%" in page
