"""Validate the expiry-ladder exit signal across the whole F&O archive.

`L0.05b` claims a truncated ladder predicts an F&O exit with lead time. That was
measured on **three** events in a 36-day window and is left-censored. With 26 years
on disk the claim can be tested properly: how many real exits were preceded by a
truncation, how many truncations were followed by an exit, and how much warning.

Streams the archive rather than loading it: per (date, underlying) it keeps only
the distinct-expiry count and the furthest expiry, which is all the signal needs.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ARCHIVE = Path("/home/opc/nse_archive/fo")
OUTPUT = Path("/home/opc/nse_archive/manifest/ladder_exit_validation.json")
STOCK_INSTRUMENTS = {"STO", "STF", "FUTSTK", "OPTSTK"}


def _parse_expiry(text: str) -> date | None:
    text = text.strip()
    for pattern in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def _read(path: Path) -> list[tuple[str, str, str]]:
    """(instrument, symbol, expiry) triples from either bhavcopy layout."""
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = {(name or "").strip().upper(): name for name in (reader.fieldnames or [])}
    legacy = "INSTRUMENT" in fields
    instrument_key = fields.get("INSTRUMENT") or fields.get("FININSTRMTP")
    symbol_key = fields.get("SYMBOL") or fields.get("TCKRSYMB")
    expiry_key = fields.get("EXPIRY_DT") or fields.get("XPRYDT")
    if not (instrument_key and symbol_key and expiry_key):
        return []
    del legacy
    return [
        (
            (row.get(instrument_key) or "").strip(),
            (row.get(symbol_key) or "").strip(),
            (row.get(expiry_key) or "").strip(),
        )
        for row in reader
    ]


def main() -> int:
    files = sorted(ARCHIVE.rglob("fo_*.csv.zip"))
    print(f"{len(files)} archive files", flush=True)
    ladder: dict[date, dict[str, tuple[int, date]]] = {}
    for index, path in enumerate(files):
        stamp = date.fromisoformat(path.stem.replace("fo_", "").replace(".csv", ""))
        expiries: dict[str, set[date]] = defaultdict(set)
        try:
            rows = _read(path)
        except (zipfile.BadZipFile, OSError):
            continue
        for instrument, symbol, expiry_text in rows:
            if instrument.upper() not in STOCK_INSTRUMENTS or not symbol:
                continue
            expiry = _parse_expiry(expiry_text)
            if expiry is not None:
                expiries[symbol].add(expiry)
        if expiries:
            ladder[stamp] = {s: (len(e), max(e)) for s, e in expiries.items()}
        if index % 500 == 0:
            print(f"  {index}/{len(files)} {stamp}", flush=True)

    dates = sorted(ladder)
    print(f"parsed {len(dates)} dates", flush=True)
    position = {stamp: i for i, stamp in enumerate(dates)}

    norms = {}
    for stamp in dates:
        depths = [depth for depth, _ in ladder[stamp].values()]
        counts = Counter(depths)
        norms[stamp] = max(counts.items(), key=lambda item: (item[1], item[0]))[0]

    last_seen: dict[str, date] = {}
    for stamp in dates:
        for symbol in ladder[stamp]:
            last_seen[symbol] = stamp

    # A real exit: absent from every date after `last_seen`, and at least 60
    # sessions of subsequent data exist so end-of-archive is not mistaken for exit.
    horizon = 60
    exits = {
        symbol: stamp for symbol, stamp in last_seen.items()
        if position[stamp] + horizon < len(dates)
    }
    print(f"{len(exits)} genuine exits with a {horizon}-session horizon", flush=True)

    leads, unwarned = [], []
    for symbol, exit_date in exits.items():
        warned_from = None
        for stamp in dates[: position[exit_date] + 1]:
            entry = ladder[stamp].get(symbol)
            if entry is None:
                continue
            depth, furthest = entry
            if depth < norms[stamp]:
                previous = [
                    ladder[s][symbol][1] for s in dates[: position[stamp]]
                    if symbol in ladder[s]
                ]
                if not previous or furthest <= max(previous):
                    warned_from = warned_from or stamp
            else:
                warned_from = None  # ladder recovered; the warning was not sustained
        if warned_from is None:
            unwarned.append(symbol)
        else:
            leads.append(position[exit_date] - position[warned_from])

    # False positives: symbols warned at some point that never exited.
    warned_ever = set()
    for stamp in dates:
        for symbol, (depth, _) in ladder[stamp].items():
            if depth < norms[stamp]:
                warned_ever.add(symbol)
    never_exited = warned_ever - set(exits)

    leads.sort()
    result = {
        "dates_parsed": len(dates),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "underlyings_seen": len({s for stamp in dates for s in ladder[stamp]}),
        "genuine_exits": len(exits),
        "exits_with_warning": len(leads),
        "exits_without_warning": len(unwarned),
        "recall": round(len(leads) / len(exits), 4) if exits else None,
        "lead_sessions_min": leads[0] if leads else None,
        "lead_sessions_median": leads[len(leads) // 2] if leads else None,
        "lead_sessions_p90": leads[int(len(leads) * 0.9)] if leads else None,
        "lead_sessions_max": leads[-1] if leads else None,
        "symbols_warned_ever": len(warned_ever),
        "warned_but_never_exited": len(never_exited),
        "norm_distribution": dict(Counter(norms.values())),
    }
    OUTPUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
