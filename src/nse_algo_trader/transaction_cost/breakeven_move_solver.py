"""How far the price must move before a trade is worth having done.

The question looks like division and is not. Cost depends on the exit price — sell-side STT,
the exchange charge and the percentage leg of brokerage all scale with it — and the exit price
is what we are solving for, so the equation is self-referential:

    quantity x (exit - entry) = cost(entry) + cost(exit)

Every ad-valorem levy is linear in the exit price, so on any one brokerage piece this reduces
to a closed form. What stops it being a one-liner is the per-order cap: `min(0.03%, Rs 20)`
makes the cost function piecewise-linear with a kink, and Angel One's floor adds a second one.
The standard method for that is to solve on each piece and keep the root that lands inside its
OWN piece, which is exact rather than tolerance-bounded — and it is the only method that
notices when the schedule is malformed, because a malformed schedule produces either no
admissible root or two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, getcontext

from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    LegCostFunction,
    LegCostPiece,
    NseTransactionCostEngine,
    TradeSpecification,
    TransactionCostError,
)

_BASIS_POINTS = Decimal(10_000)


class BreakevenSolveError(TransactionCostError):
    """The breakeven price is not uniquely determined.

    Raised rather than picking a root. No admissible root means the levies consume the move
    faster than the move produces it — real, and it happens for tiny quantities where a flat
    Rs 20 dominates. Two admissible roots means the fee schedule's pieces overlap or contradict,
    which is a defect in the data, and silently choosing one is how that defect would survive.
    """


@dataclass(frozen=True, slots=True)
class BreakevenMove:
    """The exit price at which a round trip nets exactly zero, and what it implies."""

    entry_price_paise: Decimal
    breakeven_price_paise: Decimal
    move_paise: Decimal
    round_trip_cost_paise: Decimal
    is_exact: bool
    residual_paise: Decimal
    rounding_note: str

    @property
    def is_within_arithmetic_noise(self) -> bool:
        """Whether the residual is small enough to be `Decimal`'s precision and nothing else.

        The closed form is exact over the rationals; `Decimal` evaluates it at the context's
        significant digits, so a solve that is algebraically perfect still leaves a residual
        around the twentieth significant figure. The tolerance is DERIVED from the context
        precision and the magnitude of the trade rather than written down, so it cannot
        quietly become wide enough to hide a real error.
        """
        tolerance = abs(self.round_trip_cost_paise) * Decimal(10) ** (-getcontext().prec + 6)
        return abs(self.residual_paise) <= tolerance

    @property
    def move_bps(self) -> Decimal:
        if self.entry_price_paise == 0:
            raise BreakevenSolveError("a breakeven move on a zero entry price is undefined")
        return self.move_paise / self.entry_price_paise * _BASIS_POINTS

    def move_in_ticks(self, tick_size_paise: Decimal) -> Decimal:
        """The move expressed in the instrument's REAL tick, not in rupees.

        A breakeven of 1.4 ticks and one of 0.6 ticks are different kinds of problem: the
        first cannot be earned by a one-tick scalp at all. Rupees hide that; ticks do not.
        """
        if tick_size_paise <= 0:
            raise BreakevenSolveError(f"tick size must be positive, got {tick_size_paise}")
        return self.move_paise / tick_size_paise


def _root_on_piece(
    piece: LegCostPiece,
    quantity: Decimal,
    entry_price_paise: Decimal,
    entry_cost_paise: Decimal,
    *,
    is_short_first: bool,
) -> Decimal | None:
    """Solve the linear equation on one piece, or report that it has no root there.

    Long:  q(x - p) = C + a.x + b   ->   x = (q.p + C + b) / (q - a)
    Short: q(p - x) = C + a.x + b   ->   x = (q.p - C - b) / (q + a)

    where `a` is the piece's slope, `b` its intercept and `C` the already-known entry-leg cost.
    """
    numerator_shift = entry_cost_paise + piece.intercept_paise
    if is_short_first:
        denominator = quantity + piece.slope
        if denominator == 0:
            return None
        candidate = (quantity * entry_price_paise - numerator_shift) / denominator
    else:
        denominator = quantity - piece.slope
        if denominator <= 0:
            # The levies take at least as much as the move produces: no exit price on this
            # piece breaks even, however far the price runs.
            return None
        candidate = (quantity * entry_price_paise + numerator_shift) / denominator
    if candidate < 0:
        return None
    return candidate if piece.contains(candidate) else None


def solve_breakeven_move(
    engine: NseTransactionCostEngine,
    trade: TradeSpecification,
    *,
    known_as_of: date | None = None,
) -> BreakevenMove:
    """The exit price at which this round trip nets exactly zero.

    The entry leg is priced once at the real entry price; only the exit leg is unknown.
    """
    entry_cost = engine.price_leg(
        trade, trade.entry_leg, trade.entry_price_paise, known_as_of=known_as_of
    ).exact_total_paise
    exit_function: LegCostFunction = engine.leg_cost_function(
        trade, trade.exit_leg, known_as_of=known_as_of
    )
    quantity = Decimal(trade.quantity)
    roots = [
        root
        for root in (
            _root_on_piece(
                piece,
                quantity,
                trade.entry_price_paise,
                entry_cost,
                is_short_first=trade.is_short_first,
            )
            for piece in exit_function.pieces
        )
        if root is not None
    ]
    unique_roots = sorted(set(roots))
    if not unique_roots:
        raise BreakevenSolveError(
            f"no exit price breaks even for {trade.quantity} of {trade.segment} entered at "
            f"{trade.entry_price_paise} paise: on every brokerage piece the ad-valorem levies "
            f"consume the move as fast as it is produced, or the flat charges exceed any "
            f"achievable gain"
        )
    if len(unique_roots) > 1:
        raise BreakevenSolveError(
            f"{len(unique_roots)} admissible breakeven prices ({unique_roots}) — the brokerage "
            f"pieces for {trade.segment} overlap or contradict, and choosing one would hide it"
        )
    breakeven = unique_roots[0]
    move = (
        trade.entry_price_paise - breakeven
        if trade.is_short_first
        else breakeven - trade.entry_price_paise
    )
    priced = _round_trip_cost_at(engine, trade, breakeven, known_as_of=known_as_of)
    residual = quantity * move - priced
    return BreakevenMove(
        entry_price_paise=trade.entry_price_paise,
        breakeven_price_paise=breakeven,
        move_paise=move,
        round_trip_cost_paise=priced,
        is_exact=exit_function.is_exact,
        residual_paise=residual,
        rounding_note=exit_function.rounding_note,
    )


def _round_trip_cost_at(
    engine: NseTransactionCostEngine,
    trade: TradeSpecification,
    exit_price_paise: Decimal,
    *,
    known_as_of: date | None,
) -> Decimal:
    """Price both legs with the exit at `exit_price_paise`, as the broker would BILL them.

    Deliberately the billed figure rather than the exact one: the residual it produces is the
    honest measure of what rounding does to the solve, and hiding it by comparing exact to
    exact would make every solve look perfect regardless of the broker.
    """
    entry = engine.price_leg(
        trade, trade.entry_leg, trade.entry_price_paise, known_as_of=known_as_of
    )
    exit_leg = engine.price_leg(trade, trade.exit_leg, exit_price_paise, known_as_of=known_as_of)
    return entry.total_paise + exit_leg.total_paise


def solve_breakeven_for_leg(
    engine: NseTransactionCostEngine,
    trade: TradeSpecification,
    leg: TradeLeg,
    *,
    known_as_of: date | None = None,
) -> Decimal:
    """The cost of ONE leg at its own price — the exit-only question a live position asks.

    A position already open does not care what entering cost; it cares what leaving costs. The
    round-trip number answers a different question and answers it larger.
    """
    price = trade.entry_price_paise if leg is trade.entry_leg else trade.exit_price_paise
    return engine.price_leg(trade, leg, price, known_as_of=known_as_of).total_paise
