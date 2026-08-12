# Research/215 — `L0.31` spec: the point-in-time market-rule store

**R.23(c) step 1.** Spec before signatures, signatures before tests, tests before implementation.
Source of the domain facts: `research/61` §2 (the acquisition pass that established there is no
machine-readable point-in-time rules dataset anywhere). Companion to `204` (point-in-time universe),
`nse_trading_session_calendar` (which dates were open — this answers what the RULES were on them), and
`164` (the transaction-cost engine, which is this store's first real consumer).

---

## 0. The problem, stated so it cannot be shrunk

A replay of 2019 that applies 2026's rules is not a backtest, it is fiction. The specific ways it lies:

| Applying today's rule to an old date | What it silently does |
|---|---|
| STT on options at 0.1% (Oct-2024 rate) | overstates 2013–2023 option costs by 2× |
| NIFTY weekly expiry on Tuesday (Sep-2025 rule) | puts expiry on days that were ordinary sessions for nine years |
| Exchange charges as a flat bps rate | wrong for every date before 1-Oct-2024, when the schedule was a turnover SLAB (`research/61` §2.5) |
| Today's lot size | mis-sizes every historical position |
| Today's tick size | fabricates spreads that could not exist |

`research/61`'s verdict on sourcing is the constraint that shapes this build: **no aggregator exists, for
any of it.** Every parameter is compiled from individually-dated NSE/SEBI circular PDFs. There is no API,
no CSV with a valid-from column, no vendor. So this store is not an ingest adapter — it is the
**structure that dated facts get compiled into**, and the thing that refuses to answer when they have
not been.

## 1. The difference test (`R.23(a)`)

| Requirement | How this meets it |
|---|---|
| **Inputs** | Dated rule facts, each carrying its circular reference, source date and evidence grade — seeded from `research/61` §2.1–2.7 and extended as circulars are read |
| **Solver** | Interval reconciliation: overlapping, out-of-order, contradictory records resolved into a non-overlapping timeline per family+scope, with conflicts SURFACED rather than picked |
| **State carried** | The compiled timeline per family, and the transaction-time axis — the store answers "what do we know now" and "what did we believe on date B", which are different questions |
| **Verifiable output** | A resolved rule value with provenance and grade, or a named refusal. A cost engine's answer changes with it |
| **SOTA analog** | SQL:2011 system-versioned + application-time period tables, and Zipline's adjustments database — both keep a fact's validity interval separate from when the fact was learned |

## 2. The rule families — the full space, not a sample (`R.12`)

Taken from `research/61` §2.8, which is itself the enumeration:

1. **Expiry cycle** — per index, weekday and weekly/monthly, 2000 → 2016 → 2019 → 2021 → 2023 → 2024 → 2025
2. **Lot size** (per underlying) and **2b. minimum contract value** (2000 → 2015 → 2024)
3. **Tick size** (2003+; pre-2003 is a stated blocker)
4. **Market-wide circuit breaker** (2001/2013/2015), **4b. per-stock price bands**, **4c. dynamic price
   bands for F&O stocks** (Oct-2002 onward)
5. **STT** (2004/2013/2023/2024/2026), **5b. stamp duty** (uniform from 1-Jul-2020; before that,
   state-by-state and unaggregated anywhere), **5c. exchange transaction charges** (turnover SLABS until
   1-Oct-2024, flat after)
6. **Pre-open auction** (18-Oct-2010) and **6b. session hours**
7. **Margin regime** — SPAN (1999), VaR/ELM (2001), peak margin phases (Dec-2020), penalty framework
   (2011)

Sixteen families in total. The store carries all of them as first-class kinds; a family with no facts
loaded yet reports as *uncovered*, which is a different state from *no rule*.

## 2b. What this project already holds — inventoried before designing, not assumed

An audit of the three live databases and the four ingest adapters (2026-08-12) settles what must be
built versus what can be seeded:

**There is no existing rate constant to absorb.** `rg` across `src/` and `scripts/` for `stt`,
`stamp_duty`, `gst`, `brokerage`, `transaction_charge` returns **nothing**. `tick_size` and `lot_size`
appear only in `kite_instrument_master.py`, always parsed live from Kite's dump, never as a literal. So
this store is not paying off existing debt — it is the first place a rate will live, which is the right
time to make citation mandatory.

**The adapters carry rule OUTCOMES, not rule PARAMETERS.** `fo_ban_list_adapter` gives the banned
symbols but not the MWPL percentage that banned them; `mwpl_position_limits_adapter` gives per-symbol
limit numbers but not the rule that computes them; `circuit_band_surveillance_adapter` gives ASM/GSM
surveillance stages but not the standard 2/5/10/20% band table. Every one of them is correctly dated
from the file's own content — that discipline is reusable, the data is not.

**One genuine seed source exists, and it is observational rather than documentary.**
`market_data.instrument_master` holds 227,535 rows with real `tick_size` and `lot_size`, dated by
`ingested_on`. That is a daily snapshot, not a point-in-time table — it cannot answer for a date before
capture began. But for dates it *does* cover it is *stronger* evidence than a circular: it is what the
exchange actually published that day. So the store admits a distinct evidence grade for
**observed** facts, separate from the documentary A/B/C ladder, and a rule change can be *detected* by
diffing consecutive snapshots rather than waiting for someone to read a PDF.

**Neither existing bitemporal store is a home.** `BitemporalIngestStore` is a generic raw-row ledger
keyed by `(source_name, natural_key, effective_date, content_hash)` — no validity interval;
`BitemporalBarStore` is bar-shaped. A rule needs `effective_from`/`effective_to`, which neither models.
The new store sits beside them and borrows `BitemporalIngestStore`'s fetch-audit pattern for provenance.

**One blocker inherited from `research/61`, restated because it bounds the feature:** per-stock 2/5/10/20%
circuit-band classification is decided ad hoc daily by NSE Surveillance and **appears in no circular at
all** — it exists only in daily security-master files whose historical archive is unconfirmed. That
family will be permanently uncovered before the date this project started capturing, and the store must
say so rather than extrapolate backwards from today's band.

## 3. The three states a query can return, and why the third exists

1. **Resolved** — a fact covers the date. Returns value + circular reference + grade.
2. **Conflicted** — two facts cover the date with different values. Returns the winner under the stated
   policy AND the loser, flagged. Never a silent pick.
3. **Uncovered** — the date precedes the earliest known fact for that family, or falls in a hole between
   known intervals. Raises `RuleCoverageError`.

**The third state is the point of the whole module.** A store that returns today's rule for 2005 because
that is the only record it holds would silently corrupt every historical study, and the corruption is
invisible: the number looks right. `research/61` documents real, permanent gaps — pre-2003 tick sizes,
pre-2020 stamp duty, the 2004–2013 options-STT path, the whole pre-Oct-2024 exchange-charge slab
schedule — so this state is not hypothetical. It is most of the history.

## 4. Conflict-resolution policy, stated once

Facts arrive from sources of different quality (`research/61` grades A/B/C) and get corrected as
circulars are read. When two records cover the same instant with different values:

1. Higher evidence grade wins (a read circular beats a broker blog).
2. Same grade → later **recorded_at** wins (the more recent compilation supersedes).
3. Still tied → **unresolved**: the query returns conflicted with both records and no winner. A caller
   that cannot tolerate that must say so; the store will not invent a preference.

## 5. Bitemporality, and what it is for here

Two independent axes, exactly as `bitemporal_bar_store` keeps event time apart from availability time:

- **Effective time** — when the rule was in force at the exchange.
- **Recorded time** — when this project learned it.

`resolve(family, as_of=D, known_as_of=B)` answers *"what would we have believed on B about the rule in
force on D"*. That is what makes a replay reproducible after the fact table is corrected: a study run
last month can be re-run against the beliefs it actually had, and the difference attributed to the
correction rather than to the strategy.

## 6. `R.03` versus regulatory facts — the one place constants are legitimate

`R.03` forbids hardcoded values; `R.23(e)` states the exception: *"a constant in an engine is a defect
unless it is a physical or regulatory fact, sourced in a comment."* An STT rate of 0.0625% effective
1-Apr-2023 is a regulatory fact. It is not derivable, not calibratable, and not a tuning knob — it is
what the Finance Act says.

So the seeded fact table is legitimate **only** under conditions the tests enforce: every record carries
a source reference, a source date and a grade, and no record may be added without them. A rate without a
citation is exactly the hardcoded constant `R.03` bans. The distinction is the citation, not the type.

## 6b. OSS sourcing pass — run, not recalled (`R.16`, `R.17`)

Decomposed into four parts and searched PyPI + GitHub for each; every verdict rests on a
`pip install --dry-run` result, a fetched source file, a real signature or a push date.

**PART A — bitemporal / effective-dated storage.** `bitemporal` 1.0 targets **Python 2.5**
and is SVN-hosted — dead. `temporal-sqlalchemy` last released 2018. `scd` 1.2.3 is nine
years stale and, despite the name, is not an SCD library at all. `scd2` 1.0.0 installs
cleanly but its 150-line source stamps validity with `datetime.now()` only, so it cannot
backfill a historical effective date or represent belief time — wrong shape, not merely
thin. `sqlalchemy-continuum` is very active (644★, commit 2026-07-30) but versions ORM ROWS
— an audit trail of writes, not application-time intervals — and would drag the whole ORM
in for a job that is a few lines. `pygrametl` 2.9 (commit 2026-07-27, zero required deps) is
the only serious contender: `SlowlyChangingDimension` + `lookupasof(row, when)` is genuine
as-of semantics against a stdlib `sqlite3` connection. **Rejected anyway**, mechanically:
it models ONE temporal axis, so belief time and conflict reporting — the two things that
are actually hard here — would still be hand-built, on top of its schema conventions and DB
lifecycle. **No Python library models both axes.**

**PART B — interval algebra.** `portion` 2.6.2 (523★, commit 2026-06-29, already installed
transitively, tests in repo) is the find. `IntervalDict.combine(other, how=fn)` reconciles
`datetime.date`-keyed half-open intervals and calls `fn` **only on the overlapping slice**,
passing disjoint parts through — verified live on a synthetic Thursday/Friday expiry
conflict. `intervaltree` 3.2.1 solves the same problem at a lower level (`merge_overlaps`
with a reducer) and is redundant here; this store holds low hundreds of facts, so an
augmented BST buys nothing.

**PART C — rule engines.** `business-rules` (991★) was installed and grepped: `effective`
and `temporal` return **zero hits** in its source — a condition/action DSL with no validity
concept, so 100% of the difficulty would remain. `durable_rules` is a Redis-backed CEP
engine; wrong architecture entirely.

**PART D — existing Indian charge/rule datasets.** Four GitHub calculators found
(`tahseenjamal/zerodha_brokerage_calculator` and three siblings); **all JavaScript**, none
packaged, none with a rate table as data. Nothing importable exists.

**Decision, and it is a partial adoption rather than a clean one.** `portion` genuinely does
the boundary-splitting half. It is *not* adopted as the solver, because it carries no notion
of evidence grade, scope specificity or belief time, and those decide the winner — a
last-writer-wins `IntervalDict` would silently prefer whichever fact was compiled most
recently, which is exactly the arbitrary preference §4 forbids. So it enters where a second
implementation is most valuable: as a **differential oracle in a property test**, asserting
that this store's timeline agrees with `portion`'s reconciliation on randomly generated
interval sets where the two policies coincide. Hand-rolled interval splitting is precisely
the code that is subtly wrong at the edges and passes its own tests.

## 7. Signatures (`R.23(c)` step 2)

```python
class RuleFamily(StrEnum)                    # the sixteen of §2
class EvidenceGrade(StrEnum)                 # PRIMARY / SECONDARY / UNVERIFIED
@dataclass(frozen=True) class RuleScope      # segment / index / symbol / everything
@dataclass(frozen=True) class MarketRuleRecord
@dataclass(frozen=True) class RuleResolution # value, record, conflict, grade
@dataclass(frozen=True) class FamilyCoverage # earliest, latest, holes, grade mix

class RuleCoverageError(MarketRuleError)     # the date is not covered
class RuleCitationError(MarketRuleError)     # a record arrived without provenance

class PointInTimeMarketRuleStore:
    def add(self, record) -> None
    def resolve(self, family, as_of, *, scope=..., known_as_of=None) -> RuleResolution
    def timeline(self, family, *, scope=...) -> tuple[MarketRuleRecord, ...]
    def conflicts(self) -> tuple[RuleConflict, ...]
    def coverage(self) -> Mapping[RuleFamily, FamilyCoverage]
```

## 8. Acceptance criteria — checked, not claimed

1. **Unit** — resolution at an interval's first and last day and one day either side; scope precedence
   (a symbol-specific rule beats a segment-wide one); grade precedence; recorded-time filtering.
2. **Property** — the compiled timeline never overlaps and never reorders; resolving any date inside a
   record's interval returns that record; adding facts in any order yields the same timeline.
3. **Adversarial** — a zero-length interval, an interval that ends before it starts, a record with no
   citation, two records tied on grade and recorded time, a hole between intervals, a query before the
   first fact, and a query with `known_as_of` earlier than every record.
4. **Real data (`R.05`)** — resolve the STT and expiry-weekday timelines against the dated facts in
   `research/61`, and cross-check the expiry-weekday answers against the **real trading calendar and the
   real bar store**: on dates the store says were NIFTY expiries, the retained option data must show the
   expiry behaviour. That is a check against the world, not against the table that was typed in.
5. **Gate** — ruff + mypy clean, whole suite green.
6. **`R.06`** — the first consumer is `L1.01`, the transaction-cost engine (spec `research/164`), which
   cannot be correct without it; the coverage map gets a dashboard surface per `R.08`.

## 9. Out of scope, recorded so it is not mistaken for an omission

- **No circular scraping.** `research/61` establishes the PDFs are individually fetchable but unindexed;
  automating that is its own slice, and this store is what it would write into.
- **No margin calculation.** The margin *regime* dates are carried; computing SPAN is `L7`.
- **No news/announcement history** — `research/61` §4, a separate acquisition target with a hard
  pre-2010 blocker.

## 10. As built — the observational source (added 2026-08-12, `A.81`)

§2 argued the observed grade should exist and §9 left populating it out of the first slice. It was built
the same day, because a grade with no producer is a promise rather than a design.

**`InstrumentMasterRuleObserver`** (`src/nse_algo_trader/market_rules/instrument_master_rule_observer.py`)
satisfies an `ObservationalRuleSource` protocol the store queries at resolve time:

```python
def families(self) -> frozenset[RuleFamily]: ...
def observation_window(self) -> tuple[date, date] | None: ...
def records_for(self, family: RuleFamily, scope: RuleScope) -> tuple[MarketRuleRecord, ...]: ...
```

- **Lazy by arithmetic, not by preference.** Materialising a record per symbol per family over 227,535
  rows / 106,436 symbols would put a quarter-million objects in a list every later query scans linearly.
  Asked per resolved symbol instead: **measured 0.02s** against the full table.
- **Change detection is run-compression.** Consecutive equal daily observations collapse to one interval;
  a change closes the old run at the day the NEW value first appeared (the boundary that cannot be wrong
  in the direction that puts a stale rule on a real trading day) and opens a new one. The final run stays
  open-ended, or every query for today would refuse one day after the last ingest.
- **The key is `(tradingsymbol, segment)`, and the real-data pass is what proved it.** A symbol-only
  query sees RELIANCE twice — 0.10 on NSE, 0.05 on BSE — and reads one day as a rule that changed and
  changed back, which the interval validation rejected outright. Two values for one day is an ambiguous
  question, so the source declines it and the caller pins the segment. Full-universe sweep: **1,668
  ambiguous symbol-days symbol-only, 0 segment-pinned** (`O.58`).
- **Honest boundary, carried in the data.** `effective_from` is the first day OBSERVED (2026-08-11 on
  this host), never the day the rule began. `observation_window()` reports it so `/rules` can show it,
  and pre-capture dates still refuse.

**Coverage after this slice: 14 of 16.** `tick_size` and `lot_size` are *observed only*; `session_hours`
was seeded from `research/61` §2.6 at Grade B (absence-of-change, not a circular). The two that remain —
`per_stock_price_band`, `dynamic_price_band` — are the §2 blocker verbatim: no circular exists, so
forward capture is the only route and more reading will not close them.
