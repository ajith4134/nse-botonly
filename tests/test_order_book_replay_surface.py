"""`L0.22` surface — the page, and the ways a dashboard can lie by omission.

`R.08` says a feature's status must be MEASURED. The failure mode these tests exist for is
not a crash: it is a page that renders beautifully while showing a sample, a stale session,
or a fabricated distribution, and looks exactly like one that measured everything.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nse_algo_trader.dashboard.dashboard_server import build_dashboard_app
from nse_algo_trader.dashboard.order_book_replay_surface_renderer import (
    _DARK_SERIES,
    _LIGHT_SERIES,
    render_order_book_replay_page,
)
from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    SessionPriceBasisCoverage,
)
from nse_algo_trader.market_depth.bar_tape_join_verdict_store import (
    SessionVerificationCoverage,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    InstrumentCoverageReport,
)
from nse_algo_trader.replay_session_clock import IST, session_for

LIVE_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")


def _report(**overrides: object) -> InstrumentCoverageReport:
    fields: dict[str, object] = {
        "session_date": date(2026, 8, 11),
        "instruments_replayed": 25,
        "feature_rows_emitted": 135_401,
        "quote_rule_fraction": 0.11,
        "tick_rule_fraction": 0.04,
        "zero_tick_fraction": 0.02,
        "unclassified_fraction": 0.83,
        "median_gap_milliseconds": 1_150.0,
        "gap_millisecond_deciles": (
            60.0,
            120.0,
            240.0,
            480.0,
            900.0,
            1_400.0,
            2_100.0,
            3_600.0,
            9_800.0,
        ),
        "duplicate_row_fraction": 0.27,
        "capture_run_boundaries_crossed": 3,
    }
    fields.update(overrides)
    return InstrumentCoverageReport(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_page_states_the_sample_size_rather_than_implying_the_universe() -> None:
    """A bounded replay shown without its bound is the hand-authored status R.08 forbids."""
    page = render_order_book_replay_page(_report())
    assert "25 instruments replayed" in page
    assert "bounded number of\ninstruments" in page, "the sample bound must be stated in prose too"


@pytest.mark.unit
def test_every_rule_share_is_printed_as_a_number_not_only_a_bar() -> None:
    """The light palette returns a contrast WARN, which obligates visible labels."""
    page = render_order_book_replay_page(_report())
    for share in ("11.0%", "4.0%", "2.0%", "83.0%"):
        assert share in page, f"{share} appears only as a bar"


@pytest.mark.unit
def test_a_table_view_accompanies_the_bars() -> None:
    page = render_order_book_replay_page(_report())
    assert "<table>" in page
    assert "11.00%" in page, "the table carries its own precision"


@pytest.mark.unit
def test_each_series_colour_is_declared_exactly_once() -> None:
    """Dark mode redefines VALUES, not rules.

    A second copy of `.series-N{background:…}` inside a dark block is how a stylesheet
    acquires rules that contradict each other — this dashboard has already lost five rules
    to a CSS defect once.
    """
    page = render_order_book_replay_page(_report())
    for slot in range(len(_LIGHT_SERIES)):
        assert page.count(f".series-{slot}{{background:") == 1


@pytest.mark.unit
def test_both_palettes_are_present_and_dark_is_selected_not_inverted() -> None:
    page = render_order_book_replay_page(_report())
    for colour in _LIGHT_SERIES + _DARK_SERIES:
        assert colour in page, f"{colour} missing — a mode would fall back to the other"
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page, "the explicit toggle must win too"


@pytest.mark.adversarial
def test_a_distribution_is_not_drawn_from_too_few_samples() -> None:
    """Nine bars invented from three transitions would look identical to a real one."""
    page = render_order_book_replay_page(_report(gap_millisecond_deciles=()))
    assert "Fewer than ten transitions" in page
    assert "p10" not in page


@pytest.mark.adversarial
def test_an_all_zero_report_renders_without_dividing_by_zero() -> None:
    """A session with no rows is a real state — an empty tape, or a token with no data."""
    page = render_order_book_replay_page(
        _report(
            feature_rows_emitted=0,
            quote_rule_fraction=0.0,
            tick_rule_fraction=0.0,
            zero_tick_fraction=0.0,
            unclassified_fraction=0.0,
            median_gap_milliseconds=0.0,
            gap_millisecond_deciles=(),
            duplicate_row_fraction=0.0,
        )
    )
    assert "0.0%" in page
    assert "nan" not in page.lower()


@pytest.mark.adversarial
def test_no_bar_can_overflow_its_track() -> None:
    """A width above 100% escapes the panel and silently misreads as a bigger value."""
    page = render_order_book_replay_page(_report())
    widths = [float(match) for match in re.findall(r"width:([0-9.]+)%", page)]
    assert widths, "no bars rendered at all"
    assert max(widths) <= 100.0


@pytest.mark.unit
def test_the_engine_and_its_renderer_are_declared_as_surfaced_modules() -> None:
    """`R.08`: the wall's `panel` column must not over-claim, and must not under-claim."""
    from nse_algo_trader.dashboard.dashboard_server import SURFACED_MODULES

    assert "nse_algo_trader.market_depth.order_book_snapshot_replay_engine" in SURFACED_MODULES
    assert "nse_algo_trader.dashboard.order_book_replay_surface_renderer" in SURFACED_MODULES


def _todays_session_has_opened() -> bool:
    """Has the session the route will replay actually opened yet?

    The route replays TODAY's tape, and before the open there is nothing in it to replay. This test
    failed every night between midnight IST and 09:15 — an ABSENCE of data reported as a broken
    route, which is the distinction the route's own 503 message draws ("a wrong token and an
    uncaptured instrument are different problems") and the one `GateVerdict` draws between `VETO`
    and `UNPRICEABLE`.

    Measured on the night it was found: the capture had started at 00:00:17 IST and written real
    parquet shards, so "are there files?" answers yes while "is there a book to replay?" answers no.
    The open is the honest condition, not the file count.

    Nothing is hidden by this. After the open a missing tape still FAILS, and a capture that never
    ran is reported directly by the `depth capture` step of `run_daily_operations.py`, which is
    where that belongs.
    """
    return datetime.now(IST) >= session_for(datetime.now(IST).date()).opens_at


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_the_route_renders_the_real_tape() -> None:
    """R.05 — the route replays the actual recorded session, not a fixture."""
    from nse_algo_trader.dashboard.dashboard_server import ACCESS_TOKEN_PATH

    if not ACCESS_TOKEN_PATH.exists():
        pytest.skip("no dashboard access token on this host")
    if not _todays_session_has_opened():
        pytest.skip("today's session has not opened yet — there is no book recorded to replay")
    token = ACCESS_TOKEN_PATH.read_text().strip()
    client = TestClient(build_dashboard_app())
    response = client.get("/microstructure", params={"key": token, "instrument_limit": 3})
    assert response.status_code == 200, response.text[:300]
    assert "Order-book snapshot replay" in response.text
    assert "3 instruments replayed" in response.text
    assert "feature rows" in response.text


# ------------------------------------------------- the join panel (`M14`, `R.08`)


def _coverage(**overrides: object) -> SessionVerificationCoverage:
    fields: dict[str, object] = {
        "session_date": date(2026, 8, 11),
        "significance": 0.01,
        "instruments_verified": 3_109,
        "instruments_refuted": 121,
        "instruments_unverifiable": 5_770,
        "staleness_quantiles": (0.95,),
        "rows_without_a_staleness_quantile": 0,
        "null_models": ("beta_binomial_leave_one_out",),
        "rows_without_a_null_model": 0,
        "significances": (0.01,),
        "minimum_detectable_disagreement_rates": (0.5,),
        "rows_without_a_minimum_detectable_rate": 0,
        "minimum_comparisons_to_verify": 22,
        "mean_null_intra_instrument_correlation": 0.34,
        "instruments_with_price_basis_divergence": 4,
        "bars_total": 500_000,
        "comparisons_verifiable": 120_000,
    }
    fields.update(overrides)
    return SessionVerificationCoverage(**fields)  # type: ignore[arg-type]


def test_an_unverified_join_renders_as_unrun_and_never_as_clean() -> None:
    """The `A.41` distinction on the surface: "nothing refused" and "nothing checked" are
    different facts, and a reassuring zero for both is a hand-authored status in all but name."""
    page = render_order_book_replay_page(_report(), join_coverage=None)
    assert "NOT VERIFIED for this session" in page
    assert "This is not a pass" in page


def test_the_panel_names_the_null_the_verdicts_were_produced_under() -> None:
    """A reader cannot compare two sessions' counts without knowing they were judged the same
    way, and after `A.123` there are two nulls in this system's history."""
    page = render_order_book_replay_page(_report(), join_coverage=_coverage())
    assert "beta_binomial_leave_one_out null" in page
    assert "3,109" in page and "121" in page


def test_a_session_written_under_two_policies_says_so_instead_of_adding_them_up() -> None:
    """`B6`'s lesson has to reach the SURFACE.

    An interrupted sweep left 2026-08-11 at staleness 0.95 beside two sessions at 0.99, and `B6`
    had just proved the quantile changes the verdicts. The store can now tell; this asserts the
    page does not quietly render the sum as one verification.
    """
    mixed = _coverage(staleness_quantiles=(0.95, 0.99))
    assert not mixed.is_internally_consistent
    page = render_order_book_replay_page(_report(), join_coverage=mixed)
    assert "NOT ONE VERIFICATION" in page
    assert "incomparable" in page

    two_nulls = _coverage(null_models=("beta_binomial_leave_one_out", "binomial"))
    assert not two_nulls.is_internally_consistent
    assert "NOT ONE VERIFICATION" in render_order_book_replay_page(
        _report(), join_coverage=two_nulls
    )

    two_significances = _coverage(significances=(0.01, 0.5))
    assert not two_significances.is_internally_consistent, (
        "significance is the engine's only policy input and it decides every verdict"
    )
    assert "NOT ONE VERIFICATION" in render_order_book_replay_page(
        _report(), join_coverage=two_significances
    )

    unrecorded_mixed = _coverage(rows_without_a_null_model=12)
    assert not unrecorded_mixed.is_internally_consistent, (
        "rows that cannot say which null produced them are their own setting, not a match"
    )
    assert "NOT ONE VERIFICATION" in render_order_book_replay_page(
        _report(), join_coverage=unrecorded_mixed
    )


def test_a_consistent_session_carries_no_inconsistency_banner() -> None:
    """The converse, so the banner cannot be a decoration that is always on."""
    page = render_order_book_replay_page(_report(), join_coverage=_coverage())
    assert "NOT ONE VERIFICATION" not in page


def test_the_measured_dispersion_reaches_the_surface() -> None:
    """`rho-hat` was written on every verdict row and read by NOTHING (`R.06`). It is the one
    number that says whether replacing the binomial changed anything, and `docs/research/241` §5
    names it as the model's falsification signal, so it has to be visible."""
    page = render_order_book_replay_page(_report(), join_coverage=_coverage())
    assert "intra-instrument rho" in page
    assert "0.340" in page

    unrecorded = _coverage(mean_null_intra_instrument_correlation=None)
    rendered = render_order_book_replay_page(_report(), join_coverage=unrecorded)
    assert "intra-instrument rho" in rendered
    assert "0.340" not in rendered


def test_the_panel_states_what_verified_claims() -> None:
    """`A.124`. "3,204 verified" means nothing without the claim behind it, and the claim is an
    operator input that can differ between runs — so a reader who is not told it will assume one."""
    page = render_order_book_replay_page(_report(), join_coverage=_coverage())
    assert "enough evidence to have CAUGHT an instrument disagreeing on 50%" in page

    predating = _coverage(
        minimum_detectable_disagreement_rates=(), rows_without_a_minimum_detectable_rate=12
    )
    rendered = render_order_book_replay_page(_report(), join_coverage=predating)
    assert "do not record what their verified verdicts claimed" in rendered


def test_two_different_claims_in_one_session_is_two_verifications() -> None:
    """The third policy input gets the same treatment as `significance` and `null_model`: a session
    verified under two claims about what VERIFIED means holds two incomparable measurements."""
    mixed = _coverage(minimum_detectable_disagreement_rates=(0.5, 1.0))
    assert not mixed.is_internally_consistent
    assert "NOT ONE VERIFICATION" in render_order_book_replay_page(_report(), join_coverage=mixed)


def test_the_claim_sentence_is_not_a_hardcoded_fifty_percent() -> None:
    """`C1`. Every panel test used a 0.5 claim, so a renderer that hardcoded "50%" passed all of
    them. A second fixture at a different claim is what makes the sentence a rendering of the
    data rather than a constant."""
    quarter = render_order_book_replay_page(
        _report(),
        join_coverage=_coverage(
            minimum_detectable_disagreement_rates=(0.25,), minimum_comparisons_to_verify=298
        ),
    )
    assert "disagreeing on 25% of its comparable bars" in quarter
    assert "50%" not in quarter
    assert "which took 298 comparisons" in quarter, (
        "the claim is only checkable beside its cost (`M4`)"
    )

    tiny = render_order_book_replay_page(
        _report(), join_coverage=_coverage(minimum_detectable_disagreement_rates=(0.004,))
    )
    assert "disagreeing on 0.4% of its comparable bars" in tiny, "`L1`: `:.0%` printed this as 0%"


def test_the_inconsistency_banner_names_every_setting_it_mixed() -> None:
    """`C2`/`M5`. The banner asserted only that "NOT ONE VERIFICATION" appears, so dropping the
    settings it names survived — and rows predating `A.124` were never counted in it at all, so a
    mixed store rendered four fields that all looked clean."""
    mixed = _coverage(rows_without_a_minimum_detectable_rate=4_000)
    assert not mixed.is_internally_consistent
    page = render_order_book_replay_page(_report(), join_coverage=mixed)
    assert "NOT ONE VERIFICATION" in page
    assert "4,000 rows unrecorded" in page, "the banner must say WHICH rows cannot say"
    assert "4,000 rows predate A.124 and are NOT covered by that claim" in page, (
        "and the claim sentence must not assert 50% over rows that never recorded one"
    )


# ------------------------------------------------------ the price-basis panel (`L0.37`, `R.08`)


def _basis(**overrides: object) -> SessionPriceBasisCoverage:
    fields: dict[str, object] = {
        "session_date": date(2026, 8, 13),
        "bars_on_the_traded_basis": 40_000,
        "bars_adjusted_after_the_session": 5_000,
        "bars_on_an_unknown_basis": 2_687,
        "bars_on_an_impossible_basis": 0,
        "instruments_at_risk": frozenset({359937}),
        "latest_adjustment_basis": date(2026, 8, 20),
    }
    fields.update(overrides)
    return SessionPriceBasisCoverage(**fields)  # type: ignore[arg-type]


def test_a_session_with_no_stored_bars_says_so_rather_than_scoring_zero() -> None:
    page = render_order_book_replay_page(_report(), price_basis=None)
    assert "nothing to say about what they are denominated in" in page
    assert "Not a pass" in page


def test_the_basis_panel_separates_the_repairable_shortfall_from_the_permanent_one() -> None:
    """Two counts, not one score: bars fetched late can be fetched on the right day going forward,
    bars with no basis at all never can."""
    page = render_order_book_replay_page(_report(), price_basis=_basis())
    assert "adjusted after the session" in page
    assert "basis unrecorded" in page
    assert "permanently unrecoverable" in page
    assert "2026-08-20" in page, "the furthest basis date is the size of the exposure"


def test_an_entirely_unrecorded_session_is_told_it_is_not_a_pass() -> None:
    """The live store's state on every session: 0 of 1,022,751 bars carry a basis. A panel that
    rendered that as `0.0%` and moved on would be the `A.41` fold this feature exists to end."""
    page = render_order_book_replay_page(
        _report(),
        price_basis=_basis(
            bars_on_the_traded_basis=0,
            bars_adjusted_after_the_session=0,
            bars_on_an_unknown_basis=47_687,
            instruments_at_risk=frozenset(),
            latest_adjustment_basis=None,
        ),
    )
    assert "permanently unrecoverable" in page
    assert "not a clean bill of health" in page
    assert "0.0%" in page


def test_a_fully_traded_basis_session_carries_no_alarm() -> None:
    """The converse, so the warning cannot be decoration that is always on."""
    page = render_order_book_replay_page(
        _report(),
        price_basis=_basis(
            bars_adjusted_after_the_session=0,
            bars_on_an_unknown_basis=0,
            instruments_at_risk=frozenset(),
        ),
    )
    assert "100.0%" in page
    assert "not a clean bill of health" not in page


def test_an_impossible_basis_is_shown_rather_than_returned_as_a_500() -> None:
    """`H3` of the `A.126` review. A bar adjusted as of BEFORE its own session is a store defect,
    and the classifier is right to raise on it — but the aggregate behind this panel must not, or
    one bad row makes 209,912 honest ones unreportable and `/replay` answers 500."""
    page = render_order_book_replay_page(
        _report(), price_basis=_basis(bars_on_an_impossible_basis=3)
    )
    assert "impossible basis" in page
    assert "a store defect" in page
