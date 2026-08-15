# 233 · Why a paper entry waited an hour — the capture, not the threshold

**Measured 2026-08-15** by `scripts/measure_depth_tape_packet_coverage.py`. Answers `BACKLOG` `M21`
and settles the question `O.98` left open. **Decision taken on the result: `A.116`.**

---

## 1 · The question

`O.98` measured a median 60-minute wait from a paper entry being sent to its first fill on
2026-08-12, and named two candidate causes pointing at opposite fixes: a sparse TAPE (nothing to
fill against — the latency is real), or a staleness THRESHOLD discarding books the tape holds
(`A.110` — the latency is ours, and cheap to fix).

## 2 · The measurement, per traded instrument

| | 2026-08-12 | 2026-08-13 |
|---|---:|---:|
| Instruments the session ordered | 42 | 28 |
| Packets per instrument — median | 3,893 | 2,176 |
| — minimum | 304 | 113 |
| Decision buckets with any depth — median | **59 / 76** | **30 / 76** |
| Instants the book source SERVED — median | 55 / 76 | 27 / 76 |
| **served / covered** — median | **0.932** | **0.916** |
| Instruments below half served | 0 | 0 |

**The threshold is not the constraint.** It discards about 7% of the buckets that have depth, and no
instrument loses more than half. `A.110`'s staleness rule is doing what it was built to do and is
not the reason orders wait.

## 3 · What is the constraint: the capture is a PART of each session

| Session (IST) | capture ran | of a 09:15–15:30 session |
|---|---|---|
| 2026-08-11 | 09:56 → 15:30 | misses the **first 41 minutes** |
| 2026-08-12 | 10:30 → 15:30 | misses the **first 75 minutes** |
| 2026-08-13 | 09:51 → **12:15** | misses 36 minutes at the open and **3h 15m before the close** |

That reproduces the bucket counts exactly — five hours is about 60 five-minute buckets on 08-12,
and two hours twenty is about 29 on 08-13 — and it explains the fill latency directly. **An entry
decided at 09:40 on 2026-08-12 had no recorded book until 10:30**, so it could not fill for fifty
minutes no matter what the strategy, the sizer or the venue did. The median 60-minute wait is very
close to the median distance from a decision to the start of the capture.

The 2026-08-13 case is worse and in the other direction: the capture stops at 12:15, so every entry
after it can never fill, and every decision instant from 12:15 to 15:30 — nearly half the session —
was being replayed against nothing.

## 4 · What this invalidates

Every number in `docs/research/229` through `232` was produced by replaying **76 decision instants
against a tape covering 59, 60 and 30 of them**. The loop was deciding, sizing, gating and placing
orders at instants where no fill was possible, and counting the results. Those P&Ls are not wrong
arithmetic — the ledger folds still agree — but they are measurements of a system running against a
partly-absent market, and the share of the session that was absent differs by date, which makes the
three sessions not comparable with each other either.

`A.116` restricts the loop to the window the tape actually covers, derived from the tape itself.
The re-run under that restriction is the first set of numbers where every decision could have been
acted on.

## 5 · What this does NOT establish

- **Why the capture is partial.** Session reports exist beside the tape
  (`~/nse_archive/depth_tape/session_report_*.json`) and were not read for this measurement. Whether
  the recorder started late, crashed at 12:15 on 08-13, or was stopped by hand is unknown and is the
  next question — a capture that stops silently is a worse problem than a short one.
- **That the remaining latency is zero.** Inside the covered window an order still fills one rung
  per served instant, so a large order in a thin book still takes several. That is the venue
  behaving correctly, and it is separable from this.
- **Anything about instrument quality.** The minimum packet count is 113 for a whole session, which
  is genuinely thin, and thin instruments will still fill slowly inside the window.
