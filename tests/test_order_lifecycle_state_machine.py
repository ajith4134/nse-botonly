"""The transition table, which is the thing four of the five systems in `docs/research/224` lack.

Hummingbot overwrites the state unconditionally, freqtrade stores the venue's status as a raw
string, LEAN drops an event whose order id it does not recognise, and OctoBot debug-logs an illegal
transition and carries on. Only NautilusTrader keeps the table as data and returns a typed error
(`crates/model/src/orders/mod.rs:201-285`). This is that table.

The state that earns its place here and appears in none of those systems is `AMBIGUOUS`: Kite
returns no client order id and its own timeout message tells the caller to go and look
(`docs/research/222` §1), so "I do not know whether this order exists" is an ordinary outcome of a
correct submission, not an error.
"""

from __future__ import annotations

import pytest

from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    TERMINAL_STATES,
    IllegalOrderTransitionError,
    LifecycleEvent,
    OrderLifecycleState,
    UnmappedBrokerStatusError,
    lifecycle_event_for_broker_status,
    next_state,
    reachable_states,
)

pytestmark = pytest.mark.unit


class TestTheHappyPathIsExpressible:
    def test_an_order_can_walk_from_intent_to_filled(self) -> None:
        state = OrderLifecycleState.INTENT_RECORDED
        for event in (
            LifecycleEvent.SUBMITTED,
            LifecycleEvent.ACKNOWLEDGED,
            LifecycleEvent.OPENED,
            LifecycleEvent.PARTIALLY_FILLED,
            LifecycleEvent.FULLY_FILLED,
        ):
            state = next_state(state, event)
        assert state is OrderLifecycleState.FILLED

    def test_an_order_can_fill_without_ever_being_partially_filled(self) -> None:
        state = OrderLifecycleState.WORKING
        assert next_state(state, LifecycleEvent.FULLY_FILLED) is OrderLifecycleState.FILLED

    def test_a_stop_order_rests_before_it_works(self) -> None:
        state = next_state(OrderLifecycleState.ACKNOWLEDGED, LifecycleEvent.TRIGGER_ARMED)
        assert state is OrderLifecycleState.TRIGGER_PENDING
        assert next_state(state, LifecycleEvent.OPENED) is OrderLifecycleState.WORKING


class TestAmbiguityIsAStateNotAnError:
    def test_a_submission_that_timed_out_becomes_ambiguous(self) -> None:
        state = next_state(OrderLifecycleState.SUBMISSION_IN_FLIGHT, LifecycleEvent.OUTCOME_UNKNOWN)
        assert state is OrderLifecycleState.AMBIGUOUS

    def test_an_ambiguous_order_found_at_the_broker_rejoins_its_life(self) -> None:
        assert (
            next_state(OrderLifecycleState.AMBIGUOUS, LifecycleEvent.FOUND_AT_BROKER)
            is OrderLifecycleState.ACKNOWLEDGED
        )

    def test_an_ambiguous_order_proven_absent_is_abandoned_not_retried(self) -> None:
        """`ABANDONED` is terminal on purpose. Re-deciding is the strategy's job, and a state
        machine that loops back to submission is how one intent becomes two orders."""
        state = next_state(OrderLifecycleState.AMBIGUOUS, LifecycleEvent.PROVEN_ABSENT)
        assert state is OrderLifecycleState.ABANDONED
        assert state in TERMINAL_STATES

    def test_an_ambiguous_order_can_turn_out_to_have_filled_already(self) -> None:
        assert (
            next_state(OrderLifecycleState.AMBIGUOUS, LifecycleEvent.FULLY_FILLED)
            is OrderLifecycleState.FILLED
        )


class TestTerminalStatesAbsorb:
    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
    @pytest.mark.parametrize(
        "event",
        [
            LifecycleEvent.SUBMITTED,
            LifecycleEvent.OPENED,
            LifecycleEvent.PARTIALLY_FILLED,
            LifecycleEvent.FULLY_FILLED,
            LifecycleEvent.CANCELLED,
        ],
    )
    def test_nothing_leaves_a_terminal_state(
        self, terminal: OrderLifecycleState, event: LifecycleEvent
    ) -> None:
        with pytest.raises(IllegalOrderTransitionError):
            next_state(terminal, event)

    def test_a_fill_after_a_cancel_raises_rather_than_being_logged(self) -> None:
        """The one case worth naming: it is where money is lost, and it is exactly the case the
        four systems that log-and-continue get wrong."""
        with pytest.raises(IllegalOrderTransitionError, match="CANCELLED"):
            next_state(OrderLifecycleState.CANCELLED, LifecycleEvent.FULLY_FILLED)


class TestTheTableIsData:
    def test_every_state_is_reachable_from_the_beginning(self) -> None:
        """An unreachable state is either a modelling error or dead vocabulary; both are defects."""
        assert reachable_states() == frozenset(OrderLifecycleState)

    def test_every_non_terminal_state_can_reach_a_terminal_one(self) -> None:
        for state in OrderLifecycleState:
            if state in TERMINAL_STATES:
                continue
            assert _can_terminate(state), f"{state} is a trap: nothing can end an order in it"

    def test_the_error_names_both_sides_of_the_refusal(self) -> None:
        with pytest.raises(IllegalOrderTransitionError) as raised:
            next_state(OrderLifecycleState.INTENT_RECORDED, LifecycleEvent.FULLY_FILLED)
        message = str(raised.value)
        assert "INTENT_RECORDED" in message
        assert "FULLY_FILLED" in message


class TestTheBrokersVocabularyIsMappedNotGuessed:
    @pytest.mark.parametrize(
        ("status", "event"),
        [
            ("PUT ORDER REQ RECEIVED", LifecycleEvent.ACKNOWLEDGED),
            ("AMO REQ RECEIVED", LifecycleEvent.ACKNOWLEDGED),
            ("VALIDATION PENDING", LifecycleEvent.ACKNOWLEDGED),
            ("OPEN PENDING", LifecycleEvent.ACKNOWLEDGED),
            ("OPEN", LifecycleEvent.OPENED),
            ("TRIGGER PENDING", LifecycleEvent.TRIGGER_ARMED),
            ("MODIFY PENDING", LifecycleEvent.MODIFY_REQUESTED),
            ("MODIFY VALIDATION PENDING", LifecycleEvent.MODIFY_REQUESTED),
            ("MODIFIED", LifecycleEvent.MODIFIED),
            ("CANCEL PENDING", LifecycleEvent.CANCEL_REQUESTED),
            ("COMPLETE", LifecycleEvent.FULLY_FILLED),
            ("CANCELLED", LifecycleEvent.CANCELLED),
            ("REJECTED", LifecycleEvent.REJECTED),
        ],
    )
    def test_every_published_status_maps(self, status: str, event: LifecycleEvent) -> None:
        assert lifecycle_event_for_broker_status(status) is event

    def test_the_mapping_tolerates_the_broker_shouting_or_padding(self) -> None:
        assert lifecycle_event_for_broker_status("  complete ") is LifecycleEvent.FULLY_FILLED

    def test_an_unknown_status_refuses_rather_than_defaulting(self) -> None:
        """SDK 5.2.1 exposes only the three terminal statuses as constants; the rest are
        documentation strings that Zerodha can change without telling anyone."""
        with pytest.raises(UnmappedBrokerStatusError, match="SUPER PENDING"):
            lifecycle_event_for_broker_status("SUPER PENDING")

    def test_no_status_maps_to_a_partial_fill(self) -> None:
        """Kite has no partially-filled STATUS: a part-filled order reads `OPEN` with a non-zero
        `filled_quantity`. Partial fills therefore come from quantities and trades, never from the
        status field, and a mapping that invented one would silently mis-state the book."""
        statuses = (
            "PUT ORDER REQ RECEIVED",
            "AMO REQ RECEIVED",
            "VALIDATION PENDING",
            "OPEN PENDING",
            "OPEN",
            "TRIGGER PENDING",
            "MODIFY PENDING",
            "MODIFIED",
            "CANCEL PENDING",
            "COMPLETE",
            "CANCELLED",
            "REJECTED",
        )
        assert all(
            lifecycle_event_for_broker_status(status) is not LifecycleEvent.PARTIALLY_FILLED
            for status in statuses
        )


def _can_terminate(state: OrderLifecycleState) -> bool:
    seen: set[OrderLifecycleState] = set()
    frontier = [state]
    while frontier:
        current = frontier.pop()
        if current in TERMINAL_STATES:
            return True
        if current in seen:
            continue
        seen.add(current)
        for event in LifecycleEvent:
            try:
                frontier.append(next_state(current, event))
            except IllegalOrderTransitionError:
                continue
    return False
