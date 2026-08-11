# 209 · The shared NSE ingest core — one hardened engine, nine adapters

Spec for the core adopted in `A.46`, written before any of it is built (`R.23(c)` step 1).

Nine remaining `L0` items are the same engine wearing different hats: `L0.09` delisted master,
`L0.10` gap backfill, `L0.23` bhavcopy, `L0.24` MWPL, `L0.25` F&O ban list, `L0.26` bulk/block deals,
`L0.27` ATM IV, `L0.28` circuit/ASM/GSM, `L0.29` index constituents. Each is *fetch with retry →
strict parse → bitemporal store → coverage self-check → provenance manifest*. Building nine of those
by hand produces nine places for the same defect to hide; building the core once and nine thin
adapters produces one place to harden and one place to fix.

Grounded throughout in `research/207`, which established every fact below by fetching rather than
recalling.

---

## 1 · What reconnaissance forces into the design

Four findings are load-bearing, and each kills a design I would otherwise have written.

**NSE is two platforms, not one.** `nsearchives.nseindia.com` / `archives.nseindia.com` are free and
unauthenticated — a plain `curl` with a browser User-Agent succeeds, verified across dozens of
fetches. `www.nseindia.com` is **inconsistently gated**: the homepage returns 403 (Akamai), the
historical bulk-deals API returns 503 (Apache bot-block), the option-chain API returns 404, the ASM
report is a JavaScript shell with no data, and corporate-announcements returns **200 with real data
and no cookie at all**. No header combination reliably unlocked the blocked ones. So the fetcher
cannot have a single notion of "success" or a single retry policy — it must **classify** the failure,
because a 403 Akamai wall and a 503 bot page and an empty SPA shell demand three different responses
and only one of them is worth retrying.

**Some sources have no history at all.** The F&O ban list, bulk/block deals and the circuit-band
`sec_list.csv` are **rolling today-only files**. There is no archive to backfill from: history for
these accrues only from the day we start snapshotting, and never retroactively. That makes daily
snapshotting urgent in the same way the depth tape was (`A.44`) — every day not captured is
permanently absent — and it makes bitemporality **mandatory rather than tidy**, since the only
correct record is "this is what the file said when observed at time T".

**A source's schema changes under you.** The bhavcopy layout changed in July 2024: the old path stops
working between 2024-07-05 and 2024-07-08, and the new UDiFF path back-fills earlier dates with
*different column names*. A parser written against either era alone is wrong across half the archive,
so era selection is part of the adapter contract, not an afterthought.

**Three of nine sources are blocked today.** ATM IV (404), historical bulk deals (503) and ASM (SPA
shell) could not be reached. Per `R.16` these are **not** scoped down to what is reachable — each gets
an explicit blocker with the evidence, and an alternate acquisition path is the adapter's problem to
solve, not a reason to ship less.

---

## 2 · The core's modules

| Module | Role |
|---|---|
| `nse_source_fetcher.py` | HTTP with derived backoff, and **failure classification** — reachable, rate-limited, bot-blocked, schema-shell, absent. Returns evidence, never a bare exception. |
| `ingest_source_adapter.py` | The typed `Protocol` every adapter implements. The contract. |
| `bitemporal_ingest_store.py` | `(effective_date, observed_at, source, row)` with idempotent re-ingest and immutable history. |
| `ingest_provenance_manifest.py` | What was fetched, when, from which URL, with which checksum, and which parser era read it. |
| `ingest_coverage_self_check.py` | Per-source, per-year reliability — the same self-reporting shape the trading calendar already uses. |

### The adapter contract

```python
class NseIngestSourceAdapter(Protocol):
    source_name: str                                    # self-describing, R.14
    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]: ...
    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]: ...
    def effective_date_of(self, row: IngestRow) -> date: ...
    def natural_key(self, row: IngestRow) -> tuple[str, ...]: ...
```

Four methods, no state, no I/O beyond describing what to fetch. Everything hard — retry, backoff,
classification, atomicity, dedupe, provenance, coverage — lives in the core and is written once.

---

## 3 · Bitemporality, and why it is not optional

Every row carries **`effective_date`** (the date the data is *about*) and **`observed_at`** (when we
learned it). These differ constantly: a bhavcopy for 2026-08-10 fetched on 2026-08-11 has an
effective date a day before its observation, and NSE **revises** files — a corrected bhavcopy
published days later is a second observation of the same effective date, not a correction to be
overwritten.

The store therefore never updates a row. A re-ingest with identical content is a no-op keyed on
`(source, natural_key, effective_date, content_hash)`; a re-ingest with *different* content for the
same key is a **new observation**, retained alongside the old. Readers ask either "what is true about
2026-08-10" (latest observation) or "what did we know on 2026-08-11" (as-of filter) — the same
semantics the bar store (`1.4`) and the depth tape already use, so the whole `L0` layer answers
point-in-time questions the same way.

---

## 4 · The conformance suite — the risk control for the fan-out

`A.46` parallelizes adapters across agents. The device that makes that safe is a suite living in the
core that **adapter authors do not write and may not edit**. Every adapter is parametrized through it
and must pass:

1. **Idempotent re-ingest** — ingesting the same payload twice yields the same row count.
2. **Revision retention** — a changed payload for the same key adds an observation and keeps the old.
3. **Partial-write atomicity** — a failure mid-ingest leaves the store exactly as it was.
4. **Strict parse** — a truncated, empty, or wrong-schema payload raises rather than yielding rows.
5. **No naive datetimes** — every timestamp is timezone-aware.
6. **Provenance completeness** — every stored row traces to a URL, a fetch time and a checksum.
7. **Effective-date sanity** — no row is dated in the future or before the source's verified floor.
8. **Blocked-source honesty** — an adapter for an unreachable source reports a blocker, and does not
   quietly ingest zero rows and call it success.

An agent that cannot edit the test cannot negotiate with it, which makes the done-rule mechanical
rather than a matter of my judgement about someone else's work.

---

## 5 · Build order

1. Core + conformance suite, built alone (`R.18`), with the suite proven to **fail** against a
   deliberately broken reference adapter before any real adapter exists.
2. **Wave 1, two adapters** — `nse_bhavcopy` (the hardest: two schema eras, largest volume) and
   `fo_ban_list` (the simplest: rolling today-only file). Between them they exercise most of the
   contract, so a wrong contract surfaces on two adapters rather than nine.
3. **Wave 2, the remaining seven**, in parallel worktrees, serially merged with the full gate between
   each.
4. Blocked sources (`atm_implied_volatility`, historical `bulk_block_deals`, `circuit_band_asm_gsm`)
   get an acquisition investigation each, per `R.16` — never a narrowed scope.

## 6 · Verification

Unit + property (idempotence, revision retention, as-of monotonicity) + adversarial (truncated
payloads, HTML error pages served with HTTP 200, duplicate keys, clock skew, disk full mid-ingest) +
the conformance suite + an `R.05` real-data pass per adapter against its real endpoint. The core's own
`R.05` pass rides on wave 1.


---

## 7 · Sourcing (Rule I gate)

A mechanical sourcing search is **running** as this spec is written, recorded to
`research/210_nse_ingest_core_sourcing.md`. It evaluates, by installing and calling them against the
real NSE endpoints rather than by reading their READMEs:

- **NSE-specific:** `jugaad-data`, `nsepython`, `nsepy`, `nsetools`, `pynse`, `nse-utility`,
  `bhavcopy`, plus whatever a PyPI/GitHub sweep surfaces. The decisive test is the **July 2024
  bhavcopy format change** — a library that handles only one era is wrong across half the archive,
  and that is exactly the kind of thing a README will not tell you.
- **Generic parts:** `tenacity` / `backoff` / `urllib3.Retry` / `httpx` for retry, judged specifically
  on whether they can express *classification* rather than a single retry policy — an HTML error page
  served with **HTTP 200** is the case most retry libraries handle badly, and NSE serves exactly that.
  Bitemporal storage candidates (`deltalake`, SCD2 helpers, `sqlite-utils`) are checked before any
  hand-rolled store is accepted.

**No adapter is built until that verdict lands.** If an existing library covers a source correctly it
is adopted or vendored rather than reimplemented; if it covers it partially, the gap is what gets
built. Rejections carry mechanical evidence per `R.17` and are surfaced to the operator for
double-check per the standing rule.
