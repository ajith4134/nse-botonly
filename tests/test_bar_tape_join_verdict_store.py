"""`M14` — the verdict store, and the distinction it exists to preserve.

The test that carries the design: `test_an_unrun_session_is_unrun_not_clean`. Everything else
here is round-tripping; that one is the `A.41` three-way partition surviving persistence.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from nse_algo_trader.market_depth.bar_tape_join_verdict_store import (
    instruments_not_cleared_for,
    price_basis_divergences_for,
    refuted_instruments_for,
    verification_coverage_for,
    verified_session_dates,
    write_session_verdicts,
)
from nse_algo_trader.market_depth.bar_tape_join_verification_engine import (
    DisagreementNullModel,
    DisagreementShape,
    InstrumentJoinReport,
    InstrumentJoinVerdict,
    SessionJoinVerificationReport,
)
from nse_algo_trader.paper_loop.paper_trading_session_runner import PaperInstrument

SESSION = date(2026, 8, 12)


def instrument(
    token: int,
    verdict: InstrumentJoinVerdict,
    *,
    verifiable: int = 40,
    disagreements: int = 0,
    shape: DisagreementShape = DisagreementShape.NOT_APPLICABLE,
    basis_ratio: float | None = None,
) -> InstrumentJoinReport:
    return InstrumentJoinReport(
        instrument_token=token,
        verdict=verdict,
        bars_total=50,
        comparisons_verifiable=verifiable,
        disagreements=disagreements,
        disagreement_shape=shape,
        price_basis_ratio=basis_ratio,
        null_disagreement_rate=0.01,
        null_intra_instrument_correlation=0.0,
        minimum_comparisons_to_reject=2,
        disagreement_upper_tail=None if verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE else 0.5,
        median_deviation_paise=0.0,
        median_spread_paise=200.0,
        class_counts={},
        volume_class_counts={},
    )


def report(
    *instruments: InstrumentJoinReport,
    null_model: DisagreementNullModel = DisagreementNullModel.BETA_BINOMIAL_LEAVE_ONE_OUT,
) -> SessionJoinVerificationReport:
    return SessionJoinVerificationReport(
        session_date=SESSION,
        significance=0.01,
        null_model=null_model,
        instruments=instruments,
        pooled_disagreement_rate=0.01,
        pooled_intra_instrument_correlation=0.0,
        minimum_comparisons_to_reject=2,
        minimum_detectable_disagreement_rate=0.5,
        minimum_comparisons_to_verify=22,
        bars_total=50 * len(instruments),
        comparisons_verifiable=40 * len(instruments),
        disagreements_total=sum(item.disagreements for item in instruments),
        class_counts={},
        volume_class_counts={},
        deviation_in_tolerances_deciles=(),
    )


def test_an_unrun_session_is_unrun_not_clean(tmp_path: Path) -> None:
    """`refuted_instruments_for` returns nothing for an unrun session, so it cannot be the thing
    that says whether a session was checked. `verification_coverage_for` is."""
    store = tmp_path / "verdicts.sqlite3"
    assert refuted_instruments_for(SESSION, store) == frozenset()
    assert verification_coverage_for(SESSION, store) is None

    write_session_verdicts(report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED)), store)
    assert refuted_instruments_for(SESSION, store) == frozenset()
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None, "a run session must be distinguishable from an unrun one"
    assert coverage.instruments_verified == 1


def test_the_refusals_round_trip_in_the_shape_the_replay_engine_takes(tmp_path: Path) -> None:
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(
        report(
            instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED),
            instrument(2, InstrumentJoinVerdict.JOIN_REFUTED, disagreements=30),
            instrument(3, InstrumentJoinVerdict.JOIN_UNVERIFIABLE, verifiable=1),
            instrument(4, InstrumentJoinVerdict.JOIN_REFUTED, disagreements=25),
        ),
        store,
    )
    refused = refuted_instruments_for(SESSION, store)
    assert refused == frozenset({2, 4})
    assert isinstance(refused, frozenset), "the replay engine takes a frozenset, not a list"

    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert (coverage.instruments_verified, coverage.instruments_refuted) == (1, 2)
    assert coverage.instruments_unverifiable == 1
    assert coverage.instruments_total == 4
    # 40 + 40 + 1 + 40 comparable bars out of 4 x 50 — the unverifiable instrument contributes
    # its ONE comparable bar, not a whole instrument's worth.
    assert coverage.verifiable_bar_fraction == 121 / 200


def test_a_rerun_restates_rather_than_duplicates(tmp_path: Path) -> None:
    """A stale verdict outliving its re-run would refuse instruments on evidence nobody can
    reproduce."""
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(report(instrument(7, InstrumentJoinVerdict.JOIN_REFUTED)), store)
    assert refuted_instruments_for(SESSION, store) == frozenset({7})

    write_session_verdicts(report(instrument(7, InstrumentJoinVerdict.JOIN_VERIFIED)), store)
    assert refuted_instruments_for(SESSION, store) == frozenset()
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.instruments_total == 1, "the re-run replaced the verdict rather than adding one"


def test_sessions_are_listed_oldest_first(tmp_path: Path) -> None:
    store = tmp_path / "verdicts.sqlite3"
    assert verified_session_dates(store) == ()
    for day in (13, 11, 12):
        written = report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED))
        object.__setattr__(written, "session_date", date(2026, 8, day))
        write_session_verdicts(written, store)
    assert verified_session_dates(store) == (
        date(2026, 8, 11),
        date(2026, 8, 12),
        date(2026, 8, 13),
    )


def test_a_session_with_no_bar_at_all_reports_no_fraction(tmp_path: Path) -> None:
    """`None` rather than zero: a session that compared nothing has no comparable-bar rate, and a
    zero would read as a measured failure."""
    store = tmp_path / "verdicts.sqlite3"
    empty = report(instrument(1, InstrumentJoinVerdict.JOIN_UNVERIFIABLE, verifiable=0))
    object.__setattr__(empty, "bars_total", 0)
    object.__setattr__(empty.instruments[0], "bars_total", 0)
    write_session_verdicts(empty, store)
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.verifiable_bar_fraction is None


def test_a_price_basis_divergence_is_stored_and_counted_apart_from_scatter(tmp_path: Path) -> None:
    """`M26`. The two need different actions — a rescaled series is repairable, scatter is not —
    so the store must be able to tell them apart rather than reporting one refusal count."""
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(
        report(
            instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED),
            instrument(
                2,
                InstrumentJoinVerdict.JOIN_REFUTED,
                disagreements=40,
                shape=DisagreementShape.PRICE_BASIS_DIVERGENCE,
                basis_ratio=0.95099,
            ),
            instrument(
                3,
                InstrumentJoinVerdict.JOIN_REFUTED,
                disagreements=12,
                shape=DisagreementShape.SPORADIC_DISAGREEMENT,
            ),
        ),
        store,
    )
    assert price_basis_divergences_for(SESSION, store) == {2: 0.95099}
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.instruments_refuted == 2
    assert coverage.instruments_with_price_basis_divergence == 1, (
        "the repairable half of the refusals must be countable on its own"
    )


def test_a_store_written_before_the_shape_existed_is_migrated(tmp_path: Path) -> None:
    """`CREATE TABLE IF NOT EXISTS` leaves an old table alone, so without the migration every
    insert would fail on column arity against a store from an earlier run."""
    store = tmp_path / "verdicts.sqlite3"
    connection = sqlite3.connect(store)
    connection.executescript(
        "CREATE TABLE bar_tape_join_verdict ("
        " session_date TEXT NOT NULL, instrument_token INTEGER NOT NULL, verdict TEXT NOT NULL,"
        " bars_total INTEGER NOT NULL, comparisons_verifiable INTEGER NOT NULL,"
        " disagreements INTEGER NOT NULL, null_disagreement_rate REAL, binomial_tail REAL,"
        " median_deviation_paise REAL, median_spread_paise REAL, significance REAL NOT NULL,"
        " PRIMARY KEY (session_date, instrument_token));"
    )
    connection.execute(
        "INSERT INTO bar_tape_join_verdict VALUES ('2026-08-12',9,'join_refuted',5,5,5,0.0,0.0,"
        "0.0,1.0,0.01)"
    )
    connection.commit()
    connection.close()

    write_session_verdicts(report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED)), store)
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.instruments_total == 2, "the pre-existing row survives the migration"
    assert coverage.instruments_with_price_basis_divergence == 0, (
        "a row written before the shape was classified reads as not_applicable, not as a defect"
    )

    with sqlite3.connect(store) as reopened:
        columns = {row[1] for row in reopened.execute("PRAGMA table_info(bar_tape_join_verdict)")}
    assert "disagreement_upper_tail" in columns
    assert "binomial_tail" not in columns, (
        "`A.123` renames the column IN PLACE; a dead `binomial_tail` sitting beside the live "
        "column would be an orphan holding the same quantity under a name that lies (`R.06`)"
    )


def test_a_session_verified_at_two_thresholds_is_not_internally_consistent(tmp_path: Path) -> None:
    """`B6` proved the staleness quantile changes the verdicts, so a store holding rows from two
    runs at two policies holds two incomparable measurements — not one verification.

    This is not hypothetical: an interrupted sweep left exactly that state on 2026-08-15, with
    2026-08-11 written at 0.95 and the other two sessions still at 0.99.
    """
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(
        report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED)), store, staleness_quantile=0.99
    )
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.staleness_quantiles == (0.99,)
    assert coverage.is_internally_consistent

    write_session_verdicts(
        report(instrument(2, InstrumentJoinVerdict.JOIN_VERIFIED)), store, staleness_quantile=0.95
    )
    mixed = verification_coverage_for(SESSION, store)
    assert mixed is not None
    assert mixed.staleness_quantiles == (0.95, 0.99)
    assert not mixed.is_internally_consistent, (
        "two thresholds in one session must be detectable, not averaged into a single verdict"
    )


def test_a_session_judged_under_two_nulls_is_not_internally_consistent(tmp_path: Path) -> None:
    """`B6`'s lesson applied to the null itself (`A.123`).

    The null model changes the verdicts at least as much as the staleness quantile does, so a
    store holding rows from a binomial run and a beta-binomial run holds two incomparable
    measurements. Reading their counts as one verification is the same mistake in a new place.
    """
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(
        report(
            instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED),
            null_model=DisagreementNullModel.BETA_BINOMIAL_LEAVE_ONE_OUT,
        ),
        store,
        staleness_quantile=0.95,
    )
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.null_models == ("beta_binomial_leave_one_out",)
    assert coverage.is_internally_consistent

    write_session_verdicts(
        report(
            instrument(2, InstrumentJoinVerdict.JOIN_VERIFIED),
            null_model=DisagreementNullModel.BINOMIAL,
        ),
        store,
        staleness_quantile=0.95,
    )
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert len(coverage.null_models) == 2
    assert not coverage.is_internally_consistent, (
        "two nulls in one session is two measurements, not one verification"
    )


def test_rows_predating_a_column_are_a_setting_of_their_own_not_a_match(tmp_path: Path) -> None:
    """`GROUP_CONCAT(DISTINCT …)` DROPS nulls, so a session written half before a column existed
    and half after would show ONE distinct value and read as consistent.

    That is exactly how `B6`'s mixed store went unnoticed. An unrecorded setting is not evidence
    that it matched — it is evidence that nobody can say — so it counts as its own.
    """
    store = tmp_path / "verdicts.sqlite3"
    connection = sqlite3.connect(store)
    connection.executescript(
        "CREATE TABLE bar_tape_join_verdict ("
        " session_date TEXT NOT NULL, instrument_token INTEGER NOT NULL, verdict TEXT NOT NULL,"
        " bars_total INTEGER NOT NULL, comparisons_verifiable INTEGER NOT NULL,"
        " disagreements INTEGER NOT NULL, null_disagreement_rate REAL, binomial_tail REAL,"
        " median_deviation_paise REAL, median_spread_paise REAL, significance REAL NOT NULL,"
        " PRIMARY KEY (session_date, instrument_token));"
    )
    connection.execute(
        "INSERT INTO bar_tape_join_verdict VALUES ('2026-08-12',9,'join_verified',5,5,0,0.0,0.5,"
        "0.0,1.0,0.01)"
    )
    connection.commit()
    connection.close()

    # A store holding ONLY rows that predate the column is consistent: one run, one unrecorded
    # setting. Nothing is claimed about which setting it was.
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.rows_without_a_staleness_quantile == 1
    assert coverage.rows_without_a_null_model == 1
    assert coverage.is_internally_consistent

    # Adding a row that DOES record them makes the session mixed, and it must say so.
    write_session_verdicts(
        report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED)), store, staleness_quantile=0.95
    )
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.rows_without_a_staleness_quantile == 1
    assert coverage.staleness_quantiles == (0.95,)
    assert not coverage.is_internally_consistent, (
        "one recorded quantile plus one unrecorded row is two settings, not one"
    )


def test_the_new_columns_survive_a_round_trip_through_sqlite(tmp_path: Path) -> None:
    """`B3`/`B4`/`B5`/`B6` of the `A.124` review. Writing `NULL` instead of either new column,
    and breaking either of the two aggregates that read them back, ALL survived the first pass —
    because every coverage test built `SessionVerificationCoverage` by hand and none went through
    SQLite. The write -> read chain for a column is not tested by a constructor.
    """
    store = tmp_path / "verdicts.sqlite3"
    written = report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED))
    object.__setattr__(written, "minimum_detectable_disagreement_rate", 0.25)
    object.__setattr__(written, "minimum_comparisons_to_verify", 137)
    write_session_verdicts(written, store, staleness_quantile=0.95)

    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.minimum_detectable_disagreement_rates == (0.25,)
    assert coverage.rows_without_a_minimum_detectable_rate == 0
    assert coverage.minimum_comparisons_to_verify == 137, (
        "the evidence demand was written on every row and selected by nothing (`M4`) — the "
        "identical R.06 orphan the previous review found in the correlation column"
    )
    assert coverage.mean_null_intra_instrument_correlation == 0.0


def test_a_store_written_across_a124_says_the_claim_is_unrecorded(tmp_path: Path) -> None:
    """`B1`/`M5`. `_distinct_settings` was applied to the claim, but nothing asserted it, so
    replacing it with a bare `len()` survived — and a store half-written before `A.124` then reads
    as one verification. `null_model` has this test; the claim did not."""
    store = tmp_path / "verdicts.sqlite3"
    connection = sqlite3.connect(store)
    connection.executescript(
        "CREATE TABLE bar_tape_join_verdict ("
        " session_date TEXT NOT NULL, instrument_token INTEGER NOT NULL, verdict TEXT NOT NULL,"
        " bars_total INTEGER NOT NULL, comparisons_verifiable INTEGER NOT NULL,"
        " disagreements INTEGER NOT NULL, null_disagreement_rate REAL, binomial_tail REAL,"
        " median_deviation_paise REAL, median_spread_paise REAL, significance REAL NOT NULL,"
        " PRIMARY KEY (session_date, instrument_token));"
    )
    connection.execute(
        "INSERT INTO bar_tape_join_verdict VALUES ('2026-08-12',9,'join_verified',5,5,0,0.0,0.5,"
        "0.0,1.0,0.01)"
    )
    connection.commit()
    connection.close()

    write_session_verdicts(
        report(instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED)), store, staleness_quantile=0.95
    )
    coverage = verification_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.rows_without_a_minimum_detectable_rate == 1
    assert coverage.minimum_detectable_disagreement_rates == (0.5,)
    assert not coverage.is_internally_consistent, (
        "one row claiming 50% beside one that cannot say what it claimed is two verifications"
    )


def test_the_uncleared_set_is_refused_plus_undecided_and_the_refusal_set_is_unchanged(
    tmp_path: Path,
) -> None:
    """`M31`/`A.125`. The paper loop filtered on refusals alone, so an instrument the verification
    could not decide about was treated exactly like one it cleared.

    Both readers exist because they answer different questions: `refuted_instruments_for` is "which
    were MEASURED to describe two different markets", `instruments_not_cleared_for` is "which the
    loop has no positive evidence about". The `A.41` three-way partition survives in the store.
    """
    store = tmp_path / "verdicts.sqlite3"
    write_session_verdicts(
        report(
            instrument(1, InstrumentJoinVerdict.JOIN_VERIFIED),
            instrument(2, InstrumentJoinVerdict.JOIN_REFUTED, disagreements=30),
            instrument(3, InstrumentJoinVerdict.JOIN_UNVERIFIABLE, verifiable=2),
            instrument(4, InstrumentJoinVerdict.JOIN_UNVERIFIABLE, verifiable=0),
        ),
        store,
    )
    assert refuted_instruments_for(SESSION, store) == frozenset({2}), (
        "the refusal set must NOT grow — other callers read it as 'measured to disagree'"
    )
    uncleared = instruments_not_cleared_for(SESSION, store)
    assert uncleared == frozenset({2, 3, 4})
    assert isinstance(uncleared, frozenset), "the replay engine takes a frozenset"
    assert 1 not in uncleared, "a verified instrument is cleared"


def test_an_unrun_session_clears_nothing_and_withholds_nothing(tmp_path: Path) -> None:
    """The `A.41` trap in the new reader: for a session nobody verified, it returns empty — which
    means "withhold nothing", NOT "everything is fine". `verification_coverage_for` returning
    `None` is what the caller must branch on, exactly as before."""
    store = tmp_path / "verdicts.sqlite3"
    assert instruments_not_cleared_for(SESSION, store) == frozenset()
    assert verification_coverage_for(SESSION, store) is None


# --------------------------------------- the CONSUMER decision (`M31`, `A.125` review MEDIUM-6)
#
# The review found the whole paper-loop change untested: reverting the filter to refusals only,
# mis-counting the split, and gutting the unrun-session warning ALL survived the suite, because no
# test imports the script. Three of the five real mutation survivors were on that one function.


def _paper_instrument(token: int) -> PaperInstrument:
    return PaperInstrument(instrument_token=token, trading_symbol=f"T{token}", lot_size=1)


def test_the_loop_withholds_undecided_instruments_not_only_refused_ones() -> None:
    """`M31`. Reverting this to `join_refusals` is the mutation that survived; it is the whole
    behaviour change."""
    from verify_paper_session_on_real_data import admit_instruments_for

    instruments = [_paper_instrument(token) for token in (1, 2, 3, 4)]
    admission = admit_instruments_for(
        instruments,
        refusals=frozenset({2}),
        uncleared=frozenset({2, 3}),
        coverage=object(),
        internally_consistent=True,
    )
    assert [one.instrument_token for one in admission.instruments] == [1, 4]
    assert (admission.refused, admission.undecided, admission.withheld) == (1, 1, 2), (
        "the split must separate 'measured to disagree' from 'no evidence' — collapsing them is "
        "the mutation that survived"
    )


def test_an_unverified_session_withholds_nothing_and_that_is_not_a_pass() -> None:
    """The `A.41` trap in the new reader. `instruments_not_cleared_for` returns an empty set for a
    session nobody verified, so an empty set must never be read as "everything is fine"."""
    from verify_paper_session_on_real_data import admit_instruments_for

    instruments = [_paper_instrument(token) for token in (1, 2, 3)]
    admission = admit_instruments_for(
        instruments,
        refusals=frozenset(),
        uncleared=frozenset(),
        coverage=None,
        internally_consistent=False,
    )
    assert admission.coverage is None, "the caller must be able to see there was no verification"
    assert len(admission.instruments) == 3
    assert admission.withheld == 0


def test_a_session_written_under_two_policies_trades_nothing() -> None:
    """`B6`'s lesson at the consumer: two incomparable measurements are not one verification, so
    the run refuses rather than averaging them."""
    from verify_paper_session_on_real_data import admit_instruments_for

    admission = admit_instruments_for(
        [_paper_instrument(1)],
        refusals=frozenset(),
        uncleared=frozenset(),
        coverage=object(),
        internally_consistent=False,
    )
    assert admission.inconsistent
    assert admission.instruments == []
