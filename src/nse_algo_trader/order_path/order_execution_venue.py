"""The seam every order crosses: one interface, a real broker on one side, a simulator on the other.

`L9.03` calls paper/live parity "the architectural decision the whole system rests on", and the
comparison in `docs/research/224` §6 agrees: NautilusTrader's `ExecutionClient` trait is implemented
by both its backtest client and every live adapter, and **the same report types flow into one
reconciliation path**. That last part is what makes parity real rather than cosmetic. A simulator
that returns a different shape forces a second code path, and the second path is the one that is
never tested.

**What is deliberately NOT shared**, so it is never mistaken for a defect:

* the namespace on the wire (`trading_intent.OrderNamespace`) — a simulated order must be
  identifiable as simulated by looking at the order alone;
* the simulator cannot reject for margin it does not model, and says so rather than pretending;
* fill prices, obviously — the simulator's come from the recorded tape through `L1.05`/`L1.06`.

**Three failure modes, and they are not interchangeable.** A rejection is a fact: the broker
declined and no order exists. A timeout is the absence of a fact: the order may or may not exist,
and the only correct response is to go and look (`docs/research/222` §1). An outage is neither: the
call never reached anyone. Collapsing them into one exception is how a system retries an order that
is already live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderPathError


class VenueRejectedError(OrderPathError):
    """The venue declined. A fact: no order exists, and the reason is the venue's own words."""

    def __init__(self, message: str, *, raw_message: str = "") -> None:
        super().__init__(message)
        self.raw_message = raw_message or message


class VenueOutcomeUnknownError(OrderPathError):
    """The call timed out or the response was unreadable. The order may or may not exist.

    Never retried automatically. Zerodha's own timeout text is an instruction to reconcile —
    *"check the order book and confirm before placing again"* — and this exception carries that
    instruction into the type system.
    """


class VenueUnavailableError(OrderPathError):
    """The call did not reach the venue at all — DNS, connection refused, a 5xx before dispatch.

    Distinct from `VenueOutcomeUnknownError`: it is the one failure that CAN be retried safely.
    """


@dataclass(frozen=True, slots=True)
class VenueOrderReport:
    """The venue's own account of one order, normalised but not interpreted."""

    broker_order_id: str
    tag: str
    status: str
    trading_symbol: str
    quantity: int
    filled_quantity: int
    pending_quantity: int
    average_price_paise: Decimal | None
    status_message: str
    status_message_raw: str
    exchange_timestamp: datetime | None
    placed_by: str = ""

    @property
    def is_ours_by_tag(self) -> bool:
        return bool(self.tag)


@dataclass(frozen=True, slots=True)
class VenueTradeReport:
    """One execution as the venue reports it. `trade_id` is the venue's, never ours."""

    broker_trade_id: str
    broker_order_id: str
    trading_symbol: str
    quantity: int
    price_paise: Decimal
    filled_at: datetime


@dataclass(frozen=True, slots=True)
class VenuePositionReport:
    """A position as the venue holds it, tagged with WHICH of the venue's views it came from.

    `positions()` day-scope resets each session and equity carried overnight moves into
    `holdings()`, where `t1_quantity` is bought-today-unsettled and cannot be sold
    (`docs/research/222` §7). A reconciler that reads one view and calls it "the position" is wrong
    on exactly the days that matter.
    """

    trading_symbol: str
    product: str
    quantity: int
    average_price_paise: Decimal | None
    view: str
    unsettled_quantity: int = 0


@runtime_checkable
class OrderExecutionVenue(Protocol):
    """Everything the order path is allowed to ask a venue to do."""

    @property
    def venue_name(self) -> str: ...

    def place(self, order: OrderRecord) -> str:
        """Send the order and return the venue's own order id.

        Raises `VenueRejectedError`, `VenueOutcomeUnknownError` or `VenueUnavailableError`, and
        the difference between the three is the whole contract.
        """
        ...

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None: ...

    def cancel(self, broker_order_id: str, *, variety: str) -> None: ...

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]: ...

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]: ...

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]: ...

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]: ...
