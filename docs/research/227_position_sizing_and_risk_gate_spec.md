# 227 · How much, or none — the position sizer and the pre-trade risk gate

**Feature**: `F03` (first slice) · **Operator decisions**: `A.104` (2026-08-13) ·
**Plan entries**: `L1.09` · `L1.10` · `L7.01` · `L3.05` · `L7.02` · `L7.03` ·
**Closes**: todo `0.8`, the `R.11` deferral that has stood since `capital_configuration` was built.

---

## 1 · What does not exist today

Nothing in `src/` sizes a trade and nothing in `src/` can refuse one. `capital_configuration` — the
module that holds the operator's rupees and refuses to guess them — has had **zero callers** since
the day it was written. `TradingIntent` (`order_path/trading_intent.py:92`) carries a `quantity: int`
whose only validation is `quantity > 0`; the number itself has always come from a caller that does
not exist.

So this feature is the first thing in the rebuild that answers **how much**, and the first that can
answer **none**.

## 2 · Why the sizer and the gate are one engine

`A.104` decision 1. Sizing to zero and refusing produce the same order — no order — and they are
reached by the same walk over the same facts. Built apart, the sizer emits quantities nothing
inspects and the gate inspects quantities nothing emits: two consumer-queued fragments, which is
precisely the failure `FEATURE_MAP` was written to stop. They are one decision with one output:

```
SizedOrder = (quantity, verdict, the full reasoning that produced both)
```

`quantity == 0` and `verdict.refused` always agree, because one is computed from the other rather
than checked against it.

## 3 · The sizing algorithm

`A.104` decision 2: **volatility target, then Kelly cap.**

### 3.1 · The risk budget (volatility target)

The position's expected rupee volatility over the decision's horizon is targeted, not its notional:

```
sigma_instrument   = realised volatility over the horizon, in bps of price (EWMA, close-to-close)
risk_budget_rupees = deployable_rupees * target_risk_fraction
notional_vol_target = risk_budget_rupees / (sigma_instrument / 10_000)
```

`target_risk_fraction` is **not a constant** (`R.03`). It is `1 / concurrent_position_capacity`,
where the capacity is the number of positions the six segments can carry simultaneously under
`R.10`'s equal-by-default rule — a structural fact of the configured segment set, not a tuning knob.
When the segment set changes, the fraction changes with it and nothing needs editing.

### 3.2 · The Kelly cap

Kelly answers a different question — *is this edge worth that much risk* — and it is a **cap**, never
the size itself, because Kelly is fragile to edge mis-estimation in exactly the direction that hurts:
a 2× overestimate of edge is a 2× oversize.

```
f_kelly       = edge_fraction / variance_fraction        (per-unit Kelly on the calibrated edge)
shrinkage     = edge^2 / (edge^2 + standard_error^2)     (see below)
f_used        = f_kelly * shrinkage
notional_kelly = deployable_rupees * f_used
```

**The Kelly fraction is not a chosen multiplier.** The conventional "half-Kelly" or "quarter-Kelly"
would be exactly the magic constant `R.03` forbids. Instead the estimate is shrunk toward zero by its
own measured reliability:

```
shrinkage = edge² / (edge² + se²)
```

This is the standard James–Stein / Bayes shrinkage weight, and every term is already published by
`F01`'s calibrator: `ReversionCapture.mean_captured_bps` and `.standard_error_bps`
(`cost_gate/mean_reversion_edge_calibrator.py:99`). Its behaviour is the point:

| Edge quality | `t = edge/se` | shrinkage | effect |
|---|---|---|---|
| well measured | 5 | 0.96 | size ≈ full Kelly |
| marginal | 2 | 0.80 | sized down a fifth |
| indistinguishable from zero | 1 | 0.50 | halved |
| noise | 0.5 | 0.20 | all but eliminated |

An edge measured badly therefore sizes small **by construction**, not by anybody's caution. Note
this makes the `M10` open caveat (optimistic standard errors in the calibration) a direct input to
position size and raises its priority — recorded in `BACKLOG.md`.

`variance_fraction` is taken from the same calibration's `mean_captured_sigma` and the instrument's
realised volatility, not assumed.

### 3.3 · The two are combined by minimum, then discretised

```
notional = min(notional_vol_target, notional_kelly)
quantity = floor(notional / reference_price) rounded DOWN to a whole multiple of lot_size
```

`L1.09`: lot size is read from the **live daily Kite instrument dump**
(`kite_instrument_master.py:133`, surfaced as a `LOT_SIZE` market-rule fact by
`market_rules/instrument_master_rule_observer.py:49`). The archived system's hardcoded lot map went
stale — NIFTY is 65, not 75 — and the rule here is that a hardcoded lot size is a defect, not a
fallback. When the dump has no lot size for an instrument the sizer **refuses**; it does not assume 1.

Rounding is always **down**. A rounded-up lot is a position larger than the risk budget justified,
and "one more lot" is how a discretisation becomes a leverage decision. When rounding down yields
zero lots, the answer is zero and the verdict says *the smallest tradeable unit exceeds the risk
budget* — a real and common state for a ₹1 lakh book against a large-lot contract, and one that must
be legible rather than mysterious.

## 4 · The gate — three tiers, and the operator key only tightens

`A.104` decision 3. Evaluated in order; the first refusal wins and every tier's verdict is recorded
even when an earlier one already refused, because "why was this refused" must not depend on
evaluation order.

### Tier 1 — regulatory walls (sourced facts, never derived, never overridable)

| Wall | Source | Reader |
|---|---|---|
| F&O ban list | NSE daily ban file | `nse_ingest/fo_ban_list_adapter.py` → `BitemporalIngestStore.rows_for("fo_ban_list", …)` |
| MWPL / market-wide position limit | NSE MWPL report | `nse_ingest/mwpl_position_limits_adapter.py` |
| Circuit bands, GSM/ASM surveillance | NSE circuit-band + surveillance files | `nse_ingest/circuit_band_surveillance_adapter.py` |

These are **binary or published numbers**, not statistics. An F&O ban is a fact about today, and a
system that inferred it from its own price history is a system that confidently trades a banned
scrip. Point-in-time reads only (`rows_for(..., known_by=)`), so a backtest cannot see a ban
declared after the decision.

### Tier 2 — derived limits (per instrument and segment, from its own history)

- **Maximum notional per order** — a percentile of the instrument's own traded value, so an order
  that would be a visible fraction of the day's volume is refused before impact modelling is asked
  to excuse it.
- **Price collar** — a percentile of the instrument's own realised move over the decision horizon,
  so a limit price far outside what this scrip actually does is refused.
- **Maximum leverage** — notional over deployable capital, bounded by the segment's own margin
  regime rather than a chosen multiple.
- **Order rate** — the existing limiter already holds flow under the 10/sec registration threshold
  (`A.101` decision 2, `L3.19`); the gate reads its realised rate and refuses rather than queueing,
  because an order deferred past its decision horizon is a different trade.

### Tier 3 — the operator override, which can only TIGHTEN

Every tier-2 limit accepts an operator value. It is applied as `min(derived, operator)` for a
ceiling and `max(derived, operator)` for a floor — **the direction that shrinks the permission**. A
loosening override is refused at load time with the derived figure named. A limit that can be
loosened is not a limit, and `R.22`'s two-key rule survives only if one key cannot be turned
permanently.

## 5 · The kill switches (`L7.03`) and the state they need

The only part of this engine that carries state across decisions, which is what makes it an engine
rather than a function:

- **Daily loss limit.** Session realised P&L against a limit derived from the book's own daily
  volatility — `limit = z * sigma_daily_book`, where `z` is set by the tolerated frequency of a
  false halt over a trading year (a Bonferroni-style quantile: one spurious halt per N sessions), not
  chosen. Before enough sessions exist to estimate `sigma_daily_book`, `R.04` applies: the full
  algorithm is built and its **activation** is gated behind a maturity ladder — never a reduced
  algorithm, never a guessed rupee limit.
- **Drawdown kill.** Peak-to-trough on the **Decimal** equity curve, from the paper capital ledger
  (`L1.18`) in paper and the deployable resolver (`L1.17`) in live. Both expose
  `deployable_rupees`, so the source is chosen once at construction.
- **Carried state** persists to SQLite: session realised P&L, peak equity, per-symbol and aggregate
  exposure, order timestamps for the rate window. **A restart must not reset a kill.** A halt that
  forgets itself when the process bounces is not a halt, and process bounces are exactly what
  follows a bad morning.

Both kills are **latches**: once tripped they stay tripped for the session, and clearing one is an
operator act (`R.22`).

## 6 · What it consumes and what it produces

**Consumes** — every one of these already exists and is named here so the wiring claim is checkable:

| Input | Source | Location |
|---|---|---|
| deployable capital | `PaperCapitalSnapshot` \| `DeployableCapital` | `paper_capital_ledger.py`, `deployable_capital_resolver.py` |
| operator ceiling | `TradingCapital` | `capital_configuration.py` — **first real caller, closes `0.8`** |
| calibrated edge + standard error | `ReversionCapture` | `cost_gate/mean_reversion_edge_calibrator.py:99` |
| price history for realised vol | `BitemporalBarStore.bars_as_of` | `bitemporal_bar_store.py:338` |
| lot size | `InstrumentMasterStore.instruments_as_of` | `kite_instrument_master.py:522` |
| regulatory facts | `BitemporalIngestStore.rows_for` | `nse_ingest/bitemporal_ingest_store.py:292` |

**Produces** — `SizedOrder`, carrying the quantity, the verdict, which bound bound it, and every
intermediate figure. It is what constructs a legal `TradingIntent`
(`order_path/trading_intent.py:92`), so `F02`'s order path gains its missing front half and the
`quantity` field stops being caller-supplied.

## 7 · Modules (`R.14` — each name states its job)

```
src/nse_algo_trader/sizing/
  realised_volatility_estimator.py    sigma from real bars; refuses on thin history
  kelly_edge_scaler.py                shrunk Kelly fraction from a ReversionCapture
  volatility_targeted_position_sizer.py   risk budget -> notional -> lots -> quantity  (L1.10, L7.01, L1.09)
  pre_trade_risk_gate.py              the three tiers and their verdicts               (L3.05, L7.02)
  session_risk_state_store.py         carried P&L, peak equity, exposure, rate window  (L7.03)
```

## 8 · Verification plan

- **Unit** — every tier, every refusal reason, the shrinkage table in §3.2 reproduced exactly, lot
  rounding down at the boundary, the zero-lots answer.
- **Property** — quantity is never larger than the risk budget justifies for any legal input;
  `quantity == 0` if and only if the verdict refuses; an operator override never widens a limit.
- **Adversarial** — the axes this test file must NOT hold constant (`O.85`): concurrency (two
  decisions racing the same session state), a restart mid-session, a missing calibration, a missing
  lot size, zero volatility, a banned scrip, a scrip whose ban arrives after the decision, a limit
  that would be loosened, a latch that has already tripped, and a Decimal magnitude beyond the
  declared range.
- **`R.05`** — the sizer must run against the real bar store, the real instrument master and the real
  calibration database on the full universe (`R.09`), not a sample. Until it has, the pass is an
  open blocker in `BACKLOG.md`.

## 9 · Sourcing — what was searched, run, and why rejected (`R.17`)

Probed on this box 2026-08-13: `riskfolio-lib`, `pyportfolioopt`, `empyrical`, `quantstats`, `ffn`,
`pyrb`. Two were installed and exercised on real input.

**`riskfolio-lib` 7.3.0 — INSTALLED, INTROSPECTED, REJECTED.** Maintained and substantial. Real
signature observed: `Portfolio.optimization(self, model='Classic', rm='MV', obj='Sharpe',
kelly=None, rf=0, l=2, hist=True)`. It **does** carry a `kelly` option — and that is portfolio-level
log-utility inside a mean-variance optimisation over a **returns matrix across assets**, returning
weights. Three mechanical facts decided it:
1. **Its unit of work is not this decision.** F03 sizes ONE order from a bps edge, one instrument's
   realised volatility and a lot size. There is no returns matrix at the decision point, and
   manufacturing one to reach the API would be inventing the input to fit the tool.
2. **Float throughout.** This project carries money in `Decimal` end to end and enforces it
   (`rupee_literal_detector`, `R.03`); the paper ledger refuses a `float` outright. Round-tripping
   rupees through `float64` to reach a weight is the precision leak the rest of the codebase spends
   effort preventing.
3. **It resolves nothing this feature actually needs** — no lot discretisation, no regulatory tier,
   no carried session state, no kill latch. Installing it also pulled `numpy 2.5.2`, which pip
   flagged as incompatible with `tclf 0.3.0` already in this environment.
   **It is the right tool for `F24`** (the capital allocator across segments) and is recorded there.

**`empyrical` 0.5.5 — ALREADY PRESENT, EXERCISED, REJECTED FOR THE MONEY PATH.** `pip install`
fails to build it from source here ("Failed to build 'empyrical' when getting requirements to build
wheel"), but it imports from the existing venv and runs. Real signatures:
`max_drawdown(returns, out=None)`, `annual_volatility(returns, period='daily', alpha=2.0, …)`. Ran on
real input: `max_drawdown([0.01,-0.05,0.02,-0.10])` → `-0.12790000000000004` — **the float error is
visible in the answer**. The drawdown kill compares against a `Decimal` rupee limit, and
peak-to-trough over a Decimal equity curve is exact and four lines long. Rejected on precision, not
on quality; its last release predates 2021, so it would also be a maintenance liability.

**`pyportfolioopt` 1.6.0 · `quantstats` 0.0.81 · `ffn` 1.1.5 · `pyrb` 0.10.11 — resolve on PyPI, not
installed.** All four are portfolio-construction or performance-reporting libraries operating on
returns series across assets — the same category mismatch as `riskfolio-lib`, and rejecting the
strongest member of a category on its unit of work rejects the category. Recorded rather than
silently skipped (`R.11`); if `F24` revisits allocation, `pyportfolioopt` is the second candidate to
exercise.

**Conclusion**: build it. The shrunk-Kelly scaler is roughly forty lines of arithmetic over numbers
`F01` already publishes; what is large here is the gate's tiers and the carried state, and no library
supplies those because they are this exchange's rules and this book's history.
