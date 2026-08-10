# AJITH FINAL TODO — the executable sequence

**Generated 2026-08-10 from `ajith_final_plan.md` (682 entries).** Read top to bottom; the order is the
dependency order. Every task cites the plan entries it delivers, so coverage is verifiable rather than
asserted. Nothing here is new — if a task is not traceable to a plan entry, it does not belong.

**Companion:** `ajith_final_plan.md` is the *what*. This is the *when*. When they disagree, the plan wins
and this file is regenerated.

---

## The build shape (recorded so it is not re-litigated)

**Hybrid: minimal foundation scoped to ONE holon, then that holon whole, then widen.**

Both alternatives are failure modes this project has already lived through. Pure foundation-first is what
the prior build did — months of infrastructure, 288 modules, no proven money. Pure vertical-slice produces
exactly the "code too thin" diagnosis in `REDESIGN_v1`. So: build L0–L3 **properly**, but scoped only to
what the cash-intraday holon needs, take that holon all the way to a graduated instruction, then widen to
six with the substrate already proven.

- **First holon: cash-intraday** (A.07) — lowest cost floor at 6–11 bps, no Greeks prerequisite, instrument
  and reference are the same thing so the simplest denominator, and it exercises the overnight-carry path.
- **Decision traces before panels** (A.29/L13.29) — reasoning cannot be reconstructed later.
- **Phase 1 is done when ONE instruction GRADUATES** — not when a trade is placed. A placed trade proves
  plumbing; a graduated instruction proves the pipeline.

**Standing rules apply to every task below:** R.01–R.13 in the plan. Most load-bearing here: no hardcoded
values (R.03), full function even on thin data (R.04), real-data verification (R.05), no orphans (R.06),
engine-grade depth (R.07), every feature visible (R.08), no silent skips (R.11), and **correctness of
execution is not evidence of correctness of allocation** (R.13).

---

# PHASE 0 — GROUND ZERO

*Nothing is built until the ground is clean. Mostly done already.*

- [x] **0.1** Archive everything to a private repo, verified by fresh clone + SHA + per-database integrity — `A.26`
- [x] **0.2** Reset to docs-only; retain Kite TOTP login, Claude subscription provider, 3,481 closed trades, market data — `A.26`
- [x] **0.3** Python 3.12 venv, full ARM64 stack verified by function (LightGBM trains, CVXPY solves, TA-Lib imports) — `B.09`
- [x] **0.4** Reclaim disk: 43 GB free — `B.09`
- [ ] **0.5** **Revoke the leaked GitHub PAT**, reissue via `gh auth login` — `B.10` ⚠️ *operator action, still open*
- [ ] **0.6** Repo skeleton: package layout, `pyproject.toml`, ruff + mypy + pytest config, pre-commit
- [ ] **0.7** **Kite-decoupled architecture guard** — a test that fails if `kiteconnect` is imported outside the broker seam — `L3.28`
- [ ] **0.8** Secrets loading from environment only; `.env` gitignored — `R.02`
- [ ] **0.9** Capital as a runtime parameter, ₹1 lakh → ₹1 crore, with **no rupee constant anywhere** — `A.23`, `R.03`
- [ ] **0.10** Quality-gate hooks: ruff + mypy + tests block a sign-off — `L2.32`

# PHASE 1 — TRUTH AND COST (L0 + L1)

*Nothing above this is meaningful if the data lies or the costs are wrong. Scoped to cash equity first;
the option/futures/MCX extensions land in Phase 6.*

## 1a · Data truth
- [ ] **1.1** Kite instrument master + daily NFO dump ingest, with the **token-reuse guard** (key on exchange+tradingsymbol) — `L0.01`, `L0.02`
- [ ] **1.2** Historical bar store (SQLite), cash equity — `L0.03`
- [ ] **1.3** **Bitemporal availability-time** on the bar store — event vs ingestion vs availability time — `L0.04`
- [ ] **1.4** Point-in-time universe reconstruction + frozen tradable universe per date — `L0.05`, `L0.06`
- [ ] **1.5** Corporate-action adjustment; symbol-rename/ISIN store; delisted master — `L0.07`–`L0.09`
- [ ] **1.6** Gap detection + provenance-flagged backfill — `L0.10`
- [ ] **1.7** **Causal leakage firewall** — `L0.11`
- [ ] **1.8** Trading calendar: holidays, Muhurat, expiry, event dates — `L0.30`
- [ ] **1.9** Circuit-band / ASM / GSM / F&O-ban state per symbol, with hysteresis — `L0.25`, `L0.28`
- [ ] **1.10** Bhavcopy, MWPL, bulk/block deals ingest — `L0.23`, `L0.24`, `L0.26`
- [ ] **1.11** Clock sync + drift alert — `L0.32`
- [ ] **1.12** Multi-broker data adapters + failover/gap-fill (Kite/Upstox/Angel/Breeze) — `L0.14`–`L0.17`, `A.25`

## 1b · Cost — the reality filter
- [ ] **1.13** **NSE transaction-cost engine**: STT/CTT, brokerage, GST, exchange, SEBI, stamp, per segment — `L1.01`
- [ ] **1.14** **Dual cost regime** — intraday vs delivery, priced under the regime being *entered* — `L1.15`
- [ ] **1.15** **Pre-trade cost gate** — every signal clears round-trip breakeven — `L1.02`
- [ ] **1.16** **Net-EV gate** at 1.5× as a calibrated prior, never a constant — `L1.03`, `A.12`
- [ ] **1.17** Per-segment minimum-edge floors, derived from data — `L1.04`
- [ ] **1.18** Fill/slippage model + **market-impact fill model** — `L1.05`, `L1.06`
- [ ] **1.19** Realized-vs-modelled slippage tracker, throttling drifting segments — `L1.07`
- [ ] **1.20** **The three denominators** made explicit on every calculation: notional / ticket / margin — `L11.106`
- [ ] **1.21** Capital-based position sizing across the full capital range — `L1.10`
- [ ] **1.22** P&L attribution by cost component — `L1.11`

# PHASE 2 — VALIDATION AND OPS FLOOR (L2 + L3 + L7 core)

*The gate every instruction must pass, and the floor where losses actually occur.*

## 2a · Search integrity
- [ ] **2.1** **Honest trial registry** — every trial including failures — `L2.01`, `L11.120`
- [ ] **2.2** **Holdout custodian** that refuses queries — `L2.02`
- [ ] **2.3** **Deflated Sharpe as in-loop fitness** — `L2.03`
- [ ] **2.4** **CPCV** (vendor `cpcv.py`) — `L2.04`
- [ ] **2.5** MinBTL hard gate; PBO/CSCV — `L2.05`, `L2.06`
- [ ] **2.6** **BY-FDR control** + **effective-trials estimator** — `L2.07`, `L2.08`
- [ ] **2.7** Purged + embargoed walk-forward CV — `L2.09`
- [ ] **2.8** Triple-barrier labelling + sample uniqueness, reimplemented from AFML (`mlfinlab` is stubbed — `D.18`) — `L2.10`
- [ ] **2.9** **Mechanism declaration** — no mechanism, no instruction — `L2.11`, `L11.101`
- [ ] **2.10** Regime-coverage gate — a real drawdown and vol spike, not elapsed days — `L2.12`
- [ ] **2.11** Proper scoring rules, Brier decomposition, mechanism recalibration — `L2.19`–`L2.21`
- [ ] **2.12** Prequential scorer; prediction-labelled trade tables — `L2.22`, `L2.23`
- [ ] **2.13** **The gatekeeper as a Class-B SERVICE — deterministic, never learning** — `L14.23`
- [ ] **2.14** Verification cockpit — the machine done-rule — `L2.31`

## 2b · Ops floor
- [ ] **2.15** Idempotent client order IDs — `L3.01`
- [ ] **2.16** Order-intent write-ahead log — `L3.02`
- [ ] **2.17** Broker-truth state reconciler on restart — `L3.03`
- [ ] **2.18** Crash-safe order placer composing the trio — `L3.04`
- [ ] **2.19** **Pre-trade risk gate** — notional, leverage, rate, collar, max daily loss, drawdown kill — `L3.05`, `L7.02`, `L7.03`
- [ ] **2.20** Order rate limiter — SEBI ≤10/s, Kite 400/min, 5,000/day — `L3.06`
- [ ] **2.21** Kill switch + trading control config — `L3.07`
- [ ] **2.22** **Kill-switch watchdog as a SEPARATE process** — `L7.10`
- [ ] **2.23** Corrigibility off-switch — `L3.08`
- [ ] **2.24** **Selective square-off executor** — opt-in carry whitelist, daily expiry, fail-safe to square-off — `L3.09`, `L5.49`
- [ ] **2.25** Wire the retained Kite TOTP login + token store + client builder — `L3.10`–`L3.12`
- [ ] **2.26** Kite order-type taxonomy; paper/live execution parity — `L9.01`–`L9.03`
- [ ] **2.27** Cold-start behaviour; tiered alerting; structured decision audit log — `L3.21`, `L3.23`, `L3.24`
- [ ] **2.28** SEBI compliance: Algo-ID tagging, audit trail, registration tripwire — `L3.19`, `L12.14`, `L12.20`
- [ ] **2.29** Circuit-limit-aware rejection; MWPL/ban guard — `L7.06`, `L7.07`
- [ ] **2.30** Systemd service management; deploy-and-verify script — `L3.27`, `L10.20`

# PHASE 3 — THE FIRST HOLON (cash-intraday, whole)

*One bot, complete, to paper. This is where the L14 contracts get built for real rather than in the abstract.*

## 3a · The bot contracts
- [ ] **3.1** **Bot anatomy contract** — the nine-part test — `L14.01`
- [ ] **3.2** Holonic nesting topology, three levels — `L14.02`
- [ ] **3.3** **Three-tier sharing rule** — universal / cross-holon / holon-exclusive — `L14.34`, `L14.03`
- [ ] **3.4** Inter-bot protocol; autonomy ladder (advisory → gated → armed) — `L14.04`, `L14.05`
- [ ] **3.5** **⚠️ DECISION-TRACE CONTRACT — before any panel** — inputs, candidates, gate verdicts, choice, confidence, mechanism, counterfactual — `L13.29`
- [ ] **3.6** Class-A/B/C bot classification enforced in code — `L14.22`–`L14.24`

## 3b · The holon's 21 organs
- [ ] **3.7** Universe scanner: 2,000+ → tradeable subset, liquidity and eligibility filters — `L5.20`, `L5.21`, `L14.33`
- [ ] **3.8** Feature/signal pipeline: TA-Lib indicator library + streaming incremental state — `L4.01`, `L4.27`
- [ ] **3.9** **Correlation-prune → importance → effective-trials** feature selection — `L4.03`
- [ ] **3.10** Regime reader (trend gauge + vol gauge, **filtered HMM only — never smoothed**) — `L11.01`, `L11.02`, `L14.37`
- [ ] **3.11** Soft regime *weighting*, not hard switching — `L11.03`
- [ ] **3.12** **BULL agent** — own model, pipeline, calibration, state, SHAP — `L11.09`
- [ ] **3.13** **BEAR agent** — the mirror, independently versioned — `L11.10`
- [ ] **3.14** **Deterministic meta-labelling arbiter** — both-high → FLAT — `L11.11`
- [ ] **3.15** **Mandatory linear + LightGBM baseline gate** — `L11.12`, `A.09`
- [ ] **3.16** **Calibration layer** — Platt/isotonic/temperature, before anything sizes — `L11.13`
- [ ] **3.17** Online learner + drift detection on **realized P&L only**, never self-graded — `L11.14`
- [ ] **3.18** SHAP per-decision evidence store — `L11.15`
- [ ] **3.19** **PROFIT-TRAIL LEARNING GATE — the third organ** — `L11.125`–`L11.128`
- [ ] **3.20** MFE/MAE excursion tracking — which error is being made — `L11.126`, `L5.32`
- [ ] **3.21** Regime-conditional trailing; partial scale-out with runner — `L11.131`, `L11.132`
- [ ] **3.22** **Post-exit counterfactual capture** — the free training signal — `L11.129`
- [ ] **3.23** Per-holon experience memory + track record + calibration — `L11.48`, `L14.33`
- [ ] **3.24** Risk sub-limits inside portfolio limits; own budget — `L14.33`
- [ ] **3.25** Split cadence: **per-bar entries, per-tick exits** — `A.10`
- [ ] **3.26** Signal decay TTL — drop stale signals, never queue — `L5.24`, `L3.18`

## 3c · Strategy + instructions
- [ ] **3.27** **Intraday mean-reversion family** (A.07) — the first armed engine — `L5.05`
- [ ] **3.28** Build all four regime engines, arm one — `L5.19`, `A.08`
- [ ] **3.29** **Playbook instruction engine** — the 24-cell matrix — `L11.96`, `L11.104`
- [ ] **3.30** The instruction object contract — `L11.97`
- [ ] **3.31** **Targets in basis points of the traded ticket, never absolute points** — `L11.98`
- [ ] **3.32** Range-width precondition: `range_width_bps > cost × 1.5` — `L11.99`
- [ ] **3.33** Cash-intraday instruction set CE-01→CE-25 — `L11.110`
- [ ] **3.34** Anti-instructions encoded as blocks, not omissions — `L11.112`
- [ ] **3.35** Overnight-carry decision engine + next-day forecast + whitelist + re-confirmation + horizon cap — `L5.47`–`L5.52`
- [ ] **3.36** Overnight gap-risk engine; aggregate overnight cap; event guard; corp-action exposure — `L7.23`–`L7.26`
- [ ] **3.37** MIS→CNC conversion, verified cash before promising carry; T+1 awareness — `L9.12`, `L9.13`

# PHASE 4 — PROVING GROUND AND SIMULATION

*Where instructions earn the right to exist. Phase 1 completes when one graduates.*

- [ ] **4.1** 24/7 continuous paper loop — `L10.01`
- [ ] **4.2** **Paper fills use the market-impact model and real cost engine** — or the proving ground is theatre — `L11.119`
- [ ] **4.3** Per-holon instruction proving ground — `L11.115`
- [ ] **4.4** **Statistical graduation** — sample N, net-of-cost, DSR on effective trials, BY-FDR, regime coverage, holdout — `L11.116`
- [ ] **4.5** Instruction lifecycle states + demotion path back to paper — `L11.117`
- [ ] **4.6** **Shadow stage** between paper and live — `L11.118`
- [ ] **4.7** Concurrent-trial budget, prioritised by mechanism strength — `L11.121`
- [ ] **4.8** Live instructions keep paper-trading in parallel — `L11.122`
- [ ] **4.9** The saved-winners library with full provenance — `L11.123`
- [ ] **4.10** **Market-closed simulation v2 — fidelity carried over unchanged** — `L10.24`
- [ ] **4.11** **⚠️ Curriculum owns allocation, not availability** — the actual §53 failure — `L10.25`
- [ ] **4.12** **Per-strategy and per-holon quotas — a 97%/0% split must be structurally impossible** — `L10.26`
- [ ] **4.13** **Winners practised deliberately** to reach graduation sample N — `L10.27`
- [ ] **4.14** Every simulated trade registers as a trial — `L10.29`
- [ ] **4.15** Deficit-driven curriculum diversity, not day repetition — `L10.30`, `L5.39`
- [ ] **4.16** Replay-trained calibration as a first-class use (0.4pp vs 11.8pp) — `L10.33`
- [ ] **4.17** Multi-strategy promotion pipeline: research → paper → shadow → reduced-live → full-live — `L2.13`
- [ ] **4.18** **Bootstrap from the 3,481 retained closed trades** — seed calibration, exit priors, regime coverage — `A.26`
- [ ] **4.19** ⭐ **MILESTONE — PHASE 1 DONE: one instruction GRADUATES.**

# PHASE 5 — INTROSPECTION DASHBOARD AND CHAT

*Built on traces already emitted since 3.5. Panels without traces would be fiction.*

- [ ] **5.1** Feature-surface registry + coverage audit — `L13.02`, `L13.03`
- [ ] **5.2** Level 0 — organism at a glance, hero P&L, holon cards, conductor strip — `L13.30`
- [ ] **5.3** Level 1 — inside a holon; scan funnel; organ states; hypotheses; budget burn — `L13.31`
- [ ] **5.4** Level 2 — inside an engine; research details; hypothesis board; learning progress — `L13.32`
- [ ] **5.5** Level 3 — decision trace, SHAP, **which gate came closest to blocking** — `L13.33`
- [ ] **5.6** Form selection per the dataviz procedure; several panels deliberately not charts — `L13.34`
- [ ] **5.7** Colour rules: six fixed hues following the holon not its rank; reserved status colours; **no dual-axis**; validated palette both modes; table views; designed dark mode — `L13.35`
- [ ] **5.8** Feature catalogue + **AST resolver — status measured, never hand-authored** — `L13.04`, `L13.05`, `R.08`
- [ ] **5.9** Operations wall `/wall`; system map `/map` with drift check — `L13.06`, `L13.14`
- [ ] **5.10** Offline diagnostics mode — panels survive token expiry — `L13.10`
- [ ] **5.11** P&L attribution, per-strategy health, promotion state, cockpit verdict — `L13.15`, `L13.16`
- [ ] **5.12** **LLM gateway on the reversed ladder** — subscription → cloud → local — `L11.64`–`L11.64d`, `A.28`
- [ ] **5.13** Cap-aware degradation: shed low-value calls, protect decision-path calls — `L11.64d`
- [ ] **5.14** **Grounded chat panel** — read-only tools, cites what it read, never states an unread number — `L13.36`, `L13.37`
- [ ] **5.15** Gated action layer: propose → confirm → risk gate — `L13.38`
- [ ] **5.16** Chat security: no secrets, allowlisted tools, quarantine boundary — `L13.39`
- [ ] **5.17** Screenshot-verify loop on every dashboard change — `L13.28`

# PHASE 6 — WIDEN TO SIX HOLONS

*Substrate proven; replicate and specialise. Each holon repeats 3a–3c with its own organs.*

## 6a · Options infrastructure (needed by holons 2 and 3)
- [ ] **6.1** Black-Scholes IV; full Greeks per position and portfolio — `L6.01`, `L6.02`
- [ ] **6.2** IV surface fit — skew + term structure — `L6.03`
- [ ] **6.3** Greeks-based pre-trade gate — the second risk vocabulary — `L6.04`
- [ ] **6.4** **VRP richness engine** — needs no long IV history, fixes IV-rank starvation — `L6.05`
- [ ] **6.5** IV-rank shrinkage estimator — `L6.06`
- [ ] **6.6** Regime → profit-engine map; option opportunity scorer — `L6.07`, `L6.08`
- [ ] **6.7** Terminal-distribution model + **structure payoff optimizer** (the bot composes its own structures) — `L6.09`, `L6.10`
- [ ] **6.8** Option liquidity filter; per-leg mid-to-mid P&L marker — `L6.11`, `L6.12`
- [ ] **6.9** **Minimum-ticket gate** (flat brokerage inverts the cheap-OTM instinct) + **live-spread gate** — `L11.107`, `L11.108`
- [ ] **6.10** Full option universe from the daily NFO dump; per-underlying expiry selection — `L6.27`, `L6.24`
- [ ] **6.11** SPAN + exposure margin calculator — `L6.30`
- [ ] **6.12** Physical-settlement handling + **expiry-week close-out at T-1/T-2** — `L6.31`
- [ ] **6.13** Expiry/pin-risk management; vega/gamma limits — `L6.32`, `L6.34`

## 6b · The holons
- [ ] **6.14** **Holon 2 — index options** (intraday only, no promotion path) + IO-01→IO-30 — `L14.17`, `L11.110`
- [ ] **6.15** **Holon 3 — stock options** (physically settled; **premium selling stays OFF**) + SO-01→SO-07 — `A.15`, `L6.37`
- [ ] **6.16** **Holon 4 — index futures** + IF-01→IF-09; the hedging instrument for the cash book — `L14.17c`
- [ ] **6.17** **Holon 5 — stock futures** (physical settlement, top-liquidity gate) + SF-01→SF-06 — `L14.17d`
- [ ] **6.18** **Holon 6 — commodities/MCX** — second venue, own master/margin/calendar, session to 23:30 — `L14.17b`
- [ ] **6.19** MCX plays MC-01→MC-09, incl. the **09:00 open-gap** (a genuine information gap) — `L11.110`
- [ ] **6.20** **Per-segment on/off switch** — squares off, releases capital, deschedules — `L14.17a`
- [ ] **6.21** Per-holon regime readers as genuinely different models, shared vocabulary — `L14.37`
- [ ] **6.22** Per-holon online research agents behind the shared substrate — `L14.35`, `L14.38`
- [ ] **6.23** **Hypothesis validation stays central** — no bot grades its own homework — `L14.36`
- [ ] **6.24** Cross-holon instruction transfer as hypothesis, never as licence — `L11.124`

## 6c · Cross-segment
- [ ] **6.25** **Cross-bot netting + self-trade prevention** — `L14.09`
- [ ] **6.26** **Portfolio-level risk above all holons** — six inside their own limits can breach together — `L14.10`
- [ ] **6.27** **Expression selection** — one conviction, ranked on net edge per rupee of capital — `L14.29`, `A.28`
- [ ] **6.28** Per-vehicle conversion track record — what only the paper lab can measure — `L14.30`
- [ ] **6.29** **Live exclusivity enforcement at the portfolio layer**; paper runs all expressions — `L14.31`, `L14.28`
- [ ] **6.30** Cross-segment instructions XS-01→XS-07 — `L11.110`
- [ ] **6.31** Portfolio supervisor + capital allocation across the six — `L8.14`, `L8.01`
- [ ] **6.32** Vol-target sizing + fractional-Kelly ceiling — `L8.02`, `L8.03`
- [ ] **6.33** RMT denoising → HRP clustering → CVXPY knapsack → priority queue with decay TTL — `L8.05`–`L8.09`
- [ ] **6.34** Non-stationary bandit allocator — forgetting stale edge is first-class — `L8.04`
- [ ] **6.35** Drawdown ladder; correlation breaker; portfolio VaR/CVaR with Greeks — `L7.04`, `L7.09`, `L7.11`, `L7.12`

# PHASE 7 — THE ORGANISM

*The conductor, the meta bots, and the governance the three completed trunks already provide.*

- [ ] **7.1** **CONDUCTOR BOT** — residency states, LRU model cache, admission control — `L14.11`–`L14.11c`
- [ ] **7.2** **Priority classes: pinned / elastic / opportunistic** — a dormant risk bot is a catastrophe — `L14.11d`
- [ ] **7.3** Event-triggered wake; minimum residency + hysteresis; deadline awareness — `L14.11e`–`L14.11g`
- [ ] **7.4** cgroups v2 enforcement — a voluntary budget is not a budget — `L14.11h`
- [ ] **7.5** Shared-space substrate; rate-limit arbitration — `L14.11i`, `L14.11j`
- [ ] **7.6** **Conductor fail-safe** — pinned bots continue, nothing new admitted, never suspends the kill switch — `L14.11k`
- [ ] **7.7** Off-hours reallocation, shrunk by the MCX session — `L14.11l`, `L10.31`
- [ ] **7.8** Bot lifecycle manager, reusing the archived autopoiesis machinery — `L14.06`, `L10.06`–`L10.18`
- [ ] **7.9** **Global workspace as the inter-bot bus** — Trunk VIII repurposed — `L14.07`
- [ ] **7.10** **Referee over bot actions** — Trunk VII repurposed — `L14.08`, `L12.01`–`L12.13`
- [ ] **7.11** Bot track record + defunding; disagreement-as-uncertainty — `L14.12`, `L14.13`
- [ ] **7.12** Meta bots: gatekeeper, treasurer, librarian, coroner, red-team, medic, teacher, scout, spokesperson, historian — `L14.21`
- [ ] **7.13** **Teacher/curriculum bot owns simulation allocation** (Trunk XII, 0/11 built) — `L10.25`, `L14.21`
- [ ] **7.14** Memory: episodic, semantic, consolidation, temporal KG, assumption registry — `L11.48`–`L11.53`
- [ ] **7.15** Graphiti bitemporal KG; meta-memory with belief half-life — `L11.54`, `L11.58`
- [ ] **7.16** Perception bot tier — news, filings, analyst, tipster, global, macro, flow, surface, microstructure, sector, universe, calendar, concall, regulatory-watch, alt-data — `L14.18`
- [ ] **7.17** **Dual-LLM quarantine** — mandatory before any web content reaches a bot with trading authority — `L11.36`, `L11.37`
- [ ] **7.18** Online-research organ: crawl4ai → browser-use → Camoufox → local vision LLM — `L11.38`, `L11.39`
- [ ] **7.19** Acquisition scheduler + append-only point-in-time evidence store — `L11.41`
- [ ] **7.20** Tipster harness — most sources proven to be noise is the *primary* value — `L11.46`
- [ ] **7.21** **ORG-DESIGNER BOT, unsupervised with timeout auto-approve** — `L14.14`
- [ ] **7.22** **Non-auto-approvable class** — silence can never touch a pinned safety bot, the conductor, or a main holon — `L14.14a`, `L14.21a`
- [ ] **7.23** Auto-approval grants the lowest autonomy tier only; tiered timeouts; reversibility window; rate limit; awake-hours timer; stricter when live; audit digest — `L14.14b`–`L14.14h`
- [ ] **7.24** Historian + fossil record — `L14.15`

# PHASE 8 — LIVE CAPITAL

*Only graduated instructions, only through every gate.*

- [ ] **8.1** Manual go-live button; per-strategy kill authority — `L12.15`, `L12.16`
- [ ] **8.2** Reduced-size live on graduated instructions only — `L2.13`
- [ ] **8.3** Live-vs-paper divergence monitoring per instruction — `L11.122`
- [ ] **8.4** Config versioning + rollback + change log — `L12.17`
- [ ] **8.5** Full regulatory audit trail; tax-lot record — `L12.19`, `L1.13`
- [ ] **8.6** Disaster recovery runbook + **automated backup** (the prior build had none) — `L3.22`
- [ ] **8.7** Scale live capital as the record earns it, across the ₹1 lakh → ₹1 crore range — `A.23`
- [ ] **8.8** ⭐ **MILESTONE — first real rupee traded on a graduated instruction.**

# PHASE 9 — FRONTIER

*Earned, never assumed. Every item here is gated behind a working, profitable base.*

- [ ] **9.1** Kronos NSE-finetuned, sandboxed, hash-pinned, must beat baseline — `L11.19`, `L11.20`
- [ ] **9.2** DeepLOB, gated behind LightGBM — `L11.16`
- [ ] **9.3** Offline RL for exit timing (Decision Transformer on own logs) — `L11.134`, `L11.92`
- [ ] **9.4** **Self-evolution loop — ONLY after a live edge exists** — `L11.77`, `A.13`
- [ ] **9.5** Symbolic regression for interpretable alpha — `L11.78`
- [ ] **9.6** Structure-inventor, dispersion, stat-arb, market-making — `L6.10`, `L6.15`, `L5.16`
- [ ] **9.7** Counterfactual perturbation; synthetic and adversarial days; reactive market — `L10.32`, `L10.34`, `L10.35`
- [ ] **9.8** Remaining cognitive trunks: III WILL, V SELF, XI GENERATIVITY, XII CURIOSITY, XIV AXIOLOGY — `Part II`
- [ ] **9.9** py_trees, torchhd, pymdp, Scallop, MAPIE, NumPyro, TabPFN — `L11.80`–`L11.87`
- [ ] **9.10** Later-tier holons: currency, BSE index options, ETF, SME, pre-open auction, expiry-day specialist — `L14.17`
- [ ] **9.11** Proactive assistant; natural-language strategy authoring — `L13.40`, `L11.95`

---

# COVERAGE AUDIT

*Checked twice, as asked. Every layer and part of the plan maps to at least one task.*

| Plan section | Entries | Covered in |
|---|---|---|
| L0 data truth | 34 | 1.1–1.12 |
| L1 cost | 15 | 1.13–1.22 |
| L2 validation | 32 | 2.1–2.14 |
| L3 ops floor | 28 | 2.15–2.30 |
| L4 signals | 33 | 3.8, 3.9 |
| L5 strategies | 52 | 3.7, 3.26–3.37 |
| L6 options/Greeks | 39 | 6.1–6.13 |
| L7 risk | 26 | 2.19, 3.36, 6.35 |
| L8 portfolio | 17 | 6.31–6.34 |
| L9 execution | 13 | 2.26, 3.37 |
| L10 operations + simulation v2 | 35 | 4.10–4.16, 7.8 |
| L11 intelligence | 134 | 3.10–3.22, 4.x, 5.12–5.16, 7.14–7.20, 9.1–9.3 |
| L12 governance | 23 | 2.28, 7.10, 8.1–8.5 |
| L13 dashboard | 40 | 5.1–5.17 |
| L14 organism | 38 | 3.1–3.6, 6.20–6.29, 7.1–7.24 |
| Part II — 16 trunks | 197 branches | 7.9, 7.10, 7.13, 9.8 |
| Part III — disproved | 27 | 3.34 (encoded as blocks), and as guards throughout |
| Part IV — decisions A.01–A.30 | 30 | referenced inline |
| Part IV — blockers B.01–B.11 | 11 | 0.5, 0.3, 1.12, 6.11 |
| Part IV — rules R.01–R.13 | 13 | apply to every task |

**Second pass — items that could have been dropped, explicitly placed:**
`A.26` retained trades → **4.18** (bootstrap) · `L11.129` post-exit counterfactual → **3.22** ·
`L10.23` the §53 post-mortem → **4.11–4.13** · `L14.21a` main-bot protection → **7.22** ·
`L11.112` anti-instructions → **3.34** · `L13.29` trace-before-panels → **3.5**, before Phase 5 ·
`R.13` allocation-vs-execution → **4.11** · `B.10` PAT → **0.5** · disaster recovery → **8.6**.

**Known deliberate deferrals (Rule K — surfaced, not silent):**
Later-tier holons (9.10) · ultra-tier items (9.x) · trunks III/V/XI/XII/XIV beyond the curriculum bot
(9.8) · Fyers and Groww adapters (`B.03`, credentials) · deep intraday history beyond the free ceiling
(`B.04`) · India VIX history for the shrinkage prior (`B.05`) · SPAN mechanics unverified (`B.06`) ·
per-source API limits unverified (`B.07`) · GIFT Nifty licensing (`B.08`).

---

## How this file is maintained

Regenerated from `ajith_final_plan.md` whenever the plan changes materially. Tasks are checked off only
when the Rule-A sign-off passes: engine-grade depth, real-data verification (or a hermetic harness with the
real-data pass logged as an open blocker), wired into the loop with no orphans, visible on the dashboard,
and every deferral recorded. **A task whose primary consumer is still queued is not done.**
