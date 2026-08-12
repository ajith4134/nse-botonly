"""Persists the daily Kite access token to a gitignored file with expiry logic.

Kite access tokens die at ~6:00 AM IST the morning after generation, so
the record stores its generation time and answers "is this still usable
right now?" — consumers never guess.
"""

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
_KITE_TOKEN_EXPIRY_TIME_IST = time(6, 0)

DEFAULT_KITE_ACCESS_TOKEN_FILE_PATH = Path("~/.nse_algo_trader/kite_access_token.json").expanduser()


@dataclass(frozen=True)
class KiteAccessTokenRecord:
    access_token: str
    kite_user_id: str
    generated_at: datetime  # timezone-aware, IST

    def __repr__(self) -> str:  # the token is a secret; never leak it
        return (
            f"KiteAccessTokenRecord(access_token='***', "
            f"kite_user_id={self.kite_user_id!r}, generated_at={self.generated_at!r})"
        )

    def expires_at(self) -> datetime:
        """6:00 AM IST strictly after the generation moment."""
        generated_at_ist = self.generated_at.astimezone(INDIA_MARKET_TIMEZONE)
        same_day_expiry = datetime.combine(
            generated_at_ist.date(), _KITE_TOKEN_EXPIRY_TIME_IST, INDIA_MARKET_TIMEZONE
        )
        if generated_at_ist < same_day_expiry:
            return same_day_expiry
        return same_day_expiry + timedelta(days=1)

    def is_still_valid(self, now: datetime | None = None) -> bool:
        now_ist = (now or datetime.now(INDIA_MARKET_TIMEZONE)).astimezone(INDIA_MARKET_TIMEZONE)
        return now_ist < self.expires_at()


class KiteAccessTokenFileStore:
    def __init__(self, token_file_path: Path = DEFAULT_KITE_ACCESS_TOKEN_FILE_PATH):
        self._token_file_path = token_file_path

    def save(self, token_record: KiteAccessTokenRecord) -> None:
        self._token_file_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_file_path.write_text(
            json.dumps(
                {
                    "access_token": token_record.access_token,
                    "kite_user_id": token_record.kite_user_id,
                    "generated_at": token_record.generated_at.isoformat(),
                }
            )
        )
        self._token_file_path.chmod(0o600)  # owner-only: it's a secret

    def load(self) -> KiteAccessTokenRecord | None:
        """The saved record, or None if never saved."""
        if not self._token_file_path.exists():
            return None
        stored_fields = json.loads(self._token_file_path.read_text())
        return KiteAccessTokenRecord(
            access_token=stored_fields["access_token"],
            kite_user_id=stored_fields["kite_user_id"],
            generated_at=datetime.fromisoformat(stored_fields["generated_at"]),
        )

    def load_if_still_valid(self, now: datetime | None = None) -> KiteAccessTokenRecord | None:
        token_record = self.load()
        if token_record is None or not token_record.is_still_valid(now):
            return None
        return token_record
