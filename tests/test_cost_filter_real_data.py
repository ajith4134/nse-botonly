"""`R.05` for F01 — the cost filter against real bars at the strategy's OWN horizon.

`O.74` recorded why the first end-to-end run proved less than it appeared to: the engine was fed
depth-tape mids seconds apart, so the deviations it measured were tiny, so losing to a 13-bps
hurdle was arithmetic rather than evidence.

Running it properly found something `O.74` did not anticipate. The timescale was **not** the main
problem. The main problem was that the edge formula encoded an unstated exit rule — expected move
= the deviation's excess beyond its own entry band — which understated the measured reversion by
about seven-fold. These tests exercise the replacement: an edge fitted to 150,364 real reversion
events from the deep-history archive.

What they assert is deliberately NOT "the strategy is profitable". They assert that the pipeline
reaches a *defensible* verdict on real data: that the calibration exists, that it is causal, that
it refuses where it has no evidence, and that the structure it found — an edge which is not
monotone in deviation depth and which changes sign — actually survives re-measurement. A test that
demanded profitability would be a test that fails when the honest answer is "no".

Skipped rather than failed when the archive is absent, with a message naming what to run, so a
skip can never be mistaken for a pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import (
    MINIMUM_EVENTS_FOR_A_CALIBRATION,
    CalibrationCoverageError,
    CalibrationMaturity,
    ReversionCalibrationStore,
    ReversionEvent,
    calibrate_from_events,
    deviation_bucket_of,
    measure_reversion_events,
)
from nse_algo_trader.cost_gate.priced_signal import (
    EdgeConfidence,
    PricedSignalError,
    edge_from_calibrated_reversion,
)
from nse_algo_trader.deep_history.deep_history_archive_loader import (
    DEFAULT_DEEP_HISTORY_PATH,
    DeepHistoryArchiveLoader,
)
from nse_algo_trader.strategy.intraday_mean_reversion_engine import (
    DEVIATION_BAND_QUANTILE,
    MINIMUM_OBSERVATIONS_FOR_BANDS,
)

pytestmark = [
    pytest.mark.real_data,
    pytest.mark.skipif(
        not DEFAULT_DEEP_HISTORY_PATH.exists(),
        reason=(
            f"the deep-history archive is absent at {DEFAULT_DEEP_HISTORY_PATH}; build it with "
            f"scripts/run_daily_operations.py before claiming an R.05 pass"
        ),
    ),
]

_A_REAL_TRADING_DAY = date(2026, 6, 30)
_FIT_BOUNDARY = date(2026, 7, 1)
_ROLLING_WINDOW = 20
_MINIMUM_HISTORY = 400


@pytest.fixture(scope="module")
def archive() -> Iterator[DeepHistoryArchiveLoader]:
    with DeepHistoryArchiveLoader(database_path=DEFAULT_DEEP_HISTORY_PATH) as loader:
        yield loader


@pytest.fixture(scope="module")
def real_events(archive: DeepHistoryArchiveLoader) -> list[ReversionEvent]:
    """Reversion events measured off real bars, shared across the assertions.

    Measured once because the walk is the expensive part; 120 symbols is enough to populate the
    dense buckets and is small enough that a developer will actually run this file.
    """
    for offset in range(40):
        candidate = date.fromordinal(_A_REAL_TRADING_DAY.toordinal() - offset)
        symbols = archive.symbols_on(candidate)
        if len(symbols) > 500:
            break
    else:
        pytest.skip("no cash cross-section of more than 500 symbols found in the archive")

    events: list[ReversionEvent] = []
    examined = 0
    for symbol in symbols[:120]:
        series = [
            (day, float(close))
            for day, close in archive.close_series(symbol)
            if close > 0 and day < _FIT_BOUNDARY
        ]
        if len(series) < _MINIMUM_HISTORY:
            continue
        examined += 1
        events.extend(
            measure_reversion_events(
                [close for _, close in series[-1500:]],
                trading_symbol=symbol,
                session_dates=[day for day, _ in series[-1500:]],
                horizon_bars=(1, 5),
                rolling_window=_ROLLING_WINDOW,
                band_quantile=Decimal(str(DEVIATION_BAND_QUANTILE)),
                minimum_deviations_before_banding=MINIMUM_OBSERVATIONS_FOR_BANDS,
            )
        )
    if examined < 30:
        pytest.skip(f"only {examined} symbols had {_MINIMUM_HISTORY}+ bars of usable history")
    return events


@pytest.mark.real_data
def test_the_archive_yields_enough_real_events_to_estimate_a_heavy_tailed_mean(
    real_events: list[ReversionEvent],
) -> None:
    """The precondition everything else rests on, asserted rather than assumed.

    The capture distribution is tail-carried, so an under-powered sample would produce a mean
    dominated by whether one large reversion happened to land in it. If this fails, no other
    assertion in this file is evidence of anything.
    """
    assert len(real_events) > MINIMUM_EVENTS_FOR_A_CALIBRATION * 10
    assert {event.horizon_bars for event in real_events} == {1, 5}
    # Entries fire at the 90th percentile of a symbol's OWN deviations, so extremity is relative
    # and a quiet instrument can legitimately fire below one sigma — that is `R.03` working, and
    # asserting an absolute floor here would contradict the engine's design. What must hold is
    # that the rule selects for extremity in aggregate: the typical event is a long way out.
    depths = sorted(abs(event.deviation_sigma) for event in real_events)
    assert depths[len(depths) // 2] > Decimal(2), (
        f"median entry depth is {depths[len(depths) // 2]} sigma; the band has stopped selecting "
        f"for unusual moves and the calibration would be measuring ordinary bars"
    )


@pytest.mark.real_data
def test_the_measured_edge_is_not_monotone_in_deviation_depth(
    real_events: list[ReversionEvent],
) -> None:
    """The finding that killed the old formula, pinned so it cannot be quietly re-assumed.

    `edge_from_mean_reversion_decision` computed the expected move as proportional to how far
    price had travelled: deeper deviation, bigger claim, always. The archive does not agree.
    Capture at 3.5 sigma comes out NEGATIVE at short horizons — those bars continue rather than
    revert — while 4 sigma is positive again. Any formula linear in depth is therefore wrong in
    sign somewhere, which is a worse failure than being wrong in magnitude, and no amount of
    recalibrating a linear coefficient repairs it.
    """
    by_bucket: dict[Decimal, list[float]] = {}
    for event in real_events:
        if event.horizon_bars != 1:
            continue
        by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(
            float(event.captured_bps)
        )
    dense = {
        bucket: sum(values) / len(values)
        for bucket, values in by_bucket.items()
        if len(values) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    }
    assert len(dense) >= 3, f"only {len(dense)} dense buckets; cannot speak about shape"
    ordered = [dense[bucket] for bucket in sorted(dense)]
    assert ordered != sorted(ordered), (
        f"mean capture by deviation bucket came out monotone increasing at {dense}, which is "
        f"what the replaced formula assumed; if that is genuinely true on this universe then "
        f"the correction recorded in O.74 needs revisiting in turn"
    )


@pytest.mark.real_data
def test_a_calibration_refuses_to_be_fitted_on_evidence_it_should_not_have_seen(
    real_events: list[ReversionEvent],
) -> None:
    """Look-ahead is refused loudly, on real dates, not merely documented.

    A backtest that prices a session using a coefficient fitted through that same session
    flatters every downstream number and leaves no trace in the output. The guard is asserted
    here against real archive dates because a synthetic date can be made to pass a check that
    real, irregular trading calendars would trip.
    """
    single_horizon = [event for event in real_events if event.horizon_bars == 5]
    latest = max(event.observed_on for event in single_horizon)
    with pytest.raises(Exception, match="look-ahead"):
        calibrate_from_events(
            single_horizon,
            fitted_through=latest,
            maturity=CalibrationMaturity.POOLED_UNIVERSE,
        )
    # And one day later is enough to make it legitimate — the guard is an inequality on dates,
    # not a blanket refusal that would make any same-period fit impossible.
    calibrate_from_events(
        single_horizon,
        fitted_through=date.fromordinal(latest.toordinal() + 1),
        maturity=CalibrationMaturity.POOLED_UNIVERSE,
    )


@pytest.mark.real_data
def test_the_calibrated_edge_is_materially_larger_than_the_replaced_formula_claimed(
    real_events: list[ReversionEvent], tmp_path: Path
) -> None:
    """The correction itself, measured — the reason `O.74` had to be rewritten.

    The replaced formula reported a median claim of 4.81 bps on this universe. The measured mean
    capture is several times that. The assertion is on the DIRECTION and ORDER of the gap rather
    than on an exact figure, because the figure moves with the fit window and pinning it would
    make this a change-detector rather than a test of the claim.
    """
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    horizon = 5
    by_bucket: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == horizon:
            by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)

    written = [
        calibrate_from_events(
            events,
            fitted_through=_FIT_BOUNDARY,
            maturity=CalibrationMaturity.DEVIATION_BUCKET,
        )
        for events in by_bucket.values()
        if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    assert written, "no bucket reached the evidence threshold on real data"
    store.record(written)

    what_the_old_formula_claimed_bps = Decimal("4.81")
    best = max(written, key=lambda calibration: calibration.mean_captured_bps)
    assert best.mean_captured_bps > what_the_old_formula_claimed_bps * 2, (
        f"the best-evidenced bucket captures {best.mean_captured_bps} bps, which is not "
        f"materially above the {what_the_old_formula_claimed_bps} bps the excess-over-band "
        f"formula claimed — the correction in O.74 would then be unwarranted"
    )
    # And it round-trips through the store at the same value, because a calibration that changes
    # on persistence would silently price live trades differently from the fit that was reviewed.
    reloaded = store.capture_for(
        deviation_sigma=best.deviation_bucket,
        horizon_bars=horizon,
        as_of=date(2026, 8, 12),
    )
    assert reloaded.mean_captured_bps == best.mean_captured_bps
    assert reloaded.event_count == best.event_count


@pytest.mark.real_data
def test_the_conservative_claim_refuses_where_the_evidence_cannot_rule_out_a_loss(
    real_events: list[ReversionEvent], tmp_path: Path
) -> None:
    """`EdgeConfidence.CONSERVATIVE` must actually bite on real data, not just in principle.

    Most measured cells have a lower confidence bound below zero: the evidence is consistent with
    the strategy losing money there. A gate asked for a conservative claim must refuse those
    outright rather than return a smaller positive number, because a claim of "small but positive"
    is a materially different statement from "cannot rule out negative", and only the second is
    true. This is `UNPRICEABLE` versus `VETO` again, one layer up.
    """
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    by_cell: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == 5:
            by_cell.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)
    calibrations = [
        calibrate_from_events(
            events, fitted_through=_FIT_BOUNDARY, maturity=CalibrationMaturity.DEVIATION_BUCKET
        )
        for events in by_cell.values()
        if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    store.record(calibrations)

    refused = 0
    priced = 0
    for calibration in calibrations:
        deviation = float(calibration.deviation_bucket)
        try:
            edge = edge_from_calibrated_reversion(
                calibration,
                deviation=deviation,
                conviction=1.0,
                price_uncertainty=EdgeConfidence.CONSERVATIVE,
            )
        except PricedSignalError:
            refused += 1
            continue
        priced += 1
        assert edge > 0
        # A conservative claim can never exceed the expected one; if it did, the pessimistic end
        # of the interval would be the optimistic end and every gate downstream would be reading
        # the interval backwards.
        assert edge <= edge_from_calibrated_reversion(
            calibration,
            deviation=deviation,
            conviction=1.0,
            price_uncertainty=EdgeConfidence.EXPECTED,
        )
    assert refused + priced == len(calibrations)
    assert refused > 0, (
        "no real calibration was refused under a conservative claim, which would mean every "
        "measured cell excludes zero at two standard errors — implausible on this evidence, and "
        "a sign the lower bound is not being applied"
    )


@pytest.mark.real_data
def test_an_uncalibrated_deviation_depth_is_refused_rather_than_extrapolated(
    tmp_path: Path,
) -> None:
    """The refusal contract, on the case that matters: a deviation nobody has measured.

    Extrapolating the nearest bucket would be indefensible precisely because the measured shape
    is non-monotone and sign-changing — the neighbouring bucket is not evidence about this one.
    """
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    with pytest.raises(CalibrationCoverageError, match="no reversion calibration covers"):
        store.capture_for(
            deviation_sigma=Decimal("2.5"), horizon_bars=5, as_of=date(2026, 8, 12)
        )


@pytest.mark.real_data
def test_a_calibration_from_the_future_is_never_used_to_price_the_past(
    real_events: list[ReversionEvent], tmp_path: Path
) -> None:
    """Point-in-time resolution, asserted on the store rather than on the fitter.

    The fitter can be careful and the store still leak, because a replay asks the store directly.
    """
    store = ReversionCalibrationStore(tmp_path / "calibration.sqlite3")
    by_bucket: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == 5:
            by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)
    dense = [
        events for events in by_bucket.values() if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    assert dense, "no dense bucket on real data"
    calibration = calibrate_from_events(
        dense[0], fitted_through=_FIT_BOUNDARY, maturity=CalibrationMaturity.DEVIATION_BUCKET
    )
    store.record([calibration])

    assert store.capture_for(
        deviation_sigma=calibration.deviation_bucket,
        horizon_bars=5,
        as_of=date(2026, 8, 12),
    ).mean_captured_bps == calibration.mean_captured_bps

    with pytest.raises(CalibrationCoverageError):
        store.capture_for(
            deviation_sigma=calibration.deviation_bucket,
            horizon_bars=5,
            as_of=date(2026, 6, 1),
        )


@pytest.mark.real_data
def test_the_skew_of_the_real_distribution_is_published_not_smoothed_away(
    real_events: list[ReversionEvent],
) -> None:
    """A mean carried by a thin tail is a different proposition, and must read as one.

    On real bars the shallow buckets reach a mean nearly twenty times their median: the typical
    trade earns almost nothing and a handful carry the result. A calibration that reported only
    the mean would let a strategy needing hundreds of trades to realise its edge look identical
    to one that earns it reliably, which is a risk statement, not a rounding one.
    """
    by_bucket: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == 1:
            by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)
    calibrations = [
        calibrate_from_events(
            events, fitted_through=_FIT_BOUNDARY, maturity=CalibrationMaturity.DEVIATION_BUCKET
        )
        for events in by_bucket.values()
        if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    ratios = [
        calibration.skew_ratio
        for calibration in calibrations
        if calibration.skew_ratio is not None
    ]
    assert ratios, "no skew ratio could be formed on real data"
    assert max(ratios) > Decimal(2), (
        f"the largest mean-to-median ratio on real bars is {max(ratios)}; if the real capture "
        f"distribution is genuinely this symmetric, the tail-risk warning in the calibrator's "
        f"docstring is overstated and should be corrected"
    )


@pytest.mark.real_data
def test_conviction_scales_the_calibrated_claim_and_never_inflates_it(
    real_events: list[ReversionEvent],
) -> None:
    """Half-convinced must claim less, and full conviction must not claim more than measured."""
    by_bucket: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == 5:
            by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)
    candidates = [
        calibrate_from_events(
            events, fitted_through=_FIT_BOUNDARY, maturity=CalibrationMaturity.DEVIATION_BUCKET
        )
        for events in by_bucket.values()
        if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    positive = [c for c in candidates if c.mean_captured_bps > 0]
    assert positive, "no positive-edge bucket on real data to scale"
    calibration = positive[0]
    deviation = float(calibration.deviation_bucket)
    full = edge_from_calibrated_reversion(calibration, deviation=deviation, conviction=1.0)
    half = edge_from_calibrated_reversion(calibration, deviation=deviation, conviction=0.5)
    assert half < full
    assert full == calibration.mean_captured_bps


@pytest.mark.real_data
def test_the_priced_edge_is_compared_against_the_floor_in_the_same_unit(
    real_events: list[ReversionEvent],
) -> None:
    """The point of F01: a real claim, in basis points, meeting a real hurdle in basis points.

    The measured floors from the real universe are `NSE-MIS` 8.9 bps and `NSE-CNC` 26.2 bps. This
    asserts the comparison is *possible and meaningful* — that the calibrated claim lands in the
    same order of magnitude as the hurdle, so the gate is making a real decision rather than
    rubber-stamping or rejecting everything by construction. A pipeline where every claim is a
    thousand times the hurdle, or a thousandth of it, has a unit error, not an edge.
    """
    intraday_floor_bps = Decimal("8.9")
    by_bucket: dict[Decimal, list[ReversionEvent]] = {}
    for event in real_events:
        if event.horizon_bars == 5:
            by_bucket.setdefault(deviation_bucket_of(event.deviation_sigma), []).append(event)
    calibrations = [
        calibrate_from_events(
            events, fitted_through=_FIT_BOUNDARY, maturity=CalibrationMaturity.DEVIATION_BUCKET
        )
        for events in by_bucket.values()
        if len(events) >= MINIMUM_EVENTS_FOR_A_CALIBRATION
    ]
    means = [calibration.mean_captured_bps for calibration in calibrations]
    assert means
    assert max(means) > intraday_floor_bps, (
        f"no measured bucket clears the {intraday_floor_bps} bps intraday floor (best "
        f"{max(means)}); the strategy would then have no cell worth trading on this universe, "
        f"which is a publishable finding rather than a test failure"
    )
    assert max(means) < intraday_floor_bps * 1000, (
        f"best measured capture is {max(means)} bps against a {intraday_floor_bps} bps floor; a "
        f"gap of that order means a unit error somewhere, not an edge"
    )
