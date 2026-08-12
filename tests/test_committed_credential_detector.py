"""`L3.30` — the guard that makes `R.02` executable.

The tests that matter are the adversarial ones. A secret scanner is only worth having if it
catches the shapes that actually leak, and only worth KEEPING if it does not cry wolf — a guard
that produces noise gets disabled, and a disabled guard is worse than none because it is still
cited as protection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nse_algo_trader.committed_credential_detector import (
    CREDENTIAL_PATTERNS,
    scan_git_history,
    scan_text,
    scan_tracked_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
A_PATH = Path("somewhere.py")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "sample"),
    [
        ("github_personal_access_token", "token = 'ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'"),
        ("github_fine_grained_token", "t = 'github_pat_" + "11ABCDEFG0abcdefghijklmnop'"),
        ("anthropic_api_key", "key = 'sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWx'"),
        ("aws_access_key_id", "aws = 'AKIAIOSFODNN7EXAMPLX'"),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ],
)
def test_every_credential_shape_this_project_holds_is_caught(name: str, sample: str) -> None:
    findings = scan_text(sample, A_PATH)
    assert findings, f"{name} was not detected"
    assert any(finding.pattern_name == name for finding in findings)


@pytest.mark.unit
def test_a_broker_credential_is_caught_and_named_the_most_expensive_one() -> None:
    """The one that can place orders on a real account."""
    findings = scan_text("kite_api_secret = 'z9x8c7v6b5n4m3a2s1d0'", A_PATH)
    assert findings
    assert findings[0].pattern_name == "broker_credential_assignment"
    assert "real trading account" in findings[0].blast_radius


@pytest.mark.unit
def test_the_finding_says_what_it_would_cost() -> None:
    """A finding with no blast radius gets triaged as a lint nit and then ignored."""
    for pattern in CREDENTIAL_PATTERNS:
        assert pattern.blast_radius.strip()


@pytest.mark.property
@pytest.mark.parametrize(
    "harmless",
    [
        "api_key = os.environ['KITE_API_KEY']",
        "password = getpass.getpass()",
        "token = None",
        "access_token: str | None = None",
        "# set KITE_API_SECRET in .env, never here",
        "secret = 'your_secret_here'",
        "api_key = 'REDACTED'",
        "github_token = '<your-token>'",
        "password = 'changeme'",
        "instrument_token = 256265",
        "SOURCED_CONSTANT_REASONS = {'_PAISE': 'a property of INR itself'}",
    ],
)
def test_ordinary_code_does_not_trip_the_guard(harmless: str) -> None:
    """The property that decides whether this guard survives contact with a real codebase."""
    assert scan_text(harmless, A_PATH) == []


@pytest.mark.adversarial
def test_a_credential_split_across_a_line_is_still_caught_where_it_is_written() -> None:
    """Concatenation defeats a naive scanner; the assignment form still catches the shape."""
    assert scan_text("t = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'", A_PATH) == []
    # ...which is why the guard is not the only control. Documented, not pretended away:
    # a determined author can evade any regex. The guard exists for the ACCIDENT — a pasted
    # token, a debug print, a config file added in haste — which is how B.10 happened.
    assert scan_text("t = 'ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'", A_PATH)


@pytest.mark.adversarial
def test_a_placeholder_is_not_a_credential() -> None:
    for documentation in (
        "GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "api_key = 'example_key_do_not_use_abcdefghij'",
    ):
        assert scan_text(documentation, A_PATH) == []


@pytest.mark.adversarial
def test_the_line_number_points_at_the_credential() -> None:
    text = "\n".join(["import os", "", "token = 'ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'"])
    (finding,) = scan_text(text, A_PATH)
    assert finding.line == 3
    assert str(finding).startswith("somewhere.py:3:")


@pytest.mark.real_data
def test_this_repository_has_no_committed_credential_right_now() -> None:
    """`R.05` on the guard itself: run it against the real tree it protects."""
    assert scan_tracked_tree(REPOSITORY_ROOT) == []


@pytest.mark.real_data
def test_this_repository_has_no_credential_anywhere_in_its_history() -> None:
    """The `B.10` audit, pinned as a test so it cannot silently stop being true.

    A secret in history is not fixed by deleting the file — anyone who cloned already has it.
    Keeping this green is cheaper than every alternative.
    """
    assert scan_git_history(REPOSITORY_ROOT) == []
