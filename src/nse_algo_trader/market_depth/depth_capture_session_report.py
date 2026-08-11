"""The per-session usability verdict — the gate every consumer of the tape must pass.

A tape is not self-describing. Rows exist for an instrument whether it was covered for
the whole session or for ninety seconds, whether its book was crossed a hundred times
or never, whether packets were dropped to overflow or not. A consumer that reads the
tape without knowing which of those is true is reading data of unknown provenance,
which is exactly what `L0.12` exists to prevent.

So this module answers, per instrument-session: **is this usable for microstructure
work, usable with stated caveats, or not usable at all** — and says why, in the
instrument's own measured terms.

**The verdict thresholds are cross-sectional, not typed in.** What counts as poor
coverage is decided by comparing an instrument against its peers *in the same session*
using a Tukey lower fence, so a session where the whole feed was degraded does not
quietly pass every instrument, and a normal session does not fail instruments merely
for being illiquid. The only absolute rules are the ones that are logical rather than
tuned: no rows at all is unusable, and dropped packets are always at least a caveat
because the gap they leave is unmeasurable from the tape alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from itertools import pairwise
from pathlib import Path

from nse_algo_trader.market_depth.depth_tape_schema import IntegrityFlag
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader

MINIMUM_RECEIPTS_FOR_A_GAP = 2
"""An inter-arrival needs two arrivals; with fewer there is no gap to measure and the
instrument counts as wholly uncovered."""

TAPE_EPOCH_FLOOR = datetime(1970, 1, 1, tzinfo=UTC)
TAPE_EPOCH_CEILING = datetime(2100, 1, 1, tzinfo=UTC)
"""Bounds wide enough to select a whole session without `datetime.min`/`max`, whose
microsecond extremes overflow Arrow's timestamp range."""


class InstrumentSessionUsability(Enum):
    """What a consumer may do with one instrument's rows for one session."""

    USABLE = "usable"
    USABLE_WITH_CAVEATS = "usable_with_caveats"
    UNUSABLE = "unusable"


@dataclass(frozen=True)
class InstrumentSessionQuality:
    """Everything measured about one instrument's session, and the verdict from it."""

    instrument_token: int
    row_count: int
    first_receipt: datetime | None
    last_receipt: datetime | None
    covered_seconds: float
    session_seconds: float
    largest_gap_seconds: float
    flag_counts: Mapping[IntegrityFlag, int]
    dropped_packets: int
    usability: InstrumentSessionUsability
    reasons: tuple[str, ...]

    @property
    def coverage_fraction(self) -> float:
        return self.covered_seconds / self.session_seconds if self.session_seconds else 0.0

    def flag_share(self, flag: IntegrityFlag) -> float:
        return self.flag_counts.get(flag, 0) / self.row_count if self.row_count else 0.0


@dataclass(frozen=True)
class DepthCaptureSessionReport:
    """The session's verdict, instrument by instrument."""

    session_date: date
    tape_root: Path
    instrument_quality: tuple[InstrumentSessionQuality, ...]
    total_rows: int
    total_bytes: int
    coverage_lower_fence: float
    notes: tuple[str, ...]

    @property
    def bytes_per_row(self) -> float:
        return self.total_bytes / self.total_rows if self.total_rows else 0.0

    def usable_tokens(self, include_caveats: bool = True) -> tuple[int, ...]:
        """The tokens a consumer may read. This is the call site that changes behaviour."""
        allowed = {InstrumentSessionUsability.USABLE}
        if include_caveats:
            allowed.add(InstrumentSessionUsability.USABLE_WITH_CAVEATS)
        return tuple(
            quality.instrument_token
            for quality in self.instrument_quality
            if quality.usability in allowed
        )

    def count_by_usability(self) -> dict[InstrumentSessionUsability, int]:
        counts = dict.fromkeys(InstrumentSessionUsability, 0)
        for quality in self.instrument_quality:
            counts[quality.usability] += 1
        return counts

    def quality_for(self, instrument_token: int) -> InstrumentSessionQuality | None:
        return next(
            (
                quality
                for quality in self.instrument_quality
                if quality.instrument_token == instrument_token
            ),
            None,
        )


def tukey_lower_fence(values: Sequence[float]) -> float:
    """The conventional outlier boundary: Q1 - 1.5 * IQR.

    Used rather than a chosen cutoff so "poor coverage" means poor *relative to this
    session's own instruments*. A session in which the whole feed struggled therefore
    does not silently pass everything, and a healthy session does not fail instruments
    merely for trading rarely.
    """
    if not values:
        return 0.0
    ordered = sorted(values)

    def quantile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = fraction * (len(ordered) - 1)
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(ordered) - 1)
        weight = position - lower_index
        return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight

    first_quartile = quantile(0.25)
    third_quartile = quantile(0.75)
    return first_quartile - 1.5 * (third_quartile - first_quartile)


def _gap_statistics(
    receipt_times: Sequence[datetime], session_seconds: float
) -> tuple[float, float]:
    """(covered seconds, largest gap seconds) for one instrument's receipts.

    A gap is an inter-arrival longer than that instrument's own 99th-percentile
    inter-arrival — its own notion of a pause, not a global one. Coverage is the
    session time not inside such a gap.
    """
    if len(receipt_times) < MINIMUM_RECEIPTS_FOR_A_GAP:
        return (0.0, session_seconds)
    ordered = sorted(receipt_times)
    intervals = [
        (later - earlier).total_seconds()
        for earlier, later in pairwise(ordered)
    ]
    sorted_intervals = sorted(intervals)
    extreme_index = min(len(sorted_intervals) - 1, int(0.99 * len(sorted_intervals)))
    normal_interval = sorted_intervals[extreme_index]
    gap_seconds = sum(interval for interval in intervals if interval > normal_interval)
    span = (ordered[-1] - ordered[0]).total_seconds()
    outside_span = max(0.0, session_seconds - span)
    covered = max(0.0, span - gap_seconds)
    return (covered, max([*intervals, outside_span]))


def build_session_report(
    tape_root: Path,
    session_date: date,
    session_seconds: float,
    dropped_packets_by_token: Mapping[int, int] | None = None,
) -> DepthCaptureSessionReport:
    """Read a session's tape and judge every instrument in it."""
    reader = MarketDepthTapeReader(tape_root)
    dropped = dict(dropped_packets_by_token or {})
    tokens = reader.instrument_tokens(session_date)

    measured: list[tuple[int, int, list[datetime], dict[IntegrityFlag, int]]] = []
    total_rows = 0
    for token in tokens:
        table = reader.read_instrument_window(
            token,
            TAPE_EPOCH_FLOOR,
            TAPE_EPOCH_CEILING,
            session_date,
        )
        receipts = table.column("receipt_time").to_pylist()
        flag_values = table.column("integrity_flags").to_pylist()
        flag_counts: dict[IntegrityFlag, int] = {}
        for raw_flags in flag_values:
            flags = IntegrityFlag(raw_flags)
            for flag in IntegrityFlag:
                if flag is not IntegrityFlag.NONE and flag & flags:
                    flag_counts[flag] = flag_counts.get(flag, 0) + 1
        measured.append((token, table.num_rows, receipts, flag_counts))
        total_rows += table.num_rows

    coverage_by_token = {
        token: _gap_statistics(receipts, session_seconds)
        for token, _rows, receipts, _flags in measured
    }
    coverage_fractions = [
        covered / session_seconds if session_seconds else 0.0
        for covered, _largest in coverage_by_token.values()
    ]
    lower_fence = tukey_lower_fence(coverage_fractions)

    qualities: list[InstrumentSessionQuality] = []
    for token, row_count, receipts, flag_counts in measured:
        covered, largest_gap = coverage_by_token[token]
        coverage_fraction = covered / session_seconds if session_seconds else 0.0
        dropped_here = dropped.get(token, 0)
        reasons: list[str] = []
        usability = InstrumentSessionUsability.USABLE

        if row_count == 0:
            usability = InstrumentSessionUsability.UNUSABLE
            reasons.append("no rows captured")
        else:
            if coverage_fraction < lower_fence:
                usability = InstrumentSessionUsability.UNUSABLE
                reasons.append(
                    f"coverage {coverage_fraction:.1%} is below this session's lower "
                    f"fence of {lower_fence:.1%}"
                )
            if dropped_here:
                # The size of what a dropped packet would have contained cannot be
                # recovered from the tape, so this can never be a clean pass.
                usability = (
                    InstrumentSessionUsability.UNUSABLE
                    if usability is InstrumentSessionUsability.UNUSABLE
                    else InstrumentSessionUsability.USABLE_WITH_CAVEATS
                )
                reasons.append(f"{dropped_here:,} packets dropped to queue overflow")
            crossed = flag_counts.get(IntegrityFlag.BOOK_CROSSED, 0)
            if crossed and usability is InstrumentSessionUsability.USABLE:
                usability = InstrumentSessionUsability.USABLE_WITH_CAVEATS
                reasons.append(f"{crossed:,} crossed books")
            absent = flag_counts.get(IntegrityFlag.EXCHANGE_TIME_ABSENT, 0)
            if absent and usability is InstrumentSessionUsability.USABLE:
                usability = InstrumentSessionUsability.USABLE_WITH_CAVEATS
                reasons.append(f"{absent:,} rows carry no exchange timestamp")

        qualities.append(
            InstrumentSessionQuality(
                instrument_token=token,
                row_count=row_count,
                first_receipt=min(receipts) if receipts else None,
                last_receipt=max(receipts) if receipts else None,
                covered_seconds=covered,
                session_seconds=session_seconds,
                largest_gap_seconds=largest_gap,
                flag_counts=flag_counts,
                dropped_packets=dropped_here,
                usability=usability,
                reasons=tuple(reasons),
            )
        )

    notes: list[str] = []
    if session_seconds <= 0:
        notes.append("session length was not supplied; coverage is unmeasurable")
    total_dropped = sum(dropped.values())
    if total_dropped:
        notes.append(f"{total_dropped:,} packets were dropped to queue overflow overall")

    return DepthCaptureSessionReport(
        session_date=session_date,
        tape_root=tape_root,
        instrument_quality=tuple(qualities),
        total_rows=total_rows,
        total_bytes=reader.total_bytes_on_disk(),
        coverage_lower_fence=lower_fence,
        notes=tuple(notes),
    )
