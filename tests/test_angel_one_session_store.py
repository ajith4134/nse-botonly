"""`L3.13` — the Angel One session cache, and the four ways a token store lies.

The store's whole job is to answer "may I reuse this token?" without asking Angel. Every
test here is a way that answer can be wrong: a stale token presented as fresh, a corrupt
file presented as a first run, a refused login cached as a success, and a token leaked
into a log line because the dataclass printed itself.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nse_algo_trader.broker_sessions.angel_one_session_store import (
    IST,
    AngelOneSessionError,
    AngelOneSessionFileStore,
    AngelOneSessionRecord,
    session_record_from_login,
)

EXCHANGE_DAY = date(2026, 8, 11)


def _record(day: date = EXCHANGE_DAY) -> AngelOneSessionRecord:
    return AngelOneSessionRecord(
        jwt_token="jwt-abc",
        refresh_token="refresh-abc",
        feed_token="feed-abc",
        generated_on=day,
    )


def _store(tmp_path: Path) -> AngelOneSessionFileStore:
    return AngelOneSessionFileStore(tmp_path / "angel_one_session.json")


@pytest.mark.unit
def test_round_trip_returns_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store(_record())
    loaded = store.load()
    assert loaded == _record()


@pytest.mark.unit
def test_absent_file_is_no_session_not_an_error(tmp_path: Path) -> None:
    assert _store(tmp_path).load() is None
    assert _store(tmp_path).load_for_today(today=EXCHANGE_DAY) is None


@pytest.mark.unit
def test_stored_token_file_is_owner_only(tmp_path: Path) -> None:
    """A JWT is a credential: group- or world-readable is the same as published."""
    store = _store(tmp_path)
    store.store(_record())
    mode = stat.S_IMODE((tmp_path / "angel_one_session.json").stat().st_mode)
    assert mode == 0o600, f"session file is {oct(mode)}, not owner-only"


@pytest.mark.unit
def test_token_from_a_previous_exchange_day_is_not_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store(_record(EXCHANGE_DAY - timedelta(days=1)))
    assert store.load() is not None, "the record is still readable"
    assert store.load_for_today(today=EXCHANGE_DAY) is None, "but must not be presented"


@pytest.mark.unit
def test_token_from_the_same_exchange_day_is_offered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store(_record())
    assert store.load_for_today(today=EXCHANGE_DAY) == _record()


@pytest.mark.unit
def test_exchange_day_defaults_to_ist_not_the_host_clock(tmp_path: Path) -> None:
    """A UTC host rolls over at 05:30 IST — inside the pre-open window.

    Dating the cache by the server day would discard the token every morning at exactly
    the moment the run needs it, so the default must be the IST date.
    """
    store = _store(tmp_path)
    store.store(_record(datetime.now(IST).date()))
    assert store.load_for_today() is not None


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "corrupt_body",
    [
        "{not json at all",
        json.dumps({"jwt_token": "a", "refresh_token": "b"}),  # feed_token/date missing
        json.dumps(
            {
                "jwt_token": "a",
                "refresh_token": "b",
                "feed_token": "c",
                "generated_on": "not-a-date",
            }
        ),
        "",
    ],
    ids=["unparseable", "missing-keys", "bad-date", "empty"],
)
def test_a_corrupt_cache_raises_rather_than_impersonating_a_first_run(
    tmp_path: Path, corrupt_body: str
) -> None:
    """Returning None here would make a permanently broken file look like a fresh install.

    The runner catches this and falls back to a real login, which is the right recovery —
    but it recovers from a NAMED failure, not from silence.
    """
    session_file = tmp_path / "angel_one_session.json"
    session_file.write_text(corrupt_body)
    with pytest.raises(AngelOneSessionError, match="unreadable"):
        AngelOneSessionFileStore(session_file).load()


@pytest.mark.adversarial
def test_a_refused_login_is_never_turned_into_a_record() -> None:
    """Angel signals failure with `status: false` inside an HTTP 200.

    Caching that would poison every later run of the day with a token that never worked.
    """
    with pytest.raises(AngelOneSessionError, match="refused"):
        session_record_from_login({"status": False, "message": "Invalid totp"})


@pytest.mark.adversarial
def test_a_success_with_no_token_is_refused() -> None:
    """`status: true` with an empty payload has been observed on other Angel endpoints."""
    with pytest.raises(AngelOneSessionError, match="no jwtToken"):
        session_record_from_login({"status": True, "data": {}})

    with pytest.raises(AngelOneSessionError, match="no jwtToken"):
        session_record_from_login({"status": True, "data": None})


@pytest.mark.unit
def test_login_response_maps_to_the_real_smartapi_shape() -> None:
    record = session_record_from_login(
        {
            "status": True,
            "data": {
                "jwtToken": "jwt-live",
                "refreshToken": "refresh-live",
                "feedToken": "feed-live",
            },
        },
        today=EXCHANGE_DAY,
    )
    assert record == AngelOneSessionRecord(
        jwt_token="jwt-live",
        refresh_token="refresh-live",
        feed_token="feed-live",
        generated_on=EXCHANGE_DAY,
    )


@pytest.mark.unit
def test_feed_token_falls_back_to_the_separately_fetched_one() -> None:
    """Some SmartAPI versions omit feedToken from the login body and expose it separately."""
    record = session_record_from_login(
        {"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}},
        feed_token="feed-from-getfeedtoken",
        today=EXCHANGE_DAY,
    )
    assert record.feed_token == "feed-from-getfeedtoken"


@pytest.mark.adversarial
def test_the_bearer_prefix_angel_returns_is_stripped_before_storage() -> None:
    """The defect that made the first cached client a silent dud.

    `generateSession` strips `"Bearer "` before assigning `self.access_token`, but the
    login body it returns keeps it. Storing the raw field and handing it back as
    `access_token=` builds `Authorization: Bearer Bearer eyJ…` — Angel answers `AG8001
    Invalid Token`, and nothing fails until the client is actually used.
    """
    record = session_record_from_login(
        {
            "status": True,
            "data": {
                "jwtToken": "Bearer eyJ-jwt",
                "refreshToken": "Bearer eyJ-refresh",
                "feedToken": "feed",
            },
        },
        today=EXCHANGE_DAY,
    )
    assert record.jwt_token == "eyJ-jwt"
    assert record.refresh_token == "eyJ-refresh"


@pytest.mark.adversarial
def test_a_cache_written_by_the_broken_version_is_repaired_on_read(tmp_path: Path) -> None:
    """Normalisation lives on the dataclass, so it covers the load path too."""
    session_file = tmp_path / "angel_one_session.json"
    session_file.write_text(
        json.dumps(
            {
                "jwt_token": "Bearer eyJ-jwt",
                "refresh_token": "bearer  eyJ-refresh",
                "feed_token": "feed",
                "generated_on": EXCHANGE_DAY.isoformat(),
            }
        )
    )
    loaded = AngelOneSessionFileStore(session_file).load()
    assert loaded is not None
    assert loaded.jwt_token == "eyJ-jwt"
    assert loaded.refresh_token == "eyJ-refresh", "case and padding both handled"


@pytest.mark.adversarial
def test_a_bare_token_survives_normalisation_unchanged() -> None:
    """Idempotence: the fix must not corrupt the tokens it was not written for."""
    bare = "eyJhbGciOiJIUzUxMiJ9.payload.signature"
    assert AngelOneSessionRecord(bare, bare, "f", EXCHANGE_DAY).jwt_token == bare


@pytest.mark.adversarial
def test_no_token_ever_appears_in_a_repr() -> None:
    """A default dataclass repr puts all three secrets into any traceback or log line."""
    printed = repr(_record())
    for secret in ("jwt-abc", "refresh-abc", "feed-abc"):
        assert secret not in printed, f"{secret} leaked via __repr__"
    assert repr(EXCHANGE_DAY) in printed, "the non-secret field stays useful for debugging"


@pytest.mark.real_data
@pytest.mark.filterwarnings("default::DeprecationWarning")
@pytest.mark.skipif(
    not os.environ.get("ANGEL_ONE_API_KEY") and not Path(".env").exists(),
    reason="Angel One credentials are not present on this host",
)
def test_a_rehydrated_client_can_actually_talk_to_angel() -> None:
    """R.05 — the only test that would have caught the `Bearer Bearer` defect.

    Everything above proves the STORE is correct. This proves the thing the store exists
    to produce is USABLE: rehydrate from the cache with no login, then make a real
    authenticated call. Constructing a `SmartConnect` never fails, so a cached client that
    cannot fetch anything is indistinguishable from a working one until it is used.

    The `filterwarnings` mark is load-bearing, and finding out why cost a wrong diagnosis:
    `SmartConnect.__init__` sets the deprecated `ssl.OP_NO_TLSv1`, and this project's
    `filterwarnings = ["error::DeprecationWarning"]` turns that into an exception that the
    builder's blanket `except Exception` reads as "Angel will not talk to us". Under the
    suite's default filters this test skips itself with a message blaming the broker.
    """
    pytest.importorskip("SmartApi")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_daily_operations import _build_angel_one_client

    client = _build_angel_one_client()
    if client is None:
        pytest.skip("Angel One would not issue a session on this host right now")

    cached = AngelOneSessionFileStore().load_for_today()
    assert cached is not None, "a successful build must leave a same-day cache behind"
    assert not cached.jwt_token.lower().startswith("bearer"), "stored token must be bare"

    rehydrated = _build_angel_one_client()  # second call: cache hit, no login
    assert rehydrated is not None
    profile = rehydrated.getProfile(cached.refresh_token)
    assert profile.get("status") is True, f"rehydrated client refused: {profile}"
    assert (profile.get("data") or {}).get("name"), "authenticated call returned no identity"


@pytest.mark.adversarial
def test_rewriting_an_existing_session_leaves_no_stale_bytes(tmp_path: Path) -> None:
    """A shorter second token must not leave the tail of a longer first one behind."""
    store = _store(tmp_path)
    store.store(AngelOneSessionRecord("j" * 400, "r" * 400, "f" * 400, EXCHANGE_DAY))
    store.store(_record())
    assert store.load() == _record()
    assert "jjjj" not in (tmp_path / "angel_one_session.json").read_text()
