"""The per-decision record this gate leaves behind — `L5.31`, spec `docs/research/260`.

**Why the record comes before the gate.** The system this project replaces took 3,481 trades and
lost ₹3.31 lakh (`docs/research/254`), and the reason it could keep doing that for ten sessions is
that nothing wrote down *why* each trade had looked worth taking. A verdict with no
reconstructable basis cannot be audited, cannot be argued with, and — the part that matters here —
cannot ever be found to have been WRONG. So the card is written on every assessment, `ADMIT` and
`REFUSE` alike: a record of refusals is the only way this engine can ever discover that its own
floor is too high.

**Every quantity that moved the verdict is on the card**, not a summary of them: the stated
probability AND the calibrated one AND how many trades the calibration rests on; the payoff
posterior with its credible bounds rather than two point means; each candidate floor with the name
of its derivation; and which one actually bound. `A.29` requires that reasoning be reconstructable
afterwards, and a card that carries only the answer does not satisfy it.

Money is `Decimal` end to end (`R.03`); probabilities are `float`, because they are estimates rather
than currency and no rupee ever rounds through them.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment

CERTAIN_PROBABILITY = 1.0
"""A probability at or above this asserts an outcome cannot fail, which no estimate may claim."""

IMPOSSIBLE_PROBABILITY = 0.0
"""A probability at or below this asserts an outcome cannot happen, likewise unavailable."""


class TradeQualityError(Exception):
    """The assessment, or the record of one, cannot be honoured as stated."""


class QualityVerdict(StrEnum):
    """What the floor decided about one proposal.

    The three-way split is deliberate and mirrors `GateVerdict` in the cost gate, for the same
    reason it gives: `REFUSE` is a judgement — the proposal was assessed and does not clear its
    floor. `UNASSESSABLE` is the *absence* of a judgement — something needed could not be estimated.
    Collapsing them lets a caller read "I have no opinion" as "it is fine", which is the direction
    that costs money.
    """

    ADMIT = "admit"
    REFUSE = "refuse"
    UNASSESSABLE = "unassessable"


class FloorDerivation(StrEnum):
    """How a candidate floor was arrived at. Named, because the binding one goes on the card.

    There is no `OPERATOR_CONSTANT` member and there will not be one: a floor this engine cannot
    derive is a floor it does not apply (`R.03`).
    """

    PRICED_ROUND_TRIP_COST = "priced_round_trip_cost"
    REALISED_COST_PER_TRADE = "realised_cost_per_trade"
    SELECTION_CORRECTED_NULL = "selection_corrected_null"


class CalibrationMethod(StrEnum):
    """Which estimator produced the calibrated probability, and on whose record.

    The fallback chain is a `R.04` maturity ladder over *estimators*, not a reduced algorithm: a bot
    with no record of its own is calibrated against its segment, and one whose segment is also empty
    is held to the pooled base rate. What thin data changes is which parent the estimate borrows
    from — never whether the estimate is made.
    """

    ISOTONIC_BINNED = "isotonic_binned"
    """Isotonic over equal-count BINS of the bot's own record.

    Named for what it is. It was called `isotonic_out_of_fold` and was neither: the fit used the
    bot's whole record in sample and interpolated every point exactly, so a bot with no skill
    calibrated to 0.9993 at its top stated value (`docs/research/261` CRITICAL-1). Binning is what
    makes the estimate honest; the out-of-fold construction lives in the Brier diagnostic, which is
    a different question and does not touch a verdict.
    """
    ISOTONIC_IN_SAMPLE_RETIRED = "isotonic_out_of_fold"
    """Cards written before `docs/research/261`, kept readable and named for what they were.

    The stored string is unchanged because the rows are unchanged — the store is append-only and
    rewriting history to match a corrected engine is exactly what it exists to prevent. The MEMBER
    is renamed because the old name was a false claim: that fit used the bot's whole record in
    sample. Nothing emits this; it exists so a card from before the repair still reads back, and
    reads back as suspect.
    """

    SHRUNK_TO_SEGMENT = "shrunk_to_segment"
    POOLED_BASE_RATE = "pooled_base_rate"
    UNCALIBRATED = "uncalibrated"


def _require_probability(value: float, field: str) -> None:
    """Refuse a probability that is not one. `UNASSESSABLE` exists for the unknown case."""
    if not math.isfinite(value):
        raise TradeQualityError(
            f"{field} is {value!r}, which is not finite. A non-finite probability propagates into "
            f"the expectancy posterior and every comparison against a floor then evaluates False, "
            f"which reads as 'clears nothing' in one branch and 'refuses nothing' in another"
        )
    if not IMPOSSIBLE_PROBABILITY <= value <= CERTAIN_PROBABILITY:
        raise TradeQualityError(f"{field} must lie in [0, 1], got {value}")


@dataclass(frozen=True, slots=True)
class CalibratedProbability:
    """A bot's stated win probability, and what its own record says that claim is worth.

    `docs/research/254` measured the three retained strategies' overconfidence at 6.6, 26.1 and 25.2
    percentage points — a factor of four apart — which is why calibration is fitted per bot and why
    `fitted_on_trades` is carried: a correction resting on eleven outcomes and one resting on eleven
    hundred are not the same claim, and the card must not present them as one.

    The Brier decomposition is Murphy's: `Brier = reliability - resolution + uncertainty`. It is the
    diagnostic that makes "the probabilities carried negative information" a measurement rather than
    an impression — a forecaster with reliability above resolution scores worse than the constant
    base rate, which is exactly what the retained record's 0.2855 against a 0.25 baseline says.
    """

    stated: float
    calibrated: float
    method: CalibrationMethod
    fitted_on_trades: int
    brier_reliability: float | None
    brier_resolution: float | None
    brier_uncertainty: float | None

    def __post_init__(self) -> None:
        _require_probability(self.stated, "stated probability")
        _require_probability(self.calibrated, "calibrated probability")
        if self.fitted_on_trades < 0:
            raise TradeQualityError(
                f"a calibration cannot rest on {self.fitted_on_trades} trades"
            )
        if self.method is CalibrationMethod.UNCALIBRATED and self.fitted_on_trades:
            raise TradeQualityError(
                f"method is {self.method.value} yet claims {self.fitted_on_trades} trades behind "
                f"it; an uncalibrated estimate rests on none, and reporting a count against it "
                f"lends the raw stated probability evidence it does not have"
            )

    @property
    def overconfidence(self) -> float:
        """How many probability points the bot claimed beyond what its record supports.

        Positive is optimistic — the direction every retained strategy ran, and the direction that
        turns a losing edge into one that looks tradeable.
        """
        return self.stated - self.calibrated

    @property
    def carries_negative_information(self) -> bool:
        """True when the forecaster scores worse than always saying the base rate.

        In Murphy's decomposition that is `reliability > resolution`. Unknown decomposition is not
        evidence of a good forecaster, so it answers False rather than raising: the *floor* is what
        refuses trades, not this diagnostic.
        """
        if self.brier_reliability is None or self.brier_resolution is None:
            return False
        return self.brier_reliability > self.brier_resolution


@dataclass(frozen=True, slots=True)
class PayoffPosterior:
    """What this bot's own wins and losses are worth, as distributions rather than two means.

    The point means are what a thin version would carry, and they are precisely what hides the whole
    finding: `credit_spread_v1` won 46.3% of its trades — a losing rate under any fixed rule — and
    made ₹21,213, because its wins were 3.45x its losses. `opening_range_breakout_v1` won 35.0% at a
    ratio of 0.867 and lost ₹356,631. The two are separated by the *ratio*, and a ratio of two
    estimates has an uncertainty that a ratio of two point estimates cannot express.

    Magnitudes are net of costs and positive on both sides; the sign lives in the field name.
    Scratch trades — exactly zero net — are counted in neither, because averaging them into the loss
    magnitude drags it toward zero and lowers break-even without a single rupee changing hands
    (`bot_maturity_ladder._break_even_win_rate` documents the 200-scratch case that moved it from
    50.0% to 8.3%).
    """

    win_mean_rupees: Decimal
    win_lower_rupees: Decimal
    win_upper_rupees: Decimal
    loss_mean_rupees: Decimal
    loss_lower_rupees: Decimal
    loss_upper_rupees: Decimal
    wins_observed: int
    losses_observed: int
    credible_mass: float

    def __post_init__(self) -> None:
        for name, value in (
            ("win_mean_rupees", self.win_mean_rupees),
            ("loss_mean_rupees", self.loss_mean_rupees),
            ("win_lower_rupees", self.win_lower_rupees),
            ("loss_lower_rupees", self.loss_lower_rupees),
        ):
            if value < 0:
                raise TradeQualityError(
                    f"{name} is {value}; both sides are carried as MAGNITUDES and a negative one "
                    f"silently flips the payoff ratio, turning a loser into a winner"
                )
        if self.win_lower_rupees > self.win_upper_rupees:
            raise TradeQualityError("the win credible interval is inverted")
        if self.loss_lower_rupees > self.loss_upper_rupees:
            raise TradeQualityError("the loss credible interval is inverted")
        if self.wins_observed < 0 or self.losses_observed < 0:
            raise TradeQualityError("an observation count cannot be negative")
        _require_probability(self.credible_mass, "credible mass")

    @property
    def trades_observed(self) -> int:
        return self.wins_observed + self.losses_observed

    @property
    def payoff_ratio(self) -> float | None:
        """Average win over average loss. `None` when the bot has never lost — not infinity.

        A bot with no losses has not demonstrated an infinite payoff ratio; it has demonstrated an
        unmeasured one, and the difference is the whole `UNASSESSABLE` verdict.
        """
        if self.loss_mean_rupees <= 0:
            return None
        return float(self.win_mean_rupees / self.loss_mean_rupees)

    @property
    def break_even_win_rate(self) -> float | None:
        """`1/(1+ratio)` — the rate this bot's payoffs require, carried as EVIDENCE only.

        53.6% for `opening_range_breakout_v1` against an achieved 35.0%; 22.5% for
        `credit_spread_v1` against an achieved 46.3%. It explains the verdict on the card and it
        does NOT compute it: `docs/research/256` broke the ladder's first promotion rule for
        comparing a plug-in break-even against a plug-in rate estimated from the same sample, which
        is non-monotonic in both directions and blind to magnitude concentration. The floor itself
        is a rupee quantity taken from the posterior, in `selection_corrected_quality_floor`.
        """
        ratio = self.payoff_ratio
        if ratio is None:
            return None
        return 1.0 / (1.0 + ratio)


@dataclass(frozen=True, slots=True)
class GrossExpectancyPosterior:
    """The posterior over this proposal's expected rupees per trade, BEFORE its own costs.

    **Gross on purpose.** Costs are not netted off here because the priced round-trip cost is itself
    one of the floors this quantity must clear, and a cost subtracted inside the posterior AND
    applied again as a floor would be charged twice. Keeping it gross also makes the three floors
    directly comparable in the same unit, which is what lets the card name the one that actually
    bound.

    **The verdict is a posterior probability, not a bound against a bound.** `probability_exceeding_
    floor` is `P(gross expectancy > the binding floor)` under this posterior, and it is what the
    admission confidence is compared against. An earlier version compared the lower credible bound
    against a floor that was itself a dispersion half-width, which charged the same uncertainty
    twice — structurally the same defect as netting costs off the magnitudes AND applying a cost
    floor, and caught by the same real-data run. Uncertainty is charged once, here.

    This is deliberately the same statistic `bot_maturity_ladder` promotes on, so the gate and the
    ladder cannot disagree about what "the evidence supports this" means. It is also what makes thin
    evidence self-limiting without a separate rule (`R.04`): eleven trades and eleven hundred can
    carry the same mean, and only the posterior mass knows the difference.
    """

    mean_rupees: Decimal
    lower_rupees: Decimal
    upper_rupees: Decimal
    credible_mass: float
    draws: int
    floor_rupees: Decimal
    probability_exceeding_floor: float

    def __post_init__(self) -> None:
        if self.lower_rupees > self.upper_rupees:
            raise TradeQualityError(
                f"the expectancy credible interval is inverted: [{self.lower_rupees}, "
                f"{self.upper_rupees}]"
            )
        if not self.lower_rupees <= self.mean_rupees <= self.upper_rupees:
            raise TradeQualityError(
                f"the expectancy mean {self.mean_rupees} lies outside its own interval "
                f"[{self.lower_rupees}, {self.upper_rupees}]"
            )
        _require_probability(self.credible_mass, "credible mass")
        _require_probability(self.probability_exceeding_floor, "probability exceeding the floor")
        if self.draws <= 0:
            raise TradeQualityError("a posterior with no draws is not a posterior")


@dataclass(frozen=True, slots=True)
class QualityFloor:
    """One candidate minimum, in rupees, with the name of what derived it.

    Kept as a value rather than a bare number so the *binding* one can be named on the card. A floor
    that never binds is a floor doing nothing, and only a record of which one bound each time can
    show that.
    """

    derivation: FloorDerivation
    rupees: Decimal
    explanation: str

    def __post_init__(self) -> None:
        if not self.explanation.strip():
            raise TradeQualityError(
                f"the {self.derivation.value} floor carries no explanation; an unexplained "
                f"threshold is the R.03 defect whether or not it was computed"
            )


@dataclass(frozen=True, slots=True)
class TradeQualityEvidenceCard:
    """One proposal, everything weighed against it, and the verdict — `L5.31`.

    Written for refusals too. The store keys on `content_hash`, so re-running a session re-records
    nothing and the accrual is idempotent on real data rather than on a fixture.
    """

    assessed_at: datetime
    bot_identity: str
    trading_segment: TradingSegment
    instrument_token: int
    trading_symbol: str
    proposed_quantity: int
    scan_breadth: int
    calibrated_probability: CalibratedProbability
    payoff: PayoffPosterior | None
    gross_expectancy: GrossExpectancyPosterior | None
    floors: tuple[QualityFloor, ...]
    verdict: QualityVerdict
    reason: str

    def __post_init__(self) -> None:
        if not self.bot_identity.strip():
            raise TradeQualityError("a card with no bot identity attributes its verdict to nobody")
        if self.assessed_at.tzinfo is None:
            raise TradeQualityError(
                f"{self.trading_symbol} was assessed at a naive {self.assessed_at}; a card that "
                f"cannot say which instant it describes cannot be replayed against the book of "
                f"that instant"
            )
        if self.proposed_quantity <= 0:
            raise TradeQualityError(
                f"proposed quantity must be positive, got {self.proposed_quantity}"
            )
        if self.scan_breadth < 1:
            raise TradeQualityError(
                f"scan breadth is {self.scan_breadth}; a proposal that was chosen was chosen from "
                f"at least one candidate, and a breadth below one collapses the selection "
                f"correction to a division by log(1)"
            )
        if self.proposed_quantity > 0 and not self.floors:
            raise TradeQualityError(
                f"{self.trading_symbol} carries a {self.verdict.value} verdict against no floor at "
                f"all; an admission that cleared nothing is indistinguishable from an ungated trade"
            )
        if not self.reason.strip():
            raise TradeQualityError("a verdict with no stated reason is not reconstructable")
        decided = self.verdict is not QualityVerdict.UNASSESSABLE
        if decided and (self.payoff is None or self.gross_expectancy is None):
            raise TradeQualityError(
                f"{self.trading_symbol} carries a {self.verdict.value} verdict with no payoff or "
                f"expectancy posterior behind it. Those two are the entire basis of a decision "
                f"here, and a card asserting one without them is a verdict reached by some other "
                f"route — which is precisely what UNASSESSABLE exists to say instead"
            )

    @property
    def binding_floor(self) -> QualityFloor:
        """The highest candidate floor — the one the proposal actually had to clear."""
        return max(self.floors, key=lambda floor: floor.rupees)

    @property
    def margin_rupees(self) -> Decimal | None:
        """Mean gross expectancy less the binding floor. Reported as scale, not as the test.

        The verdict is `gross_expectancy.probability_exceeding_floor` against the stated admission
        confidence; this is the rupee size of the gap, which is what a reader wants to see next to
        it. `None` on an `UNASSESSABLE` card, where there is no expectancy to take a margin from.
        """
        if self.gross_expectancy is None:
            return None
        return self.gross_expectancy.mean_rupees - self.binding_floor.rupees

    @property
    def content_hash(self) -> str:
        """A stable digest of what this card asserts, for idempotent recording.

        Built from an explicitly ordered tuple rather than `dataclasses.asdict`, so adding a field
        below cannot silently change the identity of cards already stored. `assessed_at` is inside
        the digest: the same proposal assessed at two instants is two decisions, because the book
        and the record behind it moved between them.
        """
        parts = (
            self.assessed_at.isoformat(),
            self.bot_identity,
            self.trading_segment.value,
            str(self.instrument_token),
            self.trading_symbol,
            str(self.proposed_quantity),
            str(self.scan_breadth),
            # EVERY floor, not just the binding one. `docs/research/261` MAJOR-4: two assessments
            # differing only in a NON-binding floor hashed identically, so the second `record()`
            # returned False — indistinguishable from an idempotent re-run, and the card was lost.
            "|".join(
                f"{floor.derivation.value}={floor.rupees}" for floor in sorted(
                    self.floors, key=lambda item: item.derivation.value
                )
            ),
            f"{self.calibrated_probability.stated:.10f}",
            f"{self.calibrated_probability.calibrated:.10f}",
            "unassessable"
            if self.gross_expectancy is None
            else f"{self.gross_expectancy.probability_exceeding_floor:.6f}",
            str(self.binding_floor.rupees),
            self.binding_floor.derivation.value,
            self.verdict.value,
            "none" if self.payoff is None else str(self.payoff.trades_observed),
            "none"
            if self.gross_expectancy is None
            else f"{self.gross_expectancy.mean_rupees}/{self.gross_expectancy.draws}",
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def describe(self) -> str:
        floor = self.binding_floor
        confidence = (
            "none"
            if self.gross_expectancy is None
            else f"{self.gross_expectancy.probability_exceeding_floor:.3f}"
        )
        return (
            f"{self.verdict.value.upper()} {self.trading_symbol} for {self.bot_identity}: "
            f"P(expectancy > floor) = {confidence} against the "
            f"{floor.derivation.value} floor of Rs {floor.rupees} "
            f"(margin Rs {self.margin_rupees}); stated p {self.calibrated_probability.stated:.3f} "
            f"calibrated to {self.calibrated_probability.calibrated:.3f} on "
            f"{self.calibrated_probability.fitted_on_trades} trades; chosen from "
            f"{self.scan_breadth} candidates — {self.reason}"
        )
