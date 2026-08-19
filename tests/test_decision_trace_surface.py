"""Tests for the decision-trace surface — `L13.29`'s `R.08` panel.

`A.29` required the trace before this page so the page could not be a reconstruction. The tests
that matter therefore assert it cannot flatter: an empty store must not read as a clean session,
and the unexplained count must be visible rather than rounded away.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from nse_algo_trader.dashboard.decision_trace_surface_renderer import (
    _BAR_HUE,
    render_decision_trace_page,
)
from nse_algo_trader.decision_trace.decision_trace_record import (
    CandidateAction,
    ConsultedInput,
    DecisionTrace,
    DecisionTraceSummary,
    GateEvaluation,
    GateOutcome,
)

INDIA = ZoneInfo("Asia/Kolkata")
MOMENT = datetime(2026, 8, 17, 11, 30, tzinfo=INDIA)


def _trace(chosen: str = "enter_long") -> DecisionTrace:
    return DecisionTrace(
        decided_at=MOMENT,
        bot_identity="cash_intraday_mean_reversion_bot",
        instrument_token=738561,
        trading_symbol="RELIANCE",
        inputs=(
            ConsultedInput(
                name="deviation_sigma",
                value="-2.31",
                source="strategy.intraday_mean_reversion_engine",
                as_of=MOMENT - timedelta(minutes=5),
            ),
        ),
        candidates=(
            CandidateAction(action="abstain", why_considered="always available"),
            CandidateAction(action="enter_long", why_considered="beyond its band"),
        ),
        gates=(
            GateEvaluation(
                gate="deviation_band",
                outcome=GateOutcome.PASSED,
                margin=Decimal("0.24"),
                threshold=Decimal("2.07"),
                detail="cleared its band",
            ),
        ),
        chosen_action=chosen,
        confidence=0.62,
        mechanism="mean reversion",
    )


def _summary(*, total: int = 100, unexplained: int = 0) -> DecisionTraceSummary:
    return DecisionTraceSummary(
        session_date=date(2026, 8, 17),
        total=total,
        by_action={"abstain": total - 11, "enter_long": 11},
        by_binding_gate={"deviation_band": total - unexplained - 20, "risk_order_rate": 20},
        unexplained=unexplained,
        sample_acted=(_trace(),),
    )


def test_no_traces_reads_as_not_recorded_rather_than_a_clean_session() -> None:
    """"Nothing went wrong" and "nothing was recorded" are opposite facts."""
    page = render_decision_trace_page(None)
    assert "NOT RECORDED" in page
    assert "cannot be reconstructed" in page


def test_a_summary_with_zero_traces_is_also_not_recorded() -> None:
    page = render_decision_trace_page(_summary(total=0))
    assert "NOT RECORDED" in page


def test_the_unexplained_count_is_shown_and_never_rounded_away() -> None:
    """The review found 380 traces blaming a passing gate because "nothing bound" did not exist."""
    page = render_decision_trace_page(_summary(total=1000, unexplained=4))
    assert "nothing bound" in page
    assert "4" in page
    assert "unexplained" in page


def test_an_incomplete_explanation_is_coloured_critical_not_neutral() -> None:
    incomplete = render_decision_trace_page(_summary(total=1000, unexplained=4))
    complete = render_decision_trace_page(_summary(total=1000, unexplained=0))
    assert incomplete.count("#b3261e") > complete.count("#b3261e")


def test_every_bar_carries_its_own_count_as_a_direct_label() -> None:
    """One series, so no legend — which means the value must be on the mark."""
    page = render_decision_trace_page(_summary(total=100))
    assert "risk_order_rate" in page
    assert "20" in page
    assert _BAR_HUE in page


def test_the_bars_are_one_hue_because_this_is_magnitude_not_identity() -> None:
    """A categorical palette would claim the gates are different KINDS rather than counts."""
    page = render_decision_trace_page(_summary(total=100))
    assert page.count(_BAR_HUE) >= 2


def test_the_sample_shows_what_was_consulted_and_what_bound() -> None:
    page = render_decision_trace_page(_summary())
    assert "deviation_sigma=-2.31" in page
    assert "binding constraint" in page


def test_the_page_states_that_reasoning_is_recorded_not_reconstructed() -> None:
    page = render_decision_trace_page(_summary())
    assert "never reconstructed" in page
    assert "A.29" in page


def test_a_symbol_is_escaped_rather_than_injected() -> None:
    summary = _summary()
    hostile = DecisionTrace(
        decided_at=MOMENT,
        bot_identity="b",
        instrument_token=1,
        trading_symbol="<script>alert(1)</script>",
        inputs=_trace().inputs,
        candidates=_trace().candidates,
        gates=_trace().gates,
        chosen_action="enter_long",
        confidence=0.5,
        mechanism="m",
    )
    page = render_decision_trace_page(
        DecisionTraceSummary(
            session_date=summary.session_date,
            total=summary.total,
            by_action=summary.by_action,
            by_binding_gate=summary.by_binding_gate,
            unexplained=summary.unexplained,
            sample_acted=(hostile,),
        )
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_incomplete_explanation_never_displays_as_one_hundred_percent() -> None:
    """4 unexplained in 21,270 is 99.98%, which `:.1%` renders as "100.0%".

    The live page did exactly that: it claimed every decision was accounted for while the tile
    beside it reported four that were not. A rounding that turns "nearly all" into "all" is the
    flattering direction, and this is the one number on the page that must not flatter.
    """
    nearly = render_decision_trace_page(_summary(total=21270, unexplained=4))
    assert "100.0%" not in nearly
    assert "<" in nearly

    complete = render_decision_trace_page(_summary(total=21270, unexplained=0))
    assert "100.0%" in complete


def test_times_are_shown_in_market_time_not_the_storage_timezone() -> None:
    """Traces are STORED in UTC so one instant is one row; they must be READ in IST.

    The first live render showed `04:10` for a decision taken at 09:40 IST. A dashboard for one
    exchange that prints another timezone's clock without saying so is wrong, not merely ambiguous.
    """
    from zoneinfo import ZoneInfo

    utc_trace = DecisionTrace(
        decided_at=MOMENT.astimezone(ZoneInfo("UTC")),
        bot_identity="b",
        instrument_token=1,
        trading_symbol="RELIANCE",
        inputs=_trace().inputs,
        candidates=_trace().candidates,
        gates=_trace().gates,
        chosen_action="enter_long",
        confidence=0.5,
        mechanism="m",
    )
    base = _summary()
    page = render_decision_trace_page(
        DecisionTraceSummary(
            session_date=base.session_date,
            total=base.total,
            by_action=base.by_action,
            by_binding_gate=base.by_binding_gate,
            unexplained=base.unexplained,
            sample_acted=(utc_trace,),
        )
    )
    assert "11:30" in page, "the IST wall-clock of the decision"
    assert "06:00" not in page, "the UTC spelling must not reach the reader"
    assert "IST" in page
