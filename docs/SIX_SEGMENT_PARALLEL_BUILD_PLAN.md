# Building all six segment holons at once — and why it carries no drawback

**Decided 2026-08-17 (operator).** Build all six segment bots in parallel rather than proving
cash-intraday first and widening afterwards. Recorded as `A.130`.

**This is not a deviation from the plan — it is a return to it.** `A.01` already reads: *"SIX segment
holons, all built from the start: cash-intraday · index-options · stock-options · index-futures ·
stock-futures · commodities/MCX, each with an independent on/off switch."* `R.10` says the six
segments are equal by default. The cash-first "hybrid build shape" recorded in `ajith_final_todo.md`
was the deviation, and it is superseded here.

---

## The drawback this plan has to kill

The honest objection to building six at once is one sentence:

> Six segments means six unproven decision logics running at the same time, with no working
> reference to compare any of them against.

That objection is fatal **only if each segment owns its own decision logic**. It is void if the
decision logic is singular and shared, and the segments own nothing but what is genuinely different
about their instruments. So the entire plan turns on one structural choice.

---

## 1. One spine, six BOTS — the choice that removes the objection

> **CORRECTED 2026-08-17 on operator instruction.** An earlier draft of this section called the six
> "bots". That under-scoped them and contradicted the plan. `L5.25` is explicit: *"Segment
> holons — autonomous segment **bots**, each owning its **strategies, relevance models, risk
> sub-limits, memory and track record**, under a portfolio supervisor."* `L5.26`/`L5.27`/`L5.28` name
> them `cash_intraday_bot`, `index_option_bot`, `stock_option_bot`. Under `R.23b`, a thing called a
> bot gets the full engine loop — it is not configuration.
>
> **What each segment bot owns, as an engine in its own right:**
> its **strategy set** (the alpha logic for its instruments) · its **relevance model** (which of its
> strategies applies in the current regime) · its **risk sub-limits** · its **memory** (carried state
> across sessions) · its **track record** (its own null, its own evidence, its own position on the
> maturity ladder) · and the irreducible instrument facts in the table below.
>
> **What the spine owns and the bots must NOT reimplement:** cost clearance, sizing, the pre-trade
> risk gate, session risk state, the order path, order expression, the halt latch, the trade-quality
> floor, the decision trace, and the maturity-ladder machinery itself.
>
> That division is what keeps six-at-once safe. The bots are genuinely autonomous engines — but the
> path from a decision to money is singular, proven once, and identical for all six.

**The spine is built once, by one author, serially.** It is the whole decision path, and it is
segment-blind:

| The spine owns | Existing module |
|---|---|
| Signal → conviction | `strategy/` + `regime/` |
| Cost clearance before capital | `cost_gate/pre_trade_cost_gate.py` |
| Position sizing under a vol budget | `sizing/volatility_targeted_position_sizer.py` |
| Pre-trade risk gate | `sizing/pre_trade_risk_gate.py` |
| Session risk state | `sizing/session_risk_state_store.py` |
| The only sanctioned path to a broker | `order_path/crash_safe_order_placer.py` |
| Order expression choice | `order_path/order_expression_selector.py` |
| Halt / arm latch | `order_path/trading_control_latch.py` |
| Trade-quality floor + evidence card | `L5.31` — to build |
| Decision trace | `L13.29` — to build |
| Maturity ladder governing activation | `R.04` mechanism |

**Beyond its strategies, relevance model, risk sub-limits, memory and track record, each segment bot
owns the instrument facts that are irreducibly different about its segment.** That list is short and
finite, which is exactly why six bots do not mean six decision paths:

| Each bot owns | cash | idx-opt | stk-opt | idx-fut | stk-fut | MCX |
|---|---|---|---|---|---|---|
| Tradeable-unit denominator (`L11.106`) | price | premium | premium | contract | contract | contract |
| Lot / tick granularity | 1 share | lot | lot | lot | lot | lot |
| Expiry and roll semantics | none | weekly/monthly | monthly | monthly | monthly | monthly |
| Settlement | delivery | cash | cash | cash | **physical** | **physical/warehouse** |
| Cost model row | NSE-CNC/MIS | NFO-OPT | NFO-OPT | NFO-FUT | NFO-FUT | MCX-FUT/OPT |
| Greeks / IV surface | — | required | required | — | — | required (opt) |
| Venue calendar | NSE | NSE | NSE | NSE | NSE | **MCX (evening session)** |
| Liquidity/spread gate | book | book + OI | book + OI | book + OI | book + OI | book + OI |
| Carry rule under `R.01` | promotable | **never carries** | **never carries** | promotable | promotable | promotable |

**Consequence: six bots multiply strategy and instrument code, not decision-path code.** Six bots
genuinely do six different things — that is the point of them — but there is exactly one path from a
decision to money, and it is proven once for all six. The objection assumed six copies of the
*dangerous* part; there is one.

## 2. Built ≠ armed — the maturity ladder already separates them

`A.01` gives every segment an independent on/off switch and `R.22` requires two keys for live
capital: a passed graduation *and* an explicit operator arm. `R.04` says thin data gates
**activation**, never the algorithm.

So all six get built now, all six paper-trade from day one, and each crosses to real money only when
its own evidence clears its own ladder. **"Six built" and "six risking money" were never the same
event** — the objection quietly assumed they were.

Expected reality: cash-intraday and index-options will graduate long before stock-futures, because
they accumulate evidence fastest. That is the ladder working, not a schedule slipping.

## 3. Every segment is judged against its own null — no sibling reference needed

Instead of "cash-intraday is the reference the others are compared to", each segment carries:

- a **per-segment null model** (the cost-adjusted no-skill baseline for that denominator), and
- a **shadow book** recording what the segment would have done, priced through its own cost row.

A segment is then never asked "are you as good as cash?" — a meaningless question across different
denominators, and exactly the conflation `D.01` records as an error. It is asked "do you beat your
own null after your own costs?" `validation/honest_trial_registry.py` already counts the trials that
question is being asked over.

**This is strictly better than a sibling reference,** because comparing an option-premium strategy to
a cash-equity strategy was never valid in the first place.

## 4. Cash-intraday becomes the canary, not the gate

Cash is the cheapest segment — no Greeks, simplest denominator, most data. It will finish and start
producing evidence first whether or not it is prioritised.

The difference: **when it exposes a spine defect, the fix lands in the spine and all six inherit it
immediately.** Under cash-first, the same defect would be found at the same moment, but the other
five would not exist yet to inherit the fix — they would each rediscover it later. Parallel is
*better* at defect propagation, not worse.

## 5. The conformance suite is what makes six parallel agents safe

The single largest risk of six concurrent authors is six subtly different interpretations of the
contract. It is closed mechanically, not by review discipline:

**`L5.29` the segment-bot protocol ships with a shared conformance test suite that every bot must
pass, unmodified.** One test module, parameterised over all six bots. A bot is not done because its
author says so; it is done when the shared suite is green against it. This is the same mechanism
`R.23c` already demands, applied across authors instead of within one.

Additionally: a bot is pure over injected data — no bot opens a socket or a database, the spine does
— so every one of them is testable on recorded data with no broker. That purity is also what lets six
concurrent agents work without stepping on shared state.

## 6. Build order and parallelism

**Serial (me, one at a time — the shared dependency):**

1. `L5.29` **segment-bot protocol** + conformance suite. Adversarially reviewed *before* any bot
   starts — a protocol defect is now 6× expensive, which is the one genuine new risk this plan
   creates, and reviewing it up front is the mitigation.
2. `L13.29` **decision-trace contract** — already the current frontier, and it belongs to the spine.
   Traces before panels (`A.29`); a segment that cannot explain a decision cannot graduate.
3. `L5.31` **trade-quality floor + evidence card** — the shared minimum below which no segment opens
   a trade.
4. `L5.30` **pod paper lifecycle engine** — breaks the cold-start deadlock so all six populate.

**Parallel (one coding agent per bot, each in its own git worktree, six concurrent):**

5. `L5.26` cash-intraday · `L5.27` index-option · `L5.28` stock-option · index-future ·
   stock-future · MCX.

I own integration, the conformance run, and the `R.05` real-data pass for every one of them. No agent
merges its own work.

**Then, serial again:** Phase 4 proving ground, per-segment graduation, and `R.22` arming — six
independent switches, none of which I can pull.

## 6a. Measured readiness per segment — `docs/research/247`

Audited 2026-08-17 by querying the real stores, not by reading the plan. The full matrix and the
file:line evidence are in `docs/research/247_six_segment_readiness_audit.md`; the operative summary:

| Segment | Master | History | Cost | Specs | Depth tape | Buildable now? |
|---|---|---|---|---|---|---|
| cash-intraday | ✅ 39,727 | ✅ 11.76 M rows | ✅ | ✅ | ✅ 100% | **yes, and armable** |
| index-futures | ✅ | ✅ 115 K | ✅ | ✅ | ❌ | **yes** |
| stock-futures | ✅ ~209 | ✅ 2.9 M | ✅ | ✅ | ❌ | yes, not armable (B3) |
| stock-options | ✅ ~209 | ✅ 158.6 M | ✅ | ✅ | ❌ | yes, not armable (B1, B3) |
| index-options | ✅ 5 | ✅ 20.2 M | ✅ | ✅ | ❌ | yes, not armable (B1) |
| commodities/MCX | ✅ 65,496 | ❌ **zero** | ⚠️ seeded | ⚠️ no calendar | ❌ blocked | build to contract, log blockers |

**The floor is already six-segment-wide,** and that is the strongest evidence `A.130` is buildable:
the cost engine prices all eight scopes, the instrument master spans 457,188 rows across
NSE/BSE/NFO/BFO/MCX/CDS/NCO, sizing reads lot size live with no exchange literal, and the paper
runner already takes `segment` as policy.

**Named blockers, carried into `BACKLOG.md`:**

- **B1 · no Greeks or IV-surface engine exists anywhere in `src/`** — blocks arming both option
  segments. The ATM-IV adapter ingests one published point per underlying, which is not a surface.
  Largest single gap; sourcing is under way.
- **B2 · depth capture is cash-only in two places** — `record_live_depth_session.py:114-118` filters
  the universe to `NSE`/`EQ`, and `depth_tape_schema.py:33-36` omits MCX and CDS price divisors and
  raises fatally for them. A **measured** MCX divisor is required (`R.03`).
- **B3 · no physical-settlement/assignment engine for NFO stock F&O** — SEBI-mandated since 2018,
  modelled only for MCX today. Blocks arming stock-options and stock-futures near expiry.
- **B4 · MCX has no history and no trading calendar** — zero rows, no adapter, and
  `pandas_market_calendars` has no MCX calendar for its evening session. `R.16`: acquire or log.
- **B5 · only one strategy module exists** — `intraday_mean_reversion_engine.py`, 254 lines, cash.
  Five segments have no alpha logic at all. This is the bot work itself.
- **B6 · Angel One symbology is NSE-only** — degrades cross-broker verification for five segments.

**None of these hold the others back**, which is the point of building to one contract: MCX is built
to the same protocol and sits at the bottom of the maturity ladder until B4 clears.

## 7. Risks that genuinely remain, stated rather than argued away

`R.11` — these are recorded, not resolved:

1. **A spine defect is now 6× to unwind.** Mitigated by reviewing the protocol adversarially before
   bots begin, and by the conformance suite catching drift early. Not eliminated.
2. **Two of six segments need Greeks and an IV surface** (`L6.x`) that do not exist yet.
   `nse_ingest/atm_implied_volatility_bot.py` ingests ATM IV daily, which is a start and not a
   surface. Index-options and stock-options cannot graduate before it exists — they can still be
   built and paper-traded on the spine.
3. **MCX is a second venue, not merely a sixth segment** (`A.01`). Different calendar, an evening
   session, and possibly a different data path. If the data is not present, `R.16` applies: acquire
   it, or log an explicit blocker — never scope the segment down to what is on hand.
4. **Stock futures and stock options settle physically.** The spine's square-off path assumes cash
   settlement; physical settlement is a real, separate obligation near expiry and must be modelled
   before either segment is armed, not before it is built.
5. **`R.01` is not uniform across the six.** Options — index and stock — **never** carry overnight.
   The bot, not the operator, enforces that.

## 8. What this costs, honestly

The spine work (items 1–4) is on the critical path either way — cash-first would have needed all of
it too. The parallel bot phase is where the saving is: six bots concurrently instead of one
segment proved and five repeated afterwards.

It does **not** make the first *live* trade sooner — that is gated by evidence accumulation and by
`R.22`, and no build plan can compress the evidence. It makes the first live trade arrive with **six
segments already built and paper-trading behind it** rather than one.
