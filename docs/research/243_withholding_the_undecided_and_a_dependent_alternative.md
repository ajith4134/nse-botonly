# 243 — Withholding the undecided, and an alternative that is not independent either

**`A.125`.** Closes the two questions `A.124`'s review left open — backlog `M31` and `M32` — both
by measurement, and both delegated by the operator. Engine:
`market_depth/bar_tape_join_verification_engine`. Consumer:
`scripts/verify_paper_session_on_real_data.py`. Prior: `docs/research/241`, `242`.

**Sourcing:** no new component. `M31` is a consumer wiring change over an existing store; `M32`
replaces one distribution with another already implemented and tested in this module
(`BetaBinomialDisagreementNull`). The sourcing pass covering both is `241` §5b.

---

## 1. `M31` — the loop treated "could not decide" as "cleared", and my published cost was wrong

`scripts/verify_paper_session_on_real_data.py` filtered its instrument list through
`refuted_instruments_for(...)` — **`JOIN_REFUTED` only**. An instrument the verification could not
decide about was handed a replayed book and traded exactly like one it had cleared. After `A.124`
demoted the thin instruments, that population grew rather than shrank.

### 1.1 The correction to my own number, first

I recorded in `BACKLOG.md` that withholding the undecided would cost **"~67% of the widest
session's universe"**, and used that figure as the argument for leaving the behaviour alone. It is
wrong by roughly five times, and the mistake was counting instruments that cannot trade at all.

`instruments_priced_on` selects instruments with `EXISTS (SELECT 1 FROM price_bars …)`, so an
instrument with no bars on the session **never enters the loop**. Measured against the verdict
store:

| session | unverifiable | of which **0 comparable bars** | of which **0 bars in the store at all** | genuinely undecided |
|---|---|---|---|---|
| 2026-08-11 | 6,084 | 5,751 (94.5%) | **5,673** | 333 |
| 2026-08-12 | 10 | 5 | 2 | 5 |
| 2026-08-13 | 49 | 2 | 2 | 47 |

Against the loop's ACTUAL candidate set, **under the configuration this document ships** (claim
0.75, dependent alternative — the first draft of this table measured claim 0.5 under the Binomial
alternative that §2.3 retires, and overstated the cost by up to 6x):

| session | candidates | verified | refuted | undecided | **cost of withholding** |
|---|---|---|---|---|---|
| 2026-08-11 | 3,327 | 3,039 | 20 | 268 | **8.7%** |
| 2026-08-12 | 1,418 | 1,412 | 2 | 4 | **0.4%** |
| 2026-08-13 | 650 | 642 | 1 | 7 | **1.2%** |

*(Under the retired configuration the same measurement gave 12.4% / 0.6% / 7.2%. Both are far from
the 67% I published; the shipped number is the smaller one, so the argument for withholding is
stronger than the draft made it.)*

8.7% is a cost worth paying for a verdict that means what it says. 67% was not, which is exactly
why the wrong number mattered: it was load-bearing for a recommendation to leave a defect in
place.

### 1.2 What was built

`instruments_not_cleared_for(session_date, store)` returns `JOIN_REFUTED ∪ JOIN_UNVERIFIABLE` as a
`frozenset[int]`, the same shape the replay engine already accepts.

**It sits BESIDE `refuted_instruments_for` rather than replacing it**, and that is the design
point. The two answer different questions — *"which instruments were MEASURED to describe two
different markets"* against *"which instruments the loop has no positive evidence about"* — both
have callers, and folding them together would destroy the `A.41` three-way partition inside the
consumer while preserving it in the store, which is the worst of both.

The loop now prints the split, so the two exclusions can never be read as one:

```
bar/tape join (2026-08-11): 3039 verified, 20 refuted, 5941 unverifiable —
  288 of this run's 3327 instruments withheld (20 refused, 268 undecided)
```

---

## 2. `M32` — the alternative reinstated the assumption the null had just removed

`A.123` replaced the binomial null because comparisons within one instrument are correlated:
disagreement propensity is a property of the instrument, and 24.45% of two-sided snapshots have the
tape's own last price outside its own bracket. `A.124` then modelled the ALTERNATIVE — the broken
instrument the test must be able to catch — as **Binomial**.

The old docstring defended that on the *rate* ("a specific broken one, not another draw from that
population"), which is fair and answers a different objection. It said nothing about
within-instrument dependence. `docs/research/240`'s finding 1 — *"one fact counted forty times"* —
is about the **sampling geometry**, and the geometry does not care why an instrument disagrees. Two
comparisons of the same instrument five minutes apart are not two independent facts about it
whether it is broken or sound.

### 2.1 The correction

The alternative is now `BetaBinomialDisagreementNull(claim, session_rho)` — beta-binomial at the
session's own estimated intra-instrument correlation, so **both sides of the test make the same
assumption**. The claim is read as a population MEAN rather than a point: *"an instrument whose
disagreement rate is around this, with the dispersion this session's instruments actually show"*.

At `claim = 1.0` the two models coincide exactly, because an instrument that disagrees on every bar
does so under any correlation. **That is where every defect this engine has actually found sits** —
`HINDPETRO` 60/60, `XCHANGING` 56/56 — so for the observed defect population the correction changes
nothing at all.

### 2.2 What it costs, measured

Fewest comparisons for power >= 0.99, independent alternative -> dependent alternative:

| claim | 2026-08-11 | 2026-08-12 | 2026-08-13 |
|---|---|---|---|
| 1.00 | 2 -> 2 | 2 -> 2 | 2 -> 2 |
| 0.90 | 4 -> **6** | 5 -> **6** | 5 -> **6** |
| 0.80 | 7 -> **11** | 7 -> **8** | 7 -> **11** |
| **0.75** | 8 -> **12** | 8 -> **9** | 9 -> **12** |
| 0.50 | 22 -> **240** | 19 -> **33** | 25 -> **unreachable** |

Across this band the correction RAISES the bar, so over the range the real sessions occupy nothing
was being wrongly refused — instruments were being verified on *less* evidence than a
dependence-aware alternative demands.

> ⚠️ **CORRECTED IN PLACE by the `A.125` review (`H1`). The first draft of this paragraph said the
> correction "can only RAISE the bar", universally. That is FALSE.** Where the rejection boundary
> sits at `k* = n`, power collapses to `P(K = n)`, and this engine's own
> `log_probability_everything_disagrees` records that the beta-binomial's `P(K = n)` decays
> **polynomially** where the binomial's decays **geometrically** — so the dependent model has MORE
> power there and demands LESS evidence. Measured: at `p = 0.021679`, `rho = 0.6`, claim 0.995, the
> **dependent bar is 3 and the independent bar is 7**, and a sweep found 310 gate-flip cells across
> 20 parameter groups.
>
> This mattered because `/microstructure` prints the FIXED-RATE guarantee ("could have caught an
> instrument disagreeing on 75% of its comparable bars"), which the population reading does not
> deliver. **`has_power_to_verify` now requires power under BOTH readings and takes the weaker.**
> It costs nothing where they agree — the operating bars stay 12/9/12 — and it makes the sentence
> on the surface true under either interpretation of the claim.
>
> It is the third load-bearing number in this slice that came from an argument rather than a
> command (`O.108`), and the second one in this document.

### 2.3 Why the operating claim moves to 0.75

A 0.5 claim is **unreachable on two of the three sessions** once the assumption is removed. The
honest reading is not that 0.5 became impossible, but that it was never actually being delivered:
`A.124` chose 0.5 on the strength of the 22/19/25 stability, and that stability was computed inside
an assumption worth up to 11x.

**0.75 with a dependent alternative — bars 12/9/12 — is both more defensible and CHEAPER in
evidence than 0.5 with an independent one at 22/19/25.** The trade is a slightly weaker stated
claim under a model that means what it says, against a stronger stated claim under one that
overstated its own power. It remains an operator input with no default, so the choice is re-made on
every run.

---

## 3. What this does NOT settle

- **The claim is still a judgement inside an affordable band.** 0.9 (bars 6/6/6) and 0.8 (11/8/11)
  are equally affordable and make stronger claims; 0.75 was chosen for margin below the 100% every
  observed defect shows, not because the band has a natural point in it. `O.107`.
- **`M29` is unchanged.** A saturating instrument still poisons every other instrument's
  leave-one-out null, and now also its alternative, since both read the same `rho-hat`. Same
  reachability as before: it needs an otherwise exactly-clean session.
- **The two consumers deliberately disagree about "cleared", and it is asymmetric on purpose.**
  `dashboard_server.py` still gates `/microstructure` on `refuted_instruments_for` alone, so the
  268 instruments the paper loop now withholds still appear on the surface. That is the intended
  split: the loop is a CAPITAL path, where an unexamined join makes the P&L uninterpretable, and
  the surface is a DIAGNOSTIC one, where hiding the undecided instruments would remove exactly the
  population a reader needs to see in order to judge the capture's coverage. The panel already
  reports `unverifiable` as its own tile, so nothing is being passed off as verified. Recorded
  because "both readers have callers" is not by itself a reason for them to differ.
- **The 8.7% withheld on 2026-08-11 are not a fixed cost.** They are instruments with between 1
  and 11 comparable bars; a fuller depth capture moves them into the verified set rather than
  requiring a policy change. That is an argument for `L0.33`'s scheduled capture, not against this
  gate.

---

## 4. The `R.23(c)` review of this slice — 1 HIGH, 7 MEDIUM

Third consecutive mandatory review on this engine. 16 mutations.

**What it could not break, verified rather than read.** Every figure in §1.1's split (5,673 /
5,751 / 94.5%), every candidate count (3,327 / 1,418 / 650), and **all fifteen cells** of §2.2's
table were recomputed independently and reproduce exactly. `instruments_not_cleared_for`'s `A.41`
branch is correct — the sole caller tests `coverage is None` first, so an unrun session prints
`Proceeding UNVERIFIED` rather than silently trading. `refuted_instruments_for` is byte-identical,
so `dashboard_server.py` still receives refusals only. Cost of the dependent alternative: **1.00x**
— the scan is dominated by `_fewest_disagreements_that_reject` on the NULL side, and the
alternative is one extra call.

**`H1` (HIGH)** — §2.2's central safety claim was false; corrected in place above, fixed by
requiring power under both readings.

**`M5`** — §1.1's cost table was measured under the configuration §2.3 retires. Restated at the
shipped configuration: 8.7% / 0.4% / 1.2%, verified against the store. The argument gets stronger.

**`M6`** — **the entire consumer change was untested.** No test imported the script, and three
independent mutations survived the full suite: reverting the filter to refusals only (i.e. undoing
`M31` outright), mis-counting the refused/undecided split, and gutting the unrun-session warning.
Fixed by extracting `admit_instruments_for` into a testable function and pinning all three cases,
with `pythonpath = ["scripts"]` added so a test may import the operational entry points at all —
the same argument that brought `scripts/` into the mypy gate after `A.65`.

**`M7`** — a test named `..._is_unverifiable` asserted
`verdict is JOIN_UNVERIFIABLE or null_disagreement_rate` and **passed through the `or` while the
verdict was `JOIN_VERIFIED`**. It asserted the opposite of its own name for two review rounds. Its
fixture used the default claim of 1.0, where the bar is 2 and 24 comparisons have full power, so
there was no trough to land in. Replaced with a fixture that genuinely does: against a backdrop of
eight instruments at 40 comparisons and 4 disagreements, at a 0.75 claim, **nine agreeing
comparisons verify and ten do not**.

**`M2`/`M3`/`M4`/`M8`/`L-9`/`L-11`/`L-12`/`L-13`/`L-15`** — a docstring still asserting the model
`M32` deleted; pre-`M32` figures presented as current; the scan-cap constant an `R.06` orphan; the
two consumers' disagreement undocumented (now §3); a dead `rate >= 1.0` arm that silently accepted
rates ABOVE 1 where the previous form raised; an unpinned float boundary; the independent column
unpinned; a refusal message naming only the staleness quantile when the check now covers four
policies; and a unit test taking 67-80 seconds by scanning sample sizes no instrument can reach.

### 4.1 The mutation table, and the finding that keeps recurring

Five real survivors — **three of them on the consumer script, which no test touched**, exactly the
shape of the previous round's "six of eight survivors were on the store/surface side, where the
write -> read -> render chain was untested end to end". The gap is never in the algorithm; it is
always in the wiring that carries the algorithm's answer somewhere.

**And `LARGEST_TRIALS_SEARCHED_FOR_POWER` survived mutation for the THIRD consecutive round** —
after `2**53 -> 2**6` in round one and `1000 -> 600` in round two, this time on the very constant
round two's fix was written to pin. The round-two test passes the cap explicitly, so it never
touches the default. Now pinned by asserting the published 240, which costs 0.9 seconds.

The generalisable rule: **a test that passes a parameter explicitly does not pin that parameter's
default.** That is three rounds of the same lesson arriving through the same door.

---

## 5. `R.05` — both changes on real data

**The join verification, all three sessions, under the corrected gate (claim 0.75, dependent
alternative, power required under both readings):**

| session | verified | refuted | unverifiable | `rho-hat` | demand | fewest |
|---|---|---|---|---|---|---|
| 2026-08-11 | 3,039 | 20 | 5,941 | 0.0726 | 12 | 12 |
| 2026-08-12 | 1,412 | 2 | 6 | 0.0399 | 9 | 11 |
| 2026-08-13 | 642 | 1 | 9 | 0.0740 | 12 | 12 |

**Identical to the run before `H1`'s fix, on every session and every figure.** That is the intended
result: taking the weaker of the two power readings only bites where they disagree, and at the
sessions' measured `rho-hat` of 0.04-0.074 they agree. The guard costs nothing and closes a case
that is reachable at higher dispersion. `on<=2`/`on<=5`/`on<=10` are zero throughout.

**The paper loop, 2026-08-13, 150 instruments** — the first real run of `M31`'s admission path:

```
bar/tape join (2026-08-13): 642 verified, 1 refuted, 9 unverifiable —
  2 of this run's 150 instruments withheld (0 refused, 2 undecided)
  of those, 1 have a RESCALED bar series (`M26`) — the signal and the fill are denominated
  differently:
    token 359937: bar_close = 0.95100 x traded price
...
2026-08-13: 76 steps (29 with a recorded book) over 148 instruments, 10 order(s) placed,
  10 position(s), gross Rs -1761.70 less costs Rs 702.72 = net Rs -2464.42;
  balance Rs 1000000.00 -> Rs 997535.58
```

**Two instruments were withheld that the previous code would have traded**, and the line names why
— `0 refused, 2 undecided`. That is the whole of `M31`: not that more instruments are excluded, but
that the reason is stated and the two reasons are not the same reason. The run completed with no
unhandled exception, every decision carrying an outcome (4,145 abstained, 114 refused by the gate,
10 placed), and the closing balance equal to the fold of the ledger's events.

`R.05` satisfied for both `M31` and `M32`.
