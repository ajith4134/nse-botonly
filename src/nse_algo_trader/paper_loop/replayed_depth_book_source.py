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

from array import array
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    OrderBookReplayError,
    OrderBookSnapshotReplayEngine,
    book_snapshots_from_table,
)

INSTANTS_NEEDED_FOR_A_STEP = 2
"""Two instants is the least that defines the grid's step — the bound used when an instrument has
no gap distribution of its own."""

MINIMUM_GAPS_FOR_A_QUANTILE = 2
"""Two gaps is the least that can be a distribution. Below it the replay engine calls nothing
stale rather than comparing against a fabricated threshold, and this must use the same rule."""


@dataclass(slots=True)
class SteppedRecordedBookSource:
    """One parquet read per instrument, resolved onto the session's decision grid."""

    engine: OrderBookSnapshotReplayEngine
    decision_instants: Sequence[datetime]
    staleness_quantile: float = 0.95
    """Which gap quantile counts as stale when `preload` derives the threshold itself. It matches
    the quantile the replay engine was constructed with; a caller passing a different one is
    saying the fill path and the microstructure surface may disagree about staleness."""

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

    def preload(
        self,
        tape_reader: MarketDepthTapeReader,
        instrument_tokens: Sequence[int],
        *,
        session_date: date,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        """Read the whole session ONCE for these instruments and build every grid in one pass.

        Per-instrument reads are correct and, over a real universe, unusable: 2,403 instruments
        against an eleven-million-row day is 2,403 full scans, and the first full-universe run was
        on course for six hours of them. This makes the same grids from a single streamed scan.

        Each instrument's staleness threshold is still ITS OWN — the gaps are collected per token
        as the batches arrive and quantiled at the end, exactly as the replay engine does it —
        because a threshold pooled across the universe would let a scrip that ticks once a minute
        borrow the freshness of one that ticks ten times a second.

        Returns the number of instruments that had any rows at all.
        """
        gaps_by_token: dict[int, array[float]] = {}
        last_seen: dict[int, datetime] = {}
        candidates: dict[int, dict[datetime, BookSnapshot]] = {}
        instants = list(self.decision_instants)
        for table in tape_reader.iter_session_tables_for_instruments(
            instrument_tokens, window_start, window_end, session_date
        ):
            for snapshot in book_snapshots_from_table(table):
                token = snapshot.instrument_token
                previous = last_seen.get(token)
                if previous is not None:
                    gaps = gaps_by_token.setdefault(token, array("d"))
                    gaps.append(
                        abs((snapshot.receipt_time - previous).total_seconds()) * 1_000
                    )
                last_seen[token] = snapshot.receipt_time
                index = bisect_left(instants, snapshot.receipt_time)
                if index >= len(instants):
                    continue
                instant = instants[index]
                grid = candidates.setdefault(token, {})
                held = grid.get(instant)
                if held is None or snapshot.receipt_time > held.receipt_time:
                    grid[instant] = snapshot

        for token in instrument_tokens:
            found = candidates.get(token)
            if found is None:
                self._untaped.add(token)
                continue
            self._books[token] = self._forward_fill(
                found,
                threshold_millis=self._fill_threshold(
                    _gap_quantile(gaps_by_token.get(token), self.staleness_quantile)
                ),
            )
        return len(candidates)

    def _fill_threshold(self, derived_millis: float) -> float:
        """The staleness bound for FILLING, which is not the same question as for a feature.

        `_gap_quantile` and the replay engine both return `inf` when an instrument has too few gaps
        to form a distribution — "call nothing stale rather than compare against a fabricated
        threshold", which is right for a microstructure feature and wrong here. An instrument with
        ONE recorded packet would have its book served at every later instant of the session, and an
        order would fill against a snapshot hours old at a price with no counterparty behind it
        (`A.117`).

        With no distribution, the honest bound is the decision grid's own step: a book at least one
        step old has been superseded by an instant the tape says nothing about. Derived from the
        caller's grid, not chosen.
        """
        if derived_millis != float("inf"):
            return derived_millis
        if len(self.decision_instants) < INSTANTS_NEEDED_FOR_A_STEP:
            return 0.0
        step = self.decision_instants[1] - self.decision_instants[0]
        # One tick INSIDE the step: a book exactly one step old has been superseded by an instant
        # the tape says nothing about, so it is stale rather than borderline.
        return step.total_seconds() * 1_000 - 1

    def _forward_fill(
        self, candidates: dict[datetime, BookSnapshot], *, threshold_millis: float
    ) -> dict[datetime, BookSnapshot]:
        """Carry each book forward across the grid, but only while it is still fresh."""
        grid: dict[datetime, BookSnapshot] = {}
        latest: BookSnapshot | None = None
        for instant in self.decision_instants:
            candidate = candidates.get(instant)
            if candidate is not None and (
                latest is None or candidate.receipt_time > latest.receipt_time
            ):
                latest = candidate
            if latest is None:
                continue
            age_millis = (instant - latest.receipt_time).total_seconds() * 1_000
            if age_millis <= threshold_millis:
                grid[instant] = latest
        return grid

    def covered_window(self) -> tuple[datetime, datetime] | None:
        """The first and last decision instant any instrument has a book at, or `None` for none.

        The depth capture is not a whole session: it starts after the open and can stop before the
        close (`A.116`). A caller that replays outside this window is asking the loop to decide at
        instants no order could have filled at, and every number drawn from those instants measures
        the capture rather than the market.
        """
        served: list[datetime] = []
        for grid in self._books.values():
            served.extend(grid)
        if not served:
            return None
        return min(served), max(served)

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
        threshold_millis = self._fill_threshold(
            self.engine.staleness_threshold_millis_for(ordered)
        )
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


def _gap_quantile(gaps: array[float] | None, quantile: float) -> float:
    """The same linear-interpolated quantile the replay engine takes, on this token's own gaps.

    `inf` when there are too few gaps to have a distribution: nothing is called stale rather than
    everything being compared against a fabricated threshold, which is the replay engine's rule and
    has to stay the same rule here.
    """
    if gaps is None or len(gaps) < MINIMUM_GAPS_FOR_A_QUANTILE:
        return float("inf")
    ordered = sorted(gaps)
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
