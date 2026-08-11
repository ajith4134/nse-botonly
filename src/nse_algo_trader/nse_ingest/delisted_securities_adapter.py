"""`L0.09` — the NSE delisted-securities master, a single cumulative file with no dates.

Every other `L0` source this project ingests is shaped like "one file per day, fetched
repeatedly." This one is not: `delisted.csv` is a **single flat file covering the whole
archive**, and every ROW carries its own delisting date — there is no per-fetch date to
compare against, because there is no per-fetch anything. One fetch returns the entire
known history of NSE delistings back to (at least) 2002 in one shot (`research/207` s1).

That inverts the usual `effective_date` question. For the bhavcopy, `effective_date` is
"what day is this whole payload about" and is read once per file. Here it is "what day
is THIS ROW about," read once per row — the file's own fetch date is irrelevant to any
individual fact inside it. A row observed today about a 2002 delisting is still, and
will always be, a fact dated 2002; `observed_at` (set by the core, not this adapter)
records only when *we* learned it, never when the delisting happened.

**Why `content_mismatch_reason` cannot do what it does for every other adapter here.**
The defence used everywhere else — read the date the payload claims for itself and
compare it to the date the caller requested — has nothing to compare against for a
source with no per-fetch date at all (`fetch_targets` sets `expects=""`, exactly like
`fo_ban_list`, because there is no date to expect). What IS checkable, and is checked
below, is that the payload is structurally the delisted-securities master and not some
other NSE file wearing a `.csv` extension: the header must open with the four verified
column names (`Symbol,Company,Delisted Date,Type of Delisting`). A block page, a
truncated body, or NSE's live-listed-securities file (`EQUITY_L.csv` — a genuinely
similar-looking neighbour on the very same archive host, verified in `research/207` s1
as "related but distinct") all fail this check. What it CANNOT detect: a **stale**
snapshot of this same file — e.g. NSE serving a cached copy missing the last few weeks
of new delistings. Because the file carries no whole-payload timestamp, an older valid
snapshot is byte-for-byte indistinguishable in *shape* from a fresher one; only a second
independent fetch, or watching row-count growth across `observed_at` snapshots in the
bitemporal store, could catch that — and that comparison belongs to the store, not to
one payload examined in isolation.

**Real-data quirks this parser was written against, not assumed.** The live file
(fetched 2026-08-11, 24,715 bytes, 328 data rows) has:
- **Trailing empty columns.** The header line is
  `Symbol,Company,Delisted Date,Type of Delisting,,,,,` — nine comma-separated fields,
  the last five always empty. Only the first four carry data; the rest are ignored.
- **An embedded newline inside a quoted field.** Row 204's `Symbol` field is literally
  `"EMTEXIND\n"` — NSE's own generator wrapped a value in quotes and left a raw newline
  inside it, splitting what should be one logical row across two physical lines. A
  naive `str.splitlines()` parser breaks this row into garbage; `csv.reader`, which is
  quote-aware, reassembles it correctly and this parser relies on that rather than
  re-inventing CSV quoting.
- **Two-digit years** (`15-Apr-02`, not `15-Apr-2002`), spanning at least 2002–2020 in
  the fetched file. `strptime("%d-%b-%y")` resolves them into the 2000s, which is
  correct for every year seen (`%y` maps 00–68 to 2000–2068) and would only misread a
  delisting recorded with a 69–99 two-digit year, which cannot occur in NSE's history.
- **No duplicate `Symbol` values** in the 328 rows fetched live — but nothing in NSE's
  process rules that out for the future (a symbol can be delisted, relisted under the
  same ticker, and delisted again), so the natural key deliberately does not rely on
  symbol-global uniqueness: it is scoped to `(symbol,)` and uniqueness is only required
  **within one `effective_date`**, which is how the shared conformance suite and the
  bitemporal store already treat every other adapter's natural key. Two delistings of
  the same symbol on two different dates are two rows with the same natural key but
  different `effective_date`s — not a collision, just two distinct facts.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date, datetime

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    RollingSnapshotSource,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

DELISTED_SECURITIES_MASTER_URL = "https://archives.nseindia.com/content/equities/delisted.csv"
"""The single rolling file. Verified reachable, unauthenticated, HTTP 200, 2026-08-11
(`research/207` s1) — matches byte-for-byte on re-fetch during this build."""

DELISTED_SECURITIES_COVERAGE_FLOOR = date(2002, 4, 15)
"""Earliest `Delisted Date` found by a full-column scan of the live 328-row payload
fetched 2026-08-11 — not head/tail sampling (`research/207`'s own scan was head/tail
only and flagged its floor unverified; this build re-fetched and scanned every row).
This is a floor of what the file **currently contains**, not a proven absolute floor of
NSE delisting history — if NSE ever backfills older rows this constant would need
re-measuring, the same caveat `research/207` records for the bhavcopy's own floors."""

_EXPECTED_HEADER_PREFIX = ("Symbol", "Company", "Delisted Date", "Type of Delisting")
"""Verified exact header prefix (`research/207` s1); the file carries five further
always-empty trailing columns after these four, which this adapter ignores."""

_MINIMUM_DATA_COLUMNS = len(_EXPECTED_HEADER_PREFIX)

DELISTED_SECURITIES_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="delisted_securities_master",
    reason=(
        "NSE publishes one cumulative delisted.csv with no per-date archive and no "
        "per-fetch identity; unlike a same-day rolling file, though, this file's rows "
        "each carry their own historical date, so re-fetching it does not lose history "
        "the way fo_ban_list or bulk/block deals would — it only risks missing newly "
        "appended rows between fetches, which repeated snapshotting still catches"
    ),
)


def _decode_rows(payload: bytes) -> list[list[str]]:
    """Every physical/logical row as a list of cells, quote-aware.

    `csv.reader` rather than a line-split is required here specifically: a real row in
    this file (`EMTEXIND`) has a raw newline inside a quoted field, and only a
    quote-aware reader reassembles it into one logical row instead of two garbage ones.
    """
    text = payload.decode("utf-8", "replace")
    return list(csv.reader(io.StringIO(text)))


def _header_row(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        raise IngestAdapterError("delisted-securities payload has no header row")
    return [cell.strip() for cell in rows[0]]


def _header_matches_delisted_master(header: Sequence[str]) -> bool:
    return tuple(header[: len(_EXPECTED_HEADER_PREFIX)]) == _EXPECTED_HEADER_PREFIX


def _parse_delisted_date(raw: str) -> date:
    stripped = raw.strip()
    for pattern in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(stripped, pattern).date()  # noqa: DTZ007 — a calendar date
        except ValueError:
            continue
    raise IngestAdapterError(f"unparseable delisted date {raw!r}")


class DelistedSecuritiesAdapter:
    """The cumulative delisted-securities master, one row per historical delisting."""

    @property
    def source_name(self) -> str:
        return "delisted_securities_master"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=DELISTED_SECURITIES_COVERAGE_FLOOR,
            established_by=(
                "full-column scan of the live delisted.csv fetched 2026-08-11 "
                "(328 data rows); earliest Delisted Date found was 2002-04-15 — a floor "
                "of this file's current contents, not a proven absolute floor"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """One target regardless of dates asked for — there is only ever the one file.

        Unlike `fo_ban_list`, requesting a past date is not a request this source
        cannot honour in principle: the file's rows already span decades. But there is
        still only one URL to fetch, so a caller asking for ten different dates gets
        the same single target ten times' worth of information in one payload, not ten
        fetches — repeating it would just re-download an identical multi-decade file.
        """
        if not for_dates:
            return []
        return [
            FetchTarget(
                url=DELISTED_SECURITIES_MASTER_URL,
                source_name=self.source_name,
                expects="",  # no per-fetch date exists for this source to declare
            )
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002 — the contract passes the target; this source needs only the payload
        rows = _decode_rows(payload)
        header = _header_row(rows)
        if not _header_matches_delisted_master(header):
            raise IngestAdapterError(
                f"delisted-securities header is {header!r}, expected it to start with "
                f"{list(_EXPECTED_HEADER_PREFIX)!r}"
            )

        parsed: list[IngestRow] = []
        seen_keys_by_date: dict[date, set[tuple[str, ...]]] = {}
        for raw_row in rows[1:]:
            if not any(cell.strip() for cell in raw_row):
                continue  # a wholly blank line; harmless and observed in real fetches
            if len(raw_row) < _MINIMUM_DATA_COLUMNS:
                raise IngestAdapterError(
                    f"delisted-securities row has only {len(raw_row)} columns, need "
                    f"at least {_MINIMUM_DATA_COLUMNS}: {raw_row!r}"
                )

            symbol = raw_row[0].strip()
            if not symbol:
                raise IngestAdapterError(f"delisted-securities row has no Symbol: {raw_row!r}")
            company = raw_row[1].strip()
            delisted_date = _parse_delisted_date(raw_row[2])
            delisting_type = raw_row[3].strip()

            natural_key = (symbol,)
            same_day_keys = seen_keys_by_date.setdefault(delisted_date, set())
            if natural_key in same_day_keys:
                raise IngestAdapterError(
                    f"delisted-securities row for {symbol!r} repeats within "
                    f"{delisted_date.isoformat()} — a duplicate row in the source file, "
                    f"not two distinct delistings"
                )
            same_day_keys.add(natural_key)

            parsed.append(
                IngestRow(
                    values={
                        "symbol": symbol,
                        "company": company,
                        "delisted_date": delisted_date.isoformat(),
                        "type_of_delisting": delisting_type,
                    },
                    effective_date=delisted_date,
                    natural_key=natural_key,
                )
            )
        if not parsed:
            raise IngestAdapterError(
                "delisted-securities payload has a valid header but zero data rows — "
                "the real master has never been observed empty and an empty body this "
                "shape is far more likely a truncated fetch than a genuine zero-delisting "
                "history, so this is refused rather than accepted as a quiet day"
            )
        return parsed

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Whether the payload is structurally the delisted master — see module docstring
        for exactly what this can and cannot detect for a source with no per-fetch date.
        """
        if target.expects:
            # A caller naming a specific expected date is asking a single, undated,
            # all-history file to prove something about one day — it cannot, and
            # guessing would be worse than refusing.
            return (
                f"delisted_securities_master is one cumulative file with no per-fetch "
                f"date; it cannot confirm or deny content for {target.expects!r}"
            )
        try:
            rows = _decode_rows(payload)
            header = _header_row(rows)
        except IngestAdapterError as failure:
            return str(failure)
        if not _header_matches_delisted_master(header):
            return (
                f"payload header is {header!r}, not the delisted-securities header "
                f"starting {list(_EXPECTED_HEADER_PREFIX)!r} — likely a different NSE "
                f"file or a block page served with the requested URL's status code"
            )
        return None
