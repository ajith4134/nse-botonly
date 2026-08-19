"""`L5.30`'s surface (`R.08`) — where every paper-trading bot stands on the activation ladder.

Engine: `nse_algo_trader.paper_loop.bot_maturity_ladder`. Spec `docs/research/255`, review
`docs/research/256`, decision `A.134`.

**Why this needs a surface.** The rung is the first of `R.22`'s two keys: it is the number that
decides whether a bot may act at all. A gate nobody looks at is a gate nobody notices going wrong,
and the specific failure this page exists to make visible is a bot drifting toward promotion on
evidence a reader would have questioned. Every value is measured from the store on request, so a
trade recorded tomorrow appears here with no edit.

**Why it is a table and not a chart.** There are between one and a handful of bots, each with five
fields, and the reader's question is per-row ("where is this one, and why?") rather than
distributional. A bar chart of six rungs would be a chart of a table. The one genuinely visual
element is the win-rate-against-break-even mark, because that comparison has a polarity — above or
below a threshold the bot sets for itself — and a number pair does not show which side it is on.

**Colour carries no meaning alone.** Each rung ships its name in text ink beside a status dot, per
the reserved-status rule: the dot is recognition, the word is the fact. Measured with the palette
validator, the four status steps separate under protan/deutan/tritan simulation (worst adjacent
ΔE 11.3) in both light and dark surfaces; the amber and red steps fall below 3:1 against their
surface, which is exactly why the rung name is text and never a coloured label.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.paper_loop.bot_maturity_ladder import LadderAssessment
from nse_algo_trader.segment_bots.segment_bot_protocol import BotMaturityRung

_PAGE = """<main>
<h1>Activation ladder</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""

_RUNG_COLOUR: dict[BotMaturityRung, str] = {
    BotMaturityRung.RETIRED: "#b3261e",
    BotMaturityRung.COLD_START: "#8a8a80",
    BotMaturityRung.OBSERVING: "#fab219",
    BotMaturityRung.PAPER_QUALIFIED: "#0ca30c",
    BotMaturityRung.GRADUATION_CANDIDATE: "#0ca30c",
    BotMaturityRung.GRADUATED: "#0ca30c",
}
"""Reserved status steps, taken from the house palette rather than invented here.

`COLD_START` is deliberately the neutral grey: "no evidence yet" is not a status, and giving it a
status hue would put a bot that has never traded on the same footing as one that has been judged.
`RETIRED` is the critical step because it is the opposite — evidence AGAINST.
"""

_RUNG_MEANING: dict[BotMaturityRung, str] = {
    BotMaturityRung.RETIRED: "its own record says it loses money",
    BotMaturityRung.COLD_START: "not enough evidence to form an opinion",
    BotMaturityRung.OBSERVING: "an opinion, short of the bar to act on",
    BotMaturityRung.PAPER_QUALIFIED: "clears the bar, not yet sustained",
    BotMaturityRung.GRADUATION_CANDIDATE: "clears the bar and has sustained it",
    BotMaturityRung.GRADUATED: "armed on live capital by an operator",
}


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _win_rate_mark(assessment: LadderAssessment) -> str:
    """A track from 0 to 100%, the observed win rate as a bar, break-even as a threshold tick.

    The only chart on the page, and it earns its place: the pair of numbers alone does not show
    which side of its own threshold a bot sits on, and that side is the whole verdict. The tick is
    labelled in text beneath, so the mark is never the only carrier of the fact.

    `credit_spread_v1` is why this is drawn per bot rather than against a common 50% line: it wins
    46.3% of its trades and is profitable, because its own break-even is 22.5%.
    """
    observed = max(0.0, min(1.0, assessment.observed_win_rate))
    break_even = max(0.0, min(1.0, assessment.break_even_win_rate))
    ahead = observed >= break_even
    fill = "#0ca30c" if ahead else "#b3261e"
    return (
        '<div class="rate-track" role="img" aria-label='
        f'"wins {_percent(observed)} against a break-even of {_percent(break_even)}">'
        f'<div class="rate-fill" style="width:{observed * 100:.1f}%;background:{fill}"></div>'
        f'<div class="rate-tick" style="left:{break_even * 100:.1f}%"></div>'
        "</div>"
        f'<div class="rate-legend">wins <b>{escape(_percent(observed))}</b> · break-even '
        f"<b>{escape(_percent(break_even))}</b></div>"
    )


def _row(assessment: LadderAssessment) -> str:
    colour = _RUNG_COLOUR.get(assessment.rung, "#8a8a80")
    meaning = _RUNG_MEANING.get(assessment.rung, "")
    return (
        "<tr>"
        f'<td class="bot">{escape(assessment.bot_identity)}</td>'
        f'<td class="rung"><span class="dot" style="background:{colour}"></span>'
        f"<b>{escape(assessment.rung.label)}</b>"
        f'<div class="tile-note">{escape(meaning)}</div></td>'
        f'<td class="num">{assessment.closed_trades:,}</td>'
        f'<td class="num">{assessment.sessions:,}</td>'
        f"<td>{_win_rate_mark(assessment)}</td>"
        f'<td class="num"><b>{assessment.posterior_above_break_even:.3f}</b></td>'
        f'<td class="why">{escape(assessment.reason)}</td>'
        "</tr>"
    )


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
.ladder{width:100%;border-collapse:collapse;margin-top:12px}
.ladder th{text-align:left;font-weight:600;color:var(--text-muted);font-size:12px;
 border-bottom:1px solid var(--border);padding:6px 10px 6px 0}
.ladder td{padding:10px 10px 10px 0;border-bottom:1px solid var(--border);vertical-align:top}
.ladder .bot{font-family:ui-monospace,monospace;font-size:13px}
.ladder .num{text-align:right;font-variant-numeric:tabular-nums}
.ladder .why{color:var(--text-muted);font-size:12px;max-width:34em}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
 vertical-align:middle}
.rate-track{position:relative;height:8px;width:150px;background:var(--border);border-radius:4px}
.rate-fill{position:absolute;top:0;left:0;height:8px;border-radius:4px}
.rate-tick{position:absolute;top:-3px;width:2px;height:14px;background:var(--text-secondary)}
.rate-legend{font-size:11px;color:var(--text-muted);margin-top:4px;white-space:nowrap}
</style>"""


def render_bot_maturity_page(assessments: tuple[LadderAssessment, ...]) -> str:
    """The page. Every value is passed in, so the renderer cannot disagree with the ladder.

    Args:
        assessments: one per bot that has a track record, in whatever order the caller chose.
            An empty tuple is a real state and is rendered as one — see below.
    """
    if not assessments:
        return _PAGE.format(
            subtitle="No bot has a paper track record on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. No paper session has accrued a '
                "closed trade, so no bot has a rung and none may act. This is the absence of a "
                "record, not a record of failure &mdash; and until 2026-08-17 it was the permanent "
                "state of this project, because nothing scheduled ever traded "
                "(<code>B15</code>).</p></div>"
            ),
            footer=(
                "The daily <code>paper session</code> step of "
                "<code>scripts/run_daily_operations.py</code> accrues this record."
            ),
        )

    ranked = sorted(
        assessments,
        key=lambda item: (-item.rung.ladder_position, -item.closed_trades),
    )
    rows = "".join(_row(assessment) for assessment in ranked)
    retired = sum(1 for item in ranked if item.rung is BotMaturityRung.RETIRED)
    retired_note = (
        f" {retired} bot(s) sit at RETIRED, which is evidence AGAINST them rather than the absence "
        f"of evidence that COLD_START means."
        if retired
        else ""
    )
    return _PAGE.format(
        subtitle=(
            f"{len(ranked)} bot(s) with a paper track record. The rung is the FIRST of "
            f"<code>R.22</code>'s two keys; no bot on this page is armed, and none can arm "
            f"itself.{retired_note}"
        ),
        body=(
            f"{_STYLE}"
            '<div class="panel"><table class="ladder">'
            "<thead><tr><th>bot</th><th>rung</th><th>closed</th><th>sessions</th>"
            "<th>win rate vs its own break-even</th><th>P(expectancy&gt;0)</th><th>why</th></tr>"
            f"</thead><tbody>{rows}</tbody></table></div>"
        ),
        footer=(
            "Break-even is derived per bot from its own payoffs and costs, never assumed at 50% "
            "&mdash; <code>credit_spread_v1</code> wins 46.3% of its trades and is the most "
            "profitable of the three retained strategies. The verdict is "
            "<code>P(mean net P&amp;L &gt; 0)</code> by Bayesian bootstrap over that bot's own "
            "closed trades (<code>docs/research/256</code>)."
        ),
    )
