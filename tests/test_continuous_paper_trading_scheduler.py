"""Tests for `L10.01`'s continuous loop — spec `docs/research/265`, todo `4.9`, blocker `B33`.

The test that matters most is `test_carried_state_actually_accumulates`. The first live run of this
scheduler observed **1,845 instruments off the real tape and still reported `instruments_tracked`
of 0** — because the universe was passed empty and the prices alone give a bot nothing to iterate.
Every number on the iteration record looked right. Carrying state is the ONE thing this class exists
to do, and it was silently not doing it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.paper_loop.continuous_paper_trading_scheduler import (
    ContinuousPaperTradingScheduler,
    SchedulerError,
    SchedulerIteration,
    SchedulerLivenessStore,
    SessionPhase,
)
from nse_algo_trader.replay_session_clock import IST
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    CashIntradayMeanReversionBot,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import TradeableInstrument

SESSION = date(2026, 8, 18)
"""A real NSE trading session — a Tuesday, and the day this loop was first run live."""

HOLIDAY = date(2026, 8, 15)
"""Independence Day. A real NSE holiday, and a Saturday-independent check that the phase comes from
the exchange calendar rather than from a weekday test."""


def an_instant(hour: int, minute: int, day: date = SESSION) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(
        hour=hour, minute=minute
    )


def a_universe(count: int = 8) -> tuple[TradeableInstrument, ...]:
    return tuple(
        TradeableInstrument(700_000 + index, f"CASH{index:03d}", 1, 5) for index in range(count)
    )


class RecordedTape:
    """A `MarketObservationSource` with no parquet — the seam the protocol exists for."""

    def __init__(
        self,
        prices: dict[int, Decimal],
        latest: datetime | None,
        *,
        fail: bool = False,
    ) -> None:
        self._prices = prices
        self._latest = latest
        self._fail = fail
        self._ticks: tuple[tuple[int, datetime | None, Decimal, int, int], ...] = ()
        self.calls = 0

    def with_ticks(
        self, ticks: tuple[tuple[int, datetime | None, Decimal, int, int], ...]
    ) -> RecordedTape:
        self._ticks = ticks
        return self

    def latest_prices(self, session_date: date) -> dict[int, Decimal]:
        del session_date
        self.calls += 1
        if self._fail:
            raise RuntimeError("the tape could not be read")
        return dict(self._prices)

    def latest_exchange_time(self, session_date: date) -> datetime | None:
        del session_date
        return self._latest

    def ticks_since(
        self, session_date: date, after_sequence: int, limit: int
    ) -> tuple[tuple[int, datetime | None, Decimal, int, int], ...]:
        """Serve the scripted ticks once, so the loop folds them into real bars."""
        del session_date, limit
        return tuple(row for row in self._ticks if row[4] > after_sequence)


def a_scheduler(
    tmp_path: Path,
    *,
    tape: RecordedTape | None = None,
    universe: tuple[TradeableInstrument, ...] | None = None,
) -> ContinuousPaperTradingScheduler:
    resolved = a_universe() if universe is None else universe
    prices = {
        instrument.instrument_token: Decimal(str(100_000 + index * 500))
        for index, instrument in enumerate(resolved)
    }
    return ContinuousPaperTradingScheduler(
        bots=[CashIntradayMeanReversionBot()],
        observations=tape or RecordedTape(prices, an_instant(13, 5)),
        liveness=SchedulerLivenessStore(tmp_path / "liveness.sqlite3"),
        universe=resolved,
    )


# --------------------------------------------------------------------------------------------
# phase, from the exchange calendar
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (8, 0, SessionPhase.BEFORE_OPEN),
        (9, 14, SessionPhase.BEFORE_OPEN),
        (9, 16, SessionPhase.TRADING),
        (13, 10, SessionPhase.TRADING),
        (15, 14, SessionPhase.TRADING),
        (15, 20, SessionPhase.SQUARING_OFF),
        (15, 29, SessionPhase.SQUARING_OFF),
        (15, 31, SessionPhase.AFTER_CLOSE),
        (23, 0, SessionPhase.AFTER_CLOSE),
    ],
)
def test_the_phase_is_right_at_every_real_nse_boundary(
    tmp_path: Path, hour: int, minute: int, expected: SessionPhase
) -> None:
    assert a_scheduler(tmp_path).phase_at(an_instant(hour, minute)) is expected


@pytest.mark.unit
def test_a_holiday_is_after_close_all_day_rather_than_waiting_for_an_open() -> None:
    """`BEFORE_OPEN` on a holiday would have the loop waiting all day for an open that never comes.

    Independence Day, from `NseTradingSessionCalendar` — never a weekday check.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        scheduler = a_scheduler(Path(directory))
        for hour in (8, 10, 13, 16):
            assert scheduler.phase_at(an_instant(hour, 0, HOLIDAY)) is SessionPhase.AFTER_CLOSE


@pytest.mark.unit
def test_a_naive_instant_is_refused_rather_than_read_as_utc() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        scheduler = a_scheduler(Path(directory))
        with pytest.raises(SchedulerError, match="naive"):
            scheduler.phase_at(datetime(2026, 8, 18, 13, 0))  # noqa: DTZ001 — that is the point


@pytest.mark.unit
def test_only_trading_may_open_a_position() -> None:
    """`R.01`: opening inside the square-off window is how a position survives the close."""
    assert SessionPhase.TRADING.may_open_a_position
    assert not SessionPhase.SQUARING_OFF.may_open_a_position
    assert not SessionPhase.BEFORE_OPEN.may_open_a_position
    assert not SessionPhase.AFTER_CLOSE.may_open_a_position
    # ...but the square-off window must still OBSERVE, or it flattens against a stale book.
    assert SessionPhase.SQUARING_OFF.observes_the_market
    assert SessionPhase.TRADING.observes_the_market


# --------------------------------------------------------------------------------------------
# the one thing this class exists to do
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_carried_state_actually_accumulates(tmp_path: Path) -> None:
    """Written from the defect: the first LIVE run observed 1,845 instruments and tracked ZERO.

    The universe is what a bot iterates; the tape supplies the ticks. Passing prices with an empty
    universe produced an iteration record whose every field looked right — instruments observed,
    tape lag, phase — while the bots learned nothing at all.

    **Updated when `B40` landed**, and deliberately not weakened. The bots now learn from CLOSED
    BARS rather than from a per-tick price snapshot, because the first version of that fed the
    panel 30,059 observations and the bots 6 — one clock for everything or the two halves of a
    decision disagree about what an observation is. The assertion is the same; the path is the real
    one.
    """
    universe = a_universe()
    ticks: list[tuple[int, datetime | None, Decimal, int, int]] = []
    sequence = 0
    for step in range(8):
        for index, instrument in enumerate(universe):
            sequence += 1
            ticks.append(
                (
                    instrument.instrument_token,
                    an_instant(13, 10 + step * 5),
                    Decimal(str(100_000 + index * 500 + step * 130)),
                    0,
                    sequence,
                )
            )
    tape = RecordedTape({}, an_instant(13, 40)).with_ticks(tuple(ticks))
    scheduler = a_scheduler(tmp_path, tape=tape, universe=universe)
    bot = scheduler.bots[0]
    assert bot.instruments_tracked == 0

    for minute in range(41, 46):
        scheduler.step(an_instant(13, minute))

    assert scheduler.bars_built > 0, "the loop must fold ticks into bars at all"
    assert bot.instruments_tracked == len(universe), "the bots must SEE the universe"
    assert bot.instruments_mature > 0, "a rolling statistic needs observations to accumulate"
    assert scheduler.iterations == 5


@pytest.mark.unit
def test_the_same_bot_instances_survive_every_tick(tmp_path: Path) -> None:
    """State that dies with the request made `/bots` report 0.0% readiness on every render."""
    scheduler = a_scheduler(tmp_path)
    first = scheduler.bots
    scheduler.step(an_instant(13, 10))
    scheduler.step(an_instant(13, 15))
    assert scheduler.bots is first
    assert all(a is b for a, b in zip(first, scheduler.bots, strict=True))


@pytest.mark.unit
def test_a_closed_market_observes_nothing_but_still_records_a_tick(tmp_path: Path) -> None:
    """A quiet iteration is still written — otherwise a stalled loop looks like a quiet market."""
    tape = RecordedTape({}, None)
    scheduler = a_scheduler(tmp_path, tape=tape)
    iteration = scheduler.step(an_instant(20, 0))
    assert iteration.phase is SessionPhase.AFTER_CLOSE
    assert iteration.instruments_observed == 0
    assert iteration.is_healthy
    assert tape.calls == 0, "a closed market must not even read the tape"
    assert "alive" in iteration.note


@pytest.mark.unit
def test_the_loop_records_a_failure_instead_of_dying(tmp_path: Path) -> None:
    """A scheduler that dies on one bad tick is worse than none.

    `A.143` is what a silent stop costs.
    """
    scheduler = a_scheduler(tmp_path, tape=RecordedTape({}, None, fail=True))
    iteration = scheduler.step(an_instant(13, 10))
    assert not iteration.is_healthy
    assert iteration.failure is not None
    assert "tape could not be read" in iteration.failure
    # ...and the very next tick still runs.
    assert scheduler.step(an_instant(13, 11)) is not None
    assert scheduler.iterations == 2


@pytest.mark.unit
def test_tape_lag_is_measured_so_a_stopped_capture_is_visible(tmp_path: Path) -> None:
    """A capture that has died shows as a GROWING lag, not as a quiet loop."""
    universe = a_universe()
    prices = {instrument.instrument_token: Decimal("100000") for instrument in universe}
    scheduler = a_scheduler(
        tmp_path, tape=RecordedTape(prices, an_instant(13, 0)), universe=universe
    )
    fresh = scheduler.step(an_instant(13, 1))
    stale = scheduler.step(an_instant(14, 0))
    assert fresh.tape_lag_seconds is not None
    assert stale.tape_lag_seconds is not None
    assert fresh.tape_lag_seconds == pytest.approx(60.0)
    assert stale.tape_lag_seconds == pytest.approx(3600.0)
    assert stale.tape_lag_seconds > fresh.tape_lag_seconds


@pytest.mark.unit
def test_a_scheduler_with_no_bots_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(SchedulerError, match="no bots"):
        ContinuousPaperTradingScheduler(
            bots=[],
            observations=RecordedTape({}, None),
            liveness=SchedulerLivenessStore(tmp_path / "liveness.sqlite3"),
        )


# --------------------------------------------------------------------------------------------
# the liveness record the dashboard reads
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_iteration_is_recorded_and_readable_back(tmp_path: Path) -> None:
    store = SchedulerLivenessStore(tmp_path / "liveness.sqlite3")
    scheduler = ContinuousPaperTradingScheduler(
        bots=[CashIntradayMeanReversionBot()],
        observations=RecordedTape({}, None),
        liveness=store,
        universe=a_universe(),
    )
    for minute in range(10, 14):
        scheduler.step(an_instant(13, minute))
    recent = store.recent()
    assert len(recent) == 4
    assert all(isinstance(row, SchedulerIteration) for row in recent)
    assert store.last_healthy_at() is not None


@pytest.mark.unit
def test_replaying_the_same_instant_does_not_double_a_row(tmp_path: Path) -> None:
    """A replay must not inflate the liveness record into looking busier than the loop was."""
    store = SchedulerLivenessStore(tmp_path / "liveness.sqlite3")
    scheduler = ContinuousPaperTradingScheduler(
        bots=[CashIntradayMeanReversionBot()],
        observations=RecordedTape({}, None),
        liveness=store,
        universe=a_universe(),
    )
    scheduler.step(an_instant(13, 10))
    scheduler.step(an_instant(13, 10))
    assert len(store.recent()) == 1


@pytest.mark.unit
def test_run_until_stops_and_never_sleeps_in_a_test(tmp_path: Path) -> None:
    """The clock and the sleep are injected, so a whole session drives through in milliseconds."""
    scheduler = a_scheduler(tmp_path)
    instants = iter(
        [an_instant(13, 10), an_instant(13, 15), an_instant(13, 20), an_instant(16, 0)]
    )
    slept: list[float] = []
    history = scheduler.run_until(
        an_instant(15, 30),
        cadence_seconds=300,
        now=lambda: next(instants),
        sleep=slept.append,
    )
    assert len(history) == 3
    assert slept == [300, 300, 300]


@pytest.mark.unit
def test_a_cadence_that_would_spin_is_refused(tmp_path: Path) -> None:
    scheduler = a_scheduler(tmp_path)
    with pytest.raises(SchedulerError, match="spin"):
        scheduler.run_until(an_instant(15, 30), cadence_seconds=0, now=lambda: an_instant(13, 0))


@pytest.mark.unit
def test_the_square_off_window_is_reached_even_when_the_tape_goes_silent(tmp_path: Path) -> None:
    """`R.01`: square-off must run when nothing else does. It is a PHASE, not a branch."""
    scheduler = a_scheduler(tmp_path, tape=RecordedTape({}, None))
    phases = [scheduler.step(an_instant(15, minute)).phase for minute in (10, 20, 29, 31)]
    assert phases == [
        SessionPhase.TRADING,
        SessionPhase.SQUARING_OFF,
        SessionPhase.SQUARING_OFF,
        SessionPhase.AFTER_CLOSE,
    ]


@pytest.mark.unit
def test_this_projects_calendar_agrees_with_an_independent_one() -> None:
    """The cross-check recorded in the spec, kept as a test so a divergence SURFACES.

    `NseTradingSessionCalendar` stays the single authority; `pandas_market_calendars` is a second
    opinion. The failure this prevents is a session that traded on a holiday because one authority
    was never checked against anything.
    """
    calendars = pytest.importorskip("pandas_market_calendars")
    from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

    independent = {
        stamp.date()
        for stamp in calendars.get_calendar("NSE")
        .schedule(start_date="2026-08-10", end_date="2026-08-20")
        .index
    }
    ours = {
        day
        for day in (date(2026, 8, 10) + timedelta(days=offset) for offset in range(11))
        if NseTradingSessionCalendar().is_trading_session(day)
    }
    assert ours == independent
