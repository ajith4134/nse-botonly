#!/usr/bin/env python
"""Fail the build when the work has drifted from the governing documents.

Runs alongside `check_no_committed_credentials.py` (`R.02`) and `check_no_hardcoded_money.py`
(`R.03`) in the Stop hook. Those two make secrets and magic numbers impossible to leave behind; this
one makes plan drift impossible to leave behind.

    python scripts/check_work_conforms_to_plan.py

Exit 0 when the work matches the plan, 1 when it does not. Accepted drifts are printed on every run
whether or not anything failed, because `R.11` forbids a deferral that nobody sees.
"""

from __future__ import annotations

import sys

from nse_algo_trader.plan_conformance.plan_conformance_checks import (
    run_all_plan_conformance_checks,
)


def main() -> int:
    report = run_all_plan_conformance_checks()
    print(report.describe(), flush=True)
    return 0 if report.conforms else 1


if __name__ == "__main__":
    sys.exit(main())
