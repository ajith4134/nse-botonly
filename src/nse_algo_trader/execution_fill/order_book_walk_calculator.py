"""Walking the visible ladder — and knowing exactly when the answer stops being real.

This is the mechanical half of execution cost, and it has an unusually strong precedent: it is
NSE's own definition of impact cost, used to decide index eligibility. From the Nifty indices
methodology document:

    Ideal price     = (best bid + best ask) / 2
    Actual price    = sum(quantity x price) / total quantity
    Impact Cost (%) = ((Actual - Ideal) / Ideal) x 100

with Nifty 50 requiring impact cost at or below 0.50% on a Rs 10 crore portfolio for 90% of
observations. Implementing exactly that formula means the engine can be validated against
numbers NSE publishes — an external ground truth almost no cost model has.

**The censoring is the point of this module.** A five-level book is a thin slice of the real
one: measured against the whole-book pending quantity the same feed carries, the visible ladder
is a median of **0.3%** of what is resting. So the walk runs out fast — at one-thousandth of a
session's volume, **82.4%** of snapshots exhaust five levels, and at five-thousandths, **100%**.

What makes that dangerous rather than merely limited is the SHAPE of the failure. As the walk
exhausts, its cost estimate saturates — measured 2.24 bps at 1e-3 and 2.29 bps at 5e-3, a 5x
increase in size for a 2% increase in estimated cost. A fitted exponent over 679,354 uncensored
observations comes out at **0.107**, against the square-root law's 0.5. So a naive book-walk
reports a bounded, comfortable number at exactly the sizes where the true cost is running away
from it. It does not fail loudly; it lies quietly.

Every result therefore carries whether it is censored, and a censored result is a LOWER BOUND
that must never be extrapolated. The estimate above that boundary comes from
`market_impact_estimator`, which is a different calculation on different evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nse_algo_trader.market_depth.depth_tape_schema import DepthLevel
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

_BASIS_POINTS = Decimal(10_000)
_PERCENT = Decimal(100)


class OrderBookWalkError(Exception):
    """The book cannot support a walk, and guessing past it would invent liquidity."""


class UnusableBookError(OrderBookWalkError):
    """The book is crossed, one-sided or empty at the touch.

    Refused rather than worked around. A crossed book means the snapshot is not a coherent
    picture of the market at one instant, and the mid computed from it is not a price anything
    could have traded at.
    """


@dataclass(frozen=True, slots=True)
class LevelConsumption:
    """One rung of the ladder, and how much of it this order would take."""

    price_paise: int
    available_quantity: int
    consumed_quantity: int

    @property
    def is_exhausted(self) -> bool:
        return self.consumed_quantity >= self.available_quantity

    @property
    def notional_paise(self) -> Decimal:
        return Decimal(self.price_paise) * Decimal(self.consumed_quantity)


@dataclass(frozen=True, slots=True)
class OrderBookWalk:
    """What walking the visible ladder for a given size costs, and whether that is the truth."""

    side: TradeLeg
    requested_quantity: int
    filled_quantity: int
    reference_mid_paise: Decimal
    consumptions: tuple[LevelConsumption, ...]
    visible_quantity: int
    whole_book_quantity: int

    @property
    def is_censored(self) -> bool:
        """True when the visible book could not fill the order.

        The single most important field this module produces. A censored walk understates by
        an unbounded amount, and its cost estimate saturates rather than growing — so a caller
        that ignores this reads "cheap" precisely when the answer is "unknown, and probably
        much worse".
        """
        return self.filled_quantity < self.requested_quantity

    @property
    def filled_notional_paise(self) -> Decimal:
        return sum((entry.notional_paise for entry in self.consumptions), Decimal(0))

    @property
    def average_fill_price_paise(self) -> Decimal:
        """NSE's "actual price" — the quantity-weighted average over the consumed levels."""
        if self.filled_quantity == 0:
            raise OrderBookWalkError(
                "an unfilled walk has no average price; check `filled_quantity` before asking"
            )
        return self.filled_notional_paise / Decimal(self.filled_quantity)

    @property
    def impact_cost_percent(self) -> Decimal:
        """NSE's own impact-cost figure, in percent, on the quantity that actually filled.

        Signed so that a cost is positive on both sides: buying above the mid and selling
        below it are both adverse.
        """
        if self.reference_mid_paise <= 0:
            raise UnusableBookError("impact cost against a non-positive mid is undefined")
        deviation = self.average_fill_price_paise - self.reference_mid_paise
        if self.side is TradeLeg.SELL:
            deviation = -deviation
        return deviation / self.reference_mid_paise * _PERCENT

    @property
    def impact_cost_bps(self) -> Decimal:
        return self.impact_cost_percent / _PERCENT * _BASIS_POINTS

    @property
    def visible_fraction_of_book(self) -> Decimal | None:
        """How much of the resting book this walk could even see.

        Measured median across the real universe: 0.003. A model that treats the visible
        ladder as "the book" is reasoning about a third of a percent of it.
        """
        if self.whole_book_quantity <= 0:
            return None
        return Decimal(self.visible_quantity) / Decimal(self.whole_book_quantity)

    @property
    def participation_of_visible(self) -> Decimal:
        if self.visible_quantity <= 0:
            raise UnusableBookError("no visible quantity on this side")
        return Decimal(self.requested_quantity) / Decimal(self.visible_quantity)


def _levels_for(snapshot: BookSnapshot, side: TradeLeg) -> tuple[DepthLevel, ...]:
    """The ladder a taker on `side` consumes: a buyer lifts asks, a seller hits bids."""
    return snapshot.asks if side is TradeLeg.BUY else snapshot.bids


def _whole_book_quantity_for(snapshot: BookSnapshot, side: TradeLeg) -> int:
    """NSE publishes total resting quantity per side, and almost nobody uses it.

    It is the scale variable the impact estimate needs: visible depth is 0.3% of it, so
    participation measured against the visible ladder is off by more than two orders of
    magnitude from participation against the real book.
    """
    return snapshot.total_sell_quantity if side is TradeLeg.BUY else snapshot.total_buy_quantity


def walk_order_book(
    snapshot: BookSnapshot, side: TradeLeg, quantity: int
) -> OrderBookWalk:
    """Consume the visible ladder for `quantity` units, stopping when it runs out.

    Raises:
        UnusableBookError: the book is crossed, or has no touch on the side being taken.
            Both mean the snapshot cannot answer the question; pricing against a crossed book
            would produce a negative cost, which reads as free money.
        OrderBookWalkError: the quantity is not positive.
    """
    if quantity <= 0:
        raise OrderBookWalkError(f"quantity must be positive, got {quantity}")
    best_bid = snapshot.best_bid_paise
    best_ask = snapshot.best_ask_paise
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        raise UnusableBookError(
            f"instrument {snapshot.instrument_token} has no usable touch at "
            f"{snapshot.receipt_time}: bid={best_bid} ask={best_ask}"
        )
    if best_bid >= best_ask:
        raise UnusableBookError(
            f"instrument {snapshot.instrument_token} is crossed at {snapshot.receipt_time} "
            f"(bid {best_bid} >= ask {best_ask}); a mid taken from a crossed book is not a "
            f"price anything could have traded at"
        )

    remaining = quantity
    consumptions: list[LevelConsumption] = []
    visible = 0
    for level in _levels_for(snapshot, side):
        if level.price_paise <= 0 or level.quantity <= 0:
            # A padded or empty rung. Real books arrive with fewer than five populated
            # levels routinely; treating a zero-priced pad as a rung would fill the order
            # for nothing.
            continue
        visible += level.quantity
        if remaining <= 0:
            continue
        taken = min(remaining, level.quantity)
        remaining -= taken
        consumptions.append(
            LevelConsumption(
                price_paise=level.price_paise,
                available_quantity=level.quantity,
                consumed_quantity=taken,
            )
        )
    if not consumptions:
        raise UnusableBookError(
            f"instrument {snapshot.instrument_token} has no populated levels on the "
            f"{side.value} side at {snapshot.receipt_time}"
        )
    mid = snapshot.mid_paise
    if mid is None:
        raise UnusableBookError("no mid price available")
    return OrderBookWalk(
        side=side,
        requested_quantity=quantity,
        filled_quantity=quantity - remaining,
        reference_mid_paise=Decimal(mid),
        consumptions=tuple(consumptions),
        visible_quantity=visible,
        whole_book_quantity=_whole_book_quantity_for(snapshot, side),
    )


def quantity_for_notional(notional_paise: Decimal, reference_price_paise: Decimal) -> int:
    """Units that a rupee amount buys — how NSE's Rs 10 crore criterion is applied.

    Floored: a partial unit cannot be traded, and rounding up would walk one rung further
    into the book than the money reaches.
    """
    if reference_price_paise <= 0:
        raise OrderBookWalkError(
            f"reference price must be positive, got {reference_price_paise}"
        )
    return int(notional_paise / reference_price_paise)
