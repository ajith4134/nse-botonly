"""`L10.01`'s surface (`R.08`) — is the loop alive, what is it seeing, and how stale is its data.

Engine: `nse_algo_trader.paper_loop.continuous_paper_trading_scheduler`. Spec
`docs/research/265`.

**Why this page exists.** `A.143` is what a silent stop costs: a scheduled job was SIGKILLed every
firing for days and nothing was red, because its only evidence was a log nobody opened. A continuous
loop is worse in that respect than a timer — a timer that stops at least stops producing rows
somewhere, while a loop that stops just... stops. So the loop writes an iteration record whether it
did anything or not, and this page reads it.

**The two numbers an operator actually wants**, and neither is "is the process running":

1. **How long since the last healthy tick.** A process can be up and failing every iteration.
2. **The tape lag** — how stale the market data itself is. A live loop reading a dead capture is the
   failure that looks most like success, because every iteration succeeds.

**Colour carries no meaning alone.** Each state ships its word in text beside the dot, the same
reserved-status convention `/ladder` and `/bots` use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

from nse_algo_trader.paper_loop.continuous_paper_trading_scheduler import (
    SchedulerIteration,
    SessionPhase,
)

_PAGE = """<main>
<h1>The continuous loop</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""

_PHASE_COLOUR: dict[SessionPhase, str] = {
    SessionPhase.TRADING: "#0ca30c",
    SessionPhase.SQUARING_OFF: "#fab219",
    SessionPhase.BEFORE_OPEN: "#8a8a80",
    SessionPhase.AFTER_CLOSE: "#8a8a80",
}
"""Reserved status steps from the house palette. `BEFORE_OPEN` and `AFTER_CLOSE` are the neutral
grey deliberately: a closed market is not a problem, and colouring it as one trains the reader to
ignore the colour."""

_PHASE_MEANING: dict[SessionPhase, str] = {
    SessionPhase.TRADING: "deciding on live data",
    SessionPhase.SQUARING_OFF: "flattening only — no new positions (R.01)",
    SessionPhase.BEFORE_OPEN: "alive, waiting for the open",
    SessionPhase.AFTER_CLOSE: "alive, market closed",
}

STALE_AFTER_SECONDS = 120.0
"""When a tape lag stops reading as normal and starts reading as a problem.

A PRESENTATION boundary and nothing else — no decision reads it. Measured against the real capture
on 2026-08-18: a healthy lag sat at **21-29 seconds**, because the capture flushes on shard size
rather than on a timer. Two minutes is several flushes late, which is the point at which "the tape
is being written" stops being a plausible explanation.
"""


@dataclass(frozen=True, slots=True)
class ContinuousLoopSurfaceState:
    """What the page renders. Every field is measured from the liveness store, never maintained."""

    measured_at: datetime
    iterations: tuple[SchedulerIteration, ...]
    last_healthy_at: datetime | None

    @property
    def latest(self) -> SchedulerIteration | None:
        return self.iterations[0] if self.iterations else None

    @property
    def seconds_since_healthy(self) -> float | None:
        if self.last_healthy_at is None:
            return None
        return (self.measured_at - self.last_healthy_at).total_seconds()

    @property
    def failing_iterations(self) -> int:
        return sum(1 for iteration in self.iterations if not iteration.is_healthy)


SECONDS_BEFORE_MINUTES_READ_BETTER = 90.0
MINUTES_BEFORE_HOURS_READ_BETTER = 90.0 * 60.0
"""Where the duration text switches unit. Typography, not measurement — "5400s ago" is a number a
reader has to divide, and "1.5 h ago" is one they can act on."""


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < SECONDS_BEFORE_MINUTES_READ_BETTER:
        return f"{seconds:.0f}s ago"
    if seconds < MINUTES_BEFORE_HOURS_READ_BETTER:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _row(iteration: SchedulerIteration) -> str:
    colour = _PHASE_COLOUR.get(iteration.phase, "#8a8a80")
    meaning = _PHASE_MEANING.get(iteration.phase, "")
    lag = iteration.tape_lag_seconds
    lag_text = "—" if lag is None else f"{lag:.1f}s"
    lag_class = "bad" if lag is not None and lag > STALE_AFTER_SECONDS else "num"
    status = (
        f'<span class="bad">{escape(iteration.failure or "failed")}</span>'
        if not iteration.is_healthy
        else escape(iteration.note)
    )
    return (
        "<tr>"
        f'<td class="when">{escape(iteration.observed_at.isoformat(timespec="seconds"))}</td>'
        f'<td><span class="dot" style="background:{colour}"></span>'
        f"<b>{escape(iteration.phase.value.replace('_', ' '))}</b>"
        f'<div class="tile-note">{escape(meaning)}</div></td>'
        f'<td class="num">{iteration.instruments_observed:,}</td>'
        f'<td class="num">{iteration.proposals}</td>'
        f'<td class="{lag_class}">{lag_text}</td>'
        f'<td class="why">{status}</td>'
        "</tr>"
    )


_STYLE = """<style>
.loop{width:100%;border-collapse:collapse;margin-top:12px}
.loop th{text-align:left;font-weight:600;color:var(--text-muted);font-size:12px;
 border-bottom:1px solid var(--border);padding:6px 10px 6px 0}
.loop td{padding:9px 10px 9px 0;border-bottom:1px solid var(--border);vertical-align:top}
.loop .when{font-family:ui-monospace,monospace;font-size:12px;white-space:nowrap}
.loop .num{text-align:right;font-variant-numeric:tabular-nums}
.loop .why{color:var(--text-muted);font-size:12px;max-width:34em}
.loop .bad{color:#b3261e;font-weight:600;text-align:right}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
 vertical-align:middle}
.headline{display:flex;gap:28px;margin-top:10px;flex-wrap:wrap}
.headline div{min-width:150px}
.headline .k{font-size:12px;color:var(--text-muted)}
.headline .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
</style>"""


def render_continuous_loop_page(state: ContinuousLoopSurfaceState) -> str:
    """The page. Every value is passed in, so the renderer cannot disagree with the loop."""
    if not state.iterations:
        return _PAGE.format(
            subtitle="The continuous loop has never recorded an iteration on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. This is the absence of a loop, '
                "not a loop reporting nothing &mdash; and until 2026-08-18 it was the permanent "
                "state of this project (<code>B33</code>): the depth capture wrote a live tape all "
                "session and nothing read it between the daily timer's two firings.</p></div>"
            ),
            footer="<code>systemctl --user status nse-continuous-loop</code>",
        )

    latest = state.latest
    assert latest is not None
    since = state.seconds_since_healthy
    stale = since is not None and since > STALE_AFTER_SECONDS
    lag = latest.tape_lag_seconds
    headline = (
        '<div class="headline">'
        f'<div><div class="k">last healthy tick</div>'
        f'<div class="v{" bad" if stale else ""}">{escape(_duration(since))}</div></div>'
        f'<div><div class="k">phase</div><div class="v">'
        f"{escape(latest.phase.value.replace('_', ' '))}</div></div>"
        f'<div><div class="k">instruments observed</div>'
        f'<div class="v">{latest.instruments_observed:,}</div></div>'
        f'<div><div class="k">tape lag</div><div class="v">'
        f'{"unknown" if lag is None else f"{lag:.0f}s"}</div></div>'
        f'<div><div class="k">failing ticks shown</div>'
        f'<div class="v">{state.failing_iterations}</div></div>'
        "</div>"
    )
    rows = "".join(_row(iteration) for iteration in state.iterations)
    return _PAGE.format(
        subtitle=(
            f"Measured {escape(state.measured_at.isoformat(timespec='seconds'))} from the loop's "
            f"own append-only record. The loop writes a row every tick whether it acted or not, so "
            f"a stalled loop is visibly stalled rather than merely quiet &mdash; which is the "
            f"failure <code>A.143</code> cost days to notice."
        ),
        body=(
            f"{_STYLE}{headline}"
            '<div class="panel"><table class="loop">'
            "<thead><tr><th>tick</th><th>phase</th><th>observed</th><th>proposals</th>"
            "<th>tape lag</th><th>what happened</th></tr>"
            f"</thead><tbody>{rows}</tbody></table></div>"
        ),
        footer=(
            "Tape lag is how stale the market data itself is, and it catches the failure that "
            "looks most like success: a live loop reading a dead capture, where every iteration "
            "succeeds and nothing is true. A healthy lag measured 21-29s on 2026-08-18, "
            "because the capture flushes on shard size rather than on a timer."
        ),
    )
