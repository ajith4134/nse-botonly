"""`R.08` visual confirmation, as a library so a scheduler can call it.

A change is confirmed by LOOKING at the surface, not by a 200 from `curl`. Those are
different claims, and every failure worth catching here returns 200 while doing it: a
panel rendering an empty table, a dark-mode token that was never redefined so text sits
black on black, a number formatted as `nan`, a layout that overflows.

This lived in `scripts/` and therefore ran only when a human remembered to run it, which
made `R.08` depend on memory. It is a library now so the daily run can call it and the
CLI can too, and the memory stops being load-bearing.

Both themes, always: dark mode here is SELECTED rather than flipped, and a token that was
never redefined shows itself only in rendered pixels.

Failure is loud and non-zero. A capture tool that silently writes a blank PNG when the
server is down leaves an artifact indistinguishable from a successful run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from playwright.sync_api import ConsoleMessage, sync_playwright

from nse_algo_trader.dashboard.rendered_surface_honesty_check import (
    RenderedSurfaceFinding,
    inspect_rendered_surface,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
ACCESS_TOKEN_PATH = Path("~/.nse_algo_trader/dashboard_access_token.txt").expanduser()
DEFAULT_OUTPUT_ROOT = Path("~/nse_archive/dashboard_screenshots").expanduser()

ROUTES = (
    # A hand-maintained list is the R.08 hole in this capture: a surface added without editing
    # here is never screenshotted and never visually verified, which is how `/ladder` and
    # `/traces` were both live and unphotographed on the day they shipped. `test_every_route_is_
    # captured` in tests/ compares this tuple against the app's own registered routes so the list
    # cannot silently fall behind again.
    "/wall",
    "/regime",
    "/manifest",
    "/microstructure",
    "/rules",
    "/clock",
    "/feed",
    "/history",
    "/costs",
    "/orders",
    "/paper-capital",
    "/sizing",
    "/paper-session",
    "/trials",
    "/ladder",
    "/traces",
    "/quality",
    "/bots",
    "/loop",
    "/trading",
)

PAGE_LOAD_TIMEOUT_MILLISECONDS = 120_000
"""Raised from playwright's 30 s default because a real surface crossed it.

Measured 2026-08-12: `/microstructure` takes **31 s**, because it replays the depth tape on
request and the tape keeps growing (the persisted-read-model debt already recorded in
BACKLOG). Playwright then failed the whole capture, so an `R.08` obligation was being
blocked by a known performance problem on an unrelated page. The bound stays finite — a
page that takes two minutes is broken and should fail the capture — and the underlying
replay-on-request debt is tracked, not papered over."""
"""Every route a human reads. `/healthz` is excluded deliberately — it returns plain text
and has no visual claim to confirm.

A route added here without being added to the server 404s and the capture fails, which is
the intended direction of that dependency: the screenshot pass is the `R.08` check, so it
should break when a surface disappears rather than quietly capture one page fewer."""

THEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")
"""Literal, not `str`: playwright types `color_scheme` as an enum of exact values, so a
plain string would pass a typo through to a silently wrong-theme screenshot."""

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 1000

FIRST_HTTP_ERROR_STATUS = 400
"""HTTP's own boundary between a served page and a refusal — a protocol fact."""

MINIMUM_CREDIBLE_PNG_BYTES = 3_000
"""A rendered page of tables is never this small. A near-empty PNG means the page failed
to paint, and a blank screenshot must FAIL rather than sit in the archive looking like
evidence. Sized from the floor of a blank 1440x1000 solid-fill PNG, not from taste."""


class DashboardCaptureError(RuntimeError):
    """The surface could not be captured — raised rather than returning a blank frame."""


@dataclass(frozen=True)
class CaptureResult:
    """One route in one theme, and whether it produced something worth looking at."""

    route: str
    theme: str
    path: Path
    byte_count: int
    console_errors: tuple[str, ...]
    rendered_findings: tuple[RenderedSurfaceFinding, ...] = ()
    """Ways this page misleads a reader who is not checking it against the source (`A.137`).

    The capture already loads every route in a real browser, so it is the one place that HAS the
    rendered HTML. Checking it here costs nothing extra and runs every night, which is what turns
    "render it and look at it" from an intention into a step.
    """

    @property
    def is_credible(self) -> bool:
        return self.byte_count >= MINIMUM_CREDIBLE_PNG_BYTES


def read_access_token() -> str | None:
    """The configured token, or None when no file exists (then the gate is open)."""
    if not ACCESS_TOKEN_PATH.exists():
        return None
    token = ACCESS_TOKEN_PATH.read_text().strip()
    return token or None


def _console_error_recorder(sink: list[str]) -> Callable[[ConsoleMessage], None]:
    """A handler bound to ONE sink.

    Not a lambda with a default argument: that bound the list correctly but made the
    handler two-arity, which is not how playwright calls it. A closure over an explicit
    parameter is both correctly typed and correctly bound — the sink list is rebound once
    per theme, so a handler closing over the NAME would append wherever the name points
    when it fires, mixing the light pass's errors into the dark pass's list.
    """

    def record(message: ConsoleMessage) -> None:
        if message.type == "error":
            sink.append(message.text)

    return record


def capture_dashboard(
    base_url: str, output_directory: Path, token: str | None
) -> list[CaptureResult]:
    """Screenshot every route in both themes.

    Raises:
        DashboardCaptureError: a route returned an error status, so there is no surface
            to photograph. Capturing an error page as though it were the dashboard is
            worse than failing, because the artifact looks like evidence.
        PlaywrightError: the browser could not start.
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    results: list[CaptureResult] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for theme in THEMES:
                context = browser.new_context(
                    viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                    color_scheme=theme,
                )
                # The token goes in a COOKIE, never a query string. The server stopped
                # accepting reflected keys into links for a reason, and a capture tool
                # appending `?key=` to every URL would reintroduce that habit.
                if token:
                    host = base_url.split("//", 1)[-1].split(":", 1)[0]
                    context.add_cookies(
                        [
                            {
                                "name": "nse_dashboard_key",
                                "value": token,
                                "domain": host,
                                "path": "/",
                            }
                        ]
                    )
                page = context.new_page()
                console_errors: list[str] = []
                page.on("console", _console_error_recorder(console_errors))
                for route in ROUTES:
                    before = len(console_errors)
                    response = page.goto(
                        f"{base_url}{route}",
                        wait_until="networkidle",
                        timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS,
                    )
                    if response is not None and response.status >= FIRST_HTTP_ERROR_STATUS:
                        raise DashboardCaptureError(
                            f"{route} returned HTTP {response.status} — refusing to "
                            "screenshot an error page as if it were the surface"
                        )
                    stem = route.strip("/").replace("/", "_") or "index"
                    destination = output_directory / f"{stem}_{theme}.png"
                    page.screenshot(path=str(destination), full_page=True)
                    findings = (
                        inspect_rendered_surface(route, page.content())
                        if theme == THEMES[0]  # the HTML is the same in both themes
                        else []
                    )
                    results.append(
                        CaptureResult(
                            route=route,
                            theme=theme,
                            path=destination,
                            byte_count=destination.stat().st_size,
                            console_errors=tuple(console_errors[before:]),
                            rendered_findings=tuple(findings),
                        )
                    )
                context.close()
        finally:
            browser.close()
    return results


def summarise_capture(results: Sequence[CaptureResult]) -> str:
    """One line a scheduler can log, naming the failures rather than counting them."""
    blank = [result for result in results if not result.is_credible]
    noisy = [result for result in results if result.console_errors]
    if not results:
        return "no routes captured"
    detail = f"{len(results)} screenshots, {len(ROUTES)} routes x {len(THEMES)} themes"
    if blank:
        detail += " · BLANK: " + ", ".join(f"{r.route}[{r.theme}]" for r in blank)
    if noisy:
        detail += " · CONSOLE ERRORS: " + ", ".join(f"{r.route}[{r.theme}]" for r in noisy)
    misleading = [result for result in results if result.rendered_findings]
    if misleading:
        detail += " · MISLEADING RENDER: " + "; ".join(
            finding.describe()
            for result in misleading
            for finding in result.rendered_findings
        )
    if not blank and not noisy and not misleading:
        detail += " · all painted, no console errors, nothing misleading"
    return detail


def capture_failed(results: Sequence[CaptureResult]) -> bool:
    """Whether the capture proves a problem — a blank frame, a console error, or a misleading page.

    A misleading render counts as a failure rather than a warning. A page that prints a UTC clock on
    an NSE dashboard is not degraded, it is wrong, and the whole reason these defects survived is
    that nothing treated them as failures.
    """
    return any(
        not result.is_credible or result.console_errors or result.rendered_findings
        for result in results
    )
