"""The spread term — observed, not estimated, and deliberately not pooled.

Two measured findings shape this module, and both cut against what the literature would
suggest (`docs/research/220` §2.2):

**1. On NSE, effective spread is the quoted spread.** Measured ratio 1.000 / 1.000 / 0.952 on
three instruments recovered from real prints: trades sit at the bid or the ask (55% / 43%) and
only **2.0%** land strictly inside. There is no meaningful price improvement to model for a
taker, so the cost of crossing is simply half the quoted spread — a number the book states
outright, requiring no estimator at all.

**2. Shrinkage makes the spread estimate WORSE, at every sample size tested.** Splitting a real
session and capping observations per instrument, per-instrument estimates beat pooled ones down
to five observations (RMSE 7.328 vs 9.462 at n=5), with a median shrinkage weight of 0.99.
Between-instrument variance dominates measurement variance so heavily that pooling only injects
bias. Spread is a high-signal, low-noise quantity that one observation nearly pins.

So this module holds no hierarchy and no prior. Pooling belongs on the impact coefficient,
where per-instrument observations really will be near zero — and it lives there, not here.

The mid is reported alongside the **micro-price**, because a size-imbalanced book means the
arithmetic mid is not the fair value: a bid stacked ten deep against a thin ask says the next
trade is more likely to happen above the mid than below it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    micro_price_paise,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

_BASIS_POINTS = Decimal(10_000)
_HALF = Decimal("0.5")


class QuotedSpreadError(Exception):
    """The book cannot state a spread, and inventing one would understate every cost."""


@dataclass(frozen=True, slots=True)
class QuotedSpreadObservation:
    """One snapshot's spread, and the two reference prices a caller might cross against."""

    instrument_token: int
    best_bid_paise: int
    best_ask_paise: int
    mid_paise: Decimal
    micro_price_paise: Decimal | None

    @property
    def spread_paise(self) -> int:
        return self.best_ask_paise - self.best_bid_paise

    @property
    def spread_bps(self) -> Decimal:
        return Decimal(self.spread_paise) / self.mid_paise * _BASIS_POINTS

    @property
    def half_spread_bps(self) -> Decimal:
        """What a taker pays to cross, against the mid. The spread cost of one leg."""
        return self.spread_bps * _HALF

    def crossing_price_paise(self, side: TradeLeg) -> Decimal:
        """The touch a taker on `side` actually executes against."""
        return Decimal(self.best_ask_paise if side is TradeLeg.BUY else self.best_bid_paise)

    @property
    def micro_price_skew_bps(self) -> Decimal | None:
        """How far the size-weighted fair value sits from the arithmetic mid.

        Positive means the book leans towards a higher price. A caller crossing WITH the skew
        is paying more than the half-spread suggests, and against it, less — which is the
        difference between a queue that wants your order and one that does not.
        """
        if self.micro_price_paise is None:
            return None
        return (self.micro_price_paise - self.mid_paise) / self.mid_paise * _BASIS_POINTS


@dataclass(frozen=True, slots=True)
class InstrumentSpreadProfile:
    """An instrument's spread over a window, summarised by quantiles rather than a mean.

    Quantiles because spread distributions are right-skewed and spiky — a handful of
    auction-adjacent or news-driven snapshots have spreads orders of magnitude wider than the
    body, and a mean lets those decide the number. The median is what a typical order meets;
    the upper quantile is what a badly-timed one meets, and a cost model that reports only the
    first is optimistic exactly when it matters.
    """

    instrument_token: int
    observation_count: int
    median_spread_bps: Decimal
    upper_quantile_spread_bps: Decimal
    upper_quantile: Decimal
    minimum_spread_bps: Decimal
    maximum_spread_bps: Decimal

    @property
    def median_half_spread_bps(self) -> Decimal:
        return self.median_spread_bps * _HALF

    @property
    def upper_quantile_half_spread_bps(self) -> Decimal:
        return self.upper_quantile_spread_bps * _HALF


def observe_quoted_spread(snapshot: BookSnapshot) -> QuotedSpreadObservation:
    """Read the spread off one snapshot, or refuse.

    Raises:
        QuotedSpreadError: the book has no touch on one side, or is crossed. A crossed book
            yields a negative spread, which would enter a cost model as a rebate.
    """
    best_bid = snapshot.best_bid_paise
    best_ask = snapshot.best_ask_paise
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        raise QuotedSpreadError(
            f"instrument {snapshot.instrument_token} has no two-sided touch at "
            f"{snapshot.receipt_time}"
        )
    if best_bid >= best_ask:
        raise QuotedSpreadError(
            f"instrument {snapshot.instrument_token} is crossed at {snapshot.receipt_time} "
            f"(bid {best_bid} >= ask {best_ask}); a negative spread would read as a rebate"
        )
    mid = snapshot.mid_paise
    if mid is None or mid <= 0:
        raise QuotedSpreadError(f"instrument {snapshot.instrument_token} has no usable mid")
    micro = micro_price_paise(
        best_bid_paise=best_bid,
        best_bid_quantity=snapshot.best_bid_quantity,
        best_ask_paise=best_ask,
        best_ask_quantity=snapshot.best_ask_quantity,
    )
    return QuotedSpreadObservation(
        instrument_token=snapshot.instrument_token,
        best_bid_paise=best_bid,
        best_ask_paise=best_ask,
        mid_paise=Decimal(mid),
        micro_price_paise=None if micro is None else Decimal(micro),
    )


def _quantile(sorted_values: Sequence[Decimal], quantile: Decimal) -> Decimal:
    """Nearest-rank quantile. No interpolation between two observed spreads.

    Interpolating would report a spread that was never quoted; the nearest-rank value always
    was. With hundreds of thousands of snapshots per instrument the difference is immaterial
    numerically and material to the claim being made.
    """
    if not sorted_values:
        raise QuotedSpreadError("no observations to take a quantile of")
    if not 0 < quantile <= 1:
        raise QuotedSpreadError(f"quantile must be in (0, 1], got {quantile}")
    rank = int((Decimal(len(sorted_values)) * quantile).to_integral_value(rounding="ROUND_CEILING"))
    return sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))]


def build_instrument_spread_profile(
    instrument_token: int,
    snapshots: Iterable[BookSnapshot],
    *,
    upper_quantile: Decimal = Decimal("0.75"),
) -> InstrumentSpreadProfile:
    """Summarise an instrument's spread over whatever snapshots it has.

    Unusable snapshots are SKIPPED rather than failing the profile — crossed and one-sided
    books are a normal minority of a real tape (measured 0.7% of one session), and refusing
    the whole instrument because of them would discard the other 99.3%. A profile built from
    nothing, however, is refused: an instrument with no two-sided quote has no spread, and
    reporting zero would make it look like the cheapest thing in the universe.
    """
    spreads: list[Decimal] = []
    for snapshot in snapshots:
        try:
            spreads.append(observe_quoted_spread(snapshot).spread_bps)
        except QuotedSpreadError:
            continue
    if not spreads:
        raise QuotedSpreadError(
            f"instrument {instrument_token} never quoted a two-sided book; it has no spread, "
            f"and a spread of zero would make it look like the cheapest instrument traded"
        )
    spreads.sort()
    return InstrumentSpreadProfile(
        instrument_token=instrument_token,
        observation_count=len(spreads),
        median_spread_bps=median(spreads),
        upper_quantile_spread_bps=_quantile(spreads, upper_quantile),
        upper_quantile=upper_quantile,
        minimum_spread_bps=spreads[0],
        maximum_spread_bps=spreads[-1],
    )
