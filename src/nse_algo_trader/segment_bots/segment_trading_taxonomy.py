"""The six trading segments and the instrument facts that are irreducibly different — `L5.29`.

`A.01` fixes six segment holons: cash-intraday, index-options, stock-options, index-futures,
stock-futures and commodities/MCX, *"all built from the start, each with an independent on/off
switch"*. `A.130` builds all six in parallel on one shared spine, and `R.10` makes them equal by
default.

**Why this is a second axis rather than a reuse of `ChargeableSegment`.** `ChargeableSegment`
already exists, has eight members, and answers *"what does this cost?"*. Its docstring is explicit
that index and stock derivatives are **deliberately not** separate members, because their statutory
rates are identical and *"a distinction that exists only in code invites someone to give the two
different numbers"*. `TradingSegment` answers a different question — *"who decides this?"* — and
splits index from stock exactly where the charge axis refuses to, because cash-settled and
physically-settled instruments are different **risk** shapes even when they are the same **charge**
shape.

Collapsing the two axes into one causes one of exactly two errors, and the tests pin both:

* one enum for both questions, split by index/stock → index and stock derivatives eventually get
  different tax rates, the error `ChargeableSegment` was written to prevent;
* one enum for both questions, not split → index and stock derivatives get the same settlement
  obligation, the error `A.01`'s split exists to prevent. SEBI has mandated physical delivery for
  stock F&O since 2018; index F&O is cash-settled and always has been.

So: two axes, one **total** mapping between them, and no third opinion anywhere in the codebase.

Every value in `SEGMENT_INSTRUMENT_FACTS` is a regulatory or physical fact about the instrument,
which is the single exemption `R.23e` allows for constants — and each record therefore names its
source. Nothing here is a threshold, a preference, or anything a bot could learn: those belong to
the bots.

Deliberately NOT an engine (`R.23b`): this is a typed fact table plus a lookup. No solver, no
carried state, no output that changes an allocation on its own. It is named for what it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment

MINIMUM_REGULATORY_SOURCE_LENGTH = 20
"""Shortest string that can name a statute, a circular, or an exchange rule and be checkable.

Not a style rule: `R.23e` exempts a constant from the no-hardcoding rule only when it is a sourced
regulatory or physical fact, so an unsourced value in this table is a defect. Twenty characters is
roughly "SEBI CIR/DNPD/6/2010" — long enough that a bare "NSE" cannot pass.
"""


class SegmentTaxonomyError(Exception):
    """A segment was named that the taxonomy does not define."""


class TradingSegment(StrEnum):
    """The six autonomous segment holons of `A.01` — the "who decides this?" axis.

    A seventh member would be a plan change (a new `A.` entry), never a code change.
    """

    CASH_INTRADAY = "cash_intraday"
    INDEX_OPTIONS = "index_options"
    STOCK_OPTIONS = "stock_options"
    INDEX_FUTURES = "index_futures"
    STOCK_FUTURES = "stock_futures"
    COMMODITY_MCX = "commodity_mcx"


class TradeableUnitDenominator(StrEnum):
    """What a percentage move is measured AGAINST — `L11.106`.

    `D.01` records conflating these as a real, made error: a 5-point move on a ₹100 option premium
    is 5% against a ~1% cost floor, while the same 5 points on a ₹2,400 cash price is 0.2% against a
    ~0.08% floor. The two are not comparable, and a cost gate that treats them as one denominator
    either passes trades that cannot pay for themselves or refuses trades that can.
    """

    PRICE = "price"
    PREMIUM = "premium"
    CONTRACT_NOTIONAL = "contract_notional"


class SettlementStyle(StrEnum):
    """How an unclosed position at expiry resolves.

    The spine's square-off path assumes cash settlement. `PHYSICAL_DELIVERY` is a genuinely
    different obligation — shares must be delivered or received, margins escalate through the
    delivery window, and an unclosed long option can be assigned — which is why it is a segment fact
    and not a detail.
    """

    CASH_SETTLED = "cash_settled"
    PHYSICAL_DELIVERY = "physical_delivery"


class VenueCalendar(StrEnum):
    """Which exchange calendar governs the session.

    `A.01`: *"MCX remains a second VENUE, not merely a sixth segment."* Its session runs into the
    evening, long after NSE has closed, so a bot that assumed the NSE calendar would stop deciding
    while its market was still trading.
    """

    NSE = "nse"
    MCX = "mcx"


@dataclass(frozen=True, slots=True)
class SegmentInstrumentFacts:
    """What is irreducibly different about one segment's instruments.

    Everything here is a regulatory or physical fact, and `regulatory_source` names where it comes
    from. Anything a bot could instead LEARN — thresholds, edges, sizes, relevance — is deliberately
    absent: those belong to the bot, which is an engine, while this is a table.
    """

    chargeable_segment: ChargeableSegment
    """The charge scope for an INTRADAY, squared-off position in this segment.

    Read it through :meth:`chargeable_segment_when` rather than directly wherever carry is possible:
    a position that is genuinely carried overnight in cash equity is a DELIVERY trade and is taxed
    as one, whatever product tag the order carried.
    """

    carried_chargeable_segment: ChargeableSegment | None
    """The charge scope once a position is carried overnight, when that is permitted at all.

    Exists because the flat mapping was wrong and measurably so (adversarial review 2026-08-17,
    finding C3). STT follows SETTLEMENT type, not the MIS/CNC order tag: this project's own rule
    store seeds 0.025% sell-only for `NSE-MIS` against 0.1% on BOTH legs for `NSE-CNC`
    (`nse_market_rule_history.py`), and pricing a carried cash position as MIS understated its
    round-trip cost by roughly 4x — ₹132.36 against ₹533.96 on ₹2,40,000 of notional, computed
    through this project's own cost engine. `R.01` itself says cash is promotable "net of the
    DELIVERY cost structure", so the flat mapping contradicted the rule it claimed to encode.

    `None` wherever carry is not permitted, which keeps the two facts from drifting apart.
    """

    denominator: TradeableUnitDenominator
    settlement: SettlementStyle
    venue_calendar: VenueCalendar
    has_expiry: bool
    strike_required: bool
    requires_greeks: bool
    overnight_carry_permitted: bool
    regulatory_source: str

    def chargeable_segment_when(self, *, carried_overnight: bool) -> ChargeableSegment:
        """The charge scope that actually applies, given whether the position is carried.

        Raises when asked about a carried position in a segment that may not carry, rather than
        quietly returning the intraday scope — a caller asking the question at all has a carry in
        mind, and answering it with the wrong number is worse than refusing.
        """
        if not carried_overnight:
            return self.chargeable_segment
        if self.carried_chargeable_segment is None:
            raise SegmentTaxonomyError(
                f"this segment may not carry overnight, so there is no carried charge scope to "
                f"give; permitted={self.overnight_carry_permitted}"
            )
        return self.carried_chargeable_segment

    def __post_init__(self) -> None:
        if self.overnight_carry_permitted != (self.carried_chargeable_segment is not None):
            raise SegmentTaxonomyError(
                "a segment that may carry overnight must name the charge scope its carried "
                "positions are taxed under, and a segment that may not must name none — the two "
                "facts drifting apart is how a carried position gets priced as an intraday one"
            )
        if self.strike_required != (self.denominator is TradeableUnitDenominator.PREMIUM):
            raise SegmentTaxonomyError(
                "a strike is required exactly when the denominator is a premium — the SEBI "
                "turnover "
                "fee is charged on notional, so an option signal without a strike cannot be "
                "priced, "
                "and a non-option with a strike is describing an instrument that does not exist"
            )
        if self.strike_required and self.overnight_carry_permitted:
            raise SegmentTaxonomyError(
                "R.01 admits no exception: options, index AND stock, never carry overnight. A fact "
                "record that permits it is a rule violation encoded as data"
            )
        if self.strike_required and not self.has_expiry:
            raise SegmentTaxonomyError("an option without an expiry is not an option")
        if len(self.regulatory_source) <= MINIMUM_REGULATORY_SOURCE_LENGTH:
            raise SegmentTaxonomyError(
                "R.23e exempts regulatory facts from the no-constants rule only when they are "
                "sourced; an unsourced fact is an undocumented magic value wearing a dataclass"
            )


SEGMENT_INSTRUMENT_FACTS: Mapping[TradingSegment, SegmentInstrumentFacts] = MappingProxyType(
    {
        TradingSegment.CASH_INTRADAY: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_INTRADAY,
            # A carried cash position is a DELIVERY trade and is taxed as one — see the field
            # docstring for the measured 4x understatement the flat mapping produced.
            carried_chargeable_segment=ChargeableSegment.EQUITY_DELIVERY,
            denominator=TradeableUnitDenominator.PRICE,
            settlement=SettlementStyle.CASH_SETTLED,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=False,
            strike_required=False,
            requires_greeks=False,
            # The ONLY segment R.01 permits to carry, and then only on explicit supervisor
            # promotion — the fact records permission, never the promotion itself.
            overnight_carry_permitted=True,
            regulatory_source=(
                "NSE equity cash segment. Intraday and squared off, the charge scope is NSE-MIS; "
                "CARRIED overnight it becomes a delivery trade and is charged as NSE-CNC, because "
                "STT follows the settlement type rather than the MIS/CNC order tag (0.025% "
                "sell-side "
                "for intraday against 0.1% on both legs for delivery). R.01 permits overnight "
                "carry "
                "for CASH ONLY, by explicit supervisor promotion, and states the promotion is "
                "measured net of the DELIVERY cost structure."
            ),
        ),
        TradingSegment.INDEX_OPTIONS: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_OPTIONS,
            denominator=TradeableUnitDenominator.PREMIUM,
            settlement=SettlementStyle.CASH_SETTLED,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=True,
            strike_required=True,
            requires_greeks=True,
            carried_chargeable_segment=None,
            overnight_carry_permitted=False,
            regulatory_source=(
                "NSE F&O (NFO) index options, per NSE F&O contract specifications: European "
                "exercise and cash-settled against the index settlement value since the Nifty "
                "options launch on 2001-06-04. This record previously cited SEBI CIR/DNPD/6/2010, "
                "which is titled 'European Style Stock Options' and governs STOCK options only — "
                "nine years AFTER index options were already European and cash-settled. Wrong "
                "citation found by adversarial review 2026-08-17 (M7). "
                "R.01: options never carry overnight."
            ),
        ),
        TradingSegment.STOCK_OPTIONS: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_OPTIONS,
            denominator=TradeableUnitDenominator.PREMIUM,
            # The difference from index options that justifies a separate segment existing.
            settlement=SettlementStyle.PHYSICAL_DELIVERY,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=True,
            strike_required=True,
            requires_greeks=True,
            carried_chargeable_segment=None,
            overnight_carry_permitted=False,
            regulatory_source=(
                "NSE F&O (NFO) stock options: European exercise from expiries on 2011-01-27, "
                "which SEBI CIR/DNPD/6/2010 (2010-10-27) PERMITTED rather than mandated — NSE "
                "chose "
                "the date. PHYSICALLY SETTLED: SEBI mandated physical delivery for stock "
                "derivatives, phased in from the Apr 2018 circular and universal from the Oct 2019 "
                "expiry. R.01: options never carry overnight."
            ),
        ),
        TradingSegment.INDEX_FUTURES: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_FUTURES,
            denominator=TradeableUnitDenominator.CONTRACT_NOTIONAL,
            settlement=SettlementStyle.CASH_SETTLED,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=True,
            strike_required=False,
            requires_greeks=False,
            # R.01 says only CASH EQUITY is promotable — an earlier comment here claimed futures
            # were promotable too, which contradicted the rule it cited (review finding m17).
            carried_chargeable_segment=None,
            overnight_carry_permitted=False,
            regulatory_source=(
                "NSE F&O (NFO) index futures: cash-settled at expiry against the index settlement "
                "value. Carry is NOT permitted: R.01 restricts overnight carry to cash equity, so "
                "this is the rule rather than a pending build."
            ),
        ),
        TradingSegment.STOCK_FUTURES: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.EQUITY_FUTURES,
            denominator=TradeableUnitDenominator.CONTRACT_NOTIONAL,
            settlement=SettlementStyle.PHYSICAL_DELIVERY,
            venue_calendar=VenueCalendar.NSE,
            has_expiry=True,
            strike_required=False,
            requires_greeks=False,
            carried_chargeable_segment=None,
            overnight_carry_permitted=False,
            regulatory_source=(
                "NSE F&O (NFO) stock futures: PHYSICALLY SETTLED — SEBI mandated physical delivery "
                "for all stock derivatives, phased in from 2018 and universal since Oct 2019, so "
                "an "
                "unclosed position at expiry becomes a delivery obligation with escalating margins."
            ),
        ),
        TradingSegment.COMMODITY_MCX: SegmentInstrumentFacts(
            chargeable_segment=ChargeableSegment.COMMODITY_FUTURES,
            denominator=TradeableUnitDenominator.CONTRACT_NOTIONAL,
            settlement=SettlementStyle.PHYSICAL_DELIVERY,
            venue_calendar=VenueCalendar.MCX,
            has_expiry=True,
            strike_required=False,
            requires_greeks=False,
            carried_chargeable_segment=None,
            overnight_carry_permitted=False,
            regulatory_source=(
                "MCX commodity futures. Settlement is NOT uniform, and this single value is a "
                "known simplification tracked as B8 (adversarial review 2026-08-17, M8): most "
                "metals and agri contracts are compulsory- or seller-option delivery against "
                "warehouse receipts, while CRUDE OIL, NATURAL GAS and the MCX index futures "
                "(BULLDEX, METLDEX) are CASH-SETTLED. The conservative value is kept — assuming "
                "delivery where there is none over-reserves, while assuming cash where there is "
                "delivery leaves an unhedged physical obligation. CTT (0.01%, sell side, since "
                "2013-07-01) applies to NON-AGRICULTURAL commodities only; agri futures are "
                "exempt. "
                "A.01: MCX is a second VENUE — its session runs into the evening, past the NSE "
                "close, so the NSE calendar does not describe it."
            ),
        ),
    }
)


def instrument_facts_for(segment: TradingSegment) -> SegmentInstrumentFacts:
    """The facts for one segment, or an error.

    Never a default. A fallback here would silently hand a segment somebody else's settlement style
    or carry rule, which is the most expensive wrong answer this table can produce.
    """
    try:
        return SEGMENT_INSTRUMENT_FACTS[segment]
    except (KeyError, TypeError) as failure:
        raise SegmentTaxonomyError(
            f"no instrument facts for {segment!r}; the six segments are "
            f"{sorted(member.value for member in TradingSegment)}"
        ) from failure
