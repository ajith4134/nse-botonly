"""`L2.01`/`L2.14` — how many hypotheses were actually tested, recorded so it cannot be flattered.

Spec: `docs/research/245`. First engine of `F06`, the gatekeeper.

**Why the count is the load-bearing number.** Every gate in `F06` is a function of the number of
trials: Deflated Sharpe (`L2.03`) deflates by it, PBO/CSCV (`L2.06`) resamples over it,
Benjamini-Yekutieli (`L2.07`) corrects for it, and the effective-trials estimator (`L2.08`) is
literally `N-hat = rho-hat + (1 - rho-hat) x M` in it. **An undercounted `M` flatters every one
of them, and always in the direction that lets a bad strategy through** — a Sharpe that survives
deflation it should not have, an FDR threshold that under-corrects. So the registry is not
bookkeeping under the
gatekeeper; it is the input that decides whether the gatekeeper means anything.

**Why a hash chain plus a witness file, and WHAT THAT ACTUALLY BUYS.** Append-only as an API is a
promise by the author to their future self, and the future self is precisely the party with a
motive to drop the trial that spoiled a result. Each record carries the digest of the one before
it, so an interior deletion or an edit breaks every digest after it and `verify_chain` returns
WHERE.

**The first version of this docstring claimed the chain made the record "checkable by a reader who
does not trust the author". That was an overclaim and the `A.128` review broke it in twelve lines**
(`docs/research/246` §2): `_digest_for` is a pure function of public inputs, so anyone with the
sqlite file AND this module can delete a record and recompute every downstream digest, or splice a
fabricated trial in, and `verify_chain` returns `None`. Worse, a chain that only walks forward
cannot see a TRUNCATED TAIL at all — and `DELETE FROM strategy_trial WHERE sequence >= k` is
exactly the edit an under-reporting author wants, since the count only ever needs to go down.
Measured: 10 trials, drop the last 3, `count = 7`, `verify_chain() = None`.

So the registry keeps an append-only **witness** beside the store — one `sequence\tdigest` line per
record, written after the commit. `verify_chain` reports the first sequence the witness lists and
the table lacks, which is what makes truncation visible at all.

**The honest threat model, stated so a downstream gate does not over-trust it:**

- **caught** — an interior delete, an edit to any covered field, a reorder, a renumber, and a
  truncated tail, all as made with a sqlite shell by someone not trying to cover their tracks;
- **NOT caught** — a determined author who runs this module to recompute the chain AND rewrites
  the witness. Defeating that needs a key or an anchor outside the machine, which this does not
  have. `verify_chain() is None` therefore means "no casual tampering", never "provably complete".

SOTA analog (`R.23(a)`): git's commit graph and Certificate Transparency, and the witness is the
same idea as CT's signed tree head — with the honest difference that CT's is signed by a party the
author cannot impersonate, and this one is not.

**Why nothing on PyPI does this**, measured in `docs/research/245` §3 rather than assumed:
`mlflow` 3.15.1's `search_runs` returned **4 of 5** logged runs after one `delete_run` — the
flattered count is its default — and `mlflow gc` makes it permanent; `aim` exposes
`Repo.delete_run`; `sacred` does not own its own storage. `optuna` 4.9.0 alone has the right
accounting (a 40-trial study reports `COMPLETE: 15, FAIL: 13, PRUNED: 12`, and there is no public
delete-a-trial API) — **its state model is adopted here**, but it is a sampler-coupled optimizer and
this registry must accept trials from replays, champion-challenger runs and manual investigations
that are not an optuna study. None of them is tamper-evident, because none of them is trying to
stop an author from under-reporting their own search. Those are opposite incentives.

**What this deliberately does not do.** It does not decide anything — a registry that also judged
would make the count a function of the verdict. It does not deduplicate: two identical
parameterisations tried twice ARE two tests of one hypothesis, which is exactly what a
multiple-testing correction exists to correct for. And it does not backfill trials from before it
existed; those are unrecorded and unrecoverable, precisely as `L0.37`'s `NULL` price basis is, and
the honest count starts here and says so.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

DEFAULT_TRIAL_REGISTRY = Path("~/.nse_algo_trader/trial_registry.sqlite3").expanduser()

APPEND_LOCK_TIMEOUT_SECONDS = 60.0
"""How long an append waits for a writer ahead of it before giving up LOUDLY.

Not a tuned number: an append is a single small INSERT inside one `BEGIN IMMEDIATE`, so a wait of
this length means a writer has stalled rather than that the registry is busy. The default of 5s was
short enough that a slow writer turned into a LOST TRIAL (`M6`), which is precisely the outcome
this engine exists to prevent."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_trial (
    sequence        INTEGER PRIMARY KEY,
    search          TEXT NOT NULL,
    parameters      TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    fitness         REAL,
    ran_at          TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    digest          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_trial_by_search ON strategy_trial (search);
"""

GENESIS_DIGEST = "0" * 64
"""What the first record's `previous_digest` points at. A fixed, recognisable value rather than an
empty string, so a chain that begins mid-history is distinguishable from one that begins at all."""


class TrialRegistryError(Exception):
    """A write that would make the record dishonest, or a chain that does not verify."""


class TrialOutcome(Enum):
    """How a trial ended — four terminal states of ONE population, not results plus noise.

    Adopted from `optuna`'s state model (`COMPLETE`/`PRUNED`/`FAIL` all counted in
    `study.trials`), which is the one prior art that counts what it abandoned.
    """

    COMPLETED = "completed"
    """Ran to the end and produced a fitness."""

    ABANDONED = "abandoned"
    """Stopped before it finished — a killed sweep, a timeout, an operator interrupt. It still
    tested a hypothesis, and it still counts against the search size."""

    ERRORED = "errored"
    """Raised. The most commonly uncounted trial, because a traceback does not feel like a result;
    it consumed a hypothesis exactly as a completed run did."""

    DISCARDED = "discarded"
    """Ran, produced a number, and the number was thrown away — "that one looked wrong", "the data
    was bad that week", "I had not fixed the bug yet".

    **Named explicitly because it is the trial most likely to go unrecorded**, and it is the one
    that most flatters the search size when it does. A state that has no name cannot be recorded."""

    @property
    def produces_a_fitness(self) -> bool:
        return self is TrialOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """One hypothesis, tested — and the link that proves it was not removed afterwards."""

    sequence: int
    search: str
    parameters: str
    outcome: TrialOutcome
    fitness: float | None
    ran_at: datetime
    previous_digest: str
    digest: str


def _digest_for(
    *,
    sequence: int,
    search: str,
    parameters: str,
    outcome: TrialOutcome,
    fitness: float | None,
    ran_at: datetime,
    previous_digest: str,
) -> str:
    """SHA-256 over every field AND the previous digest.

    `hashlib` rather than `hash()`: the built-in is salted per process, so a chain built on it
    would verify in the process that wrote it and fail in the next one — making tamper evidence
    indistinguishable from a restart.

    Every field is covered, not only the identity, so an edited FITNESS is caught as surely as a
    removed record. `repr` on the float keeps the round-trip exact.

    **Fields are LENGTH-PREFIXED, not separator-joined** (`M2` of the `A.128` review). A `\x1f`
    join has no field boundary, so `search="sweep-a\x1f", parameters="horizon=5"` and
    `search="sweep-a", parameters="\x1fhorizon=5"` produced the SAME digest — which let a trial be
    moved out of a per-search count with the chain still verifying. `search` and `parameters` are
    free text, so the ambiguity was reachable.
    """
    fields = (
        str(sequence),
        search,
        parameters,
        outcome.value,
        "none" if fitness is None else repr(float(fitness) + 0.0),
        ran_at.isoformat(),
        previous_digest,
    )
    digest = hashlib.sha256()
    for field in fields:
        encoded = field.encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
    return digest.hexdigest()


class HonestTrialRegistry:
    """Append-only, tamper-evident record of every trial this project has run.

    The public surface is deliberately three verbs — record, count, verify. There is no update and
    no delete, and `test_the_registry_offers_no_way_to_remove_or_edit_a_trial` asserts that against
    the SOURCE as well as the API, because the guarantee is structural rather than behavioural.
    """

    def __init__(self, store: Path = DEFAULT_TRIAL_REGISTRY) -> None:
        self.store = store
        self.witness = store.with_suffix(".witness")
        """Append-only sidecar, one `sequence\tdigest` line per record.

        The chain alone cannot see a truncated tail — it walks forward and stops when rows run out,
        so a shorter chain is a valid chain. The witness is the only thing that knows how long the
        history is meant to be, and truncation now means editing two files instead of one."""

    # -- writing ---------------------------------------------------------------

    def record(
        self,
        *,
        search: str,
        parameters: str,
        outcome: TrialOutcome,
        fitness: float | None,
        ran_at: datetime,
    ) -> TrialRecord:
        """Append one trial to the chain and return what was written.

        Raises:
            TrialRegistryError: a fitness was supplied for an outcome that produces none. An
                abandoned or errored run has no score, and allowing one would let a discarded
                trial carry a flattering number.
        """
        return self.append_recorded(
            search=search,
            parameters=parameters,
            outcome=outcome,
            fitness=fitness,
            ran_at=ran_at,
            previous_digest=None,
        )

    def append_recorded(
        self,
        *,
        search: str,
        parameters: str,
        outcome: TrialOutcome,
        fitness: float | None,
        ran_at: datetime,
        previous_digest: str | None,
    ) -> TrialRecord:
        """`record` with an explicit expected tail — the concurrency-safe form.

        `previous_digest=None` means "whatever the tail is now". Supplying it makes the write
        conditional, so two writers cannot silently interleave into a chain that no longer
        verifies: the loser is refused rather than appended out of order.
        """
        if fitness is not None and not outcome.produces_a_fitness:
            raise TrialRegistryError(
                f"a {outcome.value} trial has no fitness; got {fitness!r}. Recording one would "
                f"let a discarded trial carry a number it never earned"
            )
        if outcome.produces_a_fitness and fitness is None:
            raise TrialRegistryError(
                "a completed trial must carry the fitness it produced, or the search size counts "
                "a hypothesis whose answer nobody kept"
            )
        if fitness is not None and not math.isfinite(fitness):
            # `M1`. A NaN or infinite fitness digests one way and comes back from SQLite another
            # (NaN stores as NULL), so the record could never verify again — and because the class
            # is append-only the false break would be PERMANENT, training a reader to ignore the
            # one signal this engine exists to raise. A degenerate score is a DISCARDED trial with
            # no fitness, not a completed one.
            raise TrialRegistryError(
                f"a fitness must be finite; got {fitness!r}. A trial that scored this did not "
                f"complete — record it as DISCARDED with no fitness"
            )
        if ran_at.tzinfo is None or ran_at.utcoffset() is None:
            raise TrialRegistryError(
                "a trial's timestamp must carry a timezone; a naive one is a different instant on "
                "a different host and the digest would not survive the move"
            )

        self.store.parent.mkdir(parents=True, exist_ok=True)
        # `M6`: a lock timeout used to surface as a bare `OperationalError`, so a caller catching
        # the documented `TrialRegistryError` lost the trial silently — the engine's own defining
        # failure. WAL plus a long busy timeout makes contention rare; the wrapper makes the
        # remainder loud.
        connection = sqlite3.connect(self.store, timeout=APPEND_LOCK_TIMEOUT_SECONDS)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA busy_timeout={int(APPEND_LOCK_TIMEOUT_SECONDS * 1000)}")
            connection.executescript(_SCHEMA)
            # IMMEDIATE so the read of the tail and the append are one transaction: two writers
            # cannot both see the same tail and both append against it.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT sequence, digest FROM strategy_trial ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            tail_sequence, tail_digest = (0, GENESIS_DIGEST) if row is None else row
            if previous_digest is not None and previous_digest != tail_digest:
                connection.rollback()
                raise TrialRegistryError(
                    f"the registry has moved on: expected the chain to end at {previous_digest}, "
                    f"but it ends at {tail_digest}. Re-read the tail and append again"
                )
            sequence = int(tail_sequence) + 1
            digest = _digest_for(
                sequence=sequence,
                search=search,
                parameters=parameters,
                outcome=outcome,
                fitness=fitness,
                ran_at=ran_at,
                previous_digest=str(tail_digest),
            )
            connection.execute(
                "INSERT INTO strategy_trial (sequence, search, parameters, outcome, fitness, "
                "ran_at, previous_digest, digest) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    search,
                    parameters,
                    outcome.value,
                    fitness,
                    ran_at.isoformat(),
                    str(tail_digest),
                    digest,
                ),
            )
            connection.commit()
        except sqlite3.OperationalError as failure:
            raise TrialRegistryError(
                f"the registry could not be written ({failure}); the trial is NOT recorded and the "
                f"caller must not treat it as counted"
            ) from failure
        finally:
            connection.close()
        # After the commit, never before: a witness line for a trial that failed to commit would
        # itself be a false break.
        with self.witness.open("a", encoding="utf-8") as witness:
            witness.write(f"{sequence}\t{digest}\n")
        return TrialRecord(
            sequence=sequence,
            search=search,
            parameters=parameters,
            outcome=outcome,
            fitness=fitness,
            ran_at=ran_at,
            previous_digest=str(tail_digest),
            digest=digest,
        )

    # -- reading ---------------------------------------------------------------

    def exists(self) -> bool:
        """Whether a registry has ever been written — the TABLE, not merely the file.

        `A.41`: "no trials recorded" and "no registry" are different facts, and a gate that could
        not tell them apart would read an unbuilt registry as a search of size zero, which is the
        most flattering count of all.

        Checks for the table because file existence was drawing the distinction one level too
        shallow (`H2` of the `A.128` review): an empty file, a different database, or any non-db
        file made every read method raise `OperationalError` instead of answering.
        """
        if not self.store.exists():
            return False
        try:
            with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_trial'"
                ).fetchone()
        except sqlite3.DatabaseError:
            return False
        return row is not None

    def cumulative_trials(self, *, search: str | None = None) -> int:
        """How many hypotheses were tested — ALL of them, whatever the outcome.

        The multiple-testing burden is the whole population, so `search=None` is the number the
        gates want; the per-search count is for reporting.
        """
        if not self.exists():
            return 0
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
            if search is None:
                row = connection.execute("SELECT COUNT(*) FROM strategy_trial").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM strategy_trial WHERE search = ?", (search,)
                ).fetchone()
        return int(row[0])

    def trials_by_outcome(self, *, search: str | None = None) -> dict[TrialOutcome, int]:
        """The population split four ways, so a reader can see what was abandoned and discarded
        rather than only what completed."""
        counts = dict.fromkeys(TrialOutcome, 0)
        if not self.exists():
            return counts
        # Two CONSTANT statements rather than one built by concatenation, so
        # `test_no_sql_this_module_executes_can_remove_or_edit_a_trial` can parse every statement
        # this module runs. SQL assembled from a variable is unauditable, and the append-only
        # guarantee is only worth what an auditor can check.
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
            rows = (
                connection.execute("SELECT outcome, COUNT(*) FROM strategy_trial GROUP BY outcome")
                if search is None
                else connection.execute(
                    "SELECT outcome, COUNT(*) FROM strategy_trial WHERE search = ? "
                    "GROUP BY outcome",
                    (search,),
                )
            )
            for outcome, count in rows:
                counts[TrialOutcome(str(outcome))] = int(count)
        return counts

    def recorded_parameters(self, *, search: str) -> frozenset[str]:
        """Every `parameters` string already recorded under one search.

        The read a resumable writer needs: it lets a reconstruction record only what is missing,
        so a run that died half-way can be finished without either duplicating trials or leaving
        the count permanently short (`H4` of the `A.128` review). Reading to avoid a duplicate is
        not the same as editing — nothing here removes or changes a record.
        """
        if not self.exists():
            return frozenset()
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT parameters FROM strategy_trial WHERE search = ?", (search,)
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def tail_digest(self) -> str:
        """The digest the next record must chain from."""
        if not self.exists():
            return GENESIS_DIGEST
        with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT digest FROM strategy_trial ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return GENESIS_DIGEST if row is None else str(row[0])

    def witnessed_sequences(self) -> list[tuple[int, str]]:
        """`(sequence, digest)` as the witness recorded them, in file order.

        Unparseable lines are skipped rather than raised on: a corrupt witness is itself a finding
        for `verify_chain` to report, not a crash of the reader.
        """
        if not self.witness.exists():
            return []
        witnessed: list[tuple[int, str]] = []
        for line in self.witness.read_text(encoding="utf-8").splitlines():
            sequence, separator, digest = line.partition("\t")
            if not separator:
                continue
            try:
                witnessed.append((int(sequence), digest))
            except ValueError:
                continue
        return witnessed

    def verify_chain(self) -> int | None:
        """`None` if every link holds AND nothing the witness saw is missing; else where it breaks.

        **Returns a location rather than a boolean, and does not raise**, because a broken chain is
        a FINDING about the record — the thing this class exists to surface — and an exception
        would make it look like an outage. The first version DID raise on exactly the tamper it
        exists to locate: an edit putting an unparseable value into a covered field reached
        `datetime.fromisoformat` or `TrialOutcome(...)` before the digest comparison and became a
        stack trace (`H2`). Any field this reader cannot parse IS a break, at that sequence.

        Three checks, and the third is the one a self-contained chain cannot do:

        1. each record's `previous_digest` is its predecessor's `digest`, and sequences are gapless
           — catches an interior delete, a reorder and a renumber;
        2. each record's digest recomputes from its own fields — catches an edit to any of them;
        3. **every sequence the witness recorded is still present** — catches a TRUNCATED TAIL,
           which checks 1 and 2 cannot see at all, because a shorter chain is a valid chain and
           truncation is the cheapest way to make the count go down.
        """
        if not self.exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.store}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    "SELECT sequence, search, parameters, outcome, fitness, ran_at, "
                    "previous_digest, digest FROM strategy_trial ORDER BY sequence"
                ).fetchall()
        except sqlite3.DatabaseError:
            return 1

        expected_previous = GENESIS_DIGEST
        expected_sequence = 1
        present: set[int] = set()
        for row in rows:
            (
                sequence_value,
                search,
                parameters,
                outcome,
                fitness,
                ran_at,
                previous_digest,
                digest,
            ) = row
            try:
                sequence = int(sequence_value)
            except (TypeError, ValueError):
                return expected_sequence
            if sequence != expected_sequence or str(previous_digest) != expected_previous:
                return sequence
            try:
                recomputed = _digest_for(
                    sequence=sequence,
                    search=str(search),
                    parameters=str(parameters),
                    outcome=TrialOutcome(str(outcome)),
                    fitness=None if fitness is None else float(fitness),
                    ran_at=datetime.fromisoformat(str(ran_at)),
                    previous_digest=str(previous_digest),
                )
            except (TypeError, ValueError):
                # An outcome that is not one of ours, a timestamp that is not a timestamp, a
                # fitness that is not a number — every one of them is an edit to a covered field.
                return sequence
            if recomputed != str(digest):
                return sequence
            present.add(sequence)
            expected_previous = str(digest)
            expected_sequence = sequence + 1

        for sequence, _digest in self.witnessed_sequences():
            if sequence not in present:
                return sequence
        return None
