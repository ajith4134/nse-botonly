"""Every dashboard route must be screenshotted — `R.08`/`R.N`.

The capture list was hand-maintained, so a surface added without editing it was live and never
visually verified. `/ladder` and `/traces` were both in that state on the day they shipped. This
test compares the list against the app's OWN registered routes, so the list cannot fall behind
again without something failing.
"""

from __future__ import annotations

from nse_algo_trader.dashboard.dashboard_surface_screenshot_capture import ROUTES

# Routes that are deliberately not screenshotted, each with the reason it is not a surface.
NOT_SURFACES = {
    "/": "redirects to /wall",
    "/healthz": "a liveness probe, not a page",
    "/manifest": "included already",
}


def test_every_registered_html_route_is_in_the_capture_list() -> None:
    from nse_algo_trader.dashboard.dashboard_server import build_dashboard_app

    app = build_dashboard_app()
    registered = {
        route.path  # type: ignore[attr-defined]
        for route in app.routes
        if getattr(route, "path", "").startswith("/")
        and "HTMLResponse" in str(getattr(route, "response_class", ""))
    }
    missing = sorted(registered - set(ROUTES) - set(NOT_SURFACES))
    assert not missing, (
        f"these dashboard surfaces are live but never screenshotted: {missing}. Add them to "
        f"ROUTES, or to NOT_SURFACES with the reason they are not a page."
    )


def test_the_capture_list_has_no_route_the_app_does_not_serve() -> None:
    """A stale entry fails the capture run every night for a page that no longer exists."""
    from nse_algo_trader.dashboard.dashboard_server import build_dashboard_app

    app = build_dashboard_app()
    served = {getattr(route, "path", "") for route in app.routes}
    stale = sorted(path for path in ROUTES if path not in served)
    assert not stale, f"the capture list names routes the app does not serve: {stale}"
