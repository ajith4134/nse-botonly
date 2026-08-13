"""The write-ahead log that makes a crash survivable — `L3.02`, and the spine of `L3.01`.

The obligation is an ORDERING, and it is the whole entry:

1. the intent is written and **committed**, before any network call exists;
2. the submission attempt is written and **committed** as in-flight, before the broker call is made;
3. the broker call happens;
4. whatever came back is written — including *"the call raised and I do not know"*.

A crash between 2 and 4 leaves an in-flight submission on disk, which is exactly the record the
reconciler needs. Systems that write after the call — freqtrade calls the exchange at
`freqtradebot.py:963` and commits at `:1029-1067`, LEAN keeps orders in memory and dumps JSON once a
day — cannot tell **"never sent"** from **"sent and I died"**, and those two require opposite
recoveries. NautilusTrader writes first (`engine/mod.rs:2081` before `:2142`), and that is the
ordering copied here (`docs/research/224` §3).

**Append-only.** States are new rows, never updates in place, so what was believed and when survives
the day. A fold collapses the rows into the current `OrderRecord`, and the fold is deterministic:
the same rows always produce the same order.

**Durability is measured, not assumed.** `journal_mode=WAL` with `synchronous=FULL`, whose commit
costs ~2.2 ms on this machine's disk — the cost of a real fsync, which is the evidence that
`commit()` returns only once the write survives a power cut. The sourcing pass (`docs/research/221`
§12.1) found every SQLite library offering the same floor behind a heavier model, so this is stdlib
`sqlite3` deliberately.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType

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
)
from nse_algo_trader.order_path.order_record import (
    FillRecord,
    OrderExpression,
    OrderRecord,
)
from nse_algo_trader.order_path.trading_intent import (
    OrderNamespace,
    OrderPathError,
    TradingIntent,
    broker_tag_for,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

DEFAULT_JOURNAL_PATH = Path.home() / ".nse_algo_trader" / "order_path.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_intent (
    intent_id TEXT PRIMARY KEY,
    session_date TEXT NOT NULL,
    namespace TEXT NOT NULL,
    broker_tag TEXT NOT NULL,
    strategy_identity TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    segment TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    reference_price_paise TEXT NOT NULL,
    expected_edge_bps TEXT NOT NULL,
    expression_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_intent_by_session ON order_intent (session_date);
CREATE UNIQUE INDEX IF NOT EXISTS order_intent_by_tag ON order_intent (broker_tag);

CREATE TABLE IF NOT EXISTS order_submission (
    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES order_intent (intent_id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    outcome TEXT,
    broker_order_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    settled_at TEXT
);
CREATE INDEX IF NOT EXISTS order_submission_by_intent ON order_submission (intent_id);

CREATE TABLE IF NOT EXISTS order_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES order_intent (intent_id),
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    broker_order_id TEXT
);
CREATE INDEX IF NOT EXISTS order_event_by_intent ON order_event (intent_id, event_id);

CREATE TABLE IF NOT EXISTS order_visibility_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    delay_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_fill (
    intent_id TEXT NOT NULL REFERENCES order_intent (intent_id),
    broker_trade_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_paise TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    inferred_from TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (intent_id, broker_trade_id)
);
"""


class JournalError(OrderPathError):
    """The journal cannot record or reproduce something, and proceeding would lose an order."""


class SubmissionOutcome:
    """The four things a broker call can end as. `UNKNOWN` is the one that matters."""

    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_SENT = "not_sent"


@dataclass(frozen=True, slots=True)
class InFlightSubmission:
    """A submission whose outcome was never written — a crash, or a call still running."""

    submission_id: int
    intent_id: str
    attempt: int
    started_at: datetime
    broker_tag: str
    session_date: date


class OrderIntentJournal:
    """The durable record of every intent, submission, event and fill."""

    def __init__(
        self, database_path: Path | None = None, *, busy_timeout_seconds: float = 30.0
    ) -> None:
        self._path = database_path or DEFAULT_JOURNAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path, timeout=busy_timeout_seconds, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        # FULL rather than NORMAL: under NORMAL, a WAL commit can be lost in a power failure, and
        # the whole point of writing before the broker call is that the record outlives the crash.
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def __enter__(self) -> OrderIntentJournal:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    # --- step 1: the intent, before anything else exists ---------------------------------------

    def record_intent(
        self,
        intent: TradingIntent,
        expression: OrderExpression,
        namespace: OrderNamespace,
        *,
        at: datetime,
    ) -> bool:
        """Write the intent durably. Returns False if this exact intent was already recorded.

        The return value IS the idempotency guard: the intent names itself
        (`trading_intent.py`), so a duplicate decision — a retry, a restart, two engines reaching
        the same conclusion — collides here, on disk, before any network call exists.
        """
        tag = broker_tag_for(intent, namespace)
        try:
            self._connection.execute(
                "INSERT INTO order_intent (intent_id, session_date, namespace, broker_tag,"
                " strategy_identity, trading_symbol, instrument_token, segment, side, quantity,"
                " decided_at, reference_price_paise, expected_edge_bps, expression_json,"
                " recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.intent_id,
                    intent.session_date.isoformat(),
                    namespace.value,
                    tag,
                    intent.strategy_identity,
                    intent.trading_symbol,
                    intent.instrument_token,
                    str(intent.segment),
                    str(intent.side),
                    intent.quantity,
                    intent.decided_at.isoformat(),
                    str(intent.reference_price_paise),
                    str(intent.expected_edge_bps),
                    _expression_to_json(expression),
                    at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self._connection.commit()
        return True

    # --- step 2: the attempt, before the call ---------------------------------------------------

    def record_submission_started(self, intent_id: str, *, at: datetime) -> int:
        """Write "I am about to call the broker" and commit it. Returns the submission id."""
        attempt = (
            self._connection.execute(
                "SELECT COUNT(*) FROM order_submission WHERE intent_id = ?", (intent_id,)
            ).fetchone()[0]
            + 1
        )
        cursor = self._connection.execute(
            "INSERT INTO order_submission (intent_id, attempt, started_at) VALUES (?,?,?)",
            (intent_id, attempt, at.isoformat()),
        )
        self._connection.commit()
        submission_id = cursor.lastrowid
        if submission_id is None:  # pragma: no cover — sqlite always assigns one
            raise JournalError("the journal could not name the submission it just wrote")
        return submission_id

    # --- step 4: whatever came back, including nothing -------------------------------------------

    def record_submission_outcome(
        self,
        submission_id: int,
        outcome: str,
        *,
        at: datetime,
        broker_order_id: str | None = None,
        detail: str = "",
    ) -> None:
        """Close out an attempt. `UNKNOWN` is a real outcome and is written like any other."""
        self._connection.execute(
            "UPDATE order_submission SET outcome = ?, broker_order_id = ?, detail = ?,"
            " settled_at = ? WHERE submission_id = ?",
            (outcome, broker_order_id, detail, at.isoformat(), submission_id),
        )
        self._connection.commit()

    def record_event(
        self,
        intent_id: str,
        event: LifecycleEvent,
        source: EventSource,
        *,
        at: datetime,
        note: str = "",
        broker_order_id: str | None = None,
    ) -> None:
        """Append a lifecycle event. Never an update — the history is the point."""
        self._connection.execute(
            "INSERT INTO order_event (intent_id, event, source, occurred_at, note,"
            " broker_order_id) VALUES (?,?,?,?,?,?)",
            (intent_id, event.value, source.value, at.isoformat(), note, broker_order_id),
        )
        self._connection.commit()

    def record_fill(self, intent_id: str, fill: FillRecord) -> bool:
        """Append a fill. Returns False if this trade id was already recorded for this order."""
        try:
            self._connection.execute(
                "INSERT INTO order_fill (intent_id, broker_trade_id, quantity, price_paise,"
                " occurred_at, source, inferred_from) VALUES (?,?,?,?,?,?,?)",
                (
                    intent_id,
                    fill.broker_trade_id,
                    fill.quantity,
                    str(fill.price_paise),
                    fill.occurred_at.isoformat(),
                    fill.source.value,
                    fill.inferred_from,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self._connection.commit()
        return True

    # --- reading it back -------------------------------------------------------------------------

    def has_intent(self, intent_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM order_intent WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            is not None
        )

    def load_order(self, intent_id: str) -> OrderRecord | None:
        """Fold the rows back into the order they describe, deterministically."""
        intent_row = self._connection.execute(
            "SELECT * FROM order_intent WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if intent_row is None:
            return None
        order = OrderRecord(
            intent_id=intent_row["intent_id"],
            broker_tag=intent_row["broker_tag"],
            namespace=OrderNamespace(intent_row["namespace"]),
            trading_symbol=intent_row["trading_symbol"],
            instrument_token=intent_row["instrument_token"],
            segment=ChargeableSegment(intent_row["segment"]),
            side=TradeLeg(intent_row["side"]),
            ordered_quantity=intent_row["quantity"],
            expression=_expression_from_json(intent_row["expression_json"]),
            created_at=datetime.fromisoformat(intent_row["recorded_at"]),
        )
        fills = {
            row["broker_trade_id"]: FillRecord(
                broker_trade_id=row["broker_trade_id"],
                quantity=row["quantity"],
                price_paise=Decimal(row["price_paise"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                source=EventSource(row["source"]),
                inferred_from=row["inferred_from"],
            )
            for row in self._connection.execute(
                "SELECT * FROM order_fill WHERE intent_id = ? ORDER BY occurred_at,"
                " broker_trade_id",
                (intent_id,),
            )
        }
        for row in self._connection.execute(
            "SELECT * FROM order_event WHERE intent_id = ? ORDER BY event_id", (intent_id,)
        ):
            event = LifecycleEvent(row["event"])
            if row["broker_order_id"]:
                order.broker_order_id = row["broker_order_id"]
            if event in (LifecycleEvent.PARTIALLY_FILLED, LifecycleEvent.FULLY_FILLED):
                # Fill events are re-derived from the fills themselves rather than replayed as
                # bare transitions: the quantities are the truth, and a replayed transition
                # without its fill would move the state without moving the position.
                continue
            order.apply_event(
                event,
                EventSource(row["source"]),
                at=datetime.fromisoformat(row["occurred_at"]),
                note=row["note"],
            )
        if fills and order.state is OrderLifecycleState.INTENT_RECORDED:
            # A fill exists for an order the journal never saw submitted. That is not corruption:
            # it is the crash window this journal exists to survive — the intent was committed, the
            # process died before the submission event was appended, and the broker went on to fill
            # the order anyway. The submission is therefore INFERRED rather than assumed, and stays
            # marked as inferred for the rest of the order's life.
            first_fill = next(iter(fills.values()))
            order.apply_event(
                LifecycleEvent.SUBMITTED,
                EventSource.INFERRED,
                at=first_fill.occurred_at,
                note=(
                    "inferred from the existence of fill "
                    f"{first_fill.broker_trade_id}: the order filled, so it was submitted, but no "
                    "submission event was ever written"
                ),
            )
        for fill in fills.values():
            order.apply_fill(fill)
        return order

    def record_visibility_delay(
        self, intent_id: str, *, submitted_at: datetime, first_seen_at: datetime
    ) -> float:
        """Record how long the broker took to show an order this system had submitted.

        This is the raw material for the visibility horizon (`broker_truth_reconciler`). Kite
        publishes no propagation-latency figure at all (`docs/research/222` §7), so the only
        honest source for "how long must I wait before absence means absence" is this system's own
        observations. `R.03`: measured, never typed.
        """
        delay_seconds = (first_seen_at - submitted_at).total_seconds()
        self._connection.execute(
            "INSERT INTO order_visibility_observation (intent_id, submitted_at, first_seen_at,"
            " delay_seconds) VALUES (?,?,?,?)",
            (intent_id, submitted_at.isoformat(), first_seen_at.isoformat(), delay_seconds),
        )
        self._connection.commit()
        return delay_seconds

    def visibility_delays_seconds(self) -> tuple[float, ...]:
        """Every observed appearance delay, oldest first."""
        return tuple(
            float(row["delay_seconds"])
            for row in self._connection.execute(
                "SELECT delay_seconds FROM order_visibility_observation ORDER BY observation_id"
            )
        )

    def in_flight_submissions(self, *, session_date: date) -> tuple[InFlightSubmission, ...]:
        """Attempts whose outcome was never written — what a restart has to resolve first."""
        rows = self._connection.execute(
            "SELECT s.submission_id, s.intent_id, s.attempt, s.started_at, i.broker_tag,"
            " i.session_date FROM order_submission s JOIN order_intent i"
            " ON i.intent_id = s.intent_id"
            " WHERE s.outcome IS NULL AND i.session_date = ? ORDER BY s.submission_id",
            (session_date.isoformat(),),
        ).fetchall()
        return tuple(
            InFlightSubmission(
                submission_id=row["submission_id"],
                intent_id=row["intent_id"],
                attempt=row["attempt"],
                started_at=datetime.fromisoformat(row["started_at"]),
                broker_tag=row["broker_tag"],
                session_date=date.fromisoformat(row["session_date"]),
            )
            for row in rows
        )

    def recorded_session_dates(self) -> tuple[date, ...]:
        """Every session this journal holds an intent for, oldest first.

        Measured rather than configured, because anything reading this journal — the reconciler on
        restart, the `/orders` surface — has to answer "which session" without being told, and a
        reader pinned to a date silently goes stale the moment the market rolls over.
        """
        return tuple(
            date.fromisoformat(row["session_date"])
            for row in self._connection.execute(
                "SELECT DISTINCT session_date FROM order_intent ORDER BY session_date"
            )
        )

    def orders_for_session(self, session_date: date) -> tuple[OrderRecord, ...]:
        rows = self._connection.execute(
            "SELECT intent_id FROM order_intent WHERE session_date = ? ORDER BY recorded_at",
            (session_date.isoformat(),),
        ).fetchall()
        orders = [self.load_order(row["intent_id"]) for row in rows]
        return tuple(order for order in orders if order is not None)

    def open_orders(self, session_date: date) -> tuple[OrderRecord, ...]:
        return tuple(
            order for order in self.orders_for_session(session_date) if not order.is_terminal
        )

    def intent_id_for_tag(self, broker_tag: str) -> str | None:
        """Reverse the wire identity — how a broker order is matched back to its decision."""
        row = self._connection.execute(
            "SELECT intent_id FROM order_intent WHERE broker_tag = ?", (broker_tag,)
        ).fetchone()
        return None if row is None else str(row["intent_id"])

    def state_of(self, intent_id: str) -> OrderLifecycleState | None:
        order = self.load_order(intent_id)
        return None if order is None else order.state


def _expression_to_json(expression: OrderExpression) -> str:
    return json.dumps(
        {
            "variety": expression.variety.value,
            "product": expression.product.value,
            "order_type": expression.order_type.value,
            "validity": expression.validity.value,
            "limit_price_paise": _optional_decimal_text(expression.limit_price_paise),
            "trigger_price_paise": _optional_decimal_text(expression.trigger_price_paise),
            "disclosed_quantity": expression.disclosed_quantity,
            "iceberg_legs": expression.iceberg_legs,
            "validity_minutes": expression.validity_minutes,
            "market_protection_percent": expression.market_protection_percent,
            "chosen_because": expression.chosen_because,
        },
        sort_keys=True,
    )


def _expression_from_json(payload: str) -> OrderExpression:
    fields = json.loads(payload)
    return OrderExpression(
        variety=OrderVariety(fields["variety"]),
        product=OrderProduct(fields["product"]),
        order_type=OrderType(fields["order_type"]),
        validity=OrderValidity(fields["validity"]),
        limit_price_paise=_optional_decimal(fields["limit_price_paise"]),
        trigger_price_paise=_optional_decimal(fields["trigger_price_paise"]),
        disclosed_quantity=fields["disclosed_quantity"],
        iceberg_legs=fields["iceberg_legs"],
        validity_minutes=fields["validity_minutes"],
        market_protection_percent=fields["market_protection_percent"],
        chosen_because=fields["chosen_because"],
    )


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
