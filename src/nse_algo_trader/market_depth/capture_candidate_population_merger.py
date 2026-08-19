"""Merge cash and derivative capture candidates into ONE ranked population (`R.10`, `R.13`).

**The defect this exists to prevent.** `DepthCaptureAdmissionController` solves a knapsack by value
density — `liquidity_value / projected_bytes`. Until now it saw one population, NSE cash, whose
`liquidity_value` was rupees of traded value from the cash bhavcopy. Widening the capture to F&O
(`A.142`) hands it a second population whose liquidity is also "rupees" and is NOT the same
quantity: measured on the real 2026-08-18 session, the top index-option contract shows
**₹891,906,493,663** of traded value against the top index future's **₹53,465,513,647** and a liquid
cash name's far smaller number, because an option's turnover is notional exposure on the underlying
while a cash trade's is money changing hands.

Sorting those together is `R.13`'s error in its purest form — *correctness of execution is not
evidence of correctness of allocation*. A single descending sort would put every option above every
cash instrument and evict the coverage the only bot currently trading depends on, and it would do it
while looking like a well-solved knapsack.

**What is done instead.** Each population is ranked WITHIN ITSELF and the rank is turned into a
standing in `(0, 1]`: the most liquid member of every population stands at 1.0, the least at `1/n`.
The controller then compares like with like — the best cash instrument against the best option
against the best future — and the budget decides how deep into each it can afford to go. That is
`R.10`'s "the six segments equal by default" made mechanical rather than asserted: no segment is
privileged, and none is drowned by another's unit of measure.

**What it deliberately does NOT do.** It sets no quota and caps no segment. Standing is the only
thing it produces; the cut belongs to the controller, which is the thing that knows the packet rates
and the disk.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nse_algo_trader.market_depth.depth_capture_admission_controller import (
    InstrumentCaptureCandidate,
)


class CapturePopulationMergeError(Exception):
    """The populations cannot be merged — an empty name, or one token claimed by two of them."""


@dataclass(frozen=True)
class CaptureCandidateEntry:
    """One instrument as its own population measures it, before any cross-population comparison."""

    instrument_token: int
    liquidity_in_its_own_units: float
    measured_packets_per_second: float | None = None


@dataclass(frozen=True)
class CaptureCandidatePopulation:
    """A set of candidates whose liquidity numbers are comparable to EACH OTHER and nothing else."""

    name: str
    entries: Sequence[CaptureCandidateEntry]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise CapturePopulationMergeError("a population must be named to be diagnosable")


@dataclass(frozen=True)
class MergedCaptureCandidates:
    """The merged ranking, plus what each population contributed — never a bare list.

    `size_by_population` is what makes a later admission decision auditable: knowing that 4,522 of
    9,000 admitted slots went to options is the difference between a solved budget and an unnoticed
    eviction.
    """

    candidates: tuple[InstrumentCaptureCandidate, ...]
    population_by_token: Mapping[int, str]
    size_by_population: Mapping[str, int]

    def tokens_of(self, population: str) -> tuple[int, ...]:
        return tuple(
            token for token, name in self.population_by_token.items() if name == population
        )

    def describe(self) -> str:
        parts = ", ".join(
            f"{name} {count:,}"
            for name, count in sorted(self.size_by_population.items(), key=lambda item: -item[1])
        )
        return (
            f"{len(self.candidates):,} candidate(s) across "
            f"{len(self.size_by_population)} population(s): {parts}"
        )


def merge_capture_candidate_populations(
    populations: Sequence[CaptureCandidatePopulation],
) -> MergedCaptureCandidates:
    """One ranked population in which standing is measured within a segment, never across segments.

    Standing is `(n - rank) / n` over the population's own descending liquidity order, so it is
    strictly positive — a zero would make the candidate's value density zero and quietly guarantee
    its rejection, which is a cut disguised as arithmetic.

    Ties inside a population are broken by instrument token, so two runs over the same input produce
    the same order and a capture restart does not resubscribe a different set for no reason.
    """
    seen: dict[int, str] = {}
    merged: list[InstrumentCaptureCandidate] = []
    sizes: dict[str, int] = {}

    for population in populations:
        entries = sorted(
            population.entries,
            key=lambda entry: (-entry.liquidity_in_its_own_units, entry.instrument_token),
        )
        size = len(entries)
        sizes[population.name] = size
        if size == 0:
            continue
        for rank, entry in enumerate(entries):
            claimed_by = seen.get(entry.instrument_token)
            if claimed_by is not None:
                raise CapturePopulationMergeError(
                    f"token {entry.instrument_token} is claimed by both {claimed_by!r} and "
                    f"{population.name!r}; a duplicate subscription wastes a slot in a budget the "
                    f"controller solves exactly"
                )
            seen[entry.instrument_token] = population.name
            merged.append(
                InstrumentCaptureCandidate(
                    instrument_token=entry.instrument_token,
                    liquidity_value=(size - rank) / size,
                    measured_packets_per_second=entry.measured_packets_per_second,
                )
            )

    merged.sort(key=lambda candidate: (-candidate.liquidity_value, candidate.instrument_token))
    return MergedCaptureCandidates(
        candidates=tuple(merged),
        population_by_token=dict(seen),
        size_by_population=sizes,
    )
