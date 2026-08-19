"""Assembles each segment bot's REAL universe and prices from the stores. The spine's I/O.

**Why this is a separate module and not part of a bot.** `L5.29` forbids a bot performing any I/O:
it is handed a `SegmentBotContext` and answers. Something has to build that context out of real
rows, and that something is the spine. Putting it here rather than in the session runner means the
dashboard surface and the paper loop see exactly the same universe — a page that measured a
different universe from the loop would be worse than no page.

**Two sources, because the two cadences have two shapes** (`A.141`):

- **cash-intraday** reads `price_bars` (1,471,990 five-minute bars) joined to `instrument_master`
  for the NSE `EQ` board. Intraday, and the only segment with an intraday series today.
- **the five derivative bots** read `fo_bhavcopy_contracts`, which carries `close_price`,
  `underlying_price` and `open_interest` per contract per session — everything the option and
  futures engines need, once per session. That is the entire reason those bots are
  `ONCE_PER_SESSION`: this table publishes once a day.

**MCX reads nothing, and says so.** `fo_bhavcopy_contracts` holds zero MCX rows, so
`assemble_for` returns an EMPTY universe for `COMMODITY_MCX` rather than an approximation. An empty
universe is a fact (`B30`); a substituted one would be a fabrication.

**Point-in-time discipline.** Every query is bounded by an `as_of` session and reads only rows dated
at or before it. The bhavcopy row for a session is published after that session closes, so replaying
a past day with `as_of` set to it is honest; asking for TODAY before the close returns the last
published session and names it, rather than silently returning nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.segment_bots.segment_bot_protocol import TradeableInstrument
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment

DEFAULT_MARKET_DATA_PATH = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

PAISE_PER_RUPEE = Decimal("100")

FO_CONTRACT_TYPE_BY_SEGMENT: dict[TradingSegment, str] = {
    TradingSegment.INDEX_OPTIONS: "IDO",
    TradingSegment.STOCK_OPTIONS: "STO",
    TradingSegment.INDEX_FUTURES: "IDF",
    TradingSegment.STOCK_FUTURES: "STF",
}
"""NSE's own `contract_type` codes as they arrive in the F&O bhavcopy.

Measured from the store rather than taken from a document: `STO` 1,220,678 · `IDO` 192,789 ·
`STF` 22,561 · `IDF` 540 rows, and **no MCX code at all**, which is `B30`.
"""

# There is deliberately NO default tick size here.
#
# The first version of this module carried `DEFAULT_TICK_SIZE_PAISE = 5` and justified it as "a
# market fact". `R.03`'s money-literal guard flagged it and the guard is right: a tick is a PRICE
# increment, the exchange varies it by instrument and by price band, and a substituted one silently
# changes what every rounded order is worth. It is the same argument this module already makes about
# lot size (`L1.09`) — "a substituted lot size is worse than an absent one" — and there is no
# principled reason it stops at lot size.
#
# So an instrument whose tick the master does not carry is SKIPPED, and the count of skipped rows is
# reported in the universe's note rather than absorbed.


class SegmentUniverseError(Exception):
    """The universe cannot be assembled, and a substituted one would fabricate a decision."""


@dataclass(frozen=True, slots=True)
class AssembledSegmentUniverse:
    """One bot's universe and everything the spine measured to build it.

    `source_session` is carried because a universe assembled from the last published bhavcopy on a
    later day is a REPLAY of that session, and a reader must be able to tell the two apart.
    """

    trading_segment: TradingSegment
    instruments: tuple[TradeableInstrument, ...]
    last_price_paise_by_token: dict[int, Decimal]
    underlying_price_paise_by_symbol: dict[str, Decimal]
    open_interest_by_token: dict[int, int]
    source_session: date | None
    note: str

    @property
    def is_empty(self) -> bool:
        return not self.instruments

    def carried_memory(self, *, barred_symbols: frozenset[str] = frozenset()) -> dict[str, object]:
        """The mapping a `SegmentBotContext` carries. One shape for all six bots."""
        return {
            "last_price_paise_by_token": dict(self.last_price_paise_by_token),
            "underlying_price_paise_by_symbol": dict(self.underlying_price_paise_by_symbol),
            "open_interest_by_token": dict(self.open_interest_by_token),
            "fo_ban_list_symbols": set(barred_symbols),
        }


def _connect(market_data: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{market_data}?mode=ro", uri=True)


def _to_paise(rupees: float | int | str | None) -> Decimal | None:
    if rupees is None:
        return None
    try:
        value = (Decimal(str(rupees)) * PAISE_PER_RUPEE).quantize(Decimal("1"))
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def assemble_cash_intraday_universe(
    *,
    as_of: datetime,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    instrument_limit: int | None = None,
) -> AssembledSegmentUniverse:
    """The NSE cash board, priced from the most recent five-minute bar at or before `as_of`.

    `instrument_limit` bounds the read for a page load; it is reported in `note` rather than applied
    silently, because a sampled universe that looks like a full one is `R.09`'s exact failure.
    """
    with _connect(market_data) as connection:
        ingest = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[0]
        if ingest is None:
            return AssembledSegmentUniverse(
                TradingSegment.CASH_INTRADAY, (), {}, {}, {}, None,
                "the instrument master is empty",
            )
        rows = connection.execute(
            """
            SELECT master.instrument_token, master.tradingsymbol, master.lot_size,
                   master.tick_size, bar.close_price, bar.bar_timestamp
            FROM instrument_master AS master
            JOIN (
                SELECT instrument_token, close_price, bar_timestamp,
                       ROW_NUMBER() OVER (
                           PARTITION BY instrument_token ORDER BY bar_timestamp DESC
                       ) AS recency
                FROM price_bars
                -- `availability_time <= ?`, NOT `bar_timestamp <= ?`. A five-minute bar is
                -- knowable only once its interval has CLOSED, so filtering on the bar's own
                -- timestamp admits the bar covering the current instant and hands the bot a
                -- close it could not have seen. Caught by the point-in-time bar-read guard in
                -- `tests/test_bar_reads_are_point_in_time.py`,
                -- which exists because this is the one defect that makes every backtest look good.
                WHERE bar_interval = '5m' AND availability_time <= ?
            ) AS bar ON bar.instrument_token = master.instrument_token AND bar.recency = 1
            WHERE master.ingested_on = ? AND master.exchange = 'NSE'
              AND master.instrument_type = 'EQ'
            ORDER BY master.tradingsymbol
            """,
            (as_of.isoformat(), ingest),
        ).fetchall()

    instruments: list[TradeableInstrument] = []
    prices: dict[int, Decimal] = {}
    latest_bar: str | None = None
    skipped_for_tick = 0
    for token, symbol, lot_size, tick_size, close_price, bar_timestamp in rows:
        price = _to_paise(close_price)
        if price is None:
            continue
        tick_paise = _to_paise(tick_size)
        if tick_paise is None:
            skipped_for_tick += 1
            continue
        try:
            instrument = TradeableInstrument(
                instrument_token=int(token),
                trading_symbol=str(symbol),
                lot_size=max(int(lot_size or 1), 1),
                tick_size_paise=int(tick_paise),
            )
        except Exception:  # noqa: BLE001, S112 — one unorderable row is a skip, not a failure
            continue
        instruments.append(instrument)
        prices[instrument.instrument_token] = price
        if latest_bar is None or str(bar_timestamp) > latest_bar:
            latest_bar = str(bar_timestamp)
        if instrument_limit is not None and len(instruments) >= instrument_limit:
            break

    note = f"{len(instruments):,} cash instruments priced from five-minute bars"
    if skipped_for_tick:
        note += f", {skipped_for_tick:,} skipped for a missing tick size (no default, R.03)"
    if latest_bar:
        note += f", latest bar {latest_bar}"
    if instrument_limit is not None and len(instruments) >= instrument_limit:
        note += f" (BOUNDED at {instrument_limit:,} for this read — not the full board, R.09)"
    return AssembledSegmentUniverse(
        trading_segment=TradingSegment.CASH_INTRADAY,
        instruments=tuple(instruments),
        last_price_paise_by_token=prices,
        underlying_price_paise_by_symbol={},
        open_interest_by_token={},
        source_session=as_of.date(),
        note=note,
    )


def assemble_derivative_universe(
    trading_segment: TradingSegment,
    *,
    as_of: date,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    contract_limit: int | None = None,
) -> AssembledSegmentUniverse:
    """One derivative segment, from the last F&O bhavcopy session at or before `as_of`.

    Instrument tokens are taken from `instrument_master` where a matching row exists, and are
    otherwise DERIVED deterministically from the contract's own identity, so a contract that the
    master has not caught up with is still tradeable and still keyed the same way on every run. A
    derived token is negative, which makes it impossible to confuse with an exchange one.
    """
    contract_type = FO_CONTRACT_TYPE_BY_SEGMENT.get(trading_segment)
    if contract_type is None:
        return AssembledSegmentUniverse(
            trading_segment, (), {}, {}, {}, None,
            "no F&O contract type maps to this segment — see B30 for MCX",
        )

    with _connect(market_data) as connection:
        session_row = connection.execute(
            "SELECT MAX(trade_date) FROM fo_bhavcopy_contracts "
            "WHERE contract_type = ? AND trade_date <= ?",
            (contract_type, as_of.isoformat()),
        ).fetchone()
        session = session_row[0] if session_row else None
        if session is None:
            return AssembledSegmentUniverse(
                trading_segment, (), {}, {}, {}, None,
                f"no {contract_type} rows at or before {as_of.isoformat()}",
            )
        rows = connection.execute(
            """
            SELECT underlying_symbol, expiry_date, strike_price, option_right_code,
                   close_price, underlying_price, open_interest
            FROM fo_bhavcopy_contracts
            WHERE contract_type = ? AND trade_date = ?
            ORDER BY underlying_symbol, expiry_date, strike_price
            """,
            (contract_type, session),
        ).fetchall()
        master = _instrument_master_index(connection)

    instruments: list[TradeableInstrument] = []
    prices: dict[int, Decimal] = {}
    spots: dict[str, Decimal] = {}
    interest: dict[int, int] = {}
    skipped_for_tick = 0
    skipped_for_expiry = 0
    for (
        underlying,
        expiry_text,
        strike,
        right_code,
        close_price,
        underlying_price,
        open_interest,
    ) in rows:
        price = _to_paise(close_price)
        if price is None or not expiry_text:
            continue
        try:
            expiry = date.fromisoformat(str(expiry_text))
        except ValueError:
            continue
        if expiry < as_of:
            # A contract that settled BEFORE the day we are assembling for is not tradeable, and
            # the bhavcopy session is not proof that it is: the last session's file legitimately
            # contains every contract that expired IN that session. Measured on 2026-08-19 against
            # the 2026-08-18 file — 210 already-settled contracts, mostly weekly index options,
            # were being handed to the option bots as tradeable.
            skipped_for_expiry += 1
            continue
        symbol = _derivative_symbol(str(underlying), expiry, strike, right_code)
        lot_size, tick_size, token = master.get(symbol, (None, None, None))
        if tick_size is None:
            skipped_for_tick += 1
            continue
        try:
            instrument = TradeableInstrument(
                instrument_token=int(token) if token else _derived_token(symbol),
                trading_symbol=symbol,
                lot_size=max(int(lot_size or 1), 1),
                tick_size_paise=int(tick_size),
                strike_paise=_to_paise(strike) if right_code else None,
                expiry=expiry,
            )
        except Exception:  # noqa: BLE001, S112 — one malformed contract is a skip, not a failure
            continue
        instruments.append(instrument)
        prices[instrument.instrument_token] = price
        spot = _to_paise(underlying_price)
        if spot is not None:
            spots[str(underlying).strip().upper()] = spot
        if open_interest is not None:
            with suppress(TypeError, ValueError):
                interest[instrument.instrument_token] = int(open_interest)
        if contract_limit is not None and len(instruments) >= contract_limit:
            break

    note = (
        f"{len(instruments):,} {contract_type} contracts from the {session} bhavcopy, "
        f"{len(spots):,} underlying spot(s)"
    )
    if skipped_for_expiry:
        note += f", {skipped_for_expiry:,} already settled before {as_of.isoformat()}"
    if skipped_for_tick:
        note += f", {skipped_for_tick:,} skipped for a missing tick size (no default, R.03)"
    if contract_limit is not None and len(instruments) >= contract_limit:
        note += f" (BOUNDED at {contract_limit:,} for this read — not the full chain, R.09)"
    return AssembledSegmentUniverse(
        trading_segment=trading_segment,
        instruments=tuple(instruments),
        last_price_paise_by_token=prices,
        underlying_price_paise_by_symbol=spots,
        open_interest_by_token=interest,
        source_session=date.fromisoformat(str(session)),
        note=note,
    )


def assemble_for(
    trading_segment: TradingSegment,
    *,
    as_of: datetime,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    limit: int | None = None,
) -> AssembledSegmentUniverse:
    """The one entry point the spine calls, whichever of the six it is assembling for."""
    if trading_segment is TradingSegment.CASH_INTRADAY:
        return assemble_cash_intraday_universe(
            as_of=as_of, market_data=market_data, instrument_limit=limit
        )
    if trading_segment is TradingSegment.COMMODITY_MCX:
        # Not an error and not an approximation: there is no MCX data on this machine, and an
        # empty universe is the only honest answer (`B30`, deferred by the operator in `A.142`).
        return AssembledSegmentUniverse(
            trading_segment, (), {}, {}, {}, None,
            "NO MCX DATA — fo_bhavcopy_contracts holds zero MCX rows and no MCX token has ever "
            "been in the depth tape (B30). The bot is built whole and activates on nothing.",
        )
    return assemble_derivative_universe(
        trading_segment, as_of=as_of.date(), market_data=market_data, contract_limit=limit
    )


def _instrument_master_index(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int | None, int | None, int | None]]:
    """Trading symbol to (lot size, tick size in paise, instrument token) from the latest ingest."""
    ingest = connection.execute("SELECT MAX(ingested_on) FROM instrument_master").fetchone()[0]
    if ingest is None:
        return {}
    rows = connection.execute(
        "SELECT tradingsymbol, lot_size, tick_size, instrument_token FROM instrument_master "
        "WHERE ingested_on = ? AND exchange IN ('NFO', 'MCX', 'BFO')",
        (ingest,),
    ).fetchall()
    index: dict[str, tuple[int | None, int | None, int | None]] = {}
    for symbol, lot_size, tick_size, token in rows:
        tick_paise = _to_paise(tick_size)
        index[str(symbol).strip().upper()] = (
            int(lot_size) if lot_size else None,
            int(tick_paise) if tick_paise else None,
            int(token) if token else None,
        )
    return index


_MONTH_CODES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _derivative_symbol(
    underlying: str, expiry: date, strike: float | None, right_code: str | None
) -> str:
    """NSE's own trading-symbol convention, rebuilt from the bhavcopy's decomposed columns.

    The bhavcopy publishes underlying, expiry, strike and right as separate fields while the
    instrument master keys on the composed symbol, so one of the two has to be converted. Composing
    is the safe direction: decomposing a symbol means parsing, and a parse that silently mis-splits
    `NIFTY26AUG24800CE` is a wrong instrument rather than a missing one.
    """
    stem = f"{underlying.strip().upper()}{expiry.year % 100:02d}{_MONTH_CODES[expiry.month - 1]}"
    if right_code and strike is not None:
        strike_text = f"{strike:.0f}" if float(strike).is_integer() else f"{strike}"
        return f"{stem}{strike_text}{str(right_code).strip().upper()}"
    return f"{stem}FUT"


def _derived_token(symbol: str) -> int:
    """A stable NEGATIVE token for a contract the instrument master has not caught up with.

    Negative so it can never be mistaken for an exchange token, and derived from the symbol so the
    same contract carries the same key on every run — a randomly assigned one would split a bot's
    carried state across restarts and make its own history unreadable.
    """
    return -int.from_bytes(hashlib.sha256(symbol.encode("utf-8")).digest()[:6], "big")
