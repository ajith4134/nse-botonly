# Claude's opinions — judgements, verdicts and recommendations, with reasoning

**Purpose (operator instruction, 2026-08-10):** every opinion, judgement or recommendation I give is
recorded here so it can be referred back to, checked against what actually happened, and overturned when
it turns out wrong.

**What belongs here:** anything that is *my judgement* rather than a measured fact or an operator decision.
Measured facts live in `docs/research/`; operator decisions live in `ajith_final_plan.md` Part IV as `A.`
entries. This file is the third category — **what I thought, and why**.

**Format:** `O.NN · date · the opinion · the reasoning · confidence · what would change my mind.`
Confidence is stated honestly: `measured` (backed by data I ran), `reasoned` (argued from evidence I did
not gather myself), `judgement` (experience and pattern, no direct evidence).

---

## O.01 · 2026-08-10 · The REDESIGN-vs-atlas tension should not be resolved in the plan

**Opinion:** the catalog should carry both the depth-first redesign and the 197-branch atlas at full
weight rather than picking one.
**Reasoning:** they answer different questions — the atlas is *what could exist*, the redesign is *what
should be built next*. Collapsing them destroys information; sequencing resolves it later at no cost.
**Confidence:** judgement.
**Would change my mind:** if the todo generation had needed a single scope to be coherent. It did not.

## O.02 · 2026-08-10 · The prior build's simulation failed on ALLOCATION, not fidelity

**Opinion:** the market-closed simulation was honest and well-built; it destroyed value because nothing
decided *what it should practise*.
**Reasoning:** measured from the 3,481 retained trades — replay was slightly *pessimistic* versus live
(ORB −0.475% vs −0.390%) and its calibration gap was 0.4pp against live's 11.8pp. But 97% of replay
experience went to the losing strategy and `credit_spread_v1`, the only measured edge at +1.816%/trade,
got **zero** reps.
**Confidence:** measured.
**Would change my mind:** nothing in the data contradicts it. This became rule R.13.

## O.03 · 2026-08-10 · Scope should be set by vocabulary, not blast radius

**Opinion:** "if you call it an engine, it gets the full loop; if it cannot survive the loop, rename it"
beats tiering by risk.
**Reasoning:** the prior build's failure was not that thin code existed but that thin code was *called* an
engine (`research/155`). Blast-radius tiers do not stop that — the label slips and the tier follows it
down. Vocabulary-based scope attacks the actual failure and needs no judgement call about tiers.
**Confidence:** reasoned.
**Would change my mind:** if it turns out to produce absurd results on genuinely trivial code that happens
to carry an engine-ish name.

## O.04 · 2026-08-10 · LOC as a target is the reward-hacking failure, not a depth measure

**Opinion:** the operator asked for "high LOC"; I pushed back and recommended depth measured against a
named SOTA analog instead.
**Reasoning:** METR measured frontier models gaming benchmarks in ~30% of runs. A line-count target is
exactly the metric that invites fake substance. Thousands of justified lines follow from a real solver;
they cannot be produced *by aiming at the count*.
**Confidence:** reasoned.
**Would change my mind:** nothing likely. The operator accepted the reframe.

## O.05 · 2026-08-10 · Role-prompting and "no TODOs" bans are not worth relying on

**Opinion:** do not use them; rely on the verification loop.
**Reasoning:** role-prompting has a peer-reviewed debunking (162 personas, no measurable gain); "no
placeholders" bans trace to nothing better than anecdote, and Anthropic's own docs prescribe verification
loops *instead of* prose bans.
**Confidence:** reasoned.

## O.06 · 2026-08-10 · Delegate breadth, keep the verdict

**Opinion:** context, not tokens, is the scarce resource; searching should be delegated by default.
**Reasoning:** measured on the first adversarial review — ~74k subagent tokens in, ~2k of findings out, a
37× compression, and the findings were *better* than a context-constrained read because the agent could
afford to run the code.
**Confidence:** measured. Became R.24.

## O.07 · 2026-08-10 · Index options should be built first, and the operator's order is right

**Opinion:** of the six holons, index options is the correct first build.
**Reasoning:** measured — index options are 93.81% of all F&O volume; NIFTY is 98.54% of that; the nearest
weekly is 92.8% of NIFTY. So NIFTY nearest-weekly is ≈87% of the entire NSE F&O market. It is also where
the only measured positive edge lived (`credit_spread_v1`, +1.816%/trade).
**Confidence:** measured.
**Cost I flagged:** it needs the whole Greeks/IV/margin stack before it can trade, which cash did not.

## O.08 · 2026-08-10 · "Watch everything" is affordable; the streaming ceiling was a red herring

**Opinion:** all five derivative segments can be watched simultaneously inside one Kite key.
**Reasoning:** measured — ~6,100 instruments against a 9,000 ceiling. 80 contracts carry 99.28% of NIFTY
weekly volume, so the liquid surface is three orders of magnitude smaller than the 113,955 contract count
suggests. Only stock options need tiering, being the sole genuine long tail (top 30 of 208 = 56.53%).
**Confidence:** measured.
**Would change my mind:** if MCX turns out to need far more contracts than inferred — currently unverified
and logged as a blocker.

## O.09 · 2026-08-10 · Volume peaks OTM, not ATM — a watch window centred on ATM is wrong

**Opinion:** the moneyness window should not be centred on ATM.
**Reasoning:** measured — ATM ±0.5% is 15.57% of NIFTY weekly volume; 0.5–1% OTM is **41.27%**, 2.6×
more. Volume and open interest also have different distributions: volume clusters near spot, OI peaks
2–5% out, which matters for max-pain and gamma-positioning instructions that key off OI.
**Confidence:** measured, single session.
**Would change my mind:** a trailing-window recomputation showing the peak moves — which is why I logged
that the tiers must recompute rather than freeze.

## O.10 · 2026-08-10 · Cash focus size should be derived from the cost floor, not fixed

**Opinion:** no fixed N; a name enters when its expected move clears `cost × 1.5`, capped by executable
capacity.
**Reasoning:** a fixed count is the magic constant R.03 forbids, and it is wrong at both ends — 150 names
is too many on a dead day and too few on a volatile one. Deriving it makes the set self-sizing.
**Confidence:** reasoned.

## O.11 · 2026-08-10 · `py-moneyed` should be rejected despite being adopted in my own sourcing record

**Opinion:** reverse my earlier ADOPT; use bare `Decimal` with an explicit context.
**Reasoning:** `Money` wraps an amount *with a currency*, and the module holds a single INR scalar whose
currency is never in question. The wrapper buys formatting we do not use and adds a dependency to every
downstream sizing call. Caught by adversarial review noticing it was never imported.
**Confidence:** reasoned.
**Note:** this is an opinion I got wrong the first time and corrected. Recorded as such deliberately.

## O.12 · 2026-08-10 · Filter combinations must be gated by effective trials or the winner is noise

**Opinion:** the operator's "trade all 15 combinations and keep the winner" needs the effective-trials
estimator or it will select luck.
**Reasoning:** the 15 combinations are heavily correlated (top-volume and top-turnover sets overlap
enormously), so the naive best-of-15 is the 7,846-rule trap in miniature.
**Confidence:** reasoned.

## O.13 · 2026-08-10 · The instrument identity key in the plan was wrong

**Opinion:** key on `(exchange, segment, tradingsymbol)`, not `(exchange, tradingsymbol)`.
**Reasoning:** measured across all 113,955 real rows — the documented pair collides on `BSE:INFRA` (a
Mirae ETF and a BSE index, same exchange and symbol, different segments). The documented key would have
silently dropped one row on every ingest while looking healthy.
**Confidence:** measured.

## O.14 · 2026-08-10 · Adversarial review in a fresh subagent is the highest-value stage of the loop

**Opinion:** it is worth its cost on every engine, and I should not skip it under time pressure.
**Reasoning:** two for two so far. On `capital_configuration` it found 14 defects — including a confirmed
over-allocation bug and two property tests that were provably vacuous — in code that had already passed
ruff, mypy and 29 tests. On `kite_instrument_master` it found 24, including a token-reassignment guard
that misses the only pattern Kite actually produces.
**Confidence:** measured, twice.

---

## Maintenance

Appended at the moment an opinion is formed, per **R.25**. Opinions that turn out wrong are **corrected in
place with the correction dated and the original left visible** — the history of a wrong judgement is more
useful than a clean file.
