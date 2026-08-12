"""Detects credentials committed into the tree — the executable half of `R.02`.

`R.02` says secrets live in the environment and are never committed. That was policy with
nothing enforcing it, and the project already has one credential incident on the books
(`B.10`): a GitHub personal access token, still live at the time of writing, carrying `repo`,
`admin:org`, `delete_repo`, `workflow` and `admin:enterprise` — enough to read every private
repository on the account and delete any of them.

The audit that followed found the working tree and all 231 commits clean, which is the good
outcome and also exactly the outcome that makes a guard feel unnecessary right up until it is
not. A leak is cheap to prevent and expensive to undo: a token in a commit is public to anyone
who ever clones the repository, and rewriting history does not recall it.

**Why a bespoke detector rather than `gitleaks` or `trufflehog`:** this is the shape of guard
that must run in the Stop hook on every turn, so it has to be import-fast, dependency-free and
tuned to the credentials THIS project actually holds — Kite, Angel One, Upstox, Fyers, Groww,
Breeze, GitHub, Anthropic. A general scanner's value is breadth; the value here is that a
finding is never noise, because a guard that cries wolf gets disabled and then guards nothing.

**What it deliberately does NOT do:** it does not scan `.env`, which is gitignored and is the
correct home for every one of these values. It scans what is TRACKED, because that is what
leaves the machine.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_MINIMUM_ENTROPY_LENGTH = 24
"""Below this a random-looking string is more likely an identifier than a secret.

Not a tuning knob for sensitivity: it is the shortest length at which the token formats below
actually occur. Every real pattern here is longer, so this only bounds the generic rule.
"""


@dataclass(frozen=True, slots=True)
class CredentialPattern:
    """One credential shape, with what it grants if it escapes."""

    name: str
    expression: re.Pattern[str]
    blast_radius: str


CREDENTIAL_PATTERNS: tuple[CredentialPattern, ...] = (
    CredentialPattern(
        name="github_personal_access_token",
        expression=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
        blast_radius=(
            "read and delete every repository the account can reach; with admin scopes, the "
            "whole account. This is the incident recorded as B.10"
        ),
    ),
    CredentialPattern(
        name="github_fine_grained_token",
        expression=re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        blast_radius="whatever repositories the token was scoped to, for its lifetime",
    ),
    CredentialPattern(
        name="anthropic_api_key",
        expression=re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
        blast_radius="billable API spend against the account until revoked",
    ),
    CredentialPattern(
        name="openai_style_api_key",
        expression=re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
        blast_radius="billable API spend against the account until revoked",
    ),
    CredentialPattern(
        name="aws_access_key_id",
        expression=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        blast_radius="whatever the IAM policy allows, which is rarely what anyone believes",
    ),
    CredentialPattern(
        name="private_key_block",
        expression=re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        blast_radius="impersonation of whatever the key authenticates",
    ),
    CredentialPattern(
        name="broker_credential_assignment",
        expression=re.compile(
            r"(?i)\b(?:kite|zerodha|angel|angelone|upstox|fyers|groww|breeze|icici)"
            r"[_a-z]*(?:api[_]?key|api[_]?secret|access[_]?token|totp[_]?secret|password|pin)"
            r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
        ),
        blast_radius=(
            "the ability to place orders on a real trading account. The most expensive "
            "credential this project holds"
        ),
    ),
    CredentialPattern(
        name="generic_secret_assignment",
        expression=re.compile(
            r"(?i)\b(?:secret|passwd|password|api[_]?key|access[_]?token|auth[_]?token|"
            r"private[_]?key)\s*[:=]\s*['\"][A-Za-z0-9+/=_\-]{"
            + str(_MINIMUM_ENTROPY_LENGTH)
            + r",}['\"]"
        ),
        blast_radius="unknown, which is the reason to treat it as the worst case",
    ),
)

_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "example",
    "placeholder",
    "your_",
    "xxxx",
    "dummy",
    "redacted",
    "fake",
    "changeme",
    "<",
)
"""Substrings that mark a value as documentation rather than a credential.

Kept small and literal. Every entry here is a hole in the guard, so each has to be a string no
real token would contain.
"""


@dataclass(frozen=True, slots=True)
class CommittedCredential:
    """One finding: a credential shape, where it is, and what it would cost."""

    path: Path
    line: int
    pattern_name: str
    blast_radius: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.pattern_name} — {self.blast_radius}"


def _is_placeholder(matched_text: str) -> bool:
    lowered = matched_text.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def scan_text(text: str, path: Path) -> list[CommittedCredential]:
    """Every credential shape in `text`, with the line it sits on."""
    findings: list[CommittedCredential] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in CREDENTIAL_PATTERNS:
            match = pattern.expression.search(line)
            if match is None or _is_placeholder(match.group(0)):
                continue
            findings.append(
                CommittedCredential(
                    path=path,
                    line=line_number,
                    pattern_name=pattern.name,
                    blast_radius=pattern.blast_radius,
                )
            )
    return findings


def tracked_files(repository_root: Path) -> tuple[Path, ...]:
    """What git actually tracks — the set that leaves this machine.

    Untracked files are not scanned on purpose: `.env` is untracked and gitignored, and it is
    the CORRECT place for every credential this project uses. A guard that flagged it would
    train its reader to ignore it.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repository_root), "ls-files", "-z"],  # noqa: S607 - git is on PATH
        capture_output=True,
        check=True,
    )
    names = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return tuple(repository_root / name for name in names if name)


def scan_tracked_tree(repository_root: Path) -> list[CommittedCredential]:
    """Scan every tracked file. Binary and unreadable files are skipped, not failed."""
    findings: list[CommittedCredential] = []
    for path in tracked_files(repository_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(text, path.relative_to(repository_root)))
    return findings


SELF_TEST_PATH = "tests/test_committed_credential_detector.py"
"""This detector's own test file, excluded from the HISTORY scan only.

Its samples are assembled at runtime today, so the tracked-tree scan reads it like any other
file and finds nothing. But commit `99b4bd2` — the one that introduced the guard — contains an
earlier version with literal samples in it, and history cannot be edited without a rewrite that
would be wildly disproportionate to a handful of deliberately-fake strings.

Excluding one named path is the honest trade. The alternative was to weaken the assertion to
"no credential except the ones we expect", which is the shape of exception that later absorbs a
real finding. This exclusion is narrow, greppable, and applies to the history scan alone.
"""


def scan_git_history(repository_root: Path, *, maximum_commits: int = 0) -> list[str]:
    """Credential shapes anywhere in the commit history, outside this detector's own tests.

    Separate from the tracked-tree scan because the remedy is different and far worse: a
    secret in history is not fixed by deleting the file, only by rewriting history AND
    revoking the credential — and revocation is the half that actually matters, because
    anyone who cloned already has it.

    `maximum_commits=0` means the entire history.
    """
    revision_arguments = ["--all"] if maximum_commits == 0 else ["--all", f"-{maximum_commits}"]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - git is on PATH
            "git",
            "-C",
            str(repository_root),
            "log",
            *revision_arguments,
            "-p",
            "--",
            ".",
            f":(exclude){SELF_TEST_PATH}",
        ],
        capture_output=True,
        check=True,
    )
    text = completed.stdout.decode("utf-8", errors="replace")
    hits: list[str] = []
    for pattern in CREDENTIAL_PATTERNS:
        for match in pattern.expression.finditer(text):
            if not _is_placeholder(match.group(0)):
                hits.append(pattern.name)
    return hits
