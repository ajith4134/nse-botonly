"""`L6.30` — what a broker would block for an F&O position. Spec `docs/research/264`.

**It is an ESTIMATOR and the name is load-bearing** (`R.23b`: vocabulary sets scope). SPAN is a
scenario grid over sixteen price and volatility shifts with inter-month and inter-commodity spread
credits, and its scanning ranges live in `nsccl.<YYYYMMDD>.s.spn`, a file NSE serves only from an
interactive page (`B36`, measured in `docs/research/264` §2a — the one working open-source
implementation, `marginism`, does not even try to download it). Calling this a calculator would
claim an accuracy it does not have.

**Two paths, and the authoritative one wins whenever it is available** (`R.04` — the algorithm is
whole either way, only its ACTIVATION differs):

1. **`marginism` + a real `.spn` file** — genuine SPAN and exposure margin from the exchange's own
   risk arrays. Needs one manual operator download. `SpanFileMarginSource` is the seam.
2. **This estimator** — runs today with no operator action, from two things NSE publishes daily:
   its own per-underlying volatility (`CMVOLT`, 5,026 rows) and the extreme-loss-margin framework.

**The arithmetic, and every term is either published or derived from a published one:**

```
initial_margin_fraction = normal_quantile(confidence) x sigma_daily x sqrt(horizon_sessions)
extreme_loss_fraction   = the sourced NSE table below, by product and moneyness
margin_fraction         = initial_margin_fraction + extreme_loss_fraction
```

`confidence` is 0.99 and `horizon_sessions` is 2 for futures because NSE states both, verbatim:
*"Initial margin requirements are based on 99% value at risk over a one day time horizon. However,
in the case of futures contracts ... where it may not be possible to collect mark to market
settlement value before the commencement of trading on the next day, the initial margin is computed
over a two-day time horizon."* The multiplier is `norm.ppf(0.99)` rather than a typed 2.326, so the
CONFIDENCE stays the input and the constant stays derived (`R.03`).

**Its error direction, stated because an estimator that will not say which way it is wrong is not
usable.** It **understates** where SPAN's scenario grid charges more than a normal tail — short
options far from the money, and expiry-day positions. It **overstates** for hedged books, because it
credits no spread offsets at all. For a capital bound that is the safe pairing: the naked short is
refused sooner by the same conservatism that ignores the hedge credit.
"""

from __future__ import annotations

import csv
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from scipy.stats import norm

NSE_MARGIN_FRAMEWORK_SOURCE = (
    "NSE, Equity Derivatives Margins — "
    "https://www.nseindia.com/products-services/equity-derivatives-margins "
    "(fetched 2026-08-18, HTTP 200, 130,178 bytes)"
)
"""Where every extreme-loss figure below comes from, quoted so a reader can re-check it."""

VALUE_AT_RISK_CONFIDENCE = 0.99
"""NSE: "Initial margin requirements are based on 99% value at risk". A published framework
parameter, sourced above — the exemption `R.23e` allows."""

FUTURES_VALUE_AT_RISK_HORIZON_SESSIONS = 2
"""NSE computes futures initial margin over a TWO-day horizon where mark-to-market cannot be
collected before the next open, which is the retail case. Sourced above."""

OPTION_VALUE_AT_RISK_HORIZON_SESSIONS = 1
"""The one-day horizon the same passage states as the base case."""

COIN_FLIP_CONFIDENCE = 0.5
"""A one-tailed value-at-risk level at or below this is not a risk measure — it is the median."""

DATE_PARTS_IN_A_CMVOLT_DATE = 3
"""`17-AUG-2026` is day, month, year. A property of the format, not a parameter."""


class FuturesMarginEstimationError(Exception):
    """The margin cannot be estimated, and guessing it would size a position on a fiction."""


class MarginProduct(StrEnum):
    """What is being margined. The extreme-loss rate keys off exactly this plus moneyness."""

    INDEX_FUTURE = "index_future"
    STOCK_FUTURE = "stock_future"
    INDEX_OPTION_SHORT = "index_option_short"
    STOCK_OPTION_SHORT = "stock_option_short"
    OPTION_LONG = "option_long"

    @property
    def is_future(self) -> bool:
        return self in (MarginProduct.INDEX_FUTURE, MarginProduct.STOCK_FUTURE)

    @property
    def is_index(self) -> bool:
        return self in (MarginProduct.INDEX_FUTURE, MarginProduct.INDEX_OPTION_SHORT)


INDEX_DERIVATIVE_EXTREME_LOSS_FRACTION = Decimal("0.02")
"""NSE: "Index Derivatives — 2% of the notional value"."""

STOCK_DERIVATIVE_EXTREME_LOSS_FRACTION = Decimal("0.035")
"""NSE: "Stock Derivatives — 3.5% of the notional value"."""

DEEP_OUT_OF_THE_MONEY_INDEX_EXTREME_LOSS_FRACTION = Decimal("0.03")
"""NSE: short INDEX options with strikes more than 10% out of the money — 3%."""

DEEP_OUT_OF_THE_MONEY_STOCK_EXTREME_LOSS_FRACTION = Decimal("0.0525")
"""NSE: short SINGLE-STOCK options with strikes more than 30% out of the money — 5.25%."""

LONG_DATED_INDEX_OPTION_EXTREME_LOSS_FRACTION = Decimal("0.05")
"""NSE: short index option contracts with residual maturity of more than 9 months — 5%."""

EXPIRY_DAY_ADDITIONAL_INDEX_EXTREME_LOSS_FRACTION = Decimal("0.02")
"""NSE: "On the day of options contracts expiry, an additional extreme loss margin of 2% shall be
levied on short index options contracts"."""

INDEX_DEEP_OUT_OF_THE_MONEY_THRESHOLD_FRACTION = Decimal("0.10")
STOCK_DEEP_OUT_OF_THE_MONEY_THRESHOLD_FRACTION = Decimal("0.30")
"""The moneyness boundaries NSE states for the deep-out-of-the-money rates: 10% for index, 30% for
single stock. Boundaries of the published rule, not tuned cut-offs."""

LONG_DATED_OPTION_SESSIONS = 9 * 21
"""Nine months of residual maturity, in trading sessions, at NSE's ~21 sessions a month.

The rule is stated in MONTHS and this engine reasons in sessions, so the conversion is written down
rather than left implicit. It only ever selects a higher extreme-loss rate, so an error here is
conservative.
"""


@dataclass(frozen=True, slots=True)
class UnderlyingVolatility:
    """One underlying's volatility as the EXCHANGE published it — never a fitted substitute.

    `CMVOLT` is the same estimate SPAN itself is built on, so consuming it keeps this engine and the
    authoritative one on the same input rather than on two rival measurements.
    """

    symbol: str
    session_date: date
    close_price_rupees: Decimal
    daily_volatility: Decimal
    annualised_volatility: Decimal

    def __post_init__(self) -> None:
        if self.daily_volatility <= 0:
            raise FuturesMarginEstimationError(
                f"{self.symbol} has a daily volatility of {self.daily_volatility}; a non-positive "
                f"volatility would make the initial margin zero, which reads as a risk-free "
                f"position rather than as an unmeasurable one"
            )
        if self.close_price_rupees <= 0:
            raise FuturesMarginEstimationError(
                f"{self.symbol} has a close of {self.close_price_rupees}"
            )


@dataclass(frozen=True, slots=True)
class MarginEstimate:
    """What a broker would block, with every input that produced it kept visible.

    `caveat` is a field rather than a docstring because this number crosses into a sizing decision,
    and the thing sizing it must be able to read that it is an estimate.
    """

    product: MarginProduct
    notional_rupees: Decimal
    initial_margin_rupees: Decimal
    extreme_loss_margin_rupees: Decimal
    horizon_sessions: int
    daily_volatility: Decimal
    extreme_loss_fraction: Decimal
    source: str
    caveat: str

    @property
    def total_rupees(self) -> Decimal:
        return self.initial_margin_rupees + self.extreme_loss_margin_rupees

    @property
    def fraction_of_notional(self) -> Decimal:
        if self.notional_rupees <= 0:
            return Decimal("0")
        return self.total_rupees / self.notional_rupees

    def describe(self) -> str:
        return (
            f"{self.product.value}: Rs {self.total_rupees:,.2f} on Rs {self.notional_rupees:,.2f} "
            f"({self.fraction_of_notional * 100:.2f}% — initial "
            f"Rs {self.initial_margin_rupees:,.2f} over {self.horizon_sessions} session(s) at "
            f"sigma {self.daily_volatility}, extreme loss "
            f"Rs {self.extreme_loss_margin_rupees:,.2f} at {self.extreme_loss_fraction * 100:.2f}%)"
        )


class FuturesMarginEstimator:
    """Estimates SPAN-plus-exposure from NSE's published volatility and margin framework.

    **SOTA analog:** `marketcalls/marginism`, which computes the real thing from the exchange risk
    file and is wired in as the authoritative path the moment that file exists (`B36`). This engine
    is what runs until then, and it is deliberately built to be REPLACED rather than reconciled
    against — nothing downstream reads a number from here that it could not read from there.
    """

    def __init__(
        self,
        volatilities: dict[str, UnderlyingVolatility],
        *,
        confidence: float = VALUE_AT_RISK_CONFIDENCE,
    ) -> None:
        if not COIN_FLIP_CONFIDENCE < confidence < 1.0:
            raise FuturesMarginEstimationError(
                f"confidence {confidence} is not a one-tailed value-at-risk level; NSE states 99%"
            )
        self._volatilities = {symbol.strip().upper(): row for symbol, row in volatilities.items()}
        self._confidence = confidence
        self._quantile = Decimal(str(float(norm.ppf(confidence))))

    @property
    def quantile(self) -> Decimal:
        """The normal quantile at the stated confidence — DERIVED, never typed (`R.03`)."""
        return self._quantile

    @property
    def underlyings_covered(self) -> int:
        return len(self._volatilities)

    def volatility_for(self, underlying_symbol: str) -> UnderlyingVolatility | None:
        return self._volatilities.get(underlying_symbol.strip().upper())

    def estimate(
        self,
        *,
        product: MarginProduct,
        underlying_symbol: str,
        notional_rupees: Decimal,
        moneyness_fraction: Decimal | None = None,
        sessions_to_expiry: int | None = None,
        is_expiry_day: bool = False,
    ) -> MarginEstimate | None:
        """The margin for one position, or `None` when the underlying has no published volatility.

        `None` rather than a peer's sigma: an unmarginable contract is not tradeable, and
        substituting a similar name's volatility is the invented number `R.03` forbids. The caller
        treats it as "cannot size this", which is the same shape as `GateVerdict.UNPRICEABLE`.

        `moneyness_fraction` is how far out of the money the strike sits, as a fraction of the
        underlying — only consulted for short options, where NSE's own table steps on it.
        """
        if notional_rupees <= 0:
            return None
        volatility = self.volatility_for(underlying_symbol)
        if volatility is None:
            return None

        horizon = (
            FUTURES_VALUE_AT_RISK_HORIZON_SESSIONS
            if product.is_future
            else OPTION_VALUE_AT_RISK_HORIZON_SESSIONS
        )
        scaling = Decimal(str(math.sqrt(horizon)))
        initial_fraction = self._quantile * volatility.daily_volatility * scaling
        extreme_fraction = self._extreme_loss_fraction(
            product,
            moneyness_fraction=moneyness_fraction,
            sessions_to_expiry=sessions_to_expiry,
            is_expiry_day=is_expiry_day,
        )
        return MarginEstimate(
            product=product,
            notional_rupees=notional_rupees,
            initial_margin_rupees=initial_fraction * notional_rupees,
            extreme_loss_margin_rupees=extreme_fraction * notional_rupees,
            horizon_sessions=horizon,
            daily_volatility=volatility.daily_volatility,
            extreme_loss_fraction=extreme_fraction,
            source=NSE_MARGIN_FRAMEWORK_SOURCE,
            caveat=(
                "ESTIMATE, not SPAN: a normal-tail VaR plus the published extreme-loss rate, with "
                "no scenario grid and no spread credits. Understates a naked short far from the "
                "money and an expiry-day position; overstates a hedged book. Replaced by the real "
                "SPAN file the moment one exists (B36)."
            ),
        )

    def _extreme_loss_fraction(
        self,
        product: MarginProduct,
        *,
        moneyness_fraction: Decimal | None,
        sessions_to_expiry: int | None,
        is_expiry_day: bool,
    ) -> Decimal:
        """NSE's own table, walked in the order the page states it.

        A LONG option is charged the base rate rather than zero. NSE levies extreme loss margin on
        short positions, and a long option's risk is bounded by its premium — but this engine is
        consumed by a capital bound, and charging a long option nothing would let a book of them
        consume no capital at all while still costing real premium. The base rate is the
        conservative reading and it is named here rather than silently applied.
        """
        base = (
            INDEX_DERIVATIVE_EXTREME_LOSS_FRACTION
            if product.is_index
            else STOCK_DERIVATIVE_EXTREME_LOSS_FRACTION
        )
        if product is MarginProduct.INDEX_OPTION_SHORT:
            if sessions_to_expiry is not None and sessions_to_expiry > LONG_DATED_OPTION_SESSIONS:
                base = max(base, LONG_DATED_INDEX_OPTION_EXTREME_LOSS_FRACTION)
            elif (
                moneyness_fraction is not None
                and moneyness_fraction > INDEX_DEEP_OUT_OF_THE_MONEY_THRESHOLD_FRACTION
            ):
                base = max(base, DEEP_OUT_OF_THE_MONEY_INDEX_EXTREME_LOSS_FRACTION)
            if is_expiry_day:
                base += EXPIRY_DAY_ADDITIONAL_INDEX_EXTREME_LOSS_FRACTION
        elif product is MarginProduct.STOCK_OPTION_SHORT and (
            moneyness_fraction is not None
            and moneyness_fraction > STOCK_DEEP_OUT_OF_THE_MONEY_THRESHOLD_FRACTION
        ):
            base = max(base, DEEP_OUT_OF_THE_MONEY_STOCK_EXTREME_LOSS_FRACTION)
        return base


NSE_VOLATILITY_DECAY = Decimal("0.995")
"""NSE's own exponential decay, quoted from the header of the file it publishes:

    `Current Day Underlying Daily Volatility (E) = Sqrt(0.995*D*D + 0.005*C*C)`

Not recalled and not fitted. A fit over 600 symbols swept 0.90 to 0.98 with the error falling
monotonically to the edge of the grid and never turning — the signature of a wrong model absorbing
the parameter — while the answer was printed in the column name all along (`O.133`). Confirmed by
solving `lambda = (E^2 - C^2)/(D^2 - C^2)` over 2,714 real rows: median **0.9932**, agreeing with
0.995 to the rounding of NSE's four-decimal sigmas.

Its half-life is about **138 sessions**, which is why a freshly seeded series is reported with its
observation count rather than presented as settled.
"""

NSE_ANNUALISATION_DAYS = 365
"""NSE annualises by CALENDAR days — `Underlying Annualised Volatility (F) = E*Sqrt(365)`, quoted
from the same header.

**Deliberately different from `BlackScholesOptionAnalyticsEngine`, which annualises by TRADING
SESSIONS** (246 in 2026, measured from the real calendar) because volatility accrues while the
market is open. Both choices are defensible and they are not interchangeable: a sigma compared
against NSE's MARGIN framework uses NSE's convention, and a sigma used to PRICE an option uses the
session convention. Written down beside each other so the next reader cannot silently pick the wrong
one.
"""

SEED_OBSERVATIONS_FOR_A_VARIANCE = 2
"""A variance needs two points. A definition of the statistic, not a parameter."""

DAILY_VOLATILITY_COLUMN = "Current Day Underlying Daily Volatility"
"""The column CMVOLT publishes the EWMA daily sigma in. Matched by prefix because NSE appends the
formula to the header text (`... (E) = ...`), which has changed shape before."""


def load_published_volatilities(csv_path: Path) -> dict[str, UnderlyingVolatility]:
    """Read a `CMVOLT_<ddmmyyyy>.CSV` into the estimator's input.

    Columns are matched by NAME rather than by position: NSE embeds the formula in the header
    (`Underlying Log Returns (C) = LN(A/B)`) and has changed that text without changing the data,
    so an index-based reader would silently shift a column the day they edit it.
    """
    volatilities: dict[str, UnderlyingVolatility] = {}
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise FuturesMarginEstimationError(f"{csv_path} carries no header row")
        index = _column_index(header)
        for row in reader:
            if len(row) <= max(index.values()):
                continue
            parsed = _volatility_row(row, index)
            if parsed is not None:
                volatilities[parsed.symbol] = parsed
    return volatilities


def _column_index(header: list[str]) -> dict[str, int]:
    wanted = {
        "date": "Date",
        "symbol": "Symbol",
        "close": "Underlying Close Price",
        "daily": DAILY_VOLATILITY_COLUMN,
        "annual": "Annualised Volatility",
    }
    index: dict[str, int] = {}
    for key, prefix in wanted.items():
        for position, column in enumerate(header):
            if column.strip().lower().startswith(prefix.strip().lower()):
                index[key] = position
                break
    for required in ("symbol", "close", "daily"):
        if required not in index:
            raise FuturesMarginEstimationError(
                f"CMVOLT header is missing the {required!r} column; it reads {header!r}. A reader "
                f"that guessed a position here would margin every position off the wrong number"
            )
    # The annualised column's header carries a formula and has no stable prefix in every era; it is
    # reported rather than required, and falls back to the last column when absent.
    index.setdefault("annual", len(header) - 1)
    index.setdefault("date", 0)
    return index


def _volatility_row(row: list[str], index: dict[str, int]) -> UnderlyingVolatility | None:
    symbol = row[index["symbol"]].strip().upper()
    if not symbol:
        return None
    try:
        close = Decimal(row[index["close"]].strip())
        daily = Decimal(row[index["daily"]].strip())
        annual = Decimal(row[index["annual"]].strip())
    except (ArithmeticError, ValueError, IndexError):
        return None
    if close <= 0 or daily <= 0:
        return None
    try:
        return UnderlyingVolatility(
            symbol=symbol,
            session_date=_parse_session_date(row[index["date"]].strip()),
            close_price_rupees=close,
            daily_volatility=daily,
            annualised_volatility=annual,
        )
    except FuturesMarginEstimationError:
        return None


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_session_date(text: str) -> date:
    """CMVOLT publishes `17-AUG-2026`. Parsed explicitly so a format change fails loudly."""
    parts = text.replace("/", "-").split("-")
    if len(parts) != DATE_PARTS_IN_A_CMVOLT_DATE:
        raise FuturesMarginEstimationError(f"cannot read a session date from {text!r}")
    day, month, year = parts
    month_number = _MONTHS.get(month.strip().upper()[:3])
    if month_number is None:
        raise FuturesMarginEstimationError(f"unknown month in {text!r}")
    return date(int(year), month_number, int(day))


INDEX_CLOSE_COLUMN = "Closing Index Value"
INDEX_NAME_COLUMN = "Index Name"
"""Columns of `ind_close_all_<ddmmyyyy>.csv` — NSE's official daily index close file (HTTP 200,
17,278 bytes), the only published source of index SPOT closes. `CMVOLT` is the CASH file and carries
no index row at all, which is `B37`."""

INDEX_SYMBOL_BY_PUBLISHED_NAME: dict[str, str] = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "NIFTY NEXT 50": "NIFTYNXT50",
}
"""NSE publishes indices under their display names and trades their derivatives under ticker
symbols; the two are different strings for the same instrument and something has to join them.

Mapped explicitly rather than by a normalising rule, because the rule would have to turn
"NIFTY MIDCAP SELECT" into "MIDCPNIFTY", which no general transformation does. Only the five
underlyings that actually have index futures are listed — an index with no derivative needs no
margin.
"""


def nse_volatility_recursion(
    previous_variance: Decimal, log_return: Decimal, *, decay: Decimal = NSE_VOLATILITY_DECAY
) -> Decimal:
    """One step of NSE's own published recursion: `E^2 = 0.995*D^2 + 0.005*C^2`.

    Exposed as a named function rather than inlined so the convention has exactly one definition and
    a test can pin it against the formula quoted in `NSE_VOLATILITY_DECAY`.
    """
    return decay * previous_variance + (Decimal("1") - decay) * (log_return * log_return)


def index_volatility_from_closes(
    symbol: str,
    closes: Sequence[tuple[date, Decimal]],
    *,
    decay: Decimal = NSE_VOLATILITY_DECAY,
) -> UnderlyingVolatility | None:
    """Roll NSE's exact recursion over an index's own closes.

    `closes` are `(session, close)` in any order; they are sorted here because a series fed
    backwards produces log returns of the opposite sign, which squares away silently and yields
    a plausible wrong answer.

    **Seeded from the sample variance of the available returns**, then rolled. With a decay of 0.995
    the recursion moves very little per step, so a short history returns approximately its own
    realised variance — which is the honest answer for a short history, and the observation count
    travels on the result so a thin estimate is visible rather than confident.

    `None` when there are too few closes for a variance to mean anything, never a substituted sigma.
    """
    ordered = sorted(closes, key=lambda row: row[0])
    returns: list[Decimal] = []
    for index in range(1, len(ordered)):
        previous, current = ordered[index - 1][1], ordered[index][1]
        if previous <= 0 or current <= 0:
            continue
        returns.append(Decimal(str(math.log(float(current) / float(previous)))))
    if len(returns) < SEED_OBSERVATIONS_FOR_A_VARIANCE:
        return None

    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal("0")) / Decimal(
        len(returns) - 1
    )
    if variance <= 0:
        return None
    for value in returns:
        variance = nse_volatility_recursion(variance, value, decay=decay)
    if variance <= 0:
        return None

    daily = Decimal(str(math.sqrt(float(variance))))
    return UnderlyingVolatility(
        symbol=symbol.strip().upper(),
        session_date=ordered[-1][0],
        close_price_rupees=ordered[-1][1],
        daily_volatility=daily,
        # NSE's own annualisation, sqrt(365), NOT the trading-session one the option engine uses.
        annualised_volatility=daily * Decimal(str(math.sqrt(NSE_ANNUALISATION_DAYS))),
    )


def load_index_close_row(csv_path: Path) -> dict[str, Decimal]:
    """One day of `ind_close_all_<ddmmyyyy>.csv`, reduced to the five derivative underlyings."""
    closes: dict[str, Decimal] = {}
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get(INDEX_NAME_COLUMN) or "").strip().upper()
            symbol = INDEX_SYMBOL_BY_PUBLISHED_NAME.get(name)
            if symbol is None:
                continue
            try:
                close = Decimal((row.get(INDEX_CLOSE_COLUMN) or "").strip())
            except (ArithmeticError, ValueError):
                continue
            if close > 0:
                closes[symbol] = close
    return closes


def index_volatilities_from_market_data(
    market_data: Path, *, as_of: date
) -> dict[str, UnderlyingVolatility]:
    """Index sigmas from the underlying spot each F&O bhavcopy row already carries.

    **Why this source and not `ind_close_all`.** That file is the official index close series and it
    is one HTTP request PER SESSION — seeding a 0.995 recursion from it would mean hundreds of
    fetches against a host this project already handles carefully. `fo_bhavcopy_contracts` carries
    `underlying_price` on every index-derivative row and is ALREADY ingested, so the same series is
    available locally at zero fetches. `load_index_close_row` remains for accruing the authoritative
    file forward, one session a day, which is the acquisition that removes this compromise (`R.16`).

    Point-in-time: only sessions at or before `as_of` are read.
    """
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT underlying_symbol, trade_date, MAX(underlying_price)
            FROM fo_bhavcopy_contracts
            WHERE contract_type IN ('IDF', 'IDO') AND underlying_price > 0 AND trade_date <= ?
            GROUP BY underlying_symbol, trade_date
            ORDER BY underlying_symbol, trade_date
            """,
            (as_of.isoformat(),),
        ).fetchall()

    series: dict[str, list[tuple[date, Decimal]]] = {}
    for symbol, session, close in rows:
        try:
            parsed = date.fromisoformat(str(session))
            price = Decimal(str(close))
        except (ArithmeticError, ValueError):
            continue
        if price > 0:
            series.setdefault(str(symbol).strip().upper(), []).append((parsed, price))

    volatilities: dict[str, UnderlyingVolatility] = {}
    for symbol, closes in series.items():
        estimated = index_volatility_from_closes(symbol, closes)
        if estimated is not None:
            volatilities[symbol] = estimated
    return volatilities


def margin_estimator_for(
    *, volatility_file: Path | None, market_data: Path, as_of: date
) -> FuturesMarginEstimator:
    """What the session uses: NSE's published stock sigmas plus computed index ones.

    Both halves are measured on NSE's own convention — the stock sigmas because they ARE NSE's, and
    the index ones because `index_volatility_from_closes` rolls the recursion NSE prints in its own
    header. That is the whole point of recovering the convention rather than choosing one.
    """
    published: dict[str, UnderlyingVolatility] = {}
    if volatility_file is not None and volatility_file.exists():
        published = load_published_volatilities(volatility_file)
    return FuturesMarginEstimator(
        published | index_volatilities_from_market_data(market_data, as_of=as_of)
    )
