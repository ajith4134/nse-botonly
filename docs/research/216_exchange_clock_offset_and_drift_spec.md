# `L0.32` — Clock sync + drift alert, specified as an offset/skew ESTIMATOR

*Spec, 2026-08-12. Written before the code (`R.23(c)`). The plan entry is one line — "detects host clock
drift against exchange time" — and the whole difficulty is hidden in the word "detects": nothing on this
host can observe exchange time directly. What is observable is a **one-way delay contaminated by an
unknown offset**, quantised to whole seconds, and the engine's job is to separate those three things.*

## 1. What is actually observable, measured first

Every depth packet already carries both clocks: `exchange_time` (the exchange's stamp, Kite delivers it
at **1-second resolution**) and `receipt_time` (this host's stamp, microseconds). The tape holds
**15,902,625 packets** across 2026-08-11 and 2026-08-12. Measured over all of them:

| quantity | value |
|---|---|
| rows with an ABSENT exchange stamp (epoch 0) | 10,518 |
| lag `receipt − exchange`, after dropping those: min | 0.032 s |
| p1 / p50 / p99.9 | 0.251 / 0.791 / 1.375 s |
| max | 4,931.7 s (a stale packet, correctly flagged) |
| negative lags after filtering | **0** |
| per-minute MINIMUM of the lag, median over 506 minutes | 0.263 s |

Two facts in that table set the whole design.

**The lag is quantisation plus delay plus offset, and only their sum is visible.** Because the exchange
stamp is truncated to a second, a packet stamped `E` was really generated somewhere in `[E, E+1)`. So

```
lag_i  =  (receipt_i − E_i)  =  θ + d_i + u_i ,     u_i ∈ [0, 1)  (unknown truncation residue)
```

where `θ` is the host-minus-exchange clock offset and `d_i > 0` the one-way network delay. A mean of
`lag` estimates `θ + E[d] + 0.5` and is therefore biased by half a second by construction — which is why
the median (0.791 s) sits roughly half a second above the per-minute minimum (0.263 s). **The minimum is
the informative statistic**, not the mean; every additional packet is another chance to observe a small
`d_i + u_i` and squeeze the bound.

**The lower envelope is a line, not a level.** If the host clock runs at a rate `1 + α` relative to the
exchange, `θ` grows linearly in time and the floor of the delay cloud tilts. Fitting that tilt is the
skew estimate; a naive per-day average would attribute the tilt to noise and report a stable clock while
the host quietly slid.

## 2. The algorithm — LP on the delay envelope, not an average

The estimator is the **linear-programming clock-offset/skew estimator of Moon, Skelly & Towsley (1999)**,
which is the standard published treatment of exactly this measurement (one-way delays between two hosts
whose clocks differ in both offset and rate). It fits the line that lies **below every observation and as
close to them as possible**:

```
minimise    Σ_i ( lag_i − (α·t_i + β) )
subject to  α·t_i + β  ≤  lag_i          for every packet i
```

Solved with `scipy.optimize.linprog` (HiGHS). The objective is linear in `(α, β)` because the sum of
residuals is; the constraints are the "delay is never negative" physics. The fitted `β` is the offset at
session start and `α` the relative rate — reported in ppm because that is the unit clock hardware is
specified in, and because chrony reports its own correction the same way (this host: **6.917 ppm slow**,
read from `chronyc tracking`, which the engine records as the ground truth it is being compared against).

**Quantisation is handled by the constraint, not by a correction term.** Since `u_i ∈ [0, 1)` is
non-negative, it belongs on the same side of the inequality as the delay, so the LP's line is a lower
bound on `θ` alone: `β ≤ θ`. The residual bias is bounded by the smallest `d_i + u_i` observed, which the
engine reports as the estimate's own uncertainty rather than pretending to a point value. This is why the
output is an **interval**, and why `TimestampTrustBudget` (below) is expressed as a worst-case error.

**A second, independent estimator disciplines the first.** `ntplib` samples the reference clock directly
(this host: OCI's `169.254.169.254`, measured offset **+192 µs**, RTT 854 µs, stratum 3), giving
`host − UTC` with a *bounded round trip*, which the exchange feed cannot provide. Marzullo's algorithm
intersects the per-server intervals `[offset − rtt/2, offset + rtt/2]` into the tightest interval
consistent with the majority, discarding falsetickers. Then:

```
exchange_clock_error  ≈  (host − exchange)  −  (host − UTC)  =  θ − θ_ntp
```

That difference is the only way to tell "my clock is wrong" from "the exchange's clock is wrong or the
path is slow", and it is the reason the engine samples NTP at all on a host chrony already disciplines.

**Drift ALERTS are change detection, not a threshold.** The residual series feeds `river`'s ADWIN and
Page-Hinkley detectors: ADWIN for a distribution change in the delay floor (a route change, a VM
migration), Page-Hinkley for a cumulative one-sided drift (a clock slipping). Both are online and carry
their own state, so an alert names *when* the change began rather than which arbitrary bound was crossed.
No constant anywhere: alert levels come from the empirical quantiles of this host's own history
(`R.03`), and a fresh install is explicitly IMMATURE until it has samples, never silently permissive.

## 3. Modules (`R.14` names, one responsibility each)

| module | responsibility |
|---|---|
| `clock_integrity/exchange_feed_delay_observation.py` | The observation record + extraction from the depth tape/live packets, including the epoch-0 absent-stamp rejection |
| `clock_integrity/reference_clock_ntp_sampler.py` | `ntplib` sampling of N servers + Marzullo intersection; also reads `chronyc tracking` as the host's own account of itself |
| `clock_integrity/exchange_clock_offset_estimator.py` | The LP fit (offset, skew, bound) + rolling-window refits for online tracking (§9: no Kalman — wrong noise model) |
| `clock_integrity/depth_tape_delay_sampler.py` | The raw pipeline: parquet tape → one observation per exchange-second, an EXACT reduction of the LP |
| `clock_integrity/clock_offset_observation_store.py` | SQLite: every sample, every fit, per-day skew history — the carried state |
| `clock_integrity/clock_drift_change_detector.py` | ADWIN + Page-Hinkley over residuals, emitting dated change points |
| `clock_integrity/timestamp_trust_budget.py` | The DECISION: worst-case timestamp error and a `TRUSTED / DEGRADED / REFUSE` verdict with the maturity ladder (`R.04`) |
| `dashboard/clock_integrity_surface_renderer.py` | `/clock` |

## 4. What decision changes (`R.06`, checklist item 4)

*This section was rewritten after the wiring, because the first answer was arithmetically wrong and the
test that was supposed to demonstrate it failed instead.*

**The first answer — correcting staleness — is a no-op, and the reason is worth keeping.**
`DepthPacketIntegrityClassifier` flags a packet whose staleness exceeds the instrument's own p99.9. The
threshold is a QUANTILE of the same series the packet belongs to, and quantiles are **shift-equivariant**:
subtracting a constant from every observation moves the threshold by exactly that constant, so the flag
cannot change. Measured: a classifier told this host runs 300 ms fast produced flag-for-flag identical
output over 1,050 packets. Claiming that as a behaviour change would have been a false claim, and
`test_a_constant_host_clock_error_cannot_change_the_staleness_flag` now pins it so no future version
quietly re-adds the correction and claims it again.

**Where a constant clock error genuinely bites is an ABSOLUTE comparison**, and the classifier has one:
`_classify_session_window` tests the host's receipt instant against the exchange's 09:00-15:30 boundary. A
host running 300 ms fast pushes packets received at 15:29:59.9 across the close and flags real in-session
data as `OUTSIDE_SESSION_WINDOW`. That comparison is not shift-invariant, so the measured error is
subtracted there — and
`test_the_host_clock_error_changes_which_packets_are_called_out_of_session` asserts the flag actually
flips.

The general rule this produced: **a clock correction changes nothing that is measured relative to itself,
and everything that is measured against an external boundary.** Skew is the exception in the other
direction — a DRIFTING clock is not a constant shift, so it does move a quantile, which is why the fit
reports skew separately and the budget projects it forward.

Queued consumers, recorded rather than pretended (`R.11`): order-latency attribution and the live
execution path (`L3.x`) do not exist yet; when they do, `REFUSE` becomes an order-placement veto.

## 5. Depth justification — what a diagnostic version would omit

A monitor version of this feature is thirty lines: read `staleness_micros`, average it, print it on a
panel. It would omit (a) the quantisation reasoning entirely, and so report a half-second offset that is
not there; (b) skew, and so report a drifting clock as a stable one; (c) the NTP arm, and so be unable to
attribute the error to either side; (d) carried state, and so lose the rate estimate that only appears
across days; (e) change detection, and so alert on a threshold that a growing delay floor eventually
crosses for the wrong reason. The SOTA analog is an **NTP/PTP clock servo** — the same offset+skew state,
the same delay-floor filtering, the same falseticker rejection — and the LP fit is the published
measurement-side equivalent for one-way data.

## 6. Signatures (`R.23(c)` step 2) and error behaviour

```python
@dataclass(frozen=True, slots=True)
class ExchangeFeedDelayObservation:
    instrument_token: int
    exchange_second: datetime       # truncated stamp, tz-aware UTC
    received_at: datetime           # host stamp, tz-aware UTC
    @property
    def apparent_lag_seconds(self) -> float: ...

@dataclass(frozen=True, slots=True)
class ClockOffsetFit:
    offset_seconds: float           # β — host minus exchange at `fitted_from`
    skew_ppm: float                 # α × 1e6
    offset_lower_bound_seconds: float
    offset_upper_bound_seconds: float
    sample_count: int
    fitted_from: datetime
    fitted_to: datetime
    residual_floor_seconds: float   # the smallest observed d+u — the estimate's own slack

class ExchangeClockOffsetEstimator:
    def fit(self, observations: Sequence[ExchangeFeedDelayObservation]) -> ClockOffsetFit: ...
    def offset_at(self, fit: ClockOffsetFit, instant: datetime) -> float: ...

class TimestampTrustBudget:
    def verdict(self, at: datetime) -> TrustVerdict: ...   # TRUSTED | DEGRADED | REFUSE | IMMATURE
    def corrected(self, host_instant: datetime) -> datetime: ...
```

Error behaviour, stated because silence here is the failure mode:
- fewer than the minimum samples → `IMMATURE`, never a fitted number. Immaturity is a *state*, not an
  exception, because the daily runner must be able to record it.
- an LP that does not converge → `ClockFitInfeasibleError`, surfaced, never a fallback to the mean.
- absent exchange stamps (epoch 0) → dropped at the observation boundary with a counted reason.
- an NTP server that times out → recorded as a failed sample; the intersection proceeds on the rest, and
  Marzullo with fewer than two responders returns `None` rather than trusting one server.

## 7. Acceptance criteria — checked, not claimed

1. LP fit runs on the **real 15.9M-packet tape** and returns an offset within its own reported bounds.
2. Property test: on synthetic delays generated with a KNOWN offset and skew plus a positive delay
   distribution and 1-second truncation, the fit recovers both within the stated tolerance.
3. Adversarial: a single packet with a 4,931 s lag (the real outlier in the tape) must not move the fit —
   the LP is a lower envelope, so an upward outlier is structurally ignored; asserted, not assumed.
4. Marzullo returns the intersection on agreeing servers and rejects a falseticker.
5. Change detectors fire on an injected step and do NOT fire on stationary noise.
6. The integrity classifier's flag output demonstrably changes when a corrected offset is supplied.
7. `/clock` renders from the real store, ruff + mypy clean, and the R.05 real-data pass is recorded.

## 8. Out of scope, recorded so it is not mistaken for an omission

- **No PTP / hardware timestamping.** This is a cloud VM; there is no PHC to read.
- **No clock STEERING.** The engine estimates and refuses; it never calls `adjtimex`. Correcting the host
  clock is chrony's job and fighting it would produce two servos oscillating against each other.
- **No per-instrument offset.** The offset is a property of the two clocks and the path, not of a symbol;
  per-instrument variation is delay, which the envelope already handles.

## 9. Sourcing pass — run, with mechanical evidence (`R.16`, `R.17`)

Installs into a throwaway venv, signatures read with `inspect.signature`, every candidate run.

| part | candidate | evidence | verdict |
|---|---|---|---|
| NTP client | **`ntplib` 0.4.0** | installs; `NTPClient.request(host, version=2, port='ntp', timeout=5)`; ran against `169.254.169.254` → offset +192 µs, delay 854 µs, stratum 3, root dispersion 137 µs | **ADOPT** |
| NTP client | `python-ntp` 1.0.0 (2024-07-16) | installs; a real RFC 5905 state machine (leap/jitter/clock-filter), but no one-shot call returns `(offset, delay, stratum, dispersion)` — the caller drives `prepare_request` → socket → `process_response` → `calculate_state` | **ORACLE-ONLY** — a correctness reference, not a drop-in |
| NTP client | `pyntp` 0.1.1 | source reads: it imports `ntplib` itself, then discards `delay`, `stratum` and `root_dispersion` and exposes a thread-smoothed `offset` only | **REJECT** — strictly a subset of the thing it wraps |
| Marzullo intersection | — | PyPI 404 on `marzullo`, `ntp-intersection`, `clockcombine`, `falseticker`, `intersection-algorithm`, `pyntpsec`, `ntpsec`; GitHub returns 8 unpackaged single-file student repos (no `setup.py`, last commits 2015-2023). `ntpsec` implements it in C and ships no Python package | **NONE EXISTS — build in-house** |
| offset/skew LP | — | GitHub search for the Moon-Skelly-Towsley estimator returns nothing; no `owamp`/`pchar` on PyPI | **NONE EXISTS — build in-house** |
| LP solver | **`scipy.optimize.linprog` (HiGHS)** | scipy 1.18.0 already installed; `method='highs'` is the default | **ADOPT** as the primitive |
| drift detection | **`river.drift.ADWIN`, `river.drift.PageHinkley`** 0.25.0 | already installed; `ADWIN(delta=0.002, clock=32, max_buckets=5, min_window_length=5, grace_period=10)`, `PageHinkley(min_instances=30, delta=0.005, threshold=50.0, alpha=0.9999, mode='both')` | **ADOPT** |
| Kalman servo | `filterpy` 1.4.5 / `pykalman` 0.11.2 | both install and run a 2-state toy | **REJECTED ON MODEL GROUNDS, not maintenance** — see below |

**Why no Kalman filter, despite §2's first draft naming one.** A Kalman servo is optimal for *Gaussian*
measurement noise. The noise here is one-way network delay: strictly non-negative, heavy-tailed to the
right (the real tape's maximum lag is 4,931 s against a 0.26 s floor), and therefore about as far from
Gaussian as a measurement gets. A Kalman update would treat that 4,931 s packet as evidence and drag the
state; the envelope treats it as the non-information it is. Online tracking between session fits is done
by **refitting the envelope over a rolling window** instead — the same estimator, the same asymmetry, no
second noise model to justify. `filterpy`'s PyPI release is also frozen at 2018 (repo commits to 2024)
and `pykalman` is fresher (2026-01), but that comparison never became load-bearing.

## 11. Adversarial review round (`R.23(c)` step 5) — seven defects, all reproduced

A fresh subagent was instructed to REFUTE the engine's claims and to report only what it could reproduce
by running code. It did, against 70 passing tests. Full account in plan entry `A.83`; the design
consequences that belong in this spec:

**§2's drift-detection paragraph was wrong about its own threshold.** Calibrating by permutation is only
meaningful if the statistic measured under the null is the statistic that later fires. It was not:
`river`'s `PageHinkley` applies a 0.9999 forgetting factor and is two-sided, while the calibration
measured a one-sided plain cumulative sum. Null false-alarm rate measured at **100/100** against a
threshold claiming 1/100. The engine now runs its own two-sided Page-Hinkley, and `_page_hinkley_excursion`
— the function the permutation null measures — IS the detector's update rule. Re-measured: **1/100**.
`river.drift.ADWIN` is still used, unchanged, for the distributional shifts the one-sided sum sits through.

**The LP objective is summed over the HULL, not over every observation.** Summing over all points made
the objective depend on `mean(t)` across the whole cloud, so distant late packets could tip which hull
edge won — three copies of the tape's 4,931-second outlier flipped the fitted skew's sign. Restricting the
objective to hull points makes "an upward outlier cannot move this fit" provable rather than intended, and
on the real tape it moved the 2026-08-12 skew from +16.43 ppm to **-6.06 ppm against chrony's
independently measured -6.917 ppm**: agreement to 0.85 ppm between two paths that share no code.

**The offset bound is `min_i(lag_i - skew*t_i)`, not the LP intercept.** The LP bounds a sum; nothing in
it pins the intercept pointwise, and a steeply rising delay floor produced a "bound" 100 seconds BELOW the
true offset. The de-skewed minimum lag is supported by every observation individually — and because it is
conditional on the rate estimate, `ClockOffsetFit.skew_is_physically_plausible` now qualifies it against
NTP's own 500 ppm hardware clamp (RFC 5905 §7.2). A rate beyond that is the network, not a clock, and the
verdict reads IMMATURE.

**§7's acceptance criteria 1 and 3 were passing for the wrong reason** — the fit survived ONE outlier, and
the tests only tried one. Criterion 3 now uses three, which is what broke it.

Also fixed, each with a regression test: an unhandled `StatisticsError` when a change was detected at the
final point; the falseticker branch answering before the impossible-physics check; a drift series too
short to judge reading as "no alerts"; HiGHS's feasibility tolerance leaving the line 15 µs above two of
its own constraints (now clamped); NaN lags raising from inside scipy; collinear points counted as hull
vertices; the split-half maturity check slicing by position rather than time; and a reference bracket of
any age applied as today's correction (now bounded to 12 hours, and returned with its provenance so
"unmeasured" cannot be read as "measured zero").

**What survived a genuine attack:** Marzullo matched a brute-force oracle on every interval configuration
tried; the LP sign convention is correct and provably bounded; the hull reduction is exact for
feasibility; and the permutation scheme is a valid null for the statistic it measures.
