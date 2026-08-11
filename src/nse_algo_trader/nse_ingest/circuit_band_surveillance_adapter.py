"""`L0.28` — per-symbol circuit band, GSM stage and ASM stage: three surveillance states.

**Why this adapter matters more than its row count suggests.** A stock under ASM or GSM
does not trade normally: margins are raised, settlement moves to trade-for-trade (no
intraday netting), and in the harsher ASM stages the stock trades only in periodic call
auctions rather than continuously. A circuit band caps how far a price can move in a
session before trading halts. Any strategy that sizes a position, expects same-day
exit, or assumes continuous order matching is silently wrong for a symbol in one of
these states — this is a real, previously-measured loss mode, not a hypothetical one,
which is why `research/209` calls this source out by name rather than leaving it to be
discovered as a live-trading surprise.

**Two live NSE files, three states.**

- `sec_list.csv` (`nsearchives.nseindia.com`, free, unauthenticated) carries the circuit
  **Band** column directly, and carries **GSM** stage inside its free-text `Remarks`
  column (`"GSM STAGE - 0"`, `"- I"`, `"- II"` observed live; `research/207` §7).
- **ASM was reported BLOCKED in `research/207`** — that reconnaissance tried
  `www.nseindia.com/reports/asm` (called it "a React/Next.js SPA shell with no fetchable
  data") and a guessed `/api/reports/asm` (404), then stopped. Per `R.16` this adapter
  made its own attempt before accepting that conclusion, and the conclusion does not
  hold: `/reports/asm` is **not** a React/Next.js app at all — it is a jQuery page
  (`B.on(document, 'allGood', ...)`) that loads
  `/dist/js/sections/reports/asm.js`, and that script contains the real call:
  `B.get('/api/reportASM')` (capital ASM — the guessed lower-case `/api/reports/asm`
  path was simply the wrong URL). Fetched directly with nothing but a browser
  `User-Agent`, zero cookies, zero session priming:

  ```
  GET https://www.nseindia.com/api/reportASM
  → HTTP 200, application/json, 50,270 bytes
  → {"longterm": {"data": [...128 entries...]}, "shortterm": {"data": [...61 entries...]}}
  ```

  Reproduced on a second, cookie-free connection with byte-identical content. **ASM is
  therefore reachable and this adapter covers all three states** — there is no blocker
  to report for this source. (`research/207`'s "BLOCKED" verdict for `circuit_band_asm_gsm`
  should be corrected by whoever next touches that document; this adapter's two owned
  files cannot do that per the wave-2 file-ownership rule, so it is recorded here with
  the exact evidence instead.)

**The two files are honest to very different degrees, and `content_mismatch_reason`
reflects that asymmetry rather than pretending it away:**

- `reportASM` carries an **"as on" date inside every entry** (`asmTime`, e.g.
  `"11-Aug-2026"`, uniform across all entries observed in one fetch) — exactly the kind
  of self-declared date `fo_ban_list`'s trade-date header provides, so a wrong-day
  response IS detectable here, the same way it is there.
- `sec_list.csv` carries **no date anywhere in its body** — not a header sentence like
  the ban list, not a date column, nothing. This is a **harder case than `fo_ban_list`**:
  that adapter's `content_mismatch_reason` can prove a payload is for the wrong day;
  this one structurally cannot, ever, for the circuit-band/GSM half of the source. All
  it can verify is that the payload still has the right shape (the five expected
  columns). A stale `sec_list.csv` silently served for a past date is **undetectable
  from content alone** — the same failure mode measured on a sibling NSE endpoint
  (`research/209` §"content_mismatch_reason is the important one"), except here there is
  no defence against it at all. Callers must never request a non-"today" date from the
  circuit-band/GSM half of this source; the adapter cannot catch it if they do.

**Circuit band values are stored as NSE writes them, not normalised.** The `Band` column
holds `"2"`, `"5"`, `"10"`, `"20"` or the literal string `"No Band"` (all five observed
live in the real fixture, 3,335 rows). `"No Band"` is a real, common state (unrestricted
symbols) and is kept as the source's own text in `circuit_band_raw`; a convenience
`circuit_band_percent` field is *added* alongside it (parsed to `int`, `None` when the
raw value is not numeric) rather than replacing the raw text, so nothing the source said
is lost by adding a numeric shortcut.

**A real data-quality anomaly in `reportASM`, handled rather than crashed on.** The live
short-term ASM list served on 2026-08-11 contains the same `(symbol="DCI",
isin="INE0A1101019")` twice — once as `"DC Infotech and Communication Limited"`, once as
`"Dc Infotech And Communication Limited"` (a casing difference only; same ISIN, same
stage). A natural key of `(symbol, term, isin)` alone would collide on this real payload,
and the conformance suite explicitly forbids that (`test_parsed_rows_are_well_formed`
rejects a natural key that repeats within a payload) — silently keeping only one of the
two would also match the shape of the exact bug the suite exists to catch. This adapter
counts occurrences of each base key within one payload and appends a
`duplicate-occurrence-N` disambiguator from the second occurrence onward, so the
overwhelmingly common one-entry-per-symbol case gets a clean, day-stable key
`(symbol, term, isin)`, while the rare duplicate is preserved rather than dropped.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    RollingSnapshotSource,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
"""Circuit band + GSM. Verified free, unauthenticated, HTTP 200, 2026-08-11 (`research/207`)."""

REPORT_ASM_URL = "https://www.nseindia.com/api/reportASM"
"""ASM. Verified free, unauthenticated, HTTP 200, 2026-08-11 — found by reading the real
`asm.js` bundle the `/reports/asm` page loads, not by guessing (see module docstring)."""

_SEC_LIST_EXPECTED_HEADER = ("Symbol", "Series", "Security Name", "Band", "Remarks")
"""Verified exact header of `sec_list.csv` (`research/207` §7); the sole structural
guard `content_mismatch_reason` can offer for this half of the source."""

CIRCUIT_BAND_ASM_GSM_COVERAGE_FLOOR = date(1994, 11, 3)
"""A circuit band, GSM or ASM state is a property of a symbol trading on NSE's cash
equity segment, which cannot predate the segment's own launch — the same verified floor
`nse_bhavcopy_adapter.py`'s `CASH_COVERAGE_FLOOR` uses, re-stated here rather than
imported so this adapter has no dependency on another adapter's module. This is a
conservative outer bound only: GSM (SEBI framework, ~2017) and ASM (~2018) are both far
newer, and both `sec_list.csv` and `reportASM` are rolling snapshots with no archive
(`research/207` §7) — realised coverage begins only at this project's first captured
snapshot, exactly as `fo_ban_list_adapter.py` documents for its own rolling file."""

CIRCUIT_BAND_ASM_GSM_ROLLING_SOURCE = RollingSnapshotSource(
    source_name="circuit_band_asm_gsm",
    reason=(
        "sec_list.csv and reportASM are both single rolling snapshots NSE overwrites "
        "in place; neither has a dated archive, so history accrues only from the day "
        "snapshotting starts"
    ),
)

_ASM_SECTIONS: tuple[tuple[str, str], ...] = (
    ("longterm", "asm_long_term"),
    ("shortterm", "asm_short_term"),
)
"""(JSON key in the `reportASM` payload, self-describing term stored on the row)."""

_FIRST_OCCURRENCE = 1


def _circuit_band_percent(raw_band: str) -> int | None:
    """The numeric percentage, or `None` for `"No Band"` and anything else non-numeric.

    Adds a convenience field; does not replace `circuit_band_raw`, which keeps whatever
    NSE actually wrote (`"No Band"` included) untouched.
    """
    stripped = raw_band.strip()
    return int(stripped) if stripped.isdigit() else None


def _extract_gsm_stage(remarks: str) -> str | None:
    """The GSM stage token out of `sec_list.csv`'s free-text `Remarks` column.

    Observed live: `"GSM STAGE - 0"`, `"GSM STAGE - I"`, `"GSM STAGE - II"`, and a plain
    `"-"` (no GSM) — `research/207` §7. Matched loosely enough to also catch a stage
    this project has not seen yet (e.g. `III`/`IV`), since the *absence* of higher
    stages in one snapshot is a fact about today's data, not a schema limit.
    """
    marker = "GSM STAGE"
    upper = remarks.upper()
    position = upper.find(marker)
    if position == -1:
        return None
    tail = remarks[position + len(marker) :].lstrip()
    stage = tail.lstrip("-").strip()
    return stage or None


def _sec_list_rows(payload: bytes) -> list[dict[str, str]]:
    """`sec_list.csv` rows, or a raised error naming exactly what was wrong.

    The only structural guard available for this half of the source: there is no date
    inside the file to check, so a right-shaped-but-stale payload passes every check
    this function can perform (see module docstring).
    """
    text = payload.decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = tuple((name or "").strip() for name in (reader.fieldnames or ()))
    if fieldnames != _SEC_LIST_EXPECTED_HEADER:
        raise IngestAdapterError(
            f"sec_list.csv header {fieldnames!r} does not match the verified schema "
            f"{_SEC_LIST_EXPECTED_HEADER!r} — payload is not this source's real file"
        )
    rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise IngestAdapterError(
            "sec_list.csv has the right header but zero data rows — NSE's live "
            "securities master cannot legitimately be empty, so this is a corrupt or "
            "truncated payload, not a quiet day"
        )
    return rows


def _decode_report_asm_json(payload: bytes) -> dict[str, Any]:
    """The `reportASM` payload as a dict, or a raised error.

    Structural validity (right top-level shape) is checked here and only here; a
    genuinely empty `data` list inside a validly-shaped payload is not an error — it is
    the same "nothing in this state today" quiet day `fo_ban_list` treats as valid.
    """
    try:
        decoded: Any = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as failure:
        raise IngestAdapterError(f"reportASM payload is not UTF-8: {failure}") from failure
    except json.JSONDecodeError as failure:
        raise IngestAdapterError(f"reportASM payload is not valid JSON: {failure}") from failure
    if not isinstance(decoded, dict):
        raise IngestAdapterError("reportASM payload is not a JSON object at the top level")
    return decoded


def _report_asm_entries(decoded: dict[str, Any], json_key: str) -> list[dict[str, Any]]:
    section = decoded.get(json_key)
    if not isinstance(section, dict) or "data" not in section:
        raise IngestAdapterError(
            f"reportASM payload has no well-formed {json_key!r} section — verified "
            f"shape is {{'longterm': {{'data': [...]}}, 'shortterm': {{'data': [...]}}}}"
        )
    entries = section["data"]
    if not isinstance(entries, list):
        raise IngestAdapterError(f"reportASM {json_key!r}.data is not a list")
    return entries


def _asm_effective_date(entry: dict[str, Any], json_key: str, symbol: str) -> date:
    """The date this ASM record is as-on, read from inside the entry itself.

    Every real entry observed carries `asmTime` (e.g. `"11-Aug-2026"`) — this is the
    self-declared date `sec_list.csv` does not have, and the reason the ASM half of this
    source CAN detect a stale response while the circuit-band half cannot.
    """
    raw_date = entry.get("asmTime")
    if not raw_date or not str(raw_date).strip():
        raise IngestAdapterError(f"reportASM {json_key} row for {symbol!r} carries no asmTime")
    try:
        return datetime.strptime(str(raw_date).strip(), "%d-%b-%Y").date()  # noqa: DTZ007 — a calendar date
    except ValueError as failure:
        raise IngestAdapterError(f"unparseable asmTime {raw_date!r} for {symbol!r}") from failure


class CircuitBandSurveillanceAdapter:
    """Circuit band, GSM stage and ASM stage — three surveillance states, two sources.

    `parse` and `content_mismatch_reason` both dispatch on `target.url`: `REPORT_ASM_URL`
    goes to the JSON path, everything else (including `sec_list.csv` and any test target)
    goes to the CSV path, matching how `fetch_targets` is the only place that constructs
    a `REPORT_ASM_URL` target in normal use.
    """

    @property
    def source_name(self) -> str:
        return "circuit_band_asm_gsm"

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(
            earliest_date=CIRCUIT_BAND_ASM_GSM_COVERAGE_FLOOR,
            established_by=(
                "cash equity segment launch (shared floor with nse_bhavcopy_adapter); "
                "both underlying files are rolling snapshots with no archive, so "
                "realised coverage begins at this project's first captured snapshot"
            ),
        )

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """Both sources, claimed for the most recent requested date.

        Neither `sec_list.csv` nor `reportASM` can serve a specific past date — both are
        single rolling snapshots (`research/207` §7) — so, like `fo_ban_list`, this
        cannot honour a backfill request; unlike `fo_ban_list`, only the ASM half can
        detect being asked for a date it cannot serve (see module docstring). Callers
        are expected to request only "today", the same discipline `fo_ban_list` requires.
        """
        if not for_dates:
            return []
        requested = max(for_dates)
        if requested < CIRCUIT_BAND_ASM_GSM_COVERAGE_FLOOR:
            return []
        expects = requested.isoformat()
        return [
            FetchTarget(url=SEC_LIST_URL, source_name=self.source_name, expects=expects),
            FetchTarget(url=REPORT_ASM_URL, source_name=self.source_name, expects=expects),
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        if target.url == REPORT_ASM_URL:
            return self._parse_report_asm(payload)
        return self._parse_circuit_band_gsm(payload, target)

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        if target.url == REPORT_ASM_URL:
            return self._asm_content_mismatch_reason(payload, target)
        return self._circuit_band_gsm_content_mismatch_reason(payload)

    # ---------------------------------------------------------- circuit band + GSM

    def _parse_circuit_band_gsm(
        self, payload: bytes, target: FetchTarget
    ) -> Sequence[IngestRow]:
        if not target.expects:
            raise IngestAdapterError(
                "sec_list.csv carries no in-payload date; this adapter can only "
                "attribute a snapshot to the date the caller requested via the fetch "
                "target, and none was supplied"
            )
        try:
            effective_date = date.fromisoformat(target.expects)
        except ValueError as failure:
            raise IngestAdapterError(
                f"unparseable requested date {target.expects!r}"
            ) from failure

        parsed: list[IngestRow] = []
        for row in _sec_list_rows(payload):
            symbol = row.get("Symbol", "")
            if not symbol:
                raise IngestAdapterError(f"sec_list.csv row has no Symbol: {row!r}")
            series = row.get("Series", "")
            band_raw = row.get("Band", "")
            remarks_raw = row.get("Remarks", "")
            parsed.append(
                IngestRow(
                    values={
                        "symbol": symbol,
                        "series": series,
                        "security_name": row.get("Security Name", ""),
                        "circuit_band_raw": band_raw,
                        "circuit_band_percent": _circuit_band_percent(band_raw),
                        "gsm_stage": _extract_gsm_stage(remarks_raw),
                        "remarks_raw": remarks_raw,
                    },
                    effective_date=effective_date,
                    natural_key=(symbol, series, "circuit_band_gsm"),
                )
            )
        return parsed

    def _circuit_band_gsm_content_mismatch_reason(self, payload: bytes) -> str | None:
        """Structural only. `sec_list.csv` has no date to compare — see module docstring
        for exactly what this can and cannot catch."""
        try:
            _sec_list_rows(payload)
        except IngestAdapterError as failure:
            return str(failure)
        return None

    # -------------------------------------------------------------------------- ASM

    def _parse_report_asm(self, payload: bytes) -> Sequence[IngestRow]:
        decoded = _decode_report_asm_json(payload)
        parsed: list[IngestRow] = []
        for json_key, term in _ASM_SECTIONS:
            entries = _report_asm_entries(decoded, json_key)
            occurrence_counts: dict[tuple[str, str, str], int] = {}
            for entry in entries:
                symbol = str(entry.get("symbol") or "").strip()
                isin = str(entry.get("isin") or "").strip()
                if not symbol or not isin:
                    raise IngestAdapterError(
                        f"reportASM {json_key} row missing symbol or isin: {entry!r}"
                    )
                effective_date = _asm_effective_date(entry, json_key, symbol)
                base_key = (symbol, term, isin)
                occurrence = occurrence_counts.get(base_key, 0) + 1
                occurrence_counts[base_key] = occurrence
                natural_key = (
                    base_key
                    if occurrence == _FIRST_OCCURRENCE
                    else (*base_key, f"duplicate-occurrence-{occurrence}")
                )
                parsed.append(
                    IngestRow(
                        values={
                            "symbol": symbol,
                            "isin": isin,
                            "company_name": str(entry.get("companyName") or ""),
                            "asm_term": term,
                            "asm_stage": str(entry.get("asmSurvIndicator") or ""),
                            "asm_surveillance_code": str(entry.get("survCode") or ""),
                            "asm_surveillance_description": str(
                                entry.get("survDesc") or ""
                            ),
                            "as_on_date": effective_date.isoformat(),
                        },
                        effective_date=effective_date,
                        natural_key=natural_key,
                    )
                )
        return parsed

    def _asm_content_mismatch_reason(
        self, payload: bytes, target: FetchTarget
    ) -> str | None:
        try:
            rows = self._parse_report_asm(payload)
        except IngestAdapterError as failure:
            return str(failure)
        if not target.expects or not rows:
            # Nothing to compare a genuinely empty ASM report against; an empty report
            # is itself a valid, if fortunate, state — see `_decode_report_asm_json`.
            return None
        observed_dates = {row.effective_date.isoformat() for row in rows}
        if target.expects not in observed_dates:
            return (
                f"reportASM is as-on {sorted(observed_dates)}, requested "
                f"{target.expects} — a rolling live report cannot serve a past date"
            )
        return None
