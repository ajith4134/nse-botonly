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

## O.15 · 2026-08-10 · A guard that has never been seen to fire should be assumed broken

**Opinion:** any detector whose triggering condition has not been reproduced end to end is probably
detecting nothing, and should be treated as unverified regardless of test coverage.
**Reasoning:** the token-reassignment guard passed review, had a test, and detected **zero** real
reassignments — because it consulted only the previous ingest while Kite drops a contract before reusing
its token. The test exercised the one path that works and never the one that occurs. The same shape
recurred in the truncation guard, which returned early in exactly the case it existed for.
**Confidence:** measured, twice in one module.
**Would change my mind:** nothing so far. This is now how I read any guard.

## O.16 · 2026-08-10 · Derived thresholds must be derived from something in the data, not merely expressed as a ratio

**Opinion:** expressing a constant as a fraction does not satisfy R.03; it has to be computed from
observable data.
**Reasoning:** I wrote `0.30` with a comment claiming scale-invariance made it compliant, and justified it
with "delisting moves single-digit percentages" — which the dump itself contradicts (real cohorts are
12.5% and 10.9%). The honest version computes the tolerance from the baseline dump's own largest expiry
cohort; on live data that yields **25.04%**, close to the guess but for a reason.
**Confidence:** measured.

## O.17 · 2026-08-10 · Adversarial review should run before the real-data pass, not after

**Opinion:** the loop order in R.23 should be reconsidered — review earlier.
**Reasoning:** this module passed its real-data pass with 113,955 contracts and *looked* fully verified,
while carrying a guard that detected nothing and a path that could wipe the universe. A green real-data
pass on a broken guard is a false signal, and I nearly signed off on it.
**Confidence:** judgement, from two slices.
**Would change my mind:** if review-first proves to waste effort on code that the real-data pass would
have rejected outright anyway. Worth watching over the next few engines before changing R.23.

## O.18 · 2026-08-10 · Sonnet matches opus on adversarial review — the rule should change

**Opinion:** adversarial review should default to **sonnet**, not opus. R.26 said the opus choice was
"reasoned, not measured"; it is now measured, and the reasoning was wrong.
**Reasoning:** the sonnet review of the bar store found a **reproduced future-leak** — the exact class of
defect this whole module exists to prevent — plus a reversed sort, four validation holes and a structural
blindness in the test suite. It ran its own mutation set, restored the tree, verified with `diff`, checked
the real 659,990-row database to establish the bug was latent rather than active, and explicitly said which
categories were clean instead of padding. That is the same shape of work the two opus reviews produced, at
roughly a third the cost.
**Confidence:** measured, once. One trial is thin, which is why the revised rule keeps an escalation path
rather than deleting opus from the option set.
**Would change my mind:** a sonnet review that comes back thin — no reproductions, no mutation results, or
findings that read as speculation — on code where opus then finds something real.

## O.19 · 2026-08-10 · A correctness guarantee expressed in SQL is only as good as the column's collation

**Opinion:** any invariant enforced by a SQL comparison must be checked against the *storage type's*
ordering, not the ordering the values appear to have.
**Reasoning:** `available_from <= ?` reads like the invariant it enforces, and it is wrong, because the
column is TEXT and ISO strings with different offsets do not sort chronologically. Nothing about the code
looked suspicious; the docstring correctly described the intent; 114 tests passed. The defect lived
entirely in the gap between "these are timestamps" and "these are strings that look like timestamps".
**Confidence:** measured.
**Generalisation I am now applying:** wherever a comparison crosses a serialisation boundary, the
normalised form is the stored form, and the human-readable form is derived — never the reverse.

## O.20 · 2026-08-10 · A property test built from one constant is a unit test wearing a costume

**Opinion:** a property test whose inputs all descend from a single fixture cannot state a property; it
restates an example.
**Reasoning:** the invariant test here varied delay and query time across thousands of Hypothesis cases and
**could not have failed**, because every timestamp came from one IST constant and the bug was about
disagreeing offsets. The generated dimensions were the ones that did not matter.
**Confidence:** measured.
**What I do differently now:** before writing a property test, name the dimension along which the property
could plausibly break, and generate *that* — not whatever is easiest to parameterise.

## O.21 · 2026-08-10 · A truncated NSE expiry ladder predicts an F&O exit, and the engine should emit it

**Opinion:** the expiry-ladder depth is a leading indicator of derivatives de-listing, and belongs in the
universe engine rather than being left as a curiosity.
**Reasoning:** measured on 36 real trading days. NSE lists three monthly expiries per stock underlying; on
2026-07-27, 207 of 210 held exactly 3, and the two holding 1 — `EXIDEIND` and `NUVAMA` — were gone by the
next collected date. `SAMMAANCAP` had already left the same way. The exchange stops listing new expiries
before an underlying exits, so the ladder shortens as contracts run off. Lead time was **at least 35
trading days** and is left-censored, since the truncation was already visible in the earliest file I hold.
**Confidence:** ~~measured, three events, one window~~ → **CORRECTED 2026-08-10, measured on 349 real exits
across 25 years** (2001-07-02 → 2026-08-10, 6,207 trading days, 560 underlyings, whole F&O archive).

**The correction — the signal is real but weaker than three events suggested:**

| Measure | Value |
|---|---|
| Genuine F&O exits | **349** |
| Exits preceded by a sustained truncation | **269 — recall 77.1%** |
| Exits with **no** warning at all | **80 (22.9%)** |
| Symbols ever warned | 343, of which **57 never exited** (~17% false positives) |
| Lead time (sessions) | min **0**, median **41**, p90 43, max 44 |

**The median lead is structurally explained, which is why I believe it.** A full ladder is three monthly
expiries ≈ 63 sessions; dropping to two leaves ≈ 2 months ≈ 42 sessions of contracts to run off. The
measured median of 41 is that mechanism, not a coincidence — and the tight 41-44 clustering is the same
fact seen from the other side.

**What I got wrong:** I called it a predictor. It is a *screen* — it misses nearly a quarter of exits and
raises a false alarm about one time in six. `≥35 trading days` was right as a lower bound but the framing
oversold it. It is good enough to stop opening new multi-expiry positions, not good enough to be treated as
a forecast.
**What would still change my mind:** a truncation-then-refill population large enough to characterise; 57
such symbols now exist in the archive and have not yet been examined individually.

## O.22 · 2026-08-10 · `UNKNOWN` must be a first-class classifier output, not an error path

**Opinion:** the absence classifier should return `UNKNOWN` with evidence whenever nothing discriminates,
and snapshots should carry their unknown count to the caller.
**Reasoning:** absence from a daily exchange file is ambiguous between at least four causes — not traded,
not collected, delisted, renamed — and the retained data contains all the ambiguity needed to prove it: 324
of 3,419 cash symbols miss a day and are mostly government securities that simply do not trade daily, while
five weekdays have no file at all. A classifier forced to pick would delist most of the G-Sec universe
inside a week. This is `O.15` applied before the fact rather than after: a classifier that never says "I
cannot tell" is guessing, and its cleanliness is the symptom.
**Confidence:** reasoned, from measured ambiguity.
**Would change my mind:** nothing likely. The cost of an explicit unknown is a caller decision; the cost of
a silent guess is a backtest that cannot be trusted and gives no sign of it.

## O.23 · 2026-08-10 · The calendar dependency is load-bearing, and I proved it by getting it wrong myself

**Opinion:** a session calendar is not a convenience for this engine; without one it cannot make its central
distinction at all, and `L0.30` must be built before `L0.05`.
**Reasoning:** writing `docs/research/204` I counted five weekdays with no F&O file and called them a
collection outage. Resolved against `pandas_market_calendars`' NSE sessions, one of the five
(`2026-06-26`) is an **NSE holiday** — nothing was published and nothing is missing. The real outage is four
days, not five. I made the precise error the absence classifier exists to prevent, in the document
specifying the classifier, with the data in front of me.
**Confidence:** measured.
**Would change my mind:** nothing. An error I made while concentrating on not making it is the strongest
evidence available that the check has to be mechanical.

## O.24 · 2026-08-10 · A "200 OK" is not evidence of content, and an install is not evidence of function

**Opinion:** acquisition code should verify the *payload*, and sourcing verdicts should require a real call
returning real rows — status codes and successful installs are both routinely false positives.
**Reasoning:** three independent instances in one session. A guessed historical-constituents URL returns
**HTTP 200 with a 404 error page as the body**. The `bhavcopy` package installs, instantiates without
error, and **downloads nothing at all**. `nselib`'s `bhav_copy_equities()` returns an empty frame while its
sibling method works. Each would have passed a check based on the thing that is easy to check.
**Confidence:** measured, three times.
**How it is applied:** the archive fetcher opens and row-counts every ZIP before recording it as acquired,
and R.17 verdicts in `204` §7 each cite a byte count and a row count rather than a status.

## O.25 · 2026-08-10 · `isinstance(x, date)` is a trap, and type guards should be tested for what they let through

**Opinion:** a type guard written as `isinstance` against a base class must be tested with its subclasses,
because the guard's job is exclusion and `isinstance` is inclusive by design.
**Reasoning:** `datetime` and `pandas.Timestamp` are both subclasses of `date`. My guard admitted them, and
`day in set_of_dates` then returned `False` for the *same calendar day* — `is_trading_session` reported a
real trading Tuesday as closed, silently. In a pandas-heavy codebase where the calendar library itself
returns `Timestamp`, that input is routine, not exotic. The module's own docstring named "a confident wrong
answer" as the one unacceptable outcome, and it shipped one.
**Confidence:** measured.
**How it is applied:** the moment-carrying types are refused *by name* with a message telling the caller to
call `.date()` themselves — coercing silently would drop a time and a timezone without being asked, which
is a different bug wearing the same clothes.

## O.26 · 2026-08-10 · An inline boolean rule cannot be mutation-tested; extract it

**Opinion:** when a decision rule combines conditions inline, its operators are effectively unfalsifiable
and should be pulled into a named function taking its own inputs.
**Reasoning:** `is_reliable = recognised > 0 and recognised >= fence` looked well covered — years either
side were tested. But no real year's holiday count lands exactly on the float-valued fence (6.875), so both
`>=` vs `>` and the `> 0` conjunct survived mutation: nothing in the suite could distinguish them. Extracted
as `holiday_count_is_credible(count, fence)` the boundary is testable with values chosen to sit *on* it,
and both mutants die. The same reasoning killed the Tukey-multiplier mutant — the original fixture years
were all comfortably clear of the margin the multiplier moves.
**Confidence:** measured, three mutants.
**Would change my mind:** nothing. This generalises: a threshold that is only ever exercised by real data
is tested wherever the data happens to fall, which is not where the bugs are.

## O.27 · 2026-08-10 · I wrote the exact magic constant I had criticised, three engines after making it a rule

**Opinion:** R.03 needs a mechanical check in scripts too, not only in engines — discipline did not survive
contact with an exploratory scan.
**Reasoning:** scanning 25 years of cash bhavcopy for corporate-action discontinuities I wrote
`MATERIAL_RATIO_DEVIATION = Decimal("0.005")` with a tidy justification about tick sizes. It produced
**89,597 "events" across 4,592 symbols** whose implied factors clustered entirely in 0.93-1.05 — noise,
because `PREVCLOSE` does not always mean "yesterday's `CLOSE`" for illiquid names. This is precisely the
`0.30` shrinkage-tolerance defect from `kite_instrument_master` (`O.16`), committed by me again, in a
session where I had already written the rule twice. A plausible-sounding rationale is what makes these
survive review — mine had one both times.
**Confidence:** measured, twice now.
**What changed:** the scan records the full factor distribution and derives the cut afterwards; real ratio
actions live in the tail (2, 3, 5, 10 and their reciprocals), not near 1. The broader lesson is that "it is
only a script" is how the rule gets bypassed — `rupee_literal_detector` (`L2.31a`) covers money literals in
`src/`, and scripts are outside its reach.

## O.28 · 2026-08-10 · F&O strike adjustments come in two shapes, and only one is a ratio

**Opinion:** an adjustment engine must classify additive versus multiplicative before computing any factor,
because the two leave different fingerprints in the strike ladder.
**Reasoning:** measured over 37,334 real (underlying, expiry, strike) triples — only **4** underlyings carry
non-half-integer strikes. `CANBK` (+0.80), `INDIANB` (+0.75) and `BANKINDIA` (+0.35) keep a **standard**
ladder step and shift every strike by a constant: additive, a dividend adjustment. `TRENT` has steps of
33.30 / 66.70 / 133.30 — that is 100/3, 200/3, 400/3, a **factor of 3**: multiplicative.
**Confidence:** measured, one 36-day window, four events.
**Correction I made mid-analysis, left visible:** I first classified `BANKINDIA` as multiplicative because
it showed two fractional offsets (.35/.85). That was an artefact of a 2.5 step not being an integer, not
evidence of a ratio. The reliable test is whether the **step** is a multiple of 0.5 — standard step means
additive, non-standard step means a ratio divided it.
**Would change my mind:** an underlying showing both shapes at once, or a ratio adjustment that happens to
land on a standard step (a factor of 2 on a 5-point ladder would, and would be invisible to this test —
which is a known hole, not a solved problem).

## O.29 · 2026-08-10 · I built a detector on an unverified premise and only tested it at population scale

**Opinion:** a detector's *premise* must be verified on one known-positive event before it is run over 25
years of data, and I did this backwards.
**Reasoning:** I hypothesised that `CLOSE(t-1) != PREVCLOSE(t)` marks a corporate action, wrote the scan,
ran it over 7,857 files, and spent two rounds tuning thresholds against its output — before anyone checked
whether the premise held. It does not: on `TATASTEEL`'s 1:10 split and `IOC`'s 1:2 bonus the two values are
**exactly equal**. NSE's `PREVCLOSE` is a raw carry-forward. Every "event" the scan produced was noise or
early-archive data corruption, and the tail I was about to derive a threshold from was meaningless.
**What made it seductive:** the conclusion was convenient — it would have meant the whole adjustment engine
could be built from data already on disk, with no external dependency. I wanted that to be true and tested
it at scale instead of at a point.
**Confidence:** measured. Recorded as `D.28`.
**The rule I am applying from here:** one known-positive and one known-negative case *first*, by hand, then
the population scan. A scan that runs cleanly over 25 years proves the code runs, not that the idea is
right — and this is the second time in one session that a green large-scale pass masked a broken premise
(`O.17` was the first).

## O.30 · 2026-08-10 · A guard nobody invokes is a test fixture wearing a guard's name

**Opinion:** every mechanical rule-enforcer must have a runnable entry point wired into the gate, and its
firing must be *proven* with a planted violation — not inferred from a clean run.
**Reasoning:** `rupee_literal_detector` (`L2.31a`) was built to enforce R.03 mechanically, and it was
imported by **nothing but its own test**. It had never run over a single line of the codebase. Its tests all
passed, so it looked healthy in every check I made. That is the complete explanation for `O.27`: the
hardcoded `0.005` did not slip past the guard, it was never in front of it.
**Confidence:** measured.
**What changed:** `scripts/check_no_hardcoded_money.py` scans `src/` **and** `scripts/` and is now part of
the Stop-hook execution gate beside ruff/mypy/pytest. Proven by planting a violation — two literals caught,
exit 1 — then removing it and confirming exit 0. A clean first run proved nothing, which is `O.24`'s lesson
applied to my own tooling.
**The generalisation:** this is the same shape as `O.15` ("a guard that has never been seen to fire should
be assumed broken"), one level up. There the guard ran and detected nothing; here it did not run at all. The
check is the same either way — make it fire on purpose before believing it.

## O.31 · 2026-08-10 · A parser that matches on a NUMBER without checking the NOUN will confidently corrupt blue chips

**Opinion:** any text parser extracting a quantity must verify *what* the quantity describes, not merely
that it is present in the right shape.
**Reasoning:** my bonus regex required only the token `bonus` followed by `N:M`. It therefore quantified
`"Bonus Debentures 6:1"` and `"Bonus Preference Shares 21:1"` as equity dilution. Measured on the real feed:
**11 such actions, 7 wrongly quantified**, including `DRREDDY` at a factor of **1/7** on a day the stock
moved 2.8%, and `ZEEL` at **1/22** on a flat day. Applied, those restate a blue chip's entire pre-2011
history downward by 7x and 22x — the engine manufacturing exactly the fake crash it was built to prevent,
on a different input shape.
**Confidence:** measured, verified against real closes either side of the ex-dates.
**Would change my mind:** nothing. The fix is a non-equity instrument check (`debenture`, `preference`,
`ncrps`, `dvr`, `warrant`, `bond`, `unit`), and the general form is: match the noun, not just the number.

## O.32 · 2026-08-10 · When part of a subject cannot be quantified, nothing in it can

**Opinion:** an unquantifiable component must poison the whole record rather than letting a sibling
component supply a plausible number.
**Reasoning:** `MONNETISPA` 2018 reads `"Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To
Rs.10"`. My component matcher found the consolidation leg and returned a factor of **3.03**, silently
discarding the capital reduction — a number computed from half a compound sentence, carrying full
confidence. The ordering that prevents this (unquantified markers checked before components) was also
entirely unpinned: mutation showed swapping it flips five real demergers to "no adjustment", the failure the
spec names as catastrophic, with nothing in the suite to catch it.
**Confidence:** measured.
**The generalisation:** this is `A.41`'s three-way lesson at sub-record granularity. A record is only as
quantified as its least quantified part.

## O.33 · 2026-08-11 · A broker SDK's convenience type is a defect surface, not a convenience

**Opinion:** when an SDK hands back a parsed, "friendly" value, the raw wire value is the one to store,
and the parse should be inverted rather than trusted.
**Reasoning:** `kiteconnect` returns `exchange_timestamp` as a **naive** datetime produced by
`datetime.fromtimestamp(seconds)` — the instant expressed in the *host's* local zone, with the zone then
thrown away. On this UTC host the value is correct, which is the dangerous part: it is correct **by
accident of deployment**, and the identical code on an IST host is 5h30m wrong while raising nothing and
looking entirely plausible. Measured directly against wall clock during an open session: parsed
`04:06:51` against wall UTC `04:06:52`. The fix inverts the SDK's own construction with
`naive.astimezone()`, correct under any host zone, and the test runs it under UTC, IST and New York.
**Confidence:** measured, against the live socket.
**Would change my mind:** nothing about the hazard. If Kite ever returns tz-aware values the inversion
becomes a no-op, which the implementation already handles.
**The generalisation:** the same shape as `O.31` — the parse looked right on the data in front of me. A
value that is correct because of where it runs is not correct.

## O.34 · 2026-08-11 · Two defects the tests caught that review would not have

**Opinion:** the two defects found while building the depth recorder are both invisible to reading and
both fatal, which is the argument for tests-before-implementation stated concretely rather than as a
principle.
**Reasoning:** (a) `MINIMUM_SAMPLES_FOR_STALENESS_MATURITY = int(1 / (1 - 0.999))` evaluates to **999**,
not 1000, because `1 - 0.999` is `0.0010000000000000009` in binary floating point and `int()` truncates.
The arithmetic is right, the value is wrong, and nothing about the line looks wrong. (b)
`threading.Thread(target=self._writer_loop, ...)` was constructed **without `args=(shard,)`**, so every
tape-writer thread would have died at bootstrap with a `TypeError` raised in a thread nobody was
watching — a capture that connects, subscribes, reports itself healthy, and writes nothing. The session
would have looked fine in the log and produced an empty tape.
**Confidence:** measured — both were failing tests, not inspection findings.
**The generalisation:** defect (b) is the more instructive one. A thread that dies during bootstrap
raises where nobody is looking, so the failure presents as *silence*, and silence is what a healthy
capture also looks like. Anything that runs unattended needs its liveness asserted from the outside, which
is why the recorder now records the writer exception and re-raises it at `stop`.

## O.35 · 2026-08-11 · Disk, not the API, is what bounds a depth capture — and the bound must be solved

**Opinion:** the capture universe is the answer to a budget problem and must never be a number chosen by
me.
**Reasoning:** Kite permits 9,000 instruments across three sockets, which sounds like the constraint and
is not. Measured on the live feed: median **0.120 packets/s/instrument**, aggregate 54.4/s for 120
instruments, with a **130x spread** across instruments (0.013 to 1.72). Multiply by a 22,500-second
session and a compressed row width and the disk runs out long before the API does. Every input to that
product is measurable — free space, realized bytes/row from the tape's own parts, each instrument's own
rate — so the only genuine policy input is how many sessions of history are worth keeping.
**Confidence:** measured for the rates; the byte width was still being calibrated when this was written.
**Would change my mind:** a materially cheaper row than calibration reports would widen the universe
without changing the method. The method is the opinion, not the number.
**The generalisation:** the tempting failure was to write `CAPTURE_UNIVERSE_SIZE = 1000` and move on. It
would have been wrong by an order of magnitude in either direction depending on the day's disk, and
nothing would have said so.

## O.36 · 2026-08-11 · Resuming a counter protects against restart, not against concurrency

**Opinion:** when two writers can address the same file, the fix is to make the address unique, never to
make the counter smarter.
**Reasoning:** I widened a live capture by launching a second recorder, and the first kept running because
my kill targeted the wrapper PID rather than the Python process. I had *already* fixed part-file numbering
to resume from whatever a shard directory held, which fully solves a **sequential** restart — and does
nothing at all for a **concurrent** one. Both recorders scanned the directory, both computed the same
"next free" index, and from then on each could overwrite the other's parts. Measured aftermath: `shard=01`
held interleaved output from two processes, and 20,000 rows I quarantined as duplicates turned out to
belong to the *newer* capture, spanning `10:07:43..10:08:20` — after the older process had already
stopped. I nearly deleted live data while cleaning up.
**The fix:** the tape path now carries a `capture_run=<id>` level, so two recorders cannot share a
directory whatever the operator does. Impossible beats unlikely.
**Confidence:** measured — the interleaving is visible in the part timestamps and the recovered rows.
**Would change my mind:** nothing. A read-modify-write on a shared directory from two processes is a race
whatever the arithmetic in the middle.
**The generalisation, and the harder lesson:** the *code* defect was mine, but so was the operational one —
I verified `ps -p 131046` was gone and concluded the capture had stopped, when 131046 was the shell and
131049 was the process doing the work. Confirming that a PID exited is not confirming that the work
stopped. The check should have been on the thing itself (`pgrep -f` on the script), which is the same
principle as `O.15`: verify the property you care about, not a proxy that usually coincides with it.

## O.37 · 2026-08-11 · A green suite measures nothing until something has tried to break it

**Opinion:** "tests pass" and "tests would catch this" are unrelated claims, and I have been
conflating them.
**Reasoning:** the depth engine's suite was 104 green tests written test-first, and I signed it off. Two
independent checks then found what it could not see. Random-AST mutation scored it **21/36 (58%)** and
adversarial review proved **7 tests vacuous** — including the two guarding the properties I had argued
hardest for in the module docstring. Replacing the atomic-rename temp path with the final path (deleting
atomicity outright) left the suite **green**. Deleting both `fsync` calls left it **green**, because
`kill -9` never drops page cache, so the kill test I was proudest of *could not possibly* have exercised
durability. `test_stop_is_idempotent` contained no assertion at all.
**Confidence:** measured, twice, by two methods that agreed.
**Would change my mind:** nothing. The two methods are complementary and both are now non-optional:
mutation finds tests that do not constrain, review finds properties no test addresses.
**The generalisation:** my instinct writes a test that *demonstrates* the behaviour. What is needed is a
test that *fails when the behaviour is removed*. Those coincide less often than they feel like they do,
and the gap is invisible from the inside — which is exactly why it needs a mechanical check rather than
more care.

## O.38 · 2026-08-11 · Self-referential thresholds fail hardest on the data they exist for

**Opinion:** a threshold derived from an instrument's own distribution must use a **robust** statistic, or
it silently inverts on the pathological cases it was built to catch.
**Reasoning:** I defined a capture gap as an interval beyond the instrument's own **99th percentile**
interval, reasoning that each instrument should have its own notion of a pause — which is right, given the
measured 130x rate spread. But if more than 1% of intervals are outages, the 99th percentile *is* an
outage, so nothing is ever a gap. Review measured the consequence: an instrument that quoted for **380
seconds of a 6.5-hour session**, in twenty bursts separated by 20-minute holes, reported **99% coverage
and a clean USABLE verdict**. The worse the data, the better it scored. The fix is the **median** (50%
breakdown point) times a multiple, which keeps describing normal behaviour until an instrument is silent
more often than not.
**Confidence:** measured, with a repro.
**The generalisation, which also caught the Tukey fence:** a statistic computed *from* the population it
judges needs its breakdown point checked against the contamination it will actually meet. The same review
found my coverage fence going **negative** (`Q1 - 1.5*IQR` on a metric bounded in [0,1]) and therefore
never firing — measured at -0.42 on a real 5%..99% session. Both defects are the same mistake: I checked
the formula's *definition* and not its *range on my data*.

## O.39 · 2026-08-11 · Ship an existing library only after proving it handles the discontinuity

**Opinion:** the deciding test for a data-source library is not whether it fetches today, but whether it
handles the historical break — because that is what a README never mentions and a smoke test never reaches.
**Reasoning:** eleven NSE libraries were installed and called against real endpoints. Most fetch *today's*
bhavcopy fine. NSE changed the layout in July 2024 (UDiFF), and that one question sorted them: `nsepython`
always uses the legacy schema, `nselib` always uses the UDiFF URL and returns **silently empty** for 2020
with no error, `jugaad-data` falls back silently to the legacy schema on historical cash and throws
`BadZipFile` on recent F&O, `nsepy` cannot connect at all (dead host). Exactly one — `nse`
(BennyThadikaran) — branches explicitly on NSE's own `UDIFF_SWITCH_DATE` and fetches true historical UDiFF.
**Confidence:** measured — every verdict came from an install plus a real call on both a pre-change and a
post-change date.
**Would change my mind:** a maintained fork of any rejected library that passes the same two-era test.
**The finding that outranks all of them:** a real request for **Sunday 2026-08-09 returned Friday's data
with HTTP 200**. No retry library and no NSE library tested handles that, because nothing about the
response is an error. A fetcher that trusts a 200 will ingest stale data as fresh, silently — which is why
the content-aware failure classifier is the one part of the ingest core that must be built rather than
adopted.

## O.40 · 2026-08-11 · A single-page app is not a wall — it is a client whose API calls are readable

**Opinion:** "the page is JavaScript-rendered, so the data is unreachable" is almost never true, and
treating it as true is a research failure rather than a platform limitation.
**Reasoning:** `research/207` recorded NSE's ASM surveillance list as **BLOCKED**, having guessed
`/api/reports/asm` from the page path and received 404. The correct method is to read what the page's own
JavaScript fetches: `/dist/js/sections/reports/asm.js` contains `B.get('/api/reportASM')` in plain text —
capital `ASM`, a URL nobody had tried. It returns **HTTP 200, 50,270 bytes of JSON, with zero cookies**,
reproduced byte-identically on a fresh connection. The data was never behind a wall; the endpoint name was
simply never read off the client that calls it every time the page loads.
**Confidence:** measured — fetched, reproduced, and now parsed into 189 real surveillance entries.
**Would change my mind:** nothing about the method. Some endpoints genuinely do require a session; the
point is that guessing a URL and recording BLOCKED is not evidence of one.
**The consequence, which is the important part:** two other BLOCKED verdicts in the same document (ATM IV,
historical bulk deals) were reached the same way — by guessing rather than by reading the client. They are
now suspect, and I have marked them as needing re-test by this method. A blocker established by a weak
method is worse than no blocker, because it stops anyone looking again.

> **CONFIRMED LATER THE SAME DAY (2026-08-11), and more strongly than predicted.** Every BLOCKED
> verdict in `research/207` fell: **ASM** to reading `asm.js`; **ATM IV** to noticing the endpoint had
> been *superseded* (`option-chain-v3` returns 241KB of real data from a cold session, and NSE
> publishes `impliedVolatility` itself); **index weights** to reading `IISLComponet.js`, which
> revealed an entirely separate host (`liveindexsa.niftyindices.com`). Three for three. The failure
> mode was never NSE's gating — it was accepting a 404 as evidence about the platform when it was
> only evidence about one guessed string. I now treat "BLOCKED" in any reconnaissance, including my
> own, as a hypothesis rather than a finding until the client has been read.

## O.41 · 2026-08-11 · I twice broke my own parallel-work discipline, in the same way

**Opinion:** the rule I wrote for the fan-out — disjoint ownership, serial integration — is correct, and I
violated it twice while supervising it.
**Reasoning:** (a) I ran `git add -A` while agents were mid-flight and swept an in-flight adapter into an
unrelated commit; (b) I edited `tests/nse_ingest_conformance.py` — a **shared** file the whole fan-out
depends on — while five agents were running against it, and one reported the contract changing underneath
it mid-task and had to reorder its samples to cope. Both are the same error: I treated my own writes as
outside the concurrency rules I imposed on the agents.
**Confidence:** measured — one agent reported the disruption explicitly and the git history shows the
sweep.
**Would change my mind:** nothing. The fix is mechanical: shared files are frozen for the duration of a
fan-out, and integration commits name their files explicitly instead of `-A`.
**The generalisation, and its third instance today:** this is the same shape as `O.36` — I verified a PID
had exited and concluded the *work* had stopped. All three are me trusting a convenient proxy over the
property I actually cared about. The pattern is worth more attention than any of the individual defects.

## O.42 · 2026-08-11 · Zero probability is a CLAIM, not silence — and it cost a whole engine

**Opinion:** when combining experts that measure different things, an expert's silence must
be encoded as uniform mass, never as zero, and the pooling rule must be multiplicative rather
than linear.
**Reasoning:** I built four regime classifiers, each measuring a different axis — path geometry,
latent-state dispersion, realised volatility, session structure. Each assigned `0.0` to the regimes it
did not measure, which reads as "definitely not volatile" rather than "I have no view on volatility".
Linearly averaging four such distributions can only ever move the result TOWARD uniform, so the brain
could never be confident **by construction**. Measured on 659,990 real bars across 12 instruments:
**100% abstain, 538 of 538 decisions vetoed.** Every unit test passed throughout — the engine was
internally consistent and collectively useless.
**The fix is two changes, both principled rather than tuned:** silence spreads as uniform mass over the
unmeasured axis, and the panel pools **log-linearly** (a weighted product of experts), which sharpens
where independent experts agree instead of blurring. Re-measured on the same bars: **9.1% actionable,
concentration median 0.28 against a max of 0.92**, and the panel now separates 491 ranging from 35
volatile, 7 quiet and 5 trending sessions.
**Confidence:** measured, before and after, on the same real data.
**Would change my mind:** nothing about the encoding. The 50/50 measured-versus-silent mass split is a
neutral default I would revisit if a classifier ever spoke to three axes rather than one.
**The generalisation:** this is the strongest argument for `R.05` I have produced. A full unit, property
and adversarial suite was green while the engine was incapable of ever acting. **No test I would have
thought to write would have caught it** — only running the thing on real data did, because the defect
was in what the composition MEANT, not in what any component computed.

## O.43 · 2026-08-11 · Auth in front of a reflection hides the bug, it does not fix it

**Opinion:** when request input is echoed into a response, "but you need a valid token to reach it" is
not a defence, and I nearly accepted it as one.
**Reasoning:** an automated review flagged the dashboard for reflected XSS — the access key was threaded
through every link as `?key=…` and interpolated into `href` and `<meta refresh>` attributes. My first
instinct was that auth constrained it: to get the reflection you must already know the token, and if you
know the token you are already in. **That was wrong, and I proved it before acting.** The server
deliberately supports a no-token configuration where the gate is open, and on that path a raw
`<script>alert(1)</script>` reached the response body verbatim. The reasoning failed because it assumed
one deployment shape and the vulnerability lived in another.
**The fix removes the class, not the instances.** The key is traded for an HttpOnly cookie on first
authorised request and every link drops its query string, so there is nothing left to reflect. The
renderer's `access_query` parameter was **deleted rather than escaped** — escaping two call sites leaves
the third one someone adds next month.
**Confidence:** measured. Exploited it against the real app, then confirmed absence after the fix, then
re-verified on the live server: hostile key returns a fixed 401 body, and the rendered wall contains zero
`?key=` occurrences.
**Would change my mind:** nothing. The one honest limitation is that the cookie cannot be `Secure`
because the server speaks plain HTTP — recorded in the code rather than glossed.
**The generalisation:** "an attacker would need X" is a claim about the deployment, and deployments
change without the code changing. `O.42` was the same shape one layer down — I reasoned about what my
composition meant instead of running it. Both times the answer took one script.

---

## Maintenance

Appended at the moment an opinion is formed, per **R.25**. Opinions that turn out wrong are **corrected in
place with the correction dated and the original left visible** — the history of a wrong judgement is more
useful than a clean file.

## O.44 · 2026-08-11 · A service is not verified by `is-active`; it is verified by `kill -9`

**Opinion.** The only evidence that a supervised service works is that it recovers from a kill nobody
issued on purpose. `systemctl is-active` right after a hand-restart proves the binary starts, which was
never in doubt — the claim being made is about 3am, when nobody is watching, and that claim is only
tested by killing the main PID and refusing to touch it afterward.

**Reasoning.** Every failure this unit exists to survive is one I cannot be present for. The dashboard
had been running under `nohup` for days and looked perfectly healthy the entire time; its defect was
invisible precisely because nothing had killed it yet. `Restart=always` is a claim about the future, and
a claim about the future is verified by causing the future, not by observing that the present is fine.

**Confidence: measured.** `kill -9 190218` returned a new main PID 190379, state `active`, `/healthz`
200, `NRestarts=1` — with no intervening command from me.

**What would change my mind.** Nothing about the method. But it is deliberately incomplete: I killed the
process, not the machine, so reboot survival rests on `Linger=yes` being read correctly rather than on a
reboot I have observed. That is a weaker claim than the crash claim and I am not entitled to state them
in the same breath.

## O.45 · 2026-08-11 · A restart policy without a burst cap converts one bug into silence

**Opinion.** `Restart=always` with no `StartLimitBurst` is worse than no supervision for a server whose
job is to report status. A process that cannot bind its port will retry forever, writing an identical
error every five seconds, and the log becomes uniform — which reads exactly like nothing being wrong.

**Reasoning.** This is the same failure as the regime brain in `A.59` and the wall's alphabetical
ordering: a system that reports its problems in a form indistinguishable from its healthy state has not
reported them. Failing loudly and staying failed is more informative than recovering forever, because
a stopped unit is a state a human notices and an infinite retry loop is not.

**Confidence: reasoned.** The specific failure mode was observed here — the first start did hit
`address already in use`, and with an uncapped policy it would have retried against a port the old
`nohup` process held indefinitely. I did not run the uncapped variant to watch it happen.

**What would change my mind.** A monitor that alerts on restart RATE rather than on unit state. Then
infinite retry carries the information the cap currently provides, and the cap becomes the worse choice
because transient causes — a slow network mount at boot — would resolve themselves.

## O.46 · 2026-08-11 · A guard that cannot fire is worse than a missing guard

**Opinion.** `name.endswith("__init__")` in the catalogue excluded nothing, because the name had already
been stripped of that part upstream. The cost was not the five junk rows — it was that the line READ as
handled. Anyone auditing the file, including me on the day I wrote it, saw an exclusion and moved on.

**Reasoning.** A missing check is discoverable by the symptom it fails to prevent. A check that is
present and inert is actively camouflaging: it consumes the attention that would otherwise go to
noticing the symptom. This is the same failure as `A.59`'s zero-probability and the uncapped restart
policy in `O.45` — a system whose broken state is shaped like its working state.

**Confidence: measured.** `_module_name_for(Path('src/nse_algo_trader/regime/__init__.py'), Path('src'))`
returns `'nse_algo_trader.regime'`; `.endswith('__init__')` is `False`. The predicate was never true for
any input the function could receive.

**What would change my mind.** Nothing about this instance. The general lesson is narrower than it
sounds, though: the fix is not "distrust all guards" but "a guard over a DERIVED name must be tested
against what the derivation actually produces." I found this by eye, not by test, and a mutation test
would have caught it years earlier than I did.

## O.47 · 2026-08-11 · Verifying a surface with `curl` is verifying that bytes exist

**Opinion.** Every dashboard failure worth catching returns HTTP 200 — an empty table, a dark-mode token
that was never redefined, a `nan` where a number should be, a layout that overflows. A status check that
passes on all of them is not a status check.

**Confidence: measured, and immediately.** The very first screenshot captured under this rule exposed a
defect that had survived every previous `curl`-based verification of the same page (`O.46`).

**What would change my mind.** Nothing about the principle, but the harness is weaker than it looks: it
proves the page PAINTED and logged no console error. It does not read the pixels, so a table showing
plausible-but-wrong numbers still passes, and I am the remaining check on that. Automated visual
diffing against a stored baseline would close it, at the cost of failing on every legitimate change.

## O.48 · 2026-08-11 · Validating a palette is not validating that it renders

**Opinion.** I ran `validate_palette.js` over the regime colours, recorded the ΔE figures in the module
docstring, and treated the palette as verified. Every one of those numbers was true and the `trending`
series was invisible on the live page the whole time. The validator answers "are these four colours
distinguishable"; it cannot answer "do these four colours reach a browser", and I let the first stand in
for the second.

**Reasoning.** This is the general shape of most of my false confidence: a check that is real, passed
honestly, and load-bearing for a claim it never made. The same pattern produced `O.46` — a guard that
was genuinely present and inert — and the `curl`-200 verification in `O.47`.

**Confidence: measured.** 0 pixels of `#3987e5` in a 1440×1000 capture where the other three series had
2,869–3,409 each; ~2,000 after the fix, in both themes.

**What would change my mind.** Nothing here, but the honest scope is narrow: my new tests catch stray
semicolons and unconsumed series variables, which is the mechanism I just met, not the category. A rule
could still be killed by an unclosed brace, an invalid property value, or a specificity collision, and
none of my tests would notice. Only rendering catches rendering, and I should stop reaching for
string-level assertions as though they were.

## O.49 · 2026-08-11 · An f-string that emits CSS gets no syntax checking from anything

**Opinion.** The stray `}};` survived because it lives inside a Python f-string. Python validates the
brace ESCAPING and stops there; ruff and mypy see a string literal; the tests asserted on substrings of
the output. Five malformed rules passed the entire toolchain because no tool in it parses CSS.

**Confidence: measured** — the four remaining instances were found by a regex over the rendered output
after the first was found by eye, and every gate had been green before and stayed green after.

**What would change my mind.** A real CSS parser in the test path (`tinycss2` would do it) that fails on
any parse error in the rendered stylesheet. That would subsume both my new tests and catch the failure
modes they miss, and I should prefer it to hand-rolled string assertions — recorded in BACKLOG rather
than done now, because the immediate defect is fixed and this is a change to how the surface is tested.

## O.50 · 2026-08-11 · "The work is happening" is not evidence the scheduler is working

**Opinion.** I signed off `A.61` on a freshly written ingest database and live TLS sockets to NSE. Both
facts were true and neither was evidence for the claim I made, because a manual run of the same script
was in flight and produced identical symptoms. I verified the WORLD had changed, not that the thing I
built had changed it.

**Reasoning.** This is the failure `O.44` exists to prevent, committed on the same day I wrote it. The
correction there was "verify a service by killing it" — I applied that to the dashboard and then, for
the timer, fell back to looking for downstream effects. Downstream effects are the weakest possible
evidence when anything else can produce them, and during active development something else almost always
can. The strong forms are: read the unit's own exit status, or make the unit produce something nothing
else could.

**Confidence: measured.** `ExecMainStatus=216` (EXIT_GROUP) on a unit I had declared working. The log
file it was supposed to write did not exist, and had never existed.

**What would change my mind.** Nothing. The concrete rule I am taking: never accept a side effect as
proof a scheduler ran when a manual invocation of the same code is or was in flight — check
`ExecMainStatus` and the unit's own log, which cost one command and would have caught this immediately.

## O.51 · 2026-08-11 · Logging cannot capture a failure that happens before logging starts

**Opinion.** I added file logging to this unit precisely so nightly failures would not be silent, and
then hit a failure class it structurally cannot catch: sandbox and credential setup happens before
systemd opens stdout, so `216/EXIT_GROUP` wrote nothing anywhere. An empty log is not "nothing went
wrong" and it is not even "logging is broken" — it is a distinct third state I had no reading for.

**Confidence: measured** — the log file did not exist at all, while `systemctl show` reported a real
non-zero `ExecMainStatus`.

**What would change my mind.** Nothing about the mechanism. What it changes is where I look first: the
unit's exit status is authoritative and always present, whereas the log is a thing the unit must survive
long enough to write. I had been treating the log as primary and the status as a formality.

## O.52 · 2026-08-12 · A constructor that cannot fail is a place defects go to hide

**Opinion.** `SmartConnect(api_key=..., access_token=...)` accepts any string and returns an object. It
does not validate, does not call anything, and cannot fail. So the Angel session cache reported success
while handing out clients that answered `AG8001 Invalid Token` on their first real use — because the
stored `jwtToken` still carried the `"Bearer "` prefix that `generateSession` strips internally. Every
unit test passed. They were testing a store, and the store was correct; what was wrong was the thing the
store existed to produce.

The general shape: wherever an object is constructed from credentials without any call being made,
correctness has been *deferred to the first consumer* — and that consumer is usually far away, running
unattended, and reports the failure as somebody else's outage.

**Reasoning.** This is the same class as `O.50` (verifying the world changed rather than that my thing
changed it), but the inversion is sharper. There I accepted weak downstream evidence. Here there was no
downstream evidence at all until I went looking for it, and everything upstream was green. A green
constructor is not weak evidence, it is *zero* evidence, and it is easy to mistake for the strong kind
because an object came back.

**Confidence: measured.** `getProfile` on the rehydrated client returned
`{'success': False, 'message': 'Invalid Token', 'errorCode': 'AG8001'}` while the same call on a
freshly-logged-in client returned the account name. The difference was one `"Bearer "` prefix.

**What would change my mind.** Nothing about the diagnosis. What I would revise is the *fix's* scope: I
normalised the token, which repairs this instance. The stronger fix is that no session cache is
considered working until one authenticated call has been made through a rehydrated client — I wrote that
test for Angel and did not write it for Kite, whose entire login path has no tests at all (BACKLOG,
`A.75`). If the Kite path turns out to have the same defect, this opinion was too narrow, not wrong.

## O.53 · 2026-08-12 · An audit that finds nothing false is evidence about the auditor too

**Opinion.** I audited 58 modules against 93 completed tasks expecting to find work ticked that did not
exist. There was none. What I found instead was the opposite error: seven live modules that **no task
claimed at all**, five of them untested, including the whole Kite credential path. The todo was not
lying about what was done; it was blind to work that had been done without being tracked.

**Reasoning.** I have been treating R.06 (no orphans) as a rule about *dead* code — a file nothing
imports. The real exposure is live code nothing *claims*: it runs in the daily loop, it holds broker
credentials, and because no entry owns it, no entry asks whether it is tested or finished. Dead code is
inert. Untracked live code is load-bearing and unattended. Every one of these seven shipped inside a
commit whose subject cited a different entry ID, which is exactly how it stayed invisible.

**Confidence: measured** for the inventory (grepped per artifact, `systemctl --user list-unit-files` for
the two unit-backed tasks); **reasoned** for the claim that commit-subject drift is the mechanism — it
fits all seven cases but I did not test the alternative that they were simply built ahead of schedule.

**What would change my mind.** A second audit at a later date that finds false-done entries would mean
this one was luck or too narrow. The concrete practice I am taking regardless: when a commit ships a
module outside the entry named in its subject, the extra IDs go in the subject too, and the audit becomes
periodic rather than something run once because the operator asked.

## O.54 · 2026-08-12 · Real data and a second implementation catch different defects, and neither catches both

**Opinion.** `L0.22` shipped with three defects. Two were found by running it on the real depth tape
(zero-padded levels read as quotes at price 0; a micro-price computed from a crossed book) and one by
differential-testing against `tclf` (the quote rule comparing against the touch instead of the midpoint).
Neither method could have found the other's defects, and my unit tests — 30 of them, written first, with
hand-computed expected values — found none of the three.

**Reasoning.** They fail on different axes. Real data catches **what I did not know the input looks
like**: I had no reason to expect a five-level feed to pad absent levels with zeros, or to expect 0.56%
of books to arrive crossed. No amount of reasoning produces those facts. A second implementation catches
**what I got wrong about the definition**: my quote rule was internally consistent, tested, and matched
its own docstring, which is exactly why nothing in my own test suite could see it. A reference disagrees
because it encodes a different reading of the literature, and one of the two readings is wrong.

Hand-written unit tests find neither class, because I write the test from the same misunderstanding I
wrote the code from. My OFI cases were correct only because I derived them from the paper's formula
term by term; the one case I reasoned about informally — "the ask stepped down onto the bid" — I got
wrong, and the implementation was right.

**Confidence: measured.** Three defects, three detection methods, zero overlap, in one engine on one day.

**What would change my mind.** A defect found by unit tests that neither real data nor a reference would
have caught would show the trio is not cleanly separated — I would expect that for pure state-machine
logic (the duplicate-run counter, the eager `UnknownInstrumentError`), where there is no external input
shape to be surprised by and no reference to disagree with. The practice I am taking: for any estimator
of an unobservable quantity, all three are mandatory, and I should look for the reference implementation
BEFORE writing the estimator rather than after — `tclf` would have given me the midpoint rule for free.

## O.55 · 2026-08-12 · A negative sourcing result is worth as much as a found library, if it explains itself

**Opinion.** The `L0.22` sourcing pass found nothing to depend on. The valuable part was not the empty
result but the reason: every public order-book replay project reconstructs from order-by-order or
diff-level messages, because that is the shape raw exchange feeds arrive in. A project that already
holds materialised snapshot rows has no reconstruction problem at all — no insert, cancel or match
logic — only iteration and inference. That explains why the search was empty AND tells me the engine
should be thin on plumbing and dense on estimators, which is what it became.

**Reasoning.** "I searched and found nothing" is nearly worthless — it cannot be distinguished from
searching badly. "I searched, found five candidates, and all five are MBO-shaped for the same structural
reason" is a finding about the problem, and it survives the next time someone asks. `R.17`'s demand for
mechanical evidence applies to rejections, and a structural explanation is the strongest form of it.

**Confidence: reasoned** — I ran the triage on installability and fetched real signatures, but the claim
that this generalises to every LOB library is inference from five, not a census.

**What would change my mind.** A snapshot-native feature library that installs on aarch64 and takes a
book series as input. `mansoor-mamnoon/limit-order-book` is the one candidate that might be it — its
own analytics output already includes imbalance, micro-price and impact — but it needs a CMake/C++
build never attempted here, so it is surfaced in `research/214` for the operator rather than judged.

## O.56 · 2026-08-12 · A store that cannot say "I don't know" is worse than no store

**Opinion.** `L0.31` holds ~30 facts and refuses five of its sixteen families outright. A reasonable
instinct is that this makes it half-built. I think the refusals are the more valuable half. The
alternative — return the nearest known rule — produces a plausible number for every historical query,
and a plausible number is indistinguishable from a correct one at the point of use. A study that
silently priced 2019 options at the 2024 STT rate would look completely normal.

**Reasoning.** The asymmetry is what decides it. A refusal costs one visible failure that someone fixes
by compiling a circular. A substitution costs an invisible error that propagates into every conclusion
drawn from that study, and is discoverable only by someone who already suspects it. This is the same
shape as `O.52` — a broker client that constructs without validating — and the fix is the same: make
the ignorant state a distinct, loud value rather than a plausible one.

**Confidence: reasoned.** The design argument is strong and `research/61` measured that most eras have no
admissible source. What I have NOT measured is how often a consumer will hit a refusal in practice, or
whether that turns out to be so frequent that callers start passing a default around it — which would
reintroduce the substitution one layer up, where the store cannot see it.

**What would change my mind.** Watching `L1.01` be written. If the cost engine ends up wrapping every
`resolve()` in a try/except with a fallback rate, then the refusal was pushed rather than solved, and the
right answer was a fidelity TIER on the returned value rather than an exception. I would rather find that
out from the first consumer than guess now.

## O.57 · 2026-08-12 · Adopting a library as a test oracle is a third option I keep under-using

**Opinion.** Twice today the sourcing pass found one genuinely good library — `tclf` for trade
classification, `portion` for interval reconciliation — and both times the honest verdict was neither
DEPEND nor REJECT. Each was adopted as a **differential oracle in a property test**: not in the runtime
path, because each lacked the state or policy the engine actually needs, but asserted against on random
inputs where the two agree.

**Reasoning.** The binary framing costs real quality. Depending on `tclf` would have forced a streaming
engine through a batch sklearn estimator; rejecting it outright would have left the quote-rule defect it
found — comparing against the touch rather than the midpoint — sitting in the code with thirty passing
unit tests over it. The oracle option gets the reference's correctness without its architecture, and it
is cheap: one property test.

**Confidence: measured**, at least for the value. `tclf` found a real defect on its first run;
`portion` confirmed the interval splitting on 120 generated cases. Two for two.

**What would change my mind.** An oracle that drifts — a library changing its own semantics and turning
a green test red for a reason that is not my defect. That would make the pattern a maintenance cost
rather than a free check. I would keep it regardless for anything implementing a NAMED algorithm from a
paper, where the reference's reading of the paper is the thing I want to check mine against.

## O.58 · 2026-08-12 · A snapshot table is not a rule history until you say what its key is

**Opinion.** The defect the real-data pass found in `InstrumentMasterRuleObserver` was not a bug in the
change detector — it was an under-specified key. `tradingsymbol` alone is not an identity: RELIANCE is in
every instrument-master snapshot twice, at a 0.10 tick on NSE and 0.05 on BSE. Any "derive history by
diffing consecutive snapshots" design is silently wrong until the query key is the FULL key the exchange
actually keys on. Refusing an ambiguous day is the right response, not picking one row.

**Reasoning.** The wrong-key failure produces a plausible artefact rather than an error: two values
stamped with one date read as a rule that changed and changed back within a day. Here it surfaced only
because `MarketRuleRecord` validates its own interval and a zero-length interval is illegal — the
type caught what the query did not. Swept the full table afterwards to size it: **1,668 symbol-days are
ambiguous under a symbol-only key, and 0 are ambiguous once `segment` is pinned**, across 227,535 rows
and 106,436 symbols. So the discipline is not merely sufficient for RELIANCE; it is sufficient
everywhere in the table, and the residual ambiguity is exactly zero.

**Confidence: measured.** The 1,668/0 split is a query I ran against the real store, not an estimate.
What I have NOT measured is whether `segment` is the complete key for OPTIONS rows, where expiry and
strike are part of the instrument identity and the trading symbol already encodes them.

**What would change my mind.** A non-zero count on the segment-pinned sweep after the options universe
grows a second exchange, which would mean the key needs `exchange` as well as `segment`. The sweep is one
query and belongs in the daily run rather than in my memory — logged in BACKLOG as such.

## O.59 · 2026-08-12 · When the quantity is unobservable, the estimator's honesty matters more than its accuracy

**Opinion.** `L0.32` cannot measure the host-minus-exchange offset. It can bound it from above, and it
can measure the RATE at which it changes. The temptation was to report the bound as "the offset" and move
on — every consumer wants a number, and 31.5 ms looks like one. I think the right call was to name the
field `apparent_offset_seconds`, carry `offset_upper_bound_seconds` beside it, and put the reason in the
page footer where a reader meets it. The engine is more useful bounded and labelled than precise and
wrong.

**Reasoning.** The skew is a difference of offsets, so the unknown constant cancels and drift IS
measurable — to 0.16 ppm on one session and 16.3 ppm on the next. That asymmetry is the whole design: the
thing the plan asked for ("detect drift") is measurable, and the thing that looks easier ("what is the
offset") is not. An engine that reported both with equal confidence would be wrong exactly where a
consumer would rely on it — correcting a timestamp.

**Confidence: measured** for the arithmetic (the LP bound is provable and the tests assert the line never
sits above an observation on random inputs), **reasoned** for the judgement that consumers are better
served by a bound than a point estimate. I have not yet watched a consumer use it; the depth classifier is
the first, and it uses the NTP bracket rather than the feed fit precisely because the feed fit is a bound.

**What would change my mind.** A consumer that cannot act on a bound and ends up hardcoding a midpoint to
get past it. That would mean the honesty was pushed one layer out rather than delivered, and the right
answer would have been a point estimate with a stated error bar in the same object.

## O.60 · 2026-08-12 · Real data breaks different things than tests do, and this time it broke the two parts I was most confident about

**Opinion.** Both defects in `L0.32` were in the parts I would have signed off on from the tests alone:
the Marzullo sweep (a textbook algorithm) and the drift series (an obvious "take the minimum per minute").
Both were correct as algorithms and wrong as measurements. Marzullo returned an "intersection" of two
disjoint intervals because I never asked what a majority of two means; the floor series alerted on
illiquidity because I never asked what Kite's `exchange_timestamp` means for a stock that has not traded.

**Reasoning.** A hermetic test asserts the code does what I thought the data was. Only the data can
correct what I thought the data was. Both failures were about the SEMANTICS of an input — a field that
means "last trade time" rather than "packet time", and a server count that means "no majority" rather
than "two opinions" — and no amount of property testing over synthetic inputs generated from my own
assumption can surface an assumption error.

**Confidence: measured.** Nine spurious alerts and one fabricated consensus, both observed on the first
real-data run, both now covered by a test that encodes the real case.

**What would change my mind.** Nothing about the conclusion; the open question is whether it generalises
into a habit worth naming — run every new engine on real data BEFORE writing the second half of its
tests, so the tests encode the data's semantics rather than mine. I am inclined to that, and `R.05`
already requires the real-data pass; what it does not require is that the pass happen EARLY.

## O.61 · 2026-08-12 · The adversarial pass earns its cost on the parts that already passed their tests

**Opinion.** `L0.32` had 70 passing tests, a written spec, ruff and mypy clean, and a real-data pass, and
a fresh adversarial subagent still found seven reproduced defects — two of which would have made the
engine useless in opposite directions (refusing constantly on a false alarm rate of 100%, and never
refusing because the threshold was unreachable). `R.23(c)`'s review step is not a formality to be skipped
when the tests are green; green tests are the condition under which it is most valuable, because they
mean the remaining defects are the ones my own assumptions cannot see.

**Reasoning.** Every finding was in a place where I had written a CLAIM in a docstring — "upward outliers
cannot move this fit", "a rigorous upper bound", "a threshold that means a 1-in-100 false alarm rate".
The tests asserted the behaviour I believed followed from those claims; nobody had attacked the claims
themselves. An adversary instructed to REFUTE reads a confident docstring as a target, which is exactly
the reading my own tests could not produce.

**Confidence: measured.** Seven findings, all with reproducers I re-ran myself before fixing. And one fix
was independently corroborated: restricting the LP objective to the hull moved the feed-derived skew from
+16.43 to -6.06 ppm against chrony's -6.917 — two measurement paths sharing no code agreeing to 0.85 ppm,
where they had disagreed by 23.

**What would change my mind.** A review pass that returns only style opinions or unreproduced
speculation. The value here came from the instruction to RUN the attack and report only what reproduced;
a review that cannot do that is worth much less, and I would rather spend the tokens on more tests.

## O.62 · 2026-08-12 · When the data is perishable, acquisition outranks design

**Opinion.** `L0.33` needed cross-broker quotes, which exist only while two brokers are quoting. I built
and started the capture at 14:32 IST — before the spec, before the engine, before the operator interview
had even been answered — and wrote everything else against what it was collecting. I think that ordering
was right and I expect to repeat it: for a perishable measurement, the capture is the irreversible
decision and the engine is not.

**Reasoning.** The engine can be rebuilt a hundred times from a tape; a tape cannot be rebuilt at any
price after 15:30. By the close the capture held ~100,000 rows across 67 instruments, which is the only
reason the same day could also produce a measured identifiability finding, a measured 92.5% exact-
agreement rate, and 603 observed crossed books. Had I specced first in the usual order, the spec would
have been finished around 15:20 and the engine would have been built against nothing.

**Confidence: measured** for the value of the data, **reasoned** for the general rule. The cost is real
and I am not pretending otherwise: the capture script was written fast, and its first version stored
Kite's naive-IST timestamps as UTC — a defect that a spec-first order might have caught on paper.

**What would change my mind.** A capture whose schema turns out wrong in a way that makes the data
useless — then the rush would have bought nothing and cost a session. The mitigation is to store RAW
per-source rows and never a merged product, which is what this one does: a wrong interpretation can be
recomputed, a discarded observation cannot.

## O.63 · 2026-08-12 · Property tests find design errors; unit tests find code errors

**Opinion.** The `L0.33` fusion was wrong in a way no unit test would have caught, because every unit
test I would have written asserted behaviour I believed followed from the design. The property test —
"generate brokers whose noise is known, and check the consensus beats them" — failed, and the failure was
not a bug in the implementation of inverse-variance weighting. It was that with two sources the variances
are not identifiable at all. That is a statement about the mathematics, and only a test that measured an
OUTCOME rather than a behaviour could have said it.

**Reasoning.** Unit tests encode the author's model of the problem; a property test encodes the problem's
own criterion of success. Where the model is wrong, only the second can disagree with you. The same shape
recurred twice more the same day: the three-cornered-hat replacement then revealed that pooling paise
across a universe spanning three orders of magnitude made the estimate unusable, and that quiet sources
sit below the estimator's resolution — both found by measuring, neither by asserting.

**Confidence: measured.** Three design-level findings from one property test and its successors, against
a suite of unit tests that all passed throughout.

**What would change my mind.** Nothing about the value; the open question is cost. These property tests
run for minutes because each observation writes to SQLite, and a suite nobody waits for is a suite nobody
runs. If they get slower I would keep the property and shrink the data, not drop the property.

## O.64 · 2026-08-12 · A rounding call is a measurement decision, and I keep treating it as formatting

**Opinion.** The critical defect in `L0.33` was `round(price)` stored against an unrounded comparison —
one call, written to keep a dict tidy, which silently destroyed the single most load-bearing measurement
in the engine (29,030 of 86,306 unchanged quotes reported as movement). This is the second time in one
day that a numeric-representation choice produced a wrong measurement rather than an ugly one: the
depth-tape work already established integer paise precisely because float rupees make equal prices
compare unequal. I think the rule is that any rounding on a value that will later be COMPARED is a
measurement decision and needs the same justification as a threshold.

**Reasoning.** Rounding is invisible in review because it looks like presentation. It is not: it changes
the equivalence classes of the data, and every downstream equality test inherits that. The tell in both
cases was the same — a comparison between a stored value and a fresh one that had been through a
different number of transformations. Neither the unit tests nor mypy can see it, because both sides are
floats and both answers are plausible.

**Confidence: measured.** Two independent occurrences, both quantified on real data, both changing a
reported rate by a factor rather than a margin.

**What would change my mind.** Nothing about the diagnosis; the open question is the remedy. A lint rule
banning `round()` near a comparison would be mostly noise. The cheaper discipline is the one that caught
it here: measure the rate on real data and ask whether the number is plausible — a 0% frozen rate on a
feed known to freeze should have been as loud as an exception.


## O.65 · 2026-08-12 · A blocker is a claim about the world and deserves the same evidence as a finding

**Opinion.** I recorded "Upstox blocked on an expired token" in BACKLOG after testing ONE of the two
Upstox tokens in `.env`. The other one works and returns full depth. The cost was not the minute it took
to discover — it was that the blocker had already propagated into a plan entry, a spec section, a commit
message and a sign-off, each of which stated as fact that this host could reach only two brokers. Under
`R.17` an OSS rejection needs mechanical evidence; a BLOCKER needs exactly the same standard and I have
been holding it to a lower one.

**Reasoning.** A blocker is load-bearing in a way a finding is not: it stops work, it justifies scope
reduction, and it survives in the record until someone deliberately re-tests it. `A.78` re-tested two
inherited blockers this week and found one of them stale for a similar reason. The asymmetry is that
confirming a blocker costs one command and believing one costs a session.

**Confidence: measured.** One command distinguished "blocked" from "working" here, and the same pattern
appeared in `A.78`.

**What would change my mind.** Nothing about the standard. The open question is the mechanic: the honest
version is that every recorded blocker names the exact command that demonstrates it, so re-testing is
mechanical rather than archaeological. I have started doing that in BACKLOG entries and should apply it
retroactively to the ones already there.


## O.66 · 2026-08-12 · An implausible number is a finding; a plausible one is an assumption

**Opinion.** With two brokers, 1.2% of groups showed a crossed touch and I wrote "one book is stale" into
a spec, a plan entry and a commit message without checking a single row. The third broker pushed the same
statistic to 10-44%, which was too implausible to write down, so I looked — and the cause turned out to
be NSE's ±3% price band, which had been producing the 1.2% as well. The lesson is not "check your
numbers"; it is that a number small enough to be plausible gets no scrutiny, so the plausible range is
exactly where wrong explanations survive.

**Reasoning.** Both readings were consistent with the two-broker data. What distinguished them was one
query — the shape of the offending rows — which I only ran when the number became embarrassing. The
identical 10.07% rate on two independent brokers was the tell, and it was available from the first
session; nothing about the third feed made it more discoverable, only more urgent.

**Confidence: measured.** 25,761 rows, bid at last +3.03% and ask at −2.95%, 40,407 shares at the touch
against 281 normally, concentrated in six minutes. Correcting it moved the resolved rate from 86.29% to
95.97%.

**What would change my mind.** Nothing about the diagnosis. The practical question is which plausible
numbers deserve the query, and my current answer is: any statistic that a DESIGN DECISION rests on. The
"crossed implies stale" reading justified refusing 1.2% of the tape, which is exactly the kind of claim
that should have had to show its rows.

## O.67 · 2026-08-12 · A wrong answer that type-checks is worse than a crash, and only an independent witness catches it

**Opinion.** Loading 33 years of bhavcopy produced two failures on the same run. One was a NULL date that
hit a NOT NULL constraint and stopped the load — loud, immediate, trivially diagnosed. The other was
`cash_2020-07-13.csv.zip` writing its dates as `13-Jul-20`, which `%Y` parses as the year 20 AD: 2,001
rows loaded as `0020-07-13` with no exception, no null, and no test failure anywhere. I would rather have
ten of the first than one of the second, and the only thing that caught it was comparing the parsed date
against the file's own NAME — evidence that does not come from the file's contents.

**Reasoning.** A format list can never close this class. Every date format I add makes the parser accept
MORE inputs, and the failure here was acceptance, not rejection: the answer was well-formed, in range,
and wrong. What distinguishes a correct parse from a plausible one is agreement with an independent
witness, and archives usually have one — a filename, a manifest, a sibling file, a checksum. This project
already relies on the same move elsewhere: `L0.32` brackets the host clock against NTP because the feed
alone cannot say whether it is late, and `L0.33` needs three brokers because two cannot say which is
noisy.

**Confidence: measured.** Both defects reproduced on real files; the invariant catches both and now has a
test each. I have NOT measured how many other rows across 193 million disagree with their file's name —
the reload will say, and that number is itself a data-quality finding.

**What would change my mind.** A large disagreement count, which would mean the invariant is too strict
(a file legitimately carrying a prior session's rows — a correction file, say) rather than the data being
wrong. Then the rule becomes "quarantine and report" rather than "quarantine silently", which is what it
already does; what would change is whether those rows are recoverable rather than discarded.

## O.68 · 2026-08-12 · A test that checks half a pipeline certifies half a pipeline

**Opinion.** The differential oracle for `L0.34` was the best idea in that slice — two readers, one
obviously correct and slow, one fast, checked against each other on real files — and it still let two
entire trading sessions disappear. Not because the idea was wrong, but because I applied only the FIRST of
the fast path's three filtering stages before comparing. The readers diverged in the second stage. The
test passed, I signed off, and the store reported itself complete while missing 33,389 rows.

**Reasoning.** A differential test's guarantee is exactly as wide as the code path it exercises, and mine
was narrower than the code it was named after. Worse, the narrowing was invisible: the test looked
complete, it used real files, and it compared cell-for-cell. What it did not do was run what the loader
actually runs. The general form — the one I want to remember — is that comparing two implementations only
proves anything about the stages you put on BOTH sides of the comparison.

**Confidence: measured.** The extended test, running all three stages, failed immediately on the two files
that had been silently lost, and on the five-dump 2003 file. The same test with one stage had passed on
six files for two full load cycles.

**What would change my mind.** Nothing about the diagnosis. The open question is how to make the omission
visible rather than relying on care: the honest answer is that the loader should expose ONE function that
does all of its filtering, so a test physically cannot apply a subset of it. I have not done that — the
three stages are still separately callable, and the test now calls all three by discipline rather than by
construction. That is a weaker guarantee than it looks, and it is recorded as such.

## O.69 · 2026-08-12 · A rate table that is one day old is not a rate table I can trust

**Opinion.** The three defects the `L1.01` sourcing pass found in `L0.31`'s rate history — an options
exchange charge that is really a cash rate (~12× understated), a cash stamp duty scoped to all cash but
carrying the delivery-only rate (5× overstated intraday), and cash STT missing entirely — were all seeded
THIS MORNING, by me, in a slice I signed off. The pattern is not carelessness about rates; it is that
`L0.31`'s job was to build the STORE, and the facts were seeded as a demonstration that it worked. A fact
seeded to prove a mechanism is not a fact anybody checked against its source.

**Reasoning.** Every one of the three is invisible from inside `L0.31`: the store resolves them correctly,
reports them covered, grades them, and its 34 tests pass — because the tests check RESOLUTION, and the
defect is in the CONTENT. Nothing in a point-in-time rule store can tell you a rate is the wrong rate. Only
a consumer that knows what the number means can, which is why the defects surfaced the moment the first
real consumer went looking. The general form: **coverage is not correctness, and a store reporting a family
as covered is reporting that it has a value, not that the value is right.**

**Confidence: measured** for the three defects — each was checked against a primary circular whose PDF text
was fetched (NSE/FA/64232, NSE/FA/73061, Finance Bill 2026 Clause 143), and the arithmetic reconciles
(₹306.99 + ₹0.01 = ₹307 each side, cash; ₹3,503 + ₹50 = ₹3,553, options). **Reasoned** for the claim about
WHY they happened — I am diagnosing my own earlier slice from its shape, not from a record of my reasoning
at the time.

**What would change my mind.** If the other 11 seeded families turn out clean when their first consumer
arrives, then this was three bad rows rather than a systematic property of demonstration-seeding, and the
lesson shrinks to "cost rates specifically need a consumer to check them". I have not audited the other
families and am not claiming they are wrong — only that nothing has checked them either. That audit is the
honest next move and it is not in this slice.

## O.70 · 2026-08-12 · When the law is silent, the engine must be silent too — not average

**Opinion.** No circular mandating how statutory levies round could be read, so the engine rounds nothing
on statutory lines and models rounding as a per-BROKER rule instead. The tempting alternative — adopt
Zerodha's published "nearest rupee, ≥50 paise up" as the convention — would have been wrong in a specific
and expensive way: it would convert one broker's billing practice into a regulatory fact, and every future
reader of the code would see a rounding rule and assume it was sourced.

**Reasoning.** `R.23(e)` permits a constant when it is a physical or regulatory fact with a source. A
broker's rounding practice IS sourced, but it is a source for a fact about that broker, not about the levy.
Putting it in the statutory layer would launder the provenance — the value would be right for Zerodha and
silently wrong for every other broker, with nothing in the code saying so. Keeping it in the broker
schedule costs one extra field and makes the reconciliation ledger able to MEASURE each broker's real
rounding, which is strictly more than assuming one.

**Confidence: reasoned.** The absence of a rounding circular is a failure to fetch, not a proof of absence
— two candidate NSE circulars (INSP61999, FATAX63809) timed out on repeated attempts, so "no rule exists"
is exactly what I must NOT conclude. What I am confident about is the design consequence: given uncertainty
about where a rule lives, the layer with the weaker claim should hold it.

**What would change my mind.** Fetching either circular. If NSE does mandate a rounding precision for
statutory levies, it belongs in the rule store as a dated fact with its own family, the broker field
becomes a deviation-from-mandate rather than the primary rule, and any broker whose contract notes
disagree with the mandate becomes a finding rather than a configuration.

## O.71 · 2026-08-12 · A charge of zero is a more dangerous output than a charge that is slightly wrong

**Opinion.** The `L1.01` real-data pass priced 3,416 real symbols and found one — DHARAN, closing at 16
paise — where every charge line is a fraction of a paisa and the billed total is exactly zero. My first
instinct was to add a minimum-billable floor so the number would never be zero. That would have been
wrong: the zero is CORRECT, and inventing a floor would have put a fabricated charge into a statutory
line. What the case actually needs is for the zero to be *visible* to whatever divides by it.

**Reasoning.** A gate that computes `edge_bps / cost_bps` clears any hurdle when the cost is zero, so a
sub-paisa symbol would look like the best trade in the universe rather than the least tradeable one.
The honest fix separates two questions that had been sharing one number: what the broker BILLS (zero,
correct, reconcilable against a contract note) and what the trade COSTS (1.69 paise, also correct, and
the right denominator). So `RoundTripCost` now carries both, plus `bills_as_free` so a consumer cannot
reach the dangerous case without seeing it. The general form: **when a correct answer is dangerous for a
downstream consumer, fix the interface, not the answer.**

**Confidence: measured** on the finding — one symbol in a real 3,416-symbol cross-section, and I read its
lines individually. **Reasoned** on the design: I have not yet built `L1.02`, so my claim about how the
gate will consume this is an argument about a consumer that does not exist.

**What would change my mind.** If `L1.02` turns out to need a single number rather than a pair, the right
answer is for the gate to always use the exact figure and treat the billed one as reconciliation-only —
which would make `bills_as_free` a reporting flag rather than a branch a caller must handle. I would also
revisit if real contract notes show brokers applying a minimum charge, because then the floor is a real
broker fact and belongs in the broker schedule — where, unlike in the statutory layer, it could be cited.

## O.72 · 2026-08-12 · Coverage is a property of a store; correctness is a property of a consumer

**Opinion.** `L0.31` reported all three cost families as covered, resolved them cleanly, graded them, and
passed 34 tests — while two of the three were wrong and a third was missing. This is not a criticism of
the store. It is that **no point-in-time rule store can tell you a rate is the wrong rate**, and building
one and believing its coverage report is a category error I made this morning and corrected this
afternoon only because a consumer arrived that knew what the numbers meant.

**Reasoning.** The store's tests check resolution — the right record for the right date at the right
belief time — and every one of them passes just as happily on a wrong value. The information needed to
catch a mis-scoped rate is not in the store at all; it is in the arithmetic of the thing that uses it.
The check that actually caught the exchange-charge defect was not a test I wrote, it was reconciling
two seeded lines against a total the circular states in words: Rs 306.99 plus Rs 0.01 equals Rs 307. That
is a consumer-side check, and it is now a parametrized test across both eras and three segments.

**Confidence: measured.** Three defects, each verified against a primary circular whose text was fetched,
and each now pinned by a regression test that fails on the old value.

**What would change my mind.** Nothing about the diagnosis. What I am unsure about is the remedy at
scale: eleven other seeded families have still never been checked by a consumer, and I have logged that
rather than fixed it. The tempting general rule — "every fact needs an arithmetic cross-check" — is not
achievable for families like expiry weekday where no independent total exists to reconcile against. For
those the honest answer may be that coverage is all we get, and the dashboard should say so differently.

## O.73 · 2026-08-12 · Hunting for OSS packages has not paid; hunting for FACTS has paid every time

**Opinion.** The operator asked whether I should stop the OSS/GitHub sourcing sweeps and just build
everything bespoke. My answer is **yes for code, no for facts**, and the two have been getting bundled
together under one rule when their measured value could not be more different.

**Reasoning, from this session's own numbers.** Two full mechanical sourcing passes ran today, ~35
candidates installed and executed on this host, across the cost engine and the fill model. Result:
**zero vendored, zero depended on, one deferred** (`hftbacktest`, and only for a maker-order concern
two slices away). Both passes ended in the same sentence — "bespoke is mechanically justified". That
is not bad luck. It is structural: exact-`Decimal` integer-paise money, point-in-time provenance with
refusal, NSE-specific statute, and an aarch64/glibc-2.34 host together exclude essentially the whole
ecosystem. Across five slices now, the sourcing verdict has been "build it" every single time.

The FACT research in the same session is the opposite story. It caught the SEBI turnover fee being
charged on notional rather than premium (which `research/164` and `b28` both had wrong), the
exercised-option STT basis moving in 2019 rather than 2024, two seeded rates that contradicted their
own citations by 100x and 10x, and — for the fill model — that the `average_traded_price` field looks
usable and carries 1,012–5,684 bps of rounding noise, that Kyle's lambda is not estimable from this
tape (R² 0.001), and that 82% of snapshots exhaust the visible book at 1e-3 of session volume. **Not
one of those was derivable from my memory, and every one of them would have been silently expensive.**

So the distinction I would draw is not "research vs no research". It is: **I can write any code this
project needs, but I cannot know what the exchange charges.** Code I get wrong fails a test. A rate I
get wrong from memory produces a plausible number forever.

Three things I would keep regardless of the sourcing decision: (1) heavyweight numerical libraries
already depended on — scipy, statsmodels, duckdb, polars, pyarrow — because "build everything" must
never mean reimplementing a solver or a Parquet reader; (2) `R.17`'s mechanical-evidence standard for
the rare occasions something IS evaluated, since README-based rejection is how a stale library gets
adopted; (3) a cheap check of what the venv already has, which costs almost nothing.

**Confidence: measured** on the sourcing verdicts — 35 candidates, installed and run, zero adopted,
and I have the failure lines. **Measured** on the fact-research value — each finding was verified
against a primary source or reproduced on real data. **Reasoned** on the generalisation to future
slices: five slices is a consistent pattern but it is not proof, and the next slice could be the one
where a mature library fits.

**What would change my mind.** A slice whose problem is a well-solved commodity — a numerical solver,
a statistical estimator, a wire-format parser, an ML model — where the ecosystem is mature and
NSE-specificity does not bite. For those the search is cheap and the payoff is real, and I should
still run it. What I would stop is the reflexive 30-candidate sweep on problems that are obviously
bespoke by construction, which is most of what this project builds.

## O.74 · 2026-08-12 · The gate works, and the first thing it did was veto everything

**Opinion.** The `L1` cost filter is now wired end to end — strategy decision, priced claim,
modelled hurdle, verdict — and on its first run over 120 real instruments from the depth tape it
vetoed **every single signal the strategy produced** (17 of 17; the other 103 instruments produced
no signal at all). Claimed edges came in at 0.0–3.1 bps against hurdles of 13–68 bps. That is a
real result about the pipeline and a **non-result about the strategy**, and the two must not be
confused.

**Reasoning.** What is genuinely established is that the path functions and that the gate changes
behaviour: before this, nothing in the system could stop a trade on cost grounds, and now the
default outcome is refusal. That is the correct default. It is also the SEBI finding in miniature —
70% of intraday traders lose and loss-makers spend 57% of their losses on costs — arriving as an
engineering fact rather than a quotation.

What is NOT established is that intraday mean reversion has no edge. In this probe I fed the engine
**mid-price snapshots seconds apart** as its closes, because that is what the depth tape holds. The
engine is designed for a bar series; over seconds, the deviations it measures are genuinely tiny, so
the edges it claims are tiny, so of course they lose to a 13-bps hurdle. **I tested the plumbing on
the wrong timescale and got a number that looks like a verdict on the strategy.** A fair test needs
the engine fed the bars it was built for, from `L0.03`'s bar store, over a horizon where a
mean-reversion move is measured in tens of basis points rather than tenths.

**Confidence: measured** that the path runs and that these 17 signals were vetoed for these stated
reasons — I ran it and have the per-decision arithmetic. **Reasoned** that the timescale mismatch
explains the tiny edges; the alternative explanation, that the strategy simply has no edge at any
horizon, is not excluded by this run and would be a much bigger claim. **Judgement only** on which
is more likely, and I would not act on that judgement either way.

**What would change my mind.** Running the same path with daily or minute bars from the bar store
rather than depth-tape mids. If edges remain an order of magnitude under the hurdle at the horizon
the strategy was designed for, that IS a finding about the strategy, and the right response is not
to loosen the gate but to record that this family does not clear costs on this universe — which is
exactly the kind of conclusion `L2`'s gatekeeper exists to reach, and exactly the kind that a system
without a cost gate never reaches at all.

---

### CORRECTION · 2026-08-12 (same day) · the timescale was not the explanation

I ran the test I named above, and **the reasoning in the paragraphs above is wrong**. The original
is left standing because the way it was wrong is more instructive than the conclusion.

Fed real daily closes from the `L0.34` archive, the median claimed edge came out at **4.81 bps**
against the 0.0–3.1 bps of the seconds-scale run. Barely more than 1.5x. By my own stated criterion
that was a finding about the strategy, and I very nearly recorded it as one.

It is not. Before writing it up I decomposed the gap, and the cause was neither the timescale nor
the strategy: it was **`edge_from_mean_reversion_decision` itself**. That function computed the
expected move as `|deviation| - band` — the excess beyond the entry trigger. The band is the 90th
percentile of the same deviation distribution, so the excess beyond it is small **by construction**:
median deviation 2.44σ, median band 1.96σ, excess 0.48σ. The formula was structurally incapable of
reporting a large edge, whatever the market did.

That is not a hypothesis about mean reversion. It is an unstated **exit rule** — "the position
unwinds to the band edge and stops" — sitting inside a valuation formula, which is precisely the
defect `R.03` exists to catch and which I did not recognise when I wrote it. Assume exit at the mean
instead and the identical signal claims 5.9x more, with nothing in the data to arbitrate between
them.

**What the archive actually says**, from 150,364 measured reversion events across 379 symbols,
strictly causally (band from past deviations only, outcome from future bars only):

| horizon | mean captured | t | vs `NSE-MIS` floor 8.9 bps |
|---|---|---|---|
| 1 bar | 34.1 bps | 5.42 | clears |
| 5 bars | 40.4 bps | 5.69 | clears |
| 10 bars | 27.7 bps | 2.66 | clears |

So the strategy family **does** clear the intraday floor in gross terms at its own horizon, by about
four times, and the seconds-scale run had understated it roughly seven-fold. My "reasoned" call that
the timescale explained the tiny edges was directionally right and quantitatively irrelevant: the
timescale accounts for maybe 1.8x of a 7x gap.

**And the per-bucket structure is the part I would have missed entirely** had I stopped at the
pooled number:

| deviation | 1 bar | 3 bars | skew (mean/median) |
|---|---|---|---|
| 2.0σ | +203.6 bps | +164.3 | **19.7** |
| 2.5σ | +37.0 | +29.2 | 6.8 |
| 3.0σ | +24.7 | +22.7 | 3.3 |
| 3.5σ | **−2.1** | **−17.5** | −0.6 |
| 4.0σ | +16.6 | +32.5 | 3.5 |

The edge is **not monotone in deviation depth and changes sign**. Deeper is not better; 3.5σ moves
continue rather than revert at short horizons. Any formula linear in depth — which the replaced one
was — is therefore wrong in *sign* somewhere, and no amount of recalibrating its coefficient repairs
that. Separately, the 2σ bucket's mean is carried almost entirely by a thin tail (skew 19.7): the
typical trade there earns almost nothing while a handful carry the result, which is a materially
different risk proposition from an edge that arrives reliably, at identical mean.

**What I got wrong, precisely.** Not the conclusion — the *method*. I diagnosed a suspicious number
by reaching for the most available explanation (the timescale, which I already knew was
compromised), and I wrote that explanation into an opinion with a falsification test attached. The
test would have "passed" in the sense of confirming a finding, and the finding would have been
false, because both the probe AND the fair test shared a broken formula that neither could see. **A
falsification criterion that reuses the suspect component tests everything except the thing most
likely to be wrong.** The decomposition that actually found it — comparing two candidate exit rules
against each other on the same data — cost one query and was not part of my plan.

**Confidence: measured** on every number in the tables; they come from a strictly causal walk whose
look-ahead guard is asserted against real archive dates. **Reasoned** that the excess-over-band
formula was the dominant cause rather than a contributing one, on the strength of the 5.9x
decomposition. **Caveat I am carrying openly:** these standard errors are plain `s/√n`, and reversion
events overlap in time and cluster across instruments on the same day, so the true effective sample
is smaller than 150,364 and every `t` above is optimistic. The 2.66 at ten bars would likely not
survive a cluster-robust estimator. Logged as `M10`.

**What would change my mind now.** A cluster-robust standard error that collapses the t-statistics
below 2 — in which case the honest reading reverts to "no measurable edge", and I would want it
recorded that the point estimates were real but the confidence was not. Also: these are **gross**
captures. A round trip pays the hurdle at entry and again at exit, so a 40-bps capture against an
8.9-bps floor is closer to 40 against ~18 than the table suggests, and the 3.5σ and 10-bar cells
have no margin left at all.

## O.75 · 2026-08-12 · A falsification test that reuses the suspect component tests nothing

**Opinion.** The most dangerous verification I can write is one whose failure mode I have already
named. `O.74` named the timescale, attached a falsification criterion to it, and I ran that test.
It "worked" — it produced a number, the number was bad, and the bad number pointed exactly where I
had predicted. Had I written it up there, `F01` would have closed around a **false finding**, with
a real-data test, a recorded confidence level and a passing suite standing behind it.

**Reasoning.** The probe and the fair test differed in one input (depth-tape mids versus daily
bars) and shared everything else — including `edge_from_mean_reversion_decision`, which was the
actual defect. A test varying only the variable I suspected could not distinguish "the timescale
was wrong" from "the formula is structurally incapable of reporting a large edge", because both
produce a small number in both runs. The decomposition that found it took one query and was not
in my plan: price the *same* events under two different exit rules and compare. The moment the
two disagreed by 5.9x, the formula — not the market — was obviously the variable carrying the
result.

The general shape: **when a measurement comes out surprising, the first thing to vary is the
measuring instrument, not the subject.** I did the opposite, and I did it while believing I was
being rigorous, because the instrument was something I had written and tested and therefore did
not think of as a hypothesis. It was one. `|deviation| - band` is a *claim* about where a position
exits, and it was never labelled as one, so it was never on the list of things that could be wrong.

This also explains why `R.23(c)`'s "review from somewhere that did not write the assumptions" keeps
paying: `F01`'s review found four criticals each protected by a passing test I had written. Same
mechanism, one layer down. A test I write encodes my model; when my model is wrong, the test
certifies the error.

**Confidence: measured** that this specific near-miss happened, with both numbers on record.
**Reasoned** that the general rule follows — this is one instance, and I am generalising from it
because the same mechanism produced the `F01` review findings independently, which is a second
instance rather than a restatement.

**What would change my mind.** Cases where varying the instrument first is wasteful — a
well-established measurement with a long track record probably should not be re-derived every time
it says something unwelcome. The rule I would actually defend is narrower: **any component I wrote
during this same feature is a hypothesis, not an instrument, and must be varied before its output
is believed.** `edge_from_mean_reversion_decision` was four days old when I trusted it to
adjudicate a strategy.

## O.76 · 2026-08-12 · The strategy has an edge; the confidence in it is the weak part

**Opinion.** Intraday mean reversion on NSE cash clears the transaction-cost floor at its own
horizon — 40.4 bps mean capture at five bars against an 8.9-bps `NSE-MIS` floor — but I would not
size a position on the strength of that number today, and the reason is not the point estimate.

**Reasoning.** Three things about the measurement are weaker than the headline:

1. **The standard errors are wrong in a known direction.** Plain `s/sqrt(n)` on events that overlap
   in time and cluster by session. The true effective sample is smaller than 150,364 and every `t`
   is inflated. `M10`. The ten-bar cell at `t = 2.66` is the one I expect to disappear.
2. **Half the cells are not distinguishable from zero anyway** — 10 of 20 — and three of them
   measurably CONTINUE rather than revert. The tradeable surface is much smaller than "the strategy
   works", and the gate now refuses per cell, which is the correct shape of that answer.
3. **The strongest cells are tail-carried.** 2σ at one bar has a mean nearly twenty times its
   median. Realising that mean needs a trade count and a drawdown tolerance nobody has sized.

Against that: the sign structure is stable and interpretable, the causality guard is asserted on
real dates rather than documented, and the whole thing is now refutable — a nightly re-fit will
show drift, and a cluster-robust estimator will either survive or not.

**Confidence: measured** on the point estimates. **Judgement only** on whether this is tradeable,
and my judgement is not yet — not because the edge looks absent but because the uncertainty is
mis-stated in a direction that flatters it, and I would rather fix `M10` than act on a `t` I know
to be optimistic.

**What would change my mind.** A cluster-robust fit leaving the five-bar cells above `t = 3`. That
would move this from "measured but not trusted" to "worth sizing", and `L2`'s gatekeeper is where
that decision belongs, not here.

---

## O.77 · 2026-08-13 · The market-order prohibition binds this system, and reading it any other way would be self-serving

**Opinion:** NSE/MSD/67753's "Algo orders with order type as Market Order are not permitted" applies
to every order this project places, and MARKET must therefore be unreachable in code rather than
merely discouraged.

**Reasoning:** the operator's instruction for `F02` was to build the whole taxonomy and use each
member where it is best, so the incentive was to read the restriction narrowly. The text does not
support that: it restricts *algo orders*, and every order here is API-originated and carries the
exchange's algo tagging (`docs/research/223` §4), including sub-threshold flow. The one reading that
would exempt this system — "only registered above-threshold algos are algo orders" — is contradicted
by the same circular's tagging clause, which covers orders *below* the threshold explicitly.

**Confidence: reasoned.** The circulars were read in full from their own PDFs, but no regulator has
been asked about this specific case, and market orders are not a capability the system loses much by
refusing: a marketable limit does the same job and is the only version `F01` can price in advance.

**What would change my mind:** an exchange FAQ or broker confirmation that a sub-threshold retail
self-algo may send MARKET orders. The fact table takes that as a dated availability change with no
code edit.

## O.78 · 2026-08-13 · An identity computed from the decision beats any identity handed out by a store

**Opinion:** for a broker with no idempotency key, the only workable client order id is a content
hash of the decision itself — not a counter, not a UUID, not a database-assigned id.

**Reasoning:** the failure this protects against is a crash between "I decided" and "I know what the
broker did". A stored mapping dies with the process; a recomputable name does not. Of the five
systems in `docs/research/224`, the two with restart-stable identity behave correctly here and the
three without it either resend or lose the order. The cost is a real one and is stated in the
module: two genuinely independent decisions identical in every field and in the same microsecond
collapse to one intent.

**Confidence: measured**, in the narrow sense that the stateful property test drives submit / crash
/ restart / reconcile in arbitrary orders and the invariant holds; not measured against a real
broker, because the real-fill probe is deferred (`A.99`).

**What would change my mind:** a strategy family that legitimately needs two identical clips in the
same microsecond and cannot express them as one larger clip. I have not seen one.

## O.79 · 2026-08-13 · The state machine was worth writing by hand; the rate limiter was not

**Opinion:** `transitions` was the right library to reject and `pyrate-limiter` the right one to
adopt, and the asymmetry is not inconsistency.

**Reasoning:** both were installed and run. `transitions` works and raises properly, but injects
methods onto the model dynamically and produced **9 mypy errors** under this project's strict
settings, in exchange for replacing a `dict[(state, event)] -> state` that is thirty lines and has
no dependencies. `pyrate-limiter` enforces three simultaneous windows in one call, genuinely blocks
(measured: 1.05 s on the per-second bucket), and ships a SQLite backend for state that survives a
restart — a week of work to reproduce, and easy to get subtly wrong. `sismic` was disqualified on a
fact rather than a preference: it **silently ignored** an illegal transition.

**Confidence: measured** — every candidate was installed in a throwaway venv and run on real input.

**What would change my mind:** for the FSM, nothing short of the table growing beyond what a dict
can express legibly. For the limiter, evidence that its sliding window and the circular's calendar
second diverge in a way that matters; sliding is strictly stricter, so this would be a performance
argument, not a correctness one.

## O.80 · 2026-08-13 · F02's largest residual risk is the deferred probe, not anything in the code

**Opinion:** the order path's real weak point is that no part of it has met a real fill, a real
`order_id` lifecycle, real charges or a real postback — and the read-only pass that DID run met an
empty account.

**Reasoning:** today's real-data run authenticated, called `orders()`, `trades()` and `positions()`
and got zero rows from all three. That verifies the calls, the auth, and the reconciler's behaviour
on an empty session. It verifies **nothing** about normalising populated rows: statuses, the
paise/rupee conversion, `t1_quantity`, the positions-and-holdings union. Two adversarial reviews on
`L1.01` and `F01` each found five criticals behind a green suite, and the pattern in both was the
same — the tests asserted the assumptions rather than the facts. The same exposure exists here and
only a real order closes it.

**Confidence: judgement**, informed by the two prior reviews on this project.

**What would change my mind:** the operator running the deferred probe. One filled and squared-off
ticket would convert most of this from judgement to measurement.

## O.81 · 2026-08-13 · Paper trading should not need the same ceremony as live capital

**Opinion:** the trading control latch is right to be latched by default and right to make LIVE
unreachable without an operator key, but requiring the SAME operator ceremony to release the latch
for PAPER trading is a mistake, and the mistake is a safety one rather than a convenience one.

**Reasoning:** `R.22`'s two keys exist for real money. As built (`trading_control_latch.py:595`),
`release_latch` demands an `OperatorAuthorization` — the arming-key file plus a typed phrase — even
when the intended mode is paper. `F04`'s paper loop therefore cannot run at all until the operator
performs a ceremony designed for arming live capital. The predictable consequence is the operator
releasing the latch once and leaving it released, which leaves `arm_live_trading` as the ONLY thing
between the system and live money — a single key where the design intends two. A cheaper, still
explicit, still recorded paper release keeps the expensive ceremony rare and therefore meaningful.

**Confidence: reasoned.** The mechanism was read and the ceremony run end to end by the agent that
built it; the failure mode I am predicting is human behaviour, which I have not observed here.

**What would change my mind:** the operator saying they want one ceremony for both, or evidence
that paper releases are so rare that the friction never accumulates. Either way this is the
operator's call and is recorded, not acted on.

## O.82 · 2026-08-13 · The adversarial review is now the highest-yield step in the loop, and I should stop being surprised

**Opinion:** on this project, `R.23(c)` finds critical defects behind a green suite **every time**,
and the right response is to treat a passing suite as evidence of nothing much until the review has
run — not to keep being surprised by it.

**Reasoning:** three features, three reviews, thirteen criticals — `A.91` four, `A.98` five, `A.100`
five — all behind suites that were entirely green, and in most cases with a test actively asserting
the defect. The mechanism is not mysterious and it is not carelessness: the same reasoning that
writes the code writes the test, so the test encodes the assumption rather than the requirement. A
fresh agent that has never held the assumption is the only cheap instrument that finds it.

**The sharper version, which is today's real lesson:** the defects clustered in one place — every
point where the code had a way to say "I do not know" and used a cheaper answer instead. Unparseable
payload → "no orders". Dropped connection → "never sent". Refused transition → "log a conflict".
Inference that had done its job → "keep it". If I had reviewed my own code for that ONE pattern I
would have found four of the five.

**Confidence: measured** on the count, **judgement** on the mechanism.

**What would change my mind:** a review that finds nothing on a feature built the same way. That
has not happened in three attempts, so the prior is strong. Note the cost honestly: the review takes
roughly as long as building the feature's core, and it is still the best-value hour spent.

## O.83 · 2026-08-13 · Unlimited paper capital is the wrong denominator, not the wrong generosity

**Opinion:** `A.06`'s "paper capital is unlimited" is right for the *experiment* book and unusable
for the book that produces graduation evidence, and the correct resolution is two books rather than
a compromise figure.

**Reasoning:** it is arithmetic, not preference. Return on capital, Sharpe, percentage drawdown,
Kelly and every exposure limit are ratios whose divisor is the capital base. With no base they are
zero or undefined. An unlimited book can therefore produce a **profit** and can never produce a
**return** — and `R.22` graduates a strategy on risk-adjusted evidence, which is exactly the class of
number that does not exist without a denominator. Meanwhile the experiment book's unlimitedness is
also load-bearing for its own purpose: three expressions of one conviction cannot be compared if
they compete for the same rupees. The two requirements are not in tension once they stop sharing a
book.

**Confidence: reasoned.** The arithmetic is certain; what is judgement is that the operator will
actually want both books rather than deciding later that the experiment book was never worth its
complexity.

**What would change my mind:** if the conversion table (`L14.28`) turns out to be derivable from the
bounded book alone — e.g. if running one expression at a time across many sessions gives the same
ranking as running three at once — then the unlimited book is redundant and should be deleted rather
than kept for symmetry. That is a measurable question and nobody has measured it.

## O.84 · 2026-08-13 · A screenshot is a different instrument from a test, and today it out-performed one

**Opinion:** `R.N`'s screenshot step is not a courtesy to the operator; it is a structural check that
finds a class of defect the test suite is systematically blind to, and it should be treated as part
of verification rather than as presentation.

**Reasoning:** measured today. The `/paper-capital` ledger table rendered **two cells per row instead
of nine** — a line-wrap had turned a conditional inside a chain of implicitly-concatenated f-strings
into a ternary over the entire chain, so every event without an unfunded shortfall lost seven cells.
Eleven surface tests passed. They passed *correctly*: each asserted that some substring was PRESENT
on the page, and a two-cell row still contains those substrings. **Presence is not structure**, and
no assertion in the file could tell the difference. The screenshot showed it immediately, because a
human eye reads structure first and content second — the exact inverse of how a string assertion
reads a page.

**The generalisation worth keeping:** for any surface, write at least one test that counts or
positions rather than matches. `test_every_ledger_row_carries_one_cell_per_column` is that test, and
it now exists because a picture found what nine assertions could not.

**Confidence: measured** on the incident, **judgement** on how often the class recurs.

**What would change my mind:** if the counting-style tests catch the next three surface defects
before the screenshot does, then the screenshot is confirmation rather than detection and can move
to the end of the loop. Today it was detection.

## O.85 · 2026-08-13 · The defect class is now predictable, and it is "the scenario nobody wrote a test for"

**Opinion:** across four consecutive `R.23(c)` reviews the criticals have not been randomly
distributed — they cluster in whichever axis the test file never varied — and I should be searching
that axis deliberately before the review rather than being told about it afterwards.

**Reasoning:** measured over four features. `A.100`'s five criticals all lived where the code could
say "I do not know" and said something cheaper instead — an axis the tests never varied because
every fixture was a well-formed response. `A.103`'s two criticals both lived in **concurrency** — an
axis where every one of the 39 tests was single-threaded, and where the specification I wrote myself
listed "a commit racing an edit" under §7 as required and I then did not write it. The review needed
one afternoon to find what the suite could not find in principle, because the suite had no
representation of a second writer at all.

**The operational form, which is the useful part.** Before calling a feature done, list the axes the
test file holds CONSTANT — one process, one thread, one clock, well-formed input, sequential arrival,
bounded magnitude, no crash mid-write — and write one adversarial test per axis. `L1.18` held six of
those constant and had defects behind two of them. That is a checklist, not an insight, which is why
it is worth writing down.

**Confidence: measured** on the clustering across four features, **judgement** on whether the axis
list generalises past this codebase.

**What would change my mind:** a review whose criticals are spread evenly rather than clustered in
one held-constant axis. Three of four have clustered so far; `A.98`'s I did not analyse this way and
should re-read before treating the pattern as settled.

## O.86 · 2026-08-13 · Bounding the paper book at ₹1 crore is a real constraint on the operator, and I chose it anyway

**Opinion:** capping every paper-capital event at `MAXIMUM_SUPPORTED_CAPITAL_RUPEES` is correct, but
it does narrow what the operator asked for ("editable as I need or wished") and that trade should be
visible rather than buried in a guard clause.

**Reasoning:** the unbounded version was not merely untidy — `Decimal('1E+1000000')` committed to
the append-only log, returned `303`, and left the page raising `decimal.Overflow` on every read with
no recovery except posting blind into a surface that could not render. A bound was required. Given a
bound was required, `A.23`'s declared ₹1 lakh–₹1 crore range is the only defensible one, because it
is the envelope every other engine in this project is calibrated for; an arbitrary larger number
would be exactly the magic constant `R.03` forbids. But the operator did say "as I wished", and a
crore is a real ceiling on that.

**Confidence: reasoned.**

**What would change my mind:** the operator wanting to model a book above a crore. The fix then is
not to raise this bound — it is to raise `A.23`'s declared range, so every engine's calibration is
re-examined at the same time. That is the point of reading the bound from `capital_configuration`
rather than writing it here.

## O.87 · 2026-08-13 · Volatility targeting and a position-count capacity contradict each other, and the real-data run is what exposed it

**Opinion:** `F03`'s sizer as built can put the entire book into one quiet instrument while claiming
a concurrent capacity of six, and this is a design contradiction rather than a bug — the two halves
of the risk model are measuring different things and nothing reconciles them.

**Reasoning:** measured. The `R.05` run over 2,400 real instruments produced `AKSHAR` at the full
₹10,00,000 with `binding_bound = deployable_capital` — meaning the volatility budget
(`risk_budget / sigma`) and the Kelly cap both exceeded the whole book, and the only thing that
stopped it was the capital existing. That is arithmetically right: volatility targeting deliberately
sizes NOTIONAL up as sigma falls, because the thing being held constant is risk, not exposure. But
`target_risk_fraction = 1/6` claims six positions can coexist, and six positions of this size need
₹60,00,000. Both statements are in the same function and they cannot both be true.

**What I think the answer is, held loosely.** Capping the notional at `deployable / capacity` would
reconcile them and would also destroy most of what volatility targeting is for — it would become
notional budgeting with extra steps. The better shape is probably that the sizer keeps producing the
risk-justified figure and **concentration is `L7.05`'s job** (per-symbol and aggregate exposure,
correlation-aware), with the gate refusing what the book cannot carry. That keeps each engine
answering the question it is actually equipped for. I have not built it, so this is a direction, not
a decision.

**Confidence: measured** on the contradiction, **judgement** on the resolution.

**What would change my mind:** if `L7.05`'s exposure limits turn out to bind on almost every quiet
instrument, then the sizer is systematically producing numbers that are always refused, and a
producer whose output is always thrown away should be fixed at the source instead.

## O.88 · 2026-08-13 · The real-data run earned its keep by finding something no test could have

**Opinion:** `R.05` is not a formality that confirms what the suite already said — on this feature it
found two things the hermetic tests structurally could not, and that is the argument for running it
before sign-off rather than after.

**Reasoning:** measured, today. (1) The concentration contradiction above is invisible to a test,
because a test supplies its own fixture and I would never have written a fixture whose volatility
was low enough to blow past the whole book — the shape only appears when the market chooses the
inputs. (2) The calibration coverage gap — **2,086 of 2,400 instruments cannot be sized at all**
because only 40 calibrations exist — is a fact about the DATA, and no test over fixtures can report
it. The suite was 91 green tests and knew neither.

**The generalisation:** hermetic tests verify the algorithm against inputs I imagined; the real-data
run verifies it against inputs that exist. Those sets overlap far less than the green bar suggests,
and the second set is the one that will actually be traded.

**Confidence: measured** on both findings.

**What would change my mind:** a real-data pass that reports exactly what the suite already implied,
twice running. That has not happened yet — `F01`'s pass found the edge formula was the defect,
`F02`'s found an empty account, and this one found a concentration contradiction and a coverage gap.

## O.89 · 2026-08-13 · Correcting the Kelly denominator made the sizer right and revealed it is unusable alone

**Opinion:** `A.105`'s fix did not finish the sizer — it made the sizer's real answer visible, and
that answer is "put the whole book in one position", which means `F03`'s first slice cannot be
consumed until `L7.05` exists.

**Reasoning:** measured on the live surface immediately after the fix. Realised volatility 14.9 bps
over five bars, calibrated edge 128.8 bps: raw Kelly is roughly **5,760 times capital**, capped to
1.0, while the volatility budget independently asks for **₹11.2 crore against a ₹10 lakh book**.
Both bounds saturate at the capital. That is not a defect in either formula — full Kelly on a
high-Sharpe intraday edge genuinely says bet everything, and volatility targeting genuinely sizes
notional up as sigma falls. It is a statement that **neither formula is a concentration constraint**,
and I built a sizer out of two things that are not the thing that was missing.

**The uncomfortable part, stated plainly.** Before the fix, the broken denominator was accidentally
*hiding* this: the inverted Kelly produced small numbers for large edges, which looked like prudent
sizing. A defect was doing the job of a missing feature, and the green suite plus the passing
`R.05` run both agreed with it. That is the most dangerous shape a bug can take.

**Confidence: measured** on the arithmetic, **reasoned** on the conclusion that `L7.05` is the right
home rather than a cap inside the sizer.

**What would change my mind:** if a per-trade concentration cap turns out to be derivable from the
same calibration — e.g. sizing to the edge's own confidence interval rather than its mean — then it
belongs in the sizer after all and `L7.05` handles only the cross-position case. Worth an hour
before building `L7.05`.

## O.90 · 2026-08-13 · I asserted a data source existed without opening it, in the same commit that praised checking

**Opinion (a correction on myself):** my `BACKLOG` note calling the bhavcopy's price-band columns
"the next cheap win" was a claim I never verified, written into a commit whose whole subject was the
difference between reading a source and assuming one.

**Reasoning:** measured, one query later. `nse_bhavcopy_cash` and `nse_bhavcopy_fo` carry
`OpnPric`, `HghPric`, `LwPric`, `ClsPric`, `SttlmPric` and no circuit-limit field of any kind, and no
other ingested source carries one either. The name `circuit_band_asm_gsm` is what misled me: it
reads like a band feed and is a surveillance feed. I had already *written in the module docstring*
that it carries stages rather than bands — and then, two paragraphs later in the same session,
asserted the bands were somewhere else without looking.

**The pattern worth keeping.** The failure was not ignorance; it was writing a forward-looking claim
in the same breath as a verified one, where the prose gives them the same weight. A backlog entry
that says "the source is X" is an assertion of fact and needs the same one-command check that
`banned=True` got. The entries that survived scrutiny today were the ones with a command and an
output next to them.

**Confidence: measured.**

**What would change my mind:** nothing about the fact. What is still open is whether NSE's daily PR
archive is the right acquisition path for band prices — I believe it carries a per-scrip band file,
and I have NOT verified that either, which is exactly why it is written here as a belief and logged
in `BACKLOG` as an unbuilt adapter rather than a cheap win.

---

## O.91 · 2026-08-15 · The tests found three defects the design review would not have, and all three were about time

**Opinion:** `F04`'s spec §7 adversarial list was worth more than any amount of re-reading the code,
and the reason is that all three defects it caught are invisible in a single-step reading. A double
square-off, a residual that outlives its exit, and a venue filling from a book that no longer
exists are each *correct* at every individual line; they are wrong only across steps, and only a
loop that actually runs many steps can show it.

**Reasoning:** measured. Every one was found by running the session, not by reading it. The
double-exit surfaced as three orders in a journal that should have held two; the stale-book fill
surfaced because the "book vanishes mid-session" test PASSED when it should have failed, which is
the failure mode a test-suite reader almost never notices — a green assertion that proved the
opposite of what it claimed. I would not have found any of them by inspection, and I do not think a
review agent reading the diff would have either.

**Confidence: measured.**

**What would change my mind:** an adversarial review that finds a fourth defect of the same class by
reading alone. That would say the axis is inspectable after all and I was simply not looking hard
enough.

## O.92 · 2026-08-15 · Two simulated venues is one too many, and the newer one is the one that should go

**Opinion:** `paper_loop/simulated_execution_venue.py` (2026-08-13) should be deleted after its one
genuinely new measurement — `queue_ahead_quantity`, the size resting ahead of a passive limit — is
ported into `order_path/simulated_order_execution_venue.py`. Keeping both is the `R.06` orphan rule
being violated by the most plausible-looking route there is: the orphan is newer, better documented,
and does one thing the survivor does not.

**Reasoning:** reasoned. The order path accepts only an `OrderExecutionVenue`, and `A.108` decision
2 makes going through the order path the whole point of the slice, so the stateless ladder
calculator cannot be in the fill path without inverting that decision. What it adds is real but
small: the protocol venue never fills a passive limit at all (conservative, not optimistic), and so
it under-reports rather than over-reports — it simply cannot say HOW FAR from filling a resting
order was. That is a diagnostic, not a fill, and it belongs on the venue that actually holds orders.

**Confidence: reasoned** — I have read both and run one; I have not yet written the port.

**What would change my mind:** if a later slice needs to price a hypothetical fill WITHOUT placing
an order (a pre-trade "what would this cost against the book right now" panel), the stateless
calculator is the right shape for exactly that and should stay, renamed to what it is — a ladder
walk calculator, not a venue.

## O.93 · 2026-08-15 · The data gap was the real blocker, and acquiring the bars was cheaper than arguing about it

**Opinion:** the honest `R.05` for `F04` needed one date carrying both five-minute bars and a
recorded order book, and no such date existed — bars ended 2026-08-05, the depth tape starts
2026-08-11. The right move was to fetch the missing bars, not to run two half-verifications and
call their conjunction a pass.

**Reasoning:** measured — the two coverage queries are in this session's log. A run on 2026-08-05
would have refused every fill for want of a book, and a run on 2026-08-11 would have abstained on
every instrument for want of bars; each looks like a clean pass of the half it exercises, and
together they prove nothing about the join, which is the only part `F04` adds.

**Confidence: measured** on the gap, **reasoned** on the fix being sufficient — the backfill is
running as this is written and its `availability_time` convention (`bar_timestamp + interval`)
matches the store's existing rows, but the R.05 run has not yet been executed against it.

**What would change my mind:** if the backfilled bars turn out to disagree with the depth tape's own
last-traded prices for the same instants, the bars are not the same market the book recorded and the
join is still not verified. That comparison is worth running and is now in `BACKLOG`.

## O.94 · 2026-08-15 · The first real session loses money, and that is the most useful thing this rebuild has produced

**Opinion:** the `−Rs 10,824.69` on 2026-08-11 is worth more than a profitable number would have
been, because it is the first loss this system has produced through the code that would have carried
real money — and because its composition is legible: `Rs 868.54` of it is cost, so 92% is the edge
being wrong rather than the friction being heavy. A profitable first run would have told me almost
nothing, since I would not have known whether to believe it.

**Reasoning: measured.** 2,882 instruments, 76 decision instants, 218,936 decisions, 45 entries, all
squared off, ledger fold agreeing with the report (`docs/research/229`). The strategy abstained
213,739 times and was refused by the gate 5,079 times, so the 45 entries are what survived every
filter this project has built — and they still lost, at an average of about Rs 221 gross each.

**What I think is actually wrong,** in the order I would test it: the five-bar horizon is inherited
from the calibration and may simply be shorter than the reversion it is fitted on; the entries
cluster in illiquid scrips (`3PLAND`, `ARCHIES`, `AMJLAND`) where the spread walk is a large
fraction of the move, which is a selection effect of sizing by volatility budget; and the mean
capture the calibration reports is a GROSS one-way move being compared against a round trip
(`BACKLOG` `M12` already records that mismatch on the `/costs` page, and this run is the first place
it shows up in rupees).

**Confidence: measured** on the result, **reasoned** on the diagnosis, **judgement** on which of the
three matters most.

**What would change my mind:** the same replay on 2026-08-12 and -13 coming out positive would say
one session is noise and I over-read it. Two more losing sessions with the same composition would
promote the diagnosis above from reasoned to worth acting on — and the first thing I would act on is
the horizon, because it is the only one of the three that is a single parameter rather than a
redesign.

## O.95 · 2026-08-15 · Three sessions in, the cost is the constant and the edge is the variable — and one session was pure cost

**Opinion:** `O.94` said the 08-11 loss was 92% edge and 8% cost, and named the horizon as the first
thing I would test. Two more sessions say the diagnosis was incomplete rather than wrong, and they
change what I would do first. On 2026-08-12 the strategy was **flat gross** — Rs 5.39 lost per entry
across 61 entries — and still lost Rs 1,202.88, because cost took Rs 874.38 of it. **At this trade
size the edge is indistinguishable from zero and the cost is not**, and the first thing to act on is
therefore the trade SIZE and the entry COUNT, not the horizon.

**Reasoning: measured** (`docs/research/230`). Cost across three sessions: Rs 868.54, Rs 874.38,
Rs 876.01 — a spread of Rs 7.47 across universes differing fourfold and entry counts differing by
50%, because the charge is dominated by per-order brokerage and per-trade statutory floors and
scales with the NUMBER of round trips rather than their size. Gross across the same three:
−Rs 9,956, −Rs 329, −Rs 1,284. One of those is not like the others, and it is the variance that is
large, not the mean.

**What I would now do, in order:** (1) make the cost gate binding on ENTRY — `F01` already prices
a round trip, and an entry whose expected capture does not clear its own priced cost should never
reach the sizer, which would have suppressed most of 08-12's 61 entries; (2) then revisit the
five-bar horizon, since `O.94`'s argument for it still stands and is now testable against three
sessions rather than one; (3) leave the sizer alone until both are done — the 08-11 loss came from
a few large positions in thin scrips, and shrinking the sizer would hide that rather than fix it.

**Confidence: measured** on the cost constancy and the 08-12 composition; **reasoned** on the
ordering above; **judgement** that the cost gate is the cheapest of the three to try.

**What would change my mind:** if the entries that a cost gate would suppress turn out to be the
PROFITABLE ones — entirely possible, since the cheapest trades to enter are the liquid ones and the
capture is being measured on deviations that are largest in thin ones — then the gate makes the
gross worse while making the net better only by trading less, which is a different and weaker claim
than having an edge. That comparison is worth running before the gate is wired in.

## O.96 · 2026-08-15 · The flaky-test label was the defect protecting itself, and I nearly accepted it

**Opinion:** when Hypothesis said *"failed on the first call but did not on a subsequent one"*, the
cheapest reading — a flaky property, re-run it — would have been wrong, and I had already written
that reading into `BACKLOG` `M16` about a DIFFERENT test earlier the same day. A rate limiter is a
guard; a guard that fails once and passes on retry is exactly the shape a real breach takes when the
retry is contaminated. The rule I would keep: **a flaky failure in a guard is a defect until proven
otherwise, and the proof is a deterministic replay, not a second run.**

**Reasoning: measured.** The replay reproduced the breach on the first attempt every time once the
store was fresh; the contamination was the property naming its store after a hash of the example, so
Hypothesis's confirmation run reused a store already holding the first run's orders and the limiter
correctly granted fewer. Two defects stacked so that one hid the other, and the outer one had an
innocent name.

**On the fix itself:** widening the enforced window by one clock tick is the right shape because it
is the only one that makes the invariant hold on ANY observer's clock rather than on ours. I
considered clamping the ratchet so it can never lead the wall clock and rejected it as the primary
fix — it is correct on its own merits and I would still take it, but float truncation leaves the two
clocks disagreeing by a tick regardless, so it narrows the gap without closing it.

**Confidence: measured** on the mechanism and the fix (2,000 fresh examples, plus 265 real recorded
arrivals through a real limiter with zero breaches); **judgement** on the rule about flaky guards.

**What would change my mind:** a breach that survives the one-tick widening would mean the
disagreement between the two clocks is larger than a tick — plausible under real NTP correction
rather than under a steerable test clock — and the answer would then be to stamp items with the
observer's clock rather than to pad the window. That is worth testing with a wall clock that steps
while orders are in flight, which the suite does exercise for the ratchet but not for the window.

## O.97 · 2026-08-15 · The rate gate refused exits, not entries, and that is a risk finding rather than an accounting one

**Opinion:** I expected the rate limiter to cut the paper loop's ENTRY count and it cut none of it —
45, 61 and 40 entries before and after, identical. What it refused was square-offs, 384 legs across
three sessions where 189 had been enough before. The reason matters more than the number: every
position runs the same five-bar horizon, so they expire in waves, and a wave of square-offs at one
simulated instant is exactly the shape the 1-second ceiling of 10 refuses.

**Why that is a risk statement.** A position whose horizon has expired and whose exit is refused
stays on the book another five minutes, holding market risk the strategy did not intend — and it
does so precisely when many positions want out at once, which is when the market is most likely to
be moving against them. The ungated paper record could not show this at all.

**Reasoning: measured** (`docs/research/231`), from the journals: entries unchanged, square-off legs
up 2-3x, refusals 100% attributed to the 1-second ceiling, and every session still flat by the
close.

**What I would do about it, and it is not to raise the ceiling:** stagger the horizon per position
so expiries disperse. The horizon is currently one policy number applied to every entry, which is
what synchronises them; drawing it per instrument from the calibration's own measured horizon would
disperse the wave AND be better-founded than the single number is today. I would try that before
touching the limiter, because the limiter is behaving correctly.

**Confidence: measured** on the refusal pattern; **reasoned** on the staggering fix; **judgement**
that the synchronisation explains most of 08-12's 50.4% refusal rate.

**What would change my mind:** if dispersing the horizon leaves the refusal rate high, the collision
is coming from the entry side after all — many instruments deviating together in a correlated move —
and the answer would be a concurrency cap on simultaneous exits rather than a scheduling change.
