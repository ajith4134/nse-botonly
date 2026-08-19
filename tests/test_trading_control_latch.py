"""The latch, tested against the only question it exists to answer: may money move right now?

Every test here is one way the answer could wrongly come back "yes". A fresh store, a deleted file,
a file full of garbage, a file cut in half, a log with a row quietly removed, a caller that simply
constructs the authorization object it was supposed to earn — all of them must produce a refusal,
and the refusal must say which of those it was.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.order_path.trading_control_latch import (
    ChangeAuthority,
    LatchEvidence,
    LatchState,
    LiveTradingNotArmedError,
    OperatorAuthorityError,
    OperatorAuthorityScope,
    OperatorAuthorization,
    TradingControlLatchStore,
    TradingControlStoreError,
    TradingHaltedError,
    TradingMode,
    authorize_operator_action,
    required_confirmation_phrase,
)
from nse_algo_trader.order_path.trading_intent import OrderNamespace

pytestmark = pytest.mark.unit

_ARMING_SECRET = "a-secret-only-this-host-has"


@pytest.fixture
def arming_key_path(tmp_path: Path) -> Path:
    """Stand in for the operator's own hand: a key file only this test created."""
    path = tmp_path / "operator_arming_key.txt"
    path.write_text(f"{_ARMING_SECRET}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def latch_path(tmp_path: Path) -> Path:
    return tmp_path / "control" / "trading_control_latch.sqlite3"


def _operator_authorization(
    scope: OperatorAuthorityScope, arming_key_path: Path, name: str = "ajith"
) -> OperatorAuthorization:
    secret = arming_key_path.read_text(encoding="utf-8").strip()
    return authorize_operator_action(
        operator_name=name,
        authority_scope=scope,
        typed_confirmation=required_confirmation_phrase(scope, secret),
        arming_key_path=arming_key_path,
    )


def _arm_live_and_release(store: TradingControlLatchStore, arming_key_path: Path) -> None:
    """The full three-act operator sequence, used by the tests that need a live, open system."""
    store.arm_live_trading(
        authorization=_operator_authorization(
            OperatorAuthorityScope.ARM_LIVE_TRADING, arming_key_path
        ),
        reason="graduated on the paper record; arming for the session",
    )
    store.release_latch(
        authorization=_operator_authorization(
            OperatorAuthorityScope.RELEASE_TRADING_HALT, arming_key_path
        ),
        reason="operator at the desk, session opening",
    )


class TestAFreshStoreIsSafe:
    def test_a_store_that_has_never_been_touched_reads_latched_and_paper(
        self, latch_path: Path
    ) -> None:
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.trading_mode is TradingMode.PAPER
        assert disposition.evidence is LatchEvidence.FRESH_STORE_SAFE_DEFAULT
        assert not disposition.is_submission_permitted

    def test_a_fresh_store_refuses_every_submission_with_a_reason(self, latch_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        with pytest.raises(TradingHaltedError, match="never been armed"):
            store.assert_submission_permitted(intended_mode=TradingMode.PAPER)
        refusal = store.refusal_for_submission(intended_mode=TradingMode.PAPER)
        assert refusal is not None and "halted" in refusal

    def test_the_safe_default_puts_the_simulated_namespace_on_the_wire(
        self, latch_path: Path
    ) -> None:
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.broker_order_namespace is OrderNamespace.SIMULATED


class TestLiveIsUnreachableWithoutAnOperator:
    def test_the_authorization_object_cannot_simply_be_constructed(self) -> None:
        with pytest.raises(OperatorAuthorityError, match="cannot be constructed directly"):
            OperatorAuthorization(
                operator_name="the system itself",
                authority_scope=OperatorAuthorityScope.ARM_LIVE_TRADING,
                authorized_at=datetime.now(UTC),
                arming_key_path=Path("/nonexistent"),
            )

    def test_no_arming_key_means_no_authorization(self, tmp_path: Path) -> None:
        with pytest.raises(OperatorAuthorityError, match="no operator arming key"):
            authorize_operator_action(
                operator_name="ajith",
                authority_scope=OperatorAuthorityScope.ARM_LIVE_TRADING,
                typed_confirmation="ARM LIVE TRADING anything",
                arming_key_path=tmp_path / "absent.txt",
            )

    def test_a_world_readable_arming_key_is_not_a_key(self, tmp_path: Path) -> None:
        path = tmp_path / "operator_arming_key.txt"
        path.write_text(_ARMING_SECRET, encoding="utf-8")
        path.chmod(0o644)
        with pytest.raises(OperatorAuthorityError, match="beyond its owner"):
            authorize_operator_action(
                operator_name="ajith",
                authority_scope=OperatorAuthorityScope.ARM_LIVE_TRADING,
                typed_confirmation=required_confirmation_phrase(
                    OperatorAuthorityScope.ARM_LIVE_TRADING, _ARMING_SECRET
                ),
                arming_key_path=path,
            )

    def test_the_right_key_with_the_wrong_phrase_is_refused(self, arming_key_path: Path) -> None:
        with pytest.raises(OperatorAuthorityError, match="does not match"):
            authorize_operator_action(
                operator_name="ajith",
                authority_scope=OperatorAuthorityScope.ARM_LIVE_TRADING,
                typed_confirmation="ARM LIVE TRADING please",
                arming_key_path=arming_key_path,
            )

    def test_a_release_authorization_cannot_arm_live(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        with pytest.raises(OperatorAuthorityError, match="permits release_trading_halt"):
            store.arm_live_trading(
                authorization=_operator_authorization(
                    OperatorAuthorityScope.RELEASE_TRADING_HALT, arming_key_path
                ),
                reason="trying to stretch a release into an arm",
            )

    def test_the_system_can_never_reach_live_by_any_public_call_it_can_make_alone(
        self, latch_path: Path
    ) -> None:
        """Everything the running system may do without a human, exhaustively, ends in paper."""
        store = TradingControlLatchStore(database_path=latch_path)
        store.latch(latched_by="risk_engine", reason="daily loss bound crossed")
        store.revert_to_paper_trading(reverted_by="risk_engine", reason="back to paper")
        store.latch(latched_by="watchdog", reason="heartbeat gone")
        assert store.read_disposition().trading_mode is TradingMode.PAPER

    def test_a_live_submission_is_refused_while_the_durable_mode_is_paper(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        store.release_latch(
            authorization=_operator_authorization(
                OperatorAuthorityScope.RELEASE_TRADING_HALT, arming_key_path
            ),
            reason="paper session, gate open",
        )
        assert store.assert_submission_permitted(intended_mode=TradingMode.PAPER)
        with pytest.raises(LiveTradingNotArmedError, match="durable trading mode is paper"):
            store.assert_submission_permitted(intended_mode=TradingMode.LIVE)

    def test_arming_live_requires_the_latch_to_be_closed_first(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        store.release_latch(
            authorization=_operator_authorization(
                OperatorAuthorityScope.RELEASE_TRADING_HALT, arming_key_path
            ),
            reason="paper session, gate open",
        )
        with pytest.raises(OperatorAuthorityError, match="halt latch is CLOSED"):
            store.arm_live_trading(
                authorization=_operator_authorization(
                    OperatorAuthorityScope.ARM_LIVE_TRADING, arming_key_path
                ),
                reason="flipping to live mid-session",
            )

    def test_the_full_three_act_operator_sequence_does_reach_live(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        disposition = store.assert_submission_permitted(intended_mode=TradingMode.LIVE)
        assert disposition.trading_mode is TradingMode.LIVE
        assert disposition.broker_order_namespace is OrderNamespace.LIVE
        assert disposition.change_authority is ChangeAuthority.OPERATOR


class TestTheLatchIsDurable:
    def test_a_halt_survives_reopening_the_store(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        first = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(first, arming_key_path)
        assert first.read_disposition().is_submission_permitted
        first.latch(latched_by="watchdog", reason="heartbeat lost at 11:04")

        reopened = TradingControlLatchStore(database_path=latch_path)
        disposition = reopened.read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.changed_by == "watchdog"
        assert "11:04" in disposition.reason
        # The armed mode is remembered too — a halt is not a demotion to paper.
        assert disposition.trading_mode is TradingMode.LIVE

    def test_a_release_survives_reopening_the_store(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        _arm_live_and_release(TradingControlLatchStore(database_path=latch_path), arming_key_path)
        assert (
            TradingControlLatchStore(database_path=latch_path)
            .read_disposition()
            .is_submission_permitted
        )

    def test_relatching_with_the_same_reason_does_not_grow_the_log(self, latch_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        for _ in range(50):
            store.latch(latched_by="watchdog", reason="heartbeat gone")
        assert len(store.transition_history()) == 1

    def test_a_new_reason_is_always_recorded(self, latch_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        store.latch(latched_by="watchdog", reason="heartbeat gone")
        store.latch(latched_by="watchdog", reason="reconciliation failed as well")
        assert len(store.transition_history()) == 2

    def test_the_log_keeps_who_when_and_why_in_order(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        store.latch(latched_by="watchdog", reason="drawdown bound crossed")
        history = store.transition_history()
        assert [record.latch_state for record in history] == [
            LatchState.LATCHED,
            LatchState.RELEASED,
            LatchState.LATCHED,
        ]
        assert [record.change_authority for record in history] == [
            ChangeAuthority.OPERATOR,
            ChangeAuthority.OPERATOR,
            ChangeAuthority.SYSTEM,
        ]
        assert all(record.changed_at.tzinfo is not None for record in history)
        assert store.verify_transition_chain()

    def test_a_transition_with_no_reason_is_refused(self, latch_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        with pytest.raises(TradingControlStoreError, match="no reason"):
            store.latch(latched_by="watchdog", reason="   ")

    def test_a_naive_timestamp_is_refused(self, latch_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        with pytest.raises(TradingControlStoreError, match="no timezone"):
            store.latch(
                latched_by="watchdog",
                reason="halting",
                latched_at=datetime(2026, 8, 13, 11, 4),  # noqa: DTZ001 — the defect under test
            )


@pytest.mark.adversarial
class TestADamagedStoreReadsSafeRatherThanRaising:
    def test_a_deleted_database_reads_safe(self, latch_path: Path, arming_key_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        latch_path.unlink()
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.trading_mode is TradingMode.PAPER

    def test_a_file_that_is_not_a_database_reads_safe(self, latch_path: Path) -> None:
        latch_path.parent.mkdir(parents=True, exist_ok=True)
        latch_path.write_bytes(b"this is not a database, it is a note from an attacker")
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.trading_mode is TradingMode.PAPER
        assert disposition.evidence is LatchEvidence.UNREADABLE_STORE_SAFE_DEFAULT

    def test_a_truncated_database_reads_safe(self, latch_path: Path, arming_key_path: Path) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        with latch_path.open("r+b") as handle:
            handle.truncate(100)
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.trading_mode is TradingMode.PAPER
        assert disposition.evidence is not LatchEvidence.RECORDED_TRANSITION

    def test_the_database_file_alone_carries_the_state(
        self, latch_path: Path, arming_key_path: Path, tmp_path: Path
    ) -> None:
        """Copying the `.sqlite3` file must copy the latch, not an older version of it.

        Without the checkpoint after each commit the newest transitions live only in the `-wal`
        sidecar, and a restore of the database file alone would quietly reinstate a stale, more
        permissive latch. Measured, not assumed: this test failed before the checkpoint was added.
        """
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        store.latch(latched_by="watchdog", reason="heartbeat lost")
        copied = tmp_path / "copied_latch.sqlite3"
        copied.write_bytes(latch_path.read_bytes())
        disposition = TradingControlLatchStore(database_path=copied).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.reason == "heartbeat lost"

    def test_a_store_truncated_together_with_its_sidecars_reads_safe(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        for sidecar in ("-wal", "-shm"):
            companion = Path(str(latch_path) + sidecar)
            if companion.exists():
                companion.unlink()
        with latch_path.open("r+b") as handle:
            handle.truncate(latch_path.stat().st_size // 3)
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.trading_mode is TradingMode.PAPER
        assert disposition.evidence is not LatchEvidence.RECORDED_TRANSITION

    def test_a_database_path_that_is_a_directory_reads_safe(self, tmp_path: Path) -> None:
        directory = tmp_path / "not_a_file.sqlite3"
        directory.mkdir()
        disposition = TradingControlLatchStore(database_path=directory).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.evidence is LatchEvidence.UNREADABLE_STORE_SAFE_DEFAULT

    def test_deleting_the_row_that_halted_trading_reads_safe_not_released(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        store.latch(latched_by="watchdog", reason="heartbeat lost")
        connection = sqlite3.connect(latch_path)
        connection.execute(
            "DELETE FROM trading_control_transition WHERE latch_state = 'latched' "
            "AND changed_by = 'watchdog'"
        )
        connection.commit()
        connection.close()
        disposition = TradingControlLatchStore(database_path=latch_path).read_disposition()
        assert disposition.latch_state is LatchState.LATCHED
        assert disposition.evidence is LatchEvidence.TAMPERED_CHAIN_SAFE_DEFAULT
        assert not disposition.is_submission_permitted

    def test_editing_a_reason_after_the_fact_breaks_the_chain(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        connection = sqlite3.connect(latch_path)
        connection.execute(
            "UPDATE trading_control_transition SET reason = ? WHERE latch_state = 'released'",
            ("it was authorised, honestly",),
        )
        connection.commit()
        connection.close()
        store_after = TradingControlLatchStore(database_path=latch_path)
        assert not store_after.verify_transition_chain()
        assert not store_after.read_disposition().is_submission_permitted

    def test_a_broken_chain_still_permits_moves_toward_safety(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        connection = sqlite3.connect(latch_path)
        connection.execute("UPDATE trading_control_transition SET reason = 'edited'")
        connection.commit()
        connection.close()
        # Latching works; releasing and arming do not.
        store.latch(latched_by="watchdog", reason="the log looks tampered with")
        with pytest.raises(OperatorAuthorityError, match="does not verify"):
            store.release_latch(
                authorization=_operator_authorization(
                    OperatorAuthorityScope.RELEASE_TRADING_HALT, arming_key_path
                ),
                reason="ignoring the tamper",
            )

    def test_an_unusable_store_refuses_to_pretend_a_write_happened(self, tmp_path: Path) -> None:
        directory = tmp_path / "not_a_file.sqlite3"
        directory.mkdir()
        store = TradingControlLatchStore(database_path=directory)
        with pytest.raises(TradingControlStoreError, match="did NOT happen"):
            store.latch(latched_by="watchdog", reason="halting into a broken store")

    def test_a_clock_moved_backwards_does_not_make_an_old_release_current(
        self, latch_path: Path, arming_key_path: Path
    ) -> None:
        """The newest ROW wins, not the newest timestamp — otherwise a backdated write reopens."""
        store = TradingControlLatchStore(database_path=latch_path)
        _arm_live_and_release(store, arming_key_path)
        store.latch(
            latched_by="watchdog",
            reason="halted with a clock that had drifted backwards",
            latched_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert not store.read_disposition().is_submission_permitted
