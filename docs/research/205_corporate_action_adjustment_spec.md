# 205 — Corporate-action adjustment

**Task:** todo `1.7`. **Plan:** `L0.07`. **Idea-intake verdict: ALREADY EXISTS** — building `L0.07`, not
adding anything.

**R.23(b) classification: an engine.** Inputs are the corporate-action feed and the price/strike archive; the
solver is a subject-text parser plus a ratio-to-factor calculation and a cumulative back-adjustment; carried
state is the per-symbol adjustment history; the output changes behaviour by rewriting every historical price
and strike a backtest reads.

**SOTA analog** (R.23a): **Zipline's adjustments database** — `SQLiteAdjustmentWriter` with separate
`splits` / `mergers` / `dividends` tables and multiplicative factors applied backwards from the ex-date.
That is the right shape and this is the NSE-specific half of it. *(Note per `A.39`: zipline installs here and
can be read, but is not a dependency — it downgrades pandas and collides with vectorbt.)*

---

## 1. What it must get right

A price series that ignores a 1:10 split shows a **90% overnight crash that never happened**, and every
volatility, momentum and stop-loss calculation built on it is wrong. `TATASTEEL` closed at 959.40 on
2022-07-27 and 100.35 on 2022-07-28. Unadjusted, that is a catastrophic gap; adjusted, it is a normal day.

The same applies to F&O: a strike of 3340 becomes 3265 after a ₹75 dividend adjustment, and an option
priced against the wrong strike is mispriced by the entire adjustment.

## 2. The premise I had to abandon first (`D.28`)

I hypothesised that `CLOSE(t-1) != PREVCLOSE(t)` in the cash bhavcopy detects corporate actions, which
would have meant the engine needed no external feed. **Disproved on two documented events:**

| Event | `CLOSE(t-1)` | `PREVCLOSE(t)` | |
|---|---|---|---|
| `TATASTEEL` 1:10 split, ex 2022-07-28 | 959.40 | 959.40 | identical |
| `IOC` 1:2 bonus, ex 2022-06-30 | 109.80 | 109.80 | identical |

`PREVCLOSE` is a mechanical carry-forward of the raw prior close and is **never** adjusted. The condition
can never fire for the event it was meant to catch. An external feed is mandatory. Recorded as `D.28`;
the process failure that let a scan run over 7,857 files before the premise was checked is `O.29`.

## 3. Measured state of the acquired feed (2026-08-10)

Acquired via `scripts/fetch_nse_corporate_actions.py` into
`/home/opc/nse_archive/manifest/corporate_actions.sqlite3`:

| | |
|---|---|
| Actions | **41,885**, years **2001 → 2026** |
| Distinct `subject` strings | **8,074** |
| Distinct *shapes* (digits normalised to `N`) | **2,321** |
| Shapes appearing exactly **once** | **1,573** |

**That long tail is the engine's real risk.** A regex parser over 2,321 shapes will silently mis-parse or
drop the tail, and a dropped split is indistinguishable from no split — which is the failure mode in §1.

### 3.1 The feed carries no numeric ratio

The ratio lives in free text. Verified on the two known events:

```
TATASTEEL  exDate=28-Jul-2022  faceVal=1   subject='Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share'
IOC        exDate=30-Jun-2022  faceVal=10  subject='Bonus 1:2'
```

`Bonus 1:2` means one new share per two held, so the holder ends with 3 for every 2 and the price factor is
**2/3**. Measured against the archive: IOC 109.80 → 74.25 = **0.676**, against 0.667 expected plus a normal
day's move. The convention is confirmed, not assumed.

### 3.2 Four outcome classes, because "cannot tell" is real again (`A.41`)

| Class | Count | Meaning |
|---|---|---|
| **Adjusting, ratio parseable** | Bonus 599+, Face-Value Split 210+155+80+51+35+30+…, Rights 208+21+15 | a factor can be computed |
| **Non-adjusting** | 38,514 — AGM 11,516, dividends, interest payment, buyback 319 | no price adjustment |
| **Adjusting, ratio NOT available** | **189** — `Demerger` 91, `Scheme Of Arrangement` 98 | real price impact, **no ratio anywhere in the text** |
| **Unclassified** | **1,040** | mostly abbreviated dividends (`Int Div-Rs.N Per Share`) and EGM |

**The third class is the dangerous one.** A demerger moves the price materially and the subject line
contains no number at all. Folding it into "no adjustment" would leave a real discontinuity in the series
while the engine reported success. It must surface as **`ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED`** and taint
any series that spans it, exactly as `UNKNOWN` taints a universe snapshot in `L0.05a`.

The fourth class must never be silently dropped either: an unparsed subject is an *open question*, not a
non-event.

## 4. Design

- **Parse, never guess.** Each subject is matched against an explicit, ordered pattern table. A subject that
  matches nothing yields `UNPARSED` with the raw text attached — it is never assumed inert.
- **Factors are exact `Decimal`s** and composed multiplicatively. Applying `1/10` then `2/3` must equal
  `1/15` exactly; float would drift across a 25-year series.
- **Adjustment runs backwards from the ex-date**, the Zipline convention: today's price is truth, history is
  restated. This keeps live and historical prices consistent at the right edge, which is what a running
  strategy actually compares against.
- **Bitemporal, inherited from `L0.04`/`L0.05`.** A corporate action announced late must not appear in a
  series read as-of a date before it was known.
- **F&O strikes adjust by the same factor**, with the additive/multiplicative distinction from `O.28`: a
  dividend adjustment *subtracts* from the strike, a ratio adjustment *divides* it. Measured over 37,334 real
  (underlying, expiry, strike) triples, only 4 underlyings carried adjusted strikes — 3 additive
  (`CANBK` +0.80, `INDIANB` +0.75, `BANKINDIA` +0.35), 1 multiplicative (`TRENT`, steps 33.30/66.70/133.30 =
  100/3, 200/3, 400/3 → factor 3).

## 5. Acceptance criteria

1. `TATASTEEL` 1:10 split: the adjusted series shows a normal day across 2022-07-27/28, not a 90% gap.
2. `IOC` 1:2 bonus: adjusted move matches the measured 0.676 within a normal day's range.
3. Every one of the 41,885 real actions receives a class; **zero** fall through unclassified-and-unreported.
4. `Demerger` and `Scheme Of Arrangement` produce `ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED`, never "no change".
5. Composed factors are exact: `1/10` then `2/3` equals `1/15` with no float drift.
6. A series spanning an unquantified action is marked untrustworthy rather than silently returned.
7. Strike **arithmetic** distinguishes additive from multiplicative: a ratio divides the strike, a dividend
   subtracts from it. *Corrected 2026-08-10 — this criterion previously claimed the engine "reproduces
   `TRENT`'s factor of 3", which it cannot: `TRENT`'s corporate-action entry is a 1:10 face-value split, and
   the factor of 3 is derived from the F&O strike ladder, a source §7 lists as unbuilt.*

## 6. Sourcing record (R.16 / R.17 — mechanical)

| Source | Verdict | Evidence |
|---|---|---|
| **`nselib.capital_market.corporate_actions_for_equity`** | **ADOPT — sole source** | 41,885 rows acquired 2001-2026 in yearly calls, no truncation. Same backend as `www.nseindia.com/api/corporates-corporateActions` (cross-checked byte-identical for Jan-2023, Jan-2005, Jan-2001). |
| NSE bulk CA file on `nsearchives` | **REJECT — does not exist** | 8 path variants guessed against the working `content/equities/{symbolchange,namechange}.csv` pattern; all returned genuine Apache 404 pages (3,540 bytes), not Akamai gates. |
| **BSE `CorpactData` API** | **REJECT — blocked** | 3 variants, all HTTP 302 → `error_Bse.html`; identical to the delisted-equity block. **No independent cross-check exists — the feed is single-sourced and that is an accepted risk, logged.** |
| BSE `DefaultData` API | REJECT | 200, 55 KB real JSON, but **today only** — no historical or per-symbol path. |
| **F&O adjustment circulars** | **ADOPT for derivatives** | `www.nseindia.com/api/circulars?...&subject=Adjustment` → 1.14 MB real CSV listing. `CMPT55202.pdf` (TCS, Jan-2023) fetched and read: states "reduced by the dividend amount… Rs.75.00" with a worked example, **strike 3340 → 3265**. One PDF per underlying per event; no bulk form. `pdfplumber` extracted it cleanly. |
| `jugaad-data` | **REJECT** | Full function inventory grepped in `nse/archives.py` and `nse/live.py`: no corporate-action, bonus, split or dividend function exists. |
| `nsepy` | **REJECT** | `SSLError` — the legacy `www1.nseindia.com` host is dead (`TLSV1_ALERT_INTERNAL_ERROR`); package unmaintained. |
| `Bharat_sm_data` 4.0.1 | **REJECT for this** | Installs and imports (top-level `Base`/`Derivatives`/`Fundamentals`). `NSE` exposes 20 methods, all live/OHLC/options — zero corporate-action or adjustment methods. |
| Any Indian split/bonus adjustment library | **none found** | No package with a working, callable adjusted-price function for Indian equities was located. |

**Built rather than borrowed:** the subject-text parser and the four-class outcome taxonomy. No library
knows NSE's 2,321 subject shapes, and none of the candidates above adjusts Indian prices at all.

## 6.1 Corrections after adversarial review (2026-08-10)

Review found **8 defects and 6 surviving mutants** in code that had already passed ruff, mypy and 50 tests.
It also ran the check the suite could not: pulling real closes either side of 80 sampled ex-dates and
testing `close_after / (close_before x factor)`.

**The empirical verdict on the arithmetic: 66 of 80 resolved against the archive, 65/66 (98.5%) within 20%
of 1.0, median 1.016, stdev 0.069.** The single outlier, `EASEMYTRIP`, traded 85.9M shares that day against
a normal ~2M — real ex-date price discovery, not a parser error. **The core split/bonus arithmetic is
validated end to end.**

**And the same check disproved a whole class.** Three critical corrections:

**① A bonus of a non-equity instrument was quantified as equity dilution.** The regex required only
`bonus` + `N:M` and never checked the noun. **11 real actions, 7 wrongly quantified** — `DRREDDY` got a
factor of **1/7** on a day it moved 2.8%, `ZEEL` **1/22** on a flat day. Applied, those inject a fake 86%
and 95% crash into blue-chip histories. Now rejected on `debenture`/`preference`/`ncrps`/`dvr`/`warrant`/
`bond`/`unit` (`O.31`).

**② A compound subject let one leg supply a number for an action that could not be quantified.**
`MONNETISPA`: `"Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10"` returned **3.03** from
half the sentence. The unquantified check now runs **before** any component is computed (`O.32`).

**③ `known_as_of` was written by ingest and consulted by no read path at all** — the bitemporal promise in
§4 was schema-deep only. A late-announced action leaked into a backtest dated before it was knowable. Every
read now takes `known_as_of`.

Also fixed: `cumulative_price_factor` re-parsed every action from raw SQL on every call (measured 0.67 ms
for the busiest symbol — **~28 minutes** of pure factor lookup across a 2,000-symbol five-year backtest); it
now caches a per-symbol factor timeline, invalidated on ingest. Boundary guards for zero and unchanged face
value, zero strike and zero price factor were all unreached by any test and survived mutation.

**Mutation: 10 of 10 killed**, including all 6 previous survivors. The last one taught something: my
zero-face-value test never reached the zero guard, because a *split* from Rs 0 to Re 1 is rejected earlier by
the words-versus-numbers check. Only a *consolidation* from Rs 0 reaches the division.

**Coverage after the fixes:** 1,413 quantified · 39,598 inert · 600 unquantified · **274 unparsed (0.65%)**.
Nine actions moved from quantified to unquantified — the non-equity bonuses, now correctly refused.

**Spec correction:** §5.7 claimed the engine "reproduces `TRENT`'s factor of 3". It does not and cannot —
`TRENT`'s corporate-action entry is a 1:10 face-value split; the factor of 3 comes from the **F&O strike
ladder**, a different source listed in §7 as unbuilt. The criterion is restated below as strike arithmetic
only.

## 7. Open blockers (R.11 / R.16)

- **Single-sourced on NSE** for equity ratios; BSE is unreachable, so a wrong ratio has no second opinion.
- **Demerger and scheme-of-arrangement ratios are not in the feed** — 189 real actions need the circular
  PDFs, which is a separate acquisition with its own extraction step.
- **F&O adjustment factors need PDF extraction**, one document per event.
- **A factor of 2 on a 5-point strike ladder is invisible** to the additive/multiplicative step test
  (`O.28`) — it divides to 2.5, still a standard step.
- **The 1990s archive's `PREVCLOSE` is unreliable** in its own right (`EIMCOELECO` 1996-07-08 publishes
  72.50 against a prior close of 118.50). Unrelated to this engine, but it rules that column out as a
  verification source for the pre-2000 era.
