"""The book the paper loop fills against: read once, resolved onto the grid, refused when stale.

The three behaviours that matter are the three ways this could quietly manufacture a fill: reading
the tape again per call (correct but unusable, which is why the first `R.05` attempt did not
finish), carrying a book forward past the point it stopped describing the market, and turning an
instrument the tape never recorded into a fillable one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, IntegrityFlag
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    UnknownInstrumentError,
)
from nse_algo_trader.paper_loop.replayed_depth_book_source import SteppedRecordedBookSource
from nse_algo_trader.replay_session_clock import session_for

SESSION_DATE = date(2026, 8, 11)
OPENS_AT = session_for(SESSION_DATE).opens_at
TOKEN = 738561


def _snapshot(at: datetime) -> BookSnapshot:
    return BookSnapshot(
        instrument_token=TOKEN,
        receipt_time=at,
        receipt_sequence=int(at.timestamp()),
        capture_run="test",
        exchange_time=at,
        last_price_paise=100_000,
        last_traded_quantity=1,
        volume_traded=10,
        total_buy_quantity=50,
        total_sell_quantity=50,
        integrity_flags=IntegrityFlag.NONE,
        bids=(DepthLevel(price_paise=99_995, quantity=25, orders=2),),
        asks=(DepthLevel(price_paise=100_005, quantity=25, orders=2),),
    )


@dataclass
class ReplayEngineDouble:
    """Stands in for the replay engine, and COUNTS its reads — that is half the point."""

    snapshots: list[BookSnapshot]
    threshold_millis: float
    reads: int = 0
    unknown_tokens: frozenset[int] = field(default_factory=frozenset)

    def session_snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        self.reads += 1
        if instrument_token in self.unknown_tokens:
            raise UnknownInstrumentError(f"instrument {instrument_token} has no rows")
        return list(self.snapshots)

    def staleness_threshold_millis_for(self, snapshots: list[BookSnapshot]) -> float:
        del snapshots
        return self.threshold_millis


def _grid(count: int, step: timedelta = timedelta(minutes=5)) -> list[datetime]:
    return [OPENS_AT + step * index for index in range(count)]


def test_the_tape_is_read_once_per_instrument_however_many_instants_are_asked_about() -> None:
    grid = _grid(20)
    engine = ReplayEngineDouble(
        snapshots=[_snapshot(instant - timedelta(seconds=1)) for instant in grid],
        threshold_millis=60_000,
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    for instant in grid:
        assert source.book_at(TOKEN, instant) is not None
    assert engine.reads == 1, "one parquet window per instrument, not one per decision instant"
    assert source.instruments_loaded == 1


def test_a_book_older_than_the_instruments_own_staleness_threshold_is_refused() -> None:
    """A fill against an hour-old book is a fill at a price with no counterparty behind it."""
    grid = _grid(5)
    engine = ReplayEngineDouble(
        snapshots=[_snapshot(OPENS_AT)],  # one book, at the open, and nothing after
        threshold_millis=1_000,  # this scrip normally ticks within a second
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    assert source.book_at(TOKEN, grid[0]) is not None
    for instant in grid[1:]:
        assert source.book_at(TOKEN, instant) is None


def test_a_book_within_the_threshold_is_still_served() -> None:
    grid = _grid(3)
    engine = ReplayEngineDouble(
        snapshots=[_snapshot(grid[1] - timedelta(milliseconds=500))],
        threshold_millis=1_000,
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    assert source.book_at(TOKEN, grid[0]) is None, "nothing was recorded before the first instant"
    assert source.book_at(TOKEN, grid[1]) is not None
    assert source.book_at(TOKEN, grid[2]) is None, "500ms fresh at grid[1], five minutes stale now"


def test_an_instrument_the_tape_never_recorded_is_reported_not_filled() -> None:
    grid = _grid(4)
    engine = ReplayEngineDouble(
        snapshots=[], threshold_millis=60_000, unknown_tokens=frozenset({TOKEN})
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    for instant in grid:
        assert source.book_at(TOKEN, instant) is None
    assert source.instruments_with_no_tape == frozenset({TOKEN})
    assert engine.reads == 1, "a token with no tape is not re-scanned on every step"


def test_the_book_served_is_the_last_one_recorded_at_or_before_the_instant() -> None:
    grid = _grid(3)
    early = _snapshot(grid[1] - timedelta(seconds=30))
    late = _snapshot(grid[1] - timedelta(seconds=1))
    after = _snapshot(grid[1] + timedelta(seconds=1))
    engine = ReplayEngineDouble(snapshots=[early, late, after], threshold_millis=60_000)
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    served = source.book_at(TOKEN, grid[1])
    assert served is not None
    assert served.receipt_time == late.receipt_time, (
        "the book recorded AFTER the decision instant must never be the one filled against"
    )


def test_the_covered_window_is_the_first_and_last_instant_any_book_was_served() -> None:
    """`A.116`: the capture is not a whole session, and the loop must be told where it stops."""
    grid = _grid(6)
    engine = ReplayEngineDouble(
        snapshots=[
            _snapshot(grid[1] - timedelta(seconds=1)),
            _snapshot(grid[3] - timedelta(seconds=1)),
        ],
        threshold_millis=60_000,
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    source.book_at(TOKEN, grid[0])
    window = source.covered_window()
    assert window is not None
    assert window == (grid[1], grid[3])


def test_a_tape_that_serves_nothing_reports_no_window() -> None:
    grid = _grid(4)
    engine = ReplayEngineDouble(
        snapshots=[], threshold_millis=60_000, unknown_tokens=frozenset({TOKEN})
    )
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    source.book_at(TOKEN, grid[0])
    assert source.covered_window() is None


def test_an_instrument_with_no_gap_distribution_does_not_get_an_eternal_book() -> None:
    """`A.117`: `inf` means "call nothing stale", which is right for a feature and wrong for a fill.

    One recorded packet would otherwise be served at every later instant of the session, and an
    order would fill against a snapshot hours old at a price with no counterparty behind it.
    """
    grid = _grid(6)
    engine = ReplayEngineDouble(snapshots=[_snapshot(grid[0])], threshold_millis=float("inf"))
    source = SteppedRecordedBookSource(engine, grid)  # type: ignore[arg-type]
    assert source.book_at(TOKEN, grid[0]) is not None
    assert source.book_at(TOKEN, grid[1]) is None, "one step later the tape says nothing"
    assert all(source.book_at(TOKEN, instant) is None for instant in grid[1:])
