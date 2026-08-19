#!/usr/bin/env python
"""Run the `L5.29` conformance suite against a real bot over the REAL NSE universe — `R.05`.

A conformance suite that has only ever seen fixtures proves that the fixtures conform. This is the
`R.05` pass for the protocol itself: the universe comes from the live Kite instrument master, the
closes come from the real bitemporal bar store, the regime comes from the real classifier
vocabulary, and the bot under test is driven by the real `IntradayMeanReversionEngine`, not a stub.

`R.09` — the full universe, never a sample. Every NSE equity instrument in the master is loaded, and
the count is printed so a shrunken universe is visible rather than silent.

What this DOES verify: that the contract survives contact with real instruments — real lot sizes,
real tick sizes, real symbols including the awkward ones — and that a bot built on a real engine can
satisfy it. What it does NOT verify: that the mean-reversion engine makes money. That is `L5.26`'s
own `R.05` pass, and conflating the two would be exactly the `R.13` error — correctness of execution
is not evidence of correctness of allocation.

    python scripts/verify_segment_bot_protocol_on_real_data.py

Exit 0 when the real-data run is clean, 1 when the suite found violations or the data is missing.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.historical_bars.point_in_time_five_minute_bar_reader import (
    PointInTimeFiveMinuteBarReader,
)
from nse_algo_trader.kite_instrument_master import InstrumentMasterStore
from nse_algo_trader.regime.market_regime_state import MarketRegime, RegimeDistribution
from nse_algo_trader.segment_bots.segment_bot_conformance import run_segment_bot_conformance
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotMaturity,
    BotMaturityRung,
    SegmentBotContext,
    SegmentRelevance,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import (
    TradingSegment,
    instrument_facts_for,
)
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    IntradayMeanReversionEngine,
    MeanReversionAction,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

INDIA = ZoneInfo("Asia/Kolkata")
MARKET_DATA_DATABASE = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()

FIVE_MINUTE_INTERVAL = "5m"
"""The only interval the store holds today, and the one the paper loop consumes."""

MINIMUM_REGIME_CONCENTRATION = 0.40
"""How concentrated the regime distribution must be before the engine will act.

Operator policy carried through from `PaperSessionPolicy`, not a constant of nature — it is passed
in here rather than defaulted inside the engine for exactly the reason that docstring gives: a
different strategy family consuming the same brain legitimately needs a different bar.
"""

MINIMUM_REGIME_AGREEMENT = 0.50

ENTRY_DEVIATION_SIGMA = 1.0
"""One rolling standard deviation — the floor below which the probe will not look at all.

The engine derives its own bands; this is only the point past which the PROBE bothers building a
signal. It is NOT the selection: on the first real run a 1-sigma floor over the full universe named
**1,594 instruments at one instant**, which tripped the suite's own `proposal-size-is-sane` check —
a check added the same day because an adversarial reviewer's synthetic bot exposed its absence, and
which then caught a real bot on real data at its first run. A floor is not a view.
"""

CROSS_SECTIONAL_SELECTION_QUANTILE = 0.99
"""Which quantile of TODAY'S OWN deviation distribution counts as unusual enough to act on.

`R.03`: derived, not a magic count. A fixed "top 50" would mean something different on a quiet day
than on a violent one, whereas a quantile of the cross-section adapts to the day the market actually
had — the same reasoning `PaperSessionPolicy.price_collar_quantile` already uses for price collars.
The probe states it here because a probe must be honest about being a probe; a real bot (`L5.26`)
learns this cut from its own track record rather than asserting it.
"""

BPS_PER_SIGMA = 10.0
"""Sigma-to-basis-points scaling for the probe's placeholder edge claim.

Deliberately crude and deliberately labelled: this probe verifies the CONTRACT, and an edge number
it invented must never be mistaken for a measured one. EdgeBasis.STRATEGY_HYPOTHESIS says so on
every signal it emits.
"""


class RealDataMeanReversionCashBot:
    """A cash-intraday bot driven by the REAL mean-reversion engine.

    Not `L5.26` — that bot owns a strategy SET, a relevance model and a track record, and gets its
    own `R.23c` loop. This is the thinnest possible real-engine bot that can be pointed at the
    conformance suite, so the suite is exercised against real behaviour rather than a fixture.
    """

    trading_segment = TradingSegment.CASH_INTRADAY
    bot_identity = "cash_intraday_mean_reversion_probe"

    def __init__(self, closes_by_token: dict[int, list[float]]) -> None:
        self._closes_by_token = closes_by_token
        self._engines: dict[int, IntradayMeanReversionEngine] = {}
        self._observed: set[datetime] = set()

    def _engine_for(self, instrument_token: int) -> IntradayMeanReversionEngine:
        engine = self._engines.get(instrument_token)
        if engine is None:
            engine = IntradayMeanReversionEngine(
                minimum_regime_concentration=MINIMUM_REGIME_CONCENTRATION,
                minimum_regime_agreement=MINIMUM_REGIME_AGREEMENT,
            )
            engine.observe_closes(self._closes_by_token.get(instrument_token, []))
            self._engines[instrument_token] = engine
        return engine

    def observe(self, context: SegmentBotContext) -> None:
        # Idempotent by instant: a replay re-observes the same moment, and double-counting it would
        # move every rolling statistic the engine carries.
        if context.decision_instant in self._observed:
            return
        self._observed.add(context.decision_instant)
        for instrument in context.tradeable_universe:
            self._engine_for(instrument.instrument_token)

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        candidates: list[tuple[float, float, TradeableInstrument]] = []
        for instrument in context.tradeable_universe:
            engine = self._engine_for(instrument.instrument_token)
            deviation: float | None = engine.current_deviation_sigma()
            if deviation is None or not engine.is_mature:
                continue
            magnitude = abs(deviation)
            if magnitude < ENTRY_DEVIATION_SIGMA:
                continue
            candidates.append((magnitude, deviation, instrument))

        # Select against the cross-section of THIS instant rather than against a fixed count, so a
        # quiet day proposes few and a violent day proposes more, both for the same reason.
        cut = _cross_sectional_cut(magnitude for magnitude, _, _ in candidates)

        signals: list[PricedSignal] = []
        for magnitude, deviation, instrument in candidates:
            if magnitude < cut:
                continue
            signals.append(
                PricedSignal(
                    instrument_token=instrument.instrument_token,
                    trading_symbol=instrument.trading_symbol,
                    segment=instrument_facts_for(self.trading_segment).chargeable_segment,
                    side=TradeLeg.BUY if deviation < 0 else TradeLeg.SELL,
                    decided_at=context.decision_instant,
                    reference_price_paise=Decimal(
                        str(round(self._closes_by_token[instrument.instrument_token][-1] * 100))
                    ),
                    expected_edge_bps=Decimal(str(round(magnitude * BPS_PER_SIGMA, 2))),
                    proposed_quantity=instrument.lot_size,
                    edge_basis=EdgeBasis.STRATEGY_HYPOTHESIS,
                    source=self.bot_identity,
                )
            )
        return tuple(signals)

    def relevance(self, context: SegmentBotContext) -> SegmentRelevance:  # noqa: ARG002
        mature = sum(1 for engine in self._engines.values() if engine.is_mature)
        total = max(len(self._engines), 1)
        return SegmentRelevance(
            applicability=mature / total,
            reason=f"{mature} of {total} instrument engines have enough history to band",
        )

    def maturity(self) -> BotMaturity:
        return BotMaturity(
            rung=BotMaturityRung.COLD_START,
            closed_trades_observed=0,
            evidence=(
                "no closed trades: this probe exists to exercise the L5.29 contract on real "
                "instruments, and its activation is gated by the maturity ladder (R.04)"
            ),
        )


def _cross_sectional_cut(magnitudes: Iterable[float]) -> float:
    """The selection threshold, read off the day's own distribution (`R.03`).

    Returns `inf` when there is nothing to select from, so an empty cross-section proposes nothing
    rather than proposing everything — the failure direction matters, and "no data" must never read
    as "act on all of it".
    """
    ranked = sorted(magnitudes)
    if not ranked:
        return math.inf
    index = min(int(len(ranked) * CROSS_SECTIONAL_SELECTION_QUANTILE), len(ranked) - 1)
    return ranked[index]


def _load_real_universe(as_of: date) -> tuple[TradeableInstrument, ...]:
    """Every NSE equity instrument the master holds — `R.09`, never a sample."""
    with InstrumentMasterStore(MARKET_DATA_DATABASE) as master:
        records = master.instruments_as_of(as_of, exchange="NSE")
    return tuple(
        TradeableInstrument(
            instrument_token=record.instrument_token,
            trading_symbol=record.tradingsymbol,
            lot_size=record.lot_size,
            tick_size_paise=max(int(record.tick_size * 100), 1),
        )
        for record in records
        if record.instrument_type == "EQ" and record.lot_size > 0
    )


def _load_real_closes(as_of: datetime, tokens: set[int]) -> dict[int, list[float]]:
    """Real closes as of an instant, through the reader that cannot be called without one.

    This used to be hand-rolled SQL here, carrying its own `availability_time <= ?` clause. It was
    correct, and being correct by remembering is exactly the fragility `B12` records — so it now
    goes through `PointInTimeFiveMinuteBarReader`, which has no method that omits the cutoff.

    Reads `price_bars` (1,246,985 five-minute bars, 3,787 instruments), not the daily
    `daily_reconciled_bar` table (203 rows). An earlier version of this probe read the daily store,
    saw 201 instruments, and I published "the project has no bar history" as a measured fact;
    corrected in `A.132` / `O.114` / `O.115`.
    """
    reader = PointInTimeFiveMinuteBarReader(MARKET_DATA_DATABASE)
    return {
        instrument_token: [float(close) for close in closes]
        for instrument_token, closes in reader.closes_for_instruments(as_of, tokens).items()
    }


def main() -> int:
    as_of = datetime.now(INDIA)
    as_of_date = as_of.date()

    universe = _load_real_universe(as_of_date)
    print(f"real NSE equity universe: {len(universe):,} instruments (R.09: full, not a sample)")
    if not universe:
        print("no instrument master rows — run the daily operations ingest first", file=sys.stderr)
        return 1

    closes = _load_real_closes(as_of, {i.instrument_token for i in universe})
    stamp = f"{as_of:%Y-%m-%d %H:%M}"
    print(f"real bar history: {len(closes):,} instruments carry closes as of {stamp}")

    bot = RealDataMeanReversionCashBot(closes)
    contexts = (
        SegmentBotContext(
            decision_instant=as_of,
            tradeable_universe=universe,
            regime=RegimeDistribution(MarketRegime.uniform_probabilities()),
            carried_memory={},
        ),
        # The empty-universe context the suite refuses to run without — a real session has quiet
        # moments, and a bot that cannot answer "nothing" takes the day down on one of them.
        SegmentBotContext(
            decision_instant=as_of + timedelta(minutes=5),
            tradeable_universe=(),
            regime=RegimeDistribution(MarketRegime.uniform_probabilities()),
            carried_memory={},
        ),
    )

    violations = run_segment_bot_conformance(bot, contexts)
    proposed = bot.propose(contexts[0])
    print(f"signals proposed on real data: {len(proposed)}")
    print(f"relevance: {bot.relevance(contexts[0]).reason}")
    print(f"maturity rung: {bot.maturity().rung.label} (R.22: never self-graduates)")

    if violations:
        print(f"CONFORMANCE FAILED — {len(violations)} violation(s):", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.describe()}", file=sys.stderr)
        return 1
    print("conformance: a real-engine bot over the real NSE universe satisfies the L5.29 contract")
    # Deliberately NOT claimed: that the engine makes money. That is L5.26's own R.05 pass, and
    # conflating the two is the R.13 error this project exists to avoid.
    assert MeanReversionAction.ABSTAIN  # the abstain path is a first-class decision, not a null
    return 0


if __name__ == "__main__":
    sys.exit(main())
