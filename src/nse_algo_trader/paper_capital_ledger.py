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
an optimisation and is *verified against a fresh replay on every read* rather than trusted — a
cached figure that can silently diverge from its source is a defect, not a saving.

**Commitment is tracked apart from balance.** The common naive paper book decrements the
balance when a position opens and credits P&L back on close. Between those two moments the
capital is neither in the balance nor visibly at risk, so a second signal sizes against money
already deployed. Here `free = balance - committed`; sizing reads `free`; balance moves only on
realisation.

**Sourcing** (`R.17`, evidence in `docs/research/226` §9): `eventsourcing` 9.5.4 and `beancount`
3.2.3 were installed and introspected. The first was rejected because `Decimal` needs a custom
transcoding and its recorder owns an opaque schema that forecloses `L1.11` reading `COST_DEBIT`
apart from `REALISED_LOSS`; the second because its only entry points parse a plaintext DSL and
it has no reservation concept. The append-only-log-plus-verified-checkpoint shape is taken from
the first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import TracebackType
from zoneinfo import ZoneInfo

from nse_algo_trader.capital_configuration import TradingCapital

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_PAPER_CAPITAL_LEDGER_PATH = (
    Path.home() / ".nse_algo_trader" / "paper_capital_ledger.sqlite3"
)

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

CREATE TABLE IF NOT EXISTS paper_capital_checkpoint (
    checkpoint_id INTEGER PRIMARY KEY CHECK (checkpoint_id = 1),
    balance_rupees TEXT NOT NULL,
    committed_rupees TEXT NOT NULL,
    last_sequence INTEGER NOT NULL
);
"""


class PaperCapitalError(Exception):
    """The paper book's capital cannot be established or moved as asked."""


class InsufficientPaperCapitalError(PaperCapitalError):
    """A commitment exceeded free capital. Refused, never truncated to what fits (`§5 I3`)."""


class UnmatchedPaperCapitalReleaseError(PaperCapitalError):
    """A release with no open commitment behind it — it would manufacture free capital."""


class UnreasonedPaperCapitalEventError(PaperCapitalError):
    """An event with no stated reason. Refused at write time (`§5 I6`)."""


class PaperCapitalCheckpointDivergedError(PaperCapitalError):
    """The cached figure disagreed with a replay of the log. The log is the truth; this refuses."""


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


@dataclass(frozen=True, slots=True)
class PaperCapitalFold:
    """The replay's answer — what the log says, independent of anything cached."""

    balance_rupees: Decimal
    committed_rupees: Decimal
    open_commitments: tuple[tuple[str, Decimal], ...]
    last_sequence: int


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


class PaperCapitalLedger:
    """The append-only log and the fold over it. There is no method that edits or deletes."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._path = database_path or DEFAULT_PAPER_CAPITAL_LEDGER_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

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
        self, ceiling: TradingCapital, *, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        """The ledger's one origin event, sized from the operator ceiling rather than a literal.

        The amount is READ from `capital_configuration` so that `R.03` holds here too: no rupee
        figure is written into this module, and a system configured for a lakh does not silently
        paper-trade a crore.
        """
        if self._last_sequence() != 0:
            raise PaperCapitalError(
                "this ledger is already seeded; a second origin would make the balance depend on "
                "which seed was read, and a compensating OPERATOR_SET is the way to restate it"
            )
        amount = _positive_rupees(ceiling.total_rupees, "seed amount")
        return self._append(
            PaperCapitalEventKind.SEED,
            occurred_at=occurred_at,
            amount=amount,
            previous_balance=Decimal(0),
            resulting_balance=amount,
            reason=reason,
        )

    def set_balance(
        self, amount: Decimal, *, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        """The dashboard edit — the operator states the virtual capital they want to run.

        Deliberately NOT capped at the live ceiling: `R.03` requires every engine to work from a
        lakh to a crore, and that range cannot be exercised if the paper book is pinned to today's
        funding. It is STAMPED instead, on the snapshot and on the surface.

        Deliberately NOT refused when it falls below what open positions have committed: the
        operator must always be able to state the truth about their virtual capital, and being
        over-committed is a visible state rather than an impossible one. New commitments are refused
        until positions close.
        """
        fold = self.fold_from_events()
        new_balance = _positive_rupees(amount, "balance")
        return self._append(
            PaperCapitalEventKind.OPERATOR_SET,
            occurred_at=occurred_at,
            amount=new_balance,
            previous_balance=fold.balance_rupees,
            resulting_balance=new_balance,
            reason=reason,
        )

    def adjust_balance(
        self, delta: Decimal, *, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        """A relative move, for modelling a deposit or a withdrawal mid-run."""
        movement = _finite_rupees(delta, "adjustment")
        if movement == 0:
            raise PaperCapitalError("an adjustment of zero records nothing and is refused")
        fold = self.fold_from_events()
        self._require_seeded(fold)
        resulting = fold.balance_rupees + movement
        if resulting < 0:
            raise PaperCapitalError(
                f"an adjustment of {movement} against a balance of {fold.balance_rupees} would "
                "take the paper book below zero; state the figure with set_balance instead"
            )
        return self._append(
            PaperCapitalEventKind.OPERATOR_ADJUST,
            occurred_at=occurred_at,
            amount=movement,
            previous_balance=fold.balance_rupees,
            resulting_balance=resulting,
            reason=reason,
        )

    def commit_to_position(
        self, amount: Decimal, *, position_key: str, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        """Reserve capital for an opening paper position. Refused if it exceeds free capital.

        Refused rather than truncated: a book that quietly shrinks the order it cannot fund is not
        simulating the live discipline it exists to simulate, and the record it produces would
        overstate how often a signal was actually taken.
        """
        reservation = _positive_rupees(amount, "commitment")
        key = _required_key(position_key)
        fold = self.fold_from_events()
        self._require_seeded(fold)
        if any(open_key == key for open_key, _ in fold.open_commitments):
            raise PaperCapitalError(
                f"position key {key!r} already holds an open commitment; two commitments under one "
                "key make the release ambiguous, and an ambiguous release is how a reservation "
                "leaks"
            )
        if self._key_was_ever_committed(key):
            raise PaperCapitalError(
                f"position key {key!r} has been used before; keys are not reused, so that the "
                "commitment a release returns is never in doubt"
            )
        free = max(fold.balance_rupees - fold.committed_rupees, Decimal(0))
        if reservation > free:
            raise InsufficientPaperCapitalError(
                f"a commitment of Rs {reservation} exceeds free paper capital of Rs {free} "
                f"(balance {fold.balance_rupees}, committed {fold.committed_rupees}); the paper "
                "book refuses rather than sizing down"
            )
        return self._append(
            PaperCapitalEventKind.POSITION_COMMIT,
            occurred_at=occurred_at,
            amount=reservation,
            previous_balance=fold.balance_rupees,
            resulting_balance=fold.balance_rupees,
            position_key=key,
            reason=reason,
        )

    def release_position(
        self, *, position_key: str, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        """Return a reservation when the position closes."""
        key = _required_key(position_key)
        fold = self.fold_from_events()
        held = dict(fold.open_commitments).get(key)
        if held is None:
            raise UnmatchedPaperCapitalReleaseError(
                f"no open commitment under position key {key!r}; releasing one that was never made "
                "would manufacture free capital out of nothing"
            )
        return self._append(
            PaperCapitalEventKind.POSITION_RELEASE,
            occurred_at=occurred_at,
            amount=held,
            previous_balance=fold.balance_rupees,
            resulting_balance=fold.balance_rupees,
            position_key=key,
            reason=reason,
        )

    def record_realised_profit(
        self, amount: Decimal, *, position_key: str, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        return self._record_realisation(
            PaperCapitalEventKind.REALISED_PROFIT,
            amount,
            position_key=position_key,
            occurred_at=occurred_at,
            reason=reason,
        )

    def record_realised_loss(
        self, amount: Decimal, *, position_key: str, occurred_at: datetime, reason: str
    ) -> PaperCapitalEvent:
        return self._record_realisation(
            PaperCapitalEventKind.REALISED_LOSS,
            amount,
            position_key=position_key,
            occurred_at=occurred_at,
            reason=reason,
        )

    def record_cost_debit(
        self, amount: Decimal, *, position_key: str, occurred_at: datetime, reason: str
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

    # ------------------------------------------------------------------- reads

    def snapshot(
        self, *, measured_at: datetime, live_ceiling: TradingCapital
    ) -> PaperCapitalSnapshot:
        """What may be risked now — replayed, then checked against the checkpoint.

        The replay is not an optimisation failure. The checkpoint exists so a consumer can read a
        figure cheaply; this method exists so the figure is never believed over the log that
        produced it.
        """
        fold = self.fold_from_events()
        self._require_seeded(fold)
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
        balance = Decimal(0)
        commitments: dict[str, Decimal] = {}
        last_sequence = 0
        for event in self.events():
            last_sequence = event.sequence
            if event.kind in {
                PaperCapitalEventKind.SEED,
                PaperCapitalEventKind.OPERATOR_SET,
            }:
                balance = event.amount_rupees
            elif event.kind in {
                PaperCapitalEventKind.OPERATOR_ADJUST,
                PaperCapitalEventKind.REALISED_PROFIT,
            }:
                balance += event.amount_rupees
            elif event.kind in {
                PaperCapitalEventKind.REALISED_LOSS,
                PaperCapitalEventKind.COST_DEBIT,
            }:
                balance = max(balance - event.amount_rupees, Decimal(0))
            elif event.kind is PaperCapitalEventKind.POSITION_COMMIT:
                commitments[event.position_key] = event.amount_rupees
            elif event.kind is PaperCapitalEventKind.POSITION_RELEASE:
                commitments.pop(event.position_key, None)
        return PaperCapitalFold(
            balance_rupees=balance,
            committed_rupees=sum(commitments.values(), Decimal(0)),
            open_commitments=tuple(sorted(commitments.items())),
            last_sequence=last_sequence,
        )

    def events(self) -> tuple[PaperCapitalEvent, ...]:
        rows = self._connection.execute(
            "SELECT sequence, kind, occurred_at, amount_rupees, previous_balance_rupees, "
            "resulting_balance_rupees, unfunded_shortfall_rupees, position_key, reason "
            "FROM paper_capital_event ORDER BY sequence"
        ).fetchall()
        return tuple(
            PaperCapitalEvent(
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
            for row in rows
        )

    def total_by_kind(self, kind: PaperCapitalEventKind) -> Decimal:
        """What `L1.11` reads to keep cost apart from loss."""
        row = self._connection.execute(
            "SELECT amount_rupees FROM paper_capital_event WHERE kind = ?", (kind.value,)
        ).fetchall()
        return sum((Decimal(entry["amount_rupees"]) for entry in row), Decimal(0))

    # ---------------------------------------------------------------- internal

    def _record_realisation(
        self,
        kind: PaperCapitalEventKind,
        amount: Decimal,
        *,
        position_key: str,
        occurred_at: datetime,
        reason: str,
    ) -> PaperCapitalEvent:
        movement = _positive_rupees(amount, kind.value.lower().replace("_", " "))
        key = _required_key(position_key)
        fold = self.fold_from_events()
        self._require_seeded(fold)
        if kind is PaperCapitalEventKind.REALISED_PROFIT:
            resulting = fold.balance_rupees + movement
            shortfall = Decimal(0)
        else:
            resulting = max(fold.balance_rupees - movement, Decimal(0))
            shortfall = max(movement - fold.balance_rupees, Decimal(0))
        return self._append(
            kind,
            occurred_at=occurred_at,
            amount=movement,
            previous_balance=fold.balance_rupees,
            resulting_balance=resulting,
            unfunded_shortfall=shortfall,
            position_key=key,
            reason=reason,
        )

    def _append(
        self,
        kind: PaperCapitalEventKind,
        *,
        occurred_at: datetime,
        amount: Decimal,
        previous_balance: Decimal,
        resulting_balance: Decimal,
        unfunded_shortfall: Decimal = Decimal(0),
        position_key: str = "",
        reason: str,
    ) -> PaperCapitalEvent:
        stated_reason = reason.strip()
        if not stated_reason:
            raise UnreasonedPaperCapitalEventError(
                f"a {kind.value} with no stated reason is refused: an unexplained balance "
                "change is indistinguishable from a defect when it is read back months later"
            )
        stamped = _required_ist(occurred_at)
        latest = self._latest_occurred_at()
        if latest is not None and stamped < latest:
            raise PaperCapitalError(
                f"a {kind.value} stamped {stamped.isoformat()} is earlier than the last event at "
                f"{latest.isoformat()}; a ledger whose order can be argued with is a ledger whose "
                "balance can be argued with"
            )
        cursor = self._connection.execute(
            "INSERT INTO paper_capital_event (kind, occurred_at, amount_rupees, "
            "previous_balance_rupees, resulting_balance_rupees, unfunded_shortfall_rupees, "
            "position_key, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind.value,
                stamped.isoformat(),
                str(amount),
                str(previous_balance),
                str(resulting_balance),
                str(unfunded_shortfall),
                position_key,
                stated_reason,
            ),
        )
        sequence = int(cursor.lastrowid or 0)
        self._connection.commit()
        self._write_checkpoint()
        return PaperCapitalEvent(
            sequence=sequence,
            kind=kind,
            occurred_at=stamped,
            amount_rupees=amount,
            previous_balance_rupees=previous_balance,
            resulting_balance_rupees=resulting_balance,
            unfunded_shortfall_rupees=unfunded_shortfall,
            position_key=position_key,
            reason=stated_reason,
        )

    def _write_checkpoint(self) -> None:
        fold = self.fold_from_events()
        self._connection.execute(
            "INSERT INTO paper_capital_checkpoint "
            "(checkpoint_id, balance_rupees, committed_rupees, last_sequence) VALUES (1, ?, ?, ?) "
            "ON CONFLICT (checkpoint_id) DO UPDATE SET balance_rupees = excluded.balance_rupees, "
            "committed_rupees = excluded.committed_rupees, last_sequence = excluded.last_sequence",
            (str(fold.balance_rupees), str(fold.committed_rupees), fold.last_sequence),
        )
        self._connection.commit()

    def _require_checkpoint_agrees(self, fold: PaperCapitalFold) -> None:
        row = self._connection.execute(
            "SELECT balance_rupees, committed_rupees, last_sequence FROM paper_capital_checkpoint "
            "WHERE checkpoint_id = 1"
        ).fetchone()
        if row is None:
            raise PaperCapitalCheckpointDivergedError(
                "the log holds events but no checkpoint was written; the two disagree about "
                "whether this ledger has ever been used"
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
                "is the truth and this refuses rather than reporting either"
            )

    def _require_seeded(self, fold: PaperCapitalFold) -> None:
        if fold.last_sequence == 0:
            raise PaperCapitalError(
                f"the paper capital ledger at {self._path} has never been seeded; 'not seeded' and "
                "'seeded at zero' are the same number only to a system that then sizes against the "
                "difference"
            )

    def _latest_occurred_at(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT occurred_at FROM paper_capital_event ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row["occurred_at"])

    def _last_sequence(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM paper_capital_event"
        ).fetchone()
        return int(row["last"])

    def _key_was_ever_committed(self, position_key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM paper_capital_event WHERE kind = ? AND position_key = ? LIMIT 1",
            (PaperCapitalEventKind.POSITION_COMMIT.value, position_key),
        ).fetchone()
        return row is not None


def _finite_rupees(value: Decimal, description: str) -> Decimal:
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
    return value


def _positive_rupees(value: Decimal, description: str) -> Decimal:
    amount = _finite_rupees(value, description)
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
