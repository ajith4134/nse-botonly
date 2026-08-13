"""What may actually be risked today — measured from the broker, bounded by the operator (`L1.17`).

`capital_configuration` holds a number the operator typed. That number is a **permission**: the most
they are willing to deploy. It is not a measurement, and a system that sizes against it is sizing
against money it merely hopes is there. This module produces the other half — the balance the broker
itself reports — and the answer sizing may use is `min(measured, ceiling)`.

The asymmetry between the two is the whole design:

* A **withdrawal, a loss or a margin block shrinks the deployable figure with no act from anyone**,
  because the measurement moved.
* A **deposit cannot silently increase it**, because the ceiling did not move. Raising the ceiling
  is an operator act, which is exactly the property `R.22` protects elsewhere.
* When the balance **cannot be read, this refuses**. Falling back to the ceiling would substitute a
  permission for a measurement, and that substitution is precisely how a system comes to size a
  trade it cannot fund.

**What counts as deployable, and what does not.** Kite reports per segment an `available` block
(`cash`, `collateral`, `live_balance`, `opening_balance`, `intraday_payin`, `adhoc_margin`), a
`utilised` block, and a `net`. `net` is the figure left after what is already committed, so `net` is
what this reads. **Collateral is deliberately excluded** even though the broker counts it toward
margin: pledged securities are not cash, they carry their own haircut, and treating them as
deployable is how a margin call arrives on a day the market has already moved against you. The
component figures are all recorded anyway, so the exclusion is visible rather than silent.

**Observed on the real account, 2026-08-13:** equity `net` was **minus 88 rupees** with `cash` at
zero,
commodity flat at zero — an account in debit. The resolver's answer for that day is a deployable
capital of **zero**, with the reason stated. That is the correct answer and not an error condition:
a negative balance is a measurement like any other, and the thing that must never happen is it being
rounded up to something tradeable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from nse_algo_trader.capital_configuration import TradingCapital

DEFAULT_DEPLOYABLE_CAPITAL_PATH = Path.home() / ".nse_algo_trader" / "deployable_capital.sqlite3"

# The broker's own segment names. Kept as data because the set is the broker's to change, and a
# segment this system does not recognise must be RECORDED rather than dropped — an unrecognised
# segment holding money is a measurement error, not an absence of money.
EQUITY_SEGMENT = "equity"
COMMODITY_SEGMENT = "commodity"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployable_capital_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    ceiling_rupees TEXT NOT NULL,
    measured_rupees TEXT NOT NULL,
    deployable_rupees TEXT NOT NULL,
    binding_side TEXT NOT NULL,
    source TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deployable_capital_by_session
    ON deployable_capital_observation (session_date);

CREATE TABLE IF NOT EXISTS deployable_capital_segment (
    observation_id INTEGER NOT NULL
        REFERENCES deployable_capital_observation (observation_id),
    segment TEXT NOT NULL,
    net_rupees TEXT NOT NULL,
    cash_rupees TEXT NOT NULL,
    collateral_rupees TEXT NOT NULL,
    live_balance_rupees TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (observation_id, segment)
);
"""


class DeployableCapitalError(Exception):
    """The deployable figure cannot be established, and guessing it would size a real order."""


class BrokerBalanceUnreadableError(DeployableCapitalError):
    """The broker could not be read, or answered something this module will not interpret."""


@dataclass(frozen=True, slots=True)
class SegmentBalance:
    """One segment's money, as the broker reports it, with the components kept apart."""

    segment: str
    net_rupees: Decimal
    cash_rupees: Decimal
    collateral_rupees: Decimal
    live_balance_rupees: Decimal
    enabled: bool

    @property
    def deployable_rupees(self) -> Decimal:
        """`net`, floored at zero — a debit is not negative buying power, it is none."""
        return max(self.net_rupees, Decimal(0))


class BrokerBalanceSource(Protocol):
    """Whatever can say how much money is actually at the broker."""

    @property
    def source_name(self) -> str: ...

    def segment_balances(self) -> tuple[SegmentBalance, ...]:
        """Raises `BrokerBalanceUnreadableError` rather than returning an empty tuple.

        The distinction matters as much here as it does in the order path: "the broker holds
        nothing" and "I could not ask" are the same value only to a system that then sizes against
        the difference.
        """
        ...


@dataclass(slots=True)
class KiteMarginsBalanceSource:
    """The real broker, through `margins()`."""

    kite_client: Any

    @property
    def source_name(self) -> str:
        return "kite_margins"

    def segment_balances(self) -> tuple[SegmentBalance, ...]:
        try:
            payload = self.kite_client.margins()
        # Every failure here is one refusal, and none of them is swallowed.
        except Exception as failure:
            raise BrokerBalanceUnreadableError(
                f"the broker's margins could not be read ({type(failure).__name__}: {failure}); "
                f"the ceiling is a permission and not a measurement, so it is not substituted here"
            ) from failure
        return segment_balances_from_margins_payload(payload)


def segment_balances_from_margins_payload(payload: Any) -> tuple[SegmentBalance, ...]:
    """Normalise Kite's `margins()` answer, or refuse to.

    Separate from the client so the real payload shape can be tested without a network call, and
    so the shape observed on the live account on 2026-08-13 is pinned by a test rather than by
    memory.
    """
    if not isinstance(payload, dict) or not payload:
        raise BrokerBalanceUnreadableError(
            f"margins() answered {type(payload).__name__} rather than a mapping of segments; an "
            f"answer this module cannot read is not a balance of zero"
        )
    balances = []
    for segment, detail in payload.items():
        if not isinstance(detail, dict):
            raise BrokerBalanceUnreadableError(
                f"segment {segment!r} answered {type(detail).__name__} rather than a mapping"
            )
        available = detail.get("available")
        if not isinstance(available, dict):
            raise BrokerBalanceUnreadableError(
                f"segment {segment!r} carries no readable 'available' block"
            )
        balances.append(
            SegmentBalance(
                segment=str(segment),
                net_rupees=_as_rupees(detail.get("net"), f"{segment}.net"),
                cash_rupees=_as_rupees(available.get("cash"), f"{segment}.available.cash"),
                collateral_rupees=_as_rupees(
                    available.get("collateral"), f"{segment}.available.collateral"
                ),
                live_balance_rupees=_as_rupees(
                    available.get("live_balance"), f"{segment}.available.live_balance"
                ),
                enabled=bool(detail.get("enabled", True)),
            )
        )
    return tuple(sorted(balances, key=lambda balance: balance.segment))


@dataclass(frozen=True, slots=True)
class DeployableCapital:
    """What may be risked today, and which of the two bounds decided it."""

    measured_at: datetime
    ceiling_rupees: Decimal
    measured_rupees: Decimal
    segments: tuple[SegmentBalance, ...]
    source: str

    @property
    def deployable_rupees(self) -> Decimal:
        return min(self.ceiling_rupees, self.measured_rupees)

    @property
    def binding_side(self) -> str:
        """Which bound is doing the work — the thing an operator actually wants to know."""
        if self.measured_rupees <= 0:
            return "broker_balance_exhausted"
        if self.measured_rupees < self.ceiling_rupees:
            return "broker_balance"
        if self.measured_rupees > self.ceiling_rupees:
            return "operator_ceiling"
        return "both_agree"

    @property
    def is_tradeable(self) -> bool:
        return self.deployable_rupees > 0

    def describe(self) -> str:
        segments = " · ".join(
            f"{balance.segment} net {balance.net_rupees}" for balance in self.segments
        )
        return (
            f"deployable Rs {self.deployable_rupees} "
            f"(broker {self.measured_rupees}, ceiling {self.ceiling_rupees}, "
            f"bound by {self.binding_side}) — {segments or 'no segments reported'}"
        )


def resolve_deployable_capital(
    ceiling: TradingCapital,
    balance_source: BrokerBalanceSource,
    *,
    measured_at: datetime,
    deployable_segments: tuple[str, ...] = (EQUITY_SEGMENT,),
) -> DeployableCapital:
    """Measure, bound, and refuse rather than guess.

    `deployable_segments` defaults to equity alone because that is where this system trades today;
    a segment is included by being named, never by being present, so money in a segment nothing
    trades cannot inflate the figure that sizes an equity order.
    """
    balances = balance_source.segment_balances()
    if not balances:
        raise BrokerBalanceUnreadableError(
            "the balance source reported no segments at all, which is not the same as reporting "
            "zero in each"
        )
    named = {balance.segment for balance in balances}
    missing = [segment for segment in deployable_segments if segment not in named]
    if missing:
        raise BrokerBalanceUnreadableError(
            f"the broker reported {sorted(named)} and said nothing about {missing}; a segment this "
            f"system sizes against must be measured, not assumed empty"
        )
    measured = sum(
        (
            balance.deployable_rupees
            for balance in balances
            if balance.segment in deployable_segments
        ),
        Decimal(0),
    )
    return DeployableCapital(
        measured_at=measured_at,
        ceiling_rupees=ceiling.total_rupees,
        measured_rupees=measured,
        segments=balances,
        source=balance_source.source_name,
    )


class DeployableCapitalStore:
    """Every measurement, kept — so the figure that sized a trade can be recovered afterwards."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._path = database_path or DEFAULT_DEPLOYABLE_CAPITAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def __enter__(self) -> DeployableCapitalStore:
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

    def record(self, capital: DeployableCapital, *, detail: str = "") -> int:
        cursor = self._connection.execute(
            "INSERT INTO deployable_capital_observation (measured_at, session_date,"
            " ceiling_rupees, measured_rupees, deployable_rupees, binding_side, source, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                capital.measured_at.isoformat(),
                capital.measured_at.date().isoformat(),
                str(capital.ceiling_rupees),
                str(capital.measured_rupees),
                str(capital.deployable_rupees),
                capital.binding_side,
                capital.source,
                detail,
            ),
        )
        observation_id = cursor.lastrowid
        if observation_id is None:  # pragma: no cover — sqlite always assigns one
            raise DeployableCapitalError("the store could not name the observation it just wrote")
        self._connection.executemany(
            "INSERT INTO deployable_capital_segment (observation_id, segment, net_rupees,"
            " cash_rupees, collateral_rupees, live_balance_rupees, enabled)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (
                    observation_id,
                    balance.segment,
                    str(balance.net_rupees),
                    str(balance.cash_rupees),
                    str(balance.collateral_rupees),
                    str(balance.live_balance_rupees),
                    int(balance.enabled),
                )
                for balance in capital.segments
            ],
        )
        self._connection.commit()
        return observation_id

    def latest(self) -> DeployableCapital | None:
        row = self._connection.execute(
            "SELECT * FROM deployable_capital_observation ORDER BY observation_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        segments = tuple(
            SegmentBalance(
                segment=segment_row["segment"],
                net_rupees=Decimal(segment_row["net_rupees"]),
                cash_rupees=Decimal(segment_row["cash_rupees"]),
                collateral_rupees=Decimal(segment_row["collateral_rupees"]),
                live_balance_rupees=Decimal(segment_row["live_balance_rupees"]),
                enabled=bool(segment_row["enabled"]),
            )
            for segment_row in self._connection.execute(
                "SELECT * FROM deployable_capital_segment WHERE observation_id = ?"
                " ORDER BY segment",
                (row["observation_id"],),
            )
        )
        return DeployableCapital(
            measured_at=datetime.fromisoformat(row["measured_at"]),
            ceiling_rupees=Decimal(row["ceiling_rupees"]),
            measured_rupees=Decimal(row["measured_rupees"]),
            segments=segments,
            source=row["source"],
        )

    def observation_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM deployable_capital_observation"
        ).fetchone()
        return int(row[0])


def _as_rupees(value: Any, field_name: str) -> Decimal:
    """Kite hands back binary floats; `Decimal(str(...))` is the only honest way in."""
    if value is None:
        raise BrokerBalanceUnreadableError(f"{field_name} was absent from the broker's answer")
    if isinstance(value, bool):
        raise BrokerBalanceUnreadableError(f"{field_name} was a boolean, not an amount")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as failure:
        raise BrokerBalanceUnreadableError(
            f"{field_name} was {value!r}, which is not an amount"
        ) from failure
