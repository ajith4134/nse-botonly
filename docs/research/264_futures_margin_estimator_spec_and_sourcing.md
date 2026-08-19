# 264 · `L6.30` — the SPAN + exposure margin estimator, and why it is an ESTIMATOR

**Spec written 2026-08-18, before code, per `R.23c`.** Builds plan entry **`L6.30`** (todo **6.44**),
status `planned`. **Idea-intake verdict: ① ALREADY EXISTS** — nothing was added to the plan; this
builds the catalogued entry, which turned out to be the binding blocker for three of the six segment
bots (`B36`).

---

## 1 · Why this became urgent

`SegmentBotPaperSession` bounds what it may put on by **notional**. That makes single-stock futures
untradeable at retail capital: one ASHOKLEY lot is 5,000 shares ≈ Rs 5 lakh, so a Rs 10,00,000 book
holds two positions. Futures do not consume notional — they consume **margin**. Bounding by the
wrong quantity is not a conservative constraint, it is the wrong constraint, and it refused 23 of 23
real proposals on 2026-07-31.

The operator's decision (`A.145`) was to bound by real SPAN plus exposure margin rather than invent a
leverage multiple. This document is what happened when I went to get it.

## 2 · The SPAN risk file is NOT publicly published — measured, three strikes, `R.21`

**Round one (recorded in `docs/BACKLOG.md` `B36`)** — eleven archive URL candidates, every one HTTP
404 with NSE's 3,425/3,540-byte error body.

**Round two, this session:**

| route | result |
|---|---|
| `archives/nsccl/span/nsccl.<d>.spn` · `.s.spn` · `.zip` | 404 |
| `archives/nsccl/margin/margin_<d>.csv` · `archives/fo/mkt/margin_<d>.csv` | 404 |
| `api/nsccl-daily-reports?key=SPAN` | 404, 382 bytes |
| **full enumeration** of `api/merged-daily-reports` and `api/daily-reports` across every key (`CM`, `favCapital`, `favDerivatives`, `favDebt`, `favCurrency`, `favCommodity`, `favSLBS`) | **21 distinct reports, and NOT ONE is a margin or SPAN file** |
| **UDiFF F&O bhavcopy schema** (`BhavCopy_NSE_FO_0_0_0_<date>_F_0000.csv.zip`, HTTP 200) | 34 columns, ending `NewBrdLotQty`/`Rmks`/`Rsvd1-4` — **no margin column** |

The 21 reports NSE actually publishes daily are bhavcopies, settlement prices, the ban list,
turnover, delivery positions, price bands, haircuts and **`CM - Daily Volatility`**. SPAN itself sits
behind the NSE Clearing member portal, which needs member credentials this project does not have and
should not have.

### 2a · Round three, and the actual answer — it is an OPERATOR ACTION, not an engineering one

The search in §7 corrected two of my own errors at once: the file is `nsccl.**YYYYMMDD**.s.spn` (not
`DDMMYYYY`, which is what every probe above used) and it lives on **`nseclearing.in`**, not the
`nseindia.com` archive host. Both corrected and re-probed:

| corrected route | result |
|---|---|
| `www.nseclearing.in/content/span/nsccl.20260817.s.spn` and three sibling paths, both dates | 404 — but an **82,818-byte** 404 page, i.e. a live site rejecting the path, not a dead host |
| `www.nseclearing.in/risk-management/equity-derivatives/nsccl-span` | **HTTP 200, 125,072 bytes** — and a React shell: **zero** anchors, zero asset links in the HTML |
| six guessed API endpoints on that host | 404 |
| **Chromium render** (the project's own fallback for React-shell pages) | `Page.goto` **timed out at 90 s and again at 60 s**, one network request logged, zero anchors recovered |

**Then the decisive check, and it is not another guess.** `marginism` is a working implementation of
exactly this, so I asked what URL IT downloads from. It does not download at all: *"Download the
latest daily SPAN file from your exchange clearing house and point the engine at it... Users are
responsible for fetching the file themselves."*

**Conclusion, and it is different from what I concluded an hour ago: the SPAN file is not behind a
wrong URL, it is behind an INTERACTIVE PAGE.** No stable public endpoint serves it — which is why
the one working open-source implementation does not try. `B36` is therefore an **operator action**,
in the same class as `0.5`'s PAT revoke: one manual download of `nsccl.<YYYYMMDD>.s.spn` from
<https://www.nseclearing.in/risk-management/equity-derivatives/nsccl-span>, dropped anywhere on the
box. Everything below is what runs until that file exists, and it is deliberately built so that the
day it appears the estimator is bypassed rather than reconciled against.

## 3 · What IS published, and it is enough for an estimator

### 3a · The extreme loss margin framework — EXACT, sourced

From NSE's own page, fetched HTTP 200 / 130,178 bytes:
`https://www.nseindia.com/products-services/equity-derivatives-margins`

| case | extreme loss margin |
|---|---|
| **Index derivatives** | **2% of notional value** |
| **Stock derivatives** | **3.5% of notional value** |
| short INDEX option, deep OTM (strike >10% OTM vs previous close) | **3%** |
| short INDEX option, residual maturity > 9 months | **5%** |
| short SINGLE-STOCK option, deep OTM (strike >30% OTM vs previous close) | **5.25%** |
| options expiry day, short index options | **additional 2%** |
| calendar-spread futures | levied on **one third** of the far-month open position value |

Notional is defined by the same page: for a future, contract value at last traded/closing price; for
a short option, the value of the underlying index or the equivalent number of underlying shares.

These are **regulatory facts with a source**, which is exactly the exemption `R.23e` allows for a
constant. Every one goes into the sourced rule store beside the STT and stamp-duty facts, not into a
module as a bare number.

### 3b · The initial-margin principle — sourced, and deliberately only the principle

Same page, verbatim: *"Initial margin requirements are based on 99% value at risk over a one day
time horizon. However, in the case of futures contracts (on index or individual securities), where
it may not be possible to collect mark to market settlement value before the commencement of trading
on the next day, the initial margin is computed over a two-day time horizon, applying the
appropriate statistical formula."*

The page does NOT publish the sigma multiplier, the scanning ranges, or the minimum floors — those
live in the SPAN risk file. **So the multiplier is derived from the stated confidence level rather
than recalled**, and the two-day scaling is applied to futures because the two-day case is the retail
case the page describes.

### 3c · The volatility — NSE's own, daily, per underlying

`https://nsearchives.nseindia.com/archives/nsccl/volt/CMVOLT_<ddmmyyyy>.CSV` — HTTP 200, **303,648
bytes, 5,026 rows**, and it is already one of the 21 published reports. Columns:
`Date, Symbol, Underlying Close Price (A), Underlying Previous Day Close Price (B),
Underlying Log Returns (C) = LN(A/B), Previous Day Underlying Volatility (D),
Current Day Underlying Daily Volatility (E), <annualised>`.

Measured on 2026-08-17:

| symbol | close | daily σ | annualised |
|---|---|---|---|
| RELIANCE | 1,316.01 | 0.0135 | 0.2579 |
| ASHOKLEY | 177.11 | 0.0228 | 0.4356 |
| TATASTEEL | 186.00 | 0.0170 | 0.3248 |

This is the exchange's OWN volatility estimate — the same input SPAN itself is built on — so the
estimator is not substituting a home-grown number for NSE's.

## 4 · The design, and the name

**It is called `FuturesMarginEstimator`, not `SpanMarginCalculator`.** `R.23b`: vocabulary sets
scope. It cannot reproduce SPAN — SPAN is a scenario grid over sixteen price/volatility shifts with
inter-month and inter-commodity spread credits, and the scanning ranges are in a file this project
cannot fetch. Calling it a calculator would claim an accuracy it does not have. It estimates the
margin a broker would block, states its own error direction, and names `B36` as the thing that would
replace it.

```
initial_margin_fraction  = quantile(0.99) x sigma_daily x sqrt(horizon_sessions)
extreme_loss_fraction    = the sourced table in 3a, by product and moneyness
margin_fraction          = initial_margin_fraction + extreme_loss_fraction
margin_rupees            = margin_fraction x notional_rupees
```

- `quantile(0.99)` is `scipy.stats.norm.ppf(0.99)` — **derived from the confidence level NSE
  publishes**, not a typed multiplier;
- `horizon_sessions` is 2 for futures (the page's two-day case) and 1 for options;
- `sigma_daily` is column E of CMVOLT for that underlying, never a fitted substitute;
- every ELM rate comes from the rule store with its source string attached.

**Stated error direction, because an estimator that will not say which way it is wrong is not usable:
this UNDERSTATES margin for a portfolio SPAN would charge more for** — short options far from the
money whose scenario loss exceeds a normal-tail estimate, and expiry-day positions. It **OVERSTATES**
for hedged books, because it credits no spread offsets at all. For a paper-session capital bound,
overstating a hedge and understating a naked short is the safe direction on the leg that matters
(the naked short is refused sooner by the same conservatism that ignores the hedge credit).

## 5 · Signatures

```python
class MarginProduct(StrEnum):
    INDEX_FUTURE / STOCK_FUTURE / INDEX_OPTION_SHORT / STOCK_OPTION_SHORT / OPTION_LONG

@dataclass(frozen=True, slots=True)
class UnderlyingVolatility:
    symbol: str
    session_date: date
    close_price_rupees: Decimal
    daily_volatility: Decimal
    annualised_volatility: Decimal

@dataclass(frozen=True, slots=True)
class MarginEstimate:
    product: MarginProduct
    notional_rupees: Decimal
    initial_margin_rupees: Decimal
    extreme_loss_margin_rupees: Decimal
    horizon_sessions: int
    daily_volatility: Decimal
    extreme_loss_source: str
    caveat: str
    @property
    def total_rupees(self) -> Decimal: ...
    @property
    def fraction_of_notional(self) -> Decimal: ...

class FuturesMarginEstimator:
    def estimate(self, *, product, notional_rupees, underlying, moneyness_fraction=None,
                 sessions_to_expiry=None, is_expiry_day=False) -> MarginEstimate | None
```

`None` when the underlying has no published volatility — an unmarginable contract is not tradeable,
and substituting a peer's sigma is the invented number `R.03` forbids.

## 6 · Acceptance criteria

1. **Reconciles against a known figure.** A NIFTY future at a realistic σ must land in the band a
   real broker quotes (roughly 10–12% of notional); ASHOKLEY, at σ = 0.0228, must land materially
   higher. Both asserted as bands, not points, because the estimator is not SPAN.
2. **Every ELM rate traces to the rule store**, with its source string on the estimate.
3. **The two-day futures scaling is visible** — a future and an option on the same underlying and
   notional must differ by exactly `sqrt(2)` on the initial-margin leg.
4. **`None` on a missing underlying**, never a peer substitution.
5. **The paper session admits real futures trades** at Rs 10,00,000 of capital where the notional
   bound admitted none — measured, both directions, on the 2026-07-31 session.
6. **It never claims to be SPAN**: the caveat string is asserted present on every estimate.

## 7 · Sourcing search (`R.17`)

**Part (a) — a Python SPAN implementation.**

> **~~None exist on PyPI as maintained packages solving this. There is no installable open-source
> implementation that takes an NSE risk file and returns a margin.~~**
>
> **CORRECTED 2026-08-18, same day, before any code was written. That claim was written from memory
> and it was WRONG.** The sourcing gate demanded an actual search; the actual search found one
> immediately. Left visible per `R.25` because the failure mode — asserting an absence of prior art
> from memory and then building on it — is the exact thing `R.17` exists to prevent, and it would
> have cost a from-scratch implementation of a licensed scenario engine.

**PyPI, probed mechanically.** `span-margin`, `nse-span`, `spanlib`, `pyspanmargin`,
`margin-calculator`, `nsepy-margin`, `quantlib-span` — all `No matching distribution found`.
`pyspan` 1.0.0 EXISTS but is unrelated: its own PyPI metadata reads *"A Python package for efficient
data cleaning and preprocessing"*. Downloaded and confirmed.

**`marketcalls/marginism` 0.1.1 — FOUND, INSTALLED, AND IT IS THE REAL THING.**
`pip install git+https://github.com/marketcalls/marginism.git` → `Successfully installed
marginism-0.1.1`. Its exported signatures are exactly the decomposition this needs, not a wrapper:

```
parse_spn(path: str, symbols: Optional[Iterable[str]] = None) -> SpanFile
SpanCalculator(span_file: SpanFile, exposure: Optional[ExposureConfig] = None)  .calculate  .from_file
RiskEngine(calculator: SpanCalculator)                                          .basket  .orders  .from_file
MarginResult(span_margin, exposure_margin, adhoc_margin, expiry_day_elm, net_option_value,
             by_commodity, positions, unmatched)  .total_margin  .summary
SpanFile(file_format, created, business_date, is_settlement, clearing_org, commodities)  .symbols  .get
SymbolResolver(span_file)  .resolve  .tradingsymbols
```

It is a genuine offline SPAN scenario engine over the exchange's own risk arrays — `RiskArray`,
`CombinedCommodity`, `compute_commodity` are all present — and it returns SPAN and exposure
separately, which is the split the paper session needs.

**`R.17`'s fourth test — "ran on real input" — is NOT satisfied and cannot be yet.** It consumes
`nsccl.YYYYMMDD.s.spn`, and that file requires the operator action in §2a. Installability and
signatures are verified; execution is not. Stated rather than implied.

**Part (b) — the normal quantile.** `scipy.stats.norm.ppf` — already a project dependency, already
used elsewhere, no new dependency. Used rather than a typed 2.326 so the confidence level stays the
input.

**Part (c) — the volatility.** Not computed here at all: NSE publishes its own and the estimator
consumes it, which is strictly better than fitting a rival estimate to compare against a framework
built on theirs.

## 8 · Open after this lands

- **`B36`** stays open — this is an estimator and the authoritative SPAN file is still unfetched.
- Spread credits are not modelled (calendar spreads, hedged option books), so a hedged book is
  overcharged; the one-third calendar-spread rule is recorded but not yet applied.
- Peak-margin and cross-margining are out of scope and named here so they are not assumed handled.

---

# ADDENDUM · 2026-08-18 — `B37`, and NSE's volatility convention recovered EXACTLY

## The problem `B37` posed

`CMVOLT` is the CASH file. All five index-future underlyings — `NIFTY`, `BANKNIFTY`, `FINNIFTY`,
`MIDCPNIFTY`, `NIFTYNXT50` — are absent from its 4,767 rows, so index derivatives could not be
margined. Computing an index sigma is easy; computing one on a DIFFERENT convention from the stock
sigmas is the `A.106` defect — two quantities measured two ways feeding one comparison.

So the convention had to be recovered, not chosen.

## What was tried, in order

**1 · Look for an F&O volatility file.** `FOVOLT_<d>.CSV`, `FAOVOLT_<d>.CSV`, `INDEXVOLT_<d>.CSV`
— all **404**. NSE does not publish index volatility.

**2 · Look for index closes.** **FOUND:**
`https://nsearchives.nseindia.com/content/indices/ind_close_all_<ddmmyyyy>.csv` — **HTTP 200,
17,278 bytes**, one row per index per day with `Closing Index Value`. Verified: `Nifty 50` closed
**24,287.65** on 2026-08-17, `Nifty Next 50` at **74,474.10**.

**3 · CALIBRATE the decay against NSE's own published sigmas — and this failed, usefully.**
Fitted an EWMA over 600 symbols with both a published sigma and deep-history closes:

```
lambda=0.90  median |relative error| 27.81%      lambda=0.95  20.82%
lambda=0.92  25.52%                              lambda=0.96  17.50%
lambda=0.93  24.00%                              lambda=0.97  14.35%
lambda=0.94  22.42%                              lambda=0.98  10.13%
```

**Monotone to the edge of the grid and never turning** — which is the signature of a fit that is
compensating for a wrong model rather than finding a parameter. A 10% median error would have been
easy to report as "calibrated". It would have been wrong.

**4 · Read the file's own header, which states the formula.** The decisive step, and the cheapest:

```
Current Day Underlying Daily Volatility (E) = Sqrt(0.995*D*D + 0.005*C*C)
Underlying Annualised Volatility (F)        = E*Sqrt(365)
```

**`lambda = 0.995`, published verbatim by NSE in the column header of the file this project already
downloads.** Confirmed independently by solving `lambda = (E^2 - C^2)/(D^2 - C^2)` row by row over
the real file: **2,714 solvable rows, median 0.9932130462, standard deviation 8.0e-3** — agreeing
with 0.995 to the rounding of the four-decimal sigmas NSE publishes.

**And annualisation is `sqrt(365)`, not `sqrt(252)`.** NSE scales by CALENDAR days. This project's
option analytics annualises by TRADING sessions (246 in 2026, measured) because volatility accrues
while the market is open — a defensible and DIFFERENT choice. The two must never be mixed: a sigma
compared against NSE's margin framework uses NSE's convention, and one used to price an option uses
the session convention. Both are now written down beside each other so the next reader cannot
silently pick the wrong one.

## What follows

- `lambda = 0.995` and `sqrt(365)` are **sourced regulatory/published facts**, quoted with their
  origin, which is the exemption `R.23e` allows.
- Index sigma is computed with NSE's exact recursion from `ind_close_all` closes, so index and stock
  margins are measured the same way.
- **Stated limitation:** `lambda = 0.995` has a half-life of about **138 sessions**, so the estimate
  is dominated by its SEED until roughly two years of index closes have accrued. Seeded from the
  sample variance of whatever history is available and reported with the observation count, so a
  thin estimate is visible rather than confident. The daily run should begin accruing
  `ind_close_all` from today — that is the acquisition (`R.16`), and it is why the limitation is
  temporary rather than structural.

## The lesson

I fitted a parameter for several minutes against a file whose header states that parameter. **Read
the schema before modelling the data it describes.** Recorded as `O.133`.

## The understatement, MEASURED rather than left as a direction

With index volatility now computed on NSE's own convention, the estimator produces:

| index | sessions | daily sigma | annualised | index-future margin on Rs 18.6 lakh |
|---|---|---|---|---|
| NIFTY | 36 | 0.00919 | 17.56% | **5.02%** of notional |
| BANKNIFTY | 36 | 0.01094 | 20.91% | **5.60%** |
| FINNIFTY | 36 | 0.01145 | 21.87% | — |
| MIDCPNIFTY | 36 | 0.00975 | 18.62% | **5.21%** |
| NIFTYNXT50 | 36 | 0.01043 | 19.92% | — |

The volatilities are right — the ordering (BANKNIFTY > FINNIFTY > NIFTYNXT50 > MIDCPNIFTY > NIFTY)
is the ordering these indices actually have, and NIFTY at 17.6% annualised is a believable number.

**The margin is not.** Brokers quote roughly **11-12%** for a NIFTY future; this says **5.02%**. So
the estimator understates index-future margin by about **half**, and that is now a measured figure
rather than the generic "understates" the caveat carried.

**The likely cause, and why it is NOT encoded.** Secondary sources (broker explainers) describe a
SPAN **minimum price scan range of 4% for index and 10% for stock derivatives**, and a **short-option
minimum charge of 3% index / 7.5% stock**. A floor of that shape would close most of the gap. It was
NOT applied, because it could not be verified from a primary source:

- `nseclearing.in/risk-management/equity-derivatives/margins` returns **the byte-identical 130,178-byte
  page** as the NSE one and publishes no scan range, no minimum and no "1.5 standard deviation" rule;
- the legacy `nseindia.com/products/content/derivatives/equities/margins.htm` returns **HTTP 503**.

The same secondary sources also *contradict* NSE's own page on the rates this engine does encode —
they say exposure margin 3% index / higher-of-5%-or-1.5-sigma stock, where NSE's live page says
extreme loss margin **2%** and **3.5%**. Two of those four numbers cannot both be current. **`R.17`:
the directly-fetched primary page wins, and a number available only from a broker blog does not go
into a margin engine.**

**Where the answer actually lives: the `.spn` file.** SPAN scan ranges are IN the risk parameter
file — that is what it is for. So `B36`'s single operator download does not merely replace this
estimator, it also settles the one question the public pages cannot answer. That is the strongest
argument yet for making that download, and it is recorded here rather than argued in chat.

**Until then the honest reading of an index-future margin from this engine is: a floor, roughly half
of what will actually be blocked.** For a paper-session capital bound that means the session will
hold about twice the index-future exposure a real account could carry — which is the WRONG direction
of error, and is why the index bots stay `[~]` rather than being declared done.
