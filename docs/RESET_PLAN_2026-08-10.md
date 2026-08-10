# RESET PLAN — 2026-08-10

Full-server archive to GitHub, then destructive reset down to docs + three survivors,
then interview → `ajith_final_plan.md` + `ajith_final_todo.md` → fresh rebuild.

**Status: Phases 1–3 COMPLETE (2026-08-10 12:10 IST). Phase 4 (interview) is next.**

## Execution record

**Archive:** `github.com/ajith4134/nse-algo-trader-archive-2026-08-10` (private).
139 commits. Pre-reset snapshot commit `f5bc843` (999 tracked files); runtime state added in
`223305c` and `927e145`; reset recorded locally as `9535af0`.

**Verification gate (all passed before any deletion):**

| Check | Result |
|---|---|
| Tree SHA local vs. fresh clone | `526a0969…` both sides — identical |
| Tracked file count | 999 / 999, zero blob diff |
| SQLite integrity + row counts, 12 databases | 12 ok, 0 failed |
| Release asset SHA256 | `sha256sum -c` → OK |
| Market data after decompression | integrity ok, 659,990 bars + 1,436,568 F&O rows |
| `segment_bot_pod` (393 files) | byte-identical; 6 unmatched entries confirmed empty dirs |

**Two corrections to the pre-execution survey.** The "418 `.md` files" figure counted 138
markdown files inside `.venv` (third-party library docs); the real total is **280**. And
`/home/opc/.nse_algo_trader/segment_bot_pod/` — 393 files, 32 MB, holding per-underlying
vol-regime / IV-rank / VRP state, pod open positions and trained `bull_bear_models.joblib`
artifacts — was missed by the first staging pass, which only walked top-level files. It was
caught before deletion, archived in `927e145`, and verified byte-identical.

**Deleted:** 6.7 GB `.venv`, 354 `src/` modules, 262 test files, 74 scripts, `logs/`,
`experiments/`, `deploy/`'s unit file, all caches, `pyproject.toml`, and 11 of 13 runtime
databases. Disk went 50 G → 44 G used.

**Survives:** 281 files tracked, 280 of them `.md` (plus `.gitignore`); `/home/opc/RESET_KEEP/`
(15 files); `experience_memory.sqlite3` (3,481 closed trades, WAL checkpointed,
`integrity_check=ok`); `market_data.sqlite3` (327 MB). `nse-dashboard.service` stopped and
disabled.

**Open items.** `/home/opc/ollama` holds 21 GB of downloaded local models — untouched, since
it backs the local-first tier of the LLM cost ladder rather than being project code. The
`.git` directory (44 MB) is retained, so every deleted file is also recoverable locally; delete
it only if a truly bare slate is wanted. The GitHub PAT pasted into the session transcript
still needs revoking.

---

---

## 0. Current state (surveyed, read-only)

| Thing | Location | Size / count | In git? |
|---|---|---|---|
| Code repo | `/home/opc/nse-algo-trader` | 6.9 G total | yes → `github.com/ajith4134/nse-botonly.git`, branch `master` |
| ↳ pending work | — | 3 unpushed commits + 112 uncommitted files | **not pushed** |
| ↳ `src/` | `src/nse_algo_trader/` | 8 M, 30 trunk packages | yes |
| ↳ `docs/` | `docs/` | 3.8 M, **418 `.md`** | yes |
| ↳ `tests/` | `tests/` | 7.5 M | yes |
| ↳ `logs/` | `logs/` | 24 M (mostly dashboard PNGs) | yes |
| ↳ `.venv/` | `.venv/` | **6.7 G** | no (ignored, rebuildable) |
| ↳ `.env` | `.env` | 6.3 K — **Kite TOTP secret** | no (ignored) — **must never reach GitHub** |
| Runtime state | `/home/opc/.nse_algo_trader/` | **383 M** | **NO repo at all — currently zero backup** |
| ↳ closed trades | `experience_memory.sqlite3` | 2.8 M, **3,481 closed trades** (realized P&L, exit cause, Brier) | no |
| ↳ market data | `market_data.sqlite3` | **327 M** — 659,990 bars, 1,436,568 F&O rows, bhavcopy, MWPL, IV | no |
| ↳ kite token | `kite_access_token.json` | 130 B — **secret** | no |
| ↳ other state | news / autopoiesis / arm posterior / debate / promotion / trials | ~50 M | no |
| Free disk | `/` | 14 G free of 63 G | — |

Tooling: `gh` 2.97.0 present but **not authenticated**; `GITHUB_TOKEN` in `.env` returns
`Bad credentials`. No `git-lfs`, no `zstd`. `pigz` + `xz` available.

---

## 1. Decisions taken (user, 2026-08-10)

1. **Archive destination** — new **private** repo, full clean snapshot. `nse-botonly` left untouched.
2. **Runtime state** — back up all of it; locally keep `experience_memory.sqlite3` **and**
   `market_data.sqlite3`; delete the rest.
3. **Docs** — keep **all 418 `.md`** files.
4. **Survivors** — keep the working code for Kite TOTP login + Claude subscription provider,
   plus `.env`, `kite_access_token.json`, closed-trade DB, staged for re-integration.
5. **(added mid-turn)** After `ajith_final_plan.md` + the todo file exist, delete `CLAUDE.md`,
   `docs/RULES.md` and every other rules doc — those two files become the only governing docs.

---

## 2. Phase 1 — archive everything to GitHub

**Prerequisite (user action):** authenticate. In the Claude Code prompt, type:

```
! gh auth login
```

Choose GitHub.com → HTTPS → authenticate with a browser/device code. Scopes needed: `repo`
(create + push a private repo) and `workflow` is not required.

Then:

1. **Commit the pending work in place** — 3 unpushed commits + 112 changed/untracked files
   committed to `master` so nothing is lost. Not yet pushed to `nse-botonly` (per decision 1,
   that repo is left alone; the commit is local so the archive snapshot below captures it).
2. **Create the private archive repo** `nse-algo-trader-archive-2026-08-10`.
3. **Push the full repo history** (all branches + tags) to the archive remote.
4. **Archive the untracked runtime state.** `/home/opc/.nse_algo_trader/` is in no repo today —
   it is the single biggest data-loss risk in this whole operation.
   - Secrets (`.env`, `kite_access_token.json`, `breeze_session_token.json`,
     `dashboard_access_token.txt`) are **excluded** from anything that reaches GitHub.
   - Small state (JSON, logs, sub-100 M sqlite) → committed into the archive repo under
     `runtime_state/`.
   - `market_data.sqlite3` (327 M) exceeds GitHub's 100 M per-file limit and there is no
     `git-lfs` here → compressed with `pigz` and uploaded as a **GitHub Release asset**
     (2 G per-asset limit), not as a tracked file.
5. **Local secrets backup** — `.env` + token files copied to `/home/opc/RESET_KEEP/secrets/`
   (mode 600). These stay on-server only, never uploaded.

## 3. Phase 2 — verify the upload (gate)

Deletion is blocked until every check passes:

- `git ls-remote <archive>` shows the pushed commit SHA, and it equals local `HEAD`.
- `git fetch <archive> && git diff --stat HEAD <archive>/master` → empty.
- File-count and per-directory checksum manifest of the local tree vs. the remote tree match.
- `gh release view` confirms the market-data asset uploaded, and its SHA256 matches local.
- A **fresh clone into a scratch dir** succeeds and its manifest matches.

Failure of any check → stop, report, delete nothing.

## 4. Phase 3 — the destructive reset

Staged into `/home/opc/RESET_KEEP/` **first**, verified, then the deletion runs.

### Survives

- Every `.md` in the repo (all 418) + `README.md` + directory structure under `docs/`.
- **Kite hands-off TOTP login:** `broker_sessions/kite_totp_auto_login.py`,
  `kite_access_token_store.py`, `authenticated_kite_client_builder.py`,
  `refresh_kite_access_token.py`, `broker_credentials/kite_login_credentials_loader.py`,
  `broker_credentials/broker_api_credentials_loader.py`.
- **Claude Max subscription as LLM API:** `llm_strategy/claude_code_subscription_provider.py`,
  `llm_strategy/warm_claude_subscription_session.py`.
- **Closed-trade history:** `/home/opc/.nse_algo_trader/experience_memory.sqlite3`.
- **Market data:** `/home/opc/.nse_algo_trader/market_data.sqlite3`.
- Secrets: `.env`, `kite_access_token.json`.

### Deleted

- All other `.py` under `src/`, all of `tests/`, all of `scripts/`, `experiments/`, `deploy/`.
- `logs/` (PNGs, apiLogs), all caches (`.mypy_cache`, `.ruff_cache`, `.pytest_cache`,
  `.hypothesis`, `__pycache__`), `.venv/` (6.7 G reclaimed).
- Non-kept runtime state DBs and JSON in `/home/opc/.nse_algo_trader/`.
- `pyproject.toml` and other non-`.md` config.

### Also stopped

`nse-dashboard.service` is systemd-managed and will crash-loop once its code is gone —
it gets `systemctl stop` + `disable` as part of this phase, not left thrashing.

## 5. Phase 4 — the interview

Long, structured, forced multiple-choice session covering every axis: goals, capital, risk
appetite, instruments, strategy families, autonomy level, LLM budget, data sources, execution,
ops, dashboard, what to carry forward from the 16-trunk atlas and what to abandon. Sources:
all 418 surviving docs, re-read first. Persisted as it goes, not held in chat.

## 6. Phase 5 — the two governing files

- `ajith_final_plan.md` — every feature and idea ever discussed, combined, deduped, ordered.
- `ajith_final_todo.md` — the executable todo list derived from that plan.

## 7. Phase 6 — retire the old rules

Once both files exist and are approved: delete `CLAUDE.md`, `docs/RULES.md`, `GLOBAL_CLAUDE.md`
and every other rules/instruction doc. The two new files become the sole governing documents.

## 8. Phase 7 — rebuild

Fresh implementation driven only by `ajith_final_plan.md` + `ajith_final_todo.md`, plus the
three carried-forward survivors.
