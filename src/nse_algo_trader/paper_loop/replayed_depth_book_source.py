"""The recorded book at each decision instant, loaded once per instrument (`F04`).

`OrderBookSnapshotReplayEngine.book_at` re-reads the instrument's whole parquet window on EVERY
call — correct, and fine for the one-shot microstructure surface it was written for. A paper
session asks for a book once per instrument per step, which for a full session over the real
universe is tens of thousands of parquet scans of an 11-million-row tape. The first `R.05` attempt
did not finish.

**What this adds is a shape, not a shortcut.** The loop only ever asks for the book at a decision
instant, and the clock only ever moves forward, so an instrument's tape is read once and reduced to
exactly the snapshots those instants resolve to — the last book recorded at or before each step.
Nothing is interpolated, and the answer for any given step is the one `book_at` would have given —
with one deliberate difference, below.

**A stale book is not a book.** `book_at` returns the last snapshot at or before the instant however
old it is, which is right for a microstructure feature and wrong for a fill: an order filled against
a book recorded an hour earlier is filled at a price with no counterparty behind it. So a snapshot
is carried forward only while it is fresh by the instrument's OWN standard — the same derived gap
quantile the replay engine uses to flag staleness, `staleness_threshold_millis_for`, never a chosen
number of seconds. Past that, the answer is `None`, the loop tells the venue to forget the book, and
nothing fills.

**An instrument with no tape is a first-class answer.** `OrderBookSnapshotReplayEngine` raises
`UnknownInstrumentError` for a token it has no rows for, precisely so a wrong token and an
uncaptured instrument stay distinguishable. Here that becomes `None` — no book, therefore no fill —
and the token is remembered so the tape is not scanned for it again. The count is reported, because
"the venue refused every order" and "the tape never recorded this scrip" are different facts about
a session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    OrderBookReplayError,
    OrderBookSnapshotReplayEngine,
)


@dataclass(slots=True)
class SteppedRecordedBookSource:
    """One parquet read per instrument, resolved onto the session's decision grid."""

    engine: OrderBookSnapshotReplayEngine
    decision_instants: Sequence[datetime]
    _books: dict[int, dict[datetime, BookSnapshot]] = field(default_factory=dict, repr=False)
    _untaped: set[int] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.decision_instants:
            raise OrderBookReplayError(
                "a book source with no decision instants can answer nothing; the grid comes from "
                "the clock so that the books held are exactly the books the loop will ask for"
            )

    @property
    def instruments_with_no_tape(self) -> frozenset[int]:
        """Tokens the tape never recorded — reported, never conflated with a refusal to fill."""
        return frozenset(self._untaped)

    @property
    def instruments_loaded(self) -> int:
        return len(self._books)

    def book_at(self, instrument_token: int, as_of: datetime) -> BookSnapshot | None:
        """The last book recorded at or before `as_of`, or `None` if there is none."""
        if instrument_token in self._untaped:
            return None
        grid = self._books.get(instrument_token)
        if grid is None:
            grid = self._load(instrument_token)
            if grid is None:
                return None
        return grid.get(as_of)

    def _load(self, instrument_token: int) -> dict[datetime, BookSnapshot] | None:
        """Read this instrument's session once and reduce it onto the decision grid."""
        try:
            snapshots = self.engine.session_snapshots_for(instrument_token)
        except OrderBookReplayError:
            # Covers both an uncaptured instrument and one the consolidated feed ruled
            # inadmissible (`L0.33`): in either case this session has no evidence about its book,
            # and a fill produced anyway would be an invention.
            self._untaped.add(instrument_token)
            return None
        ordered = sorted(snapshots, key=lambda snapshot: snapshot.receipt_time)
        threshold_millis = self.engine.staleness_threshold_millis_for(ordered)
        grid: dict[datetime, BookSnapshot] = {}
        cursor = 0
        latest: BookSnapshot | None = None
        for instant in self.decision_instants:
            while cursor < len(ordered) and ordered[cursor].receipt_time <= instant:
                latest = ordered[cursor]
                cursor += 1
            if latest is None:
                continue
            age_millis = (instant - latest.receipt_time).total_seconds() * 1000
            if age_millis <= threshold_millis:
                grid[instant] = latest
        self._books[instrument_token] = grid
        return grid
