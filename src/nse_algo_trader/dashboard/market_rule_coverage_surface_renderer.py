"""`L0.31` surface — which eras this project can price, and which it must refuse.

Built against the dataviz procedure. **The form here is deliberately not a chart.** Sixteen
rule families, each either covered or not, each with an earliest date and an evidence mix,
is a *lookup* — the reader's question is "can I replay 2019?", answered by finding one row.
Plotting sixteen categories as bars would make that question harder, not easier, so the
page is three stat tiles (the headline counts) over one worst-first table.

**Colour does one job: status.** Covered / partly covered / uncovered are states, not
series, so they use the reserved status palette and never the categorical set. Each badge
carries a WORD as well as a colour, because a status shown in colour alone fails for a
colourblind reader and in a printout. The evidence grades are ordinal (observed > primary >
secondary > unverified) and appear as text counts rather than a ramp — four small integers
per row read faster as numbers than as any encoding of them.

**Worst first.** Uncovered families sort to the top. A page sorted alphabetically hides its
own findings behind the alphabet, which is the failure the operations wall already names.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    FamilyCoverage,
    RuleFamily,
)

_STATUS_UNCOVERED = "#c0392b"
_STATUS_PARTIAL = "#fab219"
_STATUS_COVERED = "#0ca30c"
"""The reserved status palette, never borrowed for a series. Each is paired with a word."""

_PAGE_CSS = """
:root{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  }
}
[data-theme="dark"]{
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
}
*{box-sizing:border-box;}
body{margin:0;padding:32px;background:var(--surface-0);color:var(--text-primary);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;}
h1{font-size:20px;margin:0 0 4px;}
.sub{color:var(--text-secondary);margin:0 0 24px;max-width:80ch;}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:26px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:160px;flex:1 1 160px;}
.tile-value{font-size:24px;font-weight:700;}
.tile-label{color:var(--text-secondary);margin-top:2px;}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:4px 18px 18px;overflow-x:auto;}
table{border-collapse:collapse;width:100%;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap;}
th{color:var(--text-secondary);font-weight:600;}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
  color:#ffffff;}
.badge-uncovered{background:__UNCOVERED__;}
.badge-partial{background:__PARTIAL__;color:#0b0b0b;}
.badge-covered{background:__COVERED__;}
.muted{color:var(--text-muted);}
footer{color:var(--text-muted);font-size:12px;margin-top:22px;max-width:80ch;}
"""
# Substituted rather than %-formatted: a CSS stylesheet is full of `%` units, and
# %-formatting a stylesheet is how `width:100%` becomes a format-string error.
_PAGE_CSS = (
    _PAGE_CSS.replace("__UNCOVERED__", _STATUS_UNCOVERED)
    .replace("__PARTIAL__", _STATUS_PARTIAL)
    .replace("__COVERED__", _STATUS_COVERED)
)

_GRADE_COLUMNS: tuple[tuple[EvidenceGrade, str], ...] = (
    (EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA, "observed"),
    (EvidenceGrade.PRIMARY_CIRCULAR, "primary"),
    (EvidenceGrade.SECONDARY_TRIANGULATED, "secondary"),
    (EvidenceGrade.UNVERIFIED_SNIPPET, "unverified"),
)


def _status(coverage: FamilyCoverage) -> tuple[str, str, int]:
    """Badge class, word, and a sort rank with the worst first."""
    if coverage.record_count == 0 and coverage.observed_window is None:
        return "badge-uncovered", "uncovered", 0
    if coverage.record_count == 0:
        return "badge-partial", "observed only", 1
    if coverage.holes:
        return "badge-partial", "holes", 1
    return "badge-covered", "covered", 2


def _grade_cell(coverage: FamilyCoverage, grade: EvidenceGrade) -> str:
    """The count, or — for a lazy observational source — the terms the facts exist on."""
    if (
        grade is EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA
        and coverage.observed_window is not None
        and not coverage.grade_counts.get(grade, 0)
    ):
        return "<span class=muted>on demand</span>"
    return str(coverage.grade_counts.get(grade, 0) or "<span class=muted>·</span>")


def _row(coverage: FamilyCoverage) -> str:
    badge_class, word, _ = _status(coverage)
    earliest = coverage.earliest.isoformat() if coverage.earliest else "—"
    latest = (
        "in force"
        if coverage.is_open_ended
        else (coverage.latest.isoformat() if coverage.latest else "—")
    )
    # A family answered by observation has no holes to list; what a reader needs there is
    # WHICH source is answering, so the column carries that instead of a dash.
    holes_note = (
        coverage.observed_by
        if coverage.observed_window is not None
        else (
            ", ".join(
                f"{start.isoformat()}→{end.isoformat()}" for start, end in coverage.holes
            )
            or "—"
        )
    )
    # A lazy source materialises nothing until a symbol is asked for, so its grade count is
    # legitimately zero — but a blank observed cell next to an "observed only" badge reads
    # as a contradiction. The cell says on what terms the facts exist instead of counting
    # objects that deliberately do not.
    grade_cells = "".join(
        f"<td>{_grade_cell(coverage, grade)}</td>" for grade, _ in _GRADE_COLUMNS
    )
    return (
        f"<tr><td>{escape(coverage.family.value)}</td>"
        f'<td><span class="badge {badge_class}">{word}</span></td>'
        f"<td>{escape(earliest)}</td><td>{escape(latest)}</td>"
        f"<td>{coverage.record_count}</td>{grade_cells}"
        f"<td>{escape(holes_note)}</td></tr>"
    )


def render_market_rule_coverage_page(
    coverage_by_family: Mapping[RuleFamily, FamilyCoverage],
) -> str:
    """The whole surface, self-contained. Every number is read off the real store."""
    ordered = sorted(coverage_by_family.values(), key=lambda c: (_status(c)[2], c.family.value))
    uncovered = sum(1 for c in ordered if not c.has_any_source)
    with_holes = sum(1 for c in ordered if c.holes)
    earliest_dates = [c.earliest for c in ordered if c.earliest is not None]
    grade_headers = "".join(f"<th>{label}</th>" for _, label in _GRADE_COLUMNS)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Point-in-time market rules</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Point-in-time market rules</h1>
<p class="sub">What the exchange's rules WERE, per era. A family shown as
<span class="badge badge-uncovered">uncovered</span> is not a gap in this page — it is the
store refusing to answer, which is the whole point: applying today's STT rate or today's
expiry weekday to an old replay returns a plausible number and a wrong conclusion.</p>

<div class="tiles">
<div class="tile"><div class="tile-value">{len(ordered) - uncovered}/{len(ordered)}</div>
<div class="tile-label">families with any facts</div></div>
<div class="tile"><div class="tile-value">{uncovered}</div>
<div class="tile-label">uncovered — every query refuses</div></div>
<div class="tile"><div class="tile-value">{with_holes}</div>
<div class="tile-label">covered but with holes</div></div>
<div class="tile"><div class="tile-value">
{min(earliest_dates).isoformat() if earliest_dates else "—"}</div>
<div class="tile-label">earliest date anything is known</div></div>
</div>

<div class="panel">
<table><thead><tr><th>Family</th><th>State</th><th>Earliest</th><th>Latest</th>
<th>Facts</th>{grade_headers}<th>Holes / source</th></tr></thead>
<tbody>{"".join(_row(coverage) for coverage in ordered)}</tbody></table>
</div>

<footer>Worst first, deliberately. Grades are ordinal: an observed exchange fact outranks a
circular, because the circular says what was announced and the instrument master says what
was in force. Sourced from docs/research/61; nothing on this page is hand-typed — the
counts come from the store's own coverage map.</footer>
</body></html>"""
