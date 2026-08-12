"""Tests for `L0.34`, written before the loader (`R.23(c)`).

Every fixture below is a REAL header from the archive, quoted exactly as the exhaustive
survey found it (`docs/research/218` §1). A loader tested against invented headers proves
only that it parses inventions.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.deep_history.bhavcopy_variant_resolver import (
    BhavcopyVariant,
    BhavcopyVariantResolver,
    UnknownBhavcopyVariantError,
)
from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import (
    DeepHistoryBhavcopyReader,
    NormalisedBhavcopyRow,
    RowQuarantineReason,
)

CASH_V1_HEADER = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
)
CASH_V2_HEADER = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
    "TOTALTRADES,ISIN,"
)
FO_LEGACY_HEADER = (
    "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
    "CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,"
)
FO_LEGACY_SYNONYM_HEADER = FO_LEGACY_HEADER.replace("OPTION_TYP", "OPTIONTYPE")
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _first_per_identity(
    rows: Sequence[NormalisedBhavcopyRow],
) -> list[NormalisedBhavcopyRow]:
    """The reference reader keeps every row; the loader keeps one per identity per file.

    Some 2002-2007 F&O files are several dumps concatenated, so the same contract appears
    up to six times. De-duplication belongs in the loader (it is a storage decision), and
    the comparison has to account for it rather than call it a disagreement.
    """
    seen: dict[tuple[object, ...], NormalisedBhavcopyRow] = {}
    for row in rows:
        key = (
            row.trading_symbol,
            row.series,
            row.instrument_type,
            row.expiry,
            row.strike_paise,
            row.option_type,
        )
        seen.setdefault(key, row)
    return list(seen.values())


def _zip_with(tmp_path: Path, name: str, member: str, text: str) -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(member, text)
    return archive


# -- variant resolution ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (CASH_V1_HEADER, BhavcopyVariant.CASH_LEGACY_V1),
        (CASH_V2_HEADER, BhavcopyVariant.CASH_LEGACY_V2),
        (FO_LEGACY_HEADER, BhavcopyVariant.FO_LEGACY),
        (FO_LEGACY_SYNONYM_HEADER, BhavcopyVariant.FO_LEGACY),
        (UDIFF_HEADER, BhavcopyVariant.UDIFF),
    ],
)
def test_every_real_header_resolves_to_its_variant(
    header: str, expected: BhavcopyVariant
) -> None:
    assert BhavcopyVariantResolver().resolve(header.split(",")) is expected


def test_an_unknown_header_stops_the_loader_rather_than_being_guessed_at() -> None:
    """A new NSE format must be a refusal. Guessing produces a plausible wrong archive."""
    with pytest.raises(UnknownBhavcopyVariantError, match="SOMETHING_NEW"):
        BhavcopyVariantResolver().resolve(["SYMBOL", "SOMETHING_NEW", "CLOSE"])


def test_the_option_type_synonym_is_aliased_not_date_branched() -> None:
    """Measured: `OPTION_TYP` and `OPTIONTYPE` INTERLEAVE day by day, 2003-05-14 to
    2008-02-05, across 871 files. Any loader that switches on a cutoff date is wrong on
    roughly half of them, and wrong in a way that reads as a missing column."""
    resolver = BhavcopyVariantResolver()
    assert resolver.resolve(FO_LEGACY_HEADER.split(",")) is BhavcopyVariant.FO_LEGACY
    assert (
        resolver.resolve(FO_LEGACY_SYNONYM_HEADER.split(",")) is BhavcopyVariant.FO_LEGACY
    )


def test_a_header_missing_its_trailing_comma_still_resolves() -> None:
    """Six real trading days do this: cash 2017-07-10, 2020-07-13; F&O 2012-05-14,
    2019-08-28, 2021-05-12, 2023-02-13. A strict field count rejects six real sessions."""
    assert (
        BhavcopyVariantResolver().resolve(CASH_V2_HEADER.rstrip(",").split(","))
        is BhavcopyVariant.CASH_LEGACY_V2
    )


# -- reading, per variant --------------------------------------------------------------


def test_a_1995_cash_row_reads_with_its_unpadded_uppercase_date(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_1995-01-02.csv.zip",
        "cm02JAN1995bhav.csv",
        CASH_V1_HEADER + "\nRELIANCE,EQ,200.00,210.50,199.00,208.75,208.00,198.50,"
        "1000,208750.00,2-JAN-1995,\n",
    )
    rows = DeepHistoryBhavcopyReader().read_file(archive)
    assert len(rows) == 1
    row = rows[0]
    assert row.trade_date == date(1995, 1, 2)
    assert row.trading_symbol == "RELIANCE"
    assert row.series == "EQ"
    assert row.close_paise == 20_875
    assert row.traded_value_paise == 20_875_000
    assert row.trade_count is None  # C1 has no TOTALTRADES column
    assert row.isin is None


def test_a_2015_cash_row_carries_its_isin_and_trade_count(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_2015-01-01.csv.zip",
        "cm01JAN2015bhav.csv",
        CASH_V2_HEADER + "\nRELIANCE,EQ,880.00,890.00,875.00,885.50,885.00,879.00,"
        "5000,4427500.00,01-JAN-2015,1234,INE002A01018,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.trade_count == 1234
    assert row.isin == "INE002A01018"
    assert row.trade_date == date(2015, 1, 1)


def test_a_legacy_futures_row_has_no_strike_and_no_option_type(tmp_path: Path) -> None:
    """Hazard 3: futures carry SENTINELS. Read as data they become zero-strike options."""
    archive = _zip_with(
        tmp_path,
        "fo_2005-01-03.csv.zip",
        "fo03JAN2005bhav.csv",
        FO_LEGACY_HEADER + "\nFUTSTK,RELIANCE,27-Jan-2005,0,XX,520.00,530.00,515.00,"
        "525.00,525.50,1200,6300.00,45000,1200,3-JAN-2005,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.instrument_type == "FUTSTK"
    assert row.strike_paise is None
    assert row.option_type is None
    assert row.expiry == date(2005, 1, 27)  # title-case month, same file as the upper one
    assert row.trade_date == date(2005, 1, 3)


def test_a_legacy_option_row_keeps_its_strike_and_type(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "fo_2005-01-03.csv.zip",
        "fo03JAN2005bhav.csv",
        FO_LEGACY_HEADER + "\nOPTSTK,RELIANCE,27-Jan-2005,520,CE,10.00,12.00,9.50,"
        "11.00,11.00,300,3.30,15000,300,3-JAN-2005,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.strike_paise == 52_000
    assert row.option_type == "CE"


def test_traded_value_in_lakhs_is_converted_at_the_boundary(tmp_path: Path) -> None:
    """Hazard 2, and the most dangerous one: legacy F&O `VAL_INLAKH` is in LAKHS of rupees
    while UDiFF's `TtlTrfVal` is in rupees. Mapping both to "traded value" is wrong by
    100,000x, and every value-ranked universe built on it is plausibly wrong."""
    archive = _zip_with(
        tmp_path,
        "fo_2005-01-03.csv.zip",
        "fo03JAN2005bhav.csv",
        FO_LEGACY_HEADER + "\nFUTSTK,RELIANCE,27-Jan-2005,0,XX,520.00,530.00,515.00,"
        "525.00,525.50,1200,6300.00,45000,1200,3-JAN-2005,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    # 6,300 lakhs = Rs 630,000,000 = 63,000,000,000 paise.
    assert row.traded_value_paise == 63_000_000_000


def test_a_udiff_cash_row_reads_with_iso_dates(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_2026-08-10.csv.zip",
        "BhavCopy_NSE_CM_0_0_0_20260810_F_0000.csv",
        UDIFF_HEADER + "\n2026-08-10,2026-08-10,CM,NSE,STK,1234,INE002A01018,RELIANCE,EQ,"
        ",,,,RELIANCE LIMITED,1320.00,1330.00,1310.00,1325.00,1325.00,1318.00,,1325.00,"
        ",,347858,461000000.00,52000,F1,1,,,,,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.trade_date == date(2026, 8, 10)
    assert row.trading_symbol == "RELIANCE"
    assert row.close_paise == 132_500
    assert row.traded_value_paise == 46_100_000_000
    assert row.strike_paise is None
    assert row.market == "cash"


def test_a_udiff_option_row_is_keyed_by_its_own_instrument_type(tmp_path: Path) -> None:
    """Hazard 8: `FinInstrmTp` is `STK` for cash but `STF/STO/IDF/IDO` for F&O. Reusing
    the cash mapping mislabels every derivative in the modern era."""
    archive = _zip_with(
        tmp_path,
        "fo_2026-08-10.csv.zip",
        "BhavCopy_NSE_FO_0_0_0_20260810_F_0000.csv",
        UDIFF_HEADER + "\n2026-08-10,2026-08-10,FO,NSE,STO,5678,,RELIANCE,,2026-08-27,"
        "2026-08-27,1300.00,CE,RELIANCE 27AUG2026 CE 1300,30.00,35.00,28.00,32.00,32.00,"
        "31.00,1325.00,32.00,9000,500,12000,3840000.00,900,F1,500,,,,,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.market == "fo"
    assert row.instrument_type == "STO"
    assert row.option_type == "CE"
    assert row.strike_paise == 130_000
    assert row.expiry == date(2026, 8, 27)
    assert row.open_interest == 9000


def test_the_zip_member_name_is_resolved_never_constructed(tmp_path: Path) -> None:
    """Hazard 5: the member naming convention changed, and it is not the zip's own name."""
    archive = _zip_with(
        tmp_path,
        "cash_2015-01-01.csv.zip",
        "an_unexpected_member_name.csv",
        CASH_V2_HEADER + "\nTCS,EQ,2500.00,2510.00,2490.00,2505.00,2505.00,2495.00,"
        "100,250500.00,01-JAN-2015,10,INE467B01029,\n",
    )
    assert DeepHistoryBhavcopyReader().read_file(archive)[0].trading_symbol == "TCS"


# -- quarantine rather than silent skipping ---------------------------------------------


def test_a_malformed_row_is_quarantined_with_a_reason_not_dropped(tmp_path: Path) -> None:
    """193 million rows will contain surprises. A silent skip is indistinguishable from a
    clean load, which is the failure this project refuses everywhere else."""
    archive = _zip_with(
        tmp_path,
        "cash_2015-01-01.csv.zip",
        "cm01JAN2015bhav.csv",
        CASH_V2_HEADER
        + "\nGOOD,EQ,10.00,11.00,9.00,10.50,10.50,9.90,100,1050.00,01-JAN-2015,5,INE1,"
        + "\nBAD,EQ,notanumber,11.00,9.00,10.50,10.50,9.90,100,1050.00,01-JAN-2015,5,INE2,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    rows = reader.read_file(archive)
    assert [row.trading_symbol for row in rows] == ["GOOD"]
    assert reader.quarantined
    assert reader.quarantined[0].reason is RowQuarantineReason.UNPARSEABLE_NUMBER
    assert "BAD" in reader.quarantined[0].raw_row


def test_a_row_whose_high_is_below_its_low_is_quarantined(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_2015-01-01.csv.zip",
        "cm01JAN2015bhav.csv",
        CASH_V2_HEADER
        + "\nBROKEN,EQ,10.00,9.00,11.00,10.50,10.50,9.90,100,1050.00,01-JAN-2015,5,INE1,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    assert reader.read_file(archive) == ()
    assert reader.quarantined[0].reason is RowQuarantineReason.IMPOSSIBLE_BAR


def test_an_unknown_series_code_is_kept_because_a_whitelist_loses_history(
    tmp_path: Path,
) -> None:
    """Hazard 7: 40+ distinct series codes across the decades. A whitelist silently drops
    whole classes of instrument, and the drop looks like an absence in the data."""
    archive = _zip_with(
        tmp_path,
        "cash_2005-01-03.csv.zip",
        "cm03JAN2005bhav.csv",
        CASH_V1_HEADER + "\nSOMEBOND,N7,100.00,101.00,99.00,100.50,100.50,99.50,"
        "10,1005.00,3-JAN-2005,\n",
    )
    row = DeepHistoryBhavcopyReader().read_file(archive)[0]
    assert row.series == "N7"


def test_an_untraded_contract_keeps_its_settlement_mark_and_open_interest(
    tmp_path: Path,
) -> None:
    """93% of F&O rows are this, and the first version quarantined every one of them.

    Measured on the real 2011-10-18 file: 31,657 of 34,008 rows carry OPEN=HIGH=LOW=0 with
    CONTRACTS=0, while CLOSE holds the exchange's mark and OPEN_INT the outstanding
    position. Treated as an impossible bar they vanish, and with them every option's
    settlement history.
    """
    archive = _zip_with(
        tmp_path,
        "fo_2011-10-18.csv.zip",
        "fo18OCT2011bhav.csv",
        FO_LEGACY_HEADER + "\nFUTIDX,CNXIT,29-Dec-2011,0,XX,0,0,0,5884.05,5990.85,"
        "0,0,250,0,18-OCT-2011,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    rows = reader.read_file(archive)
    assert reader.quarantined == ()
    row = rows[0]
    assert row.is_untraded
    assert row.open_paise is None and row.high_paise is None and row.low_paise is None
    assert row.close_paise == 588_405  # the mark survives
    assert row.open_interest == 250
    assert row.traded_quantity == 0


def test_an_untraded_cash_row_is_kept_the_same_way(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_2005-01-03.csv.zip",
        "cm03JAN2005bhav.csv",
        CASH_V1_HEADER + "\nQUIETCO,BE,0,0,0,105.00,105.00,105.00,0,0,3-JAN-2005,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    row = reader.read_file(archive)[0]
    assert reader.quarantined == ()
    assert row.is_untraded
    assert row.close_paise == 10_500


def test_a_traded_row_with_a_zero_high_is_still_impossible(tmp_path: Path) -> None:
    """The untraded rule must not become a blanket amnesty: a row that reports trades AND
    a zero high is contradicting itself, and is quarantined as before."""
    archive = _zip_with(
        tmp_path,
        "cash_2005-01-03.csv.zip",
        "cm03JAN2005bhav.csv",
        CASH_V1_HEADER + "\nBROKEN,EQ,100.00,0,0,105.00,105.00,104.00,500,52500.00,"
        "3-JAN-2005,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    assert reader.read_file(archive) == ()
    assert reader.quarantined[0].reason is RowQuarantineReason.IMPOSSIBLE_BAR


# -- the vectorised path, checked against the reference reader ---------------------------


@pytest.mark.real_data
@pytest.mark.parametrize(
    "archive_name",
    [
        "cash/1995/cash_1995-01-02.csv.zip",
        # The two files whose two-digit years the vectorised path silently mis-parsed,
        # losing 33,389 real rows from a store that reported itself complete.
        "cash/2020/cash_2020-07-13.csv.zip",
        "fo/2012/fo_2012-05-14.csv.zip",
        "fo/2003/fo_2003-04-23.csv.zip",  # five concatenated dumps in one file
        "cash/2015/cash_2015-01-01.csv.zip",
        "cash/2026/cash_2026-08-10.csv.zip",
        "fo/2005/fo_2005-01-03.csv.zip",
        "fo/2011/fo_2011-10-18.csv.zip",
        "fo/2026/fo_2026-08-10.csv.zip",
    ],
)
def test_the_vectorised_loader_agrees_with_the_reference_reader(archive_name: str) -> None:
    """The differential oracle (`O.57`): the fast path is the one that runs, the row-wise
    reader is the one that is obviously right, and this keeps them honest on REAL files
    spanning every variant — 1995 cash, 2015 cash, UDiFF cash, legacy F&O with its option
    sentinels, an untraded-heavy 2011 F&O day, and UDiFF F&O.

    A single fast implementation would have nothing to be checked against, and the hazards
    it must respect (lakh conversion, futures sentinels, untraded rows, three date
    spellings) are exactly the kind that produce plausible wrong numbers.
    """
    from nse_algo_trader.deep_history.deep_history_archive_loader import (
        DeepHistoryArchiveLoader,
    )

    archive = Path("/home/opc/nse_archive") / archive_name
    if not archive.exists():
        pytest.skip(f"{archive} is not on this host")

    from nse_algo_trader.deep_history.deep_history_archive_loader import (
        _only_rows_dated,
        _without_impossible_bars,
        _without_repeated_instruments,
    )
    from nse_algo_trader.deep_history.deep_history_bhavcopy_reader import (
        trade_date_from_archive_name,
    )

    reference_rows = DeepHistoryBhavcopyReader().read_file(archive)
    reference = _first_per_identity(reference_rows)
    with DeepHistoryArchiveLoader(database_path=Path(":memory:")) as loader:
        frame, _ = loader._frame_for(archive)
    # **The WHOLE fast pipeline, not just its first step.** The original version applied
    # only `_without_impossible_bars`, so it compared the two readers upstream of
    # `_only_rows_dated` — exactly where they diverged. It passed while two entire trading
    # sessions were being dropped.
    frame, _dropped = _without_impossible_bars(frame)
    frame, _mismatched = _only_rows_dated(frame, trade_date_from_archive_name(archive))
    frame, _duplicated = _without_repeated_instruments(frame)

    assert frame.height == len(reference), "row counts differ between the two readers"

    # Matched by INSTRUMENT IDENTITY rather than by sort position: a first version paired
    # them by sorting both sides, and polars and Python order nulls differently, so it
    # compared unrelated rows and reported a disagreement that was its own.
    def identity(
        symbol: str,
        series: object,
        instrument_type: object,
        expiry: object,
        strike: object,
        option: object,
    ) -> tuple[str, ...]:
        # Series and instrument type are part of the identity, not decoration: one symbol
        # trades in several series at once (EQ and BE), and one underlying carries several
        # instrument types on the same expiry (FUTIDX and FUTIVX).
        return (
            symbol,
            str(series or ""),
            str(instrument_type or ""),
            str(expiry or ""),
            str(strike or ""),
            str(option or ""),
        )

    fast = {
        identity(
            row["trading_symbol"],
            row["series"],
            row["instrument_type"],
            row["expiry"],
            row["strike_paise"],
            row["option_type"],
        ): row
        for row in frame.to_dicts()
    }
    assert len(fast) == frame.height, "the identity key is not unique within a file"
    for slow_row in reference:
        key = identity(
            slow_row.trading_symbol,
            slow_row.series,
            slow_row.instrument_type,
            slow_row.expiry,
            slow_row.strike_paise,
            slow_row.option_type,
        )
        fast_row = fast[key]
        assert fast_row["trade_date"] == slow_row.trade_date
        assert fast_row["close_paise"] == slow_row.close_paise
        assert fast_row["open_paise"] == slow_row.open_paise
        assert fast_row["high_paise"] == slow_row.high_paise
        assert fast_row["low_paise"] == slow_row.low_paise
        assert fast_row["traded_quantity"] == slow_row.traded_quantity
        assert fast_row["traded_value_paise"] == slow_row.traded_value_paise
        assert fast_row["expiry"] == slow_row.expiry
        assert fast_row["open_interest"] == slow_row.open_interest


def test_a_two_digit_year_is_not_read_as_the_year_twenty(tmp_path: Path) -> None:
    """The worst kind of defect: a valid answer that is thirty centuries wrong.

    `cash_2020-07-13.csv.zip` really does write `13-Jul-20`. `%Y` parses that as the year
    20 AD, so 2,001 rows loaded as `0020-07-13` with no exception and no null — nothing in
    the pipeline could see it. The file's own name is an independent witness, and rows that
    disagree with it are quarantined rather than believed.
    """
    archive = _zip_with(
        tmp_path,
        "cash_2020-07-13.csv.zip",
        "cm13JUL2020bhav.csv",
        CASH_V2_HEADER + "\n20MICRONS,EQ,32.85,33.85,31.85,33.45,33.85,32.30,"
        "187303,6187285.70,13-Jul-20,1382,INE144J01027,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    rows = reader.read_file(archive)
    assert reader.quarantined == ()
    assert rows[0].trade_date == date(2020, 7, 13)


def test_a_row_dated_differently_from_its_file_is_quarantined(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path,
        "cash_2015-01-01.csv.zip",
        "cm01JAN2015bhav.csv",
        CASH_V2_HEADER + "\nWRONGDAY,EQ,10.00,11.00,9.00,10.50,10.50,9.90,100,1050.00,"
        "02-JAN-2015,5,INE1,\n",
    )
    reader = DeepHistoryBhavcopyReader()
    assert reader.read_file(archive) == ()
    assert reader.quarantined[0].reason is RowQuarantineReason.DATE_DISAGREES_WITH_FILE


def test_an_embedded_second_header_row_does_not_become_data(tmp_path: Path) -> None:
    """`fo_2002-02-14.csv.zip` carries a second header 1,853 lines in — a file can be two
    dumps concatenated. That row has no parseable date, and it is what stopped the first
    full-archive load by hitting the store's NOT NULL constraint."""
    archive = _zip_with(
        tmp_path,
        "fo_2002-02-14.csv.zip",
        "fo14FEB2002bhav.csv",
        FO_LEGACY_HEADER + "\nFUTIDX,NIFTY,28-Feb-2002,0,XX,1100.00,1110.00,1090.00,"
        "1105.00,1105.50,50,55.25,1000,50,14-FEB-2002,\n" + FO_LEGACY_HEADER + "\n",
    )
    reader = DeepHistoryBhavcopyReader()
    rows = reader.read_file(archive)
    assert [row.trading_symbol for row in rows] == ["NIFTY"]
    assert reader.quarantined  # the header row is recorded, not silently dropped
