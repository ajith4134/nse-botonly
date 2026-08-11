"""`L0.17` — Angel One symbology, from the master it publishes without authentication.

Angel serves its full scrip master as a public JSON file: 152,555 rows, no credentials, no
rate limit worth pacing. That makes it the broker whose symbology can be resolved for the
WHOLE universe rather than a hand-written handful — which is the difference between real
cross-source reconciliation and single-source with extra steps.

**The mapping is safe because the data says so, not because it looks safe.** Measured on
the real file: within NSE, the 2,485 `-EQ` rows have 2,485 DISTINCT `name` values — zero
collisions — and the 249 `-BE` rows share no name with any `-EQ` row. So `name` is a
unique key across the equity series and there is no case where a symbol could resolve to
two different companies. That was checked before the mapping was written, because a
resolver that silently picks between two candidates fetches a different company's price
history and nothing downstream can tell.

**Only equity series are mapped.** The NSE segment also carries 4,295 `-SG` government
securities, 972 `-N0` bonds and 119 `-MF` rows. Nothing in this project trades them, and
including them would inflate coverage with instruments no strategy will ever request.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.broker_symbology.broker_symbology_resolver import (
    BrokerSymbol,
    BrokerSymbolStore,
    SymbologyError,
)

IST = ZoneInfo("Asia/Kolkata")
"""Symbology is refreshed against the EXCHANGE's day, not the server's: a UTC host rolls
over at 05:30 IST, mid-morning in the market this maps."""

ANGEL_SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
"""Angel's public master. No authentication — a published endpoint, not a scrape."""

EQUITY_SERIES = ("EQ", "BE", "SM")
"""NSE equity series Angel suffixes onto its symbols: rolling settlement, trade-for-trade,
and SME. Government securities, bonds and mutual funds are deliberately excluded."""

DOWNLOAD_TIMEOUT_SECONDS = 180
"""The master is ~35 MB. A short timeout here fails on a slow link and leaves symbology
stale, which is worse than waiting."""

MINIMUM_CREDIBLE_EQUITY_ROWS = 500
"""A sanity floor on the download. NSE lists thousands of equities; a master parsing to
fewer than this means the file changed shape or the download truncated, and REPLACING good
symbology with it would silently unname most of the universe."""


class AngelOneSymbologyResolver:
    """Resolves NSE trading symbols to Angel One tokens."""

    def __init__(
        self,
        store: BrokerSymbolStore,
        *,
        fetch_master: Any = None,
    ) -> None:
        self._store = store
        # Injected for tests (`R.J` seam): production downloads the real master, and the
        # hermetic path never reaches the network.
        self._fetch_master = fetch_master or _download_scrip_master

    @property
    def broker(self) -> BrokerName:
        return BrokerName.ANGEL_ONE

    def refresh(self, *, today: date | None = None) -> int:
        """Re-read Angel's master and replace the stored mapping."""
        rows = self._fetch_master()
        symbols = parse_equity_symbols(
            rows, refreshed_on=today or datetime.now(IST).date()
        )
        if len(symbols) < MINIMUM_CREDIBLE_EQUITY_ROWS:
            raise SymbologyError(
                f"angel master parsed to only {len(symbols)} equity rows "
                f"(expected at least {MINIMUM_CREDIBLE_EQUITY_ROWS}) — refusing to "
                "replace good symbology with a truncated download"
            )
        return self._store.replace_for(BrokerName.ANGEL_ONE, symbols)

    def resolve(self, tradingsymbol: str, exchange: str = "NSE") -> str | None:
        return self._store.identifier_for(BrokerName.ANGEL_ONE, tradingsymbol, exchange)


def parse_equity_symbols(
    rows: Sequence[dict[str, Any]], *, refreshed_on: date
) -> list[BrokerSymbol]:
    """Equity rows from Angel's master, keyed by the NSE trading symbol.

    Angel's `name` is the NSE trading symbol and its `symbol` is that plus a series suffix
    (`RELIANCE` / `RELIANCE-EQ`). The suffix is what identifies the series, so it is parsed
    rather than assumed — a row whose symbol carries no recognised suffix is skipped, not
    guessed at.
    """
    symbols: list[BrokerSymbol] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("exch_seg") != "NSE":
            continue
        broker_symbol = str(row.get("symbol", ""))
        if "-" not in broker_symbol:
            continue
        series = broker_symbol.rsplit("-", 1)[-1]
        if series not in EQUITY_SERIES:
            continue
        tradingsymbol = str(row.get("name", "")).strip()
        token = str(row.get("token", "")).strip()
        if not tradingsymbol or not token:
            continue
        key = ("NSE", tradingsymbol)
        if key in seen:
            # Measured as impossible in the real master (2,485 -EQ rows, 2,485 distinct
            # names). Kept as a refusal rather than a silent first-wins, because if Angel
            # ever does publish a collision, picking one would map a symbol to a different
            # company with no signal at all.
            raise SymbologyError(
                f"angel master maps {tradingsymbol} to more than one token — "
                "refusing to choose between them"
            )
        seen.add(key)
        symbols.append(
            BrokerSymbol(
                broker=BrokerName.ANGEL_ONE,
                exchange="NSE",
                tradingsymbol=tradingsymbol,
                broker_identifier=token,
                broker_symbol=broker_symbol,
                series=series,
                refreshed_on=refreshed_on,
            )
        )
    return symbols


def _download_scrip_master() -> list[dict[str, Any]]:
    response = requests.get(ANGEL_SCRIP_MASTER_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise SymbologyError(
            f"angel master returned {type(payload).__name__}, expected a list"
        )
    return payload
