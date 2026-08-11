# 203 — Bitemporal bar store

**Tasks:** todo `1.3` and `1.4` — one slice. Availability-time is a property *of* the bar store, not a
separate feature. **Plan:** `L0.03`, `L0.04`.

**R.23(b) classification:** an **ingest and store**, not an engine. No solver. Decision-path, so it takes
the full loop: every indicator, signal and backtest reads from it.

---

## 1. The one property that matters

**A backtest must only see what was knowable at the time.** Three timestamps, not one:

| Time | Meaning |
|---|---|
| **event time** (`bar_timestamp`) | when the bar's interval began, in the market's clock |
| **availability time** | when this system could first have *acted* on it |
| ingestion time | when the row was written — audit only, never a decision input |

Every read is filtered on **availability time**, not event time. A 09:15 five-minute bar does not exist as
a tradeable fact until 09:20; a bar backfilled three days late was never actionable on the day it
describes. Filtering on event time is the single most common way a backtest lies about itself, and it
lies *optimistically*.

## 2. Measured state of the retained data (2026-08-10)

The reset preserved 659,990 bars. Measured before designing:

| Check | Result |
|---|---|
| Rows | 659,990, all `5m`, 2,440 instruments, 2026-06-22 → 2026-08-05 |
| `availability_time` null | **0** |
| `availability_time < bar_timestamp` | **0 violations** |
| Timestamps | timezone-aware, `+05:30` |
| Price storage | **`real` (float)** |
| Primary key | `(instrument_token, bar_interval, bar_timestamp)` |

**The bitemporal work was sound and is carried forward.** Two schema defects are not:

**① Keyed on `instrument_token`.** That is the transient handle Kite **reuses** once a contract expires
(`L0.02`, `A.34`). A bar store keyed on it silently merges an expired contract's history into a new
instrument — the exact corruption the instrument master exists to prevent, one layer down. The retained
window happens to be clean (one token spans >40 days, and it is a cash equity where that is legitimate),
but the schema permits the corruption.

**② Prices stored as `real`.** Binary floats cannot represent a tick exactly, and the error compounds
across the millions of arithmetic operations an indicator pipeline performs.

## 3. Design

- **Identity** — `(exchange, segment, tradingsymbol)` from the instrument master, plus `bar_interval` and
  `bar_timestamp`. `instrument_token` is stored for subscription lookup, never as identity.
- **Prices** — `Decimal`, stored as text. Exactness beats the storage cost; a bar is written once and
  read many times, and the read parse is cheap next to what the reader then does with it.
- **Availability time** — mandatory, and **enforced `>= bar_timestamp`** at write. A row that claims to
  have been actionable before it existed is rejected, not stored.
- **Timezone** — every timestamp is offset-aware. A naive datetime in this store is a defect (the DTZ
  ruff rules are enabled for exactly this).
- **Reads are availability-filtered by default.** The API makes the safe read the easy one: `bars_as_of`
  takes the moment the caller is pretending to be at, and never returns a bar that was not yet available.

## 4. Acceptance criteria

1. Real bars ingest and round-trip losslessly with exact prices.
2. A bar whose availability precedes its event time is rejected.
3. `bars_as_of` never returns a bar with `availability_time > as_of` — property-tested.
4. A naive (offset-free) timestamp is rejected.
5. Identity is the stable triple; two contracts sharing a reused token stay separate.
6. Re-ingesting the same bar is idempotent and does not duplicate.

## 5. Out of scope

No gap detection or backfill (`L0.10`), no corporate-action adjustment (`L0.07`), no multi-broker sourcing
(`L0.14`). Those write *through* this store; they are not part of it.

## 6. Migration note (R.11)

The 659,990 retained bars are keyed on token alone. Migrating them requires joining to the instrument
master by token, which is only safe because the window is six weeks and no reuse is detectable in it.
**Logged as an open item:** the migration must record which bars could not be resolved to a stable
identity rather than dropping or guessing them.

## 7. Sourcing record (R.16 / R.17 — mechanical, run 2026-08-10)

| Candidate | Verdict | Mechanical evidence |
|---|---|---|
| **`arcticdb`** (Man Group) | **REJECT — tier-1, unavailable** | The strongest candidate on paper: purpose-built for financial time series with genuine bitemporal support, which is exactly this spec. `pip index versions arcticdb` → **"No matching distribution found"**; install fails the same way; import fails. **No aarch64 wheel and no source build path on this box.** Rejected on availability, not on merit — if this project ever moves to x86 it should be re-evaluated first. |
| **SQLite** (stdlib) | **ADOPT** | Already the store for the instrument master and the retained 659,990 bars. Zero dependency, WAL for concurrent reads, and the retained data proves the shape works at this scale. Bitemporality is a schema-and-query property, not something a library must provide. |
| `duckdb` 1.5.5 | **HOLD** — already installed | Better for large columnar scans than row-wise SQLite, and a genuine candidate once bar volume grows past a single machine's comfort. Not adopted now: it would split the storage engine across two systems while the instrument master is in SQLite, for no measured benefit at 660k rows. Revisit when scans become the bottleneck rather than assuming they will. |
| `polars` 1.43.2 | **ADOPT for reads later** | Installed and verified. The natural frame for cross-sectional scans over the focus set (`L5.21c`), reading *from* this store. Not the store itself. |
| InfluxDB / QuestDB | **REJECT** | Tier-1 scope: both are servers requiring a running daemon, which adds an operational failure mode to a box already running the trading loop. The measured data volume (660k bars over six weeks) does not justify it. |

**Built rather than borrowed:** the availability-time enforcement and the availability-filtered read API.
No library expresses "a bar does not exist until the moment it could have been acted on" — that is the
project's own correctness property, and the one thing this store exists to guarantee.

---

## 8. Corrections after adversarial review (2026-08-10)

A fresh-context reviewer — **on sonnet, the first test of R.26's standing obligation** — found defects in
code that had already passed ruff, mypy and 114 tests. **The headline one broke the single guarantee this
module exists for.**

**① The availability filter leaked, by string comparison alone.** Times were stored as
`datetime.isoformat()` with whatever offset the caller supplied, and compared as SQL `TEXT`. TEXT order is
not chronological order across differing offsets. Reproduced:

```
stored available_from : 2026-08-10T09:20:00+05:30
asked as_of           : 2026-08-10T09:30:00+05:45   (= 09:15 IST — FIVE MINUTES EARLIER)
TEXT comparison       : as_of >= available  ->  True    LEAK
real comparison       : as_of >= available  ->  False
```

A backtest asking in any offset other than the one the data happened to be written in **saw bars that did
not yet exist** — the optimistic direction, which is the one that costs money. `ORDER BY bar_timestamp`
failed the same way, returning a later bar first. The retained 659,990 rows are uniformly `+05:30`, so the
bug was **latent, not active** — but any future UTC-normalising backfill, or a host not pinned to IST,
would have activated it silently.

**Fix:** every comparison column is a **UTC-normalised, fixed-width** key (`bar_timestamp_utc`,
`available_from_utc`); the caller's original offset is stored beside it as seconds and restored on read, so
round-trip fidelity is unchanged. The primary key moved to the UTC key, which also fixes a second defect —
the same instant written `09:15+05:30` and `03:45+00:00` previously stored as **two bars**, breaking
acceptance criterion 6 for any second ingest path.

**② The test suite was structurally blind to it.** Every timestamp in the suite descended from one IST
constant, including the property test that existed to state the invariant. It could not have failed. The
property test now sweeps the write *and* read offsets across the full UTC range independently.

**③ Validation holes.** Accepted before, refused now: negative and zero prices; an open or close outside
the bar's own `[low, high]` range (the high/low check's own rationale, applied where it was missing); a
float or bool `volume` — which **wrote cleanly and then crashed the reader** with a raw `ValueError`; a
zero or negative `instrument_token`; a negative `open_interest`; empty identity fields. A malformed stored
row now surfaces as `BarStoreError` rather than escaping the documented contract — the record construction
was outside the `try`.

**Mutation testing: 19 mutants, 19 killed.** Three survived the first pass and each exposed a real gap:
the ordering test wrote bars whose storage order already matched event order, so dropping `ORDER BY`
changed nothing; the non-positive-price cases were all killed by the *range* check instead, leaving the
sign check unverified; and nothing asserted the read side refuses a naive `as_of`. A **regression mutant**
that restores the original offset-preserving key is now part of the set and is killed.

**R.05 real-data pass, full universe not a sample:** all **659,990** retained bars constructed under the
stricter validation with **zero rejects**, written in 20.3s, read back in 25.3s, 659,990 distinct storage
keys, prices exact, ordering correct, no leak. A point-in-time cut at `2026-06-29T11:45+05:30` returns 330
bars, none available after that moment.

## 9. Rule G / R.06 — the queued consumer

No production consumer yet; only tests import it. The named, queued consumer is the **indicator/feature
pipeline** (`L0.15` onward) and the **backtest reader** (`L10.x`), both of which read exclusively through
`bars_as_of`. **Until one lands, tasks 1.3/1.4 are `[~]`, not done, in the R.11 sense.** Also queued: the
migration of the retained 659,990 token-keyed bars to stable identities (§6), which must *record* what it
cannot resolve rather than drop or guess it.
