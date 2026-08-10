# 201 — Universe observation strategy, per segment

**Operator decision, 2026-08-10:** cash-intraday keeps **filter-based focus** (too many names to watch
meaningfully); the **watch-everything, wait-for-the-condition** approach is built in the five derivative
segments. Stated build order — index options → stock options → futures → commodities — is a **tie-break
reference only**; otherwise the five carry **equal weight** (R.10).

All figures below are **measured** from the retained bhavcopy (1,436,568 rows, 2026-06-08 → 2026-08-03)
and the live instrument dump (113,955 contracts), not asserted.

---

## 1. The finding that reshapes the design

The operator asked where liquidity and volume actually concentrate. Measured on the most recent session:

| Contract type | Contracts | Volume | Share |
|---|---|---|---|
| **Index options (IDO)** | 5,042 | 118,908,660 | **93.81%** |
| Stock options (STO) | 25,824 | 6,692,286 | 5.28% |
| Stock futures (STF) | 622 | 1,066,133 | 0.84% |
| Index futures (IDF) | 15 | 82,600 | **0.07%** |

Within index options, by underlying: **NIFTY 98.54%** · BANKNIFTY 1.33% · MIDCPNIFTY 0.11% ·
FINNIFTY 0.02% · NIFTYNXT50 0.00% (48 contracts traded all day).

By expiry: the **nearest weekly holds 92.8%** of index-option volume (110.4M of 118.9M).

**Compounding those: NIFTY nearest-weekly options are ≈87% of all NSE F&O volume.**

### The consequence that matters

| Top N contracts | Share of NIFTY weekly volume |
|---|---|
| 5 | 36.03% |
| 10 | 57.38% |
| 20 | 76.28% |
| 40 | 92.27% |
| **80** | **99.28%** |

**Eighty contracts carry 99.28% of the volume in the segment that is 87% of the market.** The 9,000
instrument streaming ceiling was never the binding constraint for index options — the liquid surface is
three orders of magnitude smaller than the contract count implies.

### The moneyness result, which is not what folklore says

| Distance from spot | Volume share | Open interest |
|---|---|---|
| ATM ±0.5% | 15.57% | 34,890,765 |
| **0.5–1%** | **41.27%** | 69,437,810 |
| 1–2% | 23.58% | 72,640,360 |
| 2–5% | 14.60% | **107,180,450** |
| >5% | 4.98% | 54,965,885 |

**The volume peak is 0.5–1% out of the money, not at the money** — 2.6× the ATM band. Centring a watch
window on ATM would miss the busiest strikes. Note also that **volume and open interest have different
distributions**: volume clusters near spot, OI is deepest 2–5% out. Positioning sits further from the
money than trading does, which matters for any max-pain or gamma-positioning instruction.

---

## 2. Per-segment observation strategy (the operator asked for my opinion; this is it, with the evidence)

| Segment | Contracts | Strategy | Rationale |
|---|---|---|---|
| **Index options** | 5,042 | **Watch every strike, nearest 2 expiries, all 5 underlyings.** ~2,000 instruments. | Literally watch everything. It fits trivially. 80 contracts hold 99.28%, so the rest is nearly free to carry. |
| **Index futures** | 15 | **Watch all 15.** | Fifteen contracts. There is no decision to make. |
| **Stock futures** | 622 | **Watch all 622.** | Small enough to stream whole. |
| **Stock options** | 25,824 | **Tiered.** Stream top ~30 underlyings near-ATM; REST-snapshot the rest; bhavcopy the deep tail. | The only segment where tiering is genuinely required — and the only one with a *long tail*: top 30 of 208 underlyings is just 56.53% of volume, versus NIFTY's 98.54% dominance in index options. Nothing is excluded, only observed at lower resolution. |
| **Commodities (MCX)** | 16,158 | **Watch all near-month contracts**, tail snapshotted. | Liquidity concentrates in a handful of commodities' front month. |

**Total streamed: ≈2,000 + 15 + 622 + ~3,000 + ~500 ≈ 6,100 instruments — inside a single Kite key's
9,000 ceiling.** The watch-everything ambition is affordable *today*, without multi-key sharding, for all
five derivative segments simultaneously. That was not obvious before measuring.

**Watch window by moneyness:** ATM ±2% captures 80.42% of volume; ±5% captures 95.02%. Since the whole
chain fits anyway, the window is a *resolution* choice, not an inclusion one — full chain at bar
resolution, ±2% at tick resolution.

---

## 3. Cash intraday — filter-based focus, with the combination learned

**All four filter families are in** (operator, 2026-08-10):

1. **Activity** — turnover, relative volume, volume surge, delivery %
2. **Volatility & range** — ATR%, realised vol, intraday range, overnight gap %
3. **Extremes & events** — upper/lower circuit, proximity to band, news flag, results due, bulk/block deals
4. **Relative strength & structure** — gainers/losers, sector rank, index membership, spread/depth/ADV

**The operator's instruction is the interesting part:** run them *individually and in combination*, trade
each, measure which produces the most profit, and make the winner the default. That is the proving-ground
pattern (L11.115) applied to filters rather than instructions: **each filter combination is a hypothesis
with its own track record, graduated on the same statistical bar** — sample N, net of cost, Deflated
Sharpe on effective trials, BY-FDR, regime coverage, holdout.

⚠️ **The multiple-testing cost, stated plainly:** four families yield 15 non-empty combinations, and each
is a separate trial. They are heavily correlated (a top-volume set and a top-turnover set overlap
enormously), so the **effective-trials estimator (L2.08) must be applied** or the winner will be whichever
combination got luckiest. This is the same trap as the 7,846-rule study: the best of N is not evidence.

**Focus-set sizing — operator delegated the choice. Decision: derived from the cost floor, bounded by
executable capacity.** A name enters the focus set when its expected move, given today's volatility and
live spread, clears `cost × 1.5` (A.12) — so the set is 40 names on a quiet day and 300 on a volatile one,
with **no fixed N anywhere** (R.03). It is then capped by what the configured capital and Kite's
10/sec · 400/min order budget can actually act on, because watching more than you can trade is waste.
Rejected: fixed top-N (the hardcoded constant R.03 forbids) and per-filter quotas (arbitrary slot counts,
same problem one level down).

---

## 4. What this supersedes

- **`L5.21`** (narrow 2,000+ → 150–500 by liquidity and turnover) — superseded for the derivative
  segments, where nothing is pre-excluded. **Retained for cash only**, and reshaped: the focus set is
  cost-floor-derived rather than a fixed count.
- **`L5.20`** (exclude ASM/GSM, T2T, circuit-banded) — **survives everywhere**. It is a *legality* filter,
  not a selection filter: those names cannot be traded intraday at all, so excluding them removes nothing
  that was ever available.
- **`L5.22`** (full-universe opportunity radar) — confirmed and made concrete per segment, with measured
  scope rather than an aspiration.

## 5. Open blockers (R.11)

- Volume figures are one session (2026-08-03). The concentration is structural and unlikely to move much,
  but **the observation strategy should recompute its tiers from a trailing window rather than a single
  day** — otherwise a quiet day in BANKNIFTY silently demotes it.
- Index futures at 0.07% of F&O volume is low enough to question the priority of the futures-based
  instructions (IF-01 margin-efficient momentum, IF-04 basis convergence). They remain catalogued; their
  liquidity assumption needs checking before they are armed.
- MCX liquidity distribution has **not** been measured — no MCX data is in the retained bhavcopy. Its
  tiering is inferred from structure, not measured, and must be verified against real MCX data before
  that holon arms.

---

## 6. Sourcing record (R.16 / R.17 — mechanical evidence, run 2026-08-10)

| Candidate | Verdict | Mechanical evidence |
|---|---|---|
| **`streaming-indicators` 0.1.8** | **ADOPT** for L4.27 | Installed on ARM64/py3.12. `RSI(period=14).update(v)` returns `None` during warmup then `69.23` — genuine O(1) incremental state, not a recompute. Ships `ATR · EMA · BBands · CPR · DI · PLUS_DI · MINUS_DI · HeikinAshi · Max · Min · HalfTrend`. Exactly the primitive a per-instrument streaming state needs; hand-rolling incremental indicators is avoidable work. |
| **`mabwiser` 2.7.4** | **ADOPT** for the filter-combination selector | Installed. `MAB(arms=[...], LearningPolicy.ThompsonSampling())` fits and predicts; **`partial_fit` present**, so combinations update online from realised outcomes rather than needing a batch refit. Fidelity-maintained, and the non-stationary variants match the "forget stale edge" requirement in L8.04. |
| `polars` / `duckdb` | already present | Cross-sectional screens over the focus set; installed and verified in the 0.3 stack check. |
| `river` | already present | Drift detection on filter-combination performance. |

**Built rather than borrowed:** the per-segment tiering policy, the cost-floor-derived focus sizing, and
the filter-combination proving ground. No library expresses "watch everything, tier by liquidity, size the
focus set from the cost floor" — that is project policy, not a generic primitive.
