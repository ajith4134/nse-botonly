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
