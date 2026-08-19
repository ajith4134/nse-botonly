# 251 · `B12` — making look-ahead structurally impossible for five-minute bars

**Idea-intake verdict: ALREADY EXISTS as `L0.03` (historical bar store) and `L0.04` (bitemporal
availability-time).** This is those entries' implementation being completed, not a new entry. The
discovery and both corrections are `A.132`; the judgement is `O.114`/`O.115`.

## 1. What is actually true, measured

Every number below came from querying `~/.nse_algo_trader/market_data.sqlite3` directly, and the
falsifying query was run before the conclusion (`O.115`'s rule).

| | `price_bars` | `price_bar` |
|---|---|---|
| Interval | **`5m` only** | **`day` only** |
| Rows | **1,246,985** | 203 |
| Instruments | 3,787 | 203 |
| Range | 2026-06-22 → 2026-08-14 | 2026-08-10 → 2026-08-13 |
| Written by | `backfill_five_minute_bars.py` (sole writer), scheduled at `run_daily_operations.py:1370` | `BitemporalBarStore.write`, from the bar-reconciliation step |
| Read by | the decision path | nothing that decides |

**They are disjoint by construction** — a join on `(instrument_token, bar_interval, bar_timestamp)`
returns **0 rows**, and no triple can ever match because the intervals do not overlap. There is
nothing to unify. My earlier claim that these were parallel stores of the same data was wrong and is
corrected in place in `BACKLOG.md` and `O.115`.

**The availability convention is sound.** `availability_time = bar_timestamp + interval` holds
universally: **0 rows** have `availability_time <= bar_timestamp`, so no row claims to have been
knowable before it existed. Sample: a `09:15` bar becomes available at `09:20`.

**The filter is load-bearing, not decorative.** As of `2026-08-14T12:00+05:30`, `1,121,005` of
`1,246,985` rows are visible — the filter hides **125,980 rows**. A consumer that forgets it reads
the afternoon while deciding at noon.

## 2. The actual defect

Three production consumers filter on `availability_time`; four read paths do not:

| Reader | Filters? | Verdict |
|---|---|---|
| `paper_session_signal_source.py:123` | **yes** | decision path, safe |
| `sizing_inputs_from_real_stores.py:197` | **yes** | decision path, safe |
| `verify_segment_bot_protocol_on_real_data.py:242` | **yes** | probe, safe |
| `bar_tape_join_verification_engine.py:1777` | no | **legitimate** — compares a whole recorded session against a whole tape; it is not deciding |
| `bar_price_basis_provenance.py:212,236` | no | **legitimate** — asks what a stored value MEANS, across all of history |
| `regime_brain_read_model.py:118,126` | no | **dashboard-only**, correct for a now-view; a trap if ever reused for a historical one |
| `sizing_inputs_from_real_stores.py:92`, `verify_paper_session_on_real_data.py:119` | existence-only `EXISTS` | tests "has any bar ever", not "was one knowable" — safe today, wrong shape |

So nothing is broken right now. **The defect is that safety is a property of each author's memory
rather than of the code**, and there is a second hole: `availability_time` is **nullable at the
schema level**. Today there are zero NULLs, but a NULL silently fails `availability_time <= ?`
(SQL three-valued logic), so a future writer inserting one would make bars vanish rather than leak —
safe in direction, silent in effect, and undetectable without this note.

## 3. What gets built

**`PointInTimeFiveMinuteBarReader`** — the only sanctioned way to read `price_bars`. Its entire
design is one idea: **there is no method that does not take `as_of`.** A caller cannot forget the
filter because there is no overload without it.

- `bars_for_instrument(instrument_token, as_of, *, most_recent=None)` — OHLCV rows, oldest first.
- `closes_for_instruments(as_of, instrument_tokens=None)` — token → closes, for cross-sectional work.
- `instruments_with_bars(as_of)` — the set that had any knowable bar, replacing the `EXISTS`
  subqueries that currently ask the wrong question.
- Refuses a naive `as_of` (a naive instant silently reads as UTC and shifts every cutoff by 5½ hours).
- Refuses a NULL `availability_time` encountered in a row rather than dropping it silently.

**A build-time guard**, in the family that already works here (`R.02`, `R.03`, `A.131`): a check that
no module outside the reader issues SQL against `price_bars`, with an explicit allowlist for the four
readers whose unfiltered access is legitimate — each entry carrying its reason, printed on every run
(`R.11`), exactly as `ACCEPTED_DRIFTS` does.

**The `R.14` rename, contained.** `price_bar` → `daily_reconciled_bar`. One character between two
different datasets cost two wrong findings in a day. The rename touches only
`bitemporal_bar_store.py` and its tests, with a migration that renames the existing table if present.
`price_bars` is NOT renamed — 10+ call sites, and the reader's name carries the meaning instead.

**Not built, recorded instead:** repointing the three already-safe consumers onto the reader. They
are correct today, and rewriting working decision-path SQL is a bigger risk than the hazard it
removes. The guard's allowlist names them so the debt is visible.

## 4. Verification

- **Unit** — every refusal fires; `as_of` shifts the visible set.
- **Property** (Hypothesis) — for any `as_of`, no returned bar has `availability_time > as_of`.
- **Adversarial** — a module added that queries `price_bars` raw must FAIL the guard; a reader method
  that could be called without `as_of` must not exist (checked by signature inspection).
- **Real data (`R.05`)** — read the live 1,246,985-row table at a mid-session instant and assert the
  count matches the independently-computed 1,121,005, and that the same query without the filter
  returns more.

## 5. Sourcing (`R.17`)

No third-party library is sourced. The reader is ten lines of SQL over this project's own SQLite
schema with a required parameter; no package can supply "a reader that cannot be called without an
as-of" for a local table. The guard is the same bespoke-checker pattern whose alternatives were
already evaluated mechanically in `docs/research/249` — five traceability tools installed and run,
all requiring the source be rewritten into their format.
