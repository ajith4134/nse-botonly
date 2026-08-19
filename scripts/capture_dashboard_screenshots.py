"""CLI wrapper over the `R.08` visual-confirmation capture.

The logic lives in `nse_algo_trader.dashboard.dashboard_surface_screenshot_capture` so
the daily run can call it too. When it lived only here it ran only when a human
remembered, which made `R.08` depend on memory.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from nse_algo_trader.dashboard.dashboard_surface_screenshot_capture import (
    DEFAULT_BASE_URL,
    DEFAULT_OUTPUT_ROOT,
    MINIMUM_CREDIBLE_PNG_BYTES,
    DashboardCaptureError,
    capture_dashboard,
    read_access_token,
)


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
        results = capture_dashboard(arguments.base_url, output_directory, read_access_token())
    except (PlaywrightError, DashboardCaptureError) as failure:
        print(f"CAPTURE FAILED: {failure}", file=sys.stderr)
        return 1

    blank = [result for result in results if not result.is_credible]
    noisy = [result for result in results if result.console_errors]

    for result in results:
        mark = "ok " if result.is_credible else "BLANK"
        print(
            f"{mark} {result.theme:5s} {result.route:10s} {result.byte_count:>8,}B  {result.path}"
        )
    for result in noisy:
        for message in result.console_errors:
            print(f"  console error on {result.route} [{result.theme}]: {message}")

    print(f"\n{len(results)} screenshots -> {output_directory}")
    if blank:
        print(
            f"FAILED: {len(blank)} screenshot(s) below {MINIMUM_CREDIBLE_PNG_BYTES:,}B "
            "— the page did not paint",
            file=sys.stderr,
        )
        return 1
    if noisy:
        print(f"FAILED: console errors on {len(noisy)} page(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
