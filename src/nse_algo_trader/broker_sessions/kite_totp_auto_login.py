"""Fully-automatic daily Kite login: user id + password + TOTP -> access token.

Replays the exact browser login sequence headlessly (verified against
current community implementations, 2026-07):

1. GET  connect/login?api_key=...        -> seeds session cookies
2. POST api/login  (user_id, password)   -> request_id
3. POST api/twofa  (request_id, TOTP)    -> authenticated web session
4. GET  connect/login again, follow redirects manually and pull
   request_token out of the redirect Location header — the registered
   redirect URL is never actually visited, so it needs no live server.
5. Exchange request_token + api_secret for the day's access token.

No browser, no human, no open ports. Scheduled pre-market via cron.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import pyotp
import requests

from nse_algo_trader.broker_credentials import BrokerApiCredentials
from nse_algo_trader.broker_credentials.kite_login_credentials_loader import (
    KiteLoginCredentials,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    INDIA_MARKET_TIMEZONE,
    KiteAccessTokenFileStore,
    KiteAccessTokenRecord,
)

KITE_CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"
KITE_WEB_LOGIN_API_URL = "https://kite.zerodha.com/api/login"
KITE_WEB_TWOFA_API_URL = "https://kite.zerodha.com/api/twofa"

HTTP_OK = 200
"""A protocol constant, not a tunable."""

_MAX_REDIRECTS_TO_FOLLOW = 8
_REQUEST_TIMEOUT_SECONDS = 30


class KiteAutoLoginError(Exception):
    """Raised when any step of the automatic Kite login fails."""


def fetch_kite_request_token_via_totp_login(
    kite_api_key: str,
    kite_login_credentials: KiteLoginCredentials,
    http_session: requests.Session | None = None,
) -> str:
    session = http_session or requests.Session()
    connect_login_url = f"{KITE_CONNECT_LOGIN_URL}?v=3&api_key={kite_api_key}"

    session.get(connect_login_url, timeout=_REQUEST_TIMEOUT_SECONDS)

    login_response = session.post(
        KITE_WEB_LOGIN_API_URL,
        data={
            "user_id": kite_login_credentials.kite_user_id,
            "password": kite_login_credentials.kite_password,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    login_payload = login_response.json()
    if login_response.status_code != HTTP_OK:
        raise KiteAutoLoginError(
            f"Kite user-id/password step failed: {login_payload.get('message')}"
        )

    current_totp_code = pyotp.TOTP(kite_login_credentials.kite_totp_secret).now()
    twofa_response = session.post(
        KITE_WEB_TWOFA_API_URL,
        data={
            "user_id": kite_login_credentials.kite_user_id,
            "request_id": login_payload["data"]["request_id"],
            "twofa_value": current_totp_code,
            "twofa_type": "totp",
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if twofa_response.status_code != HTTP_OK:
        raise KiteAutoLoginError(
            f"Kite TOTP step failed: {twofa_response.json().get('message')}"
        )

    # Authenticated now — re-hitting connect/login redirects toward the
    # registered redirect URL carrying request_token. Follow Location
    # headers manually and stop BEFORE visiting the redirect URL itself.
    next_url_to_follow = f"{connect_login_url}&skip_session=true"
    for _ in range(_MAX_REDIRECTS_TO_FOLLOW):
        redirect_response = session.get(
            next_url_to_follow,
            allow_redirects=False,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        redirect_location = redirect_response.headers.get("location", "")
        request_token_values = parse_qs(
            urlparse(redirect_location).query
        ).get("request_token")
        if request_token_values:
            return request_token_values[0]
        if not redirect_location:
            raise KiteAutoLoginError(
                "Kite login succeeded but no request_token redirect appeared "
                f"(stopped at HTTP {redirect_response.status_code})"
            )
        next_url_to_follow = urljoin(next_url_to_follow, redirect_location)
    raise KiteAutoLoginError(
        f"request_token not found within {_MAX_REDIRECTS_TO_FOLLOW} redirects"
    )


def generate_and_store_daily_kite_access_token(
    kite_api_credentials: BrokerApiCredentials,
    kite_login_credentials: KiteLoginCredentials,
    token_store: KiteAccessTokenFileStore,
    authenticated_session_generator: Callable[..., dict[str, Any]] | None = None,
    http_session: requests.Session | None = None,
) -> KiteAccessTokenRecord:
    """The daily refresh: auto-login -> token exchange -> persist. Returns
    the stored record. `authenticated_session_generator` defaults to the
    real KiteConnect.generate_session; injectable for tests."""
    request_token = fetch_kite_request_token_via_totp_login(
        kite_api_credentials.api_key, kite_login_credentials, http_session
    )
    if authenticated_session_generator is None:
        from kiteconnect import KiteConnect

        kite_client = KiteConnect(api_key=kite_api_credentials.api_key)
        authenticated_session_generator = kite_client.generate_session
    session_payload = authenticated_session_generator(
        request_token, api_secret=kite_api_credentials.api_secret
    )
    token_record = KiteAccessTokenRecord(
        access_token=session_payload["access_token"],
        kite_user_id=session_payload["user_id"],
        generated_at=datetime.now(INDIA_MARKET_TIMEZONE),
    )
    token_store.save(token_record)
    return token_record
