"""`L0.20`/`L0.21` — where the admission controller gets its packet rates from.

`A.77`: the first live full-session capture admitted 300 of 9,890 instruments and filled
0% of its disk budget, because rates were read from TODAY's tape while the byte cost was
read from ALL sessions. At 10:30 today's tape did not exist, so the solve had a cost for
every row and a rate for nothing.

These tests pin the resolution order, because both plausible-looking alternatives are
wrong in a way that only shows up mid-session:

- prior-only: throws away the live evidence that is strictly better;
- today-only-if-non-empty: after a restart, "today" is exactly the narrow cohort the last
  run captured, so the fix re-admits the cohort it existed to escape.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from record_live_depth_session import IST, measured_rates_from_tape

# The capture reads the session date from an IST clock; the timezone is what makes
# "which partition is today" the same question on a UTC host as on an IST one.
SESSION_DATE = datetime(2026, 8, 12, 10, 30, tzinfo=IST)
TODAY = SESSION_DATE.date()
YESTERDAY = date(2026, 8, 11)
LAST_WEEK = date(2026, 8, 5)


class FakeTapeReader:
    """Stands in for `MarketDepthTapeReader` at the seam the function actually uses."""

    def __init__(self, rates_by_session: dict[date, dict[int, float]]) -> None:
        self._rates_by_session = rates_by_session
        self.sessions_read: list[date] = []

    def session_dates(self) -> list[date]:
        return list(self._rates_by_session)

    def instrument_packet_rates(self, session_date: date) -> dict[int, float]:
        self.sessions_read.append(session_date)
        return dict(self._rates_by_session.get(session_date, {}))


# The fixture hands each test a factory that installs a `FakeTapeReader` behind the
# monkeypatch and returns it, so the test can both seed rates and later inspect what the
# function under test actually read (`sessions_read`, the reassigned method, ...).
InstallFakeTapeReader = Callable[[dict[date, dict[int, float]]], FakeTapeReader]


@pytest.fixture
def patch_reader(monkeypatch: pytest.MonkeyPatch) -> InstallFakeTapeReader:
    def install(rates_by_session: dict[date, dict[int, float]]) -> FakeTapeReader:
        reader = FakeTapeReader(rates_by_session)
        monkeypatch.setattr("record_live_depth_session.MarketDepthTapeReader", lambda _root: reader)
        return reader

    return install


@pytest.mark.hermetic
def test_an_empty_tape_yields_no_rates(patch_reader: InstallFakeTapeReader) -> None:
    patch_reader({})
    assert measured_rates_from_tape(Path("/nonexistent"), SESSION_DATE) == {}


@pytest.mark.hermetic
def test_a_prior_session_supplies_the_rates_before_today_has_any(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """The 09:15 case. Without this the controller sizes 9,890 instruments with nothing."""
    patch_reader({YESTERDAY: {101: 2.0, 102: 0.5}})
    assert measured_rates_from_tape(Path("/t"), SESSION_DATE) == {101: 2.0, 102: 0.5}


@pytest.mark.hermetic
def test_todays_measurement_wins_over_the_prior_for_the_same_instrument(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """Live evidence beats a prior; the prior only fills what live evidence lacks."""
    patch_reader({YESTERDAY: {101: 2.0, 102: 0.5}, TODAY: {101: 9.0}})
    assert measured_rates_from_tape(Path("/t"), SESSION_DATE) == {101: 9.0, 102: 0.5}


@pytest.mark.adversarial
def test_a_restart_keeps_the_universe_it_has_not_captured_yet(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """The trap that made the first version of this fix insufficient (`A.77`).

    After a restart, today's tape holds only the cohort the dead run captured. Returning
    it because it is non-empty re-admits that cohort and nothing else, forever.
    """
    prior = dict.fromkeys(range(1000, 1900), 1.0)
    captured_cohort = dict.fromkeys(range(1000, 1010), 3.0)
    patch_reader({YESTERDAY: prior, TODAY: captured_cohort})

    rates = measured_rates_from_tape(Path("/t"), SESSION_DATE)

    assert len(rates) == 900, "the prior must survive a restart, not be replaced by it"
    assert rates[1000] == 3.0, "and today's measurement still wins where it exists"


@pytest.mark.adversarial
def test_a_nearer_prior_session_overrides_a_further_one(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """Rates drift; a month-old session must not outvote yesterday."""
    patch_reader({LAST_WEEK: {101: 0.1, 999: 7.0}, YESTERDAY: {101: 5.0}})
    rates = measured_rates_from_tape(Path("/t"), SESSION_DATE)
    assert rates[101] == 5.0, "yesterday wins for an instrument both sessions saw"
    assert rates[999] == 7.0, "and the older session still fills what yesterday missed"


@pytest.mark.adversarial
def test_a_future_dated_partition_is_never_read_as_a_prior(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """A replay or a clock-skewed host can leave a later-dated partition on the tape.

    Reading it would size today's capture from data today cannot have — the same leakage
    the causal firewall exists to prevent, arriving through the sizing path instead.
    """
    reader = patch_reader({date(2026, 8, 20): {101: 4.0}, YESTERDAY: {102: 1.0}})
    rates = measured_rates_from_tape(Path("/t"), SESSION_DATE)
    assert rates == {102: 1.0}
    assert date(2026, 8, 20) not in reader.sessions_read


@pytest.mark.adversarial
def test_one_unreadable_session_does_not_lose_the_others(
    patch_reader: InstallFakeTapeReader,
) -> None:
    """A half-written partition from a killed run must not zero the whole prior."""
    reader = patch_reader({LAST_WEEK: {101: 1.0}, YESTERDAY: {102: 2.0}})
    original_read = reader.instrument_packet_rates

    def fail_on_yesterday(session_date: date) -> dict[int, float]:
        if session_date == YESTERDAY:
            raise OSError("parquet footer truncated")
        return original_read(session_date)

    reader.instrument_packet_rates = fail_on_yesterday  # type: ignore[method-assign]
    assert measured_rates_from_tape(Path("/t"), SESSION_DATE) == {101: 1.0}
