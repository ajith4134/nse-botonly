# 224 · How five mature systems build the order path, read from their source

Gathered 2026-08-13 for `F02` (`A.99`) by reading the actual repositories — NautilusTrader
(`nautechsystems/nautilus_trader`, Rust core), Hummingbot, freqtrade, QuantConnect LEAN, and
ccxt + OctoBot-Trading — not their documentation. Every claim below carries the file and line it
came from, because `R.17` counts README prose as no evidence at all.

This is the `R.23(a)` analog for `F02`: **NautilusTrader's execution subsystem is the depth target.**
Its `crates/execution/src/` is 38,075 lines of source (72,395 with tests), of which the
reconciliation module alone is 10,331, with 834 inline test functions plus property-based fuzzing of
the reconciliation state machine. The others are between a third and a tenth of that, and the
difference shows up directly as missing correctness properties rather than as missing features.

---

## 1. The state machine

| System | Where | Shape | Illegal transition |
|---|---|---|---|
| **NautilusTrader** | `crates/model/src/orders/mod.rs:201-285` | explicit `match (state, event)` table, ~60 arms, 15 states (`enums.rs:1416-1447`) | typed `Err(OrderError::InvalidStateTransition)` (line 282) |
| Hummingbot | `core/data_type/in_flight_order.py:21-32,327` | 11 states, **no table** — `update_with_order_update` overwrites | not detected at all |
| freqtrade | `persistence/trade_model.py:100,205` | raw **string**, no enum | unknown status stored and treated as open |
| LEAN | `Common/Orders/OrderTypes.cs:138-184` | enum, direct assignment | unmatched order id logged and **dropped** (`BrokerageTransactionHandler.cs:1189-1191`) |
| OctoBot | `personal_data/orders/order.py:187-193` | per-state classes | silently ignored, debug-logged |

**Copied:** the table-as-data plus a typed refusal. It is the single biggest differentiator in the
set, and the four systems without it all hide the same class of bug.

## 2. Idempotency

- **NautilusTrader** — `ClientOrderIdGenerator`
  (`crates/common/src/generators/client_order_id.rs:92-105`) produces a deterministic
  `O-{date}-{time}-{trader}-{strategy}-{count}`, and `ExecutionEngine::handle_submit_order`
  (`crates/execution/src/engine/mod.rs:2074`) checks `cache.order_owned(&client_order_id)` **before**
  materialising an order — a hit reuses rather than resubmits.
- **Hummingbot** — `connector/utils.py:50-83` mixes `os.getpid()` into the id, so the same intent
  gets a different id after a restart. The id is cosmetic.
- **freqtrade** — no client order ids anywhere. Its only protection is that `create_order`
  (`exchange.py:1445`) is deliberately **not** wrapped in the `@retrier` decorator that ~15 other
  calls use. An undocumented, implicit safeguard.
- **LEAN** — `SecurityTransactionManager.GetIncrementOrderId()`
  (`Common/Securities/SecurityTransactionManager.cs:560-563`) is an in-memory counter; a restart
  reassigns fresh ids, breaking correlation to anything placed before the crash.
- **ccxt** — `create_order` in the base class is literally `raise NotSupported`
  (`exchange.py:8335-8336`). OctoBot generates a `uuid4` it never sends to the venue
  (`order_util.py:758-759`).

**Copied:** the deterministic id plus the pre-submit lookup. **Refused:** every "id that changes
across a restart", which is the same as having none for the one purpose it exists.

This system's variant is stronger by necessity: Kite accepts no client order id at all
(`docs/research/222` §1), so the identity has to be recomputable from the decision itself and
smuggled through the 20-character `tag`.

## 3. Durability, and the ordering that matters

- **NautilusTrader** writes to the cache at `engine/mod.rs:2081` and calls the venue at `:2142` —
  **write first**. Backends behind `CacheDatabaseAdapter` (`crates/common/src/cache/database.rs:77`),
  replayed by `Cache::cache_orders()` (`cache/mod.rs:2436-2457`).
- **freqtrade** calls the exchange at `freqtradebot.py:963` and commits at `:1029-1067` — a real
  crash window with no compensating write.
- **LEAN** keeps orders in a `ConcurrentDictionary` and dumps order events to JSON once per UTC day
  (`LiveTradingResultHandler.cs:336-340`); a mid-day crash loses the day.
- **Hummingbot** tracks in memory before the call but persists to SQL only **after** the ack.
- **OctoBot** stores an order it has already fetched back from the exchange.

**Copied:** write-then-call, and only that ordering. Three of the five have the window; the entry
`L3.02` exists precisely to close it.

## 4. Reconciliation

**NautilusTrader** is the only real implementation: `crates/execution/src/reconciliation/orders.rs`.
`reconcile_order_report()` (`:311-409`) converges local state to venue truth; venue-only orders are
adopted via `materialize_external_order_from_status()` tagged `EXTERNAL`; local-only in-flight
orders are resolved by a timeout checker (`crates/live/src/execution/manager.rs`) into `REJECTED` or
`CANCELED`; and **`create_incremental_inferred_fill()` (`:1051-1131`) synthesises a real
`OrderFilled` event for the quantity gap**, flagged `reconciliation=true` and keyed by a
deterministic inferred trade id so a replay is idempotent. The consistency table is written down as
documentation and coded against (`docs/concepts/reconciliation.md:200-224`), including the cases it
deliberately leaves unresolved.

Everything else in the set only re-polls orders it already knows about — Hummingbot
`exchange_py_base.py:1042-1046`, freqtrade `freqtradebot.py:402-447` — which means **an order placed
just before a crash and never persisted is permanently invisible**. LEAN adopts the broker's
open-order snapshot blindly with fresh local ids (`BrokerageSetupHandler.cs:485-506`), a one-way
import rather than a diff.

**Copied:** full venue-state fetch and diff, venue wins, inferred fills that stay marked as
inferred, and the consistency table as data. **Refused:** poll-only-what-you-know, and blind import.

## 5. Partial fills

- **NautilusTrader** — `OrderCore::filled()` (`orders/mod.rs:1231-1272`) with saturating arithmetic,
  and a trade-id dedup check **before** the transition is applied (`:833-840`,
  `OrderError::DuplicateFill`).
- **LEAN** — matches on order id only, and `OrderEvent.Id` is self-generated rather than keyed on the
  broker's execution id, so a re-delivered fill double-applies. No guard found.
- **OctoBot / Hummingbot / freqtrade** — absolute overwrite from the venue snapshot
  (`order.py:1090`), so a stale or out-of-order poll can make `filled_quantity` go **backwards**.

**Copied:** dedup-by-trade-id before the transition, and monotonic cumulative quantities. This
matters more here than anywhere else in the set, because Kite's postbacks carry no delivery guarantee
(`docs/research/222` §3) and duplicates must be assumed.

## 6. Paper/live parity

NautilusTrader's `ExecutionClient` trait (`crates/common/src/clients/execution.rs:53`) is
implemented by both `BacktestExecutionClient` and every live adapter, with a `LatencyModel` and a
`SimulatedExchange` injected on the backtest side and **the same report types flowing into one
reconciliation path**. freqtrade's single branch point (`exchange.py:1445-1462`,
`if dry_run: ... else: self._api.create_order(...)`) is worth copying for its plainness even though
the rest of freqtrade's order path is the weakest here.

Hummingbot's paper exchange walks the **real order book** to price a fill
(`paper_trade_exchange.pyx:445-520`) — the right idea, and the one this system already owns through
`execution_fill` (`L1.05`/`L1.06`). OctoBot's simulator force-fills the whole requested quantity
(`order.py:1074-1079`), which is the fantasy this project's `F01` work exists to avoid.

## 7. Rate limiting

- **NautilusTrader** throttles order submission at the **risk layer**
  (`crates/risk/src/engine/mod.rs:99-100`, `throttled_submit`/`throttled_modify_order`) using a
  sliding-window `Throttler` (`crates/common/src/throttler.rs:124`), kept separate from the per-venue
  HTTP rate limiter in `crates/network/src/ratelimiter`.
- **ccxt** uses a genuine leaky bucket in async mode (`async_support/base/throttler.py`) and a fixed
  delay in sync mode (`exchange.py:449-456`).
- **Hummingbot** has a sliding-window log throttler applied to every REST call
  (`api_throttler/async_throttler.py`, via `rest_assistant.py:94`).
- **LEAN** has a `RateGate` token bucket (`Common/Util/RateGate.cs`) that **the order path never
  calls** — it is used for market data only.
- **freqtrade** has none of its own and leans on ccxt's default.

**Copied:** the separation of "don't flood the venue with orders" from "don't exceed the API's
request quota", and sliding-window accounting so a burst across a boundary cannot double-allow.
**Diverged deliberately:** the regulatory limit here is measured on the **broker's calendar second**
(`docs/research/223` §3), so the order bucket is calendar-aligned rather than sliding. A sliding
window would be the safer choice against a rolling limit and the *wrong* one against this limit,
which is measured the way it is measured.

## 8. Where the best of them is still wrong

Recorded so the target is not treated as infallible: NautilusTrader carries a `// TODO: fix` for
stop-to-limit trigger-price propagation in the order emulator (`order_emulator/emulator.rs:1345`),
flags its matching-engine clock freezing as not fully correct (`matching_engine/mod.rs:3630`), and
knowingly leaves `PENDING_UPDATE`/`PENDING_CANCEL` unresolved where a venue cannot distinguish
"missing" from "recently closed" (`docs/concepts/reconciliation.md:220-224`) — an order can sit
ambiguous indefinitely. This system inherits that problem in a sharper form, since Kite publishes no
propagation-latency figure at all, and answers it with an explicitly measured **visibility horizon**
rather than an unbounded wait.

## 9. Test investment

Only NautilusTrader property-fuzzes its reconciliation state machine
(`crates/execution/proptest-regressions/reconciliation/`). `hypothesis` is already a project
dependency, and `RuleBasedStateMachine` is the equivalent instrument — the "one intent never becomes
two orders under arbitrary interleaving" claim is exactly the kind that only stateful property
testing can support.
