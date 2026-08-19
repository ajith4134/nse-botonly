"""`M14` — decide whether the bar store and the depth tape describe the same market.

Spec: `docs/research/236`.

**The claim this engine exists to test.** `F04` takes its signal from five-minute bars
(`price_bars`, backfilled from Kite's historical endpoint) and produces every fill from the
recorded depth tape (parquet, captured live from Kite's full-mode websocket). Those are two
independently-sourced stores, and until now nothing had ever compared them. If they disagree,
the signal and the fill describe different markets and every paper P&L is partly a measurement
of the disagreement rather than of the strategy.

**Three comparisons, not one**, because a single price check is satisfied by two feeds that
share an upstream and disagree about everything else:

1. the bar's close against the tape's `last_price_paise` at the bar's closing instant;
2. the bar's close against the aligned book's `[best_bid, best_ask]` bracket — a traded price
   must lie inside the book that produced it, and a token collision (`L0.02`) is exactly the
   failure that passes 1 and fails this;
3. the tape's cumulative `volume_traded` increment across the bar interval against the bar's
   own volume — **asymmetric on purpose**: sampling moves the tape's endpoints inward, so it can
   only under-count. `tape_delta > bar_volume` is impossible under the sampling model and is
   therefore evidence sampling cannot explain away.

**Every tolerance is derived (`R.03`).** The price tolerance is the instrument's own prevailing
spread, read from the tape's own book that session — a ₹1 scrip and a NIFTY weekly option cannot
share a paise constant. The alignment tolerance is
`OrderBookSnapshotReplayEngine.staleness_threshold_millis_for`, the same per-instrument gap
quantile the fill path obeys; a join verified under a looser threshold than fills obey would be
verifying a market the loop never trades in.

**The verdict is a BETA-BINOMIAL test against the session's own pooled disagreement rate**, not a
cutoff. "Below 95% is bad" is the hardcoded constant `R.03` forbids, and a Tukey fence is a
constant wearing a distribution's clothes. Only the significance level is a policy input, and it
has no default — the same shape as the replay engine's `staleness_quantile`.

*Beta-binomial rather than binomial, and this replaced a binomial that over-rejected fivefold*
(`docs/research/241`, `A.123`). A binomial null asserts that every comparison in the session is an
independent draw at one common rate. The second half is false and the falseness was measured:
**24.45% of two-sided snapshots have the tape's OWN last price outside its OWN bracket**, because
disagreement propensity is a property of the INSTRUMENT — its spread against its tick, how often
its book is one-sided, how far its last trade sits from its own mid — not of the individual bar.
Comparisons within one instrument are therefore positively correlated, the binomial understates
`Var(K_i)`, and a session modelled with **no broken join at all** produced **~594 false refusals of
9,000** against the **121** actually observed. The null now carries an intra-instrument correlation
`rho` estimated from the session's own clusters, so `Var(K_i) = n·p(1-p)·[1 + (n-1)rho]`.

*And it is where `B1` is answered from, though NOT necessarily closed by.* `JOIN_VERIFIED` used to
be reachable on as few as two comparable bars. The evidence rule that guards against that —
`smallest_trials_that_can_reject` — was never wrong: under a binomial null, two all-disagreeing
draws at `p = 0.1` really are surprising enough. It answered 2 because the DISTRIBUTION was wrong,
so the rule is left untouched and the distribution under it is replaced. **How much the bar
actually rises is an empirical question, and the honest answer is: only where the pooled rate and
the dispersion are both large.** Computed exactly (`docs/research/241` §1.1), at `p = 0.05` and a
0.05 significance the answer stays at 2 for every `rho` up to 0.5; at `p = 0.2445` and a 0.01
significance it runs 4 → 9 → 79 as `rho` goes 0 → 0.25 → 0.5. So whether the thin instruments
fall to `JOIN_UNVERIFIABLE` depends on the `rho-hat` the real sessions produce, and if they do not,
`B1` goes back to the operator with those measurements attached rather than being patched with a
floor here. One fix, one mechanism; a separate minimum-bar rule would have been a second mechanism
for the same question, and the second one is where they disagree.

**Its output changes behaviour (`R.06`).** `refuted_instruments()` returns the same
`frozenset[int]` the replay engine already accepts as `inadmissible_instruments`, so an
instrument whose two stores disagree stops producing paper fills.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    DECILE_COUNT,
    MINIMUM_GAPS_FOR_A_QUANTILE,
    BookSnapshot,
    OrderBookReplayError,
    book_snapshots_from_table,
)
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    BAR_INTERVAL,
    DEFAULT_MARKET_DATA_PATH,
)

PAISE_PER_RUPEE = 100
"""NSE quotes two decimals; the wire integer is already paise (`depth_tape_schema`). A fact."""

MINIMUM_SAMPLES_FOR_A_DECILE = DECILE_COUNT
"""Ten order statistics is the least that can carry nine interior deciles. Arithmetic."""

MINIMUM_BARS_TO_SHOW_CONSTANCY = 2
"""Two points are the fewest that can DISAGREE about a factor, so it is the arithmetic floor rather
than a chosen sample size. It is not sufficient evidence, and it is deliberately not treated as if
it were: the review demonstrated that two ratios 4% apart (0.90, 0.94) still fit a median of 0.92.
The bar COUNT is carried on every verdict and printed by the runner so thin fits read as thin, and
the adjudication that settles them is `docs/research/239` part 3."""


class JoinVerificationError(Exception):
    """The comparison could not be set up. Never raised for a disagreement — a disagreement is
    the answer, not an error."""


class BarComparisonClass(Enum):
    """What one bar's comparison established. A three-way partition (`A.41`): agreement,
    disagreement, and the genuinely unverifiable — which is never folded into either side."""

    AGREES_WITHIN_DERIVED_TOLERANCE = "agrees_within_derived_tolerance"
    DISAGREES_BEYOND_DERIVED_TOLERANCE = "disagrees_beyond_derived_tolerance"
    CLOSE_OUTSIDE_RECORDED_BOOK = "close_outside_recorded_book"
    TAPE_ABSENT_IN_BAR_INTERVAL = "tape_absent_in_bar_interval"
    TAPE_STALE_AT_BAR_CLOSE = "tape_stale_at_bar_close"
    BOOK_ONE_SIDED_AT_BAR_CLOSE = "book_one_sided_at_bar_close"
    TAPE_HAS_NO_TRADE_YET = "tape_has_no_trade_yet"
    """`last_price_paise == 0` — the instrument had not traded when this packet was sent. There is
    no traded price to compare against, so the full-price "deviation" it used to produce was a fact
    about the tape rather than about the join. Measured on 2026-08-11: 450 such rows across at
    least 50 distinct tokens in the first 800k rows (`docs/research/240`, `B8`)."""

    BOOK_CROSSED_AT_BAR_CLOSE = "book_crossed_at_bar_close"
    """`best_bid > best_ask` — the recorded book is crossed, so its bracket is empty and EVERY
    close falls outside it. Counting that as a disagreement blamed the join for a tape defect the
    integrity classifier already flags as `BOOK_CROSSED`."""

    @property
    def is_verifiable(self) -> bool:
        """Whether this class carries evidence either way. The three absence classes do not,
        and counting them as agreement would let a session that captured nothing report a
        perfect join."""
        return self in _VERIFIABLE_CLASSES

    @property
    def is_disagreement(self) -> bool:
        return self in _DISAGREEMENT_CLASSES


_VERIFIABLE_CLASSES = frozenset(
    {
        BarComparisonClass.AGREES_WITHIN_DERIVED_TOLERANCE,
        BarComparisonClass.DISAGREES_BEYOND_DERIVED_TOLERANCE,
        BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
    }
)

_DISAGREEMENT_CLASSES = frozenset(
    {
        BarComparisonClass.DISAGREES_BEYOND_DERIVED_TOLERANCE,
        BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
    }
)


class VolumeReconciliationClass(Enum):
    """The asymmetric volume test. Only one side of it is impossible under sampling."""

    CONSISTENT_WITH_SAMPLING = "consistent_with_sampling"
    """`tape_delta <= bar_volume`: the tape saw the same trades or fewer, which is what
    sampling at both endpoints produces."""

    TAPE_EXCEEDS_BAR = "tape_exceeds_bar"
    """`tape_delta > bar_volume`: the tape recorded MORE volume inside the interval than the
    bar says traded in it. Sampling cannot produce this. Either the stores hold different
    instruments, or one of them is stamped on a different clock."""

    UNVERIFIABLE_NO_ENDPOINTS = "unverifiable_no_endpoints"
    """Fewer than two packets in the interval, so there is no increment to take."""


class InstrumentJoinVerdict(Enum):
    """What the session established about one instrument, and what the loop does about it."""

    JOIN_VERIFIED = "join_verified"
    JOIN_REFUTED = "join_refuted"
    JOIN_UNVERIFIABLE = "join_unverifiable"
    """Too few verifiable comparisons for the test to have rejected even if EVERY one of them
    disagreed. Not a pass — the honest third state."""


class DisagreementShape(Enum):
    """WHY a refused instrument disagrees — a different question from whether it does.

    Added after the test behind it was run by hand twice (`docs/research/237`, `238`). Running a
    discriminator by hand twice is the signal that it should not be by hand.
    """

    PRICE_BASIS_DIVERGENCE = "price_basis_divergence"
    """The ratio `bar_close / tape_last_price` is the SAME on every disagreeing bar. A market does
    not do that; one series multiplied by a factor does. This is the `M26` defect — Kite's
    historical endpoint adjusts as of the moment it is asked, so a session backfilled after an
    ex-date comes back rescaled while the tape holds what actually traded. The factor is reported
    so it can be adjudicated against a third source."""

    SPORADIC_DISAGREEMENT = "sporadic_disagreement"
    """Ratios scatter. Boundary and sampling effects — real enough to exceed the session's base
    rate, but not a store-level defect."""

    NOT_APPLICABLE = "not_applicable"
    """The instrument was not refused, or has no disagreeing bar with a usable ratio."""


@dataclass(frozen=True, slots=True)
class StoredBar:
    """One five-minute bar as the paper loop's signal source reads it.

    Deliberately not `bitemporal_bar_store.BarRecord`: the loop reads the `price_bars` table
    through `paper_session_signal_source.bars_available_at`, and the join must be verified on
    the bars the loop ACTUALLY consumes, not on a differently-keyed copy of them.
    """

    instrument_token: int
    bar_timestamp: datetime
    close_price: float
    volume: int
    interval: timedelta

    @property
    def closing_instant(self) -> datetime:
        """When the bar's last trade could have happened at the latest — the interval's end.

        A five-minute bar stamped 09:15 covers `[09:15, 09:20)`, so its close is the last trade
        strictly before 09:20. The tape is aligned to that instant, not to the stamp: aligning
        to the stamp would compare a bar's CLOSING price against the book at its OPENING.
        """
        return self.bar_timestamp + self.interval

    @property
    def close_paise(self) -> int:
        """The close as an exact paise count, matching the tape's integer scale.

        `round` recovers the wire integer exactly at NSE magnitudes, by the same argument
        `depth_tape_schema.price_to_paise` makes.
        """
        return round(self.close_price * PAISE_PER_RUPEE)


@dataclass(frozen=True, slots=True)
class BarComparison:
    """One bar, compared. Carries its evidence so a verdict can be re-derived from the row."""

    instrument_token: int
    bar_timestamp: datetime
    comparison_class: BarComparisonClass
    bar_close_paise: int
    tape_last_price_paise: int | None
    best_bid_paise: int | None
    best_ask_paise: int | None
    deviation_paise: int | None
    tolerance_paise: int | None
    alignment_age_millis: float | None
    volume_class: VolumeReconciliationClass
    bar_volume: int
    tape_volume_delta: int | None

    @property
    def deviation_in_tolerances(self) -> float | None:
        """The deviation measured in units of the instrument's own spread.

        The unit that makes a ₹1 scrip and a ₹80,000 index option comparable on one axis, which
        is what the report's deciles are binned on.
        """
        if self.deviation_paise is None or not self.tolerance_paise:
            return None
        return self.deviation_paise / self.tolerance_paise


@dataclass(frozen=True, slots=True)
class InstrumentJoinReport:
    """What the session established about one instrument's two stores."""

    instrument_token: int
    verdict: InstrumentJoinVerdict
    bars_total: int
    comparisons_verifiable: int
    disagreements: int
    disagreement_shape: DisagreementShape
    """WHY it disagrees, when it was refused — a basis divergence or scatter."""
    price_basis_ratio: float | None
    """`bar_close / tape_last_price` when that ratio is constant; `None` otherwise. The number to
    adjudicate against a third source, and the number a repair would divide by."""
    null_disagreement_rate: float | None
    """The disagreement rate over every OTHER instrument in the session — the null this one was
    tested against. `None` when no other instrument had a verifiable comparison, in which case
    there was nothing to be an outlier from."""
    null_intra_instrument_correlation: float | None
    """`rho-hat` over every OTHER instrument — how much of disagreement is a property of the
    instrument rather than of the bar. `0.0` is a real measurement (no excess dispersion, so
    the null is binomial); `None` means no null existed to estimate it from."""
    minimum_comparisons_to_reject: int | None
    """The fewest comparisons at which this instrument's own null could have refused it."""
    disagreement_upper_tail: float | None
    """`P(K >= disagreements | n)` under this instrument's own leave-one-out null. `None` when the
    verdict is `JOIN_UNVERIFIABLE`, because no test was run rather than one that passed.

    Renamed from `binomial_tail` by `A.123`: the number is no longer computed by a binomial, and a
    name that says otherwise is the kind of comment `B6` proved this engine cannot afford
    (`R.14`)."""
    median_deviation_paise: float | None
    median_spread_paise: float | None
    class_counts: dict[BarComparisonClass, int]
    volume_class_counts: dict[VolumeReconciliationClass, int]

    @property
    def agreement_fraction(self) -> float | None:
        """Agreements over verifiable comparisons, or `None` when nothing was verifiable.

        `None` rather than 1.0: an instrument with no evidence has no agreement rate, and a
        default of 1.0 would read as a clean bill of health on an empty tape.
        """
        if self.comparisons_verifiable == 0:
            return None
        return 1.0 - self.disagreements / self.comparisons_verifiable


@dataclass(frozen=True, slots=True)
class SessionJoinVerificationReport:
    """The session's answer, every number measured from the comparisons that were made."""

    session_date: date
    significance: float
    null_model: DisagreementNullModel
    """Which null produced every verdict here. Carried rather than assumed, so a store mixing two
    runs can say so (`B6`'s lesson, applied to the null itself)."""
    instruments: tuple[InstrumentJoinReport, ...]
    pooled_disagreement_rate: float
    """What disagreement looks like across the WHOLE session when only sampling is at work.

    Descriptive. Each instrument is judged against a leave-one-out null instead — see
    `InstrumentJoinReport.null_disagreement_rate` — because an instrument that contributes to
    its own null raises the bar it must clear and masks itself.
    """
    pooled_intra_instrument_correlation: float
    """`rho-hat` over the whole session — how much of disagreement belongs to the instrument
    rather than to the bar. The headline number for whether the binomial was ever adequate:
    `0.0` would say it was, and anything materially above it says the old null over-rejected
    by a factor of `1 + (n-1)rho` in variance."""
    minimum_comparisons_to_reject: int | None
    """The smallest `n` at which an all-disagreeing instrument could be refuted under the pooled
    null. `None` when the pooled rate makes rejection impossible at any `n`."""
    minimum_detectable_disagreement_rate: float
    """The operator's statement of what `JOIN_VERIFIED` CLAIMS: a verified instrument is one this
    session held enough evidence to have caught, had it disagreed at this rate. Policy, with no
    default, exactly like `significance` — it is not a threshold on the data but the definition of
    the claim being made."""
    minimum_comparisons_to_verify: int | None
    """The smallest `n` at which agreement is evidence rather than absence of evidence, under the
    POOLED null — the session's headline evidence demand.

    **Descriptive, not the gate.** Power is not monotone in `n`, so an instrument at or above this
    can still lack the required power, and each instrument is judged against its own leave-one-out
    null rather than this one. `None` when no reachable `n` gives the required power under the
    pooled null, which says the session demanded more evidence than any instrument could hold."""
    bars_total: int
    comparisons_verifiable: int
    disagreements_total: int
    class_counts: dict[BarComparisonClass, int]
    volume_class_counts: dict[VolumeReconciliationClass, int]
    deviation_in_tolerances_deciles: tuple[float, ...]

    def refuted_instruments(self) -> frozenset[int]:
        """The consumer surface (`R.06`).

        Shaped as `frozenset[int]` because that is exactly what
        `OrderBookSnapshotReplayEngine(inadmissible_instruments=…)` already takes: an
        instrument whose bar store and depth tape describe different markets is refused a
        replayed book, and so produces no paper fill.
        """
        return frozenset(
            report.instrument_token
            for report in self.instruments
            if report.verdict is InstrumentJoinVerdict.JOIN_REFUTED
        )

    def price_basis_divergences(self) -> tuple[InstrumentJoinReport, ...]:
        """The refused instruments whose bar series is on a DIFFERENT PRICE BASIS from the tape.

        Separated from the rest because the two need different actions: a basis divergence is a
        defect in a store and is repairable, while scatter is a property of sampling and is not.
        """
        return tuple(
            report
            for report in self.instruments
            if report.disagreement_shape is DisagreementShape.PRICE_BASIS_DIVERGENCE
        )

    def verdict_counts(self) -> dict[InstrumentJoinVerdict, int]:
        counts = dict.fromkeys(InstrumentJoinVerdict, 0)
        for report in self.instruments:
            counts[report.verdict] += 1
        return counts

    def describe(self) -> str:
        """One line, honest about the unverifiable share — a partial run must never read as a
        complete one."""
        counts = self.verdict_counts()
        verifiable_share = self.comparisons_verifiable / self.bars_total if self.bars_total else 0.0
        return (
            f"{self.session_date.isoformat()}: {len(self.instruments)} instruments — "
            f"{counts[InstrumentJoinVerdict.JOIN_VERIFIED]} verified, "
            f"{counts[InstrumentJoinVerdict.JOIN_REFUTED]} refuted, "
            f"{counts[InstrumentJoinVerdict.JOIN_UNVERIFIABLE]} unverifiable · "
            f"{self.comparisons_verifiable}/{self.bars_total} bars verifiable "
            f"({verifiable_share:.1%}) · pooled disagreement "
            f"{self.pooled_disagreement_rate:.4%} · intra-instrument correlation "
            f"{self.pooled_intra_instrument_correlation:.4f} · "
            f"{self.minimum_comparisons_to_reject} comparisons to refuse, "
            f"{self.minimum_comparisons_to_verify} to verify at a "
            # `:.0%` printed a claim of 0.004 as "0%" (`L1`).
            f"{self.minimum_detectable_disagreement_rate * 100:g}% detectable rate"
        )


# --------------------------------------------------------------------- estimators


def binomial_upper_tail(successes: int, trials: int, probability: float) -> float:
    """`P(X >= successes | trials, probability)`, exactly.

    Exact rather than normal-approximated because the counts that matter here are small: an
    instrument with eleven comparisons and three disagreements is precisely the case a normal
    approximation gets wrong, and it is the case the verdict turns on.

    Computed by summing the smaller tail and complementing when that is the shorter sum, so the
    result never loses precision to catastrophic cancellation at the ends.
    """
    if trials < 0:
        raise JoinVerificationError(f"trials cannot be negative; got {trials}")
    if not 0.0 <= probability <= 1.0:
        raise JoinVerificationError(f"probability must lie in [0, 1]; got {probability}")
    if successes <= 0:
        return 1.0
    if successes > trials:
        return 0.0
    # The degenerate nulls are answered directly rather than through the sum, where 0**0 and
    # log(0) both lurk. p=0 makes any success infinitely surprising; p=1 makes none surprising.
    if probability == 0.0:
        return 0.0
    if probability == 1.0:
        return 1.0

    # Computed in LOG space, term by term, for two separate reasons the review surfaced:
    #
    # - PRECISION: recovering a small tail as `1 - large_tail` cancels it away. Measured against
    #   exact `Fraction` arithmetic, that returned 1.78e-15 for a true 4.84e-25 (relative error
    #   3.7e9) and exactly 0.0 for a true 2.22e-20 (`docs/research/240`, `B5`).
    # - RANGE: summing the small tail directly with `math.comb` overflows — `comb(2000, 1000)` has
    #   no float representation at all. `lgamma` keeps every coefficient in range.
    #
    # The branch is on which tail is SMALLER (the upper one once `successes` exceeds the mean), so
    # the answer is always accumulated from its own terms rather than by subtraction.
    def log_binomial_term(count: int) -> float:
        return (
            math.lgamma(trials + 1)
            - math.lgamma(count + 1)
            - math.lgamma(trials - count + 1)
            + count * math.log(probability)
            + (trials - count) * math.log1p(-probability)
        )

    def tail_sum(lower_count: int, upper_count: int) -> float:
        logs = [log_binomial_term(count) for count in range(lower_count, upper_count + 1)]
        if not logs:
            return 0.0
        # Factor the largest term out before exponentiating, so no term underflows to zero on its
        # own and the sum keeps the digits of the dominant one.
        largest = max(logs)
        return math.exp(largest) * math.fsum(math.exp(value - largest) for value in logs)

    if successes > trials * probability:
        return min(1.0, tail_sum(successes, trials))
    return min(1.0, max(0.0, 1.0 - tail_sum(0, successes - 1)))


def smallest_trials_that_can_reject(probability: float, significance: float) -> int | None:
    """The fewest comparisons at which an all-disagreeing instrument would be refuted.

    Below this, `JOIN_UNVERIFIABLE` is the only honest verdict: the test could not have said
    otherwise, so a pass would be an artefact of the sample size rather than a finding.

    `None` when no `n` suffices — which happens at `probability = 1.0`, where the null says
    everything disagrees and nothing can be surprising.
    """
    if probability <= 0.0:
        return 1
    if probability >= 1.0:
        return None
    # p**n < significance  <=>  n > log(significance) / log(p), with log(p) negative.
    return max(1, math.floor(math.log(significance) / math.log(probability)) + 1)


LARGEST_TRIALS_SEARCHED_FOR_POWER = 1_000
"""How far `smallest_trials_that_can_verify` scans before answering `None`.

A session is 375 minutes, so a five-minute grid holds at most 75 bars per instrument and this is
more than an order of magnitude beyond any reachable comparison count — a structural bound from the
trading day, not a tuned one. Measured on the three recorded sessions the answer lands at 19-25;
a null needing more than a thousand is one that cannot verify anything, and says so."""

FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION = 2
"""Dispersion is variation BETWEEN instruments, so one instrument cannot exhibit any. Not a
threshold — the arity of the quantity being estimated."""

LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT = 2**53
"""Beyond this, `n + a` is not distinguishable from `n` in a float64, so a search for the trial
count that could reject has stopped meaning anything. A property of the float type rather than a
tuned bound (`R.03(e)`).

Tested against the NEXT bracket rather than the current one, so a returned `n` genuinely cannot
exceed it. Checking `upper` before doubling let the answer reach `2**54` — a constant that does not
bound what its name says is worse than no constant (`R.14`). In practice the positive-log-
probability stop fires first; this stays as the backstop for a null where it never would."""


class DisagreementNullModel(Enum):
    """Which null a verdict was produced under.

    Recorded beside every verdict for the same reason `B6` forced the staleness quantile to be
    recorded: it CHANGES the verdicts, so a store holding two of them is indistinguishable from a
    store holding either. `docs/research/241` §4.
    """

    BINOMIAL = "binomial"
    """Superseded 2026-08-16 (`A.123`). Kept as a value because rows written under it still exist,
    and a row that cannot say which null produced it is the failure being prevented."""

    BETA_BINOMIAL_LEAVE_ONE_OUT = "beta_binomial_leave_one_out"


@dataclass(frozen=True, slots=True)
class BetaBinomialDisagreementNull:
    """The distribution one instrument's disagreement count is judged against.

    A value rather than two loose floats, so a verdict and the null that produced it cannot drift
    apart — the recurring shape of `docs/research/240`'s findings.

    `π_i ~ Beta(a, b)` drawn once per instrument, `K_i | π_i ~ Binomial(n_i, π_i)`, so
    `E[π] = p`, `ICC = 1/(a+b+1) = rho`, and `Var(K_i) = n·p(1-p)·[1 + (n-1)rho]`. The bracketed
    variance inflation factor is the whole of the correction over a binomial: at `n = 40` and
    `rho = 0.10` the binomial understates the variance by 4.9x, which is the order of the
    over-rejection actually measured.

    `rho = 0` is a legitimate estimate and means the session showed no more dispersion than binomial
    — every method here then reduces to `binomial_upper_tail` exactly, and is tested against it.
    """

    disagreement_rate: float
    """`p` — the mean disagreement propensity, from the comparisons this null is built over."""
    intra_instrument_correlation: float
    """`rho` — the share of disagreement variance that belongs to the instrument rather than to the
    individual comparison. Estimated from the session (`DisagreementNullAccumulator`), never
    supplied."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.disagreement_rate <= 1.0:
            raise JoinVerificationError(
                f"disagreement rate must lie in [0, 1]; got {self.disagreement_rate}"
            )
        if not 0.0 <= self.intra_instrument_correlation < 1.0:
            raise JoinVerificationError(
                "intra-instrument correlation must lie in [0, 1); got "
                f"{self.intra_instrument_correlation}"
            )

    @property
    def beta_shape_for_disagreement(self) -> float:
        """`a = p(1-rho)/rho`. Undefined at `rho = 0`: a binomial null has no Beta."""
        if self.intra_instrument_correlation == 0.0:
            raise JoinVerificationError("a binomial null (rho = 0) has no Beta shape parameters")
        return (
            self.disagreement_rate
            * (1.0 - self.intra_instrument_correlation)
            / self.intra_instrument_correlation
        )

    @property
    def beta_shape_for_agreement(self) -> float:
        """`b = (1-p)(1-rho)/rho`."""
        if self.intra_instrument_correlation == 0.0:
            raise JoinVerificationError("a binomial null (rho = 0) has no Beta shape parameters")
        return (
            (1.0 - self.disagreement_rate)
            * (1.0 - self.intra_instrument_correlation)
            / self.intra_instrument_correlation
        )

    def _reduces_to_binomial(self, trials: int) -> bool:
        """Whether the variance inflation `1 + (n-1)rho` is representably different from 1.

        The comparison is against machine epsilon — a property of float64, not a tuned cutoff
        (`R.03(e)`). Below it the Beta parameterisation would only add rounding to an answer the
        binomial already gives exactly.
        """
        return (
            self.intra_instrument_correlation <= 0.0
            or max(trials - 1, 0) * self.intra_instrument_correlation <= sys.float_info.epsilon
        )

    def upper_tail(self, successes: int, trials: int) -> float:
        """`P(K >= successes | trials)` under this null, exactly.

        Summed in LOG space term by term, largest term factored out before exponentiating and
        accumulated with `math.fsum`. That construction is not decoration: `B5`
        (`docs/research/240`) measured the naive form returning exactly `0.0` for a true
        `2.22e-20`, and `scipy.stats.betabinom.sf` reproduces that same failure today
        (`docs/research/241` §5b), which is why its tail is not used here.

        Unlike the binomial tail there is no complement branch. The upper tail is always
        accumulated from its own terms, so it can never be recovered by a subtraction that cancels
        it away.
        """
        if trials < 0:
            raise JoinVerificationError(f"trials cannot be negative; got {trials}")
        if successes <= 0:
            return 1.0
        if successes > trials:
            return 0.0
        if self._reduces_to_binomial(trials):
            return binomial_upper_tail(successes, trials, self.disagreement_rate)
        if self.disagreement_rate == 0.0:
            return 0.0
        if self.disagreement_rate == 1.0:
            return 1.0

        shape_disagree = self.beta_shape_for_disagreement
        shape_agree = self.beta_shape_for_agreement
        log_beta_normaliser = _log_beta(shape_disagree, shape_agree)

        def log_term(count: int) -> float:
            return (
                math.lgamma(trials + 1)
                - math.lgamma(count + 1)
                - math.lgamma(trials - count + 1)
                + _log_beta(shape_disagree + count, shape_agree + (trials - count))
                - log_beta_normaliser
            )

        logs = [log_term(count) for count in range(successes, trials + 1)]
        largest = max(logs)
        return min(1.0, math.exp(largest) * math.fsum(math.exp(value - largest) for value in logs))

    def log_probability_everything_disagrees(self, trials: int) -> float:
        """`log P(K = n | n)` — the most surprising outcome `n` comparisons can produce.

        Closed form: `B(a+n, b)/B(a, b)`, which is strictly decreasing in `n`, so it is what
        `smallest_trials_that_can_reject` searches over. Asymptotically it decays like `n^(-b)` —
        POLYNOMIALLY, where the binomial's `p**n` decays geometrically. That single difference is
        what raises the evidence bar and retires `B1`.
        """
        if self._reduces_to_binomial(trials):
            if self.disagreement_rate <= 0.0:
                return -math.inf
            return trials * math.log(self.disagreement_rate)
        shape_disagree = self.beta_shape_for_disagreement
        shape_agree = self.beta_shape_for_agreement
        return _log_beta(shape_disagree + trials, shape_agree) - _log_beta(
            shape_disagree, shape_agree
        )

    def has_power_to_verify(
        self,
        trials: int,
        significance: float,
        minimum_detectable_disagreement_rate: float,
    ) -> bool:
        """Whether THIS many comparisons, under THIS null, could have caught the stated defect.

        **This is the gate, and `smallest_trials_that_can_verify` is not** (`H1` of the `A.124`
        review). Power is NOT monotone in `n` — the rejection boundary `k*` moves in integer steps,
        so the curve sawtooths — and therefore `n >= smallest_n_with_enough_power` does NOT imply
        this `n` has enough power. Under the shipped model at the `A.125` operating claim of 0.75,
        on 2026-08-11's own fitted null: the smallest sufficient `n` is **12**, and `n = 14` has
        power **0.98967** against a required 0.99. (The finding was first measured under the
        superseded independent alternative, where the bar was 22 and the trough at `n = 24` had
        power 0.9887; **ten instruments were `JOIN_VERIFIED` on exactly 24 comparisons** in the
        live store at the time, at a power the gate's own definition calls insufficient, while the
        dashboard printed the claim over them.)

        Asking the question at the instrument's own `n` removes the failure rather than bounding
        it, and it is cheaper: `k*` is small (4-6 at the operating claim) so this costs a few
        hundred log-terms, against a scan over every `n` up to the cap.
        """
        rejecting_count = self._fewest_disagreements_that_reject(trials, significance)
        if rejecting_count is None:
            return False
        # **Power under BOTH readings of the claim, and the weaker one governs** (`A.125` review,
        # `H1`). `M32` argued the alternative should carry the session's dependence, and that
        # argument stands — but it does NOT follow, as `docs/research/243` §2.2 claimed, that the
        # dependent model can only ever demand MORE evidence. Where the rejection boundary sits at
        # `k* = n`, power collapses to `P(K = n)`, and this module's own
        # `log_probability_everything_disagrees` records that the beta-binomial's decays
        # POLYNOMIALLY where the binomial's decays geometrically — so the dependent model has MORE
        # power there. Measured: at `p = 0.021679`, `rho = 0.6`, claim 0.995, the dependent bar is
        # **3** and the independent bar is **7**.
        #
        # The two are different readings of the same claim — a broken instrument whose rate is
        # exactly the claim (Binomial), or one whose rate is distributed around it (beta-binomial)
        # — and `/microstructure` prints the fixed-rate guarantee. Taking the smaller power honours
        # both, and costs nothing where they agree, which is everywhere the real sessions sit.
        return (
            min(
                self._alternative(minimum_detectable_disagreement_rate).upper_tail(
                    rejecting_count, trials
                ),
                binomial_upper_tail(rejecting_count, trials, minimum_detectable_disagreement_rate),
            )
            >= 1.0 - significance
        )

    def _alternative(
        self, minimum_detectable_disagreement_rate: float
    ) -> BetaBinomialDisagreementNull:
        """The broken instrument this test must be able to catch.

        **Beta-binomial at the SESSION'S OWN correlation, not Binomial** (`M32`, `A.125`). A
        Binomial alternative would reinstate on this side the exact independence assumption
        `A.123` removed from the null — and `docs/research/240`'s finding 1 ("one fact counted
        forty times") is about the sampling geometry, which applies to a broken instrument as much
        as to an ordinary one. Two comparisons of the same instrument minutes apart are not two
        independent facts about it whether it is broken or not.

        The claim is therefore read as a POPULATION MEAN rather than a point: "an instrument whose
        disagreement rate is around this, with the dispersion this session's instruments actually
        show". That is the conservative reading and it is the one that matches how the null is
        already parameterised, so both sides of the test now make the same assumption.

        Measured cost of getting this wrong, at a 0.75 claim: the Binomial alternative asks for
        8/8/9 comparisons across the three recorded sessions where this asks for 12/9/12. The
        shipped model was the PERMISSIVE one — instruments were being verified on less evidence
        than a dependence-aware alternative demands.

        At `rate = 1.0` the two coincide exactly (a deterministic all-disagreer disagrees on
        everything under any correlation), which is why the bar is 2 either way for the defect
        population actually observed so far.
        """
        if not 0.0 < minimum_detectable_disagreement_rate <= 1.0:
            # The pre-`M32` form raised here through `binomial_upper_tail`; a special-cased
            # `>= 1.0` arm silently accepted rates ABOVE 1 and answered `True` for them. This
            # method is public, so it validates rather than trusting its one internal caller.
            raise JoinVerificationError(
                "the minimum detectable disagreement rate must lie in (0, 1]; got "
                f"{minimum_detectable_disagreement_rate}"
            )
        # No `== 1.0` special case: `upper_tail` already short-circuits a rate of exactly 1, so
        # `Beta(1.0, rho)` and the degenerate point mass agree for every `(k, n)`. An arm that
        # cannot change an answer is one nothing can test (`L-9`).
        return BetaBinomialDisagreementNull(
            minimum_detectable_disagreement_rate, self.intra_instrument_correlation
        )

    def smallest_trials_that_can_verify(
        self,
        significance: float,
        minimum_detectable_disagreement_rate: float,
        largest_trials_worth_searching: int = LARGEST_TRIALS_SEARCHED_FOR_POWER,
    ) -> int | None:
        """The fewest comparisons at which AGREEMENT is evidence, rather than absence of evidence.

        **This is a different question from `smallest_trials_that_can_reject`, and conflating them
        is what `B1` actually was** (`docs/research/242` §2). That method asks *how many
        comparisons before I COULD refuse* — a SIZE question, and the right gate for
        `JOIN_REFUTED`. `JOIN_VERIFIED` needs the opposite: *how many AGREEING comparisons before
        absence of disagreement is evidence of agreement* — a POWER question, with a different
        answer. Two agreeing bars carry no power against anything except total disagreement, which
        is why 3,204 instruments could be verified with 15 of them resting on two bars.

        Returns the fewest `n` at which an instrument whose true disagreement rate is
        `minimum_detectable_disagreement_rate` would be refused with probability at least
        `1 - significance` — the same error rate the size gate obeys on the other side, so the two
        tails are treated alike and no second policy number is introduced.

        The alternative is evaluated under both readings of the claim — see `has_power_to_verify`;
        an earlier version of this docstring asserted a Binomial alternative, which `M32` retired.

        **The power curve is NOT monotone in `n`** (the rejection boundary moves in integer steps,
        so power sawtooths — measured 0.184 at n=20 against 0.169 at n=30), so this scans upward
        rather than bisecting. `None` when no `n` within the scan reaches the required power.
        """
        if not 0.0 < significance < 1.0:
            raise JoinVerificationError(f"significance must lie in (0, 1); got {significance}")
        if not 0.0 < minimum_detectable_disagreement_rate <= 1.0:
            raise JoinVerificationError(
                "the minimum detectable disagreement rate must lie in (0, 1]; got "
                f"{minimum_detectable_disagreement_rate}"
            )
        size_bar = self.smallest_trials_that_can_reject(significance)
        if size_bar is None:
            return None
        # `largest_trials_worth_searching` is handed the SESSION's own largest comparison count by
        # `verify_session`, because no instrument can be judged on more comparisons than the widest
        # one holds — a bound derived from the data rather than from the length of a trading day
        # (`M3`). Unbounded, this scan cost **82-240 seconds per session** at the measured
        # parameters and ~560s adversarially, all of it answering questions about sample sizes that
        # cannot occur: the widest real session's `MAX(comparisons_verifiable)` is 67.
        for trials in range(size_bar, largest_trials_worth_searching + 1):
            if self.has_power_to_verify(trials, significance, minimum_detectable_disagreement_rate):
                return trials
        return None

    def _fewest_disagreements_that_reject(self, trials: int, significance: float) -> int | None:
        """The smallest `k` at which `k` of `trials` disagreeing is refused. `None` if none is."""
        for count in range(trials + 1):
            if self.upper_tail(count, trials) < significance:
                return count
        return None

    def smallest_trials_that_can_reject(self, significance: float) -> int | None:
        """The fewest comparisons at which an all-disagreeing instrument would be refuted.

        Below this, `JOIN_UNVERIFIABLE` is the only honest verdict: the test could not have said
        otherwise, so a pass would report the sample size rather than the join.

        `None` when no `n` suffices — at `p = 1.0`, where nothing can be surprising, and whenever
        the required `n` exceeds what a float64 can distinguish.
        """
        if not 0.0 < significance < 1.0:
            raise JoinVerificationError(f"significance must lie in (0, 1); got {significance}")
        if self.disagreement_rate <= 0.0:
            return 1
        if self.disagreement_rate >= 1.0:
            return None
        if self.intra_instrument_correlation == 0.0:
            return smallest_trials_that_can_reject(self.disagreement_rate, significance)

        target = math.log(significance)
        if self.log_probability_everything_disagrees(1) < target:
            return 1
        # Exponential doubling to bracket the answer, then bisection inside the bracket. The
        # closed form is strictly decreasing in `n`, so both steps are exact rather than heuristic.
        upper = 1
        while self.log_probability_everything_disagrees(upper) >= target:
            # A log-probability cannot be positive. When the closed form returns one, the two
            # `lgamma` terms it differences have grown to ~1e17 and their true difference is below
            # float resolution, so the search would bisect on rounding NOISE and return a
            # fabricated finite answer — measured at 2,101,930,071,441,053, which was then written
            # to the store and rendered. A derived stop rather than a tuned bound: it fires exactly
            # when the arithmetic stops meaning anything, roughly three orders of magnitude before
            # `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT` would have.
            if (
                self.log_probability_everything_disagrees(upper) > 0.0
                or upper * 2 > LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT
            ):
                return None
            upper *= 2
        lower = upper // 2
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            if self.log_probability_everything_disagrees(middle) < target:
                upper = middle
            else:
                lower = middle
        return upper


def _verifiable_and_disagreements(comparisons: Sequence[BarComparison]) -> tuple[int, int]:
    """One instrument reduced to the two counts the null is built from: `(n_i, k_i)`."""
    verifiable = [
        comparison for comparison in comparisons if comparison.comparison_class.is_verifiable
    ]
    return len(verifiable), sum(
        1 for comparison in verifiable if comparison.comparison_class.is_disagreement
    )


def _log_beta(first_shape: float, second_shape: float) -> float:
    """`log B(x, y)` via `lgamma`, so no gamma is ever formed directly and nothing overflows.

    **The shapes are checked rather than trusted.** `math.lgamma(0.0)` raises a bare
    `ValueError: math domain error` from inside a verification, which says nothing about joins. It
    was reachable: written as `shape_agree + trials - count`, the expression evaluates
    left-to-right, and with `rho` at its clamp `shape_agree` is ~1.1e-16, so `shape_agree + trials`
    rounds to `trials` and the subtraction gives exactly 0.0. Association is not cosmetic here.
    """
    if first_shape <= 0.0 or second_shape <= 0.0:
        raise JoinVerificationError(
            f"Beta shapes must be positive; got ({first_shape}, {second_shape})"
        )
    return (
        math.lgamma(first_shape)
        + math.lgamma(second_shape)
        - math.lgamma(first_shape + second_shape)
    )


@dataclass(frozen=True, slots=True)
class DisagreementNullAccumulator:
    """The four running sums from which any instrument's leave-one-out null is taken in `O(1)`.

    **Why leave-one-out, for the dispersion as well as the rate.** An instrument that disagrees on
    everything raises the dispersion it is judged against and excuses itself — the identical
    self-masking the leave-one-out rate already prevents, and worse, because a raised `rho` widens
    the null for every instrument at once.

    **Why it is `O(1)` and not `O(N)` per instrument.** Expanding Pearson's `X²` and substituting
    `p̂ = S₂/S₃` collapses it to `(S₁ - p̂·S₂) / (p̂(1-p̂))`, so dropping one cluster is four
    subtractions. Over 9,000 instruments that is the difference between `O(N)` and `O(N²)`, and it
    is an identity rather than an approximation — pinned by a property test that recomputes the
    leave-one-out null from scratch and diffs it.
    """

    cluster_count: int
    """Instruments with at least one verifiable comparison. Those with none contribute no
    information and must not inflate the degrees of freedom."""
    verifiable_total: int
    """`S₃ = Σ n_i`."""
    disagreements_total: int
    """`S₂ = Σ k_i`."""
    squared_rate_contribution: float
    """`S₁ = Σ k_i²/n_i` — the only sum that is not a plain count, and the one that carries the
    between-instrument spread."""

    @classmethod
    def over(cls, clusters: Iterable[tuple[int, int]]) -> DisagreementNullAccumulator:
        """Build from `(verifiable, disagreements)` pairs, one per instrument."""
        accumulated = cls(0, 0, 0, 0.0)
        for verifiable, disagreements in clusters:
            accumulated = accumulated.including(verifiable=verifiable, disagreements=disagreements)
        return accumulated

    def including(self, *, verifiable: int, disagreements: int) -> DisagreementNullAccumulator:
        if disagreements < 0 or disagreements > max(verifiable, 0):
            raise JoinVerificationError(
                f"an instrument cannot disagree on {disagreements} of {verifiable} comparisons"
            )
        if verifiable <= 0:
            return self
        return DisagreementNullAccumulator(
            cluster_count=self.cluster_count + 1,
            verifiable_total=self.verifiable_total + verifiable,
            disagreements_total=self.disagreements_total + disagreements,
            squared_rate_contribution=self.squared_rate_contribution
            + disagreements * disagreements / verifiable,
        )

    def excluding(self, *, verifiable: int, disagreements: int) -> DisagreementNullAccumulator:
        """This accumulator with one instrument's contribution removed — its own null's source."""
        if verifiable <= 0:
            return self
        return DisagreementNullAccumulator(
            cluster_count=self.cluster_count - 1,
            verifiable_total=self.verifiable_total - verifiable,
            disagreements_total=self.disagreements_total - disagreements,
            squared_rate_contribution=self.squared_rate_contribution
            - disagreements * disagreements / verifiable,
        )

    def null(self) -> BetaBinomialDisagreementNull | None:
        """The null these clusters describe, or `None` when they describe nothing.

        `None` only when no comparison was verifiable — there is then nothing to be an outlier
        from, which is a different statement from a null that happens to be degenerate.
        """
        if self.verifiable_total <= 0:
            return None
        rate = self.disagreements_total / self.verifiable_total
        return BetaBinomialDisagreementNull(
            disagreement_rate=rate,
            intra_instrument_correlation=self._intra_instrument_correlation(rate),
        )

    def _intra_instrument_correlation(self, rate: float) -> float:
        """`rho-hat` by Pearson-χ² method of moments — `docs/research/241` §3.1.

        `E[X²] ≈ (N - 1) + rho·Σ(n_i - 1)` under the compound model, so
        `rho-hat = (X² - (N - 1)) / Σ(n_i - 1)`.

        Chosen over maximum likelihood because it is closed-form on nine thousand clusters and
        deterministic — no optimiser seed and no convergence branch, so a regression test can pin
        an exact number. Its cost is efficiency rather than bias, and it is re-estimated every
        session rather than carried, so an inefficient estimate is refreshed daily.

        Returns `0.0` — the binomial — whenever the data cannot speak to dispersion at all: one
        cluster, no within-cluster replication anywhere, or a degenerate rate under which `X²` is
        undefined. That is the honest answer, not a fallback.
        """
        raw_within_cluster_degrees = self.verifiable_total - self.cluster_count
        if (
            self.cluster_count < FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION
            or raw_within_cluster_degrees <= 0
        ):
            return 0.0
        if rate <= 0.0 or rate >= 1.0:
            return 0.0
        # The degree of freedom spent estimating `p` SCALES the sum rather than subtracting a bare
        # one, so the divisor carries the same `(N-1)/N` factor the numerator does. Measured over
        # 4,000 replications at a planted correlation of 0.200: without the factor the estimate
        # lands at 0.124 for N=3, 0.154 for N=5 and 0.175 for N=10, converging only by N=4,000;
        # with it, 0.187 / 0.193 / 0.195. The bias runs LOW, which NARROWS the null, which is
        # over-rejection — the exact failure this null replacement exists to remove, reappearing on
        # small sessions and on every `--limit` probe.
        within_cluster_degrees = (
            raw_within_cluster_degrees * (self.cluster_count - 1) / self.cluster_count
        )
        chi_square = (self.squared_rate_contribution - rate * self.disagreements_total) / (
            rate * (1.0 - rate)
        )
        correlation = (chi_square - (self.cluster_count - 1)) / within_cluster_degrees
        # A moment estimator of a correlation can land outside its own support on a finite sample.
        # Clamping low returns the binomial, which is the right null when the data show no excess
        # dispersion; clamping high stops just short of 1, where the Beta parameterisation would
        # divide by zero. Both bounds are the parameter's own support, not a tuned range.
        return min(max(correlation, 0.0), math.nextafter(1.0, 0.0))


def _deciles(values: Sequence[float]) -> tuple[float, ...]:
    """The nine interior deciles — the distribution describing its own shape (`R.03`).

    The same linear-interpolated form the replay engine's `_deciles` uses, so the two surfaces
    bin the same way. Fewer than ten samples cannot support them and nothing is reported.
    """
    if len(values) < MINIMUM_SAMPLES_FOR_A_DECILE:
        return ()
    ordered = sorted(values)
    deciles: list[float] = []
    for step in range(1, DECILE_COUNT):
        position = step / DECILE_COUNT * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        deciles.append(ordered[lower] * (1 - weight) + ordered[upper] * weight)
    return tuple(deciles)


def classify_disagreement_shape(
    comparisons: Sequence[BarComparison],
) -> tuple[DisagreementShape, float | None]:
    """Does ONE factor explain every disagreeing bar? Returns the shape, and the factor if so.

    **Constancy is the discriminator, not magnitude** (`docs/research/238`). Screening on how BIG a
    gap is cannot work: measured against NSE bhavcopy, the ordinary difference between a
    five-minute bar's close and the official closing price has a p99 of 4.75%, larger than the 4.9%
    adjustment this test exists to catch. Against the tape the noise floor is zero instead — the
    same quantity at the same instant, agreeing to the paise — so what stands out is a ratio that
    does not move.

    **The test is a fit, not a dispersion bound, and it reuses the tolerance already derived.**
    Take `factor` as the median ratio, then ask whether `factor x tape_price` reproduces each bar's
    close to within THAT BAR's own price tolerance — the instrument's median spread, measured from
    its own book. A first attempt bounded the ratio's dispersion by paise rounding and was wrong by
    a factor of six: a bar's close and the tape's last price are not the same trade, so even a
    perfectly rescaled series wobbles by the tick-level difference between them. The residual test
    absorbs that wobble because the tolerance is exactly the width those two ticks can differ by,
    and it introduces no constant of its own (`R.03`).

    **A factor must also move the price by more than the tolerance.** Otherwise `factor = 1` — two
    prices agreeing to the paise, refused on the BOOK bracket because the aligned book was crossed
    or one-sided — reads as a perfectly constant rescaling by one. The first real run classified 89
    instruments that way before this condition existed.

    Fewer than two disagreeing bars cannot show constancy at all, so those report
    `SPORADIC_DISAGREEMENT` rather than claiming a basis defect from a single point.
    """
    ratios: list[float] = []
    priced: list[tuple[int, int, float]] = []
    for comparison in comparisons:
        tape = comparison.tape_last_price_paise
        # ONLY bars whose close fell OUTSIDE the recorded book, and this is the load-bearing
        # restriction. A rescaled series puts the close somewhere the book never was; a bar close
        # sitting at the BID while the tape's last trade sits at the ASK also produces a stable
        # ratio, and is a side-of-book difference rather than a defect in the series. Measured on
        # the real tape: token 82945 fitted a factor of 0.99930 across seven bars, every one of
        # them with `bar_close == best_bid` and `tape_last == best_ask`. Restricting the fit to
        # out-of-book closes removes that whole family without a threshold.
        if comparison.comparison_class is not BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK:
            continue
        if not tape:
            continue
        # A bar whose instrument had no measurable spread has NO YARDSTICK, so it is EXCLUDED
        # rather than floored. The first version substituted 1 paise and the review showed that the
        # floor decided classifications; substituting NSE's 5-paise tick instead merely swapped one
        # constant for another, and the money-literal guard was right to reject it. The honest
        # reading is that such a bar carries no information about whether one factor explains the
        # gap, so it contributes none (`R.03` — nothing invented, nothing chosen).
        if not comparison.tolerance_paise:
            continue
        tolerance = float(comparison.tolerance_paise)
        ratios.append(comparison.bar_close_paise / tape)
        priced.append((comparison.bar_close_paise, tape, tolerance))
    if len(ratios) < MINIMUM_BARS_TO_SHOW_CONSTANCY:
        return (
            DisagreementShape.SPORADIC_DISAGREEMENT if ratios else DisagreementShape.NOT_APPLICABLE
        ), None

    factor = statistics.median(ratios)
    # The factor has to EXPLAIN the gap, not merely be near one: a factor whose effect is smaller
    # than the tolerance explains nothing about why the bars were refused. Judged ONCE against the
    # fit, on the median price and median tolerance.
    #
    # `B4` (`docs/research/240`) had this test INSIDE the loop with a `return`, so the FIRST bar
    # whose own book had momentarily widened aborted the entire fit — twenty bars cleanly rescaled
    # by 0.95 classified correctly until one bar's tolerance was widened to 8,000 paise, at which
    # point the whole instrument became `SPORADIC_DISAGREEMENT`. Which bar aborted depended on
    # arrival order, so the verdict was not even stable.
    median_price = statistics.median([price for _, price, _ in priced])
    median_tolerance = statistics.median([tolerance for _, _, tolerance in priced])
    if abs(factor - 1.0) * median_price <= median_tolerance:
        return DisagreementShape.SPORADIC_DISAGREEMENT, None
    residual_in_tolerances = [
        abs(bar_close_paise - factor * tape_price_paise) / tolerance
        for bar_close_paise, tape_price_paise, tolerance in priced
    ]
    # MEDIAN residual, not the worst one. The factor is fitted robustly, so it is judged robustly:
    # a single odd bar — a stale packet, a trade at the far side of a widened book — must not
    # overturn a fit that fifty-six other bars support. Measured on the real tape, the separation
    # is wide at the median and narrow at the max: `HINDPETRO`, a true rescaling, sits at 0.25
    # tolerances median against 1.58 max, while `HDFCBANK`, genuine scatter, sits at 2.02 median.
    # A max-based test rejected the true case; the median splits them by a factor of eight.
    # The comparison is `> 1`, and the 1 is a UNIT rather than a tuning knob: the residuals are
    # already expressed IN tolerances, so "median residual exceeds one tolerance" means "the fitted
    # factor fails to explain the typical bar to within the width the instrument's own book
    # allows". Rescaling the unit would rescale the residuals identically. The review flagged this
    # as `R.03`; the honest answer is that the DERIVED quantity is the tolerance and this line
    # compares against it exactly once, rather than against a multiple of it.
    if statistics.median(residual_in_tolerances) > 1.0:
        return DisagreementShape.SPORADIC_DISAGREEMENT, None
    return DisagreementShape.PRICE_BASIS_DIVERGENCE, factor


def _median_spread_paise(snapshots: Sequence[BookSnapshot]) -> float | None:
    """This instrument's own prevailing spread — the price tolerance, derived.

    The median rather than the mean: one crossed or one-sided book at the open would drag a mean
    far enough to excuse a real disagreement. `None` when no snapshot had two sides, in which
    case there is no spread to compare against and the caller falls back to exact equality.
    """
    spreads = [
        snapshot.spread_paise
        for snapshot in snapshots
        if snapshot.spread_paise is not None and snapshot.spread_paise >= 0
    ]
    if not spreads:
        return None
    return statistics.median(spreads)


# --------------------------------------------------------------------- the engine


class SessionSnapshotSource(Protocol):
    """What this engine needs from a depth tape — the seam, named for the two questions it asks.

    `OrderBookSnapshotReplayEngine` satisfies it, and is what production passes. Declaring the
    seam rather than the concrete class is what lets a test inject a hand-built tape without a
    cast, and it is also the shape `M25`'s streamed preload will have to satisfy when it replaces
    the per-instrument read.
    """

    def session_snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        """Every recorded book for this instrument this session. Raises `OrderBookReplayError`
        for an instrument the session has no rows for."""
        ...

    def staleness_threshold_millis_for(self, snapshots: Sequence[BookSnapshot]) -> float:
        """The instrument's OWN gap quantile — what "stale" means for this scrip."""
        ...

    def median_spread_paise_for(self, instrument_token: int) -> float | None:
        """The derived price tolerance, or `None` to let the engine measure it from the snapshots.

        A streamed source measures this over the FULL tape while handing back only the snapshots
        at the bar boundaries, so it must be able to say so; a source that hands back everything
        returns `None` and the engine takes the median itself.
        """
        ...


class BarTapeJoinVerificationEngine:
    """Compares one session's bar store against its depth tape and rules on each instrument.

    State carried across the run, and it IS the algorithm rather than an optimisation:

    - per instrument, its session snapshots and the derived staleness threshold and median
      spread computed from them once — the tolerances are properties of the instrument's own
      session, so they cannot be computed per bar without re-reading the tape per bar;
    - the pooled disagreement count and comparison count across every instrument, because the
      null each instrument is tested against is the session's own base rate and does not exist
      until every instrument has been compared;
    - the accumulated deviations in tolerance units, which supply the report's deciles.

    Two passes are therefore unavoidable and deliberate: compare everything, then judge. A
    single-pass version would have to test the first instrument against a null built from the
    first instrument.
    """

    def __init__(
        self,
        replay_engine: SessionSnapshotSource,
        *,
        session_date: date,
        significance: float,
        minimum_detectable_disagreement_rate: float,
    ) -> None:
        """Both policy inputs deliberately have no default.

        The estimator is derived from the session's own data; how surprised the operator has to
        be before an instrument's fills are refused is a risk appetite, not a measurement. Same
        reasoning, and same shape, as the replay engine's `staleness_quantile`.
        """
        if not 0.0 < significance < 1.0:
            raise JoinVerificationError(
                f"significance must lie in (0, 1), got {significance}: a verdict rule with no "
                f"room to be wrong on either side is not a test"
            )
        if not 0.0 < minimum_detectable_disagreement_rate <= 1.0:
            raise JoinVerificationError(
                "the minimum detectable disagreement rate must lie in (0, 1]; got "
                f"{minimum_detectable_disagreement_rate}"
            )
        self._replay_engine = replay_engine
        self._session_date = session_date
        self._significance = significance
        self._minimum_detectable_disagreement_rate = minimum_detectable_disagreement_rate

    # -- one instrument ---------------------------------------------------------

    def compare_instrument(
        self, instrument_token: int, bars: Sequence[StoredBar]
    ) -> tuple[BarComparison, ...]:
        """Every bar of one instrument, classified against the tape it will be filled on.

        Raises `JoinVerificationError` only when the tape cannot be read at all. An instrument
        the tape never recorded is not an error — it is a session-long
        `TAPE_ABSENT_IN_BAR_INTERVAL`, and reporting it as such is how the coverage limit
        (`M24`) stays visible instead of vanishing into a raised exception.
        """
        try:
            snapshots = sorted(
                self._replay_engine.session_snapshots_for(instrument_token),
                key=lambda snapshot: snapshot.receipt_time,
            )
        except OrderBookReplayError:
            snapshots = []

        if not snapshots:
            return tuple(
                self._absent_comparison(bar, BarComparisonClass.TAPE_ABSENT_IN_BAR_INTERVAL)
                for bar in bars
            )

        staleness_threshold_millis = self._replay_engine.staleness_threshold_millis_for(snapshots)
        # The source's own measurement wins when it has one: a streamed source measured the spread
        # over the whole tape, while these snapshots are only the ones at the bar boundaries.
        tolerance_paise = self._replay_engine.median_spread_paise_for(instrument_token)
        if tolerance_paise is None:
            tolerance_paise = _median_spread_paise(snapshots)

        comparisons: list[BarComparison] = []
        for bar in bars:
            comparisons.append(
                self._compare_one_bar(
                    bar=bar,
                    snapshots=snapshots,
                    staleness_threshold_millis=staleness_threshold_millis,
                    tolerance_paise=tolerance_paise,
                )
            )
        return tuple(comparisons)

    def _absent_comparison(
        self, bar: StoredBar, comparison_class: BarComparisonClass
    ) -> BarComparison:
        return BarComparison(
            instrument_token=bar.instrument_token,
            bar_timestamp=bar.bar_timestamp,
            comparison_class=comparison_class,
            bar_close_paise=bar.close_paise,
            tape_last_price_paise=None,
            best_bid_paise=None,
            best_ask_paise=None,
            deviation_paise=None,
            tolerance_paise=None,
            alignment_age_millis=None,
            volume_class=VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS,
            bar_volume=bar.volume,
            tape_volume_delta=None,
        )

    def _compare_one_bar(
        self,
        *,
        bar: StoredBar,
        snapshots: Sequence[BookSnapshot],
        staleness_threshold_millis: float,
        tolerance_paise: float | None,
    ) -> BarComparison:
        aligned = _last_snapshot_at_or_before(snapshots, bar.closing_instant)
        if aligned is None or aligned.receipt_time <= bar.bar_timestamp:
            # Nothing inside the bar. The boundary case is the one that matters and the
            # comparison is STRICT: a packet stamped at exactly the bar's opening instant
            # carries the last trade at or before the OPEN, which is the previous bar's close.
            # Admitting it would let every bar be "verified" against its predecessor's price and
            # would report agreement on an instrument the tape stopped recording an hour ago.
            return self._absent_comparison(bar, BarComparisonClass.TAPE_ABSENT_IN_BAR_INTERVAL)

        age_millis = (bar.closing_instant - aligned.receipt_time).total_seconds() * 1_000
        volume_class, volume_delta = _reconcile_volume(snapshots, bar)

        if age_millis > staleness_threshold_millis:
            return _with_alignment(
                self._absent_comparison(bar, BarComparisonClass.TAPE_STALE_AT_BAR_CLOSE),
                aligned=aligned,
                age_millis=age_millis,
                volume_class=volume_class,
                volume_delta=volume_delta,
            )

        deviation = abs(bar.close_paise - aligned.last_price_paise)
        # No two-sided book anywhere this session means no spread to derive a tolerance from,
        # so the comparison falls back to exact equality rather than inventing a width.
        effective_tolerance = int(tolerance_paise) if tolerance_paise is not None else 0

        bid, ask = aligned.best_bid_paise, aligned.best_ask_paise
        if aligned.last_price_paise <= 0:
            # No trade has happened yet, so there is no traded price to compare the close against.
            # A fact about the tape, not evidence about the join (`docs/research/240`, `B8`).
            comparison_class = BarComparisonClass.TAPE_HAS_NO_TRADE_YET
        elif bid is not None and ask is not None and bid > ask:
            # A crossed book has an empty bracket, so every close falls "outside" it. Blaming the
            # join for that is blaming the wrong store.
            comparison_class = BarComparisonClass.BOOK_CROSSED_AT_BAR_CLOSE
        elif bid is None or ask is None:
            comparison_class = BarComparisonClass.BOOK_ONE_SIDED_AT_BAR_CLOSE
        elif not (bid - effective_tolerance) <= bar.close_paise <= (ask + effective_tolerance):
            # The close is not a price this book could have traded at. Stronger evidence than
            # any last-price deviation, so it is classified first and separately.
            comparison_class = BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK
        elif deviation <= effective_tolerance:
            comparison_class = BarComparisonClass.AGREES_WITHIN_DERIVED_TOLERANCE
        else:
            comparison_class = BarComparisonClass.DISAGREES_BEYOND_DERIVED_TOLERANCE

        return BarComparison(
            instrument_token=bar.instrument_token,
            bar_timestamp=bar.bar_timestamp,
            comparison_class=comparison_class,
            bar_close_paise=bar.close_paise,
            tape_last_price_paise=aligned.last_price_paise,
            best_bid_paise=bid,
            best_ask_paise=ask,
            deviation_paise=deviation,
            tolerance_paise=effective_tolerance,
            alignment_age_millis=age_millis,
            volume_class=volume_class,
            bar_volume=bar.volume,
            tape_volume_delta=volume_delta,
        )

    # -- the session ------------------------------------------------------------

    def verify_session(
        self, bars_by_instrument: Mapping[int, Sequence[StoredBar]]
    ) -> SessionJoinVerificationReport:
        """Compare every instrument, then judge each against the session's own null.

        The two passes are the algorithm: the null an instrument is tested against is the
        pooled disagreement rate over ALL instruments, which does not exist until the first
        pass has finished.
        """
        comparisons_by_instrument: dict[int, tuple[BarComparison, ...]] = {}
        for instrument_token, bars in bars_by_instrument.items():
            comparisons_by_instrument[instrument_token] = self.compare_instrument(
                instrument_token, bars
            )

        every_comparison = [
            comparison
            for comparisons in comparisons_by_instrument.values()
            for comparison in comparisons
        ]
        verifiable = [
            comparison
            for comparison in every_comparison
            if comparison.comparison_class.is_verifiable
        ]
        disagreements_total = sum(
            1 for comparison in verifiable if comparison.comparison_class.is_disagreement
        )
        pooled_rate = disagreements_total / len(verifiable) if verifiable else 0.0

        # The accumulator is built ONCE over every instrument, and each instrument's own null is
        # taken from it by subtraction. That is what makes exact leave-one-out affordable at nine
        # thousand instruments — see `DisagreementNullAccumulator`.
        accumulator = DisagreementNullAccumulator.over(
            _verifiable_and_disagreements(comparisons)
            for comparisons in comparisons_by_instrument.values()
        )
        pooled_null = accumulator.null()
        minimum_trials = (
            pooled_null.smallest_trials_that_can_reject(self._significance)
            if pooled_null is not None
            else None
        )
        # REPORTING ONLY since `A.124`'s review. This is the session's headline "how much evidence
        # did verification demand", computed once from the pooled null so the surface has one number
        # to show. It does NOT decide any verdict — `_judge_instrument` asks `has_power_to_verify`
        # at each instrument's own comparison count against that instrument's own leave-one-out
        # null. The earlier form decided with this number and was wrong three ways at once.
        largest_comparison_count = max(
            (
                sum(1 for one in comparisons if one.comparison_class.is_verifiable)
                for comparisons in comparisons_by_instrument.values()
            ),
            default=0,
        )
        minimum_trials_to_verify = (
            pooled_null.smallest_trials_that_can_verify(
                self._significance,
                self._minimum_detectable_disagreement_rate,
                largest_trials_worth_searching=max(largest_comparison_count, 1),
            )
            if pooled_null is not None
            else None
        )

        instrument_reports = tuple(
            self._judge_instrument(
                instrument_token=instrument_token,
                comparisons=comparisons,
                session_accumulator=accumulator,
            )
            for instrument_token, comparisons in sorted(comparisons_by_instrument.items())
        )

        deviations_in_tolerances = [
            comparison.deviation_in_tolerances
            for comparison in verifiable
            if comparison.deviation_in_tolerances is not None
        ]

        return SessionJoinVerificationReport(
            session_date=self._session_date,
            significance=self._significance,
            null_model=DisagreementNullModel.BETA_BINOMIAL_LEAVE_ONE_OUT,
            instruments=instrument_reports,
            pooled_disagreement_rate=pooled_rate,
            pooled_intra_instrument_correlation=(
                pooled_null.intra_instrument_correlation if pooled_null is not None else 0.0
            ),
            minimum_comparisons_to_reject=minimum_trials,
            minimum_detectable_disagreement_rate=self._minimum_detectable_disagreement_rate,
            minimum_comparisons_to_verify=minimum_trials_to_verify,
            bars_total=len(every_comparison),
            comparisons_verifiable=len(verifiable),
            disagreements_total=disagreements_total,
            class_counts=_count_classes(every_comparison),
            volume_class_counts=_count_volume_classes(every_comparison),
            deviation_in_tolerances_deciles=_deciles(deviations_in_tolerances),
        )

    def _judge_instrument(
        self,
        *,
        instrument_token: int,
        comparisons: Sequence[BarComparison],
        session_accumulator: DisagreementNullAccumulator,
    ) -> InstrumentJoinReport:
        """Judge one instrument against a null built from every OTHER instrument.

        **Leave-one-out, and it is not a detail.** An instrument that contributes its own
        disagreements to the null it is tested against masks itself: the worse it is, the more
        it raises the bar it has to clear. With a few instruments that is the difference between
        catching a token collision and shrugging at it. Excluding it is the standard outlier
        construction and it is what makes the test a test of THIS instrument against the
        session, rather than of the session against itself.

        **Both parameters of the null are left out, not only the rate** (`A.123`). An instrument
        that disagrees on everything inflates the session's dispersion as surely as it inflates
        its rate, and an inflated dispersion widens the null it then walks through.
        """
        verifiable = [
            comparison for comparison in comparisons if comparison.comparison_class.is_verifiable
        ]
        disagreements = sum(
            1 for comparison in verifiable if comparison.comparison_class.is_disagreement
        )
        deviations = [
            comparison.deviation_paise
            for comparison in verifiable
            if comparison.deviation_paise is not None
        ]
        spreads = [
            comparison.tolerance_paise
            for comparison in verifiable
            if comparison.tolerance_paise is not None
        ]

        null = session_accumulator.excluding(
            verifiable=len(verifiable), disagreements=disagreements
        ).null()
        minimum_trials = (
            null.smallest_trials_that_can_reject(self._significance) if null is not None else None
        )

        if null is None or minimum_trials is None or len(verifiable) < minimum_trials:
            # Three ways the test could not have refused this instrument however badly it
            # disagreed — no other instrument to form a null from, a null that nothing can be
            # surprising against, or too few comparisons. A pass here would measure the sample
            # rather than the join, so it is reported as unverifiable. The third arm is what `B1`
            # turned out to be: it was always right, and it was reading the wrong distribution.
            verdict = InstrumentJoinVerdict.JOIN_UNVERIFIABLE
            tail: float | None = None
        else:
            tail = null.upper_tail(disagreements, len(verifiable))
            if tail < self._significance:
                verdict = InstrumentJoinVerdict.JOIN_REFUTED
            elif not null.has_power_to_verify(
                len(verifiable), self._significance, self._minimum_detectable_disagreement_rate
            ):
                # Not refused, and not enough AGREEING evidence for that to mean anything — the
                # `B1` arm (`A.124`). Ordered after the test on purpose: refusal is decided by the
                # size gate alone, so nothing that was refused before this gate existed stops being
                # refused, and `refuted_instruments()` is unchanged by construction.
                #
                # Asked at THIS instrument's own comparison count against THIS instrument's own
                # leave-one-out null, rather than by comparing to a session-wide minimum. Three
                # separate defects in the bar-comparison form, all closed by the same change:
                # power is not monotone in `n`, so `n >= bar` admitted instruments BELOW the
                # required power — 10 of them at n=24 and power 0.9887 in the live store (`H1`);
                # the bar came from the POOLED null while the test an instrument actually faces is
                # its LEAVE-ONE-OUT one, measured 35 against 167 on a small session rather than
                # differing "in the fourth decimal" as the old comment claimed (`M1`); and one
                # saturating instrument could clamp the pooled `rho-hat` to 1 and thereby veto
                # verification for the WHOLE session, including instruments whose own nulls verify
                # them cleanly (`M6`).
                verdict = InstrumentJoinVerdict.JOIN_UNVERIFIABLE
            else:
                verdict = InstrumentJoinVerdict.JOIN_VERIFIED

        shape, basis_ratio = (
            classify_disagreement_shape(comparisons)
            if verdict is InstrumentJoinVerdict.JOIN_REFUTED
            else (DisagreementShape.NOT_APPLICABLE, None)
        )

        return InstrumentJoinReport(
            instrument_token=instrument_token,
            verdict=verdict,
            disagreement_shape=shape,
            price_basis_ratio=basis_ratio,
            bars_total=len(comparisons),
            comparisons_verifiable=len(verifiable),
            disagreements=disagreements,
            null_disagreement_rate=null.disagreement_rate if null is not None else None,
            null_intra_instrument_correlation=(
                null.intra_instrument_correlation if null is not None else None
            ),
            minimum_comparisons_to_reject=minimum_trials,
            disagreement_upper_tail=tail,
            median_deviation_paise=statistics.median(deviations) if deviations else None,
            median_spread_paise=statistics.median(spreads) if spreads else None,
            class_counts=_count_classes(comparisons),
            volume_class_counts=_count_volume_classes(comparisons),
        )


# --------------------------------------------------------------------- streamed input


class PreloadedSessionSnapshots:
    """One streamed pass over the session, reduced to exactly what the comparison needs.

    **Why this exists (`M25`).** `OrderBookSnapshotReplayEngine.session_snapshots_for` filters the
    session's parquet to ONE instrument, so a caller that needs many pays a full scan of the day
    per instrument. On 2026-08-12's 1,420 instruments that ran past ninety minutes without
    finishing, and 2026-08-11 holds roughly nine thousand — the same wall the paper loop hit before
    `SteppedRecordedBookSource.preload` was written, and the same fix.

    **What is kept, and why keeping everything is not an option.** A session's tape runs to millions
    of rows; materialising every `BookSnapshot` would cost gigabytes. The comparison only ever asks
    two questions per bar — the last book at or before the bar's close, and the first at or after
    its open — so the pass keeps only the snapshots that answer them, at most two per bar boundary
    per instrument. Everything else is folded into two accumulators as it streams past:

    - **gaps**, from which each instrument's own staleness quantile is derived. Computed over the
      FULL stream, not over the survivors: a quantile taken over the reduced set would describe the
      bar grid rather than the feed, and would be wrong by orders of magnitude.
    - **spread samples**, whose median is the derived price tolerance — same reasoning.

    So `staleness_threshold_millis_for` deliberately ignores the list it is handed and answers from
    what the streaming pass measured, keyed by the instrument the list belongs to. That is the one
    place this class is not substitutable for the replay engine, and it is the correct behaviour
    rather than a shortcut: the engine's own answer, computed on the reduced list, would be a
    threshold for a feed that does not exist.
    """

    def __init__(
        self,
        boundary_snapshots: dict[int, list[BookSnapshot]],
        staleness_threshold_millis: dict[int, float],
        median_spread_paise: dict[int, float],
    ) -> None:
        self._snapshots = boundary_snapshots
        self._thresholds = staleness_threshold_millis
        self._spreads = median_spread_paise

    def session_snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        snapshots = self._snapshots.get(instrument_token)
        if not snapshots:
            raise OrderBookReplayError(
                f"instrument {instrument_token} has no rows in this session's tape"
            )
        return snapshots

    def staleness_threshold_millis_for(self, snapshots: Sequence[BookSnapshot]) -> float:
        """The threshold measured over the FULL stream for whichever instrument this list holds."""
        if not snapshots:
            return float("inf")
        return self._thresholds.get(snapshots[0].instrument_token, float("inf"))

    def median_spread_paise_for(self, instrument_token: int) -> float | None:
        """The price tolerance, measured over the full stream rather than over the survivors."""
        return self._spreads.get(instrument_token)


def preload_session_snapshots(
    tape_reader: MarketDepthTapeReader,
    *,
    session_date: date,
    instrument_tokens: Sequence[int],
    bars_by_instrument: Mapping[int, Sequence[StoredBar]],
    staleness_quantile: float,
    window_start: datetime,
    window_end: datetime,
) -> PreloadedSessionSnapshots:
    """Stream the session once and reduce it onto the bar boundaries.

    `staleness_quantile` is passed through rather than defaulted, and must be the SAME value the
    fill path runs at — a join verified under a looser threshold than fills obey would be verifying
    a market the loop never trades in.
    """
    if not 0.0 < staleness_quantile < 1.0:
        raise JoinVerificationError(
            f"staleness_quantile must lie in (0, 1), got {staleness_quantile}"
        )
    # The instants the comparison will ask about: each bar's open and its close, per instrument.
    wanted: dict[int, list[datetime]] = {}
    for token in instrument_tokens:
        instants: set[datetime] = set()
        for stored_bar in bars_by_instrument.get(token, ()):
            instants.add(stored_bar.bar_timestamp)
            instants.add(stored_bar.closing_instant)
        if instants:
            wanted[token] = sorted(instants)

    # B2 (`docs/research/240`): receipt TIMES are collected and sorted at the end rather than
    # differenced as they arrive. `iter_session_tables_for_instruments` yields batches in FILE
    # order, and a session holding several capture runs enumerates them out of order — the real
    # 2026-08-11 tape jumps BACKWARDS 5.34 hours between runs. Differencing on arrival and taking
    # `abs()` turned that into a ~19,200,000 ms phantom gap, inflating one instrument's derived
    # staleness threshold 3.9x and making stale books compare as fresh.
    receipt_times: dict[int, list[datetime]] = defaultdict(list)
    spreads: dict[int, list[int]] = defaultdict(list)
    # Per instrument, per wanted instant, the best candidate found so far on each side.
    at_or_before: dict[int, dict[datetime, BookSnapshot]] = defaultdict(dict)
    at_or_after: dict[int, dict[datetime, BookSnapshot]] = defaultdict(dict)

    for table in tape_reader.iter_session_tables_for_instruments(
        list(instrument_tokens), window_start, window_end, session_date
    ):
        for snapshot in book_snapshots_from_table(table):
            token = snapshot.instrument_token
            instants_for_token = wanted.get(token)
            if instants_for_token is None:
                continue
            receipt_times[token].append(snapshot.receipt_time)
            spread = snapshot.spread_paise
            if spread is not None and spread >= 0:
                spreads[token].append(spread)

            # Batches arrive in FILE order, not time order — `iter_session_tables_for_instruments`
            # says so — so every candidate is compared rather than assumed to arrive sorted.
            index = bisect_right(instants_for_token, snapshot.receipt_time)
            if index:
                instant = instants_for_token[index - 1]
                held = at_or_before[token].get(instant)
                if held is None or snapshot.receipt_time > held.receipt_time:
                    at_or_before[token][instant] = snapshot
                # B3 (`docs/research/240`): a snapshot landing EXACTLY on a wanted instant also
                # answers "first at or after" for that same instant. Without this it survived only
                # via the NEXT instant's at-or-after slot, so the LAST instant of the session lost
                # it entirely — and the runner widens the window past the close, so it never
                # survived as "latest overall" either. Latent on today's tape (microsecond receipt
                # stamps never land on a five-minute boundary) and live the moment receipt times
                # are coarsened or `exchange_time`, which is second-resolution, is used.
                if snapshot.receipt_time == instant:
                    held_after = at_or_after[token].get(instant)
                    if held_after is None or snapshot.receipt_time < held_after.receipt_time:
                        at_or_after[token][instant] = snapshot
            if index < len(instants_for_token):
                instant = instants_for_token[index]
                held = at_or_after[token].get(instant)
                if held is None or snapshot.receipt_time < held.receipt_time:
                    at_or_after[token][instant] = snapshot

    boundary: dict[int, list[BookSnapshot]] = {}
    thresholds: dict[int, float] = {}
    median_spreads: dict[int, float] = {}
    for token in wanted:
        survivors = {
            snapshot.receipt_time: snapshot
            for source in (at_or_before[token], at_or_after[token])
            for snapshot in source.values()
        }
        if not survivors:
            continue
        boundary[token] = sorted(survivors.values(), key=lambda shot: shot.receipt_time)
        ordered_times = sorted(receipt_times.get(token, []))
        thresholds[token] = _quantile(
            [
                (later - earlier).total_seconds() * 1_000
                for earlier, later in pairwise(ordered_times)
            ],
            staleness_quantile,
        )
        if spreads.get(token):
            median_spreads[token] = statistics.median(spreads[token])
    return PreloadedSessionSnapshots(boundary, thresholds, median_spreads)


def _quantile(values: Sequence[float], quantile: float) -> float:
    """The replay engine's own linear-interpolated quantile, on this token's own gaps.

    `inf` with too few gaps to have a distribution — the replay engine's rule, kept identical here
    so the two never disagree about what "stale" means for the same instrument.
    """
    if len(values) < MINIMUM_GAPS_FOR_A_QUANTILE:
        return float("inf")
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


# --------------------------------------------------------------------- input pipeline


def stored_bars_for_session(
    *,
    session_date: date,
    instrument_tokens: Sequence[int] | None = None,
    market_data: Path = DEFAULT_MARKET_DATA_PATH,
    bar_interval: str = BAR_INTERVAL,
) -> dict[int, tuple[StoredBar, ...]]:
    """Read the session's bars from the SAME table and column names the paper loop reads.

    Deliberately not routed through `bars_available_at`: that read filters on
    `availability_time <= at` for a decision instant, and this verification is not taken at a
    decision instant — it looks at the whole session after the fact. Reusing it would force a
    fabricated `at` and quietly drop the last bar of every session.

    The interval is read from `bar_interval` rather than assumed, so a store that later holds
    more than one interval cannot silently mix them into one comparison.

    Args:
        session_date: the trading day. Matched on the stored offset-carrying timestamp's own
            date prefix, which is IST — the same spelling the store was written with.
        instrument_tokens: restrict to these; `None` reads every instrument with a bar that day.
        market_data: the store. A parameter so a test can point at a copy (`R.03`).
        bar_interval: which interval to compare.

    Raises:
        JoinVerificationError: the store could not be read, or the interval is not one this
            function can convert to a duration.
    """
    interval = _interval_duration(bar_interval)
    query = (
        "SELECT instrument_token, bar_timestamp, close_price, volume FROM price_bars "
        "WHERE bar_interval = ? AND substr(bar_timestamp, 1, 10) = ?"
    )
    parameters: list[object] = [bar_interval, session_date.isoformat()]
    if instrument_tokens is not None:
        if not instrument_tokens:
            return {}
        placeholders = ",".join("?" * len(instrument_tokens))
        query += f" AND instrument_token IN ({placeholders})"
        parameters.extend(int(token) for token in instrument_tokens)
    query += " ORDER BY instrument_token, bar_timestamp"
    try:
        with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error as unreadable:
        raise JoinVerificationError(
            f"the bar store at {market_data} could not be read: {unreadable}"
        ) from unreadable

    bars_by_instrument: dict[int, list[StoredBar]] = defaultdict(list)
    for token, stamp, close_price, volume in rows:
        bars_by_instrument[int(token)].append(
            StoredBar(
                instrument_token=int(token),
                bar_timestamp=datetime.fromisoformat(str(stamp)),
                close_price=float(close_price),
                volume=int(volume),
                interval=interval,
            )
        )
    return {token: tuple(bars) for token, bars in bars_by_instrument.items()}


def _interval_duration(bar_interval: str) -> timedelta:
    """`"5m"` to five minutes, refusing anything it has not been told how to read.

    A defaulted duration would silently compare a bar's close against a book from the wrong
    instant, which is exactly the defect this whole engine exists to detect.
    """
    suffix_to_unit = {"m": "minutes", "h": "hours", "d": "days"}
    unit = suffix_to_unit.get(bar_interval[-1:])
    if unit is None or not bar_interval[:-1].isdigit():
        raise JoinVerificationError(
            f"unrecognised bar interval {bar_interval!r}: refusing to guess a duration, because "
            f"a wrong one aligns every bar against the wrong book"
        )
    return timedelta(**{unit: int(bar_interval[:-1])})


# --------------------------------------------------------------------- helpers


def _with_alignment(
    comparison: BarComparison,
    *,
    aligned: BookSnapshot,
    age_millis: float,
    volume_class: VolumeReconciliationClass,
    volume_delta: int | None,
) -> BarComparison:
    """Attach the evidence a stale alignment still carries.

    A stale comparison is unverifiable, but it is not empty: WHICH book was too old, and HOW old
    it was, is the measurement that tells a reader whether the capture or the market is at
    fault. Dropping it would leave `M24`'s coverage question unanswerable from this report.
    """
    return BarComparison(
        instrument_token=comparison.instrument_token,
        bar_timestamp=comparison.bar_timestamp,
        comparison_class=comparison.comparison_class,
        bar_close_paise=comparison.bar_close_paise,
        tape_last_price_paise=aligned.last_price_paise,
        best_bid_paise=aligned.best_bid_paise,
        best_ask_paise=aligned.best_ask_paise,
        deviation_paise=abs(comparison.bar_close_paise - aligned.last_price_paise),
        tolerance_paise=None,
        alignment_age_millis=age_millis,
        volume_class=volume_class,
        bar_volume=comparison.bar_volume,
        tape_volume_delta=volume_delta,
    )


def _last_snapshot_at_or_before(
    snapshots: Sequence[BookSnapshot], instant: datetime
) -> BookSnapshot | None:
    """Binary search over receipt-ordered snapshots.

    Linear scanning here is what turns a whole-session verification into an overnight job: the
    tape holds up to a million rows a session and every bar would re-walk all of them.
    """
    low, high = 0, len(snapshots)
    while low < high:
        middle = (low + high) // 2
        if snapshots[middle].receipt_time <= instant:
            low = middle + 1
        else:
            high = middle
    return snapshots[low - 1] if low else None


def _reconcile_volume(
    snapshots: Sequence[BookSnapshot], bar: StoredBar
) -> tuple[VolumeReconciliationClass, int | None]:
    """The tape's cumulative-volume increment across the bar, against the bar's own volume.

    Asymmetric by design (`docs/research/236`): sampling moves both endpoints inward, so the
    tape can only ever see the same trades or fewer. An increment LARGER than the bar's volume
    is impossible under that model and is therefore evidence, not coverage.
    """
    opening = _first_snapshot_at_or_after(snapshots, bar.bar_timestamp)
    closing = _last_snapshot_at_or_before(snapshots, bar.closing_instant)
    if opening is None or closing is None or closing.receipt_time <= opening.receipt_time:
        return VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS, None
    delta = closing.volume_traded - opening.volume_traded
    if delta < 0:
        # Cumulative volume ran backwards: a capture-run boundary, or two feeds spliced. Not a
        # comparison against the bar at all, so it is reported as having no usable endpoints.
        return VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS, delta
    if delta > bar.volume:
        return VolumeReconciliationClass.TAPE_EXCEEDS_BAR, delta
    return VolumeReconciliationClass.CONSISTENT_WITH_SAMPLING, delta


def _first_snapshot_at_or_after(
    snapshots: Sequence[BookSnapshot], instant: datetime
) -> BookSnapshot | None:
    low, high = 0, len(snapshots)
    while low < high:
        middle = (low + high) // 2
        if snapshots[middle].receipt_time < instant:
            low = middle + 1
        else:
            high = middle
    return snapshots[low] if low < len(snapshots) else None


def _count_classes(comparisons: Iterable[BarComparison]) -> dict[BarComparisonClass, int]:
    """Every class present with an explicit zero, so a missing key never reads as "not measured"."""
    counts = dict.fromkeys(BarComparisonClass, 0)
    for comparison in comparisons:
        counts[comparison.comparison_class] += 1
    return counts


def _count_volume_classes(
    comparisons: Iterable[BarComparison],
) -> dict[VolumeReconciliationClass, int]:
    counts = dict.fromkeys(VolumeReconciliationClass, 0)
    for comparison in comparisons:
        counts[comparison.volume_class] += 1
    return counts
