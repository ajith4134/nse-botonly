"""The bulk pass: 14,314 archived files into one queryable store, with a coverage ledger.

**Two readers, on purpose.** `DeepHistoryBhavcopyReader` parses row by row in Python and is
the CORRECTNESS REFERENCE — every hazard in `docs/research/218` §2 is expressed there in
readable code with a test each. It runs at ~7,400 rows/s, which over 193,752,000 rows is
about seven hours. This module does the same work vectorised through `polars` at roughly a
hundred times that rate, and `test_the_vectorised_loader_agrees_with_the_reference_reader`
asserts on REAL files that the two produce identical rows.

That is the same differential-oracle discipline `O.57` named: the fast path is the one that
runs, the slow path is the one that is obviously right, and a property test keeps them
honest. A single fast implementation would have nothing to be checked against.

**DuckDB is the store, and it was measured rather than assumed** (`docs/research/218` §7).
On 390,235 real rows: DuckDB loads in 1.22 s and answers a cross-sectional GROUP BY in
6.6 ms; SQLite with B-tree indices wins single-key lookups (0.49 ms vs 3.15 ms) but takes
205 ms for the same GROUP BY — 31x slower — and that cross-sectional query IS the access
pattern a point-in-time backtest has. At 193 million rows the gap widens, because SQLite has
no vectorised execution to widen it with.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import polars as pl

from nse_algo_trader.deep_history.bhavcopy_variant_resolver import (
    BhavcopyVariant,
    BhavcopyVariantResolver,
    option_type_column,
)
from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import (
    PAISE_PER_RUPEE,
    RUPEES_PER_LAKH,
    trade_date_from_archive_name,
)

DEFAULT_ARCHIVE_ROOT = Path("/home/opc/nse_archive")
DEFAULT_DEEP_HISTORY_PATH = Path("~/.nse_algo_trader/deep_history.duckdb").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bar (
    trade_date DATE NOT NULL,
    market VARCHAR NOT NULL,
    trading_symbol VARCHAR NOT NULL,
    series VARCHAR,
    instrument_type VARCHAR,
    expiry DATE,
    strike_paise BIGINT,
    option_type VARCHAR,
    open_paise BIGINT,
    high_paise BIGINT,
    low_paise BIGINT,
    close_paise BIGINT NOT NULL,
    previous_close_paise BIGINT,
    traded_quantity BIGINT NOT NULL,
    traded_value_paise HUGEINT NOT NULL,
    trade_count BIGINT,
    open_interest BIGINT,
    isin VARCHAR,
    variant VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS loaded_file (
    source_path VARCHAR NOT NULL PRIMARY KEY,
    market VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    variant VARCHAR NOT NULL,
    row_count BIGINT NOT NULL,
    quarantined_count BIGINT NOT NULL,
    loaded_at TIMESTAMP NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class DeepHistoryLoadReport:
    """What one load pass did, including what it refused."""

    files_loaded: int
    files_skipped_already_loaded: int
    files_unreadable: tuple[tuple[str, str], ...]
    rows_written: int
    rows_quarantined: int
    seconds: float

    @property
    def rows_per_second(self) -> float:
        return self.rows_written / self.seconds if self.seconds else 0.0


@dataclass(frozen=True, slots=True)
class DeepHistoryCoverage:
    """What the store holds, per market — the ledger `R.11` needs."""

    market: str
    files: int
    rows: int
    earliest: date | None
    latest: date | None
    distinct_symbols: int


class DeepHistoryArchiveLoader:
    """Loads the archive into DuckDB, one file at a time, resumably."""

    def __init__(
        self,
        *,
        archive_root: Path = DEFAULT_ARCHIVE_ROOT,
        database_path: Path = DEFAULT_DEEP_HISTORY_PATH,
        resolver: BhavcopyVariantResolver | None = None,
        read_only: bool = False,
    ) -> None:
        """`read_only` is not a nicety — DuckDB takes an exclusive lock per database file.

        A reader that opens for writing cannot coexist with a load, and the dashboard is
        exactly such a reader: `/history` would have raised `Conflicting lock is held` for
        the whole duration of any load, which on a first run is minutes. Measured while
        loading the archive, by trying to query it.
        """
        self._archive_root = archive_root
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._resolver = resolver or BhavcopyVariantResolver()
        self._read_only = read_only
        self._connection = duckdb.connect(str(database_path), read_only=read_only)
        if not read_only:
            self._connection.execute(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DeepHistoryArchiveLoader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- loading -------------------------------------------------------------------------

    def archive_files(self, markets: Sequence[str] = ("cash", "fo")) -> list[Path]:
        """Every archived zip, oldest first, so a partial load is a prefix of history."""
        files: list[Path] = []
        for market in markets:
            files.extend(sorted((self._archive_root / market).rglob(f"{market}_*.csv.zip")))
        return files

    def load(
        self,
        *,
        markets: Sequence[str] = ("cash", "fo"),
        limit: int | None = None,
        reload_existing: bool = False,
    ) -> DeepHistoryLoadReport:
        """Load every not-yet-loaded file. Safe to interrupt and resume."""
        started = datetime.now().timestamp()  # noqa: DTZ005 - a duration, not an instant
        already = set() if reload_existing else self._already_loaded()
        loaded = skipped = rows = quarantined_total = 0
        unreadable: list[tuple[str, str]] = []
        for path in self.archive_files(markets)[: limit or None]:
            if str(path) in already:
                skipped += 1
                continue
            try:
                frame, variant = self._frame_for(path)
            except Exception as failure:  # noqa: BLE001 - see below; one file, not the run
                # Recorded rather than raised: one unreadable file out of 14,314 must not
                # cost the other 14,313, and a silent skip would be indistinguishable from
                # a file that simply held no rows. The catch is BROAD deliberately — the
                # first version listed three exception types and adversarial review showed
                # a single NULL in a NOT NULL column aborting the entire run through
                # `duckdb.ConstraintException`, and a missing column doing the same through
                # polars' `ColumnNotFoundError`. Every failure here is "this file", and the
                # name and reason are reported.
                unreadable.append((path.name, f"{type(failure).__name__}: {failure}"))
                continue
            file_date = trade_date_from_archive_name(path)
            frame, quarantined = _without_impossible_bars(frame)
            frame, mismatched = _only_rows_dated(frame, file_date)
            quarantined += mismatched
            frame, duplicated = _without_repeated_instruments(frame)
            quarantined += duplicated
            # A file whose every row was quarantined is still a file that was PROCESSED.
            # Recording it keeps the ledger honest and stops the next run retrying it
            # forever; skipping the write entirely is what crashed the second full load,
            # because `_write` read row zero of an empty frame.
            if frame.is_empty() and quarantined:
                # Every row refused means the FILE could not be read, not that the session
                # was empty — `fo_2012-05-14` and `cash_2020-07-13` were recorded as loaded
                # with zero rows while `/history` called the market complete. A file that
                # yields nothing is now an unreadable file, and says so.
                unreadable.append(
                    (path.name, f"all {quarantined:,} rows refused — file not loaded")
                )
                continue
            self._write(
                frame,
                path=path,
                variant=variant,
                quarantined=quarantined,
                file_date=file_date,
            )
            loaded += 1
            rows += frame.height
            quarantined_total += quarantined
        return DeepHistoryLoadReport(
            files_loaded=loaded,
            files_skipped_already_loaded=skipped,
            files_unreadable=tuple(unreadable),
            rows_written=rows,
            rows_quarantined=quarantined_total,
            seconds=datetime.now().timestamp() - started,  # noqa: DTZ005 - a duration
        )

    def _already_loaded(self) -> set[str]:
        return {
            str(row[0])
            for row in self._connection.execute("SELECT source_path FROM loaded_file").fetchall()
        }

    def _write(
        self,
        frame: pl.DataFrame,
        *,
        path: Path,
        variant: BhavcopyVariant,
        quarantined: int,
        file_date: date | None,
    ) -> None:
        if not frame.is_empty():
            arrow_table = frame.to_arrow()
            self._connection.register("incoming_rows", arrow_table)
            self._connection.execute("INSERT INTO daily_bar SELECT * FROM incoming_rows")
            self._connection.unregister("incoming_rows")
        market = frame["market"][0] if not frame.is_empty() else _market_from_name(path)
        trade_date = frame["trade_date"][0] if not frame.is_empty() else file_date
        self._connection.execute(
            "INSERT OR REPLACE INTO loaded_file VALUES (?,?,?,?,?,?,now())",
            [str(path), market, trade_date, variant.value, frame.height, quarantined],
        )

    # -- parsing -------------------------------------------------------------------------

    def _frame_for(self, path: Path) -> tuple[pl.DataFrame, BhavcopyVariant]:
        """One file as a normalised frame, in the same shape the reference reader produces."""
        with zipfile.ZipFile(path) as archive:
            member = archive.namelist()[0]
            payload = archive.read(member)
        header = payload.split(b"\n", 1)[0].decode("utf-8", "replace").strip().split(",")
        variant = self._resolver.resolve(header)
        raw = pl.read_csv(
            payload,
            infer_schema_length=0,  # every column as text; conversion is explicit below
            truncate_ragged_lines=True,
        )
        raw.columns = [column.strip() for column in raw.columns]
        if variant is BhavcopyVariant.UDIFF:
            return self._udiff_frame(raw), variant
        if variant is BhavcopyVariant.FO_LEGACY:
            return self._legacy_derivative_frame(raw, header), variant
        return self._legacy_cash_frame(raw, variant), variant

    @staticmethod
    def _paise(column: str, *, scale: int = 1) -> pl.Expr:
        return (
            (
                pl.col(column).str.strip_chars().cast(pl.Float64, strict=False)
                * scale
                * PAISE_PER_RUPEE
            )
            .round(0)
            .cast(pl.Int64, strict=False)
        )

    @classmethod
    def _traded_price(cls, column: str, quantity_column: str) -> pl.Expr:
        """Zero prices on a zero-volume day mean "did not trade", not "cost nothing"."""
        value = cls._paise(column)
        quantity = pl.col(quantity_column).str.strip_chars().cast(pl.Float64, strict=False)
        return pl.when((value == 0) & (quantity == 0)).then(None).otherwise(value)

    def _legacy_cash_frame(self, raw: pl.DataFrame, variant: BhavcopyVariant) -> pl.DataFrame:
        has_extras = variant is BhavcopyVariant.CASH_LEGACY_V2
        return raw.select(
            trade_date=_legacy_date_expr("TIMESTAMP"),
            market=pl.lit("cash"),
            trading_symbol=pl.col("SYMBOL").str.strip_chars(),
            series=pl.col("SERIES").str.strip_chars(),
            instrument_type=pl.lit(None, dtype=pl.String),
            expiry=pl.lit(None, dtype=pl.Date),
            strike_paise=pl.lit(None, dtype=pl.Int64),
            option_type=pl.lit(None, dtype=pl.String),
            open_paise=self._traded_price("OPEN", "TOTTRDQTY"),
            high_paise=self._traded_price("HIGH", "TOTTRDQTY"),
            low_paise=self._traded_price("LOW", "TOTTRDQTY"),
            close_paise=self._paise("CLOSE"),
            previous_close_paise=self._paise("PREVCLOSE"),
            traded_quantity=_integer_expr("TOTTRDQTY"),
            traded_value_paise=self._paise("TOTTRDVAL"),
            trade_count=(
                _integer_expr("TOTALTRADES") if has_extras else pl.lit(None, dtype=pl.Int64)
            ),
            open_interest=pl.lit(None, dtype=pl.Int64),
            isin=(
                pl.col("ISIN").str.strip_chars() if has_extras else pl.lit(None, dtype=pl.String)
            ),
            variant=pl.lit(variant.value),
        )

    def _legacy_derivative_frame(self, raw: pl.DataFrame, header: Sequence[str]) -> pl.DataFrame:
        option_column = option_type_column(header)
        strike = self._paise("STRIKE_PR")
        option_text = pl.col(option_column).str.strip_chars().str.to_uppercase()
        is_future = (strike == 0) | (option_text == "XX") | option_text.is_null()
        return raw.select(
            trade_date=_legacy_date_expr("TIMESTAMP"),
            market=pl.lit("fo"),
            trading_symbol=pl.col("SYMBOL").str.strip_chars(),
            series=pl.lit(None, dtype=pl.String),
            instrument_type=pl.col("INSTRUMENT").str.strip_chars(),
            expiry=_legacy_date_expr("EXPIRY_DT"),
            strike_paise=pl.when(is_future).then(None).otherwise(strike),
            option_type=pl.when(is_future).then(None).otherwise(option_text),
            open_paise=self._traded_price("OPEN", "CONTRACTS"),
            high_paise=self._traded_price("HIGH", "CONTRACTS"),
            low_paise=self._traded_price("LOW", "CONTRACTS"),
            close_paise=self._paise("CLOSE"),
            previous_close_paise=pl.lit(None, dtype=pl.Int64),
            traded_quantity=_integer_expr("CONTRACTS"),
            # The lakh conversion, applied exactly once, here — as in the reference reader.
            traded_value_paise=self._paise("VAL_INLAKH", scale=RUPEES_PER_LAKH),
            trade_count=pl.lit(None, dtype=pl.Int64),
            open_interest=_integer_expr("OPEN_INT"),
            isin=pl.lit(None, dtype=pl.String),
            variant=pl.lit(BhavcopyVariant.FO_LEGACY.value),
        )

    def _udiff_frame(self, raw: pl.DataFrame) -> pl.DataFrame:
        is_derivative = pl.col("Sgmt").str.strip_chars().str.to_uppercase() == "FO"
        strike_text = pl.col("StrkPric").str.strip_chars()
        option_text = pl.col("OptnTp").str.strip_chars().str.to_uppercase()
        return raw.select(
            trade_date=pl.col("TradDt").str.strip_chars().str.to_date("%Y-%m-%d", strict=False),
            market=pl.when(is_derivative).then(pl.lit("fo")).otherwise(pl.lit("cash")),
            trading_symbol=pl.col("TckrSymb").str.strip_chars(),
            series=pl.when(is_derivative).then(None).otherwise(pl.col("SctySrs").str.strip_chars()),
            instrument_type=pl.when(is_derivative)
            .then(pl.col("FinInstrmTp").str.strip_chars())
            .otherwise(None),
            expiry=pl.col("XpryDt").str.strip_chars().str.to_date("%Y-%m-%d", strict=False),
            strike_paise=pl.when(strike_text.is_null() | (strike_text == ""))
            .then(None)
            .otherwise(self._paise("StrkPric")),
            option_type=pl.when(option_text.is_null() | (option_text == ""))
            .then(None)
            .otherwise(option_text),
            open_paise=self._traded_price("OpnPric", "TtlTradgVol"),
            high_paise=self._traded_price("HghPric", "TtlTradgVol"),
            low_paise=self._traded_price("LwPric", "TtlTradgVol"),
            close_paise=self._paise("ClsPric"),
            previous_close_paise=self._paise("PrvsClsgPric"),
            traded_quantity=_integer_expr("TtlTradgVol"),
            traded_value_paise=self._paise("TtlTrfVal"),
            trade_count=_integer_expr("TtlNbOfTxsExctd"),
            open_interest=_integer_expr("OpnIntrst"),
            isin=pl.col("ISIN").str.strip_chars(),
            variant=pl.lit(BhavcopyVariant.UDIFF.value),
        )

    # -- reading back ---------------------------------------------------------------------

    def coverage(self) -> tuple[DeepHistoryCoverage, ...]:
        rows = self._connection.execute(
            "SELECT market, COUNT(*) AS rows, MIN(trade_date), MAX(trade_date), "
            "COUNT(DISTINCT trading_symbol) FROM daily_bar GROUP BY market ORDER BY market"
        ).fetchall()
        files = dict(
            self._connection.execute(
                "SELECT market, COUNT(*) FROM loaded_file GROUP BY market"
            ).fetchall()
        )
        return tuple(
            DeepHistoryCoverage(
                market=str(row[0]),
                files=int(files.get(row[0], 0)),
                rows=int(row[1]),
                earliest=row[2],
                latest=row[3],
                distinct_symbols=int(row[4]),
            )
            for row in rows
        )

    def rows_per_year(self) -> tuple[tuple[int, int], ...]:
        """Loaded rows per calendar year — the coverage story, oldest first."""
        return tuple(
            (int(row[0]), int(row[1]))
            for row in self._connection.execute(
                "SELECT year(trade_date) AS y, COUNT(*) FROM daily_bar GROUP BY y ORDER BY y"
            ).fetchall()
        )

    def quarantined_total(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(quarantined_count), 0) FROM loaded_file"
        ).fetchone()
        return int(row[0]) if row else 0

    def archive_file_counts(self) -> dict[str, int]:
        """How many files the ARCHIVE holds per market, so a shortfall is visible."""
        return {
            market: len(list((self._archive_root / market).rglob(f"{market}_*.csv.zip")))
            for market in ("cash", "fo")
        }

    def symbols_on(self, trade_date: date, *, market: str = "cash") -> tuple[str, ...]:
        """The point-in-time universe: what the exchange published that day, nothing later."""
        rows = self._connection.execute(
            "SELECT DISTINCT trading_symbol FROM daily_bar WHERE trade_date = ? AND market = ? "
            "ORDER BY trading_symbol",
            [trade_date, market],
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def close_series(
        self, trading_symbol: str, *, market: str = "cash"
    ) -> Iterator[tuple[date, int]]:
        """One symbol's whole close history, oldest first."""
        for row in self._connection.execute(
            "SELECT trade_date, close_paise FROM daily_bar WHERE trading_symbol = ? "
            "AND market = ? ORDER BY trade_date",
            [trading_symbol, market],
        ).fetchall():
            yield row[0], int(row[1])


def _market_from_name(path: Path) -> str:
    """`cash` or `fo`, from the archive's own naming, for a file with no surviving rows."""
    return path.name.split("_", 1)[0]


def _only_rows_dated(frame: pl.DataFrame, expected: date | None) -> tuple[pl.DataFrame, int]:
    """Keep only rows whose date matches the one the FILE's own name claims.

    Two real defects hide behind this single check, and neither announced itself:

    - `cash_2020-07-13.csv.zip` writes its dates as `13-Jul-20`, a two-digit year that
      `%Y` parses as the year 20 AD. Both readers accepted it and wrote 2,001 rows dated
      `0020-07-13`. No exception, no null — a valid date, silently thirty centuries wrong.
    - `fo_2002-02-14.csv.zip` contains a SECOND header row 1,853 lines in, so a file can be
      two dumps concatenated. That row parses to a null date and would have hit the store's
      NOT NULL constraint, which is how the whole load stopped.

    A format list can never close the first case, because the wrong answer is well-formed.
    An independent witness can: the archive's file name is evidence about the session that
    does not come from the file's contents.
    """
    if expected is None:
        return frame, 0
    kept = frame.filter(pl.col("trade_date") == pl.lit(expected))
    return kept, frame.height - kept.height


def _without_repeated_instruments(frame: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """One row per instrument identity per file, keeping the first.

    Some 2002-2007 F&O files are several dumps concatenated — `fo_2003-04-23` holds five
    embedded header rows, so every contract appeared six times. Measured across the store
    before this existed: **90,655 excess rows over 59,246 identities, and 3,180 of those
    groups disagreed on the close.** A duplicate is not extra evidence; a duplicate that
    disagrees is ambiguous history, and both are counted rather than silently kept.
    """
    identity = [
        "trading_symbol",
        "series",
        "instrument_type",
        "expiry",
        "strike_paise",
        "option_type",
    ]
    kept = frame.unique(subset=identity, keep="first", maintain_order=True)
    return kept, frame.height - kept.height


def _without_impossible_bars(frame: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Drop rows whose own four prices contradict each other, and count them.

    The same rule the reference reader applies, expressed once here for the whole frame —
    and it must be the same rule, or the differential test that keeps the two readers
    honest would be comparing different definitions of a valid row. Untraded rows carry no
    open, high or low and are therefore unaffected.
    """
    # A row is only judged when all three of its range prices are present: keying the test
    # off `open_paise` alone made the predicate NULL when HIGH or LOW was missing, and
    # `filter(~impossible)` then DROPPED the row while counting it as an impossible bar.
    traded = (
        pl.col("open_paise").is_not_null()
        & pl.col("high_paise").is_not_null()
        & pl.col("low_paise").is_not_null()
    )
    impossible = traded & (
        (pl.col("high_paise") < pl.col("low_paise"))
        | (pl.col("open_paise") > pl.col("high_paise"))
        | (pl.col("open_paise") < pl.col("low_paise"))
        | (pl.col("close_paise") > pl.col("high_paise"))
        | (pl.col("close_paise") < pl.col("low_paise"))
    )
    kept = frame.filter(~impossible)
    return kept, frame.height - kept.height


def _legacy_date_expr(column: str) -> pl.Expr:
    """`2-JAN-1995`, `01-JAN-2015`, `27-Jan-2005` — one expression, three real spellings.

    polars' `to_date` is case-sensitive on `%b`, which the row-wise reader's `strptime` is
    not, so the text is title-cased first. Day padding is inconsistent before ~2015 and
    `%-d` is not portable, so the day is zero-padded by regex rather than by format.
    """
    normalised = pl.col(column).str.strip_chars().str.replace(r"^(\d)-", r"0$1-").str.to_lowercase()
    # **Four-digit years are matched EXPLICITLY, and two-digit ones parsed separately.**
    # chrono's `%Y` happily accepts a two-digit year, so `14-May-12` became `0012-05-14`
    # here while the reference reader — whose format list gained `%d-%b-%y` — read it as
    # 2012. `_only_rows_dated` then discarded every row in the file and `_write` recorded it
    # as loaded with zero rows: `fo_2012-05-14` (31,388 rows) and `cash_2020-07-13` (2,001)
    # vanished from a store that reported itself complete. Adversarial review found it; the
    # differential test could not, because it compared the two paths UPSTREAM of this step.
    four_digit = normalised.str.to_date("%d-%b-%Y", strict=False)
    two_digit = normalised.str.to_date("%d-%b-%y", strict=False)
    return pl.when(normalised.str.contains(r"-\d{4}$")).then(four_digit).otherwise(two_digit)


def _integer_expr(column: str) -> pl.Expr:
    # TRUNCATES, because the reference reader's `int(Decimal(...))` truncates. `CONTRACTS`
    # is fractional in 39 real F&O files (`1.5`, `6.66`, `0.66`), so rounding here stored a
    # different quantity than the reference read — 79 row-instances measured.
    return (
        pl.col(column).str.strip_chars().cast(pl.Float64, strict=False).cast(pl.Int64, strict=False)
    )
