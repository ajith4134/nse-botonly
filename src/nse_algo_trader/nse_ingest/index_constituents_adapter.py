"""`L0.29` — index constituent membership across NSE's published index universe, plus the
raw free-float weight-methodology inputs where a live feed for them could be found.

**Two genuinely different files, one adapter, because they answer the two halves of
"what is this index made of" that `research/207` §8 left open.**

1. **Membership** — `https://archives.nseindia.com/content/indices/ind_<slug>list.csv`,
   the same free/unauthenticated archive tier as bhavcopy and the ban list. Verified for
   33 indices spanning broad-market (NIFTY 50/100/200/500), size-sliced (MIDCAP 50/100/150,
   SMALLCAP 50/100/250, MIDSMALLCAP 400, LARGEMIDCAP 250), sectoral (BANK, AUTO, IT, PHARMA,
   FMCG, METAL, REALTY, ENERGY, PSU BANK, HEALTHCARE, CONSUMER DURABLES, OIL & GAS, MEDIA)
   and thematic (NEXT 50, MNC, COMMODITIES, CPSE, PSE, CONSUMPTION, FINANCIAL SERVICES
   25/50) indices — the full published set this reconnaissance located, not a NIFTY-50
   sample (`R.09`). **This file carries no date of its own** — the header is
   `Company Name,Industry,Symbol,Series,ISIN Code`, nothing else — so unlike
   `fo_ban_list.csv`'s self-dating "Trade Date" line, there is nothing inside the payload
   to check a snapshot's true date against. Membership rows are dated from the date the
   caller claims via `FetchTarget.expects`, and `content_mismatch_reason` can only verify
   that claim is a well-formed date — it can never catch NSE quietly serving a stale
   membership snapshot the way the bhavcopy check catches a stale bhavcopy. That is a real,
   disclosed gap, not an oversight.

2. **Weight-methodology inputs** — `research/207` §8 reported per-constituent index
   WEIGHTS as not located anywhere. This adapter found one: `niftyindices.com`'s public
   site (an Angular SPA — every path under it 200s with the same shell, so `curl` cannot
   drive it directly) loads its live per-stock "heatmap" widget from a *separate* static
   host, `liveindexsa.niftyindices.com`, discovered by reading the shipped
   `assets/js/IISLComponet.js` bundle rather than guessing:
   `GET https://liveindexsa.niftyindices.com/jsonfiles/HeatmapDetail/FinalHeatmap<INDEX NAME>.json`
   (verified HTTP 200, no cookie, for all 33 indices below; one name needed correcting —
   the obvious guess `"NIFTY HEALTHCARE INDEX"` 404s, the feed's real name for it is
   `"NIFTY HEALTHCARE"`, no trailing word, verified 200). Each record is one
   constituent's `sharesOutstanding`, `investableWeightFactor`, `cappingFactor`,
   `dayEndClose` and `Indexmcap_today`/`Indexmcap_yst` — the literal per-stock free-float
   market-cap inputs NSE's own index engine sums to produce the index level. **This is not
   an adapter-side approximation**: `weight_pct = Indexmcap_today / sum(Indexmcap_today
   over the index)` is NSE's own published free-float methodology, computed from fields
   NSE itself publishes, not a model this project invented. Per the brief, computing that
   ratio is left to a downstream consumer — an adapter parses, it does not model — so
   these fields are stored raw under `values` and `record_kind="free_float_weight_inputs"`.

   **This feed is self-dating and, as of this build (2026-08-11), stale.** Every record
   carries a `time` field (`"Jan 08, 2026 16:00:29"`), and the HTTP `Last-Modified` header
   independently confirms it (`Fri, 16 Jan 2026 16:5x:xx GMT`) — the same date, across
   *every one* of the 33 indices checked, while the unrelated `LiveIndicesWatch.json` feed
   on the same host returns today's real intraday tick (`"11-Aug-2026 11:23"`). One blob
   container stopped refreshing roughly seven months before this reconnaissance; the rest
   of the site is live. `content_mismatch_reason` reads the date out of the payload exactly
   the way the bhavcopy adapter does, and today it correctly reports every weight target as
   a mismatch — that is the check doing its job, not a bug in this adapter. Downstream
   readers get real, current MEMBERSHIP today and correctly get nothing for WEIGHTS until
   NSE (or niftyindices.com) refreshes that container; the alternative — silently serving
   January's weights as August's — is exactly the plausible-looking-wrong-data trap this
   whole ingest core exists to catch.

   A handful of records in this feed are `"DUMMY<SYMBOL>"` placeholders with every numeric
   field zeroed and `time: "0"` (observed: `DUMMYHDLVR` inside the NIFTY 50 feed on
   2026-08-11) — a real NSE-side sentinel for "no live slot for this constituent today",
   not corruption. These are dropped rather than stored or raised on; a payload containing
   only such sentinels (never observed, but structurally possible) is treated as corrupt.

**Not built here, and why:** `nifiio`'s vendored URL table (`research` Appendix, `A.49`
sourcing conventions) lists several *strategy* indices — Alpha 50, Low Volatility 50,
Quality 30, Value 20 — under a third URL shape
(`niftyindices.com/IndexConstituent/ind_nifty_Alpha_Index.csv`-style, inconsistent
casing/underscoring per index) that was not individually curl-verified for all variants
here. Real gap, not a silent narrowing — surfaced in this adapter's build report for
`docs/BACKLOG.md` (`R.16`/`R.23(k)`), since this file may not touch that document.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

MEMBERSHIP_ARCHIVE_HOST = "https://archives.nseindia.com/content/indices"
"""The always-free archive tier (`research/207` §0) — the same host bhavcopy, the ban
list and `sec_list.csv` already rely on. Verified directly for every slug below."""

WEIGHT_METHODOLOGY_HOST = "https://liveindexsa.niftyindices.com/jsonfiles/HeatmapDetail"
"""Not in `research/207`: found by reading `niftyindices.com/assets/js/IISLComponet.js`,
the JS bundle behind the site's live per-stock heatmap widget, and curl-verified directly
against all 33 indices in `INDEX_UNIVERSE` (2026-08-11)."""

MEMBERSHIP_HEADER = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")
"""Exact header verified across every membership CSV fetched. A file with any other
header is not this file — parsing raises rather than guessing column positions."""

MEMBERSHIP_COLUMN_COUNT = len(MEMBERSHIP_HEADER)

DUMMY_SYMBOL_PREFIX = "DUMMY"
"""NSE's own sentinel for an empty heatmap slot — observed `DUMMYHDLVR`, all-zero fields,
`time` literally the string `"0"`. Dropped, not stored, not raised on."""

WEIGHT_RECORD_TIME_FORMAT = "%b %d, %Y %H:%M:%S"
"""The `time` field's exact format, observed live: `"Jan 08, 2026 16:00:29"`."""

INDEX_LAUNCH_ERA_FLOOR = date(1996, 4, 22)
"""NIFTY 50's widely documented base/launch era — general knowledge, not a curl-verified
fact (this source has no archive to probe, unlike bhavcopy's `research/207`-measured
floors). Cited only as the earliest date the *concept* of an NSE index could exist, the
same rhetorical role `fo_ban_list`'s F&O-launch floor plays for a source with no archive:
realised coverage begins at first snapshot, and the coverage report measures that, not
this constant."""


@dataclass(frozen=True)
class IndexCatalogEntry:
    """One published NSE index: its display name and the two verified URL keys for it."""

    index_name: str
    """NSE's own display name, e.g. `"NIFTY 50"` — also the exact string the weight feed
    expects, except where noted (`heatmap_name` overrides it)."""
    archive_slug: str
    """Filename stem on `MEMBERSHIP_ARCHIVE_HOST`, e.g. `"ind_nifty50list"`."""
    heatmap_name: str = ""
    """Overrides `index_name` for the weight feed when they diverge (only `NIFTY
    HEALTHCARE`, verified: `"...INDEX"` 404s, `"NIFTY HEALTHCARE"` 200s). Empty means
    `index_name` is used as-is."""

    @property
    def weight_feed_name(self) -> str:
        return self.heatmap_name or self.index_name


INDEX_UNIVERSE: tuple[IndexCatalogEntry, ...] = (
    IndexCatalogEntry("NIFTY 50", "ind_nifty50list"),
    IndexCatalogEntry("NIFTY NEXT 50", "ind_niftynext50list"),
    IndexCatalogEntry("NIFTY 100", "ind_nifty100list"),
    IndexCatalogEntry("NIFTY 200", "ind_nifty200list"),
    IndexCatalogEntry("NIFTY 500", "ind_nifty500list"),
    IndexCatalogEntry("NIFTY MIDCAP 50", "ind_niftymidcap50list"),
    IndexCatalogEntry("NIFTY MIDCAP 100", "ind_niftymidcap100list"),
    IndexCatalogEntry("NIFTY MIDCAP 150", "ind_niftymidcap150list"),
    IndexCatalogEntry("NIFTY SMALLCAP 50", "ind_niftysmallcap50list"),
    IndexCatalogEntry("NIFTY SMALLCAP 100", "ind_niftysmallcap100list"),
    IndexCatalogEntry("NIFTY SMALLCAP 250", "ind_niftysmallcap250list"),
    IndexCatalogEntry("NIFTY MIDSMALLCAP 400", "ind_niftymidsmallcap400list"),
    IndexCatalogEntry("NIFTY LARGEMIDCAP 250", "ind_niftylargemidcap250list"),
    IndexCatalogEntry("NIFTY BANK", "ind_niftybanklist"),
    IndexCatalogEntry("NIFTY AUTO", "ind_niftyautolist"),
    IndexCatalogEntry("NIFTY IT", "ind_niftyitlist"),
    IndexCatalogEntry("NIFTY PHARMA", "ind_niftypharmalist"),
    IndexCatalogEntry("NIFTY FMCG", "ind_niftyfmcglist"),
    IndexCatalogEntry("NIFTY METAL", "ind_niftymetallist"),
    IndexCatalogEntry("NIFTY REALTY", "ind_niftyrealtylist"),
    IndexCatalogEntry("NIFTY ENERGY", "ind_niftyenergylist"),
    IndexCatalogEntry("NIFTY INFRASTRUCTURE", "ind_niftyinfralist"),
    IndexCatalogEntry("NIFTY PSU BANK", "ind_niftypsubanklist"),
    IndexCatalogEntry(
        "NIFTY HEALTHCARE INDEX", "ind_niftyhealthcarelist", heatmap_name="NIFTY HEALTHCARE"
    ),
    IndexCatalogEntry("NIFTY CONSUMER DURABLES", "ind_niftyconsumerdurableslist"),
    IndexCatalogEntry("NIFTY OIL & GAS", "ind_niftyoilgaslist"),
    IndexCatalogEntry("NIFTY MEDIA", "ind_niftymedialist"),
    IndexCatalogEntry("NIFTY MNC", "ind_niftymnclist"),
    IndexCatalogEntry("NIFTY COMMODITIES", "ind_niftycommoditieslist"),
    IndexCatalogEntry("NIFTY CPSE", "ind_niftycpselist"),
    IndexCatalogEntry("NIFTY PSE", "ind_niftypselist"),
    IndexCatalogEntry("NIFTY INDIA CONSUMPTION", "ind_niftyconsumptionlist"),
    IndexCatalogEntry("NIFTY FINANCIAL SERVICES 25/50", "ind_niftyfinancialservices25_50list"),
)
"""Every index this reconnaissance curl-verified on both hosts (2026-08-11) — 33 of
NSE's published indices, deliberately spanning broad-market, size-sliced, sectoral and
thematic, per `R.09`. Not exhaustive (see module docstring's "Not built here" section)."""


def membership_url(entry: IndexCatalogEntry) -> str:
    return f"{MEMBERSHIP_ARCHIVE_HOST}/{entry.archive_slug}.csv"


def weight_methodology_url(entry: IndexCatalogEntry) -> str:
    return f"{WEIGHT_METHODOLOGY_HOST}/FinalHeatmap{quote(entry.weight_feed_name)}.json"


_MEMBERSHIP_URL_TO_ENTRY: dict[str, IndexCatalogEntry] = {
    membership_url(entry): entry for entry in INDEX_UNIVERSE
}
_WEIGHT_URL_TO_ENTRY: dict[str, IndexCatalogEntry] = {
    weight_methodology_url(entry): entry for entry in INDEX_UNIVERSE
}


def _entry_for_url(url: str, catalog: dict[str, IndexCatalogEntry]) -> IndexCatalogEntry:
    entry = catalog.get(url)
    if entry is None:
        raise IngestAdapterError(
            f"{url!r} is not a URL this adapter's catalog constructed — cannot attribute "
            "rows to an index"
        )
    return entry


def _claimed_effective_date(target: FetchTarget) -> date:
    """The date a rolling, self-undated file is attributed to.

    Only used for membership rows: the membership CSV carries no date field of its own
    (see module docstring), so `FetchTarget.expects` — set by `fetch_targets` from the
    dates the caller asked for — is the only date available. A target with no claim at
    all cannot be dated and is refused rather than guessed.
    """
    if not target.expects:
        raise IngestAdapterError(
            "no claimed date on this target — a rolling, undated membership file cannot "
            "be attributed to a day without one"
        )
    try:
        return date.fromisoformat(target.expects)
    except ValueError as failure:
        raise IngestAdapterError(
            f"target expects {target.expects!r}, which is not a valid ISO date"
        ) from failure


def _parse_membership_rows(payload: bytes, target: FetchTarget) -> list[IngestRow]:
    text = payload.decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = tuple(cell.strip() for cell in next(reader))
    except StopIteration as failure:
        raise IngestAdapterError("membership file is empty — no header row") from failure
    if header != MEMBERSHIP_HEADER:
        raise IngestAdapterError(
            f"unexpected membership header {header!r}, expected {MEMBERSHIP_HEADER!r}"
        )

    entry = _entry_for_url(target.url, _MEMBERSHIP_URL_TO_ENTRY)
    effective_date = _claimed_effective_date(target)

    rows: list[IngestRow] = []
    for raw_row in reader:
        if not any(cell.strip() for cell in raw_row):
            continue
        if len(raw_row) < MEMBERSHIP_COLUMN_COUNT:
            raise IngestAdapterError(f"malformed membership row {raw_row!r}")
        company, industry, symbol, series, isin = (
            cell.strip() for cell in raw_row[:MEMBERSHIP_COLUMN_COUNT]
        )
        if not symbol:
            raise IngestAdapterError(f"membership row has no Symbol: {raw_row!r}")
        rows.append(
            IngestRow(
                values={
                    "record_kind": "membership",
                    "index_name": entry.index_name,
                    "symbol": symbol,
                    "company_name": company,
                    "industry": industry,
                    "series": series,
                    "isin_code": isin,
                },
                effective_date=effective_date,
                natural_key=(entry.index_name, symbol),
            )
        )
    if not rows:
        # Unlike the ban list, an index can never legitimately have zero constituents —
        # a header with no data rows is always corruption, never a quiet day.
        raise IngestAdapterError(
            f"{entry.index_name} membership file has a header but no constituent rows"
        )
    return rows


def _parse_weight_methodology_rows(payload: bytes, target: FetchTarget) -> list[IngestRow]:
    try:
        records = json.loads(payload.decode("utf-8", "replace"))
    except json.JSONDecodeError as failure:
        raise IngestAdapterError(f"weight payload is not valid JSON: {failure}") from failure
    if not isinstance(records, list) or not records:
        raise IngestAdapterError("weight payload is not a non-empty JSON array")

    entry = _entry_for_url(target.url, _WEIGHT_URL_TO_ENTRY)

    rows: list[IngestRow] = []
    for record in records:
        if not isinstance(record, dict):
            raise IngestAdapterError(f"weight record is not a JSON object: {record!r}")
        symbol = str(record.get("symbol", "")).strip().upper()
        if not symbol:
            raise IngestAdapterError(f"weight record has no symbol: {record!r}")
        if symbol.startswith(DUMMY_SYMBOL_PREFIX):
            continue  # NSE's own empty-slot sentinel — see module docstring.

        raw_time = str(record.get("time", ""))
        try:
            record_date = datetime.strptime(  # noqa: DTZ007 — a calendar date
                raw_time, WEIGHT_RECORD_TIME_FORMAT
            ).date()
        except ValueError as failure:
            raise IngestAdapterError(
                f"unparseable weight-record time {raw_time!r} for {symbol}"
            ) from failure

        rows.append(
            IngestRow(
                values={
                    "record_kind": "free_float_weight_inputs",
                    "index_name": entry.index_name,
                    "symbol": symbol,
                    "sector": record.get("sector", ""),
                    "shares_outstanding": record.get("sharesOutstanding"),
                    "investable_weight_factor": record.get("investableWeightFactor"),
                    "capping_factor": record.get("cappingFactor"),
                    "day_end_close": record.get("dayEndClose"),
                    "index_free_float_mcap_today": record.get("Indexmcap_today"),
                    "index_free_float_mcap_yesterday": record.get("Indexmcap_yst"),
                },
                effective_date=record_date,
                natural_key=(entry.index_name, symbol),
            )
        )
    if not rows:
        raise IngestAdapterError(
            f"{entry.index_name} weight payload contained only placeholder/dummy records"
        )
    return rows


class IndexConstituentsAdapter:
    """Membership across NSE's published index universe, plus free-float weight inputs
    where a live feed for them exists (see module docstring for both, in full)."""

    @property
    def source_name(self) -> str:
        return "index_constituents_weights"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=INDEX_LAUNCH_ERA_FLOOR,
            established_by=(
                "NIFTY 50's well-known base/launch era, cited as the earliest date the "
                "concept of NSE index membership could exist. This source is a rolling, "
                "undated snapshot with no archive (unlike bhavcopy or MWPL, there is "
                "nothing to probe backward from) — realised coverage begins at whichever "
                "day this project's ingest job first captured a snapshot, exactly as "
                "fo_ban_list's F&O-launch floor plays the same role for a source with no "
                "archive; the coverage report measures the real number, not this constant"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """Every index's membership and weight-methodology target, claimed as of the
        latest date asked for.

        There is only ever one live snapshot per index, never one per historical day —
        the same constraint `fo_ban_list` faces, extended across the whole index
        universe. Requesting five different days back-fills nothing: it still returns
        today's file for each index, claimed once, not five times.
        """
        if not for_dates:
            return []
        claimed = max(for_dates).isoformat()
        targets: list[FetchTarget] = []
        for entry in INDEX_UNIVERSE:
            targets.append(
                FetchTarget(
                    url=membership_url(entry), source_name=self.source_name, expects=claimed
                )
            )
            targets.append(
                FetchTarget(
                    url=weight_methodology_url(entry),
                    source_name=self.source_name,
                    expects=claimed,
                )
            )
        return targets

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        """Dispatches on payload shape, not the URL — a `[` starts the JSON weight feed,
        anything else is attempted as the CSV membership file. The same discipline
        `nse_bhavcopy_adapter` uses to tell its two schema eras apart from content rather
        than trusting the caller's URL, and the reason the malformed-payload fixtures
        (none of which start with `[`) genuinely exercise the CSV parser's header check
        rather than being waved through by a URL-based guard.
        """
        if payload.lstrip()[:1] == b"[":
            return _parse_weight_methodology_rows(payload, target)
        return _parse_membership_rows(payload, target)

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """What this can and cannot catch, and it differs by which of the two files.

        The weight feed self-dates (`time` on every record) exactly like bhavcopy, so a
        stale serve is caught the same way — and as documented in the module docstring,
        it currently *will* catch every weight target, because the real feed has been
        stuck on 2026-01-08 since before this reconnaissance. The membership feed has no
        date inside it at all, so this can only confirm the caller's claimed date is
        well-formed; it can never catch NSE serving a stale membership list, because
        nothing in the payload says which day it is for.
        """
        if not target.expects:
            return None
        if target.url in _WEIGHT_URL_TO_ENTRY:
            try:
                rows = _parse_weight_methodology_rows(payload, target)
            except IngestAdapterError as failure:
                return str(failure)
            found_dates = sorted({row.effective_date.isoformat() for row in rows})
            if target.expects not in found_dates:
                return (
                    f"weight snapshot is dated {found_dates}, requested {target.expects} "
                    "— niftyindices.com's live-heatmap blob is not refreshed daily "
                    "(observed frozen since 2026-01-08 as of this adapter's construction "
                    "on 2026-08-11); a mismatch here reflects that real staleness, not a "
                    "defect in this check"
                )
            return None
        if target.url in _MEMBERSHIP_URL_TO_ENTRY:
            try:
                date.fromisoformat(target.expects)
            except ValueError:
                return f"target expects {target.expects!r}, which is not a valid ISO date"
            return None
        return None
