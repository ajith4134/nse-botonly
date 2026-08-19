# 257 · `L13.29` — the decision-trace contract

**Idea-intake verdict: ALREADY EXISTS as `L13.29`**, todo 5.41. No new plan entry. `A.29` requires
it built **before any panel**, and `L13.30`–`L13.33` all consume it.

## 1. What the plan asks for, verbatim

> Every bot emits, at decision time, an append-only point-in-time record of: **inputs consulted (with
> provenance and timestamps) · candidate actions considered · gates evaluated and their verdicts ·
> chosen action · confidence · mechanism invoked · the counterfactual that would have changed the
> decision.** This extends Rule R from "status is measured, never hand-authored" to **"reasoning is
> recorded, never reconstructed."**

And `A.29`'s binding constraint: *"a 'why did it trade?' panel assembled from trade records
afterwards is a confident fiction"* — it shows what a reasonable bot *might* have thought, not what
this one did.

## 2. Why this is an engine and not a log (`R.23a`)

A record of fields is a log. The clause that makes it an engine is the last one: **the counterfactual
that would have changed the decision.**

| `R.23a` requirement | What supplies it |
|---|---|
| Raw input pipeline | Gate evaluations, sized quantities and signals as they are produced at the decision instant |
| Real inference procedure | The **binding-constraint search**: which gate came closest to flipping, across gates whose margins are in different units, and what value would have flipped it |
| Carried state | An append-only point-in-time store, never updated after the instant |
| Output that changes behaviour | It is the evidence `L13.33` renders and the input to any "why did it not trade?" question; `L13.30`–`L13.32` are blocked on it |

**SOTA analog:** a solver's *sensitivity report* — the shadow price and the binding constraint,
which is what an LP solver returns alongside the optimum and is more useful than the optimum alone.

`L13.33` states the same thing in trading terms: *"which gate came closest to blocking it. The
near-miss is usually more informative than the pass."*

## 3. The problem the inference actually has to solve

Gates do not measure in the same units. Today:

| Gate | Margin unit | Already computed? |
|---|---|---|
| cost gate | basis points (`net_edge_bps`) | **yes** — signed: positive passed by, negative vetoed by |
| pre-trade risk gate | rupees of exposure | as tier verdicts |
| lot sizing | whole shares | as a quantity |
| rate limiter | grants per second | as granted/refused |
| control latch | boolean | as permitted/not |

"Which came closest" across bps, rupees and shares is meaningless as a raw comparison — 3 bps is
not smaller than 300 rupees. So each margin is **normalised by its own gate's threshold** into a
dimensionless fraction: `margin / |threshold|`. A gate that passed with 3 bps of headroom against a
30 bps hurdle is at `+0.10`; one that passed with ₹500 of headroom against a ₹50,000 limit is at
`+0.01` and is the tighter constraint, which is the answer a reader wants.

**A boolean gate has no margin and must not be given a fake one.** A halt latch is open or closed;
inventing `0.0` would make it permanently "the closest gate". Booleans report `None` and are ranked
separately — a closed boolean gate is *the* binding constraint by definition, and an open one is
never the near-miss.

## 4. The contract

```python
@dataclass(frozen=True)
class ConsultedInput:          # provenance and timestamp, per the plan clause
    name: str
    value: str
    source: str                # which store or engine produced it
    as_of: datetime            # the instant the VALUE was knowable, not when it was read

@dataclass(frozen=True)
class CandidateAction:
    action: str
    why_considered: str

@dataclass(frozen=True)
class GateEvaluation:
    gate: str
    verdict: str
    margin: Decimal | None     # signed, in the gate's own unit; None for a boolean gate
    threshold: Decimal | None  # what the margin is measured against
    detail: str

@dataclass(frozen=True)
class DecisionTrace:
    decided_at: datetime
    bot_identity: str
    instrument_token: int
    trading_symbol: str
    inputs: tuple[ConsultedInput, ...]
    candidates: tuple[CandidateAction, ...]
    gates: tuple[GateEvaluation, ...]
    chosen_action: str
    confidence: float | None
    mechanism: str
```

`DecisionTrace.binding_constraint()` returns the computed counterfactual: the gate that decided the
outcome, its normalised margin, and **the value it would have needed** — stated in the gate's own
unit, because "3.2 more basis points of edge" is actionable and "0.11 normalised" is not.

## 5. Refusals

- A trace with **no gates** is refused: a decision reached without evaluating anything is not a
  decision, and recording it would put an unexplainable row on the panel.
- A trace whose `chosen_action` is not among its `candidates` is refused — it would mean the record
  does not describe what happened, which is the exact fiction `A.29` forbids.
- An `as_of` **after** `decided_at` is refused: an input that was not knowable at the decision
  instant cannot have been consulted, and this is the look-ahead check applied to reasoning.
- A `confidence` outside `[0, 1]` is refused, as in `SegmentRelevance`.
- The store is **append-only**: it has no update path, and a trace for an existing
  `(bot, instant, instrument)` is refused rather than replaced. Reasoning is recorded once.

## 6. Verification

- **Unit** — every refusal fires; margins normalise correctly across units.
- **Property** (Hypothesis) — the binding constraint is always the gate with the smallest normalised
  margin among those of the deciding sign; scaling every threshold and margin by a constant does not
  change which gate binds.
- **Adversarial** — a boolean gate cannot become the near-miss by having no margin; a gate with a
  zero threshold cannot divide by zero; a trace cannot be rewritten after the fact.
- **Real data (`R.05`)** — emit traces from a real paper session over today's recorded tape and
  confirm the binding constraint for refused decisions matches the refusal reason the session
  independently reported. If the trace says the cost gate bound and the session said "refused by
  gate", they agree; if they disagree, the trace is fiction and the whole point is lost.

## 7. Sourcing (`R.17`)

No library is sourced. The contract is over this project's own types (`GateVerdict`, `PricedSignal`,
`ChargeableSegment`); the inference is a normalised argmin over a handful of gates, which is smaller
than any dependency that could supply it; the store is SQLite, as every other store here is. The
one genuinely external candidate class — OpenTelemetry-style tracing — was considered and is
wrong-shaped: it records *spans of execution* for latency and causality, whereas this records
*evidence and counterfactuals* for a decision, and its sampling model is the opposite of what an
append-only point-in-time record needs.
