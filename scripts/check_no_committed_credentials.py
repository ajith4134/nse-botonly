"""Fail the gate when a credential is committed. The executable half of `R.02`.

Mirrors `check_no_hardcoded_money.py`: a small script the Stop hook runs every turn, exiting
non-zero on a finding so the turn is blocked rather than the finding being reported to nobody.

Run manually:

    .venv/bin/python scripts/check_no_committed_credentials.py
    .venv/bin/python scripts/check_no_committed_credentials.py --include-history
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_algo_trader.committed_credential_detector import (
    scan_git_history,
    scan_tracked_tree,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-history",
        action="store_true",
        help=(
            "also scan every commit. Slower, and the remedy for a finding is far worse: "
            "history rewrite AND revocation, because anyone who cloned already has it."
        ),
    )
    arguments = parser.parse_args()

    findings = scan_tracked_tree(REPOSITORY_ROOT)
    for finding in findings:
        print(f"{REPOSITORY_ROOT / finding.path}: {finding}", file=sys.stderr)

    history_hits: list[str] = []
    if arguments.include_history:
        history_hits = scan_git_history(REPOSITORY_ROOT)
        for name in sorted(set(history_hits)):
            print(
                f"IN GIT HISTORY: {name} — revoke the credential FIRST; rewriting history does "
                f"not recall what was already cloned",
                file=sys.stderr,
            )

    if findings or history_hits:
        print(
            f"\n{len(findings)} committed credential(s)"
            + (f" and {len(set(history_hits))} in history" if history_hits else "")
            + " — R.02 forbids these. Move the value to .env (gitignored) and revoke it.",
            file=sys.stderr,
        )
        return 1

    scanned = "tracked tree" + (" and full history" if arguments.include_history else "")
    print(f"no committed credentials under {scanned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
