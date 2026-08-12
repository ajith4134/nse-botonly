"""The raw pipeline: 15.9 million tape rows into the handful of measurements that matter.

Reading the depth tape into one `ExchangeFeedDelayObservation` per packet would build 11.4
million Python objects to fit a two-parameter line. It is also unnecessary, and the reason
is a property of the data rather than a tolerance:

**The exchange stamp is quantised to whole seconds, so packets share EXACT time values.**
For a set of points with the same `t`, only the one with the smallest lag can ever be an
active constraint of the envelope LP — every other point at that instant is implied by it.
So collapsing each exchange-second to its minimum lag is an EXACT reduction of the LP, not
a sample of it: the hull, the fitted line, and the offset are bit-for-bit the same as they
would be over every row. Measured on the real tape: 11,437,162 rows for 2026-08-11 reduce
to one point per observed second, and the hull of those is 15 vertices.

The filtering rules live in `exchange_feed_delay_observation`; this module applies them
vectorised in numpy and keeps the same counts, because the daily report needs to say how
many packets were unusable and why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pyarrow.dataset as pyarrow_dataset

from nse_algo_trader.clock_integrity.exchange_feed_delay_observation import (
    ABSENT_STAMP_CUTOFF,
    DelayObservationExtraction,
    ExchangeFeedDelayObservation,
)

DEFAULT_DEPTH_TAPE_ROOT = Path("/home/opc/nse_archive/depth_tape")

_ABSENT_STAMP_CUTOFF_MICROS = int(ABSENT_STAMP_CUTOFF.timestamp() * 1_000_000)


class DepthTapeUnavailableError(RuntimeError):
    """No tape parts for the requested session.

    Raised rather than returning an empty extraction: "the recorder did not run" and "the
    clock did not drift" are different facts and must not share a representation.
    """


@dataclass(frozen=True, slots=True)
class SessionDelaySample:
    """One session's reduced observations, with the account of what was dropped."""

    session_date: date
    extraction: DelayObservationExtraction
    distinct_exchange_seconds: int
    raw_row_count: int

    @property
    def observations(self) -> tuple[ExchangeFeedDelayObservation, ...]:
        return self.extraction.observations


def session_part_paths(
    session_date: date, *, tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT
) -> list[Path]:
    """Parquet parts for one session, ignoring the JSON reports written beside them."""
    session_directory = tape_root / f"session_date={session_date.isoformat()}"
    return sorted(session_directory.rglob("*.parquet"))


def sample_session_delays(
    session_date: date,
    *,
    tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT,
    part_paths: Sequence[Path] | None = None,
) -> SessionDelaySample:
    """Every packet of one session, reduced to one observation per exchange-second."""
    paths = list(part_paths) if part_paths is not None else session_part_paths(
        session_date, tape_root=tape_root
    )
    if not paths:
        raise DepthTapeUnavailableError(
            f"no depth-tape parts for {session_date.isoformat()} under {tape_root} — the "
            f"recorder not running is a different finding from a clock that did not drift"
        )
    table = pyarrow_dataset.dataset(
        [str(path) for path in paths], format="parquet"
    ).to_table(columns=["instrument_token", "exchange_time", "receipt_time"])
    exchange_micros = (
        table.column("exchange_time").to_numpy(zero_copy_only=False).astype("datetime64[us]")
    ).astype(np.int64)
    receipt_micros = (
        table.column("receipt_time").to_numpy(zero_copy_only=False).astype("datetime64[us]")
    ).astype(np.int64)
    tokens = table.column("instrument_token").to_numpy(zero_copy_only=False).astype(np.int64)
    raw_row_count = int(exchange_micros.size)

    present = exchange_micros >= _ABSENT_STAMP_CUTOFF_MICROS
    absent_count = int((~present).sum())
    exchange_micros, receipt_micros, tokens = (
        exchange_micros[present],
        receipt_micros[present],
        tokens[present],
    )
    lag_micros = receipt_micros - exchange_micros
    usable = lag_micros >= 0
    negative_count = int((~usable).sum())
    exchange_micros, lag_micros, tokens = (
        exchange_micros[usable],
        lag_micros[usable],
        tokens[usable],
    )

    observations = _minimum_lag_per_exchange_second(exchange_micros, lag_micros, tokens)
    return SessionDelaySample(
        session_date=session_date,
        extraction=DelayObservationExtraction(
            observations=observations,
            considered=raw_row_count,
            absent_exchange_stamp=absent_count,
            negative_lag=negative_count,
        ),
        distinct_exchange_seconds=len(observations),
        raw_row_count=raw_row_count,
    )


def _minimum_lag_per_exchange_second(
    exchange_micros: np.ndarray, lag_micros: np.ndarray, tokens: np.ndarray
) -> tuple[ExchangeFeedDelayObservation, ...]:
    """The exact reduction: one point per distinct exchange stamp, the smallest lag.

    Keeps the instrument that achieved the minimum, so a suspicious second stays traceable
    to a symbol rather than becoming an anonymous number.
    """
    if exchange_micros.size == 0:
        return ()
    order = np.lexsort((lag_micros, exchange_micros))
    sorted_micros = exchange_micros[order]
    _, first_index = np.unique(sorted_micros, return_index=True)
    chosen = order[first_index]
    return tuple(
        ExchangeFeedDelayObservation(
            instrument_token=int(tokens[index]),
            exchange_second=datetime.fromtimestamp(
                int(exchange_micros[index]) / 1_000_000, tz=UTC
            ),
            received_at=datetime.fromtimestamp(
                int(exchange_micros[index] + lag_micros[index]) / 1_000_000, tz=UTC
            ),
        )
        for index in chosen
    )


def available_session_dates(*, tape_root: Path = DEFAULT_DEPTH_TAPE_ROOT) -> list[date]:
    """Sessions the tape actually holds, read off the partition directories."""
    if not tape_root.exists():
        return []
    dates = []
    for directory in sorted(tape_root.glob("session_date=*")):
        try:
            dates.append(date.fromisoformat(directory.name.split("=", 1)[1]))
        except ValueError:
            continue
    return dates
