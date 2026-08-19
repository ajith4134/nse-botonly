# 262 · Corroborated evidence and conditional coherence — closing `MAJOR-6` and `MAJOR-7` of `L5.31`

**Spec written 2026-08-18, before any code, per `R.23c`.** It closes the two findings
`docs/research/261` left open and explicitly re-opened, and it is a **design change rather than a
patch** — that is the re-reviewer's own words about what the correct version requires.

**What it does not touch.** The three floors, the binning calibrator, the Monte-Carlo seed, the
selection clustering and the size cap all stand as repaired in rounds 1–3. This spec adds a
structural join between two corpora that are currently unrelated, and one cross-check built on it.

---

## 1 · The defect, stated precisely

`TradeQualityFloorEngine` takes two independently fitted objects:

- a `StatedProbabilityCalibrator` fitted on `ForecastOutcome` rows — *(bot, stated probability, was
  it a win)*;
- a `RealisedPayoffDistributionEstimator` fitted on `RealisedTradeOutcome` rows — *(bot, gross
  rupees, costs, notional)*.

**Nothing anywhere asserts these describe the same trades.** They share only a `bot_identity`
string. `docs/research/261` MAJOR-6 measured the consequence: a calibrator fitted on 2,000 forecasts
against a payoff estimator holding **4** trades for that same bot produces a razor-sharp posterior
over four magnitudes and admits at **P = 1.0000**, with no error raised anywhere.

That number comes from `estimate_gross_expectancy_posterior`, which draws
`Beta(1 + c·n, 1 + (1−c)·n)` with `n = calibrated.fitted_on_trades`. After round 2 that `n` is the
PAVA level set at the stated value — the *forecasts* the prediction rests on. **It is not the
evidence the EXPECTANCY rests on**, because an expectancy is a probability multiplied by
magnitudes, and the magnitudes come from the other corpus entirely. A probability certified by 2,000
forecasts and magnitudes drawn from 4 trades is reported with the confidence of the 2,000.

MAJOR-7 is the same hole seen from the other side: `expectancy_posterior_for` — the posterior of the
bot's own realised mean P&L per trade — is an **orphan** with zero call sites, so the assembled
`p·W − (1−p)·L` is never compared against what the bot has actually averaged. The round-2 version of
that comparison was deleted for cause: it put a **conditional** quantity (the modelled expectancy at
one stated value) against a **marginal** one (the whole-record mean). Those two coincide only for a
forecaster carrying no information, so the check penalised resolution — 5% refusals at zero
discrimination, **82%** at high, with the truth held constant at +₹382/trade.

**The correct comparison is like with like: the modelled conditional expectancy against the realised
mean of the trades in that same stated-value level set.** That requires knowing which realised
trades correspond to which forecasts, which is exactly the join that does not exist.

---

## 2 · The join

Both row types gain one optional field:

```python
trade_reference: str | None = None
```

It is the identity of the closed trade the row describes — for the paper corpus,
`f"{session_date}:{position_key}"`, which is already unique per closed position. Optional because
the retained 3,481-trade corpus and every row written before today have no such key, and inventing
one would fabricate a join that was never recorded. **Absence is reported, never assumed.**

Three states, and each is named on the card rather than collapsed:

| state | when | consequence |
|---|---|---|
| `JOINED` | both corpora carry references and they overlap | corroborated evidence and the coherence check are both computed from the join |
| `UNJOINABLE` | one or both corpora carry no references | corroborated evidence falls back to `min(level-set support, the bot's payoff trade count)`; the coherence check reports `UNAVAILABLE` and does not fire |
| `DISJOINT` | both carry references and the intersection for this bot is **empty** | `UNASSESSABLE`. Two corpora that share a bot name and not one trade are not describing the same bot's record, and that is a fact rather than a threshold |

`DISJOINT` is the only new refusal path that does not depend on any tunable, which is why it is safe
to make it absolute (`R.03`).

---

## 3 · Corroborated evidence — closing MAJOR-6

The Beta concentration in `estimate_gross_expectancy_posterior` becomes the **corroborated**
support: the number of forecasts in the level set at the stated value **that join to a realised
outcome**.

```
JOINED     corroborated = |{ forecasts in the level set } ∩ { trades with magnitudes }|
UNJOINABLE corroborated = min(level-set support, payoff trades for this bot)
```

The `UNJOINABLE` fallback is the honest bound on an unknown overlap: the joint evidence cannot
exceed either corpus, so the smaller one is the most that can be claimed without knowing which rows
correspond. It is derived from the two records rather than chosen, and it reproduces the exact
2,000-vs-4 case as `n = 4`.

**The measured expectation:** the MAJOR-6 reproduction drops from `P = 1.0000` to a posterior wide
enough to be refused at any admission confidence a policy would state.

**What this must not do** — and it is the reason this spec exists rather than a one-line change —
is re-create CRITICAL-A. Shrinking `n` widens every posterior, including the true positives'. The
acceptance criterion below is therefore two-sided and non-negotiable.

---

## 4 · Conditional coherence — closing MAJOR-7, correctly this time

For the trades in the level set at the proposal's stated value, both quantities are estimates of the
**same** thing — `E[gross P&L | the bot stated this]`:

- **modelled**: `p_win·W − p_loss·L`, already computed, scaled onto the proposal's size;
- **realised**: the mean gross P&L of the joined trades in that level set, scaled by the same
  factor, with a Bayesian-bootstrap credible interval from the existing `bayesian_bootstrap_mean`.

The check fires only when the modelled mean sits **above the upper credible bound** of the realised
conditional mean — the model claiming more than the bot's own trades at that stated value have ever
delivered. Below the bound, and anywhere inside the interval, it is silent. It is deliberately
one-sided: a model that under-claims is not a danger this gate exists to stop, and refusing it would
be the resolution penalty all over again.

Three properties it must have, each of which the deleted version failed:

1. **Like with like.** Both sides conditional on the same level set. A discriminating forecaster's
   high-stated-value bin genuinely does average more than its record, and this check must not read
   that as a contradiction.
2. **Powered by evidence in the right direction.** It fires more readily as the level set grows,
   because a tight realised interval makes disagreement meaningful. It reports `UNAVAILABLE` when
   the level set holds fewer joined outcomes than `OUTCOMES_NEEDED_FOR_A_POSTERIOR`, rather than
   guessing.
3. **Not scale-invariant.** The realised side is scaled by the same `_size_scale` factor as the
   modelled side, so it cannot be defeated by proposing a larger position — the defect that made the
   deleted version structurally unable to catch MAJOR-C.

A fired check yields `QualityVerdict.REFUSE` with the two numbers and the level-set size named in
the reason, not `UNASSESSABLE`: the evidence exists and it disagrees, which is a judgement rather
than an absence.

---

## 5 · Signatures

```python
# stated_probability_calibrator.py
@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    ...
    trade_reference: str | None = None

class StatedProbabilityCalibrator:
    def level_set_references_at(
        self, stated: float, *, bot_identity: str, trading_segment: TradingSegment
    ) -> tuple[str, ...]:
        """The trade references of the forecasts PAVA pooled into the value predicted at `stated`.

        Empty when the bot has no own fit, or when its forecasts carry no references.
        """

# realized_payoff_distribution_estimator.py
@dataclass(frozen=True, slots=True)
class RealisedTradeOutcome:
    ...
    trade_reference: str | None = None

class RealisedPayoffDistributionEstimator:
    def trade_references_for(self, bot_identity: str) -> frozenset[str]: ...
    def gross_outcomes_by_reference_for(
        self, bot_identity: str, references: Collection[str]
    ) -> tuple[Decimal, ...]: ...

# conditional_expectancy_coherence.py  (new)
class CorpusJoin(StrEnum):
    JOINED = "joined"
    UNJOINABLE = "unjoinable"
    DISJOINT = "disjoint"

class CoherenceOutcome(StrEnum):
    CONSISTENT = "consistent"
    MODEL_EXCEEDS_RECORD = "model-exceeds-record"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class ConditionalCoherence:
    outcome: CoherenceOutcome
    join: CorpusJoin
    modelled_rupees: Decimal | None
    realised_mean_rupees: Decimal | None
    realised_upper_rupees: Decimal | None
    level_set_outcomes: int

def corroborated_support(...) -> int: ...
def assess_conditional_coherence(...) -> ConditionalCoherence: ...
```

`ConditionalCoherence` is carried on `TradeQualityEvidenceCard` and stored, so a refusal on this
ground is legible afterwards and a `CONSISTENT` card records that the cross-check ran and passed.

---

## 6 · Acceptance criteria — two-sided, because one-sided is how both previous sign-offs went wrong

`docs/research/261`'s closing lesson is that a one-sided criterion is satisfied by a degenerate
answer, and both earlier sign-offs were one-sided in opposite directions. These are stated together
and measured together:

1. **The MAJOR-6 reproduction is refused.** A 2,000-forecast calibrator against a 4-trade payoff
   record no longer admits at P = 1.0000.
2. **Sensitivity does not fall.** A ground-truth sweep over bots with a genuine edge admits them at
   a rate no worse than the re-reviewer's measured **0.879** on the same construction.
3. **Specificity does not fall.** Zero-skill loss-making bots stay refused at no worse than the
   measured **0.983**.
4. **The resolution test.** Truth held constant, discrimination swept from 0.0 to 1.6: the refusal
   rate attributable to the coherence check must **not** rise with discrimination. This is the exact
   sweep that killed the deleted version (5% → 82%) and it is now a regression test.
5. **A null gate fails the suite.** Patching `clears = False` must fail more than one test — the
   condition that let a null gate pass 38 of 39.
6. **`R.05` on the real record**: `opening_range_breakout_v1` REFUSE, `credit_spread_v1` ADMIT, both
   on their merits, with the coherence check reported on both cards.

## 7 · Then, and only then

The mutation battery is re-run whole (it is invalid since round 2, `O.125`), **M04 first**, against
a frozen tree; the third adversarial review runs in a fresh subagent against that same frozen tree;
and `QUALITY_FLOOR_HELD_OFF_PENDING_REVIEW_REPAIRS` is flipped only after both come back. Editing
the tree during a review is what cost the battery last time and it is not repeated.

---

## 8 · Sourcing search — run 2026-08-18, mechanical evidence only (`R.16` / `R.17`)

Decomposed into three parts before searching, per `building-features-from-ideas`: **(a)** obtain the
level set of an isotonic fit and the observations in it; **(b)** an interval on a per-prediction
calibrated probability; **(c)** join two independently-fitted corpora and cross-check a conditional
model estimate against a conditional realised mean.

### (a) Level set of an isotonic fit — `scikit-learn`, ALREADY VENDORED, does NOT expose it

`sklearn.isotonic.IsotonicRegression` is already a dependency and already used at
`stated_probability_calibrator.py:48`. Probed mechanically on the installed build
(**sklearn 1.9.0**, numpy 2.4.6, scipy 1.18.0):

```
public fitted attributes: ['X_max_', 'X_min_', 'X_thresholds_', 'f_', 'increasing_', 'y_thresholds_']
X_thresholds_ [0.1 0.2 0.3 0.4 0.9]   y_thresholds_ [0. 0.5 0.5 1. 1.]
hasattr(ir, 'counts_') -> False
```

The fitted step function is exposed; **the PAVA block membership and its counts are not.** There is
no attribute carrying which observations were pooled into which level, so `level_set_references_at`
has to derive membership by grouping on the predicted value. That is a fact about the installed API,
not a preference — recorded because it is the reason this project writes the function itself
instead of calling one.

### (b) Per-prediction probability interval — `venn-abers` 1.5.4, INSTALLED AND RAN, rejected for
### THIS slice and catalogued for a later one

Installed into the project venv and exercised on real input rather than read about:

```
venn_abers 1.5.4 · exports VennAbers, VennAbersCV, VennAbersCalibrator, VennAbersMultiClass,
                   VennAbersRegressor, calc_p0p1, calc_probs
VennAbers().fit(cal, y); predict_proba(test) -> p [[0.333 0.667]]  p0p1 [[0.5 1.0]]
```

It installs, its signatures are real, and it returns exactly the object it claims: a
**[p0, p1] probability interval per prediction**, which is a principled alternative to this engine's
binned-isotonic-plus-Beta-concentration construction for expressing how much evidence supports one
calibrated value. **Rejected for this slice on two mechanical grounds**, not on prose: it addresses
the CRITICAL-2/MAJOR-B *evidence-count* axis, which rounds 2–3 already repaired and the re-reviewer
independently re-measured as fixed; and it does nothing at all for the join, which is what MAJOR-6
and MAJOR-7 actually are. Swapping the calibrator's internals while closing a different finding is
the mid-review tree edit that `O.125` was recorded for. It goes into the plan as its own entry
rather than being silently dropped.

### (c) The join and the conditional cross-check — NO PRIOR ART FOUND, written here

Searched for a package that cross-checks a forecast calibration against a realised P&L record at the
level of one prediction. Nothing matching returned: results were P&L calculators
(`danielhorizon/trading-pnl`, `sadhbh-c0d3/crypto-pnl`, `CoinAlpha/pnl-analysis`), trading
simulators, and broker API wrappers (`algobulls/pyalgotrading`) — none of which calibrates anything,
let alone cross-checks two corpora. The nearest genuine analogs are conceptual rather than
installable: Murphy's Brier decomposition (already implemented here) and prequential scoring
(`L2.22`). **Built in this repository**, using `bayesian_bootstrap_mean` which already exists in
`realized_payoff_distribution_estimator.py`, so the new module adds the join and the comparison and
no new statistics.

**Sources consulted:**
[scikit-learn IsotonicRegression](https://scikit-learn.org/stable/modules/generated/sklearn.isotonic.IsotonicRegression.html) ·
[scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html) ·
[Generalized Venn and Venn-Abers Calibration (arXiv 2502.05676)](https://arxiv.org/pdf/2502.05676) ·
[pyalgotrading](https://github.com/algobulls/pyalgotrading) ·
[trading-pnl](https://github.com/danielhorizon/trading-pnl)
