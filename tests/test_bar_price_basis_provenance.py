"""`L0.37` — the bar store must be able to say WHICH price series it is holding.

Spec: `docs/research/239`. Defect: `docs/research/237`, `238`; found by `L0.36`'s first real pass
(`A.120`) when `HINDPETRO` disagreed with the depth tape on 150 of 150 comparable bars at a
CONSTANT ratio of 0.95099, triangulated against NSE bhavcopy — which agreed with the tape.

The test that carries the design is
`test_a_bar_whose_basis_is_unknown_is_refused_rather_than_assumed_traded`. Everything else here is
classification and plumbing; that one is `A.41`'s three-state partition surviving into the store
underneath the loop. `NULL` means *nobody recorded what these prices mean*, and the whole defect is
that it currently reads as *these are the prices that traded*.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
    FIVE_MINUTE_BAR_INTERVAL,
    PriceBasis,
    PriceBasisError,
    admit_on_traded_price_basis,
    classify_price_basis,
    ensure_price_basis_column,
    price_basis_coverage_for,
    traded_basis_tokens_for,
)

SESSION = date(2026, 8, 13)

_SCHEMA_BEFORE_L0_37 = """
CREATE TABLE price_bars (
    instrument_token INTEGER NOT NULL,
    bar_interval     TEXT NOT NULL,
    bar_timestamp    TEXT NOT NULL,
    open_price       REAL NOT NULL,
    high_price       REAL NOT NULL,
    low_price        REAL NOT NULL,
    close_price      REAL NOT NULL,
    volume           INTEGER NOT NULL,
    open_interest    INTEGER,
    availability_time TEXT,
    PRIMARY KEY (instrument_token, bar_interval, bar_timestamp)
);
"""


def _store(tmp_path: Path, rows: list[tuple[int, str, str | None]]) -> Path:
    """A store at the PRE-`L0.37` shape, then migrated — the state every real store is in.

    `rows` are `(instrument_token, bar_timestamp, adjustment_basis_as_of)`; a `None` basis is
    written as `NULL`, which is what all 1,022,751 existing rows hold.
    """
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA_BEFORE_L0_37)
    ensure_price_basis_column(connection)
    connection.executemany(
        "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price, "
        "high_price, low_price, close_price, volume, adjustment_basis_as_of) "
        f"VALUES (?, '{FIVE_MINUTE_BAR_INTERVAL}', ?, 1.0, 1.0, 1.0, 1.0, 1, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


# --------------------------------------------------------------------- classification


def test_a_bar_fetched_on_its_own_session_holds_what_traded() -> None:
    assert classify_price_basis(SESSION, SESSION) is PriceBasis.TRADED


def test_a_bar_fetched_later_may_have_been_rescaled_under_it() -> None:
    """The defect, as a classification. Kite's historical endpoint adjusts as of the moment it is
    asked, so a session backfilled after an ex-date comes back on a different basis than traded.

    The class is `ADJUSTED_AFTER_THE_SESSION`, not `RESCALED`: whether an action actually fell in
    the gap is a question for a corporate-action feed this project does not have. What is knowable
    from the dates alone is that it COULD have.
    """
    assert classify_price_basis(SESSION, date(2026, 8, 15)) is PriceBasis.ADJUSTED_AFTER_THE_SESSION


def test_an_unrecorded_basis_is_unknown_and_never_traded() -> None:
    """`A.41`. Three states, and "cannot tell" is one of them — folding it into TRADED is the
    entire defect this feature exists to end."""
    assert classify_price_basis(SESSION, None) is PriceBasis.UNKNOWN


def test_a_basis_before_the_session_is_impossible_and_says_so() -> None:
    """A source cannot have adjusted a series as of a date before the bars existed. Silently
    classifying it TRADED would hide a store defect behind a reassuring word."""
    with pytest.raises(PriceBasisError):
        classify_price_basis(SESSION, date(2026, 8, 12))


# --------------------------------------------------------------------- the consumer read


def test_a_bar_whose_basis_is_unknown_is_refused_rather_than_assumed_traded(
    tmp_path: Path,
) -> None:
    """**The test that carries the design.**

    A consumer that needs prices that actually traded — the paper loop, whose fills come from a
    depth tape holding the traded series — must not be handed a bar whose basis nobody recorded.
    All 1,022,751 rows written before this feature are in exactly that state, and their basis is
    PERMANENTLY unrecoverable: Kite will not re-serve those sessions unadjusted.
    """
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (2, f"{SESSION.isoformat()}T09:15:00+05:30", None),
            (3, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-15"),
        ],
    )
    traded = traded_basis_tokens_for(SESSION, store)
    assert traded == frozenset({1}), (
        "only the bar fetched on its own session is known to hold traded prices; the NULL one is "
        "unknown and the later-fetched one may have been rescaled under it"
    )


def test_a_store_that_predates_the_column_reads_as_entirely_unknown(tmp_path: Path) -> None:
    """Reads open read-only and cannot migrate, so a store written before `L0.37` must answer
    "nothing is known to be traded" rather than raising — the same `_readable_column` lesson the
    `A.123` review taught on the verdict store."""
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA_BEFORE_L0_37)
    connection.execute(
        "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price, "
        "high_price, low_price, close_price, volume) "
        f"VALUES (1, '{FIVE_MINUTE_BAR_INTERVAL}', "
        f"'{SESSION.isoformat()}T09:15:00+05:30', 1.0, 1.0, 1.0, 1.0, 1)"
    )
    connection.commit()
    connection.close()

    assert traded_basis_tokens_for(SESSION, path) == frozenset()
    coverage = price_basis_coverage_for(SESSION, path)
    assert coverage is not None
    assert coverage.bars_on_an_unknown_basis == 1
    assert coverage.bars_on_the_traded_basis == 0
    assert coverage.traded_fraction == 0.0


def test_an_absent_store_is_unknown_not_empty(tmp_path: Path) -> None:
    assert traded_basis_tokens_for(SESSION, tmp_path / "absent.sqlite3") == frozenset()
    assert price_basis_coverage_for(SESSION, tmp_path / "absent.sqlite3") is None


# --------------------------------------------------------------------- coverage, for the surface


def test_the_coverage_counts_every_basis_separately(tmp_path: Path) -> None:
    """`R.08`. The surface needs the three states as three numbers, because a single "verified"
    fraction would hide which half of the shortfall is repairable: bars fetched late can be
    re-fetched on the right day going forward, bars with no basis at all never can."""
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (1, f"{SESSION.isoformat()}T09:20:00+05:30", SESSION.isoformat()),
            (2, f"{SESSION.isoformat()}T09:15:00+05:30", None),
            (3, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-20"),
        ],
    )
    coverage = price_basis_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.bars_on_the_traded_basis == 2
    assert coverage.bars_on_an_unknown_basis == 1
    assert coverage.bars_adjusted_after_the_session == 1
    assert coverage.traded_fraction == 0.5
    assert coverage.latest_adjustment_basis == date(2026, 8, 20)


def test_a_session_the_store_has_no_bars_for_has_no_coverage(tmp_path: Path) -> None:
    """`None`, not a zeroed row: a session with no bars was never backfilled, and reporting 0%
    traded would read as a measured failure of a backfill that never ran."""
    store = _store(tmp_path, [(1, "2026-08-11T09:15:00+05:30", "2026-08-11")])
    assert price_basis_coverage_for(SESSION, store) is None


# --------------------------------------------------------------------- migration


def test_the_migration_is_idempotent_and_keeps_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA_BEFORE_L0_37)
    connection.execute(
        "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price, "
        "high_price, low_price, close_price, volume) "
        f"VALUES (7, '{FIVE_MINUTE_BAR_INTERVAL}', "
        f"'{SESSION.isoformat()}T09:15:00+05:30', 1.0, 1.0, 1.0, 1.0, 3)"
    )
    connection.commit()
    for _ in range(3):
        ensure_price_basis_column(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(price_bars)")}
    assert "adjustment_basis_as_of" in columns
    kept = connection.execute(
        "SELECT volume, adjustment_basis_as_of FROM price_bars WHERE instrument_token = 7"
    ).fetchone()
    assert kept == (3, None), "the pre-existing row survives, and its basis is honestly unknown"
    connection.close()


def test_the_at_risk_condition_fires_on_the_condition_not_on_an_instrument(
    tmp_path: Path,
) -> None:
    """Acceptance 4 of `docs/research/239`.

    The rule is "the basis date is later than the session", stated over dates. It has never been
    told about `HINDPETRO`, so it fires on the next occurrence without being taught about it —
    which is the difference between a check and a note about something that already happened.
    """
    store = _store(
        tmp_path,
        [
            (111, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-14"),
            (222, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
        ],
    )
    coverage = price_basis_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.instruments_at_risk == frozenset({111})
    assert 222 not in coverage.instruments_at_risk


def test_the_interval_label_is_the_one_the_store_actually_uses(tmp_path: Path) -> None:
    """A first draft defaulted to `"five_minute"`; the store writes **`"5m"`**, so every read
    returned nothing — SILENTLY, because a store with no matching rows is indistinguishable from a
    session that was never backfilled.

    The label is now defined once and imported by the writer, so this pins the contract rather than
    a spelling. It caught nothing in the suite and everything in the `R.05` pass, which is the
    argument for the `R.05` pass.
    """
    from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
        FIVE_MINUTE_BAR_INTERVAL,
    )

    assert FIVE_MINUTE_BAR_INTERVAL == "5m"

    path = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA_BEFORE_L0_37)
    ensure_price_basis_column(connection)
    connection.execute(
        "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price, "
        "high_price, low_price, close_price, volume, adjustment_basis_as_of) "
        f"VALUES (1, '{FIVE_MINUTE_BAR_INTERVAL}', "
        f"'{SESSION.isoformat()}T09:15:00+05:30', 1.0, 1.0, 1.0, 1.0, 1, '{SESSION.isoformat()}')"
    )
    connection.commit()
    connection.close()

    assert traded_basis_tokens_for(SESSION, path) == frozenset({1}), (
        "the default interval must match what the writer stores, or every read is a silent miss"
    )


def test_a_writer_and_a_reader_cannot_disagree_about_the_interval() -> None:
    """The backfill script imports the label rather than defining its own — the drift this pins is
    the one that produced the silent miss above."""
    import backfill_five_minute_bars

    from nse_algo_trader.historical_bars.bar_price_basis_provenance import (
        FIVE_MINUTE_BAR_INTERVAL,
    )

    assert backfill_five_minute_bars.BAR_INTERVAL is FIVE_MINUTE_BAR_INTERVAL


# ------------------------------------------- the consumer rule, and its `R.04` activation ladder


def test_the_rule_is_unarmed_when_no_bar_records_a_basis(tmp_path: Path) -> None:
    """`R.04`. Every bar written before `L0.37` is UNKNOWN — measured on the live store, **0 of
    1,022,751** carry a basis — so a blanket "traded basis only" rule would withhold the entire
    universe and the loop would trade nothing.

    The rule is built in full and arms itself on evidence. Unarmed, it withholds NOTHING and says
    so; that emptiness means "cannot distinguish", never "all clear".
    """
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", None),
            (2, f"{SESSION.isoformat()}T09:15:00+05:30", None),
        ],
    )
    admission = admit_on_traded_price_basis(SESSION, [1, 2], store)
    assert not admission.armed
    assert admission.instruments == (1, 2)
    assert admission.withheld == ()
    assert "UNARMED" in admission.describe()
    assert "NOT a clean bill of health" in admission.describe(), (
        "an unarmed rule must not read as a passed one — the whole A.41 point"
    )


def test_a_partial_backfill_does_not_arm_and_does_not_withhold_the_universe() -> None:
    """`H2` of the `A.126` review, and the rule this replaced was wrong by a factor of six hundred.

    The first version armed on the EXISTENCE of one traded-basis bar. Measured against the live
    store, a session with five instruments stamped and 3,322 unstamped reported itself *armed* at
    0.1% coverage and withheld 3,322 names the loop had been trading. The backfill commits per
    instrument and collects per-instrument failures, so a partial session is the NORMAL outcome.

    Arming now asks whether the session records a basis for every bar — threshold-free — so an
    unrecorded basis can be told from an unfinished backfill. Positive evidence still acts
    immediately: an instrument adjusted after its own session is withheld either way.
    """


def test_a_partial_session_withholds_only_what_is_positively_bad(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (2, f"{SESSION.isoformat()}T09:15:00+05:30", None),
            (3, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-20"),
        ],
    )
    admission = admit_on_traded_price_basis(SESSION, [1, 2, 3], store)
    assert not admission.armed, "one bar with no basis means the backfill did not finish"
    assert admission.withheld == (3,), "only the instrument POSITIVELY on another basis"
    assert admission.instruments == (1, 2), (
        "the unrecorded one is not withheld — that is absence of evidence, not evidence"
    )


def test_a_complete_session_arms_and_withholds_the_unrecorded_too(tmp_path: Path) -> None:
    """The converse. With every bar carrying a basis, an unrecorded one would be anomalous rather
    than unfinished — so arming is safe and the rule applies in full."""
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (3, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-20"),
        ],
    )
    admission = admit_on_traded_price_basis(SESSION, [1, 3], store)
    assert admission.armed
    assert admission.instruments == (1,)
    assert admission.withheld == (3,)


def test_the_admission_preserves_the_callers_order(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        [
            (token, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat())
            for token in (9, 4, 7)
        ],
    )
    assert admit_on_traded_price_basis(SESSION, [7, 9, 4], store).instruments == (7, 9, 4)


def test_a_session_with_no_bars_is_unarmed_and_says_there_is_nothing_to_say(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, [(1, "2026-08-11T09:15:00+05:30", "2026-08-11")])
    admission = admit_on_traded_price_basis(SESSION, [1], store)
    assert not admission.armed
    assert admission.coverage is None
    assert "no five-minute bars stored" in admission.describe()


# ------------------------------------------------ the WIRING, which nothing tested (`A.126` review)
#
# 13 of 30 mutations survived the first pass and every one was wiring. Three of them — a backfill
# that stamps the SESSION date, one that stamps NULL, and one that omits the column entirely —
# make the whole feature a rubber stamp, and all three passed 17/17. The algorithm was exhaustively
# tested and the thing that produces its input was not tested at all.


class _StubKite:
    """The one call the writer makes of a broker, answered locally (`R.J` — a hermetic harness
    behind a DI seam; it does not replace the `R.05` pass)."""

    def __init__(self, session: date) -> None:
        self._session = session

    def historical_data(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "date": datetime.fromisoformat(f"{self._session.isoformat()}T09:15:00+05:30"),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            }
        ]


def _run_the_real_backfill(tmp_path: Path, session: date, fetched_on: date) -> Path:
    """Run the REAL writer against a stubbed broker and an explicit fetch date."""
    import backfill_five_minute_bars as writer

    store = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(store)
    connection.executescript(_SCHEMA_BEFORE_L0_37)
    connection.commit()
    connection.close()
    writer.backfill(
        session_date=session,
        market_data=store,
        kite=_StubKite(session),
        tokens=[11],
        fetched_on=fetched_on,
    )
    return store


def _stored_basis(store: Path) -> str | None:
    with sqlite3.connect(store) as connection:
        row = connection.execute("SELECT adjustment_basis_as_of FROM price_bars").fetchone()
    return None if row[0] is None else str(row[0])


def test_the_backfill_actually_records_the_basis_it_fetched_on(tmp_path: Path) -> None:
    """Kills the three mutations that made the whole feature a rubber stamp — stamping the session
    date, stamping `NULL`, and omitting the column. Every one passed the first 17 tests, because
    nothing could call the writer without a live broker and a recorded depth tape."""
    store = _run_the_real_backfill(tmp_path, SESSION, SESSION)
    assert _stored_basis(store) == SESSION.isoformat()
    assert traded_basis_tokens_for(SESSION, store) == frozenset({11})


def test_a_next_morning_catch_up_records_itself_as_not_traded(tmp_path: Path) -> None:
    """`H1`. The daily timer fires twice — 19:00 IST the same day and **08:15 IST the next
    morning**, which targets yesterday's session. The morning run's writes genuinely ARE on a later
    basis, so the feature must label its own output honestly instead of claiming "traded basis by
    construction"."""
    store = _run_the_real_backfill(tmp_path, SESSION, date(2026, 8, 14))
    assert _stored_basis(store) == "2026-08-14"
    assert classify_price_basis(SESSION, date(2026, 8, 14)) is (
        PriceBasis.ADJUSTED_AFTER_THE_SESSION
    )
    assert traded_basis_tokens_for(SESSION, store) == frozenset()


def test_a_same_day_rerun_annotates_a_row_insert_or_ignore_would_skip(tmp_path: Path) -> None:
    """`H4`. `INSERT OR IGNORE` touches nothing when the bar already exists, so a re-run could
    never annotate a row written before `L0.37` — and the only sessions holding both bars and a
    depth tape are exactly those rows. Annotating is safe ONLY when the fetch happened on the
    session's own day, which is the one case where the source served the traded series."""
    store = _run_the_real_backfill(tmp_path, SESSION, SESSION)
    with sqlite3.connect(store) as connection:
        connection.execute("UPDATE price_bars SET adjustment_basis_as_of = NULL")
        connection.commit()
    assert traded_basis_tokens_for(SESSION, store) == frozenset()

    import backfill_five_minute_bars as writer

    writer.backfill(
        session_date=SESSION,
        market_data=store,
        kite=_StubKite(SESSION),
        tokens=[11],
        fetched_on=SESSION,
    )
    assert traded_basis_tokens_for(SESSION, store) == frozenset({11}), (
        "a same-day re-run must be able to annotate a NULL row that already exists"
    )


def test_a_later_rerun_must_not_annotate_an_existing_row(tmp_path: Path) -> None:
    """The guard on `H4`'s fix. Annotating on a later day would stamp a traded basis onto prices
    the source has already rescaled — inventing the fact the whole feature exists to record."""
    store = _run_the_real_backfill(tmp_path, SESSION, SESSION)
    with sqlite3.connect(store) as connection:
        connection.execute("UPDATE price_bars SET adjustment_basis_as_of = NULL")
        connection.commit()

    import backfill_five_minute_bars as writer

    writer.backfill(
        session_date=SESSION,
        market_data=store,
        kite=_StubKite(SESSION),
        tokens=[11],
        fetched_on=date(2026, 8, 20),
    )
    assert _stored_basis(store) is None, "a later run leaves the unknown basis unknown"


def test_one_impossible_row_does_not_take_down_the_whole_reading(tmp_path: Path) -> None:
    """`H3`. `classify_price_basis` is right to raise on a bar adjusted BEFORE its own session —
    that is a store defect. But `price_basis_coverage_for` feeds `/microstructure` and the paper
    loop's admission, and letting one bad row propagate made 209,912 honest rows unreportable and
    returned HTTP 500 from the surface. A store defect must be SHOWN, not hidden behind an outage.
    """
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (2, f"{SESSION.isoformat()}T09:15:00+05:30", "2026-08-01"),
        ],
    )
    coverage = price_basis_coverage_for(SESSION, store)
    assert coverage is not None, "one impossible row must not raise out of the aggregate"
    assert coverage.bars_on_an_impossible_basis == 1
    assert coverage.bars_on_the_traded_basis == 1
    assert 2 in coverage.instruments_at_risk
    assert "impossible" in coverage.describe()

    admission = admit_on_traded_price_basis(SESSION, [1, 2], store)
    assert admission.withheld == (2,), "an impossible basis is positive evidence, so it acts"


def test_the_interval_is_threaded_through_both_reads(tmp_path: Path) -> None:
    """Four mutations survived by dropping or ignoring the `bar_interval` filter. A store holding
    the same session at two intervals is what tells them apart."""
    store = _store(tmp_path, [(1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat())])
    with sqlite3.connect(store) as connection:
        connection.execute(
            "INSERT INTO price_bars (instrument_token, bar_interval, bar_timestamp, open_price, "
            "high_price, low_price, close_price, volume, adjustment_basis_as_of) "
            f"VALUES (2, '1d', '{SESSION.isoformat()}T09:15:00+05:30', 1.0, 1.0, 1.0, 1.0, 1, NULL)"
        )
        connection.commit()

    assert traded_basis_tokens_for(SESSION, store) == frozenset({1}), "the 1d row must not appear"
    coverage = price_basis_coverage_for(SESSION, store)
    assert coverage is not None
    assert coverage.bars_total == 1, "coverage must count only the interval it was asked about"

    other = price_basis_coverage_for(SESSION, store, bar_interval="1d")
    assert other is not None
    assert other.bars_on_an_unknown_basis == 1
    assert admit_on_traded_price_basis(SESSION, [1, 2], store, bar_interval="1d").armed is False


def test_the_session_date_is_a_whole_day_not_a_month(tmp_path: Path) -> None:
    """A mutation coarsening `substr(bar_timestamp, 1, 10)` to month granularity survived."""
    store = _store(
        tmp_path,
        [
            (1, f"{SESSION.isoformat()}T09:15:00+05:30", SESSION.isoformat()),
            (2, "2026-08-11T09:15:00+05:30", "2026-08-11"),
        ],
    )
    assert traded_basis_tokens_for(SESSION, store) == frozenset({1})
    coverage = price_basis_coverage_for(SESSION, store)
    assert coverage is not None and coverage.bars_total == 1


def test_the_backfill_universe_does_not_depend_on_the_depth_capture() -> None:
    """`A.127`. The universe WAS the depth tape's, and the tape's instrument set is chosen by a
    disk budget — its own log reads `admitted 652 of 9,891 instruments | projected 0.33 GiB of a
    0.33 GiB budget`. So bar coverage was hostage to how much disk the capture happened to get, and
    to whether it ran at all: **2026-08-14 was a trading Friday with no capture and `price_bars`
    held zero rows for it**, and the daily run reported "no universe to fetch" rather than a hole.

    The cash board is the universe; the tape is unioned in, never a gate.
    """
    from backfill_five_minute_bars import cash_equity_universe, universe_for

    store = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
    if not store.exists():  # pragma: no cover - the R.05 store is not present in every checkout
        pytest.skip("the real instrument master is not on this machine")

    board = cash_equity_universe(store)
    assert len(board) > 1_000, "the NSE cash board is thousands of instruments, not hundreds"

    # A session the depth tape has no partition for must still get the full cash board.
    without_a_tape = universe_for(date(2026, 8, 14), store, Path("/nonexistent/tape"))
    assert set(without_a_tape) == set(board), (
        "a missing capture must not shrink the bar store's universe"
    )

    # And a session the tape DOES cover is a superset, never a replacement.
    with_a_tape = universe_for(date(2026, 8, 13), store)
    assert set(with_a_tape) >= set(board)
