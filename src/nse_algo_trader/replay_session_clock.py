"""`L0.13` — the replay clock, structurally unable to read the future or the wall.

A replay is only worth running if the code under test cannot tell it is a replay. Two
distinct ways that guarantee fails, and they need different defences:

**1. Reading the future.** The clock advances only forward and owns a `L0.11`
`CausalLeakageFirewall`, so a row is observable only when the clock has reached the moment
it became knowable. Data does not arrive by being fetched; it arrives by the clock moving.

**2. Reading the wall.** Far more insidious. A single `datetime.now()` anywhere in the
replay path silently returns 2026 while the simulated clock sits in 2020, and nothing
about the output looks wrong. No amount of care prevents this, because the failure is a
one-line import away at all times and the symptom is a slightly-too-good backtest.

So the honest-clock claim is not enforced by discipline. `wall_clock_access_detector`
scans the replay path's real AST for `datetime.now`, `date.today`, `time.time` and their
kin, and the execution gate fails on a hit. That is what makes "provably unable" a
statement about the code rather than an intention — the same mechanical approach this
project already uses for hardcoded rupee literals.

**The walker sequences real sessions only.** Replaying a Sunday teaches nothing and would
quietly pad every statistic with empty days, so the day sequence comes from the real NSE
calendar rather than from `timedelta(days=1)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nse_algo_trader.causal_leakage_firewall import (
    CausalLeakageFirewall,
    ObservableRow,
    SourcePublicationLag,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

IST = ZoneInfo("Asia/Kolkata")

NSE_SESSION_OPEN_IST = time(9, 15)
NSE_SESSION_CLOSE_IST = time(15, 30)
"""Continuous trading hours. Exchange facts, sourced rather than tuned."""

MAXIMUM_CONSECUTIVE_NON_TRADING_DAYS = 15
"""A bound for scans that must land on a real session. NSE's longest run of consecutive
non-trading days is a long weekend around a cluster of holidays — well inside this. It is
a loop guard, not a threshold: exceeding it means the holiday calendar is wrong, which is
raised rather than absorbed."""


class ReplayClockError(RuntimeError):
    """The replay clock was asked for something that would break the simulation."""


class NoTradingSessionFoundError(ReplayClockError):
    """No real session within the scan bound — the calendar is wrong, not the request."""


@dataclass(frozen=True)
class ReplaySession:
    """One real trading day, with its true open and close in IST."""

    trading_day: date
    opens_at: datetime
    closes_at: datetime

    @property
    def duration(self) -> timedelta:
        return self.closes_at - self.opens_at


def session_for(trading_day: date) -> ReplaySession:
    return ReplaySession(
        trading_day=trading_day,
        opens_at=datetime.combine(trading_day, NSE_SESSION_OPEN_IST, tzinfo=IST),
        closes_at=datetime.combine(trading_day, NSE_SESSION_CLOSE_IST, tzinfo=IST),
    )


class HistoricalTradingDayWalker:
    """Sequences REAL NSE sessions. Holds no data and does no I/O."""

    def __init__(self, calendar: NseTradingSessionCalendar) -> None:
        self._calendar = calendar

    def walk_backward(self, from_day: date, to_inception: date) -> Iterator[date]:
        """Real sessions from `from_day` back to `to_inception`, newest first."""
        current = from_day
        while current >= to_inception:
            if self._calendar.is_trading_session(current):
                yield current
            current -= timedelta(days=1)

    def walk_forward(self, from_inception: date, to_day: date) -> Iterator[date]:
        """Real sessions forward, oldest first — the bootstrap direction."""
        current = from_inception
        while current <= to_day:
            if self._calendar.is_trading_session(current):
                yield current
            current += timedelta(days=1)

    def most_recent_session_on_or_before(self, calendar_day: date) -> date:
        """The session to rewind to when a walk starts on a weekend or holiday."""
        current = calendar_day
        for _ in range(MAXIMUM_CONSECUTIVE_NON_TRADING_DAYS):
            if self._calendar.is_trading_session(current):
                return current
            current -= timedelta(days=1)
        raise NoTradingSessionFoundError(
            f"no NSE session within {MAXIMUM_CONSECUTIVE_NON_TRADING_DAYS} days on or "
            f"before {calendar_day} — the holiday calendar is wrong"
        )


class ReplaySessionClock:
    """The only legitimate source of 'now' inside a replay.

    Owns the leakage firewall rather than sitting beside it: a clock that could be
    advanced independently of the guard would let a caller step forward, read, and step
    back conceptually. Here the guard's virtual-now IS the clock.
    """

    def __init__(
        self,
        session: ReplaySession,
        publication_lags: dict[str, SourcePublicationLag],
        *,
        block_unknown_sources: bool = True,
    ) -> None:
        self._session = session
        self._firewall = CausalLeakageFirewall(
            session.opens_at,
            session.closes_at,
            publication_lags,
            block_unknown_sources=block_unknown_sources,
        )

    @property
    def session(self) -> ReplaySession:
        return self._session

    @property
    def now(self) -> datetime:
        """Simulated now. The replay path reads this and never the wall clock."""
        return self._firewall.virtual_now

    @property
    def firewall(self) -> CausalLeakageFirewall:
        return self._firewall

    @property
    def is_finished(self) -> bool:
        return self._firewall.virtual_now >= self._session.closes_at

    def advance_to(self, moment: datetime) -> None:
        """Move simulated time forward. Rewinding raises via the firewall."""
        self._firewall.advance_to(moment)

    def advance_by(self, step: timedelta) -> None:
        if step < timedelta(0):
            raise ReplayClockError(f"cannot advance by a negative step: {step}")
        self._firewall.advance_to(self._firewall.virtual_now + step)

    def observable_now(self, rows: Iterable[ObservableRow]) -> list[ObservableRow]:
        """Rows knowable at simulated now, with every refusal counted."""
        return list(self._firewall.filter_observable(rows))

    def step_through_session(self, step: timedelta) -> Iterator[datetime]:
        """Yield each simulated moment from open to close.

        Yields the open BEFORE advancing, so a consumer sees the session's first moment
        rather than starting one step in — a bar at 09:15 is a real bar and skipping it
        would silently drop the open from every replayed day.
        """
        if step <= timedelta(0):
            raise ReplayClockError(f"step must be positive, got {step}")
        yield self._firewall.virtual_now
        while not self.is_finished:
            self.advance_by(step)
            yield self._firewall.virtual_now


def replay_sessions(
    calendar: NseTradingSessionCalendar,
    publication_lags: dict[str, SourcePublicationLag],
    from_day: date,
    to_day: date,
    *,
    block_unknown_sources: bool = True,
) -> Iterator[ReplaySessionClock]:
    """A fresh clock per real session, oldest first.

    Fresh per session deliberately: carrying one clock across days would let a leakage
    ledger from Monday explain away Tuesday's blocks, and would make a rewind to the next
    morning's open look like an ordering violation.
    """
    walker = HistoricalTradingDayWalker(calendar)
    for trading_day in walker.walk_forward(from_day, to_day):
        yield ReplaySessionClock(
            session_for(trading_day),
            publication_lags,
            block_unknown_sources=block_unknown_sources,
        )
