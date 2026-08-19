"""The guard that stops the next consumer forgetting the availability filter — `B12`.

`price_bars` holds 1,246,985 five-minute bars and is what the decision path reads. Look-ahead safety
currently rests on each author remembering to write `availability_time <= as_of`; the filter hides
125,980 of those rows at a mid-session instant, so forgetting it means reading the afternoon while
deciding at noon.

This is a **test** rather than a Stop-hook script on purpose: it runs inside the existing execution
gate with no settings change, and the enforcement is identical. It is the same shape as the
credential guard (`R.02`) and the money-literal guard (`R.03`) — a rule that lives only in a
document holds until attention lapses.

Every exemption carries a reason and is asserted to still be needed, so an allowlist entry cannot
outlive the code that justified it (`R.11`).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SEARCHED_DIRECTORIES = ("src", "scripts")

FIVE_MINUTE_BAR_TABLE = "price_bars"

_TABLE_REFERENCE = re.compile(r"\bprice_bars\b", re.IGNORECASE)
_LOOKS_LIKE_READ = re.compile(r"\b(SELECT|FROM|JOIN)\b", re.IGNORECASE)
_IS_A_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE)
_AVAILABILITY_FILTER = re.compile(r"availability_time\s*<=", re.IGNORECASE)


@dataclass(frozen=True)
class UnfilteredReadExemption:
    """A read of `price_bars` without an availability filter that is genuinely correct.

    Not an ignore: the reason is recorded, and `test_every_exemption_is_still_needed` fails if the
    file stops containing an unfiltered read, so an entry cannot outlive its justification.
    """

    relative_path: str
    reason: str


EXEMPTIONS: tuple[UnfilteredReadExemption, ...] = (
    UnfilteredReadExemption(
        relative_path="src/nse_algo_trader/historical_bars/point_in_time_five_minute_bar_reader.py",
        reason=(
            "This IS the guarded reader, and its NULL-detection query MUST be unfiltered: a NULL "
            "`availability_time` fails `<=` under SQL three-valued logic, so a filtered query "
            "cannot see the row it exists to refuse. The check was unreachable dead code until it "
            "was given its own query."
        ),
    ),
    UnfilteredReadExemption(
        relative_path="src/nse_algo_trader/sizing/sizing_inputs_from_real_stores.py",
        reason=(
            "Its close-series read at :197 filters correctly. Its universe `EXISTS` subquery "
            "at :92 "
            "does not: it asks 'has any bar ever been recorded', which is true at 09:00 for a bar "
            "that will not exist until 15:25. Safe today because the sizer runs after the close, "
            "wrong in shape, and owed a move onto "
            "`PointInTimeFiveMinuteBarReader.instruments_with_bars`. Tracked in BACKLOG under B12."
        ),
    ),
    UnfilteredReadExemption(
        relative_path="src/nse_algo_trader/market_depth/bar_tape_join_verification_engine.py",
        reason=(
            "Compares a whole RECORDED session against a whole recorded tape to ask whether the "
            "two describe the same market. It is not deciding anything, and restricting it to "
            "what was knowable mid-session would verify a fraction of the day it is verifying."
        ),
    ),
    UnfilteredReadExemption(
        relative_path="src/nse_algo_trader/historical_bars/bar_price_basis_provenance.py",
        reason=(
            "Asks what a stored value MEANS — adjusted or unadjusted, and as of when — across all "
            "of history. A point-in-time cutoff would answer the question for part of the store "
            "and leave the rest unclassified, which is the opposite of a provenance survey."
        ),
    ),
    UnfilteredReadExemption(
        relative_path="src/nse_algo_trader/dashboard/regime_brain_read_model.py",
        reason=(
            "Renders a NOW-view: at the present instant every stored bar is by definition already "
            "available, so the filter would be a no-op. Correct today and a trap the moment this "
            "surface grows a historical mode — recorded here so that change trips this guard."
        ),
    ),
    UnfilteredReadExemption(
        relative_path="scripts/verify_paper_session_on_real_data.py",
        reason=(
            "Uses `EXISTS (SELECT 1 FROM price_bars ...)` to assemble the universe. It asks 'has "
            "any bar ever been recorded for this instrument on this date', which is a question "
            "about the RECORDING rather than about a decision instant. The shape is still wrong "
            "for a universe filter and is owed a move onto "
            "`PointInTimeFiveMinuteBarReader.instruments_with_bars`; recorded as debt, not denied."
        ),
    ),
)


def _python_files() -> list[Path]:
    found: list[Path] = []
    for directory in SEARCHED_DIRECTORIES:
        root = REPOSITORY_ROOT / directory
        found.extend(path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts)
    return found


def _uses_the_legacy_table_name(source: str) -> bool:
    """True when a file reads or writes the old singular `price_bar` name.

    The one-line rename migration in `bitemporal_bar_store` must name it — that is the whole point
    of a migration — so a line performing the rename is not a use.
    """
    return any(
        re.search(r"\b(FROM|INTO|TABLE|UPDATE)\s+price_bar\b", line)
        and "RENAME TO daily_reconciled_bar" not in line
        and "name IN" not in line
        for line in source.splitlines()
    )


def _sql_string_literals(source: str) -> list[str]:
    """Every string constant in the module, docstrings excluded.

    Working literal-by-literal rather than file-by-file is the fix for three real bypasses an
    adversarial review demonstrated against the first version of this guard (2026-08-17), each of
    which read all 1,246,985 rows unfiltered while the guard stayed green:

    * a **normal string** anywhere in the file containing the words "availability_time <=" cleared
      the whole file — a guard defeated by a comment describing the thing it guards against;
    * **adjacent string literals**, `"... FROM price_" "bars WHERE ..."`, which is what a
      line-wrapping formatter produces automatically. Python folds adjacent literals at parse time,
      so reading constants from the AST sees the joined string where a line-oriented scan does not;
    * a **comma join**, `FROM instrument_master m, price_bars b`, which no `FROM price_bars` or
      `JOIN price_bars` pattern matches. The table name is now matched as a bare token.

    **What this deliberately does not catch**, stated rather than left to be discovered: a name
    assembled at runtime, `"price_" + "bars"`. Catching that needs dataflow analysis, and a guard
    is here to stop the accident — a new consumer writing ordinary SQL without the cutoff in mind —
    not to defeat someone deliberately hiding from it. Anyone willing to concatenate the table name
    to evade a check has already decided to.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # a file that will not parse cannot be reasoned about
        return [source]
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for child in body:
            if (
                isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Constant)
                and isinstance(child.value.value, str)
            ):
                docstrings.add(id(child.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _reads_price_bars_without_a_filter(source: str) -> bool:
    """True when any single SQL literal names `price_bars` and carries no availability cutoff.

    Scoped to one literal at a time, so a filter written in a DIFFERENT query no longer vouches for
    this one.
    """
    return any(
        _TABLE_REFERENCE.search(literal)
        # A bare `"price_bars"` is a LABEL, not a query — several modules pass it in
        # `missing_inputs=(...)` to say which store was empty. Requiring a SQL keyword in the same
        # literal keeps those out while still catching a comma join, which has no FROM/JOIN
        # immediately before the table name but does contain FROM somewhere in the statement.
        and _LOOKS_LIKE_READ.search(literal)
        # A write has no decision instant to be point-in-time about. The sole writer,
        # `backfill_five_minute_bars.py`, INSERTs rows and UPDATEs their adjustment basis; demanding
        # an availability cutoff there would be demanding it filter what it is creating.
        and not _IS_A_WRITE.match(literal)
        and not _AVAILABILITY_FILTER.search(literal)
        for literal in _sql_string_literals(source)
    )


def test_no_unexempted_module_reads_five_minute_bars_without_an_availability_cutoff() -> None:
    """The guard itself. A new consumer either filters, or uses the reader, or fails here."""
    exempt = {exemption.relative_path for exemption in EXEMPTIONS}
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in _python_files()
        if _reads_price_bars_without_a_filter(path.read_text(encoding="utf-8"))
        and str(path.relative_to(REPOSITORY_ROOT)) not in exempt
    ]
    assert not offenders, (
        "these modules read `price_bars` with no `availability_time <=` cutoff anywhere in the "
        "file, which reads bars that were not yet knowable at the decision instant: "
        f"{offenders}. Use `PointInTimeFiveMinuteBarReader`, which has no method that can be "
        "called without an `as_of` — or, if the unfiltered read is genuinely correct, add an "
        "EXEMPTION here with the reason."
    )


def test_every_exemption_is_still_needed() -> None:
    """An allowlist entry that outlives its justification is how a guard quietly stops guarding."""
    stale: list[str] = []
    for exemption in EXEMPTIONS:
        path = REPOSITORY_ROOT / exemption.relative_path
        assert path.exists(), f"{exemption.relative_path} is exempted but does not exist"
        if not _reads_price_bars_without_a_filter(path.read_text(encoding="utf-8")):
            stale.append(exemption.relative_path)
    assert not stale, (
        f"these exemptions are no longer needed and should be deleted: {stale}. Keeping them "
        "means the next genuinely-unsafe read in one of these files passes unnoticed."
    )


def test_the_guard_searches_the_directories_it_claims_to() -> None:
    """A mutant that dropped "scripts" from the search list survived the suite."""
    assert set(SEARCHED_DIRECTORIES) >= {"src", "scripts"}


def test_every_exemption_states_a_real_reason() -> None:
    """`R.11`: a deferral nobody can read is a silent skip."""
    for exemption in EXEMPTIONS:
        assert len(exemption.reason) > 80, f"{exemption.relative_path} is exempted without a reason"


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """A guard that cannot fail proves nothing — the lesson from the 2026-08-17 review."""
    offending = "rows = connection.execute('SELECT close_price FROM price_bars WHERE x = ?')"
    assert _reads_price_bars_without_a_filter(offending)

    filtered = (
        "rows = connection.execute('SELECT close_price FROM price_bars "
        "WHERE availability_time <= ?')"
    )
    assert not _reads_price_bars_without_a_filter(filtered)


def test_a_file_that_never_touches_the_table_is_not_flagged() -> None:
    """A guard that fires on innocent files trains its reader to skim past it."""
    assert not _reads_price_bars_without_a_filter("x = 1  # nothing to do with bars")
    assert not _reads_price_bars_without_a_filter('"price_bars" in missing_inputs')


def test_the_legacy_singular_table_name_is_gone() -> None:
    """`R.14`: `price_bar` and `price_bars` differed by one character and held different data.

    That single character produced two wrong findings in one session (`O.115`). The daily
    cross-broker set now lives in `daily_reconciled_bar`, and nothing may reintroduce the old name.
    """
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in _python_files()
        if _uses_the_legacy_table_name(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"the legacy singular table name is back in {offenders}"
