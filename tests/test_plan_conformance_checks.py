"""Tests for the plan-conformance checks.

The check exists because on 2026-08-17 the operator had to point out plan drift twice in one session
and then asked for it to be caught automatically. So the most important tests here are the
adversarial ones: a check that cannot FAIL proves nothing, and a check that fires on correct text is
worse than no check because it teaches its reader to skim."""

from __future__ import annotations

from pathlib import Path

import pytest

from nse_algo_trader.plan_conformance.plan_conformance_checks import (
    ACCEPTED_DRIFTS,
    CANONICAL_VOCABULARY,
    UNCITED_MODULE_BASELINE,
    ConformanceFinding,
    ConformanceReport,
    check_every_cited_plan_entry_exists,
    check_new_modules_cite_a_plan_entry,
    check_plan_vocabulary_is_used,
    check_ticked_tasks_have_their_artifact,
    run_all_plan_conformance_checks,
)
from nse_algo_trader.plan_conformance.plan_entry_catalogue import (
    PlanCatalogueError,
    PlanEntryCatalogue,
    find_cited_plan_entries,
)

# ------------------------------------------------------------------ the catalogue reader


def _catalogue_with(plan_text: str, opinions_text: str, tmp_path: Path) -> PlanEntryCatalogue:
    plan = tmp_path / "plan.md"
    opinions = tmp_path / "opinions.md"
    plan.write_text(plan_text)
    opinions.write_text(opinions_text)
    return PlanEntryCatalogue(plan_path=plan, opinions_path=opinions)


def test_a_bolded_line_start_defines_an_entry(tmp_path: Path) -> None:
    catalogue = _catalogue_with("**L5.29**  Segment-bot protocol.\n", "", tmp_path)
    assert catalogue.defines("L5.29")


def test_an_id_mentioned_mid_sentence_is_a_reference_not_a_definition(tmp_path: Path) -> None:
    """This distinction is the whole basis of "cited but never catalogued"."""
    catalogue = _catalogue_with("The gate reads `L5.29` before sizing.\n", "", tmp_path)
    assert not catalogue.defines("L5.29")


def test_opinions_are_defined_in_their_own_file_not_the_plan(tmp_path: Path) -> None:
    """R.25 keeps measured facts, operator decisions and my judgement in three homes."""
    catalogue = _catalogue_with(
        "**A.130**  Six bots.\n", "## O.112 · 2026-08-17 · A title\n", tmp_path
    )
    assert catalogue.defines("O.112")
    assert catalogue.defines("A.130")
    assert not catalogue.defines("O.999")


def test_a_rule_sub_clause_resolves_to_its_parent_rule(tmp_path: Path) -> None:
    """`R.23b` is prose inside R.23, not its own heading — demanding a heading would reject it."""
    catalogue = _catalogue_with("**R.23**  Code generation procedure.\n", "", tmp_path)
    citations = find_cited_plan_entries("scope is set by `R.23b`", Path("x.py"))
    assert len(citations) == 1
    assert citations[0].resolvable_identifier == "R.23"
    assert catalogue.defines(citations[0].resolvable_identifier)


def test_an_unreadable_governing_document_is_an_error_not_an_empty_catalogue(
    tmp_path: Path,
) -> None:
    """A check that silently passes because it read nothing is worse than no check."""
    catalogue = PlanEntryCatalogue(
        plan_path=tmp_path / "absent.md", opinions_path=tmp_path / "b.md"
    )
    with pytest.raises(PlanCatalogueError, match="cannot read the governing document"):
        _ = catalogue.defined_plan_identifiers


@pytest.mark.parametrize(
    "identifier", ["L5.29", "A.130", "R.07", "L0.05a", "L13.29", "B.10", "O.112"]
)
def test_every_id_shape_the_project_actually_uses_is_recognised(identifier: str) -> None:
    citations = find_cited_plan_entries(f"see `{identifier}` for detail", Path("x.md"))
    assert [citation.identifier for citation in citations] == [identifier]


@pytest.mark.parametrize("not_an_id", ["3.12", "v2.0", "Decimal.0", "L5", "0.30"])
def test_things_that_look_like_ids_but_are_not_are_ignored(not_an_id: str) -> None:
    """Version numbers and attribute access appear constantly; false positives bury real ones."""
    assert find_cited_plan_entries(f"uses `{not_an_id}` here", Path("x.py")) == []


def test_an_unquoted_id_is_not_treated_as_a_citation() -> None:
    """Only backticked ids count — prose numbering would otherwise flood the check."""
    assert find_cited_plan_entries("section A.130 of the report", Path("x.md")) == []


# ------------------------------------------------------------------ the four checks


def test_the_vocabulary_check_catches_the_exact_drift_that_prompted_it() -> None:
    """ "segment adapter" is what the operator had to correct by hand on 2026-08-17."""
    findings = check_plan_vocabulary_is_used(
        [(r"segment[- ]adapters?\b", "segment bot", "L5.25 calls them bots.")]
    )
    # Run against the real tree: after the correction there must be no live occurrence left.
    assert findings == []


def test_the_vocabulary_check_can_actually_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that cannot fail proves nothing. This is the test that tests the test."""
    drifted = tmp_path / "src" / "drifted_module.py"
    drifted.parent.mkdir(parents=True)
    drifted.write_text('"""A segment adapter owns the denominator."""\n')
    monkeypatch.setattr(
        "nse_algo_trader.plan_conformance.plan_conformance_checks.REPOSITORY_ROOT", tmp_path
    )
    findings = check_plan_vocabulary_is_used(
        [(r"segment[- ]adapters?\b", "segment bot", "L5.25 calls them bots.")]
    )
    assert len(findings) == 1
    assert findings[0].check_name == "plan-vocabulary"
    assert "segment bot" in findings[0].detail


def test_a_quoted_drift_phrase_is_a_correction_not_a_commission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`A.130` records its own wrong wording in quotes; flagging that would force hiding
    mistakes."""
    correction = tmp_path / "docs" / "plan_excerpt.md"
    correction.parent.mkdir(parents=True)
    correction.write_text('an earlier wording said "six adapters", which under-scoped them\n')
    monkeypatch.setattr(
        "nse_algo_trader.plan_conformance.plan_conformance_checks.REPOSITORY_ROOT", tmp_path
    )
    assert check_plan_vocabulary_is_used([(r"\bsix adapters\b", "six segment bots", "why")]) == []


def test_a_struck_through_drift_phrase_is_also_a_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    struck = tmp_path / "docs" / "excerpt.md"
    struck.parent.mkdir(parents=True)
    struck.write_text("~~segment adapter~~ is now segment bot\n")
    monkeypatch.setattr(
        "nse_algo_trader.plan_conformance.plan_conformance_checks.REPOSITORY_ROOT", tmp_path
    )
    assert check_plan_vocabulary_is_used([(r"segment[- ]adapters?\b", "segment bot", "why")]) == []


def test_every_vocabulary_row_states_the_plan_entry_that_makes_it_canonical() -> None:
    """A vocabulary rule with no source is an opinion and would rightly be ignored."""
    for pattern, canonical, why in CANONICAL_VOCABULARY:
        assert canonical.strip(), f"{pattern} has no canonical term"
        assert len(why) > 40, f"{pattern} does not say WHY the plan makes it canonical"
        assert any(token in why for token in ("L5.25", "A.01", "A.130", "R.")), (
            f"{pattern} cites no governing entry"
        )


def test_a_ticked_task_naming_a_missing_artifact_is_a_finding(tmp_path: Path) -> None:
    todo = tmp_path / "todo.md"
    todo.write_text("- [x] **9.9** Built the thing — `src/nse_algo_trader/not_real.py`\n")
    findings = check_ticked_tasks_have_their_artifact(todo)
    assert len(findings) == 1
    assert "does not exist" in findings[0].detail


def test_an_unticked_task_naming_a_missing_artifact_is_not_a_finding(tmp_path: Path) -> None:
    """An open task is ALLOWED to name the file it will create — that is a plan, not a lie."""
    todo = tmp_path / "todo.md"
    todo.write_text("- [ ] **9.9** Will build — `src/nse_algo_trader/not_real.py`\n")
    assert check_ticked_tasks_have_their_artifact(todo) == []


def test_ticked_tasks_in_the_real_todo_all_have_their_artifacts() -> None:
    """R.05 against the real governing document, not a fixture."""
    assert check_ticked_tasks_have_their_artifact() == []


def test_the_uncited_module_ratchet_is_satisfied_by_the_real_tree() -> None:
    assert check_new_modules_cite_a_plan_entry() == []


def test_the_ratchet_fires_when_the_count_rises() -> None:
    """Lowering the baseline is the only permitted edit; this proves the direction is enforced.

    Asserts the ratchet RELATION rather than an exact count. An earlier version pinned equality with
    `UNCITED_MODULE_BASELINE` and failed the moment the count legitimately FELL — a test that breaks
    when the thing it guards improves teaches its reader to edit the guard, which is the opposite of
    what a ratchet is for.
    """
    findings = check_new_modules_cite_a_plan_entry(baseline=0)
    assert findings, "a baseline of zero must flag every uncited module"
    assert all(finding.check_name == "module-cites-plan-entry" for finding in findings)
    assert len(findings) <= UNCITED_MODULE_BASELINE, (
        f"{len(findings)} modules cite no plan entry, above the recorded baseline of "
        f"{UNCITED_MODULE_BASELINE}; new work must name the planned entry it implements"
    )
    assert check_new_modules_cite_a_plan_entry() == [], (
        "the live tree must satisfy its own baseline"
    )


def test_every_plan_id_cited_anywhere_in_the_repository_actually_exists() -> None:
    """R.05 against the real tree — this is the check's whole point."""
    assert check_every_cited_plan_entry_exists() == []


def test_a_citation_of_an_uncatalogued_id_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    citing = tmp_path / "src" / "citing_module.py"
    citing.parent.mkdir(parents=True)
    citing.write_text('"""Implements `L9.99`."""\n')
    monkeypatch.setattr(
        "nse_algo_trader.plan_conformance.plan_conformance_checks.REPOSITORY_ROOT", tmp_path
    )
    catalogue = _catalogue_with("**L5.29**  Real entry.\n", "", tmp_path)
    findings = check_every_cited_plan_entry_exists(catalogue)
    assert len(findings) == 1
    assert "L9.99" in findings[0].detail


# ------------------------------------------------------------------ the report


def test_every_accepted_drift_records_a_reason() -> None:
    """R.11 forbids a silent skip; an exemption with no reason is exactly that."""
    for drift in ACCEPTED_DRIFTS:
        assert len(drift.reason) > 80, f"{drift.identifier} is exempted without a real reason"
        assert drift.check_name


def test_accepted_drifts_are_printed_even_when_everything_passes() -> None:
    """An exemption nobody sees is the same failure as no check at all."""
    described = ConformanceReport(findings=[]).describe()
    assert "accepted drifts still open" in described
    for drift in ACCEPTED_DRIFTS:
        assert drift.identifier in described


def test_a_report_with_findings_does_not_conform_and_names_them() -> None:
    report = ConformanceReport(
        findings=[
            ConformanceFinding(
                check_name="plan-vocabulary",
                file_path=Path(__file__),
                line_number=7,
                detail="says the wrong word",
            )
        ]
    )
    assert not report.conforms
    assert "PLAN CONFORMANCE FAILED" in report.describe()
    assert "says the wrong word" in report.describe()


def test_the_whole_repository_conforms_to_its_own_plan_right_now() -> None:
    """The R.05 pass for this feature: run every check against the real governing documents."""
    report = run_all_plan_conformance_checks()
    assert report.conforms, report.describe()
