# Whole-server reconciliation — 2026-08-17

**What was asked.** Read everything on this VPS, count what is actually built, diff it against
`ajith_final_plan.md`, `ajith_final_todo.md` and the ideas list, say whether the direction is right,
and use the open market to verify against live data.

**Method.** Direct measurement, not doc-reading: file counts and LOC from the filesystem, checkbox
counts parsed from the todo, service state from `systemctl`, live HTTP probes against the running
dashboard, and the daily-operations log read for its last real end-to-end result. Where a document
and the server disagreed, the server won.

---

## 1. What exists, measured

| Thing | Measured |
|---|---|
| Production code | **61,808 LOC** — `src/` 169 files (56,649), `scripts/` 19 files (5,159) |
| Tests | **38,746 LOC** across 101 files (0.63 test-to-source ratio) |
| Packages under `src/nse_algo_trader/` | 21 subpackages + 15 top-level modules |
| Modules over 300 LOC | 65 |
| Modules under 150 LOC in an engine-named location | **0** — no thin-scalar `R.07` violations by that heuristic |
| Docs | 21 top-level + **280** research notes + 21 idea files |
| Live services | `nse-dashboard` (running), `nse-depth-capture` (running), `nse-daily-operations.timer` |
| Stored market data | `nse_ingest.sqlite3` 1.3 GB · `market_data.sqlite3` 488 MB · deep history cash 11.77 M rows (1994-11-03→) and F&O 181.86 M rows (2000-06-12→) |

The depth of what is built is not in question. The largest modules are genuine engines — a 1,928-line
bar/tape join verification engine with a Beta-Binomial disagreement null, a 1,609-line
optimisation-based order-expression selector, a 2,538-line cost-surface renderer. `R.07` is being
honoured.

## 2. Plan coverage, counted

`docs/ajith_final_todo.md` holds **752 checkboxes**: **110 done · 32 partial · 610 open**.

| Phase | done | partial | open | What it delivers |
|---|---|---|---|---|
| 0 GROUND ZERO | 13 | 1 | 1 | Repo, env, guards |
| 1 TRUTH AND COST | 23 | 23 | 12 | L0 data truth + L1 cost floor |
| 2 VALIDATION, OPS, EXECUTION | 19 | 3 | 68 | Search integrity, ops floor, order path |
| 3 FIRST HOLON (cash-intraday) | 6 | 1 | 125 | Signals, strategies, bot contracts |
| 4 PROVING GROUND | 1 | 2 | 42 | Where instructions earn the right to exist |
| 5 INTROSPECTION / CHAT / LLM | 1 | 2 | 49 | Decision traces, panels, chat |
| 6 SIX HOLONS | 0 | 0 | 95 | Options/Greeks, portfolio, remaining holons |
| 7 THE ORGANISM | 0 | 0 | 109 | Conductor, meta bots, memory, conscience |
| 8 LIVE CAPITAL | 0 | 0 | 12 | Real rupees, graduated instructions only |
| 9 FRONTIER | 0 | 0 | 25 | Earned-only |
| X CONTINUOUS | 47 | 0 | 72 | Standing rules/decision register (never "done") |

Adjustments that make the raw number less bleak than it reads: **14** open items are annotated
*"NOT A BUILD UNIT — MERGE/DROP"* by `A.94` and are closed-by-reclassification, and Phase X's 72 open
rows are a permanent ledger rather than backlog. The honest build figure is roughly **110 of ~666**.

**The shape matters more than the fraction.** Phases 0–2 are dense with completed work. Phase 3
onward — the first holon, the thing that actually decides a trade — is 6 done against 125 open, and
Phases 6–9 are untouched at 0. The foundation is deep and the money-making layer has not started.

## 3. Where the frontier actually sits

Last completed item in the main phases is **5.40** (screenshot-verify loop). The next twelve open
items are the entire `L13.29`–`L13.40` block: **the decision-trace contract**, then trace levels 0–3,
then the chat panel and its gating. After that, Phase 6 opens with segment holons.

That ordering is right and was decided deliberately (`A.29`/`L13.29`: *decision traces before
panels* — reasoning cannot be reconstructed afterwards). Nothing in the measurement suggests
reordering it.

## 4. Blocked, and by whom

| Item | Entry | Blocker |
|---|---|---|
| 0.5 Revoke leaked GitHub PAT | `B.10` | **Operator-only** — no API exists; also check `github.com/settings/keys`, since `admin:public_key` survives revocation |
| 1.18 Fyers deep history | `L0.18` | **Operator** — needs interactive auth or `FY_ID`/PIN/secret |
| 1.19 Groww historical | `L0.19` | **Operator** — ₹499/mo subscription not active; every endpoint returns `Access forbidden` |
| 1.26 Bulk/block deals history | `L0.26` | NSE historical API 503s behind a bot-block; 8 alternate paths all 404/403 — forward-accrual only |
| 1.12 Replay experience provenance | `L0.12` | No experience-memory store exists yet to tag (`A.70`) |
| 1.31 two price-band families | `L0.31` | Appear in no NSE circular — forward capture only |

Five of six are operator actions or external walls, not engineering.

## 5. What the live market found today

The market was open while this ran. Four things were verified against it, and two of them were
broken.

**BROKEN — the daily Kite token refresh had never existed.** Recorded in full as `A.129`. Two cron
entries invoked `nse_algo_trader.broker_sessions.refresh_kite_access_token`; the module was never
written; `nse-depth-capture.service` had been in `activating (auto-restart)` since the 09:15 open,
exiting `status=2` with `no valid Kite access token — run the daily TOTP login first`. **Fixed:** the
module is now built (188 lines, idempotent, three-strike retry per `R.21`, non-zero exit, never
prints the token) with 13 tests including one that runs the exact command cron runs. Token
regenerated for user `HZV381`, capture restarted, and it is writing real Parquet — 2.6 MB across
`session_date=2026-08-17/capture_run=093515/` within four minutes.

**BROKEN — a second dead cron entry, found by auditing the rest.** `nse_algo_trader.market_data.daily_nse_reports_ingestion_job`
names a package (`market_data`) that does not exist at all. Both its cron lines have been failing
since the reset. **This work is not missing** — NSE report ingestion runs as the `ingest` step of
`scripts/run_daily_operations.py` under `nse-daily-operations.timer`. The cron lines were stale
leftovers, and have been replaced with a comment saying where the work actually happens.

**NOT REPRODUCIBLE — the `/history` 503.** The 2026-08-15 daily-operations run failed its final step
with `/history returned HTTP 503 — refusing to screenshot an error page as if it were the surface`.
Probed live today: `/history`, `/wall`, `/costs`, `/trials`, `/sizing`, `/clock` all return **200**.
The failure was transient or has since been fixed. `/paper`, `/replay` and `/orderpath` return
**404** — those surfaces are genuinely absent, consistent with 5.13's note that 37 of 44 modules have
no panel.

**STILL OPEN — `fo_ban_list` ingest fails.** The same run reported
`fo_ban_list=FAILED(BitemporalIngestStoreError)` inside an otherwise-successful ingest step. This is
the concrete cause behind todo **1.25**'s complaint that F&O ban-list history will not accrue. Not
yet diagnosed.

**Also confirmed:** todo **2.59a** ("scheduled daily ops — not yet observed running a full end-to-end
cycle") *has* now been observed — the 2026-08-15 run completed **22 of 23 steps**, ingesting 3,464
cash and 35,089 F&O bhavcopy rows, fitting 24 reversion calibrations from 129,604 events across 329
symbols, and deriving cost floors over 392 instruments. That is a real end-to-end cycle.

## 6. Is the direction right?

**On engineering depth: yes, clearly.** No thin engines, a 0.63 test ratio, five adversarial review
rounds run, every threshold derived rather than hardcoded. The 2026-08-02 redesign to depth-first
NSE-only was correct and is being executed.

**On bookkeeping: no, and it is drifting.** Four separate defects in how progress is recorded:

1. **`MASTER_PROGRESS.md` is 21 days stale** (2026-07-27) and states two different coverage figures
   in the same passage — 33.0% and 44.2% — layered from different edit passes.
2. **Two incompatible progress frameworks coexist.** `MASTER_PROGRESS` counts atlas branches
   (87/197); `FEATURE_MAP.md` counts plan-entries and features (42 of 599). Neither references the
   other, and nothing says which governs "what is left".
3. **`atlas_branch_loop_runlog.md` is an empty template.** Zero entries, despite dated trunk
   completions claimed elsewhere. The coverage percentages cannot be corroborated.
4. **`FEATURE_MAP.md` declares `F01` closed** with all four criteria ticked, while `BACKLOG.md`
   carries **nine unfixed majors** against that same feature — including `M10`, flagged there as the
   single finding most likely to *reverse* `F01`'s recorded conclusion, and `M11`, which makes the
   `INSTRUMENT_FITTED` rung unreachable.

Item 4 is the one that matters. A feature marked closed while carrying a finding that could reverse
its conclusion is exactly the failure `R.13` was written against.

**On the deeper risk — and this is the finding of the day.** Five adversarial review rounds have
been run against *algorithms*. Zero have been run against *the wiring that decides whether those
algorithms execute*. The suite is 38,746 lines and not one line asserted that a command in the
crontab was runnable. Two of the crontab's four lines were dead. Recorded as `O.112`; the
generalisation is that this project verifies "is this computation right?" obsessively and "does this
computation happen at all?" not at all — and the recorded incident history supports it
(`M27` self-matching wait loops, `M30` a silent numpy bump, `M33` destroyed uncommitted work: three
process incidents, zero algorithm incidents).

## 7. Owed work opened today

- Diagnose `fo_ban_list=FAILED(BitemporalIngestStoreError)` — closes 1.25's `R.11` gap.
- Give every scheduled entry point a surface showing last outcome and last success time; today they
  fail into log files with no reader (`R.08` applied to jobs, not just engines).
- Reconcile the two progress frameworks and declare one governing; retire or rebuild
  `MASTER_PROGRESS.md`.
- Resolve `F01`'s nine open majors against its "closed" status in `FEATURE_MAP.md`, `M10` first.
- Build `/paper`, `/replay`, `/orderpath` surfaces — currently 404.
