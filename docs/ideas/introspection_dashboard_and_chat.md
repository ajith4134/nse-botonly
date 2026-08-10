# The introspection dashboard + project chat

**Seed (operator, 2026-08-10):** *"Ultra-advanced dashboard with real description of what each feature or
bot is doing, explaining its internal working — research details, hypothesis, learning etc — true to what
is happening in this project. And an AI chat section where I can talk to the entire project, with its LLM
connected to Claude Max subscription first, cloud LLM next, local LLM last, for everything."*

**Verdict: ② SUPERIOR VERSION of L13.04/L13.05 and L13.23.** The existing feature-catalogue dashboard
showed *status* measured from the code's AST. This asks for something categorically harder: **what each
bot is thinking, researching and learning** — reasoning, not state.

---

## 1. The distinction that makes this hard

| | Status dashboard (what exists) | **Introspection dashboard (this)** |
|---|---|---|
| Answers | Is it running? What is the P&L? | **What is it thinking? Why did it do that? What is it learning?** |
| Source | AST scan, process probes, ledger | **Decision traces the bots emit at decision time** |
| Reconstructable after the fact? | yes | **no** |

**⚠️ The load-bearing consequence:** reasoning **cannot be reconstructed after the fact**. A panel that
claims to show "why the bot entered this trade", built by inspecting the trade record afterwards, is a
plausible-looking fabrication — it shows what a reasonable bot *might* have thought, not what this one
did. So this feature is **not primarily a UI feature**. It is an instrumentation contract on every bot,
with a UI on top.

**The decision-trace contract (required of every bot before any panel is built):** at every decision, emit
a structured record of `inputs consulted (with provenance and timestamps) · candidate actions considered ·
gates evaluated and their verdicts · the chosen action · the confidence · the mechanism invoked · the
counterfactual (what would have changed the decision)`. Append-only, point-in-time, replayable. This
extends Rule R from *"status is measured, never hand-authored"* to **"reasoning is recorded, never
reconstructed."**

---

## 2. The four levels

### Level 0 — The organism at a glance
Hero number: **net P&L, paper and live split** (a hero number, not a chart — a single headline needs no
plot). Six holon cards, one per main bot: current activity in one live line · regime read with confidence ·
armed instruction count · open positions · conductor residency (HOT / WARM / COLD) · today's P&L · health.
Plus the conductor's own strip: who is resident right now, who is queued, what was evicted and why.

### Level 1 — Inside one holon
What it is doing *right now*, in words · universe-scan funnel (scanned → liquidity-passed → cost-passed →
triggered) · regime read over the session · its BULL / BEAR / TRAIL organ states side by side · armed
instructions with live track records · open positions each with the mechanism that opened it · its
research agent's active hypotheses · budget burn (capital, compute, API).

### Level 2 — Inside one engine
For each engine: **inputs consumed · algorithm and parameters in force · state carried · outputs produced ·
when it last ran · what changed since**. Research details: what the research agent read, what it concluded,
with source provenance. **Hypothesis board:** every instruction on its lifecycle stage (`proposed →
paper-trial → significant → shadow → reduced-live → full-live → decaying → retired`) with sample N,
Deflated Sharpe and holdout status. **Learning progress:** calibration reliability diagrams, Brier
decomposition, drift-detector state, model version history.

### Level 3 — The decision trace ("why did it trade?")
A replayable per-trade timeline: signal → evidence consulted → each gate and its verdict → sizing →
entry → exits considered and rejected → exit. SHAP attribution for the directional call. **Which gate came
closest to blocking it** — the near-miss is usually more informative than the pass.

---

## 3. Form selection — the data's job picks the chart

*Form first, color last. Several of these are deliberately **not** charts.*

| What is being shown | Job | Form |
|---|---|---|
| Net P&L headline | single headline | **hero number**, no plot |
| P&L over the session | change over time | line + crosshair tooltip |
| Per-holon P&L comparison | magnitude across identity | horizontal bar, one fixed hue per holon |
| Universe-scan funnel | successive filtering | stage bars with counts + pass-rate labels |
| Regime probability over session | composition over time | stacked area, 2px surface gap between bands |
| Instruction track records | many small comparisons | **small multiples** of sparklines, not one crowded chart |
| Hypothesis pipeline | stage counts | stage bars, ordered by lifecycle |
| Calibration quality | predicted vs actual | reliability diagram — scatter + diagonal reference line |
| Cost attribution | part-to-whole | stacked bar (edge / fees / slippage / impact), 2px gaps |
| MFE/MAE excursion | distribution + relationship | scatter with quadrant reference lines |
| Cross-holon exposure overlap | magnitude on a matrix | heatmap, single-hue sequential |
| Bot residency (HOT/WARM/COLD) | state | **status palette + icon + label**, never color alone |
| Bot health | state | status palette, reserved colors |
| "What it is doing right now" | narrative | **plain text**, live-updating — the most valuable panel here is not a chart |

### Color rules, fixed at design time

- **Six holons = six categorical hues in fixed order, never cycled.** Colour follows the **holon**, never
  its rank — filtering to three holons must not repaint the survivors.
- **Status colours are reserved** (good / warning / serious / critical) for bot health and residency, and
  are never reused as a seventh series.
- **Sequential = one hue light→dark** for heatmaps. **Diverging = two hues with a neutral grey midpoint**
  for P&L above/below zero. Never a rainbow, never a hue at the diverging midpoint.
- **One axis, always.** P&L and trade count are two charts, never a dual-axis chart.
- **Text wears text tokens**, never the series colour.
- **Validate the palette with the script**, in both light and dark mode, before shipping. Do not eyeball
  colourblind-safety.
- Legend present for ≥2 series; ≤4 series also directly labelled; every chart has a **table view**; dark
  mode is a designed variant with its own validated steps, not an automatic inversion.
- Hover layer by default: crosshair + tooltip on lines and areas, per-mark tooltip on bars, dots and cells.

---

## 4. The chat panel — talking to the whole project

**Routing (operator, 2026-08-10):** ① **Claude Max subscription** → ② **free-tier cloud** → ③ **local**,
identical to the system-wide ladder (L11.64). Streaming replies.

**Grounded, never generative about facts.** The assistant answers from **read-only state tools** and
**cites what it read**: `get_holon_state` · `get_positions` · `get_pnl` · `get_regime` ·
`explain_last_trade` (the decision trace + SHAP) · `get_instruction_status` · `get_hypotheses` ·
`get_engine_health` · `query_ledger` · `get_research_findings` · `get_conductor_state` · `get_backlog`.
**It never states a number it did not read from a tool.** A grounded assistant that occasionally says "I
don't have that" is worth more than a fluent one that invents plausible figures about your money.

**Questions it must answer:** *"Why did the index-options bot buy that call?"* · *"What is the cash bot
researching right now?"* · *"Which instructions graduated this week and which decayed?"* · *"What is my
open risk across all six holons?"* · *"Why is the MCX bot flat today?"* · *"What did the news organ see on
RELIANCE?"* · *"Which of my six bots is actually making money?"*

**Action layer — gated.** It may **propose** (pause an engine, flatten a position, change a parameter);
the operator confirms; the order still routes through the risk gate. Default read-only. It has no path to
capital that skips the deterministic gate — the same invariant as everywhere else.

**Security:** the assistant is a *privileged reader* of internal state. It must never expose `.env` or
credentials, its tool set is an explicit allowlist, and operator chat input is trusted while everything
the research organs ingested remains behind the dual-LLM quarantine.

---

## 5. Three tiers

- **BASE 🟩** — the four levels with real decision traces behind them, the form and colour rules above, and
  a grounded read-only chat.
- **ADVANCED 🚀** — replayable decision timelines scrubable to any moment in the session; live-vs-paper
  divergence per instruction; the near-miss gate analysis; inline charts rendered by the assistant on
  request.
- **ULTRA 🌌** — the assistant becomes proactive ("the stock-futures bot's edge has decayed three sessions
  running — want me to demote it?"), narrates the ledger into lessons, and the dashboard reorganises itself
  around whatever currently matters most rather than a fixed layout.

## 6. The honest constraint

A dashboard claiming to show "true internal working" is only as truthful as the instrumentation beneath
it. **Build the decision-trace contract first, the panels second.** Panels built ahead of traces will show
something — and that something will be a reconstruction, which is exactly the kind of confident fiction
this project's own Rule R was written to prevent.
