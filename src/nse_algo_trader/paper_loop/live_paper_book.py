"""The intraday paper book the six bots trade into while the market is open (`L5.30`, `A.146`).

**What was missing.** The continuous loop counted proposals and stopped there: it imported no venue,
no ledger and no track record, so a tick that produced 376 proposals produced zero positions, zero
fills and zero P&L. `/paper-session` could only ever show the last CLOSED session, which is the
half of the operator's original report — *"the prices are stuck"* — that the daily-run timeout fix
did not touch.

**What this owns.** Open positions across ticks, marked to the live tape, squared off before the
close (`R.01` makes square-off the failure mode, not an afterthought), priced both legs through
`NseTransactionCostEngine`, and accrued to `PaperTrackRecordStore` under each bot's own identity so
the maturity ladder reads live evidence and replayed evidence through the same door.

**State survives the process.** Open positions are persisted on every change. A trading loop that
forgets its book when systemd restarts it does not have a book — it has a sequence of unrelated
entries, and the positions it opened before the restart are never squared off by anyone.

**The fill model, stated rather than implied (`R.11`).** Entries and exits are priced at the tape's
last traded price for that instrument at that instant. That is a REAL observed price and not a
fabricated mid, but it is weaker than what `SimulatedExecutionVenue` does on the replay path, where
a fill walks the recorded L2 ladder and a resting order carries a queue position. The gap is
recorded as `B47` rather than papered over: an intraday book filled at last-traded overstates
fills in exactly the instruments where the book is thin.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.paper_loop.bot_maturity_ladder import (
    ClosedPaperTrade,
    PaperTrackRecordStore,
)
from nse_algo_trader.portfolio.portfolio_proposal_supervisor import (
    AdmittedProposal,
    PortfolioPlan,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

DEFAULT_BOOK_DATABASE: Final = Path.home() / ".nse_algo_trader" / "live_paper_book.sqlite3"

PAISE_PER_RUPEE: Final = Decimal("100")


class LivePaperBookError(Exception):
    """The book cannot be opened or is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class OpenPaperPosition:
    """One position the book is carrying, and everything needed to close it without the signal."""

    bot_identity: str
    instrument_token: int
    trading_symbol: str
    segment: ChargeableSegment
    side: TradeLeg
    quantity: int
    entry_price_paise: Decimal
    opened_at: datetime
    session_date: date
    stated_win_probability: float | None = None

    @property
    def position_key(self) -> str:
        return f"{self.bot_identity}:{self.instrument_token}:{self.session_date.isoformat()}"

    @property
    def notional_rupees(self) -> Decimal:
        return self.entry_price_paise * Decimal(self.quantity) / PAISE_PER_RUPEE

    @property
    def signed_notional_rupees(self) -> Decimal:
        sign = Decimal(1) if self.side is TradeLeg.BUY else Decimal(-1)
        return sign * self.notional_rupees

    def gross_rupees_at(self, price_paise: Decimal) -> Decimal:
        """Mark-to-market gross, in rupees, at a given price. Signed by the side."""
        move = price_paise - self.entry_price_paise
        if self.side is TradeLeg.SELL:
            move = -move
        return move * Decimal(self.quantity) / PAISE_PER_RUPEE


@dataclass(frozen=True)
class BookMark:
    """What the book is worth right now, per bot and in total."""

    marked_at: datetime
    open_positions: int
    gross_unrealised_rupees: Decimal
    unrealised_by_identity: Mapping[str, Decimal]
    net_exposure_rupees: Decimal
    unpriced_positions: int = 0
    """Positions the tape could not price at this instant. Counted, never treated as zero — a
    position marked at zero move reads as flat when it is simply unobserved."""

    def describe(self) -> str:
        return (
            f"{self.open_positions} open · unrealised Rs {self.gross_unrealised_rupees:,.0f} · "
            f"net exposure Rs {self.net_exposure_rupees:,.0f}"
            + (f" · {self.unpriced_positions} unpriced" if self.unpriced_positions else "")
        )


@dataclass(frozen=True)
class SquareOffOutcome:
    """What closing the book produced. Costs are per trade, never netted at session level."""

    closed: int
    accrued: int
    gross_rupees: Decimal
    costs_rupees: Decimal
    unclosable: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def net_rupees(self) -> Decimal:
        return self.gross_rupees - self.costs_rupees

    def describe(self) -> str:
        line = (
            f"{self.closed} closed · gross Rs {self.gross_rupees:,.2f} less costs "
            f"Rs {self.costs_rupees:,.2f} = net Rs {self.net_rupees:,.2f} · {self.accrued} accrued"
        )
        if self.unclosable:
            line += f" · {self.unclosable} could not be priced and stay OPEN"
        return line


class LivePaperBook:
    """Carries open paper positions across ticks and closes them into the track record.

    A `costs_rupees_for` seam rather than a hard dependency on the cost engine: the engine needs a
    market-rule store and a session, and a hermetic harness must be able to drive this identical
    code without one (`R.J`). Production passes the real pricing function.
    """

    def __init__(
        self,
        *,
        track_record: PaperTrackRecordStore | None = None,
        database: Path = DEFAULT_BOOK_DATABASE,
        costs_rupees_for: object | None = None,
    ) -> None:
        self._database = Path(database)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._database)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS open_paper_position (
                position_key TEXT PRIMARY KEY,
                bot_identity TEXT NOT NULL,
                instrument_token INTEGER NOT NULL,
                trading_symbol TEXT NOT NULL,
                segment TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_price_paise TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                session_date TEXT NOT NULL,
                stated_win_probability REAL
            )
            """
        )
        self._connection.commit()
        self._track_record = track_record
        self._costs_rupees_for = costs_rupees_for

    def close(self) -> None:
        self._connection.close()

    # -- reading ---------------------------------------------------------------------------

    def open_positions(self) -> tuple[OpenPaperPosition, ...]:
        rows = self._connection.execute(
            "SELECT bot_identity, instrument_token, trading_symbol, segment, side, quantity, "
            "entry_price_paise, opened_at, session_date, stated_win_probability "
            "FROM open_paper_position ORDER BY opened_at, position_key"
        ).fetchall()
        return tuple(
            OpenPaperPosition(
                bot_identity=row[0],
                instrument_token=int(row[1]),
                trading_symbol=row[2],
                segment=ChargeableSegment(row[3]),
                side=TradeLeg(row[4]),
                quantity=int(row[5]),
                entry_price_paise=Decimal(row[6]),
                opened_at=datetime.fromisoformat(row[7]),
                session_date=date.fromisoformat(row[8]),
                stated_win_probability=row[9],
            )
            for row in rows
        )

    def net_exposure_rupees(self) -> Decimal:
        return sum(
            (position.signed_notional_rupees for position in self.open_positions()),
            Decimal("0"),
        )

    def holds(self, bot_identity: str, instrument_token: int, session_date: date) -> bool:
        key = f"{bot_identity}:{instrument_token}:{session_date.isoformat()}"
        row = self._connection.execute(
            "SELECT 1 FROM open_paper_position WHERE position_key = ?", (key,)
        ).fetchone()
        return row is not None

    # -- writing ---------------------------------------------------------------------------

    def admit(self, plan: PortfolioPlan, *, at: datetime, session_date: date) -> int:
        """Open a position for every admitted proposal the book is not already holding.

        A bot proposing the same instrument on consecutive ticks is normal — the deviation it saw
        has not closed yet. Doubling the position every five minutes because of that is not, so an
        instrument already held by that bot this session is skipped rather than added to.
        """
        opened = 0
        for admitted in plan.admitted:
            if self.holds(admitted.bot_identity, admitted.signal.instrument_token, session_date):
                continue
            position = _position_from(admitted, at=at, session_date=session_date)
            self._insert(position)
            opened += 1
        if opened:
            self._connection.commit()
        return opened

    def mark(self, prices_paise: Mapping[int, Decimal], *, at: datetime) -> BookMark:
        """Mark every open position to the tape, counting the ones the tape cannot price."""
        unrealised: dict[str, Decimal] = {}
        total = Decimal("0")
        unpriced = 0
        exposure = Decimal("0")
        positions = self.open_positions()
        for position in positions:
            exposure += position.signed_notional_rupees
            price = prices_paise.get(position.instrument_token)
            if price is None:
                unpriced += 1
                continue
            gross = position.gross_rupees_at(price)
            total += gross
            unrealised[position.bot_identity] = (
                unrealised.get(position.bot_identity, Decimal("0")) + gross
            )
        return BookMark(
            marked_at=at,
            open_positions=len(positions),
            gross_unrealised_rupees=total,
            unrealised_by_identity=unrealised,
            net_exposure_rupees=exposure,
            unpriced_positions=unpriced,
        )

    def square_off(
        self,
        prices_paise: Mapping[int, Decimal],
        *,
        at: datetime,
        close_reason: str = "square_off",
    ) -> SquareOffOutcome:
        """Close every position the tape can price, accrue it, and leave the rest OPEN.

        A position the tape cannot price is NOT closed at its entry price. Closing at entry books a
        zero P&L that reads as a flat trade, and a flat trade is evidence the ladder will use.
        """
        closed: list[ClosedPaperTrade] = []
        gross_total = Decimal("0")
        costs_total = Decimal("0")
        unclosable = 0
        notes: list[str] = []

        for position in self.open_positions():
            price = prices_paise.get(position.instrument_token)
            if price is None:
                unclosable += 1
                continue
            gross = position.gross_rupees_at(price)
            costs = self._costs_for(position, exit_price_paise=price)
            if costs is None:
                unclosable += 1
                notes.append(f"{position.trading_symbol} could not be priced for costs")
                continue
            closed.append(
                ClosedPaperTrade(
                    bot_identity=position.bot_identity,
                    session_date=position.session_date,
                    position_key=position.position_key,
                    instrument_token=position.instrument_token,
                    trading_symbol=position.trading_symbol,
                    side=str(position.side),
                    filled_quantity=position.quantity,
                    opened_at=position.opened_at,
                    closed_at=at,
                    close_reason=close_reason,
                    gross_rupees=gross,
                    costs_rupees=costs,
                    stated_win_probability=position.stated_win_probability,
                )
            )
            gross_total += gross
            costs_total += costs
            self._delete(position.position_key)

        self._connection.commit()
        accrued = 0
        if self._track_record is not None:
            # `append` is idempotent on (bot, session, position key) and returns rows written, so
            # re-squaring a book that was already accrued adds nothing rather than doubling a bot's
            # evidence — which would promote it for work it did once (`R.13`).
            accrued = sum(self._track_record.append(trade) for trade in closed)
        return SquareOffOutcome(
            closed=len(closed),
            accrued=accrued,
            gross_rupees=gross_total,
            costs_rupees=costs_total,
            unclosable=unclosable,
            notes=tuple(notes),
        )

    # -- internals -------------------------------------------------------------------------

    def _costs_for(
        self, position: OpenPaperPosition, *, exit_price_paise: Decimal
    ) -> Decimal | None:
        if self._costs_rupees_for is None:
            return Decimal("0")
        try:
            return Decimal(
                str(
                    self._costs_rupees_for(  # type: ignore[operator]
                        position=position, exit_price_paise=exit_price_paise
                    )
                )
            )
        except Exception:  # noqa: BLE001 — an unpriceable trade is left OPEN, never mispriced
            return None

    def _insert(self, position: OpenPaperPosition) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO open_paper_position (position_key, bot_identity, "
            "instrument_token, trading_symbol, segment, side, quantity, entry_price_paise, "
            "opened_at, session_date, stated_win_probability) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position.position_key,
                position.bot_identity,
                position.instrument_token,
                position.trading_symbol,
                str(position.segment),
                str(position.side),
                position.quantity,
                str(position.entry_price_paise),
                position.opened_at.isoformat(),
                position.session_date.isoformat(),
                position.stated_win_probability,
            ),
        )

    def _delete(self, position_key: str) -> None:
        self._connection.execute(
            "DELETE FROM open_paper_position WHERE position_key = ?", (position_key,)
        )


def _position_from(
    admitted: AdmittedProposal, *, at: datetime, session_date: date
) -> OpenPaperPosition:
    signal: PricedSignal = admitted.signal
    return OpenPaperPosition(
        bot_identity=admitted.bot_identity,
        instrument_token=signal.instrument_token,
        trading_symbol=signal.trading_symbol,
        segment=signal.segment,
        side=signal.side,
        quantity=admitted.quantity,
        entry_price_paise=signal.reference_price_paise,
        opened_at=at,
        session_date=session_date,
        stated_win_probability=(
            float(signal.conviction) if signal.conviction is not None else None
        ),
    )


def positions_by_identity(
    positions: Sequence[OpenPaperPosition],
) -> dict[str, tuple[OpenPaperPosition, ...]]:
    grouped: dict[str, list[OpenPaperPosition]] = {}
    for position in positions:
        grouped.setdefault(position.bot_identity, []).append(position)
    return {identity: tuple(items) for identity, items in grouped.items()}
