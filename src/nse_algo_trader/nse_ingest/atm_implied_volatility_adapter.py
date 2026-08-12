"""`L0.27` — ATM implied volatility, fetched live from NSE's own option-chain feed.

`research/207` marked this source **BLOCKED**: `GET /api/option-chain-indices?symbol=NIFTY`
returned HTTP 404, and no static/archived alternative was found on `nsearchives.nseindia.com`.
That finding is correct for the URL it tested — `option-chain-indices` really is gone — but it
is **stale for the source as a whole**. Re-verified live on 2026-08-11, mechanically, with
exact bytes captured:

    GET https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=11-Aug-2026
    -> HTTP 200, 241,816 bytes, real data, e.g.
       {"records": {"data": [{"expiryDates": "11-Aug-2026", "strikePrice": 24450,
         "CE": {"impliedVolatility": 10.57, ...}, "PE": {"impliedVolatility": 9.35, ...}}, ...],
         "timestamp": "11-Aug-2026 11:29:03", "underlyingValue": 24445.1, ...}}

`option-chain-indices` was replaced by `option-chain-v3` at some point after `research/207` was
written (or that recon simply hit the deprecated name — either way, the successor works today).
Confirmed reproducible with a **completely cold, single-shot `requests.Session`** carrying only
the browser `User-Agent` this project already uses (`nse_source_fetcher.BROWSER_USER_AGENT`) —
no cookie priming, no Referer, no prior request needed. Also confirmed for equities
(`type=Equity&symbol=RELIANCE&expiry=25-Aug-2026` -> HTTP 200, real data, ATM IV ~19%). Fixtures
of both, plus a real "wrong expiry guessed" response, are saved under
`tests/fixtures_nse_ingest/option_chain_v3_*.json` — every byte in this adapter's tests was
fetched, not written.

**Why this is fetch-and-parse, not modelling.** The brief that produced this adapter asked me to
judge whether ATM IV belongs in an ingest adapter at all, since it can also be *computed* via a
Black-Scholes inversion of the F&O bhavcopy's `SttlmPric`/`UndrlygPric` fields (confirmed present
and inspected in `udiff_fo_20260810.csv.zip` — no `IV` column exists in the 34-column UDiFF
schema; the four `Rsvd*` columns and `Rmks` are verified blank in the real file, so it is not
hiding there). That path would need an assumed risk-free rate, a strike-interpolation rule and a
numerical solver — three modelling decisions with no single correct answer, which is exactly the
shape of thing this project's ingest layer refuses to store as if it were a fact (`bitemporal_
ingest_store.py`: a row is "what the source said", and a recomputation from a changed model
assumption is not a new observation of the same fact). **That path is not needed here**: NSE's
own `option-chain-v3` response already carries a real `impliedVolatility` per strike per leg,
computed by the exchange itself from live executed/quoted prices. This adapter fetches that
number: no model, no solver, no assumed rate — a straight read of what NSE published, which is
precisely what an ingest adapter is for.

**The one genuine complication: `expiry` must be an exact, currently-live date.** Unlike a bhavcopy
URL, `option-chain-v3` takes `expiry` as a query parameter, not part of a predictable path. Get it
wrong (a real-format date NSE simply isn't quoting) and the endpoint answers **HTTP 200 with a
validly-shaped, genuinely empty `records.data: []`** — not a 404, not a block page — so a wrong
guess is indistinguishable from a blocked fetch unless the adapter checks the payload itself. There
is no discovery endpoint this adapter can call first: the contract forbids I/O in `fetch_targets`,
and the core fetches every returned target independently with no chaining between them, so a
"learn the real expiry, then fetch it" two-step is not expressible here without changing the core.

Given that, `fetch_targets` offers a **bounded, calendar-derived candidate ladder** per underlying
instead of one guess:

- **Weekly window** (`for_date` .. `for_date + 7` days): index options settle weekly, so the true
  near expiry is always inside this window regardless of *which* weekday NSE currently uses for it
  (NSE has changed that weekday more than once; nothing here hardcodes one).
- **Month-end window** (the final 7 calendar days of the relevant month): stock options settle
  monthly. Verified **exchange-wide and symbol-independent** — `option-chain-contract-info` for
  `TCS`, `INFY`, `HDFCBANK` and `SBIN` all returned the identical three dates
  (`25-Aug-2026, 29-Sep-2026, 27-Oct-2026`), and all three are Tuesdays, matching the same weekday
  NIFTY's own weekly cycle is currently using. Seven days, not fewer: any 7 consecutive days
  contain every weekday once, which is the minimum guaranteed to catch "the last occurrence of
  whichever weekday NSE is using" regardless of how many days trail it in a given month — a
  narrower window would have missed the verified real `25-Aug-2026` expiry, 6 days before
  August's month-end.

Every candidate that is NOT the real expiry comes back HTTP 200 with empty `data` — caught by
`content_mismatch_reason` (which re-derives the requested expiry from the URL itself and compares
it against what the payload actually carries) and recorded as `CONTENT_MISMATCH`, never parsed.
Only the candidate(s) that are genuinely live reach `parse()`. This means the source is honest by
construction: a stale calendar assumption produces visible, recorded mismatches, never silent
zero rows mistaken for a holiday.

**Cost, stated plainly.** The full universe (5 indices + 208 equities, embedded below) times a
~13-date candidate ladder is on the order of **2,700 requests per ingest run** against
`www.nseindia.com` — the one host in this project's whole surface known to gate inconsistently
(`research/207` §0). Every fetch here was observed unthrottled and un-blocked during reconnaissance,
but that was on the order of dozens of requests, not thousands; this adapter has not been proven
safe at its true daily volume. Flagging this honestly rather than asserting it: a smarter core
(memoize yesterday's confirmed expiry per underlying, or a real two-phase discover-then-fetch
primitive) would cut this by roughly 13x and belongs on `docs/BACKLOG.md`, which this task is not
permitted to edit directly.

**This is a rolling snapshot, like the F&O ban list and bulk/block deals.** `option-chain-v3`
answers with *right now*, not a specific historical day — there is no dated archive of past
option chains on any NSE host tried (`research/207` §6). `fetch_targets` therefore only ever
targets the single most recent date asked for; earlier dates in `for_dates` are refused the same
honest way `FoBanListAdapter` refuses them, via `content_mismatch_reason` catching that the
snapshot's own timestamp can never match a day already gone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    RollingSnapshotSource,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

OPTION_CHAIN_V3_URL = "https://www.nseindia.com/api/option-chain-v3"
"""Verified HTTP 200, real data, cold single-shot GET, 2026-08-11 (see module docstring)."""

FO_LAUNCH_DATE = date(2000, 6, 12)
"""No effective date before F&O itself existed is possible in principle (shared with
`nse_bhavcopy_adapter.FO_MARKET`). Realised coverage begins at first snapshot, same as every
other rolling source — there is no archive to backfill (`RollingSnapshotSource` below)."""

INDEX_UNDERLYINGS: tuple[str, ...] = (
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
)
"""The complete index-options universe — 5 of 5, not a sample. Verified via
`GET https://www.nseindia.com/api/underlying-information` -> HTTP 200, `data.IndexList`,
2026-08-11."""

EQUITY_UNDERLYINGS: tuple[str, ...] = (
    "360ONE",
    "ABB",
    "APLAPOLLO",
    "AUBANK",
    "ADANIENSOL",
    "ADANIENT",
    "ADANIGREEN",
    "ADANIPORTS",
    "ADANIPOWER",
    "ABCAPITAL",
    "ALKEM",
    "AMBER",
    "AMBUJACEM",
    "ANGELONE",
    "APOLLOHOSP",
    "ASHOKLEY",
    "ASIANPAINT",
    "ASTRAL",
    "AUROPHARMA",
    "DMART",
    "AXISBANK",
    "BSE",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BAJAJHLDNG",
    "BANDHANBNK",
    "BANKBARODA",
    "BANKINDIA",
    "BDL",
    "BEL",
    "BHARATFORG",
    "BHEL",
    "BPCL",
    "BHARTIARTL",
    "BIOCON",
    "BLUESTARCO",
    "BOSCHLTD",
    "BRITANNIA",
    "CGPOWER",
    "CANBK",
    "CDSL",
    "CHOLAFIN",
    "CIPLA",
    "COALINDIA",
    "COCHINSHIP",
    "COFORGE",
    "COLPAL",
    "CAMS",
    "CONCOR",
    "CROMPTON",
    "CUMMINSIND",
    "DLF",
    "DABUR",
    "DALBHARAT",
    "DELHIVERY",
    "DIVISLAB",
    "DIXON",
    "DRREDDY",
    "ETERNAL",
    "EICHERMOT",
    "FORCEMOT",
    "NYKAA",
    "FORTIS",
    "GAIL",
    "GVT&D",
    "GMRAIRPORT",
    "GLENMARK",
    "GODFRYPHLP",
    "GODREJCP",
    "GODREJPROP",
    "GRASIM",
    "HCLTECH",
    "HDFCAMC",
    "HDFCBANK",
    "HDFCLIFE",
    "HAVELLS",
    "HEROMOTOCO",
    "HINDALCO",
    "HAL",
    "HINDPETRO",
    "HINDUNILVR",
    "HINDZINC",
    "POWERINDIA",
    "HYUNDAI",
    "ICICIBANK",
    "ICICIGI",
    "ICICIPRULI",
    "IDFCFIRSTB",
    "ITC",
    "INDIANB",
    "IEX",
    "IOC",
    "IRFC",
    "IREDA",
    "INDUSTOWER",
    "INDUSINDBK",
    "NAUKRI",
    "INFY",
    "INOXWIND",
    "INDIGO",
    "JINDALSTEL",
    "JSWENERGY",
    "JSWSTEEL",
    "JIOFIN",
    "JUBLFOOD",
    "KEI",
    "KPITTECH",
    "KALYANKJIL",
    "KAYNES",
    "KFINTECH",
    "KOTAKBANK",
    "LTF",
    "LICHSGFIN",
    "LTM",
    "LT",
    "LAURUSLABS",
    "LICI",
    "LODHA",
    "LUPIN",
    "M&M",
    "MANAPPURAM",
    "MANKIND",
    "MARICO",
    "MARUTI",
    "MFSL",
    "MAXHEALTH",
    "MAZDOCK",
    "MOTILALOFS",
    "MPHASIS",
    "MCX",
    "MUTHOOTFIN",
    "NBCC",
    "NHPC",
    "NMDC",
    "NTPC",
    "NATIONALUM",
    "NESTLEIND",
    "NAM-INDIA",
    "OBEROIRLTY",
    "ONGC",
    "OIL",
    "PAYTM",
    "OFSS",
    "POLICYBZR",
    "PGEL",
    "PIIND",
    "PNBHOUSING",
    "PAGEIND",
    "PATANJALI",
    "PERSISTENT",
    "PETRONET",
    "PIDILITIND",
    "POLYCAB",
    "PFC",
    "POWERGRID",
    "PREMIERENE",
    "PRESTIGE",
    "PNB",
    "RBLBANK",
    "RECLTD",
    "RADICO",
    "RVNL",
    "RELIANCE",
    "SBICARD",
    "SBILIFE",
    "SHREECEM",
    "SRF",
    "MOTHERSON",
    "SHRIRAMFIN",
    "SIEMENS",
    "SOLARINDS",
    "SONACOMS",
    "SBIN",
    "SAIL",
    "SUNPHARMA",
    "SUPREMEIND",
    "SUZLON",
    "SWIGGY",
    "TATACONSUM",
    "TVSMOTOR",
    "TCS",
    "TATAELXSI",
    "TMPV",
    "TATAPOWER",
    "TATASTEEL",
    "TECHM",
    "FEDERALBNK",
    "INDHOTEL",
    "PHOENIXLTD",
    "TITAN",
    "TORNTPHARM",
    "TRENT",
    "TIINDIA",
    "UNOMINDA",
    "UPL",
    "ULTRACEMCO",
    "UNIONBANK",
    "UNITDSPR",
    "VBL",
    "VEDL",
    "VMM",
    "IDEA",
    "VOLTAS",
    "WAAREEENER",
    "WIPRO",
    "YESBANK",
    "ZYDUSLIFE",
)
"""The complete stock-options universe as of the capture date — 208 symbols, not a sample.
Verified via the same `underlying-information` call, `data.UnderlyingList`, 2026-08-11. NSE
revises this list periodically (additions/removals roughly quarterly per public NSE circulars);
this is a dated snapshot, embedded the same way `nse_bhavcopy_adapter._MONTH_CODES` embeds a
protocol fact rather than deriving one that would require its own network call. A periodic
refresh of this table is a real, named gap — surfaced in this task's report for `docs/BACKLOG.md`
per `R.16`/Rule K, not silently accepted as permanent."""

ATM_IMPLIED_VOLATILITY_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="atm_implied_volatility",
    reason=(
        "option-chain-v3 answers with the current live snapshot only; NSE publishes no dated "
        "archive of past option chains on any host checked (research/207 §6), so history "
        "accrues only from the day snapshotting starts"
    ),
)

_WEEKLY_LOOKAHEAD_DAYS = 7
"""Index options settle at least weekly, so the true near expiry is always within this many
days of `for_date` — a structural fact about the instrument, not a tunable (`R.03`)."""

_MONTH_END_WINDOW_DAYS = 7
"""Width of the trailing window used to find the monthly equity/index expiry inside a month.
Seven, not fewer: any 7 consecutive calendar days contain every weekday exactly once, so this
is the minimum width that is guaranteed to catch "the last occurrence of whichever weekday NSE
is currently using" regardless of how many trailing days a given month happens to have after it
(verified against the real 2026-08-25 monthly expiry, which sits 6 days before August's
month-end — a 5-day window would have missed it)."""

_DECEMBER = 12
"""Calendar fact, not a tunable — used to detect a year rollover in month arithmetic."""

_MONTH_ABBREVIATIONS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
"""NSE's own `expiry=` query format is `DD-Mon-YYYY` (verified: `11-Aug-2026`). Built by hand
rather than `date.strftime("%d-%b-%Y")` because `%b` is locale-dependent and this exact casing
is a wire-protocol fact, not a display preference."""


def format_expiry_query_value(day: date) -> str:
    """NSE's own `DD-Mon-YYYY` expiry format, verified against a real live response."""
    return f"{day.day:02d}-{_MONTH_ABBREVIATIONS[day.month - 1]}-{day.year}"


def _month_end_window(year: int, month: int) -> tuple[date, ...]:
    first_of_next_month = date(year + 1, 1, 1) if month == _DECEMBER else date(year, month + 1, 1)
    last_day_of_month = first_of_next_month - timedelta(days=1)
    return tuple(
        last_day_of_month - timedelta(days=offset) for offset in range(_MONTH_END_WINDOW_DAYS)
    )


def candidate_expiry_dates(for_date: date) -> tuple[date, ...]:
    """The bounded, calendar-derived guess ladder documented at the top of this module.

    Weekly window catches index options; month-end window catches equities (and, in months
    where they coincide, both at once — verified they do: `25-Aug-2026` is simultaneously
    NIFTY's near weekly expiry and every equity's near monthly expiry).
    """
    weekly = {for_date + timedelta(days=offset) for offset in range(_WEEKLY_LOOKAHEAD_DAYS + 1)}
    this_month_window = _month_end_window(for_date.year, for_date.month)
    this_month_end = [day for day in this_month_window if day >= for_date]
    if this_month_end:
        monthly = this_month_end
    else:
        next_month = 1 if for_date.month == _DECEMBER else for_date.month + 1
        next_year = for_date.year + 1 if for_date.month == _DECEMBER else for_date.year
        monthly = list(_month_end_window(next_year, next_month))
    return tuple(sorted(weekly | set(monthly)))


def _option_chain_url(instrument_type: str, symbol: str, expiry: date) -> str:
    query = urlencode(
        {"type": instrument_type, "symbol": symbol, "expiry": format_expiry_query_value(expiry)}
    )
    return f"{OPTION_CHAIN_V3_URL}?{query}"


def _expiry_query_param(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("expiry")
    return values[0] if values else None


def _requested_calendar_date(expects: str) -> date | None:
    """The calendar date a target is asking for, or `None` only when it asks for no date.

    `FetchTarget.expects` is contractually a plain ISO date or empty
    (`nse_source_fetcher.FetchTarget.expects`) — the underlying/expiry a target is for
    already lives in the URL (`_expiry_query_param`), so `expects` carries only the date.
    An `expects` that is non-empty but not a valid ISO date is NOT silently treated as "no
    date to check": that would make the wrong-date check fail-open, which is worse than no
    check at all, so `content_mismatch_reason` treats it as unreadable and refuses to
    confirm the payload.
    """
    if not expects:
        return None
    try:
        return date.fromisoformat(expects)
    except ValueError:
        return None


def _load_option_chain_document(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise IngestAdapterError(f"payload is not valid JSON: {failure}") from failure
    if not isinstance(document, dict) or not isinstance(document.get("records"), dict):
        raise IngestAdapterError(
            "payload has no 'records' object — not an option-chain-v3 response"
        )
    return document


def _parse_snapshot_date(timestamp_text: object) -> date:
    """The date the snapshot itself claims, read from inside the payload — never trusted from
    the request. The defence this whole project builds into every source (`content_mismatch_
    reason`'s reason for existing): a rolling feed's only proof of freshness is its own body."""
    if not isinstance(timestamp_text, str) or not timestamp_text.strip():
        raise IngestAdapterError("payload carries no 'records.timestamp' to date this snapshot")
    try:
        return datetime.strptime(timestamp_text.split(" ")[0], "%d-%b-%Y").date()  # noqa: DTZ007
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable snapshot timestamp {timestamp_text!r}") from failure


def _is_positive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _select_atm_entry(entries: list[dict[str, Any]], underlying_value: object) -> dict[str, Any]:
    """The entry whose strike is nearest the live underlying price, restricted to entries
    where at least one leg carries a real (nonzero) implied volatility.

    Deep-OTM strikes routinely carry an all-zero leg (no quotes at all, verified in the real
    RELIANCE fixture) — nearest-by-strike alone can land on one of those, so the search walks
    outward from the money until it finds a strike NSE actually priced.
    """
    if not _is_positive_number(underlying_value):
        raise IngestAdapterError(f"payload underlyingValue is not usable: {underlying_value!r}")
    numeric_entries = [entry for entry in entries if _is_positive_number(entry.get("strikePrice"))]
    if not numeric_entries:
        raise IngestAdapterError("no entry in the chain carries a usable strikePrice")
    ranked = sorted(numeric_entries, key=lambda entry: abs(entry["strikePrice"] - underlying_value))
    for entry in ranked:
        call_leg = entry.get("CE") or {}
        put_leg = entry.get("PE") or {}
        if _is_positive_number(call_leg.get("impliedVolatility")) or _is_positive_number(
            put_leg.get("impliedVolatility")
        ):
            return entry
    raise IngestAdapterError("no strike in the chain carries a nonzero implied volatility")


def _positive_float_or_none(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _symbol_of_entry(entry: dict[str, Any]) -> str:
    for leg_name in ("CE", "PE"):
        underlying = (entry.get(leg_name) or {}).get("underlying")
        if isinstance(underlying, str) and underlying:
            return underlying
    raise IngestAdapterError("selected ATM entry carries no 'underlying' field in either leg")


class AtmImpliedVolatilityAdapter:
    """`L0.27` — the daily ATM implied-volatility snapshot, straight from NSE's own chain.

    A rolling, live-only source (see module docstring): each `parse()` call turns one
    `option-chain-v3` payload for one `(underlying, expiry)` guess into at most one row, keyed
    on `(underlying, expiry)` so that a genuinely different expiry never collides with — and
    silently overwrites, via the store's revision logic — a different expiry's ATM IV on the
    same day.
    """

    def __init__(self, universe: Sequence[tuple[str, str]] | None = None) -> None:
        self._universe: tuple[tuple[str, str], ...] = tuple(
            universe
            if universe is not None
            else (
                [(symbol, "Indices") for symbol in INDEX_UNDERLYINGS]
                + [(symbol, "Equity") for symbol in EQUITY_UNDERLYINGS]
            )
        )

    @property
    def source_name(self) -> str:
        return "atm_implied_volatility"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=FO_LAUNCH_DATE,
            established_by=(
                "F&O launch date; this is a rolling live snapshot with no archive, so realised "
                "coverage begins at the first snapshot and the coverage report measures it"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """Candidate targets for the single most recent date asked for.

        Collapsed to one date deliberately: every candidate here hits the SAME live endpoint
        regardless of which `for_date` it was generated for, so generating a full ~2,700-target
        sweep per date in a multi-date request would multiply real network calls against
        `www.nseindia.com` for dates this rolling source can never actually serve — earlier
        dates are refused honestly (`content_mismatch_reason`), not chased with more guesses.
        """
        if not for_dates:
            return []
        for_date = max(for_dates)
        if for_date < self.coverage_floor.earliest_date:
            return []
        candidates = candidate_expiry_dates(for_date)
        return [
            FetchTarget(
                url=_option_chain_url(instrument_type, symbol, expiry),
                source_name=self.source_name,
                expects=for_date.isoformat(),
            )
            for symbol, instrument_type in self._universe
            for expiry in candidates
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:  # noqa: ARG002
        document = _load_option_chain_document(payload)
        records = document["records"]
        entries = records.get("data")
        if not isinstance(entries, list) or not entries:
            # A wrong-expiry guess looks exactly like this and is filtered out by
            # `content_mismatch_reason` before the runner ever calls `parse()` (`research/209`
            # §4 clause 8). Reaching here with empty data means that guard was bypassed —
            # raising, never treating it as a quiet day, is the only safe response.
            raise IngestAdapterError(
                "payload carries zero option-chain entries — this is the same shape as a "
                "wrong-expiry guess and must never be parsed as a real (empty) trading day"
            )
        atm_entry = _select_atm_entry(entries, records.get("underlyingValue"))
        effective_date = _parse_snapshot_date(records.get("timestamp"))
        symbol = _symbol_of_entry(atm_entry)
        expiry_text = atm_entry.get("expiryDates")
        if not isinstance(expiry_text, str) or not expiry_text:
            raise IngestAdapterError("selected ATM entry carries no 'expiryDates' value")

        call_leg = atm_entry.get("CE") or {}
        put_leg = atm_entry.get("PE") or {}
        call_iv = _positive_float_or_none(call_leg.get("impliedVolatility"))
        put_iv = _positive_float_or_none(put_leg.get("impliedVolatility"))
        usable_ivs = [iv for iv in (call_iv, put_iv) if iv is not None]
        if not usable_ivs:
            raise IngestAdapterError(
                "the selected ATM entry carries no nonzero implied volatility on either leg"
            )
        atm_iv = sum(usable_ivs) / len(usable_ivs)

        return [
            IngestRow(
                values={
                    "underlying": symbol,
                    "expiry_date": expiry_text,
                    "atm_strike": atm_entry.get("strikePrice"),
                    "underlying_value": records.get("underlyingValue"),
                    "call_implied_volatility": call_iv,
                    "put_implied_volatility": put_iv,
                    "atm_implied_volatility": atm_iv,
                    "snapshot_timestamp": records.get("timestamp"),
                },
                effective_date=effective_date,
                natural_key=(symbol, expiry_text),
            )
        ]

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Two independent checks: is this the expiry we asked for, and is this snapshot
        actually dated the day we asked for. Either failing means the payload is not what
        `target` requested — the same defence this whole project applies everywhere
        (`research/209` §1), specialised to a source whose only date param is a query string
        rather than part of the URL path."""
        try:
            document = _load_option_chain_document(payload)
        except IngestAdapterError as failure:
            return str(failure)
        records = document["records"]

        requested_expiry = _expiry_query_param(target.url)
        entries = records.get("data") if isinstance(records.get("data"), list) else []
        payload_expiries = {
            entry.get("expiryDates") for entry in entries if isinstance(entry, dict)
        }
        if requested_expiry and requested_expiry not in payload_expiries:
            present = sorted(value for value in payload_expiries if value)
            return (
                f"no data for the requested expiry {requested_expiry!r} in this payload "
                f"(payload carries: {present})"
            )

        requested_calendar_date = _requested_calendar_date(target.expects)
        if target.expects and requested_calendar_date is None:
            # Fail closed: the caller asked for something this adapter cannot interpret,
            # so it cannot honestly claim the payload matches it.
            return (
                f"cannot interpret the requested date {target.expects!r} — expected an "
                f"ISO calendar date, so this payload cannot be confirmed as the right one"
            )
        if requested_calendar_date is not None:
            try:
                snapshot_date = _parse_snapshot_date(records.get("timestamp"))
            except IngestAdapterError as failure:
                return str(failure)
            if snapshot_date != requested_calendar_date:
                return (
                    f"snapshot is dated {snapshot_date.isoformat()}, requested "
                    f"{requested_calendar_date.isoformat()} — this is a live rolling feed and "
                    f"cannot serve a past date"
                )
        return None
