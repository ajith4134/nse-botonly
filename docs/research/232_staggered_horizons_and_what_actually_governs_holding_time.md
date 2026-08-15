# 232 · Staggering the horizon per instrument — and what actually governs holding time

**Measured 2026-08-15**, after `A.115` replaced the single policy horizon with
`PerInstrumentReversionHorizonSelector`. **Negative result on the stated goal, and a bigger finding
underneath it.**

---

## 1 · What was expected, and what happened

`O.97` predicted that one horizon for every position synchronised the exits, and that drawing the
horizon per instrument would disperse the wave the rate gate was refusing. The selector was built,
wired and run on all three sessions.

| | 08-11 | 08-12 | 08-13 |
|---|---:|---:|---:|
| Entries | 45 | 61 | 40 |
| Net — fixed 5-bar horizon (`231`) | −Rs 9,962.65 | −Rs 1,022.36 | −Rs 2,082.25 |
| Net — **selected horizon** | −Rs 10,020.26 | −Rs 1,022.36 | **−Rs 1,816.88** |
| Rate-gate refusals — fixed | 48 | 127 | 22 |
| Rate-gate refusals — **selected** | **56** | **129** | **59** |
| Horizons chosen | 1b ×43, 10b ×2 | 1b ×58, 10b ×3 | 1b ×38, 10b ×2 |

**The stagger did not stagger.** 139 of 146 positions — 95% — selected the SAME horizon, one bar.
The selector is not misbehaving: it scores lower-confidence capture per bar, and on the pooled
calibration that quantity is highest at the shortest horizon for almost every deviation bucket. With
no per-instrument calibrations in the store (`BACKLOG` `M11`: no symbol reaches the 200-event floor),
every instrument in the same bucket necessarily reads the same grid and reaches the same answer.

**Refusals rose on all three sessions**, the opposite of the prediction. Net was unchanged on 08-12,
slightly worse on 08-11, and meaningfully better on 08-13.

## 2 · Why the horizon barely mattered — measured from the journals

The horizon can only govern holding time if a position EXISTS while it runs. It usually does not:

| entry → first fill | 08-12 | 08-13 |
|---|---:|---:|
| median | **60 minutes** | **22.5 minutes** |
| maximum | 90 minutes | 40 minutes |

A five-bar horizon is 25 minutes and a one-bar horizon is 5. **The median entry on 2026-08-12 did
not receive its first fill until an hour after it was sent** — so the horizon had expired before the
position was open, and changing it from 25 minutes to 5 changed nothing that was binding. On 08-13,
where the median fill came in 22.5 minutes, the shorter horizon did bite, and that is exactly the
session whose P&L improved.

Entry-to-first-square-off gaps confirm it from the other side: 5 to 90 minutes, clustered at 40–60
on 08-12 — nothing like either horizon.

**What is actually governing holding time is the recorded book.** `SteppedRecordedBookSource` serves
a snapshot only while it is fresh by that instrument's own gap quantile (`A.110`), and on a thin
scrip most five-minute decision instants have no fresh book at all. The order waits, not for the
strategy, but for the tape to have depth again. Every one of the 146 positions still exited on
`horizon expired` — the horizon is what TRIGGERS the exit, but what SETS the holding time is
liquidity.

## 3 · What this changes

- **`O.97`'s diagnosis is wrong and is corrected in place.** The exit wave is not caused by
  synchronised horizons; it is caused by fills — and therefore exits — bunching onto the few
  instants where the tape has fresh depth for many instruments at once. Dispersing the horizon
  cannot move a wave that liquidity is creating.
- **The selector stays** (`A.115`). It is better-founded than the constant it replaced — the holding
  time is now read from fitted evidence rather than chosen, which is `R.03` — and it is the right
  mechanism the moment per-instrument calibrations exist. It simply does not solve the problem it
  was proposed for.
- **The real lever is the fill path, not the schedule.** An entry that waits an hour for its first
  fill is not the trade the strategy asked for: the deviation it was sized against is long gone.
  That is now the most valuable open question in `F04`, and it is measured rather than suspected.

## 4 · What this does NOT establish

- Whether the hour-long fill latency is a property of the MARKET or of the CAPTURE. The depth tape
  was recorded by one broker on a best-effort basis; a thin scrip with no recorded packet for an
  hour may have been quoting the whole time. `BACKLOG` `M14` (bars versus tape) touches this and
  does not answer it.
- Whether a limit order would do better. Every order here is MARKET, and `A.111`'s open question
  about how a real market order behaves when it cannot fill (`M17`) is upstream of this one.
- Anything about the edge. Gross moved by less than the cost of the trades on two of three sessions.
