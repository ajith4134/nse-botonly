"""The registry that builds all six segment bots and refuses to hand out a non-conforming one.

**Why registration is a gate and not a list.** `L5.29` deliberately made the conformance suite
*return* violations rather than assert them, so the same checks that run in CI also run at runtime.
This is where that pays: a bot is constructed, driven over a small set of contexts including one
with
an EMPTY universe, and admitted only if it comes back clean. A bot that cannot say "nothing today"
never reaches a session.

**Where the I/O lives.** Bots perform none (`L5.29`). The registry does: it reads each bot's own
closed record through `BotMaturityLadder`, turns it into a `BotTrackRecordSummary`, and hands that
to
the bot at construction. That keeps the decision path replayable while the ladder stays measured
from
real trades rather than asserted (`R.08`).

**The six, and what each is waiting on** — stated here because the honest answer differs per bot and
`R.11` says a deferral is named, never implied:

- `cash_intraday_mean_reversion_bot` — INTRADAY, real five-minute bars and a live depth tape;
- `index_options_volatility_premium_bot` / `stock_options_volatility_premium_bot` — once per session
  on daily bhavcopy, lifting to intraday when `A.142`'s F&O capture fills;
- `index_futures_basis_carry_bot` — the same, on 540 rows of `IDF` history (`B31`);
- `stock_futures_basis_carry_bot` — the same, on 22,561 rows of `STF`;
- `commodity_mcx_basis_carry_bot` — built whole, activating on nothing until MCX ingestion lands
  (`B30`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from nse_algo_trader.paper_loop.bot_maturity_ladder import (
    BotMaturityLadder,
    LadderPolicy,
    PaperTrackRecordStore,
)
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.segment_bots.cash_intraday_mean_reversion_bot import (
    CashIntradayMeanReversionBot,
)
from nse_algo_trader.segment_bots.futures_basis_carry_bots import (
    CommodityMcxBasisCarryBot,
    IndexFutureBasisCarryBot,
    StockFutureBasisCarryBot,
)
from nse_algo_trader.segment_bots.option_volatility_premium_bots import (
    IndexOptionVolatilityPremiumBot,
    StockOptionVolatilityPremiumBot,
)
from nse_algo_trader.segment_bots.segment_bot_conformance import (
    ConformanceViolation,
    run_segment_bot_conformance,
)
from nse_algo_trader.segment_bots.segment_bot_foundation import (
    BotTrackRecordSummary,
    SegmentBotFoundation,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment

REGISTRATION_PROBE_INSTANTS = 3
"""How many contexts the registration probe drives a bot over.

Three, not one: the suite's `relevance-is-stable` and `context-is-unmutated` checks both need more
than a single call to mean anything, and the third context is the empty universe the suite itself
demands.
"""

BALANCED_REGISTRATION_REGIME = RegimeDistribution.from_scores(
    {
        MarketRegime.RANGING: 1.0,
        MarketRegime.TRENDING: 1.0,
        MarketRegime.VOLATILE: 1.0,
        MarketRegime.QUIET: 1.0,
    }
)
"""A maximum-entropy belief for the registration probe.

Uniform on purpose: registration tests the CONTRACT, not the strategy. Handing a bot a belief that
favours its own strategy would let a bot pass registration precisely when it is most likely to act,
which is the opposite of what a gate is for.
"""


@dataclass(frozen=True, slots=True)
class RegisteredSegmentBot:
    """One admitted bot, with the evidence that admitted it."""

    bot: SegmentBotFoundation
    violations: tuple[ConformanceViolation, ...]

    @property
    def is_conforming(self) -> bool:
        return not self.violations


class SegmentBotRegistrationRefusedError(Exception):
    """A bot failed the shared conformance suite and must not reach a session."""


def default_ladder_policy() -> LadderPolicy:
    """The activation policy the daily run already uses, so the ladder reads the same everywhere.

    Deliberately the same three numbers as `run_daily_operations.DAILY_LADDER_POLICY` rather than a
    second, laxer set. A registry that assessed a bot more generously than the daily report does
    would put two different rungs for the same bot on two surfaces, and the operator would have no
    way to tell which one gates arming.
    """
    return LadderPolicy(
        promotion_confidence=0.95,
        sustained_sessions_required=20,
        minimum_trades_for_a_posterior=30,
    )


def track_record_for(
    bot_identity: str,
    *,
    store: PaperTrackRecordStore | None = None,
    policy: LadderPolicy | None = None,
) -> BotTrackRecordSummary:
    """Read one bot's own closed record off the store. `cold_start` when it has never traded.

    Never raises: a bot whose record cannot be read is a bot at the bottom of the ladder, and a
    registry that fell over on an empty store would make the cold-start case unreachable — which is
    the deadlock `B15` measured, from the other end.
    """
    try:
        assessment = BotMaturityLadder(store or PaperTrackRecordStore()).assess(
            bot_identity, policy or default_ladder_policy()
        )
    except Exception:  # noqa: BLE001 — an unreadable record is the lowest rung, never a crash
        return BotTrackRecordSummary.cold_start()
    return BotTrackRecordSummary(
        closed_trades=assessment.closed_trades,
        sessions=assessment.sessions,
        posterior_above_break_even=assessment.posterior_above_break_even,
        rung=assessment.rung,
    )


def build_all_segment_bots(
    *,
    store: PaperTrackRecordStore | None = None,
    policy: LadderPolicy | None = None,
) -> tuple[SegmentBotFoundation, ...]:
    """All six, each carrying its own measured track record. Order is the plan's,
    `L5.26`-`L5.28`."""
    resolved = store or PaperTrackRecordStore()
    ladder = policy or default_ladder_policy()

    def record(identity: str) -> BotTrackRecordSummary:
        return track_record_for(identity, store=resolved, policy=ladder)

    return (
        CashIntradayMeanReversionBot(
            track_record=record("cash_intraday_mean_reversion_bot")
        ),
        IndexOptionVolatilityPremiumBot(
            track_record=record("index_options_volatility_premium_bot")
        ),
        StockOptionVolatilityPremiumBot(
            track_record=record("stock_options_volatility_premium_bot")
        ),
        IndexFutureBasisCarryBot(track_record=record("index_futures_basis_carry_bot")),
        StockFutureBasisCarryBot(track_record=record("stock_futures_basis_carry_bot")),
        CommodityMcxBasisCarryBot(track_record=record("commodity_mcx_basis_carry_bot")),
    )


def registration_contexts(at: datetime) -> tuple[SegmentBotContext, ...]:
    """The probe every bot is driven over before it is admitted.

    Two populated contexts and one empty one. The populated universe carries a strike and an expiry
    so an option bot has something legal to look at, and a plain cash row so a cash bot does; every
    bot ignores what is not its own, which is itself worth exercising.
    """
    universe: tuple[TradeableInstrument, ...] = (
        TradeableInstrument(
            instrument_token=738561,
            trading_symbol="RELIANCE",
            lot_size=1,
            tick_size_paise=5,
        ),
        TradeableInstrument(
            instrument_token=9_000_001,
            trading_symbol="NIFTY26SEP24800CE",
            lot_size=75,
            tick_size_paise=5,
            strike_paise=Decimal("2480000"),
            expiry=(at + timedelta(days=30)).date(),
        ),
    )
    memory = {
        "last_price_paise_by_token": {738561: Decimal("250000"), 9_000_001: Decimal("20000")},
        "underlying_price_paise_by_symbol": {"NIFTY": Decimal("2480000")},
        "open_interest_by_token": {9_000_001: 100_000},
        "fo_ban_list_symbols": set(),
    }
    populated = tuple(
        SegmentBotContext(
            decision_instant=at + timedelta(minutes=5 * step),
            tradeable_universe=universe,
            regime=BALANCED_REGISTRATION_REGIME,
            carried_memory=memory,
        )
        for step in range(REGISTRATION_PROBE_INSTANTS - 1)
    )
    empty = SegmentBotContext(
        decision_instant=at + timedelta(minutes=5 * REGISTRATION_PROBE_INSTANTS),
        tradeable_universe=(),
        regime=BALANCED_REGISTRATION_REGIME,
        carried_memory={},
    )
    return (*populated, empty)


def register_segment_bots(
    bots: Sequence[SegmentBotFoundation],
    *,
    at: datetime,
    refuse_on_violation: bool = True,
) -> tuple[RegisteredSegmentBot, ...]:
    """Run the shared suite over each bot and admit only the clean ones.

    `refuse_on_violation` defaults to True because a non-conforming bot reaching a session is the
    failure this gate exists to prevent. It is exposed so a surface can render what WOULD be refused
    without taking the dashboard down — a report is allowed to see a broken bot; a session is not.
    """
    contexts = registration_contexts(at)
    registered: list[RegisteredSegmentBot] = []
    for bot in bots:
        for context in contexts:
            bot.observe(context)
        violations = tuple(run_segment_bot_conformance(bot, contexts))
        if violations and refuse_on_violation:
            raise SegmentBotRegistrationRefusedError(
                f"{bot.bot_identity} failed the L5.29 conformance suite with "
                f"{len(violations)} violation(s): "
                + "; ".join(violation.describe() for violation in violations[:3])
            )
        registered.append(RegisteredSegmentBot(bot=bot, violations=violations))
    return tuple(registered)


def conformance_violations_by_identity(
    *,
    at: datetime,
    store: PaperTrackRecordStore | None = None,
    policy: LadderPolicy | None = None,
) -> dict[str, tuple[ConformanceViolation, ...]]:
    """Run the `L5.29` gate on THROWAWAY bots and return the verdict per identity.

    **This exists because registration contaminates carried state, and that is not obvious.** The
    probe feeds every bot two synthetic instruments — a RELIANCE at Rs 2,500 and a NIFTY option at
    Rs 200 — so a bot that is registered and then put to work carries those two prices inside its
    rolling window, its EWMA and its premium distribution for the rest of the session. They are not
    market data. They would move every standardised quantity the bot decides on.

    A second, subtler consequence found the same way: `observe` is idempotent BY INSTANT, so a bot
    already probed at `now` silently ignores a real universe observed at the same `now`. The
    dashboard surface reported two instruments per bot and looked entirely healthy.

    So the gate runs on instances that are then discarded, and the caller builds fresh bots for the
    work. Conformance is a property of the CLASS; carried state is a property of the instance.
    """
    probes = build_all_segment_bots(store=store, policy=policy)
    registered = register_segment_bots(probes, at=at, refuse_on_violation=False)
    return {entry.bot.bot_identity: entry.violations for entry in registered}


def segment_of(bot: SegmentBotFoundation) -> TradingSegment:
    """Named rather than reached through, so the six can be indexed without touching internals."""
    return bot.trading_segment
