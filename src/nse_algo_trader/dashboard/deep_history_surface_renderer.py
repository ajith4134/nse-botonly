"""`L0.34` surface — how much history this project actually holds, and what it refused.

Built against the dataviz procedure. The reader's question is "can I ask about 2007?", which
is a coverage question, so the page is stat tiles over two tables: what is loaded per market,
and what was quarantined per reason. **A decades-long coverage story is a bar per year**, not
a line: the reader compares one year against another rather than following a trend, and rows
per year across 33 years spans three orders of magnitude, which a line chart flattens.

**Quarantine is shown as prominently as coverage.** A loader that reports only what it kept
is indistinguishable from one that dropped a third of the archive, and this one has already
had to refuse two-digit years, embedded header rows and impossible bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

_STATUS_GOOD = "#0ca30c"
_STATUS_WARN = "#fab219"
_STATUS_BAD = "#c0392b"
_SERIES_HUE = "#3d6fd6"

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
h2{font-size:15px;margin:26px 0 10px;color:var(--text-secondary);}
.sub{color:var(--text-secondary);margin:0 0 24px;max-width:80ch;}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:26px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:170px;flex:1 1 170px;}
.tile-value{font-size:24px;font-weight:700;}
.tile-label{color:var(--text-secondary);margin-top:2px;}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:4px 18px 18px;overflow-x:auto;}
table{border-collapse:collapse;width:100%;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap;}
th{color:var(--text-secondary);font-weight:600;}
.bars{display:flex;align-items:flex-end;gap:3px;height:90px;padding-top:6px;}
.bar-column{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:26px;}
.bar{width:16px;background:__SERIES__;border-radius:2px 2px 0 0;}
.bar-label{color:var(--text-muted);font-size:10px;transform:rotate(-60deg);
  transform-origin:top left;height:26px;}
.muted{color:var(--text-muted);}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#fff;}
.badge-good{background:__GOOD__;}
.badge-warn{background:__WARN__;color:#0b0b0b;}
.badge-bad{background:__BAD__;}
footer{color:var(--text-muted);font-size:12px;margin-top:22px;max-width:80ch;}
"""
_PAGE_CSS = (
    _PAGE_CSS.replace("__SERIES__", _SERIES_HUE)
    .replace("__GOOD__", _STATUS_GOOD)
    .replace("__WARN__", _STATUS_WARN)
    .replace("__BAD__", _STATUS_BAD)
)

_MAXIMUM_BAR_PIXELS = 70


@dataclass(frozen=True, slots=True)
class DeepHistoryMarketCoverage:
    """One market's loaded span, as measured from the store."""

    market: str
    files: int
    rows: int
    earliest: str
    latest: str
    distinct_symbols: int
    archive_files: int
    """How many files the ARCHIVE holds for this market, so a shortfall is visible."""

    @property
    def files_missing(self) -> int:
        return max(0, self.archive_files - self.files)


@dataclass(frozen=True, slots=True)
class DeepHistorySurfaceState:
    """Everything the page renders, all read from the store."""

    coverage: Sequence[DeepHistoryMarketCoverage]
    rows_per_year: Sequence[tuple[int, int]]
    quarantine_by_reason: Sequence[tuple[str, int]]
    total_rows: int
    total_quarantined: int


def _year_bars(rows_per_year: Sequence[tuple[int, int]]) -> str:
    """One bar per year: 33 of them, compared against each other rather than followed."""
    if not rows_per_year:
        return '<p class="muted">nothing loaded yet</p>'
    largest = max(rows for _, rows in rows_per_year) or 1
    columns = []
    for year, rows in rows_per_year:
        height = max(2, round(_MAXIMUM_BAR_PIXELS * rows / largest))
        columns.append(
            f'<div class="bar-column">'
            f'<div class="bar" style="height:{height}px" title="{year}: {rows:,} rows"></div>'
            f'<div class="bar-label">{year}</div></div>'
        )
    return f'<div class="bars">{"".join(columns)}</div>'


def _coverage_row(coverage: DeepHistoryMarketCoverage) -> str:
    badge = (
        '<span class="badge badge-good">complete</span>'
        if coverage.files_missing == 0
        else f'<span class="badge badge-warn">{coverage.files_missing:,} unloaded</span>'
    )
    return (
        f"<tr><td>{escape(coverage.market)}</td><td>{badge}</td>"
        f"<td>{coverage.files:,} / {coverage.archive_files:,}</td>"
        f"<td>{coverage.rows:,}</td>"
        f"<td>{escape(coverage.earliest)}</td><td>{escape(coverage.latest)}</td>"
        f"<td>{coverage.distinct_symbols:,}</td></tr>"
    )


def render_deep_history_page(state: DeepHistorySurfaceState) -> str:
    """The whole surface, self-contained, every number read off the store."""
    spans = [c for c in state.coverage if c.rows]
    earliest = min((c.earliest for c in spans), default="—")
    latest = max((c.latest for c in spans), default="—")
    quarantine_share = (
        state.total_quarantined / (state.total_rows + state.total_quarantined)
        if (state.total_rows + state.total_quarantined)
        else 0.0
    )
    coverage_rows = "".join(_coverage_row(coverage) for coverage in state.coverage)
    quarantine_rows = "".join(
        f"<tr><td>{escape(reason)}</td><td>{count:,}</td></tr>"
        for reason, count in state.quarantine_by_reason
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deep history</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Deep history</h1>
<p class="sub">Thirty-three years of NSE daily bhavcopy, loaded from the archive into a
queryable store. The files arrive in five different formats and none of the four changeovers
is announced anywhere, so every file is identified by its HEADER rather than its date, and
every row is checked against the session its own file name claims. What could not be
believed is quarantined with a reason rather than dropped.</p>

<div class="tiles">
<div class="tile"><div class="tile-value">{state.total_rows:,}</div>
<div class="tile-label">rows loaded</div></div>
<div class="tile"><div class="tile-value">{escape(earliest)}</div>
<div class="tile-label">earliest session held</div></div>
<div class="tile"><div class="tile-value">{escape(latest)}</div>
<div class="tile-label">latest session held</div></div>
<div class="tile"><div class="tile-value">{state.total_quarantined:,}</div>
<div class="tile-label">rows refused ({quarantine_share:.2%})</div></div>
</div>

<h2>Rows per year</h2>
<div class="panel">{_year_bars(state.rows_per_year)}</div>

<h2>Coverage by market</h2>
<div class="panel">
<table><thead><tr><th>Market</th><th>State</th><th>Files</th><th>Rows</th>
<th>Earliest</th><th>Latest</th><th>Symbols</th></tr></thead>
<tbody>{coverage_rows or '<tr><td colspan="7" class="muted">nothing loaded</td></tr>'}</tbody>
</table>
</div>

<h2>What was refused</h2>
<div class="panel">
<table><thead><tr><th>Reason</th><th>Rows</th></tr></thead>
<tbody>{quarantine_rows or '<tr><td colspan="2" class="muted">nothing refused</td></tr>'}
</tbody></table>
</div>

<footer>Untraded contracts are kept, not refused: 93% of F&amp;O rows have no open, high or low
because nothing traded, while their close is the exchange's settlement mark and their open
interest is a real position. Rows are stored RAW — corporate actions are applied on read,
because a split restated into storage cannot be un-restated when the action table is later
corrected, and NSE revises its own.</footer>
</body></html>"""
