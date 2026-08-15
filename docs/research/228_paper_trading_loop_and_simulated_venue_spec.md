# 228 · A trading day that runs itself — the paper loop and the venue that fills it

**Feature**: `F04` (first slice) · **Operator decisions**: `A.108` (2026-08-13) ·
**Consumes**: `F02` order path · `F03` sizer and gate · `L1.18` paper capital ledger ·
`session_risk_state_store` — **all four get their first real caller here.**

---

## 1 · What this feature is for

Everything built in this rebuild so far is correct and inert. `F02`'s order path has never carried
an order it did not manufacture in a test. `F03`'s sizer answers "how much" for nobody. The paper
capital ledger has had one commit/release pair written by a probe. `SessionRiskStateStore`'s entire
write surface — `open_session`, `record_realised_pnl`, `record_exposure_change`,
`record_order_sent`, the latch trips — has never run outside a fixture.

`F04` is the thing that makes all of them load-bearing at once, and the first honest answer to
"does any of this work together".

## 2 · The clock: replayed bars (`A.108` decision 1)

The loop steps through recorded five-minute bars point-in-time. It runs at any hour, reproduces
identically, and produces evidence tonight rather than at 09:15 tomorrow — 659,990 real bars are
already on disk.

**The leakage guard is the load-bearing part, and it is exactly one thing.** Bars are read on
`availability_time <= decision_instant`, never on `bar_timestamp`. That single predicate is all that
separates a replay from a backtest that sees the future, so it is exercised on every step rather
than asserted once. §7 requires an adversarial test that plants a bar stamped BEFORE the decision
and published AFTER it, and proves the loop cannot see it.

**Recorded cost** (`A.108`): the live tick path — latency, gaps, mid-session disconnects — stays
unverified until the live clock lands. That is an open blocker from the day this ships.

## 3 · The fill path: through `F02`, against a simulated venue (`A.108` decision 2)

```
signal → F03 sizer → F03 gate → TradingIntent → F02 journal → F02 state machine
                                                      ↓
                                          SimulatedExecutionVenue
                                                      ↓
                                    fills → reconciler → paper capital ledger
```

Only the venue is simulated. The intent journal, the lifecycle state machine, the broker-truth
reconciler and the crash recovery are the real ones, so **the code that will one day carry real
money is the code being tested now**. `R.13` is the argument: correctness of allocation is not
evidence of correctness of execution, and a paper record produced by code that gets thrown away
before live proves only the first.

## 4 · The venue is the risk, and this project already has the right input

`A.108` records it plainly: a venue that fills everything at the touch teaches the system that
slippage does not exist, and every paper P&L would then be optimistic by the full spread plus all
impact — against precisely the `F01` cost model that graduation reads.

**The sourcing answer is not a library.** This system already records **1.3 GB of real L2 depth**
(`~/nse_archive/depth_tape`, read by `market_depth_tape_store` and replayed by
`order_book_snapshot_replay_engine`, already surfaced at `/microstructure`). A queue-position
simulator against a real recorded book is strictly better evidence than any bar-volume slippage
formula, because the book is what actually rejected or filled an order that day.

So the venue fills against the **recorded book at the decision instant**:

- a marketable order walks the real ladder, level by level, and its average fill price is the
  volume-weighted walk — which IS the spread and the impact, measured rather than modelled;
- a limit order that rests joins the queue **behind** the size already showing at its price, and
  fills only as that size trades away — queue position, not optimism;
- an order larger than the visible ladder **partially fills** and the remainder rests or is
  cancelled at the session close, which is the commonest end-of-day event in an intraday book and
  the one `A.100` proved could corrupt the journal;
- a scrip with no recorded depth at that instant is **refused**, never filled at the last close.

## 5 · Sourcing — what was probed, run, and why rejected (`R.17`)

Probed 2026-08-13: `nautilus_trader`, `backtrader`, `vectorbt`, `zipline-reloaded`, `lean`. Two were
already installed in this venv and their fill models introspected directly.

**`zipline` 3.1.1 — ALREADY INSTALLED, INTROSPECTED, REJECTED as the fill model.** Real signatures:
`VolumeShareSlippage(volume_limit=0.025, price_impact=0.1)` and `VolatilityVolumeShare(volume_limit,
eta={...})` carrying a per-future eta table. `VolumeShareSlippage` is the classic
`price_impact × (order share of bar volume)²` model — respectable, citable, and **bar-based**: it
never represents a resting order, a queue, or a partial fill across the book. It infers impact from
volume because it has no book. This project HAS the book, so using a formula that estimates what the
ladder would have done, while the ladder itself sits on disk, is choosing the weaker evidence.
**What is taken from it**: the volume-participation cap as a sanity bound — an order may not claim
more of a bar's volume than actually traded.

**`backtrader` 1.9.78.123 — ALREADY INSTALLED, INTROSPECTED, REJECTED.** `BackBroker` exposes
`slip_perc`, `slip_fixed`, `slip_open`, `slip_match`, `slip_limit`, `slip_out` and a pluggable
`filler` (`set_filler`, `set_slippage_fixed`, `set_slippage_perc`). The fillers do model volume
participation per bar, which is closer to partial fills than zipline's. Still bar-based, still no
queue, and adopting its broker means adopting its `Cerebro` engine and data feeds wholesale — this
project has its own order path and would be inverting control to a framework to reuse one component.

**`nautilus_trader` 1.231.0 — NOT INSTALLED; it is the SOTA ANALOG, not a dependency.** It is the
one probed library with a genuine L2 queue-position simulator, which is why `R.23a` names it as the
depth bar for this component. Adopting it means a Rust-extension build and its whole domain model
(its own `Order`, `Instrument`, `Venue`, message bus) alongside `F02`'s, which is the same
control-inversion objection as backtrader at ten times the surface. **The bar to match is its
behaviour, and the input this project has — real recorded L2 — is better than what its own backtest
node usually gets.**

**`vectorbt` 1.1.0 (installed) and `lean` 1.0.228 — REJECTED on category.** `vectorbt` is a
vectorised portfolio-simulation library over arrays: it has no order lifecycle at all, which is the
half `A.108` decision 2 exists to exercise. `lean` is QuantConnect's CLI, a full platform runner.
Recorded rather than silently skipped (`R.11`).

**Conclusion**: build the venue against the recorded depth tape. The reusable piece already exists
inside this repo — `order_book_snapshot_replay_engine` — and the honest work is the queue and fill
semantics on top of it, which no library supplies for this book.

## 6 · Modules (`R.14`)

**AS BUILT, 2026-08-15 — this section was superseded while building; see `A.109`.**

```
src/nse_algo_trader/paper_loop/
  paper_trading_session_runner.py  signal -> sizer -> gate -> intent -> venue -> ledger, one day
  paper_session_signal_source.py   the real regime panel + mean-reversion engine, fed bar by bar
                                   on `availability_time <= decision_instant` and nothing else
src/nse_algo_trader/
  replay_session_clock.py          ALREADY EXISTED (`L0.13`) — used as-is, not rebuilt
src/nse_algo_trader/order_path/
  simulated_order_execution_venue.py  ALREADY EXISTED (`F02`) — the venue the loop actually drives
src/nse_algo_trader/dashboard/
  paper_session_surface_renderer.py   `/paper-session`, folded from the stores the session wrote
scripts/
  verify_paper_session_on_real_data.py  the `R.05` pass
  backfill_five_minute_bars.py          acquires the bars a session needs (`R.16`)
```

Two changes from the plan above, both recorded as decisions in `A.109`:

* **`replayed_session_clock.py` was not built.** `replay_session_clock.ReplaySessionClock` already
  advances only forward, already owns the `L0.11` firewall, and is already covered by the
  `wall_clock_access_detector` AST scan. A second clock without that guard is the one a future
  caller would reach for.
* **`paper_loop/simulated_execution_venue.py` is not in the fill path.** §3 requires the order to
  go THROUGH the order path, and the placer accepts only an `OrderExecutionVenue`; `F02`'s
  protocol-conforming simulated venue is that, walks the same recorded ladder, and models
  rung-by-rung partial fills across polls. The stateless calculator is an orphan carried in
  `BACKLOG` as `M13`, with `queue_ahead_quantity` the one measurement worth porting out of it.

## 7 · Verification plan

- **Unit** — the ladder walk (average price equals the volume-weighted walk by hand), a resting
  order behind existing size, a partial fill, a refusal when the book is absent.
- **Property** — an order can never fill better than the touch; total filled quantity never exceeds
  what traded; the paper ledger's balance after a session equals the fold of its own events.
- **Adversarial** — the axes this file must NOT hold constant (`O.85`): a bar published after the
  decision (the leakage test, §2); a crash mid-session and a restart; a latch tripped mid-session; a
  book that vanishes intra-session; an order larger than the entire visible ladder; the close
  arriving with a part-filled resting order (`A.100`'s corruption case, now end to end).
- **`R.05`** — a full replayed session over the real depth tape and the real bar store, on the full
  universe available for that date, with the resulting paper ledger and session risk state readable
  on the dashboard afterwards.
