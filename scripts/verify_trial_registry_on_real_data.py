#!/usr/bin/env python
"""`L2.01` `R.05` — reconstruct the search size the retained trades can PROVE, and chain it.

Engine: `nse_algo_trader.validation.honest_trial_registry`. Spec: `docs/research/245`.

**What this does and does not claim.** `experience_memory.sqlite3` holds 3,481 retained closed
trades. Those are trades, not trials: `experiment_id` is unique per trade, so counting them as
hypotheses would inflate the search size by three orders of magnitude and make every `F06` gate
*harsher* than the truth — the opposite failure to the one the registry exists to prevent, and just
as wrong.

What the store CAN prove is the set of distinct declared mechanisms: a `(strategy_tag,
mechanism_name)` pair is one hypothesis about why an edge should exist, which is exactly the unit
`L2.11`'s mechanism hurdle counts.

**The reconstructed number is a FLOOR, not a count.** Every configuration that was tried and
abandoned before it ever produced a trade left no trace in a store of trades — and those are
precisely the trials `L2.01` exists to stop going missing. So this pass establishes the minimum the
history admits to, records it as such, and the honest count begins here.

Usage:
    python scripts/verify_trial_registry_on_real_data.py
    python scripts/verify_trial_registry_on_real_data.py --registry /tmp/probe.sqlite3
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nse_algo_trader.validation.honest_trial_registry import (
    DEFAULT_TRIAL_REGISTRY,
    HonestTrialRegistry,
    TrialOutcome,
)

DEFAULT_EXPERIENCE_MEMORY = Path("~/.nse_algo_trader/experience_memory.sqlite3").expanduser()

RECONSTRUCTED_SEARCH = "reconstructed-from-retained-trades"
"""The search name these trials are filed under, so a reader can always separate what was
RECONSTRUCTED from a store of trades from what was RECORDED as it happened. A gate that wanted only
honestly-recorded trials can exclude this one by name."""


UNIVERSE_SUFFIX = re.compile(r"\s*\[(index|stock)\]\s*$")
"""A universe label a later code version appended to the mechanism text.

`M4` of the `A.128` review: three of the nine `(strategy_tag, mechanism_name)` pairs are ONE
mechanism spelled three ways — `... range-bound regime`, `... [index]`, `... [stock]` — and the
same for the ORB option mechanism. Counting them separately overstates the search size by 80%
(9 against 5), and this script's own docstring says inflating `N` makes every gate harsher than the
truth and is "just as wrong". A schema change is not hypothesis generation. The universe is
already carried in `instrument_kind`; the reason an edge should exist is identical across the three
spellings."""


def declared_mechanisms(experience_memory: Path) -> list[tuple[str, int, float | None]]:
    """Every distinct DECLARED MECHANISM with its trade count and mean realised return.

    One row is one hypothesis the history can prove was tested. The universe suffix is normalised
    away first (`M4`), so a mechanism relabelled by a later code version counts once. The mean
    realised return is carried as the trial's fitness because it is what a search would have been
    selecting on.
    """
    with sqlite3.connect(f"file:{experience_memory}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT strategy_tag, mechanism_name, COUNT(*), SUM(realized_return_fraction), "
            "SUM(realized_return_fraction IS NOT NULL) "
            "FROM experience_nodes GROUP BY strategy_tag, mechanism_name"
        ).fetchall()

    merged: dict[str, tuple[int, float, int]] = {}
    for tag, mechanism, count, total, scored in rows:
        name = f"{tag} :: {UNIVERSE_SUFFIX.sub('', str(mechanism))}"
        trades, summed, with_a_score = merged.get(name, (0, 0.0, 0))
        merged[name] = (
            trades + int(count),
            summed + (0.0 if total is None else float(total)),
            with_a_score + int(scored or 0),
        )
    return [
        (name, trades, (summed / with_a_score) if with_a_score else None)
        for name, (trades, summed, with_a_score) in sorted(merged.items())
    ]


def reconstruct(
    registry: HonestTrialRegistry, mechanisms: list[tuple[str, int, float | None]]
) -> int:
    """Record every mechanism not already recorded, and return how many were added.

    **Idempotent per ITEM, not per search** (`H4`). The first version skipped the whole loop when
    the search had ANY recorded trial, so a run that died after mechanism 1 of 9 could never be
    resumed: the registry stayed permanently short by eight hypotheses and the script exited 0.
    An engine whose entire purpose is that `N` must not be flattered shipped a reconstruction pass
    whose failure mode was a flattered `N` reporting success.

    Append-only is preserved — nothing is deleted, and a mechanism already present is simply not
    recorded twice.
    """
    already = registry.recorded_parameters(search=RECONSTRUCTED_SEARCH)
    added = 0
    for name, _trades, mean_return in mechanisms:
        if name in already:
            continue
        registry.record(
            search=RECONSTRUCTED_SEARCH,
            parameters=name,
            # `M5`: a mechanism with no realised return has no score. Recording a fabricated 0.0
            # would have made a mechanism with NO DATA the third-best hypothesis in the registry,
            # since seven of the nine real ones have a negative mean.
            outcome=(TrialOutcome.COMPLETED if mean_return is not None else TrialOutcome.DISCARDED),
            fitness=mean_return,
            ran_at=datetime.now(UTC),
        )
        added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experience-memory", type=Path, default=DEFAULT_EXPERIENCE_MEMORY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_TRIAL_REGISTRY)
    arguments = parser.parse_args()

    if not arguments.experience_memory.exists():
        print(f"no experience memory at {arguments.experience_memory} — nothing to reconstruct")
        return 1

    with sqlite3.connect(f"file:{arguments.experience_memory}?mode=ro", uri=True) as connection:
        trades = connection.execute("SELECT COUNT(*) FROM experience_nodes").fetchone()[0]
        distinct_experiments = connection.execute(
            "SELECT COUNT(DISTINCT experiment_id) FROM experience_nodes"
        ).fetchone()[0]

    mechanisms = declared_mechanisms(arguments.experience_memory)
    print(f"retained closed trades      : {trades:,}")
    print(f"distinct experiment_id      : {distinct_experiments:,}  <- per TRADE, not per trial")
    print(f"distinct declared mechanisms: {len(mechanisms)}  <- the hypotheses the store can prove")
    print()

    registry = HonestTrialRegistry(arguments.registry)
    added = reconstruct(registry, mechanisms)
    print(f"recorded {added} new mechanism(s); {len(mechanisms) - added} were already present")
    for name, count, mean_return in mechanisms:
        score = "no realised return" if mean_return is None else f"mean {mean_return:+.5f}"
        print(f"  {name[:70]:<70} {count:>6,} trades  {score}")

    recorded = registry.cumulative_trials(search=RECONSTRUCTED_SEARCH)
    if recorded != len(mechanisms):
        print(
            f"\nINCOMPLETE: {recorded} of {len(mechanisms)} mechanisms are recorded. The count is "
            f"SHORT and must not be used as a search size."
        )
        return 1

    print()
    print(f"cumulative trials in the registry: {registry.cumulative_trials():,}")
    for outcome, count in registry.trials_by_outcome().items():
        print(f"  {outcome.value:<10} {count:>4}")

    broken = registry.verify_chain()
    if broken is not None:
        print(f"\nCHAIN BROKEN at sequence {broken} — a trial was removed or edited")
        return 1
    print("\nchain verified over the whole registry")
    print(
        "\nThis count is a FLOOR. Every configuration tried and abandoned before it produced\n"
        "a trade left no trace in a store of trades, and those are exactly the trials `L2.01`\n"
        "exists to stop going missing. The honest count begins here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
