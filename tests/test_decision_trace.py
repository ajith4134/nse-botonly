"""Tests for the decision-trace contract — `L13.29`, spec `docs/research/257`.

`A.29`'s constraint is that reasoning cannot be reconstructed after the fact, so the tests that
matter most are the ones asserting a trace cannot describe something that did not happen: an action
outside its own candidate set, an input that was not knowable yet, or a record rewritten later.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nse_algo_trader.decision_trace.decision_trace_record import (
    CandidateAction,
    ConsultedInput,
    DecisionKind,
    DecisionTrace,
    DecisionTraceError,
    DecisionTraceStore,
    GateEvaluation,
    GateOutcome,
)

INDIA = ZoneInfo("Asia/Kolkata")
MOMENT = datetime(2026, 8, 17, 11, 30, tzinfo=INDIA)


def _input(*, as_of: datetime | None = None) -> ConsultedInput:
    return ConsultedInput(
        name="deviation_sigma",
        value="-2.31",
        source="strategy.intraday_mean_reversion_engine",
        as_of=as_of or MOMENT - timedelta(minutes=5),
    )


def _gate(
    *,
    gate: str = "cost_gate",
    outcome: GateOutcome = GateOutcome.REFUSED,
    margin: Decimal | None = Decimal("-3.2"),
    threshold: Decimal | None = Decimal("30"),
) -> GateEvaluation:
    return GateEvaluation(
        gate=gate,
        outcome=outcome,
        margin=margin,
        threshold=threshold,
        detail="edge 26.8 bps against a required 30.0 bps",
    )


def _trace(
    *,
    gates: tuple[GateEvaluation, ...] | None = None,
    chosen: str = "abstain",
    candidates: tuple[CandidateAction, ...] | None = None,
    inputs: tuple[ConsultedInput, ...] | None = None,
    confidence: float | None = 0.62,
    instrument_token: int = 738561,
    mechanism: str = "deviation beyond its own rolling band",
    kind: DecisionKind = DecisionKind.ENTRY,
    ordinal: int = 0,
) -> DecisionTrace:
    if kind is DecisionKind.EXIT and candidates is None:
        candidates = (
            CandidateAction(action="hold", why_considered="keep holding to the horizon"),
            CandidateAction(action="exit_long", why_considered="the horizon expired"),
        )
    return DecisionTrace(
        decided_at=MOMENT,
        bot_identity="cash_intraday_mean_reversion_bot",
        instrument_token=instrument_token,
        trading_symbol="RELIANCE",
        inputs=inputs if inputs is not None else (_input(),),
        candidates=candidates
        if candidates is not None
        else (
            CandidateAction(action="abstain", why_considered="always available"),
            CandidateAction(action="enter_long", why_considered="deviation below its own band"),
        ),
        gates=gates if gates is not None else (_gate(),),
        chosen_action=chosen,
        kind=kind,
        decision_ordinal=ordinal,
        confidence=confidence,
        mechanism=mechanism,
    )


# ------------------------------------------------------------------ the refusals


def test_a_trace_with_no_gates_is_refused() -> None:
    """A decision reached without evaluating anything is not a decision."""
    with pytest.raises(DecisionTraceError, match="no gate"):
        _trace(gates=())


def test_an_action_outside_its_own_candidate_set_is_refused() -> None:
    """`A.29`: the record must describe what happened, not something plausible."""
    with pytest.raises(DecisionTraceError, match="not among the candidates"):
        _trace(chosen="enter_short")


def test_an_input_not_yet_knowable_at_the_decision_instant_is_refused() -> None:
    """The look-ahead check applied to REASONING rather than to prices."""
    with pytest.raises(DecisionTraceError, match="not knowable"):
        _trace(inputs=(_input(as_of=MOMENT + timedelta(seconds=1)),))


def test_an_input_knowable_exactly_at_the_instant_is_allowed() -> None:
    """A bar that closes at the decision instant IS available at it — the boundary is inclusive."""
    assert _trace(inputs=(_input(as_of=MOMENT),)).inputs


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan")])
def test_a_confidence_outside_the_unit_interval_is_refused(confidence: float) -> None:
    with pytest.raises(DecisionTraceError, match="confidence"):
        _trace(confidence=confidence)


def test_a_naive_decision_instant_is_refused() -> None:
    with pytest.raises(DecisionTraceError, match="timezone"):
        DecisionTrace(
            decided_at=datetime(2026, 8, 17, 11, 30),  # noqa: DTZ001
            bot_identity="b",
            instrument_token=1,
            trading_symbol="X",
            inputs=(),
            candidates=(CandidateAction(action="abstain", why_considered="w"),),
            gates=(_gate(),),
            chosen_action="abstain",
            confidence=None,
            mechanism="m",
        )


def test_a_trace_with_no_candidates_is_refused() -> None:
    with pytest.raises(DecisionTraceError, match="no candidate actions"):
        _trace(candidates=())


# ------------------------------------------------------------------ the inference


def test_the_binding_constraint_is_the_gate_that_decided_the_outcome() -> None:
    """A vetoing gate binds, whatever the passing gates did."""
    trace = _trace(
        gates=(
            _gate(
                gate="risk_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("500"),
                threshold=Decimal("50000"),
            ),
            _gate(
                gate="cost_gate",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("-3.2"),
                threshold=Decimal("30"),
            ),
        )
    )
    binding = trace.binding_constraint()
    assert binding.gate == "cost_gate"
    assert binding.would_have_needed == pytest.approx(3.2)


def test_margins_are_compared_after_normalising_by_each_gates_own_threshold() -> None:
    """3 bps and 300 rupees are not comparable raw; as fractions of their thresholds they are.

    The risk gate passed with Rs 500 of headroom against a Rs 50,000 limit (+0.01); the cost gate
    passed with 3 bps against a 30 bps hurdle (+0.10). The risk gate is the tighter constraint even
    though its raw margin is 166 times larger.
    """
    trace = _trace(
        chosen="enter_long",
        gates=(
            _gate(
                gate="cost_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("3"),
                threshold=Decimal("30"),
            ),
            _gate(
                gate="risk_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("500"),
                threshold=Decimal("50000"),
            ),
        ),
    )
    assert trace.binding_constraint().gate == "risk_gate"


def test_a_boolean_gate_cannot_become_the_near_miss_by_having_no_margin() -> None:
    """A halt latch is open or closed. A fake 0.0 margin would make it permanently the closest."""
    trace = _trace(
        chosen="enter_long",
        gates=(
            _gate(gate="control_latch", outcome=GateOutcome.PASSED, margin=None, threshold=None),
            _gate(
                gate="cost_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("3"),
                threshold=Decimal("30"),
            ),
        ),
    )
    assert trace.binding_constraint().gate == "cost_gate"


def test_a_closed_boolean_gate_is_the_binding_constraint_by_definition() -> None:
    """Nothing downstream ran, so no margin elsewhere is relevant."""
    trace = _trace(
        gates=(
            _gate(gate="control_latch", outcome=GateOutcome.REFUSED, margin=None, threshold=None),
            _gate(
                gate="cost_gate",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("-3.2"),
                threshold=Decimal("30"),
            ),
        )
    )
    binding = trace.binding_constraint()
    assert binding.gate == "control_latch"
    assert binding.would_have_needed is None
    assert "no margin" in binding.explanation.lower() or "boolean" in binding.explanation.lower()


def test_a_margin_against_a_zero_threshold_is_refused_rather_than_ranked() -> None:
    """It used to fall back to the RAW magnitude, which broke scale-invariance silently.

    Adversarial review, measured: with one zero-threshold gate, scaling every margin and threshold
    by 1000 changed which gate bound. A gate with no scale cannot be normalised, so it is refused
    at construction instead of being ranked on a number that means something different.
    """
    with pytest.raises(DecisionTraceError, match="ZERO threshold"):
        _gate(gate="odd", outcome=GateOutcome.PASSED, margin=Decimal("5"), threshold=Decimal("0"))


@pytest.mark.adversarial
@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_a_non_finite_margin_is_refused(bad: Decimal) -> None:
    """It round-tripped through the store unharmed and made the ranking depend on tuple ORDER."""
    with pytest.raises(DecisionTraceError, match="non-finite"):
        _gate(margin=bad)


@pytest.mark.adversarial
def test_a_margin_whose_sign_contradicts_its_outcome_is_refused() -> None:
    """It produced counterfactuals reading "it needed 2 more" about a gate already 2 past."""
    with pytest.raises(DecisionTraceError, match="contradict"):
        _gate(outcome=GateOutcome.REFUSED, margin=Decimal("2"), threshold=Decimal("1"))


@pytest.mark.adversarial
def test_a_gate_with_no_judgement_is_never_the_binding_constraint() -> None:
    """`GateVerdict.UNPRICEABLE` used to read as PASSING.

    `pre_trade_cost_gate` warns that treating "I do not know" as "it is fine" is the failure it
    exists to prevent; a string membership test downstream reintroduced it, and an unpriceable cost
    gate was reported as the binding constraint with "0.1 of headroom left".
    """
    trace = _trace(
        chosen="enter_long",
        gates=(
            _gate(gate="cost_gate", outcome=GateOutcome.NO_JUDGEMENT, margin=None, threshold=None),
            _gate(
                gate="deviation_band",
                outcome=GateOutcome.PASSED,
                margin=Decimal("3"),
                threshold=Decimal("30"),
            ),
        ),
    )
    assert trace.binding_constraint().gate == "deviation_band"


@pytest.mark.adversarial
def test_an_abstain_with_no_refusal_is_reported_unexplained_not_blamed_on_a_passer() -> None:
    """The single worst finding: 380 real traces named a PASSING gate as the cause of an abstain.

    The order-rate limiter had refused; the trace blamed `deviation_band`, which had passed. An
    affirmative false statement about a decision is worse than no panel, and is what `A.29` forbids.
    """
    trace = _trace(
        chosen="abstain",
        gates=(
            _gate(
                gate="cost_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("20"),
                threshold=Decimal("30"),
            ),
            _gate(
                gate="risk_gate",
                outcome=GateOutcome.PASSED,
                margin=Decimal("500"),
                threshold=Decimal("50000"),
            ),
        ),
    )
    binding = trace.binding_constraint()
    assert binding.gate is None
    assert not binding.explains_the_outcome
    assert "does NOT explain" in binding.explanation


@pytest.mark.adversarial
def test_a_tie_is_declared_rather_than_resolved_by_tuple_order() -> None:
    """A sensitivity report whose answer changes with argument order is not one."""
    trace = _trace(
        chosen="enter_long",
        gates=(
            _gate(
                gate="first",
                outcome=GateOutcome.PASSED,
                margin=Decimal("3"),
                threshold=Decimal("30"),
            ),
            _gate(
                gate="second",
                outcome=GateOutcome.PASSED,
                margin=Decimal("50"),
                threshold=Decimal("500"),
            ),
        ),
    )
    assert "TIED" in trace.binding_constraint().explanation


def test_a_negative_threshold_is_normalised_by_its_magnitude() -> None:
    """`abs(threshold)` was untested, so dropping it survived mutation."""
    gate = _gate(outcome=GateOutcome.PASSED, margin=Decimal("3"), threshold=Decimal("-30"))
    assert gate.normalised_margin() == pytest.approx(0.1)


def test_a_decision_that_consulted_nothing_is_refused() -> None:
    """`gates` and `candidates` were refused when empty and this was not."""
    with pytest.raises(DecisionTraceError, match="consulted NOTHING"):
        _trace(inputs=())


def test_an_input_with_a_naive_as_of_is_refused() -> None:
    """Only `decided_at` awareness was tested, so deleting this check survived mutation."""
    with pytest.raises(DecisionTraceError, match="timezone"):
        _trace(
            inputs=(
                ConsultedInput(
                    name="x",
                    value="1",
                    source="s",
                    as_of=datetime(2026, 8, 17, 11, 0),  # noqa: DTZ001
                ),
            )
        )


def test_the_counterfactual_is_stated_in_the_gates_own_unit() -> None:
    """ "3.2 more basis points" is actionable; "0.11 normalised" is not."""
    binding = _trace().binding_constraint()
    assert "3.2" in binding.explanation
    assert "cost_gate" in binding.explanation


@settings(max_examples=40, deadline=None)
@given(scale=st.integers(min_value=2, max_value=1000))
def test_scaling_every_margin_and_threshold_does_not_change_which_gate_binds(scale: int) -> None:
    """The normalisation is a ratio, so a change of units must not change the answer."""

    def build(factor: int) -> DecisionTrace:
        return _trace(
            chosen="enter_long",
            gates=(
                _gate(
                    gate="a",
                    outcome=GateOutcome.PASSED,
                    margin=Decimal(3 * factor),
                    threshold=Decimal(30 * factor),
                ),
                _gate(
                    gate="b",
                    outcome=GateOutcome.PASSED,
                    margin=Decimal(500 * factor),
                    threshold=Decimal(50000 * factor),
                ),
            ),
        )

    assert build(scale).binding_constraint().gate == build(1).binding_constraint().gate


# ------------------------------------------------------------------ the store


def test_a_trace_is_readable_back_exactly_as_written(tmp_path: Path) -> None:
    """EVERY field, not three of them.

    The first version asserted `chosen_action`, the binding gate and one `source`, and called
    itself "exactly as written". A mutant that wrote `"margin": None` for every gate — deleting the
    persisted counterfactual wholesale — passed it.
    """
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    original = _trace()
    store.record(original)
    (read_back,) = store.traces_for_session(MOMENT.date())
    assert read_back == original


def test_the_same_instant_in_two_timezones_is_one_row(tmp_path: Path) -> None:
    """Keying on the caller's spelling let one instant become two contradictory records."""
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace())
    from zoneinfo import ZoneInfo as _Zone

    elsewhere = _trace()
    shifted = DecisionTrace(
        decided_at=elsewhere.decided_at.astimezone(_Zone("UTC")),
        bot_identity=elsewhere.bot_identity,
        instrument_token=elsewhere.instrument_token,
        trading_symbol=elsewhere.trading_symbol,
        inputs=elsewhere.inputs,
        candidates=elsewhere.candidates,
        gates=elsewhere.gates,
        chosen_action=elsewhere.chosen_action,
        confidence=elsewhere.confidence,
        mechanism=elsewhere.mechanism,
    )
    with pytest.raises(DecisionTraceError, match="already recorded"):
        store.record(shifted)


def test_the_counterfactual_survives_the_round_trip(tmp_path: Path) -> None:
    """The normalised margin and the needed value are the point; both must persist."""
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace())
    (read_back,) = store.traces_for_session(MOMENT.date())
    binding = read_back.binding_constraint()
    assert binding.normalised_margin == pytest.approx(3.2 / 30)
    assert binding.would_have_needed == pytest.approx(3.2)
    assert "more and" in binding.explanation


@pytest.mark.adversarial
def test_a_trace_cannot_be_rewritten_after_the_instant(tmp_path: Path) -> None:
    """Append-only: reasoning is recorded once. A second version is a rewritten history."""
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace())
    with pytest.raises(DecisionTraceError, match="already recorded"):
        store.record(_trace(chosen="enter_long",
            gates=(_gate(outcome=GateOutcome.PASSED, margin=Decimal("3")),)))


def test_two_instruments_at_one_instant_are_distinct_traces(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace(instrument_token=738561))
    store.record(_trace(instrument_token=408065))
    assert len(store.traces_for_session(MOMENT.date())) == 2


def test_a_session_with_no_traces_returns_empty_rather_than_raising(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    assert store.traces_for_session(MOMENT.date()) == ()


# ------------------------------------------------ B19: the review's MEDIUMs, closed


@pytest.mark.adversarial
def test_the_tightest_gate_is_not_ranked_loosest_by_a_tiny_threshold() -> None:
    """`|margin|/|threshold|` is unbounded, so a razor-thin gate scored 1000 and ranked LAST.

    Measured by the review: `razor_thin` missing by 0.001 against a 1e-6 threshold was reported as
    less binding than `miles_off` missing by 29 against 30.
    """
    trace = _trace(
        gates=(
            _gate(
                gate="razor_thin",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("-0.001"),
                threshold=Decimal("0.000001"),
            ),
            _gate(
                gate="miles_off",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("-29"),
                threshold=Decimal("30"),
            ),
        )
    )
    assert trace.binding_constraint().gate == "miles_off"


def test_the_clip_does_not_distort_the_reported_counterfactual() -> None:
    """The clip is a RANKING key; `would_have_needed` stays true in the gate's own unit."""
    trace = _trace(
        gates=(
            _gate(
                gate="far",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("-500"),
                threshold=Decimal("10"),
            ),
        )
    )
    binding = trace.binding_constraint()
    assert binding.normalised_margin == pytest.approx(1.0)
    assert binding.would_have_needed == pytest.approx(500.0)


@pytest.mark.adversarial
def test_an_input_older_than_the_session_is_refused() -> None:
    """`as_of` was only upper-bounded, so 1970-01-01 was accepted."""
    with pytest.raises(DecisionTraceError, match="before this session could produce it"):
        DecisionTrace(
            decided_at=MOMENT,
            bot_identity="b",
            instrument_token=1,
            trading_symbol="X",
            inputs=(_input(as_of=datetime(1970, 1, 1, tzinfo=INDIA)),),
            candidates=(CandidateAction(action="abstain", why_considered="w"),),
            gates=(_gate(),),
            chosen_action="abstain",
            confidence=None,
            mechanism="m",
            earliest_knowable=MOMENT - timedelta(hours=2),
        )


@pytest.mark.adversarial
def test_a_recorded_trace_cannot_be_updated_or_deleted_in_the_database(tmp_path: Path) -> None:
    """Append-only must be a property of the RECORD, not of the class's method list.

    The store had no update method, and a plain `UPDATE`/`DELETE` rewrote history unopposed,
    so "reasoning is recorded, never reconstructed" held only for callers who used the API.
    """
    database = tmp_path / "traces.sqlite3"
    store = DecisionTraceStore(database)
    store.record(_trace())

    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE decision_trace SET mechanism = 'I always knew'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM decision_trace")
    connection.close()

    (survivor,) = store.traces_for_session(MOMENT.date())
    assert survivor.mechanism == "deviation beyond its own rolling band"


# ------------------------------------------------ B20: free text and self-certifying fields


def test_a_refusing_gate_does_not_forbid_acting() -> None:
    """A gate refusal is an ENTRY veto, not a global one, so "refused implies abstain" is FALSE.

    This was built as a check and removed. The claim behind it — "0 of 21,270 real traces acted
    despite a refusing gate" — did not survive: only 11 of those traces acted at all, and those 11
    cannot violate it by construction, since the emitter sets a non-null action only on the branch
    where every gate has already passed. The invariant was measured on a sample that could not
    contain a counterexample.

    The counterexample path is real and already in the runner: when the halt latch trips, the
    session squares off open positions, bypassing the entry gate deliberately. That decision acts
    while a gate refuses, and it is correct. Enforcing the rule would have discarded exactly those
    traces once exits are traced — maintaining an invariant by deleting its counterexamples.
    """
    assert _trace(
        chosen="enter_long",
        gates=(_gate(gate="halt_latch", outcome=GateOutcome.REFUSED, margin=None, threshold=None),),
    ).chosen_action == "enter_long"


@pytest.mark.adversarial
def test_a_candidate_action_outside_the_declared_vocabulary_is_refused() -> None:
    """The candidate set was self-certifying, so `teleport` and `sell_naked_call` were accepted."""
    with pytest.raises(DecisionTraceError, match="not an action"):
        DecisionTrace(
            decided_at=MOMENT,
            bot_identity="b",
            instrument_token=1,
            trading_symbol="RELIANCE",
            inputs=(_input(),),
            candidates=(
                CandidateAction(action="abstain", why_considered="always available"),
                CandidateAction(action="teleport", why_considered="why not"),
            ),
            gates=(_gate(outcome=GateOutcome.PASSED, margin=Decimal("3")),),
            chosen_action="abstain",
            confidence=None,
            mechanism="m",
            permitted_actions=frozenset({"abstain", "enter_long", "enter_short"}),
        )


def test_an_undeclared_vocabulary_leaves_candidates_unchecked_and_says_so() -> None:
    """`None` means unchecked rather than "anything goes by accident" — the absence is visible."""
    trace = _trace(
        candidates=(
            CandidateAction(action="abstain", why_considered="w"),
            CandidateAction(action="anything_at_all", why_considered="w"),
        ),
    )
    assert trace.permitted_actions is None


def test_mechanism_free_text_is_not_cross_checked_against_gates() -> None:
    """Deliberately unchecked. A substring check here could only ever subtract TRUE records.

    Attempted and removed after a review measured it. It missed its own motivating example — the
    prose was "cost gate passed comfortably", with a space, which no `cost_gate` match catches —
    along with "cost_gate: passed", "cost_gate was passed", "not passed", and every casing variant.
    Meanwhile it destroyed truthful traces by collision: `risk_gate` is a substring of
    `pre_trade_risk_gate`, so a true statement about one was judged against the other's outcome.

    And on the live store it was a pure no-op: across all 21,270 traces, zero mechanisms named any
    of their own gates. A check that cannot fire on a real defect, can fire on a real truth, and
    reads in its docstring as assurance is worse than no check. The honest structural answer is
    `binding_constraint()`, which is computed from the gates rather than asserted in prose.
    """
    assert _trace(
        gates=(_gate(gate="cost_gate", outcome=GateOutcome.REFUSED),),
        mechanism="cost_gate passed comfortably",
    ).mechanism == "cost_gate passed comfortably"


def test_one_non_conforming_row_does_not_take_down_the_whole_session(tmp_path: Path) -> None:
    """Reads reconstruct; they do not re-decide. A poison row is unremovable AND unreadable.

    `__post_init__` used to run again on the way out of the store, so a single row that failed a
    rule added later raised from `traces_for_session` — killing the entire session's panel, not one
    row — and the append-only triggers then made that row impossible to delete. Any tightening of
    the contract could brick history that was legal when it was written.
    """
    database = tmp_path / "traces.sqlite3"
    store = DecisionTraceStore(database)
    store.record(_trace())
    store.close()

    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "INSERT INTO decision_trace (bot_identity, decided_at, instrument_token, session_date,"
            " trading_symbol, chosen_action, confidence, mechanism, inputs_json, candidates_json,"
            " gates_json, null_action) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "some_other_writer",
                MOMENT.astimezone(UTC).isoformat(),
                999,
                MOMENT.date().isoformat(),
                "INFY",
                "enter_long",
                None,
                "written by a build whose rules differed from today's",
                "[]",
                "[]",
                "[]",
                "abstain",
            ),
        )
    connection.close()

    recovered = DecisionTraceStore(database).traces_for_session(MOMENT.date())
    assert len(recovered) == 2
    assert {trace.trading_symbol for trace in recovered} == {"RELIANCE", "INFY"}


# ---------------------------------------- the store must survive its own schema history


_LIVE_SHAPED_STORE = """
CREATE TABLE decision_trace (
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
    earliest_knowable TEXT,
    null_action      TEXT NOT NULL DEFAULT 'abstain',
    PRIMARY KEY (bot_identity, decided_at, instrument_token)
);
"""
"""The LIVE store's physical column order, which is NOT the order `_SCHEMA` declares.

Columns arrive in the order they were added, so a store grown by `ALTER TABLE` ends up shaped
differently from a fresh one. A positional `INSERT ... VALUES (?,...)` silently depends on the two
agreeing, and against this shape it wrote `null_action` into `earliest_knowable` — every write then
failed NOT NULL and was reported as "already recorded", which turned the whole feature off while the
diagnostic blamed duplicate instants.
"""


def test_a_store_shaped_like_the_live_one_still_accepts_and_returns_traces(
    tmp_path: Path,
) -> None:
    database = tmp_path / "older.sqlite3"
    seeded = sqlite3.connect(database)
    with seeded:
        seeded.executescript(_LIVE_SHAPED_STORE)
        seeded.execute(
            "INSERT INTO decision_trace (bot_identity, decided_at, instrument_token, session_date,"
            " trading_symbol, chosen_action, confidence, mechanism, inputs_json, candidates_json,"
            " gates_json, null_action) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cash_intraday_mean_reversion_bot",
                MOMENT.astimezone(UTC).isoformat(),
                738561,
                MOMENT.date().isoformat(),
                "RELIANCE",
                "abstain",
                None,
                "written before kind existed",
                "[]",
                "[]",
                "[]",
                "abstain",
            ),
        )
    seeded.close()

    store = DecisionTraceStore(database)
    store.record(_trace(instrument_token=999))

    recovered = store.traces_for_session(MOMENT.date())
    assert len(recovered) == 2, "the migrated row and the new one must both survive"
    # A row that predates the distinction is an ENTRY, which is TRUE of it — the emitter only ever
    # ran on the entry path, so its exits are absent rather than mislabelled.
    older = next(trace for trace in recovered if trace.instrument_token == 738561)
    assert older.kind is DecisionKind.ENTRY
    assert older.decision_ordinal == 0
    assert older.mechanism == "written before kind existed"


def test_an_entry_and_an_exit_at_the_same_instant_are_both_kept(tmp_path: Path) -> None:
    """The final step entered and squared off at one instant, and the exit vanished."""
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace())
    store.record(_trace(kind=DecisionKind.EXIT, chosen="hold"))
    assert len(store.traces_for_session(MOMENT.date())) == 2


def test_two_exit_decisions_at_one_instant_are_both_kept(tmp_path: Path) -> None:
    """Squaring off in rounds is normal, and every round after the first was being dropped.

    An exit sent for what has filled so far draws further fills out of the venue, and the residual
    needs its own exit. Those are separate decisions about different quantities — the record used
    to keep one and report the other as a rewritten history.
    """
    store = DecisionTraceStore(tmp_path / "traces.sqlite3")
    store.record(_trace(kind=DecisionKind.EXIT, chosen="hold", ordinal=0))
    store.record(_trace(kind=DecisionKind.EXIT, chosen="hold", ordinal=1))
    assert len(store.traces_for_session(MOMENT.date())) == 2

    with pytest.raises(DecisionTraceError, match="already recorded"):
        store.record(_trace(kind=DecisionKind.EXIT, chosen="hold", ordinal=1))


def test_a_gate_that_refused_at_exactly_its_threshold_does_not_claim_zero_would_have_helped(
) -> None:
    """Zero more would have changed nothing, and it was the MODAL phrasing.

    A horizon expires on a decision-step boundary by construction, so the refusal lands at exactly
    zero seconds remaining far more often than not.
    """
    explanation = _trace(
        gates=(
            _gate(
                gate="holding_horizon",
                outcome=GateOutcome.REFUSED,
                margin=Decimal("0"),
                threshold=Decimal("1800"),
            ),
        ),
    ).binding_constraint().explanation
    assert "0 more" not in explanation
    assert "any margin at all" in explanation
