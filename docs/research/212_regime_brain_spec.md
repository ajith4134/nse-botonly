# 212 · The four-regime brain (`L11.01`–`L11.03`, `L11.06`) + its first consumer (`L5.05`)

Spec before code, per `R.23(c)`. This is the first **decision-path** work in the rebuilt system: every
engine here carries state, runs a real inference procedure, and produces an output that changes what
gets traded. Tiered verification (`A.56`) therefore puts all of it in the top tier — full loop,
adversarial review, mutation, real-data pass.

## 1 · Why this, and why now

`research/211` measured the failure: 100% of a day's work went into `L0` acquisition while 134 `L11`
entries — the reasoning and learning layer — sat untouched, on top of 659,990 bars and 3,481 closed
trades that were already on disk. This slice is the correction, and it is deliberately **vertical**:
a brain nobody consumes is an orphan (`R.06`), so the mean-reversion engine ships with it.

Two operator decisions already govern the shape and are not re-litigated here:

- **`A.08`** — build **all four** regime engines complete from day one; **arm one**. An unarmed engine
  means *abstain*, and arming later requires **no code change**. This is `Rule Q` applied: thin data
  never shrinks the algorithm, it only gates activation.
- **`A.07`** — the first armed strategy family is **intraday mean-reversion on cash equity**.

## 2 · The four regime engines

Four genuinely different inference procedures, not one idea in four costumes. Each answers "what kind
of market is this?" from a different mathematical direction, which is the point: their disagreement is
information, and §3 consumes it rather than averaging it away.

### `L11.01` Trend-strength classifier — deterministic, indicator-based
ADX (Wilder), the Choppiness Index, and Kaufman's Efficiency Ratio computed from real bars. Each maps
a price path onto "directional" vs "ranging". **State carried:** Wilder's smoothing is recursive, so
the classifier holds its smoothed true-range and directional-movement accumulators between bars rather
than recomputing a window each time — that is what makes it a streaming engine rather than a repeated
batch calculation.

### `L11.02` Markov-switching model — probabilistic, latent-state
A Gaussian HMM (`hmmlearn` 0.3.3, verified installed) fitted by Baum-Welch over bar returns, emitting
**filtered** `P(state | data up to now)` via the forward algorithm. Filtered, never smoothed:
smoothing conditions on the future and would be lookahead leakage straight into a live decision.
**State carried:** the fitted transition matrix and emission parameters persist, and the forward
recursion carries the belief vector bar to bar.

### `L11.06` Session-phase classifier — structural, time-of-day
NSE's intraday session is not homogeneous: the first and last half-hours behave differently from
midday, and mean-reversion in particular is known to fail into the close. Classifies each bar's
position in the session and the session's own character (trend day, range day, reversal day) from the
realised path.

### Volatility classifier — dispersion-based
Realised volatility against its own recent distribution, plus a high-vs-low dispersion state. This is
the fourth engine because volatility regime governs whether a mean-reversion edge survives costs at
all — a wide-spread, high-vol tape can carry a statistically real edge that is unprofitable net.

## 3 · `L11.03` Soft weighting, not hard switching

The brain does **not** pick a winner. Hard switching throws away the fact that regimes are ambiguous
precisely when it matters most — at transitions, which is exactly when a strategy is most likely to be
wrong. Instead it produces a **weight vector over regimes**, combining the four engines by:

1. each engine emitting a probability distribution over regimes (deterministic ones emit a degenerate
   or confidence-scaled distribution);
2. weighting each engine by its own **measured** reliability, learned from outcomes rather than
   assigned — an engine that has been right is trusted more, and with no history all are equal;
3. renormalising, and reporting the **disagreement** between engines as a first-class output.

High disagreement is not noise to be smoothed — it is the brain saying *I do not know*, and a
consumer is expected to size down or abstain on it. That is the whole argument for soft weighting.

**Arming.** Each engine carries an armed/unarmed flag. Unarmed engines still compute and still record
their opinions — that is how they earn their reliability estimate — but contribute **zero weight** to
the decision. Arming is a flag flip, never a code change (`A.08`).

## 4 · `L5.05` The consumer — intraday mean-reversion

Consumes the regime weight vector and produces a real decision: direction, conviction, and abstain.
Mean reversion is regime-conditional by nature — it pays in ranging markets and is exactly wrong in
trending ones — so the brain's output **changes the behaviour** rather than decorating it:

- ranging weight high → entries permitted at the derived deviation band;
- trending weight high → **abstain**, regardless of how extreme the deviation looks;
- disagreement high → abstain, because an unknown regime is not a tradeable one.

Every threshold derived (`R.03`): deviation bands from the instrument's own realised distribution,
never a typed-in sigma. Thin data gates **activation** via a maturity ladder, never the algorithm
(`R.04`).

## 5 · What it does NOT do, stated so it is not mistaken for done

- It does **not** size positions or model costs — `L1` is unbuilt, so the output is a decision and a
  conviction, not an order. `R.13`: correctness of a signal is not evidence of correctness of an
  allocation.
- It does **not** self-promote to live. `R.22` two-key rule stands.
- Reliability weighting starts uniform and only becomes meaningful as outcomes accrue — an honest
  open blocker (`R.11`), not a hidden one.

## 6 · Verification

Top tier per `A.56`: unit + property (probabilities sum to one, filtered beliefs never see the future,
soft weights never collapse to a hard switch) + adversarial (flat bars, gaps, single-bar history, all
engines disagreeing, all unarmed) + **`R.05` on the real 659,990-bar store** + fresh-subagent
adversarial review + mutation.
