"""The vocabulary every charge calculation is expressed in.

Kept in one module because the whole engine's correctness rests on three distinctions that
look like synonyms and are not:

- **Segment is not exchange.** `NSE` is an exchange segment; `NSE-CNC` and `NSE-MIS` are the
  same exchange segment under two product modes that are taxed four times apart. The rule
  store's scope strings use the second vocabulary (`A.90`).
- **Base is not turnover.** An option's premium turnover, its notional turnover and its
  intrinsic value are three different numbers, and the levies use all three. Applying a
  premium-based rate to notional is a ~100x error that produces a plausible-looking figure.
- **Leg is not "both, halved".** STT falls on the sell leg, stamp duty on the buy leg, and a
  round trip that averages them is right in total and wrong for every one-way question — such
  as "what does it cost to get out of this position right now".
"""

from __future__ import annotations

from enum import StrEnum

from nse_algo_trader.market_rules.point_in_time_market_rule_store import RuleFamily, RuleScope


class ChargeableSegment(StrEnum):
    """A traded product as the CHARGE schedule sees it.

    The value is the `RuleScope.segment` string the rule store is seeded with, so the mapping
    is the identity rather than a lookup table that can drift out of step with the facts.

    Index and stock derivatives are deliberately NOT separate members: their statutory rates
    are identical, and a distinction that exists only in code invites someone to give the two
    different numbers. Where the difference is real — physical settlement applies to stock
    derivatives and not index ones — it is expressed on the trade, not on the segment.
    """

    EQUITY_DELIVERY = "NSE-CNC"
    EQUITY_INTRADAY = "NSE-MIS"
    EQUITY_FUTURES = "NFO-FUT"
    EQUITY_OPTIONS = "NFO-OPT"
    CURRENCY_FUTURES = "CDS-FUT"
    CURRENCY_OPTIONS = "CDS-OPT"
    COMMODITY_FUTURES = "MCX-FUT"
    COMMODITY_OPTIONS = "MCX-OPT"

    @property
    def rule_scope(self) -> RuleScope:
        return RuleScope(segment=self.value)

    @property
    def is_option(self) -> bool:
        return self in _OPTION_SEGMENTS

    @property
    def is_commodity(self) -> bool:
        """Commodities pay CTT under a different statute, not STT under another name."""
        return self in _COMMODITY_SEGMENTS

    @property
    def is_equity_cash(self) -> bool:
        return self in _EQUITY_CASH_SEGMENTS

    @property
    def transaction_tax_component(self) -> ChargeComponent:
        if self.is_commodity:
            return ChargeComponent.COMMODITIES_TRANSACTION_TAX
        return ChargeComponent.SECURITIES_TRANSACTION_TAX


_OPTION_SEGMENTS = frozenset(
    {
        ChargeableSegment.EQUITY_OPTIONS,
        ChargeableSegment.CURRENCY_OPTIONS,
        ChargeableSegment.COMMODITY_OPTIONS,
    }
)
_COMMODITY_SEGMENTS = frozenset(
    {ChargeableSegment.COMMODITY_FUTURES, ChargeableSegment.COMMODITY_OPTIONS}
)
_EQUITY_CASH_SEGMENTS = frozenset(
    {ChargeableSegment.EQUITY_DELIVERY, ChargeableSegment.EQUITY_INTRADAY}
)

OPTION_EXERCISE_SCOPE = RuleScope(segment="NFO-OPT-EXERCISE")
"""Exercise is a different taxable EVENT, not a different rate on the same one.

An option sold carries STT on the seller at a rate on premium; an option exercised carries STT
on the PURCHASER at a different rate on intrinsic value. Reading the first where the second
applies is the error that makes a system irrationally afraid of holding to expiry.
"""


class ChargeComponent(StrEnum):
    """One line on the contract note. Each resolves its rate from its own rule family."""

    SECURITIES_TRANSACTION_TAX = "securities_transaction_tax"
    COMMODITIES_TRANSACTION_TAX = "commodities_transaction_tax"
    EXCHANGE_TRANSACTION_CHARGE = "exchange_transaction_charge"
    INVESTOR_PROTECTION_FUND_CONTRIBUTION = "investor_protection_fund_contribution"
    SEBI_TURNOVER_FEE = "sebi_turnover_fee"
    STAMP_DUTY = "stamp_duty"
    BROKERAGE = "brokerage"
    DEPOSITORY_PARTICIPANT_CHARGE = "depository_participant_charge"
    GOODS_AND_SERVICES_TAX = "goods_and_services_tax"

    @property
    def rule_family(self) -> RuleFamily | None:
        """The rule family carrying this component's rate, or `None` if it has no rate.

        Brokerage is the one component that is commercial rather than statutory: it comes from
        a broker schedule, not from the rule store, because it changes when the broker changes
        and carries no circular.
        """
        return _RULE_FAMILY_BY_COMPONENT.get(self)


_RULE_FAMILY_BY_COMPONENT: dict[ChargeComponent, RuleFamily] = {
    ChargeComponent.SECURITIES_TRANSACTION_TAX: RuleFamily.SECURITIES_TRANSACTION_TAX,
    ChargeComponent.COMMODITIES_TRANSACTION_TAX: RuleFamily.COMMODITIES_TRANSACTION_TAX,
    ChargeComponent.EXCHANGE_TRANSACTION_CHARGE: RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
    ChargeComponent.INVESTOR_PROTECTION_FUND_CONTRIBUTION: (
        RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION
    ),
    ChargeComponent.SEBI_TURNOVER_FEE: RuleFamily.SEBI_TURNOVER_FEE,
    ChargeComponent.STAMP_DUTY: RuleFamily.STAMP_DUTY,
    ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE: RuleFamily.DEPOSITORY_PARTICIPANT_CHARGE,
    ChargeComponent.GOODS_AND_SERVICES_TAX: RuleFamily.GOODS_AND_SERVICES_TAX,
}

GST_BEARING_COMPONENTS = frozenset(
    {
        ChargeComponent.BROKERAGE,
        ChargeComponent.EXCHANGE_TRANSACTION_CHARGE,
        ChargeComponent.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
        ChargeComponent.SEBI_TURNOVER_FEE,
        ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE,
    }
)
"""What GST is charged ON — the service lines only.

STT, CTT and stamp duty are absent and that is statutory, not an oversight: CGST Act s.2(52)
and s.2(102) exclude securities from both the goods and the services definitions, so there is
nothing for GST to attach to. Taxing the whole stack is the single most common error in
Indian cost models and it overstates every trade.
"""


class TaxableBase(StrEnum):
    """What a rate is multiplied BY. Never inferred from the segment — always resolved."""

    TRADE_TURNOVER = "trade_turnover"
    OPTION_PREMIUM_TURNOVER = "option_premium_turnover"
    OPTION_NOTIONAL_TURNOVER = "option_notional_turnover"
    OPTION_INTRINSIC_VALUE = "option_intrinsic_value"
    SETTLEMENT_VALUE = "settlement_value"
    TAXABLE_SERVICE_VALUE = "taxable_service_value"
    PER_ORDER = "per_order"
    PER_DEBIT_TRANSACTION = "per_debit_transaction"

    @property
    def scales_with_price(self) -> bool:
        """Whether this base moves when the trade's price moves.

        The breakeven solve depends on exactly this: bases that scale with the exit price
        enter the linear coefficient, bases that do not enter the constant. An option's
        NOTIONAL turnover is the interesting case — it is strike x quantity, so it does not
        move with the premium at all, and treating it as if it did puts the SEBI fee on the
        wrong side of the equation.
        """
        return self in _PRICE_SCALING_BASES


_PRICE_SCALING_BASES = frozenset(
    {
        TaxableBase.TRADE_TURNOVER,
        TaxableBase.OPTION_PREMIUM_TURNOVER,
    }
)


class TradeLeg(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LegApplicability(StrEnum):
    """Which leg a levy attaches to. A historical fact, so it is dated and cited."""

    BUY_ONLY = "buy_only"
    SELL_ONLY = "sell_only"
    BOTH_LEGS = "both_legs"

    def covers(self, leg: TradeLeg) -> bool:
        if self is LegApplicability.BOTH_LEGS:
            return True
        return (leg is TradeLeg.BUY) == (self is LegApplicability.BUY_ONLY)


class Depository(StrEnum):
    """Which depository holds the demat account. Only bites on delivery sells."""

    CDSL = "CDSL"
    CDSL_CONCESSIONAL = "CDSL-CONCESSIONAL"
    NSDL = "NSDL"

    @property
    def rule_scope(self) -> RuleScope:
        return RuleScope(segment=self.value)


class RoundingRule(StrEnum):
    """How a charge is rounded when billed — a BROKER property, not a statutory one.

    No SEBI or NSE circular mandating a rounding precision for statutory levies could be read
    (`research/219` §12.8, `O.70`), and the one primary clause recovered says levies are
    recovered "at actuals paid or payable" — a no-markup rule, not a precision rule. So the
    engine computes exactly and rounds nothing by default, and each broker schedule declares
    what it actually does. The reconciliation ledger then MEASURES it from contract notes
    instead of the engine asserting it.
    """

    EXACT = "exact"
    NEAREST_PAISA = "nearest_paisa"
    NEAREST_RUPEE = "nearest_rupee"
