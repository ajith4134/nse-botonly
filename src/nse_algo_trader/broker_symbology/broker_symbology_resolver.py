"""`L0.17` — every broker's private name for the same security, resolved from theirs to ours.

*Generalised from "ICICI stock-code resolver": ICICI is one instance of a problem every
broker has. Kite says `738561`, Angel One says `2885`, Breeze says `RELIND`, Upstox says
`NSE_EQ|INE002A01018` — and none is derivable from another. Building only the ICICI case
would have left the reconciler single-sourced against the broker that is actually live.*

**Why this blocks real cross-source work.** `L0.15` reconciles bars from several brokers,
but a bar request carries one identifier per broker; without a resolver, every instrument
outside a hand-written handful is fetchable from exactly one broker, and "cross-source
reconciliation" quietly degrades to one source with extra steps. That was measured: the
first wired reconciliation ran against 25 instruments and Angel could name none of them.

**Resolution is a lookup over a persisted master, not a guess.** No fuzzy matching, no
string munging toward a plausible answer. A broker either publishes a mapping we can read
or the instrument is UNRESOLVED and reported as such — a wrong token silently fetches a
different company's price history, which is the one failure worse than fetching nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Protocol, runtime_checkable

from nse_algo_trader.broker_credentials import BrokerName


class SymbologyError(RuntimeError):
    """A symbology mapping could not be produced or trusted."""


@dataclass(frozen=True)
class BrokerSymbol:
    """One broker's identifier for one of our instruments."""

    broker: BrokerName
    exchange: str
    tradingsymbol: str
    broker_identifier: str
    broker_symbol: str
    series: str
    refreshed_on: date

    def __post_init__(self) -> None:
        if not self.broker_identifier:
            raise SymbologyError(
                f"empty identifier for {self.tradingsymbol} at {self.broker.value}"
            )


@runtime_checkable
class BrokerSymbologyResolver(Protocol):
    """What every broker's resolver must be."""

    @property
    def broker(self) -> BrokerName: ...

    def refresh(self) -> int:
        """Re-read the broker's master. Returns how many mappings are now held."""
        ...

    def resolve(self, tradingsymbol: str, exchange: str = "NSE") -> str | None:
        """The broker's identifier, or None when the broker does not publish one."""
        ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_symbol (
    broker            TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    tradingsymbol     TEXT NOT NULL,
    broker_identifier TEXT NOT NULL,
    broker_symbol     TEXT NOT NULL,
    series            TEXT NOT NULL,
    refreshed_on      TEXT NOT NULL,
    PRIMARY KEY (broker, exchange, tradingsymbol)
);
CREATE INDEX IF NOT EXISTS broker_symbol_by_broker ON broker_symbol (broker, refreshed_on);
"""


class BrokerSymbolStore:
    """Persisted mappings, so a 35 MB master is downloaded daily rather than per lookup.

    Keyed on `(broker, exchange, tradingsymbol)` and REPLACED on refresh rather than
    appended: a symbology mapping is a statement about the present, and keeping yesterday's
    token alongside today's would reintroduce the ambiguity the store exists to remove.
    Renames are `L0.08`'s job and are tracked there against ISIN, which is the stable key.
    """

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def __enter__(self) -> BrokerSymbolStore:
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

    def replace_for(self, broker: BrokerName, symbols: Iterable[BrokerSymbol]) -> int:
        rows = [
            (
                symbol.broker.value,
                symbol.exchange,
                symbol.tradingsymbol,
                symbol.broker_identifier,
                symbol.broker_symbol,
                symbol.series,
                symbol.refreshed_on.isoformat(),
            )
            for symbol in symbols
        ]
        if not rows:
            raise SymbologyError(
                f"refusing to wipe {broker.value} symbology with an empty master — "
                "an empty download is a fetch failure, not an empty exchange"
            )
        with self._connection:
            self._connection.execute(
                "DELETE FROM broker_symbol WHERE broker = ?", (broker.value,)
            )
            self._connection.executemany(
                "INSERT INTO broker_symbol (broker, exchange, tradingsymbol, "
                "broker_identifier, broker_symbol, series, refreshed_on) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def identifier_for(
        self, broker: BrokerName, tradingsymbol: str, exchange: str = "NSE"
    ) -> str | None:
        row = self._connection.execute(
            "SELECT broker_identifier FROM broker_symbol "
            "WHERE broker = ? AND exchange = ? AND tradingsymbol = ?",
            (broker.value, exchange, tradingsymbol),
        ).fetchone()
        return str(row[0]) if row else None

    def identifiers_for_all_brokers(
        self, tradingsymbol: str, exchange: str = "NSE"
    ) -> dict[BrokerName, str]:
        """Every broker that can name this instrument — what a `BarInstrument` needs."""
        return {
            BrokerName(broker): str(identifier)
            for broker, identifier in self._connection.execute(
                "SELECT broker, broker_identifier FROM broker_symbol "
                "WHERE exchange = ? AND tradingsymbol = ?",
                (exchange, tradingsymbol),
            )
        }

    def count_for(self, broker: BrokerName) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM broker_symbol WHERE broker = ?", (broker.value,)
            ).fetchone()[0]
        )

    def last_refreshed(self, broker: BrokerName) -> date | None:
        row = self._connection.execute(
            "SELECT MAX(refreshed_on) FROM broker_symbol WHERE broker = ?",
            (broker.value,),
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None


def coverage_against(
    store: BrokerSymbolStore, brokers: Iterable[BrokerName], tradingsymbols: Iterable[str]
) -> dict[BrokerName, tuple[int, int]]:
    """Per broker, how many of OUR symbols it can name — `(resolved, total)`.

    The number that matters for `L0.15`: reconciliation is only cross-source for the
    instruments every broker can name, and this makes that fraction visible instead of
    letting a single-sourced result look like agreement.
    """
    symbols = list(tradingsymbols)
    return {
        broker: (
            sum(1 for symbol in symbols if store.identifier_for(broker, symbol)),
            len(symbols),
        )
        for broker in brokers
    }
