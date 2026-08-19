# 240 · Adversarial review of `L0.36` — eight confirmed bugs, and a premature sign-off retracted

**2026-08-15. `R.23(c)` adversarial review, run in a fresh subagent on operator authorisation
(the step that had been skipped twice and flagged twice as a deviation). Reviewer: opus, per
`R.26` — judgement-heavy work that had to RUN code and reason about the output.**

Every finding below arrived with a runnable reproduction against the real archive. That is why
they are recorded as confirmed rather than as opinions.

---

## The sign-off is RETRACTED

`M14`/`L0.36` was signed off earlier the same day on: ruff + mypy clean, 2,163 tests passing, and
an `R.05` pass over three real sessions. All three were true and none of them was sufficient.

**`R.05` is not actually satisfied**, because of `B6` below: the verification ran at a staleness
quantile the fill path does not use. The pass measured a market the paper loop does not trade in —
the exact failure the engine's own module docstring says must never happen.

## CONFIRMED BUGS

### B1 · `JOIN_VERIFIED` on two comparable bars — HIGH, live on real data

`bar_tape_join_verification_engine.py:816-829`. `smallest_trials_that_can_reject` returns **1**
whenever the leave-one-out null is 0, so a single agreeing bar clears the gate and the instrument
is reported *verified*.

Measured in the live verdict store for 2026-08-11: **14 instruments verified on 2 verifiable bars,
63 on ≤5, 130 on ≤10**, out of 3,109 verified. Those trade as verified joins.

**This is `A.41` inverted** — "cannot tell" folded into "yes" — by the engine whose central design
claim was that it does the opposite.

**Why the test suite missed it, and this is the instructive part.**
`test_a_tape_covering_one_bar_in_a_hundred_is_unverifiable_never_verified` passes only because it
runs a **one-instrument** session, where the leave-one-out null is `None` and the verdict falls
through to `JOIN_UNVERIFIABLE` for an unrelated reason. Add any second clean instrument and the
identical input flips to `JOIN_VERIFIED`. The test asserted the right thing about the wrong
mechanism.

### B2 · the streamed preload derives staleness from ARRIVAL order — HIGH, live on real data

`bar_tape_join_verification_engine.py:959-962`. Gaps accumulate from `last_seen` in stream order
with `abs()`, while `compare_instrument` sorts first. The two paths therefore derive different
thresholds — precisely what `PreloadedSessionSnapshots`' docstring claims cannot happen.

`iter_session_tables_for_instruments` on 2026-08-11 jumps **backwards 5.34 hours**
(`09:58:04 → 04:37:51`) because the day holds three capture runs and pyarrow enumerates
`pre_run_scoping` last. `abs()` converts that into a ~19,200,000 ms gap for every instrument in the
batch. Measured over the first 40 real tokens: 3 get an inflated threshold; token 257 gets
**3,031,941 ms sorted vs 11,793,201 ms streamed — 3.9×**.

Consequence: `TAPE_STALE_AT_BAR_CLOSE` under-fires on the streamed path, so stale books are
compared as fresh — and the streamed path is the one every real run used.

**The equivalence test did not catch it** because its fixture is written in time order.

### B3 · the preload drops the snapshot at the LAST wanted instant — MEDIUM, latent

`bar_tape_join_verification_engine.py:969-979`. For every non-final instant an exact-match snapshot
survives via `at_or_after[next]`; at the final instant there is no next, and the runner widens the
window to `close + 4h`, so it never survives as "latest overall" either.

Reproduced against real parquet with both paths side by side: last bar → streamed
`close_outside_recorded_book`, `tape_last=100000`; per-instrument
`agrees_within_derived_tolerance`, `tape_last=140000`.

**It answers the review's own question directly: the preload does NOT produce identical
classifications. The case I tested is the one case it agrees on.** Unreachable on today's archive
(0 of 600,000 rows land exactly on a 5-minute boundary — microsecond receipt stamps) and live the
moment receipt times are coarsened or `time_column="exchange_time"` is used.

### B4 · the classifier's "factor must move the price" check aborts the whole fit — MEDIUM

`bar_tape_join_verification_engine.py:482-483`. The check `return`s on the FIRST bar whose own
tolerance exceeds `|factor−1|·price`, before the residual test runs. It belongs outside the loop,
evaluated once against the fit.

Reproduction: 20 bars cleanly rescaled by 0.95 classify as `PRICE_BASIS_DIVERGENCE`; widen ONE
bar's tolerance to 8000 paise (a momentarily wide book) and the whole instrument becomes
`SPORADIC_DISAGREEMENT`. **One wide-spread bar destroys a true `M26` finding**, and which bar
aborts is order-dependent.

### B5 · `binomial_upper_tail` picks the branch with fewer TERMS, not the smaller tail — LOW

`bar_tape_join_verification_engine.py:374-384`. For small `p` and small `successes` the chosen
branch is `1 − lower`, which cancels the answer away. Verified against exact `Fraction` arithmetic:
`P(X≥30 | 2000, 0.001)` exact `4.84e-25`, returned `1.78e-15` — **relative error 3.7e9**;
`P(X≥25 | 1200, 0.0015)` exact `2.22e-20`, returned **exactly 0.0**. The store already holds 3 rows
with `binomial_tail = 0.0`.

Verdicts do not flip (error ≪ the 0.01 significance), so this is LOW — but the docstring's claim
that it "never loses precision to catastrophic cancellation at the ends" is **false**, and the
stored tails are unusable as evidence.

### B6 · the join is verified at a LOOSER threshold than the fill path obeys — HIGH

`scripts/verify_bar_tape_join_on_real_data.py:45` sets `STALENESS_QUANTILE = 0.99` with the comment
*"The same policy value the paper loop's book source runs at."*
`scripts/verify_paper_session_on_real_data.py:88` sets **0.95**.

They are not the same and 0.99 is looser. This is the failure the engine docstring and
`preload_session_snapshots` both explicitly warn against, committed in the runner by the same
author on the same day, and compounded by `B2` which loosens it further again.

**This is what invalidates the `R.05` pass.**

### B7 · a probe run is indistinguishable from a full verification — HIGH

`scripts/verify_bar_tape_join_on_real_data.py:169` writes `--limit N` results into the same table
with no marker; the "probe only" warning goes to **stderr**. `verification_coverage_for` then
returns a non-`None` coverage, so `verify_paper_session_on_real_data.py` takes the *verified*
branch, prints "40 verified, 0 refuted", excludes nothing, and trades all 9,000 instruments as
though the join had been checked.

The `A.41` distinction the store's module docstring is built around, defeated by the runner's own
documented usage.

### B8 · "no trade yet" and "crossed book" are counted as disagreements — MEDIUM, `A.41`

`bar_tape_join_verification_engine.py:685-692`. `last_price_paise == 0` (an instrument that has not
traded) produces a full-price deviation → `DISAGREES_BEYOND_DERIVED_TOLERANCE`; a crossed book
(`bid > ask`) makes the bracket empty → `CLOSE_OUTSIDE_RECORDED_BOOK`. Both are facts about the
tape, not about the join, and neither has an unverifiable class.

Real data: **450 rows with `last_price_paise == 0` across ≥50 distinct tokens** in the first 800k
rows of 2026-08-11, plus 8 crossed rows.

---

## The statistical test is not valid as constructed

Three reasons, and the third is the one that matters.

1. **Trials are not independent.** An instrument sampled ~4 minutes before each close on a trending
   price produces 40/40 disagreements, tail `0.0`, `JOIN_REFUTED` — then classifies as
   `PRICE_BASIS_DIVERGENCE` at ratio 1.0027, sending an operator to hunt a corporate action for a
   sampling lag. One fact counted forty times.
2. **No multiplicity control.** `L2.07`'s Benjamini-Yekutieli is unbuilt. Under a *perfectly valid*
   iid null, 9,000 instruments × 75 bars at α=0.01 yields **4–62 expected false refusals**.
3. **Overdispersion, and it invalidates the premise of comparison 2.** "A traded price must lie
   inside the book that produced it" is empirically FALSE on this tape: on 2026-08-11,
   **24.45% of two-sided snapshots have the tape's OWN `last_price` outside its OWN `[bid, ask]`**.
   Widened by each instrument's own derived median spread it is still **2.06% overall, p90
   instrument 5.0%, p99 14.6%**. The binomial test assumes one homogeneous null with no random
   effect, so this heterogeneity reads as evidence about the join.

   Modelled with those measured per-instrument rates and **no broken join at all**: pooled null
   0.0200, threshold k≥6 at n=75, **123 of 1,861 refuted (6.6%) → ~594 of 9,000**. The real run
   refused **121 of 3,230 tested (3.7%)** — the same order of magnitude.

   **So a substantial share of the 187 refusals is plausibly this artefact rather than a join
   failure.** The fix is a beta-binomial / random-effects null.

## RULE VIOLATIONS

| Rule | Site | What |
|---|---|---|
| `R.03` | engine:491 | `median(residuals) > 1.0` — the multiplier is chosen, calibrated by hand from two named instruments (0.25 vs 2.02) |
| `R.03` | engine:469 | `tolerance_paise or 1` — a 1-paise floor; NSE's tick is 5 paise, so this is a representation fact dressed as a market one. It decides classifications |
| `R.03` | engine:74 | `MINIMUM_BARS_TO_SHOW_CONSTANCY = 2` documented as "arithmetic"; two points fit a one-parameter median exactly. Ratios 0.90 and 0.94 → `PRICE_BASIS_DIVERGENCE, factor=0.92` |
| `R.03` | runner:45 | `STALENESS_QUANTILE` hardcoded while `--significance` is a required argument. Both are policy; only one is treated as such |
| `R.06` | store:213, :233 | `price_basis_divergences_for` and `verified_session_dates` have **no consumer**. The first returns the per-token repair factor — the entire point of the `M26` addition — and nothing reads it |
| `A.41` | B1, B8 | unverifiable folded into verified; tape facts folded into disagreement |
| `R.05` | B6 | the real-data pass ran at a threshold the fill path does not use |

## What the review attacked and could NOT break

Recorded because it bounds where the remaining risk is not:

- both hand-written binary searches — brute-forced against a reference over 20,000 random
  sequences × 13 probe instants: **0 mismatches**, including empty input, exact matches, duplicate
  timestamps, and tie semantics;
- `smallest_trials_that_can_reject` — 200,000 random `(p, significance)` pairs plus exact-power
  boundaries: **0 failures**;
- `binomial_upper_tail`'s degenerate branches, monotonicity and range: all hold;
- **timezone handling — not a bug.** All 210,196 real `bar_timestamp` values carry `+05:30`, the
  tape is `timestamp[us, tz=UTC]`, the IST-date filter matches how the store was written, and the
  strict boundary on the bar open is right;
- the store migration — idempotent over 5 successive writes; insert-by-name survives the reordered
  column layout;
- `verification_coverage_for` column indices — correct;
- the leave-one-out arithmetic itself — correct, including the `others_verifiable == 0` guard.

## What still stands after all of this

**The `HINDPETRO` finding.** Its constant 0.95099 was established by direct comparison and
triangulated against NSE bhavcopy (`ClsPric=390.00` agreeing with the tape's 39,000 paise against
the bar store's 37,090). It never depended on the binomial machinery, the leave-one-out null, or the
staleness quantile. The bar store is still wrong for that instrument, and `M26` stands.

**That agreement is exact where it agrees.** All nine deviation deciles at 0.00 across 11,072
instrument-sessions is a direct measurement, not a test statistic.

## What this cost, and what it bought

The review ran 48 tool calls over 15 minutes and found four HIGH bugs that ruff, mypy, 36
purpose-written tests and three real-data passes did not. Two of them (`B2`, `B6`) are failures the
code's own documentation explicitly warns against, written by the author who wrote the warning.

`R.23(c)` exists because of exactly this. It had been skipped twice in this slice and flagged as a
deviation both times; the flag was correct and the deviation was expensive.
