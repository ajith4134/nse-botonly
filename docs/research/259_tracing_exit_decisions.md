# 259 · `M25` — exits, expiries and square-offs must emit their own decision trace

**Idea-intake verdict: COMPLETION of `L13.29`, ALREADY EXISTS.** No new plan entry. `L13.29` says
*"every bot emits, at decision time"* and `A.29` forbids reconstructing reasoning afterwards; the
built emitter covers only entries and abstentions, so this closes a hole in an existing entry rather
than adding a feature.

## 1. What is missing, measured

The live store holds 21,270 traces for 2026-08-17. Every one of them was emitted from
`_consider_entries`/`_act_on`. `_close_expired_or_reversed` and `_square_off_everything` call
`_square_off`, which reaches `_place` **without touching the trace emitter at all**.

So the record answers *"why did it get in?"* and *"why did it stay out?"* — and cannot answer
**"why did it get out there?"**, which is the question a reader asks about a position that lost
money. Every exit the system has ever taken is unexplained by its own record.

## 2. Why this was invisible until now

Found while building `B20` (`O.120`). The check "a refusing gate means the bot did not act" appeared
to hold on all live data. It appeared to hold because **the decisions that violate it are the ones
not being recorded**: when the halt latch trips, the session squares off through a refusing gate,
correctly and deliberately — and emits nothing. An absence in the record read as a confirmed
invariant. That is the sharpest argument for closing the gap: a missing record does not read as
missing, it reads as agreement.

## 3. The chokepoint

`_square_off` is the single path for all four exit causes, which is why the emitter goes there and
nowhere else — the same discipline that put the entry emitter at `_record`:

| Cause | Raised by | What decided it |
|---|---|---|
| `horizon expired` | `_close_expired_or_reversed` | `moment >= position.expires_at` — the calibration's own measured horizon |
| `signal reversed` | `_close_expired_or_reversed` | a fresh signal whose side opposes the open position |
| square-off before the close | `_square_off_everything` (`R.01`) | the session clock against the intraday deadline |
| halt | `_square_off_everything` after the latch trips | the latch, which is a REFUSING gate the exit correctly acts through |

## 4. The contract change

`DecisionTrace` gains `kind: DecisionKind` (`ENTRY` / `EXIT`), defaulting to `ENTRY` and persisted.
The default is not a convenience: the 21,270 stored rows genuinely are entries and abstentions, so
the default states a true thing about them rather than papering over an unknown.

The vocabulary and null action differ by kind, which is exactly why `permitted_actions` was built
per-trace rather than per-runner:

| | entry | exit |
|---|---|---|
| vocabulary | the engine's own action enum — `{abstain, enter_long, enter_short}` | `{hold, exit_long, exit_short}` |
| null action | `abstain` | `hold` |

## 5. What an exit trace records

- **inputs** — the position's entry price and unexited quantity, its `expires_at`, and for a
  reversal the opposing signal with its own `observed_at`. Provenance and timestamps as `L13.29`
  requires; `as_of` for the position is its entry instant, which is genuinely when that value became
  knowable.
- **candidates** — `hold` (always available) and the exit actually taken.
- **gates** — the exit cause as a *signed, normalised* gate, so `binding_constraint()` works on
  exits the same way it works on entries:
  - `holding_horizon`: margin = seconds remaining until `expires_at`, threshold = the horizon in
    seconds. Negative once expired, which is the gate refusing to keep holding.
  - `signal_alignment`: boolean — no margin, refused when the fresh signal opposes the position.
  - `session_clock`: margin = seconds to the square-off deadline, threshold = the session length.
  - `halt_latch`: boolean, refused when the latch tripped.
- **mechanism** — the `reason` string already carried into `TradingIntent.strategy_identity`, so the
  trace and the order agree by construction rather than by a second author writing prose.

## 6. What this does NOT do, and why

**It does not enforce "a refusing gate implies the null action", for entries or exits.** `O.120`
records why: on exits the rule is false by design (a square-off acts through a refusing latch), and
on entries it is unfalsifiable — the emitter sets a non-null action only where every gate has
already passed, so no entry can violate it. A check that cannot fail on one path and must not fire
on the other is not worth its docstring.

## 7. Verification

- **Unit** — a trace of each of the four causes; the exit vocabulary refuses `enter_long`; the
  default `kind` is `ENTRY` and stored rows written before this change read back as `ENTRY`.
- **Property** — for an exit, `binding_constraint()` names the cause that actually fired, and does
  so across gates whose margins are in seconds and booleans.
- **Adversarial** — an exit trace must not be droppable into `_traces_skipped` more often than an
  entry one; a fresh subagent attacks the gate construction for fabricated margins.
- **Real data (`R.05`)** — replay 2026-08-17 and confirm the exit traces equal the exits the session
  independently reports, cause for cause. If the session squared off 11 positions on the horizon and
  the store holds a different count or a different reason, the trace is fiction.

## 8. Sourcing (`R.17`) — searched, not recalled

Searched 2026-08-17 across trading-engine event models, decision/audit-trail packages, structured
tracing, and LP sensitivity analysis. Queries run included `NautilusTrader position closed event
reason`, `Lean order exit reason audit trail`, `backtrader notify_trade reason`, `vectorbt exit
reason record`, `Hummingbot CloseType enum`, `python decision log audit trail explainability PyPI`,
`python event sourcing append-only audit trail`, `opentelemetry span decision evidence
counterfactual semantics`, `cvxpy dual values binding constraint normalized slack`, `scipy linprog
which constraint closest to binding`, and `pyoptexplain post-optimality explanation` — with PyPI
JSON and GitHub API pulls for stars/last-push on every named candidate.

| Candidate | What it supplies | Verdict |
|---|---|---|
| NautilusTrader (★25.7k, active) | `OrderDenied`/`OrderRejected` carry one flat `reason` string; `PositionClosed` carries none | reject — wrong shape: no evidence list, no per-gate margins, no counterfactual |
| QuantConnect/Lean (★21.2k) | `OrderEvent` + hand-rolled `self.Log()` | reject — no structured close reason exists |
| backtrader (★22.9k, last push 2024-08) | `notify_trade` gives P&L, no reason field | reject — unmaintained AND the field does not exist |
| vectorbt (★8.7k) | `StopType` enum per exit | reject — one categorical code; closest prior art for the *idea* of a typed exit cause |
| Zipline (★20k, dead upstream) | `record()` for scalar series | reject — dead, and no decision concept |
| Hummingbot (★19.5k, active) | `CloseType` enum on `PositionExecutor` | reject — same category as vectorbt |
| `eventsourcing` (★1.7k, active, BSD-3) | mature append-only replayable event store | reject as a dependency — **our store already enforces append-only in the DATABASE** via two `RAISE(ABORT)` triggers, which is stronger than a library convention; it carries zero domain semantics, so it would add a dependency and still leave the whole record to build |
| `prov` (W3C PROV-DM, ★137) | Entity/Activity/`wasGeneratedBy` provenance graphs | reject — right vocabulary, wrong altitude: an RDF lineage stack for one feature. Its Entity/Activity split is worth borrowing conceptually, and `ConsultedInput` already has that shape |
| `ai-audit-trail` (★4, 3 months stale) | Ed25519-signed compliance "decision receipts" | reject — tamper-evidence for an LLM compliance regime, not an evidence + margin + counterfactual record |
| `semantica` (★8.4k, very active) | LLM-agent accountability platform, PROV-O, causal graphs | reject — too heavy and the wrong problem: a multi-service agent-reasoning platform, not an in-process record at position close |
| OpenTelemetry | spans with attribute bags | reject as engine — spans are latency/causality-shaped; no convention for evidence or counterfactuals, and no support for the ranking step. Confirms `257`'s original rejection |
| `structlog` | structured JSON logging | reject as engine — an emission sink, no decision model |
| `scipy.optimize.linprog` / cvxpy duals / PuLP | shadow prices and slack after solving an LP | reject — **solvers**. They produce duals only for constraints solved simultaneously in one LP; these gates are independently evaluated in different units and already computed. Valuable confirmation, though: "normalise each margin by its own threshold, then rank" is precisely the LP sensitivity idiom, so `binding_constraint()` is the standard method rather than an invention |
| `pyoptexplain` (★2, ~1 month old) | uniform cross-backend "why / what-if" post-optimality explainer | reject — immature (2 stars, one maintainer, no hardening) and consumes solver model objects, so using it would mean re-expressing the gates AS an optimisation model, which is backwards. **The single closest conceptual analog found; worth re-checking if it matures** |
| CARLA | counterfactual recourse for ML classifiers | reject — wrong domain: feature recourse, not gate ranking |

**Conclusion: nothing is integrated, and the search sharpened the design rather than merely
confirming it.** No package supplies the shape needed — evidence with provenance, candidate actions,
heterogeneous signed-margin gates, and a counterfactual by normalised margin, emitted append-only at
the close. The trading frameworks all top out at a single categorical exit-reason code, which is
exactly what §4 of this document would have settled for had the search not been run; the LP
literature confirms the normalisation step is textbook; and the append-only requirement is already
met more strongly in the database than a library would meet it in Python.

**Two rejections flagged for operator double-check (`R.17`, the rule formerly numbered O.1):** `eventsourcing` (genuinely mature and
a real fit for the persistence layer — rejected because the triggers already do it, which is a
judgement call, not a defect in the library) and `pyoptexplain` (rejected for immaturity rather than
for being wrong-shaped in principle). Either can be vendored on request.
