"""`L0.33` surface — which broker to believe, and how often they disagreed.

Built against the dataviz procedure, and the reader has two questions. *"Is the feed sound
today?"* is a lookup: one banner and four stat tiles. *"Which broker should I be using?"* is
a comparison across a small set of named things, which is a RANKED TABLE, not a chart —
seven brokers would not make a useful bar chart and two make an absurd one, while a table
lets the reader compare four different quality measures per broker at once.

**Colour does one job: whether a session's quotes were usable.** Resolved / crossed /
refused are states, so they take the same reserved status palette `/rules` and `/clock` use.
The per-instrument disagreement column is a plain number: it spans three orders of magnitude
across the universe, and any colour ramp over that hides the tail this page exists to show.

**Everything is read from the stored session summary**, never recomputed on request — the
lesson `/microstructure` taught by growing to 31 seconds and breaking the screenshot capture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from nse_algo_trader.consolidated_feed.broker_reliability_store import BrokerReliability
from nse_algo_trader.consolidated_feed.consolidated_feed_engine import BrokerRanking
from nse_algo_trader.consolidated_feed.consolidated_feed_session_runner import (
    ConsolidatedFeedSessionReport,
)

_STATUS_BAD = "#c0392b"
_STATUS_WARN = "#fab219"
_STATUS_GOOD = "#0ca30c"
_STATUS_UNKNOWN = "#77766f"

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
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#ffffff;}
.badge-bad{background:__BAD__;}
.badge-warn{background:__WARN__;color:#0b0b0b;}
.badge-good{background:__GOOD__;}
.badge-unknown{background:__UNKNOWN__;}
.muted{color:var(--text-muted);}
footer{color:var(--text-muted);font-size:12px;margin-top:22px;max-width:80ch;}
"""
_PAGE_CSS = (
    _PAGE_CSS.replace("__BAD__", _STATUS_BAD)
    .replace("__WARN__", _STATUS_WARN)
    .replace("__GOOD__", _STATUS_GOOD)
    .replace("__UNKNOWN__", _STATUS_UNKNOWN)
)

WELL_RESOLVED_FRACTION = 0.95
"""Above this share of resolved groups the feed is behaving. Stated rather than tuned: it is
the level at which fewer than one group in twenty needed a refusal, which is the point past
which a consumer can rely on the consolidated price being there when asked."""


@dataclass(frozen=True, slots=True)
class ConsolidatedFeedSurfaceState:
    """Everything the page renders, all of it read from stores."""

    latest: ConsolidatedFeedSessionReport | None
    reports: Sequence[ConsolidatedFeedSessionReport]
    rankings: Sequence[BrokerRanking]
    reliabilities: Sequence[BrokerReliability]
    noise_variances: dict[str, float | None]


def _session_badge(report: ConsolidatedFeedSessionReport | None) -> tuple[str, str]:
    if report is None or not report.quotes_consolidated:
        return "badge-unknown", "no capture"
    if report.resolved_fraction >= WELL_RESOLVED_FRACTION:
        return "badge-good", "resolving"
    if report.resolved_fraction >= WELL_RESOLVED_FRACTION - 0.1:
        return "badge-warn", "degraded"
    return "badge-bad", "disagreeing"


def _broker_row(
    reliability: BrokerReliability, noise_variance: float | None, rankings: Sequence[BrokerRanking]
) -> str:
    own = [ranking for ranking in rankings if ranking.broker == reliability.broker]
    mean_score = sum(ranking.score for ranking in own) / len(own) if own else float("nan")
    best_on = sum(
        1
        for ranking in own
        if ranking.score
        == min(
            other.score
            for other in rankings
            if other.trading_symbol == ranking.trading_symbol
        )
    )
    noise = (
        f"{noise_variance:.3f}"
        if noise_variance is not None
        else '<span class=muted>unidentifiable</span>'
    )
    return (
        f"<tr><td>{escape(reliability.broker)}</td>"
        f"<td>{reliability.observation_count:,}</td>"
        f"<td>{reliability.failure_rate:.2%}</td>"
        f"<td>{reliability.unchanged_while_others_moved_rate:.2%}</td>"
        f"<td>{reliability.freshness_mean_seconds:.3f}s</td>"
        f"<td>{noise}</td>"
        f"<td>{mean_score:.2f}</td>"
        f"<td>{best_on}/{len(own)}</td></tr>"
    )


def _session_row(report: ConsolidatedFeedSessionReport) -> str:
    return (
        f"<tr><td>{escape(report.session_date.isoformat())}</td>"
        f"<td>{report.quotes_consolidated:,}</td>"
        f"<td>{report.resolved_fraction:.2%}</td>"
        f"<td>{report.crossed:,}</td>"
        f"<td>{report.single_source:,}</td>"
        f"<td>{report.instruments}</td>"
        f"<td>{escape(', '.join(report.brokers))}</td>"
        f"<td>{report.median_dispersion_paise:.1f}</td>"
        f"<td>{escape(report.worst_instrument or '-')} "
        f"({report.worst_instrument_dispersion_paise or 0:.1f})</td>"
        f"<td>{report.inadmissible_instrument_sessions}</td></tr>"
    )


def _worst_instrument_rows(rankings: Sequence[BrokerRanking], limit: int = 15) -> str:
    worst = sorted(rankings, key=lambda ranking: -ranking.mean_absolute_deviation_paise)[:limit]
    return "".join(
        f"<tr><td>{escape(ranking.trading_symbol)}</td>"
        f"<td>{escape(ranking.broker)}</td>"
        f"<td>{ranking.mean_absolute_deviation_paise:.1f}</td>"
        f"<td>{ranking.divergence_rate:.2%}</td>"
        f"<td>{ranking.frozen_rate:.2%}</td>"
        f"<td>{ranking.comparisons:,}</td></tr>"
        for ranking in worst
    )


def render_consolidated_feed_page(state: ConsolidatedFeedSurfaceState) -> str:
    """The whole surface, self-contained, every number read off a store."""
    badge_class, badge_word = _session_badge(state.latest)
    latest = state.latest
    resolved = f"{latest.resolved_fraction:.1%}" if latest else "—"
    crossed = f"{latest.crossed:,}" if latest else "—"
    inadmissible = f"{latest.inadmissible_instrument_sessions}" if latest else "—"
    brokers = ", ".join(latest.brokers) if latest else "none"

    broker_rows = "".join(
        _broker_row(reliability, state.noise_variances.get(reliability.broker), state.rankings)
        for reliability in state.reliabilities
    )
    session_rows = "".join(_session_row(report) for report in reversed(list(state.reports)))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consolidated feed</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Consolidated feed</h1>
<p class="sub">One synthetic tape from several brokers. Quotes are aligned into windows that
can describe the same market state, fused by liquidity and by each broker's measured noise,
and REFUSED rather than averaged when the sources disagree by more than the quoted spreads
can explain. Per-source noise is decomposed by the three-cornered hat, which needs three
independent feeds — with two, the engine weights by liquidity alone and says so.</p>

<div class="tiles">
<div class="tile"><div class="tile-value">
<span class="badge {badge_class}">{badge_word}</span></div>
<div class="tile-label">latest session · {escape(brokers)}</div></div>
<div class="tile"><div class="tile-value">{resolved}</div>
<div class="tile-label">groups resolved to one price</div></div>
<div class="tile"><div class="tile-value">{crossed}</div>
<div class="tile-label">crossed books — one source stale</div></div>
<div class="tile"><div class="tile-value">{inadmissible}</div>
<div class="tile-label">instrument-sessions ruled inadmissible</div></div>
</div>

<h2>Brokers</h2>
<div class="panel">
<table><thead><tr><th>Broker</th><th>Comparisons</th><th>Failure rate</th>
<th>Frozen while others moved</th><th>Mean freshness</th><th>Noise variance (bps²)</th>
<th>Mean score</th><th>Best on</th></tr></thead>
<tbody>{broker_rows or '<tr><td colspan="8" class="muted">nothing learned yet</td></tr>'}
</tbody></table>
</div>

<h2>Worst-disagreeing instruments</h2>
<div class="panel">
<table><thead><tr><th>Instrument</th><th>Broker</th><th>Mean abs deviation (paise)</th>
<th>Divergence rate</th><th>Frozen rate</th><th>Comparisons</th></tr></thead>
<tbody>{_worst_instrument_rows(state.rankings)
    or '<tr><td colspan="6" class="muted">no rankings yet</td></tr>'}</tbody></table>
</div>

<h2>Sessions</h2>
<div class="panel">
<table><thead><tr><th>Session</th><th>Groups</th><th>Resolved</th><th>Crossed</th>
<th>Single-source</th><th>Instruments</th><th>Brokers</th><th>Median dispersion</th>
<th>Worst instrument</th><th>Inadmissible</th></tr></thead>
<tbody>{session_rows or '<tr><td colspan="10" class="muted">no sessions yet</td></tr>'}
</tbody></table>
</div>

<footer>A consolidated price is only better than its best source if the engine knows which
source that is, and that needs THREE feeds: with two, `var(a-b)` is one number shared by both
and no amount of data separates them. A noise column reading "unidentifiable" therefore means
either fewer than three sources or a source quieter than the estimator can resolve — never a
missing measurement quietly rounded to zero. Read "best on" against the comparison count
beside it: a broker that answered for six minutes and one that answered all session are not
comparable on wins alone. Refusals are the point of the crossed and inadmissible counts — a
page showing only prices would hide exactly the days worth knowing about.</footer>
</body></html>"""
