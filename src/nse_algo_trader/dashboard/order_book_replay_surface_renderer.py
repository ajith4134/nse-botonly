"""`L0.22` surface — what the replay engine could and could not say about a session.

Built against the dataviz procedure, in its order: form first, colour by job, palette
validated by the script, then marks, then accessibility.

**Forms, chosen by each number's job.**

- *Instruments, rows, capture-run crossings* are single scalars with no distribution
  behind them, so they are **stat tiles**, not charts. Plotting a one-value answer buries
  it.
- *The rule mix* is magnitude across four labelled, mutually exclusive categories that sum
  to one, so **horizontal bars with direct labels** — not a pie (angles are hard to
  compare) and not a stacked bar (a single stack forces the eye to difference edges).
  Horizontal keeps the four rule names readable without rotation.
- *The gap distribution* is a distribution, so a **decile ladder**: nine bars, one per
  interior decile, ordered. Fixed bin edges would be the hardcoded constants `R.03`
  forbids and would be meaningless across instruments whose packet rates differ by two
  orders of magnitude. Deciles let the distribution describe itself.

**Colour.** The four trade-side rules are an identity, so the categorical set is assigned
in fixed slot order and never cycled — `quote_rule` is always blue, `unclassified` always
the fourth hue, whatever the data does. The gap ladder is magnitude, so it is one hue
stepped light to dark, never the categorical set. Status colours are reserved for the
fidelity badges and never borrowed for a series. The palette is the one the regime surface
already validated, re-run through `validate_palette.js` for both surfaces: light and dark
both pass every check, with light returning a contrast WARN — which obligates relief, so
**every bar carries a visible value label** rather than relying on fill alone.

**Accessibility.** Four series means a legend is always present, every bar is directly
labelled, and each stat tile states its own units, so identity is never colour-alone. Dark
mode is a selected second palette stepped for the dark surface, not an inverted light one,
and it wins under both the OS setting and an explicit toggle.

**Honesty about the sample.** The page renders a BOUNDED number of instruments, because
replaying 9,000 of them per request would make the page a batch job. The bound is stated in
the header as a count, not hidden — `R.09` is about what the SYSTEM measures, and a
dashboard that quietly showed 25 instruments while implying the universe would be exactly
the hand-authored status `R.08` exists to forbid.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    SessionPriceBasisCoverage,
)
from nse_algo_trader.market_depth.bar_tape_join_verdict_store import SessionVerificationCoverage
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    InstrumentCoverageReport,
    TradeSideRule,
)

_LIGHT_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
_DARK_SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")
"""Categorical, fixed slot order, one slot per trade-side rule. Validated for both
surfaces by the dataviz validator: all checks pass; light returns a contrast WARN, which
is why every bar is directly labelled."""

_LIGHT_MAGNITUDE = ("#cfe0f5", "#a9c8ec", "#83b0e3", "#5d98da", "#3780d1", "#2a6bb0")
_DARK_MAGNITUDE = ("#1d3552", "#274a72", "#315f92", "#3b74b2", "#4589d2", "#4f9ef2")
"""Sequential, ONE hue, for the gap deciles. A magnitude ramp is never the categorical set
— reusing the four rule hues here would imply the deciles are identities rather than an
ordered scale. The categorical validator does not apply to a ramp; the property that does
is lightness monotonicity, checked: light steps 0.732 → 0.141 relative luminance, dark
0.034 → 0.325, both strictly monotonic, each stepped against its own surface."""

_RULE_LABELS: tuple[tuple[TradeSideRule, str], ...] = (
    (TradeSideRule.QUOTE_RULE, "Quote rule"),
    (TradeSideRule.TICK_RULE, "Tick rule"),
    (TradeSideRule.ZERO_TICK_INHERITED, "Zero tick, inherited"),
    (TradeSideRule.UNCLASSIFIED, "Unclassified"),
)


def _rule_fractions(report: InstrumentCoverageReport) -> tuple[float, ...]:
    return (
        report.quote_rule_fraction,
        report.tick_rule_fraction,
        report.zero_tick_fraction,
        report.unclassified_fraction,
    )


def _stat_tile(value: str, label: str, note: str) -> str:
    return (
        '<div class="tile">'
        f'<div class="tile-value">{escape(value)}</div>'
        f'<div class="tile-label">{escape(label)}</div>'
        f'<div class="tile-note">{escape(note)}</div>'
        "</div>"
    )


def _rule_bars(report: InstrumentCoverageReport) -> str:
    fractions = _rule_fractions(report)
    widest = max(fractions) or 1.0
    rows = []
    for slot, ((_, label), fraction) in enumerate(zip(_RULE_LABELS, fractions, strict=True)):
        width = fraction / widest * 100
        rows.append(
            '<div class="bar-row">'
            f'<div class="bar-label">{escape(label)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill series-{slot}" style="width:{width:.2f}%"></div>'
            "</div>"
            f'<div class="bar-value">{fraction:.1%}</div>'
            "</div>"
        )
    return "".join(rows)


def _gap_ladder(report: InstrumentCoverageReport) -> str:
    deciles = report.gap_millisecond_deciles
    if not deciles:
        return (
            '<p class="empty">Fewer than ten transitions — a distribution cannot be '
            "described from that, so none is drawn.</p>"
        )
    widest = max(deciles) or 1.0
    rows = []
    for index, value in enumerate(deciles):
        step = min(index * len(_LIGHT_MAGNITUDE) // len(deciles), len(_LIGHT_MAGNITUDE) - 1)
        rows.append(
            '<div class="bar-row">'
            f'<div class="bar-label">p{(index + 1) * 10}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill magnitude-{step}" '
            f'style="width:{value / widest * 100:.2f}%"></div>'
            "</div>"
            f'<div class="bar-value">{value:,.0f} ms</div>'
            "</div>"
        )
    return "".join(rows)


def _legend() -> str:
    swatches = "".join(
        f'<span class="legend-item"><span class="swatch series-{slot}"></span>'
        f"{escape(label)}</span>"
        for slot, (_, label) in enumerate(_RULE_LABELS)
    )
    return f'<div class="legend">{swatches}</div>'


def _colour_variables(series: tuple[str, ...], magnitude: tuple[str, ...]) -> str:
    return "".join(
        [
            *(f"--series-{slot}:{colour};" for slot, colour in enumerate(series)),
            *(f"--magnitude-{step}:{colour};" for step, colour in enumerate(magnitude)),
        ]
    )


def _series_css() -> str:
    """Palette as custom properties, so dark mode redefines VALUES, not rules.

    Emitting a second copy of every `.series-N{background:…}` rule inside the dark blocks
    is how a stylesheet acquires rules that silently contradict each other — and this
    dashboard has already lost five rules to a CSS defect once. Each class is declared
    exactly once and reads a variable; the three theme blocks set the variables. Dark is a
    SELECTED palette stepped for the dark surface, not an inversion, and it wins under both
    the OS setting and the explicit toggle.
    """
    light_variables = _colour_variables(_LIGHT_SERIES, _LIGHT_MAGNITUDE)
    dark_variables = _colour_variables(_DARK_SERIES, _DARK_MAGNITUDE)
    class_rules = "".join(
        [
            *(
                f".series-{slot}{{background:var(--series-{slot});}}"
                for slot in range(len(_LIGHT_SERIES))
            ),
            *(
                f".magnitude-{step}{{background:var(--magnitude-{step});}}"
                for step in range(len(_LIGHT_MAGNITUDE))
            ),
        ]
    )
    return (
        f":root{{{light_variables}}}"
        f"@media (prefers-color-scheme: dark){{"
        f':root:not([data-theme="light"]){{{dark_variables}}}}}'
        f'[data-theme="dark"]{{{dark_variables}}}'
        f"{class_rules}"
    )


_PAGE_CSS = """
:root{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
  --track:#e8e7e2;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
    --track:#26251f;
  }
}
[data-theme="dark"]{
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  --track:#26251f;
}
*{box-sizing:border-box;}
body{margin:0;padding:32px;background:var(--surface-0);color:var(--text-primary);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;}
h1{font-size:20px;margin:0 0 4px;}
h2{font-size:14px;margin:0 0 12px;color:var(--text-secondary);font-weight:600;}
.sub{color:var(--text-secondary);margin:0 0 24px;}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:150px;flex:1 1 150px;}
.tile-value{font-size:24px;font-weight:700;letter-spacing:-0.01em;}
.tile-label{color:var(--text-secondary);margin-top:2px;}
.tile-note{color:var(--text-muted);font-size:12px;margin-top:6px;}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:18px;margin-bottom:20px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:6px;}
.bar-label{width:170px;color:var(--text-secondary);}
.bar-track{flex:1;height:14px;background:var(--track);border-radius:4px;overflow:hidden;}
.bar-fill{height:100%;border-radius:0 4px 4px 0;}
.bar-value{width:90px;text-align:right;color:var(--text-primary);}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:12px;color:var(--text-secondary);}
.legend-item{display:flex;align-items:center;gap:6px;}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block;}
.empty{color:var(--text-muted);}
table{border-collapse:collapse;width:100%;margin-top:8px;}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);}
th{color:var(--text-secondary);font-weight:600;}
footer{color:var(--text-muted);font-size:12px;margin-top:24px;max-width:70ch;}
"""


def _join_verification_panel(coverage: SessionVerificationCoverage | None) -> str:
    """`M14`'s verdicts (`R.08`) — whether the bars and the tape describe the same market.

    An unrun verification renders as UNRUN, never as clean. The two are different facts and a
    panel that showed a reassuring zero for both would be a hand-authored status in everything
    but name.
    """
    if coverage is None:
        return (
            '<div class="panel"><h2>Bar / depth-tape join</h2>'
            '<p class="empty">NOT VERIFIED for this session. The signal comes from the bar '
            "store and the fill comes from the depth tape, and nothing has yet measured that "
            "they describe the same market. Run "
            "<code>scripts/verify_bar_tape_join_on_real_data.py</code>. This is not a pass.</p>"
            "</div>"
        )
    verifiable = coverage.verifiable_bar_fraction
    tiles = "".join(
        (
            _stat_tile(
                f"{coverage.instruments_verified:,}",
                "verified",
                "bars and tape agree, tested against the session",
            ),
            _stat_tile(
                f"{coverage.instruments_refuted:,}",
                "refuted",
                "refused a replayed book — no paper fill",
            ),
            _stat_tile(
                f"{coverage.instruments_with_price_basis_divergence:,}",
                "wrong price basis",
                "bar series off by a constant factor (M26)",
            ),
            _stat_tile(
                f"{coverage.instruments_unverifiable:,}",
                "unverifiable",
                "too little overlap to have decided either way",
            ),
            _stat_tile(
                "—" if verifiable is None else f"{verifiable:.1%}",
                "bars comparable",
                f"{coverage.comparisons_verifiable:,} of {coverage.bars_total:,}",
            ),
            _stat_tile(
                "—"
                if coverage.mean_null_intra_instrument_correlation is None
                else f"{coverage.mean_null_intra_instrument_correlation:.3f}",
                "intra-instrument rho",
                "0 = the old binomial null was adequate; near 1 falsifies the model",
            ),
        )
    )
    # `B6`'s lesson has to reach the SURFACE, not only the store. A session whose rows were written
    # under two staleness quantiles or two nulls holds two incomparable measurements, and rendering
    # their sum as one verification is how the mixed store went unnoticed for a day.
    # `:.0%` rendered a claim of 0.004 as "0%" (`L1`). `:g` on the percentage keeps small claims
    # legible without inventing precision on ordinary ones.
    claims = ", ".join(
        f"{value * 100:g}%" for value in coverage.minimum_detectable_disagreement_rates
    )
    inconsistency = (
        ""
        if coverage.is_internally_consistent
        else (
            '<p class="empty">NOT ONE VERIFICATION. This session\'s rows were written under '
            f"{len(coverage.staleness_quantiles) or 'no recorded'} staleness quantile(s) "
            f"({', '.join(f'{value:g}' for value in coverage.staleness_quantiles) or 'unrecorded'}"
            f"; {coverage.rows_without_a_staleness_quantile:,} rows unrecorded) and "
            f"{', '.join(coverage.null_models) or 'no recorded'} null model(s) "
            f"({coverage.rows_without_a_null_model:,} rows unrecorded) at "
            f"{', '.join(f'{value:g}' for value in coverage.significances) or 'no recorded'} "
            f"significance(s) and "
            f"{claims or 'no recorded'} detectable-rate claim(s) "
            f"({coverage.rows_without_a_minimum_detectable_rate:,} rows unrecorded). "
            f"The counts below add two "
            "incomparable measurements together. Re-run the verification for this session before "
            "reading them.</p>"
        )
    )
    nulls_named = ", ".join(coverage.null_models) or "unrecorded"
    # `A.124`: a verified verdict is only meaningful alongside the CLAIM it rests on, so the panel
    # states it rather than leaving a reader to assume "verified" means the same thing every run.
    # `M5`: when SOME rows predate the claim, saying "verified means 50%" over all of them is a
    # statement about rows that never recorded one. The sentence has to qualify itself.
    demand = coverage.minimum_comparisons_to_verify
    if not claims:
        claim_sentence = (
            "These rows predate A.124 and do not record what their verified verdicts claimed."
        )
    else:
        cost = f", which took {demand} comparisons" if demand is not None else ""
        unrecorded = (
            f" {coverage.rows_without_a_minimum_detectable_rate:,} rows predate A.124 and are NOT "
            "covered by that claim."
            if coverage.rows_without_a_minimum_detectable_rate
            else ""
        )
        claim_sentence = (
            "Verified means this session held enough evidence to have CAUGHT an instrument "
            f"disagreeing on {claims} of its comparable bars{cost}; below that the verdict is "
            f"unverifiable.{unrecorded}"
        )
    return (
        '<div class="panel"><h2>Bar / depth-tape join</h2>'
        f'<p class="sub">Tested at significance {coverage.significance:g} against a '
        f"{nulls_named} null built from every other instrument in the session. "
        f"{escape(claim_sentence)}</p>"
        f"{inconsistency}"
        f'<div class="tiles">{tiles}</div>'
        "<table><thead><tr><th>Verdict</th><th>Instruments</th></tr></thead><tbody>"
        f"<tr><td>Join verified</td><td>{coverage.instruments_verified:,}</td></tr>"
        f"<tr><td>Join refuted</td><td>{coverage.instruments_refuted:,}</td></tr>"
        f"<tr><td>&nbsp;&nbsp;of which wrong price basis</td>"
        f"<td>{coverage.instruments_with_price_basis_divergence:,}</td></tr>"
        f"<tr><td>Join unverifiable</td><td>{coverage.instruments_unverifiable:,}</td></tr>"
        f"<tr><td>Total instruments</td><td>{coverage.instruments_total:,}</td></tr>"
        "</tbody></table></div>"
    )


def _price_basis_panel(coverage: SessionPriceBasisCoverage | None) -> str:
    """`L0.37` (`R.08`) — what the SIGNAL prices are denominated in.

    The panel above says the bars and the tape agree. This one says whether the bars are even the
    series that traded: Kite adjusts its historical data as of the moment it is asked, so a session
    backfilled after an ex-date is quoted on a basis that did not exist on the day. `HINDPETRO` sat
    at 0.95099 of the traded price on 150 of 150 comparable bars.

    Three counts rather than one score, because the two shortfalls need different actions — bars
    fetched late can be fetched on the right day going forward, bars with no basis at all never
    can.
    """
    if coverage is None:
        return (
            '<div class="panel"><h2>Signal price basis</h2>'
            '<p class="empty">No five-minute bars stored for this session, so there is nothing to '
            "say about what they are denominated in. Not a pass.</p></div>"
        )
    unknown_note = (
        '<p class="empty">Every bar in this session has an UNRECORDED basis. These rows predate '
        "<code>L0.37</code> and are permanently unrecoverable — the backfill dates were never "
        "stored and the source will not serve those sessions unadjusted again. The traded-basis "
        "rule cannot distinguish anything here and is not applied; that is not a clean bill of "
        "health.</p>"
        if coverage.bars_on_the_traded_basis == 0
        else ""
    )
    tiles = "".join(
        (
            _stat_tile(
                f"{coverage.traded_fraction:.1%}",
                "on the traded basis",
                f"{coverage.bars_on_the_traded_basis:,} of {coverage.bars_total:,} bars",
            ),
            _stat_tile(
                f"{coverage.bars_adjusted_after_the_session:,}",
                "adjusted after the session",
                "fetched later; a corporate action in the gap may have rescaled them",
            ),
            _stat_tile(
                f"{coverage.bars_on_an_unknown_basis:,}",
                "basis unrecorded",
                "written before L0.37 — permanently unrecoverable",
            ),
            _stat_tile(
                f"{coverage.bars_on_an_impossible_basis:,}",
                "impossible basis",
                "adjusted as of BEFORE the session they describe — a store defect",
            ),
            _stat_tile(
                f"{len(coverage.instruments_at_risk):,}",
                "instruments at risk",
                "hold a bar adjusted after, or before, their own session",
            ),
            _stat_tile(
                "—"
                if coverage.latest_adjustment_basis is None
                else coverage.latest_adjustment_basis.isoformat(),
                "furthest basis date",
                "the size of the gap is the size of the exposure",
            ),
        )
    )
    return (
        '<div class="panel"><h2>Signal price basis</h2>'
        '<p class="sub">A bar is on the traded basis only if it was fetched on its own session. '
        "Anything later may have been rescaled by a corporate action in between, while the depth "
        "tape holds what actually traded.</p>"
        f"{unknown_note}"
        f'<div class="tiles">{tiles}</div></div>'
    )


def render_order_book_replay_page(
    report: InstrumentCoverageReport,
    join_coverage: SessionVerificationCoverage | None = None,
    price_basis: SessionPriceBasisCoverage | None = None,
) -> str:
    """The whole surface as one self-contained HTML page.

    A table view accompanies both charts, which is what the light-mode contrast WARN
    obligates and what a screen reader needs regardless.
    """
    fractions = _rule_fractions(report)
    table_rows = "".join(
        f"<tr><td>{escape(label)}</td><td>{fraction:.2%}</td></tr>"
        for (_, label), fraction in zip(_RULE_LABELS, fractions, strict=True)
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order-book replay — {report.session_date.isoformat()}</title>
<style>{_PAGE_CSS}{_series_css()}</style>
</head><body>
<h1>Order-book snapshot replay</h1>
<p class="sub">Session {report.session_date.isoformat()} ·
{report.instruments_replayed:,} instruments replayed ·
every number below is computed from the rows the engine actually emitted</p>

<div class="tiles">
{_stat_tile(f"{report.feature_rows_emitted:,}", "feature rows", "one per snapshot transition")}
{
        _stat_tile(
            f"{report.median_gap_milliseconds:,.0f} ms",
            "median gap",
            "between consecutive snapshots",
        )
    }
{
        _stat_tile(
            f"{report.duplicate_row_fraction:.1%}", "duplicate books", "no change between snapshots"
        )
    }
{
        _stat_tile(
            f"{report.capture_run_boundaries_crossed:,}",
            "run boundaries",
            "transitions spanning two captures",
        )
    }
</div>

<div class="panel">
<h2>How each traded quantity was attributed</h2>
{_legend()}
{_rule_bars(report)}
<table><thead><tr><th>Rule</th><th>Share of rows</th></tr></thead>
<tbody>{table_rows}</tbody></table>
</div>

<div class="panel">
<h2>Inter-snapshot gap, by decile</h2>
{_gap_ladder(report)}
</div>

{_join_verification_panel(join_coverage)}

{_price_basis_panel(price_basis)}

<footer>Rule shares are over ALL emitted rows, not only rows carrying a trade: most
transitions in this tape are quote updates with no trade, and a metric that hid them would
overstate how much of the session is classified. The replay covers a bounded number of
instruments so this page stays a page rather than a batch job — the count is stated above
rather than implied away.</footer>
</body></html>"""
