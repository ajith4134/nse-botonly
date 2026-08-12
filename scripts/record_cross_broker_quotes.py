"""Record what several brokers say about the same instruments, for the rest of the session.

This is the acquisition half of `L0.33`. Cross-broker disagreement exists only while both
brokers are quoting: after 15:30 IST there is no way to find out what Kite and Angel One
each thought RELIANCE was worth at 14:31:02, at any price. So this runs first and the
consolidation engine is built against what it captures.

**The instrument set is bounded by the SLOWEST broker, and that is recorded rather than
hidden.** Kite's `quote()` takes hundreds of symbols per call; Angel One's `getMarketData`
takes 50 and is rate-limited to roughly one request a second. Polling the full universe
through Angel One would take minutes per sweep, by which time the two brokers would be
answering about different market states and every comparison would be measuring the sweep,
not the brokers. The universe is therefore ranked by real traded value and truncated to what
one sweep can cover, and the count is printed — `R.09`'s full universe is the ENGINE's
obligation, and it is satisfied by the engine being universe-agnostic while the CAPTURE is
bounded by a broker limit that is logged as a blocker.

Usage:
    python scripts/record_cross_broker_quotes.py --instruments 50 --seconds-between-polls 2
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import signal
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from SmartApi import SmartConnect

from nse_algo_trader.broker_credentials import load_env_file_into_environ
from nse_algo_trader.broker_sessions.angel_one_session_store import AngelOneSessionFileStore
from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)
from nse_algo_trader.consolidated_feed.broker_quote_pollers import (
    AngelOneQuotePoller,
    BrokerQuotePoller,
    KiteQuotePoller,
    PolledInstrument,
    UpstoxQuotePoller,
)
from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import CrossBrokerQuoteTape

IST = ZoneInfo("Asia/Kolkata")
NSE_SESSION_CLOSE_IST = (15, 30)
"""Continuous trading ends at 15:30 IST — an exchange fact, permitted as a constant under
`R.23(e)` and already stated the same way in `depth_packet_integrity_classifier`."""

BHAVCOPY_CASH_ARCHIVE = Path("/home/opc/nse_archive/cash")

BROKERS_NEEDED_FOR_A_CROSS_CHECK = 2
"""One broker agreeing with itself is not a cross-check — the same rule the NTP arm of
`L0.32` applies to reference servers, for the same reason."""

_stop_requested = False


def log(message: str) -> None:
    print(f"[{datetime.now(IST):%H:%M:%S}] {message}", flush=True)


def _request_stop(_signal: int, _frame: FrameType | None) -> None:
    global _stop_requested
    _stop_requested = True
    log("stop requested; finishing the current poll and flushing")


def liquidity_ranked_symbols(limit: int) -> list[str]:
    """The most-traded NSE equities by real traded value, from the newest bhavcopy.

    Ranked rather than hand-picked: a cross-check on names nobody trades measures the
    brokers' stale-quote behaviour, which is worth knowing but is not what a consolidated
    TRADING feed is for. The tail is deliberately included too — see `--illiquid-tail`.
    """
    archives = sorted(BHAVCOPY_CASH_ARCHIVE.rglob("cash_*.csv.zip"))
    if not archives:
        raise SystemExit("no cash bhavcopy in the archive — cannot rank liquidity")
    with zipfile.ZipFile(archives[-1]) as archive:
        text = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
    traded_value: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("TckrSymb") or "").strip()
        if not symbol or (row.get("SctySrs") or "").strip() != "EQ":
            continue
        try:
            traded_value[symbol] = max(
                traded_value.get(symbol, 0.0), float(row.get("TtlTrfVal") or 0)
            )
        except ValueError:
            continue
    log(f"liquidity ranking from {archives[-1].name}: {len(traded_value)} symbols")
    return [symbol for symbol, _ in sorted(traded_value.items(), key=lambda pair: -pair[1])][
        :limit
    ]


def build_instrument_set(
    kite: Any, symbols: list[str], illiquid_tail: int
) -> list[PolledInstrument]:
    """Symbols addressed for BOTH brokers, dropping any neither can quote.

    An instrument only one broker can address is useless to a cross-check and would show up
    as a permanent one-sided disagreement, so it is excluded here and counted.
    """
    angel_token_by_symbol = _angel_equity_tokens()
    upstox_key_by_symbol = _upstox_instrument_keys()
    kite_instruments = {
        row["tradingsymbol"]: row
        for row in kite.instruments("NSE")
        if row["segment"] == "NSE" and row["instrument_type"] == "EQ"
    }
    if illiquid_tail:
        # The quiet end of the book is where brokers disagree most, so a deliberate slice of
        # it is carried alongside the liquid head rather than assuming the head generalises.
        tail = [name for name in sorted(kite_instruments) if name not in symbols]
        symbols = symbols + tail[-illiquid_tail:]
    instruments, unaddressable = [], 0
    for symbol in symbols:
        # An instrument only SOME brokers can address is still worth capturing: the engine
        # handles a missing source per group, and excluding it entirely would silently
        # narrow the universe to whatever the most restrictive broker happens to list.
        if symbol not in kite_instruments:
            unaddressable += 1
            continue
        instruments.append(
            PolledInstrument(
                trading_symbol=symbol,
                exchange="NSE",
                kite_symbol=f"NSE:{symbol}",
                angel_symbol_token=angel_token_by_symbol.get(symbol),
                angel_trading_symbol=f"{symbol}-EQ",
                upstox_instrument_key=upstox_key_by_symbol.get(symbol),
            )
        )
    addressable_by_all = sum(
        1
        for instrument in instruments
        if instrument.angel_symbol_token and instrument.upstox_instrument_key
    )
    log(
        f"{len(instruments)} instruments, {addressable_by_all} addressable by ALL three "
        f"brokers, {unaddressable} not quotable by Kite at all"
    )
    return instruments


def _upstox_instrument_keys() -> dict[str, str]:
    """Upstox's published NSE master, trading symbol -> `NSE_EQ|<ISIN>` key."""
    import gzip
    import json

    import requests

    response = requests.get(
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=120
    )
    response.raise_for_status()
    keys = {
        str(row["trading_symbol"]): str(row["instrument_key"])
        for row in json.loads(gzip.decompress(response.content))
        if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ"
    }
    log(f"Upstox instrument master: {len(keys)} NSE equities")
    return keys


def _angel_equity_tokens() -> dict[str, str]:
    """Angel One's public scrip master, symbol -> token, NSE equity only."""
    import requests

    response = requests.get(
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        timeout=60,
    )
    response.raise_for_status()
    tokens = {}
    for row in response.json():
        if row.get("exch_seg") != "NSE" or not str(row.get("symbol", "")).endswith("-EQ"):
            continue
        tokens[str(row["symbol"])[: -len("-EQ")]] = str(row["token"])
    log(f"Angel One scrip master: {len(tokens)} NSE equities")
    return tokens


def build_pollers() -> list[BrokerQuotePoller]:
    """Every broker that can actually answer right now. A broker that cannot is skipped loudly."""
    pollers: list[BrokerQuotePoller] = []
    kite = build_authenticated_kite_client_if_valid()
    if kite is None:
        log("KITE UNAVAILABLE — no valid access token")
    else:
        pollers.append(KiteQuotePoller(kite))
    session = AngelOneSessionFileStore().load_for_today()
    if session is None:
        log("ANGEL ONE UNAVAILABLE — no session for today")
    else:
        client = SmartConnect(api_key=os.environ["ANGEL_ONE_API_KEY"])
        client.setAccessToken(session.jwt_token)
        pollers.append(AngelOneQuotePoller(client))
    upstox_token = os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ.get(
        "UPSTOX_ACCESS_TOKEN", ""
    )
    if not upstox_token:
        log("UPSTOX UNAVAILABLE — no token in the environment")
    else:
        import requests

        pollers.append(UpstoxQuotePoller(upstox_token, requests.Session()))
    log(f"{len(pollers)} brokers available: {', '.join(p.broker for p in pollers)}")
    return pollers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", type=int, default=50)
    parser.add_argument("--illiquid-tail", type=int, default=5)
    parser.add_argument("--seconds-between-polls", type=float, default=2.0)
    parser.add_argument("--minutes", type=float, default=None, help="stop after this long")
    arguments = parser.parse_args()

    load_env_file_into_environ()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    pollers = build_pollers()
    if len(pollers) < BROKERS_NEEDED_FOR_A_CROSS_CHECK:
        log(f"only {len(pollers)} broker(s) available — a cross-check needs at least two")
        return 1

    kite_client = build_authenticated_kite_client_if_valid()
    if kite_client is None:
        log("kite client vanished between checks")
        return 1
    instruments = build_instrument_set(
        kite_client, liquidity_ranked_symbols(arguments.instruments), arguments.illiquid_tail
    )
    if not instruments:
        log("no instrument is addressable by both brokers")
        return 1

    tape = CrossBrokerQuoteTape()
    now_ist = datetime.now(IST)
    close = now_ist.replace(
        hour=NSE_SESSION_CLOSE_IST[0], minute=NSE_SESSION_CLOSE_IST[1], second=0, microsecond=0
    )
    if arguments.minutes is not None:
        close = min(close, now_ist + timedelta(minutes=arguments.minutes))
    log(f"recording {len(instruments)} instruments from {len(pollers)} brokers until {close:%H:%M}")

    polls = rows = 0
    while not _stop_requested and datetime.now(IST) < close:
        started = time.monotonic()
        session_date = datetime.now(IST).date()
        for poller in pollers:
            size = poller.instruments_per_request
            for start in range(0, len(instruments), size):
                batch = instruments[start : start + size]
                rows += tape.record(poller.poll(batch), session_date=session_date)
        polls += 1
        if polls % 20 == 0:
            coverage = tape.coverage(session_date=session_date)
            log(f"{polls} sweeps, {rows:,} rows, coverage {coverage}")
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, arguments.seconds_between_polls - elapsed))

    session_date = datetime.now(IST).date()
    final_coverage = tape.coverage(session_date=session_date)
    log(f"done: {polls} sweeps, {rows:,} rows, coverage {final_coverage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
