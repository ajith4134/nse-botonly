"""Daily broker session/token management (Kite first; other brokers later)."""

from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
    build_authenticated_kite_client_if_valid,
)
from nse_algo_trader.broker_sessions.kite_access_token_store import (
    KiteAccessTokenFileStore,
    KiteAccessTokenRecord,
)
from nse_algo_trader.broker_sessions.kite_totp_auto_login import (
    KiteAutoLoginError,
    fetch_kite_request_token_via_totp_login,
    generate_and_store_daily_kite_access_token,
)

# `refresh_kite_access_token` is deliberately NOT re-exported here. It is a `python -m`
# entry point run from cron, and importing it into the package __init__ makes runpy warn
# that the module was already in sys.modules before execution — a real double-import, not
# a cosmetic warning. Import the submodule directly if you need its function.

__all__ = [
    "KiteAccessTokenFileStore",
    "KiteAccessTokenRecord",
    "KiteAutoLoginError",
    "build_authenticated_kite_client_if_valid",
    "fetch_kite_request_token_via_totp_login",
    "generate_and_store_daily_kite_access_token",
]
