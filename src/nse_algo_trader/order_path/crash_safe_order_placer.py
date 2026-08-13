"""The only way an order may reach a broker — `L3.04`, composing `L3.01`, `L3.02` and the gates.

The sequence is the feature. Nothing here is clever; the value is that the steps happen in this
order, every time, with no path around them:

1. **the control latch** — if trading is halted, or the mode is paper and the venue is live, the
   submission is refused before anything is written;
2. **the intent is recorded and committed** — and this is where a duplicate decision collides,
   because the intent names itself from its own content (`trading_intent.py`). A second attempt at
   the same decision does not reach the broker;
3. **the rate gate** — blocks rather than drops, because an order refused by the broker for
   crossing the threshold is a silently lost order (`docs/research/223` §7);
4. **the attempt is recorded and committed as in flight** — before the network call, so a crash
   here is recoverable;
5. **the venue call**, retried ONLY for a failure that provably never reached the venue;
6. **the outcome is written, whatever it is**, including "I do not know".

**A timeout is never retried.** `VenueOutcomeUnknownError` moves the order to `AMBIGUOUS` and
stops there. The reconciler resolves it by looking for our tag in the broker's order book, which
is the only method that cannot double an order. freqtrade achieves the same effect by *not*
wrapping its submit call in its retry decorator (`exchange.py:1445`) — an implicit safeguard that a
later edit can silently remove; here it is a type.

**The gates are Protocols, not imports.** The latch and the rate limiter are separate engines
(`L3.07`, `L3.06`); depending on their shape rather than their identity keeps this module testable
against a fake and keeps the seam honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from nse_algo_trader.order_path.order_execution_venue import (
    OrderExecutionVenue,
    VenueOutcomeUnknownError,
    VenueRejectedError,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal, SubmissionOutcome
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    EventSource,
    LifecycleEvent,
    OrderLifecycleState,
)
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent

# Retries apply ONLY to `VenueUnavailableError` — a call that provably never reached the venue. The
# attempt count is small because the alternative to retrying is not failure, it is reconciliation,
# which is always available and never doubles an order.
_MAXIMUM_DISPATCH_ATTEMPTS = 3


class PlacementVerdict(StrEnum):
    """What happened to a request to place an order."""

    PLACED = "placed"
    ALREADY_PLACED = "already_placed"
    REFUSED_BY_LATCH = "refused_by_latch"
    REFUSED_BY_RATE_GATE = "refused_by_rate_gate"
    REJECTED_BY_VENUE = "rejected_by_venue"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class PlacementOutcome:
    """The answer, with enough detail that nobody has to guess what to do next."""

    verdict: PlacementVerdict
    intent_id: str
    broker_order_id: str | None
    state: OrderLifecycleState
    reason: str
    attempts: int = 0

    @property
    def needs_reconciliation(self) -> bool:
        return self.verdict is PlacementVerdict.AMBIGUOUS


class TradingControlGate(Protocol):
    """The kill switch as this module needs to see it (`L3.07`)."""

    def permits_submission(self, namespace: OrderNamespace) -> tuple[bool, str]: ...


class SubmissionRateGate(Protocol):
    """The rate limiter as this module needs to see it (`L3.06`)."""

    def acquire(self, exchange: str, deadline: datetime) -> tuple[bool, str]: ...


class AlwaysPermits:
    """The null gate, used where a gate is genuinely absent — never as a default in production.

    It exists so a test can isolate the placer, and it announces itself in `reason` so a system
    running with it cannot look like a system running with a real switch.
    """

    def permits_submission(self, namespace: OrderNamespace) -> tuple[bool, str]:
        del namespace
        return True, "no control latch attached"

    def acquire(self, exchange: str, deadline: datetime) -> tuple[bool, str]:
        del exchange, deadline
        return True, "no rate gate attached"


@dataclass(slots=True)
class CrashSafeOrderPlacer:
    """The single door between a decision and a broker."""

    journal: OrderIntentJournal
    venue: OrderExecutionVenue
    namespace: OrderNamespace
    control_gate: TradingControlGate = field(default_factory=AlwaysPermits)
    rate_gate: SubmissionRateGate = field(default_factory=AlwaysPermits)

    def place(
        self,
        intent: TradingIntent,
        expression: OrderExpression,
        *,
        now: datetime,
        deadline: datetime | None = None,
    ) -> PlacementOutcome:
        """Take a decision to the broker, exactly once, or explain why it did not go."""
        permitted, latch_reason = self.control_gate.permits_submission(self.namespace)
        if not permitted:
            return PlacementOutcome(
                verdict=PlacementVerdict.REFUSED_BY_LATCH,
                intent_id=intent.intent_id,
                broker_order_id=None,
                state=OrderLifecycleState.INTENT_RECORDED,
                reason=latch_reason,
            )

        # Step 2 — durable before anything can go wrong, and the point at which a duplicate
        # decision collides with itself.
        first_time = self.journal.record_intent(intent, expression, self.namespace, at=now)
        if not first_time:
            existing = self.journal.load_order(intent.intent_id)
            state = existing.state if existing else OrderLifecycleState.INTENT_RECORDED
            return PlacementOutcome(
                verdict=PlacementVerdict.ALREADY_PLACED,
                intent_id=intent.intent_id,
                broker_order_id=existing.broker_order_id if existing else None,
                state=state,
                reason=(
                    "this exact decision is already in the journal; a second order for it would "
                    "double the position, so the request is answered with the first order's state"
                ),
            )

        order = OrderRecord.from_intent(intent, expression, self.namespace, at=now)

        granted, rate_reason = self.rate_gate.acquire(
            _exchange_of(order), deadline or _end_of(intent, now)
        )
        if not granted:
            self.journal.record_event(
                intent.intent_id,
                LifecycleEvent.EXPIRED,
                EventSource.LOCAL,
                at=now,
                note=rate_reason,
            )
            return PlacementOutcome(
                verdict=PlacementVerdict.REFUSED_BY_RATE_GATE,
                intent_id=intent.intent_id,
                broker_order_id=None,
                state=OrderLifecycleState.EXPIRED,
                reason=rate_reason,
            )

        # Steps 4 to 6. EVERY attempt gets its own journal row, before its own call.
        #
        # An earlier version wrote one row and then made up to three calls under it, so the crash
        # record could not express "I may have created more than one order" — the exact question a
        # restart has to answer. The `R.23(c)` review found it alongside the classification defect
        # that made those extra calls dangerous in the first place.
        self.journal.record_event(
            intent.intent_id, LifecycleEvent.SUBMITTED, EventSource.LOCAL, at=now
        )
        attempts = 0
        submission_id = 0

        def _open_an_attempt(state: RetryCallState) -> None:
            nonlocal attempts, submission_id
            attempts = state.attempt_number
            submission_id = self.journal.record_submission_started(intent.intent_id, at=now)

        def _close_a_failed_attempt(state: RetryCallState) -> None:
            outcome = state.outcome
            if outcome is None or not outcome.failed:
                return
            failure = outcome.exception()
            self.journal.record_submission_outcome(
                submission_id,
                SubmissionOutcome.NOT_SENT
                if isinstance(failure, VenueUnavailableError)
                else SubmissionOutcome.UNKNOWN,
                at=now,
                detail=str(failure),
            )

        try:
            for attempt in Retrying(
                retry=retry_if_exception_type(VenueUnavailableError),
                stop=stop_after_attempt(_MAXIMUM_DISPATCH_ATTEMPTS),
                wait=wait_random_exponential(multiplier=0.2, max=2.0),
                before=_open_an_attempt,
                after=_close_a_failed_attempt,
                reraise=True,
            ):
                with attempt:
                    broker_order_id = self.venue.place(order)
        except VenueRejectedError as rejection:
            self.journal.record_submission_outcome(
                submission_id,
                SubmissionOutcome.REJECTED,
                at=now,
                detail=rejection.raw_message,
            )
            self.journal.record_event(
                intent.intent_id,
                LifecycleEvent.REJECTED,
                EventSource.BROKER_REPORTED,
                at=now,
                note=rejection.raw_message,
            )
            return PlacementOutcome(
                verdict=PlacementVerdict.REJECTED_BY_VENUE,
                intent_id=intent.intent_id,
                broker_order_id=None,
                state=OrderLifecycleState.REJECTED,
                reason=rejection.raw_message,
                attempts=attempts,
            )
        except (VenueOutcomeUnknownError, VenueUnavailableError) as unknown:
            # The distinction between these two collapses only HERE, after the retries are spent:
            # an outage that survived every attempt leaves the same question a timeout does — did
            # the last attempt land? — and the same answer: go and look, do not send again.
            self.journal.record_submission_outcome(
                submission_id, SubmissionOutcome.UNKNOWN, at=now, detail=str(unknown)
            )
            self.journal.record_event(
                intent.intent_id,
                LifecycleEvent.OUTCOME_UNKNOWN,
                EventSource.LOCAL,
                at=now,
                note=str(unknown),
            )
            return PlacementOutcome(
                verdict=PlacementVerdict.AMBIGUOUS,
                intent_id=intent.intent_id,
                broker_order_id=None,
                state=OrderLifecycleState.AMBIGUOUS,
                reason=(
                    f"{unknown}; the order may or may not exist and MUST be resolved by looking "
                    f"for tag {order.broker_tag} in the broker's order book, never by sending "
                    f"again"
                ),
                attempts=attempts,
            )

        self.journal.record_submission_outcome(
            submission_id,
            SubmissionOutcome.ACKNOWLEDGED,
            at=now,
            broker_order_id=broker_order_id,
        )
        self.journal.record_event(
            intent.intent_id,
            LifecycleEvent.ACKNOWLEDGED,
            EventSource.BROKER_REPORTED,
            at=now,
            broker_order_id=broker_order_id,
        )
        return PlacementOutcome(
            verdict=PlacementVerdict.PLACED,
            intent_id=intent.intent_id,
            broker_order_id=broker_order_id,
            state=OrderLifecycleState.ACKNOWLEDGED,
            reason=f"accepted by {self.venue.venue_name}",
            attempts=attempts,
        )


def _exchange_of(order: OrderRecord) -> str:
    """The exchange the regulatory per-second threshold is counted against.

    `ChargeableSegment`'s value already encodes it (`NSE-MIS`, `NFO-OPT`, `MCX-FUT`), so the
    exchange is read off the segment rather than kept as a second field that can disagree with it.
    """
    return str(order.segment).split("-", maxsplit=1)[0]


def _end_of(intent: TradingIntent, now: datetime) -> datetime:
    """The default deadline: the intent's own horizon, or now if it never stated one.

    A signal with a five-bar horizon queued past that horizon is not the same signal, and sending
    it late is worse than not sending it — `L3.18`'s discipline, applied at the gate.
    """
    if intent.horizon_minutes is None:
        return now
    return now + timedelta(minutes=intent.horizon_minutes)
