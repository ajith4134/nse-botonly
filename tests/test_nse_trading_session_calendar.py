"""Tests for the NSE session calendar — written before the implementation (R.23 step 3).

This component exists to answer one question the universe engine cannot answer for
itself: *should* there have been a file on this date? Its failure mode is not a
crash, it is a confident wrong answer — calling a holiday a collection gap, or
worse, calling a genuine gap a holiday and so hiding missing data.

The tests are therefore built around the two directions of that error, plus the
limitation measured before writing any code: the library's holiday rules are not
applied at all before 1997, and a calendar that silently returns "every weekday is
a session" for 1995 is more dangerous than one that refuses to answer.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas
import pytest

from nse_algo_trader.nse_trading_session_calendar import (
    NseTradingSessionCalendar,
    TradingCalendarError,
)

REPUBLIC_DAY_2024 = date(2024, 1, 26)
HOLI_2024 = date(2024, 3, 25)
INDEPENDENCE_DAY_2024 = date(2024, 8, 15)
A_NORMAL_TUESDAY = date(2024, 1, 30)
A_SATURDAY = date(2024, 1, 27)
FIRST_RELIABLE_YEAR = 1997
A_YEAR_BEFORE_THE_RULES_APPLY = 1996
JANUARY_2024_SESSIONS = 22


@pytest.fixture(scope="module")
def calendar() -> NseTradingSessionCalendar:
    return NseTradingSessionCalendar()


# --------------------------------------------------------------------------- sessions


@pytest.mark.unit
def test_a_normal_weekday_is_a_session(calendar: NseTradingSessionCalendar) -> None:
    assert calendar.is_trading_session(A_NORMAL_TUESDAY)


@pytest.mark.unit
def test_a_weekend_is_not_a_session(calendar: NseTradingSessionCalendar) -> None:
    assert not calendar.is_trading_session(A_SATURDAY)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("holiday", "name"),
    [(REPUBLIC_DAY_2024, "Republic Day"), (HOLI_2024, "Holi"),
     (INDEPENDENCE_DAY_2024, "Independence Day")],
)
def test_known_nse_holidays_are_not_sessions(
    calendar: NseTradingSessionCalendar, holiday: date, name: str
) -> None:
    """Named holidays, so a wrong calendar fails with something readable."""
    assert not calendar.is_trading_session(holiday), f"{name} should not be a session"


@pytest.mark.unit
def test_sessions_between_counts_a_known_month(calendar: NseTradingSessionCalendar) -> None:
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    assert len(sessions) == JANUARY_2024_SESSIONS
    assert REPUBLIC_DAY_2024 not in sessions
    assert sessions == tuple(sorted(sessions))


@pytest.mark.adversarial
def test_an_inverted_range_is_refused(calendar: NseTradingSessionCalendar) -> None:
    """Silently returning nothing would read as 'no sessions', which is a lie."""
    with pytest.raises(TradingCalendarError, match="after"):
        calendar.sessions_between(date(2024, 3, 1), date(2024, 1, 1))


@pytest.mark.unit
def test_a_single_day_range_is_allowed(calendar: NseTradingSessionCalendar) -> None:
    assert calendar.sessions_between(A_NORMAL_TUESDAY, A_NORMAL_TUESDAY) == (A_NORMAL_TUESDAY,)


# --------------------------------------------------------------------------- reliability


@pytest.mark.unit
def test_the_fence_excludes_zero_coverage_years_from_its_population() -> None:
    """Including them dragged the fence below zero, declaring everything reliable.

    Zero is not a low sample from the coverage distribution; it is the absence of
    coverage. Pinning this keeps the derivation from silently regressing.
    """
    calendar = NseTradingSessionCalendar()
    assert calendar.reliability_lower_fence() > 0


@pytest.mark.unit
def test_the_reliability_fence_is_derived_not_declared(
    calendar: NseTradingSessionCalendar,
) -> None:
    """R.03 — the threshold comes from the calendar's own output.

    It is Tukey's lower fence over the per-year recognised-holiday counts, so it
    moves if the library's coverage changes rather than being frozen here.
    """
    fence = calendar.reliability_lower_fence()
    assert 0 < fence < calendar.year_reliability(2024).recognised_holiday_count


@pytest.mark.unit
@pytest.mark.parametrize("year", [1990, 1994, 1995, 1996, 2027, 2028, 2030])
def test_years_with_no_holiday_rules_are_reported_unreliable(
    calendar: NseTradingSessionCalendar, year: int
) -> None:
    """Measured: these years recognise ZERO holidays — every weekday is a 'session'.

    A caller that trusted this would classify every 1995 holiday as a missing file
    and 'discover' an outage that never happened. **The future years matter just as
    much**: the library has no published rules past 2026, so it would report
    2028-01-26 (Republic Day) as a trading session.
    """
    verdict = calendar.year_reliability(year)
    assert verdict.recognised_holiday_count == 0
    assert not verdict.is_reliable


@pytest.mark.adversarial
def test_an_implausibly_low_but_nonzero_year_is_caught_by_the_fence() -> None:
    """1998 recognises 4 holidays. India has ~13-17. Zero-checking alone misses it.

    This is the case that justifies deriving a fence at all rather than just
    testing for zero.
    """
    calendar = NseTradingSessionCalendar()
    verdict = calendar.year_reliability(1998)
    assert verdict.recognised_holiday_count > 0
    assert not verdict.is_reliable


@pytest.mark.unit
@pytest.mark.parametrize("year", [2015, 2020, 2024, 2026])
def test_well_covered_years_are_reported_reliable(
    calendar: NseTradingSessionCalendar, year: int
) -> None:
    assert calendar.year_reliability(year).is_reliable


@pytest.mark.adversarial
def test_an_unreliable_year_refuses_to_answer_when_asked_strictly(
    calendar: NseTradingSessionCalendar,
) -> None:
    """The safe default is refusal, not a plausible-looking guess."""
    assert calendar.is_trading_session(date(1995, 6, 15))  # permissive read still works
    with pytest.raises(TradingCalendarError, match="unreliable"):
        calendar.sessions_between(date(1995, 1, 1), date(1995, 12, 31), require_reliable=True)


@pytest.mark.unit
def test_a_range_spanning_the_reliability_boundary_names_the_bad_years(
    calendar: NseTradingSessionCalendar,
) -> None:
    report = calendar.classify_observed_dates(
        observed=frozenset(), start=date(1996, 1, 1), end=date(1998, 12, 31)
    )
    assert A_YEAR_BEFORE_THE_RULES_APPLY in report.unreliable_years
    assert FIRST_RELIABLE_YEAR not in report.unreliable_years


# --------------------------------------------------------------------------- classification


@pytest.mark.unit
def test_a_missing_session_is_reported_as_uncollected(
    calendar: NseTradingSessionCalendar,
) -> None:
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    observed = frozenset(sessions) - {sessions[5]}
    report = calendar.classify_observed_dates(observed, date(2024, 1, 1), date(2024, 1, 31))
    assert report.uncollected_sessions == (sessions[5],)


@pytest.mark.unit
def test_a_holiday_is_never_reported_as_uncollected(
    calendar: NseTradingSessionCalendar,
) -> None:
    """The whole point: nothing was published, so nothing is missing."""
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    report = calendar.classify_observed_dates(
        frozenset(sessions), date(2024, 1, 1), date(2024, 1, 31)
    )
    assert report.uncollected_sessions == ()
    assert REPUBLIC_DAY_2024 not in report.uncollected_sessions


@pytest.mark.adversarial
def test_a_file_on_a_non_session_is_surfaced_not_discarded(
    calendar: NseTradingSessionCalendar,
) -> None:
    """Data exists for a day the calendar says was closed.

    One of the two is wrong and the caller must be told which dates disagree —
    silently dropping the observation would hide a real calendar defect, and
    silently keeping it would hide a real data defect.
    """
    report = calendar.classify_observed_dates(
        observed=frozenset({REPUBLIC_DAY_2024, A_NORMAL_TUESDAY}),
        start=date(2024, 1, 1), end=date(2024, 1, 31),
    )
    assert report.observed_non_sessions == (REPUBLIC_DAY_2024,)


@pytest.mark.unit
def test_observations_outside_the_window_are_ignored_not_counted(
    calendar: NseTradingSessionCalendar,
) -> None:
    report = calendar.classify_observed_dates(
        observed=frozenset({date(2023, 12, 5)}), start=date(2024, 1, 1), end=date(2024, 1, 31)
    )
    assert date(2023, 12, 5) not in report.observed_non_sessions
    assert len(report.uncollected_sessions) == JANUARY_2024_SESSIONS


@pytest.mark.property
@pytest.mark.parametrize("dropped", [0, 1, 5, 11, 21])
def test_every_expected_session_is_either_observed_or_uncollected(
    calendar: NseTradingSessionCalendar, dropped: int
) -> None:
    """The partition invariant — no session may fall through the classification."""
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    observed = frozenset(sessions[dropped:])
    report = calendar.classify_observed_dates(observed, date(2024, 1, 1), date(2024, 1, 31))
    assert set(report.expected_sessions) == observed | set(report.uncollected_sessions)
    assert len(report.uncollected_sessions) == dropped


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
def test_the_retained_collection_gap_is_resolved_correctly(
    calendar: NseTradingSessionCalendar,
) -> None:
    """R.05 — against the real retained F&O collection window.

    The measured truth (`docs/research/204` §2.1): of five weekdays with no file,
    `2026-06-26` is an NSE holiday and the other four are a genuine outage. My own
    first reading of this counted all five as gaps; this test pins the correction.
    """
    import sqlite3

    connection = sqlite3.connect(
        "file:/home/opc/.nse_algo_trader/market_data.sqlite3?mode=ro", uri=True
    )
    observed = frozenset(
        date.fromisoformat(row[0])
        for row in connection.execute("SELECT DISTINCT trade_date FROM fo_bhavcopy_contracts")
    )
    connection.close()

    report = calendar.classify_observed_dates(observed, min(observed), max(observed))
    assert date(2026, 6, 26) not in report.uncollected_sessions
    assert report.uncollected_sessions == (
        date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31),
    )
    assert report.observed_non_sessions == ()
    assert report.unreliable_years == ()


# --------------------------------------------------------------- review corrections
# Everything below was added after an adversarial review found 9 defects and 8
# surviving mutants. Each test names the specific failure it pins.


@pytest.mark.adversarial
def test_a_datetime_is_refused_rather_than_silently_missing(
    calendar: NseTradingSessionCalendar,
) -> None:
    """`datetime` is a SUBCLASS of `date`, so `isinstance` admitted it.

    Membership against a set of dates then failed for the very same calendar day:
    `is_trading_session(datetime(2024,1,30))` returned False while the date form
    returned True. A confident wrong answer, which is the one outcome this module
    is not allowed to produce.
    """
    with pytest.raises(TradingCalendarError, match="moment is not a trading day"):
        calendar.is_trading_session(datetime(2024, 1, 30))  # noqa: DTZ001 - the point


@pytest.mark.adversarial
def test_a_pandas_timestamp_is_refused(calendar: NseTradingSessionCalendar) -> None:
    """The library itself hands back Timestamps, so this is not an exotic input."""
    timestamp = pandas.Timestamp("2024-01-30")
    assert isinstance(timestamp, date)  # the trap, stated
    with pytest.raises(TradingCalendarError, match="moment is not a trading day"):
        calendar.is_trading_session(timestamp)


@pytest.mark.adversarial
def test_holidays_in_an_unreliable_year_are_not_reported_as_collection_gaps(
    calendar: NseTradingSessionCalendar,
) -> None:
    """The defect that defeated the module's whole purpose.

    1993 has no holiday rules, so the library calls every weekday a session. An
    empty collection previously produced 261 'uncollected sessions' at 0.0
    completeness — including Republic Day — reading as a total outage for a year
    that was merely unverified. Those dates must be partitioned out.
    """
    report = calendar.classify_observed_dates(
        frozenset(), date(1993, 1, 1), date(1993, 12, 31)
    )
    assert report.uncollected_sessions == ()
    assert date(1993, 1, 26) in report.unverifiable_dates
    assert report.has_unverifiable_dates
    assert report.collection_completeness is None


@pytest.mark.unit
def test_a_mixed_window_separates_real_gaps_from_unverifiable_ones(
    calendar: NseTradingSessionCalendar,
) -> None:
    """A window spanning the reliability boundary must not blur the two."""
    report = calendar.classify_observed_dates(
        frozenset(), date(1996, 12, 1), date(1997, 1, 31)
    )
    assert all(day.year == FIRST_RELIABLE_YEAR for day in report.uncollected_sessions)
    assert all(day.year == 1996 for day in report.unverifiable_dates)
    assert report.uncollected_sessions and report.unverifiable_dates


@pytest.mark.property
@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2024, 1, 1), date(2024, 1, 31)), (date(1996, 6, 1), date(1997, 6, 30)),
     (date(2026, 12, 1), date(2027, 1, 31)), (date(2024, 2, 29), date(2024, 2, 29))],
)
def test_the_three_way_partition_is_exhaustive_and_disjoint(
    calendar: NseTradingSessionCalendar, start: date, end: date
) -> None:
    """No expected session may fall through, and none may be counted twice."""
    report = calendar.classify_observed_dates(frozenset(), start, end)
    observed = set(report.observed_sessions)
    uncollected = set(report.uncollected_sessions)
    unverifiable = set(report.unverifiable_dates)
    assert observed | uncollected | unverifiable == set(report.expected_sessions)
    assert not (observed & uncollected) and not (observed & unverifiable)
    assert not (uncollected & unverifiable)


@pytest.mark.adversarial
def test_an_inverted_year_range_is_refused(calendar: NseTradingSessionCalendar) -> None:
    """Sibling of `sessions_between`, same silent-empty trap, previously unguarded.

    `unreliable_years_between(2025, 1993)` returned `()`, which reads as 'no
    unreliable years' — the opposite of the truth.
    """
    with pytest.raises(TradingCalendarError, match="before"):
        calendar.unreliable_years_between(2025, 1993)


# ------------------------------------------------------------------- the fence rule


@pytest.mark.unit
@pytest.mark.parametrize(
    ("count", "fence", "expected"),
    [
        (10, 6.875, True),
        (7, 6.875, True),
        (6, 6.875, False),
        (0, 6.875, False),      # zero is categorical, whatever the fence says
        (0, -5.0, False),       # ... including when the fence would admit it
        (5, 5.0, True),         # the boundary itself: >= not >
        (5, 5.000001, False),
    ],
)
def test_the_reliability_rule_is_exact_at_its_boundary(
    count: int, fence: float, *, expected: bool
) -> None:
    """Pins both conjuncts and the comparison operator.

    Inline in `year_reliability` neither could be falsified — no real year's count
    lands exactly on the float fence — so `>=` vs `>` and the `> 0` guard both
    survived mutation. Testing the rule directly fixes that.
    """
    assert NseTradingSessionCalendar.holiday_count_is_credible(count, fence) is expected


@pytest.mark.unit
@pytest.mark.parametrize("year", [2000, 2002, 2003, 2004, 2005, 2010])
def test_years_just_above_the_fence_are_reliable(
    calendar: NseTradingSessionCalendar, year: int
) -> None:
    """These recognise 10-11 holidays and sit between the real fence and the fence
    a zero Tukey multiple would produce — so they fail if the multiple is dropped.

    The original 'well-covered' fixtures were all 12-15, comfortably clear of that
    band, which is why the multiplier mutant survived.
    """
    assert calendar.year_reliability(year).is_reliable


# ------------------------------------------------------- previously untested surface


@pytest.mark.unit
def test_collection_completeness_reflects_the_missing_share(
    calendar: NseTradingSessionCalendar,
) -> None:
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    report = calendar.classify_observed_dates(
        frozenset(sessions[:-2]), date(2024, 1, 1), date(2024, 1, 31)
    )
    assert report.collection_completeness == pytest.approx(
        1.0 - 2 / JANUARY_2024_SESSIONS
    )


@pytest.mark.unit
def test_a_complete_collection_is_fully_complete(
    calendar: NseTradingSessionCalendar,
) -> None:
    sessions = calendar.sessions_between(date(2024, 1, 1), date(2024, 1, 31))
    report = calendar.classify_observed_dates(
        frozenset(sessions), date(2024, 1, 1), date(2024, 1, 31)
    )
    assert report.collection_completeness == 1.0
    assert not report.has_disagreement
    assert not report.has_unverifiable_dates


@pytest.mark.unit
def test_disagreement_is_reported_when_data_exists_on_a_closed_day(
    calendar: NseTradingSessionCalendar,
) -> None:
    report = calendar.classify_observed_dates(
        frozenset({REPUBLIC_DAY_2024}), date(2024, 1, 1), date(2024, 1, 31)
    )
    assert report.has_disagreement


@pytest.mark.unit
def test_observed_sessions_excludes_dates_the_calendar_calls_closed(
    calendar: NseTradingSessionCalendar,
) -> None:
    """`observed_sessions` must be the intersection, not the raw input."""
    report = calendar.classify_observed_dates(
        frozenset({REPUBLIC_DAY_2024, A_NORMAL_TUESDAY}), date(2024, 1, 1), date(2024, 1, 31)
    )
    assert report.observed_sessions == frozenset({A_NORMAL_TUESDAY})


@pytest.mark.unit
def test_non_session_observations_are_returned_in_order(
    calendar: NseTradingSessionCalendar,
) -> None:
    report = calendar.classify_observed_dates(
        frozenset({date(2024, 3, 25), REPUBLIC_DAY_2024, date(2024, 1, 27)}),
        date(2024, 1, 1), date(2024, 12, 31),
    )
    assert report.observed_non_sessions == tuple(sorted(report.observed_non_sessions))
    assert len(report.observed_non_sessions) > 1  # order is actually exercised


@pytest.mark.unit
def test_year_reliability_describes_itself_readably(
    calendar: NseTradingSessionCalendar,
) -> None:
    assert "UNRELIABLE" in calendar.year_reliability(1995).describe()
    assert "reliable" in calendar.year_reliability(2024).describe()


@pytest.mark.adversarial
def test_an_unknown_calendar_name_raises_the_documented_error() -> None:
    with pytest.raises(TradingCalendarError, match="no market calendar"):
        NseTradingSessionCalendar("NOT_A_REAL_EXCHANGE")


@pytest.mark.adversarial
@pytest.mark.parametrize("day", [date(1985, 1, 2), date(2035, 1, 2)])
def test_a_date_outside_the_measured_span_is_refused(
    calendar: NseTradingSessionCalendar, day: date
) -> None:
    """The library answers happily out here — with spurious results. We do not."""
    with pytest.raises(TradingCalendarError, match="outside the supported span"):
        calendar.is_trading_session(day)


@pytest.mark.adversarial
def test_a_non_date_is_refused(calendar: NseTradingSessionCalendar) -> None:
    with pytest.raises(TradingCalendarError, match="expected a date"):
        calendar.is_trading_session("2024-01-30")  # type: ignore[arg-type]


@pytest.mark.unit
def test_repeated_session_lookups_reuse_the_cached_year(
    calendar: NseTradingSessionCalendar,
) -> None:
    """Measured at ~5 ms per uncached call; the universe engine calls this per
    symbol per date, so an uncached lookup is a real cost, not a micro-optimisation."""
    calendar.is_trading_session(A_NORMAL_TUESDAY)
    first = calendar._sessions_in_year(2024)
    second = calendar._sessions_in_year(2024)
    assert first is second
