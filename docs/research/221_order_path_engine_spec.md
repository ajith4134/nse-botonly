# 221 · The order path — spec

**Feature `F02` · "An intent becomes exactly one order, survives a crash, broker believed over local
state"** · opened 2026-08-13 (`A.99`) · entries `L3.01`-`L3.04`, `L3.06`, `L3.07`, `L3.16`, `L3.19`,
`L7.10`, `L9.01`-`L9.03`, `L9.14` (+ `L9.10`/`L9.11`/`L12.14` resolved as duplicates).

This document is the `R.23(c)` step-1 artefact: it exists before any code, it names the SOTA analog
the engine must be comparable to, and it fixes signatures and error behaviour so the tests can be
written against something.

---

## 1. What this feature is, stated so it can fail

A **trading intent** is a decision that already cleared `F01`: an instrument, a side, a quantity, a
priced edge and a reason. The order path is everything between that intent and a settled fact about
what the market did with it.

Three claims, each of which must be independently falsifiable by a test:

1. **Exactly one order.** The same intent, submitted twice — by a retry, a restart, a duplicate
   signal, two engines reaching the same conclusion, or a network timeout that hid the broker's
   answer — produces **one** order at the broker or an explicit refusal, never two.
2. **Survives a crash.** If the process dies at any instant, including between writing the intent
   and the broker acknowledging it, the next start can determine what happened and complete or
   abandon it deliberately. Nothing is lost silently and nothing is retried blindly.
3. **The broker is believed.** Where local state and broker state disagree, broker state wins, the
   disagreement is recorded rather than smoothed away, and any event the system had to *infer* to
   close the gap is marked as inferred forever after.

### What would prove it wrong

- Two `order_id`s traceable to one `intent_id`.
- A restart that produces a position the system does not know it holds, or claims one it does not.
- A fill applied twice, or an average price that drifts from the broker's own.
- A reconciliation that silently rewrites history rather than recording a conflict.

---

## 2. The SOTA analog (`R.23(a)`)

The engine is measured against **NautilusTrader's execution subsystem** — `ExecutionEngine`,
`ExecutionClient`, `OrderManager`, the `Order` state machine and its reconciliation procedure — with
**QuantConnect LEAN's** brokerage-side transaction handler as the second reference. The properties
this spec copies deliberately are listed in §12 with their source.

The difference test (`R.23(a)`) for this feature:

| Requirement | This engine |
|---|---|
| inputs | trading intents; broker order/trade/position state; the live depth tape; the `F01` cost and impact engines |
| solver / procedure | a deterministic order state machine + a reconciliation algorithm over two disagreeing state sets + an expression selector that optimises over the taxonomy |
| state carried between decisions | the durable intent journal, the order book of record, the fill ledger, the rate-limit budget, the kill-switch latch |
| verifiable output | an order actually sent (or refused with a reason), and a position the broker agrees with |

Anything here that turns out to be a labelling layer over a broker SDK call is renamed to what it is
(`R.23(b)`), not shipped wearing engine vocabulary.

---

## 3. Domain — the types that do not exist yet

`src/` today has no order, position, trade or fill type at all. Everything below is new, lives in
`src/nse_algo_trader/order_path/`, and is frozen-dataclass, slotted and typed, matching the
convention of `bitemporal_bar_store.py` and `cost_gate/priced_signal.py`.

### 3.1 Identity

Three distinct identifiers, never conflated:

| Identifier | Assigned by | Meaning |
|---|---|---|
| `intent_id` | this system, **deterministically from the intent's own content** | "this decision" |
| `submission_id` | this system, per attempt | "this attempt to express the decision" |
| `broker_order_id` | the broker | "the order that exists at the broker" |

`intent_id` is a content hash over the fields that make the decision the decision — strategy
identity, instrument, side, quantity, product, the decision timestamp truncated to its own
resolution, and the session date — so that the *same* decision recomputed after a restart yields the
*same* id without consulting any store. This is the property that makes deduplication work across a
crash: recovery does not need to remember what it was doing, it can recompute the name of what it
was doing. A different decision that happens to look similar gets a different id because the
decision timestamp is inside the hash.

`submission_id` is per attempt because a retry after an ambiguous timeout is a genuinely different
network event and must be distinguishable in the journal from the first attempt.

### 3.2 The order record

`OrderRecord` carries: identity, the instrument and its segment, side, ordered quantity, the chosen
expression (variety/product/order type/validity/prices), the current lifecycle state, cumulative
filled quantity, leaves quantity, average fill price, every state transition with its timestamp and
its **source** (local, broker-reported, or inferred), and the terminal reason.

`leaves_quantity` is derived, never stored independently — a stored pair that can disagree is a
defect waiting for a partial fill.

### 3.3 Fills

`FillRecord` is append-only and keyed by the broker's own trade id where one exists. A fill is
applied to an order exactly once; re-applying the same trade id is a no-op, not an addition, because
broker postbacks are at-least-once and the naive handler double-counts. Where a fill must be
inferred (reconciliation found a quantity gap with no trade to explain it), the record is flagged
`inferred=True` and carries what it was inferred from.

### 3.4 Positions

A position is **derived from fills**, never independently maintained, and then *checked* against the
broker's own position — the two are separate numbers and the check reports disagreement rather than
overwriting.

---

## 4. The order state machine (`L3.01`-`L3.04` core)

An explicit transition table as **data**, not scattered conditionals — the single most-copied
property from NautilusTrader, whose `Order` FSM is a dict of `(state, trigger) -> state` and raises
on anything absent.

States: `INTENT_RECORDED` → `SUBMISSION_IN_FLIGHT` → `ACKNOWLEDGED` → `WORKING` →
{`PARTIALLY_FILLED` → `FILLED`} | `CANCELLED` | `REJECTED` | `EXPIRED`, plus `AMBIGUOUS` (the
in-flight attempt whose outcome is unknown) and `ABANDONED` (an ambiguous order the reconciler
proved never reached the broker).

Rules the machine enforces:

- Terminal states are absorbing. A transition out of one raises `IllegalOrderTransition`; it never
  logs-and-continues, because a fill arriving after a cancel is exactly the situation where
  continuing quietly loses money.
- `AMBIGUOUS` is a first-class state, not an exception path. Every real order path spends time
  there and the systems that model it as an error are the ones that duplicate orders.
- Every transition records its source. An inferred transition can never be presented later as an
  observed one.

Broker status strings are mapped into this vocabulary by an explicit table keyed by the broker's
exact string; an unrecognised status is `UnmappedBrokerStatus`, a refusal, never a default.

---

## 5. The intent write-ahead log (`L3.02`)

SQLite at `~/.nse_algo_trader/order_path.sqlite3`, WAL journal mode, `busy_timeout`, schema created
idempotently on open — the `BitemporalBarStore` convention.

The ordering obligation, which is the whole point of the entry:

1. `intent` row written and **committed** — before any network call exists.
2. `submission` row written and committed with `state=SUBMISSION_IN_FLIGHT` — before the broker call
   is made.
3. the broker call.
4. the outcome written, whatever it is, including "the call raised and I do not know".

A crash between 2 and 4 leaves an `AMBIGUOUS` submission on disk, which is precisely the record the
reconciler needs. A system that writes after the call cannot distinguish "never sent" from "sent and
I died", and those two require opposite recoveries.

The log is append-only: states are new rows, never updates in place, so the history of what was
believed and when survives. Reads collapse rows to current state by a deterministic fold.

---

## 6. Reconciliation (`L3.03`) — the algorithm

On every start, and on demand, over one session date:

1. Fetch broker truth: `orders()`, `order_history()` per open order, `trades()`, `positions()`.
2. Fold the local journal to its own view.
3. Classify every order into one of five buckets:
   - **agreed** — both sides, same state and quantity.
   - **broker-only** — the broker has an order the journal does not. Adopt it, tagged with its
     origin (a manual order placed in the Kite app is the common real case, and it must not be
     mistaken for the system's own).
   - **local-only, ambiguous** — the journal has an in-flight submission the broker never saw.
     Resolve by the identity tag (§7), not by guessing: present at the broker under our tag means it
     landed; absent after the broker's own visibility horizon means `ABANDONED`.
   - **quantity disagreement** — the broker's filled quantity exceeds ours. Emit an **inferred
     fill** for the difference, flagged, priced at the broker's own average, and record the
     disagreement.
   - **state conflict** — the broker says terminal, we say working, or the reverse. Broker wins;
     the conflict is written to the ledger and surfaced on `/orders`, never silently reconciled.
4. Nothing is deleted. Reconciliation only ever appends.

The refusal case matters: if broker state cannot be fetched, reconciliation **refuses** and the kill
switch latches. A system that proceeds on a stale local view after a failed reconciliation is the
one that doubles a position.

---

## 7. Identity on the wire (`L3.19`) and what the broker gives us

*(Filled from the broker-API and regulatory research — see §7.1/§7.2, and `docs/research/222`.)*

The system tags every order with a compact encoding of its `intent_id` in whatever free-form field
the broker exposes, plus the regulatory identifier the framework requires. Two obligations that must
not be confused:

- **Ours**: recovering "did my intent already become an order" after a crash, without a client-side
  order id the API does not offer.
- **The regulator's**: the algo identifier that must ride on an automated order.

Both are constrained by the field's real length limit, which is small; the encoding is therefore a
truncation-resistant prefix of the hash plus the identifier, and the collision probability is stated
in the code rather than assumed away.

---

## 8. Rate limiting (`L3.06`)

A token-bucket limiter with **several simultaneous buckets** — per second, per minute, per day —
because the constraints come from different authorities with different windows, and the tightest one
binds. Limits are configuration derived from published fact, carried point-in-time with their source
(`R.03`), not literals.

Crossing the regulatory per-second threshold changes the regulatory category of the flow, so the
limiter's default is to **block and queue**, never to drop, and never to burst above it. The queue
is bounded, and an intent that ages past its own validity while queued is expired with that reason,
because a stale order is worse than no order (`L3.18`'s discipline, applied here).

---

## 9. Kill switch (`L3.07` + `L7.10`)

Two parts, and the separation is the point:

- **The latch** — a durable, crash-surviving flag that the order path consults before every
  submission. Latched means every submission is refused with the latch reason. Un-latching is an
  explicit operator act, never automatic.
- **The watchdog** — its own systemd **user** unit (system units are blocked under `/home` by
  SELinux, as the dashboard already documents), with its own heartbeat. It latches the switch when
  the trader stops heartbeating, when reconciliation fails, when the daily-loss or drawdown bound is
  crossed, or when the operator says so. It reconciles broker state on its own restart, so a
  watchdog that itself crashed does not come back believing yesterday.

A trader that is wedged or looping cannot stop itself; that is the entire reason `L7.10` asks for a
separate process, and an in-process flag would be a labelling layer over the same failure.

---

## 10. Paper/live parity (`L9.03`)

One `Protocol`, `OrderExecutionVenue`, with `place`, `modify`, `cancel`, `fetch_orders`,
`fetch_trades`, `fetch_positions`. Two implementations:

- `KiteOrderExecutionVenue` — the real broker (`L9.01`).
- `SimulatedOrderExecutionVenue` — fills against the **real recorded depth tape** through the
  existing `execution_fill` engine (`L1.05`/`L1.06`), with queue position and latency modelled, not
  a mid-price fantasy.

Everything above the seam — journal, state machine, reconciler, limiter, kill switch, selector — is
identical for both, and that is asserted by a **differential test**: the same intent stream through
both venues must produce the same sequence of *journal* events, differing only in fill prices and
broker ids.

Deliberate differences, stated so they are not mistaken for defects: the simulator's `order_id`
namespace is distinct and self-identifying; the simulator cannot reject for margin it does not
model, and says so rather than pretending.

---

## 11. The expression selector (`L9.14`, new)

Given an intent and the live book, choose the member of the taxonomy that expresses it best:

- **urgency** from the signal's own decay (a mean-reversion entry at a 5-bar horizon cannot wait for
  a passive fill that may take the whole horizon);
- **ticket against visible depth** from the depth tape — a clip that exceeds the visible book at an
  acceptable impact becomes an iceberg or a slice, and one that does not stays whole;
- **cost regime** from `F01` — the maker/taker difference, the segment's own statutory profile, and
  the flat-brokerage effect that makes small tickets expensive;
- **spread** live, because crossing a wide spread is a decision, not a default.

Output is an `OrderExpression` plus the **reason it beat the alternatives**, scored against the
`L1.05`/`L1.06` impact engine, so the choice is auditable rather than a preference table. Every
variety's availability is a point-in-time fact with a source and a date: a withdrawn variety is a
named refusal, and one that returns is available again without a code change.

---

## 12. Design decisions copied, and refused

From `docs/research/224`, which read five systems' source rather than their documentation. Copied:
the transition table as data with a typed refusal (NautilusTrader `orders/mod.rs:201-285`);
write-to-disk **before** the venue call (`engine/mod.rs:2081` vs `:2142`); a deterministic identity
plus a pre-submit lookup (`generators/client_order_id.rs` + `engine/mod.rs:2074`); full venue-state
fetch and diff with venue-wins and **inferred fills that stay marked inferred**
(`reconciliation/orders.rs:311-409`, `:1051-1131`); fill dedup keyed on trade id **before** the
transition (`orders/mod.rs:833-840`); one execution interface across paper and live
(`clients/execution.rs:53`); order throttling separated from HTTP throttling
(`risk/engine/mod.rs:99-100`); and property-fuzzing the reconciliation machine.

Refused, each with the code that shows why: in-memory-only order state recovered from a daily JSON
dump (LEAN `LiveTradingResultHandler.cs:336-340`); no identity at all, protected only by not
retrying (freqtrade `exchange.py:1445`); reconciliation that polls only what it already knows
(Hummingbot `exchange_py_base.py:1042-1046`, freqtrade `freqtradebot.py:402-447`); silent-ignore of
illegal transitions (OctoBot `order.py:187-193`, LEAN `BrokerageTransactionHandler.cs:1189-1191`);
and absolute-overwrite fill quantities with no monotonicity guard (OctoBot `order.py:1090`).

### 12.1 Sourcing (`R.16`/`R.17`) — what was searched, run, and rejected

Every candidate below was installed into a throwaway venv on this machine, imported, and **run on a
real input**; verdicts are from observed behaviour, not from a README.

| Part | Adopted | Rejected, with the mechanical reason |
|---|---|---|
| multi-window rate limiting | **`pyrate-limiter` 4.4.0** — one `Limiter` enforced `[3/sec, 5/min]` simultaneously, blocked 1.05 s on the per-second bucket and refused after the per-minute bucket exhausted; ships a `SQLiteBucket` for state that survives a restart | `limits` (no wait/block API at all — built for HTTP 429 responses); `ratelimit` (last release 2018-12-17); `token-bucket` (single window per instance); `aiolimiter` (asyncio-only, and `kiteconnect`'s client is synchronous) |
| retry that must NOT retry a timeout | **`tenacity` 9.1.4** — `retry_if_exception_type(ConnectionError)` retried thrice, and a `requests.exceptions.Timeout` propagated on the first attempt untouched, which is exactly the routing this feature needs | `backoff` (GitHub repo archived); `stamina` (read its source: `import tenacity as _t`, a facade over the same engine) |
| state machine | **written here** — a plain `dict[(state, event)] -> state` | `sismic` **silently ignored** an illegal transition and left the state unchanged, which is the one behaviour this feature exists to prevent; `transitions` works and raises, but injects methods onto the model dynamically and produced **9 mypy errors** under this project's strict settings, for a need that is a pure function; `python-statemachine` and `automat` both behave correctly and are viable, but neither keeps the table as data the way a dict does |
| durable append-only journal | **written here** on stdlib `sqlite3` + WAL + `synchronous=FULL` — measured **2.21 ms per commit** on the real XFS disk, which is the cost of a genuine fsync and therefore the proof that `commit()` returns only after the write is durable | `sqlite-utils` (`db.conn` **is** a `sqlite3.Connection` — identical durability, extra dependency); `eventsourcing` (its SQLite layer is the same `connect()`+`commit()`, wrapped in a DDD model this feature does not want); `litequeue` and `persist-queue` (job-queue shape, `persist-queue` pickles by default — unacceptable for an audit trail) |
| a Kite order-lifecycle or reconciliation library | **nothing exists** | GitHub repo-search returned `total_count: 0` for `"kite connect order state machine"`, `"kiteconnect reconciliation"` and `"zerodha kite order manager"`; every other hit was `zerodha/pykiteconnect` itself or a personal bot with the logic inline. `kitetrader` is an alternate REST client, not a lifecycle layer |
| property-testing the machine | **`hypothesis` 6.165.2**, already a project dependency. Its `RuleBasedStateMachine` was run twice — once against a correct model (invariant held over 50 examples × 20 steps) and once against a deliberately planted bug (a retry minting a fresh id), which it caught and **shrank to a two-step repro** | — |

---

## 13. Test plan (`R.23(c)`, written before the implementation)

- **unit** — the transition table, every legal and every illegal transition; the identity hash's
  determinism and its sensitivity to each field; the fold of the append-only journal.
- **property** — an intent submitted N times under arbitrary interleaving and crash points produces
  ≤1 broker order; fills applied in arbitrary order and with arbitrary duplication produce the same
  final position; the limiter never exceeds any bucket under any arrival pattern.
- **adversarial** — the broker reports a fill for an order we cancelled; a trade id repeats with a
  different quantity; the broker's clock disagrees with ours; reconciliation runs while an order is
  in flight; the journal contains a submission whose broker call outcome is genuinely unknowable.
- **hermetic** — the whole path against a fake venue behind the DI seam, including crash injection
  at every write point. `R.05` counts none of this as a real-data pass.
- **real_data** — against real `orders()`/`order_history()`/`trades()`/`positions()` state and real
  rejection responses. **The real-fill probe is deferred by operator decision (`A.99`) and is an
  open blocker, not a pass.**
