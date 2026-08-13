"""`R.08` — the `/orders` page, tested for the CLAIMS it makes rather than for its markup.

A surface test that asserts "the HTML contains a table" is worth almost nothing. Each test below
corresponds to a way this page could mislead an operator while looking perfectly healthy, and every
one of those ways is a real defect in shipped order-management code (`docs/research/224`):

- `test_an_absent_average_fill_price_renders_as_absent_and_never_as_zero` — the order record
  refuses to invent a cost basis; a surface that printed `0.00` would put the invention back where
  a human reads it and acts on it.
- `test_an_inferred_fill_renders_with_the_weaker_badge_and_the_word_inferred` — an inferred fill
  is this system's own reconstruction of a gap. Shown like an observed one, it becomes
  indistinguishable from evidence the moment the page is refreshed.
- `test_an_unestablished_horizon_explains_itself_rather_than_rendering_blank` — a blank horizon
  reads as zero, and a horizon of zero is the claim that any unseen order may be abandoned at once.
- `test_the_open_blocker_banner_is_on_the_page` — `F02` closes with its `R.05` real-fill pass
  explicitly unmet; a page that hid that would let the feature look finished.

The journal is REAL in every test — a live SQLite write-ahead log in `tmp_path`, folded back the
way production folds it. A hand-built `OrderRecord` would test the renderer against a shape the
journal may not actually produce.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

import pytest

from nse_algo_trader.dashboard.order_path_surface_renderer import (
    OrderPathSurfaceState,
    _average_fill_price_cell,
    _order_row,
    _reconciliation_section,
    build_order_path_surface_state,
    empty_order_path_surface_state,
    render_order_path_page,
)
from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.broker_truth_reconciler import (
    OrderReconciliation,
    ReconciliationReport,
    ReconciliationVerdict,
    visibility_horizon_from_observed_delays,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    LifecycleEvent,
    OrderLifecycleState,
)
from nse_algo_trader.order_path.order_record import FillRecord, OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = pytest.mark.unit

_DECIDED_AT = datetime(2026, 8, 13, 10, 15, 30, tzinfo=UTC)
_SESSION = date(2026, 8, 13)
_MEASURED_AT = datetime(2026, 8, 13, 11, 0, 0, tzinfo=UTC)


def _intent(*, symbol: str = "RELIANCE", quantity: int = 100) -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_DECIDED_AT,
        reference_price_paise=Decimal("142350"),
        expected_edge_bps=Decimal("34.1"),
    )


def _expression(*, chosen_because: str = "spread is one tick, so a limit at the touch") -> (
    OrderExpression
):
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal("142350"),
        chosen_because=chosen_because,
    )


def _journal_with_one_unfilled_intent(
    tmp_path: Path, *, chosen_because: str = "spread is one tick, so a limit at the touch"
) -> tuple[OrderIntentJournal, Path]:
    """One intent, submitted and acknowledged, and NOTHING filled — the absent-average case."""
    path = tmp_path / "order_path.sqlite3"
    journal = OrderIntentJournal(path)
    intent = _intent()
    journal.record_intent(
        intent, _expression(chosen_because=chosen_because), OrderNamespace.SIMULATED, at=_DECIDED_AT
    )
    journal.record_event(
        intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT
    )
    journal.record_event(
        intent.intent_id,
        LifecycleEvent.ACKNOWLEDGED,
        EventSource.BROKER_REPORTED,
        at=_DECIDED_AT,
        broker_order_id="250813000123456",
    )
    return journal, path


def _order_with_an_inferred_fill(tmp_path: Path) -> OrderRecord:
    """A quantity gap patched from the broker's own average — the reconciler's own shape."""
    path = tmp_path / "inferred.sqlite3"
    journal = OrderIntentJournal(path)
    intent = _intent()
    journal.record_intent(intent, _expression(), OrderNamespace.SIMULATED, at=_DECIDED_AT)
    journal.record_event(
        intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=_DECIDED_AT
    )
    journal.record_fill(
        intent.intent_id,
        FillRecord(
            broker_trade_id="inferred:250813000123456:100",
            quantity=100,
            price_paise=Decimal("142400"),
            occurred_at=_DECIDED_AT,
            source=EventSource.INFERRED,
            inferred_from=(
                "broker reports 100 filled against 0 locally, and no trade accounts for the "
                "difference; 100 units are INFERRED at the broker's own average of 142400"
            ),
        ),
    )
    order = journal.load_order(intent.intent_id)
    journal.close()
    assert order is not None
    return order


def _report(*reconciliations: OrderReconciliation) -> ReconciliationReport:
    return ReconciliationReport(
        session_date=_SESSION,
        ran_at=_MEASURED_AT,
        horizon=visibility_horizon_from_observed_delays(()),
        reconciliations=reconciliations,
    )


def _reconciliation(
    verdict: ReconciliationVerdict, *, detail: str = "local and broker agree"
) -> OrderReconciliation:
    return OrderReconciliation(
        verdict=verdict,
        intent_id="a1b2c3d4e5f6a7b8",
        broker_order_id="250813000123456",
        trading_symbol="RELIANCE",
        local_state=OrderLifecycleState.WORKING,
        broker_status="OPEN",
        local_filled_quantity=0,
        broker_filled_quantity=0,
        detail=detail,
    )


def _page_from(
    journal: OrderIntentJournal,
    path: Path,
    *,
    reconciliation: ReconciliationReport | None = None,
) -> str:
    state = build_order_path_surface_state(
        journal,
        measured_at=_MEASURED_AT,
        journal_path=path,
        reconciliation=reconciliation,
    )
    return render_order_path_page(state)


def test_the_page_renders_and_is_a_complete_document(tmp_path: Path) -> None:
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        page = _page_from(journal, path)
    finally:
        journal.close()
    assert page.startswith("<!doctype html>")
    assert "</html>" in page
    assert "Order path" in page
    # Measured, not typed: the intent written above must be the one on the page.
    assert "RELIANCE" in page
    assert "250813000123456" in page


def test_an_absent_average_fill_price_renders_as_absent_and_never_as_zero(
    tmp_path: Path,
) -> None:
    """`OrderRecord` returns `None` rather than a zero so no fabricated cost basis exists.

    The cell is asserted on its own rather than through the whole page, because the page is full
    of other numbers: the claim under test is that THIS cell contains no numeral at all.
    """
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        orders = journal.orders_for_session(_SESSION)
        page = _page_from(journal, path)
    finally:
        journal.close()
    (order,) = orders
    assert order.average_fill_price_paise is None

    cell = _average_fill_price_cell(order)
    assert "no fill" in cell
    assert "badge-absent" in cell
    assert not any(character.isdigit() for character in cell), (
        "an absent average must contain no figure at all — a zero would read as a price paid"
    )
    assert "no fill" in page


def test_an_inferred_fill_renders_with_the_weaker_badge_and_the_word_inferred(
    tmp_path: Path,
) -> None:
    """An invention must never be presentable as an observation.

    Two assertions, because either alone is defeatable: the WORD (a status shown in colour alone
    fails for a colourblind reader and in a printout) and the weaker STYLE (`badge-absent` — no
    fill, dashed edge — the same vocabulary `/costs` gives `UNPRICEABLE`).
    """
    order = _order_with_an_inferred_fill(tmp_path)
    assert order.has_inferred_events

    row = _order_row(order)
    assert "INFERRED" in row
    assert "badge-absent" in row
    assert 'class="inferred"' in row

    observed_journal, _observed_path = _journal_with_one_unfilled_intent(
        tmp_path / "observed"
    )
    try:
        (observed_order,) = observed_journal.orders_for_session(_SESSION)
    finally:
        observed_journal.close()
    assert not observed_order.has_inferred_events
    observed_row = _order_row(observed_order)
    assert "INFERRED" not in observed_row, "an observed order must not wear the inferred badge"
    assert _order_row(order) != observed_row


def test_the_evidence_an_inference_rests_on_is_printed_in_full(tmp_path: Path) -> None:
    """A badge says something was invented; only the evidence says why it was believable."""
    order = _order_with_an_inferred_fill(tmp_path)
    page = render_order_path_page(
        OrderPathSurfaceState(
            session_date=_SESSION,
            measured_at=_MEASURED_AT,
            orders=(order,),
            in_flight_submissions=(),
            horizon=visibility_horizon_from_observed_delays(()),
            reconciliation=None,
            journal_path=tmp_path / "inferred.sqlite3",
            journal_exists=True,
            recorded_session_dates=(_SESSION,),
        )
    )
    assert "no trade accounts for the difference" in page
    assert "at the broker&#x27;s own average of 142400" in page or (
        "own average of 142400" in page
    )


def test_an_unestablished_horizon_explains_itself_rather_than_rendering_blank(
    tmp_path: Path,
) -> None:
    """The refusal to abandon anything yet is a FACT the operator needs, not an empty cell."""
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        page = _page_from(journal, path)
    finally:
        journal.close()
    assert "NOT ESTABLISHED" in page
    assert "No order will be declared abandoned yet" in page
    # And the reason, in the estimator's own words rather than the page's.
    assert "observed appearance delays" in page


def test_an_established_horizon_shows_its_value_and_what_it_rests_on(tmp_path: Path) -> None:
    """Once measured, both halves must be visible: a horizon without its N cannot be judged."""
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        (order,) = journal.orders_for_session(_SESSION)
        for index in range(25):
            journal.record_visibility_delay(
                order.intent_id,
                submitted_at=_DECIDED_AT,
                first_seen_at=_DECIDED_AT.replace(second=31 + index % 20),
            )
        page = _page_from(journal, path)
    finally:
        journal.close()
    assert "measured visibility horizon" in page
    assert "appearance delays it rests on" in page
    assert "25" in page
    assert "NOT ESTABLISHED" not in page


def test_the_open_blocker_banner_is_on_the_page(tmp_path: Path) -> None:
    """`F02` is not allowed to look finished while its `R.05` real-fill pass is unmet."""
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        page = _page_from(journal, path)
    finally:
        journal.close()
    assert "OPEN BLOCKER" in page
    assert "deferred by operator" in page
    assert "BACKLOG.md" in page
    # Above the tables it qualifies, not in a footnote below them.
    assert page.index("OPEN BLOCKER") < page.index("Every intent of the session")


def test_the_verdict_counts_are_the_report_s_own_counts(tmp_path: Path) -> None:
    """Nothing on this page is a tally somebody typed: the counts come out of the report.

    Asserted against `counts_by_verdict()` rather than against a literal, so a report shaped
    differently tomorrow cannot leave a stale number on the page.
    """
    report = _report(
        _reconciliation(ReconciliationVerdict.AGREED),
        _reconciliation(ReconciliationVerdict.AGREED),
        _reconciliation(
            ReconciliationVerdict.STATE_CONFLICT_BROKER_WON,
            detail="the broker reports 'CANCELLED' for an order this system believes is working",
        ),
        _reconciliation(
            ReconciliationVerdict.QUANTITY_GAP_PATCHED,
            detail="broker reports 100 filled against 0 locally",
        ),
    )
    counts = report.counts_by_verdict()
    assert counts["agreed"] == 2

    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        page = _page_from(journal, path, reconciliation=report)
    finally:
        journal.close()

    section = _reconciliation_section(report)
    for verdict_value, count in counts.items():
        assert f'<span class="badge badge-good">{verdict_value}</span>' in section or (
            verdict_value in section
        )
        assert str(count) in section
    # Every disagreement is listed in full, with its own detail text — never summarised away.
    assert len(report.disagreements) == 2
    for disagreement in report.disagreements:
        # Escaped, verbatim: the reconciler's own words are what an operator acts on, and the
        # apostrophes in them are exactly what an unescaping page would let through as markup.
        assert escape(disagreement.detail) in page


def test_no_reconciliation_reads_as_unchecked_rather_than_as_zero_disagreements() -> None:
    """"I could not ask" and "there is nothing there" must not render alike."""
    section = _reconciliation_section(None)
    assert "No reconciliation report was supplied" in section
    assert "doubles positions" in section


def test_the_lifecycle_census_counts_every_state_including_the_empty_ones(
    tmp_path: Path,
) -> None:
    """A missing row reads as "not a problem"; an explicit zero is the honest statement."""
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        state = build_order_path_surface_state(
            journal, measured_at=_MEASURED_AT, journal_path=path
        )
    finally:
        journal.close()
    census = state.state_census()
    assert set(census) == set(OrderLifecycleState)
    assert census[OrderLifecycleState.ACKNOWLEDGED] == 1
    assert census[OrderLifecycleState.AMBIGUOUS] == 0
    page = render_order_path_page(state)
    for lifecycle_state in OrderLifecycleState:
        assert lifecycle_state.value in page


def test_an_in_flight_submission_with_no_outcome_is_surfaced(tmp_path: Path) -> None:
    """The crash-recovery queue: the row that tells "never sent" from "sent and I died"."""
    journal, path = _journal_with_one_unfilled_intent(tmp_path)
    try:
        (order,) = journal.orders_for_session(_SESSION)
        journal.record_submission_started(order.intent_id, at=_DECIDED_AT)
        state = build_order_path_surface_state(
            journal, measured_at=_MEASURED_AT, journal_path=path
        )
        page = render_order_path_page(state)
    finally:
        journal.close()
    assert len(state.in_flight_submissions) == 1
    assert "no outcome" in page
    assert order.broker_tag in page


def test_a_hostile_reason_string_cannot_inject_markup(tmp_path: Path) -> None:
    """`chosen_because`, a rejection message and a symbol all flow from outside onto this page.

    The selector's reason is written by this system, but the broker's status message is not, and
    both land in the same cells. One unescaped path is the whole vulnerability, so the assertion is
    made on the page as a whole rather than on one helper.
    """
    hostile = "<script>alert('order')</script>"
    journal, path = _journal_with_one_unfilled_intent(tmp_path, chosen_because=hostile)
    try:
        page = _page_from(
            journal,
            path,
            reconciliation=_report(
                _reconciliation(
                    ReconciliationVerdict.UNMAPPABLE,
                    detail=f"the broker reported status {hostile!r}",
                )
            ),
        )
    finally:
        journal.close()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert page.count("&lt;script&gt;") >= 2, "both the reason and the broker's words are escaped"


def test_a_journal_that_does_not_exist_says_so_rather_than_drawing_an_empty_table(
    tmp_path: Path,
) -> None:
    """An empty table looks like a quiet day; a missing journal is a different statement."""
    missing = tmp_path / "never_written.sqlite3"
    page = render_order_path_page(
        empty_order_path_surface_state(
            session_date=_SESSION, measured_at=_MEASURED_AT, journal_path=missing
        )
    )
    assert "has never been created" in page
    assert not missing.exists(), "rendering the page must not create the journal"
    # The blocker and the horizon refusal survive the empty case — they are not table decorations.
    assert "OPEN BLOCKER" in page
    assert "No order will be declared abandoned yet" in page
