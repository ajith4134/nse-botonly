"""The decision-trace contract — `L13.29`, spec `docs/research/257`.

**Reasoning is recorded, never reconstructed.** `A.29` states the binding constraint plainly: a "why
did it trade?" panel assembled from trade records afterwards shows what a *reasonable* bot might
have thought, not what *this* one did — a confident fiction. So every bot emits, at decision time,
an append-only point-in-time record of what it consulted, what it considered, what each gate said,
what it chose, and **the counterfactual that would have changed its mind**.

This extends `R.08` from "status is measured, never hand-authored" to "reasoning is recorded, never
reconstructed", and `L13.30`-`L13.33` are all blocked on it: `A.29` requires it **before any
panel**.

**Why the counterfactual makes this an engine rather than a log** (`R.23a`). A record of fields is a
log. The clause that carries inference is the last one: which gate came closest to flipping, and
what value would have flipped it. `L13.33` puts it in trading terms — *"the near-miss is usually
more informative than the pass"* — and the SOTA analog is a solver's sensitivity report, where the
binding constraint and its shadow price matter more than the optimum.

**The problem the inference actually has to solve.** Gates do not measure in the same units. The
cost gate's margin is basis points, the risk gate's is rupees, sizing's is whole shares. "Which came
closest" across those is meaningless raw — 3 bps is not smaller than 300 rupees. Each margin is
therefore normalised by **its own gate's threshold** into a dimensionless fraction: a gate that
passed with 3 bps of headroom against a 30 bps hurdle sits at `0.10`, while one that passed with
₹500 against a ₹50,000 limit sits at `0.01` and is the tighter constraint, which is the answer a
reader wants.

**A boolean gate has no margin and is not given a fake one.** A halt latch is open or closed;
inventing `0.0` for it would make it permanently "the closest gate" and drown every real near-miss.
Booleans are ranked separately: a closed one is *the* binding constraint by definition, since
nothing downstream of it ran, and an open one can never be the near-miss.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

MAXIMUM_NORMALISED_MARGIN = 1.0
"""A margin is clipped at one whole threshold before ranking, and the reason is measured.

`|margin| / |threshold|` is unbounded, and an adversarial review showed what that does: a gate
missing by a hair against a 1e-6 threshold scored 1000.0 and was ranked LOOSER than one that missed
by 29 against a threshold of 30 (0.967). The gate closest to flipping was reported as the least
binding constraint in the trace.

Clipping at 1.0 says "this gate is at least a full threshold away", which is all the ranking needs:
past one threshold, everything is comfortably clear and the ordering among them carries no
information worth acting on. It is a ranking key, not a reported quantity — `would_have_needed` is
still the true unclipped value in the gate's own unit.
"""

DEFAULT_DECISION_TRACE_PATH = Path("~/.nse_algo_trader/decision_trace.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_trace (
    bot_identity     TEXT NOT NULL,
    decided_at       TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    session_date     TEXT NOT NULL,
    trading_symbol   TEXT NOT NULL,
    chosen_action    TEXT NOT NULL,
    confidence       REAL,
    mechanism        TEXT NOT NULL,
    inputs_json      TEXT NOT NULL,
    candidates_json  TEXT NOT NULL,
    gates_json       TEXT NOT NULL,
    permitted_actions_json TEXT,
    null_action      TEXT NOT NULL DEFAULT 'abstain',
    earliest_knowable TEXT,
    kind             TEXT NOT NULL DEFAULT 'entry',
    decision_ordinal INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_identity, decided_at, instrument_token, kind, decision_ordinal)
);
CREATE INDEX IF NOT EXISTS decision_trace_by_session ON decision_trace (session_date);
-- Append-only enforced by the DATABASE, not merely by the absence of an update method on the
-- class. An adversarial review pointed out that a plain UPDATE or DELETE rewrote history with no
-- resistance at all, which makes "reasoning is recorded, never reconstructed" a property of the
-- Python API rather than of the record.
CREATE TRIGGER IF NOT EXISTS decision_trace_is_append_only_update
BEFORE UPDATE ON decision_trace
BEGIN
    SELECT RAISE(ABORT, 'decision traces are append-only: reasoning recorded once, never revised');
END;
CREATE TRIGGER IF NOT EXISTS decision_trace_is_append_only_delete
BEFORE DELETE ON decision_trace
BEGIN
    SELECT RAISE(ABORT, 'decision traces are append-only: a deleted trace is a reconstructed one');
END;
"""


class GateOutcome(StrEnum):
    """What a gate concluded — three states, because two is the mistake this replaces.

    `refused` used to be a membership test over five lowercase words, and an adversarial review
    measured what that missed: `REJECTED`, `rejected`, `vetoed`, `deny` and `FAIL` all read as
    PASSING, and so did **`GateVerdict.UNPRICEABLE`**. That last one is the serious case.
    `pre_trade_cost_gate` warns in its own docstring that treating "I do not know" as "it is fine"
    is the failure it exists to prevent, and a string test downstream reintroduced it: an
    unpriceable cost gate — a dead quote feed, no opinion formed — was ranked as the binding
    constraint with "0.1 of headroom left before it would have changed the outcome".

    `NO_JUDGEMENT` is therefore its own state and is never the binding constraint, because the
    absence of a judgement is not a judgement.
    """

    PASSED = "passed"
    REFUSED = "refused"
    NO_JUDGEMENT = "no_judgement"


class DecisionKind(StrEnum):
    """Whether the decision was about GETTING IN or GETTING OUT.

    The two are not interchangeable and the record has to say which, because a gate refusal means
    opposite things across them. On an entry a refusal is a veto and the bot stands down. On an
    exit it is not: when the halt latch trips, the session squares off THROUGH the refusing latch,
    which is the correct behaviour — the latch forbids taking risk on, not shedding it.

    Traces written before this distinction existed default to `ENTRY`, which is a true statement
    about them rather than a placeholder: the emitter only ever ran on the entry path, so the exits
    of the first 21,270 traced decisions are not mislabelled here, they are absent (`M25`).
    """

    ENTRY = "entry"
    EXIT = "exit"


class DecisionTraceError(Exception):
    """The trace does not describe something that could have happened, so it is not recorded."""


@dataclass(frozen=True, slots=True)
class ConsultedInput:
    """One value the decision actually looked at, with where it came from and when it was true.

    `as_of` is the instant the VALUE was knowable, not the instant it was read. That distinction is
    the whole provenance clause: a bar read at 11:30 that closed at 11:25 is knowable at 11:25, and
    recording the read time would make every input look fresher than it was.
    """

    name: str
    value: str
    source: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class CandidateAction:
    """An action that was on the table, and why it was considered at all."""

    action: str
    why_considered: str


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    """One gate's verdict, and how close it came to the other answer.

    `margin` is SIGNED and in the gate's own unit: positive means it passed by that much, negative
    means it refused by that much. `None` means the gate is boolean and has no margin — see the
    module docstring for why that is not the same as zero.
    """

    gate: str
    outcome: GateOutcome
    margin: Decimal | None
    threshold: Decimal | None
    detail: str

    def __post_init__(self) -> None:
        for name, value in (("margin", self.margin), ("threshold", self.threshold)):
            if value is not None and not value.is_finite():
                raise DecisionTraceError(
                    f"{self.gate} reports a non-finite {name} ({value}); it round-trips through "
                    f"the store unharmed and then poisons the ranking, which made the binding "
                    f"constraint depend on tuple ORDER rather than on the evidence"
                )
        if self.threshold is not None and self.threshold == 0 and self.margin is not None:
            raise DecisionTraceError(
                f"{self.gate} reports a margin against a ZERO threshold. There is no scale to "
                f"normalise against, and falling back to the raw magnitude silently destroys the "
                f"scale-invariance this ranking depends on — scaling every gate by 1000 changed "
                f"which one bound"
            )
        if self.margin is not None:
            refused_but_positive = self.outcome is GateOutcome.REFUSED and self.margin > 0
            passed_but_negative = self.outcome is GateOutcome.PASSED and self.margin < 0
            if refused_but_positive or passed_but_negative:
                raise DecisionTraceError(
                    f"{self.gate} reports outcome {self.outcome} with margin {self.margin}, which "
                    f"contradict each other. A refusal has a negative margin and a pass a positive "
                    f"one; incoherent signs produced counterfactuals that read 'it needed 2 more' "
                    f"about a gate already 2 past its threshold"
                )

    @property
    def is_boolean(self) -> bool:
        return self.margin is None

    @property
    def refused(self) -> bool:
        return self.outcome is GateOutcome.REFUSED

    def normalised_margin(self) -> float | None:
        """`|margin| / |threshold|` — dimensionless, so gates in different units compare.

        Falls back to the raw magnitude when the threshold is zero or absent rather than dividing by
        zero: a gate with a zero threshold has no scale to normalise against, and excluding it
        entirely would hide a gate that genuinely bound.
        """
        if self.margin is None:
            return None
        magnitude = abs(float(self.margin))
        if self.threshold is None:
            return magnitude
        return min(magnitude / abs(float(self.threshold)), MAXIMUM_NORMALISED_MARGIN)


@dataclass(frozen=True, slots=True)
class BindingConstraint:
    """The gate that decided the outcome, and what would have changed it."""

    gate: str | None
    """`None` when NO gate bound — see `DecisionTrace.binding_constraint` step 4."""

    outcome: GateOutcome | None
    normalised_margin: float | None
    would_have_needed: float | None
    explanation: str

    @property
    def explains_the_outcome(self) -> bool:
        """Whether this trace actually accounts for what happened."""
        return self.gate is not None


def _readable(value: Decimal | None) -> str:
    """A threshold a person can read.

    `Decimal` arithmetic over rupee quantities produces things like
    `138893.7766666666666666666666`, and the first live render of this explanation printed exactly
    that. Four significant figures is the resolution any of these decisions turned on.
    """
    if value is None:
        return "none"
    number = float(value)
    # Plain thousands rather than `:g`, which renders a rupee threshold as `1.389e+05`. Money is
    # read in groups of three digits; scientific notation is for physics.
    return f"{number:,.2f}" if abs(number) >= 1 else f"{number:.4g}"


def _require_aware(moment: datetime, label: str) -> None:
    if moment.tzinfo is None:
        raise DecisionTraceError(
            f"{label} carries no timezone; a naive instant is read as UTC and moves every decision "
            f"in this project by five and a half hours"
        )


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    """One decision, recorded at the instant it was taken."""

    decided_at: datetime
    bot_identity: str
    instrument_token: int
    trading_symbol: str
    inputs: tuple[ConsultedInput, ...]
    candidates: tuple[CandidateAction, ...]
    gates: tuple[GateEvaluation, ...]
    chosen_action: str
    confidence: float | None
    mechanism: str
    earliest_knowable: datetime | None = None
    """The session open, when the caller knows it — the FLOOR on how old an input may claim to be.

    `as_of` was only upper-bounded, so `1970-01-01` was accepted: an input claiming to predate the
    session by fifty-six years described a decision nobody made. `None` leaves the floor unchecked
    rather than inventing one, and the check is stated here so its absence is visible.
    """

    kind: DecisionKind = DecisionKind.ENTRY
    """Getting in or getting out. Defaults to `ENTRY`, which is what every stored trace was."""

    decision_ordinal: int = 0
    """Which decision of this kind, for this instrument, at this instant — 0 for the first.

    Not a uniqueness hack. Squaring off a position genuinely takes more than one decision at one
    instant: a round that sends an exit for what has filled so far draws further fills out of the
    venue, and the residual needs its own exit. Those are separate decisions about different
    quantities, not two versions of one — but with the instant alone as the key the second was
    refused as "already recorded" and dropped, so the record understated the exited quantity while
    reporting a duplicate. A genuine re-record of the SAME ordinal still raises, which is the
    protection that matters.
    """

    permitted_actions: frozenset[str] | None = None
    """The action vocabulary this bot may choose from, or `None` to leave it unchecked.

    The candidate set was self-certifying: an adversarial review recorded `teleport` and
    `sell_naked_call` on a cash instrument and neither was refused, so "what did it consider?" could
    be answered with anything. Supplied from the strategy's own action enum rather than a list
    invented here; `None` states that the check is absent rather than implying it passed.
    """

    null_action: str = "abstain"
    """Which candidate means "did nothing" — needed to tell an unexplained abstain from a trade."""

    def __post_init__(self) -> None:
        _require_aware(self.decided_at, "the decision instant")
        if not self.gates:
            raise DecisionTraceError(
                f"{self.trading_symbol} recorded a decision with no gate evaluated. A decision "
                f"reached without evaluating anything is not a decision, and a row like this "
                f"on the panel is unexplainable by construction"
            )
        if not self.inputs:
            raise DecisionTraceError(
                f"{self.trading_symbol} recorded a decision that consulted NOTHING. `gates` and "
                f"`candidates` were both refused when empty and this was not, which made 'what did "
                f"it look at?' the one question a trace could decline to answer"
            )
        if not self.candidates:
            raise DecisionTraceError(
                f"{self.trading_symbol} recorded no candidate actions; a choice with nothing "
                f"to choose between is not a choice"
            )
        if self.chosen_action not in {candidate.action for candidate in self.candidates}:
            raise DecisionTraceError(
                f"{self.trading_symbol} chose {self.chosen_action!r}, which is not among "
                f"the candidates it recorded considering. The record would then describe a "
                f"decision that did not happen — exactly the fiction A.29 forbids"
            )
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise DecisionTraceError(
                f"confidence must be a finite value in [0, 1], got {self.confidence!r}"
            )
        if self.permitted_actions is not None:
            unknown = sorted(
                candidate.action
                for candidate in self.candidates
                if candidate.action not in self.permitted_actions
            )
            if unknown:
                raise DecisionTraceError(
                    f"{self.trading_symbol} considered {unknown}, which is not an action this bot "
                    f"can take. Its vocabulary is {sorted(self.permitted_actions)}"
                )

        for consulted in self.inputs:
            _require_aware(consulted.as_of, f"input {consulted.name!r}")
            if self.earliest_knowable is not None and consulted.as_of < self.earliest_knowable:
                raise DecisionTraceError(
                    f"input {consulted.name!r} claims to be from {consulted.as_of}, before this "
                    f"session could produce it ({self.earliest_knowable}). An input older than the "
                    f"session describes a decision nobody made"
                )
            if consulted.as_of > self.decided_at:
                raise DecisionTraceError(
                    f"input {consulted.name!r} was not knowable until {consulted.as_of}, after the "
                    f"decision at {self.decided_at}. It cannot have been consulted — this is the "
                    f"look-ahead check applied to REASONING rather than to prices"
                )

    @property
    def storage_key_instant(self) -> str:
        """The decision instant in UTC, which is what the store keys on.

        Keying on `decided_at.isoformat()` let the SAME instant spelled in two zones become two
        rows with contradictory content, and `traces_for_session` returned both with no way to tell
        which was the record. Append-only means one instant is one row, whatever offset the caller
        used to say it.
        """
        return self.decided_at.astimezone(UTC).isoformat()

    @property
    def session_date(self) -> date:
        return self.decided_at.date()

    def binding_constraint(self) -> BindingConstraint:
        """Which gate decided this, and what would have changed it.

        Order of resolution, and each step exists because the one before it was not enough:

        1. **A refusing boolean gate binds outright.** Nothing downstream of a closed latch ran, so
           no other gate's margin describes this decision.
        2. **If anything refused**, the binding gate is the refuser with the smallest normalised
           margin — the one that came closest to letting it through.
        3. **If nothing refused and an action was taken**, the binding gate is the passer with the
           smallest normalised margin — the one that came closest to stopping it.
        4. **If nothing refused and NO action was taken, no gate bound**, and this says so.

        Step 4 is the important one and it was missing. Without it the passers were ranked anyway
        and the winner asserted as *the* cause — so an abstain caused by something outside the
        recorded gates was blamed on a gate that had passed. An adversarial review measured the
        consequence on real data: **380 traces from the 2026-08-17 session named `deviation_band`,
        which passed, as the reason for an abstain the order-rate limiter had actually caused.**
        That is an affirmative false statement about a decision, which is worse than no panel at
        all and is exactly what `A.29` forbids.

        Boolean gates are excluded from every margin comparison, since they have no margin and a
        fake zero would win each one.
        """
        refusing_boolean = next(
            (gate for gate in self.gates if gate.is_boolean and gate.refused), None
        )
        if refusing_boolean is not None:
            return BindingConstraint(
                gate=refusing_boolean.gate,
                outcome=refusing_boolean.outcome,
                normalised_margin=None,
                would_have_needed=None,
                explanation=(
                    f"{refusing_boolean.gate} refused and is a boolean gate with no margin: it is "
                    f"open or closed, and nothing downstream of it ran. {refusing_boolean.detail}"
                ),
            )

        refusers = [gate for gate in self.gates if gate.refused and not gate.is_boolean]
        if not refusers and self.chosen_action == self.null_action:
            return BindingConstraint(
                gate=None,
                outcome=None,
                normalised_margin=None,
                would_have_needed=None,
                explanation=(
                    f"no recorded gate refused, yet the decision was {self.chosen_action!r}. This "
                    f"trace does NOT explain the outcome: something outside the gates it recorded "
                    f"caused it. Naming the tightest passing gate here would be an affirmative "
                    f"false statement about why nothing happened"
                ),
            )

        considered = refusers or [gate for gate in self.gates if not gate.is_boolean]
        if not considered:
            only = self.gates[0]
            return BindingConstraint(
                gate=only.gate,
                outcome=only.outcome,
                normalised_margin=None,
                would_have_needed=None,
                explanation=(
                    f"every gate evaluated was boolean, so there is no margin to rank. "
                    f"{only.detail}"
                ),
            )

        ranked = sorted(considered, key=lambda gate: gate.normalised_margin() or 0.0)
        binding = ranked[0]
        tied = [
            gate for gate in ranked[1:] if gate.normalised_margin() == binding.normalised_margin()
        ]
        needed = abs(float(binding.margin)) if binding.margin is not None else None
        direction = "more and" if binding.refused else "of headroom before"
        tie_note = (
            f" TIED with {', '.join(gate.gate for gate in tied)} at the same normalised margin, so "
            f"which one is named is arbitrary."
            if tied
            else ""
        )
        return BindingConstraint(
            gate=binding.gate,
            outcome=binding.outcome,
            normalised_margin=binding.normalised_margin(),
            would_have_needed=needed,
            explanation=(
                f"{binding.gate} was the binding constraint: "
                + (
                    # A gate that refuses AT its threshold has a margin of exactly zero, and
                    # "0 more and the outcome would have changed" is arithmetically false — the
                    # test is `>=`, so zero more changes nothing. This is the MODAL case for a
                    # horizon that expires on a decision-step boundary, which is how horizons are
                    # constructed, so the false phrasing was the common one rather than an edge.
                    "it refused at exactly its threshold, so any margin at all would have changed "
                    "the outcome, against a threshold of "
                    if needed == 0
                    else f"{needed:.4g} {direction} the outcome would have changed, against a "
                    f"threshold of "
                )
                + f"{_readable(binding.threshold)}. "
                + f"{binding.detail}{tie_note}"
            ),
        )


def _dump(trace: DecisionTrace) -> tuple[str, str, str]:
    inputs = json.dumps(
        [
            {
                "name": item.name,
                "value": item.value,
                "source": item.source,
                "as_of": item.as_of.isoformat(),
            }
            for item in trace.inputs
        ]
    )
    candidates = json.dumps(
        [
            {"action": item.action, "why_considered": item.why_considered}
            for item in trace.candidates
        ]
    )
    gates = json.dumps(
        [
            {
                "gate": item.gate,
                "outcome": str(item.outcome),
                "margin": None if item.margin is None else str(item.margin),
                "threshold": None if item.threshold is None else str(item.threshold),
                "detail": item.detail,
            }
            for item in trace.gates
        ]
    )
    return inputs, candidates, gates


@dataclass(frozen=True, slots=True)
class DecisionTraceSummary:
    """One session's reasoning, counted — what the `R.08` surface renders."""

    session_date: date
    total: int
    by_action: dict[str, int]
    by_binding_gate: dict[str, int]
    unexplained: int
    sample_acted: tuple[DecisionTrace, ...]
    by_kind: dict[str, int] = field(default_factory=dict)
    """Entries versus exits.

    On the surface because their absence was invisible: the first 21,270 traces were entries and
    abstentions ONLY, and the panel said nothing about that, so a reader had no way to notice that
    every exit the system had ever taken was unexplained by its own record (`M25`).
    """

    sample_exited: tuple[DecisionTrace, ...] = ()

    @property
    def explained_fraction(self) -> float:
        """How much of the session its own traces account for. Below 1.0 is the honest number."""
        return 1.0 if not self.total else (self.total - self.unexplained) / self.total


class DecisionTraceStore:
    """Append-only decision traces. There is deliberately no update path.

    A trace that could be rewritten is a reconstructed one, and reconstruction is the single thing
    `A.29` forbids. Re-recording the same `(bot, instant, instrument)` raises rather than replacing,
    so a second version of a decision announces itself instead of quietly becoming the record.
    """

    def __init__(self, database_path: Path = DEFAULT_DECISION_TRACE_PATH) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._bring_an_older_store_up_to_the_current_schema()
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DecisionTraceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _bring_an_older_store_up_to_the_current_schema(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` is a no-op on a store that already exists.

        The live store held 21,270 rows written before the kind, the action vocabulary and the
        decision ordinal existed, and those rows are append-only BY TRIGGER — so the table is
        rebuilt under dropped triggers rather than mutated in place, and the triggers go back on
        afterwards. Rebuilding rather than `ALTER`-ing is deliberate: the primary key itself
        changed, and an `ALTER` cannot change a key.

        Existing rows are stamped `kind='entry'` and `decision_ordinal=0`, which is TRUE of them
        rather than a placeholder — the emitter only ever ran on the entry path, so the exits of
        the first 21,270 traced decisions are absent, not mislabelled (`M25`).
        """
        columns = {
            str(column[1]): int(column[5])
            for column in self._connection.execute("PRAGMA table_info(decision_trace)")
        }
        if not columns:
            return
        key_is_current = columns.get("kind", 0) > 0 and columns.get("decision_ordinal", 0) > 0
        if key_is_current:
            return
        carried = [
            "bot_identity",
            "decided_at",
            "instrument_token",
            "session_date",
            "trading_symbol",
            "chosen_action",
            "confidence",
            "mechanism",
            "inputs_json",
            "candidates_json",
            "gates_json",
        ]
        optional = ("permitted_actions_json", "null_action", "earliest_knowable", "kind")
        carried += [name for name in optional if name in columns]
        column_list = ", ".join(carried)
        with self._connection:
            self._connection.execute("DROP TRIGGER IF EXISTS decision_trace_is_append_only_update")
            self._connection.execute("DROP TRIGGER IF EXISTS decision_trace_is_append_only_delete")
            self._connection.execute("ALTER TABLE decision_trace RENAME TO decision_trace_previous")
        self._connection.executescript(_SCHEMA)
        with self._connection:
            # The column names come from a fixed allow-list intersected with the store's own
            # `PRAGMA table_info`, never from a caller, so there is no input to inject.
            self._connection.execute(
                f"INSERT INTO decision_trace ({column_list}) "  # noqa: S608
                f"SELECT {column_list} FROM decision_trace_previous"
            )
            self._connection.execute("DROP TABLE decision_trace_previous")

    def record(self, trace: DecisionTrace) -> None:
        inputs, candidates, gates = _dump(trace)
        try:
            with self._connection:
                self._connection.execute(
                    # NAMED, never positional. A positional INSERT silently depends on the
                    # physical column order agreeing with `_SCHEMA`, and on a store migrated by
                    # `ALTER TABLE` it does NOT: the live store grew its columns in the order they
                    # were added, so `null_action` landed where `earliest_knowable` was expected.
                    # Every write then failed its NOT NULL constraint and `record` reported it as
                    # "already recorded" — the whole feature off, under a diagnostic that blamed
                    # duplicate instants. Naming the columns makes the bug unexpressible.
                    "INSERT INTO decision_trace ("
                    " bot_identity, decided_at, instrument_token, session_date, trading_symbol,"
                    " chosen_action, confidence, mechanism, inputs_json, candidates_json,"
                    " gates_json, permitted_actions_json, null_action, earliest_knowable, kind,"
                    " decision_ordinal"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trace.bot_identity,
                        trace.storage_key_instant,
                        trace.instrument_token,
                        trace.session_date.isoformat(),
                        trace.trading_symbol,
                        trace.chosen_action,
                        trace.confidence,
                        trace.mechanism,
                        inputs,
                        candidates,
                        gates,
                        (
                            None
                            if trace.permitted_actions is None
                            else json.dumps(sorted(trace.permitted_actions))
                        ),
                        trace.null_action,
                        (
                            None
                            if trace.earliest_knowable is None
                            else trace.earliest_knowable.isoformat()
                        ),
                        str(trace.kind),
                        trace.decision_ordinal,
                    ),
                )
        except sqlite3.IntegrityError as clash:
            raise DecisionTraceError(
                f"a decision for {trace.trading_symbol} by {trace.bot_identity} at "
                f"{trace.decided_at} is already recorded. Traces are append-only: a second version "
                f"of a decision is a rewritten history, and reasoning that can be rewritten is "
                f"reasoning that was reconstructed"
            ) from clash

    def sessions_recorded(self) -> tuple[date, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT session_date FROM decision_trace ORDER BY session_date DESC"
        ).fetchall()
        return tuple(date.fromisoformat(str(row[0])) for row in rows)

    def session_summary(self, session_date: date) -> DecisionTraceSummary:
        """Counts for one session, aggregated in SQL rather than by loading every trace.

        The 2026-08-17 session recorded **21,270** traces. A page that materialised all of them to
        count five things would be slow for no reason, and slow surfaces stop being looked at —
        which for an `R.08` panel is the same as not existing.

        The binding-gate tally still needs the inference, so it runs over the traces; everything
        countable in SQL is counted in SQL.
        """
        counts = dict(
            self._connection.execute(
                "SELECT chosen_action, COUNT(*) FROM decision_trace WHERE session_date = ?"
                " GROUP BY chosen_action",
                (session_date.isoformat(),),
            ).fetchall()
        )
        traces = self.traces_for_session(session_date)
        binding: dict[str, int] = {}
        unexplained = 0
        for trace in traces:
            constraint = trace.binding_constraint()
            if constraint.gate is None:
                unexplained += 1
                continue
            binding[constraint.gate] = binding.get(constraint.gate, 0) + 1
        kinds = dict(
            self._connection.execute(
                "SELECT kind, COUNT(*) FROM decision_trace WHERE session_date = ? GROUP BY kind",
                (session_date.isoformat(),),
            ).fetchall()
        )
        return DecisionTraceSummary(
            session_date=session_date,
            by_kind={str(key): int(value) for key, value in kinds.items()},
            sample_exited=tuple(
                trace
                for trace in traces
                if trace.kind is DecisionKind.EXIT and trace.chosen_action != trace.null_action
            )[:8],
            total=sum(int(value) for value in counts.values()),
            by_action={str(key): int(value) for key, value in counts.items()},
            by_binding_gate=binding,
            unexplained=unexplained,
            sample_acted=tuple(
                trace for trace in traces if trace.chosen_action != trace.null_action
            )[:8],
        )

    def traces_for_session(self, session_date: date) -> tuple[DecisionTrace, ...]:
        rows = self._connection.execute(
            "SELECT bot_identity, decided_at, instrument_token, trading_symbol, chosen_action,"
            " confidence, mechanism, inputs_json, candidates_json, gates_json,"
            " permitted_actions_json, null_action, earliest_knowable, kind,"
            " decision_ordinal FROM decision_trace"
            " WHERE session_date = ? ORDER BY decided_at, instrument_token",
            (session_date.isoformat(),),
        ).fetchall()
        return tuple(_load(row) for row in rows)


def _load(row: tuple[object, ...]) -> DecisionTrace:
    """Rebuild a stored trace WITHOUT re-running the write-time refusals.

    `__post_init__` is the contract for producing a trace. Running it again on the way OUT of the
    store made every historical row hostage to today's rules: a review demonstrated that a single
    row failing a newly added check raised from `traces_for_session`, which takes down the whole
    session's panel rather than one row — and the append-only triggers then make that row
    undeletable, so the surface is dead short of dropping the table.

    A row in this store was validated when it was written. Re-deciding that on read is not a second
    opinion, it is a way to lose history to a rule that did not exist when the history was made.
    Validation belongs on the way in; reading is reconstruction.
    """
    return _reconstruct_stored_trace(
        bot_identity=str(row[0]),
        decided_at=datetime.fromisoformat(str(row[1])),
        instrument_token=int(str(row[2])),
        trading_symbol=str(row[3]),
        chosen_action=str(row[4]),
        confidence=None if row[5] is None else float(str(row[5])),
        mechanism=str(row[6]),
        inputs=tuple(
            ConsultedInput(
                name=item["name"],
                value=item["value"],
                source=item["source"],
                as_of=datetime.fromisoformat(item["as_of"]),
            )
            for item in json.loads(str(row[7]))
        ),
        candidates=tuple(
            CandidateAction(action=item["action"], why_considered=item["why_considered"])
            for item in json.loads(str(row[8]))
        ),
        gates=tuple(
            GateEvaluation(
                gate=item["gate"],
                outcome=GateOutcome(item["outcome"]),
                margin=None if item["margin"] is None else Decimal(item["margin"]),
                threshold=None if item["threshold"] is None else Decimal(item["threshold"]),
                detail=item["detail"],
            )
            for item in json.loads(str(row[9]))
        ),
        permitted_actions=(
            None if row[10] is None else frozenset(json.loads(str(row[10])))
        ),
        null_action=str(row[11]),
        earliest_knowable=(
            None if row[12] is None else datetime.fromisoformat(str(row[12]))
        ),
        kind=DecisionKind(str(row[13])),
        decision_ordinal=int(str(row[14])),
    )


def _reconstruct_stored_trace(**fields: object) -> DecisionTrace:
    """Assemble a `DecisionTrace` from an already-validated row, bypassing `__post_init__`.

    Bypassing `__init__` also bypasses the DEFAULTS, so every field the caller does not supply is
    filled from the dataclass itself. Leaving one unset produces an object that raises
    `AttributeError` on attribute access instead of failing at construction — a worse failure than
    the one this function exists to avoid.
    """
    trace = object.__new__(DecisionTrace)
    supplied = dict(fields)
    for declared in dataclasses.fields(DecisionTrace):
        if declared.name in supplied:
            object.__setattr__(trace, declared.name, supplied.pop(declared.name))
        elif declared.default is not dataclasses.MISSING:
            object.__setattr__(trace, declared.name, declared.default)
        else:
            raise DecisionTraceError(
                f"stored trace is missing {declared.name!r}, no default to fall back on"
            )
    if supplied:
        raise DecisionTraceError(f"stored trace carries unknown fields {sorted(supplied)}")
    return trace
