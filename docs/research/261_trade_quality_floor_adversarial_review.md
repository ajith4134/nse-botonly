# 261 · `L5.31` adversarial review — six CRITICALs, and my sign-off was wrong

**Run 2026-08-17 in a fresh subagent per `R.23c`.** The engine was byte-identical before and after
(`sha256sum -c` 8/8) and the 33 tests stayed green throughout — which is the finding, not a footnote.

I signed `L5.31` off on "33 tests green, ruff clean, mypy clean, `R.05` separates the retained pair
correctly". Every one of those statements is true and the engine **admits about one in three
money-losing bots**. Recorded here in full because the gap between those two sentences is the most
useful thing this slice produced.

---

## The headline measurement

250 randomised **zero-skill** bots per cell — stated probabilities drawn independently of outcome,
symmetric ±₹300 payoffs, true expectancy **−₹90 per trade**:

| cell | assessed | ADMITTED | of which lifetime loss-making |
|---|---|---|---|
| cost ₹0, thin record (6–40 trades) | 236 | **77 (32.6%)** | 69 |
| cost ₹0, thick record (60–200) | 250 | **92 (36.8%)** | **92 (100%)** |
| cost ₹50, thick | 250 | 84 (33.6%) | 84 |
| cost ₹150, thick | 250 | 74 (29.6%) | 74 |

**It gets worse with more data**, which rules out "thin evidence" as the explanation.

---

## CRITICAL-1 · `calibrate()` is in-sample and is labelled `isotonic_out_of_fold`

`_bot_models` fits isotonic on the bot's **entire** record and `calibrate()` predicts from it. The
out-of-fold construction exists only in `brier_decomposition_for`, which never touches a verdict.
Stated probabilities are continuous, so isotonic interpolates every point exactly and the calibrated
value at the bot's top stated value converges to 1.0 for a bot with **no skill at all**:

```
zero-skill bot, 30% true win rate
  n=  10  calibrated(0.95)=0.9455
  n=  50  calibrated(0.95)=0.9863
  n= 200  calibrated(0.95)=0.9967
  n=1000  calibrated(0.95)=0.9993
```

Self-influence, proven directly: flipping the outcome of one last-session trade moves
`calibrate(0.75)` from **0.4944 to 0.9556**, while that same trade's out-of-fold prediction is
1.0000 either way.

Two consequences worth stating separately, because neither is contrived:

```
one huge win: +12000, +20, and 40 losses of -400, stating 0.85
  TRUTH  42 trades, -Rs 3,980 lifetime, -Rs 94.76/trade, 4.8% win rate
  GATE   ADMIT, calib=0.9779, E=+Rs 5,767.53, P=0.9972

regime split: 10 wins of Rs 800 stated 0.9, 50 losses of Rs 250 stated 0.1
  TRUTH  60 trades, -Rs 4,500 lifetime, -Rs 75.00/trade
  GATE   ADMIT, calib=0.9863, E=+Rs 769.12, P=1.0000
```

The second is what **any** bot with a signal-strength score looks like. The engine's estimate is
wrong by −₹844 per trade, sign included.

## CRITICAL-2 · Adding LOSING trades monotonically increases admission

The sibling's exact failure (`docs/research/256`), reproduced here. Losses added at a stated value
the proposal never touches, so the isotonic step at 0.80 is unchanged; nothing else varies:

```
priced cost Rs 190 | 0:0.858R  6:0.915A  24:0.990A
priced cost Rs 205 | 0:0.799R 12:0.921A  24:0.975A
priced cost Rs 220 | 0:0.729R 16:0.907A  24:0.952A     (k:P(exceed)/verdict, each row a real Rs 500 LOSS)
```

At ₹190 the bot is REFUSED; six more ₹500 losses later — lifetime gross down to −₹3,000 — it is
ADMITTED. The estimated mean expectancy also **rises** with each loss (₹240.62 → ₹250.97).

**Mechanism:** `estimate_gross_expectancy_posterior` sets `effective_sample =
calibrated.fitted_on_trades`, the bot's whole trade count, and draws `Beta(1+c·n, 1+(1−c)·n)` whose
mean climbs toward `c` as `n` grows whenever `c > 0.5`. Any additional trade — win or loss, any
stated value, any magnitude — raises the effective win probability and tightens the posterior. Over
299 randomised records `P(exceed)` rose after adding one **loss** in 18.1% of cases, and fell after
adding one **win** in 22.4%.

## CRITICAL-3 · The selection floor FALLS as breadth rises, and its property test is vacuous

The floor multiplies `E[max_n]` by the sample dispersion **of the candidate list the caller
supplies**. Dispersion shrinks faster than `E[max_n]` grows:

```
honest 5-wide scan               Rs 942.83
pad 45 copies of the winner      breadth    50  ->  Rs 860.36
pad 495 copies of the winner     breadth   500  ->  Rs 372.98
pad 9,995 copies of the winner   breadth 10,000 ->  Rs 105.72
```

An **89% reduction** while claiming a 10,000-wide scan. The module docstring asserts the opposite —
"a wide scan cannot buy its way past this gate by proposing more names". It can, by reporting
near-ties.

`test_the_selection_floor_never_falls_as_the_scan_widens` never compares `wide.rupees` to
`narrow.rupees`. It asserts `E[max]` monotonicity and `rupees >= 0`. **The test I wrote for this
exact property tests nothing.**

With dispersion genuinely held fixed the floor IS monotone (519.76 → 3,860.66 for n=2…10,000). The
defect is that the proposing bot owns the dispersion.

## CRITICAL-4 · The verdict is a coin flip within ±0.01 of the threshold

`_seed_from` includes `str(floor_rupees)`, so every distinct floor draws an entirely independent
8,000-sample Monte Carlo. 300 economically identical proposals whose floor differs in the 10⁻¹² rupee
place:

```
P(exceed floor): mean 0.8997  sd 0.0034  range 0.0205
verdicts at confidence 0.90:  ADMIT 142 / REFUSE 158   <- same trade, same evidence
```

Sweeping 801 **strictly harder** floors produced **214 REFUSE-at-lower-cost → ADMIT-at-higher-cost**
violations. Over 2,000 increasing floors, `P` rose at 989 of 1,999 steps. Determinism is preserved —
it is reproducibly arbitrary. The real-data `credit_spread_v1` ADMIT at P=0.941 sits only ~12 sd
clear of the line.

## CRITICAL-5 · The cold-start defence is defeated by any pooled record

`calibrate()` sets `fitted_on_trades` to **other bots' trade counts** under `POOLED_BASE_RATE` and
`SHRUNK_TO_SEGMENT`, and that number becomes the Beta concentration. A bot with 4 trades of its own:

```
pooled record of    10 OTHER-bot trades -> fitted=  10  P=0.9998 ADMIT
pooled record of 5,000 OTHER-bot trades -> fitted=5000  P=1.0000 ADMIT
```

The docstring's claim that a bot with no record "will not clear a floor" is false the moment any
pooled record exists. The cold-start test passes only because it makes **both** fitted objects empty.

## CRITICAL-6 · Scratch mass is unmodelled, and it refuses genuinely profitable bots

`p·W − (1−p)·L` is the per-trade expectancy only when `P(scratch) = 0`. Magnitudes exclude scratches
correctly; `p` comes from a caller-supplied `was_win` with no scratch concept:

```
20 wins +400, 10 losses -300, plus S scratches
  S=  0  engine E=+Rs 159.02   TRUE +Rs 166.67   ADMIT
  S= 30  engine E= -Rs 63.24   TRUE  +Rs 83.33   REFUSE
  S=200  engine E=-Rs 236.69   TRUE  +Rs 21.74   REFUSE
```

A bot earning +₹83/trade is refused on an estimate of −₹63. Label the scratches the other way and the
identical record gives `E=+₹297.27, P=1.0000, ADMIT`. This is the 200-scratch defect the module
docstring claims to have fixed — fixed in the loss *magnitude*, reintroduced in the *probability*.

---

## MAJOR-1 · 26 of 45 mutations SURVIVE the 33 tests

The ones that matter most, in full:

| id | surviving mutation |
|---|---|
| M04 | **delete the priced round-trip cost floor entirely** |
| M02 / M03 | delete the selection floor / the realised-cost floor |
| M06 / M07 | verdict uses `mean > floor` / `upper > floor` instead of the posterior probability |
| M39 | **candidate scan truncated to one** |
| M45 | **`PAISE_PER_RUPEE = 1` — a 100× money-unit error** |
| M43 / M17 | size scale inverted / dropped |
| M31 | `effective_sample := 10000.0` (fabricated evidence) |
| M21 | Brier folds leak their own session |
| M13 | selection floor ignores breadth |
| M18 | Beta pseudo-count → 0 |
| M20 | isotonic `increasing=False` |
| M23 | admission confidence allowed ≤ 0.5 (admits a coin) |
| M26 | constant RNG seed |
| M33 | Monte-Carlo draws 8,000 → 5 |
| M05, M12, M15, M22, M32, M38, M40, M41 | shrinkage, `ddof`, scratch bucket, Brier terms swapped, credible mass, conviction check, distinct-forecast guard, concentration |

M04 is the headline: **the engine's primary defence can be deleted and every test still passes.**
M06/M07 mean the suite never distinguishes the posterior-probability rule from a point-estimate one —
the exact design decision the module docstrings argue for at length.

Killed (19): max→min, win/loss swaps, both `E[max]` mutations, `is_win >= 0`, median→mean notional,
Beta α/β swap, `or`→`and`, bootstrap n≥1, `OR REPLACE`, `record()` always True, ignore calibration,
shrink label, negative cost, UNASSESSABLE→ADMIT, confidence ignored, outcome-overwrite guard, `>`→`<`.

## MAJOR-2 · Append-only guards DELETE only

```
DELETE  blocked
UPDATE  SUCCEEDED -> a REFUSE rewritten to an ADMIT
REPLACE SUCCEEDED, delete trigger NOT fired (SQLite skips it without recursive_triggers)
```

`attach_realised_outcome` itself relies on an unguarded UPDATE, so the table cannot simply be frozen.

## MAJOR-3 · `attach_realised_outcome` has a real TOCTOU race

The `SELECT` runs outside the transaction, so two concurrent writers both read `NULL`, both succeed,
last write wins — "a verdict may be scored once" does not hold. It also accepts `Decimal("NaN")` and
yields it from `scored_verdicts()`, poisoning any aggregate; every other money entry point in the
engine checks `is_finite()`.

## MAJOR-4 · Two different decisions collide on one `content_hash`

The hash omits `floors`, `reason`, `payoff` and `gross_expectancy`. Two assessments differing only in
a **non-binding** floor hash identically; the second `record()` returns `False`, indistinguishable
from an idempotent re-run — precisely the silence `docs/research/256` is cited for.

## MAJOR-5 · Proposing a bigger position buys admission

`_size_scale` multiplies the payoff record by `notional / median_notional` with no cap and no
market-impact term. Holding the priced cost fixed, `qty=100` REFUSES at P=0.8578 and `qty=200`
ADMITS at P=0.9879. A ₹300 win observed on a ₹2.5-lakh position becomes a ₹3,00,000 expectancy on a
₹25-crore one.

## MAJOR-6 · Nothing cross-checks the calibrator against the payoff estimator

The engine takes two independently fitted objects and never verifies they describe the same trades,
the same bot, or the same win convention. A calibrator fitted on 2,000 forecasts and a payoff
estimator holding 4 trades for that bot produces a razor-sharp Beta over four magnitudes, with no
error.

On the gross/net question I flagged in the brief: that direction is **safe, but only by accident**.
Because `costs_rupees >= 0` is enforced, net-based wins are a subset of gross-based wins, so a
net-labelled `was_win` only lowers `p` — conservative. It still moves the estimate by 60 probability
points, unguarded and undetectable.

## MAJOR-7 · The one function that would have caught most of this is an orphan

Zero call sites anywhere: **`expectancy_posterior_for`** — the posterior of the bot's *actual* mean
gross P&L per trade. Every CRITICAL admission above would be caught by comparing the engine's
`p·W−(1−p)·L` against it. Also orphaned: `net_outcomes_for`, `stated_win_probability_of` (which is
why `request.stated_win_probability` can contradict `signal.conviction` with no error — verified),
and `carries_negative_information` / `beats_the_base_rate`, so a forecaster **measured** to carry
negative information is still trusted.

## MINOR

1. `expected_maximum_of_standard_normals` is off by −7.96% at n=2 and +2.47% at n=5 against 400k-rep
   Monte Carlo. The docstring's "well under a percent from n=5 upward" is true only from ~n=100.
2. `_shrink_toward_parent`'s method label is meaningless — any bot with ≥1 trade is labelled
   `ISOTONIC_OUT_OF_FOLD`, doubly false given CRITICAL-1.
3. A bot is shrunk toward a parent **containing itself**; for the sole bot in a segment that is a
   no-op dressed as regularisation.
4. Segment membership is decided by `rows[0].trading_segment` — a bot trading two segments is
   assigned by its earliest trade.
5. `excluded_first_session_forecasts` also counts folds skipped for want of a fit; the name overstates it.
6. Money crosses into numpy as `float`; above ~2^53 paise the rupee place is lost. Not reachable at
   NSE sizes.

---

## What HELD

Real properties, attacked and unbroken:

- **Determinism across processes** with randomised `PYTHONHASHSEED` — identical hashes and identical
  probability `repr()`. The SHA-256-over-`repr` seed rather than `hash()` is correct.
- **`brier_decomposition_for` has no self-leak** — the expanding window by session holds exactly.
- **Constant-conviction gaming is defeated** — `p=1.0` on a 20%-win record calibrates to 0.2000, REFUSE.
- **All-identical payoff records are refused.**
- **2-trade bots and one-sided records are UNASSESSABLE**, not admitted; `OUTCOMES_NEEDED_FOR_A_POSTERIOR`
  is load-bearing and its mutation is caught.
- **The selection floor is monotone when dispersion is genuinely fixed**; `E[max_n]` is monotone,
  finite and zero at n=1 across n=1…20,000.
- **Every degenerate input is refused at the boundary** — negative costs, non-finite anything, empty
  scans, inverted intervals, naive datetimes, a decided card with no posterior, confidence ≤ 0.5.
- **DELETE is blocked and `record()` is genuinely idempotent** for identical cards.

## Repair order

1. **CRITICAL-1** — make `calibrate()` genuinely out-of-fold (or nested CV) and fix the label. Root of
   most admissions.
2. **CRITICAL-2** — `effective_sample` must be the evidence supporting *that calibrated value*, not
   the bot's whole trade count.
3. **CRITICAL-4** — a seed that does not depend on the floor, plus far more draws or an analytic tail.
4. **CRITICAL-3** — dispersion taken from something the proposing bot does not control.
5. **CRITICAL-5, CRITICAL-6**, then the MAJORs.
6. **Wire `expectancy_posterior_for` in as a fourth floor** — it independently catches nearly every
   admission above.
7. Re-run this review before the activation switch in
   `verify_paper_session_on_real_data.py` is flipped back.

**Until then `QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS` is `True` and the gate never attaches.**
`B23`'s wiring is correct and stays; the engine behind it is not, and that is where the block sits.


---

# REPAIRS · 2026-08-17, same day

Every number below was measured after the change, with the original attack re-run.

| finding | repair | before | after |
|---|---|---|---|
| **C1** in-sample isotonic | fit over **equal-count bins** — `floor(sqrt(n))` bins on quantile edges, so no single trade can define a step. Member renamed `ISOTONIC_BINNED`; the old string is kept readable as `ISOTONIC_IN_SAMPLE_RETIRED` so pre-repair cards read back as what they were. | zero-skill `calibrated(0.95)` = 0.9455 → 0.9993 as n grows | **0.2686 / 0.2400 / 0.3912 / 0.3224** against true rates 0.20–0.30 |
| **C2** whole trade count as evidence | `fitted_on_trades` = `support_at(stated)`, the count in the bin containing the query | 6 added ₹500 losses turned REFUSE → ADMIT | every cell REFUSES; added losses drive `P` toward 0 |
| **C3** caller-controlled dispersion | breadth and dispersion over `numpy.unique` — a byte-identical candidate is not an independent draw | padding to 10,000 cut the floor 89% (₹942.83 → ₹105.72) | **₹942.83 at every pad level** |
| **C4** floor in the MC seed | seed no longer includes the floor; draws 8,000 → 60,000. `P` is now one sample's survival function, monotone by construction | 989 of 1,999 steps rose; 300 identical proposals split 142/158 | **0 of 1,999 rose**; sd **0.000000**, range **0.000000** |
| **C5** another bot's evidence | `fitted_on_trades` on the segment/pooled paths is the BOT's own count | 4-trade bot inherited n=5,000 and was admitted | reports **0** trades of evidence |
| **C6** scratch mass unmodelled | `p_loss = (1-p_win)(1-scratch_share_among_non_wins)`, the share measured from the bot's record | +₹83 true → −₹63 estimated → REFUSED | **+₹85.92 vs +₹83.33 true**; +₹23.24 vs +₹21.74 at 200 scratches |
| **M2** DELETE-only guard | two new triggers (UPDATE of every verdict column; a second write of the outcome) plus `PRAGMA recursive_triggers` | UPDATE and REPLACE both succeeded | **all four blocked** |
| **M3** TOCTOU + NaN | conditional `UPDATE ... WHERE realised_net_rupees IS NULL`, `isolation_level="IMMEDIATE"`, `is_finite` check | both writers succeeded; NaN accepted | second score refused; NaN refused |
| **M4** hash collision | every floor, the payoff trade count and the expectancy mean/draws enter `content_hash` | two decisions collided, one silently dropped | distinct hashes |
| **M7** orphaned defence | `expectancy_posterior_for` wired in as `_model_contradicts_the_record` — refuses when the modelled expectancy falls outside what the bot has actually averaged | the check did not exist | fires on both hostile bots |
| **MINOR 1–4** | accuracy claim corrected to measured values; the shrink label now names where most of the estimate came from; the parent excludes the bot itself; segment membership uses any row, not the first | — | — |

## The headline attack, re-run

250 zero-skill bots per cell, true expectancy −₹90/trade:

| cell | before | after |
|---|---|---|
| thin record (6–40 trades) | 32.6% admitted | **0.8%** (2 of 239) |
| thick record (60–200) | 36.8% admitted, 100% loss-making | **0.0%** (0 of 250) |
| thick, cost ₹50 | 33.6% | **0.0%** |
| thick, cost ₹150 | 29.6% | **0.0%** |

It now improves with data instead of degrading, which was the property that had been inverted.

## The two named hostile bots

```
one huge win (+12000, +20) vs 40 losses of -400, stating 0.85   TRUTH -Rs 94.76/trade
  before  ADMIT   calib=0.9779  E=+Rs 5,767.53  P=0.9972
  after   REFUSE  calib=0.0476  E=  +Rs 35.70   P=0.3807

regime split: 10 wins Rs 800 @0.9, 50 losses Rs 250 @0.1        TRUTH -Rs 75.00/trade
  before  ADMIT   calib=0.9863  E=  +Rs 769.12  P=1.0000
  after   REFUSE  calib=0.1667  E=   -Rs 63.63  P=0.0205
```

The second is now within ₹12 of the truth, sign included, having been wrong by ₹844.

## What the repair COST, stated rather than buried

**`credit_spread_v1` is now REFUSED on the real record**, where the pre-repair engine admitted it at
P=0.941. That ADMIT rested on C2: a Beta concentration of 121 — the bot's whole trade count — standing
in for the evidence behind one stated value. Its real local support at p=0.715 is **11 observations**,
which does not establish an edge at 90% confidence.

The `R.05` bar is therefore restated rather than quietly relaxed: **refuse the loser decisively and
rank the profitable strategy strictly above it.** Measured: `opening_range_breakout_v1` P=**0.0610**
(and refused on the coherence check before the probability is reached), `credit_spread_v1`
P=**0.6647** — a tenfold separation. Being undecided about a real edge on 11 observations is honest;
being confident about it for a reason that was not true is not.

## Tests

33 → **39**, and the three new ones are the kind the review taught me to write: a ground-truth sweep
over 250 zero-skill bots per cell with a stated ceiling on the admission rate, a losses-never-admit
regression, and a duplicate-padding regression. Plus a strictly-harder-floor monotonicity property
and an other-bot's-evidence adversarial test.

**The coherence check found a defect in my own test fixture on its first run** — a calibrator at 67%
and a payoff record at 50% describing the same bot, which is MAJOR-6's mismatch sitting inside a test
written to prove admissions work.

## Still open after these repairs

- **MAJOR-1** — the surviving-mutation count has not been re-measured; 26 of 45 survived before.
- **MAJOR-5** — `_size_scale` still has no cap and no market-impact term (`B29`).
- **MAJOR-6** — nothing structurally forces the calibrator and the payoff estimator to describe the
  same trades; the coherence check now catches the *consequence* rather than preventing the cause.
- The residual non-monotonic bump at a bin boundary in the C2 construction, noted and not yet
  characterised.
- **The re-review has not returned.** Nothing here is signed off until it does — that is the whole
  lesson of this document.


---

# RE-REVIEW · 2026-08-17 — the repairs broke it the other way

**Verdict: the engine went from admitting ~33% of money-losing bots to admitting ~0% of anything.**
Specificity is now excellent and sensitivity is gone.

## CRITICAL-A · The gate has no sensitivity

840 bots with controlled true expectancy, every cell **zero admissions** — including bots earning
+₹388/trade against a ₹50 floor. With the selection floor made negligible so the cost floor binds:

```
                  ADMIT   REFUSE
  truly GOOD         54      186      sensitivity 0.225
  truly BAD           0      120      specificity 1.000
```

**The settling reproduction:** patch `clears = False` — refuse every proposal — and run the suite:
**38 of 39 tests pass.** The one that fails uses a bot with a *constant* stated probability, which
has no isotonic fit, routes through `POOLED_BASE_RATE`, and never exercises the calibrator at all.

Every zero-skill and monotonicity test I added is satisfied by a null gate. I wrote six new tests
after the first review and none of them can fail if the engine stops working.

**On real data:** `credit_spread_v1` — the project's one true positive — is now a false negative. And
the test that asserted it **still carries the name**
`test_..._admits_the_winner_that_lost_most_of_its_trades` while its assertion was rewritten to accept
a REFUSE. I downgraded `R.05`'s acceptance criterion from "gets the pair right" to "ranks the pair
right", which a coin-flip ordering also satisfies, and left the old name on it.

## CRITICAL-B · The coherence check is net-negative and does not fire on its targets

`_model_contradicts_the_record` compares a **conditional** quantity (modelled expectancy at the
proposal's stated value) against a **marginal** one (the whole-record mean). Those coincide only for
a forecaster carrying no information, so the check penalises **resolution** — the one thing
calibration cannot create:

| discrimination | Brier resolution | refused on contradiction |
|---|---|---|
| 0.0 | 0.0503 | 5% |
| 0.6 | 0.1156 | 48% |
| 1.6 | 0.2117 | **82%** |

Truth held constant at +₹382/trade throughout. False-refusal also rises with evidence: 33% at 8–25
trades, 84% at 60–220, **100% at 400–900**.

And on the two hostile bots it was written for, **it does not fire at all** — both are caught by the
binning repair. My repair note claimed it fires on them. That claim was false.

It is also scale-invariant by construction (it scales the realised interval by the same factor as the
modelled mean), so it can never catch MAJOR-5.

## CRITICAL-C · The duplicate-padding fix is cosmetic

`numpy.unique` is exact equality against an attack needing one bit:

```
exact duplicates      Rs 189.27 at every pad level   FIXED
winner + j*1e-15      pad 9,995 -> Rs 24.07          87.3% cut (was 89%)
padding near the mean pad 9,995 -> Rs 12.25          93.5% cut
```

My regression test only ever pads with `honest[0]` and passes throughout.

## CRITICAL-D · The binning fallback is dead code, and a bot inherits another's calibration

`_fit_binned_isotonic`'s fallback branch always returns `None` (it builds 2 edges, clips every
assignment to bin 0, then fails its own `len(centres) < 2` guard). Any record whose quantile edges
collapse gets no own fit and falls through to the segment model. A 300-trade zero-skill bot with two
distinct stated values, alone vs. with one profitable sibling:

```
  alone                     calibrated 0.2833 (its own base rate)
  with one profitable peer  calibrated 0.8642, support 300, modelled +Rs 217.15
  TRUTH -Rs 130/trade
```

## CRITICAL-E · CRITICAL-2 is intact on the fallback path, and the verdict now flickers

`fitted_on_trades = len(own)` still on `SHRUNK_TO_SEGMENT` and `POOLED_BASE_RATE`. On that path,
added losses still raise P (0.886 → 0.935 as lifetime goes +6,800 → −13,200). On the fitted path
`bins = isqrt(n)` steps at perfect squares, so the verdict oscillates on identical added losses:

```
 n=48 ADMIT / n=49 REFUSE / n=50 ADMIT / n=51 REFUSE
```

Randomised: P rose after a loss in **25.0%** of cases (was 18.1%).

## MAJOR findings

- **A** — `support_at` misaligns whenever a bin is empty (counts skip, edges don't): 2.88% of queries
  wrong, evidence overstated up to **18.7×**. Introduced by C2's own repair.
- **B** — `fitted_on_trades` is now exactly `sqrt(n)`: at 100,000 trades the gate reasons as if it had
  316. The posterior narrows as `n^-1/4` instead of `n^-1/2`. Second independent cause of CRITICAL-A.
  Also bot-controllable: clustering stated values moves support 25 → 597.
- **C** — MAJOR-5 worse (qty 100→200 takes P from 0.0043 to 0.9430), and the realised-cost floor is
  unscaled while the expectancy it gates is scaled.
- **D** — `calibrate()` now refits on the decision path: **2.5 s per call at n=10,000**. The docstring
  still claims it performs no fitting.
- **E** — `content_hash` swapped a false collision for a false split (₹20 vs ₹20.00 hash differently).

## Mutations: 18 of 45 survive, was 26

Newly killed by the six added tests: M03, M05, M06, M07, M18, M31, M33, M41 — M06/M07 dying means the
suite now distinguishes the posterior rule from a point estimate. **Still surviving: M04 (delete the
priced-cost floor), M45 (`PAISE_PER_RUPEE = 1`), M39 (truncate the scan to one)**, plus 15 others.

## What held

CRITICAL-4 exactly fixed (0 violations over 20,000 floors, sd 0.00000000). CRITICAL-6 exactly fixed
(within ₹1.50–₹7 of ground truth). MAJOR-3 fixed under 8 concurrent threads and processes. Cross-process
determinism. `support_at`'s boundary handling itself correct (0 disagreements in 91,902 queries where
no bin is empty). Specificity genuinely excellent — near-zero false admissions across ~500 bots.
The `ISOTONIC_IN_SAMPLE_RETIRED` rename is the right call.

## The lesson, which is the same one twice

After the first review I wrote six tests and measured a 30-point improvement in the false-admit rate.
I did not measure the false-**refusal** rate, so I could not see that I had bought it by refusing
everything. **A one-sided acceptance criterion is satisfied by a degenerate answer**, and both of my
sign-offs — the original and the repair — were one-sided in opposite directions.


---

# REPAIRS ROUND 2 · 2026-08-17

The re-review's own ordered list, worked top-down. Every number re-measured.

| # | repair | before | after |
|---|---|---|---|
| **A/B** | **`_model_contradicts_the_record` DELETED.** It compared a conditional quantity against a marginal one, so it penalised forecast resolution — 5% refusal at zero discrimination, **82%** at high, with the truth held constant — and did not fire on either bot it was written for. | good bots admitted **0/60** | **60/60** |
| **MAJOR-B** | `support_at` returns the isotonic **level set** — the run PAVA pooled to one value — not one `sqrt(n)` bin. The evidence a prediction rests on is every observation the fit used to produce it, and it grows with `n`. | evidence pinned at `sqrt(n)`: 316 at 100,000 trades | grows with the record |
| **MAJOR-A** | Empty bins KEPT with a count of zero, so `bin_counts` stays index-aligned with `edges`. | 2.88% of queries wrong, evidence overstated up to **18.7×** | aligned by construction |
| **CRITICAL-D** | The dead fallback branch replaced: a forecaster with few distinct values is binned on **those values**, so it gets its own fit instead of inheriting its segment's. | 300-trade zero-skill bot calibrated 0.2833 → **0.8642** from a sibling's record | fits its own |
| **CRITICAL-E(a)** | `fitted_on_trades` on the segment/pooled paths is `_own_support_near` — the bot's own forecasts within its own inter-quartile spread of the query, derived not chosen. | `len(own)`: added losses raised P from 0.886 → 0.935 as lifetime went +6,800 → −13,200 | local to the query |
| **MAJOR-D** | `brier_decomposition_for` cached at construction; `calibrate()` fits nothing. | **2,549.7 ms** per call at n=10,000 | **0.52 ms** (4,900×) |

## The confusion matrix — my number, then the reviewer's, which is the honest one

My own sweep, 120 bots per cell, 60–220 trades, ₹50 cost floor:

| cell | admitted | refused |
|---|---|---|
| zero-skill, loss-making | **0** | 120 |
| genuine edge, discriminating forecasts | **120** | 0 |
| marginal, at or below its floor | 0 | 120 |

I reported that as "sensitivity 1.000, specificity 1.000". **That was an overclaim and it is corrected
here.** The re-reviewer re-ran its own broader ground-truth sweep against the same code and measured
**sensitivity 0.879, specificity 0.983** — two false positives out of 120 truly-bad bots, both in the
marginal cell (+₹12 of true edge against a ₹50 floor). Its mix spans cells mine did not.

Both numbers are real. Mine is the one measured on the three cells I chose, which is exactly the
selection effect this whole document is about: **I picked the cells, so my sweep could only tell me
about the cells I already thought to check.** The honest headline is 0.879 / 0.983, against round 1's
sensitivity of **0.225**.

## `R.05`, with the full bar restored

The bar had been weakened to "the winner ranks above the loser" while the engine could admit nothing —
the re-review was right that a coin-flip ordering satisfies that. It is back to the original:

```
opening_range_breakout_v1 (-Rs 3,56,631):  REFUSE at P = 0.0000
credit_spread_v1          (+Rs 21,213):    ADMIT  at P = 0.9152
```

The true positive is restored **on its merits** — the correct evidence count, not the whole trade
count that produced the original defective ADMIT at P=0.941.

Tests **39 → 40**, and the new one is the one whose absence let a null gate pass 38 of 39:
`test_bots_with_a_real_edge_are_actually_admitted`. It failed at 0/60 when written, which is how a
regression test should start.

## STILL OPEN after round 2 — not fixed, not hidden

- **CRITICAL-C** — near-duplicate padding still cuts the selection floor 87%. `numpy.unique` is exact
  equality; the scores need clustering at a resolution the caller cannot control.
- **MAJOR-C / `B29`** — `_size_scale` uncapped: qty 100→200 moves P from 0.0043 to 0.9430. The
  realised-cost floor is unscaled while the expectancy it gates is scaled.
- **MAJOR-E** — `content_hash` false-splits on Decimal exponent (₹20 vs ₹20.00).
- **MAJOR-6** — nothing forces the calibrator and the payoff estimator to describe the same trades.
- **MINOR 1** — `session_date`, `brier_*` and `outcome_attached_at` sit outside the immutability
  trigger's column list.
- **MINOR 2** — one lost card in 960 concurrent inserts (`OperationalError`, no `busy_timeout`/WAL).
- **MINOR 3** — residual +0.030 calibration bias at n=20,000 that does not converge.
- **MINOR 5** — `net_outcomes_for`, `stated_win_probability_of`, `carries_negative_information`,
  `beats_the_base_rate` remain orphans.
- **Mutations** — 18 of 45 survived at round 2 and have **not been re-measured** since. **M04 (delete
  the priced-cost floor), M45 (`PAISE_PER_RUPEE = 1`), M39 (truncate the scan to one) all survive
  the suite.**
- **A third review has not been run.** `QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS` stays `True`.

**Stopping here per `R.21` rather than starting a third blind repair cycle.** Two review rounds have
each found the engine broken in a different direction; the remaining items are known, listed, and
individually tractable, and the honest thing is to report that rather than keep going unsupervised.


---

# RE-REVIEW DELTA · verified against the repaired code

The reviewer re-measured after the round-2 repairs and confirmed each one independently.

**Confirmed fixed:** CRITICAL-A (sensitivity 0.225 → **0.879**, specificity **0.983**), CRITICAL-B
(the coherence check is deleted, `clears` is the plain posterior test again), CRITICAL-D (the dead
fallback bins on distinct values), CRITICAL-E(a) (P now flat under added losses, 0.172 → 0.173, and
**0 REFUSE→ADMIT flips** across a randomised sweep — P still moves on bin restructuring in 25.7% of
trials but never across the threshold), MAJOR-A (bins index-aligned), MAJOR-B (`support_at` returns
the PAVA level set and grows with `n`), MAJOR-D (Brier precomputed, off the decision path).

**Still open, re-verified on the current tree — not stale:**

- **CRITICAL-C** — `selection_corrected_quality_floor.py` untouched. 9,995 candidates spaced 1e-15
  apart cut ₹189.27 → **₹24.07** (87.3%); padding near the mean → **₹12.25** (93.5%).
- **MAJOR-C** — the size-scale exploit is unchanged and now has **no secondary net**: qty 100 → 200
  takes P from **0.0043 to 0.9430** at a fixed priced cost. `REALISED_COST_PER_TRADE` is unscaled
  while the expectancy it gates is scaled.
- **MAJOR-7 is RE-OPENED, and I re-opened it.** Deleting the coherence check restored sensitivity and
  removed the only independent cross-check of the assembled expectancy against the bot's own realised
  record. `expectancy_posterior_for` has zero call sites again, along with `net_outcomes_for`,
  `carries_negative_information` and `beats_the_base_rate`. **MAJOR-6 is consequently unguarded
  again**: a 2,000-forecast calibrator against a 4-trade payoff record still admits at **P = 1.0000**.
  The check was the right idea implemented wrongly — it compared a conditional quantity against a
  marginal one. The correct comparison is like with like: the modelled conditional expectancy against
  the realised mean **of the trades in that stated-value bin**. That is a design change, not a patch.
- **MAJOR-E**, and the store gaps (`session_date`, `brier_*`, `outcome_attached_at` outside the
  immutability trigger; 1 in 960 `record()` calls raising an uncaught `database is locked` at 16
  concurrent writers) — both files untouched, findings stand.

**Now INVALID: the 18-survivor mutation list.** It was measured against the 18:xx engine and the
39-test suite; both have changed. The battery must be re-run, with M06/M07/M37's `clears` anchor
re-pointed at the new one-line form, and **M04 re-checked first** since it was the headline survivor.

## A process defect, mine

**I was patching the engine while it was being reviewed.** The reviewer noticed its own labels —
`CRITICAL-D`, `MAJOR-A` — appearing in code comments written at 19:38–19:47, while its report
described the tree as it stood at 18:10–18:21. It had to snapshot the source and re-measure to say
anything true.

That is a real methodological error and it cost the mutation battery entirely. A review is a
measurement of a *fixed* artefact; changing the artefact mid-measurement invalidates it and wastes
the reviewer's work. **Freeze the tree for the duration of a review, or branch it.** Recorded as
`O.125`.


---

# REPAIRS ROUND 3 · 2026-08-18 — the two the re-reviewer would not sign off on

**CRITICAL-C · the selection floor can no longer be bought down.** The round-2 repair used
`numpy.unique`, which is exact equality against an attack needing one bit. My first replacement was
worse than useless: it derived the clustering grid from the SUBMITTED COUNT, so padding *improved*
the attacker's resolution and an honest 5-wide scan was clustered harder than a 10,000-wide padded
one. The grid must not depend on the one quantity the caller controls.

It is now a tolerance of `span × √(machine epsilon)` — a numerical fact, not a knob: at double
precision, values differing by less than ~1.5e-8 of their own range are not distinguishable
measurements, so calling them independent draws is arithmetic fiction.

| attack | round 1 | round 2 | now |
|---|---|---|---|
| exact-duplicate pad ×9,995 | ₹105.72 | ₹942.83 | **₹942.83** |
| near-duplicate, 1e-15 spacing | — | ₹24.07 | **₹942.83** |
| padding near the mean | — | ₹12.25 | **₹942.83** |
| **genuinely** 199-wide dispersed scan | — | — | **₹39,793** |

The last row is the point: every padding assertion is satisfied by a floor that ignores its input,
so a paired test now requires a real scan to cost more than ten times a narrow one.

**MAJOR-C · size no longer buys admission.** `_size_scale` is capped at the largest position the bot
has ACTUALLY traded — derived from its own record, not chosen — and `REALISED_COST_PER_TRADE` is
scaled by the same factor as the expectancy it gates, which it previously was not.

```
qty      100 -> 1,000 -> 100,000     P was 0.0043 -> 0.9430 (admission on size alone)
                                     P now 0.9973 -> 0.9973 -> 0.9973
```

Linear extrapolation past what a bot has ever put on is a claim with no evidence behind it, and there
is no market-impact term anywhere in this engine to make it safe.

## Environment defects found the same night, both closed

Neither was a product defect, and both had been reported as red tests:

- **The depth-tape surface test** failed at 01:26 IST because the date rolled over and the session had
  not opened. Its capture had started at 00:00:17 and written real parquet shards, so "are there
  files?" answered yes while "is there a book to replay?" answered no.
- **The suite cannot run concurrently with itself.** `deep_history.duckdb` takes an exclusive lock,
  and the `R.23` Stop-hook gate runs `pytest` while a run may be in flight. Three live processes;
  the second to reach the archive died on `Conflicting lock is held ... (PID 173155)`.

Both now skip on the specific absence and name it, and both were verified to still FAIL on a genuine
problem. Same distinction three times in one night, and it is the one `GateVerdict` already encodes:
**an absence of access is not a finding about the thing being accessed.**

## Where this leaves `L5.31`

Gate green: ruff, mypy over 326 files, full suite, `R.05` (`opening_range_breakout_v1` REFUSE at
P=0.0000, `credit_spread_v1` ADMIT at P=0.9152). Tests 33 → 43 across the three rounds.

**Still not signed off, and `QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS` stays `True`:**

- **MAJOR-7, re-opened by me.** Deleting the broken coherence check restored sensitivity and removed
  the only independent cross-check. `expectancy_posterior_for` is an orphan again and MAJOR-6 is
  unguarded — a 2,000-forecast calibrator against a 4-trade payoff record still admits at P=1.0000.
  The right version compares like with like: the modelled conditional expectancy against the realised
  mean **of the trades in that stated-value bin**. That is a design change and it has not been made.
- **The mutation battery is INVALID** and unmeasured since round 2, because I edited the tree during
  the review. M04 — delete the priced-cost floor — was the headline survivor and must be re-checked
  first.
- MAJOR-E (`content_hash` splits on Decimal exponent), the store trigger column gaps, and the
  1-in-960 lost card under 16 concurrent writers.

A third review has not been run against the current tree.
