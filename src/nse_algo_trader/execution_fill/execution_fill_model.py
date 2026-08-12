"""The two terms combined: what price this order should expect, and how sure that is.

    expected cost = spread crossed + impact of own size

The terms come from different evidence and fail differently, which is why they are computed by
different modules and only joined here. The spread is OBSERVED — read off the book, exact, no
estimation, no pooling. The impact is ESTIMATED — anchored to the book-walk at visible depth and
extrapolated beyond it, with an interval that widens as it goes.

**The output is an interval, and callers are expected to use the wide end.** A point estimate
of execution cost is a fiction the moment an order exceeds the visible book, which measured on
this tape is most orders of any size: 82.4% of snapshots exhaust five levels at one-thousandth
of session volume. `L1.02`'s gate should refuse on `upper_cost_bps` and size on `point_cost_bps`,
because being wrong about cost in the cheap direction is how a losing strategy looks profitable.

**Relationship to `L1.01`.** That engine prices what is written in circulars — statutory and
brokerage charges, exact and knowable. This prices what is written in the order book. Neither is
the cost of trading; their sum is. A hurdle built from `L1.01` alone is a FLOOR, and on small
cash trades it is a badly misleading one, because the spread term frequently dominates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nse_algo_trader.execution_fill.instrument_liquidity_buckets import LiquidityBucket
from nse_algo_trader.execution_fill.market_impact_estimator import (
    PLAUSIBLE_EXPONENT_RANGE,
    ImpactEstimate,
    ImpactEstimateMaturity,
    MarketImpactError,
    estimate_impact_from_walk,
)
from nse_algo_trader.execution_fill.order_book_walk_calculator import (
    OrderBookWalk,
    OrderBookWalkError,
    walk_order_book,
)
from nse_algo_trader.execution_fill.quoted_spread_observer import (
    QuotedSpreadError,
    QuotedSpreadObservation,
    observe_quoted_spread,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import BookSnapshot
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

_BASIS_POINTS = Decimal(10_000)


class ExecutionFillError(Exception):
    """This order cannot be priced against this book."""


@dataclass(frozen=True, slots=True)
class ExpectedFill:
    """What an order should expect to execute at, as an interval, with its provenance."""

    instrument_token: int
    side: TradeLeg
    quantity: int
    decision_mid_paise: Decimal
    spread: QuotedSpreadObservation
    impact: ImpactEstimate
    visible_walk: OrderBookWalk

    @property
    def spread_cost_bps(self) -> Decimal:
        """Half the quoted spread — what crossing costs against the mid, before size."""
        return self.spread.half_spread_bps

    @property
    def point_cost_bps(self) -> Decimal:
        return self.spread_cost_bps + self.impact.point_bps

    @property
    def lower_cost_bps(self) -> Decimal:
        return self.spread_cost_bps + self.impact.lower_bps

    @property
    def upper_cost_bps(self) -> Decimal:
        """The end a gate should refuse on.

        Being wrong about execution cost in the cheap direction is how a strategy that loses
        money looks profitable in a backtest, so the pessimistic end is the decision-relevant
        one. The optimistic end exists to show how wide the uncertainty is, not to be used.
        """
        return self.spread_cost_bps + self.impact.upper_bps

    @property
    def cost_interval_width_bps(self) -> Decimal:
        return self.upper_cost_bps - self.lower_cost_bps

    @property
    def is_censored(self) -> bool:
        """True when the order exceeds the visible book, so the impact term is extrapolated."""
        return self.impact.is_extrapolated

    @property
    def maturity(self) -> ImpactEstimateMaturity:
        return self.impact.maturity

    def _signed_cost_paise(self, cost_bps: Decimal) -> Decimal:
        """A cost moves the price against the trader: up when buying, down when selling."""
        move = self.decision_mid_paise * cost_bps / _BASIS_POINTS
        return move if self.side is TradeLeg.BUY else -move

    @property
    def expected_price_paise(self) -> Decimal:
        return self.decision_mid_paise + self._signed_cost_paise(self.point_cost_bps)

    @property
    def optimistic_price_paise(self) -> Decimal:
        return self.decision_mid_paise + self._signed_cost_paise(self.lower_cost_bps)

    @property
    def pessimistic_price_paise(self) -> Decimal:
        return self.decision_mid_paise + self._signed_cost_paise(self.upper_cost_bps)

    @property
    def expected_slippage_paise(self) -> Decimal:
        """Total rupees-in-paise given up against the decision mid, at the point estimate."""
        return abs(self.expected_price_paise - self.decision_mid_paise) * Decimal(self.quantity)


class ExecutionFillModel:
    """Prices an order against a real book snapshot.

    Stateless per call by design: the state that matters — accrued fills, fitted exponents,
    shrinkage weights — belongs to `execution_fill_parameter_store`, and passing it in rather
    than hiding it in the model is what lets a backtest replay a past parameterisation instead
    of silently using today's.
    """

    def __init__(
        self,
        *,
        exponent_range: tuple[Decimal, Decimal] = PLAUSIBLE_EXPONENT_RANGE,
        bucket: LiquidityBucket | None = None,
        maturity: ImpactEstimateMaturity = ImpactEstimateMaturity.ANCHORED_PRIOR,
        observation_count: int = 0,
    ) -> None:
        self._exponent_range = exponent_range
        self._bucket = bucket
        self._maturity = maturity
        self._observation_count = observation_count

    def price_fill(
        self, snapshot: BookSnapshot, side: TradeLeg, quantity: int
    ) -> ExpectedFill:
        """The expected execution of `quantity` on `side`, against this book.

        Raises:
            ExecutionFillError: the book cannot support the question — crossed, one-sided, or
                with no populated level on the side being taken. Refused rather than answered,
                because every fallback available here (use the last good book, use the mid,
                assume a typical spread) produces a number that looks ordinary and is not.
        """
        if quantity <= 0:
            raise ExecutionFillError(f"quantity must be positive, got {quantity}")
        try:
            spread = observe_quoted_spread(snapshot)
            visible_quantity = _visible_quantity_on(snapshot, side)
            if visible_quantity <= 0:
                raise ExecutionFillError(
                    f"instrument {snapshot.instrument_token} has no visible depth on the "
                    f"{side.value} side; there is nothing to anchor an impact estimate to"
                )
            anchor = walk_order_book(snapshot, side, visible_quantity)
            impact = estimate_impact_from_walk(
                anchor,
                quantity,
                bucket=self._bucket,
                exponent_range=self._exponent_range,
                maturity=self._maturity,
                observation_count=self._observation_count,
            )
        except (OrderBookWalkError, QuotedSpreadError, MarketImpactError) as error:
            raise ExecutionFillError(
                f"cannot price {quantity} of instrument {snapshot.instrument_token} at "
                f"{snapshot.receipt_time}: {error}"
            ) from error
        return ExpectedFill(
            instrument_token=snapshot.instrument_token,
            side=side,
            quantity=quantity,
            decision_mid_paise=spread.mid_paise,
            spread=spread,
            impact=impact,
            visible_walk=anchor,
        )


def _visible_quantity_on(snapshot: BookSnapshot, side: TradeLeg) -> int:
    """Total populated depth on the side a taker consumes."""
    levels = snapshot.asks if side is TradeLeg.BUY else snapshot.bids
    return sum(level.quantity for level in levels if level.price_paise > 0 and level.quantity > 0)
