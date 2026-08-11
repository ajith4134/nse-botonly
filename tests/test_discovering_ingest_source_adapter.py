"""Two-phase discover-then-fetch: memory, then asking, then guessing — in that order.

The property under test throughout is that the three ways of arriving at fetch targets
stay distinguishable. A run that remembered, a run that asked, and a run that guessed
produce identical-looking target lists but differ in request cost by three orders of
magnitude, so collapsing them would hide the entire point of the primitive.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.discovering_ingest_source_adapter import (
    DiscoveredParameter,
    DiscoveredParameterStore,
    DiscoveringNseIngestSourceAdapter,
    DiscoveryOutcome,
    DiscoveryResult,
    UnsafeDiscoveredParameterError,
    discovery_result_from_json_list,
    plan_targets_with_discovery,
    validity_horizon_from_dates,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    IngestRow,
    SourceCoverageFloor,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget

SOURCE = "option_chain_probe"
TODAY = date(2026, 8, 11)
LADDER_WIDTH = 40


class ExpiryDiscoveringAdapter:
    """Mirrors L0.27's real shape: needs an expiry it cannot know, guesses if it must."""

    def __init__(self, ladder_width: int = LADDER_WIDTH) -> None:
        self._ladder_width = ladder_width

    @property
    def source_name(self) -> str:
        return SOURCE

    @property
    def coverage_floor(self) -> SourceCoverageFloor:
        return SourceCoverageFloor(date(2000, 6, 12), established_by="F&O launch")

    def discovery_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        return [FetchTarget(url="https://x/api/expiries", source_name=SOURCE)]

    def parse_discovery(self, payload: bytes, target: FetchTarget) -> DiscoveryResult:
        return discovery_result_from_json_list(
            payload, "expiryDates", "expiry", TODAY, date_format="%d-%b-%Y"
        )

    def fetch_targets_from_discovery(
        self, for_dates: Sequence[date], discovered: Sequence[DiscoveredParameter]
    ) -> Sequence[FetchTarget]:
        return [
            FetchTarget(
                url=f"https://x/api/chain?expiry={parameter.parameter_value}",
                source_name=SOURCE,
                expects=max(for_dates).isoformat(),
            )
            for parameter in discovered
        ]

    def fetch_targets(self, for_dates: Sequence[date]) -> Sequence[FetchTarget]:
        """The expensive fallback ladder — one request per guessed expiry."""
        return [
            FetchTarget(
                url=f"https://x/api/chain?expiry=guess-{index}",
                source_name=SOURCE,
                expects=max(for_dates).isoformat(),
            )
            for index in range(self._ladder_width)
        ]

    def parse(self, payload: bytes, target: FetchTarget) -> Sequence[IngestRow]:
        return [IngestRow({"x": 1}, TODAY, ("A",))]

    def content_mismatch_reason(self, payload: bytes, target: FetchTarget) -> str | None:
        return None


@pytest.fixture
def memo(tmp_path: Path) -> Iterator[DiscoveredParameterStore]:
    with DiscoveredParameterStore(tmp_path / "discovery.sqlite3") as opened:
        yield opened


def _expiry_payload(*expiries: str) -> bytes:
    return json.dumps({"expiryDates": list(expiries)}).encode()


# ------------------------------------------------------------------ validity


@pytest.mark.unit
def test_validity_ends_at_the_nearest_future_date_not_the_furthest() -> None:
    """An expiry list stops being current the moment its front expiry passes, because
    the exchange adds a new far one by then. Using the furthest date would keep serving
    a list missing its front month for weeks."""
    horizon = validity_horizon_from_dates(
        [date(2026, 8, 25), date(2026, 8, 18), date(2026, 9, 29)], discovered_on=TODAY
    )
    assert horizon == date(2026, 8, 18)


@pytest.mark.adversarial
def test_a_set_with_no_future_date_is_trusted_no_further_than_today() -> None:
    assert validity_horizon_from_dates([date(2026, 8, 1)], discovered_on=TODAY) == TODAY
    assert validity_horizon_from_dates([], discovered_on=TODAY) == TODAY


# --------------------------------------------------------------------- memo


@pytest.mark.unit
def test_a_remembered_parameter_is_reused_and_costs_no_requests(
    memo: DiscoveredParameterStore,
) -> None:
    adapter = ExpiryDiscoveringAdapter()
    memo.remember(
        SOURCE,
        [DiscoveredParameter("expiry", "28-Aug-2026", date(2026, 8, 28))],
        datetime.now(UTC),
    )
    plan = plan_targets_with_discovery(adapter, [TODAY], memo, today=TODAY)
    assert plan.reused_memo
    assert not plan.used_fallback
    assert plan.request_count == 1
    assert "28-Aug-2026" in plan.targets[0].url


@pytest.mark.unit
def test_an_expired_parameter_is_not_reused(memo: DiscoveredParameterStore) -> None:
    adapter = ExpiryDiscoveringAdapter()
    memo.remember(
        SOURCE,
        [DiscoveredParameter("expiry", "04-Aug-2026", date(2026, 8, 4))],
        datetime.now(UTC),
    )
    plan = plan_targets_with_discovery(adapter, [TODAY], memo, today=TODAY)
    assert not plan.reused_memo
    assert plan.used_fallback


@pytest.mark.unit
def test_forgetting_expired_parameters_reports_how_many_went(
    memo: DiscoveredParameterStore,
) -> None:
    memo.remember(
        SOURCE,
        [
            DiscoveredParameter("expiry", "04-Aug-2026", date(2026, 8, 4)),
            DiscoveredParameter("expiry", "28-Aug-2026", date(2026, 8, 28)),
        ],
        datetime.now(UTC),
    )
    assert memo.forget_expired(TODAY) == 1
    assert len(memo.valid_for(SOURCE, TODAY)) == 1


@pytest.mark.unit
def test_rediscovering_replaces_rather_than_accumulates(
    memo: DiscoveredParameterStore,
) -> None:
    """A discovered parameter is a fact about now with no historical meaning — unlike an
    ingested row, last Tuesday's expiry list helps nobody and would make `valid_for`
    choose between stale candidates."""
    parameter = DiscoveredParameter("expiry", "28-Aug-2026", date(2026, 8, 28))
    memo.remember(SOURCE, [parameter], datetime.now(UTC))
    memo.remember(SOURCE, [parameter], datetime.now(UTC))
    assert len(memo.valid_for(SOURCE, TODAY)) == 1


# ---------------------------------------------------------------- discovery


@pytest.mark.unit
def test_discovery_beats_the_ladder_by_orders_of_magnitude(tmp_path: Path) -> None:
    """The entire justification for the primitive, stated as a test.

    Separate stores on purpose: sharing one would let the discovering run's memo serve
    the guessing run, which is correct behaviour but measures the wrong thing.
    """
    adapter = ExpiryDiscoveringAdapter()
    with DiscoveredParameterStore(tmp_path / "asked.sqlite3") as asked_store:
        discovered = adapter.parse_discovery(
            _expiry_payload("18-Aug-2026", "25-Aug-2026"),
            adapter.discovery_targets([TODAY])[0],
        )
        planned = plan_targets_with_discovery(
            adapter, [TODAY], asked_store, {"d": discovered}, today=TODAY
        )
    with DiscoveredParameterStore(tmp_path / "guessed.sqlite3") as guessing_store:
        guessed = plan_targets_with_discovery(adapter, [TODAY], guessing_store, today=TODAY)
    assert planned.request_count == 2
    assert not planned.used_fallback
    assert guessed.request_count == LADDER_WIDTH
    assert guessed.used_fallback


@pytest.mark.adversarial
def test_an_empty_answer_is_not_a_failure_and_stops_the_ladder(
    memo: DiscoveredParameterStore,
) -> None:
    """HTTP 200 with `data: []` is the source answering that nothing exists. Guessing
    past a clear answer is the ladder's worst case for no reason at all."""
    adapter = ExpiryDiscoveringAdapter()
    empty = adapter.parse_discovery(_expiry_payload(), adapter.discovery_targets([TODAY])[0])
    assert empty.outcome is DiscoveryOutcome.ANSWERED_EMPTY
    plan = plan_targets_with_discovery(adapter, [TODAY], memo, {"d": empty}, today=TODAY)
    assert plan.targets == ()
    assert not plan.used_fallback
    assert plan.outcome is DiscoveryOutcome.ANSWERED_EMPTY


@pytest.mark.adversarial
def test_an_unavailable_discovery_falls_back_and_says_it_fell_back(
    memo: DiscoveredParameterStore,
) -> None:
    """A run that guessed and a run that asked produce identical target lists but differ
    in cost by orders of magnitude. Hiding which happened defeats the primitive."""
    adapter = ExpiryDiscoveringAdapter()
    blocked = DiscoveryResult(outcome=DiscoveryOutcome.UNAVAILABLE, evidence="HTTP 403")
    plan = plan_targets_with_discovery(adapter, [TODAY], memo, {"d": blocked}, today=TODAY)
    assert plan.used_fallback
    assert plan.outcome is DiscoveryOutcome.UNAVAILABLE
    assert plan.request_count == LADDER_WIDTH
    assert "fell back" in plan.evidence


@pytest.mark.adversarial
def test_a_malformed_discovery_payload_raises_rather_than_answering_empty() -> None:
    """`ANSWERED_EMPTY` means the source said 'nothing exists'. Broken JSON says no such
    thing, and conflating them would let a parser fault masquerade as a real answer."""
    adapter = ExpiryDiscoveringAdapter()
    target = adapter.discovery_targets([TODAY])[0]
    for payload in (b"{not json", b"[]", json.dumps({"other": []}).encode()):
        with pytest.raises(IngestAdapterError):
            adapter.parse_discovery(payload, target)


@pytest.mark.adversarial
def test_a_discovered_value_that_is_not_a_date_raises() -> None:
    adapter = ExpiryDiscoveringAdapter()
    with pytest.raises(IngestAdapterError, match="not a date"):
        adapter.parse_discovery(
            _expiry_payload("not-a-date"), adapter.discovery_targets([TODAY])[0]
        )


@pytest.mark.unit
def test_discovered_parameters_carry_the_derived_horizon_not_a_ttl() -> None:
    adapter = ExpiryDiscoveringAdapter()
    result = adapter.parse_discovery(
        _expiry_payload("18-Aug-2026", "25-Aug-2026", "29-Sep-2026"),
        adapter.discovery_targets([TODAY])[0],
    )
    assert result.is_usable
    assert {parameter.valid_until for parameter in result.parameters} == {date(2026, 8, 18)}


@pytest.mark.unit
def test_discovery_is_remembered_so_the_next_run_costs_nothing(
    memo: DiscoveredParameterStore,
) -> None:
    adapter = ExpiryDiscoveringAdapter()
    discovered = adapter.parse_discovery(
        _expiry_payload("18-Aug-2026"), adapter.discovery_targets([TODAY])[0]
    )
    first = plan_targets_with_discovery(adapter, [TODAY], memo, {"d": discovered}, today=TODAY)
    second = plan_targets_with_discovery(adapter, [TODAY], memo, today=TODAY)
    assert not first.reused_memo
    assert second.reused_memo
    assert second.request_count == first.request_count


@pytest.mark.unit
def test_the_discovering_protocol_is_checkable_at_runtime() -> None:
    """A bare Protocol annotation is erased at runtime — a mistake already made once in
    this project and caught by review."""
    assert isinstance(ExpiryDiscoveringAdapter(), DiscoveringNseIngestSourceAdapter)


@pytest.mark.property
@pytest.mark.parametrize("ladder_width", [1, 5, 200, 2700])
def test_discovery_costs_exactly_the_real_parameters_while_the_ladder_costs_its_width(
    tmp_path: Path, ladder_width: int
) -> None:
    """The honest invariant, which is stronger than "discovery is cheaper".

    Discovery produces exactly one target per REAL parameter — no waste, whatever the
    ladder's width. The ladder produces its full width regardless of how many parameters
    actually exist, so all but a handful of its requests are known-useless before they
    are sent. At the real `L0.27` width of ~2,700 that is the whole problem.

    Note the comparison is NOT "discovery always yields fewer targets": against a
    1-wide ladder it can yield more, because two real expiries genuinely need two
    fetches. Cheapness is a consequence of realistic widths, not the invariant itself.
    """
    adapter = ExpiryDiscoveringAdapter(ladder_width=ladder_width)
    real_expiries = ("18-Aug-2026", "25-Aug-2026")
    with DiscoveredParameterStore(tmp_path / f"asked{ladder_width}.sqlite3") as asked_store:
        discovered = adapter.parse_discovery(
            _expiry_payload(*real_expiries), adapter.discovery_targets([TODAY])[0]
        )
        asked = plan_targets_with_discovery(
            adapter, [TODAY], asked_store, {"d": discovered}, today=TODAY
        )
    with DiscoveredParameterStore(tmp_path / f"guess{ladder_width}.sqlite3") as guess_store:
        guessed = plan_targets_with_discovery(adapter, [TODAY], guess_store, today=TODAY)

    assert asked.request_count == len(real_expiries)
    assert guessed.request_count == ladder_width
    assert guessed.used_fallback
    if ladder_width > len(real_expiries):
        assert asked.request_count < guessed.request_count


# ------------------------------- the one sink where remote data reaches a request


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "hostile_value",
    [
        "28-Aug-2026&symbol=EVIL",
        "28-Aug-2026#fragment",
        "../../etc/passwd",
        "https://attacker.example/x",
        "28-Aug-2026 OR 1=1",
        "28-Aug-2026\r\nHost: evil",
        "%2e%2e%2f",
        "x" * 200,
        "",
        "   ",
    ],
)
def test_a_discovered_value_that_could_reshape_a_request_is_refused(
    hostile_value: str,
) -> None:
    """Discovery is the ONLY place in the ingest core where remote data flows into URL
    construction — the value arrives in an NSE response and goes into the next fetch.
    Everything else builds URLs from our own dates and symbols."""
    with pytest.raises(UnsafeDiscoveredParameterError):
        DiscoveredParameter("expiry", hostile_value, date(2026, 8, 28))


@pytest.mark.unit
def test_real_parameter_values_are_accepted() -> None:
    """The guard must not reject what NSE actually sends."""
    for value in ("28-Aug-2026", "RELIANCE", "NIFTY", "NIFTY NEXT 50".replace(" ", "-")):
        assert DiscoveredParameter("expiry", value, date(2026, 12, 31)).parameter_value


@pytest.mark.adversarial
def test_a_hostile_payload_cannot_reach_the_memo_or_a_fetch_target() -> None:
    """End to end: the guard is at construction, so no path — parse, remember, plan —
    can carry an unsafe value through to a request."""
    adapter = ExpiryDiscoveringAdapter()
    hostile = json.dumps({"expiryDates": ["28-Aug-2026&symbol=EVIL"]}).encode()
    with pytest.raises((UnsafeDiscoveredParameterError, IngestAdapterError)):
        adapter.parse_discovery(hostile, adapter.discovery_targets([TODAY])[0])
