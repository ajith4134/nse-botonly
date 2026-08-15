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
from nse_algo_trader.strategy.intraday_mean_reversion_engine import IntradayMeanReversionEngine

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

    deviation, _dispersion_fraction = deviation_and_dispersion(closes)
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


def recent_closes_available_at(
    *,
    instrument_token: int,
    as_of: datetime,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    closes_wanted: int = CLOSES_WANTED,
) -> list[tuple[datetime, Decimal]]:
    """The public read of the same availability-filtered closes `assemble_sizing_inputs` uses.

    Exposed because the horizon is now CHOSEN from the deviation (`A.115`), and the deviation is
    measured from these closes — so a caller has to see them before it can ask for inputs at a
    horizon. Same function underneath, so the two can never diverge on what was knowable when.
    """
    return _recent_closes(
        instrument_token=instrument_token,
        as_of=as_of,
        market_data=market_data,
        closes_wanted=closes_wanted,
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


def deviation_and_dispersion(
    closes: list[tuple[datetime, Decimal]],
) -> tuple[Decimal, Decimal]:
    """The deviation coordinate the calibration is INDEXED BY, and the dispersion it is measured in.

    Both come from `IntradayMeanReversionEngine` itself rather than being recomputed here, and that
    is the entire point. The calibrator's own docstring warns that `rolling_window` "must equal the
    live engine's" because "a calibration silently fitted to a different window would be measuring a
    different strategy while looking perfectly healthy" — and the first version of this function
    ignored that from the other side, taking a z-score over the whole 120-close window instead of
    the engine's 20.

    The two coordinates were not close. Against the operator's forty real calibrations the implied
    one-bar dispersion is **340 to 1,650 bps**, while a 120-bar z-score of five-minute closes put it
    near **7 bps** — a factor of fifty to two hundred. Every edge this assembler looked up was
    therefore the edge for a deviation depth the instrument was not at (`A.106`).

    Returns `(deviation_in_sigma, dispersion_as_a_fraction_of_price)`. The second is what Kelly's
    denominator needs, and taking it from the same engine is what keeps the edge and the variance
    expressed in the same units.
    """
    # The two regime thresholds gate `decide()` and have no bearing on the deviation coordinate,
    # which is pure arithmetic over the rolling window. They are required by the constructor, so
    # they are passed as zero and NOTHING here calls `decide()` — this assembler reads the
    # engine's measurement, never its verdict. The regime veto belongs to whoever decides to
    # trade, not to whoever prices the size.
    engine = IntradayMeanReversionEngine(
        minimum_regime_concentration=0.0, minimum_regime_agreement=0.0
    )
    engine.observe_closes([float(close) for _, close in closes])
    dispersion = engine.rolling_dispersion()
    latest = engine.latest_close()
    if dispersion is None or latest is None or dispersion <= 0 or latest <= 0:
        raise SizingInputAssemblyError(
            "the mean-reversion engine reports no rolling dispersion for this window, so the "
            "deviation cannot be expressed in the units the calibration is indexed by",
            missing=("price_bars",),
        )
    deviation = engine.current_deviation_sigma()
    if deviation is None:
        raise SizingInputAssemblyError(
            "the mean-reversion engine has not seen enough closes to report a deviation",
            missing=("price_bars",),
        )
    return Decimal(str(deviation)), Decimal(str(dispersion)) / Decimal(str(latest))
