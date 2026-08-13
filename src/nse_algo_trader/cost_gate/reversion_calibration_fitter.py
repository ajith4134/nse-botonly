"""Fits the reversion calibration from the deep-history archive, and re-fits it nightly.

`mean_reversion_edge_calibrator` defines what a calibration IS and refuses to price without one.
This is what produces them: it walks the archive, replays the entry rule bar by bar, and reduces
what it finds to one row per deviation bucket per horizon.

**Why this is a nightly job and not a one-off.** The calibration is a claim about the current
market, and the evidence for it decays. A coefficient fitted in a quiet range regime prices trades
in a trending one at exactly the moment it is most wrong. Re-fitting rolls the window forward and,
more importantly, makes the coefficient's drift VISIBLE — a bucket whose mean capture halves
between fits is telling the operator something no static number ever could.

**What the fit deliberately does not do: choose the strategy's horizon.** It fits every horizon
asked for and stores them side by side, because the horizon belongs to the position-management
rule, not to the calibration. Picking one here would be the same mistake the old edge formula made
— hiding a policy choice inside a measurement.

**Instrument-level fits are attempted and usually refused, on purpose.** A single symbol rarely
produces the events a heavy-tailed mean needs, so most instruments fall back to the pooled bucket
estimate through the `R.04` ladder. The attempt still runs, because the ladder can only promote an
instrument that is being measured, and refusing to measure until there is enough evidence is a
trap that never opens.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    MINIMUM_EVENTS_FOR_A_CALIBRATION,
    CalibrationError,
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionCapture,
    ReversionEvent,
    calibrate_from_events,
    deviation_bucket_of,
    measure_reversion_events,
    shrink_toward_pooled,
)
from nse_algo_trader.deep_history.deep_history_archive_loader import DeepHistoryArchiveLoader
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    DEVIATION_BAND_QUANTILE,
    MINIMUM_OBSERVATIONS_FOR_BANDS,
)

DEFAULT_CALIBRATION_HORIZONS = (1, 3, 5, 10)
"""Horizons fitted, in bars.

Spans same-day exit through a two-week hold because the measurement showed capture is strongly
horizon-dependent — 34 bps at one bar against 40 bps at five — and a system that fitted only one
would have no way to notice it had chosen badly."""

MINIMUM_HISTORY_BARS_TO_FIT = 400
"""An instrument with less history cannot supply a causal band AND an out-of-sample outcome.

The entry band needs its own warm-up before the first event can be recorded, and the outcome needs
bars after it, so short series contribute nothing and are skipped rather than partially used."""


@dataclass(frozen=True, slots=True)
class CalibrationFitReport:
    """What one nightly fit actually did — including what it refused to do.

    The refusals are the load-bearing part. A fit that silently produced fewer rows than expected
    looks identical to a healthy one from the row count alone, so the counts that explain the
    shortfall are carried alongside it.
    """

    fitted_through: date
    symbols_examined: int
    symbols_skipped_for_short_history: int
    events_measured: int
    pooled_calibrations: int
    bucket_calibrations: int
    instrument_calibrations: int
    buckets_below_evidence_threshold: tuple[Decimal, ...]

    @property
    def calibrations_written(self) -> int:
        return (
            self.pooled_calibrations + self.bucket_calibrations + self.instrument_calibrations
        )

    def describe(self) -> str:
        return (
            f"fitted through {self.fitted_through}: {self.events_measured} events from "
            f"{self.symbols_examined} symbols produced {self.calibrations_written} calibrations "
            f"({self.bucket_calibrations} bucket, {self.instrument_calibrations} instrument); "
            f"{self.symbols_skipped_for_short_history} symbols skipped for short history; "
            f"{len(self.buckets_below_evidence_threshold)} buckets held below the "
            f"{MINIMUM_EVENTS_FOR_A_CALIBRATION}-event threshold"
        )


def fit_reversion_calibrations(
    archive: DeepHistoryArchiveLoader,
    store: ReversionCalibrationStore,
    *,
    symbols: Sequence[str],
    fitted_through: date,
    horizons: Sequence[int] = DEFAULT_CALIBRATION_HORIZONS,
    rolling_window: int = 20,
    minimum_history_bars: int = MINIMUM_HISTORY_BARS_TO_FIT,
    maximum_bars_per_symbol: int = 1500,
) -> CalibrationFitReport:
    """Measure reversion across `symbols`; write the calibrations that clear the evidence bar.

    `fitted_through` is the exclusive upper bound on the evidence: every event used must predate
    it, enforced in `calibrate_from_events` rather than trusted here. A caller that passes today's
    date and an archive containing today's bar gets an error rather than a look-ahead.

    `rolling_window` must equal the live engine's. It is a parameter rather than an import because
    the engine's window is itself an argument there, and a calibration silently fitted to a
    different window would be measuring a different strategy while looking perfectly healthy.
    """
    if not symbols:
        raise CalibrationError("no symbols to fit; a calibration over nothing is not a calibration")
    if any(horizon <= 0 for horizon in horizons):
        raise CalibrationError(f"horizons must all be positive, got {tuple(horizons)}")

    events_by_horizon_and_bucket: dict[tuple[int, Decimal], list[ReversionEvent]] = defaultdict(
        list
    )
    events_by_symbol: dict[tuple[str, int, Decimal], list[ReversionEvent]] = defaultdict(list)
    examined = 0
    skipped_short = 0
    total_events = 0

    for symbol in symbols:
        series = [(day, float(close)) for day, close in archive.close_series(symbol) if close > 0]
        if len(series) < minimum_history_bars:
            skipped_short += 1
            continue
        series = series[-maximum_bars_per_symbol:]
        # Only evidence strictly before the declared fit boundary may inform the fit. Trimming
        # here as well as asserting downstream means an over-long archive is handled rather than
        # rejected: the caller should not have to pre-slice the input to avoid an error.
        series = [(day, close) for day, close in series if day < fitted_through]
        if len(series) < minimum_history_bars:
            skipped_short += 1
            continue
        session_dates = [day for day, _ in series]
        closes = [close for _, close in series]
        examined += 1
        # One walk for every horizon: the entry decision is horizon-independent, so re-walking
        # per horizon would repeat the whole band reconstruction for no additional evidence.
        for event in measure_reversion_events(
            closes,
            trading_symbol=symbol,
            session_dates=session_dates,
            horizon_bars=tuple(horizons),
            rolling_window=rolling_window,
            band_quantile=Decimal(str(DEVIATION_BAND_QUANTILE)),
            minimum_deviations_before_banding=MINIMUM_OBSERVATIONS_FOR_BANDS,
        ):
            bucket = deviation_bucket_of(event.deviation_sigma)
            events_by_horizon_and_bucket[(event.horizon_bars, bucket)].append(event)
            events_by_symbol[(symbol, event.horizon_bars, bucket)].append(event)
            total_events += 1

    written: list[ReversionCapture] = []
    pooled_by_horizon: dict[int, ReversionCapture] = {}
    thin_buckets: list[Decimal] = []

    # Pooled first: it is the fallback the whole ladder rests on, and it is also the shrinkage
    # target, so nothing else can be computed until it exists.
    for horizon in horizons:
        pooled_events = [
            event
            for (event_horizon, _), bucket_events in events_by_horizon_and_bucket.items()
            if event_horizon == horizon
            for event in bucket_events
        ]
        if len(pooled_events) < MINIMUM_EVENTS_FOR_A_CALIBRATION:
            continue
        pooled = calibrate_from_events(
            pooled_events,
            fitted_through=fitted_through,
            maturity=CalibrationMaturity.POOLED_UNIVERSE,
        )
        pooled_by_horizon[horizon] = pooled
        written.append(pooled)
    pooled_count = len(written)

    bucket_count = 0
    for (_horizon, bucket), bucket_events in sorted(events_by_horizon_and_bucket.items()):
        if len(bucket_events) < MINIMUM_EVENTS_FOR_A_CALIBRATION:
            thin_buckets.append(bucket)
            continue
        written.append(
            calibrate_from_events(
                bucket_events,
                fitted_through=fitted_through,
                maturity=CalibrationMaturity.DEVIATION_BUCKET,
            )
        )
        bucket_count += 1

    instrument_count = 0
    for (symbol, horizon, _bucket), symbol_events in sorted(events_by_symbol.items()):
        if len(symbol_events) < MINIMUM_EVENTS_FOR_A_CALIBRATION:
            continue
        shrinkage_target = pooled_by_horizon.get(horizon)
        own = calibrate_from_events(
            symbol_events,
            fitted_through=fitted_through,
            maturity=CalibrationMaturity.INSTRUMENT_FITTED,
            trading_symbol=symbol,
        )
        written.append(
            own
            if shrinkage_target is None
            else shrink_toward_pooled(own, shrinkage_target)
        )
        instrument_count += 1

    store.record(written)
    return CalibrationFitReport(
        fitted_through=fitted_through,
        symbols_examined=examined,
        symbols_skipped_for_short_history=skipped_short,
        events_measured=total_events,
        pooled_calibrations=pooled_count,
        bucket_calibrations=bucket_count,
        instrument_calibrations=instrument_count,
        buckets_below_evidence_threshold=tuple(sorted(set(thin_buckets))),
    )
