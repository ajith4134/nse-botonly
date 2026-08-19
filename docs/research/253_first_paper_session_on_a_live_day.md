# 253 · Paper trading run against the LIVE session — 2026-08-17, market still open

**First paper session this project has run on a day that had not finished yet.** Every prior run
replayed a closed session. This one executed at 12:49 IST against a market that closes at 15:30,
over a depth tape still being written by the live capture.

## What was run

```
python scripts/verify_paper_session_on_real_data.py 2026-08-17 --instrument-limit 200
```

Real bar store, real price-basis provenance, real regime panel, real `IntradayMeanReversionEngine`,
real order path with its rate limiter and control latch, real recorded depth tape. No fixtures.

## The result, verbatim

| | |
|---|---|
| Decision steps | **76** (40 with a recorded book) |
| Capture window | **09:40–12:55** — entries considered only inside it (`A.116`) |
| Instruments | 47 priceable of the 200-instrument limit; 35 had recorded depth, 12 never recorded |
| Orders placed | **10** |
| Gross | **−₹5,731.99** |
| Costs | **₹699.74** |
| **Net** | **−₹6,431.73** |
| Balance | ₹10,00,000.00 → **₹9,93,568.27** (−0.64%) |
| Rate gate | 21 granted, 0 refused |
| Ledger fold agrees with report | **True** |

Decision outcomes: **1,820 abstained · 33 refused by gate · 10 placed · 1 no evidenced horizon.**

All ten positions were **SELL**, all ten exited on horizon expiry, and **nothing survived the
close** (`R.01`). Six of the ten moved against the short.

## Against the pass criteria the script states before it runs

The script fixes its own bar in its docstring so it cannot move afterwards. All five held:

1. **No unhandled exception** — exit 0, no traceback.
2. **Every decision carries a reason** — all 1,864 outcomes are named, abstentions included.
3. **No fill better than the touch, no quantity beyond recorded depth** — the two ways a paper
   record flatters itself; neither triggered.
4. **Nothing survives the close** — all ten exited.
5. **Closing balance equals the fold of the ledger's own events** — `agrees with report: True`.

**So the machinery passed and the strategy lost money.** Those are different results and the report
keeps them apart, which is `R.13` working: correctness of execution is not evidence of correctness
of allocation.

## Two things this exposed

**A second instance of a bug I had already fixed.** `scripts/backfill_five_minute_bars.py:94` handed
pyarrow the session directory, so the live recorder's `<hhmmss>_capture_liveness.json` sidecar raised
`Parquet magic bytes not found in footer`. This is the *identical* defect fixed in
`MarketDepthTapeReader` earlier the same day — and this copy sits in **scheduled** code: the
daily-operations five-minute backfill step would have failed tonight, for every session a capture had
run, which is every session it exists to serve. Fixed, and the whole class was swept this time: the
third pyarrow call site (`depth_tape_delay_sampler.py:88`) already enumerated paths and was safe.

**Paper trading has never accrued a track record.** The verifier writes to a scratch state directory
and **deletes the ledger at the start of every run** — by design, since it is a verification harness.
The production `paper_capital_ledger.sqlite3` holds 13 events total. There is no daily paper session
in `run_daily_operations.py`; the twelve steps it runs cover ingest, stores and reconciliation, and
none of them trade. So no instrument has a paper record, which means `R.04`'s maturity ladder has
nothing to promote and `R.22`'s graduation has nothing to graduate. Recorded as **B15**.

## Honest limits of this run

- **`--instrument-limit 200`** — not the full universe (`R.09`). The limit was to get an answer
  inside market hours; the unlimited run is what counts for a verification claim.
- **The bar/tape join is UNVERIFIED for 2026-08-17** — the script said so and proceeded, which is
  the correct behaviour but leaves the `R.05` claim partial for this date.
- **Today's five-minute bars were still backfilling while this ran** (105 of 10,197 instruments at
  the time), so the priceable universe was small and skewed toward whatever had landed.
- **One partial session is not evidence about the strategy.** Ten trades on a truncated day says
  nothing about edge; it says the pipeline runs end to end on a live day, which is what was being
  tested.
