"""The watchdog, tested with its clock, its sleep and its reconciler injected.

`R.05`/`hermetic`: a supervisor whose loop can only be exercised by waiting is a supervisor nobody
exercises, so the clock is a callable and the tests move it by hand. The behaviours under test are
the ones that decide whether a wedged trader keeps trading: a gap in the heartbeat, a tolerance that
has to come out of the data rather than out of a constant, a restart that must not inherit
yesterday's opinion, and a beat stamped in the future trying to look alive forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.order_path.trading_control_latch import (
    LatchState,
    OperatorAuthorityScope,
    TradingControlLatchStore,
    TradingMode,
    authorize_operator_action,
    required_confirmation_phrase,
)
from nse_algo_trader.order_path.trading_halt_watchdog import (
    HaltTrigger,
    HeartbeatVerdict,
    InsufficientHeartbeatHistoryError,
    ReconciliationVerdict,
    TraderHeartbeatJournal,
    TradingHaltWatchdog,
    derive_heartbeat_staleness_threshold,
    heartbeat_intervals,
    supervise_until_stopped,
)

pytestmark = pytest.mark.unit

_ARMING_SECRET = "a-secret-only-this-host-has"
_COMPONENT = "order_path_trader"
_SESSION_START = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)


class SteppableClock:
    """A clock the test moves. The watchdog cannot tell it from `datetime.now`."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


@pytest.fixture
def arming_key_path(tmp_path: Path) -> Path:
    path = tmp_path / "operator_arming_key.txt"
    path.write_text(f"{_ARMING_SECRET}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def latch_store(tmp_path: Path) -> TradingControlLatchStore:
    return TradingControlLatchStore(database_path=tmp_path / "trading_control_latch.sqlite3")


@pytest.fixture
def heartbeat_journal(tmp_path: Path) -> TraderHeartbeatJournal:
    return TraderHeartbeatJournal(database_path=tmp_path / "trader_heartbeat.sqlite3")


@pytest.fixture
def clock() -> SteppableClock:
    return SteppableClock(_SESSION_START)


def _open_the_gate(
    store: TradingControlLatchStore, arming_key_path: Path, *, live: bool = False
) -> None:
    """Put the system into the only state where the watchdog has anything to do."""
    if live:
        store.arm_live_trading(
            authorization=authorize_operator_action(
                operator_name="ajith",
                authority_scope=OperatorAuthorityScope.ARM_LIVE_TRADING,
                typed_confirmation=required_confirmation_phrase(
                    OperatorAuthorityScope.ARM_LIVE_TRADING, _ARMING_SECRET
                ),
                arming_key_path=arming_key_path,
            ),
            reason="graduated; arming for the session",
        )
    store.release_latch(
        authorization=authorize_operator_action(
            operator_name="ajith",
            authority_scope=OperatorAuthorityScope.RELEASE_TRADING_HALT,
            typed_confirmation=required_confirmation_phrase(
                OperatorAuthorityScope.RELEASE_TRADING_HALT, _ARMING_SECRET
            ),
            arming_key_path=arming_key_path,
        ),
        reason="operator at the desk",
    )


def _beat_regularly(
    journal: TraderHeartbeatJournal,
    clock: SteppableClock,
    *,
    beats: int,
    interval: timedelta,
) -> None:
    for _ in range(beats):
        journal.record_heartbeat(
            component=_COMPONENT, claimed_beat_at=clock.now, recorded_at=clock.now, process_id=4242
        )
        clock.advance(interval)


def _watchdog(
    latch_store: TradingControlLatchStore,
    heartbeat_journal: TraderHeartbeatJournal,
    clock: SteppableClock,
    **overrides: object,
) -> TradingHaltWatchdog:
    return TradingHaltWatchdog(
        latch_store=latch_store,
        heartbeat_journal=heartbeat_journal,
        monitored_component=_COMPONENT,
        clock=clock,
        **overrides,  # type: ignore[arg-type]
    )


class TestTheToleranceIsDerivedFromTheData:
    def test_no_intervals_means_no_tolerance_rather_than_an_invented_one(self) -> None:
        with pytest.raises(InsufficientHeartbeatHistoryError, match="derived from data"):
            derive_heartbeat_staleness_threshold([])

    def test_the_tolerance_scales_with_the_observed_beat_rate(self) -> None:
        fast = derive_heartbeat_staleness_threshold([timedelta(seconds=1)] * 5)
        slow = derive_heartbeat_staleness_threshold([timedelta(seconds=60)] * 5)
        assert slow.staleness_tolerance.total_seconds() == pytest.approx(
            60 * fast.staleness_tolerance.total_seconds()
        )
        assert fast.staleness_tolerance > timedelta(seconds=1)
        assert slow.staleness_tolerance > timedelta(seconds=60)

    def test_a_gap_already_seen_while_healthy_never_becomes_a_halt(self) -> None:
        intervals = [timedelta(seconds=1)] * 4 + [timedelta(seconds=10)]
        estimate = derive_heartbeat_staleness_threshold(intervals)
        assert estimate.staleness_tolerance > timedelta(seconds=10)
        assert estimate.longest_observed_interval == timedelta(seconds=10)

    def test_a_jittery_beat_earns_a_wider_tolerance_than_a_regular_one(self) -> None:
        regular = derive_heartbeat_staleness_threshold([timedelta(seconds=1)] * 9)
        jittery = derive_heartbeat_staleness_threshold(
            [timedelta(seconds=value) for value in (0.6, 1.4, 0.7, 1.3, 0.8, 1.2, 0.9, 1.1, 1.0)]
        )
        assert jittery.staleness_tolerance > regular.staleness_tolerance
        assert jittery.dispersion > regular.dispersion

    def test_a_perfectly_regular_beat_still_gets_a_positive_dispersion(self) -> None:
        estimate = derive_heartbeat_staleness_threshold([timedelta(seconds=2)] * 20)
        assert estimate.dispersion > timedelta(0)
        assert estimate.staleness_tolerance > timedelta(seconds=2)

    def test_more_evidence_bounds_the_false_halt_rate_more_tightly(self) -> None:
        few = derive_heartbeat_staleness_threshold([timedelta(seconds=1)] * 3)
        many = derive_heartbeat_staleness_threshold([timedelta(seconds=1)] * 300)
        assert many.false_halt_probability_bound < few.false_halt_probability_bound
        assert many.cantelli_multiplier > few.cantelli_multiplier

    def test_zero_and_negative_gaps_are_not_evidence_of_an_instant_heartbeat(self) -> None:
        estimate = derive_heartbeat_staleness_threshold(
            [timedelta(seconds=1), timedelta(0), timedelta(seconds=1), timedelta(seconds=-5)]
        )
        assert estimate.sample_size == 2
        assert estimate.centre_interval == timedelta(seconds=1)


@pytest.mark.hermetic
class TestTheWatchdogHaltsOnEvidence:
    def test_a_beating_trader_is_left_alone(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.FRESH
        assert cycle.halt_triggers == ()
        assert cycle.disposition_after.latch_state is LatchState.RELEASED

    def test_a_heartbeat_gap_beyond_the_derived_tolerance_halts_trading(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        watchdog = _watchdog(latch_store, heartbeat_journal, clock)
        tolerance = watchdog.assess_heartbeat_freshness().tolerance
        assert tolerance is not None
        clock.advance(tolerance.staleness_tolerance + timedelta(seconds=1))

        cycle = watchdog.run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.STALE
        assert cycle.halt_triggers == (HaltTrigger.HEARTBEAT_STALE,)
        assert cycle.did_halt_trading
        assert not latch_store.read_disposition().is_submission_permitted
        assert "heartbeat_stale" in latch_store.read_disposition().reason

    def test_the_halt_reason_records_the_derivation_that_justified_it(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        clock.advance(timedelta(minutes=5))
        _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        reason = latch_store.read_disposition().reason
        assert "Cantelli" in reason and "n=5" in reason

    def test_a_trader_that_never_beat_at_all_halts_trading(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.NO_EVIDENCE
        assert cycle.halt_triggers == (HaltTrigger.HEARTBEAT_ABSENT,)
        assert cycle.did_halt_trading

    def test_a_single_beat_cannot_derive_a_tolerance_and_therefore_cannot_prove_life(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=1, interval=timedelta(seconds=1))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.TOLERANCE_UNDERIVABLE
        assert cycle.did_halt_trading

    def test_a_failed_reconciliation_halts_a_perfectly_healthy_trader(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))

        def failing_probe() -> ReconciliationVerdict:
            return ReconciliationVerdict(
                succeeded=False,
                checked_at=clock.now,
                detail="broker reports a position this system does not know it holds",
            )

        cycle = _watchdog(
            latch_store, heartbeat_journal, clock, reconciliation_probe=failing_probe
        ).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.FRESH
        assert cycle.halt_triggers == (HaltTrigger.RECONCILIATION_FAILED,)
        assert cycle.did_halt_trading
        assert "does not know it holds" in latch_store.read_disposition().reason

    def test_a_reconciler_that_raises_is_a_failed_reconciliation_not_an_ignored_one(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))

        def exploding_probe() -> ReconciliationVerdict:
            raise RuntimeError("the broker session expired")

        cycle = _watchdog(
            latch_store, heartbeat_journal, clock, reconciliation_probe=exploding_probe
        ).run_supervision_cycle()
        assert cycle.halt_triggers == (HaltTrigger.RECONCILIATION_FAILED,)
        assert cycle.did_halt_trading

    def test_an_operator_can_halt_with_no_evidence_at_all(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).halt_on_operator_command(
            operator_name="ajith", reason="I do not like what I am seeing"
        )
        assert cycle.halt_triggers == (HaltTrigger.OPERATOR_COMMAND,)
        assert cycle.did_halt_trading

    def test_the_watchdog_never_opens_a_latch_it_finds_closed(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
    ) -> None:
        latch_store.latch(latched_by="risk_engine", reason="daily loss bound crossed")
        _beat_regularly(heartbeat_journal, clock, beats=20, interval=timedelta(seconds=1))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.FRESH
        assert cycle.disposition_after.latch_state is LatchState.LATCHED
        assert not cycle.did_halt_trading

    def test_the_poll_interval_is_derived_from_the_same_estimate(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        tolerance = cycle.heartbeat_assessment.tolerance
        assert tolerance is not None
        assert cycle.next_poll_interval == tolerance.staleness_tolerance / 2


@pytest.mark.hermetic
class TestTheWatchdogReconcilesOnItsOwnRestart:
    def test_a_restart_into_a_live_trader_that_is_still_beating_leaves_it_alone(
        self,
        tmp_path: Path,
        arming_key_path: Path,
        clock: SteppableClock,
    ) -> None:
        latch_path = tmp_path / "latch.sqlite3"
        heartbeat_path = tmp_path / "heartbeat.sqlite3"
        _open_the_gate(TradingControlLatchStore(database_path=latch_path), arming_key_path)
        _beat_regularly(
            TraderHeartbeatJournal(database_path=heartbeat_path),
            clock,
            beats=6,
            interval=timedelta(seconds=1),
        )
        restarted = TradingHaltWatchdog(
            latch_store=TradingControlLatchStore(database_path=latch_path),
            heartbeat_journal=TraderHeartbeatJournal(database_path=heartbeat_path),
            monitored_component=_COMPONENT,
            clock=clock,
            reconciliation_probe=lambda: ReconciliationVerdict(
                succeeded=True, checked_at=clock.now, detail="broker and local agree"
            ),
        )
        cycle = restarted.reconcile_on_start()
        assert cycle.is_startup_reconciliation
        assert cycle.halt_triggers == ()
        assert cycle.disposition_after.latch_state is LatchState.RELEASED

    def test_a_restart_the_next_day_does_not_inherit_yesterdays_released_state(
        self,
        tmp_path: Path,
        arming_key_path: Path,
        clock: SteppableClock,
    ) -> None:
        latch_path = tmp_path / "latch.sqlite3"
        heartbeat_path = tmp_path / "heartbeat.sqlite3"
        _open_the_gate(
            TradingControlLatchStore(database_path=latch_path), arming_key_path, live=True
        )
        _beat_regularly(
            TraderHeartbeatJournal(database_path=heartbeat_path),
            clock,
            beats=10,
            interval=timedelta(seconds=1),
        )
        # Yesterday ended. Nobody beat overnight; the watchdog itself was restarted.
        clock.advance(timedelta(days=1))
        restarted = TradingHaltWatchdog(
            latch_store=TradingControlLatchStore(database_path=latch_path),
            heartbeat_journal=TraderHeartbeatJournal(database_path=heartbeat_path),
            monitored_component=_COMPONENT,
            clock=clock,
            reconciliation_probe=lambda: ReconciliationVerdict(
                succeeded=True, checked_at=clock.now, detail="broker and local agree"
            ),
        )
        cycle = restarted.reconcile_on_start()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.STALE
        assert cycle.did_halt_trading
        assert cycle.disposition_after.trading_mode is TradingMode.LIVE
        assert "startup reconciliation" in cycle.disposition_after.reason

    def test_a_restart_into_a_live_released_system_with_no_reconciler_halts(
        self,
        tmp_path: Path,
        arming_key_path: Path,
        clock: SteppableClock,
    ) -> None:
        latch_path = tmp_path / "latch.sqlite3"
        heartbeat_path = tmp_path / "heartbeat.sqlite3"
        _open_the_gate(
            TradingControlLatchStore(database_path=latch_path), arming_key_path, live=True
        )
        _beat_regularly(
            TraderHeartbeatJournal(database_path=heartbeat_path),
            clock,
            beats=10,
            interval=timedelta(seconds=1),
        )
        cycle = TradingHaltWatchdog(
            latch_store=TradingControlLatchStore(database_path=latch_path),
            heartbeat_journal=TraderHeartbeatJournal(database_path=heartbeat_path),
            monitored_component=_COMPONENT,
            clock=clock,
        ).reconcile_on_start()
        assert HaltTrigger.RECONCILIATION_UNAVAILABLE in cycle.halt_triggers
        assert cycle.did_halt_trading

    def test_a_restart_reads_a_halt_a_previous_watchdog_wrote(
        self,
        tmp_path: Path,
        arming_key_path: Path,
        clock: SteppableClock,
    ) -> None:
        latch_path = tmp_path / "latch.sqlite3"
        heartbeat_path = tmp_path / "heartbeat.sqlite3"
        _open_the_gate(TradingControlLatchStore(database_path=latch_path), arming_key_path)
        _beat_regularly(
            TraderHeartbeatJournal(database_path=heartbeat_path),
            clock,
            beats=6,
            interval=timedelta(seconds=1),
        )
        first = TradingHaltWatchdog(
            latch_store=TradingControlLatchStore(database_path=latch_path),
            heartbeat_journal=TraderHeartbeatJournal(database_path=heartbeat_path),
            monitored_component=_COMPONENT,
            clock=clock,
        )
        clock.advance(timedelta(minutes=10))
        assert first.run_supervision_cycle().did_halt_trading

        # The trader comes back and beats healthily again. The watchdog restarts too. Neither of
        # those facts is permission to trade: only an operator can clear the halt.
        _beat_regularly(
            TraderHeartbeatJournal(database_path=heartbeat_path),
            clock,
            beats=6,
            interval=timedelta(seconds=1),
        )
        second = TradingHaltWatchdog(
            latch_store=TradingControlLatchStore(database_path=latch_path),
            heartbeat_journal=TraderHeartbeatJournal(database_path=heartbeat_path),
            monitored_component=_COMPONENT,
            clock=clock,
        )
        cycle = second.reconcile_on_start()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.FRESH
        assert cycle.disposition_after.latch_state is LatchState.LATCHED
        assert cycle.disposition_before.changed_by == "trading_halt_watchdog"


@pytest.mark.adversarial
class TestAForgedHeartbeatCannotKeepTheSystemArmed:
    def test_a_beat_claiming_a_time_after_it_was_written_halts_rather_than_reassures(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        # The wedged trader's last act: one beat stamped an hour into the future, which under a
        # naive "is the newest timestamp recent?" check would keep it armed for the whole hour.
        heartbeat_journal.record_heartbeat(
            component=_COMPONENT,
            claimed_beat_at=clock.now + timedelta(hours=1),
            recorded_at=clock.now,
            process_id=4242,
        )
        clock.advance(timedelta(minutes=30))
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP
        assert cycle.halt_triggers == (HaltTrigger.HEARTBEAT_TIMESTAMP_IMPLAUSIBLE,)
        assert cycle.did_halt_trading
        assert not latch_store.read_disposition().is_submission_permitted

    def test_a_wholly_future_dated_record_reads_as_a_negative_age_and_halts(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        future = clock.now + timedelta(hours=2)
        for offset in range(6):
            heartbeat_journal.record_heartbeat(
                component=_COMPONENT,
                claimed_beat_at=future + timedelta(seconds=offset),
                recorded_at=future + timedelta(seconds=offset),
                process_id=4242,
            )
        cycle = _watchdog(latch_store, heartbeat_journal, clock).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.IMPLAUSIBLE_TIMESTAMP
        assert cycle.heartbeat_assessment.heartbeat_age is not None
        assert cycle.heartbeat_assessment.heartbeat_age < timedelta(0)
        assert cycle.did_halt_trading

    def test_the_effective_beat_time_never_exceeds_the_moment_it_was_recorded(
        self, heartbeat_journal: TraderHeartbeatJournal, clock: SteppableClock
    ) -> None:
        record = heartbeat_journal.record_heartbeat(
            component=_COMPONENT,
            claimed_beat_at=clock.now + timedelta(days=365),
            recorded_at=clock.now,
        )
        assert record.effective_beat_at == clock.now
        assert record.is_timestamp_implausible

    def test_out_of_order_beats_do_not_collapse_the_derived_tolerance(
        self, heartbeat_journal: TraderHeartbeatJournal, clock: SteppableClock
    ) -> None:
        for offset in (0, 5, 1, 4, 2, 3):
            heartbeat_journal.record_heartbeat(
                component=_COMPONENT,
                claimed_beat_at=clock.now + timedelta(seconds=offset),
                recorded_at=clock.now + timedelta(seconds=offset),
            )
        records = heartbeat_journal.recent_heartbeats(component=_COMPONENT)
        intervals = heartbeat_intervals(records)
        assert intervals == (timedelta(seconds=1),) * 5

    def test_an_unreadable_heartbeat_journal_reads_as_no_evidence_of_life(
        self,
        tmp_path: Path,
        latch_store: TradingControlLatchStore,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        corrupt_path = tmp_path / "heartbeat.sqlite3"
        corrupt_path.write_bytes(b"not a database")
        cycle = _watchdog(
            latch_store, TraderHeartbeatJournal(database_path=corrupt_path), clock
        ).run_supervision_cycle()
        assert cycle.heartbeat_assessment.verdict is HeartbeatVerdict.NO_EVIDENCE
        assert cycle.did_halt_trading


@pytest.mark.hermetic
class TestTheSupervisionLoop:
    def test_the_loop_reconciles_first_then_polls_until_told_to_stop(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
        arming_key_path: Path,
    ) -> None:
        _open_the_gate(latch_store, arming_key_path)
        _beat_regularly(heartbeat_journal, clock, beats=6, interval=timedelta(seconds=1))
        watchdog = _watchdog(latch_store, heartbeat_journal, clock)
        slept: list[float] = []
        cycles_seen: list[bool] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            # The trader keeps beating while the watchdog sleeps, which is what keeps it fresh.
            heartbeat_journal.record_heartbeat(
                component=_COMPONENT, claimed_beat_at=clock.now, recorded_at=clock.now
            )
            clock.advance(timedelta(seconds=seconds))

        cycles = supervise_until_stopped(
            watchdog,
            stop_requested=lambda: len(cycles_seen) >= 4,
            sleep=fake_sleep,
            bootstrap_poll_interval=timedelta(seconds=1),
            report=lambda cycle: cycles_seen.append(cycle.is_startup_reconciliation),
        )
        assert cycles == 4
        assert cycles_seen[0] is True
        assert cycles_seen[1:] == [False, False, False]
        assert all(interval > 0 for interval in slept)
        assert latch_store.read_disposition().is_submission_permitted

    def test_the_loop_falls_back_to_the_operator_bootstrap_interval_with_no_history(
        self,
        latch_store: TradingControlLatchStore,
        heartbeat_journal: TraderHeartbeatJournal,
        clock: SteppableClock,
    ) -> None:
        watchdog = _watchdog(latch_store, heartbeat_journal, clock)
        slept: list[float] = []
        cycles_seen: list[bool] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(timedelta(seconds=seconds))

        supervise_until_stopped(
            watchdog,
            stop_requested=lambda: len(cycles_seen) >= 2,
            sleep=fake_sleep,
            bootstrap_poll_interval=timedelta(seconds=0.25),
            report=lambda cycle: cycles_seen.append(cycle.did_halt_trading),
        )
        assert slept == [0.25]
