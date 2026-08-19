"""Reads the governing documents and answers "is this plan entry real?".

`docs/ajith_final_plan.md` and `docs/ajith_final_todo.md` govern this project alone. Code and docs
refer to their entries by id — `` `L5.29` ``, `` `A.130` ``, `` `R.07` `` — and those references are
how a module says which planned thing it implements. A reference to an id that was never catalogued
is drift: either the plan entry was renamed and the citation not updated, or work was done against
an
id that does not exist and nobody noticed.

This module is the reader. It knows the three id namespaces the project uses and where each is
defined, so a citation can be checked rather than trusted:

* **plan entries** — `A` decisions, `B` blockers, `D` disproved claims, `F` features, `L` layer
  entries, `M` review findings, `Q` open questions, `S` sessions — defined in `ajith_final_plan.md`;
* **standing rules** — `R.NN`, optionally with a sub-clause letter (`R.23b`) — also defined in the
  plan, where the sub-clause is prose inside the rule rather than its own heading, so `R.23b`
  resolves to `R.23`;
* **opinions** — `O.NN` — defined in `docs/CLAUDE_OPINIONS.md`, deliberately a separate file because
  `R.25` keeps measured facts, operator decisions and my judgement in three different homes.

Deliberately NOT an engine (`R.23b`): this parses two markdown files into a set of strings. It has
no
solver, carries no state between decisions and changes no allocation. It is named for what it is — a
catalogue reader — and the thing built on top of it is named a check, not a gate-engine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PLAN_PATH = REPOSITORY_ROOT / "docs" / "ajith_final_plan.md"
DEFAULT_TODO_PATH = REPOSITORY_ROOT / "docs" / "ajith_final_todo.md"
DEFAULT_OPINIONS_PATH = REPOSITORY_ROOT / "docs" / "CLAUDE_OPINIONS.md"

PLAN_ENTRY_IDENTIFIER_PATTERN = re.compile(r"\b([A-Z][0-9]*)\.([0-9]+)([a-z]?)\b")
"""One plan id: a letter, an optional layer number, a dot, an entry number, an optional sub-letter.

Matches `L5.29`, `A.130`, `R.23b`, `L0.05a`. The letter-plus-optional-digits prefix is what lets
`L5` and `L13` and a bare `A` share one pattern. """

_PLAN_DEFINITION_PATTERN = re.compile(
    r"^\*\*([A-Z][0-9]*\.[0-9]+[a-z]?)(?:\*\*|\s|\b)", re.MULTILINE
)
"""How the plan marks an entry as DEFINED here: bolded, at the start of a line.

An id merely mentioned mid-sentence is a reference, not a definition — which is the distinction that
makes "cited but never catalogued" detectable at all. """

_OPINION_DEFINITION_PATTERN = re.compile(r"^##\s+(O\.[0-9]+)\b", re.MULTILINE)
"""Opinions are `## O.NN · date · title` headings."""

OPINION_NAMESPACE = "O"
RULE_NAMESPACE = "R"


class PlanCatalogueError(Exception):
    """A governing document could not be read, so no citation can be checked against it."""


@dataclass(frozen=True)
class CitedPlanEntry:
    """One citation of a plan id, and where it was found."""

    identifier: str
    file_path: Path
    line_number: int
    line_text: str

    @property
    def resolvable_identifier(self) -> str:
        """The id this citation must be found under in a governing document.

        A rule sub-clause resolves to its parent rule: `R.23b` is prose inside `R.23`, not a
        separate heading, so demanding a `R.23b` definition would reject a correct citation."""
        namespace, _, remainder = self.identifier.partition(".")
        if namespace == RULE_NAMESPACE and remainder and remainder[-1].isalpha():
            return f"{namespace}.{remainder[:-1]}"
        return self.identifier


@dataclass(frozen=True)
class PlanEntryCatalogue:
    """Every id the governing documents actually define."""

    plan_path: Path = DEFAULT_PLAN_PATH
    opinions_path: Path = DEFAULT_OPINIONS_PATH

    @cached_property
    def defined_plan_identifiers(self) -> frozenset[str]:
        return frozenset(_PLAN_DEFINITION_PATTERN.findall(self._read(self.plan_path)))

    @cached_property
    def defined_opinion_identifiers(self) -> frozenset[str]:
        return frozenset(_OPINION_DEFINITION_PATTERN.findall(self._read(self.opinions_path)))

    @cached_property
    def all_defined_identifiers(self) -> frozenset[str]:
        return self.defined_plan_identifiers | self.defined_opinion_identifiers

    def defines(self, identifier: str) -> bool:
        return identifier in self.all_defined_identifiers

    def namespace_home(self, identifier: str) -> Path:
        """Which document SHOULD define this id — used to say where a bad citation belongs."""
        namespace = identifier.partition(".")[0]
        if namespace == OPINION_NAMESPACE:
            return self.opinions_path
        return self.plan_path

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as failure:
            raise PlanCatalogueError(
                f"cannot read the governing document {path}: {failure}. Every citation check is "
                f"vacuous without it, so this is an error rather than an empty catalogue — a check "
                f"that silently passes because it read nothing is worse than no check."
            ) from failure


def find_cited_plan_entries(source_text: str, file_path: Path) -> list[CitedPlanEntry]:
    """Every backticked plan id in one file, with its line.

    Only backticked ids count. Unquoted capital-letter-dot-number sequences appear constantly in
    ordinary prose and code — version numbers, `Decimal.0`, section numbering — and treating those
    as
    citations would bury the real ones in false positives."""
    citations: list[CitedPlanEntry] = []
    for line_number, line_text in enumerate(source_text.splitlines(), start=1):
        for quoted in re.findall(r"`([^`]{1,24})`", line_text):
            match = PLAN_ENTRY_IDENTIFIER_PATTERN.fullmatch(quoted.strip())
            if match is None:
                continue
            citations.append(
                CitedPlanEntry(
                    identifier=quoted.strip(),
                    file_path=file_path,
                    line_number=line_number,
                    line_text=line_text.strip(),
                )
            )
    return citations
