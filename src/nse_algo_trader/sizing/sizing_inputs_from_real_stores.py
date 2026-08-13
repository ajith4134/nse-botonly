"""Assembling a real sizing decision from the stores this system already fills.

The sizer is deliberately pure: it opens nothing and reads nothing, so it can be tested without a
database and cannot see the future by accident. That purity has to be paid for somewhere, and this
is where — the one module that knows which store holds what, and how to ask each of them
point-in-time.

**Everything is read as-of a date.** Bars through `bars_as_of`, the instrument master through
`instruments_as_of`, the calibration through `capture_for(as_of=...)`. A sizing decision that could
see a lot-size revision or an edge calibration published after the decision would flatter every
backtest it touched without leaving a trace.

**A missing input is named, never substituted.** Each failure returns which store could not answer,
because "RELIANCE has no calibration" and "RELIANCE could not be sized" are different facts and only
the first one tells anybody what to fix. This is `L1.09`'s lesson generalised: a plausible
substituted lot size is worse than an absent one, and the same holds for the edge, the price history
and the reference price.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    DEFAULT_CALIBRATION_PATH,
    CalibrationCoverageError,
    ReversionCalibrationStore,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import SizingInputs

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_MARKET_DATA_PATH = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

BAR_INTERVAL = "5m"
"""The interval the recorder actually writes. Read from the data rather than assumed elsewhere."""

CLOSES_WANTED = 120
"""How many recent closes to hand the volatility estimator.

Four times its thirty-close minimum, so an EWMA with a twenty-bar half-life has several half-lives
of history behind its heaviest weights rather than being dominated by the window's own edge.
"""


class SizingInputAssemblyError(Exception):
    """A real store could not answer, and the missing input is named rather than replaced."""

    def __init__(self, message: str, *, missing: tuple[str, ...]) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True, slots=True)
class RealStorePaths:
    """Where the real data lives. Kept as a value so a test can point at a copy."""

    market_data: Path = DEFAULT_MARKET_DATA_PATH
    calibration: Path = DEFAULT_CALIBRATION_PATH


def tradeable_symbols(
    *, as_of: date, paths: RealStorePaths | None = None, limit: int | None = None
) -> tuple[tuple[int, str, int], ...]:
    """Every instrument that has BOTH a lot size and recorded bars — the real sizable universe.

    Returned as `(instrument_token, trading_symbol, lot_size)`. `R.09`: the caller verifies across
    all of them rather than a chosen few, because a sizer that works on RELIANCE and divides by zero
    on an illiquid scrip is not verified.
    """
    store = paths or RealStorePaths()
    with sqlite3.connect(f"file:{store.market_data}?mode=ro", uri=True) as connection:
        effective = connection.execute(
            "SELECT MAX(ingested_on) FROM instrument_master WHERE ingested_on <= ?",
            (as_of.isoformat(),),
        ).fetchone()[0]
        if effective is None:
            raise SizingInputAssemblyError(
                f"the instrument master holds no ingest at or before {as_of.isoformat()}",
                missing=("instrument_master",),
            )
        query = (
            "SELECT m.instrument_token, m.tradingsymbol, m.lot_size "
            "FROM instrument_master m "
            "WHERE m.ingested_on = ? AND m.lot_size > 0 "
            "AND EXISTS (SELECT 1 FROM price_bars b WHERE b.instrument_token = m.instrument_token) "
            "ORDER BY m.tradingsymbol"
        )
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = connection.execute(query, (effective,)).fetchall()
    return tuple((int(token), str(symbol), int(lot)) for token, symbol, lot in rows)


def assemble_sizing_inputs(
    *,
    instrument_token: int,
    trading_symbol: str,
    lot_size: int,
    deployable_rupees: Decimal,
    concurrent_position_capacity: int,
    horizon_bars: int,
    as_of: datetime,
    paths: RealStorePaths | None = None,
    closes_wanted: int = CLOSES_WANTED,
) -> SizingInputs:
    """Gather one real decision's inputs, or say which store could not answer.

    Raises:
        SizingInputAssemblyError: carrying `.missing` — the names of the stores that had no answer.
    """
    store = paths or RealStorePaths()
    closes = _recent_closes(
        instrument_token=instrument_token,
        as_of=as_of,
        market_data=store.market_data,
        closes_wanted=closes_wanted,
    )
    if not closes:
        raise SizingInputAssemblyError(
            f"{trading_symbol} has no {BAR_INTERVAL} bars available at {as_of.isoformat()}",
            missing=("price_bars",),
        )
    reference_price = closes[-1][1]
    if reference_price <= 0:
        raise SizingInputAssemblyError(
            f"{trading_symbol}'s most recent close is {reference_price}, which cannot price a lot",
            missing=("price_bars",),
        )

    deviation = _latest_deviation_sigma(closes)
    calibrations = ReversionCalibrationStore(store.calibration)
    try:
        capture = calibrations.capture_for(
            deviation_sigma=deviation,
            horizon_bars=horizon_bars,
            as_of=as_of.date(),
            trading_symbol=trading_symbol,
        )
    except CalibrationCoverageError as failure:
        raise SizingInputAssemblyError(
            f"{trading_symbol} has no reversion calibration covering a {deviation} sigma "
            f"deviation over {horizon_bars} bars at {as_of.date().isoformat()}: {failure}",
            missing=("reversion_calibration",),
        ) from failure

    return SizingInputs(
        trading_symbol=trading_symbol,
        deployable_rupees=deployable_rupees,
        reference_price_rupees=reference_price,
        lot_size=lot_size,
        concurrent_position_capacity=concurrent_position_capacity,
        horizon_bars=horizon_bars,
        calibration=capture,
        recent_closes=closes,
    )


def _recent_closes(
    *, instrument_token: int, as_of: datetime, market_data: Path, closes_wanted: int
) -> list[tuple[datetime, Decimal]]:
    """The most recent closes that were AVAILABLE at `as_of`, oldest first.

    Filtered on `availability_time`, not on `bar_timestamp`: the safe read is the only read, and a
    bar stamped before the decision but published after it is exactly the row that lets a backtest
    see the future.
    """
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT bar_timestamp, close_price FROM price_bars "
            "WHERE instrument_token = ? AND bar_interval = ? AND availability_time <= ? "
            "ORDER BY bar_timestamp DESC LIMIT ?",
            (instrument_token, BAR_INTERVAL, as_of.isoformat(), closes_wanted),
        ).fetchall()
    closes = [
        (datetime.fromisoformat(str(stamp)), Decimal(str(close)))
        for stamp, close in reversed(rows)
    ]
    # The estimator refuses duplicates and mis-ordering rather than repairing them, so a duplicate
    # bar_timestamp in the store must be surfaced there rather than smoothed away here.
    return closes


def _latest_deviation_sigma(closes: list[tuple[datetime, Decimal]]) -> Decimal:
    """How far the last close sits from the window's mean, in window standard deviations.

    This is the coordinate the reversion calibration is indexed by, so it is computed the same way
    the calibrator computed it: a z-score of the close against the window it belongs to. A window
    with no dispersion yields zero, which the calibration ladder treats as its smallest bucket
    rather than as an error.
    """
    values = [close for _, close in closes]
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
    if variance <= 0:
        return Decimal(0)
    return (values[-1] - mean) / variance.sqrt()
