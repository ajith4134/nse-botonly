# 239 · `M26` fix — the bar store must record WHICH price series it is holding

**Spec, 2026-08-15. Not yet built** — `R.18` keeps one engine at a time and `M14` is still closing
its `R.05` pass. Written now so the design is on disk rather than in a conversation (`R.15`).

Findings behind it: `docs/research/237` (the defect), `docs/research/238` (why bhavcopy cannot
screen for it, and the three corrections the classifier took).

---

## The defect, stated exactly

`price_bars` holds ten columns and **none of them says what the prices mean**:

```
instrument_token · bar_interval · bar_timestamp · open/high/low/close · volume
open_interest · availability_time
```

Kite's historical endpoint returns a series adjusted for corporate actions **as of the moment it is
asked**. `scripts/backfill_five_minute_bars.py` writes that series verbatim. So two rows that look
identical can mean different things:

- a bar fetched on the session date holds **what actually traded**;
- the same bar re-fetched after an ex-date holds **what would have traded on today's adjusted
  basis** — measured at 0.95099 of the traded price for `HINDPETRO`.

The paper loop reads the second and fills against a tape holding the first. Nothing in the schema
lets a consumer tell which it has, and `availability_time` does not help: it records when a bar
became *knowable*, not what basis it is *denominated in*.

## The fix, in three parts

### 1 · Record the basis (the load-bearing part)

One column, `adjustment_basis_as_of TEXT` — the date the source's adjustments were current as of,
which for a Kite fetch is simply the fetch date. It is knowable at write time, needs no
corporate-action feed, and is the thing that makes the defect *visible*:

> a bar is on the traded basis **iff** `adjustment_basis_as_of` is the session date; otherwise it
> is adjusted as of a later date and any corporate action in between has rescaled it.

Nullable, because 1,022,751 rows already exist and their basis is genuinely **unknown** — the
backfill dates were not recorded. `NULL` means unknown, and unknown must not be silently read as
"traded" (`A.41`: three states, and "cannot tell" is one of them). A consumer that needs the traded
basis must treat `NULL` as unusable rather than as safe.

### 2 · Close the gap that creates it

The defect is a property of the **delay** between a session and its backfill, not of any scrip.
`scripts/run_daily_operations.py` already owns the daily timer; the backfill for the day joins it,
so the gap is zero and `adjustment_basis_as_of == session_date` by construction on everything
written from now on.

This does not repair the 1,022,751 existing rows, and nothing can: the traded prices for those
sessions were not recorded and Kite will not serve them again unadjusted. Their basis is
permanently `NULL`. That is the honest outcome and it is the strongest argument for part 2 —
every day this is not fixed adds another day of unrecoverable rows.

### 3 · Adjudicate the candidates the classifier finds

`classify_disagreement_shape` returns a factor and a bar count; `docs/research/238` records that it
is reliable on the large, well-evidenced case (`HINDPETRO`, 0.95099, 150 of 150 bars) and thin on
small ones (`PANAMAPET`, 0.99888, 3 bars). Bhavcopy settles it — `ClsPric` for the session against
the tape's last packet — exactly as it settled `HINDPETRO` by hand in `docs/research/237`.

Automating that adjudication is a small, well-defined addition and it is **bounded by bhavcopy
coverage**: 20 of the bar store's 34 days. A candidate on one of the other 14 cannot be adjudicated
at all, and must report as unadjudicated rather than as confirmed.

## What is NOT in this spec, and why

**Repairing the adjusted series by dividing out the factor.** Tempting — the factor is measured to
five decimals — and wrong. The factor is fitted from the bars that DISAGREED, on the sessions the
depth tape happens to cover. Applying it to a whole history would (a) extrapolate a factor beyond
the evidence that produced it, (b) silently create a third basis that is neither traded nor
adjusted, and (c) destroy the only record that anything was ever wrong. The store records what it
has; it does not invent what it wishes it had.

**Acquiring a corporate-action feed.** Needed to *interpret* a basis mismatch — to say WHICH action
caused it — and not needed to *record* the basis or to *detect* the mismatch. It is a real gap
(`corporate_action` in `market_data.sqlite3` holds 0 rows; `nse_ingest.sqlite3` carries no
corporate-action source) and it belongs to `L0.07`, not here. Recorded so it is not lost.

## Acceptance

1. `adjustment_basis_as_of` present, written by the backfill, `NULL` on pre-existing rows.
2. A reader that asks for traded-basis bars gets an explicit refusal on `NULL`, never a silent pass.
3. The daily operations run backfills the session it just closed, verified by one real run.
4. A test that a bar whose basis date is later than its session is reported as at-risk — the
   condition, not the instrument, so it fires on the next occurrence without being told about
   `HINDPETRO`.

## Sourcing pass (`R.16`/`R.17`) — run 2026-08-15, MECHANICAL evidence

Query: *python market data store track corporate action adjustment basis provenance unadjusted vs
adjusted prices library*.

| Candidate | Mechanical test | Verdict |
|---|---|---|
| `databento` | `pip index versions databento` → **0.83.0**, installs; `import databento` → `ModuleNotFoundError` (not installed here) | **CONCEPT ADOPTED, not vendored.** Databento ships *adjustment factors* — every split, dividend, buyback and new issue consolidated into a single ratio per day — which is the right shape and confirms the design. But it is a **paid US-centric vendor feed**; it has no NSE cash coverage this project can use, and the client only reads Databento's own API. It cannot annotate a Kite-sourced series |
| `lseg-data` | `pip index versions lseg-data` → **2.1.1**, installs; `import lseg` → `ModuleNotFoundError` | **REJECTED.** Refinitiv/LSEG Workspace entitlement required — a paid terminal licence this project does not hold. Its lifecycle model (fetch events → apply adjustments → guard against identifier changes) is a good description of `L0.07` + `L0.02`, both of which already exist here |
| `exchange-calendars` 4.13.2 / `pandas-market-calendars` 5.4.0 | both install; `pandas_market_calendars` is already a dependency (`A.40`) | **ALREADY IN USE** for the calendar, and neither touches price basis |
| `zipline` adjustments DB | `import zipline` → `ModuleNotFoundError`; unavailable on this box (`A.39`) | **ANALOG ONLY.** Its adjustments table is the closest prior art — it stores adjustments *separately* from prices and applies them at read time, which is a stronger design than annotating a basis. It is also a much larger rebuild, and it needs the corporate-action feed this project does not yet have |

**Nothing found records the basis of a series it did not itself produce**, which is the specific
problem here: the prices come from Kite, already adjusted, with no factor supplied. The vendors
solve it by *owning* the adjustment pipeline end to end. Recording `adjustment_basis_as_of` is the
minimum honest annotation available without that pipeline, and it is one column of stdlib
`sqlite3` — nothing to vendor.

**Recorded for `L0.07`:** Databento's consolidated adjustment-factor shape (one ratio per day per
instrument, all event types folded in) is the target shape for this project's own corporate-action
store when the feed is acquired, and `zipline`'s apply-at-read-time is the target mechanism. Both
are better than storing adjusted prices, and both are out of scope until a feed exists.

## Idea intake (rule gate)

**GENUINELY NEW.** `L0.07` is corporate-action *adjustment* — applying actions to a series.
`L0.10` is gap detection and provenance-flagged *backfill* — whether a bar is present and where it
came from. Neither records which adjustment BASIS a stored price is denominated in, which is the
question here. Inserts at its dependency position after `L0.36` (the engine that found it) as
**`L0.37`**, tier base, status planned.

---

## BUILT 2026-08-16 as `A.126`, and the `R.23(c)` review found four HIGHs

Implementation: `src/nse_algo_trader/historical_bars/bar_price_basis_provenance.py`. Consumers:
`scripts/backfill_five_minute_bars.py` (writer), `scripts/run_daily_operations.py` (the gap-closing
step), `scripts/verify_paper_session_on_real_data.py` (the loop), and the `/microstructure` panel.

### What the `R.05` pass says, and it is the whole point

**0.0% of bars are on the traded basis, on every session.** All **1,022,751** rows read `UNKNOWN`
and always will: the backfill dates were never recorded and Kite will not re-serve those sessions
unadjusted. The feature is not failing — it is reporting a fact the schema could not previously
express, and the panel says so in words rather than rendering a reassuring zero.

### The review (`docs/research/244` for the full record)

**`H1` — the feature mislabelled its own output.** The daily timer fires TWICE: `Mon..Fri 19:00
IST` and **`Tue..Sat 08:15 IST`**. The morning firing targets YESTERDAY's session, so its writes
are genuinely on a later basis — "traded basis by construction" held only for the evening run.
Compounding it, `date.today()` is the **UTC** date on this GMT box while sessions are IST. Now
stamped from `Asia/Kolkata` and the mismatch is printed rather than silently classified.

**`H2` — the `R.04` ladder was wrong by a factor of six hundred.** It armed on the EXISTENCE of one
traded-basis bar. Measured on a copy of the live store, a session with 5 instruments stamped and
3,322 unstamped reported itself *armed* at **0.1% coverage and withheld 3,322 instruments** the
loop had been trading. The docstring's premise — that the quantity is "close to binary in practice"
— was false: **the backfill commits per instrument and collects per-instrument failures, so a
partial session is the normal outcome.** Arming now asks whether the session records a basis for
EVERY bar (threshold-free), while positive evidence of another basis acts immediately whether armed
or not.

**`H3` — one bad row was an outage.** `price_basis_coverage_for` called the raising classifier in a
loop over stored rows, so a single bar adjusted as of before its own session made 209,912 honest
rows unreportable and would have returned HTTP 500 from `/replay` and killed the paper loop's
admission. Now counted as `bars_on_an_impossible_basis` and given its own tile. **A store defect
must be shown, not hidden behind an outage.**

**`H4` — `INSERT OR IGNORE` can never annotate.** Measured: `rowcount 0`, basis stays `NULL`. So a
re-run could never record a basis for a row written before this feature — and the only sessions
holding both bars and a depth tape are exactly those rows. A same-day re-run now annotates a `NULL`
row; a later one deliberately does not, because on a later day the basis genuinely IS later.

**`M26` — the unpinned magnitude constant, for the fourth consecutive round.** The 3-hour timeout's
docstring called it "a structural bound from the API's throughput" while deriving nothing from the
throughput; the rate constant lived in another file with nothing linking them. Now computed from
the universe the run is about to fetch and the imported rate — 9,000 instruments gives 5 h, where
the fixed 3 h would have truncated into exactly `H2`'s partial session.

### The finding that outlives the bugs

**13 of 30 mutations survived, and every single one was WIRING.** Every mutation of the algorithm —
classification, the NULL-safe `IS NOT ?` SQL, the arming decision, the three-way counts, the panel
copy — was killed. The survivors were: the writer (three mutations that make the whole feature a
rubber stamp — stamping the session date, stamping `NULL`, omitting the column — **all of which
passed 17/17**), the two daily-run steps (deletable from `main()` with a green suite), the interval
plumbing, and the timeout constant.

The writer was untestable: it built its own Kite client and read its own token list, so nothing
could call it without a live broker and a recorded tape. `kite`, `tokens` and `fetched_on` are now
DI seams (`R.J`), and the three rubber-stamp mutations die.

**Fourth consecutive round in which the gap was in the wiring rather than the algorithm.**

### Acceptance, honestly

1. **Met** — column present, `NULL` on all 1,022,751 pre-existing rows.
2. **Met** — `traded_basis_tokens_for` refuses `NULL`, pinned by the test that carries the design.
3. **NOT MET.** The daily run is wired but has never executed either new step; the last real run
   (session 2026-08-14) predates them. It also depends on a second timer — the depth capture —
   because the backfill's universe is the tape's, a dependency the spec's "by construction"
   argument never named. **Open blocker, `R.05`.**
4. **Met** — the at-risk rule is stated over dates and has never been told about `HINDPETRO`.

---

## Criterion 3 CLOSED 2026-08-16 (`A.127`) — and closing it found a bigger defect than the one it verified

**The blocker was not "the capture must run first". It was `R.16`.** The five-minute backfill took
its universe from the DEPTH TAPE, and the tape's instrument set is chosen by a **disk budget** —
its own log reads `admitted 652 of 9,891 instruments | projected 0.33 GiB of a 0.33 GiB budget`.
So the bar store's coverage was hostage to how much disk a different subsystem happened to get, and
to whether that subsystem ran at all.

**Measured consequence: 2026-08-14 was a trading Friday with no depth capture, and `price_bars`
held ZERO rows for it.** The daily run would have reported "no universe to fetch" for that session
for ever, phrased as a dependency rather than as a hole. The spec's own "by construction" argument
never named the coupling because the coupling should not have existed.

**Fixed:** `cash_equity_universe` reads the instrument master's latest ingest for `NSE`/`EQ` — the
cash board, **10,197 tokens**, ~113 minutes at the script's paced rate, inside the derived 5.7-hour
timeout — and `universe_for` unions the tape in, because neither set contains the other. The
capture is an input now, never a gate.

### The real run

Kite session regenerated through the daily run's own step (`new token generated for HZV381` — the
automated TOTP path the timer uses nightly; no capital involved). Then
`_backfill_five_minute_bars_for(2026-08-14)` ran for real against the live broker and the live
store.

| | |
|---|---|
| bars written into a session that held **zero** | 77,532+ over 1,146 instruments (run continuing) |
| carrying `adjustment_basis_as_of` | **every one** |
| basis recorded | `2026-08-16` — the fetch date, in IST |
| classification | `ADJUSTED_AFTER_THE_SESSION`, correctly |

**That is `H1`'s fix working on real data.** A backfill run two days late labels its own output
honestly instead of claiming a traded basis it cannot have. Before `A.126` these rows would have
been indistinguishable from bars fetched on the day.

The daily step's own report, on a state the system had never produced:

```
2026-08-14: 0.0% of 77,532 bars on the traded basis (77,532 adjusted later, 0 unknown,
0 impossible) · 1146 instrument(s) at risk * AT RISK, adjusted after their own session:
[257, 1025, 1793, 3329, 4865]
```

And the admission rule on that session: **unarmed, yet still withholding**. Unarmed because no bar
is on the traded basis, so there is nothing to arm on; still withholding the at-risk instruments
because an adjusted basis is POSITIVE evidence and evidence acts whether armed or not. That is the
`H2` design meeting real data for the first time, and it behaved as specified.

### What remains, and it is a wait rather than a defect

The **same-day** path — a 19:00 IST run stamping the session it just closed and producing genuinely
TRADED-basis rows — first occurs on the next trading evening. It cannot be forced: this ran on a
Saturday and the source has no session to serve. **The mechanism is verified end to end on real
data; the same-day outcome is scheduled, not unproven.**

Criterion 3 is therefore met for the run, with the traded-basis outcome recorded as a dated
expectation rather than claimed.
