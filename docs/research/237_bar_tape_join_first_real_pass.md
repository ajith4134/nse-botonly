# 237 · The first real pass of the bar/tape join — and the price series that was wrong

**Measured 2026-08-15. Engine: `bar_tape_join_verification_engine` (spec `docs/research/236`,
backlog `M14`). Runner: `scripts/verify_bar_tape_join_on_real_data.py --all --significance 0.01`.**

> This is a MEASUREMENT report, not a design doc. The `R.16`/`R.17` sourcing pass for this engine
> was run and recorded in `docs/research/236` — four candidates, all rejected on mechanical
> evidence (`pip index versions` misses, `ModuleNotFoundError`), with `pandas` reused for the
> volume arithmetic. No design decision is taken here that would need its own search.

---

## What was asked

`F04` takes its signal from the bar store (`price_bars`, backfilled from Kite's `5minute`
historical endpoint) and produces every fill from the recorded depth tape (parquet, captured live
from Kite's full-mode websocket). Two independently-sourced stores, joined at every decision, and
never compared. If they disagree, every P&L in `229`–`234` is partly a measurement of the
disagreement rather than of the strategy.

## The result, all three recorded sessions

| Session | Instruments | Verified | Refuted | Unverifiable | Bars comparable | Pooled disagreement |
|---|---|---|---|---|---|---|
| 2026-08-11 | 9,000 | 3,109 | **121** | 5,770 | 176,059 / 210,196 (83.8%) | 2.478% |
| 2026-08-12 | 1,420 | 1,359 | **56** | 5 | 79,886 / 104,878 (76.2%) | 2.888% |
| 2026-08-13 | 652 | 640 | **10** | 2 | 18,359 / 47,687 (38.5%) | 2.517% |

**The join is broadly sound, and the agreement is exact rather than approximate.** All nine
deciles of `|bar_close − tape_last_price|`, measured in units of each instrument's own spread, are
**0.00** on every session. Where the two stores agree they agree to the paise, not to within a
tolerance — which is the answer you want and not the one a sampling argument would predict.

The 2026-08-13 coverage of 38.5% is `A.118`'s half-session (the capture was stopped by hand at
12:15), not a property of the join.

## The disagreements, and which check found them

| Class | 08-11 | 08-12 | 08-13 |
|---|---|---|---|
| `close_outside_recorded_book` | 3,543 | 1,616 | 333 |
| `disagrees_beyond_derived_tolerance` | 820 | 691 | 129 |

**The book-bracket comparison found roughly four times what the last-price comparison found.** The
spec argued for it on the grounds that a last-price check is satisfied by two feeds sharing an
upstream and by a token collision alike; the measurement says it is also the check that does most
of the work in practice. Had this engine shipped with only the price comparison — which is what a
first draft would have done — it would have missed most of what it found.

`tape_exceeds_bar` fired **384 times** across the three sessions (269 / 114 / 1): bars where the
tape's cumulative-volume increment across the interval EXCEEDS the volume the bar says traded in
it. Sampling moves both endpoints inward and can only under-count, so this side of the asymmetric
test cannot be explained away as coverage.

---

## The finding: `HINDPETRO`'s bar series is adjusted and its book is not

187 instrument-sessions were refused, over 175 distinct instruments. Classifying each refusal by
the SHAPE of its disagreement — the spread of `bar_close / tape_last_price` across its
disagreeing bars — separates two entirely different causes:

| Signature | 08-11 | 08-12 | 08-13 |
|---|---|---|---|
| **constant ratio** (a price-series adjustment) | 2 | 1 | 1 |
| sporadic (boundary and sampling) | 119 | 55 | 9 |

> **The by-hand classification above was later built into the engine**, and building it took three
> corrections — a dispersion bound that was wrong by a factor of six, an all-bars residual test
> that rejected `HINDPETRO` itself, and a fit that had to be restricted to out-of-book closes.
> `docs/research/238` records all three. The engine's final counts are 3 / 2 / 1 rather than
> 2 / 1 / 1: it additionally surfaces `OIL` (1.00053 on 11% of bars) and `PANAMAPET` (0.99888 on
> 10%) as CANDIDATES. Neither is claimed as a defect — the two real ones rescale **100%** of their
> bars, and the counts are carried on every verdict so thin evidence reads as thin.

**`HINDPETRO` (token 359937) disagreed on 150 of 150 comparable bars, across all three sessions,
at a ratio of 0.95099 that does not vary.** Binomial tail 6.66e-104. A constant ratio is not a
market phenomenon and is not a sampling artefact; it is one series multiplied by a factor.

**Triangulated against a third, independent source.** NSE's own bhavcopy — the exchange's
end-of-day record, already ingested under `nse_bhavcopy_cash` — for 2026-08-12:

```
HINDPETRO  ClsPric=390.00  LastPric=390.00  PrvsClsgPric=390.85
```

- depth tape, last packet of the session: **39,000 paise = ₹390.00** ✅ agrees with the exchange
- bar store, 5-minute close: **37,090 paise = ₹370.90** ❌ off by the constant 0.951

So it is the **bar store** that is wrong, and the tape and the exchange that agree. The depth tape
is exonerated; the historical endpoint is the defective source.

**The mechanism.** Kite's historical endpoint returns a series adjusted for corporate actions **as
of the time you ask**. `scripts/backfill_five_minute_bars.py` ran on 2026-08-14/15, after
`HINDPETRO`'s ex-date, so the bars it fetched for sessions that had ALREADY HAPPENED came back
retro-adjusted, while the tape recorded on each of those days holds the raw traded price. The
adjustment is correct as an adjustment; it is wrong as a record of what a trade on 2026-08-12
would have filled at.

`XCHANGING` on 2026-08-11 has the same signature at ratio 0.96958. Those two are the whole of it.

**Why it matters more than its two instruments suggest.** The defect is not a property of these
scrips — it is a property of the GAP between a session and the backfill that fetches it. It scales
with that gap, it is silent, and it is directionally invisible: a 4.9% shift in the whole series
leaves every deviation, every fitted reversion event and every chart looking entirely normal while
the fill happens 4.9% away. Nothing in this system would have found it. `L0.07`'s
`corporate_action_adjustment_engine` exists, but the corporate-action feed is not reaching the
backfill path — the `corporate_action` table in `market_data.sqlite3` holds **0 rows**, and the
ingest store carries no corporate-action source at all.

The other 183 refusals are sporadic: instruments whose disagreement rate exceeded their session's
own base rate without a constant factor behind it. Those are boundary and sampling effects, and
the leave-one-out binomial test is doing exactly what it should by ranking them below a systematic
one rather than lumping them together.

---

## What this changes

1. **187 instrument-sessions now produce no paper fill.** `refuted_instruments_for` unions into
   `dashboard_server.inadmissible_depth_instruments` and filters the paper replay's instrument
   set, so the refusal is a behaviour change rather than a report.
2. **The `R.05` obligation `M14` recorded is closed** for all three recorded sessions.
3. **A new defect is open, and it is the bigger one:** the backfill must either fetch raw prices,
   or record the adjustment basis it fetched under so a consumer can tell an adjusted series from
   a traded one. Tracked as `M26`.
4. **`docs/research/229`–`234` are NOT invalidated**, which was the live risk. The join holds to
   the paise on 3,109 / 1,359 / 640 instruments; the sessions those numbers were computed over are
   sound apart from the two instruments named here.

## Cost, and the fix that made the run possible

The first implementation read the tape once PER INSTRUMENT via
`OrderBookSnapshotReplayEngine.session_snapshots_for`, each call re-scanning the session's parquet.
It did not finish 2026-08-12's 1,420 instruments in **ninety minutes**. `preload_session_snapshots`
streams the session ONCE and reduces it onto the bar boundaries, keeping gaps and spreads as
accumulators so the derived thresholds still describe the full feed rather than the survivors.

**2026-08-13 went from not finishing to 2 minutes 46 seconds.** All three sessions, 11,072
instrument-sessions, now run in one pass. `test_the_streamed_preload_answers_the_same_questions_as_a_per_instrument_read`
diffs the two paths bar by bar so the speed-up cannot quietly change an answer.
