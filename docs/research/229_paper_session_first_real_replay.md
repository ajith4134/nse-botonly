# 229 · The first real trading day this rebuild has run — `F04`'s `R.05` pass

**Measured 2026-08-15** by `scripts/verify_paper_session_on_real_data.py 2026-08-11` ·
**Feature**: `F04` · **Spec**: `docs/research/228` · **Decisions**: `A.108`, `A.109`, `A.110`,
`A.111`.

This file records what the run MEASURED. The judgements it prompted are in `CLAUDE_OPINIONS.md`;
the decisions taken because of it are in the plan's Part IV.

---

## 1 · What was run

One full NSE session — **2026-08-11** — replayed point-in-time from the open to the close, over
**2,882 instruments**: every scrip that had both a lot size in the instrument master and five-minute
bars on that date. Every book came from the recorded L2 depth tape; every bar was read
`availability_time <= decision_instant`; every order went through `F02`'s real journal, placer,
lifecycle machine and reconciler. Only the venue was simulated (`A.108` decision 2).

The bars for this date did not exist on 2026-08-14 — the store ended 2026-08-05 while the tape
starts 2026-08-11 — and were acquired by `scripts/backfill_five_minute_bars.py` for this run
(`A.109`, `R.16`): **3,327 instruments answered, 210,059 bars written, 0 failures.**

## 2 · The result

| | |
|---|---|
| Decision instants | 76 (five-minute steps, 09:15–15:30) |
| Instruments | 2,882 · **all** had recorded depth; none was refused for want of a tape |
| Decisions taken | **218,936** |
| Orders placed | **45** (90 legs — every entry squared off) |
| Gross result | **−Rs 9,956.15** |
| Costs (`F01`, real charge schedule) | **Rs 868.54** |
| Net | **−Rs 10,824.69** on a Rs 10,00,000 book — **−1.08%** |
| Book | Rs 10,00,000.00 → Rs 9,89,175.31 |
| Open at the close | **none** |
| Ledger fold agrees with the report | **yes** |

**Decision outcomes**

| Outcome | Count | What it means |
|---|---:|---|
| `abstained` | 213,739 | the strategy declined — no deviation, or the regime panel vetoed |
| `refused_by_gate` | 5,079 | sized, then refused by a tier of `F03`'s gate |
| `unsizable` | 73 | inputs could not be assembled — every one a missing calibration cell |
| `placed` | 45 | funded, gated, sent |

Every one of the 73 unsizable decisions names the store that could not answer, and all 73 are the
same store: the reversion calibration has no cell covering that instrument's deviation at that
horizon. Mostly liquid-fund and ETF units (`ABSLLIQUID`, `LIQUIDSBI`, `GILT5YBEES`) — instruments
whose deviations sit outside the fitted ladder because they barely move.

## 3 · Against the pass criteria stated before the run

1. **No unhandled exception.** Passed — on the fourth attempt. The first three ended in tracebacks,
   each a real defect: a negative price collar (`A.110` 1), a division by `log10(1)` in the
   choppiness index (`A.110` 2), and a position marked closed while its entry was still filling
   (`A.111`). None was reachable from the hermetic suite.
2. **Every decision carries a reason.** Passed — 218,936 decision records, each with an outcome and,
   where it declined, the rule or the store that declined it.
3. **No fill better than the touch, no fill beyond recorded depth.** Passed by construction and by
   test: fills come from `walk_order_book` over the recorded ladder, one rung per poll, and the
   venue refuses when no book is held.
4. **Nothing survives the close** (`R.01`). Passed — all 45 positions exited in full; `open_at_close`
   is empty.
5. **The closing balance equals the fold of the ledger's own events.** Passed.

## 4 · What the number actually says, and what it does not

**It says the pipeline works end to end and loses money.** −1.08% of the book in one session, of
which **Rs 868.54 is cost** — costs are 8% of the loss, so this is not a cost problem, it is an edge
problem. The strategy entered 45 times and the average entry lost about Rs 221 gross.

**It does not say the strategy is unprofitable.** One session is one draw. What it does establish is
that the loss is now MEASURABLE through the real path — the same journal, reconciler and ledger that
would carry real money — which is the whole point of `A.108` decision 2 and the thing 3,481 closed
trades from the previous build could never demonstrate about this one.

**It does not say the fills are realistic in one specific way** (`R.11`): a MARKET order in this
venue rests across books and fills rung by rung for as long as the session lasts, and a real market
order does not. What NSE does with the unfilled remainder is unsourced — `BACKLOG` `M17`.

**What one session cannot answer**, and what the next runs must: whether the loss is the strategy,
the horizon, or the collar; whether it holds across sessions (only 2026-08-11, -12 and -13 have
tape); and whether the backfilled bars agree with the tape's own last-traded prices at the same
instants (`BACKLOG` `M14`).

## 5 · Cost of the run

Roughly 25 minutes wall-clock end to end, of which the depth tape's single streamed pass over
11.4 million rows is the bulk. Before `SteppedRecordedBookSource.preload`, the same run was on
course for **six hours** — one full scan per instrument — which is the difference between a
verification anyone will actually re-run and one nobody will.
