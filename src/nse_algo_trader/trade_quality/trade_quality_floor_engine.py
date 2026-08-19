"""The gate itself — `L5.31`, spec `docs/research/260`.

One proposal in, one `TradeQualityEvidenceCard` out. `REFUSE` means the proposal does not become an
order.

**Where it sits.** `PreTradeCostGate` (`L1.02`) already asks whether a signal's *stated* edge clears
its priced cost hurdle. It has to trust `PricedSignal.expected_edge_bps` to do that, and the whole
lesson of the retained record is that the stated quantity could not be trusted: mean stated win
probability 0.4585 against a realised 0.3689, and a Brier of 0.2855 — worse than answering "50%"
every time (`docs/research/254`). This engine is what asks whether the claim itself is credible,
given the bot's own record and the breadth of the scan the proposal won. It consumes the cost gate's
priced cost rather than re-pricing it, so costs are computed once by the engine that owns them.

**Segment-blind, and performing no I/O.** The same discipline `L5.29` imposes on the six bots, for
the same reason: a decision that reads nothing at the moment it is made can be replayed exactly, and
six concurrent authors cannot race it. Everything it needs is passed in — the calibrator and the
payoff estimator carry their own fitted state, and the store is written by the caller afterwards.

**There is no whole-record coherence check, and its removal is a measured decision rather than a
retreat.** One was added for `docs/research/261` MAJOR-7 and the re-review killed it: it compared
the modelled expectancy at the proposal's stated value — a CONDITIONAL quantity — against the
bootstrap interval of the bot's whole-record mean, a MARGINAL one. Those coincide only for a
forecaster whose claims carry no information, so the check penalised **resolution**: at a fixed
true edge of +Rs 382/trade it refused 5% of non-discriminating bots and **82%** of highly
discriminating ones, and 100% of good bots once their records passed 400 trades. It also did not
fire on either hostile bot it was written for. A check that is inversely powered in evidence and
anti-correlated with skill is worse than none.

**It refuses rather than falls through.** Every path that cannot produce an estimate yields
`UNASSESSABLE`, never a permissive default. `docs/research/256` found the opposite in the maturity
ladder's first version, where a non-finite posterior fell through to the most permissive rung.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.gross_expectancy_posterior import (
    estimate_gross_expectancy_posterior,
)
from nse_algo_trader.trade_quality.realized_payoff_distribution_estimator import (
    RealisedPayoffDistributionEstimator,
)
from nse_algo_trader.trade_quality.selection_corrected_quality_floor import (
    binding_floor_of,
    priced_cost_floor,
    realised_cost_floor,
    selection_corrected_floor,
)
from nse_algo_trader.trade_quality.stated_probability_calibrator import (
    StatedProbabilityCalibrator,
)
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    CalibratedProbability,
    CalibrationMethod,
    GrossExpectancyPosterior,
    PayoffPosterior,
    QualityFloor,
    QualityVerdict,
    TradeQualityError,
    TradeQualityEvidenceCard,
)

PAISE_PER_RUPEE = Decimal("100")
"""A unit conversion in the currency itself. `PricedSignal` carries paise; floors are rupees."""

COIN_FLIP_CONFIDENCE = 0.5
"""A confidence at or below this admits a coin, so the policy floor sits strictly above it.

The same boundary `LadderPolicy` enforces, and for the same reason.
"""


@dataclass(frozen=True, slots=True)
class QualityFloorPolicy:
    """What the operator requires before a proposal may become an order. Stated, never defaulted.

    `R.03`: this is the only number in the engine that is not measured, and it is policy rather than
    fact — how much of the posterior must sit above the binding floor. It mirrors
    `LadderPolicy.promotion_confidence` deliberately, so the per-trade gate and the per-bot ladder
    are answerable in the same currency.
    """

    admission_confidence: float

    def __post_init__(self) -> None:
        if not COIN_FLIP_CONFIDENCE < self.admission_confidence < 1.0:
            raise TradeQualityError(
                f"admission confidence must sit strictly between 0.5 and 1.0, got "
                f"{self.admission_confidence}; 1.0 is unreachable from finite evidence, and "
                f"anything at or below 0.5 admits a coin"
            )


def stated_win_probability_of(signal: PricedSignal) -> float | None:
    """Read the bot's own claim off the signal, or answer `None` when it made none.

    `PricedSignal.conviction` is where a bot states how strongly it believes its own proposal, and
    it is optional on that type. `None` is propagated rather than replaced by a default, because a
    default here would be this engine inventing the very quantity it exists to audit.
    """
    if signal.conviction is None:
        return None
    conviction = float(signal.conviction)
    if not math.isfinite(conviction) or not 0.0 <= conviction <= 1.0:
        raise TradeQualityError(
            f"{signal.trading_symbol} carries a conviction of {signal.conviction}, which is not a "
            f"probability; a bot that cannot state its belief in [0, 1] cannot be calibrated"
        )
    return conviction


@dataclass(frozen=True, slots=True)
class QualityAssessmentRequest:
    """Everything the gate needs about one proposal, gathered by the caller.

    `candidate_expectancy_fractions` is the scan this proposal won — every candidate the bot
    ranked, not only the survivors, and each scored as a FRACTION of its own notional rather than in
    rupees. Handing over the survivors would understate the breadth and therefore the selection
    correction, which is the one term a wide scanner has an incentive to shrink; handing over rupee
    amounts would make the correction track the universe's share prices, which is the defect this
    engine's first real-data run produced.
    """

    signal: PricedSignal
    bot_identity: str
    trading_segment: TradingSegment
    assessed_at: datetime
    round_trip_cost_rupees: Decimal
    candidate_expectancy_fractions: tuple[Decimal, ...]
    stated_win_probability: float | None

    def __post_init__(self) -> None:
        if not self.bot_identity.strip():
            raise TradeQualityError("a proposal with no bot identity cannot be attributed")
        if self.assessed_at.tzinfo is None:
            raise TradeQualityError(
                f"{self.signal.trading_symbol} was assessed at a naive {self.assessed_at}"
            )
        if not self.candidate_expectancy_fractions:
            raise TradeQualityError(
                f"{self.signal.trading_symbol} arrived with an empty candidate scan; a proposal "
                f"was chosen from at least itself, and an empty scan reports a correction of "
                f"zero for a scan that may have been thousands wide"
            )

    @property
    def notional_rupees(self) -> Decimal:
        return self.signal.notional_paise / PAISE_PER_RUPEE


class TradeQualityFloorEngine:
    """The pre-trade minimum standard, assembled from calibration, payoffs and selection breadth."""

    def __init__(
        self,
        calibrator: StatedProbabilityCalibrator,
        payoff_estimator: RealisedPayoffDistributionEstimator,
        policy: QualityFloorPolicy,
    ) -> None:
        self._calibrator = calibrator
        self._payoffs = payoff_estimator
        self._policy = policy

    def assess(self, request: QualityAssessmentRequest) -> TradeQualityEvidenceCard:
        """Decide, and record every quantity the decision rested on.

        Never raises for a proposal it cannot judge — that is `UNASSESSABLE`, and the caller must
        treat it as a refusal to act rather than as permission.
        """
        floors: list[QualityFloor] = [priced_cost_floor(request.round_trip_cost_rupees)]
        selection = selection_corrected_floor(
            request.candidate_expectancy_fractions,
            proposal_notional_rupees=request.notional_rupees,
        )
        floors.append(selection)

        scale = self._size_scale(request)
        realised_cost = self._payoffs.mean_cost_per_trade_for(request.bot_identity)
        if realised_cost is not None:
            floors.append(
                realised_cost_floor(
                    # Scaled by the SAME factor as the expectancy it gates. `docs/research/261`
                    # MAJOR-C: this floor was in unscaled record units while the expectancy was
                    # scaled, so the cross-check that exists to catch an under-priced cost model was
                    # defeated by any proposal larger than the bot's median — the floor stayed put
                    # while the expectancy grew, and P climbed on size alone.
                    realised_cost if scale is None else realised_cost * scale,
                    len(self._payoffs.gross_outcomes_for(request.bot_identity)),
                )
            )
        binding = binding_floor_of(floors)

        calibrated = self._calibrated_probability(request)
        payoff = self._payoffs.posterior_for(request.bot_identity)
        wins = self._scaled(self._payoffs.win_magnitudes_for(request.bot_identity), scale)
        losses = self._scaled(self._payoffs.loss_magnitudes_for(request.bot_identity), scale)
        expectancy = (
            None
            if calibrated is None
            else estimate_gross_expectancy_posterior(
                calibrated=calibrated,
                win_magnitudes=wins,
                loss_magnitudes=losses,
                floor_rupees=binding.rupees,
                scratch_share_among_non_wins=self._payoffs.scratch_share_among_non_wins_for(
                    request.bot_identity
                ),
            )
        )

        if calibrated is None or payoff is None or expectancy is None:
            return self._unassessable(request, floors, calibrated, payoff, expectancy, scale)

        clears = expectancy.probability_exceeding_floor >= self._policy.admission_confidence
        verdict = QualityVerdict.ADMIT if clears else QualityVerdict.REFUSE
        return TradeQualityEvidenceCard(
            assessed_at=request.assessed_at,
            bot_identity=request.bot_identity,
            trading_segment=request.trading_segment,
            instrument_token=request.signal.instrument_token,
            trading_symbol=request.signal.trading_symbol,
            proposed_quantity=request.signal.proposed_quantity,
            scan_breadth=len(request.candidate_expectancy_fractions),
            calibrated_probability=calibrated,
            payoff=payoff,
            gross_expectancy=expectancy,
            floors=tuple(floors),
            verdict=verdict,
            reason=self._reason(clears, binding, expectancy, calibrated, scale, self._policy),
        )

    def _calibrated_probability(
        self, request: QualityAssessmentRequest
    ) -> CalibratedProbability | None:
        """Correct the bot's stated belief, or answer `None` when it stated none."""
        if request.stated_win_probability is None:
            return None
        return self._calibrator.calibrate(
            request.stated_win_probability,
            bot_identity=request.bot_identity,
            trading_segment=request.trading_segment,
        )

    def _size_scale(self, request: QualityAssessmentRequest) -> Decimal | None:
        """How much larger this proposal is than the trades that produced the payoff record.

        A payoff distribution earned on ₹50,000 positions says nothing directly about a ₹5,00,000
        one, and comparing an unscaled expectancy against a cost floor priced for the LARGER trade
        would refuse good proposals and admit bad ones depending only on size. `None` when the
        record carries no notionals, in which case the card says so rather than assuming parity.

        **Capped at the largest size this bot has actually traded** (`docs/research/261` MAJOR-C).
        Uncapped, a ₹300 win observed on a ₹2.5-lakh position became a ₹3,00,000 expectancy on a
        ₹25-crore one, with no market-impact term anywhere: qty 100 REFUSED at P=0.0043 and qty 200
        ADMITTED at P=0.9430 on size alone. Linear scaling is a claim that the edge survives the
        size, and beyond what a bot has ever traded that claim has no evidence behind it. The cap is
        the record's own maximum notional over its median — derived, not chosen (`R.03`) — so a bot
        earns the right to size up by having done it.
        """
        median = self._payoffs.median_notional_for(request.bot_identity)
        if median is None or median <= 0:
            return None
        largest = self._payoffs.largest_notional_for(request.bot_identity)
        proposed = request.notional_rupees / median
        if largest is None or largest <= 0:
            return proposed
        return min(proposed, largest / median)

    @staticmethod
    def _scaled(magnitudes: Sequence[Decimal], scale: Decimal | None) -> tuple[Decimal, ...]:
        if scale is None:
            return tuple(magnitudes)
        return tuple(magnitude * scale for magnitude in magnitudes)

    def _unassessable(
        self,
        request: QualityAssessmentRequest,
        floors: list[QualityFloor],
        calibrated: CalibratedProbability | None,
        payoff: PayoffPosterior | None,
        expectancy: GrossExpectancyPosterior | None,
        scale: Decimal | None,
    ) -> TradeQualityEvidenceCard:
        """A card that records an ABSENCE of judgement, and names exactly what was missing.

        It is still written, and still carries its floors. A refusal for want of evidence is the
        most informative record this engine produces — it is what tells the operator which bot needs
        a track record rather than a better strategy.
        """
        missing: list[str] = []
        if calibrated is None:
            missing.append("the bot stated no win probability, so there was nothing to calibrate")
        if payoff is None:
            missing.append(
                "the bot has not yet both won and lost, so its payoff ratio is unmeasured rather "
                "than favourable"
            )
        if expectancy is None and calibrated is not None:
            missing.append("its payoff record has no magnitudes to resample")
        placeholder = calibrated or CalibratedProbability(
            stated=0.0,
            calibrated=0.0,
            method=CalibrationMethod.UNCALIBRATED,
            fitted_on_trades=0,
            brier_reliability=None,
            brier_resolution=None,
            brier_uncertainty=None,
        )
        return TradeQualityEvidenceCard(
            assessed_at=request.assessed_at,
            bot_identity=request.bot_identity,
            trading_segment=request.trading_segment,
            instrument_token=request.signal.instrument_token,
            trading_symbol=request.signal.trading_symbol,
            proposed_quantity=request.signal.proposed_quantity,
            scan_breadth=len(request.candidate_expectancy_fractions),
            calibrated_probability=placeholder,
            payoff=payoff,
            gross_expectancy=expectancy,
            floors=tuple(floors),
            verdict=QualityVerdict.UNASSESSABLE,
            reason=(
                f"no verdict is available: {'; '.join(missing)}. Scaling onto this proposal's size "
                f"was {'not possible' if scale is None else f'x{scale}'}"
            ),
        )

    @staticmethod
    def _reason(
        clears: bool,
        binding: QualityFloor,
        expectancy: GrossExpectancyPosterior,
        calibrated: CalibratedProbability,
        scale: Decimal | None,
        policy: QualityFloorPolicy,
    ) -> str:
        movement = (
            "was not rescaled (the record carries no notionals)"
            if scale is None
            else f"was rescaled x{scale} onto this proposal's size"
        )
        clause = "clears" if clears else "does not clear"
        return (
            f"P(gross expectancy > the binding {binding.derivation.value} floor of Rs "
            f"{binding.rupees}) is {expectancy.probability_exceeding_floor:.4f}, which {clause} "
            f"the stated admission confidence of {policy.admission_confidence:.3f}; the "
            f"stated probability of {calibrated.stated:.3f} calibrated to "
            f"{calibrated.calibrated:.3f} by {calibrated.method.value} on "
            f"{calibrated.fitted_on_trades} trades, and the payoff record {movement}"
        )

