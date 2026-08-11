"""Fail the build on a hardcoded money constant. Enforces R.03 mechanically.

**Why this exists as a runnable entry point.** `rupee_literal_detector` was built
to enforce R.03 and then imported by nothing but its own test — it had never run
over the codebase. Meanwhile a hardcoded `0.005` threshold went into a scan and
produced 89,597 false corporate-action "events" (`O.27`). A guard nobody invokes
is not a guard; it is a test fixture that looks like one.

Covers `src/` **and** `scripts/`: "it is only a script" is exactly how the rule got
bypassed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nse_algo_trader.rupee_literal_detector import RupeeLiteral, scan_source_tree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GUARDED_TREES = ("src", "scripts")


def main() -> int:
    findings: dict[Path, list[RupeeLiteral]] = {}
    for tree in GUARDED_TREES:
        root = PROJECT_ROOT / tree
        if root.exists():
            findings.update(scan_source_tree(root))

    if not findings:
        guarded = ", ".join(GUARDED_TREES)
        print(f"no hardcoded money literals under {guarded}")
        return 0

    for path, literals in sorted(findings.items()):
        for literal in literals:
            print(f"{path}: {literal}")
    total = sum(len(items) for items in findings.values())
    print(f"\n{total} hardcoded money literal(s) — R.03 forbids these; derive them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
