"""Tests for the bitemporal bar store — written before the implementation (R.23 step 3).

The property that matters is availability filtering: a backtest must only see what
was knowable at the time. These tests are built to fail if that guarantee is
weakened, including the mutants the instrument-master review taught me to expect —
a boundary that accepts when it should refuse, a guard that returns early in the
case it exists for, and a filter applied to the wrong column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.bitemporal_bar_store import (
    BarRecord,
    BarStoreError,
    BarValidationError,
    BitemporalBarStore,
)

INDIA = timezone(timedelta(hours=5, minutes=30), name="IST")
IST_OFFSET = timedelta(hours=5, minutes=30)
REAL_BAR_SAMPLE_SIZE = 5000
EXPECTED_SEPARATE_CONTRACTS = 2
NEPAL = timezone(timedelta(hours=5, minutes=45), name="NPT")
NEW_YORK = timezone(timedelta(hours=-4), name="EDT")
OTHER_OFFSETS = (UTC, NEPAL, NEW_YORK, timezone(timedelta(hours=14), name="LINT"))
OPEN = datetime(2026, 8, 10, 9, 15, tzinfo=INDIA)
FIVE_MINUTES = timedelta(minutes=5)
RELIANCE = ("NSE", "NSE", "RELIANCE")


def _bar(
    *,
    at: datetime = OPEN,
    available_at: datetime | None = None,
    identity: tuple[str, str, str] = RELIANCE,
    close: str = "1066.20",
) -> BarRecord:
    return BarRecord(
        exchange=identity[0],
        segment=identity[1],
        tradingsymbol=identity[2],
        instrument_token=408065,
        bar_interval="5m",
        bar_timestamp=at,
        available_from=available_at if available_at else at + FIVE_MINUTES,
        open_price=Decimal("1055.00"),
        high_price=Decimal("1071.90"),
        low_price=Decimal("1055.00"),
        close_price=Decimal(close),
        volume=1_163_898,
        open_interest=None,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[BitemporalBarStore]:
    with BitemporalBarStore(tmp_path / "bars.sqlite3") as opened:
        yield opened


# --------------------------------------------------------------------------- the record


@pytest.mark.unit
def test_prices_are_exact_decimals(store: BitemporalBarStore) -> None:
    store.write([_bar()])
    stored = store.bars_as_of(OPEN + timedelta(hours=1))[0]
    assert isinstance(stored.close_price, Decimal)
    assert stored.close_price == Decimal("1066.20")


@pytest.mark.adversarial
def test_a_float_price_is_refused(store: BitemporalBarStore) -> None:
    """Accepting a float would silently reintroduce binary rounding into every indicator."""
    with pytest.raises(BarValidationError):
        BarRecord(
            exchange="NSE",
            segment="NSE",
            tradingsymbol="RELIANCE",
            instrument_token=1,
            bar_interval="5m",
            bar_timestamp=OPEN,
            available_from=OPEN + FIVE_MINUTES,
            open_price=1055.0,  # type: ignore[arg-type]
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=1,
            open_interest=None,
        )


@pytest.mark.adversarial
def test_a_naive_bar_timestamp_is_refused() -> None:
    """A naive datetime cannot be compared across venues; IST/UTC confusion is a money bug."""
    naive = datetime(2026, 8, 10, 9, 15)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(BarValidationError, match="timezone"):
        _bar(at=naive)


@pytest.mark.adversarial
def test_a_naive_availability_timestamp_is_refused() -> None:
    naive = datetime(2026, 8, 10, 9, 20)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(BarValidationError, match="timezone"):
        _bar(available_at=naive)


@pytest.mark.adversarial
def test_availability_before_the_bar_itself_is_refused() -> None:
    """A bar cannot have been actionable before it existed."""
    with pytest.raises(BarValidationError, match="available"):
        _bar(available_at=OPEN - timedelta(seconds=1))


@pytest.mark.unit
def test_availability_equal_to_the_bar_timestamp_is_allowed() -> None:
    """The boundary is inclusive: a tick-derived bar can be available immediately."""
    assert _bar(available_at=OPEN).available_from == OPEN


@pytest.mark.adversarial
def test_an_inverted_high_low_is_refused() -> None:
    """high < low is a corrupt bar, and it silently breaks every range calculation."""
    with pytest.raises(BarValidationError, match="high"):
        BarRecord(
            exchange="NSE",
            segment="NSE",
            tradingsymbol="RELIANCE",
            instrument_token=1,
            bar_interval="5m",
            bar_timestamp=OPEN,
            available_from=OPEN,
            open_price=Decimal("100"),
            high_price=Decimal("90"),
            low_price=Decimal("95"),
            close_price=Decimal("97"),
            volume=1,
            open_interest=None,
        )


@pytest.mark.adversarial
def test_a_negative_volume_is_refused() -> None:
    with pytest.raises(BarValidationError, match="volume"):
        BarRecord(
            exchange="NSE",
            segment="NSE",
            tradingsymbol="RELIANCE",
            instrument_token=1,
            bar_interval="5m",
            bar_timestamp=OPEN,
            available_from=OPEN,
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=-1,
            open_interest=None,
        )


@pytest.mark.adversarial
@pytest.mark.parametrize("poison", ["NaN", "Infinity"])
def test_non_finite_prices_are_refused(poison: str) -> None:
    with pytest.raises(BarValidationError):
        _bar(close=poison)


@pytest.mark.unit
def test_identity_is_the_stable_triple_not_the_token() -> None:
    """L0.02 — the token is Kite's transient handle and is reused after expiry."""
    bar = _bar()
    assert bar.identity == RELIANCE
    assert str(bar.instrument_token) not in bar.identity


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("field_name", "poison"),
    [
        ("open_price", Decimal("-100")),  # negative price inverts every return
        ("open_price", Decimal("0")),  # nothing on NSE trades at zero
        ("open_price", Decimal("99999")),  # open outside the bar's own range
        ("close_price", Decimal("-1")),
        ("close_price", Decimal("99999")),
    ],
)
def test_prices_outside_the_bars_own_range_are_refused(field_name: str, poison: Decimal) -> None:
    """The high/low check's own rationale, applied where review found it missing."""
    fields = {
        "exchange": "NSE",
        "segment": "NSE",
        "tradingsymbol": "RELIANCE",
        "instrument_token": 1,
        "bar_interval": "5m",
        "bar_timestamp": OPEN,
        "available_from": OPEN,
        "open_price": Decimal("100"),
        "high_price": Decimal("105"),
        "low_price": Decimal("95"),
        "close_price": Decimal("102"),
        "volume": 1,
        "open_interest": None,
    }
    fields[field_name] = poison
    with pytest.raises(BarValidationError):
        BarRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.adversarial
@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-12.5")])
def test_a_flat_non_positive_bar_is_refused(price: Decimal) -> None:
    """Isolates the sign check from the range check.

    Every price equal, so the bar is internally consistent and inside its own
    range — the only thing wrong with it is that nothing on NSE trades at or
    below zero. Mutation testing found the earlier cases were all killed by the
    range check instead, leaving the sign check unverified.
    """
    with pytest.raises(BarValidationError, match="positive"):
        BarRecord(
            exchange="NSE",
            segment="NSE",
            tradingsymbol="RELIANCE",
            instrument_token=1,
            bar_interval="5m",
            bar_timestamp=OPEN,
            available_from=OPEN,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1,
            open_interest=None,
        )


@pytest.mark.adversarial
@pytest.mark.parametrize(
    ("field_name", "poison"),
    [
        ("volume", 10.5),  # a float volume wrote cleanly, then broke the read
        ("volume", True),  # bool is an int; a flag is not a share count
        ("instrument_token", 0),
        ("instrument_token", -1),
        ("open_interest", -50),
        ("exchange", ""),
        ("tradingsymbol", "   "),
        ("bar_interval", ""),
    ],
)
def test_malformed_scalar_fields_are_refused(field_name: str, poison: object) -> None:
    fields = {
        "exchange": "NSE",
        "segment": "NSE",
        "tradingsymbol": "RELIANCE",
        "instrument_token": 1,
        "bar_interval": "5m",
        "bar_timestamp": OPEN,
        "available_from": OPEN,
        "open_price": Decimal("100"),
        "high_price": Decimal("105"),
        "low_price": Decimal("95"),
        "close_price": Decimal("102"),
        "volume": 1,
        "open_interest": None,
    }
    fields[field_name] = poison
    with pytest.raises(BarValidationError):
        BarRecord(**fields)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_a_malformed_stored_row_surfaces_as_a_store_error(tmp_path: Path) -> None:
    """A corrupt row must not escape as a raw ValueError from int()/Decimal().

    Reached by writing behind the store's back, which is the only way a bad row
    exists now that the record validates — but a hand-edited or externally
    migrated database is exactly that.
    """
    with BitemporalBarStore(tmp_path / "bars.sqlite3") as store:
        store.write([_bar()])
        store._connection.execute("UPDATE price_bar SET volume = 'not-a-number'")
        store._connection.commit()
        with pytest.raises(BarStoreError):
            store.bars_as_of(OPEN + timedelta(hours=1))


# --------------------------------------------------------------------------- availability


@pytest.mark.unit
def test_a_bar_is_invisible_before_it_was_available(store: BitemporalBarStore) -> None:
    """The whole point of the store: the 09:15 bar does not exist at 09:17."""
    store.write([_bar()])
    assert store.bars_as_of(OPEN + timedelta(minutes=2)) == []
    assert len(store.bars_as_of(OPEN + FIVE_MINUTES)) == 1


@pytest.mark.adversarial
def test_a_late_backfilled_bar_is_invisible_on_the_day_it_describes(
    store: BitemporalBarStore,
) -> None:
    """The failure mode that makes a backtest lie optimistically.

    A bar for Monday, backfilled on Wednesday, was never actionable on Monday. A
    store filtering on event time would hand it to a Monday backtest.
    """
    store.write([_bar(available_at=OPEN + timedelta(days=3))])
    assert store.bars_as_of(OPEN + timedelta(days=1)) == []
    assert len(store.bars_as_of(OPEN + timedelta(days=3))) == 1


@pytest.mark.adversarial
def test_the_availability_filter_is_inclusive_at_the_boundary(
    store: BitemporalBarStore,
) -> None:
    """Kills the `<` vs `<=` mutant on the read filter."""
    available = OPEN + FIVE_MINUTES
    store.write([_bar(available_at=available)])
    assert len(store.bars_as_of(available)) == 1
    assert store.bars_as_of(available - timedelta(microseconds=1)) == []


@pytest.mark.property
@given(
    minutes_late=hypothesis_strategies.integers(min_value=0, max_value=5000),
    minutes_asked=hypothesis_strategies.integers(min_value=-100, max_value=5000),
    write_offset_minutes=hypothesis_strategies.integers(min_value=-720, max_value=840),
    read_offset_minutes=hypothesis_strategies.integers(min_value=-720, max_value=840),
)
def test_no_read_ever_returns_a_bar_that_was_not_yet_available(
    tmp_path_factory: pytest.TempPathFactory,
    minutes_late: int,
    minutes_asked: int,
    write_offset_minutes: int,
    read_offset_minutes: int,
) -> None:
    """The invariant, stated so it can fail.

    Whatever the delay, whatever moment is asked for, **and whatever offset
    either side is expressed in**, a returned bar's availability must not be in
    the future relative to that moment.

    The offset arguments are the correction: the first version of this test
    built every timestamp from a single IST constant, so the mixed-offset
    string-comparison leak had no way to surface. The full UTC range is swept
    because the failure was never about India — it was about two spellings of
    the same instant sorting in the wrong order.
    """
    writer_zone = timezone(timedelta(minutes=write_offset_minutes))
    reader_zone = timezone(timedelta(minutes=read_offset_minutes))
    path = tmp_path_factory.mktemp("property") / "bars.sqlite3"
    with BitemporalBarStore(path) as store:
        available = (OPEN + timedelta(minutes=minutes_late)).astimezone(writer_zone)
        store.write([_bar(at=OPEN.astimezone(writer_zone), available_at=available)])
        as_of = (OPEN + timedelta(minutes=minutes_asked)).astimezone(reader_zone)
        for bar in store.bars_as_of(as_of):
            assert bar.available_from <= as_of


@pytest.mark.adversarial
@pytest.mark.parametrize("reader_zone", OTHER_OFFSETS)
def test_a_bar_cannot_leak_by_asking_in_a_different_offset(
    store: BitemporalBarStore, reader_zone: timezone
) -> None:
    """The defect adversarial review reproduced, pinned per offset.

    ISO strings compared as SQL TEXT are not chronologically ordered across
    differing offsets. Asking for an instant five minutes *before* availability,
    spelled in another zone, returned the bar — a backtest seeing the future,
    caused by nothing but string collation.
    """
    available = OPEN + FIVE_MINUTES
    store.write([_bar(available_at=available)])
    too_early = (available - timedelta(minutes=1)).astimezone(reader_zone)
    assert store.bars_as_of(too_early) == []
    assert len(store.bars_as_of(available.astimezone(reader_zone))) == 1


@pytest.mark.adversarial
def test_event_ordering_holds_across_mixed_offsets(store: BitemporalBarStore) -> None:
    """A TEXT sort on offset-bearing strings reversed real chronological order."""
    later = OPEN + FIVE_MINUTES
    store.write([_bar(at=later.astimezone(UTC), close="1070.55"), _bar(at=OPEN)])
    stored = store.bars_as_of(later + FIVE_MINUTES)
    assert [b.bar_timestamp.astimezone(UTC) for b in stored] == [
        OPEN.astimezone(UTC),
        later.astimezone(UTC),
    ]


@pytest.mark.adversarial
def test_the_same_instant_in_two_offsets_is_one_bar(store: BitemporalBarStore) -> None:
    """Identity is the instant, not its spelling — otherwise a UTC backfill duplicates
    every bar an IST ingest already wrote."""
    store.write([_bar(at=OPEN, close="1066.20")])
    store.write([_bar(at=OPEN.astimezone(UTC), close="1070.55")])
    stored = store.bars_as_of(OPEN + timedelta(hours=1))
    assert len(stored) == 1
    assert stored[0].close_price == Decimal("1070.55")


@pytest.mark.adversarial
def test_a_naive_as_of_is_refused(store: BitemporalBarStore) -> None:
    """A naive read moment does not raise on its own — it is silently taken as
    local time, shifting the cutoff by the host's offset. Refuse it."""
    store.write([_bar()])
    with pytest.raises(BarValidationError, match="timezone"):
        store.bars_as_of(datetime(2026, 8, 10, 9, 30))  # noqa: DTZ001 - deliberately naive


@pytest.mark.unit
def test_reads_can_be_scoped_to_one_instrument(store: BitemporalBarStore) -> None:
    other = ("NSE", "NSE", "INFY")
    store.write([_bar(), _bar(identity=other)])
    scoped = store.bars_as_of(OPEN + FIVE_MINUTES, identity=RELIANCE)
    assert [b.identity for b in scoped] == [RELIANCE]


@pytest.mark.unit
def test_reads_are_ordered_by_event_time(store: BitemporalBarStore) -> None:
    """Indicators consume a series; unordered bars would corrupt every rolling window.

    Symbol order is deliberately the *inverse* of event order, and every bar
    shares one availability instant. Without an explicit ORDER BY, the index
    scan returns them alphabetically — so this fails if the clause is dropped.
    A previous version wrote bars whose storage order already matched event
    order, which made the assertion true regardless of the implementation.
    """
    shared_availability = OPEN + timedelta(hours=2)
    minutes = [25, 20, 15, 10, 5, 0]
    store.write(
        [
            _bar(
                at=OPEN + timedelta(minutes=offset),
                available_at=shared_availability,
                identity=("NSE", "NSE", symbol),
            )
            for symbol, offset in zip("ABCDEF", minutes, strict=True)
        ]
    )
    stored = store.bars_as_of(shared_availability)
    assert [b.bar_timestamp for b in stored] == [
        OPEN + timedelta(minutes=offset) for offset in sorted(minutes)
    ]
    assert [b.tradingsymbol for b in stored] == list("FEDCBA")


# --------------------------------------------------------------------------- writes


@pytest.mark.unit
def test_rewriting_the_same_bar_is_idempotent(store: BitemporalBarStore) -> None:
    for _ in range(3):
        store.write([_bar()])
    assert len(store.bars_as_of(OPEN + timedelta(hours=1))) == 1


@pytest.mark.unit
def test_a_corrected_bar_replaces_the_original(store: BitemporalBarStore) -> None:
    """Exchanges do revise bars; the store must carry the correction, not both."""
    store.write([_bar(close="1066.20")])
    store.write([_bar(close="1070.55")])
    stored = store.bars_as_of(OPEN + timedelta(hours=1))
    assert len(stored) == 1
    assert stored[0].close_price == Decimal("1070.55")


@pytest.mark.unit
def test_two_instruments_sharing_a_reused_token_stay_separate(
    store: BitemporalBarStore,
) -> None:
    """L0.02 one layer down: keying on the token would merge their histories."""
    first = _bar(identity=("NFO", "NFO-OPT", "NIFTY26AUG24000CE"))
    second = _bar(identity=("NFO", "NFO-OPT", "NIFTY26SEP24000CE"))
    store.write([first, second])
    assert len(store.bars_as_of(OPEN + timedelta(hours=1))) == EXPECTED_SEPARATE_CONTRACTS


@pytest.mark.unit
def test_writing_nothing_is_harmless(store: BitemporalBarStore) -> None:
    store.write([])
    assert store.bars_as_of(OPEN + timedelta(hours=1)) == []


@pytest.mark.adversarial
def test_a_store_failure_raises_a_bar_store_error(tmp_path: Path) -> None:
    """A raw sqlite error must not escape the documented contract."""
    store = BitemporalBarStore(tmp_path / "bars.sqlite3")
    store.close()
    with pytest.raises(BarStoreError):
        store.write([_bar()])


@pytest.mark.unit
def test_the_store_uses_write_ahead_logging(store: BitemporalBarStore) -> None:
    with sqlite3.connect(store.database_path) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


@pytest.mark.unit
def test_timestamps_survive_a_round_trip_with_their_offset(
    store: BitemporalBarStore,
) -> None:
    """An offset lost in storage is an IST/UTC bug waiting to happen."""
    store.write([_bar()])
    stored = store.bars_as_of(OPEN + timedelta(hours=1))[0]
    assert stored.bar_timestamp == OPEN
    assert stored.bar_timestamp.utcoffset() == IST_OFFSET
    assert stored.bar_timestamp.astimezone(UTC) == OPEN.astimezone(UTC)


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
@pytest.mark.skipif(
    not Path("/home/opc/.nse_algo_trader/market_data.sqlite3").exists(),
    reason="the retained market-data store is not present",
)
def test_real_retained_bars_round_trip(tmp_path: Path) -> None:
    """R.05 — against the 659,990 bars the reset preserved.

    Also re-asserts the property measured before designing: every retained bar
    has an availability time at or after its event time.
    """
    source = sqlite3.connect(
        "file:/home/opc/.nse_algo_trader/market_data.sqlite3?mode=ro", uri=True
    )
    rows = source.execute(
        "SELECT instrument_token, bar_interval, bar_timestamp, open_price, high_price,"
        " low_price, close_price, volume, open_interest, availability_time"
        " FROM price_bars LIMIT 5000"
    ).fetchall()
    source.close()

    bars = [
        BarRecord(
            exchange="NSE",
            segment="NSE",
            tradingsymbol=f"TOKEN{row[0]}",
            instrument_token=row[0],
            bar_interval=row[1],
            bar_timestamp=datetime.fromisoformat(row[2]),
            available_from=datetime.fromisoformat(row[9]),
            open_price=Decimal(str(row[3])),
            high_price=Decimal(str(row[4])),
            low_price=Decimal(str(row[5])),
            close_price=Decimal(str(row[6])),
            volume=row[7],
            open_interest=row[8],
        )
        for row in rows
    ]
    assert len(bars) == REAL_BAR_SAMPLE_SIZE
    assert all(b.available_from >= b.bar_timestamp for b in bars)

    with BitemporalBarStore(tmp_path / "real.sqlite3") as store:
        store.write(bars)
        latest = max(b.available_from for b in bars)
        assert len(store.bars_as_of(latest)) == len({b.storage_key for b in bars})
