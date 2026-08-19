# 248 · `L5.29` — the segment-bot protocol, specified before it is built

**Plan entry:** `L5.29` "Segment-bot protocol — the shared contract all bots implement."
**Governing decision:** `A.130` (all six segment bots in parallel, one shared spine).
**Readiness evidence:** `docs/research/247`.
**Idea-intake verdict: ALREADY EXISTS in the plan as `L5.29`, not yet built.** No new entry.

---

## 1. What this is, and what it deliberately is not

`R.23b` sets scope by vocabulary. This deliverable is called a **protocol**, so it is a contract and
its conformance kit — not an engine, and it will not wear engine vocabulary. The engines are the six
**bots** that implement it (`L5.26`–`L5.28` and their three siblings), each of which owns a real
strategy set, a relevance model, carried memory and a track record, and each of which gets the full
`R.23c` loop in its own right.

**What the protocol must achieve** is narrow and load-bearing: make it impossible for six
independently-authored bots to disagree about the path from a decision to money. Everything the six
share is defined here once; everything they legitimately differ on is declared here as a *question
each bot must answer*, so a difference is always explicit rather than emergent.

## 2. The discovery that shapes the design

`ChargeableSegment` already exists and is **not** the six-segment axis. Its docstring is explicit:

> *"Index and stock derivatives are deliberately NOT separate members: their statutory rates are
> identical, and a distinction that exists only in code invites someone to give the two different
> numbers."*

It has eight members and answers *"what does this cost?"*. `A.01`'s six segments answer *"who decides
this?"* — and they split index from stock precisely where `ChargeableSegment` refuses to, because
cash-settled and physically-settled instruments are different **risk** shapes even when they are the
same **charge** shape.

**So the protocol introduces a second, orthogonal axis — `TradingSegment` — and makes the mapping to
`ChargeableSegment` total, explicit and tested.** Conflating them would either give index and stock
derivatives different tax rates (the error `ChargeableSegment` was written to prevent) or give them
the same settlement obligation (the error `A.01` was written to prevent). Two axes, one mapping, no
third opinion.

`PricedSignal` already exists too, and is exactly the right output type for a bot: it carries the
instrument, side, reference price, expected edge, `EdgeBasis`, `conviction`, an optional strike, and
a horizon. **A segment bot emits `PricedSignal`s and the spine consumes them.** The protocol does not
invent a new currency; it names the one that is already there.

## 3. The contract

### 3.1 `TradingSegment` — the six, plus their irreducible facts

Six members: `CASH_INTRADAY`, `INDEX_OPTIONS`, `STOCK_OPTIONS`, `INDEX_FUTURES`, `STOCK_FUTURES`,
`COMMODITY_MCX`.

Each carries a `SegmentInstrumentFacts` record — the facts that are true of the segment itself rather
than of any one bot's opinion:

| Fact | Why it is on the segment and not the bot |
|---|---|
| `chargeable_segment` | The cost engine's view; the mapping must be total and tested |
| `denominator` (`PRICE` / `PREMIUM` / `CONTRACT_NOTIONAL`) | `L11.106`; `D.01` records that conflating price and premium denominators was an error |
| `carries_overnight_permitted` | `R.01` — options, index **and** stock, NEVER carry. Enforced by the segment, not by an operator remembering |
| `settlement` (`CASH` / `PHYSICAL_DELIVERY`) | SEBI has mandated physical delivery for stock F&O since 2018; `B3` |
| `requires_option_greeks` | Gates arming on `B1` existing |
| `has_expiry` | Roll and square-off obligations exist only where this is true |
| `venue_calendar` (`NSE` / `MCX`) | MCX's evening session has no calendar source today; `B4` |
| `strike_required` | Mirrors `PricedSignal.__post_init__`'s existing refusal |

These are **regulatory and physical facts**, which is the one exemption `R.23e` allows for constants
— each is written with its source in a comment. Everything else a bot uses must be derived.

### 3.2 `SegmentBotContext` — what the spine hands a bot

A bot never opens a socket or a database. It is a pure function of what it is given, which is what
makes it testable on recorded data with no broker, and what lets six agents build concurrently
without shared state. The context carries: the decision instant, the segment's tradeable universe for
that instant, a book-snapshot reader, a bar reader, the current regime state, and the bot's own
carried memory loaded from the previous session.

### 3.3 `SegmentBot` — the protocol every bot implements

Six required members, and every one of them exists because a bot that cannot answer it cannot be
compared, gated, or held to a record:

1. `trading_segment` — which of the six it is.
2. `bot_identity` — stable, self-describing, the key its track record is filed under (`R.14`).
3. `observe(context)` — advance carried state on new market data. Separated from deciding, because a
   bot that only learns when it trades learns from a biased sample.
4. `propose(context) -> tuple[PricedSignal, ...]` — its decisions, already priced. May be empty; an
   empty proposal is a real answer and must not raise.
5. `relevance(context) -> SegmentRelevance` — how applicable its strategies are right now. `L5.25`
   gives each bot a relevance model; the supervisor needs a comparable number across six bots.
6. `maturity() -> BotMaturity` — its own position on the ladder, computed from its own evidence.
   `R.04`: thin data gates **activation**, never the algorithm.

### 3.4 `BotMaturity` — where "built is not armed" is mechanised

The ladder rungs are `COLD_START` → `OBSERVING` → `PAPER_QUALIFIED` → `GRADUATION_CANDIDATE` →
`GRADUATED`. A bot reports its own rung from its own evidence; the protocol enforces that **no rung
can be self-declared as `GRADUATED`** — that transition requires the `R.22` second key, and the type
system is where that is made unavailable rather than merely discouraged.

### 3.5 What the protocol REFUSES to let a bot do

Stated as refusals because they are the failure modes six concurrent authors would otherwise each
invent:

- A bot may not size a position. Sizing is spine-owned; a bot proposes a quantity as a *preference*
  and the sizer may reduce it.
- A bot may not clear its own cost hurdle, place an order, or touch the halt latch.
- A bot may not propose an overnight-carrying position in a segment whose facts forbid it (`R.01`).
- A bot may not propose an option signal with no strike (the cost of it is uncomputable).
- A bot may not report `GRADUATED`.

## 4. The conformance suite — the mechanism that makes six authors safe

One test module, parameterised over every registered bot, run unmodified against each. This is the
single most important artifact here: it converts "did the author understand the contract?" from a
review question into a test result.

The suite asserts, for every bot: identity is stable and self-describing; `propose` on an empty
universe returns empty rather than raising; every emitted signal's segment maps to the bot's own
`TradingSegment`; no signal violates its segment's carry rule, strike requirement, or denominator;
`observe` is idempotent for a repeated instant; maturity never self-reports `GRADUATED`; relevance is
in range; the bot performs no I/O; and the bot is deterministic given identical context.

## 5. Verification plan

- **Unit** — every refusal in §3.5 has a test that triggers it.
- **Property** (Hypothesis) — for arbitrary generated contexts, a bot's emitted signals always
  satisfy their segment's facts; the `TradingSegment → ChargeableSegment` mapping is total.
- **Adversarial** — a deliberately non-conforming bot must FAIL the conformance suite. A suite that
  cannot fail proves nothing, and this is the test that tests the tests.
- **Real data (`R.05`)** — the suite is run against a real cash-intraday bot over recorded NSE
  sessions, and the mapping is checked against every `ChargeableSegment` the cost engine actually
  prices.

## 6. Sourcing (`R.17`)

**No third-party library is sourced for this deliverable, and the reason is mechanical rather than
stylistic:** the contract is expressed entirely in terms of types this repository already owns —
`PricedSignal`, `ChargeableSegment`, `TradeLeg`, `RegimeState` — and no external package can define a
protocol over local types. `typing.Protocol` and `StrEnum` are standard library. The conformance kit
is `pytest` parameterisation, already a dependency.

Sourcing **was** run for the largest gap this protocol exposes — `B1`, the Greeks/IV-surface engine
that gates arming both option segments — and is recorded separately; that engine is a real
integration candidate and is not part of `L5.29`.
