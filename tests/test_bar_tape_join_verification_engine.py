"""`M14` — the bar store and the depth tape are made to agree, or made to say they do not.

The tests that carry the design:

- `test_a_token_collision_is_refuted_even_though_every_price_looks_plausible` — the adversarial
  case the whole engine exists for. Two instruments' prices are individually reasonable; only
  the book bracket catches that they are not the same instrument.
- `test_a_tape_covering_one_bar_in_a_hundred_is_unverifiable_never_verified` — the failure that
  a naive agreement fraction reports as a perfect score.
- `test_the_verdict_threshold_is_the_sessions_own_disagreement_rate` — the reason there is no
  95% constant anywhere.
- `test_sampling_can_only_make_the_tape_undercount` — the asymmetry that gives the volume test
  a side sampling cannot explain away.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as strategy

from nse_algo_trader.market_depth.bar_tape_join_verification_engine import (
    FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION,
    LARGEST_TRIALS_SEARCHED_FOR_POWER,
    BarComparison,
    BarComparisonClass,
    BarTapeJoinVerificationEngine,
    BetaBinomialDisagreementNull,
    DisagreementNullAccumulator,
    DisagreementNullModel,
    DisagreementShape,
    InstrumentJoinReport,
    InstrumentJoinVerdict,
    JoinVerificationError,
    SessionJoinVerificationReport,
    StoredBar,
    VolumeReconciliationClass,
    _log_beta,
    binomial_upper_tail,
    classify_disagreement_shape,
    preload_session_snapshots,
    smallest_trials_that_can_reject,
    stored_bars_for_session,
)
from nse_algo_trader.market_depth.depth_tape_schema import (
    DepthLevel,
    DepthPacket,
    IntegrityFlag,
)
from nse_algo_trader.market_depth.market_depth_tape_store import (
    MarketDepthTapeReader,
    MarketDepthTapeStore,
)
from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
    BookSnapshot,
    OrderBookReplayError,
    OrderBookSnapshotReplayEngine,
)

SESSION = date(2026, 8, 12)
SESSION_OPEN = datetime(2026, 8, 12, 3, 45, tzinfo=UTC)  # 09:15 IST
FIVE_MINUTES = timedelta(minutes=5)
SIGNIFICANCE = 0.01
"""A test's policy input, chosen here so the tests read against one number rather than
each choosing its own. The engine itself has no default (`R.03`)."""


# --------------------------------------------------------------------- doubles


class TapeStub:
    """A replay engine stand-in holding hand-built snapshots per instrument.

    A stub rather than a written parquet tape: these tests are about the COMPARISON, and a real
    tape would make each of them a test of pyarrow. The real tape is exercised by
    `scripts/verify_bar_tape_join_on_real_data.py` under `R.05`.
    """

    def __init__(
        self,
        snapshots_by_instrument: dict[int, list[BookSnapshot]],
        *,
        staleness_threshold_millis: float = float("inf"),
    ) -> None:
        self.snapshots = snapshots_by_instrument
        self._threshold = staleness_threshold_millis

    def session_snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        if instrument_token not in self.snapshots:
            raise OrderBookReplayError(f"instrument {instrument_token} is not in this tape")
        return list(self.snapshots[instrument_token])

    def staleness_threshold_millis_for(self, snapshots: Sequence[BookSnapshot]) -> float:
        return self._threshold

    def median_spread_paise_for(self, instrument_token: int) -> float | None:
        """`None`: this stub hands back every snapshot, so the engine measures the spread from
        them. Only a streamed source, which returns a reduced set, answers this itself."""
        return None


def snapshot(
    *,
    instrument_token: int = 1,
    at: datetime,
    last_price_paise: int,
    bid_paise: int | None = None,
    ask_paise: int | None = None,
    volume_traded: int = 0,
) -> BookSnapshot:
    """One book. Defaults bracket the last price by one rupee on each side."""
    bid = last_price_paise - 100 if bid_paise is None else bid_paise
    ask = last_price_paise + 100 if ask_paise is None else ask_paise
    return BookSnapshot(
        instrument_token=instrument_token,
        receipt_time=at,
        receipt_sequence=int(at.timestamp()),
        capture_run="test",
        exchange_time=at,
        last_price_paise=last_price_paise,
        last_traded_quantity=1,
        volume_traded=volume_traded,
        total_buy_quantity=1_000,
        total_sell_quantity=1_000,
        integrity_flags=IntegrityFlag.NONE,
        bids=() if bid_paise is False else (DepthLevel(price_paise=bid, quantity=100, orders=1),),
        asks=() if ask_paise is False else (DepthLevel(price_paise=ask, quantity=100, orders=1),),
    )


def one_sided_snapshot(*, at: datetime, last_price_paise: int) -> BookSnapshot:
    return BookSnapshot(
        instrument_token=1,
        receipt_time=at,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=at,
        last_price_paise=last_price_paise,
        last_traded_quantity=1,
        volume_traded=0,
        total_buy_quantity=1_000,
        total_sell_quantity=0,
        integrity_flags=IntegrityFlag.NONE,
        bids=(DepthLevel(price_paise=last_price_paise - 100, quantity=100, orders=1),),
        asks=(),
    )


def bar(
    *,
    index: int,
    close_paise: int,
    volume: int = 1_000,
    instrument_token: int = 1,
) -> StoredBar:
    return StoredBar(
        instrument_token=instrument_token,
        bar_timestamp=SESSION_OPEN + index * FIVE_MINUTES,
        close_price=close_paise / 100,
        volume=volume,
        interval=FIVE_MINUTES,
    )


MINIMUM_DETECTABLE_DISAGREEMENT_RATE = 1.0
"""The tests' own policy input. 1.0 — "verified means this is not a token collision or a rescaled
series" — is chosen so the POWER gate is a no-op in the fixtures written before `A.124`, and the
tests that exercise the gate state their own rate. The engine has no default (`R.03`)."""


def engine(
    tape: TapeStub,
    *,
    significance: float = SIGNIFICANCE,
    minimum_detectable_disagreement_rate: float = MINIMUM_DETECTABLE_DISAGREEMENT_RATE,
) -> BarTapeJoinVerificationEngine:
    return BarTapeJoinVerificationEngine(
        tape,
        session_date=SESSION,
        significance=significance,
        minimum_detectable_disagreement_rate=minimum_detectable_disagreement_rate,
    )


def agreeing_session(
    *, bar_count: int, instrument_token: int = 1, base_paise: int = 140_000
) -> tuple[TapeStub, list[StoredBar]]:
    """A tape and bars that describe the same market at every bar, with a 2-rupee spread."""
    snapshots: list[BookSnapshot] = []
    bars: list[StoredBar] = []
    for index in range(bar_count):
        price = base_paise + index * 10
        snapshots.append(
            snapshot(
                instrument_token=instrument_token,
                at=SESSION_OPEN + (index + 1) * FIVE_MINUTES,
                last_price_paise=price,
                volume_traded=1_000 * (index + 1),
            )
        )
        bars.append(bar(index=index, close_paise=price, instrument_token=instrument_token))
    return TapeStub({instrument_token: snapshots}), bars


# --------------------------------------------------------------------- classification


def test_a_bar_matching_the_tape_agrees() -> None:
    tape, bars = agreeing_session(bar_count=3)
    comparisons = engine(tape).compare_instrument(1, bars)
    assert [comparison.comparison_class for comparison in comparisons] == [
        BarComparisonClass.AGREES_WITHIN_DERIVED_TOLERANCE
    ] * 3


def test_a_close_inside_the_spread_but_off_the_last_price_still_agrees() -> None:
    """The tolerance is the instrument's own spread, so a sub-spread deviation is one market
    seen twice — not two markets."""
    tape = TapeStub(
        {
            1: [
                snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000),
            ]
        }
    )
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=140_050)])
    assert comparison.comparison_class is BarComparisonClass.AGREES_WITHIN_DERIVED_TOLERANCE
    assert comparison.tolerance_paise == 200  # the book's own 2-rupee spread, measured
    assert comparison.deviation_paise == 50


def test_a_close_outside_the_book_is_its_own_class_not_a_large_deviation() -> None:
    """Stronger evidence than any deviation: a traded price must lie in the book that made it."""
    tape = TapeStub({1: [snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000)]})
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=180_000)])
    assert comparison.comparison_class is BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK
    assert comparison.comparison_class.is_disagreement


def test_a_bar_with_no_packet_is_absent_not_agreeing() -> None:
    tape = TapeStub({1: [snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000)]})
    comparisons = engine(tape).compare_instrument(
        1, [bar(index=0, close_paise=140_000), bar(index=5, close_paise=140_000)]
    )
    # Bar 5's window has no packet inside it, and the only earlier packet predates the bar.
    assert comparisons[1].comparison_class is BarComparisonClass.TAPE_ABSENT_IN_BAR_INTERVAL
    assert not comparisons[1].comparison_class.is_verifiable


def test_an_instrument_the_tape_never_recorded_is_absent_rather_than_an_error() -> None:
    """`M24`'s coverage limit stays visible: a raised exception would hide it."""
    comparisons = engine(TapeStub({})).compare_instrument(999, [bar(index=0, close_paise=1)])
    assert comparisons[0].comparison_class is BarComparisonClass.TAPE_ABSENT_IN_BAR_INTERVAL


def test_a_book_older_than_the_derived_staleness_threshold_is_stale_not_compared() -> None:
    tape = TapeStub(
        {1: [snapshot(at=SESSION_OPEN + timedelta(seconds=1), last_price_paise=140_000)]},
        staleness_threshold_millis=500,
    )
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=140_000)])
    assert comparison.comparison_class is BarComparisonClass.TAPE_STALE_AT_BAR_CLOSE
    assert not comparison.comparison_class.is_verifiable
    # The evidence survives the classification — `M24` needs to know HOW stale.
    assert comparison.alignment_age_millis == pytest.approx(299_000, rel=1e-6)
    assert comparison.tape_last_price_paise == 140_000


def test_a_one_sided_book_cannot_bracket_and_says_so() -> None:
    tape = TapeStub({1: [one_sided_snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=1)]})
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=1)])
    assert comparison.comparison_class is BarComparisonClass.BOOK_ONE_SIDED_AT_BAR_CLOSE
    assert not comparison.comparison_class.is_verifiable


def test_the_bar_is_aligned_to_its_close_not_its_stamp() -> None:
    """A bar stamped 09:15 closes at 09:20; aligning to the stamp compares a close against the
    book at the open, which is a different price on any moving instrument."""
    at_open = SESSION_OPEN
    at_close = SESSION_OPEN + FIVE_MINUTES
    tape = TapeStub(
        {
            1: [
                snapshot(at=at_open, last_price_paise=140_000),
                snapshot(at=at_close, last_price_paise=145_000),
            ]
        }
    )
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=145_000)])
    assert comparison.tape_last_price_paise == 145_000
    assert comparison.comparison_class is BarComparisonClass.AGREES_WITHIN_DERIVED_TOLERANCE


# --------------------------------------------------------------------- volume


def test_sampling_can_only_make_the_tape_undercount() -> None:
    tape = TapeStub(
        {
            1: [
                snapshot(at=SESSION_OPEN, last_price_paise=140_000, volume_traded=10_000),
                snapshot(
                    at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000, volume_traded=10_700
                ),
            ]
        }
    )
    (comparison,) = engine(tape).compare_instrument(
        1, [bar(index=0, close_paise=140_000, volume=1_000)]
    )
    assert comparison.volume_class is VolumeReconciliationClass.CONSISTENT_WITH_SAMPLING
    assert comparison.tape_volume_delta == 700


def test_a_tape_recording_more_volume_than_the_bar_is_impossible_and_flagged() -> None:
    tape = TapeStub(
        {
            1: [
                snapshot(at=SESSION_OPEN, last_price_paise=140_000, volume_traded=10_000),
                snapshot(
                    at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000, volume_traded=99_000
                ),
            ]
        }
    )
    (comparison,) = engine(tape).compare_instrument(
        1, [bar(index=0, close_paise=140_000, volume=1_000)]
    )
    assert comparison.volume_class is VolumeReconciliationClass.TAPE_EXCEEDS_BAR


def test_cumulative_volume_running_backwards_is_unverifiable_not_a_disagreement() -> None:
    """A capture-run boundary restarts the counter; that says nothing about the bar."""
    tape = TapeStub(
        {
            1: [
                snapshot(at=SESSION_OPEN, last_price_paise=140_000, volume_traded=90_000),
                snapshot(
                    at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000, volume_traded=10
                ),
            ]
        }
    )
    (comparison,) = engine(tape).compare_instrument(1, [bar(index=0, close_paise=140_000)])
    assert comparison.volume_class is VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS


# --------------------------------------------------------------------- the verdict


def test_a_clean_session_verifies_every_instrument() -> None:
    tape_one, bars_one = agreeing_session(bar_count=40, instrument_token=1)
    tape_two, bars_two = agreeing_session(bar_count=40, instrument_token=2, base_paise=50_000)
    merged = TapeStub({**tape_one.snapshots, **tape_two.snapshots})
    report = engine(merged).verify_session({1: bars_one, 2: bars_two})
    assert report.pooled_disagreement_rate == 0.0
    assert report.verdict_counts()[InstrumentJoinVerdict.JOIN_VERIFIED] == 2
    assert report.refuted_instruments() == frozenset()


def test_a_token_collision_is_refuted_even_though_every_price_looks_plausible() -> None:
    """The adversarial case. Instrument 2's tape holds instrument 1's prices: right shape, right
    magnitude, wrong instrument. Only the book bracket catches it."""
    clean_tape, clean_bars = agreeing_session(bar_count=40, instrument_token=1)
    # The collided instrument's BARS are its own; its TAPE is instrument 1's, retokened.
    collided_snapshots = [
        snapshot(
            instrument_token=2,
            at=shot.receipt_time,
            last_price_paise=shot.last_price_paise,
            volume_traded=shot.volume_traded,
        )
        for shot in clean_tape.snapshots[1]
    ]
    collided_bars = [
        bar(index=index, close_paise=50_000 + index * 10, instrument_token=2) for index in range(40)
    ]
    merged = TapeStub({1: clean_tape.snapshots[1], 2: collided_snapshots})
    report = engine(merged).verify_session({1: clean_bars, 2: collided_bars})

    assert report.refuted_instruments() == frozenset({2})
    collided = next(item for item in report.instruments if item.instrument_token == 2)
    assert collided.verdict is InstrumentJoinVerdict.JOIN_REFUTED
    assert collided.class_counts[BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK] == 40, (
        "every bar's close must fall outside the colliding instrument's book"
    )


def test_a_tape_covering_one_bar_in_a_hundred_is_unverifiable_never_verified() -> None:
    """The failure a naive agreement fraction reports as 100%."""
    tape = TapeStub({1: [snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000)]})
    bars = [bar(index=index, close_paise=140_000) for index in range(100)]
    report = engine(tape).verify_session({1: bars})
    (only,) = report.instruments
    assert only.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE
    assert only.comparisons_verifiable == 1
    assert only.disagreement_upper_tail is None, (
        "no test was run, so no tail may be reported as passing"
    )
    assert report.bars_total == 100


def test_an_empty_tape_reports_no_agreement_rather_than_perfect_agreement() -> None:
    report = engine(TapeStub({})).verify_session(
        {1: [bar(index=index, close_paise=140_000) for index in range(50)]}
    )
    (only,) = report.instruments
    assert only.agreement_fraction is None
    assert only.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE
    assert report.comparisons_verifiable == 0


def test_the_verdict_threshold_is_the_sessions_own_disagreement_rate() -> None:
    """The same instrument, unchanged, flips verdict when the session around it changes — which
    is what "no hardcoded threshold" means operationally.

    Instrument 1 disagrees on 4 of 40 bars in both runs. Against a clean session it is refused;
    against a session where everyone disagrees at a similar rate it is not.
    """

    def session_with(
        disagreements_per_instrument: dict[int, int],
    ) -> tuple[TapeStub, dict[int, Sequence[StoredBar]]]:
        snapshots: dict[int, list[BookSnapshot]] = {}
        bars: dict[int, list[StoredBar]] = {}
        for token, bad_count in disagreements_per_instrument.items():
            shots: list[BookSnapshot] = []
            token_bars: list[StoredBar] = []
            for index in range(40):
                price = 140_000 + index * 10
                shots.append(
                    snapshot(
                        instrument_token=token,
                        at=SESSION_OPEN + (index + 1) * FIVE_MINUTES,
                        last_price_paise=price,
                    )
                )
                # A disagreeing bar sits well outside the book, so it is unambiguous evidence.
                close = price + 50_000 if index < bad_count else price
                token_bars.append(bar(index=index, close_paise=close, instrument_token=token))
            snapshots[token] = shots
            bars[token] = token_bars
        return TapeStub(snapshots), dict(bars)

    clean_tape, clean_bars = session_with({1: 4, 2: 0, 3: 0, 4: 0})
    verdict_clean = engine(clean_tape).verify_session(clean_bars)
    assert verdict_clean.refuted_instruments() == frozenset({1})

    noisy_tape, noisy_bars = session_with({1: 4, 2: 4, 3: 4, 4: 4})
    verdict_noisy = engine(noisy_tape).verify_session(noisy_bars)
    assert verdict_noisy.refuted_instruments() == frozenset(), (
        "an instrument no worse than its session's own base rate is not evidence of a broken join"
    )


def test_a_single_disagreement_refutes_when_nothing_else_in_the_session_disagreed() -> None:
    """The leave-one-out null is what makes this work.

    Instrument 2 disagrees once in forty. Pooled over the whole session that is 1 in 80 and
    entirely unsurprising — the instrument would mask itself. Against a null built from
    instrument 1 alone the rate is zero, any disagreement is infinitely surprising, and it is
    refused. The engine answers the `p̂ = 0` case directly rather than through an epsilon.
    """
    clean_tape, clean_bars = agreeing_session(bar_count=40, instrument_token=1)
    second_snapshots = [
        snapshot(
            instrument_token=2,
            at=SESSION_OPEN + (index + 1) * FIVE_MINUTES,
            last_price_paise=140_000,
        )
        for index in range(40)
    ]
    second_bars = [
        bar(index=index, close_paise=140_000 if index else 999_999, instrument_token=2)
        for index in range(40)
    ]
    merged = TapeStub({1: clean_tape.snapshots[1], 2: second_snapshots})
    report = engine(merged).verify_session({1: clean_bars, 2: second_bars})
    assert report.refuted_instruments() == frozenset({2})
    refuted = next(item for item in report.instruments if item.instrument_token == 2)
    assert refuted.null_disagreement_rate == 0.0
    assert refuted.minimum_comparisons_to_reject == 1
    assert report.pooled_disagreement_rate == 1 / 80, (
        "the session-level rate stays descriptive and still counts the disagreement"
    )


def test_a_session_where_nothing_agreed_refuses_to_call_anything_verified() -> None:
    """`p̂ = 1` leaves the test powerless, and powerless must read as unverifiable."""
    snapshots = [
        snapshot(at=SESSION_OPEN + (index + 1) * FIVE_MINUTES, last_price_paise=140_000)
        for index in range(40)
    ]
    bars = [bar(index=index, close_paise=999_999) for index in range(40)]
    report = engine(TapeStub({1: snapshots})).verify_session({1: bars})
    assert report.pooled_disagreement_rate == 1.0
    assert report.minimum_comparisons_to_reject is None
    assert report.verdict_counts()[InstrumentJoinVerdict.JOIN_UNVERIFIABLE] == 1


def test_neither_policy_input_has_a_default_and_both_reject_a_degenerate_one() -> None:
    """`significance` is how surprised to be before REFUSING; the minimum detectable disagreement
    rate is what VERIFIED claims (`A.124`). Both are risk appetites rather than measurements, so
    neither may be defaulted (`R.03`) — an engine that guesses either one is deciding policy."""
    with pytest.raises(TypeError):
        BarTapeJoinVerificationEngine(TapeStub({}), session_date=SESSION)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        BarTapeJoinVerificationEngine(  # type: ignore[call-arg]
            TapeStub({}), session_date=SESSION, significance=SIGNIFICANCE
        )
    for degenerate in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(JoinVerificationError):
            BarTapeJoinVerificationEngine(
                TapeStub({}),
                session_date=SESSION,
                significance=degenerate,
                minimum_detectable_disagreement_rate=MINIMUM_DETECTABLE_DISAGREEMENT_RATE,
            )
    # 1.0 IS admissible here — "verified only claims this is not total disagreement" — so the
    # degenerate set differs from significance's by exactly that value.
    for degenerate in (0.0, -0.5, 2.0):
        with pytest.raises(JoinVerificationError):
            BarTapeJoinVerificationEngine(
                TapeStub({}),
                session_date=SESSION,
                significance=SIGNIFICANCE,
                minimum_detectable_disagreement_rate=degenerate,
            )


def test_the_report_describes_its_own_coverage_rather_than_only_its_verdicts() -> None:
    tape = TapeStub({1: [snapshot(at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000)]})
    report = engine(tape).verify_session(
        {1: [bar(index=index, close_paise=140_000) for index in range(10)]}
    )
    described = report.describe()
    assert "1/10 bars verifiable" in described
    assert "10.0%" in described


# --------------------------------------------------------------------- estimators


def test_disagreement_upper_tail_matches_a_hand_computed_case() -> None:
    # P(X >= 2 | n=3, p=0.5) = (3 + 1) / 8
    assert binomial_upper_tail(2, 3, 0.5) == pytest.approx(0.5)
    assert binomial_upper_tail(0, 3, 0.5) == 1.0
    assert binomial_upper_tail(4, 3, 0.5) == 0.0


@given(
    trials=strategy.integers(min_value=1, max_value=60),
    probability=strategy.floats(min_value=0.001, max_value=0.999),
)
@settings(max_examples=150, deadline=None)
def test_the_disagreement_upper_tail_is_a_monotone_probability(
    trials: int, probability: float
) -> None:
    """Non-increasing in `successes`, and always a probability. Both are definitional, and both
    are what a hand-rolled tail sum gets wrong at the ends."""
    tails = [binomial_upper_tail(count, trials, probability) for count in range(trials + 2)]
    for value in tails:
        assert 0.0 <= value <= 1.0
    for earlier, later in pairwise(tails):
        assert later <= earlier + 1e-12


@given(
    probability=strategy.floats(min_value=0.001, max_value=0.999),
    significance=strategy.floats(min_value=0.0001, max_value=0.5),
)
@settings(max_examples=150, deadline=None)
def test_the_minimum_trials_is_the_smallest_n_that_could_reject(
    probability: float, significance: float
) -> None:
    minimum = smallest_trials_that_can_reject(probability, significance)
    assert minimum is not None
    # Compared with a relative tolerance: the tail is computed in log space (`B5`), so it is exact
    # to ~1e-13 rather than to the last bit, and Hypothesis will find `probability == significance`
    # where an exact comparison turns on a single ULP.
    assert binomial_upper_tail(minimum, minimum, probability) < significance * (1 + 1e-9)
    if minimum > 1:
        assert binomial_upper_tail(minimum - 1, minimum - 1, probability) >= significance * (
            1 - 1e-9
        )


@given(shift_paise=strategy.integers(min_value=-50_000, max_value=500_000))
@settings(max_examples=60, deadline=None)
def test_agreement_is_invariant_to_a_shift_applied_to_both_stores(shift_paise: int) -> None:
    """If both stores move together, the join is still sound. A comparison that failed this
    would be measuring the price level rather than the agreement."""
    base = 600_000 + shift_paise
    snapshots = [
        snapshot(at=SESSION_OPEN + (index + 1) * FIVE_MINUTES, last_price_paise=base + index * 10)
        for index in range(20)
    ]
    bars = [bar(index=index, close_paise=base + index * 10) for index in range(20)]
    report = engine(TapeStub({1: snapshots})).verify_session({1: bars})
    assert report.disagreements_total == 0


# --------------------------------------------------------------------- input pipeline


def test_the_loader_reads_the_same_table_the_paper_loop_reads(tmp_path: Path) -> None:
    store = tmp_path / "market_data.sqlite3"
    connection = sqlite3.connect(store)
    connection.execute(
        "CREATE TABLE price_bars (instrument_token INTEGER, bar_interval TEXT, "
        "bar_timestamp TEXT, open_price REAL, high_price REAL, low_price REAL, "
        "close_price REAL, volume INTEGER, open_interest INTEGER, availability_time TEXT)"
    )
    connection.executemany(
        "INSERT INTO price_bars VALUES (?, '5m', ?, 1, 1, 1, ?, ?, NULL, ?)",
        [
            (1, "2026-08-12T09:15:00+05:30", 100.5, 10, "2026-08-12T09:20:00+05:30"),
            (1, "2026-08-12T09:20:00+05:30", 101.0, 20, "2026-08-12T09:25:00+05:30"),
            (2, "2026-08-12T09:15:00+05:30", 50.0, 30, "2026-08-12T09:20:00+05:30"),
            (1, "2026-08-13T09:15:00+05:30", 999.0, 40, "2026-08-13T09:20:00+05:30"),
        ],
    )
    connection.commit()
    connection.close()

    loaded = stored_bars_for_session(session_date=SESSION, market_data=store)
    assert set(loaded) == {1, 2}
    assert len(loaded[1]) == 2, "the next session's bars must not leak into this one"
    assert loaded[1][0].close_paise == 10_050
    assert loaded[1][0].interval == FIVE_MINUTES
    assert loaded[1][0].closing_instant == loaded[1][1].bar_timestamp

    restricted = stored_bars_for_session(
        session_date=SESSION, market_data=store, instrument_tokens=[2]
    )
    assert set(restricted) == {2}


def test_the_loader_refuses_an_interval_it_cannot_convert(tmp_path: Path) -> None:
    """A defaulted duration would align every bar against the wrong book silently."""
    with pytest.raises(JoinVerificationError):
        stored_bars_for_session(
            session_date=SESSION, market_data=tmp_path / "absent.sqlite3", bar_interval="fortnight"
        )


def test_the_loader_reports_an_unreadable_store_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(JoinVerificationError):
        stored_bars_for_session(session_date=SESSION, market_data=tmp_path / "absent.sqlite3")


# --------------------------------------------------------------------- streamed preload


def _write_tape(
    tape_root: Path,
    packets: list[tuple[int, datetime, int, int, int, int]],
) -> None:
    """A real parquet tape — the streamed preload's whole job is reading one, so it reads one."""
    store = MarketDepthTapeStore(
        tape_root=tape_root,
        session_date=SESSION,
        shard_index=1,
        capture_run_id="join-test",
        max_buffered_rows=10_000,
        max_seconds_between_flushes=3600.0,
    )
    for sequence, (token, at, last, bid, ask, volume) in enumerate(packets):
        store.append(
            DepthPacket(
                instrument_token=token,
                exchange="NSE",
                exchange_time=at,
                receipt_time=at,
                receipt_sequence=sequence,
                last_price_paise=last,
                last_traded_quantity=1,
                average_traded_price_paise=last,
                volume_traded=volume,
                total_buy_quantity=1_000,
                total_sell_quantity=1_000,
                open_interest=0,
                bids=tuple(
                    DepthLevel(price_paise=bid - level, quantity=100, orders=1)
                    for level in range(5)
                ),
                asks=tuple(
                    DepthLevel(price_paise=ask + level, quantity=100, orders=1)
                    for level in range(5)
                ),
            ),
            IntegrityFlag.NONE,
        )
    store.flush()
    store.close()


def test_the_streamed_preload_answers_the_same_questions_as_a_per_instrument_read(
    tmp_path: Path,
) -> None:
    """`M25`. One pass over the tape must classify every bar exactly as the per-instrument read
    does — otherwise the fix that makes the verification affordable also changes its answers."""
    packets = []
    bars: list[StoredBar] = []
    for index in range(12):
        price = 140_000 + index * 10
        at = SESSION_OPEN + (index + 1) * FIVE_MINUTES - timedelta(seconds=1)
        packets.append((1, at, price, price - 100, price + 100, 1_000 * (index + 1)))
        packets.append((2, at, 50_000, 49_900, 50_100, 500 * (index + 1)))
        bars.append(bar(index=index, close_paise=price, instrument_token=1))
    _write_tape(tmp_path, packets)

    bars_by_instrument: dict[int, Sequence[StoredBar]] = {1: bars}
    reader = MarketDepthTapeReader(tmp_path)
    streamed = preload_session_snapshots(
        reader,
        session_date=SESSION,
        instrument_tokens=[1, 2],
        bars_by_instrument=bars_by_instrument,
        staleness_quantile=0.99,
        window_start=SESSION_OPEN - timedelta(hours=4),
        window_end=SESSION_OPEN + timedelta(hours=8),
    )
    from_stream = BarTapeJoinVerificationEngine(
        streamed,
        session_date=SESSION,
        significance=SIGNIFICANCE,
        minimum_detectable_disagreement_rate=MINIMUM_DETECTABLE_DISAGREEMENT_RATE,
    ).compare_instrument(1, bars)

    engine_source = OrderBookSnapshotReplayEngine(
        reader, session_date=SESSION, staleness_quantile=0.99
    )
    from_reads = BarTapeJoinVerificationEngine(
        _PerInstrumentSource(engine_source),
        session_date=SESSION,
        significance=SIGNIFICANCE,
        minimum_detectable_disagreement_rate=MINIMUM_DETECTABLE_DISAGREEMENT_RATE,
    ).compare_instrument(1, bars)

    assert [row.comparison_class for row in from_stream] == [
        row.comparison_class for row in from_reads
    ]
    assert [row.tape_last_price_paise for row in from_stream] == [
        row.tape_last_price_paise for row in from_reads
    ]
    assert [row.tape_volume_delta for row in from_stream] == [
        row.tape_volume_delta for row in from_reads
    ]


class _PerInstrumentSource:
    """The old per-instrument path, kept only so the streamed one can be diffed against it."""

    def __init__(self, engine_source: OrderBookSnapshotReplayEngine) -> None:
        self._engine = engine_source

    def session_snapshots_for(self, instrument_token: int) -> list[BookSnapshot]:
        return self._engine.session_snapshots_for(instrument_token)

    def staleness_threshold_millis_for(self, snapshots: Sequence[BookSnapshot]) -> float:
        return self._engine.staleness_threshold_millis_for(snapshots)

    def median_spread_paise_for(self, instrument_token: int) -> float | None:
        return None


def test_the_streamed_preload_measures_the_spread_over_the_whole_tape(tmp_path: Path) -> None:
    """The reduced set it hands back is only the bar boundaries, so the tolerance must come from
    what streamed past — not from the survivors."""
    packets = []
    # One wide-spread book at a bar boundary, many tight ones between. A median over survivors
    # alone would report the wide one; over the stream it reports the tight one.
    for index in range(40):
        at = SESSION_OPEN + timedelta(seconds=30 * index)
        wide = index == 9  # lands at the 09:20 boundary
        spread = 10_000 if wide else 100
        packets.append((1, at, 140_000, 140_000 - spread // 2, 140_000 + spread // 2, index))
    _write_tape(tmp_path, packets)

    streamed = preload_session_snapshots(
        MarketDepthTapeReader(tmp_path),
        session_date=SESSION,
        instrument_tokens=[1],
        bars_by_instrument={1: [bar(index=0, close_paise=140_000)]},
        staleness_quantile=0.99,
        window_start=SESSION_OPEN - timedelta(hours=4),
        window_end=SESSION_OPEN + timedelta(hours=8),
    )
    measured = streamed.median_spread_paise_for(1)
    assert measured == 100, "the tolerance must describe the feed, not the two books kept"


def test_the_streamed_preload_refuses_a_degenerate_quantile(tmp_path: Path) -> None:
    for degenerate in (0.0, 1.0, -1.0):
        with pytest.raises(JoinVerificationError):
            preload_session_snapshots(
                MarketDepthTapeReader(tmp_path),
                session_date=SESSION,
                instrument_tokens=[1],
                bars_by_instrument={},
                staleness_quantile=degenerate,
                window_start=SESSION_OPEN,
                window_end=SESSION_OPEN + timedelta(hours=8),
            )


# --------------------------------------------------------------------- M26 shape


def test_a_constant_ratio_is_named_a_price_basis_divergence() -> None:
    """`M26`. `HINDPETRO`'s real signature: the bar series rescaled by a fixed factor, so every
    disagreeing bar carries the SAME ratio. A market cannot do that."""
    factor = 0.95099
    snapshots = [
        snapshot(
            at=SESSION_OPEN + (index + 1) * FIVE_MINUTES, last_price_paise=140_000 + index * 10
        )
        for index in range(30)
    ]
    bars = [
        bar(index=index, close_paise=round((140_000 + index * 10) * factor)) for index in range(30)
    ]
    # A clean second instrument, because the null this one is tested against is built from the
    # OTHERS — with nobody else in the session there is nothing to be an outlier from.
    clean_tape, clean_bars = agreeing_session(bar_count=30, instrument_token=2, base_paise=50_000)
    merged = TapeStub({1: snapshots, **clean_tape.snapshots})
    report = engine(merged).verify_session({1: bars, 2: clean_bars})
    (only,) = [item for item in report.instruments if item.instrument_token == 1]
    assert only.disagreement_shape is DisagreementShape.PRICE_BASIS_DIVERGENCE
    assert only.price_basis_ratio == pytest.approx(factor, abs=1e-4)
    assert report.price_basis_divergences() == (only,)


def test_scattered_ratios_are_not_called_a_basis_divergence() -> None:
    """The counterweight: a wrong classification here would send someone hunting a corporate
    action that does not exist."""
    snapshots = [
        snapshot(at=SESSION_OPEN + (index + 1) * FIVE_MINUTES, last_price_paise=140_000)
        for index in range(30)
    ]
    # Each disagreement a different distance out — scatter, not a factor.
    bars = [bar(index=index, close_paise=140_000 + 20_000 + index * 900) for index in range(30)]
    clean_tape, clean_bars = agreeing_session(bar_count=30, instrument_token=2, base_paise=50_000)
    merged = TapeStub({1: snapshots, **clean_tape.snapshots})
    report = engine(merged).verify_session({1: bars, 2: clean_bars})
    (only,) = [item for item in report.instruments if item.instrument_token == 1]
    assert only.disagreement_shape is DisagreementShape.SPORADIC_DISAGREEMENT
    assert only.price_basis_ratio is None
    assert report.price_basis_divergences() == ()


def test_the_constancy_bound_is_paise_rounding_and_nothing_chosen() -> None:
    """A truly constant factor still wobbles, because both sides are integer paise. The bound is
    that rounding computed from the prices seen — so it tightens on expensive instruments and
    loosens on cheap ones, which is what the arithmetic requires and what a constant cannot do."""
    factor = 0.97
    cheap = [
        BarComparison(
            instrument_token=1,
            bar_timestamp=SESSION_OPEN + index * FIVE_MINUTES,
            comparison_class=BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
            bar_close_paise=round((500 + index) * factor),
            tape_last_price_paise=500 + index,
            best_bid_paise=None,
            best_ask_paise=None,
            deviation_paise=1,
            tolerance_paise=1,
            alignment_age_millis=0.0,
            volume_class=VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS,
            bar_volume=0,
            tape_volume_delta=None,
        )
        for index in range(20)
    ]
    shape, ratio = classify_disagreement_shape(cheap)
    assert shape is DisagreementShape.PRICE_BASIS_DIVERGENCE
    assert ratio == pytest.approx(factor, abs=0.002)


def test_one_disagreeing_bar_cannot_establish_constancy() -> None:
    """One point is trivially constant; calling it a basis defect would be an artefact."""
    shape, ratio = classify_disagreement_shape(
        [
            BarComparison(
                instrument_token=1,
                bar_timestamp=SESSION_OPEN,
                comparison_class=BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
                bar_close_paise=100,
                tape_last_price_paise=200,
                best_bid_paise=None,
                best_ask_paise=None,
                deviation_paise=100,
                tolerance_paise=1,
                alignment_age_millis=0.0,
                volume_class=VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS,
                bar_volume=0,
                tape_volume_delta=None,
            )
        ]
    )
    assert shape is DisagreementShape.SPORADIC_DISAGREEMENT
    assert ratio is None


def test_an_unrefused_instrument_carries_no_shape() -> None:
    tape, bars = agreeing_session(bar_count=40, instrument_token=1)
    tape_two, bars_two = agreeing_session(bar_count=40, instrument_token=2, base_paise=50_000)
    merged = TapeStub({**tape.snapshots, **tape_two.snapshots})
    report = engine(merged).verify_session({1: bars, 2: bars_two})
    assert {item.disagreement_shape for item in report.instruments} == {
        DisagreementShape.NOT_APPLICABLE
    }


def test_a_ratio_of_one_is_not_a_basis_divergence() -> None:
    """The false positive the first real run produced: 89 instruments classified as rescaled whose
    ratio was exactly 1.0.

    Their prices AGREE to the paise; they were refused on the book bracket, because the aligned
    book was crossed or one-sided. That is a fact about the tape, not about the price series, and
    sending someone to hunt a corporate action for it wastes the finding.
    """
    agreeing_prices = [
        BarComparison(
            instrument_token=1,
            bar_timestamp=SESSION_OPEN + index * FIVE_MINUTES,
            comparison_class=BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
            bar_close_paise=140_000,
            tape_last_price_paise=140_000,
            best_bid_paise=141_000,
            best_ask_paise=142_000,
            deviation_paise=0,
            tolerance_paise=5,
            alignment_age_millis=0.0,
            volume_class=VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS,
            bar_volume=0,
            tape_volume_delta=None,
        )
        for index in range(8)
    ]
    shape, ratio = classify_disagreement_shape(agreeing_prices)
    assert shape is DisagreementShape.SPORADIC_DISAGREEMENT
    assert ratio is None


# --------------------------------------------------------------------- adversarial regressions
# One test per confirmed bug in `docs/research/240`. Each reproduces the ORIGINAL failure, so a
# regression fails here rather than on the next real session.


def test_b2_the_streamed_threshold_ignores_arrival_order(tmp_path: Path) -> None:
    """`B2`. Batches arrive in FILE order and a multi-run session enumerates them out of order —
    the real 2026-08-11 tape jumps backwards 5.34 hours. Differencing on arrival with `abs()` turned
    that into a phantom 19,200,000 ms gap and inflated the staleness threshold 3.9x."""
    early = SESSION_OPEN
    late = SESSION_OPEN + timedelta(hours=5)
    # Written late-run-first, so the stream sees a large BACKWARD step between the two runs.
    packets = [
        (1, late + timedelta(seconds=30 * i), 140_000, 139_900, 140_100, i) for i in range(6)
    ]
    packets += [
        (1, early + timedelta(seconds=30 * i), 140_000, 139_900, 140_100, i) for i in range(6)
    ]
    _write_tape(tmp_path, packets)

    streamed = preload_session_snapshots(
        MarketDepthTapeReader(tmp_path),
        session_date=SESSION,
        instrument_tokens=[1],
        bars_by_instrument={1: [bar(index=0, close_paise=140_000)]},
        staleness_quantile=0.99,
        window_start=SESSION_OPEN - timedelta(hours=4),
        window_end=SESSION_OPEN + timedelta(hours=12),
    )
    threshold = streamed.staleness_threshold_millis_for(streamed.session_snapshots_for(1))
    # Sorted, the gaps are eleven 30-second steps and ONE five-hour step between the runs. The p99
    # of that is the real jump, not a doubled phantom one.
    biggest_real_gap = (
        early + timedelta(hours=5) - (early + timedelta(seconds=150))
    ).total_seconds() * 1_000
    assert threshold <= biggest_real_gap + 1, (
        "the threshold must come from time-ordered gaps, not from the order rows happen to arrive"
    )


def test_b4_one_wide_book_does_not_destroy_a_true_rescaling() -> None:
    """`B4`. The "factor must move the price" check used to `return` inside the loop, so a single
    momentarily-wide book aborted the whole fit — and which bar aborted was order-dependent."""

    def comparison(close: int, tape: int, tolerance: int) -> BarComparison:
        return BarComparison(
            instrument_token=1,
            bar_timestamp=SESSION_OPEN,
            comparison_class=BarComparisonClass.CLOSE_OUTSIDE_RECORDED_BOOK,
            bar_close_paise=close,
            tape_last_price_paise=tape,
            best_bid_paise=None,
            best_ask_paise=None,
            deviation_paise=abs(close - tape),
            tolerance_paise=tolerance,
            alignment_age_millis=0.0,
            volume_class=VolumeReconciliationClass.UNVERIFIABLE_NO_ENDPOINTS,
            bar_volume=0,
            tape_volume_delta=None,
        )

    factor = 0.95
    rows = [comparison(round(140_000 * factor), 140_000, 5) for _ in range(20)]
    shape, fitted = classify_disagreement_shape(rows)
    assert shape is DisagreementShape.PRICE_BASIS_DIVERGENCE
    assert fitted == pytest.approx(factor, abs=1e-6)

    # One bar's book momentarily widens. The instrument is still rescaled.
    rows[7] = comparison(round(140_000 * factor), 140_000, 8_000)
    shape_after, fitted_after = classify_disagreement_shape(rows)
    assert shape_after is DisagreementShape.PRICE_BASIS_DIVERGENCE, (
        "one wide-spread bar must not overturn a fit that nineteen others support"
    )
    assert fitted_after == pytest.approx(factor, abs=1e-6)


def test_b5_the_small_tail_keeps_its_significant_digits() -> None:
    """`B5`. Branching on which sum was SHORTER computed the small tail as `1 - large`, which
    cancelled it away: a true 4.84e-25 came back as 1.78e-15, and a true 2.22e-20 as exactly 0.0."""
    from fractions import Fraction

    def exact(successes: int, trials: int, numerator: int, denominator: int) -> float:
        probability = Fraction(numerator, denominator)
        total = sum(
            Fraction(math.comb(trials, k)) * probability**k * (1 - probability) ** (trials - k)
            for k in range(successes, trials + 1)
        )
        return float(total)

    for successes, trials, numerator, denominator in ((30, 2000, 1, 1000), (25, 1200, 15, 10000)):
        truth = exact(successes, trials, numerator, denominator)
        computed = binomial_upper_tail(successes, trials, numerator / denominator)
        assert computed > 0.0, "a positive tail must never be cancelled to zero"
        assert computed == pytest.approx(truth, rel=1e-6), (
            f"P(X>={successes}|{trials}) lost its digits: {computed} vs {truth}"
        )


def test_b8_an_untraded_instrument_is_not_a_disagreement() -> None:
    """`B8`. `last_price_paise == 0` means the instrument had not traded, so there is no traded
    price to compare. It used to produce a full-price deviation and count as a disagreement."""
    untraded = BookSnapshot(
        instrument_token=1,
        receipt_time=SESSION_OPEN + FIVE_MINUTES,
        receipt_sequence=1,
        capture_run="test",
        exchange_time=SESSION_OPEN + FIVE_MINUTES,
        last_price_paise=0,
        last_traded_quantity=0,
        volume_traded=0,
        total_buy_quantity=100,
        total_sell_quantity=100,
        integrity_flags=IntegrityFlag.NONE,
        bids=(DepthLevel(price_paise=139_900, quantity=100, orders=1),),
        asks=(DepthLevel(price_paise=140_100, quantity=100, orders=1),),
    )
    (comparison,) = engine(TapeStub({1: [untraded]})).compare_instrument(
        1, [bar(index=0, close_paise=140_000)]
    )
    assert comparison.comparison_class is BarComparisonClass.TAPE_HAS_NO_TRADE_YET
    assert not comparison.comparison_class.is_verifiable
    assert not comparison.comparison_class.is_disagreement


def test_b8_a_crossed_book_is_not_a_disagreement() -> None:
    """`B8`. A crossed book has an empty bracket, so EVERY close falls outside it. That is a tape
    defect the integrity classifier already flags, not evidence about the join."""
    crossed = snapshot(
        at=SESSION_OPEN + FIVE_MINUTES,
        last_price_paise=140_000,
        bid_paise=140_500,
        ask_paise=139_500,
    )
    (comparison,) = engine(TapeStub({1: [crossed]})).compare_instrument(
        1, [bar(index=0, close_paise=140_000)]
    )
    assert comparison.comparison_class is BarComparisonClass.BOOK_CROSSED_AT_BAR_CLOSE
    assert not comparison.comparison_class.is_verifiable


# ------------------------------------------------- the beta-binomial null (`A.123`, r/241)
#
# Every fixture below carries AT LEAST THREE instruments. `B1`'s original miss
# (`docs/research/240`) was a single-instrument fixture where the leave-one-out null is `None`
# and the verdict fell through for an unrelated reason, so the test passed while asserting
# nothing about the mechanism it named.


def session_from_counts(
    counts: Sequence[tuple[int, int, int]],
    *,
    significance: float = SIGNIFICANCE,
    minimum_detectable_disagreement_rate: float = MINIMUM_DETECTABLE_DISAGREEMENT_RATE,
) -> SessionJoinVerificationReport:
    """A session built to order: `(instrument_token, comparisons, disagreements)`.

    Each instrument gets `comparisons` bars whose tape agrees exactly, except the first
    `disagreements` of them, whose close is placed a full rupee outside the book's bracket — a
    `CLOSE_OUTSIDE_BOOK` disagreement rather than a large-deviation one, so the count the null is
    built from is exactly the number asked for.
    """
    snapshots: dict[int, list[BookSnapshot]] = {}
    bars_by_instrument: dict[int, list[StoredBar]] = {}
    for token, comparisons, disagreements in counts:
        books: list[BookSnapshot] = []
        bars: list[StoredBar] = []
        for index in range(comparisons):
            price = 140_000 + index * 10
            books.append(
                snapshot(
                    instrument_token=token,
                    at=SESSION_OPEN + (index + 1) * FIVE_MINUTES,
                    last_price_paise=price,
                    volume_traded=1_000 * (index + 1),
                )
            )
            close = price + 100_000 if index < disagreements else price
            bars.append(bar(index=index, close_paise=close, instrument_token=token))
        snapshots[token] = books
        bars_by_instrument[token] = bars
    tape = TapeStub(snapshots)
    return engine(
        tape,
        significance=significance,
        minimum_detectable_disagreement_rate=minimum_detectable_disagreement_rate,
    ).verify_session(bars_by_instrument)


def test_a_null_with_no_dispersion_is_the_binomial_it_replaced() -> None:
    """The beta-binomial must CONTAIN the binomial, or the change is a different test rather
    than a corrected one. `rho = 0` is not a special case in the maths; it is the boundary."""
    for probability in (0.01, 0.05, 0.1, 0.25, 0.5, 0.9):
        for trials in (1, 2, 5, 11, 40):
            for successes in range(0, trials + 1):
                assert BetaBinomialDisagreementNull(probability, 0.0).upper_tail(
                    successes, trials
                ) == pytest.approx(binomial_upper_tail(successes, trials, probability), abs=1e-12)


def test_the_tail_matches_an_independent_implementation() -> None:
    """Checked against `scipy.stats.betabinom`, a different author's code.

    Checking a hand-written tail against itself is not a check. scipy's `logpmf` was measured
    accurate to 3.7e-13 against a 60-digit reference (`docs/research/241` §5b), so it is a real
    oracle for the TERMS — while its `sf` is the thing this engine must not use, which the next
    test pins.
    """
    from scipy.stats import betabinom

    for rate, correlation in ((0.1, 0.1), (0.25, 0.05), (0.4, 0.5), (0.05, 0.25)):
        null = BetaBinomialDisagreementNull(rate, correlation)
        shape_disagree = null.beta_shape_for_disagreement
        shape_agree = null.beta_shape_for_agreement
        for trials in (2, 7, 30):
            for successes in range(1, trials + 1):
                expected = float(betabinom.sf(successes - 1, trials, shape_disagree, shape_agree))
                assert null.upper_tail(successes, trials) == pytest.approx(expected, rel=1e-9)


def test_b5_a_vanishing_tail_keeps_its_digits_where_scipys_returns_zero() -> None:
    """`B5`, re-run against the beta-binomial — and it is why scipy's own tail is not used.

    `betabinom.sf` returns EXACTLY 0.0 here and `logsf` returns `nan`, because it computes
    `log(sf)`. That is the same failure `B5` measured on the binomial path, so adopting the
    library's tail would have reintroduced a fixed bug (`docs/research/241` §5b).
    """
    from scipy.stats import betabinom

    null = BetaBinomialDisagreementNull(0.05, 0.02)
    tail = null.upper_tail(200, 200)
    assert 0.0 < tail < 1e-15, "a tiny probability must stay a number, not collapse to zero"
    assert math.isfinite(math.log(tail))
    assert (
        betabinom.sf(199, 200, null.beta_shape_for_disagreement, null.beta_shape_for_agreement)
        == 0.0
    ), "the library's tail underflows here — pinned so the rejection is not re-litigated"


def test_dispersion_never_makes_the_all_disagree_case_more_surprising() -> None:
    """The invariant the correction rests on — stated where it is actually true.

    Two earlier drafts of this test asserted more than the maths gives. Measured:

    - `rho` does NOT raise the tail monotonically at a general `k`. At `p = 0.1`, `k = 8`,
      `n = 40` it runs 0.0419 -> 0.1916 at `rho = 0.25` and back DOWN to 0.1013, because a
      beta-binomial with `rho -> 1` is all-or-nothing and `P(K = n) = p` exactly.
    - Nor does dispersion always make a count above the mean less surprising. Dispersion moves
      mass to BOTH ends, so near the mean the tail SHRINKS: at `p = 0.05`, `n = 10`, `k = 1`,
      `rho = 0.01` gives 0.3877 against the binomial's 0.4013.

    Where it holds without qualification is `k = n` — every comparison disagreeing — and that is
    the case that matters, because it is the exact quantity `smallest_trials_that_can_reject`
    reads. There the tail is monotone in `rho`, never below the binomial's `p**n`, and rises to
    `p` in the all-or-nothing limit. That is why the corrected null cannot demand LESS evidence
    than the one it replaced, which the next test asserts on the rule itself.
    """
    for rate in (0.05, 0.1, 0.25):
        for trials in (2, 5, 10, 40):
            binomial_extreme = rate**trials
            previous = 0.0
            for correlation in (0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 0.9, 0.99):
                tail = BetaBinomialDisagreementNull(rate, correlation).upper_tail(trials, trials)
                assert tail >= binomial_extreme - 1e-18, f"rate={rate} n={trials}"
                assert tail >= previous - 1e-18, f"not monotone at rate={rate} n={trials}"
                previous = tail
            assert previous == pytest.approx(rate, rel=0.05), (
                "as dispersion approaches its ceiling the instrument is all-or-nothing, so "
                "every comparison disagreeing has probability p regardless of how many there are"
            )


@pytest.mark.property
@given(
    trials=strategy.integers(min_value=1, max_value=60),
    rate=strategy.floats(min_value=0.01, max_value=0.9),
    correlation=strategy.floats(min_value=0.0, max_value=0.95),
)
@settings(max_examples=200, deadline=None)
def test_the_tail_is_a_monotone_probability(trials: int, rate: float, correlation: float) -> None:
    null = BetaBinomialDisagreementNull(rate, correlation)
    assert null.upper_tail(0, trials) == 1.0
    assert null.upper_tail(trials + 1, trials) == 0.0
    previous = 1.0
    for successes in range(0, trials + 1):
        tail = null.upper_tail(successes, trials)
        assert 0.0 <= tail <= 1.0
        assert tail <= previous + 1e-12
        previous = tail


def test_the_evidence_bar_rises_with_dispersion_and_never_falls() -> None:
    """`smallest_trials_that_can_reject` is the rule `B1` complained about, and it is UNCHANGED.
    What changed is the distribution it reads. It must therefore be monotone in dispersion —
    and at `rho = 0` it must return exactly what the binomial form returns.
    """
    for rate in (0.05, 0.1, 0.2445):
        assert BetaBinomialDisagreementNull(rate, 0.0).smallest_trials_that_can_reject(
            SIGNIFICANCE
        ) == smallest_trials_that_can_reject(rate, SIGNIFICANCE)
        previous = 0
        for correlation in (0.0, 0.05, 0.1, 0.25, 0.5):
            minimum = BetaBinomialDisagreementNull(
                rate, correlation
            ).smallest_trials_that_can_reject(SIGNIFICANCE)
            assert minimum is not None
            assert minimum >= previous
            previous = minimum


def test_a_null_nothing_can_be_surprising_against_admits_no_evidence() -> None:
    for correlation in (0.0, 0.3):
        assert (
            BetaBinomialDisagreementNull(1.0, correlation).smallest_trials_that_can_reject(
                SIGNIFICANCE
            )
            is None
        )
        assert (
            BetaBinomialDisagreementNull(0.0, correlation).smallest_trials_that_can_reject(
                SIGNIFICANCE
            )
            == 1
        )


@pytest.mark.property
@given(
    clusters=strategy.lists(
        strategy.tuples(
            strategy.integers(min_value=1, max_value=50),
            strategy.integers(min_value=0, max_value=50),
        ),
        min_size=3,
        max_size=40,
    )
)
@settings(max_examples=150, deadline=None)
def test_leave_one_out_by_subtraction_equals_leave_one_out_from_scratch(
    clusters: list[tuple[int, int]],
) -> None:
    """The `O(1)` identity is only worth having if it is provably the same number.

    `M25` was the last time an input-pipeline speed-up touched this engine, and the test that
    made it safe was one that diffed the fast path against the slow one bar by bar. Same shape.
    """
    pairs = [(count, min(count, disagreements)) for count, disagreements in clusters]
    accumulated = DisagreementNullAccumulator.over(pairs)
    for index, (count, disagreements) in enumerate(pairs):
        by_subtraction = accumulated.excluding(verifiable=count, disagreements=disagreements).null()
        from_scratch = DisagreementNullAccumulator.over(
            pair for position, pair in enumerate(pairs) if position != index
        ).null()
        if from_scratch is None:
            assert by_subtraction is None
            continue
        assert by_subtraction is not None
        assert by_subtraction.disagreement_rate == pytest.approx(
            from_scratch.disagreement_rate, abs=1e-12
        )
        assert by_subtraction.intra_instrument_correlation == pytest.approx(
            from_scratch.intra_instrument_correlation, abs=1e-9
        )


def test_the_dispersion_estimator_recovers_a_planted_correlation() -> None:
    """Simulated clusters with a KNOWN `rho`, and the estimator has to find it.

    Without this the estimator is a formula nobody checked. Both directions matter: it must find
    dispersion that is there, and must report none when there is none.
    """
    import numpy

    generator = numpy.random.default_rng(20260816)
    comparisons_each = 40
    cluster_count = 4_000

    for planted in (0.05, 0.2, 0.4):
        rate = 0.2
        shape_disagree = rate * (1 - planted) / planted
        shape_agree = (1 - rate) * (1 - planted) / planted
        propensities = generator.beta(shape_disagree, shape_agree, size=cluster_count)
        draws = generator.binomial(comparisons_each, propensities)
        estimated = DisagreementNullAccumulator.over(
            (comparisons_each, int(count)) for count in draws
        ).null()
        assert estimated is not None
        assert estimated.intra_instrument_correlation == pytest.approx(planted, abs=0.02), (
            f"planted rho={planted}"
        )

    pure_binomial = generator.binomial(comparisons_each, 0.2, size=cluster_count)
    unclustered = DisagreementNullAccumulator.over(
        (comparisons_each, int(count)) for count in pure_binomial
    ).null()
    assert unclustered is not None
    assert unclustered.intra_instrument_correlation == pytest.approx(0.0, abs=0.01)


def test_one_cluster_cannot_exhibit_dispersion_and_reports_none() -> None:
    """Dispersion is variation BETWEEN instruments. With one, or with no instrument holding two
    comparisons, the honest estimate is zero — the binomial — not an artefact."""
    single = DisagreementNullAccumulator.over([(40, 8)]).null()
    assert single is not None
    assert single.intra_instrument_correlation == 0.0

    no_replication = DisagreementNullAccumulator.over([(1, 1), (1, 0), (1, 1), (1, 0)]).null()
    assert no_replication is not None
    assert no_replication.intra_instrument_correlation == 0.0

    assert DisagreementNullAccumulator.over([]).null() is None


def test_an_instrument_that_disagrees_on_everything_cannot_excuse_itself() -> None:
    """The self-masking case, now for the DISPERSION as well as the rate.

    Instrument 1 disagrees on every one of 40 comparisons while the rest of the session is clean.
    It is the entire source of the session's dispersion, so a non-leave-one-out estimate would
    let it widen the null it then walks through.
    """
    report = session_from_counts(
        [(1, 40, 40)] + [(token, 40, 0) for token in range(2, 12)],
    )
    judged = {item.instrument_token: item for item in report.instruments}
    assert judged[1].verdict is InstrumentJoinVerdict.JOIN_REFUTED
    assert judged[1].null_intra_instrument_correlation == 0.0, (
        "with instrument 1 removed the remaining session is clean, so its own null carries no "
        "dispersion — which is exactly the point of leaving it out"
    )
    # And the converse, which is the half a non-leave-one-out estimate would get wrong: for every
    # OTHER instrument, the broken one is still in the null, and it is the only source of variance
    # there, so their nulls carry near-total dispersion.
    for token in range(2, 12):
        correlation = judged[token].null_intra_instrument_correlation
        assert correlation is not None and correlation > 0.9


def test_one_extreme_instrument_saturates_the_estimator_and_the_rest_go_unverifiable() -> None:
    """A documented property of the estimator, pinned rather than discovered later.

    Pearson's `X^2` is a sum of squared standardised residuals, so ONE cluster at 40/40 in a
    session that is otherwise perfectly clean dominates it. Measured on this exact fixture
    (`[(40, 40)] + [(40, 0)] * 10`): that single cluster contributes **400.0** of a total
    **440.0**, against an expectation of `N - 1` = **10**, and `rho-hat` clamps to its ceiling.
    (An earlier draft of this docstring said "~8,000 against ~200" — wrong by twenty times, and
    the same defect class as the comment `docs/research/240` retracted a sign-off over. Measured
    and corrected 2026-08-16.) The inference is CORRECT for that data — instruments really
    are all-or-nothing there — but it means an all-or-nothing fixture makes every other
    instrument's evidence bar unreachable, so they come back `JOIN_UNVERIFIABLE`.

    Two reasons this is pinned instead of smoothed away. It is only reachable when the rest of
    the session is EXACTLY clean, which real tapes are not — the `R.05` pass measures `rho-hat`
    on three real sessions and `docs/research/241` §5 already names `rho-hat` near 1 as a
    falsification signal for the whole model. And a robust estimator swapped in to hide it would
    be a second undiscussed change riding along with this one.
    """
    report = session_from_counts([(1, 40, 40)] + [(token, 40, 0) for token in range(2, 12)])
    judged = {item.instrument_token: item for item in report.instruments}
    assert report.pooled_intra_instrument_correlation > 0.99
    assert all(
        judged[token].verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE for token in range(2, 12)
    ), "with total between-instrument dispersion, nothing about a clean instrument is surprising"
    assert judged[1].verdict is InstrumentJoinVerdict.JOIN_REFUTED, (
        "the broken instrument must still be caught — its own null excludes itself"
    )


def test_a_dispersed_session_stops_refusing_the_instruments_the_binomial_over_rejected() -> None:
    """The `~594 false refusals of 9,000` finding, as a test.

    A session with NO broken join, but with genuine between-instrument variation in how often a
    close falls outside its own book — which is what 24.45% of two-sided snapshots being
    self-inconsistent means. Under the binomial null the ordinary instruments are refused;
    under the corrected one they are not, and the count comes back to the significance level.
    """
    import numpy

    generator = numpy.random.default_rng(4_2)
    comparisons_each = 40
    rate, planted = 0.2, 0.35
    shape_disagree = rate * (1 - planted) / planted
    shape_agree = (1 - rate) * (1 - planted) / planted
    propensities = generator.beta(shape_disagree, shape_agree, size=120)
    counts = [
        (token + 1, comparisons_each, int(generator.binomial(comparisons_each, propensity)))
        for token, propensity in enumerate(propensities)
    ]

    report = session_from_counts(counts)
    refused_now = len(report.refuted_instruments())

    # The OLD algorithm, written out here because it is the baseline being compared against.
    pooled_verifiable = sum(count for _, count, _ in counts)
    pooled_disagreements = sum(disagreements for _, _, disagreements in counts)
    refused_before = 0
    for _, count, disagreements in counts:
        others_rate = (pooled_disagreements - disagreements) / (pooled_verifiable - count)
        minimum = smallest_trials_that_can_reject(others_rate, SIGNIFICANCE)
        if minimum is not None and count >= minimum:
            refused_before += binomial_upper_tail(disagreements, count, others_rate) < SIGNIFICANCE

    assert report.pooled_intra_instrument_correlation == pytest.approx(planted, abs=0.1)
    assert refused_before > 20, (
        "the binomial null must over-reject here, or there is nothing to fix"
    )
    assert refused_now * 4 < refused_before, (
        f"the corrected null still refuses {refused_now} of {len(counts)} where the binomial "
        f"refused {refused_before}"
    )


def test_the_session_reports_the_null_it_judged_under() -> None:
    """A verdict whose null cannot be named is a verdict that cannot be compared to another
    run's — `B6`'s lesson, applied to the null itself."""
    report = session_from_counts([(1, 40, 0), (2, 40, 1), (3, 40, 0)])
    assert report.null_model is DisagreementNullModel.BETA_BINOMIAL_LEAVE_ONE_OUT
    assert "intra-instrument correlation" in report.describe()
    for item in report.instruments:
        assert item.null_intra_instrument_correlation is not None


# ---------------------------------------- what the first pass of these tests did NOT pin
#
# The `A.123` adversarial review mutated the source 27 ways and SEVEN mutations survived — every
# one on a mechanism a test above claims to cover. These are the tests that kill them. Recorded
# with the mutation each one answers, because a regression test whose motivating mutation is not
# written down decays into a test nobody dares delete and nobody understands.


def test_the_dispersion_estimator_computes_one_exact_hand_checked_value() -> None:
    """Kills every arithmetic mutation of `rho-hat`, which a simulation cannot reliably do.

    A tolerance wide enough to absorb sampling noise is also wide enough to absorb a
    degrees-of-freedom error: with 4,000 replications at N=3 the `(X^2 - N)` mutation shifts the
    mean by 0.013 against a 0.03 tolerance, so it survived. One deterministic fixture, computed by
    hand, does not have that problem.

    Three clusters of 40 comparisons with 10, 5 and 15 disagreements:

        S1 = 100/40 + 25/40 + 225/40 = 8.75      S2 = 30      S3 = 120      N = 3
        p  = 30/120 = 0.25
        X^2 = (8.75 - 0.25 x 30) / (0.25 x 0.75) = 1.25 / 0.1875 = 6.6667
        within-cluster degrees = (120 - 3) x (3-1)/3 = 78
        rho-hat = (6.6667 - 2) / 78 = 0.0598290598...

    The two mutations that survived the review's own suite land at 0.0470 (`X^2 - N`) and 0.0399
    (dropping the `(N-1)/N` factor), both separated from this by more than 1e-3.
    """
    accumulated = DisagreementNullAccumulator.over([(40, 10), (40, 5), (40, 15)])
    assert accumulated.squared_rate_contribution == 8.75
    null = accumulated.null()
    assert null is not None
    assert null.disagreement_rate == 0.25
    assert null.intra_instrument_correlation == pytest.approx(4.66666666666667 / 78, abs=1e-12)


def test_the_dispersion_estimator_is_unbiased_on_only_a_handful_of_instruments() -> None:
    """Kills: `(X^2 - (N-1))` -> `(X^2 - N)`, and the `(N-1)/N` divisor correction itself.

    `test_the_dispersion_estimator_recovers_a_planted_correlation` uses 4,000 clusters, where the
    degrees-of-freedom correction is worth 6e-6 against a 0.02 tolerance — so BOTH mutations
    passed it. The correction only bites at small `N`, which is exactly a `--limit` probe and
    exactly the unit fixtures. Measured against the uncorrected form, at a planted 0.200:
    N=3 lands at 0.124, N=5 at 0.154, N=10 at 0.175. The bias runs LOW, and a low `rho` is a
    narrow null, which is over-rejection.
    """
    import numpy

    generator = numpy.random.default_rng(20260816)
    comparisons_each = 40
    planted, rate = 0.2, 0.2
    shape_disagree = rate * (1 - planted) / planted
    shape_agree = (1 - rate) * (1 - planted) / planted

    for cluster_count, tolerance in ((3, 0.03), (5, 0.03), (10, 0.02)):
        estimates = []
        for _ in range(4_000):
            propensities = generator.beta(shape_disagree, shape_agree, size=cluster_count)
            draws = generator.binomial(comparisons_each, propensities)
            null = DisagreementNullAccumulator.over(
                (comparisons_each, int(count)) for count in draws
            ).null()
            assert null is not None
            estimates.append(null.intra_instrument_correlation)
        mean_estimate = sum(estimates) / len(estimates)
        assert mean_estimate == pytest.approx(planted, abs=tolerance), (
            f"N={cluster_count}: estimator averages {mean_estimate:.4f} for a planted {planted}"
        )


def test_the_evidence_bar_is_the_exact_number_the_spec_publishes() -> None:
    """Kills: `return upper` -> `return upper + 1` in the bisection, and shrinking
    `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT`.

    `test_the_evidence_bar_rises_with_dispersion_and_never_falls` asserts monotonicity (survives a
    uniform +1) and equality with the binomial only at `rho = 0`, which takes the DELEGATION
    branch and never enters the search at all. So the bisection ran unpinned, and an off-by-one
    there moves every instrument holding exactly that many comparisons to `JOIN_UNVERIFIABLE`.

    These are the figures `docs/research/241` §1.1 publishes, so the table and the code cannot
    drift apart either.
    """
    expected = {
        (0.05, 0.05): (2, 2, 2, 2, 2),
        (0.10, 0.05): (2, 2, 2, 2, 3),
        (0.2445, 0.05): (3, 3, 3, 4, 10),
        (0.05, 0.01): (2, 2, 2, 3, 6),
        (0.10, 0.01): (3, 3, 3, 4, 14),
        (0.2445, 0.01): (4, 4, 5, 9, 79),
    }
    for (rate, significance), row in expected.items():
        measured = tuple(
            BetaBinomialDisagreementNull(rate, correlation).smallest_trials_that_can_reject(
                significance
            )
            for correlation in (0.0, 0.05, 0.10, 0.25, 0.50)
        )
        assert measured == row, f"p={rate} significance={significance}"


def test_the_evidence_gate_refuses_at_exactly_one_comparison_below_the_bar() -> None:
    """Kills: `len(verifiable) < minimum_trials` -> `< minimum_trials - 1`.

    The gate boundary IS `B1`, and nothing pinned it. Built so the session's own null puts the bar
    at a known place, then run one instrument at the bar and one a single comparison below it.
    """
    backdrop = [(1, 40, 10), (2, 40, 9), (3, 40, 11), (4, 40, 10), (5, 40, 8)]

    def judge_an_all_disagreeing_instrument_with(comparisons: int) -> InstrumentJoinReport:
        session = session_from_counts([*backdrop, (7, comparisons, comparisons)])
        return {item.instrument_token: item for item in session.instruments}[7]

    at_the_bar = judge_an_all_disagreeing_instrument_with(4)
    assert at_the_bar.minimum_comparisons_to_reject == 4
    assert at_the_bar.verdict is InstrumentJoinVerdict.JOIN_REFUTED, (
        "AT the bar the test could have refused it, and it disagreed on everything, so it must"
    )

    one_below = judge_an_all_disagreeing_instrument_with(3)
    assert one_below.minimum_comparisons_to_reject == 4
    assert one_below.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE, (
        "one comparison below the bar the test could not have refused it, whatever it showed"
    )
    assert one_below.disagreement_upper_tail is None, "no test was run, so there is no tail"


def test_an_underdispersed_session_reports_no_dispersion_rather_than_a_negative_one() -> None:
    """Kills: removing the low clamp on `rho-hat`.

    No fixture above ever produced `X^2 < N - 1`, so a negative estimate would have sailed
    through into `BetaBinomialDisagreementNull`, whose `__post_init__` demands `[0, 1)`. Clusters
    that all sit at exactly the pooled rate are more uniform than binomial sampling would be, and
    that is what produces one.
    """
    identical = DisagreementNullAccumulator.over([(40, 10)] * 8).null()
    assert identical is not None
    assert identical.intra_instrument_correlation == 0.0, (
        "less dispersion than binomial is still no evidence of clustering, and a correlation "
        "cannot be negative"
    )


def test_one_cluster_has_no_dispersion_because_dispersion_is_between_clusters() -> None:
    """Kills: `FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION` 2 -> 1.

    `test_one_cluster_cannot_exhibit_dispersion_and_reports_none` used `over([(40, 8)])`, where
    `X^2 = 0` and `N - 1 = 0` arithmetically, so `rho-hat` is 0 whatever the guard says. A single
    cluster can never disagree with the pooled rate it alone defines — which is precisely why the
    guard exists and why it cannot be tested through the public path. Asserted on the guard.
    """
    single = DisagreementNullAccumulator(
        cluster_count=1,
        verifiable_total=40,
        disagreements_total=8,
        # Deliberately inconsistent with the counts above: no real single cluster can produce this,
        # which is the point — it forces X^2 large so ONLY the guard can return zero.
        squared_rate_contribution=400.0,
    )
    assert single._intra_instrument_correlation(0.2) == 0.0
    assert FEWEST_CLUSTERS_THAT_CAN_SHOW_DISPERSION == 2


def test_the_pmf_sums_to_one() -> None:
    """Test-plan item 3 of `docs/research/241` §6, which the first pass did not write.

    Taken by differencing the tail, so it checks the tail's own telescoping rather than a separate
    pmf that could agree with nothing.
    """
    for rate, correlation in ((0.1, 0.1), (0.25, 0.4), (0.05, 0.02), (0.4, 0.75)):
        null = BetaBinomialDisagreementNull(rate, correlation)
        for trials in (1, 3, 12, 45):
            mass = math.fsum(
                null.upper_tail(count, trials) - null.upper_tail(count + 1, trials)
                for count in range(trials + 1)
            )
            assert mass == pytest.approx(1.0, abs=1e-12), f"p={rate} rho={correlation} n={trials}"


def test_a_beta_shape_that_rounds_to_zero_is_an_error_not_a_math_domain_crash() -> None:
    """`H1` of the `A.123` review. `shape_agree + trials - count` associates left-to-right, so
    with `rho` at its clamp `shape_agree` is ~1.1e-16, `shape_agree + trials` rounds to `trials`,
    and the subtraction is exactly 0.0 — `math.lgamma(0.0)` then raises a bare `ValueError` from
    inside a verification. Reachable on any session with a couple of all-disagreeing instruments.
    """
    saturated = BetaBinomialDisagreementNull(0.005, math.nextafter(1.0, 0.0))
    assert saturated.upper_tail(2, 2) == pytest.approx(0.005, rel=1e-9), (
        "at total dispersion, every comparison disagreeing has probability p"
    )
    assert BetaBinomialDisagreementNull(0.2, math.nextafter(1.0, 0.0)).upper_tail(
        40, 40
    ) == pytest.approx(0.2, rel=1e-9)
    with pytest.raises(JoinVerificationError):
        _log_beta(0.0, 1.0)


def test_a_search_that_would_bisect_on_rounding_noise_returns_no_answer() -> None:
    """`M1` of the `A.123` review. The closed form differences two `lgamma` terms that reach ~1e17,
    so past a certain `n` the result is rounding noise — it returned a POSITIVE log-probability
    (i.e. `P > 1`) and the bisection duly produced 2,101,930,071,441,053, which was written to the
    store and rendered."""
    saturated = BetaBinomialDisagreementNull(0.05, math.nextafter(1.0, 0.0))
    assert saturated.smallest_trials_that_can_reject(0.01) is None, (
        "no n suffices when every comparison disagreeing still has probability 0.05 > 0.01"
    )
    assert BetaBinomialDisagreementNull(0.2445, 0.5).smallest_trials_that_can_reject(0.01) == 79


def test_an_impossible_cluster_is_refused_rather_than_absorbed() -> None:
    """`L4`. `k > n` silently produced a rate above 1 downstream, and `verifiable=0` silently
    discarded its disagreements — both of which turn a caller's bug into a wrong null."""
    with pytest.raises(JoinVerificationError):
        DisagreementNullAccumulator.over([(5, 9)])
    with pytest.raises(JoinVerificationError):
        DisagreementNullAccumulator.over([(0, 3)])


# ------------------------------------------------- the POWER gate (`A.124`, r/242 §2)


def test_the_b1_case_from_the_spec_that_was_never_written() -> None:
    """Test-plan item 8 of `docs/research/241` §6 — specced, not written, and it is the ONE test
    that would have surfaced `H2` before the adversarial review did.

    An instrument with two comparable bars is `JOIN_UNVERIFIABLE` when the session demands enough
    evidence to have caught a half-disagreeing instrument, and `JOIN_VERIFIED` when the session
    only claims to catch total disagreement. Same instrument, same bars, same tape — the verdict
    turns on the claim being made and on nothing else.
    """
    session = [(1, 40, 1), (2, 40, 1), (3, 40, 0), (4, 40, 1), (5, 40, 0), (9, 2, 0)]

    thin_under_a_strong_claim = {
        item.instrument_token: item
        for item in session_from_counts(
            session, minimum_detectable_disagreement_rate=0.5
        ).instruments
    }[9]
    assert thin_under_a_strong_claim.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE, (
        "two agreeing bars cannot rule out an instrument that disagrees half the time"
    )

    thin_under_a_weak_claim = {
        item.instrument_token: item
        for item in session_from_counts(
            session, minimum_detectable_disagreement_rate=1.0
        ).instruments
    }[9]
    assert thin_under_a_weak_claim.verdict is InstrumentJoinVerdict.JOIN_VERIFIED, (
        "against TOTAL disagreement two bars really are decisive, and B1 is then not a defect"
    )


def test_the_power_gate_never_takes_a_refusal_away() -> None:
    """The gate is applied AFTER the test, so `refuted_instruments()` — the set the replay engine
    consumes — cannot change. A correction that silently stopped refusing a broken instrument
    would be far worse than the problem it fixes."""
    session = [(1, 40, 1), (2, 40, 0), (3, 40, 1), (4, 40, 0), (5, 40, 40), (9, 3, 3)]
    refusals = {
        rate: session_from_counts(
            session, minimum_detectable_disagreement_rate=rate
        ).refuted_instruments()
        for rate in (1.0, 0.75, 0.5, 0.25)
    }
    assert len({frozenset(value) for value in refusals.values()}) == 1, (
        f"refusals moved with the claim: {refusals}"
    )
    assert 5 in refusals[0.5], "the all-disagreeing instrument must still be refused"


def test_a_zero_null_can_still_refute_but_can_no_longer_verify_on_one_bar() -> None:
    """`B1`'s original mechanism, retired without a special case.

    `docs/research/240` `B1`: *"`smallest_trials_that_can_reject` returns 1 whenever the
    leave-one-out null is 0, so a single agreeing bar clears the gate."* That is still true and
    still correct — at `p = 0` any disagreement IS decisive, which is how a token collision in a
    clean session is caught. What changed is that refuting and verifying no longer share a gate.

    Measured: at `p = 0` a single disagreement refutes, so the rejection boundary is `k >= 1`, and
    `P(K >= 1 | n, 0.5) = 1 - 0.5**n` first clears 0.99 at **n = 7**. So one agreeing bar is no
    longer a verification, and it took no branch on `p == 0` to get there.
    """
    clean_null = BetaBinomialDisagreementNull(0.0, 0.0)
    assert clean_null.smallest_trials_that_can_reject(0.01) == 1
    assert clean_null.smallest_trials_that_can_verify(0.01, 0.5) == 7
    assert 1.0 - 0.5**7 >= 0.99 > 1.0 - 0.5**6

    # And end to end: the exact fixture `A.122` named — one comparable bar in a hundred — beside
    # two clean instruments, which is the arrangement that flipped it to VERIFIED.
    tape = TapeStub(
        {
            1: [
                snapshot(
                    instrument_token=1, at=SESSION_OPEN + FIVE_MINUTES, last_price_paise=140_000
                )
            ]
        }
    )
    bars = {1: [bar(index=index, close_paise=140_000) for index in range(100)]}
    for token in (2, 3):
        clean_tape, clean_bars = agreeing_session(bar_count=40, instrument_token=token)
        tape.snapshots[token] = clean_tape.snapshots[token]
        bars[token] = clean_bars
    report = engine(tape, minimum_detectable_disagreement_rate=0.5).verify_session(bars)
    one_bar = {item.instrument_token: item for item in report.instruments}[1]
    assert one_bar.comparisons_verifiable == 1
    assert one_bar.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE, (
        "1 comparable bar of 100 reported JOIN_VERIFIED before A.124 — that was B1, live"
    )


def test_the_power_bar_is_monotone_in_what_verified_claims() -> None:
    """A stronger claim can only need more evidence. If it could need less, the gate would be
    rewarding a weaker guarantee with a lower bar."""
    null = BetaBinomialDisagreementNull(0.021679, 0.0726)
    previous: float = 0
    for claim in (1.0, 0.9, 0.8, 0.75, 0.6, 0.5, 0.4):
        bar_value = null.smallest_trials_that_can_verify(
            0.01, claim, largest_trials_worth_searching=400
        )
        # `None` means "more evidence than the search reaches", which is the TOP of the order
        # rather than an exception to it — a claim so strong nothing available can support it.
        effective = math.inf if bar_value is None else float(bar_value)
        assert effective >= previous, (
            f"claim {claim} needs {bar_value}, a weaker one needed {previous}"
        )
        previous = effective
    assert previous == math.inf, "the strongest claims tested must run past the search bound"


def test_a_claim_no_amount_of_evidence_could_support_verifies_nothing() -> None:
    """`None` rather than a large number: if no reachable comparison count gives the required
    power, the honest answer is that this session cannot verify anything, and every instrument it
    does not refuse is `JOIN_UNVERIFIABLE`."""
    # A claim barely above the null's own mean is one the test cannot separate from ordinary
    # behaviour at any n.
    null = BetaBinomialDisagreementNull(0.0217, 0.0726)
    # Capped explicitly: unbounded this took 67-80 SECONDS inside a unit test, scanning sample
    # sizes an instrument cannot reach (`M3`, `L-15`). The DEFAULT cap is pinned separately and
    # cheaply by `test_the_default_scan_cap_is_pinned_by_the_published_answer`.
    assert (
        null.smallest_trials_that_can_verify(0.01, 0.03, largest_trials_worth_searching=200) is None
    )

    session = session_from_counts(
        [(1, 40, 1), (2, 40, 0), (3, 40, 1)], minimum_detectable_disagreement_rate=0.03
    )
    assert session.minimum_comparisons_to_verify is None
    assert all(
        item.verdict is InstrumentJoinVerdict.JOIN_UNVERIFIABLE for item in session.instruments
    )


def test_the_report_states_the_claim_its_verdicts_rest_on() -> None:
    session = session_from_counts(
        [(1, 40, 1), (2, 40, 0), (3, 40, 1), (4, 40, 0)],
        minimum_detectable_disagreement_rate=0.5,
    )
    assert session.minimum_detectable_disagreement_rate == 0.5
    assert session.minimum_comparisons_to_verify is not None
    assert "to verify at a 50% detectable rate" in session.describe()


# ------------------------- what the POWER gate's first tests did not pin (`A.124` review)
#
# 18 mutations, 8 survived. Two of them — an unpinned magnitude constant and a missing gate
# boundary — are the SAME TWO CLASSES that survived the previous round, reproduced inside the
# slice written to apply that round's lesson. These are the tests that kill them.


def test_power_is_not_monotone_in_n_so_the_bar_cannot_be_the_gate() -> None:
    """`H1`. The defect that shipped: the gate asked `n >= bar`, and `bar` is the SMALLEST n with
    enough power, not the smallest n after which every n has enough.

    Measured on 2026-08-11's own fitted null at a 0.5 claim and 1% significance: the bar is 22,
    and n=24 has power **0.9887** against a required 0.99 — because the rejection boundary steps
    from k*=6 to k*=7 there. Ten instruments sat at exactly 24 comparisons in the live store and
    were reported JOIN_VERIFIED, with the dashboard printing the claim over them.
    """
    null = BetaBinomialDisagreementNull(0.021679, 0.0726)
    claim = 0.75  # the `A.125` operating claim; 0.5 is unreachable under a dependent alternative
    assert (
        null.smallest_trials_that_can_verify(0.01, claim, largest_trials_worth_searching=200) == 12
    )
    powers = {
        trials: null._alternative(claim).upper_tail(
            null._fewest_disagreements_that_reject(trials, 0.01) or 0, trials
        )
        for trials in (12, 13, 14, 15)
    }
    assert powers[14] < 0.99 < powers[12], f"the sawtooth is the whole point: {powers}"
    assert null.has_power_to_verify(12, 0.01, claim)
    assert null.has_power_to_verify(13, 0.01, claim)
    assert not null.has_power_to_verify(14, 0.01, claim), (
        "a bar comparison admits this (14 >= 12); asking at the instrument's own n does not"
    )
    assert null.has_power_to_verify(15, 0.01, claim)


def test_more_comparisons_can_mean_less_power_and_the_verdict_follows() -> None:
    """`H1` end to end, on a fixture that genuinely lands in a power trough.

    The version of this test written with `H1` asserted
    `verdict is JOIN_UNVERIFIABLE or null_disagreement_rate` — and passed through the `or` while
    the verdict was `JOIN_VERIFIED`. It asserted the opposite of its own name for two review rounds
    (`MEDIUM-7`). Its fixture used the default claim of 1.0, where the bar is 2 and 24 comparisons
    have full power, so there was no trough to land in.

    Here the session's own null puts a trough between 9 and 10 comparisons at a 0.75 claim: **nine
    agreeing comparisons verify and ten do not**, because the rejection boundary `k*` steps up
    between them. That is the whole of `H1` — a bar comparison would admit both.
    """
    backdrop = [(token, 40, 4) for token in range(1, 9)]

    def verdict_on(comparisons: int) -> InstrumentJoinVerdict:
        report = session_from_counts(
            [*backdrop, (99, comparisons, 0)], minimum_detectable_disagreement_rate=0.75
        )
        return {item.instrument_token: item for item in report.instruments}[99].verdict

    assert verdict_on(9) is InstrumentJoinVerdict.JOIN_VERIFIED
    assert verdict_on(10) is InstrumentJoinVerdict.JOIN_UNVERIFIABLE, (
        "ten comparisons carry LESS power than nine here; a gate that compared n to a minimum "
        "would verify this one"
    )


def test_the_gate_boundary_is_pinned_at_exactly_one_comparison() -> None:
    """`A8`. The previous round's surviving mutation was `< minimum_trials` -> `< minimum_trials-1`
    on the SIZE gate; it survived again on the power gate, which was written to fix it. Pinned by
    running the same instrument either side of its own boundary."""
    backdrop = [(1, 40, 0), (2, 40, 0)]

    def verdict_with(comparisons: int) -> InstrumentJoinVerdict:
        judged = {
            item.instrument_token: item
            for item in session_from_counts(
                [*backdrop, (3, comparisons, 0)], minimum_detectable_disagreement_rate=0.5
            ).instruments
        }
        return judged[3].verdict

    assert verdict_with(6) is InstrumentJoinVerdict.JOIN_UNVERIFIABLE
    assert verdict_with(7) is InstrumentJoinVerdict.JOIN_VERIFIED, (
        "at p=0 the boundary is 7 agreeing bars: 1 - 0.5**7 = 0.9922 >= 0.99 > 1 - 0.5**6"
    )


def test_the_power_scan_bound_is_pinned_by_a_large_but_finite_answer() -> None:
    """`A6`. Nothing asserted a large finite answer, so shrinking the scan bound survived — the
    verbatim repeat of the previous round's `LARGEST_EXACTLY_REPRESENTABLE_TRIAL_COUNT` survivor.
    """
    null = BetaBinomialDisagreementNull(0.029203, 0.0399)
    assert (
        null.smallest_trials_that_can_verify(0.01, 0.4, largest_trials_worth_searching=1_000) == 110
    )
    assert (
        null.smallest_trials_that_can_verify(0.01, 0.4, largest_trials_worth_searching=109) is None
    ), "the bound must actually bound: one short of the answer returns None"


def test_the_evidence_demand_is_searched_only_as_far_as_the_session_can_reach() -> None:
    """`M3`. The scan cost 82-240 seconds per session and 143 seconds inside one unit test, all of
    it above the largest comparison count any instrument can hold (the widest real session's
    maximum is 67). The session's own maximum is the derived bound."""
    null = BetaBinomialDisagreementNull(0.021679, 0.0726)
    assert (
        null.smallest_trials_that_can_verify(0.01, 0.03, largest_trials_worth_searching=75) is None
    )


def test_one_saturating_instrument_still_poisons_the_session_and_that_is_not_the_pooling() -> None:
    """`M6`, and the review's proposed conclusion is WRONG — recorded so it is not repeated.

    The review said the per-instrument leave-one-out predicate "closes this too". Measured, it
    does not. Leave-one-out removes only the instrument BEING JUDGED, so a single all-disagreeing
    instrument sits in every OTHER instrument's null and drives `rho-hat` there just as high:

        one saturator among 4  -> pooled rho 1.0000, a clean instrument's own LOO rho 1.0000
        one saturator among 12 -> pooled rho 0.8300, its own LOO rho 0.8544
        one saturator among 40 -> pooled rho 0.4937, its own LOO rho 0.5007

    What the fix DID change is the mechanism: verification is no longer vetoed session-wide by a
    single `minimum_comparisons_to_verify is None`, it is decided per instrument. The outcome here
    is the same because the contamination is in the DATA, not in the pooling.

    **And that outcome is correct inference, not a defect.** If one instrument disagrees on every
    bar while the rest agree on every bar, instruments in that session really are all-or-nothing,
    and agreement on 40 bars really does say little. It is the same property as backlog `M29`, and
    it has the same reachability: it needs a session that is otherwise EXACTLY clean, while the
    three real sessions measure `rho-hat` at 0.0399-0.0740.
    """
    accumulated = DisagreementNullAccumulator.over([(40, 1), (40, 1), (40, 0), (40, 40), (25, 0)])
    pooled = accumulated.null()
    assert pooled is not None
    assert pooled.intra_instrument_correlation > 0.99
    assert (
        pooled.smallest_trials_that_can_verify(0.01, 0.5, largest_trials_worth_searching=75) is None
    )

    # The clean instrument's OWN null, with itself removed, is just as saturated — which is the
    # half the review got wrong.
    own = accumulated.excluding(verifiable=40, disagreements=1).null()
    assert own is not None
    assert own.intra_instrument_correlation > 0.99
    assert not own.has_power_to_verify(40, 0.01, 0.5)

    report = session_from_counts(
        [(1, 40, 1), (2, 40, 1), (3, 40, 0), (4, 40, 40), (5, 25, 0)],
        minimum_detectable_disagreement_rate=0.5,
    )
    verdicts = {item.instrument_token: item.verdict for item in report.instruments}
    assert verdicts[4] is InstrumentJoinVerdict.JOIN_REFUTED, "the saturator is still caught"
    assert all(
        verdicts[token] is InstrumentJoinVerdict.JOIN_UNVERIFIABLE for token in (1, 2, 3, 5)
    ), "and nothing else can be verified against a session that is all-or-nothing"


def test_a_tiny_claim_is_not_printed_as_zero_percent() -> None:
    """`L1`. `:.0%` rendered a 0.004 claim as "0%", so the report asserted the engine claimed
    nothing while it was in fact claiming something very demanding."""
    report = session_from_counts(
        [(1, 40, 1), (2, 40, 0), (3, 40, 1)], minimum_detectable_disagreement_rate=0.004
    )
    assert "0.4% detectable rate" in report.describe()
    assert "0% detectable rate" not in report.describe()


# ------------------------------------- the alternative carries the session's own dependence (M32)


def test_the_alternative_is_not_independent_either() -> None:
    """`M32`/`A.125`. The null became beta-binomial because comparisons within one instrument are
    correlated; modelling the ALTERNATIVE as Binomial reinstated that assumption on the other side.

    The correction can only ever RAISE the bar — a dependence-aware alternative is harder to
    detect — so the previous model was the permissive one and nothing was being wrongly refused.
    """
    null = BetaBinomialDisagreementNull(0.021679, 0.0726)
    for claim in (0.9, 0.8, 0.75):
        dependent = null.smallest_trials_that_can_verify(
            0.01, claim, largest_trials_worth_searching=400
        )
        independent = next(
            trials
            for trials in range(2, 401)
            if (k := null._fewest_disagreements_that_reject(trials, 0.01)) is not None
            and binomial_upper_tail(k, trials, claim) >= 0.99
        )
        assert dependent is not None and dependent > independent, (
            f"claim {claim}: dependent bar {dependent} must exceed the independent {independent}"
        )


def test_a_total_disagreer_is_detected_identically_under_both_models() -> None:
    """At a claim of 1.0 the two models coincide exactly, because an instrument that disagrees on
    EVERY bar does so under any correlation. That is the claim the observed defect population
    actually sits at — `HINDPETRO` 60/60, `XCHANGING` 56/56 — which is why the bar is 2 either
    way for the defects this engine has really found."""
    for rate, correlation in ((0.021679, 0.0726), (0.029203, 0.0399), (0.05, 0.5)):
        null = BetaBinomialDisagreementNull(rate, correlation)
        assert null.smallest_trials_that_can_verify(
            0.01, 1.0, largest_trials_worth_searching=200
        ) == null.smallest_trials_that_can_reject(0.01)


def test_the_dependence_aware_bars_reproduce_the_measured_sessions() -> None:
    """The figures `docs/research/243` publishes, pinned so the record and the code cannot drift.

    A 0.5 claim is UNREACHABLE on two of the three sessions once the assumption is removed, which
    is why `A.125` moved the operating claim to 0.75.
    """
    expected: dict[str, tuple[tuple[float, float], dict[float, int | None]]] = {
        "2026-08-11": ((0.021679, 0.0726), {1.0: 2, 0.9: 6, 0.8: 11, 0.75: 12, 0.5: None}),
        "2026-08-12": ((0.029203, 0.0399), {1.0: 2, 0.9: 6, 0.8: 8, 0.75: 9, 0.5: 33}),
        "2026-08-13": ((0.025591, 0.0740), {1.0: 2, 0.9: 6, 0.8: 11, 0.75: 12, 0.5: None}),
    }
    for label, ((rate, correlation), rows) in expected.items():
        null = BetaBinomialDisagreementNull(rate, correlation)
        for claim, bar in rows.items():
            assert (
                null.smallest_trials_that_can_verify(
                    0.01, claim, largest_trials_worth_searching=200
                )
                == bar
            ), f"{label} at a {claim} claim"


def test_the_default_scan_cap_is_pinned_by_the_published_answer() -> None:
    """`M16` — the THIRD consecutive round in which an unpinned magnitude constant survived
    mutation, this time on the very constant the previous round's fix was meant to pin.

    `test_the_power_scan_bound_is_pinned_by_a_large_but_finite_answer` passes
    `largest_trials_worth_searching` EXPLICITLY, so it never touches
    `LARGEST_TRIALS_SEARCHED_FOR_POWER` and shrinking that constant survived. This calls the
    default, and pins the 240 `docs/research/243` §2.2 publishes for a 0.5 claim.
    """
    assert LARGEST_TRIALS_SEARCHED_FOR_POWER == 1_000
    assert (
        BetaBinomialDisagreementNull(0.021679, 0.0726).smallest_trials_that_can_verify(0.01, 0.5)
        == 240
    ), "the published 240 is only reachable if the default cap is not shrunk"


def test_the_power_boundary_is_an_inclusive_comparison() -> None:
    """`L-11`. `>= 1 - significance` -> `>` survived mutation; at an exact tie the gate would flip
    while every reported number stayed identical."""
    exact = BetaBinomialDisagreementNull(0.0, 0.0)
    assert exact.has_power_to_verify(1, 0.2, 0.8) is True, (
        "power 0.8 against a required 1 - 0.2 = 0.8 is sufficient, not insufficient"
    )


def test_the_claim_is_evaluated_under_both_readings_and_the_weaker_governs() -> None:
    """`H1` of the `A.125` review, and it falsified `243` §2.2's central safety claim.

    "The dependence-aware correction can only RAISE the bar" is FALSE. Where the rejection
    boundary sits at `k* = n`, power collapses to `P(K = n)`, and the beta-binomial's decays
    POLYNOMIALLY where the binomial's decays geometrically — so the dependent model has MORE power
    there and would demand LESS evidence. Measured: at `p = 0.021679`, `rho = 0.6`, claim 0.995,
    the dependent bar is 3 and the independent bar is 7.

    The two are different readings of one claim — a broken instrument at exactly the claim, or one
    distributed around it — and the dashboard prints the fixed-rate guarantee. The gate takes the
    smaller power so both readings hold.
    """
    null = BetaBinomialDisagreementNull(0.021679, 0.6)
    rejecting = null._fewest_disagreements_that_reject(3, 0.01)
    assert rejecting == 3, "the boundary must sit at k* = n for the divergence to appear"
    dependent = null._alternative(0.995).upper_tail(rejecting, 3)
    independent = binomial_upper_tail(rejecting, 3, 0.995)
    assert dependent > independent, "the dependent model really is the more powerful one here"

    assert not null.has_power_to_verify(3, 0.01, 0.995), (
        "gating on the dependent model alone would verify this on three comparisons"
    )
    assert null.smallest_trials_that_can_verify(0.01, 0.995, largest_trials_worth_searching=80) == 7

    # And where the real sessions actually sit, the two readings agree, so the guard costs nothing.
    for rate, correlation, bar in ((0.021679, 0.0726, 12), (0.029203, 0.0399, 9)):
        assert (
            BetaBinomialDisagreementNull(rate, correlation).smallest_trials_that_can_verify(
                0.01, 0.75, largest_trials_worth_searching=200
            )
            == bar
        )


def test_an_alternative_outside_the_unit_interval_is_refused() -> None:
    """`L-9`. A special-cased `>= 1.0` arm silently accepted rates ABOVE 1 and answered `True`,
    where the pre-`M32` `binomial_upper_tail` raised."""
    null = BetaBinomialDisagreementNull(0.02, 0.07)
    for outside in (0.0, -0.1, 1.5):
        with pytest.raises(JoinVerificationError):
            null._alternative(outside)
    assert null._alternative(1.0).disagreement_rate == 1.0


def test_the_independent_column_the_safety_argument_rests_on_is_pinned() -> None:
    """`L-12`. `docs/research/243` §2.2 compares a dependent bar against an independent one, and
    only the dependent column was pinned — so the comparison the whole argument rests on could
    drift silently. These are the independent-alternative figures in that table."""
    expected: dict[str, tuple[tuple[float, float], dict[float, int]]] = {
        "2026-08-11": ((0.021679, 0.0726), {1.0: 2, 0.9: 4, 0.8: 7, 0.75: 8, 0.5: 22}),
        "2026-08-12": ((0.029203, 0.0399), {1.0: 2, 0.9: 5, 0.8: 7, 0.75: 8, 0.5: 19}),
        "2026-08-13": ((0.025591, 0.0740), {1.0: 2, 0.9: 5, 0.8: 7, 0.75: 9, 0.5: 25}),
    }
    for label, ((rate, correlation), rows) in expected.items():
        null = BetaBinomialDisagreementNull(rate, correlation)
        for claim, bar in rows.items():
            independent = next(
                trials
                for trials in range(2, 401)
                if (k := null._fewest_disagreements_that_reject(trials, 0.01)) is not None
                and binomial_upper_tail(k, trials, claim) >= 0.99
            )
            assert independent == bar, (
                f"{label} at a {claim} claim under an independent alternative"
            )
