"""Grouping instruments so a thinly-observed one can borrow from its peers.

The impact coefficient cannot be estimated per instrument — most instruments will have almost
no fills, ever. So it is estimated per BUCKET and shrunk toward. That only works if the buckets
are real, and on this universe they measurably are (`docs/research/220` §2.2.6): turnover
deciles over 1,403 instruments are perfectly monotone in spread, from **19.34 bps** in the
cheapest-traded decile to **2.20 bps** in the busiest, and the cross-sectional relationships are
strong power laws:

    log(quoted_spread_bps) = 8.285 - 0.346 * log(turnover)     R^2 = 0.565
    log(visible_depth_Rs)  = 1.508 + 0.563 * log(turnover)     R^2 = 0.354

Those two elasticities ARE the prior for an instrument nobody has traded yet. The depth exponent
of 0.563 sitting close to one-half is not a coincidence — it is the same square-root liquidity
scaling the impact law assumes, showing up independently in the resting book.

**Deciles, not fixed rupee thresholds.** A boundary written in rupees is a hardcoded value that
silently reclassifies the whole universe as the market grows (`R.03`); a decile is defined by the
distribution and re-derives itself every time it is computed.

**The second axis is the TICK REGIME, and it is not optional.** A large-tick instrument — one
whose spread is pinned at one tick — does not move by price at all; it moves by depth depletion,
which breaks any continuous-price impact model applied to it. Dayri and Rosenbaum formalise this
as the ratio of spread to volatility. NSE also *changed* the tick size on 2025-04-15 and again
for stock options on 2025-11-03, and moved F&O expiry to Tuesday from 2025-09-01, so instruments
mechanically switched regime with no change in real activity. A calibration that pools across
those dates is pooling across different markets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

_DECILE_COUNT = 10
"""Ten buckets, the convention. The count is a reporting choice, not a threshold: nothing is
compared against it, and changing it re-derives every boundary from the same distribution."""


class LiquidityBucketError(Exception):
    """The universe cannot be bucketed, and a made-up bucket would pool the wrong peers."""


class TickRegime(StrEnum):
    """Whether an instrument's spread is pinned by the tick or free to move.

    `LARGE_TICK` means the quoted spread sits at one tick essentially always: the price cannot
    express a smaller increment, so liquidity varies by queue depth instead. Impact models that
    assume a continuous price are wrong for these, and averaging them together with small-tick
    names contaminates both.
    """

    LARGE_TICK = "large_tick"
    SMALL_TICK = "small_tick"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InstrumentLiquidityObservation:
    """What is known about one instrument's liquidity on one date, from real data."""

    instrument_token: int
    trading_symbol: str
    observed_on: date
    traded_value_paise: Decimal
    traded_quantity: int
    median_spread_bps: Decimal
    tick_size_paise: Decimal | None = None

    @property
    def spread_in_ticks(self) -> Decimal | None:
        """How many ticks wide the typical spread is — the large-tick diagnostic.

        A value at or near one means the spread has nowhere smaller to go.
        """
        if self.tick_size_paise is None or self.tick_size_paise <= 0:
            return None
        if self.traded_quantity <= 0 or self.traded_value_paise <= 0:
            return None
        average_price_paise = self.traded_value_paise / Decimal(self.traded_quantity)
        spread_paise = self.median_spread_bps / Decimal(10_000) * average_price_paise
        return spread_paise / self.tick_size_paise


@dataclass(frozen=True, slots=True)
class LiquidityBucket:
    """One peer group: a turnover decile within a tick regime."""

    turnover_decile: int
    tick_regime: TickRegime

    def __post_init__(self) -> None:
        if not 0 <= self.turnover_decile < _DECILE_COUNT:
            raise LiquidityBucketError(
                f"decile {self.turnover_decile} is outside 0..{_DECILE_COUNT - 1}"
            )

    @property
    def key(self) -> str:
        return f"{self.tick_regime.value}/d{self.turnover_decile}"


@dataclass(frozen=True, slots=True)
class UniverseLiquidityBucketing:
    """The whole universe assigned to buckets, with the boundaries that did the assigning."""

    observed_on: date
    turnover_boundaries_paise: tuple[Decimal, ...]
    bucket_by_instrument: Mapping[int, LiquidityBucket]
    observation_by_instrument: Mapping[int, InstrumentLiquidityObservation]

    @property
    def instrument_count(self) -> int:
        return len(self.bucket_by_instrument)

    def bucket_for(self, instrument_token: int) -> LiquidityBucket:
        try:
            return self.bucket_by_instrument[instrument_token]
        except KeyError as error:
            raise LiquidityBucketError(
                f"instrument {instrument_token} was not in the universe on {self.observed_on}; "
                f"it cannot borrow a peer group it was never measured against"
            ) from error

    def members_of(self, bucket: LiquidityBucket) -> tuple[int, ...]:
        return tuple(
            token
            for token, assigned in self.bucket_by_instrument.items()
            if assigned == bucket
        )

    def bucket_median_spread_bps(self, bucket: LiquidityBucket) -> Decimal:
        """The peer group's typical spread — the fallback for an unobserved instrument."""
        spreads = sorted(
            self.observation_by_instrument[token].median_spread_bps
            for token in self.members_of(bucket)
        )
        if not spreads:
            raise LiquidityBucketError(f"bucket {bucket.key} has no members to summarise")
        return spreads[len(spreads) // 2]


def _decile_boundaries(sorted_values: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """The nine internal cut points of ten equal-count buckets."""
    if len(sorted_values) < _DECILE_COUNT:
        raise LiquidityBucketError(
            f"cannot cut {len(sorted_values)} instruments into {_DECILE_COUNT} deciles; a "
            f"decile with no members is a peer group that cannot be borrowed from"
        )
    return tuple(
        sorted_values[len(sorted_values) * index // _DECILE_COUNT]
        for index in range(1, _DECILE_COUNT)
    )


def _decile_of(value: Decimal, boundaries: Sequence[Decimal]) -> int:
    decile = 0
    for boundary in boundaries:
        if value < boundary:
            break
        decile += 1
    return min(decile, _DECILE_COUNT - 1)


def classify_tick_regime(
    observation: InstrumentLiquidityObservation, *, pinned_spread_in_ticks: Decimal
) -> TickRegime:
    """Large-tick when the typical spread is at or near the minimum increment.

    `pinned_spread_in_ticks` is supplied by the caller rather than fixed here because it is a
    modelling boundary, and the honest place for it is where it can be derived from the
    universe's own distribution of spread-in-ticks.
    """
    ratio = observation.spread_in_ticks
    if ratio is None:
        return TickRegime.UNKNOWN
    return TickRegime.LARGE_TICK if ratio <= pinned_spread_in_ticks else TickRegime.SMALL_TICK


def bucket_universe_by_liquidity(
    observations: Sequence[InstrumentLiquidityObservation],
    *,
    observed_on: date,
    pinned_spread_in_ticks: Decimal = Decimal("1.5"),
) -> UniverseLiquidityBucketing:
    """Assign every observed instrument to a turnover decile within its tick regime.

    Turnover is the primary axis because it is the one the measured elasticities are expressed
    in, and because it is available for every instrument from the daily bars without needing a
    book at all — which matters for the thousands of instruments the depth capture cannot
    cover.
    """
    if not observations:
        raise LiquidityBucketError("no observations to bucket")
    turnovers = sorted(observation.traded_value_paise for observation in observations)
    boundaries = _decile_boundaries(turnovers)
    bucket_by_instrument: dict[int, LiquidityBucket] = {}
    observation_by_instrument: dict[int, InstrumentLiquidityObservation] = {}
    for observation in observations:
        bucket_by_instrument[observation.instrument_token] = LiquidityBucket(
            turnover_decile=_decile_of(observation.traded_value_paise, boundaries),
            tick_regime=classify_tick_regime(
                observation, pinned_spread_in_ticks=pinned_spread_in_ticks
            ),
        )
        observation_by_instrument[observation.instrument_token] = observation
    return UniverseLiquidityBucketing(
        observed_on=observed_on,
        turnover_boundaries_paise=boundaries,
        bucket_by_instrument=bucket_by_instrument,
        observation_by_instrument=observation_by_instrument,
    )
