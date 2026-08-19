"""Seed a freshly started loop's bots from prior sessions, so the first tick can decide (`B43`).

**The arithmetic this exists for, not a bug.** `IntradayMeanReversionEngine` fills a 20-bar rolling
window before it counts a single deviation, then needs `MINIMUM_OBSERVATIONS_FOR_BANDS = 60` of
them: **80 five-minute bars**. An NSE session is 09:15 to 15:30 — 375 minutes, **75 bars**.
Shortfall five bars; 1.07 sessions required. A process that starts cold in the morning therefore
cannot band before the close however well the rest of the chain works, and the live loop measured
exactly that: **80,486 bars built, 1,844 panels mature, `engines_mature = 0` at every tick**. The
loop was not failing; the arithmetic forbade it.

**Why seeding and not a smaller window.** Shrinking the window changes the strategy the
`reversion_calibration` was fitted on, so the bands would be measured at a depth the instrument was
never calibrated at — `A.106`'s defect, a calibration looked up under a coordinate measured a
different way.

**Point-in-time, and this is the load-bearing part.** Each seeded step assembles that session's OWN
universe rather than replaying today's prices at yesterday's timestamps. The cheaper version —
observing the same prices N times to fill the window — produces a **zero dispersion**, which makes
every deviation infinite and every bot propose everything. `SegmentBotPaperSession._warm_up` already
holds that discipline for the daily-cadence path; this is the same discipline for the live loop,
expressed once so the two cannot drift.

**Each observation carries its own session's close as its instant.** `observe` is idempotent BY
INSTANT (`B35`), so stamping every seeded observation with `now` would make the first one land and
every one after it be silently ignored — a seeding step that reports success and seeds nothing.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Final, Protocol

from nse_algo_trader.paper_loop.segment_bot_paper_session import UNIFORM_REGIME
from nse_algo_trader.replay_session_clock import IST
from nse_algo_trader.segment_bots.segment_bot_protocol import SegmentBotContext
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment

NSE_SESSION_CLOSE: Final = time(15, 30)
"""The exchange's own close. Each seeded observation is stamped here rather than at `now`, so the
instants are distinct AND correspond to when the prices being observed were actually true."""


class WarmStartSeedingError(Exception):
    """Seeding was asked for with nothing to seed — no bots at all."""


class SeedableBot(Protocol):
    """The two things seeding needs of a bot, named so a test can answer them."""

    trading_segment: TradingSegment
    bot_identity: str

    def observe(self, context: SegmentBotContext) -> None: ...


@dataclass(frozen=True)
class WarmStartSeedingOutcome:
    """What seeding actually managed, per bot — never a bare success.

    A seeding step that reports only "done" cannot distinguish a bot warmed over 25 sessions from a
    bot whose store had nothing in it, and the second one is indistinguishable at the next tick from
    a bot that is simply being cautious.
    """

    observations_by_bot: Mapping[str, int]
    empty_sessions: int
    failures_by_bot: Mapping[str, str] = field(default_factory=dict)
    sessions_offered: int = 0

    def describe(self) -> str:
        if not self.sessions_offered:
            return "nothing to seed from — no prior session carries data"
        per_bot = " · ".join(
            f"{identity} {count}"
            for identity, count in sorted(self.observations_by_bot.items())
        )
        line = f"seeded over {self.sessions_offered} session(s): {per_bot}"
        if self.empty_sessions:
            line += f"; {self.empty_sessions} session(s) had no universe"
        if self.failures_by_bot:
            failed = ", ".join(
                f"{identity} ({reason})"
                for identity, reason in sorted(self.failures_by_bot.items())
            )
            line += f"; FAILED for {failed}"
        return line


def seed_bots_from_prior_sessions(
    bots: Sequence[SeedableBot],
    *,
    sessions: Sequence[date],
    universe_for: Callable[[TradingSegment, datetime], Any],
    maximum_sessions: int | None = None,
) -> WarmStartSeedingOutcome:
    """Observe each bot over the given prior sessions, oldest first, each at its own close.

    `maximum_sessions` keeps the MOST RECENT sessions when it binds. Seeding from the oldest of a
    long archive would band the engine on a dispersion the instrument has since moved away from,
    which is a worse answer than seeding from fewer sessions.

    A bot that raises is recorded and the rest are still seeded: one bot left cold is a bot that
    abstains, and five bots left cold because of it is an outage.
    """
    if not bots:
        raise WarmStartSeedingError(
            "asked to seed no bots; a seeding step with nothing to seed reports success and "
            "leaves the loop exactly as cold as it found it"
        )

    ordered = [
        datetime.combine(session, NSE_SESSION_CLOSE, tzinfo=IST) for session in sorted(sessions)
    ]
    return seed_bots_from_prior_instants(
        bots, instants=ordered, universe_for=universe_for, maximum_observations=maximum_sessions
    )


def seed_bots_from_prior_instants(
    bots: Sequence[SeedableBot],
    *,
    instants: Sequence[datetime],
    universe_for: Callable[[TradingSegment, datetime], Any],
    maximum_observations: int | None = None,
) -> WarmStartSeedingOutcome:
    """Seed on arbitrary instants, so each bot is warmed on ITS OWN clock.

    The cash bot bands on five-minute bars and the five derivative bots decide on daily closes.
    Warming the cash bot on daily closes would fill its window with observations spaced a day apart
    and then advance it with observations spaced five minutes apart — one rolling dispersion built
    from two different clocks, which is `A.106`'s defect wearing a different hat. So the caller
    chooses the instants and this function does not assume them.
    """
    if not bots:
        raise WarmStartSeedingError(
            "asked to seed no bots; a seeding step with nothing to seed reports success and "
            "leaves the loop exactly as cold as it found it"
        )

    ordered = sorted(instants)
    if maximum_observations is not None and maximum_observations >= 0:
        ordered = ordered[-maximum_observations:] if maximum_observations else []

    observations: dict[str, int] = {bot.bot_identity: 0 for bot in bots}
    failures: dict[str, str] = {}
    empty = 0

    for instant in ordered:
        for bot in bots:
            if bot.bot_identity in failures:
                continue
            try:
                universe = universe_for(bot.trading_segment, instant)
            except Exception as failure:  # noqa: BLE001 — one store's bad day is not an outage
                failures[bot.bot_identity] = f"{type(failure).__name__}: {failure}"
                continue
            instruments = tuple(getattr(universe, "instruments", ()) or ())
            if not instruments:
                empty += 1
                continue
            try:
                bot.observe(_context_for(instant, universe, instruments))
            except Exception as failure:  # noqa: BLE001 — recorded per bot, never raised
                failures[bot.bot_identity] = f"{type(failure).__name__}: {failure}"
                continue
            observations[bot.bot_identity] += 1

    return WarmStartSeedingOutcome(
        observations_by_bot=observations,
        empty_sessions=empty,
        failures_by_bot=failures,
        sessions_offered=len(ordered),
    )


def _context_for(
    instant: datetime, universe: Any, instruments: tuple[Any, ...]
) -> SegmentBotContext:
    """One seeded observation, carrying that session's own prices.

    `carried_memory()` is asked of the universe when it offers it, because a derivative universe
    carries underlying spots as well as contract prices and a variance-premium engine needs both.
    """
    carried_memory = getattr(universe, "carried_memory", None)
    memory = (
        carried_memory()
        if callable(carried_memory)
        else {"last_price_paise_by_token": getattr(universe, "last_price_paise_by_token", {})}
    )
    return SegmentBotContext(
        decision_instant=instant,
        tradeable_universe=instruments,
        regime=UNIFORM_REGIME,
        carried_memory=memory,
    )
