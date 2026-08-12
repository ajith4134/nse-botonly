# Research/61 — Point-in-Time Market Rules, Trading Calendar, and Deep Historical News Sourcing

**Renumbered 2026-07-24 from `57_point_in_time_rules_calendar_and_news_sourcing.md` to
`61_...` to resolve a filename collision with `57_corporate_actions_master_sourcing_
consolidated.md` (both were independently created the same day and both claimed "57").
Content unchanged; only the number/filename moved.**

**Rule I acquisition research (Rule D: saved to file).** Skill used: `deep-research`
(three parallel multi-angle sweeps + source triangulation + credibility grading).
Companion to `53_market_open_simulation_idea_map.md` §9.4/§11.3/§11.4 and to
`54_nse_microstructure_data_sourcing.md` / `55_replay_rl_lob_technique_and_oss_parts.md`
(tick/depth/options data — not repeated here). This pass answers the THREE remaining
acquisition targets needed for a leak-free, era-correct full-history replay:
point-in-time market rules, point-in-time trading calendar, and a NEW target — deep
historical news/announcements.

**As-of date: 2026-07-24.** Every dated fact below carries its own circular/source date;
several items are flagged explicitly as unverified-this-session and need a follow-up pass
before being trusted for build decisions.

---

## 0. Bottom line (the direct answer)

**Market rules (§11.3) and trading calendar (§11.4): no machine-readable, point-in-time
dataset exists anywhere for either.** Every era-correct parameter (expiry cycle, lot size,
tick size, circuit bands, STT, session hours, margin regime) must be manually compiled from
individually-dated NSE/SEBI circular PDFs — there is no API, no CSV-with-valid-from/valid-to
field, no vendor selling this as structured data. The trading calendar is one notch better:
the OSS package `pandas_market_calendars` ships a hardcoded, actively-maintained NSE/BSE
holiday list from **1997** forward, which should be the seed — but it treats muhurat day as
a full closure (no special-session hours) and has zero ad-hoc-outage coverage, so it is a
strong partial solution, not a complete one.

**Deep historical news (§9.4, brand-new target): no single source covers 15–20 years at
intraday precision.** The realistic design is a **stitched, era-dependent pipeline**:
BSE's own announcement backend does carry exact HH:MM:SS timestamps (confirmed via an
Apify scraper's schema) but only for the live/recent feed — historical bulk depth is
unconfirmed and gated behind bot-defenses or paid vendors (₹2.5–9 lakh/yr). Generic news
APIs (GDELT, NewsAPI.ai) go back to 2013/2014 but are weak on India-company-level
granularity. **Before ~2010, point-in-time news is a hard blocker at intraday precision**
— NSE's own announcements page has zero Wayback Machine coverage before Sep 2010, and no
Indian financial outlet offers a historical API.

**Net effect on the §53 build:** all three targets should be tagged, like the tick-data
fidelity ladder in §54, as **era-dependent fidelity tiers** rather than uniform capabilities
— rules/calendar compilation is a bounded (if tedious) engineering project with no hard
data blocker; news is the one item with a genuine, permanent pre-2010 blocker requiring an
explicit design decision (see §4).

---

## 1. Source-credibility note

Grades: **A** = primary/official (NSE/SEBI circular text read directly, or a primary spec)
· **B** = reputable secondary (vendor pages, well-maintained OSS, major outlets, verified
API schemas) · **C** = forum/blog/single-source/unverified (leads only). Both
`nseindia.com` and `bseindia.com` are bot-protected SPAs that reject or redirect plain
automated fetches (confirmed directly — BSE's announcement API 301-redirects to a
human-verification page; several NSE archive pages timed out at 60s). This means most NSE/
BSE facts below were confirmed either via successfully-fetched circular PDFs at
`archives.nseindia.com`/`nsearchives.nseindia.com` (which ARE fetchable), via SEBI's own
site (`sebi.gov.in`, generally fetchable), or via independent secondary corroboration —
never from the live site's JSON/HTML directly.

---

## 2. §11.3 — Point-in-time MARKET-RULES history

### 2.0 The master finding: no aggregator exists, but two anchor documents shortcut the compilation

The single most useful discovery for this whole section: **NSE and SEBI both publish
annual "consolidated master circulars"** whose every clause carries a footnote citing the
original dated circular that introduced or amended it. These give the chronological chain
of circular numbers/dates in one place — the point-in-time *skeleton* — even though the
full text of each historical circular still must be fetched individually to get the actual
parameter values that were in force.

- **NSE Capital Market Consolidated Circular** — Ref 48/2025, download ref
  NSE/CMTR/67774, dated Apr 30, 2025 (99 pp; each section embeds a "Relevant circulars"
  table). Grade A, read directly.
- **SEBI Master Circular Ch.4** — Comprehensive Risk Management for Cash Market:
  `sebi.gov.in/sebi_data/commondocs/oct-2023/Chapter-4-Comprehensive_Risk_Management_for_Cash_p.pdf`.
  Grade A, read directly.
- **SEBI Master Circular Ch.5** — Exchange Traded Derivatives (228 pp; contains a
  rescinded-circular reference list back to **June 16, 1998**):
  `sebi.gov.in/sebi_data/commondocs/jul-2021/Chapter%205%20-%20Exchange%20Traded%20Derivatives_p.pdf`.
  Grade A, read directly.

**Verdict for the whole section:** compilation project, not a data-sourcing problem. Each
of the 7 items below is "achievable but tedious" — walk each anchor's citation chain,
fetch each cited circular PDF, extract the dated parameter. No item in this section is a
hard blocker on the scale of §3's L3/MBO or §4's pre-2010 news; the work is mechanical, not
impossible.

### 2.1 Options expiry-cycle history

**Origins (Grade B/C for the pre-2010 launch dates — NSE's own history page 403'd on fetch,
triangulated via secondary sources + corroborated by SEBI circular text):**
Index futures (NIFTY) launched Jun 12, 2000; index options Jun 4, 2001; single-stock
options Jul 2, 2001; single-stock futures Nov 9, 2001. SEBI's Ch.5 rescinded-circular list
independently pins the risk-containment circulars for these launches (index options Dec 11,
2000; stock options Jun 20, 2001; single-stock futures Nov 2, 2001) — Grade A for the
circular dates, B for the "launch date" framing.

**Weekly-expiry introduction, per index (Grade A/B, primary circulars read where available):**

| Index | Weekly launch | Primary/secondary source |
|---|---|---|
| BANKNIFTY | **27-May-2016** (announced 5-May-2016) | Business Standard (B); circular number cited but PDF 404'd |
| NIFTY 50 | **11-Feb-2019** (Monday) | Business Standard (B) |
| FINNIFTY | **11-Jan-2021** | NSE/FAOP/46603 + Zerodha bulletin (A/B) |
| MIDCPNIFTY | F&O 24-Jan-2022 (monthly first); weekly added by 2023 | NSE press release PR_cc_18042024 (A/B) |
| NIFTYNXT50 | F&O 24-Apr-2024 — **monthly only, never had a weekly** | NSE circular 123/2024 (A) |

**Weekday changes over time (critical for era-correct replay — these predate the 2025
unification and are commonly missed):**
- MIDCPNIFTY → Monday expiry, eff. 16-Aug-2023 (NSE/FAOP/57538, 23-Jun-2023).
- BANKNIFTY Thursday → Wednesday: NSE circular 119/2023 (NSE/FAOP/57540, 12-Jul-2023),
  first Wednesday expiry 6-Sep-2023.
- FINNIFTY → Tuesday expiry, eff. 18-Oct-2021 (Grade B/C, single broker source).
- BANKNIFTY Wednesday → Thursday (reversion): NSE circular 154/2024 (NSE/FAOP/65336,
  29-Nov-2024), eff. 2-Jan-2025. This circular gives a clean dated snapshot of per-index
  expiry weekdays valid 2-Jan through 31-Aug-2025: BANKNIFTY=last Thursday,
  FINNIFTY=last Tuesday, MIDCPNIFTY=last Monday, NIFTYNXT50=last Friday, NIFTY=Thursday.

**2024 rationalization — one weekly per exchange (Grade A):**
- SEBI SEBI/HO/MRD/TPD/P/CIR/2024/132, 1-Oct-2024 ("Measures to Strengthen Equity Index
  Derivatives Framework"): one weekly expiry per exchange; index contract value raised to
  ≥ Rs 15 lakh.
- NSE 123/2024 (NSE/FAOP/64506), 10-Oct-2024: discontinued BANKNIFTY/MIDCPNIFTY/FINNIFTY
  weeklies eff. 20-Nov-2024 (last weekly expiries: BANKNIFTY 13-Nov, MIDCPNIFTY 18-Nov,
  FINNIFTY 19-Nov-2024). NIFTY retained as sole NSE weekly; BSE retained SENSEX only.

**2025 weekday unification — NSE→Tuesday, BSE→Thursday (Grade A):**
- SEBI SEBI/HO/MRD/MRD-TPD-1/P/CIR/2025/76, 26-May-2025 ("Final Settlement Day for Equity
  Derivatives"): each exchange must pick Tuesday OR Thursday uniformly across its expiries.
- NSE 111/2025 (NSE/FAOP/68747), 25-Jun-2025: all NSE expiries move Thursday → Tuesday,
  new contracts from 1-Sep-2025 (minor Aug-28-vs-29 discrepancy across secondaries on the
  exact first live Tuesday session — resolve against the raw circular before hard-coding).
  BSE → Thursday, same window.

**Sourcing verdict:** individually-fetchable PDFs at `nsearchives.nseindia.com/content/
circulars/FAOP*.pdf`; no working full-text search index and no chronological "all expiry
circulars" list exists anywhere. Compilation project.

### 2.2 Lot-size revision history

**No single source exists** — a Zerodha community thread (tradingqna.com) states this
explicitly: "don't think there is a single source that can get u the required info in one
go" (Grade C corroboration of the absence, but consistent with everything else found).

- **Current lot sizes only, machine-readable:**
  `nsearchives.nseindia.com/content/fo/fo_mktlots.csv` — symbol × contract-month CSV,
  **no historical field, current snapshot only**. Grade A for the file's existence, but it
  answers "today" not "point-in-time." Reconstructing history requires either (a) our own
  periodic forward snapshots from today, or (b) probing Wayback Machine coverage of this
  exact URL (not reachable from this research sandbox — flagged as a follow-up from a
  non-sandboxed machine).
- **Best compiled NIFTY-only history found (Grade C, needs primary reconciliation)**, via
  tradingqna.com: 200 (Jun 2000) → 100 (Apr 2005) → 50 (Feb 2007) → 25 (Oct 2014) → 75
  (Oct 2015) → 25 (26-Apr-2024) → 75 (2-Jan-2025) → 65 (30-Dec-2025). **No comparable table
  exists** for BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50 or any individual stock — this is a
  real gap, not just an inconvenience, given the 210-underlying full-universe requirement.
- **Minimum-contract-value threshold history (the driver of lot-size revisions, Grade A):**
  Rs 2 lakh (2000) → **Rs 5 lakh** (Nov 2015, SEBI circular Jul-2015:
  `sebi.gov.in/legal/circulars/jul-2015/review-of-minimum-contract-size-in-equity-derivatives-segment_30253.html`)
  → **Rs 15–20 lakh for INDEX derivatives only** (SEBI CIR/2024/132, eff. 20-Nov-2024).
  Important nuance confirmed by NSE circular FAOP70616 (3-Oct-2025): **stock derivatives
  stayed on a separate Rs 5–10 lakh track** even after the 2024 index-side change — a
  detail easy to get wrong if only the headline "Rs 15L" figure is used.

**Gap:** per-stock lot-size history across ~210 F&O underlyings and ~2,000 cash symbols is
essentially unstarted — a dedicated circular-by-circular pass would be needed, or periodic
NSE bhavcopy/`fo_mktlots.csv` snapshots taken going forward stitched with whatever
historical NSE circulars can be located.

### 2.3 Tick size history

**Structural finding:** tick size is exclusively an NSE/exchange-bylaw matter, not a SEBI
circular matter (a grep of SEBI's full trading-framework annexure returned zero hits for
"tick size"). The authoritative trail lives only in NSE's own circular archive.

**Complete NSE circular chain (Grade A, extracted verbatim from NSE Master Circular §3.3):**

| Download Ref | Date |
|---|---|
| NSE/CMTR/4181 | 5-Jun-2003 |
| NSE/CMTR/4272 | 14-Jul-2003 |
| NSE/CMTR/14348 | 19-Mar-2010 |
| NSE/CMTR/14666 | 27-Apr-2010 |
| NSE/CMTR/43225 | 15-Jan-2020 |
| NSE/CMTR/62174 | 24-May-2024 |
| NSE/CMTR/67133 | 13-Mar-2025 |

**Point-in-time regimes:**
- **2003 → Jun-2024:** flat **Rs 0.05** for essentially all equity/F&O instruments — ~20
  years stable, a genuinely simple constant for the bulk of the replay history.
- **Eff. 10-Jun-2024** (NSE/CMTR/62174): first price-linked regime — Rs 0.01 if price <
  Rs 250, Rs 0.05 if ≥ Rs 250; monthly re-evaluation.
- **Eff. 15-Apr-2025** (NSE/CMTR/67133): 6-tier ladder — <250=0.01, 250–1,000=0.05,
  1,000–5,000=0.10, 5,000–10,000=0.50, 10,000–20,000=1.00, >20,000=5.00. Index futures got
  a parallel ladder; options remain flat Rs 0.05. A separate stock-options tick circular
  (FAOP67134) exists but was not read this session.

**Confirmed gap/blocker:** **pre-2003 tick size (1994 electronic-trading launch → 2003) —
no source found by any search angle.** Requires a manual dig into `archives.nseindia.com`
1994–2003 circulars or NSE annual reports; not resolved this pass. The full text of the
2003/2010 circulars (dates confirmed, content unread) is also an open item.

### 2.4 Circuit filters / price bands & market-wide circuit breaker (MWCB)

**MWCB lineage (all Grade A, primary text extracted directly):**

| Date | Circular | Change |
|---|---|---|
| 28-Jun-2001 (eff 2-Jul) | SEBI SMDRPD/Policy/Cir-37/2001 (NSE via CMTR/2657) | Original: 10/15/20% triggers on Sensex OR Nifty; quarterly point-recalc; halt durations vary by time-of-day; individual scrips got 20% bands |
| 3-Sep-2013 (eff 1-Oct; NSE 14-Oct) | SEBI CIR/MRD/DP/25/2013 | Recalc quarterly → daily; each halt shortened 15 min; mandatory 15-min pre-open call auction on resumption |
| 12-Jan-2015 | SEBI CIR/MRD/DP/02/2015 | Mechanism refinement: compute index after every trade, continuous breach check, auto-purge unmatched orders |

Full text: `sebi.gov.in/legal/circulars/jun-2001/index-based-market-wide-circuit-breaker-in-compulsory-rolling-settlement_17986.html`;
`sebi.gov.in/legal/circulars/sep-2013/index-based-market-wide-circuit-breaker-mechanism_25303.html`;
`sebi.gov.in/cms/sebi_data/attachdocs/1421059410188.pdf`.

**Two premise corrections to the parent idea-map (§53 §11.3 speculated a 2016 revision and
a 2025 overhaul — both checked directly against SEBI's own circular index and found not to
exist as stated):**
- **There was no 2016 equity MWCB revision.** SEBI's own self-citation list contains only
  commodity-segment (CDMRD) circulars in 2016 — confident Grade-A negative.
- **The "2025 MWCB overhaul with intraday triggers" does not exist as a finalized rule.**
  The closest match is a SEBI consultation paper dated 9-Jan-2026 (unified trading
  rulebook) that consolidates/reformats the 2001→2013→2015 rules and **explicitly leaves
  the 10/15/20% thresholds unchanged** — confirmed from SEBI's own PDF annexure. Still in
  comment stage as of this research date.

**Individual-stock price bands (2/5/10/20%) — the single biggest structural finding in this
whole section, with real implications for the simulator:**
Per NSE Master Circular §3.1.E, the specific 2/5/10/20% classification for a given stock on
a given day is **decided ad hoc, day-to-day, by NSE's Surveillance department** — it is
**not documented in any circular**. It lives instead in NSE's daily downloadable
security-master files (`security.gz` / `NSE_CM_security_ddmmyyyy.csv.gz`). NSE currently
exposes only the **current day's** file; whether a historical archive of these daily files
exists anywhere is **unconfirmed and is likely the single biggest data blocker in this
entire market-rules section** — this needs dedicated follow-up (distinct from the bhavcopy
archive, which IS retained long-term but doesn't carry the band classification).

**Dynamic price bands** (the flexing "operating range" for F&O-eligible stocks) DO have
documented circular history: NSE §3.1.C chain runs from NSE/CMTR/3671 (9-Oct-2002) through
13 circulars to Nov-2024, with a confirmed real mechanism change eff. May–Aug 2024
(CMTR/62237, 63404: 5%→3%→2% flex steps, 15/30/60-min cooling-off periods). SEBI origin:
CIR/MRD/DP/34/2012 (13-Dec-2012).

**Machine-readable leads (weak):** `rhnvrm.github.io/stock-market-circulars` is a
GitHub-Actions-fed, genuinely structured Markdown+YAML circular index, but **forward-looking
only from ~2026, no historical backfill**. `exchangecirculars.com` was seen in search but
403'd on fetch (depth unverified — follow-up item).

### 2.5 STT / stamp duty / exchange transaction charges

**STT equity delivery/intraday:** Introduced 1-Oct-2004 (Finance No.2 Act 2004). Secondary
sources (Wikipedia) state 0.125% delivery at inception, but **sources disagree on the exact
per-side split**, and the primary text (Finance Act 2004 Ch.VII / STT Rules 2004
S.O.1059(E)) was inaccessible this session (`incometaxindia.gov.in` is a JS shell,
`indiankanoon.org` bot-walled) — **this exact-2004-rate gap needs primary verification**
from `indiacode.nic.in` or `egazette.gov.in` before being hard-coded. Delivery reduced
0.125% → 0.10% ~Jul-2012 (Grade B).

**STT F&O — Grade A for the recent deltas, the era most relevant to near-term replay:**

| Effective | Futures | Options (premium basis) |
|---|---|---|
| 1-Oct-2004 | 0.017% | 0.017% (pre-2013 options-specific path uncertain, Grade C) |
| 1-Jun-2013 (NSE/FATAX/23500) | 0.017% → 0.01% | → 0.05% |
| 1-Apr-2023 | 0.01% → 0.0125% | 0.05% → 0.0625% |
| 1-Oct-2024 | 0.0125% → 0.02% | 0.0625% → 0.1% (options-on-exercise 0.125% intrinsic) |
| 1-Apr-2026 (already law) | 0.02% → 0.05% | 0.1% → 0.15% |

Sources: TaxGuru (`taxguru.in/income-tax/budget-2024-securities-transaction-tax-rate-revised-wef-1st-october-2024.html`),
Economic Times, ClearTax, Zerodha bulletin (Grade B, cross-triangulated across ≥3
independent secondary sources). **Gap:** the clean 2004–2013 options-specific STT path
(and the ~2008 base-change from strike+premium to premium-only) is Grade-C snippet-only.

**Stamp duty:** Pre-Jul-2020 = state-by-state variation, **no secondary aggregation
exists anywhere** (Grade C, real gap). National uniform reform (Finance Act 2019
amendments to the Indian Stamp Act) eff. **1-Jul-2020**: delivery 0.015% (buyer side),
intraday 0.003%, futures 0.002%, options 0.003% (Grade B, 3-source triangulated; primary
gazette notification not fetched this session).

**NSE exchange transaction charges — a major finding easy to model wrong:** historically a
**volume/turnover SLAB structure** (higher turnover tier → lower per-crore fee) for both
cash and F&O — **not a flat rate**, contrary to how many backtest cost models simplify it.
Ended by SEBI's "True to Label" circular SEBI/HO/MRD/TPD-1/P/CIR/2024/92 (1-Jul-2024, eff.
1-Oct-2024), which mandates one uniform flat rate per exchange. Only two point circulars
were located: NSE/FA/46730 (18-Dec-2020, increase for IPFT) and NSE/FA/56129 (24-Mar-2023,
a reduction). **No aggregated table of the historical slab schedules 2004–2024 exists** —
**modelling exchange fees as a flat bps rate for any pre-Oct-2024 period will be wrong.**
Blocker: compilation project against `archive.nseclearing.in/circulars` (PDF-per-circular,
no CSV/API).

### 2.6 Session hours and pre-open auction

**Pre-open call auction timeline (Grade A):**

| Date | Event | Circular |
|---|---|---|
| 15-Jul-2010 | SEBI introduces call auction in pre-open | SEBI/CIR/MRD/DP/21/2010 |
| 17-Sep-2010 | Final clarification | CIR/MRD/DP/32/2010 |
| **18-Oct-2010** | **Live launch**, 9:00–9:15, **Nifty 50 + Sensex 30 constituents ONLY** | NSE Circular No. 116 (11-Oct-2010) |
| **20-Jan-2012** | Extended to **all listed scrips** + periodic call auction for illiquids | SEBI CIR/MRD/DP/01/2012 |
| **8-Dec-2025** | **First-ever pre-open for the F&O/derivatives segment** | SEBI CIR/2025/79 (29-May-2025) + NSE/FAOP/71092 (3-Nov-2025) |

**Simulator implication (directly actionable):** before 18-Oct-2010, model no pre-open at
all — a straight 9:15 continuous open. From 18-Oct-2010 to 20-Jan-2012, pre-open applies
**only to Nifty 50 + Sensex 30 constituent symbols** — this must be a **symbol-scoped**
flag for that ~15-month window, not a single global date switch, or every non-index-name
day in that window will be modeled wrong. No F&O pre-open ever existed before Dec-2025.

**Normal session (9:15–15:30):** no evidence of any change since NSE's 1994 launch was
found — but this is an absence-of-evidence (Grade B) negative, not a positive confirmation;
recommend a dedicated circular sweep if 100% certainty on this specific point is required
before build sign-off.

**Extended-hours history (context, not applicable to equity cash so far):** SEBI
SEBI/HO/CDMRD/DMP/CIR/P/2018/146 (30-Nov-2018) extended **commodity** hours to ~9AM–11:55PM
(implemented). SEBI separately permitted equity-derivatives hours up to 11:50PM in 2018 but
**exchanges never implemented it**. NSE's 2024 proposal for a 6PM–9PM index-F&O evening
session was **rejected by SEBI** (no broker consensus) — status quo 9:15–15:30 holds
throughout the replay history.

### 2.7 Margin regime milestones (SPAN / VaR / peak margin)

**SPAN (Grade A dates, Grade B narrative):** no clean standalone "SPAN adoption" circular —
embedded in the 1998–2001 risk-containment series (J.R. Varma Committee, Jun-1998 → SEBI
"Risk Containment Measures for Index Futures Market," 28-Jul-1999 → live Jun-2000). SEBI's
Ch.5 rescinded-circular list is the primary record. Re-parameterized most recently by
SEBI/HO/MRD2/DCAP/CIR/P/2020/27 (24-Feb-2020). Treat as a date-range, not a single instant.

**VaR margining, cash market (Grade A/B):** origin SMDRP/Policy/Cir-34/01, 21-Jun-2001,
eff. 2-Jul-2001 (99% VaR, gross-basis, alongside the post-Ketan-Parekh rolling-settlement
shift) — corroborated by NSE ISMR 2002 (`nsearchives.nseindia.com/web/sites/default/files/inline-files/ismr2002ch5.pdf`,
Grade A primary). Parameter-evolution chain (best build-list for a dated VaR/ELM table):
2005 (MRD/DoP/SE/Cir-07/2005) → 2006 → CIR/MRD/DRMNP/9/2013 → CIR/MRD/DRMNP/65/2016 →
2019/33 → 2020/27. Current: VaR 6σ minimum 9% (Group I), ELM flat 3.5%.

**Peak margin framework (Grade A origin, Grade B-high for dates):** origin
SEBI/HO/MRD2/DCAP/CIR/P/2020/127, 20-Jul-2020
(`sebi.gov.in/legal/circulars/jul-2020/...47101.html`). Phased implementation, verified
verbatim from SEBI's Master Circular:

| Phase | Window | % of peak margin |
|---|---|---|
| 1 | 1-Dec-2020 – 28-Feb-2021 | 25% |
| 2 | 1-Mar – 31-May-2021 | 50% |
| 3 | 1-Jun – 31-Aug-2021 | 75% |
| 4 | From 1-Sep-2021 | 100% |

Mechanism: ClearingCorp sends ≥4 intraday margin snapshots/day; report the higher of
EOD/peak shortfall. Later amendment to fixed BOD margin parameters:
SEBI/HO/MRD2/DCAP/P/CIR/2022/60 (10-May-2022) and 2023/016 (1-Feb-2023).

**Penalty framework (Grade A, verbatim):** base = CIR/DNPD/7/2011, 10-Aug-2011 —
0.5%/day if shortfall <Rs 1L and <10% margin; 1.0%/day otherwise; +5%/day beyond 3
consecutive days or beyond 5 days/month; non-reporting = 100% short-collection; false
reporting = 100% + 1-day suspension. **No automatic "disablement threshold" found in SEBI's
own text** — blog claims of a "30-day disablement" rule are Grade-C unverified. A 2025
rationalization (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/57, 28-Apr-2025: 0.07%/0.10%/5%
schedule) exists but is Grade B/C, primary text unverified this session.

**Naked-option margin:** for equity/index F&O there is **no separate short-option minimum
charge** (SEBI 2020/27 explicitly states "no separate short option minimum charge" — it is
captured entirely inside SPAN worst-scenario + ELM). Only commodities have an explicit SOMM
with a dated chain (Jan-2020 → Jan-2021 → Sep-2021 → Mar-2022, e.g. NCL/COM/51528). The
"2% ELM on short options at expiry" figure sometimes cited for Oct-2024 is Grade-C
snippet-only and needs the actual SEBI circular before use.

> ### ⛔ CORRECTED 2026-08-12 — THIS PARAGRAPH WAS WRONG (`A.96`)
>
> The claim below — that no public deep archive of daily SPAN files exists — is **false**, and
> the workaround it recommends is unnecessary. Measured: every probe from **2008-01-01 to
> today** returns HTTP 200 at
> `https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.{YYYYMMDD}.s.zip` — no cookie, no
> auth, no 403, a plain browser user-agent is enough. Today's file is 9.4 MB zipped, 49 MB of
> XML, **133,274 option contracts each carrying its 16-scenario risk array**, 237 underlyings.
> Intraday snapshots exist too (`.i1` … `.i5`).
>
> **Why the original conclusion was reached, and the lesson:** the research followed NSE's
> official SPAN *page*, which is a React shell whose only download link points at the member
> login wall `ims.connect2nsccl.com` — so the data looked gated. It is served publicly from the
> archive host the whole time. **A login wall on the documented path is not evidence that the
> data is private.** The same fetcher headers this repo already uses for other NSE endpoints
> reach it unchanged.
>
> Point-in-time margin for any backtest date since 2008 is therefore RECOVERABLE DIRECTLY,
> rather than reconstructed from a dated rule table. The algorithmic-reconstruction workaround
> below is retired.

~~**Confirmed blocker — historical daily SPAN files:** no public deep archive of daily SPAN
risk-parameter (`.spn`) files was found. NSE Clearing download pages timed out on fetch;
only current-day utilities and third-party parsers exist (GitHub `nse-span-risk-parser`,
PyPI `marginism`), none with historical backfill. **Recommended workaround: don't try to
source historical SPAN files — recompute point-in-time margins algorithmically** from a
dated rule-parameter table (VaR%/ELM%/scan-range/MPOR/peak-phase, keyed by circular
effective date) applied to the historical price/volatility series we already have.~~ ICCL
(BSE's clearing corp) reportedly has a date-range risk-parameter portal — an unexplored
lead for a follow-up pass.

### 2.8 §11.3 summary table

| # | Item | Best source | Earliest date | Machine-readable? | Grade |
|---|---|---|---|---|---|
| 1 | Expiry cycles (weekly launches, weekday changes, 2024/25 rationalization) | SEBI CIR/2024/132 + NSE 123/2024, 111/2025, 119/2023, 154/2024 | 2000 (launch) / 2016 (first weekly) | No — individual PDFs, no index | A (recent) / B (pre-2019) |
| 2 | Lot sizes | `fo_mktlots.csv` (current only); tradingqna table (NIFTY history) | 2000 (NIFTY, Grade C) | Current CSV only; **no history** | A (current) / C (history) |
| 2b | Minimum contract-value thresholds | SEBI Jul-2015 + Oct-2024 circulars | 2000 → 2015 → 2024 | No | A |
| 3 | Tick size | NSE Master Circular §3.3 chain | 2003 (pre-2003 = blocker) | Chain in one PDF; underlying circulars separate | A (2003+) / gap (pre-2003) |
| 4 | Market-wide circuit breaker (MWCB) | SEBI 2001/2013/2015 circulars, full text read | 2-Jul-2001 | No | A |
| 4b | Per-stock 2/5/10/20% bands | NSE daily security-master files (not circulars) | Operational/daily | **Only via daily files; historical archive unconfirmed** | A (mechanism) / gap (data) |
| 4c | Dynamic price bands (F&O stocks) | NSE Master §3.1.C, 13-circular chain | Oct-2002 | No | A (chain) |
| 5 | STT F&O (2013/2023/2024/2026 deltas) | TaxGuru, ET, ClearTax, NSE/FATAX/23500 | 2004 (exact rate contested) | No | A (recent) / C (2004–13 options) |
| 5b | Stamp duty | Zerodha/Angel/ClearTax | 1-Jul-2020 (pre-2020 = gap) | No | B / C (pre-2020) |
| 5c | NSE exchange transaction charges (slabs) | Individual NSE circulars + SEBI True-to-Label 2024/92 | 2020 (only what surfaced) | **No aggregator; PDF-per-circular** | A (per-circular) / gap |
| 6 | Pre-open auction | SEBI CIR/MRD/DP/21/2010, 01/2012; NSE No.116; 2025/79 | 18-Oct-2010 | No | A |
| 6b | Session hours 9:15–15:30 stability | Absence-of-change (secondary) | 1994 | No | B (unverified negative) |
| 7 | SPAN | SEBI Master Ch.5 rescinded-circular list | 28-Jul-1999 | No — compile from list | A (dates) / B (narrative) |
| 7b | VaR/ELM cash market | SEBI Cir-34/01 + NSE ISMR 2002 + Ch.4 footnote chain | 2-Jul-2001 | No | A |
| 7c | Peak margin phases | SEBI 2020/127 + Master Circular | 1-Dec-2020 | No | A / B-high (dates) |
| 7d | Penalty framework | CIR/DNPD/7/2011 (verbatim table) | 10-Aug-2011 | No | A |
| 7e | Historical daily SPAN files | None found | — | **Does not exist publicly** | Gap confirmed |

---

## 3. §11.4 — Point-in-time TRADING CALENDAR

### 3.1 Official NSE holiday-list sources

NSE issues an annual circular titled *"Trading holidays for the calendar year [Y]"*
(Capital Market Segment), published via `archives.nseindia.com/content/circulars/`
(mirrored at `nsearchives.nseindia.com/content/circulars/`). Directly read example:
**NSE/CMTR/50560, Circular Ref. 117/2021, dated 10-Dec-2021** — the full 2022 holiday
list (13 weekday holidays + 5 weekend-falling holidays), noting *"Muhurat Trading will be
conducted on Monday, October 24, 2022. Timings of Muhurat Trading shall be notified
subsequently."* URL: `archives.nseindia.com/content/circulars/CMTR50560.pdf`.

**Important structural finding:** the annual holiday circular names the muhurat DATE but
explicitly defers the TIMING to a separate circular issued closer to the event — so a
complete simulation calendar needs to track **two circular types per year**, not one.

**Archive depth — only partially confirmed.** `archives.nseindia.com` was confirmed to
serve circulars back to at least Apr-2019 (an unrelated SEBI-matter circular,
NSE/INVG/40881, 30-Apr-2019, was successfully read) and forward through 2021/2022. A
**pre-2015 holiday circular could not be located or retrieved** — several plausible URLs
(`nsearchives.nseindia.com/content/circulars/COM65590.pdf`, the general
`nseindia.com/resources/exchange-communication-holidays` page,
`nseclearing.in/resources/holidays`) all timed out (60s) rather than returning content.
**This is an open item, not a confirmed absence** — a follow-up pass with longer timeouts
or a headless browser is needed before concluding pre-2015 circulars are unreachable.

**Verdict:** NSE publishes and retains individual year-ahead holiday circulars at stable
download-ref URLs, but **there is no visible single index page listing all past years** —
each year's circular must be located individually (by circular number, not obviously
enumerable without site search or a crawled index).

### 3.2 Muhurat trading session dates/times

NSE has conducted muhurat trading since 1992 (BSE since 1957) — stated across multiple
secondary sources (groww.in) but not independently triangulated to a primary NSE
statement, Grade B.

**No single dated table spanning many years exists.** groww.in's "Muhurat Trading Over
the Last Decade" covers only 2015–2024 **index returns**, not session dates/times.
groww.in's 2025 article gives only the 2025 session — **Tuesday, 21-Oct-2025,
1:45–2:45 PM** (pre-open 1:30–1:45, block deals 1:15–1:30, closing session 2:55–3:05) —
explicitly noting *"the main difference is that trading will be done in the afternoon
slot, in place of the usual evening session"* — **confirming timing/duration has not been
consistent across years** (historically evening, shifted to afternoon at least in 2025).
Wikipedia's "Muhurat trading" article confirms only that sessions occur "during evening
hour" and are announced separately each year — no dated list.

**Conclusion: there is no ready-made table of muhurat dates+times going back 20+ years.**
This must be reconstructed year-by-year from NSE's two-circular pattern (§3.1) or from
financial-portal news archives around each Diwali. **This is the single hardest item in
the whole calendar section** — flag explicitly for a dedicated, likely partly-manual
compilation task.

### 3.3 Ad-hoc closures/extensions

- **24-Feb-2021 telecom/SAN outage (Grade A/B, well corroborated):** NSE halted trading
  across cash and derivatives segments due to simultaneous failure of both telecom links
  feeding its Storage Area Network; suspension began around 11:40 AM, resumed
  ~3:45–4:00 PM (reports vary slightly on the exact resumption minute). SEBI later imposed
  ~₹72.6 crore in combined settlements on NSE and NSE Clearing. Sources: scroll.in and
  business-standard.com (search-snippet corroboration only, direct fetch 403'd),
  `aseemjuneja.in/nse-technical-glitch/` (fully fetched, Grade B primary-narrative).
- **Other documented glitches (Grade C, single blog aggregator, not independently
  cross-verified per-incident):** Jun-2020 Bank Nifty options price-feed freeze;
  Sep-2019 last-30-minutes screen freeze/price-feed stoppage; Apr-2024 successful DR
  failover drill (Mumbai→Chennai). All from `aseemjuneja.in` — treat as a **lead list**,
  verify each independently before treating as fact.
- **COVID Mar–Apr 2020 session-timing changes: not confirmed.** moneycontrol.com and
  economictimes.indiatimes.com were both blocked for fetch this session; the one relevant
  snippet found referenced an NSE circular (27-Mar-2020) on **cross-currency
  futures/options** trading-hours revision — NOT equity cash-market hours. **No evidence
  was found that NSE/BSE equity cash-market hours were shortened during COVID** (consistent
  with prior general knowledge that, unlike some other Asian exchanges, Indian equity
  cash-market hours were not altered) — but this is unverified-this-session and should be
  explicitly checked via a SEBI circular archive or news search before being included or
  excluded from the ad-hoc-closure table.
- **2001–2004 volatility-era closures (Parliament attack Dec-2001, May-2004
  election-result circuit-breaker halts): not researched this session** — the WebSearch
  budget ran out before these queries executed. The May-2004 post-election-result crash
  (trading halted twice by circuit breakers) is a well-known event but **was not verified
  with a fetched source this session** — treat as an unconfirmed lead only, flagged
  explicitly for a follow-up pass.

### 3.4 Machine-readable / structured historical calendars

This is the strongest sub-finding in the whole calendar section — two real OSS candidates
examined at source-code level:

**a) `pandas_market_calendars` (`rsheftel/pandas_market_calendars`) — best candidate
found.** File `pandas_market_calendars/calendars/bse.py`, fetched directly from GitHub raw.
Aliases both `BSE`/`XBOM` and `NSE`/`XNSE` to the identical calendar (single hardcoded
holiday list shared by both exchanges). **Hardcoded holiday list explicitly spans
23-Jan-1997 through 25-Dec-2026** — code comment: *"Due to the complexity around the BSE
holidays, we are hardcoding a list of holidays back to 1997, and forward through 2026."*
Detailed per-holiday-name comments start appearing from 2021 onward.

**Muhurat handling — an important limitation:** muhurat days (e.g. 4-Nov-2021,
24-Oct-2022) are present only as `BSEClosedDay` entries with a comment noting "muhurat
trading day" — **there is no `special_opens`/`special_closes` schedule object
representing the actual 1-hour session.** The calendar treats muhurat day as a full
closure with zero representation of the real intraday trading window. **This means the
package alone will incorrectly skip muhurat sessions entirely** — a separate, manually
compiled data source (§3.2) is required for those specific days.

No documented early closes/late opens outside muhurat days ("There are no known early
closes or late opens" per code comment). No explicit ad-hoc-closure entries (COVID, the
Feb-2021 glitch) — confirms the package models *scheduled* holidays only, not *operational*
outages (which weren't full-day closures anyway, so this is expected, not a defect).

**Maintenance: actively maintained.** GitHub commit history shows updates through
May-2026, including a 30-Dec-2025 commit explicitly titled "Update list of BSE holidays for
2025 and 2026." PyPI shows 80+ released versions, v0.1 (Dec-2016) through v5.4.0
(27-May-2026). **Grade: B+** — reputable, actively maintained OSS, but the underlying
holiday list is manually curated/hardcoded by maintainers rather than sourced from a
documented pipeline against NSE circulars, so it should be cross-checked (not blindly
trusted) for years before ~2015.

**b) `exchange_calendars` (`gerrymanoim/exchange_calendars`, Zipline-lineage).** README
confirms `XBOM` (Bombay Stock Exchange, India) support, added in v1.5, actively maintained
(recent commits, CI badges, releases through v4.13.2+). **No `XNSE`-specific calendar
found** — only `XBOM`. Historical depth not independently verified this session (the
underlying holiday-data file 403'd via the GitHub API). Grade B — weaker than
`pandas_market_calendars` for this use case since it lacks an NSE-labeled calendar and its
depth is unconfirmed.

**c) `jugaad-py/master-data` `holidays.csv` — stale but useful cross-check.** A flat
date-only file (no holiday names), read directly via GitHub raw, spanning **1997-01-23
through 2020-12-25**, ~580 rows. Repo shows only a single commit, 3 stars, 7 forks —
effectively unmaintained/abandoned since ~2020. **Value:** its 1997 start date
independently corroborates `pandas_market_calendars`' 1997 start — a useful (weak)
triangulation that "1997" is a real, recurring boundary across the OSS ecosystem, not one
project's artifact. Grade C (unverified provenance, unmaintained) — secondary sanity-check
only.

**d) `nsepy`, `nsepython`, `nse-trading-calendar` (PyPI)** — found by name via search but
**not substantively verified this session**: `nsepy`'s GitHub page appeared in search
results but wasn't fetched; `pypi.org/project/nse-trading-calendar/` returned only an
error page on fetch. Historical depth and maintenance status **unconfirmed** — an open
item for a follow-up pass, not something to report as fact either way.

### 3.5 Independent cross-check / retrospective sources

- **Wikipedia** ("Muhurat trading," "National Stock Exchange of India," both fetched in
  full): thin on dated operational history — no dated muhurat table, no mention of the
  Feb-2021 glitch, COVID timing, or other ad-hoc closures. Not useful as a standalone
  cross-check for this task.
- **groww.in:** useful for current-year muhurat detail and a 2015–2024 returns
  retrospective, not a dated calendar source. Grade B.
- **moneycontrol.com and economictimes.indiatimes.com: both blocked outright for fetch**
  in this research environment — these are commonly cited outlets for exactly this kind
  of retrospective and should be checked directly by a tool/browser without this
  restriction before concluding they add nothing.
- **business-standard.com:** appeared repeatedly as the best secondary source for the
  Feb-2021 glitch, but direct fetch of both the topic page and a specific article returned
  403 — seen only via search snippets, so specific facts from it are corroborating leads,
  not independently verified citations.

### 3.6 §11.4 summary table

| Source | Earliest year covered | Machine-readable? | Maintenance status | Grade |
|---|---|---|---|---|
| NSE annual holiday circulars (`archives.nseindia.com` / `nsearchives.nseindia.com`) | Confirmed retrievable ≥2019; pre-2015 not confirmed (timeouts, not confirmed absent) | No (PDF per year, no index) | Ongoing (official, current) | A |
| `pandas_market_calendars` (rsheftel), `bse.py` calendar, aliased BSE/XBOM + NSE/XNSE | 1997 (hardcoded) → 2026 | **Yes** (Python import, `date_range`/`schedule` API) | Active — commits through May-2026, PyPI v5.4.0 | B+ |
| `exchange_calendars` (gerrymanoim) | Unconfirmed depth; XBOM added v1.5 | Yes (Python) | Active (recent commits/releases) | B |
| `jugaad-py/master-data` `holidays.csv` | 1997–2020 (dates only, no names) | Yes (flat CSV) | **Abandoned** (single commit, stale since 2020) | C |
| `nsepy` / `nsepython` / `nse-trading-calendar` (PyPI) | Unconfirmed this session | Presumed yes | Unconfirmed | Unverified |
| groww.in muhurat articles | 2015–2024 (returns only, not full calendar) | No | Actively published | B |
| Wikipedia (Muhurat trading; NSE India) | N/A — no dated calendar content | No | N/A | B (thin coverage) |
| aseemjuneja.in glitch timeline | 2019–2024 (ad-hoc glitches) | No | Blog, editorial rigor unclear | C (leads only) |
| moneycontrol / economictimes / business-standard specific articles | N/A | No | N/A | **Unverified this session** (fetch-blocked) |

### 3.7 Bottom line / honest gap assessment for §11.4

There is **no single clean machine-readable source spanning the full ~1994–2026 NSE
history.** The best candidate, `pandas_market_calendars`'s BSE/XNSE calendar, gives a
genuinely useful, importable, actively-maintained holiday list from **1997 forward** — that
covers roughly 28 of the ~30+ years needed and should be the primary automated seed. But it
(1) starts in 1997, not NSE's ~1994 equity-trading inception, so **1994–1996 must be
manually reconstructed** from old NSE/SEBI records or news archives; (2) treats muhurat day
as a full closure with **no special-session hours modeled at all**, so muhurat sessions
need a fully separate, manually-compiled data source; and (3) contains no ad-hoc
operational closures/halts (Feb-2021, COVID-era circulars if any, 2001–2004 events) — those
are exchange *outages*, not calendar *holidays*, and were only partially verified this
session.

---

## 4. §9.4 — Deep historical NEWS / announcements archive (NEW acquisition target)

### 4.0 Bottom line for this section

There is **no single source** that gives 15–25 years of intraday-timestamped, per-symbol
news+announcements for the Indian market. The realistic architecture is a **stitched
pipeline of 3–4 sources by era**:
- **~2007-present (BSE) / ~2010-present (NSE):** exact HH:MM:SS timestamped corporate
  announcements exist at the exchanges themselves, but there is **no official bulk
  historical API at either exchange** — both `nseindia.com` and `bseindia.com` are
  bot-protected SPAs (confirmed directly: plain HTTP fetches return only page titles; the
  BSE announcement JSON endpoint 301-redirects to a human-verification page when hit
  without a proper browser session/referer). Access requires either paid vendor feeds
  (TickerPlant ₹2.5–3 lakh/yr, BSE Direct ₹9 lakh/yr) or unofficial scraper libraries that
  replay the same session-cookie handshake the website itself uses.
- **~2014-present:** commercial news-API aggregators (NewsAPI.ai/Event Registry) offer
  historical text-news archives with publish-date fields — generic (not exchange-grade)
  coverage of India.
- **Pre-2010, especially pre-2005: effectively unobtainable at intraday precision.** The
  only fallback is Wayback Machine snapshots of Indian financial-news homepages, which are
  sparse, irregular, capture-time-only (not article-publish-time), and don't start
  meaningfully until ~2001 (moneycontrol.com) — and even that is unreliable before ~2008.
- Enterprise vendors (Refinitiv/LSEG, Factiva) claim 1989+ coverage for non-US markets
  including India, but are priced and gated for institutional procurement, not personal use.

### 4.1 NSE Corporate Announcements archive

NSE's live site has a "Corporate Announcements"/"Corporate Filings" section (results,
board meetings, insider trading under SAST/PIT, other disclosures). Direct WebFetch
attempts at `nseindia.com/resources/historical-reports-capital-market-daily-monthly-archives`,
`archives.nseindia.com/`, and NSE data-analytics pages **all timed out** — consistent with
known Akamai/Cloudflare-style bot protection requiring a full browser session + cookie
handshake, not a plain GET. This is corroborated by third-party libraries
(`BennyThadikaran/NseIndiaApi`, `hi-imcodeman/stock-nse-india`) existing specifically to
work around this — they first hit the homepage to acquire session cookies before calling
the JSON endpoints, rate-limited to ~3 req/sec.

An `.announcements(from_date, to_date)` method exists (per docs at
`bennythadikaran.github.io/NseIndiaApi/api.html`) supporting date-range and symbol
filtering, but **the docs don't state an earliest supported date, nor confirm whether
records carry exact submission time vs. date-only** — needs direct endpoint testing.
**No official bulk historical download/API was found** — only the live/rolling feed plus
the unrelated bhavcopy/price archives.

- Earliest coverage: unconfirmed via primary source (site inaccessible to plain fetch).
- Timestamp granularity: unconfirmed directly; industry consensus (Zerodha TradingQnA
  thread) is that NSE/BSE announcements DO carry an exact submission timestamp internally,
  but it's not exposed via a documented bulk historical endpoint.
- Format: JSON (unofficial), PDF attachments. Cost: free via live/unofficial scraper
  (rate-limited) or paid vendor feed (§4.4). Grade: **A** that the exchange is the primary
  source; **C** for all historical-depth/granularity claims (unconfirmed).

### 4.2 BSE Corporate Announcements archive

`bseindia.com/corporates/ann.aspx` is BSE's live announcements portal, backed by an
unofficial JSON API (`api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w`) taking
`strPrevDate`/`strToDate` (YYYYMMDD) params — implying **date-range query support is built
into BSE's own backend**, a positive signal that BSE's system is more queryable than NSE's,
matching its market reputation for the longer/more structured announcement history.

**Directly verified:** hitting that API endpoint without a proper session
**301-redirects to `bseindia.com/members/showinterest.aspx`** — confirming BSE, like NSE,
blocks/redirects bare API calls. This is exactly what `BennyThadikaran/BseIndiaApi`
(unofficial Python) and `hirawatt/BSE_NSE_Announcement` (scraper, output files named
`BSE_{from-year}_{to-year}.csv`) exist to work around; the file-naming convention implies
multi-year range queries are possible, but the actual covered range/precision wasn't
confirmed (would need to inspect the actual CSVs).

**Vendor-confirmed pricing** (from a Zerodha TradingQnA thread, fetched directly):
**TickerPlant** sells the official real-time corporate-announcement feed at **₹2.5–3
lakh/yr + GST** (~$3,000–3,600/yr); **BSE Direct** (BSE's own paid feed) costs **₹9
lakh/yr + GST** (~$10,800/yr), described by users as the most expensive official option.
Community sentiment: "getting access to data is painful" at retail scale; RapidAPI
listings and unofficial scrapers are the realistic middle ground.

**Apify scraper (verified directly):** `apify.com/nexgendata/nse-bse-announcements` —
pay-per-result at **$0.05/announcement record**, and importantly, **confirmed to capture
separate `announcement_date` and `announcement_time` fields in IST** — the single
strongest concrete evidence in this whole pass that **exact intraday timestamp granularity
does exist and is scrapable** for the live/recent feed. Historical backfill depth prior to
first run is not documented (likely limited — the tool is built for live monitoring,
"announcements appear within minutes of NSE publishing them").

- Earliest coverage: unconfirmed depth; live+recent confirmed.
- Timestamp granularity: **confirmed intraday** (date + time, IST) for the live/recent feed.
- Format: HTML/JSON (unofficial), PDF for actual filings.
- Cost: free (self-scrape, blocked without header/session workaround) → $0.05/record
  (Apify) → ₹2.5–3L/yr (TickerPlant) → ₹9L/yr (BSE Direct official).
- Grade: **B** (vendor-confirmed pricing, verified redirect-block behavior, verified
  timestamp field names).

### 4.3 SEBI / regulatory filing archives

`sebi.gov.in/filings.html` is a navigation hub with no visible bulk-download documentation.
The most important concrete finding, from the Apify "India SEBI Filings Tracker" product
page: **"SEBI's listing pages cover roughly the last 12–18 months in the visible HTML
table"** — SEBI's own site is **not** a deep historical archive for company-level
disclosure events (SAST/PIT insider-trading disclosures, buyback/takeover offers,
DRHP/RHP). That Apify tool supports custom ranges beyond that window but caps at 730 days
lookback, $0.10/filing (max $500/run at 5,000-filing cap) — implying SEBI's backend CAN be
queried further than the UI shows, but only via an unofficial/reverse-engineered path.

SEBI's regulatory circulars/orders (distinct from company-specific disclosures) are a
different, generally well-archived corpus on `sebi.gov.in` back to the 1990s (SEBI
established 1988, statutory powers 1992) — useful for regulatory/market-structure event
context (§2 of this doc draws on exactly this corpus) but **not** a per-company news feed.

- Earliest coverage: company-level filings via web UI ~12–18 months only; circulars/orders
  corpus potentially 1990s+ (not evaluated for company-news purposes).
- Timestamp granularity: unknown for company filings; likely date-level for circulars.
- Cost: free (shallow) → $0.10/filing via Apify (deeper, unofficial). Grade: **B**.

### 4.4 Financial-news APIs / vendors

**GDELT Project** (fetched `gdeltproject.org` and `/data.html` directly):
- GDELT 1.0: 1-Jan-1979–present, but dated by "date the event was **found** in world news
  media" (not publish date), English-only, **daily** granularity.
- GDELT 2.0: 1-Apr-2013–present, updates **every 15 minutes**, 65 live-translated
  languages — the closest GDELT gets to intraday granularity, though the 15-min bucket is
  GDELT's processing cadence, not necessarily the article's true publish timestamp.
- GKG (Global Knowledge Graph): same two-tier structure (1.0 from 2013 daily; 2.0 also
  2013, 15-min).
- **India/company-level coverage not confirmed** — GDELT's documentation doesn't describe
  per-company/ticker entity tagging suitable for filtering a specific NSE-listed company's
  news; its Global Entity Graph is coarse (persons/orgs/locations, Google-NLP-tagged) and
  unverified for reliably resolving specific Indian tickers. GDELT is broad geopolitical
  event-coding, not corporate-disclosure-grade.
- Cost: free, bulk CSV/ZIP, also on AWS Open Data Registry. Grade: **A** for the source
  itself; **C** for "good India company-level coverage" (unverified, plausibly weak).

**NewsAPI.org:** Business plan $449/mo ($358.80/mo annual), 250K req/mo, **only 5 years of
historical data** (not 15–20). No India-specific coverage claims found. Grade: B.

**NewsAPI.ai / Event Registry** (fetched `newsapi.ai/plans` directly): free tier (2,000
searches), paid from $90/mo. **Historical data confirmed from 2014 onward** (directly
stated on the pricing page). 150,000+ outlets, 60+ languages, but **no explicit
confirmation of Indian-source coverage or exact timestamp granularity** (only "publication
date" mentioned; hour/minute precision unconfirmed). Grade: B.

**Refinitiv/LSEG** (fetched `developers.lseg.com/.../filings-API` and
`vendr.com/marketplace/refinitiv` directly): Filings API explicitly lists **India** among
its automated document-feed countries. Stated depth: "over 50 years of history…dated back
to 1968 for US and **1989 for other markets**" (India not broken out individually).
**Procurement confirmed enterprise-only**: named-user licensing $1,000–3,000/user/month
base + $500–2,000+/user/month for data entitlements + $5,000–25,000+ implementation, with
realistic minimum deployments **$75,000–150,000/year** even under 10 users — **not
feasible as an individual/personal subscription.** Grade: **A** for coverage claim, **A**
for the pricing/procurement conclusion (independent vendor-pricing-intelligence source).

**Factiva/Dow Jones:** a $79/month single-user figure appears on one secondary
pricing-aggregator, but conflicts with a more commonly cited $2,000–3,800/user/year
figure — **a genuine, unresolved disagreement between two secondary sources**, neither
Dow Jones's own (unpublished) pricing page. 74% of sources reportedly not on the free web
(implying strong archive depth), no India-specific coverage % found. Grade: **C** —
would need a direct sales contact to resolve.

**AYLIEN:** WebFetch failed outright (DNS resolution error) — possibly rebranded/merged
post-acquisition; **unresolved**, not a confirmed dead end.

**Webz.io:** homepage confirms "News API"/"News API Lite" products exist, but no
specifics on depth/granularity/India-coverage/pricing were reachable this session
(`webz.io/products/news-api/` or `docs.webz.io` not fetched) — flagged for direct
follow-up.

**India-specific news aggregators (Moneycontrol, Economic Times, Business Standard,
LiveMint):** **no evidence found of any offering a public historical API or bulk
dataset/archive product** — all appear to be live-scrape-only websites for the general
public. **This is a hard gap**: no primary-source, India-native, machine-readable
historical news API exists for any major Indian financial outlet.

### 4.5 Academic / Kaggle / GitHub datasets

**Kaggle:** the vast majority of "Indian stock market" Kaggle datasets found (NIFTY 50
2000–2023, Sensex/Nifty 27-year, "1M+ Real Time stock market data NSE/BSE," etc.) are
price/OHLCV, **not** news/announcements. No sizeable, credible Indian-stock-news-specific
Kaggle dataset was surfaced — the relevant search page was JS-rendered and unreadable via
WebFetch, so this is **inconclusive, not a confirmed absence**; a logged-in Kaggle
API/CLI query is needed to close this out.

**GitHub:** a direct code-search (`NSE+BSE+announcements+historical+dataset`) returned
**zero repository results** ("no repositories matched"). Repos found via general web
search (`hirawatt/BSE_NSE_Announcement`) are **active scrapers**, not static historical
archive dumps; their actual covered range/precision is undocumented in the README.

**Academic papers:** Semantic Scholar fetches (both direct API and search-results page)
returned no usable content this session (JS-rendered/empty). **Unresolved** — could not
confirm or deny the existence of academic NLP papers using large-scale Indian
financial-news corpora within this session's tool constraints.

**Grade for this whole subsection: C — genuinely inconclusive, not a confirmed negative.**
Flagged for follow-up with restored search budget or authenticated API access.

### 4.6 Common Crawl / Wayback Machine (fallback for point-in-time reconstruction)

Verified directly via the Wayback `archive.org/wayback/available` API (direct fetches of
`web.archive.org` pages themselves were blocked by the research tool with an explicit
"unable to fetch from web.archive.org" error — a tool-level restriction, not a
source-availability fact):

| Site | Earliest snapshot found | Notes |
|---|---|---|
| moneycontrol.com | **2001-01-18** | Earliest snapshot returned when queried against a Dec-2000 target — real coverage starts ~2001 |
| moneycontrol.com | 2008-01-02 confirmed present | Sanity-check date returned exact-day snapshot |
| economictimes.indiatimes.com | **empty** for Jan-2005 target | No snapshot near 2005 — confirms a real pre-2008ish ET gap |
| economictimes.indiatimes.com | 2009-01-01 confirmed present | Coverage clearly present by 2009 |
| livemint.com | 2008-05-17 confirmed present | Consistent with Mint's actual 2007 launch |
| `nseindia.com/corporates/corporateHome.html` | **no 2005 snapshot; closest is 2010-09-16** | **NSE's own corporate-announcements page has no Wayback coverage until 2010** — even the "public saw it on the website" fallback fails pre-2010 |

**Assessment:** Wayback/Common Crawl is a legitimate but weak supplementary source —
useful mainly for verifying homepage-level "what was the top story" snapshots from
~2008–2009 onward for major outlets, and **not usable at all for NSE's own
corporate-announcements page before 2010**, and **not usable for any Indian financial
outlet before ~2001, unreliable before ~2008.** Snapshot frequency per era wasn't
established (would need the CDX API, not fetchable this session). Timestamp granularity
when a snapshot exists is capture-time (exact to the second) but reflects **when the
crawler visited, not the article's true publish time** — a systematic bias that worsens
the sparser the crawl frequency was in a given era.

Grade: **A** for the specific snapshot-existence facts (directly queried primary source);
**B/C** for the broader "viable for point-in-time reconstruction" claim given the
demonstrated gaps.

### 4.7 Cost & procurement summary

| Path | Personal/individual feasible? | Cost |
|---|---|---|
| NSE/BSE unofficial scraper libraries (self-hosted) | Yes, technically | Free, but must defeat anti-bot session handling; fragile, ToS-grey-area |
| Apify NSE/BSE announcement scrapers | Yes | $0.05/record; SEBI filings $0.10/record (capped $500/run) |
| TickerPlant (official BSE-adjacent vendor) | Marginal | ₹2.5–3 lakh/yr (~$3,000–3,600) |
| BSE Direct (official) | No (too costly for individual) | ₹9 lakh/yr (~$10,800) |
| NewsAPI.org | Yes | $449/mo (or $358.80/mo annual), only 5yr history |
| NewsAPI.ai / Event Registry | Yes | Free tier → $90/mo+, history from 2014 |
| GDELT | Yes | Free (self-hosted BigQuery/CSV), weak on Indian company granularity |
| Factiva | Disputed | $79/mo claimed by one source vs. $2,000–3,800/user/yr by another — unresolved conflict |
| Refinitiv/LSEG | **No** | $75K–150K/yr minimum enterprise deployment |
| Wayback Machine / Common Crawl | Yes | Free, but sparse/unreliable pre-2010 |

### 4.8 §9.4 ranked summary table

| Source | Earliest coverage | Timestamp granularity | Format | Cost | Grade |
|---|---|---|---|---|---|
| BSE announcements (via Apify scraper) | Unconfirmed depth; live+recent confirmed | **Intraday, confirmed** (date + time, IST) | JSON/PDF | $0.05/record | B |
| BSE Direct (official) | Unconfirmed | Likely intraday (unverified) | Unknown | ₹9L/yr (~$10.8K) | B |
| TickerPlant (BSE-adjacent vendor) | Unconfirmed | Likely intraday (unverified) | Unknown | ₹2.5–3L/yr (~$3–3.6K) | C |
| NSE announcements (unofficial `.announcements()` API) | Undocumented, site inaccessible to fetch | Unconfirmed | JSON/PDF | Free (rate-limited) | C |
| SEBI filings (via Apify) | ~12–18mo via UI; deeper via unofficial query, capped 730 days | Unconfirmed | HTML/PDF | $0.10/filing | B |
| Refinitiv/LSEG Filings API | Claims 1989 for "other markets" incl. India | Unconfirmed | Structured | $75–150K/yr min | A (coverage) / blocked (access) |
| Factiva | Deep (unspecified) | Unconfirmed | Structured | $79/mo–$3,800/yr (disputed) | C |
| GDELT 2.0 | 2013–present | 15-min processing bucket (≠ true publish-time) | CSV, free | Free | A (source) / C (India-company fit) |
| GDELT 1.0 | 1979–present | Date-level only | CSV, free | Free | A (source) / C (India-company fit) |
| NewsAPI.ai | 2014–present | Unconfirmed precision | JSON | Free tier–$90/mo+ | B |
| NewsAPI.org | 5-year rolling window only | Unconfirmed | JSON | $449/mo | B |
| Wayback Machine (moneycontrol) | 2001-01-18 earliest confirmed | Capture-time exact, ≠ publish-time; sparse pre-2008 | HTML snapshots, free | Free | A (facts) / C (usability) |
| Wayback Machine (economictimes) | No coverage near 2005; present by 2009 | Same caveat | Free | Free | A (facts) / C (usability) |
| Wayback Machine (NSE corp. announcements page) | **No coverage before 2010-09-16** | N/A | Free | Free | A (hard blocker confirmed) |
| Kaggle news datasets | Not found (search inconclusive) | N/A | N/A | N/A | Unresolved |
| GitHub historical announcement dumps | Not found (zero search hits) | N/A | N/A | N/A | Unresolved |
| Academic Indian-financial-news-NLP corpora | Not found (search tooling failed) | N/A | N/A | N/A | Unresolved |

### 4.9 Explicit hard blockers vs. items needing more direct contact/negotiation

**Hard blockers (confirmed this pass, not just assumed):**
1. NSE's and BSE's own websites are not fetchable by plain HTTP GET — both require full
   browser session/cookie handshakes; any pipeline must budget for maintaining that
   handshake (fragile, subject to breaking on site changes) or paying a vendor who already
   handles it.
2. NSE's corporate-announcements webpage has **zero Wayback Machine coverage before
   September 2010** — there is no fallback "reconstruct what the public saw" path for NSE
   announcements pre-2010.
3. SEBI's own website surfaces only ~12–18 months of company-filing history in its
   visible UI — not a deep archive by itself.
4. No India-native financial news outlet (Moneycontrol, ET, Business Standard, LiveMint)
   offers a public historical API/dataset — confirmed absent, not merely unresearched.
5. Refinitiv/LSEG is confirmed enterprise-only pricing ($75K–150K/yr minimum) — not a
   personal-subscription path regardless of its strong India coverage claim.
6. Wayback/Common Crawl coverage of Indian financial news is essentially nonexistent
   before ~2001 and unreliable before ~2008–2009.

**Needs more direct contact/negotiation (not closed out, not blockers — session
constraints, not source-availability facts):**
1. AYLIEN — URL/company status unresolved (DNS failure); needs a fresh search for its
   current name/URL (possibly rebranded post-acquisition).
2. Webz.io — product confirmed to exist; pricing/depth/India-coverage needs
   `webz.io/products/news-api/` or `docs.webz.io` directly.
3. Factiva pricing — genuine conflict between $79/mo and $2,000–3,800/user/yr claims,
   both secondary; needs a direct sales quote.
4. Kaggle/GitHub/academic-paper searches for Indian financial-news datasets were
   inconclusive, not negative — a follow-up with restored search budget or an
   authenticated Kaggle CLI/API call is needed.
5. Exact earliest-date and timestamp-granularity for NSE's and BSE's own announcement
   backends were not directly confirmed (site inaccessible to plain fetch) — needs either
   a working scraper session or a direct vendor (TickerPlant/BSE Direct) sales
   conversation.

---

## 5. Consolidated gaps & hard blockers table (all three targets)

| # | Blocker | Target | Severity | Mitigation / substitute |
|---|---|---|---|---|
| R1 | Pre-2003 tick size — no source found | §11.3 | Medium (research gap) | Manual dig into 1994–2003 NSE archives/annual reports |
| R2 | Per-stock daily circuit-band (2/5/10/20%) history | §11.3 | **Hard (data may not exist historically)** | Confirm whether NSE's daily security-master file is archived anywhere historically; if not, this is unrecoverable pre-our-own-recording |
| R3 | NSE historical transaction-charge slab schedules 2004–2024 | §11.3 | Medium (compilation) | Walk `archive.nseclearing.in/circulars` PDF-by-PDF; no shortcut found |
| R4 | Historical daily SPAN margin files | §11.3 | **Hard (confirmed not publicly archived)** | Recompute margins algorithmically from a dated rule-parameter table + historical price/vol series, not from recorded SPAN files |
| R5 | Pre-2020 state-wise stamp duty | §11.3 | Medium (research gap) | No aggregator exists; would need state-gazette-level digging, likely low ROI vs. materiality |
| R6 | Exact 2004 STT rate split (Finance Act primary text) | §11.3 | Low-medium (verification gap) | Fetch `indiacode.nic.in`/`egazette.gov.in` primary text directly (blocked hosts this session) |
| C1 | Pre-2015 NSE holiday circulars — retrieval unconfirmed | §11.4 | Medium (open, not confirmed absent) | Retry with longer timeouts / headless browser; cross-check against `pandas_market_calendars` 1997+ list |
| C2 | Muhurat session dates+times, dated table 20+ years | §11.4 | **Hard (no aggregator exists anywhere)** | Manual/semi-automated compilation from NSE's two-circular pattern per year |
| C3 | 1994–1996 calendar (pre-`pandas_market_calendars` coverage) | §11.4 | Medium | Manual reconstruction from earliest NSE/SEBI records |
| C4 | 2001–2004 ad-hoc closures (Parliament attack, May-2004 election halts) | §11.4 | Low-medium (unverified lead) | Dedicated follow-up search pass; treat current knowledge as unconfirmed |
| C5 | COVID-era (Mar–Apr 2020) session-hours change — inconclusive | §11.4 | Low | Direct SEBI circular archive check; current evidence suggests equity hours were NOT changed, but unconfirmed |
| N1 | Intraday-precise news/announcements before ~2010 | §9.4 | **Hard, likely permanent** | Accept date-level-only fidelity for pre-2010 era; tag provenance accordingly (ties to §53 §8.2 fidelity-tier watermark) |
| N2 | Any Indian financial-news outlet's historical API | §9.4 | **Hard (confirmed absent)** | Use exchange announcement feeds (BSE/NSE, once licensed) as the primary point-in-time news backbone instead of generic media |
| N3 | NSE/BSE bulk historical announcement API pricing/depth | §9.4 | Open (needs vendor contact) | Contact TickerPlant and BSE Direct directly for quote + historical-depth confirmation |
| N4 | Wayback coverage of NSE announcements page before 2010-09-16 | §9.4 | **Hard, confirmed** | No public-facing fallback exists; pre-2010 news is a genuine blocker, not a sourcing gap to solve |
| N5 | Factiva pricing conflict; AYLIEN/Webz.io unresolved | §9.4 | Open (needs follow-up) | Direct vendor contact / a fresh research pass with restored budget |

---

## 6. Ranked acquisition plan

**Design principle (mirrors §54):** fidelity-laddered and era-tagged — deliver what can be
compiled/sourced NOW, escalate as licensed sources and direct vendor contact land. Every
experience the replay generates should carry a rules/calendar/news **fidelity-tier tag**
(extends the §53 §8.2 provenance watermark), not just a live/replay tag.

**Rank 1 — NOW, ~free, compilation work (no external blocker):**
- Seed the trading calendar from `pandas_market_calendars`'s BSE/XNSE holiday list
  (1997–2026, actively maintained) as the default backward-walk calendar for that window.
- Compile the market-rules skeleton by walking the two anchor documents (§2.0: NSE
  Consolidated Circular 48/2025 + SEBI Master Circular Ch.4/Ch.5) and fetching each cited
  circular PDF — this is tedious but has no external blocker; prioritize the items with
  Grade-A chains already found in this pass (expiry cycles, MWCB, pre-open auction, peak
  margin, tick size 2003+).
- Build the muhurat-session table and the pre-2015/pre-2019 holiday-circular gaps via a
  dedicated manual/semi-automated compilation pass (this is explicitly the hardest
  calendar item — budget real time for it, not a quick script).

**Rank 2 — cheap paid / low-friction, fills near-term news + rules verification gaps:**
- Contract an Apify-style scraper (BSE/NSE announcements, $0.05/record; SEBI filings,
  $0.10/record) to backfill whatever historical depth is actually available behind the
  live feed — cheap enough to just try and see how far back it reaches before committing
  to a bigger spend.
- Direct email/contact to TickerPlant and BSE Direct for pricing + confirmed historical
  depth (mirrors the §54 B5 NSE Data & Analytics email pattern) — resolves N3.
- Direct contact to resolve the Factiva pricing conflict (N5) and re-search AYLIEN/Webz.io
  with a fresh budget.

**Rank 3 — the deep, era-correct backbone once budget/contact is resolved:**
- If BSE/NSE's own announcement backend does turn out to have deep historical bulk export
  (unconfirmed — the single most valuable open question from §4), license it as the
  primary point-in-time news source for the exchange-disclosure category (results, board
  meetings, insider disclosures) — this is the category most directly tied to price action
  and most important to get right.
- Layer NewsAPI.ai (2014+, cheap) or GDELT (2013+, free) as a supplementary general-news
  signal for the 2013–2025 window, accepting the unconfirmed India-company-granularity
  weakness as a known limitation, not a blocker.

**Rank 4 — accept as a permanent, explicit blocker; do not keep searching:**
- Intraday-precise general financial news before ~2010 (N1/N4) — design the replay to
  degrade gracefully: pre-2010 sessions carry date-level-only (or no) news signal, tagged
  at a lower fidelity tier, never silently backfilled with anything invented.
- Historical daily SPAN margin files (R4) — don't keep searching for a source that
  multiple angles confirmed doesn't exist; switch immediately to algorithmic
  recomputation from the dated parameter table.
- Enterprise-only vendors (Refinitiv/LSEG) — do not pursue for a personal/non-distributed
  project; the pricing tier is definitively out of scope (mirrors §54's treatment of
  co-location TBT as B1, permanently out of reach).

**Do NOT pursue:** scraping `nseindia.com`/`bseindia.com` live JSON at scale (same ToS
red line as §54 Tier 6); treating any Kaggle/GitHub "Indian stock news" find as a primary
source without first verifying its actual date range and timestamp precision by
inspection (several leads in this pass turned out to be OHLCV, not news, when checked).

---

## 7. What this pass did NOT cover

- Direct vendor quotes (TickerPlant, BSE Direct, Refinitiv, Factiva) — all flagged as
  "contact directly," none actually obtained (out of scope for a research pass; a
  procurement follow-up).
- Full text of ~20 tabulated circulars whose dates/numbers were confirmed but content not
  read (listed inline throughout §2).
- BSE-side expiry-cycle circulars (only NSE side was primary-verified in §2.1).
- Per-stock lot-size and expiry history across the full ~210 F&O underlyings and ~2,000
  cash symbols (only NIFTY-level history was compiled).
- Wayback Machine coverage of `fo_mktlots.csv` and the NSE daily security-master file
  specifically (R2) — the research sandbox could not reach `archive.org` directly; needs
  a follow-up from an unrestricted environment.
- 2001–2004 ad-hoc-closure verification (C4) and the COVID session-hours question (C5) —
  both ran out of search budget mid-pass; flagged, not resolved.
- Kaggle/GitHub/academic dataset searches for Indian financial news (§4.5) — inconclusive
  due to JS-rendered search pages and exhausted search budget, not a confirmed negative.

*Stopping condition: three parallel deep-research passes (market rules, trading calendar,
historical news), each running 4-8+ distinct search angles with primary-source WebFetch
verification wherever the target sites allowed it; each pass independently exhausted its
search budget while still surfacing genuinely new findings in its final rounds — the
gaps documented above are real open items for a follow-up pass, not artifacts of an
under-scoped search.*
