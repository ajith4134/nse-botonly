"""`L0.22` surface — the page, and the ways a dashboard can lie by omission.

`R.08` says a feature's status must be MEASURED. The failure mode these tests exist for is
not a crash: it is a page that renders beautifully while showing a sample, a stale session,
or a fabricated distribution, and looks exactly like one that measured everything.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nse_algo_trader.dashboard.dashboard_server import build_dashboard_app
from nse_algo_trader.dashboard.order_book_replay_surface_renderer import (
    _DARK_SERIES,
    _LIGHT_SERIES,
    render_order_book_replay_page,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    InstrumentCoverageReport,
)

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


@pytest.mark.real_data
@pytest.mark.skipif(not LIVE_TAPE_ROOT.exists(), reason="no depth tape on this host")
def test_the_route_renders_the_real_tape() -> None:
    """R.05 — the route replays the actual recorded session, not a fixture."""
    from nse_algo_trader.dashboard.dashboard_server import ACCESS_TOKEN_PATH

    if not ACCESS_TOKEN_PATH.exists():
        pytest.skip("no dashboard access token on this host")
    token = ACCESS_TOKEN_PATH.read_text().strip()
    client = TestClient(build_dashboard_app())
    response = client.get("/microstructure", params={"key": token, "instrument_limit": 3})
    assert response.status_code == 200, response.text[:300]
    assert "Order-book snapshot replay" in response.text
    assert "3 instruments replayed" in response.text
    assert "feature rows" in response.text
