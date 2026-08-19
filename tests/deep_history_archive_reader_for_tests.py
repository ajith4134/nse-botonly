"""Open the real deep-history archive, or SKIP when another process holds its lock.

**Why this exists.** DuckDB takes an EXCLUSIVE file lock on `deep_history.duckdb`, so two readers
cannot coexist — and this project runs the suite twice concurrently by construction: the `R.23`
execution gate runs `pytest` from the Stop hook while a run may already be in flight. Measured
2026-08-18: three `pytest` processes, and the second to reach the archive died with

    IO Error: Could not set lock on file ".../deep_history.duckdb":
    Conflicting lock is held in /usr/bin/python3.12 (PID 173155)

That is an ENVIRONMENT condition, not a defect in the cost engine, and reporting it as a red test
teaches the reader to ignore red tests. It is the same distinction the cost gate draws between
`VETO` and `UNPRICEABLE`, and the one the depth-tape surface test needed on the same night: an
absence of access is not a finding about the thing being accessed.

**What is NOT masked.** Only a lock held by a DIFFERENT live process is skipped, and the skip names
that process so a genuine leaked lock is visible rather than silent. A missing archive, a corrupt
file, or any other `IOException` still fails.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from duckdb import IOException

from nse_algo_trader.deep_history.deep_history_archive_loader import DeepHistoryArchiveLoader

_CONFLICTING_LOCK = re.compile(r"Conflicting lock is held.*?\(PID (\d+)\)", re.DOTALL)


@contextmanager
def deep_history_archive_or_skip(database_path: Path) -> Iterator[DeepHistoryArchiveLoader]:
    """Yield the real archive, or skip this test naming the process that holds it."""
    try:
        loader = DeepHistoryArchiveLoader(database_path=database_path)
    except IOException as unavailable:
        holder = _CONFLICTING_LOCK.search(str(unavailable))
        if holder is None:
            raise
        pytest.skip(
            f"the deep-history archive is locked by PID {holder.group(1)} — another pytest run, "
            f"most likely the R.23 Stop-hook gate. DuckDB's lock is exclusive, so this is an "
            f"absence of access rather than a finding about the archive."
        )
    with loader:
        yield loader
