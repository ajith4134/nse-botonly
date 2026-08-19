# 267 · Continuous six-segment paper trading — spec, sourcing and acceptance criteria

*Written 2026-08-19, before any code, under `A.146`. Governs the whole slice: the six segment bots
trade a single paper book against the live tape while the exchange is open, and walk forward through
the archive while it is closed.*

---

## 0 · What is being built, and what it replaces

The six bots already exist and conform (`L5.29`, 126 tests green, zero violations). What does not
exist is a **path from a proposal to a closed trade on a schedule**. Measured before writing this:

| # | gap | evidence |
|---|---|---|
| S0 | `fo_bhavcopy_contracts` — the table every derivative bot's universe is assembled from — stops at **2026-08-03** while `BitemporalIngestStore` holds `nse_bhavcopy_fo` through **2026-08-18 (35,433 rows)**. **Nothing in `src/` or `scripts/` writes that table**; all four references read it. Five of six bots have therefore been deciding on 16-day-old contracts, including two weekly expiries that have already settled. |
| S1 | depth capture subscribes `segment == "NSE" and instrument_type == "EQ"` (`scripts/record_live_depth_session.py:118`). Today's tape measured: **2,295 distinct tokens, every one `NSE` cash, zero `NFO-OPT`/`NFO-FUT`**. `A.142` decided to widen this and it was never implemented. |
| S2 | `0 panels mature` at every live tick — `B43`: the reversion engine needs 20 window bars + 60 banded observations = **80 five-minute bars**, and a session is **75**. A loop that starts cold can never band. |
| S3 | the loop assembles the CASH universe only (`run_continuous_paper_trading_loop.py:73`) and `continue`s past every non-cash bot (`continuous_paper_trading_scheduler.py:480`). |
| S4 | the loop OBSERVES and never EXECUTES — it counts proposals and imports no venue, no ledger, no track record. |
| S5 | market closed is *"deliberately deciding nothing"* (`scheduler.py:454`). There is no historical branch. |
| S6 | futures cannot be sized: `B36` (no SPAN file) and `B38` (index margin **5.02%** estimated against **11–12%** quoted). |
| S7 | no route shows per-bot open positions, fills or P&L. `/bots` and `/ladder` are aggregate; `/orders` and `/paper-session` are bot-agnostic and show the last recorded run. |

`SegmentBotPaperSession` (629 LOC, all six bots, real cost model) exists and is **imported by
nothing** — an `R.06` orphan. It is the daily-cadence engine this slice wires in as the walk-forward
worker rather than a second implementation.

---

## S0 · Derivative contract record projection

**What it is.** A projection engine that materialises `nse_bhavcopy_fo` observations out of the
bitemporal ingest store into the queryable contract table the five derivative universes read, and
keeps it current. Not a scalar: it is the raw→queryable pipeline layer of the derivative side, the
exact counterpart of what `price_bars` is for cash.

**Algorithm.** Read every effective date present for `nse_bhavcopy_fo`; for each observation derive
the contract's natural key (underlying · contract type · expiry · strike · right), coerce NSE's
string payload into typed columns, and upsert idempotently. Two things the legacy table does not
carry are added because the rest of the slice needs them:

- **`total_traded_value`** (`TtlTrfVal`) — the liquidity measure S1 ranks option contracts by. The
  cash side already ranks on exactly this field; the derivative side had no equivalent.
- **`availability_time`** — the instant this project first OBSERVED the row (`observed_at`), so a
  point-in-time query can ask "what did we know at time T" rather than "what is true now". Without
  it every walk-forward replay in S5 leaks the future.

Also carried: `lot_size` (`NewBrdLotQty`), `instrument_name` (`FinInstrmNm`), `trades_executed`
(`TtlNbOfTxsExctd`).

**State.** The table itself, plus a per-source projection cursor so a re-run is incremental rather
than a full rebuild.

**Why upsert into `fo_bhavcopy_contracts` rather than a new table.** The legacy table holds 36
effective dates from **2026-06-08** and the ingest store holds 28 including dates the legacy table
does not. Replacing it loses the union; a new table orphans four working readers. The columns are
added by `ALTER TABLE`, `NULL` on the legacy rows, and every consumer that needs them filters on
`IS NOT NULL` rather than assuming.

**Decision changed.** The universe five bots decide over. Directly measurable: expired contracts
leave the universe and the current expiry enters it.

**Acceptance.** `max(trade_date)` equals the latest ingested F&O session; a second run inserts zero
rows; every projected row's `availability_time` is `>=` its `trade_date`; the assembled `IDO`/`STO`/
`IDF`/`STF` universes contain no contract whose expiry is in the past relative to the as-of instant.

---

## S1 · Derivative capture universe selection

**What it is.** The engine that decides which F&O tokens the depth capture subscribes, so the five
non-cash bots get an intraday tape. Operator decision (`A.146c`): **all futures + near-expiry
option chains around the money**.

**Algorithm — the band is MEASURED, not typed (`R.03`).** A strike count ("ATM ± 10") is exactly the
magic number the rules forbid, and it is also wrong: a NIFTY 50-point strike ladder and a stock
option's 20-rupee ladder do not contain the same amount of the distribution. Instead:

1. **Futures** — every `IDF` and `STF` contract of the nearest unexpired expiry per underlying.
   These are ~640 contracts in total; no cut is needed.
2. **Options** — for each underlying, take the nearest unexpired expiry, then rank its strikes by
   the contract's **own traded value** from the most recent projected session (S0's
   `total_traded_value`). Liquidity concentrates around the money as a market fact, so the band
   emerges from the measurement instead of being asserted — and where it does not (a skewed
   underlying, an expiry-week pin), the measurement is right and the assertion would have been
   wrong.
3. **Ranking across the whole candidate set** is by that same traded value, so the existing
   `DepthCaptureAdmissionController` — which already solves a token budget against measured packets
   per second and a disk fraction — cuts the list on evidence it already trusts. The cash candidates
   keep their own ranking and both streams are merged before admission, so widening cannot silently
   evict cash coverage: the controller sees one ranked population.
4. Contracts with no Kite `instrument_token` are dropped, not synthesised. `segment_universe_assembler`
   invents a negative SHA-derived token for its own use; a token that cannot be subscribed must
   never reach a subscription list.

**State.** None carried beyond the projected table; the selection is a pure function of (instrument
master, projected contracts, as-of instant), which is what makes it testable.

**Decision changed.** What the websocket subscribes, and therefore whether five of six bots have any
intraday price at all.

**Acceptance.** The selected set contains zero expired contracts; every token resolves in
`instrument_master` with `segment` in `{NFO-FUT, NFO-OPT}`; the merged candidate list preserves the
cash ranking's order among cash tokens; after one capture session the tape contains `NFO` tokens
(the measurement that fails today).

---

## S2 · Warm-start seeding

**What it is.** The engine that fills each bot's carried state from stored history at loop start, so
the first live tick decides against a mature panel rather than an empty one. Closes `B43`.

**Algorithm.** For each instrument in the cash universe, read the stored five-minute bars ending at
the loop's start instant, ordered by `availability_time <= as_of` (point-in-time, the same predicate
the universe assembler uses), and replay them through the same `observe` path a live bar takes —
never a second code path, because a seeded engine and a live-fed engine that disagree is a defect
that only shows up in production. Seed depth is derived from the engine's own requirement
(`window + MINIMUM_OBSERVATIONS_FOR_BANDS`), imported rather than typed, with a margin equal to one
session so a warm engine stays warm across a restart mid-session.

**Why not shrink the window.** `A.106`: the `reversion_calibration` was fitted at the current depth;
banding at a different depth measures the instrument at a depth it was never calibrated at.

**State.** The bots' own carried state — this engine writes into it and holds none of its own.

**Decision changed.** Whether any proposal is possible at all in the first 1.07 sessions of a
process's life.

**Acceptance.** After seeding, `instruments_with_a_panel > 0` at the first tick; a seeded engine and
an engine fed the same bars live produce byte-identical band state (property test); seeding respects
`availability_time` so no bar published after the seed instant is used.

---

## S3 · Six bots on their own cadences in one loop

**What it is.** The scheduler stops being cash-only. Each bot is stepped on the cadence its data
actually supports, and the cadence is a property of the bot's segment rather than a branch in the
loop.

**Algorithm.** A per-segment universe is assembled and refreshed; each bot receives ITS OWN
`SegmentBotContext` built from its own universe and its own price source. Cadence:

- **cash** — every five-minute bar close, as today.
- **derivatives, once the F&O tape exists (S1)** — the same five-minute clock, because a tape is a
  tape; the option and futures engines are cadence-agnostic by construction and `A.141` put them on
  daily closes only because there was no intraday tape to put them on.
- **derivatives, while the tape is absent** — once per session on the projected daily closes, and
  the card records which cadence it was decided on so a daily decision can never be read back as an
  intraday one.

The cadence a bot actually got is recorded per iteration, not inferred later.

**Acceptance.** Every iteration records six per-bot rows; a bot with an empty universe records an
empty universe rather than being skipped; the MCX bot records `NO DATA` and is not silently absent.

---

## S4 · Live execution and the one portfolio book

**What it is.** The path from a proposal to a fill to a closed trade, on the live tape, under one
book. Operator decision (`A.146d`): **one portfolio book with portfolio-wide bounds**.

**Algorithm, in order per tick:**

1. Collect every bot's proposals for this instant.
2. **Portfolio supervisor** — splits deployable capital across the six and applies the
   net-directional bound ACROSS bots, closing `B39` (measured: `index_futures` 100% directional,
   portfolio 43.1%, against a 25% bound nothing applied at that level). The bound is applied on the
   DIRECTION OF TRAVEL, per `docs/research/261`'s finding that a bound stated as a fraction of gross
   cannot be met by the first position and refuses everything.
3. **Sizing** — cash on the verified VaR+ELM margin file; stock futures on `FuturesMarginEstimator`;
   index futures REFUSED with a named blocker until S6's SPAN file arrives (`B38`); options on
   premium plus the short-option margin the estimator already derives.
4. **Execution** — `SimulatedExecutionVenue` against the live tape's recorded book. Not a mid-fill:
   it walks the real L2 ladder, gives resting orders a queue position, and REFUSES an instrument
   with no recorded book rather than fabricating a reference price.
5. **Square-off** — in the `SQUARING_OFF` phase, flatten only (`R.01`).
6. **Accrual** — every closed trade to `PaperTrackRecordStore` under its own bot identity, priced
   both legs through `NseTransactionCostEngine`, with the ledger updated before the order goes.

**State.** Open positions carried across ticks in the scheduler, the capital ledger, and the track
record — all three persisted, so a restart mid-session resumes rather than orphans.

**Acceptance.** A live session produces fills attributable to a named bot; the ledger fold agrees
with the reported P&L; nothing is open after the close; a `kill -9` mid-session resumes with the
same open positions.

---

## S5 · Walk-forward archive replay

**What it is.** What the loop does while the exchange is closed. Operator decision (`A.146a`):
**walk forward through the archive, never repeating a session**.

**Algorithm.** A persisted cursor names the last archived session replayed. While closed, the loop
takes the next unreplayed session, runs `SegmentBotPaperSession` (the orphan this wires in) over it
with every bot assembling THAT session's own universe point-in-time, accrues closed trades marked
`replayed` so historical evidence is distinguishable from live, advances the cursor, and repeats
until the next open. A session is replayed at most once; the cursor is the reason.

**Why walk-forward rather than re-replay.** Re-replaying the latest session accrues no new evidence
after its first pass — the ladder stops moving while the loop looks busy, which is `O.134`'s exact
failure shape.

**Acceptance.** The cursor advances; a second pass over the same session inserts zero trades; a
`replayed` trade and a `live` trade are distinguishable in the store; the ladder's rung changes as
sessions accrue.

---

## S6 · SPAN risk-parameter loader

**What it is.** The adapter that turns NSE's own `.spn` file into real futures and short-option
margin, superseding the estimator the moment the file exists.

**Sourcing (`R.17`).** `marginism` 0.1.1 is installed and its signatures verified — `parse_spn`,
`SpanCalculator`, `RiskEngine`, `MarginResult.span_margin|exposure_margin|total_margin`. Its own
documentation states *"Users are responsible for fetching the file themselves"*, which is decisive:
the file sits behind an interactive React page that timed out at 90 s and again at 60 s under
Chromium, so no adapter can fetch it. **One manual download is required** and this engine is built
to activate on it without further work.

**Algorithm.** Watch a drop directory for `nsccl.<YYYYMMDD>.s.spn`; parse with `marginism`; expose
`span_margin` + `exposure_margin` per contract; the sizing layer prefers it over the estimator
whenever a file covering the session exists, and index futures unlock because the scan ranges `B38`
needs are inside that same file.

**Acceptance.** With no file present, behaviour is exactly today's (estimator for stock futures,
refusal for index) and the blocker is named on the surface; with a file present, sizing switches and
says so.

---

## S7 · Per-bot live trading surface

**What it is.** The route a human opens to watch the six bots trade. `R.08`: measured from the
stores, never hand-authored.

**Contents.** Per bot: phase and cadence, universe size and how much of it is priced right now, open
positions with unrealised P&L marked to the live tape, today's fills, realised P&L net of costs,
rung, and the named blocker if it has one. Above them: the portfolio book — deployed capital, net
direction, the bound and how close it is. Below: the walk-forward cursor and what it last replayed.

**Acceptance.** HTTP 200 with real numbers during a live session; registered in `SURFACED_MODULES`
and in the screenshot capture; no number on the page is computed anywhere but the stores.

---

## Verification plan (`R.05`)

Each slice carries its own real-data script under `scripts/`. The slice is not signed off on a unit
suite: S0 against the real ingest store, S1 against the real instrument master and a real capture
session, S2 against the real bar history, S3–S4 against a live session with the market open, S5
against real archived sessions, S6 against the real `.spn` file when it arrives (stated as the one
open blocker until then, `R.11`), S7 against the running server.

## Open blockers carried into this slice, stated rather than implied (`R.11`)

- `B30` — MCX has no data; the commodity bot is built whole and activates on nothing (`A.142`,
  operator-deferred).
- `B36`/`B38` — the SPAN file needs one manual download; index futures do not size until then.
- `B28` — the trade-quality floor stays held off; no bot trades behind a gate known to be broken.
