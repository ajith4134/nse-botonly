# 236 · Bar-close versus depth-tape join verification — spec

**Backlog item `M14`. Opened 2026-08-15. Engine for `F04`'s outstanding `R.05` obligation.**

---

## The gap this closes

`F04`'s claim is that a signal taken from real bars is filled against the book that was
actually there. Two independent stores make that claim true or false:

- **the bar store** (`price_bars` in `~/.nse_algo_trader/market_data.sqlite3`), backfilled from
  Kite's `5minute` historical endpoint by `scripts/backfill_five_minute_bars.py`, and read by
  `paper_session_signal_source.bars_available_at`;
- **the depth tape** (`~/nse_archive/depth_tape`, parquet), recorded live from Kite's full-mode
  websocket, and read by `replayed_depth_book_source` to produce every fill.

**Nothing has ever compared them.** The backfill fetched bars for the instruments the tape
recorded, and stopped there. If the two disagree — different prices at the same instant, a bar
whose close lies outside the book the tape held, volume that does not reconcile — then the
signal and the fill are describing different markets and every P&L in `docs/research/229`–`234`
is partly a measurement of that disagreement rather than of the strategy.

Recorded as opinion `O.93`: an unverified join is the thing that would change my mind about the
backfill acquisition being sufficient.

This is a **verification engine**, not a script: its output is an admissibility verdict per
instrument-session that the paper loop consumes, in exactly the shape `L0.33`'s
`inadmissible_instruments` already takes.

---

## What is compared, and why each comparison is independent

Three comparisons, deliberately not one. A single price check can be satisfied by two feeds that
share an upstream and disagree about everything else; three checks over different quantities can
not.

### 1 · Bar close against the tape's last traded price

For a five-minute bar stamped `T`, the interval is `[T, T+5m)` and the bar's close is the last
trade inside it. The tape's `last_price_paise` on the last packet at or before `T+5m` is the same
quantity, sampled by a different path. **They should be equal.**

They will not always be, and the reasons are knowable:

- the tape samples; its last packet inside the bar may precede the bar's final trade;
- `exchange_time` has one-second resolution while the packet gap p10 is 0.25s (`research/206`),
  so alignment at a boundary is approximate by construction;
- the capture is partial (`A.116`/`A.118`), so a bar may have no packet at all.

**The tolerance is derived, never assumed (`R.03`):** a difference smaller than the
instrument's own prevailing spread at the aligned instants is the same market observed twice a
fraction of a second apart. A difference larger than that is not. The spread is read from the
tape's own book, per instrument, per session — a one-rupee scrip and a `NIFTY` weekly option
cannot share a paise constant.

### 2 · Bar close against the aligned book's bracket

Stronger than 1 and cheap: a traded price must lie inside the book that produced it. If the
aligned packet has both sides, the bar's close must satisfy
`best_bid <= close <= best_ask`, widened by the derived tolerance. A close **outside** the book
is not a sampling artefact — it says the two stores hold different instruments, a wrong token, or
a wrong day.

This is the comparison that catches the failure that actually matters, because that failure
(a token collision, `L0.02`) produces prices that are entirely plausible in isolation.

### 3 · Traded volume across the bar interval

`volume_traded` on the tape is the exchange's **cumulative** day volume. Its increment between
the packet aligned to `T` and the packet aligned to `T+5m` is the volume traded in between, up to
sampling at both ends. The bar carries the same quantity directly.

Sampling can only make the tape's increment **smaller** than the bar's volume, never larger
(the endpoints move inward, never outward). So:

- `tape_delta <= bar_volume` — consistent, magnitude bounded by the sampling gap;
- `tape_delta > bar_volume` — **impossible under the sampling model**, and therefore evidence of
  a genuine disagreement between the two stores rather than of coverage.

An asymmetric test is the point: it has a side that sampling cannot explain away.

---

## The three-way partition (`A.41`)

Every comparison lands in one of three states, and "cannot tell" is a first-class state that is
never folded into either of the others.

| Class | Meaning |
|---|---|
| `AGREES_WITHIN_DERIVED_TOLERANCE` | bar and tape describe the same market at this instant |
| `DISAGREES_BEYOND_DERIVED_TOLERANCE` | they do not, and the gap exceeds what sampling explains |
| `CLOSE_OUTSIDE_RECORDED_BOOK` | the close is not a price this book could have traded at |
| `TAPE_ABSENT_IN_BAR_INTERVAL` | no packet at all — the capture did not cover this bar |
| `TAPE_STALE_AT_BAR_CLOSE` | nearest packet older than the instrument's derived staleness threshold |
| `BOOK_ONE_SIDED_AT_BAR_CLOSE` | a packet exists but cannot bracket; falls back to comparison 1 |

The last three are **unverifiable**, not agreements. A session in which the tape covered nothing
must report zero verified bars, not perfect agreement.

The staleness threshold is not a new number: it is
`OrderBookSnapshotReplayEngine.staleness_threshold_millis_for`, the same per-instrument gap
quantile the fill path uses. A join verified under a threshold looser than the one fills obey
would be verifying a market the loop never trades in.

---

## The verdict, and why it is a binomial test rather than a cutoff

A per-instrument agreement fraction needs a rule to become a verdict, and "below 95% is bad" is
precisely the hardcoded constant `R.03` forbids. Neither is a Tukey fence — `1.5 × IQR` is a
constant wearing a distribution's clothes.

**The session supplies its own null.** Pool every verifiable comparison across every instrument
in the session; the pooled disagreement rate `p̂` is what disagreement looks like when the join
is sound and only sampling is at work. An individual instrument is then tested against that
pooled rate with an exact one-sided binomial tail:

```
P(X >= k | n, p̂) = sum_{i=k..n} C(n,i) p̂^i (1-p̂)^(n-i)
```

- tail `< significance` → **`JOIN_REFUTED`**: this instrument disagrees more than the session's
  own base rate can explain;
- otherwise, and with enough comparisons to have been able to reject → **`JOIN_VERIFIED`**;
- too few comparisons for the test to reject **even if every one disagreed** →
  **`JOIN_UNVERIFIABLE`**. The minimum is derived, not chosen: the smallest `n` with
  `p̂ⁿ < significance`.

`significance` is a **policy input with no default**, following the established pattern of
`OrderBookSnapshotReplayEngine.__init__`'s `staleness_quantile`: the estimator is derived from
data, the risk appetite belongs to the operator.

Degenerate `p̂` is handled explicitly rather than by an epsilon. `p̂ = 0` (nothing disagreed
anywhere) makes any single disagreement infinitely surprising, so one disagreement refutes;
`p̂ = 1` makes the test powerless and every instrument is `JOIN_UNVERIFIABLE`, which is the
honest reading of a session where nothing agreed.

---

## Output, and who consumes it

`SessionJoinVerificationReport` carries, all measured:

- per-instrument verdicts and their evidence counts;
- pooled disagreement rate and the derived minimum comparison count;
- deciles of `|bar_close − tape_last_price|` in units of the instrument's own spread — the
  distribution describing its own shape, as `_deciles` already does for gaps;
- the volume reconciliation, split by the asymmetric test above;
- coverage: bars total, bars verifiable, and the unverifiable split by cause.

**Consumer (`R.06`, no orphan):** `refuted_instruments()` returns a `frozenset[int]` in the same
shape `OrderBookSnapshotReplayEngine(inadmissible_instruments=…)` already accepts, and is unioned
with `L0.33`'s consolidated-feed refusals by `dashboard_server.inadmissible_depth_instruments`.
An instrument whose bar store and depth tape describe different markets stops producing paper
fills, which is a behaviour change, not a diagnostic.

**Surface (`R.08`):** the counts render on the depth surface beside the capture liveness the same
report already feeds.

---

## Acceptance

1. Unit tests over hand-built tapes for each of the six classes.
2. Property test: agreement is invariant to a uniform paise-scale shift applied to BOTH stores,
   and the volume test never reports `tape_delta > bar_volume` for a tape sampled from the bar.
3. Adversarial: a token-collision tape (right shape, wrong instrument's prices) must be
   `JOIN_REFUTED`; a tape covering one bar in a hundred must be `JOIN_UNVERIFIABLE`, never
   `JOIN_VERIFIED`.
4. `R.05`: run on all three recorded sessions (2026-08-11/12/13) against the real bar store, and
   record what it found — including if what it finds invalidates `research/229`–`234`.

## Sourcing pass (`R.16`/`R.17`) — run 2026-08-15, MECHANICAL evidence only

Searched for an existing implementation of "reconcile an OHLC bar series against the tick/depth
stream it came from, and emit a per-instrument verdict". Query run:
*python library reconcile OHLC bars against tick data consistency check open source*.

| Candidate | Mechanical test | Verdict |
|---|---|---|
| `ticks_data_sampling_preprocessing` (GitHub, vsheigani) | `pip index versions ticks_data_sampling_preprocessing` → **`ERROR: No matching distribution found`** — not on PyPI | **REJECTED** — not installable. Also solves the inverse problem: it *builds* bars from ticks, it does not compare two independently-sourced series |
| Backtrex "native OHLC validation layer" | `pip index versions backtrex` → **`ERROR: No matching distribution found`** | **REJECTED** — commercial SaaS, no distributable package. Its checks are intra-bar (`high >= close`, gaps, duplicate stamps) — already covered by `BarRecord.__post_init__` |
| `nautilus_trader` `BarAggregator` consistency checks | `python -c "import nautilus_trader"` → `ModuleNotFoundError` | **NOT VENDORED** — stays a design analog, matching `A.39`'s finding for `qlib`. Aggregation-time invariants, not a cross-store join test |
| `zipline` adjustments reconciliation | `python -c "import zipline"` → `ModuleNotFoundError` | **NOT VENDORED** — same, and it reconciles corporate actions, not a book |
| `pandas.resample('5min').ohlc()` | installed, works | **REUSED as a component** for the volume-increment arithmetic only. It converts; it does not decide |

**Nothing found decides admissibility from the comparison**, which is the part that changes
behaviour here. Everything found either builds bars from ticks or checks a bar against itself.
Built rather than vendored, with the arithmetic kept in `pandas`/stdlib.

## SOTA analog (`R.23a`)

Zipline's `adjustments` reconciliation and NautilusTrader's `BarAggregator` consistency checks
both compare a derived bar series against the tick stream it came from. Neither publishes a
per-instrument admissibility verdict from the comparison; that is this engine's addition, and it
exists because this project's fills are produced from the tape rather than from the bars.
