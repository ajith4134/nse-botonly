"""`L0.22` — replay the depth tape and infer the order flow the snapshots do not show.

Spec: `docs/research/214`.

**Named for what it is.** The plan calls this "tick-level order-book reconstruction" and
also says it is retail-infeasible, and both are true: rebuilding a book tick by tick needs
order-by-order messages that NSE sells for ₹12.5 lakh/year behind an institutional licence
(`research/72`). What this project has is Kite's 5-level snapshot at a measured p10 gap of
0.25s. So this is a snapshot REPLAY engine, and per `R.14` it is called one.

That is not a smaller problem, only a different one. The quantities a strategy needs —
which side initiated a trade, whether a queue drained by execution or cancellation, where
the pressure in the book is — are all unobservable in a snapshot feed and all estimable
from it. The estimators here are the ones the literature built for exactly this data:

- **Order Flow Imbalance**, Cont-Kukanov-Stoikov (2014), defined on consecutive L2
  snapshots. This is the SOTA analog `R.23(a)` asks for, and the depth bar to clear.
- **Trade-side classification**, quote rule first with a Lee-Ready (1991) tick-rule
  fallback, driven by the cumulative `volume_traded` delta.
- **Micro-price**, Stoikov (2018) — the imbalance-weighted fair price.
- **Queue depletion decomposition**, reported as an INTERVAL because the snapshot gap
  makes it genuinely under-determined and a point estimate would be a fiction.

**Every emitted row carries the fidelity that produced it** — the inter-snapshot gap, the
integrity flags of both books, the duplicate run length, and whether the transition
crossed a capture-run boundary. A feature computed across a 4-second hole is a different
measurement from one computed across 250ms, and a consumer that cannot tell them apart
will average them into nonsense.

**There is no ground truth available to check the estimates against**, so the tests are
the conservation and symmetry laws the estimators are defined by, asserted on the real
tape rather than on fixtures.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from itertools import pairwise

import pyarrow as pa

from nse_algo_trader.market_depth.depth_tape_schema import (
    DEPTH_LEVELS_PER_SIDE,
    DepthLevel,
    IntegrityFlag,
)
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader

MILLISECONDS_PER_SECOND = 1_000
"""A unit conversion, not a tunable."""

DECILE_COUNT = 10
"""Ten parts is what "decile" means. A definition, not a tuning knob."""

MINIMUM_GAPS_FOR_A_QUANTILE = 2
"""Interpolating between order statistics needs two of them. Arithmetic, not policy."""


class OrderBookReplayError(RuntimeError):
    """Base for every failure this engine raises."""


class UnknownInstrumentError(OrderBookReplayError):
    """The requested instrument has no rows in this session.

    Deliberately an error rather than an empty iterator: "this instrument was never
    captured" and "this token is wrong" are different bugs, and an empty iterator makes
    them the same observation.
    """


class TradeSideRule(Enum):
    """Which rule decided a trade's side, so the consumer can weigh the answer."""

    QUOTE_RULE = "quote_rule"
    TICK_RULE = "tick_rule"
    ZERO_TICK_INHERITED = "zero_tick_inherited"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class SignedVolumeAttribution:
    """Traded quantity split by inferred aggressor, with the rule that split it.

    The three parts always sum to `traded_quantity`. A classifier that loses or
    duplicates volume is worse than one that admits ignorance, because the error is
    invisible once aggregated.
    """

    traded_quantity: int
    buyer_initiated_quantity: int
    seller_initiated_quantity: int
    unclassified_quantity: int
    rule: TradeSideRule

    @property
    def signed_quantity(self) -> int:
        return self.buyer_initiated_quantity - self.seller_initiated_quantity


@dataclass(frozen=True, slots=True)
class QueueDepletionInterval:
    """How much of a queue's shrinkage was execution and how much was cancellation.

    An interval, not a point. Between two snapshots the book can have moved many times,
    so the split is under-determined by construction; the bounds are what the data
    actually supports.
    """

    depleted_quantity: int
    minimum_executed: int
    maximum_executed: int
    minimum_cancelled: int
    maximum_cancelled: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """One tape row, typed, with the book as levels rather than 30 flat columns."""

    instrument_token: int
    receipt_time: datetime
    receipt_sequence: int
    capture_run: str
    exchange_time: datetime | None
    last_price_paise: int
    last_traded_quantity: int
    volume_traded: int
    total_buy_quantity: int
    total_sell_quantity: int
    integrity_flags: IntegrityFlag
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]

    @property
    def best_bid_paise(self) -> int | None:
        return self.bids[0].price_paise if self.bids else None

    @property
    def best_ask_paise(self) -> int | None:
        return self.asks[0].price_paise if self.asks else None

    @property
    def best_bid_quantity(self) -> int:
        return self.bids[0].quantity if self.bids else 0

    @property
    def best_ask_quantity(self) -> int:
        return self.asks[0].quantity if self.asks else 0

    @property
    def mid_paise(self) -> int | None:
        """Integer paise, rounded half-up. Half-even would bias a one-paise spread."""
        if self.best_bid_paise is None or self.best_ask_paise is None:
            return None
        return (self.best_bid_paise + self.best_ask_paise + 1) // 2

    @property
    def spread_paise(self) -> int | None:
        if self.best_bid_paise is None or self.best_ask_paise is None:
            return None
        return self.best_ask_paise - self.best_bid_paise

    def book_equals(self, other: BookSnapshot) -> bool:
        return self.bids == other.bids and self.asks == other.asks


@dataclass(frozen=True, slots=True)
class MicrostructureFeatureRow:
    """One transition between consecutive snapshots — the unit of everything here.

    N snapshots produce N-1 rows: order flow is a property of a CHANGE, and the first
    snapshot of a session has nothing to have changed from.
    """

    instrument_token: int
    receipt_time: datetime
    gap_milliseconds: int
    capture_run: str
    crossed_capture_run_boundary: bool
    duplicate_run_length: int
    is_stale_by_derived_threshold: bool
    previous_integrity_flags: IntegrityFlag
    integrity_flags: IntegrityFlag

    best_bid_paise: int | None
    best_ask_paise: int | None
    mid_paise: int | None
    spread_paise: int | None
    micro_price_paise: int | None

    order_flow_imbalance_by_level: tuple[int, ...]
    order_flow_imbalance_level_one: int
    order_flow_imbalance_depth_weighted: float
    depth_imbalance_by_level: tuple[float | None, ...]

    signed_volume: SignedVolumeAttribution
    bid_queue_depletion: QueueDepletionInterval | None
    ask_queue_depletion: QueueDepletionInterval | None


@dataclass(frozen=True, slots=True)
class InstrumentCoverageReport:
    """What the replay actually managed to say, measured — never asserted.

    `R.08`: the dashboard surface reads this, so every number here is computed from the
    rows that were emitted.
    """

    session_date: date
    instruments_replayed: int
    feature_rows_emitted: int
    quote_rule_fraction: float
    tick_rule_fraction: float
    zero_tick_fraction: float
    unclassified_fraction: float
    median_gap_milliseconds: float
    gap_millisecond_deciles: tuple[float, ...]
    duplicate_row_fraction: float
    capture_run_boundaries_crossed: int


# --------------------------------------------------------------------- estimators


def _deciles(values: Sequence[int]) -> tuple[float, ...]:
    """The nine interior deciles, so the dashboard bins by the data's own shape.

    Fixed bin edges ("0-100ms, 100-500ms, ...") would be exactly the hardcoded constants
    `R.03` forbids, and would also be wrong: a one-packet-a-minute instrument and a
    ten-a-second one share no sensible edge. Deciles are the distribution describing
    itself. Fewer than ten samples cannot support them, so nothing is reported rather
    than a shape invented from three points.
    """
    if len(values) < DECILE_COUNT:
        return ()
    ordered = sorted(values)
    deciles: list[float] = []
    for step in range(1, DECILE_COUNT):
        position = step / DECILE_COUNT * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        deciles.append(ordered[lower] * (1 - weight) + ordered[upper] * weight)
    return tuple(deciles)


def _level_at(levels: Sequence[DepthLevel], index: int) -> DepthLevel | None:
    return levels[index] if index < len(levels) else None


def order_flow_imbalance_by_level(
    previous_bids: Sequence[DepthLevel],
    previous_asks: Sequence[DepthLevel],
    current_bids: Sequence[DepthLevel],
    current_asks: Sequence[DepthLevel],
) -> tuple[int, ...]:
    """Cont-Kukanov-Stoikov order-flow imbalance, evaluated at each level independently.

    For one level, with `b` the bid side and `a` the ask side::

        e =  q^b_n · 1{P^b_n ≥ P^b_{n-1}}  -  q^b_{n-1} · 1{P^b_n ≤ P^b_{n-1}}
           - q^a_n · 1{P^a_n ≤ P^a_{n-1}}  +  q^a_{n-1} · 1{P^a_n ≥ P^a_{n-1}}

    Read it as pressure: a bid that held or improved while growing is buying interest
    arriving; a bid that fell away is buying interest leaving. When neither price moves,
    every indicator is true and the expression collapses to the size delta — which is the
    identity that makes order flow computable from snapshots at all.

    **An absent level is absence, not a zero price.** A vanished bid is treated as
    infinitely far below the previous one (so it counts as the old size leaving) and a
    vanished ask as infinitely far above. Treating a missing level as price zero would
    read a disappearing bid as the largest price improvement in the book's history.

    Always returns exactly `DEPTH_LEVELS_PER_SIDE` entries so callers can index without
    checking; levels neither snapshot carried contribute zero.
    """
    imbalances: list[int] = []
    for index in range(DEPTH_LEVELS_PER_SIDE):
        previous_bid = _level_at(previous_bids, index)
        current_bid = _level_at(current_bids, index)
        previous_ask = _level_at(previous_asks, index)
        current_ask = _level_at(current_asks, index)

        bid_term = 0
        if previous_bid is not None or current_bid is not None:
            current_bid_quantity = current_bid.quantity if current_bid else 0
            previous_bid_quantity = previous_bid.quantity if previous_bid else 0
            if previous_bid is None or current_bid is None:
                # One side of the comparison does not exist: an arriving level is an
                # improvement over nothing, a vanished one is the worst possible price.
                bid_improved = current_bid is not None
                bid_worsened = current_bid is None
            else:
                bid_improved = current_bid.price_paise >= previous_bid.price_paise
                bid_worsened = current_bid.price_paise <= previous_bid.price_paise
            bid_term = (current_bid_quantity if bid_improved else 0) - (
                previous_bid_quantity if bid_worsened else 0
            )

        ask_term = 0
        if previous_ask is not None or current_ask is not None:
            current_ask_quantity = current_ask.quantity if current_ask else 0
            previous_ask_quantity = previous_ask.quantity if previous_ask else 0
            if previous_ask is None or current_ask is None:
                ask_improved = current_ask is not None  # a nearer ask is selling pressure
                ask_worsened = current_ask is None
            else:
                ask_improved = current_ask.price_paise <= previous_ask.price_paise
                ask_worsened = current_ask.price_paise >= previous_ask.price_paise
            ask_term = -(current_ask_quantity if ask_improved else 0) + (
                previous_ask_quantity if ask_worsened else 0
            )

        imbalances.append(bid_term + ask_term)
    return tuple(imbalances)


def depth_imbalance_by_level(
    bids: Sequence[DepthLevel], asks: Sequence[DepthLevel]
) -> tuple[float | None, ...]:
    """`(bid - ask) / (bid + ask)` per level, in `[-1, 1]`.

    `None` where a level is absent on either side. Zero would mean "balanced", which is a
    claim; absence is not a claim, and the two must not share a value.
    """
    imbalances: list[float | None] = []
    for index in range(DEPTH_LEVELS_PER_SIDE):
        bid = _level_at(bids, index)
        ask = _level_at(asks, index)
        if bid is None or ask is None or (bid.quantity + ask.quantity) == 0:
            imbalances.append(None)
            continue
        imbalances.append((bid.quantity - ask.quantity) / (bid.quantity + ask.quantity))
    return tuple(imbalances)


def micro_price_paise(
    *,
    best_bid_paise: int | None,
    best_bid_quantity: int,
    best_ask_paise: int | None,
    best_ask_quantity: int,
) -> int | None:
    """Stoikov's imbalance-weighted fair price, in integer paise.

    `(P^a·q^b + P^b·q^a) / (q^b + q^a)` — heavier bid pulls the fair price toward the ask,
    because that is the side about to be consumed. Undefined, not zero, when either side
    is missing or empty: a consumer that sees a number will use it.

    The result always lies within `[bid, ask]` (both integers, so rounding cannot escape
    the interval), which the property test asserts rather than assumes.

    **Undefined on a crossed book, and that is not a technicality.** Measured on the
    2026-08-11 tape: 757 of 135,401 transitions across 60 instruments (0.56%) had the ask
    below the bid, and the recorder's `BOOK_CROSSED` flag caught every one of them. The
    weighted formula still yields a number there — it produced 770,584 paise against a
    best bid of 781,900 — but a fair price below every bid in the book is a value derived
    from a contradiction, and returning it would launder a stale feed into a price a
    consumer would trade on. The row still carries the flag, so the crossing stays visible
    as evidence rather than being smoothed away.
    """
    if best_bid_paise is None or best_ask_paise is None:
        return None
    if best_ask_paise < best_bid_paise:
        return None
    total_quantity = best_bid_quantity + best_ask_quantity
    if total_quantity <= 0:
        return None
    weighted = best_ask_paise * best_bid_quantity + best_bid_paise * best_ask_quantity
    return round(weighted / total_quantity)


def classify_trade_side(
    *,
    traded_quantity: int,
    trade_price_paise: int,
    previous_best_bid_paise: int | None,
    previous_best_ask_paise: int | None,
    previous_trade_price_paise: int | None,
    last_signed_direction: int,
) -> SignedVolumeAttribution:
    """Split traded quantity into buyer- and seller-initiated, and say how.

    Order of preference, strongest evidence first:

    1. **Quote rule** — a trade above the prevailing MIDPOINT was taken by a buyer, below
       it by a seller. This is Lee-Ready's actual quote rule, and the first version here
       got it wrong: it compared against the touch prices instead, classifying only
       trades at or through a quote. That is a stricter variant which is not wrong so much
       as *weak* — every trade inside the spread fell through to the tick rule, which is
       the poorer estimator. Caught by differential-testing against `tclf`, which
       classified `bid=1 ask=4 trade=2` as a sell (below the 2.5 midpoint) where this
       function said "unclassified" (`research/214` §6).

       Compared as `2 · price` against `bid + ask` so a half-paise midpoint stays exact;
       dividing would round the midpoint and misclassify trades sitting on it.
    2. **Tick rule** (Lee-Ready) — compare against the previous trade price. Up-tick buy,
       down-tick sell.
    3. **Zero tick** — inherit the last known direction rather than guess.
    4. **Unclassified** — no prevailing quote and no prior trade. The quantity is recorded
       as unattributed. Assigning a side here would fabricate order flow, and fabricated
       flow is indistinguishable from real flow once summed.

    The quotes are the ones prevailing BEFORE the trade, which is why the caller passes
    the previous snapshot's book: comparing a trade against the book it already moved
    classifies backwards.
    """
    if traded_quantity <= 0:
        return SignedVolumeAttribution(0, 0, 0, 0, TradeSideRule.UNCLASSIFIED)

    def buy(rule: TradeSideRule) -> SignedVolumeAttribution:
        return SignedVolumeAttribution(traded_quantity, traded_quantity, 0, 0, rule)

    def sell(rule: TradeSideRule) -> SignedVolumeAttribution:
        return SignedVolumeAttribution(traded_quantity, 0, traded_quantity, 0, rule)

    if previous_best_bid_paise is not None and previous_best_ask_paise is not None:
        doubled_midpoint = previous_best_bid_paise + previous_best_ask_paise
        doubled_trade_price = 2 * trade_price_paise
        if doubled_trade_price > doubled_midpoint:
            return buy(TradeSideRule.QUOTE_RULE)
        if doubled_trade_price < doubled_midpoint:
            return sell(TradeSideRule.QUOTE_RULE)

    if previous_trade_price_paise is not None:
        if trade_price_paise > previous_trade_price_paise:
            return buy(TradeSideRule.TICK_RULE)
        if trade_price_paise < previous_trade_price_paise:
            return sell(TradeSideRule.TICK_RULE)

    if last_signed_direction > 0:
        return buy(TradeSideRule.ZERO_TICK_INHERITED)
    if last_signed_direction < 0:
        return sell(TradeSideRule.ZERO_TICK_INHERITED)

    return SignedVolumeAttribution(
        traded_quantity, 0, 0, traded_quantity, TradeSideRule.UNCLASSIFIED
    )


def queue_depletion_interval(
    *,
    previous_quantity: int,
    current_quantity: int,
    price_unchanged: bool,
    inferred_executed_quantity: int,
) -> QueueDepletionInterval | None:
    """Bound how much of a queue's shrinkage was execution versus cancellation.

    `None` when the touch price moved: the old queue did not deplete, it was replaced, and
    a decomposition of a queue that no longer exists is meaningless rather than merely
    uncertain.

    Executions are bounded above by the inferred aggressive volume on the opposite side
    over the same interval, and by the depletion itself — signed-volume inference is an
    estimate and can legitimately exceed what the visible queue lost, since trades also
    hit levels this side of the book cannot see.
    """
    if not price_unchanged:
        return None
    depleted = max(0, previous_quantity - current_quantity)
    maximum_executed = min(depleted, max(0, inferred_executed_quantity))
    return QueueDepletionInterval(
        depleted_quantity=depleted,
        minimum_executed=0,
        maximum_executed=maximum_executed,
        minimum_cancelled=depleted - maximum_executed,
        maximum_cancelled=depleted,
    )


# ------------------------------------------------------------------- tape decoding


def _levels_from_row(row: dict[str, object], side: str) -> tuple[DepthLevel, ...]:
    """The REAL levels of one side, stopping at the feed's zero padding.

    **Measured, and it broke the first real-data run.** Kite always sends five levels a
    side and pads the ones that do not exist with price 0, quantity 0, orders 0. Treating
    a padded level as a quote makes `best_ask_paise` zero for any instrument whose book is
    one-sided, which sent the micro-price to 0 while the best bid was 640 paise — a fair
    price below every price in the book, and exactly the kind of number a consumer would
    have used without blinking.

    On the 2026-08-11 tape, sampled across one instrument's session: a zero price came
    with a zero quantity in every one of 195 padded levels, and never with real size. So
    `price_paise > 0` is the test for a level existing, and it is a measurement rather
    than a guess. Padding runs from the end inward, so the first padded level truncates
    the side — a real level appearing AFTER a padded one would be a feed defect, and
    truncating keeps every surviving level at its true depth index instead of silently
    promoting a level-3 quote into level 2's slot.
    """
    levels: list[DepthLevel] = []
    for index in range(DEPTH_LEVELS_PER_SIDE):
        price = row.get(f"{side}_price_paise_{index}")
        quantity = row.get(f"{side}_quantity_{index}")
        orders = row.get(f"{side}_orders_{index}")
        if not isinstance(price, int) or not isinstance(quantity, int) or price <= 0:
            break
        levels.append(DepthLevel(price, quantity, orders if isinstance(orders, int) else 0))
    return tuple(levels)


def book_snapshots_from_table(table: pa.Table) -> list[BookSnapshot]:
    """Typed, replay-ordered snapshots from a tape table.

    **Ordered by receipt time first, capture run second, sequence last.** The sequence
    counter restarts per feed, so ordering by it alone lets an older run's high counter
    beat a newer run's low one and return a stale book while looking perfectly sorted.
    Measured on the 2026-08-11 tape: two runs with overlapping ranges 1..132,313 across
    4,236 shared instruments.
    """
    rows = table.to_pylist()
    snapshots = [
        BookSnapshot(
            instrument_token=int(row["instrument_token"]),
            receipt_time=row["receipt_time"],
            receipt_sequence=int(row["receipt_sequence"]),
            capture_run=str(row.get("capture_run", "")),
            exchange_time=row.get("exchange_time"),
            last_price_paise=int(row["last_price_paise"]),
            last_traded_quantity=int(row["last_traded_quantity"]),
            volume_traded=int(row["volume_traded"]),
            total_buy_quantity=int(row["total_buy_quantity"]),
            total_sell_quantity=int(row["total_sell_quantity"]),
            integrity_flags=IntegrityFlag(int(row["integrity_flags"])),
            bids=_levels_from_row(row, "bid"),
            asks=_levels_from_row(row, "ask"),
        )
        for row in rows
    ]
    snapshots.sort(key=lambda s: (s.receipt_time, s.capture_run, s.receipt_sequence))
    return snapshots


# ---------------------------------------------------------------------- the engine


class OrderBookSnapshotReplayEngine:
    """Replays one session of the depth tape and emits microstructure observations.

    State carried across a replay — and it IS the algorithm, not an optimisation:

    - the previous book, without which order-flow imbalance is undefined;
    - the last trade price and last inferred direction, which the tick rule needs;
    - the running duplicate-book run length, so a stall is visible as a stall;
    - a running mean of `|OFI|` per level, which supplies the depth weights so that no
      hand-picked 0.5/0.3/0.2 ladder ever appears (`R.03`).
    """

    def __init__(
        self,
        tape_reader: MarketDepthTapeReader,
        *,
        session_date: date,
        staleness_quantile: float,
        inadmissible_instruments: frozenset[int] = frozenset(),
    ) -> None:
        """`staleness_quantile` is a POLICY input and deliberately has no default.

        The threshold itself is derived per instrument from its own observed gaps — a
        one-packet-a-minute instrument and a ten-a-second one cannot share a constant —
        but which quantile counts as "stale" is a choice the operator owns, exactly like
        the capture's retention and coverage floor.
        """
        if not 0.0 < staleness_quantile < 1.0:
            raise OrderBookReplayError(
                f"staleness_quantile must lie in (0, 1), got {staleness_quantile}"
            )
        self._tape_reader = tape_reader
        self._session_date = session_date
        self._staleness_quantile = staleness_quantile
        # `L0.33`'s admissibility gate, applied here. The tape is recorded from ONE broker,
        # and where the consolidated feed measured that broker as divergent or frozen for an
        # instrument-session, its rows are not microstructure evidence: features built from
        # a book that disagreed with every other source describe the broker, not the market.
        # Refused loudly rather than filtered silently — a caller asking for an inadmissible
        # instrument has asked a question the data cannot answer.
        self._inadmissible_instruments = inadmissible_instruments

    # -- reading ----------------------------------------------------------------

    def _session_window(self) -> tuple[datetime, datetime]:
        start = datetime.combine(self._session_date, datetime.min.time(), tzinfo=UTC)
        return start, start + timedelta(days=1)

    def _snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        if instrument_token in self._inadmissible_instruments:
            raise OrderBookReplayError(
                f"instrument {instrument_token} is inadmissible for "
                f"{self._session_date.isoformat()}: the consolidated feed (`L0.33`) measured "
                f"this broker as divergent or frozen on it, so its recorded book is evidence "
                f"about the feed rather than about the market"
            )
        window_start, window_end = self._session_window()
        table = self._tape_reader.read_instrument_window(
            instrument_token, window_start, window_end, self._session_date
        )
        if table.num_rows == 0:
            raise UnknownInstrumentError(
                f"instrument {instrument_token} has no rows in the "
                f"{self._session_date.isoformat()} tape — a wrong token and an uncaptured "
                f"instrument are different problems, so this is not an empty result"
            )
        return book_snapshots_from_table(table)

    def book_at(self, instrument_token: int, as_of: datetime) -> BookSnapshot | None:
        """The last known book at or before `as_of`, typed.

        The store offers a dict-shaped version of this; the typed one is what a consumer
        should hold, because it cannot silently mis-key a column name.
        """
        candidates = [
            snapshot
            for snapshot in self._snapshots_for(instrument_token)
            if snapshot.receipt_time <= as_of
        ]
        return candidates[-1] if candidates else None

    # -- replay -----------------------------------------------------------------

    def _derived_staleness_threshold_millis(self, snapshots: Sequence[BookSnapshot]) -> float:
        """This instrument's own gap distribution decides what "stale" means for it.

        Fewer than two gaps is not enough distribution to quantile, so nothing is called
        stale rather than everything being compared against a fabricated threshold.
        """
        gaps = [
            (later.receipt_time - earlier.receipt_time).total_seconds() * MILLISECONDS_PER_SECOND
            for earlier, later in pairwise(snapshots)
        ]
        if len(gaps) < MINIMUM_GAPS_FOR_A_QUANTILE:
            return float("inf")
        ordered = sorted(gaps)
        position = self._staleness_quantile * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    def replay_instrument(self, instrument_token: int) -> Iterator[MicrostructureFeatureRow]:
        """One row per TRANSITION, in replay order.

        Raises `UnknownInstrumentError` eagerly rather than on first iteration, so a bad
        token fails where it was passed rather than wherever the iterator is consumed.
        """
        snapshots = self._snapshots_for(instrument_token)
        threshold_millis = self._derived_staleness_threshold_millis(snapshots)
        return self._replay(snapshots, threshold_millis)

    def _replay(
        self, snapshots: Sequence[BookSnapshot], threshold_millis: float
    ) -> Iterator[MicrostructureFeatureRow]:
        last_trade_price_paise: int | None = None
        last_signed_direction = 0
        duplicate_run_length = 0
        absolute_imbalance_totals = [0.0] * DEPTH_LEVELS_PER_SIDE

        for previous, current in pairwise(snapshots):
            gap_millis = int(
                (current.receipt_time - previous.receipt_time).total_seconds()
                * MILLISECONDS_PER_SECOND
            )
            duplicate_run_length = duplicate_run_length + 1 if current.book_equals(previous) else 0

            imbalance_by_level = order_flow_imbalance_by_level(
                previous.bids, previous.asks, current.bids, current.asks
            )
            for index, value in enumerate(imbalance_by_level):
                absolute_imbalance_totals[index] += abs(value)
            weight_total = sum(absolute_imbalance_totals)
            depth_weighted = (
                sum(
                    value * (absolute_imbalance_totals[index] / weight_total)
                    for index, value in enumerate(imbalance_by_level)
                )
                if weight_total > 0
                else 0.0
            )

            traded_quantity = max(0, current.volume_traded - previous.volume_traded)
            attribution = classify_trade_side(
                traded_quantity=traded_quantity,
                trade_price_paise=current.last_price_paise,
                previous_best_bid_paise=previous.best_bid_paise,
                previous_best_ask_paise=previous.best_ask_paise,
                previous_trade_price_paise=last_trade_price_paise,
                last_signed_direction=last_signed_direction,
            )
            if attribution.signed_quantity != 0:
                last_signed_direction = 1 if attribution.signed_quantity > 0 else -1
            if traded_quantity > 0:
                last_trade_price_paise = current.last_price_paise

            yield MicrostructureFeatureRow(
                instrument_token=current.instrument_token,
                receipt_time=current.receipt_time,
                gap_milliseconds=gap_millis,
                capture_run=current.capture_run,
                crossed_capture_run_boundary=current.capture_run != previous.capture_run,
                duplicate_run_length=duplicate_run_length,
                is_stale_by_derived_threshold=gap_millis > threshold_millis,
                previous_integrity_flags=previous.integrity_flags,
                integrity_flags=current.integrity_flags,
                best_bid_paise=current.best_bid_paise,
                best_ask_paise=current.best_ask_paise,
                mid_paise=current.mid_paise,
                spread_paise=current.spread_paise,
                micro_price_paise=micro_price_paise(
                    best_bid_paise=current.best_bid_paise,
                    best_bid_quantity=current.best_bid_quantity,
                    best_ask_paise=current.best_ask_paise,
                    best_ask_quantity=current.best_ask_quantity,
                ),
                order_flow_imbalance_by_level=imbalance_by_level,
                order_flow_imbalance_level_one=imbalance_by_level[0],
                order_flow_imbalance_depth_weighted=depth_weighted,
                depth_imbalance_by_level=depth_imbalance_by_level(current.bids, current.asks),
                signed_volume=attribution,
                bid_queue_depletion=queue_depletion_interval(
                    previous_quantity=previous.best_bid_quantity,
                    current_quantity=current.best_bid_quantity,
                    price_unchanged=previous.best_bid_paise == current.best_bid_paise,
                    inferred_executed_quantity=attribution.seller_initiated_quantity,
                ),
                ask_queue_depletion=queue_depletion_interval(
                    previous_quantity=previous.best_ask_quantity,
                    current_quantity=current.best_ask_quantity,
                    price_unchanged=previous.best_ask_paise == current.best_ask_paise,
                    inferred_executed_quantity=attribution.buyer_initiated_quantity,
                ),
            )

    # -- outputs ----------------------------------------------------------------

    def session_feature_frame(self, instrument_token: int) -> pa.Table:
        """The instrument's whole session as an Arrow table, for a downstream consumer.

        Interval-valued and level-valued fields are flattened into named columns rather
        than nested, because the consumers this feeds (`L1.05` fill model, `L1.06` impact
        model) join on flat columns and nesting would push the flattening into each.
        """
        rows = list(self.replay_instrument(instrument_token))
        columns: dict[str, list[object]] = {
            "instrument_token": [row.instrument_token for row in rows],
            "receipt_time": [row.receipt_time for row in rows],
            "gap_milliseconds": [row.gap_milliseconds for row in rows],
            "capture_run": [row.capture_run for row in rows],
            "crossed_capture_run_boundary": [row.crossed_capture_run_boundary for row in rows],
            "duplicate_run_length": [row.duplicate_run_length for row in rows],
            "is_stale": [row.is_stale_by_derived_threshold for row in rows],
            "integrity_flags": [int(row.integrity_flags) for row in rows],
            "best_bid_paise": [row.best_bid_paise for row in rows],
            "best_ask_paise": [row.best_ask_paise for row in rows],
            "mid_paise": [row.mid_paise for row in rows],
            "spread_paise": [row.spread_paise for row in rows],
            "micro_price_paise": [row.micro_price_paise for row in rows],
            "order_flow_imbalance_level_one": [row.order_flow_imbalance_level_one for row in rows],
            "order_flow_imbalance_depth_weighted": [
                row.order_flow_imbalance_depth_weighted for row in rows
            ],
            "traded_quantity": [row.signed_volume.traded_quantity for row in rows],
            "signed_quantity": [row.signed_volume.signed_quantity for row in rows],
            "unclassified_quantity": [row.signed_volume.unclassified_quantity for row in rows],
            "trade_side_rule": [row.signed_volume.rule.value for row in rows],
        }
        for level in range(DEPTH_LEVELS_PER_SIDE):
            columns[f"order_flow_imbalance_level_{level + 1}"] = [
                row.order_flow_imbalance_by_level[level] for row in rows
            ]
            columns[f"depth_imbalance_level_{level + 1}"] = [
                row.depth_imbalance_by_level[level] for row in rows
            ]
        return pa.table(columns)

    def coverage_report(self, *, instrument_limit: int | None = None) -> InstrumentCoverageReport:
        """Measured coverage of the session, for the dashboard surface (`R.08`).

        Rule fractions are over ALL emitted rows, not only rows that carried a trade. A
        quiet instrument therefore reports a high unclassified fraction, which is the
        honest reading: most transitions in this tape are quote updates with no trade,
        and a metric that hid them would overstate how much of the session is classified.
        """
        tokens = self._tape_reader.instrument_tokens(self._session_date)
        if instrument_limit is not None:
            tokens = tokens[:instrument_limit]

        rule_counts: dict[TradeSideRule, int] = dict.fromkeys(TradeSideRule, 0)
        gaps: list[int] = []
        duplicate_rows = 0
        boundaries = 0
        total_rows = 0

        for token in tokens:
            for row in self.replay_instrument(token):
                total_rows += 1
                rule_counts[row.signed_volume.rule] += 1
                gaps.append(row.gap_milliseconds)
                duplicate_rows += 1 if row.duplicate_run_length > 0 else 0
                boundaries += 1 if row.crossed_capture_run_boundary else 0

        def fraction(rule: TradeSideRule) -> float:
            return rule_counts[rule] / total_rows if total_rows else 0.0

        return InstrumentCoverageReport(
            session_date=self._session_date,
            instruments_replayed=len(tokens),
            feature_rows_emitted=total_rows,
            quote_rule_fraction=fraction(TradeSideRule.QUOTE_RULE),
            tick_rule_fraction=fraction(TradeSideRule.TICK_RULE),
            zero_tick_fraction=fraction(TradeSideRule.ZERO_TICK_INHERITED),
            unclassified_fraction=fraction(TradeSideRule.UNCLASSIFIED),
            median_gap_milliseconds=statistics.median(gaps) if gaps else 0.0,
            gap_millisecond_deciles=_deciles(gaps),
            duplicate_row_fraction=duplicate_rows / total_rows if total_rows else 0.0,
            capture_run_boundaries_crossed=boundaries,
        )
