# 242 — The corrected null on three real sessions, and the second adversarial review

**`R.05` record for `A.123` / `L0.36`, plus the `R.23(c)` review that gated it.** Spec:
`docs/research/241`. Previous review: `docs/research/240`. Run 2026-08-16.

```
# the pass recorded in §1, under the null replacement alone
scripts/verify_bar_tape_join_on_real_data.py --all --staleness-quantile 0.95 --significance 0.01

# re-run under the POWER gate added by `A.124` (§6), which adds a third policy input
scripts/verify_bar_tape_join_on_real_data.py --all --staleness-quantile 0.95 \
    --significance 0.01 --minimum-detectable-disagreement-rate 0.5
```

Staleness 0.95 because that is what `verify_paper_session_on_real_data.STALENESS_QUANTILE` is, and
`B6` was the finding that verifying at 0.99 while fills obey 0.95 invalidated the previous pass.

**Sourcing:** this is a RESULTS record, not a design doc — it introduces no new component. The
sourcing pass for everything measured here is `docs/research/241` §5b, which probed
`scipy.stats.betabinom` (part-adopted as the test oracle, its `sf` rejected on measured underflow),
rejected `statsmodels` on model shape and PyPI `betabinomial` 0.0.2 on four mechanical grounds, and
established that no package ships the leave-one-out ICC estimator. Nothing new was sourced for this
run; `scipy-stubs` was installed as tooling and is recorded in §4.

---

## 1. The measurement

| session | instruments | verified | refuted | unverifiable | bars comparable | pooled rate | **`rho-hat`** | bar |
|---|---|---|---|---|---|---|---|---|
| 2026-08-11 | 9,000 | 3,204 | **20** | 5,776 | 165,042/210,196 (78.5%) | 2.1679% | **0.0726** | 2 |
| 2026-08-12 | 1,420 | 1,413 | 2 | 5 | 75,233/104,878 (71.7%) | 2.9203% | **0.0399** | 2 |
| 2026-08-13 | 652 | 649 | 1 | 2 | 17,584/47,687 (36.9%) | 2.5591% | **0.0738** | 2 |

All nine deviation deciles are 0.00 spread-units on every session: where the two stores are
comparable at all they agree to the paise, which is the same result `docs/research/237` got and is
unchanged by the null.

### 1.1 The over-rejection is fixed, and this was the point

**2026-08-11 refusals: 121 -> 20.** Twenty of 9,000 is **0.22%** against a 1% significance, which is
a test rejecting *below* its nominal size rather than at five times it. `docs/research/240`'s
modelling said a session with no broken join would produce ~594 false refusals under the binomial
null against the 121 observed; the corrected null does not produce them.

### 1.2 Every falsification check in `241` §5 passes

- **`HINDPETRO` (token 359937) survives on ALL THREE sessions** — refused at a constant 0.95099 on
  60/60, 53/53 and 29/29 comparable bars. `241` §5 named its survival as the check that the
  correction has not overshot. It has not. **`XCHANGING` (2996481) survives** on 2026-08-11 at
  0.96959 on 56/56.
- **`rho-hat` is neither 0 nor near 1** — 0.0399 to 0.0738 across the three. Zero would have said
  the binomial was adequate all along; near-one would have falsified the beta-binomial model and
  sent this to a random-effects logistic null. It is small, positive and stable, which is what a
  genuine between-instrument effect looks like.
- **Backlog `M29` does not materialise on real data.** The moment estimator's saturation on a
  single extreme cluster needs the rest of the session to be EXACTLY clean; real tapes disagree at
  ~2-3%, and `rho-hat` lands two orders of magnitude below its ceiling. `M29` stays open as a
  known property, downgraded from a risk to a curiosity.

### 1.3 `B1` is NOT closed by the null replacement, and the numbers say so plainly

*(Closed separately by the power gate of §6 / `A.124`. This section records the state as the null
replacement left it, which is what the decision was made from.)*

`smallest_trials_that_can_reject` answers **2** on every session, because `rho-hat` at 0.04-0.07 is
far too small to move it — exactly as `241` §1.1 predicted once the arithmetic was corrected.

| session | verified | fewest comparisons behind a VERIFIED verdict | on <=2 | on <=5 | on <=10 |
|---|---|---|---|---|---|
| 2026-08-11 | 3,204 | **2** | **15** | 65 | 136 |
| 2026-08-12 | 1,413 | 7 | 0 | 0 | 1 |
| 2026-08-13 | 649 | 4 | 0 | 2 | 7 |

On the widest session this project holds, **15 instruments are reported `JOIN_VERIFIED` on two
comparable bars and 136 on ten or fewer**, and they trade as verified joins. `A.122`'s original
figures (14 on 2, 130 on <=10) are essentially reproduced. **The null replacement did not touch
this.**

---

## 2. Why it did not, stated properly this time

`docs/research/240` `B1` is: *"`smallest_trials_that_can_reject` returns 1 whenever the
leave-one-out null is 0, so a single agreeing bar clears the gate."* `241` §1 restated it as "two
comparable bars" and argued the rule was reading the wrong distribution. **Both the restatement and
the argument were wrong, and the second review caught it** (`H2`). Reproduced: the exact fixture
`A.122` named, plus two clean instruments, gives `verifiable=1 of 100 bars · null p=0.0, rho=0.0 ·
min trials 1 · JOIN_VERIFIED`. At `p̂' = 0` the estimator forces `rho-hat = 0` too, so the
beta-binomial cannot reach that path at all.

**The real defect is that one gate is being asked to do two jobs.**
`smallest_trials_that_can_reject` answers *how many comparisons before I COULD refuse*. That is a
**size** question, and it is the correct gate for a `JOIN_REFUTED` verdict — an instrument refused
on fewer comparisons than that would be refused by the sample rather than by the evidence.

`JOIN_VERIFIED` needs the opposite question — *how many AGREEING comparisons before absence of
disagreement is evidence of agreement* — which is a **power** question, and it has a different
answer. Two agreeing bars carry essentially no power against any alternative except total
disagreement. The engine currently uses the size gate in both directions, which is why the honest
`JOIN_UNVERIFIABLE` state exists and is still not reached by the instruments that most need it.

**And the answer depends on what a verified join is required to rule out**, which is a policy
question and not a statistical one — so it goes to the operator rather than being picked here
(`R.19`). It is put in `A.124`.

---

## 3. The second adversarial review (`R.23(c)`, mandatory and blocking per `A.123` decision 4)

Fresh subagent, told to break the change rather than confirm it, with the standing instruction from
`A.122` — *for every new test, would it still pass if the mechanism it names were broken?* It ran
**27 source mutations**. Findings, all fixed and each with a regression test that reproduces the
original failure:

| | severity | the defect |
|---|---|---|
| `H1` | HIGH | `_log_beta(shape_disagree + count, shape_agree + trials - count)` associates left-to-right. With `rho` at its clamp `shape_agree` is ~1.1e-16, so `shape_agree + trials` rounds to `trials` and the subtraction is **exactly 0.0** — `math.lgamma(0.0)` raises a bare `ValueError` from inside a verification. Reachable on any session with a couple of all-disagreeing instruments. Fixed by parenthesising; `_log_beta` now rejects a non-positive shape with `JoinVerificationError`. |
| `H2` | HIGH | `B1` misread and not fixed — §2 above. |
| `M1` | MED | The doubling+bisection search differences two `lgamma` terms that reach ~1e17, so past a certain `n` it bisects on **rounding noise** and returns a fabricated finite answer — measured at **2,101,930,071,441,053**, written to the store and rendered. Fixed: a log-probability cannot be positive, so the search now stops when the closed form returns one. A derived stop, and it fires ~3 orders of magnitude before `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT` would have. |
| `M2` | MED | `rho-hat = (X^2 - (N-1)) / sum(n_i - 1)` is the wrong normalisation: the degree of freedom spent estimating `p` SCALES the sum rather than subtracting a bare one. Measured over 4,000 replications at a planted 0.200 — **0.124 (N=3), 0.154 (N=5), 0.175 (N=10)**, converging only by N=4,000. The bias runs LOW, and a low `rho` is a NARROW null, which is over-rejection — the very failure being fixed, reappearing on small sessions and on every `--limit` probe. Fixed with the `(N-1)/N` factor: 0.187 / 0.193 / 0.195. |
| `M3` | MED | `is_internally_consistent` never checked **`significance`** — the engine's only policy input, which decides every verdict — while `MAX(significance)` reported the LOOSEST of a mixed set as though it were in force. The identical argument that justified recording `staleness_quantile` and `null_model`. Fixed. |
| `M4` | MED | A docstring of mine claimed one 40/40 cluster contributes "~8,000 of `X^2` against ~200 expected". Measured on that exact fixture: **400.0 of 440.0, against 10**. Wrong by twenty times, and the same defect class as the comment `240` retracted a sign-off over. Corrected in place with the measurement. |
| `L1` | LOW | `null_intra_instrument_correlation` was written on every row and read by NOTHING — an orphan (`R.06`) holding the one number that says whether the change did anything. Now on `SessionVerificationCoverage` and on the `/microstructure` panel. |
| `L2` | LOW | `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT` was tested against the CURRENT bracket before doubling, so a returned `n` could reach `2**54` — a constant that does not bound what its name says. Now tested against the next bracket. |
| `L4` | LOW | The accumulator absorbed impossible clusters: `k > n` produced a rate above 1 downstream, and `verifiable=0` silently discarded its disagreements. Both now raise. |

`L3` (SQLite rendering two float-distinct staleness quantiles as one string) is cosmetic —
`is_internally_consistent` counts rather than dedupes, so it is still correctly `False`. Left.

**What the review attacked and could NOT break**, recorded because it bounds where the risk is not:
`refuted_instruments()`'s shape; the store migration across all three historical schemas; the
`# noqa: S608` (`_readable_column` raises outside its allow-list); the leave-one-out identity
`(S1 - p̂·S2)/(p̂(1-p̂))`, exact to 1e-13 against a from-scratch recomputation; pmf normalisation
to 1.0 ± 2e-14; and — exhaustively, over 199 `rho` x 12 `p` x 9 `n` x every `k` at three
significances — **zero cases where a previously-VERIFIED instrument becomes REFUTED**.

### 3.1 The finding that matters most: seven of 27 mutations survived the first test pass

Every one on a mechanism a test above it claimed to cover. This is `A.122`'s lesson recurring
inside the slice written to apply `A.122`'s lesson.

| mutation | why it survived |
|---|---|
| delete the df correction (`X^2 - (N-1)` -> `X^2 - N`) | the recovery test used 4,000 clusters, where the correction is worth 6e-6 against a 0.02 tolerance |
| the `(N-1)/N` divisor itself | same fixture, same reason — the normalisation was unpinned in BOTH directions |
| `return upper` -> `return upper + 1` in the bisection | the monotonicity test survives a uniform +1, and its only equality check is at `rho = 0`, which takes the delegation branch and never enters the search |
| `< minimum_trials` -> `< minimum_trials - 1` | the evidence gate — the whole of `B1` — had no boundary test at all |
| `FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION` 2 -> 1 | its test used `over([(40, 8)])`, where `X^2 = 0` and `N-1 = 0` arithmetically, so `rho-hat` is 0 whatever the guard says |
| `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT` 2**53 -> 2**6 | nothing asserted a large-but-finite answer |
| remove the low clamp on `rho-hat` | no fixture ever produced `X^2 < N-1` |

**Nine tests added, and all eight re-run mutations now die.** The df error needed an exact
hand-computed fixture rather than a simulation — three clusters of 40 with 10, 5 and 15
disagreements gives `rho-hat = (6.6667 - 2)/78 = 0.05982905982905983`, where the two surviving
mutations land at 0.0470 and 0.0399. **A tolerance wide enough to absorb sampling noise is wide
enough to absorb a degrees-of-freedom error**, which is the generalisable lesson and the reason
simulation-based tests need a deterministic companion.

### 3.2 Two planned tests that were never written, and one of them was the one that mattered

`241` §6 item 8 — *the `B1` case: an instrument with 2 comparisons both disagreeing returns
`JOIN_UNVERIFIABLE`, and the same instrument in a `rho-hat = 0` session returns `JOIN_REFUTED`* —
**was specced and not written, and it is the single test that would have surfaced `H2` before the
review did.** Item 3 (`sum pmf = 1`) was also absent; now written. Recorded because writing a test
plan and then not executing it is a failure mode with no external symptom.

---

## 4. Also found while building, outside the review

- **Reads open the store `mode=ro`, so they cannot migrate it.** Every read named the
  migration-added columns unconditionally, so any store written by an earlier version raised
  `OperationalError` — which on the dashboard reads as an outage, not as an old store. Fixed with
  an allow-listed column substitution.
- **The `/microstructure` join panel never surfaced `is_internally_consistent` at all**, so a
  session written under two policies rendered as one clean verification. `B6`'s lesson had reached
  the store and stopped there. The panel had no test of any kind; it now has five.
- **`scipy-stubs` was installed for these tests and made every scipy call in the repository
  checkable for the first time.** It immediately surfaced two real defects in
  `exchange_clock_offset_estimator`: `linprog`'s `method` passed as a bare `str` (a typo would have
  failed at runtime inside the clock fit), and `solution.x` indexed without checking it is not
  `None`. Both fixed; the stubs are pinned in `pyproject.toml`. Same argument as bringing
  `scripts/` into the gate after `A.65`.
- **The live verdict store did not exist.** `~/.nse_algo_trader/bar_tape_join_verdicts.sqlite3` was
  absent, so `A.122`'s "14 verified on 2 bars" could not be re-read and this pass rebuilt it from
  nothing. Every session in it is now written under one staleness quantile, one significance and
  one null model.

---

## 5. Verdict

The **overdispersion half of `A.122` is closed on real data.** The **`B1` half was open, measured,
and correctly diagnosed here for the first time** — a power question wearing a size gate — and is
closed by §6 / `A.124`, with what a verified join must rule out exposed as an operator input rather
than assumed.

---

## 6. The power gate — measurements behind `A.124`

Operator delegated the choice. Everything below was computed against the three sessions' own fitted
nulls at a 1% significance BEFORE anything was recommended, because `O.102` was not.

### 6.1 A derived alternative was tried first, and it fails

The elegant design is to choose nothing: let the alternative be the `(1 - significance)` quantile of
the session's OWN fitted Beta. **It does not work.** Power against it plateaus at **~0.40 even at
n = 400** and never approaches `1 - significance`:

| n | 2 | 5 | 10 | 20 | 30 | 57 | 100 | 200 | 400 |
|---|---|---|---|---|---|---|---|---|---|
| power (2026-08-11, `pi* = 0.1892`) | 0.036 | 0.050 | 0.103 | 0.162 | 0.194 | 0.273 | 0.335 | 0.376 | 0.403 |

The reason is worth keeping: at `rho-hat = 0.0726` that quantile is `pi* = 0.19`, **8.7x the
pooled mean**, and an instrument sitting there is a *normal* instrument under the session's own
null. A test calibrated to spare ordinary instruments cannot be asked to catch one.

A `(1 - 1/N)` Bonferroni-style derived alternative also fails, differently — it is unstable:

| | 2026-08-11 (N=9,000) | 2026-08-12 (N=1,420) | 2026-08-13 (N=652) |
|---|---|---|---|
| derived `pi` | 0.4132 (19x null) | 0.2412 (8x null) | 0.3053 (12x null) |
| power bar | 34 | **156** | **129** |

156 and 129 sit above those sessions' median comparison counts (54 and 28), so it would refuse
verification to most of two sessions and not the third, for no reason a reader could defend.

### 6.2 Fixed alternatives, and the counterintuitive result

Fewest comparisons for power `>= 0.99` against a fixed alternative:

| the claim | 2026-08-11 | 2026-08-12 | 2026-08-13 | spread |
|---|---|---|---|---|
| 0.25 | 298 | 136 | 525 | **3.9x** |
| 0.40 | 39 | 32 | 45 | 1.4x |
| **0.50** | **22** | **19** | **25** | **1.3x** |
| 0.75 | 8 | 8 | 9 | 1.1x |
| 1.00 | 2 | 2 | 2 | 1.0x |

**A FIXED alternative is more stable here than a derived one**, across a 14x range of universe
size. `rho-hat` and instrument count move the bar in opposite directions, so anything that scales
with the session amplifies rather than cancels. Recorded because it cuts against the instinct
`R.03` trains, and it does not violate it: `R.03` forbids a tuned threshold on the data, and this is
not one — it is the definition of the claim, the same category as `significance`, which has been a
required argument with no default since the engine was written.

**0.50 chosen** (`O.104`): the point where the stores disagree at least as often as they agree, far
above the null's own 99th percentile of 0.19 so it is genuinely outside the null, far below the
100% every real defect has shown (`HINDPETRO` 60/60, `XCHANGING` 56/56) so a margin remains, and a
bar of 22 against a median of 57 comparisons. Exposed as `--minimum-detectable-disagreement-rate`
with no default, so the choice is the operator's on every run.

### 6.3 The zero-null case falls out, with no special case

`docs/research/240` `B1`'s literal mechanism — `p̂' = 0` gives a size bar of **1**, so one agreeing
bar verified — resolves without a branch on `p == 0`:

- refuting is unchanged: at `p = 0` any disagreement is decisive, so the rejection boundary is
  `k >= 1` and a token collision in a clean session is still caught on one bar;
- verifying now needs `P(K >= 1 | n, 0.5) = 1 - 0.5**n >= 0.99`, first true at **n = 7**.

Asymmetric, derived, and measured before it was claimed — the failure mode of `O.102`.

### 6.4 What the gate cannot do

Applied AFTER the test, so `refuted_instruments()` — the only output the replay engine consumes —
is unchanged by construction. `test_the_power_gate_never_takes_a_refusal_away` asserts the refusal
set is identical at claims of 1.0, 0.75, 0.5 and 0.25. A correction that silently stopped refusing a
broken instrument would be worse than the problem it fixes.

### 6.5 `R.05` re-run under the power gate — `B1` closed on real data

```
scripts/verify_bar_tape_join_on_real_data.py --all --staleness-quantile 0.95 \
    --significance 0.01 --minimum-detectable-disagreement-rate 0.5
```

| session | verified before -> after | refuted | unverifiable before -> after | `rho-hat` | verify bar | fewest bars behind a VERIFIED verdict |
|---|---|---|---|---|---|---|
| 2026-08-11 | 3,204 -> **2,906** | 20 -> **20** | 5,776 -> 6,074 | 0.0726 | 22 | 2 -> **22** |
| 2026-08-12 | 1,413 -> **1,408** | 2 -> **2** | 5 -> 10 | 0.0399 | 19 | 7 -> **21** |
| 2026-08-13 | 649 -> **602** | 1 -> **1** | 2 -> 49 | 0.0740 | 25 | 4 -> **25** |

**`B1` is closed.** `on<=2`, `on<=5` and `on<=10` are **zero on every session**, against 15 / 65 /
136 on 2026-08-11 before the gate. 350 instrument-sessions moved from `JOIN_VERIFIED` to
`JOIN_UNVERIFIABLE` — 9.3% of the widest session's verified set, 0.4% of 2026-08-12's, 7.2% of
2026-08-13's — and none of them was refused, so none was trading on a broken join; they were
trading on an *unexamined* one, which is the distinction `A.41` exists to preserve and which the
engine was failing to make about itself.

**Refusals are byte-identical on all three sessions**, token list included, which is the property
the after-the-test ordering was designed to guarantee and `test_the_power_gate_never_takes_a_refusal_away`
asserts. `HINDPETRO` survives on all three at 0.95099/0.95099/0.95100, `XCHANGING` on 2026-08-11 at
0.96959.

**One incidental confirmation of `M2`.** `rho-hat` on 2026-08-13 moved 0.0738 -> 0.0740 between the
pre-fix and post-fix runs while 2026-08-11 and 2026-08-12 did not move at all. 2026-08-13 is the
smallest session, and the `(N-1)/N` degrees-of-freedom correction bites at small `N` — exactly
where the review's simulation said it would, observed on real data without being looked for.

**The store is now internally consistent by construction**: every row written by one run under one
staleness quantile, one significance, one null model and one claim.

---

## 7. The `R.23(c)` review OF the power gate — `A.124`'s own slice, reviewed

Mandatory and blocking per `A.123` decision 4. Fresh subagent, 18 source mutations, told to break
the gate rather than confirm it.

**The central claim held, and was verified far more strongly than by reading.** The reviewer
stubbed `smallest_trials_that_can_verify` to return **every integer 1…80 and `None`** — covering
every claim any operator can pass — across 200 randomised sessions: **16,200 verdict passes, zero
changes to the refusal set.** `refuted_instruments()` is invariant under the gate, as designed.

**And every number in §6 and in the new docstrings reproduced exactly** — 22/19/25, 298/136/525,
34/156/129, `π* = 0.1892` at 8.7x the mean, the 0.40 power plateau, `n = 7` for the zero null, and
the median of 57 checked against `SELECT median(comparisons_verifiable)` on the live store. The
two-round false-number streak is broken.

### 7.1 `H1` — HIGH, and live in the store: the gate admitted below the power it advertised

The gate asked `n >= smallest_n_with_enough_power`. **Power is not monotone in `n`** — the
rejection boundary `k*` moves in integer steps, so the curve sawtooths — and the engine's own
docstring said so while the gate assumed otherwise. On 2026-08-11's fitted null at a 0.5 claim:

| n | 22 | 23 | **24** | 25 | 26 |
|---|---|---|---|---|---|
| `k*` | 6 | 6 | **7** | 7 | 7 |
| power | 0.9915 | 0.9947 | **0.9887** | 0.9927 | 0.9953 |

`SELECT COUNT(*) … WHERE verdict='join_verified' AND comparisons_verifiable=24` on the live store
returns **10**. Ten instruments were reported verified at a power the gate's own definition calls
insufficient, with `/microstructure` printing the claim over them. Worst case swept over 56
`(α, p, ρ, claim)` combinations: **0.972 against a required 0.99**, and **0.8906 against 0.95** at
α=0.05.

**Fixed** by replacing the bar comparison with `has_power_to_verify(n, significance, rate)`,
evaluated at the instrument's own `n`. It also removes the question of whether "smallest" is even
the right thing to return — it is not, for a non-monotone function.

### 7.2 `M1` — the bar came from the pooled null; the test an instrument faces is its own

The old comment justified computing the bar once from the pooled null, claiming nine thousand
leave-one-out answers "differ in the fourth decimal". Measured, they differ by **4.8x** on small
sessions: on `[(34,0),(53,5),(36,5)]` the pooled bar is 35 and instrument `(53,5)`'s own bar is
**167**, so it cleared the pooled bar with 53 comparisons and was reported verified while the test
it actually faced had power **0.795**. Same fix: the predicate is asked on the leave-one-out null
already in hand.

### 7.3 `M3` — the scan cost 82-240 seconds per session, all of it unreachable

`O(n³)` over `n <= 1000`, measured at **82.3s / 127.2s / 149.7s / 239.6s** at realistic parameters
and ~560s adversarially — with **143 seconds inside a single unit test**. No instrument can hold
more than the session's widest comparison count (`MAX(comparisons_verifiable) = 67` on the widest
real session), so everything above that was answering questions about sample sizes that cannot
occur. The scan is now bounded by the session's own maximum, passed in from `verify_session`: the
same call that took 82s returns in **0.03s**.

### 7.4 `M4`, `M5`, `L1` — and one repeat that is worth naming

- `M4` — `minimum_comparisons_to_verify` was written on every row and **read by nothing**. This is
  the *identical* `R.06` orphan the previous review found in `null_intra_instrument_correlation`
  (§3, `L1`), shipped again in the same slice one round later. Now on
  `SessionVerificationCoverage` and rendered beside the claim, because a claim without its cost is
  not checkable.
- `M5` — the inconsistency banner reported unrecorded rows for the staleness quantile and the null
  model but not for the claim, so a store 4,000 rows of which predate `A.124` rendered four fields
  that all looked clean while the sentence above asserted "50%" over all of them.
- `L1` — `:.0%` printed a claim of 0.004 as **"0%"**, on both the report line and the panel.

### 7.5 `M6` — and the review's own conclusion about it is WRONG, measured

The reviewer found that one saturating instrument clamps the pooled `rho-hat` to 1, which under the
old form vetoed verification for the entire session, and concluded *"M1's fix (per-instrument LOO
predicate) closes this too"*. **It does not, and the claim was checked rather than adopted:**

| fixture | pooled `rho-hat` | a CLEAN instrument's own leave-one-out `rho-hat` |
|---|---|---|
| one saturator among 4 | 1.0000 | 1.0000 |
| one saturator among 12 | 0.8300 | 0.8544 |
| one saturator among 40 | 0.4937 | 0.5007 |

Leave-one-out removes only the instrument BEING JUDGED, so the saturator sits in everyone else's
null and drives it just as high. What the fix genuinely changed is the *mechanism* — verification
is decided per instrument rather than vetoed session-wide by a single `None` — and the outcome is
unchanged here because the contamination is in the data, not in the pooling.

**And the outcome is correct inference rather than a defect.** If one instrument disagrees on every
bar while the rest agree on every bar, instruments in that session genuinely are all-or-nothing and
agreement on forty bars genuinely says little. Same property and same reachability as backlog
`M29`: it needs an otherwise exactly-clean session, and the three real ones measure `rho-hat` at
0.0399-0.0740. Pinned by a test that asserts the measured behaviour, not the review's conclusion.

### 7.6 `M2` — OPEN, and it is a modelling question rather than a defect

The alternative instrument is modelled as **Binomial(n, rate)** — which reinstates, on the
alternative side, exactly the independence assumption `A.123` removed from the null. The docstring
defends the choice on the *rate* ("a specific broken one, not another draw from that population"),
which is fair, and says nothing about within-instrument dependence, which is a property of the
sampling geometry and applies to a broken instrument as much as to an ordinary one.

**It dominates the chosen number:**

| session | bar under a Binomial alternative | bar if the alternative carries the session's own `rho-hat` |
|---|---|---|
| 2026-08-11 | 22 | **240** |
| 2026-08-12 | 19 | **33** |
| 2026-08-13 | 25 | **>400** |

§6.2's entire case for 0.50 rests on the 1.3x cross-session stability of 22/19/25, and that
stability is computed inside the independence assumption, which is worth up to **11x**. The
shipped model is the PERMISSIVE one — a beta-binomial alternative raises the bar (8 -> 12 at a 0.75
claim), so nothing is being wrongly refused; instruments are being verified on less evidence than a
dependence-aware alternative would demand.

**Not decided here** (`R.19`). Recorded as the sensitivity §6 did not carry, and put to the
operator alongside backlog `M31`.

### 7.7 Mutations — 18 run, 8 survived, and two were repeats of the previous round's survivors

The six real gaps, all now closed with tests that were verified to fail against the mutation:

| mutation | why it survived |
|---|---|
| gate `< bar` -> `< bar - 1` | **the power gate had no boundary test** — verbatim the previous round's surviving `< minimum_trials - 1`, on the gate written to fix it |
| `LARGEST_TRIALS_SEARCHED_FOR_POWER` 1000 -> 600 | **nothing asserted a large-but-finite answer** — verbatim the previous round's `2**53 -> 2**6` survivor |
| write `NULL` for the claim / for the demand | no store test took either new column through SQLite |
| `SUM(claim IS NULL)` -> `0`, `GROUP_CONCAT(claim)` -> `NULL` | the read path was entirely unpinned: every coverage test built the dataclass by hand |
| `_distinct_settings(claims, …)` -> `len(claims)` | `null_model` had this test; the claim did not |
| the claim sentence hardcoded to `"50%"` | every panel fixture used 0.5 |

**Six of the eight survivors were on the store/surface side**, where the new column's whole
write -> read -> render chain was untested end to end. That is the generalisable finding: a
constructor-built fixture tests the dataclass, not the column.

**And the two engine survivors were the same two classes as last round.** Writing the lesson down
did not prevent repeating it; only the mutation run caught it, both times. The standing conclusion
is that `R.23(c)`'s value is in the mutation table specifically, not in the review generally.


### 7.8 `R.05` re-run under the CORRECTED gate — the fix confirms itself to the instrument

```
scripts/verify_bar_tape_join_on_real_data.py --all --staleness-quantile 0.95 \
    --significance 0.01 --minimum-detectable-disagreement-rate 0.5
```

| session | verified before -> after `H1` | refuted | unverifiable | `rho-hat` | demand | fewest |
|---|---|---|---|---|---|---|
| 2026-08-11 | 2,906 -> **2,896** | 20 | 6,084 | 0.0726 | 22 | 22 |
| 2026-08-12 | 1,408 -> **1,408** | 2 | 10 | 0.0399 | 19 | 21 |
| 2026-08-13 | 602 -> **602** | 1 | 49 | 0.0740 | 25 | 25 |

**Only 2026-08-11 moved, and by exactly ten, and `H1` identified exactly ten** —
`SELECT COUNT(*) … WHERE verdict='join_verified' AND comparisons_verifiable=24` returned 10 in the
pre-fix store, and those are the instruments the sawtooth trough at n=24 (power 0.9887 against a
required 0.99) was admitting. Refusals unchanged at 20, `rho-hat` unchanged at 0.0726, the pooled
demand unchanged at 22, and `on<=2`/`on<=5`/`on<=10` still zero.

2026-08-12 and 2026-08-13 are unchanged, which is the other half of the confirmation: their
comparison counts happen not to land in a sawtooth trough, so a correct fix must leave them alone.
Refusals are byte-identical on all three, `HINDPETRO` survives on all three and `XCHANGING` on
2026-08-11.

A verdict count that moves by precisely the number a review predicted, on precisely the one session
that had them, with every other figure fixed, is the strongest confirmation this slice produced: it
means the change did what it claimed and did nothing else.

Full suite after the fix: **2,227 tests, zero failures**.
