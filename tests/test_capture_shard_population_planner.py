"""Tests for contention-group shard planning.

Written from a measured production regression, not from a hypothesis. Widening the capture to F&O on
2026-08-19 dropped cash from 1,153,998 ticks over 2,295 instruments in 42 minutes to **1.3 ticks per
instrument in 11 minutes**, uniformly across all three mixed connections, while derivatives on those
same connections delivered 114 (options) and 328 (futures) each.

The first fix — one connection per SEGMENT — failed in the opposite direction the same afternoon:
five segments against three connections gave cash all three and the derivative bots none
(`shard 01 cash 3,000 · shard 02 cash 3,000 · shard 03 cash 259`). Both failures are asserted below,
because a plan that can only be wrong in one direction is not a plan.
"""

from __future__ import annotations

import pytest

from nse_algo_trader.market_depth.capture_shard_population_planner import (
    LOUD_GROUP,
    QUIET_GROUP,
    CaptureShardPlanError,
    plan_capture_shards,
)


def _populations(**counts: int) -> tuple[list[int], dict[int, str]]:
    tokens: list[int] = []
    population_by_token: dict[int, str] = {}
    next_token = 1
    for population, count in counts.items():
        for _ in range(count):
            tokens.append(next_token)
            population_by_token[next_token] = population
            next_token += 1
    return tokens, population_by_token


def _plan(tokens, populations, connections: int = 3, per_connection: int = 3_000):
    return plan_capture_shards(
        tokens,
        populations,
        instruments_per_connection=per_connection,
        maximum_connections=connections,
    )


# -- the invariant ----------------------------------------------------------------------------


def test_no_connection_ever_carries_both_a_quiet_and_a_loud_feed() -> None:
    tokens, populations = _populations(cash=2_444, stock_options=5_546, index_options=726)
    plan = _plan(tokens, populations)
    for assignment in plan.assignments:
        groups = {
            QUIET_GROUP if populations[token] == "cash" else LOUD_GROUP
            for token in assignment.tokens
        }
        assert len(groups) == 1


# -- the two failures this module was written from --------------------------------------------


def test_the_original_regression_cash_is_not_crowded_off_every_connection() -> None:
    """The admitted shape of the 2026-08-19 10:00 run."""
    tokens, populations = _populations(
        cash=2_444, stock_options=5_546, index_options=726, stock_futures=276, index_futures=8
    )
    plan = _plan(tokens, populations)
    assert len(plan.tokens_of_group(QUIET_GROUP)) == 2_444
    assert plan.connections_by_group[QUIET_GROUP] >= 1


def test_the_second_regression_derivatives_never_get_zero_connections() -> None:
    """The admitted shape of the 2026-08-19 10:23 run, where cash took all three connections."""
    tokens, populations = _populations(
        cash=6_259, stock_options=2_535, index_options=198, stock_futures=8
    )
    plan = _plan(tokens, populations)
    assert plan.connections_by_group[LOUD_GROUP] >= 1
    assert len(plan.tokens_of_group(LOUD_GROUP)) > 0
    assert len(plan.tokens_of_group(QUIET_GROUP)) > 0


def test_a_tiny_group_still_gets_a_whole_connection() -> None:
    tokens, populations = _populations(cash=8_900, stock_options=100)
    plan = _plan(tokens, populations)
    assert plan.connections_by_group[LOUD_GROUP] == 1
    assert len(plan.tokens_of_group(LOUD_GROUP)) == 100


def test_connections_are_shared_out_in_proportion_to_admitted_size() -> None:
    tokens, populations = _populations(cash=2_444, stock_options=6_556)
    plan = _plan(tokens, populations)
    assert plan.connections_by_group == {QUIET_GROUP: 1, LOUD_GROUP: 2}


# -- accounting -------------------------------------------------------------------------------


def test_every_admitted_token_is_either_placed_or_reported_unplaced() -> None:
    tokens, populations = _populations(
        cash=2_444, stock_options=5_546, index_options=726, stock_futures=276, index_futures=8
    )
    plan = _plan(tokens, populations)
    assert plan.planned_tokens + plan.unplaced_tokens == len(tokens)


def test_unplaced_tokens_are_attributed_to_their_own_segment_not_their_group() -> None:
    """`R.11`: "2,741 derivatives dropped" cannot tell an operator which bot lost its tape."""
    tokens, populations = _populations(cash=2_444, stock_options=6_000, index_options=1_000)
    plan = _plan(tokens, populations)
    assert set(plan.unplaced_by_population) <= {"stock_options", "index_options"}
    assert plan.unplaced_tokens > 0


def test_stranded_slots_are_counted_rather_than_hidden() -> None:
    tokens, populations = _populations(cash=2_444, stock_options=6_556)
    plan = _plan(tokens, populations)
    assert plan.stranded_slots == 556
    assert "stranded" in plan.describe()


def test_the_controller_ranking_survives_inside_a_group() -> None:
    tokens, populations = _populations(cash=5)
    plan = plan_capture_shards(
        tokens, populations, instruments_per_connection=2, maximum_connections=3
    )
    assert [token for assignment in plan.assignments for token in assignment.tokens] == tokens


def test_the_tail_of_an_oversized_group_is_what_is_dropped_not_its_head() -> None:
    tokens, populations = _populations(cash=10)
    plan = plan_capture_shards(
        tokens, populations, instruments_per_connection=2, maximum_connections=3
    )
    placed = [token for assignment in plan.assignments for token in assignment.tokens]
    assert placed == tokens[: len(placed)]


def test_no_token_is_placed_twice() -> None:
    tokens, populations = _populations(cash=2_444, stock_options=5_546)
    plan = _plan(tokens, populations)
    placed = [token for assignment in plan.assignments for token in assignment.tokens]
    assert len(placed) == len(set(placed))


def test_shard_indices_are_contiguous_from_the_given_start() -> None:
    tokens, populations = _populations(cash=3_000, stock_options=3_000)
    plan = plan_capture_shards(
        tokens,
        populations,
        instruments_per_connection=3_000,
        maximum_connections=3,
        first_shard_index=4,
    )
    assert [assignment.shard_index for assignment in plan.assignments] == [4, 5, 6][
        : len(plan.assignments)
    ]
    assert plan.assignments[0].shard_index == 4


# -- refusals ---------------------------------------------------------------------------------


def test_an_unknown_population_defaults_to_the_loud_group_never_the_quiet_one() -> None:
    """A new derivative segment must not starve cash by being forgotten in a mapping."""
    tokens, populations = _populations(cash=1_000, currency_options=8_000)
    plan = _plan(tokens, populations)
    assert len(plan.tokens_of_group(QUIET_GROUP)) == 1_000


def test_a_token_with_no_population_is_refused_by_name() -> None:
    with pytest.raises(CaptureShardPlanError, match="99"):
        plan_capture_shards([99], {}, instruments_per_connection=10, maximum_connections=1)


def test_a_non_positive_connection_budget_is_refused() -> None:
    with pytest.raises(CaptureShardPlanError, match="maximum_connections"):
        plan_capture_shards([], {}, instruments_per_connection=10, maximum_connections=0)
    with pytest.raises(CaptureShardPlanError, match="instruments_per_connection"):
        plan_capture_shards([], {}, instruments_per_connection=0, maximum_connections=1)


def test_one_connection_and_two_groups_gives_it_to_the_quiet_one_and_says_so() -> None:
    tokens, populations = _populations(cash=100, stock_options=100)
    plan = plan_capture_shards(
        tokens, populations, instruments_per_connection=3_000, maximum_connections=1
    )
    assert plan.connections_by_group == {QUIET_GROUP: 1}
    assert plan.unplaced_by_population["stock_options"] == 100
