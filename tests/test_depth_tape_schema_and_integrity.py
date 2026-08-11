"""Tests for the depth-tape row contract, the feed seam's normalization, and the
per-instrument integrity classifier.

The adversarial cases here are drawn from what the live feed actually did during the
2026-08-11 probe: epoch-0 timestamps, an 11-minute-stale subscribe snapshot, and
naive datetimes whose correctness depends on the host's timezone.
"""

from __future__ import annotations

import os
import time as time_module
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as strategy

from nse_algo_trader.market_depth.depth_packet_integrity_classifier import (
    MINIMUM_SAMPLES_FOR_STALENESS_MATURITY,
    DepthPacketIntegrityClassifier,
)
from nse_algo_trader.market_depth.depth_tape_schema import (
    DEPTH_LEVELS_PER_SIDE,
    DEPTH_TAPE_ARROW_SCHEMA,
    DepthLevel,
    DepthPacket,
    IntegrityFlag,
    UnsupportedExchangeScaleError,
    exchange_time_from_epoch_seconds,
    packet_to_row,
    price_to_paise,
)
from nse_algo_trader.market_depth.live_depth_feed_seam import (
    depth_packet_from_kite_tick,
    epoch_seconds_from_sdk_timestamp,
)

IST = ZoneInfo("Asia/Kolkata")

EXPECTED_LAST_PRICE_PAISE = 125_000
EXPECTED_BEST_BID_PAISE = 124_995
EXPECTED_BEST_ASK_PAISE = 125_005
EXPECTED_RECEIPT_SEQUENCE = 7
EXPECTED_MATURITY_SAMPLES = 1000
SLOW_TO_FAST_THRESHOLD_RATIO = 10


def _levels(prices: list[int], quantity: int = 100) -> tuple[DepthLevel, ...]:
    return tuple(
        DepthLevel(price_paise=price, quantity=quantity, orders=3) for price in prices
    )


def _packet(
    *,
    token: int = 738561,
    exchange_time: datetime | None = None,
    receipt_time: datetime | None = None,
    sequence: int = 1,
    bids: tuple[DepthLevel, ...] | None = None,
    asks: tuple[DepthLevel, ...] | None = None,
    volume: int = 1_000,
) -> DepthPacket:
    """A well-formed packet on a real trading session, unless overridden."""
    default_receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    return DepthPacket(
        instrument_token=token,
        exchange="NSE",
        exchange_time=exchange_time,
        receipt_time=receipt_time or default_receipt,
        receipt_sequence=sequence,
        last_price_paise=125_000,
        last_traded_quantity=10,
        average_traded_price_paise=124_900,
        volume_traded=volume,
        total_buy_quantity=5_000,
        total_sell_quantity=4_000,
        open_interest=0,
        bids=bids or _levels([124_995, 124_990, 124_985, 124_980, 124_975]),
        asks=asks or _levels([125_005, 125_010, 125_015, 125_020, 125_025]),
    )


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_epoch_zero_is_absent_not_nineteen_seventy() -> None:
    """Measured at ~1 in 6 packets on the live probe. Stored as 1970 it would poison
    every time-ordered read and every staleness statistic in the tape."""
    assert exchange_time_from_epoch_seconds(0) is None
    assert exchange_time_from_epoch_seconds(None) is None
    assert exchange_time_from_epoch_seconds(-1) is None


@pytest.mark.unit
def test_exchange_time_is_utc_aware() -> None:
    parsed = exchange_time_from_epoch_seconds(1_786_000_000)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.unit
def test_price_to_paise_recovers_the_exact_wire_integer() -> None:
    """The SDK computed `raw / 100`; this must invert that without float drift."""
    for raw_paise in (1, 5, 99, 12_345, 999_999, 123_456_789):
        assert price_to_paise(raw_paise / 100, "NSE") == raw_paise


@pytest.mark.unit
def test_unknown_exchange_scale_is_fatal_not_defaulted() -> None:
    """A wrong divisor is wrong by orders of magnitude while looking plausible."""
    with pytest.raises(UnsupportedExchangeScaleError, match="CDS"):
        price_to_paise(83.25, "CDS")


@pytest.mark.unit
def test_row_matches_the_arrow_schema_field_for_field() -> None:
    row = packet_to_row(_packet(), IntegrityFlag.NONE)
    assert set(row) == set(DEPTH_TAPE_ARROW_SCHEMA.names)


@pytest.mark.unit
def test_staleness_is_none_when_exchange_time_is_absent() -> None:
    assert _packet(exchange_time=None).staleness_micros() is None


@pytest.mark.unit
def test_staleness_measures_the_gap_between_the_two_clocks() -> None:
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    packet = _packet(exchange_time=receipt - timedelta(seconds=11 * 60), receipt_time=receipt)
    assert packet.staleness_micros() == 11 * 60 * 1_000_000


# ------------------------------------------------------- the timezone hazard


@pytest.mark.unit
@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_sdk_naive_timestamp_inverts_correctly_on_any_host_timezone(
    host_timezone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK builds its value with `datetime.fromtimestamp(x)` and drops the zone.

    Reading it back with `.replace(tzinfo=UTC)` is correct only by accident of this
    host running UTC. This test runs the inversion under three host zones; it fails
    for the naive-replace implementation and passes for `.astimezone()`.
    """
    monkeypatch.setenv("TZ", host_timezone)
    time_module.tzset()
    try:
        original_epoch = 1_786_000_000
        sdk_value = datetime.fromtimestamp(original_epoch)  # noqa: DTZ006 — reproducing the SDK exactly
        assert sdk_value.tzinfo is None
        assert epoch_seconds_from_sdk_timestamp(sdk_value) == original_epoch
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time_module.tzset()


@pytest.mark.unit
def test_kite_tick_normalization_carries_every_level_and_the_true_instant() -> None:
    epoch = 1_786_000_000
    kite_tick = {
        "instrument_token": 738561,
        "last_price": 1250.0,
        "last_traded_quantity": 10,
        "average_traded_price": 1249.0,
        "volume_traded": 1000,
        "total_buy_quantity": 5000,
        "total_sell_quantity": 4000,
        "oi": 0,
        "exchange_timestamp": datetime.fromtimestamp(epoch),  # noqa: DTZ006 — the SDK's own shape
        "depth": {
            "buy": [{"price": 1249.95 - i * 0.05, "quantity": 100, "orders": 3} for i in range(5)],
            "sell": [{"price": 1250.05 + i * 0.05, "quantity": 100, "orders": 3} for i in range(5)],
        },
    }
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    packet = depth_packet_from_kite_tick(kite_tick, "NSE", receipt, 7)
    assert packet.exchange_time == datetime.fromtimestamp(epoch, UTC)
    assert packet.last_price_paise == EXPECTED_LAST_PRICE_PAISE
    assert len(packet.bids) == DEPTH_LEVELS_PER_SIDE
    assert packet.best_bid_paise == EXPECTED_BEST_BID_PAISE
    assert packet.best_ask_paise == EXPECTED_BEST_ASK_PAISE
    assert packet.receipt_sequence == EXPECTED_RECEIPT_SEQUENCE


@pytest.mark.unit
def test_quote_mode_tick_reaching_the_normalizer_is_surfaced_not_swallowed() -> None:
    """A depth-less tick here means the subscription mode was wrong — a defect to
    raise, not a row to quietly drop."""
    with pytest.raises(KeyError):
        depth_packet_from_kite_tick(
            {"instrument_token": 1, "last_price": 10.0},
            "NSE",
            datetime(2026, 8, 11, tzinfo=UTC),
            1,
        )


# ------------------------------------------------------------------ integrity


@pytest.fixture
def classifier() -> DepthPacketIntegrityClassifier:
    return DepthPacketIntegrityClassifier()


@pytest.mark.unit
def test_clean_packet_raises_no_flags(classifier: DepthPacketIntegrityClassifier) -> None:
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    assert (
        classifier.classify(_packet(exchange_time=receipt, receipt_time=receipt))
        == IntegrityFlag.NONE
    )


@pytest.mark.adversarial
def test_crossed_book_is_flagged(classifier: DepthPacketIntegrityClassifier) -> None:
    crossed = _packet(
        bids=_levels([125_010, 125_005, 125_000, 124_995, 124_990]),
        asks=_levels([125_005, 125_010, 125_015, 125_020, 125_025]),
    )
    assert IntegrityFlag.BOOK_CROSSED & classifier.classify(crossed)


@pytest.mark.adversarial
def test_empty_levels_are_not_a_crossed_book(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    """A zero price is an absent order, normal in an illiquid name and at the
    pre-open. Treating it as crossed would flag most of the pre-open."""
    empty = _packet(bids=_levels([0, 0, 0, 0, 0]), asks=_levels([0, 0, 0, 0, 0]))
    assert not (IntegrityFlag.BOOK_CROSSED & classifier.classify(empty))


@pytest.mark.adversarial
def test_short_depth_is_flagged(classifier: DepthPacketIntegrityClassifier) -> None:
    assert IntegrityFlag.DEPTH_SHAPE_UNEXPECTED & classifier.classify(
        _packet(bids=_levels([124_995, 124_990]))
    )


@pytest.mark.adversarial
def test_negative_level_values_are_flagged(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    malformed = _packet(bids=(DepthLevel(-1, 100, 3), *_levels([124_990] * 4)))
    assert IntegrityFlag.MALFORMED_LEVEL_VALUE & classifier.classify(malformed)


@pytest.mark.unit
def test_repeated_identical_book_is_flagged_duplicate_only_after_the_first(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    first, second = _packet(sequence=1), _packet(sequence=2)
    assert not (IntegrityFlag.DUPLICATE_OF_PREVIOUS_BOOK & classifier.classify(first))
    assert IntegrityFlag.DUPLICATE_OF_PREVIOUS_BOOK & classifier.classify(second)


@pytest.mark.unit
def test_a_changed_volume_alone_defeats_the_duplicate_test(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    classifier.classify(_packet(sequence=1, volume=1_000))
    assert not (
        IntegrityFlag.DUPLICATE_OF_PREVIOUS_BOOK
        & classifier.classify(_packet(sequence=2, volume=1_001))
    )


@pytest.mark.adversarial
def test_out_of_order_exchange_time_is_flagged(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    base = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    classifier.classify(_packet(exchange_time=base, sequence=1))
    assert IntegrityFlag.EXCHANGE_TIME_NOT_MONOTONIC & classifier.classify(
        _packet(exchange_time=base - timedelta(seconds=5), sequence=2, volume=2_000)
    )


@pytest.mark.adversarial
def test_one_late_packet_does_not_drag_the_watermark_backwards(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    """If a single out-of-order packet reset the reference, every in-order packet
    after it would be judged against a stale watermark and pass silently."""
    base = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    classifier.classify(_packet(exchange_time=base, sequence=1, volume=1))
    classifier.classify(
        _packet(exchange_time=base - timedelta(seconds=30), sequence=2, volume=2)
    )
    still_late = classifier.classify(
        _packet(exchange_time=base - timedelta(seconds=10), sequence=3, volume=3)
    )
    assert IntegrityFlag.EXCHANGE_TIME_NOT_MONOTONIC & still_late


@pytest.mark.adversarial
def test_packet_outside_the_quoting_window_is_flagged(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    after_close = datetime(2026, 8, 11, 16, 30, tzinfo=IST).astimezone(UTC)
    assert IntegrityFlag.OUTSIDE_SESSION_WINDOW & classifier.classify(
        _packet(receipt_time=after_close)
    )


@pytest.mark.adversarial
def test_packet_on_a_non_session_date_is_flagged(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    sunday = datetime(2026, 8, 9, 10, 0, tzinfo=IST).astimezone(UTC)
    assert IntegrityFlag.OUTSIDE_SESSION_WINDOW & classifier.classify(
        _packet(receipt_time=sunday)
    )


# ------------------------------------------------- the derived staleness threshold


@pytest.mark.unit
def test_staleness_flag_is_withheld_until_the_estimate_is_mature(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    """`R.04`: the algorithm is full-strength from the first packet; only its
    activation waits for enough data to mean anything."""
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    outrageous = _packet(
        exchange_time=receipt - timedelta(hours=3), receipt_time=receipt
    )
    assert not (
        IntegrityFlag.STALE_BEYOND_DERIVED_THRESHOLD & classifier.classify(outrageous)
    )
    assert not classifier.state_for(738561).staleness_threshold_is_mature


@pytest.mark.unit
def test_minimum_maturity_follows_from_the_quantile_not_from_a_guess() -> None:
    assert MINIMUM_SAMPLES_FOR_STALENESS_MATURITY == EXPECTED_MATURITY_SAMPLES


@pytest.mark.unit
def test_an_extreme_packet_is_flagged_once_the_instrument_is_mature(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    for sequence in range(MINIMUM_SAMPLES_FOR_STALENESS_MATURITY + 50):
        classifier.classify(
            _packet(
                exchange_time=receipt - timedelta(milliseconds=200),
                receipt_time=receipt,
                sequence=sequence,
                volume=sequence,
            )
        )
    assert classifier.state_for(738561).staleness_threshold_is_mature
    flags = classifier.classify(
        _packet(
            exchange_time=receipt - timedelta(seconds=600),
            receipt_time=receipt,
            sequence=99_999,
            volume=99_999,
        )
    )
    assert IntegrityFlag.STALE_BEYOND_DERIVED_THRESHOLD & flags


@pytest.mark.unit
def test_the_threshold_is_per_instrument_not_global(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    """A name quoting twice a second and one quoting every eight seconds have
    legitimately different notions of stale — the measured spread was 130x."""
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    for sequence in range(MINIMUM_SAMPLES_FOR_STALENESS_MATURITY + 50):
        classifier.classify(
            _packet(
                token=111,
                exchange_time=receipt - timedelta(milliseconds=10),
                receipt_time=receipt,
                sequence=sequence,
                volume=sequence,
            )
        )
        classifier.classify(
            _packet(
                token=222,
                exchange_time=receipt - timedelta(seconds=8),
                receipt_time=receipt,
                sequence=sequence,
                volume=sequence,
            )
        )
    fast = classifier.state_for(111).derived_staleness_threshold_micros()
    slow = classifier.state_for(222).derived_staleness_threshold_micros()
    assert fast is not None and slow is not None
    assert slow > fast * SLOW_TO_FAST_THRESHOLD_RATIO


@pytest.mark.unit
def test_a_packet_cannot_be_the_reason_it_is_judged_normal(
    classifier: DepthPacketIntegrityClassifier,
) -> None:
    """The observation must be tested against the estimate *before* updating it,
    otherwise a single enormous outlier partly excuses itself."""
    receipt = datetime(2026, 8, 11, 10, 0, tzinfo=IST).astimezone(UTC)
    for sequence in range(MINIMUM_SAMPLES_FOR_STALENESS_MATURITY + 50):
        classifier.classify(
            _packet(
                exchange_time=receipt - timedelta(milliseconds=100),
                receipt_time=receipt,
                sequence=sequence,
                volume=sequence,
            )
        )
    threshold_before = classifier.state_for(738561).derived_staleness_threshold_micros()
    flags = classifier.classify(
        _packet(
            exchange_time=receipt - timedelta(seconds=3600),
            receipt_time=receipt,
            sequence=1,
            volume=1,
        )
    )
    assert IntegrityFlag.STALE_BEYOND_DERIVED_THRESHOLD & flags
    assert threshold_before is not None


# ------------------------------------------------------------------- property


@settings(max_examples=120, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    best_bid=strategy.integers(min_value=1, max_value=10_000_000),
    spread=strategy.integers(min_value=1, max_value=1_000),
)
@pytest.mark.property
def test_an_uncrossed_book_is_never_flagged_crossed(best_bid: int, spread: int) -> None:
    classifier = DepthPacketIntegrityClassifier()
    packet = _packet(
        bids=_levels([best_bid - i for i in range(DEPTH_LEVELS_PER_SIDE)]),
        asks=_levels([best_bid + spread + i for i in range(DEPTH_LEVELS_PER_SIDE)]),
    )
    assert not (IntegrityFlag.BOOK_CROSSED & classifier.classify(packet))


@settings(max_examples=120)
@given(raw_paise=strategy.integers(min_value=0, max_value=1_000_000_000))
@pytest.mark.property
def test_paise_conversion_round_trips_for_every_representable_price(
    raw_paise: int,
) -> None:
    assert price_to_paise(raw_paise / 100, "NSE") == raw_paise


@settings(max_examples=80)
@given(epoch=strategy.integers(min_value=1, max_value=4_000_000_000))
@pytest.mark.property
def test_sdk_timestamp_inversion_round_trips_for_any_epoch(epoch: int) -> None:
    sdk_value = datetime.fromtimestamp(epoch)  # noqa: DTZ006 — reproducing the SDK
    assert epoch_seconds_from_sdk_timestamp(sdk_value) == epoch


@pytest.mark.unit
def test_host_timezone_is_not_assumed_by_the_test_suite_itself() -> None:
    """Guards the guard: if CI ever pins TZ, the timezone-hazard test above would
    silently stop exercising the case it exists for."""
    assert "TZ" not in os.environ or os.environ["TZ"] in ("", "UTC")
