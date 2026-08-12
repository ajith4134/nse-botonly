"""`L1.04` — the edge below which a whole segment is not worth looking at.

The floor is a SCREENING bound, and the tests are written to keep it one. Clearing it means a
trade is conceivable somewhere in the segment; it never means this trade is worth taking, and
`test_the_floor_screens_it_does_not_approve` exists so nobody later reads it that way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.per_segment_edge_floor import (
    EdgeFloorError,
    SegmentEdgeFloorStore,
    derive_segment_floor,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment

SESSION = date(2026, 8, 11)
A_SEGMENT = ChargeableSegment.EQUITY_INTRADAY


def hurdles(*values: str) -> list[Decimal]:
    return [Decimal(value) for value in values]


@pytest.mark.unit
def test_the_floor_sits_at_the_cheap_end_of_the_measured_distribution() -> None:
    """The floor answers "can ANYTHING here clear this", so it comes from the cheap end."""
    floor = derive_segment_floor(
        A_SEGMENT,
        hurdles("5", "8", "10", "12", "15", "20", "40", "100"),
        session_date=SESSION,
        floor_quantile=Decimal("0.25"),
    )
    assert floor.cheapest_hurdle_bps == Decimal(5)
    assert floor.dearest_hurdle_bps == Decimal(100)
    assert floor.floor_bps < floor.median_hurdle_bps
    assert floor.instrument_count == 8


@pytest.mark.unit
def test_the_floor_screens_it_does_not_approve() -> None:
    """The distinction the whole module rests on.

    Clearing the floor says a trade is conceivable somewhere in the segment. Only the gate
    knows the instrument, the size and the book, so only the gate can approve. Reading the
    floor as an approval is the false-precision failure the gate exists to prevent.
    """
    floor = derive_segment_floor(
        A_SEGMENT, hurdles("10", "12", "14", "16", "18"), session_date=SESSION
    )
    assert floor.clears(floor.floor_bps + Decimal(1))
    assert not floor.clears(floor.floor_bps)
    assert not hasattr(floor, "approves")


@pytest.mark.property
def test_a_lower_quantile_gives_a_lower_floor() -> None:
    """The quantile is a robustness knob, not a policy one, and behaves monotonically."""
    measured = hurdles("5", "7", "9", "11", "13", "20", "35", "60", "90", "200")
    floors = [
        derive_segment_floor(
            A_SEGMENT, measured, session_date=SESSION, floor_quantile=Decimal(quantile)
        ).floor_bps
        for quantile in ("0.1", "0.25", "0.5", "0.9")
    ]
    assert floors == sorted(floors)
    assert floors[0] <= Decimal(7)


@pytest.mark.property
def test_the_floor_never_exceeds_the_median_it_came_from() -> None:
    measured = hurdles("4", "6", "8", "10", "12", "14", "16", "18", "20", "500")
    floor = derive_segment_floor(A_SEGMENT, measured, session_date=SESSION)
    assert floor.cheapest_hurdle_bps <= floor.floor_bps <= floor.median_hurdle_bps
    assert floor.spread_of_hurdles_bps > 0


@pytest.mark.adversarial
def test_a_floor_from_too_few_instruments_is_refused() -> None:
    """Three instruments is not a property of a segment.

    Publishing it as one would let a screening bound built from noise silently reject real
    trades — a failure nobody would ever see, because a rejected signal leaves no trace.
    """
    with pytest.raises(EdgeFloorError, match="below the"):
        derive_segment_floor(A_SEGMENT, hurdles("10", "12", "14"), session_date=SESSION)


@pytest.mark.adversarial
def test_non_positive_hurdles_are_discarded_rather_than_dragging_the_floor_down() -> None:
    """A zero hurdle is an unpriced instrument, and it would pull the floor to nothing."""
    floor = derive_segment_floor(
        A_SEGMENT,
        [Decimal(0), Decimal(-5), *hurdles("10", "12", "14", "16", "18")],
        session_date=SESSION,
    )
    assert floor.instrument_count == 5
    assert floor.cheapest_hurdle_bps == Decimal(10)


@pytest.mark.adversarial
def test_an_impossible_quantile_is_refused() -> None:
    for quantile in (Decimal(0), Decimal("1.5"), Decimal(-1)):
        with pytest.raises(EdgeFloorError, match="quantile"):
            derive_segment_floor(
                A_SEGMENT,
                hurdles("10", "12", "14", "16", "18"),
                session_date=SESSION,
                floor_quantile=quantile,
            )


@pytest.mark.unit
def test_a_floor_round_trips_and_is_point_in_time(tmp_path: Path) -> None:
    """A replay must screen with the floor that was true then, not one derived later."""
    store = SegmentEdgeFloorStore(tmp_path / "floors.sqlite3")
    early = derive_segment_floor(
        A_SEGMENT, hurdles("10", "12", "14", "16", "18"), session_date=date(2026, 8, 10)
    )
    late = derive_segment_floor(
        A_SEGMENT, hurdles("40", "42", "44", "46", "48"), session_date=date(2026, 8, 11)
    )
    store.record([early, late])

    assert store.floor_for(A_SEGMENT).floor_bps == late.floor_bps
    assert store.floor_for(A_SEGMENT, as_of=date(2026, 8, 10)).floor_bps == early.floor_bps
    assert store.measured_segments() == (A_SEGMENT,)


@pytest.mark.adversarial
def test_an_unmeasured_segment_is_refused_not_waved_through(tmp_path: Path) -> None:
    """A missing floor must never become an open door."""
    store = SegmentEdgeFloorStore(tmp_path / "floors.sqlite3")
    with pytest.raises(EdgeFloorError, match="must not be screened in by default"):
        store.floor_for(ChargeableSegment.EQUITY_OPTIONS)


@pytest.mark.unit
def test_the_floor_carries_the_evidence_behind_it(tmp_path: Path) -> None:
    """`R.04`: a floor from one quiet session is a number and should read like one."""
    floor = derive_segment_floor(
        A_SEGMENT, hurdles("10", "12", "14", "16", "18"), session_date=SESSION
    )
    described = floor.describe()
    assert "5 instruments" in described
    assert str(SESSION) in described
    assert "floor" in described
