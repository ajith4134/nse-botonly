"""The daily operations runner — the guarding, not the network.

The behaviour worth pinning is the part that decides whether a morning run is useful: one
blocked NSE endpoint must not stop the bar store from being reported, and a failure must
reach the exit code rather than being swallowed into a cheerful summary.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_daily_operations.py"


def _load_runner() -> ModuleType:
    """Load the script as a module.

    Registered in `sys.modules` BEFORE execution because `@dataclass` resolves its
    annotations through `sys.modules[cls.__module__]`, which does not exist yet for a
    module loaded straight from a path.
    """
    spec = importlib.util.spec_from_file_location("run_daily_operations", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


@pytest.mark.unit
def test_a_successful_step_records_what_it_did(runner: ModuleType) -> None:
    report = runner.DailyRunReport(started_at=datetime.now(UTC))
    runner._run_step(report, "ingest", lambda: "1,234 rows")
    assert report.outcomes[0].succeeded
    assert report.outcomes[0].detail == "1,234 rows"
    assert not report.failures


@pytest.mark.adversarial
def test_one_failing_step_does_not_stop_the_others(runner: ModuleType) -> None:
    """A blocked endpoint must not cost the morning its bar-store report."""
    report = runner.DailyRunReport(started_at=datetime.now(UTC))

    def explode() -> str:
        raise ConnectionError("NSE bot-blocked")

    runner._run_step(report, "ingest", explode)
    runner._run_step(report, "bar store", lambda: "659,990 bars")

    assert len(report.outcomes) == 2
    assert not report.outcomes[0].succeeded
    assert report.outcomes[1].succeeded
    assert report.outcomes[1].detail == "659,990 bars"


@pytest.mark.adversarial
def test_a_failure_names_its_cause_not_just_its_type(runner: ModuleType) -> None:
    """A bare exception type rarely says enough to act on the next morning."""
    report = runner.DailyRunReport(started_at=datetime.now(UTC))

    def explode() -> str:
        raise ValueError("expiry list was empty")

    runner._run_step(report, "ingest", explode)
    failure = report.outcomes[0].failure or ""
    assert "ValueError" in failure
    assert "expiry list was empty" in failure


@pytest.mark.unit
def test_the_report_states_how_many_steps_survived(runner: ModuleType) -> None:
    report = runner.DailyRunReport(started_at=datetime.now(UTC))
    runner._run_step(report, "a", lambda: "ok")
    runner._run_step(report, "b", lambda: (_ for _ in ()).throw(RuntimeError("no")))
    described = report.describe()
    assert "1/2 steps succeeded" in described
    assert "FAIL" in described
    assert "[ok  ] a" in described


@pytest.mark.unit
def test_backfill_is_bounded_so_one_run_cannot_become_a_crawl(runner: ModuleType) -> None:
    """A source years behind must not turn a nightly run into an unbounded scrape of a
    host that bot-blocks."""
    assert runner.MAXIMUM_BACKFILL_DATES_PER_RUN > 0
    assert runner.MAXIMUM_BACKFILL_DATES_PER_RUN <= 50


@pytest.mark.unit
def test_escalation_follows_the_three_strikes_rule(runner: ModuleType) -> None:
    """`R.21`: grinding silently on a date that will not fix itself is the failure."""
    assert runner.PERSISTENT_FAILURE_ATTEMPTS == 3
