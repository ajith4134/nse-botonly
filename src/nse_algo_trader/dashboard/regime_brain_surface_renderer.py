"""`L13.01` renderer — the regime brain as a page a human can read in five seconds.

Built against the dataviz procedure, in its order: form first, colour by job, palette
validated by the script, then marks, then accessibility.

**Forms, chosen by what each number's job is.**

- *The decision* is a single headline, so it is a hero statement, not a chart. Putting a
  one-value answer in a plot buries it.
- *Each classifier's belief* is magnitude across four labelled categories, so horizontal
  bars — labels read left-to-right without rotation, and four small multiples let the eye
  compare panels rather than decode a stacked bar.
- *The combined belief* is the same form at larger size, because it is the same question
  answered once more with authority.
- *Concentration and agreement* are two bounded scalars, so meters, not charts.

**Colour.** The four regimes are an identity (categorical), assigned in fixed slot order
and never cycled — `trending` always blue, `ranging` always orange, whatever the data
does. Armed/abstain state uses the reserved status palette, which is never borrowed for a
series. The categorical set was run through `validate_palette.js` for both surfaces and
passes every check; light mode returns a contrast WARN on aqua and yellow, which
obligates relief, so **every bar carries a visible value label** rather than relying on
fill alone.

**Accessibility.** Four series means a legend is always present, and each bar is directly
labelled, so identity is never colour-alone. Status badges carry a word as well as a
colour. Dark mode is a *selected* second palette stepped for the dark surface, not an
inverted light one, and it wins under both the OS setting and an explicit toggle.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.dashboard.regime_brain_read_model import (
    ClassifierPanel,
    RegimeBrainSnapshot,
)
from nse_algo_trader.regime.market_regime_state import MarketRegime

REGIME_SLOT_ORDER: tuple[MarketRegime, ...] = (
    MarketRegime.TRENDING,
    MarketRegime.RANGING,
    MarketRegime.VOLATILE,
    MarketRegime.QUIET,
)
"""Fixed slot order. Colour follows the ENTITY, never its rank — a regime that happens to
lead this bar must not steal the colour of the one that led last bar."""

_LIGHT_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
_DARK_SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")
"""Validated: all checks PASS on both surfaces. Worst adjacent CVD separation is
ΔE 9.1 protan (light) / 8.4 (dark), above the 8.0 target; normal-vision floor 22.9 / 19.8,
far above the hard-fail line of 15."""

_STATUS_GOOD = "#0ca30c"
_STATUS_WARNING = "#fab219"
_STATUS_MUTED = "#8a8a85"


def _bar_row(regime: MarketRegime, probability: float, slot: int, compact: bool) -> str:
    """One horizontal bar with a direct value label.

    The label is not decoration — it is the relief the light-mode contrast WARN
    obligates, and it also means the chart still reads in greyscale or forced-colors.
    """
    width = max(0.0, min(1.0, probability)) * 100.0
    height = "10px" if compact else "16px"
    return f"""
      <div class="bar-row">
        <span class="bar-label">{escape(regime.value)}</span>
        <span class="bar-track" style="height:{height}">
          <span class="bar-fill series-{slot}" style="width:{width:.2f}%"></span>
        </span>
        <span class="bar-value">{probability * 100:.0f}%</span>
      </div>"""


def _meter(label: str, value: float, caption: str) -> str:
    """A bounded scalar. A meter, not a chart — one number needs no axes."""
    return f"""
      <div class="meter">
        <div class="meter-head"><span>{escape(label)}</span><b>{value * 100:.0f}%</b></div>
        <span class="meter-track"><span class="meter-fill"
              style="width:{max(0.0, min(1.0, value)) * 100:.1f}%"></span></span>
        <p class="caption">{escape(caption)}</p>
      </div>"""


def _classifier_panel(panel: ClassifierPanel) -> str:
    badge_colour = {
        "armed": _STATUS_GOOD,
        "unarmed": _STATUS_MUTED,
        "immature": _STATUS_WARNING,
    }[panel.status_label]
    bars = "".join(
        _bar_row(regime, panel.probabilities.get(regime, 0.0), slot, compact=True)
        for slot, regime in enumerate(REGIME_SLOT_ORDER, start=1)
    )
    return f"""
    <article class="panel">
      <header class="panel-head">
        <h3>{escape(panel.name)}</h3>
        <span class="badge" style="--badge:{badge_colour}">{escape(panel.status_label)}</span>
      </header>
      <div class="panel-meta">
        weight in belief <b>{panel.weight_in_belief * 100:.0f}%</b> ·
        confidence <b>{panel.confidence * 100:.0f}%</b> ·
        {panel.observations_seen:,} obs
      </div>
      <div class="bars">{bars}</div>
      <p class="evidence">{escape(panel.evidence)}</p>
    </article>"""


def render_regime_brain_page(snapshot: RegimeBrainSnapshot) -> str:
    """The whole surface. Server-rendered, self-contained, no external requests."""
    decision = snapshot.decision
    actionable = decision.is_actionable
    hero_colour = _STATUS_GOOD if actionable else _STATUS_MUTED

    panels = "".join(_classifier_panel(panel) for panel in snapshot.panels)
    belief_bars = "".join(
        _bar_row(
            regime,
            snapshot.belief.distribution.probability_of(regime),
            slot,
            compact=False,
        )
        for slot, regime in enumerate(REGIME_SLOT_ORDER, start=1)
    )
    legend = "".join(
        f'<span class="legend-item"><i class="swatch series-{slot}"></i>'
        f"{escape(regime.value)}</span>"
        for slot, regime in enumerate(REGIME_SLOT_ORDER, start=1)
    )
    band_label = (
        "—" if snapshot.deviation_band is None else f"{snapshot.deviation_band:.2f}"
    )
    leading = snapshot.belief.most_likely
    leading_label = leading.value if leading else "no armed classifier — abstaining"

    table_rows = "".join(
        f"<tr><td>{escape(panel.name)}</td><td>{escape(panel.status_label)}</td>"
        f"<td>{panel.weight_in_belief * 100:.0f}%</td>"
        + "".join(
            f"<td>{panel.probabilities.get(regime, 0.0) * 100:.1f}%</td>"
            for regime in REGIME_SLOT_ORDER
        )
        + "</tr>"
        for panel in snapshot.panels
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regime brain — nse-algo-trader</title>
<style>
:root {{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
  --series-1:{_LIGHT_SERIES[0]}; --series-2:{_LIGHT_SERIES[1]};
  --series-3:{_LIGHT_SERIES[2]}; --series-4:{_LIGHT_SERIES[3]};
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
    --series-1:{_DARK_SERIES[0]}; --series-2:{_DARK_SERIES[1]};
    --series-3:{_DARK_SERIES[2]}; --series-4:{_DARK_SERIES[3]};
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  --series-1:{_DARK_SERIES[0]}; --series-2:{_DARK_SERIES[1]};
  --series-3:{_DARK_SERIES[2]}; --series-4:{_DARK_SERIES[3]};
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--surface-0);color:var(--text-primary);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 2px}}
.sub{{color:var(--text-secondary);font-size:13px;margin:0 0 20px}}
.hero{{background:var(--surface-1);border:1px solid var(--border);
border-left:4px solid {hero_colour};
  border-radius:10px;padding:18px 20px;margin-bottom:18px}}
.hero-action{{font-size:26px;font-weight:650;letter-spacing:-.01em}}
.hero-why{{color:var(--text-secondary);font-size:13.5px;margin-top:6px}}
.grid{{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
margin-bottom:18px}};
.panel,.card{{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
padding:14px 16px}};
.panel-head{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
.panel-head h3{{font-size:14px;margin:0;font-weight:600}}
.badge{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
  color:var(--badge);border:1px solid var(--badge);white-space:nowrap}}
.panel-meta{{font-size:12px;color:var(--text-secondary);margin:6px 0 10px}}
.bar-row{{display:grid;grid-template-columns:66px 1fr 40px;align-items:center;gap:8px;margin:5px 0}}
.bar-label{{font-size:11.5px;color:var(--text-secondary)}}
.bar-track{{background:var(--surface-0);border-radius:4px;display:block;overflow:hidden}}
.bar-fill{{display:block;height:100%;border-radius:0 4px 4px 0}}
.bar-value{{font-size:11.5px;color:var(--text-secondary);text-align:right;
font-variant-numeric:tabular-nums}};
.series-1{{background:var(--series-1)}} .series-2{{background:var(--series-2)}}
.series-3{{background:var(--series-3)}} .series-4{{background:var(--series-4)}}
.evidence{{font-size:11px;color:var(--text-muted);margin:10px 0 0;
font-family:ui-monospace,monospace;
  word-break:break-word}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 12px;font-size:12.5px;
color:var(--text-secondary)}};
.legend-item{{display:inline-flex;align-items:center;gap:6px}}
.swatch{{width:11px;height:11px;border-radius:3px;display:inline-block}}
.meter{{margin-bottom:14px}}
.meter-head{{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}}
.meter-track{{display:block;height:8px;background:var(--surface-0);border-radius:4px;
overflow:hidden}};
.meter-fill{{display:block;height:100%;background:var(--text-secondary);border-radius:0 4px 4px 0}}
.caption{{font-size:11.5px;color:var(--text-muted);margin:5px 0 0}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--border)}}
th{{color:var(--text-secondary);font-weight:600}}
td{{font-variant-numeric:tabular-nums}}
details{{margin-top:16px}} summary{{cursor:pointer;font-size:13px;color:var(--text-secondary)}}
.foot{{color:var(--text-muted);font-size:11.5px;margin-top:20px}}
</style></head>
<body><div class="wrap">
  <h1>Regime brain</h1>
  <p class="sub">Instrument {snapshot.instrument_token} · {snapshot.bars_used:,} real bars ·
     measured {snapshot.measured_at:%Y-%m-%d %H:%M} UTC ·
     {snapshot.armed_count} of {len(snapshot.panels)} armed ·
     {snapshot.contributing_count} contributing</p>

  <div class="hero">
    <div class="hero-action">{escape(decision.action.value.replace("_", " "))}</div>
    <div class="hero-why">{escape(decision.reason)}</div>
    <div class="hero-why">conviction <b>{decision.conviction:.3f}</b> ·
      deviation <b>{decision.deviation:.2f}</b> ·
      own band <b>{band_label}</b></div>
  </div>

  <div class="grid">
    <div class="card">
      <h3 style="margin:0 0 10px;font-size:14px">Combined belief — {escape(leading_label)}</h3>
      <div class="legend">{legend}</div>
      {belief_bars}
    </div>
    <div class="card">
      {_meter("Concentration", snapshot.belief.concentration,
              "How peaked the pooled belief is. Low means the panel has no clear view.")}
      {_meter("Agreement", snapshot.belief.agreement,
              "How much the classifiers agreed. Low means split — "
              "an unknown regime is not tradeable.")}
    </div>
  </div>

  <div class="grid">{panels}</div>

  <details><summary>Table view — the same numbers, no colour required</summary>
    <table><thead><tr><th>classifier</th><th>state</th><th>weight</th>
      {"".join(f"<th>{escape(regime.value)}</th>" for regime in REGIME_SLOT_ORDER)}
    </tr></thead><tbody>{table_rows}</tbody></table>
  </details>

  <p class="foot">Every value on this page is read from live engine objects run over real
    bars — armed state from the brain, maturity from each classifier, the decision from the
    mean-reversion engine. Nothing here is hand-authored (R.08). Output is a decision and a
    conviction, never an order: L1 costs and sizing are unbuilt (R.13).</p>
</div></body></html>"""
