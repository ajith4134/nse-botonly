"""Assign admitted tokens to websocket connections so a loud feed cannot starve a quiet one.

**The measurement that produced this module.** On 2026-08-19 the depth capture was widened from
cash-only to cash plus F&O (`A.142`). The admission controller admitted 9,000 tokens and sharded
them across three Kite connections by their global standing, so every connection carried a mix.
Eleven minutes later the tape read:

| shard | segment | tokens | ticks |
|---|---|---|---|
| 01 | `NFO-OPT` | 1,976 | 441,884 |
| 01 | `NSE` | 934 | **1,446** |
| 02 | `NFO-OPT` | 2,583 | 240,583 |
| 02 | `NSE` | 300 | **411** |
| 03 | `NFO-OPT` | 1,713 | 74,298 |
| 03 | `NSE` | 1,210 | **1,317** |

**1.3 ticks per cash instrument against 114 per option**, uniformly across all three connections —
and the immediately preceding cash-only run on the same box, the same session and the same account
had delivered **1,153,998 ticks over 2,295 cash instruments in 42 minutes**. The cash feed had not
broken; it was being crowded off a saturated socket by a derivative book that updates two orders of
magnitude more often.

**The contention is between CONTENTION GROUPS, not between segments.** One socket carries one
stream and its loudest subscriber sets what everyone else gets, so the thing that must not be mixed
is a quiet feed with a loud one. Futures are as loud as options — measured the same session at 328
ticks per instrument against the option chain's 114 — so all five derivative segments belong to one
group and cash to another. Splitting per SEGMENT instead was tried first and failed in the opposite
direction: with five segments and three connections, cash took every connection and the derivative
bots got nothing (`shard 01 cash 3,000 · shard 02 cash 3,000 · shard 03 cash 259`).

**How connections are divided.** Proportionally to each group's admitted token count, by largest
remainder, with **every non-empty group guaranteed at least one connection**. The guarantee is the
part that matters: a proportional split alone would hand all three connections to whichever group
the controller happened to admit more of, which is exactly the failure above.

**What it costs, stated rather than hidden.** Connections are whole units, so a group of 2,444 on a
3,000-instrument connection strands 556 slots and a group larger than its allocation loses its tail.
Both are REPORTED (`R.11`) instead of quietly capturing fewer instruments than the controller solved
for.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

QUIET_GROUP: Final = "cash"
"""The group that loses its connections last when the division is uneven.

Not a preference: cash is the only segment whose bot has a closed-trade record, and its strategy is
the only one that decides on a five-minute bar built from the tape itself. Named as a constant so
the choice is visible and revisable rather than implied by dictionary ordering.
"""

LOUD_GROUP: Final = "derivatives"

DEFAULT_GROUP_BY_POPULATION: Final = {
    "cash": QUIET_GROUP,
    "index_options": LOUD_GROUP,
    "stock_options": LOUD_GROUP,
    "index_futures": LOUD_GROUP,
    "stock_futures": LOUD_GROUP,
    "commodity_mcx": LOUD_GROUP,
}


class CaptureShardPlanError(Exception):
    """The plan cannot be made — no connections, or a token with no population."""


@dataclass(frozen=True)
class CaptureShardAssignment:
    """One websocket connection's worth of instruments, all from one contention group."""

    shard_index: int
    group: str
    tokens: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.tokens)


@dataclass(frozen=True)
class CaptureShardPlan:
    """Every connection's assignment, plus exactly what did not fit and why."""

    assignments: tuple[CaptureShardAssignment, ...]
    unplaced_by_population: Mapping[str, int]
    connections_by_group: Mapping[str, int]
    stranded_slots: int
    """Connection capacity lost to the one-group-per-connection guarantee."""

    @property
    def planned_tokens(self) -> int:
        return sum(assignment.size for assignment in self.assignments)

    @property
    def unplaced_tokens(self) -> int:
        return sum(self.unplaced_by_population.values())

    def tokens_of_group(self, group: str) -> tuple[int, ...]:
        placed: list[int] = []
        for assignment in self.assignments:
            if assignment.group == group:
                placed.extend(assignment.tokens)
        return tuple(placed)

    def describe(self) -> str:
        parts = [
            f"shard {assignment.shard_index:02d} {assignment.group} {assignment.size:,}"
            for assignment in self.assignments
        ]
        line = f"{self.planned_tokens:,} token(s) across {len(self.assignments)} connection(s): "
        line += " · ".join(parts)
        if self.stranded_slots:
            line += f"; {self.stranded_slots:,} slot(s) stranded to keep one group per connection"
        if self.unplaced_by_population:
            dropped = ", ".join(
                f"{population} {count:,}"
                for population, count in sorted(
                    self.unplaced_by_population.items(), key=lambda item: -item[1]
                )
            )
            line += f"; {self.unplaced_tokens:,} did not fit ({dropped})"
        return line


def plan_capture_shards(
    admitted_tokens: Sequence[int],
    population_by_token: Mapping[int, str],
    *,
    instruments_per_connection: int,
    maximum_connections: int,
    first_shard_index: int = 1,
    group_by_population: Mapping[str, str] | None = None,
) -> CaptureShardPlan:
    """Group the admitted tokens into connections, one contention group per connection.

    `admitted_tokens` keeps its incoming order INSIDE each group, so the controller's ranking still
    decides which instruments survive a shortage — this only decides which connection they sit on,
    and therefore what they have to share bandwidth with.

    A population not named in `group_by_population` falls into `LOUD_GROUP`: a new derivative
    segment must never default into the quiet group and starve cash by omission.
    """
    if instruments_per_connection <= 0:
        raise CaptureShardPlanError("instruments_per_connection must be positive")
    if maximum_connections <= 0:
        raise CaptureShardPlanError("maximum_connections must be positive")

    groups = dict(group_by_population or DEFAULT_GROUP_BY_POPULATION)
    tokens_by_group: dict[str, list[int]] = {}
    populations_by_group: dict[str, list[str]] = {}
    for token in admitted_tokens:
        population = population_by_token.get(token)
        if population is None:
            raise CaptureShardPlanError(
                f"token {token} has no population; an unattributed token cannot be kept off a "
                f"connection it would crowd"
            )
        group = groups.get(population, LOUD_GROUP)
        tokens_by_group.setdefault(group, []).append(token)
        populations_by_group.setdefault(group, []).append(population)

    allocation = _connections_by_group(
        {group: len(tokens) for group, tokens in tokens_by_group.items()},
        maximum_connections=maximum_connections,
        instruments_per_connection=instruments_per_connection,
    )

    assignments: list[CaptureShardAssignment] = []
    unplaced: dict[str, int] = {}
    stranded = 0

    for group in sorted(tokens_by_group, key=lambda name: (name != QUIET_GROUP, name)):
        tokens = tokens_by_group[group]
        capacity = allocation.get(group, 0) * instruments_per_connection
        placed = tokens[:capacity]
        for population, token in zip(
            populations_by_group[group][len(placed) :], tokens[len(placed) :], strict=True
        ):
            del token
            unplaced[population] = unplaced.get(population, 0) + 1
        for offset in range(0, len(placed), instruments_per_connection):
            chunk = placed[offset : offset + instruments_per_connection]
            assignments.append(
                CaptureShardAssignment(
                    shard_index=first_shard_index + len(assignments),
                    group=group,
                    tokens=tuple(chunk),
                )
            )
            stranded += instruments_per_connection - len(chunk)

    return CaptureShardPlan(
        assignments=tuple(assignments),
        unplaced_by_population=unplaced,
        connections_by_group=allocation,
        stranded_slots=stranded,
    )


def _connections_by_group(
    size_by_group: Mapping[str, int],
    *,
    maximum_connections: int,
    instruments_per_connection: int,
) -> dict[str, int]:
    """Connections per group: proportional by largest remainder, at least one for each non-empty.

    The floor is the whole point. A purely proportional split hands every connection to whichever
    group the admission controller happened to admit more of, and the other group then captures
    nothing at all — measured in production on 2026-08-19 in both directions on the same afternoon.
    """
    active = {group: size for group, size in size_by_group.items() if size > 0}
    if not active:
        return {}
    if len(active) > maximum_connections:
        # More groups than connections: the guarantee cannot hold for all of them, so the quiet
        # group and then the largest groups take one each and the rest are reported as unplaced.
        ordered = sorted(active, key=lambda group: (group != QUIET_GROUP, -active[group], group))
        return dict.fromkeys(ordered[:maximum_connections], 1)

    total = sum(active.values())
    exact = {group: size / total * maximum_connections for group, size in active.items()}
    allocation = {group: max(1, int(share)) for group, share in exact.items()}

    # Largest-remainder, then trim from the biggest allocation if the floors overshot.
    while sum(allocation.values()) < maximum_connections:
        group = max(
            active,
            key=lambda name: (
                exact[name] - allocation[name],
                allocation[name] * instruments_per_connection < active[name],
                name == QUIET_GROUP,
            ),
        )
        allocation[group] += 1
    while sum(allocation.values()) > maximum_connections:
        group = max(
            (name for name in active if allocation[name] > 1),
            key=lambda name: (allocation[name] - exact[name], name != QUIET_GROUP),
        )
        allocation[group] -= 1
    return allocation
