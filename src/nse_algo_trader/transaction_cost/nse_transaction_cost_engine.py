"""What a trade costs, decomposed, priced at the date it happened.

The engine resolves every rate out of the `L0.31` point-in-time store at the trade's own date,
applies it to the base the structure history says it applies to, on the leg the structure
history says it falls on, and returns the lines rather than the total — because the total
alone cannot be checked against a contract note and the lines can.

Three things it deliberately does NOT do:

- **It does not round statutory charges.** No circular mandating a precision could be read
  (`O.70`), so rounding is a per-broker property and the default is exact to the paise.
- **It does not substitute.** A date whose rate is uncompiled raises rather than borrowing
  today's, because the borrowed answer is the one that looks right.
- **It does not average legs.** STT falls on the sell, stamp duty on the buy; a round-trip
  average is right in total and wrong for "what does it cost to get out of this right now".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import PAISE_PER_RUPEE
from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    MarketRuleError,
    PointInTimeMarketRuleStore,
    evidence_grade_rank,
)
from nse_algo_trader.transaction_cost.broker_fee_schedules import (
    DEFAULT_BROKER,
    BrokeragePiece,
    BrokerFeeSchedule,
    broker_fee_schedule,
)
from nse_algo_trader.transaction_cost.charge_structure_history import (
    ChargeStructureError,
    ChargeStructureHistory,
    ChargeStructureRecord,
    nse_charge_structure_history,
    option_exercise_structure,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    GST_BEARING_COMPONENTS,
    ChargeableSegment,
    ChargeComponent,
    Depository,
    RoundingRule,
    TaxableBase,
    TradeLeg,
    exercise_rule_scope,
)

_EXERCISE_BASES = frozenset({TaxableBase.OPTION_INTRINSIC_VALUE, TaxableBase.SETTLEMENT_VALUE})
"""The bases that mean "this is the exercise event", so the exercise scope is the right one."""

_PAISE_PER_RUPEE = Decimal(PAISE_PER_RUPEE)
"""Reused from `L0.34`'s reader rather than restated: one rupee is a hundred paise wherever
it is written, and two definitions of a unit are two chances to disagree about it."""

_BASIS_POINTS = Decimal(10_000)
"""Dimensionless, not money — a basis point is a ten-thousandth of anything."""


class TransactionCostError(Exception):
    """Base for every costing failure, so a caller can catch the family."""


class CostCoverageError(TransactionCostError):
    """The cost of this trade on this date is not knowable from compiled facts.

    Always raised in preference to producing a number. The failure modes it covers — an
    uncompiled rate era, a levy with no structural record, a broker with no schedule — all
    have the same property: a plausible answer is available by substitution, and it is wrong
    in a way nothing downstream can detect.
    """


class TradeSpecificationError(TransactionCostError):
    """The trade as described cannot be priced, and guessing the missing part would misprice it."""


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True, slots=True)
class TradeSpecification:
    """One round trip, described in the terms the charges actually key off.

    Prices are in PAISE and `Decimal`, never float: a rate multiplies every trade, and 0.0625
    has no exact binary representation.
    """

    segment: ChargeableSegment
    quantity: int
    entry_price_paise: Decimal
    exit_price_paise: Decimal
    trade_date: date
    strike_paise: Decimal | None = None
    option_right: OptionRight | None = None
    is_short_first: bool = False
    is_option_exercise: bool = False
    settlement_price_paise: Decimal | None = None
    is_physically_settled: bool = False
    orders_per_leg: int = 1
    depository: Depository | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise TradeSpecificationError(
                f"quantity must be positive, got {self.quantity} — a zero-quantity trade has "
                f"no turnover but still attracts per-order brokerage, so pricing it would "
                f"produce a cost for a trade that never happened"
            )
        if self.orders_per_leg <= 0:
            raise TradeSpecificationError(
                f"orders_per_leg must be positive, got {self.orders_per_leg}"
            )
        for label, value in (
            ("entry_price_paise", self.entry_price_paise),
            ("exit_price_paise", self.exit_price_paise),
        ):
            if value < 0:
                raise TradeSpecificationError(f"{label} is negative: {value}")
        if self.segment.is_option and self.strike_paise is None:
            raise TradeSpecificationError(
                f"{self.segment} needs a strike: the SEBI turnover fee is charged on NOTIONAL "
                f"turnover (strike x quantity), not on premium, so without a strike the fee "
                f"cannot be computed and defaulting it to the premium understates it ~100x"
            )
        if self.is_option_exercise:
            if not self.segment.is_option:
                raise TradeSpecificationError(f"{self.segment} cannot be exercised")
            if self.settlement_price_paise is None or self.option_right is None:
                raise TradeSpecificationError(
                    "an exercise needs both a settlement price and a call/put right — STT on "
                    "an exercised option is charged on intrinsic value, which is undefined "
                    "without knowing which side of the strike counts"
                )

    @property
    def entry_leg(self) -> TradeLeg:
        return TradeLeg.SELL if self.is_short_first else TradeLeg.BUY

    @property
    def exit_leg(self) -> TradeLeg:
        return TradeLeg.BUY if self.is_short_first else TradeLeg.SELL

    @property
    def intrinsic_value_paise(self) -> Decimal:
        """Per-unit intrinsic value at settlement. Zero when the option expires worthless."""
        if self.settlement_price_paise is None or self.strike_paise is None:
            raise TradeSpecificationError("intrinsic value needs a settlement price and a strike")
        if self.option_right is OptionRight.PUT:
            return max(Decimal(0), self.strike_paise - self.settlement_price_paise)
        return max(Decimal(0), self.settlement_price_paise - self.strike_paise)


@dataclass(frozen=True, slots=True)
class ChargeLine:
    """One levy on one leg, with what it was applied to and how well that is known."""

    component: ChargeComponent
    leg: TradeLeg
    taxable_base: TaxableBase
    taxable_amount_paise: Decimal
    rate: Decimal | None
    exact_paise: Decimal
    billed_paise: Decimal
    rounding: RoundingRule
    source_reference: str
    evidence_grade: EvidenceGrade

    @property
    def rounding_residual_paise(self) -> Decimal:
        """What rounding added or removed — the quantity the ledger reconciles against."""
        return self.billed_paise - self.exact_paise


@dataclass(frozen=True, slots=True)
class LegCost:
    """Everything one leg is charged, at one price."""

    leg: TradeLeg
    price_paise: Decimal
    lines: tuple[ChargeLine, ...]

    @property
    def total_paise(self) -> Decimal:
        return sum((line.billed_paise for line in self.lines), Decimal(0))

    @property
    def exact_total_paise(self) -> Decimal:
        return sum((line.exact_paise for line in self.lines), Decimal(0))


@dataclass(frozen=True, slots=True)
class RoundTripCost:
    """The answer, and enough of its provenance to judge how much to trust it."""

    trade: TradeSpecification
    entry: LegCost
    exit: LegCost
    known_as_of: date | None
    broker: str

    @property
    def lines(self) -> tuple[ChargeLine, ...]:
        return (*self.entry.lines, *self.exit.lines)

    @property
    def total_paise(self) -> Decimal:
        return self.entry.total_paise + self.exit.total_paise

    @property
    def total_rupees(self) -> Decimal:
        return self.total_paise / _PAISE_PER_RUPEE

    @property
    def exact_total_paise(self) -> Decimal:
        """The cost before any broker rounding — always positive for a real trade."""
        return self.entry.exact_total_paise + self.exit.exact_total_paise

    @property
    def bills_as_free(self) -> bool:
        """True when every line rounds away and the contract note reads zero.

        Real, and found on real data: DHARAN closed at 16 paise on 2026-06-30, so a
        hundred-share round trip turns over Rs 16 and every levy on it is a fraction of a
        paisa. The billed total is genuinely zero.

        It is exposed rather than smoothed because a cost of zero is a DANGEROUS input to a
        gate — an edge divided by zero cost clears any hurdle, so `L1.02` must branch on this
        and use `exact_total_paise` rather than dividing by the billed figure. A trade whose
        costs are invisible is not a cheap trade; it is a trade too small for costs to be the
        binding constraint, and the spread will be.
        """
        return self.total_paise == 0 and self.exact_total_paise > 0

    @property
    def exact_bps_of_turnover(self) -> Decimal:
        """Cost in bps before rounding — the figure a gate should divide by."""
        turnover = self.entry_turnover_paise
        if turnover == 0:
            raise TransactionCostError("cannot express cost in bps of a zero turnover")
        return self.exact_total_paise / turnover * _BASIS_POINTS

    @property
    def entry_turnover_paise(self) -> Decimal:
        return self.trade.entry_price_paise * self.trade.quantity

    @property
    def total_bps_of_turnover(self) -> Decimal:
        """Cost as basis points of the ENTRY turnover.

        One-sided rather than round-trip turnover, deliberately: the number a sizing decision
        needs is "what fraction of the capital I am about to commit does this cost", and
        dividing by both legs halves it into something that reads better and decides worse.
        """
        turnover = self.entry_turnover_paise
        if turnover == 0:
            raise TransactionCostError("cannot express cost in bps of a zero turnover")
        return self.total_paise / turnover * _BASIS_POINTS

    @property
    def weakest_evidence_grade(self) -> EvidenceGrade:
        """A cost is exactly as trustworthy as its worst-sourced line."""
        return min((line.evidence_grade for line in self.lines), key=evidence_grade_rank)

    def by_component(self) -> Mapping[ChargeComponent, Decimal]:
        totals: dict[ChargeComponent, Decimal] = {}
        for line in self.lines:
            totals[line.component] = totals.get(line.component, Decimal(0)) + line.billed_paise
        return totals


@dataclass(frozen=True, slots=True)
class LegCostPiece:
    """`slope * price + intercept` over `[price_from, price_to)`, in paise.

    The decomposition the breakeven solve needs. `slope` collects every levy whose base moves
    with the price; `intercept` collects the flat ones and the ones charged on a base that
    does not move — an option's notional turnover being the case that catches people out,
    since it is strike x quantity and does not respond to the premium at all.
    """

    price_from_paise: Decimal
    price_to_paise: Decimal | None
    slope: Decimal
    intercept_paise: Decimal

    def contains(self, price_paise: Decimal) -> bool:
        if price_paise < self.price_from_paise:
            return False
        return self.price_to_paise is None or price_paise < self.price_to_paise

    def cost_paise(self, price_paise: Decimal) -> Decimal:
        return self.slope * price_paise + self.intercept_paise


@dataclass(frozen=True, slots=True)
class LegCostFunction:
    """One leg's cost as an exact piecewise-linear function of its own price."""

    leg: TradeLeg
    pieces: tuple[LegCostPiece, ...]
    is_exact: bool
    rounding_note: str = ""

    def cost_paise(self, price_paise: Decimal) -> Decimal:
        for piece in self.pieces:
            if piece.contains(price_paise):
                return piece.cost_paise(price_paise)
        raise TransactionCostError(f"no cost piece covers price {price_paise} paise")


_ROUNDING_QUANTUM: dict[RoundingRule, Decimal | None] = {
    RoundingRule.EXACT: None,
    RoundingRule.NEAREST_PAISA: Decimal(1),
    RoundingRule.NEAREST_RUPEE: _PAISE_PER_RUPEE,
}

_STATUTORY_ROUNDED_COMPONENTS = frozenset(
    {
        ChargeComponent.SECURITIES_TRANSACTION_TAX,
        ChargeComponent.COMMODITIES_TRANSACTION_TAX,
    }
)
"""Which lines follow the broker's coarse statutory rounding rule — the transaction taxes only.

The only rounding evidence anywhere is a broker's published practice of rounding STT to the
nearest rupee. Extending it to stamp duty looked harmless and is not: a 0.003% duty on a
Rs 9,750 option premium is 29 paise, and rounding that to the nearest rupee DELETES it. A
charge that vanishes is worse than one that is slightly wrong, because nothing downstream can
see that it was ever there. Everything outside this set follows the finer rule rather than
inheriting a claim that was never made about it.
"""


def _round_to(amount: Decimal, rule: RoundingRule) -> Decimal:
    """Round HALF UP, because that is the rule the evidence actually states.

    `Decimal.quantize` defaults to the context's `ROUND_HALF_EVEN` (banker's rounding), and
    inheriting that default silently contradicted the only rounding fact this project has:
    the broker's published practice is "nearest rupee, **50 paise and above rounds up**".
    Under banker's rounding a Rs 2.50 STT bills as Rs 2.00, and half-rupee STT is not exotic —
    intraday at 0.025% lands exactly on it at every odd multiple of Rs 2,000 of turnover.
    """
    quantum = _ROUNDING_QUANTUM[rule]
    if quantum is None:
        return amount
    return (amount / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum


class NseTransactionCostEngine:
    """Prices a trade against the facts in force on its own date."""

    def __init__(
        self,
        rule_store: PointInTimeMarketRuleStore,
        *,
        structure_history: ChargeStructureHistory | None = None,
        broker: str = DEFAULT_BROKER,
        schedule: BrokerFeeSchedule | None = None,
    ) -> None:
        self._rules = rule_store
        self._structures = structure_history or nse_charge_structure_history()
        self._schedule = schedule
        self._broker = schedule.broker if schedule is not None else broker

    @property
    def broker(self) -> str:
        return self._broker

    def schedule_for(self, trade_date: date) -> BrokerFeeSchedule:
        """The broker schedule in force, or an injected one.

        The injection seam exists for two real needs, not for convenience: a test that wants a
        broker who rounds NOTHING, so the round-trip identity can be asserted exactly rather
        than to a tolerance; and pricing against a schedule that has not been published to the
        data file yet. When one is injected its own effective window still governs.
        """
        if self._schedule is not None:
            if not self._schedule.covers_date(trade_date):
                raise CostCoverageError(
                    f"the injected {self._schedule.broker} schedule does not cover {trade_date}"
                )
            return self._schedule
        try:
            return broker_fee_schedule(self._broker, trade_date)
        except Exception as error:
            raise CostCoverageError(str(error)) from error

    # ---------------------------------------------------------------- pricing

    def price_round_trip(
        self, trade: TradeSpecification, *, known_as_of: date | None = None
    ) -> RoundTripCost:
        """Both legs, at the prices the trade actually specifies."""
        entry = self.price_leg(
            trade, trade.entry_leg, trade.entry_price_paise, known_as_of=known_as_of
        )
        exit_leg = (
            self._price_exercise_settlement(trade, known_as_of=known_as_of)
            if trade.is_option_exercise
            else self.price_leg(
                trade, trade.exit_leg, trade.exit_price_paise, known_as_of=known_as_of
            )
        )
        return RoundTripCost(
            trade=trade,
            entry=entry,
            exit=exit_leg,
            known_as_of=known_as_of,
            broker=self._broker,
        )

    def price_leg(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        price_paise: Decimal,
        *,
        known_as_of: date | None = None,
    ) -> LegCost:
        """One leg, priced at `price_paise`.

        Taken as an argument rather than read off the trade so the breakeven solver can price
        a candidate exit without fabricating a whole `TradeSpecification` for each guess.
        """
        schedule = self.schedule_for(trade.trade_date)
        structures = self._applicable_structures(trade, leg)
        lines: list[ChargeLine] = []
        for structure in structures:
            if structure.component is ChargeComponent.GOODS_AND_SERVICES_TAX:
                continue
            lines.append(self._line_for(trade, leg, price_paise, structure, schedule, known_as_of))
        gst_structure = self._gst_structure(trade, leg)
        if gst_structure is not None:
            lines.append(self._gst_line(trade, leg, lines, gst_structure, schedule, known_as_of))
        return LegCost(leg=leg, price_paise=price_paise, lines=tuple(lines))

    def _price_exercise_settlement(
        self, trade: TradeSpecification, *, known_as_of: date | None
    ) -> LegCost:
        """What an exercise costs — which is NOT a second trade.

        An exercised option is settled by the exchange. No order is sent, so there is no
        brokerage, no exchange transaction charge, no SEBI turnover fee and no stamp duty on
        the way out; pricing a full exit leg charges the SEBI notional fee twice and invents a
        brokerage for an order that never existed.

        Two parties, and only one of them pays. The HOLDER exercising is the purchaser and
        owes the exercise tax on intrinsic value. The WRITER being assigned owes nothing at
        settlement — they already paid the sell-side tax on the premium when they wrote it,
        which the entry leg has priced. Filtering that entry-leg tax away and then charging
        the writer the purchaser's tax, as an earlier version did, inverts both halves.
        """
        if trade.is_short_first:
            return LegCost(leg=trade.exit_leg, price_paise=Decimal(0), lines=())
        try:
            structure = option_exercise_structure(
                trade.segment,
                trade.trade_date,
                is_physically_settled=trade.is_physically_settled,
            )
        except ChargeStructureError as error:
            raise CostCoverageError(str(error)) from error
        schedule = self.schedule_for(trade.trade_date)
        line = self._line_for(
            trade, trade.exit_leg, Decimal(0), structure, schedule, known_as_of
        )
        return LegCost(leg=trade.exit_leg, price_paise=Decimal(0), lines=(line,))

    def _applicable_structures(
        self, trade: TradeSpecification, leg: TradeLeg
    ) -> tuple[ChargeStructureRecord, ...]:
        try:
            everything = self._structures.components_for(trade.segment, trade.trade_date)
        except ChargeStructureError as error:
            raise CostCoverageError(str(error)) from error
        applicable = [record for record in everything if record.leg.covers(leg)]
        if trade.segment is ChargeableSegment.EQUITY_DELIVERY and trade.depository is None:
            applicable = [
                record
                for record in applicable
                if record.component is not ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE
            ]
        return tuple(applicable)

    def _gst_structure(
        self, trade: TradeSpecification, leg: TradeLeg
    ) -> ChargeStructureRecord | None:
        try:
            record = self._structures.resolve(
                ChargeComponent.GOODS_AND_SERVICES_TAX, trade.segment, trade.trade_date
            )
        except ChargeStructureError:
            return None
        return record if record.leg.covers(leg) else None

    def _line_for(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        price_paise: Decimal,
        structure: ChargeStructureRecord,
        schedule: BrokerFeeSchedule,
        known_as_of: date | None,
    ) -> ChargeLine:
        component = structure.component
        if component is ChargeComponent.BROKERAGE:
            return self._brokerage_line(trade, leg, price_paise, schedule)
        if component is ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE:
            return self._depository_line(trade, leg, schedule, known_as_of)
        rate, source, grade = self._rate_for(trade, structure, known_as_of)
        base_amount = self._taxable_amount(trade, price_paise, structure.taxable_base)
        exact = rate * base_amount
        rule = (
            schedule.statutory_rounding
            if component in _STATUTORY_ROUNDED_COMPONENTS
            else schedule.brokerage_rounding
        )
        return ChargeLine(
            component=component,
            leg=leg,
            taxable_base=structure.taxable_base,
            taxable_amount_paise=base_amount,
            rate=rate,
            exact_paise=exact,
            billed_paise=_round_to(exact, rule),
            rounding=rule,
            source_reference=source,
            evidence_grade=grade,
        )

    def _rate_for(
        self,
        trade: TradeSpecification,
        structure: ChargeStructureRecord,
        known_as_of: date | None,
    ) -> tuple[Decimal, str, EvidenceGrade]:
        family = structure.component.rule_family
        if family is None:
            raise TransactionCostError(f"{structure.component} carries no rule family")
        scope = trade.segment.rule_scope
        if trade.is_option_exercise and structure.taxable_base in _EXERCISE_BASES:
            exercise_scope = exercise_rule_scope(
                trade.segment, is_physically_settled=trade.is_physically_settled
            )
            if exercise_scope is None:
                raise CostCoverageError(
                    f"{trade.segment} has no exercise tax scope — this segment does not "
                    f"attract a transaction tax on exercise, and borrowing another segment's "
                    f"would apply the wrong statute at the wrong rate"
                )
            scope = exercise_scope
        try:
            resolution = self._rules.resolve(
                family, trade.trade_date, scope=scope, known_as_of=known_as_of
            )
        except MarketRuleError as error:
            raise CostCoverageError(
                f"{structure.component} on {trade.segment} at {trade.trade_date}: {error}"
            ) from error
        return resolution.as_decimal(), resolution.record.source_reference, resolution.grade

    def _taxable_amount(
        self, trade: TradeSpecification, price_paise: Decimal, base: TaxableBase
    ) -> Decimal:
        quantity = Decimal(trade.quantity)
        match base:
            case TaxableBase.TRADE_TURNOVER | TaxableBase.OPTION_PREMIUM_TURNOVER:
                return price_paise * quantity
            case TaxableBase.OPTION_NOTIONAL_TURNOVER:
                if trade.strike_paise is None:
                    raise TradeSpecificationError("notional turnover needs a strike")
                return trade.strike_paise * quantity
            case TaxableBase.OPTION_INTRINSIC_VALUE:
                return trade.intrinsic_value_paise * quantity
            case TaxableBase.SETTLEMENT_VALUE:
                if trade.settlement_price_paise is None:
                    raise TradeSpecificationError("settlement value needs a settlement price")
                return trade.settlement_price_paise * quantity
            case _:
                raise TransactionCostError(f"{base} is not an ad-valorem base")

    def _brokerage_line(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        price_paise: Decimal,
        schedule: BrokerFeeSchedule,
    ) -> ChargeLine:
        """Brokerage is per ORDER, so a leg split across orders pays it more than once."""
        turnover_per_order = price_paise * trade.quantity / trade.orders_per_leg
        per_order = schedule.brokerage_paise(trade.segment, turnover_per_order)
        exact = per_order * trade.orders_per_leg
        return ChargeLine(
            component=ChargeComponent.BROKERAGE,
            leg=leg,
            taxable_base=TaxableBase.PER_ORDER,
            taxable_amount_paise=price_paise * trade.quantity,
            rate=None,
            exact_paise=exact,
            billed_paise=_round_to(exact, schedule.brokerage_rounding),
            rounding=schedule.brokerage_rounding,
            source_reference=f"{schedule.broker}: {schedule.source_reference}",
            evidence_grade=schedule.grade,
        )

    def _depository_line(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        schedule: BrokerFeeSchedule,
        known_as_of: date | None,
    ) -> ChargeLine:
        """Flat per debit, plus the broker's markup — independent of quantity entirely."""
        depository = trade.depository or schedule.depository
        family = ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE.rule_family
        if family is None:  # pragma: no cover - the mapping declares this family
            raise TransactionCostError("the depository charge has no rule family")
        try:
            resolution = self._rules.resolve(
                family,
                trade.trade_date,
                scope=depository.rule_scope,
                known_as_of=known_as_of,
            )
        except MarketRuleError as error:
            raise CostCoverageError(
                f"{depository} charge at {trade.trade_date}: {error}"
            ) from error
        exact = Decimal(resolution.as_integer()) + schedule.depository_markup_paise
        return ChargeLine(
            component=ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE,
            leg=leg,
            taxable_base=TaxableBase.PER_DEBIT_TRANSACTION,
            taxable_amount_paise=Decimal(1),
            rate=None,
            exact_paise=exact,
            billed_paise=_round_to(exact, schedule.brokerage_rounding),
            rounding=schedule.brokerage_rounding,
            source_reference=(
                f"{resolution.record.source_reference}; broker markup per "
                f"{schedule.broker}: {schedule.source_reference}"
            ),
            evidence_grade=min(
                (resolution.grade, schedule.grade), key=evidence_grade_rank
            ),
        )

    def _gst_line(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        lines: Sequence[ChargeLine],
        structure: ChargeStructureRecord,
        schedule: BrokerFeeSchedule,
        known_as_of: date | None,
    ) -> ChargeLine:
        """18% of the SERVICE lines only — never of STT, CTT or stamp duty.

        Computed on the EXACT amounts rather than the billed ones: GST is charged on what the
        service costs, and compounding one rounding into another is a second error rather than
        a correction of the first.
        """
        rate, source, grade = self._rate_for(trade, structure, known_as_of)
        taxable = sum(
            (line.exact_paise for line in lines if line.component in GST_BEARING_COMPONENTS),
            Decimal(0),
        )
        exact = rate * taxable
        weakest = min(
            (
                grade,
                *(
                    line.evidence_grade
                    for line in lines
                    if line.component in GST_BEARING_COMPONENTS
                ),
            ),
            key=evidence_grade_rank,
        )
        return ChargeLine(
            component=ChargeComponent.GOODS_AND_SERVICES_TAX,
            leg=leg,
            taxable_base=TaxableBase.TAXABLE_SERVICE_VALUE,
            taxable_amount_paise=taxable,
            rate=rate,
            exact_paise=exact,
            billed_paise=_round_to(exact, schedule.brokerage_rounding),
            rounding=schedule.brokerage_rounding,
            source_reference=source,
            evidence_grade=weakest,
        )

    # ------------------------------------------------- the linear decomposition

    def leg_cost_function(
        self,
        trade: TradeSpecification,
        leg: TradeLeg,
        *,
        known_as_of: date | None = None,
    ) -> LegCostFunction:
        """This leg's cost as an exact piecewise-linear function of its own price.

        Exact only where the broker rounds nothing; `is_exact` says which, and the solver
        reports the residual rather than pretending otherwise.
        """
        schedule = self.schedule_for(trade.trade_date)
        structures = self._applicable_structures(trade, leg)
        gst_rate = self._gst_rate_or_zero(trade, leg, known_as_of)
        quantity = Decimal(trade.quantity)

        ad_valorem_slope = Decimal(0)
        constant = Decimal(0)
        for structure in structures:
            component = structure.component
            if component is ChargeComponent.GOODS_AND_SERVICES_TAX:
                continue
            bears_gst = component in GST_BEARING_COMPONENTS
            multiplier = Decimal(1) + (gst_rate if bears_gst else Decimal(0))
            if component is ChargeComponent.BROKERAGE:
                continue
            if component is ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE:
                line = self._depository_line(trade, leg, schedule, known_as_of)
                constant += line.exact_paise * multiplier
                continue
            rate, _, _ = self._rate_for(trade, structure, known_as_of)
            if structure.taxable_base.scales_with_price:
                ad_valorem_slope += rate * quantity * multiplier
            else:
                fixed_base = self._taxable_amount(trade, Decimal(0), structure.taxable_base)
                constant += rate * fixed_base * multiplier

        brokerage_multiplier = Decimal(1) + gst_rate
        pieces = self._brokerage_pieces_in_price(trade, schedule)
        cost_pieces = tuple(
            LegCostPiece(
                price_from_paise=price_from,
                price_to_paise=price_to,
                slope=ad_valorem_slope + piece.rate * quantity * brokerage_multiplier,
                intercept_paise=constant
                + piece.flat_paise * Decimal(trade.orders_per_leg) * brokerage_multiplier,
            )
            for piece, price_from, price_to in pieces
        )
        rounds_nothing = (
            schedule.statutory_rounding is RoundingRule.EXACT
            and schedule.brokerage_rounding is RoundingRule.EXACT
        )
        return LegCostFunction(
            leg=leg,
            pieces=cost_pieces,
            is_exact=rounds_nothing,
            rounding_note=(
                ""
                if rounds_nothing
                else (
                    f"{schedule.broker} rounds statutory lines to "
                    f"{schedule.statutory_rounding.value} and others to "
                    f"{schedule.brokerage_rounding.value}, so the linear form is the "
                    f"pre-rounding cost and the solved price carries a residual"
                )
            ),
        )

    def _gst_rate_or_zero(
        self, trade: TradeSpecification, leg: TradeLeg, known_as_of: date | None
    ) -> Decimal:
        structure = self._gst_structure(trade, leg)
        if structure is None:
            return Decimal(0)
        rate, _, _ = self._rate_for(trade, structure, known_as_of)
        return rate

    def _brokerage_pieces_in_price(
        self, trade: TradeSpecification, schedule: BrokerFeeSchedule
    ) -> tuple[tuple[BrokeragePiece, Decimal, Decimal | None], ...]:
        """Re-express the brokerage pieces as price intervals for THIS quantity.

        The schedule's boundaries are turnovers; the solve happens in price. Dividing by
        quantity per order is the whole conversion, and it is done once here so no caller can
        forget that a leg split into several orders crosses the cap at a higher price.
        """
        orders = Decimal(trade.orders_per_leg)
        divisor = Decimal(trade.quantity) / orders
        converted: list[tuple[BrokeragePiece, Decimal, Decimal | None]] = []
        for piece in schedule.pieces_for(trade.segment):
            price_from = piece.turnover_from_paise / divisor
            upper = piece.turnover_to_paise
            price_to = None if upper is None else upper / divisor
            converted.append((piece, price_from, price_to))
        return tuple(converted)


def default_transaction_cost_engine(
    *, broker: str = DEFAULT_BROKER, observe_instrument_master: bool = False
) -> NseTransactionCostEngine:
    """The engine wired to the seeded rule store — the form every consumer should use."""
    from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store

    return NseTransactionCostEngine(
        seeded_nse_market_rule_store(observe_instrument_master=observe_instrument_master),
        broker=broker,
    )
