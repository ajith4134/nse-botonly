"""Which dates NSE was actually open, and how far that answer can be trusted.

**This is a calendar, not an engine** (R.23b) — no solver, no carried state. It is
decision-path all the same: the point-in-time universe engine (`docs/research/204`)
cannot distinguish *"the symbol did not trade"* from *"we never collected that day"*
from *"the exchange was shut"* without it, and getting that wrong is how a data
outage disguises itself as a delisting.

**Why this library.** Measured, not assumed (`204` §7.2): `exchange_calendars` has
**no NSE at all** — its only Indian calendar is `XBOM` (BSE) — and zipline depends
on it internally, so that path inherits the gap. `pandas_market_calendars` exposes
`NSE`/`XNSE` and returns 22 correct January-2024 sessions, excluding Republic Day.

**The limitation this module exists to contain.** The library answers for any date
from 1990 to 2030, but its holiday rules only cover part of that. Measured across
every year:

    1990-1996   0 holidays recognised   <- rules not applied yet
    1997       17 holidays recognised
    1998        4 holidays recognised   <- implausible; India runs ~13-17
    1999-2026  10-19 holidays recognised
    2027-2030   0 holidays recognised   <- rules not published yet

Both ends fail, and **the far end is the dangerous one**: nothing warns you that
2028-01-26 is being reported as a trading session. A caller trusting either would
treat every holiday as a missing file and 'discover' an outage that never happened.

So the calendar **reports its own reliability per year**, on two derived conditions
(R.03), never a written-down year:

* **zero recognised holidays is categorical** — no real exchange year has none, so
  zero means no rules were applied, not an unusually quiet year. This catches both
  1990-1996 and 2027-2030.
* **Tukey's lower fence** over the years that *do* have coverage catches the
  implausibly-low-but-nonzero case, which is how 1998 surfaces. The zero years are
  excluded from that population deliberately: leaving them in drags the quartiles
  down until the fence goes negative and declares everything reliable — measured,
  and the reason the first version of this check silently passed everything.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cached_property
from typing import Final

import pandas_market_calendars

_TUKEY_FENCE_MULTIPLE: Final = 1.5  # standard outlier fence, not a tuned value
_QUARTILE_DIVISIONS: Final = 4
_WEEKEND_START: Final = 5  # Python weekday() index for Saturday
# The span this module has MEASURED reliability across. The library itself answers
# far outside it (1800, 2035 — it is bounded only by pandas' Timestamp range) and
# does so with spurious, unverified results, which is precisely why the bound is
# ours rather than borrowed.
_SUPPORTED_FIRST_YEAR: Final = 1990
_SUPPORTED_LAST_YEAR: Final = 2030


class TradingCalendarError(Exception):
    """The calendar was asked something it cannot answer honestly."""


@dataclass(frozen=True, slots=True)
class CalendarYearReliability:
    """Whether the library's holiday rules actually cover a given year."""

    year: int
    recognised_holiday_count: int
    lower_fence: float
    is_reliable: bool

    def describe(self) -> str:
        verdict = "reliable" if self.is_reliable else "UNRELIABLE"
        return (
            f"{self.year}: {self.recognised_holiday_count} holidays recognised "
            f"(fence {self.lower_fence:.2f}) — {verdict}"
        )


@dataclass(frozen=True, slots=True)
class ObservedDateReport:
    """What a collection of observed dates means against the real session list.

    Every expected session lands in exactly one of ``observed_sessions``,
    ``uncollected_sessions`` or ``unverifiable_dates`` — the partition is the
    point, because a date that falls through would be a silently unexamined day.

    ``uncollected_sessions`` is the backfill worklist and carries only dates the
    calendar can actually vouch for. ``unverifiable_dates`` holds the rest: days
    in years whose holiday rules are missing, where "no file" and "no session"
    cannot be told apart at all.
    """

    window_start: date
    window_end: date
    expected_sessions: tuple[date, ...]
    observed_sessions: frozenset[date]
    uncollected_sessions: tuple[date, ...]
    unverifiable_dates: tuple[date, ...]
    observed_non_sessions: tuple[date, ...]
    unreliable_years: tuple[int, ...]

    @property
    def collection_completeness(self) -> float | None:
        """Share of *verifiable* expected sessions actually collected.

        ``None`` when the window contains nothing verifiable — neither 1.0 nor
        0.0 is true there, and returning either would be the confident guess this
        whole component exists to refuse.
        """
        verifiable = len(self.expected_sessions) - len(self.unverifiable_dates)
        if verifiable <= 0:
            return None
        return 1.0 - len(self.uncollected_sessions) / verifiable

    @property
    def has_unverifiable_dates(self) -> bool:
        """True when part of the window falls in a year whose holidays are unknown.

        A caller treating ``uncollected_sessions`` as a backfill worklist must check
        this first, or it will chase files the exchange never published.
        """
        return bool(self.unverifiable_dates)

    @property
    def has_disagreement(self) -> bool:
        """True when data exists for a day the calendar calls closed.

        One of the two is wrong, and which one matters — so this is surfaced
        rather than resolved here.
        """
        return bool(self.observed_non_sessions)


class NseTradingSessionCalendar:
    """NSE trading sessions, with an honest account of where it stops being sure."""

    def __init__(self, calendar_name: str = "NSE") -> None:
        try:
            self._calendar = pandas_market_calendars.get_calendar(calendar_name)
        except Exception as error:
            raise TradingCalendarError(
                f"no market calendar named {calendar_name!r}; "
                "note that exchange_calendars has no NSE at all, only XBOM"
            ) from error
        self._calendar_name = calendar_name
        self._sessions_by_year: dict[int, frozenset[date]] = {}

    # ------------------------------------------------------------------ sessions

    def _sessions_in_year(self, year: int) -> frozenset[date]:
        """Cached per year: the uncached version measured ~5 ms per call, and the
        universe engine asks this once per symbol per date."""
        cached = self._sessions_by_year.get(year)
        if cached is None:
            stamps = self._calendar.valid_days(f"{year}-01-01", f"{year}-12-31")
            cached = frozenset(stamp.date() for stamp in stamps)
            self._sessions_by_year[year] = cached
        return cached

    def is_trading_session(self, day: date) -> bool:
        """Whether NSE was open on ``day``.

        Deliberately permissive about reliability: it answers for any supported
        year. Callers that must not be misled ask for ``require_reliable`` on the
        range methods, or consult :meth:`year_reliability` first.
        """
        self._require_supported(day)
        return day in self._sessions_in_year(day.year)

    def sessions_between(
        self, start: date, end: date, *, require_reliable: bool = False
    ) -> tuple[date, ...]:
        """Every trading session in ``[start, end]``, inclusive and ordered.

        Raises:
            TradingCalendarError: ``end`` is before ``start``, a date is outside the
                supported span, or ``require_reliable`` is set and the range touches
                a year whose holiday rules are not applied.
        """
        if end < start:
            raise TradingCalendarError(
                f"end {end.isoformat()} is after start {start.isoformat()}; an inverted "
                "range would return nothing, which reads as 'no sessions' rather than as "
                "the mistake it is"
            )
        self._require_supported(start)
        self._require_supported(end)

        if require_reliable:
            unreliable = self.unreliable_years_between(start.year, end.year)
            if unreliable:
                raise TradingCalendarError(
                    f"{self._calendar_name} holiday rules are unreliable for "
                    f"{', '.join(str(year) for year in unreliable)}: every weekday is "
                    "reported as a session, so absences cannot be told from holidays"
                )

        stamps = self._calendar.valid_days(start.isoformat(), end.isoformat())
        return tuple(stamp.date() for stamp in stamps)

    # -------------------------------------------------------------- reliability

    @cached_property
    def _recognised_holidays_by_year(self) -> dict[int, int]:
        """Holidays the library actually applies, per year, measured from its output.

        A holiday here is a weekday that is not a session — the only definition
        available without a second source, and the one that exposes the gap.
        """
        counts: dict[int, int] = {}
        for year in range(_SUPPORTED_FIRST_YEAR, _SUPPORTED_LAST_YEAR + 1):
            # No weekday filter on sessions: weekend days cannot survive the set
            # difference below because they are never in `weekdays`. A filter here
            # was dead code, and dead defensive code reads as a guard that works.
            sessions = self._sessions_in_year(year)
            first, last = date(year, 1, 1), date(year, 12, 31)
            weekdays = {
                first + timedelta(days=offset)
                for offset in range((last - first).days + 1)
                if (first + timedelta(days=offset)).weekday() < _WEEKEND_START
            }
            counts[year] = len(weekdays - sessions)
        return counts

    @cached_property
    def _lower_fence(self) -> float:
        """Tukey's lower fence over the years that have *any* holiday coverage.

        Years recognising **zero** holidays are excluded from the population on
        purpose. Zero is not an unusually low sample from the coverage
        distribution — no real exchange year has no holidays — it is the absence
        of any rules at all, and leaving those years in drags the quartiles down
        until the fence goes negative and declares everything reliable. Measured:
        including them produced a fence below zero; excluding them gives 6.88,
        which then also catches 1998's implausible 4.
        """
        covered = sorted(
            count for count in self._recognised_holidays_by_year.values() if count > 0
        )
        quartiles = statistics.quantiles(covered, n=_QUARTILE_DIVISIONS)
        first_quartile, third_quartile = quartiles[0], quartiles[2]
        return first_quartile - _TUKEY_FENCE_MULTIPLE * (third_quartile - first_quartile)

    def reliability_lower_fence(self) -> float:
        """The derived cut-off below which a year's holiday coverage is not credible."""
        return self._lower_fence

    def year_reliability(self, year: int) -> CalendarYearReliability:
        self._require_supported(date(year, 1, 1))
        recognised = self._recognised_holidays_by_year[year]
        # Two conditions, both derived. Zero is categorical — an exchange year with
        # no holidays means no rules were applied, which is how 1990-1996 *and*
        # 2027-2030 (rules not yet published) both surface. The fence then catches
        # implausibly-low-but-nonzero coverage, which is how 1998's 4 surfaces.
        return CalendarYearReliability(
            year=year,
            recognised_holiday_count=recognised,
            lower_fence=self._lower_fence,
            is_reliable=self.holiday_count_is_credible(recognised, self._lower_fence),
        )

    @staticmethod
    def holiday_count_is_credible(recognised_holidays: int, lower_fence: float) -> bool:
        """The reliability rule itself, separated so its boundary can be tested.

        Inline, neither condition could be falsified: no real year's count lands
        exactly on the float-valued fence, so ``>=`` versus ``>`` was unverifiable
        and both mutants survived.
        """
        return recognised_holidays > 0 and recognised_holidays >= lower_fence

    def unreliable_years_between(self, first_year: int, last_year: int) -> tuple[int, ...]:
        """Raises:
            TradingCalendarError: ``last_year`` precedes ``first_year``. Returning
                an empty tuple would read as "no unreliable years", which is the
                same silent-empty trap ``sessions_between`` already guards against.
        """
        if last_year < first_year:
            raise TradingCalendarError(
                f"last_year {last_year} is before first_year {first_year}; an empty "
                "result would read as 'no unreliable years'"
            )
        return tuple(
            year
            for year in range(first_year, last_year + 1)
            if not self.year_reliability(year).is_reliable
        )

    # ------------------------------------------------------------ classification

    def classify_observed_dates(
        self, observed: frozenset[date], start: date, end: date
    ) -> ObservedDateReport:
        """Resolve a set of collected dates against the real session list.

        Dates outside ``[start, end]`` are ignored rather than counted against the
        window — a caller passing a wider collection is asking about this window,
        not being told its other data is wrong.
        """
        expected = self.sessions_between(start, end)
        in_window = frozenset(day for day in observed if start <= day <= end)
        expected_set = set(expected)
        unreliable = self.unreliable_years_between(start.year, end.year)
        unreliable_set = set(unreliable)

        # The correction that matters. In a year with no holiday rules the library
        # reports EVERY weekday as a session, so a missing date there is not
        # evidence of a gap — it may be a holiday the library cannot see. Measured:
        # over an empty 1993 collection the first version returned 261 'uncollected
        # sessions' at 0.0 completeness, including Republic Day, reading as a total
        # outage for a year that was mostly just unverified. Those dates are
        # partitioned out rather than reported as gaps.
        missing = expected_set - in_window
        unverifiable = {day for day in missing if day.year in unreliable_set}
        return ObservedDateReport(
            window_start=start,
            window_end=end,
            expected_sessions=expected,
            observed_sessions=in_window & expected_set,
            uncollected_sessions=tuple(sorted(missing - unverifiable)),
            unverifiable_dates=tuple(sorted(unverifiable)),
            observed_non_sessions=tuple(sorted(in_window - expected_set)),
            unreliable_years=unreliable,
        )

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _require_supported(day: date) -> None:
        # datetime and pandas.Timestamp are SUBCLASSES of date, so a plain
        # isinstance check admits them — and then `day in set_of_dates` is False
        # for the very same calendar day, silently. That is the confident-wrong
        # answer this module exists to avoid, so the moment-carrying types are
        # refused by name rather than coerced: dropping a time and a timezone
        # without being asked is its own class of bug.
        if isinstance(day, datetime):
            raise TradingCalendarError(
                f"expected a date, not {type(day).__name__}: a moment is not a trading day. "
                "Call .date() explicitly, so the timezone it is resolved in is your choice"
            )
        if not isinstance(day, date):
            raise TradingCalendarError(f"expected a date, not {type(day).__name__}")
        if not _SUPPORTED_FIRST_YEAR <= day.year <= _SUPPORTED_LAST_YEAR:
            raise TradingCalendarError(
                f"{day.isoformat()} is outside the supported span "
                f"{_SUPPORTED_FIRST_YEAR}-{_SUPPORTED_LAST_YEAR}"
            )
