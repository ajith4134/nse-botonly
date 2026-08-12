"""The state that makes this an engine rather than a formula: what it has learned, per session.

Three things accrue here, and they are stored separately because they are known to different
degrees and must never be averaged into one "calibration":

1. **Per-instrument spread profiles**, measured directly from the book. High-signal: one session
   nearly pins an instrument's typical spread, and measurement showed pooling makes it WORSE
   (`quoted_spread_observer` carries the numbers). Stored per instrument, never shrunk.
2. **Per-bucket impact exponents**, which cannot be measured from a snapshot tape at all
   (measured R-squared of 0.010 to 0.070 intraday) and start as the published range. These are what
   real fills will eventually narrow, per liquidity bucket first and per instrument only much
   later.
3. **Realised-versus-expected fills**, which is the only evidence that can move (2), and which
   does not exist yet because nothing has traded.

**Why the store carries a session count.** Every number here is derived from a tape that is
currently two sessions long. An engine that reports a fitted exponent without saying how much
data stands behind it invites a reader to trust a number that one quiet Tuesday produced. So
every read returns its own provenance, and `L1.02`'s gate can refuse on thin evidence rather
than on a confident-looking point estimate.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.execution_fill.instrument_liquidity_buckets import LiquidityBucket, TickRegime
from nse_algo_trader.execution_fill.market_impact_estimator import (
    PLAUSIBLE_EXPONENT_RANGE,
    ImpactEstimateMaturity,
    shrinkage_weight,
)
from nse_algo_trader.execution_fill.quoted_spread_observer import InstrumentSpreadProfile

DEFAULT_PARAMETER_STORE_PATH = Path("~/.nse_algo_trader/execution_fill.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument_spread_profile (
    instrument_token INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    median_spread_bps TEXT NOT NULL,
    upper_quantile_spread_bps TEXT NOT NULL,
    upper_quantile TEXT NOT NULL,
    minimum_spread_bps TEXT NOT NULL,
    maximum_spread_bps TEXT NOT NULL,
    PRIMARY KEY (instrument_token, session_date)
);
CREATE TABLE IF NOT EXISTS bucket_impact_parameter (
    tick_regime TEXT NOT NULL,
    turnover_decile INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    fitted_exponent TEXT,
    exponent_dispersion TEXT,
    fill_observation_count INTEGER NOT NULL,
    PRIMARY KEY (tick_regime, turnover_decile, session_date)
);
CREATE TABLE IF NOT EXISTS realised_fill_observation (
    order_reference TEXT NOT NULL PRIMARY KEY,
    instrument_token INTEGER NOT NULL,
    session_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    expected_cost_bps TEXT NOT NULL,
    realised_cost_bps TEXT NOT NULL,
    was_censored INTEGER NOT NULL
);
"""


class ExecutionFillParameterError(Exception):
    """The store cannot answer, and answering anyway would invent a calibration."""


@dataclass(frozen=True, slots=True)
class BucketImpactParameter:
    """One liquidity bucket's impact exponent, and how much evidence stands behind it."""

    bucket: LiquidityBucket
    session_date: date
    fitted_exponent: Decimal | None
    exponent_dispersion: Decimal | None
    fill_observation_count: int

    @property
    def maturity(self) -> ImpactEstimateMaturity:
        """`R.04`'s ladder, read off the evidence rather than switched by configuration."""
        if self.fitted_exponent is None or self.fill_observation_count == 0:
            return ImpactEstimateMaturity.ANCHORED_PRIOR
        return ImpactEstimateMaturity.BUCKET_FITTED

    def exponent_range(
        self, *, prior_range: tuple[Decimal, Decimal] = PLAUSIBLE_EXPONENT_RANGE
    ) -> tuple[Decimal, Decimal]:
        """The range to estimate with — the published span, narrowed by evidence as it arrives.

        With no fills this is the published disagreement, unchanged: the world does not agree
        on the exponent and pretending otherwise would make every interval too narrow. As fills
        accrue the fitted value pulls the range in, weighted by the same empirical-Bayes weight
        the impact estimator uses, so the narrowing is EARNED rather than declared.
        """
        if self.fitted_exponent is None:
            return prior_range
        dispersion = self.exponent_dispersion or Decimal(0)
        weight = shrinkage_weight(
            self.fill_observation_count,
            own_variance=dispersion * dispersion,
            between_instrument_variance=_prior_variance(prior_range),
        )
        lower_prior, upper_prior = prior_range
        half_width = (upper_prior - lower_prior) / Decimal(2) * (Decimal(1) - weight)
        return (
            max(lower_prior, self.fitted_exponent - half_width),
            min(upper_prior, self.fitted_exponent + half_width),
        )


def _prior_variance(prior_range: tuple[Decimal, Decimal]) -> Decimal:
    """Treat the published span as roughly four standard deviations wide.

    A range is not a variance, so one has to be implied to combine them. Four sigma is the
    conventional reading of a stated plausible range and it is used only to set how fast
    evidence displaces the prior — not to make any claim about the exponent itself.
    """
    lower, upper = prior_range
    sigma = (upper - lower) / Decimal(4)
    return sigma * sigma


@dataclass(frozen=True, slots=True)
class RealisedFillObservation:
    """One executed order, modelled against what actually happened.

    The seam `L1.07` builds on. Nothing writes these yet — no order path exists — and that is
    recorded rather than stubbed: the table is real, the reader is real, and the count is
    honestly zero.
    """

    order_reference: str
    instrument_token: int
    session_date: date
    quantity: int
    expected_cost_bps: Decimal
    realised_cost_bps: Decimal
    was_censored: bool

    @property
    def residual_bps(self) -> Decimal:
        """Expected minus realised. Positive means the model OVERSTATED the cost."""
        return self.expected_cost_bps - self.realised_cost_bps


class ExecutionFillParameterStore:
    """Carries spread profiles, bucket exponents and realised fills across sessions."""

    def __init__(self, database_path: Path = DEFAULT_PARAMETER_STORE_PATH) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    # ------------------------------------------------------------------ spread

    def record_spread_profiles(
        self, profiles: Iterable[InstrumentSpreadProfile], *, session_date: date
    ) -> int:
        rows = [
            (
                profile.instrument_token,
                session_date.isoformat(),
                profile.observation_count,
                str(profile.median_spread_bps),
                str(profile.upper_quantile_spread_bps),
                str(profile.upper_quantile),
                str(profile.minimum_spread_bps),
                str(profile.maximum_spread_bps),
            )
            for profile in profiles
        ]
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO instrument_spread_profile (instrument_token, "
                "session_date, observation_count, median_spread_bps, upper_quantile_spread_bps, "
                "upper_quantile, minimum_spread_bps, maximum_spread_bps) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return len(rows)

    def spread_profile(
        self, instrument_token: int, *, as_of: date | None = None
    ) -> InstrumentSpreadProfile:
        """The most recent profile at or before `as_of`, or a refusal.

        Point-in-time by default so a replay of an old session cannot pick up a spread
        measured later — the same look-ahead discipline `L0.31` enforces for rates.
        """
        clause = "" if as_of is None else " AND session_date <= ?"
        parameters: list[object] = [instrument_token]
        if as_of is not None:
            parameters.append(as_of.isoformat())
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT observation_count, median_spread_bps, upper_quantile_spread_bps, "
                "upper_quantile, minimum_spread_bps, maximum_spread_bps "
                "FROM instrument_spread_profile WHERE instrument_token = ?"
                + clause
                + " ORDER BY session_date DESC LIMIT 1",
                parameters,
            ).fetchone()
        if row is None:
            raise ExecutionFillParameterError(
                f"no spread profile for instrument {instrument_token}"
                + (f" at or before {as_of}" if as_of else "")
                + " — an unmeasured instrument has no spread, and substituting a peer's would "
                "hide exactly the instrument-specific variation that makes spread worth "
                "measuring per instrument in the first place"
            )
        return InstrumentSpreadProfile(
            instrument_token=instrument_token,
            observation_count=int(row[0]),
            median_spread_bps=Decimal(row[1]),
            upper_quantile_spread_bps=Decimal(row[2]),
            upper_quantile=Decimal(row[3]),
            minimum_spread_bps=Decimal(row[4]),
            maximum_spread_bps=Decimal(row[5]),
        )

    def measured_instrument_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT instrument_token) FROM instrument_spread_profile"
            ).fetchone()
        return int(row[0])

    def session_count(self) -> int:
        """How many sessions of evidence everything here rests on."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT session_date) FROM instrument_spread_profile"
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ impact

    def record_bucket_parameter(self, parameter: BucketImpactParameter) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO bucket_impact_parameter (tick_regime, turnover_decile, "
                "session_date, fitted_exponent, exponent_dispersion, fill_observation_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    parameter.bucket.tick_regime.value,
                    parameter.bucket.turnover_decile,
                    parameter.session_date.isoformat(),
                    None if parameter.fitted_exponent is None else str(parameter.fitted_exponent),
                    None
                    if parameter.exponent_dispersion is None
                    else str(parameter.exponent_dispersion),
                    parameter.fill_observation_count,
                ),
            )
            connection.commit()

    def bucket_parameter(
        self, bucket: LiquidityBucket, *, as_of: date | None = None
    ) -> BucketImpactParameter:
        """This bucket's fitted exponent, or an honest unfitted one.

        Never raises for an unfitted bucket: with no fills anywhere, EVERY bucket is unfitted,
        and that is the normal state today rather than an error. It returns the prior with a
        zero observation count, so the caller sees `ANCHORED_PRIOR` and the full published
        interval rather than a number that looks fitted.
        """
        # Two complete literal queries rather than one assembled from a fragment. The fragment
        # was never caller-controlled, but a query built by concatenation has to be READ to be
        # judged safe, and this one does not.
        select_latest = (
            "SELECT session_date, fitted_exponent, exponent_dispersion, fill_observation_count "
            "FROM bucket_impact_parameter WHERE tick_regime = ? AND turnover_decile = ? "
            "ORDER BY session_date DESC LIMIT 1"
        )
        select_latest_as_of = (
            "SELECT session_date, fitted_exponent, exponent_dispersion, fill_observation_count "
            "FROM bucket_impact_parameter WHERE tick_regime = ? AND turnover_decile = ? "
            "AND session_date <= ? ORDER BY session_date DESC LIMIT 1"
        )
        parameters: list[object] = [bucket.tick_regime.value, bucket.turnover_decile]
        if as_of is None:
            query = select_latest
        else:
            query = select_latest_as_of
            parameters.append(as_of.isoformat())
        with closing(self._connect()) as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return BucketImpactParameter(
                bucket=bucket,
                session_date=as_of or date.min,
                fitted_exponent=None,
                exponent_dispersion=None,
                fill_observation_count=0,
            )
        return BucketImpactParameter(
            bucket=bucket,
            session_date=date.fromisoformat(row[0]),
            fitted_exponent=None if row[1] is None else Decimal(row[1]),
            exponent_dispersion=None if row[2] is None else Decimal(row[2]),
            fill_observation_count=int(row[3]),
        )

    # ------------------------------------------------------------- realised fills

    def record_realised_fills(self, observations: Iterable[RealisedFillObservation]) -> int:
        rows = [
            (
                observation.order_reference,
                observation.instrument_token,
                observation.session_date.isoformat(),
                observation.quantity,
                str(observation.expected_cost_bps),
                str(observation.realised_cost_bps),
                int(observation.was_censored),
            )
            for observation in observations
        ]
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO realised_fill_observation (order_reference, "
                "instrument_token, session_date, quantity, expected_cost_bps, "
                "realised_cost_bps, was_censored) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return len(rows)

    def realised_fill_count(self) -> int:
        """Zero today, and that is the honest state — nothing has traded yet."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM realised_fill_observation").fetchone()
        return int(row[0])

    def realised_fills(
        self, instrument_token: int | None = None
    ) -> Sequence[RealisedFillObservation]:
        clause = "" if instrument_token is None else " WHERE instrument_token = ?"
        parameters = [] if instrument_token is None else [instrument_token]
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT order_reference, instrument_token, session_date, quantity, "
                "expected_cost_bps, realised_cost_bps, was_censored "
                "FROM realised_fill_observation" + clause,
                parameters,
            ).fetchall()
        return tuple(
            RealisedFillObservation(
                order_reference=row[0],
                instrument_token=int(row[1]),
                session_date=date.fromisoformat(row[2]),
                quantity=int(row[3]),
                expected_cost_bps=Decimal(row[4]),
                realised_cost_bps=Decimal(row[5]),
                was_censored=bool(row[6]),
            )
            for row in rows
        )


def unfitted_parameter_for(bucket: LiquidityBucket) -> BucketImpactParameter:
    """The starting state of every bucket: the published range, no fills, honest about it."""
    return BucketImpactParameter(
        bucket=bucket,
        session_date=date.min,
        fitted_exponent=None,
        exponent_dispersion=None,
        fill_observation_count=0,
    )


DEFAULT_BUCKET = LiquidityBucket(turnover_decile=0, tick_regime=TickRegime.UNKNOWN)
"""The bucket an instrument falls in before the universe has been bucketed at all.

Decile 0 and UNKNOWN tick regime, deliberately: the least liquid group and the least-known
regime, so an unbucketed instrument is treated as the most expensive rather than the average.
"""
