# Research/214 — `L0.22` spec: order-book snapshot replay and microstructure inference

**R.23(c) step 1.** Spec before signatures, signatures before tests, tests before implementation.
Companion to `70` (the recorder design), `71`/`72` (why record-forward is the only path), `206` (the
tape row schema), `208` (the storage sourcing run). This file is the WHAT and the WHY; the acceptance
criteria at the end are what "done" is checked against, and they are checked, not self-reported.

---

## 0. The honesty constraint that shapes everything below

The plan entry says it plainly: **`L0.22` is retail-infeasible as literally named.** "Tick-level order-book
reconstruction" means rebuilding the book from order-by-order messages — add, modify, cancel, trade, each
with an order id — which is exactly what NSE sells for ₹12.5 lakh/year and gates behind an institutional
licence (`research/72` §1, rows 2 and 5). What this project has is Kite's **5-level snapshot** feed at a
measured p10 inter-packet gap of 0.25s.

So the engine is named for what it is: **`order_book_snapshot_replay_engine`**, not a reconstructor.
Per `R.23(b)`, vocabulary sets scope — and per `R.14`, a name that overclaims is a defect. What the
snapshot tape *can* support is a genuine inference problem, and the literature that solves it was built
for exactly this data:

| Impossible from 5-level snapshots | Possible, and what supplies it |
|---|---|
| Queue position of a specific order | Aggregate queue depletion at the touch, between snapshots |
| Individual add/cancel/modify events | **Order Flow Imbalance** (Cont–Kukanov–Stoikov 2014), which is *defined* on L2 snapshot deltas |
| Exact trade prints and their aggressor | Signed volume inferred from `volume_traded` deltas + the tick rule (Lee–Ready 1991 fallback) |
| Book beyond level 5 | `total_buy_quantity` / `total_sell_quantity`, which the feed carries for the whole book |
| Sub-250ms dynamics | Nothing. This is a hard ceiling and every consumer must be told. |

**Every output of this engine carries the fidelity ceiling with it**, as a field, not as a comment in a
doc nobody opens. A feature computed from two snapshots 4 seconds apart is not the same object as one
computed from two snapshots 0.25s apart, and a consumer that cannot tell them apart will silently
average them.

---

## 1. The difference test (`R.23(a)`)

| Requirement | How this engine meets it |
|---|---|
| **Inputs** | The real depth tape: Parquet partitions under `session_date=`/`capture_run=`/`shard=`, read through `MarketDepthTapeReader`. Not a fixture — the 2026-08-11 tape holds 9,000 instruments and 2026-08-12 is being written now. |
| **Solver / inference procedure** | Multi-level Order Flow Imbalance, signed-volume inference by the tick rule with a quote-rule fallback, micro-price, and queue-depletion decomposition. These are estimators of unobservable quantities from observable snapshots, not arithmetic on one row. |
| **State carried between decisions** | The previous book per instrument, cumulative OFI, cumulative signed volume, the last mid used by the tick rule, and the run of duplicate books. OFI is *undefined* without the previous book — the state is the algorithm. |
| **Verifiable output** | A per-instrument feature series with an explicit fidelity field, plus `book_at`-style point queries. Verifiable in three independent ways, listed in §5. |
| **SOTA analog** | Cont–Kukanov–Stoikov, *The Price Impact of Order Book Events* (2014) — the paper that established OFI is computable from L2 snapshots and explains price moves better than trade imbalance. Depth comparison, not an import (`R.23a`). |

---

## 2. What the engine computes, and the exact definition of each

Notation: at snapshot *n*, `P^b_n` and `q^b_n` are the best bid price and its quantity; `P^a_n`, `q^a_n`
the best ask. All prices are **integer paise** (`research/206`); no float ever enters a comparison.

### 2.1 Order Flow Imbalance (level 1)

The Cont–Kukanov–Stoikov event term between consecutive snapshots:

```
e_n =  q^b_n · 1{P^b_n ≥ P^b_{n-1}}  −  q^b_{n-1} · 1{P^b_n ≤ P^b_{n-1}}
     − q^a_n · 1{P^a_n ≤ P^a_{n-1}}  +  q^a_{n-1} · 1{P^a_n ≥ P^a_{n-1}}
```

Read it as: a bid that improved or held while growing is buying pressure; a bid that fell away is selling
pressure; and symmetrically for the ask. When both prices are unchanged the indicators collapse to
`q^b_n − q^b_{n-1} − q^a_n + q^a_{n-1}`, the pure size delta — which is the identity that makes this
computable from snapshots at all.

### 2.2 Multi-level OFI

The same term evaluated independently at each of the 5 levels, giving a vector `(e_n^1 … e_n^5)`. Deeper
levels move for different reasons than the touch, and collapsing them to a scalar before a consumer has
seen them is a decision this engine does not get to make. A depth-weighted scalar is *offered* as a
convenience, computed with weights derived from each level's own realised contribution (`R.03`: no
hand-chosen 0.5/0.3/0.2 ladder).

### 2.3 Signed volume, inferred

`volume_traded` is cumulative for the session, so `Δvolume` between snapshots is real traded quantity.
Its sign is not observable and must be inferred:

1. **Quote rule first** — a trade above the prior MIDPOINT is buyer-initiated, below it seller-initiated.
   *Corrected after the differential test in §6*: the first implementation compared against the touch
   prices instead, so every trade inside the spread fell through to the weaker tick rule. The comparison
   is `2·price` against `bid + ask` so a half-paise midpoint stays exact.
2. **Tick rule as fallback** — compare `last_price` to the previous different `last_price`. Up-tick is
   a buy, down-tick a sell, zero-tick inherits the last non-zero direction.
3. **Explicitly unclassified** — when neither applies (first packet of a session, no prior trade), the
   quantity is recorded as unsigned rather than assigned a coin-flip direction.

The proportion classified by each rule is reported per instrument. A series that is 80% tick-rule is a
weaker series than one that is 80% quote-rule, and the consumer is told which it has.

### 2.4 Micro-price

`(P^a·q^b + P^b·q^a) / (q^b + q^a)` — Stoikov's imbalance-weighted mid, which is a better predictor of
the next trade price than the arithmetic mid when the book is lopsided. Computed in paise with exact
integer arithmetic and a documented rounding rule.

### 2.5 Queue depletion at the touch

When `P^b` is unchanged between snapshots, the drop in `q^b` decomposes into cancellations and executions.
Executions are bounded by the inferred sell-side signed volume over the same interval; the residual is
cancellation. The decomposition is reported as an interval, not a point estimate, because the snapshot
gap makes it genuinely under-determined — and an honest interval is more useful than a false point.

### 2.6 Fidelity, attached to every row

Each emitted feature row carries: the gap in milliseconds to the previous snapshot, the integrity flags
of both snapshots, whether the previous book was a `DUPLICATE_OF_PREVIOUS_BOOK`, and the run length of
duplicates preceding it. `R.03` applies here too — there is no hardcoded "stale beyond N seconds"; the
threshold is derived from the instrument's own observed gap distribution, consistent with how the
recorder already derives `STALE_BEYOND_DERIVED_THRESHOLD`.

---

## 3. What it must refuse to do

- **Never order by `receipt_sequence` alone.** The counter restarts per feed; the 2026-08-11 tape has two
  runs with overlapping ranges 1..132,313 across 4,236 shared instruments. Order by `receipt_time`, break
  ties by sequence *within one capture run*. This is already established in `book_at`'s docstring and is
  a correctness trap, not a style preference.
- **Never compute a feature across a capture-run boundary without saying so.** Today's tape has two runs
  (`103035` and `104353`) with a 65-second hole between them. An OFI computed across that hole is not
  wrong — it is a different measurement, and it must be labelled.
- **Never silently fill a gap.** A missing interval is emitted as a gap row, not interpolated.
- **Never use `exchange_time` for ordering.** One-second resolution against a 0.25s p10 gap (`research/206`).

---

## 4. Signatures (`R.23(c)` step 2)

```python
@dataclass(frozen=True, slots=True)
class BookSnapshot:            # one row of the tape, typed
class MicrostructureFeatureRow # one emitted feature observation, with fidelity fields
class SignedVolumeAttribution  # quantity + rule used + confidence
class QueueDepletionInterval   # (min_executed, max_executed), cancellation residual

class OrderBookSnapshotReplayEngine:
    def __init__(self, tape_reader, *, session_date, staleness_quantile) -> None
    def replay_instrument(self, instrument_token) -> Iterator[MicrostructureFeatureRow]
    def book_at(self, instrument_token, as_of) -> BookSnapshot | None
    def session_feature_frame(self, instrument_token) -> pa.Table
    def coverage_report(self) -> InstrumentCoverageReport
```

Error behaviour: an instrument absent from the session raises `UnknownInstrumentError` rather than
yielding an empty iterator, because "no data" and "wrong token" are different bugs and an empty
iterator makes them identical.

---

## 5. Acceptance criteria — what is checked, not claimed

1. **Unit**: OFI matches hand-computed values for all nine (bid-direction × ask-direction) cases,
   including both degenerate ones where a side vanishes.
2. **Property (hypothesis)**: OFI is antisymmetric under swapping the bid and ask sides of both
   snapshots; signed volume magnitudes always sum to `Δvolume_traded`; micro-price always lies within
   `[best_bid, best_ask]`.
3. **Adversarial**: crossed books, zero-quantity levels, a level count below 5, epoch-0 exchange times,
   duplicate books, a capture-run boundary mid-window, and a session whose first packet is a duplicate.
4. **Real data (`R.05`)**: replay real instruments from the 2026-08-11 tape end to end, and assert the
   three invariants above hold on every row produced. Not a smoke test — the invariants are the test.
5. **Gate**: ruff + mypy clean, whole suite green.
6. **`R.06`**: a named consumer. The feature frame feeds `L1.05`/`L1.06` (fill and market-impact models),
   which is the next unblocked engine after this one, and the coverage report gets a dashboard surface
   per `R.08`.

## 6. OSS sourcing pass — run, not recalled (`R.16`, `R.17`)

Decomposed into five parts and searched PyPI + GitHub for each. Every verdict below rests
on a mechanical fact — a `pip install --dry-run` result, a fetched source file, a real
signature, a push date — never on a README claim. Environment checked first: nothing
microstructure-related was already installed (`backtrader`, `zipline-reloaded`,
`pandas_market_calendars` are the closest, and none touches L2/OFI/micro-price).

### A — Multi-level Order Flow Imbalance

| Candidate | Source | Freshness | Installs here? | Verdict |
|---|---|---|---|---|
| *(nothing on PyPI)* | — | — | — | No package for CKS-OFI exists at all |
| `akshai0296/Multi-Level-Order-Flow-Imbalance…` | GitHub | pushed 2025-01-07 | not packaged | **Reference** — real per-level `calculate_ofi()` over `bid_px_0N/bid_sz_0N` columns, our exact shape; no tests, single Colab export |
| `nicolezattarin/LOB-feature-analysis` | GitHub | pushed 2022-04-12 | not packaged | **Reject** — correct `e_i = ΔW_i − ΔV_i` logic but an argparse CLI wired to a LOBSTER CSV, a baked-in `tick_size=1e-4` float, and a per-row Python loop |
| `orderbooktools/crobat` | GitHub | pushed 2026-05-04 | source-only | **Reject** — its `compute_sign()` consumes `insertion`/`cancellation`/`market` EVENTS, i.e. an MBO stream we do not have |
| Dean Markwick blog | blog | 2022 | — | **Reject** — Julia, and BBO-only rather than multi-level |

**Built here.** The formula is four indicator terms; the value is in getting the integer-paise
handling and the absent-level semantics right, and no candidate offered either.

### B — Trade-side classification

| Candidate | Source | Freshness | Installs here? | Verdict |
|---|---|---|---|---|
| `tclf` | PyPI + GitHub, BSD-3 | GitHub pushed 2026-08-10; **PyPI 0.0.9 is from 2024-02-18** | PyPI build **fails at runtime**, git main works | **Adopted as a test oracle** |
| `jktis/Trade-Classification-Algorithms` | GitHub | pushed 2022-12-14 | **No** — never on PyPI, needs a local Cython build | **Reject** |

The `tclf` finding is worth stating precisely, because "it installs" was not the same as "it
works": `pip install tclf` succeeds and then `fit()` raises
`AttributeError: 'ClassicalClassifier' object has no attribute '_validate_data'` — the PyPI
artifact predates scikit-learn 1.6's removal of that method, and this host runs 1.9.0.
Installing from git main (0.3.0) fixes it. That is the difference between checking a version
number and running the thing.

**Not adopted as the classifier, and the reason matters.** `tclf` is sklearn/pandas
batch-shaped (`fit`/`predict` over a DataFrame); this engine classifies inside a streaming
replay that carries the last trade price and last direction forward. Wrapping a batch
estimator per transition would be slower and would still not hold the state. So it is used
where a second implementation is most valuable — as a **differential oracle in a property
test**, generating random books and asserting both agree.

**It paid for itself on the first run.** It found that this engine's quote rule compared
trades against the TOUCH rather than the MIDPOINT: `bid=1 ask=4 trade=2` is a sell under
Lee-Ready (below the 2.5 midpoint) and was "unclassified" here. Every trade inside the spread
was falling through to the weaker tick rule. Fixed, and the test now pins it.

### C — Micro-price

`sstoikov/microprice` (471 stars) is Stoikov's own reference code and is a **Jupyter notebook
plus two CSVs** — no module, no packaging, no tests, last pushed 2021-01-10. Nothing else
exists. The imbalance-weighted mid is one expression; it is written here directly, in integer
paise, with the crossed-book and empty-side cases the notebook does not consider.

### D / E — Feature libraries and snapshot-native replay engines

| Candidate | Installs here? | Verdict |
|---|---|---|
| `pymicrostructure` | yes | **Reject** — an agent-based market SIMULATOR (`traders/informed.py`, `orders/limit.py`); generates synthetic flow, does not featurise a recorded tape. No commits since its 2024 release |
| `fastlob` | yes | **Reject** — a matching engine you feed individual orders into |
| `order-book` (`bmoscon`) | yes | **Reject** — a sorted state container maintained from streaming diffs; its own docs say it computes no OFI, imbalance or micro-price |
| `mlfinlab` | **no** — `ERROR: Could not find a version that satisfies the requirement mlfinlab (from versions: none)` | **Reject** — no longer on public PyPI at all |
| `hftbacktest` | yes | **Reject** — its docs require "tick-by-tick full order book and trade feed data", i.e. MBO. Reported rather than dropped silently, per the constraint |
| `mansoor-mamnoon/limit-order-book` | **unverified** — no wheel, CMake/C++ build never attempted on aarch64 | **Surfaced for operator double-check** — its `analytics/` output already includes imbalance, micro-price and impact, so it is functionally close; it needs a dedicated aarch64 build pass before any vendoring decision |

**The negative result is itself the finding.** Every public "LOB replay" project reconstructs
from order-level or diff-level messages, because that is what raw exchange feeds give you. A
consumer that already holds materialised 5-level snapshot rows has no reconstruction problem
to solve — no insert/cancel/match logic — only iteration and inference. That is why this
engine is thin on plumbing and dense on estimators.

**Net: depend on nothing new in the hot path; `tclf` (git main) enters as a test-time oracle.**

## 7. Deliberately out of scope, recorded so it is not mistaken for an omission

- No queue-position model. It needs order-by-order data this project cannot get (`research/72`).
- No cross-instrument or index-level aggregation — that is a consumer's job, not the replay engine's.
- No live/streaming mode. The engine reads the tape; the recorder owns the socket.
