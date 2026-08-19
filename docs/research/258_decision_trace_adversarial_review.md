# 258 · `L13.29` adversarial review — the contract held, my emission site emitted fiction

**Run 2026-08-17 in a fresh subagent, `R.23c` step 5.** Verdict: *"the contract holds up well in
isolation; the emission site makes it emit fiction. 100% of 21,270 real traces bind on the same
gate, and 380 of them name a gate that PASSED as the cause of an abstain."*

`A.29` exists to forbid exactly one thing — reasoning that is reconstructed rather than recorded, a
"confident fiction". I built the contract to prevent it and then wrote an emitter that produced it.

## CRITICAL

**C1 · The risk gate could never refuse.** `_emit_decision_trace` read
`str(getattr(verdict, "verdict", verdict))`. `RiskGateVerdict` has **no `verdict` attribute** — the
`getattr` default returned the dataclass and `str()` gave its full repr:

```
('pre_trade_risk_gate', "RiskGateVerdict(trading_symbol='725GS2063-GS', quantity=234,", margin=None)
GateEvaluation.refused on that string -> False
```

So the "a refusing boolean gate binds outright" branch — the most important in the whole inference —
was **dead code in production**.

**C2 · 380 real traces positively asserted the wrong cause.** Verbatim from the store:

```
symbol 96IIFL28A-NF   chosen: abstain
REAL cause : ORDER_RATE  ('5 order(s) already sent in the rate window against a limit of 5')
TRACE SAYS : gate='deviation_band' verdict='pass'
             "deviation_band was the binding constraint: 0.432875 of headroom left..."
```

**C3 · The counterfactual was degenerate.** All 21,270 traces bound on one gate; 57.1% had no
counterfactual at all (`band` absent during warm-up). The normalisation machinery — the thing that
makes this an engine — **had never once compared two gates in different units on real data**.

**C4 · Swallowed traces vanished silently.** No logger, no counter, in a 1,242-line module. Three
reachable loss paths were demonstrated, including an entry and an exit at the same instant where the
exit's trace was simply dropped.

## HIGH

**H1 · I defeated my own contract.** The emitter back-filled the chosen action into the candidate
set when it was missing, making the load-bearing "chose something it never considered" refusal
**unreachable at the only place traces are emitted**.

**H2 · `UNPRICEABLE` read as PASSING.** `refused` was a membership test over five lowercase words:
`REJECTED`, `rejected`, `vetoed`, `deny`, `FAIL` all read as passing, and so did
`GateVerdict.UNPRICEABLE`. `pre_trade_cost_gate`'s own docstring warns that treating "I do not know"
as "it is fine" is the failure it exists to prevent — reintroduced downstream, with an unpriceable
gate reported as binding with "0.1 of headroom left".

**H3 · A passing gate described as having "changed the outcome."** Nothing consulted
`chosen_action`, so when no gate refused but the decision was abstain, the passers were ranked
anyway and the winner asserted as the cause. This is the mechanism behind C2's 380.

**H4/H5 ·** margin sign incoherent with verdict produced backwards counterfactuals ("it needed 2
more" about a gate already 2 past its band); `if band` treated a band of exactly `0.0` as absent.

## MEDIUM

Zero threshold destroyed scale-invariance (×1000 changed which gate bound); a tiny threshold made
the tightest gate rank loosest; `NaN`/`Infinity` margins round-tripped and made the ranking
**order-dependent**; ties resolved silently by tuple order; the same instant in two timezones became
two contradictory rows; "append-only" was a primary key rather than an integrity property; look-ahead
was only upper-bounded; free text was never cross-checked.

## Mutation testing

14 of 15 killed in round 1 — the core inference is genuinely well tested. **9 survived**, including
one test that **passed for the wrong reason**: `match="candidate"` was satisfied by the *next*
refusal's message, so deleting the empty-candidates refusal was invisible.

## What was fixed, and the measured result

`GateOutcome` replaces the string test (`PASSED`/`REFUSED`/`NO_JUDGEMENT`, so "no judgement" is
never the binding constraint); finiteness and sign-coherence refused at construction; zero thresholds
refused; ties declared; the storage key normalised to UTC; a decision that consulted nothing refused;
the emitter reads `verdict.is_allowed`; the candidate back-fill deleted; NaN conviction no longer
clamped into a value the contract rejects; skipped traces counted.

**Re-run over the same real session:**

| | before | after |
|---|---|---|
| binding `deviation_band` | 21,270 (100%) | 20,886 |
| binding `pre_trade_risk_gate` | **0** | **380** |
| honestly UNEXPLAINED | 0 | **4** |

The 380 the reviewer identified as lies now name the gate that actually refused, and four traces say
plainly that they do not explain their outcome rather than blaming a passing gate. Tests 20 → 32.

## Still open, recorded not fixed

`B17` — the emitter supplies two gates; the cost gate's `net_edge_bps`, the sizer and the rate
limiter are not plumbed through, so the unit-normalisation still has little to compare.
`B18` — skips are counted but not yet surfaced on the session report.
`B19` — the remaining MEDIUMs: tiny-threshold ranking inversion, no integrity triggers on the store,
`as_of` lower bound, and free-text cross-checks.

## The lesson

Three reviews today, three times the same shape: **the thing I claimed to have done was not the
thing I did.** Here the claim was in the module docstring — "reasoning is recorded, never
reconstructed" — sitting directly above an emitter that fabricated 380 causes. A docstring asserting
a property is the most expensive place to be wrong, because it stops the next reader checking.
