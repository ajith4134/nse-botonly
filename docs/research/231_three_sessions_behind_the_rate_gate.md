# 231 · The same three sessions, behind the real rate limiter

**Measured 2026-08-15**, after `A.114` wired `SimulatedTimeSubmissionRateGate` into the paper loop.
**Supersedes the numbers in `docs/research/230`**, which were produced with `AlwaysPermits` and
therefore assumed an order flow the wire would not have carried.

---

## 1 · Before and after

| | 08-11 | 08-12 | 08-13 |
|---|---:|---:|---:|
| Instruments | 3,327 | 1,418 | 650 |
| Entries placed | 45 | 61 | 40 |
| Gross — **ungated** (`230`) | −Rs 9,956.15 | −Rs 328.50 | −Rs 1,284.14 |
| Gross — **gated** | **−Rs 9,091.93** | **−Rs 148.04** | **−Rs 1,206.22** |
| Costs — gated | Rs 870.72 | Rs 874.32 | Rs 876.03 |
| Net — ungated | −Rs 10,824.69 | −Rs 1,202.88 | −Rs 2,160.15 |
| Net — **gated** | **−Rs 9,962.65** | **−Rs 1,022.36** | **−Rs 2,082.25** |
| Rate gate | 124 granted, 48 refused (27.9%) | 125 granted, 127 refused (50.4%) | 84 granted, 22 refused (20.8%) |
| Open at the close | none | none | none |
| Ledger fold agrees | yes | yes | yes |

Aggregate net moves from **−Rs 14,187.72** to **−Rs 13,067.26**. Every session lost slightly less,
and none of that is edge: it is fewer round trips paying fewer tolls.

*(The 08-11 universe grew from 2,882 to 3,327 between the two runs because the bar backfill
continued after `230` was written. The comparison is therefore not perfectly like-for-like on that
date; 08-12 and 08-13 are.)*

## 2 · The refusals did not fall where I expected

**Entry counts are IDENTICAL before and after: 45, 61, 40.** Not one entry was lost to the rate
gate. What the gate refused was **square-offs**, and the journal shows it plainly:

| Square-off legs written | ungated | gated |
|---|---:|---:|
| 08-11 | 81 | **127** |
| 08-12 | 64 | **191** |
| 08-13 | 44 | **66** |

The mechanism: entries are spread thinly across a 76-step session and rarely collide, but a
horizon-expiry wave squares off many positions at ONE simulated instant, and the 1-second ceiling of
10 refuses everything past the tenth. The loop retries the unexited quantity on the next step, so
each refused exit becomes another leg, and the count balloons.

**This is the finding, and it is a risk statement rather than an accounting one:** under a real
wire, a position whose horizon has expired does not necessarily get to leave. It stays on the book
another five minutes, taking market risk the strategy did not intend, and it does so precisely when
many positions want out at once — which is when the market is most likely to be moving. The paper
record now shows that; before, it did not.

Everything still went flat by the close on all three dates, so the retry loop does converge. It
converges because the horizon wave disperses across later steps, not because the gate was generous.

## 3 · What the gate is, exactly

- **The real `OrderSubmissionRateLimiter`**, on the real dated ceilings — the binding one here is
  the 1-second window of 10 that keeps this system under the registration threshold (`A.101`).
- **Counting in SIMULATED time** (`A.114` decision 1): clock seams driven from the replay clock, so
  the result depends on the market rather than on how fast the host replays it.
- **Never queuing** (`A.114` decision 2, forced by the code): `acquire` waits by sleeping in real
  seconds, and a replay's clock does not advance while it sleeps. The gate asks at the decision
  instant and takes no for an answer.

## 4 · What this still does NOT establish

- **The queueing half of `L3.06` is unexercised** (`BACKLOG` `M20`). A live order that waits 200 ms
  for budget is SENT; this gate would have refused it. The paper record therefore under-counts what
  a live session sends, which is the safe direction for a guard and still a gap.
- **The refusal wave is an artefact of one horizon.** Every position uses the same five-bar horizon,
  so they expire together. A mixed-horizon strategy would collide far less, and the 50.4% refusal
  rate on 08-12 says more about that synchronisation than about the ceiling.
- **Nothing about the edge has changed.** Gross improved because fewer trades happened, not because
  any trade got better. `O.95`'s reading — the edge is indistinguishable from zero and the cost is
  not — survives this run unchanged.
