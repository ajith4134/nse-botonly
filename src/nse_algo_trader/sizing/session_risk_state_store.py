"""What the session has already done to the book — and the latches that stop it (`L7.03`).

Specification: `docs/research/227_position_sizing_and_risk_gate_spec.md` §5.

This is the state the risk gate carries between decisions, and carrying it is what makes the gate an
engine rather than a function of its arguments. Four things are remembered:

* **realised session P&L** — what the day has actually cost or made;
* **peak equity** — the high-water mark the drawdown is measured from;
* **open exposure** per symbol and in aggregate — what is already at risk;
* **order timestamps** — the rolling window the rate limit reads.

**A restart must not clear a halt.** Everything here persists to SQLite, because process bounces are
exactly what follows a bad morning: a supervisor restart, an operator restart, a crash on the same
bad tick that caused the loss. A daily-loss latch that forgets itself when the process comes back is
not a latch, it is a delay. This is the single most important property in the module and it has its
own test.

**A latch, once tripped, stays tripped for the session.** Clearing one is an operator act (`R.22`).
The system may halt itself; it may never un-halt itself, because the condition that tripped the
latch
is precisely the condition under which its own judgement is least trustworthy.

**The daily-loss limit is derived, not chosen.** `limit = z * sigma_daily_book`, where
`sigma_daily_book` is the book's own realised daily volatility and `z` is set by the tolerated
frequency of a false halt across a trading year — a Bonferroni-style quantile, not a preference. A
book with no daily history yet cannot form `sigma_daily_book`, and `R.04` governs what happens then:
the algorithm is not reduced and no rupee figure is invented. The limit reports itself
**unavailable**, the gate treats an unavailable limit as *not yet active*, and the maturity ladder
that activates it is visible rather than implied.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import TracebackType
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_SESSION_RISK_STATE_PATH = (
    Path.home() / ".nse_algo_trader" / "session_risk_state.sqlite3"
)

WRITE_LOCK_TIMEOUT_SECONDS = 30.0
"""How long one writer waits for another. The same discipline `L1.18` learned the hard way: the
fold, the decision and the insert happen inside one `BEGIN IMMEDIATE`, so two decisions racing the
same session state cannot both pass a limit only one of them fits under."""

MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT = 20
"""Below this the book's daily volatility cannot be estimated, so the daily-loss limit is
UNAVAILABLE.

`R.04`: the algorithm is not reduced for thin data and no rupee figure is invented — the limit
simply
is not active yet, and says so. Twenty sessions is the same order as the volatility estimator's
thirty observations and for the same reason: the standard error of a second moment at n=20 is
already
~16%, and below it the limit would halt the book on noise as readily as on loss.
"""

FALSE_HALT_SESSIONS_PER_YEAR = Decimal(1)
"""How often a correct book may be halted by chance in a 250-session year.

This is the operator-facing form of the `z` in `limit = z * sigma`: instead of choosing a quantile,
one states how often a spurious halt is tolerable, and the quantile follows. One per year gives a
one-tailed tail probability of 1/250, hence `z` around 2.65 — derived below rather than written
down, so changing the tolerance changes the limit with no edit here.
"""

TRADING_SESSIONS_PER_YEAR = 250
"""NSE trades roughly 250 sessions a year. A calendar fact, not a tuning knob."""


class SessionRiskStateError(Exception):
    """The session's risk state cannot be established, and guessing it would permit a trade."""


class RiskLatch(Enum):
    """A halt that has been tripped. Cleared only by an operator (`R.22`)."""

    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_risk_event (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    trading_symbol TEXT NOT NULL DEFAULT '',
    amount_rupees TEXT NOT NULL DEFAULT '0',
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_risk_event_by_session
    ON session_risk_event (session_date);

CREATE TABLE IF NOT EXISTS session_risk_latch (
    session_date TEXT NOT NULL,
    latch TEXT NOT NULL,
    tripped_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by TEXT,
    PRIMARY KEY (session_date, latch)
);

CREATE TABLE IF NOT EXISTS session_equity_mark (
    session_date TEXT NOT NULL PRIMARY KEY,
    opening_equity_rupees TEXT NOT NULL,
    peak_equity_rupees TEXT NOT NULL,
    realised_pnl_rupees TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_book_result (
    session_date TEXT NOT NULL PRIMARY KEY,
    realised_pnl_rupees TEXT NOT NULL,
    opening_equity_rupees TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class SessionRiskState:
    """Everything the gate needs to know about what today has already done."""

    session_date: date
    opening_equity_rupees: Decimal
    peak_equity_rupees: Decimal
    realised_pnl_rupees: Decimal
    open_exposure_rupees: Decimal
    exposure_by_symbol: tuple[tuple[str, Decimal], ...]
    orders_in_rate_window: int
    tripped_latches: tuple[RiskLatch, ...]

    @property
    def current_equity_rupees(self) -> Decimal:
        return self.opening_equity_rupees + self.realised_pnl_rupees

    @property
    def drawdown_rupees(self) -> Decimal:
        """Peak to current, floored at zero. A book above its peak is not in drawdown."""
        return max(self.peak_equity_rupees - self.current_equity_rupees, Decimal(0))

    @property
    def drawdown_fraction(self) -> Decimal:
        if self.peak_equity_rupees <= 0:
            return Decimal(0)
        return self.drawdown_rupees / self.peak_equity_rupees

    @property
    def is_halted(self) -> bool:
        return bool(self.tripped_latches)

    def describe(self) -> str:
        latches = (
            ", ".join(latch.value for latch in self.tripped_latches) if self.tripped_latches else
            "none"
        )
        return (
            f"{self.session_date.isoformat()}: equity Rs {self.current_equity_rupees} "
            f"(opened {self.opening_equity_rupees}, peak {self.peak_equity_rupees}), realised "
            f"Rs {self.realised_pnl_rupees}, drawdown Rs {self.drawdown_rupees} "
            f"({(self.drawdown_fraction * 100).quantize(Decimal('0.01'))}%), exposure "
            f"Rs {self.open_exposure_rupees} across {len(self.exposure_by_symbol)} symbol(s), "
            f"{self.orders_in_rate_window} order(s) in the rate window; latches: {latches}"
        )


@dataclass(frozen=True, slots=True)
class DailyLossLimit:
    """The derived limit, or a stated reason it is not yet available."""

    limit_rupees: Decimal | None
    sessions_observed: int
    sigma_daily_rupees: Decimal | None
    z_quantile: Decimal
    unavailable_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.limit_rupees is not None

    def describe(self) -> str:
        if not self.is_active:
            return f"daily-loss limit NOT ACTIVE: {self.unavailable_reason}"
        return (
            f"daily-loss limit Rs {self.limit_rupees} = "
            f"{self.z_quantile.quantize(Decimal('0.001'))} x daily sigma Rs "
            f"{self.sigma_daily_rupees} over {self.sessions_observed} sessions"
        )


def false_halt_quantile(
    *,
    sessions_per_year: int = TRADING_SESSIONS_PER_YEAR,
    false_halts_per_year: Decimal = FALSE_HALT_SESSIONS_PER_YEAR,
) -> Decimal:
    """The `z` implied by tolerating `false_halts_per_year` spurious halts.

    One states how often a correct book may be stopped by chance; the quantile follows. This is the
    inverse of choosing `z = 3` because three looks careful — the operator-facing quantity is the
    frequency, and the statistic is derived from it.

    The normal inverse CDF is approximated by Acklam's rational method, which is accurate to about
    1.15e-9 across the tail — far finer than the daily volatility estimate it multiplies, so the
    approximation is not the binding error anywhere in this calculation.
    """
    if false_halts_per_year <= 0 or sessions_per_year <= 0:
        raise SessionRiskStateError(
            "a tolerated false-halt frequency must be positive; zero tolerance implies an infinite "
            "limit, which is the same as having no limit at all"
        )
    tail = false_halts_per_year / Decimal(sessions_per_year)
    if tail >= 1:
        raise SessionRiskStateError(
            f"tolerating {false_halts_per_year} false halts in {sessions_per_year} sessions means "
            "halting always; the limit would be zero"
        )
    return _normal_inverse_cdf(Decimal(1) - tail)


def _normal_inverse_cdf(probability: Decimal) -> Decimal:
    """Acklam's rational approximation to the standard normal quantile function."""
    a = [
        Decimal("-3.969683028665376e+01"),
        Decimal("2.209460984245205e+02"),
        Decimal("-2.759285104469687e+02"),
        Decimal("1.383577518672690e+02"),
        Decimal("-3.066479806614716e+01"),
        Decimal("2.506628277459239e+00"),
    ]
    b = [
        Decimal("-5.447609879822406e+01"),
        Decimal("1.615858368580409e+02"),
        Decimal("-1.556989798598866e+02"),
        Decimal("6.680131188771972e+01"),
        Decimal("-1.328068155288572e+01"),
    ]
    c = [
        Decimal("-7.784894002430293e-03"),
        Decimal("-3.223964580411365e-01"),
        Decimal("-2.400758277161838e+00"),
        Decimal("-2.549732539343734e+00"),
        Decimal("4.374664141464968e+00"),
        Decimal("2.938163982698783e+00"),
    ]
    d = [
        Decimal("7.784695709041462e-03"),
        Decimal("3.224671290700398e-01"),
        Decimal("2.445134137142996e+00"),
        Decimal("3.754408661907416e+00"),
    ]
    low = Decimal("0.02425")
    high = Decimal(1) - low
    if probability < low:
        q = (Decimal(-2) * probability.ln()).sqrt()
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + Decimal(1)
        )
    if probability > high:
        q = (Decimal(-2) * (Decimal(1) - probability).ln()).sqrt()
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + Decimal(1)
        )
    q = probability - Decimal("0.5")
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + Decimal(1)
    )


class SessionRiskStateStore:
    """The carried state, persisted — so a restart cannot clear a halt."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._path = database_path or DEFAULT_SESSION_RISK_STATE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path, isolation_level=None, timeout=WRITE_LOCK_TIMEOUT_SECONDS
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(f"PRAGMA busy_timeout = {int(WRITE_LOCK_TIMEOUT_SECONDS * 1000)}")
        self._connection.executescript(_SCHEMA)

    def __enter__(self) -> SessionRiskStateStore:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """One writer at a time — the lesson `L1.18`'s review taught at some expense."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # ------------------------------------------------------------------ writes

    def open_session(
        self, *, session_date: date, opening_equity_rupees: Decimal, occurred_at: datetime
    ) -> None:
        """Record the equity a session began with. Idempotent — reopening does not reset it.

        Idempotence is the point: a restart calls this again, and a second call that overwrote the
        opening mark would erase the loss the day had already taken and release the latch by
        arithmetic.
        """
        _require_decimal(opening_equity_rupees, "opening equity")
        with self._write_transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM session_equity_mark WHERE session_date = ?",
                (session_date.isoformat(),),
            ).fetchone()
            if existing is not None:
                return
            self._connection.execute(
                "INSERT INTO session_equity_mark (session_date, opening_equity_rupees, "
                "peak_equity_rupees, realised_pnl_rupees) VALUES (?, ?, ?, '0')",
                (
                    session_date.isoformat(),
                    str(opening_equity_rupees),
                    str(opening_equity_rupees),
                ),
            )
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind="SESSION_OPENED",
                amount=opening_equity_rupees,
                reason=f"session opened with Rs {opening_equity_rupees}",
            )

    def record_realised_pnl(
        self,
        amount_rupees: Decimal,
        *,
        session_date: date,
        trading_symbol: str,
        occurred_at: datetime,
        reason: str,
    ) -> SessionRiskState:
        """Fold a realised result into the session and re-mark the peak."""
        _require_decimal(amount_rupees, "realised P&L")
        with self._write_transaction():
            mark = self._require_session_mark(session_date)
            realised = Decimal(mark["realised_pnl_rupees"]) + amount_rupees
            equity = Decimal(mark["opening_equity_rupees"]) + realised
            peak = max(Decimal(mark["peak_equity_rupees"]), equity)
            self._connection.execute(
                "UPDATE session_equity_mark SET realised_pnl_rupees = ?, peak_equity_rupees = ? "
                "WHERE session_date = ?",
                (str(realised), str(peak), session_date.isoformat()),
            )
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind="REALISED_PNL",
                trading_symbol=trading_symbol,
                amount=amount_rupees,
                reason=reason,
            )
        return self.state_for(session_date=session_date, now=occurred_at)

    def record_exposure_change(
        self,
        amount_rupees: Decimal,
        *,
        session_date: date,
        trading_symbol: str,
        occurred_at: datetime,
        reason: str,
    ) -> None:
        """Positive when a position opens, negative when it closes."""
        _require_decimal(amount_rupees, "exposure change")
        with self._write_transaction():
            self._require_session_mark(session_date)
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind="EXPOSURE_CHANGE",
                trading_symbol=trading_symbol,
                amount=amount_rupees,
                reason=reason,
            )

    def record_order_sent(
        self, *, session_date: date, trading_symbol: str, occurred_at: datetime
    ) -> None:
        """One order on the wire — what the rate window counts."""
        with self._write_transaction():
            self._require_session_mark(session_date)
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind="ORDER_SENT",
                trading_symbol=trading_symbol,
                reason="order sent",
            )

    def trip_latch(
        self, latch: RiskLatch, *, session_date: date, occurred_at: datetime, reason: str
    ) -> None:
        """Halt the session. Idempotent: a latch already tripped keeps its original reason."""
        if not reason.strip():
            raise SessionRiskStateError(
                f"a {latch.value} latch with no stated reason is refused; a halt nobody can "
                "explain is a halt nobody can safely clear"
            )
        with self._write_transaction():
            self._connection.execute(
                "INSERT INTO session_risk_latch (session_date, latch, tripped_at, reason) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (session_date, latch) DO NOTHING",
                (session_date.isoformat(), latch.value, occurred_at.isoformat(), reason.strip()),
            )
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind=f"LATCH_{latch.value}",
                reason=reason.strip(),
            )

    def clear_latch(
        self, latch: RiskLatch, *, session_date: date, occurred_at: datetime, cleared_by: str
    ) -> None:
        """An OPERATOR act (`R.22`). The system can halt itself and can never un-halt itself.

        The condition that tripped a latch is precisely the condition under which this system's own
        judgement is least trustworthy, so the key that releases it is deliberately not one it
        holds.
        """
        if not cleared_by.strip():
            raise SessionRiskStateError(
                "clearing a latch requires the operator's identity; an anonymous clear is "
                "indistinguishable from the system clearing itself, which R.22 forbids"
            )
        with self._write_transaction():
            self._connection.execute(
                "UPDATE session_risk_latch SET cleared_at = ?, cleared_by = ? "
                "WHERE session_date = ? AND latch = ? AND cleared_at IS NULL",
                (
                    occurred_at.isoformat(),
                    cleared_by.strip(),
                    session_date.isoformat(),
                    latch.value,
                ),
            )
            self._record_event(
                session_date=session_date,
                occurred_at=occurred_at,
                kind=f"LATCH_CLEARED_{latch.value}",
                reason=f"cleared by {cleared_by.strip()}",
            )

    def close_session(self, *, session_date: date) -> None:
        """Fold the session into the daily history the loss limit is derived from."""
        with self._write_transaction():
            mark = self._require_session_mark(session_date)
            self._connection.execute(
                "INSERT INTO daily_book_result (session_date, realised_pnl_rupees, "
                "opening_equity_rupees) VALUES (?, ?, ?) ON CONFLICT (session_date) DO UPDATE SET "
                "realised_pnl_rupees = excluded.realised_pnl_rupees",
                (
                    session_date.isoformat(),
                    mark["realised_pnl_rupees"],
                    mark["opening_equity_rupees"],
                ),
            )

    # ------------------------------------------------------------------- reads

    def state_for(
        self, *, session_date: date, now: datetime, rate_window: timedelta = timedelta(seconds=1)
    ) -> SessionRiskState:
        """Everything the gate reads, folded from the log rather than cached anywhere."""
        mark = self._connection.execute(
            "SELECT opening_equity_rupees, peak_equity_rupees, realised_pnl_rupees "
            "FROM session_equity_mark WHERE session_date = ?",
            (session_date.isoformat(),),
        ).fetchone()
        if mark is None:
            raise SessionRiskStateError(
                f"session {session_date.isoformat()} has never been opened, so there is no equity "
                "to measure a loss or a drawdown against. 'Not opened' and 'opened flat' are the "
                "same number only to a system that then permits a trade on the difference"
            )
        exposures: dict[str, Decimal] = {}
        for row in self._connection.execute(
            "SELECT trading_symbol, amount_rupees FROM session_risk_event "
            "WHERE session_date = ? AND kind = 'EXPOSURE_CHANGE' ORDER BY sequence",
            (session_date.isoformat(),),
        ):
            symbol = str(row["trading_symbol"])
            exposures[symbol] = exposures.get(symbol, Decimal(0)) + Decimal(row["amount_rupees"])
        open_exposures = {
            symbol: amount for symbol, amount in exposures.items() if amount != 0
        }
        # Compared in UTC, because the comparison is a LEXICOGRAPHIC one over ISO strings and an
        # offset changes the text without changing the instant. Stamps were written as `+05:30` and
        # the window start was rendered in the caller's zone: from Asia/Tokyo the window reported
        # ZERO orders — the rate limit never fired and the 10/sec registration threshold was
        # unguarded — and from UTC it counted five-hour-old orders as current (`A.105` finding 5).
        window_start = (now - rate_window).astimezone(UTC)
        orders = self._connection.execute(
            "SELECT COUNT(*) AS sent FROM session_risk_event WHERE session_date = ? "
            "AND kind = 'ORDER_SENT' AND occurred_at > ?",
            (session_date.isoformat(), window_start.isoformat()),
        ).fetchone()
        latches = tuple(
            RiskLatch(row["latch"])
            for row in self._connection.execute(
                "SELECT latch FROM session_risk_latch WHERE session_date = ? AND cleared_at IS NULL"
                " ORDER BY latch",
                (session_date.isoformat(),),
            )
        )
        return SessionRiskState(
            session_date=session_date,
            opening_equity_rupees=Decimal(mark["opening_equity_rupees"]),
            peak_equity_rupees=Decimal(mark["peak_equity_rupees"]),
            realised_pnl_rupees=Decimal(mark["realised_pnl_rupees"]),
            open_exposure_rupees=sum(open_exposures.values(), Decimal(0)),
            exposure_by_symbol=tuple(sorted(open_exposures.items())),
            orders_in_rate_window=int(orders["sent"]),
            tripped_latches=latches,
        )

    def daily_loss_limit(
        self,
        *,
        as_of: date,
        minimum_sessions: int = MINIMUM_SESSIONS_FOR_A_DAILY_LIMIT,
        false_halts_per_year: Decimal = FALSE_HALT_SESSIONS_PER_YEAR,
    ) -> DailyLossLimit:
        """`z * sigma_daily_book`, derived — or an honest statement that it is not yet available.

        `R.04` in its correct form: the algorithm is complete from the first day and its ACTIVATION
        waits for evidence. A book with nineteen sessions of history does not get a smaller limit;
        it gets no limit and a stated reason, because a limit fitted to nineteen numbers would halt
        on noise as readily as on loss.
        """
        z = false_halt_quantile(false_halts_per_year=false_halts_per_year)
        results = [
            Decimal(row["realised_pnl_rupees"])
            for row in self._connection.execute(
                "SELECT realised_pnl_rupees FROM daily_book_result WHERE session_date < ? "
                "ORDER BY session_date",
                (as_of.isoformat(),),
            )
        ]
        if len(results) < minimum_sessions:
            return DailyLossLimit(
                limit_rupees=None,
                sessions_observed=len(results),
                sigma_daily_rupees=None,
                z_quantile=z,
                unavailable_reason=(
                    f"{len(results)} closed session(s) of daily history, below the "
                    f"{minimum_sessions} needed to estimate the book's daily volatility. The limit "
                    "is NOT ACTIVE rather than guessed: a rupee figure nobody measured would halt "
                    "the book on noise as readily as on loss"
                ),
            )
        mean = sum(results, Decimal(0)) / Decimal(len(results))
        variance = sum(((value - mean) ** 2 for value in results), Decimal(0)) / Decimal(
            len(results) - 1
        )
        sigma = variance.sqrt()
        if sigma <= 0:
            # Twenty sessions that all returned the same figure — the ordinary state of a paper
            # book's first month, when nothing has traded — give a variance of zero and therefore a
            # limit of ZERO, which is ACTIVE and halts the book on the first paisa of loss. That
            # halt is a latch only an operator can clear (`R.22`): a permanent stop earned by
            # rounding. `R.04` counts sessions; it must count INFORMATION (`A.105` finding 6).
            return DailyLossLimit(
                limit_rupees=None,
                sessions_observed=len(results),
                sigma_daily_rupees=sigma,
                z_quantile=z,
                unavailable_reason=(
                    f"{len(results)} closed sessions all returned the same result, so the book's "
                    "daily volatility is zero and the derived limit would be Rs 0 — active, and "
                    "tripped by the first paisa. The limit is NOT ACTIVE until the sessions differ"
                ),
            )
        return DailyLossLimit(
            limit_rupees=z * sigma,
            sessions_observed=len(results),
            sigma_daily_rupees=sigma,
            z_quantile=z,
        )

    # ---------------------------------------------------------------- internal

    def _require_session_mark(self, session_date: date) -> sqlite3.Row:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT opening_equity_rupees, peak_equity_rupees, realised_pnl_rupees "
            "FROM session_equity_mark WHERE session_date = ?",
            (session_date.isoformat(),),
        ).fetchone()
        if row is None:
            raise SessionRiskStateError(
                f"session {session_date.isoformat()} has not been opened; nothing may be recorded "
                "against a session whose starting equity is unknown"
            )
        return row

    def _record_event(
        self,
        *,
        session_date: date,
        occurred_at: datetime,
        kind: str,
        reason: str,
        trading_symbol: str = "",
        amount: Decimal = Decimal(0),
    ) -> None:
        if occurred_at.tzinfo is None:
            raise SessionRiskStateError(
                "a naive timestamp cannot be ordered against a stamped one, and the rate window "
                "depends on the order being real"
            )
        self._connection.execute(
            "INSERT INTO session_risk_event (session_date, occurred_at, kind, trading_symbol, "
            "amount_rupees, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_date.isoformat(),
                occurred_at.astimezone(UTC).isoformat(),
                kind,
                trading_symbol,
                str(amount),
                reason,
            ),
        )


def _require_decimal(value: Decimal, description: str) -> None:
    if not isinstance(value, Decimal):
        raise SessionRiskStateError(
            f"{description} must be a Decimal; a float here would put binary rounding between the "
            "book's losses and the limit that stops them"
        )
