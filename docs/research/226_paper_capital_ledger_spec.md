# 226 · The paper trading book's money — a bounded, editable, event-sourced virtual ledger

**Feature**: `paper_capital_ledger` · **Operator decision**: `A.102` (2026-08-13) ·
**Plan entry**: `L1.18` · **Supersedes nothing; reconciles `A.06`/`L14.28` with `A.101`.**

---

## 1 · The question this answers

The operator's account carries a **debit of −88 rupees** (real, measured 2026-08-13, recorded in
`deployable_capital_resolver`'s header). `A.101` decided that deployable capital is
`min(broker balance, operator ceiling)` with the broker balance **floored at zero**, so the real
answer for that day is **₹0 deployable** and `is_tradeable = False`.

The operator asked the right question: *why should a real debit stop a paper book that trades
imaginary money?* It should not. It only would because `A.101`'s resolver was about to become the
single source of the sizing number for every mode. **The resolver's authority is hereby scoped to
LIVE.** Paper gets its own money, and this document specifies it.

## 2 · Why the paper book is BOUNDED at all — the conflict with `A.06`

`A.06` and `L14.28` state that **paper capital is unlimited**, and that this is a feature: with six
holons on overlapping underlyings, running long RELIANCE cash *and* futures *and* calls
simultaneously is a controlled experiment that measures which vehicle converts a correct view into
profit. An unlimited book is what makes those expressions non-competing.

That decision stands, untouched. What this specification adds is that **one unlimited book cannot
also be the book that produces graduation evidence**, for a reason that is arithmetic rather than
preference:

> **Unlimited capital has no denominator.** Return on capital, Sharpe, max drawdown as a percentage,
> Kelly fraction and exposure limits are all ratios whose divisor is the capital base. With an
> infinite base every one of them is either zero or undefined. An unlimited book can produce a
> *profit*; it cannot produce a *return*. The two-key arming rule (`R.22`) graduates a strategy on
> risk-adjusted evidence, and risk-adjusted evidence does not exist without a finite denominator.

Hence **two paper books over one signal stream**:

| | Experiment book (`A.06`, `L14.28`) | Trading book (this spec) |
|---|---|---|
| Capital | unlimited | finite, operator-set |
| Expressions per conviction | all of them, simultaneously | exactly one, as live would |
| Produces | the vehicle-conversion table | the risk-adjusted record graduation reads |
| Sizing source | none needed | `PaperCapitalLedger` |
| Live sizing authority | none | none — live uses `deployable_capital_resolver` |

Neither book is a degraded version of the other. They answer different questions and both are kept.

## 3 · What the operator asked for, exactly

> *"the virtual currency is editable on the dashboard as I need or wished"*

The figure is **operator-editable at any time, to any positive amount**, from the dashboard. It is
not capped at the live ceiling (`NSE_TRADING_CAPITAL_RUPEES`), because the point of a virtual book is
to explore capital regimes the operator does not currently hold — `R.03` already requires every
engine to work from ₹1 lakh to ₹1 crore, and that range cannot be exercised if the paper book is
pinned to today's funding.

**But an edit is never silent and never invisible.** Three properties enforce that:

1. **An edit is an event, not an overwrite.** It appends; it does not mutate. The balance that sized
   any past paper trade is recoverable forever.
2. **An edit that exceeds the live ceiling is STAMPED**, not blocked. `exceeds_live_ceiling` rides on
   every snapshot and on every surface, because a record produced on ₹1 crore while the operator can
   fund ₹10 lakh is not achievable evidence, and that must be known at the moment it is read rather
   than discovered at arming time.
3. **An edit cannot retroactively fund a past trade.** The fold is ordered by sequence, so a trade
   that was refused for want of capital stays refused in the record.

## 4 · Event model

Balance is **never stored as truth**. It is the fold of an append-only event log. The stored
checkpoint exists only so a long log need not be replayed on every read, and it is **verified
against a fresh fold** rather than trusted (`R.23e` — a cached number that can silently diverge from
its source is a defect, not an optimisation).

| Event | Moves balance | Moves committed | Meaning |
|---|---|---|---|
| `SEED` | + | — | The ledger's first and only creation event. Amount = the operator ceiling at creation time, read from `capital_configuration`, never a literal. |
| `OPERATOR_SET` | to an absolute figure | — | The dashboard edit. Records the previous balance, the new balance and a reason. |
| `OPERATOR_ADJUST` | ± a delta | — | Relative top-up or withdrawal, for modelling a deposit mid-run. |
| `POSITION_COMMIT` | — | + | Capital reserved by an opening paper order. |
| `POSITION_RELEASE` | — | − | Reservation returned when the position closes. |
| `REALISED_PROFIT` | + | — | Simulated exit in profit. |
| `REALISED_LOSS` | − | — | Simulated exit at a loss. |
| `COST_DEBIT` | − | — | Simulated brokerage, taxes, slippage and impact, from `nse_transaction_cost_engine`. Separated from `REALISED_LOSS` so `L1.11`'s cost attribution can read the two apart. |

**Why commitment is tracked separately from balance.** A paper book that decrements the balance when
a position opens and credits it back with P&L on close is the commonest naive design, and it
double-spends: between open and close, the capital is neither in the balance nor visibly at risk, so
a second signal sizes against money already deployed. Here `free = balance − committed`, and sizing
reads `free`. Balance moves only on realisation; commitment moves on open and close.

## 5 · Invariants

- **I1** — the log is append-only. No event is deleted or amended. A correction is a new
  compensating event carrying the corrected event's sequence in its reason.
- **I2** — `committed_rupees >= 0` always. A release without a matching commit is refused, because it
  would manufacture free capital.
- **I3** — `free_rupees = max(balance - committed, 0)`, and a `POSITION_COMMIT` exceeding `free` is
  **refused with `InsufficientPaperCapitalError`**, never truncated to what fits. A book that
  silently shrinks an order is not simulating the live discipline it exists to simulate.
- **I4** — an `OPERATOR_SET` below the currently committed figure is **accepted**, and the ledger
  reports `over_committed = True` with `free = 0`. It is not refused: the operator must always be
  able to state what their virtual capital is, and being over-committed is a visible state rather
  than an impossible one. New commits are refused until positions close.
- **I5** — the balance may not go negative through `REALISED_LOSS` or `COST_DEBIT`; it floors at zero
  and the shortfall is recorded on the event as `unfunded_shortfall_rupees`. A paper book cannot owe
  money to nobody, and silently carrying a negative balance would let a blown book keep trading.
- **I6** — every event carries `occurred_at` (IST, timezone-aware), a monotonic `sequence`, and a
  non-empty `reason`. An event with no stated reason is refused at write time.

## 6 · Interface parity with live

`PaperCapitalSnapshot` exposes the same four members `DeployableCapital` does — `deployable_rupees`,
`is_tradeable`, `binding_side`, `describe()` — so `F04`'s loop takes one `TradingCapitalSource`
protocol and swaps the implementation by mode. The loop must not contain an `if paper:` branch
around sizing; the mode is chosen once, at construction.

`binding_side` values for paper: `paper_free_capital` · `paper_exhausted` · `paper_fully_committed` ·
`paper_over_committed`.

## 7 · Verification plan

- **Unit** — every event kind, every invariant, every refusal path.
- **Property** — for any random legal event sequence, the fold equals the checkpoint, and
  `balance − committed` never disagrees with an independent recomputation.
- **Adversarial** — an edit below commitment; a release without a commit; a loss exceeding balance;
  a commit racing an edit; a checkpoint deliberately corrupted on disk; an event with an empty
  reason; a naive-double-spend sequence (two commits against one balance).
- **`R.05` real-data**: the ledger's inputs are simulated by construction, so its own real-data pass
  is the **dashboard round trip on the live server** — set a figure, reload, confirm the fold and the
  surface agree — plus its first real consumption by `F04`'s paper loop, which is an **open blocker**
  recorded in `BACKLOG.md` until that loop exists.

## 8 · Consumers (`R.06`)

- **Now**: `/paper-capital` dashboard surface (GET renders, POST edits) — visible on `/wall` via the
  module manifest, so `R.08` is satisfied at birth rather than promised.
- **Queued, named**: `F04` paper trading loop (sizing), `L1.10` capital-based position sizing (the
  paper-mode capital source), `L1.11` P&L attribution (reads `COST_DEBIT` apart from
  `REALISED_LOSS`).

## 9 · Sourcing — what was searched, run, and why rejected (`R.17`)

Searches run 2026-08-13 against PyPI from this box's venv: `eventsourcing`, `beancount`,
`python-accounting`, `pyledger`, `sqlalchemy-continuum`, `ledger-python`, `doubleentry`. Two
candidates were **installed and their real signatures introspected**; the rest failed on presence or
maintenance. No candidate was judged on its README.

**`eventsourcing` 9.5.4 — INSTALLED, INTROSPECTED, REJECTED FOR THE STORE.** Maintained (9.5.x
line), installs clean, pure-Python. Real signatures observed:
`Aggregate.trigger_event(self, event_class, **kwargs) -> None`,
`Application.save(self, *objs, **kwargs) -> list[Recording]`, `JSONTranscoder.register(transcoding)`.
Three mechanical facts decided it:
1. **`Decimal` is not natively transcoded.** Money round-trips only through a custom `Transcoding`
   this project would have to write and test — which is the bulk of the work the library was being
   considered to save.
2. **Its SQLite recorder owns its own schema** (`stored_events`, opaque blob payloads), so
   `COST_DEBIT` cannot be selected apart from `REALISED_LOSS` by a query. §8 names `L1.11` as exactly
   that consumer, so the library forecloses a named requirement.
3. It imposes an `Application`/`Aggregate`/`Repository` architecture against a repo whose two nearest
   analogues (`order_intent_journal`, `deployable_capital_resolver`) are plain `sqlite3` + frozen
   dataclasses. Two persistence philosophies in one codebase is a standing cost.

   **What was taken from it anyway**: the append-only-log-plus-verified-checkpoint shape, including
   never trusting the checkpoint over a fresh fold.

**`beancount` 3.2.3 — INSTALLED, INTROSPECTED, REJECTED ON SHAPE.** Its only entry points are
`load_file`, `load_string`, `load_encrypted_file`, `load_doc` — all parsers of Beancount's plaintext
DSL returning `tuple[Directives, list[BeancountError], OptionsMap]`. There is no write API: every
append would mean generating DSL text and re-parsing the whole file. It also has no reservation
concept, and `POSITION_COMMIT`/`POSITION_RELEASE` (§4) is the invariant that stops the double-spend.

**`python-accounting` 1.0.1 — REJECTED ON MAINTENANCE.** Two releases total; requires a SQLAlchemy +
MySQL-shaped configuration this project does not run.

**`pyledger` 0.5 — REJECTED ON MAINTENANCE.** Last line 0.5, versions from 0.0.0; no evidence of an
active line.

**`sqlalchemy-continuum`, `ledger-python`, `doubleentry` — not resolvable on PyPI** from this box.

**Conclusion**: build the store, borrow the pattern. Roughly 400 lines of ledger against ~200 lines
of custom transcoding plus an architecture mismatch plus a foreclosed consumer.
