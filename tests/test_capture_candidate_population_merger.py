"""Tests for the capture-candidate population merger (`R.10` / `R.13`).

The number these defend against is real and was measured before the module was written: on the
2026-08-18 session the top index-option contract carries ₹891,906,493,663 of traded value and the
top index FUTURE ₹53,465,513,647, because an option's turnover is notional exposure and a cash
trade's is money changing hands. A single descending sort over both would evict cash coverage
entirely while looking like a correctly solved knapsack.
"""

from __future__ import annotations

import pytest

from nse_algo_trader.market_depth.capture_candidate_population_merger import (
    CaptureCandidateEntry,
    CaptureCandidatePopulation,
    CapturePopulationMergeError,
    merge_capture_candidate_populations,
)

# Deliberately the real orders of magnitude, not toy numbers.
CASH = CaptureCandidatePopulation(
    "cash",
    [
        CaptureCandidateEntry(101, 9_800_000_000.0),
        CaptureCandidateEntry(102, 4_100_000_000.0),
        CaptureCandidateEntry(103, 12_000.0),
    ],
)
OPTIONS = CaptureCandidatePopulation(
    "index_options",
    [
        CaptureCandidateEntry(201, 891_906_493_663.0),
        CaptureCandidateEntry(202, 797_213_513_772.0),
        CaptureCandidateEntry(203, 1_000.0),
    ],
)


def test_the_top_of_every_population_stands_equal() -> None:
    merged = merge_capture_candidate_populations([CASH, OPTIONS])
    top = {c.instrument_token: c.liquidity_value for c in merged.candidates[:2]}
    assert set(top) == {101, 201}
    assert top[101] == pytest.approx(top[201])


def test_a_notional_giant_does_not_outrank_every_member_of_the_other_population() -> None:
    """The defect this module exists to prevent, stated as an assertion."""
    merged = merge_capture_candidate_populations([CASH, OPTIONS])
    order = [c.instrument_token for c in merged.candidates]
    assert order.index(102) < order.index(203)


def test_standing_is_strictly_positive_for_the_weakest_member() -> None:
    """A zero would make the value density zero — a cut disguised as arithmetic."""
    merged = merge_capture_candidate_populations([CASH, OPTIONS])
    assert min(c.liquidity_value for c in merged.candidates) > 0


def test_standing_descends_with_liquidity_inside_a_population() -> None:
    merged = merge_capture_candidate_populations([CASH])
    by_token = {c.instrument_token: c.liquidity_value for c in merged.candidates}
    assert by_token[101] > by_token[102] > by_token[103]


def test_the_measured_packet_rate_is_carried_through_untouched() -> None:
    population = CaptureCandidatePopulation(
        "cash", [CaptureCandidateEntry(101, 1.0, measured_packets_per_second=3.5)]
    )
    merged = merge_capture_candidate_populations([population])
    assert merged.candidates[0].measured_packets_per_second == pytest.approx(3.5)


def test_a_token_claimed_by_two_populations_is_refused_by_name() -> None:
    first = CaptureCandidatePopulation("cash", [CaptureCandidateEntry(101, 1.0)])
    second = CaptureCandidatePopulation("index_options", [CaptureCandidateEntry(101, 2.0)])
    with pytest.raises(CapturePopulationMergeError, match="101"):
        merge_capture_candidate_populations([first, second])


def test_an_empty_population_is_reported_rather_than_dropped() -> None:
    """`commodity_mcx` has no instruments at all (B30) and must still be visible as zero."""
    merged = merge_capture_candidate_populations(
        [CASH, CaptureCandidatePopulation("commodity_mcx", [])]
    )
    assert merged.size_by_population["commodity_mcx"] == 0
    assert "commodity_mcx" in merged.describe()


def test_an_unnamed_population_is_refused() -> None:
    with pytest.raises(CapturePopulationMergeError, match="named"):
        CaptureCandidatePopulation("  ", [])


def test_the_merge_is_deterministic() -> None:
    first = merge_capture_candidate_populations([CASH, OPTIONS])
    second = merge_capture_candidate_populations([CASH, OPTIONS])
    assert [c.instrument_token for c in first.candidates] == [
        c.instrument_token for c in second.candidates
    ]


def test_ties_inside_a_population_break_on_the_token() -> None:
    tied = CaptureCandidatePopulation(
        "cash",
        [CaptureCandidateEntry(300, 5.0), CaptureCandidateEntry(200, 5.0)],
    )
    merged = merge_capture_candidate_populations([tied])
    assert [c.instrument_token for c in merged.candidates] == [200, 300]


def test_every_token_keeps_its_population_for_later_audit() -> None:
    merged = merge_capture_candidate_populations([CASH, OPTIONS])
    assert merged.population_by_token[101] == "cash"
    assert merged.population_by_token[201] == "index_options"
    assert set(merged.tokens_of("cash")) == {101, 102, 103}


def test_no_candidate_is_lost_in_the_merge() -> None:
    merged = merge_capture_candidate_populations([CASH, OPTIONS])
    assert len(merged.candidates) == len(CASH.entries) + len(OPTIONS.entries)
