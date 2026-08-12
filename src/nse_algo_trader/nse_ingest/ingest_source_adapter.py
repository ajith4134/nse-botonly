"""The contract every NSE source adapter implements — and the reason it is this small.

Nine `L0` sources are the same engine wearing different hats. Everything hard about
ingest — retry, failure classification, atomicity, deduplication, revision retention,
provenance, coverage — belongs to the core and is written and hardened **once**. What
differs between sources is only: where the file lives, how to read it, what date each
row is about, and what makes a row unique.

So an adapter has four methods and no I/O of its own. It cannot open a socket, cannot
touch the database, and cannot decide what "done" means. That is deliberate: adapters
are the part that gets fanned out to parallel agents (`A.46`), and a narrow contract is
what makes that safe. An adapter author's whole surface is parsing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget


class IngestAdapterError(Exception):
    """Raised by an adapter when a payload cannot be parsed.

    Adapters raise rather than returning an empty sequence. Zero rows is a legitimate
    answer for a holiday; zero rows is NOT a legitimate answer for a corrupt file, and
    conflating the two is how a broken parser is mistaken for a quiet day.
    """


@dataclass(frozen=True)
class IngestRow:
    """One parsed record, before the core decides what to do with it.

    `values` carries the source's own fields untouched. The core never interprets them —
    it stores them, keys them and versions them — which is why a new source needs no
    change to the core.
    """

    values: Mapping[str, Any]
    effective_date: date
    natural_key: tuple[str, ...]
    """What makes this row unique WITHIN its effective date and source. Two rows sharing
    a natural key and effective date are the same fact observed twice, and the store
    treats them as a revision rather than a duplicate."""

    def __post_init__(self) -> None:
        if not self.natural_key:
            raise IngestAdapterError("a row must carry a non-empty natural key")


@dataclass(frozen=True)
class SourceCoverageFloor:
    """The earliest date a source can possibly answer for, and how that was established.

    Recorded because `R.17` demands mechanical evidence: "cash bhavcopy goes back to
    1994-11" is a claim, and the claim's provenance belongs next to it so a later reader
    can tell a verified floor from a remembered one.
    """

    earliest_date: date
    established_by: str


@runtime_checkable
class NseIngestSourceAdapter(Protocol):
    """What every source must provide, and nothing more.

    `runtime_checkable` on purpose: the conformance suite verifies adapters structurally
    at runtime. A plain `Protocol` annotation is erased at runtime and checks nothing,
    which is a mistake this project has already made once and had caught by review.
    """

    @property
    def source_name(self) -> str:
        """Self-describing, `R.14`, and the store's partition key for this source."""
        ...

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        """The verified earliest date, used to reject impossible effective dates."""
        ...

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """Where to fetch each requested date from. May return fewer than requested."""
        ...

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        """Rows from one payload. Raises `IngestAdapterError` on anything malformed."""
        ...

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        """Why this payload is NOT what `target` asked for, or None if it is.

        The defence against the measured case where a request for Sunday 2026-08-09
        returned Friday's file with HTTP 200. Only the adapter can read the date out of
        its own format, so only the adapter can catch it.
        """
        ...


@dataclass(frozen=True)
class RollingSnapshotSource:
    """Marks a source whose file has no history — today's file is all there is.

    The F&O ban list, bulk/block deals and the circuit-band `sec_list.csv` are published
    as a single rolling file that is overwritten in place (`research/207`). There is no
    archive to backfill: history for these accrues **only** from the day snapshotting
    starts, and never retroactively.

    That is the same permanent-loss shape as the depth tape (`A.44`), so these sources
    are the most urgent of the nine, and it is also why `observed_at` is not a nicety —
    for a rolling source it is the only thing that distinguishes one day's file from the
    next, since the URL and often the content are otherwise identical.
    """

    source_name: str
    reason: str = field(default="published as a single rolling file with no dated archive")
