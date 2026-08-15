# 235 · Why the depth capture stopped — read from its own logs

**Measured 2026-08-15** from `~/nse_archive/depth_capture_*.log`,
`~/nse_archive/depth_tape/session_report_*.json` and `~/nse_archive/cross_broker_*.log`. Answers
`BACKLOG` `M22`. **Decision taken on the result: `A.118`.**

---

## 1 · The answer, in one line

**Nothing failed. The captures are started by hand and stopped by hand**, and on 2026-08-13 a
`SIGTERM` at 12:15:06 ended the depth capture and the cross-broker capture in the same second.

## 2 · The evidence

`depth_capture_2026-08-13.log`, in full at the relevant points:

```
[09:49:26] session closes at 15:30 IST, 5:40:33 remaining
[09:51:17] admitted 652 of 9,891 instruments | projected 0.33 GiB of a 0.33 GiB budget (100%)
[09:51:17] capturing
[12:15:06] signal 15 — ending the session cleanly
[12:15:17] session: 2,373,256 packets written, 0 dropped to overflow
[12:15:17] tape now holds 1.29 GiB
```

`cross_broker_2026-08-13.log` at the same instant:

```
[12:15:06] stop requested; finishing the current poll and flushing
[12:15:08] done: 4356 sweeps, 718,740 rows
```

Two independent processes, one second, no traceback, no error, no OOM (which would be signal 9).
The launcher installs a `SIGTERM`/`SIGINT` handler that logs exactly that line, so the signal came
from OUTSIDE — a terminal closing, a Ctrl-C on a shared shell, or a `pkill`.

**No scheduler exists.** `crontab` holds only the Kite token refresh and the NSE report ingestion;
the systemd user units are the dashboard and daily-operations. Nothing starts or stops the depth
capture, and left alone it runs to the close under its own clock — `depth_capture_2026-08-11_run4`
proves it: `[15:30:13] session: 10,212,041 packets written`.

## 3 · The late starts have the same cause

| session | first capture line | why the tape starts later still |
|---|---|---|
| 2026-08-11 | 10:00:58 (run 1) | runs 1–3 killed at 10:07, 10:09 and 10:40 while the operator retuned the admission budget (0.49 → 2.11 → 2.10 → 2.08 GiB); **run 4 at 10:40:42 is the one that ran to the close** |
| 2026-08-12 | ~10:30 (run `103035`) | that run's session report records **0 usable instruments out of 300**; a second run started 10:43 and holds the 7.96 M packets |
| 2026-08-13 | 09:49:26 | one run, ~2 minutes of liquidity ranking and admission sizing before `capturing` |

The market opens at 09:15. Every session begins its capture 35 to 90 minutes late because a person
started it, and 2026-08-11 and -12 begin later than that because their first attempts were replaced.

## 4 · Why nothing on disk said so

The launcher writes its session report — coverage, usability, bytes per row — **once, at the very
end, after a scan of the whole tape**. On 2026-08-13 the log stops immediately after
`tape now holds 1.29 GiB`, which is the line before `build_session_report(...)`: the process was
killed again before the verdict could be built. **So the 2026-08-13 tape carries no report at all**,
and there is no file anywhere on disk that says it covers 09:51–12:15 rather than a full session.

That is the whole of `M22`. Not a crash — a silence.

## 5 · A second fact worth reading separately

`[09:51:17] admitted 652 of 9,891 instruments | projected 0.33 GiB of a 0.33 GiB budget (100%)`

The universe the capture records is set by a **disk budget**, not by the market. 652 of 9,891 NSE
equities on 08-13, 649 on 08-11 run 1, 9,000 on runs 3 and 4. `R.09` asks for the full universe and
the tape delivers between 7% and 91% of it depending on how much disk the operator gave that run —
which is a separate, unrecorded constraint on every measurement taken through this tape.

## 6 · What was done about it

`A.118`: the recorder now writes a **liveness record** on every poll — atomically, a few hundred
bytes, beside the shards — carrying the run id, the first and last packet, the rows written and
whether it reached the session close. It survives `SIGKILL`. `session_was_fully_captured` answers
`True`, `False` or `None`, and `None` (no record at all, as on all three existing tapes) is never
read as complete. `/paper-session` shows it above the risk latches.

## 7 · What this does NOT do

- **It does not start the capture.** Whether the capture should run on a schedule from the open is
  an operator decision, not a code one, and it is not taken here (`BACKLOG` `M23`).
- **It does not widen the universe.** The disk budget still decides how many instruments are
  recorded, and nothing yet reports that as a coverage limit (`BACKLOG` `M24`).
- **It cannot describe the three existing tapes.** They were recorded before the record existed, so
  they answer `None` — unknown — and `A.116`'s packet-derived window remains how the paper loop
  knows what they cover.
