"""`L3.10` / `L3.11` — the automatic Kite login, the token it stores, and the client built
from that token.

This is the credential path for the ONLY execution broker, and until now it had no tests.
`A.74` is the reason it has them: the Angel session store round-tripped its token perfectly
and still produced clients that could not authenticate, because constructing a broker
client validates nothing. So the last test here is the one that matters — it builds a
client from the stored token and makes a real authenticated call.

The login itself is exercised through the `http_session` DI seam (`R.05` hermetic), which
is functional verification only: the real login runs pre-market from the scheduler, and no
test may spend a live login on a schedule.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp
import pytest

from nse_algo_trader.broker_credentials import BrokerApiCredentials, BrokerName
from nse_algo_trader.broker_credentials.kite_login_credentials_loader import (
    KiteLoginCredentials,
)
from nse_algo_trader.broker_sessions import kite_access_token_store
from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    INDIA_MARKET_TIMEZONE,
    KiteAccessTokenFileStore,
    KiteAccessTokenRecord,
)
from nse_algo_trader.broker_sessions.kite_totp_auto_login import (
    KiteAutoLoginError,
    fetch_kite_request_token_via_totp_login,
    generate_and_store_daily_kite_access_token,
)

TOTP_SECRET = "JBSWY3DPEHPK3PXP"
API_KEY = "api-key"
LOGIN_CREDENTIALS = KiteLoginCredentials("AB1234", "pw", TOTP_SECRET)
API_CREDENTIALS = BrokerApiCredentials(BrokerName.ZERODHA_KITE, API_KEY, "api-secret")


# ------------------------------------------------------------------ the DI seam


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return self.payload


@dataclass
class FakeHttpSession:
    """A scripted stand-in for `requests.Session` that records what was sent.

    Recording matters as much as replying: the TOTP code is computed inside the module
    under test, so the only way to prove it was derived from the secret rather than from
    anything else is to read what was posted.
    """

    post_responses: list[FakeResponse] = field(default_factory=list)
    get_responses: list[FakeResponse] = field(default_factory=list)
    posted: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.visited.append(url)
        if not self.get_responses:
            return FakeResponse()
        return self.get_responses.pop(0)

    def post(self, url: str, data: dict[str, Any], **_: Any) -> FakeResponse:
        self.posted.append((url, data))
        return self.post_responses.pop(0)


def _successful_login_posts() -> list[FakeResponse]:
    return [
        FakeResponse(payload={"data": {"request_id": "req-1"}}),  # user id + password
        FakeResponse(payload={"data": {}}),  # twofa
    ]


# ------------------------------------------------------------------- L3.10 login


@pytest.mark.hermetic
def test_request_token_is_pulled_from_the_redirect_without_visiting_it() -> None:
    """The registered redirect URL is never fetched, so no live server is needed.

    Visiting it would require a listener on a public URL — the whole reason this login
    stops at the `Location` header instead of following it.
    """
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[
            FakeResponse(),  # the cookie-seeding GET
            FakeResponse(
                status_code=302,
                headers={"location": "https://example.invalid/cb?request_token=tok-9"},
            ),
        ],
    )
    token = fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]
    assert token == "tok-9"
    assert not any("example.invalid" in url for url in session.visited)


@pytest.mark.hermetic
def test_the_posted_twofa_code_is_derived_from_the_configured_secret() -> None:
    """A login that posts a code from anywhere else fails only at 6am, unattended."""
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[
            FakeResponse(),
            FakeResponse(headers={"location": "https://x.invalid/?request_token=t"}),
        ],
    )
    fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]
    _, twofa_body = session.posted[1]
    assert twofa_body["twofa_value"] in {
        pyotp.TOTP(TOTP_SECRET).at(datetime.now(UTC) + timedelta(seconds=offset))
        for offset in (-30, 0, 30)
    }, "the code was not generated from the configured TOTP secret"
    assert twofa_body["request_id"] == "req-1", "the request_id must come from step 2"
    assert twofa_body["twofa_type"] == "totp"


@pytest.mark.adversarial
def test_a_rejected_password_names_the_step_that_failed() -> None:
    session = FakeHttpSession(
        post_responses=[FakeResponse(status_code=403, payload={"message": "bad creds"})]
    )
    with pytest.raises(KiteAutoLoginError, match="user-id/password"):
        fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_a_rejected_totp_names_the_step_that_failed() -> None:
    """Distinguishable from the password failure: one means the clock, one means the vault."""
    session = FakeHttpSession(
        post_responses=[
            FakeResponse(payload={"data": {"request_id": "req-1"}}),
            FakeResponse(status_code=403, payload={"message": "wrong totp"}),
        ]
    )
    with pytest.raises(KiteAutoLoginError, match="TOTP step"):
        fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_an_authenticated_session_with_no_redirect_is_an_error_not_an_empty_token() -> None:
    """Kite answering 200 with no `Location` means the flow changed under us."""
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[FakeResponse(), FakeResponse(status_code=200, headers={})],
    )
    with pytest.raises(KiteAutoLoginError, match="no request_token redirect"):
        fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_a_redirect_loop_terminates_instead_of_hanging_the_scheduler() -> None:
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[FakeResponse()]
        + [FakeResponse(status_code=302, headers={"location": "/again"})] * 20,
    )
    with pytest.raises(KiteAutoLoginError, match="within 8 redirects"):
        fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_a_relative_location_header_is_resolved_against_the_current_url() -> None:
    """Kite has used relative redirects; treating one as absolute loses the token."""
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[
            FakeResponse(),
            FakeResponse(status_code=302, headers={"location": "/finish"}),
            FakeResponse(status_code=302, headers={"location": "?request_token=tok-rel"}),
        ],
    )
    assert (
        fetch_kite_request_token_via_totp_login(API_KEY, LOGIN_CREDENTIALS, session)  # type: ignore[arg-type]
        == "tok-rel"
    )
    assert session.visited[1].startswith("https://kite.zerodha.com/")


@pytest.mark.hermetic
def test_the_daily_refresh_stores_what_the_exchange_returned(tmp_path: Path) -> None:
    session = FakeHttpSession(
        post_responses=_successful_login_posts(),
        get_responses=[
            FakeResponse(),
            FakeResponse(headers={"location": "https://x.invalid/?request_token=tok"}),
        ],
    )
    exchanged: dict[str, Any] = {}

    def fake_generate_session(request_token: str, api_secret: str) -> dict[str, Any]:
        exchanged["request_token"] = request_token
        exchanged["api_secret"] = api_secret
        return {"access_token": "access-abc", "user_id": "AB1234"}

    store = KiteAccessTokenFileStore(tmp_path / "kite_access_token.json")
    record = generate_and_store_daily_kite_access_token(
        API_CREDENTIALS,
        LOGIN_CREDENTIALS,
        store,
        authenticated_session_generator=fake_generate_session,
        http_session=session,  # type: ignore[arg-type]
    )

    assert exchanged == {"request_token": "tok", "api_secret": "api-secret"}
    assert record.access_token == "access-abc"
    assert store.load() == record, "the returned record is the one on disk"
    mode = stat.S_IMODE((tmp_path / "kite_access_token.json").stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, not owner-only"


# --------------------------------------------------------------- L3.11 token store


@pytest.mark.unit
def test_a_token_minted_before_6am_dies_at_6am_the_same_day() -> None:
    record = KiteAccessTokenRecord(
        "tok", "AB1234", datetime(2026, 8, 12, 5, 30, tzinfo=INDIA_MARKET_TIMEZONE)
    )
    assert record.expires_at() == datetime(2026, 8, 12, 6, 0, tzinfo=INDIA_MARKET_TIMEZONE)


@pytest.mark.unit
def test_a_token_minted_after_6am_survives_until_6am_tomorrow() -> None:
    """The normal case: the scheduler mints at ~09:00 IST for that session."""
    record = KiteAccessTokenRecord(
        "tok", "AB1234", datetime(2026, 8, 12, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE)
    )
    assert record.expires_at() == datetime(2026, 8, 13, 6, 0, tzinfo=INDIA_MARKET_TIMEZONE)


@pytest.mark.adversarial
def test_validity_is_decided_in_ist_even_when_asked_in_utc() -> None:
    """A UTC host asking "is this valid?" must not get a different answer.

    05:30 UTC is 11:00 IST — the middle of the session — and a naive comparison against
    a 6:00 boundary would call a live token expired.
    """
    record = KiteAccessTokenRecord(
        "tok", "AB1234", datetime(2026, 8, 12, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE)
    )
    assert record.is_still_valid(datetime(2026, 8, 12, 5, 30, tzinfo=UTC))
    assert not record.is_still_valid(datetime(2026, 8, 13, 1, 0, tzinfo=UTC))


@pytest.mark.adversarial
def test_the_expiry_boundary_is_exclusive_at_exactly_6am() -> None:
    record = KiteAccessTokenRecord(
        "tok", "AB1234", datetime(2026, 8, 12, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE)
    )
    one_second_before = datetime(2026, 8, 13, 5, 59, 59, tzinfo=INDIA_MARKET_TIMEZONE)
    assert record.is_still_valid(one_second_before)
    assert not record.is_still_valid(one_second_before + timedelta(seconds=1))


@pytest.mark.unit
def test_an_expired_token_is_withheld_rather_than_returned(tmp_path: Path) -> None:
    store = KiteAccessTokenFileStore(tmp_path / "token.json")
    store.save(
        KiteAccessTokenRecord(
            "tok", "AB1234", datetime(2026, 8, 10, 9, 0, tzinfo=INDIA_MARKET_TIMEZONE)
        )
    )
    assert store.load() is not None, "still readable"
    assert store.load_if_still_valid() is None, "but not offered to a caller"


@pytest.mark.unit
def test_no_saved_token_is_none_not_an_error(tmp_path: Path) -> None:
    assert KiteAccessTokenFileStore(tmp_path / "absent.json").load() is None
    assert KiteAccessTokenFileStore(tmp_path / "absent.json").load_if_still_valid() is None


@pytest.mark.adversarial
def test_a_corrupt_token_file_raises_rather_than_reading_as_a_first_run(
    tmp_path: Path,
) -> None:
    """DOCUMENTS current behaviour, which differs from the Angel store on purpose.

    Angel wraps this in a named `AngelOneSessionError` because its caller recovers with a
    fresh login. Kite has no such recovery inside the process — the token is minted by the
    pre-market scheduler — so a corrupt file must be loud rather than quietly equal to
    "not logged in yet", which would silently degrade every consumer to stored-data mode.
    """
    token_file = tmp_path / "token.json"
    token_file.write_text("{ truncated")
    with pytest.raises(json.JSONDecodeError):
        KiteAccessTokenFileStore(token_file).load()

    token_file.write_text(json.dumps({"access_token": "t"}))
    with pytest.raises(KeyError):
        KiteAccessTokenFileStore(token_file).load()


@pytest.mark.adversarial
def test_the_stored_token_never_appears_in_a_repr() -> None:
    printed = repr(KiteAccessTokenRecord("live-access-token", "AB1234", datetime.now(UTC)))
    assert "live-access-token" not in printed
    assert "AB1234" in printed


# ------------------------------------------------- L3.11 client builder


@pytest.mark.unit
def test_no_valid_token_means_no_client_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every analysis feature must run with no Kite session at all (`A.25`).

    Raising here would couple the dashboard and the research path to broker availability.
    """
    monkeypatch.setattr(
        kite_access_token_store.KiteAccessTokenFileStore,
        "load_if_still_valid",
        lambda self, now=None: None,
    )
    assert build_authenticated_kite_client_if_valid() is None


@pytest.mark.real_data
@pytest.mark.skipif(
    not KiteAccessTokenFileStore().load_if_still_valid(),
    reason="no valid Kite access token on this host right now",
)
def test_a_client_built_from_the_stored_token_can_actually_talk_to_kite() -> None:
    """R.05 — the test the Angel path did not have, and paid for (`A.74`, `O.52`).

    `KiteConnect(...)` plus `set_access_token(...)` validates nothing: a client built from
    a malformed, stale or wrongly-prefixed token is indistinguishable from a working one
    until something asks it for data. `profile()` is the cheapest authenticated call.
    """
    client = build_authenticated_kite_client_if_valid()
    assert client is not None, "a valid stored token must yield a client"
    profile = client.profile()
    assert profile["user_id"], "authenticated call returned no identity"
    stored = KiteAccessTokenFileStore().load_if_still_valid()
    assert stored is not None
    assert profile["user_id"] == stored.kite_user_id, (
        "the client authenticated as a different account than the token records"
    )
