# 247 · What actually exists for each of the six segments — measured 2026-08-17

**Why this exists.** `A.130` commits to building all six segment bots in parallel. Before writing the
protocol they share, the question "what does each segment already have?" had to be answered by
querying the real stores, not by reading the plan. Everything below was verified by running code
against live databases on this server.

## The matrix

| Segment | Instrument master | History | Cost model | Contract specs | Depth tape |
|---|---|---|---|---|---|
| cash-intraday (NSE) | **YES** 39,727 | **YES** 11.76 M rows, 1994-11-03→ | **YES** `NSE-CNC` / `NSE-MIS` | YES (live lot/tick) | **YES** 100% of sampled tokens |
| index-options (NFO) | **YES** 5 underlyings, 34,624 CE+PE | **YES** 20.2 M rows | **YES** `NFO-OPT` | YES (live) | **NO** — 0 NFO tokens |
| stock-options (NFO) | **YES** ~209 underlyings | **YES** 158.6 M rows | **YES** `NFO-OPT` | YES (live) | **NO** |
| index-futures (NFO) | **YES** 5 underlyings | **YES** 115 K rows | **YES** `NFO-FUT` | YES (live) | **NO** |
| stock-futures (NFO) | **YES** ~209 underlyings | **YES** 2.9 M rows | **YES** `NFO-FUT` | YES (live) | **NO** |
| commodities/MCX | **YES** 65,496 rows | **NO — zero rows** | PARTIAL (seeded, never exercised) | PARTIAL (no calendar) | **NO** (architecturally blocked) |

**The good news is larger than expected.** Five of six segments have deep history — 158.6 M rows of
stock options alone, back to 2001. The cost engine already prices all eight scopes. The instrument
master already spans NSE/BSE/NFO/BFO/MCX/CDS/NCO at 457,188 rows. Sizing reads lot size live per
instrument with no exchange literal. The paper session runner already takes `segment` and
`segment_margin_fraction` as policy. **The floor is genuinely six-segment-wide already** — this is
the single strongest piece of evidence that `A.130` is buildable rather than aspirational.

## What is genuinely missing — the real blockers

Ranked by how many segments each one blocks.

**B1 · No Greeks or IV-surface engine exists at all.** *(blocks index-options, stock-options)*
There is no Black-Scholes, no delta/gamma/vega/theta, nowhere in `src/`.
`nse_ingest/atm_implied_volatility_adapter.py` ingests NSE's single published daily ATM-IV point per
underlying (1,492 rows) — that is one point, not a strike-level surface. Strike selection and risk
for both option segments depend on this. It is the largest single gap.

**B2 · Depth capture is cash-equity-only in two independent places.** *(blocks all five non-cash)*
- `scripts/record_live_depth_session.py:114-118` asks Kite for `instruments("NSE")` and filters to
  `segment == "NSE" and instrument_type == "EQ"`. The universe selector never requests an NFO or MCX
  token, which is why the tape is 100 % cash despite a generic schema.
- `market_depth/depth_tape_schema.py:33-36` — `_PRICE_DIVISOR_BY_EXCHANGE` covers only
  `NSE/BSE/NFO/BFO`; MCX and CDS are deliberately absent and raise
  `UnsupportedExchangeScaleError` fatally. A **measured** MCX divisor is required before a single MCX
  packet can be stored (`R.03` — it must be measured, not assumed to be 100).
- `dashboard/dashboard_server.py:513` hardcodes `segment = 'NSE'` in the join-refusal query, and must
  be generalised in lockstep or the surface will silently under-report.

**B3 · No physical-settlement / assignment engine for NFO stock F&O.** *(blocks stock-options,
stock-futures near expiry)* `transaction_cost/chargeable_market_segments.py:83-99` models physical
settlement **only** for `MCX-OPT-EXERCISE-PHYSICAL`. SEBI has mandated physical delivery for stock
F&O since 2018, and there is no equivalent scope, margin escalation, or assignment-obligation logic.
Both segments can be built and paper-traded without it; neither can be **armed** without it.

**B4 · MCX has no history and no calendar.** *(blocks commodities)* Zero rows —
`deep_history.duckdb`'s `daily_bar.market` holds only `cash` and `fo`. No MCX bhavcopy adapter
exists. And `pandas_market_calendars` offers `BSE/NSE/XNSE/CBOE_Index_Options/XBSE` — **no MCX** — so
its evening session (to ~23:30 IST) has no calendar source. `R.16` applies: acquire both, or MCX is
an explicit logged blocker. It is the least ready segment by a wide margin.

**B5 · Only one strategy module exists.** *(blocks five of six)*
`strategy/intraday_mean_reversion_engine.py`, 254 lines, cash equity. There is no alpha logic for any
other segment. This is the gap the reconciliation named: 61,808 lines of code, 254 of them strategy.

**B6 · Angel One symbology is NSE-only.** *(degrades five of six)*
`broker_symbology/angel_one_symbology_resolver.py:107` skips every row where `exch_seg != "NSE"`, so
cross-broker reliability and the consolidated feed degrade to cash-only elsewhere. Not a build
blocker; a verification-quality blocker.

## Already segment-agnostic — reusable by all six unchanged

Verified in code, not assumed:

- `kite_instrument_master.py` — exchange/segment-neutral ingest and store.
- `sizing/sizing_inputs_from_real_stores.py:73-91` + `volatility_targeted_position_sizer.py` — lot
  size read live per `(ingested_on, tradingsymbol)`, no exchange literal.
- `transaction_cost/` — `ChargeableSegment` covers all eight scopes; `charge_structure_history.py`
  and `broker_fee_schedules.py` follow it.
- `cost_gate/per_segment_edge_floor.py` — parameterised by `ChargeableSegment`, floors derived per
  segment from measured hurdles.
- `order_path/broker_order_facility_facts.py:64-68` — `OrderProduct` already carries `NRML`, which is
  the F&O and commodity carry product.
- `order_path/order_expression_selector.py` — `tick_size_paise` is caller-supplied.
- `paper_loop/paper_trading_session_runner.py:199-238` — already accepts `segment` and
  `segment_margin_fraction`; the universe is assembled at the call site so `R.09`'s full-universe
  obligation is visible there.
- `market_rules/` — rule scopes seeded for all six target segments plus CDS.

## What this changes about the build order

The readiness split is uneven enough that it must shape the schedule, and it argues for something the
original `A.130` write-up did not say:

- **Four segments are buildable today** with no new data acquisition: cash-intraday, index-futures,
  stock-futures, and — for everything except near-expiry — stock-options. They have masters, deep
  history, cost models and specs.
- **Two option segments are buildable but not armable** until B1 (Greeks/IV surface) exists. They can
  run on the spine and paper-trade against their own null immediately; strike selection will be
  crude until the surface lands.
- **MCX is the outlier.** It has an instrument master and a seeded cost scope and nothing else — no
  history, no calendar, no price scale. It should be built to the same contract as the other five so
  it is ready, and its blockers logged explicitly rather than being allowed to hold the other five
  back.

None of this changes the decision in `A.130`; it changes which blockers get named at sign-off.
