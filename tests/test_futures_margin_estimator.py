"""Tests for `L6.30`'s margin estimator and the capital rule it feeds — spec `docs/research/264`.

The exposure test is the one that matters. That rule was written THREE times and the first two both
looked correct while failing in opposite directions — refusing every proposal, then admitting a
100%-one-sided book against a 25% bound. So it is pinned from BOTH ends in one test, for the same
reason `docs/research/261` gives: a one-sided criterion is satisfied by a degenerate answer.
"""

from __future__ import annotations

import csv
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.paper_loop.segment_bot_paper_session import (
    SegmentBotCapitalPolicy,
    SegmentBotPaperSession,
    SegmentBotPaperSessionError,
)
from nse_algo_trader.sizing.futures_margin_estimator import (
    NSE_ANNUALISATION_DAYS,
    NSE_VOLATILITY_DECAY,
    FuturesMarginEstimationError,
    FuturesMarginEstimator,
    MarginProduct,
    UnderlyingVolatility,
    index_volatility_from_closes,
    load_published_volatilities,
    nse_volatility_recursion,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)

INSTANT = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
SESSION = date(2026, 8, 17)
NOTIONAL = Decimal("500000")

PUBLISHED_VOLATILITY_FILE = Path("~/.nse_algo_trader/CMVOLT_17082026.CSV").expanduser()
"""NSE's real published volatility file, if this machine has fetched one."""


def a_volatility(symbol: str, daily: str, close: str = "1000") -> UnderlyingVolatility:
    return UnderlyingVolatility(
        symbol=symbol,
        session_date=SESSION,
        close_price_rupees=Decimal(close),
        daily_volatility=Decimal(daily),
        annualised_volatility=Decimal(daily) * Decimal(str(math.sqrt(246))),
    )


def an_estimator(**volatilities: str) -> FuturesMarginEstimator:
    return FuturesMarginEstimator(
        {symbol: a_volatility(symbol, daily) for symbol, daily in volatilities.items()}
    )


# --------------------------------------------------------------------------------------------
# the estimator
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_quantile_is_derived_from_the_confidence_never_typed() -> None:
    """`R.03`: NSE publishes 99%; 2.326 is what that IMPLIES, not an input."""
    estimator = an_estimator(RELIANCE="0.0135")
    assert float(estimator.quantile) == pytest.approx(2.3263478740, abs=1e-9)
    stricter = FuturesMarginEstimator(
        {"RELIANCE": a_volatility("RELIANCE", "0.0135")}, confidence=0.999
    )
    assert stricter.quantile > estimator.quantile


@pytest.mark.unit
def test_a_confidence_that_is_not_a_risk_level_is_refused() -> None:
    with pytest.raises(FuturesMarginEstimationError, match="value-at-risk"):
        FuturesMarginEstimator({}, confidence=0.5)


@pytest.mark.unit
def test_futures_carry_a_two_day_horizon_and_options_one() -> None:
    """NSE states both. The ratio is exactly sqrt(2) and nothing else may move it."""
    estimator = an_estimator(RELIANCE="0.0135")
    future = estimator.estimate(
        product=MarginProduct.STOCK_FUTURE, underlying_symbol="RELIANCE", notional_rupees=NOTIONAL
    )
    option = estimator.estimate(
        product=MarginProduct.STOCK_OPTION_SHORT,
        underlying_symbol="RELIANCE",
        notional_rupees=NOTIONAL,
    )
    assert future is not None and option is not None
    assert future.horizon_sessions == 2
    assert option.horizon_sessions == 1
    ratio = future.initial_margin_rupees / option.initial_margin_rupees
    assert float(ratio) == pytest.approx(math.sqrt(2), abs=1e-9)


@pytest.mark.unit
def test_margin_rises_with_the_underlyings_own_volatility() -> None:
    """The whole point: ASHOKLEY is not RELIANCE, and a flat percentage would say it is."""
    estimator = an_estimator(RELIANCE="0.0135", ASHOKLEY="0.0228")
    calm = estimator.estimate(
        product=MarginProduct.STOCK_FUTURE, underlying_symbol="RELIANCE", notional_rupees=NOTIONAL
    )
    wild = estimator.estimate(
        product=MarginProduct.STOCK_FUTURE, underlying_symbol="ASHOKLEY", notional_rupees=NOTIONAL
    )
    assert calm is not None and wild is not None
    assert wild.fraction_of_notional > calm.fraction_of_notional
    # Both land in the band a real broker quotes for stock futures rather than anywhere at all.
    assert Decimal("0.05") < calm.fraction_of_notional < Decimal("0.20")
    assert Decimal("0.05") < wild.fraction_of_notional < Decimal("0.25")


@pytest.mark.unit
def test_the_extreme_loss_rates_are_the_ones_nse_publishes() -> None:
    """Index 2%, stock 3.5% — sourced, and asserted so a silent edit fails here."""
    estimator = an_estimator(RELIANCE="0.0135", NIFTY="0.0100")
    stock = estimator.estimate(
        product=MarginProduct.STOCK_FUTURE, underlying_symbol="RELIANCE", notional_rupees=NOTIONAL
    )
    index = estimator.estimate(
        product=MarginProduct.INDEX_FUTURE, underlying_symbol="NIFTY", notional_rupees=NOTIONAL
    )
    assert stock is not None and index is not None
    assert stock.extreme_loss_fraction == Decimal("0.035")
    assert index.extreme_loss_fraction == Decimal("0.02")


@pytest.mark.unit
def test_a_deep_out_of_the_money_short_option_is_charged_the_higher_published_rate() -> None:
    estimator = an_estimator(RELIANCE="0.0135", NIFTY="0.0100")
    near = estimator.estimate(
        product=MarginProduct.STOCK_OPTION_SHORT,
        underlying_symbol="RELIANCE",
        notional_rupees=NOTIONAL,
        moneyness_fraction=Decimal("0.05"),
    )
    far = estimator.estimate(
        product=MarginProduct.STOCK_OPTION_SHORT,
        underlying_symbol="RELIANCE",
        notional_rupees=NOTIONAL,
        moneyness_fraction=Decimal("0.40"),
    )
    assert near is not None and far is not None
    assert near.extreme_loss_fraction == Decimal("0.035")
    assert far.extreme_loss_fraction == Decimal("0.0525")

    expiry = estimator.estimate(
        product=MarginProduct.INDEX_OPTION_SHORT,
        underlying_symbol="NIFTY",
        notional_rupees=NOTIONAL,
        is_expiry_day=True,
    )
    assert expiry is not None
    assert expiry.extreme_loss_fraction == Decimal("0.04")  # 2% base + 2% expiry-day addition


@pytest.mark.unit
def test_an_underlying_with_no_published_volatility_is_none_not_a_substitute() -> None:
    """`R.03`: a peer's sigma is an invented number. `None` means "cannot size this"."""
    estimator = an_estimator(RELIANCE="0.0135")
    assert (
        estimator.estimate(
            product=MarginProduct.STOCK_FUTURE,
            underlying_symbol="SOMETHINGELSE",
            notional_rupees=NOTIONAL,
        )
        is None
    )


@pytest.mark.unit
def test_every_estimate_says_it_is_an_estimate() -> None:
    """It crosses into a sizing decision, so the thing sizing it must be able to read the caveat."""
    estimate = an_estimator(RELIANCE="0.0135").estimate(
        product=MarginProduct.STOCK_FUTURE, underlying_symbol="RELIANCE", notional_rupees=NOTIONAL
    )
    assert estimate is not None
    assert "not SPAN" in estimate.caveat
    assert "B36" in estimate.caveat
    assert "nseindia.com" in estimate.source


@pytest.mark.unit
def test_a_zero_volatility_underlying_is_refused_at_construction() -> None:
    with pytest.raises(FuturesMarginEstimationError, match="volatility"):
        a_volatility("RELIANCE", "0")


@pytest.mark.real_data
@pytest.mark.skipif(
    not PUBLISHED_VOLATILITY_FILE.exists(), reason="no CMVOLT file fetched on this machine"
)
def test_the_real_published_file_parses_and_covers_the_cash_board() -> None:
    """`R.05`. Also pins `B37`: the INDEX underlyings are genuinely absent from this file."""
    volatilities = load_published_volatilities(PUBLISHED_VOLATILITY_FILE)
    assert len(volatilities) > 4000
    assert "RELIANCE" in volatilities
    for index_underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
        assert index_underlying not in volatilities, (
            f"{index_underlying} appeared in CMVOLT — B37 may be closed, re-check the blocker"
        )


# --------------------------------------------------------------------------------------------
# the capital rule — pinned from BOTH ends, deliberately
# --------------------------------------------------------------------------------------------


def a_future_signal(symbol: str, side: TradeLeg, price_paise: str, quantity: int) -> PricedSignal:
    return PricedSignal(
        instrument_token=abs(hash(symbol)) % 10_000_000 + 1,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_FUTURES,
        side=side,
        decided_at=INSTANT,
        reference_price_paise=Decimal(price_paise),
        expected_edge_bps=Decimal("20"),
        proposed_quantity=quantity,
        edge_basis=EdgeBasis.CALIBRATED_MODEL,
        source="test",
        conviction=Decimal("0.6"),
    )


def a_session(**volatilities: str) -> SegmentBotPaperSession:
    return SegmentBotPaperSession(
        bots=(),
        capital=SegmentBotCapitalPolicy(deployable_rupees=Decimal("1000000")),
        margin_estimator=an_estimator(**volatilities),
    )


@pytest.mark.unit
def test_the_exposure_rule_admits_a_balanced_book_and_refuses_a_one_sided_one() -> None:
    """BOTH ends in one test, because each of the first two versions passed one end and failed the
    other: version 1 refused 23 of 23 proposals, version 2 admitted a 100%-one-sided book against a
    25% bound. A rule that can only build or only refuse is not a bound.
    """
    session = a_session(AAA="0.0150", BBB="0.0150", CCC="0.0150", DDD="0.0150")
    proposals = [
        a_future_signal("AAA26AUGFUT", TradeLeg.SELL, "100000", 100),
        a_future_signal("BBB26AUGFUT", TradeLeg.SELL, "100000", 100),
        a_future_signal("CCC26AUGFUT", TradeLeg.BUY, "100000", 100),
        a_future_signal("DDD26AUGFUT", TradeLeg.BUY, "100000", 100),
    ]
    admitted, refused = session._within_capital(proposals, budget_rupees=Decimal("1000000"))
    assert admitted, "the rule must be able to BUILD a book — version 1 could not"
    sides = {signal.side for signal in admitted}
    assert sides == {TradeLeg.BUY, TradeLeg.SELL}, (
        "a book this rule builds must be two-sided — version 2 admitted four sells"
    )
    gross: Decimal = sum(
        ((s.reference_price_paise * s.proposed_quantity) / Decimal("100") for s in admitted),
        Decimal("0"),
    )
    net: Decimal = sum(
        (
            ((s.reference_price_paise * s.proposed_quantity) / Decimal("100"))
            * (Decimal("1") if s.side is TradeLeg.BUY else Decimal("-1"))
            for s in admitted
        ),
        Decimal("0"),
    )
    assert abs(net) / gross <= Decimal("0.25")
    assert refused >= 0


@pytest.mark.unit
def test_an_all_one_side_scan_cannot_build_a_large_one_sided_book() -> None:
    """Given nothing but sells, the rule admits the first and then binds."""
    session = a_session(AAA="0.0150", BBB="0.0150", CCC="0.0150")
    proposals = [
        a_future_signal(f"{name}26AUGFUT", TradeLeg.SELL, "100000", 100)
        for name in ("AAA", "BBB", "CCC")
    ]
    admitted, refused = session._within_capital(proposals, budget_rupees=Decimal("1000000"))
    assert len(admitted) == 1, "a book of one is unavoidable; a book of three one-sided is not"
    assert refused == 2


@pytest.mark.unit
def test_a_derivative_with_no_margin_estimate_is_refused_rather_than_sized_on_notional() -> None:
    """Margin and notional differ by roughly 10x for a future; falling back is not conservative."""
    session = a_session(AAA="0.0150")
    unknown = a_future_signal("ZZZ26AUGFUT", TradeLeg.BUY, "100000", 100)
    admitted, refused = session._within_capital([unknown], budget_rupees=Decimal("1000000"))
    assert admitted == ()
    assert refused == 1


@pytest.mark.unit
def test_margin_lets_the_same_capital_hold_far_more_than_notional_would() -> None:
    """The measured reason the futures bots went from 0 trades to a real book."""
    session = a_session(AAA="0.0150")
    signal = a_future_signal("AAA26AUGFUT", TradeLeg.BUY, "100000", 100)
    notional = Decimal("100000") * 100 / Decimal("100")
    consumed = session._capital_consumed_by(signal, notional)
    assert consumed is not None
    assert consumed < notional / 4, "margin must be a small fraction of notional for a future"


@pytest.mark.unit
def test_a_session_with_no_capital_is_refused_at_construction() -> None:
    with pytest.raises(SegmentBotPaperSessionError, match="deployable capital"):
        SegmentBotCapitalPolicy(deployable_rupees=Decimal("0"))


# --------------------------------------------------------------------------------------------
# NSE's own volatility convention — recovered from their file, not recalled
# --------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_recursion_is_the_formula_nse_prints_in_its_own_header() -> None:
    """`E^2 = 0.995*D^2 + 0.005*C^2`, quoted verbatim from the CMVOLT column name.

    Pinned as an equation rather than a remembered constant, because a fit against this same data
    swept 0.90 to 0.98 without ever turning and would have shipped 0.98 as "calibrated" (`O.133`).
    """
    assert Decimal("0.995") == NSE_VOLATILITY_DECAY
    assert NSE_ANNUALISATION_DAYS == 365, "NSE annualises by CALENDAR days, not trading sessions"
    previous_variance = Decimal("0.0004")
    log_return = Decimal("0.01")
    expected = Decimal("0.995") * previous_variance + Decimal("0.005") * (log_return * log_return)
    assert nse_volatility_recursion(previous_variance, log_return) == expected


@pytest.mark.real_data
@pytest.mark.skipif(
    not PUBLISHED_VOLATILITY_FILE.exists(), reason="no CMVOLT file fetched on this machine"
)
def test_the_recursion_reproduces_every_published_sigma_within_nses_own_precision() -> None:
    """`R.05`, and the strongest check available: NSE publishes D, C and E on the same row.

    Measured over the real file: **4,881 rows, 100% within 1e-4, worst deviation 9.95e-05** — below
    the four-decimal granularity NSE publishes at, so the convention is matched exactly rather than
    approximately.
    """
    rows = list(
        csv.reader(PUBLISHED_VOLATILITY_FILE.open(newline="", encoding="utf-8", errors="replace"))
    )
    checked = 0
    worst = 0.0
    for row in rows[1:]:
        try:
            log_return, previous, current = Decimal(row[4]), Decimal(row[5]), Decimal(row[6])
        except Exception:  # noqa: BLE001 — a malformed row is not the subject of this test
            continue
        predicted = Decimal(
            str(float(nse_volatility_recursion(previous * previous, log_return)) ** 0.5)
        )
        checked += 1
        worst = max(worst, abs(float(predicted - current)))
    assert checked > 4000
    assert worst < 1e-4, f"worst deviation {worst:.3e} exceeds NSE's own publication precision"


@pytest.mark.unit
def test_an_index_volatility_is_computed_on_the_same_convention_as_the_stock_ones() -> None:
    """`B37`: index sigma must not be measured differently from the stock sigmas it sits beside."""
    closes = [
        (date(2026, 7, 1) + timedelta(days=index), Decimal(str(24000 + (index % 5) * 60)))
        for index in range(40)
    ]
    volatility = index_volatility_from_closes("NIFTY", closes)
    assert volatility is not None
    assert volatility.symbol == "NIFTY"
    assert volatility.daily_volatility > 0
    # NSE's own annualisation, sqrt(365) — not the trading-session one the option engine uses.
    assert float(volatility.annualised_volatility) == pytest.approx(
        float(volatility.daily_volatility) * math.sqrt(365), rel=1e-9
    )


@pytest.mark.unit
def test_index_volatility_is_order_independent_and_refuses_a_series_too_short() -> None:
    """A series fed backwards produces sign-flipped returns that square away silently."""
    closes = [
        (date(2026, 7, 1), Decimal("24000")),
        (date(2026, 7, 2), Decimal("24200")),
        (date(2026, 7, 3), Decimal("24100")),
        (date(2026, 7, 6), Decimal("24350")),
    ]
    forwards = index_volatility_from_closes("NIFTY", closes)
    backwards = index_volatility_from_closes("NIFTY", list(reversed(closes)))
    assert forwards is not None and backwards is not None
    assert forwards.daily_volatility == backwards.daily_volatility
    assert index_volatility_from_closes("NIFTY", closes[:1]) is None
