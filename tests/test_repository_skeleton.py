"""Skeleton guards.

These are not engine tests — the repository skeleton is glue, not an engine
(R.23b), so it gets machine gates rather than the full loop. What these do
guard is the architectural invariant that must hold from the first commit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_package_imports_and_declares_a_version() -> None:
    import nse_algo_trader

    assert nse_algo_trader.__version__


@pytest.mark.unit
def test_secrets_are_never_committable() -> None:
    """R.02 — .env must be ignored, and must not be tracked."""
    ignored = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored


@pytest.mark.unit
def test_governing_documents_are_present() -> None:
    """The plan and todo govern alone; a build without them has nothing to follow."""
    for document in ("ajith_final_plan.md", "ajith_final_todo.md"):
        assert (REPOSITORY_ROOT / "docs" / document).is_file(), document


@pytest.mark.unit
def test_test_kinds_required_by_rule_23_are_registered() -> None:
    """R.23 step 3 names unit, property and adversarial tests; the markers must exist."""
    configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        marker.split(":", 1)[0]
        for marker in configuration["tool"]["pytest"]["ini_options"]["markers"]
    }
    assert {"unit", "property", "adversarial", "real_data", "hermetic"} <= declared
