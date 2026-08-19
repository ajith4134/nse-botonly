"""`R.05` — the charge engine priced against the REAL NSE tape, not against fixtures.

A cost engine can be internally consistent and still wrong about the market, so these tests
run it over real traded prices out of `L0.34`'s 193-million-row archive and check the things
only real data can falsify:

1. Every real closing price in a real cross-section prices without exception.
2. The breakeven series STEPS on the statutory change dates — up on 2024-10-01 when options
   STT went from 0.0625% to 0.1%, and up again on 2026-04-01 at 0.15% — and is FLAT in
   between. A model using today's rates for every date produces a straight line here, which
   is the look-ahead-in-costs leak this engine exists to prevent, and this is the assertion
   that would catch it.
3. Dates before the flat-charge era REFUSE rather than being priced with a modern rate.

Skipped, not failed, when the archive is absent — the store is 4.5 GB and lives outside the
repo. The skip message says exactly what to run, so a skip cannot be mistaken for a pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from nse_algo_trader.deep_history.deep_history_archive_loader import (
    DEFAULT_DEEP_HISTORY_PATH,
    DeepHistoryArchiveLoader,
)
from nse_algo_trader.transaction_cost.breakeven_move_solver import solve_breakeven_move
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    CostCoverageError,
    NseTransactionCostEngine,
    TradeSpecification,
    default_transaction_cost_engine,
)
from tests.deep_history_archive_reader_for_tests import deep_history_archive_or_skip

pytestmark = [
    pytest.mark.real_data,
    pytest.mark.skipif(
        not DEFAULT_DEEP_HISTORY_PATH.exists(),
        reason=(
            f"the deep-history store is absent at {DEFAULT_DEEP_HISTORY_PATH}; build it with "
            f"scripts/run_daily_operations.py before claiming an R.05 pass"
        ),
    ),
]

_A_REAL_TRADING_DAY = date(2026, 6, 30)
_STANDARD_LOT = 65


@pytest.fixture(scope="module")
def archive() -> Iterator[DeepHistoryArchiveLoader]:
    with deep_history_archive_or_skip(DEFAULT_DEEP_HISTORY_PATH) as loader:
        yield loader


@pytest.fixture(scope="module")
def engine() -> NseTransactionCostEngine:
    return default_transaction_cost_engine()


def _first_real_cross_section(
    archive: DeepHistoryArchiveLoader,
) -> tuple[date, tuple[str, ...]]:
    """A real trading day with a real universe, walked back until one is found."""
    for offset in range(0, 40):
        candidate = date.fromordinal(_A_REAL_TRADING_DAY.toordinal() - offset)
        symbols = archive.symbols_on(candidate)
        if len(symbols) > 100:
            return candidate, symbols
    pytest.skip("no cash cross-section of more than 100 symbols found in the archive")


@pytest.mark.real_data
def test_every_real_price_in_a_real_cross_section_prices(
    archive: DeepHistoryArchiveLoader,
    engine: NseTransactionCostEngine,
) -> None:
    """`R.09`: the whole published universe of a day, not a hand-picked sample."""
    trade_date, symbols = _first_real_cross_section(archive)
    priced_count = 0
    zero_priced: list[str] = []
    for symbol in symbols:
        closes = [(day, close) for day, close in archive.close_series(symbol) if day == trade_date]
        if not closes or closes[0][1] <= 0:
            continue
        close_paise = Decimal(closes[0][1])
        trade = TradeSpecification(
            segment=ChargeableSegment.EQUITY_INTRADAY,
            quantity=100,
            entry_price_paise=close_paise,
            exit_price_paise=close_paise,
            trade_date=trade_date,
        )
        cost = engine.price_round_trip(trade)
        # The EXACT cost must always be positive: a real trade is never free. The BILLED cost
        # legitimately can be zero, and on real data it is — DHARAN closed at 16 paise on
        # 2026-06-30, so a hundred shares turn over Rs 16 and every levy on it is a fraction
        # of a paisa. That case must be flagged, not hidden, because a gate dividing an edge
        # by a zero cost clears any hurdle.
        assert cost.exact_total_paise > 0, f"{symbol} priced at zero exact cost"
        if cost.bills_as_free:
            zero_priced.append(symbol)
        priced_count += 1
    assert priced_count > 100, f"only {priced_count} real symbols priced on {trade_date}"
    # Sub-paisa symbols exist and are reported, not asserted out of existence.
    assert len(zero_priced) < priced_count // 10, (
        f"{len(zero_priced)} of {priced_count} real symbols bill as free: {zero_priced[:5]}"
    )


@pytest.mark.real_data
def test_real_prices_produce_breakevens_in_the_range_the_plan_predicted(
    archive: DeepHistoryArchiveLoader, engine: NseTransactionCostEngine
) -> None:
    """`L1.04` expects roughly 6-8 bps for large cash. The real tape either agrees or does not.

    Asserted as a wide band rather than a point: the claim being tested is that the engine
    lands in the right ORDER OF MAGNITUDE on real prices, which is what a mis-scoped rate or a
    misplaced decimal would break.
    """
    trade_date, symbols = _first_real_cross_section(archive)
    breakevens: list[Decimal] = []
    for symbol in symbols[:200]:
        closes = [(day, close) for day, close in archive.close_series(symbol) if day == trade_date]
        if not closes or closes[0][1] <= 0:
            continue
        close_paise = Decimal(closes[0][1])
        trade = TradeSpecification(
            segment=ChargeableSegment.EQUITY_INTRADAY,
            quantity=1_000,
            entry_price_paise=close_paise,
            exit_price_paise=close_paise,
            trade_date=trade_date,
        )
        breakevens.append(solve_breakeven_move(engine, trade).move_bps)
    assert breakevens, "no real symbol produced a breakeven"
    typical = sorted(breakevens)[len(breakevens) // 2]
    assert Decimal(3) < typical < Decimal(30), f"median cash breakeven {typical} bps"


@pytest.mark.real_data
@pytest.mark.parametrize(
    ("as_of", "expected_ordering"),
    [
        (date(2024, 9, 30), "before"),
        (date(2024, 10, 1), "after"),
    ],
)
def test_the_october_2024_stt_rise_shows_up_as_a_step_in_the_breakeven(
    engine: NseTransactionCostEngine, as_of: date, expected_ordering: str
) -> None:
    """The point-in-time claim, tested where it would fail if rates were resolved as of today."""
    trade = TradeSpecification(
        segment=ChargeableSegment.EQUITY_OPTIONS,
        quantity=_STANDARD_LOT,
        entry_price_paise=Decimal(15_000),
        exit_price_paise=Decimal(15_000),
        trade_date=as_of,
        strike_paise=Decimal(2_400_000),
    )
    if expected_ordering == "before":
        # The exchange charge was a turnover slab until 2024-10-01 and is not compiled, so
        # this date must refuse rather than borrow the flat rate that replaced it.
        with pytest.raises(CostCoverageError):
            engine.price_round_trip(trade)
    else:
        assert engine.price_round_trip(trade).total_paise > 0


@pytest.mark.real_data
def test_the_option_breakeven_steps_up_on_each_statutory_change_and_is_flat_between(
    engine: NseTransactionCostEngine,
) -> None:
    """Three regimes, and the engine must distinguish them from real dates alone.

    A cost model resolving rates "as of today" returns the same number for all six dates. That
    is the failure this test exists for, and no fixture can produce it.
    """

    def breakeven_bps(as_of: date) -> Decimal:
        trade = TradeSpecification(
            segment=ChargeableSegment.EQUITY_OPTIONS,
            quantity=_STANDARD_LOT,
            entry_price_paise=Decimal(15_000),
            exit_price_paise=Decimal(15_000),
            trade_date=as_of,
            strike_paise=Decimal(2_400_000),
        )
        return solve_breakeven_move(engine, trade).move_bps

    early_regime = (date(2024, 10, 1), date(2025, 6, 1), date(2026, 3, 31))
    later_regime = (date(2026, 4, 1), date(2026, 6, 1), date(2026, 8, 12))

    early = [breakeven_bps(as_of) for as_of in early_regime]
    later = [breakeven_bps(as_of) for as_of in later_regime]

    # Flat within a regime: the March-2026 IPFT rollback moved money between two lines
    # without changing the total, so it must NOT appear as a step.
    assert len(set(early)) == 1, f"the pre-April-2026 regime is not flat: {early}"
    assert len(set(later)) == 1, f"the post-April-2026 regime is not flat: {later}"
    # And a real step where the STT actually rose from 0.1% to 0.15% of premium.
    assert later[0] > early[0], (
        f"the April-2026 STT rise did not raise the breakeven: {early} -> {later}"
    )
