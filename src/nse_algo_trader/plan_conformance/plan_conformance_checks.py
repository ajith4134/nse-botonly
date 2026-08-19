"""The four checks that make "the work follows the plan" mechanical instead of remembered.

**Why this exists.** On 2026-08-17 the operator had to point out, twice in one session, that the
work
had drifted from the plan: the six segment holons — which `L5.25` and `A.01` call autonomous
**bots**
that own their strategies, relevance models, memory and track record — were being written up as
"adapters", and the cash-first build shape had quietly displaced `A.01`'s "all six built from the
start". Both drifts were caught by a human reading prose. The instruction that followed was that
following the plan must be **automatic**, not something the operator polices.

`R.02` and `R.03` are already enforced this way — `check_no_committed_credentials.py` and
`check_no_hardcoded_money.py` run in the Stop hook and fail the build. Rules that live only in a
document get followed until attention lapses; rules with a check get followed. These are the checks
for plan conformance:

1. **Every cited plan id exists.** A module saying it implements `` `L1.16` `` when the plan
   catalogues no such entry is either a stale rename or work done against an id nobody wrote down.
2. **New modules cite the plan entry they implement.** Enforced as a ratchet against a recorded
   baseline rather than retroactively, so it can only improve — `R.06`'s no-orphans rule made
   checkable at the level of "which planned thing is this?".
3. **A ticked task's artifact exists.** A `- [x]` whose text names a `src/` or `scripts/` path that
   is not on disk is a tick that outran the work.
4. **The plan's own vocabulary is used.** Where the plan names a thing, a different word for the
same
   thing is drift — this is the check that would have caught "segment adapter" the moment it was
   written, without the operator reading it.

Deliberately NOT an engine (`R.23b`): these are file checks. No solver, no carried state, no
allocation changed. Named for what they are."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nse_algo_trader.plan_conformance.plan_entry_catalogue import (
    REPOSITORY_ROOT,
    PlanEntryCatalogue,
    find_cited_plan_entries,
)

CHECKED_SOURCE_DIRECTORIES = ("src", "scripts")
CHECKED_DOCUMENT_DIRECTORIES = ("docs",)

GOVERNING_DOCUMENTS = (
    Path("docs/ajith_final_plan.md"),
    Path("docs/ajith_final_todo.md"),
    Path("docs/CLAUDE_OPINIONS.md"),
)
"""The documents that DEFINE ids are not scanned for citations of them.

A definition and a citation look identical to a text scan, so including these would make every
document trivially self-consistent and the check meaningless. """


@dataclass(frozen=True)
class ConformanceFinding:
    """One way the work has drifted from the plan."""

    check_name: str
    file_path: Path
    line_number: int
    detail: str

    def describe(self) -> str:
        location = self.file_path.relative_to(REPOSITORY_ROOT)
        return f"{location}:{self.line_number}: [{self.check_name}] {self.detail}"


@dataclass(frozen=True)
class AcceptedDrift:
    """A finding that is knowingly accepted, with the reason recorded next to it.

    `R.11` forbids silent skips, so an exemption is not an ignore: every accepted drift carries a
    reason, and the checker PRINTS all of them on every run. An exemption nobody sees is the same
    failure as no check at all."""

    check_name: str
    identifier: str
    reason: str


ACCEPTED_DRIFTS: tuple[AcceptedDrift, ...] = (
    AcceptedDrift(
        check_name="cited-entry-exists",
        identifier="L1.16",
        reason=(
            "Cited by scripts/run_daily_operations.py:1120 and by plan entry A.-region prose, but "
            "never catalogued as its own **L1.16** entry in the plan. Found by this check on the "
            "day it was written. Tracked in BACKLOG until the entry is either written or the "
            "citations are repointed — accepted here so the check can go live now rather than "
            "waiting on a plan edit, NOT because the finding is wrong."
        ),
    ),
)

CANONICAL_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    (
        r"segment[- ]adapters?\b",
        "segment bot",
        "L5.25 calls them autonomous segment BOTS that own their strategies, relevance models, "
        "risk sub-limits, memory and track record. 'Adapter' under-scopes them to configuration, "
        "and under R.23b a bot gets the full engine loop while an adapter does not. This is the "
        "exact drift the operator had to correct by hand on 2026-08-17.",
    ),
    (
        r"\bsix adapters\b",
        "six segment bots",
        "Same drift as above, in the counted form. A.130 originally shipped with this wording.",
    ),
)
"""Plan-canonical term, the word that must be used, and WHY the plan says so.

Every row cites the entry that makes it canonical, because a vocabulary rule with no source is an
opinion and would rightly be ignored. Rows are added when a drift is **actually observed** — this is
a
record of real mistakes, not a style guide invented up front.

A row that was tried and REMOVED, recorded so it is not re-added: `five segments` -> `six segments`,
on the reasoning that `A.01` struck "five segments" in favour of six. It fired nine times on its
first run and every hit was correct English — "blocks five of six segments", "the other five
segments", "five segments have deep history". A check that fires on correct text trains its reader
to
skim past it, which costs more than the drift it would catch. The distinction it needed to draw — is
this text CLAIMING the project has five segments, or counting five of the six? — is not one a
regular
expression can make, and pretending otherwise would have made the honest rows less believed. """

_VOCABULARY_QUOTATION_MARKERS = ("CORRECTED", "superseded", "SUPERSEDED", "~~", "originally")
"""A line that is RECORDING the drift is not committing it.

The plan keeps its own errors visible rather than rewriting them away, so the correction notes
themselves contain the forbidden phrase. Flagging those would force the project to hide its
mistakes to satisfy the checker, which is the opposite of what it is for. """


_QUOTATION_PAIRS = (('"', '"'), ("“", "”"), ("'", "'"), ("«", "»"))


def _phrase_is_quoted(line_text: str, start: int, end: int) -> bool:
    """True when the matched phrase is IMMEDIATELY wrapped in quotation marks.

    `A.130`'s correction note reads: *an earlier wording of this clause said "six adapters", which
    under-scoped them*. That line COMMITS no drift — it quotes the drift in order to correct it —
    but
    the correction keyword sits on the previous line, so the marker list alone does not save it.

    The test is deliberately "is the character immediately before an opening quote and the character
    immediately after a closing one", NOT "is the quote count before this match odd". The parity
    version was written first and was wrong in a way worth recording: a Python docstring opens with
    three quote characters, so `\"\"\"A segment adapter owns the denominator.\"\"\"` has odd parity
    at
    the match and was silently treated as a quotation. That made the check blind to drift inside
    every docstring in the repository — which is where module-level vocabulary actually lives. The
    test that caught it is `test_the_vocabulary_check_can_actually_fail`, which exists precisely
    because a check that cannot fail proves nothing."""
    if start == 0 or end >= len(line_text):
        return False
    return any(
        line_text[start - 1] == opening and line_text[end] == closing
        for opening, closing in _QUOTATION_PAIRS
    )


TICKED_TASK_PATTERN = re.compile(r"^\s*-\s*\[x\]", re.IGNORECASE)
NAMED_ARTIFACT_PATTERN = re.compile(r"`((?:src|scripts|tests)/[A-Za-z0-9_./-]+\.py)`")

UNCITED_MODULE_BASELINE = 26
"""How many src modules cite no plan id. Measured 2026-08-17; started at 27, now 26.

Lowered when `bitemporal_bar_store.py` gained citations while its table was renamed — which is
the ratchet doing its job: the number fell because ordinary work made it fall, not because
anyone set out to move it.

A ratchet, not a target: the count may fall and must never rise, so every NEW module says which
planned thing it implements while the existing 23 are cleaned up as they are touched. Lowering this
number is the only permitted edit. """


def _repository_files(directories: Sequence[str], suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for directory in directories:
        root = REPOSITORY_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in suffixes and path.is_file() and "__pycache__" not in path.parts:
                found.append(path)
    return found


def check_every_cited_plan_entry_exists(
    catalogue: PlanEntryCatalogue | None = None,
) -> list[ConformanceFinding]:
    """A citation of an id the governing documents never define is drift."""
    catalogue = catalogue or PlanEntryCatalogue()
    accepted = {
        drift.identifier for drift in ACCEPTED_DRIFTS if drift.check_name == "cited-entry-exists"
    }
    findings: list[ConformanceFinding] = []
    files = _repository_files(
        (*CHECKED_SOURCE_DIRECTORIES, *CHECKED_DOCUMENT_DIRECTORIES), (".py", ".md")
    )
    for path in files:
        if path.relative_to(REPOSITORY_ROOT) in GOVERNING_DOCUMENTS:
            continue
        for citation in find_cited_plan_entries(path.read_text(encoding="utf-8"), path):
            resolvable = citation.resolvable_identifier
            if catalogue.defines(resolvable) or citation.identifier in accepted:
                continue
            home = catalogue.namespace_home(resolvable).relative_to(REPOSITORY_ROOT)
            findings.append(
                ConformanceFinding(
                    check_name="cited-entry-exists",
                    file_path=path,
                    line_number=citation.line_number,
                    detail=(
                        f"cites `{citation.identifier}`, which {home} does not define. Either "
                        f"the entry was renamed and this citation is stale, or work was done "
                        f"against an id that was never catalogued."
                    ),
                )
            )
    return findings


def check_new_modules_cite_a_plan_entry(
    baseline: int = UNCITED_MODULE_BASELINE,
) -> list[ConformanceFinding]:
    """The count of src modules citing no plan id must never rise (`R.06` made checkable)."""
    uncited: list[Path] = []
    for path in _repository_files(("src",), (".py",)):
        if path.name == "__init__.py":
            continue
        if not find_cited_plan_entries(path.read_text(encoding="utf-8"), path):
            uncited.append(path)
    if len(uncited) <= baseline:
        return []
    newest = sorted(uncited, key=lambda path: path.stat().st_mtime, reverse=True)[
        : len(uncited) - baseline
    ]
    return [
        ConformanceFinding(
            check_name="module-cites-plan-entry",
            file_path=path,
            line_number=1,
            detail=(
                f"names no plan entry, and the uncited-module count has risen to {len(uncited)} "
                f"against a baseline of {baseline}. Say which planned entry this module implements "
                f"in its docstring, in backticks — or if it implements nothing planned, it is an "
                f"orphan under R.06 and needs a plan entry before it needs code."
            ),
        )
        for path in newest
    ]


def check_ticked_tasks_have_their_artifact(
    todo_path: Path = REPOSITORY_ROOT / "docs" / "ajith_final_todo.md",
) -> list[ConformanceFinding]:
    """A task ticked `[x]` whose text names a source path must have that path on disk."""
    findings: list[ConformanceFinding] = []
    for line_number, line_text in enumerate(
        todo_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not TICKED_TASK_PATTERN.match(line_text):
            continue
        for named in NAMED_ARTIFACT_PATTERN.findall(line_text):
            if (REPOSITORY_ROOT / named).exists():
                continue
            findings.append(
                ConformanceFinding(
                    check_name="ticked-task-has-artifact",
                    file_path=todo_path,
                    line_number=line_number,
                    detail=(
                        f"is ticked and names `{named}`, which does not exist. A tick that outran "
                        f"its artifact is the shape of progress that cannot be trusted."
                    ),
                )
            )
    return findings


def check_plan_vocabulary_is_used(
    vocabulary: Iterable[tuple[str, str, str]] = CANONICAL_VOCABULARY,
) -> list[ConformanceFinding]:
    """Where the plan names a thing, a different word for it is drift."""
    compiled = [
        (re.compile(pattern, re.IGNORECASE), canonical, why)
        for pattern, canonical, why in vocabulary
    ]
    findings: list[ConformanceFinding] = []
    files = _repository_files(
        (*CHECKED_SOURCE_DIRECTORIES, *CHECKED_DOCUMENT_DIRECTORIES), (".py", ".md")
    )
    for path in files:
        if path.name == Path(__file__).name:
            continue  # this module quotes every forbidden phrase by definition
        for line_number, line_text in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(marker in line_text for marker in _VOCABULARY_QUOTATION_MARKERS):
                continue
            for pattern, canonical, why in compiled:
                match = pattern.search(line_text)
                if match is not None and not _phrase_is_quoted(
                    line_text, match.start(), match.end()
                ):
                    findings.append(
                        ConformanceFinding(
                            check_name="plan-vocabulary",
                            file_path=path,
                            line_number=line_number,
                            detail=(
                                f'says "{pattern.pattern}" where the plan says "{canonical}". {why}'
                            ),
                        )
                    )
    return findings


@dataclass
class ConformanceReport:
    """Every finding, plus the exemptions, so both are visible at once."""

    findings: list[ConformanceFinding] = field(default_factory=list)
    accepted: tuple[AcceptedDrift, ...] = ACCEPTED_DRIFTS

    @property
    def conforms(self) -> bool:
        return not self.findings

    def describe(self) -> str:
        lines: list[str] = []
        if self.findings:
            lines.append(f"PLAN CONFORMANCE FAILED — {len(self.findings)} finding(s):")
            lines.extend(f"  {finding.describe()}" for finding in self.findings)
        else:
            lines.append("plan conformance: the work matches the governing documents")
        if self.accepted:
            lines.append(f"  accepted drifts still open ({len(self.accepted)}), surfaced per R.11:")
            lines.extend(
                f"    {drift.check_name} `{drift.identifier}` — {drift.reason}"
                for drift in self.accepted
            )
        return "\n".join(lines)


def run_all_plan_conformance_checks() -> ConformanceReport:
    """Every check, in one report — the entry point the Stop hook and the tests both use."""
    return ConformanceReport(
        findings=[
            *check_every_cited_plan_entry_exists(),
            *check_new_modules_cite_a_plan_entry(),
            *check_ticked_tasks_have_their_artifact(),
            *check_plan_vocabulary_is_used(),
        ]
    )
