"""What this project has learned about each broker, carried across sessions.

A consolidated feed is only better than its best source if it knows which source that is,
and that cannot be read off a single quote. It is a property of the broker, learned from
how its quotes have behaved against the others over time — which makes this store the state
that turns a comparison into an engine.

**Four things are learned per broker, because §1 of `docs/research/217` needs three
explanations told apart.**

- *pairwise difference variances, in BASIS POINTS* — `var(price_a - price_b)` for every
  pair, which is what
  the THREE-CORNERED HAT needs. Storing each broker's deviation from the others instead was
  the first design and it is mathematically dead: with two brokers, `var(a - b)` is the same
  number for both, so no amount of data can say which one is noisy. The property test caught
  it (the consensus came out worse than the better broker), and the fix is the technique
  time-and-frequency metrology already uses for exactly this problem — see
  `three_cornered_hat_variances` in the engine.
- *deviation mean* — the broker's bias, which IS identifiable pairwise and which averaging
  would otherwise bake into every consensus.
- *availability* — polls attempted against polls failed. A broker that answers half the time
  is not a half-good source, it is a source a router must plan around.
- *freshness* — how far behind its own exchange stamp its quotes arrive.
- *unchanged-while-others-moved* — the direct measure of a FROZEN feed. This is the one that
  separates "late but right" from "wrong": a lagging broker still moves, a stale one does
  not, and only the second should be excluded from the synthetic touch.

**Estimated with decay, not from all history.** A broker that was bad last month and is fine
today should be judged mostly on today. `river`'s exponentially-weighted mean and variance
carry that decay in O(1) memory per broker, and the decayed state is what is persisted —
so a restart resumes the estimate rather than restarting it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_RELIABILITY_PATH = Path("~/.nse_algo_trader/broker_reliability.sqlite3").expanduser()

MINIMUM_OBSERVATIONS_FOR_A_PRECISION_ESTIMATE = 30
"""Below this the variance is estimated from too few points to weight anything by.

Arithmetic rather than taste: an inverse-VARIANCE weight is a ratio of second moments, and a
second moment from ten samples has a standard error of the same order as itself. Under this
count the broker contributes on liquidity alone and the engine says so (`R.04` — the
algorithm is full-strength, only its ACTIVATION is gated)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_reliability (
    broker TEXT NOT NULL PRIMARY KEY,
    observation_count INTEGER NOT NULL,
    polls_attempted INTEGER NOT NULL,
    polls_failed INTEGER NOT NULL,
    deviation_mean_paise REAL NOT NULL,
    deviation_variance_paise REAL NOT NULL,
    freshness_mean_seconds REAL NOT NULL,
    unchanged_while_others_moved INTEGER NOT NULL,
    others_moved_occasions INTEGER NOT NULL,
    decayed_state TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_pair_difference (
    broker_a TEXT NOT NULL,
    broker_b TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    difference_mean_paise REAL NOT NULL,
    difference_variance_paise REAL NOT NULL,
    decayed_state TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (broker_a, broker_b)
);
CREATE TABLE IF NOT EXISTS broker_instrument_session (
    session_date TEXT NOT NULL,
    broker TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    comparisons INTEGER NOT NULL,
    divergent_comparisons INTEGER NOT NULL,
    frozen_comparisons INTEGER NOT NULL,
    mean_absolute_deviation_paise REAL NOT NULL,
    PRIMARY KEY (session_date, broker, trading_symbol)
);
"""


@dataclass(frozen=True, slots=True)
class BrokerReliability:
    """One broker's learned behaviour. Every field is measured; none is configured."""

    broker: str
    observation_count: int
    polls_attempted: int
    polls_failed: int
    deviation_mean_paise: float
    deviation_variance_paise: float
    freshness_mean_seconds: float
    unchanged_while_others_moved: int
    others_moved_occasions: int
    first_seen: datetime
    last_updated: datetime

    @property
    def failure_rate(self) -> float:
        return self.polls_failed / self.polls_attempted if self.polls_attempted else 0.0

    @property
    def unchanged_while_others_moved_rate(self) -> float:
        """How often this broker sat still while the rest of the market moved."""
        if not self.others_moved_occasions:
            return 0.0
        return self.unchanged_while_others_moved / self.others_moved_occasions

    @property
    def precision_is_mature(self) -> bool:
        return self.observation_count >= MINIMUM_OBSERVATIONS_FOR_A_PRECISION_ESTIMATE

    @property
    def precision(self) -> float | None:
        """`1 / variance`, or `None` while immature or degenerate.

        A zero variance would be an infinite weight, which is how one broker silently
        becomes the only broker. Two sources agreeing perfectly is common on a quiet
        instrument and must not be read as certainty.
        """
        if not self.precision_is_mature or self.deviation_variance_paise <= 0.0:
            return None
        return 1.0 / self.deviation_variance_paise


@dataclass(frozen=True, slots=True)
class BrokerInstrumentSessionQuality:
    """One broker's behaviour on one instrument on one day — what the gate reads."""

    session_date: date
    broker: str
    trading_symbol: str
    comparisons: int
    divergent_comparisons: int
    frozen_comparisons: int
    mean_absolute_deviation_paise: float

    @property
    def divergence_rate(self) -> float:
        return self.divergent_comparisons / self.comparisons if self.comparisons else 0.0

    @property
    def frozen_rate(self) -> float:
        return self.frozen_comparisons / self.comparisons if self.comparisons else 0.0


class BrokerReliabilityStore:
    """Persisted, decayed per-broker state plus the per-instrument-session ledger."""

    def __init__(
        self, database_path: Path = DEFAULT_RELIABILITY_PATH, *, write_behind: bool = True
    ) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_behind = write_behind
        # **Write-behind, and the arithmetic forced it.** A session's capture is ~45,000
        # rows and every one produces a pairwise observation and a per-broker update; the
        # first version opened a connection per update, which turns one session into
        # ~200,000 connection open/commit cycles. The working set is a few hundred rows, so
        # it is held in memory and written once. `flush()` is called by the session runner;
        # a caller that forgets loses nothing already on disk, only the run's own updates,
        # and `flush_on_exit` makes the common path automatic.
        self._open_connection: sqlite3.Connection | None = None
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        self.flush()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection for the store's lifetime, committed by `flush()`.

        Opening and committing per call is what the first version did, and a session's
        capture produces roughly 200,000 updates — the connection churn dominated the run.
        Reads inside this connection see this run's uncommitted writes, which is exactly
        the semantics the engine needs while it walks a session.
        """
        if self._open_connection is None:
            self._open_connection = sqlite3.connect(self._database_path)
            self._open_connection.execute("PRAGMA journal_mode=WAL")
            self._open_connection.execute("PRAGMA synchronous=NORMAL")
        previous_factory = self._open_connection.row_factory
        try:
            yield self._open_connection
        finally:
            self._open_connection.row_factory = previous_factory
        if not self._write_behind:
            self._open_connection.commit()

    def flush(self) -> None:
        """Commit everything this run has learned. Called by the session runner."""
        if self._open_connection is not None:
            self._open_connection.commit()

    def close(self) -> None:
        self.flush()
        if self._open_connection is not None:
            self._open_connection.close()
            self._open_connection = None

    def __enter__(self) -> BrokerReliabilityStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- reading ------------------------------------------------------------------------

    def reliability(self, broker: str) -> BrokerReliability:
        """What is known about `broker`, or an honest empty record for one never seen."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM broker_reliability WHERE broker = ?", (broker,)
            ).fetchone()
        if row is None:
            now = datetime.now(UTC)
            return BrokerReliability(
                broker=broker,
                observation_count=0,
                polls_attempted=0,
                polls_failed=0,
                deviation_mean_paise=0.0,
                deviation_variance_paise=0.0,
                freshness_mean_seconds=0.0,
                unchanged_while_others_moved=0,
                others_moved_occasions=0,
                first_seen=now,
                last_updated=now,
            )
        return _reliability_from_row(row)

    def all_reliabilities(self) -> tuple[BrokerReliability, ...]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM broker_reliability ORDER BY broker"
            ).fetchall()
        return tuple(_reliability_from_row(row) for row in rows)

    def instrument_session_quality(
        self, *, session_date: date
    ) -> tuple[BrokerInstrumentSessionQuality, ...]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM broker_instrument_session WHERE session_date = ? "
                "ORDER BY broker, trading_symbol",
                (session_date.isoformat(),),
            ).fetchall()
        return tuple(
            BrokerInstrumentSessionQuality(
                session_date=date.fromisoformat(row["session_date"]),
                broker=row["broker"],
                trading_symbol=row["trading_symbol"],
                comparisons=row["comparisons"],
                divergent_comparisons=row["divergent_comparisons"],
                frozen_comparisons=row["frozen_comparisons"],
                mean_absolute_deviation_paise=row["mean_absolute_deviation_paise"],
            )
            for row in rows
        )

    # -- writing ------------------------------------------------------------------------

    def observe_pair(
        self, broker_a: str, broker_b: str, difference_paise: float, *, at: datetime | None = None
    ) -> None:
        """Fold one price difference into the pair's decayed variance.

        Stored under a SORTED key so `(kite, angel_one)` and `(angel_one, kite)` are one
        pair; the sign of the mean is defined by that ordering and is read back the same way.
        """
        first, second = sorted((broker_a, broker_b))
        signed = difference_paise if (first, second) == (broker_a, broker_b) else -difference_paise
        now = at or datetime.now(UTC)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM broker_pair_difference WHERE broker_a = ? AND broker_b = ?",
                (first, second),
            ).fetchone()
            state = _DecayedMoments.from_json(row["decayed_state"]) if row else _DecayedMoments()
            state.update_deviation(signed)
            connection.execute(
                "INSERT OR REPLACE INTO broker_pair_difference VALUES (?,?,?,?,?,?,?)",
                (
                    first,
                    second,
                    (row["observation_count"] if row else 0) + 1,
                    state.deviation_mean,
                    state.deviation_variance,
                    state.to_json(),
                    now.isoformat(),
                ),
            )

    def pair_difference_variances(self) -> dict[tuple[str, str], tuple[float, int]]:
        """`(broker_a, broker_b) -> (variance, observation count)`, keys sorted."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM broker_pair_difference").fetchall()
        return {
            (row["broker_a"], row["broker_b"]): (
                float(row["difference_variance_paise"]),
                int(row["observation_count"]),
            )
            for row in rows
        }

    def observe(
        self,
        broker: str,
        *,
        deviation_paise: float | None,
        freshness_seconds: float | None,
        failed: bool,
        others_moved: bool,
        stayed_unchanged: bool,
        at: datetime | None = None,
    ) -> None:
        """Fold one comparison into the broker's decayed state.

        `deviation_paise` is `None` for a poll that produced no price: availability still
        updates, precision does not, because a missing quote says nothing about accuracy.
        """
        now = at or datetime.now(UTC)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM broker_reliability WHERE broker = ?", (broker,)
            ).fetchone()
            state = _DecayedMoments.from_json(row["decayed_state"]) if row else _DecayedMoments()
            observation_count = row["observation_count"] if row else 0
            polls_attempted = (row["polls_attempted"] if row else 0) + 1
            polls_failed = (row["polls_failed"] if row else 0) + (1 if failed else 0)
            unchanged = (row["unchanged_while_others_moved"] if row else 0) + (
                1 if (others_moved and stayed_unchanged) else 0
            )
            occasions = (row["others_moved_occasions"] if row else 0) + (
                1 if others_moved else 0
            )
            if deviation_paise is not None:
                state.update_deviation(deviation_paise)
                observation_count += 1
            if freshness_seconds is not None:
                state.update_freshness(freshness_seconds)
            connection.execute(
                "INSERT OR REPLACE INTO broker_reliability VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    broker,
                    observation_count,
                    polls_attempted,
                    polls_failed,
                    state.deviation_mean,
                    state.deviation_variance,
                    state.freshness_mean,
                    unchanged,
                    occasions,
                    state.to_json(),
                    (row["first_seen"] if row else now.isoformat()),
                    now.isoformat(),
                ),
            )

    def observe_instrument_session(
        self,
        *,
        session_date: date,
        broker: str,
        trading_symbol: str,
        diverged: bool,
        frozen: bool,
        absolute_deviation_paise: float | None,
    ) -> None:
        """The per-instrument ledger the admissibility gate reads."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM broker_instrument_session WHERE session_date = ? AND "
                "broker = ? AND trading_symbol = ?",
                (session_date.isoformat(), broker, trading_symbol),
            ).fetchone()
            comparisons = (row["comparisons"] if row else 0) + 1
            divergent = (row["divergent_comparisons"] if row else 0) + (1 if diverged else 0)
            frozen_count = (row["frozen_comparisons"] if row else 0) + (1 if frozen else 0)
            previous_mean = row["mean_absolute_deviation_paise"] if row else 0.0
            previous_count = row["comparisons"] if row else 0
            mean_absolute = (
                (previous_mean * previous_count + abs(absolute_deviation_paise)) / comparisons
                if absolute_deviation_paise is not None
                else previous_mean
            )
            connection.execute(
                "INSERT OR REPLACE INTO broker_instrument_session VALUES (?,?,?,?,?,?,?)",
                (
                    session_date.isoformat(),
                    broker,
                    trading_symbol,
                    comparisons,
                    divergent,
                    frozen_count,
                    mean_absolute,
                ),
            )


PAIR_VARIANCE_DECAY = 0.005
"""Weight given to each new pairwise difference.

Memory of ~200 comparisons, which a session of thousands of sweeps refreshes many times over.
It also SETS THE RESOLUTION of the three-cornered hat: the relative standard error of an
exponentially-weighted variance is about `sqrt(decay)`, so 7% here. Measured consequence of
the previous 0.01 — on sources with true sigmas of 1, 4 and 12 paise the estimator noise
alone pushed the quietest source's solved variance ABOVE the middle one's, i.e. the ordering
the decomposition exists to produce was inverted by its own noise. A source quieter than the
resolution is floored, and the floor is honest about being a resolution limit rather than a
measurement."""


@dataclass
class _DecayedMoments:
    """Exponentially-weighted mean and variance, kept in a form that survives a restart.

    Written out rather than taken from `river` so the persisted state is a handful of plain
    numbers this project owns. `river.stats.EWVar` computes the same recursion; the reason
    not to depend on it here is purely that its internal state is not part of its public
    contract, and the state is exactly what has to be stored.
    """

    decay: float = PAIR_VARIANCE_DECAY
    """Weight given to each new observation; see `PAIR_VARIANCE_DECAY`."""

    deviation_mean: float = 0.0
    deviation_variance: float = 0.0
    freshness_mean: float = 0.0
    seen: int = 0

    def update_deviation(self, value: float) -> None:
        self.seen += 1
        if self.seen == 1:
            self.deviation_mean = value
            self.deviation_variance = 0.0
            return
        difference = value - self.deviation_mean
        increment = self.decay * difference
        self.deviation_mean += increment
        # West's exponentially-weighted variance recursion: the new variance is the decayed
        # old one plus the weighted squared innovation.
        self.deviation_variance = (1.0 - self.decay) * (
            self.deviation_variance + difference * increment
        )

    def update_freshness(self, value: float) -> None:
        if self.freshness_mean == 0.0:
            self.freshness_mean = value
            return
        self.freshness_mean += self.decay * (value - self.freshness_mean)

    def to_json(self) -> str:
        return json.dumps(
            {
                "decay": self.decay,
                "deviation_mean": self.deviation_mean,
                "deviation_variance": self.deviation_variance,
                "freshness_mean": self.freshness_mean,
                "seen": self.seen,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> _DecayedMoments:
        payload = json.loads(raw)
        return cls(
            decay=payload["decay"],
            deviation_mean=payload["deviation_mean"],
            deviation_variance=payload["deviation_variance"],
            freshness_mean=payload["freshness_mean"],
            seen=payload["seen"],
        )


def _reliability_from_row(row: sqlite3.Row) -> BrokerReliability:
    return BrokerReliability(
        broker=row["broker"],
        observation_count=row["observation_count"],
        polls_attempted=row["polls_attempted"],
        polls_failed=row["polls_failed"],
        deviation_mean_paise=row["deviation_mean_paise"],
        deviation_variance_paise=row["deviation_variance_paise"],
        freshness_mean_seconds=row["freshness_mean_seconds"],
        unchanged_while_others_moved=row["unchanged_while_others_moved"],
        others_moved_occasions=row["others_moved_occasions"],
        first_seen=datetime.fromisoformat(row["first_seen"]),
        last_updated=datetime.fromisoformat(row["last_updated"]),
    )
