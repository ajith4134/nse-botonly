"""Mechanical enforcement that the work matches the governing documents.

`docs/ajith_final_plan.md` and `docs/ajith_final_todo.md` govern this project alone. This package
makes conformance to them checkable instead of remembered — the same treatment `R.02` and `R.03`
already get from `check_no_committed_credentials.py` and `check_no_hardcoded_money.py`.
"""

from nse_algo_trader.plan_conformance.plan_conformance_checks import (
    ConformanceFinding,
    ConformanceReport,
    run_all_plan_conformance_checks,
)
from nse_algo_trader.plan_conformance.plan_entry_catalogue import (
    PlanCatalogueError,
    PlanEntryCatalogue,
    find_cited_plan_entries,
)

__all__ = [
    "ConformanceFinding",
    "ConformanceReport",
    "PlanCatalogueError",
    "PlanEntryCatalogue",
    "find_cited_plan_entries",
    "run_all_plan_conformance_checks",
]
