# The tradeable-unit denominator — and the per-segment play catalog

**Seed (operator, 2026-08-10):** *"You have not considered all the possibilities. Instead of focusing on
the index, what about the contract of the index option — like 10th August 24000 CE at 100, fluctuating
90-95-100-105-100? Then we can utilise that. Same for the rest of the segments and other things — more
examples."*

**Verdict: ② SUPERIOR VERSION, and it corrects an error in my own prior analysis.** The D.01 arithmetic in
`regime_playbook_instruction_engine.md` computed a 2-point target against the *index* level of ~24,000.
That was the wrong denominator. The traded instrument is the **option contract**, priced around ₹100, so a
5-point move is **5% of the thing actually being traded** — not 0.008% of something it merely references.

---

## 1. The general principle that was missing

> **The percentage that matters is the percentage of the INSTRUMENT YOU TRADE — never the percentage of
> the thing it references.**

Every viability calculation in this project must state which of **three different denominators** it is
using, because they differ by an order of magnitude or more:

| Denominator | What it is | Where it governs |
|---|---|---|
| **Notional value** | what the position is worth in the underlying | exposure limits, portfolio risk, correlation |
| **Ticket value** | what is actually laid out — premium × lot size for options | **scalp viability, cost-as-percentage, minimum profitable move** |
| **Margin posted** | capital blocked — SPAN + exposure for futures, full cash for CNC | **return on capital, expression selection (A.12)** |

The prior analysis conflated notional with ticket. This entry fixes it: **cost-as-percentage is always
computed against the ticket**, and **return on capital is always computed against the margin**.

## 2. The flat-brokerage inversion (the non-obvious consequence)

Zerodha's ₹20-per-order is **fixed**, so it is a percentage that explodes as the ticket shrinks:

| Premium | Lot | Ticket | Round-trip brokerage | As % of ticket |
|---|---|---|---|---|
| ₹200 | 65 | ₹13,000 | ₹40 | **0.31%** |
| ₹100 | 65 | ₹6,500 | ₹40 | **0.62%** |
| ₹30 | 65 | ₹1,950 | ₹40 | **2.05%** |
| ₹10 | 65 | ₹650 | ₹40 | **6.15%** |

**This inverts the common retail instinct.** Cheap far-OTM options *feel* safer because they cost less in
rupees, but they are the **worst** scalping vehicle in the entire universe — the fixed cost alone eats
several percent per round trip before spread or slippage. Higher-premium near-ATM contracts, which feel
expensive, are the only viable ones.

**Rule derived:** every option instruction carries a **minimum ticket value** precondition, calibrated so
the fixed-cost component stays below a set fraction of the target. Never a hardcoded rupee floor —
derived from the live brokerage schedule and the instruction's own target.

## 3. The second gate: spread as a percentage of premium

Option bid-ask spreads are far wider in percentage terms than cash spreads, and they scale with illiquidity
rather than with price:

- Liquid ATM weekly NIFTY: spread ≈ ₹0.05–0.25 on ₹100 → **0.05–0.25%** — negligible.
- Illiquid strike or far expiry: spread ≈ ₹2–5 on ₹50 → **4–10%** — instantly fatal.

So **liquidity tier is a hard gate on any premium-scalping instruction**, not a preference. The playbook's
`cost_gate` for options must read the *live* spread, never a modelled constant.

---

## 4. Per-segment play catalog — the "more examples" expanded

### Index options — the operator's example, plus its siblings

| Play | Mechanism | Viability note |
|---|---|---|
| **Premium oscillation scalp** ⭐ | ATM premium mean-reverts intraday while the index chops; 5% swings on a ₹100 premium clear a ~1% cost floor | requires: liquid ATM, ticket above the minimum, live spread tight |
| **Delta-neutral gamma scalp** 🚀 | hedge delta with futures, harvest realized-vs-implied vol; profits from *movement* regardless of direction | the professional version of "trade the oscillation" — needs the futures holon |
| **Intraday IV crush** 🚀 | IV inflates before a scheduled event and collapses after; sell the inflation, buy back post-event | needs the calendar bot |
| **Expiry-day pinning** 🚀 | price gravitates to max-pain strikes as gamma positioning forces market-maker hedging | genuine mechanism, not a pattern — dealer hedging is the *why* |
| **Adjacent-strike spread scalp** 🌌 | relative mispricing between neighbouring strikes | needs the surface engine |
| **Maker-side premium capture** 🌌 | quote both sides on a liquid strike, earn the spread rather than pay it | the one durable small-edge lever (L1.14) |

### Stock options
Same premium arithmetic, but **worse ticket economics** (thinner premiums outside the top names) and
physical settlement. Directional and vertical only (A.15). The premium-oscillation play is viable **only**
in the ~20–30 genuinely liquid underlyings; elsewhere the spread gate kills it.

### Index futures — a *third* denominator

Futures have no premium; the relevant denominators are **contract notional** for P&L and **SPAN margin**
for return on capital. A NIFTY future at 24,000 × 65 ≈ ₹15.6 lakh notional on roughly ₹1.2–1.5 lakh
margin — **~10–12× leverage.** So a **0.5% index move ≈ 5–6% return on capital posted.**

| Play | Mechanism |
|---|---|
| **Margin-efficient index momentum** ⭐ | small index moves become large returns on margin; the cheapest bps-per-rupee directional expression |
| **Cash-futures basis convergence** 🚀 | basis narrows into expiry; a near-arbitrage with a real mechanism |
| **Roll-week calendar spread** 🚀 | systematic roll pressure distorts the near-far spread |
| **The bearish expression of choice** ⭐ | shorting index futures has no borrow constraint, unlike cash equity intraday |
| **The hedging instrument** ⭐ | how a long cash book gets delta-neutralised, now that cash can carry overnight |

### Stock futures
Same margin leverage, but physically settled with escalating expiry-week margin, and liquid in only a few
dozen names. Basis plays exist per-name but the liquidity gate is severe.

### Cash intraday
The one segment where instrument and reference are the same thing, so percentage-of-price is correct — and
the segment where my original arithmetic *was* right. Cost floor 6–11 bps; the range-width precondition
(L11.99) applies as written.

### MCX / commodities
Margin-based like futures, with two structural quirks worth their own plays:

| Play | Mechanism |
|---|---|
| **Open-gap follow-through** 🚀 | MCX opens at 09:00 after COMEX/international moves overnight — a genuine information gap, not a priced-in one |
| **Inventory-release volatility** 🚀 | scheduled crude inventory data produces repeatable volatility expansion |
| **Late-session international overlap** 🚀 | the 17:00–23:30 window overlaps live US trading; different regime from the morning session |
| **Gold-silver ratio** 🌌 | a relative-value pair with a long mean-reverting history |

---

## 5. What changes in the catalog

1. **`L11.98` is amended** — targets are in basis points *of the traded instrument's ticket*, with the
   denominator named explicitly on every instruction. The rule was right; the worked example behind it
   was computed on the wrong base.
2. **`D.01` is narrowed** — small-target scalping is disproved **in cash equity and in index-level terms**,
   which is where the Carver and SEBI evidence actually applies. It is **not** disproved for option
   premium at adequate ticket size and liquidity, where the percentage move is an order of magnitude
   larger. The distinction is the denominator.
3. **Two new hard preconditions on option instructions** — minimum ticket value (the flat-brokerage gate)
   and live spread as a percentage of premium (the liquidity gate).
4. **Expression selection (L14.29) gets sharper** — with three denominators made explicit, "net edge per
   rupee of capital" is now computable across vehicles that were previously being compared on
   incompatible bases.
