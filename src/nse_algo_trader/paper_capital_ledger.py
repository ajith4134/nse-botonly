"""The paper trading book's money — bounded, operator-editable, event-sourced (`L1.18`, `A.102`).

Specification: `docs/research/226_paper_capital_ledger_spec.md`.

`deployable_capital_resolver` answers what may be risked with REAL money, and its answer on an
account in debit is correctly zero. **That resolver's authority is scoped to LIVE.** A paper book
trades imaginary money and must not be stopped by a real debit, so it gets its own capital, and this
module is it.

**Why the paper book is finite at all, when `A.06` says paper capital is unlimited.** That decision
stands and is not weakened here: the *experiment* book stays unlimited, because running one
conviction through cash, futures and options simultaneously is how the vehicle-conversion table gets
built, and expressions that compete for capital cannot be compared. What this module serves is the
second book — the *trading* book — and it is finite for an arithmetic reason rather than a
preference:

    Unlimited capital has no denominator. Return on capital, Sharpe, drawdown-as-a-percentage and
    Kelly are all ratios whose divisor is the capital base. An unlimited book can produce a PROFIT;
    it cannot produce a RETURN. `R.22` graduates a strategy on risk-adjusted evidence, and
    risk-adjusted evidence does not exist without a finite denominator.

**The balance is a fold, not a number.** Every change is an appended event. The stored checkpoint is
an optimisation and is *verified against a fresh replay* rather than trusted — on every read AND
before every write. A cached figure that can silently diverge from its source is a defect, not a
saving, and a divergence that the next ordinary write quietly repairs is worse than one that shouts.

**Commitment is tracked apart from balance.** The common naive paper book decrements the
balance when a position opens and credits P&L back on close. Between those two moments the
capital is neither in the balance nor visibly at risk, so a second signal sizes against money
already deployed. Here
`free = balance - committed`; sizing reads `free`; balance moves only on realisation.

**Every write is one serialised transaction, and this is not optional.** The first version folded
the log, decided, and inserted in three separate transactions. The `R.23(c)` review drove eight
threads at a ₹10,00,000 book with ₹2,00,000 reservations and **all eight were accepted** —
₹6,00,000 of commitments the log never justified, in 15 trials out of 15, with `snapshot()`
raising nothing because the checkpoint agreed with the corrupted fold. Cross-process reproduced it
too. The fold, the decision, the insert and the checkpoint now happen inside one `BEGIN IMMEDIATE`,
so a second writer
waits rather than reading a balance that is about to be spent. `F04`'s loop writing fills while the
operator edits the balance is exactly that second writer.

**Sourcing** (`R.17`, evidence in `docs/research/226` §9): `eventsourcing` 9.5.4 and `beancount`
3.2.3 were installed and introspected. The first was rejected because `Decimal` needs a custom
transcoding and its recorder owns an opaque schema that forecloses `L1.11` reading `COST_DEBIT`
apart from `REALISED_LOSS`; the second because its only entry points parse a plaintext DSL and
it has no reservation concept. The append-only-log-plus-verified-checkpoint shape is taken from
the first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import TracebackType
from zoneinfo import ZoneInfo

from nse_algo_trader.capital_configuration import (
    MAXIMUM_SUPPORTED_CAPITAL_RUPEES,
    TradingCapital,
)

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_PAPER_CAPITAL_LEDGER_PATH = (
    Path.home() / ".nse_algo_trader" / "paper_capital_ledger.sqlite3"
)

WRITE_LOCK_TIMEOUT_SECONDS = 30.0
"""How long a writer waits for the one before it.

Not a tuning knob and not derived from data: it is the point past which a blocked dashboard request
should fail loudly rather than hang, and every write here is a handful of small statements. A writer
that genuinely waits half a minute is a symptom, not a slow query."""

MAXIMUM_EVENT_AMOUNT_RUPEES = MAXIMUM_SUPPORTED_CAPITAL_RUPEES
"""No single event may move more than the declared supported capital (`A.23`: ₹1 crore).

Read from `capital_configuration` rather than written here, so the bound is the project's declared
envelope and not a second opinion about it. The `R.23(c)` review posted `Decimal('1E+1000000')` —
finite and positive, so the original guard passed it — which committed to the append-only log, was
answered with a 303 telling the operator it had worked, and then made every subsequent read of the
surface raise `decimal.Overflow`. Recovery existed only by blind-posting into a page that could no
longer render. An engine whose declared range is a lakh to a crore has no business accepting a
figure outside it."""


class PaperCapitalError(Exception):
    """The paper book's capital cannot be established or moved as asked."""


class InsufficientPaperCapitalError(PaperCapitalError):
    """A commitment exceeded free capital. Refused, never truncated to what fits (`§5 I3`)."""


class UnmatchedPaperCapitalReleaseError(PaperCapitalError):
    """A release with no open commitment behind it — it would manufacture free capital."""


class UnreasonedPaperCapitalEventError(PaperCapitalError):
    """An event with no stated reason. Refused at write time (`§5 I6`)."""


class PaperCapitalCheckpointDivergedError(PaperCapitalError):
    """The cached figure disagreed with a replay of the log. The log is the truth; this refuses.

    Raised on reads AND on writes. Refusing the write is the half that matters: the review proved
    that when only reads refused, the next ordinary operator edit rewrote the checkpoint from the
    log and the divergence vanished with no record — so a divergence caused by corruption rather
    than a crash survived exactly until somebody clicked a button. Repair is now an EVENT
    (`repair_checkpoint`), never a side effect.
    """


class CorruptedPaperCapitalLogError(PaperCapitalError):
    """The log itself cannot be folded — two open commitments under one position key.

    Unreachable through the public surface, which refuses a duplicate key under the write lock and
    is backed by a partial unique index. It exists because the fold used to resolve a duplicate by
    LAST-WRITE-WINS, which silently deleted a live reservation: two ₹3,00,000 commits under one key
    folded to ₹3,00,000 committed and ₹7,00,000 free, and one release then returned the lot.
    Refusing to fold is the only answer that does not invent money.
    """


class PaperCapitalEventKind(Enum):
    """Every way the paper book's money can move, kept apart so consumers can read them apart."""

    SEED = "SEED"
    OPERATOR_SET = "OPERATOR_SET"
    OPERATOR_ADJUST = "OPERATOR_ADJUST"
    POSITION_COMMIT = "POSITION_COMMIT"
    POSITION_RELEASE = "POSITION_RELEASE"
    REALISED_PROFIT = "REALISED_PROFIT"
    REALISED_LOSS = "REALISED_LOSS"
    COST_DEBIT = "COST_DEBIT"
    CHECKPOINT_REPAIR = "CHECKPOINT_REPAIR"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_capital_event (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    amount_rupees TEXT NOT NULL,
    previous_balance_rupees TEXT NOT NULL,
    resulting_balance_rupees TEXT NOT NULL,
    unfunded_shortfall_rupees TEXT NOT NULL DEFAULT '0',
    position_key TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_capital_event_by_kind ON paper_capital_event (kind);

-- A position key may be committed at most ONCE in the whole log. Enforced by the database and not
-- only by the write path, because the fold cannot represent two open reservations under one key
-- and the review proved a race can produce them.
CREATE UNIQUE INDEX IF NOT EXISTS paper_capital_one_commit_per_position
    ON paper_capital_event (position_key)
    WHERE kind = 'POSITION_COMMIT';

CREATE TABLE IF NOT EXISTS paper_capital_checkpoint (
    checkpoint_id INTEGER PRIMARY KEY CHECK (checkpoint_id = 1),
    balance_rupees TEXT NOT NULL,
    committed_rupees TEXT NOT NULL,
    last_sequence INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PaperCapitalEvent:
    """One appended fact. Never amended; a correction is a later compensating event."""

    sequence: int
    kind: PaperCapitalEventKind
    occurred_at: datetime
    amount_rupees: Decimal
    previous_balance_rupees: Decimal
    resulting_balance_rupees: Decimal
    unfunded_shortfall_rupees: Decimal
    position_key: str
    reason: str

    @property
    def realised_rupees(self) -> Decimal:
        """What actually left or entered the book — the stated amount less what was unfunded.

        `L1.11` reads this rather than `amount_rupees`. A ₹5,00,000 loss against a ₹1,00,000 book
        removes ₹1,00,000 and records ₹4,00,000 as unfunded, and a cost attribution that reports the
        stated figure overstates the damage by the whole shortfall — measured at 9-fold in the
        review's
        reproduction.
        """
        return self.amount_rupees - self.unfunded_shortfall_rupees


@dataclass(frozen=True, slots=True)
class PaperCapitalFold:
    """The replay's answer — what the log says, independent of anything cached."""

    balance_rupees: Decimal
    committed_rupees: Decimal
    open_commitments: tuple[tuple[str, Decimal], ...]
    last_sequence: int


@dataclass(slots=True)
class _FoldAccumulator:
    """The running state of a replay.

    Shared by the full fold and by the incremental checkpoint written inside a write transaction, so
    the two can never drift apart by being two implementations of the same rule.
    """

    balance: Decimal = Decimal(0)
    commitments: dict[str, Decimal] = field(default_factory=dict)
    last_sequence: int = 0

    def apply(self, event: PaperCapitalEvent) -> None:
        self.last_sequence = event.sequence
        kind = event.kind
        if kind in {PaperCapitalEventKind.SEED, PaperCapitalEventKind.OPERATOR_SET}:
            self.balance = event.amount_rupees
        elif kind in {
            PaperCapitalEventKind.OPERATOR_ADJUST,
            PaperCapitalEventKind.REALISED_PROFIT,
        }:
            self.balance += event.amount_rupees
        elif kind in {
            PaperCapitalEventKind.REALISED_LOSS,
            PaperCapitalEventKind.COST_DEBIT,
        }:
            self.balance = max(self.balance - event.amount_rupees, Decimal(0))
        elif kind is PaperCapitalEventKind.POSITION_COMMIT:
            if event.position_key in self.commitments:
                raise CorruptedPaperCapitalLogError(
                    f"position key {event.position_key!r} is committed twice in the log "
                    f"(sequence {event.sequence}); folding it would delete a live reservation "
                    "rather than report one, so this refuses to produce a balance at all"
                )
            self.commitments[event.position_key] = event.amount_rupees
        elif kind is PaperCapitalEventKind.POSITION_RELEASE:
            self.commitments.pop(event.position_key, None)
        elif kind is PaperCapitalEventKind.CHECKPOINT_REPAIR:
            pass  # a record of a repair, not a movement of money

    def snapshot(self) -> PaperCapitalFold:
        return PaperCapitalFold(
            balance_rupees=self.balance,
            committed_rupees=sum(self.commitments.values(), Decimal(0)),
            open_commitments=tuple(sorted(self.commitments.items())),
            last_sequence=self.last_sequence,
        )

    @property
    def free(self) -> Decimal:
        return max(self.balance - sum(self.commitments.values(), Decimal(0)), Decimal(0))


@dataclass(frozen=True, slots=True)
class PaperCapitalSnapshot:
    """What the paper book may risk now.

    Exposes the same four members `DeployableCapital` does — `deployable_rupees`, `is_tradeable`,
    `binding_side`, `describe()` — so `F04`'s loop takes one capital source and chooses the mode at
    construction rather than branching on it at every sizing decision.
    """

    measured_at: datetime
    balance_rupees: Decimal
    committed_rupees: Decimal
    live_ceiling_rupees: Decimal
    open_commitments: tuple[tuple[str, Decimal], ...] = field(default=())

    @property
    def free_rupees(self) -> Decimal:
        """Balance less what open positions have reserved, floored at zero."""
        return max(self.balance_rupees - self.committed_rupees, Decimal(0))

    @property
    def deployable_rupees(self) -> Decimal:
        return self.free_rupees

    @property
    def over_committed(self) -> bool:
        """Accepted, not impossible — an operator may always restate their virtual capital."""
        return self.committed_rupees > self.balance_rupees

    @property
    def exceeds_live_ceiling(self) -> bool:
        """A record made on money the operator cannot fund is not achievable evidence."""
        return self.balance_rupees > self.live_ceiling_rupees

    @property
    def is_tradeable(self) -> bool:
        return self.free_rupees > 0

    @property
    def binding_side(self) -> str:
        """Which constraint is doing the work — the thing an operator actually wants to know."""
        if self.balance_rupees <= 0:
            return "paper_exhausted"
        if self.over_committed:
            return "paper_over_committed"
        if self.free_rupees == 0:
            return "paper_fully_committed"
        return "paper_free_capital"

    def describe(self) -> str:
        commitments = " · ".join(f"{key} {amount}" for key, amount in self.open_commitments)
        stamp = (
            " — WARNING: this balance exceeds the live ceiling of "
            f"Rs {self.live_ceiling_rupees}, so results produced on it are not achievable with the "
            "capital the operator can currently fund"
            if self.exceeds_live_ceiling
            else ""
        )
        return (
            f"paper free Rs {self.free_rupees} "
            f"(balance {self.balance_rupees}, committed {self.committed_rupees}, "
            f"bound by {self.binding_side}) — "
            f"{commitments or 'no open commitments'}{stamp}"
        )


@dataclass(frozen=True, slots=True)
class _PlannedEvent:
    """What a write decided, once it had a fold nobody else could move underneath it."""

    amount: Decimal
    resulting_balance: Decimal
    unfunded_shortfall: Decimal = Decimal(0)
    position_key: str = ""


class PaperCapitalLedger:
    """The append-only log and the fold over it. There is no method that edits or deletes."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._path = database_path or DEFAULT_PAPER_CAPITAL_LEDGER_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # `isolation_level=None` turns OFF the driver's implicit transaction management so this
        # module can open its own `BEGIN IMMEDIATE` and hold it across the fold, the decision and
        # the insert. With the driver managing them, the fold's SELECT ran outside any write lock.
        self._connection = sqlite3.connect(
            self._path, isolation_level=None, timeout=WRITE_LOCK_TIMEOUT_SECONDS
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(f"PRAGMA busy_timeout = {int(WRITE_LOCK_TIMEOUT_SECONDS * 1000)}")
        try:
            self._connection.executescript(_SCHEMA)
        except sqlite3.IntegrityError as failure:
            self._connection.close()
            raise CorruptedPaperCapitalLogError(
                f"the log at {self._path} cannot satisfy the one-commit-per-position rule, so it "
                "already holds two open commitments under one key and no balance folded from it "
                f"would be trustworthy ({failure})"
            ) from failure

    def __enter__(self) -> PaperCapitalLedger:
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

    # ------------------------------------------------------------------ writes

    def seed_from_ceiling(
        self, ceiling: TradingCapital, *, occurred_at: datetime | None = None, reason: str
    ) -> PaperCapitalEvent:
        """The ledger's one origin event, sized from the operator ceiling rather than a literal.

        The amount is READ from `capital_configuration` so that `R.03` holds here too: no rupee
        figure is written into this module, and a system configured for a lakh does not silently
        paper-trade a crore.
        """
        amount = _bounded_positive_rupees(ceiling.total_rupees, "seed amount")

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            if accumulator.last_sequence != 0:
                raise PaperCapitalError(
                    "this ledger is already seeded; a second origin would make the balance depend "
                    "on which seed was read, and a compensating OPERATOR_SET restates it instead"
                )
            return _PlannedEvent(amount=amount, resulting_balance=amount)

        return self._append(
            PaperCapitalEventKind.SEED, occurred_at=occurred_at, reason=reason, plan=plan
        )

    def set_balance(
        self, amount: Decimal, *, occurred_at: datetime | None = None, reason: str
    ) -> PaperCapitalEvent:
        """The dashboard edit — the operator states the virtual capital they want to run.

        Deliberately NOT capped at the live ceiling: `R.03` requires every engine to work from a
        lakh to a crore, and that range cannot be exercised if the paper book is pinned to today's
        funding. It is STAMPED instead, on the snapshot and on the surface. It IS bounded by the
        declared supported maximum, which is a different thing — see `MAXIMUM_EVENT_AMOUNT_RUPEES`.

        Deliberately NOT refused when it falls below what open positions have committed: the
        operator must always be able to state the truth about their virtual capital, and being
        over-committed is a visible state rather than an impossible one. New commitments are refused
        until positions close.
        """
        new_balance = _bounded_positive_rupees(amount, "balance")

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            _require_seeded(accumulator, self._path)
            return _PlannedEvent(amount=new_balance, resulting_balance=new_balance)

        return self._append(
            PaperCapitalEventKind.OPERATOR_SET, occurred_at=occurred_at, reason=reason, plan=plan
        )

    def adjust_balance(
        self, delta: Decimal, *, occurred_at: datetime | None = None, reason: str
    ) -> PaperCapitalEvent:
        """A relative move, for modelling a deposit or a withdrawal mid-run."""
        movement = _bounded_rupees(delta, "adjustment")
        if movement == 0:
            raise PaperCapitalError("an adjustment of zero records nothing and is refused")

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            _require_seeded(accumulator, self._path)
            resulting = accumulator.balance + movement
            if resulting < 0:
                raise PaperCapitalError(
                    f"an adjustment of {movement} against a balance of {accumulator.balance} would "
                    "take the paper book below zero; state the figure with set_balance instead"
                )
            return _PlannedEvent(amount=movement, resulting_balance=resulting)

        return self._append(
            PaperCapitalEventKind.OPERATOR_ADJUST, occurred_at=occurred_at, reason=reason, plan=plan
        )

    def commit_to_position(
        self,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime | None = None,
        reason: str,
    ) -> PaperCapitalEvent:
        """Reserve capital for an opening paper position. Refused if it exceeds free capital.

        Refused rather than truncated: a book that quietly shrinks the order it cannot fund is not
        simulating the live discipline it exists to simulate, and the record it produces would
        overstate how often a signal was actually taken.

        The check and the insert are one serialised transaction. They were not, and eight concurrent
        reservations of ₹2,00,000 against a ₹10,00,000 book were all accepted.
        """
        reservation = _bounded_positive_rupees(amount, "commitment")
        key = _required_key(position_key)

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            _require_seeded(accumulator, self._path)
            if key in accumulator.commitments:
                raise PaperCapitalError(
                    f"position key {key!r} already holds an open commitment; two commitments under "
                    "one key make the release ambiguous, and an ambiguous release leaks a "
                    "reservation"
                )
            free = accumulator.free
            if reservation > free:
                raise InsufficientPaperCapitalError(
                    f"a commitment of Rs {reservation} exceeds free paper capital of Rs {free} "
                    f"(balance {accumulator.balance}, committed "
                    f"{sum(accumulator.commitments.values(), Decimal(0))}); the paper book refuses "
                    "rather than sizing down"
                )
            return _PlannedEvent(
                amount=reservation,
                resulting_balance=accumulator.balance,
                position_key=key,
            )

        return self._append(
            PaperCapitalEventKind.POSITION_COMMIT,
            occurred_at=occurred_at,
            reason=reason,
            plan=plan,
        )

    def release_position(
        self, *, position_key: str, occurred_at: datetime | None = None, reason: str
    ) -> PaperCapitalEvent:
        """Return a reservation when the position closes."""
        key = _required_key(position_key)

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            held = accumulator.commitments.get(key)
            if held is None:
                raise UnmatchedPaperCapitalReleaseError(
                    f"no open commitment under position key {key!r}; releasing one that was never "
                    "made would manufacture free capital out of nothing"
                )
            return _PlannedEvent(
                amount=held, resulting_balance=accumulator.balance, position_key=key
            )

        return self._append(
            PaperCapitalEventKind.POSITION_RELEASE,
            occurred_at=occurred_at,
            reason=reason,
            plan=plan,
        )

    def record_realised_profit(
        self,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime | None = None,
        reason: str,
    ) -> PaperCapitalEvent:
        return self._record_realisation(
            PaperCapitalEventKind.REALISED_PROFIT,
            amount,
            position_key=position_key,
            occurred_at=occurred_at,
            reason=reason,
        )

    def record_realised_loss(
        self,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime | None = None,
        reason: str,
    ) -> PaperCapitalEvent:
        return self._record_realisation(
            PaperCapitalEventKind.REALISED_LOSS,
            amount,
            position_key=position_key,
            occurred_at=occurred_at,
            reason=reason,
        )

    def record_cost_debit(
        self,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime | None = None,
        reason: str,
    ) -> PaperCapitalEvent:
        """Simulated brokerage, taxes, slippage and impact.

        A separate kind from `REALISED_LOSS` on purpose: `L1.11` decomposes result into edge, fees,
        slippage and impact, and it cannot do that if the cost is already folded into the loss.
        """
        return self._record_realisation(
            PaperCapitalEventKind.COST_DEBIT,
            amount,
            position_key=position_key,
            occurred_at=occurred_at,
            reason=reason,
        )

    def repair_checkpoint(
        self, *, occurred_at: datetime | None = None, reason: str
    ) -> PaperCapitalEvent:
        """The ONLY way a diverged checkpoint is brought back — as an event, never silently.

        Records what the checkpoint claimed and what the log actually says, then rewrites the
        checkpoint from the log. Before this existed, the next ordinary operator edit repaired the
        divergence as a side effect and left nothing behind, so a corruption that was not a crash
        was erased by the first person to touch the page.
        """
        stated_reason = _required_reason(PaperCapitalEventKind.CHECKPOINT_REPAIR, reason)
        with self._write_transaction():
            accumulator = self._replay()
            claimed = self._stored_checkpoint()
            fold = accumulator.snapshot()
            claimed_balance = (
                Decimal(claimed["balance_rupees"]) if claimed is not None else Decimal(0)
            )
            divergence = abs(claimed_balance - fold.balance_rupees)
            event = self._insert(
                PaperCapitalEventKind.CHECKPOINT_REPAIR,
                occurred_at=self._monotonic_stamp(occurred_at),
                planned=_PlannedEvent(
                    amount=divergence, resulting_balance=fold.balance_rupees
                ),
                previous_balance=fold.balance_rupees,
                reason=(
                    f"{stated_reason} — checkpoint claimed balance "
                    f"{claimed_balance} at sequence "
                    f"{claimed['last_sequence'] if claimed is not None else 'none'}, the log says "
                    f"{fold.balance_rupees} at {fold.last_sequence}"
                ),
            )
            accumulator.apply(event)
            self._store_checkpoint(accumulator.snapshot())
        return event

    # ------------------------------------------------------------------- reads

    def snapshot(
        self, *, measured_at: datetime, live_ceiling: TradingCapital
    ) -> PaperCapitalSnapshot:
        """What may be risked now — replayed, then checked against the checkpoint.

        The replay is not an optimisation failure. The checkpoint exists so a consumer can read a
        figure cheaply; this method exists so the figure is never believed over the log that
        produced it.
        """
        accumulator = self._replay()
        _require_seeded(accumulator, self._path)
        fold = accumulator.snapshot()
        self._require_checkpoint_agrees(fold)
        return PaperCapitalSnapshot(
            measured_at=measured_at,
            balance_rupees=fold.balance_rupees,
            committed_rupees=fold.committed_rupees,
            live_ceiling_rupees=live_ceiling.total_rupees,
            open_commitments=fold.open_commitments,
        )

    def fold_from_events(self) -> PaperCapitalFold:
        """Replay the whole log. The only place the balance is ever computed."""
        return self._replay().snapshot()

    def stored_checkpoint_figures(self) -> tuple[Decimal, Decimal, int] | None:
        """What is CACHED on disk, read straight from the table — not recomputed.

        Exists so a test can compare the cache against a replay rather than comparing a replay
        against itself. The property test used to do the latter and passed happily against a
        deliberately corrupted checkpoint.
        """
        row = self._stored_checkpoint()
        if row is None:
            return None
        return (
            Decimal(row["balance_rupees"]),
            Decimal(row["committed_rupees"]),
            int(row["last_sequence"]),
        )

    def events(self) -> tuple[PaperCapitalEvent, ...]:
        rows = self._connection.execute(
            "SELECT sequence, kind, occurred_at, amount_rupees, previous_balance_rupees, "
            "resulting_balance_rupees, unfunded_shortfall_rupees, position_key, reason "
            "FROM paper_capital_event ORDER BY sequence"
        ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def total_by_kind(self, kind: PaperCapitalEventKind) -> Decimal:
        """What `L1.11` reads: money that ACTUALLY moved, not money that was asked for.

        Sums `realised_rupees`, so a loss larger than the book contributes only what the book
        actually had. Summing the stated amount instead reported a ₹5,00,000 loss against a
        ₹1,00,000 book as ₹5,00,000 of damage when ₹1,00,000 left — a 9-fold error in the net P&L
        implied by the kinds, found by the `R.23(c)` review.
        """
        return sum(
            (event.realised_rupees for event in self._events_of_kind(kind)),
            Decimal(0),
        )

    def total_stated_by_kind(self, kind: PaperCapitalEventKind) -> Decimal:
        """What was ASKED for, shortfall included — the counterpart to `total_by_kind`."""
        return sum((event.amount_rupees for event in self._events_of_kind(kind)), Decimal(0))

    def total_unfunded_shortfall_by_kind(self, kind: PaperCapitalEventKind) -> Decimal:
        """How much of that kind the book could not fund. The difference between the two totals."""
        return sum(
            (event.unfunded_shortfall_rupees for event in self._events_of_kind(kind)), Decimal(0)
        )

    # ---------------------------------------------------------------- internal

    def _events_of_kind(self, kind: PaperCapitalEventKind) -> tuple[PaperCapitalEvent, ...]:
        rows = self._connection.execute(
            "SELECT sequence, kind, occurred_at, amount_rupees, previous_balance_rupees, "
            "resulting_balance_rupees, unfunded_shortfall_rupees, position_key, reason "
            "FROM paper_capital_event WHERE kind = ? ORDER BY sequence",
            (kind.value,),
        ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def _record_realisation(
        self,
        kind: PaperCapitalEventKind,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime | None,
        reason: str,
    ) -> PaperCapitalEvent:
        movement = _bounded_positive_rupees(amount, kind.value.lower().replace("_", " "))
        key = _required_key(position_key)

        def plan(accumulator: _FoldAccumulator) -> _PlannedEvent:
            _require_seeded(accumulator, self._path)
            if kind is PaperCapitalEventKind.REALISED_PROFIT:
                return _PlannedEvent(
                    amount=movement,
                    resulting_balance=accumulator.balance + movement,
                    position_key=key,
                )
            return _PlannedEvent(
                amount=movement,
                resulting_balance=max(accumulator.balance - movement, Decimal(0)),
                unfunded_shortfall=max(movement - accumulator.balance, Decimal(0)),
                position_key=key,
            )

        return self._append(kind, occurred_at=occurred_at, reason=reason, plan=plan)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """One writer at a time, from the fold to the checkpoint.

        `BEGIN IMMEDIATE` takes the RESERVED lock up front, so a second writer waits here instead of
        reading a balance the first is about to spend. A deferred transaction would not: its lock is
        taken at the first WRITE, which is after the decision has already been made on stale state.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _append(
        self,
        kind: PaperCapitalEventKind,
        *,
        occurred_at: datetime | None,
        reason: str,
        plan: Callable[[_FoldAccumulator], _PlannedEvent],
    ) -> PaperCapitalEvent:
        stated_reason = _required_reason(kind, reason)
        with self._write_transaction():
            accumulator = self._replay()
            fold = accumulator.snapshot()
            if fold.last_sequence != 0:
                self._require_checkpoint_agrees(fold)
            planned = plan(accumulator)
            event = self._insert(
                kind,
                occurred_at=self._monotonic_stamp(occurred_at),
                planned=planned,
                previous_balance=fold.balance_rupees,
                reason=stated_reason,
            )
            accumulator.apply(event)
            self._store_checkpoint(accumulator.snapshot())
        return event

    def _insert(
        self,
        kind: PaperCapitalEventKind,
        *,
        occurred_at: datetime,
        planned: _PlannedEvent,
        previous_balance: Decimal,
        reason: str,
    ) -> PaperCapitalEvent:
        try:
            cursor = self._connection.execute(
                "INSERT INTO paper_capital_event (kind, occurred_at, amount_rupees, "
                "previous_balance_rupees, resulting_balance_rupees, unfunded_shortfall_rupees, "
                "position_key, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind.value,
                    occurred_at.isoformat(),
                    str(planned.amount),
                    str(previous_balance),
                    str(planned.resulting_balance),
                    str(planned.unfunded_shortfall),
                    planned.position_key,
                    reason,
                ),
            )
        except sqlite3.IntegrityError as failure:
            raise PaperCapitalError(
                f"position key {planned.position_key!r} has already been committed in this log; "
                "keys are never reused, so that the commitment a release returns is never in doubt "
                f"({failure})"
            ) from failure
        return PaperCapitalEvent(
            sequence=int(cursor.lastrowid or 0),
            kind=kind,
            occurred_at=occurred_at,
            amount_rupees=planned.amount,
            previous_balance_rupees=previous_balance,
            resulting_balance_rupees=planned.resulting_balance,
            unfunded_shortfall_rupees=planned.unfunded_shortfall,
            position_key=planned.position_key,
            reason=reason,
        )

    def _monotonic_stamp(self, occurred_at: datetime | None) -> datetime:
        """The event's time, which can never precede the event before it.

        A caller that STATES a time is held to it: a backdated stamp is refused, because a ledger
        whose order can be argued with is a ledger whose balance can be argued with.

        A caller that states nothing gets the time this transaction is running, clamped forward to
        the previous event's stamp if the two are indistinguishable. This is the ordinary path and
        it exists because the review made two operator edits 211 microseconds apart and the SECOND
        was rejected — each thread had read `now` before the other committed, so the guard blamed
        clock ordering for a race. The log's ORDER is `sequence`, which the transaction assigns; the
        stamp is what a human reads, and it must not be able to reject a legitimate edit.
        """
        latest = self._latest_occurred_at()
        if occurred_at is None:
            now = datetime.now(IST)
            return now if latest is None or now >= latest else latest
        stamped = _required_ist(occurred_at)
        if latest is not None and stamped < latest:
            raise PaperCapitalError(
                f"an event stamped {stamped.isoformat()} is earlier than the last event at "
                f"{latest.isoformat()}; a ledger whose order can be argued with is a ledger whose "
                "balance can be argued with"
            )
        return stamped

    def _replay(self) -> _FoldAccumulator:
        accumulator = _FoldAccumulator()
        for event in self.events():
            accumulator.apply(event)
        return accumulator

    def _store_checkpoint(self, fold: PaperCapitalFold) -> None:
        self._connection.execute(
            "INSERT INTO paper_capital_checkpoint "
            "(checkpoint_id, balance_rupees, committed_rupees, last_sequence) VALUES (1, ?, ?, ?) "
            "ON CONFLICT (checkpoint_id) DO UPDATE SET balance_rupees = excluded.balance_rupees, "
            "committed_rupees = excluded.committed_rupees, last_sequence = excluded.last_sequence",
            (str(fold.balance_rupees), str(fold.committed_rupees), fold.last_sequence),
        )

    def _stored_checkpoint(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT balance_rupees, committed_rupees, last_sequence FROM paper_capital_checkpoint "
            "WHERE checkpoint_id = 1"
        ).fetchone()
        return row

    def _require_checkpoint_agrees(self, fold: PaperCapitalFold) -> None:
        row = self._stored_checkpoint()
        if row is None:
            raise PaperCapitalCheckpointDivergedError(
                "the log holds events but no checkpoint was written; the two disagree about "
                "whether this ledger has ever been used. `repair_checkpoint` records the "
                "discrepancy and rebuilds it from the log"
            )
        cached_balance = Decimal(row["balance_rupees"])
        cached_committed = Decimal(row["committed_rupees"])
        if (
            cached_balance != fold.balance_rupees
            or cached_committed != fold.committed_rupees
            or int(row["last_sequence"]) != fold.last_sequence
        ):
            raise PaperCapitalCheckpointDivergedError(
                f"the checkpoint says balance {cached_balance} / committed {cached_committed} at "
                f"sequence {row['last_sequence']}, and replaying the log says "
                f"{fold.balance_rupees} / {fold.committed_rupees} at {fold.last_sequence}; the log "
                "is the truth and this refuses rather than reporting either. Writes are refused "
                "too, so nothing repairs this as a side effect — `repair_checkpoint` records what "
                "was claimed and what the log says, then rebuilds it"
            )

    def _latest_occurred_at(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT occurred_at FROM paper_capital_event ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row["occurred_at"])


def _event_from_row(row: sqlite3.Row) -> PaperCapitalEvent:
    return PaperCapitalEvent(
        sequence=int(row["sequence"]),
        kind=PaperCapitalEventKind(row["kind"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        amount_rupees=Decimal(row["amount_rupees"]),
        previous_balance_rupees=Decimal(row["previous_balance_rupees"]),
        resulting_balance_rupees=Decimal(row["resulting_balance_rupees"]),
        unfunded_shortfall_rupees=Decimal(row["unfunded_shortfall_rupees"]),
        position_key=str(row["position_key"]),
        reason=str(row["reason"]),
    )


def _require_seeded(accumulator: _FoldAccumulator, path: Path) -> None:
    if accumulator.last_sequence == 0:
        raise PaperCapitalError(
            f"the paper capital ledger at {path} has never been seeded; 'not seeded' and 'seeded "
            "at zero' are the same number only to a system that then sizes against the difference"
        )


def _required_reason(kind: PaperCapitalEventKind, reason: str) -> str:
    stated = reason.strip()
    if not stated:
        raise UnreasonedPaperCapitalEventError(
            f"a {kind.value} with no stated reason is refused: an unexplained balance change is "
            "indistinguishable from a defect when it is read back months later"
        )
    return stated


def _bounded_rupees(value: Decimal, description: str) -> Decimal:
    """Finite, a real `Decimal`, and inside the capital range this project declares it supports."""
    if not isinstance(value, Decimal):
        raise PaperCapitalError(
            f"{description} must be a Decimal; a float would make the paper book's arithmetic "
            "depend on binary rounding"
        )
    try:
        if not value.is_finite():
            raise PaperCapitalError(f"{description} must be finite, and {value} is not")
    except InvalidOperation as failure:  # pragma: no cover — Decimal guards this already
        raise PaperCapitalError(f"{description} is not a usable Decimal") from failure
    # `copy_abs()` and not `abs()`: `abs()` is a context operation and raises `decimal.Overflow` on
    # the very input this guard exists to reject, so the guard would fail before it could refuse.
    if value.copy_abs() > MAXIMUM_EVENT_AMOUNT_RUPEES:
        raise PaperCapitalError(
            f"{description} of {value} exceeds the largest capital this project declares it "
            f"supports (Rs {MAXIMUM_EVENT_AMOUNT_RUPEES}, `A.23`). A figure outside the declared "
            "range runs every engine outside the envelope it was built and calibrated for, and an "
            "unbounded one makes the surface unreadable rather than merely wrong"
        )
    return value


def _bounded_positive_rupees(value: Decimal, description: str) -> Decimal:
    amount = _bounded_rupees(value, description)
    if amount <= 0:
        raise PaperCapitalError(
            f"{description} must be positive, and {amount} is not; a zero or negative figure here "
            "would be a statement about the book rather than a movement in it"
        )
    return amount


def _required_key(position_key: str) -> str:
    key = position_key.strip()
    if not key:
        raise PaperCapitalError(
            "a commitment or release with no position key cannot be matched to the position it "
            "belongs to, which is the only thing that makes the reservation releasable"
        )
    return key


def _required_ist(occurred_at: datetime) -> datetime:
    if occurred_at.tzinfo is None:
        raise PaperCapitalError(
            "a naive timestamp is refused: this system trades one session in one timezone, and an "
            "event whose clock is unstated cannot be ordered against one whose clock is stated"
        )
    return occurred_at.astimezone(IST)
