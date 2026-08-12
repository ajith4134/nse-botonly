"""`L0.25` — the F&O securities-in-ban-period list, a rolling file with no archive.

The simplest of the nine adapters in parsing terms and the most urgent in acquisition
terms, which is exactly why it is in wave 1 alongside the hardest one.

**There is no history to backfill.** NSE publishes one file, `fo_secban.csv`, and
overwrites it in place each day (`research/207`). Yesterday's list is not retrievable at
any price. So this source's history begins the day snapshotting begins and grows only
forward — the same permanent-loss shape as the depth tape (`A.44`), and the reason a
rolling source is worth building before a source with a 25-year archive sitting still.

**The file dates itself, and that is the whole content check.** Its first line reads
`Securities in Ban For Trade Date 11-AUG-2026:`. Because the URL carries no date, that
header is the only evidence of which day a snapshot belongs to — fetch it twice a day
apart and the URL is identical while the content is not. This is the case that makes
`observed_at` load-bearing rather than tidy.

The real file on 2026-08-11 was 66 bytes naming two securities. A short file is normal
here and must not be mistaken for a broken one — but a file with a header and **no**
securities is also normal (no stock in ban), so emptiness is judged on the header's
presence, never on the row count.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    RollingSnapshotSource,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

FO_BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
"""The single rolling file. Verified reachable, unauthenticated, 2026-08-11."""

_TRADE_DATE_PATTERN = re.compile(rb"Trade\s+Date\s*:?\s*(\d{1,2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE)

_BANNED_ROW_PATTERN = re.compile(r"^\s*(\d+)\s*,\s*([A-Za-z0-9&._-]+)\s*$")

FO_BAN_LIST_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="fo_ban_list",
    reason=(
        "NSE overwrites fo_secban.csv in place each day; no dated archive exists, so "
        "history accrues only from the day snapshotting starts"
    ),
)


def parse_ban_list_trade_date(payload: bytes) -> date:
    """The trade date the file declares for itself.

    Raised rather than defaulted to today: a snapshot whose date cannot be read is a
    snapshot that cannot be filed, and guessing would silently attribute one day's bans
    to another.
    """
    match = _TRADE_DATE_PATTERN.search(payload[:512])
    if match is None:
        raise IngestAdapterError(
            "ban list carries no 'Trade Date' header — the file dates itself and the "
            "URL does not, so an undated payload cannot be attributed to a day"
        )
    raw = match.group(1).decode()
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()  # noqa: DTZ007 — a calendar date
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable ban-list trade date {raw!r}") from failure


class FoBanListAdapter:
    """Today's F&O ban list, snapshotted so that tomorrow it still exists."""

    @property
    def source_name(self) -> str:
        return "fo_ban_list"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        # The rolling file has no archive, so the floor is the day this project first
        # captured it. Stated as the F&O launch because an earlier effective date is
        # impossible in principle; in practice coverage begins at first snapshot and the
        # coverage report shows exactly that.
        return SourceCoverageFloor(
            earliest_date=date(2000, 6, 12),
            established_by=(
                "F&O launch date; the rolling file has no archive, so realised coverage "
                "begins at the first snapshot and the coverage report measures it"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """One target regardless of dates asked for — there is only ever today's file.

        Requesting a past date cannot be honoured, and pretending otherwise by
        constructing a plausible-looking URL would produce today's file labelled as that
        past date. Returning a single undated target keeps the impossibility visible.
        """
        if not for_dates:
            return []
        return [
            FetchTarget(
                url=FO_BAN_LIST_URL,
                source_name=self.source_name,
                expects="",  # the file declares its own date; the URL cannot
            )
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002 — the contract passes the target; this era needs only the payload
        trade_date = parse_ban_list_trade_date(payload)
        text = payload.decode("utf-8", "replace")
        rows: list[IngestRow] = []
        for line in text.splitlines()[1:]:
            if not line.strip():
                continue
            match = _BANNED_ROW_PATTERN.match(line)
            if match is None:
                raise IngestAdapterError(f"unparseable ban-list row {line!r}")
            serial, symbol = match.group(1), match.group(2).upper()
            rows.append(
                IngestRow(
                    values={
                        "symbol": symbol,
                        "serial_number": int(serial),
                        "trade_date": trade_date.isoformat(),
                    },
                    effective_date=trade_date,
                    natural_key=(symbol,),
                )
            )
        # An empty ban list is a real and common state — no stock in ban today. It is
        # distinguished from a broken file by the header, which parsing already required.
        return rows

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Only a date the file cannot state is a mismatch here.

        The URL is undated, so there is nothing to disagree with unless a caller has
        explicitly stated which day it expects — which the runner does only when asking
        for a specific date, and this source cannot serve one.
        """
        try:
            trade_date = parse_ban_list_trade_date(payload)
        except IngestAdapterError as failure:
            return str(failure)
        if target.expects and trade_date.isoformat() != target.expects:
            return (
                f"ban list is for {trade_date.isoformat()}, requested {target.expects} — "
                f"this source is a rolling file and cannot serve a past date"
            )
        return None
