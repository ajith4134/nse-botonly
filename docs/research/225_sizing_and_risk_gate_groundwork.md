# 225 · F03 groundwork — sizing, the risk gate, and what already exists

Gathered 2026-08-13 while `F02`'s adversarial review ran. Read-only; nothing here is a design yet.
Feature **F03**: *"Nothing sizes itself; capital, risk and lot structure decide size, and a gate can
refuse"* — 14 entries, next on the critical path to `F04`'s paper loop.

---

## 1. The fourteen entries

The count is not a guess: `docs/ajith_final_plan.md:103` says `F03` closes the `0.8` `R.11`
deferral, and `docs/ajith_final_todo.md:71` names that deferral's two first callers as `L1.10` and
`L7.01` — the head of exactly this cluster.

| Entry | What it is | plan | todo |
|---|---|---|---|
| `L1.09` | discrete option-lot sizing — integer lots, lot size pulled live daily, never typed | 244 | 440 (`1.43`) |
| `L1.10` | capital-based position sizing — min/max per trade as a function of configured capital | 246 | 441 (`1.44`) |
| `L3.05` | the pre-trade risk gate — notional, leverage, rate, price collar, max daily loss, drawdown kill | 347 | 503 (`2.37`) |
| `L7.01` | risk-based position sizer | 709 | 540 (`2.61`) |
| `L7.02` | max position / order / rate / price-collar / max-leverage limits | 710 | 541 (`2.62`) |
| `L7.03` | max daily loss + drawdown kill | 712 | 542 (`2.63`) |
| `L7.04` | graduated drawdown ladder | 713 | 543 (`2.64`) |
| `L7.05` | per-symbol and aggregate exposure, correlation-aware | 714 | 544 (`2.65`) |
| `L7.06` | MWPL / F&O-ban / position-limit guard, with hysteresis | 715 | 545 (`2.66`) |
| `L7.07` | circuit-limit-aware order rejection | 716 | 546 (`2.67`) |
| `L7.08` | liquidation / margin-shortfall monitor | 718 | 547 (`2.68`) |
| `L7.09` | correlation-breakdown breaker | 719 | 548 (`2.69`) |
| `L7.11` | CVaR / tail risk with stress replays | 722 | 550 (`2.71`) |
| `L7.12` | real-time portfolio VaR including Greeks | 724 | 551 (`2.72`) |

**Deliberately out of scope, with the reason** — `L7.10` is built inside `F02`; `L7.13`/`L7.14` were
dispositioned inside `F01`; `L7.15`–`L7.26` are option-book (`F19`/`F20`), constitution (`F28`) or
overnight-carry (`F18`), and four of them are recorded duplicates of `L12` entries; `L7.18` depends
on `L2.28`'s replay store; **all of `L8.01`–`L8.17` are `F24`**, whose own title says it — *jointly
sized, not one at a time*. `L8.03`'s text ("Kelly sizes multiple simultaneous bets jointly, **not
one at a time**") is the line that settles the boundary. `L11.107` and `L11.18` are already built in
`F01` and belong to `F13` respectively.

## 2. What exists and must be reused rather than rebuilt

**`capital_configuration.py`** — `TradingCapital` (frozen, `:78`) with `.of_rupees()`,
`.rupees_for_fraction()` (rounds down to paise under a high-precision context) and its inverse
`.fraction_of_capital()`; envelope ₹1 lakh–₹1 crore at `:37`; `load_trading_capital_from_environment`
at `:154`, no default. **Nothing calls it** — `grep` over `src/` and `tests/` finds only the module
and its own test. `F03` is where the `0.8` deferral finally closes and this gets its first caller.

**`cost_gate/pre_trade_cost_gate.py`** — the RESIZE machinery not to reinvent. `GateVerdict` is
`PASS/RESIZE/VETO/UNPRICEABLE` (`:67`); `GateDecision` carries `approved_quantity` (`:113`);
`_largest_viable_quantity` (`:343`) bisects over `_scan_quantities` (`:398`) using the same cost and
fill engines that produced the verdict. `F03`'s sizing must compose with this, not shadow it.

**`cost_gate/per_segment_edge_floor.py`** — `SegmentEdgeFloor.clears(edge_bps)` (`:73`), and a store
with `floor_for(...)` (`:190`). Re-derived nightly; today's real run: `NSE-MIS` 7.3 bps, `NSE-CNC`
23.8 bps over 398 instruments.

**Lot size, point-in-time** — `PointInTimeMarketRuleStore.resolve(RuleFamily.LOT_SIZE, as_of,
scope=RuleScope(symbol=...)).as_integer()` (`:394`, `:278`). Populated by
`InstrumentMasterRuleObserver` (`:76`) as a change-detector over 227,535 dated instrument-master
rows, and it **refuses dates it never observed** rather than extrapolating. Coverage begins
2026-08-11, which is a real constraint on any backtest that needs lot sizes before then.

**Freeze quantity — nothing exists.** `market_rules` covers only tick and lot size. The only model
is `F02`'s simulator rejection shape (`simulated_order_execution_venue.py:110`), which reproduces
Kite's literal `"RMS:Rule: Check freeze quantity for NSE CASH"` against an **injected** mapping. The
values themselves have to be sourced from the NSE bulletins (`R.16`: acquire, do not scope down).

**Margin — no calculator exists.** `A.96` resolved `B.06` as a *data-source* finding: NSE's SPAN
risk-parameter archive is reachable 2008→today without auth, and an offline computation reproduced
Kite's `basket_order_margins` within ±8%. But `L6.30` is unbuilt, and the plan scopes it to
`F19`/`F20`. **`F03` must therefore state plainly which of its checks need margin and which do not:**
notional, leverage, price collar, daily loss and drawdown do not; `L7.08`'s margin-shortfall monitor
does, and would otherwise be a labelling layer.

## 3. How mature systems actually size, read from source

**QuantConnect LEAN** — `Common/Securities/BuyingPowerModel.cs`. There is no `IPositionSizingModel`;
sizing and margin live together in `IBuyingPowerModel`.
`GetMaximumOrderQuantityForTargetBuyingPower` (`:366`) converges in a `do…while`, re-pricing fees
each iteration. The discrete-lot handling is `GetAmountToOrder` (`:480`): read
`SymbolProperties.LotSize`, `DiscretelyRoundBy(lotSize, roundingMode)` (`:500`) with the rounding
direction chosen so the position never overshoots the margin target, then step by whole lots and
re-check (`:514`) with a divergence guard. A rounded size of zero returns quantity 0 with the reason
`OrderQuantityLessThanLotSize` (`:425`) — **a named refusal, not a silent no-op.**

**NautilusTrader** — `crates/risk/src/engine/mod.rs`. `RiskEngine` (`:95`) holds
`max_notional_per_order` and nothing else per-instrument. `check_order` (`:923`) denies and the
order never reaches the venue; `check_quantity` (`:1957`) enforces precision, `min_quantity` and
`max_quantity` as instrument metadata; `check_orders_risk_for_account` (`:1008`) computes initial
margin through the account model and denies both per-order (`:1550`) and **cumulatively across the
batch** (`:1562`). It is a pure veto gate — it never resizes.

**vectorbt** (already in this venv) — `Order.size_granularity` (`enums.py:1512`) is a flat rounding
increment with no margin model beyond a global leverage float. The lower bound of what "not costing
basis points" looks like.

**The comparison that matters for `F03`:** LEAN *solves* for a lot-respecting quantity under a
margin target; NautilusTrader *validates and denies* a quantity someone else formed; this project's
own `pre_trade_cost_gate` already does the LEAN-shaped active resize — but for **cost**, not for lot
size or margin. That gap is precisely what `L1.09` and `L7.02`/`L7.08` have to fill, and the
existing bisection is the shape to extend rather than a second architecture to introduce.

## 4. Open questions for the F03 interview, not decided here

- Does sizing target **risk** (a stop-distance fraction of capital), **volatility** (a target
  annualised contribution) or **margin** (LEAN's posture)? The three disagree most on exactly the
  instruments this system trades most.
- `L7.08` needs margin and margin needs `L6.30`, which the plan scopes to `F19`. Build the SPAN
  calculator early inside `F03`, or ship `L7.08` blocked with a named consumer?
- Freeze quantities must be acquired from NSE bulletins. In `F03`, or as part of `F40`'s data pass?
