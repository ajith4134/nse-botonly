"""The capture's verdict logic — the part that decides whether a run FAILED.

The browser path is verified on the real server (`R.05`); these cover the judgement,
because a capture tool whose verdict is wrong is worse than none: it archives blank
frames that look exactly like evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nse_algo_trader.dashboard.dashboard_surface_screenshot_capture import (
    MINIMUM_CREDIBLE_PNG_BYTES,
    CaptureResult,
    capture_failed,
    summarise_capture,
)


def _result(
    *,
    route: str = "/wall",
    theme: str = "dark",
    size: int = 400_000,
    errors: tuple[str, ...] = (),
) -> CaptureResult:
    return CaptureResult(
        route=route,
        theme=theme,
        path=Path(f"/tmp/{route.strip('/')}_{theme}.png"),
        byte_count=size,
        console_errors=errors,
    )


@pytest.mark.unit
def test_a_blank_frame_is_not_credible_and_fails_the_capture() -> None:
    """A near-empty PNG means the page did not paint. Silence here is the whole risk."""
    blank = _result(size=MINIMUM_CREDIBLE_PNG_BYTES - 1)
    assert not blank.is_credible
    assert capture_failed([blank])
    assert "BLANK" in summarise_capture([blank])
    assert "/wall[dark]" in summarise_capture([blank])


@pytest.mark.unit
def test_a_console_error_fails_the_capture_even_when_the_page_painted() -> None:
    """A page can paint and still be broken — the console is the second channel."""
    noisy = _result(errors=("TypeError: x is not a function",))
    assert noisy.is_credible
    assert capture_failed([noisy])
    assert "CONSOLE ERRORS" in summarise_capture([noisy])


@pytest.mark.unit
def test_a_healthy_capture_says_so_without_hedging() -> None:
    results = [
        _result(theme=theme, route=route)
        for theme in ("light", "dark")
        for route in ("/wall", "/regime")
    ]
    assert not capture_failed(results)
    summary = summarise_capture(results)
    assert "all painted, no console errors" in summary
    assert "BLANK" not in summary


@pytest.mark.unit
def test_an_empty_capture_is_reported_rather_than_passing_silently() -> None:
    """Zero screenshots must never read as success — it is the tool having done nothing."""
    assert summarise_capture([]) == "no routes captured"


@pytest.mark.unit
def test_the_threshold_is_a_floor_not_a_range() -> None:
    """Exactly at the threshold is credible; one byte under is not."""
    assert _result(size=MINIMUM_CREDIBLE_PNG_BYTES).is_credible
    assert not _result(size=MINIMUM_CREDIBLE_PNG_BYTES - 1).is_credible
