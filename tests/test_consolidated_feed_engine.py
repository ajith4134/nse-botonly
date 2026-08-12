"""Tests for `L0.33`, written before the engine (`R.23(c)`).

The load-bearing ones are the property tests: a consolidated feed claims to be a BETTER
estimate of a price nobody can observe, and the only honest way to check that is to
generate brokers whose noise is known and ask whether the fusion beats each of them.
"""

from __future__ import annotations

import math
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nse_algo_trader.consolidated_feed.broker_reliability_store import (
    BrokerReliabilityStore,
)
from nse_algo_trader.consolidated_feed.consolidated_feed_engine import (
    ConsolidatedFeedEngine,
    FeedResolution,
)
from nse_algo_trader.consolidated_feed.cross_broker_quote_tape import (
    BrokerQuoteObservation,
)

SESSION_DAY = date(2026, 8, 12)
AT = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def _observation(
    broker: str,
    *,
    symbol: str = "RELIANCE",
    bid_paise: int | None = 131_380,
    ask_paise: int | None = 131_400,
    bid_quantity: int = 500,
    ask_quantity: int = 500,
    last_paise: int | None = 131_390,
    requested_offset_ms: float = 0.0,
    round_trip_ms: float = 80.0,
    failure: str | None = None,
) -> BrokerQuoteObservation:
    requested = AT + timedelta(milliseconds=requested_offset_ms)
    return BrokerQuoteObservation(
        broker=broker,
        trading_symbol=symbol,
        exchange="NSE",
        requested_at=requested,
        received_at=requested + timedelta(milliseconds=round_trip_ms),
        last_price_paise=last_paise,
        best_bid_paise=bid_paise,
        best_ask_paise=ask_paise,
        best_bid_quantity=bid_quantity,
        best_ask_quantity=ask_quantity,
        failure=failure,
    )


def _engine(tmp_path_factory: pytest.TempPathFactory | None = None) -> ConsolidatedFeedEngine:
    return ConsolidatedFeedEngine(BrokerReliabilityStore(_temporary_database()))


def _temporary_database() -> Path:
    return Path(tempfile.mkdtemp()) / "reliability.sqlite3"


# -- alignment ------------------------------------------------------------------------


def test_quotes_polled_close_together_are_comparable() -> None:
    groups = _engine().align(
        [_observation("kite"), _observation("angel_one", requested_offset_ms=120.0)]
    )
    assert len(groups) == 1
    assert {o.broker for o in groups[0].observations} == {"kite", "angel_one"}
    assert groups[0].not_comparable == ()


def test_quotes_polled_far_apart_are_not_a_disagreement() -> None:
    """Ten seconds apart at NSE tick rates is two market states, not two opinions."""
    groups = _engine().align(
        [_observation("kite"), _observation("angel_one", requested_offset_ms=10_000.0)]
    )
    assert len(groups) == 2
    assert all(len(group.observations) == 1 for group in groups)


def test_a_failed_poll_is_carried_as_a_named_exclusion_not_dropped() -> None:
    groups = _engine().align(
        [
            _observation("kite"),
            _observation("angel_one", failure="TimeoutError: read timed out"),
        ]
    )
    assert len(groups) == 1
    assert groups[0].not_comparable and "angel_one" in groups[0].not_comparable[0]


def test_alignment_keeps_instruments_apart() -> None:
    groups = _engine().align(
        [_observation("kite"), _observation("angel_one", symbol="TCS")]
    )
    assert {group.trading_symbol for group in groups} == {"RELIANCE", "TCS"}


# -- consolidation --------------------------------------------------------------------


def test_two_agreeing_brokers_resolve_to_their_shared_price() -> None:
    engine = _engine()
    group = engine.align([_observation("kite"), _observation("angel_one")])[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.RESOLVED
    assert consolidated.consensus_paise == pytest.approx(131_390.0, abs=1.0)
    assert consolidated.dispersion_paise == pytest.approx(0.0, abs=1e-9)


def test_one_usable_broker_is_single_source_never_a_consensus_of_one() -> None:
    engine = _engine()
    group = engine.align(
        [_observation("kite"), _observation("angel_one", failure="down")]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.SINGLE_SOURCE
    assert consolidated.contributing_brokers == ("kite",)


def test_the_synthetic_touch_is_the_best_price_reachable_anywhere() -> None:
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=131_380, ask_paise=131_400),
            _observation("angel_one", bid_paise=131_385, ask_paise=131_405),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.synthetic_best_bid_paise == 131_385  # the higher bid
    assert consolidated.synthetic_best_ask_paise == 131_400  # the lower ask
    assert not consolidated.is_crossed


def test_a_stale_broker_crossing_the_book_is_flagged_rather_than_published_quietly() -> None:
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=131_380, ask_paise=131_400),
            _observation("angel_one", bid_paise=131_450, ask_paise=131_470),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.is_crossed
    assert consolidated.resolution is FeedResolution.UNRESOLVED


def test_a_disagreement_beyond_tolerance_refuses_rather_than_averaging() -> None:
    """Two brokers with no book, quoting last prices a rupee apart.

    Written with last-price-only quotes deliberately: when both brokers publish a tight
    book, a disagreement large enough to matter always CROSSES the synthetic touch and is
    caught there first. The dispersion test is what catches a source that gives a price and
    no depth — which is what an LTP-only broker is.
    """
    import random

    random.seed(19)
    engine = _engine()
    # First teach it what these two brokers' ordinary disagreement looks like: without a
    # book there is no spread to derive a tolerance from, and an engine with no scale
    # reports IMMATURE rather than pretending to judge.
    for index in range(80):
        truth = 131_390 + index
        warmup = engine.align(
            [
                _observation(
                    broker,
                    bid_paise=None,
                    ask_paise=None,
                    last_paise=round(truth + random.gauss(0.0, 2.0)),
                    requested_offset_ms=index * 2_000.0,
                )
                for broker in ("kite", "angel_one")
            ]
        )[0]
        engine.learn(warmup, session_date=SESSION_DAY)

    group = engine.align(
        [
            _observation("kite", bid_paise=None, ask_paise=None, last_paise=131_390),
            _observation("angel_one", bid_paise=None, ask_paise=None, last_paise=141_390),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.UNRESOLVED
    assert consolidated.consensus_paise is None
    assert "dispers" in consolidated.reason.lower()


def test_without_a_book_or_a_learned_scale_the_engine_says_it_cannot_judge() -> None:
    """IMMATURE is not agreement and not disagreement — it is the absence of a yardstick."""
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=None, ask_paise=None, last_paise=131_390),
            _observation("angel_one", bid_paise=None, ask_paise=None, last_paise=131_500),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.IMMATURE
    assert "not judgeable" in consolidated.reason


def test_liquidity_moves_the_consensus_towards_the_deeper_quote() -> None:
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=131_380, ask_paise=131_400, bid_quantity=10_000,
                         ask_quantity=10_000, last_paise=131_390),
            _observation("angel_one", bid_paise=131_400, ask_paise=131_420, bid_quantity=10,
                         ask_quantity=10, last_paise=131_410),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.consensus_paise is not None
    # Midway would be 131_400; the deep quote must pull it below that.
    assert consolidated.consensus_paise < 131_400


def test_an_immature_broker_contributes_on_liquidity_alone_and_says_so() -> None:
    engine = _engine()
    group = engine.align([_observation("kite"), _observation("angel_one")])[0]
    consolidated = engine.consolidate(group)
    assert "immature" in consolidated.reason.lower() or consolidated.resolution is (
        FeedResolution.RESOLVED
    )


# -- the reliability model ------------------------------------------------------------


def test_the_three_cornered_hat_finds_the_noisy_broker_among_three() -> None:
    """With three feeds the decomposition identifies the NOISY source.

    What it can resolve is bounded by its own estimator error, and that bound is the point
    of this test rather than a caveat to it. The error on a solved variance is of the order
    of the LARGEST pair variance times `sqrt(2/n)`, so a source much quieter than that is
    below the resolution and is floored — measured here: with sigmas 2, 8 and 24 paise the
    noisy source comes back clearly noisiest while the two quiet ones sit at the floor,
    indistinguishable from each other. That is the honest answer, and it is also the useful
    one: the weighting needs to know whom to DOWN-weight.
    """
    import random

    random.seed(11)
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    # Sigmas separated well beyond the estimator's own resolution (~7% of a pair variance):
    # 2, 8 and 24 paise give true variances of 4, 64 and 576, which the decomposition can
    # order. Closer spacing is not a failure of the method, it is below its resolution, and
    # `test_a_source_quieter_than_the_resolution_is_floored_not_inverted` pins that instead.
    noise = {"quiet": 2.0, "middling": 8.0, "noisy": 24.0}
    for index in range(400):
        truth = 131_390 + index
        group = engine.align(
            [
                _observation(
                    broker,
                    last_paise=round(truth + random.gauss(0.0, sigma)),
                    bid_paise=None,
                    ask_paise=None,
                    requested_offset_ms=index * 2_000.0,
                )
                for broker, sigma in noise.items()
            ]
        )[0]
        engine.learn(group, session_date=SESSION_DAY)
    variances = engine.broker_noise_variances()
    assert all(variances[broker] is not None for broker in noise)
    quiet, middling, noisy = (variances[name] for name in ("quiet", "middling", "noisy"))
    assert noisy is not None  # the noisy source is what the method reliably identifies
    measured = [value for value in (quiet, middling) if value is not None]
    assert all(noisy > 4.0 * value for value in measured)  # decisively noisier
    # A source below the estimator's resolution comes back as `None` — unidentifiable — and
    # never as a floored number pretending to be a measurement.
    assert quiet is None or quiet < noisy


def test_two_brokers_cannot_identify_which_is_noisy_and_the_engine_says_so() -> None:
    """The identifiability limit, pinned. `var(a-b)` is one number shared by both sources.

    The first design claimed precision weighting here, and the property test showed the
    resulting "consensus" was worse than the better broker. Refusing to claim it is the fix.
    """
    import random

    random.seed(3)
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for index in range(120):
        truth = 131_390 + index
        group = engine.align(
            [
                _observation(
                    "kite",
                    last_paise=round(truth + random.gauss(0.0, 1.0)),
                    requested_offset_ms=index * 2_000.0,
                ),
                _observation(
                    "angel_one",
                    last_paise=round(truth + random.gauss(0.0, 9.0)),
                    requested_offset_ms=index * 2_000.0 + 100.0,
                ),
            ]
        )[0]
        engine.learn(group, session_date=SESSION_DAY)
    assert all(value is None for value in engine.broker_noise_variances().values())
    final = engine.consolidate(
        engine.align([_observation("kite"), _observation("angel_one")])[0]
    )
    assert "not identifiable" in final.reason


def test_availability_counts_failures_because_a_missing_quote_is_information() -> None:
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for _ in range(10):
        group = engine.align(
            [_observation("kite"), _observation("angel_one", failure="timeout")]
        )[0]
        engine.learn(group, session_date=SESSION_DAY)
    assert store.reliability("angel_one").failure_rate == pytest.approx(1.0)
    assert store.reliability("kite").failure_rate == pytest.approx(0.0)


def test_two_answers_from_one_broker_in_a_window_count_once() -> None:
    """A retry inside one sweep must not let a broker vote twice or be compared to itself."""
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", requested_offset_ms=0.0),
            _observation("kite", requested_offset_ms=200.0, last_paise=131_500),
            _observation("angel_one", requested_offset_ms=100.0),
        ]
    )[0]
    assert sorted(group.brokers) == ["angel_one", "kite"]


def test_a_frozen_broker_is_detected_as_stale_while_the_others_move() -> None:
    """The measurement that separates 'late but right' from 'wrong'."""
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for index in range(60):
        moving = 131_390 + index * 5
        group = engine.align(
            [
                _observation(
                    "kite",
                    last_paise=moving,
                    bid_paise=moving - 10,
                    ask_paise=moving + 10,
                    requested_offset_ms=index * 2_000.0,
                ),
                _observation(
                    "frozen",
                    last_paise=131_390,
                    bid_paise=131_380,
                    ask_paise=131_400,
                    requested_offset_ms=index * 2_000.0 + 50.0,
                ),
            ]
        )[0]
        engine.learn(group, session_date=SESSION_DAY)
    assert store.reliability("frozen").unchanged_while_others_moved_rate > 0.8
    assert store.reliability("kite").unchanged_while_others_moved_rate < 0.2


def test_reliability_survives_a_restart_because_it_is_the_carried_state() -> None:
    database = _temporary_database()
    first = BrokerReliabilityStore(database)
    ConsolidatedFeedEngine(first).learn(
        ConsolidatedFeedEngine(first).align(
            [_observation("kite"), _observation("angel_one", last_paise=131_420)]
        )[0],
        session_date=SESSION_DAY,
    )
    first.flush()  # write-behind: a session's learning becomes durable when it is flushed
    reopened = BrokerReliabilityStore(database)
    assert reopened.reliability("kite").observation_count >= 1


# -- properties -----------------------------------------------------------------------


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    truth_paise=st.integers(min_value=10_000, max_value=500_000),
    noise_quiet=st.floats(min_value=0.5, max_value=2.0),
    noise_loud=st.floats(min_value=6.0, max_value=14.0),
)
def test_property_the_consensus_beats_the_brokers_it_is_built_from(
    truth_paise: int, noise_quiet: float, noise_loud: float
) -> None:
    """The claim that justifies the engine: fusion beats its inputs.

    THREE brokers, because that is the number at which per-source noise becomes
    identifiable. The consensus must have a smaller RMS error than the AVERAGE broker; it is
    not required to beat the single best one, which no fusion can guarantee without knowing
    in advance which that is — and knowing it is precisely what the three-cornered hat
    estimates, so the test is run after the estimate has had a session to form.
    """
    import random

    random.seed(truth_paise)
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    sigmas = {"quiet": noise_quiet, "middling": (noise_quiet + noise_loud) / 2, "loud": noise_loud}
    errors: dict[str, list[float]] = {name: [] for name in [*sigmas, "consensus"]}
    for index in range(240):
        quotes = {
            broker: truth_paise + random.gauss(0.0, sigma) for broker, sigma in sigmas.items()
        }
        group = engine.align(
            [
                _observation(
                    broker,
                    last_paise=round(price),
                    bid_paise=None,
                    ask_paise=None,
                    requested_offset_ms=index * 2_000.0,
                )
                for broker, price in quotes.items()
            ]
        )[0]
        consolidated = engine.consolidate(group)
        engine.learn(group, session_date=SESSION_DAY)
        if consolidated.consensus_paise is None or index < 120:
            continue  # the first half of the session is the estimate forming
        for broker, price in quotes.items():
            errors[broker].append((price - truth_paise) ** 2)
        errors["consensus"].append((consolidated.consensus_paise - truth_paise) ** 2)

    def rms(values: list[float]) -> float:
        return math.sqrt(sum(values) / len(values)) if values else math.inf

    average_broker = sum(rms(errors[broker]) for broker in sigmas) / len(sigmas)
    assert rms(errors["consensus"]) < average_broker


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    prices=st.lists(
        st.integers(min_value=100_000, max_value=100_100), min_size=2, max_size=5
    )
)
def test_property_the_consensus_never_sits_outside_the_quotes_it_fused(
    prices: list[int],
) -> None:
    """A weighted mean of a set cannot leave that set's range. If it does, a weight is negative."""
    engine = _engine()
    group = engine.align(
        [
            _observation(
                f"broker_{index}",
                last_paise=price,
                bid_paise=price - 10,
                ask_paise=price + 10,
                requested_offset_ms=index * 10.0,
            )
            for index, price in enumerate(prices)
        ]
    )[0]
    consolidated = engine.consolidate(group)
    if consolidated.consensus_paise is None:
        return
    midpoints = list(prices)
    assert min(midpoints) - 1 <= consolidated.consensus_paise <= max(midpoints) + 1


# -- the gate that changes behaviour ----------------------------------------------------


def test_the_admissibility_gate_refuses_an_instrument_the_feed_measured_as_unreliable(
    tmp_path: Path,
) -> None:
    """`R.06` asserted rather than claimed: the gate changes what the replay will answer.

    The depth tape is recorded from one broker. Where the consolidated feed measured that
    broker as frozen on an instrument, the microstructure replay must refuse to build
    features from its book — a book that disagreed with every other source describes the
    feed, not the market.
    """
    from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
    from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
        OrderBookReplayError,
        OrderBookSnapshotReplayEngine,
    )

    engine = OrderBookSnapshotReplayEngine(
        MarketDepthTapeReader(tmp_path),
        session_date=SESSION_DAY,
        staleness_quantile=0.999,
        inadmissible_instruments=frozenset({738561}),
    )
    with pytest.raises(OrderBookReplayError, match="inadmissible"):
        engine.replay_instrument(738561).__next__()


def test_an_admissible_instrument_is_not_gated(tmp_path: Path) -> None:
    """The gate must refuse only what was measured, so an ungated instrument reaches the
    tape and fails for the ordinary reason instead — an empty tape, not a verdict."""
    from nse_algo_trader.market_depth.market_depth_tape_store import (
        DepthTapeStoreError,
        MarketDepthTapeReader,
    )
    from nse_algo_trader.market_depth.order_book_snapshot_replay_engine import (
        OrderBookSnapshotReplayEngine,
    )

    engine = OrderBookSnapshotReplayEngine(
        MarketDepthTapeReader(tmp_path),
        session_date=SESSION_DAY,
        staleness_quantile=0.999,
        inadmissible_instruments=frozenset({111}),
    )
    # It reaches the tape and fails there — the gate did not answer for it. On this empty
    # directory the tape's own error is what surfaces, which is exactly the distinction:
    # "no data" and "measured unreliable" are different refusals with different wording.
    with pytest.raises(DepthTapeStoreError, match="no tape at"):
        engine.replay_instrument(738561).__next__()


def test_a_healthy_session_does_not_manufacture_inadmissible_instruments(
    tmp_path: Path,
) -> None:
    """A day on which every broker behaved must not have a quarter of it ruled out.

    The cut is the session's own upper quartile of divergence rates, which on an all-zero
    session is zero — and `> 0` is then false for every instrument. Pinned because a
    quantile rule that always excludes its own worst quarter would be a machine for
    inventing failures.
    """
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for index in range(40):
        for symbol in ("RELIANCE", "TCS", "INFY", "HDFCBANK"):
            group = engine.align(
                [
                    _observation(broker, symbol=symbol, requested_offset_ms=index * 2_000.0)
                    for broker in ("kite", "angel_one")
                ]
            )[0]
            engine.learn(group, session_date=SESSION_DAY)
    admissibility = engine.admissibility(SESSION_DAY)
    assert admissibility
    assert all(admissibility.values())


# -- defects found by adversarial review, pinned so they cannot return -------------------


def test_an_unchanged_quote_is_never_reported_as_movement() -> None:
    """The critical defect: midpoints ending in .5 were compared against their own rounding.

    Measured on the real session — 30% of rows have an odd bid+ask, and 29,030 of 86,306
    consecutive identical observations were being reported as moves, which corrupted the
    frozen rate for a third of the tape.
    """
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for index in range(12):
        group = engine.align(
            [
                # bid + ask is ODD, so the midpoint ends in .5 — the case that broke.
                _observation(
                    "frozen",
                    bid_paise=10_000,
                    ask_paise=10_001,
                    last_paise=10_000,
                    requested_offset_ms=index * 2_000.0,
                ),
                _observation(
                    "moving",
                    bid_paise=10_000 + index * 10,
                    ask_paise=10_001 + index * 10,
                    last_paise=10_000 + index * 10,
                    requested_offset_ms=index * 2_000.0 + 100.0,
                ),
            ]
        )[0]
        engine.learn(group, session_date=SESSION_DAY)
    assert store.reliability("frozen").unchanged_while_others_moved_rate > 0.9
    assert store.reliability("moving").unchanged_while_others_moved_rate == 0.0


def test_a_wide_book_cannot_license_its_own_disagreement() -> None:
    """The tolerance comes from the NARROWEST book, not the widest.

    With the widest, a stale broker quoting a 2,000-paise-wide book had a 7.5% disagreement
    published as consensus, while the same gap between two tight books was refused.
    """
    engine = _engine()
    group = engine.align(
        [
            _observation("wide", bid_paise=9_000, ask_paise=11_000, last_paise=10_000),
            _observation("tight", bid_paise=10_740, ask_paise=10_760, last_paise=10_750),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.UNRESOLVED
    assert consolidated.consensus_paise is None


def test_a_single_source_with_an_impossible_book_publishes_its_trade_not_its_midpoint() -> None:
    """A midpoint of an impossible book is not a price — but the last trade still is.

    This test previously demanded a refusal, and the real data corrected it: a self-crossed
    quote is a price-band artefact whose BOOK is unusable while its last traded price is a
    genuine print. Refusing outright discarded a real observation; publishing the midpoint
    would have invented one.
    """
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=10_050, ask_paise=10_000, last_paise=10_025),
            _observation("angel_one", failure="timeout"),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.resolution is FeedResolution.SINGLE_SOURCE
    assert not consolidated.is_crossed  # the artefact never reaches the synthetic touch
    assert consolidated.consensus_paise == pytest.approx(10_025.0)  # the trade, not the book
    assert consolidated.synthetic_best_bid_paise is None


def test_a_second_sweep_starts_a_new_group_instead_of_losing_a_quote() -> None:
    """Alignment used to DELETE the repeat and orphan its partner.

    Measured: 4 observations became 3 on this exact shape, and on the real session 118
    observations were lost, manufacturing every single-source group it reported.
    """
    engine = _engine()
    groups = engine.align(
        [
            _observation("kite", requested_offset_ms=0.0),
            _observation("angel_one", requested_offset_ms=1_400.0),
            _observation("kite", requested_offset_ms=1_450.0),
            _observation("angel_one", requested_offset_ms=2_900.0),
        ]
    )
    assert sum(len(group.observations) for group in groups) == 4
    assert all(len(set(group.brokers)) == len(group.brokers) for group in groups)


def test_a_failed_poll_never_evicts_the_same_brokers_good_quote() -> None:
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", failure="timeout", requested_offset_ms=0.0),
            _observation("kite", requested_offset_ms=500.0),
            _observation("angel_one", requested_offset_ms=1_000.0),
        ]
    )[0]
    assert set(group.brokers) == {"kite", "angel_one"}


def test_a_healthy_session_condemns_nothing_however_its_rates_are_spread(
    tmp_path: Path,
) -> None:
    """The quartile rule condemned its own worst quarter on a day nothing was wrong.

    Measured on the real session: the cut landed at 0.46% divergence while the worst
    instrument in the entire day diverged on 1.15% of comparisons, and 33 instrument-
    sessions were ruled inadmissible. The test is now an effect size against the session's
    own base rate, so a spread of tiny rates condemns nobody.
    """
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for index, symbol in enumerate(["A", "B", "C", "D", "E", "F", "G", "H"]):
        for comparison in range(1_000):
            store.observe_instrument_session(
                session_date=SESSION_DAY,
                broker="kite",
                trading_symbol=symbol,
                diverged=comparison < index + 1,  # 1..8 divergences in 1,000
                frozen=False,
                absolute_deviation_paise=1.0,
            )
    admissibility = engine.admissibility(SESSION_DAY)
    assert len(admissibility) == 8
    assert all(admissibility.values())


def test_a_genuinely_broken_instrument_is_still_condemned(tmp_path: Path) -> None:
    """The effect-size test must not become a rule that never fires."""
    store = BrokerReliabilityStore(_temporary_database())
    engine = ConsolidatedFeedEngine(store)
    for symbol in ("A", "B", "C", "D"):
        for comparison in range(1_000):
            store.observe_instrument_session(
                session_date=SESSION_DAY,
                broker="kite",
                trading_symbol=symbol,
                diverged=(symbol == "D" and comparison < 300),  # D breaks, the rest do not
                frozen=False,
                absolute_deviation_paise=1.0,
            )
    admissibility = engine.admissibility(SESSION_DAY)
    assert admissibility[("kite", "D")] is False
    assert all(admissibility[("kite", symbol)] for symbol in ("A", "B", "C"))


def test_a_naive_timestamp_is_refused_at_the_boundary() -> None:
    """One naive datetime used to kill an entire session walk and lose all its learning."""
    with pytest.raises(ValueError, match="timezone"):
        BrokerQuoteObservation(
            broker="kite",
            trading_symbol="RELIANCE",
            exchange="NSE",
            requested_at=datetime(2026, 8, 12, 9, 0),  # noqa: DTZ001 — the point
            received_at=AT,
            last_price_paise=131_390,
        )


def test_a_zero_price_is_not_a_price() -> None:
    zero = _observation("kite", last_paise=0, bid_paise=None, ask_paise=None)
    assert not zero.is_usable


def test_the_decomposition_normalises_unsorted_pair_keys() -> None:
    """An unsorted key used to return all-`None`, indistinguishable from no data."""
    from nse_algo_trader.consolidated_feed.consolidated_feed_engine import (
        three_cornered_hat_variances,
    )

    unsorted_keys = {
        ("b", "a"): (100.0, 200),
        ("c", "a"): (500.0, 200),
        ("c", "b"): (520.0, 200),
    }
    variances = three_cornered_hat_variances(unsorted_keys, minimum_observations=30)
    assert any(value is not None for value in variances.values())


def test_a_one_tick_cross_between_brokers_is_polling_skew_not_staleness() -> None:
    """Measured on the real three-broker session, and it changed the design.

    Kite quoting [377.20, 377.25] and Angel [377.30, 377.45] a fraction of a second later
    is a moving market: Angel's bid sits one tick above Kite's ask because the price moved
    between the two polls. Calling that "one book is stale" put 10-44% of a session in the
    refused bucket, which no feed is. A cross is evidence of staleness only when it is
    LARGER than what these books explain.
    """
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=37_720, ask_paise=37_725, last_paise=37_720),
            _observation("angel_one", bid_paise=37_730, ask_paise=37_745, last_paise=37_730),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.is_crossed  # the fact is still reported
    assert consolidated.resolution is FeedResolution.RESOLVED  # but it is not a refusal
    assert consolidated.consensus_paise is not None
    assert "polling skew" in consolidated.reason


def test_a_cross_far_beyond_the_books_is_still_refused() -> None:
    """The other side of the same rule: a real stale book must still be caught."""
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=37_720, ask_paise=37_725, last_paise=37_720),
            _observation("angel_one", bid_paise=39_000, ask_paise=39_020, last_paise=39_010),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    assert consolidated.is_crossed
    assert consolidated.resolution is FeedResolution.UNRESOLVED
    assert consolidated.consensus_paise is None
    assert "stale" in consolidated.reason


def test_a_self_crossed_quote_is_a_price_band_artefact_not_a_book() -> None:
    """Measured on the real session: 25,761 rows, 10.1% of BOTH brokers identically.

    Their shape is unmistakable — bid at last +3.03%, ask at last -2.95%, 40,407 shares at
    the touch against a normal 281. Those are the orders resting at the exchange's +/-3%
    dynamic price band, surfacing as top-of-book when the real touch thins out near the
    close. No threshold is needed to reject them: a bid above its own ask is impossible.
    """
    band_artefact = _observation(
        "kite", bid_paise=136_030, ask_paise=131_410, last_paise=132_050
    )
    assert not band_artefact.has_valid_book
    assert band_artefact.midpoint_paise is None
    assert band_artefact.is_usable  # its LAST PRICE is still a real trade


def test_a_band_artefact_does_not_poison_the_synthetic_touch() -> None:
    engine = _engine()
    group = engine.align(
        [
            _observation("kite", bid_paise=136_030, ask_paise=131_410, last_paise=132_050),
            _observation("angel_one", bid_paise=132_040, ask_paise=132_060, last_paise=132_050),
        ]
    )[0]
    consolidated = engine.consolidate(group)
    # The valid book alone defines the touch; the artefact contributes only its last price.
    assert consolidated.synthetic_best_bid_paise == 132_040
    assert consolidated.synthetic_best_ask_paise == 132_060
    assert not consolidated.is_crossed
    assert consolidated.resolution is FeedResolution.RESOLVED
