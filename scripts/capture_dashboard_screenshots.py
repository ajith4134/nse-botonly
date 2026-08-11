"""`R.08`/`L13.x` visual confirmation — screenshot every dashboard route, both themes.

The standing rule is that a change is confirmed by LOOKING at the surface, not by a 200
from `curl`. Those are different claims: a 200 says bytes were returned, and every one of
the failures worth catching here returns 200 while doing so — a panel rendering an empty
table, a dark-mode token that never got redefined so text sits black on black, a number
formatted as `nan`, a layout that overflows on a phone.

Two themes because dark mode on this project is SELECTED, never an automatic flip, and
the only way a missing token shows itself is in the rendered pixels.

Failure here is loud and non-zero-exit. A screenshot tool that silently writes a blank
PNG when the server is down is worse than no tool, because the artifact it leaves behind
looks exactly like a successful run.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
ACCESS_TOKEN_PATH = Path("~/.nse_algo_trader/dashboard_access_token.txt").expanduser()
DEFAULT_OUTPUT_ROOT = Path("~/nse_archive/dashboard_screenshots").expanduser()

ROUTES = ("/wall", "/regime", "/manifest")
"""Every route a human reads. `/healthz` is excluded deliberately: it returns plain text
and has no visual claim to confirm."""

THEMES = ("light", "dark")

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 1000

FIRST_HTTP_ERROR_STATUS = 400
"""HTTP's own boundary between a served page and a refusal — a protocol fact."""

MINIMUM_CREDIBLE_PNG_BYTES = 3_000
"""A rendered page of tables is never this small. A near-empty PNG means the page failed
to paint — a blank screenshot must FAIL rather than sit in the archive looking like
evidence. Sized from the floor of a blank 1440x1000 solid-fill PNG, not from taste."""


@dataclass(frozen=True)
class CaptureResult:
    """One route in one theme, and whether it produced something worth looking at."""

    route: str
    theme: str
    path: Path
    byte_count: int
    console_errors: tuple[str, ...]

    @property
    def is_credible(self) -> bool:
        return self.byte_count >= MINIMUM_CREDIBLE_PNG_BYTES


def read_access_token() -> str | None:
    if not ACCESS_TOKEN_PATH.exists():
        return None
    token = ACCESS_TOKEN_PATH.read_text().strip()
    return token or None


def capture_dashboard(
    base_url: str, output_directory: Path, token: str | None
) -> list[CaptureResult]:
    """Screenshot every route in both themes. Raises if the browser cannot start."""
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
                # accepting reflected keys into links for a reason, and a screenshot tool
                # appending `?key=` to every URL would reintroduce the habit that caused it.
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
                page.on(
                    "console",
                    lambda message, sink=console_errors: (
                        sink.append(message.text) if message.type == "error" else None
                    ),
                )
                for route in ROUTES:
                    before = len(console_errors)
                    response = page.goto(f"{base_url}{route}", wait_until="networkidle")
                    if response is not None and response.status >= FIRST_HTTP_ERROR_STATUS:
                        raise RuntimeError(
                            f"{route} returned HTTP {response.status} — refusing to "
                            "screenshot an error page as if it were the surface"
                        )
                    filename = f"{route.strip('/').replace('/', '_') or 'index'}_{theme}.png"
                    destination = output_directory / filename
                    page.screenshot(path=str(destination), full_page=True)
                    results.append(
                        CaptureResult(
                            route=route,
                            theme=theme,
                            path=destination,
                            byte_count=destination.stat().st_size,
                            console_errors=tuple(console_errors[before:]),
                        )
                    )
                context.close()
        finally:
            browser.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--label", default="", help="appended to the run directory name")
    arguments = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"_{arguments.label}" if arguments.label else ""
    output_directory = arguments.output_root / f"{stamp}{suffix}"

    try:
        results = capture_dashboard(
            arguments.base_url, output_directory, read_access_token()
        )
    except (PlaywrightError, RuntimeError) as failure:
        print(f"CAPTURE FAILED: {failure}", file=sys.stderr)
        return 1

    blank = [result for result in results if not result.is_credible]
    noisy = [result for result in results if result.console_errors]

    for result in results:
        mark = "ok " if result.is_credible else "BLANK"
        print(
            f"{mark} {result.theme:5s} {result.route:10s} "
            f"{result.byte_count:>8,}B  {result.path}"
        )
    for result in noisy:
        for message in result.console_errors:
            print(f"  console error on {result.route} [{result.theme}]: {message}")

    print(f"\n{len(results)} screenshots -> {output_directory}")
    if blank:
        print(f"FAILED: {len(blank)} screenshot(s) below {MINIMUM_CREDIBLE_PNG_BYTES:,}B "
              "— the page did not paint", file=sys.stderr)
        return 1
    if noisy:
        print(f"FAILED: console errors on {len(noisy)} page(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
