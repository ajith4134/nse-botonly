# 252 · Adversarial review of the point-in-time bar reader — the anti-look-ahead reader leaked the future

**Run 2026-08-17 in a fresh subagent, `R.23c` step 5.** The claim under review: *"look-ahead is
structurally impossible for five-minute bars, and the guard test stops any new consumer reading the
table unfiltered."*

**Both halves were false, and both were broken with running code against live data.**

---

## CRITICAL · The cutoff was compared as a STRING, so the same instant leaked the future

`availability_time` is a TEXT column, so `availability_time <= ?` is a **string** comparison, and
lexical order over mixed-offset ISO-8601 is not chronological order. The reader passed
`as_of.isoformat()` straight through — the caller's own offset spelling. Measured on the live store,
one physical instant, five spellings:

| `as_of` spelling | bars visible | latest available bar |
|---|---|---|
| `+05:30` IST | 2,538 | 2026-08-14 12:00 (correct) |
| `+05:45` Nepal | 2,541 | 12:15 |
| `+09:00` Tokyo | **2,577** | **15:15 — 39 future bars, 3h15m ahead** |
| `+00:00` UTC | 2,505 | previous day's close — **the whole session hidden** |

```
close at 12:00 IST truth: 1165.3
close leaked via Tokyo  : 1169.2 at 2026-08-14 15:10:00+05:30
```

**The UTC row is the one that would actually have happened** — `datetime.now(UTC)` appears 26 times
elsewhere in `src/`. A sweep of every offset from −12:00 to +14:00 found **499 (offset, cutoff)
combinations** violating `bar.availability_time <= as_of`.

Two things made it invisible. `_require_aware` blessed *any* aware datetime and then **discarded its
own return value**, so the one place a normalisation could have lived was a no-op. And the property
test built its cutoffs only in IST, so it could never have found this.

**The sibling store already had the answer.** `BitemporalBarStore` normalises
(`instant = moment.astimezone(UTC)`), and its schema comment reads *"Always UTC, always fixed width,
so SQL TEXT order is chronological order. Never store a caller-supplied offset spelling here."* My
docstring claimed to be "the same move as `BitemporalBarStore.bars_as_of`". **It copied the API shape
and not the mechanism** — which is a sharper description of the failure than I would have written
myself.

**Fixed** — `_cutoff_text` refuses a naive instant *and* converts into `STORE_TIMEZONE` before the
comparison. Re-verified on live data: IST, UTC, Tokyo, Kathmandu and Honolulu now all return **2,538
bars / 3,712 instruments**, no bar past the true instant.

## MAJOR · The guard test was defeated four ways, all green

Four modules were created under `src/`, run against the guard, then deleted:

| bypass | mechanism | rows read unfiltered | guard |
|---|---|---|---|
| A | `"price_" + "bars"` runtime concatenation | 1,246,985 | **passed** |
| B | the words `availability_time <=` in a normal (non-docstring) string | 1,246,985 | **passed** |
| C | adjacent literals `"…FROM price_" "bars WHERE…"` via `pandas.read_sql` | 1,246,985 | **passed** |
| D | comma join `FROM instrument_master m, price_bars b` | 4,950,811 | **passed** |

**C is the dangerous one: a line-wrapping formatter produces it automatically.** B meant any file
mentioning the phrase was cleared wholesale — a guard defeated by a comment describing the thing it
guards against.

**Fixed** — the guard now works **literal by literal** off the AST rather than file-wide, matches the
bare table token rather than a `FROM`/`JOIN` prefix, and excludes writes. B and C die because
adjacent literals fold at parse time and a filter in a different query no longer vouches for this
one; D dies with the bare-token match. **A survives by design**: catching a name assembled at runtime
needs dataflow analysis, and a guard exists to stop the accident, not to defeat someone deliberately
hiding from it. That limit is written into the code.

The sharper guard immediately surfaced two real unfiltered reads the file-level version had hidden —
the reader's own NULL-detection query (which must be unfiltered) and `sizing_inputs_from_real_stores`'
`EXISTS` universe subquery (real debt). Both now carry exemptions with reasons.

## MAJOR · The rename migration was a TOCTOU race — 5 of 6 concurrent opens crashed

Six barrier-synchronised processes against real database copies, five trials, identical result:

```
trial 0: 6 concurrent opens -> 5 failures ['OperationalError: no such table: price_bar', ...]
```

Data survived; the constructor died. It fires exactly once per database — on the first open after
deploying, which is precisely when the dashboard, the ingest and the paper loop all restart together.
**Fixed** with `BEGIN IMMEDIATE` around the look-then-rename.

## MAJOR · With both tables present, the migration silently abandoned the legacy rows

A 7-row `price_bar` beside the live table produced **no error, no log, no merge** — the rows became
permanently unreadable and the banned name stayed in the schema forever. The docstring called
re-running "harmless"; it was harmless only when the legacy table was empty, which was never checked.
**Fixed** — both names present is now a refusal.

## MAJOR · `instrument_tokens` did not narrow the read

The docstring said "narrows the read"; the filter ran in a Python loop after materialising the whole
table. Asking for **one** instrument cost **1.913s** — 71% of asking for all 3,712 — and pulled all
1,246,985 rows through Python. **Fixed** with an `IN (…)` clause: **0.145s**, a 13× improvement.

## MINOR, all fixed

| # | Finding |
|---|---|
| 6 | The migration filtered `type='table'`, so a legacy **VIEW** was invisible: the store created a fresh empty table beside 203 live rows and wrote into the empty one. Filter dropped. |
| 7 | `ALTER TABLE … RENAME TO` renames the table, **not its indexes** — the live database carried `price_bar_by_availability` and `price_bar_by_token` alongside their new-name duplicates. `R.14` satisfied in Python, violated in the schema, with two identical indexes rebuilt on every insert. Dropped, and **applied to the live database**. |
| 8 | 5 of 17 mutants survived. The naive-`as_of` refusal was tested on only one of three methods; nothing pinned the guard's directory list. Both now covered. |
| 9 | `with sqlite3.connect(...)` scopes a **transaction**, not the handle: 100 calls left 135 descriptors open until cyclic GC. Churn rather than a leak — the reviewer could not exhaust it — now `closing()`. |
| 10 | `_as_int` passed bools through (`isinstance(True, int)`) and silently truncated `1234.7 → 1234` while its docstring promised failures were surfaced. Both refused now. Not reachable on live data, where every `volume` is an integer. |

**Operational note that was not a code defect but would have cost a day:** the rename lived only in
an un-checkpointed WAL. A file-level `cp` of the `.sqlite3` alone restored the **pre-migration**
schema. The live database has now been checkpointed (`PRAGMA wal_checkpoint(TRUNCATE)`, WAL → 0
bytes).

## Could not break — stated explicitly

`most_recent` on live data across `{1, 2, 3, 20, 2537, 2538, 2539, 1_000_000}` — no off-by-one,
correct tail, chronological, and asking for more than exists returns everything. Microsecond
cutoffs — `'.'` sorts above `'+'`, which is the safe direction. `Decimal(str(...))` — not lossy;
`str(float)` is shortest-round-trip, and 0 of 200,000 sampled closes need more than two decimals.
`'Z'`-suffix cutoffs — unreachable, since `isoformat()` never emits one. **Every factual claim in the
module docstring verified**: 1,246,985 rows, 0 with `availability_time <= bar_timestamp`, 0 NULLs,
exactly 125,980 hidden at the stated instant, 0 rows where availability ≠ timestamp + 5m.

## The lesson

I asked the reviewer to test the string-comparison hypothesis because I suspected it — and I had
still shipped the bug, written a docstring claiming the sibling store's guarantee, and built a
property test whose generator could not reach it. **Suspecting a failure mode is not the same as
testing it**, and the review is worth its cost precisely at that gap. This is the second review in
one day where the finding was not "the algorithm is wrong" but "the thing you claimed to have done
is not the thing you did" — the same shape as `O.115`.
