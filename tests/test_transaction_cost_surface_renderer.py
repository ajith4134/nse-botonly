"""`R.08` — the `/costs` page, tested for the claims it makes rather than for its markup.

A surface test that asserts "the HTML contains a table" is worth almost nothing. These assert
the things the page exists to communicate, and each corresponds to a way the page could mislead
an operator while looking perfectly healthy:

- `test_unpriceable_is_never_presented_as_a_rejection` — a veto is a judgement, unpriceable is
  the absence of one. A page that colours them alike teaches its reader to conflate them, and
  the reader then treats a data outage as a stream of confident rejections.
- `test_the_uncertainty_component_is_shown_separately` — this gate has no safety multiplier; the
  margin IS the measured uncertainty. If the page hides that, the single most important property
  of the design is invisible.
- `test_a_floor_from_few_instruments_reads_as_weaker_evidence` — `R.04` made visible.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import ReversionCapture
from nse_algo_trader.cost_gate.per_segment_edge_floor import SegmentEdgeFloor, derive_segment_floor
from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
    PreconditionFailureRow,
    SegmentEdgeFloorRow,
    build_transaction_cost_surface_state,
    render_transaction_cost_page,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment

SESSION = date(2026, 8, 11)


def floor_with(instrument_count: int, *, segment: ChargeableSegment) -> SegmentEdgeFloor:
    hurdles = [Decimal(10 + index) for index in range(instrument_count)]
    return derive_segment_floor(segment, hurdles, session_date=SESSION)


@pytest.fixture(scope="module")
def rendered_page() -> str:
    """One real render, built from real engines, reused across the assertions."""
    from nse_algo_trader.dashboard.dashboard_server import (
        TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
        TRANSACTION_COST_DISPLAY_CEILING_BPS,
    )
    from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
    from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
        ChargeReconciliationLedger,
    )
    from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
        NseTransactionCostEngine,
    )

    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    state = build_transaction_cost_surface_state(
        NseTransactionCostEngine(store),
        ChargeReconciliationLedger(),
        TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
        priced_on=date(2026, 8, 12),
        cost_bps_ceiling=TRANSACTION_COST_DISPLAY_CEILING_BPS,
        rule_store=store,
        edge_floors=[
            floor_with(200, segment=ChargeableSegment.EQUITY_INTRADAY),
            floor_with(6, segment=ChargeableSegment.EQUITY_DELIVERY),
        ],
    )
    return render_transaction_cost_page(state)


@pytest.mark.unit
def test_the_page_renders_and_is_a_complete_document(rendered_page: str) -> None:
    assert rendered_page.startswith("<!doctype html>")
    assert "</html>" in rendered_page
    assert "Transaction costs" in rendered_page


@pytest.mark.unit
def test_the_uncertainty_component_is_shown_separately(rendered_page: str) -> None:
    """The design's most important property must be visible, not merely true.

    There is no 1.5x multiplier anywhere in this gate: the safety margin IS the measured
    execution uncertainty. A page that folds it into one cost number hides the reason the
    hurdle moves with size and liquidity.
    """
    assert "uncertain" in rendered_page.lower()


@pytest.mark.unit
def test_unpriceable_is_never_counted_as_a_rejection() -> None:
    """A veto is a judgement; unpriceable is the absence of one.

    Asserted on the TALLY rather than on the prose, because the tally is what a reader acts on:
    summing the two would report a data outage as a wall of confident rejections. A real
    unpriceable is produced here from a crossed book rather than constructed by hand, so the
    path that generates it is the same one production uses.
    """
    from nse_algo_trader.cost_gate.pre_trade_cost_gate import GateVerdict, PreTradeCostGate
    from nse_algo_trader.dashboard.dashboard_server import (
        TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
        TRANSACTION_COST_DISPLAY_CEILING_BPS,
    )
    from nse_algo_trader.execution_fill.execution_fill_model import ExecutionFillModel
    from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
    from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
        ChargeReconciliationLedger,
    )
    from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
        NseTransactionCostEngine,
    )
    from tests.test_pre_trade_cost_gate import book, signal

    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    gate = PreTradeCostGate(NseTransactionCostEngine(store), ExecutionFillModel())
    crossed = book(bids=((140_200, 100),), asks=((140_100, 100),))
    unpriceable = gate.evaluate(signal(), crossed, trade_date=date(2026, 8, 12))
    vetoed = gate.evaluate(
        signal(expected_edge_bps=Decimal(1)), book(), trade_date=date(2026, 8, 12)
    )
    assert unpriceable.verdict is GateVerdict.UNPRICEABLE
    assert vetoed.verdict is GateVerdict.VETO

    state = build_transaction_cost_surface_state(
        NseTransactionCostEngine(store),
        ChargeReconciliationLedger(),
        TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
        priced_on=date(2026, 8, 12),
        cost_bps_ceiling=TRANSACTION_COST_DISPLAY_CEILING_BPS,
        rule_store=store,
        gate_decisions=[unpriceable, vetoed],
    )
    census = state.verdict_census
    assert census is not None
    assert census.unpriceable_count == 1
    assert census.veto_count == 1
    # The load-bearing assertion: the count a reader acts on excludes the unpriceable, so a
    # broken feed cannot inflate a "rejections" figure.
    assert census.judged_count == 1
    assert census.evaluated_count == 2
    assert census.tradeable_count == 0
    assert "unpriceable" in render_transaction_cost_page(state).lower()


@pytest.mark.unit
def test_the_derived_floors_appear_with_the_distribution_behind_them(
    rendered_page: str,
) -> None:
    """A floor without its spread is a number nobody can judge."""
    assert "floor" in rendered_page.lower()
    for word in ("median", "cheapest"):
        assert word in rendered_page.lower()


@pytest.mark.property
def test_a_floor_from_few_instruments_reads_as_weaker_evidence() -> None:
    """`R.04` made visible: a floor from six instruments must not look like one from two hundred."""
    from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
        FloorEvidenceVerdict,
        _edge_floor_row,
    )

    strong = floor_with(200, segment=ChargeableSegment.EQUITY_INTRADAY)
    weak = floor_with(6, segment=ChargeableSegment.EQUITY_DELIVERY)
    assert strong.instrument_count > weak.instrument_count

    rows = [
        SegmentEdgeFloorRow(
            segment_value=floor.segment.value,
            session_date=floor.session_date,
            floor_bps=floor.floor_bps,
            median_hurdle_bps=floor.median_hurdle_bps,
            cheapest_hurdle_bps=floor.cheapest_hurdle_bps,
            dearest_hurdle_bps=floor.dearest_hurdle_bps,
            floor_quantile=floor.floor_quantile,
            instrument_count=floor.instrument_count,
            supporting_instrument_rank=index,
            floor_uncertainty_bps=floor.spread_of_hurdles_bps,
            evidence_verdict=(
                FloorEvidenceVerdict.RESOLVED
                if floor.instrument_count > 50
                else FloorEvidenceVerdict.SINGLE_OBSERVATION
            ),
            description=floor.describe(),
        )
        for index, floor in enumerate((strong, weak))
    ]
    strong_html, weak_html = (_edge_floor_row(row) for row in rows)
    assert strong_html != weak_html
    # The weak floor must carry a WORD, not only a colour — a status shown in colour alone
    # fails for a colourblind reader and in a printout.
    assert "SINGLE OBSERVATION" in weak_html
    assert "badge-critical" in weak_html
    assert "badge-good" in strong_html


@pytest.mark.unit
def test_precondition_failures_are_named_not_lumped_into_cost() -> None:
    """An operator must see WHICH economic fact killed the trade.

    "Vetoed on cost" is not actionable; "the flat charge is 6% of this ticket" is.
    """
    from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
        _precondition_failure_table_row,
    )

    row = PreconditionFailureRow(
        precondition_name="minimum_ticket",
        failure_count=7,
        evaluated_count=10,
        example_failure_reason="the flat charges alone are 615.0 bps of a 650-rupee ticket",
    )
    html = _precondition_failure_table_row(row)
    assert "minimum_ticket" in html
    assert "650-rupee ticket" in html


@pytest.mark.adversarial
def test_a_hostile_reason_string_cannot_inject_markup() -> None:
    """Reason strings come from engines and flow straight onto the page."""
    from nse_algo_trader.dashboard.transaction_cost_surface_renderer import (
        _precondition_failure_table_row,
    )

    html = _precondition_failure_table_row(
        PreconditionFailureRow(
            precondition_name="live_spread",
            failure_count=1,
            evaluated_count=1,
            example_failure_reason="<script>alert('x')</script>",
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.unit
def test_the_page_still_shows_what_it_cannot_price(rendered_page: str) -> None:
    """The `L1.01` panel must survive the extension.

    An absence that is invisible reads as coverage, so the refusal panel is not optional.
    """
    assert "CANNOT price" in rendered_page or "cannot price" in rendered_page.lower()
    assert "2024-10-01" in rendered_page


# ------------------------------------------------------- the measured-edge panel (`L1.16`)


def calibration_of(
    mean_bps: str, *, bucket: str, standard_error: str = "5", median: str = ""
) -> ReversionCapture:
    """A calibration row shaped like the ones the real fit produced."""
    from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import CalibrationMaturity

    return ReversionCapture(
        deviation_bucket=Decimal(bucket),
        horizon_bars=5,
        event_count=10_814,
        mean_captured_bps=Decimal(mean_bps),
        median_captured_bps=Decimal(median or mean_bps),
        standard_error_bps=Decimal(standard_error),
        mean_captured_sigma=Decimal("0.06"),
        fitted_through=date(2026, 7, 1),
        maturity=CalibrationMaturity.DEVIATION_BUCKET,
    )


def page_with_calibrations(calibrations: Sequence[ReversionCapture]) -> str:
    from nse_algo_trader.dashboard.dashboard_server import (
        TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
        TRANSACTION_COST_DISPLAY_CEILING_BPS,
    )
    from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
    from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
        ChargeReconciliationLedger,
    )
    from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
        NseTransactionCostEngine,
    )

    store = seeded_nse_market_rule_store(observe_instrument_master=False)
    return render_transaction_cost_page(
        build_transaction_cost_surface_state(
            NseTransactionCostEngine(store),
            ChargeReconciliationLedger(),
            TRANSACTION_COST_DISPLAY_ASSUMPTIONS,
            priced_on=date(2026, 8, 12),
            cost_bps_ceiling=TRANSACTION_COST_DISPLAY_CEILING_BPS,
            rule_store=store,
            calibrations=calibrations,
        )
    )


@pytest.mark.unit
def test_a_cell_that_continues_rather_than_reverts_is_shown_not_suppressed() -> None:
    """The finding a reader is least likely to go looking for must be impossible to miss.

    3.5-sigma deviations measurably CONTINUE at short horizons. A page that showed only the
    positive cells would leave a reader believing the edge grows with depth — which is exactly
    the assumption the replaced edge formula encoded, arriving back through the front end.
    """
    html = page_with_calibrations(
        [calibration_of("24.68", bucket="3"), calibration_of("-17.51", bucket="3.5")]
    )
    assert "CONTINUES" in html
    assert "-17.51" in html
    assert "1 continue rather than revert" in html


@pytest.mark.unit
def test_a_cell_whose_lower_bound_spans_zero_never_reads_as_a_confirmed_edge() -> None:
    """"Cannot tell" and "can tell" must not render alike.

    Asserted on the HEADLINE as well as the row, because the headline is what a reader acts on:
    the largest mean in the real fit belongs to a cell whose interval spans zero, and promoting
    it would advertise an edge the evidence does not support.
    """
    html = page_with_calibrations(
        [
            # The real five-bar fit: 2.0 sigma has the LARGEST mean and a lower bound of
            # -12.99, so its interval spans zero (t = 1.79). 3.0 sigma is smaller and credible.
            calibration_of("110.48", bucket="2", standard_error="61.7", median="18"),
            calibration_of("24.68", bucket="3", standard_error="5", median="8"),
        ]
    )
    assert "NOT SIGNIFICANT" in html
    # 24.68 is the smaller mean but the only credible one, so it must be the cell headlined.
    assert "Strongest credible cell: 3 sigma" in html
    assert "110.48" in html, "the weak cell is still SHOWN, just not promoted"


@pytest.mark.unit
def test_a_tail_carried_edge_is_labelled_as_one() -> None:
    """A mean far above its median needs many more trades to realise than the headline implies."""
    html = page_with_calibrations(
        [calibration_of("37.0", bucket="2.5", standard_error="5", median="5.5")]
    )
    assert "TAIL-CARRIED" in html
    assert "6.7x" in html


@pytest.mark.unit
def test_no_calibration_reads_as_a_refusal_not_as_an_absence_of_edge() -> None:
    """A blank panel would say "no edge"; the truth is "nothing has been measured"."""
    html = page_with_calibrations([])
    assert "No reversion calibration was supplied" in html
    assert "would have to be assumed" in html


@pytest.mark.adversarial
def test_every_cell_failing_significance_is_stated_rather_than_left_to_the_reader() -> None:
    """If nothing is credible the page must say so outright, not present a table to interpret."""
    html = page_with_calibrations(
        [calibration_of("10", bucket="3", standard_error="40", median="3")]
    )
    assert "no measurable edge anywhere on this fit" in html
