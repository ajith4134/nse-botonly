"""What this bot's wins and losses are worth — `L5.31`, spec `docs/research/260`.

**The finding this estimator has to be able to see.** Two of the three retained strategies had
losing win rates. One of them made money:

    credit_spread_v1           46.3% win  win Rs 570.83  loss Rs 199.14  ratio 2.866   +Rs 21,213
    opening_range_breakout_v1  35.0% win  win Rs 292.96  loss Rs 337.96  ratio 0.867  -Rs 356,631

Any gate that thresholds a win rate gets the profitable one backwards. What separates them is the
*ratio* of two estimated magnitudes, and a ratio of two point estimates carries no statement about
how well either is known — so both sides are estimated as **posteriors**, by Bayesian bootstrap, and
the credible bounds travel with them onto the evidence card.

**Bayesian bootstrap, not the frequentist percentile one.** Rubin's construction draws
`Dirichlet(1, ..., 1)` weights over the observed outcomes and takes the weighted mean, which is the
posterior of the mean under a non-informative Dirichlet-process prior. It makes no distributional
assumption — trade P&L is not Normal and nothing here claims it is — and it prices magnitude
*concentration* directly, which is the failure `docs/research/256` found in the ladder's first rule:
30 wins of ₹400, and 29 wins of ₹13.79 plus one of ₹11,600, are identical to a mean and completely
different to a bootstrap.

**Gross, because costs are a separate floor.** The magnitudes here are BEFORE costs. The priced
round-trip cost is one of the three minimums the resulting expectancy must clear
(`selection_corrected_quality_floor`), and taking costs off the magnitudes as well would charge them
twice — a defect that survived into this engine's first real-data run and was caught by it.

**Scratch trades count in neither magnitude, and that correction is not academic.** A zero outcome
averaged into the loss side drags that magnitude toward zero and lowers the implied break-even
without a rupee changing hands — the 200-scratch case in
`bot_maturity_ladder._break_even_win_rate`, which moved break-even from 50.0% to 8.3%. It bit here
too: `docs/research/254` reports `credit_spread_v1`'s average loss as Rs 165.44 and its payoff ratio
as 3.450, both computed with `realized_pnl <= 0` as the loss bucket. Excluding its **11 scratches**
the average loss is Rs 199.14 and the ratio is **2.866**. Corrected in `docs/research/260`.

**Deterministic by construction.** The generator is seeded from the outcomes themselves, so the same
record always yields the same posterior and the same verdict. A gate whose answer changed on re-run
would be unreplayable, and `A.29` requires that a decision be reconstructable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy

from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment
from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    PayoffPosterior,
    TradeQualityError,
)

OUTCOMES_NEEDED_FOR_A_POSTERIOR = 2
"""One observation has no dispersion, so a bootstrap over it returns that observation exactly.

A mathematical fact about the Dirichlet draw, not a tuning knob: with `n = 1` every weight vector is
`[1.0]` and the "posterior" is a point mass pretending to be an interval.
"""

DEFAULT_BOOTSTRAP_DRAWS = 4000
"""Draws per posterior, matching `bot_maturity_ladder._posterior_profitable` so the two agree.

Monte-Carlo error on a quantile at this many draws is well below the rupee resolution of the
quantities being compared; it is a numerical setting, not a threshold that decides anything.
"""

DEFAULT_CREDIBLE_MASS = 0.9
"""The central mass reported as the credible interval, when a caller states no preference.

Carried on every posterior it produces rather than assumed downstream, because a bound is
meaningless without the mass it excludes.
"""


@dataclass(frozen=True, slots=True)
class RealisedTradeOutcome:
    """One closed trade reduced to what this estimator needs: whose it was, and what it netted.

    Deliberately narrower than `ClosedPaperTrade` and than the retained `experience_nodes` row. Both
    of those adapt INTO this; neither is imported here, so the estimator is not coupled to the shape
    of any one record and can be fitted from the paper ledger, the retained corpus, or a replay.
    """

    bot_identity: str
    trading_segment: TradingSegment
    session_date: date
    gross_rupees: Decimal
    costs_rupees: Decimal
    notional_rupees: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.bot_identity.strip():
            raise TradeQualityError("an outcome with no bot identity belongs to no payoff record")
        if self.notional_rupees is not None and self.notional_rupees <= 0:
            raise TradeQualityError(
                f"{self.bot_identity} recorded a trade of notional {self.notional_rupees}; it is "
                f"the denominator that rescales this record onto a differently-sized proposal, and "
                f"a non-positive one inverts or explodes the scaling"
            )
        if self.costs_rupees < 0:
            raise TradeQualityError(
                f"{self.bot_identity} recorded costs of {self.costs_rupees}; a negative cost is a "
                f"rebate this project does not model, and it turns a gross loss into a net win"
            )
        if not (self.gross_rupees.is_finite() and self.costs_rupees.is_finite()):
            raise TradeQualityError(
                f"{self.bot_identity} recorded a non-finite outcome ({self.gross_rupees} gross, "
                f"{self.costs_rupees} costs); it would propagate through the bootstrap and make "
                f"every quantile meaningless"
            )

    @property
    def net_rupees(self) -> Decimal:
        return self.gross_rupees - self.costs_rupees

    @property
    def is_win(self) -> bool:
        """BEFORE costs.

        The quantity this estimator models is the **gross** payoff distribution, because the priced
        round-trip cost is a separate floor that the gross expectancy has to clear. Classifying by
        net here while reporting gross magnitudes would put a trade that earned Rs 20 and paid Rs 30
        into the loss bucket at a magnitude of +20, which is neither quantity.
        """
        return self.gross_rupees > 0

    @property
    def is_scratch(self) -> bool:
        return self.gross_rupees == 0


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    """A posterior mean with the credible interval it came with."""

    mean: Decimal
    lower: Decimal
    upper: Decimal
    credible_mass: float
    observations: int


def _seed_from(values: Sequence[float]) -> int:
    """A stable seed derived from the data, so the same record replays to the same answer.

    `hash()` is deliberately not used: it is salted per process for `str`, and while it is stable
    for `float` today that is an implementation detail rather than a promise. A digest of the exact
    repr is a promise.
    """
    digest = hashlib.sha256("|".join(repr(value) for value in values).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


def bayesian_bootstrap_mean(
    values: Sequence[Decimal],
    *,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
    credible_mass: float = DEFAULT_CREDIBLE_MASS,
) -> BootstrapInterval | None:
    """Rubin's Bayesian bootstrap over a sample, returning the posterior of its mean.

    `None` when the sample is too small to have dispersion, which the caller must treat as an
    absence of evidence rather than as a comfortable answer.
    """
    if not 0.0 < credible_mass < 1.0:
        raise TradeQualityError(
            f"credible mass must lie strictly inside (0, 1), got {credible_mass}; 1.0 is an "
            f"interval that excludes nothing and 0.0 is a point"
        )
    if draws <= 0:
        raise TradeQualityError("a posterior with no draws is not a posterior")
    if len(values) < OUTCOMES_NEEDED_FOR_A_POSTERIOR:
        return None
    sample = numpy.asarray([float(value) for value in values], dtype=float)
    if not numpy.all(numpy.isfinite(sample)):
        raise TradeQualityError(
            "a payoff sample carries a non-finite value; every quantile computed from it would be "
            "non-finite too, and a non-finite floor compares False against everything"
        )
    generator = numpy.random.default_rng(_seed_from(sample.tolist()))
    weights = generator.dirichlet(numpy.ones(sample.size), size=draws)
    means = weights @ sample
    tail = (1.0 - credible_mass) / 2.0
    lower, upper = numpy.quantile(means, [tail, 1.0 - tail])
    return BootstrapInterval(
        mean=Decimal(str(float(means.mean()))),
        lower=Decimal(str(float(lower))),
        upper=Decimal(str(float(upper))),
        credible_mass=credible_mass,
        observations=len(values),
    )


class RealisedPayoffDistributionEstimator:
    """Carried payoff state: per-bot win and loss magnitude posteriors, fitted from closed trades.

    Construction fits; `posterior_for` reads. No I/O, so it is safe on the decision path and
    replayable — the same discipline `L5.29` imposes on the bots themselves.
    """

    def __init__(
        self,
        outcomes: Sequence[RealisedTradeOutcome],
        *,
        draws: int = DEFAULT_BOOTSTRAP_DRAWS,
        credible_mass: float = DEFAULT_CREDIBLE_MASS,
    ) -> None:
        self._draws = draws
        self._credible_mass = credible_mass
        self._by_bot: dict[str, list[RealisedTradeOutcome]] = {}
        self._by_segment: dict[TradingSegment, list[RealisedTradeOutcome]] = {}
        for outcome in outcomes:
            self._by_bot.setdefault(outcome.bot_identity, []).append(outcome)
            self._by_segment.setdefault(outcome.trading_segment, []).append(outcome)

    @property
    def outcomes_fitted(self) -> int:
        return sum(len(rows) for rows in self._by_bot.values())

    def bot_identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_bot))

    def gross_outcomes_for(self, bot_identity: str) -> tuple[Decimal, ...]:
        """Every gross outcome this bot recorded, scratches included.

        Scratches belong here — this is the bot's own per-trade distribution, and a flat trade is
        real evidence about its expectancy — while they are excluded from the win and loss
        *magnitudes*, where they would distort the ratio.
        """
        return tuple(row.gross_rupees for row in self._by_bot.get(bot_identity, ()))

    def net_outcomes_for(self, bot_identity: str) -> tuple[Decimal, ...]:
        """Every outcome after its own costs. Reported as evidence; not what the floors compare."""
        return tuple(row.net_rupees for row in self._by_bot.get(bot_identity, ()))

    def scratch_share_among_non_wins_for(self, bot_identity: str) -> float:
        """Of this bot's non-winning trades, what fraction closed at exactly zero gross.

        **`docs/research/261` CRITICAL-6.** Per-trade expectancy is `p_win*W - p_loss*L`, and
        `p_loss` equals `1 - p_win` only when no trade ever scratches. Treating every non-win as
        a loss charged a full magnitude to trades that cost nothing: a bot earning +Rs 83 a trade
        was REFUSED on an estimated -Rs 63 with 30 scratches in its record, and -Rs 237 with 200.

        Scratches are correctly excluded from the loss MAGNITUDE — averaging zeros into it drags
        break-even down without a rupee moving. This is the other half of the same fact, and the
        module's own docstring claimed both halves were handled when only one was.

        Zero when the bot has no non-winning trades, which is the honest answer: nothing scratched
        because nothing failed to win.
        """
        rows = self._by_bot.get(bot_identity, ())
        non_wins = [row for row in rows if not row.is_win]
        if not non_wins:
            return 0.0
        return sum(1 for row in non_wins if row.is_scratch) / len(non_wins)

    def mean_cost_per_trade_for(self, bot_identity: str) -> Decimal | None:
        """What this bot has actually paid per round trip. `None` when it has no record.

        The mean rather than the median: the floor this feeds is a total cost of doing business, and
        a bot that pays little on most trades and a great deal on a few is genuinely paying the
        average. Concentration matters for payoffs, where one outlier can carry a whole record; it
        does not rescue a cost.
        """
        rows = self._by_bot.get(bot_identity, ())
        if not rows:
            return None
        return sum((row.costs_rupees for row in rows), Decimal(0)) / len(rows)

    def largest_notional_for(self, bot_identity: str) -> Decimal | None:
        """The biggest position this bot has actually traded. `None` when it recorded no notionals.

        The ceiling on `_size_scale`: extrapolating a payoff record linearly past the largest size a
        bot has ever put on is a claim with no evidence behind it, and `docs/research/261` MAJOR-C
        measured what that claim buys — admission on size alone.
        """
        notionals = [
            row.notional_rupees
            for row in self._by_bot.get(bot_identity, ())
            if row.notional_rupees is not None
        ]
        return max(notionals) if notionals else None

    def median_notional_for(self, bot_identity: str) -> Decimal | None:
        """The typical rupee size this bot's record was earned at. `None` when it recorded none.

        This is the denominator that rescales a payoff distribution onto a proposal of a different
        size, and it is the median rather than the mean because one outsized position would
        otherwise redefine "typical" and shrink every subsequent proposal's scaled expectancy.
        """
        notionals = sorted(
            row.notional_rupees
            for row in self._by_bot.get(bot_identity, ())
            if row.notional_rupees is not None
        )
        if not notionals:
            return None
        middle = len(notionals) // 2
        if len(notionals) % 2:
            return notionals[middle]
        return (notionals[middle - 1] + notionals[middle]) / 2

    def win_magnitudes_for(self, bot_identity: str) -> tuple[Decimal, ...]:
        """Positive magnitudes of this bot's winning trades. Scratches excluded, per the note above.

        Exposed raw, not only as an interval, because the net-edge posterior has to resample them
        JOINTLY with the loss side and the probability. Collapsing each side to three summary
        numbers first and reconstructing a distribution from them would throw away exactly the
        magnitude concentration this estimator exists to preserve.
        """
        return tuple(row.gross_rupees for row in self._by_bot.get(bot_identity, ()) if row.is_win)

    def loss_magnitudes_for(self, bot_identity: str) -> tuple[Decimal, ...]:
        """Positive magnitudes of this bot's losing trades."""
        return tuple(
            -row.gross_rupees for row in self._by_bot.get(bot_identity, ()) if row.gross_rupees < 0
        )

    def posterior_for(self, bot_identity: str) -> PayoffPosterior | None:
        """This bot's win and loss magnitude posteriors. `None` when it has not yet done both.

        A bot that has only won has not demonstrated an infinite payoff ratio; it has demonstrated
        an unmeasured one, and the gate must say `UNASSESSABLE` rather than admit on the strength of
        a missing denominator.
        """
        rows = self._by_bot.get(bot_identity, ())
        wins = [row.gross_rupees for row in rows if row.is_win]
        losses = [-row.gross_rupees for row in rows if row.gross_rupees < 0]
        win_interval = bayesian_bootstrap_mean(
            wins, draws=self._draws, credible_mass=self._credible_mass
        )
        loss_interval = bayesian_bootstrap_mean(
            losses, draws=self._draws, credible_mass=self._credible_mass
        )
        if win_interval is None or loss_interval is None:
            return None
        return PayoffPosterior(
            win_mean_rupees=win_interval.mean,
            win_lower_rupees=win_interval.lower,
            win_upper_rupees=win_interval.upper,
            loss_mean_rupees=loss_interval.mean,
            loss_lower_rupees=loss_interval.lower,
            loss_upper_rupees=loss_interval.upper,
            wins_observed=len(wins),
            losses_observed=len(losses),
            credible_mass=self._credible_mass,
        )

    def expectancy_posterior_for(self, bot_identity: str) -> BootstrapInterval | None:
        """The posterior of this bot's mean GROSS P&L per trade, over its own outcomes.

        This is what the payoff-implied floor is derived from, and it is deliberately NOT the
        plug-in break-even rate `L/(W+L)`. `docs/research/256` broke that rule on this exact record:
        a threshold re-estimated from the same sample it is compared against is non-monotonic in
        both directions and blind to magnitude concentration. The dispersion of this interval is a
        magnitude-aware quantity and moves in one direction only as evidence accumulates.
        """
        return bayesian_bootstrap_mean(
            self.gross_outcomes_for(bot_identity),
            draws=self._draws,
            credible_mass=self._credible_mass,
        )
