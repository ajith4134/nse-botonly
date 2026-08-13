"""`/sizing` — how big a position would be, and every reason it might be refused.

`R.08`. A sizing decision an operator cannot see is exactly the class of thing the rule exists to
prevent: the number that decides how much real money is at risk must be readable before it is used,
not reconstructed afterwards from a log.

**This page shows a WORKED DECISION, not a summary.** Every intermediate figure that produced the
quantity is on it — the volatility budget, the Kelly cap, which of the two bound it, the lot
rounding, and each gate tier's verdict with the rule that fired. A size an operator cannot
reconstruct is a size they cannot argue with.

**It never invents an input.** When the calibration, the price history or the lot size is missing,
the page says which one and renders no number. `L1.09`'s lesson is that a plausible substituted lot
size is worse than an absent one, and the same holds for every input above it.

**GET never writes.** The page opens the session risk state read-only and creates nothing; a
dashboard that writes a database on page load is a side effect nobody asked for. The one thing it
does show from live state — the session's latches and realised P&L — is read, never tripped: a read
that halts is a read nobody can safely perform to find out where they stand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

from nse_algo_trader.sizing.pre_trade_risk_gate import GateTier, RiskGateVerdict
from nse_algo_trader.sizing.session_risk_state_store import DailyLossLimit, SessionRiskState
from nse_algo_trader.sizing.volatility_targeted_position_sizer import SizedPosition

_STATUS_CRITICAL = "#b3261e"
_STATUS_WARNING = "#8a6100"
_STATUS_OK = "#1f6b3a"

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
h2{font-size:14px;margin:26px 0 10px;}
.sub{color:var(--text-secondary);margin:0 0 24px;max-width:88ch;}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:26px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:170px;flex:1 1 170px;}
.tile-value{font-size:24px;font-weight:700;}
.tile-label{color:var(--text-secondary);margin-top:2px;}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:4px 18px 18px;overflow-x:auto;}
.note{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;margin-bottom:22px;max-width:88ch;}
.note.warn{border-left:4px solid __WARNING__;}
.note.crit{border-left:4px solid __CRITICAL__;}
table{border-collapse:collapse;width:100%;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap;}
th{color:var(--text-secondary);font-weight:600;}
td.figure,th.figure{text-align:right;font-variant-numeric:tabular-nums;}
td.reason{white-space:normal;color:var(--text-secondary);}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#ffffff;}
.badge-ok{background:__OK__;}
.badge-warn{background:__WARNING__;}
.badge-crit{background:__CRITICAL__;}
"""

_PAGE_CSS = (
    _PAGE_CSS.replace("__CRITICAL__", _STATUS_CRITICAL)
    .replace("__WARNING__", _STATUS_WARNING)
    .replace("__OK__", _STATUS_OK)
)

_BOUND_WORDS = {
    "volatility_target": "the volatility budget — the risk this instrument's own movement implies",
    "kelly_cap": "the Kelly cap — the measured edge does not justify the full risk budget",
    "concentration_cap": (
        "the concentration cap — no position may exceed the book divided by the number of "
        "positions the segments claim to carry at once (`A.107`)"
    ),
}

_TIER_WORDS = {
    GateTier.SESSION_LATCH: "session latch",
    GateTier.REGULATORY: "regulatory wall",
    GateTier.DERIVED: "derived limit",
    GateTier.SIZER: "sizer",
}


@dataclass(frozen=True, slots=True)
class SizingSurfaceState:
    """One worked decision, plus the session state the gate read."""

    measured_at: datetime
    session_date: date
    sized: SizedPosition | None
    verdict: RiskGateVerdict | None
    session_state: SessionRiskState | None
    daily_loss_limit: DailyLossLimit | None
    state_path: Path
    unavailable_reason: str = ""
    missing_inputs: tuple[str, ...] = field(default=())

    @property
    def has_decision(self) -> bool:
        return self.sized is not None and self.verdict is not None


def absent_sizing_surface_state(
    *,
    measured_at: datetime,
    session_date: date,
    state_path: Path,
    unavailable_reason: str,
    missing_inputs: tuple[str, ...] = (),
) -> SizingSurfaceState:
    """No decision could be worked — stated, and never rendered as a size of zero.

    "The sizer answered zero" and "the sizer could not be run" are different facts, and only one of
    them is a decision.
    """
    return SizingSurfaceState(
        measured_at=measured_at,
        session_date=session_date,
        sized=None,
        verdict=None,
        session_state=None,
        daily_loss_limit=None,
        state_path=state_path,
        unavailable_reason=unavailable_reason,
        missing_inputs=missing_inputs,
    )


def _rupees(value: Decimal) -> str:
    rounded = f"Rs {value:,.2f}"
    if value != 0 and Decimal(rounded.removeprefix("Rs ").replace(",", "")) == 0:
        return f"Rs {value:f}"
    return rounded


def _sigma_text(sized: SizedPosition) -> str:
    """Volatility in bps, rendered plainly — a tile is read at a glance, not parsed."""
    return str(sized.volatility.sigma_bps.quantize(Decimal("0.1")))


def _tiles(state: SizingSurfaceState) -> str:
    if not state.has_decision or state.sized is None or state.verdict is None:
        return (
            '<div class="tiles"><div class="tile"><div class="tile-value">no decision</div>'
            '<div class="tile-label">nothing has been sized on this page</div></div></div>'
        )
    sized = state.sized
    verdict = state.verdict
    badge = (
        '<span class="badge badge-ok">allowed</span>'
        if verdict.is_allowed
        else '<span class="badge badge-crit">refused</span>'
    )
    return f"""<div class="tiles">
<div class="tile"><div class="tile-value">{sized.quantity:,}</div>
<div class="tile-label">units — {sized.lots} lot(s) of {sized.lot_size}</div></div>
<div class="tile"><div class="tile-value">{escape(_rupees(sized.notional_rupees))}</div>
<div class="tile-label">notional, against
{escape(_rupees(sized.deployable_rupees))} deployable</div></div>
<div class="tile"><div class="tile-value">{escape(_sigma_text(sized))}</div>
<div class="tile-label">bps volatility over {sized.volatility.horizon_bars} bar(s)</div></div>
<div class="tile"><div class="tile-value">{badge}</div>
<div class="tile-label">{len(verdict.refusals)} refusal(s)</div></div>
</div>"""


def _how_the_size_was_reached(state: SizingSurfaceState) -> str:
    if state.sized is None:
        return ""
    sized = state.sized
    rows = [
        (
            "deployable capital",
            _rupees(sized.deployable_rupees),
            "what may be risked today — the live resolver in live, the paper ledger in paper",
        ),
        (
            "target risk fraction",
            str(sized.target_risk_fraction.quantize(Decimal("0.000001"))),
            "1 / concurrent position capacity. Not chosen: a structural fact of the segment set",
        ),
        ("risk budget", _rupees(sized.risk_budget_rupees), "capital x risk fraction"),
        (
            "realised volatility",
            f"{sized.volatility.sigma_bps.quantize(Decimal('0.01'))} bps",
            escape(sized.volatility.describe()),
        ),
        (
            "volatility budget notional",
            _rupees(sized.volatility_target_notional_rupees),
            "risk budget / volatility — the notional whose expected move IS the budget",
        ),
        (
            "calibrated edge",
            f"{sized.kelly.edge_bps} bps +/- {sized.kelly.standard_error_bps}",
            f"t = {sized.kelly.t_statistic.quantize(Decimal('0.01'))}",
        ),
        (
            "Kelly shrinkage",
            str(sized.kelly.shrinkage.quantize(Decimal("0.0001"))),
            "edge^2 / (edge^2 + se^2) — an edge measured badly sizes small by construction",
        ),
        (
            "Kelly cap notional",
            _rupees(sized.kelly_notional_rupees),
            "capital x shrunk Kelly fraction",
        ),
        (
            "concentration cap",
            _rupees(sized.concentration_cap_rupees),
            "deployable / concurrent capacity — neither bound above is a concentration limit, "
            "and on a quiet instrument both saturate. Does NOT know two instruments move "
            "together; that is L7.05 and it is still required",
        ),
        (
            "bound by",
            escape(sized.binding_bound),
            escape(_BOUND_WORDS.get(sized.binding_bound, "")),
        ),
        (
            "lot rounding",
            f"{sized.lots} x {sized.lot_size} = {sized.quantity}",
            "always rounded DOWN — a rounded-up lot is a leverage decision in disguise",
        ),
    ]
    body = "".join(
        f"<tr><td>{escape(name)}</td><td class='figure'>{escape(value)}</td>"
        f"<td class='reason'>{note}</td></tr>"
        for name, value, note in rows
    )
    return (
        '<div class="panel"><table><thead><tr><th>step</th><th class="figure">value</th>'
        f"<th>what it means</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def _refusals(state: SizingSurfaceState) -> str:
    if state.verdict is None:
        return ""
    verdict = state.verdict
    if verdict.is_allowed:
        return (
            '<div class="panel"><p class="sub">No rule refused this order. '
            "Limits applied: notional "
            f"{escape(_rupees(verdict.limits_applied.maximum_notional_rupees))}, "
            f"leverage {verdict.limits_applied.maximum_leverage.quantize(Decimal('0.01'))}x, "
            "collar "
            f"{(verdict.limits_applied.price_collar_fraction * 100).quantize(Decimal('0.01'))}%, "
            f"rate {verdict.limits_applied.maximum_orders_per_rate_window} per window.</p></div>"
        )
    rows = "".join(
        f"<tr><td>{escape(_TIER_WORDS.get(refusal.tier, refusal.tier.value))}</td>"
        f"<td>{escape(refusal.rule)}</td>"
        f"<td class='reason'>{escape(refusal.detail)}</td></tr>"
        for refusal in verdict.refusals
    )
    return (
        '<div class="panel"><table><thead><tr><th>tier</th><th>rule</th><th>why</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _unchecked_walls(state: SizingSurfaceState) -> str:
    if state.verdict is None or not state.verdict.unchecked_regulatory_sources:
        return ""
    sources = ", ".join(state.verdict.unchecked_regulatory_sources)
    return f"""<div class="note crit">
<span class="badge badge-crit">regulatory walls unchecked</span>
{escape(sources)} could not be read, so this decision does NOT know whether the instrument is
banned, at its market-wide position limit, or outside its circuit band. An unread source is
reported here rather than treated as a clear one: <strong>"not checked" and "no ban" are the same
value only to a system that then trades on the difference.</strong>
</div>"""


def _session_state(state: SizingSurfaceState) -> str:
    if state.session_state is None:
        return '<div class="panel"><p class="sub">No session risk state has been opened.</p></div>'
    session = state.session_state
    limit_text = (
        escape(state.daily_loss_limit.describe())
        if state.daily_loss_limit is not None
        else "not evaluated"
    )
    halted = (
        '<span class="badge badge-crit">HALTED</span>'
        if session.is_halted
        else '<span class="badge badge-ok">trading</span>'
    )
    return f"""<div class="panel"><table><tbody>
<tr><td>status</td><td class="figure">{halted}</td></tr>
<tr><td>equity</td><td class="figure">{escape(_rupees(session.current_equity_rupees))}</td></tr>
<tr><td>realised today</td>
<td class="figure">{escape(_rupees(session.realised_pnl_rupees))}</td></tr>
<tr><td>drawdown from peak</td><td class="figure">
{escape(_rupees(session.drawdown_rupees))}
({(session.drawdown_fraction * 100).quantize(Decimal('0.01'))}%)</td></tr>
<tr><td>open exposure</td>
<td class="figure">{escape(_rupees(session.open_exposure_rupees))}</td></tr>
<tr><td>orders in rate window</td>
<td class="figure">{session.orders_in_rate_window}</td></tr>
<tr><td>daily-loss limit</td><td class="figure">{limit_text}</td></tr>
</tbody></table></div>"""


def _unavailable(state: SizingSurfaceState) -> str:
    if state.has_decision or not state.unavailable_reason:
        return ""
    missing = (
        f" Missing inputs: <strong>{escape(', '.join(state.missing_inputs))}</strong>."
        if state.missing_inputs
        else ""
    )
    return (
        '<div class="note crit"><span class="badge badge-crit">no decision</span> '
        f"{escape(state.unavailable_reason)}{missing} Nothing is rendered as a size, because "
        '"the sizer answered zero" and "the sizer could not be run" are different facts and only '
        "one of them is a decision.</div>"
    )


def render_sizing_page(state: SizingSurfaceState) -> str:
    """The whole `/sizing` surface — self-contained and pure; no store is opened here."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sizing and risk gate</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Sizing and the risk gate — how much, or none</h1>
<p class="sub">Session {escape(state.session_date.isoformat())}, measured
{escape(state.measured_at.isoformat())}. Session risk state read from
<code>{escape(str(state.state_path))}</code>. Every figure below is recomputed from its inputs on
each request; nothing here is a status anybody maintains. Specification:
<code>docs/research/227</code>.</p>

{_unavailable(state)}
{_unchecked_walls(state)}

{_tiles(state)}

<h2>How the size was reached</h2>
<p class="sub">The size is <code>min(volatility budget, Kelly cap)</code>, floored to whole lots.
The volatility budget asks how much risk this instrument's own movement implies; the Kelly cap asks
whether the measured edge justifies that risk. Neither fraction is a chosen number.</p>
{_how_the_size_was_reached(state)}

<h2>What the gate said</h2>
<p class="sub">Every tier is evaluated even after one refuses, so "why was this refused" does not
depend on evaluation order. Regulatory walls are published facts and no lower tier may override
them; the operator key only tightens.</p>
{_refusals(state)}

<h2>The session the gate read</h2>
<p class="sub">Read-only. A latch is tripped by the trading loop, never by loading this page: a read
that halts is a read nobody can safely perform to find out where they stand.</p>
{_session_state(state)}
</body></html>"""
