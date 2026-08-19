# 250 · `L5.29` adversarial review — 15 of 18 non-conforming bots passed the suite

**Run 2026-08-17 in a fresh subagent, per `R.23c` step 5.** The claim under review: *these three
modules make it safe for six independently-authored segment bots to share one decision path, and the
conformance suite mechanically catches a bot that violates the contract.*

**Result: the claim was false in its second half.** The reviewer wrote 18 non-conforming bots.
**15 passed the suite clean, 2 crashed it, 1 was caught.** Of 24 source mutations applied, **12
survived** the whole test suite.

I had already ticked the todo item before the review returned — the exact self-report `R.23c`
forbids. The tick was reverted to `[~]` and only restored after the reviewer's own bots fail.

---

## What the review found, and what was done

### CRITICAL

**C1 · The suite checked 4 of roughly 20 clauses the contract states.** Bots that passed clean:
mutating the context it was handed · emitting the same signal three times · proposing a quantity that
is not a whole number of lots · proposing an option quantity of 1 against a lot size of 75 ·
inventing instruments outside its universe, *including when the universe was empty* · emitting 5,000
signals per context · performing real I/O (the side-effect file existed after the run) · returning a
different relevance on every call · writing `applicability=10000` past the frozen dataclass with
`object.__setattr__` · changing its own identity between reads · claiming a five-day horizon on an
*option*.

The worst is the relevance bypass: it is the **same** `object.__setattr__` trick the suite already
anticipates for `BotMaturity`, and there was no equivalent guard for the number the supervisor
allocates on. A bot handing back 10,000 takes the entire allocation from five siblings.

**Fixed** — six new checks, each with a regression test built from the reviewer's own bot:
`signal-is-inside-the-given-universe`, `quantity-is-a-whole-number-of-lots`, `signals-are-distinct`,
`proposal-size-is-sane`, `context-is-not-mutated`, `relevance-is-deterministic`, plus bounds
**re-validated at the boundary** rather than trusted from the constructor, and identity read once and
re-compared. *Not fixed:* detecting I/O, which needs process-level isolation rather than a check —
recorded as **B9**.

**C2 · `_check_carry_rule` was provably unreachable dead code, and checked the wrong object.** It
tested the *fact table* (`facts.strike_required and facts.overnight_carry_permitted`), a combination
`SegmentInstrumentFacts.__post_init__` already refuses to construct. Deleting the call survived the
suite. So `R.01` — which `A.130` describes as *"enforced by the bot rather than the operator"* — had
**zero** enforcement against any bot.

**Fixed** — replaced with `_check_emitted_carry`, which reads the `horizon_minutes` a bot puts on its
own signals against the 375-minute NSE session.

**C3 · `CASH_INTRADAY` mapped to `EQUITY_INTRADAY` while permitting overnight carry — a ~4× cost
understatement, priced through this project's own engine.** STT follows the settlement type, not the
MIS/CNC order tag, and this repository's own rule store seeds 0.025% sell-only for `NSE-MIS` against
**0.1% on both legs** for `NSE-CNC`:

```
EQUITY_INTRADAY  NSE-MIS  round trip on Rs 2,40,000 notional:  Rs 132.36
EQUITY_DELIVERY  NSE-CNC  round trip on Rs 2,40,000 notional:  Rs 533.96
```

`R.01` itself says cash is promotable *"net of the **delivery** cost structure"* — so the flat
mapping contradicted the very rule its `regulatory_source` cited.

**Fixed** — `carried_chargeable_segment` added, `chargeable_segment_when(carried_overnight=...)`
introduced, and the two facts pinned together at construction so they cannot drift. Asking a
non-carrying segment for a carried scope now raises rather than returning the intraday one.

### MAJOR

| # | Finding | Status |
|---|---|---|
| M4 | `_check_identity` matched only the FIRST WORD, so an `INDEX_FUTURES` bot named `index_options_greek_scalper` passed, as did `cashew_nut_arbitrage_bot` on the substring "cash". Mutating the branch to `if True` survived — it had no test at all. | **Fixed**: full segment value must appear; three parameterised regressions |
| M5 | The suite **crashed** rather than returning violations when `relevance()` or `maturity()` raised — defeating the "runtime gate" the module docstring sells. | **Fixed**: both wrapped; `relevance-does-not-raise` and `maturity-does-not-raise` |
| M6 | `_check_maturity` caught every `SegmentBotProtocolError`, so an unrelated bug inside `maturity()` was reported as an attempted **self-graduation** — the loudest alarm the suite has, fired for the wrong reason. | **Fixed**: `BotGraduationRefusedError` subclass; only it raises the `R.22` alarm |
| M7 | Factual error: `CIR/DNPD/6/2010` is titled *"European Style Stock **Options**"* and does not govern index options — NSE index options launched European and cash-settled on **2001-06-04**, nine years earlier. | **Fixed**: citation moved to the stock-options record, index record cites the contract specification |
| M8 | Factual error: MCX is not uniformly physically settled — **crude oil, natural gas and the MCX index futures (BULLDEX, METLDEX) are cash-settled**, and CTT applies to non-agricultural commodities only. | **Partly fixed**: the conservative value is kept and the source now says it is a simplification; the real fix is per-contract settlement — **B8** |
| M9 | Real modelling gap: `COMMODITY_MCX → COMMODITY_FUTURES` makes **MCX options unreachable**, though `ChargeableSegment.COMMODITY_OPTIONS` exists, the cost engine prices it, and MCX option notional ADT grew ₹1.92 L Cr (FY25) → ₹4.72 L Cr (FY26). The `strike_required ⟺ PREMIUM` invariant makes an MCX options bot inexpressible. | **Open — B10.** Needs a plan decision (seventh segment vs per-instrument scope); not silently invented |
| M10 | The `TradingSegment → ChargeableSegment` mapping was **completely unpinned**: the test asserted only enum membership, so mutating MCX to `CURRENCY_FUTURES` survived — and silently, since CDS attracts no transaction tax at all (₹48.36 vs ₹64.40 per ₹1,00,000). | **Fixed**: exact scope asserted for all six |

### MINOR

m11 `observe` idempotence claimed but unchecked (a bot double-counted to 6 against a truth of 3) —
**comment corrected, check recorded as B9**; m12 `option-signal-carries-a-strike` unreachable as an
independent finding — kept as defence in depth, noted; m13 two checks had no test — **now tested**;
m14 dedup collapsed 400 distinct forgeries into one finding — **fixed**, the detail now carries the
instrument token; m15 a misnamed test plus `TradeableInstrument` accepting a negative token, a
negative strike, a strike with no expiry — **fixed**; m16 a test vacuous for one of three parameters
— **fixed**; m17 a comment claiming futures are promotable, contradicting `R.01` — **fixed**;
m18 spec-vs-code drift in `docs/research/248` — the book/bar readers and Hypothesis tests §3.2/§5
promised are **not built**; recorded rather than quietly dropped.

## What the reviewer could not break

`BotMaturity`'s `GRADUATED` refusal · the `[0,1]` relevance bound at construction · negative closed
trades · empty evidence · the naive-datetime refusal · `lot_size <= 0` · the zero-context and
no-empty-universe vacuity guards · `instrument_facts_for`'s no-default refusal · the determinism
comparison · `signal-matches-own-segment` · the index/stock settlement split · the
`strike_required ⟺ PREMIUM` invariant · the 2018/2019 physical-settlement claim. Every mutation
applied to these was killed by a test.

## Outcome

**52 → 67 tests** (20 adversarial). Every bot the reviewer wrote that previously passed now fails,
except the I/O detector (B9). Execution gate green; the `R.05` real-data pass over the real
10,061-instrument NSE universe re-run clean after the fixes.

**The lesson worth keeping.** I wrote the suite, wrote six adversarial tests for it, watched them
pass, and concluded it worked. A fresh adversary wrote twelve more in one sitting and walked straight
through it. The tests I thought of are exactly the failures I had already designed against; the
value of the review is entirely in the ones I could not think of, which is why `R.23c` puts it in a
**fresh** subagent and why "done" is gated on it rather than on my own report.
