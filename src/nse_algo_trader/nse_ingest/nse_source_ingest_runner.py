"""The one loop that drives every source: fetch → classify → parse → store → report.

This is the piece that makes the core a core rather than five modules that happen to
live together. Every adapter goes through exactly this path, so a fix here fixes all
nine sources at once — which is the entire argument for building a shared core rather
than nine hand-rolled ingesters (`A.46`).

The ordering of the failure cases is the design. A fetch that was blocked, shelled or
mismatched is **recorded and skipped**, never parsed: handing a block page to a parser
produces either a crash or, far worse, zero rows that look exactly like a quiet holiday.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import (
    BitemporalIngestStore,
    IngestResult,
)
from nse_algo_trader.nse_ingest.discovering_ingest_source_adapter import (
    DiscoveredParameterStore,
    DiscoveringNseIngestSourceAdapter,
    DiscoveryPlan,
    plan_targets_with_discovery,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import (
    FetchOutcome,
    FetchStatus,
    FetchTarget,
    NseSourceFetcher,
)


@dataclass(frozen=True)
class TargetIngestOutcome:
    """What happened for one target — enough to explain a gap without re-running."""

    target: FetchTarget
    fetch_status: FetchStatus
    evidence: str
    rows_parsed: int = 0
    ingest: IngestResult | None = None
    parse_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.fetch_status.is_success and self.parse_error is None


@dataclass
class SourceIngestRun:
    """Everything one run of one source did, including what it could not do."""

    source_name: str
    started_at: datetime
    outcomes: list[TargetIngestOutcome] = field(default_factory=list)
    discovery_plan: object | None = None
    """How the targets were arrived at, when discovery was used — remembered, asked, or
    guessed. A run that guessed and a run that asked look identical in their outcomes
    and differ in request cost by orders of magnitude, so the distinction is recorded."""

    @property
    def rows_inserted(self) -> int:
        return sum(
            outcome.ingest.rows_inserted for outcome in self.outcomes if outcome.ingest
        )

    @property
    def revisions_recorded(self) -> int:
        return sum(
            outcome.ingest.revisions_recorded
            for outcome in self.outcomes
            if outcome.ingest
        )

    @property
    def blocked_targets(self) -> list[TargetIngestOutcome]:
        """`R.16`: a blocked source is an acquisition problem to solve, not a smaller
        feature to ship. Surfaced so it can never be mistaken for an empty day."""
        return [
            outcome
            for outcome in self.outcomes
            if outcome.fetch_status
            in (FetchStatus.BOT_BLOCKED, FetchStatus.JAVASCRIPT_SHELL)
        ]

    @property
    def content_mismatches(self) -> list[TargetIngestOutcome]:
        """The measured Sunday-returns-Friday case. Never silently ingested."""
        return [
            outcome
            for outcome in self.outcomes
            if outcome.fetch_status is FetchStatus.CONTENT_MISMATCH
        ]

    @property
    def parse_failures(self) -> list[TargetIngestOutcome]:
        return [outcome for outcome in self.outcomes if outcome.parse_error]

    def _describe_target_source(self) -> str:
        """How the targets were arrived at, when discovery was involved."""
        if self.discovery_plan is None:
            return ""
        route = (
            "fallback ladder"
            if getattr(self.discovery_plan, "used_fallback", False)
            else "discovery"
        )
        return f" | targets via {route}"

    def describe(self) -> str:
        return (
            f"{self.source_name}: {self.rows_inserted:,} rows inserted, "
            f"{self.revisions_recorded:,} revisions, "
            f"{len(self.blocked_targets)} blocked, "
            f"{len(self.content_mismatches)} content mismatches, "
            f"{len(self.parse_failures)} parse failures"
            + self._describe_target_source()
        )


class NseSourceIngestRunner:
    """Drives one adapter through the shared pipeline."""

    def __init__(
        self,
        fetcher: NseSourceFetcher,
        store: BitemporalIngestStore,
        clock: type[datetime] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._clock = clock or datetime

    def _now(self) -> datetime:
        return self._clock.now(UTC)

    def ingest(
        self,
        adapter: NseIngestSourceAdapter,
        for_dates: Sequence[date],
        discovery_memo: DiscoveredParameterStore | None = None,
    ) -> SourceIngestRun:
        """Fetch, classify, parse and store every target for the requested dates.

        When the adapter can discover its own parameters and a memo is supplied, the
        two-phase path runs first: remembered parameters cost nothing, one discovery
        request beats a thousand guesses, and the adapter's own candidate ladder is the
        last resort rather than the only one (`L0.35`).
        """
        run = SourceIngestRun(source_name=adapter.source_name, started_at=self._now())
        targets = self._plan_targets(adapter, for_dates, discovery_memo, run)

        for target in targets:
            outcome = self._fetcher.fetch(
                target, content_check=adapter.content_mismatch_reason
            )
            fetch_id = self._record(adapter, outcome)

            if not outcome.status.is_success or outcome.payload is None:
                # Recorded and skipped. Parsing a block page yields either a crash or
                # zero rows indistinguishable from a holiday — the second is worse.
                run.outcomes.append(
                    TargetIngestOutcome(
                        target=target,
                        fetch_status=outcome.status,
                        evidence=outcome.evidence,
                    )
                )
                continue

            try:
                rows = adapter.parse(outcome.payload, target)
            except IngestAdapterError as parse_failure:
                run.outcomes.append(
                    TargetIngestOutcome(
                        target=target,
                        fetch_status=outcome.status,
                        evidence=outcome.evidence,
                        parse_error=str(parse_failure),
                    )
                )
                continue

            ingest_result = self._store.ingest_rows(
                source_name=adapter.source_name,
                rows=rows,
                observed_at=outcome.fetched_at,
                fetch_id=fetch_id,
                coverage_floor=adapter.coverage_floor.earliest_date,
            )
            run.outcomes.append(
                TargetIngestOutcome(
                    target=target,
                    fetch_status=outcome.status,
                    evidence=outcome.evidence,
                    rows_parsed=len(rows),
                    ingest=ingest_result,
                )
            )
        return run

    def _plan_targets(
        self,
        adapter: NseIngestSourceAdapter,
        for_dates: Sequence[date],
        discovery_memo: DiscoveredParameterStore | None,
        run: SourceIngestRun,
    ) -> Sequence[FetchTarget]:
        """Targets via discovery when the adapter supports it, else the plain contract."""
        if discovery_memo is None or not isinstance(
            adapter, DiscoveringNseIngestSourceAdapter
        ):
            return adapter.fetch_targets(for_dates)

        discovered: dict[str, object] = {}
        for discovery_target in adapter.discovery_targets(for_dates):
            outcome = self._fetcher.fetch(discovery_target)
            self._record(adapter, outcome)
            if outcome.status.is_success and outcome.payload is not None:
                discovered[discovery_target.url] = adapter.parse_discovery(
                    outcome.payload, discovery_target
                )

        plan: DiscoveryPlan = plan_targets_with_discovery(
            adapter, for_dates, discovery_memo, discovered or None  # type: ignore[arg-type]
        )
        run.discovery_plan = plan
        return plan.targets

    def _record(
        self, adapter: NseIngestSourceAdapter, outcome: FetchOutcome
    ) -> int:
        """Every attempt is written down, including the failures.

        A source bot-blocked for a week is a fact the coverage report needs; recording
        only successes makes a week-long outage look like a week nobody asked.
        """
        return self._store.record_fetch(
            source_name=adapter.source_name,
            url=outcome.target.url,
            fetched_at=outcome.fetched_at,
            fetch_status=outcome.status.value,
            evidence=outcome.evidence,
            attempts=outcome.attempts,
            http_status=outcome.http_status,
            payload_sha256=outcome.content_sha256,
            payload_bytes=len(outcome.payload) if outcome.payload else 0,
        )
