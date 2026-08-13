"""Tests for the paper trading book's virtual capital ledger — written before the implementation.

`docs/research/226` is the spec. The three kinds `R.23` requires are all here, and the adversarial
block is the one that matters: every invariant in §5 has a test that tries to break it, because a
paper book that silently double-spends produces evidence that graduation (`R.22`) will read as real.

`R.05` does not apply to the ledger's arithmetic — its inputs are simulated by construction. Its
real-data obligation is the dashboard round trip on the live server plus first consumption by
`F04`'s loop, and both are tracked as open blockers in `BACKLOG.md`.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.paper_capital_ledger import (
    InsufficientPaperCapitalError,
    PaperCapitalCheckpointDivergedError,
    PaperCapitalError,
    PaperCapitalEventKind,
    PaperCapitalLedger,
    PaperCapitalSnapshot,
    UnmatchedPaperCapitalReleaseError,
    UnreasonedPaperCapitalEventError,
)

IST = ZoneInfo("Asia/Kolkata")
TEN_LAKH = Decimal("1000000")
ONE_CRORE = Decimal("10000000")
CEILING = TradingCapital.of_rupees(TEN_LAKH)


def _at(minute: int) -> datetime:
    """A deterministic clock — an ordered ledger must never depend on wall time."""
    return datetime(2026, 8, 13, 9, 15, tzinfo=IST) + timedelta(minutes=minute)


@pytest.fixture(name="ledger_path")
def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "paper_capital_ledger.sqlite3"


def _seeded(path: Path) -> PaperCapitalLedger:
    ledger = PaperCapitalLedger(path)
    ledger.seed_from_ceiling(CEILING, occurred_at=_at(0), reason="test seed")
    return ledger


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_a_new_ledger_seeds_from_the_operator_ceiling_and_not_from_a_literal(
    ledger_path: Path,
) -> None:
    """§4 — the seed amount is READ from `capital_configuration`, never written into this module."""
    with _seeded(ledger_path) as ledger:
        snapshot = ledger.snapshot(measured_at=_at(1), live_ceiling=CEILING)
    assert snapshot.balance_rupees == TEN_LAKH
    assert snapshot.committed_rupees == Decimal(0)
    assert snapshot.free_rupees == TEN_LAKH
    assert snapshot.is_tradeable is True
    assert snapshot.binding_side == "paper_free_capital"


@pytest.mark.unit
def test_seeding_twice_is_refused_because_a_ledger_has_one_origin(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger, pytest.raises(PaperCapitalError):
        ledger.seed_from_ceiling(CEILING, occurred_at=_at(1), reason="second seed")


@pytest.mark.unit
def test_the_operator_edit_the_dashboard_performs_sets_an_absolute_figure(
    ledger_path: Path,
) -> None:
    """The operator's actual ask: the virtual currency is editable to whatever they wish."""
    with _seeded(ledger_path) as ledger:
        event = ledger.set_balance(
            Decimal("2500000"), occurred_at=_at(2), reason="operator wants a 25L regime"
        )
        snapshot = ledger.snapshot(measured_at=_at(3), live_ceiling=CEILING)
    assert event.kind is PaperCapitalEventKind.OPERATOR_SET
    assert event.previous_balance_rupees == TEN_LAKH
    assert snapshot.balance_rupees == Decimal("2500000")


@pytest.mark.unit
def test_an_edit_above_the_live_ceiling_is_allowed_but_stamped(ledger_path: Path) -> None:
    """§3.2 — not blocked, because R.03 requires exercising up to a crore. Stamped, because a
    record made on money the operator cannot fund is not achievable evidence."""
    with _seeded(ledger_path) as ledger:
        ledger.set_balance(ONE_CRORE, occurred_at=_at(2), reason="explore the crore regime")
        snapshot = ledger.snapshot(measured_at=_at(3), live_ceiling=CEILING)
    assert snapshot.balance_rupees == ONE_CRORE
    assert snapshot.exceeds_live_ceiling is True
    assert "exceeds the live ceiling" in snapshot.describe()


@pytest.mark.unit
def test_a_balance_at_or_below_the_ceiling_is_not_stamped(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger:
        snapshot = ledger.snapshot(measured_at=_at(1), live_ceiling=CEILING)
    assert snapshot.exceeds_live_ceiling is False


@pytest.mark.unit
def test_a_relative_adjustment_models_a_deposit_without_restating_the_whole_figure(
    ledger_path: Path,
) -> None:
    with _seeded(ledger_path) as ledger:
        ledger.adjust_balance(Decimal("50000"), occurred_at=_at(2), reason="top up")
        ledger.adjust_balance(Decimal("-20000"), occurred_at=_at(3), reason="withdraw")
        snapshot = ledger.snapshot(measured_at=_at(4), live_ceiling=CEILING)
    assert snapshot.balance_rupees == TEN_LAKH + Decimal("30000")


@pytest.mark.unit
def test_committing_capital_reduces_free_but_leaves_the_balance_alone(ledger_path: Path) -> None:
    """§4 — the distinction the naive design gets wrong."""
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("400000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="entry"
        )
        snapshot = ledger.snapshot(measured_at=_at(3), live_ceiling=CEILING)
    assert snapshot.balance_rupees == TEN_LAKH
    assert snapshot.committed_rupees == Decimal("400000")
    assert snapshot.free_rupees == Decimal("600000")
    assert snapshot.binding_side == "paper_free_capital"


@pytest.mark.unit
def test_releasing_a_commitment_returns_the_capital_to_free(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("400000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="entry"
        )
        ledger.release_position(position_key="RELIANCE-1", occurred_at=_at(3), reason="exit")
        snapshot = ledger.snapshot(measured_at=_at(4), live_ceiling=CEILING)
    assert snapshot.committed_rupees == Decimal(0)
    assert snapshot.free_rupees == TEN_LAKH


@pytest.mark.unit
def test_realised_profit_loss_and_costs_move_the_balance_and_stay_distinguishable(
    ledger_path: Path,
) -> None:
    """§4 — `L1.11` must be able to read cost apart from loss, so they are separate kinds."""
    with _seeded(ledger_path) as ledger:
        ledger.record_realised_profit(
            Decimal("12000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="exit in profit"
        )
        ledger.record_realised_loss(
            Decimal("5000"), position_key="TCS-1", occurred_at=_at(3), reason="stopped out"
        )
        ledger.record_cost_debit(
            Decimal("340.75"), position_key="TCS-1", occurred_at=_at(4), reason="simulated charges"
        )
        snapshot = ledger.snapshot(measured_at=_at(5), live_ceiling=CEILING)
        costs = ledger.total_by_kind(PaperCapitalEventKind.COST_DEBIT)
        losses = ledger.total_by_kind(PaperCapitalEventKind.REALISED_LOSS)
    assert snapshot.balance_rupees == TEN_LAKH + Decimal("12000") - Decimal("5000") - Decimal(
        "340.75"
    )
    assert costs == Decimal("340.75")
    assert losses == Decimal("5000")


@pytest.mark.unit
def test_the_whole_history_survives_a_reopen(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger:
        ledger.set_balance(Decimal("750000"), occurred_at=_at(2), reason="down-size the book")
    with PaperCapitalLedger(ledger_path) as reopened:
        snapshot = reopened.snapshot(measured_at=_at(3), live_ceiling=CEILING)
        events = reopened.events()
    assert snapshot.balance_rupees == Decimal("750000")
    assert [event.kind for event in events] == [
        PaperCapitalEventKind.SEED,
        PaperCapitalEventKind.OPERATOR_SET,
    ]


@pytest.mark.unit
def test_an_unseeded_ledger_refuses_to_report_a_balance_rather_than_answering_zero(
    ledger_path: Path,
) -> None:
    """"Not seeded" and "seeded at zero" are the same number only to a system that then sizes
    against the difference — the same distinction the order path draws."""
    with PaperCapitalLedger(ledger_path) as ledger, pytest.raises(PaperCapitalError):
        ledger.snapshot(measured_at=_at(1), live_ceiling=CEILING)


# ----------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
def test_a_commit_exceeding_free_capital_is_refused_and_never_truncated(
    ledger_path: Path,
) -> None:
    """§5 I3 — a book that shrinks the order it cannot fund is not simulating live discipline."""
    with _seeded(ledger_path) as ledger:
        with pytest.raises(InsufficientPaperCapitalError):
            ledger.commit_to_position(
                TEN_LAKH + Decimal("1"),
                position_key="RELIANCE-1",
                occurred_at=_at(2),
                reason="oversized entry",
            )
        snapshot = ledger.snapshot(measured_at=_at(3), live_ceiling=CEILING)
    assert snapshot.committed_rupees == Decimal(0)
    assert snapshot.free_rupees == TEN_LAKH


@pytest.mark.adversarial
def test_two_commits_against_one_balance_cannot_both_succeed(ledger_path: Path) -> None:
    """The double-spend §4 exists to stop."""
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("600000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="first"
        )
        with pytest.raises(InsufficientPaperCapitalError):
            ledger.commit_to_position(
                Decimal("600000"), position_key="TCS-1", occurred_at=_at(3), reason="second"
            )


@pytest.mark.adversarial
def test_a_release_without_a_matching_commit_is_refused(ledger_path: Path) -> None:
    """§5 I2 — it would manufacture free capital out of nothing."""
    with _seeded(ledger_path) as ledger, pytest.raises(UnmatchedPaperCapitalReleaseError):
        ledger.release_position(
            position_key="NEVER-OPENED", occurred_at=_at(2), reason="phantom exit"
        )


@pytest.mark.adversarial
def test_releasing_the_same_position_twice_is_refused(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("100000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="entry"
        )
        ledger.release_position(position_key="RELIANCE-1", occurred_at=_at(3), reason="exit")
        with pytest.raises(UnmatchedPaperCapitalReleaseError):
            ledger.release_position(
                position_key="RELIANCE-1", occurred_at=_at(4), reason="exit again"
            )


@pytest.mark.adversarial
def test_committing_the_same_position_key_twice_is_refused(ledger_path: Path) -> None:
    """Two commits under one key make the release ambiguous, and an ambiguous release is how a
    reservation leaks."""
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("100000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="entry"
        )
        with pytest.raises(PaperCapitalError):
            ledger.commit_to_position(
                Decimal("100000"), position_key="RELIANCE-1", occurred_at=_at(3), reason="again"
            )


@pytest.mark.adversarial
def test_an_edit_below_the_committed_figure_is_accepted_and_reported_over_committed(
    ledger_path: Path,
) -> None:
    """§5 I4 — the operator may always state the truth about their virtual capital."""
    with _seeded(ledger_path) as ledger:
        ledger.commit_to_position(
            Decimal("800000"), position_key="RELIANCE-1", occurred_at=_at(2), reason="entry"
        )
        ledger.set_balance(Decimal("500000"), occurred_at=_at(3), reason="shrink the book")
        snapshot = ledger.snapshot(measured_at=_at(4), live_ceiling=CEILING)
        with pytest.raises(InsufficientPaperCapitalError):
            ledger.commit_to_position(
                Decimal("1"), position_key="TCS-1", occurred_at=_at(5), reason="anything at all"
            )
    assert snapshot.over_committed is True
    assert snapshot.free_rupees == Decimal(0)
    assert snapshot.is_tradeable is False
    assert snapshot.binding_side == "paper_over_committed"


@pytest.mark.adversarial
def test_a_loss_larger_than_the_balance_floors_at_zero_and_records_the_shortfall(
    ledger_path: Path,
) -> None:
    """§5 I5 — a paper book cannot owe money to nobody, and a negative balance would let a blown
    book keep trading."""
    with _seeded(ledger_path) as ledger:
        event = ledger.record_realised_loss(
            TEN_LAKH + Decimal("40000"),
            position_key="RELIANCE-1",
            occurred_at=_at(2),
            reason="catastrophic",
        )
        snapshot = ledger.snapshot(measured_at=_at(3), live_ceiling=CEILING)
    assert event.unfunded_shortfall_rupees == Decimal("40000")
    assert snapshot.balance_rupees == Decimal(0)
    assert snapshot.is_tradeable is False
    assert snapshot.binding_side == "paper_exhausted"


@pytest.mark.adversarial
@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_an_event_with_no_stated_reason_is_refused_at_write_time(
    ledger_path: Path, reason: str
) -> None:
    """§5 I6 — an unexplained balance change is indistinguishable from a bug months later."""
    with _seeded(ledger_path) as ledger, pytest.raises(UnreasonedPaperCapitalEventError):
        ledger.set_balance(Decimal("1"), occurred_at=_at(2), reason=reason)


@pytest.mark.adversarial
@pytest.mark.parametrize("amount", [Decimal("-1"), Decimal("0")])
def test_a_non_positive_balance_edit_or_commit_is_refused(
    ledger_path: Path, amount: Decimal
) -> None:
    with _seeded(ledger_path) as ledger:
        with pytest.raises(PaperCapitalError):
            ledger.set_balance(amount, occurred_at=_at(2), reason="nonsense")
        with pytest.raises(PaperCapitalError):
            ledger.commit_to_position(
                amount, position_key="RELIANCE-1", occurred_at=_at(3), reason="nonsense"
            )


@pytest.mark.adversarial
def test_an_event_timestamped_before_the_previous_one_is_refused(ledger_path: Path) -> None:
    """A ledger whose order can be argued with is a ledger whose balance can be argued with."""
    with _seeded(ledger_path) as ledger:
        ledger.set_balance(Decimal("900000"), occurred_at=_at(5), reason="first")
        with pytest.raises(PaperCapitalError):
            ledger.set_balance(Decimal("800000"), occurred_at=_at(4), reason="backdated")


@pytest.mark.adversarial
def test_a_naive_timestamp_is_refused_because_ist_is_not_optional(ledger_path: Path) -> None:
    with _seeded(ledger_path) as ledger, pytest.raises(PaperCapitalError):
        ledger.set_balance(
            Decimal("900000"),
            occurred_at=datetime(2026, 8, 13, 10, 0),  # noqa: DTZ001 — the naive clock IS the input
            reason="naive clock",
        )


@pytest.mark.adversarial
def test_a_corrupted_checkpoint_is_detected_rather_than_trusted(ledger_path: Path) -> None:
    """§4 — the checkpoint is an optimisation, and an optimisation that can silently disagree with
    its source is a defect."""
    with _seeded(ledger_path) as ledger:
        ledger.set_balance(Decimal("700000"), occurred_at=_at(2), reason="down-size")
    connection = sqlite3.connect(ledger_path)
    connection.execute("UPDATE paper_capital_checkpoint SET balance_rupees = '999999999'")
    connection.commit()
    connection.close()
    with (
        PaperCapitalLedger(ledger_path) as reopened,
        pytest.raises(PaperCapitalCheckpointDivergedError),
    ):
        reopened.snapshot(measured_at=_at(3), live_ceiling=CEILING)


@pytest.mark.adversarial
def test_the_event_log_cannot_be_amended_through_the_public_surface(ledger_path: Path) -> None:
    """§5 I1 — there is no edit or delete, only compensation."""
    with _seeded(ledger_path) as ledger:
        assert not [
            name
            for name in dir(ledger)
            if not name.startswith("_")
            and any(verb in name for verb in ("delete", "amend", "update", "overwrite"))
        ]


# -------------------------------------------------------------------------- property


_LEGAL_AMOUNTS = hypothesis_strategies.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("500000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(
    profits=hypothesis_strategies.lists(_LEGAL_AMOUNTS, min_size=0, max_size=6),
    costs=hypothesis_strategies.lists(_LEGAL_AMOUNTS, min_size=0, max_size=6),
)
def test_the_fold_always_equals_the_checkpoint(
    tmp_path_factory: pytest.TempPathFactory, profits: list[Decimal], costs: list[Decimal]
) -> None:
    """No legal sequence of events can make the cached figure disagree with a fresh replay."""
    path = tmp_path_factory.mktemp("fold") / "paper_capital_ledger.sqlite3"
    minute = 0
    with _seeded(path) as ledger:
        for amount in profits:
            minute += 1
            ledger.record_realised_profit(
                amount, position_key=f"P{minute}", occurred_at=_at(minute), reason="profit"
            )
        for amount in costs:
            minute += 1
            ledger.record_cost_debit(
                amount, position_key=f"C{minute}", occurred_at=_at(minute), reason="cost"
            )
        snapshot = ledger.snapshot(measured_at=_at(minute + 1), live_ceiling=CEILING)
        replayed = ledger.fold_from_events()
    assert snapshot.balance_rupees == replayed.balance_rupees
    expected = TEN_LAKH + sum(profits, Decimal(0)) - sum(costs, Decimal(0))
    assert snapshot.balance_rupees == max(expected, Decimal(0))


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(commitments=hypothesis_strategies.lists(_LEGAL_AMOUNTS, min_size=1, max_size=5))
def test_free_capital_never_exceeds_the_balance_and_never_goes_negative(
    tmp_path_factory: pytest.TempPathFactory, commitments: list[Decimal]
) -> None:
    path = tmp_path_factory.mktemp("free") / "paper_capital_ledger.sqlite3"
    with _seeded(path) as ledger:
        for index, amount in enumerate(commitments, start=1):
            with suppress(InsufficientPaperCapitalError):
                ledger.commit_to_position(
                    amount, position_key=f"K{index}", occurred_at=_at(index), reason="entry"
                )
            snapshot = ledger.snapshot(measured_at=_at(index), live_ceiling=CEILING)
            assert Decimal(0) <= snapshot.free_rupees <= snapshot.balance_rupees
            assert snapshot.committed_rupees >= Decimal(0)


@pytest.mark.property
@settings(max_examples=40, deadline=None)
@given(figure=_LEGAL_AMOUNTS)
def test_the_snapshot_reports_the_same_four_members_the_live_resolver_does(
    tmp_path_factory: pytest.TempPathFactory, figure: Decimal
) -> None:
    """§6 — parity, so `F04` swaps the source by mode instead of branching on it."""
    path = tmp_path_factory.mktemp("parity") / "paper_capital_ledger.sqlite3"
    with _seeded(path) as ledger:
        ledger.set_balance(figure, occurred_at=_at(1), reason="operator")
        snapshot: PaperCapitalSnapshot = ledger.snapshot(measured_at=_at(2), live_ceiling=CEILING)
    assert isinstance(snapshot.deployable_rupees, Decimal)
    assert isinstance(snapshot.is_tradeable, bool)
    assert snapshot.binding_side in {
        "paper_free_capital",
        "paper_exhausted",
        "paper_fully_committed",
        "paper_over_committed",
    }
    assert isinstance(snapshot.describe(), str)
    assert snapshot.deployable_rupees == snapshot.free_rupees
