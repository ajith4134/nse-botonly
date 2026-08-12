"""Rule facts DERIVED from what the exchange actually published, not from a circular.

`A.80` established the grade ladder: `OBSERVED_FROM_EXCHANGE_DATA` outranks
`PRIMARY_CIRCULAR`, because a circular says what was *announced* and the instrument master
says what was *in force*. This module is what populates that grade — and it closes two of
the five families `L0.31` shipped uncovered (`tick_size`, `lot_size`) with real data rather
than with a compilation project.

**Lazy, and the reason is arithmetic.** `market_data.instrument_master` holds 227,535 dated
rows across ~105,000 symbols. Materialising a `MarketRuleRecord` per symbol per family
would put a quarter of a million objects into a list that every subsequent query scans
linearly. Instead this is an `ObservationalRuleSource`: the store asks it only about the
symbol being resolved, which is one indexed lookup — measured at **0.02s** for a single
symbol against the full 227,535-row table.

**What it actually computes is a change detector.** The instrument master is a daily
snapshot with no validity interval; the rule history is the sequence of values a symbol
held and the days they changed. So consecutive snapshots are compressed into runs: a
symbol whose tick size is 0.10 on every observed day yields ONE record spanning them, and
a symbol that changes yields a closed interval for the old value and an open one for the
new. That is the same operation a slowly-changing dimension performs, done on read.

**The honest limit, carried in the record rather than in a footnote.** A snapshot source
can only speak for days it observed. This host began capturing on 2026-08-11, so every
derived interval starts there — and the interval's `effective_from` is the first day
OBSERVED, not the day the rule began, which may be years earlier. `observation_window()`
reports that boundary so the coverage surface can show it, and the store still refuses any
date before it rather than extrapolating a snapshot backwards.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    MarketRuleRecord,
    RuleFamily,
    RuleScope,
    RuleValueKind,
)

DEFAULT_MARKET_DATA_PATH = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

_FAMILIES = frozenset({RuleFamily.TICK_SIZE, RuleFamily.LOT_SIZE})

_COLUMN_BY_FAMILY = {
    RuleFamily.TICK_SIZE: "tick_size",
    RuleFamily.LOT_SIZE: "lot_size",
}
_KIND_BY_FAMILY = {
    RuleFamily.TICK_SIZE: RuleValueKind.DECIMAL_FRACTION,
    RuleFamily.LOT_SIZE: RuleValueKind.INTEGER,
}


class InstrumentMasterUnavailableError(RuntimeError):
    """The retained market-data store is missing or has no instrument master.

    Raised rather than returning nothing: a source that silently yields zero facts is
    indistinguishable from one that correctly found none, and the store would then report
    the family as uncovered for a reason that has nothing to do with the exchange.
    """


@dataclass(frozen=True)
class _ObservedValue:
    observed_on: date
    value: str


class InstrumentMasterRuleObserver:
    """Derives tick-size and lot-size history from the daily instrument-master snapshots.

    Satisfies `ObservationalRuleSource`. Scope must pin a `symbol` — tick size is a
    per-instrument property and answering a segment-wide question with one symbol's value
    would be a fabrication, so an unpinned scope yields nothing rather than a guess.
    """

    def __init__(self, market_data_path: Path = DEFAULT_MARKET_DATA_PATH) -> None:
        self._market_data_path = market_data_path
        self._window: tuple[date, date] | None = None
        self._window_loaded = False

    # -- source protocol --------------------------------------------------------

    def families(self) -> frozenset[RuleFamily]:
        return _FAMILIES

    def describe(self) -> str:
        window = self.observation_window()
        span = f"{window[0].isoformat()}..{window[1].isoformat()}" if window else "no snapshots"
        return f"instrument_master snapshots ({span})"

    def observation_window(self) -> tuple[date, date] | None:
        """First and last day the instrument master was captured. Cached — it moves daily."""
        if self._window_loaded:
            return self._window
        self._window_loaded = True
        if not self._market_data_path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(ingested_on), MAX(ingested_on) FROM instrument_master"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        self._window = (date.fromisoformat(row[0]), date.fromisoformat(row[1]))
        return self._window

    def records_for(self, family: RuleFamily, scope: RuleScope) -> tuple[MarketRuleRecord, ...]:
        if family not in _FAMILIES or scope.symbol is None:
            return ()
        observations = self._observations(family, scope)
        if not observations:
            return ()
        return self._records_from_runs(family, scope, observations)

    # -- reading ----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if not self._market_data_path.exists():
            raise InstrumentMasterUnavailableError(
                f"no market-data store at {self._market_data_path} — the observed-fact "
                f"source cannot tell 'no snapshots' from 'no such file', so it refuses"
            )
        return sqlite3.connect(f"file:{self._market_data_path}?mode=ro", uri=True)

    def _observations(self, family: RuleFamily, scope: RuleScope) -> list[_ObservedValue]:
        """One value per observed day, or nothing when the day is ambiguous.

        **Measured, and it broke the first real-data run.** A trading symbol is NOT unique
        across exchanges: RELIANCE appears on both NSE and BSE in every snapshot, with tick
        sizes of 0.10 and 0.05 respectively. A symbol-only query therefore sees two
        different values stamped with the same date, and the change detector read that as a
        rule changing and changing back within one day — producing a zero-length interval
        the store then rejected.

        Two values for one day is not a change, it is an ambiguous question. The source
        refuses it and the caller pins `segment`, which is the same discipline as refusing
        a scope with no symbol at all.
        """
        column = _COLUMN_BY_FAMILY[family]
        conditions = ["tradingsymbol = ?"]
        parameters: list[str] = [str(scope.symbol)]
        if scope.segment is not None:
            conditions.append("segment = ?")
            parameters.append(scope.segment)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT ingested_on, {column} FROM instrument_master "  # noqa: S608 — column
                # comes from a closed mapping keyed by an enum, never from caller input
                f"WHERE {' AND '.join(conditions)} AND {column} IS NOT NULL "
                f"ORDER BY ingested_on",
                parameters,
            ).fetchall()
        values_by_day: dict[date, set[str]] = {}
        for observed_on, value in rows:
            values_by_day.setdefault(date.fromisoformat(observed_on), set()).add(str(value))
        if any(len(values) > 1 for values in values_by_day.values()):
            return []
        return [
            _ObservedValue(observed_on, next(iter(values)))
            for observed_on, values in sorted(values_by_day.items())
        ]

    # -- change detection -------------------------------------------------------

    @staticmethod
    def _records_from_runs(
        family: RuleFamily, scope: RuleScope, observations: Sequence[_ObservedValue]
    ) -> tuple[MarketRuleRecord, ...]:
        """Compress consecutive equal observations into intervals, splitting on change.

        The last run is left OPEN-ENDED rather than closed at the final snapshot: the value
        observed on the most recent capture is the value in force now, and closing it there
        would make every query for today refuse one day after the last ingest.

        A run that ends because the value changed is closed at the day the NEW value was
        first seen — not at the last day the old one was, because the change happened
        somewhere in between and the later boundary is the one that cannot be wrong in the
        direction that matters. Claiming the old rule persisted into a day it may not have
        would put a stale rule on a real trading day.
        """
        window = observations[-1].observed_on
        records: list[MarketRuleRecord] = []
        run_start = observations[0]
        for index, observation in enumerate(observations):
            is_last = index == len(observations) - 1
            next_value = None if is_last else observations[index + 1].value
            if next_value == observation.value:
                continue
            records.append(
                MarketRuleRecord(
                    family=family,
                    scope=scope,
                    value=run_start.value,
                    value_kind=_KIND_BY_FAMILY[family],
                    effective_from=run_start.observed_on,
                    effective_to=None if is_last else observations[index + 1].observed_on,
                    source_reference=(
                        f"instrument_master snapshot, {scope.symbol} observed "
                        f"{run_start.observed_on.isoformat()}"
                        + ("" if is_last else f"..{observation.observed_on.isoformat()}")
                    ),
                    source_date=run_start.observed_on,
                    grade=EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA,
                    recorded_at=window,
                )
            )
            if not is_last:
                run_start = observations[index + 1]
        return tuple(records)
