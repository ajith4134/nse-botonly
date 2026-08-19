"""`M14` — where the join verdicts live, so the loop and the dashboard read the same answer.

Spec: `docs/research/236`. Engine: `bar_tape_join_verification_engine`.

**Why a store rather than a recomputation.** Verifying one session means walking 1.3 GiB of
parquet. A paper loop that recomputed it at start-up, and a dashboard panel that recomputed it
per page load, would be two expensive answers that can silently differ. The verification is run
once by `scripts/verify_bar_tape_join_on_real_data.py`; everything downstream reads this table.

**An unrun verification reads as unrun, never as clean.** `refuted_instruments_for` returns an
empty set for a date nothing has been verified for, and `verification_coverage_for` is how a
caller tells that apart from a date that was verified and found sound. Folding the two together
is the exact `A.41` mistake: "no refusals recorded" and "nothing refused" are different facts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nse_algo_trader.market_depth.bar_tape_join_verification_engine import (
    DisagreementShape,
    InstrumentJoinVerdict,
    SessionJoinVerificationReport,
)

DEFAULT_VERDICT_STORE = Path("~/.nse_algo_trader/bar_tape_join_verdicts.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_tape_join_verdict (
    session_date            TEXT NOT NULL,
    instrument_token        INTEGER NOT NULL,
    verdict                 TEXT NOT NULL,
    bars_total              INTEGER NOT NULL,
    comparisons_verifiable  INTEGER NOT NULL,
    disagreements           INTEGER NOT NULL,
    null_disagreement_rate  REAL,
    disagreement_upper_tail REAL,
    median_deviation_paise  REAL,
    median_spread_paise     REAL,
    disagreement_shape      TEXT NOT NULL DEFAULT 'not_applicable',
    price_basis_ratio       REAL,
    significance            REAL NOT NULL,
    staleness_quantile      REAL,
    null_intra_instrument_correlation REAL,
    null_model              TEXT,
    minimum_detectable_disagreement_rate REAL,
    minimum_comparisons_to_verify INTEGER,
    PRIMARY KEY (session_date, instrument_token)
);
CREATE INDEX IF NOT EXISTS bar_tape_join_verdict_by_verdict
    ON bar_tape_join_verdict (session_date, verdict);
"""


@dataclass(frozen=True, slots=True)
class SessionVerificationCoverage:
    """What a session's verification actually established — the dashboard surface's row.

    Carries the unverifiable count as a first-class number rather than leaving it to be inferred
    from a subtraction, because it is the one that says how far to trust the other two.
    """

    session_date: date
    significance: float
    instruments_verified: int
    instruments_refuted: int
    instruments_unverifiable: int
    staleness_quantiles: tuple[float, ...]
    """Every distinct staleness quantile the rows for this session were written under. More than
    one means the session was verified by two different runs and the rows are NOT comparable —
    which is what an interrupted sweep leaves behind."""
    rows_without_a_staleness_quantile: int
    """Rows written before the quantile was recorded at all. Counted separately because
    `GROUP_CONCAT(DISTINCT …)` DROPS nulls, so a session half-written before `B6` and half after
    would show one distinct quantile and read as consistent — the exact failure `B6` was."""
    significances: tuple[float, ...]
    """Every distinct significance the rows were written under. It is the engine's ONLY policy
    input and it decides every verdict, so it belongs in the consistency check for exactly the
    reason `staleness_quantile` does — and it was missing, while `MAX(significance)` reported the
    LOOSEST of a mixed set as though it were the one in force."""
    null_models: tuple[str, ...]
    """Every distinct null the rows were judged under. Same role as the quantile: it changes the
    verdicts, so more than one makes the counts two incomparable measurements added together."""
    rows_without_a_null_model: int
    """Rows written before `A.123` recorded which null produced them."""
    minimum_detectable_disagreement_rates: tuple[float, ...]
    """Every distinct claim the rows were written under. A session verified under two different
    claims about what VERIFIED means is two incomparable measurements — the same argument as
    `significance` and `null_model`, applied to the third policy input (`A.124`)."""
    rows_without_a_minimum_detectable_rate: int
    """Rows written before `A.124`, which cannot say what their verified verdicts claimed."""
    minimum_comparisons_to_verify: int | None
    """How many comparisons the session's evidence demand worked out to, under the pooled null.

    Read here because a claim without its cost is not checkable: "verified means we could have
    caught a 50% disagreer" is only meaningful beside "and that took 22 comparisons". Written on
    every row and selected by nothing until `M4` — the identical `R.06` orphan the previous review
    found in `null_intra_instrument_correlation`, shipped again one round later in the same
    slice."""
    mean_null_intra_instrument_correlation: float | None
    """The session's own `rho-hat`, averaged across its rows — the headline number for whether
    replacing the binomial changed anything at all. `None` on a store written before it existed.

    Read here rather than left write-only: a column nothing selects is an orphan (`R.06`), and it
    is the one quantity `docs/research/241` §5 names as the model's falsification signal."""
    instruments_with_price_basis_divergence: int
    """Refused instruments whose bar series sits on a DIFFERENT price basis from the tape (`M26`).
    Reported separately because it is a repairable store defect, while the rest is sampling."""
    bars_total: int
    comparisons_verifiable: int

    @property
    def is_internally_consistent(self) -> bool:
        """Whether every row for this session came from one run, at one threshold, under one null.

        `False` means a sweep was interrupted and re-run at a different policy, so the counts mix
        two incomparable measurements and must not be read as one verification.

        **An unrecorded value counts as its own distinct setting.** Rows predating a column are not
        evidence that they match the rows that have it — they are evidence that nobody can say. So
        a session written entirely before a column existed is consistent (one setting, unknown),
        one written entirely after is consistent, and one written across the change is NOT. Folding
        the nulls away instead is how `B6`'s mixed store would have gone on reading as clean.
        """
        return (
            _distinct_settings(
                self.minimum_detectable_disagreement_rates,
                self.rows_without_a_minimum_detectable_rate,
            )
            <= 1
            and len(self.significances) <= 1
            and _distinct_settings(self.staleness_quantiles, self.rows_without_a_staleness_quantile)
            <= 1
            and _distinct_settings(self.null_models, self.rows_without_a_null_model) <= 1
        )

    @property
    def instruments_total(self) -> int:
        return self.instruments_verified + self.instruments_refuted + self.instruments_unverifiable

    @property
    def verifiable_bar_fraction(self) -> float | None:
        """`None`, not zero, when no bar was compared: an unrun session has no fraction."""
        if self.bars_total == 0:
            return None
        return self.comparisons_verifiable / self.bars_total


def write_session_verdicts(
    report: SessionJoinVerificationReport,
    store: Path = DEFAULT_VERDICT_STORE,
    *,
    staleness_quantile: float | None = None,
) -> int:
    """Persist one session's verdicts, returning how many rows were written.

    `staleness_quantile` is recorded alongside the verdicts because it CHANGES them (`B6`): the
    same session verified at 0.99 and at 0.95 produces different refusals, and without this column
    a store holding both is indistinguishable from one holding either.

    `INSERT OR REPLACE`: re-running at a different significance restates the same question
    rather than answering it twice, and a stale verdict outliving its re-run would refuse
    instruments on evidence nobody can reproduce.
    """
    store.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store)
    try:
        connection.executescript(_SCHEMA)
        _add_missing_columns(connection)
        connection.executemany(
            # Columns NAMED, never positional. `ALTER TABLE` appends, so a migrated store has a
            # different column ORDER from a freshly created one, and a positional insert would
            # write the shape into the significance column on exactly the stores that were
            # migrated — silently, and only there.
            "INSERT OR REPLACE INTO bar_tape_join_verdict ("
            " session_date, instrument_token, verdict, bars_total, comparisons_verifiable,"
            " disagreements, null_disagreement_rate, disagreement_upper_tail,"
            " median_deviation_paise, median_spread_paise, disagreement_shape, price_basis_ratio,"
            " significance, staleness_quantile, null_intra_instrument_correlation, null_model,"
            " minimum_detectable_disagreement_rate, minimum_comparisons_to_verify"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    report.session_date.isoformat(),
                    item.instrument_token,
                    item.verdict.value,
                    item.bars_total,
                    item.comparisons_verifiable,
                    item.disagreements,
                    item.null_disagreement_rate,
                    item.disagreement_upper_tail,
                    item.median_deviation_paise,
                    item.median_spread_paise,
                    item.disagreement_shape.value,
                    item.price_basis_ratio,
                    report.significance,
                    staleness_quantile,
                    item.null_intra_instrument_correlation,
                    report.null_model.value,
                    report.minimum_detectable_disagreement_rate,
                    report.minimum_comparisons_to_verify,
                )
                for item in report.instruments
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return len(report.instruments)


_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("disagreement_shape", "TEXT NOT NULL DEFAULT 'not_applicable'"),
    ("price_basis_ratio", "REAL"),
    # `B6` (`docs/research/240`) proved the staleness quantile CHANGES the verdicts — verifying at
    # 0.99 while the fill path runs at 0.95 is what invalidated an entire `R.05` pass. A store that
    # records `significance` but not this cannot tell two runs apart, and a session interrupted
    # mid-sweep leaves a store mixing both. `NULL` on rows written before it was recorded, which is
    # honest: those rows genuinely do not know what threshold produced them.
    ("staleness_quantile", "REAL"),
    # `A.123`. `rho-hat`, the intra-instrument correlation of the leave-one-out null this row's
    # verdict was produced under. `NULL` on rows written under the binomial null, which had none.
    ("null_intra_instrument_correlation", "REAL"),
    # WHICH null produced the verdict, and it is here for exactly the reason `staleness_quantile`
    # is: it CHANGES the verdicts, so a store holding two of them is indistinguishable from a store
    # holding either. `NULL` on every row written before `A.123` — correctly marking those rows as
    # unable to say, which is what they are.
    ("null_model", "TEXT"),
    # `A.124`. What `JOIN_VERIFIED` claims, and the power bar that claim produced. Recorded for the
    # third time for the same reason: a parameter that changes the verdicts must sit beside them.
    ("minimum_detectable_disagreement_rate", "REAL"),
    ("minimum_comparisons_to_verify", "INTEGER"),
)

_RENAMED_COLUMNS: tuple[tuple[str, str], ...] = (
    # `A.123`: the number is no longer computed by a binomial, so the old name lied about the
    # mechanism (`R.14`). Renamed IN PLACE rather than shadowed by a second column — a dead column
    # beside a live one holding the same quantity is an orphan (`R.06`) and the next reader has no
    # way to tell which one is current.
    ("binomial_tail", "disagreement_upper_tail"),
)


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Bring a store written before `M26` or `A.123` up to the current shape.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a store from an earlier run
    would silently keep the old column count and every insert would fail on arity. Migrating is
    cheap and keeps already-recorded verdicts: the added columns default to `not_applicable`, which
    is the honest reading of a row written before the shape was ever classified.

    Renames run BEFORE adds, so a store carrying the old `binomial_tail` is brought to the current
    name rather than growing a second, empty column beside it.
    """
    present = {row[1] for row in connection.execute("PRAGMA table_info(bar_tape_join_verdict)")}
    for old_name, new_name in _RENAMED_COLUMNS:
        if old_name in present and new_name not in present:
            connection.execute(
                f"ALTER TABLE bar_tape_join_verdict RENAME COLUMN {old_name} TO {new_name}"
            )
            present.discard(old_name)
            present.add(new_name)
    for column, declaration in _ADDED_COLUMNS:
        if column not in present:
            connection.execute(
                f"ALTER TABLE bar_tape_join_verdict ADD COLUMN {column} {declaration}"
            )


def refuted_instruments_for(
    session_date: date, store: Path = DEFAULT_VERDICT_STORE
) -> frozenset[int]:
    """The consumer read (`R.06`) — every instrument this session's join refused.

    Shaped as `frozenset[int]` so it unions directly with `L0.33`'s consolidated-feed refusals
    and drops straight into `OrderBookSnapshotReplayEngine(inadmissible_instruments=…)`.
    """
    if not store.exists():
        return frozenset()
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT instrument_token FROM bar_tape_join_verdict "
            "WHERE session_date = ? AND verdict = ?",
            (session_date.isoformat(), InstrumentJoinVerdict.JOIN_REFUTED.value),
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


def instruments_not_cleared_for(
    session_date: date, store: Path = DEFAULT_VERDICT_STORE
) -> frozenset[int]:
    """Every instrument this session's verification did NOT clear — refused OR undecided (`M31`).

    **Why this exists beside `refuted_instruments_for`, rather than replacing it.** They answer
    different questions and both have callers. `refuted_instruments_for` is "which instruments were
    MEASURED to describe two different markets"; this is "which instruments the paper loop has no
    positive evidence about". The `A.41` three-way partition is preserved in the store and in the
    coverage; what a CONSUMER does with `JOIN_UNVERIFIABLE` is a separate decision, and until
    `A.125` the loop silently treated it as cleared.

    Shaped as `frozenset[int]` so it unions with `L0.33`'s consolidated-feed refusals and drops
    into `OrderBookSnapshotReplayEngine(inadmissible_instruments=…)` exactly as the refusal set
    does.
    """
    if not store.exists():
        return frozenset()
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT instrument_token FROM bar_tape_join_verdict "
            "WHERE session_date = ? AND verdict IN (?, ?)",
            (
                session_date.isoformat(),
                InstrumentJoinVerdict.JOIN_REFUTED.value,
                InstrumentJoinVerdict.JOIN_UNVERIFIABLE.value,
            ),
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


def verification_coverage_for(
    session_date: date, store: Path = DEFAULT_VERDICT_STORE
) -> SessionVerificationCoverage | None:
    """What this session's verification established, or `None` if it was never run.

    `None` is the point: it is how a caller distinguishes "verified and clean" from "not looked
    at", which `refuted_instruments_for` alone cannot say.
    """
    if not store.exists():
        return None
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        # Reads open READ-ONLY, so they cannot migrate — and a store written by an earlier version
        # is missing whichever columns were added since. Naming them unconditionally makes every
        # read of such a store raise `OperationalError`, which on the dashboard reads as an outage
        # rather than as an old store. Absent columns are substituted with `NULL`, which is what
        # those rows genuinely know.
        shape = _readable_column(connection, "disagreement_shape")
        correlation = _readable_column(connection, "null_intra_instrument_correlation")
        claim = _readable_column(connection, "minimum_detectable_disagreement_rate")
        evidence_demand = _readable_column(connection, "minimum_comparisons_to_verify")
        staleness = _readable_column(connection, "staleness_quantile")
        null_model = _readable_column(connection, "null_model")
        row = connection.execute(
            # `S608` is suppressed on the next line only: every interpolated name is either drawn
            # from `_SUBSTITUTABLE_COLUMNS` or is the constant "NULL", and `_readable_column`
            # raises on anything else, so no caller-supplied text can reach this SQL.
            "SELECT "  # noqa: S608
            " SUM(verdict = ?), SUM(verdict = ?), SUM(verdict = ?), "
            " SUM(bars_total), SUM(comparisons_verifiable), MAX(significance), COUNT(*), "
            f" SUM({shape} = ?), "
            f" GROUP_CONCAT(DISTINCT {staleness}), "
            f" SUM({staleness} IS NULL), "
            f" GROUP_CONCAT(DISTINCT {null_model}), "
            f" SUM({null_model} IS NULL), "
            " GROUP_CONCAT(DISTINCT significance), "
            f" AVG({correlation}), "
            f" GROUP_CONCAT(DISTINCT {claim}), "
            f" SUM({claim} IS NULL), "
            f" MAX({evidence_demand}) "
            "FROM bar_tape_join_verdict WHERE session_date = ?",
            (
                InstrumentJoinVerdict.JOIN_VERIFIED.value,
                InstrumentJoinVerdict.JOIN_REFUTED.value,
                InstrumentJoinVerdict.JOIN_UNVERIFIABLE.value,
                DisagreementShape.PRICE_BASIS_DIVERGENCE.value,
                session_date.isoformat(),
            ),
        ).fetchone()
    if row is None or not row[6]:
        return None
    return SessionVerificationCoverage(
        session_date=session_date,
        significance=float(row[5]),
        instruments_verified=int(row[0] or 0),
        instruments_refuted=int(row[1] or 0),
        instruments_unverifiable=int(row[2] or 0),
        instruments_with_price_basis_divergence=int(row[7] or 0),
        staleness_quantiles=tuple(
            sorted(float(value) for value in str(row[8] or "").split(",") if value)
        ),
        rows_without_a_staleness_quantile=int(row[9] or 0),
        null_models=tuple(sorted(value for value in str(row[10] or "").split(",") if value)),
        rows_without_a_null_model=int(row[11] or 0),
        significances=tuple(
            sorted(float(value) for value in str(row[12] or "").split(",") if value)
        ),
        mean_null_intra_instrument_correlation=(None if row[13] is None else float(row[13])),
        minimum_detectable_disagreement_rates=tuple(
            sorted(float(value) for value in str(row[14] or "").split(",") if value)
        ),
        rows_without_a_minimum_detectable_rate=int(row[15] or 0),
        minimum_comparisons_to_verify=(None if row[16] is None else int(row[16])),
        bars_total=int(row[3] or 0),
        comparisons_verifiable=int(row[4] or 0),
    )


_SUBSTITUTABLE_COLUMNS = frozenset(
    {
        "disagreement_shape",
        "price_basis_ratio",
        "staleness_quantile",
        "null_model",
        "null_intra_instrument_correlation",
        "minimum_detectable_disagreement_rate",
        "minimum_comparisons_to_verify",
    }
)
"""The only names `_readable_column` will ever interpolate. Every one is a migration-added
column, and the allow-list is what makes the interpolation safe rather than a promise that it
is: a name outside it raises instead of reaching the SQL text."""


def _readable_column(connection: sqlite3.Connection, column: str) -> str:
    """The column's name if this store has it, else the literal `NULL`.

    The return value is drawn from `_SUBSTITUTABLE_COLUMNS` or is the constant `"NULL"`, so the
    strings the callers interpolate cannot be influenced by anything outside this module.
    """
    if column not in _SUBSTITUTABLE_COLUMNS:
        raise ValueError(f"{column!r} is not a substitutable column")
    present = {row[1] for row in connection.execute("PRAGMA table_info(bar_tape_join_verdict)")}
    return column if column in present else "NULL"


def _distinct_settings(recorded: tuple[object, ...], unrecorded_rows: int) -> int:
    """How many different settings a session's rows were written under, counting "unrecorded" as
    one of them."""
    return len(recorded) + (1 if unrecorded_rows > 0 else 0)


def price_basis_divergences_for(
    session_date: date, store: Path = DEFAULT_VERDICT_STORE
) -> dict[int, float]:
    """Instrument token to the constant factor its bar series is off by (`M26`).

    The number a repair would divide by, and the number to adjudicate against a third source. Empty
    when nothing diverged OR when nothing was verified — `verification_coverage_for` is what tells
    those apart.
    """
    if not store.exists():
        return {}
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        shape = _readable_column(connection, "disagreement_shape")
        ratio = _readable_column(connection, "price_basis_ratio")
        rows = connection.execute(
            # See `verification_coverage_for` — same allow-listed substitution, same guarantee.
            f"SELECT instrument_token, {ratio} FROM bar_tape_join_verdict "  # noqa: S608
            f"WHERE session_date = ? AND {shape} = ? AND {ratio} IS NOT NULL",
            (session_date.isoformat(), DisagreementShape.PRICE_BASIS_DIVERGENCE.value),
        ).fetchall()
    return {int(token): float(ratio) for token, ratio in rows}


def verified_session_dates(store: Path = DEFAULT_VERDICT_STORE) -> tuple[date, ...]:
    """Every session the verification has been run for, oldest first."""
    if not store.exists():
        return ()
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT DISTINCT session_date FROM bar_tape_join_verdict ORDER BY session_date"
        ).fetchall()
    return tuple(date.fromisoformat(str(row[0])) for row in rows)
