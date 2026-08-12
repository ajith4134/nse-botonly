"""WHICH levy applies to which segment, on what base, on which leg — dated and cited.

A rate record answers "how much"; it cannot answer "of what" or "from whom", and both of
those have their own history:

- Exercised options moved from the settlement price to INTRINSIC VALUE on 2019-09-01. No
  change of rate records that, and pricing a 2026 exercise under the old basis overstates it
  by roughly 120x (`research/219` §12.1).
- Currency derivatives carry no STT at all. That is a structural absence, and a model that
  represents it as "rate zero" cannot tell it apart from a rate nobody has looked up.

Why these facts are not in the `L0.31` rule store: `RuleScope` addresses a subject
(segment, underlying, symbol), not a levy, so expressing "the base of the STT on options"
would need a parallel `*_BASIS` and `*_LEG` family for every component — eighteen families to
carry structure for six levies. They live here instead, under the same discipline the store
enforces: an effective window, a source reference, an evidence grade, and a refusal rather
than a substitution when a date is not covered.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from nse_algo_trader.market_rules.point_in_time_market_rule_store import EvidenceGrade
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    ChargeComponent,
    LegApplicability,
    TaxableBase,
)


class ChargeStructureError(Exception):
    """Base for every structural failure, so a caller can catch the family."""


class ChargeStructureCoverageError(ChargeStructureError):
    """No structural fact covers this (component, segment, date).

    Raised rather than defaulting, for the same reason `RuleCoverageError` exists: a default
    here would silently price a historical trade under today's structure and the result would
    look entirely reasonable.
    """


class ChargeStructureConflictError(ChargeStructureError):
    """Two structural facts claim the same (component, segment, date).

    Never resolved by preference — two overlapping claims about a levy's base mean the
    compilation is wrong, and picking one would hide that.
    """


@dataclass(frozen=True, slots=True)
class ChargeStructureRecord:
    """One dated structural fact about one levy on one segment."""

    component: ChargeComponent
    segment: ChargeableSegment
    taxable_base: TaxableBase
    leg: LegApplicability
    effective_from: date
    effective_to: date | None
    source_reference: str
    source_date: date
    grade: EvidenceGrade

    def __post_init__(self) -> None:
        if not self.source_reference.strip():
            raise ChargeStructureError(
                f"{self.component} on {self.segment} has an empty source_reference — an "
                f"uncited structural claim is indistinguishable from a guess"
            )
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ChargeStructureError(
                f"{self.component} on {self.segment} has the empty or inverted interval "
                f"[{self.effective_from}, {self.effective_to}); effective_to is EXCLUSIVE"
            )

    def covers_date(self, as_of: date) -> bool:
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of < self.effective_to


class ChargeStructureHistory:
    """Resolves (component, segment, date) to the structure in force, or refuses."""

    def __init__(self, records: Iterable[ChargeStructureRecord]) -> None:
        self._records: tuple[ChargeStructureRecord, ...] = tuple(records)

    @property
    def records(self) -> Sequence[ChargeStructureRecord]:
        return self._records

    def resolve(
        self, component: ChargeComponent, segment: ChargeableSegment, as_of: date
    ) -> ChargeStructureRecord:
        matches = [
            record
            for record in self._records
            if record.component is component
            and record.segment is segment
            and record.covers_date(as_of)
        ]
        if not matches:
            raise ChargeStructureCoverageError(
                f"no structural fact for {component} on {segment} at {as_of} — either the "
                f"levy does not apply to this segment or the era is uncompiled, and the two "
                f"must not be guessed apart"
            )
        if len(matches) > 1:
            raise ChargeStructureConflictError(
                f"{len(matches)} structural facts claim {component} on {segment} at {as_of}: "
                + "; ".join(sorted(record.source_reference for record in matches))
            )
        return matches[0]

    def components_for(
        self, segment: ChargeableSegment, as_of: date
    ) -> tuple[ChargeStructureRecord, ...]:
        """Every levy in force on this segment at this date, in contract-note order.

        This is what makes an absent levy visible: a segment simply has fewer records, and
        nothing anywhere holds a zero rate that could be mistaken for a looked-up one.
        """
        applicable = [
            record
            for record in self._records
            if record.segment is segment and record.covers_date(as_of)
        ]
        return tuple(sorted(applicable, key=lambda record: _COMPONENT_ORDER[record.component]))


_COMPONENT_ORDER: dict[ChargeComponent, int] = {
    component: index
    for index, component in enumerate(
        (
            ChargeComponent.BROKERAGE,
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeComponent.COMMODITIES_TRANSACTION_TAX,
            ChargeComponent.EXCHANGE_TRANSACTION_CHARGE,
            ChargeComponent.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            ChargeComponent.SEBI_TURNOVER_FEE,
            ChargeComponent.STAMP_DUTY,
            ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE,
            ChargeComponent.GOODS_AND_SERVICES_TAX,
        )
    )
}
"""Contract-note order, so a printed breakdown can be read against a real one line by line.

GST is last because it is charged on the lines above it.
"""

_UNIFORM_CHARGE_ERA_START = date(2024, 10, 1)
"""SEBI's 'True to Label' flat-rate era. Before it the exchange charge was a turnover slab."""

_EXCHANGE_CHARGE_ERA_START = date(2004, 10, 1)
"""When an exchange transaction charge first became payable at all.

Kept distinct from the flat-rate era on purpose. A structure record says the levy EXISTS; a
rate record says what it is. Starting the structure in 2024 would have said the levy did not
exist before then, and a 2019 trade would have priced cleanly with the charge simply missing —
silently cheaper, and in exactly the direction that flatters a backtest.
"""

_STAMP_DUTY_ERA_START = date(2020, 7, 1)
_STT_ERA_START = date(2013, 6, 1)
_CTT_ERA_START = date(2013, 7, 1)


def _structure(
    component: ChargeComponent,
    segment: ChargeableSegment,
    base: TaxableBase,
    leg: LegApplicability,
    effective_from: date,
    *,
    effective_to: date | None = None,
    source: str,
    source_date: date,
    grade: EvidenceGrade = EvidenceGrade.SECONDARY_TRIANGULATED,
) -> ChargeStructureRecord:
    return ChargeStructureRecord(
        component=component,
        segment=segment,
        taxable_base=base,
        leg=leg,
        effective_from=effective_from,
        effective_to=effective_to,
        source_reference=source,
        source_date=source_date,
        grade=grade,
    )


def _transaction_tax_structures() -> tuple[ChargeStructureRecord, ...]:
    """STT and CTT: who pays, and on what.

    Currency derivatives appear nowhere here, deliberately — no securities transaction tax is
    levied on them. Their absence from this table is the fact.
    """
    return (
        _structure(
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeableSegment.EQUITY_DELIVERY,
            TaxableBase.TRADE_TURNOVER,
            LegApplicability.BOTH_LEGS,
            _STT_ERA_START,
            source="Finance (No.2) Act 2004 s.98 — delivery equity STT on purchase AND sale",
            source_date=date(2013, 5, 10),
        ),
        _structure(
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeableSegment.EQUITY_INTRADAY,
            TaxableBase.TRADE_TURNOVER,
            LegApplicability.SELL_ONLY,
            _STT_ERA_START,
            source="Finance (No.2) Act 2004 s.98 — non-delivery equity STT, seller only",
            source_date=date(2013, 5, 10),
        ),
        _structure(
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeableSegment.EQUITY_FUTURES,
            TaxableBase.TRADE_TURNOVER,
            LegApplicability.SELL_ONLY,
            _STT_ERA_START,
            source=(
                "Finance (No.2) Act 2004 s.98 — futures STT on the price at which traded, seller"
            ),
            source_date=date(2013, 5, 20),
        ),
        _structure(
            ChargeComponent.SECURITIES_TRANSACTION_TAX,
            ChargeableSegment.EQUITY_OPTIONS,
            TaxableBase.OPTION_PREMIUM_TURNOVER,
            LegApplicability.SELL_ONLY,
            _STT_ERA_START,
            source=(
                "NSE/FATAX/23500 — options STT on PREMIUM, seller only. The pre-2008 basis of "
                "strike plus premium is not compiled and those dates are refused"
            ),
            source_date=date(2013, 5, 20),
        ),
        _structure(
            ChargeComponent.COMMODITIES_TRANSACTION_TAX,
            ChargeableSegment.COMMODITY_FUTURES,
            TaxableBase.TRADE_TURNOVER,
            LegApplicability.SELL_ONLY,
            _CTT_ERA_START,
            source="Finance Act 2013 s.117 CTT schedule — commodity futures, seller",
            source_date=date(2013, 7, 1),
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
        ),
        _structure(
            ChargeComponent.COMMODITIES_TRANSACTION_TAX,
            ChargeableSegment.COMMODITY_OPTIONS,
            TaxableBase.OPTION_PREMIUM_TURNOVER,
            LegApplicability.SELL_ONLY,
            date(2018, 4, 1),
            source="Finance Act 2013 s.117 CTT schedule — commodity options on premium, seller",
            source_date=date(2018, 4, 1),
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
        ),
    )


def _exchange_and_regulator_structures() -> tuple[ChargeStructureRecord, ...]:
    """Exchange charge, IPFT and the SEBI fee — every one of them on BOTH legs.

    The SEBI fee's base is the disputed one: NSE charges it on NOTIONAL turnover for options,
    not on premium, which `research/164` and `b28` both got wrong. On a 24,000-strike option
    trading at a 150 premium that is a 160-fold difference in the base.
    """
    records: list[ChargeStructureRecord] = []
    for segment in ChargeableSegment:
        premium_or_turnover = (
            TaxableBase.OPTION_PREMIUM_TURNOVER if segment.is_option else TaxableBase.TRADE_TURNOVER
        )
        records.append(
            _structure(
                ChargeComponent.EXCHANGE_TRANSACTION_CHARGE,
                segment,
                premium_or_turnover,
                LegApplicability.BOTH_LEGS,
                _EXCHANGE_CHARGE_ERA_START,
                source=(
                    "SEBI/HO/MRD/TPD-1/P/CIR/2024/92 'True to Label', NSE/FA/64232 and "
                    "NSE/FA/73061 — a uniform per-side charge; options are charged on premium. "
                    "The STRUCTURE starts when the levy did, not when the flat era did: an "
                    "exchange charge has always been payable, so a pre-2024 trade must REFUSE "
                    "for want of a rate rather than be priced as if no charge existed"
                ),
                source_date=date(2024, 9, 27),
                grade=EvidenceGrade.PRIMARY_CIRCULAR,
            )
        )
        records.append(
            _structure(
                ChargeComponent.SEBI_TURNOVER_FEE,
                segment,
                TaxableBase.OPTION_NOTIONAL_TURNOVER
                if segment.is_option
                else TaxableBase.TRADE_TURNOVER,
                LegApplicability.BOTH_LEGS,
                date(2006, 9, 25),
                source=(
                    "SEBI (Stock Brokers) (Third Amendment) Regulations 2006 S.O. 1600(E). The "
                    "NOTIONAL basis for options rests on BSE's own 2024-04-26 disclosure of a "
                    "SEBI letter and a ~Rs 165 crore back-payment; no public circular states "
                    "it, and research/164 and b28 both record premium, which is wrong"
                ),
                source_date=date(2024, 4, 26),
            )
        )
    for segment in (
        ChargeableSegment.EQUITY_DELIVERY,
        ChargeableSegment.EQUITY_INTRADAY,
        ChargeableSegment.EQUITY_FUTURES,
        ChargeableSegment.EQUITY_OPTIONS,
    ):
        records.append(
            _structure(
                ChargeComponent.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
                segment,
                TaxableBase.OPTION_PREMIUM_TURNOVER
                if segment.is_option
                else TaxableBase.TRADE_TURNOVER,
                LegApplicability.BOTH_LEGS,
                date(2023, 4, 1),
                source=(
                    "NSE/FA/56129 and NSE/FA/73061 — a separately billed line, per side. No "
                    "IPFT line item was found for the commodity or SLB segments, so they carry "
                    "no record rather than a zero"
                ),
                source_date=date(2023, 3, 24),
                grade=EvidenceGrade.PRIMARY_CIRCULAR,
            )
        )
    return tuple(records)


def _stamp_duty_structures() -> tuple[ChargeStructureRecord, ...]:
    """Buy side only, and that is statutory: Indian Stamp Act s.9A(1)(a)."""
    return tuple(
        _structure(
            ChargeComponent.STAMP_DUTY,
            segment,
            TaxableBase.OPTION_PREMIUM_TURNOVER
            if segment.is_option
            else TaxableBase.TRADE_TURNOVER,
            LegApplicability.BUY_ONLY,
            _STAMP_DUTY_ERA_START,
            source=(
                "Indian Stamp Act s.9A(1)(a) as amended by Finance Act 2019: collected 'from "
                "its buyer on the market value of such securities at the time of settlement'. "
                "Before 2020-07-01 duty was state-by-state and no aggregation exists"
            ),
            source_date=date(2020, 7, 1),
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
        )
        for segment in ChargeableSegment
    )


def _brokerage_and_depository_structures() -> tuple[ChargeStructureRecord, ...]:
    """Brokerage is per ORDER; the DP charge is per DEBIT and only on a delivery sell."""
    records: list[ChargeStructureRecord] = [
        _structure(
            ChargeComponent.BROKERAGE,
            segment,
            TaxableBase.PER_ORDER,
            LegApplicability.BOTH_LEGS,
            date(2010, 1, 1),
            source=(
                "Broker commercial schedule, not a statutory levy — carried in "
                "broker_fee_schedules because it changes when the broker changes"
            ),
            source_date=date(2026, 8, 12),
        )
        for segment in ChargeableSegment
    ]
    records.append(
        _structure(
            ChargeComponent.DEPOSITORY_PARTICIPANT_CHARGE,
            ChargeableSegment.EQUITY_DELIVERY,
            TaxableBase.PER_DEBIT_TRANSACTION,
            LegApplicability.SELL_ONLY,
            date(2024, 10, 1),
            source=(
                "CDSL media release 2024-09-26 and the NSDL fee schedule — a flat charge per "
                "debit instruction, independent of quantity, on the sell leg only. Intraday "
                "positions never reach the depository, which is why only delivery carries it"
            ),
            source_date=date(2024, 9, 26),
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
        )
    )
    return tuple(records)


def _goods_and_services_tax_structures() -> tuple[ChargeStructureRecord, ...]:
    return tuple(
        _structure(
            ChargeComponent.GOODS_AND_SERVICES_TAX,
            segment,
            TaxableBase.TAXABLE_SERVICE_VALUE,
            LegApplicability.BOTH_LEGS,
            date(2017, 7, 1),
            source=(
                "CGST Act s.15 value-of-supply — 18% on brokerage, exchange charge, IPFT, the "
                "SEBI fee and DP charges. NOT on STT/CTT or stamp duty, because s.2(52) and "
                "s.2(102) exclude securities from both definitions"
            ),
            source_date=date(2017, 7, 1),
        )
        for segment in ChargeableSegment
    )


def nse_charge_structure_history() -> ChargeStructureHistory:
    """The compiled structure of every levy this engine can price."""
    return ChargeStructureHistory(
        (
            *_transaction_tax_structures(),
            *_exchange_and_regulator_structures(),
            *_stamp_duty_structures(),
            *_brokerage_and_depository_structures(),
            *_goods_and_services_tax_structures(),
        )
    )


OPTION_EXERCISE_STRUCTURES: tuple[ChargeStructureRecord, ...] = (
    _structure(
        ChargeComponent.SECURITIES_TRANSACTION_TAX,
        ChargeableSegment.EQUITY_OPTIONS,
        TaxableBase.SETTLEMENT_VALUE,
        LegApplicability.BUY_ONLY,
        _STT_ERA_START,
        effective_to=date(2019, 9, 1),
        source=(
            "Pre-2019 rule: STT on an exercised option was charged on the full SETTLEMENT "
            "VALUE. Carried so the era is described rather than silently priced under today's "
            "basis; the rate itself is uncompiled, so these dates still refuse"
        ),
        source_date=date(2013, 6, 1),
        grade=EvidenceGrade.UNVERIFIED_SNIPPET,
    ),
    _structure(
        ChargeComponent.SECURITIES_TRANSACTION_TAX,
        ChargeableSegment.EQUITY_OPTIONS,
        TaxableBase.OPTION_INTRINSIC_VALUE,
        LegApplicability.BUY_ONLY,
        date(2019, 9, 1),
        source=(
            "Finance (No.2) Act 2019 s.99(a)(ii) — STT on an exercised option is charged on "
            "INTRINSIC VALUE (settlement price less strike) and paid by the PURCHASER. This "
            "replaced the settlement-value basis on 2019-09-01, five years before the "
            "Oct-2024 rate changes left it untouched"
        ),
        source_date=date(2019, 8, 1),
    ),
)
"""Exercise, held apart from the sale of an option because it is a different taxable event.

Same component, same segment, opposite leg and a different base — which is exactly why it
cannot live in the main table keyed by (component, segment): it would collide with the
sell-side record and the conflict check would be right to reject it.
"""


def option_exercise_structure(as_of: date) -> ChargeStructureRecord:
    """The exercise structure in force, or a refusal."""
    matches = [record for record in OPTION_EXERCISE_STRUCTURES if record.covers_date(as_of)]
    if len(matches) != 1:
        raise ChargeStructureCoverageError(
            f"{len(matches)} exercise structures cover {as_of}; expected exactly one"
        )
    return matches[0]
