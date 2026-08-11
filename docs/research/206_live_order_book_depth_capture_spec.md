# 206 · Live order-book depth capture — `L0.20` recorder + `L0.21` tape store

Spec for the two plan entries built as one engine, per `R.23(c)` step 1.
Written 2026-08-11, during an open session, from measurements taken on the live feed.

**Pulled forward out of sequence** (tasks `1.20`/`1.21`, twelve items ahead of the cursor at `1.8`).
Justification recorded as an `A.` decision entry: the depth tape is the only artifact in `L0` that
cannot be reconstructed after the fact. Bhavcopy, ISIN records, corporate actions and delisting
histories are all retrievable at 3 a.m.; a 09:15–15:30 depth tape exists only while the session is
open. Every session that passes without a recorder is permanently lost data, so the cost of building
this in sequence is not delay but destruction.

---

## 1 · What was measured before designing

A 75-second probe on the live feed (2026-08-11, 09:36 IST), 120 instruments in `MODE_FULL` —
60 NSE equities and 60 NIFTY options at the nearest expiry. Every number below is measured on this
host against the real Kite socket, not recalled.

| Quantity | Measured |
|---|---|
| Packets received | 4,084 over 75.0 s, all 120 instruments seen |
| Packets/s/instrument | min 0.013 · median 0.120 · max 1.720 |
| Aggregate packets/s | 54.4 |
| Inter-packet gap | p10 0.250 s · median 0.750 s · p90 2.500 s |
| Depth shape (buy levels, sell levels) | `(5,5)` on 4,084 of 4,084 packets |
| Serialized JSON bytes/tick | median 996, max 1,030 |
| `exchange_timestamp` | naive `datetime`, **true epoch** — parsed 04:06:51 vs wall UTC 04:06:52 |
| Zero timestamps | 2 of 12 sampled packets parsed as `1970-01-01` |
| Worst observed staleness | 11 minutes (subscribe-time snapshot on an illiquid instrument) |

Four of these findings are load-bearing and each kills an assumption I would otherwise have shipped:

**The tick rate is ~8× lower than the obvious assumption.** Kite does not push one update per second
per instrument; it pushes on change, throttled, and the median instrument in a mixed basket updates
every 8.3 seconds. Sizing the capture universe off an assumed 1 Hz would have under-admitted by
almost an order of magnitude. The rate is also strongly instrument-dependent (0.013 to 1.72, a 130×
spread), so a single global rate is the wrong model — the controller measures per instrument.

**The timestamp is a true epoch, and this host is UTC.** `kiteconnect` calls
`datetime.fromtimestamp(seconds)` with no timezone, yielding a naive local-time value. Because the
host runs UTC that naive value equals UTC and is correct today. On an IST host the identical code
would produce a value 5h30m off with no error raised. The recorder therefore never consumes the
SDK's parsed datetime; it re-derives the instant from the epoch and attaches `UTC` explicitly.

**Zero timestamps are common enough to matter.** A sixth of the sampled packets carried epoch 0.
Stored as `1970-01-01` these would poison every time-ordered read and every staleness statistic. They
are absent values and are stored as null, with the count surfaced in the session report.

**Packets can arrive badly stale.** The first packet after subscribe is a snapshot whose exchange
timestamp may be minutes old. Any consumer treating receipt order as exchange order would mis-sequence
the open. Staleness is computed and stored per row rather than assumed away.

---

## 2 · What this engine is

A capture and storage subsystem, and the vocabulary is deliberate per `R.23(b)`: this is a
**recorder** and a **store**, not a decision engine, and it is not dressed in engine vocabulary it
cannot support. It nonetheless carries real state, runs a real derived-parameter control loop, and
emits an output that changes behaviour — see §6.

**SOTA analogs:** NautilusTrader's `OrderBook` + Parquet data catalog for the tape and read-back
shape; kdb+/tick's capture-log-then-query split for the write path; ArcticDB's chunked columnar
store for partitioning and retention.

### Modules

| Module | Role |
|---|---|
| `depth_tape_schema.py` | The Arrow schema and row model. One definition, shared by writer and reader. |
| `live_depth_feed_seam.py` | `LiveDepthFeed` Protocol (the `R.J` DI seam) + the real Kite adapter + a deterministic fake. |
| `market_depth_tape_store.py` | `L0.21`. Append-only Parquet tape, atomic writes, read-back and book-at-time reconstruction, coverage metadata, retention. |
| `depth_capture_admission_controller.py` | Derives the admissible instrument set from measured disk, measured bytes/row and measured per-instrument rate. |
| `live_order_book_depth_recorder.py` | `L0.20`. Session lifecycle, shard/subscription management, bounded queue, integrity checks, per-instrument quality accounting. |
| `depth_capture_session_report.py` | The per-session usability verdict a downstream consumer must consult before trusting a day's tape. |

---

## 3 · Storage design (`L0.21`)

**Format: Parquet + zstd, via pyarrow 25.** Verified available on this host (`pa.Codec.is_available("zstd")`
is true). SQLite — used by the bar store at `1.3` — is the wrong tool at this volume: the measured
aggregate is 54.4 rows/s for 120 instruments, so a 1,000-instrument capture is ~450 rows/s and ~10.2M
rows/session, where a row-store's per-row overhead and index maintenance dominate. Depth data is
extremely compressible: within one instrument the five price levels are near-constant across
consecutive packets, so dictionary + delta encoding does most of the work.

**Prices are stored as integer paise, never floats.** `kiteconnect` divides by a divisor to produce a
float; that reintroduces binary-float error into a value that is exactly representable as an integer.
The tape stores `int64` paise and the schema records the scale.

**Partitioning:** `session_date=YYYY-MM-DD / shard=NN / part-NNNN.parquet`. Shards match the recorder's
connection shards so a writer thread owns its files exclusively and no cross-thread file locking is
needed. Row groups are flushed on a size threshold derived from the measured row width, not a constant.

**Atomicity:** every part file is written to `.part-NNNN.parquet.tmp` and `os.replace`d into place, so
a crash or a kill mid-flush can never leave a half-written file a reader might pick up. `os.replace` is
atomic within a filesystem, and the temp file is deliberately created in the destination directory to
guarantee that.

**Bitemporality, consistent with `1.4`:** every row carries both `exchange_time` (when the exchange says
it happened) and `receipt_time` (when this process learned it). These are genuinely different clocks —
the measured 11-minute stale packet is the proof — and the availability-time semantics of the bar store
apply unchanged here. A backtest asking "what did I know at time T" filters on `receipt_time`; an
analysis asking "what was true at time T" filters on `exchange_time`.

### Row schema

| Column | Type | Notes |
|---|---|---|
| `instrument_token` | `uint32` | Kite's token |
| `exchange_time` | `timestamp[us, UTC]` | null when the epoch was 0 |
| `receipt_time` | `timestamp[us, UTC]` | this process's clock, always present |
| `receipt_sequence` | `uint64` | monotonic per session; the only true intra-second ordering |
| `last_price_paise` … `total_sell_quantity` | ints | the quote-mode fields |
| `bid_price_paise_0..4`, `bid_quantity_0..4`, `bid_orders_0..4` | ints | 5 levels |
| `ask_price_paise_0..4`, `ask_quantity_0..4`, `ask_orders_0..4` | ints | 5 levels |
| `staleness_micros` | `int64` | `receipt_time − exchange_time`, null when exchange time is absent |
| `integrity_flags` | `uint16` | bitfield, §5 |

`receipt_sequence` exists because **the feed carries no sequence number and `exchange_time` has
one-second resolution**. At the measured p10 gap of 0.25 s, multiple packets per instrument per
exchange-second are routine, so exchange time alone cannot order them. Receipt sequence is the only
total order the tape can honestly offer, and the schema says so rather than implying the exchange
provided one.

---

## 4 · Admission control (the derived-parameter loop, `R.03`)

Disk is the binding constraint, not the API. Kite permits 3,000 instruments per connection and three
connections per key — 9,000 instruments — while the disk holds far less than 9,000 instruments × 250
sessions. Nothing here may be a hardcoded universe size.

The controller solves, each session:

```
admitted = argmax |S|  subject to  Σ_{i∈S} rate_i × session_seconds × bytes_per_row
                                   ≤ disk_budget_bytes / retention_sessions
```

with every input measured rather than declared:

- `rate_i` — per-instrument packets/s, from that instrument's own history in the tape; instruments
  with no history use the measured median of their liquidity band, and the estimate is revised from
  observed data as the session proceeds.
- `bytes_per_row` — the realized compressed bytes/row of the tape's own recent parts, recomputed at
  each flush. Never a constant.
- `disk_budget_bytes` — a fraction of measured free space, re-read live, so a disk filling for an
  unrelated reason contracts the capture instead of crashing it.
- `retention_sessions` — the operator's stated retention horizon, the one genuine policy input.

Instruments are ranked by a liquidity proxy derived from the tape and the retained bhavcopy, so under
a tight budget the controller keeps the instruments whose microstructure is worth having. When the
budget contracts mid-session it **sheds** the lowest-ranked instruments rather than truncating the
session, so what survives is complete rather than uniformly partial — a half-recorded book on every
instrument is worth much less than a whole book on the instruments that matter.

---

## 5 · Integrity checks (`integrity_flags`)

Every check below is recorded per row and counted per instrument. None of them drop data — a flagged
row is still stored, because a flagged row is evidence and a discarded row is not.

| Bit | Condition | Why it matters |
|---|---|---|
| 0 | `exchange_time` absent (epoch 0) | measured at ~1/6 of sampled packets |
| 1 | crossed book — best bid ≥ best ask | a genuine data defect; silently poisons any spread feature |
| 2 | depth shape ≠ (5,5) | held on all 4,084 measured packets; asserted rather than assumed |
| 3 | non-monotonic exchange time for the instrument | out-of-order delivery |
| 4 | staleness beyond a derived per-instrument threshold | the measured 11-minute case |
| 5 | duplicate of the previous packet in every book field | Kite re-sends unchanged books |
| 6 | price ≤ 0 or quantity < 0 at any level | malformed |
| 7 | received outside the session window per `1.30`'s calendar | pre-open or post-close leakage |

The staleness threshold in bit 4 is derived per instrument from its own observed staleness
distribution, not set to a constant — a stock quoting every 8 seconds and one quoting twice a second
have legitimately different notions of stale.

---

## 6 · The output that changes behaviour

Two outputs, and neither is a panel number.

**The admission decision** is consumed by the recorder itself: it determines which instruments are
subscribed, and it sheds instruments mid-session under budget pressure. This is a control loop acting
on measured state, not a report.

**The session usability verdict** (`depth_capture_session_report`) is the gate for every downstream
consumer of the tape. It answers, per instrument and per session: was this instrument covered for the
whole session, what fraction of the session had gaps exceeding its derived normal, what share of rows
carry each integrity flag, and therefore is this instrument-session **usable / usable-with-caveats /
unusable** for microstructure feature derivation. A consumer that reads the tape without consulting
the verdict is reading data of unknown provenance, which is the failure `L0.12` (replay experience
provenance) exists to prevent.

**Queued consumers, per `R.11`:** `1.22` tick-level order-book reconstruction and the microstructure
feature set are the first real callers. Until one consumes the verdict this ships as `[~]`, not `[x]`,
and the honest statement is that it is an accrual engine whose consumer is queued — which is
precisely why it is worth building today rather than on the day its consumer arrives.

---

## 7 · Verification plan

- **Unit** — schema round-trip, paise conversion exactness, epoch-0 handling, each integrity bit fired
  deliberately.
- **Property** — for any generated packet stream: every packet appears exactly once in the tape;
  `receipt_sequence` is strictly increasing; a read-back book at time T equals the last packet at or
  before T; atomic-write invariant (no reader ever observes a partial file).
- **Adversarial** — crossed books, epoch-0 floods, duplicate storms, out-of-order exchange times,
  reconnect mid-flush, disk exhaustion during a flush, kill -9 mid-session, subscribe rejection,
  a shard whose writer dies while others continue.
- **Rule J hermetic** — the whole recorder driven by the deterministic fake feed behind the seam, with
  no socket, so the session logic is verified independent of market hours.
- **Rule F / R.05 real-data** — the recorder run against the live socket during an open session, with
  the resulting tape read back, the numbers inspected by eye, and the realized bytes/row and rate
  compared against the probe measurements above.
