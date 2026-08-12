"""`L0.31` — what the exchange's rules WERE on a past date.

Spec: `docs/research/215`; the dated facts come from `research/61` §2.

The defect this module exists to prevent does not raise. It returns a number: today's
STT rate applied to a 2019 replay, today's expiry weekday applied to nine years that had a
different one. Every test below is a way that silent substitution can happen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EVERYTHING,
    EvidenceGrade,
    MarketRuleRecord,
    PointInTimeMarketRuleStore,
    RuleCitationError,
    RuleCoverageError,
    RuleFamily,
    RuleIntervalError,
    RuleScope,
    RuleValueKind,
)

RECORDED = date(2026, 8, 12)


def _record(
    *,
    family: RuleFamily = RuleFamily.SECURITIES_TRANSACTION_TAX,
    scope: RuleScope = EVERYTHING,
    value: str = "0.0625",
    value_kind: RuleValueKind = RuleValueKind.DECIMAL_FRACTION,
    effective_from: date = date(2023, 4, 1),
    effective_to: date | None = None,
    source_reference: str = "Finance Act 2023",
    source_date: date = date(2023, 3, 31),
    grade: EvidenceGrade = EvidenceGrade.PRIMARY_CIRCULAR,
    recorded_at: date = RECORDED,
) -> MarketRuleRecord:
    return MarketRuleRecord(
        family=family,
        scope=scope,
        value=value,
        value_kind=value_kind,
        effective_from=effective_from,
        effective_to=effective_to,
        source_reference=source_reference,
        source_date=source_date,
        grade=grade,
        recorded_at=recorded_at,
    )


# ------------------------------------------------------------------ provenance


@pytest.mark.adversarial
@pytest.mark.parametrize("blank", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_a_rule_without_a_citation_is_refused(blank: str) -> None:
    """The citation is what separates a regulatory fact from a hardcoded constant.

    `R.03` bans hardcoded values; `R.23(e)` exempts regulatory facts *sourced in a
    comment*. A rate with no source is not the exemption, it is the thing being banned.
    """
    with pytest.raises(RuleCitationError, match="source_reference"):
        _record(source_reference=blank)


@pytest.mark.adversarial
def test_an_interval_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(RuleIntervalError):
        _record(effective_from=date(2024, 1, 1), effective_to=date(2023, 1, 1))


@pytest.mark.adversarial
def test_a_zero_length_interval_is_refused() -> None:
    """`effective_to` is EXCLUSIVE, so from == to covers no day at all.

    Accepting it would put a record in the timeline that can never be resolved — a fact
    that exists, looks loaded, and answers nothing.
    """
    with pytest.raises(RuleIntervalError):
        _record(effective_from=date(2024, 1, 1), effective_to=date(2024, 1, 1))


# ------------------------------------------------------------------ resolution


@pytest.mark.unit
def test_a_date_inside_an_interval_resolves_to_that_rule() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(effective_from=date(2023, 4, 1), effective_to=date(2024, 10, 1)))
    resolution = store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1))
    assert resolution.as_decimal() == Decimal("0.0625")
    assert resolution.record.source_reference == "Finance Act 2023"


@pytest.mark.unit
def test_the_boundaries_are_half_open_first_day_in_last_day_out() -> None:
    """A rate effective 1-Apr applies ON 1-Apr; a rate superseded 1-Oct does not apply then.

    Off-by-one here is a whole day of every trade priced at the wrong rate, and it is
    invisible in aggregate.
    """
    store = PointInTimeMarketRuleStore()
    store.add(_record(effective_from=date(2023, 4, 1), effective_to=date(2024, 10, 1)))

    assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2023, 4, 1)).value == "0.0625"
    assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 9, 30)).value == "0.0625"
    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 10, 1))
    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2023, 3, 31))


@pytest.mark.unit
def test_an_open_ended_rule_covers_every_later_date() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(effective_from=date(2024, 10, 1), effective_to=None, value="0.1"))
    assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2030, 1, 1)).value == "0.1"


@pytest.mark.adversarial
def test_a_date_before_any_known_fact_refuses_rather_than_returning_the_oldest() -> None:
    """THE defect this module exists for.

    A store holding only the 2024 rate, asked about 2019, must not answer 2024's rate. The
    number would look right, the study would be wrong, and nothing would say so.
    """
    store = PointInTimeMarketRuleStore()
    store.add(_record(effective_from=date(2024, 10, 1), value="0.1"))
    with pytest.raises(RuleCoverageError, match="2019-06-01"):
        store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2019, 6, 1))


@pytest.mark.adversarial
def test_a_hole_between_intervals_refuses_rather_than_bridging_it() -> None:
    """A gap in what was compiled is not evidence the older rule persisted."""
    store = PointInTimeMarketRuleStore()
    store.add(_record(effective_from=date(2013, 6, 1), effective_to=date(2015, 1, 1), value="0.05"))
    store.add(_record(effective_from=date(2023, 4, 1), value="0.0625"))
    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2019, 6, 1))


@pytest.mark.adversarial
def test_an_uncovered_family_is_distinguishable_from_a_missing_rule() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record())
    with pytest.raises(RuleCoverageError, match="no facts"):
        store.resolve(RuleFamily.PER_STOCK_PRICE_BAND, date(2024, 1, 1))


# ---------------------------------------------------------------------- scope


@pytest.mark.unit
def test_a_more_specific_scope_wins_over_a_broader_one() -> None:
    """NIFTY's expiry weekday is not every index's expiry weekday."""
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(
            family=RuleFamily.EXPIRY_CYCLE,
            scope=EVERYTHING,
            value="THURSDAY",
            value_kind=RuleValueKind.WEEKDAY,
            effective_from=date(2019, 2, 11),
        )
    )
    store.add(
        _record(
            family=RuleFamily.EXPIRY_CYCLE,
            scope=RuleScope(index_or_underlying="BANKNIFTY"),
            value="WEDNESDAY",
            value_kind=RuleValueKind.WEEKDAY,
            effective_from=date(2023, 9, 6),
            effective_to=date(2025, 1, 2),
        )
    )
    banknifty = store.resolve(
        RuleFamily.EXPIRY_CYCLE,
        date(2023, 10, 1),
        scope=RuleScope(index_or_underlying="BANKNIFTY"),
    )
    everything_else = store.resolve(RuleFamily.EXPIRY_CYCLE, date(2023, 10, 1))
    assert banknifty.value == "WEDNESDAY"
    assert everything_else.value == "THURSDAY"


@pytest.mark.unit
def test_a_broader_rule_still_applies_where_no_specific_one_exists() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(
            family=RuleFamily.EXPIRY_CYCLE,
            scope=EVERYTHING,
            value="TUESDAY",
            value_kind=RuleValueKind.WEEKDAY,
            effective_from=date(2025, 9, 1),
        )
    )
    resolution = store.resolve(
        RuleFamily.EXPIRY_CYCLE,
        date(2025, 10, 1),
        scope=RuleScope(index_or_underlying="MIDCPNIFTY"),
    )
    assert resolution.value == "TUESDAY"
    assert resolution.record.scope == EVERYTHING, "the broad rule is what answered"


@pytest.mark.adversarial
def test_a_different_scopes_rule_never_leaks_across() -> None:
    """A BANKNIFTY rule must not answer a FINNIFTY question just by being more specific."""
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(
            family=RuleFamily.LOT_SIZE,
            scope=RuleScope(index_or_underlying="BANKNIFTY"),
            value="15",
            value_kind=RuleValueKind.INTEGER,
            effective_from=date(2024, 1, 1),
        )
    )
    with pytest.raises(RuleCoverageError):
        store.resolve(
            RuleFamily.LOT_SIZE,
            date(2024, 6, 1),
            scope=RuleScope(index_or_underlying="FINNIFTY"),
        )


# ------------------------------------------------------------------ conflicts


@pytest.mark.unit
def test_a_stronger_grade_wins_and_the_loser_is_still_reported() -> None:
    """A read circular beats a broker blog — but the disagreement is not erased."""
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(value="0.05", grade=EvidenceGrade.UNVERIFIED_SNIPPET, source_reference="forum")
    )
    store.add(_record(value="0.0625", grade=EvidenceGrade.PRIMARY_CIRCULAR))

    resolution = store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1))
    assert resolution.value == "0.0625"
    assert resolution.is_conflicted
    assert [record.value for record in resolution.superseded_records] == ["0.05"]


@pytest.mark.unit
def test_an_observed_exchange_fact_outranks_a_documentary_one() -> None:
    """What the exchange published that day beats what a circular says it should have been.

    `instrument_master` holds 227,535 dated rows of real tick and lot sizes. Where it
    covers a date it is stronger evidence than a PDF, because it is the value that was
    actually in force rather than the value that was announced.
    """
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(
            family=RuleFamily.TICK_SIZE,
            value="0.05",
            grade=EvidenceGrade.PRIMARY_CIRCULAR,
            effective_from=date(2024, 6, 10),
        )
    )
    store.add(
        _record(
            family=RuleFamily.TICK_SIZE,
            value="0.01",
            grade=EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA,
            source_reference="instrument_master ingested_on=2024-06-11",
            effective_from=date(2024, 6, 10),
        )
    )
    assert store.resolve(RuleFamily.TICK_SIZE, date(2024, 7, 1)).value == "0.01"


@pytest.mark.unit
def test_at_equal_grade_the_later_compilation_supersedes() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", recorded_at=date(2026, 1, 1)))
    store.add(_record(value="0.0625", recorded_at=date(2026, 8, 1)))
    assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1)).value == "0.0625"


@pytest.mark.adversarial
def test_a_genuine_tie_is_reported_unresolved_rather_than_picked() -> None:
    """Same grade, same recording date, different values. The store has no basis to choose.

    Choosing anyway would be the store inventing a preference and presenting it as a fact.
    """
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", source_reference="circular A"))
    store.add(_record(value="0.0625", source_reference="circular B"))

    resolution = store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1))
    assert resolution.is_unresolved, "a tie must not silently resolve"
    assert len(resolution.superseded_records) == 1
    assert store.conflicts(), "and it must appear in the conflict report"


@pytest.mark.unit
def test_the_conflict_report_names_the_overlapping_window() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", effective_from=date(2023, 1, 1), effective_to=date(2024, 1, 1)))
    store.add(
        _record(
            value="0.0625",
            grade=EvidenceGrade.SECONDARY_TRIANGULATED,
            effective_from=date(2023, 6, 1),
            effective_to=date(2024, 6, 1),
        )
    )
    conflict = store.conflicts()[0]
    assert conflict.overlap_start == date(2023, 6, 1)
    assert conflict.overlap_end == date(2024, 1, 1)


# ----------------------------------------------------------------- bitemporal


@pytest.mark.unit
def test_a_belief_date_hides_facts_learned_afterwards() -> None:
    """Reproducing a study means reproducing what it BELIEVED, not what we know now."""
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", recorded_at=date(2026, 1, 1)))
    store.add(_record(value="0.0625", recorded_at=date(2026, 8, 1)))

    now = store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1))
    then = store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1), known_as_of=date(2026, 3, 1)
    )
    assert now.value == "0.0625"
    assert then.value == "0.05", "the correction had not been made yet"


@pytest.mark.adversarial
def test_a_belief_date_before_every_fact_is_uncovered_not_empty() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(recorded_at=date(2026, 8, 1)))
    with pytest.raises(RuleCoverageError):
        store.resolve(
            RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1), known_as_of=date(2020, 1, 1)
        )


# ------------------------------------------------------------------ timeline


@pytest.mark.unit
def test_the_timeline_is_ordered_and_non_overlapping() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.1", effective_from=date(2024, 10, 1)))
    store.add(_record(value="0.05", effective_from=date(2013, 6, 1), effective_to=date(2023, 4, 1)))
    store.add(
        _record(value="0.0625", effective_from=date(2023, 4, 1), effective_to=date(2024, 10, 1))
    )

    timeline = store.timeline(RuleFamily.SECURITIES_TRANSACTION_TAX)
    assert [record.value for record in timeline] == ["0.05", "0.0625", "0.1"]
    for earlier, later in pairwise(timeline):
        assert earlier.effective_to is not None
        assert earlier.effective_to <= later.effective_from


@pytest.mark.property
@settings(max_examples=150, deadline=None)
@given(
    starts=st.lists(st.integers(2000, 2030), min_size=1, max_size=8, unique=True),
    shuffle_seed=st.integers(0, 1_000),
)
def test_insertion_order_never_changes_the_timeline(starts: list[int], shuffle_seed: int) -> None:
    """Facts are compiled out of order, over months. The answer must not depend on that."""
    records = [
        _record(value=str(year), effective_from=date(year, 1, 1), effective_to=date(year, 6, 1))
        for year in sorted(starts)
    ]
    forward = PointInTimeMarketRuleStore()
    for record in records:
        forward.add(record)
    backward = PointInTimeMarketRuleStore()
    for record in reversed(records):
        backward.add(record)
    rotated = PointInTimeMarketRuleStore()
    offset = shuffle_seed % len(records)
    for record in records[offset:] + records[:offset]:
        rotated.add(record)

    expected = [record.value for record in forward.timeline(RuleFamily.SECURITIES_TRANSACTION_TAX)]
    assert [r.value for r in backward.timeline(RuleFamily.SECURITIES_TRANSACTION_TAX)] == expected
    assert [r.value for r in rotated.timeline(RuleFamily.SECURITIES_TRANSACTION_TAX)] == expected


@pytest.mark.property
@settings(max_examples=150, deadline=None)
@given(
    start_year=st.integers(2000, 2020),
    span_days=st.integers(1, 3_000),
    probe_offset=st.integers(0, 3_000),
)
def test_every_date_inside_a_records_interval_resolves_to_it(
    start_year: int, span_days: int, probe_offset: int
) -> None:
    from datetime import timedelta

    effective_from = date(start_year, 1, 1)
    effective_to = effective_from + timedelta(days=span_days)
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="X", effective_from=effective_from, effective_to=effective_to))

    probe = effective_from + timedelta(days=probe_offset)
    if probe >= effective_to:
        with pytest.raises(RuleCoverageError):
            store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, probe)
    else:
        assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, probe).value == "X"


@pytest.mark.property
@settings(max_examples=120, deadline=None)
@given(
    spans=st.lists(
        st.tuples(st.integers(2000, 2028), st.integers(1, 2_000)),
        min_size=1,
        max_size=6,
    )
)
def test_the_timeline_agrees_with_an_independent_interval_library(
    spans: list[tuple[int, int]],
) -> None:
    """Differential test of the interval algebra against `portion` (`research/215` §6b).

    The sourcing pass found `portion` is the one maintained library whose `IntervalDict`
    reconciles half-open `date` intervals natively. It is not adopted as the solver — it
    carries no notion of evidence grade, scope specificity or belief time, which is where
    all the difficulty in this store actually lives — but the boundary-splitting half is
    exactly what it does, and a hand-rolled splitter is precisely the kind of code that is
    subtly wrong at the edges and passes its own tests.

    Later facts win here by construction (equal grade, ascending `recorded_at`), which is
    the one policy `portion`'s last-writer-wins overwrite also encodes, so the two are
    comparable.
    """
    from datetime import timedelta

    portion_module = pytest.importorskip("portion")

    store = PointInTimeMarketRuleStore()
    interval_map = portion_module.IntervalDict()
    for index, (year, span_days) in enumerate(spans):
        effective_from = date(year, 1, 1)
        effective_to = effective_from + timedelta(days=span_days)
        store.add(
            _record(
                value=f"v{index}",
                effective_from=effective_from,
                effective_to=effective_to,
                recorded_at=date(2026, 1, 1) + timedelta(days=index),
                source_reference=f"circular {index}",
            )
        )
        interval_map[portion_module.closedopen(effective_from, effective_to)] = f"v{index}"

    for segment in store.timeline(RuleFamily.SECURITIES_TRANSACTION_TAX):
        probe = segment.effective_from
        assert interval_map[probe] == segment.value, (
            f"disagreed with portion at {probe}: ours={segment.value}, "
            f"portion={interval_map[probe]}"
        )
        if segment.effective_to is not None:
            last_day = segment.effective_to - timedelta(days=1)
            assert interval_map[last_day] == segment.value


# ------------------------------------------------------------------- coverage


@pytest.mark.unit
def test_coverage_reports_the_holes_it_cannot_answer() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", effective_from=date(2013, 6, 1), effective_to=date(2015, 1, 1)))
    store.add(_record(value="0.0625", effective_from=date(2023, 4, 1)))

    coverage = store.coverage()[RuleFamily.SECURITIES_TRANSACTION_TAX]
    assert coverage.earliest == date(2013, 6, 1)
    assert coverage.holes == ((date(2015, 1, 1), date(2023, 4, 1)),)


@pytest.mark.unit
def test_coverage_lists_every_family_including_the_empty_ones() -> None:
    """A family nobody has compiled yet must be visible as uncovered, not absent.

    `R.08`: the dashboard reads this, and an absent row reads as "nothing to worry about".
    """
    store = PointInTimeMarketRuleStore()
    store.add(_record())
    coverage = store.coverage()
    assert set(coverage) == set(RuleFamily)
    assert coverage[RuleFamily.PER_STOCK_PRICE_BAND].earliest is None


@pytest.mark.unit
def test_coverage_counts_the_grades_so_weak_evidence_is_visible() -> None:
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.05", effective_from=date(2013, 6, 1), effective_to=date(2023, 4, 1)))
    store.add(
        _record(
            value="0.0625",
            effective_from=date(2023, 4, 1),
            grade=EvidenceGrade.UNVERIFIED_SNIPPET,
            source_reference="forum thread",
        )
    )
    coverage = store.coverage()[RuleFamily.SECURITIES_TRANSACTION_TAX]
    assert coverage.grade_counts[EvidenceGrade.UNVERIFIED_SNIPPET] == 1
    assert coverage.grade_counts[EvidenceGrade.PRIMARY_CIRCULAR] == 1


# --------------------------------------------------------------- typed values


@pytest.mark.unit
def test_a_value_read_as_the_wrong_kind_raises_rather_than_coercing() -> None:
    """A weekday read as a rate would produce a number, and numbers get used."""
    store = PointInTimeMarketRuleStore()
    store.add(
        _record(
            family=RuleFamily.EXPIRY_CYCLE,
            value="TUESDAY",
            value_kind=RuleValueKind.WEEKDAY,
            effective_from=date(2025, 9, 1),
        )
    )
    resolution = store.resolve(RuleFamily.EXPIRY_CYCLE, date(2025, 10, 1))
    assert resolution.as_weekday() == 1, "Monday is 0, so Tuesday is 1"
    with pytest.raises(ValueError, match="DECIMAL_FRACTION"):
        resolution.as_decimal()


@pytest.mark.unit
def test_a_rate_is_exact_decimal_never_float() -> None:
    """0.0625 as a float is not 0.0625, and a tax rate is multiplied by every trade."""
    store = PointInTimeMarketRuleStore()
    store.add(_record(value="0.0625"))
    assert store.resolve(RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1)).as_decimal() == (
        Decimal("0.0625")
    )


# ------------------------------------------------------------------ real data

_MARKET_DATA_PATH = "/home/opc/.nse_algo_trader/market_data.sqlite3"


@pytest.mark.real_data
@pytest.mark.skipif(
    not __import__("pathlib").Path(_MARKET_DATA_PATH).exists(),
    reason="the retained market-data store is not present",
)
def test_the_seeded_expiry_rule_matches_the_exchanges_own_contracts() -> None:
    """R.05 — checked against the world, not against the table that was typed in.

    `instrument_master` holds real NSE contracts with their real expiry dates. If the
    seeded expiry-weekday rule is right, every observed expiry falls on the ruled weekday
    — EXCEPT where that weekday was not a trading session, in which case NSE rolls the
    expiry back to the previous one. That exception is not a caveat added to save the
    test: it was found by running it. NIFTY's 2029-12-24 expiry is a Monday because
    2029-12-25 is Christmas.
    """
    import sqlite3
    from datetime import timedelta

    from nse_algo_trader.market_rules.nse_market_rule_history import (
        seeded_nse_market_rule_store,
    )
    from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

    connection = sqlite3.connect(f"file:{_MARKET_DATA_PATH}?mode=ro", uri=True)
    observed = [
        (name, date.fromisoformat(expiry[:10]))
        for name, expiry in connection.execute(
            "SELECT DISTINCT name, expiry FROM instrument_master "
            "WHERE name IN ('NIFTY','BANKNIFTY') AND instrument_type IN ('CE','PE') "
            "AND expiry != ''"
        )
    ]
    connection.close()
    assert len(observed) >= 20, f"only {len(observed)} real expiries — too thin to check"

    store = seeded_nse_market_rule_store()
    calendar = NseTradingSessionCalendar()
    rolled_back: list[tuple[str, date]] = []

    for name, expiry_date in observed:
        resolution = store.resolve(
            RuleFamily.EXPIRY_CYCLE,
            expiry_date,
            scope=RuleScope(index_or_underlying=name),
        )
        ruled_weekday = resolution.as_weekday()
        if expiry_date.weekday() == ruled_weekday:
            continue
        # Not the ruled weekday: the only legitimate reason is a closed exchange.
        days_early = (ruled_weekday - expiry_date.weekday()) % 7
        scheduled = expiry_date + timedelta(days=days_early)
        assert days_early <= 3, (
            f"{name} expired {expiry_date} ({expiry_date:%A}), {days_early} days before the "
            f"ruled {resolution.value} — too far to be a holiday roll-back"
        )
        if calendar.year_reliability(scheduled.year).is_reliable:
            assert not calendar.is_trading_session(scheduled), (
                f"{name} expired {expiry_date} ({expiry_date:%A}) but the rule says "
                f"{resolution.value} and {scheduled} WAS a trading session"
            )
        # else: the calendar has no holiday rules for that year and says so
        # (`year_reliability`), so it cannot adjudicate. Asserting against it anyway would
        # be trusting a source that has already declared itself unreliable — which is the
        # exact failure that module's reliability check exists to prevent. Measured: NIFTY's
        # 2029-12-24 Monday expiry is Christmas Eve, and the calendar reports 0 recognised
        # holidays for 2029 because `pandas_market_calendars` has no rules published past
        # 2026.
        rolled_back.append((name, expiry_date))

    assert len(rolled_back) <= len(observed) // 4, (
        f"{len(rolled_back)} of {len(observed)} expiries were rolled back — that is too "
        f"many to be holidays, so the seeded weekday is probably wrong"
    )


@pytest.mark.real_data
def test_the_seeded_store_refuses_the_eras_it_has_no_source_for() -> None:
    """R.05 for the ABSENCES, which are most of the history (`research/61` §2.5).

    Pre-2024 exchange charges were a turnover slab schedule that exists in no aggregated
    form anywhere. A store that answered anyway would make every pre-2024 cost model
    quietly wrong — so the refusal is the verified behaviour, not a gap in the test.
    """
    from nse_algo_trader.market_rules.nse_market_rule_history import (
        seeded_nse_market_rule_store,
    )

    store = seeded_nse_market_rule_store()
    # The charge-scope vocabulary declared in `A.90`: "NFO" alone was ambiguous
    # between options and futures, and cash needed splitting by product mode.
    segment_scope = RuleScope(segment="NFO-OPT")

    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.EXCHANGE_TRANSACTION_CHARGE, date(2019, 6, 1), scope=segment_scope)
    with pytest.raises(RuleCoverageError):
        store.resolve(
            RuleFamily.STAMP_DUTY, date(2018, 6, 1), scope=RuleScope(segment="NSE-CNC")
        )
    with pytest.raises(RuleCoverageError):
        store.resolve(RuleFamily.PER_STOCK_PRICE_BAND, date(2024, 6, 1))

    coverage = store.coverage()
    assert coverage[RuleFamily.PER_STOCK_PRICE_BAND].record_count == 0
    assert coverage[RuleFamily.TICK_SIZE].record_count == 0, (
        "tick size is deliberately unseeded until the 2003-2024 circular chain is read"
    )


@pytest.mark.real_data
def test_the_seeded_rates_are_the_ones_a_cost_model_would_use_today() -> None:
    """The specific numbers, spot-checked against `research/61` §2.5's table."""
    from decimal import Decimal

    from nse_algo_trader.market_rules.nse_market_rule_history import (
        seeded_nse_market_rule_store,
    )

    store = seeded_nse_market_rule_store()
    options = RuleScope(segment="NFO-OPT")
    futures = RuleScope(segment="NFO-FUT")

    # 0.0625% of premium in the 2023-2024 era, 0.1% after.
    assert store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1), scope=options
    ).as_decimal() == Decimal("0.000625")
    assert store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, date(2025, 1, 1), scope=options
    ).as_decimal() == Decimal("0.001")
    # The already-legislated 2026 step, which a forward-looking model must not miss.
    assert store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, date(2026, 5, 1), scope=options
    ).as_decimal() == Decimal("0.0015")
    assert store.resolve(
        RuleFamily.SECURITIES_TRANSACTION_TAX, date(2024, 1, 1), scope=futures
    ).as_decimal() == Decimal("0.000125")


@pytest.mark.real_data
def test_every_seeded_fact_carries_a_citation_and_a_source_date() -> None:
    """The condition that makes these constants legal at all (`R.23(e)`)."""
    from nse_algo_trader.market_rules.nse_market_rule_history import (
        seeded_nse_market_rule_store,
    )

    store = seeded_nse_market_rule_store()
    assert len(store) >= 25
    for record in store.records():
        assert record.source_reference.strip(), record
        assert record.source_date <= record.effective_from or record.source_date.year >= 1999
