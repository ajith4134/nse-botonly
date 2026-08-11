# 204 — Point-in-time universe reconstruction + frozen snapshot

**Tasks:** todo `1.5` and `1.6` — one slice. A frozen snapshot is what the reconstruction *emits*; they are
one engine and its output type, not two features. **Plan:** `L0.05`, `L0.06`.

**R.23(b) classification:** an **engine**. It passes the difference test (`R.23a`):

| Requirement | What it is here |
|---|---|
| Inputs | per-date exchange files — F&O bhavcopy, cash bhavcopy with `series`, MWPL with ISIN, ban list — plus the instrument master |
| Solver / inference | temporal interval resolution over sparse observations, plus a **cross-sectional absence classifier** that decides *why* a symbol vanished |
| Carried state | the accumulated validity intervals and the belief revisions applied to them |
| Output that changes behaviour | the frozen universe a backtest is **allowed** to consider; it gates every scan, signal and order downstream |

**SOTA analogs:** Zipline's `AssetFinder` (`lifetimes()`, `sids` as-of-date) and Qlib's instrument universe
with `start`/`end` bounds. Both keep listing intervals; **neither classifies why an instrument disappeared**,
which is the part this engine has to add because the input is a daily file, not a curated master.

**This closes the R.06/R.11 gap on tasks `1.1`/`1.2`** — it is the named queued consumer of the instrument
master.

---

## 1. The property that matters

**A backtest must consider exactly the instruments that were tradeable on the day it is simulating —
including the ones that no longer exist.** A universe built from today's instrument list contains only
survivors, and every strategy backtested against it inherits an edge it could not have had. Survivorship
bias is not a small correction; it is the reason a dead strategy looks alive.

The hard part is not storing intervals. It is that **the input is evidence of trading, not a statement of
listing**, and absence from a file is ambiguous between at least four causes. The engine's real job is to
tell them apart — and to refuse to guess when it cannot.

## 2. Measured state of the retained evidence (2026-08-10)

The reset preserved per-date exchange files that were not previously accounted for. Measured before
designing:

| Table | Rows | Dates | Coverage |
|---|---|---|---|
| `fo_bhavcopy_contracts` | 1,436,568 | **36** trading days, 2026-06-08 → 2026-08-03 | 216 underlyings, per-contract OI and volume |
| `cash_bhavcopy_delivery` | 16,372 | 5 days, 2026-07-22 → 2026-08-03 | 3,419 symbols, **with `series`** |
| `mwpl_position_limits` | 1,048 | 5 days | 210 underlyings, **with ISIN** |
| `fo_ban_list_symbols` | 1 | 1 day | shape only |
| `bulk_block_deals` | 763 | 5 days | 97 symbols |
| `atm_implied_volatility_daily` | 1,492 | 7 days | 215 underlyings |

**ISIN is clean where present:** 210 symbols ↔ 210 ISINs in MWPL, zero symbols with two ISINs and zero
ISINs with two symbols. It is a viable stable key (`L0.08`) — but only for F&O underlyings, and only across
five days, which is far too short a window for a rename to have occurred. **Not yet evidence that it
survives renames.**

### 2.1 The collection is gapped, and the gaps are not holidays

41 weekdays in the F&O window; **36 collected; 5 weekdays with no file.** My first reading of this counted
all five as collection gaps. **That was wrong, and the calendar proves it** — resolved against
`pandas_market_calendars`' NSE session list:

| Weekday with no file | Verdict |
|---|---|
| `2026-06-26` | **NSE holiday — not a gap at all.** Nothing was published; nothing is missing. |
| `2026-07-28` … `2026-07-31` | **4 genuine missing trading days** — a real collection outage. |

So the outage is **four trading days, not five**. This is exactly the confusion the design exists to
prevent, and I made it myself while writing the spec — which is the strongest possible argument that the
engine must never resolve it by inspection.

**Consequence for the design:** "symbol absent on date D", "no file for date D" and "the exchange was shut
on D" are three different states and must never be conflated. The engine cannot even *name* which dates it
is missing without a session list, so the trading calendar is a **hard, load-bearing dependency**, not a
convenience — and `L0.30` is promoted from a later task to a prerequisite of this one.

### 2.2 Absence from bhavcopy means "did not trade", not "not listed"

324 of 3,419 cash symbols are absent from at least one of the five collected days — and the examples are
government securities (`694GS2036`, `719GS2060`, `750GS2056`), which are listed continuously and simply do
not trade every day. **Presence is evidence of listing; absence is evidence of nothing on its own.** Any
engine that infers delisting from a single missing day would delist most of the G-Sec universe in a week.

### 2.3 The real exit signal: a truncated expiry ladder, measured cross-sectionally

Three F&O underlyings are not present on all 36 dates. Naively, all three look like exits. They are not the
same event, and the discriminator is measurable:

NSE lists **three monthly expiries** for a stock F&O underlying. On 2026-07-27, of 210 stock underlyings:

| Distinct expiries held | Underlyings |
|---|---|
| 3 (the ladder is full) | **207** |
| 2 | 1 |
| **1 (truncated)** | **2 — exactly `EXIDEIND` and `NUVAMA`** |

The exchange stops listing new expiries *before* an underlying leaves F&O, so the ladder shortens as the
outstanding contracts run off. That is the signal:

```
SAMMAANCAP  last seen 2026-06-30, holding ONLY the 2026-06-30 expiry   -> exit, confirmed by runoff
EXIDEIND    last seen 2026-07-27, holding ONLY the 2026-07-28 expiry   -> exit
NUVAMA      last seen 2026-07-27, holding ONLY the 2026-07-28 expiry   -> exit
```

**No new symbol appeared on any date**, so none of the three is a rename — a rename would show a
simultaneous disappearance and appearance.

**The norm is measured, not assumed (R.03e).** "Three expiries" is computed as the cross-sectional mode of
the ladder depth on that date, from that date's own file. It is not written down as a constant, because the
NSE ladder is a regulatory choice that has changed before and will change again.

**And the signal leads the event.** Measured from the first collected date:

| Underlying | Ladder first truncated | Last seen | Lead time |
|---|---|---|---|
| `EXIDEIND` | 2026-06-08 (the first date held) | 2026-07-27 | **≥ 35 trading days** |
| `NUVAMA` | 2026-06-08 | 2026-07-27 | **≥ 35 trading days** |
| `SAMMAANCAP` | 2026-06-08 | 2026-06-30 | ≥ 16 trading days |

Both leads are left-censored — the truncation was already present on the earliest file, so the true lead is
longer. **`DALBHARAT` is truncated to a single expiry on 2026-08-03**, the most recent collected date: a
live prediction the engine emits, testable the moment fresh data arrives.

This is a genuine forward-looking output, not bookkeeping. It belongs to this engine because it falls out
of the same interval reconstruction, and it changes behaviour: an underlying with a running-off ladder must
not be given new multi-expiry positions.

## 3. Design

### 3.1 Three separate questions, never collapsed into one flag

| Question | Answered from | Failure if conflated |
|---|---|---|
| `traded_on(symbol, D)` | direct file presence | — this is the only directly observed fact |
| `listed_on(symbol, D)` | interval reconstruction + absence classification | delists every illiquid G-Sec |
| `eligible_on(symbol, D, segment)` | F&O ladder state, MWPL presence, ban list, `series` | trades a symbol that could not be traded that day |

The API exposes all three. It deliberately does **not** expose a single `is_tradeable` boolean, because the
three have different evidence and different confidence, and collapsing them is how the ambiguity gets
silently resolved in the caller.

### 3.2 The absence classifier

For every (symbol, date) where the symbol is absent from a file that exists, emit a classification **with
its evidence**, never a bare verdict:

| Class | Evidence required |
|---|---|
| `UNOBSERVED` | no file collected for D — the default whenever the date is not in the collection |
| `NOT_TRADED` | symbol present before and after D within the same collection-dense window |
| `EXITED_DERIVATIVES` | ladder truncated relative to that date's cross-sectional mode, then runoff to zero, no reappearance |
| `RENAMED` | disappearance coincident with an appearance carrying the same ISIN |
| `DELISTED` | absent from the cash file across a dense window, with a listing-status source confirming it |
| `UNKNOWN` | the honest default — none of the above discriminates |

**`UNKNOWN` is a first-class outcome, not a failure.** A universe snapshot reports its unknown count, and a
backtest reading a snapshot with unresolved symbols in its date range is told so rather than handed a
clean-looking answer. This is the `O.15` lesson applied at design time: a classifier that never returns
`UNKNOWN` is not classifying, it is guessing.

### 3.3 Bitemporality, inherited from `L0.04`

Two axes, matching the bar store: what was true on D, versus what we *believed* on D. A delisting learned
about three weeks later must not appear in a snapshot dated before we learned it. Snapshots are therefore
addressed by `(as_of_trade_date, known_as_of)` and are **immutable once written** — a revision writes a new
belief, it never edits the old one. `L0.06`'s "frozen" is enforced, not documentary.

### 3.4 Explicitly built rather than borrowed

The interval store and the absence classifier. The classifier especially: no library knows that a shortened
NSE expiry ladder predicts an F&O exit, because that is a fact about this exchange's contract-listing
behaviour, measured here.

## 3.5 The exit signal, validated across 25 years (2026-08-10)

The spec first measured this on **three** events in a 36-day window and said so. With the full archive on
disk (`scripts/validate_expiry_ladder_exit_signal.py`, output in
`/home/opc/nse_archive/manifest/ladder_exit_validation.json`) it was tested properly:

| | |
|---|---|
| Window | 2001-07-02 → 2026-08-10, **6,207 trading days** (stock derivatives began July 2001) |
| Underlyings ever seen | 560 |
| **Genuine exits** | **349** (absent for ≥60 subsequent sessions, so end-of-archive is not mistaken for exit) |
| Warned before exiting | **269 — recall 77.1%** |
| **Missed entirely** | **80 (22.9%)** |
| Warned but never exited | **57 of 343 (~17% false positives)** |
| Lead (sessions) | min 0 · **median 41** · p90 43 · max 44 |

**The median is the mechanism, not a coincidence.** Three monthly expiries ≈ 63 sessions; dropping to two
leaves ≈ 2 months ≈ 42 sessions of runoff. That the measurement lands on 41 with a tight 41-44 spread is
the same fact viewed from the other end, and is the strongest reason to trust it.

**Verdict: a screen, not a forecast.** It misses ~1 exit in 4 and false-alarms ~1 time in 6. Sufficient to
stop opening new multi-expiry positions in a warned underlying; **not** sufficient to be published as a
prediction. `O.21` is corrected in place accordingly.

**And it independently vindicates R.03e.** The cross-sectional ladder norm came out as **3 on 6,160 dates
and 4 on 47 dates**. A hardcoded `3` would have been silently wrong on 47 real trading days — exactly the
class of defect the no-hardcoded-values rule exists to prevent, caught here by measurement rather than
argument.

## 4. Acceptance criteria

1. Reconstructs the universe as of any date in the collected window, from the real 1.4M-row F&O file and
   the real cash file.
2. `SAMMAANCAP` is **in** the 2026-06-29 F&O universe and **out** of the 2026-07-01 universe — the
   survivorship test, on a real event.
3. A symbol absent only on an uncollected date is `UNOBSERVED`, never absent from the universe.
4. An illiquid G-Sec absent for a day remains listed (`NOT_TRADED`), not delisted.
5. `EXIDEIND`, `NUVAMA` and `SAMMAANCAP` are classified `EXITED_DERIVATIVES` with the ladder evidence
   attached; `DALBHARAT` is flagged as a live truncation on 2026-08-03.
6. The cross-sectional ladder norm is computed from the data, with no literal `3` anywhere in the engine.
7. Snapshots are immutable and bitemporal; a later belief never mutates an earlier snapshot.
8. Every snapshot reports its `UNKNOWN` count, and a caller can refuse a snapshot above a tolerance it sets.

## 5. Out of scope

Corporate-action price adjustment (`L0.07`), the delisted-securities master (`L0.09`), gap-detection
backfill (`L0.10`), the trading calendar itself (`L0.30`). This engine *consumes* the calendar and *feeds*
the leakage firewall (`L0.11`).

## 6. Open blockers (R.11 / R.16 — logged, not silently scoped around)

- **Index membership as of a past date** is not present in any retained file. NIFTY 50 / 500 / BANKNIFTY
  constituent history is required by `L0.05` and has no local source yet.
- **Listing status** (as opposed to trading activity) needs a securities master with listing dates; the
  `DELISTED` class cannot be reached without it.
- **The collection window is 36 trading days.** The engine is built to full depth regardless (R.04) —
  thin data gates *activation*, never the algorithm — but the exit classifier's lead-time estimates are
  left-censored and cannot be validated until history extends.
- **The four-day July outage** must be backfilled or permanently recorded as unobserved.

## 7. Sourcing record (R.16 / R.17)

### 7.1 NSE historical sources — swept mechanically, headline claims re-verified independently

**The gating discovery: `nsearchives.nseindia.com` is not Akamai-gated.** `www.nseindia.com` and
`archives.nseindia.com` return 403 to a plain `curl` even with a browser UA and a primed cookie; the
`nsearchives` CDN subdomain — the one NSE's own pages link to — serves clean 200s unauthenticated. Every
row below is an actual fetch with a real byte count and parsed rows, not a documented URL.

| Source | Working URL pattern | Verdict | Mechanical evidence | Depth |
|---|---|---|---|---|
| **Cash bhavcopy (legacy)** | `…/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip` | **ADOPT** | **Re-verified here:** `1995-01-02` → 200, 4,177 bytes, valid ZIP, **203 rows**, header `SYMBOL,SERIES,OPEN,…`. `SERIES` is native — no separate file needed. | **1994-11 → 2024-07-05** |
| **Cash bhavcopy (UDiFF)** | `…/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | **ADOPT** | 200, 171,042 bytes, 3,039 rows, 34 cols incl. `ISIN,TckrSymb,SctySrs`. 404 before 2024-07-01. | 2024-07-01 → present |
| **F&O bhavcopy (legacy)** | `…/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip` | **ADOPT** | **Re-verified here:** `2000-06-12` → 200, 386 bytes, **3 rows** — literally India's F&O launch day. `2015-01-05` → 315,639 bytes, **29,094 rows**. | **2000-06-12 → 2024-07-05** |
| **F&O bhavcopy (UDiFF)** | `…/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | **ADOPT** | **Re-verified here:** `2025-06-02` → 200, 930,990 bytes, **29,538 rows**, header carries `ISIN,TckrSymb,XpryDt`. | 2024-07-01 → present |
| **F&O ban list, dated** | `…/archives/fo/sec_ban/fo_secban_{DDMMYYYY}.csv` | **ADOPT** | 200 at 2015-01-05, 2018-06-04, 2020-03-02 (all `NIL`), 2025-06-02 (real: `1,MANAPPURAM`). A genuine dated archive, not a snapshot. | 2015 (tested) → present |
| **Delisted companies (NSE)** | scrape `/static/list/list-of-companies-proposed-to-be-delisted` → timestamped `.xlsx` | **ADOPT (two-step)** | 200, 61,538 bytes, parsed: **455 rows**, cols `Symbol, ISIN, Company Name, Board, Delisted Date, Type of Delisting`, **2002-04-15 → 2026**. URL is not predictable and must be re-discovered each run. | 2002 → 2026 |
| **Symbol change history** | `…/content/equities/symbolchange.csv` | **ADOPT** | 200, 68,434 bytes, **1,054 rows**, `old_name,old_symbol,new_symbol,date`, **1999 → 2026**. This is the `RENAMED` classifier's evidence. | 1999 → 2026 |
| **Name change history** | `…/content/equities/namechange.csv` | **ADOPT** | 200, 239,693 bytes, **2,322 rows** (ISIN-preserving renames). | multi-year |
| **Securities master** | `…/content/equities/EQUITY_L.csv` (plural `equities`; singular 404s) | **ADOPT — current only** | 200, 170,093 bytes, 2,411 rows, incl. `DATE OF LISTING, ISIN NUMBER`. **No dated version exists** — `EQUITY_L_{DDMMYYYY}.csv` 404s. | today only |
| **F&O lot sizes** | `…/content/fo/fo_mktlots.csv` | **PARTIAL — current only** | 200, 215 rows. Dated variant 404s. | today only |
| **Index constituents** | `…/content/indices/ind_nifty50list.csv`, `…nifty500list`, `…niftybanklist` | **ADOPT — current only** | 200s, 3,352 / 32,766 / 916 bytes, cols incl. `Symbol, ISIN Code`. | today only |
| **Index membership *history*** | `niftyindices.com/IndexConstituent/ind_nifty50list_{DDMMYYYY}.csv` | **REJECT — false positive** | Returns **HTTP 200 with an NSE 404 error page as the body** (`<title>Error 404</title>`). Caught by inspecting content, not status. A status-code-only check would have adopted a source that returns nothing. | — |
| **Index history via circulars** | `www.nseindia.com/api/circulars` (cookie-primed) | **PARTIAL** | 200, 63,866 bytes of real JSON (`cirDate, circCategory, circFilelink`). Date-param variants 500. Yields **PDFs needing per-document parsing**, not structured data. | — |
| **BSE delisted list** | `api.bseindia.com/…/DelEquity/w` | **REJECT — blocked** | 301/302 → `error_Bse.html` under every UA/Origin/Referer combination tried. The HTML report page is a 13,850-byte JS SPA shell with no embedded data. | — |
| **MWPL history** | `fo_mwpl.csv`, `/archives/fo/mwpl/` guesses | **BLOCKED — incomplete sweep** | All guessed patterns 404. Given the ban list *does* live under `/archives/fo/sec_ban/`, a parallel path likely exists and was simply not found. Needs a discovery pass like the one that found the delisted `.xlsx`. | — |

**Python packages — installed and called on aarch64, not read about:**

| Package | Result |
|---|---|
| **`jugaad-data` 0.35.2** | **ADOPT.** `bhavcopy_save` → 2,396×15 real rows; `bhavcopy_fo_save` → 45,840×16. Independently derives the UDiFF cutover as `2024-07-08` — matching the manual `curl` boundary. `index_csv` returns index **price** history only; **no constituent method exists**. |
| **`nselib` 2.5.1** | **ADOPT with one exclusion.** `bhav_copy_with_delivery` → 2,396×15; `corporate_actions_for_equity` → 49 real rows with `isin, exDate`. **`bhav_copy_equities()` returns an empty (0,0) frame — broken, do not use.** |
| **`nsepython` 2.97** | **PARTIAL.** Only the raw `nsefetch` primitive is trustworthy; `equity_history()` raises `KeyError: 'data'` and `nse_eq()` returns `[]`. |
| **`bhavcopy` 3.0** | **REJECT.** Instantiates without error and **downloads zero files** — silently non-functional, the worst failure mode. |
| **`nse-utility`** | **REJECT.** `No matching distribution found`; aliases `nseutility`/`pynse` also absent. |
| **`nsetools` 2.0.1** | **PARTIAL.** `get_stock_codes()` → 2,410 real symbols; `get_quote()` dies on Akamai. |

**What this changes.** The retained window was 36 trading days. The verified archive is **~30 years of
daily cash history and ~26 years of F&O history**, free and unauthenticated. The engine is built to full
depth either way (R.04), but the exit-classifier's lead-time estimates stop being left-censored once the
backfill runs, and every acceptance criterion below can be tested against decades of real de-listings
instead of three.

**Built rather than borrowed, confirmed:** there is **no dated securities master anywhere** — NSE publishes
only current snapshots. Point-in-time listing status must therefore be *derived*: intersect the daily
bhavcopy universe (ground truth for what traded) with `symbolchange.csv`/`namechange.csv` (renames) and the
delisted `.xlsx` (exits). That is precisely the absence classifier in §3.2. **The F&O-bhavcopy-derived
eligibility is the primary source, not a degraded fallback** — no published list with effective dates
exists, so the daily file *is* the ground truth.

### 7.2 OSS interval / point-in-time candidates

Every verdict below is an install plus a real call. The two load-bearing claims — the NSE calendar and the
step-function representation — were **re-verified independently here** before being written down.

| Candidate | Verdict | Mechanical evidence |
|---|---|---|
| **`pandas_market_calendars` 5.4.0** | **ADOPT — load-bearing** | **Re-verified here:** exposes both `NSE` and `XNSE`. Jan-2024 → **22 sessions**, correctly excluding 2024-01-26 (Republic Day). Applied to my own gap list it resolved `2026-06-26` as a holiday and `2026-07-28`…`31` as real missing trading days — **it immediately corrected an error in §2.1 of this spec.** |
| **`exchange_calendars` 4.13.2** | **REJECT — no NSE** | **Re-verified here:** `get_calendar("XNSE")` and `("NSE")` both raise `InvalidCalendarName`; the only Indian calendar is `XBOM` (BSE). Consequential beyond itself: **zipline depends on this library internally**, so zipline's session handling inherits the same gap. |
| **`staircase` 2.8.0** | **ADOPT** | **Re-verified here:** a `Stairs` layered over `SAMMAANCAP`'s real interval sampled `[1, 0, 0]` at 2026-06-29 / 07-01 / 07-15 — correct membership across its actual exit. Half-open natively; universes compose by addition, so "how many instruments were in the universe at time *t*" is one object, not a loop. |
| **`portion` 2.6.2** | **HOLD** | Correct half-open union (adjacent intervals collapse to one atom) and intersection. A lighter alternative if the step-function model proves heavier than needed. |
| **`piso` 1.3.0** | **HOLD — with a named landmine** | `piso.union()` on `closed="both"` intervals **crashes in its own exception handler** (`piso/util.py:10` raises `AttributeError` while converting another error). Correct on half-open. Since bhavcopy date ranges are naturally inclusive, this is precisely the surface we would have hit first. |
| **`pygrametl` 2.9** `tables.SlowlyChangingDimension` | **ADOPT the pattern** | The only SCD2 candidate that mechanically proved it can backfill history: driven twice with caller-supplied dates (2020-01-01, 2020-06-15) it produced correct `valid_from`/`valid_to`/`version` rows keyed to *those* dates. Works against SQLite, already the project's store. |
| **`scd2` 1.0.0** | **REJECT** | Source read plus a real run: `_create_time_parts()` hardcodes `datetime.now(tz)` with **no injection point**. A "delisted" row got stamped with today's wall-clock time instead of the historical date supplied. Unusable for backfilling years of snapshots without forking it. |
| **`bitemporal`** (PyPI) | **REJECT** | No installable release; metadata points at a dead `svn.ervacon.com` link, Python 2.5 era, one release ~2010. |
| **`zipline-reloaded` 3.1.1** | **HOLD as a pattern, do not adopt** | Installs (needs `python3.12-devel`), and `AssetFinder.lifetimes()` genuinely works — a built asset DB correctly returned `False` for a delisted sid on 2020-08-01. But its schema carries exactly **one `start_date`/`end_date` pair per sid**: no re-listing, no bitemporality, and it *consumes* known intervals rather than reconstructing them from sparse observations, which is the actual problem here. It also **downgrades pandas 3.0.5 → 2.3.3**, colliding with `vectorbt`. Imitate the vectorised dates×symbols boolean mask; take nothing else. |
| **`qlib` / `pyqlib` 0.9.7** | **REJECT — unavailable on aarch64** | PyPI lists wheels for `manylinux2014_x86_64`, `macosx`, `win_amd64` only — **zero aarch64 wheels and zero sdist**. No install path exists on this box. *Note: it remains a valid SOTA analog for §0's design comparison; it simply cannot be a dependency here.* |
| **`nautilus_trader` 1.231.0** | **REJECT — unbuildable here** | Publishes `manylinux_2_35_aarch64`; this OS has **glibc 2.34**, so pip falls back to sdist. Two distinct terminal failures across two attempts: missing `clang`, then — after installing clang and cargo — `cargo build` exit 101 because the sdist omits `examples/tutorials/Cargo.toml` referenced by its own workspace manifest. A packaging defect upstream, not fixable from here. |
| **`vectorbt` 1.1.0**, **`backtrader` 1.9.78** | **REJECT for this problem** | Grep across each package for `delist\|survivorship\|point_in_time\|lifetime`: **zero hits** in vectorbt; one English-prose false positive in backtrader. Neither has any universe machinery — both consume whatever price array they are handed. |
| **`openfigi` 0.0.9** | **HOLD — wrong question** | The live API is real (a raw POST returned 4 genuine FIGI records for Reliance across NSE/BSE), but it answers "what maps to this ISIN **now**", not "was this tradeable on D". Right tool for symbol cross-referencing (`L0.08`), wrong shape here. |
| **`pyopenfigi` 0.1.0** | **REJECT** | Installs, fails to import: `TypeError: To define root models, use pydantic.RootModel rather than a field called '__root__'` — pydantic v1 syntax against the v2 installed here. |
| **`investpy` 1.0.8** | **REJECT** | Fails to import out of the box (`ModuleNotFoundError: pkg_resources`); pinning `setuptools<81` made it work and it returned 1,711 real Indian stocks — but it is a **current static list** with no historical dates, scraped through a deprecated packaging API. |

### 7.3 Calendar component — corrections after adversarial review

The session calendar (`L0.30`/`L0.30a`, `src/nse_algo_trader/nse_trading_session_calendar.py`) went through
the full R.23 loop. A fresh sonnet reviewer found **9 defects and 8 surviving mutants** in code that had
already passed ruff, mypy and 34 tests. The three that mattered:

**① `is_trading_session` reported real trading days as closed.** `datetime` and `pandas.Timestamp` are
subclasses of `date`, so the `isinstance` guard admitted them; membership against a set of `date` then
failed for the same calendar day. Reproduced: `date(2024,1,30)` → `True`, `pd.Timestamp("2024-01-30")` →
`False`. The library itself hands back `Timestamp`, so this input is routine. Now refused by name, with a
message telling the caller to resolve `.date()` in a timezone of their choosing rather than being coerced
silently (`O.25`).

**② The reliability check never reached the method that needed it.** `classify_observed_dates` had no
gating, so in a year with no holiday rules every unrecognised holiday landed in `uncollected_sessions`
beside genuine gaps. Reproduced: an empty 1993 collection returned **261 "uncollected sessions" at 0.0
completeness**, Republic Day among them — a total outage reported for a year that was merely unverified.
The report is now a **three-way partition** — observed / uncollected / **unverifiable** — and
`collection_completeness` returns `None` rather than a number it cannot justify.

**③ `unreliable_years_between` carried the silent-empty bug its own sibling guards against.**
`unreliable_years_between(2025, 1993)` returned `()`, reading as "no unreliable years".

Also fixed: `_sessions_in_year` re-derived a full year per call (~5 ms measured) and is now cached;
`collection_completeness`, `has_disagreement`, `describe()` and both `_require_supported` branches were
entirely untested; the comment justifying the 1990-2030 span was factually wrong (the library answers for
1800 and 2035 — with spurious results, which is precisely *why* the bound is ours); and a weekday filter in
the holiday count was dead code that read as a working guard.

**Mutation: 11 of 11 killed**, including all 8 previous survivors. The reliability rule was extracted as
`holiday_count_is_credible(count, fence)` because inline it was unfalsifiable — no real year's count lands
on the float-valued fence, so `>=` versus `>` could not be distinguished by any fixture (`O.26`).

**Conclusion: the interval-resolution core is built, not borrowed** — and this is now a measured verdict rather
than a preference. No library distinguishes *genuinely absent* from *not collected*; that separation is a
three-way join between a real session calendar, this project's own ingestion log, and an interval
representation. `staircase` supplies the third; `pandas_market_calendars` supplies the first; the ingestion
log and the absence classifier are ours.
