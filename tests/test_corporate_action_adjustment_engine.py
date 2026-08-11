"""Tests for the corporate-action adjustment engine — written before the implementation.

A price series that ignores a 1:10 split shows a 90% overnight crash that never
happened. `TATASTEEL` closed 959.40 on 2022-07-27 and 100.35 on 2022-07-28; every
volatility, momentum and stop calculation over that pair is wrong unadjusted.

The tests are anchored on the two events verified by hand before any code existed
(`docs/research/205` §3.1) and on the taxonomy measured across the whole acquired
feed: 41,885 real actions, 2,321 distinct subject shapes, 1,573 of them appearing
exactly once. That tail is the hazard, so the tests care as much about what the
parser REFUSES to guess as about what it computes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nse_algo_trader.corporate_action_adjustment_engine import (
    ActionClass,
    AdjustmentKind,
    CorporateAction,
    CorporateActionAdjustmentEngine,
    CorporateActionError,
)

FEED = Path("/home/opc/nse_archive/manifest/corporate_actions.sqlite3")
TATASTEEL_EX = date(2022, 7, 28)
IOC_EX = date(2022, 6, 30)
# Measured from the real archive, not assumed.
TATASTEEL_CLOSE_BEFORE = Decimal("959.40")
TATASTEEL_CLOSE_AFTER = Decimal("100.35")
IOC_CLOSE_BEFORE = Decimal("109.80")
IOC_CLOSE_AFTER = Decimal("74.25")
REAL_ACTION_COUNT = 41885


def _action(subject: str, *, symbol: str = "ACME", ex: date | None = None) -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        ex_date=ex or date(2022, 7, 28),
        subject=subject,
        isin=None,
        face_value=None,
        series="EQ",
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[CorporateActionAdjustmentEngine]:
    with CorporateActionAdjustmentEngine(tmp_path / "actions.sqlite3") as opened:
        yield opened


# --------------------------------------------------------------------------- parsing


@pytest.mark.unit
def test_the_real_tatasteel_split_is_parsed(engine: CorporateActionAdjustmentEngine) -> None:
    """The exact subject string the live feed returned, not a tidied version."""
    parsed = engine.parse(
        _action("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
    )
    assert parsed.classification is ActionClass.ADJUSTING_QUANTIFIED
    assert parsed.kind is AdjustmentKind.RATIO_SPLIT
    assert parsed.price_factor == Decimal("1") / Decimal("10")


@pytest.mark.unit
def test_the_real_ioc_bonus_is_parsed(engine: CorporateActionAdjustmentEngine) -> None:
    """`Bonus 1:2` — one new share per two held, so the holder ends with 3 for 2."""
    parsed = engine.parse(_action("Bonus 1:2"))
    assert parsed.classification is ActionClass.ADJUSTING_QUANTIFIED
    assert parsed.kind is AdjustmentKind.RATIO_BONUS
    assert parsed.price_factor == Decimal("2") / Decimal("3")
    assert parsed.quantity_factor == Decimal("3") / Decimal("2")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Fv Split Rs.10/- To Re.1/", Decimal("1") / Decimal("10")),
        ("Face Value Split From Rs.10/- To Rs.2/-", Decimal("2") / Decimal("10")),
        ("Face Value Split From Rs 10 To Re 1", Decimal("1") / Decimal("10")),
        ("Fv Split Rs.100/- To Rs.10/", Decimal("10") / Decimal("100")),
    ],
)
def test_the_abbreviated_split_shapes_are_parsed(
    engine: CorporateActionAdjustmentEngine, subject: str, expected: Decimal
) -> None:
    """Four distinct real shapes for the same event, all present in the feed."""
    parsed = engine.parse(_action(subject))
    assert parsed.price_factor == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("subject", "price_factor"),
    [
        ("Bonus 1:1", Decimal("1") / Decimal("2")),
        ("Bonus 1 : 1", Decimal("1") / Decimal("2")),
        ("Bonus 3:5", Decimal("5") / Decimal("8")),
        ("Bonus 1:1250", Decimal("1250") / Decimal("1251")),
    ],
)
def test_bonus_shapes_including_the_extreme_real_one(
    engine: CorporateActionAdjustmentEngine, subject: str, price_factor: Decimal
) -> None:
    """`Bonus 1:1250` is a real GODREJIND action, not a contrived edge case."""
    assert engine.parse(_action(subject)).price_factor == price_factor


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    ["Annual General Meeting", "Agm", "Interest Payment", "Dividend - Rs 5 Per Share",
     "Interim Dividend - Re 1 Per Share", "Annual General Meeting/Dividend - Rs 2 Per Share",
     "Buy Back", "Buyback"],
)
def test_inert_actions_do_not_adjust(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """38,514 of 41,885 real actions are inert; a false adjustment here corrupts everything."""
    parsed = engine.parse(_action(subject))
    assert parsed.classification is ActionClass.NON_ADJUSTING
    assert parsed.price_factor is None


@pytest.mark.adversarial
@pytest.mark.parametrize("subject", ["Demerger", "Scheme Of Arrangement", "De-Merger"])
def test_a_demerger_is_flagged_as_unquantified_never_as_inert(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """The dangerous class: 189 real actions that move price with NO ratio in the text.

    Folding these into "no adjustment" would leave a real discontinuity in the
    series while the engine reported success.
    """
    parsed = engine.parse(_action(subject))
    assert parsed.classification is ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED
    assert parsed.price_factor is None


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "subject",
    ["Bonus", "Bonus 0:0", "Face Value Split From Rs 0/- To Re 1/-", "Bonus 1:0",
     "Some Entirely Novel Corporate Event Nobody Anticipated"],
)
def test_an_unparseable_subject_is_reported_not_assumed_inert(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """1,573 subject shapes appear exactly once. A dropped split is invisible."""
    parsed = engine.parse(_action(subject))
    assert parsed.classification is ActionClass.UNPARSED
    assert parsed.price_factor is None
    assert subject in parsed.evidence_summary()


@pytest.mark.adversarial
def test_a_split_that_increases_face_value_is_refused(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """A 'split' from Rs 1 to Rs 10 is a consolidation written wrong, or a typo.

    Either way the engine must not silently apply a 10x expansion to history.
    """
    parsed = engine.parse(_action("Face Value Split From Rs 1/- To Rs 10/-"))
    assert parsed.classification is ActionClass.UNPARSED


# --------------------------------------------------------------------------- factors


@pytest.mark.unit
def test_factors_compose_exactly_with_no_float_drift(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """A 1:10 split then a 1:2 bonus must compose to exactly 1/15.

    The price is quoted BEFORE both, so both restate it: 1/10 x 2/3 = 1/15. A
    price quoted after both is already current and takes a factor of 1 — asserted
    below so the direction cannot silently reverse.
    """
    engine.ingest([
        _action("Face Value Split From Rs 10/- To Re 1/-", ex=date(2020, 1, 10)),
        _action("Bonus 1:2", ex=date(2021, 1, 10)),
    ])
    assert engine.cumulative_price_factor("ACME", as_of=date(2019, 1, 1)) == (
        Decimal("1") / Decimal("15")
    )
    assert engine.cumulative_price_factor("ACME", as_of=date(2022, 1, 1)) == Decimal(1)
    # Between the two: only the bonus still applies.
    assert engine.cumulative_price_factor("ACME", as_of=date(2020, 6, 1)) == (
        Decimal("2") / Decimal("3")
    )


@pytest.mark.unit
def test_only_actions_after_the_price_date_adjust_it(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """Adjustment runs BACKWARDS from the ex-date — today's price is the truth.

    A price already quoted post-split must not be divided again.
    """
    engine.ingest([_action("Face Value Split From Rs 10/- To Re 1/-", ex=date(2020, 1, 10))])
    assert engine.cumulative_price_factor("ACME", as_of=date(2019, 1, 1)) == Decimal("0.1")
    assert engine.cumulative_price_factor("ACME", as_of=date(2021, 1, 1)) == Decimal("1")


@pytest.mark.unit
def test_a_price_on_the_ex_date_itself_is_already_adjusted(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """The ex-date's own close is post-adjustment; the boundary is exclusive."""
    engine.ingest([_action("Bonus 1:1", ex=date(2020, 1, 10))])
    assert engine.cumulative_price_factor("ACME", as_of=date(2020, 1, 10)) == Decimal("1")
    assert engine.cumulative_price_factor("ACME", as_of=date(2020, 1, 9)) == Decimal("0.5")


@pytest.mark.adversarial
def test_a_series_spanning_an_unquantified_action_is_marked_untrustworthy(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """A demerger in the window means the series cannot be trusted, and says so."""
    engine.ingest([_action("Demerger", ex=date(2020, 6, 1))])
    verdict = engine.series_trust("ACME", start=date(2020, 1, 1), end=date(2021, 1, 1))
    assert not verdict.is_trustworthy
    assert verdict.unquantified_actions


@pytest.mark.unit
def test_a_clean_series_is_trustworthy(engine: CorporateActionAdjustmentEngine) -> None:
    engine.ingest([_action("Annual General Meeting", ex=date(2020, 6, 1))])
    assert engine.series_trust("ACME", start=date(2020, 1, 1), end=date(2021, 1, 1)).is_trustworthy


@pytest.mark.adversarial
def test_an_unparsed_action_also_taints_the_series(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """An unparsed subject is an open question, not a non-event."""
    engine.ingest([_action("Something Nobody Has Seen Before", ex=date(2020, 6, 1))])
    assert not engine.series_trust(
        "ACME", start=date(2020, 1, 1), end=date(2021, 1, 1)
    ).is_trustworthy


# --------------------------------------------------------------------------- strikes


@pytest.mark.unit
def test_a_ratio_action_divides_the_strike(engine: CorporateActionAdjustmentEngine) -> None:
    """`TRENT`'s real factor of 3: 7000 -> 2333.35 after tick rounding."""
    adjusted = engine.adjust_strike(Decimal("7000"), price_factor=Decimal("1") / Decimal("3"))
    assert adjusted == Decimal("2333.35")


@pytest.mark.unit
def test_an_additive_dividend_adjustment_subtracts_from_the_strike(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """The real TCS circular: strike 3340 reduced by Rs 75.00 to 3265."""
    assert engine.adjust_strike(Decimal("3340"), dividend_rupees=Decimal("75")) == Decimal("3265")


@pytest.mark.adversarial
def test_a_strike_adjustment_needs_exactly_one_of_the_two_modes(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """Additive and multiplicative are different events (`O.28`); passing both is a bug."""
    with pytest.raises(CorporateActionError):
        engine.adjust_strike(Decimal("100"))
    with pytest.raises(CorporateActionError):
        engine.adjust_strike(
            Decimal("100"), price_factor=Decimal("0.5"), dividend_rupees=Decimal("5")
        )


@pytest.mark.adversarial
def test_a_dividend_larger_than_the_strike_is_refused(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    with pytest.raises(CorporateActionError, match="below zero"):
        engine.adjust_strike(Decimal("50"), dividend_rupees=Decimal("75"))


# --------------------------------------------------------------------------- real data


@pytest.mark.real_data
@pytest.mark.skipif(not FEED.exists(), reason="acquired corporate-action feed absent")
def test_every_real_action_receives_a_class(tmp_path: Path) -> None:
    """R.05 across the whole acquired feed. Nothing may fall through unreported."""
    engine = _engine_from_feed(tmp_path)
    coverage = engine.coverage_report()
    assert sum(coverage.values()) == REAL_ACTION_COUNT
    assert coverage[ActionClass.ADJUSTING_QUANTIFIED] > 0
    assert coverage[ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED] > 0
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not FEED.exists(), reason="acquired corporate-action feed absent")
def test_the_real_tatasteel_split_restates_history_sanely(tmp_path: Path) -> None:
    """The headline test: a 90% crash that never happened must disappear."""
    engine = _engine_from_feed(tmp_path)
    factor = engine.cumulative_price_factor("TATASTEEL", as_of=date(2022, 7, 27))
    restated = TATASTEEL_CLOSE_BEFORE * factor
    move = (TATASTEEL_CLOSE_AFTER - restated) / restated
    assert abs(move) < Decimal("0.10"), f"restated move {move} is not a normal day"
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not FEED.exists(), reason="acquired corporate-action feed absent")
def test_the_real_ioc_bonus_restates_history_sanely(tmp_path: Path) -> None:
    engine = _engine_from_feed(tmp_path)
    factor = engine.cumulative_price_factor("IOC", as_of=date(2022, 6, 29))
    restated = IOC_CLOSE_BEFORE * factor
    move = (IOC_CLOSE_AFTER - restated) / restated
    assert abs(move) < Decimal("0.10"), f"restated move {move} is not a normal day"
    engine.close()


@pytest.mark.real_data
@pytest.mark.skipif(not FEED.exists(), reason="acquired corporate-action feed absent")
def test_the_unparsed_share_of_the_real_feed_is_small_and_measured(tmp_path: Path) -> None:
    """Not a pass/fail on beauty — a measurement that must be stated, not hidden.

    The parser is allowed to fail; it is not allowed to fail silently or to fail
    on the shapes that carry actual ratios.
    """
    engine = _engine_from_feed(tmp_path)
    coverage = engine.coverage_report()
    unparsed_share = Decimal(coverage[ActionClass.UNPARSED]) / Decimal(REAL_ACTION_COUNT)
    assert unparsed_share < Decimal("0.05"), f"unparsed share {unparsed_share:.4f}"
    engine.close()


def _engine_from_feed(tmp_path: Path) -> CorporateActionAdjustmentEngine:
    connection = sqlite3.connect(f"file:{FEED}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT symbol, ex_date, subject, isin, face_value, series FROM corporate_action"
    ).fetchall()
    connection.close()
    engine = CorporateActionAdjustmentEngine(tmp_path / "real_actions.sqlite3")
    engine.ingest_raw(rows)
    return engine


# ------------------------------------------------------- parser tail, found by measurement
# The shapes below were all found sitting in the UNPARSED bucket after the first
# implementation ran against the real 41,885-action feed. Each is a real action.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Fv Splt Frm Rs 10 To Rs 2", Decimal("2") / Decimal("10")),
        ("Fv Splt Frm Rs 10 To Re 1", Decimal("1") / Decimal("10")),
        ("Face Valus Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
         Decimal("2") / Decimal("10")),
        ("Split-Rs.10tors.2/Div-60%Purpose Revised", Decimal("2") / Decimal("10")),
    ],
)
def test_typo_and_abbreviation_shapes_from_the_real_feed_are_parsed(
    engine: CorporateActionAdjustmentEngine, subject: str, expected: Decimal
) -> None:
    """`Splt`, `Frm`, `Valus`, and no spaces at all — 25 real actions between them.

    A tidy regex drops these silently, and a dropped split is indistinguishable
    from no split.
    """
    assert engine.parse(_action(subject)).price_factor == expected


@pytest.mark.unit
def test_a_consolidation_raises_historical_prices(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """A reverse split is a real action the first implementation refused outright."""
    parsed = engine.parse(
        _action("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share")
    )
    assert parsed.classification is ActionClass.ADJUSTING_QUANTIFIED
    assert parsed.kind is AdjustmentKind.RATIO_CONSOLIDATION
    assert parsed.price_factor == Decimal("10")


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "subject",
    ["Face Value Split From Rs 1/- To Rs 10/-",
     "Consolidation Of Equity Shares From Rs 10 Per Share To Re 1 Per Share"],
)
def test_a_subject_whose_words_and_numbers_disagree_is_refused(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """A 'split' whose face value rises, or a 'consolidation' whose face value falls.

    One of the two is wrong and nothing decides which, so it is reported rather
    than resolved.
    """
    assert engine.parse(_action(subject)).classification is ActionClass.UNPARSED


@pytest.mark.adversarial
def test_a_compound_split_and_bonus_composes_both(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """Ten real actions carry both in one line: `Fv Spl-Rs10tors2/Bon-1:1`.

    Returning on the first match would apply half the adjustment and look
    entirely successful — the worst available outcome.
    """
    parsed = engine.parse(_action("Fv Spl-Rs10tors2/Bon-1:1"))
    assert parsed.classification is ActionClass.ADJUSTING_QUANTIFIED
    assert parsed.kind is AdjustmentKind.COMPOUND
    assert parsed.price_factor == (Decimal("2") / Decimal("10")) * (Decimal("1") / Decimal("2"))


@pytest.mark.unit
def test_a_bonus_hidden_inside_a_dividend_string_is_still_found(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """`Intdiv-Rs 3persh/Bonus1:1` — the inert keyword must not win the race."""
    parsed = engine.parse(_action("Intdiv-Rs 3persh/Bonus1:1"))
    assert parsed.classification is ActionClass.ADJUSTING_QUANTIFIED
    assert parsed.price_factor == Decimal("1") / Decimal("2")


# --------------------------------------------------- adversarial-review corrections
# 8 defects and 6 surviving mutants. Each test names the failure it pins.


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "subject",
    ["Bonus Preference Shares 21:1", "Bonus Ncrps 1:116", "Bonus Ncrps 4:1",
     "Bonus Debentures 6:1", "Bonus 1 Dvr : 10 Eq Share", "Bonus Warrants 1:1"],
)
def test_a_bonus_of_a_non_equity_instrument_never_adjusts_the_equity_price(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """The critical false positive, found on real blue chips.

    A bonus of debentures, preference shares, NCRPS, DVR shares or warrants is a
    distribution of a DIFFERENT instrument — the equity is not diluted and the
    exchange does not divide its price. The first implementation gave `DRREDDY` a
    factor of 1/7 on a day it moved 2.8%, and `ZEEL` 1/22 on a flat day: the
    engine injecting precisely the fake crash it exists to prevent.
    """
    assert engine.parse(_action(subject)).price_factor is None


@pytest.mark.adversarial
def test_an_unquantifiable_leg_poisons_the_whole_subject(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """`MONNETISPA` 2018: "Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10".

    The split matcher finds the consolidation leg and returns 3.03 — a factor
    computed from half a compound sentence, silently discarding the capital
    reduction. When part of a subject cannot be quantified, nothing in it can.
    """
    parsed = engine.parse(
        _action("Capital Reduction Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10")
    )
    assert parsed.classification is ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED
    assert parsed.price_factor is None


@pytest.mark.adversarial
def test_the_unquantified_check_outranks_the_inert_check(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """Six real subjects carry BOTH an unquantified and an inert marker.

    Mutation showed the ordering was load-bearing and completely unpinned: swap it
    and five real demergers flip to "no adjustment" — the failure the spec names
    as catastrophic, with nothing in the suite to catch it.
    """
    parsed = engine.parse(
        _action("Annual General Meeting/Dividend Rs.3/- Per Share/Scheme Of Arrangement")
    )
    assert parsed.classification is ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED


@pytest.mark.adversarial
def test_a_late_announced_action_does_not_leak_into_an_earlier_read(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """`known_as_of` was written by ingest and consulted by no read path at all.

    A corporate action corrected or announced late must not appear in a backtest
    dated before anyone could have known it.
    """
    engine.ingest(
        [_action("Bonus 1:1", ex=date(2020, 1, 10))], known_as_of=date(2020, 3, 1)
    )
    as_of = date(2020, 1, 5)
    assert engine.cumulative_price_factor(
        "ACME", as_of=as_of, known_as_of=date(2020, 1, 5)
    ) == Decimal(1)
    assert engine.cumulative_price_factor(
        "ACME", as_of=as_of, known_as_of=date(2020, 6, 1)
    ) == Decimal("0.5")


@pytest.mark.unit
def test_the_factor_timeline_is_cached_and_invalidated_on_ingest(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """Measured at 0.67 ms/call uncached — ~28 minutes across a full-universe backtest."""
    engine.ingest([_action("Bonus 1:1", ex=date(2020, 1, 10))])
    first = engine._factor_timeline("ACME", None)
    assert engine._factor_timeline("ACME", None) is first
    engine.ingest([_action("Bonus 1:1", ex=date(2021, 1, 10))])
    refreshed = engine._factor_timeline("ACME", None)
    assert refreshed is not first
    assert len(refreshed) == 2


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "subject",
    ["Face Value Split From Rs 0/- To Re 1/-", "Face Value Split From Rs 10/- To Rs 10/-"],
)
def test_degenerate_face_value_changes_are_refused(
    engine: CorporateActionAdjustmentEngine, subject: str
) -> None:
    """Zero would divide by zero; unchanged would mark a real oddity as handled.

    Both boundaries survived mutation — nothing in the suite reached them.
    """
    assert engine.parse(_action(subject)).classification is ActionClass.UNPARSED


@pytest.mark.adversarial
def test_a_strike_cannot_be_adjusted_to_exactly_zero(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """`adjusted <= 0` versus `< 0` survived mutation; a zero strike is not tradeable."""
    with pytest.raises(CorporateActionError, match="below zero"):
        engine.adjust_strike(Decimal("75"), dividend_rupees=Decimal("75"))


@pytest.mark.adversarial
def test_a_zero_price_factor_is_refused(engine: CorporateActionAdjustmentEngine) -> None:
    with pytest.raises(CorporateActionError, match="positive"):
        engine.adjust_strike(Decimal("100"), price_factor=Decimal("0"))


@pytest.mark.real_data
@pytest.mark.skipif(not FEED.exists(), reason="acquired corporate-action feed absent")
def test_no_real_non_equity_bonus_is_quantified(tmp_path: Path) -> None:
    """R.05 — the 11 real actions that exposed the defect, checked as a population."""
    engine = _engine_from_feed(tmp_path)
    connection = sqlite3.connect(f"file:{FEED}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT symbol, ex_date, subject FROM corporate_action WHERE subject LIKE '%onus%'"
        " AND (subject LIKE '%ebenture%' OR subject LIKE '%reference%'"
        " OR subject LIKE '%NCRPS%' OR subject LIKE '%DVR%' OR subject LIKE '%arrant%')"
    ).fetchall()
    connection.close()
    assert rows, "the fixture population disappeared from the feed"
    for symbol, ex_text, subject in rows:
        from nse_algo_trader.corporate_action_adjustment_engine import _feed_date

        parsed_date = _feed_date(ex_text)
        assert parsed_date is not None
        parsed = engine.parse(
            CorporateAction(symbol, parsed_date, subject, None, None, "EQ")
        )
        assert parsed.price_factor is None, f"{symbol} {subject} was quantified"
    engine.close()


@pytest.mark.adversarial
def test_a_zero_face_value_does_not_reach_the_division(
    engine: CorporateActionAdjustmentEngine,
) -> None:
    """The zero guard survived mutation because my first zero test never reached it.

    `"Face Value Split From Rs 0 To Re 1"` is rejected by the words-versus-numbers
    check (a split whose face value rises), so the zero guard was never exercised.
    A *consolidation* from Rs 0 passes that check and divides by zero.
    """
    parsed = engine.parse(
        _action("Consolidation Of Equity Shares From Rs 0 Per Share To Rs 10 Per Share")
    )
    assert parsed.classification is ActionClass.UNPARSED
