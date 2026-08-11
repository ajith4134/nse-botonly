"""`L0.26` — bulk deals and block deals, two SEBI-mandated large-trade disclosures.

**Two different regulatory disclosures, not one.** A *bulk deal* is any single-scrip
transaction whose quantity exceeds 0.5% of the company's listed shares, mandated by SEBI
circular `SEBI/MRD/SE/Cir-7/2004` (2004-01-14, effective 2004-02-17,
https://www.sebi.gov.in/legal/circulars/jan-2004/disclosure-of-trade-details-of-bulk-deals_11912.html).
A *block deal* is a trade executed through NSE's separate block-deal trading window under
minimum-size rules introduced by SEBI circular `MRD/DoP/SE/Cir-19/2005` (2005-09-02,
https://www.sebi.gov.in/legal/circulars/sep-2005/guidelines-for-execution-of-block-deals-on-the-stock-exchanges_8382.html).
Different thresholds, different regulatory basis, different files — `research/207` §5
verified both are published as separate CSVs with slightly different schemas (block
carries no `Remarks` column). Shipping only one would be a silent narrowing of `L0.26`,
so this module carries both, parameterised by `BulkBlockDealsCategory`, the same shape
`nse_bhavcopy_adapter.py` uses to separate cash from F&O.

**Both are rolling today-only files, like `fo_ban_list`.** `research/207` §5 verified
`archives.nseindia.com/content/equities/bulk.csv` and `.../block.csv` return HTTP 200
with only the single most recent trading day's deals — re-fetched and independently
re-verified while building this adapter (2026-08-11: both files carried exactly one
distinct `Date` value, `10-AUG-2026`). **There is no dated archive.** History for these
two sources accrues only from the day snapshotting starts, exactly the shape
`RollingSnapshotSource` documents for the F&O ban list.

**The historical API is blocked — reproduced first-hand, not just cited.** Per `R.16` an
alternate acquisition path was seriously attempted before accepting this:

- `www.nseindia.com/api/historical/bulk-deals?from=...&to=...` — **HTTP 503**, Apache
  "Service Unavailable / maintenance downtime" bot-block page (reproduced).
- `www.nseindia.com/api/historical/block-deals?from=...&to=...` — **HTTP 503**, same
  block page (reproduced).
- `nsearchives.nseindia.com/api/historical/bulk-deals?...` (archive host instead of
  `www`) — **HTTP 404**.
- `archives.nseindia.com/api/historical/bulk-deals?...` — **HTTP 404**.
- `nsearchives.nseindia.com/content/historical/bulk-deals/2026/AUG/bulk10082026.csv`
  (dated path, bhavcopy-style) — **HTTP 404**.
- `archives.nseindia.com/content/historical/BULKDEALS/bulk10082026.csv` — **HTTP 403**.
- `archives.nseindia.com/archives/equities/bulkdeals/bulk10082026.csv` — **HTTP 404**.
- `nsearchives.nseindia.com/archives/equities/bulk-deals/bulk10082026.csv` — **HTTP 404**.
- `archives.nseindia.com/content/equities/bulk_deals_10082026.csv` (bhavcopy-style dated
  filename) — **HTTP 404**.
- `www.nseindia.com/api/snapshot-capital-market-largedeal` — **HTTP 200**, but is the
  SAME rolling today-only data as JSON (`as_on_date` matches `bulk.csv`'s date), not
  history.

**Conclusion: BLOCKED for history, confirmed reachable for today-only.** No dated
archive, mirror, or alternate host serves a past bulk/block deal. This adapter covers
the reachable rolling file fully and certifies it; the historical gap is a
`docs/BACKLOG.md` item for the caller to record, not a reason to narrow this adapter.

**The natural key is qualified, not guaranteed, and that is stated rather than
invented.** A row identifies: date, symbol, client, buy/sell side, quantity, price. The
file carries no trade-sequence number or timestamp, and real fetched data
(`archives.nseindia.com/content/equities/bulk.csv`, 2026-08-11) proves a naive key of
`(date, symbol, client, side)` collides: `VISHAL MAHESH WAGHELA` bought `ATALREAL` twice
on `10-AUG-2026` (636554 shares @ 35.08 and 1061984 shares @ 35.24) and sold it twice the
same day too (136554 @ 34.57 and 1080463 @ 34.64). Adding quantity and price to the key
(`symbol, client, side, quantity, price`) makes every row in both real fixtures unique —
but that is an empirical fact about the payloads observed, not a schema guarantee: two
genuinely identical trades by the same client (same symbol, side, quantity AND price, on
the same day) would still collide under this key and be silently treated by the store as
a revision of one another rather than two distinct trades. No key derivable from this
file's columns can rule that out. This is disclosed here rather than papered over with
an invented sequence number.

**What `content_mismatch_reason` can and cannot do.** Unlike the ban list's prose
header, this file dates itself per-row (`Date` on every data row, not once at the top),
so the check reads the distinct dates present across all rows and compares them to what
was requested. On a payload with **zero data rows** (a header-only file, legitimate on a
day when no trade crossed either threshold) there is no date anywhere in the content, so
the check cannot assert a match OR a mismatch and returns `None` — the same limitation
`fo_ban_list` would have if its prose header were absent, stated explicitly rather than
guessed at.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    RollingSnapshotSource,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

_ROLLING_FILE_REASON = (
    "NSE overwrites this file in place each day; no dated archive exists (verified: "
    "www.nseindia.com historical bulk/block-deals API returns HTTP 503, and every "
    "nsearchives/archives alternate path tried 404s or 403s — research/207 §5), so "
    "history accrues only from the day snapshotting starts"
)

_BUY = "BUY"
_SELL = "SELL"
_VALID_SIDES = (_BUY, _SELL)
"""The only two values NSE's `Buy/Sell` column carries. A row with anything else is a
corrupt payload, not a third kind of trade."""

_DATE_COLUMN = "Date"
_SYMBOL_COLUMN = "Symbol"
_SECURITY_NAME_COLUMN = "Security Name"
_CLIENT_NAME_COLUMN = "Client Name"
_SIDE_COLUMN = "Buy/Sell"
_QUANTITY_COLUMN = "Quantity Traded"
_PRICE_COLUMN = "Trade Price / Wght. Avg. Price"
_REMARKS_COLUMN = "Remarks"

_BULK_HEADER = (
    _DATE_COLUMN,
    _SYMBOL_COLUMN,
    _SECURITY_NAME_COLUMN,
    _CLIENT_NAME_COLUMN,
    _SIDE_COLUMN,
    _QUANTITY_COLUMN,
    _PRICE_COLUMN,
    _REMARKS_COLUMN,
)
"""Verified exact, `research/207` §5, from a live fetch of `bulk.csv` on 2026-08-11."""

_BLOCK_HEADER = (
    _DATE_COLUMN,
    _SYMBOL_COLUMN,
    _SECURITY_NAME_COLUMN,
    _CLIENT_NAME_COLUMN,
    _SIDE_COLUMN,
    _QUANTITY_COLUMN,
    _PRICE_COLUMN,
)
"""Verified exact, `research/207` §5 — identical to bulk's header but with no `Remarks`
column, from a live fetch of `block.csv` on 2026-08-11."""


@dataclass(frozen=True)
class BulkBlockDealsCategory:
    """A deal disclosure category, and everything that differs between bulk and block."""

    category_name: str
    url: str
    expected_header: tuple[str, ...]
    coverage_floor: date
    coverage_evidence: str


BULK_DEALS_URL = "https://archives.nseindia.com/content/equities/bulk.csv"
"""The single rolling file. Verified reachable, unauthenticated, 2026-08-11."""

BLOCK_DEALS_URL = "https://archives.nseindia.com/content/equities/block.csv"
"""The single rolling file. Verified reachable, unauthenticated, 2026-08-11."""

BULK_DEALS_CATEGORY = BulkBlockDealsCategory(
    category_name="bulk",
    url=BULK_DEALS_URL,
    expected_header=_BULK_HEADER,
    coverage_floor=date(2004, 2, 17),
    coverage_evidence=(
        "SEBI/MRD/SE/Cir-7/2004 (2004-01-14, effective 2004-02-17) mandated bulk-deal "
        "disclosure; the rolling file has no archive, so realised coverage begins at "
        "the first snapshot and the coverage report measures it"
    ),
)

BLOCK_DEALS_CATEGORY = BulkBlockDealsCategory(
    category_name="block",
    url=BLOCK_DEALS_URL,
    expected_header=_BLOCK_HEADER,
    coverage_floor=date(2005, 9, 2),
    coverage_evidence=(
        "SEBI circular MRD/DoP/SE/Cir-19/2005 (2005-09-02) introduced the block-deal "
        "window mechanism; the rolling file has no archive, so realised coverage "
        "begins at the first snapshot and the coverage report measures it"
    ),
)

BULK_DEALS_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="bulk_block_deals_bulk", reason=_ROLLING_FILE_REASON
)
BLOCK_DEALS_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="bulk_block_deals_block", reason=_ROLLING_FILE_REASON
)


def _deal_rows_from_csv(payload: bytes, category: BulkBlockDealsCategory) -> list[dict[str, str]]:
    """Rows from one deals CSV, header-validated against the category's exact schema.

    The header check is the corruption guard: a block page, a truncated download, or the
    wrong category's file all fail it, and failing it raises rather than returning rows
    that happen to parse as something else.
    """
    text = payload.decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = tuple(reader.fieldnames or ())
    if fieldnames != category.expected_header:
        raise IngestAdapterError(
            f"{category.category_name} deals header {fieldnames!r} does not match the "
            f"verified schema {category.expected_header!r} — payload is not this "
            f"source's real file"
        )
    rows = [
        {(name or "").strip(): (value or "").strip() for name, value in row.items()}
        for row in reader
    ]
    return rows


def _parse_deal_date(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()  # noqa: DTZ007 — a calendar date
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable deal date {raw!r}") from failure


class BulkBlockDealsAdapter:
    """One deal-disclosure category's rolling file, bulk or block."""

    def __init__(self, category: BulkBlockDealsCategory = BULK_DEALS_CATEGORY) -> None:
        self._category = category

    @property
    def source_name(self) -> str:
        return f"bulk_block_deals_{self._category.category_name}"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=self._category.coverage_floor,
            established_by=self._category.coverage_evidence,
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """One target regardless of dates asked for — there is only ever today's file.

        Mirrors `FoBanListAdapter`: constructing a plausible per-date URL would return
        today's file mislabelled as that date, so a past-date request cannot be honoured
        and pretending otherwise would hide that fact rather than surface it.
        """
        if not for_dates:
            return []
        return [
            FetchTarget(
                url=self._category.url,
                source_name=self.source_name,
                expects="",  # each row dates itself; the URL cannot
            )
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002 — the contract passes the target; this era needs only the payload
        rows = _deal_rows_from_csv(payload, self._category)
        parsed: list[IngestRow] = []
        for row in rows:
            side = row.get(_SIDE_COLUMN, "")
            if side not in _VALID_SIDES:
                raise IngestAdapterError(
                    f"{self._category.category_name} deal row has an unrecognised "
                    f"Buy/Sell value {side!r}, expected one of {_VALID_SIDES}"
                )
            effective_date = _parse_deal_date(row.get(_DATE_COLUMN, ""))
            symbol = row.get(_SYMBOL_COLUMN, "").upper()
            if not symbol:
                raise IngestAdapterError(
                    f"{self._category.category_name} deal row has no {_SYMBOL_COLUMN}"
                )
            client_name = row.get(_CLIENT_NAME_COLUMN, "")
            quantity = row.get(_QUANTITY_COLUMN, "")
            price = row.get(_PRICE_COLUMN, "")
            parsed.append(
                IngestRow(
                    values={**row, "deal_category": self._category.category_name},
                    effective_date=effective_date,
                    natural_key=(symbol, client_name, side, quantity, price),
                )
            )
        # An empty deals file is a real state — no trade crossed the threshold that day.
        # Judged on the header validating cleanly, never on the row count, matching the
        # F&O ban list's "quiet day looks like zero rows, a broken parser also looks like
        # zero rows, only the header tells them apart" reasoning.
        return parsed

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Whether any row's own date matches the one requested.

        Each row dates itself (there is no single document-level header, unlike the ban
        list), so every distinct date present is compared against what was asked for. A
        header-only, zero-row payload carries no date anywhere and this check cannot
        assert anything about it either way — documented in the module docstring.
        """
        try:
            rows = _deal_rows_from_csv(payload, self._category)
        except IngestAdapterError as failure:
            return str(failure)
        if not target.expects:
            return None
        if not rows:
            return None
        dates_present = {
            _parse_deal_date(row.get(_DATE_COLUMN, "")).isoformat() for row in rows
        }
        if target.expects not in dates_present:
            return (
                f"{self._category.category_name} deals file carries date(s) "
                f"{sorted(dates_present)}, requested {target.expects} — this source is "
                f"a rolling file and cannot serve a past date"
            )
        return None
