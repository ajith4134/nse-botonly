"""`bulk_block_deals` certified through the conformance suite, on REAL captured payloads.

Both fixtures are bytes fetched live from `archives.nseindia.com` on 2026-08-11 and
stored under `tests/fixtures_nse_ingest/` — `bulk_deals_10082026.csv` (150 real rows,
including the same-client-same-day collision that forced the natural-key design) and
`block_deals_10082026.csv` (2 real rows, one BUY and one matching SELL).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.nse_ingest.bulk_block_deals_adapter import (
    BLOCK_DEALS_CATEGORY,
    BLOCK_DEALS_URL,
    BULK_DEALS_CATEGORY,
    BULK_DEALS_URL,
    BulkBlockDealsAdapter,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

DEAL_DAY = date(2026, 8, 10)
BULK_ROW_COUNT = 150
BLOCK_ROW_COUNT = 2
REPEAT_CLIENT_SYMBOL = "ATALREAL"
REPEAT_CLIENT_NAME = "VISHAL MAHESH WAGHELA"
DISTINCT_SIDES_FOR_REPEAT_CLIENT = 2
TRADES_PER_SIDE_FOR_REPEAT_CLIENT = 2


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(url: str, source: str, expects: str = "") -> FetchTarget:
    return FetchTarget(url=url, source_name=source, expects=expects)


class TestBulkDealsAdapterCertifies(NseIngestAdapterConformance):
    """Bulk deals — 150 real rows, including the client that traded the same side twice."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(BULK_DEALS_URL, "bulk_block_deals_bulk"),
                _fixture("bulk_deals_10082026.csv"),
                BULK_ROW_COUNT,
            )
        ]


class TestBlockDealsAdapterCertifies(NseIngestAdapterConformance):
    """Block deals — the smaller, Remarks-less sibling schema."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return BulkBlockDealsAdapter(BLOCK_DEALS_CATEGORY)

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target(BLOCK_DEALS_URL, "bulk_block_deals_block"),
                _fixture("block_deals_10082026.csv"),
                BLOCK_ROW_COUNT,
            )
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_the_rolling_source_cannot_serve_a_past_date() -> None:
    """Same shape as `fo_ban_list`: constructing a plausible per-date URL would return
    today's file mislabelled as that date, so a past-date request is refused, not faked."""
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    targets = adapter.fetch_targets([date(2026, 8, 1), date(2026, 8, 2)])
    assert len(targets) == 1
    assert targets[0].url == BULK_DEALS_URL


@pytest.mark.unit
def test_no_dates_requested_yields_no_targets() -> None:
    assert BulkBlockDealsAdapter(BULK_DEALS_CATEGORY).fetch_targets([]) == []


@pytest.mark.unit
def test_bulk_and_block_are_distinct_sources_sharing_no_state() -> None:
    """The two categories share a class but must never be confused for one another —
    different regulatory thresholds, different files, different source partitions."""
    bulk = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    block = BulkBlockDealsAdapter(BLOCK_DEALS_CATEGORY)
    assert bulk.source_name == "bulk_block_deals_bulk"
    assert block.source_name == "bulk_block_deals_block"
    assert bulk.coverage_floor.earliest_date != block.coverage_floor.earliest_date


@pytest.mark.unit
def test_every_real_bulk_row_is_dated_from_its_own_row() -> None:
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    rows = adapter.parse(
        _fixture("bulk_deals_10082026.csv"), _target(BULK_DEALS_URL, "bulk_block_deals_bulk")
    )
    assert {row.effective_date for row in rows} == {DEAL_DAY}
    assert all(row.values["deal_category"] == "bulk" for row in rows)


@pytest.mark.adversarial
def test_the_same_client_trading_the_same_side_twice_does_not_collapse() -> None:
    """The real fact that forced the natural-key design: a naive `(date, symbol, client,
    side)` key collides for this client — quantity and price are what separate them."""
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    rows = adapter.parse(
        _fixture("bulk_deals_10082026.csv"), _target(BULK_DEALS_URL, "bulk_block_deals_bulk")
    )
    repeat_client_rows = [
        row
        for row in rows
        if row.values["Symbol"] == REPEAT_CLIENT_SYMBOL
        and row.values["Client Name"] == REPEAT_CLIENT_NAME
    ]
    sides = {row.values["Buy/Sell"] for row in repeat_client_rows}
    assert len(sides) == DISTINCT_SIDES_FOR_REPEAT_CLIENT
    for side in sides:
        same_side = [row for row in repeat_client_rows if row.values["Buy/Sell"] == side]
        assert len(same_side) == TRADES_PER_SIDE_FOR_REPEAT_CLIENT
    keys = {row.natural_key for row in repeat_client_rows}
    assert len(keys) == len(repeat_client_rows), (
        "quantity+price failed to separate same-client same-side trades — rows would "
        "collapse into revisions of one another and be lost"
    )


@pytest.mark.adversarial
def test_an_unrecognised_buy_sell_value_raises() -> None:
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    payload = (
        b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
        b"Trade Price / Wght. Avg. Price,Remarks\n"
        b"10-AUG-2026,FOO,Foo Ltd,SOME CLIENT,HOLD,100,10.00,-\n"
    )
    with pytest.raises(IngestAdapterError, match="Buy/Sell"):
        adapter.parse(payload, _target(BULK_DEALS_URL, "bulk_block_deals_bulk"))


@pytest.mark.adversarial
def test_the_block_schema_rejects_a_bulk_shaped_payload() -> None:
    """Block has no Remarks column. A bulk file fed to the block adapter must fail the
    header check rather than silently reading seven of the eight columns."""
    adapter = BulkBlockDealsAdapter(BLOCK_DEALS_CATEGORY)
    with pytest.raises(IngestAdapterError, match="does not match"):
        adapter.parse(
            _fixture("bulk_deals_10082026.csv"), _target(BULK_DEALS_URL, "bulk_block_deals_block")
        )


@pytest.mark.unit
def test_an_empty_deals_file_is_valid_not_broken() -> None:
    """No trade crossed the threshold that day is a real and common state, distinguished
    from a broken payload by the header validating cleanly, never by the row count."""
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    header_only = (
        b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
        b"Trade Price / Wght. Avg. Price,Remarks\n"
    )
    rows = adapter.parse(header_only, _target(BULK_DEALS_URL, "bulk_block_deals_bulk"))
    assert rows == []


@pytest.mark.unit
def test_content_mismatch_reason_accepts_the_real_payloads_own_date() -> None:
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    target = _target(BULK_DEALS_URL, "bulk_block_deals_bulk", expects=DEAL_DAY.isoformat())
    assert adapter.content_mismatch_reason(_fixture("bulk_deals_10082026.csv"), target) is None


@pytest.mark.adversarial
def test_content_mismatch_reason_rejects_a_date_the_file_does_not_carry() -> None:
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    target = _target(BULK_DEALS_URL, "bulk_block_deals_bulk", expects="2026-01-01")
    reason = adapter.content_mismatch_reason(_fixture("bulk_deals_10082026.csv"), target)
    assert reason is not None
    assert "rolling file" in reason


@pytest.mark.unit
def test_content_mismatch_reason_cannot_assert_anything_about_a_dateless_empty_file() -> None:
    """Documented limitation: a header-only, zero-row payload carries no date anywhere,
    so the check can neither confirm nor deny a match — it returns `None`, not a guess."""
    adapter = BulkBlockDealsAdapter(BULK_DEALS_CATEGORY)
    header_only = (
        b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
        b"Trade Price / Wght. Avg. Price,Remarks\n"
    )
    target = _target(BULK_DEALS_URL, "bulk_block_deals_bulk", expects="2026-08-10")
    assert adapter.content_mismatch_reason(header_only, target) is None
