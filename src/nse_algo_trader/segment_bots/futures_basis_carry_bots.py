"""The three futures segment bots — index futures, stock futures and MCX commodities. `A.141`.

All three trade the same claim through `FuturesBasisCarryEngine`: a contract whose annualised
implied carry has departed from its OWN history is dislocated, and the dislocation is expected to
close as expiry approaches. They are three bots and not one because each is judged against its own
null and carries its own risk sub-limits — and because their data situations are radically
different,
which the maturity ladder is supposed to express rather than hide.

**Data reality, measured 2026-08-18 and stated on every card:**

- **index futures** — `IDF` **540** rows over 36 sessions. Tradeable, and the ladder will hold it
  low for a long time; recorded as `B31` so a low rung is never read as a broken bot.
- **stock futures** — `STF` **22,561** rows. The healthiest of the three.
- **MCX commodities** — **zero** rows, and no MCX token has ever been in the depth tape. Built
  whole, **activates on nothing**; recorded as `B30`.

`R.04` is the rule that makes this honest: the MCX bot runs the identical algorithm to its siblings
and is not a stub, a placeholder or a reduced version. What it lacks is data, so what is gated is
its
ACTIVATION. `A.142` records the operator's decision to defer MCX ingestion, so this is a recorded
deferral rather than a silent gap (`R.11`).

**Direction.** An EXPENSIVE carry means the future is rich to its own history, so the future is
SOLD;
CHEAP means it is bought. Neither is a view on the underlying's direction, which is the entire point
of trading basis rather than price.

**Carry rule.** Every one of these segments permits overnight carry as a market fact, and every one
of these bots proposes INTRADAY anyway (`R.01`): the carry decision belongs to a supervisor
promotion, and `SegmentBotFoundation.build_signal` hard-codes `carried_overnight=False` so no bot
can
make it.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar
from nse_algo_trader.segment_bots.segment_bot_foundation import (
    BASIS_POINTS_PER_UNIT,
    PAISE_PER_RUPEE,
    BotTrackRecordSummary,
    DecisionCadence,
    SegmentBotFoundation,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    SegmentBotContext,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.strategy.futures_basis_carry_engine import (
    BasisCarryReading,
    BasisDislocation,
    FuturesBasisCarryEngine,
    OpenInterestBuildup,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

FUTURES_CARRY_WINDOW = 20
"""Sessions of carry history the shared observation window holds."""

BUILDUP_CONVICTION_AGREEMENT = Decimal("1.15")
BUILDUP_CONVICTION_DISAGREEMENT = Decimal("0.85")
"""How much the open-interest taxonomy may move conviction, and no further.

Deliberately small and deliberately symmetric. Open interest is published once a day and this
project
holds 36 sessions of it, so it is evidence about who is arriving and not evidence about price. A
modifier that could double conviction would be trading a series with 36 observations as though it
were the signal; these two bound its influence to +/-15%.
"""


class FuturesBasisCarryBot(SegmentBotFoundation):
    """Shared body of the three futures bots: sell rich carry, buy cheap carry.

    A plain class rather than a `@dataclass` for the measured reason in the cash bot — decorating a
    subclass of a slotted dataclass breaks the zero-argument `super()` closure.
    """

    _carry: FuturesBasisCarryEngine
    _calendar: NseTradingSessionCalendar
    _instants_observed: set[datetime]
    _last_readings: dict[int, BasisCarryReading]
    _last_cut: float | None
    _sessions_per_year_cache: dict[int, int]

    def __init__(
        self,
        *,
        trading_segment: TradingSegment,
        track_record: BotTrackRecordSummary | None = None,
        carry_engine: FuturesBasisCarryEngine | None = None,
        calendar: NseTradingSessionCalendar | None = None,
        window_size: int = FUTURES_CARRY_WINDOW,
    ) -> None:
        SegmentBotFoundation.__init__(
            self,
            trading_segment_value=trading_segment,
            cadence=DecisionCadence.ONCE_PER_SESSION,
            window_size=window_size,
            track_record=track_record or BotTrackRecordSummary.cold_start(),
        )
        self._carry = carry_engine or FuturesBasisCarryEngine()
        self._calendar = calendar or NseTradingSessionCalendar()
        self._instants_observed = set()
        self._last_readings = {}
        self._last_cut = None
        self._sessions_per_year_cache = {}

    # ---- observation ------------------------------------------------------------------

    def observe(self, context: SegmentBotContext) -> None:
        """Record each contract's basis for the session. Idempotent by instant."""
        if context.decision_instant in self._instants_observed:
            return
        self._instants_observed.add(context.decision_instant)
        SegmentBotFoundation.observe(self, context)

        spots = self._spot_prices(context)
        open_interest = self._open_interest(context)
        sessions_per_year = self._sessions_per_year(context.decision_instant.year)
        for contract in context.tradeable_universe:
            inputs = self._carry_inputs(contract, context, spots)
            if inputs is None:
                continue
            future_price, spot_price, sessions_to_expiry = inputs
            self._carry.observe(
                self._contract_key(contract),
                future_price=future_price,
                spot_price=spot_price,
                sessions_to_expiry=sessions_to_expiry,
                sessions_per_year=sessions_per_year,
                open_interest=open_interest.get(contract.instrument_token),
            )

    # ---- decision ---------------------------------------------------------------------

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        """Every contract whose carry dislocation clears the day's own cross-sectional cut."""
        spots = self._spot_prices(context)
        open_interest = self._open_interest(context)
        sessions_per_year = self._sessions_per_year(context.decision_instant.year)

        readings: dict[int, BasisCarryReading] = {}
        standardised: dict[int, float] = {}
        for contract in context.tradeable_universe:
            inputs = self._carry_inputs(contract, context, spots)
            if inputs is None:
                continue
            future_price, spot_price, sessions_to_expiry = inputs
            reading = self._carry.read(
                self._contract_key(contract),
                future_price=future_price,
                spot_price=spot_price,
                sessions_to_expiry=sessions_to_expiry,
                sessions_per_year=sessions_per_year,
                dislocation_cut=0.0,
                open_interest=open_interest.get(contract.instrument_token),
            )
            readings[contract.instrument_token] = reading
            if reading.standardised_carry is not None and math.isfinite(reading.standardised_carry):
                standardised[contract.instrument_token] = reading.standardised_carry
        self._last_readings = readings

        cut = self.cross_sectional_cut(standardised)
        self._last_cut = cut
        if cut is None:
            return ()

        signals: list[PricedSignal] = []
        for contract in context.tradeable_universe:
            selected = readings.get(contract.instrument_token)
            if selected is None or selected.standardised_carry is None:
                continue
            if abs(selected.standardised_carry) < cut:
                continue
            signal = self._signal_for(contract, context, selected)
            if signal is not None:
                signals.append(signal)
        return tuple(signals)

    def _signal_for(
        self,
        contract: TradeableInstrument,
        context: SegmentBotContext,
        reading: BasisCarryReading,
    ) -> PricedSignal | None:
        state = self.state_for(contract.instrument_token)
        if state is None or state.last_price_paise is None or state.last_price_paise <= 0:
            return None
        if reading.basis is None or reading.implied_carry is None:
            return None
        future_price = float(state.last_price_paise / PAISE_PER_RUPEE)
        if future_price <= 0.0:
            return None

        # The move this bot expects: the basis closing back to this contract's own normal carry.
        # In rupees that is the departure of the CARRY from its mean, un-annualised back over the
        # remaining life and multiplied by spot. Derived end to end — no constant anywhere.
        expected_move_rupees = self._expected_basis_close_rupees(reading, future_price)
        if expected_move_rupees is None or expected_move_rupees <= 0.0:
            return None
        expected_edge_bps = Decimal(
            str(round(expected_move_rupees / future_price * float(BASIS_POINTS_PER_UNIT), 4))
        )
        if expected_edge_bps <= 0:
            return None

        return self.build_signal(
            instrument=contract,
            context=context,
            # Rich carry is sold, cheap carry is bought. Not a view on the underlying's direction.
            side=(
                TradeLeg.SELL
                if reading.dislocation is BasisDislocation.EXPENSIVE
                else TradeLeg.BUY
            ),
            reference_price_paise=state.last_price_paise,
            expected_edge_bps=expected_edge_bps,
            conviction=self._conviction_from(reading),
            edge_basis=EdgeBasis.CALIBRATED_MODEL,
        )

    @staticmethod
    def _expected_basis_close_rupees(
        reading: BasisCarryReading, future_price: float
    ) -> float | None:
        """The rupee move implied by the carry returning to this contract's own mean.

        Recovered from the reading's own numbers rather than recomputed: `standardised x spread`
        is the carry departure, and multiplying it back by the remaining year fraction and the price
        converts an annual rate into the basis move it represents over the contract's actual life.
        """
        if (
            reading.standardised_carry is None
            or reading.implied_carry is None
            or reading.sessions_to_expiry <= 0
        ):
            return None
        # The engine's own arithmetic, inverted: implied_carry = (basis / spot) / year_fraction, so
        # a departure of `d` in carry corresponds to `d x year_fraction x price` rupees of basis.
        # The year fraction is recovered from the reading rather than recomputed for the same
        # reason the option bot recovers its spread — two derivations of one number drift.
        departure = abs(reading.implied_carry) if reading.standardised_carry != 0.0 else 0.0
        if departure <= 0.0 or not math.isfinite(departure):
            return None
        # Bounded by the basis actually observed: a model that claims a larger move than the basis
        # itself is claiming the future will cross its own spot, which the carry relation forbids.
        implied_move = departure * future_price
        if reading.basis is not None:
            implied_move = min(implied_move, abs(reading.basis))
        return implied_move if math.isfinite(implied_move) and implied_move > 0.0 else None

    @staticmethod
    def _conviction_from(reading: BasisCarryReading) -> Decimal:
        """Standardised carry, bounded by a ratio, then nudged by the open-interest taxonomy."""
        magnitude = abs(reading.standardised_carry or 0.0)
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            return Decimal("0.5")
        base = Decimal(str(round(magnitude / (1.0 + magnitude), 4)))
        agrees = (
            reading.dislocation is BasisDislocation.EXPENSIVE
            and reading.buildup is OpenInterestBuildup.SHORT_BUILDUP
        ) or (
            reading.dislocation is BasisDislocation.CHEAP
            and reading.buildup is OpenInterestBuildup.LONG_BUILDUP
        )
        disagrees = (
            reading.dislocation is BasisDislocation.EXPENSIVE
            and reading.buildup is OpenInterestBuildup.LONG_BUILDUP
        ) or (
            reading.dislocation is BasisDislocation.CHEAP
            and reading.buildup is OpenInterestBuildup.SHORT_BUILDUP
        )
        if agrees:
            base *= BUILDUP_CONVICTION_AGREEMENT
        elif disagrees:
            base *= BUILDUP_CONVICTION_DISAGREEMENT
        return min(max(base, Decimal("0")), Decimal("1"))

    # ---- relevance --------------------------------------------------------------------

    def _strategy_applicability(self, context: SegmentBotContext) -> float:  # noqa: ARG002
        """How much of the board is classifiable, and how dislocated the strongest of it is.

        Reads carried state rather than the universe, deliberately: a bot handed a thousand
        contracts it has no carry history for is not more applicable than one handed ten it knows.
        """
        tracked = max(self._carry.contracts_tracked, 1)
        classifiable = self._carry.contracts_with_a_carry_distribution() / tracked
        magnitudes = [
            abs(reading.standardised_carry)
            for reading in self._last_readings.values()
            if reading.standardised_carry is not None and math.isfinite(reading.standardised_carry)
        ]
        if not magnitudes:
            return max(0.0, min(classifiable, 1.0))
        strongest = max(magnitudes)
        return max(0.0, min(classifiable * (strongest / (1.0 + strongest)), 1.0))

    # ---- inputs -----------------------------------------------------------------------

    def _carry_inputs(
        self,
        contract: TradeableInstrument,
        context: SegmentBotContext,
        spots: dict[str, Decimal],
    ) -> tuple[float, float, int] | None:
        state = self.state_for(contract.instrument_token)
        if state is None or state.last_price_paise is None or contract.expiry is None:
            return None
        spot = spots.get(self._underlying_of(contract))
        if spot is None or spot <= 0:
            return None
        sessions = len(
            tuple(
                self._calendar.sessions_between(context.decision_instant.date(), contract.expiry)
            )
        )
        if sessions <= 0:
            return None
        return (
            float(state.last_price_paise / PAISE_PER_RUPEE),
            float(spot / PAISE_PER_RUPEE),
            sessions,
        )

    def _sessions_per_year(self, year: int) -> int:
        if year not in self._sessions_per_year_cache:
            count = len(
                tuple(self._calendar.sessions_between(date(year, 1, 1), date(year, 12, 31)))
            )
            self._sessions_per_year_cache[year] = max(count, 1)
        return self._sessions_per_year_cache[year]

    @staticmethod
    def _contract_key(contract: TradeableInstrument) -> str:
        """Stable per contract, not per underlying: two expiries carry differently by
        construction."""
        return f"{contract.trading_symbol.strip().upper()}:{contract.expiry}"

    @staticmethod
    def _underlying_of(contract: TradeableInstrument) -> str:
        symbol = contract.trading_symbol.strip().upper()
        underlying = ""
        for character in symbol:
            if character.isalpha():
                underlying += character
            else:
                break
        return underlying or symbol

    def _spot_prices(self, context: SegmentBotContext) -> dict[str, Decimal]:
        raw = context.carried_memory.get("underlying_price_paise_by_symbol")
        if not isinstance(raw, dict):
            return {}
        prices: dict[str, Decimal] = {}
        for symbol, value in raw.items():
            try:
                price = Decimal(str(value))
            except (ArithmeticError, ValueError):
                continue
            if price > 0:
                prices[str(symbol).strip().upper()] = price
        return prices

    @staticmethod
    def _open_interest(context: SegmentBotContext) -> dict[int, int]:
        raw = context.carried_memory.get("open_interest_by_token")
        if not isinstance(raw, dict):
            return {}
        interest: dict[int, int] = {}
        for token, value in raw.items():
            try:
                interest[int(token)] = int(value)
            except (TypeError, ValueError):
                continue
        return interest

    @property
    def last_cross_sectional_cut(self) -> float | None:
        return self._last_cut

    def last_reading_for(self, instrument_token: int) -> BasisCarryReading | None:
        return self._last_readings.get(instrument_token)

    @property
    def contracts_classifiable(self) -> int:
        return self._carry.contracts_with_a_carry_distribution()


class IndexFutureBasisCarryBot(FuturesBasisCarryBot):
    """Index futures. Its own null because index basis is a financing rate, not a borrow rate.

    `B31`: this project holds **540** `IDF` rows, so its maturity ladder will sit low for a long
    time. That is the ladder working, not the bot failing, and it is recorded so the two are never
    confused.
    """

    def __init__(self, **keywords: object) -> None:
        super().__init__(trading_segment=TradingSegment.INDEX_FUTURES, **keywords)  # type: ignore[arg-type]

    @property
    def bot_identity(self) -> str:
        return "index_futures_basis_carry_bot"


class StockFutureBasisCarryBot(FuturesBasisCarryBot):
    """Single-stock futures — the segment where basis carries real borrow and dividend information.

    The healthiest of the three on data: **22,561** `STF` rows over 36 sessions.
    """

    def __init__(self, **keywords: object) -> None:
        super().__init__(trading_segment=TradingSegment.STOCK_FUTURES, **keywords)  # type: ignore[arg-type]

    @property
    def bot_identity(self) -> str:
        return "stock_futures_basis_carry_bot"


class CommodityMcxBasisCarryBot(FuturesBasisCarryBot):
    """MCX commodity futures. **Built whole; activates on nothing until `B30` closes.**

    `R.04` in its purest form, and the reason this class is here rather than a TODO: the algorithm
    is
    identical to its two NSE siblings — same engine, same carry arithmetic, same cross-sectional
    cut,
    same conformance suite — and what it lacks is DATA. `fo_bhavcopy_contracts` holds zero MCX rows
    and the depth tape has never subscribed an MCX token, so its universe arrives empty, it proposes
    nothing, and its maturity stays at the bottom rung. The moment MCX ingestion lands it trades
    with
    no code change at all.

    Its venue calendar genuinely differs — MCX runs an evening session NSE does not — which
    `SegmentInstrumentFacts` already carries as a sourced regulatory fact and which the spine reads
    when it assembles this bot's universe.
    """

    def __init__(self, **keywords: object) -> None:
        super().__init__(trading_segment=TradingSegment.COMMODITY_MCX, **keywords)  # type: ignore[arg-type]

    @property
    def bot_identity(self) -> str:
        return "commodity_mcx_basis_carry_bot"
