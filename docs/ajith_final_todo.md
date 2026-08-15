# AJITH FINAL TODO — the complete project, start to finish

**Generated 2026-08-10 directly from `ajith_final_plan.md`.** Every one of the plan's **588 feature
entries** appears below as its own task, in dependency order, plus the operational and governance work.
Nothing is grouped away and nothing is summarised — the coverage audit at the end diffs this file against
the plan and proves it.

**This is the whole project, beginning to end**, not one phase. Phases 1–4 reach the first graduated
instruction; 5–7 build the full six-holon organism and its dashboard; 8 puts real money on it; 9 is the
frontier.

**Read top to bottom.** Order is dependency order. Each task carries its plan entry ID — read that entry
before building it.

**Standing rules apply to every task:** R.01–R.26. Most load-bearing: no hardcoded values (R.03), full
function on thin data (R.04), real-data verification (R.05), no orphans (R.06), engine-grade depth (R.07),
every feature visible (R.08), no silent skips (R.11), and **correctness of execution is not evidence of
correctness of allocation** (R.13).

---

## Build shape (recorded so it is not re-litigated)

**Hybrid: minimal foundation scoped to ONE holon, then that holon whole, then widen.** Both alternatives
are failure modes this project already lived through — pure foundation-first produced 288 modules and no
proven money; pure vertical-slice produces the "code too thin" diagnosis in REDESIGN_v1.

- **First holon: cash-intraday** (A.07) — lowest cost floor, no Greeks prerequisite, simplest denominator,
  and it exercises the overnight-carry path.
- **Decision traces before panels** (A.29 / L13.29) — reasoning cannot be reconstructed afterwards.
- **Phase 1 done = one instruction GRADUATES**, not a trade placed. A placed trade proves plumbing; a
  graduated instruction proves the pipeline.

---

# PHASE 0 — GROUND ZERO

*Repository, environment and the guards that everything else assumes.*

- [x] **0.1** Archive everything to a private repo, verified by fresh clone + tree-SHA + per-database integrity — `A.26`
- [x] **0.2** Reset to docs-only; retain Kite TOTP login, Claude subscription provider, 3,481 closed trades, market-data store — `A.26`
- [x] **0.3** Python 3.12 venv; ARM64 stack verified by function (LightGBM trains, CVXPY solves, TA-Lib imports) — `B.09`
- [x] **0.4** Reclaim disk — 43 GB free — `B.09`
- [ ] **0.5** **Revoke the leaked GitHub PAT** — `B.10` ⚠️ *operator action, and the ONLY part of
      this that cannot be automated: GitHub publishes no API to revoke a classic PAT, so it is a
      web-UI action.* **Audited 2026-08-12 (`A.95`) — exposure is bounded but the scopes are not.**
      The working tree, all **231 commits** and every git remote are CLEAN — the token is not in
      this repository. It lives only in `~/.config/gh/hosts.yml` at mode 0600. Both project repos
      are PRIVATE and the account has no organisations.
      **What makes it urgent anyway:** it is a *classic* PAT carrying `repo`, `admin:org`,
      `admin:enterprise`, `delete_repo`, `workflow`, `admin:public_key`, `write:packages`, `gist`
      and `user` — reach over **10 private and 4 public repositories**, with the power to delete
      any of them and to inject CI workflows. The blast radius is the entire account, not this
      project.
      **Do this:** (1) revoke at https://github.com/settings/tokens ; (2) replace with a
      FINE-GRAINED token scoped to `nse-botonly` and `nse-algo-trader-archive-2026-08-10` only,
      with `Contents: read and write` and nothing else — that is everything this project uses and
      it removes essentially all of the blast radius permanently; (3) `gh auth login` with it.
- [x] **0.6** Committed-credential guard — `L3.30` · a dependency-free detector for every
      credential shape this project holds, wired into the Stop hook over the TRACKED tree and
      available over full history with `--include-history`. Findings name their blast radius. 23
      tests, including two `R.05` tests that assert THIS repository and its whole history are
      clean — so the `B.10` audit result is pinned and cannot silently stop being true.
      *The half of `B.10` that was actually in my control (`A.95`).*
- [x] **0.6** Repo skeleton — `src/nse_algo_trader` package, `pyproject.toml` with ruff (security, naming, datetime-awareness, blind-except bans) + strict mypy + pytest markers naming the R.23 test kinds; installed editable
- [x] **0.7** Execution gate — the Stop hook now runs **ruff + mypy + pytest** and blocks the turn on any failure (`L2.32`, R.23 step 6)
- [x] **0.7a** Rupee-literal detector — ***now actually wired*** via `scripts/check_no_hardcoded_money.py` into the Stop gate over `src/` + `scripts/`; firing proven with a planted violation (`A.43`, `O.30`). Was an orphan invoked only by its own test — AST guard failing the build on hardcoded money; enforces `R.03` mechanically — `L2.31a` *(retro-listed 2026-08-10: built earlier but had no plan entry or task, so it was an untracked orphan)*
- [~] **0.8** Capital as a runtime parameter across ₹1 lakh → ₹1 crore, with **no rupee constant anywhere** — `A.23`, `R.03`
      · built: `capital_configuration` (Decimal money, high-precision context, validated in `__post_init__`) + `rupee_literal_detector` (26/26 evasion corpus caught, was 0)
      · spec `docs/research/200`; 42 tests; gate green; 3 mutants verified caught
      · ⚠️ **NOT R.11-done — primary consumer queued**: the position sizer (`L1.10`) and risk sizer (`L7.01`) in Phases 1–2 are the first real callers. Tick to [x] when one consumes it.
- [x] **0.9** Secrets from environment only — capital loads from `NSE_TRADING_CAPITAL_RUPEES` with no default; `.env` gitignored and asserted by a skeleton guard — `R.02`
- [x] **0.10** Decision-log discipline: every decision recorded as an `A.` entry in the plan, with reasoning — `A.22`
- [x] **0.11** Idea-intake protocol wired into the working habit — four verdicts before anything is written
- [x] **0.12** Deleted `CLAUDE.md`, `docs/RULES.md`, `GLOBAL_CLAUDE.md` — `ajith_final_plan.md` + `ajith_final_todo.md` are now the only governing docs (recoverable from `f5bc843` and the GitHub archive)
- [x] **0.13** Enforcement hooks reconciled in `~/.claude/settings.json` — the old rule text and dead-path checks are gone; the gate now injects R.01–R.13, the idea-intake protocol and the decision-log rule, and three Stop gates enforce quality, todo-sync and test-pairing (backup at `settings.json.bak_2026-08-10`)


# PHASE 1 — TRUTH AND COST

*L0 data truth + L1 cost. Nothing above this is meaningful if the data lies or the costs are wrong.*


**— L0 —**

- [~] **1.1** Kite instrument master + daily dump ingest — fetch (retries, backoff, size check) + strict parse + point-in-time store — `L0.01`
      · spec `docs/research/202`; 89 tests; gate green; **13/13 mutants caught**; R.05 passed on the live 113,955-row dump
      · ⚠️ **NOT R.11-done — consumer queued**: observation tiers (`L5.21c`, task 3.53d) and the option-chain feed (`L6.28`)
- [~] **1.2** Instrument-token reuse guard — full-history lookup, rename-vs-reuse discrimination, persisted + auditable — `L0.02`
      · adversarial review found the original detected only same-day swaps, the one pattern Kite never produces
- [~] **1.3** Historical bar store (SQLite) — `L0.03` — *built, gated, mutation-tested 19/19, R.05 pass on all 659,990 retained bars. `[~]` not `[x]` per R.11: the primary consumer (indicator pipeline / backtest reader) is still queued.*
- [~] **1.4** Bitemporal availability-time on the bar store — `L0.04` — *same slice; availability filtering is a property of the store. Same R.11 caveat.*
- [~] **1.5** Point-in-time universe reconstruction — `L0.05` — *built; review found 13 defects, all fixed;
      mutation 16/16; R.05 on real F&O + real cash + real MWPL. `[~]` per R.08 (no dashboard surface yet).*
- [~] **1.5a** Absence classifier — six evidence-carrying classes, `UNKNOWN` first-class — `L0.05a` — *built
      + review-hardened (gap ceiling derived, ISIN ambiguity surfaced, multi-date absences retained).*
- [~] **1.5b** F&O exit early-warning from expiry-ladder truncation — `L0.05b` — ***VALIDATED on 349 real
      exits over 25 years**: recall 77.1%, 22.9% missed, ~17% false positives, median lead 41 sessions.
      Reclassified as a SCREEN, not a forecast. Norm measured 3 on 6,160 dates and **4 on 47** — a hardcoded
      3 would have been wrong on 47 real days.*
- [~] **1.6** Frozen tradable-universe snapshot per date — `L0.06` — *built with `1.5`; immutable +
      bitemporal, reports its own unresolved count. `[~]` per R.08.*
- [~] **1.7** Corporate-action adjustment — `L0.07` — *built + review-hardened. Feed acquired: **41,885 real
      actions 2001-2026**. Review found 8 defects incl. a critical false positive (`DRREDDY` factor 1/7 on a
      flat day); all fixed, mutation 10/10. Empirical end-to-end check: 65/66 sampled ex-dates within 20% of
      1.0, median 1.016. `[~]` per R.08 + F&O factor acquisition unbuilt.*
- [x] **1.8** Symbol-rename / ISIN / merger record store — `L0.08` · 166 renames + 199 unsafe symbols found in 43,209 real observations; wired into the daily run (`A.68`)
- [~] **1.9** Delisted-securities master (BSE-sourced) — `L0.09` — *NSE half built and certified (328
      rows, real fetch); handles a raw newline inside a quoted CSV field and two-digit years.
      ⛔ **NOT DONE — the plan says BSE-sourced and I routed the agent to NSE (`A.51`).** Measured: NSE's
      list is **frozen since 2020-11-11 at 328 rows**, BSE returns **4,612 rows live today**. The BSE
      adapter is queued in BACKLOG; a proven prior implementation exists at `63aa3a1`.*
- [~] **1.10** Gap detection + provenance-flagged backfill — `L0.10` — *built on the ingest core.
      The content is the CLASSIFICATION, not the list: a missing date is never-attempted (work
      outstanding), established-absent (asked, archive said 404 — stop retrying) or recoverable-failure
      (bot-blocked/timeout/wrong-date — keep retrying, escalate at three strikes per `R.21`).
      Collapsing those produces a backfill that hammers holidays nightly and quietly gives up on real
      outages. Backfill provenance is MEASURED from the store's two clocks rather than set as a flag
      somebody must remember — a row learned 40 days after its effective date was not knowable then,
      and a point-in-time reader excludes it automatically. Backfills are bounded so a source years
      behind cannot turn one run into an unbounded crawl of a host that bot-blocks. `[~]` per `R.11`:
      nothing schedules it yet.*
- [x] **1.11** Causal leakage firewall — `L0.11` · bitemporal rebuild; blocks 44,526 of 720,240 real rows when replaying 2026-08-10 (`A.69`)
- [ ] **1.12** Replay experience provenance — `L0.12` ⛔ BLOCKED: no experience-memory store exists to tag (`A.70`)
- [x] **1.13** Honest clock / day-walker + firewall — `L0.13` · wall-clock reads now a GATE test; replays real sessions blocking 44,526 rows (`A.70`)
- [x] **1.14** Multi-broker historical bar source — `L0.14` · Kite + Angel One live-verified behind one Protocol; 3 brokers blocked with measured reasons (`A.71`)
- [x] **1.15** Multi-broker failover + gap-fill aggregation — `L0.15` · union+disagreement grading, derived source preference; fills the bar store nightly (`A.72`)
- [ ] **1.16** Breeze 1-second historical bars — `L0.16`
- [x] **1.17** Broker symbology resolver (ICICI is one instance) — `L0.17` · Angel One built: 3,163 mappings, 100% coverage of the bar step (`A.73`); ICICI instance still credential-blocked
- [ ] **1.18** Fyers deep-history adapter — `L0.18` ⛔ BLOCKED, **re-verified mechanically 2026-08-12** (`A.78`): `fyers-apiv3` imports fine and `.env` carries `FYERS_APP_ID` + `FYERS_API_SECRET`, but v3 mints a token only through an interactive `generate-authcode` browser redirect, and the non-interactive TOTP path needs `FY_ID` + PIN + TOTP secret, none of which exist. `generate_token()` without an auth code raises `AttributeError: 'SessionModel' object has no attribute 'auth_token'`. ⚠️ *operator action: complete one browser auth, or add the three TOTP-login variables*
- [ ] **1.19** Groww historical adapter — `L0.19` ⛔ BLOCKED, **re-verified mechanically 2026-08-12** (`A.78`): `growwapi` installs, the client builds and prints "Ready to Groww!" on BOTH stored tokens — and every endpoint (`get_historical_candles`, `get_quote`, `get_ltp`, `get_holdings_for_user`) answers `GrowwAPIException: Access forbidden for this request`. The ₹499/mo subscription is not active. ⚠️ *operator action: activate the subscription*
- [x] **1.20** Live order-book depth recorder (P4b) — `L0.20` — ***PULLED FORWARD out of sequence, see
      `A.44`***: the depth tape is the only `L0` artifact that cannot be reconstructed after the fact, so
      every session without a recorder is permanently lost data. `src/nse_algo_trader/market_depth/`
      (feed seam · integrity classifier · admission controller · recorder · session report), 60 tests,
      gate green. **R.05 passed on the live socket 2026-08-11** — first capture 51,134 real packets in
      4 minutes across 300 instruments, then widened to **4,861 instruments across 2 shards**.
      Measurements that became design inputs rather than assumptions: `exchange_timestamp` is a true
      epoch parsed NAIVE by the SDK (correct on this UTC host **by accident** — `A.45`), epoch 0 in ~1 in
      6 packets, one subscribe snapshot **11 minutes stale**, depth shape `(5,5)` on 4,084 of 4,084.
      **Closed 2026-08-12 (`A.79`):** both `R.11` conditions are now met — the primary consumer
      `1.22` exists and replays this tape, and `R.08` is satisfied by `/microstructure`. The
      first full-session capture also corrected the admission solve (`A.77`).
- [x] **1.21** Market-depth store — `L0.21` — built with `1.20`. Append-only Parquet + zstd, atomic
      part files via temp-then-`replace` **proven by an actual `kill -9` mid-session test**, bitemporal
      (`exchange_time` vs `receipt_time`) consistent with `1.4`, integer paise never floats, plus
      `book_at()` — the reconstruction primitive `1.22` builds on. **Measured on real data: 62.02
      compressed bytes/row**, which beat every synthetic benchmark in the sourcing run (`research/208`)
      and is the admission controller's key input. ArcticDB and `nautilus_trader` were both rejected on
      MECHANICAL evidence (`R.17`): neither is pip-installable on this ARM64 / glibc-2.34 host.
- [x] **1.22** Tick-level order-book reconstruction — `L0.22` · **renamed to what it is: `order_book_snapshot_replay_engine`.** True tick reconstruction needs order-by-order data NSE sells for ₹12.5 lakh/yr (`research/72`); this is the snapshot-replay engine the 5-level tape can actually support, with the ceiling carried on every row rather than in a footnote. Cont-Kukanov-Stoikov multi-level OFI, Lee-Ready trade-side inference, Stoikov micro-price, and an interval-valued queue-depletion decomposition. Spec `docs/research/214`; 39 tests incl. 3 R.05 passes over the real 2026-08-11 tape (135,401 transitions) and a differential property test against `tclf`. Surface at `/microstructure` (`A.79`)
- [~] **1.23** NSE bhavcopy ingest — `L0.23` — *built on the shared ingest core (`A.46`), cash AND
      F&O, across BOTH schema eras: legacy 14-column `SYMBOL`/`TIMESTAMP` files and 34-column UDiFF
      `TckrSymb`/`TradDt` files, with the era boundary MEASURED (legacy 404s from 2024-07-08, UDiFF
      404s before 2024-07-01, so the overlap is real). Certified through the conformance suite on real
      captured payloads. **R.05 passed end-to-end on live NSE: 107,695 rows** across three sources and
      both eras; idempotent re-run inserted 0. The F&O natural key carries expiry/strike/option-type —
      a symbol-only key would collapse a whole option chain into one row and discard the rest as
      revisions. `[~]` per `R.08`: no dashboard surface yet.*
- [~] **1.24** MWPL position-limit ingest — `L0.24` — *built on the ingest core. **The current feed
      was FOUND** at `combineoi_{DDMMYYYY}.zip`, closing what `research/207` §3 recorded as "not
      located" (`A.50`) — it is a strict superset of the dead `nseoi_` file, working from 2011 through
      today. CSV chosen over the XML sibling on evidence: the XML is malformed in both eras (spaces in
      a tag name; an unescaped `&` in `SBI CARDS & PAY SER LTD`). Schema changed three times (7/6/8
      columns), handled by reading whatever header a payload carries. Cross-source corroboration: the
      only two `"No Fresh Positions"` rows are BANDHANBNK and SAIL — exactly the ban-list symbols.
      21 tests. `[~]` per `R.08`: no dashboard surface.*
- [~] **1.25** F&O ban-list ingest — `L0.25` — *built on the shared ingest core. A ROLLING file with
      no archive: NSE overwrites `fo_secban.csv` in place, so history accrues only from the first
      snapshot and is never retrievable retroactively — the same permanent-loss shape as the depth
      tape (`A.44`). The file dates itself in a prose header (`Trade Date 11-AUG-2026`) and the URL
      does not, which is what makes `observed_at` load-bearing. First real snapshot stored:
      BANDHANBNK, SAIL for 2026-08-11. `[~]` per `R.11`: nothing schedules the daily run yet, and
      without it the history this exists to build will not build.*
- [~] **1.26** Bulk / block deals ingest — `L0.26` — *built on the ingest core; BOTH regulatory
      disclosures covered (bulk AND block are different disclosures with different SEBI thresholds —
      shipping only bulk would have been a silent narrowing per `R.12`). Real fetches: bulk.csv 150
      rows, block.csv 2 rows, both 2026-08-10. Coverage floors sourced to the actual SEBI circulars.
      **Natural-key finding from REAL data:** `(date, symbol, client, side)` COLLIDES — one client
      bought ATALREAL twice on the same day at different prices — so quantity and price are part of
      the key. Stated honestly as empirical, not guaranteed: two genuinely identical trades would
      still collide and the file carries no sequence number to rule it out. ⛔ **BLOCKER
      (re-verified first-hand, not cited):** the historical API 503s behind an Apache bot-block; 8
      alternate paths tried, all 404/403; the one HTTP-200 alternative returns the same rolling
      today-only data as JSON, not history. So this source accrues forward only.*
- [~] **1.27** ATM implied-volatility daily series — `L0.27` — *built and certified. **NOT blocked** —
      `research/207`'s verdict was wrong (`A.54`): the old endpoint was superseded, and
      `/api/option-chain-v3` returns 241KB of real data from a COLD session. **NSE publishes
      `impliedVolatility` itself**, so this is fetch-and-parse, not a Black-Scholes solver — the real F&O
      bhavcopy was checked first and has no IV column. Full universe: 5 index + 208 equity underlyings,
      not a sample. A wrong expiry guess returns HTTP 200 with empty `data: []` rather than 404, so a
      bounded calendar-derived expiry ladder is used and wrong guesses are caught by the content check
      before `parse()`. ⚠️ **Cost surfaced:** ~2,700 requests/run at full universe against the
      inconsistently-gated host; a two-phase discover-then-fetch primitive belongs in the CORE (BACKLOG).*
- [~] **1.28** Circuit-band / ASM / GSM state per symbol — `L0.28` — *ALL THREE states built and
      certified. **ASM was NOT blocked** — `research/207`'s verdict was wrong and is corrected in place
      (`A.53`): the endpoint is `/api/reportASM`, found by reading the page's own JavaScript rather than
      guessing a URL. Real fetches: `sec_list.csv` 3,335 rows (bands 20/5/10/2 + 208 `No Band`, GSM
      stages 0/I/II), `reportASM` 189 entries (128 long-term, 61 short-term). Circuit bands stored RAW
      including the literal `No Band`, plus a derived nullable percent — nothing normalised away. Found a
      real NSE data-quality anomaly: `DCI`/`INE0A1101019` appears twice in short-term ASM differing only
      in company-name casing; disambiguated by occurrence rather than dropped. 35 tests.
      ⚠️ **Documented blind spot:** `sec_list.csv` carries no date anywhere in its body, so its half of
      `content_mismatch_reason` can validate structure but never recency. `[~]` per `R.08`.*
- [~] **1.29** Index constituents + weights — `L0.29` — *33 NSE indices (broad, size-sliced, sectoral,
      thematic — not a NIFTY-50 sample), membership certified. **Weights FOUND** (`A.55`) by reading the
      index provider's own SPA bundle, on a separate host, carrying NSE's free-float methodology inputs —
      so weights are derivable from published fields, never approximated. ⛔ **The weight feed is STALE
      since 2026-01-08** (~7 months) while a sibling feed on the same host serves today's data; the
      content check self-dates the payload and therefore correctly REJECTS every weight target today —
      surfaced by a passing test rather than ingested as current. Membership unaffected. A real NSE
      sentinel row (`DUMMYHDLVR`, all zeros) is dropped, documented and tested.*
- [~] **1.30a** Calendar coverage self-check — per-year reliability, two derived conditions — `L0.30a`
      *(new 2026-08-10; built with `1.30`, same review pending)*
- [~] **1.30** Trading calendar — `L0.30` — ***PULLED FORWARD out of sequence, see `A.40`.*** Built as a
      prerequisite of `1.5`/`1.6`: the universe engine cannot tell a holiday from a collection gap without
      it. `src/nse_algo_trader/nse_trading_session_calendar.py`, 34 tests, gate green, R.05 pass against the
      real retained window. Adversarial review done (9 defects, all fixed); mutation 11/11 killed. `[~]` not `[x]` per R.11: the primary consumer (`1.5`) is still being built. **Measured
      limitation carried forward:** `pandas_market_calendars` recognises ZERO NSE holidays for 1990-1996
      and 2027-2030, and only 4 for 1998 — the calendar reports its own per-year reliability rather than
      trusting the library.
- [~] **1.30b** TWO-PHASE DISCOVER-THEN-FETCH in the ingest core — `L0.35` — *built 2026-08-11 at
      operator instruction (`A.58`), closing the core gap `A.54` recorded. Order is memory → discovery →
      the adapter's ladder, with the path taken RECORDED (asking and guessing yield identical outcomes but
      differ ~140x in cost). Validity horizon DERIVED from the data — an expiry list is valid until its
      nearest expiry passes, never a typed-in TTL. `ANSWERED_EMPTY` is a third outcome, distinct from
      failure and from absence. **R.05 on the live chain: 19 requests vs ~2,700, 99.3% eliminated**; the
      derived horizon correctly picked the nearest FUTURE expiry (2026-08-18), not today's. Wired into the
      runner, so not an orphan. **Security hardening after a review finding:** discovery is the ONLY
      place in the ingest core where remote payload data reaches URL construction, so discovered values
      are validated at construction — URL control characters, control bytes, unbounded length and empty
      values all refused, with 10 hostile inputs tested. `[~]` per `R.08`: no dashboard surface yet.*
- [x] **1.31** Point-in-time market rules + calendar history — `L0.31` · bitemporal rule store: 16 families, effective-time × belief-time, interval reconciliation with conflicts SURFACED not resolved away, and `RuleCoverageError` rather than substituting today's regime. Seeded from `research/61`'s dated circulars — 11 of 16 families covered, 5 deliberately uncovered because no admissible source exists. Spec `docs/research/215`; 34 tests incl. a `portion` differential property test and an R.05 pass against the exchange's own contracts (`A.80`). Surface at `/rules`. **Observed-fact path built same day** (`A.81`): `InstrumentMasterRuleObserver` derives tick/lot history from the daily instrument-master snapshots by run-compression, lazy per symbol (0.02s against 227,535 rows), registered as an `ObservationalRuleSource`; `tick_size`/`lot_size` now **observed only**, `session_hours` seeded — **14 of 16 covered, 2 left and both BLOCKED** (`per_stock_price_band`, `dynamic_price_band` appear in no circular; forward capture only). +12 tests, and a full-universe key sweep: 1,668 ambiguous symbol-days symbol-only, 0 with `segment` pinned (`O.58`)
- [x] **1.32** Clock sync + drift alert — `L0.32` · offset/skew ESTIMATOR, not a monitor: the Moon-Skelly-Towsley lower-envelope LP (`scipy` HiGHS) over the depth tape, exact-reduced by per-exchange-second minima + lower convex hull (11.4M rows → 20,086 observations → **15 hull vertices**); NTP arm (`ntplib` + a hand-built Marzullo intersection — no packaged implementation exists on PyPI) brackets host-minus-UTC; ADWIN + permutation-calibrated Page-Hinkley date the change points; SQLite carries fits/consensuses/alerts across days. Changes behaviour via `DepthPacketIntegrityClassifier`'s host-error-corrected staleness; wired into `run_daily_operations`; surface at `/clock`. Real-data pass on both tape sessions found TWO defects no hermetic test could (a fabricated 2-server "consensus", and illiquidity read as delay). Spec `docs/research/216`; 87 tests. **Adversarial review (`R.23(c)`) found 7 reproduced defects against 70 passing tests** — 2 critical (a permutation threshold calibrated against a statistic the detector never computed: 100/100 null false alarms, now 1/100; and a verdict ladder comparing two different quantities, making REFUSE unreachable) — all fixed with regression tests, and the LP fix moved the feed-derived skew to within **0.85 ppm of chrony's independent measurement**. `A.82`, `A.83`, `O.59`, `O.60`, `O.61`
- [x] **1.33** Multi-broker consolidated feed with liquidity-weighted cross-check — `L0.33` · captured live (Kite + Angel One, 128,640 rows, 67 instruments) then fused: alignment into comparable windows, inverse-variance x liquidity weighting with per-source noise from the **three-cornered hat** (Gray & Allan 1974 — the first design was mathematically unidentifiable with two sources and a property test proved it), synthetic NBBO, robust median with refusal, and a per-instrument broker ranking. Carried state in SQLite (pairwise variances in bps, availability, freshness, frozen-while-others-moved). **Real session: 80,180 groups, 98.10% resolved, 955 crossed, 92.5% exact midpoint agreement, Kite best on 54/67 driven by Angel One freezing 13.20% vs 10.08%.** Wired: daily-runner step, `/feed` surface, and an admissibility gate the microstructure replay refuses on. Adversarial review found **11 reproduced defects, one critical** (a rounding call had been reporting 29,030 of 86,306 unchanged quotes as movement) — all fixed with regression tests. Spec `docs/research/217`; 39 tests. **Third feed added same day** (`A.86` — the Upstox blocker was an untested credential, not an expired one), which made per-source noise identifiable (`angel_one` 0.074 bps², `kite` 0.342) and exposed a data defect two brokers had hidden: 25,761 rows where the top of book is an NSE ±3% price-band order, not a touch (`A.87`). Rejecting self-crossed books moved resolved from 86.29% to **95.97%** and refusals from 13,552 to **332**. `A.84`-`A.87`, `O.62`-`O.66`
- [x] **1.34** Deep-history price + universe sourcing (~20yr) — `L0.34` · **193,624,250 rows across 33 years are queryable** (14,314 files, zero unreadable, 123,722 rows/s, 130,515 quarantined; cash 1994-11-03→, F&O 2000-06-12→). Five header variants resolved by HEADER not date (the F&O `OPTION_TYP`/`OPTIONTYPE` pair **interleaves day-by-day** across 871 files, so date-branching is wrong by construction). Two readers — a row-wise reference and a vectorised polars path — kept honest by a differential oracle on real files; **DuckDB** chosen on a measured benchmark (6.6 ms vs SQLite's 205 ms on the cross-section a backtest issues). Point-in-time universe query 26 ms, a full 10,689-row symbol history 34 ms. Four defects found by RUNNING it, incl. `13-Jul-20` parsed as the year 20 AD — fixed by requiring every row's date to match its own file's name. **83.2% of F&O rows are untraded contracts** kept with null OHLC plus settlement and open interest. Wired: daily-runner step (incremental), `/history` surface. Adversarial review then found **6 defects behind a passing suite** — worst: two entire trading sessions silently absent from a store reporting itself complete (`A.89`), plus 90,655 duplicates and ~30,800 wrongly-discarded 1990s rows, all fixed and verified by query. Spec `docs/research/218`; 34 tests; `A.88`, `A.89`, `O.67`, `O.68`. **`B.04` NOT closed** — daily bhavcopy only, deep intraday remains unfree; narrow its wording

      *(The shared ingest core these nine depend on was built 2026-08-11 — `A.46`, spec `research/209`,
      sourcing `research/210`. `src/nse_algo_trader/nse_ingest/`: content-aware fetcher, adapter
      contract, bitemporal store, runner, coverage self-check, plus the conformance suite adapters
      must pass and may not edit. 46 tests. R.05 on the classifier PASSED against live NSE endpoints,
      including a first-hand reproduction of the stale-Sunday trap. End-to-end R.05 opens with wave 1.)*

      *(Wave 2 dispatched 2026-08-11 as six concurrent agents against the frozen contract, per `A.46`.
      Landed and gate-green so far: `1.26` bulk/block deals, plus `1.09`/`1.24` files present and being
      integrated. Integration is serial with the full gate between each; nothing is ticked until it
      certifies through the conformance suite.)*

      *(DAILY OPERATIONS RUNNER built 2026-08-11 (`A.60`): `scripts/run_daily_operations.py` is the
      operational spine every `L0` engine was built to be used by. It refreshes the Kite session and
      verifies a client builds from it, refreshes the instrument master, runs all NINE ingest adapters
      through the shared core with the discovery memo, classifies gaps and backfills a BOUNDED number,
      escalates dates failing 3x (`R.21`), and reports universe / corporate-action / bar-store state.
      Every step guarded so one blocked endpoint cannot stop the rest; exit code reflects failure so a
      scheduler can tell. **Orphans 26 -> 0, healthy 21% -> 40%** on `/wall`. This also closes the
      "nothing schedules any ingest run" blocker — the three rolling sources accrue forward only, so
      every day it does not run is unrecoverable history.)*

**— L1 —**

- [~] **1.35** NSE transaction-cost engine — `L1.01` · every statutory and broker levy on an NSE trade,
      priced POINT-IN-TIME out of `L0.31`'s bitemporal store and REFUSED where the era is uncompiled.
      Eight chargeable segments (cash delivery/intraday, equity futures/options, currency, commodity),
      nine components, exact `Decimal` paise, evidence grade carried through to the answer. Three
      genuine solves rather than a fee calculator: a closed-form piecewise-linear BREAKEVEN root
      (self-referential — the exit price sets the sell-side levies that set the breakeven), an integer
      solve for the minimum viable quantity over the brokerage step function, and a reconciliation
      LEDGER that measures the engine's own error against real contract notes and refuses to turn a
      residual into a correction factor. Spec `docs/research/219`; 82 tests; wired as a daily-runner
      step and surfaced at `/costs`. **Found and fixed three silent defects in facts seeded the same
      morning** (`A.90`): an options exchange charge that was really a cash rate (~12x low), a cash
      stamp duty scoped to all cash but carrying the delivery-only rate (5x high intraday), and cash
      STT missing entirely — plus a latent `coverage()` crash in `L0.31` that the new facts triggered.
      **Corrected the corpus twice:** the SEBI turnover fee is charged on NOTIONAL, not premium
      (`research/164` and `b28` both wrong), and exercised-option STT is 0.125% until 2026-04-01 with
      the intrinsic-value basis dating from 2019, not 2024. R.05 passed on the real tape: 3,416 real
      symbols priced, and the option breakeven steps on the real statutory dates and is flat between
      them. `[~]` not `[x]` per `R.11` — the primary consumer, `L1.02`'s gate, is still queued
- [x] **1.36** Pre-trade cost gate — `L1.02` — *IN PROGRESS, built with `1.37` as ONE decision:
      "does this clear cost" and "does it clear cost by enough" are the same comparison against
      different hurdles, and splitting them would ship a gate whose answer the next entry
      immediately replaces.*
- [x] **1.37** Net-EV gate — `L1.03` · built inside `cost_gate/pre_trade_cost_gate.py` with `1.36`,
      because "does this clear cost" and "does it clear cost by ENOUGH" are the same comparison against
      different hurdles. The plan's `edge > cost x 1.5-2` is implemented WITHOUT a multiplier: the
      required hurdle is statutory cost plus the PESSIMISTIC end of the measured execution interval, so
      the margin is the uncertainty itself and scales with it — 1.2 bps at 500 units, 18.1 bps at 5,000,
      measured. `GateDecision.net_edge_bps` and `.shortfall_bps` are the net-EV outputs. *Closes with
      F01.*

      *(Both inside feature **THE COST REALITY FILTER** — `A.93`. Nothing here ticks until a real
      signal on real data is actually vetoed or resized by real modelled cost, visible on
      `/costs`.)*

      *Built: `cost_gate/priced_signal.py` — the contract the whole tree was missing. Every gate
      above `L1` needs expected edge in bps, and the only strategy module emits action +
      conviction + a unitless deviation. `PricedSignal` demands an edge AND an `EdgeBasis` naming
      how it was arrived at, so the claim stays attributable and falsifiable; the mean-reversion
      adapter is labelled `STRATEGY_HYPOTHESIS`, not fact. `cost_gate/pre_trade_cost_gate.py` —
      PASS / RESIZE / VETO / UNPRICEABLE over the two cost engines. 17 tests.*

      *Three decisions worth finding again later: (1) **no 1.5x multiplier anywhere.** `R.03`
      forbids the magic number and `A.12` calls it a prior, so the gate requires edge to clear the
      PESSIMISTIC end of the measured cost interval instead — the margin comes free and scales
      itself (measured: 1.2 bps of uncertainty at 500 units, **18.1 bps at 5,000**, one rule, no
      constant that has to be wrong for one of them). (2) **`UNPRICEABLE` is not `VETO`** — a veto
      is a judgement, unpriceable is the absence of one, and conflating them turns a data outage
      into a flood of confident rejections. Pre-2024 dates come back unpriceable, since `L0.31`
      refuses the slab era. (3) **RESIZE is a solve**, bisecting the same engines for the largest
      size that still clears, so a resized signal has been priced rather than estimated.*

      *WIRED (`R.06` closed on the gate): `cost_gate/cost_gated_mean_reversion_entries.py` runs
      the full path — strategy decision, priced claim, modelled hurdle, verdict. `L5.05` gained
      `rolling_dispersion()`, completing its interface: its deviation is scale-free by design and
      therefore unpriceable until multiplied back by the dispersion it is measured in. The regime
      veto runs BEFORE costing, asserted by test, so an extreme deviation in a trending tape
      cannot talk its way past the brain by being cheap. 11 further tests.*

      *First real-tape run of the whole path, 120 instruments: 103 produced no signal, and **all
      17 that did were VETOED** — claimed edges 0.0-3.1 bps against hurdles of 13-68 bps. The gate
      changes behaviour, which is the whole point of `L1`. **Read `O.74` before drawing a
      conclusion about the strategy:** the engine was fed depth-tape mids seconds apart rather
      than the bars it was designed for, so this tests the plumbing on the wrong timescale and is
      NOT evidence that mean reversion lacks edge.*

      *`L1.04` floor DONE and derived on the real universe; `/costs` surface DONE and now FED by
      the daily runner (`R.08`) — the nightly run logs real verdicts, 36 pass / 11 resize / 13
      veto / 0 unpriceable over 60 real books; floor derivation wired as a daily-runner step
      (`R.06`).*

      *ADVERSARIAL REVIEW DONE (`A.98`), run before the execution gate rather than after —
      **5 critical, 9 major**. All five criticals fixed and regression-tested: a resize that
      refused real Rs 2.6-20 lakh tickets while stating "not tradeable at any quantity" as the
      reason (the hurdle is U-shaped, not monotone — 35% of instruments); the half-spread counted
      twice (median 1.52x overstatement on 2,390 real books); one ladder priced twice instead of
      two ladders once (25.8% of books understated by >10%); a silent 100x unit error that flipped
      veto to pass; and an inverting exponent range timed to fire exactly when `R.04`'s ladder
      advanced. **Four of the five were protected by a passing test that asserted the defect.**
      The nine majors are recorded in `BACKLOG.md` with their measurements.*

      ***`R.05` DONE 2026-08-12, and it changed the feature.*** *Fed real daily closes from the
      `L0.34` archive, the median claimed edge came out at **4.81 bps** — barely above the
      seconds-scale run. By `O.74`'s own criterion that was a finding about the strategy. It was
      not. Decomposing first showed the defect was `edge_from_mean_reversion_decision`: it valued
      the move as the deviation's excess beyond its own entry band, and the band IS the 90th
      percentile of that distribution, so the excess is small by construction. An unstated **exit
      rule** inside a valuation formula — the `R.03` defect, not a market hypothesis.*

      *Replaced by `L1.16`, fitted to **150,364 real reversion events across 379 symbols**,
      strictly causally. Measured: **34.1 bps at 1 bar (t 5.42), 40.4 at 5 (t 5.69), 27.7 at 10
      (t 2.66)** — the family clears the 8.9-bps `NSE-MIS` floor by ~4x, and the old formula had
      understated it ~7x. Capture is **not monotone in depth and changes sign** (3.5σ CONTINUES
      rather than reverts at short horizons), so any formula linear in depth is wrong in sign
      somewhere. `O.74` corrected in place, original left visible; `O.75` records the method
      lesson; `O.76` records why I would not size on this yet. `M10`-`M12` logged.*

      ***F01 CLOSED.*** *All four completion criteria pass; 15 entries tick together.*
- [x] **1.38** Per-segment minimum-edge floor — `L1.04` · built in
      `cost_gate/per_segment_edge_floor.py`, DERIVED from the real hurdle distribution rather than
      typed: the plan's ~6-8 / ~10-11 / ~25-30 bps figures appear nowhere in the code, and the floor
      moves when the market, the statutory rates or liquidity move, with nobody editing a number.
      Point-in-time, so a replay screens with the floor that was true then. Refuses a segment with
      too few priced instruments — a floor from three is not a property of a segment, and a
      screening bound built from noise rejects real trades invisibly, because a rejected signal
      leaves no trace. 10 tests. *Closes with F01.*

      *`R.05` DERIVED ON THE REAL UNIVERSE, 200 instruments, session 2026-08-11:*
      *`NSE-MIS` floor **9.3 bps** (cheapest 5.5, median 23.1, dearest 351.9);*
      *`NSE-CNC` floor **26.4 bps** (cheapest 23.8, median 36.8, dearest 363.6).*
      *The plan predicted ~6-8 bps for large cash and the measured CHEAPEST is 5.5, so its
      optimistic end was right — but the measured MEDIAN is 23.1, roughly triple. The gap is the
      execution cost the plan's figures omitted, which is precisely what `L1.05`/`L1.06` exist to
      supply. Delivery sits ~17 bps above intraday, which is the double-sided STT plus the
      per-scrip depository debit showing up as a number rather than an argument.*
- [x] **1.39** Fill / slippage model — `L1.05` — *built as ONE engine with
      `1.40` per `A.92`: with a real five-level book the size-dependence IS the calculation, so
      splitting them would ship a size-blind model the second entry replaces wholesale.*
- [x] **1.40** Market-impact fill model — `L1.06` · built inside
      `execution_fill/market_impact_estimator.py` with `1.39` (`A.92`). Size-dependent by construction:
      the square-root curve is ANCHORED to the book-walk's own measured cost at visible depth, so the
      coefficient is derived from the instrument's own data and sigma cancels — no literature constant
      is imported. The exponent stays uncertain because this tape cannot fit it (measured R-squared
      0.010-0.070), so the output is an interval that widens with extrapolation and has width exactly
      zero at the anchor. *Closes with F01.*

      *(Both sit inside the feature **THE COST REALITY FILTER** — `A.93` — with `L1.01` (done),
      `L1.02`, `L1.03`, `L1.04` and the minimal priced-signal contract the gate needs. The feature
      lands when a real signal on real data is actually vetoed or resized by real modelled cost,
      visible on `/costs`. No part of it ticks on its own: an entry is a unit of description, and
      the deliverable is the capability.*

      *Built so far, spec `docs/research/220`, 47 tests green: `order_book_walk_calculator`
      (NSE's own impact-cost formula, with the CENSORING boundary made explicit),
      `quoted_spread_observer` (deliberately unpooled — measured: shrinkage made the spread
      estimate worse at every sample size down to n=5), `instrument_liquidity_buckets` (turnover
      deciles x tick regime, derived from the distribution, never rupee thresholds), and
      `market_impact_estimator` (the square-root curve ANCHORED to the book-walk's own cost at
      visible depth, so the coefficient is derived from this instrument's data rather than
      imported from a paper, and `sigma` cancels).*

      *Measured on the real tape and driving the design: the visible five levels are a median
      **0.3%** of the resting book; at 1e-3 of session volume **82.4%** of snapshots exhaust
      them; and the walk SATURATES as it does — reproduced live at 5.527 bps for both 1,000 and
      100,000 units. A naive book-walk reports a comfortable number exactly where cost runs away.*

      *Added since: `execution_fill_model` (the two terms joined, output as an INTERVAL with the
      pessimistic end marked as the one a gate must refuse on) and `execution_fill_parameter_store`
      (carried state — per-instrument spread profiles read point-in-time, per-bucket exponents that
      start at the published range and narrow only as fills accrue, and the realised-fill table
      which is real and honestly EMPTY because nothing has traded). 58 tests.*

      *Measured on the real tape, and this is the engine's whole thesis in one line: for a real
      instrument the naive book-walk reports **5.527 bps at 1,000 units and 5.527 bps at 100,000**
      — a hundredfold size increase for the same cost — while the anchored curve gives 14.8 bps
      with a [9.3, 23.9] interval. On an illiquid name it gives 149.9 bps with [69.1, 331.0],
      which is the correct way to say "this is a bad idea and I do not know how bad".*

      *Still to build in this feature: `L1.03` net-EV, `L1.02` gate, `L1.04` floor, the
      priced-signal contract, wiring, `/costs`, then the adversarial review and the `R.05`
      real-data pass.*
- [ ] **1.41** Realized-vs-modelled slippage tracker — `L1.07`
- [x] **1.42** STT options-sell rate change (0.15% from Apr 2026) + ITM auto-exercise STT trap — `L1.08`
      · **delivered by `L1.01` and reconciled here (`A.97`)**, not built separately. Options STT is seeded
      0.0005 -> 0.000625 -> 0.001 -> **0.0015 from 2026-04-01** with the boundary tested in both
      directions; the exercise trap is modelled as a distinct taxable EVENT — 0.125% of INTRINSIC value
      until 2026-04-01 then 0.15%, on the PURCHASER, with the basis dating from 2019 rather than 2024.
      Pricing an exercise under the pre-2019 full-notional rule is the 121x error that makes a system
      irrationally afraid of holding to expiry, and there is a test for it.
- [x] **1.46b** Reversion edge calibration — `L1.16` · **NEW, not in the 503-entry catalogue.**
      Inserted at F01's dependency position because the `R.05` pass proved the edge could not be
      derived from a decision alone: the old formula encoded an unstated exit rule, and no
      recalibration of a linear coefficient repairs a relationship that changes sign. Built in
      `cost_gate/mean_reversion_edge_calibrator.py` + `reversion_calibration_fitter.py`: a strictly
      causal walk of the deep-history archive, bucketed by deviation depth and horizon, empirical-
      Bayes shrunk toward the pooled estimate, point-in-time, refusing rather than substituting
      where it has no evidence. Re-fits nightly (`R.06`); rendered on `/costs` with CONTINUES /
      NOT SIGNIFICANT / TAIL-CARRIED verdicts (`R.08`). 10 real-data + 28 unit + 5 surface tests.
      *`M10` (optimistic standard errors) is the open caveat and is the finding most likely to
      reverse the conclusion.*
- [ ] **1.43** Discrete option-lot sizing — `L1.09` · **BUILT 2026-08-13** inside `F03`'s first
      slice (`volatility_targeted_position_sizer`): lots read from the live Kite instrument dump,
      rounding always DOWN, zero lots answered with a stated reason. A hardcoded lot size is a
      defect, not a fallback — the archived map went stale (NIFTY 65, not 75). NOT ticked: no
      consumer, no `R.05` pass, no surface.
- [ ] **1.44** Capital-based position sizing — `L1.10` · **BUILT 2026-08-13** inside `F03`'s first
      slice. Volatility-targeted risk budget capped by a shrunk Kelly fraction (`A.104` decision 2);
      `target_risk_fraction = 1 / concurrent_position_capacity`, so no fraction is chosen. First
      real caller `capital_configuration` has ever had, which is what closes todo `0.8` — **once a
      consumer exists**. NOT ticked: see `BACKLOG.md` under `F03`.
      · ⚠️ **now sizes against a capital SOURCE chosen by mode, not a number**: `L1.17` for live,
      `L1.18` for the paper trading book. Both expose the same `deployable_rupees` / `is_tradeable` /
      `binding_side` / `describe()`, so this picks one at construction and never branches per sizing.
- [ ] **1.44b** Paper trading book's capital ledger — `L1.18` · **NEW 2026-08-13 (`A.102`)**, built
      the same day. Event-sourced virtual money for the *trading* paper book: append-only SQLite log,
      balance is a fold verified against its checkpoint on every read, `free = balance − committed`
      with per-position reservations so a second signal cannot size against capital an open position
      holds, operator-editable from `/paper-capital` to any positive figure, over-ceiling and
      over-committed states STAMPED rather than blocked. Spec `docs/research/226`; 42 tests; ruff +
      mypy-strict green; live dashboard round trip passed on the running server.

      *Why it exists:* the operator's real account is in debit (−88), and `A.101` had just made the
      broker-measured resolver the single source of the sizing number — which would have paper-traded
      ₹0 forever. `A.102` scopes that resolver to LIVE. *Why finite when `A.06` says paper is
      unlimited:* two books. The experiment book stays unlimited for `L14.28`'s conversion table; the
      trading book is finite because return, Sharpe, percentage drawdown and Kelly all divide by the
      capital base, and `R.22` graduates on risk-adjusted evidence that does not exist without one.

      ***`R.N` screenshot caught what 11 surface tests could not.*** *The ledger table rendered TWO
      cells per row instead of nine — a line-wrap turned a conditional inside a chain of implicitly
      concatenated f-strings into a ternary over the whole chain. Every test passed, because each
      asserted a substring was PRESENT and a two-cell row still contains it. Presence is not
      structure. Fixed, and `test_every_ledger_row_carries_one_cell_per_column` now counts cells
      (`O.84`).*

      ***`R.23(c)` adversarial review, 2026-08-13 (`A.103`) — two criticals, and both were mine to
      have caught.*** *`commit_to_position` folded, decided and inserted in three separate
      transactions: eight threads reserving Rs 200,000 each against a Rs 1,000,000 book were ALL
      ACCEPTED, 15 trials of 15, cross-process too, with `snapshot()` raising nothing because the
      checkpoint agreed with the corrupted fold. The suite could not have caught it — all 39 tests
      were single-threaded, and the spec's own section 7 listed "a commit racing an edit" as
      required. The second: the fold resolved a duplicate commit key by last-write-wins, deleting a
      live reservation and returning it as free capital. Plus five majors — a refused edit rendered
      the EMPTY state so one typo told the operator their funded book had no capital or history;
      `Decimal('1E+1000000')` committed to the log, answered 303 and bricked every later read; a
      diverged checkpoint refused reads but not writes, so the next edit repaired it silently; two
      edits 211 microseconds apart lost one to a "backdated" refusal; `total_by_kind` overstated a
      loss against a small book nine-fold. And four tautological tests, each proven by mutation —
      including one that checked METHOD NAMES against a verb blocklist and passed a real public
      `erase_history()` running `DELETE FROM paper_capital_event`.*

      *All fixed. Every write is now one `BEGIN IMMEDIATE` spanning fold, decision, insert and
      checkpoint, with a partial unique index behind it. Verified LIVE: eight concurrent commits,
      five accepted, three refused, committed exactly equal to the balance. 55 tests.*

      · ⚠️ **NOT R.11-done — primary consumer queued**: nothing sizes against it yet. `F04`'s paper
      loop is the first real caller; `L1.10` must take the source by mode; `L1.11` is the named
      consumer of `COST_DEBIT` being readable apart from `REALISED_LOSS`. Tick to [x] when one
      consumes it. Commit/release HAS now run live (the `A.103` probe); no realisation has —
      no profit, loss or cost has been written by anything but a test, because nothing simulates a
      fill yet.
- [ ] **1.45** P&L attribution by cost component — `L1.11`
      *(NOT A BUILD UNIT — MERGE -> `L13.15`. One engine + one panel, catalogued twice. Recorded by `A.94`.)*
- [ ] **1.46** Cost homeostasis — `L1.12`
- [ ] **1.47** Tax-lot record (STT/CTT/stamp/GST), exportable — `L1.13`
- [ ] **1.48** Maker-order spread capture — `L1.14`
- [x] **1.49** Dual cost regime — intraday vs delivery — `L1.15` · **delivered by `L1.01` and reconciled
      here (`A.97`)**. `NSE-CNC` and `NSE-MIS` are separate chargeable segments precisely because the two
      are materially different: delivery STT is 0.1% on BOTH legs against 0.025% sell-side intraday, stamp
      duty 0.015% against 0.003%, and delivery alone carries the per-scrip depository debit. A carry
      decision priced with intraday costs is wrong in the unsafe direction, so the segments were split at
      the vocabulary level rather than left as a flag — see `A.90`. Tested on both legs.

# PHASE 2 — VALIDATION, OPS FLOOR AND EXECUTION

*L2 search integrity + L3 ops floor + L9 execution + core risk. The gate every instruction must pass, and the floor where losses actually occur.*


**— L2 —**

- [ ] **2.1** Honest trial registry — `L2.01`
- [ ] **2.2** Holdout custodian — `L2.02`
- [ ] **2.3** Deflated Sharpe Ratio as in-loop fitness — `L2.03`
- [ ] **2.4** Combinatorial Purged Cross-Validation (CPCV) — `L2.04`
- [ ] **2.5** Minimum Backtest Length (MinBTL) hard gate — `L2.05`
- [ ] **2.6** Probability of Backtest Overfitting (PBO / CSCV) — `L2.06`
- [ ] **2.7** Benjamini-Yekutieli FDR control — `L2.07`
- [ ] **2.8** Effective-trials estimator — `L2.08`
- [ ] **2.9** Purged + embargoed walk-forward CV per strategy family — `L2.09`
- [ ] **2.10** Triple-barrier labelling + sample uniqueness — `L2.10`
- [ ] **2.11** Mechanism declaration — `L2.11`
- [ ] **2.12** Regime-coverage gate — `L2.12`
- [ ] **2.13** Multi-strategy validated promotion pipeline — `L2.13`
- [ ] **2.14** Strategy trial registry store — `L2.14`
- [ ] **2.15** Strategy-family promotion registry — `L2.15`
- [ ] **2.16** Hansen SPA test at the promotion gate — `L2.16`
- [ ] **2.17** White's Reality Check / bootstrap-vs-random-walk null for pattern candidates — `L2.17`
- [ ] **2.18** Bootstrapped max-drawdown distribution — `L2.18`
- [ ] **2.19** Proper scoring rules — `L2.19`
- [ ] **2.20** Brier decomposition — `L2.20`
- [ ] **2.21** Mechanism recalibration — `L2.21`
- [ ] **2.22** Prequential forecast scorer — `L2.22`
- [ ] **2.23** Prediction-labelled trade tables — `L2.23`
- [ ] **2.24** Minimum-backtest-length + holdout custodian wiring into the live loop — `L2.24`
      *(NOT A BUILD UNIT — MERGE -> `L2.02` + `L2.05`. Wiring into the live loop, not a second custodian. Recorded by `A.94`.)*
- [ ] **2.25** Skill-vs-luck court — `L2.25`
- [ ] **2.26** Random-control arm — `L2.26`
- [ ] **2.27** Shadow-rejected arm — `L2.27`
- [ ] **2.28** Per-trade pre-mortem — `L2.28`
- [ ] **2.29** World-model scoreboard + profit provenance — `L2.29`
- [ ] **2.30** Holdout custodian + minimum-backtest-length for the *option* families specifically — `L2.30`
      *(NOT A BUILD UNIT — MERGE -> `L2.02` + `L2.05`. Parameterisation for option families. Recorded by `A.94`.)*
- [ ] **2.31** Verification cockpit — `L2.31`
- [x] **2.32** Execution-grounded quality gates — `L2.32` — *DUPLICATE of `0.7`, which is already
      satisfied by the Stop hook running ruff + mypy + pytest. Ticked to match (`A.94`).*

**— L3 —**

- [x] **2.33** Idempotent client order IDs — `L3.01`
- [x] **2.34** Order-intent write-ahead log — `L3.02`
- [x] **2.35** Broker-truth state reconciler — `L3.03`
- [x] **2.36** Crash-safe order placer — `L3.04`
- [ ] **2.37** Pre-trade risk gate — `L3.05`
- [x] **2.38** Order rate limiter — `L3.06`
- [x] **2.39** Kill switch / trading control config — `L3.07`
- [ ] **2.40** Corrigibility off-switch — `L3.08`
- [ ] **2.41** Intraday square-off executor — `L3.09`
- [x] **2.42** Daily Kite token auto-refresh via TOTP — `L3.10` · `broker_sessions/kite_totp_auto_login.py`; 7 tests through the `http_session` DI seam — redirect-token extraction without visiting the callback, relative-`Location` resolution, redirect-loop termination, both named step failures, and proof the posted `twofa_value` is derived from the configured secret (`A.76`)
- [x] **2.43** Kite access-token store + authenticated client builder — `L3.11` · 6:00-IST expiry proven at the boundary and across timezones, corrupt-file behaviour documented as deliberately loud, plus the **R.05 test that builds a client from the stored token and calls `profile()`** — the one the Angel path lacked (`A.74`, `O.52`)
- [x] **2.44** Broker credential loader — `L3.12` · 18 tests: blank-vs-absent, whitespace stripping, secret masking, absent-secret-prints-`None`, and a guard that every `BrokerName` has an env-var prefix (`A.76`)
- [x] **2.45** Angel One SmartAPI session (TOTP via pyotp) — `L3.13` · session cached per exchange day, tokens owner-only and repr-masked; two defects caught only by the real-data pass — the `"Bearer "` prefix that made every cached client a silent `AG8001`, and a wrong throttling diagnosis that was really an unloaded `.env` (`A.74`). 20 tests incl. an R.05 rehydrate-and-`getProfile` call
- [ ] **2.46** Breeze session-token store + builder — `L3.14`
- [ ] **2.47** Atomic multi-leg executor — `L3.15`
- [x] **2.48** Partial-fill tracking loop — `L3.16`
- [ ] **2.49** Cross-strategy netting — `L3.17`
- [ ] **2.50** Signal-expiry / TIF discipline — `L3.18`
- [x] **2.51** SEBI Algo-ID tagging on every order + audit trail — `L3.19`
- [ ] **2.52** Rate-limit budgeter — `L3.20`
- [ ] **2.53** Cold-start behaviour — `L3.21`
- [ ] **2.54** Disaster-recovery runbook — `L3.22`
- [ ] **2.55** Tiered alerting (page / notify / log) — `L3.23`
- [ ] **2.56** Structured audit log of every decision — `L3.24`
- [ ] **2.57** Blue-green deploy + config versioning and rollback — `L3.25`
- [ ] **2.58** Safety-incident forensic store — `L3.26`
- [~] **2.59a** SCHEDULED DAILY OPERATIONS — systemd user timer — `L3.28` — *built 2026-08-11 (`A.61`).
      Fires twice per session day: 19:00 IST (same-day bhavcopy + next day's ban list) and 08:15 IST
      (overnight MWPL). A **USER** unit with linger, because SELinux denies `init_t` reading
      `user_home_t` — a system unit failed 203/EXEC and could not execute the venv at all. Logs to a
      FILE as well as journald, because `journalctl --user` captures nothing from user units on this
      host. `Persistent=true` (missed firings run on boot — rolling sources are unrecoverable) and
      `RandomizedDelaySec=600`. Enabled and active; next firing verified. `[~]` until a full run has
      been observed end to end.*
- [x] **2.59** Systemd service management for the dashboard — `L3.27` · user unit + linger, `Restart=always`, crash-recovery verified by `kill -9` (`A.62`)
- [ ] **2.60** Kite-decoupled architecture guard — `L3.29` — *renumbered from `L3.28`, which the
      catalogue used for TWO different entries: this guard and the systemd timer at `2.59a`. Any
      audit keyed on the ID reported one of them as done when only the other was (`A.94`).*

**— L7 —**

- [ ] **2.61** Risk-based position sizer — `L7.01`
- [ ] **2.62** Max position / order / rate / price-collar / max-leverage limits — `L7.02`
- [ ] **2.63** Max daily loss + drawdown kill — `L7.03`
- [ ] **2.64** Graduated drawdown ladder (−5 / −10 / −15%) — `L7.04`
- [ ] **2.65** Per-symbol and aggregate exposure with correlation-aware limits — `L7.05`
- [ ] **2.66** MWPL / F&O-ban / position-limit guard, with hysteresis — `L7.06`
- [ ] **2.67** Circuit-limit-aware order rejection — `L7.07`
- [ ] **2.68** Liquidation / margin-shortfall monitor — `L7.08`
- [ ] **2.69** Correlation-breakdown breaker — `L7.09`
- [x] **2.70** Kill switch as a separate watchdog process, with reconciliation on restart — `L7.10`
- [ ] **2.71** CVaR / tail-risk with stress scenarios — `L7.11`
- [ ] **2.72** Real-time portfolio VaR including Greeks — `L7.12`
- [ ] **2.73** Pre-trade cost gate as a risk control (see L1.02) — `L7.13`
      *(NOT A BUILD UNIT — DROP -> `L1.02`. The plan's own text says "(see L1.02)". Not a second gate. Recorded by `A.94`.)*
- [ ] **2.74** Indian trading cost model integrated into the risk decision — `L7.14`
      *(NOT A BUILD UNIT — DROP -> `L1.01` + `L3.05`. "Cost model integrated into the risk decision" is wiring. Recorded by `A.94`.)*

**— L9 —**

- [x] **2.75** Kite broker client + order placement — `L9.01`
- [x] **2.76** Full Kite order-type taxonomy — `L9.02`
- [x] **2.77** Paper/live execution parity — `L9.03`

      *(All of `2.33`-`2.36`, `2.38`, `2.39`, `2.48`, `2.51`, `2.70`, `2.75`-`2.77` and the new
      `2.77a` are feature **F02 — THE ORDER PATH**, opened 2026-08-13 (`A.99`), IN PROGRESS. They
      tick together and not before, per `A.93`: an intent becoming exactly one order, surviving a
      crash, and the broker believed over local state is ONE capability, and any part of it shipped
      alone is a fragment whose consumer does not exist. Checklist in `docs/FEATURE_MAP.md`.)*
- [x] **2.77a** Order-expression selector — `L9.14` · **NEW 2026-08-13 (`A.99`)**, inserted at its
      dependency position inside F02: it consumes the `L9.02` taxonomy and the `L1.05`/`L1.06`
      impact engine, and produces the order `L3.04` places. Chooses variety, product, order type,
      validity and any iceberg split from the intent's urgency, the ticket against visible depth,
      the segment's cost regime and the live spread. Not `L9.05` (routes a decided order), not
      `L9.07` (a quantity decision).

      ***`R.23(c)` adversarial review, 2026-08-13 — it broke all three of F02's claims.*** *Five
      criticals and four majors, every one REPRODUCED end to end before it was reported, behind a
      green suite of 367 tests. The pattern in four of the five was one thing: an exceptional answer
      quietly turned into an ordinary one. A part-filled order that was CANCELLED made the journal
      unreadable forever (the fold replayed events then fills); a dropped connection classified as
      "never sent" sent ONE decision to the venue THREE times; an inference was never withdrawn when
      the real trades arrived, running the position 30 units past the order; an unparseable payload
      read as "no orders" and wrote off a possibly-live order as ABANDONED; and the commonest state
      in an intraday book — part-filled, still OPEN — was reported as a broker disagreement on every
      pass. All fixed with 14 regression tests (`a10c13c`).*

      ***The most valuable finding was about the tests.*** *Two asserted a defect outright, one
      tested the only ordering where it could not fail, and the flagship property test's fake could
      not reach the states where the defects lived. Widened, it found two more on its first run —
      both clock skew between the exchange's stamp and this host's. F02 does not tick until the
      four remaining majors are closed.*
- [ ] **2.78** Realistic options fills — `L9.04`
- [ ] **2.79** Cost-aware maker/taker and segment routing — `L9.05`
- [ ] **2.80** Per-order slippage budget with abort — `L9.06`
- [ ] **2.81** Impact-aware order slicing, only where the clip exceeds available liquidity — `L9.07`
- [ ] **2.82** Smart order routing across brokers — `L9.08`
- [ ] **2.83** Reinforcement-learning execution agent — `L9.09`
- [ ] **2.84** Broker state reconciler in the execution path — `L9.10`
      *(NOT A BUILD UNIT — MERGE -> `L3.03`. "...in the execution path" is placement, not a component. Recorded by `A.94`.)*
- [ ] **2.85** Order-intent WAL in the execution path — `L9.11`
      *(NOT A BUILD UNIT — MERGE -> `L3.02`. Same. Recorded by `A.94`.)*
- [ ] **2.86** MIS → CNC position conversion — `L9.12`
- [ ] **2.87** T+1 settlement awareness — `L9.13`

**— L12 —**

- [ ] **2.88** SEBI Feb-2025 retail-algo framework compliance — `L12.14`
      *(NOT A BUILD UNIT — MERGE -> `L3.06` + `L3.19` + `L9.01`. Compliance here is the union of the
      rate limiter, Algo-ID tagging and broker-as-principal routing; no separate module exists.
      Recorded by `A.94`.)*

# PHASE 3 — THE FIRST HOLON (cash-intraday, whole)

*L4 signals + L5 cash strategies + the L14 bot contracts + the three directional organs + the instruction engine + overnight carry. One bot, complete, to paper.*


**— L4 —**

- [ ] **3.1** Full indicator library via TA-Lib (150+ functions) — `L4.01`
- [ ] **3.2** `pandas-ta-classic` fallback — `L4.02`
- [ ] **3.3** Correlation-prune → feature-importance → effective-trials selection pipeline — `L4.03`
- [ ] **3.4** Candlestick pattern library — `L4.04`
- [ ] **3.5** Chart-pattern library — `L4.05`
- [ ] **3.6** Average Directional Index + trend-strength gauge — `L4.06`
- [ ] **3.7** Session strategy regime gate — `L4.07` — *title repaired; the generator emitted the
      literal word "Name" (`A.94`).*
- [ ] **3.8** VPIN order-flow toxicity (Bulk Volume Classification) — `L4.08`
- [x] **3.9** Order-flow imbalance (depth-weighted, never L1) — `L4.09` · multi-level
      Cont-Kukanov-Stoikov OFI, already implemented inside `order_book_snapshot_replay_engine` and
      surfaced at `/microstructure`. *Tick reconciled by `A.94` — do not rebuild.*
- [x] **3.10** Microprice (Stoikov) — `L4.10` · `micro_price_paise()` in
      `order_book_snapshot_replay_engine`, consumed by `quoted_spread_observer` (`L1.05`).
      *Tick reconciled by `A.94` — do not rebuild.*
- [ ] **3.11** Kyle's lambda — `L4.11`
- [ ] **3.12** Volume profile — `L4.12`
- [ ] **3.13** Market breadth — `L4.13`
- [ ] **3.14** Cross-market context — `L4.14`
- [ ] **3.15** Realized volatility, multi-horizon, HAR-RV — `L4.15`
- [ ] **3.16** Fractional differentiation — `L4.16`
- [ ] **3.17** Option-chain features — `L4.17`
- [ ] **3.18** India VIX regime features — `L4.18`
- [ ] **3.19** Cash-futures basis / calendar-roll carry — `L4.19`
- [ ] **3.20** Cross-sectional ranking across the F&O universe — `L4.20`
- [ ] **3.21** Expiry-day, day-of-week and event seasonality features — `L4.21`
- [ ] **3.22** Absorption detection — `L4.22`
- [ ] **3.23** Learned representations — `L4.23`
- [ ] **3.24** Automated feature/indicator discovery — `L4.24`
- [ ] **3.25** Participant-wise open interest (FII / DII / client / pro) — `L4.25`
- [ ] **3.26** FII/DII daily cash flows — `L4.26`
- [ ] **3.27** Streaming incremental indicator state — `L4.27`
- [ ] **3.28** Robust streaming anomaly stack — `L4.28`
- [ ] **3.29** NSE intraday scanner/filter taxonomy — `L4.29`
- [ ] **3.30** Time-of-day session playbook — `L4.30`
- [ ] **3.31** Stock symbol ↔ name gazetteer + headline symbol matching — `L4.31`
- [ ] **3.32** Trade-log data schema — `L4.32`

**— L5 —**

- [ ] **3.33** Opening-range breakout (ORB) + variants — `L5.01`
- [ ] **3.34** ORB champion-challenger auto-tuning — `L5.02`
- [ ] **3.35** Scheduled champion-challenger auto-re-evaluation — `L5.03`
- [ ] **3.36** Per-market-regime champion — `L5.04`
- [~] **3.37** Intraday mean-reversion family — `L5.05` · `strategy/intraday_mean_reversion_engine.py`
      exists and is consumed by the `/regime` read model. `[~]` not `[x]`: it emits action + conviction
      with NO expected edge, so it cannot yet clear a cost gate — completed inside feature **F01**,
      which gives it a priced signal. *Reconciled by `A.94`.*
- [ ] **3.38** VWAP reversion (±2σ, skipped when ADX > 25) — `L5.06`
- [ ] **3.39** VWAP trend / pullback — `L5.07`
- [ ] **3.40** Momentum / relative-strength ranking — `L5.08`
- [ ] **3.41** Gap fade (>8%, low-vol) and gap-and-go (vol 140%+) — `L5.09`
- [ ] **3.42** Range / pivot / CPR breakout, prior-day high-low — `L5.10`
- [ ] **3.43** Relative-volume surge (3–10×) — `L5.11`
- [ ] **3.44** Momentum ignition detection — `L5.12`
- [ ] **3.45** NR7 / Bollinger-squeeze breakout — `L5.13`
- [ ] **3.46** Connors RSI(2) mean-reversion — `L5.14`
- [ ] **3.47** Pairs / statistical arbitrage — `L5.15`
- [ ] **3.48** Cross-sectional statistical arbitrage — `L5.16`
- [ ] **3.49** Event-driven family — `L5.17`
- [ ] **3.50** Sector rotation — `L5.18`
- [ ] **3.51** The four regime engines as one committed set — `L5.19`
      *(NOT A BUILD UNIT — MERGE -> F11/F12. `A.08`'s arming policy, already in the brain's arm/disarm. Recorded by `A.94`.)*
- [ ] **3.52** Intraday tradable cash-universe filter — `L5.20`
- [ ] **3.53** Cash focus set — derived from the cost floor, capped by executable capacity; no fixed N — `L5.21`
- [ ] **3.53a** Cash filter families, all four (activity · volatility+range · extremes+events · relative-strength+structure) — `L5.21a`
- [ ] **3.53b** Filter-combination proving ground — all 15 combinations traded as hypotheses, winner becomes default, gated by the effective-trials estimator — `L5.21a`, `L2.08`
- [ ] **3.53c** Watch-everything scoped to the five DERIVATIVE segments; cash keeps its focus set — `L5.21b`
- [ ] **3.53d** Per-segment observation tiers — index options: every strike, nearest 2 expiries, all 5 underlyings (~2,000) · index futures: all 15 · stock futures: all 622 · stock options: top ~30 streamed + tail snapshotted · MCX: near-month streamed. Total ~6,100, one Kite key — `L5.21c`
- [ ] **3.53e** Moneyness watch window from the measurement: volume peaks 0.5–1% OTM (41.27%) not ATM (15.57%); OI peaks 2–5% out. ATM ±2% = 80.42% of volume, ±5% = 95.02% — `L5.21d`
- [ ] **3.53f** Recompute observation tiers from a trailing window, never a single session — `r/201` §5
- [ ] **3.53g** ⚠️ Verify MCX liquidity against real MCX data before that holon arms — inferred, not measured — `r/201` §5
- [ ] **3.54** Full-universe opportunity radar — `L5.22`
- [ ] **3.55** Setup/condition library — `L5.23`
- [ ] **3.56** Signal decay TTL — `L5.24`
- [ ] **3.57** Overnight-carry decision engine — `L5.47`
- [ ] **3.58** Next-day return forecast model — `L5.48`
- [ ] **3.59** Carry whitelist + daily expiry — `L5.49`
- [ ] **3.60** Options never carry — `L5.50`
- [ ] **3.61** Carry re-confirmation loop — `L5.51`
- [ ] **3.62** Carry horizon cap + exit ladder — `L5.52`

**— L7 —**

- [ ] **3.63** Overnight gap risk engine — `L7.23`
- [ ] **3.64** Maximum aggregate overnight exposure cap — `L7.24`
- [ ] **3.65** Overnight event guard — `L7.25`
- [ ] **3.66** Corporate-action exposure on held positions — `L7.26`

      *(FOUR-REGIME BRAIN + FIRST CONSUMER built 2026-08-11 — the first decision-path slice since the
      reset, per `research/211`'s finding that a whole day had gone into L0 plumbing while L11 sat
      untouched. `src/nse_algo_trader/regime/` (`L11.01` trend strength with carried Wilder state ·
      `L11.02` Markov-switching with FILTERED beliefs and parameters frozen on a training prefix ·
      volatility against the instrument's own quantiles · `L11.06` session phase · `L11.03` soft
      log-linear pooling with measured Brier reliability and arm/disarm flags per `A.08`) plus
      `src/nse_algo_trader/strategy/intraday_mean_reversion_engine.py` (`L5.05`, per `A.07`) as its
      consumer so the brain is not an orphan. 28 tests. **R.05 on 659,990 REAL bars found a genuine
      design flaw — 100% abstain — fixed and re-verified at 9.1% actionable (`A.59`, `O.42`).** The
      brain CHANGES the decision: identical -3.82σ deviation gives ENTER_LONG in a ranging regime and
      ABSTAIN in a trending one. `[~]`: `L1` costs/sizing unbuilt so output is a decision + conviction,
      never an order (`R.13`); no dashboard surface (`R.08`); reliability weighting starts uniform and
      only becomes meaningful as outcomes accrue (`R.11`).)*

**— L11 —**

- [x] **3.67** Regime classifier — `L11.01` · trend-strength classifier, built 2026-08-11 in
      `regime/trend_strength_regime_classifier.py`; live consumer is the mean-reversion engine and
      the `/regime` surface. *Tick reconciled by `A.94` — it was built and never ticked.*
- [x] **3.68** HMM / Markov-switching regime model — `L11.02` · filtered Markov-switching model in
      `regime/markov_switching_regime_model.py`. *Tick reconciled by `A.94`.*
- [x] **3.69** Soft regime *weighting* rather than hard switching — `L11.03` · soft log-linear pooling
      with arm/disarm in `regime/soft_regime_weighting_brain.py`. *Tick reconciled by `A.94`.*
- [ ] **3.70** Non-stationary bandit router (discounted / sliding-window Thompson) — `L11.04`
- [ ] **3.71** Meta-model over the experiment ledger — `L11.05`
- [x] **3.72** Historical session market-regime classifier — `L11.06` · `regime/market_regime_state.py`
      plus the session-phase classifier. *Tick reconciled by `A.94`.*
- [ ] **3.73** Regime-conditional model bank — `L11.07`
- [ ] **3.74** Qlib DDG-DA drift adaptation as the router reference — `L11.08`
- [ ] **3.75** BULL agent — `L11.09`
- [ ] **3.76** BEAR agent — `L11.10`
- [ ] **3.77** Meta-labelling arbiter — `L11.11`
- [ ] **3.78** Mandatory linear + LightGBM baseline gate — `L11.12`
- [ ] **3.79** Calibration layer (Platt / isotonic / temperature) — `L11.13`
- [ ] **3.80** Online learner with drift detection (river ADWIN/DDM) fed by *realized P&L only* — `L11.14`
- [ ] **3.81** SHAP per-decision evidence store — `L11.15`
- [ ] **3.82** DeepLOB (CNN+LSTM on raw book) as a gated second evidence source — `L11.16`
- [ ] **3.83** Directional verdict wiring into the three segment bots — `L11.17`
- [ ] **3.84** Win-probability engine — `L11.18`
- [ ] **3.85** Playbook instruction engine — `L11.96`
- [ ] **3.86** The instruction object — `L11.97`
- [ ] **3.87** Targets are stated in BASIS POINTS OF THE TRADED INSTRUMENT'S TICKET — `L11.98`
      *(NOT A BUILD UNIT — MERGE -> `L11.106`, which states the same rule more fully. Recorded by `A.94`.)*
- [ ] **3.88** Range-width precondition — `L11.99` · built in
      `cost_gate/tradeable_ticket_preconditions.py`. The plan states it as `range_width > cost x 1.5`
      and `A.12` records the 1.5 as a prior; it is NOT used as a number — the comparison is against the
      required hurdle, which already carries the measured uncertainty margin, so a wide-uncertainty
      instrument automatically needs a wider range without anyone choosing how much wider. *Closes with
      feature F01.*
- [ ] **3.89** Flat-regime dual playbook — `L11.100`
- [ ] **3.90** Mechanism statement mandatory — `L11.101`
- [ ] **3.91** Instructions are hypotheses, validated centrally — `L11.102`
- [ ] **3.92** Instruction decay and retirement — `L11.103`
      *(NOT A BUILD UNIT — MERGE -> `L11.117`. The decay half survives as a lifecycle state. Recorded by `A.94`.)*
- [ ] **3.93** The 24-cell playbook matrix, populated — `L11.104`
- [ ] **3.94** Instruction discovery (ultra) — `L11.105`
- [ ] **3.95** The tradeable-unit denominator rule — `L11.106` · built. Refuses an option signal whose
      reference price is at or above its strike, because that is the UNDERLYING rather than the premium
      and every bps figure derived from it is understated by roughly the ratio between them — in the
      flattering direction. Also absorbs `L11.98`, which states the same rule less fully. *Closes with
      F01.*
- [ ] **3.96** Minimum-ticket precondition (the flat-brokerage gate) — `L11.107` · built, and derived
      rather than floored (`R.03`): the minimum ticket is whatever makes the flat charges bearable
      against THIS signal's own claimed edge, so a signal claiming 500 bps can carry a far smaller ticket
      than one claiming 20. Tested by showing the same ticket pass or fail purely on the claim. Encodes
      the inversion worth remembering — cheap far-OTM options are the WORST scalping vehicle, not the
      safest. *Closes with F01.*
- [ ] **3.97** Live-spread liquidity gate for options — `L11.108` · built, reading the spread from the
      BOOK via `L1.05`'s observer rather than from a model — which is the whole point, since a modelled
      spread cannot tell an illiquid strike from a liquid one and that is the only case where the check
      matters. *Closes with F01.*
- [ ] **3.98** Per-segment play catalog — `L11.109`
- [ ] **3.99** Profit-trail learning gate, one per holon — `L11.125`
- [ ] **3.100** The exit problem stated honestly — `L11.126`
- [ ] **3.101** Trail-policy family, all as candidates rather than a chosen one — `L11.127`
- [ ] **3.102** Learned exit policy — `L11.128`
- [ ] **3.103** The counterfactual advantage of paper — `L11.129`
- [ ] **3.104** Per-segment exit asymmetries — `L11.130`
- [ ] **3.105** Regime-conditional trailing — `L11.131`
- [ ] **3.106** Partial scale-out with a runner — `L11.132`
- [ ] **3.107** Exit quality feeds the instruction track record — `L11.133`
- [ ] **3.108** Offline RL as the frontier form — `L11.134`

**— L14 —**

- [ ] **3.109** Bot anatomy contract — `L14.01`
- [ ] **3.110** Holonic nesting topology — `L14.02`
- [ ] **3.111** Shared-acquisition / owned-interpretation rule — `L14.03`
      *(NOT A BUILD UNIT — MERGE -> `L14.34`, the three-data-tier refinement the plan says supersedes
      the blunt global form. Recorded by `A.94`.)*
- [ ] **3.112** Inter-bot protocol — `L14.04`
- [ ] **3.113** Autonomy ladder — `L14.05`
- [ ] **3.114** CLASS A — `L14.22`
- [ ] **3.115** CLASS B — `L14.23`
- [ ] **3.116** CLASS C — `L14.24`
- [ ] **3.117** TYPES versus INSTANCES — `L14.25`
- [ ] **3.118** Borderline cases, resolved and recorded — `L14.26`
- [ ] **3.119** SIGNED OFF 2026-08-10 — `L14.27`
- [ ] **3.120** Every main bot owns a full internal organ set. A holon is not a strategy wrapper; it is a complete trading… — `L14.33`
- [ ] **3.121** REFINEMENT OF THE SHARING RULE (L14.03) — `L14.34`
- [ ] **3.122** Per-holon online research agent — `L14.35`
- [ ] **3.123** Hypothesis validation stays CENTRAL and shared — `L14.36`
- [ ] **3.124** Per-holon regime readers are legitimately different models. Commodity regimes are driven by inventory… — `L14.37`
- [ ] **3.125** Shared research substrate under the private research agents — `L14.38`

# PHASE 4 — PROVING GROUND AND MARKET-CLOSED SIMULATION

*Where instructions earn the right to exist. PHASE-1 MILESTONE lands here: one instruction GRADUATES.*


**— L5 —**

- [ ] **4.1** Deficit-driven replay curriculum — `L5.39`
- [ ] **4.2** Live universe-wide paper loop — `L5.40`
- [~] **4.3** Historical bar replay source + replay universe feed — `L5.41` — *built 2026-08-15 as
      `F04`'s input half. `paper_session_signal_source.bars_available_at` is the replay source and
      reads `availability_time <= decision_instant` ONLY, so the leakage guard is in the reader
      rather than in every caller; `verify_paper_session_on_real_data.instruments_priced_on` is the
      replay universe feed (every instrument with a lot size AND bars on that exact session).
      `scripts/backfill_five_minute_bars.py` acquires the bars a session needs (`A.109`, `R.16`).
      `[~]` not `[x]`: only the 5-minute interval is sourced, and only from Kite — the multi-source
      windowing `L0.15` describes is not wired in here.*
- [ ] **4.4** Replay-to-live handoff — `L5.42`
- [ ] **4.5** Point-in-time universe + corporate-action adjustment inside replay (§53 slice 2) — `L5.43`
- [ ] **4.6** Prequential learning + provenance-separable memory (§53 slice 3) — `L5.44`
- [ ] **4.7** Session/regime replay curriculum store — `L5.45`
- [ ] **4.8** HFT / latency arbitrage — `L5.46`
      *(**DISPROVED — NEVER BUILD.** `D.02`: proven closed from a retail cloud VM by this project's own
      prior build. Encoded as guard X.A2. Recorded by `A.94`.)*

**— L10 —**

- [ ] **4.9** 24/7 continuous paper-trading loop — `L10.01` — *unblocked 2026-08-15: `4.10` built
      the session this loop would run repeatedly. What is missing is only the scheduler and the
      multi-day state that carries between sessions, not the session itself.*
- [x] **4.10** Market-closed real-market replay engine (§53) — `L10.02` — ***`F04` — the first
      thing in this rebuild that produces a trading DAY rather than a component.*** Built and
      **`R.05`-passed 2026-08-15** on the real 2026-08-11 session, full universe: **2,882
      instruments, 76 decision instants, 218,936 decisions, 45 orders placed and squared off,
      nothing open at the close, ledger fold agreeing with the report, net −Rs 10,824.69 on a
      Rs 10,00,000 book.** Evidence `docs/research/229`. The loop steps point-in-time
      (`availability_time` only), decides with the real regime panel and mean-reversion engine,
      sizes and gates with `F03`, reserves capital in `L1.18`'s ledger BEFORE the order goes, sends
      it through `F02`'s real journal, placer, lifecycle machine and reconciler to a venue that
      fills from the recorded L2 depth tape, and squares off before the close (`R.01`). Spec
      `docs/research/228`; decisions `A.108`–`A.111`; surface `/paper-session`; 35 tests including
      the five adversarial axes §7 names. **Four real defects found by the real-data run, none
      reachable from the hermetic suite** — a negative price collar, a division by `log10(1)`, a
      position marked closed while still filling, and a book served however stale it was.
      **Named open items, none of them this task:** live tick path (`A.108`'s recorded cost),
      options fills (`2.78`/`L9.04`), the continuous scheduler (`4.9`/`L10.01`), and `BACKLOG`
      `M13`–`M17`. **Re-run on 2026-08-12 and 2026-08-13 (2026-08-15)** — every session this
      project holds a depth tape for. All three pass: 146 entries, none open at the close, ledger
      fold agreeing each time, net −Rs 10,824.69 / −Rs 1,202.88 / −Rs 2,160.15. Comparison and what
      it shows about cost versus edge: `docs/research/230`. A fifth real-data defect surfaced and
      was fixed (`A.112`). **Re-run again behind the REAL rate limiter (`A.114`, 2026-08-15)** —
      the loop no longer runs with a null rate gate, so the paper record stops assuming an order
      flow the wire would not have carried. All three sessions still pass, still flat at the close:
      net −Rs 9,962.65 / −Rs 1,022.36 / −Rs 2,082.25. The gate refused 197 of 592 requests and every
      one of them was a SQUARE-OFF rather than an entry (`docs/research/231`, `O.97`).

**— L10 —**

- [ ] **4.9** 24/7 continuous paper-trading loop — `L10.01` — *unblocked 2026-08-15: `4.10` built
      the session this loop would run repeatedly. What is missing is only the scheduler and the
      multi-day state that carries between sessions, not the session itself.*
- [~] **4.10** Market-closed real-market replay engine (§53) — `L10.02` — ***`F04` — the first
      thing in this rebuild that produces a trading DAY rather than a component.*** Built 2026-08-15:
      `paper_loop/paper_trading_session_runner.py` steps a real session point-in-time, decides with
      the real regime panel and mean-reversion engine, sizes and gates with `F03`, reserves capital
      in `L1.18`'s ledger BEFORE the order goes, sends it through `F02`'s real journal, placer,
      lifecycle machine and reconciler to a venue that fills from the recorded L2 depth tape, then
      squares off everything before the close (`R.01`). Spec `docs/research/228`, decisions `A.108`
      + `A.109`, surface `/paper-session`. 20 tests including the five adversarial axes §7 names —
      three real defects found by them, all recorded in `A.109`. **Open before `[x]`:** the `R.05`
      full-session pass on the real tape (blocked until the bar backfill for a depth-tape date
      completes), the live tick path (`A.108`'s recorded cost), options fills (`L9.04`), and the
      continuous scheduler that makes it 24/7 (`4.9`/`L10.01`).
- [ ] **4.11** Non-blocking high-fidelity replay prebuild — `L10.03`
- [ ] **4.12** Autonomous unattended Breeze 1s replay — `L10.04`
- [ ] **4.13** Multi-broker fleet auto-activation in the replay loop — `L10.05`
- [ ] **4.14** The measured post-mortem — `L10.23`
- [ ] **4.15** Fidelity is carried forward unchanged — `L10.24`
- [ ] **4.16** The curriculum decides what is practised — `L10.25`
- [ ] **4.17** Per-strategy and per-holon experience quotas — `L10.26`
- [ ] **4.18** Winners are practised deliberately — `L10.27`
- [ ] **4.19** Simulation feeds the proving ground — `L10.28`
- [ ] **4.20** Every simulated trade is registered as a trial — `L10.29`
- [ ] **4.21** Curriculum diversity, not day repetition — `L10.30`
- [ ] **4.22** Simulation is an `opportunistic`-class conductor consumer — `L10.31`
- [ ] **4.23** Counterfactual perturbation (advanced) — `L10.32`
- [ ] **4.24** Replay-trained calibration as a first-class use — `L10.33`
- [ ] **4.25** Synthetic and adversarial days (ultra) — `L10.34`
- [ ] **4.26** Reactive market (ultra) — `L10.35`

**— L11 —**

- [ ] **4.27** THE INSTRUCTION LIBRARY — `L11.110`
- [ ] **4.28** Highest-mechanism instructions, to build first within each segment — `L11.111`
- [ ] **4.29** Anti-instructions catalogued deliberately — `L11.112`
- [ ] **4.30** Newly-unlocked instructions from this session's decisions — `L11.113`
- [ ] **4.31** Expected outcome, stated up front: MOST OF THESE WILL FAIL VALIDATION. That is the system working, not failing — `L11.114`
- [ ] **4.32** Per-holon instruction proving ground. Each main bot continuously paper-trades its own instruction subset… — `L11.115`
- [ ] **4.33** "REPEATED PROFIT" IS A STATISTICAL CLAIM, NOT A WIN COUNT — `L11.116`
- [ ] **4.34** Instruction lifecycle states — `L11.117`
- [ ] **4.35** Shadow stage between paper and live — `L11.118`
- [ ] **4.36** Paper fills must be realistic or the whole proving ground is theatre — `L11.119`
- [ ] **4.37** Every trial is registered, including the failures — `L11.120`
- [ ] **4.38** Concurrent-trial budget and scheduling — `L11.121`
- [ ] **4.39** Live instructions keep paper-trading in parallel — `L11.122`
- [ ] **4.40** The saved-winners library — `L11.123`
- [ ] **4.41** Cross-holon instruction transfer — `L11.124`
- [ ] **4.42** **Bootstrap calibration, exit priors and regime coverage from the 3,481 retained closed trades** — `A.26`
- [ ] **4.43** ⭐ **MILESTONE — PHASE 1 COMPLETE: one instruction GRADUATES** (sample N · net-of-cost · DSR on effective trials · BY-FDR · regime coverage · holdout)

# PHASE 5 — INTROSPECTION DASHBOARD, CHAT AND THE LLM LANE

*L13 in full + the LLM gateway. Built on decision traces emitted since Phase 3 — panels without traces would be fiction.*


**— L11 —**

- [ ] **5.1** Universal LLM gateway — `L11.60`
- [ ] **5.2** Claude Code subscription provider — `L11.61`
- [ ] **5.3** Warm-persistent subscription client — `L11.62`
- [ ] **5.4** Swappable multi-provider LLM client + provider registry — `L11.63`
- [ ] **5.5** LLM ROUTING LADDER — `L11.64`
- [ ] **5.6** Tier ① Claude Max subscription — `L11.64a`
- [ ] **5.7** Tier ② free-tier cloud fallback — `L11.64b`
- [ ] **5.8** Tier ③ local, last resort and off-market — `L11.64c`
- [ ] **5.9** Cap-aware degradation — `L11.64d`
- [ ] **5.10** Free-tier provider registry + failover order — `L11.65`
- [ ] **5.11** Local Ollama constrained-decoding provider with a think-then-answer contract — `L11.66`
- [ ] **5.12** Subscription token ledger + gateway dashboard surface — `L11.67`

      *(`L13.06`'s audit property is LIVE as `/manifest`: it walks the REAL package tree, so a new
      engine appears automatically and reads UNSURFACED until it has a panel. Measured now: **44 modules,
      7 surfaced, 37 UNSURFACED** — the accrued `R.08` debt is now auditable rather than asserted.)*

**— L13 —**

- [~] **5.13** Dashboard server + read model + HTML renderer — `L13.01` — *built 2026-08-11. There was
      NO dashboard at all after the reset (systemd unit inactive, no web code survived), so every `R.08`
      deferral logged during the day had nothing to attach to. FastAPI + server-rendered HTML.
      **Status is MEASURED, never hand-authored:** the read model instantiates the real classifiers,
      runs them over real bars, and reads armed state off the brain, maturity off each classifier and
      the decision off the mean-reversion engine. A failed measurement returns **503 rather than a
      placeholder** — a green page measuring nothing is exactly what `R.08` forbids. Built through the
      dataviz procedure: palette validated by `validate_palette.js` (PASS both surfaces; the light
      contrast WARN obligated relief, so every bar is direct-labelled), fixed categorical slot order,
      legend + table view + word-bearing status badges, dark mode as its own selected steps. 12 tests.
      `[~]`: only the regime brain has a panel — 37 of 44 modules read UNSURFACED.*
- [ ] **5.14** Feature-surface registry — `L13.02`
- [ ] **5.15** Feature-coverage audit — `L13.03`
- [~] **5.16** Project-wide feature-catalogue dashboard — `L13.04` — *built 2026-08-11. ALL 53 modules
      now surfaced on `/wall`, and none of it hand-typed — which is the point, since 53 hand-written
      panels would be 53 claims that rot invisibly. Every fact DERIVED: reachability from a real `ast`
      import graph walked breadth-first from `scripts/` **and served ASGI apps**, orphan = nothing
      runnable reaches it (`R.06`); test pairing from modules actually imported in the test tree;
      real-data coverage from the `real_data` marker (`R.05`); tier from the module's own vocabulary
      (`R.23b`). Sorted WORST FIRST — alphabetical would hide its own findings. **Measured now: 53
      modules, 21% healthy, 26 orphans, 8 untested, 8 owing a real-data pass.** Token-gated because it
      binds a public interface, and the token is traded for an HttpOnly cookie so it never appears in a
      link — a reflected-XSS defect found by security review, PROVEN exploitable on the no-token path,
      then fixed by deleting the reflection rather than escaping it (`O.43`). 9 tests + 5 XSS regressions.*
- [ ] **5.17** Feature-catalogue AST resolver + live freshness — `L13.05`
- [ ] **5.18** Operations wall (`/wall`) — `L13.06`
- [ ] **5.19** Segment-bot surface prober — `L13.07`
- [ ] **5.20** Pod dashboard service + renderer — `L13.08`
- [ ] **5.21** Persisted closed-trades panel — `L13.09`
- [ ] **5.22** Offline diagnostics mode — `L13.10`
- [ ] **5.23** Performance charts / first real charts — `L13.11`
- [ ] **5.24** AI-atlas concept-tree panel — `L13.12`
- [ ] **5.25** LLM gateway panel — `L13.13`
- [ ] **5.26** System map page (`/map`) — `L13.14`
- [ ] **5.27** P&L attribution by cost component — `L13.15`
- [ ] **5.28** Per-strategy health board + promotion state + cockpit verdict — `L13.16`
- [ ] **5.29** Fill-quality vs assumed — `L13.17`
- [ ] **5.30** Option Greeks / exposure panel — `L13.18`
- [ ] **5.31** Regime + India-VIX panel — `L13.19`
- [ ] **5.32** Latency histograms — `L13.20`
- [ ] **5.33** Replayable decision timeline — `L13.21`
- [ ] **5.34** Live risk heatmap across the F&O universe — `L13.22`
- [ ] **5.35** Conversational chat assistant — `L13.23`
- [ ] **5.36** Gated action layer for the assistant — `L13.24`
- [ ] **5.37** Proactive assistant push — `L13.25`
- [ ] **5.38** JARVIS system view — `L13.26`
- [ ] **5.39** Dashboard delivery stack sourcing — `L13.27`
- [x] **5.40** Screenshot-verify loop — `L13.28` · `dashboard/dashboard_surface_screenshot_capture.py` + `scripts/capture_dashboard_screenshots.py`, tested, and run nightly by `nse-daily-operations`; built across `1a751b2`/`ec5392a`/`9cc592f` without citing this ID, found by the `A.75` audit
- [ ] **5.41** THE DECISION-TRACE CONTRACT — `L13.29`
- [ ] **5.42** Level 0 — `L13.30`
- [ ] **5.43** Level 1 — `L13.31`
- [ ] **5.44** Level 2 — `L13.32`
- [ ] **5.45** Level 3 — `L13.33`
- [ ] **5.46** Form selection — `L13.34`
- [ ] **5.47** Colour rules fixed at design time — `L13.35`
- [ ] **5.48** Project chat panel, LLM-laddered — `L13.36`
- [ ] **5.49** Grounded, never generative about facts — `L13.37`
- [ ] **5.50** Gated action layer — `L13.38`
- [ ] **5.51** Chat security — `L13.39`
- [ ] **5.52** Proactive assistant (ultra) — `L13.40`

# PHASE 6 — WIDEN TO SIX HOLONS

*L6 options/Greeks + L8 portfolio + the five remaining holons + cross-segment netting, portfolio risk and expression selection.*


**— L5 —**

- [ ] **6.1** Segment holons — `L5.25`
- [ ] **6.2** Cash-intraday bot — `L5.26`
- [ ] **6.3** Index-option bot — `L5.27`
- [ ] **6.4** Stock-option bot — `L5.28`
- [ ] **6.5** Segment-bot protocol — `L5.29`
- [ ] **6.6** Pod paper lifecycle engine — `L5.30`
- [ ] **6.7** Trade-quality floor + per-trade evidence card — `L5.31`
- [ ] **6.8** Profit-trail gating + MFE/MAE excursion tracking — `L5.32`
      *(NOT A BUILD UNIT — DROP -> `L11.125`. The plan: "a static gate, now a learning organ". Recorded by `A.94`.)*
- [ ] **6.9** Proportionate entry gates — `L5.33`
- [ ] **6.10** Unobservability must not hard-veto — `L5.34`
- [ ] **6.11** Adaptive arm selector + posterior store — `L5.35`
- [ ] **6.12** Index-options strategy ensemble + adaptive meta-selector — `L5.36`
- [ ] **6.13** Moneyness-varied directional option arm — `L5.37`
- [ ] **6.14** Trending-regime index directional edge arm (A3) — `L5.38`

**— L6 —**

- [ ] **6.15** Black-Scholes implied volatility — `L6.01`
- [ ] **6.16** Full Greeks per position (Δ Γ ν Θ ρ) and portfolio-aggregated — `L6.02`
- [ ] **6.17** IV surface fit — `L6.03`
- [ ] **6.18** Greeks-based pre-trade gate — `L6.04`
- [ ] **6.19** Variance-risk-premium richness engine — `L6.05`
- [ ] **6.20** IV-rank shrinkage estimator — `L6.06`
- [ ] **6.21** Regime → profit-engine map — `L6.07`
      *(NOT A BUILD UNIT — DROP -> `L11.96`, which the plan says supersedes it. Recorded by `A.94`.)*
- [ ] **6.22** Option opportunity scorer — `L6.08`
- [ ] **6.23** Terminal-distribution model — `L6.09`
- [ ] **6.24** Structure payoff optimizer — `L6.10`
- [ ] **6.25** Option liquidity filter — `L6.11`
- [ ] **6.26** Per-leg mid-to-mid P&L marker — `L6.12`
- [ ] **6.27** Self-learning engine re-weighting — `L6.13`
- [ ] **6.28** Option book optimizer — `L6.14`
- [ ] **6.29** Dispersion engine — `L6.15`
- [ ] **6.30** Skew relative value — `L6.16`
- [ ] **6.31** Term-structure relative value — `L6.17`
- [ ] **6.32** Credit-spread live path — `L6.18`
- [ ] **6.33** 0-DTE expiry-day options engine — `L6.19`
- [ ] **6.34** Iron condor / short strangle premium-seller — `L6.20`
- [ ] **6.35** Long straddle / strangle / long gamma — `L6.21`
- [ ] **6.36** Directional verticals — `L6.22`
- [ ] **6.37** Calendar, diagonal, ratio, backspread, butterfly, broken-wing structures — `L6.23`
- [ ] **6.38** Per-underlying expiry selection — `L6.24`
- [ ] **6.39** Option underlying re-look — `L6.25`
- [ ] **6.40** Segment-scoped option mechanism identity — `L6.26`
- [ ] **6.41** Full option universe — `L6.27`
- [ ] **6.42** Live multi-broker option-chain feed — `L6.28`
- [ ] **6.43** Underlying intraday price source — `L6.29`
- [ ] **6.44** SPAN + exposure margin calculator — `L6.30`
- [ ] **6.45** Physical-settlement handling for stock F&O — `L6.31`
- [ ] **6.46** Expiry / pin-risk management — `L6.32`
- [ ] **6.47** Auto delta-hedge scheduler — `L6.33`
- [ ] **6.48** Vega / gamma exposure limits — `L6.34`
- [ ] **6.49** Local-vol / SABR / rough-vol surface calibration — `L6.35`
- [ ] **6.50** American-option pricing for physically-settled stock options with early exercise — `L6.36`
- [ ] **6.51** Stock-option premium selling — `L6.37`
- [ ] **6.52** Confident-loss-aware P&L + assigned-table column on closed trades — `L6.38`
- [ ] **6.53** Implied-correlation computation across index constituents — `L6.39`

**— L7 —**

- [ ] **6.54** Option book risk engine — `L7.15`
- [ ] **6.55** Debate-as-risk-check — `L7.16`
- [ ] **6.56** LLM-risk entry gate at all four entry sites, calibration-gated so it only earns authority as it proves itself — `L7.17`
- [ ] **6.57** Per-trade pre-mortem CVaR sizing consumer — `L7.18`
- [ ] **6.58** Power budgets — `L7.19`
      *(NOT A BUILD UNIT — MERGE -> `L12.12`. The plan writes "(see L7.19)" on it. Build once, under L12. Recorded by `A.94`.)*
- [ ] **6.59** Instrumental-convergence limiter — `L7.20`
      *(NOT A BUILD UNIT — MERGE -> `L12.09`. Same duplicate pair. Recorded by `A.94`.)*
- [ ] **6.60** Scalable oversight — `L7.21`
      *(NOT A BUILD UNIT — MERGE -> `L12.08`. Same duplicate pair. Recorded by `A.94`.)*
- [ ] **6.61** Market-data integrity defense — `L7.22`
      *(NOT A BUILD UNIT — MERGE -> `L12.13`. Same duplicate pair. Recorded by `A.94`.)*

**— L8 —**

- [ ] **6.62** Capital-allocation optimizer — `L8.01`
- [ ] **6.63** Volatility-target sizing as the primary mechanism — `L8.02`
- [ ] **6.64** Fractional-Kelly ceiling — `L8.03`
- [ ] **6.65** Discounted / non-stationary bandit allocator across strategies — `L8.04`
- [ ] **6.66** CVXPY constrained optimizer — `L8.05`
- [ ] **6.67** RMT correlation denoising (Marchenko-Pastur) — `L8.06`
- [ ] **6.68** Hierarchical Risk Parity clustering, with a cap on picks per cluster — `L8.07`
- [ ] **6.69** Mixed-integer knapsack selection under capital, margin and rate constraints, with a greedy value-density… — `L8.08`
- [ ] **6.70** Priority queue with decay TTL — `L8.09`
- [ ] **6.71** Capacity tracking per strategy — `L8.10`
- [ ] **6.72** Bayesian hierarchical alpha (NumPyro) — `L8.11`
- [ ] **6.73** Regime-conditional allocation — `L8.12`
- [ ] **6.74** Meta-strategy allocator — `L8.13`
- [ ] **6.75** Portfolio supervisor over the three segment bots — `L8.14`
      *(NOT A BUILD UNIT — MERGE -> `L14.10` + `L14.21a`. Scoped to a superseded three-bot topology. Recorded by `A.94`.)*
- [ ] **6.76** Multi-objective arbitration — `L8.15`
- [ ] **6.77** Goal-priority scheduler — `L8.16`
- [ ] **6.78** Paper-trading ledger — `L8.17`

**— L14 —**

- [ ] **6.78a** PER-SEGMENT INDICATOR + CHART-PATTERN DISCOVERY — each holon EARNS its feature set — `L11.135`
      *(operator 2026-08-11, `A.57`). Each segment bot searches a shared library and learns which
      indicators and patterns pay IN ITS OWN SEGMENT, from measured outcomes, re-earned on a schedule —
      never hand-assigned (`R.03`). Chart patterns enter as GATED HYPOTHESES ONLY per `D.03`, whose
      evidence stands: computed cheaply, never assumed predictive, must clear the same gate as any
      candidate. No operator-override recorded — none was needed, the two are compatible as scoped.
      Selection per (segment × regime) where data allows, linking to `L11.03`.*
- [ ] **6.79** Bot lifecycle manager — `L14.06`
- [ ] **6.80** Global workspace as the inter-bot bus — `L14.07`
- [ ] **6.81** Referee over bot actions — `L14.08`
- [ ] **6.82** Cross-bot netting + self-trade prevention — `L14.09`
- [ ] **6.83** Portfolio-level risk above all bots — `L14.10`
- [ ] **6.84** THE SIX SEGMENT HOLONS — `L14.17`
- [ ] **6.85** Per-segment on/off switch — `L14.17a`
- [ ] **6.86** MCX is a second venue, not a fifth NSE segment — `L14.17b`
- [ ] **6.87** Index-futures holon — `L14.17c`
- [ ] **6.88** Stock-futures holon — `L14.17d`
- [ ] **6.89** Cross-segment interactions the six holons create — `L14.17e`
- [ ] **6.90** Paper is a parallel-expression laboratory; live is a single-expression selector. With six holons on… — `L14.28`
- [ ] **6.91** Expression-selection engine — `L14.29`
- [ ] **6.92** Per-vehicle conversion track record — `L14.30`
- [ ] **6.93** Live-mode exclusivity enforcement — `L14.31`
- [ ] **6.94** Paper and live run concurrently, not sequentially — `L14.32`

# PHASE 7 — THE ORGANISM

*The conductor, the meta bots, perception tier, memory, autopoiesis, conscience and the org-designer.*


**— L10 —**

- [ ] **7.1** Component registry — `L10.06`
- [ ] **7.2** Component telemetry collector — `L10.07`
- [ ] **7.3** Component health index — `L10.08`
- [ ] **7.4** Component failure-hazard model — `L10.09`
- [ ] **7.5** Hierarchical failure-rate prior — `L10.10`
- [ ] **7.6** Maintenance policy solver — `L10.11`
- [ ] **7.7** Operational-closure auditor — `L10.12`
- [ ] **7.8** Component supervision tree — `L10.13`
- [ ] **7.9** Component repair executor — `L10.14`
- [ ] **7.10** Homeostatic setpoint keeper — `L10.15`
- [ ] **7.11** Organism vitality gate — `L10.16`
- [ ] **7.12** Autopoiesis orchestrator — `L10.17`
- [ ] **7.13** Autopoiesis state store — `L10.18`
- [ ] **7.14** Feature-plane decoupling — `L10.19`
- [ ] **7.15** Deploy-and-verify script + verification cockpit as the operational gate — `L10.20`
- [ ] **7.16** Instrument-token map refresh job — `L10.21`
- [ ] **7.17** Holiday / Muhurat / session calendar in operations — `L10.22`

**— L11 —**

- [ ] **7.18** News ingestion organ — `L11.22`
- [ ] **7.19** Structured index support/resistance extraction from headlines — `L11.23`
- [ ] **7.20** Stock-level S/R extraction — `L11.24`
- [ ] **7.21** Per-source reliability scoring — `L11.25`
- [ ] **7.22** News acquisition ladder — `L11.26`
- [ ] **7.23** NSE corporate-announcement filings ingest — `L11.27`
- [ ] **7.24** News entry-gate — `L11.28`
- [ ] **7.25** Headline sentiment — `L11.29`
- [ ] **7.26** Index-option S/R proximity gate — `L11.30`
- [ ] **7.27** Telegram social-tier ingestion — `L11.31`
- [ ] **7.28** FinBERT-tone / ProsusAI FinBERT for financial sentiment — `L11.32`
- [ ] **7.29** Event extraction over sentiment — `L11.33`
- [ ] **7.30** Novelty-discount + time-decay + news-volume weighting on the aggregate signal — `L11.34`
- [ ] **7.31** IndicBERT / MuRIL for vernacular Indian press — `L11.35`
- [ ] **7.32** Dual-LLM quarantine — `L11.36`
- [ ] **7.33** Source allowlist + ≥2-independent-source corroboration — `L11.37`
- [ ] **7.34** Online-research organ — `L11.38`
- [ ] **7.35** Camoufox anti-detection browser — `L11.39`
- [ ] **7.36** Data-target catalog — `L11.40`
- [ ] **7.37** Acquisition scheduler + append-only point-in-time evidence store — `L11.41`
- [ ] **7.38** Fincept Terminal connector mining — `L11.42`
- [ ] **7.39** Global / cross-market linkage engine — `L11.43`
- [ ] **7.40** Lead-lag / Granger / VECM cross-market analysis — `L11.44`
- [ ] **7.41** ADR-implied single-stock open dislocation — `L11.45`
- [ ] **7.42** Community / tipster experiment harness — `L11.46`
- [ ] **7.43** Source-reliability leaderboard + crowd-sentiment aggregate as a contrarian feature — `L11.47`
- [ ] **7.44** Episodic experience memory (SQLite) — `L11.48`
- [ ] **7.45** Temporal knowledge graph over experience — `L11.49`
- [ ] **7.46** Semantic memory — `L11.50`
- [ ] **7.47** Memory consolidation engine — `L11.51`
- [ ] **7.48** Assumption registry — `L11.52`
- [ ] **7.49** Information diet — `L11.53`
- [ ] **7.50** Graphiti bitemporal knowledge graph — `L11.54`
- [ ] **7.51** HippoRAG2 associative multi-hop recall — `L11.55`
- [ ] **7.52** LangMem procedural (strategy) memory — `L11.56`
- [ ] **7.53** Reflection formula (recency × importance × relevance decay) + A-MEM memory evolution as the consolidation… — `L11.57`
- [ ] **7.54** Meta-memory — `L11.58`
- [ ] **7.55** Working memory · procedural memory · in-weights/in-context tiering · conflict-and-duplicate resolution ·… — `L11.59`
- [ ] **7.56** Memory-grounded strategy analyst — `L11.68`
- [ ] **7.57** Causal cluster analyst — `L11.69`
- [ ] **7.58** Thesis debate risk panel — `L11.70`
- [ ] **7.59** Prediction council with track-record weighting — `L11.71`
- [ ] **7.60** Synthetic stress rehearsal — `L11.72`
- [ ] **7.61** Consensus / conflict resolution — `L11.73`
- [ ] **7.62** Multi-agent memory governance — `L11.74`
- [ ] **7.63** LLM-authored strategy code — `L11.75`
- [ ] **7.64** Automated post-mortem writer feeding the ledger — `L11.76`

**— L12 —**

- [ ] **7.65** Constitutional core — `L12.01`
- [ ] **7.66** Constitutional referee — `L12.02`
- [ ] **7.67** Corrigibility / off-switch — `L12.03`
- [ ] **7.68** Deceptive-alignment monitor + wireheading tripwire — `L12.04`
- [ ] **7.69** Incident post-mortem + forensic store — `L12.05`
- [ ] **7.70** Goal-integrity monitor — `L12.06`
- [ ] **7.71** Mechanistic interpretability — `L12.07`
- [ ] **7.72** Scalable oversight (see L7.21) — `L12.08`
- [ ] **7.73** Instrumental-convergence limiter (see L7.20) — `L12.09`
- [ ] **7.74** Red-team harness — `L12.10`
- [ ] **7.75** Ethics / law reasoner — `L12.11`
- [ ] **7.76** Power budgets (see L7.19) — `L12.12`
- [ ] **7.77** Market-data integrity defense (see L7.22) — `L12.13`

**— L14 —**

- [ ] **7.77a** PER-SEGMENT LIVE-CAPITAL ARMING — one dashboard switch per segment holon — `L13.41`
      *(operator 2026-08-11, `A.56`). Arming one segment puts ONLY that segment on real money; the rest
      keep paper-trading in parallel. Fully live only when every switch is on. Switch set DERIVED from the
      segment registry, never a hardcoded count — this also dissolves the `L5.25` nine vs `D.01` six
      contradiction. `R.22` two-key applies PER SEGMENT (own graduation + own operator arm), so this makes
      live capital harder to reach than the single master switch it replaces.*
- [ ] **7.78** CONDUCTOR BOT — `L14.11`
- [ ] **7.79** Bot residency states — `L14.11a`
- [ ] **7.80** Model cache with LRU eviction — `L14.11b`
- [ ] **7.81** Admission control — `L14.11c`
- [ ] **7.82** Priority classes + preemption — `L14.11d`
- [ ] **7.83** Event-triggered wake — `L14.11e`
- [ ] **7.84** Minimum residency + hysteresis — `L14.11f`
- [ ] **7.85** Deadline awareness — `L14.11g`
- [ ] **7.86** cgroups v2 enforcement — `L14.11h`
- [ ] **7.87** Shared-space substrate — `L14.11i`
- [ ] **7.88** Rate-limit budget arbitration — `L14.11j`
- [ ] **7.89** Conductor failure semantics — `L14.11k`
- [ ] **7.90** Overnight / off-hours reallocation — `L14.11l`
- [ ] **7.91** Bot track record + defunding — `L14.12`
- [ ] **7.92** Disagreement-as-uncertainty — `L14.13`
- [ ] **7.93** Org-designer bot (bot-of-bots) — `L14.14`
- [ ] **7.94** Non-auto-approvable class — `L14.14a`
- [ ] **7.95** Auto-approval grants the LOWEST autonomy tier, never full. An auto-approved spawn starts `advisory` on… — `L14.14b`
- [ ] **7.96** Tiered timeout by blast radius — `L14.14c`
- [ ] **7.97** Reversibility window + one-click rollback — `L14.14d`
- [ ] **7.98** Auto-approval rate limit — `L14.14e`
- [ ] **7.99** Timer runs on operator-awake hours, not wall-clock — `L14.14f`
- [ ] **7.100** Stricter regime while live capital is armed — `L14.14g`
- [ ] **7.101** Auto-approval audit digest — `L14.14h`
- [ ] **7.102** Historian bot + fossil record — `L14.15`
- [ ] **7.103** Population dynamics — `L14.16`
- [ ] **7.104** Perception bots (15): news-research · corporate-filings · expert/analyst-call · tipster/social ·… — `L14.18`
- [ ] **7.105** Decision bots (13): BULL · BEAR [exist] · deterministic arbiter · regime · trend · mean-reversion ·… — `L14.19`
- [ ] **7.106** Survival bots (8): risk · cost · execution · reconciliation · margin · compliance · kill-switch watchdog… — `L14.20`
- [ ] **7.107** Meta bots (13): gatekeeper/validation · treasurer/allocator · librarian/memory · coroner (post-mortem) ·… — `L14.21`
- [ ] **7.108** THE SIX SEGMENT HOLONS ARE THE MAIN BOTS (operator 2026-08-10) — `L14.21a`

# PHASE 8 — LIVE CAPITAL

*Only graduated instructions, only through every gate. SECOND MILESTONE: first real rupee.*


**— L12 —**

- [ ] **8.1** Manual go-live button — `L12.15`
- [ ] **8.2** Per-strategy kill authority — `L12.16`
- [ ] **8.3** Config versioning + rollback + change-log tied to deployments — `L12.17`
- [ ] **8.4** Jurisdiction / segment eligibility — `L12.18`
- [ ] **8.5** Full regulatory audit trail — `L12.19`
- [ ] **8.6** Registration tripwire — `L12.20`
- [ ] **8.7** Alignment tripwires wired at all four entry sites — `L12.21`
- [ ] **8.8** Value-drift monitor — `L12.22`
- [ ] **8.9** Explicit utility function — `L12.23`
- [ ] **8.10** *(DUPLICATE of `2.54`, same `L3.22`. One runbook, not two — see `A.94`.)*
- [ ] **8.11** Scale live capital as the track record earns it, across the full capital range — `A.23`
- [ ] **8.12** ⭐ **MILESTONE — first real rupee traded on a graduated instruction**

# PHASE 9 — FRONTIER

*Earned, never assumed. Every item gated behind a working, profitable base.*


**— L11 —**

- [ ] **9.1** Kronos candlestick foundation model — `L11.19`
- [ ] **9.2** Kronos weight-loading security — `L11.20`
- [ ] **9.3** Chronos / TimesFM / Moirai time-series foundation models as alternative forecast priors — `L11.21`
- [ ] **9.4** Self-evolution loop — `L11.77`
- [ ] **9.5** Symbolic regression for interpretable alpha (PySR, gplearn, DEAP) — `L11.78`
- [ ] **9.6** DSPy — `L11.79`
- [ ] **9.7** py_trees behaviour-tree control loop — `L11.80`
- [ ] **9.8** torchhd hyperdimensional binding — `L11.81`
- [ ] **9.9** pymdp active inference — `L11.82`
- [ ] **9.10** Scallop differentiable Datalog — `L11.83`
- [ ] **9.11** Prolog/PySwip deterministic compliance and blackout veto layer — `L11.84`
- [ ] **9.12** MAPIE conformal prediction — `L11.85`
- [ ] **9.13** NumPyro / GPyTorch Bayesian posteriors feeding sizing — `L11.86`
- [ ] **9.14** TabPFN Bayesian tabular foundation model — `L11.87`
- [ ] **9.15** EWC + replay continual learning on drift — `L11.88`
- [ ] **9.16** learn2learn MAML/Reptile fast regime adaptation — `L11.89`
- [ ] **9.17** htm.core streaming anomaly and regime detection without batch retrain — `L11.90`
- [ ] **9.18** Shared Global Workspace (Goyal et al., ICLR'22) as the modern upgrade to the built `global_workspace` — `L11.91`
- [ ] **9.19** Decision Transformer — `L11.92`
- [ ] **9.20** Ideation loop — `L11.93`
- [ ] **9.21** External-knowledge → validated-hypothesis pipeline — `L11.94`
- [ ] **9.22** Natural-language strategy authoring — `L11.95`
- [ ] **9.23** Later-tier holons: currency derivatives · BSE index options · ETF · SME/illiquid · pre-open auction · expiry-day specialist — `L14.17`
- [ ] **9.24** Remaining cognitive trunks to completion: III WILL · V SELF · XI GENERATIVITY · XII CURIOSITY · XIV AXIOLOGY · the unbuilt branches of I, II, IV, VI, IX, XV, XVI — `Part II`
- [ ] **9.25** ⭐ **MILESTONE — PROJECT COMPLETE: six holons live, all trunks built, the organism designing itself**

---

# PHASE X — CONTINUOUS: GUARDS, BLOCKERS, RULES AND DECISIONS

*Not sequential work. These run alongside every phase above, and the project is not complete until
every one is either satisfied, encoded or explicitly closed.*


## X.A — The 27 disproved findings, encoded as GUARDS

*Part III of the plan. Each becomes an executable block or test, not a footnote — encoded so the trap
cannot be rediscovered as a fresh idea in six months.*

- [ ] **X.A1** Encode as a guard: NARROWED 2026-08-10 — `D.01`
- [ ] **X.A2** Encode as a guard: HFT / latency arbitrage. Closed from a retail cloud VM — `D.02`
- [ ] **X.A3** Encode as a guard: Candlestick and chart patterns as tradeable edge. Rejected by data-snooping-corrected studies… — `D.03`
- [ ] **X.A4** Encode as a guard: Cheap gamma before an event. Contradicted — `D.04`
- [ ] **X.A5** Encode as a guard: Volume-profile levels (POC/VAH/VAL) as signal. Zero peer-reviewed predictive studies; Steidlmayer practitioner… — `D.05`
- [ ] **X.A6** Encode as a guard: Naive stock-option premium selling. Single-name gap risk plus physical settlement plus the STT auto-exercise… — `D.06`
- [ ] **X.A7** Encode as a guard: VPIN as a flash-crash early warning. Rebutted by Andersen-Bondarenko: it peaked *after* the event and co-moves… — `D.07`
- [ ] **X.A8** Encode as a guard: Order-flow imbalance as a forecast. ~65% *contemporaneous* R², but properly lagged the out-of-sample R² is ~3%,… — `D.08`
- [ ] **X.A9** Encode as a guard: L1 book imbalance. The most spoofable signal in the book; use multi-level depth pressure — `D.09`
- [ ] **X.A10** Encode as a guard: India VIX as a leading indicator. Coincident and reactive — `D.10`
- [ ] **X.A11** Encode as a guard: Twitter/Derwent-style mood trading. The 87.6% claim did not replicate; the fund closed in about two years — `D.11`
- [ ] **X.A12** Encode as a guard: Regime switching beats a static blend. *Not proven* net of whipsaw and transaction cost — `D.12`
- [ ] **X.A13** Encode as a guard: Multi-LLM debate committees. Budget-matched committees *underperform* (Berkeley MAST: 41–86% failure) — `D.13`
- [ ] **X.A14** Encode as a guard: Self-correction without ground truth. Measurably *degrades* (Huang, ICLR 2024) — `D.14`
- [ ] **X.A15** Encode as a guard: World-model RL (DreamerV3, MuZero, EfficientZero). Research-demo tier for markets: markets are non-stationary… — `D.15`
- [ ] **X.A16** Encode as a guard: LLMs trading directly. Alpha Arena, real money, Oct 2025: four of six frontier models finished in the red, one… — `D.16`
- [ ] **X.A17** Encode as a guard: LLM look-ahead contamination in backtests. An LLM knows post-cutoff outcomes (Glasserman-Lin) — `D.17`
- [ ] **X.A18** Encode as a guard: `mlfinlab` public repo is stubbed — `D.18`
- [ ] **X.A19** Encode as a guard: `pandas-ta` went paid and was archived Jul 2026 → use `pandas-ta-classic` — `D.19`
- [ ] **X.A20** Encode as a guard: Pollinations LLM provider — `D.20`
- [ ] **X.A21** Encode as a guard: `mem0` deprioritised despite 62k stars — `D.21`
- [ ] **X.A22** Encode as a guard: TabPFN v3 weights are non-commercial — `D.22`
- [ ] **X.A23** Encode as a guard: Finnhub / AlphaVantage / Polygon — `D.23`
- [ ] **X.A24** Encode as a guard: LIDA / Sigma / EPIC / ICARUS cognitive architectures — `D.24`
- [ ] **X.A25** Encode as a guard: The 11-layer roadmap was superseded as the true scope measure by the 16-trunk atlas, which was in turn… — `D.25`
- [ ] **X.A26** Encode as a guard: "Engine #1 = flat premium-seller only" was superseded on 2026-08-02 by "all regimes in scope from the start,… — `D.26`
- [ ] **X.A27** Encode as a guard: Breadth-first atlas program ("build all 197 branches, then resume depth") was superseded in scope by the… — `D.27`

## X.B — The 11 hard blockers, tracked to closure

- [ ] **X.B1** Live-market accrual. Several engines are built and armed but cannot earn their calibration until real sessions… — `B.01`
- [ ] **X.B2** Order-book depth capture needs an open market; OFI and queue-position fills depend on it — `B.02`
- [ ] **X.B3** Fyers and Groww — `B.03`
- [ ] **X.B4** Deep intraday NSE history is largely not free; the realistic free ceiling was researched (r/74, r/77) and it is… — `B.04`
- [ ] **X.B5** India VIX history + per-name IV backfill needed for the IV-rank shrinkage prior — `B.05`
- [x] **X.B6** SPAN margin mechanics — `B.06` — **RESOLVED 2026-08-12 (`A.96`)**, and it was the
      highest-leverage blocker in the plan: `L6.30` gated option book risk (`L6.31`), the structure
      optimizer (`L6.12`-`L6.23`) and the capital allocator's margin constraint. The published
      risk-parameter archive goes back to **2008** and the offline computation agrees with the live
      broker to ±8%. Data path and the ±8% calibration requirement recorded in `research/61` and
      `A.96`; the engine itself is built with **F19/F20**, not now.
- [ ] **X.B7** Per-source API endpoints, rate limits and free-vs-paid for data targets 2–9 were never freshly verified (the… — `B.07`
- [ ] **X.B8** GIFT Nifty data source and licence unverified, as are global-index and ADR free feeds — `B.08`
- [x] **X.B9** RESOLVED 2026-08-10. Environment settled and verified end to end — `B.09`
- [ ] **X.B10** SECURITY — `B.10`
- [ ] **X.B11** Open decisions deferred, not resolved: the todo sequencing question (what is built first) was deliberately… — `B.11`

## X.R — The 26 standing rules, obligations on every task

*Never 'done'. Checked at every sign-off.*

- [ ] **X.R1** Intraday by default; overnight carry only by explicit promotion — `R.01`
- [ ] **X.R2** Never commit secrets; `.env` is gitignored and credentials load from environment only — `R.02`
- [ ] **X.R3** No hardcoded values — `R.03`
- [ ] **X.R4** Thin data never shrinks a feature — `R.04`
- [ ] **X.R5** Real-data verification before sign-off; a hermetic simulation harness behind a DI seam is acceptable only as… — `R.05`
- [ ] **X.R6** No orphaned features — `R.06`
- [ ] **X.R7** Engine-grade depth — `R.07`
- [ ] **X.R8** Every feature visible on the dashboard, with status *measured* from real code and server state, never hand-authored — `R.08`
- [ ] **X.R9** Full universe, never a sample — `R.09`
- [ ] **X.R10** Three segments equal by default; priority order only as a constrained tie-break — `R.10`
- [ ] **X.R11** No silent skips — `R.11`
- [ ] **X.R12** Correctness of execution is not evidence of correctness of allocation. The old market simulation passed its own… — `R.13`
- [ ] **X.R13** "Example" and "etc" are direction pointers, never complete lists — `R.12`

*Rules R.14–R.26 were added after this list was first generated; appended 2026-08-10 during a
plan↔todo consistency audit.*

- [ ] **X.R14** **Self-describing names.** Every file, module, function, class and variable reveals its role by name alone. — `R.14`
- [ ] **X.R15** **Research, planning and decisions are saved to files, every time, without fail.** Never left in chat. — `R.15`
- [ ] **X.R16** **Never compromise a feature down to what is on hand — acquire what it needs.** If a feature needs data, a tool, a source, a library or a capability t — `R.16`
- [ ] **X.R17** **OSS rejection needs mechanical evidence, never README prose.** Triage on facts: does it install here, is it maintained, what do the actual signature — `R.17`
- [ ] **X.R18** **One engine at a time; sign off out loud before advancing.** The unit of work is a complete engine-grade feature, not a thin slice. — `R.18`
- [ ] **X.R19** **Ambiguity is interviewed, never assumed.** When something mid-build is genuinely ambiguous, stop and ask rather than picking and hoping. — `R.19`
- [ ] **X.R20** **Recommendation honesty — the "(Recommended)" mark is earned, not reflexive.** When presenting options: give the **full set**, not a token two or thr — `R.20`
- [ ] **X.R21** **Three strikes, then stop and report.** Try, try a different way, try a third — then stop. — `R.21`
- [ ] **X.R22** **Two-key rule for live capital.** No instruction reaches real money without **both** keys: (1) it has **graduated** — demonstrated repeatable profit  — `R.22`
- [ ] **X.R24** **SEARCH BY DELEGATION — breadth goes to a subagent, only the verdict comes back.** Standing method for every search, established 2026-08-10 and perma — `R.24`
- [ ] **X.R25** **OPINIONS ARE RECORDED, NOT SPOKEN.** Every judgement, verdict or recommendation I give goes into `docs/CLAUDE_OPINIONS.md` at the moment it is forme — `R.25`
- [ ] **X.R26** **SUBAGENT MODEL SELECTION — cheapest model that reliably does the job.** Match the model to the task's difficulty, not to habit, and prefer the cheap — `R.26`

## X.Q — The 12 open questions, all resolved

*Carried for provenance: each was resolved by a recorded decision, so none is silently outstanding.*

- [x] **X.Q1** Which strategy family goes first (mean-reversion/ORB · index-option premium selling · cash-futures basis ·… — `Q.01` → resolved in A.07–A.18
- [x] **X.Q2** Cash-equity, F&O, or both in the first cut? — `Q.02` → resolved in A.07–A.18
- [x] **X.Q3** How many regime engines are armed at launch — `Q.03` → resolved in A.07–A.18
- [x] **X.Q4** First model class for the directional bots — `Q.04` → resolved in A.07–A.18
- [x] **X.Q5** Decision cadence — `Q.05` → resolved in A.07–A.18
- [x] **X.Q6** Radar universe at launch — `Q.06` → resolved in A.07–A.18
- [x] **X.Q7** Minimum edge threshold above the cost floor before firing? — `Q.07` → resolved in A.07–A.18
- [x] **X.Q8** Self-evolution — `Q.08` → resolved in A.07–A.18
- [x] **X.Q9** How much of the 16-faculty atlas is in scope for *this* system versus the deferred phase? — `Q.09` → resolved in A.07–A.18
- [x] **X.Q10** Stock-option premium selling — `Q.10` → resolved in A.07–A.18
- [x] **X.Q11** Bank Nifty (monthly-only) — `Q.11` → resolved in A.07–A.18
- [x] **X.Q12** Which ultra-tier items are genuinely wanted rather than nice-to-have? — `Q.12` → resolved in A.07–A.18

## X.D — The 40 governing decisions

*The decisions this plan was built on, each with its reasoning in the plan so it can be overturned by
evidence rather than mood.*

- [x] **X.D1** Futures and commodities last and optional. Five segments. SIX segment holons, all built from the start:… — `A.01`
- [x] **X.D2** Same-underlying multi-instrument exposure: allowed in paper, exclusive in live. All expressions of one… — `A.06`
- [x] **X.D3** The org-designer runs unsupervised with timeout auto-approve — `A.05`
- [x] **X.D4** Cash equity may carry overnight; options never. Default remains intraday for everything — `A.02`
- [x] **X.D5** Two distinct top-level bots, not one. The conductor (L14.11) governs *hardware* — `A.03`
- [x] **X.D6** Hardware contention: solved by the conductor bot (L14.11). Bots do not assume residency; they are woken when… — `A.04`
- [x] **X.D7** First strategy family (resolves Q.01) → intraday MEAN-REVERSION on cash equity. Chosen over premium-selling,… — `A.07`
- [x] **X.D8** Regime engines at launch (resolves Q.03) → build all four, arm ONE. Per Rule Q: the four-regime brain is built… — `A.08`
- [x] **X.D9** Model class (resolves Q.04) → LightGBM only; DeepLOB stays catalogued and gated. (a) The corpus's own research… — `A.09`
- [x] **X.D10** Decision cadence (resolves Q.05) → split cadence: per-BAR entries, per-TICK exits. Entry deliberation runs on… — `A.10`
- [x] **X.D11** Radar universe at launch (resolves Q.06) → liquid subset first, shard later. Top ~200–500 cash names plus… — `A.11`
- [x] **X.D12** Minimum edge above the cost floor (resolves Q.07) → start at 1.5×, calibrate from realized data. The research… — `A.12`
- [x] **X.D13** Self-evolution timing (resolves Q.08) → strictly after a live edge exists. Unambiguous in the corpus: evolution… — `A.13`
- [x] **X.D14** Atlas scope (resolves Q.09) → the three completed trunks are in scope NOW as infrastructure; the rest re-enter… — `A.14`
- [x] **X.D15** Stock-option premium selling (resolves Q.10) → stays OFF. Confirmed: single-name gap risk, physical settlement,… — `A.15`
- [x] **X.D16** Bank Nifty (resolves Q.11) → directional and hedged mid-cycle; theta only in the final expiry week.… — `A.16`
- [x] **X.D17** Ultra-tier items (resolves Q.12) → catalogued, unscheduled, revisited by evidence. No ultra item is committed now — `A.17`
- [x] **X.D18** The verdict on all of the above. Every one of these is a *recommendation adopted*, not a constraint discovered — `A.18`
- [x] **X.D19** Regime playbook instructions adopted; the 2-point scalping example rejected on arithmetic. The instruction… — `A.19`
- [x] **X.D20** Denominator correction — `A.20`
- [x] **X.D21** Paper is the instruction proving ground; graduated winners run live. Each of the six holons continuously… — `A.21`
- [x] **X.D22** Process fix — `A.22`
- [x] **X.D23** Market-closed open-market simulation rebuilt (v2), with the failure diagnosed from data. The operator named… — `A.30`
- [x] **X.D24** Ultra-advanced introspection dashboard + project chat adopted. Shows what each bot is *thinking* — `A.29`
- [x] **X.D25** LLM routing ladder reversed: Claude Max subscription is PRIMARY. Order is now subscription → free-tier cloud →… — `A.28`
- [x] **X.D26** Profit-trail learning gate added as the third directional organ, per holon. The architecture had two entry… — `A.27`
- [x] **X.D27** The foundational answers (interview round 1, 2026-08-10). These govern everything above and were previously… — `A.23`
- [x] **X.D28** This file is a CATALOG, not a build plan. Its purpose is to hold every feature, idea, design and finding this… — `A.24`
- [x] **X.D29** Broker scope: Kite for execution, multi-broker for data. Zerodha Kite is the single execution path; the data… — `A.25`
- [x] **X.D30** The three survivors of the reset. Everything else was archived and deleted; these were kept live on the server… — `A.26`

---

# COVERAGE AUDIT — machine-verified

Diffed programmatically against `ajith_final_plan.md`:

```
plan feature entries (L0-L14)   588
referenced in this todo         588
MISSING                           0
extra / orphaned                  0
```

| Plan section | Count | Where |
|---|---|---|
| L0 data truth | 34 | Phase 1 |
| L1 cost | 15 | Phase 1 |
| L2 validation | 32 | Phase 2 |
| L3 ops floor | 28 | Phase 2 |
| L4 signals | 32 | Phase 3 |
| L5 strategies | 52 | Phases 3, 4, 6 |
| L6 options & Greeks | 39 | Phase 6 |
| L7 risk | 26 | Phases 2, 3, 6 |
| L8 portfolio | 17 | Phase 6 |
| L9 execution | 13 | Phase 2 |
| L10 operations & simulation v2 | 35 | Phases 4, 7 |
| L11 intelligence | 138 | Phases 3, 4, 5, 7, 9 |
| L12 governance | 23 | Phases 2, 7, 8 |
| L13 dashboard | 40 | Phase 5 |
| L14 organism | 64 | Phases 3, 6, 7 |
| **Part II** — 16 trunks / 197 branches | — | Phases 7, 9 (9.x closes the remainder) |
| **Part III** — disproved | 27 | **X.A** — encoded as guards |
| **Part IV** — blockers | 11 | **X.B** |
| **Part IV** — standing rules | 13 | **X.R** |
| **Part IV** — open questions | 12 | **X.Q** — all resolved |
| **Part IV** — decisions | 30 | **X.D** |

**Milestones:** Phase 4 — first instruction graduates · Phase 8 — first real rupee · Phase 9 — project
complete: six holons live, all trunks built, the organism designing itself.

---

*Decisions A.31–A.39 were added after this list was first generated; appended 2026-08-10 during the
same audit.*

- [ ] **X.D31** The old governing docs are deleted; these two files govern alone.** `CLAUDE.md`, `docs/RULES.md` and `GLOBAL_CLAUDE.md` were deleted 2026-08-10 once ` — `A.31`
- [ ] **X.D32** Enforcement hooks rewritten to match the new rules (2026-08-10).** The hooks embedded the old rule text inline and checked deleted paths, so retired r — `A.32`
- [ ] **X.D33** Search-by-delegation adopted as permanent standing method (R.24).** Context is the scarce resource, not tokens: a subagent burns its own context and r — `A.33`
- [ ] **X.D34** Instrument-master primary key corrected before implementation (2026-08-10).** The plan specified `(exchange, tradingsymbol)`; measurement against the  — `A.34`
- [ ] **X.D35** Watch-everything moves to the five derivative segments; cash keeps a filter-based focus set.** Operator decision. — `A.35`
- [ ] **X.D36** Measured: watch-everything is affordable in ONE Kite key (~6,100 instruments).** Before measuring, the 9,000-instrument ceiling looked like the bindin — `A.36`
- [ ] **X.D37** Opinions get their own file (R.25) and subagents get a model-selection rule (R.26).** Operator instructions 2026-08-10. — `A.37`
- [ ] **X.D38** 2026-08-10 · `L0.05` + `L0.06` build as one engine, and the universe exposes three questions, not one flag.** Point-in-time reconstruction and the fro — `A.38`
- [ ] **X.D39** 2026-08-10 · Two SOTA analogs named in R.23a cannot be installed on this box — they stay analogs, never dependencies.** Mechanically confirmed: `qlib` — `A.39`

- [ ] **X.D41** A collection report is a THREE-way partition — observed / uncollected / **unverifiable**; 'cannot tell' is a real state and must not be folded into either side. — `A.41`
- [ ] **X.D40** `L0.30` trading calendar promoted to a PREREQUISITE of `L0.05`/`L0.06`; `pandas_market_calendars` is the source (`exchange_calendars` has no NSE). — `A.40`

- [x] **X.R27** Standing order: work the todo continuously, terse sign-off per slice, stop only for genuine ambiguity / operator-only actions / live-capital arming — `R.27`
- [x] **X.D42** Operator granted the standing order; paired with stop-and-interview on ambiguity — `A.42`

- [x] **X.D43** R.03 money-literal guard wired into the execution gate over `src/` + `scripts/`; firing proven with a planted violation. — `A.43`

## Maintenance

Regenerated from `ajith_final_plan.md` whenever the plan changes materially — the generator reads the plan
and re-emits this file, so drift between the two is structurally impossible.

A task is checked off only when the Rule-A sign-off passes: engine-grade depth, real-data verification (or
a hermetic harness with the real-data pass logged as an open blocker), wired into the loop with no orphans,
visible on the dashboard, and every deferral recorded. **A task whose primary consumer is still queued is
not done.**

