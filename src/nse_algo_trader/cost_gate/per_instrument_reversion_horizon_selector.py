"""How long to hold THIS deviation, chosen from the fitted grid rather than set once (`A.115`).

Every position in the paper loop used one policy horizon — five bars, for every instrument, every
deviation, every session. Two things follow from that, and the second is the one that cost money.

**It was not derived.** The calibration was fitted over four horizons (1, 3, 5 and 10 bars) and the
loop consulted exactly one of them, so three quarters of the measured evidence never reached a
decision. `R.03`: the holding time is a threshold like any other, and a threshold that is chosen
rather than measured is a defect.

**It synchronised the exits.** Positions opened at different instants but all expired exactly five
bars later, so they queued for the door together — and the real rate limiter refuses a wave. The
2026-08-15 gated replays measured it: **197 refusals across three sessions, every single one a
square-off, none an entry** (`docs/research/231`). A position whose exit is refused holds market
risk it did not intend for another five minutes, and it does so precisely when everything else
wants out too.

**What this selector does.** For one instrument's measured deviation it asks every horizon the
calibration actually holds: *what does this depth of deviation recover over that many bars, and how
sure is the evidence?* It scores each on **lower-confidence capture per bar** — the pessimistic end
of the edge claim, divided by the holding time it costs — and takes the best. Two standard errors
below the mean rather than the mean itself, because a horizon chosen on an optimistic reading is a
horizon that will disappoint exactly when the sample was thin. Per bar rather than per trade,
because holding ten bars for the same capture as three is worse: it occupies the book, and the
capacity it occupies is the thing the concentration cap is rationing (`A.107`).

**Dispersion is a consequence, not the goal.** Instruments differ in how far they have deviated, so
they land in different buckets, so they select different horizons — and the exit wave spreads out on
its own. Choosing a horizon at random would also disperse the exits and would be a worse decision;
this one has to be defensible on its own terms before the dispersion counts for anything.

**SOTA analog** (`R.23a`): the holding-period selection in Qlib's `TopkDropoutStrategy` and in
Zipline's `schedule_function` horizons — both let the holding period be a property of the signal
rather than a constant of the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    CalibrationCoverageError,
    ReversionCalibrationStore,
    ReversionCapture,
)


class NoHorizonHasEvidenceError(Exception):
    """No fitted horizon covers this deviation, so there is no defensible holding time.

    Refused rather than defaulted. Falling back to a policy horizon here would put the exact
    unfitted number back into the decision that this selector exists to remove, and it would do it
    silently, on precisely the instruments whose deviations the calibration has never seen.
    """


@dataclass(frozen=True, slots=True)
class SelectedHorizon:
    """The chosen holding time, and every horizon that was considered against it."""

    horizon_bars: int
    capture: ReversionCapture
    score_bps_per_bar: Decimal
    considered: tuple[tuple[int, Decimal], ...]
    """`(horizon_bars, score)` for every horizon with evidence, ascending by horizon.

    Carried because a holding time an operator cannot reconstruct is one they cannot argue with —
    the same reason `SizedPosition` carries its intermediates (`L13.29`)."""

    @property
    def is_distinguishable_from_zero(self) -> bool:
        """Whether the winning horizon's own capture clears its standard error twice over."""
        return self.capture.is_distinguishable_from_zero

    def describe(self) -> str:
        alternatives = ", ".join(
            f"{horizon}b={score.quantize(Decimal('0.01'))}" for horizon, score in self.considered
        )
        return (
            f"{self.horizon_bars} bars at {self.score_bps_per_bar.quantize(Decimal('0.01'))} "
            f"bps/bar (lower-confidence), chosen from [{alternatives}]"
        )


@dataclass(frozen=True, slots=True)
class PerInstrumentReversionHorizonSelector:
    """Picks a holding time for one deviation, from the horizons the calibration was fitted on."""

    calibrations: ReversionCalibrationStore

    def horizon_for(
        self, *, deviation_sigma: Decimal, trading_symbol: str, as_of: date
    ) -> SelectedHorizon:
        """The best-evidenced holding time for this deviation, or a refusal naming what was tried.

        Args:
            deviation_sigma: how far this instrument sits from its own mean, in its own sigma.
            trading_symbol: consulted first on the `R.04` ladder, then the pooled universe.
            as_of: only calibrations fitted BEFORE this date are eligible — the same
                point-in-time rule `capture_for` applies, applied to the choice of horizon too.

        Raises:
            NoHorizonHasEvidenceError: no fitted horizon covers this deviation.
        """
        horizons = self.calibrations.calibrated_horizons(as_of=as_of)
        if not horizons:
            raise NoHorizonHasEvidenceError(
                f"the calibration store holds no horizon fitted before {as_of.isoformat()}, so "
                "there is no evidenced holding time for any instrument"
            )

        scored: list[tuple[int, Decimal, ReversionCapture]] = []
        missed: list[str] = []
        for horizon in horizons:
            try:
                capture = self.calibrations.capture_for(
                    deviation_sigma=deviation_sigma,
                    horizon_bars=horizon,
                    as_of=as_of,
                    trading_symbol=trading_symbol,
                )
            except CalibrationCoverageError:
                missed.append(f"{horizon}b")
                continue
            # Lower-confidence capture, per bar held. The numerator is the pessimistic end of the
            # claim; the denominator is what the claim costs in book capacity.
            scored.append((horizon, capture.lower_confidence_bps / Decimal(horizon), capture))

        if not scored:
            raise NoHorizonHasEvidenceError(
                f"no fitted horizon covers a {deviation_sigma} sigma deviation for "
                f"{trading_symbol} as of {as_of.isoformat()}; tried {', '.join(missed)}"
            )

        # Highest score wins; the SHORTER horizon breaks a tie, because holding longer for the same
        # measured capture buys nothing and occupies capacity the concentration cap is rationing.
        best_horizon, best_score, best_capture = max(scored, key=lambda row: (row[1], -row[0]))
        return SelectedHorizon(
            horizon_bars=best_horizon,
            capture=best_capture,
            score_bps_per_bar=best_score,
            considered=tuple((horizon, score) for horizon, score, _ in scored),
        )
