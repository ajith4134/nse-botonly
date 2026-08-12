"""The wall's catalogue — derived facts, and the tests that prove they are derived.

The whole value of this module is that nothing in it is typed. So the tests are written
to fail if a derived fact were ever replaced with a declared one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nse_algo_trader.dashboard.module_surface_catalogue import (
    CatalogueSummary,
    ModuleTier,
    SurfaceHealth,
    build_module_catalogue,
    worst_first,
)
from nse_algo_trader.dashboard.operations_wall_renderer import render_operations_wall

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalogue() -> CatalogueSummary:
    return build_module_catalogue(REPOSITORY_ROOT)


@pytest.mark.real_data
def test_the_catalogue_measures_the_real_tree(catalogue: CatalogueSummary) -> None:
    """`R.05` for an audit engine: its real data IS the repository."""
    names = {surface.module_name for surface in catalogue.surfaces}
    assert "nse_algo_trader.regime.soft_regime_weighting_brain" in names
    assert "nse_algo_trader.nse_ingest.bitemporal_ingest_store" in names
    assert catalogue.total > 40


@pytest.mark.unit
def test_tier_is_read_from_the_modules_own_vocabulary(catalogue: CatalogueSummary) -> None:
    """`R.23(b)`: a module calling itself an engine has claimed the full loop."""
    by_name = {surface.module_name: surface for surface in catalogue.surfaces}
    assert (
        by_name["nse_algo_trader.strategy.intraday_mean_reversion_engine"].tier
        is ModuleTier.DECISION_PATH
    )
    assert (
        by_name["nse_algo_trader.market_depth.market_depth_tape_store"].tier
        is ModuleTier.STORE_OR_PIPELINE
    )


@pytest.mark.unit
def test_reachability_comes_from_a_real_import_graph(tmp_path: Path) -> None:
    """Built as a miniature repository so the graph is unambiguous: `reached` is imported
    by a script, `stranded` is imported by nobody."""
    package = tmp_path / "src" / "nse_algo_trader"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "reached_engine.py").write_text("VALUE = 1\n")
    (package / "stranded_engine.py").write_text("VALUE = 2\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text(
        "from nse_algo_trader.reached_engine import VALUE\n"
    )
    (tmp_path / "tests").mkdir()

    catalogue = build_module_catalogue(tmp_path)
    states = {surface.short_name: surface for surface in catalogue.surfaces}
    assert states["reached_engine"].is_reachable
    assert not states["stranded_engine"].is_reachable
    assert states["stranded_engine"].health is SurfaceHealth.ORPHAN


@pytest.mark.unit
def test_an_asgi_app_counts_as_an_entry_point(tmp_path: Path) -> None:
    """A served application is reachable. A narrower rule reported the dashboard itself
    as an orphan, which was wrong about the code rather than a finding about it."""
    package = tmp_path / "src" / "nse_algo_trader"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "served_engine.py").write_text("VALUE = 1\n")
    (package / "web_server.py").write_text(
        "from nse_algo_trader.served_engine import VALUE\napp = object()\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()

    states = {surface.short_name: surface for surface in build_module_catalogue(tmp_path).surfaces}
    assert states["web_server"].is_reachable
    assert states["served_engine"].is_reachable


@pytest.mark.unit
def test_test_pairing_and_real_data_coverage_are_measured(tmp_path: Path) -> None:
    package = tmp_path / "src" / "nse_algo_trader"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "covered_engine.py").write_text("VALUE = 1\n")
    (package / "bare_engine.py").write_text("VALUE = 2\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text(
        "from nse_algo_trader.covered_engine import VALUE\n"
        "from nse_algo_trader.bare_engine import VALUE as OTHER\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_covered.py").write_text(
        "import pytest\nfrom nse_algo_trader.covered_engine import VALUE\n"
        "@pytest.mark.real_data\ndef test_x(): assert VALUE\n"
    )

    states = {surface.short_name: surface for surface in build_module_catalogue(tmp_path).surfaces}
    assert states["covered_engine"].test_count == 1
    assert states["covered_engine"].has_real_data_test
    assert states["covered_engine"].health is SurfaceHealth.HEALTHY
    assert states["bare_engine"].test_count == 0
    assert states["bare_engine"].health is SurfaceHealth.UNTESTED


@pytest.mark.unit
def test_worst_findings_sort_to_the_top(catalogue: CatalogueSummary) -> None:
    """A wall sorted alphabetically hides its own findings."""
    ordered = worst_first(catalogue.surfaces)
    healths = [surface.health for surface in ordered]
    ranking = {
        SurfaceHealth.ORPHAN: 0,
        SurfaceHealth.UNTESTED: 1,
        SurfaceHealth.NO_REAL_DATA: 2,
        SurfaceHealth.HEALTHY: 3,
    }
    assert healths == sorted(healths, key=lambda health: ranking[health])


@pytest.mark.unit
def test_the_wall_renders_every_module_with_its_state(catalogue: CatalogueSummary) -> None:
    page = render_operations_wall(catalogue)
    assert "<table" in page
    for health in SurfaceHealth:
        if catalogue.count_of(health):
            assert health.value in page
    assert page.count("<tr>") >= catalogue.total


@pytest.mark.unit
def test_the_wall_states_counts_that_match_the_catalogue(catalogue: CatalogueSummary) -> None:
    page = render_operations_wall(catalogue)
    assert f"{catalogue.total} modules" in page
    assert f"<b>{catalogue.count_of(SurfaceHealth.ORPHAN)}</b> orphaned" in page


@pytest.mark.adversarial
def test_a_missing_package_raises_rather_than_reporting_zero(tmp_path: Path) -> None:
    """Reporting an empty catalogue would read as a perfectly healthy repository."""
    with pytest.raises(FileNotFoundError):
        build_module_catalogue(tmp_path)


@pytest.mark.unit
def test_a_package_is_reachable_when_any_submodule_is(tmp_path: Path) -> None:
    """Importing a submodule executes its package, so the package is used too.

    Without this every `__init__` read as an ORPHAN — the catalogue being wrong about
    Python rather than a finding about the code, and it inflated the orphan count by 2.
    """
    package = tmp_path / "src" / "nse_algo_trader"
    nested = package / "regime"
    nested.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (nested / "__init__.py").write_text("")
    (nested / "deep_engine.py").write_text("VALUE = 1\n")
    # Real content, so the package keeps a row to assert reachability on: empty markers
    # are excluded from the catalogue as punctuation, which would remove the subject of
    # this test without changing the reachability rule it exists to prove.
    (nested / "__init__.py").write_text("PACKAGE_MARKER = 1\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text(
        "from nse_algo_trader.regime.deep_engine import VALUE\n"
    )
    (tmp_path / "tests").mkdir()

    states = {surface.short_name: surface for surface in build_module_catalogue(tmp_path).surfaces}
    assert states["regime.deep_engine"].is_reachable
    assert states["regime"].is_reachable, "the package holding a used module is used"


@pytest.mark.unit
def test_an_unused_package_is_still_an_orphan(tmp_path: Path) -> None:
    """The parent rule must not excuse a package nothing reaches at all."""
    package = tmp_path / "src" / "nse_algo_trader"
    unused = package / "abandoned"
    unused.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    # Real content for the same reason as above — an EMPTY unreachable marker is not a
    # hidden orphan, because there is no code in it to strand.
    (unused / "__init__.py").write_text("PACKAGE_MARKER = 1\n")
    (unused / "stranded.py").write_text("VALUE = 1\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()

    states = {surface.short_name: surface for surface in build_module_catalogue(tmp_path).surfaces}
    assert not states["abandoned"].is_reachable
    assert not states["abandoned.stranded"].is_reachable


def test_empty_package_marker_is_not_reported_as_an_untested_module(tmp_path: Path) -> None:
    """An empty `__init__.py` is punctuation, not a module owing tests.

    Regression for a filter that could never fire: `_module_name_for` strips the
    `__init__` part, so the `name.endswith("__init__")` guard excluded nothing and five
    zero-byte package markers padded the wall's UNTESTED count with rows no amount of
    work could ever clear.
    """
    package = tmp_path / "src" / "nse_algo_trader"
    (package / "quiet").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "quiet" / "__init__.py").write_text("")
    (package / "quiet" / "real_module.py").write_text("VALUE = 1\n")
    (tmp_path / "scripts").mkdir()

    names = {surface.module_name for surface in build_module_catalogue(tmp_path).surfaces}

    assert "nse_algo_trader.quiet" not in names, "empty package marker was counted"
    assert "nse_algo_trader.quiet.real_module" in names


def test_package_marker_with_real_code_still_owes_tests(tmp_path: Path) -> None:
    """The fix must not become a blanket skip — a re-exporting `__init__` is real code.

    `broker_credentials/__init__.py` carries fifteen live lines of re-exports, so dropping
    every `__init__` would have hidden a genuine finding while fixing a cosmetic one.
    """
    package = tmp_path / "src" / "nse_algo_trader"
    (package / "loud").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "loud" / "__init__.py").write_text(
        '"""Docstring alone would not count."""\n'
        "from __future__ import annotations\n"
        "EXPORTED = 42\n"
    )
    (tmp_path / "scripts").mkdir()

    names = {surface.module_name for surface in build_module_catalogue(tmp_path).surfaces}
    assert "nse_algo_trader.loud" in names


def test_docstring_only_package_marker_counts_as_empty(tmp_path: Path) -> None:
    """A module that only describes itself has no behaviour to assert on."""
    package = tmp_path / "src" / "nse_algo_trader"
    (package / "described").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "described" / "__init__.py").write_text(
        '"""Just a description."""\nfrom __future__ import annotations\n'
    )
    (tmp_path / "scripts").mkdir()

    names = {surface.module_name for surface in build_module_catalogue(tmp_path).surfaces}
    assert "nse_algo_trader.described" not in names


def test_empty_package_marker_never_becomes_an_orphan_either(tmp_path: Path) -> None:
    """Excluding empty markers must not be a way to hide unreachable code.

    The exclusion is narrow on purpose: it drops rows with NOTHING in them. A package
    marker holding real code that nothing reaches must still surface as an ORPHAN.
    """
    package = tmp_path / "src" / "nse_algo_trader"
    (package / "unreached").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "unreached" / "__init__.py").write_text("UNREACHED = 1\n")
    (tmp_path / "scripts").mkdir()

    surfaces = {s.module_name: s for s in build_module_catalogue(tmp_path).surfaces}
    assert surfaces["nse_algo_trader.unreached"].health is SurfaceHealth.ORPHAN
