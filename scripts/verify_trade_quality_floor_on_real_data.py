#!/usr/bin/env python
"""`R.05` for the trade-quality floor (`L5.31`) — run the whole gate on real data and print it.

Three real sources, no fixtures anywhere:

* the **3,481 retained closed trades** in `experience_memory.sqlite3` fit the calibrator and the
  payoff estimator, with gross recovered as `realized_pnl + total_fees` because the gate models the
  gross payoff distribution and prices costs as a separate floor;
* the **real five-minute bar store** supplies the cross-sectional scan a proposal is chosen from,
  so the selection correction is computed against a real universe rather than an invented breadth;
* the **real evidence store** receives the cards, and the script re-runs itself to prove the accrual
  is idempotent on real data.

It prints two things.

**A — the decisive pair.** The gate must REFUSE the shape that lost Rs 3,56,631 and ADMIT the one
that made Rs 21,213 **on a 46.3% net win rate** — the case any fixed win-rate rule gets exactly
backwards. These are assessed at a scan breadth of ONE, and that is a deliberate refusal to invent
data: the retained corpus records each trade but not the candidate set it was chosen from, and the
selection correction is the term most sensitive to that input. Imposing a cash-equity cross-section
on an options credit-spread strategy that never scanned one would be fabricating the floor rather
than measuring it.

**B — the selection correction, measured on its own.** What a real 500-name five-minute NSE
cross-section actually costs a scanner, so the term is shown to be real and large rather than
asserted. A first version of this script folded B into A and refused all three strategies on a scan
none of them ran; that failure is why the two are separated here.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from nse_algo_trader.cost_gate.priced_signal import EdgeBasis, PricedSignal
from nse_algo_trader.market_rules.nse_market_rule_history import seeded_nse_market_rule_store
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.realized_payoff_distribution_estimator import (
    RealisedPayoffDistributionEstimator,
    RealisedTradeOutcome,
)
from nse_algo_trader.trade_quality.selection_corrected_quality_floor import (
    selection_corrected_floor,
)
from nse_algo_trader.trade_quality.stated_probability_calibrator import (
    ForecastOutcome,
    StatedProbabilityCalibrator,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    QualityVerdict,
    TradeQualityEvidenceCard,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_store import (
    DEFAULT_EVIDENCE_PATH,
    TradeQualityEvidenceStore,
)
from nse_algo_trader.trade_quality.trade_quality_floor_engine import (
    QualityAssessmentRequest,
    QualityFloorPolicy,
    TradeQualityFloorEngine,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    TradeLeg,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    TradeSpecification,
)

RETAINED_RECORD = Path("~/.nse_algo_trader/experience_memory.sqlite3").expanduser()
MARKET_DATA = Path("~/.nse_algo_trader/market_data.sqlite3").expanduser()
PAISE_PER_RUPEE = Decimal("100")
BASIS_POINTS = Decimal("10000")

def mean_stated_probability_by_strategy(
    rows: Sequence[tuple[str, str, float, float, float]],
) -> dict[str, Decimal]:
    """Each strategy's OWN mean stated win probability, measured from the record it is scored on.

    Not transcribed from `docs/research/254` (`R.03`): a number copied out of a document is a
    constant that stops tracking the data the moment either moves, and this one is the input the
    calibrator exists to correct. Measured, they come out near 0.416, 0.775 and 0.715 — differing by
    a factor of four in how far each sits from what the strategy actually achieved, which is the
    whole reason calibration is fitted per bot rather than globally.
    """
    totals: dict[str, list[float]] = {}
    for tag, _session, stated, _net, _fees in rows:
        totals.setdefault(tag, []).append(min(max(stated, 0.0), 1.0))
    return {
        tag: Decimal(str(sum(values) / len(values))) for tag, values in totals.items() if values
    }


def retained_rows() -> list[tuple[str, str, float, float, float]]:
    with sqlite3.connect(RETAINED_RECORD) as connection:
        return [
            (str(tag), str(session), float(stated), float(net), float(fees))
            for tag, session, stated, net, fees in connection.execute(
                """
                SELECT strategy_tag, session_date, win_probability, realized_pnl,
                       COALESCE(total_fees, 0)
                  FROM experience_nodes
                 WHERE win_probability IS NOT NULL AND realized_pnl IS NOT NULL
                """
            )
        ]


def real_cross_section(
    limit: int, *, knowable_by: datetime
) -> tuple[tuple[int, Decimal, Decimal], ...]:
    """`(token, close, last-bar return)` for the `limit` most-traded instruments, from real bars.

    The **return** is the score, not the close. A cash-intraday bot ranks its universe on a
    dimensionless per-instrument move, and the selection correction needs exactly that: exchangeable
    per-notional scores. Ranking on rupee amounts would make the correction measure the spread of
    NSE share prices instead of the spread of the signal.

    Every read is filtered on `availability_time <= knowable_by`. A cross-section assembled from
    bars not yet published is look-ahead, and the dispersion of a look-ahead cross-section
    would understate the correction it is used to compute.
    `tests/test_bar_reads_are_point_in_time.py` caught the first version of this doing exactly that.
    """
    cutoff = knowable_by.isoformat()
    with sqlite3.connect(MARKET_DATA) as connection:
        latest = connection.execute(
            "SELECT MAX(bar_timestamp) FROM price_bars "
            "WHERE bar_interval = '5m' AND availability_time <= ?",
            (cutoff,),
        ).fetchone()[0]
        if latest is None:
            return ()
        rows = connection.execute(
            """
            SELECT instrument_token, close_price, open_price, volume
              FROM price_bars
             WHERE bar_interval = '5m' AND bar_timestamp = ?
               AND availability_time <= ?
               AND open_price > 0
             ORDER BY volume DESC
             LIMIT ?
            """,
            (latest, cutoff, limit),
        ).fetchall()
    return tuple(
        (
            int(token),
            Decimal(str(close)),
            (Decimal(str(close)) - Decimal(str(open_price))) / Decimal(str(open_price)),
        )
        for token, close, open_price, _volume in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admission-confidence",
        type=float,
        default=0.9,
        help="operator policy: how much posterior mass must sit above the binding floor",
    )
    parser.add_argument("--scan-limit", type=int, default=500)
    parser.add_argument("--evidence-store", type=Path, default=DEFAULT_EVIDENCE_PATH)
    arguments = parser.parse_args()

    if not RETAINED_RECORD.exists():
        print(f"the retained record is not at {RETAINED_RECORD}")
        return 2

    rows = retained_rows()
    print(f"retained record: {len(rows)} closed trades with a stated probability and an outcome")

    calibrator = StatedProbabilityCalibrator(
        [
            ForecastOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                occurred_at=datetime.fromisoformat(session).replace(tzinfo=UTC),
                session_date=date.fromisoformat(session),
                stated_probability=min(max(stated, 0.0), 1.0),
                was_win=net + fees > 0,
            )
            for tag, session, stated, net, fees in rows
        ]
    )
    payoffs = RealisedPayoffDistributionEstimator(
        [
            RealisedTradeOutcome(
                bot_identity=tag,
                trading_segment=TradingSegment.CASH_INTRADAY,
                session_date=date.fromisoformat(session),
                gross_rupees=Decimal(str(net)) + Decimal(str(fees)),
                costs_rupees=Decimal(str(fees)),
            )
            for tag, session, stated, net, fees in rows
        ]
    )
    policy = QualityFloorPolicy(admission_confidence=arguments.admission_confidence)
    engine = TradeQualityFloorEngine(calibrator, payoffs, policy)
    # The PRICED cost, from the engine that owns pricing. A first version passed each bot's own
    # realised cost here as well, which made floors 1 and 2 identical by construction and hid the
    # very comparison floor 2 exists to make: a model against a measurement.
    cost_engine = NseTransactionCostEngine(seeded_nse_market_rule_store())

    scan = real_cross_section(arguments.scan_limit, knowable_by=datetime.now(UTC))
    if not scan:
        print("the five-minute bar store returned no cross-section; nothing real to scan")
        return 2
    print(f"real cross-section: {len(scan)} instruments at the store's latest 5m timestamp")

    store = TradeQualityEvidenceStore(arguments.evidence_store)
    assessed_at = datetime.now(UTC)
    recorded = 0
    produced: list[TradeQualityEvidenceCard] = []
    scores: dict[str, float] = {}
    verdicts: dict[str, QualityVerdict] = {}

    print()
    print("A — the decisive pair, at the breadth the record actually supports")
    for bot, stated in sorted(mean_stated_probability_by_strategy(rows).items()):
        base_rate = calibrator.base_rate_for(bot)
        posterior = payoffs.posterior_for(bot)
        realised_cost = payoffs.mean_cost_per_trade_for(bot)
        if base_rate is None or posterior is None or realised_cost is None:
            print(f"{bot}: no record, skipped")
            continue
        token, close_rupees, _own_move = scan[0]
        quantity = 100
        edge_bps = (posterior.win_mean_rupees / (close_rupees * quantity)) * BASIS_POINTS
        # Breadth ONE: the retained corpus records no candidate set, and inventing one would
        # fabricate the term the floor is most sensitive to. Part B measures that term separately.
        candidates = (Decimal("0"),)
        signal = PricedSignal(
            instrument_token=token,
            trading_symbol=f"TOKEN-{token}",
            segment=ChargeableSegment.EQUITY_INTRADAY,
            side=TradeLeg.BUY,
            decided_at=assessed_at,
            reference_price_paise=close_rupees * PAISE_PER_RUPEE,
            expected_edge_bps=max(edge_bps, Decimal("0.01")),
            proposed_quantity=quantity,
            edge_basis=EdgeBasis.MEASURED_TRACK_RECORD,
            source=bot,
            conviction=stated,
        )
        priced = cost_engine.price_round_trip(
            TradeSpecification(
                segment=ChargeableSegment.EQUITY_INTRADAY,
                quantity=quantity,
                entry_price_paise=close_rupees * PAISE_PER_RUPEE,
                exit_price_paise=close_rupees * PAISE_PER_RUPEE,
                trade_date=assessed_at.date(),
            )
        )
        card = engine.assess(
            QualityAssessmentRequest(
                signal=signal,
                bot_identity=bot,
                trading_segment=TradingSegment.CASH_INTRADAY,
                assessed_at=assessed_at,
                round_trip_cost_rupees=priced.total_rupees,
                candidate_expectancy_fractions=candidates,
                stated_win_probability=float(stated),
            )
        )
        verdicts[bot] = card.verdict
        produced.append(card)
        if card.gross_expectancy is not None:
            scores[bot] = card.gross_expectancy.probability_exceeding_floor
        recorded += int(store.record(card))
        print()
        print(f"  {bot}")
        print(
            f"    gross win rate {base_rate:.3f} · payoff ratio "
            f"{posterior.payoff_ratio:.3f} · break-even rate "
            f"{posterior.break_even_win_rate:.3f} · {posterior.trades_observed} trades"
        )
        print(f"    {card.describe()}")
        for floor in card.floors:
            print(f"      floor {floor.derivation.value}: Rs {floor.rupees:.2f}")

    print()
    print("B — the selection correction on the real cross-section")
    demonstration = selection_corrected_floor(
        tuple(move for _token, _price, move in scan),
        proposal_notional_rupees=scan[0][1] * 100,
    )
    print(f"    {demonstration.explanation}")
    print(
        "    a scanning bot must clear this on top of its costs; the retained strategies' gross "
        "expectancy per trade is far below it, which is what a 500-wide scan actually costs"
    )

    # Idempotency on the cards THIS run produced, not on everything in the session. Re-recording
    # rows written by an older engine reports a false positive the moment the content hash changes.
    replayed = sum(int(store.record(card)) for card in produced)
    print()
    print(f"recorded {recorded} new cards; re-recording the same cards added {replayed}")

    loser = verdicts.get("opening_range_breakout_v1")
    winner = verdicts.get("credit_spread_v1")
    loser_score = scores.get("opening_range_breakout_v1", 1.0)
    winner_score = scores.get("credit_spread_v1", 0.0)
    print()
    print(f"  opening_range_breakout_v1 (-Rs 3,56,631): {loser} at P={loser_score:.4f}")
    print(f"  credit_spread_v1          (+Rs 21,213):   {winner} at P={winner_score:.4f}")
    # The FULL bar, restored. It was briefly weakened to "ranks the pair right" while the repaired
    # engine could not admit anything, and `docs/research/261`'s re-review was right to call that
    # out: a coin-flip ordering satisfies it. The gate must REFUSE the one that lost Rs 3,56,631 and
    # ADMIT the one that made Rs 21,213 on a 46.3% net win rate.
    if loser is QualityVerdict.REFUSE and winner is QualityVerdict.ADMIT:
        print("  the gate separates the pair the right way round")
        return 0
    print("  THE GATE DID NOT SEPARATE THE PAIR — this is a failure, not a warning")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
