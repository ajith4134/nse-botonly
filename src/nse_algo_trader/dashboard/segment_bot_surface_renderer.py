"""The six-segment-bot surface (`R.08`) — every bot, what it can see, and what it is waiting on.

Engines: `nse_algo_trader.segment_bots.*`. Decision `A.141`; the F&O capture that lifts five of the
six off daily cadence is `A.142`.

**Why this page exists and what it is for.** `A.130` put all six holons on one shared spine, and the
question an operator actually has is not "is bot X alive" but **"which of the six can act, on what
cadence, and which are held back by data rather than by judgement"** — because those two reasons
look identical from the outside and have completely different remedies. A bot at `COLD_START`
because its strategy is unproven needs sessions; a bot at `COLD_START` because its segment has no
data at all needs an ingestion pipeline. This page separates them, by name, on every row.

**Nothing here is hand-authored** (`R.08`). The rung is measured by `BotMaturityLadder` from that
bot's own closed trades; the conformance column is the `L5.29` suite actually re-run at render time
against a probe that includes an empty universe; the relevance is the bot's own carried state
answering. A status this page shows is a status the code just produced.

**Colour carries no meaning alone.** Every rung and every cadence ships its word in text beside its
dot, following the same reserved-status convention as `/ladder` — the dot is recognition and the
word is the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from nse_algo_trader.segment_bots.segment_bot_foundation import DecisionCadence
from nse_algo_trader.segment_bots.segment_bot_protocol import BotMaturityRung
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment

_PAGE = """<main>
<h1>The six segment bots</h1>
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
"""The same reserved steps `/ladder` uses, so one bot cannot appear in two colours on two pages."""

READY_ENOUGH_TO_READ_AS_HEALTHY = 0.5
"""Where the readiness bar turns from amber to green.

A presentation boundary and nothing else — no decision anywhere reads it. Half the universe ready is
the point at which "mostly warming up" becomes "mostly ready", which is the only claim the colour is
making.
"""

_CADENCE_NOTE: dict[DecisionCadence, str] = {
    DecisionCadence.INTRADAY: "decides through the session on live five-minute data",
    DecisionCadence.ONCE_PER_SESSION: "decides once, on the session's real closes",
}


@dataclass(frozen=True, slots=True)
class SegmentBotSurfaceRow:
    """One bot's measured state. Every field is produced by code, never typed.

    `data_blocker` is the field this page was built for: it is the difference between a bot that has
    not proven itself and a bot that has never been given anything to prove itself on.
    """

    bot_identity: str
    trading_segment: TradingSegment
    cadence: DecisionCadence
    rung: BotMaturityRung
    closed_trades: int
    sessions: int
    instruments_tracked: int
    instruments_mature: int
    relevance: float
    relevance_reason: str
    conformance_violations: int
    data_blocker: str = ""

    @property
    def is_conforming(self) -> bool:
        return self.conformance_violations == 0

    @property
    def can_act(self) -> bool:
        """Whether anything at all could come out of this bot today.

        A bot with no instruments has nothing to decide about, whatever its rung says. Stated as a
        property rather than inferred by the reader because `commodity_mcx` is exactly this case and
        it must not read as a broken bot.
        """
        return self.is_conforming and self.instruments_tracked > 0


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _coverage_mark(row: SegmentBotSurfaceRow) -> str:
    """How much of this bot's own universe it has enough history to speak about.

    The only chart on the page and it earns its place: "312 of 3,618" is a pair of numbers, while
    the reader's question is what FRACTION is ready, and a fraction is a length.
    """
    if row.instruments_tracked <= 0:
        return (
            '<div class="rate-legend">no instruments &mdash; nothing to be ready about</div>'
        )
    ready = row.instruments_mature / row.instruments_tracked
    fill = "#0ca30c" if ready >= READY_ENOUGH_TO_READ_AS_HEALTHY else "#fab219"
    return (
        '<div class="rate-track" role="img" aria-label='
        f'"{row.instruments_mature} of {row.instruments_tracked} instruments carry enough history">'
        f'<div class="rate-fill" style="width:{ready * 100:.1f}%;background:{fill}"></div>'
        "</div>"
        f'<div class="rate-legend"><b>{row.instruments_mature:,}</b> of '
        f"<b>{row.instruments_tracked:,}</b> ready ({escape(_percent(ready))})</div>"
    )


def _row_html(row: SegmentBotSurfaceRow) -> str:
    colour = _RUNG_COLOUR.get(row.rung, "#8a8a80")
    cadence_note = _CADENCE_NOTE.get(row.cadence, "")
    conformance = (
        '<span class="ok">conforms</span>'
        if row.is_conforming
        else f'<span class="bad">{row.conformance_violations} violation(s)</span>'
    )
    blocker = (
        f'<div class="blocker">{escape(row.data_blocker)}</div>' if row.data_blocker else ""
    )
    return (
        "<tr>"
        f'<td class="bot">{escape(row.bot_identity)}'
        f'<div class="tile-note">{escape(row.trading_segment.value)}</div></td>'
        f'<td class="rung"><span class="dot" style="background:{colour}"></span>'
        f"<b>{escape(row.rung.label)}</b>"
        f'<div class="tile-note">{row.closed_trades:,} closed · {row.sessions:,} session(s)</div>'
        f"</td>"
        f"<td><b>{escape(row.cadence.value.replace('_', ' '))}</b>"
        f'<div class="tile-note">{escape(cadence_note)}</div></td>'
        f"<td>{_coverage_mark(row)}{blocker}</td>"
        f'<td class="num"><b>{row.relevance:.3f}</b></td>'
        f"<td>{conformance}</td>"
        f'<td class="why">{escape(row.relevance_reason)}</td>'
        "</tr>"
    )


_STYLE = """<style>
.segbots{width:100%;border-collapse:collapse;margin-top:12px}
.segbots th{text-align:left;font-weight:600;color:var(--text-muted);font-size:12px;
 border-bottom:1px solid var(--border);padding:6px 10px 6px 0}
.segbots td{padding:10px 10px 10px 0;border-bottom:1px solid var(--border);vertical-align:top}
.segbots .bot{font-family:ui-monospace,monospace;font-size:13px}
.segbots .num{text-align:right;font-variant-numeric:tabular-nums}
.segbots .why{color:var(--text-muted);font-size:12px;max-width:30em}
.segbots .ok{color:#0ca30c;font-weight:600}
.segbots .bad{color:#b3261e;font-weight:600}
.blocker{font-size:11px;color:#b3261e;margin-top:6px;max-width:26em}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
 vertical-align:middle}
.rate-track{position:relative;height:8px;width:140px;background:var(--border);border-radius:4px}
.rate-fill{position:absolute;top:0;left:0;height:8px;border-radius:4px}
.rate-legend{font-size:11px;color:var(--text-muted);margin-top:4px;white-space:nowrap}
</style>"""


def render_segment_bot_page(rows: tuple[SegmentBotSurfaceRow, ...]) -> str:
    """The page. Every value is passed in, so the renderer cannot disagree with the bots."""
    if not rows:
        return _PAGE.format(
            subtitle="No segment bot could be built on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. The registry returned no bot, '
                "which is a failure of construction rather than of trading &mdash; the six are "
                "built unconditionally by <code>build_all_segment_bots</code>.</p></div>"
            ),
            footer="<code>nse_algo_trader.segment_bots.segment_bot_registry</code>",
        )

    ordered = sorted(rows, key=lambda row: (not row.can_act, row.trading_segment.value))
    body_rows = "".join(_row_html(row) for row in ordered)
    acting = sum(1 for row in ordered if row.can_act)
    intraday = sum(1 for row in ordered if row.cadence is DecisionCadence.INTRADAY)
    blocked = sum(1 for row in ordered if row.data_blocker)
    return _PAGE.format(
        subtitle=(
            f"{len(ordered)} bot(s) built and conformance-checked at render time. "
            f"<b>{acting}</b> have a universe to decide about; <b>{intraday}</b> decide intraday "
            f"and the rest once per session, which is a property of what data exists rather than "
            f"of the algorithm (<code>R.04</code>, <code>A.141</code>). <b>{blocked}</b> carry a "
            f"named data blocker. No bot on this page is armed and none can arm itself "
            f"(<code>R.22</code>)."
        ),
        body=(
            f"{_STYLE}"
            '<div class="panel"><table class="segbots">'
            "<thead><tr><th>bot</th><th>rung</th><th>cadence</th>"
            "<th>universe readiness</th><th>relevance</th><th>L5.29</th><th>why</th></tr>"
            f"</thead><tbody>{body_rows}</tbody></table></div>"
        ),
        footer=(
            "Rung is measured by <code>BotMaturityLadder</code> from each bot's OWN closed trades; "
            "the <code>L5.29</code> column is the shared conformance suite re-run on this request "
            "against a probe that includes an empty universe. A bot held back by a data blocker is "
            "not a bot that failed &mdash; the algorithm is built whole either way and only its "
            "ACTIVATION is gated (<code>R.04</code>)."
        ),
    )
