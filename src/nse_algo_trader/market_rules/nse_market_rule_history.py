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

_OPTIONS = RuleScope(segment="NFO", index_or_underlying=None, symbol=None)
_FUTURES = RuleScope(segment="NFO-FUT")
_CASH = RuleScope(segment="NSE")


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
    """STT on F&O, the era most relevant to any near-term replay (`research/61` §2.5).

    Options are taxed on PREMIUM, futures on turnover — a difference big enough that
    applying one rate to the other is not a rounding error. The two are separate scopes
    precisely so a caller cannot pick up the wrong one by asking loosely.
    """
    return (
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
    )


def stamp_duty_facts() -> tuple[MarketRuleRecord, ...]:
    """Uniform national rates from 1-Jul-2020. Before that: no aggregation exists."""
    return (
        _fact(
            RuleFamily.STAMP_DUTY,
            "0.00015",
            date(2020, 7, 1),
            scope=_CASH,
            source=(
                "Finance Act 2019 amendments to the Indian Stamp Act — delivery 0.015%, buy side"
            ),
            source_date=date(2020, 7, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
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


def contract_value_and_charge_facts() -> tuple[MarketRuleRecord, ...]:
    """Minimum contract value, and the ONLY exchange-charge era that can be stated flatly."""
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
        _fact(
            RuleFamily.EXCHANGE_TRANSACTION_CHARGE,
            "0.0000297",
            date(2024, 10, 1),
            scope=_OPTIONS,
            source=(
                "SEBI/HO/MRD/TPD-1/P/CIR/2024/92 'True to Label' — uniform flat rate replaces "
                "the turnover slabs; options 0.00297% of premium. Nothing BEFORE this date is "
                "seeded: the slab schedules 2004-2024 are not aggregated anywhere"
            ),
            source_date=date(2024, 7, 1),
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
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


def seeded_nse_market_rule_store() -> PointInTimeMarketRuleStore:
    """Every fact `research/61` established, loaded and reconciled.

    Deliberately NOT every family: the ones with no admissible source stay uncovered, and
    `coverage()` reports them as such. That report is the honest picture of what a replay
    can and cannot claim, and it is what the dashboard surface shows.
    """
    store = PointInTimeMarketRuleStore()
    store.extend(securities_transaction_tax_facts())
    store.extend(stamp_duty_facts())
    store.extend(expiry_weekday_facts())
    store.extend(contract_value_and_charge_facts())
    store.extend(regime_milestone_facts())
    return store
