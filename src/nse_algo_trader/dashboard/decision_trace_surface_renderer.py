"""`L13.29`'s surface (`R.08`) — what the bot was actually thinking, from its own record.

Engine: `nse_algo_trader.decision_trace`. Spec `docs/research/257`, review `docs/research/258`,
decision `A.136`.

**`A.29` required the trace before any panel**, precisely so this page cannot be a reconstruction: a
"why did it trade?" view assembled from trade records afterwards shows what a reasonable bot might
have thought. Every number here is read from records written at the decision instant.

**The one number that must never be hidden is the unexplained count.** A trace that does not account
for its own outcome is the honest state, and an adversarial review found 380 traces asserting a
PASSING gate as the cause of an abstain because the alternative — saying nothing bound — had not
been built. The page therefore leads with how much of the session its own reasoning accounts for,
and shows the shortfall in the critical colour rather than rounding it away.

**Form.** The binding-gate tally is the one chart: a single-series magnitude across a handful of
named categories, which is a horizontal bar — the labels are words of unequal length and vertical
bars would rotate them. It is one series, so there is no legend (the title names it) and each bar
carries its own value as a direct label. Everything else is a stat tile or a table, because a
count and a sample are not chart-shaped.

**Colour.** House ramp, single hue for the bars, since this is magnitude and not identity — a
categorical palette here would imply the gates are unrelated kinds rather than counts of one thing.
The unexplained tile uses the reserved critical step, with its meaning in text beside it.
"""

from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from nse_algo_trader.decision_trace.decision_trace_record import (
    DecisionTrace,
    DecisionTraceSummary,
)

_PAGE = """<main>
<h1>Decision traces</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""

_BAR_HUE = "#3b7dd8"
"""One hue for a magnitude series.

Sequential, not categorical: these are counts of the same thing (decisions) split by which gate
bound, so giving each gate its own hue would claim they are different kinds of quantity.

Chosen by the validator, not by eye. The first pick (#5a7fb8) FAILED the chroma floor at 0.097 —
it read as grey against this dashboard's warm neutrals, which for the only coloured mark on the
page means the magnitude carries no colour at all. This one passes chroma and >=3:1 contrast in
both light and dark.
"""

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
"""Traces are STORED in UTC so one instant is one row; they are READ in market time.

The first live render showed `04:10` for a decision taken at 09:40 IST. A trading dashboard for
one exchange that prints another timezone's clock without saying so is not ambiguous, it is wrong:
a reader matching a trace against the session has to discover the five-and-a-half-hour offset for
themselves.
"""

_CRITICAL = "#b3261e"
_MUTED = "var(--text-muted)"

_STYLE = """<style>
:root{
  --border:#e2e1dc; --text-secondary:#52514e; --text-muted:#77766f;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --border:var(--border); --text-secondary:var(--text-secondary); --text-muted:var(--text-muted);
  }
}
[data-theme="dark"]{
  --border:var(--border); --text-secondary:var(--text-secondary); --text-muted:var(--text-muted);
}
.trace-tiles{display:flex;gap:18px;flex-wrap:wrap;margin:12px 0 20px}
.trace-tile{min-width:150px}
.trace-tile .v{font-size:26px;font-variant-numeric:tabular-nums}
.trace-tile .l{font-size:12px;color:var(--text-muted);margin-top:2px}
.trace-tile .n{font-size:11px;color:var(--text-muted);margin-top:4px;max-width:22em}
.gate-row{display:flex;align-items:center;gap:10px;margin:6px 0}
.gate-name{width:230px;font-family:ui-monospace,monospace;font-size:12px;text-align:right}
.gate-bar{height:14px;border-radius:3px;min-width:2px}
.gate-count{font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums}
.trace-table{width:100%;border-collapse:collapse;margin-top:10px}
.trace-table th{text-align:left;font-size:12px;color:var(--text-muted);padding:6px 10px 6px 0;
 border-bottom:1px solid var(--border)}
.trace-table td{padding:8px 10px 8px 0;border-bottom:1px solid var(--border);vertical-align:top;
 font-size:12px}
.trace-table .sym{font-family:ui-monospace,monospace}
.trace-table .why{color:var(--text-muted);max-width:40em}
</style>"""


def _tile(value: str, label: str, note: str, colour: str = "") -> str:
    style = f' style="color:{colour}"' if colour else ""
    return (
        '<div class="trace-tile">'
        f'<div class="v"{style}>{escape(value)}</div>'
        f'<div class="l">{escape(label)}</div>'
        f'<div class="n">{escape(note)}</div>'
        "</div>"
    )


def _binding_bars(by_gate: dict[str, int], unexplained: int) -> str:
    """Horizontal bars, widest first, each directly labelled with its own count.

    Unexplained is charted alongside the gates rather than kept apart: it is a real answer to "what
    decided this?", and putting it in a footnote would let the eye read the chart as complete.
    """
    rows = sorted(by_gate.items(), key=lambda pair: -pair[1])
    if unexplained:
        rows.append(("(nothing bound — unexplained)", unexplained))
    largest = max((count for _, count in rows), default=1) or 1
    return "".join(
        '<div class="gate-row">'
        f'<div class="gate-name">{escape(gate)}</div>'
        f'<div class="gate-bar" style="width:{max(count / largest * 460, 2):.0f}px;'
        f'background:{_CRITICAL if gate.startswith("(") else _BAR_HUE}"></div>'
        f'<div class="gate-count">{count:,}</div>'
        "</div>"
        for gate, count in rows
    )


def _trimmed(explanation: str, limit: int = 220) -> str:
    """Cut at a word boundary, and SAY it was cut.

    A hard slice ended the live page mid-identifier — "TIED with sizing_co" — which reads as a
    corrupted value rather than as an abbreviation, on the one column a reader consults to check
    whether the machine's reasoning makes sense.
    """
    if len(explanation) <= limit:
        return explanation
    return explanation[:limit].rsplit(" ", 1)[0] + " …"


def _sample_row(trace: DecisionTrace) -> str:
    binding = trace.binding_constraint()
    consulted = " · ".join(
        f"{item.name}={item.value}" for item in trace.inputs[:2]
    )
    return (
        "<tr>"
        f'<td class="sym">{escape(trace.trading_symbol)}</td>'
        f"<td>{trace.decided_at.astimezone(MARKET_TIMEZONE):%H:%M}</td>"
        f"<td><b>{escape(trace.chosen_action)}</b></td>"
        f"<td>{'—' if trace.confidence is None else f'{trace.confidence:.2f}'}</td>"
        f'<td class="sym">{escape(consulted)}</td>'
        f'<td class="why">{escape(_trimmed(binding.explanation))}</td>'
        "</tr>"
    )


def render_decision_trace_page(summary: DecisionTraceSummary | None) -> str:
    """The page. Every value comes from the summary, so the renderer cannot disagree with the store.

    Args:
        summary: one session's counted reasoning, or `None` when no session has been traced.
    """
    if summary is None or summary.total == 0:
        return _PAGE.format(
            subtitle="No decision has been traced on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. No session has emitted a '
                "decision trace, so nothing here can say why anything happened. That is the "
                "absence of a record &mdash; and <code>A.29</code> is explicit that the missing "
                "record cannot be reconstructed afterwards from what was traded.</p></div>"
            ),
            footer=(
                "Run <code>scripts/verify_paper_session_on_real_data.py &lt;date&gt; "
                "--trace-to &lt;store&gt;</code>, or the daily <code>paper session</code> step."
            ),
        )

    exits = summary.by_kind.get("exit", 0)
    acted = summary.total - summary.by_action.get("abstain", 0) - summary.by_kind.get("exit", 0)
    explained = summary.explained_fraction
    # 4 unexplained in 21,270 is 99.98%, which `:.1%` renders as "100.0%" — the page would claim
    # every decision was accounted for while the tile beside it said four were not. An incomplete
    # explanation never displays as complete, however small the shortfall.
    explained_text = (
        "100.0%" if summary.unexplained == 0 else f"<{min(explained, 0.999):.1%}"
    )
    tiles = (
        _tile(f"{summary.total:,}", "decisions traced", "recorded at the instant, not after")
        + _tile(f"{acted:,}", "entries taken", "the rest abstained, each with a reason")
        + _tile(
            explained_text,
            "explained by their own gates",
            "the share whose recorded gates account for the outcome",
            "" if explained >= 1.0 else _CRITICAL,
        )
        + _tile(
            f"{exits:,}",
            "exit decisions",
            (
                "why it got OUT, recorded at the exit instant"
                if exits
                else "NONE — every exit this session is unexplained by its own record (M25)"
            ),
            "" if exits else _CRITICAL,
        )
        + _tile(
            f"{summary.unexplained:,}",
            "nothing bound",
            "no recorded gate refused, yet the bot did nothing — the honest answer, not a guess",
            _MUTED if not summary.unexplained else _CRITICAL,
        )
    )

    exit_sample = (
        '<div class="panel"><h2>Why it got out</h2>'
        '<p class="sub">The exit cause is recorded as a GATE, not as prose, so the same '
        "normalised comparison that ranks entry gates ranks these &mdash; a horizon measured in "
        "seconds against a latch that is simply open or shut.</p>"
        '<table class="trace-table"><thead><tr><th>instrument</th><th>at</th><th>chose</th>'
        "<th>confidence</th><th>consulted</th><th>binding constraint</th></tr></thead>"
        f"<tbody>{''.join(_sample_row(trace) for trace in summary.sample_exited)}</tbody></table>"
        "</div>"
        if summary.sample_exited
        else (
            '<div class="panel"><h2>Why it got out</h2><p class="empty">NOT RECORDED. No exit '
            "decision was traced this session, so every position that closed did so without its "
            "reasoning being written down. <code>A.29</code> is explicit that this cannot be "
            "reconstructed afterwards from what was traded.</p></div>"
        )
    )

    sample = (
        '<div class="panel"><h2>What it was thinking</h2>'
        '<table class="trace-table"><thead><tr><th>instrument</th><th>at</th><th>chose</th>'
        "<th>confidence</th><th>consulted</th><th>binding constraint</th></tr></thead>"
        f"<tbody>{''.join(_sample_row(trace) for trace in summary.sample_acted)}</tbody></table>"
        "</div>"
        if summary.sample_acted
        else ""
    )

    return _PAGE.format(
        subtitle=(
            f"{summary.session_date:%Y-%m-%d} &mdash; every row written at the decision instant, "
f"shown in market time (IST). "
            f"<code>A.29</code>: reasoning is recorded, never reconstructed."
        ),
        body=(
            f"{_STYLE}"
            f'<div class="trace-tiles">{tiles}</div>'
            '<div class="panel"><h2>What decided each outcome</h2>'
            '<p class="sub">Gates are compared after normalising each margin by its own '
            "threshold, so a gate measured in rupees and one measured in sigma can be ranked "
            "against each other.</p>"
            f"{_binding_bars(summary.by_binding_gate, summary.unexplained)}</div>"
            f"{sample}"
            f"{exit_sample}"
        ),
        footer=(
            "The binding constraint is the gate that came closest to changing the outcome &mdash; "
            "<code>L13.33</code>: the near-miss is usually more informative than the pass. Where "
            "no gate refused and the bot still did nothing, this page says so rather than naming "
            "the tightest gate that passed, which is a false statement about a decision "
            "(<code>docs/research/258</code>)."
        ),
    )
