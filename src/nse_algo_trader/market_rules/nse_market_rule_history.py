"""The compiled NSE/SEBI rule facts, each carrying the circular that establishes it.

Source: `docs/research/61` §2 — the acquisition pass that established there is **no
machine-readable point-in-time rules dataset anywhere**, for any of these families. Every
value below was compiled by hand from an individually-dated circular or a triangulated
secondary source, and carries the grade that source earned there.

**Every rate is a FRACTION OF TURNOVER, not a percentage.** The circulars are written in
percent (0.0625%) and the arithmetic wants a fraction (0.000625). Converting at the point
of use is how a factor-of-100 error gets into a cost model, so the conversion happens once,
here, and each record's source reference carries the circular's own wording so the two can
be checked against each other.

**What is deliberately NOT seeded, and why that is the honest choice:**

- **Exchange transaction charges before 1-Oct-2024.** They were a turnover SLAB schedule,
  not a flat rate (`research/61` §2.5), and no aggregated table of the historical slabs
  exists anywhere. Seeding a flat approximation would make every pre-2024 cost model
  quietly wrong; leaving it uncovered makes the store refuse, which is correct.
- **Stamp duty before 1-Jul-2020.** State-by-state, no aggregation exists anywhere.
- **Per-stock 2/5/10/20% price bands.** Decided ad hoc daily by NSE Surveillance and
  published in no circular at all — the single largest gap in the whole rules section.
- **The 2004-2013 options-STT path.** Grade-C snippets only; a guess here would be
  indistinguishable from a fact.

Those absences are the feature. `PointInTimeMarketRuleStore` raises `RuleCoverageError`
for them rather than extrapolating today's regime backwards.
"""

from __future__ import annotations

from datetime import date

from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EVERYTHING,
    EvidenceGrade,
    MarketRuleRecord,
    PointInTimeMarketRuleStore,
    RuleFamily,
    RuleScope,
    RuleValueKind,
)

COMPILED_ON = date(2026, 8, 12)
"""When this table was compiled — the transaction-time stamp on every record below.

A later correction gets a later `recorded_at` and supersedes without deleting, so a study
run today can be reproduced after the correction by passing `known_as_of=COMPILED_ON`.
"""

_OPTIONS = RuleScope(segment="NFO-OPT", index_or_underlying=None, symbol=None)
_OPTIONS_EXERCISED = RuleScope(segment="NFO-OPT-EXERCISE")
_FUTURES = RuleScope(segment="NFO-FUT")
_CASH_DELIVERY = RuleScope(segment="NSE-CNC")
_CASH_INTRADAY = RuleScope(segment="NSE-MIS")
_CURRENCY_FUTURES = RuleScope(segment="CDS-FUT")
_CURRENCY_OPTIONS = RuleScope(segment="CDS-OPT")
_COMMODITY_FUTURES = RuleScope(segment="MCX-FUT")
_COMMODITY_OPTIONS = RuleScope(segment="MCX-OPT")
"""The charge-scope vocabulary (`A.90`).

`RuleScope.segment` was carrying two meanings — Kite exchange codes (`NSE`, `NFO`, which is what
`InstrumentMasterRuleObserver` emits) and instrument type (`NFO-FUT`). Charges need a third axis
neither expressed: PRODUCT MODE, because delivery and intraday are taxed differently on the same
exchange segment. These constants are the single declared vocabulary; `ChargeableSegment` in
`transaction_cost.chargeable_market_segments` maps to exactly these strings and nothing else may
invent a variant.

Index-vs-stock is deliberately absent: the statutory rates are identical for index and stock
derivatives, and splitting the scope would invent a difference that does not exist. It reappears as
`index_or_underlying` only where it genuinely bites (physical settlement).
"""


def _fact(
    family: RuleFamily,
    value: str,
    effective_from: date,
    *,
    scope: RuleScope = EVERYTHING,
    effective_to: date | None = None,
    kind: RuleValueKind = RuleValueKind.DECIMAL_FRACTION,
    source: str,
    source_date: date,
    grade: EvidenceGrade = EvidenceGrade.PRIMARY_CIRCULAR,
) -> MarketRuleRecord:
    return MarketRuleRecord(
        family=family,
        scope=scope,
        value=value,
        value_kind=kind,
        effective_from=effective_from,
        effective_to=effective_to,
        source_reference=source,
        source_date=source_date,
        grade=grade,
        recorded_at=COMPILED_ON,
    )


def securities_transaction_tax_facts() -> tuple[MarketRuleRecord, ...]:
    """STT across cash and F&O (`research/61` §2.5, `research/219` §12.1).

    Options are taxed on PREMIUM, futures on turnover, cash on turnover — a difference big
    enough that applying one rate to the other is not a rounding error. The scopes are
    separate precisely so a caller cannot pick up the wrong one by asking loosely.

    The BASIS and the LEG each rate applies to are not stored here — a rate record carries a
    number, not the structure it multiplies. They live in
    `transaction_cost.charge_structure_history`, dated and cited the same way, because the
    structure has its own history: exercised options moved from settlement price to intrinsic
    value on 2019-09-01, which no change of rate would record.
    """
    return (
        # Cash equity. Delivery is taxed on BOTH legs, intraday on the sell leg only, and the
        # two rates differ by 4x — which is why they are separate scopes rather than one
        # "NSE" rate with a note. Added by `L1.01` (`A.90`): the cash segment could not be
        # priced at all before this, because the store correctly refused a family it had no
        # fact for.
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.001",
            date(2013, 6, 1),
            scope=_CASH_DELIVERY,
            source=(
                "Finance Act 2013 amendment to Finance (No.2) Act 2004 s.98 — delivery equity "
                "STT 0.125% -> 0.1%, charged on BOTH the purchase and the sale"
            ),
            source_date=date(2013, 5, 10),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.00025",
            date(2013, 6, 1),
            scope=_CASH_INTRADAY,
            source=(
                "Finance (No.2) Act 2004 s.98 as amended — non-delivery (intraday) equity STT "
                "0.025% of turnover, sell side only"
            ),
            source_date=date(2013, 5, 10),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        # Options, premium basis. 0.05% -> 0.0625% -> 0.1% -> 0.15%.
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0005",
            date(2013, 6, 1),
            scope=_OPTIONS,
            effective_to=date(2023, 4, 1),
            source="NSE/FATAX/23500 — options STT 0.05% of premium, sell side",
            source_date=date(2013, 5, 20),
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.000625",
            date(2023, 4, 1),
            scope=_OPTIONS,
            effective_to=date(2024, 10, 1),
            source="Finance Act 2023 — options STT 0.05% -> 0.0625% of premium",
            source_date=date(2023, 3, 31),
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.001",
            date(2024, 10, 1),
            scope=_OPTIONS,
            effective_to=date(2026, 4, 1),
            source="Finance (No.2) Act 2024 — options STT 0.0625% -> 0.1% of premium",
            source_date=date(2024, 7, 23),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0015",
            date(2026, 4, 1),
            scope=_OPTIONS,
            source="Finance Act 2025 — options STT 0.1% -> 0.15% of premium (already law)",
            source_date=date(2025, 3, 31),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        # Futures, turnover basis. 0.01% -> 0.0125% -> 0.02% -> 0.05%.
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0001",
            date(2013, 6, 1),
            scope=_FUTURES,
            effective_to=date(2023, 4, 1),
            source="NSE/FATAX/23500 — futures STT 0.017% -> 0.01%, sell side",
            source_date=date(2013, 5, 20),
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.000125",
            date(2023, 4, 1),
            scope=_FUTURES,
            effective_to=date(2024, 10, 1),
            source="Finance Act 2023 — futures STT 0.01% -> 0.0125%",
            source_date=date(2023, 3, 31),
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0002",
            date(2024, 10, 1),
            scope=_FUTURES,
            effective_to=date(2026, 4, 1),
            source="Finance (No.2) Act 2024 — futures STT 0.0125% -> 0.02%",
            source_date=date(2024, 7, 23),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0005",
            date(2026, 4, 1),
            scope=_FUTURES,
            source="Finance Act 2025 — futures STT 0.02% -> 0.05% (already law)",
            source_date=date(2025, 3, 31),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        # Options taken to EXERCISE are a different taxable event from options sold: a
        # different rate, a different base, and the buyer pays rather than the seller. Held
        # apart because folding them together is the "121x folklore" error -- pricing an
        # exercise as if the pre-2019 full-notional rule still applied makes a system
        # irrationally afraid of holding to expiry.
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.00125",
            date(2019, 9, 1),
            scope=_OPTIONS_EXERCISED,
            effective_to=date(2026, 4, 1),
            source=(
                "Finance (No.2) Act 2019 s.99(a)(ii) — STT on exercised options 0.125% of "
                "INTRINSIC VALUE (settlement price less strike), payable by the purchaser; "
                "this replaced the settlement-price basis on 2019-09-01, five years before "
                "the Oct-2024 rate changes that left it untouched"
            ),
            source_date=date(2019, 8, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.SECURITIES_TRANSACTION_TAX,
            "0.0015",
            date(2026, 4, 1),
            scope=_OPTIONS_EXERCISED,
            source=(
                "Finance Bill 2026 (Bill No. 3 of 2026) Clause 143(ii) — STT on exercised "
                "options 0.125% -> 0.15% of intrinsic value, effective 2026-04-01"
            ),
            source_date=date(2026, 2, 1),
        ),
    )


def commodities_transaction_tax_facts() -> tuple[MarketRuleRecord, ...]:
    """CTT — a different levy under a different statute, not STT with another name.

    Exercised commodity options split by HOW they settle: physically-delivered exercise is
    taxed at 0.0001% of the settlement price, cash-settled exercise at 0.125% of intrinsic
    value. Aggregators routinely collapse the two into the first number, which understates a
    cash-settled exercise by more than a thousandfold.
    """
    _ctt_source = (
        "Finance Act 2013 s.117 CTT schedule, reproduced in the NCDEX member compliance "
        "guide (PDF fetched)"
    )
    return (
        _fact(
            RuleFamily.COMMODITIES_TRANSACTION_TAX,
            "0.0001",
            date(2013, 7, 1),
            scope=_COMMODITY_FUTURES,
            source=f"{_ctt_source} — commodity futures 0.01% of traded price, seller",
            source_date=date(2013, 7, 1),
        ),
        _fact(
            RuleFamily.COMMODITIES_TRANSACTION_TAX,
            "0.0005",
            date(2018, 4, 1),
            scope=_COMMODITY_OPTIONS,
            source=(
                f"{_ctt_source} — commodity options 0.05% of premium, seller. The RATE is "
                "primary; the 2018-04-01 extension date is secondary"
            ),
            source_date=date(2018, 4, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
    )


def stamp_duty_facts() -> tuple[MarketRuleRecord, ...]:
    """Uniform national rates from 1-Jul-2020. Before that: no aggregation exists.

    Buy side only, and that is statutory rather than conventional: Indian Stamp Act s.9A(1)(a)
    says the duty "shall be collected ... from its buyer on the market value of such securities
    at the time of settlement". (s.9B puts it on the transferor for off-market transfers, which
    is why the buy-side rule cannot be stated globally — only for on-exchange trades.)

    CORRECTED by `L1.01` (`A.90`): the delivery rate was previously scoped to ALL cash, so
    every intraday query resolved to 0.015% instead of 0.003% — five times too high, from a
    record that looked correct because the number itself was right.
    """
    return (
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00015",
            date(2020, 7, 1),
            scope=_CASH_DELIVERY,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "equity delivery 0.015% of market value at settlement, buy side"
            ),
            source_date=date(2020, 7, 1),
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00003",
            date(2020, 7, 1),
            scope=_CASH_INTRADAY,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "equity intraday 0.003%, buy side"
            ),
            source_date=date(2020, 7, 1),
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00002",
            date(2020, 7, 1),
            scope=_COMMODITY_FUTURES,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "futures 0.002%, buy side; commodity and equity futures share ONE rate"
            ),
            source_date=date(2020, 7, 1),
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00003",
            date(2020, 7, 1),
            scope=_COMMODITY_OPTIONS,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "options 0.003% of premium, buy side; commodity and equity options share ONE rate"
            ),
            source_date=date(2020, 7, 1),
        ),
        # Currency futures AND options share a single 0.0001% line in SEBI's own table. Stored
        # as two records with one source string saying so, rather than two independently
        # derived rates that happen to be equal -- a later revision would move both together.
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.000001",
            date(2020, 7, 1),
            scope=_CURRENCY_FUTURES,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "currency and interest-rate derivatives 0.0001%, buy side, ONE line covering "
                "both futures and options"
            ),
            source_date=date(2020, 7, 1),
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.000001",
            date(2020, 7, 1),
            scope=_CURRENCY_OPTIONS,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act, SEBI FAQ (PDF fetched) — "
                "currency and interest-rate derivatives 0.0001%, buy side, ONE line covering "
                "both futures and options"
            ),
            source_date=date(2020, 7, 1),
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00002",
            date(2020, 7, 1),
            scope=_FUTURES,
            source="Finance Act 2019 amendments to the Indian Stamp Act — futures 0.002%, buy side",
            source_date=date(2020, 7, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00003",
            date(2020, 7, 1),
            scope=_OPTIONS,
            source="Finance Act 2019 amendments to the Indian Stamp Act — options 0.003%, buy side",
            source_date=date(2020, 7, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
    )


def expiry_weekday_facts() -> tuple[MarketRuleRecord, ...]:
    """Per-index expiry weekday. The family most likely to be silently wrong in a replay.

    NIFTY expired on Thursday for six and a half years and on Tuesday since Sep-2025;
    BANKNIFTY moved Thursday -> Wednesday -> Thursday -> (weeklies discontinued). A replay
    using today's weekday puts expiry on days that were ordinary sessions, which changes
    every gamma, every pin, and every square-off.
    """
    return (
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "THURSDAY",
            date(2019, 2, 11),
            scope=RuleScope(index_or_underlying="NIFTY"),
            effective_to=date(2025, 9, 1),
            kind=RuleValueKind.WEEKDAY,
            source="NIFTY weekly options launched 11-Feb-2019, Thursday expiry",
            source_date=date(2019, 2, 11),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "TUESDAY",
            date(2025, 9, 1),
            kind=RuleValueKind.WEEKDAY,
            source="NSE 111/2025 (NSE/FAOP/68747) — all NSE expiries Thursday -> Tuesday",
            source_date=date(2025, 6, 25),
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "THURSDAY",
            date(2016, 5, 27),
            scope=RuleScope(index_or_underlying="BANKNIFTY"),
            effective_to=date(2023, 9, 6),
            kind=RuleValueKind.WEEKDAY,
            source="BANKNIFTY weekly options launched 27-May-2016, Thursday expiry",
            source_date=date(2016, 5, 5),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "WEDNESDAY",
            date(2023, 9, 6),
            scope=RuleScope(index_or_underlying="BANKNIFTY"),
            effective_to=date(2025, 1, 2),
            kind=RuleValueKind.WEEKDAY,
            source="NSE 119/2023 (NSE/FAOP/57540) — BANKNIFTY Thursday -> Wednesday",
            source_date=date(2023, 7, 12),
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "THURSDAY",
            date(2025, 1, 2),
            scope=RuleScope(index_or_underlying="BANKNIFTY"),
            effective_to=date(2025, 9, 1),
            kind=RuleValueKind.WEEKDAY,
            source="NSE 154/2024 (NSE/FAOP/65336) — BANKNIFTY Wednesday -> Thursday",
            source_date=date(2024, 11, 29),
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "MONDAY",
            date(2023, 8, 16),
            scope=RuleScope(index_or_underlying="MIDCPNIFTY"),
            effective_to=date(2024, 11, 20),
            kind=RuleValueKind.WEEKDAY,
            source="NSE/FAOP/57538 — MIDCPNIFTY Monday expiry",
            source_date=date(2023, 6, 23),
        ),
        _fact(
            RuleFamily.EXPIRY_CYCLE,
            "TUESDAY",
            date(2021, 10, 18),
            scope=RuleScope(index_or_underlying="FINNIFTY"),
            effective_to=date(2024, 11, 20),
            kind=RuleValueKind.WEEKDAY,
            source="FINNIFTY Tuesday expiry — single broker source, not circular-confirmed",
            source_date=date(2021, 10, 18),
            grade=EvidenceGrade.UNVERIFIED_SNIPPET,
        ),
    )


def minimum_contract_value_facts() -> tuple[MarketRuleRecord, ...]:
    """Minimum contract value for index derivatives.

    Renamed from `contract_value_and_charge_facts` by `L1.01`: the exchange-charge record it
    also carried has moved to `exchange_transaction_charge_facts`, which now holds every
    segment rather than one mislabelled one, so the old name described work the function no
    longer does.
    """
    return (
        _fact(
            RuleFamily.MINIMUM_CONTRACT_VALUE,
            "500000",
            date(2015, 10, 30),
            kind=RuleValueKind.INTEGER,
            effective_to=date(2024, 11, 20),
            source="SEBI Jul-2015 circular — index derivatives minimum contract value Rs 5 lakh",
            source_date=date(2015, 7, 13),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.MINIMUM_CONTRACT_VALUE,
            "1500000",
            date(2024, 11, 20),
            kind=RuleValueKind.INTEGER,
            source="SEBI/HO/MRD/TPD/P/CIR/2024/132 — raised to Rs 15 lakh",
            source_date=date(2024, 10, 1),
        ),
    )


_UNIFORM_RATE_ERA = (
    "Nothing BEFORE 2024-10-01 is seeded and that is deliberate: until SEBI's "
    "'True to Label' circular SEBI/HO/MRD/TPD-1/P/CIR/2024/92 (2024-07-01) the schedule was a "
    "turnover SLAB, its tier breakpoints are published nowhere in aggregate, and a flat rate "
    "applied to a slab era is wrong by construction. Those dates are REFUSED, not guessed"
)


def exchange_transaction_charge_facts() -> tuple[MarketRuleRecord, ...]:
    """NSE's own charge, per side. Two eras, and the second is not a fee rise.

    CORRECTED by `L1.01` (`A.90`): the options scope previously carried `0.0000297`, which is
    the CASH rate of the same era (Rs 297/crore). Options are charged on premium at roughly
    twelve times that. The record resolved cleanly, reported itself covered, and was wrong.

    Read together with `investor_protection_fund_contribution_facts` — NSE/FA/73061 moved
    money between the two lines without changing the total, so either line read alone tells a
    misleading story about 2026-03-01.
    """
    return (
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0000297",
            date(2024, 10, 1),
            scope=_CASH_DELIVERY,
            effective_to=date(2026, 3, 1),
            source=(
                f"NSE/FA/64232 (PDF fetched) — cash market Rs 297/crore each side. "
                f"{_UNIFORM_RATE_ERA}"
            ),
            source_date=date(2024, 9, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0000297",
            date(2024, 10, 1),
            scope=_CASH_INTRADAY,
            effective_to=date(2026, 3, 1),
            source=(
                f"NSE/FA/64232 (PDF fetched) — cash market Rs 297/crore each side. "
                f"{_UNIFORM_RATE_ERA}"
            ),
            source_date=date(2024, 9, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000030699",
            date(2026, 3, 1),
            scope=_CASH_DELIVERY,
            source=(
                "NSE/FA/73061 (PDF fetched), verbatim: 'Cash Market: Transaction Charges Rs. "
                "306.99 each side, Contribution to NSE IPFT Rs. 0.01 each side, Total Rs. 307 "
                "each side'"
            ),
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000030699",
            date(2026, 3, 1),
            scope=_CASH_INTRADAY,
            source=(
                "NSE/FA/73061 (PDF fetched) — cash market Rs 306.99/crore each side, plus "
                "Rs 0.01 IPFT, total Rs 307 unchanged"
            ),
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0000173",
            date(2024, 10, 1),
            scope=_FUTURES,
            effective_to=date(2026, 3, 1),
            source=(
                f"NSE/FA/64232 (PDF fetched) — equity futures Rs 173/crore each side. "
                f"{_UNIFORM_RATE_ERA}"
            ),
            source_date=date(2024, 9, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000018299",
            date(2026, 3, 1),
            scope=_FUTURES,
            source=(
                "NSE/FA/73061 (PDF fetched) — equity futures Rs 183/crore each side including "
                "the Rs 0.01 IPFT; total outflow unchanged"
            ),
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0003503",
            date(2024, 10, 1),
            scope=_OPTIONS,
            effective_to=date(2026, 3, 1),
            source=(
                f"NSE/FA/64232 (PDF fetched) — equity options Rs 3,503/crore OF PREMIUM each "
                f"side, plus Rs 50/crore IPFT, total Rs 3,553. {_UNIFORM_RATE_ERA}"
            ),
            source_date=date(2024, 9, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000355299",
            date(2026, 3, 1),
            scope=_OPTIONS,
            source=(
                "NSE/FA/73061 (PDF fetched) — equity options Rs 3,552.99/crore of premium each "
                "side plus Rs 0.01 IPFT; the IPFT rollback moved Rs 50 into the base charge and "
                "left the Rs 3,553 total unchanged"
            ),
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000000035",
            date(2024, 10, 1),
            scope=_CURRENCY_FUTURES,
            source="Broker tariff disclosures — currency futures Rs 35/crore of notional each side",
            source_date=date(2026, 8, 12),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        # MCX flattened its own slabs under the same SEBI circular. Its historical structure
        # was TWO tiers (agri and non-agri), not the four commodity groups the segment names
        # suggest, and the 2017-2024 tier table could not be recovered — so, as with NSE,
        # nothing before the flat era is seeded and those dates refuse.
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000021",
            date(2024, 10, 1),
            scope=_COMMODITY_FUTURES,
            source=(
                "MCX/F&A/631/2024 (2024-09-24, eff 2024-10-01) implementing "
                "SEBI/HO/MRD/TPD-1/P/CIR/2024/92 — futures Rs 2.10 per lakh of turnover. The "
                "MCX PDF itself could not be fetched (mcxindia.com returns 403 to automated "
                "requests); the figure is triangulated across five independent sources that "
                "agree on the rate AND the circular number"
            ),
            source_date=date(2024, 9, 24),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.000418",
            date(2024, 10, 1),
            scope=_COMMODITY_OPTIONS,
            source=(
                "MCX/F&A/631/2024 (2024-09-24, eff 2024-10-01) — options Rs 41.80 per lakh of "
                "PREMIUM turnover. Same fetch limitation and same five-source triangulation"
            ),
            source_date=date(2024, 9, 24),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0000311",
            date(2024, 10, 1),
            scope=_CURRENCY_OPTIONS,
            source=(
                "Broker tariff disclosures — currency options Rs 3,110/crore of premium each side"
            ),
            source_date=date(2026, 8, 12),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
    )


def investor_protection_fund_contribution_facts() -> tuple[MarketRuleRecord, ...]:
    """The second column of the same NSE bill, carried separately because it moves separately.

    NSE raised IPFT sharply in Apr-2023 while cutting the base transaction charge, then rolled
    it back on 2026-03-01 "since the corpus ... has been replenished and is currently adequate"
    while raising the base charge by the same amount. A model carrying only the total would
    show a flat line across both events and learn nothing; a model carrying only the base
    charge would show a rise in Mar-2026 that never reached the member.
    """
    return (
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000001",
            date(2023, 4, 1),
            scope=_CASH_DELIVERY,
            effective_to=date(2026, 3, 1),
            source="NSE/FA/56129 (2023-03-24, eff 2023-04-01) — cash IPFT Rs 10/crore each side",
            source_date=date(2023, 3, 24),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000001",
            date(2023, 4, 1),
            scope=_CASH_INTRADAY,
            effective_to=date(2026, 3, 1),
            source="NSE/FA/56129 (2023-03-24, eff 2023-04-01) — cash IPFT Rs 10/crore each side",
            source_date=date(2023, 3, 24),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000001",
            date(2023, 4, 1),
            scope=_FUTURES,
            effective_to=date(2026, 3, 1),
            source="NSE/FA/56129 — equity futures IPFT Rs 10/crore each side",
            source_date=date(2023, 3, 24),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000005",
            date(2023, 4, 1),
            scope=_OPTIONS,
            effective_to=date(2026, 3, 1),
            source="NSE/FA/56129 — equity options IPFT Rs 50/crore of premium each side",
            source_date=date(2023, 3, 24),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000000001",
            date(2026, 3, 1),
            scope=_CASH_DELIVERY,
            source="NSE/FA/73061 (PDF fetched) — IPFT rolled back to Rs 0.01/crore each side",
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000000001",
            date(2026, 3, 1),
            scope=_CASH_INTRADAY,
            source="NSE/FA/73061 (PDF fetched) — IPFT rolled back to Rs 0.01/crore each side",
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000000001",
            date(2026, 3, 1),
            scope=_FUTURES,
            source="NSE/FA/73061 (PDF fetched) — IPFT rolled back to Rs 0.01/crore each side",
            source_date=date(2026, 2, 27),
        ),
        _fact(
            RuleFamily.INVESTOR_PROTECTION_FUND_CONTRIBUTION,
            "0.000000001",
            date(2026, 3, 1),
            scope=_OPTIONS,
            source="NSE/FA/73061 (PDF fetched) — IPFT rolled back to Rs 0.01/crore each side",
            source_date=date(2026, 2, 27),
        ),
    )


def sebi_turnover_fee_facts() -> tuple[MarketRuleRecord, ...]:
    """Rs 10/crore, and the BASE for options is the part everybody gets wrong.

    NSE charges this on NOTIONAL turnover (strike x lot size) for options, not on premium.
    `research/164` and `b28` both recorded premium; they are wrong. SEBI forced BSE onto the
    notional basis by a letter BSE disclosed on 2024-04-26, with a back-payment of roughly
    Rs 165 crore including interest — a dispute that only makes sense if the basis is notional.
    No public circular states it, so this is SECONDARY however strongly corroborated, and the
    disagreement is recorded here rather than smoothed away.
    """
    _sebi_source = (
        "SEBI (Stock Brokers and Sub-Brokers) (Third Amendment) Regulations 2006, "
        "S.O. 1600(E) — Rs 10 per crore of turnover"
    )
    return tuple(
        _fact(
            RuleFamily.SEBI_TURNOVER_FEE,
            "0.000001",
            date(2006, 9, 25),
            scope=scope,
            source=_sebi_source,
            source_date=date(2006, 9, 25),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        )
        for scope in (
            _CASH_DELIVERY,
            _CASH_INTRADAY,
            _FUTURES,
            _OPTIONS,
            _CURRENCY_FUTURES,
            _CURRENCY_OPTIONS,
            _COMMODITY_FUTURES,
            _COMMODITY_OPTIONS,
        )
    )


def goods_and_services_tax_facts() -> tuple[MarketRuleRecord, ...]:
    """18% — on the service lines only, never on STT or stamp duty.

    The exclusion is statutory rather than administrative: CGST Act s.2(52) and s.2(102)
    exclude "securities" from both the goods and the services definitions, so no CBIC circular
    names STT at all and the exemption is DERIVED. The reason the pass-through lines (exchange
    charge, SEBI fee, DP) do attract GST is that brokers do not structure them as CGST Rule 33
    pure-agent reimbursements, so they fall into value-of-supply under s.15. The Sept-2025
    GST 2.0 restructuring left financial services at 18%.
    """
    return (
        _fact(
            RuleFamily.GOODS_AND_SERVICES_TAX,
            "0.18",
            date(2017, 7, 1),
            source=(
                "CGST Act s.15 read with the 18% financial-services rate — applies to "
                "brokerage, exchange transaction charges, IPFT, SEBI turnover fee and DP "
                "charges; NOT to STT/CTT or stamp duty (s.2(52)/s.2(102) securities exclusion)"
            ),
            source_date=date(2017, 7, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
    )


def depository_participant_charge_facts() -> tuple[MarketRuleRecord, ...]:
    """Flat per DEBIT transaction, independent of quantity — in paise, not a rate.

    Selling 100 shares and selling 10,000 shares of the same scrip on the same day cost the
    same. Buy side is nil. The familiar Rs 15.34 is not a depository rate at all: it is a
    broker's bundled total (depository fee + broker markup + GST), and it is carried in
    `broker_fee_schedules`, where a change of broker moves it.
    """
    return (
        _fact(
            RuleFamily.DEPOSITORY_PARTICIPANT_CHARGE,
            "350",
            date(2024, 10, 1),
            scope=RuleScope(segment="CDSL"),
            kind=RuleValueKind.INTEGER,
            source=(
                "CDSL media release 2024-09-26, verbatim: 'Uniform tariff of Rs. 3.50 per debit "
                "transaction ... effective from October 01, 2024'"
            ),
            source_date=date(2024, 9, 26),
        ),
        _fact(
            RuleFamily.DEPOSITORY_PARTICIPANT_CHARGE,
            "325",
            date(2024, 10, 1),
            scope=RuleScope(segment="CDSL-CONCESSIONAL"),
            kind=RuleValueKind.INTEGER,
            source=(
                "CDSL media release 2024-09-26 — Rs 3.25 for a female first holder, and for "
                "mutual-fund and bond ISINs"
            ),
            source_date=date(2024, 9, 26),
        ),
        _fact(
            RuleFamily.DEPOSITORY_PARTICIPANT_CHARGE,
            "400",
            date(2024, 10, 1),
            scope=RuleScope(segment="NSDL"),
            kind=RuleValueKind.INTEGER,
            source=(
                "NSDL fee schedule (2026-07 revision) — Rs 4.00 per debit instruction, flat "
                "before and after the Oct-2024 CDSL change"
            ),
            source_date=date(2026, 7, 1),
        ),
    )


def regime_milestone_facts() -> tuple[MarketRuleRecord, ...]:
    """Dates a regime began. Carried as dates, not as the calculations they enable."""
    return (
        _fact(
            RuleFamily.MARKET_WIDE_CIRCUIT_BREAKER,
            "10/15/20 percent index-move halts",
            date(2001, 7, 2),
            kind=RuleValueKind.TEXT,
            source="SEBI market-wide circuit breaker circular, effective 2-Jul-2001",
            source_date=date(2001, 6, 28),
        ),
        _fact(
            RuleFamily.PRE_OPEN_AUCTION,
            "call auction pre-open session",
            date(2010, 10, 18),
            kind=RuleValueKind.TEXT,
            source="SEBI CIR/MRD/DP/21/2010 — pre-open call auction introduced",
            source_date=date(2010, 7, 15),
        ),
        _fact(
            RuleFamily.SPAN_MARGIN,
            "SPAN initial margin",
            date(1999, 7, 28),
            kind=RuleValueKind.TEXT,
            source="SEBI Master Circular Ch.5 rescinded-circular list — SPAN from 28-Jul-1999",
            source_date=date(1999, 7, 28),
        ),
        _fact(
            RuleFamily.VALUE_AT_RISK_MARGIN,
            "VaR + ELM cash-market margin",
            date(2001, 7, 2),
            kind=RuleValueKind.TEXT,
            source="SEBI Cir-34/01 — VaR-based margining in the cash market",
            source_date=date(2001, 7, 2),
        ),
        _fact(
            RuleFamily.PEAK_MARGIN,
            "peak margin phase-in",
            date(2020, 12, 1),
            kind=RuleValueKind.TEXT,
            source="SEBI 2020/127 — peak margin reporting, phased from 1-Dec-2020",
            source_date=date(2020, 7, 20),
        ),
        _fact(
            RuleFamily.PENALTY_FRAMEWORK,
            "short-collection penalty framework",
            date(2011, 8, 10),
            kind=RuleValueKind.TEXT,
            source="SEBI CIR/DNPD/7/2011 — penalty table for short collection",
            source_date=date(2011, 8, 10),
        ),
    )


def session_hour_facts() -> tuple[MarketRuleRecord, ...]:
    """Session hours and the pre-open window (`research/61` §2.6).

    The continuous session has been 09:15-15:30 since 1994 and `research/61` grades that a
    Grade-B **unverified negative** — an absence-of-change argument rather than a circular
    saying so. That grade is carried honestly rather than upgraded because the fact is
    convenient, and `effective_from` starts at the pre-open reform date rather than 1994,
    because that is the earliest date a source was actually read for.
    """
    return (
        _fact(
            RuleFamily.SESSION_HOURS,
            "09:15-15:30",
            date(2010, 10, 18),
            kind=RuleValueKind.TIME_RANGE,
            source=(
                "NSE continuous session 09:15-15:30, stated alongside SEBI CIR/MRD/DP/21/2010's "
                "pre-open window; secondary and argued from absence of change, not a circular"
            ),
            source_date=date(2010, 7, 15),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
        ),
        _fact(
            RuleFamily.SESSION_HOURS,
            "09:00-09:15",
            date(2010, 10, 18),
            scope=RuleScope(segment="PRE_OPEN"),
            kind=RuleValueKind.TIME_RANGE,
            source="SEBI CIR/MRD/DP/21/2010 — 15-minute pre-open call auction",
            source_date=date(2010, 7, 15),
        ),
    )


def seeded_nse_market_rule_store(
    *, observe_instrument_master: bool = True
) -> PointInTimeMarketRuleStore:
    """Every fact `research/61` established, loaded and reconciled.

    Deliberately NOT every family: the ones with no admissible source stay uncovered, and
    `coverage()` reports them as such. That report is the honest picture of what a replay
    can and cannot claim, and it is what the dashboard surface shows.
    """
    store = PointInTimeMarketRuleStore()
    store.extend(securities_transaction_tax_facts())
    store.extend(commodities_transaction_tax_facts())
    store.extend(stamp_duty_facts())
    store.extend(expiry_weekday_facts())
    store.extend(minimum_contract_value_facts())
    store.extend(exchange_transaction_charge_facts())
    store.extend(investor_protection_fund_contribution_facts())
    store.extend(sebi_turnover_fee_facts())
    store.extend(goods_and_services_tax_facts())
    store.extend(depository_participant_charge_facts())
    store.extend(regime_milestone_facts())
    store.extend(session_hour_facts())
    if observe_instrument_master:
        # Tick and lot sizes are NOT seeded from circulars, deliberately: the exchange
        # publishes them daily and an observed fact outranks a documentary one (`A.80`).
        # The observer is lazy, so registering it costs nothing until a symbol is asked for.
        from nse_algo_trader.market_rules.instrument_master_rule_observer import (
            InstrumentMasterRuleObserver,
        )

        store.register_source(InstrumentMasterRuleObserver())
    return store
