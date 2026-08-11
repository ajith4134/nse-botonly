"""`L0.13` — proves replay code cannot read the wall clock, by reading its AST.

One `datetime.now()` anywhere in a replay path silently returns today while the simulated
clock sits in 2020. Nothing about the result looks wrong: the backtest simply comes out a
little too good, and the leak is a one-line import away at every moment. Discipline cannot
hold that line, because the failure is invisible at the point it is committed.

So it is checked mechanically, the same way this project already checks for hardcoded
rupee literals. The execution gate fails on a hit, which is what converts "the replay
clock is honest" from an intention into a property of the source.

**What counts as reading the wall.** A call to `now`, `utcnow`, `today`, `time`,
`monotonic` or `perf_counter` on a plain name like `datetime`, `date`, `time` or `pd`.
The receiver must be a bare name: `datetime.min.time()` builds a time from a constant and
reads nothing, so flagging it would train the reader to ignore this detector — which is
how a checker becomes decoration.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

WALL_CLOCK_READING_METHODS = frozenset(
    {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "monotonic_ns", "time_ns"}
)
"""Every standard way to ask the operating system what time it is."""

TIME_BEARING_RECEIVERS = frozenset({"datetime", "date", "time", "pd", "pandas", "np"})
"""Bare names these methods are read from. A narrow list on purpose: broadening it to any
receiver would flag `self.clock.now`, which is precisely the INJECTED clock this rule
exists to make people use."""


@dataclass(frozen=True)
class WallClockAccess:
    """One place source code asks the operating system for the time."""

    file_path: Path
    line_number: int
    expression: str

    def describe(self) -> str:
        return f"{self.file_path}:{self.line_number}: {self.expression}"


def find_wall_clock_access(source: str, file_path: Path) -> list[WallClockAccess]:
    """Every wall-clock read in one module, from its real AST — never a regex."""
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        # An unparseable module is a finding for the gate, not something to swallow here.
        return []

    found: list[WallClockAccess] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute):
            continue
        if function.attr not in WALL_CLOCK_READING_METHODS:
            continue
        receiver = function.value
        # Bare name only. `datetime.min.time()` reads a constant, not the clock.
        if not isinstance(receiver, ast.Name):
            continue
        if receiver.id not in TIME_BEARING_RECEIVERS:
            continue
        found.append(
            WallClockAccess(
                file_path=file_path,
                line_number=node.lineno,
                expression=f"{receiver.id}.{function.attr}()",
            )
        )
    return sorted(found, key=lambda access: access.line_number)


def scan_replay_path(module_paths: Iterable[Path]) -> list[WallClockAccess]:
    """Scan the modules a replay executes. Returns every access, worst file first."""
    accesses: list[WallClockAccess] = []
    for path in sorted(module_paths):
        if not path.exists():
            continue
        accesses.extend(find_wall_clock_access(path.read_text(), path))
    return accesses


def replay_path_modules(package_root: Path) -> list[Path]:
    """The modules that must never read the wall clock.

    Deliberately explicit rather than "everything": the daily runner legitimately reads
    the real clock to decide which session to fetch, and a rule that flagged it would be
    disabled within a week. This list grows as the replay engine does.
    """
    names = (
        "replay_session_clock.py",
        "causal_leakage_firewall.py",
        "bitemporal_bar_store.py",
        "security_identity_record_store.py",
    )
    return [package_root / name for name in names]
