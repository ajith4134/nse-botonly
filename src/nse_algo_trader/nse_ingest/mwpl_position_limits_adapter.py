"""`L0.24` — market-wide position limits (MWPL), and the successor feed the recon missed.

MWPL is the per-underlying ceiling NSCCL sets on aggregate F&O open interest. It matters
operationally because a security whose open interest crosses `MWPL_BAN_UTILISATION_THRESHOLD_PCT`
of this limit enters the next day's F&O ban (`fo_ban_list_adapter.py`, `research/207` §3/§4) —
so this file is upstream of a real trading-halt decision, not a reference table.

**The "current feed not located" gap in `research/207` §3 is closed here.** Reconnaissance
found `nsearchives.nseindia.com/archives/nsccl/mwpl/nseoi_{DDMMYYYY}.zip` working from
2011-01-03 to 2024-04-30 and 404ing on every later date it tried, and reported the
successor as genuinely not found. Re-probing the *same directory* for a differently named
file (per the calling brief's instruction to try "differently-named files") found it:

    https://nsearchives.nseindia.com/archives/nsccl/mwpl/combineoi_{DDMMYYYY}.zip

Verified directly (this adapter's own reconnaissance, 2026-08-11): `combineoi_` exists
side-by-side with `nseoi_` for every date the two were cross-checked (`03012011`,
`02012012`, `03012013`, `15062015`, `04012016`, `02012024`, `15042024` — all HTTP 200 for
both names), and unlike `nseoi_`, `combineoi_` keeps working straight through the date
`nseoi_` stops at and up to the most recent trading day before this reconnaissance:
`30042024`-`10082026` all HTTP 200 (`11082026`, i.e. today, 404s — not yet published,
the same "available by the next morning" cadence bhavcopy shows). The handful of 404s
inside that range (`01052024`, `15082024`, `15012026`) line up with NSE trading holidays,
not gaps in the feed. **`combineoi_` is therefore the sole URL this adapter uses** — it is
a strict superset of `nseoi_`'s coverage plus the live feed, so offering both as separate
fetch targets would double-ingest every historical date as spurious "revisions" of each
other for no benefit (the runner fetches and stores every target `fetch_targets` returns;
see `nse_source_ingest_runner.py`).

**CSV, not XML, is authoritative — demonstrated, not assumed.** The ZIP contains both.
Direct verification here found:

- The current-era file (`combineoi_10082026.zip`) has an 8th CSV column, `Future
  Equivalent Open Interest`, whose XML sibling renders as the literal tag
  `<Future Equivalent Open Interest>` — a tag name containing spaces, which is not
  well-formed XML. `xml.etree.ElementTree.parse` on the real file raises
  `ParseError: not well-formed (invalid token)`.
- The legacy `nseoi_` XML (`02012024`) independently fails the same parse for a different
  reason: an unescaped `&` inside a company name (`SBI CARDS & PAY SER LTD`) breaks the
  parser at that record.
- Only the oldest sampled file (`combineoi_03012011`, before the extra column existed)
  parses as valid XML at all — i.e. the XML sibling is broken in exactly the eras that
  matter. The CSV has none of these defects in any sampled era and was cross-checked
  field-for-field against the (parseable) XML rows and found identical.

So this adapter reads the `.csv` member and never touches the `.xml` member.

**The `Limit for Next Day` column carries NSE's own ban signal, verified by
cross-source agreement.** In the real 2026-08-10 file, the only two rows where this
column reads the literal string `"No Fresh Positions"` instead of a number are
`BANDHANBNK` and `SAIL` — and those are exactly the two symbols the real `fo_ban_list`
fixture for 2026-08-11 names as banned the very next trading day. That is an independent
cross-check between two different NSE files, not a coincidence this adapter manufactured.
This adapter stores the column verbatim (as `values["Limit for Next Day"]`, string) and
does **not** special-case, parse, or gate on that string: `values` carries the source's
own fields untouched (`ingest_source_adapter.py`), and turning the marker into a decision
is a downstream consumer's job, not the adapter's.

**Schema is not identical across the archive** — three shapes were observed directly:
the 2011-era CSV has 7 columns (no `Future Equivalent Open Interest`); the 2024-era
`nseoi_` CSV has 6 (no `Limit for Next Day` at all, only in its broken XML, always
empty there); the current `combineoi_` CSV has 8. `csv.DictReader` reads whatever header
a given payload actually carries — this adapter does not assume a fixed column count and
only demands the four columns present in every observed era (`_REQUIRED_COLUMNS`).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from datetime import date, datetime

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

MWPL_COVERAGE_FLOOR = date(2011, 1, 3)
"""Verified directly against `nsearchives.nseindia.com`: `combineoi_03012011.zip` -> HTTP
200; `combineoi_01012011.zip` (New Year holiday) and every pre-2011 date probed
(`04012010`, `03012005`, `03012000`) -> HTTP 404. Matches the floor `research/207` §3
independently found for the `nseoi_` filename at the same path."""

MWPL_BAN_UTILISATION_THRESHOLD_PCT = 95
"""Regulatory fact, not a tunable (`R.03` exempts protocol/regulatory constants when
sourced): NSE places a security under an F&O ban once open interest crosses this
percentage of its market-wide position limit (`research/207` §3-§4, `research/209`
§1). This adapter never computes or gates on the ratio itself — it stores `MWPL` and
`Open Interest` exactly as the file states them; deriving and thresholding utilisation is
a downstream consumer's decision, not this adapter's. Recorded here only as the fact that
explains why the `Limit for Next Day` column NSE publishes sometimes reads the literal
string `"No Fresh Positions"` in place of a number — see the module docstring."""

_REQUIRED_COLUMNS = ("Date", "ISIN", "NSE Symbol", "MWPL")
"""Present, under these exact stripped names, in every one of the three schema shapes
observed directly (2011 7-column, legacy `nseoi_` 6-column, current 8-column)."""


def combineoi_mwpl_url(day: date) -> str:
    """The one URL pattern this adapter fetches — see module docstring for why."""
    return f"https://nsearchives.nseindia.com/archives/nsccl/mwpl/combineoi_{day:%d%m%Y}.zip"


def _csv_rows_from_zip(payload: bytes) -> list[dict[str, str]]:
    """The CSV member of the MWPL ZIP, as stripped dict rows.

    Selected by extension rather than zip-member order, because the ZIP always carries
    both a `.csv` and an unreliable `.xml` sibling (module docstring) and member order is
    not a contract NSE has made. Errors are raised, never absorbed: a `BadZipFile` or a
    missing/short header means the payload is a block page or a truncated download, and
    returning zero rows for it would be indistinguishable from a market holiday.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            csv_members = [
                name for name in archive.namelist() if name.lower().endswith(".csv")
            ]
            if not csv_members:
                raise IngestAdapterError("MWPL ZIP contains no .csv member")
            text = archive.read(csv_members[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as failure:
        raise IngestAdapterError(f"payload is not a ZIP: {failure}") from failure

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise IngestAdapterError("MWPL CSV has no header row")
    header = {(name or "").strip() for name in reader.fieldnames}
    missing = [column for column in _REQUIRED_COLUMNS if column not in header]
    if missing:
        raise IngestAdapterError(f"MWPL CSV is missing required column(s): {missing}")
    rows = [
        {(name or "").strip(): (value or "").strip() for name, value in row.items()}
        for row in reader
    ]
    if not rows:
        raise IngestAdapterError("MWPL CSV has a header but no data rows")
    return rows


def _parse_effective_date(row: dict[str, str]) -> date:
    """The date the file says it is about, read from its own `Date` column."""
    raw = row.get("Date", "")
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()  # noqa: DTZ007 — a calendar date
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable MWPL Date {raw!r}") from failure


class MwplPositionLimitsAdapter:
    """One underlying's market-wide position limit and open interest, one row per day.

    No I/O of its own — see `ingest_source_adapter.NseIngestSourceAdapter`. Every field
    NSE publishes is carried through in `IngestRow.values` untouched; this class only
    decides where to fetch from, what a row's date is, and what makes a row unique.
    """

    @property
    def source_name(self) -> str:
        return "mwpl_position_limits"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=MWPL_COVERAGE_FLOOR,
            established_by=(
                "combineoi_03012011.zip fetched HTTP 200 directly against "
                "nsearchives.nseindia.com; combineoi_01012011.zip and every pre-2011 "
                "date probed (2010-01-04, 2005-01-03, 2000-01-03) returned HTTP 404"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """One candidate URL per requested date at or after the verified floor.

        Only `combineoi_` is offered — see the module docstring for why `nseoi_` is not
        also offered even though it is reachable for part of the range: the runner
        fetches and ingests every target this returns, and `combineoi_` is a proven
        superset, so a second target for the same date would only manufacture spurious
        revisions of an already-current fact.
        """
        targets: list[FetchTarget] = []
        for day in for_dates:
            if day < MWPL_COVERAGE_FLOOR:
                continue
            targets.append(
                FetchTarget(
                    url=combineoi_mwpl_url(day),
                    source_name=self.source_name,
                    expects=day.isoformat(),
                )
            )
        return targets

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002 — the contract passes the target; this source needs only the payload
        rows = _csv_rows_from_zip(payload)
        parsed: list[IngestRow] = []
        for row in rows:
            effective_date = _parse_effective_date(row)
            symbol = row.get("NSE Symbol", "")
            if not symbol:
                raise IngestAdapterError("MWPL row has no NSE Symbol")
            parsed.append(
                IngestRow(
                    values=row,
                    effective_date=effective_date,
                    natural_key=(symbol,),
                )
            )
        return parsed

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Whether the file's own `Date` column matches the date requested.

        The same defence as `nse_bhavcopy_adapter`: a request for a non-session day has
        been measured returning the previous session's file with HTTP 200 on a sibling
        NSE endpoint, and only reading the date out of the payload itself catches it.
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
