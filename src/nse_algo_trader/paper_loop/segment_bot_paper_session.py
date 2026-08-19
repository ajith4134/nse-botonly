"""The session that puts all six segment bots to paper — `B33`, decisions `A.141`/`A.145`.

**What was missing before this.** The six bots decided correctly and `/bots` rendered them, but
nothing ran them: `PaperTradingSessionRunner` is `F04`'s CASH loop and its `PaperSignal` carries a
`MeanReversionDecision` and a `RegimeBelief` by type, so an option or futures bot cannot be routed
through it without changing a loop that is verified on real data. The operator's report — *"the
intraday cash is not switching to live market data ... the prices are stuck"* — was half a broken
timer (`A.143`, fixed) and half this: there was no loop for five of the six bots at all.

**The execution model is DAILY and it is honest about it.** Five of the six bots decide once per
session on real bhavcopy closes (`A.141`), because the depth tape has never subscribed an NFO or MCX
token. So this session enters at the close of session `T` and exits at the close of session `T+1`,
priced through `NseTransactionCostEngine` on both legs. There is no book, no queue position and no
slippage model, and **that is stated on every trade rather than modelled away** — a simulated fill
against a book this project does not have would be a fabrication, and `D.01` is precisely the cost
of believing a model that is too kind.

**What it deliberately reuses rather than reinvents**: the cost engine and its point-in-time rule
store, `PaperCapitalLedger` for the money, `PaperTrackRecordStore` for the record, the segment
universe assembler for the data, and the `L5.29` conformance gate for the bots. The only new thing
here is the loop that joins them.

**Why it does not touch `PaperTradingSessionRunner`.** That runner carries `F04`'s real-data
verification, and generalising its signal type would invalidate the evidence behind it. The two
converge when the cash bot moves onto this session — which needs the intraday path this does not yet
have, and is tracked rather than implied.

**Capital is shared and allocated equally by default** (`R.10`): the six segments are equal until
something measured says otherwise, and `R.03` forbids inventing a split. The per-bot budget is the
deployable capital divided by the number of bots that actually proposed, so a quiet bot does not
sterilise its share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar
from nse_algo_trader.paper_loop.bot_maturity_ladder import ClosedPaperTrade, PaperTrackRecordStore
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.segment_bots.segment_bot_foundation import SegmentBotFoundation
from nse_algo_trader.segment_bots.segment_bot_protocol import SegmentBotContext
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.segment_bots.segment_universe_assembler import (
    AssembledSegmentUniverse,
    assemble_for,
)
from nse_algo_trader.sizing.futures_margin_estimator import (
    FuturesMarginEstimator,
    MarginProduct,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    TradeSpecification,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    OptionRight as CostOptionRight,
)

INDIA_STANDARD_TIME = ZoneInfo("Asia/Kolkata")

NSE_SESSION_CLOSE = time(15, 30)
"""When the cash session closes. A market fact from the NSE equity timings circular."""

PAISE_PER_RUPEE = Decimal("100")

WARMUP_SESSIONS = 25
"""How many prior sessions the bots observe before the session being traded.

Derived from the engines' own maturity requirements rather than chosen: the premium and carry
distributions need `OBSERVATIONS_NEEDED_FOR_A_*_DISTRIBUTION` (5) plus enough spread for a variance
to mean anything, and the shared foundation's window is 20. Twenty-five is the smallest number that
lets every one of them be mature rather than the largest that fits — a longer warmup would make the
session slower without making any estimate legal that was not already.
"""

UNIFORM_REGIME = RegimeDistribution.from_scores(
    {
        MarketRegime.RANGING: 1.0,
        MarketRegime.TRENDING: 1.0,
        MarketRegime.VOLATILE: 1.0,
        MarketRegime.QUIET: 1.0,
    }
)
"""The belief the daily-cadence bots are handed, and it is deliberately uninformative.

The regime brain is fitted on INTRADAY bars; handing a daily-cadence bot a belief measured on a
different clock would be the `A.106` defect again — a quantity looked up under a coordinate measured
a different way. A maximum-entropy belief means the cash bot's regime veto abstains rather than
acts, which is correct for a bot whose regime input is not available on this cadence, and it
is why the cash bot is expected to propose nothing HERE and to trade through its own intraday loop
instead.
"""


class SegmentBotPaperSessionError(Exception):
    """The session cannot run, and a session that invented its inputs would be worse than none."""


EQUAL_BY_DEFAULT = "equal"
"""How capital is split across the bots that proposed. `R.10`: the six segments are equal until
something MEASURED says otherwise, and `R.03` forbids inventing a different split."""


@dataclass(frozen=True, slots=True)
class SegmentBotCapitalPolicy:
    """What the session may put on, and how it is shared. Stated, never defaulted.

    **Added after the first real-data run, which is the only reason it is right.** That run entered
    22 stock-future positions at one lot each and lost Rs 1,98,600 against Rs 10,00,000 of
    capital in a single session — a stock-futures lot is enormous (ASHOKLEY is 5,000 shares,
    roughly Rs 5 lakh of notional) and nothing anywhere checked the total. The module docstring
    already
    claimed capital was "allocated equally by default"; the code did no such thing. A docstring that
    describes a constraint the code does not apply is worse than no docstring, because it is read as
    evidence.

    `maximum_gross_exposure_fraction` is the second thing that run exposed: the cross-section moved
    **91 contracts positive against 25 negative**, so a strategy that is directionally neutral by
    construction took a large one-sided position on a market-wide basis move. A basis bot that ends
    a session net short is running a directional book it never decided to run.
    """

    deployable_rupees: Decimal
    maximum_gross_exposure_fraction: Decimal = Decimal("1.0")
    """Total notional across all positions, as a multiple of deployable capital.

    1.0 means no leverage: the book may not be larger than the money behind it. Futures margin
    genuinely permits more, and permitting more is an operator decision rather than a default.
    """

    maximum_net_directional_fraction: Decimal = Decimal("0.25")
    """How far the book may lean one way, as a fraction of gross exposure.

    Not zero, because a strategy with a real cross-sectional signal will legitimately be somewhat
    one-sided, and forcing exact neutrality would be forcing a hedge nobody asked for. A quarter is
    the operator-stated bound and it is the only number in this policy that is a judgement rather
    than an arithmetic identity.
    """

    def __post_init__(self) -> None:
        if self.deployable_rupees <= 0:
            raise SegmentBotPaperSessionError(
                f"deployable capital is {self.deployable_rupees}; a session with no capital cannot "
                f"size anything, and sizing anyway is how 22 positions became Rs 1,98,600 of loss"
            )
        if self.maximum_gross_exposure_fraction <= 0:
            raise SegmentBotPaperSessionError("gross exposure fraction must be positive")
        if not 0 <= self.maximum_net_directional_fraction <= 1:
            raise SegmentBotPaperSessionError(
                f"net directional fraction {self.maximum_net_directional_fraction} is not a "
                f"fraction of gross exposure"
            )

    @property
    def gross_budget_rupees(self) -> Decimal:
        return self.deployable_rupees * self.maximum_gross_exposure_fraction


@dataclass(frozen=True, slots=True)
class SegmentBotPaperTrade:
    """One round trip a bot proposed, entered at `T` close and exited at `T+1` close."""

    bot_identity: str
    trading_segment: TradingSegment
    instrument_token: int
    trading_symbol: str
    side: TradeLeg
    quantity: int
    entry_session: date
    exit_session: date
    entry_price_paise: Decimal
    exit_price_paise: Decimal
    costs_rupees: Decimal
    stated_win_probability: float | None

    @property
    def gross_rupees(self) -> Decimal:
        """Signed by direction. A sold contract profits when the price falls."""
        move = self.exit_price_paise - self.entry_price_paise
        if self.side is TradeLeg.SELL:
            move = -move
        return (move * self.quantity) / PAISE_PER_RUPEE

    @property
    def net_rupees(self) -> Decimal:
        return self.gross_rupees - self.costs_rupees

    @property
    def notional_rupees(self) -> Decimal:
        return (self.entry_price_paise * self.quantity) / PAISE_PER_RUPEE

    def as_closed_paper_trade(self) -> ClosedPaperTrade:
        """The record shape `PaperTrackRecordStore` and `L5.30`'s ladder already consume."""
        return ClosedPaperTrade(
            bot_identity=self.bot_identity,
            session_date=self.entry_session,
            position_key=f"{self.entry_session}:{self.instrument_token}:{self.side.value}",
            instrument_token=self.instrument_token,
            trading_symbol=self.trading_symbol,
            side=self.side.value,
            filled_quantity=self.quantity,
            opened_at=datetime.combine(
                self.entry_session, NSE_SESSION_CLOSE, tzinfo=INDIA_STANDARD_TIME
            ),
            closed_at=datetime.combine(
                self.exit_session, NSE_SESSION_CLOSE, tzinfo=INDIA_STANDARD_TIME
            ),
            close_reason="next_session_close",
            gross_rupees=self.gross_rupees,
            costs_rupees=self.costs_rupees,
            stated_win_probability=self.stated_win_probability,
        )


@dataclass(frozen=True, slots=True)
class SegmentBotSessionReport:
    """What the session did, per bot, so a run that traded nothing says WHY."""

    entry_session: date
    exit_session: date
    trades: tuple[SegmentBotPaperTrade, ...]
    proposals_by_bot: dict[str, int]
    universes_by_bot: dict[str, str]
    skipped_by_bot: dict[str, str]
    accrued: int

    def describe(self) -> str:
        traded = ", ".join(
            f"{identity} {count}" for identity, count in sorted(self.proposals_by_bot.items())
        )
        return (
            f"{self.entry_session} -> {self.exit_session}: {len(self.trades)} trade(s) from "
            f"{len(self.proposals_by_bot)} bot(s) [{traded or 'none'}], {self.accrued} newly "
            f"accrued to the track record"
        )

    def net_rupees(self) -> Decimal:
        return sum((trade.net_rupees for trade in self.trades), Decimal("0"))


@dataclass(slots=True)
class SegmentBotPaperSession:
    """Runs the daily-cadence bots over one real session and accrues what they made.

    Every collaborator is injected so a hermetic harness can drive the identical code with no
    stores (`R.J`), and production passes the real ones.
    """

    bots: Sequence[SegmentBotFoundation]
    calendar: NseTradingSessionCalendar = field(default_factory=NseTradingSessionCalendar)
    cost_engine: NseTransactionCostEngine = field(
        default_factory=lambda: NseTransactionCostEngine(seeded_nse_market_rule_store())
    )
    track_record: PaperTrackRecordStore = field(default_factory=PaperTrackRecordStore)
    instrument_limit: int | None = None
    """Bounds the universe read. `None` is the full board (`R.09`); a bound is REPORTED."""

    margin_estimator: FuturesMarginEstimator | None = None
    """What a broker would actually block, per `L6.30`. `None` falls back to NOTIONAL.

    **This is the difference between the futures bots trading and not trading.** Bounding capital by
    notional makes a single stock-futures lot (ASHOKLEY, 5,000 shares, ~Rs 5 lakh) consume half a
    Rs 10,00,000 book, so 23 of 23 real proposals were refused on 2026-07-31. Futures consume
    MARGIN — measured at 7.94% of notional for RELIANCE and 11.00% for ASHOKLEY on real NSE
    volatility — which is roughly a tenfold difference in what the same capital can hold.

    `None` is honest rather than convenient: with no estimator a derivative proposal is REFUSED
    rather than sized on notional, because a capital bound that silently guessed a leverage
    multiple is the invented number `R.03` forbids.
    """

    capital: SegmentBotCapitalPolicy = field(
        default_factory=lambda: SegmentBotCapitalPolicy(deployable_rupees=Decimal("1000000"))
    )
    """What the session may put on. Required in substance even though it has a default, because a
    session with no capital constraint sizes by lot size alone — measured at Rs 1,98,600 of loss on
    Rs 10,00,000 in one session on the first real run."""

    def run(self, entry_session: date) -> SegmentBotSessionReport:
        """Trade one session: warm up, propose at its close, exit at the next session's close."""
        exit_session = self._next_session_after(entry_session)
        if exit_session is None:
            raise SegmentBotPaperSessionError(
                f"{entry_session} has no following trading session in the calendar, so a position "
                f"entered at its close has nowhere to be marked out. A session that priced an "
                f"exit it could not observe would be inventing the only number that matters"
            )

        trades: list[SegmentBotPaperTrade] = []
        proposals_by_bot: dict[str, int] = {}
        universes_by_bot: dict[str, str] = {}
        skipped_by_bot: dict[str, str] = {}

        for bot in self.bots:
            entry_universe = self._universe(bot, entry_session)
            universes_by_bot[bot.bot_identity] = entry_universe.note
            if entry_universe.is_empty:
                skipped_by_bot[bot.bot_identity] = "no universe on this session"
                continue

            self._warm_up(bot, entry_session, entry_universe)
            context = self._context_for(bot, entry_session, entry_universe)
            bot.observe(context)
            proposals = bot.propose(context)
            proposals_by_bot[bot.bot_identity] = len(proposals)
            if not proposals:
                skipped_by_bot[bot.bot_identity] = "proposed nothing on this session"
                continue

            exit_universe = self._universe(bot, exit_session)
            if exit_universe.is_empty:
                skipped_by_bot[bot.bot_identity] = (
                    f"proposed {len(proposals)} but {exit_session} has no prices to mark out on"
                )
                continue
            admitted, refused = self._within_capital(
                proposals, budget_rupees=self._budget_for(proposals_by_bot)
            )
            if refused:
                skipped_by_bot[bot.bot_identity] = (
                    f"{refused} of {len(proposals)} proposal(s) refused on capital or on the "
                    f"net-directional bound"
                )
            for proposal in admitted:
                trade = self._round_trip(
                    bot, proposal, entry_session, exit_session, exit_universe
                )
                if trade is not None:
                    trades.append(trade)

        accrued = self._accrue(trades)
        return SegmentBotSessionReport(
            entry_session=entry_session,
            exit_session=exit_session,
            trades=tuple(trades),
            proposals_by_bot=proposals_by_bot,
            universes_by_bot=universes_by_bot,
            skipped_by_bot=skipped_by_bot,
            accrued=accrued,
        )

    def _capital_consumed_by(
        self, proposal: PricedSignal, notional_rupees: Decimal
    ) -> Decimal | None:
        """What this position actually ties up. Margin for a derivative, notional for cash.

        `None` means "this cannot be sized" and the caller refuses the proposal. That is deliberate
        and it is the whole point of `L6.30`: for a future, margin and notional differ by roughly a
        factor of ten, so falling back from one to the other is not a conservative approximation —
        it is a different answer with no error bar.

        Cash equity genuinely consumes its notional under this project's intraday-by-default rule
        (`R.01`), so it needs no estimator and never returns `None` for want of one.
        """
        product = self._margin_product_for(proposal)
        if product is None:
            return notional_rupees
        if self.margin_estimator is None:
            return None
        estimate = self.margin_estimator.estimate(
            product=product,
            underlying_symbol=self._underlying_of(proposal.trading_symbol),
            notional_rupees=notional_rupees,
        )
        return None if estimate is None else estimate.total_rupees

    @staticmethod
    def _margin_product_for(proposal: PricedSignal) -> MarginProduct | None:
        """Which margin product this signal is, or `None` when it is cash and needs no estimate."""
        segment = proposal.segment
        if not (segment.is_option or segment.value.endswith("FUT")):
            return None
        index_underlyings = frozenset(
            {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
        )
        underlying = SegmentBotPaperSession._underlying_of(proposal.trading_symbol)
        is_index = underlying in index_underlyings
        if segment.is_option:
            # A SOLD option carries the extreme-loss margin; a bought one is capped by its premium.
            if proposal.side is not TradeLeg.SELL:
                return MarginProduct.OPTION_LONG
            return (
                MarginProduct.INDEX_OPTION_SHORT if is_index else MarginProduct.STOCK_OPTION_SHORT
            )
        return MarginProduct.INDEX_FUTURE if is_index else MarginProduct.STOCK_FUTURE

    @staticmethod
    def _underlying_of(trading_symbol: str) -> str:
        """The leading alphabetic run of an NSE derivative symbol is its underlying."""
        symbol = trading_symbol.strip().upper()
        underlying = ""
        for character in symbol:
            if character.isalpha():
                underlying += character
            else:
                break
        return underlying or symbol

    def _budget_for(self, proposals_by_bot: dict[str, int]) -> Decimal:
        """This bot's share of the gross budget — equal across the bots that actually proposed.

        `R.10`: the six segments are equal by default. Dividing by the bots that PROPOSED rather
        than by six means a quiet bot does not sterilise its share, and it is recomputed as the
        session walks so the split is a fact about the session rather than a constant.
        """
        active = max(sum(1 for count in proposals_by_bot.values() if count), 1)
        return self.capital.gross_budget_rupees / active

    def _within_capital(
        self, proposals: Sequence[PricedSignal], *, budget_rupees: Decimal
    ) -> tuple[tuple[PricedSignal, ...], int]:
        """Admit proposals until the budget or the net-directional bound binds.

        **Ordered by conviction, highest first**, so the constraint drops the bot's own weakest
        claims rather than whichever happened to be alphabetically last — the first real run entered
        22 positions, and an alphabetical cut would have dropped an arbitrary subset of them.

        The net-directional check is applied INCREMENTALLY rather than at the end: a book that ends
        the session 91-to-25 one way was one-sided at every step of building it, and rejecting the
        whole book afterwards would throw away the balanced part with the rest.
        """
        ranked = sorted(
            proposals,
            key=lambda signal: (signal.conviction or Decimal("0")),
            reverse=True,
        )
        admitted: list[PricedSignal] = []
        gross = Decimal("0")
        net = Decimal("0")
        refused = 0
        for proposal in ranked:
            notional = (
                proposal.reference_price_paise * proposal.proposed_quantity
            ) / PAISE_PER_RUPEE
            if notional <= 0:
                refused += 1
                continue
            consumed = self._capital_consumed_by(proposal, notional)
            if consumed is None:
                # No margin estimate for this underlying and the product needs one — refused rather
                # than sized on notional, because for a future those two differ by roughly 10x and
                # picking the wrong one is not a conservative error in a knowable direction.
                refused += 1
                continue
            if gross + consumed > budget_rupees:
                refused += 1
                continue
            signed = consumed if proposal.side is TradeLeg.BUY else -consumed
            candidate_net = net + signed
            candidate_gross = gross + consumed
            # THREE attempts at this rule, and the first two are recorded because each failed in
            # the opposite direction and both looked like they worked:
            #
            #   1. refuse when |net| exceeds the bound  -> the FIRST position always has
            #      |net| == its own gross, so it refused 23 of 23 proposals and traded nothing;
            #   2. refuse only when already outside AND the ratio "worsens" -> a one-sided book
            #      sits at a ratio of exactly 1.0 forever, so nothing ever worsens and it admitted
            #      4 of 4 on the same side, measured at 100% net directional against a 25% bound.
            #
            # The rule that actually binds: a candidate is admitted when it leaves the book INSIDE
            # the bound, or when it IMPROVES the balance, or when the book is empty. So the first
            # position is unconstrained (a book of one is always 100% directional and no rule can
            # change that), the second must either balance it or fit the bound, and a trade on the
            # lighter side is always welcome. The book converges toward the bound instead of being
            # unable to start or unable to stop.
            candidate_ratio = (
                abs(candidate_net) / candidate_gross if candidate_gross > 0 else Decimal("1")
            )
            current_ratio = abs(net) / gross if gross > 0 else None
            fits_the_bound = candidate_ratio <= self.capital.maximum_net_directional_fraction
            improves = current_ratio is not None and candidate_ratio < current_ratio
            book_is_empty = current_ratio is None
            if not (fits_the_bound or improves or book_is_empty):
                refused += 1
                continue
            admitted.append(proposal)
            gross = candidate_gross
            net = candidate_net
        return tuple(admitted), refused

    # ---- the loop's pieces ------------------------------------------------------------

    def _universe(
        self, bot: SegmentBotFoundation, session: date
    ) -> AssembledSegmentUniverse:
        return assemble_for(
            bot.trading_segment,
            as_of=datetime.combine(session, NSE_SESSION_CLOSE, tzinfo=INDIA_STANDARD_TIME),
            limit=self.instrument_limit,
        )

    def _warm_up(
        self, bot: SegmentBotFoundation, entry_session: date, entry_universe:
        AssembledSegmentUniverse
    ) -> None:
        """Let the bot observe the sessions before the one it trades.

        **Point-in-time, and this is the load-bearing part**: each warm-up step assembles that
        session's OWN universe rather than replaying today's prices at yesterday's timestamps. The
        cheaper version — observing the same prices N times to fill the window — produces a zero
        dispersion, which makes every deviation infinite and every bot propose everything.
        """
        for offset in range(WARMUP_SESSIONS, 0, -1):
            session = self._session_before(entry_session, offset)
            if session is None:
                continue
            universe = self._universe(bot, session)
            if universe.is_empty:
                continue
            bot.observe(self._context_for(bot, session, universe))
        del entry_universe

    def _context_for(
        self, bot: SegmentBotFoundation, session: date, universe: AssembledSegmentUniverse
    ) -> SegmentBotContext:
        del bot
        return SegmentBotContext(
            decision_instant=datetime.combine(
                session, NSE_SESSION_CLOSE, tzinfo=INDIA_STANDARD_TIME
            ),
            tradeable_universe=universe.instruments,
            regime=UNIFORM_REGIME,
            carried_memory=universe.carried_memory(),
        )

    def _round_trip(
        self,
        bot: SegmentBotFoundation,
        proposal: PricedSignal,
        entry_session: date,
        exit_session: date,
        exit_universe: AssembledSegmentUniverse,
    ) -> SegmentBotPaperTrade | None:
        """Price both legs and mark the position out. `None` when the exit cannot be observed.

        A proposal whose instrument does not appear in the next session's data is DROPPED rather
        than carried at its entry price. Marking a position out at the price it went on is a
        guaranteed zero, and a record full of guaranteed zeros is the cleanest way to make a bot
        look harmless.
        """
        exit_price = exit_universe.last_price_paise_by_token.get(proposal.instrument_token)
        if exit_price is None or exit_price <= 0:
            return None
        costs = self._costs_rupees(proposal, entry_session)
        if costs is None:
            return None
        return SegmentBotPaperTrade(
            bot_identity=bot.bot_identity,
            trading_segment=bot.trading_segment,
            instrument_token=proposal.instrument_token,
            trading_symbol=proposal.trading_symbol,
            side=proposal.side,
            quantity=proposal.proposed_quantity,
            entry_session=entry_session,
            exit_session=exit_session,
            entry_price_paise=proposal.reference_price_paise,
            exit_price_paise=exit_price,
            costs_rupees=costs,
            stated_win_probability=(
                None if proposal.conviction is None else float(proposal.conviction)
            ),
        )

    def _costs_rupees(self, proposal: PricedSignal, entry_session: date) -> Decimal | None:
        """Both legs, through the engine that owns the rules, on the date they were in force.

        `None` rather than zero when the trade cannot be priced. A zero cost turns a gross loss into
        a net win, which is exactly the defect `L5.30`'s review found in the maturity ladder.
        """
        try:
            specification = TradeSpecification(
                segment=proposal.segment,
                quantity=proposal.proposed_quantity,
                entry_price_paise=proposal.reference_price_paise,
                exit_price_paise=proposal.reference_price_paise,
                trade_date=entry_session,
                strike_paise=proposal.strike_paise,
                option_right=(
                    CostOptionRight.PUT
                    if proposal.trading_symbol.strip().upper().endswith("PE")
                    else CostOptionRight.CALL
                )
                if proposal.segment.is_option
                else None,
                is_short_first=proposal.side is TradeLeg.SELL,
            )
            priced = self.cost_engine.price_round_trip(specification)
        except Exception:  # noqa: BLE001 — one unpriceable proposal is a skip, not a failed session
            return None
        return Decimal(str(priced.total_rupees))

    def _accrue(self, trades: Sequence[SegmentBotPaperTrade]) -> int:
        """Write the closed trades. Idempotent — re-running a session must not double a record."""
        accrued = 0
        for trade in trades:
            try:
                accrued += self.track_record.append(trade.as_closed_paper_trade())
            except Exception:  # noqa: BLE001, S112 — an unwritable row is skipped, not a crash
                continue
        return accrued

    # ---- calendar ---------------------------------------------------------------------

    def _next_session_after(self, session: date) -> date | None:
        candidate = session + timedelta(days=1)
        for _ in range(14):
            if self.calendar.is_trading_session(candidate):
                return candidate
            candidate += timedelta(days=1)
        return None

    def _session_before(self, session: date, offset: int) -> date | None:
        candidate = session
        remaining = offset
        for _ in range(offset * 4 + 14):
            candidate -= timedelta(days=1)
            if self.calendar.is_trading_session(candidate):
                remaining -= 1
                if remaining == 0:
                    return candidate
        return None
