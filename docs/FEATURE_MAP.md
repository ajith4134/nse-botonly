# FEATURE MAP — the deliverable units

**Created 2026-08-12** under `A.93`, which changed the unit of work from a plan ENTRY to a
FEATURE. Built by full synthesis over `ajith_final_plan.md` Parts I–III, `ajith_final_todo.md`,
and the **current** `src/` tree — not the pre-reset tree the docs assume.

**A feature is a capability that is USABLE the moment it lands:** a real consumer (built inside
the feature if it does not exist), an output that changes behaviour, and something visible on
the dashboard. A group of entries that would land unable to do anything is not a feature.

---

## THE ARITHMETIC

| | count |
|---|---|
| Distinct plan-entry IDs (`L0.01` → `L14.38`) | **599** |
| Distinct capabilities | **600** — `L3.28` is used for TWO entries (defect, below) |
| Closed | **42** (all `L0` except `L0.12/.16/.18/.19`; plus `L1.01`, `L2.31a`, `L3.10`–`L3.13`, `L3.27`, `L13.28`) |
| **Outstanding** | **557** |
| Placed into features | **557** — zero unplaced, zero double-placed |
| Features | **41** |
| Of the 557, NOT their own build unit (drop/merge/duplicate) | **~40** |
| **Real outstanding delivery surface** | **~515 entries across 41 features** |
| **Between here and end-to-end paper trading** | **4 features · ~56 entries** |

## WHAT EXISTS IN `src/` TODAY — the blocked-ness baseline

**Exists:** `nse_ingest/` (16) · `market_depth/` (9, incl. the replay engine with
Cont-Kukanov-Stoikov OFI, Stoikov micro-price, Lee-Ready) · `deep_history/` · `historical_bars/` ·
`consolidated_feed/` · `clock_integrity/` · `market_rules/` · `broker_sessions/` +
`broker_credentials/` · `transaction_cost/` (7) · `regime/` (7) ·
`strategy/intraday_mean_reversion_engine.py` · `dashboard/` (13 renderers) · plus
`bitemporal_bar_store`, `point_in_time_universe_engine`, `corporate_action_adjustment_engine`,
`causal_leakage_firewall`, `capital_configuration`.

**Does NOT exist — the hard floor under every blocked-ness call:** no order type · no position
type · no trade/fill type · no execution path · no ledger · no portfolio object · no
experience-memory store · no indicator library · no statistics/validation package · no risk gate ·
no LLM lane · no bot/holon framework · **and the only strategy module emits action + conviction +
deviation, with nothing convertible to basis points.**

---

## THE 41 FEATURES

Size: small ≤6 entries · medium 7–14 · large 15+.

| # | Capability | Entries | Size | Blocked? |
|---|---|---|---|---|
| **F01** | No signal reaches capital without clearing what it actually costs to trade | 14 | med-lg | no — **in flight** |
| **F02** | An intent becomes exactly one order, survives a crash, broker believed over local state | 15 | large | no — greenfield |
| **F03** | Nothing sizes itself; capital, risk and lot structure decide size, and a gate can refuse | 14 | med-lg | no |
| **F04** | ⭐ A trading day runs itself end to end and leaves an auditable ledger | 13 | med-lg | self-unblocks `L0.12` |
| **F05** | The system measures what its execution actually cost and moves its own model | 10 | medium | needs F04 fills |
| **F06** | A candidate cannot be believed until it survives the search size that produced it | 25 | large | no |
| **F07** | Every forecast is scored against what happened; miscalibration corrected, not reported | 13 | medium | no |
| **F08** | Broad feature set computed cheaply; only members that pay in their segment are kept | 14 | medium | no |
| **F09** | Book pressure, toxicity and true price read from the tape we already own | 7 | medium | live mode only (`B.02`) |
| **F10** | Two thousand names narrow to the ones today can pay for | 9 | medium | MCX tier only |
| **F11** | The regime read routes among engines instead of one global model | 10 | medium | no — 5 of 10 built |
| **F12** | Cash-intraday has a real strategy library, each member competing for its slot | 16 | large | no |
| **F13** | A directional view arrives as a calibrated probability with its evidence attached | 8 | medium | `L11.14` needs F04 |
| **F14** | Exits decided by an organ that learns, not a parameter hanging off a strategy | 10 | medium | no |
| **F15** | The brain writes executable instructions per regime × segment | 12 | medium | no |
| **F16** | ⭐ An instruction earns its way to live money by proving itself in paper | 9 | medium | no |
| **F17** | Overnight, the box practises the sessions it is worst at | 18 | large | fidelity (`B.04`) |
| **F18** | A cash position may be held overnight — by a decision it re-earns every morning | 12 | medium | no |
| **F19** | An option can be priced, greeked and refused on its own liquidity | 16 | large | `L6.06` (`B.05`) |
| **F20** | The option book is risk-managed as a book; margin known before a structure is proposed | 10 | medium | no — `B.06` RESOLVED `A.96` |
| **F21** | The bot invents its own option structures and learns which of its ideas pay | 20 | large | no — `B.06` RESOLVED `A.96` |
| **F22** | Six independent trading operations, each complete, each switchable off | 31 | large | no |
| **F23** | Six bots cannot take the same bet six times or trade against each other | 13 | medium | no |
| **F24** | Capital allocated across bets that are jointly sized, not one at a time | 12 | medium | no |
| **F25** | Five cores and 28 GB host eighty bots because residency is allocated | 15 | large | no |
| **F26** | The organism notices its own components failing and repairs them | 17 | large | autonomy grant |
| **F27** | When something breaks at 09:20 you are paged, you see why, and you can roll back | 6 | small | no |
| **F28** | Nothing the system does can escape a constitution, a referee and an off-switch | 23 | large | no |
| **F29** | The system reads news and filings, and knows which sources earned belief | 18 | large | `B.07` |
| **F30** | One crawler fleet serves every bot, and it does not get banned | 6 | sm-med | `B.07` |
| **F31** | Positioning, flows and the overnight world read as context, not strategy | 7 | medium | `B.08` |
| **F32** | The system remembers what it learned, not just what it did | 6 | sm-med | no |
| **F33** | Every LLM call routes down a ladder that degrades deliberately | 13 | medium | no |
| **F34** | LLM desks argue, but a deterministic gate decides capital | 10 | medium | `B.01` arming |
| **F35** | You can see what every bot is thinking, at four levels of zoom | 25 | large | needs F04 traces |
| **F36** | You can ask the system a question; it answers only from state it read | 6 | small | no |
| **F37** | ⭐ Real money armed one segment at a time, with two keys | 6 | small | **`R.22` operator** |
| **F38** | The system decides which bots should exist, and silence is bounded | 11 | medium | `A.13` gate |
| **F39** | Edges that live across names and events, not within one session | 4 | medium | no |
| **F40** | The blocked data adapters, held together to unblock in one pass | 6 | small | **operator** |
| **F41** | The frontier — earned by evidence, never scheduled | 46 | holding pen | by design |

---

## BUILD ORDER

**Critical path to a working paper loop: F01 → F02 → F03 → F04.**

1. **F01** cost reality filter — in flight; buildable on real data today.
2. **F02** order path — **pulled ~90 entries ahead of the plan.** There is no order, position,
   trade or fill type anywhere, so every gate above `L1` has nothing to gate. Building `L2`'s 32
   validation entries first produces 32 more consumer-queued fragments.
3. **F03** size + risk gate — closes the `0.8` R.11 deferral; `capital_configuration` finally
   gets its first real caller.
4. **F04** ⭐ paper loop + ledger — **end-to-end paper trading reaches here.** `L13.29` decision
   traces land INSIDE it: reasoning cannot be reconstructed afterwards.
5. **F06** gatekeeper — after the loop, not before. A judge with no trials is the exact failure
   this map prevents. Bootstraps on the 3,481 retained closed trades.
6. **F07** scoring + calibration · 7. **F11** regime close-out (mostly built) · 8. **F09**
   microstructure (mostly built — register and surface, do not rebuild) · 9. **F08** features
   (after F06 by design) · 10. **F10** universe · 11. **F12** cash families
7. **F14** exit organ — **deliberately ahead of BULL/BEAR.** Exits destroy more edge than entry
   selection, and the free counterfactual only exists if paper records post-exit paths from the
   FIRST session. Retrofitting loses the data permanently.
8. **F13** → **F15** → **F16** ⭐ **first instruction graduates — PHASE 1 complete**
9. **F17** simulation — **moved after F16**, against the plan's phase order: the curriculum
   targets highest uncertainty, which only the proving ground can measure. Building it first is
   precisely how 97% of replay experience went to the losing strategy and 0% to the only winner.
10. **F05** → **F35** → **F18** (after F06: next-day forecasting is a different prediction
    problem and must clear the gate on multi-day data) → **F19→F20→F21** (strict order; the
    optimizer's constraints ARE margin) → **F22** → **F23** → **F24** → **F25** → **F27** →
    **F28** → **F26** → **F32** → **F33** → **F29/F30/F31/F34** → **F36** → **F39** →
    **F37** ⭐ **first real rupee** → **F38** → **F40** (whenever unblocked) → **F41**

**Net effect:** the plan reaches a paper trade around Phase 3–4 after ~200 entries. This order
reaches it after **4 features / ~56 entries**.

---

## UNBUILDABLE TODAY, AND WHAT EACH WAITS ON

| Item | Waiting on |
|---|---|
| **F40 entire** | **Operator.** Breeze session credentials · Fyers: one browser auth OR `FY_ID`+PIN+TOTP · Groww: activate the ₹499/mo subscription. Re-verified mechanically 2026-08-12. |
| ~~`L6.30` SPAN~~ | **RESOLVED 2026-08-12 (`A.96`).** The published risk-parameter archive runs 2008→today (HTTP 200, no auth) and an offline computation reproduces Kite's live `basket_order_margins` within ±8%. F20, F21 and F24's margin constraint are unblocked. |
| `L6.06` IV-rank shrinkage | `B.05` — India VIX history absent. VRP path works without it. |
| F29 / F30 endpoints | `B.07` — per-source endpoints and rate limits never freshly verified. |
| `L11.43`–`L11.45` | `B.08` — GIFT Nifty source and licence unverified. |
| `L11.14`, `L7.16`/`L7.17`, `L12.22`, autopoiesis posteriors | `B.01` — cannot earn calibration until real sessions run. Build now, arm on accrual (`R.04`). |
| F17 sub-1m fidelity | `B.04` (narrowed) — daily bhavcopy free; deep intraday is not. |
| F37 | **`R.22` operator arm. Permanent hard stop.** |
| todo `0.5` | **Operator — `B.10`. The leaked GitHub PAT is still live. Overdue, unrelated to any feature.** |

**Only one whole feature (F40) is unbuildable.**

---

## DROP · MERGE · RENUMBER

*"Drop" = not its own build unit. The catalogue entry stays, per the idea-intake protocol.*

**Already recorded:** `L1.05`+`L1.06` merged (`A.92`) · `L7.13` → `L1.02` · `L7.14` → `L1.01`+`L3.05`.

**Exact duplicates — build once:** `L7.19`=`L12.12` · `L7.20`=`L12.09` · `L7.21`=`L12.08` ·
`L7.22`=`L12.13` (the plan literally writes "see L7.xx" on all four) · `L9.10`=`L3.03` ·
`L9.11`=`L3.02` · `L1.11`=`L13.15` · `L11.98`=`L11.106` · todo `2.54`=`8.10` · todo `0.7`=`2.32`.

**Superseded in place (the plan says so):** `L6.07`→`L11.96` · `L5.32`→`L11.125` ·
`L11.103`→`L11.117` · `L14.03`→`L14.34` · `L5.26`–`L5.30`→F22 (the archived three-bot topology) ·
`L8.14`→`L14.10`+`L14.21a` · `L5.19`→F11/F12 · `L12.14`→`L3.06`+`L3.19`+`L9.01` ·
`L2.24`/`L2.30`→`L2.02`+`L2.05` · `L13.05`/`L13.06`→`L13.04`.

**Declarations, not deliverables:** `L10.23` `L10.24` `L11.114` `L14.22`–`L14.27` (satisfy with ONE
machine-derived bot register, not nine tasks) · `L14.18`–`L14.21` (four roster lists) · `L11.112`
(anti-instructions are guards).

**Policies, not engines — one test each:** `L6.37` (stock-option premium selling stays OFF, `A.15`) ·
`L5.50` (options never carry — implemented as ABSENCE of a promotion path, verified by a test) ·
`L12.20` (SEBI-registration tripwire).

**Disproved — never build:** `L5.46` HFT/latency arbitrage (`D.02`, proven closed from a retail
cloud VM by this project's own prior build).

**Catalogue defects to fix:**
- **`L3.28` is used for TWO entries** — "scheduled daily operations" (built, `A.61`) and
  "Kite-decoupled architecture guard" (not built). Todo carries the collision as `2.59a` and
  `2.60`. **Renumber the guard to `L3.29`.** Until fixed, any ID-keyed audit reports one as done.
- **Todo `3.7` is titled literally "Name"** — generator bug; it is `L4.07`.
- **Built but unticked:** `L11.01` `L11.02` `L11.03` `L11.06` `L5.05` (the `regime/` package) and
  `L4.09` `L4.10` (inside `order_book_snapshot_replay_engine`). Reconcile ticks; do NOT rebuild.

---

## LIVE CHECKLIST — F01 · "No signal reaches capital without clearing what it actually costs"

`A.97`: building feature-by-feature is not achieved by grouping entries into a map, it is achieved
by working THROUGH the map's list for the feature in hand. This is that list, kept current. **The
feature does not close until every row is resolved and the four completion criteria below pass.**

| Entry | What it is | State |
|---|---|---|
| `L1.01` | statutory + broker charges, point-in-time | ✅ built, adversarially reviewed, 13 defects fixed |
| `L1.02` | the pre-trade gate — PASS / RESIZE / VETO / UNPRICEABLE | ✅ built |
| `L1.03` | net-EV — edge against the complete hurdle | ✅ built inside `L1.02`; no multiplier, margin is the measured interval |
| `L1.04` | per-segment minimum-edge floor | ✅ built + derived on 200 real instruments |
| `L1.05` | fill / slippage model | ✅ built |
| `L1.06` | market-impact model | ✅ built with `L1.05` as one engine (`A.92`) |
| `L1.08` | options STT rate change + ITM exercise trap | ✅ delivered by `L1.01`, ticked (`A.97`) |
| `L1.15` | dual intraday/delivery cost regime | ✅ delivered by `L1.01`, ticked (`A.97`) |
| `L7.13` | — | ⛔ DROP → `L1.02`; the plan says "(see L1.02)" |
| `L7.14` | — | ⛔ DROP → `L1.01` + `L3.05`; wiring, not an engine |
| `L11.98` | — | ⛔ MERGE → `L11.106`, which states it more fully |
| `L11.99` | range-width precondition | ✅ built |
| `L11.106` | tradeable-unit denominator | ✅ built |
| `L11.107` | minimum-ticket / flat-brokerage gate | ✅ built |
| `L11.108` | live-spread liquidity gate | ✅ built |
| — | priced-signal contract + strategy wiring | ✅ built inside the feature (`A.93`) |

**All 14 entries are resolved in code.** What remains is the feature's own completion criteria:

- [x] **`R.08` surface** — DONE. `/costs` now shows the hurdle DECOMPOSED (statutory vs
      execution, with the uncertainty component held apart — the design's central property made
      visible, since the margin IS the uncertainty and there is no multiplier), the derived floors
      with the distribution behind them and an evidence badge that makes a thinly-supported floor
      LOOK thin, gate verdict counts, and precondition failures by name. `UNPRICEABLE` renders
      `badge-absent` — no fill, dashed edge — and is deliberately excluded from `judged_count`, so
      a broken feed cannot inflate a rejections figure. 8 tests assert the CLAIMS the page makes,
      including that exclusion and that a hostile reason string cannot inject markup.
- [x] **`R.06` loop wiring** — DONE. `_derive_per_segment_edge_floors` is a daily-runner step,
      run for real: 170 instruments, `NSE-MIS` floor 8.9 bps (median 23.0), `NSE-CNC` 26.2 bps
      (median 36.8). Bounded per run like the backfill is, with the examined count reported so a
      truncation is visible rather than silent. A floor is a property of the market, so leaving it
      un-refreshed would turn it into a stale filter that rejects newly-viable trades invisibly.
- [ ] **`R.23(c)` adversarial review** in a fresh subagent, run BEFORE the execution gate this
      time. `L1.01` was signed off without one and the review then found 5 critical defects.
- [ ] **`R.05` real-data pass** at the strategy's own horizon — bar store, not depth-tape mids.
      `O.74` records why the first end-to-end run does NOT establish what it appears to.

Only when those four pass do all fourteen entries tick together.
