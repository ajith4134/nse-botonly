# 254 · The 3,481 retained closed trades — measured 2026-08-17

The plan names "3,481 closed trades" three times as a retained asset of the reset (`A.26`, `L11.48`).
I had never opened them. Found in `~/.nse_algo_trader/experience_memory.sqlite3`, table
`experience_nodes`, **3,481 rows** — exactly the stated count.

Every number below is a query against that table.

## What they are

27 columns per node: strategy tag, mechanism, regime context, instrument and kind, direction,
**predicted** outcome and win probability, **actual** outcome, whether the prediction was correct,
Brier contribution, realised P&L and return fraction, predicted vs actual exit cause, kill criteria,
MFE/MAE, total fees, and data provenance. This is a fully-formed track record, not a trade blotter.

| | |
|---|---|
| Sessions | 2026-07-22 → 2026-08-05 |
| Provenance | **2,620 live**, 861 replay-faithful |
| Instruments | 3,049 cash equity · 399 stock option · 33 index option |

| Strategy | n | prediction accuracy | realised P&L |
|---|---|---|---|
| `opening_range_breakout_v1` | 3,049 | 0.544 | **−₹356,631** |
| `directional_option_orb_v1` | 311 | 0.514 | +₹4,104 |
| `credit_spread_v1` | 121 | 0.463 | +₹21,213 |
| **Total** | **3,481** | 0.539 | **−₹331,314** |

## The shape of the loss, which is the whole finding

```
loss  n=2,197   avg -₹365.01   total -₹801,918.76
win   n=1,284   avg +₹366.51   total +₹470,605.14
```

**The average win and the average loss are the same size to within ₹1.50.** The system was not
losing because its losers were large. It was losing because it had **1,284 winners against 2,197
losers — a 36.9% win rate with a symmetric payoff**, which is arithmetically a guaranteed loss.

Note the 0.539 "prediction accuracy" column above is `prediction_was_correct` — whether the system
correctly predicted *win or loss*, not whether it won. Reading that 54% as a win rate would be the
flattering misread; the actual win rate is **36.9%**.

## Costs were more than half the loss

```
total_fees recorded: ₹176,588.88 across 3,118 of 3,481 trades
```

Net −₹331,314 with fees of ₹176,589 implies a **gross of roughly −₹154,725**. So the strategy was
mildly negative *before* costs and **costs more than doubled the loss**. That is `D.01` and the whole
cost-gate layer (`L1.01`, `per_segment_edge_floor`) restated as an outcome rather than an argument:
a strategy that is marginally negative pre-cost is decisively negative post-cost, and no amount of
execution quality rescues it.

## It was overconfident, and by a measurable amount

```
mean predicted win probability : 0.4585
actual win rate                : 0.3689
mean Brier contribution        : 0.2855
```

Predictions ran **~9 percentage points optimistic**. A Brier score of 0.2855 is *worse than 0.25*,
which is what a model that says "50%" to everything scores — so the probability estimates carried
negative information as calibrated quantities, whatever the ranking signal was worth.

## What these trades may and may not be used for

**They may NOT count toward any segment bot's `closed_trades_observed`.** They belong to
`opening_range_breakout_v1` and two siblings — none of which is a segment bot under `L5.25`. Counting
another strategy's record as a bot's own is exactly the flattery `R.22`'s two-key rule exists to
prevent, and it would let a bot graduate on evidence it never earned. The ladder in
`BotMaturity` must climb on the bot's own trades or it means nothing.

**They are legitimately valuable for four things**, none of which requires attributing them to a bot:

1. **The per-segment null** (`A.130` §3) — each bot is judged against its own cost-adjusted baseline,
   and this is 3,049 real cash-equity outcomes to build that baseline from.
2. **Calibration priors** — the Brier decomposition is already stored per node.
3. **Cost realism** — ₹176,589 of recorded fees across 3,118 trades, against the segments the cost
   engine prices.
4. **A disproof to check new strategies against** — any cash-intraday bot proposing something
   `opening_range_breakout_v1`-shaped now has 3,049 live outcomes saying what happened last time.

## Consequence for `B15`

The evidence question is answered in both directions: **a track record exists, and it is not one any
new bot may borrow.** So `B15` stands unchanged — a daily paper session that accrues per-bot evidence
is still required. What changes is that the schema for such a record already exists and is proven at
3,481 rows, so building the accrual is a wiring problem rather than a design one.

## Falsifier: is any slice profitable? No.

Run before concluding, per `O.115`'s rule. Every regime slice of `opening_range_breakout_v1` loses
**both net and gross**:

| regime | n | win rate | net | gross (pre-fee) |
|---|---|---|---|---|
| range_bound | 624 | 0.433 | −₹52,268 | **−₹17,349** |
| trending | 219 | 0.347 | −₹56,183 | −₹42,835 |
| indecisive | 524 | 0.286 | −₹76,098 | −₹58,761 |
| unknown | 1,682 | 0.340 | −₹172,082 | −₹85,377 |

Direction gives no shelter either: long n=1,230 win 0.376 −₹120,933; short n=1,819 win 0.333
−₹235,698. The best slice still loses before fees, so "the strategy was run outside its regime" is
ruled out and "the strategy loses" stands.

**Unlooked-for finding:** **1,682 of 3,049** nodes carry `market_regime='unknown'` — the classifier
meant to condition these decisions had no opinion on more than half of them. For that half the
regime hypothesis is untestable rather than false.
