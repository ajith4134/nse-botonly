# `L0.33` — Consolidated multi-broker feed, specified as a FUSION of noisy estimators

*Spec, 2026-08-12, written after the capture was already running (`A.84`) and before the engine.
The plan entry asks for "one synthetic tape from several brokers" with a "liquidity-weighted
cross-check". The hard part is not merging quotes; it is knowing which broker to believe when they
disagree, which is a question about the BROKERS and can only be answered by carrying state about them
across sessions.*

## 1. The measurement, and why one number per broker is not enough

Measured live at 14:27 IST today, the same instrument, seconds apart: Kite quoted RELIANCE at
Rs 1,313.90 and Angel One at Rs 1,314.10. Twenty paise. Nothing in that pair of numbers says which is
right, and three different explanations produce it:

1. **Polling skew** — the two brokers were asked 200 ms apart and the price genuinely moved.
2. **Feed lag** — one broker's price is real but late, so it is a correct answer to an older question.
3. **A wrong quote** — one broker is serving a stale or erroneous book.

The engine must separate these, because the response differs: (1) is not a disagreement at all, (2) makes
that broker unsuitable as a timing reference but fine as a price reference, and (3) makes its rows
inadmissible. Every design decision below follows from needing to tell the three apart.

## 2. What the capture stores, and why it stores it unmerged

`scripts/record_cross_broker_quotes.py` (already running) writes one row per broker per instrument per
sweep into `cross_broker_quotes.sqlite3`, carrying `requested_at` and `received_at` — a BRACKET, not an
instant — plus the broker's own `exchange_time` where it supplies one, the touch, sizes, and totals.
Failures are stored as rows, because a broker that fails is telling the reliability model something.

Merging at capture time would destroy exactly the evidence the cross-check needs: once two prices become
one, nothing downstream can tell a lagging broker from a wrong one.

**Bounded by the slowest broker, and recorded as such (`R.09`).** Angel One's `getMarketData` takes 50
instruments per request at ~1 request/second; Kite's `quote()` takes hundreds. A full-universe sweep
through Angel One would take minutes, by which time the two brokers are answering about different market
states and every comparison measures the sweep rather than the brokers. The capture therefore covers a
liquidity-ranked head plus a deliberate illiquid tail (67 instruments today); the ENGINE is
universe-agnostic and the capture bound is an open blocker in BACKLOG, not a silent narrowing.

## 3. The algorithm — inverse-variance fusion, weighted by liquidity

Each broker is an estimator of one unobservable quantity: the instrument's true touch at an instant. The
statistically correct fusion of noisy estimators of the same quantity is **inverse-variance weighting** —
each source weighted by the precision of its own historical deviations — and the plan's requested
liquidity weight enters as the second factor, because a quote backed by 5 shares is worth less than the
identical quote backed by 5,000:

```
weight_b  =  (1 / variance_b)  x  liquidity_b
consensus =  sum_b (weight_b x price_b) / sum_b weight_b
```

`variance_b` is the variance of broker `b`'s signed deviation from the consensus of the OTHERS (never
from a consensus including itself, which would let a broker vote itself precise), estimated online and
carried across sessions. `liquidity_b` is the size at that broker's touch for that instrument.

**Alignment before comparison.** Two quotes are comparable only if their `[requested_at, received_at]`
brackets can refer to the same market state. The tolerance is derived from the measured round-trip
distribution of the sweep itself — not a constant — and a pair outside it is recorded as
`NOT_COMPARABLE` rather than as disagreement. `L0.32`'s bracketed host clock error is subtracted first,
which is what makes the two brokers' host stamps comparable at all.

**Four views over the fused state, all published (`A.84`).**

| view | what it answers | who wants it |
|---|---|---|
| `liquidity_weighted_consensus` | "one price, best estimate" | analytics, the dashboard |
| `synthetic_nbbo` | "best bid and best ask actually reachable" | a future router; carries a CROSSED flag when one broker is stale |
| `robust_median_or_refusal` | "a price I can rely on, or nothing" | the admissibility gate |
| `broker_ranking` | "who should I have asked" | the execution path (queued consumer) |

**Refusal is a first-class output.** When the dispersion across brokers exceeds what tick size, spread and
measured latency can explain, the engine publishes `UNRESOLVED` for that instrument-instant rather than a
consensus — the same discipline `L0.31` applies to rule eras and `L0.32` to timestamps. The threshold is
derived from the instrument's own measured dispersion, not chosen.

## 4. Carried state — the part that makes it an engine rather than a comparison

`BrokerReliabilityStore` holds, per broker and per instrument where the data supports it:

- deviation mean (bias) and variance (precision), updated online, decayed so a broker that improves is
  not judged forever on its worst week;
- availability: polls attempted, polls failed;
- freshness: distribution of `received_at - exchange_time`, clock-corrected via `L0.32`;
- staleness: how often this broker's quote was unchanged while the others moved — the direct measure of
  a lagging feed, and the one that distinguishes explanation (2) from (3) in §1;
- a maturity flag per broker (`R.04`): before enough observations, the weight falls back to liquidity
  alone and the engine says so, rather than trusting a variance estimated from ten samples.

## 5. What changes today (`R.06`), and what is queued (`R.11`)

**Wired now — the admissibility gate.** The depth tape is recorded from ONE broker (Kite). Where this
engine measures that broker as divergent or stale for an instrument-session, the recorded depth rows for
that instrument-session are marked inadmissible, and the microstructure replay refuses to build features
from them. That is a real behaviour change against data that already exists.

**Built in full, consumer queued — the routing signal.** `broker_ranking` produces, per instrument, the
broker whose touch was measurably best. Its consumer is the live execution path, which does not exist
(`L3.x`). Recorded, not pretended.

## 6. Signatures (`R.23(c)` step 2)

```python
@dataclass(frozen=True, slots=True)
class AlignedQuoteGroup:
    trading_symbol: str
    at: datetime
    observations: tuple[BrokerQuoteObservation, ...]
    not_comparable: tuple[str, ...]        # brokers excluded, by name, with the reason kept

@dataclass(frozen=True, slots=True)
class ConsolidatedQuote:
    trading_symbol: str
    at: datetime
    consensus_paise: float | None
    synthetic_best_bid_paise: int | None
    synthetic_best_ask_paise: int | None
    is_crossed: bool
    robust_median_paise: float | None
    resolution: FeedResolution            # RESOLVED | UNRESOLVED | IMMATURE | SINGLE_SOURCE
    dispersion_paise: float
    contributing_brokers: tuple[str, ...]
    reason: str

class ConsolidatedFeedEngine:
    def align(self, observations: Sequence[BrokerQuoteObservation]) -> list[AlignedQuoteGroup]: ...
    def consolidate(self, group: AlignedQuoteGroup) -> ConsolidatedQuote: ...
    def rank_brokers(self, session_date: date) -> tuple[BrokerRanking, ...]: ...
    def admissibility(self, session_date: date) -> Mapping[tuple[str, str], bool]: ...
```

Error behaviour: a group with one usable broker returns `SINGLE_SOURCE` (never a consensus of one); a
broker with an immature reliability estimate contributes on liquidity alone and the reason says so; an
instrument whose brackets never overlap returns `UNRESOLVED` with `not_comparable` populated.

## 7. Acceptance criteria — checked, not claimed

1. Runs on the REAL captured tape from 2026-08-12 and produces consensus, NBBO, median and ranking.
2. Property: with synthetic brokers whose noise variances are known, the fitted weights recover the
   variance ordering, and the consensus beats every individual broker's RMS error against the truth.
3. Adversarial: one broker frozen (repeating a stale quote) is detected as stale, down-weighted, and
   excluded from the NBBO rather than crossing it.
4. A two-broker disagreement beyond tolerance returns UNRESOLVED, never an average.
5. The admissibility gate demonstrably changes which depth rows the microstructure replay accepts.
6. `/feed` renders from the real store; ruff + mypy clean; adversarial review; R.05 pass recorded.

## 8. Out of scope, recorded so it is not mistaken for an omission

- **No order routing execution** — the ranking is produced, the routing decision belongs to `L3.x`.
- **No websocket consolidation.** Both brokers offer streaming; the poll-based capture is what two
  brokers' rate limits allow today and is enough to measure disagreement. A streaming capture is a
  separate slice and is what full-universe coverage will need.
- **No cross-EXCHANGE consolidation** (NSE vs BSE). Different books, different prices, not a disagreement.


## 9. Sourcing pass — run, with mechanical evidence (`R.16`, `R.17`)

Throwaway venv, signatures read with `inspect.signature`, every candidate installed and run.

| part | candidate | evidence | verdict |
|---|---|---|---|
| consolidated tape / NBBO | `nautilus_trader` 1.231.0 | stable release does not install here (no `manylinux_2_35` wheel against glibc 2.34; sdist needs a Rust build that exits 101). Only the `2.0.0rc2` prerelease installs. `model.OrderBook` is a SINGLE-venue book: `best_bid_price`, `midpoint`, keyed by one `instrument_id`; no `CBBO`/`NBBO`/`consolidat*` symbol exists in any submodule | **REJECT** — single-venue book, not a consolidator |
| consolidated tape / NBBO | `cryptofeed.nbbo.NBBO` 2.5.0 | installs; the class is 40 lines computing `max(bid)`/`min(ask)` across exchanges, wired into cryptofeed's async callback objects | **ORACLE-ONLY** — the right algorithm, unusable shape; read as a reference |
| consolidated tape / NBBO | `ccxt` 4.5.73 | `fetch_tickers(symbols=None, params={})` returns many symbols for ONE exchange; no cross-venue fusion | **REJECT — irrelevant** |
| consolidated tape / NBBO | `nbbo`, `market-data-aggregator`, … | no such distribution on PyPI | **NONE EXISTS — build in-house** |
| inverse-variance pooling | **`statsmodels.stats.meta_analysis`** 0.14.6 | already installed; `combine_effects(effect, variance, method_re='iterated', …)`; ran on three sources → fixed-effect mean 100.012857, se 0.0756 | **ADOPT** (batch; recomputed per group, not streamed) |
| inverse-variance pooling | `pymare` 0.0.10 | installs; `WeightedLeastSquares.fit(y, X, v=None)` reproduced the identical estimate | **ORACLE-ONLY** — same math, staler repo, heavier API |
| inverse-variance pooling | `PythonMeta` 1.26 | no public source repository; last release 2021 | **REJECT** — unauditable |
| online moments | **`river.stats`** 0.25.0 | `Mean()`, `Var(ddof=1)`, `EWMean(fading_factor=0.5)`, `EWVar(...)` all import and run; `RollingMean`/`RollingVar` do NOT exist in 0.25.0 — `river.utils.Rolling(Var(), window_size=…)` is the replacement | **ADOPT** the recursion; the persisted state is written out here because a store must own its own state format |
| robust dispersion | **`scipy.stats`** 1.18.0 | `median_abs_deviation(x, axis=0, center=None, scale=1.0, …)`, `trim_mean`, `iqr` verified on a 5-source vector with one stale outlier | **ADOPT** |
| robust dispersion | `robustats` | Cython build fails on this Python 3.12 / aarch64 | **REJECT** — does not build |
| robust dispersion | `hampel` 1.0.2 | rolling-window time-series filter; needs a series, not a 2-5 value cross-section | **REJECT** — wrong shape |

## 10. What the first real session measured (2026-08-12)

Kite and Angel One, 67 instruments addressable by both, polled every ~2 s from 14:32 IST.

| quantity | value |
|---|---|
| aligned groups consolidated | 52,441 (10.4 s to walk) |
| resolved to one price | 98.66% |
| crossed books (one source stale) | 603 |
| single-source groups | 101 |
| exact midpoint agreement | **92.5%** of 45,286 paired instants |
| median absolute midpoint difference | **0 paise**; p95 5 paise; max 27.50 rupees |
| frozen-while-others-moved | Kite 4.58%, **Angel One 6.22%** |
| instruments where each broker scored best | Kite 52/67, Angel One 17/67 |
| per-source noise variance | **unidentifiable** — two feeds, as designed |

The brokers agree exactly nine times in ten, and the tail is where the engine earns its
keep: 603 crossed books and a 27-rupee maximum disagreement are not noise, they are one
source being stale on a specific instrument at a specific instant, and the gate below is
what stops that from becoming a microstructure feature.

## 11. The identifiability limit, found by a property test

The first fusion weighted each broker by the variance of its deviation from the others. With
TWO brokers that quantity is `var(a - b)` for both — one number, shared — so the weighting
could not discriminate, and the property test measured the consequence: the "consensus" had a
larger RMS error than the better broker. Replaced with the **three-cornered hat** (Gray &
Allan 1974): with three or more independent sources the pairwise difference variances
determine each source's own. With two, the engine reports `unidentifiable` and weights by
liquidity alone.

Two further measurements shaped it:

- **The decomposition resolves the NOISY source, and floors the quiet ones.** The error on a
  solved variance is of order the largest pair variance times `sqrt(2/n)`, so a source much
  quieter than that is below resolution. A negative solution is therefore floored at that
  resolution rather than dropped (which would discard the best broker) or clamped to zero
  (which would make it infinitely precise and hand it the whole consensus).
- **Pairwise state is stored in BASIS POINTS.** Pooled in paise across a universe spanning
  three orders of magnitude in price, the expensive names dominated the variance and the
  estimate was unusable for the cheap ones.


## 12. Adversarial review round (`R.23(c)` step 5) — eleven defects, all reproduced

Full account in plan entry `A.85`. The design consequences that belong here:

**§3's dispersion tolerance was unreachable and backwards.** Two times the WIDEST book meant an uncrossed
pair could never exceed it (797 of 797 real refusals came from the crossed test), and it let the stalest
broker set the bar. It now comes from the NARROWEST book.

**§5's admissibility rule was a quantile, which cannot express "how much".** It condemned the worst
quarter of every session regardless: 33 instrument-sessions on a day whose worst instrument diverged on
1.15% of comparisons. It is now a one-sided binomial test against the session's own base rate — measured
after the change: base 1.21%, 26 condemned, all diverging at 3.4-5.0%.

**§4's frozen measurement was wrong for a third of the tape** because the previous price was stored
rounded and compared unrounded, so odd-spread midpoints never compared equal. This is the measurement
that separates a lagging feed from a wrong one, so it was the most load-bearing number in the engine.

**Alignment now closes a window on a repeat instead of deleting it**, and a failed poll can never evict
the same broker's good quote — 118 real observations were being lost, which was also the sole source of
every single-source group the engine reported.

**A source below the estimator's resolution is `unidentifiable`, not a floored number.** Mixing a floor
(a statement about the estimator) with a measurement (a statement about the broker) inverted an ordering
when maturity masking left different triplets available to different sources.

**One malformed group can no longer cost a session's learning:** the observation boundary refuses naive
timestamps, and the session runner counts a failed group rather than letting it escape and discard the
uncommitted transaction holding tens of thousands of comparisons.


## 13. What the third broker changed (2026-08-12, `A.86`/`A.87`)

Upstox joined as the third feed the same afternoon (the blocker was an untested credential, not an
expired one — `A.86`). Two things followed immediately.

**Per-source noise became identifiable.** `angel_one` 0.074 bps², `kite` 0.342 bps², `upstox` below the
estimator's resolution and reported as `unidentifiable` rather than floored to a number. §11's
identifiability limit is closed for two of three sources.

**A data defect two brokers had hidden.** Crossed synthetic touches went from 1.2% to 10-44%, which
prompted an investigation rather than an acceptance, and the cause was NSE's ±3% dynamic price band: on
25,761 rows (10.07% of Kite's and 10.07% of Angel One's — the identical rate is what proved it was the
data) the top of book is a band order at last +3.03% / −2.95% with 40,407 shares against a normal 281.
`BrokerQuoteObservation.has_valid_book` now rejects a quote whose bid exceeds its own ask — impossible by
construction, so no threshold is involved — while keeping its last traded price, which is a real print.

**Measured on the same tape:** resolved 86.29% → **95.97%**, refusals 13,552 → **332**, crossed 14,478 →
1,229, inadmissible instrument-sessions 24 → 11.

**And a softened claim.** "Crossed implies one book is stale" was too strong even before the band
artefact: two brokers polled ~100 ms apart in a moving stock cross legitimately. A cross is now evidence
of staleness only beyond what the books and the instrument's own p99 dispersion explain.
