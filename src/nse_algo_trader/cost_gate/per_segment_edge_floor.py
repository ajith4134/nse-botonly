"""`L1.04` — the edge below which a whole segment is not worth looking at.

The gate answers "is THIS trade worth taking". This answers the cheaper question that should be
asked first: **is any trade in this segment worth taking at this edge?** A signal claiming 3 bps
in a segment whose cheapest achievable hurdle is 12 has nothing to discuss, and finding that out
costs one comparison instead of a full costing run per instrument.

**The plan's numbers are illustrations, not inputs.** `L1.04` names roughly 6-8 bps for large
cash, 10-11 for small cash and 25-30 for options, and immediately says "derived from data, never
hardcoded". So none of those figures appear in this module. The floor is computed by pricing the
REAL universe and taking a quantile of the resulting hurdle distribution — which means it moves
when the market moves, when the statutory rates change, and when liquidity changes, without
anyone editing a number.

**What the floor MEANS, precisely.** It is the hurdle at the cheap end of the segment: the level
below which essentially no instrument clears. That makes it a screening bound, not a target —
clearing the floor says a trade is *conceivable* somewhere in the segment, never that this
particular trade is worth taking. Only the gate can say that, because only the gate knows the
instrument, the size and the book. Using the floor as an approval would be exactly the
false-precision failure the gate exists to prevent.

**Maturity is visible (`R.04`).** Every floor carries how many instruments it was derived from
and on which session. A floor from eleven instruments on one quiet Tuesday is a number, and it
should be treated as one; the algorithm is identical, and only the confidence differs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment

DEFAULT_EDGE_FLOOR_PATH = Path("~/.nse_algo_trader/cost_gate.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS segment_edge_floor (
    segment TEXT NOT NULL,
    session_date TEXT NOT NULL,
    floor_bps TEXT NOT NULL,
    median_hurdle_bps TEXT NOT NULL,
    cheapest_hurdle_bps TEXT NOT NULL,
    dearest_hurdle_bps TEXT NOT NULL,
    floor_quantile TEXT NOT NULL,
    instrument_count INTEGER NOT NULL,
    PRIMARY KEY (segment, session_date)
);
"""


class EdgeFloorError(Exception):
    """The floor cannot be derived, and a made-up one would screen out real trades."""


@dataclass(frozen=True, slots=True)
class SegmentEdgeFloor:
    """One segment's screening bound, and the distribution it came from."""

    segment: ChargeableSegment
    session_date: date
    floor_bps: Decimal
    median_hurdle_bps: Decimal
    cheapest_hurdle_bps: Decimal
    dearest_hurdle_bps: Decimal
    floor_quantile: Decimal
    instrument_count: int

    def clears(self, edge_bps: Decimal) -> bool:
        """Whether an edge is worth carrying into a full costing run.

        Deliberately NOT named `approves`. Clearing the floor means a trade is conceivable
        somewhere in this segment; it says nothing about this instrument at this size, which
        only the gate can judge.
        """
        return edge_bps > self.floor_bps

    @property
    def spread_of_hurdles_bps(self) -> Decimal:
        """How much the segment varies. A wide spread means the floor screens weakly."""
        return self.dearest_hurdle_bps - self.cheapest_hurdle_bps

    def describe(self) -> str:
        return (
            f"{self.segment.value}: floor {self.floor_bps:.1f} bps "
            f"(cheapest {self.cheapest_hurdle_bps:.1f}, median {self.median_hurdle_bps:.1f}, "
            f"dearest {self.dearest_hurdle_bps:.1f}) from {self.instrument_count} instruments "
            f"on {self.session_date}"
        )


def _quantile(sorted_values: Sequence[Decimal], quantile: Decimal) -> Decimal:
    """Nearest-rank. No interpolation between two hurdles that were actually measured."""
    if not sorted_values:
        raise EdgeFloorError("no hurdles to take a quantile of")
    if not 0 < quantile <= 1:
        raise EdgeFloorError(f"quantile must be in (0, 1], got {quantile}")
    rank = int((Decimal(len(sorted_values)) * quantile).to_integral_value(rounding="ROUND_CEILING"))
    return sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))]


def derive_segment_floor(
    segment: ChargeableSegment,
    hurdles_bps: Iterable[Decimal],
    *,
    session_date: date,
    floor_quantile: Decimal = Decimal("0.05"),
    minimum_instruments: int = 5,
) -> SegmentEdgeFloor:
    """Derive one segment's floor from real measured hurdles.

    `floor_quantile` is a robustness choice, not a policy one: the true "nothing clears below
    this" line is the minimum, and a low quantile is the same statement made resistant to one
    freak instrument. It is a parameter rather than a constant so a caller can ask for the
    strict minimum by passing a quantile small enough to select it.

    Raises:
        EdgeFloorError: fewer than `minimum_instruments` hurdles. A floor derived from three
            instruments is not a property of a segment, and publishing it as one would let a
            screening bound built from noise silently reject real trades.
    """
    ordered = sorted(hurdle for hurdle in hurdles_bps if hurdle > 0)
    if len(ordered) < minimum_instruments:
        raise EdgeFloorError(
            f"{segment.value} has only {len(ordered)} priced instruments, below the "
            f"{minimum_instruments} needed to call a quantile a property of the segment"
        )
    return SegmentEdgeFloor(
        segment=segment,
        session_date=session_date,
        floor_bps=_quantile(ordered, floor_quantile),
        median_hurdle_bps=_quantile(ordered, Decimal("0.5")),
        cheapest_hurdle_bps=ordered[0],
        dearest_hurdle_bps=ordered[-1],
        floor_quantile=floor_quantile,
        instrument_count=len(ordered),
    )


class SegmentEdgeFloorStore:
    """Carries derived floors across sessions, point-in-time.

    Point-in-time for the same reason every other store here is: a replay of an old session
    must screen with the floor that was true then. A floor derived after a statutory rate change
    would silently admit trades that the earlier market would have rejected.
    """

    def __init__(self, database_path: Path = DEFAULT_EDGE_FLOOR_PATH) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def record(self, floors: Iterable[SegmentEdgeFloor]) -> int:
        rows = [
            (
                floor.segment.value,
                floor.session_date.isoformat(),
                str(floor.floor_bps),
                str(floor.median_hurdle_bps),
                str(floor.cheapest_hurdle_bps),
                str(floor.dearest_hurdle_bps),
                str(floor.floor_quantile),
                floor.instrument_count,
            )
            for floor in floors
        ]
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO segment_edge_floor (segment, session_date, floor_bps, "
                "median_hurdle_bps, cheapest_hurdle_bps, dearest_hurdle_bps, floor_quantile, "
                "instrument_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return len(rows)

    def floor_for(
        self, segment: ChargeableSegment, *, as_of: date | None = None
    ) -> SegmentEdgeFloor:
        """The most recent floor at or before `as_of`, or a refusal.

        Refuses rather than returning a permissive default: a missing floor must not become an
        open door. A segment nobody has measured is a segment nobody should be screening in.
        """
        latest = (
            "SELECT session_date, floor_bps, median_hurdle_bps, cheapest_hurdle_bps, "
            "dearest_hurdle_bps, floor_quantile, instrument_count FROM segment_edge_floor "
            "WHERE segment = ? ORDER BY session_date DESC LIMIT 1"
        )
        latest_as_of = (
            "SELECT session_date, floor_bps, median_hurdle_bps, cheapest_hurdle_bps, "
            "dearest_hurdle_bps, floor_quantile, instrument_count FROM segment_edge_floor "
            "WHERE segment = ? AND session_date <= ? ORDER BY session_date DESC LIMIT 1"
        )
        parameters: list[object] = [segment.value]
        if as_of is None:
            query = latest
        else:
            query = latest_as_of
            parameters.append(as_of.isoformat())
        with closing(self._connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise EdgeFloorError(
                f"no floor derived for {segment.value}"
                + (f" at or before {as_of}" if as_of else "")
                + " — a segment nobody has measured must not be screened in by default"
            )
        return SegmentEdgeFloor(
            segment=segment,
            session_date=date.fromisoformat(row[0]),
            floor_bps=Decimal(row[1]),
            median_hurdle_bps=Decimal(row[2]),
            cheapest_hurdle_bps=Decimal(row[3]),
            dearest_hurdle_bps=Decimal(row[4]),
            floor_quantile=Decimal(row[5]),
            instrument_count=int(row[6]),
        )

    def measured_segments(self) -> tuple[ChargeableSegment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT segment FROM segment_edge_floor ORDER BY segment"
            ).fetchall()
        return tuple(ChargeableSegment(row[0]) for row in rows)
