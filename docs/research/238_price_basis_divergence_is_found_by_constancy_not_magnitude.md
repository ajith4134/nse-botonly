# 238 · The blast radius of `M26`, and why bhavcopy cannot measure it

**Measured 2026-08-15, following `A.120`/`docs/research/237`. Operator delegated the `M26` design
call (2026-08-15): "choose the best option, including all of them or one not listed."** This is
what the measurement said before any of them were built.

> MEASUREMENT report, not a design doc. The `R.16`/`R.17` sourcing pass for this engine is recorded
> in `docs/research/236`. The one design decision taken here — the constant-ratio classifier — is
> arithmetic over data this project already holds (dispersion of a ratio), with no external
> component to source; the statistical machinery it uses (`math.comb`, `statistics.median`) is
> stdlib and already in use.

---

## The plan, and why it was wrong

`M26` found that `HINDPETRO`'s bar series is retro-adjusted (constant ratio 0.95099) while the
depth tape holds the traded price. The obvious next question is **how many instruments and days
are affected**, and the obvious way to answer it looked like a full-history sweep against NSE
bhavcopy: bhavcopy is the exchange's own end-of-day record, is already ingested (88,241 rows,
2020-01-02 .. 2026-08-14), is independent of Kite, and covers **20 of the bar store's 34 days** —
against the depth tape's three.

That sweep was run. It compares the last 5-minute bar close of each `(instrument, session)` against
bhavcopy's `ClsPric`, over **21,752 pairs**.

## What the sweep actually measures

| quantile of \|last 5m close / official close − 1\| | value |
|---|---|
| p50 | **0.36%** |
| p90 | 1.72% |
| p99 | **4.75%** |
| p99.9 | 10.57% |
| max | 21.59% |

**`HINDPETRO`'s 4.9% adjustment sits at roughly the 99th percentile of ordinary noise.** It is not
separable from it on a single day.

The reason is that the two quantities are not the same quantity. A five-minute bar's close is its
last trade before 15:30; NSE's official close is computed from the closing session that follows it.
They differ for every liquid scrip, every day, and the difference is often larger than a dividend
adjustment. Screening on magnitude against bhavcopy therefore produces a flood: **2,220 instruments**
showed a gap above 0.5%, of which the overwhelming majority are ordinary closing-auction drift.

Worse, `0.5%` was a number I chose, which is exactly the hardcoded threshold `R.03` forbids — and
the moment it was replaced by the distribution's own quantiles the screen stopped separating
anything.

## What actually discriminates: constancy, not magnitude

`HINDPETRO` was caught by the depth-tape comparison because that comparison puts the **same quantity
at the same instant** on both sides — the last traded price — where the noise floor is not merely
small but **zero**: all nine deciles of the deviation, in units of each instrument's own spread,
measured 0.00 across 11,072 instrument-sessions (`docs/research/237`).

Against that floor the signature is unmistakable and it is not the size of the gap:

- **a price-basis divergence** holds the SAME ratio across every disagreeing bar — 0.95099 on 150
  of 150 bars, to five decimal places, across three sessions;
- **sporadic disagreement** produces ratios that scatter.

Applying that test BY HAND to the 187 refused instrument-sessions separated them cleanly:
**2 instruments** with a constant ratio (`HINDPETRO` 0.95099, `XCHANGING` 0.96958) against **183**
sporadic.

> **Corrected 2026-08-15, same day, after building it.** The sentence that stood here — "the
> discriminator is the DISPERSION of the ratio, and it needs no threshold beyond the tape's own
> spread" — was written from the by-hand analysis and is wrong as stated. A dispersion bound does
> not work; it took three corrections to find a rule that reproduces the by-hand answer, and the
> working rule is a robust FIT restricted to out-of-book closes, not a dispersion test. The
> by-hand numbers above stand; the claim about what makes them separable did not. See "The
> classifier took three corrections" at the end of this document.

## Conclusions, and what changes

1. **Bhavcopy is an adjudicator, not a detector.** It settled which store was wrong once the tape
   comparison found the disagreement — `ClsPric=390.00` agreeing with the tape's 39,000 paise
   against the bar store's 37,090 — and that is decisive and valuable. As a screening instrument it
   is blind at the magnitude that matters. It keeps the adjudicating role and loses the screening
   one it was never given.
2. **The blast radius cannot presently be measured beyond the depth tape's three sessions**, and
   saying otherwise from a bhavcopy sweep would be reporting noise as coverage. That is a real
   limit, recorded rather than glossed: 31 of the bar store's 34 days have no instrument capable of
   detecting this defect. The tape is the only instrument, and it began on 2026-08-11.
3. **The classifier belongs in the engine.** The constant-versus-scattered test was run by hand
   here and by hand in `docs/research/237`; running a discriminator by hand twice is the signal that
   it should not be by hand. It becomes a verdict the join engine emits, so a future refusal arrives
   already labelled as a basis divergence or as noise.
4. **The `M26` fixes that survive this measurement:** record the price basis on the bar store so an
   adjusted series is distinguishable from a traded one, and backfill same-day so the gap that
   causes the retro-adjustment does not open. Neither depends on a full-history sweep, and the sweep
   cannot support either.

## What this does NOT establish

Whether 2 instruments is the true count or merely the count the three recorded sessions could see.
The defect is a property of the GAP between a session and its backfill; the tape's three sessions
were backfilled two to four days later, which is a short gap. A longer gap should produce more, and
the only way to know is to keep capturing. That is an argument for `A.119`'s scheduled capture
rather than for another sweep.

---

## The classifier took three corrections, and each was found by real data

Recorded because the failures are more instructive than the final rule, and because `R.21` caps
this at three: the classifier is now reported as-is rather than refined a fourth time.

**Attempt 1 — bound the ratio's dispersion by paise rounding.** Reasoning: both sides are integer
paise, so a truly constant factor can only wobble by that rounding. Wrong by a factor of six.
`HINDPETRO`'s ratio disperses 3.36e-4 against a rounding width of 5.05e-5, because a bar's close
and the tape's aligned last price **are not the same trade** — they are two ticks a few seconds
apart, and the tick-level difference dwarfs the rounding. The rule also admitted 89 instruments
whose ratio was exactly 1.0: their prices agree to the paise and they were refused on the BOOK
bracket, so "perfectly constant rescaling by one" was technically true and completely useless.

**Attempt 2 — fit a factor, require every bar's residual within its own derived tolerance.** Right
shape, wrong statistic. `HINDPETRO` fits at a median residual of **0.25 tolerances** but has one
bar at **1.58**, so an all-bars test rejected the very case the engine was built for. The fix is
consistency: the factor is fitted robustly (median ratio), so it must be judged robustly (median
residual). Measured separation at the median is wide — 0.25 for a true rescaling against **2.02**
for `HDFCBANK`'s genuine scatter — and narrow at the max.

**Attempt 3 — restrict the fit to closes that fell OUTSIDE the recorded book.** Token 82945 fitted
a factor of 0.99930 across seven bars, and every one of them read
`bar_close == best_bid, tape_last == best_ask`. A stable ratio, and not a defect: the two sources
sampled opposite sides of the same book. A rescaled series puts the close somewhere the book never
was, which is exactly what `CLOSE_OUTSIDE_RECORDED_BOOK` already identifies, so the restriction
costs no new threshold. On 2026-08-12 it cut the candidates from **89 to 2**.

## Where it landed — 187 refusals to 4 candidates, and the two real ones are unmistakable

Final run, all three sessions, verdicts as stored:

| Session | Instrument | Factor | Bars |
|---|---|---|---|
| 2026-08-11 | **`HINDPETRO`** | 0.95099 | **64 / 64 — 100%** |
| 2026-08-11 | **`XCHANGING`** | 0.96958 | **64 / 64 — 100%** |
| 2026-08-11 | `OIL` | 1.00053 | 7 / 63 — 11% |
| 2026-08-12 | **`HINDPETRO`** | 0.95099 | **57 / 57 — 100%** |
| 2026-08-12 | `PANAMAPET` | 0.99888 | 6 / 58 — 10% |
| 2026-08-13 | **`HINDPETRO`** | 0.95100 | **29 / 29 — 100%** |

**The separation is total, and it is visible without any further rule.** The two real defects
rescale EVERY comparable bar, by 4.9% and 3.0%. The two borderline candidates touch a tenth of
their bars at a factor within 0.12% of one — which is what a handful of stale or oddly-priced
closes looks like, not what a rescaled series looks like.

**A fourth rule was NOT added, deliberately.** Requiring 100% of bars would be defensible — a
series is either denominated on a basis or it is not, so a partial rescaling is close to a
contradiction in terms — but `R.21` caps this at three corrections and the counts are already on
the surface: the runner prints `on N/M bars` and the store keeps both numbers, so `64/64` and
`7/63` are distinguishable by anyone reading them. A rule tuned until exactly the two instruments
I already believed in survived would be fitting the classifier to its own test set.

`OIL` and `PANAMAPET` are therefore **candidates, not findings**. What settles them is the same
bhavcopy adjudication that settled `HINDPETRO` in `docs/research/237`, specced as part 3 of
`docs/research/239` and bounded by bhavcopy's 20-of-34-day coverage.

**What would settle `PANAMAPET`:** the same bhavcopy adjudication that settled `HINDPETRO`, run
per candidate rather than by hand. That is a small, well-defined addition and it is recorded as
open rather than built, because it needs bhavcopy coverage on the candidate's session and
2026-08-12 is one of only 20 days that has it.
