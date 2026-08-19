# 256 · `L5.30` adversarial review — the break-even was sound, the promotion rule was not

**Run 2026-08-17 in a fresh subagent, `R.23c` step 5.** Verdict: *"the break-even derivation is
genuinely sound and I could not fool it. The promotion rule built on top of it is broken in three
independent ways, one of which promotes a bot that lost ₹56,000."*

## The attack I predicted, and got wrong

I told the reviewer to try the classic "pennies in front of a steamroller" — many small wins, a few
catastrophic losses — expecting a high win rate and low break-even to promote a losing record. **It
is impossible by construction**, and the reviewer proved it algebraically:

`observed_win_rate > break_even ⟺ total net P&L > 0` is an exact identity, since
`p̂·W − (1−p̂)·L = (Σwins − Σlosses)/n`. Zero violations in 20,000 random records; zero promotions in
a 60×60×6×6 grid with negative totals.

```
195 x +100, 5 x -8000    TOTAL -20,500   win=0.9750 be=0.9877 post=0.0398  refused
990 x  +50, 10 x -20000  TOTAL -150,500  win=0.9900 be=0.9975 post=0.0001  refused
```

Worth recording plainly: I named the failure mode, was confident about it, and it was the one thing
that could not happen.

## What actually broke — three HIGHs

**1 · Non-monotonic in BOTH directions.** Because break-even was re-estimated from the same sample
it was compared against, the threshold could move faster than the posterior:

```
8x+50 / 25x-10    n=33  P&L=+150.00  be=0.1667  post=0.8995  OBSERVING
+ ONE LOSS -0.01  n=34  P&L=+149.99  be=0.1613  post=0.9007  GRADUATION_CANDIDATE  (up two rungs)

1x+50 / 19x-10    n=20  P&L=-140.00  be=0.1667  post=0.1130  OBSERVING
+ ONE WIN  +0.01  n=21  P&L=-139.99  be=0.2857  post=0.0285  demoted
```

428 and 317 counterexamples in a small grid. **My Hypothesis test could not see it** — `_record`
hard-coded `win_size=400`, so every added winner was exactly the current average and the threshold
never moved. The test was vacuous.

The reviewer also caught that **the spec's stated property was itself wrong**: no expectancy-aware
estimator can satisfy "adding a winner never lowers the rung" — only a pure win-rate counter can,
which is the thing this engine exists not to be. I had asserted a property that should not hold.

**2 · `INSERT OR IGNORE` silently lost evidence.** It could not distinguish "replay of the same
trade" from "different trade, same key" — both returned 0.

```
260 real closed trades, TRUE P&L = -Rs 56,000
ladder saw n=64, P&L = +22,400, sessions=4  ->  GRADUATION_CANDIDATE
```

Latent rather than live: the real `position_key` is content-derived and the 3,049-row replay dropped
zero rows. A trap, not a loss.

**3 · Systematically overconfident, and blind to magnitude.** Break-even was a plug-in estimate
treated as known, and a Bernoulli posterior cannot see concentration:

```
30x+400 / 20x-400 steady                    post=0.919610  GRADUATION_CANDIDATE
29x+13.79 +1x+11600 / 20x-400               post=0.919610  GRADUATION_CANDIDATE  (identical)
1x+1e9 / 24x-400                            post=1.000000  GRADUATION_CANDIDATE  (4% win rate)
credit_spread_v1 (real)                     post=1.000000  vs bootstrap P(total<=0) = 12.4%
```

## The fix: the test is now expectancy, not win rate

The reviewer's recommended single change, adopted: **a Bayesian bootstrap on `P(mean net P&L > 0)`**
— Dirichlet(1,…,1) weights over the bot's own realised outcomes — keeping break-even as *reported
evidence* rather than as the threshold. No distributional assumption (returns are not Normal and
this claims they are not), magnitude variance priced directly, and deterministic by a seed derived
from the outcomes so a verdict is replayable (`A.29`).

**Re-validated on the real 3,481-trade record after the change:**

| strategy | rung | P(expectancy > 0) | real P&L |
|---|---|---|---|
| `opening_range_breakout_v1` | **RETIRED** | 0.000 | −₹356,631 |
| `directional_option_orb_v1` | **OBSERVING** | 0.530 | +₹4,104 |
| `credit_spread_v1` | **GRADUATION_CANDIDATE** | **0.953** | +₹21,213 |

All three still correct, and better calibrated — `credit_spread_v1` fell from a falsely precise
`1.000000` to `0.953`.

## The other seven, all fixed

| # | Finding | Fix |
|---|---|---|
| 4 | "Sustained across N sessions" counted any day with any trade, so three losing days padded the count and promoted | Expanding-window cutoffs: the case must have *held* at N of them |
| 5 | `ClosedPaperTrade` had no validation — **negative costs turned a −₹5,000 gross into a promotion**; empty identity, `filled_quantity=-99`, `closed_at` before `opened_at`, and a session label disagreeing with the open were all accepted | Full `__post_init__` |
| 6 | A non-finite posterior made every comparison `False` and **fell through to the most permissive rung** — the default branch was promotion | Refuse non-finite, fail closed |
| 7 | Zero-P&L trades counted as zero-magnitude losses; 200 scratches moved break-even 50.0% → 8.3% with total P&L unchanged | Excluded from both magnitudes |
| 8 | Zero losers gave `break_even=0.0`, so the posterior was identically 1.0 at any sample size | Superseded — expectancy replaces the rate test |
| 9 | `to_bot_maturity()` dropped `is_decisively_below`, so a bot that lost ₹3.5 lakh and one that never traded were both `COLD_START` | **`RETIRED` rung added** at position −1, which spec §4 had asked for and I never built |
| 11 | Connections committed but never closed — 60 descriptors after 500 appends, ~75s for the real-data replay | One connection for the store's life |

**10 · `R.06` orphan — still open.** `BotMaturityLadder.assess()` has no caller in `src/`. Only the
store is wired, via `--record-as`. Recorded as **B16**: no segment bot implements `maturity()` from
it and no dashboard surface shows it, so the engine's output does not yet change behaviour.

## Mutation testing: 6 of 17 survived, all now killed

The prior itself was untested (`Beta(1+w,1+l)` → `Beta(2+w,2+l)` passed all 16 tests), as were the
promotion boundary, zero-P&L classification, the min-trades off-by-one, and both break-even guards.
The suite went **16 → 25 tests**, with a regression test built from each of the reviewer's attacks.

## The lesson

Two reviews in one day, and in both the finding was not "the algorithm is wrong" but **"the thing
you claimed to have done is not the thing you did"**. Here I claimed a monotonicity property, wrote a
test for it, watched it pass, and the test could not have failed. The reviewer's sharpest
contribution was not a counterexample — it was noticing the property I asserted was not one the
design could have.
