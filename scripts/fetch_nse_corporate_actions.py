"""Acquire NSE corporate-action history and store it with provenance.

**Acquisition, not the adjustment engine.** `L0.07` needs ex-dates and ratios; the
only structured historical source is NSE's corporate-actions feed, reached through
`nselib` (same backend as `www.nseindia.com/api/corporates-corporateActions`).
Verified to 2001; BSE is not available as a cross-check, so this is single-sourced
and that is logged as an accepted risk.

The ratio is **not** a numeric field — it lives in free text (`"Bonus 1:2"`,
`"Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"`).
This script stores the raw text verbatim and parses nothing: parsing is the
engine's job and its failure modes are the real risk in `L0.07`.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from nselib import capital_market

STORE = Path("/home/opc/nse_archive/manifest/corporate_actions.sqlite3")
FIRST_YEAR = 2001  # measured: the feed returns nothing usable before this
POLITENESS_SECONDS = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corporate_action (
    symbol        TEXT NOT NULL,
    ex_date       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    company       TEXT,
    isin          TEXT,
    face_value    TEXT,
    series        TEXT,
    record_date   TEXT,
    fetched_for_year INTEGER NOT NULL,
    PRIMARY KEY (symbol, ex_date, subject)
);
CREATE INDEX IF NOT EXISTS corporate_action_by_date ON corporate_action (ex_date);
CREATE INDEX IF NOT EXISTS corporate_action_by_isin ON corporate_action (isin);
"""


def main() -> int:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STORE, timeout=30.0)
    connection.executescript(_SCHEMA)
    connection.commit()

    last_year = date.today().year  # noqa: DTZ011 - a year boundary, not a market clock
    total = 0
    for year in range(FIRST_YEAR, last_year + 1):
        already = connection.execute(
            "SELECT COUNT(*) FROM corporate_action WHERE fetched_for_year = ?", (year,)
        ).fetchone()[0]
        if already:
            print(f"{year}: {already} rows already stored, skipping", flush=True)
            total += already
            continue
        try:
            frame = capital_market.corporate_actions_for_equity(
                from_date=f"01-01-{year}", to_date=f"31-12-{year}"
            )
        except Exception as error:  # noqa: BLE001 - the library raises bare exceptions
            print(f"{year}: FAILED {type(error).__name__}: {error}", flush=True)
            continue

        rows = [
            (
                str(row.get("symbol", "")).strip(),
                str(row.get("exDate", "")).strip(),
                str(row.get("subject", "")).strip(),
                str(row.get("comp", "")).strip() or None,
                str(row.get("isin", "")).strip() or None,
                str(row.get("faceVal", "")).strip() or None,
                str(row.get("series", "")).strip() or None,
                str(row.get("recDate", "")).strip() or None,
                year,
            )
            for _, row in frame.iterrows()
            if str(row.get("symbol", "")).strip() and str(row.get("exDate", "")).strip()
        ]
        with connection:
            connection.executemany(
                "INSERT OR REPLACE INTO corporate_action (symbol, ex_date, subject, company,"
                " isin, face_value, series, record_date, fetched_for_year)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        total += len(rows)
        print(f"{year}: {len(rows)} rows", flush=True)
        time.sleep(POLITENESS_SECONDS)

    stored = connection.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0]
    span = connection.execute(
        "SELECT MIN(fetched_for_year), MAX(fetched_for_year) FROM corporate_action"
    ).fetchone()
    print(f"DONE stored={stored} (this run touched {total}) years {span[0]}..{span[1]}", flush=True)
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
