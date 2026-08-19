# 266 · `L4.27` — folding the live tape into bars so the regime panel and the strategy share a clock

**Spec before code (`R.23c`). Idea-intake verdict: ① ALREADY EXISTS** — plan entry **`L4.27`**
("Streaming incremental indicator state — O(1) per tick, never recomputed from history. Required for
any full-universe scan"), todo **3.27**, status `spec`. Nothing added to the plan.

Closes **`B40`**.

---

## 1 · The defect this fixes, and it is a clock mismatch rather than a missing feature

`ContinuousPaperTradingScheduler` runs live and records **0 proposals on every tick** — visible on
`/loop` across fifteen real iterations on 2026-08-18. That is correct behaviour and it is not what
anyone wants.

The cash bot's regime veto needs a `RegimeBelief`. The regime panel
(`TrendStrengthRegimeClassifier`, `VolatilityRegimeClassifier`, `SessionPhaseRegimeClassifier`) is
fitted on **five-minute bars**. The loop ticks on the **tape**. Handing a bar-fitted classifier a
tape-derived observation is the `A.106` defect — a calibration looked up under a coordinate measured
a different way — so the loop currently passes a maximum-entropy belief, the veto abstains, and
nothing is proposed.

**So the fix is not "make the bot less strict". It is to give the panel the bars it was fitted on**,
built from the ticks that are already arriving.

## 2 · What already exists, and is deliberately NOT rebuilt

| piece | where | state |
|---|---|---|
| bar → classifier opinions | `InstrumentSignalState.observe(AvailableBar)` / `.opinions(at)` | built, used by `F04` |
| opinions → belief | `SoftRegimeWeightingBrain.combine(...)` | built, reliability-weighted |
| belief → decision | `IntradayMeanReversionEngine.decide(belief, at)` | built, real-data passed |
| live ticks on disk | depth tape, 7.1 M ticks/1,845 tokens on 2026-08-18 | flowing |
| **ticks → bars** | **nothing** | **this document** |

The whole chain exists except one link. Building anything else here would be duplicating a verified
path, which is how two answers to one question get created.

## 3 · The aggregator

**O(1) per tick and no history retained** — that is `L4.27`'s actual requirement, and it is a
constraint rather than an aspiration: a full-universe scan over 1,845 instruments that recomputed a
bar from stored ticks would re-read the tape once per instrument per tick.

```
for each tick (token, exchange_time, last_price):
    bucket = floor(exchange_time to the bar interval)
    if bucket > the open bar's bucket:  emit the open bar, start a new one
    else:                                update high/low/close/volume in place
```

State per instrument is **five numbers and a timestamp** — open, high, low, close, volume, bucket.
Not a list of ticks.

**Bars are emitted only when CLOSED.** A bar covering the current instant is not knowable yet, and
handing the panel a partial bar is the same look-ahead the point-in-time guard exists to stop —
`availability_time = bar_timestamp + interval`, exactly the convention `L0.37` already writes into
the bar store. The in-progress bar is readable separately and clearly named, so a consumer that
wants it has to ask for it.

## 4 · What it does NOT do

- **No indicators.** The panel computes those; this emits bars.
- **No storage.** The bar store (`L0.03`) owns persistence. This is in-memory state on the decision
  path, which is why it must be O(1).
- **No gap filling.** An instrument that did not trade in a bucket emits no bar for that bucket.
  Inventing a flat bar would feed the volatility classifier a zero-range observation that the market
  never produced — and it would do it most often on exactly the illiquid names where the classifier
  is already weakest.

## 5 · Signatures

```python
@dataclass(frozen=True, slots=True)
class CompletedBar:
    instrument_token: int
    bar_timestamp: datetime        # the bucket's START
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    @property
    def availability_time(self) -> datetime   # bucket start + interval; knowable only then

class LiveTickToBarAggregator:
    def __init__(self, *, bar_interval: timedelta)
    def observe(self, *, instrument_token: int, exchange_time: datetime,
                last_price_paise: Decimal, volume: int = 0) -> CompletedBar | None
    def drain_completed(self) -> tuple[CompletedBar, ...]
    def force_close(self, at: datetime) -> tuple[CompletedBar, ...]   # session close only
    @property
    def instruments_tracked(self) -> int
```

`observe` returns the bar that the arriving tick CLOSED, or `None`. That shape is deliberate: the
caller learns about a completed bar at the moment it completes, without polling.

`force_close` exists for one reason — the session close. `R.01` squares off at 15:30 and the final
partial bar would otherwise never be emitted, so the strategy would never see the last minutes of
the day it just traded.

## 6 · Acceptance criteria

1. **Bucketing is correct on the real session boundaries** — a 09:15:00 tick and a 09:19:59 tick
   land in the same bar; 09:20:00 starts a new one.
2. **A bar is emitted only once, and only when closed.** No partial bar reaches a consumer.
3. **`availability_time` is bucket start + interval**, matching `L0.37`, so a bar cannot be consumed
   before it was knowable.
4. **O(1) memory per instrument** — asserted by feeding thousands of ticks to one instrument and
   checking the retained state does not grow.
5. **A silent instrument produces no bar**, rather than a fabricated flat one.
6. **`R.05` LIVE:** run against the real tape during an open market and report bars produced, the
   belief the panel then forms, and whether the cash bot's veto lifts — including if it does not.

## 7 · Sourcing (`R.17`)

**Probed on PyPI, not recalled.** The sourcing gate has now caught me writing this section from
memory three times (`O.131`, and twice since), so the raw output is pasted rather than summarised:

```
arctic              1.82.2      tardis-dev          4.3.0       cryptofeed          2.5.0
lightweight-charts  2.1         bartender           2.4.11      ohlcv               0.1.0
tick2bar            No matching distribution        pandas-ta   No matching distribution
trade-aggregation   No matching distribution        pandas      2.3.3  <- already a dependency
```

Two of my guessed names were **wrong about what the package is**, which is exactly why guessing is
not sourcing: `bartender` 2.4.11 is *"Beergarden Backend"*, nothing to do with bars, and `ohlcv`
0.1.0 downloads but ships **no summary at all** at version 0.1.0 — an unmaintained stub rather than
a library. Neither is usable and neither would have been caught by reading names.

- **`pandas` 2.3.3 `.resample()` — already a dependency, and REJECTED on a mechanical ground rather
  than preference.** `resample` is a batch operation over a materialised frame; the requirement here
  is O(1) per tick with no history retained. Using it would mean holding every tick for 1,845
  instruments in order to recompute bars that are already known incrementally. It stays the right
  tool for the batch backfill path.
- **`arctic` / `tardis-dev` / `cryptofeed` are real and maintained**, and all three solve a
  different problem: `arctic` is a tick STORE, and the other two are crypto-venue feed handlers with
  their own connectivity layers. This project already has the store (the depth tape) and the feed
  (the capture). None offers a plain in-memory OHLCV bucketer as a usable seam.

**Built here.** The whole thing is a bucket index plus five running numbers, and any vendored
alternative would still have to be adapted to `AvailableBar` — the shape the already-verified
downstream path consumes.

## 8 · Open after this lands

- The panel needs enough bars to be mature; on a fresh process start that is a warm-up, and the
  honest options are to seed from the stored 5-minute bars or to wait. Seeding is preferred and is
  called out here so it is not discovered as a surprise.
- Live ORDER placement remains `B41` / `A.108`.
