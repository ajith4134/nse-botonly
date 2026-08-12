# 219 — `L1.01` NSE transaction-cost engine (institutional spec)

**Date:** 2026-08-12 · **Plan entry:** `L1.01` (⟨IV⟩ · base) · **Todo:** `1.35`
**Supersedes in part:** `research/164` (2026-08-03), which specified `L1.01`+`L1.02`+`L1.05` as one slice
against a codebase that no longer exists (the reset of 2026-08-10 deleted
`paper_trading/indian_trading_cost_model.py`, so the "SALVAGE, not greenfield" finding in `164` §9 is
void). `164`'s rate research and OSS triage remain valid and are carried forward here.
**Rules in force:** `R.03` (no hardcoded values), `R.05` (real-data pass), `R.07` (engine-grade),
`R.23` (code-generation loop), `R.18` (one engine).

---

## 1. Scope — what this slice is and is NOT

`L1.01` is the **deterministic charge engine**: given a real instrument, a real price, a real quantity
and a real DATE, it returns what the exchange, the government and the broker will actually take, as an
exact rupee decomposition with provenance — and the price move required to get it back.

**In scope**
- Every statutory and broker levy on an NSE trade, per segment, per leg, **point-in-time**.
- The breakeven solve (§5) and the quantity-economics solve (§6).
- Modelled-vs-actual reconciliation state against real broker contract notes (§7).

**Explicitly NOT in scope** — these are their own plan entries and get their own engines:
| Deferred to | What |
|---|---|
| `L1.02` | Pre-trade cost GATE (PASS / RESIZE / VETO in the entry path) |
| `L1.03` | Net-EV gate |
| `L1.04` | Per-segment minimum-edge floor (derived from this engine's output distribution) |
| `L1.05` / `L1.06` | Slippage and market-impact models (the carried, fitted models) |
| `L1.11` | Full P&L attribution by cost component |

`164` bundled the gate and the slippage model into `L1.01`. That bundling is **rejected here** as a
violation of `R.18`: three engines in one slice is how a slice ships thin. The output contract in §4 is
designed so `L1.02` consumes it without modification.

> **Consequence for `R.11`:** the primary consumer of this engine (`L1.02`'s gate) is QUEUED at
> sign-off. This slice therefore closes as `[~]`, not `[x]`, and the open condition is stated in §11 —
> unless the interim consumers in §8 are accepted as satisfying `R.06`.

## 2. Why this is an ENGINE (the `R.23(a)` difference test)

A charge calculator is `turnover × rate`, summed — a labelling layer. Four things make this
decision-grade instead, and each is a real computation, not vocabulary:

1. **Inference over a bitemporal fact store, with refusal.** Rates are not constants in this codebase.
   They are resolved out of the `L0.31` `PointInTimeMarketRuleStore` at `(effective_date, belief_date)`,
   carrying an evidence grade, and the engine **refuses** (`CostCoverageError`) for a date whose rate is
   not known rather than silently substituting today's. This kills the look-ahead-in-costs leak that
   makes every pre-2024 backtest flatter than reality — `research/215` measured the size of it: options
   STT at the Oct-2024 rate overstates 2013–2023 option costs by 2×.
2. **A genuine root-find, not an evaluation.** The breakeven price move is **self-referential** — the
   exit price determines the sell-leg STT, exchange charge and stamp, which determine the breakeven,
   which determines the exit price. With per-order brokerage caps (`min(0.03%, ₹20)`) the cost function
   is piecewise-linear with kinks, and with DP charges it has a step. §5 solves it exactly rather than
   iterating to a tolerance.
3. **An integer program over lots.** Per-ORDER charges (brokerage cap, DP per scrip per day) make cost
   in bps a *decreasing step function* of quantity. "What is the smallest position for which costs stop
   dominating?" is an optimisation, and it is the question that actually decides whether a ₹1 lakh
   account can trade a segment at all (`R.03`: capital is a parameter from ₹1 lakh to ₹1 crore).
4. **Carried state that corrects the model.** Real broker contract notes are ingested and reconciled
   per component; per-component residuals accrue in SQLite and are surfaced. The engine measures its own
   error instead of asserting correctness (§7).

**SOTA analogs (`R.23(a)`):** NautilusTrader's `FeeModel`/`MakerTakerFeeModel` (per-venue fee objects
resolved at fill time) and Zipline's adjustments DB (point-in-time facts queried by date, never
back-filled). This engine is deeper than either on the point-in-time axis, because both assume a fee
schedule constant over the backtest.

## 3. The fact layer — where rates live (`R.03` / `R.23(e)`)

**No rate is a literal in this package.** Every rate is a `MarketRuleRecord` in the `L0.31` store, which
already refuses to accept a record without a `source_reference` (`point_in_time_market_rule_store.py:196`).
Three of the families this engine needs already exist and are seeded; the rest are added by this slice.

Existing: `SECURITIES_TRANSACTION_TAX`, `STAMP_DUTY`, `EXCHANGE_TRANSACTION_CHARGE`.

Added by this slice (extends the closed `RuleFamily` set; `coverage()` reports them automatically):
- `SEBI_TURNOVER_FEE`
- `GOODS_AND_SERVICES_TAX`
- `DEPOSITORY_PARTICIPANT_CHARGE`
- `SECURITIES_TRANSACTION_TAX_BASIS` — **structural, not a rate.** Which quantity STT multiplies
  (premium / turnover / intrinsic value / strike-plus-premium) has itself changed by circular; encoding
  it as a Python constant would hardcode a fact that has a history. `TEXT` value kind.
- `CHARGE_LEG_APPLICABILITY` — which leg a levy attaches to (buy / sell / both). Also historical.

Broker fees are **not** market rules — they are a commercial schedule per broker, so they live in
`broker_fee_schedules.py` as effective-dated records with the same provenance discipline and the same
refusal behaviour, keyed by broker.

### 3.1 Defects in the already-seeded facts that this slice must fix

The sourcing pass (§12) was run against primary circulars and found **three defects in `L0.31`'s seeded
history**. All three are silent — they produce a number, and numbers get used.

1. **The options exchange-transaction charge is a cash rate wearing an options scope.**
   `nse_market_rule_history.py:308-320` seeds `EXCHANGE_TRANSACTION_CHARGE` for `_OPTIONS` at
   `0.0000297`. That is ₹297/crore — the **cash-market** rate for the Oct-2024 era. The options rate for
   the same era is ₹3,503/crore of premium (₹3,553 including IPFT). The seeded value understates the
   options exchange charge by a factor of **~12**, on the segment that trades the most.
2. **The cash stamp-duty record is scoped to all cash but carries the delivery-only rate.**
   `nse_market_rule_history.py:172-182` seeds `STAMP_DUTY` at `0.00015` (0.015%) against
   `RuleScope(segment="NSE")`. 0.015% is the **delivery** rate; intraday is 0.003%. Any intraday query
   resolves to the delivery rate — **5× overstated** — because the scope claims more than the fact
   supports.
3. **Cash-equity STT is absent entirely**, so the cash segment cannot be priced at all: the store
   refuses. Correct behaviour for an unseeded fact, but it means `L1.01` cannot ship without seeding it.

Defects 1 and 2 are corrections to records compiled the same day (`COMPILED_ON = 2026-08-12`), so they
are fixed in place with the reason recorded, not superseded by a later-dated record — there is no belief
history to preserve between this morning and this afternoon.

### 3.2 Segment vocabulary (a scope decision this slice forces)

`RuleScope.segment` is doing two jobs in the existing seeds: `"NSE"`/`"NFO"` are Kite instrument-master
exchange codes (that is what `InstrumentMasterRuleObserver` emits for `TICK_SIZE`/`LOT_SIZE`), while
`"NFO-FUT"` is an instrument *type*. Costs need a third axis the codes do not carry — **product mode**,
because delivery and intraday are taxed differently on the same exchange segment (`L1.15`'s "dual cost
regime" is exactly this).

Resolution: one explicit, self-describing vocabulary, declared once as `ChargeableSegment` in the cost
package and used as the scope string everywhere:

| `ChargeableSegment` | scope string | what it is |
|---|---|---|
| `EQUITY_DELIVERY` | `NSE-CNC` | cash, carried overnight |
| `EQUITY_INTRADAY` | `NSE-MIS` | cash, squared off same day |
| `EQUITY_FUTURES` | `NFO-FUT` | index + stock futures (identical statutory treatment) |
| `EQUITY_OPTIONS` | `NFO-OPT` | index + stock options (identical statutory treatment) |
| `CURRENCY_FUTURES` | `CDS-FUT` | |
| `CURRENCY_OPTIONS` | `CDS-OPT` | |
| `COMMODITY_FUTURES` | `MCX-FUT` | CTT, not STT |
| `COMMODITY_OPTIONS` | `MCX-OPT` | CTT, not STT |

Index-vs-stock is NOT a segment distinction for charges — the statutory rates are identical and
splitting them would invent a difference that does not exist. It reappears as `index_or_underlying`
scope only where it genuinely bites: physical settlement, which applies to stock derivatives and not
index ones. The existing `"NFO"` and `"NSE"` seeds are re-scoped to `NFO-OPT` and `NSE-CNC`
respectively; the three store tests that assert on those strings move with them.

> This mapping is the seam where the six segment holons (`L14.17`) meet the charge engine. Commodity is
> a second venue (`L14.17b`) and is priced here because CTT rates were sourced primary — pricing it is
> free, and refusing to price it later would be the compromise `R.16` forbids.

## 4. Output contract

```python
@dataclass(frozen=True, slots=True)
class ChargeLine:
    component: ChargeComponent      # STT, EXCHANGE_TRANSACTION, SEBI_TURNOVER_FEE, GST, STAMP_DUTY,
                                    # BROKERAGE, DEPOSITORY_PARTICIPANT
    leg: TradeLeg                   # BUY | SELL
    taxable_base_paise: int         # what the rate was applied to, so a caller can audit the line
    rate: Decimal | None            # None for flat per-order charges
    amount_paise: int               # exact, integer paise, rounding rule recorded
    rounding: RoundingRule
    source_reference: str
    evidence_grade: EvidenceGrade

@dataclass(frozen=True, slots=True)
class RoundTripCost:
    lines: tuple[ChargeLine, ...]
    total_paise: int
    total_bps_of_turnover: Decimal
    breakeven_move_paise: int       # §5 — exact, not iterated
    breakeven_move_bps: Decimal
    breakeven_ticks: Decimal        # in real tick sizes from the L0.31 observed TICK_SIZE family
    weakest_evidence_grade: EvidenceGrade   # the cost is only as trustworthy as its worst-sourced line
    as_of: date
    known_as_of: date
```

**Money is integer paise throughout** (repo convention, `research/200`). Rates are `Decimal`, never
float — `0.0625` is not representable in binary and a rate multiplies every trade.

`weakest_evidence_grade` is deliberate: a caller must be able to tell that a 2019 backtest's costs rest
on `SECONDARY_TRIANGULATED` stamp duty while a 2026 live trade rests on a primary circular.

## 5. The breakeven solve (exact, not iterative)

Let `q` = quantity, `p_in` = entry price, `p_out` = the unknown exit price. Round-trip cost is
`C(p_in, p_out)` and breakeven requires `q·(p_out − p_in) = C(p_in, p_out)` for a long.

Every ad-valorem component is linear in `p_out` with a segment- and leg-dependent coefficient `k`
(the sum of sell-leg rates that apply to the exit turnover, GST-grossed where GST applies), and the
per-order and entry-side components are a constant `c`. So:

```
q·p_out − q·p_in = k·q·p_out + c   ⟹   p_out = (q·p_in + c) / (q·(1 − k))
```

— a closed form, solved exactly in `Decimal`. The kinks (brokerage `min(0.03%·turnover, ₹20)`) are
handled by solving on **each piece** and keeping the root that falls inside its own piece's interval,
which is the standard piecewise-linear root method and is exact rather than tolerance-bounded. The
solver asserts that exactly one root is admissible and raises `BreakevenSolveError` when the pieces
disagree — a disagreement means the fee schedule was mis-specified, and silently picking one root is
how that defect would hide.

Short legs invert the sign; options with a sell-first structure invert which leg carries STT. Both are
tested as separate cases, not assumed symmetric.

## 6. Quantity economics (integer program over lots)

Per-order charges do not scale with size, so `cost_bps(q)` is a decreasing step function. The engine
answers two questions used later by sizing (`L1.10`) and the min-edge floor (`L1.04`):

- `minimum_viable_quantity(instrument, price, cost_bps_ceiling)` — the smallest integer quantity (in
  whole lots for derivatives, using the **live** lot size from the `L0.31` `LOT_SIZE` observed family,
  never a hardcoded lot) whose round-trip cost is within the ceiling. Solved by evaluating the closed
  form at each brokerage-piece boundary and taking the ceiling within the winning piece — O(pieces),
  not a scan over quantities.
- `cost_curve(instrument, price, quantities)` — the full step curve, for the dashboard and for
  `L1.04`'s floor derivation.

This is where a ₹1 lakh account learns that a segment is arithmetically closed to it, rather than
finding out through losses.

## 7. Carried state — modelled vs actual reconciliation

`ChargeReconciliationLedger` (SQLite at `~/.nse_algo_trader/transaction_cost.sqlite3`, matching the
repo's store convention):

- Ingests real broker contract notes / order-charge responses per executed order.
- Per `(broker, segment, component)`: accrues `n`, mean signed residual (modelled − actual, in paise),
  residual dispersion, and the worst single residual with the order that produced it.
- Surfaces a **per-component verdict**: `AGREES` / `DRIFTS` / `UNVERIFIED` (n below the maturity
  threshold — which is a percentile of accrued counts, not a hardcoded N, per `R.03`).
- A drifting component is a **defect signal about the rate table**, not a fudge factor: the engine does
  NOT auto-correct its rates from residuals. Auto-correcting would launder a stale statutory rate into
  a fitted parameter and destroy the provenance chain. It reports, loudly, and the fix is a new dated
  `MarketRuleRecord`.

Per `R.04`, thin data gates ACTIVATION of the verdict, never the algorithm: with zero contract notes
the ledger is fully built and reports `UNVERIFIED`, and it arms itself as notes accrue with no code
change.

## 8. Wiring (`R.06` — no orphans)

1. **Daily runner step** in `scripts/run_daily_operations.py` (`_run_step` pattern at `:242`): resolve
   the current schedule for every segment, assert coverage for today, compute the cost curve over the
   real universe's price distribution, and persist the day's breakeven-bps snapshot per segment.
2. **Dashboard surface** `/costs` (`R.08`) — breakeven bps per segment, the cost curve by quantity, the
   evidence grade of every live rate, the reconciliation verdicts, and the dates the engine would
   REFUSE to price. Module added to `SURFACED_MODULES` in `dashboard_server.py`.
3. **Deep-history consumer**: `L0.34`'s store is the input to the historical breakeven series — the
   engine prices the real traded prices of the past, which is what makes the point-in-time claim
   testable rather than asserted.

## 9. Module layout

```
src/nse_algo_trader/transaction_cost/
  nse_charge_rule_history.py        # the added rate families, seeded with dated primary-sourced facts
  broker_fee_schedules.py           # effective-dated per-broker commercial schedules + refusal
  nse_transaction_cost_engine.py    # CORE: resolve -> apply -> ChargeLine[] -> RoundTripCost
  breakeven_move_solver.py          # §5 exact piecewise-linear root solve
  quantity_cost_economics.py        # §6 minimum viable quantity + cost curve
  charge_reconciliation_ledger.py   # §7 carried state vs real contract notes
  transaction_cost_dashboard_surface.py  # /costs
```

Names state their subject (`R.14`). No `cost_utils`, no `helpers`.

## 10. Verification plan (`R.23(c)`)

- **Unit** — every component against known-good rupee figures from a published broker charge
  calculator for concrete trades, exact to the paise.
- **Property** — round-trip ≥ one-way; every line ≥ 0; total monotonic non-decreasing in qty and price;
  `cost_bps` non-increasing in qty; GST base excludes STT and stamp duty; breakeven move > 0 for every
  segment; solving the breakeven and then pricing the solved exit reproduces exactly zero net P&L
  (round-trip identity — the strongest single test in the suite).
- **Adversarial** — qty 0; a ₹1 price; a date one day before and one day after every rate change in the
  history (boundary correctness, both directions); a date with NO coverage (must raise, must not
  substitute); a conflicted rate (two records, same window — must surface, not silently pick); belief-
  time replay (`known_as_of` set to before a correction reproduces the pre-correction cost).
- **Hermetic (`R.10`/DI seam)** — reconciliation ledger fitted against injected real-shaped contract
  notes; never leaks to production paths.
- **Real-data (`R.05`)** — price the REAL universe: every `L0.34` traded row for a sample of real dates
  spanning at least three rate regimes, plus today's live option chain. The pass criterion is not "it
  ran": it is that the historical breakeven series shows the rate-change step changes on the right
  dates and in the right direction, and that the modelled charges for real recent orders match the
  broker's own reported charges within the reconciliation tolerance.
- **Adversarial review in a fresh subagent** before the gate, per `R.23(c)`.

## 11. Open items at sign-off (`R.11` — surfaced, never silent)

*(filled at sign-off)*

## 12. Sourced rate table

Sourcing pass run 2026-08-12 across five parallel streams; primary circulars fetched as raw PDF text
where the tag says PRIMARY. Every row below becomes a `MarketRuleRecord` with this exact provenance.
**A rate is stored as the fraction of its own base** — the base is a separate stored fact (§3), because
applying an options rate to notional instead of premium is a 100× error, not a rounding one.

### 12.1 STT / CTT — `SECURITIES_TRANSACTION_TAX`

| Scope | Value | Base | Leg | From | To | Source | Grade |
|---|---|---|---|---|---|---|---|
| `NSE-CNC` | 0.001 | turnover | both | 2013-06-01 | — | Finance Act 2013 §98 schedule, broker-corroborated | SECONDARY |
| `NSE-MIS` | 0.00025 | turnover | sell | 2013-06-01 | — | same | SECONDARY |
| `NFO-FUT` | 0.0001 | turnover | sell | 2013-06-01 | 2023-04-01 | NSE/FATAX/23500 | SECONDARY |
| `NFO-FUT` | 0.000125 | turnover | sell | 2023-04-01 | 2024-10-01 | Finance Act 2023 | SECONDARY |
| `NFO-FUT` | 0.0002 | turnover | sell | 2024-10-01 | 2026-04-01 | Finance (No.2) Act 2024 memo, Clause 155 (PDF fetched) | **PRIMARY** |
| `NFO-FUT` | 0.0005 | turnover | sell | 2026-04-01 | — | Finance Bill 2026, Clause 143 (PDF fetched) | **PRIMARY** |
| `NFO-OPT` | 0.0005 | premium | sell | 2013-06-01 | 2023-04-01 | NSE/FATAX/23500 | SECONDARY |
| `NFO-OPT` | 0.000625 | premium | sell | 2023-04-01 | 2024-10-01 | Finance Act 2023 | SECONDARY |
| `NFO-OPT` | 0.001 | premium | sell | 2024-10-01 | 2026-04-01 | Finance (No.2) Act 2024 memo, Clause 155 | **PRIMARY** |
| `NFO-OPT` | 0.0015 | premium | sell | 2026-04-01 | — | Finance Bill 2026, Clause 143 | **PRIMARY** |
| `NFO-OPT` exercised | 0.00125 | **intrinsic value** | buyer | 2019-09-01 | 2026-04-01 | Finance (No.2) Act 2019 §99(a)(ii) | SECONDARY |
| `NFO-OPT` exercised | 0.0015 | **intrinsic value** | buyer | 2026-04-01 | — | Finance Bill 2026, Clause 143(ii) | **PRIMARY** |
| `MCX-FUT` (CTT) | 0.0001 | traded price | seller | 2013-07-01 | — | NCDEX compliance guide reproducing Finance Act 2013 §117 | **PRIMARY** |
| `MCX-OPT` (CTT) | 0.0005 | premium | seller | 2018-04-01 | — | same (rate PRIMARY, date SECONDARY) | **PRIMARY** |
| `MCX-OPT` exercised, cash-settled | 0.00125 | intrinsic value | buyer | 2013-07-01 | — | same | **PRIMARY** |
| `MCX-OPT` exercised, physical | 0.000001 | settlement price | buyer | 2013-07-01 | — | same | **PRIMARY** |
| Agricultural commodities | 0 | — | — | 2013-07-01 | — | same (named exemption list) | **PRIMARY** |

**Two corrections to the corpus's own belief, both material:**
- `b28` claimed the settlement-price → intrinsic-value base change for exercised options happened in
  2024. It happened **2019-09-01**, five years earlier. `research/164`'s history table (0.125% on
  intrinsic at the Oct-2024 point) was right and `b28` was wrong; the two are now reconciled.
- Exercised-option STT is **0.125%, not 0.15%**, until 2026-04-01 — it moved to 0.15% only with the
  April-2026 change. `b28`'s worked example treating exercise and sell STT as identical is correct
  *today* and wrong for every date before 2026-04-01.

The April-2026 0.15% is **enacted law, not a proposal** — Finance Bill 2026 (Bill No. 3 of 2026),
Clause 143, amending §98 of the Finance (No.2) Act 2004, effective 2026-04-01, raising futures
0.02%→0.05%, options premium 0.1%→0.15% and exercised options 0.125%→0.15% together.

### 12.2 NSE exchange transaction charge + IPFT — two families, because the circular has two columns

NSE/FA/73061 (2026-02-27, eff. 2026-03-01, PDF fetched verbatim) states for cash: *"Transaction Charges
Rs. 306.99 each side, Contribution to NSE IPFT Rs. 0.01 each side, Total Rs. 307 each side"*. The
March-2026 change is **not a fee rise** — it is the IPFT rollback with the freed amount moved into the
base charge, "ensuring no change in the overall outflow". Modelling only the total would lose that, and
modelling only the transaction charge would understate the cost. Both are stored.

| Scope | Transaction charge | IPFT | Total | From | To | Source | Grade |
|---|---|---|---|---|---|---|---|
| `NSE-CNC`/`NSE-MIS` | 0.0000297 | 0.000001 | ₹307/cr | 2024-10-01 | 2026-03-01 | NSE/FA/64232 (PDF fetched) | **PRIMARY** |
| `NSE-CNC`/`NSE-MIS` | 0.000030699 | 0.000000001 | ₹307/cr | 2026-03-01 | — | NSE/FA/73061 (PDF fetched) | **PRIMARY** |
| `NFO-FUT` | 0.0000173 | 0.000001 | ₹183/cr | 2024-10-01 | 2026-03-01 | NSE/FA/64232 | **PRIMARY** |
| `NFO-FUT` | 0.0000183 | 0.000000001 | ₹183/cr | 2026-03-01 | — | NSE/FA/73061 | **PRIMARY** |
| `NFO-OPT` | 0.00035030 | 0.000005 | ₹3,553/cr premium | 2024-10-01 | 2026-03-01 | NSE/FA/64232 | **PRIMARY** |
| `NFO-OPT` | 0.00035530 | 0.000000001 | ₹3,553/cr premium | 2026-03-01 | — | NSE/FA/73061 | **PRIMARY** |
| `CDS-FUT` | 0.0000035 | — | ₹35/cr notional | 2024-10-01 | — | broker tariff pages | SECONDARY |
| `CDS-OPT` | 0.000311 | — | ₹3,110/cr premium | 2024-10-01 | — | broker tariff pages | SECONDARY |

**Nothing before 2024-10-01 is seeded, deliberately.** The pre-`True to Label` schedule was a turnover
SLAB (cash ₹2.97–₹3.22/lakh, options ₹29.50–₹49.50/lakh), the tier breakpoints are not published
anywhere in aggregate, and a flat rate applied to a slab era is wrong by construction. The engine
REFUSES those dates. The driver of the flattening is SEBI/HO/MRD/TPD-1/P/CIR/2024/92 (2024-07-01),
which also forbids brokers marking the charge up over what they pay the exchange.

### 12.3 SEBI turnover fee — `SEBI_TURNOVER_FEE`

₹10/crore (0.000001) of turnover for equity, equity derivatives, currency and commodity; ₹2.5/crore for
debt. Rate origin: SEBI (Stock Brokers) (Third Amendment) Regulations 2006, S.O. 1600(E).

**The base for options is NOTIONAL, not premium — and this contradicts `research/164` and `b28`, which
both say premium.** NSE has charged it on notional (strike × lot size) since 2018-19; SEBI forced BSE
onto the same basis by private letter, disclosed by BSE on 2024-04-26 with a ~₹165 crore back-payment
including 15%/yr interest. Corroborated by BSE's own exchange disclosure plus five independent outlets;
**no public circular exists**, so this is stored as `SECONDARY_TRIANGULATED` with the disagreement
recorded in the source string. It matters: on a NIFTY option with a ₹150 premium and a 24,000 strike,
notional is ~160× premium, so the fee is 160× larger than `164` assumed. It is still small in absolute
terms — that is not a reason to model it wrongly.

`research/164`'s claim of a 2023 SEBI turnover-fee reduction **could not be verified across six search
angles** and appears to be a conflation with NSE's own 2023 transaction-charge/IPFT rejig
(NSE/FA/56129). Not seeded; recorded as unverified.

### 12.4 GST — `GOODS_AND_SERVICES_TAX`

18%, applied to **brokerage + exchange transaction charge + IPFT + SEBI turnover fee + DP charges**.
NOT applied to STT/CTT or stamp duty. The exclusion is statutory rather than administrative: CGST Act
§2(52) and §2(102) exclude "securities" from both goods and services, so no dedicated CBIC circular
names STT — the engine records that the exemption is *derived*. The reason the pass-through charges DO
attract GST is that brokers do not structure them as CGST Rule 33 "pure agent" reimbursements, so they
fall into value-of-supply under §15. The September-2025 GST 2.0 slab restructuring left financial
services at 18%.

### 12.5 Stamp duty — `STAMP_DUTY`, buy side only, from 2020-07-01

Buy-side-only is statutory: Indian Stamp Act §9A(1)(a) — *"shall be collected … from its buyer on the
market value of such securities at the time of settlement"*. Effective date confirmed PRIMARY from
SEBI's own FAQ PDF.

| Scope | Value | Base |
|---|---|---|
| `NSE-CNC` | 0.00015 | turnover |
| `NSE-MIS` | 0.00003 | turnover |
| `NFO-FUT` / `MCX-FUT` | 0.00002 | notional |
| `NFO-OPT` / `MCX-OPT` | 0.00003 | premium |
| `CDS-FUT` / `CDS-OPT` | 0.000001 | notional / premium — **one shared rate**, not two coincidentally equal ones |
| Government securities | 0 | — |

### 12.6 Depository participant charge — `DEPOSITORY_PARTICIPANT_CHARGE`

**Flat per debit transaction, independent of quantity** — selling 100 shares costs the same as selling
10,000. Sell side only; buy side is nil.

| Depository | Value | From | Source | Grade |
|---|---|---|---|---|
| CDSL | ₹3.50 | 2024-10-01 | CDSL media release 2024-09-26, verbatim | **PRIMARY** |
| CDSL, female first holder / MF / bond ISINs | ₹3.25 | 2024-10-01 | same | **PRIMARY** |
| NSDL | ₹4.00 | (already flat before 2024) | NSDL fee schedule 2026-07 | **PRIMARY** |

The familiar ₹15.34 is **not a depository rate** — it is Zerodha's bundled client-facing total
(₹3.50 CDSL + ₹9.50 broker markup + 18% GST on the ₹13). `research/164` recorded ₹15.34 as if it were
the charge; it is stored here correctly as depository fact plus broker schedule, because the broker half
changes when the broker changes and the depository half does not.

### 12.7 Broker schedules — `broker_fee_schedules.py`, not the rule store

| | Zerodha | Upstox | Angel One |
|---|---|---|---|
| Delivery | ₹0 | ₹20/order | lower of ₹20 or 0.1%, **min ₹5** |
| Intraday | lower of ₹20 or 0.03% | lower of ₹20 or 0.1% | lower of ₹20 or 0.1%, min ₹5 |
| Futures | lower of ₹20 or 0.03% | lower of ₹20 or 0.05% | ₹20 flat |
| Options | ₹20 flat | ₹20 flat | ₹20 flat |
| DP markup | ₹9.50 | — | — |
| Auto square-off | ₹50 + GST | ₹75 + GST | none |
| Call & trade | ₹50 | ₹75 + GST | ₹20 |
| Physical settlement | 0.25% of contract value (0.1% if netted off) | same | ₹50/certificate demat |

All PRIMARY from the brokers' own published charge pages. **Do not average these into a "market
standard"** — the percentage legs differ by 3× and only Angel One has a floor, so the quantity at which
the flat ₹20 starts binding is broker-specific and is an input to §6. Zerodha is the default because
Kite is the execution path (`A.25`); no 2025/2026 Zerodha pricing change was found.

### 12.8 Rounding — UNRESOLVED, and treated as unresolved

No SEBI or NSE circular mandating a rounding convention for statutory levies could be read; two
candidate circulars (NSE/INSP61999, NSE/FATAX63809) timed out on repeated fetches. The one primary
clause recovered — NSE/INSP/2006/44 (2006-03-30) — says levies "may be recovered from clients only at
actuals paid or payable", which is a **no-markup** rule, not a precision rule. The only concrete
convention found is Zerodha's published practice of rounding STT to the nearest rupee (≥50 paise up),
which is broker behaviour, not law.

**Design consequence:** the engine computes every statutory line **exact in `Decimal`, to the paise, and
rounds nothing**. Rounding is modelled as a per-broker `RoundingRule` on the broker schedule, because
that is where the evidence actually points. The reconciliation ledger (§7) then *measures* each broker's
real rounding from contract notes instead of the engine asserting one. Assuming a rounding rule here
would be inventing a regulatory fact — the exact failure `R.23(e)` describes.

This is an **open blocker**, logged in `docs/BACKLOG.md`, not a silent skip (`R.11`).

### 12.9 Everything the sourcing pass could NOT verify (`R.11`)

1. Equity cash STT base rates (0.1% / 0.025%) — SECONDARY only; the Finance Bill 2023 PDF returned 403.
2. Pre-Oct-2024 exchange-charge slab breakpoints — do not exist in aggregate anywhere.
3. The SEBI options-fee notional basis — no public circular; corroborated only by BSE's disclosure.
4. GST exclusion of STT/stamp — statute-derived, no administrative confirmation.
5. Rounding — §12.8.
6. Commodity and SLB segment IPFT — no separate line item found in any circular.
7. The Sept-2019 intrinsic-value base change — cited by three sources, primary text not pulled.

## 13. Sourcing record (`R.17` — mechanical evidence, run 2026-08-12)

A throwaway venv was built on this host (`python3.12 -m venv`, aarch64/glibc-2.34) and candidates were
installed and RUN. `research/164`'s 2026-08-03 triage was **re-run rather than inherited**, because it
predates the reset and its central finding ("this is SALVAGE") is void.

| Candidate | Installs on this host? | Maintained | Verdict | Mechanical evidence |
|---|---|---|---|---|
| `zerodha-brokerage-calculator` 0.2.0 | yes | PyPI 2024-10-26; the 2026 GitHub push is a dependabot npm bump in `frontend/`, not the Python code | **REJECT** | Source dumped and grepped. `calculator.py:117` futures STT `0.0001` — half the current 0.0002. `calculator.py:168` options STT `0.0005` — half the current 0.001. `calculator.py:172` options exchange charge `0.00053` — 49% high vs 0.0003553. `calculator.py:13` cash exchange charge `0.0000345` — 12% high vs 0.0000307. Pure `float`, no Decimal, no effective-date parameter of any kind |
| `nautilus_trader` 1.231.0 | **NO** | active (2026-08-02) | **REJECT** | No compatible wheel — the aarch64 wheel is `manylinux_2_35`, host glibc is 2.34; cargo source build fails `error: failed to load manifest for workspace member .../examples/tutorials`, exit 101. Same mechanical blocker `L0.21` recorded |
| `backtrader` `CommInfoBase` | already installed | last push 2024-08-19 | REJECT | `getcommission()` is one flat rate; no component decomposition, no dating |
| `vectorbt` fees | already installed | active | REJECT | `fees`/`fixed_fees`/`slippage` are flat scalars |
| `zipline-reloaded` `commission` | already installed | 2026-01-06 | REJECT | `PerShare(cost, min_trade_cost)` — single linear rate |
| `qlib` `Exchange` | not installed (heavy torch stack) | very active | REJECT | `open_cost=0.0015, close_cost=0.0025, min_cost=5.0` — flat China-A-share rates, no dating API |
| `ccxt` | n/a | very active | REJECT | `exchanges.json` enumerates 103 venue ids, **zero** Indian equity/derivative venues |
| `bt`, `pyfolio` | n/a | active / stale (2023-12-23) | REJECT | No fee-schedule primitives at all |
| `intervaltree` 3.2.1 | already installed | 2025-12-24 | **REJECT, but it worked** | Actually ran: an interval tree keyed on `date.toordinal()` correctly returned the 0.15% tier for 2026-08-12 and the 0.05% tier for 2024-05-01. Rejected because `L0.31`'s store already does point-in-time resolution with provenance and refusal — adding a second, provenance-free lookup path would be the duplication `R.06` exists to prevent |
| `staircase` 2.8.0 | already installed | 2026-06-07 | REJECT | Ran it: `layer(0,100,20); layer(100,None,0.0003); sample(150)` returned `0.0002999999999993008` — **float**, not exact. Disqualifying for paise-exact money |
| `scd2` 1.0.0 | already installed | **stale**, last release 2023-01-31 | REJECT | Only API is `SCD2.pandas_scd2(src, tgt, cols_to_track, tz)` — source→target ETL change-tracking, not point-in-time lookup |
| `piso` 1.3.0 | already installed | 2026-06-08 | REJECT | Interval set algebra over `pd.IntervalIndex`; pandas already provides the lookup |
| `py-moneyed` 3.0 | already installed | **stale**, 2022-11-27 | REJECT | stdlib `Decimal.quantize()` already gives exact paise |
| `pwlf`, `piecewise-regression` | PyPI only | active | REJECT | Curve **fitting** — they recover breakpoints from noisy data. §5 is a closed-form solve of a known piecewise function; wrong problem |
| `mpmath`, `decimalfp` | PyPI only | active | REJECT | Arbitrary-precision float / fixed-point; redundant with stdlib `Decimal` |
| GitHub/npm Indian charge calculators (`tahseenjamal/zerodha_brokerage_calculator`, `sarthak070707/trade-charges-calculator`, `ARVIKODE000/brokerage-calculator`, +) | n/a | 0–4 stars, several are React UIs | REJECT | None published as packages; none Decimal-exact; none effective-dated |

**Conclusion: bespoke-in-repo, no vendor, no new dependency.** Not one candidate — Indian-specific or
general-framework — offers point-in-time-dated, Decimal-exact, per-component charge computation, and
every Indian calculator inspected carried at least one statutory constant that is now materially stale.
That is the precise failure mode this engine exists to prevent, so importing one would import the bug.
