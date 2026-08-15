"""A scheduled capture must not start on a holiday (`A.119`).

The timer fires Monday to Friday and has no idea which weekdays the exchange is shut. If the guard
were wrong in the permissive direction the capture would run on a holiday, record nothing, and
write a liveness record saying it stopped early — training a reader to ignore exactly the signal
`A.118` added. Wrong in the other direction it would skip a real session, which is unrecoverable.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# The same shim the sibling capture tests use. An importlib.util spec here would register a SECOND
# module object under the same name, and whichever test file ran first would win — which is exactly
# what happened: `test_depth_capture_rate_prior` passed alone and failed in the full suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from record_live_depth_session import is_capture_worth_starting


class CalendarStub:
    """Answers the one question the guard asks, so the test is about the GUARD."""

    def __init__(self, sessions: set[date]) -> None:
        self._sessions = sessions

    def is_trading_session(self, day: date) -> bool:
        return day in self._sessions


def test_a_session_day_starts_the_capture() -> None:
    trading_day = date(2026, 8, 13)
    assert is_capture_worth_starting(trading_day, CalendarStub({trading_day}))


def test_a_holiday_does_not(capsys) -> None:  # type: ignore[no-untyped-def]
    holiday = date(2026, 8, 15)
    assert not is_capture_worth_starting(holiday, CalendarStub(set()))
    assert "not an NSE session" in capsys.readouterr().out


def test_the_real_calendar_agrees_about_a_weekend() -> None:
    """Against the REAL calendar, not a stub: 2026-08-15 is a Saturday."""
    assert not is_capture_worth_starting(date(2026, 8, 15))
    assert is_capture_worth_starting(date(2026, 8, 13))
