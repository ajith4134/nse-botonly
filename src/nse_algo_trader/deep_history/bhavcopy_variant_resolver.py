"""Which of the archive's five shapes a file is, decided by its HEADER and never its date.

Thirty-three years of NSE bhavcopy is not one format. An exhaustive scan of all 14,314
archived files (`docs/research/218` §1) found five, and only one of the four transitions was
known to this project before that scan:

- `CASH_LEGACY_V1` — 1994-11-03 to 2011-06-21, 4,125 files
- `CASH_LEGACY_V2` — adds `TOTALTRADES` and `ISIN`, 2011-06-22 to 2024-07-05, 3,215 files
- `FO_LEGACY` — 2000-06-12 to 2024-07-05, 5,936 files across two column-name spellings
- `UDIFF` — both markets, 2024-07-08 onward, 517 files each

**Resolution is by header, and that is not a stylistic choice.** Between 2003-05-14 and
2008-02-05 the F&O files alternate DAY BY DAY between naming the column `OPTION_TYP` and
`OPTIONTYPE` — 871 files interleaved among their neighbours. A loader that switches on a
cutoff date is wrong on roughly half of them, and wrong in the worst way: the column simply
appears to be missing. Reading what the file says removes the whole class of error.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum


class UnknownBhavcopyVariantError(RuntimeError):
    """A header this loader has never seen.

    Raised rather than guessed at. NSE has changed this format four times in thirty-three
    years and will change it again; a loader that falls back to "closest match" would keep
    running and produce an archive whose errors are invisible until something downstream
    disagrees with the exchange.
    """


class BhavcopyVariant(Enum):
    """The five shapes, named for what they are rather than when they occurred."""

    CASH_LEGACY_V1 = "cash_legacy_v1"
    CASH_LEGACY_V2 = "cash_legacy_v2"
    FO_LEGACY = "fo_legacy"
    UDIFF = "udiff"

    @property
    def is_cash(self) -> bool:
        return self in {BhavcopyVariant.CASH_LEGACY_V1, BhavcopyVariant.CASH_LEGACY_V2}

    @property
    def is_legacy(self) -> bool:
        return self is not BhavcopyVariant.UDIFF


OPTION_TYPE_COLUMN_ALIASES = ("OPTION_TYP", "OPTIONTYPE")
"""Both spellings NSE used for one column, interleaved daily for five years."""

_CASH_V1_REQUIRED = frozenset(
    {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP"}
)
_CASH_V2_ADDITIONS = frozenset({"TOTALTRADES", "ISIN"})
_FO_LEGACY_REQUIRED = frozenset(
    {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPEN_INT", "VAL_INLAKH", "TIMESTAMP"}
)
_UDIFF_REQUIRED = frozenset({"TradDt", "TckrSymb", "ClsPric", "TtlTradgVol", "FinInstrmTp"})


class BhavcopyVariantResolver:
    """Header in, variant out."""

    def resolve(self, header: Sequence[str]) -> BhavcopyVariant:
        """The variant this header belongs to, or a refusal naming what it did not know.

        Membership is tested against a REQUIRED SUBSET rather than the full column list,
        because six real files omit the header's trailing comma and a handful carry an
        extra empty field. An exact-match rule rejects six genuine trading days.
        """
        columns = {column.strip() for column in header if column.strip()}
        if columns >= _UDIFF_REQUIRED:
            return BhavcopyVariant.UDIFF
        if columns >= _FO_LEGACY_REQUIRED and self._has_option_type_column(columns):
            return BhavcopyVariant.FO_LEGACY
        if columns >= _CASH_V1_REQUIRED:
            return (
                BhavcopyVariant.CASH_LEGACY_V2
                if columns >= _CASH_V2_ADDITIONS
                else BhavcopyVariant.CASH_LEGACY_V1
            )
        raise UnknownBhavcopyVariantError(
            f"no known bhavcopy variant matches this header: {', '.join(header)}. "
            f"NSE has changed this format four times in thirty-three years; a fifth change "
            f"stops the loader here rather than being absorbed into the closest match"
        )

    @staticmethod
    def _has_option_type_column(columns: frozenset[str] | set[str]) -> bool:
        return any(alias in columns for alias in OPTION_TYPE_COLUMN_ALIASES)


def option_type_column(header: Sequence[str]) -> str:
    """Whichever spelling THIS file used. Both are the same column."""
    present = {column.strip() for column in header}
    for alias in OPTION_TYPE_COLUMN_ALIASES:
        if alias in present:
            return alias
    raise UnknownBhavcopyVariantError(
        f"no option-type column under any known spelling "
        f"({', '.join(OPTION_TYPE_COLUMN_ALIASES)}) in header: {', '.join(header)}"
    )
