# 230 · Three replayed sessions, and what changes between them

**Measured 2026-08-15** by `scripts/verify_paper_session_on_real_data.py` on 2026-08-11, -12 and
-13 — every session for which this project holds a recorded L2 depth tape. **Feature**: `F04` ·
**Extends**: `docs/research/229` (the first of the three) · **Decisions**: `A.108`–`A.112`.

Each session was replayed independently against a fresh Rs 10,00,000 book: these are three
separate draws, not a compounding equity curve.

---

## 1 · The three results

| | 2026-08-11 | 2026-08-12 | 2026-08-13 |
|---|---:|---:|---:|
| Instruments (bars **and** a recorded book) | 2,882 | 1,418 | 650 |
| Decisions | 218,936 | 107,717 | 49,323 |
| Entries placed | 45 | 61 | 40 |
| Refused by the gate | 5,079 | 4,405 | 1,812 |
| Unsizable (no calibration cell) | 73 | 24 | 40 |
| **Gross** | **−Rs 9,956.15** | **−Rs 328.50** | **−Rs 1,284.14** |
| **Costs** (`F01`, real schedule) | **Rs 868.54** | **Rs 874.38** | **Rs 876.01** |
| **Net** | **−Rs 10,824.69** | **−Rs 1,202.88** | **−Rs 2,160.15** |
| Net as % of the book | −1.08% | −0.12% | −0.22% |
| Open at the close | none | none | none |
| Ledger fold agrees | yes | yes | yes |

**Totals across the three:** 146 entries, gross **−Rs 11,568.79**, costs **Rs 2,618.93**, net
**−Rs 14,187.72**. Three sessions, three losses.

## 2 · What is stable, and it is the surprising part

**Cost is almost constant: Rs 868.54, Rs 874.38, Rs 876.01** — a spread of Rs 7.47 across sessions
whose universes differ by a factor of four and whose entry counts differ by 50%. That is not a
coincidence and it is not noise: the charge is dominated by per-order brokerage and the statutory
per-trade floors, so it scales with the NUMBER of round trips and barely at all with their size.
The book was ~Rs 870 a day poorer before any view about direction was expressed.

**Every position exited on its horizon.** Across all 146 entries, not one reached the close still
open — the five-bar horizon always expired first, so `R.01`'s square-off never had to fire in
anger. The close-time path is therefore exercised but never load-bearing in these three sessions,
which is worth knowing before it is trusted.

**Gross is not stable at all:** −Rs 9,956, −Rs 329, −Rs 1,284. The 08-11 loss is eight times the
other two combined, and it came from 45 entries rather than 61 — the loss is concentrated in a few
large positions in illiquid scrips (`3PLAND`, `ARCHIES`, `AMJLAND`), which is exactly what a
volatility-budget sizer does when it meets a quiet, thin instrument.

## 3 · The composition changes, and that is the finding

| | 08-11 | 08-12 | 08-13 |
|---|---:|---:|---:|
| Share of the loss that is COST | 8% | **73%** | 41% |
| Gross per entry | −Rs 221.25 | −Rs 5.39 | −Rs 32.10 |
| Cost per entry | Rs 19.30 | Rs 14.33 | Rs 21.90 |

On 2026-08-12 the strategy was **almost exactly flat gross** — Rs 5.39 lost per entry across 61
entries — and still lost Rs 1,202.88, because costs took Rs 874.38 of it. That is the clearest
statement the three runs make: **at this trade size the edge is indistinguishable from zero and the
cost is not.** A strategy whose gross per trade is Rs 5 cannot pay a Rs 14 toll.

`docs/research/229` read the 08-11 session as 92% edge and 8% cost. Across three sessions that
reading holds only for 08-11; the honest aggregate is **gross −Rs 11,568.79 against costs
−Rs 2,618.93**, so 82% edge and 18% cost — and one of the three sessions is a pure cost story.

## 4 · What this does NOT establish

- **Not a verdict on the strategy.** Three sessions, 146 entries, one tape week. The `t` on a mean
  of −Rs 79 per entry over 146 draws with this dispersion is nowhere near a conclusion.
- **Not a fill-realism claim.** A MARKET order still rests across books in this venue
  (`BACKLOG` `M17`), which flatters an illiquid entry by letting it fill patiently at prices a real
  market order would not have waited for.
- **Not independent of the bar backfill.** All three sessions' bars were fetched from Kite for these
  runs and have not been checked against the tape's own last-traded prices (`BACKLOG` `M14`).
- **Not a full universe on every date.** 2,882 / 1,418 / 650 instruments is what the DEPTH TAPE
  recorded on each date, not what NSE traded. The narrowing is a property of the capture, and a
  session that recorded 650 instruments cannot say anything about the other 1,700.

## 5 · What the runs cost to produce

Bars had to be acquired for all three dates (`R.16`). The store now holds **362,761 five-minute
bars across 5,395 instrument-sessions** for them (210,196 / 104,878 / 47,687), of which 362,624 were
fetched by these runs from Kite at 1.5 requests/second with zero failures. Each replay then
takes roughly 10–25 minutes, dominated by the single streamed pass over that day's depth tape.

## 6 · The defect the third session found

2026-08-13 did not finish on its first attempt: a round trip that closed at **exactly** its entry
price asked the paper ledger to record a realised profit of zero, and the ledger refuses a
zero-rupee event — correctly, since an event of zero is a statement about the book rather than a
movement in it. Fixed in `A.112`: a breakeven trade records no realisation, and still pays its
costs and releases its capital. **Fifth real-data defect in this feature, and the fifth that no
hermetic test reached** — a synthetic ladder always moves the price, and only a real book on a
thin scrip stands perfectly still.
