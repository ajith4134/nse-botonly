# 207 — NSE Ingest Source Reconnaissance (mechanical, verified-only)

Date of reconnaissance: 2026-08-11 (system clock). All fetches below were performed live with `curl` from this
machine's outbound network path, using a real browser `User-Agent` string:
`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36`.
No application code was written. Every fact below is tagged **VERIFIED** (I fetched it and observed the result
directly) or **UNVERIFIED / BLOCKED** (I attempted the fetch and it failed, or I could not find a working URL and
am not guessing). Nothing is reported from memory as fact.

## 0. Cross-cutting platform finding (applies to every source below)

NSE's data surface splits into two very different access tiers, and this split governs everything else in this
document:

- **`nsearchives.nseindia.com` and `archives.nseindia.com`** (static file archives) — **VERIFIED free and
  unauthenticated**. A plain `curl` with only a browser `User-Agent` header succeeds. No cookie priming, no
  Referer, no session needed. Confirmed across dozens of fetches (bhavcopy, ban list, bulk/block deals, sec
  list, delisted list, MWPL archive).
- **`www.nseindia.com`** (the live dynamic site/API, e.g. option-chain, ASM report page, historical large-deals
  API) — **VERIFIED heavily and inconsistently gated** from this machine:
  - `GET https://www.nseindia.com/` (homepage, used to prime cookies) → **VERIFIED HTTP 403**, Akamai
    `AKA_A2` cookie only, "Access Denied" page. Reproduced twice, including after cookie reuse.
  - `GET /api/historical/bulk-deals?...` → **VERIFIED HTTP 503**, Apache-served "Unable to process your
    request" bot-block page (a different block page from the Akamai one, but still a block, not real data).
  - `GET /api/option-chain-indices?symbol=NIFTY` → **VERIFIED HTTP 404** "Resource not found" (Apache-served,
    a *third* distinct failure mode — looks like the route requires a session established by first loading the
    SPA, not a hard bot-block). Adding a `Referer: https://www.nseindia.com/option-chain` header did not help.
  - `GET /reports/asm` → **VERIFIED HTTP 200** but is the React/Next.js SPA shell only; the ASM table is
    populated client-side by JS I cannot execute with `curl`, so no data was recoverable.
  - `GET /api/corporate-announcements?index=equities` → **VERIFIED HTTP 200 with live real data**, no cookie
    needed at all. This is the one dynamic endpoint that worked.
  - **Conclusion (VERIFIED, unexplained): blocking on `www.nseindia.com` is inconsistent per-endpoint**, not a
    blanket bot-wall. Some `/api/*` routes answer plainly; others 403/503/404 regardless of headers. I did not
    find a pattern (tried UA, Referer, Accept-Language, cookie-priming) that reliably unlocks the blocked ones
    from this machine's IP.

Given this, every source whose only known feed lives on `www.nseindia.com/api/*` and which returned a block above
is marked **BLOCKED** below, not silently assumed reachable.

---

## 1. `delisted_securities_master`

- **URL (VERIFIED, HTTP 200):** `https://archives.nseindia.com/content/equities/delisted.csv`
- **Auth:** none required — plain UA-only GET succeeded.
- **Format:** CSV, single flat file (not chunked by year/date).
- **Header row (VERIFIED, exact):**

| Symbol | Company | Delisted Date | Type of Delisting | (5 trailing empty columns) |
|---|---|---|---|---|

  Raw header line as fetched: `Symbol,Company,Delisted Date,Type of Delisting,,,,,`
- **Sample rows (VERIFIED, exact):**
  ```
  CABOTINDIA,Cabot India Ltd,15-Apr-02,Voluntary Delisting ,,,,,
  ANIKSHIP,Anik Ship Breaking Industries Ltd.,26-Jul-04,Compulsory Delisting ,,,,,
  HEXAWARE,Hexaware Technologies Limited,09-Nov-20,Voluntary Delisting ,,,,,
  ```
- **Earliest date observed in file:** 15-Apr-2002 (earliest row seen; I did not verify this is the true minimum
  across the whole file — I only visually scanned head/tail, did not sort the full column). **UNVERIFIED** as an
  absolute floor.
- **Update cadence:** **UNVERIFIED** — no `Last-Modified` header was returned by the server for this file
  (`curl -I` showed only `content-length: 280` for the headers-only request, which is odd — the HEAD response
  content-length does not match the GET body size of 24,715 bytes, suggesting the server may not handle HEAD
  correctly for this resource; treat HEAD results on this endpoint as unreliable, use GET only).
- **NSE-side cross-reference for context (VERIFIED, related but distinct file):**
  `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` — HTTP 200, this is the **currently listed**
  securities master (not delisted), header: `SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
  MARKET LOT, ISIN NUMBER, FACE VALUE`. Useful as the complement set to delisted.csv, not a delisting feed
  itself.
- **Rate limiting:** none hit.
- **Free/unauthenticated:** **VERIFIED yes.**

---

## 2. `nse_bhavcopy` (cash + F&O, full-archive URL patterns + the 2024 format change)

### 2a. Cash market (CM) — old format

- **URL pattern (VERIFIED):** `https://archives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip`
- **Example fetched successfully:** `.../2024/JAN/cm01JAN2024bhav.csv.zip` → HTTP 200, 100,666 bytes (zip).
- **Header row (VERIFIED, exact, from the unzipped CSV):**
  `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,`
- **Last date this old-format URL still resolves (VERIFIED):** `cm05JUL2024bhav.csv.zip` → HTTP 200
  (Friday 5 Jul 2024). The very next trading day, `cm08JUL2024bhav.csv.zip` (Monday 8 Jul 2024) →
  **VERIFIED HTTP 404**. Also confirmed 404 for `cm15JUL2024bhav.csv.zip` and `cm01OCT2024bhav.csv.zip`.
  **Verified changeover boundary: the old CM path was retired between 2024-07-05 and 2024-07-08.**

### 2b. Cash market (CM) — new UDiFF format

- **URL pattern (VERIFIED):** `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`
- **Examples fetched successfully:** `20240701` (166,236 bytes), `20240705` (164,296 bytes), `20240708`
  (166,236 bytes), `20260810` — the most recent trading day before this reconnaissance — (198,559 bytes, all
  HTTP 200).
- **Header row (VERIFIED, exact):**
  `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4`
- **Sample row (VERIFIED, exact):**
  `2024-07-01,2024-07-01,CM,NSE,STK,3010,INF846K01Y96,AXISTECETF,EQ,,,,,AXISAMC - AXISTECETF,382.00,391.82,381.80,390.48,389.25,382.08,,390.48,,,17444,6781410.83,467,F1,1,,,,,`
- **Important verified nuance:** the new UDiFF URL was **also fetched successfully for a date well before the
  July 2024 changeover** — `BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv.zip` → HTTP 200, 151,548 bytes. **NSE has
  back-filled the new unified format across historical dates on `nsearchives.nseindia.com`**; the old-format
  path on `archives.nseindia.com` is the one that was actually decommissioned, not the underlying data. Anyone
  building an ingest pipeline should prefer the new UDiFF path for ALL dates and can likely ignore the old path
  entirely (unverified how far back the UDiFF backfill goes — I only probed 2024-01-01).
- **Last-Modified header (VERIFIED)** for the most recent file (`20260810`): `Mon, 10 Aug 2026 11:05:03 GMT`,
  content-length 198,559 — establishes the file for Monday 2026-08-10's session was already published by
  11:05 GMT (~16:35 IST) the same day it's dated, i.e. bhavcopy for a session is not published same-day at
  market close but the next observation point I have; I did not capture intraday polling to pin down the exact
  publish time — **cadence/time-of-day is VERIFIED only to "available by the next morning I checked", not to a
  precise HH:MM window.**

### 2c. Cash market — earliest date reachable (old-format path, `archives.nseindia.com`)

Binary-searched by year, same day-of-month probes each time:

| Year probed | Result |
|---|---|
| 2026, 2024, 2023, 2022, 2020, 2018, 2017, 2016 | **VERIFIED HTTP 200** |
| 2015, 2014, 2012, 2010, 2005, 2001, 2000, 1996, 1995 | **VERIFIED HTTP 403** (Akamai "Access Denied", genuine block page — reproduced on retry, not a transient rate-limit: retried 2024/2010/2005 back-to-back and got the same 200/403 split every time) |

**Verified boundary: 2016-01-04 works, 2015-12-01 and earlier are blocked from this machine.** I cannot
determine from here whether this is a true data-availability floor or an IP/environment-specific Akamai rule
scoped to older paths — NSE publicly claims data back to 1994, but I could not fetch any of it. Reporting this
as **BLOCKED, not absent** — do not treat "no data before 2016" as fact, only "no data before 2016 fetchable
from this machine."

### 2d. F&O (derivatives) bhavcopy — old format

- **URL pattern (VERIFIED):** `https://archives.nseindia.com/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip`
- **Example fetched:** `.../2024/JAN/fo01JAN2024bhav.csv.zip` → HTTP 200, 728,713 bytes.
- **Header row (VERIFIED, exact):**
  `INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,`
- **Sample row:** `FUTIDX,BANKNIFTY,25-Jan-2024,0,XX,48580,48869,48389.4,48535.7,48535.7,120422,878243.28,2052195,-25530,01-JAN-2024,`

### 2e. F&O bhavcopy — new UDiFF format

- **URL pattern (VERIFIED):** `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`
- **Example fetched:** `20240701` → HTTP 200, 1,092,193 bytes.
- **Header row:** identical schema to the CM UDiFF file (same 34 unified columns — segment `Sgmt` distinguishes
  `FO` rows). **Sample row (VERIFIED, exact):**
  `2024-07-01,2024-07-01,FO,NSE,STO,128701,,MARUTI,,2024-07-25,2024-07-25,11000.00,PE,MARUTI24JUL11000PE,40.60,40.60,19.40,29.85,31.00,38.25,12108.60,29.85,143000,28300,3100,1709339217.50,2225,F1,50,,,,,`
  — note options rows carry `ISIN` blank and strike/option-type populated; futures rows carry `STRIKE_PR`-style
  field blank. I did not separately re-verify the exact old→new changeover date for the F&O leg (only checked
  CM); given both CM and FO UDiFF files share the same unified schema and publishing infrastructure, it is
  **UNVERIFIED but plausible** that the FO changeover date matches the CM one (2024-07-08) — flagging as an
  assumption, not a fact.

### 2f. Rate limiting / blocking behavior observed for bhavcopy fetches

- No 429s or throttling seen across ~35 sequential bhavcopy-family requests spaced 1–2s apart.
- All blocks encountered were binary (200 or 403/404), tied to path/date, not to request rate.

---

## 3. `mwpl_position_limits`

- **URL pattern (VERIFIED):** `https://nsearchives.nseindia.com/archives/nsccl/mwpl/nseoi_{DDMMYYYY}.zip`
- **Working examples (VERIFIED HTTP 200):** `03012011` (13,713 B), `02012012` (13,675 B), `02012013`
  (9,308 B), `03012017` (11,050 B), `02012024` (12,322 B), `01022024`, `01032024`, `01042024`, `15042024`,
  `22042024`, `26042024`, `29042024`, `30042024` — all 200.
- **Format:** ZIP containing a CSV and an equivalent XML (e.g. `nseoi_02012024.csv` + `.xml`).
- **CSV header row (VERIFIED, exact):** `Date, ISIN, Scrip Name, NSE Symbol, MWPL, NSE Open Interest`
- **Sample row (VERIFIED):** `02-JAN-2024,INE180A01020,MAX FINANCIAL SERV LTD,MFSL,64519703,6976800`
- **Verified end-of-life boundary:** last working date at this path is **30-Apr-2024** (HTTP 200); the very
  next probed date, **01-May-2024, returns HTTP 404**, as do every later date tried: `03-Jun-2024`,
  `01-Aug-2024`, `02-Dec-2024`, `30-Dec-2025`, `15-Jan-2026`, `01-Jul-2025`, `01-Jan-2026`, and the two most
  recent trading days (`07-Aug-2026`, `10-Aug-2026`) — **all VERIFIED 404**.
- **Successor location: NOT FOUND.** I tried these guessed patterns, all **VERIFIED HTTP 404** (genuine
  not-found body, not a block page):
  - `https://nsearchives.nseindia.com/content/fo/MWPL_20240701.csv`
  - `https://nsearchives.nseindia.com/content/nsccl/mwpl_20240701.csv`
  - `https://nsearchives.nseindia.com/content/fo/mwpl_client_20240701.csv`
  - `https://nsearchives.nseindia.com/archives/nsccl/mwpl/nseoi_dd20240701.zip`
  I did not find the current/live MWPL feed's URL. The likely real location is behind `www.nseindia.com`'s
  gated dynamic API (same tier that blocked option-chain and historical bulk-deals above), but I have no
  evidence of the specific path — **reporting as BLOCKED/NOT LOCATED, not guessing a URL.**
- **Earliest date:** **VERIFIED working at 2011-01-03**; 2010-01-04 and 2005-01-03 both returned 404 — could
  not confirm whether 2010/2005 fail because the archive genuinely starts later than 2011, or because those
  specific dates were non-trading days/wrong filename convention for that era. **Earliest-date claim is
  therefore soft: "at least back to 2011-01-03," not a confirmed hard floor.**
- **Free/unauthenticated:** VERIFIED yes for the legacy archive path.

---

## 4. `fo_ban_list`

- **URL (VERIFIED, HTTP 200):** `https://nsearchives.nseindia.com/content/fo/fo_secban.csv`
- **Format:** CSV-like but **not a standard header/rows CSV** — first line is a descriptive sentence, not a
  column header:
  ```
  Securities in Ban For Trade Date 11-AUG-2026:
  1,BANDHANBNK
  2,SAIL
  ```
  (Verified live, dated the actual day of this reconnaissance — 2 symbols in ban that day.) Parsing this
  programmatically requires special-casing line 1 (extract the trade date via regex/split on
  `"For Trade Date "`/`":"`) rather than treating it as a normal CSV header.
- **Historical access:** **UNVERIFIED / not found.** This URL has no date parameter and I found no evidence of
  a per-date archive for this specific file (unlike bhavcopy). It appears to be a single rolling
  "today's ban list" file, overwritten daily.
- **Last-Modified (VERIFIED):** `Mon, 10 Aug 2026 12:28:02 GMT` on the fetch that returned Monday's (10-Aug)
  ban list before Tuesday's had posted — i.e., **UNVERIFIED exact publish time**, but this shows the file is
  stamped/updated same-day, consistent with an intraday-refreshed ban list (F&O ban status is legally required
  to be published each trading day once MWPL crosses 95%).
- **Free/unauthenticated:** VERIFIED yes.

---

## 5. `bulk_block_deals`

- **Bulk deals URL (VERIFIED, HTTP 200):** `https://archives.nseindia.com/content/equities/bulk.csv`
- **Block deals URL (VERIFIED, HTTP 200):** `https://archives.nseindia.com/content/equities/block.csv`
- **Bulk header (VERIFIED, exact):**
  `Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks`
- **Bulk sample row (VERIFIED):**
  `10-AUG-2026,AASTHA,Aastha Spintex Limited,JAGID VANITABEN RAJENDRAPRASAD,BUY,348313,73.43,-`
- **Block header (VERIFIED, exact, no Remarks column):**
  `Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price`
- **Block sample row (VERIFIED):**
  `10-AUG-2026,EIDPARRY,EID Parry Ltd.,SBI MUTUAL FUND,BUY,2215000,795.00`
- **Verified fact about scope:** both files contain **only the single most recent trading day** — I confirmed
  by extracting the unique `Date` values from the fetched `bulk.csv`, which returned exactly one distinct date
  (`10-AUG-2026`). **These are rolling latest-day snapshots, NOT historical archives.**
- **Historical/date-ranged access attempted:**
  `https://www.nseindia.com/api/historical/bulk-deals?from=01-08-2026&to=10-08-2026` →
  **VERIFIED HTTP 503**, Apache "Service Unavailable / maintenance downtime" block page — **BLOCKED** from
  this environment, real data not obtained. Same result pattern for the equivalent `block-deals` and
  `short-selling` endpoints (not separately re-tested but same base API and same block observed on the sibling
  bulk-deals call).
- **Free/unauthenticated:** the static rolling CSVs — VERIFIED yes, no cookie needed. The historical API — VERIFIED blocked regardless of headers.

---

## 6. `atm_implied_volatility`

- **Attempted URL:** `https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY`
- **Result: VERIFIED HTTP 404** ("Resource not found", Apache-served page), reproduced with and without an
  added `Referer: https://www.nseindia.com/option-chain` header. This is a different failure signature than the
  Akamai 403 seen on the homepage and different from the 503 seen on historical/bulk-deals — suggests this
  specific route may require a session/token minted by first loading the SPA in a real browser, which `curl`
  cannot replicate.
- **No static/archived alternative found.** Unlike bhavcopy, ban lists, and bulk deals, NSE does not appear to
  publish option-chain or IV snapshots as static files on `nsearchives.nseindia.com` — I searched but found no
  such path, and did not fetch one.
- **Conclusion: BLOCKED, no data obtained, no column schema verified.** Any downstream ATM-IV feature
  depending on this source needs either (a) a real headless-browser session with cookie/token capture, or
  (b) a third-party vendor, or (c) deriving IV from the F&O bhavcopy's `SttlmPric`/`UndrlygPric` fields via
  your own Black-Scholes inversion — the bhavcopy fields needed for that (`StrkPric`, `OptnTp`, `SttlmPric`,
  `UndrlygPric`, `XpryDt`) **are** present and verified in section 2e above, so a bhavcopy-derived EOD ATM-IV
  series is buildable even though the live NSE option-chain snapshot API is not reachable from here.
- **Free/unauthenticated:** N/A — could not reach it to test.

---

## 7. `circuit_band_asm_gsm`

- **URL (VERIFIED, HTTP 200):** `https://nsearchives.nseindia.com/content/equities/sec_list.csv` (161,622
  bytes; `Last-Modified: Mon, 10 Aug 2026 13:53:10 GMT`)
- **Header (VERIFIED, exact):** `Symbol,Series,Security Name,Band,Remarks`
- **Sample rows (VERIFIED, exact):**
  ```
  21STCENMGM,EQ,21ST CENTURY MANAGEMENT SERVICES LIMITED,2,"-"
  ANSALAPI,BZ,ANSAL PROPERTIES & INFRASTRUCTURE LIMITED,2,"GSM STAGE - I"
  EUROTEXIND,BE,EUROTEX INDUSTRIES AND EXPORTS LIMITED,2,"GSM STAGE - 0"
  ```
- **`Band` column = the circuit band/price band %** for that symbol (verified present, values like `2`, `5`,
  `20` observed across the file, consistent with circuit-band percentage conventions — I did not cross-check
  the numeric meaning against an NSE circular, so treat "Band = circuit percentage" as a reasonable but
  **UNVERIFIED interpretation** of the column's exact semantics beyond what the column name states).
- **GSM stage:** carried inside the free-text `Remarks` column, not a dedicated column. **Exhaustive set of
  distinct `Remarks` values found in the whole file (VERIFIED, by full-column dedup):** `"-"`, `"-\t"`,
  `"GSM STAGE - 0"`, `"GSM STAGE - I"`, `"GSM STAGE - II"`. No `GSM STAGE - III` or `IV` symbols existed in
  this particular snapshot — that's a fact about today's data, not a schema limit.
- **ASM stage: NOT present in this file.** I grepped the full file for ASM-related remarks and found none — the
  only "ASM" substrings present are coincidental (ticker/company names containing the letters ASM, e.g.
  symbol `ASM`, `ASML`, `ASMS`, unrelated to surveillance status).
- **ASM data source attempted separately:**
  - `https://www.nseindia.com/reports/asm` → **VERIFIED HTTP 200** but is a client-rendered SPA shell with no
    inline data table (confirmed by grepping the HTML for `csv`/`Download`/`__NEXT_DATA__` — a `Download` link
    exists in the markup but the actual CSV endpoint it calls is populated via client-side JS I cannot execute).
  - `https://www.nseindia.com/api/reports/asm` (guessed) → **VERIFIED HTTP 404** "Resource not found".
  - > **CORRECTION 2026-08-11 — this BLOCKED verdict was WRONG. ASM is fully reachable.**
    > The endpoint is `https://www.nseindia.com/api/reportASM` (capital `ASM`), which returns
    > **HTTP 200, `application/json`, 50,270 bytes, with ZERO cookies**, reproduced on a fresh
    > connection with byte-identical content: `{"longterm": {"data": [...128]}, "shortterm":
    > {"data": [...61]}}` — 189 entries in total.
    > **How it was found, which is the transferable part:** this document guessed `/api/reports/asm`
    > from the page path and recorded 404. The correct method is to read what the page's own
    > JavaScript calls — fetching `/dist/js/sections/reports/asm.js` shows `B.get('/api/reportASM')`
    > outright. A single-page app is not a wall; it is a client whose API calls are readable.
    > The original text is left below unaltered, per the correct-in-place rule.
    >
    > **This casts doubt on the other BLOCKED verdicts in this document** (§6 ATM IV, §5 historical
    > bulk deals), each of which was also reached by guessing an endpoint rather than reading the
    > client. They should be re-tested by the same method before being trusted.

  - **Conclusion: ASM stage is BLOCKED/NOT LOCATED from this environment.** Only circuit band + GSM stage were
    obtained; ASM needs either browser automation against `/reports/asm` or the correct (unknown to me) API
    path.
- **Free/unauthenticated:** for `sec_list.csv` — VERIFIED yes, no cookie needed.

---

## 8. `index_constituents_weights`

- **Constituent list URL (VERIFIED, HTTP 200):** `https://niftyindices.com/IndexConstituent/ind_nifty50list.csv`
  (3,352 bytes) and `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv` (32,766 bytes).
- **Mirrored on NSE's own archive (VERIFIED, HTTP 200, identical schema):**
  `https://archives.nseindia.com/content/indices/ind_nifty50list.csv`
- **Header (VERIFIED, exact):** `Company Name,Industry,Symbol,Series,ISIN Code`
- **Sample row (VERIFIED):** `Adani Enterprises Ltd.,Metals & Mining,ADANIENT,EQ,INE423A01024`
- **Weights: NOT obtained.** This is a constituent *membership* list only — **there is no weight/percentage
  column in this file.** I attempted one guessed alternate endpoint,
  `https://niftyindices.com/Backpage.aspx/getConstitutents`, which **VERIFIED returned HTTP 200 but with a
  genuine 404-branded HTML error page body** (not JSON, not real data) — a wrong guess, not a block.
  I did not find niftyindices.com's actual weight-bearing endpoint (their "Historical Index Data with
  weightage" reports appear to be served through an interactive JS/POST-driven report picker on
  niftyindices.com/reports, which a plain `curl` cannot drive).
- **Conclusion: constituent membership lists are VERIFIED free/unauthenticated; per-constituent index WEIGHTS
  are UNVERIFIED / NOT LOCATED** via any endpoint tried. Getting weights will likely require either browser
  automation of the niftyindices.com reports UI, or deriving approximate weights yourself from free-float
  market cap using the bhavcopy `ClsPric` × shares-outstanding (shares-outstanding itself not verified as
  available from any source checked here).

---

## 9. `corporate_announcements`

- **URL (VERIFIED, HTTP 200):** `https://www.nseindia.com/api/corporate-announcements?index=equities`
- **Notable:** this is the **one** `www.nseindia.com/api/*` endpoint that worked from this environment with
  zero cookie priming and just a UA header — inconsistent with the blocking seen on sibling endpoints (see
  section 0). Not explained; just observed and reproducible at the time of this test.
- **Format:** JSON array of announcement objects.
- **Fields observed (VERIFIED, exact key names from a live real response, 13,316 bytes for the first page):**
  `an_dt, attFileSize, attchmntFile, attchmntText, bflag, csvName, desc, difference, dt, exchdisstime,
  fileSize, hasXbrl, old_new, orgid, seq_id, smIndustry, sm_isin, sm_name, sort_date, symbol`
- **Sample record (VERIFIED, exact, real live data matching today's date):**
  ```json
  {
    "an_dt": "11-Aug-2026 09:41:17",
    "attFileSize": "726.12 KB",
    "attchmntFile": "https://nsearchives.nseindia.com/corporate/ESCORTS2_11082026093836_EKL_AgriTractor_Price_Increase_August_26_Signed.pdf",
    "attchmntText": "Escorts Kubota Limited has informed the Exchange regarding 'Announcement under Regulation 30 (LODR) - Updates'.",
    "desc": "Updates",
    "seq_id": "106735961",
    "sm_isin": "INE042A01014",
    "sm_name": "Escorts Kubota Limited",
    "sort_date": "2026-08-11 09:41:17",
    "symbol": "ESCORTS"
  }
  ```
  Note the `attchmntFile` field points back into `nsearchives.nseindia.com/corporate/...` for the actual PDF —
  that sub-path was not independently fetch-tested but follows the same free-archive domain pattern verified
  working everywhere else in this document, so it is very likely also fetchable (UNVERIFIED, not tested).
- **Historical/date-ranged access, cadence, rate limits:** **UNVERIFIED** — I fetched the endpoint once,
  unparameterized (`index=equities`, no date range), and got the current day's live stream. I did not test
  whether `from`/`to` query params work, did not probe for pagination, and did not send repeated requests to
  check for rate-limiting on this specific endpoint.
- **Free/unauthenticated:** **VERIFIED yes**, at least for this single unparameterized call.

---

## Appendix: OSS sourcing search performed (Rule I / sourcing gate)

This task was mechanical reconnaissance (curl-verify endpoints), not a feature build, so no library was
vendored. But endpoint discovery for sources without an obvious static-archive path (MWPL successor, ASM, bulk
deal history, option chain) was grounded in an actual OSS search rather than guessed from memory. Searches run
and repositories evaluated:

- **Search queries run (via WebSearch):** `nseindia.com "fo_mwpl" OR "mwpl" archive csv url site:github.com`;
  `nsearchives.nseindia.com fo_secban.csv OR "securities in ban" F&O ban list url`;
  `nseindia.com bulk deals block deals archive csv url pattern historical`;
  `niftyindices.com historical index constituents weights csv download url`;
  `"nseindia.com" "mwpl" report file name daily "position limit" derivatives download`;
  `jugaad-data OR nselib python github MWPL market wide position limit url`;
  `"archives.nseindia.com" OR "nsearchives.nseindia.com" "bulk_deals" OR "bulk.csv" OR "block.csv" historical file`;
  `"ASM" "GSM" NSE surveillance csv download url nsearchives OR archives.nseindia.com`;
  `nseindia.com "eq_security" OR "delisted" securities list csv archive "compulsory delisting" download`;
  `github hi-imcodeman stock-nse-india nseindia.com api endpoints "mwpl" OR "corporate-announcements" OR "circulars"`.
- **Repos/pages fetched and evaluated directly (not just search snippets):**
  - `github.com/hi-imcodeman/stock-nse-india` (TypeScript NSE API wrapper) — pulled its GitHub tree
    (`api.github.com/repos/hi-imcodeman/stock-nse-india/git/trees/master?recursive=1`) and read
    `src/routes.ts` and `src/constants.ts` directly. **Result: no MWPL/ASM/GSM/bulk-deal/delisted endpoints
    defined in this library** — it only wraps option-chain, equity-details, and circulars. Not usable as a
    source of the missing endpoints; ruled out.
  - `bennythadikaran.github.io/NseIndiaApi/api.html` (Python NSE API wrapper docs, `BennyThadikaran/NseIndiaApi`)
    — fetched the rendered docs page. Confirmed the bulk/block/short-selling deal sample-JSON file names and
    the `archives/equities/bhavcopy/pr/PR{ddmmyy}.zip` PR-bhavcopy pattern, but the docs explicitly do **not**
    disclose MWPL/ASM/GSM/delisted URLs either.
  - `unofficed.com/nse-python/nse-large-deal-historical-api/` and `.../nse-large-deal-api/` — fetched directly;
    yielded the exact `www.nseindia.com/api/historical/{bulk-deals|block-deals|short-selling}` and
    `www.nseindia.com/api/snapshot-capital-market-largedeal` endpoint shapes, which I then curl-tested myself
    (section 5) and found blocked (503) from this environment — the endpoint shape is correct/real, access is
    what's blocked.
  - `jugaad-data` (`github.com/jugaad-py/jugaad-data`) and `nselib` (`github.com/RuchiTanmay/nselib`) — surfaced
    by search but not fetched in depth; search snippets contained no MWPL/ASM specifics, so not pursued further
    given time already spent finding a dead end on the legacy MWPL successor path.
- **Why nothing was vendored:** the task explicitly forbids writing application code ("Do NOT write any
  application code"); the point of this search was purely to find real, citable NSE URL patterns to then
  curl-verify myself, not to adopt a dependency. Every endpoint used or cited in this document was independently
  fetch-tested (section 1–9), not taken on the word of a third-party library.
- **Open gap surfaced by this search, not resolved:** the current (post-2024-04-30) MWPL feed and the raw
  ASM CSV feed were not found in any of the above libraries either — this is a genuine unresolved gap, not a
  skipped search. This task's instructions restricted me to writing only this one file, so the gap is recorded
  here (and in the summary table below) rather than in `docs/BACKLOG.md`; the caller should transcribe it to
  `docs/BACKLOG.md` per Rule K if this recon is accepted as final.

## Summary table

| # | Source | Status | Working example URL | Format |
|---|---|---|---|---|
| 1 | delisted_securities_master | VERIFIED | `archives.nseindia.com/content/equities/delisted.csv` | CSV |
| 2 | nse_bhavcopy (CM+FO, old+new) | VERIFIED (both eras + changeover boundary pinned) | `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260810_F_0000.csv.zip` | CSV in ZIP |
| 3 | mwpl_position_limits | PARTIALLY VERIFIED (legacy archive 2011‑01‑03→2024‑04‑30 only; current feed NOT LOCATED) | `nsearchives.nseindia.com/archives/nsccl/mwpl/nseoi_02012024.zip` | CSV+XML in ZIP |
| 4 | fo_ban_list | VERIFIED (today-only, no history) | `nsearchives.nseindia.com/content/fo/fo_secban.csv` | quasi-CSV |
| 5 | bulk_block_deals | VERIFIED (today-only static file; historical API BLOCKED 503) | `archives.nseindia.com/content/equities/bulk.csv` | CSV |
| 6 | atm_implied_volatility | BLOCKED (404 from www.nseindia.com option-chain API, no static alternative found) | none working | n/a |
| 7 | circuit_band_asm_gsm | **VERIFIED — all three** (Band + GSM via sec_list.csv; ASM via `/api/reportASM`, see the 2026-08-11 correction in §7) | `sec_list.csv` + `www.nseindia.com/api/reportASM` | CSV + JSON |
| 8 | index_constituents_weights | PARTIALLY VERIFIED (constituents yes; weights NOT LOCATED) | `niftyindices.com/IndexConstituent/ind_nifty50list.csv` | CSV |
| 9 | corporate_announcements | VERIFIED (live, unparameterized only) | `www.nseindia.com/api/corporate-announcements?index=equities` | JSON |
