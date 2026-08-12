"""`L0.15` — reconciling brokers that disagree, without ever silently picking one.

Built against two cases measured on real data 2026-08-11, not against hypotheticals.

**Case 1, a coverage gap.** Fetching RELIANCE daily bars for 2026-08-07..11, Kite returned
three and Angel One returned two — Angel had no 08-07. Neither source is wrong; they hold
different history. A caller pinned to one broker simply loses that day, and loses it
silently, because a short answer looks exactly like a short window.

**Case 2, and the more dangerous one: partial agreement.** On the dates both held, the
closes matched EXACTLY (1327.3, 1320.6) while the volumes did not — 8,508,600 at Kite
against 8,701,285 at Angel for 08-11, a 2.3% difference. Prices agreeing does not mean
fields agree. Anything that checked `close` and concluded "the sources match" would import
a volume it never verified, and every volume-weighted calculation downstream would inherit
it with no signal that a choice was made.

So the reconciler grades disagreement by CONSEQUENCE rather than treating any difference
as one thing:

- `PRICE` — the sources disagree about what something traded at. A data-integrity alarm.
  Prices are the thing every decision is built on, and two brokers reporting different
  ones means at least one is wrong.
- `VOLUME_ONLY` — prices agree, turnover does not. Expected rather than alarming: brokers
  source volume from different vendors and differ on which trade types they count. It is
  recorded, never hidden, because a strategy that sizes on volume is affected by it.
- `NONE` — every field agrees, or only one source had the bar.

**Source preference is derived, never declared** (`R.03`). A fixed "trust Kite first" would
be a guess that never updates. Instead each source earns a reliability score from its own
measured record: how often it holds a bar at all, and how often it agrees with the other
sources when it does. A source that is frequently absent or frequently the odd one out
sinks on its own evidence.

**Nothing is discarded.** Every bar carries the source that supplied it, every rejected
alternative is counted, and a price disagreement is surfaced rather than resolved away —
the reconciler's job is to make the choice visible, not to make it quietly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from nse_algo_trader.bitemporal_bar_store import BarRecord
from nse_algo_trader.broker_credentials import BrokerName
from nse_algo_trader.historical_bars.historical_bar_source import (
    BarRequest,
    HistoricalBarSource,
    HistoricalBarSourceError,
)


class ReconciliationError(HistoricalBarSourceError):
    """Reconciliation could not produce an honest answer."""


class Disagreement(Enum):
    """How badly sources differ about one bar, graded by what it costs."""

    NONE = "agree"
    VOLUME_ONLY = "volume differs, prices agree"
    PRICE = "prices differ"

    @property
    def is_integrity_alarm(self) -> bool:
        """Only price disagreement means someone is actually wrong."""
        return self is Disagreement.PRICE


@dataclass(frozen=True)
class ReconciledBar:
    """One bar, the source it came from, and what the others said.

    `alternatives` is kept rather than dropped: when a price disagreement is investigated
    later, the rejected values are the whole investigation.
    """

    bar: BarRecord
    chosen_source: BrokerName
    contributing_sources: tuple[BrokerName, ...]
    disagreement: Disagreement
    alternatives: tuple[tuple[BrokerName, BarRecord], ...] = ()

    @property
    def was_single_sourced(self) -> bool:
        return len(self.contributing_sources) == 1


@dataclass
class SourceReliability:
    """One source's measured record. Earned, never configured."""

    broker: BrokerName
    bars_offered: int = 0
    bars_agreeing: int = 0
    bars_disagreeing_on_price: int = 0

    @property
    def agreement_rate(self) -> float:
        """Of the bars this source offered alongside others, how often it matched.

        Bars where it was the only source are excluded: agreeing with nobody is not
        evidence of accuracy, and counting it would reward a source for being alone.
        """
        compared = self.bars_agreeing + self.bars_disagreeing_on_price
        return self.bars_agreeing / compared if compared else 1.0

    def reliability_score(self, total_bars_seen: int) -> float:
        """Coverage x agreement.

        Both matter and neither substitutes for the other: a source that is always right
        but usually absent cannot be preferred, and one that always answers but often
        disagrees on price should not be. The product sinks on either failure.
        """
        if total_bars_seen == 0:
            return 0.0
        coverage = self.bars_offered / total_bars_seen
        return coverage * self.agreement_rate


@dataclass
class ReconciliationReport:
    """What reconciliation did, in terms a human can act on."""

    bars: list[ReconciledBar] = field(default_factory=list)
    reliability: dict[BrokerName, SourceReliability] = field(default_factory=dict)
    source_failures: dict[BrokerName, str] = field(default_factory=dict)

    @property
    def price_disagreements(self) -> list[ReconciledBar]:
        return [bar for bar in self.bars if bar.disagreement.is_integrity_alarm]

    @property
    def volume_disagreements(self) -> list[ReconciledBar]:
        return [bar for bar in self.bars if bar.disagreement is Disagreement.VOLUME_ONLY]

    @property
    def gap_filled_bars(self) -> list[ReconciledBar]:
        """Bars only one source held — the reason failover exists."""
        return [bar for bar in self.bars if bar.was_single_sourced]

    def describe(self) -> str:
        if not self.bars and not self.source_failures:
            return "no bars from any source"
        parts = [f"{len(self.bars):,} bars"]
        if self.gap_filled_bars:
            parts.append(f"{len(self.gap_filled_bars)} single-sourced")
        if self.volume_disagreements:
            parts.append(f"{len(self.volume_disagreements)} volume-only disagreements")
        if self.price_disagreements:
            parts.append(f"{len(self.price_disagreements)} PRICE DISAGREEMENTS")
        for broker, reliability in sorted(self.reliability.items(), key=lambda item: item[0].value):
            parts.append(
                f"{broker.value} coverage={reliability.bars_offered}"
                f"/agree={reliability.agreement_rate:.0%}"
            )
        for broker, failure in sorted(self.source_failures.items(), key=lambda i: i[0].value):
            parts.append(f"{broker.value} FAILED: {failure}")
        return " · ".join(parts)


def _prices_agree(left: BarRecord, right: BarRecord) -> bool:
    return (
        left.open_price == right.open_price
        and left.high_price == right.high_price
        and left.low_price == right.low_price
        and left.close_price == right.close_price
    )


MINIMUM_SOURCES_TO_DISAGREE = 2
"""One source cannot disagree with anything. Arithmetic, not a threshold."""


def classify_disagreement(candidates: Sequence[BarRecord]) -> Disagreement:
    """Grade a set of candidate bars for the same instant."""
    if len(candidates) < MINIMUM_SOURCES_TO_DISAGREE:
        return Disagreement.NONE
    reference = candidates[0]
    if any(not _prices_agree(reference, other) for other in candidates[1:]):
        return Disagreement.PRICE
    if any(other.volume != reference.volume for other in candidates[1:]):
        return Disagreement.VOLUME_ONLY
    return Disagreement.NONE


def reconcile_bars(
    bars_by_source: dict[BrokerName, list[BarRecord]],
    *,
    source_failures: dict[BrokerName, str] | None = None,
) -> ReconciliationReport:
    """Merge every source's bars into one series, recording every choice.

    The union of timestamps is taken, not the intersection: intersecting would silently
    discard exactly the bars failover exists to recover — 2026-08-07 in the measured case.
    """
    report = ReconciliationReport(source_failures=dict(source_failures or {}))
    reliability = {broker: SourceReliability(broker=broker) for broker in bars_by_source}

    candidates_by_moment: dict[datetime, list[tuple[BrokerName, BarRecord]]] = defaultdict(list)
    for broker, bars in bars_by_source.items():
        for bar in bars:
            candidates_by_moment[bar.bar_timestamp].append((broker, bar))

    total_moments = len(candidates_by_moment)
    disagreement_by_moment: dict[datetime, Disagreement] = {}

    # TWO passes, deliberately. Scoring while choosing would make the FIRST bar's choice
    # depend on almost no evidence and every later choice depend on how many bars happened
    # to precede it — the same input in a different date order would reconcile differently.
    # Reliability is a property of the whole window, so it is measured over the whole
    # window before anything is chosen.
    for moment, candidates in candidates_by_moment.items():
        disagreement = classify_disagreement([bar for _broker, bar in candidates])
        disagreement_by_moment[moment] = disagreement
        for broker, _bar in candidates:
            reliability[broker].bars_offered += 1
            if len(candidates) > 1:
                if disagreement.is_integrity_alarm:
                    reliability[broker].bars_disagreeing_on_price += 1
                else:
                    reliability[broker].bars_agreeing += 1

    for moment in sorted(candidates_by_moment):
        candidates = candidates_by_moment[moment]
        # Best measured record wins; ties break on broker name so the same input always
        # reconciles the same way. A result that depended on dict ordering would be
        # untestable and would drift between runs for no visible reason.
        chosen_broker, chosen_bar = max(
            candidates,
            key=lambda item: (
                reliability[item[0]].reliability_score(total_moments),
                item[0].value,
            ),
        )
        report.bars.append(
            ReconciledBar(
                bar=chosen_bar,
                chosen_source=chosen_broker,
                contributing_sources=tuple(broker for broker, _bar in candidates),
                disagreement=disagreement_by_moment[moment],
                alternatives=tuple(
                    (broker, bar) for broker, bar in candidates if broker is not chosen_broker
                ),
            )
        )

    report.reliability = reliability
    return report


def fetch_and_reconcile(
    sources: Sequence[HistoricalBarSource], request: BarRequest
) -> ReconciliationReport:
    """Ask every source, reconcile what came back, and record what did not.

    One source failing never fails the call — that is the entire point of failover — but
    the failure is RECORDED, so a permanently broken broker cannot masquerade as a broker
    that simply had no data for the window.
    """
    if not sources:
        raise ReconciliationError("no sources configured")

    bars_by_source: dict[BrokerName, list[BarRecord]] = {}
    failures: dict[BrokerName, str] = {}
    for source in sources:
        if not source.supports(request.interval):
            failures[source.broker] = f"cannot express {request.interval.value}"
            continue
        try:
            bars_by_source[source.broker] = source.fetch_bars(request)
        except HistoricalBarSourceError as failure:
            failures[source.broker] = str(failure)

    if not bars_by_source and failures:
        raise ReconciliationError(
            "every source failed: "
            + "; ".join(f"{broker.value}: {reason}" for broker, reason in failures.items())
        )

    return reconcile_bars(bars_by_source, source_failures=failures)
