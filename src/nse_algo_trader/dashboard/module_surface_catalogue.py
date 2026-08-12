"""`L13.04` — every module's build status, DERIVED from the real code.

`R.08` demands status measured from real code, never hand-authored, and `L13.04` says the
same thing in its own words: *machine-derived build status, never hand-typed*. Those two
sentences rule out the obvious way to surface 44 modules, which is to write 44 panels.
Forty-four hand-written panels would be forty-four claims that start true and rot — and
the rot is invisible, because a stale panel looks exactly like a fresh one.

So nothing here is typed. Every fact is read out of the tree:

- **Reachability** comes from a real `ast` import graph, breadth-first from the runnable
  entry points. A module nothing reaches is an ORPHAN — `R.06`'s violation, detected
  rather than remembered.
- **Test pairing** comes from scanning the test tree for modules actually imported there,
  so a module with no test says so itself.
- **Real-data coverage** comes from finding `@pytest.mark.real_data` in the tests that
  import a module — `R.05`'s status, measured.
- **Tier** comes from the module's own vocabulary, which is `R.23(b)`'s rule applied
  mechanically: something named `engine`, `model`, `brain` or `gate` is decision-path and
  owes the full loop; a `store` or `adapter` owes less.

The point is not the panel. The point is that adding a module tomorrow changes this page
with no edit to it, and a module that loses its tests turns red without anyone noticing
in time to hide it.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

PACKAGE_ROOT_NAME = "nse_algo_trader"

ENTRY_POINT_DIRECTORIES = ("scripts",)
"""Runnable things. Reachability is measured FROM these, because "is this code actually
used" means "does anything a human can run eventually import it"."""

DECISION_PATH_WORDS = frozenset(
    {"engine", "model", "brain", "gate", "optimizer", "reasoner", "classifier", "router"}
)
"""`R.23(b)`: vocabulary sets scope. A module calling itself an engine has claimed the
full loop, and this is that claim read back mechanically rather than taken on trust."""

STORE_WORDS = frozenset({"store", "tape", "ledger", "catalogue", "manifest", "registry"})
ADAPTER_WORDS = frozenset({"adapter", "fetcher", "loader", "renderer", "seam", "client"})


class ModuleTier(Enum):
    """What a module owes, derived from what it calls itself."""

    DECISION_PATH = "decision-path"
    STORE_OR_PIPELINE = "store/pipeline"
    ADAPTER_OR_GLUE = "adapter/glue"
    SUPPORT = "support"

    @property
    def owes_real_data_pass(self) -> bool:
        """Everything that touches real inputs owes `R.05`; pure glue does not."""
        return self is not ModuleTier.SUPPORT


class SurfaceHealth(Enum):
    """The one-word verdict a wall needs. Ordered worst-first for sorting."""

    ORPHAN = "orphan"
    UNTESTED = "untested"
    NO_REAL_DATA = "no real-data pass"
    HEALTHY = "healthy"


@dataclass(frozen=True)
class ModuleSurface:
    """One module's measured state. Every field read from the tree, none declared."""

    module_name: str
    relative_path: str
    source_lines: int
    tier: ModuleTier
    is_reachable: bool
    importing_tests: tuple[str, ...]
    has_real_data_test: bool
    has_dedicated_panel: bool

    @property
    def test_count(self) -> int:
        return len(self.importing_tests)

    @property
    def health(self) -> SurfaceHealth:
        """Worst finding wins. An orphan's test count is not the interesting fact."""
        if not self.is_reachable:
            return SurfaceHealth.ORPHAN
        if not self.importing_tests:
            return SurfaceHealth.UNTESTED
        if self.tier.owes_real_data_pass and not self.has_real_data_test:
            return SurfaceHealth.NO_REAL_DATA
        return SurfaceHealth.HEALTHY

    @property
    def short_name(self) -> str:
        return self.module_name.removeprefix(f"{PACKAGE_ROOT_NAME}.")


@dataclass
class CatalogueSummary:
    """Counts the wall shows, all derived."""

    surfaces: tuple[ModuleSurface, ...] = field(default_factory=tuple)

    def count_of(self, health: SurfaceHealth) -> int:
        return sum(1 for surface in self.surfaces if surface.health is health)

    @property
    def total(self) -> int:
        return len(self.surfaces)

    @property
    def healthy_fraction(self) -> float:
        return self.count_of(SurfaceHealth.HEALTHY) / self.total if self.total else 0.0

    def by_tier(self, tier: ModuleTier) -> tuple[ModuleSurface, ...]:
        return tuple(surface for surface in self.surfaces if surface.tier is tier)


def _module_name_for(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imported_modules(source: str) -> set[str]:
    """Project modules a file imports, from its real AST — never a regex on text."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(
                alias.name for alias in node.names if alias.name.startswith(PACKAGE_ROOT_NAME)
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(PACKAGE_ROOT_NAME)
        ):
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _defines_asgi_application(source: str) -> bool:
    """Whether a module assigns a module-level `app`, i.e. something uvicorn can serve."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
        for node in tree.body
    )


def _has_testable_content(source: str) -> bool:
    """Whether a module contains anything a test could exercise.

    An EMPTY package marker is not an untested module; it is punctuation. Reporting five
    zero-byte `__init__.py` files as UNTESTED inflated the wall's headline count with rows
    no work could ever clear — the opposite of `R.08`, since a metric that cannot reach
    zero stops being read.

    The distinction is content, never filename. `broker_credentials/__init__.py` carries
    fifteen lines of real re-export code and DOES owe tests, so a blanket "skip every
    `__init__`" rule would have hidden a genuine finding while fixing a cosmetic one.

    Docstrings and `from __future__` imports do not count: a module that only describes
    itself has no behaviour to assert on.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True  # Unparseable is a finding, not something to quietly drop.
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        return True
    return False


def _tier_for(module_name: str) -> ModuleTier:
    words = set(module_name.replace(".", "_").split("_"))
    if words & DECISION_PATH_WORDS:
        return ModuleTier.DECISION_PATH
    if words & STORE_WORDS:
        return ModuleTier.STORE_OR_PIPELINE
    if words & ADAPTER_WORDS:
        return ModuleTier.ADAPTER_OR_GLUE
    return ModuleTier.SUPPORT


def build_module_catalogue(
    repository_root: Path,
    surfaced_modules: Iterable[str] = (),
) -> CatalogueSummary:
    """Measure every module in the package. Raises if the tree is not where expected."""
    source_root = repository_root / "src"
    package_root = source_root / PACKAGE_ROOT_NAME
    if not package_root.exists():
        raise FileNotFoundError(f"no package at {package_root}")

    module_sources: dict[str, tuple[Path, str]] = {}
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module_sources[_module_name_for(path, source_root)] = (path, path.read_text())

    import_graph = {
        name: _imported_modules(source) for name, (_path, source) in module_sources.items()
    }

    # Reachability from what a human can actually run. Scripts are the entry points; the
    # package's own modules are only "used" if something runnable reaches them.
    frontier: deque[str] = deque()
    reachable: set[str] = set()
    for directory in ENTRY_POINT_DIRECTORIES:
        for script in sorted((repository_root / directory).glob("*.py")):
            frontier.extend(_imported_modules(script.read_text()))
    # A served ASGI application is an entry point too, and a narrower definition would
    # have reported the dashboard itself as an orphan — which was wrong about the code
    # rather than a finding about it. Detected from the AST, never declared.
    for name, (_path, source) in module_sources.items():
        if _defines_asgi_application(source):
            frontier.append(name)
    while frontier:
        current = frontier.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        # Importing a submodule executes its package, so the package is reachable too.
        # Without this, every `__init__` read as an ORPHAN — which was the catalogue
        # being wrong about Python rather than a finding about the code.
        parts = current.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            reachable.add(".".join(parts[:depth]))
        # An import may name a symbol rather than a module; fall back to its parent.
        for candidate in (current, current.rsplit(".", 1)[0]):
            for imported in import_graph.get(candidate, set()):
                if imported not in reachable:
                    frontier.append(imported)

    tests_by_module: dict[str, list[str]] = {name: [] for name in module_sources}
    real_data_modules: set[str] = set()
    tests_root = repository_root / "tests"
    if tests_root.exists():
        for test_path in sorted(tests_root.rglob("test_*.py")):
            source = test_path.read_text()
            imported_here = _imported_modules(source)
            marks_real_data = "real_data" in source
            for module_name in module_sources:
                if module_name not in imported_here:
                    continue
                tests_by_module[module_name].append(test_path.name)
                if marks_real_data:
                    real_data_modules.add(module_name)

    surfaced = set(surfaced_modules)
    surfaces = tuple(
        ModuleSurface(
            module_name=name,
            relative_path=str(path.relative_to(repository_root)),
            source_lines=source.count("\n") + 1,
            tier=_tier_for(name),
            is_reachable=name in reachable,
            importing_tests=tuple(tests_by_module[name]),
            has_real_data_test=name in real_data_modules,
            has_dedicated_panel=name in surfaced,
        )
        for name, (path, source) in sorted(module_sources.items())
        # `_module_name_for` already strips the `__init__` part, so the obvious
        # `name.endswith("__init__")` guard this replaces could NEVER fire — it read as an
        # exclusion for the whole life of the file while excluding nothing. Package
        # markers are identified by their real PATH, and dropped only when empty.
        if name != PACKAGE_ROOT_NAME
        and (path.name != "__init__.py" or _has_testable_content(source))
    )
    return CatalogueSummary(surfaces=surfaces)


def worst_first(surfaces: Sequence[ModuleSurface]) -> list[ModuleSurface]:
    """Problems at the top. A wall sorted alphabetically hides its own findings."""
    order = {
        SurfaceHealth.ORPHAN: 0,
        SurfaceHealth.UNTESTED: 1,
        SurfaceHealth.NO_REAL_DATA: 2,
        SurfaceHealth.HEALTHY: 3,
    }
    return sorted(surfaces, key=lambda surface: (order[surface.health], surface.module_name))
