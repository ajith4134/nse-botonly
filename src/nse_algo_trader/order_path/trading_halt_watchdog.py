"""The process that pulls the latch, living outside the process it is watching.

`L7.10`. `docs/research/221` §9: *"A trader that is wedged or looping cannot stop itself; that is
the entire reason `L7.10` asks for a separate process, and an in-process flag would be a labelling
layer over the same failure."* Everything in this module follows from taking that sentence
literally.

**What it watches, and why a heartbeat rather than a health check.** A trader that is deadlocked on
a socket, spinning in a retry loop, or paused by the kernel still answers "am I alive?" with
whatever its last thread wrote. It cannot, however, keep *writing new rows* — so liveness here is a
monotonic stream of heartbeats into a store the watchdog reads from a different process, and the
absence of new rows is the signal. Nothing the trader can say makes it look alive; only what it
does.

**Why the staleness threshold is derived and not typed (`R.03`).** A heartbeat interval is a
property of the deployment — a scanner beating once a minute and a tick handler beating ten times a
second are both healthy, and any constant that suits one declares the other dead or lets it hang for
minutes. So the tolerance comes out of the observed intervals themselves:

    centre      = median of the observed intervals
                  — robust to a single long GC pause dragging the mean
    dispersion  = 1.4826 x median absolute deviation
                  — the MAD-to-sigma consistency constant, used only as a *scale*
    alpha       = 1 / (n + 1)
                  — at most one false halt per window of history as long as the one observed
    k           = sqrt((1 - alpha) / alpha) = sqrt(n)
                  — Cantelli's one-sided inequality, which assumes no distribution at all;
                    heartbeat gaps are right-skewed and are emphatically not Gaussian
    tolerance   = max(centre + k*dispersion, longest observed gap + dispersion)

Every term is a function of the data. The floor matters as much as the bound: a gap that has already
been observed while the trader was demonstrably healthy must not be treated as death the next time
it happens, so the tolerance can never sit below the worst gap the deployment has actually shown.
The dispersion itself has a floor of `centre / (n + 1)` — with `n` observations you cannot claim a
jitter finer than that, and a perfectly regular heartbeat would otherwise derive a zero-width
tolerance and halt on its first microsecond of scheduling noise.

**Why a future-dated heartbeat cannot keep the system armed.** The trader writes its own beats, so
its timestamps are exactly as trustworthy as the process this watchdog exists to distrust. Two
independent defences, both of which halt rather than ignore:

- a beat whose *claimed* time is later than the time at which it was *recorded* is impossible, and
  is treated as a forged or skewed clock (`HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP`);
- freshness is measured against the **watchdog's own clock** using `min(claimed, recorded)`, so a
  beat stamped an hour into the future ages exactly as fast as one stamped honestly, and a wholly
  future-dated record produces a negative age, which is itself an implausibility rather than
  infinite freshness.

**Why restart is a reconciliation and not a resume.** A watchdog that crashed and came back has no
right to believe anything it believed before. `reconcile_on_start` re-derives everything from
evidence on disk: the latch's own durable state, and the heartbeat history read against the current
clock. A released latch with no live trader behind it is latched. A released latch inherited from a
previous session, whose newest beat is older than the derived tolerance, is latched. The watchdog
has exactly one power — it can close the latch, never open it — so the worst outcome of a wrong
inference here is a halt an operator clears, which is the direction this whole feature is pointed.

**What it does not do yet, named rather than hidden (`R.06`/`R.08`).** Broker-state reconciliation
is injected as a `ReconciliationProbe` callable rather than imported, because `F02`'s reconciler
(`L3.03`) is being built alongside this. The seam is the integration point and it is not optional:
a probe that raises is a *failed* reconciliation and latches, so wiring a broken reconciler in
cannot quietly disable the check.
"""

from __future__ import annotations

import itertools
import math
import os
import sqlite3
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from nse_algo_trader.order_path.trading_control_latch import (
    ChangeAuthority,
    LatchDisposition,
    LatchState,
    OperatorAuthorization,
    TradingControlLatchStore,
    TradingMode,
)

DEFAULT_TRADER_HEARTBEAT_PATH: Final[Path] = Path(
    "~/.nse_algo_trader/trader_heartbeat.sqlite3"
).expanduser()

# MAD -> sigma. 1/Phi^-1(0.75); the standard consistency constant, not a tuned parameter.
_MEDIAN_ABSOLUTE_DEVIATION_TO_SIGMA: Final[float] = 1.4826

# How much history the estimator keeps in view. A memory bound, NOT a decision threshold: every
# number the estimator produces is derived from whatever intervals fall inside this window, and a
# deployment that changes its beat rate is fully re-learned within one window.
DEFAULT_HEARTBEAT_HISTORY_WINDOW: Final[int] = 256

_MINIMUM_INTERVALS_FOR_DERIVATION: Final[int] = 1

_HEARTBEAT_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS trader_heartbeat (
    component            TEXT NOT NULL,
    claimed_beat_at_utc  TEXT NOT NULL,
    recorded_at_utc      TEXT NOT NULL,
    process_id           INTEGER NOT NULL,
    PRIMARY KEY (component, claimed_beat_at_utc)
);
CREATE INDEX IF NOT EXISTS trader_heartbeat_by_record_time
    ON trader_heartbeat (component, recorded_at_utc);
"""

_HEARTBEAT_COLUMNS: Final[str] = "component, claimed_beat_at_utc, recorded_at_utc, process_id"


class WatchdogError(Exception):
    """The base of every refusal the watchdog makes."""


class InsufficientHeartbeatHistoryError(WatchdogError):
    """Not enough beats to derive a tolerance from data, and this module will not invent one."""


class HeartbeatJournalError(WatchdogError):
    """A heartbeat could not be written, so the trader must not believe it has been seen."""


class HeartbeatVerdict(StrEnum):
    """What the heartbeat evidence says. Only `FRESH` is compatible with trading."""

    FRESH = "fresh"
    STALE = "stale"
    NO_EVIDENCE = "no_evidence"
    IMPLAUSIBLE_TIMESTAMP = "implausible_timestamp"
    TOLERANCE_UNDERIVABLE = "tolerance_underivable"


class HaltTrigger(StrEnum):
    """Why the watchdog closed the latch. Carried into the latch reason, so the log answers it."""

    HEARTBEAT_STALE = "heartbeat_stale"
    HEARTBEAT_ABSENT = "heartbeat_absent"
    HEARTBEAT_TIMESTAMP_IMPLAUSIBLE = "heartbeat_timestamp_implausible"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RECONCILIATION_UNAVAILABLE = "reconciliation_unavailable"
    OPERATOR_COMMAND = "operator_command"


@dataclass(frozen=True, slots=True)
class HeartbeatRecord:
    """One beat: what the trader claimed the time was, and when the row actually landed."""

    component: str
    claimed_beat_at: datetime
    recorded_at: datetime
    process_id: int

    @property
    def effective_beat_at(self) -> datetime:
        """The latest instant this beat can honestly be said to prove liveness for.

        `min` of the two, never the claim alone. A beat cannot have happened after the write that
        recorded it, so a later claim is either a clock fault or an attempt to look alive.
        """
        return min(self.claimed_beat_at, self.recorded_at)

    @property
    def is_timestamp_implausible(self) -> bool:
        """Whether this beat claims to come from after the moment it was written down."""
        return self.claimed_beat_at > self.recorded_at


@dataclass(frozen=True, slots=True)
class HeartbeatToleranceEstimate:
    """The derived staleness threshold, with every term that produced it kept for inspection.

    Kept as data rather than returned as a bare `timedelta` because the number will one day be
    blamed for a halt, and "why was the tolerance 4.1 seconds" has to be answerable from the object
    itself rather than from a re-run.
    """

    sample_size: int
    centre_interval: timedelta
    dispersion: timedelta
    longest_observed_interval: timedelta
    false_halt_probability_bound: float
    cantelli_multiplier: float
    staleness_tolerance: timedelta

    def describe(self) -> str:
        """One line naming the derivation, for a log line that has to justify a halt."""
        return (
            f"tolerance {self.staleness_tolerance.total_seconds():.3f}s derived from "
            f"n={self.sample_size} intervals: centre "
            f"{self.centre_interval.total_seconds():.3f}s, dispersion "
            f"{self.dispersion.total_seconds():.3f}s, k={self.cantelli_multiplier:.3f} "
            f"(Cantelli at alpha={self.false_halt_probability_bound:.4g}), longest observed "
            f"{self.longest_observed_interval.total_seconds():.3f}s"
        )


@dataclass(frozen=True, slots=True)
class HeartbeatFreshnessAssessment:
    """Whether the watched process is demonstrably alive, and on what evidence."""

    component: str
    assessed_at: datetime
    verdict: HeartbeatVerdict
    latest_heartbeat: HeartbeatRecord | None
    heartbeat_age: timedelta | None
    tolerance: HeartbeatToleranceEstimate | None
    detail: str

    @property
    def is_trader_demonstrably_alive(self) -> bool:
        return self.verdict is HeartbeatVerdict.FRESH


@dataclass(frozen=True, slots=True)
class ReconciliationVerdict:
    """What a reconciliation run concluded. A failure is a halt condition, not a warning."""

    succeeded: bool
    checked_at: datetime
    detail: str


class ReconciliationProbe(Protocol):
    """The seam `F02`'s reconciler (`L3.03`) plugs into. One call, one verdict, no arguments."""

    def __call__(self) -> ReconciliationVerdict: ...


@dataclass(frozen=True, slots=True)
class WatchdogSupervisionCycle:
    """One pass of the watchdog, whole enough to explain itself without re-reading the store."""

    observed_at: datetime
    is_startup_reconciliation: bool
    disposition_before: LatchDisposition
    disposition_after: LatchDisposition
    heartbeat_assessment: HeartbeatFreshnessAssessment
    reconciliation_verdict: ReconciliationVerdict | None
    halt_triggers: tuple[HaltTrigger, ...]
    next_poll_interval: timedelta

    @property
    def did_halt_trading(self) -> bool:
        """Whether this cycle is the one that closed a latch that had been open."""
        return (
            self.disposition_before.latch_state is LatchState.RELEASED
            and self.disposition_after.latch_state is LatchState.LATCHED
        )

    def describe(self) -> str:
        triggers = ", ".join(trigger.value for trigger in self.halt_triggers) or "none"
        prefix = "startup reconciliation" if self.is_startup_reconciliation else "supervision cycle"
        return (
            f"[{self.observed_at.astimezone(UTC).isoformat()}] {prefix}: "
            f"{self.heartbeat_assessment.verdict.value}; triggers={triggers}; "
            f"latch={self.disposition_after.describe()}"
        )


@dataclass(slots=True)
class TraderHeartbeatJournal:
    """Where the trader proves it is still running, and the watchdog reads that proof.

    Its own store, not a table in the latch database: the trader writes here at a high rate and must
    never contend with, lock, or be able to damage the file that decides whether it may trade.
    """

    database_path: Path = DEFAULT_TRADER_HEARTBEAT_PATH
    busy_timeout_seconds: float = 30.0
    _initialisation_failure: str | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.executescript(_HEARTBEAT_SCHEMA)
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as failure:
            # Same reasoning as the latch: a journal that cannot be opened must degrade into "no
            # evidence of life", which halts, rather than into an exception that stops the watchdog.
            self._initialisation_failure = f"{type(failure).__name__}: {failure}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=self.busy_timeout_seconds)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def record_heartbeat(
        self,
        *,
        component: str,
        claimed_beat_at: datetime | None = None,
        recorded_at: datetime | None = None,
        process_id: int | None = None,
    ) -> HeartbeatRecord:
        """Write one beat. Raises on failure, because a beat nobody stored is not a beat.

        `recorded_at` is injectable for the hermetic harness only. In production it is this
        process's own clock at the instant of the write, and it is the value that bounds what the
        claimed time is allowed to be.
        """
        if not component.strip():
            raise HeartbeatJournalError(
                "a heartbeat with no component name proves that *something* is alive, which is not "
                "a fact the watchdog can act on"
            )
        moment_recorded = recorded_at or datetime.now(UTC)
        moment_claimed = claimed_beat_at or moment_recorded
        for label, moment in (("claimed", moment_claimed), ("recorded", moment_recorded)):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise HeartbeatJournalError(
                    f"the {label} heartbeat time {moment!r} carries no timezone; liveness compared "
                    f"across processes cannot rest on an ambiguous instant"
                )
        if self._initialisation_failure is not None:
            raise HeartbeatJournalError(
                f"the heartbeat journal at {self.database_path} is unusable "
                f"({self._initialisation_failure}); the beat was NOT recorded"
            )
        record = HeartbeatRecord(
            component=component.strip(),
            claimed_beat_at=moment_claimed,
            recorded_at=moment_recorded,
            process_id=process_id if process_id is not None else os.getpid(),
        )
        try:
            connection = self._connect()
            try:
                connection.execute(
                    f"INSERT OR REPLACE INTO trader_heartbeat ({_HEARTBEAT_COLUMNS}) "  # noqa: S608
                    "VALUES (?,?,?,?)",
                    (
                        record.component,
                        record.claimed_beat_at.astimezone(UTC).isoformat(),
                        record.recorded_at.astimezone(UTC).isoformat(),
                        record.process_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as failure:
            raise HeartbeatJournalError(
                f"the heartbeat for {component!r} could not be committed to {self.database_path} "
                f"({type(failure).__name__}: {failure})"
            ) from failure
        return record

    def recent_heartbeats(
        self, *, component: str, window: int = DEFAULT_HEARTBEAT_HISTORY_WINDOW
    ) -> tuple[HeartbeatRecord, ...]:
        """The last `window` beats, oldest first. **Never raises** — an unreadable journal is
        indistinguishable from a dead trader, and both must resolve to a halt rather than to a
        traceback inside the process whose job is to survive.
        """
        if self._initialisation_failure is not None:
            return ()
        try:
            connection = self._connect()
            try:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"SELECT {_HEARTBEAT_COLUMNS} FROM trader_heartbeat "  # noqa: S608
                    "WHERE component = ? ORDER BY recorded_at_utc DESC, claimed_beat_at_utc DESC "
                    "LIMIT ?",
                    (component.strip(), int(window)),
                ).fetchall()
            finally:
                connection.close()
            records = [_heartbeat_from_row(row) for row in rows]
        except (sqlite3.Error, OSError, ValueError):
            return ()
        return tuple(reversed(records))

    def latest_heartbeat(self, *, component: str) -> HeartbeatRecord | None:
        """The newest beat by the instant it can honestly vouch for, or `None`."""
        records = self.recent_heartbeats(component=component)
        if not records:
            return None
        return max(records, key=lambda record: record.effective_beat_at)


def heartbeat_intervals(records: Sequence[HeartbeatRecord]) -> tuple[timedelta, ...]:
    """Successive gaps between beats, on the sanitised timeline and strictly positive.

    Sorted on `effective_beat_at` rather than trusting insertion order, and zero or negative gaps
    are dropped: a duplicate or out-of-order beat is not evidence of a zero-latency deployment, and
    letting one into the estimator would drag the derived tolerance toward zero and manufacture a
    halt.
    """
    ordered = sorted(records, key=lambda record: record.effective_beat_at)
    gaps = [
        later.effective_beat_at - earlier.effective_beat_at
        for earlier, later in itertools.pairwise(ordered)
    ]
    return tuple(gap for gap in gaps if gap.total_seconds() > 0)


def derive_heartbeat_staleness_threshold(
    observed_intervals: Sequence[timedelta],
) -> HeartbeatToleranceEstimate:
    """Turn observed heartbeat intervals into the gap after which the trader is presumed dead.

    See the module docstring for the derivation. Nothing here is a typed threshold: a deployment
    that beats once a second and one that beats once a minute produce tolerances that differ by the
    same factor, without a line of configuration.
    """
    seconds = [interval.total_seconds() for interval in observed_intervals]
    positive = [value for value in seconds if value > 0]
    if len(positive) < _MINIMUM_INTERVALS_FOR_DERIVATION:
        raise InsufficientHeartbeatHistoryError(
            f"a staleness tolerance needs at least {_MINIMUM_INTERVALS_FOR_DERIVATION} positive "
            f"heartbeat interval to be derived from data, and {len(positive)} were supplied. "
            f"Inventing a constant here is exactly the R.03 defect this refusal exists to prevent; "
            f"the caller must treat 'no derivable tolerance' as 'no evidence of life'"
        )
    sample_size = len(positive)
    centre = statistics.median(positive)
    median_absolute_deviation = statistics.median([abs(value - centre) for value in positive])
    # With n observations the finest jitter the sample can support a claim about is centre/(n+1);
    # a zero MAD (a perfectly regular beat) would otherwise derive a zero-width tolerance and halt
    # on the first microsecond of scheduling noise.
    dispersion_floor = centre / (sample_size + 1)
    dispersion = max(
        median_absolute_deviation * _MEDIAN_ABSOLUTE_DEVIATION_TO_SIGMA, dispersion_floor
    )
    false_halt_probability_bound = 1.0 / (sample_size + 1)
    cantelli_multiplier = math.sqrt(
        (1.0 - false_halt_probability_bound) / false_halt_probability_bound
    )
    longest_observed = max(positive)
    tolerance_seconds = max(
        centre + cantelli_multiplier * dispersion,
        longest_observed + dispersion,
    )
    return HeartbeatToleranceEstimate(
        sample_size=sample_size,
        centre_interval=timedelta(seconds=centre),
        dispersion=timedelta(seconds=dispersion),
        longest_observed_interval=timedelta(seconds=longest_observed),
        false_halt_probability_bound=false_halt_probability_bound,
        cantelli_multiplier=cantelli_multiplier,
        staleness_tolerance=timedelta(seconds=tolerance_seconds),
    )


@dataclass(slots=True)
class TradingHaltWatchdog:
    """The supervisor. It can close the latch and it can never open it.

    Runs as its own systemd **user** unit (`deploy/nse-trading-halt-watchdog.service`); system units
    under `/home` are blocked by SELinux on this host, as the dashboard unit already records.
    """

    latch_store: TradingControlLatchStore
    heartbeat_journal: TraderHeartbeatJournal
    monitored_component: str
    reconciliation_probe: ReconciliationProbe | None = None
    heartbeat_history_window: int = DEFAULT_HEARTBEAT_HISTORY_WINDOW
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    watchdog_identity: str = "trading_halt_watchdog"

    # -- evidence ----------------------------------------------------------------------------

    def assess_heartbeat_freshness(self) -> HeartbeatFreshnessAssessment:
        """Read the heartbeat history and decide, against this process's own clock, whether the
        watched component is demonstrably alive."""
        assessed_at = self.clock()
        records = self.heartbeat_journal.recent_heartbeats(
            component=self.monitored_component, window=self.heartbeat_history_window
        )
        if not records:
            return HeartbeatFreshnessAssessment(
                component=self.monitored_component,
                assessed_at=assessed_at,
                verdict=HeartbeatVerdict.NO_EVIDENCE,
                latest_heartbeat=None,
                heartbeat_age=None,
                tolerance=None,
                detail=(
                    f"no heartbeat has ever been recorded for {self.monitored_component!r}; a "
                    f"trader that has not proved it is running is treated as not running"
                ),
            )
        implausible = [record for record in records if record.is_timestamp_implausible]
        latest = max(records, key=lambda record: record.effective_beat_at)
        age = assessed_at - latest.effective_beat_at
        if implausible:
            return HeartbeatFreshnessAssessment(
                component=self.monitored_component,
                assessed_at=assessed_at,
                verdict=HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP,
                latest_heartbeat=latest,
                heartbeat_age=age,
                tolerance=None,
                detail=(
                    f"{len(implausible)} of {len(records)} heartbeats claim a beat time later than "
                    f"the moment they were written (newest offender claims "
                    f"{implausible[-1].claimed_beat_at.astimezone(UTC).isoformat()} recorded at "
                    f"{implausible[-1].recorded_at.astimezone(UTC).isoformat()}); a clock running "
                    f"ahead of its own writes cannot be used to prove liveness"
                ),
            )
        if age.total_seconds() < 0:
            return HeartbeatFreshnessAssessment(
                component=self.monitored_component,
                assessed_at=assessed_at,
                verdict=HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP,
                latest_heartbeat=latest,
                heartbeat_age=age,
                tolerance=None,
                detail=(
                    f"the newest heartbeat is dated "
                    f"{latest.effective_beat_at.astimezone(UTC).isoformat()}, which is "
                    f"{-age.total_seconds():.3f}s in the FUTURE of this watchdog's clock; a beat "
                    f"from the future would otherwise read as infinitely fresh forever"
                ),
            )
        try:
            tolerance = derive_heartbeat_staleness_threshold(heartbeat_intervals(records))
        except InsufficientHeartbeatHistoryError as failure:
            return HeartbeatFreshnessAssessment(
                component=self.monitored_component,
                assessed_at=assessed_at,
                verdict=HeartbeatVerdict.TOLERANCE_UNDERIVABLE,
                latest_heartbeat=latest,
                heartbeat_age=age,
                tolerance=None,
                detail=(
                    f"{len(records)} heartbeat(s) recorded but no staleness tolerance can be "
                    f"derived from them ({failure}); with no derivable tolerance there is no "
                    f"evidence-based way to call the trader alive"
                ),
            )
        verdict = (
            HeartbeatVerdict.FRESH
            if age <= tolerance.staleness_tolerance
            else HeartbeatVerdict.STALE
        )
        return HeartbeatFreshnessAssessment(
            component=self.monitored_component,
            assessed_at=assessed_at,
            verdict=verdict,
            latest_heartbeat=latest,
            heartbeat_age=age,
            tolerance=tolerance,
            detail=(
                f"newest beat is {age.total_seconds():.3f}s old against a {tolerance.describe()}"
            ),
        )

    def run_reconciliation_probe(self) -> ReconciliationVerdict | None:
        """Ask the injected reconciler how it went. A probe that raises has FAILED.

        Swallowing the exception into "unknown" would let a reconciler broken by a deployment
        silently remove the second of this watchdog's two halt conditions.
        """
        if self.reconciliation_probe is None:
            return None
        try:
            return self.reconciliation_probe()
        except Exception as failure:  # noqa: BLE001 — any failure IS a failed reconciliation
            return ReconciliationVerdict(
                succeeded=False,
                checked_at=self.clock(),
                detail=(
                    f"the reconciliation probe raised {type(failure).__name__}: {failure}; a "
                    f"reconciliation that cannot run is a reconciliation that did not pass"
                ),
            )

    # -- acting ------------------------------------------------------------------------------

    def run_supervision_cycle(self) -> WatchdogSupervisionCycle:
        """One pass: gather evidence, and close the latch if any of it says to."""
        return self._supervise(is_startup_reconciliation=False)

    def reconcile_on_start(self) -> WatchdogSupervisionCycle:
        """The first pass after this watchdog's OWN start. Believes nothing it believed before.

        Identical evidence gathering to a normal cycle — which is the point. There is no "restore
        previous state" step anywhere in this module, because there is no previous state worth
        restoring: the latch on disk is a fact, the heartbeat history is a fact, and everything else
        was in the memory of a process that is gone.
        """
        return self._supervise(is_startup_reconciliation=True)

    def _supervise(self, *, is_startup_reconciliation: bool) -> WatchdogSupervisionCycle:
        observed_at = self.clock()
        disposition_before = self.latch_store.read_disposition()
        assessment = self.assess_heartbeat_freshness()
        reconciliation = self.run_reconciliation_probe()

        triggers: list[HaltTrigger] = []
        if assessment.verdict is HeartbeatVerdict.STALE:
            triggers.append(HaltTrigger.HEARTBEAT_STALE)
        elif assessment.verdict is HeartbeatVerdict.NO_EVIDENCE:
            triggers.append(HaltTrigger.HEARTBEAT_ABSENT)
        elif assessment.verdict is HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP:
            triggers.append(HaltTrigger.HEARTBEAT_TIMESTAMP_IMPLAUSIBLE)
        elif assessment.verdict is HeartbeatVerdict.TOLERANCE_UNDERIVABLE:
            triggers.append(HaltTrigger.HEARTBEAT_ABSENT)
        if reconciliation is not None and not reconciliation.succeeded:
            triggers.append(HaltTrigger.RECONCILIATION_FAILED)
        if (
            is_startup_reconciliation
            and self.reconciliation_probe is None
            and disposition_before.latch_state is LatchState.RELEASED
            and disposition_before.trading_mode is TradingMode.LIVE
        ):
            # A watchdog that restarts into an armed, LIVE, released system with no way to check
            # broker state cannot certify the position it inherited. It halts and says so.
            triggers.append(HaltTrigger.RECONCILIATION_UNAVAILABLE)

        disposition_after = disposition_before
        if triggers and disposition_before.latch_state is LatchState.RELEASED:
            disposition_after = self.latch_store.latch(
                latched_by=self.watchdog_identity,
                authority=ChangeAuthority.SYSTEM,
                reason=self._halt_reason(
                    triggers=tuple(triggers),
                    assessment=assessment,
                    reconciliation=reconciliation,
                    is_startup_reconciliation=is_startup_reconciliation,
                ),
                latched_at=observed_at,
            )
        return WatchdogSupervisionCycle(
            observed_at=observed_at,
            is_startup_reconciliation=is_startup_reconciliation,
            disposition_before=disposition_before,
            disposition_after=disposition_after,
            heartbeat_assessment=assessment,
            reconciliation_verdict=reconciliation,
            halt_triggers=tuple(triggers),
            next_poll_interval=self.derive_poll_interval(assessment),
        )

    def halt_on_operator_command(
        self, *, operator_name: str, reason: str
    ) -> WatchdogSupervisionCycle:
        """Stop trading because a human said so. No evidence required and none asked for."""
        observed_at = self.clock()
        disposition_before = self.latch_store.read_disposition()
        assessment = self.assess_heartbeat_freshness()
        disposition_after = self.latch_store.latch(
            latched_by=operator_name.strip() or "unnamed_operator",
            authority=ChangeAuthority.OPERATOR,
            reason=f"[{HaltTrigger.OPERATOR_COMMAND.value}] {reason.strip()}",
            latched_at=observed_at,
        )
        return WatchdogSupervisionCycle(
            observed_at=observed_at,
            is_startup_reconciliation=False,
            disposition_before=disposition_before,
            disposition_after=disposition_after,
            heartbeat_assessment=assessment,
            reconciliation_verdict=None,
            halt_triggers=(HaltTrigger.OPERATOR_COMMAND,),
            next_poll_interval=self.derive_poll_interval(assessment),
        )

    def release_after_operator_review(
        self, *, authorization: OperatorAuthorization, reason: str
    ) -> LatchDisposition:
        """Pass an operator's release through to the latch, adding no authority of its own.

        Present so an operator has one tool rather than two, and deliberately a thin pass-through:
        every check that matters lives in `TradingControlLatchStore.release_latch`, and a watchdog
        that could weaken them would be the hole in the design.
        """
        return self.latch_store.release_latch(authorization=authorization, reason=reason)

    def derive_poll_interval(self, assessment: HeartbeatFreshnessAssessment) -> timedelta:
        """How long until the next cycle, derived from the same estimate that judges staleness.

        Half the tolerance — the Nyquist argument: sampling any slower than twice the interval you
        are trying to detect means a stall can begin and the system can trade through the whole of
        it before anyone looks. Returns `None`-free: with no derivable tolerance the caller falls
        back to the operator's bootstrap interval, which is the only number in this feature a human
        supplies and the only one that stops mattering the moment history exists.
        """
        if assessment.tolerance is None:
            return timedelta(0)
        return assessment.tolerance.staleness_tolerance / 2

    def _halt_reason(
        self,
        *,
        triggers: tuple[HaltTrigger, ...],
        assessment: HeartbeatFreshnessAssessment,
        reconciliation: ReconciliationVerdict | None,
        is_startup_reconciliation: bool,
    ) -> str:
        parts = [
            "[" + "+".join(trigger.value for trigger in triggers) + "]",
            "startup reconciliation:" if is_startup_reconciliation else "supervision:",
            assessment.detail,
        ]
        if reconciliation is not None and not reconciliation.succeeded:
            parts.append(f"reconciliation: {reconciliation.detail}")
        if HaltTrigger.RECONCILIATION_UNAVAILABLE in triggers:
            parts.append(
                "no reconciliation probe is wired in, so an inherited LIVE released state cannot "
                "be certified against the broker"
            )
        return " ".join(parts)


def supervise_until_stopped(
    watchdog: TradingHaltWatchdog,
    *,
    stop_requested: Callable[[], bool],
    sleep: Callable[[float], None],
    bootstrap_poll_interval: timedelta,
    report: Callable[[WatchdogSupervisionCycle], None] | None = None,
) -> int:
    """The supervision loop, with its clock, its sleep and its stop condition all injected.

    A loop with `time.sleep` and `while True` baked in is a loop that can only be tested by waiting,
    and a watchdog whose own loop is untested is decoration. The entry point supplies the real
    implementations; the hermetic tests supply fakes and the loop is identical.

    Returns the number of cycles run, so a caller can assert on it.
    """
    cycles = 0
    cycle = watchdog.reconcile_on_start()
    cycles += 1
    if report is not None:
        report(cycle)
    while not stop_requested():
        interval = cycle.next_poll_interval
        if interval.total_seconds() <= 0:
            interval = bootstrap_poll_interval
        sleep(interval.total_seconds())
        if stop_requested():
            break
        cycle = watchdog.run_supervision_cycle()
        cycles += 1
        if report is not None:
            report(cycle)
    return cycles


def _heartbeat_from_row(row: sqlite3.Row) -> HeartbeatRecord:
    return HeartbeatRecord(
        component=str(row["component"]),
        claimed_beat_at=datetime.fromisoformat(str(row["claimed_beat_at_utc"])),
        recorded_at=datetime.fromisoformat(str(row["recorded_at_utc"])),
        process_id=int(row["process_id"]),
    )
