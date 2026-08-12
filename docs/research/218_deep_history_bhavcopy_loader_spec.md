# `L0.34` — Deep history, specified as a SCHEMA-DRIFT-TOLERANT loader over 33 years

*Spec, 2026-08-12, written before the code (`R.23(c)`). The acquisition half of `L0.34` is already
done — 7,857 cash files (1994-11-03 →) and 6,457 F&O files (2000-06-12 →), 2.8 GB compressed, with a
provenance manifest recording 433 and 370 dates as legitimately absent. **Nothing loads them.** The
bitemporal bar store holds 78 bars; the only code that touches the archive reads the single newest file
to rank liquidity. Thirty-three years of history sits on disk unqueryable, and this is the engine that
changes that.*

## 1. What is actually in the archive, measured exhaustively

Every one of the 14,314 files was opened and its header read — not sampled — because a loader that works
on a sample and fails on 1997 is worse than no loader.

| quantity | value |
|---|---|
| files | 7,857 cash + 6,457 F&O |
| data rows | **193,752,000** (approx, exhaustive count) |
| uncompressed | **16.88 GB** (2.8 GB compressed on disk) |
| header variants | **5** |
| rows in one day, 1995 → 2026 | cash 203 → 3,564; F&O 3 → 50,741 |

**Five header variants, and only one of them was known before this survey.** The fetcher knows the UDiFF
cutover; the other three transitions were not documented anywhere in this project.

| variant | market | in force | files | shape |
|---|---|---|---|---|
| C1 | cash | 1994-11-03 → 2011-06-21 | 4,125 | `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP` |
| C2 | cash | 2011-06-22 → 2024-07-05 | 3,215 | C1 + `TOTALTRADES,ISIN` |
| F1 | F&O | 2000-06-12 → 2024-07-05 | 5,065 | `INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,…,OPEN_INT,CHG_IN_OI,TIMESTAMP` |
| F2 | F&O | interleaved 2003-05-14 → 2008-02-05 | 871 | F1 with `OPTIONTYPE` instead of `OPTION_TYP` |
| C3/F3 | both | 2024-07-08 → | 517 each | UDiFF: `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,…` |

## 2. The nine hazards, each of which silently corrupts a naive loader

These are the reason this is an engine and not a `pandas.read_csv` loop. Every one was observed in the
real archive.

1. **`OPTION_TYP` / `OPTIONTYPE` interleave DAY BY DAY** between 2003-05-14 and 2008-02-05 — 871 files
   scattered among F1's. A date-branching loader is wrong by construction here; the reader must ALIAS
   both names and never switch on a cutoff.
2. **`VAL_INLAKH` is in lakhs of rupees; UDiFF's `TtlTrfVal` is in rupees.** A loader that maps both to
   "traded value" is wrong by a factor of **100,000** and every value-ranked universe built on it is
   wrong in a way that looks plausible.
3. **Futures rows carry SENTINELS, not nulls**: `STRIKE_PR = 0` and `OPTION_TYP = "XX"` on
   `FUTIDX/FUTSTK/FUTIVX/FUTINT`. Read as data, they produce zero-strike options.
4. **Two date formats coexist inside one file**: legacy `TIMESTAMP` is `2-JAN-1995` (uppercase month,
   day-padding inconsistent before ~2015) while `EXPIRY_DT` in the SAME file is `27-Jan-2005`
   (title-case). UDiFF is ISO `2026-08-10`.
5. **The zip member name is not the zip name, and its convention changed**: `cm02JAN1995bhav.csv` and
   `fo03JAN2005bhav.csv` versus `BhavCopy_NSE_CM_0_0_0_20260810_F_0000.csv`. It must be resolved through
   `namelist()`, never constructed.
6. **Six files omit the header's trailing comma** (cash 2017-07-10, 2020-07-13; F&O 2012-05-14,
   2019-08-28, 2021-05-12, 2023-02-13). A strict field-count check rejects six real trading days.
7. **The series column holds 40+ distinct codes** across the decades — `EQ,BE,AE,E1,E2,N1–N9,W1–W4,DR,
   SM,BZ,GB,GS,ST,TB,MF,IV,SG,NA–NL,RR,NC,ND` and more. A whitelist silently discards history.
8. **`FinInstrmTp` means different things per market** in UDiFF: cash is always `STK`; F&O is
   `STF/STO/IDF/IDO`. Reusing the cash mapping mislabels every derivative.
9. **F&O row volume grew ~17,000x** (3 rows/day in 2000 to 50,741 in 2026), so nothing may preallocate
   or assume a stable per-file cost.

## 3. What this engine is, and what it deliberately is not

**It is a reader + normaliser + loader with a coverage ledger.** Its algorithm is schema resolution: for
each file, identify which of the five variants it is (by HEADER, never by date — hazard 1), map that
variant's columns onto one normalised row, convert units and sentinels, and emit typed records.

**It is not a new store, and not a new adjustment engine.** Four contracts already exist in this project
and this loader feeds them rather than duplicating them:

| existing engine | what the loader gives it |
|---|---|
| `BitemporalBarStore` (`BarRecord`) | one daily OHLCV bar per instrument per day, with `available_from` set to the exchange's own publication instant |
| `PointInTimeUniverseEngine` (`UniverseObservation`) | presence of a symbol in the cash or F&O file on a date — which is what makes a 33-year point-in-time universe possible |
| `SecurityIdentityRecordStore` | ISIN↔symbol observations from C2/C3, which is how a rename becomes visible across the decades |
| `CorporateActionAdjustmentEngine` | nothing — it is applied ON READ, so stored history stays raw |

**Raw is stored, adjusted is computed.** A split restated into stored rows cannot be un-restated when the
action table is later corrected, and NSE's action data is itself revised. The archive is the record of
what the exchange published; adjustment is an interpretation of it.

## 4. Signatures (`R.23(c)` step 2)

```python
class BhavcopyVariant(Enum):
    CASH_LEGACY_V1 = "cash_legacy_v1"
    CASH_LEGACY_V2 = "cash_legacy_v2"
    FO_LEGACY = "fo_legacy"
    UDIFF = "udiff"

@dataclass(frozen=True, slots=True)
class NormalisedBhavcopyRow:
    trade_date: date
    market: str                    # "cash" | "fo"
    trading_symbol: str
    series: str | None             # cash only
    instrument_type: str | None    # fo only, normalised across legacy/UDiFF
    expiry: date | None
    strike_paise: int | None       # None for futures, never 0 (hazard 3)
    option_type: str | None        # None for futures, never "XX"
    open_paise: int
    high_paise: int
    low_paise: int
    close_paise: int
    previous_close_paise: int | None
    traded_quantity: int
    traded_value_paise: int        # lakhs converted at the boundary (hazard 2)
    trade_count: int | None
    open_interest: int | None
    isin: str | None
    variant: BhavcopyVariant

class BhavcopyVariantResolver:
    def resolve(self, header: Sequence[str]) -> BhavcopyVariant: ...

class DeepHistoryBhavcopyReader:
    def read_file(self, path: Path) -> tuple[NormalisedBhavcopyRow, ...]: ...

class DeepHistoryLoader:
    def load(self, *, markets: Sequence[str], since: date | None) -> DeepHistoryLoadReport: ...
    def coverage(self) -> DeepHistoryCoverage: ...
```

Error behaviour: an unrecognised header raises `UnknownBhavcopyVariantError` naming the header — a new
NSE format must stop the loader, not be guessed at. A row that fails validation is COUNTED and quarantined
with its reason rather than dropped, because 193 million rows will contain surprises and a silent skip is
indistinguishable from a clean load. A file the manifest records as absent is not an error.

## 5. Acceptance criteria — checked, not claimed

1. All five variants resolve by header on the real archive, and the resolver rejects an invented header.
2. The 871 interleaved `OPTIONTYPE` files load identically to their `OPTION_TYP` neighbours.
3. `VAL_INLAKH` is converted: a 2015 row's traded value equals the UDiFF-era scale for the same rupee
   amount, asserted numerically.
4. Futures rows produce `strike_paise=None` and `option_type=None`, never 0 and never "XX".
5. The six trailing-comma files load without a field-count error.
6. Loads the FULL archive (both markets, 193M rows) with a recorded row count, quarantine count and
   wall-clock, and the coverage ledger reconciles against the acquisition manifest's stored dates.
7. A point-in-time universe query for an arbitrary 2005 date returns symbols observed in the 2005 file
   and nothing observed only later.
8. `/history` renders coverage from the store; ruff + mypy clean; adversarial review; R.05 on the real
   archive.

## 6. Out of scope, recorded so it is not mistaken for an omission

- **No intraday history.** `B.04` stays open: this is daily bhavcopy only, and deep intraday NSE history
  remains unfree. Narrowing `B.04`'s wording is a separate action.
- **No index history** — the bhavcopy carries constituents' prices, not index levels.
- **No corporate-action re-derivation.** The existing engine owns that; this feeds it.
