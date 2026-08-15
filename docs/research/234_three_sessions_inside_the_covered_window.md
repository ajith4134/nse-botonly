# 234 · The three sessions, measured only where the tape can account for them

**Measured 2026-08-15** after `A.116` (replay restricted to the window the depth capture covers) and
`A.117` (a book with no gap distribution no longer lives forever). **These supersede every P&L in
`docs/research/229`–`232`**, which were produced by replaying decision instants where no fill was
possible.

---

## 1 · The numbers

| | 08-11 | 08-12 | 08-13 |
|---|---:|---:|---:|
| Capture covers (IST) | 10:00–15:30 | 10:35–15:30 | 09:55–13:00 |
| Decision instants with a book | **67 / 76** | **60 / 76** | **38 / 76** |
| Entries placed | 11 | 11 | 10 |
| **Gross** | −Rs 7,870.39 | **+Rs 225.97** | −Rs 1,761.70 |
| Costs (`F01`) | Rs 719.39 | Rs 731.89 | Rs 702.72 |
| **Net** | −Rs 8,589.78 | −Rs 505.92 | −Rs 2,464.42 |
| Rate-gate refusals | **0** | **0** | **0** |
| Open at the close | none | none | none |
| Ledger fold agrees | yes | yes | yes |

**2026-08-12 is the first positive GROSS this rebuild has produced** — +Rs 225.97 across 11 entries.
It is still a net loss, because Rs 731.89 of cost sits on top of it. That is `O.95`'s finding in its
sharpest form yet: the edge is small and real on at least one session, and the toll is bigger.

## 2 · The exit wave was an artefact, and it is gone

| rate-gate refusals | before `A.116` | after |
|---|---:|---:|
| 08-11 | 56 | **0** |
| 08-12 | 129 | **0** |
| 08-13 | 59 | **0** |

Not one refusal on any session. The wave that `O.97` tried to disperse with staggered horizons, and
that `A.114` measured at 21% of all orders, was **entries piling up unfilled in the part of the
session the tape does not cover**, retrying step after step until the capture began. With the loop
restricted to instants that have a book, nothing queues and nothing is refused.

Entry counts fell with it: 45/61/40 → 11/11/10. Those are not lost trades; they are decisions that
were being recorded as trades while having no market to trade against.

## 3 · A claim from `230` that this run refutes

`docs/research/230` §2 said cost "scales with the NUMBER of round trips and barely at all with their
size", from three sessions whose costs sat within Rs 7.47 of each other. This run separates the two
variables for the first time:

| 08-12 | trades | cost |
|---|---:|---:|
| ungated, whole session | 61 | Rs 874.38 |
| inside the covered window | 11 | Rs 731.89 |

**Trade count fell 82% and cost fell 16%.** Cost is therefore dominated by TURNOVER, not by trade
count — the earlier reading was an accident of three sessions that happened to have similar
turnover. Per-trade cost rose from about Rs 14 to about Rs 65 because the surviving positions are
larger, not because anything got more expensive. `O.95` is corrected in place.

This matters for what to do next: a cost gate that suppresses marginal ENTRIES saves less than it
appeared to, because the cost follows the size of what is traded rather than how often. Sizing is
the lever cost responds to.

## 4 · What is still true, and what is now open

- **Still true:** all three sessions lose net; nothing survives the close; every ledger fold agrees;
  the loop runs end to end on real bars and a real recorded book through the real order path.
- **Now open — and above every strategy question:** why the capture is partial, and why it stopped
  at 12:15 on 2026-08-13 (`BACKLOG` `M22`). On that date the loop can only see half a session, and
  no amount of strategy work is worth more than fixing the instrument that measures it.
- **Sample:** 32 entries across three partial sessions. Nothing here is a verdict on the strategy.
