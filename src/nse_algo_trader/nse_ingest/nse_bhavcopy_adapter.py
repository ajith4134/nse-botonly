"""`L0.23` — the daily bhavcopy, cash and F&O, across both of NSE's schema eras.

The hardest of the nine adapters and therefore the first, because if the contract can
carry this it can carry the rest. Two things make it hard:

**Two eras with different everything.** NSE replaced the bhavcopy in July 2024. The
legacy file is 14 columns keyed `SYMBOL`/`SERIES` with a `TIMESTAMP` like `02-JAN-2020`;
the UDiFF file is 34 columns keyed `TckrSymb`/`SctySrs` with an ISO `TradDt`, and it
carries derivatives contract terms the legacy cash file never had. A parser written
against either era alone is wrong across half a 25-year archive. The boundary is
**measured, not assumed**: legacy URLs 404 from 2024-07-08 and UDiFF 404s before
2024-07-01, so the two overlap for a few days and both are offered in that window.

**A request for a non-session day can return the previous session's file with HTTP 200.**
Reproduced first-hand on 2026-08-11: `sec_bhavdata_full_09082026.csv` (a Sunday) returned
374 KB of real-looking data dated 07-Aug-2026. The zipped bhavcopy 404s honestly for
that Sunday, but the same trap on a sibling endpoint is reason enough to verify the date
*inside* every file rather than trusting the one in the URL. `content_mismatch_reason`
does exactly that, in both era formats.

Sourced per `A.49`/`A.50`: the URL construction is this project's own, whose era boundary
was measured while acquiring the 2.8 GB archive now on disk, rather than a dependency
taken for logic already proven here.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

LEGACY_LAST_DAY = date(2024, 7, 5)
"""Measured: legacy per-day ZIPs 404 from 2024-07-08 onward (`research/204` §7.1)."""

UDIFF_FIRST_DAY = date(2024, 7, 1)
"""Measured: UDiFF 404s before this date. The overlap with the legacy era is real."""

_MONTH_CODES = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

CASH_COVERAGE_FLOOR = date(1994, 11, 3)
FO_COVERAGE_FLOOR = date(2000, 6, 12)
"""F&O trading launched on this date; nothing earlier can exist."""


@dataclass(frozen=True)
class BhavcopyMarket:
    """Cash or derivatives, and everything that differs between them in the URL."""

    market_name: str
    legacy_directory: str
    legacy_prefix: str
    udiff_segment: str
    udiff_folder: str
    coverage_floor: date
    coverage_evidence: str


CASH_MARKET = BhavcopyMarket(
    market_name="cash",
    legacy_directory="EQUITIES",
    legacy_prefix="cm",
    udiff_segment="CM",
    udiff_folder="cm",
    coverage_floor=CASH_COVERAGE_FLOOR,
    coverage_evidence="legacy EQUITIES archive fetched successfully back to 1994-11",
)

FO_MARKET = BhavcopyMarket(
    market_name="fo",
    legacy_directory="DERIVATIVES",
    legacy_prefix="fo",
    udiff_segment="FO",
    udiff_folder="fo",
    coverage_floor=FO_COVERAGE_FLOOR,
    coverage_evidence="F&O launch date; DERIVATIVES archive fetched from 2000-06-12",
)


def legacy_bhavcopy_url(market: BhavcopyMarket, day: date) -> str:
    month = _MONTH_CODES[day.month - 1]
    return (
        f"https://nsearchives.nseindia.com/content/historical/{market.legacy_directory}/"
        f"{day.year}/{month}/{market.legacy_prefix}{day.day:02d}{month}{day.year}bhav.csv.zip"
    )


def udiff_bhavcopy_url(market: BhavcopyMarket, day: date) -> str:
    return (
        f"https://nsearchives.nseindia.com/content/{market.udiff_folder}/"
        f"BhavCopy_NSE_{market.udiff_segment}_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    )


def _csv_rows_from_zip(payload: bytes) -> list[dict[str, str]]:
    """The single CSV inside a bhavcopy ZIP, as dict rows.

    Errors are raised, never absorbed. A `BadZipFile` here means the payload is a block
    page or a truncated download, and returning zero rows for it would be
    indistinguishable from a market holiday.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if not names:
                raise IngestAdapterError("bhavcopy ZIP contains no files")
            text = archive.read(names[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as failure:
        raise IngestAdapterError(f"payload is not a ZIP: {failure}") from failure

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise IngestAdapterError("bhavcopy CSV has no header row")
    rows = [
        {(name or "").strip(): (value or "").strip() for name, value in row.items()}
        for row in reader
    ]
    if not rows:
        raise IngestAdapterError("bhavcopy CSV has a header but no data rows")
    return rows


def _is_udiff(row: dict[str, str]) -> bool:
    return "TradDt" in row


def _parse_effective_date(row: dict[str, str]) -> date:
    """The date the file says it is about, in whichever era's format it uses."""
    if _is_udiff(row):
        raw = row.get("TradDt", "")
        try:
            return date.fromisoformat(raw)
        except ValueError as failure:
            raise IngestAdapterError(f"unparseable UDiFF TradDt {raw!r}") from failure
    raw = row.get("TIMESTAMP", "")
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()  # noqa: DTZ007 — a calendar date
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable legacy TIMESTAMP {raw!r}") from failure


class NseBhavcopyAdapter:
    """One market's daily bhavcopy, across both schema eras."""

    def __init__(self, market: BhavcopyMarket = CASH_MARKET) -> None:
        self._market = market

    @property
    def source_name(self) -> str:
        return f"nse_bhavcopy_{self._market.market_name}"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=self._market.coverage_floor,
            established_by=self._market.coverage_evidence,
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """Candidate URLs per date, both eras offered inside the measured overlap."""
        targets: list[FetchTarget] = []
        for day in for_dates:
            if day < self._market.coverage_floor:
                continue
            if day <= LEGACY_LAST_DAY:
                targets.append(
                    FetchTarget(
                        url=legacy_bhavcopy_url(self._market, day),
                        source_name=self.source_name,
                        expects=day.isoformat(),
                    )
                )
            if day >= UDIFF_FIRST_DAY:
                targets.append(
                    FetchTarget(
                        url=udiff_bhavcopy_url(self._market, day),
                        source_name=self.source_name,
                        expects=day.isoformat(),
                    )
                )
        return targets

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002 — the contract passes the target; this era needs only the payload
        rows = _csv_rows_from_zip(payload)
        parsed: list[IngestRow] = []
        for row in rows:
            effective_date = _parse_effective_date(row)
            parsed.append(
                IngestRow(
                    values=row,
                    effective_date=effective_date,
                    natural_key=self._natural_key(row),
                )
            )
        return parsed

    def _natural_key(self, row: dict[str, str]) -> tuple[str, ...]:
        """What identifies one instrument on one day.

        For derivatives the symbol alone is nowhere near unique — one underlying has
        hundreds of contracts on a single day — so expiry, strike and option type are
        part of the identity. Getting this wrong would collapse an entire option chain
        into one row and silently discard the rest as duplicates.
        """
        if _is_udiff(row):
            symbol = row.get("TckrSymb", "")
            if not symbol:
                raise IngestAdapterError("UDiFF row has no TckrSymb")
            return (
                symbol,
                row.get("SctySrs", ""),
                row.get("FinInstrmTp", ""),
                row.get("XpryDt", ""),
                row.get("StrkPric", ""),
                row.get("OptnTp", ""),
            )
        symbol = row.get("SYMBOL", "")
        if not symbol:
            raise IngestAdapterError("legacy row has no SYMBOL")
        if self._market is FO_MARKET:
            return (
                symbol,
                row.get("INSTRUMENT", ""),
                row.get("EXPIRY_DT", ""),
                row.get("STRIKE_PR", ""),
                row.get("OPTION_TYP", ""),
            )
        return (symbol, row.get("SERIES", ""))

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Whether the file's own date matches the one requested.

        The defence against a non-session request being served the previous session's
        file with HTTP 200 — reproduced first-hand on a sibling NSE endpoint.
        """
        if not target.expects:
            return None
        try:
            rows = _csv_rows_from_zip(payload)
            payload_date = _parse_effective_date(rows[0])
        except IngestAdapterError as failure:
            return f"payload could not be dated: {failure}"
        if payload_date.isoformat() != target.expects:
            return f"file is dated {payload_date.isoformat()}, requested {target.expects}"
        return None
