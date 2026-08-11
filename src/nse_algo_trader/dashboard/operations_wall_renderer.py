"""`L13.06` — the operations wall: every module, its measured state, worst first.

Matches the design system already established by the regime surface, so this file adds no
new colour decisions — the categorical slots and both surfaces were validated there and
are reused unchanged.

**Form.** 44 rows of four categorical states is not a chart; it is a table with a status
column and a summary strip. Forcing it into a plot would be decoration. The one genuinely
graphical element is the health strip at the top, which is a magnitude comparison across
four ordered states, so it is a single stacked bar with direct labels — the only place
the eye needs to grasp a proportion rather than read a value.

**Ordering is the design.** Worst first, always. A wall sorted alphabetically hides its
own findings behind whichever module happens to start with 'a', and the entire purpose of
this page is that problems are impossible to miss.

**Status colours are the reserved status palette**, never the categorical slots — orphans
and untested modules are states, not series, and `R.08`'s rule that colour never carries
meaning alone is satisfied by the word in every badge.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.dashboard.module_surface_catalogue import (
    CatalogueSummary,
    ModuleSurface,
    ModuleTier,
    SurfaceHealth,
    worst_first,
)

_HEALTH_COLOUR = {
    SurfaceHealth.ORPHAN: "#d03b3b",
    SurfaceHealth.UNTESTED: "#ec835a",
    SurfaceHealth.NO_REAL_DATA: "#fab219",
    SurfaceHealth.HEALTHY: "#0ca30c",
}
"""The reserved status palette — critical / serious / warning / good. Never reused for a
series, and every badge carries its word so colour is never the only channel."""

_HEALTH_ORDER = (
    SurfaceHealth.ORPHAN,
    SurfaceHealth.UNTESTED,
    SurfaceHealth.NO_REAL_DATA,
    SurfaceHealth.HEALTHY,
)


def _health_strip(summary: CatalogueSummary) -> str:
    """One stacked bar: the only place a proportion beats a number here."""
    if not summary.total:
        return ""
    segments = "".join(
        f'<span class="seg" style="width:{summary.count_of(health) / summary.total * 100:.2f}%;'
        f'background:{_HEALTH_COLOUR[health]}" title="{escape(health.value)}"></span>'
        for health in _HEALTH_ORDER
        if summary.count_of(health)
    )
    labels = "".join(
        f'<span class="legend-item"><i class="swatch" style="background:{_HEALTH_COLOUR[health]}">'
        f"</i>{escape(health.value)} <b>{summary.count_of(health)}</b></span>"
        for health in _HEALTH_ORDER
    )
    return f'<div class="strip">{segments}</div><div class="legend">{labels}</div>'


def _module_row(surface: ModuleSurface, access_query: str) -> str:
    colour = _HEALTH_COLOUR[surface.health]
    panel_cell = (
        f'<a href="/regime{access_query}">panel</a>'
        if surface.has_dedicated_panel
        else '<span class="muted">catalogue only</span>'
    )
    tests = (
        f"{surface.test_count} file{'s' if surface.test_count != 1 else ''}"
        if surface.test_count
        else "—"
    )
    return f"""<tr>
      <td><code>{escape(surface.short_name)}</code></td>
      <td><span class="badge" style="--badge:{colour}">{escape(surface.health.value)}</span></td>
      <td>{escape(surface.tier.value)}</td>
      <td class="num">{surface.source_lines:,}</td>
      <td class="num">{tests}</td>
      <td>{"yes" if surface.has_real_data_test else "—"}</td>
      <td>{panel_cell}</td>
    </tr>"""


def render_operations_wall(summary: CatalogueSummary, access_query: str = "") -> str:
    """The whole wall. Server-rendered, self-contained, no external requests."""
    rows = "".join(
        _module_row(surface, access_query) for surface in worst_first(summary.surfaces)
    )
    tier_counts = "".join(
        f'<span class="legend-item">{escape(tier.value)} <b>{len(summary.by_tier(tier))}</b></span>'
        for tier in ModuleTier
    )
    orphans = summary.count_of(SurfaceHealth.ORPHAN)
    untested = summary.count_of(SurfaceHealth.UNTESTED)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Operations wall — nse-algo-trader</title>
<style>
:root {{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--surface-0);color:var(--text-primary);padding:24px;
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 2px}}
.sub{{color:var(--text-secondary);font-size:13px;margin:0 0 18px}}
.card{{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;margin-bottom:16px}}
.strip{{display:flex;height:14px;border-radius:5px;overflow:hidden;gap:2px;margin-bottom:10px}}
.seg{{display:block;height:100%}}
.legend{{display:flex;flex-wrap:wrap;gap:16px;font-size:12.5px;color:var(--text-secondary)}}
.legend-item{{display:inline-flex;align-items:center;gap:6px}}
.swatch{{width:11px;height:11px;border-radius:3px;display:inline-block}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border)}}
td{{vertical-align:middle}}
th{{color:var(--text-secondary);font-weight:600;font-size:12px;text-transform:uppercase;
  letter-spacing:.04em}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
code{{font:12px ui-monospace,SFMono-Regular,monospace;color:var(--text-primary)}}
.badge{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
  color:var(--badge);border:1px solid var(--badge);white-space:nowrap}}
.muted{{color:var(--text-muted)}}
a{{color:inherit}}
.foot{{color:var(--text-muted);font-size:11.5px;margin-top:18px}}
.scroll{{overflow-x:auto}}
</style></head>
<body><div class="wrap">
  <h1>Operations wall</h1>
  <p class="sub">{summary.total} modules · {summary.healthy_fraction * 100:.0f}% healthy ·
    <b>{orphans}</b> orphaned · <b>{untested}</b> untested ·
    every value derived from the real import graph and test tree</p>

  <div class="card">
    {_health_strip(summary)}
    <div class="legend" style="margin-top:12px">{tier_counts}</div>
  </div>

  <div class="card scroll">
    <table>
      <thead><tr>
        <th>module</th><th>state</th><th>tier</th><th>lines</th>
        <th>tests</th><th>real-data</th><th>surface</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <p class="foot">Worst first, deliberately — a wall sorted alphabetically hides its own
    findings. ORPHAN means no runnable entry point reaches the module (`R.06`), measured
    by breadth-first walk of a real <code>ast</code> import graph from <code>scripts/</code>.
    UNTESTED means no test file imports it. NO REAL-DATA means its tier owes an `R.05`
    pass and no test carries the marker. Tier is read from the module's own vocabulary
    (`R.23b`). Nothing on this page is hand-typed: a module added tomorrow appears here
    with no edit, and one that loses its tests turns red by itself.</p>
</div></body></html>"""
