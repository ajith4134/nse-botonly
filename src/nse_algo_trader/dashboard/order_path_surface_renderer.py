"""`F02` surface — every intent this system decided, and what actually became of it.

The page exists to answer one question an operator cannot answer any other way: **is what this
system believes about its orders the same as what the broker did?** Everything on it is measured
from the write-ahead journal (`L3.02`) and from the last reconciliation report (`L3.03`); nothing
is typed in, and nothing is a status a human maintains by hand.

Six properties are load-bearing, and each one is a way this page could lie while looking healthy:

* **An absent average fill price renders as ABSENT, never as zero.** `OrderRecord` returns `None`
  when nothing has filled, precisely so a zero cost basis can never enter the book, and a surface
  that formatted that `None` as `0.00` would put the lie back on the screen where a human reads it.
* **Anything INFERRED looks inferred.** A quantity gap patched from the broker's own average, and a
  submission reconstructed from a fill that arrived without one, are this system's inventions used
  to explain a gap. They wear `badge-absent` — no fill, a dashed edge, muted ink — the same
  vocabulary `/costs` gives `UNPRICEABLE`, because both are the absence of an observation rather
  than a bad one. A reader must never mistake something the system invented for something it saw.
* **The visibility horizon says when it is not established.** Until enough appearance delays have
  been observed, the reconciler refuses to declare any order abandoned. That refusal is a fact the
  operator needs — "no order will be declared abandoned yet" — and a blank cell would read as a
  horizon of zero, which is the opposite claim.
* **The open blocker is at the top, not in a footnote.** `F02`'s `R.05` real-FILL probe is deferred
  by operator decision (`docs/BACKLOG.md`, `F02` order path). Every claim below about the fill path
  therefore rests on a hermetic harness. A page that omitted that would let a feature LOOK finished
  while its largest verification is still open.
* **An order this build cannot read back is a ROW, not a 500 and not a gap.** The journal's fold
  can raise on a single damaged or unrecognised row, and this page used to let that raise take the
  whole surface with it — including the in-flight queue and the inferred ledger, which is what an
  operator needs at exactly that moment (`M5`). Every intent is now folded behind its own guard and
  an unfoldable one draws a `COULD NOT BE READ` row carrying the exception. It is not dropped
  either: a missing row would say, with this page's full authority, that the order does not exist.
* **The exchange's algo identifier says whether it is being sent.** The venue omits `algo_id` when
  the operator has configured none, which is right — inventing one would be far worse. But the
  omission had no reader anywhere in the system (`M6`), so it is stated at the top of this page,
  with the circular that requires it, as an operator action rather than as a footnote.

Counts are never carried as text: the lifecycle census, the verdict tallies and the disagreement
list are folded out of the objects passed in, so a state that appears tomorrow appears here, and a
verdict that stops occurring drops to zero rather than lingering as a sentence somebody wrote once.

Pure renderer, in the shape `/costs` established: `render_order_path_page` takes a frozen state and
returns one self-contained HTML document. It opens no database and reconciles nothing —
`build_order_path_surface_state` reads the journal once, so a page refresh can never place, patch
or abandon an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from html import escape
from pathlib import Path

from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import PAISE_PER_RUPEE
from nse_algo_trader.order_path.broker_truth_reconciler import (
    OrderReconciliation,
    ReconciliationReport,
    ReconciliationVerdict,
    VisibilityHorizon,
    visibility_horizon_from_observed_delays,
)
from nse_algo_trader.order_path.defensive_session_order_fold import (
    UnreadableOrder,
    fold_session_orders_defensively,
)
from nse_algo_trader.order_path.kite_order_execution_venue import (
    EXCHANGE_ALGO_IDENTIFIER_ENV_VAR,
)
from nse_algo_trader.order_path.order_intent_journal import (
    InFlightSubmission,
    OrderIntentJournal,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    TERMINAL_STATES,
    EventSource,
    OrderLifecycleState,
)
from nse_algo_trader.order_path.order_record import FillRecord, OrderExpression, OrderRecord

_STATUS_CRITICAL = "#c0392b"
_STATUS_WARNING = "#fab219"
_STATUS_GOOD = "#0ca30c"
"""The same reserved status palette `/costs` uses, and used for the same one job: status.

Deliberately not a second visual language. An operator moving between `/costs` and `/orders`
must not have to relearn what a colour means, and every badge here carries a WORD as well as a
colour so the status survives a colourblind reader and a printout.
"""

_ABSENT_MARK = "—"
"""What is printed where there is nothing. Never `0`, and never an empty cell.

An empty cell is ambiguous between "nothing happened" and "this page failed to render it", and a
zero is a third claim entirely: it asserts a measurement whose value happens to be nothing.
"""

_PAISE_PER_RUPEE = Decimal(PAISE_PER_RUPEE)
"""The currency's own subdivision, taken from where the rest of the system already keeps it.

Not re-declared here: a second copy of a conversion factor is a second place for it to be wrong.
"""

_DISPLAY_PAISE_PRECISION = Decimal(1).scaleb(-len(str(PAISE_PER_RUPEE)))
"""How finely a paise figure is PRINTED — a display rounding, and labelled as one.

Derived from the subdivision rather than typed: two more decimal places than a rupee has paise,
which is finer than any price this exchange quotes, so nothing a reader compares against can move.
"""

_RUPEE_DISPLAY_PRECISION = Decimal(1) / _PAISE_PER_RUPEE
"""Rupees are shown to the paise, because that is the smallest unit the exchange prices in."""

_STATE_BADGES: dict[OrderLifecycleState, str] = {
    OrderLifecycleState.INTENT_RECORDED: "badge-warning",
    OrderLifecycleState.SUBMISSION_IN_FLIGHT: "badge-warning",
    OrderLifecycleState.AMBIGUOUS: "badge-critical",
    OrderLifecycleState.ACKNOWLEDGED: "badge-good",
    OrderLifecycleState.TRIGGER_PENDING: "badge-good",
    OrderLifecycleState.WORKING: "badge-good",
    OrderLifecycleState.MODIFY_PENDING: "badge-warning",
    OrderLifecycleState.CANCEL_PENDING: "badge-warning",
    OrderLifecycleState.PARTIALLY_FILLED: "badge-good",
    OrderLifecycleState.FILLED: "badge-good",
    OrderLifecycleState.CANCELLED: "badge-absent",
    OrderLifecycleState.REJECTED: "badge-critical",
    OrderLifecycleState.EXPIRED: "badge-absent",
    OrderLifecycleState.ABANDONED: "badge-critical",
}
"""Colour by what the state COSTS a reader who ignores it, not by where it sits in the table.

`AMBIGUOUS` is critical rather than neutral: it is the one state where this system does not know
whether a live order exists, and it is the state a retry would turn into a doubled position.
`CANCELLED` and `EXPIRED` wear the absent badge because nothing happened and nothing is wrong.
"""

_VERDICT_BADGES: dict[ReconciliationVerdict, str] = {
    ReconciliationVerdict.AGREED: "badge-good",
    ReconciliationVerdict.BROKER_ONLY: "badge-warning",
    ReconciliationVerdict.LOCAL_ONLY_UNRESOLVED: "badge-warning",
    ReconciliationVerdict.LOCAL_ONLY_ABANDONED: "badge-critical",
    ReconciliationVerdict.QUANTITY_GAP_PATCHED: "badge-critical",
    ReconciliationVerdict.STATE_CONFLICT_BROKER_WON: "badge-critical",
    ReconciliationVerdict.UNMAPPABLE: "badge-critical",
}
"""Every verdict the reconciler can reach, so a new one shows up as a `KeyError` in a test rather
than as an unstyled word on a live page."""

_INFERRED_WORD = "INFERRED"
"""The word that must appear beside anything this system invented. Colour alone is not a label."""

_UNREADABLE_WORD = "COULD NOT BE READ"
"""What an order that would not fold is called, in words, on the row where it would have been.

Not `INFERRED` and not an absence: those two say the system knows something it did not observe.
This one says the system holds a record it cannot interpret, which is a different and worse
statement, and it is styled `badge-critical` because ignoring it loses an order.
"""

_EXCHANGE_ALGO_TAGGING_CIRCULAR = (
    "NSE/INVG/67858 (2025-05-05), para G: “All algo orders (Below and above the threshold) "
    "shall be tagged with a unique identifier provided by the Exchange in order to establish "
    "audit trail.”"
)
"""The obligation the identifier row is measured against, quoted rather than paraphrased.

The paraphrase an operator would otherwise get — "orders should be tagged" — is the version under
which an absent identifier reads as a nicety. The circular's own words say ALL algo orders, above
and below the registration threshold, which is what makes an unset identifier a gap in this
system's own compliance rather than a broker's problem. Source: `docs/research/223` §4.
"""

_PAGE_CSS = """
:root{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
  --axis:#c3c2b7;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
    --axis:#383835;
  }
}
[data-theme="dark"]{
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  --axis:#383835;
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
  padding:14px 18px;overflow-x:auto;}
table{border-collapse:collapse;width:100%;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap;}
th{color:var(--text-secondary);font-weight:600;}
td.figure,th.figure{text-align:right;font-variant-numeric:tabular-nums;}
td.reason{white-space:normal;color:var(--text-secondary);}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
  color:#ffffff;}
.badge-critical{background:__CRITICAL__;}
.badge-warning{background:__WARNING__;color:#0b0b0b;}
.badge-good{background:__GOOD__;}
/* No fill and a dashed edge — the same vocabulary /costs gives UNPRICEABLE. An inferred fact is
   the ABSENCE of an observation, and painting it in any status colour would file it under one. */
.badge-absent{background:transparent;color:var(--text-muted);
  border:1px dashed var(--axis);padding:0 7px;}
.tile-absent{border-style:dashed;}
.tile-absent .tile-value{color:var(--text-muted);}
/* A count that is not an absence and not a status — it is damage. Solid critical edge, same ink
   as the blocker band, because an unreadable order is an order nobody can account for. */
.tile-critical{border:2px solid __CRITICAL__;}
.tile-critical .tile-value{color:__CRITICAL__;}
/* The same critical ink as a rule down the left of the row, rather than a second colour value:
   one definition of "critical" on this page, used in three places. */
tr.unreadable td:first-child{box-shadow:inset 4px 0 0 __CRITICAL__;}
tr.inferred td{color:var(--text-muted);}
.muted{color:var(--text-muted);}
.mono-id{font-size:12px;color:var(--text-secondary);}
/* The open blocker. Solid critical edge and a full-width band: this is the one thing on the page
   that must be impossible to scroll past, because it is the reason the feature is not finished. */
.blocker{background:var(--surface-1);border:2px solid __CRITICAL__;border-left-width:10px;
  border-radius:8px;padding:14px 18px;margin:0 0 24px;max-width:100%;}
.blocker h2{margin:0 0 6px;font-size:15px;color:__CRITICAL__;}
.blocker p{margin:0 0 8px;max-width:88ch;}
code{font-size:12px;color:var(--text-secondary);}
details{margin-top:8px;}
summary{cursor:pointer;color:var(--text-secondary);font-size:12px;}
details table{margin-top:6px;}
details th,details td{font-size:12px;padding:3px 8px;}
footer{color:var(--text-muted);font-size:12px;margin-top:26px;max-width:88ch;}
"""
# Substituted rather than %-formatted, for the reason `/costs` records: a stylesheet is full of
# `%` units, and %-formatting one is how `width:100%` becomes a format-string error.
_PAGE_CSS = (
    _PAGE_CSS.replace("__CRITICAL__", _STATUS_CRITICAL)
    .replace("__WARNING__", _STATUS_WARNING)
    .replace("__GOOD__", _STATUS_GOOD)
)


# --------------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class OrderPathSurfaceState:
    """Everything the page draws, already measured. No object here can reach a broker."""

    session_date: date
    measured_at: datetime
    orders: tuple[OrderRecord, ...]
    in_flight_submissions: tuple[InFlightSubmission, ...]
    horizon: VisibilityHorizon
    reconciliation: ReconciliationReport | None
    journal_path: Path
    journal_exists: bool
    recorded_session_dates: tuple[date, ...]
    unreadable_orders: tuple[UnreadableOrder, ...]
    """Intents the journal holds that this build could not fold back into an order.

    Carried beside `orders` rather than merged into it, because the page has to be able to say
    "this many were recorded and this many of them are unreadable" — a total that stays right even
    when part of it cannot be read is the only total worth printing.
    """

    exchange_algo_identifier: str | None
    """The exchange's algo audit-trail identifier, or `None` when the operator has not set one.

    Passed in rather than read here: this module opens nothing and asks nothing. `None` is a real
    and reportable state — the identifier belongs to the exchange and is never invented — and the
    page's job is to make that state impossible to miss rather than to fill it in.
    """

    @property
    def inferred_orders(self) -> tuple[OrderRecord, ...]:
        """Orders any part of whose history this system invented rather than observed."""
        return tuple(order for order in self.orders if order.has_inferred_events)

    @property
    def open_orders(self) -> tuple[OrderRecord, ...]:
        return tuple(order for order in self.orders if not order.is_terminal)

    def state_census(self) -> dict[OrderLifecycleState, int]:
        """How many orders sit in each lifecycle state, every state present even at zero.

        Zeros are drawn rather than dropped for the reason the precondition chart on `/costs`
        draws them: a missing row reads as "not a problem", and "no order is ambiguous" is a
        different statement from "ambiguity is not tracked here".
        """
        census = dict.fromkeys(OrderLifecycleState, 0)
        for order in self.orders:
            census[order.state] += 1
        return census


def build_order_path_surface_state(
    journal: OrderIntentJournal,
    *,
    session_date: date | None = None,
    reconciliation: ReconciliationReport | None = None,
    exchange_algo_identifier: str | None,
    measured_at: datetime,
    journal_path: Path,
) -> OrderPathSurfaceState:
    """Read the journal ONCE and fold it into what the page draws.

    The session is derived rather than configured: the latest session the journal actually holds,
    falling back to the caller's date when it holds none. A surface pinned to a date goes stale
    the moment the market rolls over, which is the same defect as a hand-authored status.

    The horizon comes from the reconciler's own estimator over this journal's observations, so the
    number shown is the number that would decide an abandonment — not a second opinion computed
    here. A `reconciliation` of `None` is carried as `None`: the page says no reconciliation has
    run rather than showing tallies of zero, because a zero disagreement count is a claim and an
    absent one is not.

    The orders are folded DEFENSIVELY (`defensive_session_order_fold`), which is the whole of the
    `M5` fix: one order this build cannot interpret used to raise out of `orders_for_session`, out
    of this function, out of the route, and out as an HTTP 500 — taking the in-flight submission
    queue and the inferred-event ledger with it, at the one moment an operator needs them. It now
    becomes a visible row that says it could not be read, and the rest of the session still draws.

    `exchange_algo_identifier` is keyword-only and has NO default on purpose. A default of `None`
    would let a caller that simply forgot to ask the venue render an ABSENT identifier badge, and
    "nobody configured it" and "nobody asked" must not be able to print the same sentence.
    """
    recorded_sessions = journal.recorded_session_dates()
    session = session_date or (recorded_sessions[-1] if recorded_sessions else measured_at.date())
    if recorded_sessions and session_date is None:
        session = recorded_sessions[-1]
    folded = fold_session_orders_defensively(
        journal, session_date=session, journal_path=journal_path
    )
    return OrderPathSurfaceState(
        session_date=session,
        measured_at=measured_at,
        orders=folded.orders,
        in_flight_submissions=journal.in_flight_submissions(session_date=session),
        horizon=visibility_horizon_from_observed_delays(journal.visibility_delays_seconds()),
        reconciliation=reconciliation,
        journal_path=journal_path,
        journal_exists=journal_path.exists(),
        recorded_session_dates=recorded_sessions,
        unreadable_orders=folded.unreadable_orders,
        exchange_algo_identifier=exchange_algo_identifier,
    )


def empty_order_path_surface_state(
    *,
    session_date: date,
    measured_at: datetime,
    journal_path: Path,
    exchange_algo_identifier: str | None,
) -> OrderPathSurfaceState:
    """The state for a journal that does not exist yet — no orders have ever been placed.

    A distinct constructor rather than a page that opens the journal anyway: opening it CREATES
    it, and a read-only surface that writes a database on every page load is a side effect nobody
    asked for. The page then says the journal has never been written, which is a stronger and more
    useful statement than an empty table.
    """
    return OrderPathSurfaceState(
        session_date=session_date,
        measured_at=measured_at,
        orders=(),
        in_flight_submissions=(),
        horizon=visibility_horizon_from_observed_delays(()),
        reconciliation=None,
        journal_path=journal_path,
        journal_exists=False,
        recorded_session_dates=(),
        unreadable_orders=(),
        exchange_algo_identifier=exchange_algo_identifier,
    )


# --------------------------------------------------------------------------- formatting


def _format_quantity(quantity: int) -> str:
    return f"{quantity:,}"


def _format_paise(value: Decimal) -> str:
    """A paise figure with its rupee equivalent, because operators think in rupees."""
    quantised = value.quantize(_DISPLAY_PAISE_PRECISION, rounding=ROUND_HALF_EVEN).normalize()
    rupees = (value / _PAISE_PER_RUPEE).quantize(
        _RUPEE_DISPLAY_PRECISION, rounding=ROUND_HALF_EVEN
    )
    return f"{quantised:,f} p (₹{rupees:,})"


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _absent(word: str, explanation: str) -> str:
    """The one way this page renders nothing: a dashed badge carrying the reason."""
    return (
        f'<span class="badge badge-absent" title="{escape(explanation)}">{escape(word)}</span>'
    )


def _state_badge(state: OrderLifecycleState | None) -> str:
    if state is None:
        return _absent("no local order", "this system never recorded an intent for this order")
    return f'<span class="badge {_STATE_BADGES[state]}">{escape(state.value)}</span>'


def _inferred_badge_or_observed(order: OrderRecord) -> str:
    """Whether any part of this order was invented, said in a word as well as in a style."""
    if not order.has_inferred_events:
        return '<span class="muted">observed</span>'
    return _absent(
        _INFERRED_WORD,
        "part of this order's history was inferred by this system to explain a gap between its "
        "own view and the broker's; it was never observed",
    )


def _average_fill_price_cell(order: OrderRecord) -> str:
    """The average fill price, or the ABSENCE of one — and never a zero standing in for it.

    `OrderRecord.average_fill_price_paise` returns `None` when nothing has filled, so that a
    fabricated cost basis can never enter the book. Formatting that `None` as `0.00` would put the
    fabrication back on the screen, where a human would read it as a real price paid.
    """
    average = order.average_fill_price_paise
    if average is None:
        absent = _absent(
            "no fill",
            "nothing has filled, so there is no average to report — this is an absence, not a "
            "price of zero",
        )
        return f"<td>{absent}</td>"
    return f'<td class="figure">{escape(_format_paise(average))}</td>'


def _expression_summary(expression: OrderExpression) -> str:
    """WHAT was actually sent — the member of the taxonomy, not the intent behind it."""
    parts = [
        expression.variety.value,
        expression.product.value,
        expression.order_type.value,
        expression.validity.value,
    ]
    if expression.limit_price_paise is not None:
        parts.append(f"limit {_format_paise(expression.limit_price_paise)}")
    if expression.trigger_price_paise is not None:
        parts.append(f"trigger {_format_paise(expression.trigger_price_paise)}")
    if expression.disclosed_quantity is not None:
        parts.append(f"disclosed {_format_quantity(expression.disclosed_quantity)}")
    if expression.iceberg_legs is not None:
        parts.append(f"{expression.iceberg_legs} iceberg legs")
    if expression.validity_minutes is not None:
        parts.append(f"{expression.validity_minutes} minute life")
    if expression.market_protection_percent is not None:
        parts.append(f"{expression.market_protection_percent}% market protection")
    return " · ".join(parts)


# --------------------------------------------------------------------------- sections


def _open_blocker_banner() -> str:
    """The `R.05` real-fill probe, stated where it cannot be missed.

    Deliberately the first thing in the body and not a footnote: `F02` closes with this criterion
    explicitly unmet, and a page that buried it would let the feature read as finished. The wording
    is the same wording `docs/BACKLOG.md` carries, so the two cannot drift into disagreeing.
    """
    return (
        '<div class="blocker">'
        '<h2>OPEN BLOCKER — the real-fill verification has NOT been run</h2>'
        "<p>The real-FILL lifecycle probe for <code>F02</code> is <strong>deferred by operator "
        "decision</strong> (<code>A.99</code>, 2026-08-13), to be run after the project is "
        "complete. This feature's <code>R.05</code> pass is READ-ONLY: real "
        "<code>orders()</code>, <code>order_history()</code>, <code>trades()</code>, "
        "<code>positions()</code> and real rejection responses.</p>"
        "<p>Nothing below has yet been proved against a real fill, a real broker order-id "
        "lifecycle, real charges on a real contract note, or a real postback. Until that probe "
        "runs, every claim this page makes about the FILL path rests on a hermetic harness, "
        "which <code>R.05</code> counts as functional verification only — not as a pass. Tracked "
        "as an open blocker in <code>docs/BACKLOG.md</code> under "
        "<code>F02</code> order path, and surfaced at every sign-off.</p>"
        "</div>"
    )


def _exchange_algo_identifier_section(identifier: str | None) -> str:
    """Whether the orders this system sends carry the exchange's audit-trail identifier.

    **Why this is on the page at all** (`M6` adversarial review). The venue reads the identifier
    from the environment and omits `algo_id` when it is unset, which is the correct behaviour —
    a fabricated audit-trail identifier is worse than none, because it is indistinguishable from
    a real one in the exchange's own records. But the omission was invisible. The venue's
    `carries_exchange_algo_identifier` property was referenced nowhere outside its own module, so
    the only way to discover that months of orders went out untagged was an exchange query. A
    compliance gap that can only be found by the regulator is not a gap this system is managing.

    **Why ABSENT is critical and not the dashed absent badge.** Everything else on this page that
    wears `badge-absent` is the absence of an OBSERVATION — no fill, no horizon, nothing inferred.
    This is the absence of a CONFIGURATION, and it has a consequence that runs the other way: the
    orders are still sent, and they are sent untagged. It is styled like the thing it is.
    """
    if identifier is not None:
        status_cell = '<span class="badge badge-good">PRESENT</span>'
        detail = (
            f"Every order this venue sends carries <code>algo_id</code>. The identifier is relayed "
            f"from <code>{escape(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR)}</code> exactly as the exchange "
            f"issued it and is never manufactured here. It is a different field, with a different "
            f"purpose, from the wire tag this system computes to find its own orders."
        )
        value_cell = f'<span class="mono-id">{escape(identifier)}</span>'
    else:
        status_cell = '<span class="badge badge-critical">ABSENT</span>'
        detail = (
            f"<strong>Every order this system sends today goes out with no "
            f"<code>algo_id</code>.</strong> The identifier is issued by the exchange and relayed "
            f"by this system — omitting it is correct, and inventing one would be far worse — so "
            f"this is an OPERATOR action, not a defect: set "
            f"<code>{escape(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR)}</code> to the identifier the "
            f"exchange issued for this flow. Until then the audit trail the circular requires does "
            f"not exist for any order in the journal below. Tracked in "
            f"<code>docs/BACKLOG.md</code> under <code>F02</code> order path."
        )
        value_cell = _absent(
            "not configured",
            "the environment variable is unset on this host, so the venue omits the parameter "
            "rather than sending a value nobody issued",
        )
    return (
        f'<div class="panel"><table><thead><tr><th>exchange algo identifier</th>'
        f"<th>value</th><th>what that means for the orders below</th></tr></thead><tbody>"
        f"<tr><td>{status_cell}</td><td>{value_cell}</td>"
        f'<td class="reason">{detail}</td></tr>'
        f'<tr><td class="muted">the obligation</td>'
        f'<td colspan="2" class="reason">{escape(_EXCHANGE_ALGO_TAGGING_CIRCULAR)}</td></tr>'
        f"</tbody></table></div>"
    )


def _tiles(state: OrderPathSurfaceState) -> str:
    """Counts, each folded out of the data rather than carried as a sentence."""
    report = state.reconciliation
    unreadable_count = len(state.unreadable_orders)
    # Shown at zero as well as above it, and only styled critical when it is not zero. Zero here is
    # a MEASUREMENT — every intent of the session folded — and is worth stating; a hidden tile
    # would mean the page looked identical whether or not the check had been made at all.
    unreadable_tile = (
        f'<div class="tile{" tile-critical" if unreadable_count else ""}">'
        f'<div class="tile-value">{escape(_format_quantity(unreadable_count))}</div>'
        f'<div class="tile-label">intents this build could NOT read back</div></div>'
    )
    disagreement_tile = (
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(len(report.disagreements)))}</div>"
        f'<div class="tile-label">disagreements in the last reconciliation</div></div>'
        if report is not None
        else f'<div class="tile tile-absent"><div class="tile-value">{_ABSENT_MARK}</div>'
        f'<div class="tile-label">no reconciliation has run — a zero here would be a '
        f"claim</div></div>"
    )
    return (
        f'<div class="tiles">'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(len(state.orders) + unreadable_count))}</div>"
        f'<div class="tile-label">intents recorded this session</div></div>'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(len(state.open_orders)))}</div>"
        f'<div class="tile-label">not yet in a terminal state</div></div>'
        f'<div class="tile tile-absent"><div class="tile-value">'
        f"{escape(_format_quantity(len(state.inferred_orders)))}</div>"
        f'<div class="tile-label">orders carrying an {_INFERRED_WORD} event</div></div>'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(len(state.in_flight_submissions)))}</div>"
        f'<div class="tile-label">submissions with no outcome written</div></div>'
        f"{unreadable_tile}"
        f"{disagreement_tile}"
        f"</div>"
    )


def _order_row(order: OrderRecord) -> str:
    """One intent, across: what was asked, what was sent, and what came back."""
    if order.broker_order_id:
        broker_order_id = f'<td class="mono-id">{escape(order.broker_order_id)}</td>'
    else:
        absent_broker_order_id = _absent(
            "no broker id",
            "the broker has never named this order — either it was not sent, or it has not been "
            "seen at the broker yet",
        )
        broker_order_id = f"<td>{absent_broker_order_id}</td>"
    chosen_because = order.expression.chosen_because
    if chosen_because:
        chosen_cell = f'<td class="reason">{escape(chosen_because)}</td>'
    else:
        absent_reason = _absent(
            "unstated",
            "the expression selector recorded no reason for this choice, so nobody can audit why "
            "this shape was sent",
        )
        chosen_cell = f"<td>{absent_reason}</td>"
    row_class = ' class="inferred"' if order.has_inferred_events else ""
    return (
        f"<tr{row_class}>"
        f'<td class="mono-id">{escape(order.intent_id[:12])}</td>'
        f"<td>{escape(order.trading_symbol)}</td>"
        f"<td>{escape(str(order.side))}</td>"
        f"<td>{_state_badge(order.state)}</td>"
        f"<td>{_inferred_badge_or_observed(order)}</td>"
        f'<td class="figure">{escape(_format_quantity(order.ordered_quantity))}</td>'
        f'<td class="figure">{escape(_format_quantity(order.filled_quantity))}</td>'
        f'<td class="figure">{escape(_format_quantity(order.leaves_quantity))}</td>'
        f"{_average_fill_price_cell(order)}"
        f"{broker_order_id}"
        f'<td class="mono-id">{escape(order.broker_tag)}</td>'
        f'<td class="reason">{escape(_expression_summary(order.expression))}</td>'
        f"{chosen_cell}"
        f"</tr>"
    )


_ORDER_TABLE_COLUMNS = 13
"""How many columns the intent table has. Read by the unreadable row, which spans the middle of it.

Named rather than typed twice: a column added to the table and not to the span produces a row that
silently mis-aligns, and a mis-aligned row is one a reader skips.
"""

_UNREADABLE_ROW_SPAN = _ORDER_TABLE_COLUMNS - 2
"""The middle of the row: everything between the intent id and the failure text."""


def _unreadable_order_row(unreadable: UnreadableOrder) -> str:
    """The row an order gets when this build cannot fold it back — never a gap in the table.

    A dropped row is not a smaller failure than a 500, it is a quieter one: the table would then
    say, with the page's full authority, that this order does not exist. So the row is drawn, it
    says COULD NOT BE READ in words, and it carries the exception verbatim — the exception being
    the only thing on the row that tells anyone what to do about it.
    """
    intent_cell = (
        f'<td class="mono-id">{escape(unreadable.intent_id[:12])}</td>'
        if unreadable.intent_id
        else "<td>"
        + _absent(
            "no intent id",
            "the journal could not be asked which intents this session holds, so this failure "
            "cannot be attributed to one order",
        )
        + "</td>"
    )
    return (
        f'<tr class="unreadable">'
        f"{intent_cell}"
        f'<td colspan="{_UNREADABLE_ROW_SPAN}">'
        f'<span class="badge badge-critical">{escape(_UNREADABLE_WORD)}</span> '
        f"this intent is recorded in the journal and this build could not fold it back into an "
        f"order, so nothing below about it — state, quantities, fills — is known.</td>"
        f'<td class="reason">{escape(unreadable.failure_sentence)}</td>'
        f"</tr>"
    )


def _orders_section(state: OrderPathSurfaceState) -> str:
    if not state.orders and not state.unreadable_orders:
        return _empty_note(
            f"No intent is recorded for {state.session_date.isoformat()}. That is a statement "
            f"about the journal at {state.journal_path}, not about this page: an intent is "
            f"written and committed BEFORE any network call exists, so an empty session means "
            f"nothing was decided — never that something was decided and lost."
            if state.journal_exists
            else f"The write-ahead journal at {state.journal_path} has never been created, so no "
            f"intent has ever been recorded by this system. The order path writes the journal on "
            f"its first placement; until then this page has nothing to show and says so rather "
            f"than drawing an empty table that looks like a quiet day."
        )
    # Unreadable first, deliberately: they are the rows a reader must not scroll past, and a table
    # sorted by insertion time would bury them among the orders that folded perfectly well.
    rows = "".join(
        _unreadable_order_row(unreadable) for unreadable in state.unreadable_orders
    ) + "".join(_order_row(order) for order in state.orders)
    return (
        f'<div class="panel"><table><thead><tr>'
        f"<th>intent</th><th>symbol</th><th>side</th><th>state</th><th>evidence</th>"
        f'<th class="figure">ordered</th><th class="figure">filled</th>'
        f'<th class="figure">leaves</th><th class="figure">average fill</th>'
        f"<th>broker order id</th><th>wire tag</th><th>expression sent</th>"
        f"<th>chosen because</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _inferred_fill_row(order: OrderRecord, fill: FillRecord) -> str:
    return (
        f'<tr class="inferred"><td class="mono-id">{escape(order.intent_id[:12])}</td>'
        f"<td>{escape(order.trading_symbol)}</td>"
        f"<td>fill {escape(fill.broker_trade_id)}</td>"
        f'<td class="figure">{escape(_format_quantity(fill.quantity))}</td>'
        f'<td class="figure">{escape(_format_paise(fill.price_paise))}</td>'
        f'<td class="reason">{escape(fill.inferred_from)}</td></tr>'
    )


def _inferred_transition_row(order: OrderRecord, transition_index: int) -> str:
    transition = order.transitions[transition_index]
    return (
        f'<tr class="inferred"><td class="mono-id">{escape(order.intent_id[:12])}</td>'
        f"<td>{escape(order.trading_symbol)}</td>"
        f"<td>{escape(transition.from_state.value)} → {escape(transition.to_state.value)}"
        f" on {escape(transition.event.value)}</td>"
        f'<td class="figure">{_ABSENT_MARK}</td>'
        f'<td class="figure">{_ABSENT_MARK}</td>'
        f'<td class="reason">{escape(transition.note)}</td></tr>'
    )


def _inferred_section(state: OrderPathSurfaceState) -> str:
    """Every invention, listed with the evidence it was invented from.

    An inferred fill must record what it was inferred from — `FillRecord` refuses to exist
    otherwise — and this is where that text is read. A page that only badged the row would tell an
    operator that something was invented without telling them why, which is half an answer.
    """
    rows = "".join(
        "".join(
            _inferred_fill_row(order, fill)
            for fill in order.fills
            if fill.source is EventSource.INFERRED
        )
        + "".join(
            _inferred_transition_row(order, index)
            for index, transition in enumerate(order.transitions)
            if transition.source is EventSource.INFERRED
        )
        for order in state.inferred_orders
    )
    if not rows:
        return _empty_note(
            "Nothing on this page was inferred. Every fill and every transition shown above was "
            "either recorded locally as it happened or reported by the broker — none of it is "
            "this system's reconstruction of a gap."
        )
    return (
        f'<div class="panel"><table><thead><tr><th>intent</th><th>symbol</th>'
        f'<th>what was invented</th><th class="figure">quantity</th>'
        f'<th class="figure">priced at</th><th>inferred from</th>'
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _census_section(state: OrderPathSurfaceState) -> str:
    census = state.state_census()
    rows = "".join(
        f"<tr><td>{_state_badge(lifecycle_state)}</td>"
        f'<td class="figure">{escape(_format_quantity(count))}</td>'
        f'<td class="reason">'
        f"{'terminal' if lifecycle_state in TERMINAL_STATES else 'still moving'}</td></tr>"
        for lifecycle_state, count in census.items()
    )
    return (
        f'<div class="panel"><table><thead><tr><th>lifecycle state</th>'
        f'<th class="figure">orders</th><th>terminal?</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _horizon_section(horizon: VisibilityHorizon) -> str:
    """How long absence has to last before it counts as evidence of absence.

    When the horizon is not established the page says so in words rather than leaving the cell
    blank: a blank reads as zero, and a horizon of zero is the claim that any unseen order may be
    declared abandoned immediately — the exact opposite of what the reconciler does.
    """
    seconds = horizon.seconds
    if seconds is None:
        return (
            f'<div class="note">'
            f'<p><span class="badge badge-absent">NOT ESTABLISHED</span> '
            f"<strong>No order will be declared abandoned yet.</strong> The horizon is measured "
            f"from this system's own observations of how long its orders took to appear at the "
            f"broker — Kite publishes no propagation-latency figure at all — and there are not "
            f"enough of them yet. Orders the broker has never shown are reported UNRESOLVED and "
            f"held, which is the honest answer rather than a convenient one.</p>"
            f"<p class=\"muted\">{escape(horizon.detail)}</p>"
            f'<p class="muted">Observations recorded so far: '
            f"{escape(_format_quantity(horizon.observations))}.</p></div>"
        )
    return (
        f'<div class="tiles">'
        f'<div class="tile"><div class="tile-value">{escape(_format_seconds(seconds))}</div>'
        f'<div class="tile-label">measured visibility horizon</div></div>'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(horizon.observations))}</div>"
        f'<div class="tile-label">appearance delays it rests on</div></div></div>'
        f'<div class="note"><p>{escape(horizon.detail)}</p>'
        f"<p>An order absent from the broker's book for longer than this is declared ABANDONED "
        f"rather than resent, because resending is the one action that can double a position.</p>"
        f"</div>"
    )


def _reconciliation_row(reconciliation: OrderReconciliation) -> str:
    intent = reconciliation.intent_id
    broker_order_id = reconciliation.broker_order_id
    row_class = ' class="inferred"' if reconciliation.inferred else ""
    inferred_cell = (
        _absent(
            _INFERRED_WORD,
            "this verdict rests on something this system invented rather than observed",
        )
        if reconciliation.inferred
        else '<span class="muted">observed</span>'
    )
    intent_cell = (
        escape(intent[:12])
        if intent
        else _absent(
            "not ours",
            "the broker has this order and this system never decided it — usually a human order "
            "placed in the broker's own app",
        )
    )
    broker_status_cell = (
        escape(reconciliation.broker_status)
        if reconciliation.broker_status
        else _absent(
            "no status",
            "the broker never showed this order, so it reported no status for it",
        )
    )
    return (
        f"<tr{row_class}>"
        f'<td><span class="badge {_VERDICT_BADGES[reconciliation.verdict]}">'
        f"{escape(reconciliation.verdict.value)}</span></td>"
        f'<td class="mono-id">{intent_cell}</td>'
        f"<td>{escape(reconciliation.trading_symbol)}</td>"
        f"<td>{_state_badge(reconciliation.local_state)}</td>"
        f"<td>{broker_status_cell}</td>"
        f'<td class="figure">'
        f"{escape(_format_quantity(reconciliation.local_filled_quantity))}</td>"
        f'<td class="figure">'
        f"{escape(_format_quantity(reconciliation.broker_filled_quantity))}</td>"
        f'<td class="mono-id">'
        f"{escape(broker_order_id) if broker_order_id else _ABSENT_MARK}</td>"
        f"<td>{inferred_cell}</td>"
        f'<td class="reason">{escape(reconciliation.detail)}</td>'
        f"</tr>"
    )


def _reconciliation_section(report: ReconciliationReport | None) -> str:
    """The verdict tallies, and every disagreement in full — never summarised into a number.

    The detail text is printed verbatim because it is the only place the evidence lives: "the
    broker reports COMPLETE for an order this system believes is cancelled" is actionable, and the
    count of state conflicts is not.
    """
    if report is None:
        return _empty_note(
            "No reconciliation report was supplied to this page, so nothing here has been checked "
            "against the broker. This is deliberately NOT rendered as zero disagreements: the "
            "reconciler needs a live broker session, and 'I could not ask' and 'there is nothing "
            "there' are the same answer only to a system that doubles positions. And this section "
            "cannot yet populate for a second reason, stated here rather than left to look like a "
            "quiet day: the daily runner DOES reconcile the order path every day, but it folds the "
            "report into a one-line summary and keeps nothing, so there is no last report for any "
            "reader to load. Persisting it is an open item in docs/BACKLOG.md under F02 order "
            "path; until it is done, this page will say exactly this."
        )
    counts = report.counts_by_verdict()
    count_tiles = "".join(
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_quantity(counts.get(verdict.value, 0)))}</div>"
        f'<div class="tile-label"><span class="badge {_VERDICT_BADGES[verdict]}">'
        f"{escape(verdict.value)}</span></div></div>"
        for verdict in ReconciliationVerdict
    )
    agreement_sentence = (
        f"Every order the broker knows about matched what this system believed. That is a "
        f"measured agreement across {_format_quantity(len(report.reconciliations))} orders, not "
        f"an empty table."
    )
    body = "".join(_reconciliation_row(item) for item in report.disagreements) or (
        f'<tr><td colspan="10" class="muted">{escape(agreement_sentence)}</td></tr>'
    )
    return (
        f'<p class="sub">Reconciled at {escape(report.ran_at.isoformat())} for session '
        f"{escape(report.session_date.isoformat())}, across "
        f"{escape(_format_quantity(len(report.reconciliations)))} orders.</p>"
        f'<div class="tiles">{count_tiles}</div>'
        f'<div class="panel"><table><thead><tr><th>verdict</th><th>intent</th><th>symbol</th>'
        f'<th>local state</th><th>broker status</th><th class="figure">filled here</th>'
        f'<th class="figure">filled there</th><th>broker order id</th><th>evidence</th>'
        f"<th>what the reconciler decided, in its own words</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _in_flight_section(state: OrderPathSurfaceState) -> str:
    """The crash-recovery queue: attempts whose outcome was never written."""
    if not state.in_flight_submissions:
        return _empty_note(
            "No submission is in flight without an outcome. Every broker call this session was "
            "followed by a committed answer — including, where that was the answer, 'I do not "
            "know'. A row here after a restart is an order that may or may not exist at the "
            "broker, and it is resolved by LOOKING rather than by resending."
        )
    absent_outcome = _absent(
        "no outcome",
        "the broker call was started and no answer was ever committed — a crash, or a call still "
        "running",
    )
    rows = "".join(
        f'<tr><td class="figure">{escape(str(submission.submission_id))}</td>'
        f'<td class="mono-id">{escape(submission.intent_id[:12])}</td>'
        f'<td class="mono-id">{escape(submission.broker_tag)}</td>'
        f'<td class="figure">{escape(_format_quantity(submission.attempt))}</td>'
        f"<td>{escape(submission.started_at.isoformat())}</td>"
        f"<td>{absent_outcome}</td></tr>"
        for submission in state.in_flight_submissions
    )
    return (
        f'<div class="panel"><table><thead><tr><th class="figure">submission</th>'
        f'<th>intent</th><th>wire tag</th><th class="figure">attempt</th><th>started at</th>'
        f"<th>outcome</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _empty_note(sentence: str) -> str:
    """An emptiness that states what it means. A blank panel states nothing."""
    return f'<div class="note muted">{escape(sentence)}</div>'


# --------------------------------------------------------------------------- the page


def render_order_path_page(state: OrderPathSurfaceState) -> str:
    """The whole `/orders` surface, self-contained. Pure: no journal, no broker, no reconciler."""
    sessions = state.recorded_session_dates
    session_note = (
        f"Latest of {_format_quantity(len(sessions))} sessions in the journal "
        f"({sessions[0].isoformat()} to {sessions[-1].isoformat()})."
        if sessions
        else "The journal holds no session yet."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order path</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Order path — session {escape(state.session_date.isoformat())}</h1>
<p class="sub">Every intent this system decided, what was actually sent for it, and what the
broker did about it. Read from the write-ahead journal at
<code>{escape(str(state.journal_path))}</code> at
{escape(state.measured_at.isoformat())}. {escape(session_note)} Nothing on this page is a status
anybody maintains: the states, the counts and the verdicts are folded out of the journal's own
rows and the last reconciliation report, so an order that changes changes here.</p>

{_open_blocker_banner()}

<h2>The exchange's algo audit-trail identifier — carried, or not carried</h2>
<p class="sub">The identifier is the exchange's, not this system's: it is relayed when the operator
has configured one and OMITTED when they have not, because an invented audit-trail identifier is
indistinguishable from a real one in the exchange's records and is therefore worse than none. What
must never happen is the omission being invisible, which is why it is stated here rather than left
to be discovered in an exchange query.</p>
{_exchange_algo_identifier_section(state.exchange_algo_identifier)}

{_tiles(state)}

<h2>Every intent of the session — what was asked, what was sent, what came back</h2>
<p class="sub">An average fill price is shown only where something actually filled. Where nothing
has, the cell reads <span class="badge badge-absent">no fill</span> and never <code>0</code> — a
zero there would be a fabricated cost basis, which is precisely what the order record refuses to
produce. A row whose history this system had to invent is muted and carries
<span class="badge badge-absent">{_INFERRED_WORD}</span>. An intent the journal holds that this
build could not fold back into an order is shown FIRST, as a
<span class="badge badge-critical">{_UNREADABLE_WORD}</span> row carrying the exception that
stopped it — never omitted, because an omitted row would say this order does not exist.</p>
{_orders_section(state)}

<h2>What was INFERRED rather than observed</h2>
<p class="sub">These are this system's own inventions, used to explain a gap between its view and
the broker's — a quantity the broker had filled that no trade accounted for, a submission
reconstructed from a fill that arrived without one. Each carries the evidence it was inferred
from, because an invention that cannot be traced back to its evidence is indistinguishable later
from something that was observed.</p>
{_inferred_section(state)}

<h2>Reconciliation against the broker</h2>
{_reconciliation_section(state.reconciliation)}

<h2>The visibility horizon — when absence becomes evidence of absence</h2>
{_horizon_section(state.horizon)}

<h2>Submissions with no outcome — the crash-recovery queue</h2>
<p class="sub">The journal writes the attempt and commits it BEFORE the broker call, so a process
killed mid-call leaves exactly this row behind. It is the record that tells "never sent" apart
from "sent and I died", and those two need opposite recoveries.</p>
{_in_flight_section(state)}

<h2>Lifecycle census — where every order in the session sits</h2>
<p class="sub">Every state is listed even at zero. A missing row reads as "not a problem", and
"no order is ambiguous" is a different statement from "ambiguity is not tracked here".</p>
{_census_section(state)}

<footer>Colour on this page does one job — status — and every badge carries a word, so nothing
here depends on being able to tell two hues apart. The dashed, unfilled badge is reserved for the
ABSENCE of an observation: an inferred event, a missing average, an unestablished horizon. It is
never a rejection and never a zero. The open blocker at the top of this page is not resolved by
anything below it.</footer>
</body></html>"""
