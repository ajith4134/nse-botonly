"""Where the cards live, and how the gate finds out it was wrong — `L5.31`.

Spec `docs/research/260`.

**Append-only, and refusals are kept.** A store that held only admitted trades could never answer
the question that matters most about a floor: *what did it turn away, and would that have made
money?* Refusals are the control group. Without them the engine can only ever be told it was right.

**Idempotent on `content_hash`.** Re-running a session re-records nothing, and that is proven on
real data rather than on a fixture — the same property `L5.30`'s ledger earned when a second run of
the same paper session accrued zero new trades.

**The learning loop.** `attach_realised_outcome` joins an admitted card to what the trade actually
did. Those joins are what the calibrator and the payoff estimator refit on next, which is the whole
mechanism by which this gate is scored on its own subsequent record instead of on its confidence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    CalibratedProbability,
    CalibrationMethod,
    FloorDerivation,
    GrossExpectancyPosterior,
    PayoffPosterior,
    QualityFloor,
    QualityVerdict,
    TradeQualityError,
    TradeQualityEvidenceCard,
)

DEFAULT_EVIDENCE_PATH = Path("~/.nse_algo_trader/trade_quality_evidence.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_quality_evidence_card (
    content_hash            TEXT PRIMARY KEY,
    assessed_at             TEXT NOT NULL,
    session_date            TEXT NOT NULL,
    bot_identity            TEXT NOT NULL,
    trading_segment         TEXT NOT NULL,
    instrument_token        INTEGER NOT NULL,
    trading_symbol          TEXT NOT NULL,
    proposed_quantity       INTEGER NOT NULL,
    scan_breadth            INTEGER NOT NULL,
    stated_probability      REAL NOT NULL,
    calibrated_probability  REAL NOT NULL,
    calibration_method      TEXT NOT NULL,
    calibration_trades      INTEGER NOT NULL,
    brier_reliability       REAL,
    brier_resolution        REAL,
    brier_uncertainty       REAL,
    payoff_json             TEXT,
    expectancy_json         TEXT,
    floors_json             TEXT NOT NULL,
    binding_derivation      TEXT NOT NULL,
    binding_rupees          TEXT NOT NULL,
    verdict                 TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    realised_net_rupees     TEXT,
    outcome_attached_at     TEXT
);
CREATE INDEX IF NOT EXISTS trade_quality_evidence_by_session
    ON trade_quality_evidence_card (session_date);
CREATE INDEX IF NOT EXISTS trade_quality_evidence_by_bot
    ON trade_quality_evidence_card (bot_identity);
CREATE TRIGGER IF NOT EXISTS trade_quality_evidence_is_append_only
BEFORE DELETE ON trade_quality_evidence_card
BEGIN
    SELECT RAISE(ABORT, 'append-only: a deleted refusal can never be shown to have been wrong');
END;
CREATE TRIGGER IF NOT EXISTS trade_quality_evidence_verdict_is_immutable
BEFORE UPDATE OF content_hash, assessed_at, bot_identity, trading_segment, instrument_token,
                 trading_symbol, proposed_quantity, scan_breadth, stated_probability,
                 calibrated_probability, calibration_method, calibration_trades, payoff_json,
                 expectancy_json, floors_json, binding_derivation, binding_rupees, verdict, reason
ON trade_quality_evidence_card
BEGIN
    SELECT RAISE(ABORT, 'append-only: a verdict may gain an outcome, never be rewritten');
END;
CREATE TRIGGER IF NOT EXISTS trade_quality_evidence_outcome_is_written_once
BEFORE UPDATE OF realised_net_rupees ON trade_quality_evidence_card
WHEN OLD.realised_net_rupees IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a verdict may be scored once, never re-scored');
END;
"""

PRAGMA_RECURSIVE_TRIGGERS = "PRAGMA recursive_triggers = ON"
"""Without this SQLite does NOT fire `BEFORE DELETE` on `INSERT OR REPLACE`.

`docs/research/261` MAJOR-2 measured it: a `REPLACE` overwrote a stored card wholesale, with no
error, no trigger and no change in row count — so the append-only guarantee had a hole exactly where
someone trying to rewrite a verdict would look.
"""


def _floors_to_json(floors: tuple[QualityFloor, ...]) -> str:
    return json.dumps(
        [
            {
                "derivation": floor.derivation.value,
                "rupees": str(floor.rupees),
                "explanation": floor.explanation,
            }
            for floor in floors
        ]
    )


def _floors_from_json(payload: str) -> tuple[QualityFloor, ...]:
    rows = json.loads(payload)
    return tuple(
        QualityFloor(
            derivation=FloorDerivation(row["derivation"]),
            rupees=Decimal(row["rupees"]),
            explanation=row["explanation"],
        )
        for row in rows
    )


def _payoff_to_json(payoff: PayoffPosterior | None) -> str | None:
    if payoff is None:
        return None
    return json.dumps(
        {
            "win_mean_rupees": str(payoff.win_mean_rupees),
            "win_lower_rupees": str(payoff.win_lower_rupees),
            "win_upper_rupees": str(payoff.win_upper_rupees),
            "loss_mean_rupees": str(payoff.loss_mean_rupees),
            "loss_lower_rupees": str(payoff.loss_lower_rupees),
            "loss_upper_rupees": str(payoff.loss_upper_rupees),
            "wins_observed": payoff.wins_observed,
            "losses_observed": payoff.losses_observed,
            "credible_mass": payoff.credible_mass,
        }
    )


def _payoff_from_json(payload: str | None) -> PayoffPosterior | None:
    if payload is None:
        return None
    row = json.loads(payload)
    return PayoffPosterior(
        win_mean_rupees=Decimal(row["win_mean_rupees"]),
        win_lower_rupees=Decimal(row["win_lower_rupees"]),
        win_upper_rupees=Decimal(row["win_upper_rupees"]),
        loss_mean_rupees=Decimal(row["loss_mean_rupees"]),
        loss_lower_rupees=Decimal(row["loss_lower_rupees"]),
        loss_upper_rupees=Decimal(row["loss_upper_rupees"]),
        wins_observed=int(row["wins_observed"]),
        losses_observed=int(row["losses_observed"]),
        credible_mass=float(row["credible_mass"]),
    )


def _expectancy_to_json(expectancy: GrossExpectancyPosterior | None) -> str | None:
    if expectancy is None:
        return None
    return json.dumps(
        {
            "mean_rupees": str(expectancy.mean_rupees),
            "lower_rupees": str(expectancy.lower_rupees),
            "upper_rupees": str(expectancy.upper_rupees),
            "credible_mass": expectancy.credible_mass,
            "draws": expectancy.draws,
            "floor_rupees": str(expectancy.floor_rupees),
            "probability_exceeding_floor": expectancy.probability_exceeding_floor,
        }
    )


def _expectancy_from_json(payload: str | None) -> GrossExpectancyPosterior | None:
    if payload is None:
        return None
    row = json.loads(payload)
    return GrossExpectancyPosterior(
        mean_rupees=Decimal(row["mean_rupees"]),
        lower_rupees=Decimal(row["lower_rupees"]),
        upper_rupees=Decimal(row["upper_rupees"]),
        credible_mass=float(row["credible_mass"]),
        draws=int(row["draws"]),
        floor_rupees=Decimal(row["floor_rupees"]),
        probability_exceeding_floor=float(row["probability_exceeding_floor"]),
    )


class TradeQualityEvidenceStore:
    """Persistence for the gate's verdicts, and the join back to what actually happened."""

    def __init__(self, database_path: Path = DEFAULT_EVIDENCE_PATH) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level="IMMEDIATE")
        connection.execute(PRAGMA_RECURSIVE_TRIGGERS)
        return connection

    @property
    def database_path(self) -> Path:
        return self._path

    def record(self, card: TradeQualityEvidenceCard) -> bool:
        """Store one card. Answers False when this exact card was already stored.

        The boolean is what makes a re-run auditable: a session that re-records nothing returns
        False for every card, which is a stronger statement than a silent `INSERT OR IGNORE` — and
        `docs/research/256` found exactly that silence hiding 196 of 260 trades in the ladder's
        first ledger.
        """
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO trade_quality_evidence_card (
                    content_hash, assessed_at, session_date, bot_identity, trading_segment,
                    instrument_token, trading_symbol, proposed_quantity, scan_breadth,
                    stated_probability, calibrated_probability, calibration_method,
                    calibration_trades, brier_reliability, brier_resolution, brier_uncertainty,
                    payoff_json, expectancy_json, floors_json, binding_derivation, binding_rupees,
                    verdict, reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    card.content_hash,
                    card.assessed_at.isoformat(),
                    card.assessed_at.date().isoformat(),
                    card.bot_identity,
                    card.trading_segment.value,
                    card.instrument_token,
                    card.trading_symbol,
                    card.proposed_quantity,
                    card.scan_breadth,
                    card.calibrated_probability.stated,
                    card.calibrated_probability.calibrated,
                    card.calibrated_probability.method.value,
                    card.calibrated_probability.fitted_on_trades,
                    card.calibrated_probability.brier_reliability,
                    card.calibrated_probability.brier_resolution,
                    card.calibrated_probability.brier_uncertainty,
                    _payoff_to_json(card.payoff),
                    _expectancy_to_json(card.gross_expectancy),
                    _floors_to_json(card.floors),
                    card.binding_floor.derivation.value,
                    str(card.binding_floor.rupees),
                    card.verdict.value,
                    card.reason,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def attach_realised_outcome(
        self, content_hash: str, net_rupees: Decimal, *, attached_at: datetime
    ) -> None:
        """Join an admitted card to what the trade it admitted actually netted.

        Refuses to overwrite an outcome already attached: a verdict may be scored once, and a second
        score against the same decision would let a bad admission be quietly re-graded.
        """
        if attached_at.tzinfo is None:
            raise TradeQualityError("an outcome attached at a naive instant cannot be ordered")
        if not net_rupees.is_finite():
            raise TradeQualityError(
                f"a realised outcome of {net_rupees} is not finite; it would be stored, "
                f"returned by "
                f"scored_verdicts() and poison every aggregate over this gate's record. "
                f"Every other money entry point here checks this (docs/research/261 MAJOR-3)"
            )
        # The write is guarded by the WHERE clause, not by a prior SELECT. `docs/research/261`
        # MAJOR-3: the SELECT ran outside the transaction, so two concurrent writers both read NULL,
        # both succeeded, and the last one won — "a verdict may be scored once" did not hold.
        with closing(self._connect()) as connection:
            updated = connection.execute(
                """
                UPDATE trade_quality_evidence_card
                   SET realised_net_rupees = ?, outcome_attached_at = ?
                 WHERE content_hash = ? AND realised_net_rupees IS NULL
                """,
                (str(net_rupees), attached_at.isoformat(), content_hash),
            ).rowcount
            connection.commit()
            if updated == 1:
                return
            existing = connection.execute(
                "SELECT realised_net_rupees FROM trade_quality_evidence_card "
                "WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        if existing is None:
            raise TradeQualityError(
                f"no evidence card is stored under {content_hash}, so there is no verdict for "
                f"this outcome to score"
            )
        raise TradeQualityError(
            f"card {content_hash} already carries a realised outcome of Rs {existing[0]}; "
            f"re-scoring a decision after the fact is how a bad admission stops looking bad"
        )

    def cards_for_session(self, session_date: date) -> tuple[TradeQualityEvidenceCard, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM trade_quality_evidence_card WHERE session_date = ? "
                "ORDER BY assessed_at, trading_symbol",
                (session_date.isoformat(),),
            ).fetchall()
            columns = [description[0] for description in connection.execute(
                "SELECT * FROM trade_quality_evidence_card LIMIT 0"
            ).description]
        return tuple(_card_from_row(dict(zip(columns, row, strict=True))) for row in rows)

    def verdict_counts(self, session_date: date) -> dict[QualityVerdict, int]:
        """How many proposals each verdict covered on a session — the dashboard's headline."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT verdict, COUNT(*) FROM trade_quality_evidence_card "
                "WHERE session_date = ? GROUP BY verdict",
                (session_date.isoformat(),),
            ).fetchall()
        return {QualityVerdict(verdict): int(count) for verdict, count in rows}

    def sessions_recorded(self) -> tuple[date, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_date FROM trade_quality_evidence_card ORDER BY 1"
            ).fetchall()
        return tuple(date.fromisoformat(row[0]) for row in rows)

    def scored_verdicts(self) -> Iterator[tuple[QualityVerdict, Decimal]]:
        """Every card that has an outcome attached, as `(verdict, realised net rupees)`.

        This is the pair the engine is ultimately judged on: admitted trades that lost, and — the
        harder and more valuable half — refused proposals whose outcome was later observed.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT verdict, realised_net_rupees FROM trade_quality_evidence_card "
                "WHERE realised_net_rupees IS NOT NULL ORDER BY assessed_at"
            ).fetchall()
        for verdict, net in rows:
            yield QualityVerdict(verdict), Decimal(net)


def _card_from_row(row: dict[str, object]) -> TradeQualityEvidenceCard:
    """Rebuild a card from its stored row, so a session can be re-read and re-argued."""
    return TradeQualityEvidenceCard(
        assessed_at=datetime.fromisoformat(str(row["assessed_at"])),
        bot_identity=str(row["bot_identity"]),
        trading_segment=TradingSegment(str(row["trading_segment"])),
        instrument_token=int(str(row["instrument_token"])),
        trading_symbol=str(row["trading_symbol"]),
        proposed_quantity=int(str(row["proposed_quantity"])),
        scan_breadth=int(str(row["scan_breadth"])),
        calibrated_probability=CalibratedProbability(
            stated=float(str(row["stated_probability"])),
            calibrated=float(str(row["calibrated_probability"])),
            method=CalibrationMethod(str(row["calibration_method"])),
            fitted_on_trades=int(str(row["calibration_trades"])),
            brier_reliability=_optional_float(row["brier_reliability"]),
            brier_resolution=_optional_float(row["brier_resolution"]),
            brier_uncertainty=_optional_float(row["brier_uncertainty"]),
        ),
        payoff=_payoff_from_json(_optional_text(row["payoff_json"])),
        gross_expectancy=_expectancy_from_json(_optional_text(row["expectancy_json"])),
        floors=_floors_from_json(str(row["floors_json"])),
        verdict=QualityVerdict(str(row["verdict"])),
        reason=str(row["reason"]),
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else float(str(value))


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
