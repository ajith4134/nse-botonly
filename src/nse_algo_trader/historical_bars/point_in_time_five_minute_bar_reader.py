"""The only sanctioned way to read five-minute bars — `L0.03`/`L0.04`, spec `docs/research/251`.

**Why this exists.** `price_bars` holds 1,246,985 five-minute bars and is what the decision path
reads. Its look-ahead safety rests entirely on each consumer remembering to write `availability_time
<= as_of`. Three production consumers remember; four read paths do not, and of those four, three are
legitimate (a whole-session join comparison, a provenance survey across all history, a dashboard
now-view) while the fourth shape — `EXISTS (SELECT 1 FROM price_bars ...)` — asks *"has any bar ever
been recorded"* when the question in a universe filter is *"was one knowable yet"*. Nothing stops
the next consumer forgetting.

**The design is one idea: there is no method that does not take `as_of`.** A caller cannot omit the
cutoff because no overload exists without it, and a test inspects the signatures so a future
convenience method cannot quietly reintroduce one. This is the same move as
`BitemporalBarStore.bars_as_of` — *"the safe read is the only read"* — applied to the table that
actually feeds decisions.

**The convention this relies on, measured rather than assumed.** `availability_time = bar_timestamp
+ interval`: a 09:15 bar becomes knowable at 09:20. Verified against the live store —
**0 of 1,246,985 rows** have `availability_time <= bar_timestamp`, so no row claims to have been
knowable before it existed. The filter is load-bearing rather than decorative: at
`2026-08-14T12:00+05:30` it hides **125,980** of those rows.

**The hole it closes that nobody would have seen.** `availability_time` is nullable at the schema
level. A NULL fails `<=` under SQL's three-valued logic, so a row with one silently *vanishes*
rather than leaking — safe in direction, invisible in effect. Today there are zero NULLs; this
reader refuses one rather than dropping it, so the day a writer inserts one is the day somebody
finds out.

Deliberately NOT an engine (`R.23b`): this is a reader over one table. No solver, no carried state,
no output that changes an allocation by itself. Named for exactly what it is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

FIVE_MINUTE_INTERVAL = "5m"
"""The interval this reader serves, and the only one `price_bars` currently holds.

Named and filtered on explicitly rather than assumed: the sibling table `price_bar` holds
`bar_interval='day'`, and confusing the two produced two wrong findings in a single day (`O.115`). A
reader that ignores the interval column would silently serve daily bars to an intraday strategy the
moment anything writes one here.
"""

DEFAULT_MARKET_DATA_DATABASE = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

STORE_TIMEZONE = ZoneInfo("Asia/Kolkata")
"""The offset every stored timestamp is written in, and the one every cutoff must be spelled in.

**This is the fix for a real look-ahead leak, not a tidiness preference.** `availability_time` is a
TEXT column, so `availability_time <= ?` is a STRING comparison, and lexical order over
mixed-offset ISO-8601 strings is not chronological order. Passing `as_of.isoformat()` straight
through meant the same physical instant gave four different answers depending on how the caller
spelled it — measured on the live store at 2026-08-14 12:00 IST:

    IST     +05:30  ->  2,538 bars   (correct)
    Nepal   +05:45  ->  2,541 bars
    Tokyo   +09:00  ->  2,577 bars   -- 39 FUTURE bars leaked, up to 3h15m ahead
    UTC     +00:00  ->  2,505 bars   -- the entire session hidden instead

The UTC case is the one that would actually have happened: `datetime.now(UTC)` appears 26 times
elsewhere in `src/`. Normalising the cutoff into the store's own offset makes all four spellings
return 2,538. `BitemporalBarStore` already does exactly this — its schema comment reads *"Always
UTC, always fixed width, so SQL TEXT order is chronological order. Never store a caller-supplied
offset spelling here."* This reader copied that API's shape and not its mechanism, and an
adversarial review found the difference (2026-08-17).
"""

_BAR_COLUMNS = (
    "instrument_token, bar_interval, bar_timestamp, open_price, high_price, "
    "low_price, close_price, volume, open_interest, availability_time"
)


class BarAvailabilityError(Exception):
    """A bar could not be read safely, so nothing is returned rather than something unsafe."""


@dataclass(frozen=True, slots=True)
class FiveMinuteBar:
    """One five-minute bar, with the instant it became knowable."""

    instrument_token: int
    bar_interval: str
    bar_timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    availability_time: datetime
    open_interest: int | None = None

    def __post_init__(self) -> None:
        if self.availability_time <= self.bar_timestamp:
            raise BarAvailabilityError(
                f"{self.trading_description} claims to have been knowable before it closed "
                f"({self.availability_time} <= {self.bar_timestamp}); a bar is knowable only once "
                f"its interval has ended, and a store that says otherwise is a machine for seeing "
                f"the future"
            )

    @property
    def trading_description(self) -> str:
        return f"token {self.instrument_token} {self.bar_interval} bar at {self.bar_timestamp}"


def _cutoff_text(as_of: datetime) -> str:
    """The cutoff, spelled the one way the stored column can be compared against.

    Refuses a naive instant, then converts into `STORE_TIMEZONE` — both halves matter. An earlier
    version only refused naive instants and then discarded its own return value, which made it a
    no-op check in front of a string comparison that silently depended on the caller's offset.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise BarAvailabilityError(
            f"as_of {as_of!r} carries no timezone; a naive instant sorts BELOW an offset-bearing "
            f"one as text, so it silently drops the boundary bar, and read as UTC it would move "
            f"every cutoff in this project by five and a half hours"
        )
    return as_of.astimezone(STORE_TIMEZONE).isoformat()


def _as_int(value: object) -> int:
    """Coerce a SQLite column to `int` with the failure surfaced, not swallowed.

    SQLite returns `object` as far as the type-checker is concerned, and scattering `type: ignore`
    at every call site hides a real failure mode: a column that is unexpectedly TEXT would raise a
    bare `ValueError` from deep inside a comprehension with no mention of which column it was.
    """
    if isinstance(value, bool):
        # `isinstance(True, int)` is True, so a bool would otherwise flow straight through as a
        # quantity. A volume of `True` is a schema fault, not a value.
        raise BarAvailabilityError(f"expected an integer column, got a bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise BarAvailabilityError(
                f"expected a whole number, got {value!r}; silently truncating it would turn a "
                f"volume of 1234.7 into 1234 while the docstring promised the failure was surfaced"
            )
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as failure:
            raise BarAvailabilityError(f"expected an integer column, got {value!r}") from failure
    raise BarAvailabilityError(f"expected an integer column, got {type(value).__name__}: {value!r}")


def _to_bar(row: tuple[object, ...]) -> FiveMinuteBar:
    availability = row[9]
    return FiveMinuteBar(
        instrument_token=_as_int(row[0]),
        bar_interval=str(row[1]),
        bar_timestamp=datetime.fromisoformat(str(row[2])),
        open_price=Decimal(str(row[3])),
        high_price=Decimal(str(row[4])),
        low_price=Decimal(str(row[5])),
        close_price=Decimal(str(row[6])),
        volume=_as_int(row[7]),
        open_interest=None if row[8] is None else _as_int(row[8]),
        availability_time=datetime.fromisoformat(str(availability)),
    )


@dataclass(frozen=True)
class PointInTimeFiveMinuteBarReader:
    """Five-minute bars, readable only as of a stated instant.

    Every public method requires `as_of` and injects `availability_time <= as_of`. There is no
    method that reads without a cutoff, which is the entire point — a guarantee enforced by the
    signature rather than by the caller's memory.
    """

    database_path: Path = DEFAULT_MARKET_DATA_DATABASE

    def bars_for_instrument(
        self,
        instrument_token: int,
        as_of: datetime,
        *,
        most_recent: int | None = None,
    ) -> tuple[FiveMinuteBar, ...]:
        """Every bar for one instrument that was knowable at `as_of`, oldest first.

        `most_recent` returns the LATEST n rather than the first n — a sizer asking for the last
        twenty closes wants this session's, and returning the earliest would price a move that has
        already finished.
        """
        cutoff = _cutoff_text(as_of)
        if most_recent is not None and most_recent <= 0:
            raise BarAvailabilityError(
                f"most_recent must be positive, got {most_recent}; asking for zero bars and asking "
                f"for all of them are different requests and must not share a spelling"
            )
        query = (
            f"SELECT {_BAR_COLUMNS} FROM price_bars "  # noqa: S608 — no interpolated input
            "WHERE instrument_token = ? AND bar_interval = ? AND availability_time <= ? "
            "ORDER BY bar_timestamp DESC"
        )
        parameters: list[object] = [instrument_token, FIVE_MINUTE_INTERVAL, cutoff]
        if most_recent is not None:
            query += " LIMIT ?"
            parameters.append(most_recent)
        self._refuse_unfilterable_rows(instrument_token)
        rows = self._query(query, parameters)
        # Selected newest-first so LIMIT takes the LATEST rows, then reversed so consumers feeding
        # rolling statistics get chronological order — the order those statistics assume.
        return tuple(_to_bar(row) for row in reversed(rows))

    def closes_for_instruments(
        self,
        as_of: datetime,
        instrument_tokens: Collection[int] | None = None,
    ) -> dict[int, list[Decimal]]:
        """Token → closes knowable at `as_of`, oldest first, for cross-sectional work.

        `instrument_tokens` narrows the read; `None` means every instrument in the store, which is
        what `R.09`'s full-universe obligation usually wants.
        """
        cutoff = _cutoff_text(as_of)
        self._refuse_unfilterable_rows(None)
        wanted = None if instrument_tokens is None else sorted(set(instrument_tokens))
        query = (
            "SELECT instrument_token, close_price, availability_time FROM price_bars "
            "WHERE bar_interval = ? AND availability_time <= ?"
        )
        parameters: list[object] = [FIVE_MINUTE_INTERVAL, cutoff]
        if wanted is not None:
            # Narrowed in SQL, not in Python. The docstring claimed "narrows the read" while the
            # filter actually ran in a loop over every row: asking for ONE instrument cost 1.9s and
            # pulled all 1,246,985 rows through Python, 71% of the cost of asking for all 3,712
            # (measured by adversarial review, 2026-08-17).
            query += f" AND instrument_token IN ({','.join('?' * len(wanted))})"
            parameters.extend(wanted)
        rows = self._query(query + " ORDER BY instrument_token, bar_timestamp", parameters)
        closes: dict[int, list[Decimal]] = {}
        for instrument_token, close_price, availability in rows:
            if availability is None:
                raise BarAvailabilityError(
                    f"token {instrument_token} has a bar with a NULL availability_time; see "
                    f"`_to_bar` for why this is refused rather than dropped"
                )
            closes.setdefault(_as_int(instrument_token), []).append(Decimal(str(close_price)))
        return closes

    def instruments_with_bars(self, as_of: datetime) -> frozenset[int]:
        """Instruments that had at least one KNOWABLE bar at `as_of`.

        Replaces the `EXISTS (SELECT 1 FROM price_bars ...)` universe filters, which ask whether a
        bar has ever been recorded. That is true at 09:00 for a bar that will not exist until 15:25,
        and it is asked in the filter that decides what is tradeable.
        """
        cutoff = _cutoff_text(as_of)
        self._refuse_unfilterable_rows(None)
        rows = self._query(
            "SELECT DISTINCT instrument_token FROM price_bars "
            "WHERE bar_interval = ? AND availability_time <= ?",
            [FIVE_MINUTE_INTERVAL, cutoff],
        )
        return frozenset(_as_int(row[0]) for row in rows)

    def _refuse_unfilterable_rows(self, instrument_token: int | None) -> None:
        """Refuse to answer at all when a row cannot be filtered by availability.

        This has to be its OWN query, and the reason is a bug this reader shipped with for one test
        run: the point-in-time `WHERE availability_time <= ?` clause **already excludes** a NULL row
        under SQL three-valued logic, so a NULL check applied to the returned rows can never fire.
        It was unreachable dead code of exactly the kind an adversarial review caught elsewhere in
        this project the same day. A NULL is therefore looked for directly, before the read, so the
        silent-disappearance case announces itself instead of looking like a quiet market.
        """
        query = (
            "SELECT COUNT(*) FROM price_bars WHERE bar_interval = ? AND availability_time IS NULL"
        )
        parameters: list[object] = [FIVE_MINUTE_INTERVAL]
        if instrument_token is not None:
            query += " AND instrument_token = ?"
            parameters.append(instrument_token)
        (unfilterable,) = self._query(query, parameters)[0]
        if unfilterable:
            scope = "the store" if instrument_token is None else f"token {instrument_token}"
            raise BarAvailabilityError(
                f"{scope} holds {unfilterable} five-minute bar(s) with a NULL availability_time. "
                f"SQL's three-valued logic makes NULL fail every `<=` comparison, so those rows "
                f"would silently VANISH from a point-in-time read rather than leak — safe in "
                f"direction, invisible in effect, and indistinguishable from a quiet market. The "
                f"column is nullable at the schema level and held zero NULLs when this was written"
            )

    def _query(self, query: str, parameters: list[object]) -> list[tuple[object, ...]]:
        try:
            # `with sqlite3.connect(...)` scopes a TRANSACTION, not the handle — 100 calls left
            # 135 file descriptors open until cyclic GC reclaimed them (measured 2026-08-17).
            with closing(
                sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
            ) as connection:
                return list(connection.execute(query, parameters))
        except sqlite3.Error as failure:
            raise BarAvailabilityError(
                f"cannot read five-minute bars from {self.database_path}: {failure}. An empty "
                f"answer from an absent or malformed store reads as 'nothing happened', which is a "
                f"lie a strategy would act on"
            ) from failure
