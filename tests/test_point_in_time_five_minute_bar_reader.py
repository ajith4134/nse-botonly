"""Tests for the point-in-time five-minute bar reader — `B12`, spec `docs/research/251`.

The reader's whole design is one idea: **there is no method that does not take `as_of`**. So the
most important test here is the one that inspects the signatures and fails if such a method ever
appears — a guarantee that lives in prose is a guarantee that lasts until someone adds a convenience
overload.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nse_algo_trader.historical_bars.point_in_time_five_minute_bar_reader import (
    FIVE_MINUTE_INTERVAL,
    BarAvailabilityError,
    FiveMinuteBar,
    PointInTimeFiveMinuteBarReader,
)

INDIA = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = datetime(2026, 8, 14, 9, 15, tzinfo=INDIA)


def _write_bars(path: Path, rows: list[tuple[int, str, str, float, str | None]]) -> None:
    """Build a `price_bars`-shaped table, matching the production schema exactly."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE price_bars ("
            " instrument_token INTEGER NOT NULL, bar_interval TEXT NOT NULL,"
            " bar_timestamp TEXT NOT NULL, open_price REAL NOT NULL, high_price REAL NOT NULL,"
            " low_price REAL NOT NULL, close_price REAL NOT NULL, volume INTEGER NOT NULL,"
            " open_interest INTEGER, availability_time TEXT, adjustment_basis_as_of TEXT,"
            " PRIMARY KEY (instrument_token, bar_interval, bar_timestamp))"
        )
        connection.executemany(
            "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price,"
            " high_price, low_price, close_price, volume, availability_time)"
            " VALUES (?,?,?,?,?,?,?,100,?)",
            [
                (token, interval, stamp, close, close, close, close, availability)
                for token, interval, stamp, close, availability in rows
            ],
        )


def _session_bars(token: int, count: int) -> list[tuple[int, str, str, float, str | None]]:
    """`count` consecutive five-minute bars, each available when its interval closes."""
    rows: list[tuple[int, str, str, float, str | None]] = []
    for index in range(count):
        opened = SESSION_OPEN + timedelta(minutes=5 * index)
        rows.append(
            (
                token,
                FIVE_MINUTE_INTERVAL,
                opened.isoformat(),
                100.0 + index,
                (opened + timedelta(minutes=5)).isoformat(),
            )
        )
    return rows


@pytest.fixture
def reader(tmp_path: Path) -> PointInTimeFiveMinuteBarReader:
    database = tmp_path / "market_data.sqlite3"
    _write_bars(database, _session_bars(738561, 12) + _session_bars(408065, 12))
    return PointInTimeFiveMinuteBarReader(database)


# ------------------------------------------------------------------ the guarantee itself


def test_no_reader_method_can_be_called_without_an_as_of() -> None:
    """The design IS the required parameter; prose alone would not survive a new overload."""
    public = [
        (name, member)
        for name, member in inspect.getmembers(
            PointInTimeFiveMinuteBarReader, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    ]
    assert public, "the reader exposes no public methods at all"
    for name, member in public:
        parameters = inspect.signature(member).parameters
        assert "as_of" in parameters, f"{name} can be called without an as_of"
        assert parameters["as_of"].default is inspect.Parameter.empty, (
            f"{name} defaults its as_of, so a caller can omit the cutoff without noticing"
        )


@pytest.mark.parametrize(
    "call",
    [
        lambda reader, moment: reader.bars_for_instrument(738561, moment),
        lambda reader, moment: reader.closes_for_instruments(moment),
        lambda reader, moment: reader.instruments_with_bars(moment),
    ],
    ids=["bars_for_instrument", "closes_for_instruments", "instruments_with_bars"],
)
def test_a_naive_as_of_is_refused_by_every_method(
    reader: PointInTimeFiveMinuteBarReader, call: object
) -> None:
    """Parametrised over all three, because it was not.

    An adversarial review mutated the refusal out of `closes_for_instruments` and
    `instruments_with_bars` and both mutants SURVIVED — only `bars_for_instrument` was covered. A
    naive instant does not merely go unguarded: `'2026-08-14T12:00:00'` sorts BELOW
    `'2026-08-14T12:00:00+05:30'` as text, so it silently drops the boundary bar.
    """
    with pytest.raises(BarAvailabilityError, match="timezone"):
        call(reader, datetime(2026, 8, 14, 12, 0))  # type: ignore[operator]  # noqa: DTZ001


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "zone", ["Asia/Kolkata", "UTC", "Asia/Tokyo", "Asia/Kathmandu", "Pacific/Honolulu"]
)
def test_the_same_instant_gives_the_same_answer_in_any_timezone(tmp_path: Path, zone: str) -> None:
    """The CRITICAL an adversarial review found, pinned.

    `availability_time` is TEXT, so `<=` is a STRING comparison, and lexical order over
    mixed-offset ISO-8601 is not chronological order. Passing the caller's spelling straight through
    meant one physical instant gave five different answers — a Tokyo-offset cutoff leaked 39 future
    bars up to 3h15m ahead on live data, while a UTC cutoff (`datetime.now(UTC)` appears 26 times
    elsewhere in `src/`) hid the entire session instead.
    """
    database = tmp_path / f"market_data_{zone.replace('/', '_')}.sqlite3"
    _write_bars(database, _session_bars(738561, 12))
    reader = PointInTimeFiveMinuteBarReader(database)

    truth = SESSION_OPEN + timedelta(minutes=30)
    in_ist = reader.bars_for_instrument(738561, truth)
    in_zone = reader.bars_for_instrument(738561, truth.astimezone(ZoneInfo(zone)))

    assert [bar.bar_timestamp for bar in in_zone] == [bar.bar_timestamp for bar in in_ist]
    assert all(bar.availability_time <= truth for bar in in_zone)
    assert reader.instruments_with_bars(truth.astimezone(ZoneInfo(zone))) == (
        reader.instruments_with_bars(truth)
    )
    assert reader.closes_for_instruments(truth.astimezone(ZoneInfo(zone))) == (
        reader.closes_for_instruments(truth)
    )


def test_asking_for_a_non_positive_number_of_bars_is_refused(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    """`most_recent=0` survived as a mutant: `<= 0` weakened to `< 0` changed nothing measurable."""
    for count in (0, -1):
        with pytest.raises(BarAvailabilityError, match="must be positive"):
            reader.bars_for_instrument(738561, SESSION_OPEN, most_recent=count)


# ------------------------------------------------------------------ the cutoff


def test_only_bars_already_available_are_returned(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    """The bar covering 09:15-09:20 is knowable at 09:20, not at 09:19."""
    just_before = reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(minutes=4))
    just_after = reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(minutes=5))
    assert just_before == ()
    assert len(just_after) == 1
    assert just_after[0].bar_timestamp == SESSION_OPEN


def test_the_visible_set_grows_monotonically_through_the_session(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    counts = [
        len(reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(minutes=minute)))
        for minute in range(0, 65, 5)
    ]
    assert counts == sorted(counts)
    assert counts[0] == 0
    assert counts[-1] > counts[0]


def test_most_recent_returns_the_latest_available_bars_not_the_earliest(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    """A sizer wants the last N closes; returning the first N would price yesterday's move."""
    as_of = SESSION_OPEN + timedelta(minutes=60)
    latest = reader.bars_for_instrument(738561, as_of, most_recent=3)
    everything = reader.bars_for_instrument(738561, as_of)
    assert [bar.bar_timestamp for bar in latest] == [bar.bar_timestamp for bar in everything[-3:]]


def test_bars_are_returned_oldest_first(reader: PointInTimeFiveMinuteBarReader) -> None:
    """Every consumer feeds these to a rolling statistic, which is order-dependent."""
    bars = reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(minutes=60))
    assert [bar.bar_timestamp for bar in bars] == sorted(bar.bar_timestamp for bar in bars)


def test_closes_for_instruments_respects_the_same_cutoff(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    early = reader.closes_for_instruments(SESSION_OPEN + timedelta(minutes=10))
    later = reader.closes_for_instruments(SESSION_OPEN + timedelta(minutes=60))
    assert set(early) == {738561, 408065}
    assert len(early[738561]) == 2
    assert len(later[738561]) == 12


def test_closes_can_be_restricted_to_a_named_universe(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    closes = reader.closes_for_instruments(
        SESSION_OPEN + timedelta(minutes=60), instrument_tokens={738561}
    )
    assert set(closes) == {738561}


def test_instruments_with_bars_asks_whether_a_bar_was_knowable_not_whether_it_exists(
    reader: PointInTimeFiveMinuteBarReader,
) -> None:
    """This replaces the `EXISTS (SELECT 1 FROM price_bars ...)` subqueries.

    Those ask "has any bar ever been recorded", which is true at 09:00 for a bar that will not exist
    until 15:25 — the wrong question, asked in the universe filter that decides what is tradeable.
    """
    assert reader.instruments_with_bars(SESSION_OPEN + timedelta(minutes=4)) == frozenset()
    assert reader.instruments_with_bars(SESSION_OPEN + timedelta(minutes=60)) == frozenset(
        {738561, 408065}
    )


# ------------------------------------------------------------------ adversarial


@pytest.mark.adversarial
def test_a_null_availability_time_is_refused_rather_than_silently_dropped(
    tmp_path: Path,
) -> None:
    """SQL three-valued logic makes a NULL fail `<=`, so the row vanishes without a word.

    The column is nullable at the schema level and today holds zero NULLs; a future writer inserting
    one would make bars disappear rather than leak. Safe in direction, silent in effect, and
    invisible without this check.
    """
    database = tmp_path / "market_data.sqlite3"
    _write_bars(
        database,
        [(738561, FIVE_MINUTE_INTERVAL, SESSION_OPEN.isoformat(), 100.0, None)],
    )
    reader = PointInTimeFiveMinuteBarReader(database)
    with pytest.raises(BarAvailabilityError, match="availability_time"):
        reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(hours=6))


@pytest.mark.adversarial
def test_a_daily_bar_sharing_the_table_is_not_returned_by_the_five_minute_reader(
    tmp_path: Path,
) -> None:
    """Guards the exact confusion that produced two wrong findings in one day (`O.115`)."""
    database = tmp_path / "market_data.sqlite3"
    rows = _session_bars(738561, 3)
    daily_availability = (SESSION_OPEN + timedelta(days=1)).isoformat()
    rows.append((738561, "day", SESSION_OPEN.isoformat(), 999.0, daily_availability))
    _write_bars(database, rows)
    reader = PointInTimeFiveMinuteBarReader(database)
    bars = reader.bars_for_instrument(738561, SESSION_OPEN + timedelta(days=2))
    assert all(bar.bar_interval == FIVE_MINUTE_INTERVAL for bar in bars)
    assert Decimal("999.0") not in [bar.close_price for bar in bars]


@pytest.mark.adversarial
def test_a_missing_table_is_an_error_not_an_empty_result(tmp_path: Path) -> None:
    """An empty answer from an absent store reads as 'nothing happened', which is a lie."""
    reader = PointInTimeFiveMinuteBarReader(tmp_path / "absent.sqlite3")
    with pytest.raises(BarAvailabilityError):
        reader.bars_for_instrument(738561, SESSION_OPEN)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(minutes=st.integers(min_value=-30, max_value=240))
def test_no_returned_bar_is_ever_available_after_the_as_of(tmp_path: Path, minutes: int) -> None:
    """The invariant the whole reader exists to hold, over arbitrary cutoffs."""
    database = tmp_path / f"market_data_{minutes}.sqlite3"
    _write_bars(database, _session_bars(738561, 12))
    reader = PointInTimeFiveMinuteBarReader(database)
    as_of = SESSION_OPEN + timedelta(minutes=minutes)
    for bar in reader.bars_for_instrument(738561, as_of):
        assert bar.availability_time <= as_of


def test_a_bar_record_rejects_an_availability_time_at_or_before_its_own_timestamp() -> None:
    """Measured against the live store: 0 of 1,246,985 rows violate this, so it is an invariant."""
    with pytest.raises(BarAvailabilityError, match="knowable before"):
        FiveMinuteBar(
            instrument_token=738561,
            bar_interval=FIVE_MINUTE_INTERVAL,
            bar_timestamp=SESSION_OPEN,
            open_price=Decimal("100"),
            high_price=Decimal("100"),
            low_price=Decimal("100"),
            close_price=Decimal("100"),
            volume=1,
            availability_time=SESSION_OPEN,
        )
