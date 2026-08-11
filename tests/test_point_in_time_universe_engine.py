"""Tests for the point-in-time universe engine — written before the implementation.

The engine answers "what was tradeable on date D, using only what was knowable on
D". Its failure mode is survivorship bias: a universe built from today's symbol
list contains only the survivors, and every backtest run against it inherits an
edge that was never available.

The tests are built around the three real events measured in `docs/research/204`
before any code existed — `SAMMAANCAP` leaving F&O on 2026-06-30, `EXIDEIND` and
`NUVAMA` following on 2026-07-28, and `DALBHARAT` currently mid-exit — plus the
ambiguity that makes those events hard to read: 324 of 3,419 cash symbols miss a
day without being delisted, and four trading days were never collected at all.

`A.41` binds here: absence is a three-way question at minimum, and "cannot tell"
is a real answer that must survive to the caller.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.point_in_time_universe_engine import (
    AbsenceClass,
    LadderDirection,
    ObservationSource,
    PointInTimeUniverseEngine,
    UniverseObservation,
    UniverseSnapshotError,
)

RETAINED_STORE = Path("/home/opc/.nse_algo_trader/market_data.sqlite3")
FULL_LADDER = 3  # NSE's usual monthly ladder — asserted as an OUTCOME, never an input
EXIT_DATE_SAMMAANCAP = date(2026, 6, 30)
LAST_COLLECTED = date(2026, 8, 3)
CASH = ObservationSource.CASH_BHAVCOPY


def _observation(
    symbol: str,
    day: date,
    *,
    segment: str = "NFO-OPT",
    source: ObservationSource = ObservationSource.FO_BHAVCOPY,
    ladder_depth: int | None = FULL_LADDER,
    isin: str | None = None,
    furthest: date | None = None,
) -> UniverseObservation:
    return UniverseObservation(
        symbol=symbol,
        trade_date=day,
        segment=segment,
        source=source,
        expiry_ladder_depth=ladder_depth,
        isin=isin,
        furthest_expiry=furthest,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[PointInTimeUniverseEngine]:
    with PointInTimeUniverseEngine(tmp_path / "universe.sqlite3") as opened:
        yield opened


def _populate(engine: PointInTimeUniverseEngine, days: list[date], symbols: list[str]) -> None:
    engine.ingest([_observation(s, d) for d in days for s in symbols])


# --------------------------------------------------------------------------- observation


@pytest.mark.unit
def test_a_symbol_present_in_a_file_traded_that_day(engine: PointInTimeUniverseEngine) -> None:
    day = date(2026, 6, 29)
    engine.ingest([_observation("RELIANCE", day)])
    assert engine.traded_on("RELIANCE", day)


@pytest.mark.unit
def test_a_symbol_absent_from_a_collected_file_did_not_trade(
    engine: PointInTimeUniverseEngine,
) -> None:
    day = date(2026, 6, 29)
    engine.ingest([_observation("RELIANCE", day)])
    assert not engine.traded_on("INFY", day)


@pytest.mark.adversarial
def test_traded_on_is_not_asked_about_an_uncollected_date(
    engine: PointInTimeUniverseEngine,
) -> None:
    """`traded_on` is a fact about a file. With no file there is no fact.

    Returning False would be indistinguishable from "did not trade", which is the
    exact conflation this engine exists to prevent.
    """
    engine.ingest([_observation("RELIANCE", date(2026, 6, 29))])
    with pytest.raises(UniverseSnapshotError, match="not collected"):
        engine.traded_on("RELIANCE", date(2026, 6, 30))


@pytest.mark.unit
def test_ingesting_the_same_observation_twice_is_idempotent(
    engine: PointInTimeUniverseEngine,
) -> None:
    day = date(2026, 6, 29)
    engine.ingest([_observation("RELIANCE", day)])
    engine.ingest([_observation("RELIANCE", day)])
    assert len(engine.snapshot_as_of(day).members) == 1


# --------------------------------------------------------------------------- absence


@pytest.mark.adversarial
def test_an_uncollected_date_is_unobserved_not_absent(
    engine: PointInTimeUniverseEngine,
) -> None:
    """The four-day July outage must never read as 216 simultaneous delistings."""
    collected = [date(2026, 6, 29), date(2026, 7, 1)]
    _populate(engine, collected, ["RELIANCE", "INFY"])
    verdict = engine.classify_absence("RELIANCE", date(2026, 6, 30))
    assert verdict.classification is AbsenceClass.UNOBSERVED
    assert "no file" in verdict.evidence_summary().lower()


@pytest.mark.adversarial
def test_an_illiquid_symbol_missing_one_day_is_not_traded_not_delisted(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Measured: 324 of 3,419 cash symbols miss a day, mostly G-Secs.

    A classifier that inferred delisting from a single gap would delist most of
    the government-securities universe inside a week.
    """
    days = [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]
    engine.ingest(
        [
            _observation("694GS2036", days[0], segment="CASH", source=CASH),
            _observation("694GS2036", days[2], segment="CASH", source=CASH),
            *[_observation("RELIANCE", d, segment="CASH", source=CASH) for d in days],
        ]
    )
    verdict = engine.classify_absence("694GS2036", days[1])
    assert verdict.classification is AbsenceClass.NOT_TRADED


@pytest.mark.adversarial
def test_a_symbol_that_never_returns_without_ladder_evidence_is_unknown(
    engine: PointInTimeUniverseEngine,
) -> None:
    """`UNKNOWN` is a first-class outcome (`O.22`, `A.41`).

    A symbol vanishing at the end of the window with a full ladder and no exit
    evidence is genuinely undecidable: it may have exited, or the collection may
    simply stop there. The engine must say so.
    """
    days = [date(2026, 6, 29), date(2026, 7, 1), date(2026, 7, 2)]
    engine.ingest(
        [_observation("MYSTERY", days[0]), _observation("MYSTERY", days[1]),
         *[_observation("RELIANCE", d) for d in days]]
    )
    verdict = engine.classify_absence("MYSTERY", days[2])
    assert verdict.classification is AbsenceClass.UNKNOWN


@pytest.mark.unit
def test_a_rename_is_detected_by_a_shared_isin(engine: PointInTimeUniverseEngine) -> None:
    """A disappearance coincident with an appearance carrying the same ISIN."""
    before, after = date(2026, 6, 29), date(2026, 7, 1)
    engine.ingest(
        [
            _observation("OLDNAME", before, isin="INE001A01036"),
            _observation("NEWNAME", after, isin="INE001A01036"),
            _observation("RELIANCE", before), _observation("RELIANCE", after),
        ]
    )
    verdict = engine.classify_absence("OLDNAME", after)
    assert verdict.classification is AbsenceClass.RENAMED
    assert "NEWNAME" in verdict.evidence_summary()


# --------------------------------------------------------------------- expiry ladder


@pytest.mark.unit
def test_the_ladder_norm_is_the_cross_sectional_mode_of_that_date(
    engine: PointInTimeUniverseEngine,
) -> None:
    """R.03e — the norm is measured from the file, never written down.

    Deliberately built with a norm of 4 rather than NSE's usual 3, so a
    hardcoded 3 fails.
    """
    day = date(2026, 6, 29)
    engine.ingest(
        [_observation(f"SYM{i}", day, ladder_depth=4) for i in range(9)]
        + [_observation("SHRINKING", day, ladder_depth=1)]
    )
    assert engine.ladder_norm_on(day) == 4


@pytest.mark.unit
def test_a_truncated_ladder_is_flagged_against_that_norm(
    engine: PointInTimeUniverseEngine,
) -> None:
    day = date(2026, 6, 29)
    engine.ingest(
        [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(9)]
        + [_observation("LEAVING", day, ladder_depth=1)]
    )
    truncated = {t.symbol for t in engine.exit_warnings_on(day)}
    assert truncated == {"LEAVING"}


@pytest.mark.adversarial
def test_a_full_ladder_is_never_flagged(engine: PointInTimeUniverseEngine) -> None:
    """Kills the mutant that flags everything and looks prescient."""
    day = date(2026, 6, 29)
    engine.ingest([_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(10)])
    assert engine.exit_warnings_on(day) == ()


@pytest.mark.unit
def test_an_exit_after_ladder_runoff_is_classified_with_its_evidence(
    engine: PointInTimeUniverseEngine,
) -> None:
    days = [date(2026, 6, 29), date(2026, 7, 1)]
    engine.ingest(
        [_observation("LEAVING", days[0], ladder_depth=1)]
        + [_observation(f"SYM{i}", d, ladder_depth=FULL_LADDER) for d in days for i in range(9)]
    )
    verdict = engine.classify_absence("LEAVING", days[1])
    assert verdict.classification is AbsenceClass.EXITED_DERIVATIVES
    assert "ladder" in verdict.evidence_summary().lower()


@pytest.mark.unit
def test_observations_without_a_ladder_depth_do_not_break_the_norm(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Cash observations carry no ladder; they must not be counted as depth zero."""
    day = date(2026, 7, 22)
    engine.ingest(
        [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(5)]
        + [_observation(f"CASH{i}", day, segment="CASH",
                        source=ObservationSource.CASH_BHAVCOPY, ladder_depth=None)
           for i in range(20)]
    )
    assert engine.ladder_norm_on(day) == FULL_LADDER


# --------------------------------------------------------------------------- snapshots


@pytest.mark.unit
def test_a_snapshot_holds_exactly_the_symbols_observed_that_day(
    engine: PointInTimeUniverseEngine,
) -> None:
    day = date(2026, 6, 29)
    _populate(engine, [day], ["RELIANCE", "INFY", "TCS"])
    assert engine.snapshot_as_of(day).members == frozenset({"RELIANCE", "INFY", "TCS"})


@pytest.mark.adversarial
def test_a_snapshot_of_an_uncollected_date_is_refused(
    engine: PointInTimeUniverseEngine,
) -> None:
    _populate(engine, [date(2026, 6, 29)], ["RELIANCE"])
    with pytest.raises(UniverseSnapshotError, match="not collected"):
        engine.snapshot_as_of(date(2026, 6, 30))


@pytest.mark.adversarial
def test_a_later_belief_never_mutates_an_earlier_snapshot(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Bitemporality — what we believe NOW about D versus what we believed ON D.

    A delisting learned three weeks late must not appear in a snapshot dated
    before we learned it.
    """
    day = date(2026, 6, 29)
    _populate(engine, [day], ["RELIANCE"])
    early = engine.snapshot_as_of(day, known_as_of=date(2026, 6, 29))
    engine.ingest([_observation("LATEARRIVAL", day)], known_as_of=date(2026, 7, 15))
    assert engine.snapshot_as_of(day, known_as_of=date(2026, 6, 29)).members == early.members
    assert "LATEARRIVAL" in engine.snapshot_as_of(day, known_as_of=date(2026, 7, 15)).members


@pytest.mark.adversarial
def test_a_snapshot_is_frozen(engine: PointInTimeUniverseEngine) -> None:
    day = date(2026, 6, 29)
    _populate(engine, [day], ["RELIANCE"])
    snapshot = engine.snapshot_as_of(day)
    with pytest.raises((AttributeError, TypeError)):
        snapshot.members = frozenset()  # type: ignore[misc]


@pytest.mark.unit
def test_a_snapshot_reports_its_unresolved_count(engine: PointInTimeUniverseEngine) -> None:
    """A caller must be able to refuse a snapshot it cannot trust (`O.22`)."""
    days = [date(2026, 6, 29), date(2026, 7, 1)]
    engine.ingest(
        [_observation("MYSTERY", days[0])] + [_observation("RELIANCE", d) for d in days]
    )
    snapshot = engine.snapshot_as_of(days[1])
    assert snapshot.unknown_count >= 1
    assert not snapshot.is_fully_resolved


@pytest.mark.property
@pytest.mark.parametrize("absent_count", [0, 1, 3, 7])
def test_every_prior_member_is_classified_exactly_once(
    engine: PointInTimeUniverseEngine, absent_count: int
) -> None:
    """The partition invariant. A symbol falling through is silently unexamined."""
    first, second = date(2026, 6, 29), date(2026, 7, 1)
    symbols = [f"SYM{i}" for i in range(10)]
    engine.ingest([_observation(s, first) for s in symbols])
    survivors = symbols[absent_count:]
    engine.ingest([_observation(s, second) for s in survivors])

    snapshot = engine.snapshot_as_of(second)
    classified = {verdict.symbol for verdict in snapshot.absences}
    assert classified == set(symbols) - set(survivors)
    assert len(classified) == absent_count
    assert not (classified & snapshot.members)


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_the_real_sammaancap_exit_survives_reconstruction(tmp_path: Path) -> None:
    """R.05 — the survivorship test, on a real F&O exit.

    `SAMMAANCAP` traded until 2026-06-30 and never appeared again. A universe
    built from the current symbol list would place it nowhere; a point-in-time
    universe must place it in June and not in July.
    """
    engine = _engine_from_retained_fo(tmp_path)
    assert "SAMMAANCAP" in engine.snapshot_as_of(date(2026, 6, 29)).members
    assert "SAMMAANCAP" not in engine.snapshot_as_of(date(2026, 7, 1)).members
    verdict = engine.classify_absence("SAMMAANCAP", date(2026, 7, 1))
    assert verdict.classification is AbsenceClass.EXITED_DERIVATIVES
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_the_real_ladder_norm_is_three_and_is_measured(tmp_path: Path) -> None:
    """The norm comes out of the data. Asserting it as an OUTCOME, not a constant."""
    engine = _engine_from_retained_fo(tmp_path)
    assert engine.ladder_norm_on(date(2026, 7, 27)) == FULL_LADDER
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_the_real_pending_exits_are_flagged_with_lead_time(tmp_path: Path) -> None:
    """`EXIDEIND` and `NUVAMA` on their last day; `DALBHARAT` still mid-exit."""
    engine = _engine_from_retained_fo(tmp_path)
    on_last_full_day = {t.symbol for t in engine.exit_warnings_on(date(2026, 7, 27))}
    assert {"EXIDEIND", "NUVAMA"} <= on_last_full_day
    still_leaving = {t.symbol for t in engine.exit_warnings_on(LAST_COLLECTED)}
    assert "DALBHARAT" in still_leaving
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_the_real_universe_is_stable_across_the_window(tmp_path: Path) -> None:
    """216 underlyings, 213 on every date. The engine must not invent churn."""
    engine = _engine_from_retained_fo(tmp_path)
    sizes = [
        len(engine.snapshot_as_of(day).members) for day in engine.collected_dates()
    ]
    assert min(sizes) >= 200
    assert max(sizes) - min(sizes) <= 5
    engine.close()


def _engine_from_retained_fo(tmp_path: Path) -> PointInTimeUniverseEngine:
    connection = sqlite3.connect(f"file:{RETAINED_STORE}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT trade_date, underlying_symbol, COUNT(DISTINCT expiry_date), MAX(expiry_date)"
        " FROM fo_bhavcopy_contracts WHERE contract_type LIKE 'ST%'"
        " GROUP BY trade_date, underlying_symbol"
    ).fetchall()
    connection.close()
    engine = PointInTimeUniverseEngine(tmp_path / "real_universe.sqlite3")
    engine.ingest(
        [
            UniverseObservation(
                symbol=symbol,
                trade_date=date.fromisoformat(stamp),
                segment="NFO-STK",
                source=ObservationSource.FO_BHAVCOPY,
                expiry_ladder_depth=depth,
                isin=None,
                furthest_expiry=date.fromisoformat(furthest),
            )
            for stamp, symbol, depth, furthest in rows
        ]
    )
    return engine


# --------------------------------------------------------------- review corrections
# Added after an adversarial review found 13 defects and 4 real surviving mutants.
# Each test names the failure it pins.


@pytest.mark.adversarial
def test_a_new_listing_ramping_up_is_not_an_exit_warning(
    engine: PointInTimeUniverseEngine,
) -> None:
    """The confirmed false positive: ramp-up and run-off look identical by depth.

    A newly listed underlying climbing 1 -> 2 -> 3 was flagged with exactly the
    confidence `EXIDEIND` was, and would have been barred from new multi-expiry
    positions on the day it launched. The furthest expiry separates them: a ramping
    listing pushes its horizon out, a running-off one does not.
    """
    days = [date(2026, 6, 29), date(2026, 7, 1), date(2026, 7, 2)]
    horizons = [date(2026, 7, 28), date(2026, 8, 25), date(2026, 9, 29)]
    engine.ingest(
        [
            _observation("NEWLISTING", day, ladder_depth=depth, furthest=horizon)
            for day, depth, horizon in zip(days, [1, 2, 3], horizons, strict=True)
        ]
        + [
            _observation(f"SYM{i}", day, ladder_depth=FULL_LADDER, furthest=date(2026, 9, 29))
            for day in days for i in range(9)
        ]
    )
    on_second_day = {t.symbol: t.direction for t in engine.ladder_truncations_on(days[1])}
    assert on_second_day["NEWLISTING"] is LadderDirection.RAMPING_UP
    assert "NEWLISTING" not in {t.symbol for t in engine.exit_warnings_on(days[1])}


@pytest.mark.adversarial
def test_a_running_off_ladder_is_still_an_exit_warning(
    engine: PointInTimeUniverseEngine,
) -> None:
    """The other half — the discriminator must not simply suppress everything."""
    days = [date(2026, 6, 29), date(2026, 7, 1)]
    engine.ingest(
        [
            _observation("LEAVING", days[0], ladder_depth=2, furthest=date(2026, 7, 28)),
            _observation("LEAVING", days[1], ladder_depth=1, furthest=date(2026, 7, 28)),
        ]
        + [
            _observation(f"SYM{i}", day, ladder_depth=FULL_LADDER, furthest=date(2026, 9, 29))
            for day in days for i in range(9)
        ]
    )
    warned = {t.symbol for t in engine.exit_warnings_on(days[1])}
    assert warned == {"LEAVING"}


@pytest.mark.adversarial
def test_a_first_sighting_with_a_short_ladder_still_warns(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Left-censoring is the real case, not a corner one.

    `EXIDEIND` and `NUVAMA` were already truncated on the earliest file held, so
    treating "no prior observation" as safe would suppress the warning in exactly
    the situation the retained data proves happens.
    """
    day = date(2026, 6, 29)
    engine.ingest(
        [_observation("ALREADY_LEAVING", day, ladder_depth=1, furthest=date(2026, 7, 28))]
        + [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(9)]
    )
    warning = engine.exit_warnings_on(day)
    assert [t.symbol for t in warning] == ["ALREADY_LEAVING"]
    assert warning[0].direction is LadderDirection.INDETERMINATE


@pytest.mark.adversarial
def test_a_later_belief_supersedes_an_earlier_one_rather_than_aggregating(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Storage keeps every belief; a read must resolve to exactly one.

    Reproduced: a stale depth-3 row masked a corrected depth-1 revision because
    `MAX(expiry_ladder_depth)` aggregated across beliefs, so a genuine
    later-learned truncation disappeared.
    """
    day = date(2026, 6, 29)
    later = date(2026, 7, 15)
    engine.ingest(
        [_observation("TARGET", day, ladder_depth=FULL_LADDER, furthest=date(2026, 9, 29))]
        + [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(9)]
    )
    engine.ingest(
        [_observation("TARGET", day, ladder_depth=1, furthest=date(2026, 7, 28))],
        known_as_of=later,
    )
    assert engine.exit_warnings_on(day, known_as_of=day) == ()
    assert [t.symbol for t in engine.exit_warnings_on(day, known_as_of=later)] == ["TARGET"]


@pytest.mark.adversarial
def test_a_symbol_absent_for_several_dates_stays_classified(
    engine: PointInTimeUniverseEngine,
) -> None:
    """The silent hole: absences were diffed against the previous date only.

    A symbol missing on two consecutive dates dropped out of `absences` after the
    first, and `is_fully_resolved` reported True while its fate was unaccounted
    for — a confident wrong answer by omission.
    """
    days = [date(2026, 6, 29), date(2026, 7, 1), date(2026, 7, 2)]
    engine.ingest([_observation("GONE", days[0]), *[_observation("RELIANCE", d) for d in days]])
    third = engine.snapshot_as_of(days[2])
    assert "GONE" in {verdict.symbol for verdict in third.absences}
    assert not third.is_fully_resolved


@pytest.mark.adversarial
def test_an_extraordinarily_long_absence_is_not_called_ordinary_illiquidity(
    engine: PointInTimeUniverseEngine,
) -> None:
    """`NOT_TRADED` needs a density bound, derived from the universe's own gaps.

    Reproduced: a symbol absent 44 consecutive sessions then relisted was called
    ordinary illiquidity with full confidence. The ceiling is Tukey's upper fence
    over observed gap lengths (R.03), not a declared number of days.
    """
    days = [date(2026, 6, 1) + timedelta(days=index) for index in range(40)]
    engine.ingest([_observation("RELIANCE", day) for day in days])
    # A universe of ordinary one-day gaps, so the fence is tight.
    engine.ingest([_observation("BLINKER", day) for day in days[::2]])
    engine.ingest([_observation("VANISHED", days[0]), _observation("VANISHED", days[-1])])
    verdict = engine.classify_absence("VANISHED", days[20])
    assert verdict.classification is AbsenceClass.UNKNOWN
    assert "ordinary_gap_ceiling" in verdict.evidence_summary()


@pytest.mark.adversarial
@pytest.mark.parametrize("deep_first", [True, False])
def test_the_ladder_norm_tie_break_is_deterministic(tmp_path: Path, *, deep_first: bool) -> None:
    """A 5-5 tie previously flipped with ingest order, via SQLite's row order.

    A flipped norm flips the truncation set for every symbol on the losing side.
    Ties now break toward the deeper ladder, which is also conservative: it can
    only produce more warnings, never fewer.
    """
    day = date(2026, 6, 29)
    shallow = [_observation(f"TWO{i}", day, ladder_depth=2) for i in range(5)]
    deep = [_observation(f"THREE{i}", day, ladder_depth=3) for i in range(5)]
    with PointInTimeUniverseEngine(tmp_path / f"tie_{deep_first}.sqlite3") as engine:
        engine.ingest(deep + shallow if deep_first else shallow + deep)
        assert engine.ladder_norm_on(day) == FULL_LADDER


@pytest.mark.adversarial
def test_an_isin_shared_by_several_symbols_is_not_called_a_rename(
    engine: PointInTimeUniverseEngine,
) -> None:
    """A duplicate ISIN is a data-quality artefact, not a rename.

    Reproduced: `fetchone()` picked one candidate arbitrarily, discarded the rest,
    and never disclosed that the choice was ambiguous.
    """
    before, after = date(2026, 6, 29), date(2026, 7, 1)
    engine.ingest(
        [
            _observation("ALPHACORP", before, isin="INE000A00001"),
            _observation("BETACORP", after, isin="INE000A00001"),
            _observation("GAMMACO", after, isin="INE000A00001"),
            _observation("RELIANCE", before), _observation("RELIANCE", after),
        ]
    )
    verdict = engine.classify_absence("ALPHACORP", after)
    assert verdict.classification is AbsenceClass.UNKNOWN
    assert "BETACORP" in verdict.evidence_summary()
    assert "GAMMACO" in verdict.evidence_summary()


@pytest.mark.unit
def test_the_ladder_norm_is_undefined_when_nothing_carries_a_ladder(
    engine: PointInTimeUniverseEngine,
) -> None:
    """`None`, not 0 — a mode over nothing is undefined (the docstring's own claim)."""
    day = date(2026, 7, 22)
    engine.ingest(
        [_observation(f"CASH{i}", day, segment="CASH", source=CASH, ladder_depth=None)
         for i in range(5)]
    )
    assert engine.ladder_norm_on(day) is None
    assert engine.ladder_truncations_on(day) == ()


@pytest.mark.unit
def test_missing_expiries_counts_the_shortfall(engine: PointInTimeUniverseEngine) -> None:
    day = date(2026, 6, 29)
    engine.ingest(
        [_observation("LEAVING", day, ladder_depth=1)]
        + [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(9)]
    )
    assert engine.ladder_truncations_on(day)[0].missing_expiries == FULL_LADDER - 1


@pytest.mark.unit
def test_cash_symbols_never_enter_the_truncation_set(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Without the ladder filter, cash rows would be admitted at depth NULL."""
    day = date(2026, 7, 22)
    engine.ingest(
        [_observation(f"SYM{i}", day, ladder_depth=FULL_LADDER) for i in range(5)]
        + [_observation(f"CASH{i}", day, segment="CASH", source=CASH, ladder_depth=None)
           for i in range(20)]
    )
    assert all(not t.symbol.startswith("CASH") for t in engine.ladder_truncations_on(day))


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_the_real_cash_universe_illiquidity_claim(tmp_path: Path) -> None:
    """R.05 against the REAL cash table, not a fabricated stand-in.

    The "324 of 3,419 miss a day, mostly G-Secs" figure was cited in prose by the
    spec, this module and a test, while every real-data test read only the F&O
    table. Verified here against `cash_bhavcopy_delivery` itself.
    """
    connection = sqlite3.connect(f"file:{RETAINED_STORE}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT trade_date, symbol FROM cash_bhavcopy_delivery"
    ).fetchall()
    connection.close()
    with PointInTimeUniverseEngine(tmp_path / "cash.sqlite3") as engine:
        engine.ingest(
            [
                UniverseObservation(
                    symbol=symbol, trade_date=date.fromisoformat(stamp), segment="CASH",
                    source=CASH, expiry_ladder_depth=None, isin=None,
                )
                for stamp, symbol in rows
            ]
        )
        collected = engine.collected_dates()
        everywhere = set.intersection(
            *[set(engine.snapshot_as_of(day).members) for day in collected]
        )
        total = {symbol for _, symbol in rows}
        assert len(total) == 3419
        assert len(total) - len(everywhere) == 324
        # No ladder anywhere in cash, so nothing may be called an exit.
        assert engine.exit_warnings_on(collected[-1]) == ()


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_real_isins_from_the_mwpl_table_are_one_to_one(tmp_path: Path) -> None:
    """The rename classifier had only synthetic coverage; F&O bhavcopy has no ISIN.

    `mwpl_position_limits` does. Measured 1:1 over 210 underlyings, so no real
    symbol may be reported as renamed from this source — which is itself the
    assertion worth making.
    """
    connection = sqlite3.connect(f"file:{RETAINED_STORE}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT trade_date, underlying_symbol, isin FROM mwpl_position_limits"
    ).fetchall()
    connection.close()
    with PointInTimeUniverseEngine(tmp_path / "mwpl.sqlite3") as engine:
        engine.ingest(
            [
                UniverseObservation(
                    symbol=symbol, trade_date=date.fromisoformat(stamp), segment="NFO-STK",
                    source=ObservationSource.MWPL, expiry_ladder_depth=None, isin=isin,
                )
                for stamp, symbol, isin in rows
            ]
        )
        collected = engine.collected_dates()
        snapshot = engine.snapshot_as_of(collected[-1])
        renamed = [
            verdict for verdict in snapshot.absences
            if verdict.classification is AbsenceClass.RENAMED
        ]
        assert renamed == []


@pytest.mark.real_data
@pytest.mark.skipif(not RETAINED_STORE.exists(), reason="retained market-data store absent")
def test_a_full_universe_snapshot_is_not_quadratic(tmp_path: Path) -> None:
    """Measured at 10.78 s for 2,500 departed symbols before the context rewrite.

    The ladder norm and truncation set are identical for every symbol on a date and
    were being re-derived from SQL once per departed symbol.
    """
    import time

    engine = _engine_from_retained_fo(tmp_path)
    collected = engine.collected_dates()
    started = time.monotonic()
    snapshot = engine.snapshot_as_of(collected[-1])
    elapsed = time.monotonic() - started
    engine.close()
    assert snapshot.members
    assert elapsed < 5.0, f"snapshot_as_of took {elapsed:.2f}s"


@pytest.mark.adversarial
@pytest.mark.parametrize("deep_first", [True, False])
def test_the_truncation_set_uses_the_same_tie_break_as_the_norm(
    tmp_path: Path, *, deep_first: bool
) -> None:
    """The tie-break existed twice and only one copy was covered.

    Mutating the uncovered copy left every test green while the truncation set
    silently used a different norm from the one `ladder_norm_on` reports.
    """
    day = date(2026, 6, 29)
    shallow = [_observation(f"TWO{i}", day, ladder_depth=2) for i in range(5)]
    deep = [_observation(f"THREE{i}", day, ladder_depth=3) for i in range(5)]
    with PointInTimeUniverseEngine(tmp_path / f"tie2_{deep_first}.sqlite3") as engine:
        engine.ingest(deep + shallow if deep_first else shallow + deep)
        norm = engine.ladder_norm_on(day)
        truncated = engine.ladder_truncations_on(day)
        assert norm == FULL_LADDER
        assert {t.symbol for t in truncated} == {f"TWO{i}" for i in range(5)}
        assert all(t.cross_sectional_norm == norm for t in truncated)


@pytest.mark.adversarial
def test_a_never_observed_symbol_is_unknown_not_absent(
    engine: PointInTimeUniverseEngine,
) -> None:
    """Asking about a symbol the engine has never seen is not evidence of anything.

    Mutation found this branch entirely uncovered: flipping it to `NOT_TRADED`
    left every test green, which would report a typo'd or out-of-universe symbol
    as a healthy listed instrument that simply had a quiet day.
    """
    day = date(2026, 6, 29)
    engine.ingest([_observation("RELIANCE", day)])
    verdict = engine.classify_absence("NEVER_HEARD_OF_IT", day)
    assert verdict.classification is AbsenceClass.UNKNOWN
    assert "never been observed" in verdict.evidence_summary()
