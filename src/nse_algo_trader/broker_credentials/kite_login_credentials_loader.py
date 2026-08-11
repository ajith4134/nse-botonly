"""Loads the Zerodha Kite interactive-login credentials (user id, password,
TOTP secret) used by the fully-automatic daily token refresh.

Separate from `broker_api_credentials_loader` because these are login
credentials, not API credentials — and only the Kite TOTP auto-login is
allowed to touch them. Secrets masked in repr, same as everywhere else.
"""

import os
from dataclasses import dataclass

from nse_algo_trader.broker_credentials.broker_api_credentials_loader import (
    MissingBrokerCredentialsError,
)

_KITE_USER_ID_ENV_VAR = "ZERODHA_KITE_USER_ID"
_KITE_PASSWORD_ENV_VAR = "ZERODHA_KITE_PASSWORD"  # noqa: S105 — the variable NAME, not a secret
_KITE_TOTP_SECRET_ENV_VAR = "ZERODHA_KITE_TOTP_SECRET"  # noqa: S105 — the variable NAME, not a secret


@dataclass(frozen=True)
class KiteLoginCredentials:
    kite_user_id: str
    kite_password: str
    kite_totp_secret: str  # base32 secret behind the authenticator app

    def __repr__(self) -> str:  # never leak password/TOTP secret
        return (
            f"KiteLoginCredentials(kite_user_id={self.kite_user_id!r}, "
            f"kite_password='***', kite_totp_secret='***')"
        )


def load_kite_login_credentials(
    environ: dict[str, str] | None = None,
) -> KiteLoginCredentials:
    environ_to_read = os.environ if environ is None else environ
    missing_env_vars = [
        env_var
        for env_var in (
            _KITE_USER_ID_ENV_VAR,
            _KITE_PASSWORD_ENV_VAR,
            _KITE_TOTP_SECRET_ENV_VAR,
        )
        if not environ_to_read.get(env_var, "").strip()
    ]
    if missing_env_vars:
        raise MissingBrokerCredentialsError(
            f"{', '.join(missing_env_vars)} not set — add to the project's "
            f"gitignored .env to enable automatic Kite login"
        )
    return KiteLoginCredentials(
        kite_user_id=environ_to_read[_KITE_USER_ID_ENV_VAR].strip(),
        kite_password=environ_to_read[_KITE_PASSWORD_ENV_VAR].strip(),
        kite_totp_secret=environ_to_read[_KITE_TOTP_SECRET_ENV_VAR].strip(),
    )
