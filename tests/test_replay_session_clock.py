"""`L0.13` — the replay clock, and the two ways a replay lies to itself.

Reading the future is caught by the firewall. Reading the WALL is caught by a detector,
and the last test here is the gate: it fails the build if any replay-path module ever
learns to ask the operating system what time it is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.causal_leakage_firewall import (
    CausalOrderingError,
    LeakageReason,
    ObservableRow,
    SourcePublicationLag,
)
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar
from nse_algo_trader.replay_session_clock import (
    IST,
    HistoricalTradingDayWalker,
    NoTradingSessionFoundError,
    ReplayClockError,
    ReplaySessionClock,
    replay_sessions,
    session_for,
)
from nse_algo_trader.wall_clock_access_detector import (
    find_wall_clock_access,
    replay_path_modules,
    scan_replay_path,
)

MONDAY = date(2026, 8, 10)
NEXT_DAY_SOURCE = {
    "bhavcopy": SourcePublicationLag("bhavcopy", days=1, observations_used=100)
}


def _clock(day: date = MONDAY) -> ReplaySessionClock:
    return ReplaySessionClock(session_for(day), NEXT_DAY_SOURCE)


@pytest.mark.unit
def test_the_session_spans_real_nse_hours() -> None:
    session = session_for(MONDAY)
    assert session.opens_at == datetime(2026, 8, 10, 9, 15, tzinfo=IST)
    assert session.closes_at == datetime(2026, 8, 10, 15, 30, tzinfo=IST)
    assert session.duration == timedelta(hours=6, minutes=15)


@pytest.mark.unit
def test_the_clock_starts_at_the_open_not_at_wall_time() -> None:
    clock = _clock()
    assert clock.now == session_for(MONDAY).opens_at
    assert not clock.is_finished


@pytest.mark.unit
def test_the_clock_cannot_be_rewound() -> None:
    clock = _clock()
    clock.advance_by(timedelta(hours=1))
    with pytest.raises(CausalOrderingError):
        clock.advance_to(session_for(MONDAY).opens_at)


@pytest.mark.unit
def test_a_negative_step_is_refused_rather_than_silently_absorbed() -> None:
    clock = _clock()
    with pytest.raises(ReplayClockError):
        clock.advance_by(timedelta(minutes=-1))


@pytest.mark.unit
def test_stepping_yields_the_open_before_advancing() -> None:
    """A bar at 09:15 is a real bar; starting one step in drops every day's open."""
    clock = _clock()
    moments = list(clock.step_through_session(timedelta(hours=1)))
    assert moments[0] == session_for(MONDAY).opens_at
    assert moments[-1] == session_for(MONDAY).closes_at
    assert clock.is_finished


@pytest.mark.unit
def test_a_zero_step_is_refused_instead_of_looping_forever() -> None:
    clock = _clock()
    with pytest.raises(ReplayClockError):
        list(clock.step_through_session(timedelta(0)))


@pytest.mark.unit
def test_data_becomes_observable_only_as_the_clock_reaches_it() -> None:
    """The behaviour that makes replay honest: data arrives by time passing."""
    clock = _clock()
    rows = [
        ObservableRow("bhavcopy", date(2026, 8, 7), datetime(2026, 8, 11, tzinfo=UTC)),
        ObservableRow("bhavcopy", MONDAY, datetime(2026, 8, 11, tzinfo=UTC)),
    ]
    observable = clock.observable_now(rows)
    # Friday's bhavcopy was published Saturday, so it is knowable on Monday. Monday's own
    # is published Tuesday and must not be visible during Monday's session.
    assert [row.effective_date for row in observable] == [date(2026, 8, 7)]
    assert clock.firewall.ledger.blocked[LeakageReason.NOT_YET_PUBLISHED] == 1


@pytest.mark.unit
def test_the_walker_yields_only_real_sessions() -> None:
    walker = HistoricalTradingDayWalker(NseTradingSessionCalendar())
    days = list(walker.walk_forward(date(2026, 8, 7), date(2026, 8, 11)))
    # 8th and 9th are a weekend and must not appear.
    assert date(2026, 8, 8) not in days
    assert date(2026, 8, 9) not in days
    assert days == sorted(days)


@pytest.mark.unit
def test_walking_backward_is_the_reverse_of_walking_forward() -> None:
    walker = HistoricalTradingDayWalker(NseTradingSessionCalendar())
    forward = list(walker.walk_forward(date(2026, 7, 1), date(2026, 7, 31)))
    backward = list(walker.walk_backward(date(2026, 7, 31), date(2026, 7, 1)))
    assert backward == list(reversed(forward))


@pytest.mark.unit
def test_a_weekend_start_rewinds_to_the_previous_session() -> None:
    walker = HistoricalTradingDayWalker(NseTradingSessionCalendar())
    assert walker.most_recent_session_on_or_before(date(2026, 8, 9)) == date(2026, 8, 7)


@pytest.mark.unit
def test_an_impossible_calendar_raises_rather_than_scanning_forever() -> None:
    class NeverOpen:
        def is_trading_session(self, day: date) -> bool:
            return False

    walker = HistoricalTradingDayWalker(NeverOpen())  # type: ignore[arg-type]
    with pytest.raises(NoTradingSessionFoundError):
        walker.most_recent_session_on_or_before(MONDAY)


@pytest.mark.unit
def test_each_session_gets_a_fresh_clock_and_ledger() -> None:
    """Carrying one clock across days would let Monday's blocks explain away Tuesday's."""
    clocks = list(
        replay_sessions(
            NseTradingSessionCalendar(),
            NEXT_DAY_SOURCE,
            date(2026, 8, 7),
            date(2026, 8, 11),
        )
    )
    assert [clock.session.trading_day for clock in clocks] == [
        date(2026, 8, 7),
        date(2026, 8, 10),
        date(2026, 8, 11),
    ]
    assert all(clock.firewall.ledger.admitted == 0 for clock in clocks)


@pytest.mark.unit
def test_the_detector_finds_every_way_to_read_the_wall() -> None:
    source = (
        "import time\n"
        "from datetime import date, datetime\n"
        "a = datetime.now()\n"
        "b = datetime.utcnow()\n"
        "c = date.today()\n"
        "d = time.time()\n"
        "e = time.monotonic()\n"
    )
    assert len(find_wall_clock_access(source, Path("x.py"))) == 5


@pytest.mark.unit
def test_the_detector_does_not_cry_wolf() -> None:
    """A checker that flags legitimate code gets ignored, which is how it dies.

    `datetime.min.time()` reads a constant. `self.clock.now` is the INJECTED clock this
    whole rule exists to make people use — flagging it would punish the fix.
    """
    source = (
        "from datetime import date, datetime\n"
        "a = datetime.min.time()\n"
        "b = self.clock.now\n"
        "c = datetime.combine(date(2020, 1, 1), datetime.min.time())\n"
    )
    assert find_wall_clock_access(source, Path("x.py")) == []


@pytest.mark.unit
def test_no_replay_path_module_reads_the_wall_clock() -> None:
    """THE GATE. This is what makes the honest clock a property of the code.

    A single `datetime.now()` in the replay path returns today while simulated time sits
    in 2020, and the only symptom is a backtest that comes out slightly too good.
    """
    package_root = Path(__file__).resolve().parents[1] / "src" / "nse_algo_trader"
    accesses = scan_replay_path(replay_path_modules(package_root))
    assert accesses == [], "replay path reads the wall clock: " + "; ".join(
        access.describe() for access in accesses
    )
