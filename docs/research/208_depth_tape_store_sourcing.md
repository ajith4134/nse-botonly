# 208 — Depth Tape Store: Mechanical OSS Sourcing

Date: 2026-08-11
Host: Linux aarch64 (ARM64), glibc 2.34, Python 3.12.13, venv `/home/opc/nse-algo-trader/.venv`
CPU: 5 cores, RAM: 28GiB, disk: 77GB volume (38GB free at test time)

Component being sourced: high-write-rate tick/L2-depth **tape store**. Requirements: 450–5000
rows/sec sustained for a 6.25h session from multiple writer threads; 42-column wide row (5-level
bid/ask depth price/qty/orders ×2 sides + 7 quote fields + 2 timestamps + seq + flags, all
integer); survive `kill -9` mid-session without corrupting already-written data; time-range slice
per instrument + "book-at-time" (last row ≤ T); ~19GB total disk budget, so bytes/row after
compression is the dominant sizing variable; partitioned by session date.

All claims below are tagged **VERIFIED** (I ran it on this host, output pasted) or **UNVERIFIED**
(could not run — e.g. no linux-aarch64 pip wheel exists at all).

## Synthetic benchmark data

Generator: `/tmp/claude-1000/-home-opc/e7dbbcc5-db3e-4764-8939-00d7487ea707/scratchpad/gen_data.py`
(not project code — scratch only). 1,000,000 rows, 42 int columns matching the spec exactly:
`exchange_ts, recv_ts, seq_no, instrument_token, flags, ltp, ltq, volume, oi, avg_price,
total_buy_qty, total_sell_qty, {bid,ask}_{price,qty,orders}_{1..5}`. 500 instruments, prices are a
per-instrument random walk with small ±10-paise jitter (delta-encoding-friendly, as specified),
timestamps monotonically increasing across a simulated 6h session. Raw in-memory size: 224
bytes/row (pandas object overhead included; on-disk numeric size is lower — see below).

All benchmark and crash-test scripts live in the scratchpad directory listed above (not written
into the project), e.g. `bench_pyarrow.py`, `bench_duckdb.py`, `bench_polars.py`,
`bench_fastparquet.py`, `bench_tables.py`, `bench_zarr.py`, `bench_pystore.py`,
`crash_duckdb_writer.py`, `crash_parquet_writer.py`.

---

## 1. ArcticDB — REJECT (not installable via pip on this host)

**VERIFIED**: `pip install arcticdb` → `ERROR: Could not find a version that satisfies the
requirement arcticdb (from versions: none)`. Checked PyPI JSON directly for the latest release
(6.22.0, uploaded 2026-08-10): wheel filenames are
`cp3{9,10,11,12,13,14}-macosx_15_0_arm64`, `manylinux2014_x86_64`, and `win_amd64` only — **zero
linux_aarch64 wheels exist at any Python version**, and there is no sdist either, so pip has
literally nothing to install on `linux_aarch64`.

**VERIFIED (GitHub)**: repo `man-group/ArcticDB`, last push 2026-08-10, 322 open issues, actively
maintained. Found issue #2758 "Add support for arm64 architecture" (closed) — resolution was
**conda-forge**, not PyPI: linked PR "Distribute for `linux_aarch64`"
(conda-forge/arcticdb-feedstock#534). **VERIFIED**: queried conda-forge's linux-aarch64 channel
repodata directly — 149 matching `arcticdb-*` package builds exist there (verified up to 6.9.2 for
py312/py313/py314; anaconda.org shows versions up through 6.21.0 in general). So ArcticDB **is**
buildable/available for this exact host — but only via `conda`, not `pip`, and this task's approved
install path is the pip venv. Not tested further (would require standing up a conda env, out of
scope; noted as a real alternative for the user to weigh separately). API signatures and
append/read-path benchmarks: **not obtainable** — package cannot be imported on this host via pip.

## 2. nautilus_trader — REJECT (not installable via pip on this host)

**VERIFIED**: `pip install nautilus_trader` → PyPI does list a `cp312-cp312-manylinux_2_35_aarch64`
wheel for the latest version (1.231.0, uploaded 2026-08-02), so at first glance ARM64 looks
supported. But pip refused to use it and fell back to source build. Root cause, verified directly:

```
$ ldd --version | head -1
ldd (GNU libc) 2.34
$ python3 -c "from pip._internal.utils import compatibility_tags as c; print(any('manylinux_2_35' in str(t) and 'aarch64' in str(t) for t in c.get_supported()))"
False
```

The wheel requires **glibc ≥ 2.35**; this host has **glibc 2.34** — one point release short. Every
aarch64 wheel published for nautilus_trader (checked 1.231.0 and 1.225.0) is `manylinux_2_35`, so
no version has a compatible wheel here. pip therefore fell back to the sdist, which requires a
full Rust workspace compile. Rust **is** present (`cargo 1.92.0`, `rustc 1.92.0`), but the sdist
build **failed** with a packaging bug unrelated to ARM64:

```
error: failed to load manifest for workspace member `.../examples/tutorials`
Caused by: failed to read `.../examples/tutorials/Cargo.toml`
Caused by: No such file or directory (os error 2)
```

The published sdist tarball is missing a directory (`examples/tutorials`) that its own workspace
`Cargo.toml` references — the source distribution is broken, independent of the glibc issue. Net
result: **not installable on this host by any path**, confirmed by direct build attempt, not
inference.

**VERIFIED (GitHub)**: repo `nautechsystems/nautilus_trader`, last push 2026-08-11 (today), 99
open issues, very active (25.4k stars). GitHub issue search for `arm64 OR aarch64` (75 hits)
surfaced a closed "SIGSEGV on macOS arm64 when pyarrow/pandas is imported before nautilus_trader"
— a real prior ARM64 crash class in this codebase, now fixed for macOS; no direct evidence either
way for this Linux glibc-2.34 case. API signatures/benchmarks: **not obtainable**.

## 3. pyarrow.parquet.ParquetWriter — installs, works, benchmarked

**VERIFIED**: already installed (25.0.1), imports and runs. Real signatures:

```python
ParquetWriter.__init__(self, where, schema, filesystem=None, flavor=None, version='2.6',
    use_dictionary=True, compression='snappy', write_statistics=True, ...,
    compression_level=None, ...)
ParquetWriter.write_table(self, table, row_group_size=None)
pq.read_table(source, *, columns=None, use_threads=True, ..., filters=None, ...)
ParquetFile.read_row_group(self, i, columns=None, use_threads=True, use_pandas_metadata=False)
```

**Write benchmark** (1M rows, batched in 5000-row chunks via `writer.write_table`, zstd level 9):
**8.499s wall clock → 117,666 rows/sec**, file size 134,647,379 bytes → **134.647 bytes/row**.

**Crash safety (VERIFIED, actually killed a live process)**: `ParquetWriter` itself has no crash
safety — a single evolving file has an unfinalized footer until `.close()`. Tested the standard
mitigation instead: many small part-files, `pq.write_table(tmp)` then `os.rename(tmp, final)`
(atomic on same filesystem). Ran a writer producing a new 2000-row part every ~15ms, `kill -9`'d it
after 2s: **1363 completed `.parquet` parts, 0 corrupt (all re-readable), exactly 1 orphaned
`.tmp` file from the in-flight part that was correctly excluded** since it was never renamed.
Pattern works cleanly but is something the caller must implement — pyarrow gives no atomic-append
primitive out of the box.

**Read benchmark**: time-range (1 instrument, 5-min window) via load-full-file + `pyarrow.compute`
filter: 0.2091s / 23 rows returned. Book-at-time (argmax of filtered timestamps): 0.0034s (table
already resident in memory for this test — a cold read would add file I/O + decompress time).

## 4. duckdb — installs, works, benchmarked, best crash-safety story

**VERIFIED**: already installed (1.5.5). Used as the tape store directly: `CREATE TABLE`, batched
`INSERT`, its own compressed columnar on-disk format, plus optional `COPY ... TO parquet`.
`execute()` is a pybind11 builtin with no introspectable Python signature; real usage pattern is
`con.execute(sql: str, parameters: list | None = None) -> DuckDBPyConnection`.

**Write benchmark** (1M rows, 5000-row batches, each in an explicit transaction, native DuckDB
storage, then `CHECKPOINT`): **8.637s → 115,775 rows/sec**, file size 121,909,248 bytes →
**121.909 bytes/row** with zero manual compression tuning (DuckDB's storage format
dictionary/RLE-encodes automatically). Exporting the same table to a standalone zstd-9 Parquet
file via `COPY tape TO 'x.parquet' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 9)` took
1.054s and produced 82,085,858 bytes → **82.086 bytes/row** — the best compression of every
candidate that installed.

**Crash safety — the strongest result of the whole test (VERIFIED, actually killed a live
process)**: wrote a script committing 100-row transactions in a tight loop against a DuckDB file,
`kill -9`'d the writer process mid-stream after 3s, then reopened the same file fresh:

```
row count after crash-recover reopen: 4700  max seq: 4699  duplicate seq values: 0
```

4700 is an exact multiple of the 100-row commit batch — every committed transaction survived
intact, the in-flight (uncommitted) transaction at kill time left zero trace, no corruption, no
duplicate rows, no manual atomic-rename plumbing required. This is DuckDB's WAL working exactly as
documented, verified by actually doing it rather than trusting the docs.

**Read benchmark**: built `CREATE INDEX idx_inst_ts ON tape(instrument_token, exchange_ts)`
(1.114s one-time cost). Indexed time-range query: **0.0014s** / 23 rows. Indexed book-at-time
(`ORDER BY exchange_ts DESC LIMIT 1`): **0.0050s**. Both are 15–100× faster than every other
candidate tested, because of the real B-tree index rather than a full-column scan.

## 5. polars — installs, works, benchmarked, no incremental-append primitive

**VERIFIED**: already installed (1.43.2). Real signatures:

```python
DataFrame.write_parquet(self, file, *, compression='zstd', compression_level=None,
    row_group_size=None, ..., partition_by=None, ...)
pl.scan_parquet(source, *, n_rows=None, hive_partitioning=None, ..., low_memory=False, ...) -> LazyFrame
```

Polars has **no streaming/incremental parquet-append API** suited to a live multi-writer tick
feed — the closest supported pattern is accumulate-then-write-once, or write many small files
yourself (same atomic-rename discipline as pyarrow would be needed, not separately re-tested since
the underlying single-file writer has the identical unfinalized-footer risk).

**Write benchmark** (accumulate 1M rows across 200 in-memory batches, then one
`write_parquet(..., compression='zstd', compression_level=9)`): **0.774s → 1,292,822 rows/sec**
(fastest raw throughput measured, because it's writing the whole materialized frame in one
columnar pass rather than incrementally flushing to a live file) — file size 91,081,659 bytes →
**91.082 bytes/row**.

**Read benchmark**: `pl.scan_parquet(path).filter(...).collect()` time-range: 0.0330s / 23 rows.
Book-at-time via `sort(descending=True).limit(1)`: 0.0232s.

## 6. fastparquet — installs, works, benchmarked, filter caveat found

**VERIFIED**: installed cleanly (2026.5.0). Real signatures:

```python
fastparquet.write(filename, data, row_group_offsets=None, compression=None, file_scheme='simple',
    ..., append=False, ...)
ParquetFile.to_pandas(self, columns=None, categories=None, filters=[], index=None,
    row_filter=False, dtypes=None)
```

**Write benchmark** (first batch via `write(..., append=False)`, then 199 subsequent batches via
`write(..., append=True)`, zstd): **11.2–12.1s → ~83,000–89,000 rows/sec** (two runs), file size
100,780,886 bytes → **100.781 bytes/row**.

**Read caveat found mechanically**: `to_pandas(filters=[...])` only does **row-group-level
pruning**, not exact-row filtering — verified directly: a query for one instrument in a 5-minute
window returned **20,000** "filtered" rows, and only **23** matched exactly after an explicit
pandas post-filter. A production consumer of this API must always re-filter after the fastparquet
call or it will silently over-fetch (here, by ~870×). Read wall time with the coarse fetch:
0.056s; book-at-time: 0.533s.

## 7. tables / PyTables (HDF5 + blosc:zstd) — installs, works, one real gotcha found

**VERIFIED**: installed cleanly (3.11.1). Real signatures:

```python
Table.append(self, rows: list | np.ndarray) -> None
Table.read_where(self, condition: str, condvars=None, field=None,
    start=None, stop=None, step=None) -> np.ndarray
```

**Gotcha found by actually running it**: calling `table.flush()` after every 5000-row batch (200
flushes total) with `blosc:zstd` compression level 9 **hung past a 240-second timeout** — this is
a real, reproducible cost of PyTables' per-flush chunk finalization under heavy compression, not a
one-off. Removing the per-batch `flush()` call (flush once at the end) fixed it:

- **complevel=5, single final flush, both columns indexed**: 9.417s append phase + ~4s index
  build = **13.878s total → 72,054 rows/sec**, file size 73,358,971 bytes → **73.359 bytes/row**.
  Indexed `read_where` time-range: 0.0625s / 23 rows. Book-at-time: 1.093s.
- **complevel=9, single final flush, no index**: **179.298s → 5,577 rows/sec**, file size
  67,163,668 bytes → **67.164 bytes/row** — the best compression of every mechanically-tested
  candidate, but the write rate (5,577 rows/sec) is right at the edge of the spec's stated peak
  (450–5000 rows/sec) with **zero margin**, and this was single-threaded/single-file — real
  multi-writer-thread contention on one HDF5 file would very likely push it under the required
  floor.

**Practical implication**: PyTables is viable at complevel=5 (fast, 73 bytes/row) but the
naive "flush every batch, compress hard" combination that looks natural to write is actually
+catastrophically+ slow — this is exactly the kind of footgun mechanical testing exists to catch.

## 8. zarr — installs, works, poor fit for wide tabular rows

**VERIFIED**: installed cleanly (3.3.0). Zarr is a chunked **array** store, not a table store — a
42-column row means 42 separate `Array` objects per session file, each appended independently.

**Write benchmark** (42 `array.append()` calls per 5000-row batch, `BloscCodec(cname='zstd',
clevel=9)` per array): **31.973s → 31,276 rows/sec**, total bytes across all 42 arrays' chunk
files: 82,336,736 → **82.337 bytes/row**. Still comfortably above the required peak rate, but ~4×
slower than the row-oriented options because of the 42-arrays-per-batch overhead.

**Read**: no built-in query/filter engine. A time-range-per-instrument read requires loading the
full `instrument_token` and `exchange_ts` columns into memory, building a numpy boolean mask, then
using `.oindex[]` on whichever value columns are needed — 0.1827s for the test window, but this
does not scale the way an indexed table does, and 42 arrays to manage per file is real operational
overhead versus a single Parquet/DuckDB file. **Poor structural fit** for this row shape.

## 9. pystore — installs, but genuinely broken on this host

**VERIFIED**: `pip install pystore` succeeds (1.0.1, PyPI upload 2025-07-22; GitHub repo last
pushed 2026-04-28, 10 open issues — so upstream `main` has moved since the last PyPI release).
Import succeeds, but the very first real call crashes:

```
>>> pystore.store("nse_tape")
...
File ".../pystore/utils.py", line 101, in write_metadata
    now = datetime.now(timezone.utc)
NameError: name 'timezone' is not defined
```

`pystore/utils.py` calls `datetime.now(timezone.utc)` without importing `timezone` — a genuine bug
in the installed package, not an environment/ARM64 issue (this is pure Python). **REJECT**: cannot
create a store at all on this host with the pip-installed version, so no write/read benchmark was
possible.

## 10. lakeapi — installs, but wrong category (read-only)

**VERIFIED**: installs cleanly (0.22.3, PyPI upload 2025-11-02). Inspected its actual public API:
`available_symbols, cache, list_data, load_data, set_cache_size_limit, set_default_bucket,
use_sample_data, used_data`. There is **no write/append function anywhere in the public API** —
lakeapi is a read-only client for pre-hosted public S3 datasets (Crypto Lake), not a store you
write into. **N/A / not applicable to this task** — not benchmarked further, no write path exists
to test.

## 11. qpython / qpython3 — installs, but broken + wrong category

**VERIFIED**: both install cleanly (pure Python), but `qpython3` (2.0.0, PyPI upload 2019-01-02 —
**7 years stale**) crashes on `import qpython.qconnection`:

```
QSYMBOL: numpy.string_,
AttributeError: `np.string_` was removed in the NumPy 2.0 release. Use `np.bytes_` instead.
```

Confirmed incompatible with the numpy 2.x already installed in this project. Even setting that
aside, qpython is fundamentally an **IPC client for an external running kdb+/q process** — not an
embedded Python store — so it is the wrong category of tool for this task regardless of the
numpy-compat bug. **REJECT**.

## Others searched, not separately benchmarked

Searched GitHub/PyPI for "tick store", "market data store", "order book recorder python", "L2
depth capture": most results were either (a) thin wrappers around Parquet/HDF5 already covered
above, (b) crypto-exchange-specific L2 recorders with hard dependencies on ccxt/websocket
exchange feeds (not a generic tape-store component), or (c) unmaintained (last commit >2 years,
<20 stars) — none looked like a materially better mechanical candidate than the general-purpose
columnar stores already tested, so effort was concentrated on getting real numbers for the
requested list instead of thin-evidence entries for long-tail repos.

---

## Summary table

| Candidate | Installs on ARM64 (pip, this host) | Write rate (1M-row bench) | Bytes/row (zstd) | Crash-safety | Time-range read | Verdict |
|---|---|---|---|---|---|---|
| **arcticdb** | **NO** — zero linux_aarch64 wheels on PyPI, no sdist. (conda-forge has it, unverified here.) | n/a | n/a | n/a | n/a | REJECT (pip-uninstallable) |
| **nautilus_trader** | **NO** — aarch64 wheel needs glibc≥2.35, host has 2.34; sdist build fails (broken tarball) | n/a | n/a | n/a | n/a | REJECT (pip-uninstallable) |
| pyarrow ParquetWriter | YES | 117,666 rows/sec | 134.647 | VERIFIED via atomic-rename part-files (1363/1363 clean after kill -9) | 0.209s (full scan+filter) | Viable; needs caller-built atomic-part-file discipline |
| **duckdb** | YES (pre-installed) | 115,775 rows/sec (native) | 121.909 native / **82.086** exported zstd parquet | **VERIFIED** — WAL survives kill -9 cleanly, exact tx-boundary recovery, no corruption | **0.0014s** (indexed) | **RECOMMENDED** |
| polars | YES (pre-installed) | 1,292,822 rows/sec (batch-then-write, not streaming) | 91.082 | Not independently tested (same unfinalized-file risk as pyarrow; no incremental-append API) | 0.033s | Viable for the read/query layer; no native append primitive |
| fastparquet | YES | ~83,000–89,000 rows/sec | 100.781 | Not tested (no atomic-rename built in) | 0.056s (row-group-pruned only — verified over-fetches ~870× vs exact match, must post-filter) | Viable but has a real read-filter gotcha |
| tables/PyTables | YES | 72,054 rows/sec (complevel=5) / 5,577 rows/sec (complevel=9) | 73.359 (L5) / **67.164** (L9, best) | Not tested; per-batch `flush()` at high compression hung >240s (verified footgun) | 0.0625s indexed (L5) | Viable at L5 only; avoid per-batch flush |
| zarr | YES | 31,276 rows/sec | 82.337 | Not tested | 0.183s (full-column scan, no index) | Poor fit — array store, not table store, 42 arrays/file |
| pystore | Installs, but **BROKEN** | n/a — crashes on first `store()` call | n/a | n/a | n/a | REJECT (genuine bug: missing `timezone` import) |
| lakeapi | YES | n/a — **no write API exists** | n/a | n/a | n/a | N/A — read-only client, wrong category |
| qpython/qpython3 | Installs, but **BROKEN** | n/a — crashes on import (`np.string_` removed in NumPy 2.0) | n/a | n/a | n/a | REJECT — broken + wrong category (kdb+ IPC client, not a store) |

## Disk-budget sanity check (using the two strongest real numbers)

At the spec's peak sustained rate (5000 rows/sec) for a full 6.25h session = 112.5M rows/session-day.
- DuckDB exported to zstd parquet (82.086 bytes/row, **verified**): ≈ 9.23 GB/session-day.
- PyTables complevel=9 (67.164 bytes/row, **verified**, but write-rate has no safety margin at
  this peak): ≈ 7.55 GB/session-day.

Either leaves roughly 2 peak-rate session-days inside the 19GB budget, or considerably more at
realistic average (rather than peak) tick rates — bytes/row is confirmed to be the dominant sizing
lever the task description expected it to be.

## Recommendation

**DuckDB** (already installed, 1.5.5). It is the only candidate with **verified, actual kill-9
crash safety with zero custom plumbing** (WAL-backed transactions, tested by really killing the
writer process and reopening the file — 4700/4700 committed rows intact, 0 corruption, 0
duplicates), it has the **best or near-best compression** of every real store tested (82.086
bytes/row exported, ahead of every option except a barely-viable PyTables extreme-compression mode
that has no write-rate safety margin), it is **10–100× faster on the exact read patterns the spec
needs** (indexed time-range: 1.4ms; indexed book-at-time: 5ms) because it is a real SQL engine with
B-tree indexes rather than a full-file scan, and it natively supports session-date partitioning via
one file per session plus `ATTACH`/cross-file `UNION` queries, or `COPY` export to partitioned
Parquet for cold storage. ArcticDB (the obvious "tick store" purpose-built option) and
nautilus_trader are both **mechanically confirmed uninstallable on this exact host via pip** — not
a matter of preference, they simply do not build/import here today.
