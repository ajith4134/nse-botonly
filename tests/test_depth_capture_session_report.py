"""The session report's verdicts — the gate a tape consumer must pass.

The point under test throughout is that the verdict is *earned from measurement*: an
instrument is failed for being an outlier among its own session's peers, not for
falling under a number someone typed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.market_depth.depth_capture_session_report import (
    InstrumentSessionUsability,
    build_session_report,
    tukey_lower_fence,
)
from nse_algo_trader.market_depth.depth_tape_schema import (
    DepthLevel,
    DepthPacket,
    IntegrityFlag,
)
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeStore

SESSION_DATE = date(2026, 8, 11)
SESSION_OPEN = datetime(2026, 8, 11, 3, 45, tzinfo=UTC)
SESSION_SECONDS = 6.5 * 3600
STEADY_TOKEN = 1001
SPARSE_TOKEN = 1002
EXPECTED_QUARTILE_FENCE = -0.5
HEALTHY_INSTRUMENT_COUNT = 10
NEGLIGIBLE_COVERAGE = 0.01
MEASURED_ROW_COUNT = 400
HALF = 0.5
MINIMUM_INTERRUPTION_GAP_SECONDS = 2600
NEARLY_THE_WHOLE_SESSION = 0.9


def _packet(token: int, sequence: int, receipt: datetime, price: int = 100) -> DepthPacket:
    return DepthPacket(
        instrument_token=token,
        exchange="NSE",
        exchange_time=receipt,
        receipt_time=receipt,
        receipt_sequence=sequence,
        last_price_paise=price,
        last_traded_quantity=1,
        average_traded_price_paise=price,
        volume_traded=sequence,
        total_buy_quantity=1,
        total_sell_quantity=1,
        open_interest=0,
        bids=tuple(DepthLevel(price - 1 - i, 1, 1) for i in range(5)),
        asks=tuple(DepthLevel(price + 1 + i, 1, 1) for i in range(5)),
    )


def _write_tape(
    tape_root: Path,
    packets_by_token: dict[int, list[DepthPacket]],
    flags_by_token: dict[int, IntegrityFlag] | None = None,
) -> None:
    flags = flags_by_token or {}
    with MarketDepthTapeStore(
        tape_root=tape_root,
        session_date=SESSION_DATE,
        shard_index=0,
        max_buffered_rows=500,
        max_seconds_between_flushes=3600.0,
        capture_run_id="testrun",
    ) as store:
        for token, packets in packets_by_token.items():
            for packet in packets:
                store.append(packet, flags.get(token, IntegrityFlag.NONE))


def _evenly_spaced(token: int, count: int, spacing_seconds: float) -> list[DepthPacket]:
    return [
        _packet(token, index + 1, SESSION_OPEN + timedelta(seconds=index * spacing_seconds))
        for index in range(count)
    ]


@pytest.mark.unit
def test_tukey_fence_is_the_conventional_boundary() -> None:
    assert tukey_lower_fence([1.0, 2.0, 3.0, 4.0]) == pytest.approx(EXPECTED_QUARTILE_FENCE)


@pytest.mark.unit
def test_tukey_fence_of_nothing_is_zero() -> None:
    assert tukey_lower_fence([]) == 0.0


@pytest.mark.unit
def test_a_single_value_has_no_spread_so_the_fence_is_that_value() -> None:
    assert tukey_lower_fence([0.9]) == pytest.approx(0.9)


@pytest.mark.unit
def test_evenly_covered_instruments_are_usable(tmp_path: Path) -> None:
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)},
    )
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    assert (
        report.count_by_usability()[InstrumentSessionUsability.USABLE]
        == HEALTHY_INSTRUMENT_COUNT
    )
    assert len(report.usable_tokens()) == HEALTHY_INSTRUMENT_COUNT


@pytest.mark.unit
def test_an_instrument_that_stopped_early_is_failed_against_its_peers(
    tmp_path: Path,
) -> None:
    """The verdict is relative: this instrument is failed because the others in the
    same session did far better, not because it fell under a typed-in number."""
    packets = {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)}
    packets[SPARSE_TOKEN] = _evenly_spaced(SPARSE_TOKEN, 6, 10.0)
    _write_tape(tmp_path, packets)
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    sparse = report.quality_for(SPARSE_TOKEN)
    assert sparse is not None
    assert sparse.usability is InstrumentSessionUsability.UNUSABLE
    assert any("lower fence" in reason for reason in sparse.reasons)
    assert SPARSE_TOKEN not in report.usable_tokens()


@pytest.mark.unit
def test_a_uniformly_degraded_session_does_not_silently_pass_everything(
    tmp_path: Path,
) -> None:
    """A cross-sectional fence cannot flag a session where everything was equally bad,
    so the report must say so through coverage rather than through a clean verdict."""
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 5, 10.0) for token in range(1001, 1011)},
    )
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    for quality in report.instrument_quality:
        assert quality.coverage_fraction < NEGLIGIBLE_COVERAGE


@pytest.mark.unit
def test_dropped_packets_are_always_at_least_a_caveat(tmp_path: Path) -> None:
    """What a dropped packet contained cannot be recovered from the tape, so a clean
    pass is never available once any were lost."""
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)},
    )
    report = build_session_report(
        tmp_path, SESSION_DATE, SESSION_SECONDS, dropped_packets_by_token={STEADY_TOKEN: 12}
    )
    quality = report.quality_for(STEADY_TOKEN)
    assert quality is not None
    assert quality.usability is InstrumentSessionUsability.USABLE_WITH_CAVEATS
    assert any("overflow" in reason for reason in quality.reasons)
    assert any("overflow" in note for note in report.notes)


@pytest.mark.unit
def test_crossed_books_downgrade_to_caveats(tmp_path: Path) -> None:
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)},
        flags_by_token={STEADY_TOKEN: IntegrityFlag.BOOK_CROSSED},
    )
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    quality = report.quality_for(STEADY_TOKEN)
    assert quality is not None
    assert quality.usability is InstrumentSessionUsability.USABLE_WITH_CAVEATS
    assert quality.flag_share(IntegrityFlag.BOOK_CROSSED) == 1.0


@pytest.mark.unit
def test_missing_exchange_timestamps_downgrade_to_caveats(tmp_path: Path) -> None:
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)},
        flags_by_token={STEADY_TOKEN: IntegrityFlag.EXCHANGE_TIME_ABSENT},
    )
    quality = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS).quality_for(
        STEADY_TOKEN
    )
    assert quality is not None
    assert quality.usability is InstrumentSessionUsability.USABLE_WITH_CAVEATS


@pytest.mark.unit
def test_excluding_caveats_narrows_the_usable_set(tmp_path: Path) -> None:
    """The knob a cautious consumer actually turns."""
    _write_tape(
        tmp_path,
        {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)},
        flags_by_token={STEADY_TOKEN: IntegrityFlag.BOOK_CROSSED},
    )
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    assert STEADY_TOKEN in report.usable_tokens(include_caveats=True)
    assert STEADY_TOKEN not in report.usable_tokens(include_caveats=False)


@pytest.mark.adversarial
def test_an_instrument_with_one_packet_is_wholly_uncovered(tmp_path: Path) -> None:
    packets = {token: _evenly_spaced(token, 200, 100.0) for token in range(1001, 1011)}
    packets[SPARSE_TOKEN] = _evenly_spaced(SPARSE_TOKEN, 1, 1.0)
    _write_tape(tmp_path, packets)
    quality = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS).quality_for(
        SPARSE_TOKEN
    )
    assert quality is not None
    assert quality.covered_seconds == 0.0
    assert quality.usability is InstrumentSessionUsability.UNUSABLE


@pytest.mark.adversarial
def test_a_zero_length_session_is_reported_not_divided_by(tmp_path: Path) -> None:
    _write_tape(tmp_path, {STEADY_TOKEN: _evenly_spaced(STEADY_TOKEN, 10, 1.0)})
    report = build_session_report(tmp_path, SESSION_DATE, 0.0)
    assert any("unmeasurable" in note for note in report.notes)
    assert report.instrument_quality[0].coverage_fraction == 0.0


@pytest.mark.unit
def test_the_report_measures_the_tape_it_read(tmp_path: Path) -> None:
    _write_tape(tmp_path, {STEADY_TOKEN: _evenly_spaced(STEADY_TOKEN, MEASURED_ROW_COUNT, 10.0)})
    report = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS)
    assert report.total_rows == MEASURED_ROW_COUNT
    assert report.total_bytes > 0
    assert report.bytes_per_row > 0


@pytest.mark.adversarial
def test_the_gap_threshold_is_the_instruments_own_extreme_interval(tmp_path: Path) -> None:
    """Coverage only means something if the p99 interval is really computed. With
    uniform spacing any index gives the same answer, so this uses UNEVEN spacing where
    picking the wrong quantile index changes the verdict."""
    steady = [
        _packet(STEADY_TOKEN, i + 1, SESSION_OPEN + timedelta(seconds=i * 10))
        for i in range(200)
    ]
    # One genuine hour-long hole, after which normal quoting resumes.
    interrupted = steady[:100] + [
        _packet(STEADY_TOKEN, 100 + i, SESSION_OPEN + timedelta(seconds=3600 + i * 10))
        for i in range(100)
    ]
    _write_tape(tmp_path, {STEADY_TOKEN: interrupted})
    quality = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS).quality_for(
        STEADY_TOKEN
    )
    assert quality is not None
    # The hole is excluded from coverage, so covered time is far below the span.
    span = (interrupted[-1].receipt_time - interrupted[0].receipt_time).total_seconds()
    assert quality.covered_seconds < span * HALF
    assert quality.largest_gap_seconds >= MINIMUM_INTERRUPTION_GAP_SECONDS


@pytest.mark.adversarial
def test_largest_gap_accounts_for_time_outside_the_observed_span(tmp_path: Path) -> None:
    """An instrument quoting for one minute of a six-hour session has a gap of nearly
    the whole session, even though every interval inside its span was tiny."""
    brief = [
        _packet(STEADY_TOKEN, i + 1, SESSION_OPEN + timedelta(seconds=i))
        for i in range(60)
    ]
    _write_tape(tmp_path, {STEADY_TOKEN: brief})
    quality = build_session_report(tmp_path, SESSION_DATE, SESSION_SECONDS).quality_for(
        STEADY_TOKEN
    )
    assert quality is not None
    assert quality.largest_gap_seconds > SESSION_SECONDS * NEARLY_THE_WHOLE_SESSION
