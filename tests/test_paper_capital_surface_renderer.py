"""Tests for the `/paper-capital` surface and its one write control.

The route tests exist because `R.08` is about a human being able to SEE the state, and a renderer
that is correct in isolation while the route hands it the wrong thing is invisible in exactly the
way the rule exists to prevent. They drive the real FastAPI app through its real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.dashboard import dashboard_server
from nse_algo_trader.dashboard.paper_capital_surface_renderer import (
    _LEDGER_COLUMNS,
    absent_paper_capital_surface_state,
    build_paper_capital_surface_state,
    render_paper_capital_page,
)
from nse_algo_trader.paper_capital_ledger import PaperCapitalLedger

IST = ZoneInfo("Asia/Kolkata")
TEN_LAKH = Decimal("1000000")
CEILING = TradingCapital.of_rupees(TEN_LAKH)


def _at(minute: int) -> datetime:
    return datetime(2026, 8, 13, 9, 15, tzinfo=IST) + timedelta(minutes=minute)


def _page(path: Path, *, limit: int = 60) -> str:
    with PaperCapitalLedger(path) as ledger:
        state = build_paper_capital_surface_state(
            ledger,
            measured_at=_at(30),
            live_ceiling_rupees=TEN_LAKH,
            ledger_path=path,
            recent_event_limit=limit,
        )
    return render_paper_capital_page(state)


@pytest.fixture(name="ledger_path")
def _ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "paper_capital_ledger.sqlite3"
    with PaperCapitalLedger(path) as ledger:
        ledger.seed_from_ceiling(CEILING, occurred_at=_at(0), reason="fixture seed")
    return path


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_the_page_states_why_a_real_debit_does_not_stop_the_paper_book(ledger_path: Path) -> None:
    """The operator's actual question, answered on the surface rather than in a chat log."""
    page = _page(ledger_path)
    assert "This money is not the broker's money" in page
    assert "deployable_capital_resolver" in page
    assert "cannot stop" in page


@pytest.mark.unit
def test_the_page_carries_the_edit_control_the_operator_asked_for(ledger_path: Path) -> None:
    page = _page(ledger_path)
    assert 'method="post" action="/paper-capital"' in page
    assert 'name="balance_rupees"' in page
    assert 'name="reason"' in page


@pytest.mark.unit
def test_a_book_above_the_live_ceiling_is_stamped_on_the_page(ledger_path: Path) -> None:
    with PaperCapitalLedger(ledger_path) as ledger:
        ledger.set_balance(Decimal("10000000"), occurred_at=_at(1), reason="crore regime")
    page = _page(ledger_path)
    assert "exceeds the live ceiling" in page
    assert "not achievable" in page


@pytest.mark.unit
def test_an_over_committed_book_is_stamped_on_the_page(ledger_path: Path) -> None:
    with PaperCapitalLedger(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("800000"), position_key="RELIANCE-1", occurred_at=_at(1), reason="entry"
        )
        ledger.set_balance(Decimal("500000"), occurred_at=_at(2), reason="shrink")
    page = _page(ledger_path)
    assert "over-committed" in page
    assert "RELIANCE-1" in page


@pytest.mark.unit
def test_the_absent_state_states_the_reason_and_never_renders_a_zero_balance() -> None:
    page = render_paper_capital_page(
        absent_paper_capital_surface_state(
            measured_at=_at(0),
            ledger_path=Path("/nowhere/paper_capital_ledger.sqlite3"),
            live_ceiling_rupees=None,
            unavailable_reason="the paper capital ledger has never been created.",
        )
    )
    assert "not seeded" in page
    assert "never been created" in page
    assert "Rs 0.00" not in page


@pytest.mark.unit
def test_the_reason_text_is_escaped_rather_than_reflected(ledger_path: Path) -> None:
    with PaperCapitalLedger(ledger_path) as ledger:
        ledger.set_balance(
            Decimal("900000"), occurred_at=_at(1), reason="<script>alert(1)</script>"
        )
    page = _page(ledger_path)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_bounded_event_view_says_what_it_dropped(ledger_path: Path) -> None:
    """`R.11` — a truncated table that does not say so reads as complete when it is not."""
    with PaperCapitalLedger(ledger_path) as ledger:
        for minute in range(1, 6):
            ledger.record_cost_debit(
                Decimal("10"), position_key=f"C{minute}", occurred_at=_at(minute), reason="charges"
            )
    page = _page(ledger_path, limit=2)
    assert "Showing the most recent 2 of 6 events" in page
    assert "folded from ALL of them" in page


@pytest.mark.adversarial
def test_a_get_on_the_route_never_creates_the_ledger_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashboard that writes a database on page load is a side effect nobody asked for."""
    absent = tmp_path / "never_created.sqlite3"
    monkeypatch.setattr(dashboard_server, "DEFAULT_PAPER_CAPITAL_LEDGER_PATH", absent)
    monkeypatch.setattr(dashboard_server, "read_access_token", lambda: None)
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", "1000000")
    with TestClient(dashboard_server.build_dashboard_app()) as client:
        response = client.get("/paper-capital")
    assert response.status_code == 200
    assert "never been created" in response.text
    assert not absent.exists()


@pytest.mark.adversarial
def test_the_post_seeds_then_applies_the_operator_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_file = tmp_path / "created_by_post.sqlite3"
    monkeypatch.setattr(dashboard_server, "DEFAULT_PAPER_CAPITAL_LEDGER_PATH", ledger_file)
    monkeypatch.setattr(dashboard_server, "read_access_token", lambda: None)
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", "1000000")
    with TestClient(dashboard_server.build_dashboard_app()) as client:
        posted = client.post(
            "/paper-capital",
            data={"balance_rupees": "2,50,000", "reason": "operator wants a smaller book"},
            follow_redirects=True,
        )
    assert posted.status_code == 200
    assert "Rs 250,000.00" in posted.text
    with PaperCapitalLedger(ledger_file) as ledger:
        kinds = [event.kind.value for event in ledger.events()]
    assert kinds == ["SEED", "OPERATOR_SET"]


@pytest.mark.adversarial
def test_a_refused_edit_is_rendered_as_a_refusal_and_not_as_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirect on failure would be indistinguishable from a redirect on success."""
    ledger_file = tmp_path / "refusal.sqlite3"
    monkeypatch.setattr(dashboard_server, "DEFAULT_PAPER_CAPITAL_LEDGER_PATH", ledger_file)
    monkeypatch.setattr(dashboard_server, "read_access_token", lambda: None)
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", "1000000")
    with TestClient(dashboard_server.build_dashboard_app()) as client:
        rubbish = client.post(
            "/paper-capital", data={"balance_rupees": "lots", "reason": "typo"}
        )
        negative = client.post(
            "/paper-capital", data={"balance_rupees": "-5", "reason": "negative"}
        )
    assert rubbish.status_code == 400
    assert "is not a number this ledger will interpret" in rubbish.text
    assert negative.status_code == 400
    assert "must be positive" in negative.text


@pytest.mark.adversarial
def test_every_ledger_row_carries_one_cell_per_column(ledger_path: Path) -> None:
    """The defect the screenshot found and the tests did not.

    The row was a chain of implicitly-concatenated f-strings with a conditional in the middle; a
    line-wrap turned that conditional into a ternary over the WHOLE chain, so every event with no
    unfunded shortfall — which is every ordinary event — rendered TWO cells instead of nine. Each
    assertion of the form "RELIANCE-1 appears on the page" still passed, because a two-cell row
    still contains it. Presence is not structure, and this test counts.
    """
    with PaperCapitalLedger(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("100000"), position_key="RELIANCE-1", occurred_at=_at(1), reason="entry"
        )
        ledger.record_realised_loss(
            Decimal("2000000"), position_key="TCS-1", occurred_at=_at(2), reason="blown"
        )
    page = _page(ledger_path)
    body = page.split("<tbody>")[-1].split("</tbody>")[0]
    rows = [row for row in body.split("<tr>") if row.strip()]
    assert len(rows) == 3
    for row in rows:
        assert row.count("<td") == len(_LEDGER_COLUMNS)
    ledger_head = page.split("<tbody>")[-2].split("<thead>")[-1]
    assert ledger_head.count("<th>") == len(_LEDGER_COLUMNS)
    assert "Rs 1,000,000.00" in page


@pytest.mark.adversarial
def test_the_surface_is_claimed_in_the_manifest_so_the_wall_cannot_call_it_unsurfaced() -> None:
    """`R.08`/`L13.06` — a panel that exists but is not claimed reads as UNSURFACED forever."""
    assert "nse_algo_trader.paper_capital_ledger" in dashboard_server.SURFACED_MODULES
    assert (
        "nse_algo_trader.dashboard.paper_capital_surface_renderer"
        in dashboard_server.SURFACED_MODULES
    )
