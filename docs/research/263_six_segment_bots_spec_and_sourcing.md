# 263 · The six segment bots — spec, sourcing evidence and what was measured

**Built 2026-08-18 under decision `A.141`** (operator reordered `A.130`'s spine-first sequence to put
all six bots to paper now), with `A.142` covering the F&O capture extension and `A.143` the daily-run
timeout found on the way.

---

## 1 · What was built

| module | what it is |
|---|---|
| `option_analytics/black_scholes_option_analytics_engine.py` | pricing, implied volatility, Greeks, trading-session year fractions |
| `strategy/volatility_risk_premium_engine.py` | per-underlying realised volatility (EWMA) and standardised variance risk premium |
| `strategy/futures_basis_carry_engine.py` | basis, annualised implied carry, standardised dislocation, open-interest taxonomy |
| `segment_bots/segment_bot_foundation.py` | carried state, maturity, relevance, cadence, cross-sectional cut, signal assembly |
| `segment_bots/cash_intraday_mean_reversion_bot.py` | `L5.26` |
| `segment_bots/option_volatility_premium_bots.py` | `L5.27`, `L5.28` |
| `segment_bots/futures_basis_carry_bots.py` | index futures, stock futures, MCX |
| `segment_bots/segment_bot_registry.py` | builds all six, runs the `L5.29` gate on throwaway instances |
| `segment_bots/segment_universe_assembler.py` | the spine's I/O: real universes and prices per segment |
| `dashboard/segment_bot_surface_renderer.py` + `/bots` | `R.08` surface |

## 2 · Sourcing search, run 2026-08-18 — mechanical evidence only (`R.17`)

Decomposed before searching: **(a)** option pricing, implied volatility inversion and Greeks;
**(b)** a variance-risk-premium construction; **(c)** a futures cost-of-carry monitor.

### (a) Option analytics — `vollib` 1.0.12 ADOPTED, `QuantLib` 1.43 REJECTED WITH THE MEASUREMENT

Both installed into the project venv and exercised on NSE-shaped inputs. A 7-session at-the-money
NIFTY-like call, `S = K = 24,800`, `r = 6.5%`, `sigma = 12%`:

```
vollib     price Rs 180.2222   delta 0.5332021587   vega 13.6539   theta -14.0261   rho 2.5014
           implied_volatility(180.2222) -> 0.11999998244077295
QuantLib   price Rs 180.2222   delta 0.5332          vega 1365.3902
           impliedVolatility -> 0.12
           2,000 prices in 18 ms (vollib: 9 ms)
```

**Every Greek convention was checked against a numerical derivative rather than a README:**

| Greek | reported | numerical | unit |
|---|---|---|---|
| delta | 0.5332021587 | 0.5332021197 | per 1 unit of underlying |
| vega | 13.6539 | 13.6568 | per **1 percentage point** of volatility |
| theta | -14.0261 | -14.4759 | per **calendar day** |

Theta's 3% gap is the curvature of theta across the day the finite difference spans, not an error.

**Boundary behaviour, which decided it.** A zero premium on a deep out-of-the-money strike:
`vollib` returns an implied volatility of **0.0**; the deprecated `py_vollib` shim returned
**0.12** — the last value it had been handed. That is the difference between "this quote carries no
information" and "this quote agrees with my prior", and the second one silently manufactures a
signal. `vollib` is imported directly and `py_vollib` is not used.

**QuantLib is rejected on three mechanical grounds, none of them correctness**: its Greeks require
rebuilding a pricing engine per evaluation (2x slower on the same contract); its `India(NSE)`
calendar duplicates `NseTradingSessionCalendar`, which is this project's own sourced authority
(`cal.isBusinessDay(15 Aug 2026)` correctly returns False, so the two would agree — and two
calendars that agree today are two calendars that can disagree later); and its vega is quoted per
100% of volatility against `vollib`'s per 1 point, a 100x unit difference between two libraries in
one codebase. Kept installed — it is the right tool the day this project needs an American exercise
or a term structure.

### (b) Variance risk premium — NO INSTALLABLE PRIOR ART, built here

Searched for a maintained Python package computing a variance risk premium against realised
volatility. Nothing installable returned: results were realised-volatility calculators and academic
replication notebooks. The construction here is the standard Carr-Wu shape with the model-free
VIX-style strip replaced by the at-the-money implied volatility this project can actually observe —
NSE publishes no free variance swap rate, and integrating a synthetic strip over a 208,191-row option
chain is a batch job, not a decision-path computation. **The approximation is stated in the module
docstring rather than hidden**: at-the-money implied volatility understates the premium when skew is
steep, which biases the engine toward FAIR, which is the safe direction for a seller.

### (c) Futures cost-of-carry — NO PRIOR ART, built here

Same search shape, same outcome: P&L calculators and broker wrappers, nothing that annualises a
basis and standardises it against a contract's own history. Built on Welford online moments, which
`numpy` and `statistics` both leave to the caller.

**Sources consulted:**
[scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html) ·
[QuantLib](https://www.quantlib.org/) ·
[trading-pnl](https://github.com/danielhorizon/trading-pnl) ·
[pyalgotrading](https://github.com/algobulls/pyalgotrading)

## 3 · What the data actually supports, measured 2026-08-18

| segment | universe assembled | source | cadence |
|---|---|---|---|
| cash intraday | **3,966** instruments | `price_bars`, 5m, latest 2026-08-17T15:25 IST | INTRADAY |
| index options | **5,042** contracts, 5 spots | `IDO` bhavcopy 2026-08-03 | once per session |
| stock options | **25,785** contracts, 208 spots | `STO` bhavcopy | once per session |
| stock futures | **622** contracts, 208 spots | `STF` bhavcopy | once per session |
| index futures | **15** contracts, 5 spots | `IDF` bhavcopy — only 540 rows exist (`B31`) | once per session |
| MCX | **0** | none exist (`B30`) | activates on nothing |

The depth tape carried **2,135,786 ticks over 1,845 instruments** today, **all NSE cash** — no NFO or
MCX token has ever been subscribed. That single fact is why five of six bots are
`ONCE_PER_SESSION`, and `A.142` is the decision to fix it at the subscription list.

## 4 · Conformance — the `L5.29` gate, and the two defects it caught

All six return **zero violations**. Two real defects were caught by running it rather than by
reading the code:

1. **`source` was `"{identity}:{cadence}"`.** The suite requires `source == bot_identity` exactly,
   and it is right to: the source is the key a track record is filed under, and a decorated one files
   the same bot's trades under two names the day the decoration changes. Failed on all five non-cash
   bots at once; the cash bot alone would have shipped it.
2. **The identity must contain its own segment value.** `index_option_...` did not contain
   `index_options`. Renamed rather than the check weakened.

## 5 · Two defects in my own bot, found by probing rather than by review

1. **One constant standing for two different measurements.** `MINIMUM_REGIME_CONCENTRATION` is a bar
   on the belief's ENTROPY; I also used it as a bar on `1 - trending mass`, an unrelated quantity.
   Replaced with a comparison between two probabilities in the same distribution — `P(trending) >=
   P(ranging)` — which needs no constant at all. Same shape as the `A.106` defect: a calibration
   looked up under a coordinate measured a different way.
2. **An invented threshold that silently refused everything.** I wrote `0.45 / 0.60` where the
   real-data probe uses `0.40 / 0.50`. `concentration` is `1 - normalised entropy`, and a
   distribution as peaked as `{0.70, 0.15, 0.10, 0.05}` scores only **0.34** — so the bot abstained
   on every instrument of a 40-name probe and looked like working caution. Numbers invented rather
   than measured fail in exactly this direction: silently, and in the safe-looking one.

Verified after the fix: on a peaked ranging belief the cash bot proposes at step 78 of a synthetic
session with `edge = 98.5 bps, conviction = 0.579`, source exactly `cash_intraday_mean_reversion_bot`.

## 6 · Open, and named (`R.11`)

- **`B33`** — no live intraday loop yet. `/bots` builds fresh bots per request, so
  `universe readiness` reads 0.0%: one page load is one observation and a dispersion needs three.
  The session runner that carries state across a session, routes proposals into
  `PaperTradingSessionRunner` and persists what the surface READS is the remaining piece, and it is
  what the operator actually asked to see.
- **`B34`** — the risk-free rate is a stated 6.5%, not a curve.
- **`B35`** — registration contaminates carried state; fixed by convention, not by type.
- **`B30`/`B31`** — MCX has no data at all; index futures has 540 rows.
- **`R.05` is PARTIALLY satisfied**: the universes, prices and instrument facts are real and the
  bots were driven over them. What has NOT happened is a real closed trade from any of the five new
  bots, because that needs the loop in `B33`. Stated as an open blocker rather than claimed.
