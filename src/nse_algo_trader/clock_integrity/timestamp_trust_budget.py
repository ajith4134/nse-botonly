"""The decision: how wrong this host's timestamps can be, and what that forbids.

Everything upstream of this module measures. This is the part that *changes what the system
does*: it turns the envelope fit, the reference consensus and the alert history into a
single worst-case error and a verdict other engines act on.

**The two arms answer different questions and are combined, not averaged.**

- The reference consensus (`host - UTC`, bracketed by NTP round trips) is the host's own
  error. It is rigorous in both directions and it is what timestamp CORRECTION uses.
- The feed envelope (`host - exchange`, an upper bound only) contains the host error plus
  the exchange's own error plus the minimum network delay. Subtracting the reference from
  it therefore isolates *the part that is not this host's fault* — the feed floor. A feed
  floor that grows while the reference stays flat is a route change; a reference that moves
  while the feed floor holds is this machine's clock. Reporting one number for both would
  make those two indistinguishable, which is the whole failure this engine exists to avoid.

**The verdict ladder is derived, and it is a maturity ladder (`R.04`).** Thresholds are
quantiles of this host's OWN history — not constants — and before there is history the
verdict is `IMMATURE`, which reads as "do not rely on this", never as "fine". The engine
never becomes permissive by default and never silently graduates itself.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import numpy as np

from nse_algo_trader.clock_integrity.clock_drift_change_detector import (
    MINIMUM_POINTS_TO_JUDGE_A_SERIES,
)
from nse_algo_trader.clock_integrity.clock_offset_observation_store import (
    ClockDriftAlert,
    ClockOffsetObservationStore,
)
from nse_algo_trader.clock_integrity.exchange_clock_offset_estimator import ClockOffsetFit
from nse_algo_trader.clock_integrity.reference_clock_ntp_sampler import (
    ReferenceClockConsensus,
)

DEGRADED_HISTORY_QUANTILE = 0.90
REFUSE_HISTORY_QUANTILE = 0.99
"""Where on this host's own record of itself the two lines fall. Quantiles are a definition
of "unusual for this machine", which is what the verdict is trying to express; a millisecond
figure would be a guess about a machine nobody has measured."""

MINIMUM_SESSIONS_FOR_A_DERIVED_THRESHOLD = 3
"""Below this there is no distribution to take a quantile of. Not a tuning knob — it is the
smallest number of sessions for which "the 90th percentile of history" is a sentence that
means anything."""


class TrustVerdict(Enum):
    """What the rest of the system is allowed to conclude from a host timestamp."""

    TRUSTED = "trusted"
    DEGRADED = "degraded"
    REFUSE = "refuse"
    IMMATURE = "immature"


@dataclass(frozen=True, slots=True)
class TimestampTrustAssessment:
    """The verdict, the number behind it, and the reason in words."""

    verdict: TrustVerdict
    worst_case_error_seconds: float
    host_error_seconds: float
    host_error_half_width_seconds: float
    feed_floor_seconds: float
    skew_ppm: float
    reason: str
    degraded_above_seconds: float | None
    refuse_above_seconds: float | None
    assessed_at: datetime

    @property
    def is_actionable(self) -> bool:
        """`TRUSTED` or `DEGRADED`: a caller may proceed, knowing the budget."""
        return self.verdict in {TrustVerdict.TRUSTED, TrustVerdict.DEGRADED}


REFERENCE_BRACKET_USABLE_FOR = timedelta(hours=12)
"""How long a bracket may be applied as a correction.

Derived from what it measures rather than chosen: at this host's own worst measured skew
(16 ppm) a 12-hour-old bracket has decayed by 0.7 ms, which is below the bracket's own
half-width. Adversarial review found the previous version applying a bracket of any age,
including a month-old one, as though it were today's."""


@dataclass(frozen=True, slots=True)
class HostClockErrorCorrection:
    """The correction to apply, and whether anything actually measured it."""

    seconds: float
    is_measured: bool
    sampled_at: datetime | None
    reason: str


def measured_host_clock_error(
    store: ClockOffsetObservationStore | None = None, *, now: datetime | None = None
) -> HostClockErrorCorrection:
    """This host's last USABLE bracketed clock error, with its provenance attached.

    Returning a bare `0.0` made "measured as zero" and "never measured" the same value —
    the exact ambiguity this project refuses everywhere else. The caller gets both the
    number and whether it means anything.
    """
    at = now or datetime.now(UTC)
    consensus = (store or ClockOffsetObservationStore()).latest_reference_consensus()
    if consensus is None:
        return HostClockErrorCorrection(0.0, False, None, "no reference bracket has been taken")
    age = at - consensus.sampled_at
    if age > REFERENCE_BRACKET_USABLE_FOR:
        return HostClockErrorCorrection(
            0.0,
            False,
            consensus.sampled_at,
            f"the last bracket is {age.total_seconds() / 3600:.1f}h old, beyond the "
            f"{REFERENCE_BRACKET_USABLE_FOR.total_seconds() / 3600:.0f}h it stays usable for",
        )
    return HostClockErrorCorrection(
        consensus.midpoint_seconds,
        True,
        consensus.sampled_at,
        f"bracketed by {', '.join(consensus.agreeing_servers)}",
    )


def measured_host_clock_error_seconds(
    store: ClockOffsetObservationStore | None = None, *, now: datetime | None = None
) -> float:
    """The correction alone, for callers that take a plain float."""
    return measured_host_clock_error(store, now=now).seconds


class TimestampTrustBudget:
    """Combines the two measurement arms into one decision, with derived thresholds."""

    def __init__(
        self,
        store: ClockOffsetObservationStore,
        *,
        degraded_quantile: float = DEGRADED_HISTORY_QUANTILE,
        refuse_quantile: float = REFUSE_HISTORY_QUANTILE,
    ) -> None:
        self._store = store
        self._degraded_quantile = degraded_quantile
        self._refuse_quantile = refuse_quantile

    # -- the decision ------------------------------------------------------------------

    def assess(
        self,
        *,
        fit: ClockOffsetFit | None,
        consensus: ReferenceClockConsensus | None,
        alerts: Sequence[ClockDriftAlert] = (),
        at: datetime,
        drift_series_points: int | None = None,
    ) -> TimestampTrustAssessment:
        """One verdict for `at`, from whatever evidence exists — and honest when none does."""
        host_error, host_half_width = self._host_error(consensus)
        feed_floor = (
            fit.apparent_offset_seconds - host_error if fit is not None else math.nan
        )
        skew_ppm = fit.skew_ppm if fit is not None else math.nan
        worst_case = self._worst_case_error(fit, consensus, at)
        degraded_above, refuse_above = self.derived_thresholds(before=at)

        if consensus is None and fit is None:
            return self._assessment(
                TrustVerdict.IMMATURE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                "no reference sample and no session fit: nothing has been measured",
                degraded_above,
                refuse_above,
                at,
            )
        if fit is not None and not math.isfinite(fit.split_half_offset_disagreement_seconds):
            return self._assessment(
                TrustVerdict.IMMATURE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                "the session's two halves do not agree on a line, so the fit is not a fit",
                degraded_above,
                refuse_above,
                at,
            )
        if consensus is None:
            # The feed fit alone bounds `host - exchange` from above and cannot separate a
            # host clock error from a slow route. Enough to keep working with, not enough to
            # correct a timestamp by — so the caller may proceed knowing the budget, and
            # `corrected()` remains unavailable because it needs the bracket.
            return self._assessment(
                TrustVerdict.DEGRADED,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                "no reference bracket (no NTP majority): the feed fit bounds the offset from "
                "above only, so host error and network delay stay entangled",
                degraded_above,
                refuse_above,
                at,
            )
        if fit is not None and not fit.skew_is_physically_plausible:
            return self._assessment(
                TrustVerdict.IMMATURE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"the fitted rate is {fit.skew_ppm:+.0f} ppm, which no crystal oscillator "
                f"reaches — the line is describing the network's delay floor, not a clock",
                degraded_above,
                refuse_above,
                at,
            )
        if fit is not None and feed_floor < 0.0:
            # Checked BEFORE the falseticker branch, because adversarial review found that
            # ordering hid it: identical impossible numbers read REFUSE with a clean NTP
            # round and DEGRADED when one server happened to be excluded.
            return self._assessment(
                TrustVerdict.REFUSE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"packets appear to arrive {abs(feed_floor):.3f}s before the exchange stamped "
                f"them once the host's own error is removed — impossible, so one of the two "
                f"clocks is wrong by more than this engine can attribute",
                degraded_above,
                refuse_above,
                at,
            )
        if (
            drift_series_points is not None
            and drift_series_points < MINIMUM_POINTS_TO_JUDGE_A_SERIES
        ):
            # "No alerts" from a series too short to detect anything is not evidence of a
            # steady clock, and the previous version let it read as one.
            return self._assessment(
                TrustVerdict.IMMATURE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"only {drift_series_points} usable floor measurements this session — too "
                f"few for a change detector to say anything, so silence is not evidence",
                degraded_above,
                refuse_above,
                at,
            )
        if consensus.falsetickers:
            # A rejected falseticker is NTP working as designed, not a failure: the majority
            # still brackets the clock. It is recorded as DEGRADED rather than TRUSTED
            # because a server drifting out of consensus is worth a human's attention, and
            # `marzullo_intersection` already refuses outright when no majority exists.
            return self._assessment(
                TrustVerdict.DEGRADED,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"reference majority holds, but {', '.join(consensus.falsetickers)} sits "
                f"outside it and was discarded as a falseticker",
                degraded_above,
                refuse_above,
                at,
            )
        if degraded_above is None or refuse_above is None:
            return self._assessment(
                TrustVerdict.IMMATURE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"fewer than {MINIMUM_SESSIONS_FOR_A_DERIVED_THRESHOLD} recorded sessions, so "
                f"there is no distribution to take a threshold from",
                degraded_above,
                refuse_above,
                at,
            )
        if worst_case >= refuse_above or any(
            alert.detector == "page_hinkley" for alert in alerts
        ):
            reason = (
                f"worst-case timestamp error {worst_case * 1000:.1f}ms is at or beyond this "
                f"host's {self._refuse_quantile:.0%} historical level "
                f"({refuse_above * 1000:.1f}ms)"
                if worst_case >= refuse_above
                else "a Page-Hinkley change point says the clock is sliding, not merely noisy"
            )
            return self._assessment(
                TrustVerdict.REFUSE,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                reason,
                degraded_above,
                refuse_above,
                at,
            )
        if worst_case >= degraded_above or alerts:
            return self._assessment(
                TrustVerdict.DEGRADED,
                worst_case,
                host_error,
                host_half_width,
                feed_floor,
                skew_ppm,
                f"worst-case error {worst_case * 1000:.1f}ms is above this host's "
                f"{self._degraded_quantile:.0%} level ({degraded_above * 1000:.1f}ms)"
                if worst_case >= degraded_above
                else f"{len(alerts)} change point(s) detected this session",
                degraded_above,
                refuse_above,
                at,
            )
        return self._assessment(
            TrustVerdict.TRUSTED,
            worst_case,
            host_error,
            host_half_width,
            feed_floor,
            skew_ppm,
            f"worst-case error {worst_case * 1000:.1f}ms is within this host's usual range",
            degraded_above,
            refuse_above,
            at,
        )

    def corrected(self, host_instant: datetime, *, consensus: ReferenceClockConsensus) -> datetime:
        """A host stamp moved onto the reference timeline.

        Uses the NTP consensus alone, deliberately: it is the only bracketed measurement of
        this host's own error. The feed envelope's offset contains the exchange's error and
        the network delay too, so correcting with it would import both into every timestamp.
        """
        return host_instant - timedelta(seconds=consensus.midpoint_seconds)

    def corrected_staleness_seconds(
        self, raw_staleness_seconds: float, *, consensus: ReferenceClockConsensus
    ) -> float:
        """`receipt - exchange` with this host's clock error removed.

        This is the number `DepthPacketIntegrityClassifier` should be flagging outliers on:
        on a host running 300 ms fast, every packet looks 300 ms staler than it is, and an
        instrument's derived threshold quietly absorbs the offset instead of exposing it.
        """
        return raw_staleness_seconds - consensus.midpoint_seconds

    # -- derived thresholds --------------------------------------------------------------

    def derived_thresholds(
        self, *, before: datetime | None = None
    ) -> tuple[float | None, float | None]:
        """`(degraded_above, refuse_above)` from this host's own PAST verdicts.

        Two corrections from adversarial review live in this method. The history is the
        series of worst-case errors this engine actually judged — the same quantity the
        live comparison produces — rather than a second formula over the stored fits, which
        put the REFUSE line 15x above anything the live path could reach. And it is read
        strictly BEFORE the assessment being made, so a bad session cannot raise its own
        bar (measured: a 50-second-offset session moved its own line from 0.46s to 48.7s).
        """
        history = [
            value
            for value in self._store.worst_case_history_seconds(before=before)
            if math.isfinite(value)
        ]
        if len(history) < MINIMUM_SESSIONS_FOR_A_DERIVED_THRESHOLD:
            return None, None
        values = np.array(history, dtype=float)
        return (
            float(np.quantile(values, self._degraded_quantile)),
            float(np.quantile(values, self._refuse_quantile)),
        )

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _host_error(consensus: ReferenceClockConsensus | None) -> tuple[float, float]:
        if consensus is None:
            return math.nan, math.nan
        return consensus.midpoint_seconds, consensus.half_width_seconds

    def _worst_case_error(
        self,
        fit: ClockOffsetFit | None,
        consensus: ReferenceClockConsensus | None,
        at: datetime,
    ) -> float:
        """The error budget: host bracket + projected skew + the fit's own disagreement.

        Every term is an upper bound on a *different* unknown, so they add rather than
        combine in quadrature — this is a budget, not a variance.
        """
        budget = 0.0
        if consensus is not None:
            budget += abs(consensus.midpoint_seconds) + consensus.half_width_seconds
        if fit is not None:
            elapsed = abs((at - fit.fitted_to).total_seconds())
            budget += abs(fit.skew_ppm) * 1e-6 * elapsed
            if math.isfinite(fit.split_half_offset_disagreement_seconds):
                budget += fit.split_half_offset_disagreement_seconds
        return budget

    @staticmethod
    def _assessment(
        verdict: TrustVerdict,
        worst_case: float,
        host_error: float,
        host_half_width: float,
        feed_floor: float,
        skew_ppm: float,
        reason: str,
        degraded_above: float | None,
        refuse_above: float | None,
        at: datetime,
    ) -> TimestampTrustAssessment:
        return TimestampTrustAssessment(
            verdict=verdict,
            worst_case_error_seconds=worst_case,
            host_error_seconds=host_error,
            host_error_half_width_seconds=host_half_width,
            feed_floor_seconds=feed_floor,
            skew_ppm=skew_ppm,
            reason=reason,
            degraded_above_seconds=degraded_above,
            refuse_above_seconds=refuse_above,
            assessed_at=at,
        )
