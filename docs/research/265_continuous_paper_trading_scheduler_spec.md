# 265 · `L10.01` — the continuous paper-trading scheduler. Spec, before code (`R.23c`)

**Idea-intake verdict: ① ALREADY EXISTS** — plan entry **`L10.01`** ("24/7 continuous paper-trading
loop — the system never stops, market open or closed"), todo **4.9**. Nothing added to the plan.
Task `4.9` already states what is missing, and it is exactly right:

> *unblocked 2026-08-15: `4.10` built the session this loop would run repeatedly. What is missing is
> only the scheduler and the multi-day state that carries between sessions, not the session itself.*

This is also `B33`, and it is the unfixed half of the operator's 2026-08-18 report — *"the intraday
cash is not switching to live market data when market is open ... the prices are stuck"*. The other
half was the daily-run timeout, fixed and proven as `A.143`.

---

## 1 · What "live" can honestly mean today, measured

Checked at **13:06:41 IST on 2026-08-18, market open**:

| thing | state |
|---|---|
| `nse-depth-capture` | **active** |
| parquet parts written today | **410** |
| ticks in today's tape | **7,135,786** over **1,845** instrument tokens |
| latest `exchange_time` | **13:06:30 IST — eleven seconds old** |

So live market data IS flowing into this box continuously. A loop that tails that tape is consuming
real live NSE depth, not a replay. **That is what makes this buildable today** and it is why the
answer to "why are the prices stuck" is a scheduler rather than a broker integration: the data was
already arriving, and nothing was reading it between daily runs.

**What this does NOT do.** It does not place orders through a broker. Fills come from
`SimulatedOrderExecutionVenue` against the depth tape — which for a live session is the tape being
written *now*, so the fill is against the book that actually existed. Live ORDER placement remains
`A.108`'s recorded open cost and is not in this slice.

## 2 · The three things that are actually missing

1. **Carried state across iterations.** Every consumer so far rebuilds its bots per invocation, so
   `/bots` reports 0.0% universe readiness on every render: one page load is one observation and a
   dispersion needs three. The scheduler owns the bots for the life of the process and observes on
   every tick, which is what makes a rolling statistic possible at all.
2. **A cadence.** Derived, not chosen — see §4.
3. **A liveness record the dashboard READS rather than re-derives.** `R.08`: a loop whose only
   evidence is that a process is running is a loop nobody can audit. Each iteration appends what it
   saw, what it decided and why, so a stalled loop is visibly stalled rather than merely quiet.

## 3 · The state machine, and why it has four states rather than two

```
BEFORE_OPEN --(session opens)--> TRADING --(square-off window)--> SQUARING_OFF --> AFTER_CLOSE
     ^                                                                                  |
     +---------------------------(next session date)-----------------------------------+
```

- **`BEFORE_OPEN`** — observe nothing, decide nothing. A bot fed pre-open indicative prices learns a
  distribution the session never trades at.
- **`TRADING`** — observe every tick batch, decide on the cadence, size against the margin estimator
  (`L6.30`), record.
- **`SQUARING_OFF`** — a separate state and not a special case of `TRADING`, because `R.01` makes
  square-off the failure mode: it must run even when the tape has gone quiet, even when no bot
  proposes anything, and even when the previous state errored. A state that only exists as a branch
  inside another state is a state that gets skipped.
- **`AFTER_CLOSE`** — accrue the record, roll to the next session. This is where the "24/7" of
  `L10.01` lives: the loop does not exit at 15:30, it changes what it is doing.

## 4 · The cadence is DERIVED (`R.03`)

The loop must not decide faster than its own data changes, and must not decide slower than the
strategy's own horizon. Both bounds are measurable rather than chosen:

- **lower bound — the tape's own write cadence.** Measured today: 410 parts over the session so far,
  and the capture flushes on a fixed shard size. Deciding between two flushes re-reads the same
  book and produces the same answer, which inflates the decision count without adding information.
- **upper bound — the strategy's bar interval.** The cash bot bands on **five-minute** bars
  (`REVERSION_WINDOW_IN_FIVE_MINUTE_BARS`), so a decision cadence slower than five minutes drops
  bars the engine was fitted on.

The cadence is therefore the bar interval, and the loop states it on every iteration rather than
assuming the reader knows. A constant here would be the exact magic number `R.03` forbids; the bar
interval is a property of the fitted strategy and is imported from it.

## 5 · Signatures

```python
class SessionPhase(StrEnum):
    BEFORE_OPEN / TRADING / SQUARING_OFF / AFTER_CLOSE

@dataclass(frozen=True, slots=True)
class SchedulerIteration:
    observed_at: datetime
    phase: SessionPhase
    session_date: date
    instruments_observed: int
    proposals: int
    orders_placed: int
    tape_lag_seconds: float | None
    note: str

class ContinuousPaperTradingScheduler:
    def __init__(self, *, bots, calendar, clock, tape_reader, liveness_store, cadence_seconds)
    def phase_at(self, instant: datetime) -> SessionPhase
    def step(self, instant: datetime) -> SchedulerIteration      # ONE iteration, no sleeping
    def run_until(self, stop_at: datetime) -> tuple[SchedulerIteration, ...]
```

**`step` takes the instant and never reads the wall clock.** That is what makes the whole loop
replayable and testable: a market-closed test drives a whole session through `step` in
milliseconds, and production passes `datetime.now`. `run_until` is the only thing that sleeps.

## 6 · Acceptance criteria

1. **Phases are correct at the real NSE boundaries** — before 09:15 `BEFORE_OPEN`, at 09:16
   `TRADING`, inside the square-off window `SQUARING_OFF`, after 15:30 `AFTER_CLOSE`, and on a
   holiday `AFTER_CLOSE` all day. Driven off `NseTradingSessionCalendar`, never a weekday check.
2. **State genuinely carries** — after N iterations a bot's `instruments_mature` is greater than
   after one, which is precisely what `/bots` cannot show today.
3. **Tape lag is measured and reported**, so a capture that has stopped is visible as a growing lag
   rather than as a quiet loop.
4. **Square-off runs from its own state**, verified by driving a session where the tape goes silent
   before the close: the loop must still reach `SQUARING_OFF`.
5. **`R.05` LIVE** — run against the real tape during the open market on 2026-08-18 and report
   instruments observed, proposals, and lag against the eleven seconds measured above.
6. **It never raises.** A scheduler that dies on one bad iteration is worse than no scheduler; every
   iteration records its own failure and the loop continues.

## 7 · Sourcing (`R.17`)

**Probed on PyPI, not recalled** (the `O.131` failure was writing exactly this section from
memory):

```
APScheduler              3.11.3      schedule                 1.2.2
rocketry                 2.5.1       pytimeparse              1.1.8
exchange-calendars       4.13.2      pandas-market-calendars  5.4.0  <- ALREADY a dependency
```

**The three scheduler libraries are real and are still NOT vendored**, and the reason is mechanical
rather than preference: what this slice needs is not periodic invocation. Cron and systemd already
provide that and this project already depends on both for the daily run and the depth capture. What
is missing is a state machine over the EXCHANGE calendar **with state carried between ticks** —
which is the whole of `4.9`'s "multi-day state that carries between sessions", and which none of
the three provides. Adding one would supply the timer, the part already solved twice.

**`pandas_market_calendars` 5.4.0 is already installed, and it was used as an independent CHECK
rather than as a replacement.** Run against the real dates:

```
pandas_market_calendars NSE : 2026-08-10 .. 2026-08-20 -> 10,11,12,13,14,17,18,19,20
this project's calendar      : 2026-08-10 .. 2026-08-20 -> 10,11,12,13,14,17,18,19,20
market_open 03:45 UTC = 09:15 IST      market_close 10:00 UTC = 15:30 IST
```

**Nine sessions, exact agreement, and the library independently confirms the 09:15/15:30 boundaries
this project carries as constants.** That is worth more than vendoring it: `NseTradingSessionCalendar`
stays the single authority (every other module already agrees with it, and `A.121`-era work sourced
its holiday list), and it now has a second opinion behind it rather than being self-asserted. The
cross-check is kept as a test so a future divergence surfaces instead of being discovered by a
session that traded on a holiday.

## 8 · Open after this lands

- Live ORDER placement through a broker (`A.108`'s recorded cost) — fills stay simulated against
  the real tape.
- The five daily-cadence bots still decide once per session; this scheduler runs the cash bot
  intraday and steps the others at the close, until `A.142`'s F&O capture fills.
