"""Every verdict the gate reached, kept — so a refusal can be argued with afterwards.

A gate that decides and forgets cannot be audited, cannot be reviewed, and cannot answer the
only question an operator ever really asks about it: *why did you not take that trade?* The
decision is cheap to make and impossible to reconstruct, because it depends on a book snapshot
that no longer exists by the time anyone asks.

So each decision is written down with the arithmetic that produced it: the hurdle decomposed
into statutory and execution parts, how much of it was uncertainty rather than expected cost,
which named precondition failed if one did, and the shortfall if it was vetoed.

**`UNPRICEABLE` is stored as its own verdict and never folded into the refusals.** A veto is a
judgement; an unpriceable is the absence of one. A log that conflated them would make a data
outage indistinguishable from a run of genuinely uneconomic signals, which is precisely the
confusion the gate's own vocabulary was built to prevent.

**This is also `L1.07`'s seam.** When realised fills exist, they join to these rows on the order
reference and the modelled-versus-realised comparison becomes possible without re-deriving
anything. Nothing writes fills yet, and that is recorded rather than stubbed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.cost_gate.pre_trade_cost_gate import GateDecision, GateVerdict

DEFAULT_DECISION_LOG_PATH = Path("~/.nse_algo_trader/cost_gate.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_decision (
    decided_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    instrument_token INTEGER NOT NULL,
    trading_symbol TEXT NOT NULL,
    segment TEXT NOT NULL,
    side TEXT NOT NULL,
    source TEXT NOT NULL,
    verdict TEXT NOT NULL,
    expected_edge_bps TEXT NOT NULL,
    proposed_quantity INTEGER NOT NULL,
    approved_quantity INTEGER NOT NULL,
    statutory_bps TEXT,
    execution_point_bps TEXT,
    execution_upper_bps TEXT,
    required_bps TEXT,
    uncertainty_bps TEXT,
    is_execution_censored INTEGER,
    failed_preconditions TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (session_date, instrument_token, decided_at, source)
);
CREATE INDEX IF NOT EXISTS gate_decision_by_session ON gate_decision (session_date, verdict);
"""


class GateDecisionLogError(Exception):
    """The log cannot be read or written."""


@dataclass(frozen=True, slots=True)
class LoggedGateDecision:
    """One recorded verdict, flat enough to query and complete enough to argue with."""

    decided_at: datetime
    session_date: date
    instrument_token: int
    trading_symbol: str
    segment: str
    side: str
    source: str
    verdict: GateVerdict
    expected_edge_bps: Decimal
    proposed_quantity: int
    approved_quantity: int
    statutory_bps: Decimal | None
    execution_point_bps: Decimal | None
    execution_upper_bps: Decimal | None
    required_bps: Decimal | None
    uncertainty_bps: Decimal | None
    is_execution_censored: bool | None
    failed_preconditions: tuple[str, ...]
    reason: str

    @property
    def is_judged(self) -> bool:
        """Whether the gate formed an opinion at all. `UNPRICEABLE` did not."""
        return self.verdict is not GateVerdict.UNPRICEABLE

    @property
    def shortfall_bps(self) -> Decimal | None:
        if self.required_bps is None or self.verdict is not GateVerdict.VETO:
            return None
        return self.required_bps - self.expected_edge_bps


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class GateDecisionLog:
    """Writes verdicts and reads them back by session."""

    def __init__(self, database_path: Path = DEFAULT_DECISION_LOG_PATH) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def record(self, decisions: Iterable[GateDecision], *, session_date: date) -> int:
        rows = []
        for decision in decisions:
            signal = decision.signal
            hurdle = decision.hurdle
            failed = (
                ",".join(failure.name.value for failure in decision.preconditions.failures)
                if decision.preconditions is not None
                else ""
            )
            rows.append(
                (
                    signal.decided_at.isoformat(),
                    session_date.isoformat(),
                    signal.instrument_token,
                    signal.trading_symbol,
                    signal.segment.value,
                    signal.side.value,
                    signal.source,
                    decision.verdict.value,
                    str(signal.expected_edge_bps),
                    signal.proposed_quantity,
                    decision.approved_quantity,
                    _decimal_or_none(None if hurdle is None else hurdle.statutory_bps),
                    _decimal_or_none(None if hurdle is None else hurdle.execution_point_bps),
                    _decimal_or_none(None if hurdle is None else hurdle.execution_upper_bps),
                    _decimal_or_none(None if hurdle is None else hurdle.required_bps),
                    _decimal_or_none(None if hurdle is None else hurdle.uncertainty_bps),
                    None if hurdle is None else int(hurdle.is_execution_censored),
                    failed,
                    decision.reason,
                )
            )
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO gate_decision (decided_at, session_date, "
                "instrument_token, trading_symbol, segment, side, source, verdict, "
                "expected_edge_bps, proposed_quantity, approved_quantity, statutory_bps, "
                "execution_point_bps, execution_upper_bps, required_bps, uncertainty_bps, "
                "is_execution_censored, failed_preconditions, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return len(rows)

    def decisions_for(self, session_date: date) -> Sequence[LoggedGateDecision]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT decided_at, session_date, instrument_token, trading_symbol, segment, "
                "side, source, verdict, expected_edge_bps, proposed_quantity, "
                "approved_quantity, statutory_bps, execution_point_bps, execution_upper_bps, "
                "required_bps, uncertainty_bps, is_execution_censored, failed_preconditions, "
                "reason FROM gate_decision WHERE session_date = ? ORDER BY decided_at",
                [session_date.isoformat()],
            ).fetchall()
        return tuple(_row_to_decision(row) for row in rows)

    def latest_session(self) -> date | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT MAX(session_date) FROM gate_decision").fetchone()
        return None if row is None or row[0] is None else date.fromisoformat(row[0])

    def verdict_counts(self, session_date: date) -> dict[GateVerdict, int]:
        """Counts per verdict, with every verdict present even at zero.

        Zeros are kept rather than dropped so a reader can tell "none were vetoed" from "the
        veto count was never computed" — a distinction a sparse dict silently destroys.
        """
        counts = dict.fromkeys(GateVerdict, 0)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT verdict, COUNT(*) FROM gate_decision WHERE session_date = ? "
                "GROUP BY verdict",
                [session_date.isoformat()],
            ).fetchall()
        for verdict_value, count in rows:
            counts[GateVerdict(verdict_value)] = int(count)
        return counts

    def decision_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM gate_decision").fetchone()
        return int(row[0])


def _row_to_decision(row: Sequence[object]) -> LoggedGateDecision:
    def optional(index: int) -> Decimal | None:
        value = row[index]
        return None if value is None else Decimal(str(value))

    censored = row[16]
    return LoggedGateDecision(
        decided_at=datetime.fromisoformat(str(row[0])),
        session_date=date.fromisoformat(str(row[1])),
        instrument_token=int(str(row[2])),
        trading_symbol=str(row[3]),
        segment=str(row[4]),
        side=str(row[5]),
        source=str(row[6]),
        verdict=GateVerdict(str(row[7])),
        expected_edge_bps=Decimal(str(row[8])),
        proposed_quantity=int(str(row[9])),
        approved_quantity=int(str(row[10])),
        statutory_bps=optional(11),
        execution_point_bps=optional(12),
        execution_upper_bps=optional(13),
        required_bps=optional(14),
        uncertainty_bps=optional(15),
        is_execution_censored=None if censored is None else bool(censored),
        failed_preconditions=tuple(part for part in str(row[17]).split(",") if part),
        reason=str(row[18]),
    )
