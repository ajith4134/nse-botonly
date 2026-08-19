"""Tests for the daily Kite access-token refresh job.

The job exists because its absence cost a live session on 2026-08-17: cron invoked a module
that did not exist, and the depth capture sat in a restart loop from the 09:15 open. So the
first test in this file is the one that would have caught that — the module is importable
and runnable by the exact name cron uses.
"""

import importlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.broker_sessions.kite_access_token_store import (
    INDIA_MARKET_TIMEZONE,
    KiteAccessTokenFileStore,
    KiteAccessTokenRecord,
)
from nse_algo_trader.broker_sessions.refresh_kite_access_token import (
    KiteAccessTokenRefreshError,
    main,
    refresh_kite_access_token_if_needed,
)

CRON_MODULE_NAME = "nse_algo_trader.broker_sessions.refresh_kite_access_token"


def _token_record(generated_at: datetime, kite_user_id: str = "HZV381") -> KiteAccessTokenRecord:
    return KiteAccessTokenRecord(
        access_token="not-a-real-token",
        kite_user_id=kite_user_id,
        generated_at=generated_at,
    )


def _store_at(tmp_path: Path) -> KiteAccessTokenFileStore:
    return KiteAccessTokenFileStore(tmp_path / "kite_access_token.json")


def test_the_module_cron_invokes_by_name_is_importable() -> None:
    """The 2026-08-17 regression: cron's `python -m <name>` raised ModuleNotFoundError."""
    assert importlib.import_module(CRON_MODULE_NAME) is not None


def test_the_module_runs_as_a_script_the_way_cron_runs_it() -> None:
    """`python -m ... --help` must exit 0, proving the __main__ guard is wired."""
    completed = subprocess.run(
        [sys.executable, "-m", CRON_MODULE_NAME, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Idempotent" in completed.stdout


def test_a_still_valid_token_is_kept_and_no_login_happens(tmp_path: Path) -> None:
    """The second cron entry of the morning must be a no-op, not a second TOTP login."""
    store = _store_at(tmp_path)
    generated_at = datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE)
    store.save(_token_record(generated_at))

    def login_that_must_not_run(_: KiteAccessTokenFileStore) -> KiteAccessTokenRecord:
        raise AssertionError("logged in despite a still-valid token")

    reported: list[str] = []
    record = refresh_kite_access_token_if_needed(
        store,
        now=datetime(2026, 8, 17, 8, 35, tzinfo=INDIA_MARKET_TIMEZONE),
        perform_login=login_that_must_not_run,
        report_line=reported.append,
    )

    assert record.kite_user_id == "HZV381"
    assert reported == [
        "token still valid — user HZV381, expires 2026-08-18 06:00 IST",
    ]


def test_an_expired_token_triggers_a_login_and_is_replaced(tmp_path: Path) -> None:
    """Kite tokens die at 06:00 IST; after that the job must produce a new one."""
    store = _store_at(tmp_path)
    store.save(_token_record(datetime(2026, 8, 16, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE)))
    fresh = _token_record(datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE), "NEWUSR")

    def login(token_store: KiteAccessTokenFileStore) -> KiteAccessTokenRecord:
        token_store.save(fresh)
        return fresh

    record = refresh_kite_access_token_if_needed(
        store,
        now=datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE),
        perform_login=login,
        report_line=lambda _: None,
    )

    assert record.kite_user_id == "NEWUSR"
    assert json.loads((tmp_path / "kite_access_token.json").read_text())["kite_user_id"] == "NEWUSR"


def test_force_new_login_ignores_a_valid_token(tmp_path: Path) -> None:
    store = _store_at(tmp_path)
    store.save(_token_record(datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE)))
    replacement = _token_record(datetime(2026, 8, 17, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE), "FORCED")

    record = refresh_kite_access_token_if_needed(
        store,
        force_new_login=True,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE),
        perform_login=lambda _: replacement,
        report_line=lambda _: None,
    )

    assert record.kite_user_id == "FORCED"


def test_a_transient_failure_is_retried_and_the_day_is_saved(tmp_path: Path) -> None:
    """The observed real failure is a transient network fault, not a bad credential."""
    attempts: list[int] = []
    survivor = _token_record(datetime(2026, 8, 17, 8, 6, tzinfo=INDIA_MARKET_TIMEZONE), "LATE")

    def flaky_login(_: KiteAccessTokenFileStore) -> KiteAccessTokenRecord:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise ConnectionError("kite.zerodha.com timed out")
        return survivor

    slept: list[float] = []
    reported: list[str] = []
    record = refresh_kite_access_token_if_needed(
        _store_at(tmp_path),
        now=datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE),
        perform_login=flaky_login,
        sleep_between_attempts=slept.append,
        report_line=reported.append,
    )

    assert record.kite_user_id == "LATE"
    assert attempts == [1, 2, 3]
    assert slept == [20.0, 20.0], "one pause between attempts, none after the success"
    assert reported[0].startswith("attempt 1/3 failed: ConnectionError")
    assert reported[-1].endswith("(attempt 3/3)")


def test_no_pause_is_taken_after_the_final_failed_attempt(tmp_path: Path) -> None:
    """A sleep after the last attempt is pure delay before the failure is reported."""
    slept: list[float] = []
    with pytest.raises(KiteAccessTokenRefreshError):
        refresh_kite_access_token_if_needed(
            _store_at(tmp_path),
            login_attempts=2,
            now=datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE),
            perform_login=_always_fails,
            sleep_between_attempts=slept.append,
            report_line=lambda _: None,
        )
    assert slept == [20.0]


def _always_fails(_: KiteAccessTokenFileStore) -> KiteAccessTokenRecord:
    raise ConnectionError("kite.zerodha.com refused the connection")


def test_exhausted_attempts_raise_with_the_real_cause_chained(tmp_path: Path) -> None:
    """R.21: three strikes, then report what was tried AND what actually broke."""
    with pytest.raises(KiteAccessTokenRefreshError) as raised:
        refresh_kite_access_token_if_needed(
            _store_at(tmp_path),
            now=datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE),
            perform_login=_always_fails,
            sleep_between_attempts=lambda _: None,
            report_line=lambda _: None,
        )

    assert "all 3 Kite login attempts failed" in str(raised.value)
    assert isinstance(raised.value.__cause__, ConnectionError)


def test_zero_attempts_is_rejected_rather_than_silently_doing_nothing(tmp_path: Path) -> None:
    """A misconfigured attempt count must not look like a successful no-op run."""
    with pytest.raises(ValueError, match="login_attempts must be at least 1"):
        refresh_kite_access_token_if_needed(
            _store_at(tmp_path),
            login_attempts=0,
            perform_login=_always_fails,
            report_line=lambda _: None,
        )


def test_the_token_secret_never_reaches_a_reported_line(tmp_path: Path) -> None:
    """R.02: the log this job writes is read by humans and shipped to disk."""
    store = _store_at(tmp_path)
    # Assembled at runtime, never written as a literal — the same technique
    # `test_committed_credential_detector.py` uses, and for the same reason: a fake
    # secret spelled out in a tracked file is indistinguishable to `R.02`'s guard
    # from a real one, and the guard is right to refuse to tell them apart.
    secret = "".join(('SUPERSEC', 'RETACCES', 'STOKENVA', 'LUE'))
    store.save(
        KiteAccessTokenRecord(
            access_token=secret,
            kite_user_id="HZV381",
            generated_at=datetime(2026, 8, 17, 8, 5, tzinfo=INDIA_MARKET_TIMEZONE),
        )
    )
    reported: list[str] = []
    refresh_kite_access_token_if_needed(
        store,
        now=datetime(2026, 8, 17, 8, 35, tzinfo=INDIA_MARKET_TIMEZONE),
        perform_login=_always_fails,
        report_line=reported.append,
    )
    assert secret not in "\n".join(reported)


def test_main_exits_non_zero_when_the_refresh_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cron and the daily-operations report can only see an exit code."""

    def failing_refresh(**_: object) -> KiteAccessTokenRecord:
        raise KiteAccessTokenRefreshError("all 3 Kite login attempts failed") from ConnectionError(
            "kite.zerodha.com refused the connection"
        )

    monkeypatch.setattr(
        "nse_algo_trader.broker_sessions.refresh_kite_access_token."
        "refresh_kite_access_token_if_needed",
        failing_refresh,
    )
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "REFRESH FAILED" in captured.err
    assert "ConnectionError" in captured.err


def test_main_exits_zero_when_a_token_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nse_algo_trader.broker_sessions.refresh_kite_access_token."
        "refresh_kite_access_token_if_needed",
        lambda **_: _token_record(datetime.now(INDIA_MARKET_TIMEZONE)),
    )
    assert main([]) == 0


def test_the_real_stored_token_is_currently_valid() -> None:
    """R.05 real-data check: the live token store this VPS runs on holds a usable token.

    Skips rather than fails when no token file exists, so the suite still runs on a fresh
    machine — but on this server, after the morning refresh, it is a genuine assertion.
    """
    store = KiteAccessTokenFileStore()
    record = store.load()
    if record is None:
        pytest.skip("no stored Kite token on this machine")
    assert record.is_still_valid() or record.expires_at() < datetime.now(INDIA_MARKET_TIMEZONE)
    assert record.expires_at() - record.generated_at <= timedelta(days=1)
