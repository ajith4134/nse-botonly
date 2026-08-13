"""The order, its expression, and the fill arithmetic that decides what is actually held.

This is the only place in the system that answers "how much of that order is done, and at what
price". Three properties are deliberate, and each one is a defect found in shipped code
(`docs/research/224` §5):

* **Cumulative quantities only ever go up.** OctoBot, Hummingbot and freqtrade all assign the
  venue's snapshot straight onto `filled_quantity`, so a stale or out-of-order poll response makes
  the position go backwards. Kite publishes no ordering guarantee for postbacks
  (`docs/research/222` §3), so out-of-order delivery here is a certainty rather than a risk.
* **A fill is applied once, keyed on the broker's own trade id, checked before the state changes.**
  LEAN keys its fill events on an id it generates itself, so a re-delivered execution applies
  twice.
* **The same trade id carrying different facts is a refusal, not a merge.** Idempotence and
  tolerance are different things: two different quantities under one id means either the broker's
  data or this system's matching is wrong, and absorbing it silently would hide whichever it is.

**Derived, never stored twice.** `leaves_quantity` and `average_fill_price_paise` are computed from
the fills. A stored pair that can disagree is a partial-fill bug waiting for a busy morning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    LifecycleEvent,
    OrderLifecycleState,
    next_state,
)
from nse_algo_trader.order_path.trading_intent import (
    OrderNamespace,
    OrderPathError,
    TradingIntent,
    broker_tag_for,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg


class DuplicateFillRejectedError(OrderPathError):
    """One trade id, two different stories. Absorbing it would hide which one is wrong."""


class OrderQuantityError(OrderPathError):
    """The arithmetic of the fills contradicts the order they belong to."""


@dataclass(frozen=True, slots=True)
class OrderExpression:
    """HOW an intent is being expressed — the member of the taxonomy actually sent.

    Separate from the intent because the same decision can be expressed several ways, and `L9.14`'s
    selector is the thing that chooses between them. Keeping them apart is what lets a re-expression
    (a limit repriced, an iceberg split differently) happen without the decision being renamed.
    """

    variety: OrderVariety
    product: OrderProduct
    order_type: OrderType
    validity: OrderValidity
    limit_price_paise: Decimal | None = None
    trigger_price_paise: Decimal | None = None
    disclosed_quantity: int | None = None
    iceberg_legs: int | None = None
    validity_minutes: int | None = None
    market_protection_percent: int | None = None
    chosen_because: str = ""

    def __post_init__(self) -> None:
        needs_price = self.order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT)
        if needs_price and self.limit_price_paise is None:
            raise OrderPathError(
                f"{self.order_type} needs a limit price; sending it without one lets the broker "
                f"decide what this system meant"
            )
        needs_trigger = self.order_type in (OrderType.STOP_LOSS_LIMIT, OrderType.STOP_LOSS_MARKET)
        if needs_trigger and self.trigger_price_paise is None:
            raise OrderPathError(f"{self.order_type} needs a trigger price")
        if self.variety is OrderVariety.ICEBERG and not self.iceberg_legs:
            raise OrderPathError(
                "an iceberg without a leg count is a plain order wearing the wrong name"
            )
        if self.validity is OrderValidity.TIME_TO_LIVE and not self.validity_minutes:
            raise OrderPathError("a time-to-live validity needs the time to live")


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One execution. Immutable, and keyed by the broker's own trade id wherever one exists."""

    broker_trade_id: str
    quantity: int
    price_paise: Decimal
    occurred_at: datetime
    source: EventSource
    inferred_from: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise OrderQuantityError(f"a fill of {self.quantity} is not a fill")
        if self.price_paise <= 0:
            raise OrderQuantityError(f"a fill at {self.price_paise} paise is not a price")
        if self.source is EventSource.INFERRED and not self.inferred_from.strip():
            raise ValueError(
                "an inferred fill must record what it was inferred from: it is this system's own "
                "invention used to explain a gap, and an invention that cannot be traced back to "
                "its evidence is indistinguishable later from something that was observed"
            )

    @property
    def value_paise(self) -> Decimal:
        return Decimal(self.quantity) * self.price_paise


@dataclass(frozen=True, slots=True)
class StateTransition:
    """A move in the lifecycle, with the authority behind it."""

    from_state: OrderLifecycleState
    to_state: OrderLifecycleState
    event: LifecycleEvent
    source: EventSource
    occurred_at: datetime
    note: str = ""


@dataclass(slots=True)
class OrderRecord:
    """One order: what was asked for, what was sent, and everything that happened to it."""

    intent_id: str
    broker_tag: str
    namespace: OrderNamespace
    trading_symbol: str
    instrument_token: int
    segment: ChargeableSegment
    side: TradeLeg
    ordered_quantity: int
    expression: OrderExpression
    created_at: datetime
    state: OrderLifecycleState = OrderLifecycleState.INTENT_RECORDED
    broker_order_id: str | None = None
    terminal_reason: str = ""
    transitions: tuple[StateTransition, ...] = ()
    fills: tuple[FillRecord, ...] = ()
    submission_attempts: int = 0

    _fills_by_trade_id: dict[str, FillRecord] = field(default_factory=dict, repr=False)

    @classmethod
    def from_intent(
        cls,
        intent: TradingIntent,
        expression: OrderExpression,
        namespace: OrderNamespace,
        *,
        at: datetime,
    ) -> OrderRecord:
        """Build the order an intent asks for, named by the intent rather than by a counter."""
        return cls(
            intent_id=intent.intent_id,
            broker_tag=broker_tag_for(intent, namespace),
            namespace=namespace,
            trading_symbol=intent.trading_symbol,
            instrument_token=intent.instrument_token,
            segment=intent.segment,
            side=intent.side,
            ordered_quantity=intent.quantity,
            expression=expression,
            created_at=at,
        )

    @property
    def filled_quantity(self) -> int:
        """Cumulative, derived from the fills, and monotone by construction."""
        return sum(fill.quantity for fill in self.fills)

    @property
    def leaves_quantity(self) -> int:
        return self.ordered_quantity - self.filled_quantity

    @property
    def average_fill_price_paise(self) -> Decimal | None:
        """Quantity-weighted, or `None` when nothing has filled — never a zero standing in."""
        filled = self.filled_quantity
        if filled == 0:
            return None
        return sum((fill.value_paise for fill in self.fills), Decimal(0)) / Decimal(filled)

    @property
    def has_inferred_events(self) -> bool:
        """Whether any part of this order's history was invented to explain a gap."""
        return any(fill.source is EventSource.INFERRED for fill in self.fills) or any(
            transition.source is EventSource.INFERRED for transition in self.transitions
        )

    @property
    def is_terminal(self) -> bool:
        from nse_algo_trader.order_path.order_lifecycle_state_machine import TERMINAL_STATES

        return self.state in TERMINAL_STATES

    def apply_event(
        self,
        event: LifecycleEvent,
        source: EventSource,
        *,
        at: datetime,
        note: str = "",
    ) -> OrderLifecycleState:
        """Move the order, or refuse and leave it exactly where it was.

        The refusal is the point: `next_state` raises before anything here mutates, so a rejected
        event cannot leave a half-applied order behind.
        """
        destination = next_state(self.state, event)
        self.transitions = (
            *self.transitions,
            StateTransition(
                from_state=self.state,
                to_state=destination,
                event=event,
                source=source,
                occurred_at=at,
                note=note,
            ),
        )
        self.state = destination
        if event is LifecycleEvent.SUBMITTED:
            self.submission_attempts += 1
        if destination in (
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.ABANDONED,
        ):
            self.terminal_reason = note or self.terminal_reason
        return destination

    def apply_fill(self, fill: FillRecord) -> None:
        """Apply an execution exactly once, and let the arithmetic decide the state."""
        existing = self._fills_by_trade_id.get(fill.broker_trade_id)
        if existing is not None:
            if (existing.quantity, existing.price_paise) != (fill.quantity, fill.price_paise):
                raise DuplicateFillRejectedError(
                    f"trade {fill.broker_trade_id} was already applied as "
                    f"{existing.quantity}@{existing.price_paise} and has now been reported as "
                    f"{fill.quantity}@{fill.price_paise}; one of the two is wrong and absorbing "
                    f"either would hide which"
                )
            return
        if self.filled_quantity + fill.quantity > self.ordered_quantity:
            raise OrderQuantityError(
                f"fills would exceed the order: {self.filled_quantity} + {fill.quantity} against "
                f"an ordered {self.ordered_quantity}. An over-fill is either a mismatched trade "
                f"or a real broker error, and both need a human before the book is changed"
            )
        self._fills_by_trade_id[fill.broker_trade_id] = fill
        self.fills = (*self.fills, fill)
        filled = self.filled_quantity
        event = (
            LifecycleEvent.FULLY_FILLED
            if filled == self.ordered_quantity
            else LifecycleEvent.PARTIALLY_FILLED
        )
        self.apply_event(event, fill.source, at=fill.occurred_at)
