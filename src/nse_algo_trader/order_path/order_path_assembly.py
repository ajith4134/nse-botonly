"""One place that assembles the order path, so nothing else has to know how the parts fit.

The pieces of `F02` are deliberately independent — the placer depends on the SHAPE of a control
gate and a rate gate rather than on their identity, which is what let them be built and tested
separately. Something still has to put them together, and if that something is spread across
callers then every caller gets to decide whether the kill switch is attached. This module is the
only assembly, so "was the latch consulted" has exactly one answer.

The two adapters here exist because the engines they wrap answer richer questions than the placer
asks. `TradingControlLatchStore` distinguishes paper from live and returns evidence; the placer only
needs "may I submit, and if not, why not". `OrderSubmissionRateLimiter` returns a measured wait and
the window that bound; the placer only needs "did it clear before the intent expired". Narrowing at
the seam keeps the placer honest about what it is entitled to know.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from nse_algo_trader.order_path.broker_truth_reconciler import BrokerTruthReconciler
from nse_algo_trader.order_path.crash_safe_order_placer import (
    CrashSafeOrderPlacer,
    PlacementOutcome,
)
from nse_algo_trader.order_path.order_execution_venue import OrderExecutionVenue
from nse_algo_trader.order_path.order_expression_selector import (
    ExpressionChoice,
    ExpressionSelectionRequest,
    OrderExpressionSelector,
)
from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_submission_rate_limiter import OrderSubmissionRateLimiter
from nse_algo_trader.order_path.trading_control_latch import TradingControlLatchStore, TradingMode
from nse_algo_trader.order_path.trading_intent import OrderNamespace, OrderPathError


class OrderPathAssemblyError(OrderPathError):
    """The path was asked for something the parts it was assembled with cannot do."""


@dataclass(slots=True)
class LatchBackedControlGate:
    """The kill switch, narrowed to the question the placer is allowed to ask.

    The intended mode is derived from the namespace rather than passed alongside it: a LIVE
    namespace order IS a request to trade live, and letting the two be stated separately would
    create a combination — live namespace, paper intent — that means nothing and could be waved
    through.
    """

    latch_store: TradingControlLatchStore

    def permits_submission(self, namespace: OrderNamespace) -> tuple[bool, str]:
        intended_mode = TradingMode.LIVE if namespace is OrderNamespace.LIVE else TradingMode.PAPER
        refusal = self.latch_store.refusal_for_submission(intended_mode=intended_mode)
        if refusal is None:
            return True, f"trading control permits {intended_mode.value} submission"
        return False, refusal


@dataclass(slots=True)
class RateLimiterBackedSubmissionGate:
    """The rate limiter, narrowed the same way — and it BLOCKS rather than dropping.

    The wait happens inside `acquire`; by the time this returns, either the budget was there or the
    intent outlived its own deadline. An order the broker refuses for crossing the threshold is a
    silently lost order (`docs/research/223` §7), so waiting locally is the only version of this
    that can be observed.
    """

    limiter: OrderSubmissionRateLimiter

    def acquire(self, exchange: str, deadline: datetime) -> tuple[bool, str]:
        outcome = self.limiter.acquire(exchange, deadline)
        return outcome.granted, outcome.reason


@dataclass(slots=True)
class OrderPath:
    """Everything `F02` is, assembled: journal, venue, gates, selector, placer and reconciler."""

    journal: OrderIntentJournal
    venue: OrderExecutionVenue
    placer: CrashSafeOrderPlacer
    reconciler: BrokerTruthReconciler
    namespace: OrderNamespace
    latch_store: TradingControlLatchStore
    rate_limiter: OrderSubmissionRateLimiter
    selector: OrderExpressionSelector | None = None

    def choose_and_place(
        self, request: ExpressionSelectionRequest, *, now: datetime
    ) -> tuple[ExpressionChoice, PlacementOutcome]:
        """Decide HOW to express the intent, then place it — the whole path in one call.

        The two halves are kept separate everywhere else because they answer different questions
        and fail in different ways, but nothing above this should have to know that a chosen
        expression and a placed order are two steps. The choice is returned alongside the outcome
        rather than swallowed: `chosen_because` is the audit trail for why this order and not the
        alternative, and a placement whose reasoning is lost cannot be reviewed afterwards.
        """
        if self.selector is None:
            raise OrderPathAssemblyError(
                "this order path was assembled without an expression selector, so it can place an "
                "expression it is given but cannot choose one; a caller that wants the choice made "
                "for it must assemble with the cost and fill engines attached"
            )
        choice = self.selector.select(request)
        outcome = self.placer.place(request.intent, choice.chosen, now=now)
        return choice, outcome

    def close(self) -> None:
        self.journal.close()
        self.rate_limiter.close()


def assemble_order_path(
    venue: OrderExecutionVenue,
    *,
    session_date: date,
    namespace: OrderNamespace,
    journal_path: Path | None = None,
    selector: OrderExpressionSelector | None = None,
) -> OrderPath:
    """Build the whole path with every gate attached. There is no variant without them."""
    journal = OrderIntentJournal(journal_path)
    latch_store = TradingControlLatchStore()
    rate_limiter = OrderSubmissionRateLimiter(session_date)
    placer = CrashSafeOrderPlacer(
        journal=journal,
        venue=venue,
        namespace=namespace,
        control_gate=LatchBackedControlGate(latch_store),
        rate_gate=RateLimiterBackedSubmissionGate(rate_limiter),
    )
    return OrderPath(
        journal=journal,
        venue=venue,
        placer=placer,
        reconciler=BrokerTruthReconciler(journal=journal, venue=venue, namespace=namespace),
        namespace=namespace,
        latch_store=latch_store,
        rate_limiter=rate_limiter,
        selector=selector,
    )
