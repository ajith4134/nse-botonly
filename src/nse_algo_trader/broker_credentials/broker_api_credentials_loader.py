"""Loads broker API credentials from the environment / gitignored .env file.

The only module allowed to know credential env-var names. Secrets are
never hardcoded, never committed (`.env` is gitignored), and never
logged — `BrokerApiCredentials.__repr__` masks them.
"""

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv


class BrokerName(StrEnum):
    """Every broker this project can hold credentials for (data or execution)."""

    ZERODHA_KITE = "zerodha_kite"
    UPSTOX = "upstox"
    ANGEL_ONE = "angel_one"
    ICICI_BREEZE = "icici_breeze"
    GROWW = "groww"


_ENV_VAR_PREFIX_BY_BROKER: dict[BrokerName, str] = {
    BrokerName.ZERODHA_KITE: "ZERODHA_KITE",
    BrokerName.UPSTOX: "UPSTOX",
    BrokerName.ANGEL_ONE: "ANGEL_ONE",
    BrokerName.ICICI_BREEZE: "ICICI_BREEZE",
    BrokerName.GROWW: "GROWW",
}


class MissingBrokerCredentialsError(Exception):
    """Raised when a required credential env var is absent or empty."""


@dataclass(frozen=True)
class BrokerApiCredentials:
    broker_name: BrokerName
    api_key: str
    api_secret: str | None  # None when the broker's secret hasn't been provided yet

    def __repr__(self) -> str:  # never leak secrets into logs/tracebacks
        return (
            f"BrokerApiCredentials(broker_name={self.broker_name.value!r}, "
            f"api_key='***', api_secret={'***' if self.api_secret else None})"
        )


def load_env_file_into_environ(env_file_path: Path | None = None) -> None:
    """Loads the project .env into os.environ (existing vars win)."""
    load_dotenv(dotenv_path=env_file_path, override=False)


def load_broker_api_credentials(
    broker_name: BrokerName, environ: dict[str, str] | None = None
) -> BrokerApiCredentials:
    environ_to_read = os.environ if environ is None else environ
    env_var_prefix = _ENV_VAR_PREFIX_BY_BROKER[broker_name]
    api_key_env_var = f"{env_var_prefix}_API_KEY"
    api_key = environ_to_read.get(api_key_env_var, "").strip()
    if not api_key:
        raise MissingBrokerCredentialsError(
            f"{api_key_env_var} is not set — add it to the project's .env "
            f"(gitignored) or the process environment"
        )
    api_secret = environ_to_read.get(f"{env_var_prefix}_API_SECRET", "").strip()
    return BrokerApiCredentials(
        broker_name=broker_name,
        api_key=api_key,
        api_secret=api_secret or None,
    )
