"""`L2.01`/`L2.14` — the count every `F06` gate is computed over, and it must not be flattered.

Spec: `docs/research/245`.

The test that carries the design is `test_a_removed_trial_is_located_not_merely_suspected`. Every
other test here is accounting; that one is the reason this is a hash chain rather than a table.
`mlflow` was rejected because `search_runs` returned 4 of 5 logged runs after one `delete_run` —
the flattered count is its DEFAULT — and no tracker on PyPI is tamper-evident, because none of them
is trying to stop an author from under-reporting their own search.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as strategy

from nse_algo_trader.validation.honest_trial_registry import (
    HonestTrialRegistry,
    TrialOutcome,
    TrialRecord,
    TrialRegistryError,
)

SEARCH = "mean-reversion-horizon-sweep"


def _registry(tmp_path: Path) -> HonestTrialRegistry:
    return HonestTrialRegistry(tmp_path / "trial_registry.sqlite3")


def _record(
    registry: HonestTrialRegistry,
    *,
    outcome: TrialOutcome = TrialOutcome.COMPLETED,
    fitness: float | None = 0.5,
    parameters: str = "horizon=5",
    search: str = SEARCH,
) -> TrialRecord:
    return registry.record(
        search=search,
        parameters=parameters,
        outcome=outcome,
        fitness=fitness if outcome is TrialOutcome.COMPLETED else None,
        ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------- the count itself


def test_every_terminal_state_counts_toward_the_search_size(tmp_path: Path) -> None:
    """**The whole point.** A run that was abandoned, that raised, or whose result was thrown away
    is still a hypothesis that was tested. Counting only the completed ones is how a search of
    forty becomes a search of twelve, and how Deflated Sharpe then calls noise significant.

    `DISCARDED` is named explicitly because "I did not like this result" is the trial most likely
    to go unrecorded, and naming a thing is what makes it recordable.
    """
    registry = _registry(tmp_path)
    for outcome in TrialOutcome:
        _record(registry, outcome=outcome)
    assert registry.cumulative_trials() == len(TrialOutcome) == 4
    assert registry.cumulative_trials(search=SEARCH) == 4
    counts = registry.trials_by_outcome()
    assert set(counts) == set(TrialOutcome)
    assert all(count == 1 for count in counts.values())


def test_two_identical_parameterisations_are_two_trials(tmp_path: Path) -> None:
    """Deduplication would flatter `N` while looking like tidiness. Trying the same thing twice IS
    two tests of the same hypothesis, and that is exactly what a multiple-testing correction is
    correcting for."""
    registry = _registry(tmp_path)
    _record(registry, parameters="horizon=5")
    _record(registry, parameters="horizon=5")
    assert registry.cumulative_trials() == 2


def test_searches_are_counted_apart_and_together(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _record(registry, search="sweep-a")
    _record(registry, search="sweep-a")
    _record(registry, search="sweep-b")
    assert registry.cumulative_trials(search="sweep-a") == 2
    assert registry.cumulative_trials(search="sweep-b") == 1
    assert registry.cumulative_trials() == 3, (
        "the multiple-testing burden is the whole population, not one sweep"
    )


def test_an_absent_registry_has_no_trials_and_says_which(tmp_path: Path) -> None:
    """`A.41`. "No trials recorded" and "no registry" are different facts, and a gate that cannot
    tell them apart would treat an unbuilt registry as a clean search."""
    registry = _registry(tmp_path)
    assert registry.cumulative_trials() == 0
    assert not registry.exists()
    _record(registry)
    assert registry.exists()


# --------------------------------------------------------------- append-only


def test_no_sql_this_module_executes_can_remove_or_edit_a_trial() -> None:
    """Structural, and asserted by PARSING the module rather than grepping it.

    The first version of this test read:

        assert "UPDATE " not in statements or "UPDATE TRIAL" not in statements

    The table is `strategy_trial`, so a real statement uppercases to `UPDATE STRATEGY_TRIAL`, which
    never contains `UPDATE TRIAL` — the right disjunct was **unconditionally true and so was the
    whole assertion**. Proven by the `A.128` review: a live `revise()` doing
    `UPDATE strategy_trial SET fitness = ?` was added to the module and the suite stayed green
    15/15. The `DELETE FROM` half fell to `"DELE" "TE FROM ..."` string concatenation.

    Parsing cannot be defeated by spelling.
    """
    import ast

    import nse_algo_trader.validation.honest_trial_registry as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Module-level string constants ARE auditable — resolve them, so `executescript(_SCHEMA)` is
    # read rather than refused.
    constants: dict[str, str] = {}
    for top_level in tree.body:
        if (
            isinstance(top_level, ast.Assign)
            and isinstance(top_level.value, ast.Constant)
            and isinstance(top_level.value.value, str)
        ):
            for target in top_level.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = top_level.value.value

    executed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "executemany", "executescript"}:
            continue
        assert node.args, "every execute() must carry a statement this test can read"
        called_with = node.args[0]
        if isinstance(called_with, ast.Constant) and isinstance(called_with.value, str):
            executed.append(called_with.value)
        elif isinstance(called_with, ast.Name) and called_with.id in constants:
            executed.append(constants[called_with.id])
        elif isinstance(called_with, ast.JoinedStr):
            executed.append(
                "".join(
                    part.value
                    for part in called_with.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
        else:
            raise AssertionError(
                "SQL built from a non-constant expression cannot be audited; the registry's "
                "append-only guarantee is structural and must stay readable"
            )

    assert executed, "the audit must actually find the statements, or it proves nothing"
    for whole in executed:
        for statement in (one for one in whole.split(";") if one.strip()):
            verb = statement.strip().split(None, 1)[0].upper()
            assert verb in {"SELECT", "INSERT", "CREATE", "BEGIN", "PRAGMA"}, (
                f"the registry executed a {verb} statement: {statement[:70]!r}. Append-only "
                f"means no UPDATE and no DELETE reaches the trial table, ever"
            )


def test_the_public_surface_offers_no_mutating_verb() -> None:
    """A method that does not exist cannot be called by a future author in a hurry.

    Kept beside the AST test rather than replaced by it: this one catches a mutating method that
    reaches the table through a helper, and the names it screens for are the ones a hurried author
    would reach for. The `A.128` review defeated it with `forget()` and `revise()`, which is why
    the AST test above is the load-bearing one.
    """
    registry = HonestTrialRegistry(Path("/nonexistent/registry.sqlite3"))
    for forbidden in ("delete", "remove", "update", "edit", "amend", "purge", "forget", "revise"):
        assert not any(forbidden in name for name in dir(registry) if not name.startswith("_")), (
            f"the public surface must not offer `{forbidden}`"
        )


def test_a_trial_appended_against_a_stale_tail_is_refused(tmp_path: Path) -> None:
    """Two writers racing must not silently interleave into a chain that no longer verifies."""
    registry = _registry(tmp_path)
    first = _record(registry)
    with pytest.raises(TrialRegistryError):
        registry.append_recorded(
            search=SEARCH,
            parameters="horizon=9",
            outcome=TrialOutcome.COMPLETED,
            fitness=0.1,
            ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            previous_digest="not-the-tail",
        )
    assert registry.cumulative_trials() == 1
    assert first.digest == registry.tail_digest()


def test_a_fitness_on_a_trial_that_produced_none_is_refused(tmp_path: Path) -> None:
    """An abandoned or errored run has no score. Allowing one would let a discarded trial be
    recorded with a flattering number attached to it."""
    registry = _registry(tmp_path)
    for outcome in (TrialOutcome.ABANDONED, TrialOutcome.ERRORED, TrialOutcome.DISCARDED):
        with pytest.raises(TrialRegistryError):
            registry.record(
                search=SEARCH,
                parameters="horizon=5",
                outcome=outcome,
                fitness=1.23,
                ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            )
    assert registry.cumulative_trials() == 0


def test_sequence_numbers_are_gapless_and_monotonic(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    records = [_record(registry, parameters=f"horizon={index}") for index in range(10)]
    assert [one.sequence for one in records] == list(range(1, 11))


# --------------------------------------------------------------- tamper evidence


def test_a_clean_chain_verifies(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for index in range(25):
        _record(registry, parameters=f"horizon={index}")
    assert registry.verify_chain() is None


def test_a_removed_trial_is_located_not_merely_suspected(tmp_path: Path) -> None:
    """**The test that carries the design.**

    An append-only API is a promise by the author to their future self, and the future self is
    exactly the party with an incentive to drop a trial that spoiled a result. The chain makes the
    promise checkable by someone who does not trust the author — including a later me.

    Deleted directly in SQLite, behind the module's back, which is the only way it could actually
    happen.
    """
    registry = _registry(tmp_path)
    for index in range(10):
        _record(registry, parameters=f"horizon={index}")
    assert registry.verify_chain() is None

    with sqlite3.connect(registry.store) as connection:
        connection.execute("DELETE FROM strategy_trial WHERE sequence = 5")
        connection.commit()

    broken_at = registry.verify_chain()
    assert broken_at == 6, (
        "the chain must LOCATE the break at the record after the missing one, not merely report "
        f"that something is wrong; got {broken_at}"
    )
    assert registry.cumulative_trials() == 9, "and the count is visibly short"


def test_an_edited_fitness_is_located(tmp_path: Path) -> None:
    """Removing a trial flatters `N`; editing one flatters the RESULT. The same chain catches
    both, because the digest covers every field rather than only the identity."""
    registry = _registry(tmp_path)
    for index in range(6):
        _record(registry, parameters=f"horizon={index}")

    with sqlite3.connect(registry.store) as connection:
        connection.execute("UPDATE strategy_trial SET fitness = 99.0 WHERE sequence = 3")
        connection.commit()

    assert registry.verify_chain() == 3


def test_an_edited_outcome_is_located(tmp_path: Path) -> None:
    """The subtlest edit: quietly reclassify a DISCARDED trial as ERRORED so it reads as bad luck
    rather than a judgement call. Same chain, same detection."""
    registry = _registry(tmp_path)
    for index in range(4):
        _record(registry, outcome=TrialOutcome.DISCARDED, parameters=f"h={index}")

    with sqlite3.connect(registry.store) as connection:
        connection.execute(
            "UPDATE strategy_trial SET outcome = ? WHERE sequence = 2",
            (TrialOutcome.ERRORED.value,),
        )
        connection.commit()

    assert registry.verify_chain() == 2


@pytest.mark.property
@given(outcomes=strategy.lists(strategy.sampled_from(list(TrialOutcome)), min_size=1, max_size=40))
@settings(max_examples=50, deadline=None)
def test_any_append_only_history_verifies(
    outcomes: list[TrialOutcome], tmp_path_factory: pytest.TempPathFactory
) -> None:
    registry = HonestTrialRegistry(tmp_path_factory.mktemp("registry") / "trial_registry.sqlite3")
    for index, outcome in enumerate(outcomes):
        _record(registry, outcome=outcome, parameters=f"h={index}")
    assert registry.verify_chain() is None
    assert registry.cumulative_trials() == len(outcomes)


def test_a_digest_depends_on_every_record_before_it(tmp_path: Path) -> None:
    """The chain property itself: change anything early and every later digest moves. Without it,
    a tamperer could rewrite one record and re-hash only that one."""
    first = _registry(tmp_path / "a")
    second = _registry(tmp_path / "b")
    for index in range(5):
        _record(first, parameters=f"horizon={index}")
    for index in range(5):
        _record(second, parameters=f"horizon={index}" if index != 1 else "horizon=999")
    assert first.tail_digest() != second.tail_digest()


def test_the_digest_is_stable_across_processes(tmp_path: Path) -> None:
    """A chain that depended on `hash()` would verify in one process and fail in the next, which
    would make tamper evidence indistinguishable from a restart."""
    registry = _registry(tmp_path)
    recorded = _record(registry)
    reopened = HonestTrialRegistry(registry.store)
    assert reopened.tail_digest() == recorded.digest
    assert reopened.verify_chain() is None


# ------------------------------------------- what the first pass did not pin (`A.128` review)
#
# 26 of 43 mutations survived. The hash math was sound; every survivor was wiring — an unanchored
# chain length, three unpinned cryptographic constants, four unguarded validation branches, and an
# R.05 script no test touched. Fourth review running, same shape.


def test_a_truncated_tail_is_located(tmp_path: Path) -> None:
    """`H1`, and it is the only edit an under-reporting author actually needs.

    A forward-walking chain stops when rows run out, so a SHORTER chain is a valid chain. Measured
    on the first implementation: 10 trials, `DELETE FROM strategy_trial WHERE sequence >= 8`,
    `count = 7`, `verify_chain() = None`. The interior deletion the flagship test covers is the
    case with no motive — nobody removes trial 5 of 10 when removing 10, 9 and 8 is free.

    The witness file is the only thing that knows how long the history is meant to be.
    """
    registry = _registry(tmp_path)
    for index in range(10):
        _record(registry, parameters=f"horizon={index}")
    assert registry.verify_chain() is None

    with sqlite3.connect(registry.store) as connection:
        connection.execute("DELETE FROM strategy_trial WHERE sequence >= 8")
        connection.commit()

    assert registry.cumulative_trials() == 7
    assert registry.verify_chain() == 8, "the first sequence the witness saw and the table lacks"


def test_the_witness_records_every_append(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for index in range(5):
        _record(registry, parameters=f"h={index}")
    witnessed = registry.witnessed_sequences()
    assert [sequence for sequence, _ in witnessed] == [1, 2, 3, 4, 5]
    assert witnessed[-1][1] == registry.tail_digest()


def test_the_digest_is_pinned_to_a_literal(tmp_path: Path) -> None:
    """One assertion killing three survivors at once — `GENESIS_DIGEST` altered or emptied, the
    field separator removed (which CREATES collisions), and `sha256` swapped for `md5` (which
    makes forgery cheap rather than merely possible). A cryptographic constant nothing pins is the
    same defect as an unpinned magnitude constant, one layer down."""
    from nse_algo_trader.validation.honest_trial_registry import GENESIS_DIGEST, _digest_for

    assert GENESIS_DIGEST == "0" * 64
    assert (
        _digest_for(
            sequence=1,
            search="s",
            parameters="p",
            outcome=TrialOutcome.COMPLETED,
            fitness=0.5,
            ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            previous_digest=GENESIS_DIGEST,
        )
        == "dbc95cb99ecc93bd178137b3b559b4a86742ae7f8821e7547583c791995760fd"
    )


def test_fields_cannot_be_confused_across_a_boundary(tmp_path: Path) -> None:
    """`M2`. A `\\x1f` join has no field boundary, so these two produced the SAME digest — which
    let a trial be moved out of a per-search count with the chain still verifying. Both fields are
    free text, so it was reachable."""
    from nse_algo_trader.validation.honest_trial_registry import GENESIS_DIGEST, _digest_for

    def digest_of(search: str, parameters: str) -> str:
        return _digest_for(
            sequence=1,
            search=search,
            parameters=parameters,
            outcome=TrialOutcome.COMPLETED,
            fitness=0.5,
            ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            previous_digest=GENESIS_DIGEST,
        )

    assert digest_of("sweep-a\x1f", "horizon=5") != digest_of("sweep-a", "\x1fhorizon=5")


def test_a_non_finite_fitness_is_refused_rather_than_breaking_the_chain(tmp_path: Path) -> None:
    """`M1`. `NaN` digests one way and returns from SQLite as `NULL`, and `-0.0` returns as `0.0`,
    so either wrote a record that could never verify again — and because the class is append-only
    the false break was PERMANENT. A false break is worse than none: it teaches the reader to
    ignore the one signal this engine exists to raise."""
    registry = _registry(tmp_path)
    for degenerate in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(TrialRegistryError):
            registry.record(
                search=SEARCH,
                parameters="h=1",
                outcome=TrialOutcome.COMPLETED,
                fitness=degenerate,
                ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            )
    _record(registry, fitness=-0.0)
    assert registry.verify_chain() is None, "-0.0 must normalise, not break the chain"


def test_an_unparseable_field_is_located_rather_than_raised(tmp_path: Path) -> None:
    """`H2`, and the docstring claimed the opposite. An edit putting a non-value into a covered
    field reached `TrialOutcome(...)`, `datetime.fromisoformat` or `float()` BEFORE the digest
    comparison, so the tamper this method exists to locate arrived as a stack trace."""
    for column, value in (
        ("outcome", "succeeded"),
        ("ran_at", "not a date"),
        ("fitness", "n/a"),
    ):
        registry = HonestTrialRegistry(tmp_path / f"{column}.sqlite3")
        for index in range(3):
            _record(registry, parameters=f"h={index}")
        with sqlite3.connect(registry.store) as connection:
            connection.execute(
                f"UPDATE strategy_trial SET {column} = ? WHERE sequence = 2",
                (value,),
            )
            connection.commit()
        assert registry.verify_chain() == 2, f"an unparseable {column} is a break at 2"


def test_a_store_that_is_not_a_registry_answers_rather_than_raising(tmp_path: Path) -> None:
    """`H2`. `exists()` tested FILE existence, so an empty file, a different database or any
    non-db file made every read raise `OperationalError` — drawing the `A.41` distinction one
    level too shallow."""
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE something_else (x INTEGER)")
        connection.commit()
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database at all")

    for path in (empty, other, junk):
        registry = HonestTrialRegistry(path)
        assert not registry.exists()
        assert registry.cumulative_trials() == 0
        assert registry.trials_by_outcome() == dict.fromkeys(TrialOutcome, 0)
        assert registry.verify_chain() is None


def test_a_naive_timestamp_and_a_scoreless_completion_are_both_refused(tmp_path: Path) -> None:
    """Two guards whose mutations survived: dropping the timezone check, and dropping "a completed
    trial must carry the fitness it produced"."""
    registry = _registry(tmp_path)
    with pytest.raises(TrialRegistryError):
        registry.record(
            search=SEARCH,
            parameters="h=1",
            outcome=TrialOutcome.COMPLETED,
            fitness=0.5,
            ran_at=datetime(2026, 8, 16, 12, 0),  # noqa: DTZ001 — the point of the test
        )
    with pytest.raises(TrialRegistryError):
        registry.record(
            search=SEARCH,
            parameters="h=1",
            outcome=TrialOutcome.COMPLETED,
            fitness=None,
            ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        )
    assert registry.cumulative_trials() == 0


def test_an_edit_to_any_covered_field_is_located(tmp_path: Path) -> None:
    """Three mutations survived by dropping `search`, `sequence` or `ran_at` from the digest
    payload — no test edited those columns. Swept over every covered column instead of three."""
    for column, value in (
        ("search", "a-different-sweep"),
        ("parameters", "horizon=999"),
        ("ran_at", "2020-01-01T00:00:00+00:00"),
        ("fitness", 42.0),
    ):
        registry = HonestTrialRegistry(tmp_path / f"edit-{column}.sqlite3")
        for index in range(4):
            _record(registry, parameters=f"h={index}")
        with sqlite3.connect(registry.store) as connection:
            connection.execute(
                f"UPDATE strategy_trial SET {column} = ? WHERE sequence = 2",
                (value,),
            )
            connection.commit()
        assert registry.verify_chain() == 2, f"an edited {column} must be located"


def test_an_integer_fitness_still_verifies(tmp_path: Path) -> None:
    """The `float()` cast inside the digest is load-bearing: an `int` would digest as `"1"` on
    write and `"1.0"` on verify, and the mutation removing it survived."""
    registry = _registry(tmp_path)
    registry.record(
        search=SEARCH,
        parameters="h=1",
        outcome=TrialOutcome.COMPLETED,
        fitness=1,
        ran_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    assert registry.verify_chain() is None


def test_the_outcome_split_respects_the_search(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _record(registry, search="a", outcome=TrialOutcome.COMPLETED)
    _record(registry, search="b", outcome=TrialOutcome.DISCARDED)
    assert registry.trials_by_outcome(search="a")[TrialOutcome.COMPLETED] == 1
    assert registry.trials_by_outcome(search="a")[TrialOutcome.DISCARDED] == 0
    assert registry.trials_by_outcome(search="b")[TrialOutcome.DISCARDED] == 1


# --------------------------------------- the `R.05` reconstruction, which had NO test (`M9`)
#
# Eight mutations of the script survived the first pass, including recording NOTHING, counting only
# WINNING mechanisms (the flattering direction), and ignoring a broken chain. `pythonpath =
# ["scripts"]` was added to `pyproject.toml` by an earlier review for exactly this reason and was
# still unused here — the same finding one review later, on the next script.


def _experience_memory(tmp_path: Path, rows: list[tuple[str, str, float | None]]) -> Path:
    store = tmp_path / "experience_memory.sqlite3"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE experience_nodes (experiment_id TEXT, strategy_tag TEXT, "
            "mechanism_name TEXT, realized_return_fraction REAL)"
        )
        connection.executemany(
            "INSERT INTO experience_nodes VALUES (?,?,?,?)",
            [
                (f"e{index}", tag, mechanism, ret)
                for index, (tag, mechanism, ret) in enumerate(rows)
            ],
        )
        connection.commit()
    return store


def test_a_universe_relabel_is_not_a_new_hypothesis(tmp_path: Path) -> None:
    """`M4`. Four of the nine real `(strategy_tag, mechanism_name)` pairs were ONE mechanism
    spelled three ways — `...regime`, `...regime [index]`, `...regime [stock]`. Counting them
    separately overstated the real search size by 80% (9 against 5), and the script's own docstring
    says inflating `N` makes every gate harsher than the truth and is "just as wrong". A schema
    change is not hypothesis generation."""
    from verify_trial_registry_on_real_data import declared_mechanisms

    store = _experience_memory(
        tmp_path,
        [
            ("credit_spread_v1", "premium in a range-bound regime", 0.01),
            ("credit_spread_v1", "premium in a range-bound regime [index]", 0.03),
            ("credit_spread_v1", "premium in a range-bound regime [stock]", 0.02),
            ("orb_v1", "breakout continuation", -0.01),
        ],
    )
    mechanisms = declared_mechanisms(store)
    assert len(mechanisms) == 2, (
        f"three spellings of one mechanism are one hypothesis: {mechanisms}"
    )
    names = {name for name, _, _ in mechanisms}
    assert "credit_spread_v1 :: premium in a range-bound regime" in names
    merged = next(count for name, count, _ in mechanisms if "credit_spread" in name)
    assert merged == 3, "and their trades pool"


def test_the_pooled_mean_is_over_all_the_trades_not_the_labels(tmp_path: Path) -> None:
    """Averaging the per-label averages would weight a 1-trade label like a 1,000-trade one."""
    from verify_trial_registry_on_real_data import declared_mechanisms

    store = _experience_memory(
        tmp_path,
        [
            ("s", "m", 1.0),
            ("s", "m [index]", 4.0),
            ("s", "m [index]", 4.0),
            ("s", "m [stock]", 1.0),
        ],
    )
    ((_, count, mean),) = declared_mechanisms(store)
    assert count == 4
    assert mean == pytest.approx((1.0 + 4.0 + 4.0 + 1.0) / 4)


def test_a_mechanism_with_no_realised_return_is_discarded_not_scored_zero(
    tmp_path: Path,
) -> None:
    """`M5`. The module refuses a fitness on a non-completed trial to stop "a discarded trial
    carrying a number it never earned"; its only caller invented one. And `0.0` is not neutral —
    seven of the nine real mechanisms have a NEGATIVE mean, so a mechanism with no data at all
    would have been recorded as the third-best hypothesis in the registry."""
    from verify_trial_registry_on_real_data import declared_mechanisms, reconstruct

    store = _experience_memory(tmp_path, [("s", "unscored", None), ("s", "scored", -0.02)])
    registry = _registry(tmp_path)
    assert reconstruct(registry, declared_mechanisms(store)) == 2
    counts = registry.trials_by_outcome()
    assert counts[TrialOutcome.DISCARDED] == 1, "no realised return is no score"
    assert counts[TrialOutcome.COMPLETED] == 1
    assert registry.verify_chain() is None


def test_a_half_finished_reconstruction_can_be_resumed(tmp_path: Path) -> None:
    """`H4`, and it is the engine's own defining failure aimed at itself.

    The first version skipped the whole loop when the search had ANY recorded trial, so a run that
    died after mechanism 1 of 9 left the registry permanently short by eight — and exited 0. An
    engine whose entire purpose is that `N` must not be flattered shipped a reconstruction pass
    whose failure mode was a flattered `N` reporting success.
    """
    from verify_trial_registry_on_real_data import declared_mechanisms, reconstruct

    store = _experience_memory(
        tmp_path, [("s", f"mechanism-{index}", 0.01 * index) for index in range(5)]
    )
    mechanisms = declared_mechanisms(store)
    registry = _registry(tmp_path)

    assert reconstruct(registry, mechanisms[:2]) == 2, "a run that died after two"
    assert registry.cumulative_trials() == 2

    assert reconstruct(registry, mechanisms) == 3, "resuming records only what is missing"
    assert registry.cumulative_trials() == 5
    assert reconstruct(registry, mechanisms) == 0, "and running again adds nothing"
    assert registry.cumulative_trials() == 5
    assert registry.verify_chain() is None


def test_the_reconstruction_records_every_mechanism_it_found(tmp_path: Path) -> None:
    """Kills the mutations that recorded nothing, or only the winners — the flattering direction."""
    from verify_trial_registry_on_real_data import declared_mechanisms, reconstruct

    store = _experience_memory(
        tmp_path,
        [("s", "winner", 0.05), ("s", "loser", -0.05), ("s", "flat", 0.0)],
    )
    mechanisms = declared_mechanisms(store)
    assert len(mechanisms) == 3
    registry = _registry(tmp_path)
    assert reconstruct(registry, mechanisms) == 3
    assert registry.cumulative_trials() == 3, (
        "a losing mechanism is a hypothesis that was tested; dropping it flatters the search size"
    )
