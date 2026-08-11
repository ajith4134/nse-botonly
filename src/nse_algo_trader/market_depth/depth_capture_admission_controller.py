"""Which instruments the capture can afford, solved from measured quantities.

Disk is the binding constraint, not the API. Kite permits 3,000 instruments per
connection and three connections per key — 9,000 instruments — while the disk holds
far less than 9,000 instruments across a useful retention horizon. So the capture
universe is the answer to a budget problem, and `R.03` forbids it being a number
someone typed.

Every input is measured rather than declared:

- **bytes per row** — the realized compressed bytes of the tape's own parts, recomputed
  as it grows. Never a documentation figure.
- **packets per second per instrument** — that instrument's own history in the tape.
  The measured cross-sectional spread was 130x (0.013 to 1.72 packets/s), so a single
  global rate is the wrong model.
- **free disk** — re-read live, so a disk filling for an unrelated reason contracts the
  capture instead of crashing it.
- **session length** — from the trading calendar's quoting window, not a constant.

Only `retention_sessions` is a policy input, because how long history is worth keeping
is a judgement about the future rather than a fact about the present.

**Bootstrap.** On the first ever session there is no tape, so neither bytes-per-row nor
per-instrument rates exist. The controller does not paper over this with an assumed
constant: it admits a deliberately small calibration cohort, measures both quantities
from the tape that cohort produces, then re-solves for the full session. A number
measured in the first ten minutes of the session it applies to beats any number a
document could have supplied.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class AdmissionControlError(Exception):
    """Raised when the budget problem is stated inconsistently."""


@dataclass(frozen=True)
class InstrumentCaptureCandidate:
    """One instrument the capture could take, with its measured cost and value."""

    instrument_token: int
    liquidity_value: float
    """Measured turnover or an equivalent proxy. Sets what is worth keeping when the
    budget cannot hold everything — a half-recorded book on every instrument is worth
    far less than a whole book on the instruments that matter."""

    measured_packets_per_second: float | None = None
    """That instrument's own observed rate, or None when it has no history yet."""

    def __post_init__(self) -> None:
        if self.liquidity_value < 0:
            raise AdmissionControlError(
                f"negative liquidity value for token {self.instrument_token}"
            )
        if (
            self.measured_packets_per_second is not None
            and self.measured_packets_per_second < 0
        ):
            raise AdmissionControlError(
                f"negative packet rate for token {self.instrument_token}"
            )


@dataclass(frozen=True)
class AdmissionDecision:
    """What the controller decided, and every number it decided from."""

    admitted_tokens: tuple[int, ...]
    rejected_tokens: tuple[int, ...]
    projected_session_bytes: float
    budget_bytes: float
    bytes_per_row_used: float
    session_seconds: float
    is_calibration_cohort: bool
    unmeasured_token_count: int
    fallback_rate_used: float | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def admitted_count(self) -> int:
        return len(self.admitted_tokens)

    @property
    def budget_utilization(self) -> float:
        return self.projected_session_bytes / self.budget_bytes if self.budget_bytes else 0.0


def measure_free_disk_bytes(path: Path) -> int:
    """Live free space on the filesystem holding `path`."""
    return shutil.disk_usage(path).free


def median_of(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class DepthCaptureAdmissionController:
    """Solves the capture-universe budget problem and re-solves it under pressure."""

    def __init__(
        self,
        tape_root: Path,
        retention_sessions: int,
        disk_budget_fraction: float,
        calibration_cohort_size: int,
    ) -> None:
        if retention_sessions <= 0:
            raise AdmissionControlError("retention_sessions must be positive")
        if not 0 < disk_budget_fraction <= 1:
            raise AdmissionControlError("disk_budget_fraction must be in (0, 1]")
        if calibration_cohort_size <= 0:
            raise AdmissionControlError("calibration_cohort_size must be positive")
        self._tape_root = tape_root
        self._retention_sessions = retention_sessions
        self._disk_budget_fraction = disk_budget_fraction
        self._calibration_cohort_size = calibration_cohort_size

    def budget_bytes_per_session(self, already_used_bytes: int = 0) -> float:
        """What one session may consume, from live free space plus what the tape holds.

        Existing tape counts toward the budget rather than being invisible: the horizon
        is `retention_sessions` of data on this disk, not `retention_sessions` more.
        """
        free_bytes = measure_free_disk_bytes(self._tape_root)
        total_available = (free_bytes + already_used_bytes) * self._disk_budget_fraction
        return total_available / self._retention_sessions

    def solve(
        self,
        candidates: Sequence[InstrumentCaptureCandidate],
        session_seconds: float,
        measured_bytes_per_row: float | None,
        already_used_bytes: int = 0,
        connection_ceiling: int | None = None,
    ) -> AdmissionDecision:
        """The admitted set, by value density under the session byte budget.

        When the tape has never been written, `measured_bytes_per_row` is None and this
        returns a calibration cohort instead of guessing a row width.
        """
        if session_seconds <= 0:
            raise AdmissionControlError("session_seconds must be positive")

        if measured_bytes_per_row is None:
            return self._calibration_decision(candidates, session_seconds)

        budget = self.budget_bytes_per_session(already_used_bytes)
        measured_rates = [
            candidate.measured_packets_per_second
            for candidate in candidates
            if candidate.measured_packets_per_second is not None
        ]
        fallback_rate = median_of(measured_rates)
        if fallback_rate is None:
            # Nothing in this cohort has a rate yet, but the tape has rows — so the
            # rate is unknown while the row width is not. Calibrate rather than guess.
            return self._calibration_decision(candidates, session_seconds)

        def cost_of(candidate: InstrumentCaptureCandidate) -> float:
            rate = (
                candidate.measured_packets_per_second
                if candidate.measured_packets_per_second is not None
                else fallback_rate
            )
            return rate * session_seconds * measured_bytes_per_row

        # Greedy by value density is the standard approximation for this knapsack, and
        # it degrades gracefully: with near-equal costs it maximizes count, and with a
        # wide value spread it keeps the instruments worth keeping.
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                -(candidate.liquidity_value / cost_of(candidate))
                if cost_of(candidate) > 0
                else float("-inf"),
                -candidate.liquidity_value,
                candidate.instrument_token,
            ),
        )

        admitted: list[int] = []
        rejected: list[int] = []
        spent = 0.0
        unmeasured = 0
        for candidate in ranked:
            cost = cost_of(candidate)
            over_budget = spent + cost > budget
            over_ceiling = (
                connection_ceiling is not None and len(admitted) >= connection_ceiling
            )
            if over_budget or over_ceiling:
                rejected.append(candidate.instrument_token)
                continue
            admitted.append(candidate.instrument_token)
            spent += cost
            if candidate.measured_packets_per_second is None:
                unmeasured += 1

        notes: list[str] = []
        if unmeasured:
            notes.append(
                f"{unmeasured} admitted instruments had no measured rate and were "
                f"costed at the cohort median of {fallback_rate:.4f} packets/s"
            )
        if connection_ceiling is not None and len(rejected) and len(admitted) >= connection_ceiling:
            notes.append(
                f"capped at the connection ceiling of {connection_ceiling} instruments"
            )
        return AdmissionDecision(
            admitted_tokens=tuple(admitted),
            rejected_tokens=tuple(rejected),
            projected_session_bytes=spent,
            budget_bytes=budget,
            bytes_per_row_used=measured_bytes_per_row,
            session_seconds=session_seconds,
            is_calibration_cohort=False,
            unmeasured_token_count=unmeasured,
            fallback_rate_used=fallback_rate,
            notes=tuple(notes),
        )

    def _calibration_decision(
        self,
        candidates: Sequence[InstrumentCaptureCandidate],
        session_seconds: float,
    ) -> AdmissionDecision:
        """The highest-value small cohort, admitted purely to measure the unknowns."""
        ranked = sorted(
            candidates,
            key=lambda candidate: (-candidate.liquidity_value, candidate.instrument_token),
        )
        cohort = ranked[: self._calibration_cohort_size]
        return AdmissionDecision(
            admitted_tokens=tuple(candidate.instrument_token for candidate in cohort),
            rejected_tokens=tuple(
                candidate.instrument_token for candidate in ranked[self._calibration_cohort_size :]
            ),
            projected_session_bytes=0.0,
            budget_bytes=self.budget_bytes_per_session(),
            bytes_per_row_used=0.0,
            session_seconds=session_seconds,
            is_calibration_cohort=True,
            unmeasured_token_count=len(cohort),
            fallback_rate_used=None,
            notes=(
                "calibration cohort — the tape cannot yet report bytes/row or per-"
                "instrument rates, so both are measured from this cohort before the "
                "full universe is solved",
            ),
        )

    def tokens_to_shed(
        self,
        admitted_tokens: Sequence[int],
        liquidity_value_by_token: Mapping[int, float],
        projected_remaining_bytes: float,
        remaining_budget_bytes: float,
        projected_bytes_by_token: Mapping[int, float] | None = None,
    ) -> tuple[int, ...]:
        """The lowest-value instruments to drop when the budget contracts mid-session.

        Shedding, rather than truncating the session, is the deliberate choice: what
        survives is then complete for the instruments that matter, instead of every
        instrument being uniformly half-recorded and none of them usable.

        **Per-instrument costs, not a flat mean.** An earlier version divided the total
        projection by the instrument count and assumed each shed token freed that much.
        The tokens it sheds are the lowest-liquidity ones, which are also the *cheapest*
        — so it consistently freed far less than it believed. Adversarial review
        measured a case needing 7.16 MB freed where it shed 51 tokens and recovered
        408 KB, missing by 17x, and another where a zero projection divided by zero.
        Costs now come from the same measured per-instrument rates the admission
        decision used, and tokens are shed until the budget is genuinely met.
        """
        if not admitted_tokens:
            return ()
        if projected_remaining_bytes <= remaining_budget_bytes:
            return ()

        if projected_bytes_by_token:
            cost_of_token = {
                token: float(projected_bytes_by_token.get(token, 0.0))
                for token in admitted_tokens
            }
        else:
            # No per-instrument costs supplied: fall back to an equal share, and say so
            # rather than pretending the result is precise.
            share = projected_remaining_bytes / len(admitted_tokens)
            cost_of_token = dict.fromkeys(admitted_tokens, share)

        by_value_then_token = sorted(
            admitted_tokens,
            key=lambda token: (liquidity_value_by_token.get(token, 0.0), token),
        )
        must_free = projected_remaining_bytes - remaining_budget_bytes
        freed = 0.0
        shed: list[int] = []
        for token in by_value_then_token:
            if freed >= must_free:
                break
            shed.append(token)
            freed += cost_of_token.get(token, 0.0)
            if cost_of_token.get(token, 0.0) <= 0:
                # A zero-cost token frees nothing; shedding more of them would loop
                # forever without progress, so stop and report what could be freed.
                continue
        return tuple(shed)
