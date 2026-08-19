"""Fold a session's orders ONE AT A TIME, so one unreadable order cannot hide all the others.

`OrderIntentJournal.orders_for_session` folds every intent of a session by replaying its events and
fills, and any one of those folds can raise: a row written by an earlier build carrying an enum
member this build no longer knows, an event stream the state machine will not accept, a corrupted
`expression_json`. The method is all-or-nothing, so ONE such order takes the whole call down —
and with it every consumer that asks the journal what happened today.

**The defect this module exists to prevent** (`M5` adversarial review, reproduced end to end). The
`/orders` page called `orders_for_session` unguarded, so a single unfoldable order turned the whole
surface into an HTTP 500. That page carries the in-flight submission queue — the attempts whose
outcome was never written — and the inferred-event ledger. Those are precisely what an operator
needs at the moment something in the journal has gone wrong, and they became unreachable exactly
then. The blast radius of one bad row was the entire operational view.

**Why the unreadable order becomes a ROW rather than a skipped one.** `orders_for_session` already
drops anything `load_order` returns `None` for, and a dropped row is not a smaller failure than a
500 — it is a quieter one. An operator reading a table of six orders where seven were placed has no
way to know the seventh exists, and the page has told them, in the most credible way available,
that it does not. So every intent the journal holds for the session leaves this module either as an
`OrderRecord` or as an `UnreadableOrder` carrying the exception that stopped it, and the count of
the two together is the count of intents recorded.

**The happy path is untouched.** `orders_for_session` is tried first, exactly as before, and the
per-order recovery below only runs once it has already raised. A session that folds cleanly opens
no extra connection, runs no extra query, and pays nothing for this module existing. That ordering
is also what keeps the recovery honest about the schema: the SQL here is a recovery tool, and if it
ever disagreed with the journal's own reader the disagreement could not affect a healthy session.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nse_algo_trader.order_path.order_intent_journal import OrderIntentJournal
from nse_algo_trader.order_path.order_record import OrderRecord

# The journal's own table and columns, named here only so that a session whose fold has ALREADY
# failed can still be enumerated intent by intent. `OrderIntentJournal` owns this schema; this
# query is a recovery path that never runs while the journal's own reader is working.
_INTENT_IDS_FOR_SESSION_QUERY = (
    "SELECT intent_id FROM order_intent WHERE session_date = ? ORDER BY recorded_at"
)


@dataclass(frozen=True, slots=True)
class UnreadableOrder:
    """An intent the journal holds that this build could not fold back into an order.

    It carries the exception verbatim because the exception IS the finding. "Something could not be
    read" is not actionable; `ValueError: 'iceberg_v2' is not a valid OrderVariety` names the
    migration that has to happen, and it names it to whoever is looking at the page rather than to
    whoever later reads a log.
    """

    intent_id: str | None
    """The intent that could not be folded, or `None` when the failure was the session as a whole
    and the journal could not even be asked which intents it holds."""

    failure_type: str
    """The exception's class name — the part that says what KIND of damage this is."""

    failure_detail: str
    """The exception's own message, verbatim and unabridged."""

    @property
    def failure_sentence(self) -> str:
        """Type and message as one line, for a surface that has one cell to say it in."""
        return f"{self.failure_type}: {self.failure_detail}"


@dataclass(frozen=True, slots=True)
class DefensivelyFoldedSession:
    """Every intent of the session, split into the ones that folded and the ones that did not."""

    orders: tuple[OrderRecord, ...]
    unreadable_orders: tuple[UnreadableOrder, ...]

    @property
    def recorded_intent_count(self) -> int:
        """How many intents the session holds — readable or not. The number that must not shrink."""
        return len(self.orders) + len(self.unreadable_orders)


def fold_session_orders_defensively(
    journal: OrderIntentJournal, *, session_date: date, journal_path: Path
) -> DefensivelyFoldedSession:
    """Every order of the session, with the unfoldable ones named instead of raising or vanishing.

    Never raises for a bad row. The one thing a reader of this function may rely on is that it
    returns: an order path surface, a reconciliation review, or an operator at 09:20 asking what
    happened must not be denied the readable ninety-nine because of the hundredth.
    """
    # `except Exception` is deliberate and is NOT a swallow: the fold can raise anything a row on
    # disk can provoke — a retired enum member, an event stream the state machine refuses, a
    # corrupt JSON column — and narrowing it would restore the 500 for every type not listed. The
    # exception is carried into a visible row instead of being logged and lost.
    try:
        cleanly_folded = journal.orders_for_session(session_date)
    except Exception as whole_session_failure:  # noqa: BLE001 — see the comment above
        return _fold_one_intent_at_a_time(
            journal,
            session_date=session_date,
            journal_path=journal_path,
            whole_session_failure=whole_session_failure,
        )
    return DefensivelyFoldedSession(orders=cleanly_folded, unreadable_orders=())


def _fold_one_intent_at_a_time(
    journal: OrderIntentJournal,
    *,
    session_date: date,
    journal_path: Path,
    whole_session_failure: BaseException,
) -> DefensivelyFoldedSession:
    """The recovery: ask for the intent ids, then fold each one behind its own guard."""
    try:
        intent_ids = _recorded_intent_ids_for_session(journal_path, session_date)
    except (sqlite3.Error, OSError) as enumeration_failure:
        # The session cannot even be enumerated, so no per-order row can be produced. That is
        # itself the finding, and it is reported as ONE unidentified unreadable order rather than
        # as an empty session, which would read as "nothing was decided today".
        return DefensivelyFoldedSession(
            orders=(),
            unreadable_orders=(
                UnreadableOrder(
                    intent_id=None,
                    failure_type=type(whole_session_failure).__name__,
                    failure_detail=(
                        f"{whole_session_failure}; and the intents of {session_date.isoformat()} "
                        f"could not then be listed from {journal_path} either "
                        f"({type(enumeration_failure).__name__}: {enumeration_failure}), so no "
                        f"individual order could be reported"
                    ),
                ),
            ),
        )

    orders: list[OrderRecord] = []
    unreadable: list[UnreadableOrder] = []
    for intent_id in intent_ids:
        try:
            # Same breadth as above, now per order: whatever this one row provokes stops here.
            order = journal.load_order(intent_id)
        except Exception as fold_failure:  # noqa: BLE001 — see the comment inside the try
            unreadable.append(
                UnreadableOrder(
                    intent_id=intent_id,
                    failure_type=type(fold_failure).__name__,
                    failure_detail=str(fold_failure),
                )
            )
            continue
        if order is None:
            # The intent row was listed a moment ago and the journal now says there is no such
            # intent. Reported rather than dropped: it means the journal changed underneath the
            # read, and a silently shorter table is the one outcome this module exists to refuse.
            unreadable.append(
                UnreadableOrder(
                    intent_id=intent_id,
                    failure_type="MissingIntent",
                    failure_detail=(
                        "the journal listed this intent for the session and then returned no "
                        "order for it, which means the journal changed while it was being read"
                    ),
                )
            )
            continue
        orders.append(order)

    if not unreadable:
        # Every order folded on the second pass, yet the whole-session fold raised. The failure is
        # real and is not attributable to a row, so it is carried unattributed rather than lost.
        unreadable.append(
            UnreadableOrder(
                intent_id=None,
                failure_type=type(whole_session_failure).__name__,
                failure_detail=(
                    f"{whole_session_failure} — raised while folding the session as a whole, but "
                    f"no individual order reproduced it, so the session below may be incomplete"
                ),
            )
        )
    return DefensivelyFoldedSession(orders=tuple(orders), unreadable_orders=tuple(unreadable))


def _recorded_intent_ids_for_session(journal_path: Path, session_date: date) -> tuple[str, ...]:
    """Which intents the session holds, read WITHOUT folding any of them.

    Opened `mode=ro` through SQLite's URI syntax rather than as an ordinary connection: this runs
    on a read-only surface, and a read-only surface must be incapable of creating a journal, of
    migrating one, or of taking a write lock on a database the trading loop is writing to.
    """
    with closing(sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)) as read_only_connection:
        return tuple(
            str(row[0])
            for row in read_only_connection.execute(
                _INTENT_IDS_FOR_SESSION_QUERY, (session_date.isoformat(),)
            )
        )
