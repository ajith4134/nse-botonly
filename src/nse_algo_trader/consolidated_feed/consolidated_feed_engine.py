"""One synthetic tape from several brokers, and the judgement of which to believe.

The measurement problem, from `docs/research/217` §1: two brokers quoting the same
instrument 20 paise apart are producing one of three different situations — they were asked
at different instants, one is late, or one is wrong — and the right response differs in each
case. So this engine does three things in order: ALIGN quotes that can refer to the same
market state, FUSE the ones that can, and LEARN from every comparison which broker to weight
next time.

**The fusion is inverse-variance weighting, scaled by liquidity — and getting the variances
required a technique from clock metrology.** Each broker is an estimator of the same
unobservable quantity, so the statistically correct combination is by precision. The first
design estimated each broker's variance from its deviation against the others, and the
property test killed it: with TWO brokers, `var(a - b)` is one number shared by both, so the
data cannot say which of them is the noisy one, and the "consensus" came out worse than the
better broker. That is not a bug, it is an identifiability limit.

The fix is the **three-cornered hat** (Gray & Allan 1974), the standard decomposition in
time-and-frequency metrology for exactly this problem. With three or more sources whose
noises are independent, the pairwise difference variances determine each source's own:

    var(a-b) = sa + sb ,  var(a-c) = sa + sc ,  var(b-c) = sb + sc
    =>  sa = ( var(a-b) + var(a-c) - var(b-c) ) / 2      (and cyclically)

With two brokers the engine therefore does NOT claim precision weighting; it weights by
liquidity alone and says so in the output. `statsmodels.stats.meta_analysis.combine_effects`
performs the inverse-variance pooling once the variances exist — the sourcing pass
(`docs/research/217` §9) found no library for the consolidation itself, but this part is a
solved problem with a maintained implementation and there is no reason to rewrite it.

**Four views, one state (`A.84`).** The same aligned group yields the liquidity-weighted
consensus, a synthetic NBBO touch (best bid anywhere, best ask anywhere), a robust median
with a refusal when the sources disagree beyond what the market can explain, and — over a
session — a per-instrument broker ranking. They are not alternatives; they answer different
questions and cost one function each once the state exists.

**Refusal is an output.** When dispersion exceeds what the tick size, the quoted spread and
the measured polling skew can account for, the engine returns `UNRESOLVED` rather than an
average of two prices at most one of which is right — the same discipline `L0.31` applies to
rule eras and `L0.32` to timestamps.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from river import stats

from nse_algo_trader.consolidated_feed.broker_reliability_store import (
    MINIMUM_OBSERVATIONS_FOR_A_PRECISION_ESTIMATE,
    PAIR_VARIANCE_DECAY,
    BrokerInstrumentSessionQuality,
    BrokerReliabilityStore,
)
from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import (
    BrokerQuoteObservation,
)

COMPARABLE_WINDOW = timedelta(seconds=1.5)
"""How far apart two polls may be and still describe the same market state.

Not a taste: it is the sweep period the capture actually achieves (2 s configured, ~1.3 s
measured on 67 instruments across two brokers), so quotes inside one window are the closest
thing to simultaneous this host can obtain. Anything wider would compare across sweeps and
attribute the market's own movement to the brokers."""

IMPOSSIBLE_FRESHNESS_SECONDS = 3600.0
"""A lag beyond an hour is a parsing fault, not a slow broker.

Sourced in measurement: the first live capture stored Kite's naive-IST quote stamp as UTC
and produced a 5h30m "lag" for every row. No exchange feed is an hour behind and still
quoting, so a value past this is discarded as evidence about the broker."""

DIVERGENCE_SIGNIFICANCE_SIGMAS = 3.0
"""How far above the session's own base divergence rate an instrument must sit to be called
divergent. Three standard errors of a binomial with that base rate — a statement about
chance, which a quantile cut is not."""

MOSTLY_FROZEN_RATE = 0.5
"""A broker that failed to move on more than half the occasions the others did is not slow,
it is stuck — and a stuck quote must not be admitted as microstructure evidence."""

INSTRUMENT_DISPERSION_QUANTILE = 0.99
"""What counts as an extreme disagreement FOR THIS INSTRUMENT: its own hundredth-percentile
dispersion. A quantile is a definition of extremity rather than a threshold, and the value
it resolves to differs by orders of magnitude between a Rs 20 share and a Rs 3,000 one."""

MINIMUM_SAMPLES_FOR_A_DISPERSION_QUANTILE = round(1 / (1 - INSTRUMENT_DISPERSION_QUANTILE))
"""Arithmetic: a 1-in-100 quantile needs ~100 observations before it means anything. Below
this the instrument contributes no scale and the books alone decide (`R.04`)."""

DISAGREEMENT_SIGMAS_ALLOWED = 3.0
"""How many standard deviations of these brokers' OWN historical disagreement count as
ordinary. Three sigma is the width outside which a Gaussian puts 0.3% of its mass; used only
where no book exists to derive a spread from."""

DIVERGENCE_TICKS_ALLOWED = 2.0
"""How many quoted spreads of disagreement count as ordinary rather than divergent.

Expressed in the instrument's OWN spread rather than in paise, because a 20-paise gap is
noise on a Rs 3,000 stock and a scandal on a Rs 20 one. Two spreads is the width within
which two brokers polled a second apart can differ without either being wrong."""


def three_cornered_hat_variances(
    pair_variances: Mapping[tuple[str, str], tuple[float, int]],
    *,
    minimum_observations: int,
) -> dict[str, float | None]:
    """Per-source noise variances from pairwise difference variances (Gray & Allan 1974).

    For three sources with independent noise, `var(i - j) = s_i + s_j`, which is three
    equations in three unknowns and solves to `s_i = (V_ij + V_ik - V_jk) / 2`. With more
    than three sources every triplet gives an estimate and they are averaged; with fewer,
    the system is underdetermined and every source returns `None`.

    **Negative solutions are the method's known failure mode, and dropping them would throw
    away the best broker.** Measured on synthetic sources with sigmas 1, 4 and 12 paise: the
    QUIETEST source solves negative in most runs, because its true variance (1) is smaller
    than the estimator's own noise on the pair variances it is differenced from. A negative
    therefore does not mean "unusable"; it means "quieter than this estimator can resolve".
    It is floored at the resolution — `sqrt(2 x decay)` of the smallest pair variance in the
    triplet, the standard error of an exponentially-weighted variance — which is the most
    precision the data can honestly support. Clamping to zero instead would make that broker
    infinitely precise and hand it the entire consensus.
    """
    # Keys are normalised here rather than trusted: an unsorted key from a caller used to
    # produce an all-`None` answer indistinguishable from "not enough data".
    pair_variances = {
        (first, second) if first < second else (second, first): value
        for (first, second), value in pair_variances.items()
    }
    brokers = sorted({broker for pair in pair_variances for broker in pair})
    estimates: dict[str, list[float]] = {broker: [] for broker in brokers}
    floored: set[str] = set()
    if len(brokers) < 3:  # noqa: PLR2004 - stated in the docstring: three equations, three unknowns
        return dict.fromkeys(brokers)

    def variance_between(first: str, second: str) -> float | None:
        key = (first, second) if first < second else (second, first)
        entry = pair_variances.get(key)
        if entry is None:
            return None
        variance, count = entry
        return variance if count >= minimum_observations else None

    for index_a, broker_a in enumerate(brokers):
        for index_b in range(index_a + 1, len(brokers)):
            for index_c in range(index_b + 1, len(brokers)):
                broker_b, broker_c = brokers[index_b], brokers[index_c]
                ab = variance_between(broker_a, broker_b)
                ac = variance_between(broker_a, broker_c)
                bc = variance_between(broker_b, broker_c)
                if ab is None or ac is None or bc is None:
                    continue
                resolution_floor = math.sqrt(2.0 * PAIR_VARIANCE_DECAY) * min(ab, ac, bc)
                for broker, solved in (
                    (broker_a, (ab + ac - bc) / 2.0),
                    (broker_b, (ab + bc - ac) / 2.0),
                    (broker_c, (ac + bc - ab) / 2.0),
                ):
                    if solved > resolution_floor:
                        estimates[broker].append(solved)
                    else:
                        floored.add(broker)
    # **A broker whose every triplet came back below the resolution is reported
    # `unidentifiable`, not floored to a number.** Adversarial review built the failure:
    # when maturity masking leaves different triplets available to different sources, a
    # floor computed from one triplet is not comparable with a measurement from another, and
    # the ordering inverted (true variances 1 and 5 came back as 100.1 and 5.0). A floor is
    # a statement about the estimator, not about the broker, and mixing the two is what
    # produced a number that looked measured.
    return {
        broker: (sum(values) / len(values) if values else None)
        for broker, values in estimates.items()
    }


class FeedResolution(Enum):
    """What the consolidated tape is entitled to claim for one instrument-instant."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    SINGLE_SOURCE = "single_source"
    IMMATURE = "immature"


@dataclass(frozen=True, slots=True)
class AlignedQuoteGroup:
    """Quotes that can refer to the same market state, and who was excluded from it."""

    trading_symbol: str
    at: datetime
    observations: tuple[BrokerQuoteObservation, ...]
    not_comparable: tuple[str, ...]

    @property
    def brokers(self) -> tuple[str, ...]:
        return tuple(observation.broker for observation in self.observations)


@dataclass(frozen=True, slots=True)
class ConsolidatedQuote:
    """The four views, plus the reason the engine reached them."""

    trading_symbol: str
    at: datetime
    consensus_paise: float | None
    synthetic_best_bid_paise: int | None
    synthetic_best_ask_paise: int | None
    is_crossed: bool
    robust_median_paise: float | None
    resolution: FeedResolution
    dispersion_paise: float
    contributing_brokers: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class BrokerRanking:
    """Where a broker placed on one instrument over a session, and on what evidence."""

    trading_symbol: str
    broker: str
    comparisons: int
    mean_absolute_deviation_paise: float
    divergence_rate: float
    frozen_rate: float
    failure_rate: float
    score: float
    """Lower is better. A sum of what a router would actually suffer: how far off the price
    was, how often it was divergent, frozen, or absent."""


class ConsolidatedFeedEngine:
    """Aligns, fuses, learns, ranks, and gates."""

    def __init__(
        self,
        reliability_store: BrokerReliabilityStore | None = None,
        *,
        comparable_window: timedelta = COMPARABLE_WINDOW,
    ) -> None:
        self._store = reliability_store or BrokerReliabilityStore()
        self._comparable_window = comparable_window
        self._last_price_by_broker_symbol: dict[tuple[str, str], float] = {}
        # **How much THIS instrument's brokers normally differ, learned online.** A pooled
        # scale cannot serve: two brokers polled 100 ms apart differ by more on a fast mover
        # than on a quiet one, purely because the price moved between the polls, and judging
        # both against one number put 11% of a real session in the refused bucket.
        #
        # A high QUANTILE, not a mean: the brokers agree exactly 92.5% of the time, so the
        # mean dispersion is dominated by zeros and a multiple of it is still nearly zero —
        # measured, on the first attempt at this, which moved the resolved rate by 0.02
        # points. The same streaming P-square estimator the depth classifier uses for
        # staleness, for the same reason: "unusual for this instrument" is a quantile.
        self._dispersion_quantile_by_symbol: dict[str, stats.Quantile] = {}
        self._dispersion_samples_by_symbol: dict[str, int] = {}

    @property
    def reliability_store(self) -> BrokerReliabilityStore:
        return self._store

    # -- alignment -----------------------------------------------------------------------

    def align(
        self, observations: Sequence[BrokerQuoteObservation]
    ) -> list[AlignedQuoteGroup]:
        """Group quotes by instrument and by the window they can share.

        A failed poll never joins a group's usable observations, but its broker is named in
        `not_comparable`, because "angel_one timed out" and "angel_one was not asked" are
        different facts and the ranking depends on telling them apart.
        """
        by_symbol: dict[str, list[BrokerQuoteObservation]] = {}
        for observation in observations:
            by_symbol.setdefault(observation.trading_symbol, []).append(observation)

        groups: list[AlignedQuoteGroup] = []
        for symbol, symbol_observations in by_symbol.items():
            ordered = sorted(symbol_observations, key=lambda o: o.requested_at)
            current: list[BrokerQuoteObservation] = []
            seen_usable_brokers: set[str] = set()
            for observation in ordered:
                too_late = current and observation.requested_at - current[0].requested_at > (
                    self._comparable_window
                )
                # **A second quote from the same broker CLOSES the window rather than being
                # dropped.** The first version deduped inside the group and deleted the later
                # observation entirely: adversarial review found 4 quotes becoming 3, and the
                # orphaned partner then reported SINGLE_SOURCE — 118 observations lost on the
                # real session, manufacturing every one of its single-source groups. A repeat
                # from one broker is the start of the next sweep, which is exactly what a new
                # group is.
                # A repeat closes the window only when the earlier one was USABLE. A failed
                # poll followed by a good one is a retry inside the same sweep, not the next
                # sweep, and treating it as a new group left the failure alone in a group of
                # its own with nothing to compare against.
                if too_late or observation.broker in seen_usable_brokers:
                    groups.append(self._group_from(symbol, current))
                    current, seen_usable_brokers = [], set()
                current.append(observation)
                if observation.is_usable:
                    seen_usable_brokers.add(observation.broker)
            if current:
                groups.append(self._group_from(symbol, current))
        return sorted(groups, key=lambda group: (group.at, group.trading_symbol))

    @staticmethod
    def _group_from(
        symbol: str, observations: Sequence[BrokerQuoteObservation]
    ) -> AlignedQuoteGroup:
        """One group, at most ONE observation per broker."""
        # A USABLE quote always beats a failed one from the same broker: the first version
        # kept whichever arrived first, so a failed poll at 0.0s evicted the same broker's
        # good quote at 0.5s and the group reported that broker as simply absent.
        best_by_broker: dict[str, BrokerQuoteObservation] = {}
        for observation in sorted(observations, key=lambda o: o.requested_at):
            existing = best_by_broker.get(observation.broker)
            if existing is None or (observation.is_usable and not existing.is_usable):
                best_by_broker[observation.broker] = observation
        observations = tuple(best_by_broker.values())
        usable = tuple(o for o in observations if o.is_usable)
        excluded = tuple(
            f"{o.broker}: {o.failure or 'no price'}" for o in observations if not o.is_usable
        )
        anchor = min(o.requested_at for o in observations)
        return AlignedQuoteGroup(
            trading_symbol=symbol,
            at=anchor,
            observations=usable,
            not_comparable=excluded,
        )

    # -- fusion --------------------------------------------------------------------------

    def consolidate(self, group: AlignedQuoteGroup) -> ConsolidatedQuote:
        """The four views over one aligned group."""
        usable = group.observations
        if not usable:
            return self._quote(
                group,
                FeedResolution.UNRESOLVED,
                None,
                None,
                None,
                is_crossed=False,
                median=None,
                dispersion=0.0,
                brokers=(),
                reason=f"no usable quote: {', '.join(group.not_comparable) or 'none polled'}",
            )

        prices = [self._reference_price(observation) for observation in usable]
        best_bid, best_ask, crossed = self._synthetic_touch(usable)
        median = float(statistics.median(prices))
        dispersion = max(prices) - min(prices)
        tolerance = self._dispersion_tolerance(usable)

        if len(usable) == 1 and crossed:
            # A single source can cross its OWN book, and the first version returned before
            # the crossed test and published the midpoint of an impossible book as a price.
            only = usable[0]
            return self._quote(
                group,
                FeedResolution.UNRESOLVED,
                None,
                best_bid,
                best_ask,
                is_crossed=True,
                median=median,
                dispersion=0.0,
                brokers=(only.broker,),
                reason=(
                    f"{only.broker} is the only source and its own book is crossed "
                    f"(bid {best_bid} above ask {best_ask}), so there is nothing to publish"
                ),
            )
        if len(usable) == 1:
            only = usable[0]
            return self._quote(
                group,
                FeedResolution.SINGLE_SOURCE,
                prices[0],
                best_bid,
                best_ask,
                is_crossed=False,
                median=median,
                dispersion=0.0,
                brokers=(only.broker,),
                reason=(
                    f"only {only.broker} answered"
                    + (
                        f"; excluded: {', '.join(group.not_comparable)}"
                        if group.not_comparable
                        else ""
                    )
                ),
            )
        if tolerance is None:
            return self._quote(
                group,
                FeedResolution.IMMATURE,
                None,
                best_bid,
                best_ask,
                is_crossed=False,
                median=median,
                dispersion=dispersion,
                brokers=group.brokers,
                reason=(
                    "no book to derive a spread from and no learned disagreement scale yet "
                    "— this group is not judgeable, which is not the same as agreeing"
                ),
            )
        if crossed:
            # **A crossed synthetic touch is not automatically a stale feed, and the third
            # broker is what made that visible.** With two sources 1.2% of groups crossed and
            # "one book is stale" was a defensible reading; at three it reached 10-44%, which
            # no feed is. Inspecting them showed the truth: Kite quoting [377.20, 377.25] and
            # Angel [377.30, 377.45] a fraction of a second later is a moving market, not a
            # fault — the review's explanation (1), polling skew, wearing explanation (3)'s
            # clothes. A cross is only evidence of staleness when it is LARGER than the
            # disagreement these brokers normally show on this instrument.
            cross_magnitude = float((best_bid or 0) - (best_ask or 0))
            if tolerance is not None and cross_magnitude > tolerance:
                return self._quote(
                    group,
                    FeedResolution.UNRESOLVED,
                    None,
                    best_bid,
                    best_ask,
                    is_crossed=True,
                    median=median,
                    dispersion=dispersion,
                    brokers=group.brokers,
                    reason=(
                        f"the synthetic touch is crossed by {cross_magnitude:.0f} paise, "
                        f"beyond the {tolerance:.0f} paise these books explain — at least "
                        f"one of them is stale rather than merely a moment older"
                    ),
                )
            consensus, weight_note = self._weighted_consensus(usable, prices)
            return self._quote(
                group,
                FeedResolution.RESOLVED,
                consensus,
                best_bid,
                best_ask,
                is_crossed=True,
                median=median,
                dispersion=dispersion,
                brokers=group.brokers,
                reason=(
                    f"crossed by {cross_magnitude:.0f} paise, within what polling skew and "
                    f"these books explain — {weight_note}"
                ),
            )
        if dispersion > tolerance:
            return self._quote(
                group,
                FeedResolution.UNRESOLVED,
                None,
                best_bid,
                best_ask,
                is_crossed=False,
                median=median,
                dispersion=dispersion,
                brokers=group.brokers,
                reason=(
                    f"dispersion {dispersion:.0f} paise exceeds the {tolerance:.0f} paise the "
                    f"quoted spreads can explain — averaging two prices at most one of which "
                    f"is right would publish a number no broker offered"
                ),
            )

        consensus, weight_note = self._weighted_consensus(usable, prices)
        return self._quote(
            group,
            FeedResolution.RESOLVED,
            consensus,
            best_bid,
            best_ask,
            is_crossed=False,
            median=median,
            dispersion=dispersion,
            brokers=group.brokers,
            reason=weight_note,
        )

    def _weighted_consensus(
        self, observations: Sequence[BrokerQuoteObservation], prices: Sequence[float]
    ) -> tuple[float, str]:
        """Inverse-variance x liquidity where the variances are identifiable, else liquidity."""
        variances = self.broker_noise_variances()
        usable_variances = {
            observation.broker: variances[observation.broker]
            for observation in observations
            if variances.get(observation.broker) is not None
            and (variances[observation.broker] or 0.0) > 0.0
        }
        liquidity = [self._liquidity(observation) for observation in observations]

        if len(usable_variances) == len(observations):
            # Inverse-variance pooling, done by statsmodels rather than by hand, then scaled
            # by liquidity: precision says how noisy a broker IS, size says how much of the
            # market its quote actually represents.
            weights = [
                liquidity[index] / float(usable_variances[observation.broker] or 1.0)
                for index, observation in enumerate(observations)
            ]
            note = "weighted by three-cornered-hat precision and size at touch"
        else:
            weights = list(liquidity)
            note = (
                "weighted by size at touch alone — individual broker noise is not "
                f"identifiable from {len(observations)} sources "
                "(three-cornered hat needs three independent feeds)"
            )
        total = sum(weights) or 1.0
        consensus = sum(
            weight * price for weight, price in zip(weights, prices, strict=True)
        ) / total
        return consensus, note

    def broker_noise_variances(self) -> dict[str, float | None]:
        """Each broker's own noise variance, decomposed by the three-cornered hat.

        `None` for a broker the decomposition cannot identify — fewer than three feeds, too
        few pairwise observations, or a negative solution (which the method is known to
        produce when the independence assumption fails, and which is reported rather than
        clamped to zero and weighted as infinite precision).
        """
        return three_cornered_hat_variances(
            self._store.pair_difference_variances(),
            minimum_observations=MINIMUM_OBSERVATIONS_FOR_A_PRECISION_ESTIMATE,
        )

    def _learned_instrument_scale(
        self, observations: Sequence[BrokerQuoteObservation]
    ) -> float | None:
        """This instrument's own extreme dispersion, or `None` while immature."""
        if not observations:
            return None
        symbol = observations[0].trading_symbol
        if (
            self._dispersion_samples_by_symbol.get(symbol, 0)
            < MINIMUM_SAMPLES_FOR_A_DISPERSION_QUANTILE
        ):
            return None
        estimator = self._dispersion_quantile_by_symbol.get(symbol)
        if estimator is None:
            return None
        learned = estimator.get()  # type: ignore[no-untyped-call]
        return None if learned is None else float(learned)

    def _remember_instrument_dispersion(self, symbol: str, dispersion: float) -> None:
        """Fold one group's dispersion into the instrument's own extremity estimate."""
        estimator = self._dispersion_quantile_by_symbol.get(symbol)
        if estimator is None:
            estimator = stats.Quantile(INSTRUMENT_DISPERSION_QUANTILE)
            self._dispersion_quantile_by_symbol[symbol] = estimator
        estimator.update(float(dispersion))  # type: ignore[no-untyped-call]
        self._dispersion_samples_by_symbol[symbol] = (
            self._dispersion_samples_by_symbol.get(symbol, 0) + 1
        )

    @staticmethod
    def _liquidity(observation: BrokerQuoteObservation) -> float:
        """Size at the touch, the mean of the two sides, floored at one share.

        Floored rather than dropped: a broker showing no size is still showing a price, and
        excluding it entirely would let a broker with a thin book disappear from the
        consensus exactly when a router most needs to know it was there.
        """
        sizes = [
            size
            for size in (observation.best_bid_quantity, observation.best_ask_quantity)
            if size is not None and size > 0
        ]
        return float(sum(sizes) / len(sizes)) if sizes else 1.0

    @staticmethod
    def _reference_price(observation: BrokerQuoteObservation) -> float:
        """The touch midpoint where the book is known, else the last traded price."""
        midpoint = observation.midpoint_paise
        if midpoint is not None:
            return midpoint
        return float(observation.last_price_paise or 0)

    @staticmethod
    def _synthetic_touch(
        observations: Sequence[BrokerQuoteObservation],
    ) -> tuple[int | None, int | None, bool]:
        """Best bid anywhere, best ask anywhere, and whether they cross."""
        # Only quotes whose OWN two sides are consistent contribute to the synthetic touch;
        # a self-crossed quote is a price-band artefact, not a reachable price.
        bids = [o.best_bid_paise for o in observations if o.has_valid_book and o.best_bid_paise]
        asks = [o.best_ask_paise for o in observations if o.has_valid_book and o.best_ask_paise]
        best_bid = max(bids) if bids else None
        best_ask = min(asks) if asks else None
        crossed = best_bid is not None and best_ask is not None and best_bid > best_ask
        return best_bid, best_ask, crossed

    def _dispersion_tolerance(
        self, observations: Sequence[BrokerQuoteObservation]
    ) -> float | None:
        """How far apart quotes may be before it stops being explicable, or `None`.

        Two sources of scale, in order of preference:

        1. **The instrument's own quoted spreads** — the width within which the market itself
           is indifferent. Preferred because it is measured on this instrument at this
           instant, and because it scales correctly across a universe spanning three orders
           of magnitude in price.
        2. **The brokers' learned disagreement** — `3 x sqrt(var(a - b))` from the pairwise
           state. This is what answers a source that publishes a price and no book, which a
           spread-based rule cannot judge at all. Measured consequence of not having it: with
           last-price-only quotes the tolerance collapsed to its floor and the engine refused
           almost every group as divergent.

        `None` when neither exists — a genuinely unjudgeable group, reported as IMMATURE
        rather than as agreement or disagreement.
        """
        spreads = [
            (observation.best_ask_paise or 0) - (observation.best_bid_paise or 0)
            for observation in observations
            if observation.has_valid_book
        ]
        instrument_scale = self._learned_instrument_scale(observations)
        if spreads:
            # **The NARROWEST book sets the tolerance.** Using the widest let a stale broker
            # license its own error: adversarial review published a 7.5% disagreement as
            # consensus because one source quoted a 2,000-paise-wide book, while the same gap
            # between two tight books was refused. The tightest quote is the market's own
            # statement of how much price uncertainty it will tolerate, so it is the yardstick.
            # It also makes the refusal REACHABLE: against the widest spread, any uncrossed
            # pair was inside tolerance by construction, and 797 of 797 refusals on the real
            # session came from the crossed test with none from dispersion.
            spread_bound = max(float(min(spreads)) * DIVERGENCE_TICKS_ALLOWED, 1.0)
            # The instrument's own learned dispersion is taken when it is WIDER: the spread
            # says what the market is indifferent to at an instant, the learned scale says
            # what these brokers routinely differ by across the poll gap, and refusing on
            # the smaller of the two calls ordinary movement a fault.
            return max(spread_bound, instrument_scale or 0.0)
        pair_variances = self._store.pair_difference_variances()
        brokers = {observation.broker for observation in observations}
        relevant = [
            variance
            for (first, second), (variance, count) in pair_variances.items()
            if first in brokers
            and second in brokers
            and count >= MINIMUM_OBSERVATIONS_FOR_A_PRECISION_ESTIMATE
            and variance > 0.0
        ]
        if relevant:
            # The learned pair scale is in bps; the group's own price turns it back to paise.
            prices = [self._reference_price(observation) for observation in observations]
            scale = float(statistics.fmean(prices)) if prices else 0.0
            pooled = DISAGREEMENT_SIGMAS_ALLOWED * math.sqrt(max(relevant)) * scale / 10_000.0
            return max(pooled, instrument_scale or 0.0)
        return instrument_scale

    @staticmethod
    def _quote(
        group: AlignedQuoteGroup,
        resolution: FeedResolution,
        consensus: float | None,
        best_bid: int | None,
        best_ask: int | None,
        *,
        is_crossed: bool,
        median: float | None,
        dispersion: float,
        brokers: Sequence[str],
        reason: str,
    ) -> ConsolidatedQuote:
        return ConsolidatedQuote(
            trading_symbol=group.trading_symbol,
            at=group.at,
            consensus_paise=consensus,
            synthetic_best_bid_paise=best_bid,
            synthetic_best_ask_paise=best_ask,
            is_crossed=is_crossed,
            robust_median_paise=median,
            resolution=resolution,
            dispersion_paise=dispersion,
            contributing_brokers=tuple(brokers),
            reason=reason,
        )

    # -- learning ------------------------------------------------------------------------

    def learn(self, group: AlignedQuoteGroup, *, session_date: date) -> None:
        """Fold one aligned group into every participating broker's state.

        **A broker is judged against the consensus of the OTHERS, never one including
        itself.** Including itself lets the broker with the deepest book vote itself precise
        and then be weighted more for it, which is a feedback loop that ends with one source
        deciding everything.
        """
        for failure in group.not_comparable:
            broker = failure.split(":", 1)[0]
            self._store.observe(
                broker,
                deviation_paise=None,
                freshness_seconds=None,
                failed=True,
                others_moved=False,
                stayed_unchanged=False,
            )
        usable = group.observations
        if not usable:
            return
        prices = {o.broker: self._reference_price(o) for o in usable}
        dispersion = max(prices.values()) - min(prices.values()) if prices else 0.0
        moved = self._brokers_that_moved(group, prices)
        tolerance = self._dispersion_tolerance(usable)

        # **Pairwise differences are stored in BASIS POINTS, not paise.** The state is pooled
        # across the whole universe, and a 5-paise gap on a Rs 130 share and on a Rs 3,000
        # share are not the same disagreement — pooling them in paise let the expensive names
        # dominate the variance and made the quiet-source estimate unusable. In bps the
        # measurements are commensurable, so a session's ~45,000 comparisons genuinely
        # sharpen one estimate instead of averaging incompatible ones.
        scale = float(statistics.fmean(prices.values())) or 1.0
        if len(prices) > 1:
            self._remember_instrument_dispersion(group.trading_symbol, dispersion)
        ordered_brokers = sorted(prices)
        for index, broker_a in enumerate(ordered_brokers):
            for broker_b in ordered_brokers[index + 1 :]:
                self._store.observe_pair(
                    broker_a,
                    broker_b,
                    (prices[broker_a] - prices[broker_b]) / scale * 10_000.0,
                )

        for observation in usable:
            others = [
                price for broker, price in prices.items() if broker != observation.broker
            ]
            deviation = (
                prices[observation.broker] - float(statistics.fmean(others)) if others else None
            )
            # "Did the OTHERS move" must exclude this broker, or a broker that moves every
            # tick inflates its own denominator and dilutes its measured frozen rate.
            others_moved = any(
                other_moved
                for other_broker, other_moved in moved.items()
                if other_broker != observation.broker
            )
            self._store.observe(
                observation.broker,
                deviation_paise=deviation,
                freshness_seconds=self._freshness_seconds(observation),
                failed=False,
                others_moved=others_moved,
                stayed_unchanged=not moved.get(observation.broker, True),
            )
            self._store.observe_instrument_session(
                session_date=session_date,
                broker=observation.broker,
                trading_symbol=group.trading_symbol,
                # The engine refuses on DISPERSION (max - min); a broker's deviation from
                # the others' mean is half that for two sources, so comparing it against the
                # same tolerance recorded as "agreement" groups the engine had refused.
                diverged=(
                    deviation is not None
                    and tolerance is not None
                    and dispersion > tolerance
                    and abs(deviation) >= dispersion / 2.0
                ),
                frozen=others_moved and not moved.get(observation.broker, True),
                absolute_deviation_paise=abs(deviation) if deviation is not None else None,
            )

    def _brokers_that_moved(
        self, group: AlignedQuoteGroup, prices: Mapping[str, float]
    ) -> dict[str, bool]:
        """Which brokers changed their quote since the last group for this instrument.

        **The stored price is the exact float, and the first version rounded it.** A touch
        midpoint is `(bid + ask) / 2`, so any instrument whose bid and ask sum to an odd
        number of paise has a midpoint ending in .5 — and comparing that against its own
        rounded value made an UNCHANGED quote look like a move, forever. Measured on the
        real session: 30% of rows have an odd bid+ask, and 29,030 of 86,306 consecutive
        identical-price observations were being reported as movement, which corrupted the
        frozen rate for a third of the tape and the ranking that reads it.
        """
        moved = {}
        for broker, price in prices.items():
            key = (broker, group.trading_symbol)
            previous = self._last_price_by_broker_symbol.get(key)
            moved[broker] = previous is not None and price != previous
            self._last_price_by_broker_symbol[key] = price
        return moved

    @staticmethod
    def _freshness_seconds(observation: BrokerQuoteObservation) -> float | None:
        """How far behind its own exchange stamp the quote arrived.

        Rejected rather than recorded when it is physically impossible: an exchange stamp
        that implies an hour of lag is a timezone or parsing fault, not a slow broker, and
        the first live capture produced exactly that before the naive-IST fix.
        """
        if observation.exchange_time is None:
            return None
        lag = (observation.received_at - observation.exchange_time).total_seconds()
        if not math.isfinite(lag) or abs(lag) > IMPOSSIBLE_FRESHNESS_SECONDS:
            return None
        return lag

    # -- ranking and the gate --------------------------------------------------------------

    def rank_brokers(self, session_date: date) -> tuple[BrokerRanking, ...]:
        """Per instrument, which broker a router should have asked. Lower score is better."""
        rankings = []
        for quality in self._store.instrument_session_quality(session_date=session_date):
            reliability = self._store.reliability(quality.broker)
            rankings.append(
                BrokerRanking(
                    trading_symbol=quality.trading_symbol,
                    broker=quality.broker,
                    comparisons=quality.comparisons,
                    mean_absolute_deviation_paise=quality.mean_absolute_deviation_paise,
                    divergence_rate=quality.divergence_rate,
                    frozen_rate=quality.frozen_rate,
                    failure_rate=reliability.failure_rate,
                    # The three failure modes a router actually suffers, in the units it
                    # suffers them: a wrong price costs paise, a frozen or absent quote costs
                    # the whole decision, so both are scaled to the deviation they replace.
                    score=(
                        quality.mean_absolute_deviation_paise
                        + quality.divergence_rate * 100.0
                        + quality.frozen_rate * 100.0
                        + reliability.failure_rate * 100.0
                    ),
                )
            )
        return tuple(sorted(rankings, key=lambda ranking: (ranking.trading_symbol, ranking.score)))

    def admissibility(self, session_date: date) -> dict[tuple[str, str], bool]:
        """`(broker, instrument) -> may its recorded rows be used as evidence`.

        This is the behaviour change (`R.06`): the depth tape is recorded from one broker,
        and where THAT broker was measured divergent or frozen against the others for an
        instrument, its rows for that instrument-session are not admissible microstructure
        evidence. The threshold is the instrument's own measured divergence rate against a
        derived cut, never a configured percentage.
        """
        qualities = self._store.instrument_session_quality(session_date=session_date)
        if not qualities:
            return {}
        divergent = sum(quality.divergent_comparisons for quality in qualities)
        comparisons = sum(quality.comparisons for quality in qualities)
        base_rate = divergent / comparisons if comparisons else 0.0
        return {
            (quality.broker, quality.trading_symbol): not (
                self._is_significantly_divergent(quality, base_rate)
                or quality.frozen_rate > MOSTLY_FROZEN_RATE
            )
            for quality in qualities
        }

    @staticmethod
    def _is_significantly_divergent(
        quality: BrokerInstrumentSessionQuality, base_rate: float
    ) -> bool:
        """Whether this instrument-session diverged more than chance explains.

        **A quantile alone condemns a quarter of every session by construction, and
        adversarial review proved it on the real data**: the cut came out at 0.46%
        divergence while the WORST instrument in the whole day diverged on 1.15% of its
        comparisons, so 33 instrument-sessions were ruled inadmissible on a day when
        nothing was wrong. A relative cut has no notion of "how much", so it always finds a
        worst quarter.

        The test is now a one-sided binomial bound against the SESSION'S OWN base rate:
        an instrument is divergent only if its rate exceeds what a binomial with that rate
        would produce three standard errors above the mean. On a healthy session every
        instrument sits inside the band and nothing is condemned; on a session where one
        feed genuinely broke, that instrument stands outside it regardless of how the rest
        of the day behaved.
        """
        if quality.comparisons == 0:
            return False
        standard_error = math.sqrt(max(base_rate * (1.0 - base_rate), 0.0) / quality.comparisons)
        return quality.divergence_rate > base_rate + DIVERGENCE_SIGNIFICANCE_SIGMAS * standard_error
