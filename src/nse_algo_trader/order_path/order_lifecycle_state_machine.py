"""What an order may do next, as a table rather than as scattered conditionals.

The comparison in `docs/research/224` is unambiguous about why this shape and not another. Of five
mature systems, only NautilusTrader keeps the transition table as data and refuses an illegal
transition with a typed error (`crates/model/src/orders/mod.rs:201-285`). Hummingbot overwrites the
state with whatever the venue last said, freqtrade keeps it as an unvalidated string, LEAN drops an
event whose order it cannot match, and OctoBot logs the anomaly at debug level and continues. The
case that separates them is a fill arriving after a cancel: in four of the five it quietly becomes
the truth, and the position is wrong in the direction that costs money.

**`AMBIGUOUS` is the state this table has that the others do not.** Kite Connect accepts no client
order id and answers a timed-out submission with *"Order request timed out. Please check the order
book and confirm before placing again"* (`docs/research/222` §1). "I do not know whether this order
exists" is therefore an ordinary outcome of a correct submission. Modelling it as an exception —
which is what a system without the state must do — leaves the caller holding a decision it cannot
make, and the decision it usually makes is to retry.

**`ABANDONED` is terminal on purpose.** An ambiguous order proven never to have reached the broker
does NOT return to the submission path. Re-deciding is the strategy's business; a state machine that
loops back is how one intent becomes two orders.

**Partial fills do not come from here.** Kite has no partially-filled status: a part-filled order
reads `OPEN` with a non-zero `filled_quantity`. So `PARTIALLY_FILLED` is raised by the fill ledger
from quantities and trades, and the broker-status map deliberately produces it for no status at all.
"""

from __future__ import annotations

from enum import StrEnum


class IllegalOrderTransitionError(Exception):
    """The order cannot do that from where it is, and pretending otherwise loses money."""


class UnmappedBrokerStatusError(Exception):
    """The broker used a word this system does not know. Guessing would mis-state the book."""


class OrderLifecycleState(StrEnum):
    """Where an order is. Terminal states are listed in `TERMINAL_STATES`."""

    INTENT_RECORDED = "intent_recorded"
    SUBMISSION_IN_FLIGHT = "submission_in_flight"
    AMBIGUOUS = "ambiguous"
    ACKNOWLEDGED = "acknowledged"
    TRIGGER_PENDING = "trigger_pending"
    WORKING = "working"
    MODIFY_PENDING = "modify_pending"
    CANCEL_PENDING = "cancel_pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ABANDONED = "abandoned"


class LifecycleEvent(StrEnum):
    """What happened. Sources differ — see `EventSource` — but the vocabulary does not."""

    SUBMITTED = "submitted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    FOUND_AT_BROKER = "found_at_broker"
    PROVEN_ABSENT = "proven_absent"
    ACKNOWLEDGED = "acknowledged"
    TRIGGER_ARMED = "trigger_armed"
    OPENED = "opened"
    MODIFY_REQUESTED = "modify_requested"
    MODIFIED = "modified"
    CANCEL_REQUESTED = "cancel_requested"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EventSource(StrEnum):
    """Where the claim came from, carried on every transition and never erased.

    An inferred transition — one this system had to invent to explain a gap between its own view
    and the broker's — can never be presented later as something that was observed. NautilusTrader
    marks its synthetic fills `reconciliation=true` for the same reason
    (`reconciliation/orders.rs:1051-1131`).
    """

    LOCAL = "local"
    BROKER_REPORTED = "broker_reported"
    INFERRED = "inferred"


TERMINAL_STATES: frozenset[OrderLifecycleState] = frozenset(
    {
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.ABANDONED,
    }
)

_LIVE_AT_BROKER = (
    OrderLifecycleState.ACKNOWLEDGED,
    OrderLifecycleState.TRIGGER_PENDING,
    OrderLifecycleState.WORKING,
    OrderLifecycleState.MODIFY_PENDING,
    OrderLifecycleState.CANCEL_PENDING,
    OrderLifecycleState.PARTIALLY_FILLED,
)


def _build_transitions() -> dict[tuple[OrderLifecycleState, LifecycleEvent], OrderLifecycleState]:
    table: dict[tuple[OrderLifecycleState, LifecycleEvent], OrderLifecycleState] = {
        # The intent is on disk; the network call has not been made.
        (OrderLifecycleState.INTENT_RECORDED, LifecycleEvent.SUBMITTED): (
            OrderLifecycleState.SUBMISSION_IN_FLIGHT
        ),
        # An intent can be abandoned before it is ever sent — the kill switch, a stale signal, or
        # a precondition that failed between the decision and the call.
        (OrderLifecycleState.INTENT_RECORDED, LifecycleEvent.EXPIRED): OrderLifecycleState.EXPIRED,
        (OrderLifecycleState.INTENT_RECORDED, LifecycleEvent.PROVEN_ABSENT): (
            OrderLifecycleState.ABANDONED
        ),
        (OrderLifecycleState.INTENT_RECORDED, LifecycleEvent.REJECTED): (
            OrderLifecycleState.REJECTED
        ),
        # The call was made. Any of four things can come back, including nothing.
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.ACKNOWLEDGED): (
            OrderLifecycleState.ACKNOWLEDGED
        ),
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.REJECTED): (
            OrderLifecycleState.REJECTED
        ),
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.OUTCOME_UNKNOWN): (
            OrderLifecycleState.AMBIGUOUS
        ),
        # A fast venue can fill before the acknowledgement is even read.
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.OPENED): (
            OrderLifecycleState.WORKING
        ),
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.PARTIALLY_FILLED): (
            OrderLifecycleState.PARTIALLY_FILLED
        ),
        (OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.FULLY_FILLED): (
            OrderLifecycleState.FILLED
        ),
        # Ambiguity resolves by looking, never by assuming.
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.FOUND_AT_BROKER): (
            OrderLifecycleState.ACKNOWLEDGED
        ),
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.PROVEN_ABSENT): (
            OrderLifecycleState.ABANDONED
        ),
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.OPENED): OrderLifecycleState.WORKING,
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.TRIGGER_ARMED): (
            OrderLifecycleState.TRIGGER_PENDING
        ),
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.PARTIALLY_FILLED): (
            OrderLifecycleState.PARTIALLY_FILLED
        ),
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.FULLY_FILLED): OrderLifecycleState.FILLED,
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.CANCELLED): OrderLifecycleState.CANCELLED,
        (OrderLifecycleState.AMBIGUOUS, LifecycleEvent.REJECTED): OrderLifecycleState.REJECTED,
        # Acknowledged: the broker has it, the exchange may not have shown it yet.
        (OrderLifecycleState.ACKNOWLEDGED, LifecycleEvent.OPENED): OrderLifecycleState.WORKING,
        (OrderLifecycleState.ACKNOWLEDGED, LifecycleEvent.TRIGGER_ARMED): (
            OrderLifecycleState.TRIGGER_PENDING
        ),
        (OrderLifecycleState.TRIGGER_PENDING, LifecycleEvent.OPENED): OrderLifecycleState.WORKING,
        (OrderLifecycleState.MODIFY_PENDING, LifecycleEvent.MODIFIED): (
            OrderLifecycleState.WORKING
        ),
        (OrderLifecycleState.MODIFY_PENDING, LifecycleEvent.OPENED): OrderLifecycleState.WORKING,
        (OrderLifecycleState.PARTIALLY_FILLED, LifecycleEvent.PARTIALLY_FILLED): (
            OrderLifecycleState.PARTIALLY_FILLED
        ),
    }

    # Every state in which the order is live at the broker shares the same repertoire: it can fill,
    # part-fill, be modified, be asked to cancel, be cancelled, be rejected, or expire at the close.
    # Written as a loop rather than 40 lines of near-identical entries — the table is still data,
    # and a reader can see that the repertoire is uniform instead of having to diff the rows.
    for state in _LIVE_AT_BROKER:
        # OPENED is in this repertoire because Kite HAS NO partially-filled status: a part-filled
        # order reads `OPEN` with a non-zero filled quantity, so the reconciler maps it to OPENED on
        # every pass. Without this line the commonest state in an intraday book was reported as a
        # disagreement with the broker on every pass forever, burying the real conflicts — found by
        # the R.23(c) review. A partially-filled order that is still open stays partially filled;
        # every other live state goes to WORKING.
        table.setdefault(
            (state, LifecycleEvent.OPENED),
            OrderLifecycleState.PARTIALLY_FILLED
            if state is OrderLifecycleState.PARTIALLY_FILLED
            else OrderLifecycleState.WORKING,
        )
        table.setdefault(
            (state, LifecycleEvent.PARTIALLY_FILLED), OrderLifecycleState.PARTIALLY_FILLED
        )
        table.setdefault((state, LifecycleEvent.FULLY_FILLED), OrderLifecycleState.FILLED)
        table.setdefault(
            (state, LifecycleEvent.MODIFY_REQUESTED), OrderLifecycleState.MODIFY_PENDING
        )
        table.setdefault(
            (state, LifecycleEvent.CANCEL_REQUESTED), OrderLifecycleState.CANCEL_PENDING
        )
        table.setdefault((state, LifecycleEvent.CANCELLED), OrderLifecycleState.CANCELLED)
        table.setdefault((state, LifecycleEvent.REJECTED), OrderLifecycleState.REJECTED)
        table.setdefault((state, LifecycleEvent.EXPIRED), OrderLifecycleState.EXPIRED)
    return table


_TRANSITIONS = _build_transitions()

# Kite's own vocabulary, mapped explicitly. SDK 5.2.1 exposes only the three terminal statuses as
# constants (`docs/research/222`), so the rest are documentation strings the broker can change
# without notice — which is exactly why an unrecognised one refuses instead of defaulting.
_BROKER_STATUS_EVENTS: dict[str, LifecycleEvent] = {
    "PUT ORDER REQ RECEIVED": LifecycleEvent.ACKNOWLEDGED,
    "AMO REQ RECEIVED": LifecycleEvent.ACKNOWLEDGED,
    "VALIDATION PENDING": LifecycleEvent.ACKNOWLEDGED,
    "OPEN PENDING": LifecycleEvent.ACKNOWLEDGED,
    "OPEN": LifecycleEvent.OPENED,
    "TRIGGER PENDING": LifecycleEvent.TRIGGER_ARMED,
    "MODIFY VALIDATION PENDING": LifecycleEvent.MODIFY_REQUESTED,
    "MODIFY PENDING": LifecycleEvent.MODIFY_REQUESTED,
    "MODIFIED": LifecycleEvent.MODIFIED,
    "CANCEL PENDING": LifecycleEvent.CANCEL_REQUESTED,
    "COMPLETE": LifecycleEvent.FULLY_FILLED,
    "CANCELLED": LifecycleEvent.CANCELLED,
    "REJECTED": LifecycleEvent.REJECTED,
}


def next_state(state: OrderLifecycleState, event: LifecycleEvent) -> OrderLifecycleState:
    """Apply an event, or refuse. There is no third behaviour, and that is the point."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalOrderTransitionError(
            f"an order in {state.name} cannot receive {event.name}"
            + (
                f"; {state.name} is terminal, and an event arriving after it means the local view "
                f"and the broker's have diverged — which is a reconciliation problem, not a state "
                f"update"
                if state in TERMINAL_STATES
                else ""
            )
        ) from None


def lifecycle_event_for_broker_status(status: str) -> LifecycleEvent:
    """Translate the broker's word into this system's vocabulary, or refuse to."""
    try:
        return _BROKER_STATUS_EVENTS[status.strip().upper()]
    except KeyError:
        raise UnmappedBrokerStatusError(
            f"the broker reported status {status!r}, which this system has no mapping for; "
            f"guessing would put a wrong state on a real order, so the order is held and "
            f"surfaced instead"
        ) from None


def reachable_states() -> frozenset[OrderLifecycleState]:
    """Every state an order can actually get to, walked from the beginning.

    Used as a test rather than for control flow: an unreachable state is either a modelling error
    or dead vocabulary, and both are defects worth failing a build over.
    """
    seen = {OrderLifecycleState.INTENT_RECORDED}
    frontier = [OrderLifecycleState.INTENT_RECORDED]
    while frontier:
        current = frontier.pop()
        for (state, _event), destination in _TRANSITIONS.items():
            if state is current and destination not in seen:
                seen.add(destination)
                frontier.append(destination)
    return frozenset(seen)
