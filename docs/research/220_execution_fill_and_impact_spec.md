# 220 — `L1.05` + `L1.06` execution fill and market-impact engine (institutional spec)

**Date:** 2026-08-12 · **Plan entries:** `L1.05` (fill / slippage model) + `L1.06` (market-impact
fill model) · **Todo:** `1.39` + `1.40` · **Sequencing:** `A.92` (these move ahead of `L1.02`–`L1.04`,
and build as ONE engine because with a real book the size-dependence IS the calculation)
**Rules in force:** `R.03`, `R.04` (maturity ladder), `R.05`, `R.07`, `R.23`

---

## 1. The question this engine answers

> If I send an order for N units of this instrument right now, what execution price should I expect
> against the mid at the moment of decision — and how much should I trust that number?

It answers in three parts, because they come from different evidence and fail differently: the
**spread** I will cross, the **impact** of my own size, and the **confidence** in both.

## 2. What the research measured — and what it ruled OUT

A full method-and-sourcing pass ran 2026-08-12 against the REAL tape (2026-08-12 session:
7,804,159 rows, 1,420 instruments, 4h45m). Most of its value is negative results, and they
determine the design more than the positive ones do.

### 2.1 Ruled out, with the measurement that ruled it out

| Method | Why it is not available | Measured |
|---|---|---|
| Effective/realised spread at scale | Trade prices recoverable only when `ΔV == last_traded_quantity` | Exact-print intervals are **0.0–2.4% of volume**, all small trades |
| Interval VWAP from `average_traded_price` deltas | ATP is stored as an INTEGER paise, so its ±0.5 paise rounding is multiplied by cumulative volume and divided by interval volume | Noise bound **1,012–5,684 bps** against a true spread of 0.69–3.84 bps |
| Kyle's lambda | No usable signed-volume series | Regression of 5-min mid returns on signed volume: **R² = 0.001, 0.026, 0.000** |
| Square-root law fitted intraday | Same | |ret| vs √participation: **R² = 0.070, 0.010, 0.014** |
| Huang-Stoll decomposition | Needs full TAQ; also premised on a designated market maker, which NSE does not have | — |
| Hasbrouck VAR / propagator | Needs signed trade-by-trade; deconvolution is highly sensitive to sign-classification noise | — |
| EDGE / Corwin-Schultz / Abdi-Ranaldo on daily bars | Overstate NSE spreads by 1–2 orders of magnitude | EDGE: RELIANCE **25.0 bps**, HDFCBANK **54.5 bps**, SUZLON **150.4 bps** — the book says **0.7–3.8 bps** |

**Consequence:** anything requiring trade prints or a signed-volume series is out. This engine is
built on what a snapshot book actually supports, and says so rather than implying more.

### 2.2 What IS available, and what it showed

1. **Effective spread ≈ quoted spread on NSE.** Measured ratio **1.000 / 1.000 / 0.952** on three
   instruments; prints sit at bid or ask (55%/43%), only **2.0% strictly inside**. There is no
   meaningful price improvement to model for small orders — the spread term is directly observable
   and needs no estimation machinery at all.
2. **Visible depth is a rounding error on the real book.** The feed carries
   `total_buy_quantity`/`total_sell_quantity` — NSE's WHOLE-book pending quantity, which most
   systems never see. Median visible 5-level depth is **0.3%** of it (0.2–0.49% by decile).
3. **The book exhausts almost immediately, and then LIES COMFORTABLY.** Walking the visible ladder:
   at 1e-3 of session volume **82.4%** of snapshots exhaust five levels; at 5e-3, **100%**. Cost
   saturates at 2.24 → 2.29 bps as it does so. **A naive book-walk reports a bounded, comfortable
   number exactly when the true cost becomes unbounded.** This is the single most dangerous failure
   mode in the whole design.
4. **Mechanical book-walk measures the spread, not impact.** A hierarchical fit over **679,354
   uncensored observations across 1,413 instruments** gives cost scaling as `notional^0.107` —
   against the square-root law's 0.5. Within visible depth, cost is nearly flat in size.
5. **Shrinkage HURTS the spread term.** Splitting the session and capping observations per
   instrument, per-instrument estimates beat shrunk ones at every sample size down to five
   (RMSE 7.328 vs 9.462 at n=5; median shrinkage weight 0.99). Between-instrument variance
   dominates measurement variance — spread is a high-signal, low-noise quantity that one
   observation nearly pins. **Pooling belongs on the impact coefficient only**, where per-instrument
   observations really will be near zero.
6. **The universe has clean, exploitable cross-sectional structure**, and these are the pooling
   priors:
   ```
   log(quoted_spread_bps) = 8.285 − 0.346·log(turnover)     R² = 0.565, n = 1,403
   log(visible_depth_Rs)  = 1.508 + 0.563·log(turnover)     R² = 0.354, n = 1,403
   ```
   Turnover deciles are perfectly monotone in spread: decile 0 **19.34 bps** → decile 9 **2.20 bps**.
   The depth exponent **0.563** sitting near ½ is consistent with square-root liquidity scaling.
7. **NSE's own official liquidity measure IS a book-walk**, which is both a precedent and a free
   external ground truth. From the Nifty methodology document (fetched):
   ```
   Ideal price     = (best bid + best ask) / 2
   Actual price    = Σ(quantity × price) / total quantity        [walking the book]
   Impact Cost (%) = [(Actual − Ideal) / Ideal] × 100
   ```
   Nifty 50 eligibility requires impact cost **≤ 0.50% for a Rs 10 crore portfolio on 90% of
   observations**; Nifty 500 uses ≤ 1%. Reproducing NSE's published per-stock impact cost is a
   validation almost nobody else can run.

### 2.3 Regime breaks that must split any calibration

NSE changed the tick size effective **2025-04-15** (sub-₹250 securities to ₹0.01, banded above),
stock options again **2025-11-03**, and moved F&O expiry Thursday → **Tuesday from 2025-09-01**.
Stocks mechanically flipped large-tick/small-tick class independent of any change in real activity,
and near-expiry liquidity concentration moved. Bucketing and calibration split at these dates; the
dates come from the `L0.31` rule store, not from constants here.

## 3. Why this is an ENGINE (`R.23(a)`)

- **Inputs:** the real 5-level book, the whole-book pending quantity, and real daily turnover.
- **Solver:** a censored book-walk with an explicit censoring boundary, plus a hierarchical
  cross-sectional impact fit that supplies the estimate above that boundary.
- **State:** per-(instrument, bucket) spread and impact parameters that accrue and re-fit, and an
  empirical-Bayes weight that shifts from pooled prior to own estimate as observations arrive.
- **Output that changes behaviour:** an expected execution price and a cost interval that `L1.02`
  and `L1.03` add to the statutory hurdle. Today's `L1.01` breakeven is a FLOOR; this is what makes
  it a hurdle.

**SOTA analogs:** Almgren-Chriss-Thum-Hauptmann (2005) for the impact functional form, Weber &
Rosenow (2005) for the virtual-vs-actual book-walk correction, and NSE's own published impact-cost
methodology for the mechanical half.

## 4. The model

For an order of `Q` units on side `s` at decision time `t`:

```
expected_cost_bps(Q) = spread_cost_bps + impact_cost_bps(Q)
```

**Spread term** — half the quoted spread against the mid, taken straight from the book, per
instrument, no pooling (§2.2.5). Reported against the micro-price too, since a size-imbalanced book
means the mid is not the fair value.

**Impact term** — piecewise by whether the visible book covers `Q`:

- **Uncensored** (`Q ≤ visible depth on side s`): walk the ladder exactly. This is NSE's own
  definition and it is arithmetic, not estimation.
- **Censored** (`Q >` visible depth): the walk is a lower bound and is NOT extrapolated. The
  estimate becomes `Y · σ · √(Q / V)` with `V` the whole-book pending quantity (which is ~300× the
  visible), `σ` the instrument's realised volatility, and `Y` fitted hierarchically per turnover
  decile × tick regime. The exponent is a FITTED parameter with 0.5 as its prior, because the
  empirical literature spans 0.4–0.7 and the review of it is explicit that ½ is a mid-range regime
  rather than a law at all sizes.

Every output carries `is_censored`, and a censored answer is returned as an INTERVAL, not a point.

**Maturity ladder (`R.04`)** — the full estimator ships on day one; only the ACTIVATION of the
per-instrument fit is gated:
1. *pooled* — bucket prior for `Y`, own spread. Available immediately, for every instrument.
2. *shrinking* — per-instrument `Y` blended toward the bucket via `w = τ²/(τ² + se²)`.
3. *own* — `w → 1`. No code changes between stages; the weight is computed, not switched.

## 5. Modules

```
src/nse_algo_trader/execution_fill/
  quoted_spread_observer.py            # per-instrument spread/mid/micro-price from the real book
  order_book_walk_calculator.py        # exact ladder walk + the censoring boundary
  instrument_liquidity_buckets.py      # turnover deciles x tick regime, from real ADV
  market_impact_estimator.py           # hierarchical sqrt-law fit; pooled -> shrunk -> own
  execution_fill_model.py              # CORE: the two terms into one ExpectedFill
  execution_fill_parameter_store.py    # SQLite carried state + accrual counts
  realised_fill_reconciler.py          # modelled vs realised, once real fills exist (L1.07's seam)
```

## 6. Verification

- **Unit** — book-walk against hand-computed ladders; the NSE formula reproduced exactly.
- **Property** — cost non-decreasing in size; walking 0 units costs the half-spread; a walk that
  consumes exactly the visible depth is the last uncensored point; shrinkage weight in [0,1] and
  monotone in observation count.
- **Adversarial** — a crossed book; a one-sided book; a book whose levels are all zero-padded;
  `Q` far beyond the whole-book quantity; an instrument with one observation; a tick-regime
  boundary date.
- **Real-data (`R.05`)** — the whole 19.4M-row tape. Reproduce NSE's own impact-cost definition at
  ₹10 crore and check the Nifty-50 constituents come in under 0.50%; confirm the censoring rate by
  size bucket matches the measured 82%/100%; confirm the fitted exponent against the measured
  0.107 uncensored.

## 7. Sourcing (`R.17`, run 2026-08-12 on this aarch64 host)

Nineteen candidates installed and RUN in a throwaway venv. Verdicts on the ones that matter:

| Candidate | Result |
|---|---|
| `hftbacktest` 2.4.4 | **INSTALLS** (`manylinux_2_28`, glibc 2.34 ≥ 2.28). Its `event_dtype` maps exactly onto this project's bitemporal tape; 4,000 real NSE snapshots were converted to 40,000 events and the engine RAN, reconstructing a book matching the tape. Exposes real probabilistic queue models. **DEFERRED, not rejected** — queue position is a MAKER-order concern and belongs to `L1.14`; this slice prices taker fills and would not use it. Recorded so `L1.14` starts from a mechanically verified dependency |
| `bidask` (EDGE) 2.1.0 | Installs and runs; **REJECTED for cost** — returns 25–54 bps where the book measures 0.7–3.8. Usable only as a cross-sectional ranker for the pre-tape era |
| `nautilus_trader` 1.231.0 | **Confirmed uninstallable, and the earlier reason was wrong**: wheels ARE published for aarch64 but target `manylinux_2_35` against this host's glibc 2.34; the sdist then fails on a packaging bug (`failed to load manifest for workspace member .../examples/tutorials`). Its `FillModel` is naive random slippage regardless |
| `priceprop` 1.0.2 | Installs but `import` fails — a Python-2 implicit relative import. One-line fix, and the only propagator implementation in existence. **Useless without trade prints**, so not vendored |
| `zipline-reloaded` slippage | Source read: `volume_share**2 · price_impact · price` — **quadratic** in participation, contradicted by every measurement in §2. Cited as what not to ship |
| `tclf` 0.3.0 | Already installed; ran correctly on 2,368 real prints. Kept — the limit is the data, not the library |
| `frds`, `PyPortfolioOpt`, `vectorbt`, `order-book`, `mlfinlab`, `arbitragelab`, `tcapy`, `mbt-gym`, `abides-markets`, `pylob`, `qlib` | Rejected: uninstallable here, wrong problem, or fewer than 20 lines |
| Roll / Corwin-Schultz / Abdi-Ranaldo / Amihud | **No maintained PyPI implementation exists for any of them.** Each is under 20 lines |

**Bespoke in-repo, one deferred dependency.** Nothing available implements a censored book-walk over
a 5-level snapshot tape with whole-book scaling and a hierarchical prior.

## 8. Open items (`R.11`)

- All intraday measurements rest on **one session** (2026-08-12). The cross-sectional elasticities
  are stable across 1,403 instruments, but the time-series claims must be re-checked as the tape
  accumulates. The engine reports how many sessions its fit rests on.
- **Selection bias is unaddressed and unaddressable today.** Real fills will be signal-triggered, so
  a realised-vs-predicted comparison can look good or bad depending on whether the signal trades
  with contemporaneous drift. Honest validation needs randomised order timing, which needs an order
  path that does not exist. Recorded for `L1.07`.
- Queue position is not modelled. Every number here is a TAKER cost.
