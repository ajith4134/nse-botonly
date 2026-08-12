"""One archived file in, normalised rows out — with every unit and sentinel handled once.

The nine hazards in `docs/research/218` §2 all live here, because this is the only place
that touches a raw NSE row. Everything downstream sees `NormalisedBhavcopyRow`, in which a
futures contract has no strike rather than a strike of zero, traded value is in paise
whatever the file said, and a date is a `date` regardless of which of three formats — two
of which appear in the SAME file — the exchange used that decade.

**Prices are integer paise.** The same reason the depth tape uses them: a float rupee makes
two identical prices compare unequal, and this loader is about to write 193 million rows
that later engines will compare.

**A bad row is QUARANTINED, not skipped.** Across 193,752,000 rows there will be surprises,
and a silent skip is indistinguishable from a clean load — which is the failure this project
refuses in the rule store, the clock engine and the consolidated feed alike. Each rejection
keeps its reason and its raw text so the archive can be audited rather than trusted.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from nse_algo_trader.deep_history.bhavcopy_variant_resolver import (
    BhavcopyVariant,
    BhavcopyVariantResolver,
    option_type_column,
)

PAISE_PER_RUPEE = 100
"""A regulatory fact: the rupee is divided into 100 paise."""

RUPEES_PER_LAKH = 100_000
"""A unit fact, and the single most dangerous number in this file. Legacy F&O reports traded
value in LAKHS of rupees (`VAL_INLAKH`); UDiFF reports it in rupees (`TtlTrfVal`). Mapping
both to "traded value" without this conversion is wrong by five orders of magnitude, and
every value-ranked universe built on it is wrong in a way that looks entirely plausible."""

FUTURES_STRIKE_SENTINEL = "0"
FUTURES_OPTION_TYPE_SENTINEL = "XX"
"""What legacy F&O puts in the option columns of a FUTURES row. Read as data these produce
zero-strike options; they mean "not applicable"."""

_LEGACY_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y")
"""`2-JAN-1995`, `27-Jan-2005`, and — in at least one real file — `13-Jul-20`.

Uppercase in `TIMESTAMP`, title-case in `EXPIRY_DT`, in the same file, with inconsistent day
padding before ~2015. `%b` is case-insensitive in `strptime`, so one pattern covers both
spellings. The two-digit-year pattern is last, and it is the reason `TRADE_DATE_MUST_MATCH_
THE_FILE` exists: `%Y` happily parses `20` as the year 20 AD, so `cash_2020-07-13.csv.zip`
loaded as `0020-07-13` — silently, in both readers, with no error anywhere. A crash would
have been kinder."""


def trade_date_from_archive_name(path: Path) -> date | None:
    """The session date the FILE claims, read from `cash_YYYY-MM-DD.csv.zip`.

    The archive's own naming is evidence independent of the file's contents, which is what
    makes it usable as a check on them.
    """
    stem = path.name.split(".")[0]
    _, _, tail = stem.partition("_")
    try:
        return date.fromisoformat(tail)
    except ValueError:
        return None


class RowQuarantineReason(Enum):
    """Why a row did not become history."""

    UNPARSEABLE_NUMBER = "unparseable_number"
    UNPARSEABLE_DATE = "unparseable_date"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    IMPOSSIBLE_BAR = "impossible_bar"
    NEGATIVE_QUANTITY = "negative_quantity"
    DATE_DISAGREES_WITH_FILE = "date_disagrees_with_file"


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """A row that could not be normalised, kept with its evidence."""

    source_path: Path
    reason: RowQuarantineReason
    detail: str
    raw_row: str


@dataclass(frozen=True, slots=True)
class NormalisedBhavcopyRow:
    """One instrument's day, in one shape, whatever era it came from."""

    trade_date: date
    market: str
    trading_symbol: str
    variant: BhavcopyVariant
    open_paise: int | None
    high_paise: int | None
    low_paise: int | None
    close_paise: int
    traded_quantity: int
    traded_value_paise: int
    series: str | None = None
    instrument_type: str | None = None
    expiry: date | None = None
    strike_paise: int | None = None
    option_type: str | None = None
    previous_close_paise: int | None = None
    trade_count: int | None = None
    open_interest: int | None = None
    isin: str | None = None

    @property
    def is_untraded(self) -> bool:
        """No trade occurred; the close is the exchange's mark, not a print.

        **93% of F&O rows are this.** Measured on 2011-10-18: 31,657 of 34,008 rows carry
        `OPEN=HIGH=LOW=0` with `CONTRACTS=0`, while `CLOSE` and `SETTLE_PR` still hold the
        exchange's valuation and `OPEN_INT` the outstanding position. Quarantining them as
        impossible bars — which the first version did — would have discarded almost the
        entire derivative archive, and with it every option's settlement history.
        """
        return self.traded_quantity == 0 and self.open_paise is None

    @property
    def is_derivative(self) -> bool:
        return self.market == "fo"

    @property
    def is_option(self) -> bool:
        return self.option_type is not None


class DeepHistoryBhavcopyReader:
    """Reads one archived zip into normalised rows, collecting what it had to refuse."""

    def __init__(self, resolver: BhavcopyVariantResolver | None = None) -> None:
        self._resolver = resolver or BhavcopyVariantResolver()
        self._quarantined: list[QuarantinedRow] = []

    @property
    def quarantined(self) -> tuple[QuarantinedRow, ...]:
        return tuple(self._quarantined)

    def read_file(
        self, path: Path, *, expected_trade_date: date | None = None
    ) -> tuple[NormalisedBhavcopyRow, ...]:
        """Every usable row in one archive file.

        `expected_trade_date` defaults to the date in the file's own name, and every row is
        checked against it. That invariant is what catches a date this reader parsed but
        parsed WRONG — the failure mode a format list can never close, because the wrong
        answer is a valid date.
        """
        expected = expected_trade_date or trade_date_from_archive_name(path)
        with zipfile.ZipFile(path) as archive:
            # The member name is resolved, never constructed: the convention changed from
            # `cm02JAN1995bhav.csv` to `BhavCopy_NSE_CM_0_0_0_20260810_F_0000.csv`, and it
            # was never the zip's own name.
            member = archive.namelist()[0]
            text = archive.read(member).decode("utf-8", "replace")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if header is None:
            return ()
        variant = self._resolver.resolve(header)
        columns = [column.strip() for column in header]
        rows = []
        for raw in reader:
            if not any(field.strip() for field in raw):
                continue  # a trailing blank line is not a row
            normalised = self._normalise(path, variant, columns, raw)
            if normalised is None:
                continue
            if expected is not None and normalised.trade_date != expected:
                self._quarantined.append(
                    QuarantinedRow(
                        source_path=path,
                        reason=RowQuarantineReason.DATE_DISAGREES_WITH_FILE,
                        detail=(
                            f"row says {normalised.trade_date.isoformat()}, "
                            f"file says {expected.isoformat()}"
                        ),
                        raw_row=",".join(raw),
                    )
                )
                continue
            rows.append(normalised)
        return tuple(rows)

    # -- normalisation -------------------------------------------------------------------

    def _normalise(
        self,
        path: Path,
        variant: BhavcopyVariant,
        columns: Sequence[str],
        raw: Sequence[str],
    ) -> NormalisedBhavcopyRow | None:
        fields = dict(zip(columns, raw, strict=False))
        try:
            row = (
                self._udiff_row(variant, fields)
                if variant is BhavcopyVariant.UDIFF
                else self._legacy_row(variant, columns, fields)
            )
            _reject_impossible_bar(row)
        except _RowRejectedError as rejection:
            self._quarantined.append(
                QuarantinedRow(
                    source_path=path,
                    reason=rejection.reason,
                    detail=rejection.detail,
                    raw_row=",".join(raw),
                )
            )
            return None
        return row

    def _legacy_row(
        self,
        variant: BhavcopyVariant,
        columns: Sequence[str],
        fields: Mapping[str, str],
    ) -> NormalisedBhavcopyRow:
        if variant is BhavcopyVariant.FO_LEGACY:
            return self._legacy_derivative_row(columns, fields)
        cash_quantity = _quantity(fields, "TOTTRDQTY")
        return NormalisedBhavcopyRow(
            trade_date=_legacy_date(fields.get("TIMESTAMP")),
            market="cash",
            trading_symbol=_required_text(fields, "SYMBOL"),
            variant=variant,
            series=_optional_text(fields, "SERIES"),
            open_paise=_traded_price_paise(fields, "OPEN", traded_quantity=cash_quantity),
            high_paise=_traded_price_paise(fields, "HIGH", traded_quantity=cash_quantity),
            low_paise=_traded_price_paise(fields, "LOW", traded_quantity=cash_quantity),
            close_paise=_paise(fields, "CLOSE"),
            previous_close_paise=_optional_paise(fields, "PREVCLOSE"),
            traded_quantity=cash_quantity,
            traded_value_paise=_paise(fields, "TOTTRDVAL"),
            trade_count=_optional_quantity(fields, "TOTALTRADES"),
            isin=_optional_text(fields, "ISIN"),
        )

    def _legacy_derivative_row(
        self, columns: Sequence[str], fields: Mapping[str, str]
    ) -> NormalisedBhavcopyRow:
        strike_raw = (fields.get("STRIKE_PR") or "").strip()
        option_raw = (fields.get(option_type_column(columns)) or "").strip()
        contracts = _quantity(fields, "CONTRACTS")
        is_future = (
            strike_raw in {FUTURES_STRIKE_SENTINEL, "0.00", ""}
            or option_raw.upper() == FUTURES_OPTION_TYPE_SENTINEL
        )
        return NormalisedBhavcopyRow(
            trade_date=_legacy_date(fields.get("TIMESTAMP")),
            market="fo",
            trading_symbol=_required_text(fields, "SYMBOL"),
            variant=BhavcopyVariant.FO_LEGACY,
            instrument_type=_optional_text(fields, "INSTRUMENT"),
            expiry=_legacy_date(fields.get("EXPIRY_DT")) if fields.get("EXPIRY_DT") else None,
            strike_paise=None if is_future else _paise(fields, "STRIKE_PR"),
            option_type=None if is_future else option_raw.upper(),
            open_paise=_traded_price_paise(fields, "OPEN", traded_quantity=contracts),
            high_paise=_traded_price_paise(fields, "HIGH", traded_quantity=contracts),
            low_paise=_traded_price_paise(fields, "LOW", traded_quantity=contracts),
            close_paise=_paise(fields, "CLOSE"),
            traded_quantity=contracts,
            # The lakh conversion, applied exactly once, here.
            traded_value_paise=_paise(fields, "VAL_INLAKH", scale=RUPEES_PER_LAKH),
            open_interest=_optional_quantity(fields, "OPEN_INT"),
        )

    def _udiff_row(
        self, variant: BhavcopyVariant, fields: Mapping[str, str]
    ) -> NormalisedBhavcopyRow:
        segment = (fields.get("Sgmt") or "").strip().upper()
        market = "fo" if segment == "FO" else "cash"
        option_raw = (fields.get("OptnTp") or "").strip().upper()
        strike_raw = (fields.get("StrkPric") or "").strip()
        volume = _quantity(fields, "TtlTradgVol")
        return NormalisedBhavcopyRow(
            trade_date=_iso_date(fields.get("TradDt")),
            market=market,
            trading_symbol=_required_text(fields, "TckrSymb"),
            variant=variant,
            # `FinInstrmTp` is `STK` for cash but `STF/STO/IDF/IDO` for F&O, so it is only
            # carried as an instrument type where it means one.
            series=_optional_text(fields, "SctySrs") if market == "cash" else None,
            instrument_type=_optional_text(fields, "FinInstrmTp") if market == "fo" else None,
            expiry=_iso_date(fields["XpryDt"]) if (fields.get("XpryDt") or "").strip() else None,
            strike_paise=_paise(fields, "StrkPric") if strike_raw else None,
            option_type=option_raw or None,
            open_paise=_traded_price_paise(fields, "OpnPric", traded_quantity=volume),
            high_paise=_traded_price_paise(fields, "HghPric", traded_quantity=volume),
            low_paise=_traded_price_paise(fields, "LwPric", traded_quantity=volume),
            close_paise=_paise(fields, "ClsPric"),
            previous_close_paise=_optional_paise(fields, "PrvsClsgPric"),
            traded_quantity=volume,
            traded_value_paise=_paise(fields, "TtlTrfVal"),
            trade_count=_optional_quantity(fields, "TtlNbOfTxsExctd"),
            open_interest=_optional_quantity(fields, "OpnIntrst"),
            isin=_optional_text(fields, "ISIN"),
        )


def _reject_impossible_bar(row: NormalisedBhavcopyRow) -> None:
    """A bar whose own four prices contradict each other is not a price.

    Checked rather than trusted because 193 million rows across three decades will contain
    transcription faults, and a high below its low silently poisons every range, volatility
    and gap statistic computed from it. Quarantined with the row attached so the archive can
    be audited rather than believed.

    An UNTRADED row is not a contradiction and is not checked here: it has no open, high or
    low because nothing traded, and its close is the exchange's mark.
    """
    if row.open_paise is None or row.high_paise is None or row.low_paise is None:
        return
    if row.previous_close_paise is not None and row.open_paise == row.previous_close_paise:
        # **The 1990s files put the PREVIOUS CLOSE in `OPEN` when there was no opening
        # trade**, which then sits outside that day's [low, high]. Measured: 30,851 cash
        # rows in 1995-96 trip the range test and 30,799 of them (99.8%) are exactly this
        # shape — 552 of 965 rows on 1996-05-20 alone. Discarding the row threw away a
        # perfectly good high, low, close, quantity and turnover to punish one field.
        return
    if row.high_paise < row.low_paise:
        raise _RowRejectedError(
            RowQuarantineReason.IMPOSSIBLE_BAR,
            f"high {row.high_paise} is below low {row.low_paise}",
        )
    if not (row.low_paise <= row.open_paise <= row.high_paise):
        raise _RowRejectedError(
            RowQuarantineReason.IMPOSSIBLE_BAR,
            f"open {row.open_paise} lies outside [{row.low_paise}, {row.high_paise}]",
        )
    if not (row.low_paise <= row.close_paise <= row.high_paise):
        raise _RowRejectedError(
            RowQuarantineReason.IMPOSSIBLE_BAR,
            f"close {row.close_paise} lies outside [{row.low_paise}, {row.high_paise}]",
        )


class _RowRejectedError(Exception):
    """Internal: this row cannot become history, and here is why."""

    def __init__(self, reason: RowQuarantineReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _required_text(fields: Mapping[str, str], column: str) -> str:
    value = (fields.get(column) or "").strip()
    if not value:
        raise _RowRejectedError(
            RowQuarantineReason.MISSING_REQUIRED_FIELD, f"{column} is empty or absent"
        )
    return value


def _optional_text(fields: Mapping[str, str], column: str) -> str | None:
    value = (fields.get(column) or "").strip()
    return value or None


def _traded_price_paise(
    fields: Mapping[str, str], column: str, *, traded_quantity: int
) -> int | None:
    """An OHLC price, or `None` when the instrument did not trade that day.

    A zero open on a day with zero volume is the exchange saying "no trades", not a price
    of zero rupees. Keeping it as 0 would put a zero into every minimum, range and return
    computed across 33 years.
    """
    raw = (fields.get(column) or "").strip()
    if not raw:
        return None
    value = _paise(fields, column)
    return None if (value == 0 and traded_quantity == 0) else value


def _paise(fields: Mapping[str, str], column: str, *, scale: int = 1) -> int:
    """A rupee amount as integer paise, with an optional unit scale applied first."""
    raw = (fields.get(column) or "").strip()
    if not raw:
        raise _RowRejectedError(
            RowQuarantineReason.MISSING_REQUIRED_FIELD, f"{column} is empty or absent"
        )
    try:
        rupees = Decimal(raw) * scale
        if not rupees.is_finite():
            raise _RowRejectedError(
                RowQuarantineReason.UNPARSEABLE_NUMBER, f"{column}={raw!r} is not finite"
            )
    except (InvalidOperation, ValueError) as failure:
        raise _RowRejectedError(
            RowQuarantineReason.UNPARSEABLE_NUMBER, f"{column}={raw!r}: {failure}"
        ) from failure
    return int(rupees * PAISE_PER_RUPEE)


def _optional_paise(fields: Mapping[str, str], column: str) -> int | None:
    return _paise(fields, column) if (fields.get(column) or "").strip() else None


def _quantity(fields: Mapping[str, str], column: str) -> int:
    raw = (fields.get(column) or "").strip()
    if not raw:
        raise _RowRejectedError(
            RowQuarantineReason.MISSING_REQUIRED_FIELD, f"{column} is empty or absent"
        )
    try:
        decimal_quantity = Decimal(raw)
        if not decimal_quantity.is_finite():
            raise _RowRejectedError(
                RowQuarantineReason.UNPARSEABLE_NUMBER, f"{column}={raw!r} is not finite"
            )
        quantity = int(decimal_quantity)
    except (InvalidOperation, ValueError) as failure:
        raise _RowRejectedError(
            RowQuarantineReason.UNPARSEABLE_NUMBER, f"{column}={raw!r}: {failure}"
        ) from failure
    if quantity < 0:
        raise _RowRejectedError(RowQuarantineReason.NEGATIVE_QUANTITY, f"{column}={raw!r}")
    return quantity


def _optional_quantity(fields: Mapping[str, str], column: str) -> int | None:
    return _quantity(fields, column) if (fields.get(column) or "").strip() else None


def _legacy_date(raw: str | None) -> date:
    """`2-JAN-1995`, `01-JAN-2015`, `27-Jan-2005` — one parser, three real spellings."""
    text = (raw or "").strip()
    if not text:
        raise _RowRejectedError(RowQuarantineReason.UNPARSEABLE_DATE, "date field is empty")
    for layout in _LEGACY_DATE_FORMATS:
        try:
            return datetime.strptime(text, layout).date()  # noqa: DTZ007 - a calendar date
        except ValueError:
            continue
    raise _RowRejectedError(RowQuarantineReason.UNPARSEABLE_DATE, f"unrecognised date {text!r}")


def _iso_date(raw: str | None) -> date:
    text = (raw or "").strip()
    if not text:
        raise _RowRejectedError(RowQuarantineReason.UNPARSEABLE_DATE, "date field is empty")
    try:
        return date.fromisoformat(text)
    except ValueError as failure:
        raise _RowRejectedError(
            RowQuarantineReason.UNPARSEABLE_DATE, f"unrecognised ISO date {text!r}"
        ) from failure
