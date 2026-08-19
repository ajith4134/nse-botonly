"""Acquire NSE daily bhavcopy archives to disk, resumably and politely.

**This is data acquisition, not the universe engine.** It puts raw exchange bytes
on disk with provenance; parsing and interval reconstruction belong to
`204`'s engine and go through the full R.23 loop. Kept as a script deliberately.

Two URL regimes, boundary measured rather than assumed (`docs/research/204` §7.1):
legacy per-day ZIPs up to 2024-07-05, UDiFF from 2024-07-01. The overlap is real,
so both are attempted for dates inside it and either satisfies the day.

Politeness is the point of the pacing: this walks ~26 years of daily files. The
delay is derived from the observed response time rather than fixed, so a slow or
struggling origin is asked for less, not more.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

ARCHIVE_ROOT = Path("/home/opc/nse_archive")
MANIFEST_PATH = ARCHIVE_ROOT / "manifest" / "bhavcopy_acquisition.sqlite3"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MONTH_CODES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
# Measured boundaries, not guesses: legacy 404s from 2024-07-08, UDiFF 404s before 2024-07-01.
LEGACY_LAST_DAY = date(2024, 7, 5)
UDIFF_FIRST_DAY = date(2024, 7, 1)
REQUEST_TIMEOUT_SECONDS = 60
POLITENESS_MULTIPLE = 1.5  # sleep this many times the observed response time
MINIMUM_DELAY_SECONDS = 0.4
MAXIMUM_DELAY_SECONDS = 8.0
RETRY_ATTEMPTS = 3

_MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS acquisition (
    trade_date    TEXT NOT NULL,
    market        TEXT NOT NULL,
    url           TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    http_status   INTEGER,
    byte_count    INTEGER,
    row_count     INTEGER,
    stored_path   TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (trade_date, market)
);
"""


def legacy_url(market: str, day: date) -> str:
    month = MONTH_CODES[day.month - 1]
    stem = "EQUITIES" if market == "cash" else "DERIVATIVES"
    prefix = "cm" if market == "cash" else "fo"
    return (
        f"https://nsearchives.nseindia.com/content/historical/{stem}/{day.year}/{month}/"
        f"{prefix}{day.day:02d}{month}{day.year}bhav.csv.zip"
    )


def udiff_url(market: str, day: date) -> str:
    segment = "CM" if market == "cash" else "FO"
    folder = "cm" if market == "cash" else "fo"
    return (
        f"https://nsearchives.nseindia.com/content/{folder}/"
        f"BhavCopy_NSE_{segment}_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    )


def candidate_urls(market: str, day: date) -> list[str]:
    urls = []
    if day <= LEGACY_LAST_DAY:
        urls.append(legacy_url(market, day))
    if day >= UDIFF_FIRST_DAY:
        urls.append(udiff_url(market, day))
    return urls


def open_manifest() -> sqlite3.Connection:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(MANIFEST_PATH, timeout=30.0)
    connection.executescript(_MANIFEST_SCHEMA)
    connection.commit()
    return connection


def already_settled(connection: sqlite3.Connection, day: date, market: str) -> bool:
    """A date is settled when stored, or when every candidate URL returned 404.

    A 404 on both regimes means the exchange published nothing — a holiday or a
    pre-launch date — and re-asking every run would be the rudest possible loop.
    Transport failures are deliberately *not* settled, so they retry next run.
    """
    row = connection.execute(
        "SELECT outcome FROM acquisition WHERE trade_date = ? AND market = ?",
        (day.isoformat(), market),
    ).fetchone()
    return row is not None and row[0] in {"stored", "absent"}


def record(connection: sqlite3.Connection, **columns: object) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO acquisition (trade_date, market, url, outcome, http_status,"
        " byte_count, row_count, stored_path, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            columns["trade_date"],
            columns["market"],
            columns["url"],
            columns["outcome"],
            columns.get("http_status"),
            columns.get("byte_count"),
            columns.get("row_count"),
            columns.get("stored_path"),
            columns["fetched_at"],
        ),
    )
    connection.commit()


def fetch_once(url: str) -> tuple[int, bytes, float]:
    request = urllib.request.Request(  # noqa: S310 - fixed https NSE archive host
        url, headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "*/*"}
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = response.read()
        return response.status, payload, time.monotonic() - started


def rows_in_zip(payload: bytes) -> int | None:
    """Row count if this really is a CSV zip. A 200 is not proof of content.

    The sourcing sweep found an NSE endpoint returning HTTP 200 with a 404 error
    page as the body, so every payload is opened before it counts as acquired.
    """
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            name = archive.namelist()[0]
            text = archive.read(name).decode("utf-8", "replace")
    except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    return max(len(lines) - 1, 0)


def acquire_day(connection: sqlite3.Connection, market: str, day: date) -> str:
    stamp = day.isoformat()
    urls = candidate_urls(market, day)
    last_status: int | None = None
    for url in urls:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                status, payload, elapsed = fetch_once(url)
            except urllib.error.HTTPError as error:
                last_status = error.code
                if error.code == 404:  # noqa: PLR2004 - HTTP status, a protocol fact
                    break
                time.sleep(min(MAXIMUM_DELAY_SECONDS, 2**attempt + random.random()))  # noqa: S311
                continue
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(min(MAXIMUM_DELAY_SECONDS, 2**attempt + random.random()))  # noqa: S311
                continue

            if not payload:
                # HTTP 200 with a zero-byte body: the archive genuinely holds an
                # empty file for this date. Measured once, on 1995-09-06. Settled
                # rather than failed, so it is not retried on every future run.
                record(
                    connection,
                    trade_date=stamp,
                    market=market,
                    url=url,
                    outcome="absent",
                    http_status=status,
                    byte_count=0,
                    fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                return "absent"

            rows = rows_in_zip(payload)
            if rows is None:
                record(
                    connection,
                    trade_date=stamp,
                    market=market,
                    url=url,
                    outcome="not_a_zip",
                    http_status=status,
                    byte_count=len(payload),
                    fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                break

            destination = ARCHIVE_ROOT / market / f"{day:%Y}" / f"{market}_{stamp}.csv.zip"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            record(
                connection,
                trade_date=stamp,
                market=market,
                url=url,
                outcome="stored",
                http_status=status,
                byte_count=len(payload),
                row_count=rows,
                stored_path=str(destination),
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            time.sleep(
                max(
                    MINIMUM_DELAY_SECONDS, min(MAXIMUM_DELAY_SECONDS, elapsed * POLITENESS_MULTIPLE)
                )
            )
            return "stored"

    record(
        connection,
        trade_date=stamp,
        market=market,
        url=urls[-1] if urls else "",
        outcome="absent" if last_status == 404 else "failed",  # noqa: PLR2004
        http_status=last_status,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    time.sleep(MINIMUM_DELAY_SECONDS)
    return "absent" if last_status == 404 else "failed"  # noqa: PLR2004


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["cash", "fo"], required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    arguments = parser.parse_args()

    connection = open_manifest()
    tally = {"stored": 0, "absent": 0, "failed": 0, "skipped": 0}
    day = arguments.start
    while day <= arguments.end:
        if day.weekday() >= 5:  # noqa: PLR2004 - Saturday/Sunday; NSE never publishes
            day += timedelta(days=1)
            continue
        if already_settled(connection, day, arguments.market):
            tally["skipped"] += 1
        else:
            tally[acquire_day(connection, arguments.market, day)] += 1
        if sum(tally.values()) % 250 == 0:
            print(f"{day} {arguments.market} {tally}", flush=True)
        day += timedelta(days=1)

    print(f"DONE {arguments.market} {arguments.start}..{arguments.end} {tally}", flush=True)
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
