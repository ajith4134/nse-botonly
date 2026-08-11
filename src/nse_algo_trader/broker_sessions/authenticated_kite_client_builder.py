"""Builds a live-authenticated KiteConnect client — the only place outside the
market-data adapters that touches `kiteconnect` for a trading client.

Kite is a bounded execution/live-feed adapter: nothing outside `broker_*` and the
market-data adapters may import `kiteconnect`. The dashboard and every analysis
feature must run with NO Kite session; only live/paper trading reaches for a broker
client, and it does so through THIS function so the dependency stays isolated.

Returns None when there is no valid daily token — the caller then runs on stored data.
The `kiteconnect` import is function-local so importing this module never loads the SDK.
"""

from __future__ import annotations

from typing import Any


def build_authenticated_kite_client_if_valid() -> Any | None:
    """A live-authenticated `KiteConnect`, or None when no valid daily token exists,
    in which case the caller degrades to stored-data mode. Typed `Any` so callers and
    tests need not import the SDK to reference it."""
    from kiteconnect import KiteConnect

    from nse_algo_trader.broker_credentials import (
        BrokerName,
        load_broker_api_credentials,
        load_env_file_into_environ,
    )
    from nse_algo_trader.broker_sessions.kite_access_token_store import (
        KiteAccessTokenFileStore,
    )

    load_env_file_into_environ()
    token_record = KiteAccessTokenFileStore().load_if_still_valid()
    if token_record is None:
        return None
    credentials = load_broker_api_credentials(BrokerName.ZERODHA_KITE)
    kite_client = KiteConnect(api_key=credentials.api_key)
    kite_client.set_access_token(token_record.access_token)
    return kite_client
