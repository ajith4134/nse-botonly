"""`L0.32` surface — how wrong this host's timestamps can be, and what that forbids.

Built against the dataviz procedure, and the form follows the reader's question. There are
two of those. *"Can I trust today's timestamps?"* is a lookup with one answer, so it is a
verdict banner and four stat tiles at the top. *"Has the clock been moving?"* is a change
over time across a handful of sessions, so it is a small **sparkline-style bar row** of the
per-session skew, where the reader compares heights rather than reading numbers — with the
numbers still in the table below, because a bar cannot be copied into a bug report.

**Colour does one job: verdict.** `trusted / degraded / refuse / immature` are states, so
they take the reserved status palette (the same four values `/rules` uses, for the same
reason) and every badge carries its word. Skew bars are a single neutral hue: they are one
series, and colouring a single series by value invents a category that is not there.

**A number nobody measured is never shown as zero.** An unbracketed host reads `immature`
and the tiles say so, rather than displaying `0.0 ms` and letting the reader assume a
measurement was taken.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html import escape

from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockDriftAlert,
    StoredClockOffsetFit,
)
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
    ChronyTracking,
    ReferenceClockConsensus,
)
from nse_algo_trader.clock_integrity.timestamp_trust_budget import (
    TimestampTrustAssessment,
    TrustVerdict,
)

_STATUS_REFUSE = "#c0392b"
_STATUS_DEGRADED = "#fab219"
_STATUS_TRUSTED = "#0ca30c"
_STATUS_IMMATURE = "#77766f"
_SERIES_HUE = "#3d6fd6"
"""One hue for the one series on this page. Status colours are never borrowed for it."""

_BADGE_BY_VERDICT = {
    TrustVerdict.TRUSTED: ("badge-trusted", "trusted"),
    TrustVerdict.DEGRADED: ("badge-degraded", "degraded"),
    TrustVerdict.REFUSE: ("badge-refuse", "refuse"),
    TrustVerdict.IMMATURE: ("badge-immature", "immature"),
}

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
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
  color:#ffffff;}
.badge-refuse{background:__REFUSE__;}
.badge-degraded{background:__DEGRADED__;color:#0b0b0b;}
.badge-trusted{background:__TRUSTED__;}
.badge-immature{background:__IMMATURE__;}
.verdict{display:flex;align-items:center;gap:12px;background:var(--surface-1);
  border:1px solid var(--border);border-radius:8px;padding:14px 18px;margin-bottom:22px;}
.verdict-reason{color:var(--text-secondary);}
.bars{display:flex;align-items:flex-end;gap:6px;height:74px;padding-top:6px;}
.bar-column{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:52px;}
.bar{width:26px;background:__SERIES__;border-radius:2px 2px 0 0;}
.bar-label{color:var(--text-muted);font-size:11px;}
.muted{color:var(--text-muted);}
footer{color:var(--text-muted);font-size:12px;margin-top:22px;max-width:80ch;}
"""
_PAGE_CSS = (
    _PAGE_CSS.replace("__REFUSE__", _STATUS_REFUSE)
    .replace("__DEGRADED__", _STATUS_DEGRADED)
    .replace("__TRUSTED__", _STATUS_TRUSTED)
    .replace("__IMMATURE__", _STATUS_IMMATURE)
    .replace("__SERIES__", _SERIES_HUE)
)

_MAXIMUM_BAR_PIXELS = 56


@dataclass(frozen=True, slots=True)
class ClockIntegritySurfaceState:
    """Everything the page renders, read off the store — nothing hand-authored."""

    assessment: TimestampTrustAssessment | None
    fits: Sequence[StoredClockOffsetFit]
    alerts: Sequence[ClockDriftAlert]
    consensus: ReferenceClockConsensus | None
    chrony: ChronyTracking | None


def _milliseconds(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value * 1000:.2f} ms"


def _skew_bars(fits: Sequence[StoredClockOffsetFit]) -> str:
    """Per-session skew as bar heights: the reader compares, then reads the table."""
    if not fits:
        return '<p class="muted">no sessions fitted yet</p>'
    magnitudes = [abs(stored.fit.skew_ppm) for stored in fits]
    largest = max(magnitudes) or 1.0
    columns = []
    for stored, magnitude in zip(fits, magnitudes, strict=True):  # one bar per session
        height = max(2, round(_MAXIMUM_BAR_PIXELS * magnitude / largest))
        columns.append(
            f'<div class="bar-column">'
            f'<div class="bar" style="height:{height}px" '
            f'title="{stored.fit.skew_ppm:+.2f} ppm"></div>'
            f'<div class="bar-label">{escape(stored.session_date.isoformat()[5:])}</div>'
            f"</div>"
        )
    return f'<div class="bars">{"".join(columns)}</div>'


def latest_belief_per_session(
    fits: Sequence[StoredClockOffsetFit],
) -> list[tuple[StoredClockOffsetFit, int]]:
    """One row per session — the newest belief — with how many beliefs preceded it.

    The store deliberately keeps every fit rather than overwriting, so re-running a session
    adds a row. Rendering them all made the page look like it had fitted the same day twice
    with identical numbers, which reads as a bug rather than as a belief history. The
    revision count keeps the history visible without repeating it.
    """
    newest: dict[date, StoredClockOffsetFit] = {}
    counts: dict[date, int] = {}
    for stored in fits:
        newest[stored.session_date] = stored
        counts[stored.session_date] = counts.get(stored.session_date, 0) + 1
    return [
        (newest[session], counts[session] - 1)
        for session in sorted(newest, key=lambda session: session.isoformat())
    ]


def _fit_row(stored: StoredClockOffsetFit, earlier_beliefs: int) -> str:
    fit = stored.fit
    disagreement = (
        "—"
        if not math.isfinite(fit.split_half_offset_disagreement_seconds)
        else _milliseconds(fit.split_half_offset_disagreement_seconds)
    )
    return (
        f"<tr><td>{escape(stored.session_date.isoformat())}</td>"
        f"<td>{_milliseconds(fit.apparent_offset_seconds)}</td>"
        f"<td>{fit.skew_ppm:+.2f}</td>"
        f"<td>{disagreement}</td>"
        f"<td>{fit.sample_count:,}</td>"
        f"<td>{fit.hull_vertex_count}</td>"
        f"<td>{stored.raw_row_count:,}</td>"
        f"<td>{stored.absent_exchange_stamp_count:,}</td>"
        f"<td>{stored.negative_lag_count:,}</td>"
        f"<td>{earlier_beliefs or '<span class=muted>-</span>'}</td></tr>"
    )


def _deduplicated_alerts(alerts: Sequence[ClockDriftAlert]) -> list[ClockDriftAlert]:
    """The same change point found by a re-run is one finding, not two."""
    seen: dict[tuple[str, str, str], ClockDriftAlert] = {}
    for alert in alerts:
        seen[(alert.change_began_at.isoformat(), alert.detector, alert.series)] = alert
    return [seen[key] for key in sorted(seen)]


def _alert_row(alert: ClockDriftAlert) -> str:
    return (
        f"<tr><td>{escape(alert.change_began_at.isoformat(timespec='seconds'))}</td>"
        f"<td>{escape(alert.detector)}</td>"
        f"<td>{escape(alert.series)}</td>"
        f"<td>{_milliseconds(alert.before_value)}</td>"
        f"<td>{_milliseconds(alert.after_value)}</td>"
        f"<td>{escape(alert.session_date.isoformat())}</td></tr>"
    )


def _reference_line(state: ClockIntegritySurfaceState) -> str:
    if state.consensus is None:
        return (
            '<p class="muted">No reference bracket: no NTP majority was reached, so this '
            "host's own clock error is unmeasured and timestamps are not corrected.</p>"
        )
    consensus = state.consensus
    chrony_note = (
        f" chrony reports {state.chrony.frequency_ppm:+.3f} ppm against "
        f"{escape(state.chrony.reference_id)} (stratum {state.chrony.stratum})."
        if state.chrony is not None
        else " chrony is not installed on this host."
    )
    falseticker_note = (
        f" Discarded as falsetickers: {escape(', '.join(consensus.falsetickers))}."
        if consensus.falsetickers
        else ""
    )
    return (
        f"<p>Host minus UTC is bracketed to "
        f"[{_milliseconds(consensus.lower_bound_seconds)}, "
        f"{_milliseconds(consensus.upper_bound_seconds)}] by "
        f"{escape(', '.join(consensus.agreeing_servers))}.{falseticker_note}{chrony_note}</p>"
    )


def render_clock_integrity_page(state: ClockIntegritySurfaceState) -> str:
    """The whole surface, self-contained. Every number comes from the store."""
    assessment = state.assessment
    badge_class, word = (
        _BADGE_BY_VERDICT[assessment.verdict]
        if assessment is not None
        else _BADGE_BY_VERDICT[TrustVerdict.IMMATURE]
    )
    reason = escape(assessment.reason) if assessment is not None else "no session has been assessed"
    worst_case = _milliseconds(assessment.worst_case_error_seconds if assessment else None)
    host_error = _milliseconds(assessment.host_error_seconds if assessment else None)
    feed_floor = _milliseconds(assessment.feed_floor_seconds if assessment else None)
    by_session = latest_belief_per_session(state.fits)
    latest_skew = f"{by_session[-1][0].fit.skew_ppm:+.2f} ppm" if by_session else "—"
    ordered_fits = [stored for stored, _ in by_session][-14:]

    fit_rows = "".join(_fit_row(stored, earlier) for stored, earlier in reversed(by_session))
    alert_rows = "".join(
        _alert_row(alert) for alert in reversed(_deduplicated_alerts(state.alerts))
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clock integrity</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Clock integrity</h1>
<p class="sub">Nothing here observes exchange time directly. The feed gives a one-way delay
contaminated by an unknown offset and truncated to whole seconds, so the offset is fitted as
the LOWER ENVELOPE of the delay cloud; NTP brackets this host's own error from both sides.
The two are combined into one worst-case budget, and the verdict is what the rest of the
system is allowed to conclude from a host timestamp.</p>

<div class="verdict">
<span class="badge {badge_class}">{word}</span>
<span class="verdict-reason">{reason}</span>
</div>

<div class="tiles">
<div class="tile"><div class="tile-value">{worst_case}</div>
<div class="tile-label">worst-case timestamp error</div></div>
<div class="tile"><div class="tile-value">{host_error}</div>
<div class="tile-label">host minus UTC (NTP bracket)</div></div>
<div class="tile"><div class="tile-value">{feed_floor}</div>
<div class="tile-label">feed floor — delay + exchange error</div></div>
<div class="tile"><div class="tile-value">{latest_skew}</div>
<div class="tile-label">latest fitted skew</div></div>
</div>

{_reference_line(state)}

<h2>Fitted skew per session</h2>
<div class="panel">{_skew_bars(ordered_fits)}</div>

<h2>Session fits</h2>
<div class="panel">
<table><thead><tr><th>Session</th><th>Offset (upper bound)</th><th>Skew ppm</th>
<th>Split-half disagreement</th><th>Observations</th><th>Hull vertices</th>
<th>Raw rows</th><th>Absent stamps</th><th>Negative lags</th><th>Earlier beliefs</th>
</tr></thead>
<tbody>{fit_rows or '<tr><td colspan="10" class="muted">no fits recorded</td></tr>'}</tbody>
</table>
</div>

<h2>Change points</h2>
<div class="panel">
<table><thead><tr><th>Change began</th><th>Detector</th><th>Series</th><th>Before</th>
<th>After</th><th>Session</th></tr></thead>
<tbody>{alert_rows or '<tr><td colspan="6" class="muted">no change points detected</td></tr>'}
</tbody></table>
</div>

<footer>The offset is an UPPER bound: it contains the smallest network delay observed, which
cannot be separated from the clock by feed data alone. Skew is a difference of offsets and is
therefore free of that constant, which is why drift is measurable to ppm here while the
absolute offset is not. Change points are dated by Page-Hinkley, which located a synthetic
step to within one minute; ADWIN runs alongside for distribution changes it would miss.
</footer>
</body></html>"""


def session_dates_rendered(state: ClockIntegritySurfaceState) -> tuple[date, ...]:
    """The sessions the page is showing — used by the tests and the manifest audit."""
    return tuple(stored.session_date for stored in state.fits)
