"""Angel One session tokens, cached so a nightly run does not re-login every call.

**Why caching, stated honestly.** The observation that prompted this was `generateSession`
returning None twice about thirty seconds apart, which was read at the time as Angel
throttling repeated logins. That reading was WRONG: the real cause was the daily runner
calling the builder before `.env` had been loaded, and a direct login succeeded immediately
once that was ruled out. Caching survives the correction on its own merits — the runner
wants a client once per component that needs one, and a login per component is waste — but
it is not a workaround for a limit Angel was never imposing.

The failure mode caching also softens is quiet in the worst way: without a client the
runner degrades to single-source reconciliation and records "angel one session could not
be generated", which reads as a broker outage rather than as this project's own defect.

**A token is a secret.** Written owner-only (`0o600`) into the state directory outside the
repository, never into the tree, and never logged — the same handling as the Kite token.

**Reuse is bounded by the EXCHANGE day, not by a duration.** Angel's tokens expire on its
own schedule and the honest thing to do is try the cached token and fall back to a fresh
login when it is rejected, rather than guess a lifetime. The stored date exists so a token
from a previous session is not presented at all, which saves a round trip that is certain
to fail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_SESSION_FILE = Path("~/.nse_algo_trader/angel_one_session.json").expanduser()


_BEARER_PREFIX = "bearer "


class AngelOneSessionError(RuntimeError):
    """A stored session could not be read or trusted."""


def _bare_token(raw_token: str) -> str:
    """Angel returns `jwtToken` as `"Bearer eyJ..."`; SmartConnect wants it without.

    MEASURED, and the reason the first cached client was a silent dud: `generateSession`
    strips this prefix before assigning `self.access_token`, but the login body it returns
    keeps it. Storing the raw field and handing it back as `access_token=` produced the
    header `Authorization: Bearer Bearer eyJ...`, and Angel answered `AG8001 Invalid Token`
    on the first real call. The client object still constructed fine — nothing failed until
    something asked it for data — so the cache looked healthy while being worthless.
    """
    stripped = raw_token.strip()
    if stripped.lower().startswith(_BEARER_PREFIX):
        return stripped[len(_BEARER_PREFIX) :].strip()
    return stripped


@dataclass(frozen=True)
class AngelOneSessionRecord:
    """One login's tokens, and the exchange day they were minted on."""

    jwt_token: str
    refresh_token: str
    feed_token: str
    generated_on: date

    def __post_init__(self) -> None:
        # Normalise on EVERY construction path, not just the login one, so a file written
        # by the version that stored the raw `"Bearer …"` field is repaired on read rather
        # than served as-is. `_bare_token` is idempotent, so a bare token is untouched.
        object.__setattr__(self, "jwt_token", _bare_token(self.jwt_token))
        object.__setattr__(self, "refresh_token", _bare_token(self.refresh_token))

    def __repr__(self) -> str:  # three secrets; a default dataclass repr leaks all of them
        return f"AngelOneSessionRecord(tokens='***', generated_on={self.generated_on!r})"

    def is_for_exchange_day(self, day: date) -> bool:
        return self.generated_on == day


class AngelOneSessionFileStore:
    """Reads and writes the cached Angel session."""

    def __init__(self, session_file_path: Path = DEFAULT_SESSION_FILE) -> None:
        self._session_file_path = session_file_path

    def store(self, record: AngelOneSessionRecord) -> None:
        self._session_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_file_path.write_text(
            json.dumps(
                {
                    "jwt_token": record.jwt_token,
                    "refresh_token": record.refresh_token,
                    "feed_token": record.feed_token,
                    "generated_on": record.generated_on.isoformat(),
                }
            )
        )
        self._session_file_path.chmod(0o600)  # owner-only: it's a secret

    def load(self) -> AngelOneSessionRecord | None:
        if not self._session_file_path.exists():
            return None
        try:
            payload = json.loads(self._session_file_path.read_text())
            return AngelOneSessionRecord(
                jwt_token=payload["jwt_token"],
                refresh_token=payload["refresh_token"],
                feed_token=payload["feed_token"],
                generated_on=date.fromisoformat(payload["generated_on"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as failure:
            # A corrupt cache must not be indistinguishable from no cache: returning None
            # silently would make a broken file look like a first run, forever.
            raise AngelOneSessionError(
                f"cached angel session at {self._session_file_path} is unreadable: {failure}"
            ) from failure

    def load_for_today(self, *, today: date | None = None) -> AngelOneSessionRecord | None:
        """A session minted on the current EXCHANGE day, or None.

        Exchange day, not server day: a UTC host rolls over at 05:30 IST, which is inside
        the pre-open window, so a UTC-dated cache would be discarded every morning exactly
        when the run needs it.
        """
        record = self.load()
        if record is None:
            return None
        return record if record.is_for_exchange_day(today or datetime.now(IST).date()) else None


def session_record_from_login(
    login_response: dict[str, Any], *, feed_token: str = "", today: date | None = None
) -> AngelOneSessionRecord:
    """Build a record from `generateSession`'s real response shape.

    Angel signals failure with `status: false` inside a 200, so the flag is checked rather
    than assumed — the same quirk the bar adapter guards against, and caching a failed
    login would poison every later run with a token that never worked.
    """
    if not login_response.get("status", False):
        raise AngelOneSessionError(
            f"angel login refused: {login_response.get('message', 'no message')}"
        )
    data = login_response.get("data") or {}
    jwt_token = _bare_token(str(data.get("jwtToken", "")))
    if not jwt_token:
        raise AngelOneSessionError("angel login returned no jwtToken")
    return AngelOneSessionRecord(
        jwt_token=jwt_token,
        refresh_token=_bare_token(str(data.get("refreshToken", ""))),
        feed_token=str(data.get("feedToken", "") or feed_token),
        generated_on=today or datetime.now(IST).date(),
    )
