"""Daily Kite access-token refresh, runnable as ``python -m`` from cron.

Kite access tokens die at roughly 06:00 IST every morning, which is before the pre-open.
Every Kite-dependent job in this project — live depth capture, the paper session, the
cross-broker quote recorder — reads the token from
:class:`KiteAccessTokenFileStore` and refuses to start without a valid one. So the refresh
is the first domino of the trading day, and when it silently fails the whole live-capture
side of the day is lost with no other symptom than services in a restart loop.

That is exactly what happened on 2026-08-17: two cron entries invoked this module by name,
the module did not exist, cron's only record was a ``ModuleNotFoundError`` in a log nobody
reads, and the depth capture sat in ``auto-restart`` from the 09:15 open. This module is
the missing piece, written so the same silence cannot repeat:

* it is **idempotent** — a still-valid token is a success, not a re-login, so the second
  cron entry of the morning is a cheap no-op rather than a redundant TOTP round-trip;
* it **retries** the login, because the failure mode observed in practice is a transient
  network or Kite-side hiccup rather than a bad credential, and a single attempt at 08:05
  IST throws away the whole day for a fault that clears in seconds;
* it **exits non-zero** on genuine failure so the process supervisor and the daily
  operations report can see it, and it prints one line per attempt so the log says what
  happened rather than only that something did;
* it **never prints the token**, only the Kite user id and the expiry.

Deliberately NOT an engine (`R.07`/`R.23b`): this is an operational entry point around
:func:`generate_and_store_daily_kite_access_token`, it carries no state between decisions
and no output that changes an allocation, so it is named for what it is — a refresh job.
The login logic itself lives in ``kite_totp_auto_login``.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from nse_algo_trader.broker_credentials.broker_api_credentials_loader import (
    BrokerName,
    load_broker_api_credentials,
    load_env_file_into_environ,
)
from nse_algo_trader.broker_credentials.kite_login_credentials_loader import (
    load_kite_login_credentials,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    KiteAccessTokenFileStore,
    KiteAccessTokenRecord,
)
from nse_algo_trader.broker_sessions.kite_totp_auto_login import (
    generate_and_store_daily_kite_access_token,
)

INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")

DEFAULT_LOGIN_ATTEMPTS = 3
"""Three attempts, matching the R.21 three-strikes rule: enough to ride out a transient
network fault, few enough that a genuinely bad credential is reported the same morning
instead of being retried into a rate limit."""

DEFAULT_SECONDS_BETWEEN_ATTEMPTS = 20.0
"""Long enough that a retry is not simply the same failed TCP connection again, short
enough that all three attempts finish well inside the gap between the two cron entries."""


class KiteAccessTokenRefreshError(RuntimeError):
    """Every login attempt failed; the trading day has no Kite session."""


def refresh_kite_access_token_if_needed(
    token_store: KiteAccessTokenFileStore | None = None,
    *,
    login_attempts: int = DEFAULT_LOGIN_ATTEMPTS,
    seconds_between_attempts: float = DEFAULT_SECONDS_BETWEEN_ATTEMPTS,
    force_new_login: bool = False,
    now: datetime | None = None,
    perform_login: Callable[[KiteAccessTokenFileStore], KiteAccessTokenRecord] | None = None,
    sleep_between_attempts: Callable[[float], None] = time.sleep,
    report_line: Callable[[str], None] = lambda line: print(line, flush=True),
) -> KiteAccessTokenRecord:
    """Return a valid token record, logging in only if the stored one is dead.

    ``perform_login``, ``sleep_between_attempts`` and ``report_line`` are injected seams so
    the retry and reporting paths can be verified without a broker round-trip or real
    wall-clock time (`R.05`: the hermetic test proves the control flow, the real-data pass
    is this job running against Kite at 08:05 IST). In production ``perform_login`` is
    ``None`` and the real TOTP login is used — the fake never reaches a live run.

    Raises :class:`KiteAccessTokenRefreshError` when every attempt failed, with the last
    underlying error chained, so the caller reports the real cause and not just "failed".
    """
    if login_attempts < 1:
        raise ValueError(f"login_attempts must be at least 1, got {login_attempts}")

    load_env_file_into_environ()
    store = token_store if token_store is not None else KiteAccessTokenFileStore()

    if not force_new_login:
        existing = store.load_if_still_valid(now)
        if existing is not None:
            report_line(
                f"token still valid — user {existing.kite_user_id}, "
                f"expires {existing.expires_at().astimezone(INDIA_TIMEZONE):%Y-%m-%d %H:%M %Z}"
            )
            return existing

    login = perform_login
    if login is None:
        # Credentials are loaded ONCE, outside the retry loop and before it: a missing or
        # malformed credential is a configuration fault, and retrying it three times only
        # delays the report by a minute without any chance of a different answer.
        api_credentials = load_broker_api_credentials(BrokerName.ZERODHA_KITE)
        login_credentials = load_kite_login_credentials()

        def login(token_store_to_write: KiteAccessTokenFileStore) -> KiteAccessTokenRecord:
            return generate_and_store_daily_kite_access_token(
                api_credentials, login_credentials, token_store_to_write
            )

    last_failure: Exception | None = None
    for attempt_number in range(1, login_attempts + 1):
        try:
            record = login(store)
        except Exception as failure:  # noqa: BLE001 — every failure shape gets a retry
            last_failure = failure
            report_line(
                f"attempt {attempt_number}/{login_attempts} failed: "
                f"{type(failure).__name__}: {failure}"
            )
            if attempt_number < login_attempts:
                sleep_between_attempts(seconds_between_attempts)
            continue
        report_line(
            f"new token stored — user {record.kite_user_id}, "
            f"expires {record.expires_at().astimezone(INDIA_TIMEZONE):%Y-%m-%d %H:%M %Z} "
            f"(attempt {attempt_number}/{login_attempts})"
        )
        return record

    raise KiteAccessTokenRefreshError(
        f"all {login_attempts} Kite login attempts failed; "
        f"no broker session for {datetime.now(INDIA_TIMEZONE):%Y-%m-%d}"
    ) from last_failure


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nse_algo_trader.broker_sessions.refresh_kite_access_token",
        description=(
            "Ensure a valid daily Kite access token exists. Idempotent: a still-valid "
            "token is reported and kept, so running this twice a morning is safe."
        ),
    )
    parser.add_argument(
        "--force-new-login",
        action="store_true",
        help="log in again even if the stored token is still valid",
    )
    parser.add_argument(
        "--login-attempts",
        type=int,
        default=DEFAULT_LOGIN_ATTEMPTS,
        help=f"login attempts before giving up (default {DEFAULT_LOGIN_ATTEMPTS})",
    )
    parser.add_argument(
        "--seconds-between-attempts",
        type=float,
        default=DEFAULT_SECONDS_BETWEEN_ATTEMPTS,
        help=f"pause between attempts (default {DEFAULT_SECONDS_BETWEEN_ATTEMPTS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    stamp = f"[{datetime.now(INDIA_TIMEZONE):%Y-%m-%d %H:%M:%S IST}]"
    try:
        refresh_kite_access_token_if_needed(
            login_attempts=arguments.login_attempts,
            seconds_between_attempts=arguments.seconds_between_attempts,
            force_new_login=arguments.force_new_login,
            report_line=lambda line: print(f"{stamp} {line}", flush=True),
        )
    except KiteAccessTokenRefreshError as failure:
        print(f"{stamp} REFRESH FAILED: {failure}", file=sys.stderr, flush=True)
        cause = failure.__cause__
        if cause is not None:
            print(f"{stamp}   cause: {type(cause).__name__}: {cause}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
