# 241 — The join null is overdispersed, and B1 and the overdispersion finding are ONE defect

**Spec.** Engine: `market_depth/bar_tape_join_verification_engine`. Store:
`market_depth/bar_tape_join_verdict_store`. Opened by `A.122`, which fixed seven of the
adversarial review's eight findings and deliberately left two undecided because both change what
*verified* MEANS (`R.19`). Operator answered 2026-08-16 (`A.123`).

---

## 1. The two findings, and why they are the same finding

`docs/research/240` left two questions open.

**`B1` — `JOIN_VERIFIED` is reachable on as few as 2 comparable bars.** The live store holds 14
instruments verified on 2 comparisons and 130 on ≤10. A verdict resting on two observations is
not obviously a verdict.

**Overdispersion — the null's own premise fails on a quarter of the evidence.** 24.45% of
two-sided snapshots have **the tape's OWN last price outside its OWN bracket**. A session
modelled with *no broken join at all* produces **~594 false refusals of 9,000** against the
**121** actually observed — the test over-rejects by roughly five times.

**They are one defect — with one measured caveat, recorded below rather than discovered later.**
The engine already refuses to pass an instrument the test could not have refused:
`_judge_instrument` computes `smallest_trials_that_can_reject(null_rate, significance)` and
returns `JOIN_UNVERIFIABLE` below it. That rule is derived, not constant (`R.03`), and it is not
what is broken. It returns 2 because **under a binomial null, 2 genuinely is enough**: two
independent Bernoulli draws at `p = 0.1` both landing on disagreement has probability 0.01, which
clears any ordinary significance. The evidence rule is correct and the distribution it is computed
against is wrong.

So the fix is **one fix, in one place**. Replace the null; the untouched evidence rule reads the
corrected distribution, and no fourth verdict, second power rule or hand-set floor is added.
Adding one would be a second mechanism for a problem that already has one, and the second
mechanism is where they disagree.

### 1.1 How much the evidence bar actually moves — measured, not asserted

An earlier draft of this spec asserted the bar would rise "from two to six" at `p = 0.1`,
`ρ = 0.1`. **That was wrong**, from misapplying the large-`n` asymptotic at `n = 2`. Computed
exactly against the implementation:

| pooled rate `p` | significance | ρ=0 | ρ=0.05 | ρ=0.10 | ρ=0.25 | ρ=0.50 |
|---|---|---|---|---|---|---|
| 0.05 | 0.05 | 2 | 2 | 2 | 2 | 2 |
| 0.10 | 0.05 | 2 | 2 | 2 | 2 | 3 |
| 0.2445 | 0.05 | 3 | 3 | 3 | 4 | 10 |
| 0.05 | 0.01 | 2 | 2 | 2 | 3 | 6 |
| 0.10 | 0.01 | 3 | 3 | 3 | 4 | 14 |
| 0.2445 | 0.01 | 4 | 4 | 5 | 9 | 79 |

**The bar moves materially only when `p` and `ρ` are BOTH large.** At a low pooled disagreement
rate it stays at 2 for any dispersion the data are likely to show. So:

- The **over-rejection** half of the finding (~594 modelled against 121 observed) is fixed by this
  change directly, because it is a variance failure and this is a variance correction. That does
  not depend on the table.
- The **`B1`** half is **conditional on the `ρ̂` the real sessions actually produce**, and this
  spec does not know it yet. It is measured by the `R.05` pass in §7 and reported with the numbers.

**If the `R.05` pass shows the bar still sitting at 2 or 3**, `B1` is NOT closed by this change and
goes back to the operator with measured `ρ̂`, measured pooled rate and the resulting verdict
distribution attached — which is a far better question than the one asked without them. Recorded
here so that outcome is a planned branch rather than a surprise (`R.11`).

**Rejected, and why.** A fixed minimum bar count is the constant `R.03` forbids and would have to
be re-picked per session. A separate power criterion bolted beside the existing one duplicates it
under a different distribution — two rules, two answers, no way to say which is in force. A
fourth verdict for "thin" collapses into `JOIN_UNVERIFIABLE`, which already means exactly
"the test could not have refused this".

---

## 2. Why the binomial is the wrong null here

The current null says: every verifiable comparison in the session is an independent Bernoulli
draw at one common rate `p`, and one instrument's `n` comparisons are `n` draws from it.

The second half is false, and the 24.45% figure is the measurement of how false. Disagreement
propensity is a **property of the instrument** — its spread relative to its tick, its quoting
frequency, how often its book is one-sided or crossed, how far its last trade sits from its own
mid. Those do not vary from bar to bar within an instrument nearly as much as they vary between
instruments. Comparisons inside one instrument are therefore **positively correlated**, and a
binomial null that assumes they are not underestimates the variance of `K_i` — which is
precisely a test that rejects too often, which is precisely the ~594-against-121 result.

This is textbook clustered-binomial overdispersion, and the standard remedy is a
compound-binomial null in which the per-cluster rate is itself random.

---

## 3. The beta-binomial null

Let instrument `i` have a latent disagreement propensity `π_i`, drawn once per instrument per
session, and let its comparisons be conditionally independent given it:

```
π_i  ~ Beta(a, b)
K_i | π_i ~ Binomial(n_i, π_i)
```

Marginally `K_i` is beta-binomial:

```
P(K_i = k | n_i) = C(n_i, k) · B(a + k, b + n_i − k) / B(a, b)
```

with

```
E[π]     = p  = a / (a + b)
ICC      = ρ  = 1 / (a + b + 1)
Var(K_i) = n_i · p(1 − p) · [1 + (n_i − 1)ρ]
```

`ρ` is the **intra-instrument correlation**: the share of disagreement variance that is a
property of the instrument rather than of the individual comparison. `ρ = 0` recovers the
binomial exactly; `ρ → 1` says an instrument either disagrees on everything or on nothing.
The bracketed term is the **variance inflation factor**, and it is the whole of the correction:
at `n = 40` and `ρ = 0.10` the binomial understates the variance by a factor of 4.9, which is the
order of the over-rejection actually observed.

Parameterised from the two quantities that are measured rather than assumed:

```
a = p(1 − ρ)/ρ        b = (1 − p)(1 − ρ)/ρ
```

(Check: `a + b = (1 − ρ)/ρ`, so `1/(a + b + 1) = ρ`.)

**Both `p` and `ρ` are estimated from the session's own comparisons. Neither is an input.** The
only policy input remains `significance`, exactly as before, and it still has no default.

### 3.1 Estimating ρ — Pearson-χ² method of moments

For clusters `i` with `n_i` verifiable comparisons and `k_i` disagreements, and pooled
`p̂ = Σk_i / Σn_i`:

```
X² = Σ_i (k_i − n_i p̂)² / (n_i p̂ (1 − p̂))
```

Each term has expectation `1 + (n_i − 1)ρ` under the model above, and estimating `p` from the
same data costs one degree of freedom, so `E[X²] ≈ (N − 1) + ρ · Σ_i (n_i − 1)` and

```
ρ̂ = ( X² − (N − 1) ) / Σ_i (n_i − 1)          clamped to [0, 1)
```

The clamp is not a tuned bound: `ρ` is a correlation, and a moment estimator of a correlation can
land outside its own support on a finite sample. Clamping to 0 returns the binomial, which is the
correct answer when the data show no more dispersion than binomial.

Chosen over maximum likelihood because it is closed-form on 9,000 clusters, deterministic (no
optimiser seed, no convergence branch, so a regression test pins an exact number), and does not
need a fitting library on the critical path. Its cost is efficiency, not bias, and the estimate is
recomputed per session rather than carried, so an inefficient estimate is refreshed daily.

### 3.2 Leave-one-out, in O(1) per instrument

The rate is already leave-one-out, and for the same reason `ρ` must be: an instrument that
disagrees on everything **raises the dispersion it is judged against** and masks itself, which is
the identical self-masking the leave-one-out rate exists to prevent — and worse, because raising
`ρ` widens the null for every instrument at once.

Naively that is `O(N²)`. It is not necessary. Expanding `X²` and substituting `p̂ = S₂/S₃`:

```
X² = [ Σ k_i²/n_i − 2p̂ Σ k_i + p̂² Σ n_i ] / (p̂(1 − p̂))
   = ( S₁ − p̂ · S₂ ) / (p̂(1 − p̂))            since p̂ · S₃ = S₂
```

with three running sums `S₁ = Σ k_i²/n_i`, `S₂ = Σ k_i`, `S₃ = Σ n_i` and the cluster count `N`.
Dropping one instrument subtracts `(k²/n, k, n, 1)` from the four, so each instrument's own null
costs four subtractions and a division — **exact leave-one-out, `O(1)` each, `O(N)` overall**, no
approximation and no sampling.

### 3.3 Degenerate cases, each answered explicitly

| Condition | Answer |
|---|---|
| `N' < 2` (no other instrument) | no null — `JOIN_UNVERIFIABLE`, as today |
| `S₃' − N' ≤ 0` (no other instrument has 2+ comparisons) | `ρ̂ = 0`; there is no within-cluster replication, so there is no dispersion signal, and the binomial is the honest null |
| `p̂' = 0` | nothing ever disagrees; any disagreement is infinitely surprising — tail 0, as today |
| `p̂' = 1` | everything disagrees; nothing can be surprising — no null, `JOIN_UNVERIFIABLE`, as today |
| `(n − 1)·ρ ≤ machine epsilon` | the variance inflation factor is not representable as distinct from 1; use the binomial. Derived from `sys.float_info.epsilon`, a property of the float type (`R.03(e)`), not a tuned cutoff |

### 3.4 Numerics

The tail is summed in **log space**, term by term, factoring out the largest term before
exponentiating and accumulating with `math.fsum` — the same construction `B5` forced on the
binomial tail after it returned exactly `0.0` for a true `2.22e-20`. Every beta function is
evaluated as `lgamma(x) + lgamma(y) − lgamma(x + y)`, so no `Γ` is ever formed directly and
`C(n, k)` never overflows.

### 3.5 The evidence rule, unchanged in form

`smallest_trials_that_can_reject` keeps its meaning exactly — *the fewest comparisons at which an
all-disagreeing instrument would be refuted* — and is recomputed under the new null:

```
P(K = n | n) = B(a + n, b) / B(a, b) = Γ(a+n)Γ(a+b) / (Γ(a)Γ(a+b+n))
```

strictly decreasing in `n`, so the smallest qualifying `n` is found by exponential doubling then
binary search. Asymptotically `P(K = n | n) ~ [Γ(a+b)/Γ(a)] · n^(−b)` — **polynomial decay, where
the binomial's was geometric**. That is what raises the bar *eventually*; §1.1 measures how
quickly, and the honest answer is "not much until `p` and `ρ` are both large". The figures for
this project's sessions are whatever `ρ̂` measures and are recorded by the `R.05` pass, not
predicted here.

`None` is returned when no `n` suffices, as before.

---

## 4. What changes, mechanically

**Engine.**

- `BetaBinomialDisagreementNull` — frozen, carries `disagreement_rate` and
  `intra_instrument_correlation`, exposes `upper_tail(successes, trials)` and
  `smallest_trials_that_can_reject(significance)`. It is the null as a value, so an instrument's
  verdict and the null that produced it cannot drift apart.
- `LeaveOneOutNullAccumulator` — the four running sums, with `null_excluding(verifiable,
  disagreements)` returning the instrument's own null or `None`.
- `binomial_upper_tail` **stays**, unchanged, as the `ρ = 0` limb and as the thing the
  beta-binomial is tested against.
- `InstrumentJoinReport.binomial_tail` → **`disagreement_upper_tail`**. The old name would
  describe a number no longer computed by a binomial (`R.14`).
- `InstrumentJoinReport` gains `null_intra_instrument_correlation: float | None`.
- `SessionJoinVerificationReport` gains `pooled_intra_instrument_correlation: float`, and its
  session-level `minimum_comparisons_to_reject` is computed under the pooled beta-binomial.

**Store.** `binomial_tail` is **renamed in place** (`ALTER TABLE … RENAME COLUMN`), not shadowed
by a second column — a dead column beside a live one is an orphan (`R.06`). Two columns are added:
`null_intra_instrument_correlation REAL` and `null_model TEXT`.

`null_model` exists because of `B6`'s lesson and is the same lesson: **a parameter that changes
the verdicts must be recorded beside them, or a store holding two runs is indistinguishable from
a store holding either.** The null model changes verdicts at least as much as the staleness
quantile did. So `SessionVerificationCoverage.is_internally_consistent` requires one distinct
`null_model` as well as one distinct `staleness_quantile`, and every row written before this
change reads `NULL` — which correctly marks the existing live store **inconsistent until the
sessions are re-verified**. It is inconsistent; saying so is the point.

---

## 5. What this is expected to do to the live store, and what would falsify it

**Expected:** refusals fall from 121 toward the count a correctly-sized test produces; a
meaningful share of the 14-on-2 and 130-on-≤10 instruments move from `JOIN_VERIFIED` to
`JOIN_UNVERIFIABLE`; the four `M26` price-basis candidates — `HINDPETRO` (150/150 comparable bars
at a constant 0.95099), `XCHANGING` (64/64 at 0.96958) — **survive**, because a constant ratio on
every comparable bar is refused at any dispersion the data can support. If `HINDPETRO` stops
being refused, the correction has overshot and this spec is wrong.

**Also falsifying:** `ρ̂` clamping to 0 across every session (the overdispersion would then be
somewhere other than between instruments, and the 24.45% figure would need re-reading);
`ρ̂` near 1 (the model would be saying instruments are all-or-nothing, which the observed mixed
counts contradict); or the modelled no-broken-join session still producing far more false
refusals than observed after the change, which would mean the dispersion is real but not
beta-shaped and a random-effects logistic null is required instead.

**Not claimed here.** That the beta-binomial is the *true* generative model. It is a
variance-corrected null, chosen because the measured failure is a variance failure, and it is
falsifiable by the checks above.

---

## 5b. Sourcing pass (`R.16`/`R.17`) — run 2026-08-16, mechanical evidence only

Two separable parts were sourced independently: **(A)** the beta-binomial tail, **(B)** the
dispersion estimator. Nothing was judged on a README.

### (A) The tail — `scipy.stats.betabinom` — **PART-ADOPTED**

Already installed: **scipy 1.18.0**. Probed directly, not read about.

- **`logpmf` is sound.** Checked against a 60-digit `mpmath` reference across the range that
  matters, including the extreme all-disagree term: max absolute error **3.66e-13** at
  `(k=199, n=200, a=0.9, b=8.1)`, and `0.0` at `(150,150)` and `(40,40)`. The hand-written
  `lgamma`-based term is **2.20e-13** on the same worst case — the two are the same quality.
- **`sf`/`logsf` are UNUSABLE here, and the reason is `B5` verbatim.** Measured:
  `betabinom.sf(199, 200, 0.9, 8.1)` returns **exactly `0.0`** for a value that is small but
  nonzero, and `betabinom.logsf(...)` returns **`nan`** with
  `RuntimeWarning: invalid value encountered in log` — it computes `log(sf)`, which is the naive
  construction `docs/research/240` `B5` already forced this engine to abandon after it returned
  `0.0` for a true `2.22e-20`. Adopting scipy's tail would reintroduce a fixed bug.
- **`betabinom.fit` does not exist** (`hasattr` → `False`), so it answers none of part B.
- **Speed, measured:** `betabinom.logpmf` **65.29 µs** per call against **1.19 µs** for the
  `lgamma` term — **55×**. Over a real session (~9,000 instruments, ~40 terms per tail) that is
  **23.5 s against 0.43 s**, on the path a full sweep already runs three times.

**Verdict: adopt it as the TEST ORACLE, not on the hot path.** Being an independent
implementation is exactly what makes it valuable for test 1/3/4 — a hand-written term checked
against another author's is a real check, where checking it against itself is not. The engine's
own term stays `lgamma`-based, for the 55× and because it is the same construction the binomial
path already uses and `B5` already hardened. `mpmath` installed as the exact 60-dps reference for
the precision tests.

### (B) The dispersion estimator — **NOTHING FIT; WRITTEN HERE**

- **`statsmodels` 0.14.6** (installed) — walked the entire module tree for
  `betabin|icc|overdisp|dispers`: **zero matches**. `othermod.betareg.BetaModel` exists but is
  **Beta regression on a continuous (0,1) response**, a different model from clustered binomial
  counts; it cannot consume `(n_i, k_i)` pairs and does not produce an ICC. **Rejected on model
  shape.**
- **`betabinomial` 0.0.2** (PyPI, downloaded and read) — **rejected on four mechanical grounds:**
  last release **2022-02-17**, two releases ever, still `0.0.x`; the sdist ships **no tests at
  all** (`betabinomial/betabinomial.py` + `__init__.py`, nothing else); it pulls `tqdm` and
  `statsmodels.stats.multitest` onto the path; and its `infer` fits a **separate `(α, β)` per
  ROW** by digamma iteration for aberration detection in genomic count matrices. This problem has
  **one `(α, β)` per session shared across instruments**, and wants the leave-one-out ICC — the
  opposite decomposition. Wrong model, unmaintained, untested.
- **PyPI has no dedicated package for this:** `pybetabinomial`, `overdispersion`,
  `icc-estimator` all return **404** from the JSON index.

**Verdict: write it.** The Pearson-χ² moment estimator of §3.1 is closed-form, roughly fifteen
lines, and the `O(1)` leave-one-out identity of §3.2 is specific to this engine's accumulator —
no library would have provided it. Correctness is pinned by property test 7 (recover a planted
`ρ` from simulated beta-binomial clusters, return `0` on simulated binomial ones), which is a
stronger check than a dependency would have come with, given the candidate ships no tests.

---

## 6. Test plan (`R.23(c)`, written before the implementation)

**Unit.**
1. `ρ = 0` reproduces `binomial_upper_tail` to 1e-12 across a grid of `(k, n, p)`.
2. The tail is monotone decreasing in `k` and, at fixed `k/n`, increasing in `ρ`.
3. `Σ_k pmf(k) = 1` to 1e-12 for representative `(n, p, ρ)`.
4. A tail whose true value is `~1e-20` is returned to full relative precision, not `0.0` —
   the `B5` regression, re-run against the beta-binomial.
5. `smallest_trials_that_can_reject` is monotone increasing in `ρ`, agrees with the binomial at
   `ρ = 0`, and returns `None` exactly when no `n` suffices.

**Property.**
6. Leave-one-out by running sums equals leave-one-out recomputed from scratch, to 1e-12, over
   randomised cluster sets — the `O(1)` derivation is only worth having if it is provably the
   same number.
7. `ρ̂` recovers a planted `ρ` on simulated beta-binomial clusters within sampling error, and
   returns 0 on simulated pure-binomial clusters.

**Adversarial** — each reproduces a way the *previous* implementation was wrong.
8. **The `B1` case:** an instrument with 2 comparisons, both disagreeing, in a session whose other
   instruments show measurable overdispersion, returns `JOIN_UNVERIFIABLE` — and the same
   instrument in a session with `ρ̂ = 0` returns `JOIN_REFUTED`. The verdict must turn on the
   measured dispersion and nothing else.
9. **The over-rejection case:** a synthetic session generated with **no broken join** but real
   between-instrument dispersion produces refusals at approximately the significance level, where
   the binomial null produces many times that. This is the ~594-against-121 finding as a test.
10. **The self-masking case:** one instrument disagreeing on every one of many comparisons is
    still refused — it must not be able to inflate `ρ̂` enough to excuse itself. Fails against a
    non-leave-one-out dispersion estimate.
11. **The `A.122` lesson applied:** every fixture above carries **at least three instruments**.
    `B1`'s original miss was a single-instrument fixture where the leave-one-out null is `None`
    and the verdict fell through for an unrelated reason.

**Store.**
12. A store holding rows under two different `null_model` values reports
    `is_internally_consistent == False`.
13. A store migrated from the pre-rename shape keeps its rows, exposes
    `disagreement_upper_tail`, and no longer exposes `binomial_tail`.

**`R.05`.** Re-run `scripts/verify_bar_tape_join_on_real_data.py --all` over 2026-08-11, -12 and
-13 at the paper loop's own staleness quantile (`B6`: the same 0.95 the fills obey, not 0.99),
and record verdict movement, `ρ̂` per session, and whether `HINDPETRO` and `XCHANGING` survive.

**`R.23(c)` adversarial review** in a fresh subagent, blocking, per `A.123` decision 4.
