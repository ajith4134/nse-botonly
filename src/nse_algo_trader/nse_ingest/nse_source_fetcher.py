"""Fetching an NSE file, and deciding honestly whether what came back is the data.

The naive contract — "HTTP 200 means success" — is wrong on this platform in three
separately measured ways (`research/207`, `research/210`), and each one produces
plausible-looking wrong data rather than an error:

- **A request for Sunday 2026-08-09 returned Friday's file, with HTTP 200.** Nothing in
  the response says so. A fetcher that trusts the status code ingests stale data as
  fresh, dated to the day it asked for. This is the worst of the three because the
  result is silently, confidently wrong forever.
- **`www.nseindia.com/reports/asm` returns HTTP 200** carrying a React shell whose table
  is populated by JavaScript. The body is a page, not data.
- **Blocking is inconsistent per endpoint**: the homepage 403s behind Akamai, the
  historical-deals API 503s behind an Apache bot page, the option-chain API 404s, and
  corporate-announcements answers 200 with real data and no cookie at all.

So a fetch does not return bytes-or-exception. It returns a **classified outcome** with
the evidence for its classification, and the retry policy follows from the class: a
transient timeout is worth retrying, a bot wall is worth retrying slowly, and a
content mismatch is worth retrying **never** — asking again returns the same wrong file,
so retrying only converts a detectable fault into a slow detectable fault.

The content check is the caller's, because only the adapter knows what its payload
should contain. That is the seam through which "this is Friday's data" becomes knowable.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

import requests

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
"""NSE's archive hosts serve any request carrying a browser User-Agent and refuse those
that do not. Verified across dozens of fetches in `research/207`; not a preference."""

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
HTTP_SERVICE_UNAVAILABLE = 503
"""Protocol constants, permitted under `R.23(e)` as facts of HTTP rather than tunables."""

_BLOCK_PAGE_MARKERS = (
    b"access denied",
    b"unable to process your request",
    b"reference #",
    b"akamai",
    b"request unsuccessful",
)
"""Byte markers observed in the real block pages NSE served, each with HTTP 200 or an
error status but always as HTML. Matched case-insensitively against a lowered prefix."""

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<script")

_JAVASCRIPT_SHELL_MARKERS = (b"__next_data__", b"<div id=\"root\"", b"<div id=\"__next\"")
"""A single-page-app shell: HTTP 200, a real page, and no data in the body."""


class FetchStatus(Enum):
    """What actually came back, as distinct from what the status code claimed."""

    RETRIEVED = "retrieved"
    NOT_FOUND = "not_found"
    BOT_BLOCKED = "bot_blocked"
    JAVASCRIPT_SHELL = "javascript_shell"
    CONTENT_MISMATCH = "content_mismatch"
    EMPTY_PAYLOAD = "empty_payload"
    TRANSPORT_FAILURE = "transport_failure"

    @property
    def is_worth_retrying(self) -> bool:
        """Whether asking again could plausibly produce a different answer.

        `CONTENT_MISMATCH` is deliberately excluded: the server answered, and it answered
        with the wrong data. Retrying returns the same wrong data, so a retry converts a
        clean detection into a delayed one and nothing else.
        """
        return self in (FetchStatus.TRANSPORT_FAILURE, FetchStatus.BOT_BLOCKED)

    @property
    def is_success(self) -> bool:
        return self is FetchStatus.RETRIEVED


@dataclass(frozen=True)
class FetchTarget:
    """One thing to fetch, and what would make the answer believable."""

    url: str
    source_name: str
    expects: str = ""
    """The ISO calendar date (`YYYY-MM-DD`) this payload must be about, or empty when the
    source cannot be addressed by date at all.

    Deliberately narrow. While it was merely "a human-readable statement", an adapter
    invented a composite `date|symbol|expiry` encoding, and a plain ISO date handed to it
    then parsed as nothing — so its wrong-date check **silently skipped itself**. A
    fail-open check is worse than no check because it reports success. Anything else an
    adapter needs belongs in the URL, which is already per-target."""


@dataclass(frozen=True)
class FetchOutcome:
    """The classified result, carrying the evidence for its own classification."""

    target: FetchTarget
    status: FetchStatus
    payload: bytes | None
    http_status: int | None
    attempts: int
    fetched_at: datetime
    evidence: str
    elapsed_seconds: float = 0.0

    @property
    def content_sha256(self) -> str | None:
        """The payload's checksum — the provenance record's identity for this fetch."""
        return hashlib.sha256(self.payload).hexdigest() if self.payload else None

    def require_payload(self) -> bytes:
        """The bytes, or a raised error naming exactly why there are none.

        Callers that want the happy path use this; nothing silently receives `None`.
        """
        if self.payload is None or not self.status.is_success:
            raise NseSourceFetchError(
                f"{self.target.source_name}: {self.status.value} for {self.target.url} "
                f"after {self.attempts} attempt(s) — {self.evidence}"
            )
        return self.payload


class NseSourceFetchError(Exception):
    """Raised when a caller demands a payload from an unsuccessful fetch."""


class PayloadContentCheck(Protocol):
    """Verifies that a payload really is the thing that was asked for.

    Returning a reason string means MISMATCH; returning None means the content is
    consistent with the request. This is where "the file is Friday's" is detected, and
    it lives with the adapter because only the adapter can read its own format.
    """

    def __call__(self, payload: bytes, target: FetchTarget) -> str | None: ...


def classify_payload(
    payload: bytes,
    http_status: int,
    target: FetchTarget,
    content_check: PayloadContentCheck | None = None,
) -> tuple[FetchStatus, str]:
    """(status, evidence) for one response, judged on its body and not only its code."""
    if http_status == HTTP_NOT_FOUND:
        return (FetchStatus.NOT_FOUND, f"HTTP {http_status}")
    if not payload:
        return (FetchStatus.EMPTY_PAYLOAD, f"HTTP {http_status} with a zero-length body")

    lowered_prefix = payload[:4096].lower()
    looks_like_html = any(marker in lowered_prefix for marker in _HTML_MARKERS)

    if any(marker in lowered_prefix for marker in _BLOCK_PAGE_MARKERS):
        return (
            FetchStatus.BOT_BLOCKED,
            f"HTTP {http_status} carrying a block page",
        )
    if http_status in (HTTP_FORBIDDEN, HTTP_SERVICE_UNAVAILABLE):
        return (FetchStatus.BOT_BLOCKED, f"HTTP {http_status}")
    if looks_like_html and any(
        marker in lowered_prefix for marker in _JAVASCRIPT_SHELL_MARKERS
    ):
        return (
            FetchStatus.JAVASCRIPT_SHELL,
            f"HTTP {http_status} carrying a JavaScript shell, no data in the body",
        )
    if http_status != HTTP_OK:
        return (FetchStatus.TRANSPORT_FAILURE, f"HTTP {http_status}")
    if looks_like_html:
        # A data endpoint answering with HTML is answering with something else. Reported
        # as a block rather than data, because that is what it has always turned out to be.
        return (
            FetchStatus.BOT_BLOCKED,
            f"HTTP {http_status} returned HTML where data was expected",
        )

    if content_check is not None:
        mismatch_reason = content_check(payload, target)
        if mismatch_reason is not None:
            return (
                FetchStatus.CONTENT_MISMATCH,
                f"HTTP {http_status} but the payload is not what was requested: "
                f"{mismatch_reason}",
            )
    return (FetchStatus.RETRIEVED, f"HTTP {http_status}, {len(payload):,} bytes")


@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter — the AWS-documented shape.

    Jitter is not decoration: without it every retry after a shared outage fires in
    lockstep, which is precisely the traffic pattern a bot-wall is watching for.
    """

    maximum_attempts: int = 4
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 3.0
    maximum_backoff_seconds: float = 60.0

    def backoff_for(self, attempt_number: int, jitter: float) -> float:
        """Seconds to wait before `attempt_number` (1-based), given jitter in [0, 1)."""
        uncapped = self.initial_backoff_seconds * (
            self.backoff_multiplier ** max(0, attempt_number - 1)
        )
        return min(uncapped, self.maximum_backoff_seconds) * jitter


@dataclass
class NseSourceFetcher:
    """Fetches NSE files and classifies what comes back.

    The HTTP session is injectable so the whole classification path is testable with no
    network — the `Rule J` seam for this component.
    """

    http_session: requests.Session | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    request_timeout_seconds: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    jitter_source: Callable[[], float] = random.random
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        self._session = self.http_session or requests.Session()
        self._session.headers.update({"User-Agent": BROWSER_USER_AGENT})

    def fetch(
        self,
        target: FetchTarget,
        content_check: PayloadContentCheck | None = None,
    ) -> FetchOutcome:
        """Fetch once, retrying only the classes where retrying can help."""
        started = time.monotonic()
        last_status = FetchStatus.TRANSPORT_FAILURE
        last_evidence = "no attempt was made"
        last_http_status: int | None = None
        payload: bytes | None = None

        for attempt in range(1, self.retry_policy.maximum_attempts + 1):
            try:
                response = self._session.get(
                    target.url, timeout=self.request_timeout_seconds
                )
                last_http_status = response.status_code
                payload = response.content
                last_status, last_evidence = classify_payload(
                    payload, response.status_code, target, content_check
                )
            except requests.RequestException as transport_failure:
                # Recorded, never swallowed: the reason is the evidence a blocker needs.
                last_status = FetchStatus.TRANSPORT_FAILURE
                last_evidence = f"{type(transport_failure).__name__}: {transport_failure}"
                last_http_status = None
                payload = None

            if last_status.is_success or not last_status.is_worth_retrying:
                break
            if attempt < self.retry_policy.maximum_attempts:
                self.sleep(self.retry_policy.backoff_for(attempt, self.jitter_source()))

        return FetchOutcome(
            target=target,
            status=last_status,
            # The payload is kept even on failure: a block page or a wrong-dated file
            # is the evidence for the classification, and discarding it would leave a
            # blocker report with nothing behind it.
            payload=payload,
            http_status=last_http_status,
            attempts=attempt,
            fetched_at=self.clock(),
            evidence=last_evidence,
            elapsed_seconds=time.monotonic() - started,
        )
