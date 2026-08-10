# Regime playbook instruction engine — how the brain authors its own trading instructions

**Seed (operator, 2026-08-10):** *"instructions for the main AI bot to create, depending on the market
regime — flat / volatile / bull / bear — on how logically and smartly people can make money by following
some instructions, even in a flat market. E.g. if a price is fluctuating between 1 or 2 points, create
instructions to enter and exit taking those 2 points as profit repeatedly. Like this example there are
many more, depending on regime and segment."*

**Verdict: ③ NEW (the generator) + ④ DISPROVED (the specific 2-point example).** Both recorded below —
the generator is a genuine upgrade; the example is `D.01` and fails on arithmetic, but the *intent* behind
it survives with one change of units and one change of vehicle.

---

## 1. Why the 2-point example fails, and what rescues it

| | |
|---|---|
| NIFTY ≈ 24,000, target 2 points | **0.0083%** |
| Cash round-trip cost | **0.06–0.11%** (6–11 bps) |
| Option premium round-trip | **0.25–0.65%** (25–65 bps) |
| Verdict | target sits **8–13× below breakeven** — every repetition is a guaranteed net loss |

Repeating a below-breakeven trade does not accumulate profit; it accumulates the cost. This is the exact
mechanism behind SEBI's finding that 80% of traders placing 500+ trades a year are net-negative, and
behind Carver's measured collapse from a pre-cost Sharpe of 28.8 to nothing after costs.

**What rescues the intent — two corrections:**

1. **Units.** "2 points" is not a target, it is an artefact of the instrument's price. 2 points on a ₹50
   stock is **4%** and highly tradeable; 2 points on NIFTY is 0.008% and fatal. **Every instruction states
   its target in basis points relative to price, and every instruction carries a cost precondition.** A
   range is tradeable only when `range_width_bps > cost_bps × 1.5` (A.12).
2. **Vehicle.** The professional way to profit from "nothing is happening" is not to scalp the noise — it
   is to **sell time**. In a flat market, theta pays you for the passage of time rather than requiring
   movement you have already observed is absent. This is the variance-risk-premium harvest, the most
   durable edge in the options complex, and it captures the operator's exact intent through a mechanism
   that survives costs.

So the flat-market playbook has **two** legitimate entries, not zero: range mean-reversion where the range
is genuinely wide enough, and theta harvest where implied volatility exceeds forecast realized volatility.

---

## 2. What an "instruction" is (the executable contract)

An instruction is **not** free-form LLM text and **not** a human-readable tip. It is a machine-executable,
falsifiable, cost-aware rule object that the gatekeeper can validate and the holon can run:

```
INSTRUCTION
  id, regime, segment, mechanism_statement   ← WHY this should work; no mechanism, no instruction
  trigger        precondition that must be true for this to be live at all
  entry          the condition that opens
  sizing         derived from confidence, cost floor, and risk budget
  exit_target    in BPS, never in absolute points
  exit_stop      in BPS
  exit_time      hard time-stop; signals decay in minutes
  invalidation   what proves this instruction wrong TODAY (regime flip, IV collapse, gap)
  cost_gate      required: expected_edge_bps > (cost + spread + slippage) × 1.5
  ttl            instructions expire; a stale instruction is deleted, not queued
  status         proposed → validated → armed → decaying → retired
  track_record   hit rate, net edge, sample N — the thing that gets it retired
```

**The hard rules governing every instruction:**

- **Generation is distributed, judgement is central.** Each holon's research agent may author instructions;
  every one passes the *single* deterministic gatekeeper before it can arm (L14.36).
- **An instruction is a hypothesis, never a licence.** It is traded only after clearing DSR, CPCV and the
  net-EV gate — never on first sighting.
- **No instruction may state a target in absolute points.** Basis points only.
- **Every instruction declares its mechanism.** "Prices bounce here" is not a mechanism; "market makers
  defend this strike because of gamma positioning" is. Mechanism-backed instructions get a lower
  statistical hurdle than pure pattern matches (Harvey-Liu-Zhu).
- **Instructions decay.** Each carries a TTL and a live track record; when its edge stops clearing costs
  it is retired automatically, not left running.

---

## 3. The playbook matrix — 4 regimes × 6 segments = 24 cells

The operator's "there are many more depending on regime and segment" is exactly this matrix. Each cell
needs its own instruction set because the same regime pays differently in different instruments.

### FLAT / RANGE-BOUND — *the operator's example regime*

| Segment | What actually pays | Instruction sketch |
|---|---|---|
| **Cash intraday** | range mean-reversion, **only if the range is wide enough** | trigger: ADX below its own low percentile AND `range_width_bps > cost×1.5`; entry: fade a touch of the band edge with a rejection confirmation; target: mid-range in bps; stop: beyond the band; time-stop 4–8 min |
| **Index options** | **theta harvest — the home engine of this regime** | trigger: VRP positive (IV > forecast RV) AND range-bound; entry: defined-risk iron condor / short strangle at a calibrated delta; target: a percentage of max credit; stop: a multiple of credit; always square off intraday |
| **Stock options** | theta **not permitted** (A.15) → directional or vertical only | trigger: range edge + own-name catalyst absent; entry: debit vertical, defined risk |
| **Index futures** | range mean-reversion at index level; **cheapest vehicle in bps terms** | same geometry as cash but on one instrument with tighter spreads |
| **Stock futures** | mostly stand down — thin liquidity punishes range-trading | trigger requires top-tier liquidity only |
| **MCX** | range mean-reversion around inventory-cycle equilibria | commodity ranges are wider in bps; cost floor easier to clear |

### VOLATILE / EXPANSION

| Segment | What pays | Instruction sketch |
|---|---|---|
| **Cash intraday** | breakout continuation, wider stops, smaller size | trigger: ATR percentile high + volume surge; size *down* as volatility rises so risk stays constant |
| **Index options** | **long gamma / long vega** | trigger: IV below forecast RV (cheap vol) + compression breaking; entry: long straddle/strangle; the one regime where buying premium is right |
| **Stock options** | long directional premium on event-driven names | trigger: event + IV not yet inflated |
| **Index futures** | momentum continuation | trigger: range expansion confirmed over N bars |
| **Stock futures** | avoid — gap risk on physical settlement | |
| **MCX** | event-driven expansion (inventory data, OPEC) | scheduled-event playbooks |

### BULL TREND

| Segment | What pays | Instruction sketch |
|---|---|---|
| **Cash intraday** | momentum, ORB-long, pullback-to-VWAP entries; **the regime where overnight carry is most often justified** (R.01) | trigger: trend gauge high + breadth confirming; carry promotion evaluated at square-off |
| **Index options** | bull-call spreads, long CE; **not** naked short puts | defined risk always |
| **Stock options** | directional CE on relative-strength leaders | |
| **Index futures** | long, and the **hedging instrument** for the cash book | |
| **Stock futures** | long on the liquid few | |
| **MCX** | trend-follow in the trending commodity | |

### BEAR TREND

| Segment | What pays | Instruction sketch |
|---|---|---|
| **Cash intraday** | breakdown-short — **but intraday shorting in cash equity has borrow and square-off constraints**; verify before relying on it | |
| **Index options** | bear-put spreads, long PE — **also the regime where volatility rises, so long premium is doubly favoured** | |
| **Stock options** | long PE on breakdown names | |
| **Index futures** | short — **the cleanest bearish expression available**, no borrow constraint | likely the winner of expression-selection (L14.29) in this regime |
| **Stock futures** | short on liquid names | |
| **MCX** | short the weakening commodity | |

**The cross-cutting observation:** the *same* bearish conviction has a different best vehicle from the
*same* bullish conviction, because shorting cash equity intraday is constrained while shorting index
futures is not. This is precisely what the expression-selection engine (L14.29) exists to discover, and
the playbook matrix is where its results get written back.

---

## 4. Three tiers

- **BASE 🟩** — a hand-authored instruction set per cell (24 cells), each with an explicit mechanism, a
  cost precondition and bps-denominated targets, all validated before arming. Effectively a codified,
  falsifiable trading manual the machine can execute.
- **ADVANCED 🚀** — each holon's research agent **authors new instructions** for its own cells from what it
  observes and reads online; the gatekeeper validates; live track records promote and retire them
  automatically. The manual writes itself and prunes itself.
- **ULTRA 🌌** — instruction *discovery*: symbolic regression and evolutionary search over the trigger and
  exit space, generating candidate instructions no human wrote, gated by the same deterministic evaluator.
  Combined with the expression selector, the system learns not only *what* to do per regime but *through
  which instrument* to do it. This is the breeder bot (L14.21 meta tier) pointed at instructions.

## 5. Where this plugs in

Regime reader (per holon, L14.37) → **playbook instruction engine** → candidate instruction → gatekeeper
(central, deterministic) → armed instruction set for that holon and regime → entry decisions → realized
outcome → instruction track record → promote / decay / retire. The expression selector (L14.29) chooses
the vehicle when the same instruction is available across segments.
