# 245 · `L2.01` + `L2.14` — the honest trial registry, and why every tracker on PyPI is the wrong shape

**Spec, 2026-08-16.** First engine of feature `F06` ("a candidate cannot be believed until it
survives the search size that produced it"), which `docs/FEATURE_MAP.md` places immediately after
`F04` — *a judge with no trials is the exact failure this map prevents*.

Plan entries:

> **`L2.01`** Honest trial registry — cumulative N including every discarded and abandoned run, so
> the search size is known rather than flattered. ⟨XIII⟩ · base · archived · r/166
>
> **`L2.14`** Strategy trial registry store — 20 trials recorded. ⟨XIII⟩ · base · archived · r/166

## Idea intake (rule gate)

**ALREADY EXISTS — `L2.01` and `L2.14`.** Both are in the plan, both `[ ]` in
`docs/ajith_final_todo.md`, both tagged `archived` (a design existed pre-reset and was deleted with
the code on 2026-08-10). Nothing is inserted; this builds what is already catalogued.

---

## 1. Why this is the first engine of `F06`, and not an arbitrary starting point

Every downstream gate in `F06` is a function **of the number of trials**:

| entry | statistic | what it needs from here |
|---|---|---|
| `L2.03` | Deflated Sharpe Ratio | the number of independent trials that produced the candidate |
| `L2.06` | PBO via CSCV | the set of trials to resample over |
| `L2.07` | Benjamini-Yekutieli FDR | the count of hypotheses tested |
| `L2.08` | effective trials `N̂ = ρ̂ + (1−ρ̂)·M` | `M`, the raw trial count, and their correlation |
| `L2.11` | Harvey-Liu-Zhu hurdle | the multiple-testing burden |

**If the count is flattered, every gate above it is flattered, and each one is flattered in the
direction that lets a bad strategy through.** Deflated Sharpe with an undercounted `N` reports a
candidate as significant when it is not; FDR with an undercounted hypothesis count under-corrects.
The registry is not bookkeeping — it is the input that decides whether the whole apparatus is
honest or decorative. That is `F06`'s own thesis applied to `F06`.

## 2. The requirement, stated precisely

**Cumulative `N` must include every trial that was run, including the ones nobody wants to count:**
runs abandoned half-way, runs that raised, runs whose result was discarded because it looked wrong,
and runs from a search that was itself abandoned. A trial that is deleted, hidden, or never written
is a trial that flatters the search size.

Three properties follow, and the third is the one no existing tool provides:

1. **Append-only** — no API removes or edits a recorded trial.
2. **Every terminal state counted** — completed, abandoned, errored and discarded are four
   outcomes of one population, not "results" plus "noise".
3. **Tamper-evident** — a reader can VERIFY that nothing was removed, rather than trusting that
   nothing was. Without this, honesty is a promise by the author to their future self, and the
   future self is exactly the party with an incentive to drop a trial.

---

## 3. Sourcing pass (`R.16`/`R.17`) — run 2026-08-16, MECHANICAL evidence only

Experiment tracking is a crowded field, so every candidate was installed and probed against ONE
decisive question: **can a recorded trial disappear from the count?**

| candidate | mechanical test | verdict |
|---|---|---|
| **`mlflow` 3.15.1** | installed. Logged 5 runs, called `client.delete_run(...)`, re-queried: **`search_runs` returned 4.** `ViewType.ALL` recovers 5, and `mlflow gc` destroys them permanently. | **REJECTED — its DEFAULT read is the failure mode.** The count a caller gets excludes deleted runs unless it knows to ask otherwise, which is precisely "the search size is flattered". Not a criticism of mlflow: hiding deleted runs is correct for its job and wrong for this one. |
| **`optuna` 4.9.0** | installed. Ran a 40-trial study with deliberate prunes and exceptions: `Counter({'COMPLETE': 15, 'FAIL': 13, 'PRUNED': 12})`, `len(study.trials) == 40`. **No public delete-a-trial API**; `RDBStorage` exposes only `delete_study`. | **CONCEPT ADOPTED, not vendored.** Its accounting is exactly right — every terminal state counted, no per-trial deletion — and it CONFIRMS the design. But it is a sampler-coupled optimizer: adopting it would force every trial through an optuna study, and this registry must accept trials from replays, champion-challenger runs and manual investigations that are not a study. `delete_study` also destroys everything at once, so it is not tamper-evident. |
| **`aim`** | installed. `hasattr(aim.Repo, "delete_run")` → **`True`**. | **REJECTED** — per-run deletion in the public API. |
| **`sacred` 0.8.7** | installed. Storage is per-observer (`FileStorageObserver`, `MongoObserver`); deletion is a filesystem or Mongo operation with nothing in the library that would notice. | **REJECTED** — no ownership of its own record, so no property can be enforced over it. |
| `wandb`, `neptune` | hosted SaaS. Not probed further: this project's data does not leave the box, and a registry whose record lives on someone else's server cannot be the arbiter of what this system may trade. | **REJECTED on architecture**, stated rather than measured. |

**Nothing found is tamper-evident, because no tracker is trying to be.** They exist to help an
author find their best run; this exists to stop an author from under-reporting how many runs there
were. Those are opposite incentives, and the tools are correctly built for the other one.

**Therefore: build, with optuna's state model adopted and a hash chain added.**

### The SOTA analog for the part that is genuinely built (`R.23(a)`)

Not an experiment tracker — a **hash-chained append-only log**, as in git's commit graph and
Certificate Transparency. Each trial record carries the digest of the previous record, so removing
or editing any trial breaks every digest after it and the break is *locatable*. Verification is a
single pass. This is a real mechanism with real failure modes to test, not vocabulary.

---

## 4. What gets built

**`src/nse_algo_trader/validation/honest_trial_registry.py`**

- `TrialOutcome` — `COMPLETED` / `ABANDONED` / `ERRORED` / `DISCARDED`. Four terminal states of one
  population. `DISCARDED` exists precisely because "I did not like this result" is the trial most
  likely to go unrecorded, and naming it is what makes it recordable.
- `TrialRecord` — frozen: monotonic sequence number, the search it belongs to, what was tried
  (a parameterisation digest), the outcome, the fitness if there was one, when it ran, and
  `previous_digest` / `digest`.
- `HonestTrialRegistry` — `record(...)` appends; `cumulative_trials(...)` counts; `verify_chain()`
  returns the first broken link or `None`. **No update, no delete.**
- `TrialRegistryError` — a chain that does not verify, or an attempt to write a trial that
  contradicts a recorded one.

**Storage (`L2.14`)** — SQLite at `~/.nse_algo_trader/trial_registry.sqlite3`, one table, a
`PRIMARY KEY` on the sequence number, and **no `UPDATE` or `DELETE` statement anywhere in the
module**. The chain is what defends against edits made outside it.

### Error behaviour, stated before the code

- recording a trial whose `previous_digest` does not match the tail → `TrialRegistryError`
- `verify_chain()` on a store with a removed row → returns the sequence number where the chain
  breaks, never `None`, never an exception
- reading an absent store → zero trials, and a coverage reader that can tell "no trials recorded"
  from "no store", the `A.41` distinction this codebase keeps relearning
- a fitness on an `ERRORED` or `ABANDONED` trial → `TrialRegistryError`; those states have no score

## 5. Test plan (`R.23(c)`, before implementation)

**Unit** — the four outcomes all count toward `cumulative_trials`; sequence numbers are gapless;
digests are stable across processes.
**Property** — for any sequence of appends, `verify_chain()` returns `None`; the digest of record
`n` depends on every record before it (change any earlier field, every later digest changes).
**Adversarial** — delete a row directly in SQLite and assert `verify_chain()` LOCATES it; edit a
fitness in place and assert the same; append a record with a stale `previous_digest` and assert the
refusal; assert the module contains no `UPDATE`/`DELETE` against the trial table (a source-level
test, because the guarantee is structural rather than behavioural).
**`R.05`** — replay the **3,481 retained closed trades** in `experience_memory.sqlite3` through the
registry and report cumulative `N`, then verify the chain over the whole real population.

## 6. What this deliberately does NOT do

- **Decide anything.** It counts. `L2.03`, `L2.06`, `L2.07` and `L2.08` are the gates; a registry
  that also judged would make the count a function of the verdict, which is the failure inverted.
- **Deduplicate trials.** Two identical parameterisations tried twice ARE two trials — that is what
  multiple-testing correction is correcting for. Collapsing them would flatter `N` while looking
  like tidiness.
- **Backfill history it cannot know.** Trials run before this registry existed are unrecorded and
  unrecoverable, exactly as `L0.37`'s `NULL` basis is. The count starts honest from here and says
  so, rather than inventing a prior.
