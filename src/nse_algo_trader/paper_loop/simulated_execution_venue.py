"""Filling a paper order against the book that was actually there (`F04`, `A.108` decision 2).

Specification: `docs/research/228_paper_trading_loop_and_simulated_venue_spec.md` §4.

Every other part of the paper path is the real one — `F02`'s intent journal, its lifecycle state
machine, its reconciler. Only this is simulated, and it is the piece that decides whether the paper
record is worth anything: **a venue that fills at the touch teaches the system that slippage does
not exist**, and every paper P&L would then be optimistic by the full spread plus all impact,
against precisely the `F01` cost model that graduation reads.

**It fills against the RECORDED ladder, not a slippage formula.** `zipline`'s
`VolumeShareSlippage(volume_limit, price_impact)` and `backtrader`'s `slip_perc`/`filler` — both
already installed here and both introspected before this was written (`docs/research/228` §5) — are
bar-based: they infer impact from volume because they have no book. This system HAS the book, 1.3 GB
of recorded L2 depth, so the average fill price is the **volume-weighted walk of the real ladder**.
That walk IS the spread and IS the impact, measured rather than modelled.

**What the walk means, and what it does not.** The ladder is a snapshot at one instant. Walking it
answers "what would this order have paid against the resting size that was there", which is the
honest question for a marketable order. It does NOT model the queue that forms after the snapshot,
other participants reacting, or a hidden iceberg refilling a level — so a fill here is an estimate
bounded by real depth rather than a claim about what the exchange would have done. Stated because
the difference matters when this record is read as evidence.

**A resting order does not fill in this snapshot, and that is the point.** A limit order placed
away from the touch joins the queue BEHIND the size already showing at its price. With a snapshot
and no trade stream, the honest answer is that it rests with a recorded `queue_ahead_quantity` and
fills nothing yet — never that it fills because the price was "touched". Optimism about queue
position is the single most common way a paper book flatters itself.

**No book, no fill.** An instrument with no recorded depth at the decision instant is REFUSED. It is
not filled at the last close, and it is not filled at the reference price: both would be inventing a
counterparty. `L1.09`'s lesson, one layer out.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel, DepthPacket

PAISE_PER_RUPEE = Decimal(100)


class SimulatedVenueError(Exception):
    """The order cannot be simulated, and any fill produced would be an invention."""


class PaperOrderSide(Enum):
    """Which side of the book this order takes from."""

    BUY = "BUY"
    SELL = "SELL"


class PaperFillOutcome(Enum):
    """What the venue did with the order."""

    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    RESTING = "RESTING"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class LevelConsumption:
    """One price level this order ate, and how much of it."""

    price_paise: int
    quantity: int

    @property
    def value_paise(self) -> int:
        return self.price_paise * self.quantity


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """What the recorded book would have given this order, and the walk that produced it."""

    outcome: PaperFillOutcome
    filled_quantity: int
    requested_quantity: int
    average_price_paise: Decimal | None
    levels_consumed: tuple[LevelConsumption, ...]
    queue_ahead_quantity: int
    reference_price_paise: int | None
    refusal_reason: str | None = None

    @property
    def unfilled_quantity(self) -> int:
        return self.requested_quantity - self.filled_quantity

    @property
    def slippage_paise(self) -> Decimal | None:
        """How far the average fill sat from the touch — the spread and impact this order paid.

        `None` when nothing filled or there was no touch to measure from. Never zero-by-default: a
        slippage of zero is a claim that the order paid the best price, and that claim must be
        earned by a walk that stopped at the first level.
        """
        if self.average_price_paise is None or self.reference_price_paise is None:
            return None
        return self.average_price_paise - Decimal(self.reference_price_paise)

    def describe(self) -> str:
        if self.outcome is PaperFillOutcome.REFUSED:
            return f"REFUSED: {self.refusal_reason}"
        if self.outcome is PaperFillOutcome.RESTING:
            return (
                f"RESTING: {self.requested_quantity} behind {self.queue_ahead_quantity} already "
                "showing at this price"
            )
        walk = " + ".join(f"{level.quantity}@{level.price_paise}" for level in self.levels_consumed)
        slip = self.slippage_paise
        return (
            f"{self.outcome.value}: {self.filled_quantity}/{self.requested_quantity} at average "
            f"{self.average_price_paise} paise ({walk})"
            + (f", {slip} paise from the touch" if slip is not None else "")
        )


@dataclass(frozen=True, slots=True)
class SimulatedExecutionVenue:
    """Fills against a recorded `DepthPacket`. Holds no state — the book is the state."""

    def fill_market_order(
        self, *, book: DepthPacket, side: PaperOrderSide, quantity: int
    ) -> SimulatedFill:
        """Walk the real ladder until the order is filled or the visible depth runs out.

        A marketable order takes liquidity, so it pays progressively worse prices as it consumes
        levels. That progression is the impact, and it is read off the book rather than estimated
        from a volume share.
        """
        _require_positive_quantity(quantity)
        ladder = self._ladder_for(book, side)
        if not ladder:
            return self._refused(
                quantity,
                f"instrument {book.instrument_token} shows no "
                f"{'ask' if side is PaperOrderSide.BUY else 'bid'} depth at this instant; "
                "filling it would be inventing a counterparty",
            )
        return self._walk(ladder, quantity=quantity, limit_price_paise=None)

    def fill_limit_order(
        self,
        *,
        book: DepthPacket,
        side: PaperOrderSide,
        quantity: int,
        limit_price_paise: int,
    ) -> SimulatedFill:
        """Marketable limits walk the ladder; the rest REST behind the size already there.

        The distinction is the whole honesty of this module. A limit at or through the touch takes
        liquidity and is filled by the same walk a market order gets, stopped at the limit. A limit
        away from the touch joins a queue, and with a snapshot rather than a trade stream the only
        truthful answer is that it has not filled yet.
        """
        _require_positive_quantity(quantity)
        if limit_price_paise <= 0:
            raise SimulatedVenueError(f"a limit price of {limit_price_paise} paise is not a price")
        ladder = self._ladder_for(book, side)
        if not ladder:
            return self._refused(
                quantity,
                f"instrument {book.instrument_token} shows no depth on the side this order takes "
                "from; a fill here would be an invented counterparty",
            )
        touch = ladder[0].price_paise
        marketable = (
            limit_price_paise >= touch if side is PaperOrderSide.BUY else limit_price_paise <= touch
        )
        if marketable:
            return self._walk(ladder, quantity=quantity, limit_price_paise=limit_price_paise)
        return SimulatedFill(
            outcome=PaperFillOutcome.RESTING,
            filled_quantity=0,
            requested_quantity=quantity,
            average_price_paise=None,
            levels_consumed=(),
            queue_ahead_quantity=self._queue_ahead(book, side, limit_price_paise),
            reference_price_paise=touch,
        )

    @staticmethod
    def _ladder_for(book: DepthPacket, side: PaperOrderSide) -> tuple[DepthLevel, ...]:
        """The side an order TAKES from — a buy consumes asks, a sell consumes bids."""
        return book.asks if side is PaperOrderSide.BUY else book.bids

    @staticmethod
    def _queue_ahead(book: DepthPacket, side: PaperOrderSide, price_paise: int) -> int:
        """Size already showing at this price on the side this order would JOIN.

        A resting buy joins the bids. Everything already there is ahead of it, because this order
        arrived last — price-time priority, and the time is now.
        """
        resting_side = book.bids if side is PaperOrderSide.BUY else book.asks
        return sum(level.quantity for level in resting_side if level.price_paise == price_paise)

    @staticmethod
    def _walk(
        ladder: tuple[DepthLevel, ...], *, quantity: int, limit_price_paise: int | None
    ) -> SimulatedFill:
        """Consume levels in book order until filled, out of depth, or past the limit."""
        remaining = quantity
        consumed: list[LevelConsumption] = []
        for level in ladder:
            if remaining <= 0:
                break
            if limit_price_paise is not None:
                through = (
                    level.price_paise > limit_price_paise
                    if ladder[0].price_paise <= ladder[-1].price_paise
                    else level.price_paise < limit_price_paise
                )
                if through:
                    break
            taken = min(remaining, level.quantity)
            if taken <= 0:
                continue
            consumed.append(LevelConsumption(price_paise=level.price_paise, quantity=taken))
            remaining -= taken

        filled = sum(level.quantity for level in consumed)
        if filled == 0:
            return SimulatedFill(
                outcome=PaperFillOutcome.RESTING,
                filled_quantity=0,
                requested_quantity=quantity,
                average_price_paise=None,
                levels_consumed=(),
                queue_ahead_quantity=0,
                reference_price_paise=ladder[0].price_paise,
            )
        value = sum(level.value_paise for level in consumed)
        return SimulatedFill(
            outcome=(
                PaperFillOutcome.FILLED if filled == quantity else PaperFillOutcome.PARTIALLY_FILLED
            ),
            filled_quantity=filled,
            requested_quantity=quantity,
            average_price_paise=Decimal(value) / Decimal(filled),
            levels_consumed=tuple(consumed),
            queue_ahead_quantity=0,
            reference_price_paise=ladder[0].price_paise,
        )

    @staticmethod
    def _refused(quantity: int, reason: str) -> SimulatedFill:
        return SimulatedFill(
            outcome=PaperFillOutcome.REFUSED,
            filled_quantity=0,
            requested_quantity=quantity,
            average_price_paise=None,
            levels_consumed=(),
            queue_ahead_quantity=0,
            reference_price_paise=None,
            refusal_reason=reason,
        )


def _require_positive_quantity(quantity: int) -> None:
    if quantity <= 0:
        raise SimulatedVenueError(
            f"a quantity of {quantity} is not an order; the sizer's zero is a refusal and must "
            "never reach a venue"
        )
