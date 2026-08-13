"""`/paper-capital` — the paper trading book's money, and the one control that changes it.

`R.08` is satisfied here rather than promised: the ledger is visible from the day it exists, and the
figure on this page is FOLDED out of the event log on every request rather than read from anything a
human maintains.

The page carries one write control, which is the operator's actual request — the virtual currency is
editable to whatever they wish. Three properties make that safe to expose:

* The edit **appends**; it never overwrites. The full history is on the page underneath the control,
  so a figure that changed is a row, not a mystery.
* An edit above the live ceiling is **stamped, not blocked** (`docs/research/226` §3), because
  `R.03` requires every engine to be exercisable from a lakh to a crore, and results made on money
  the operator cannot fund must be marked as unachievable at the moment they are read.
* **GET never writes.** Rendering this page opens the ledger read-only and creates nothing; the
  ledger is seeded only by an explicit POST. A dashboard that creates a database on page load is a
  side effect nobody asked for — the same rule `/orders` follows.

The page also states, in plain words, why a real account in debit does not stop the paper book: the
live resolver and this ledger are different money, and only one of them is the broker's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.paper_capital_ledger import (
    PaperCapitalError,
    PaperCapitalEvent,
    PaperCapitalEventKind,
    PaperCapitalLedger,
    PaperCapitalSnapshot,
)

RECENT_EVENT_LIMIT = 60

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
form.edit{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:16px 18px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;}
form.edit label{display:flex;flex-direction:column;gap:4px;color:var(--text-secondary);}
form.edit input{font:inherit;padding:7px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--surface-0);color:var(--text-primary);min-width:220px;}
form.edit button{font:inherit;padding:8px 18px;border:1px solid var(--border);border-radius:6px;
  background:var(--text-primary);color:var(--surface-0);cursor:pointer;font-weight:700;}
"""

_PAGE_CSS = (
    _PAGE_CSS.replace("__CRITICAL__", _STATUS_CRITICAL)
    .replace("__WARNING__", _STATUS_WARNING)
    .replace("__OK__", _STATUS_OK)
)

_BINDING_SIDE_WORDS = {
    "paper_free_capital": "free capital available",
    "paper_exhausted": "the book is blown — balance is zero",
    "paper_fully_committed": "every rupee is committed to an open position",
    "paper_over_committed": "commitments exceed the balance the operator set",
}

_EVENT_KIND_WORDS = {
    PaperCapitalEventKind.SEED: "seeded from the operator ceiling",
    PaperCapitalEventKind.OPERATOR_SET: "operator set the balance",
    PaperCapitalEventKind.OPERATOR_ADJUST: "operator adjusted the balance",
    PaperCapitalEventKind.POSITION_COMMIT: "capital committed to a position",
    PaperCapitalEventKind.POSITION_RELEASE: "commitment released",
    PaperCapitalEventKind.REALISED_PROFIT: "simulated exit in profit",
    PaperCapitalEventKind.REALISED_LOSS: "simulated exit at a loss",
    PaperCapitalEventKind.COST_DEBIT: "simulated charges",
}


@dataclass(frozen=True, slots=True)
class PaperCapitalSurfaceState:
    """Everything the page renders, measured before rendering and never during it."""

    measured_at: datetime
    ledger_path: Path
    snapshot: PaperCapitalSnapshot | None
    recent_events: tuple[PaperCapitalEvent, ...]
    total_event_count: int
    live_ceiling_rupees: Decimal | None
    unavailable_reason: str
    refusal: str = ""
    """What the ledger REFUSED, rendered over the book's real state rather than instead of it.

    The `R.23(c)` review posted a typo into the amount field against a funded ledger and the page
    came back saying "not seeded", "the paper book has no capital yet", "the ledger holds no events"
    and "the operator ceiling reads unreadable" — none of which was true. A refusal must not erase
    the thing it refused to change.
    """

    @property
    def is_seeded(self) -> bool:
        return self.snapshot is not None


def build_paper_capital_surface_state(
    ledger: PaperCapitalLedger,
    *,
    measured_at: datetime,
    live_ceiling_rupees: Decimal,
    ledger_path: Path,
    recent_event_limit: int = RECENT_EVENT_LIMIT,
) -> PaperCapitalSurfaceState:
    """Fold the ledger into a page state, or record why it could not be folded.

    An unseeded ledger is not an error and is not rendered as a zero: it is rendered as an empty
    state carrying the control that seeds it, because "no ledger yet" and "a ledger holding nothing"
    are different facts and only one of them means the operator has not started.
    """
    events = ledger.events()
    try:
        snapshot: PaperCapitalSnapshot | None = ledger.snapshot(
            measured_at=measured_at,
            live_ceiling=TradingCapital.of_rupees(live_ceiling_rupees),
        )
        reason = ""
    except PaperCapitalError as failure:
        snapshot = None
        reason = str(failure)
    recent = events[-recent_event_limit:] if recent_event_limit > 0 else ()
    return PaperCapitalSurfaceState(
        measured_at=measured_at,
        ledger_path=ledger_path,
        snapshot=snapshot,
        recent_events=tuple(reversed(recent)),
        total_event_count=len(events),
        live_ceiling_rupees=live_ceiling_rupees,
        unavailable_reason=reason,
    )


def absent_paper_capital_surface_state(
    *,
    measured_at: datetime,
    ledger_path: Path,
    live_ceiling_rupees: Decimal | None,
    unavailable_reason: str,
    refusal: str = "",
) -> PaperCapitalSurfaceState:
    """No ledger file, or no readable operator ceiling — stated, never rendered as zero."""
    return PaperCapitalSurfaceState(
        measured_at=measured_at,
        ledger_path=ledger_path,
        snapshot=None,
        recent_events=(),
        total_event_count=0,
        live_ceiling_rupees=live_ceiling_rupees,
        unavailable_reason=unavailable_reason,
        refusal=refusal,
    )


def with_refusal(state: PaperCapitalSurfaceState, refusal: str) -> PaperCapitalSurfaceState:
    """The same page, with what the ledger refused stated on top of it.

    Used by the POST route so a rejected edit renders the book AS IT STANDS plus the reason, rather
    than a page that reports an empty ledger the operator does not have.
    """
    return replace(state, refusal=refusal)


def _rupees(value: Decimal) -> str:
    """Rupees, and never a zero that is not zero.

    Two decimals is what an operator reads, but the review set a balance of `0.005` and the page
    reported `Rs 0.00` in both the balance and the free tile while the badge still read TRADEABLE.
    A displayed zero beside a green badge is a contradiction the reader has to resolve; the exact
    figure is shown instead whenever rounding would hide a non-zero amount.
    """
    rounded = f"Rs {value:,.2f}"
    if value != 0 and Decimal(rounded.removeprefix("Rs ").replace(",", "")) == 0:
        return f"Rs {value:f}"
    return rounded


def _tiles(state: PaperCapitalSurfaceState) -> str:
    snapshot = state.snapshot
    if snapshot is None:
        return (
            '<div class="tiles"><div class="tile"><div class="tile-value">not seeded</div>'
            '<div class="tile-label">the paper book has no capital yet</div></div></div>'
        )
    badge = (
        '<span class="badge badge-ok">tradeable</span>'
        if snapshot.is_tradeable
        else '<span class="badge badge-crit">not tradeable</span>'
    )
    return f"""<div class="tiles">
<div class="tile"><div class="tile-value">{escape(_rupees(snapshot.free_rupees))}</div>
<div class="tile-label">free — what the paper book may size against now</div></div>
<div class="tile"><div class="tile-value">{escape(_rupees(snapshot.balance_rupees))}</div>
<div class="tile-label">balance — folded from {state.total_event_count} events</div></div>
<div class="tile"><div class="tile-value">{escape(_rupees(snapshot.committed_rupees))}</div>
<div class="tile-label">committed to {len(snapshot.open_commitments)} open position(s)</div></div>
<div class="tile"><div class="tile-value">{badge}</div>
<div class="tile-label">{escape(_BINDING_SIDE_WORDS[snapshot.binding_side])}</div></div>
</div>"""


def _why_the_real_debit_does_not_matter(state: PaperCapitalSurfaceState) -> str:
    ceiling = (
        escape(_rupees(state.live_ceiling_rupees))
        if state.live_ceiling_rupees is not None
        else "unreadable"
    )
    return f"""<div class="note">
<strong>This money is not the broker's money.</strong> The live sizing figure comes from
<code>deployable_capital_resolver</code>, which measures the real Zerodha balance and floors it at
zero — so a real account in debit correctly reports nothing deployable and refuses to size a live
order. <strong>That resolver has no authority over this page.</strong> The paper trading book runs
on the virtual capital below, so a real debit, a real loss or an unpaid brokerage charge cannot stop
paper trading. What a real debit CAN eventually stop is broker API access, which starves the live
tick feed that paper also consumes; that is an account-access matter, not a capital matter.
The operator ceiling for LIVE sizing currently reads {ceiling}.
</div>"""


def _ceiling_stamp(state: PaperCapitalSurfaceState) -> str:
    snapshot = state.snapshot
    if snapshot is None or not snapshot.exceeds_live_ceiling:
        return ""
    return f"""<div class="note warn">
<span class="badge badge-warn">exceeds the live ceiling</span>
The paper book is running <strong>{escape(_rupees(snapshot.balance_rupees))}</strong> against a live
ceiling of <strong>{escape(_rupees(snapshot.live_ceiling_rupees))}</strong>. This is allowed on
purpose — <code>R.03</code> requires every engine to be exercisable from a lakh to a crore, and that
cannot be done if the paper book is pinned to today's funding. It is stamped rather than blocked so
that any record produced in this regime is known to be <strong>not achievable</strong> with the
capital the operator can currently fund, at the moment it is read rather than at arming time.
</div>"""


def _over_committed_stamp(state: PaperCapitalSurfaceState) -> str:
    snapshot = state.snapshot
    if snapshot is None or not snapshot.over_committed:
        return ""
    return f"""<div class="note crit">
<span class="badge badge-crit">over-committed</span>
Open positions hold {escape(_rupees(snapshot.committed_rupees))} against a balance of
{escape(_rupees(snapshot.balance_rupees))}. The edit that caused this was accepted rather than
refused, because the operator must always be able to state the truth about their virtual capital.
Free capital reads zero and every new commitment is refused until positions close.
</div>"""


def _edit_form(state: PaperCapitalSurfaceState) -> str:
    verb = "Set the paper capital" if state.is_seeded else "Seed the paper book"
    return f"""<form class="edit" method="post" action="/paper-capital">
<label>virtual capital (rupees)
<input name="balance_rupees" inputmode="decimal" placeholder="1000000" required></label>
<label>reason — recorded with the event, and refused if blank
<input name="reason" placeholder="why this figure" required></label>
<button type="submit">{escape(verb)}</button>
</form>"""


def _commitments_section(state: PaperCapitalSurfaceState) -> str:
    snapshot = state.snapshot
    if snapshot is None or not snapshot.open_commitments:
        return '<div class="panel"><p class="sub">No open commitments.</p></div>'
    rows = "".join(
        f"<tr><td>{escape(key)}</td><td class='figure'>{escape(_rupees(amount))}</td></tr>"
        for key, amount in snapshot.open_commitments
    )
    return (
        '<div class="panel"><table><thead><tr><th>position key</th>'
        "<th class='figure'>reserved</th></tr></thead><tbody>"
        f"{rows}</tbody></table></div>"
    )


_LEDGER_COLUMNS = (
    "#",
    "at",
    "what",
    "amount",
    "balance before",
    "balance after",
    "unfunded",
    "position",
    "reason",
)


def _event_row(event: PaperCapitalEvent) -> str:
    """One ledger row, built as a list of cells rather than a chain of f-strings.

    It was written the other way first, and a line-wrap turned the `unfunded` cell's conditional
    into a ternary over the WHOLE concatenated row: every event with no shortfall rendered two cells
    instead of nine. Every test still passed, because they asserted that substrings were PRESENT and
    a two-cell row still contains them. The screenshot caught it. Cells are a list now, and
    `test_every_ledger_row_carries_one_cell_per_column` counts them.
    """
    shortfall = (
        escape(_rupees(event.unfunded_shortfall_rupees)) if event.unfunded_shortfall_rupees else "—"
    )
    cells = (
        f"<td class='figure'>{event.sequence}</td>",
        f"<td>{escape(event.occurred_at.isoformat())}</td>",
        f"<td>{escape(_EVENT_KIND_WORDS[event.kind])}</td>",
        f"<td class='figure'>{escape(_rupees(event.amount_rupees))}</td>",
        f"<td class='figure'>{escape(_rupees(event.previous_balance_rupees))}</td>",
        f"<td class='figure'>{escape(_rupees(event.resulting_balance_rupees))}</td>",
        f"<td class='figure'>{shortfall}</td>",
        f"<td>{escape(event.position_key) or '—'}</td>",
        f"<td class='reason'>{escape(event.reason)}</td>",
    )
    return f"<tr>{''.join(cells)}</tr>"


def _event_rows(state: PaperCapitalSurfaceState) -> str:
    if not state.recent_events:
        return (
            '<div class="panel"><p class="sub">The ledger holds no events. Nothing has been '
            "seeded, set or traded.</p></div>"
        )
    rows = "".join(_event_row(event) for event in state.recent_events)
    headers = "".join(f"<th>{escape(column)}</th>" for column in _LEDGER_COLUMNS)
    return (
        f'<div class="panel"><table><thead><tr>{headers}</tr></thead><tbody>'
        f"{rows}</tbody></table></div>"
    )


def _truncation_note(state: PaperCapitalSurfaceState) -> str:
    """`R.11` — a bounded view says what it dropped, or it reads as complete when it is not."""
    shown = len(state.recent_events)
    if shown >= state.total_event_count:
        return ""
    return (
        f'<p class="sub">Showing the most recent {shown} of {state.total_event_count} events. '
        f"The earlier {state.total_event_count - shown} are in the ledger at "
        f"<code>{escape(str(state.ledger_path))}</code> and are not shown here — the balance above "
        "is folded from ALL of them, not from the rows displayed.</p>"
    )


def _refusal_note(state: PaperCapitalSurfaceState) -> str:
    """What the ledger refused, over the book as it actually stands."""
    if not state.refusal:
        return ""
    return (
        '<div class="note crit"><span class="badge badge-crit">edit refused</span> '
        f"{escape(state.refusal)} <strong>Nothing below has changed.</strong></div>"
    )


def _unavailable_note(state: PaperCapitalSurfaceState) -> str:
    if state.is_seeded or not state.unavailable_reason:
        return ""
    return (
        f'<div class="note crit"><span class="badge badge-crit">no balance</span> '
        f"{escape(state.unavailable_reason)}</div>"
    )


def render_paper_capital_page(state: PaperCapitalSurfaceState) -> str:
    """The whole `/paper-capital` surface — self-contained and pure; no ledger access happens."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper capital</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Paper capital — the trading book's virtual money</h1>
<p class="sub">Folded from the append-only ledger at
<code>{escape(str(state.ledger_path))}</code> at {escape(state.measured_at.isoformat())}. Every
figure on this page is replayed from the event log on each request and checked against the stored
checkpoint; if the two ever disagree, this page refuses to show a number rather than showing the
cheaper one. Specification: <code>docs/research/226</code>.</p>

{_why_the_real_debit_does_not_matter(state)}
{_refusal_note(state)}
{_unavailable_note(state)}
{_ceiling_stamp(state)}
{_over_committed_stamp(state)}

{_tiles(state)}

<h2>Set the virtual capital</h2>
<p class="sub">Any positive figure is accepted, including one above the live ceiling — that case is
stamped above rather than blocked. The edit appends an event; it does not overwrite the history, and
the balance that sized any past paper trade stays recoverable. A blank reason is refused at write
time, because an unexplained balance change is indistinguishable from a defect when it is read back
months later.</p>
{_edit_form(state)}

<h2>Open commitments — capital reserved by positions that have not closed</h2>
<p class="sub">Committed capital reduces what may be sized against without moving the balance. The
alternative — decrementing the balance on entry and crediting P&amp;L back on exit — leaves the
capital invisible between the two, and a second signal then sizes against money already
deployed.</p>
{_commitments_section(state)}

<h2>The ledger — every movement, in the order it happened</h2>
{_truncation_note(state)}
{_event_rows(state)}
</body></html>"""
