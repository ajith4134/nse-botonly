"""`/paper-session` — what the paper loop actually did, measured from the stores it wrote (`R.08`).

`F04` is the first thing in this rebuild that produces a trading day rather than a component, and a
day nobody can read is a day nobody can argue with. This surface is therefore not a summary someone
maintains: every figure is folded, on each request, out of the three stores the session itself
wrote — the intent journal (what was ordered and what filled), the paper capital ledger (what was
reserved, realised and released) and the session risk state (exposure, realised P&L and latches).

**It never states a number the stores cannot produce.** A session directory with no journal is
reported as absent rather than as a day with no trades: "nothing happened" and "nothing was
recorded" are the same picture only to a reader who is being misled.

**GET never writes.** The stores are opened read-only, and no latch is tripped, cleared or
re-derived by loading this page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

DEFAULT_PAPER_SESSION_ROOT = Path("~/.nse_algo_trader/paper_verification").expanduser()

_STATUS_CRITICAL = "#b3261e"
_STATUS_WARNING = "#8a6100"
_STATUS_OK = "#1f6b3a"

_PAGE_CSS = """
:root{ --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc; --ink:#1b1b19;
       --ink-soft:#5f5e59; }
*{box-sizing:border-box}
body{margin:0;padding:2rem;background:var(--surface-0);color:var(--ink);
     font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1.05rem;margin:2rem 0 .5rem;padding-top:1rem;border-top:1px solid var(--border)}
p.sub{color:var(--ink-soft);margin:.25rem 0 1rem;max-width:70ch}
code{background:var(--surface-1);border:1px solid var(--border);border-radius:3px;padding:0 .25em}
.tiles{display:flex;flex-wrap:wrap;gap:.75rem;margin:1rem 0}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:6px;
      padding:.75rem 1rem;min-width:11rem}
.tile .label{color:var(--ink-soft);font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.tile .value{font-size:1.25rem;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;background:var(--surface-1);
      border:1px solid var(--border);border-radius:6px;overflow:hidden}
th,td{padding:.45rem .7rem;text-align:left;border-bottom:1px solid var(--border);
      font-variant-numeric:tabular-nums}
th{background:var(--surface-0);font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:none}
.absent{background:#fff6f4;border:1px solid #f0c9c1;border-left:4px solid #b3261e;
        padding:.75rem 1rem;border-radius:4px;margin:1rem 0}
"""


@dataclass(frozen=True, slots=True)
class PaperOrderRow:
    """One order as the journal holds it — never as anything recomputed it."""

    intent_id: str
    trading_symbol: str
    side: str
    state: str
    ordered_quantity: int
    filled_quantity: int
    average_price_paise: Decimal | None
    created_at: str


@dataclass(frozen=True, slots=True)
class PaperSessionSurfaceState:
    """One replayed session, folded from the three stores it wrote."""

    measured_at: datetime
    session_date: date | None
    session_directory: Path
    orders: tuple[PaperOrderRow, ...] = ()
    ledger_balance_rupees: Decimal | None = None
    ledger_committed_rupees: Decimal | None = None
    realised_profit_rupees: Decimal = Decimal(0)
    realised_loss_rupees: Decimal = Decimal(0)
    cost_debit_rupees: Decimal = Decimal(0)
    tripped_latches: tuple[str, ...] = ()
    available_sessions: tuple[date, ...] = ()
    unavailable_reason: str = ""
    missing_stores: tuple[str, ...] = field(default=())

    @property
    def has_session(self) -> bool:
        return self.session_date is not None and not self.unavailable_reason

    @property
    def net_realised_rupees(self) -> Decimal:
        return self.realised_profit_rupees - self.realised_loss_rupees - self.cost_debit_rupees

    @property
    def filled_orders(self) -> int:
        return sum(1 for order in self.orders if order.filled_quantity > 0)


def absent_paper_session_state(
    *,
    measured_at: datetime,
    session_directory: Path,
    unavailable_reason: str,
    missing_stores: tuple[str, ...] = (),
    available_sessions: tuple[date, ...] = (),
) -> PaperSessionSurfaceState:
    """No session could be read — stated as an absence, never drawn as a quiet day."""
    return PaperSessionSurfaceState(
        measured_at=measured_at,
        session_date=None,
        session_directory=session_directory,
        unavailable_reason=unavailable_reason,
        missing_stores=missing_stores,
        available_sessions=available_sessions,
    )


def recorded_paper_sessions(root: Path = DEFAULT_PAPER_SESSION_ROOT) -> tuple[date, ...]:
    """Every session directory that exists, newest first. Directory names ARE the dates."""
    if not root.exists():
        return ()
    sessions: list[date] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            sessions.append(date.fromisoformat(child.name))
        except ValueError:
            continue
    return tuple(sorted(sessions, reverse=True))


def read_paper_session_state(
    *,
    measured_at: datetime,
    session_date: date | None = None,
    root: Path = DEFAULT_PAPER_SESSION_ROOT,
) -> PaperSessionSurfaceState:
    """Fold one session out of its own stores, or say which store could not answer."""
    sessions = recorded_paper_sessions(root)
    if not sessions:
        return absent_paper_session_state(
            measured_at=measured_at,
            session_directory=root,
            unavailable_reason=(
                f"no paper session has been recorded under {root} — run "
                "scripts/verify_paper_session_on_real_data.py to produce one"
            ),
            missing_stores=("paper_session_directory",),
        )
    chosen = session_date if session_date in sessions else sessions[0]
    directory = root / chosen.isoformat()
    journal_path = directory / "journal.sqlite3"
    ledger_path = directory / "ledger.sqlite3"
    risk_path = directory / "risk.sqlite3"
    missing = tuple(
        name
        for name, path in (
            ("order_intent_journal", journal_path),
            ("paper_capital_ledger", ledger_path),
            ("session_risk_state", risk_path),
        )
        if not path.exists()
    )
    if "order_intent_journal" in missing:
        return absent_paper_session_state(
            measured_at=measured_at,
            session_directory=directory,
            unavailable_reason=(
                f"{chosen.isoformat()} has no intent journal, so no order can be read"
            ),
            missing_stores=missing,
            available_sessions=sessions,
        )

    orders = _orders_from_journal(journal_path)
    balance, committed = _ledger_position(ledger_path)
    profit, loss, costs = _ledger_realisations(ledger_path)
    latches = _tripped_latches(risk_path, chosen)
    return PaperSessionSurfaceState(
        measured_at=measured_at,
        session_date=chosen,
        session_directory=directory,
        orders=orders,
        ledger_balance_rupees=balance,
        ledger_committed_rupees=committed,
        realised_profit_rupees=profit,
        realised_loss_rupees=loss,
        cost_debit_rupees=costs,
        tripped_latches=latches,
        available_sessions=sessions,
        missing_stores=missing,
    )


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _orders_from_journal(path: Path) -> tuple[PaperOrderRow, ...]:
    """Orders and their fills, folded by the journal's own tables."""
    # `price_paise` is stored as TEXT because a rate multiplies every trade and float cannot hold
    # it exactly, so the weighted average is folded in Python rather than by SQL's SUM.
    with _read_only(path) as connection:
        intents = connection.execute(
            "SELECT intent_id, trading_symbol, side, quantity, decided_at FROM order_intent "
            "ORDER BY decided_at"
        ).fetchall()
        fills: dict[str, list[tuple[int, Decimal]]] = {}
        for fill in connection.execute(
            "SELECT intent_id, quantity, price_paise FROM order_fill WHERE superseded_by = ''"
        ):
            fills.setdefault(str(fill["intent_id"]), []).append(
                (int(fill["quantity"]), Decimal(str(fill["price_paise"])))
            )
        latest_event = {
            str(row["intent_id"]): str(row["event"])
            for row in connection.execute(
                "SELECT intent_id, event FROM order_event ORDER BY event_id"
            )
        }
    rows: list[PaperOrderRow] = []
    for intent in intents:
        intent_id = str(intent["intent_id"])
        legs = fills.get(intent_id, [])
        filled = sum(quantity for quantity, _ in legs)
        value = sum(Decimal(quantity) * price for quantity, price in legs)
        rows.append(
            PaperOrderRow(
                intent_id=intent_id,
                trading_symbol=str(intent["trading_symbol"]),
                side=str(intent["side"]),
                state=_state_from(
                    ordered=int(intent["quantity"]),
                    filled=filled,
                    last_event=latest_event.get(intent_id, "recorded"),
                ),
                ordered_quantity=int(intent["quantity"]),
                filled_quantity=filled,
                average_price_paise=(value / Decimal(filled)) if filled > 0 else None,
                created_at=str(intent["decided_at"]),
            )
        )
    return tuple(rows)


def _state_from(*, ordered: int, filled: int, last_event: str) -> str:
    """What the order IS, from the journal's own rows rather than from its last event name.

    The lifecycle event log records transitions the state machine made; a fill does not always
    produce one, because `record_fill` moves the order itself. Reading the last event alone showed
    a fully filled order as "opened" — true of the last transition recorded, and misleading as a
    statement about the order. Quantities are the ground truth here, and the last event is kept for
    the orders no fill has touched.
    """
    if filled <= 0:
        return last_event
    if filled >= ordered:
        return "filled"
    return "partially filled"


def _ledger_position(path: Path) -> tuple[Decimal | None, Decimal | None]:
    """Balance and open commitments, from the ledger's own checkpoint row."""
    if not path.exists():
        return None, None
    # Amounts are stored as TEXT because they are exact rupees, so the fold happens in Decimal
    # here rather than in SQL. `CAST(... AS REAL)` did it once, and a book that had released
    # every commitment reported "Rs -0.00 still committed" — a float residue of about 1e-10
    # displayed as a defect.
    with _read_only(path) as connection:
        row = connection.execute(
            "SELECT resulting_balance_rupees FROM paper_capital_event "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        committed = Decimal(0)
        for event in connection.execute(
            "SELECT kind, amount_rupees FROM paper_capital_event "
            "WHERE kind IN ('POSITION_COMMIT', 'POSITION_RELEASE')"
        ):
            amount = Decimal(str(event["amount_rupees"]))
            committed += amount if str(event["kind"]) == "POSITION_COMMIT" else -amount
    balance = Decimal(str(row["resulting_balance_rupees"])) if row is not None else None
    return balance, committed


def _ledger_realisations(path: Path) -> tuple[Decimal, Decimal, Decimal]:
    """Profit, loss and cost, kept apart — `L1.11` cannot attribute a result that was folded."""
    if not path.exists():
        return Decimal(0), Decimal(0), Decimal(0)
    totals = {"REALISED_PROFIT": Decimal(0), "REALISED_LOSS": Decimal(0), "COST_DEBIT": Decimal(0)}
    with _read_only(path) as connection:
        for row in connection.execute(
            "SELECT kind, amount_rupees FROM paper_capital_event"
        ):
            kind = str(row["kind"])
            if kind in totals:
                totals[kind] += Decimal(str(row["amount_rupees"]))
    return totals["REALISED_PROFIT"], totals["REALISED_LOSS"], totals["COST_DEBIT"]


def _tripped_latches(path: Path, session_date: date) -> tuple[str, ...]:
    if not path.exists():
        return ()
    with _read_only(path) as connection:
        rows = connection.execute(
            "SELECT latch, reason FROM session_risk_latch WHERE session_date = ?",
            (session_date.isoformat(),),
        ).fetchall()
    return tuple(f"{row['latch']}: {row['reason']}" for row in rows)


def _paise(value: Decimal | None) -> str:
    """A price to the paise. The unrounded quotient of a volume-weighted walk has twenty-eight
    significant figures and none of them past the second mean anything to a reader."""
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01'))}"


def _rupees(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"Rs {value.quantize(Decimal('0.01'))}"


def _tiles(state: PaperSessionSurfaceState) -> str:
    if not state.has_session:
        return ""
    net = state.net_realised_rupees
    colour = _STATUS_OK if net >= 0 else _STATUS_CRITICAL
    tiles = [
        ("Session", escape(state.session_date.isoformat()) if state.session_date else "—", None),
        ("Orders", str(len(state.orders)), None),
        ("Orders filled", str(state.filled_orders), None),
        ("Net realised", _rupees(net), colour),
        ("Costs", _rupees(state.cost_debit_rupees), None),
        ("Ledger balance", _rupees(state.ledger_balance_rupees), None),
        ("Still committed", _rupees(state.ledger_committed_rupees), None),
    ]
    rendered = "".join(
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value"{f" style=\'color:{colour}\'" if colour else ""}>{value}</div></div>'
        for label, value, colour in tiles
    )
    return f'<div class="tiles">{rendered}</div>'


def _orders_table(state: PaperSessionSurfaceState) -> str:
    if not state.orders:
        return (
            '<div class="absent">This session recorded no order. That is a result — the loop ran '
            "and every decision was refused or abstained — and it is NOT the same as a session "
            "that was never run.</div>"
        )
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(order.intent_id[:12])}</code></td>"
        f"<td>{escape(order.trading_symbol)}</td>"
        f"<td>{escape(order.side)}</td>"
        f"<td>{escape(order.state)}</td>"
        f"<td>{order.filled_quantity}/{order.ordered_quantity}</td>"
        f"<td>{_paise(order.average_price_paise)}</td>"
        f"<td>{escape(order.created_at)}</td>"
        "</tr>"
        for order in state.orders
    )
    return (
        "<table><tr><th>Intent</th><th>Symbol</th><th>Side</th><th>State</th><th>Filled</th>"
        f"<th>Average paise</th><th>Created</th></tr>{rows}</table>"
    )


def _latches(state: PaperSessionSurfaceState) -> str:
    if not state.tripped_latches:
        return '<p class="sub">No latch tripped in this session.</p>'
    items = "".join(f"<li>{escape(latch)}</li>" for latch in state.tripped_latches)
    return f'<div class="absent"><strong>Latches tripped</strong><ul>{items}</ul></div>'


def _unavailable(state: PaperSessionSurfaceState) -> str:
    if not state.unavailable_reason:
        return ""
    missing = (
        f" Stores that could not answer: <code>{escape(', '.join(state.missing_stores))}</code>."
        if state.missing_stores
        else ""
    )
    return f'<div class="absent">{escape(state.unavailable_reason)}.{missing}</div>'


def render_paper_session_page(state: PaperSessionSurfaceState) -> str:
    """The whole `/paper-session` surface — pure; every store was read by the caller."""
    others = (
        ", ".join(session.isoformat() for session in state.available_sessions)
        if state.available_sessions
        else "none"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper trading session</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>The paper session — a day that ran itself</h1>
<p class="sub">Measured {escape(state.measured_at.isoformat())} from
<code>{escape(str(state.session_directory))}</code>. Every figure is folded out of the stores the
session itself wrote — the intent journal, the paper capital ledger and the session risk state — on
each request. Nothing here is a status anybody maintains. Sessions recorded:
<code>{escape(others)}</code>. Specification: <code>docs/research/228</code>.</p>

{_unavailable(state)}
{_tiles(state)}

<h2>Every order the session sent</h2>
<p class="sub">Both legs of every position: the entry the sizer and gate allowed, and the square-off
that took it flat before the close (`R.01`). A fill price is the volume-weighted walk of the book
that was actually recorded at that instant — never a modelled slippage.</p>
{_orders_table(state)}

<h2>Risk latches</h2>
<p class="sub">Read-only. The loop can halt itself and can never un-halt itself (`R.22`); clearing a
latch is an operator act and does not happen from this page.</p>
{_latches(state)}
</body></html>"""
