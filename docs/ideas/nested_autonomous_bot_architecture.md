# Nested autonomous bot architecture — every capability becomes a bot

**Seed (operator's words, 2026-08-10):** *"Instead of having features I need to make them into AI advanced
bots for the main features with its own architecture and data, and the features in that bot that support
them. Instead of a news research feature, a news research BOT which autonomously goes online, searches the
web, browses Moneycontrol and other stock news, expert suggestions etc. Instead of one bot handling all
segments — cash, options, futures, commodities — each segment is its own bot, with its own news research,
strategies etc inside it."*

**Standing instruction restated by the operator:** *"when I say example or etc, I am pointing you a
direction on how to find out others like the example — across the entire project."*

**Verdict against `ajith_final_plan.md`: ② SUPERIOR VERSION**, at the largest possible scope. It does not
replace one entry; it replaces the catalog's **organizing principle**. See §7.

---

## 1. Where the two examples sit on the map

Strip the examples away and the core request is:

> **Every capability in the system becomes an autonomous agent that owns its goal, its data, its models,
> its memory, its lifecycle and its own sub-agents — and agents nest, so an agent is simultaneously a
> whole and a part of a larger one.**

That structure has a formal name the corpus never used: a **holonic architecture**. A *holon* is a unit
that is a complete autonomous whole when viewed from below and a component when viewed from above.
Holonic manufacturing systems (Koestler's term, adopted by industrial control in the 1990s) exist
precisely because purely hierarchical systems are rigid and purely flat multi-agent systems are chaotic.
The operator has independently arrived at the standard answer to that trade-off.

- **Category one level UP:** organisational architecture for autonomous systems — how an intelligence is
  decomposed into agents at all. Siblings the operator has not named: subsumption architecture (Brooks),
  blackboard systems, Contract-Net / market-based task allocation, the firm-as-agents org-chart model,
  the biological cell → tissue → organ → organism ladder.
- **Category one level DOWN:** the anatomy of a single bot — what actually separates a *bot* from a
  *function*. Answered in §3.

**The reframe worth more than the taxonomy:** this project already built three complete cognitive trunks
that looked like over-engineering for a feature-based system — **VII CONSCIENCE (14/14)**, **VIII
SENTIENCE / global workspace (13/13)** and **X AUTOPOIESIS (9/9)**. Under a bot architecture they stop
being philosophical decoration and become load-bearing infrastructure:

| Built trunk | What it was, as a feature | What it becomes, under bots |
|---|---|---|
| VIII global workspace + broadcast bus | an integrator over advisory signals | **the inter-bot nervous system** — the coordination substrate every multi-agent system needs |
| VII conscience + referee + off-switch | safety gates on one loop | **the government over N autonomous agents** — the thing that stops a rogue bot |
| X autopoiesis (registry, health, hazard, repair, supervision tree) | component lifecycle monitoring | **the bot lifecycle manager** — spawn, health-check, restart, quarantine, retire |
| VI society (consensus, reputation, governance) | LLM desks arguing | **the actual inter-bot political system** |

The hardest infrastructure for this idea is already designed and archived. That is a strong argument the
architecture is right, not a coincidence.

---

## 2. The FULL taxonomy — everything that becomes a bot

⭐ operator named it · ✅ direct sibling · 🚀 advanced · 🌌 ultra/frontier · **[✓]** already exists as a bot

### Tier A — SEGMENT bots (the operator's second example, completed)

| Bot | Notes |
|---|---|
| ⭐**[✓]** NSE cash-intraday bot | exists (`L5.26`) |
| ⭐**[✓]** NSE index-options bot | exists (`L5.27`) |
| ⭐**[✓]** NSE stock-options bot | exists (`L5.28`) |
| ⭐ NSE index-futures bot | **scope change** — futures were explicitly deferred in the old plan |
| ✅ NSE stock-futures bot | physically settled; different risk shape from index futures |
| ⭐ Commodity bot (MCX) | **⚠️ different exchange** — separate segment, separate margin regime, separate hours (up to 23:30 IST), Kite supports MCX but it is not NSE |
| ✅ Currency-derivatives bot (USDINR, EURINR) | NSE segment; RBI-driven rather than equity-driven |
| 🚀 BSE index-options bot (SENSEX / BANKEX) | the only *weekly* expiry outside NIFTY — structurally valuable, not a duplicate |
| 🚀 ETF bot | liquid, low-cost, different microstructure |
| 🚀 Cash-delivery / multi-day bot | **⚠️ violates the intraday-only non-negotiable** — needs an explicit decision |
| 🌌 SME / illiquid-names bot | wide spreads, requires a different execution model entirely |
| 🌌 Pre-open / auction bot | the 09:00–09:15 call auction is its own game |
| 🌌 Expiry-day specialist bot | cuts across segments; arguably its own regime rather than its own segment |

### Tier B — PERCEPTION bots (the operator's first example, completed)

Each autonomously acquires, verifies and publishes a typed view of the world.

| Bot | What it hunts |
|---|---|
| ⭐ **News-research bot** | RSS, Moneycontrol, ET, BusinessLine, live browsing, anti-bot evasion |
| ✅ **Corporate-filings bot** | NSE/BSE announcements — the fastest *free regulatory ground truth*, keyed by symbol so no NLP disambiguation is needed |
| ⭐ **Expert / analyst-call bot** | broker ratings, target prices, upgrades and downgrades, consensus estimates |
| ✅ **Tipster / social bot** | Telegram, fintwit, Reddit, TradingView ideas — every source scored as a falsifiable hypothesis |
| ✅ **Global-markets bot** | GIFT Nifty, US close, Asia live, Europe, ADRs of Indian names |
| ✅ **Macro bot** | RBI policy, CPI/WPI/GDP/IIP, USDINR, DXY, crude, US10Y |
| ✅ **Flow bot** | FII/DII cash flows, participant-wise OI, bulk and block deals, promoter pledging |
| 🚀 **Options-surface bot** | IV surface, skew, term structure, PCR, max-pain, VRP |
| 🚀 **Microstructure bot** | depth, OFI, microprice, VPIN, sweeps |
| 🚀 **Sector-rotation bot** | sector indices, peer moves, relative strength |
| 🚀 **Universe bot** | instrument master, lot sizes, strike intervals, F&O eligibility, ban list, ASM/GSM, circuits |
| 🚀 **Calendar bot** | expiry, holidays, results dates, budget, RBI meetings, Muhurat |
| 🌌 **Concall / transcript bot** | earnings-call audio → transcript → guidance extraction |
| 🌌 **Regulatory-watch bot** | SEBI circulars that change the rules the system operates under — it reads its own rulebook |
| 🌌 **Alt-data bot** | satellite, shipping, electricity, app-download, hiring signals → sector nowcasts |

### Tier C — DECISION bots

| Bot | Role |
|---|---|
| ⭐**[✓]** BULL bot · BEAR bot | exist as specs (`L11.09/10`) — already own-architecture by design |
| ✅ **Arbiter** | **must stay deterministic — see §5, this is where the disproved trap lives** |
| ✅ **Regime bot** | reads which of the four market states is live, with what confidence |
| ✅ **Trend bot · Mean-reversion bot · Breakout bot · Premium-seller bot** | one per regime archetype |
| 🚀 **Volatility bot** | long-gamma and vol-expansion specialist |
| 🚀 **Structure-inventor bot** | searches the option leg space to compose novel structures rather than picking from a library |
| 🚀 **Radar bot** | full-universe scan, surfaces candidates to whoever can trade them |
| 🌌 **Dispersion bot · Stat-arb bot · Event bot · Pairs bot** | cross-sectional and relative-value specialists |
| 🌌 **Market-making bot** | two-sided quoting to earn the spread |

### Tier D — SURVIVAL bots

| Bot | Role |
|---|---|
| ✅ **Risk bot** | the gate nothing bypasses; portfolio-level, above all segment bots |
| ✅ **Cost bot** | prices every proposed trade before it is allowed to exist |
| ✅ **Execution bot** | order placement, slicing, fills, partial-fill tracking |
| ✅ **Reconciliation bot** | believes the broker, not local state, on every restart |
| ✅ **Margin bot** | SPAN plus exposure, and what is affordable *right now* |
| ✅ **Compliance bot** | SEBI limits, Algo-ID, order-rate ceiling, MWPL |
| 🚀 **Kill-switch watchdog** | a **separate process** — a watchdog inside the thing it watches is not a watchdog |
| 🚀 **Netting bot** | stops segment bots trading against each other |

### Tier E — META bots (what an expert adds, and the operator has not named)

This tier is the real answer to "think ahead and find the others."

| Bot | Role | Why it matters |
|---|---|---|
| ✅ **Gatekeeper / validation bot** | runs DSR, CPCV, MinBTL, PBO, FDR on every candidate | the hard evaluator — the only thing standing between "a bot had an idea" and "capital moved" |
| ✅ **Treasurer / allocator bot** | distributes capital and risk budget across all bots | the bots' economy |
| ✅ **Librarian / memory bot** | owns the shared memory, its provenance and its half-life | prevents N bots keeping N contradictory histories |
| 🚀 **Coroner / post-mortem bot** | autopsies every loss and every incident, writes the lesson to memory | the corpus already built the forensic store for this |
| 🚀 **Red-team bot** | actively attacks the other bots' configs to find their fragility | already exists as `L12.10`, never pointed at bots |
| 🚀 **Referee / auditor bot** | enforces the constitution over every bot action | VII CONSCIENCE, repurposed |
| 🚀 **Medic bot** | health, hazard, repair and quarantine of the other bots | X AUTOPOIESIS, repurposed |
| 🚀 **Teacher / curriculum bot** | decides what the others should train on next, targeting their weakest coverage | XII CURIOSITY — currently 0/11 built |
| 🚀 **Scout bot** | hunts new data sources, OSS libraries and techniques the system does not yet have | XVI's unbuilt "tool foundry" |
| 🚀 **Spokesperson bot** | the chat interface — explains what the organism is doing, in language | `L13.23` |
| 🌌 **Breeder / evolution bot** | invents new strategies and new *bots*, gated by the gatekeeper | XI GENERATIVITY |
| 🌌 **Org-designer bot (bot-of-bots)** | decides which bots should exist at all — spawns, merges, retires them | the true top of the recursion; V SELF, currently 0/12 built |
| 🌌 **Historian bot** | maintains the fossil record of every bot that ever lived and why it died | so the system cannot repeat its own extinct mistakes |

---

## 3. What separates a BOT from a FEATURE (the anatomy contract)

Without a hard definition, "make it a bot" degrades into renaming functions. A unit qualifies as a bot
only if it owns **all nine**:

1. **A goal** it can succeed or fail at, stated in its own terms.
2. **Its own data pipeline** — what it goes and gets, on its own schedule.
3. **Its own model/algorithm and state** — carried across time, not recomputed per call.
4. **Its own memory** — what it learned, with provenance.
5. **Its own lifecycle** — start, health, degrade, repair, retire.
6. **Its own evaluation** — a track record it is scored on, that can get it defunded.
7. **Its own budget** — compute, API calls, capital or risk, which it can exhaust.
8. **A published contract** — the typed interface others consume; internals private.
9. **Its own autonomy level** — advisory, gated, or armed; earned, never granted by default.

Anything lacking these is a **capability** the bot uses, not a bot. This is the test that keeps the
architecture honest.

---

## 4. The nesting topology — and the shared-vs-owned boundary (the hard problem)

The operator's phrasing — *"options bot which has its own news research"* — has a naive reading that
fails badly, and a professional reading that is exactly right.

**Naive reading:** every segment bot runs its own crawler. With 8 segment bots that is 8 independent
scrapers hitting Moneycontrol, 8× the cost, 8× the ban and rate-limit exposure, and — worst — **8
mutually inconsistent versions of what the news says**, so the options bot and the cash bot can act on
contradictory world-states in the same second. This is the documented *context-fragmentation* failure of
naive multi-agent systems.

**Professional reading, and the rule this architecture adopts:**

> **Acquisition is shared and singular. Interpretation is owned and plural.**

One news-research bot goes and gets the world — once, cleanly, point-in-time, into an append-only
evidence store. Every segment bot then runs **its own relevance model, its own weighting and its own
reaction** over that shared evidence. The options bot cares that a result is due tomorrow because it
implies an IV crush; the cash bot cares about the same filing for a gap; the futures bot cares about the
basis. Same fact, three private interpretations. Nobody scrapes twice.

Applied generally:

| Always SHARED (one instance) | Always OWNED (one per bot) |
|---|---|
| raw data acquisition and the evidence store | relevance and interpretation models |
| the instrument/universe master | strategy logic and parameters |
| the cost model (it is exchange arithmetic, not opinion) | feature engineering and signal weighting |
| the risk gate and portfolio limits | position management within its own limits |
| the validation gatekeeper | its own track record and calibration |
| the broker connection and rate-limit budget | its own order intent, before the shared execution bot |
| the memory substrate and its provenance rules | its private memories and lessons |
| the clock and calendar | its own trading schedule inside that calendar |

**Topology:**

```
                        ORG-DESIGNER BOT  🌌 (spawns / retires bots)
                                 │
              ┌──────────────────┼──────────────────┐
        GOVERNANCE          COORDINATION         ECONOMY
     referee · medic     global workspace       treasurer
     red-team · coroner    (the bus)            (capital)
              └──────────────────┼──────────────────┘
                                 │
   ┌────────────┬────────────────┼────────────────┬─────────────┐
 PERCEPTION   SEGMENT BOTS (each a holon)      SURVIVAL      META
  (shared)   ┌──────────────────────────┐     (shared)    gatekeeper
  news       │ cash · index-opt · stock-│      risk        librarian
  filings    │ opt · futures · MCX ·    │      cost        teacher
  flows      │ currency · BSE · ETF     │      exec        scout
  global     │                          │      margin      spokesperson
  macro      │  each OWNS internally:   │      compliance
  surface    │  · its relevance model   │      netting
  universe   │  · its strategy bots     │      watchdog
  calendar   │  · its risk sub-limits   │
             │  · its memory + record   │
             │  · its own BULL/BEAR pair│
             └──────────────────────────┘
```

Three levels of recursion, which is where it should stop: **organism → segment holon → strategy bot**.
Deeper nesting buys nothing and costs coordination.

---

## 5. ⚠️ The trap this idea must not fall into (Part III, D.13/D.14)

The catalog's disproved section contains a finding that sits directly beside this idea, and the distinction
is the difference between an architecture and a known failure:

- **Specialist bots over *different domains and different data* — sound.** A news bot and a microstructure
  bot are not competing; they are perceiving different things. Division of labour is real.
- **Multiple bots *voting on the same question* — disproved.** Budget-matched LLM committees underperform
  (Berkeley MAST: 41–86% failure). TradingAgents, the highest-starred trading repo at 95k, is exactly this
  anti-pattern: shared pretraining priors produce stylistic rather than informational disagreement, which
  amplifies bias instead of cancelling it.

**Therefore, binding constraints on this architecture:**

1. Bots may **propose**; only a **deterministic, non-LLM gate** may decide capital. Unchanged.
2. The **arbiter is deterministic** — never a bot debate. BULL and BEAR are opposing *models*, not
   opposing *arguers*, and both-confident resolves to FLAT.
3. Bots earn autonomy through **track record**, never by declaration. Every bot starts advisory.
4. **No bot self-grades.** Scoring comes from realized P&L and the gatekeeper, because self-correction
   without ground truth measurably degrades (Huang, ICLR 2024).
5. Disagreement between bots is **information about uncertainty**, not a vote to be tallied — it should
   shrink position size, not elect a winner.

---

## 6. The three tiers

### BASE 🟩 — the operator's idea, done properly
Nine segment holons (cash, index-opt, stock-opt, index-fut, stock-fut, MCX, currency, BSE-opt, ETF), each
owning its strategies, its relevance models, its risk sub-limits and its own track record. One shared
perception layer of ~6 acquisition bots publishing to a point-in-time evidence store. Shared survival
bots (risk, cost, execution, compliance) that nothing bypasses. The gatekeeper validating every candidate.
The global workspace as the bus. **This is a real multi-agent trading desk.**

### ADVANCED 🚀 — the organism governs itself
Add the meta tier: treasurer allocating capital across bots by track record via a non-stationary bandit
that *forgets stale edge*; medic running health, hazard and repair over every bot; referee enforcing the
constitution on every bot action; coroner autopsying every loss into shared memory; teacher directing each
bot's training at its own weakest coverage; red-team continuously attacking bot configs; scout hunting new
data sources. Bots are hired, promoted, demoted and fired on measured performance. **The desk now runs
its own HR, treasury, audit and medical departments.**

### ULTRA 🌌 — the organism designs itself
The org-designer bot decides *which bots should exist*: it spawns a specialist when it detects an
unexploited niche, merges two bots whose track records have converged, retires one whose edge decayed, and
splits one whose behaviour has become bimodal. The breeder invents new strategies *and new bot types*,
gated by the gatekeeper. The historian keeps the fossil record so extinct mistakes cannot recur.
Population dynamics — quality-diversity archives, speciation, red-queen coevolution between the breeder
and the red-team. **The system stops being a fixed set of bots and becomes an evolving population, where
the architecture itself is the thing being optimised.** This is what trunks V SELF (0/12 built) and
XI GENERATIVITY (1/11) were always pointing at.

---

## 7. Catalog impact — what this supersedes

This is a **② SUPERIOR VERSION** verdict against the organizing principle, not a single entry:

- **`L5.25–L5.29`** (three segment bots + supervisor) → superseded by the nine-holon segment tier.
- **`L11.22–L11.47`** (news, research, tipster, global, flow organs, currently passive features) →
  superseded by the perception-bot tier with the shared-acquisition / owned-interpretation rule.
- **`L11.09–L11.11`** (BULL/BEAR/arbiter) → confirmed, and now instantiated *per segment holon*.
- **VII / VIII / X** → re-scoped from cognitive faculties of one loop to governance, coordination and
  lifecycle infrastructure for a bot population.
- **A new layer is required.** None of the existing 13 layers holds "the organisation of the agents
  themselves." Proposed: **L14 · ORGANISM — bot anatomy contract, nesting topology, shared-vs-owned
  boundary, inter-bot protocol, autonomy ladder, bot lifecycle, population dynamics.**

## 8. Open decisions this raises

1. **Futures and commodities are a scope change** — both were explicitly deferred, and **MCX is a
   different exchange** with its own hours and margin regime. Confirm they are now in.
2. **Cash-delivery / multi-day bot would violate the intraday-only non-negotiable.** In or out?
3. **Compute:** 5 cores and 28 GB will not host 30+ bots each with its own model. Does the treasurer
   schedule bots in time slices, or do most stay dormant until their regime appears?
4. **Autonomy ladder:** what does a bot have to prove, numerically, to move from advisory to armed?
5. **How many bots at launch** — the full nine segments, or one holon proven end-to-end first?
6. **Does the org-designer bot ever run unsupervised?** A system that can spawn and retire its own
   agents is the single largest autonomy grant in this whole plan.
