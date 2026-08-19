"""The live trading surface (`R.08`) — what the six bots hold right now, and what it is worth.

Engines: `nse_algo_trader.paper_loop.live_paper_book`,
`nse_algo_trader.portfolio.portfolio_proposal_supervisor`. Decision `A.146`.

**Why this page exists.** `/bots` answers "which of the six can act" and `/ladder` answers "which
have earned anything". Neither answers the question the operator actually asked — *"where can I see
the six bots open, closed or trading"* — because until `A.146` there was nothing to see: the loop
counted proposals and opened no position, and `/paper-session` could only ever render the last
CLOSED session. This page is the missing one: open positions per bot, marked to the live tape,
beside what each bot has already closed today.

**Nothing here is hand-authored** (`R.08`). Open positions are read from `live_paper_book`, closed
trades from `PaperTrackRecordStore`, and the portfolio exposure is folded from the positions
themselves rather than cached — so the page cannot disagree with the book it is describing.

**Unrealised is separated from unpriced, deliberately.** A position the tape cannot price at this
instant is NOT marked at zero: zero move reads as a flat position when it is simply unobserved, and
that is the difference between "this bot is doing nothing" and "we cannot currently see it".

**Colour carries no meaning alone.** Profit and loss ship their sign and their rupees; the exposure
bar ships its percentage and its bound in words. The reserved status steps are the same ones
`/ladder` and `/bots` use, so one bot cannot appear in two colours on two pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from html import escape

PROFIT_COLOUR = "#0ca30c"
LOSS_COLOUR = "#b3261e"
NEUTRAL_COLOUR = "#8a8a80"
WATCH_COLOUR = "#fab219"

EXPOSURE_IS_CROWDED_AT = 0.8
"""Where the exposure bar turns amber.

A presentation boundary and nothing else — no decision reads it. The supervisor's bound is the
decision; this is the point at which "there is room" becomes "there is nearly none", which is the
only claim the colour makes.
"""


@dataclass(frozen=True, slots=True)
class LivePositionRow:
    """One open position, exactly as the book carries it."""

    trading_symbol: str
    side: str
    quantity: int
    entry_price_paise: Decimal
    last_price_paise: Decimal | None
    opened_at: datetime

    @property
    def is_priced(self) -> bool:
        return self.last_price_paise is not None

    @property
    def unrealised_rupees(self) -> Decimal | None:
        if self.last_price_paise is None:
            return None
        move = self.last_price_paise - self.entry_price_paise
        if self.side.lower() == "sell":
            move = -move
        return move * Decimal(self.quantity) / Decimal(100)


@dataclass(frozen=True, slots=True)
class LiveTradingBotRow:
    """One bot's live state — what it holds, what it has closed, and what it proposed."""

    bot_identity: str
    trading_segment: str
    open_positions: tuple[LivePositionRow, ...] = ()
    closed_today: int = 0
    realised_today_rupees: Decimal = Decimal("0")
    closed_all_time: int = 0
    realised_all_time_rupees: Decimal = Decimal("0")
    proposals_last_tick: int | None = None
    blocker: str = ""

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def unpriced_count(self) -> int:
        return sum(1 for position in self.open_positions if not position.is_priced)

    @property
    def unrealised_rupees(self) -> Decimal:
        return sum(
            (
                position.unrealised_rupees
                for position in self.open_positions
                if position.unrealised_rupees is not None
            ),
            Decimal("0"),
        )

    @property
    def net_exposure_rupees(self) -> Decimal:
        total = Decimal("0")
        for position in self.open_positions:
            notional = position.entry_price_paise * Decimal(position.quantity) / Decimal(100)
            total += -notional if position.side.lower() == "sell" else notional
        return total

    @property
    def is_trading(self) -> bool:
        return self.open_count > 0 or self.closed_today > 0


@dataclass(frozen=True)
class LiveTradingSurface:
    """Everything the page renders. Every field is measured by the caller, never typed here."""

    rows: tuple[LiveTradingBotRow, ...]
    session_date: date | None
    phase: str
    observed_at: datetime | None
    deployable_rupees: Decimal | None
    net_directional_bound_rupees: Decimal | None
    tape_lag_seconds: float | None = None
    archive_progress: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_open(self) -> int:
        return sum(row.open_count for row in self.rows)

    @property
    def total_closed_today(self) -> int:
        return sum(row.closed_today for row in self.rows)

    @property
    def total_unrealised_rupees(self) -> Decimal:
        return sum((row.unrealised_rupees for row in self.rows), Decimal("0"))

    @property
    def total_realised_today_rupees(self) -> Decimal:
        return sum((row.realised_today_rupees for row in self.rows), Decimal("0"))

    @property
    def net_exposure_rupees(self) -> Decimal:
        return sum((row.net_exposure_rupees for row in self.rows), Decimal("0"))

    @property
    def bots_trading(self) -> int:
        return sum(1 for row in self.rows if row.is_trading)


_PAGE = """<main>
<h1>The six bots, live</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""

_STYLE = """<style>
.livetrade{width:100%;border-collapse:collapse;margin-top:12px}
.livetrade th{text-align:left;font-weight:600;color:var(--text-muted);font-size:12px;
 border-bottom:1px solid var(--border);padding:6px 10px 6px 0}
.livetrade td{padding:10px 10px 10px 0;border-bottom:1px solid var(--border);vertical-align:top}
.livetrade .bot{font-family:ui-monospace,monospace;font-size:13px}
.livetrade .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.livetrade .money{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.tile-row{display:flex;gap:18px;flex-wrap:wrap;margin-top:6px}
.tile{min-width:150px}
.tile .value{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .label{font-size:11px;color:var(--text-muted);margin-top:2px}
.exposure-track{position:relative;height:10px;width:100%;max-width:420px;
 background:var(--border);border-radius:5px;margin-top:8px}
.exposure-fill{position:absolute;top:0;left:0;height:10px;border-radius:5px}
.exposure-legend{font-size:11px;color:var(--text-muted);margin-top:4px}
.positions{margin-top:6px;font-size:11px;color:var(--text-muted);max-width:34em}
.positions code{font-size:11px}
.blocker{font-size:11px;color:#b3261e;margin-top:6px;max-width:26em}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
 vertical-align:middle}
.idle{color:var(--text-muted)}
</style>"""


def _rupees(value: Decimal) -> str:
    """Signed rupees, separated. The sign is the fact; the colour only repeats it."""
    sign = "+" if value > 0 else ""
    return f"{sign}Rs {value:,.2f}"


def _capital_text(value: Decimal | None) -> str:
    """`R.03`: an unmeasured capital is reported as unmeasured, never as a round number."""
    if value is None:
        return "NOT RECORDED — the paper ledger holds no balance"
    return f"Rs {value:,.0f}"


def _money_cell(value: Decimal, *, muted_when_zero: bool = True) -> str:
    if value == 0 and muted_when_zero:
        return '<span class="idle">Rs 0.00</span>'
    colour = PROFIT_COLOUR if value > 0 else LOSS_COLOUR
    return f'<span class="money" style="color:{colour}">{escape(_rupees(value))}</span>'


def _exposure_mark(surface: LiveTradingSurface) -> str:
    """The one chart on the page, and it earns its place.

    "Rs 89,794 against Rs 250,000" is a pair of numbers while the reader's question is how much room
    is left, and room is a length. The bar is absolute exposure against the bound, so a short book
    and a long book of the same size fill it the same amount — which is what the bound measures.
    """
    bound = surface.net_directional_bound_rupees
    if bound is None or bound <= 0:
        return '<div class="exposure-legend">no bound is configured, so none can be shown</div>'
    used = float(abs(surface.net_exposure_rupees) / bound)
    fill = WATCH_COLOUR if used >= EXPOSURE_IS_CROWDED_AT else PROFIT_COLOUR
    if used > 1:
        fill = LOSS_COLOUR
    direction = "long" if surface.net_exposure_rupees > 0 else "short"
    if surface.net_exposure_rupees == 0:
        direction = "flat"
    return (
        '<div class="exposure-track" role="img" aria-label='
        f'"net exposure Rs {surface.net_exposure_rupees:,.0f} against a bound of '
        f'Rs {bound:,.0f}">'
        f'<div class="exposure-fill" style="width:{min(used, 1.0) * 100:.1f}%;'
        f'background:{fill}"></div></div>'
        f'<div class="exposure-legend">net <b>{escape(_rupees(surface.net_exposure_rupees))}</b> '
        f"({escape(direction)}) against a portfolio bound of <b>Rs {bound:,.0f}</b> "
        f"&mdash; <b>{used * 100:.1f}%</b> used. The bound is applied ACROSS the six bots "
        f"(<code>B39</code>), not inside each one.</div>"
    )


def _positions_detail(row: LiveTradingBotRow) -> str:
    if not row.open_positions:
        return ""
    shown = sorted(
        row.open_positions,
        key=lambda position: -(abs(position.unrealised_rupees or Decimal("0"))),
    )[:6]
    parts = []
    for position in shown:
        unrealised = position.unrealised_rupees
        mark = "unpriced" if unrealised is None else _rupees(unrealised)
        parts.append(
            f"<code>{escape(position.trading_symbol)}</code> "
            f"{escape(position.side)} {position.quantity:,} &rarr; {escape(mark)}"
        )
    more = len(row.open_positions) - len(shown)
    detail = " · ".join(parts)
    if more > 0:
        detail += f" · and {more:,} more"
    return f'<div class="positions">{detail}</div>'


def _row_html(row: LiveTradingBotRow) -> str:
    colour = NEUTRAL_COLOUR
    state = "idle"
    if row.open_count:
        colour = PROFIT_COLOUR if row.unrealised_rupees >= 0 else LOSS_COLOUR
        state = f"{row.open_count:,} open"
    elif row.closed_today:
        colour = NEUTRAL_COLOUR
        state = "flat, traded today"
    unpriced = (
        f'<div class="tile-note">{row.unpriced_count:,} unpriced &mdash; counted, '
        f"not marked at zero</div>"
        if row.unpriced_count
        else ""
    )
    proposals = (
        f"{row.proposals_last_tick:,}" if row.proposals_last_tick is not None else "&mdash;"
    )
    blocker = f'<div class="blocker">{escape(row.blocker)}</div>' if row.blocker else ""
    return (
        "<tr>"
        f'<td class="bot">{escape(row.bot_identity)}'
        f'<div class="tile-note">{escape(row.trading_segment)}</div>{blocker}</td>'
        f'<td><span class="dot" style="background:{colour}"></span><b>{escape(state)}</b>'
        f"{unpriced}{_positions_detail(row)}</td>"
        f'<td class="num">{_money_cell(row.unrealised_rupees)}</td>'
        f'<td class="num">{_money_cell(row.realised_today_rupees)}'
        f'<div class="tile-note">{row.closed_today:,} closed today</div></td>'
        f'<td class="num">{_money_cell(row.realised_all_time_rupees)}'
        f'<div class="tile-note">{row.closed_all_time:,} closed ever</div></td>'
        f'<td class="num">{proposals}</td>'
        "</tr>"
    )


def render_live_trading_page(surface: LiveTradingSurface) -> str:
    """The page. Every value is passed in, so the renderer cannot disagree with the book."""
    if not surface.rows:
        return _PAGE.format(
            subtitle="No segment bot could be built on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. The registry returned no bot, '
                "which is a failure of construction rather than of trading.</p></div>"
            ),
            footer="<code>nse_algo_trader.paper_loop.live_paper_book</code>",
        )

    ordered = sorted(
        surface.rows,
        key=lambda row: (not row.is_trading, -row.open_count, row.bot_identity),
    )
    lag = (
        f"{surface.tape_lag_seconds:.0f}s"
        if surface.tape_lag_seconds is not None
        else "unknown"
    )
    observed = (
        surface.observed_at.strftime("%H:%M:%S IST") if surface.observed_at else "not yet"
    )
    tiles = (
        '<div class="tile-row">'
        f'<div class="tile"><div class="value">{surface.total_open:,}</div>'
        '<div class="label">open positions</div></div>'
        f'<div class="tile"><div class="value" style="color:'
        f'{PROFIT_COLOUR if surface.total_unrealised_rupees >= 0 else LOSS_COLOUR}">'
        f'{escape(_rupees(surface.total_unrealised_rupees))}</div>'
        '<div class="label">unrealised, marked to the live tape</div></div>'
        f'<div class="tile"><div class="value" style="color:'
        f'{PROFIT_COLOUR if surface.total_realised_today_rupees >= 0 else LOSS_COLOUR}">'
        f'{escape(_rupees(surface.total_realised_today_rupees))}</div>'
        f'<div class="label">realised today, net of costs '
        f"({surface.total_closed_today:,} closed)</div></div>"
        f'<div class="tile"><div class="value">{surface.bots_trading} of {len(ordered)}</div>'
        '<div class="label">bots holding or traded today</div></div>'
        "</div>"
    )
    archive = (
        f'<p class="sub">While the exchange is shut the same six bots walk forward through the '
        f"archive: {escape(surface.archive_progress)}.</p>"
        if surface.archive_progress
        else ""
    )
    notes = (
        "".join(f'<div class="blocker">{escape(note)}</div>' for note in surface.notes)
        if surface.notes
        else ""
    )
    return _PAGE.format(
        subtitle=(
            f"Phase <b>{escape(surface.phase)}</b> on <b>"
            f"{escape(surface.session_date.isoformat() if surface.session_date else 'no session')}"
            f"</b>, last observed {escape(observed)}, tape lag {escape(lag)}. "
            f"Capital <b>{escape(_capital_text(surface.deployable_rupees))}</b>. Every number is "
            f"folded from the "
            f"book and the track record on this request &mdash; nothing here is cached and nothing "
            f"is hand-authored (<code>R.08</code>). No bot is armed and none can arm itself "
            f"(<code>R.22</code>)."
        ),
        body=(
            _STYLE
            + '<div class="panel">'
            + tiles
            + _exposure_mark(surface)
            + archive
            + notes
            + "</div>"
            + '<div class="panel"><table class="livetrade">'
            "<thead><tr><th>bot</th><th>state</th><th>unrealised</th><th>realised today</th>"
            "<th>realised ever</th><th>proposed last tick</th></tr></thead><tbody>"
            + "".join(_row_html(row) for row in ordered)
            + "</tbody></table></div>"
        ),
        footer=(
            "<code>nse_algo_trader.paper_loop.live_paper_book</code> &middot; "
            "<code>nse_algo_trader.portfolio.portfolio_proposal_supervisor</code> &middot; "
            "<code>nse_algo_trader.paper_loop.walk_forward_archive_replay</code>"
        ),
    )
