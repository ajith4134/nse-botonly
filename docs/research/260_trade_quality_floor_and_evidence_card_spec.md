# 260 · Trade-quality floor + per-trade evidence card — `L5.31` spec

**Written 2026-08-17, before any code, per `R.23c`.**
Supersedes the archived design kept at `docs/research/trade_quality_floor_and_evidence.md`, which is
recorded below as the thing this replaces rather than deleted.

Spine item three of the `A.130` build order: `L5.29` protocol → `L13.29` decision trace → **`L5.31`
quality floor** → `L5.30` pod paper lifecycle (built early). Every one of the six segment bots proposes
through this gate, so it is built once, segment-blind, before the bots.

---

## 1. What this engine is for, stated as the failure it exists to prevent

`docs/research/254` measured the retained record of the system this project replaced. The finding is not
that the strategy was unlucky. It is arithmetic:

| strategy | n | stated p(win) | net win rate | avg win | avg loss | payoff ratio | realised net |
|---|---|---|---|---|---|---|---|
| `opening_range_breakout_v1` | 3,049 | 0.416 | **0.350** | ₹292.96 | −₹337.96 | **0.867** | **−₹356,631** |
| `directional_option_orb_v1` | 311 | 0.775 | 0.514 | ₹786.01 | −₹805.68 | 0.976 | +₹4,104 |
| `credit_spread_v1` | 121 | 0.715 | **0.463** | ₹570.83 | −₹199.14 | **2.866** | **+₹21,213** |

> **Correction to `docs/research/254`.** That document reports `credit_spread_v1`'s average loss as
> ₹165.44 and its payoff ratio as 3.450, both computed with `realized_pnl <= 0` as the loss bucket.
> Excluding its **11 scratch trades** — which is the treatment `bot_maturity_ladder` already
> documents and defends, having measured 200 scratches move a break-even from 50.0% to 8.3% — the
> average loss is **₹199.14** and the ratio is **2.866**. The finding is unchanged and if anything
> sharper: a 46.3% win rate against a required 25.9%. The corrected numbers are pinned by a test.

Read the two extremes together, because between them they contain the entire specification.

`opening_range_breakout_v1` needed a win rate of `1/(1+0.867) = 53.6%` to break even at its own payoff
ratio. It achieved 35.0%. It was **18.6 percentage points short of its own break-even on every one of
3,049 trades**, and it took all 3,049 of them.

`credit_spread_v1` needed `1/(1+2.866) = 25.9%`. It achieved 46.3% — **a losing win rate by any fixed
50% rule, and nearly double what its payoff actually required**. It made money.

So the gate cannot be a threshold on probability, and it cannot be a threshold on payoff. It is a
threshold on **the joint quantity, against that trade's own costs, corrected for how many candidates the
bot looked at before choosing this one** — and every term of it is estimated from the bot's own record
rather than declared (`R.03`).

Two further measured facts constrain the design:

- **Costs were more than half the loss.** ₹176,589 of fees against a −₹331,314 net, i.e. roughly
  −₹154,725 gross. A pre-cost-marginal strategy is decisively negative post-cost. Costs therefore enter
  as an explicit **floor** priced through this project's own cost engine, applied to a **gross**
  expectancy — once, never twice (§3.3, §3.4).
- **The stated probabilities carried negative information.** Mean Brier contribution 0.2855, worse than
  the 0.25 a model scores by saying "50%" to everything. A bot's stated `p` is an **input to be
  calibrated, never a quantity to be trusted**. Note the per-strategy overconfidence differs by a factor
  of four (6.6pp for ORB, 25.2pp for the credit spread), so calibration is **per bot**, not global.

## 2. The `R.23a` difference test

| requirement | how this meets it |
|---|---|
| **inputs** | a bot's `PricedSignal`, the breadth of the scan it was chosen from, the bot's own closed-trade record, and the raw instrument facts needed to price costs |
| **solver / inference** | isotonic calibration of stated probability against realised outcome; a Bayesian-bootstrap posterior over the win and loss payoff distributions; a Monte-Carlo convolution into a posterior over net per-trade P&L; a selection correction that is the expected maximum of `n` null draws |
| **carried STATE** | per-bot calibration maps, payoff posteriors and floor history, persisted and refitted as trades close; every emitted card persisted and later joined to its own outcome, so the engine learns from its own verdicts |
| **verifiable output** | an `EvidenceCard` carrying an `ADMIT` / `REFUSE` verdict, the binding constraint by name, and every input and intermediate that produced it |
| **changes behaviour** | a `REFUSE` stops the proposal becoming an order in the paper-session step; nothing downstream sees it |

**SOTA analogs named per `R.23a`:** scikit-learn's `CalibratedClassifierCV` / isotonic reliability
machinery for the calibration layer; López de Prado's Deflated Sharpe Ratio (the expected-maximum-of-`n`
correction) for the selection layer; Qlib's signal-to-portfolio filter for where it sits in the pipeline.
Comparable in depth to those, not to a pair of `if` comparisons.

**What a thin diagnostic version would omit**, and which this must therefore contain: the calibration
layer entirely (it would trust the bot's `p`); the payoff *distribution* (it would use a point mean, and
the whole `credit_spread_v1` finding is invisible to a point mean); the selection correction (it would
score each candidate in isolation and be blind to the fact that 1,594 names cleared a 1-sigma cut at one
instant in `L5.29`'s real-data pass); the cost engine (it would gate gross edge and reproduce `D.01`);
and the persisted card joined to outcome (it would never find out that its own floor was wrong).

## 2b. What already exists, and why this is not it (idea-intake, `R.12`)

Three built things sit near this one. None of them answers its question, and this engine **consumes**
rather than re-implements all three.

| built | the question it answers | why it is not `L5.31` |
|---|---|---|
| `cost_gate/pre_trade_cost_gate.py` (`PreTradeCostGate`) | does the **stated** edge clear this trade's priced cost hurdle? | it **trusts** `PricedSignal.expected_edge_bps`. Research/254 measured stated probabilities with a Brier of 0.2855 — *worse than a constant 50%*. A gate that trusts the input cannot catch a bot that is confidently wrong, which is precisely how ₹356,631 was lost. |
| `cost_gate/per_segment_edge_floor.py` | what edge does a **whole segment** need before any trade in it is worth taking? | a segment-wide quantile floor. It has no view of an individual bot's calibration, its payoff asymmetry, or how many candidates this particular proposal beat. |
| `paper_loop/bot_maturity_ladder.py` (`BotMaturityLadder`) | may this **bot** act at all, given its whole record? | bot-level and retrospective, assessed per session. `L5.31` is per-**proposal** and prospective: a matured bot still proposes bad individual trades, and this is what stops them. |

Composition, therefore: the quality floor takes the cost gate's `GateDecision` as an **input**, so the
round-trip cost is priced exactly once, by the engine that owns pricing, and the cost floor term is
`CostHurdle.required_bps` already computed rather than a second opinion about it.

## 3. Modules

Under `src/nse_algo_trader/trade_quality/`.

### 3.1 `trade_quality_evidence_card.py` — the record
The immutable, serialisable per-decision artifact. It is written whether the verdict is ADMIT or REFUSE,
because a record of what was refused is the only way to ever learn that the floor is too high.

Carries: the proposing bot's identity and segment · the instrument · the stated probability and the
calibrated one, with the calibration method and the `n` behind it · the win and loss payoff posteriors
(mean and credible interval each) · the priced round-trip cost with its own components · the gross edge ·
the net edge posterior (mean and lower credible bound) · the scan breadth · each candidate floor with the
name of its derivation · the binding floor · the verdict and the binding constraint's name · a content
hash for idempotent re-recording. Money is `Decimal` throughout (`R.03`).

### 3.2 `stated_probability_calibrator.py` — calibration, with carried state
Fits an isotonic regression from a bot's stated `p(win)` to its realised outcome, **time-ordered and
out-of-fold** so a trade never calibrates itself (`R.23`, leakage-free validation). Reports the Murphy
decomposition of the Brier score — reliability, resolution, uncertainty — so "the probabilities carry
negative information" is a measured verdict rather than an impression.

`R.04` in force: the full estimator is built now and the *activation* is gated by evidence. The hierarchy
when a bot's own record is thin is **bot → segment → globally pooled**, with Beta-binomial shrinkage
toward the parent, so a cold-start bot is calibrated against its segment rather than left uncalibrated or
denied the algorithm. The shrinkage weight is derived from the parent's own dispersion, not chosen.

Adversarial requirement: a bot that games the gate by stating `p = 0.99` on everything must be *worse*
off after calibration than one that states the truth, because a constant input has zero resolution and
isotonic collapses it to the base rate.

### 3.3 `realized_payoff_distribution_estimator.py` — the payoff posterior, with carried state

**GROSS, and this was corrected during the build.** The magnitudes are before costs, because the
priced round-trip cost is a separate floor. The first implementation used net magnitudes AND applied
a cost floor, charging costs twice; the real-data run is what surfaced it. `RealisedTradeOutcome`
therefore carries `gross_rupees` and `costs_rupees` separately, and the retained corpus is read as
`realized_pnl + total_fees`.
Estimates the distributions of `|P&L|` conditional on win and on loss, from the bot's own closed trades,
by Bayesian bootstrap — a posterior, not the two point means, because the decision turns on the *ratio*
of two estimated quantities and a point ratio hides its own uncertainty. Scaled to the proposal's own
risk so the estimate transfers across position sizes. Where the bot carries MFE/MAE excursions they
sharpen the loss tail; where it does not, the estimator says so on the card rather than assuming.

### 3.4 `gross_expectancy_posterior.py` — the joint quantity
Convolves the calibrated probability posterior with the two payoff posteriors by Monte Carlo,
yielding a posterior over **gross per-trade P&L in rupees**, and reports
`P(gross expectancy > the binding floor)`.

**That probability, not a bound against a bound, is the verdict statistic — also corrected during the
build.** The first version compared the lower credible bound against a floor that was itself a
dispersion half-width, which subtracts the same uncertainty twice, since a lower bound has already
subtracted it once. It is now charged exactly once, in this probability. It is also the statistic
`bot_maturity_ladder` promotes on, so the per-trade gate and the per-bot ladder cannot mean different
things by "the evidence supports this".

The probability is compared against a stated `QualityFloorPolicy.admission_confidence` — the one
number in the engine that is policy rather than measurement, mirroring `LadderPolicy` and stated
rather than defaulted (`R.03`).

### 3.5 `selection_corrected_quality_floor.py` — the floor, derived three ways
The floor is the **maximum** of three candidates, each derived, none declared:

1. **The cost floor** — the trade's own priced round-trip cost through `NseTransactionCostEngine`. A
   physical, statutory quantity. This is the floor `D.01` and research/254's ₹176,589 demand.
2. **The realised-cost floor** — what this bot has actually PAID per round trip, averaged over its own
   record. It exists to catch an **under-priced cost model** rather than to restate floor 1: if the
   cost engine quotes ₹30 for this trade while the bot's own 121 closed trades averaged ₹41.90, the
   higher number is the honest hurdle, and `D.01` is exactly the failure of believing the lower one.
   *(This replaced a dispersion half-width during the build — see §3.4 for why that version was wrong.)*
   The break-even *rate* stays on the card as evidence and explanation only — 57.2% gross for
   `opening_range_breakout_v1` against an achieved 46.4%, 37.9% for `credit_spread_v1` against 70.2% —
   never as a threshold, because `docs/research/256` measured a plug-in break-even compared against a
   plug-in rate from the same sample moving in **both** directions, promoting a bot two rungs on one
   added losing trade.
3. **The selection floor** — when a bot chooses one proposal out of `n` scanned candidates, the winner is
   the maximum of `n` noisy draws and is biased upward even under a null of no edge. The correction is
   the expected maximum of `n` standard-normal draws,
   `E[max_n] ≈ (1−γ)Φ⁻¹(1−1/n) + γΦ⁻¹(1−1/(n·e))`, scaled by the dispersion of the candidate scores
   and by the proposal's notional — the Deflated-Sharpe construction. It **rises with scan breadth**,
   which is the property no fixed threshold has and which the 1,594-names-at-one-instant measurement
   demands.

   **The candidate scores must be FRACTIONS of each candidate's own notional, and that was the third
   defect the real-data run caught.** The expected-maximum expansion assumes exchangeable draws;
   candidates of different position sizes are not exchangeable, so a scan scored in rupees measures
   the spread of NSE share prices rather than the spread of the signal. Over 500 real instruments that
   produced a ₹1,582 floor which dominated every other term and refused everything. Standardised, the
   same real 500-name five-minute cross-section gives a dispersion of 0.008227 per notional, an
   `E[max]` of 3.053 sigma, and a correction of **2.51% of notional — ₹680 on a ₹27,090 position**.
   That is a large, real number, and it is what a wide intraday scanner must clear before anything
   else.

The binding floor and its name go on the card. A floor that never binds is a floor that is not doing
anything, and the store makes that visible.

### 3.6 `trade_quality_floor_engine.py` — the gate
The orchestrator. Segment-blind, performing **no I/O** — stores and the cost engine are injected, the
same discipline `L5.29` imposes on bots, which is what makes it replayable and safe for six concurrent
authors. In: a `PricedSignal`, the scan breadth, the bot's identity. Out: an `EvidenceCard` with a
verdict. Refuses to admit on any non-finite intermediate rather than falling through to permissive, which
is the defect `L5.30`'s review found in the ladder.

### 3.7 `trade_quality_evidence_store.py` — persistence and the learning loop
Append-only SQLite store of cards, keyed by content hash so re-recording is idempotent on real data.
Joins each admitted card to the outcome of the trade it admitted, which is what feeds §3.2 and §3.3 on
the next fit — the engine is scored by its own subsequent record, not by its own confidence.

## 4. Decision integration (`R.06`)

Wired into the paper-session step of `scripts/run_daily_operations.py`, between a bot's `propose` and the
order path: a `REFUSE` verdict means no order. Every verdict emits a decision trace through the `L13.29`
contract, so a refusal is reconstructable afterwards. The engine is therefore load-bearing on the live
decision path from the day it lands — not advisory.

## 5. Verification plan

- **Unit** — each estimator against hand-computable cases.
- **Property** — the floor is monotone non-decreasing in scan breadth; the verdict is monotone in net
  edge; calibration is order-preserving; the card's hash is stable under field reordering.
- **Adversarial** — the constant-`p = 0.99` bot; `n = 0` cold start; zero-variance payoffs; a negative
  priced cost; non-finite probability; a bot whose wins and losses are all identical; scan breadth of 1
  and of 10⁵.
- **`R.05` real data, and it is falsifiable in advance.** Fit on the 3,481-node
  `experience_memory.sqlite3` record and require, on pain of the build being wrong:
  the calibrator recovers the per-strategy overconfidence (≈6.6pp / 26.1pp / 25.2pp);
  the payoff estimator recovers the ratios (0.867 / 0.976 / 3.450);
  the gate **REFUSES** `opening_range_breakout_v1`-shaped proposals, which lost ₹356,631;
  and **ADMITS** `credit_spread_v1`-shaped ones, which made ₹21,213 **on a 46.3% win rate** — the case
  any fixed win-rate rule gets backwards.
  Then a live pass over the paper session, reporting admitted and refused counts and the binding floor.

## 6. Sourcing (`R.17`) — search actually run 2026-08-17

A four-part sweep was run against GitHub and PyPI, with `pip install --dry-run` on this box for every
shortlisted package and the decisive source files read rather than the READMEs. Every rejection below
rests on a mechanical fact, and all of them are surfaced here for the operator's double-check.

| part | candidates checked | outcome |
|---|---|---|
| **Brier decomposition** | `sklearn.calibration`, `netcal` (379★, 2026-04), `xskillscore` (243★), `MAPIE` (1,579★), `venn-abers` (207★), `uncertainty-toolbox` (2,009★, 17 months stale), `properscoring` (188★, 3.4 years stale), `PyCalib` (dev-alpha) | **None ships a Murphy/Sanders decomposition.** `netcal`'s whole source tree has zero occurrences of "brier"/"resolution"/"murphy"; `xskillscore` ships `brier_score`, `reliability` and `discrimination` separately but no unified REL−RES+UNC call. The decomposition here is ~30 lines of arithmetic on binned counts, built on `sklearn.isotonic`, which IS integrated. |
| **Deflated Sharpe / expected-max-of-N** | `mlfinlab` (4,906★), `PortfolioLab` (184★), `quantstats` (7,555★), `skfolio` (2,170★), `deflated-sharpe` (7★), `pypbo` (137★) | `mlfinlab` and `PortfolioLab` **rejected on hard facts**: not on PyPI at all and 3–4.7 years stale. `quantstats` verified to implement PSR only — grep for "deflated"/"expected_max" in `stats.py` returns zero. `pypbo` implements PSR+DSR but is not indexed on PyPI. `mnemox-ai/deflated-sharpe` is correct and verified against the paper's own worked example, but is 7★ and five months old, so the ~20-line expected-maximum expansion is written here rather than taken as a supply-chain dependency on an unproven micro-package. **Surfaced for double-check.** |
| **Bayesian bootstrap** | `bayesian_bootstrap` (123★, 4.4 years stale), `scipy.stats.bootstrap`, `resample` (86★), `arviz`, `pymc`/`bambi` | `scipy.stats.bootstrap` and `resample` are **frequentist by their own documented method lists** (percentile/basic/BCa) and cannot produce a Rubin posterior. `lmc2179/bayesian_bootstrap` is genuinely Rubin — its source draws `dirichlet(ones(n))` weights and dots them against the data, which is exactly the construction used here — but has had no commit since March 2022 and ships no type hints. Its useful surface is ~15 lines, already matched by `bayesian_bootstrap_mean`. **Surfaced for double-check.** |
| **the whole gate** | `nautilus_trader` (25,744★), `freqtrade`/FreqAI (53,382★), `qlib` (47,643★), `vectorbt`, `backtesting.py`, `zipline-reloaded`, `hummingbot`, `jesse`, Lean, `hudson-and-thames/meta-labeling`, `IgorGanapolsky/trading` | **No library does this.** Verified by reading the actual risk code, not inferring: `nautilus_trader`'s Rust risk engine checks notional, rate, precision, quantity and balance — zero probability/payoff/expectancy gating; `qlib`'s `signal_strategy.py` turns scores into positions with no calibration or cost layer. The closest conceptual analog is `meta-labeling`'s `calibration_and_position_sizing/` (research code, not installable). |

`scikit-learn`, `scipy` and `numpy` are already project dependencies and are integrated rather than
reimplemented. Nothing new was added to `pyproject.toml`.

## 7. Known open items to carry to `BACKLOG` (`R.11`)

- The record is **10 sessions**. Every estimate here is thin, and the maturity ladder, not a reduced
  algorithm, is what that thinness gates (`R.04`).
- 1,781 of 3,481 nodes carry `market_regime='unknown'`, so a regime-conditional floor is not yet
  estimable and is deliberately out of this engine's first scope.
- The 3,481 trades belong to three retired strategies and **may not** count toward any segment bot's own
  `closed_trades_observed` (research/254). They calibrate the estimators; they do not mature a bot.

## 8. What the real-data run changed (`R.05` is why this section exists)

Three defects survived the spec, the unit tests and the property tests, and were caught only by
running the whole engine on the 3,481-trade record and a real 500-name cross-section. All three are
the same shape — a quantity charged in two places — and all three are recorded here rather than
quietly fixed:

1. **Costs charged twice.** Payoff magnitudes were net of costs AND a cost floor was applied. Fixed by
   carrying `gross_rupees` and `costs_rupees` separately and modelling the gross distribution.
2. **Uncertainty charged twice.** The lower credible bound was compared against a floor that was itself
   a dispersion half-width. Replaced by `P(gross expectancy > binding floor)` against a stated
   confidence — the statistic the maturity ladder already uses.
3. **Selection correction measured on the wrong axis.** Candidate scores in rupees made the correction
   track NSE share prices; over 500 real instruments it produced a ₹1,582 floor that refused
   everything. Fixed by requiring per-notional fractions, which the same real cross-section then
   prices at 2.51% of notional.

A fourth was caught in the verification script rather than the engine: it had imposed a cash-equity
500-name scan on two options strategies that never ran one, fabricating the term the floor is most
sensitive to. The script now assesses the retained pair at the breadth its record actually supports
and measures the selection correction separately.

**Final `R.05` result.** Fitted on all 3,481 trades, the gate REFUSES `opening_range_breakout_v1`
(P = 0.000, −₹3,56,631), REFUSES `directional_option_orb_v1` (P = 0.537, +₹13/trade — correctly
undecided rather than admitted) and ADMITS `credit_spread_v1` (P = 0.941, +₹21,213 **on a 46.3% net
win rate**). Re-running recorded 0 new cards, so the accrual is idempotent on real data.
