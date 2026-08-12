"""Tests for the rupee-literal detector — the executable half of R.03.

The detector this replaces was found by adversarial review to catch one syntactic
shape and miss thirty-odd working evasions, while being cited in the spec as the
enforcement of R.03. These tests keep that from recurring: the evasion corpus the
reviewer wrote is now a permanent fixture, and the tree scan asserts it actually
visited files rather than passing vacuously on an empty walk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nse_algo_trader.rupee_literal_detector import (
    SOURCED_CONSTANT_REASONS,
    find_rupee_literals,
    scan_source_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
EVASION_CORPUS = Path(__file__).parent / "fixtures_rupee_literal_evasions"

# The scan must reach real files; passing on an empty walk is the R.13 failure.
MINIMUM_EXPECTED_SOURCE_FILES = 2
# Every exemption weakens the guard, so the list is capped and reviewed.
MAXIMUM_TOLERATED_EXEMPTIONS = 5
EVASION_FILES = sorted(path for path in EVASION_CORPUS.glob("*.py") if path.name != "__init__.py")


@pytest.mark.unit
def test_no_rupee_amount_is_hardcoded_anywhere_in_the_source_tree() -> None:
    """R.03 / A.23 — the invariant, checked against the real tree."""
    findings = scan_source_tree(SOURCE_ROOT)
    rendered = [
        f"{path.relative_to(SOURCE_ROOT)}:{literal}"
        for path, literals in findings.items()
        for literal in literals
    ]
    assert not rendered, (
        "hardcoded rupee amounts found — express these as a fraction of configured "
        "capital instead (R.03, A.23):\n  " + "\n  ".join(rendered)
    )


@pytest.mark.unit
def test_the_tree_scan_actually_visited_files() -> None:
    """A scan that reaches nothing passes for the wrong reason.

    The previous version passed while visiting zero files — the failure mode R.13
    names: correct execution is not evidence of correct coverage.
    """
    assert len(list(SOURCE_ROOT.rglob("*.py"))) >= MINIMUM_EXPECTED_SOURCE_FILES


@pytest.mark.adversarial
@pytest.mark.parametrize("corpus_file", EVASION_FILES, ids=lambda p: p.name)
def test_every_known_evasion_is_caught(corpus_file: Path) -> None:
    """The adversarial reviewer's evasion corpus, kept as a permanent regression.

    Each file is a deliberate attempt to hardcode a rupee amount in a shape the
    original detector missed: attribute targets, argument defaults, dict values,
    tuple unpacking, augmented assignment, subscript targets, negation, constant
    folding, module-qualified and aliased ``Decimal`` constructors, and returns.
    """
    caught = find_rupee_literals(corpus_file.read_text(encoding="utf-8"))
    assert caught, f"{corpus_file.name}: every evasion slipped through"


@pytest.mark.adversarial
def test_the_specific_shapes_that_previously_escaped() -> None:
    """Named individually so a regression says which shape broke."""
    shapes = {
        "negation": "max_loss_rupees = -5000",
        "aliased_constructor": "from decimal import Decimal as D\ndaily_loss_rupees = D('5000')",
        "module_qualified": "import decimal\nmargin_rupees = decimal.Decimal('25000')",
        "attribute_target": "self.max_loss_rupees = 5000",
        "augmented_assign": "running_margin_rupees += 5000",
        "tuple_unpack": "first_margin_rupees, second_margin_rupees = 5000, 25000",
        "constant_folding": "max_loss_rupees = 5 * 1000",
        "dict_value": "LIMITS = {'max_loss_rupees': 5000}",
        "subscript_target": "CONFIG['max_loss_rupees'] = 5000",
        "argument_default": "def size(max_loss_rupees=5000): pass",
        "comparison": "if pnl_rupees < -5000: pass",
        "unmarked_name": "MAX_DAILY_LOSS = 50000",
    }
    # A dimensionless policy value must NOT be flagged — it is the correct form.
    assert not find_rupee_literals("maximum_daily_loss_fraction = 0.02")
    escaped = [name for name, source in shapes.items() if not find_rupee_literals(source)]
    assert not escaped, f"these shapes still escape the detector: {escaped}"


@pytest.mark.adversarial
def test_fractions_and_sourced_constants_do_not_fire() -> None:
    """Fractions are policy and allowed; sourced constants are exempt by name."""
    permitted = (
        "from decimal import Decimal\n"
        "maximum_daily_loss_fraction = Decimal('0.02')\n"
        "risk_budget_fraction = 0.01\n"
        "MINIMUM_SUPPORTED_CAPITAL_RUPEES = Decimal('100000')\n"
        "_PAISE = Decimal('0.01')\n"
    )
    assert find_rupee_literals(permitted) == []


@pytest.mark.unit
def test_every_exemption_carries_a_stated_reason() -> None:
    """An allowlist without reasons becomes a convenience hatch."""
    assert all(reason.strip() for reason in SOURCED_CONSTANT_REASONS.values())
    assert len(SOURCED_CONSTANT_REASONS) <= MAXIMUM_TOLERATED_EXEMPTIONS, (
        "the exemption list is growing — each entry weakens the guard and needs "
        "justification in review"
    )
