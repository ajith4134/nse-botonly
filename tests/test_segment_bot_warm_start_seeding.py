"""Tests for warm-start seeding of a freshly started loop (`B43`, `A.146`), written first.

`B43` is arithmetic, not a bug: `IntradayMeanReversionEngine` fills a 20-bar rolling window before
it counts a single deviation, then needs 60 of them — **80 five-minute bars** — and an NSE session
is 09:15 to 15:30, which is **75**. A process that starts cold in the morning can never band,
however well the rest of the chain works, and the live loop showed exactly that: 80,486 bars built,
1,844 panels mature, and `engines_mature = 0` at every tick.

The fix is seeding, never a smaller window: shrinking the window changes the strategy the
`reversion_calibration` was fitted on (`A.106`), and the bands would then be measured at a depth the
instrument was never calibrated at.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nse_algo_trader.paper_loop.segment_bot_warm_start_seeding import (
    WarmStartSeedingError,
    seed_bots_from_prior_sessions,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment


class RecordingBot:
    """A bot that only records what it was asked to observe."""

    def __init__(self, segment: TradingSegment = TradingSegment.CASH_INTRADAY) -> None:
        self.trading_segment = segment
        self.bot_identity = f"{segment.value}_recording_bot"
        self.observed: list[SegmentBotContext] = []

    def observe(self, context: SegmentBotContext) -> None:
        self.observed.append(context)


class ExplodingBot(RecordingBot):
    def observe(self, context: SegmentBotContext) -> None:
        raise RuntimeError("this bot cannot observe")


INSTRUMENT = TradeableInstrument(
    instrument_token=1,
    trading_symbol="RELIANCE",
    lot_size=1,
    tick_size_paise=5,
    strike_paise=None,
    expiry=None,
)

SESSIONS = (date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13))


def _universe_for(segment: TradingSegment, as_of: datetime):
    """A universe whose prices MOVE per session, so a dispersion can actually form."""
    from nse_algo_trader.segment_bots.segment_universe_assembler import AssembledSegmentUniverse

    day = as_of.date()
    if day not in SESSIONS:
        return AssembledSegmentUniverse(segment, (), {}, {}, {}, None, "no data")
    price = Decimal(str(100_00 + SESSIONS.index(day) * 25))
    return AssembledSegmentUniverse(
        segment,
        (INSTRUMENT,),
        {INSTRUMENT.instrument_token: price},
        {"RELIANCE": price},
        {},
        day,
        f"{day} fixture",
    )


def test_every_prior_session_is_observed_oldest_first() -> None:
    bot = RecordingBot()
    outcome = seed_bots_from_prior_sessions(
        [bot], sessions=SESSIONS, universe_for=_universe_for
    )
    assert [context.decision_instant.date() for context in bot.observed] == list(SESSIONS)
    assert outcome.observations_by_bot[bot.bot_identity] == 3


def test_each_session_is_observed_with_its_own_universe() -> None:
    """The load-bearing property. Replaying today's prices at yesterday's timestamps produces a
    zero dispersion, which makes every deviation infinite and every bot propose everything."""
    bot = RecordingBot()
    seed_bots_from_prior_sessions([bot], sessions=SESSIONS, universe_for=_universe_for)
    prices = [
        context.carried_memory["last_price_paise_by_token"][1] for context in bot.observed
    ]
    assert len(set(prices)) == len(SESSIONS)


def test_a_session_with_no_data_is_skipped_and_counted() -> None:
    bot = RecordingBot()
    outcome = seed_bots_from_prior_sessions(
        [bot], sessions=(*SESSIONS, date(2026, 8, 14)), universe_for=_universe_for
    )
    assert outcome.observations_by_bot[bot.bot_identity] == 3
    assert outcome.empty_sessions == 1


def test_a_bot_that_raises_is_recorded_and_the_others_are_still_seeded() -> None:
    """One bot's bad session must not leave five bots cold."""
    good = RecordingBot()
    bad = ExplodingBot(TradingSegment.INDEX_OPTIONS)
    outcome = seed_bots_from_prior_sessions(
        [bad, good], sessions=SESSIONS, universe_for=_universe_for
    )
    assert outcome.observations_by_bot[good.bot_identity] == 3
    assert bad.bot_identity in outcome.failures_by_bot
    assert "cannot observe" in outcome.failures_by_bot[bad.bot_identity]


def test_seeding_no_sessions_is_answerable_rather_than_fatal() -> None:
    bot = RecordingBot()
    outcome = seed_bots_from_prior_sessions([bot], sessions=(), universe_for=_universe_for)
    assert outcome.observations_by_bot[bot.bot_identity] == 0
    assert "nothing to seed from" in outcome.describe()


def test_seeding_no_bots_is_refused() -> None:
    with pytest.raises(WarmStartSeedingError, match="no bots"):
        seed_bots_from_prior_sessions([], sessions=SESSIONS, universe_for=_universe_for)


def test_the_outcome_says_what_it_did_per_bot() -> None:
    bot = RecordingBot()
    outcome = seed_bots_from_prior_sessions([bot], sessions=SESSIONS, universe_for=_universe_for)
    described = outcome.describe()
    assert bot.bot_identity in described
    assert "3" in described


def test_the_seeding_instant_is_each_session_s_own_close_not_now() -> None:
    """A context stamped `now` would make every seeded observation share one instant, and `observe`
    is idempotent BY INSTANT — so the second session would be silently ignored (`B35`)."""
    bot = RecordingBot()
    seed_bots_from_prior_sessions([bot], sessions=SESSIONS, universe_for=_universe_for)
    instants = [context.decision_instant for context in bot.observed]
    assert len(set(instants)) == len(SESSIONS)
    assert all(instant.tzinfo is not None for instant in instants)


def test_seeding_is_bounded_by_the_requested_depth() -> None:
    bot = RecordingBot()
    outcome = seed_bots_from_prior_sessions(
        [bot], sessions=SESSIONS, universe_for=_universe_for, maximum_sessions=2
    )
    assert outcome.observations_by_bot[bot.bot_identity] == 2
    assert [context.decision_instant.date() for context in bot.observed] == list(SESSIONS[-2:])


def test_a_bounded_seed_takes_the_most_recent_sessions() -> None:
    """Seeding from the oldest two of five would band the engine on stale dispersion."""
    bot = RecordingBot()
    seed_bots_from_prior_sessions(
        [bot], sessions=SESSIONS, universe_for=_universe_for, maximum_sessions=1
    )
    assert bot.observed[0].decision_instant.date() == SESSIONS[-1]


def test_the_clock_used_for_seeding_is_the_exchange_s_not_the_host_s() -> None:
    bot = RecordingBot()
    seed_bots_from_prior_sessions([bot], sessions=SESSIONS, universe_for=_universe_for)
    instant = bot.observed[0].decision_instant
    assert instant.utcoffset() is not None
    assert instant.astimezone(UTC).hour == 10  # 15:30 IST
