# 210 — NSE Ingest Core: OSS Sourcing (mechanical evidence)

Scope: before building the shared NSE ingest core (fetch-with-retry → strict parse → bitemporal
store → coverage self-check → provenance manifest) plus nine adapters, this document records
what already exists, tested mechanically against real NSE endpoints on this host
(Linux aarch64, Python 3.12, venv `/home/opc/nse-algo-trader/.venv`).

Test date: 2026-08-11. Test dates used: **2026-08-10** (Monday, recent trading day, post-UDiFF),
**2020-01-15** (Wednesday, pre-UDiFF), plus the exact July-2024 boundary: **2024-07-04** (last
week before cutover) and **2024-07-08** (NSE's own UDiFF-switch date). Every claim below is
tagged VERIFIED (I executed the command/call myself, output pasted) or UNVERIFIED (README/docs
claim I could not or did not independently execute).

All commands and outputs below were run in this session; raw transcripts are reproducible with
the commands shown.

---

## Part A — NSE-specific libraries

### A.1 `jugaad-data`

- **Install**: VERIFIED. `pip install jugaad-data` succeeds cleanly. Installed version `0.35.2`.
- **Maintenance**: VERIFIED. PyPI releases: 0.34.0 (2026-07-31), 0.35.0 (2026-07-31), 0.35.1
  (2026-08-02), **0.35.2 (2026-08-07)** — 4 days before this test. GitHub mirror
  (`jugaad-py/jugaad-data`, 550 stars) pushed 2026-08-07. Most actively maintained candidate by far.
- **License**: `YOLO` per package metadata (an actual, if informal, SPDX-ish license string used
  by the author). Not rejecting on license per project policy.
- **Cash bhavcopy, both eras**: VERIFIED.
  - `bhavcopy_save(date(2026,8,10), dir)` → real UDiFF file, columns
    `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,...`
  - `bhavcopy_save(date(2020,1,15), dir)` → real legacy file, columns
    `SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, ...`
  - Both calls returned real, correctly-dated data.
- **Boundary test, cash**: VERIFIED, with an important caveat. `bhavcopy_save(date(2024,7,8), dir)`
  (NSE's own cutover date) returned the **old legacy schema**, not UDiFF, even though the date is
  `>= udiff_start_date`. Root cause, read directly from source
  (`jugaad_data/nse/archives.py`):
  - `NSEArchives.udiff_start_date = date(2024, 7, 8)` (line 184).
  - `bhavcopy_raw()` (line 214) tries `NSEDailyReports.download_file('CM-UDIFF-BHAVCOPY-CSV', ...)`
    for any `dt >= udiff_start_date`, and **silently falls back** to
    `full_bhavcopy_raw()` (old `sec_bhavdata_full_*.csv` schema) on `ValueError` /
    `RequestException` / `BadZipFile` (lines 236-255), with **no warning, no exception, no
    schema flag** surfaced to the caller.
  - `NSEDailyReports`'s own docstring (line 16-23) states: *"API supports current day and previous
    day only. For historical data, use `NSEArchives.full_bhavcopy_raw()` instead."*
  - I called the UDiFF path directly to confirm: `daily_reports.download_file('CM-UDIFF-BHAVCOPY-CSV',
    trading_date=date(2024,7,8))` → `ValueError: File CM-UDIFF-BHAVCOPY-CSV not found in daily
    reports` (same for 2024-07-05, 2024-07-09). Only `date(2026,8,10)` (current/previous day at
    test time) succeeded (198,559 bytes).
  - **Critical finding**: `jugaad-data`'s cash bhavcopy only returns **true UDiFF** for the
    current/previous trading day. Every other historical post-cutover date is silently served in
    the **old legacy schema**, indistinguishable from a genuine pre-2024 file without inspecting
    the columns yourself. A consumer expecting UDiFF-only fields (segment, instrument type,
    settlement price, richer F&O columns) for, say, 2025-01-15 will get none of that and no error.
- **F&O bhavcopy is broken for any post-cutover date**: VERIFIED, critical finding.
  `bhavcopy_fo_save(date(2020,1,15), dir)` → OK, real data (`INSTRUMENT,SYMBOL,EXPIRY_DT,...`).
  `bhavcopy_fo_save(date(2026,8,10), dir)` → **`BadZipFile: File is not a zip file`**. Traced to
  source: `bhavcopy_fo_raw` (line ~319) always hits the fixed legacy path
  `/content/historical/DERIVATIVES/{yyyy}/{MMM}/fo{dd}{MMM}{yyyy}bhav.csv.zip` with **no status
  check before unzipping** (`r = self.get(...); return r.content`, no `raise_for_status()`).
  I fetched that exact URL directly for 2026-08-10 and got a real **HTTP 404** with a 3,425-byte
  HTML error body — the library tries to `zipfile.ZipFile()` on that HTML and throws the
  confusing `BadZipFile` instead of a clear 404. **F&O bhavcopy in `jugaad-data` does not
  handle the 2024 format change at all — it is dead for any recent date.**
- **Retry/backoff/caching**: VERIFIED absent. `grep -rn "Retry|backoff|tenacity|retry"` over the
  whole installed package: zero matches. `NSEDailyReports.__init__` declares `self._cache = {}`
  (line 41) but it is never read or written anywhere else in the file — a dead attribute, not a
  real cache.
- **Bitemporal storage**: VERIFIED absent. No `observed_at`/`as_of`/`valid_time`/`transaction_time`
  anywhere in the package. It is a pure fetch-and-save library; storage is entirely the caller's
  responsibility.
- **Verdict: ADAPT.** Best-maintained candidate and the only one that gets true recent-day UDiFF
  cash bhavcopy correctly, but its silent old/new schema fallback for historical cash dates and
  its broken F&O path make it unsafe to use unmodified — wrap it, never trust its F&O bhavcopy,
  and add our own schema-detection check on every cash response before it is trusted.

### A.2 `nsepython`

- **Install**: VERIFIED, succeeds. Installed version `2.97`.
- **Maintenance**: VERIFIED. Last PyPI release 2025-05-26 — about 14.5 months stale relative to
  today (2026-08-11). GitHub repo `aeron7/nsepython`.
- **License**: `GNU` (GPL family) per package metadata.
- **Cash bhavcopy, all eras**: VERIFIED, works but never touches UDiFF. `get_bhavcopy(date)`
  (`nsepython/rahu.py:797`) always fetches
  `https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv` — the same
  legacy full-bhavdata file NSE still publishes in parallel post-2024. Tested all four dates
  (`10-08-2026`, `15-01-2020`, `08-07-2024`, `04-07-2024`): all four returned 200 with real data,
  identical old-format 15-column schema in every case (`SYMBOL, SERIES, DATE1, ...`).
  **It "survives" the format change only because it ignores UDiFF entirely** — it never returns
  the richer new schema, ever.
- **F&O ban list**: VERIFIED not implemented as a wrapper, but the underlying NSE endpoint is
  real and reachable. `grep -ni "ban"` over the 965-line source file: zero hits — no ban-list
  function exists in `nsepython`. I confirmed the actual ban-list CSV endpoint independently
  (see A.9 below) works fine; `nsepython` simply doesn't wrap it.
- **Retry/backoff/caching**: VERIFIED absent — no `Retry`, `backoff`, `tenacity`, or session-level
  retry logic anywhere in `rahu.py`.
- **Bitemporal storage**: VERIFIED absent.
- **Verdict: ADAPT** (narrow use). Fine as a thin, stable wrapper around the legacy
  full-bhavdata CSV endpoint (works across all dates because it deliberately never engages
  UDiFF), but contributes nothing toward F&O bhavcopy, ban list, or any temporal/retry
  scaffolding — those still have to be built.

### A.3 `nsepy`

- **Install**: VERIFIED, succeeds. Installed version `0.8`.
- **Maintenance**: VERIFIED, dead. Last PyPI release **2020-03-07** (over 6 years stale). Last
  GitHub commit 2023-12-24.
- **License**: unspecified in package metadata; GitHub API reports `NOASSERTION` (no LICENSE file
  recognized).
- **Actual call**: VERIFIED failure, both dates. `get_history(symbol="SBIN", start=..., end=...)`
  for both 2020-01-15 and 2026-08-10 raised:
  `requests.exceptions.SSLError: HTTPSConnectionPool(host='www1.nseindia.com', port=443):
  Max retries exceeded ... SSLError(1, '[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal
  error')`. The library targets `www1.nseindia.com`, a hostname NSE no longer serves cleanly —
  the library is completely non-functional on this host, for any date.
- **Format-change handling**: N/A — cannot reach the server at all.
- **Retry/backoff/caching/bitemporal**: not evaluated further; the library cannot make a
  successful request in its current state.
- **Verdict: REJECT.** Dead endpoint, dead upstream host, unmaintained for years.

### A.4 `nse-utility`

- **Install**: VERIFIED failure. `pip install nse-utility` → `ERROR: Could not find a version
  that satisfies the requirement nse-utility (from versions: none)`. Also tried `nseutility`
  (no dash) and `NseUtility` — both 404 on PyPI's JSON API.
- **GitHub search**: VERIFIED. Web search and GitHub repo search for "nse-utility" /
  "NseUtility" python found only `jigsoft/nseutility` (0 stars, last pushed 2019-03-17) — not a
  real candidate.
- **Verdict: REJECT.** This package does not exist under any plausible name on PyPI, and the one
  GitHub repo matching the name is a 7-year-stale, 0-star project. Not installable, not usable.

### A.5 `bhavcopy`

- **Install**: VERIFIED, succeeds. Installed version `3.0` (its only release, 2023-07-29,
  PyPI project has no other versions).
- **License**: MIT (package metadata).
- **Source review**: read `bhavcopy/downloader.py` in full (269 lines).
  - Equities path: `https://archives.nseindia.com/content/historical/EQUITIES/{yyyy}/{MMM}/
    cm{dd}{MMM}{yyyy}bhav.csv.zip` — fixed legacy path, no UDiFF awareness.
  - Derivatives path: hardcoded `http://` (not https) `archives.nseindia.com/.../fo...bhav.csv.zip`.
  - `file_checks()` builds paths with a **literal Windows backslash**:
    `file_path = data_storage + "\\" + instr + ".csv"` — Windows-only code shipped as a
    general-purpose package.
  - Error handling is a bare `except: print("...:failed"); pass` in `update_database()` — swallows
    every failure silently, no re-raise, no structured error.
  - No retry/backoff (only a `time.sleep(random.randint(*wait_time))` politeness delay between
    calls). No caching beyond "skip if a local file already exists." No bitemporal fields —
    single `TIMESTAMP` column only (trade date; no ingestion/observed timestamp anywhere).
- **Actual calls**: VERIFIED.
  - `bhavcopy("equities", ..., date(2026,8,10), ...).get_data()` → `HTTP Error: 404 Client Error:
    Not Found for url: https://archives.nseindia.com/content/historical/EQUITIES/2026/AUG/
    cm10AUG2026bhav.csv.zip` — completely dead for the recent date (that legacy `archives.`
    subdomain / path pairing is gone; NSE now serves historical zips from `nsearchives.`).
  - `bhavcopy("derivatives", ..., date(2020,1,15), ...).get_data()` → succeeded, real old-format
    F&O data downloaded and parsed.
- **Format-change handling**: VERIFIED — does not handle it. Equities fetch is **dead** for any
  post-2024 date (404, confirmed above); derivatives path was not tested post-2024 but shares the
  same fixed-URL pattern that already 404s for equities, so it is presumed equally dead (same
  `archives.nseindia.com` legacy host).
- **Verdict: REJECT.** Single stale release, Windows-hardcoded paths, silent bare-except error
  swallowing, dead endpoint for the equities path on any current date.

### A.6 `nsetools`

- **Install**: VERIFIED, succeeds. Installed version `2.0.1`.
- **Maintenance**: VERIFIED. Releases jumped from 1.0.8 (2018) straight to 2.0.0/2.0.1 both dated
  2025-03-18 — a revival, but still ~17 months stale relative to today. GitHub last push
  2025-03-18.
- **License**: MIT.
- **Scope mismatch**: VERIFIED by introspection. `Nse` class exposes only live/quote-style
  methods: `get_quote`, `get_top_gainers/losers`, `get_index_quote`, `get_52_week_high/low`,
  `get_stock_codes`, etc. **No bhavcopy method, no ban-list method, no historical-data method at
  all.** It is architecturally the wrong tool for a bhavcopy/ban-list/MWPL ingest core regardless
  of whether it works.
- **Actual call**: VERIFIED failure even on its own supported use case.
  `Nse().get_quote('SBIN')` → `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` — NSE
  returned a non-JSON body (consistent with an Akamai block/redirect page) that the library did
  not handle.
- **Verdict: REJECT.** Wrong scope for this project (no bhavcopy/ban-list/historical support at
  all) and currently broken against the live site even for what it does support.

### A.7 `pynse`

- **Install**: VERIFIED failure. `pip install pynse` → `ERROR: Could not find a version that
  satisfies the requirement pynse (from versions: none)`. PyPI's own JSON API for the `pynse`
  project returns an empty `releases` dict and `info.summary: null` — the project name is
  reserved on PyPI but has **zero published releases**, ever.
- **GitHub search**: VERIFIED. Several unrelated/abandoned repos named `pynse` exist
  (`mlfreerl/pynse` last pushed 2020-06-30, `raaghulr/pynse` 2021-09-03, `ustayready/pynse` 2017,
  `purwarak/pynse` last pushed **2023-05-22**, not archived but 3+ years stale, 4 stars). None is
  installable via pip under the `pynse` name.
- **Verdict: REJECT.** Not installable by any of the standard means; the closest matching GitHub
  project is multi-year stale with negligible adoption.

### A.8 `breeze-connect`

- **Install**: VERIFIED, succeeds (builds a wheel locally). Installed version `1.0.69`.
- **Maintenance**: PyPI releases visible through 1.0.69; repo is `Idirect-Tech/Breeze-Python-SDK`
  (ICICI Direct's own official SDK — actively maintained, but by intent this is a **broker API
  client**, not an NSE public-data library).
- **Scope check**: VERIFIED by introspection. `BreezeConnect` exposes `generate_session`,
  `get_quotes`, `get_historical_data`, `place_order`, `get_funds`, `get_portfolio_holdings`, GTT
  order methods, etc. — entirely built around **authenticated broker access** (requires an ICICI
  Direct trading account, an API key, a secret, and a session token generated through ICICI's own
  web login flow). **No bhavcopy, no F&O ban list, no MWPL, no bulk/block deals, no delisted
  securities, no ASM/GSM, no index constituents, no corporate announcements** — none of the nine
  target adapters map onto this SDK's surface.
- **Actual call**: not executed — doing so requires live ICICI Direct credentials this session
  does not have, and would not exercise any of the nine adapters even if it succeeded.
- **Verdict: REJECT** for this project. Real, maintained, legitimate SDK — but it is a live-broker
  execution/quotes client, not an NSE public-archive data source, and requires credentials outside
  this task's scope. None of the nine planned adapters are in its domain.

### A.9 Bonus finds (discovered via GitHub/PyPI search, not on the original list)

**`nse`** (PyPI name `nse`, GitHub `BennyThadikaran/NseIndiaApi`, 156 stars, license GPL-3.0)

- **Install**: VERIFIED, succeeds. Installed version `3.2.1`.
- **Maintenance**: VERIFIED, the most actively maintained candidate besides `jugaad-data` —
  GitHub last pushed 2026-07-23 (19 days before this test), 0 open issues.
- **Cash + F&O bhavcopy, correctly spanning the format change**: VERIFIED, the strongest result
  of any candidate tested. Source (`nse/NSE.py`) has an explicit
  `UDIFF_SWITCH_DATE = datetime(2024, 7, 8).date()` and branches on it (line 355):
  dates `< UDIFF_SWITCH_DATE` use the legacy `cm{dd}{MMM}{yyyy}bhav.csv.zip` path; dates
  `>= UDIFF_SWITCH_DATE` use the **real historical UDiFF archive**
  `content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip` — this is a genuinely different
  (and correct) endpoint from `jugaad-data`'s "current/previous day only" daily-reports API, so it
  works for **any** historical post-cutover date, not just the last day. I called
  `equityBhavcopy()` for all four test dates and got the right schema every time:
  2020-01-15 → old `SYMBOL,SERIES,OPEN,...`; 2024-07-04 → old schema; **2024-07-08 → real UDiFF**
  (`TradDt,BizDt,Sgmt,...`); 2026-08-10 → real UDiFF. `fnoBhavcopy(2026-08-10)` also succeeded
  with real UDiFF F&O data (`TradDt,BizDt,Sgmt,...`).
- **Failure classification**: VERIFIED, better than any other candidate. `_download()`
  (line 201-230) explicitly checks `content-type` for `text/html` before treating a response as a
  valid file and raises `RuntimeError("NSE file is unavailable or not yet updated.")` if so —
  i.e. it already partially solves the "HTTP-200-with-HTML-error-body" trap that every other
  library tested here falls into. `_req()` (line 232-243) also explicitly checks
  `200 <= r.status_code < 300` and raises `ConnectionError` otherwise.
- **Throttling**: VERIFIED real (not decorative). Uses the `mthrottle` dependency
  (`from mthrottle import Throttle; th = Throttle(throttleConfig, 10)`), calling `th.check()`
  before every request — genuine, if simple, rate limiting. No exponential-backoff retry on
  failure, though (a throttle, not a retry-with-backoff).
- **Ban list**: VERIFIED not implemented — no `ban`-named method anywhere in the 50+ method
  surface, confirmed by grep and by listing `dir(NSE)`.
- **Bitemporal storage**: VERIFIED absent for market data (it persists cookies and a small
  options-expiry cache to local JSON files, but that's session/auth plumbing, not
  valid-time/transaction-time data storage).
- **Verdict: ADOPT** (best of the NSE-specific libraries for cash+F&O bhavcopy specifically).
  Genuinely handles the July-2024 format change correctly across historical dates, and already
  does real 200-vs-HTML-error classification. Still needs our own ban-list, MWPL, bulk/block-deal,
  delisted-securities, ASM/GSM, and bitemporal-storage work on top.

**`nselib`** (PyPI, GitHub `RuchiTanmay/nselib`, Apache-2.0)

- **Install**: VERIFIED, succeeds. Latest release `2.5.1` (2026-05-01) — 3 months stale but
  actively maintained (7 releases since April 2026).
- **F&O ban list — the only candidate with a real, dedicated wrapper**: VERIFIED.
  `derivatives.fno_security_in_ban_period(trade_date='10-08-2026')` →
  `['BANDHANBNK', 'KAYNES', 'LICI']`, real current data from
  `https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_*` (confirmed by reading
  `nselib/derivatives/derivative_data.py:785-826`).
- **Cash & F&O bhavcopy — critical one-era-only finding**: VERIFIED. `bhav_copy_equities()` and
  `fno_bhav_copy()` (`nselib/capital_market/capital_market_data.py:376-400`) **always** build the
  UDiFF-only URL `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}
  _F_0000.csv.zip`, unconditionally, for every date — there is no branch for pre-cutover dates
  anywhere in the source. Calling both for 2026-08-10 returned real data (3,564 rows / 33,601 rows
  respectively, correct UDiFF columns). Calling both for **2020-01-15 returned an EMPTY DataFrame
  — `shape: (0, 0)`, no exception raised, no warning.** Source shows why: the function only
  special-cases `status_code == 200` (populate) and `status_code == 403` (raise
  `FileNotFoundError`); any other status (the real pre-2024 response is a 404 from that UDiFF-only
  URL) falls through to `return bhav_df` where `bhav_df = pd.DataFrame()` was never populated —
  a **silent empty-result failure**, worse than an exception because a caller who doesn't check
  `.empty` will not notice anything went wrong.
- **Verdict: ADAPT.** The single best source for the F&O ban-list adapter specifically (real,
  dedicated, working wrapper, actively maintained) — but its bhavcopy functions are a textbook
  case of "only handles one era," and fail silently rather than loudly, which is more dangerous
  than jugaad-data's or bhavcopy's loud failures.

**`nseazy`**

- **Install**: VERIFIED, `pip install` succeeds (version `0.0.1b5`), but the package is **broken
  on import**: `import nseazy` → `ModuleNotFoundError: No module named 'nseazy._warnings'` — a
  packaging defect in the published wheel itself (an internal submodule referenced in
  `__init__.py` was never shipped). GitHub (`DrChandrakant/NSEazy`) last pushed 2023-06-22,
  3 stars.
- **Verdict: REJECT.** Cannot even be imported as installed from PyPI. Dead project.

---

## Part A summary table

| Candidate | `pip install` | Last release / commit | Real call: recent bhavcopy | Real call: 2020 bhavcopy | Real call: F&O ban list | Handles Jul-2024 format change | Retry/backoff | Bitemporal | License | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| jugaad-data | OK | 2026-08-07 / 2026-08-07 | OK (true UDiFF, cash) | OK (legacy) | not implemented | **Partial** — cash: only current/previous day gets true UDiFF, older post-cutover dates silently fall back to legacy schema; F&O: **broken** (BadZipFile) for any recent date | none found | none | YOLO | **ADAPT** |
| nsepython | OK | 2025-05-26 / n/a | OK (legacy schema only) | OK | not implemented (endpoint works standalone) | Ignores UDiFF entirely; always old schema | none found | none | GNU | **ADAPT** (narrow) |
| nsepy | OK | 2020-03-07 / 2023-12-24 | **FAILED** (SSLError, dead host `www1.nseindia.com`) | **FAILED** (same) | n/a | n/a — cannot connect | n/a | n/a | none asserted | **REJECT** |
| nse-utility | **FAILED** (not on PyPI under any spelling) | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **REJECT** |
| bhavcopy | OK | single release 2023-07-29 | **FAILED** (404, dead legacy URL) | OK | n/a | No — equities path dead for any recent date | sleep-only, no retry | none | MIT | **REJECT** |
| nsetools | OK | 2025-03-18 / 2025-03-18 | n/a — no bhavcopy support at all | n/a | n/a — no ban-list support | n/a | none found | none | MIT | **REJECT** (wrong scope + currently broken) |
| pynse | **FAILED** (0 releases ever published) | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **REJECT** |
| breeze-connect | OK | active (broker SDK) | n/a — not a bhavcopy source | n/a | n/a | n/a | n/a | n/a | none asserted | **REJECT** (wrong tool: broker/execution API, needs live credentials) |
| **nse** (bonus) | OK | 2026-07-23 / 2026-07-23 | OK (true UDiFF, cash + F&O) | OK (legacy) | not implemented | **Yes, correctly** — explicit `UDIFF_SWITCH_DATE` branch, works for any historical date via real archive endpoint | throttle (mthrottle) only, no retry-with-backoff | none for data (cookies/expiry cache only) | GPL-3.0 | **ADOPT** |
| **nselib** (bonus) | OK | 2026-05-01 / 2026-05-01 | OK (true UDiFF, cash + F&O) | **FAILED SILENTLY** — empty DataFrame, no error | **OK, real dedicated wrapper** | **No** — always UDiFF-only URL; silently empty for pre-cutover dates | none found | none | Apache-2.0 | **ADAPT** |
| nseazy | OK (install) | 2023-06-22 | **FAILED** — `ModuleNotFoundError` on import, cannot run at all | n/a | n/a | n/a | n/a | n/a | BSD-style | **REJECT** |

---

## Part B — Generic building blocks

### B.1 HTTP retry/backoff and failure classification

Installed and probed: `tenacity` (9.1.4), `backoff` (2.2.1), `requests-cache` (1.3.3, uses
`cattrs`/`platformdirs`), `httpx` (0.28.1, already present), plus stdlib `urllib3.util.Retry`.

Real NSE failure modes captured this session (not simulated):

1. **404 with HTML error body** — dead legacy F&O bhavcopy URL
   (`nsearchives.nseindia.com/.../fo10AUG2026bhav.csv.zip`): `status=404`,
   `content-type: text/html;charset=UTF-8`, 3,425-byte Apache-style error page.
2. **500 with a near-empty error body** — `www.nseindia.com/api/liveEquity-derivatives?...`
   called with a browser User-Agent but no session cookies: `status=500`,
   `content-type: text/html; charset=utf-8`, body is literally `Error` (5 bytes).
3. **HTTP 200 with silently stale/wrong-date data** — requesting the full-bhavdata CSV for a
   Sunday (2026-08-09, non-trading day): `status=200`, `content-type: text/csv`, 374,452 bytes of
   **real-looking data — but it is the previous Friday's (2026-08-07) file**, served under the
   requested filename with no indication anything is off. This is the case none of the Part A
   libraries defend against: every one of them treats "200 + parseable CSV" as success without
   checking that the `DATE1`/`TradDt` column inside actually matches the requested date.
4. **HTTP 200, JS-shell page with no data** — `www.nseindia.com/get-quotes/equity?symbol=SBIN`
   without proper session bootstrap: `status=200`, `content-type: text/html`, 146,286 bytes of a
   Next.js SPA shell (`<!DOCTYPE html>...`) that fetches its actual data client-side; a naive
   HTML-scrape finds nothing.

**Classification test, executed**: wrote a `classify(resp)` function inspecting status code +
content-type + body-prefix, then wrapped a fetch in `tenacity.retry(..., retry=retry_if_result(...))`
using that classifier as the predicate. Result:
- JS-shell case → classified `JS_SHELL_OR_HTML_ERROR_PAGE` on both attempts, retried per policy,
  then correctly gave up with a clean `RetryError` (transient-looking condition exhausted, not a
  crash).
- 404 dead-URL case → classified `NOT_FOUND`, correctly **not** retried (retrying a 404 is
  pointless), returned immediately.
- Real 200 CSV case → classified `OK`, returned immediately, no wasted retry.

This is mechanically confirmed to work because `tenacity.retry_if_result` (and `backoff`'s
equivalent `giveup=`/`on_predicate` mechanism) accepts an arbitrary Python predicate over the
*parsed response*, not just the status code.

**`urllib3.util.Retry` cannot do this by design** — confirmed by inspecting its constructor
signature directly: `total, connect, read, redirect, status, other, allowed_methods,
status_forcelist, backoff_factor, backoff_max, raise_on_redirect, raise_on_status, history,
respect_retry_after_header, remove_headers_on_redirect, backoff_jitter, retry_after_max`. There is
no parameter that inspects response body or content-type — `status_forcelist` is the only
classification axis, so cases 3 and 4 above (both HTTP 200) are structurally invisible to it. The
`requests`/`urllib3` retry adapter is the wrong composition point for our failure taxonomy; it can
still be layered underneath for pure connection-level retries (DNS, TCP resets, timeouts), but the
Akamai-wall/bot-page/stale-200/JS-shell classification has to happen in application code regardless
of which retry library wraps it.

**`httpx` transport retries** — confirmed via `HTTPTransport.__init__` signature: `retries: int = 0`
is a **connection-level retry count only** (applies to network-level failures before a response is
even received); it has no visibility into status code or body at all. Not sufficient alone, same
conclusion as urllib3.

**`backoff`** — functionally equivalent capability to `tenacity` for this purpose (decorator-based,
accepts a `giveup` predicate over the return value/exception), smaller community and fewer
maintenance signals than `tenacity`. Not tested further once `tenacity` was confirmed to work,
since duplicating the same capability twice adds no value.

**`requests-cache`** — pure caching layer (`CachedSession` wraps `requests`/`httpx` and stores
responses, keyed by request, with configurable expiry). It has no retry or classification logic of
its own; it composes underneath a `tenacity`-wrapped fetch (cache the final good response, not the
transient failures) but does not replace anything above. Useful later purely as a local
HTTP-response cache, orthogonal to this comparison.

**Verdict — B.1**: **ADOPT `tenacity`** as the retry/backoff engine, driven by our own
`classify()` function (status + content-type + body-prefix heuristics, as demonstrated above) —
this is the only tested option whose retry predicate can see the full parsed response, which is a
hard requirement given that 2 of the 4 real failure modes captured this session were HTTP 200.
**REJECT `urllib3.util.Retry` and bare `httpx` transport retries** as the sole retry mechanism —
they are structurally blind to body-based failures; they may still sit underneath tenacity for
raw connection-level retries if desired, but the classification logic itself cannot live there.
`requests-cache` is a separate, optional concern (response caching), not a retry/classification
tool — defer/ADOPT-later, not a decision blocker now.

### B.2 Bitemporal / SCD2 storage

- **`bitemporal`** (PyPI): VERIFIED dead. `pip install bitemporal` fails (`Could not find a
  version that satisfies the requirement`). PyPI's JSON API confirms the project has exactly one
  release entry with **zero attached files** — never actually published. Its own PyPI summary
  reads *"A bitemporality framework for Python 2.5"* — targets a 15+-year-obsolete Python.
  **REJECT.**
- **`temporal-tables`** (PyPI): VERIFIED does not exist — 404 on PyPI. (The real "temporal_tables"
  is a PostgreSQL extension/trigger set, not a Python package; no Python wrapper found under this
  name.) **REJECT / not applicable.**
- **`sqlite-utils`**: VERIFIED installs cleanly (version 4.1.1, released 2026-07-12 — 1 month
  stale, actively maintained, Apache-2.0). It is a general SQLite convenience/CLI library with no
  built-in SCD2/bitemporal primitive — you'd hand-roll the versioning columns and upsert logic on
  top of it exactly as you would with raw `sqlite3`. Useful as a lower-level tool, not a
  bitemporal solution by itself.
- **`deltalake` (delta-rs)**: VERIFIED installs cleanly on **aarch64** — real native manylinux
  wheel (`deltalake-1.6.2-cp310-abi3-manylinux_2_28_aarch64.whl`, 48.8 MB), no source build
  needed, plus its native dependency `arro3-core` also has an aarch64 wheel. **Ran a real
  end-to-end test**: wrote a table with `symbol/trade_date/close/ingested_at`, then used
  `DeltaTable.merge(...).when_matched_update(...).when_not_matched_insert_all().execute()` to
  simulate an NSE price correction arriving the next day. Verified:
  - `dt.history()` returned two real versions (`WRITE` then `MERGE`) with real millisecond
    timestamps — this is genuine, working transaction-time versioning, for free.
  - `DeltaTable(path, version=0).to_pandas()` correctly time-traveled and returned the
    **pre-correction** value (820.5), while `DeltaTable(path).to_pandas()` (latest) returned the
    corrected value (821.0) — confirmed both dimensions are queryable: `trade_date` (business/valid
    time, a modeled column we control) and the delta commit log (transaction/observed time, native
    to the format).
  - This gives us the **transaction-time axis of bitemporality natively** (immutable version log +
    time-travel reads) and a real `MERGE`/upsert primitive for handling corrections — but the
    **bitemporal query semantics themselves** (e.g. "what did we believe about 2026-08-10's SBIN
    close, as of an ingestion run on 2026-08-11 specifically, distinct from the current state") are
    not a built-in query — we still have to compose `trade_date` filtering with `version`/
    `timestamp`-as-of reads ourselves. Delta gives the storage substrate and the correction
    mechanism, not the bitemporal query layer.
- **Verdict — B.2**: **ADOPT `deltalake`** as the storage substrate for the bitemporal store —
  it is the only real, working, aarch64-installable option found (both purpose-built Python
  bitemporal packages are dead/nonexistent), and it mechanically demonstrated real MERGE +
  version-history + time-travel in this session. **REJECT `bitemporal`/`temporal-tables`**
  (nonexistent/unpublished). `sqlite-utils` stays a candidate only as a much lighter-weight
  fallback for small/local tables, not as the primary store — it contributes no bitemporal
  primitive of its own. **Hand-roll** the bitemporal query layer (the `(valid_time, observed_time)
  → value` lookup semantics, and the SCD2 close-out/supersede policy) on top of deltalake's
  merge+history primitives; nothing off-the-shelf provides that layer directly.

---

## Overall recommendation for the ingest core

- **Fetch layer**: `tenacity`, driven by a hand-written `classify()` predicate covering the five
  real failure modes captured above (BLOCKED_WALL / NOT_FOUND / SERVER_ERROR /
  JS_SHELL_OR_HTML_ERROR_PAGE / STALE_OR_WRONG_DATE / OK) — the STALE_OR_WRONG_DATE case requires
  validating the in-body date column against the requested date, which no retry library does for
  us and which is now confirmed to be a real NSE behavior (Sunday request silently served Friday's
  file), not a hypothetical.
- **Cash + F&O bhavcopy adapter base**: vendor-and-adapt `nse` (BennyThadikaran/NseIndiaApi) for
  its correct historical UDiFF handling across the July-2024 boundary; do not trust its output for
  dates it wasn't explicitly tested against without our own schema-validation gate on top.
- **F&O ban-list adapter**: vendor-and-adapt `nselib`'s `fno_security_in_ban_period` — it is the
  only tested wrapper that actually works, though every other data point from that library (its
  bhavcopy functions) must be treated as unreliable/one-era-only.
- **Bitemporal store**: `deltalake`, with our own SCD2/valid-time-vs-transaction-time query layer
  written on top of its merge + version-history primitives.
- **Everything else the nine adapters need** — MWPL position limits, bulk/block deals (the CSV
  endpoints exist and are simple; `nsepython`'s `get_bulkdeals`/`get_blockdeals` are thin working
  examples of the URL pattern but not adopted wholesale), delisted securities, circuit
  bands/ASM/GSM, index constituents, ATM implied volatility, corporate announcements — none of the
  nine candidates provide dedicated, tested, currently-working wrappers for these; they will be
  built directly against NSE's endpoints using the same `tenacity`-based classify-and-retry fetch
  layer.
