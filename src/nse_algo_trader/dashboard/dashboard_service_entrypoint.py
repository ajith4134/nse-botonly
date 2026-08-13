"""The ASGI object systemd runs — and the ONE place the dashboard reads the project `.env`.

`uvicorn` is pointed at `dashboard_service_entrypoint:app`, not at `dashboard_server:app`, and the
split is the whole point of this module.

**Why the `.env` load is not in `build_dashboard_app`.** The server needs it: without it,
`NSE_TRADING_CAPITAL_RUPEES` sat correctly in `.env` and every surface reported it unset, which is
the worst shape a configuration gap takes — it looks done and behaves as though it is not. But
`load_env_file_into_environ()` loads the WHOLE file into `os.environ`, and that file holds roughly
fifty real credentials: `ZERODHA_KITE_API_SECRET`, `GROWW_ACCESS_TOKEN`, `UPSTOX_*`, `GITHUB_TOKEN`.
With the load inside the app factory, every test that built an app injected those into the test
process for the remainder of the session, and `monkeypatch.setenv` cannot undo a variable it did not
set — so a credential test could silently start passing against the operator's live secrets instead
of its fixture. The `R.23(c)` review confirmed the injection and confirmed nothing depends on it
today; this module makes sure nothing starts to.

Tests build the app through `build_dashboard_app()` and get a bare environment. The service imports
this module and gets the operator's configuration. Existing process variables still win
(`override=False`), so a systemd `Environment=` line beats the file.
"""

from __future__ import annotations

from fastapi import FastAPI

from nse_algo_trader.broker_credentials import load_env_file_into_environ
from nse_algo_trader.dashboard.dashboard_server import build_dashboard_app

load_env_file_into_environ()

app: FastAPI = build_dashboard_app()
