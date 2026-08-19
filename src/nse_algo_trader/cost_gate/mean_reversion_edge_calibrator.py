"""How much of a deviation actually reverts — measured, so the edge claim stops being invented.

`edge_from_mean_reversion_decision` used to answer this by assumption. It computed the expected
move as `|deviation| - band`: the excess beyond the entry trigger. That is not a measurement and
it is not a hypothesis about markets — it is an **exit rule** ("unwind to the band edge") wearing
the costume of an edge formula, and `R.03` exists to catch exactly that. Choose to exit at the
mean instead and the same signal claims 5.9x more, with nothing in the data to arbitrate.

**What this module does instead: it asks the archive.** For every historical bar where the engine
would have fired, it measures how far price actually moved back toward the mean over the next `h`
bars, and keeps the distribution. The edge claim then becomes what it should always have been —
the expected value of a measured outcome, carrying the standard error of its own estimate.

The measurement on the deep-history archive, 38,456 events across 600 symbols, is why this module
is shaped the way it is:

- **The mean and the median disagree violently.** At a one-day horizon the median captured move is
  5.6 bps and the mean is 34.1 bps: a small number of large reversions carry the whole result. A
  gate needs the MEAN, because a strategy run many times realises the average and not the typical
  case; a calibrator that reported the median would understate this strategy by six-fold. Both are
  kept, and their disagreement is published as `skew_ratio`, because a strategy whose edge lives in
  a thin tail is a different risk proposition from one whose edge is typical, even at equal mean.
- **The reversion fraction is small and the sigma is large.** Only about 6% of a 3-sigma deviation
  unwinds within five bars — but one sigma is ~286 bps of price on this universe, so 6% of it is
  still ~40 bps. Reasoning in sigma units alone would have called this strategy dead; reasoning in
  basis points, which is what costs are denominated in, shows it clearing the intraday floor. The
  calibration is therefore stored in **basis points of price**, the same unit as the hurdle.
- **Deeper deviations do not revert proportionally more.** Capture at 2.0 sigma and at 4.0 sigma is
  similar in sigma terms, so the edge is NOT linear in deviation, which is precisely what the old
  formula assumed. Calibration is bucketed by deviation so the non-linearity survives.

**Strict causality.** Every calibration row carries `fitted_through`, and `capture_for` refuses a
row fitted on data at or after the session being priced. A backtest that prices 2024 with a
coefficient fitted on 2026 is not a backtest, and the refusal is a hard error rather than a warning
because a silent look-ahead flatters every downstream number without leaving a trace.

**Refusal over substitution.** No covering calibration means `CalibrationCoverageError`, never a
fallback constant — the same rule `L0.31` applies to statutory rates, for the same reason: an
invented number is indistinguishable from a measured one once it is downstream.

**`R.04` maturity ladder.** The algorithm is identical at every stage; only the source of the
estimate moves. Pooled across the universe, then per bucket, then per instrument as evidence
accumulates, with empirical-Bayes shrinkage toward the pooled estimate throughout.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

DEFAULT_CALIBRATION_PATH = Path("~/.nse_algo_trader/reversion_calibration.sqlite3").expanduser()

DEEPEST_DISTINGUISHED_DEVIATION_SIGMA = Decimal(4)
"""Deviations beyond this are pooled into one bucket rather than each getting their own.

Beyond four sigma the population stops being dislocations and becomes corporate actions and stale
prints. Splitting them finer would let a handful of such bars form a singleton calibration that
then prices a real trade."""

SMALLEST_WINDOW_WITH_A_DEFINED_DISPERSION = 2
"""One close has no dispersion, so a deviation measured in units of it is undefined."""

MINIMUM_EVENTS_FOR_A_CALIBRATION = 200
"""Below this an estimate of a heavy-tailed mean is noise.

Not a tuned cutoff: the measured distribution is tail-carried (mean 6x the median at one bar), and
a mean estimated from a handful of draws off such a distribution is dominated by whether a tail
event happened to land in the sample. Rows below this threshold are stored — the evidence is real
and accumulates — but `capture_for` refuses to price from them, so thin data gates ACTIVATION
rather than shrinking the algorithm (`R.04`, `R.21`).
"""


class CalibrationError(Exception):
    """A calibration cannot be fitted or stored."""


class CalibrationCoverageError(CalibrationError):
    """No calibration covers this case, and a substituted number would be an invention."""


class CalibrationMaturity(StrEnum):
    """Where the estimate came from. The ALGORITHM does not change between these."""

    POOLED_UNIVERSE = "pooled_universe"
    DEVIATION_BUCKET = "deviation_bucket"
    INSTRUMENT_FITTED = "instrument_fitted"


@dataclass(frozen=True, slots=True)
class ReversionCapture:
    """What a deviation of this depth actually recovered, over this horizon, in bps of price."""

    deviation_bucket: Decimal
    horizon_bars: int
    event_count: int
    mean_captured_bps: Decimal
    median_captured_bps: Decimal
    standard_error_bps: Decimal
    mean_captured_sigma: Decimal
    fitted_through: date
    maturity: CalibrationMaturity
    trading_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.event_count <= 0:
            raise CalibrationError("a calibration with no events is not a calibration")
        if self.horizon_bars <= 0:
            raise CalibrationError(f"horizon must be positive, got {self.horizon_bars}")
        if self.standard_error_bps < 0:
            raise CalibrationError("standard error cannot be negative")

    @property
    def t_statistic(self) -> Decimal | None:
        """How far the mean sits from zero in standard errors. `None` when it cannot be formed.

        The single most important number here: it is what separates "this strategy earns 40 bps"
        from "this sample happened to contain a rally".
        """
        if self.standard_error_bps <= 0:
            return None
        return self.mean_captured_bps / self.standard_error_bps

    @property
    def is_distinguishable_from_zero(self) -> bool:
        """Two standard errors, the conventional bar, applied to a one-sided claim of positive edge.

        A calibration that fails this says the strategy's edge is not measurable on the evidence
        available — which is a legitimate answer, and a very different one from an edge of zero.
        """
        t = self.t_statistic
        return t is not None and t > Decimal(2)

    @property
    def skew_ratio(self) -> Decimal | None:
        """Mean over median. Large means the edge lives in a thin tail of big reversions.

        Published rather than smoothed away because it changes what the edge IS. At `skew_ratio`
        near 1 a strategy earns its mean most times it trades; at 6, most trades earn far less
        than the mean and a few carry everything, so realising the mean needs many more trades
        and far more capital patience than the headline number suggests.
        """
        if self.median_captured_bps == 0:
            return None
        return self.mean_captured_bps / self.median_captured_bps

    @property
    def lower_confidence_bps(self) -> Decimal:
        """The pessimistic end of the edge claim, two standard errors below the mean.

        What a gate should price against when it wants the claim to survive being wrong. Not
        floored at zero: a negative lower bound is information, and clipping it would disguise
        a calibration that cannot rule out the strategy losing money.
        """
        return self.mean_captured_bps - Decimal(2) * self.standard_error_bps

    def describe(self) -> str:
        t = self.t_statistic
        t_text = "undefined" if t is None else f"{t:.2f}"
        return (
            f"{self.deviation_bucket} sigma deviations recovered {self.mean_captured_bps:.1f} bps "
            f"on average over {self.horizon_bars} bars (median {self.median_captured_bps:.1f}, "
            f"t={t_text}) across {self.event_count} events, fitted through {self.fitted_through} "
            f"[{self.maturity.value}]"
        )


@dataclass(frozen=True, slots=True)
class ReversionEvent:
    """One historical bar where the engine would have fired, and what happened next.

    `captured_bps` is signed TOWARD the mean: positive means price moved back, negative means it
    kept going. Storing it signed rather than absolute is what allows the calibration to come out
    negative, which is the outcome that would refute the strategy outright.
    """

    trading_symbol: str
    observed_on: date
    deviation_sigma: Decimal
    sigma_bps_of_price: Decimal
    captured_bps: Decimal
    captured_sigma: Decimal
    horizon_bars: int


def deviation_bucket_of(deviation_sigma: Decimal) -> Decimal:
    """Half-sigma buckets, capped at 4.

    Half a sigma is the finest grain the event counts support at the deep end, and the cap exists
    because 6-sigma and 12-sigma deviations are overwhelmingly corporate actions and stale prints
    rather than tradeable dislocations — pooling them into one bucket keeps them from each
    forming a singleton calibration that would then price a real trade.
    """
    magnitude = abs(deviation_sigma)
    if magnitude > DEEPEST_DISTINGUISHED_DEVIATION_SIGMA:
        return DEEPEST_DISTINGUISHED_DEVIATION_SIGMA
    return (magnitude * 2).quantize(Decimal(1), rounding="ROUND_HALF_EVEN") / 2


def measure_reversion_events(
    closes: Sequence[float],
    *,
    trading_symbol: str,
    session_dates: Sequence[date],
    horizon_bars: int | Sequence[int],
    rolling_window: int,
    band_quantile: Decimal,
    minimum_deviations_before_banding: int,
) -> list[ReversionEvent]:
    """Walk one instrument's history and record every event the engine would have entered on.

    **Causality is enforced by construction, not by convention.** The mean and dispersion at `t`
    come only from the window ENDING at `t`; the entry band at `t` is a quantile of only those
    deviations already seen by `t`; and the outcome comes only from bars after `t`. There is no
    point in this function where a future bar is visible to a past decision, which is what makes
    the resulting number a backtest rather than a description of the sample.

    `rolling_window`, `band_quantile` and `minimum_deviations_before_banding` are passed in rather
    than imported so the calibration provably matches whatever the live engine is configured with.
    A calibration fitted to a 20-bar window and applied to a 50-bar engine would be measuring a
    different strategy.

    **The band uses `river.stats.Quantile`, the same streaming estimator the live engine uses, not
    an exact quantile of the history so far.** That is a fidelity requirement rather than an
    optimisation. The streaming estimator is an approximation, so it fires on a slightly different
    set of bars than an exact quantile would; calibrating against the exact one would measure a
    strategy that does not exist and would then price the one that does. It happens also to turn an
    O(n^2 log n) walk into a linear one, which is what makes a nightly fit over the full universe
    affordable.

    `horizon_bars` accepts several horizons and walks the series ONCE for all of them. The entry
    decision does not depend on the horizon — only the outcome does — so re-walking per horizon
    repeats identical work and, worse, would let the horizons silently disagree about which bars
    were entries if anything about the band ever became stateful.
    """
    horizons = (horizon_bars,) if isinstance(horizon_bars, int) else tuple(horizon_bars)
    if not horizons:
        raise CalibrationError("no horizons requested")
    if any(horizon <= 0 for horizon in horizons):
        raise CalibrationError(f"horizons must all be positive, got {horizons}")
    if rolling_window < SMALLEST_WINDOW_WITH_A_DEFINED_DISPERSION:
        raise CalibrationError(
            f"rolling window must be at least {SMALLEST_WINDOW_WITH_A_DEFINED_DISPERSION}, "
            f"got {rolling_window}"
        )
    if not 0 < band_quantile < 1:
        raise CalibrationError(f"band quantile must be in (0, 1), got {band_quantile}")
    if len(closes) != len(session_dates):
        raise CalibrationError(
            f"{len(closes)} closes against {len(session_dates)} dates; an event cannot be dated"
        )

    from river import stats

    events: list[ReversionEvent] = []
    deviation_quantile = stats.Quantile(float(band_quantile))
    seen = 0
    longest_horizon = max(horizons)

    for index in range(rolling_window, len(closes) - longest_horizon):
        window = closes[index - rolling_window : index]
        price = closes[index]
        if price <= 0:
            continue
        dispersion = stdev(window)
        if dispersion <= 0:
            continue
        deviation = (price - fmean(window)) / dispersion
        # Read the band BEFORE this deviation updates it. Updating first would let a bar's own
        # extremity raise the bar it has to clear, which is a subtle look-ahead: the entry rule
        # would depend on a quantity that does not exist until the decision is already made.
        band = deviation_quantile.get()  # type: ignore[no-untyped-call]
        deviation_quantile.update(abs(deviation))  # type: ignore[no-untyped-call]
        seen += 1
        if seen < minimum_deviations_before_banding or band is None:
            continue
        if abs(deviation) < band:
            continue
        toward_the_mean = -1.0 if deviation > 0 else 1.0
        for horizon in horizons:
            move = toward_the_mean * (closes[index + horizon] - price)
            events.append(
                ReversionEvent(
                    trading_symbol=trading_symbol,
                    observed_on=session_dates[index],
                    deviation_sigma=Decimal(str(round(deviation, 6))),
                    sigma_bps_of_price=Decimal(str(round(dispersion / price * 10_000, 4))),
                    captured_bps=Decimal(str(round(move / price * 10_000, 4))),
                    captured_sigma=Decimal(str(round(move / dispersion, 6))),
                    horizon_bars=horizon,
                )
            )
    return events


def calibrate_from_events(
    events: Sequence[ReversionEvent],
    *,
    fitted_through: date,
    maturity: CalibrationMaturity,
    trading_symbol: str | None = None,
) -> ReversionCapture:
    """Reduce a set of events to one calibration row, keeping the shape of the distribution.

    The standard error is the plain `s / sqrt(n)`. That is a deliberate choice and a limitation
    worth stating: reversion events overlap in time and cluster across instruments on the same
    day, so the effective sample is smaller than `n` and this standard error is optimistic. It is
    kept because the alternative — a cluster-robust estimator — needs the event dates joined
    across instruments, and reporting an optimistic error honestly beats reporting a corrected one
    that hides its own assumptions. `M10` in the backlog carries the fix.
    """
    if not events:
        raise CalibrationError("cannot calibrate from an empty event set")
    horizons = {event.horizon_bars for event in events}
    if len(horizons) != 1:
        raise CalibrationError(
            f"events span horizons {sorted(horizons)}; captures at different horizons are "
            f"different quantities and averaging them would be meaningless"
        )
    latest = max(event.observed_on for event in events)
    if latest >= fitted_through:
        raise CalibrationError(
            f"an event on {latest} cannot inform a calibration declared fitted through "
            f"{fitted_through}; that is look-ahead, and it flatters every number downstream"
        )

    captured = [float(event.captured_bps) for event in events]
    count = len(captured)
    mean_bps = fmean(captured)
    spread = stdev(captured) if count > 1 else 0.0
    return ReversionCapture(
        deviation_bucket=deviation_bucket_of(
            Decimal(str(fmean([float(abs(event.deviation_sigma)) for event in events])))
        ),
        horizon_bars=horizons.pop(),
        event_count=count,
        mean_captured_bps=Decimal(str(round(mean_bps, 4))),
        median_captured_bps=Decimal(str(round(median(captured), 4))),
        standard_error_bps=Decimal(str(round(spread / sqrt(count), 4))),
        mean_captured_sigma=Decimal(
            str(round(fmean([float(event.captured_sigma) for event in events]), 6))
        ),
        fitted_through=fitted_through,
        maturity=maturity,
        trading_symbol=trading_symbol,
    )


def shrink_toward_pooled(own: ReversionCapture, pooled: ReversionCapture) -> ReversionCapture:
    """Empirical-Bayes blend of an instrument's own calibration with the universe's.

    Weight is `tau^2 / (tau^2 + se^2)` — the same estimator the impact model uses, for the same
    reason. With few own events the standard error dominates and the estimate collapses onto the
    pooled one; with many it keeps its own. The blended standard error is the weighted one, so a
    shrunk estimate cannot claim more precision than it has.

    `tau^2`, the between-instrument variance, is taken as the pooled row's own sampling variance.
    That is a conservative stand-in: it shrinks harder than a properly estimated `tau^2` would,
    which errs toward the universe estimate rather than toward an instrument's small sample.
    """
    if own.horizon_bars != pooled.horizon_bars:
        raise CalibrationError("cannot shrink across different horizons")
    own_variance = own.standard_error_bps**2
    between_variance = pooled.standard_error_bps**2 * Decimal(pooled.event_count)
    denominator = between_variance + own_variance
    weight = Decimal(0) if denominator <= 0 else between_variance / denominator
    blended = weight * own.mean_captured_bps + (Decimal(1) - weight) * pooled.mean_captured_bps
    blended_error = (
        weight * own.standard_error_bps + (Decimal(1) - weight) * pooled.standard_error_bps
    )
    return ReversionCapture(
        deviation_bucket=own.deviation_bucket,
        horizon_bars=own.horizon_bars,
        event_count=own.event_count,
        mean_captured_bps=blended,
        median_captured_bps=own.median_captured_bps,
        standard_error_bps=blended_error,
        mean_captured_sigma=own.mean_captured_sigma,
        fitted_through=max(own.fitted_through, pooled.fitted_through),
        maturity=own.maturity,
        trading_symbol=own.trading_symbol,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reversion_calibration (
    trading_symbol TEXT NOT NULL DEFAULT '',
    deviation_bucket TEXT NOT NULL,
    horizon_bars INTEGER NOT NULL,
    fitted_through TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    mean_captured_bps TEXT NOT NULL,
    median_captured_bps TEXT NOT NULL,
    standard_error_bps TEXT NOT NULL,
    mean_captured_sigma TEXT NOT NULL,
    maturity TEXT NOT NULL,
    PRIMARY KEY (trading_symbol, deviation_bucket, horizon_bars, fitted_through)
);
CREATE INDEX IF NOT EXISTS reversion_calibration_lookup
    ON reversion_calibration (deviation_bucket, horizon_bars, fitted_through);
"""


class ReversionCalibrationStore:
    """Persisted calibrations, resolved point-in-time.

    `capture_for` walks the `R.04` ladder from most to least specific — this instrument, then this
    deviation bucket across the universe, then the pooled estimate — and refuses if nothing at any
    level both covers the case and predates the session. Refusing is the whole contract: a gate
    that received a silently substituted coefficient would price a trade on a number nobody fitted.
    """

    def __init__(self, database_path: Path = DEFAULT_CALIBRATION_PATH) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def record(self, calibrations: Iterable[ReversionCapture]) -> int:
        rows = [
            (
                calibration.trading_symbol or "",
                str(calibration.deviation_bucket),
                calibration.horizon_bars,
                calibration.fitted_through.isoformat(),
                calibration.event_count,
                str(calibration.mean_captured_bps),
                str(calibration.median_captured_bps),
                str(calibration.standard_error_bps),
                str(calibration.mean_captured_sigma),
                calibration.maturity.value,
            )
            for calibration in calibrations
        ]
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO reversion_calibration (trading_symbol, "
                "deviation_bucket, horizon_bars, fitted_through, event_count, "
                "mean_captured_bps, median_captured_bps, standard_error_bps, "
                "mean_captured_sigma, maturity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return len(rows)

    def capture_for(
        self,
        *,
        deviation_sigma: Decimal,
        horizon_bars: int,
        as_of: date,
        trading_symbol: str | None = None,
        minimum_events: int = MINIMUM_EVENTS_FOR_A_CALIBRATION,
    ) -> ReversionCapture:
        """The best-evidenced calibration that covers this case and predates `as_of`.

        Raises:
            CalibrationCoverageError: nothing covers it. The message names the ladder rungs that
                were tried, so the absence is diagnosable rather than merely fatal.
        """
        bucket = deviation_bucket_of(deviation_sigma)
        attempts: list[str] = []
        for symbol in ([trading_symbol] if trading_symbol else []) + [""]:
            row = self._best_row(
                symbol=symbol,
                bucket=bucket,
                horizon_bars=horizon_bars,
                as_of=as_of,
                minimum_events=minimum_events,
            )
            attempts.append(f"{symbol or 'pooled universe'} at {bucket} sigma")
            if row is not None:
                return row
        raise CalibrationCoverageError(
            f"no reversion calibration covers a {deviation_sigma} sigma deviation over "
            f"{horizon_bars} bars as of {as_of} with at least {minimum_events} events; tried "
            f"{', '.join(attempts)}. The edge cannot be priced, and substituting a coefficient "
            f"here would put an unfitted number into a live trading decision"
        )

    def _best_row(
        self,
        *,
        symbol: str,
        bucket: Decimal,
        horizon_bars: int,
        as_of: date,
        minimum_events: int,
    ) -> ReversionCapture | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT trading_symbol, deviation_bucket, horizon_bars, fitted_through, "
                "event_count, mean_captured_bps, median_captured_bps, standard_error_bps, "
                "mean_captured_sigma, maturity FROM reversion_calibration "
                "WHERE trading_symbol = ? AND deviation_bucket = ? AND horizon_bars = ? "
                "AND fitted_through <= ? AND event_count >= ? "
                "ORDER BY fitted_through DESC LIMIT 1",
                [
                    symbol,
                    str(bucket),
                    horizon_bars,
                    as_of.isoformat(),
                    minimum_events,
                ],
            ).fetchone()
        if row is None:
            return None
        return ReversionCapture(
            trading_symbol=str(row[0]) or None,
            deviation_bucket=Decimal(str(row[1])),
            horizon_bars=int(row[2]),
            fitted_through=date.fromisoformat(str(row[3])),
            event_count=int(row[4]),
            mean_captured_bps=Decimal(str(row[5])),
            median_captured_bps=Decimal(str(row[6])),
            standard_error_bps=Decimal(str(row[7])),
            mean_captured_sigma=Decimal(str(row[8])),
            maturity=CalibrationMaturity(str(row[9])),
        )

    def calibrated_horizons(self, *, as_of: date) -> Sequence[int]:
        """Which holding horizons have evidence at all, ascending — the grid, read not assumed.

        A caller choosing a horizon must choose from what was FITTED. Hardcoding the ladder here or
        in the caller would mean a refit that added or dropped a horizon left a stale list deciding
        how long real positions are held.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT horizon_bars FROM reversion_calibration "
                "WHERE fitted_through <= ? ORDER BY horizon_bars",
                [as_of.isoformat()],
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def calibrated_buckets(self, *, horizon_bars: int, as_of: date) -> Sequence[Decimal]:
        """Which deviation depths have evidence. An empty result is a coverage statement."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT deviation_bucket FROM reversion_calibration "
                "WHERE horizon_bars = ? AND fitted_through <= ? ORDER BY deviation_bucket",
                [horizon_bars, as_of.isoformat()],
            ).fetchall()
        return tuple(Decimal(str(row[0])) for row in rows)

    def calibration_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM reversion_calibration").fetchone()[0]
            )
