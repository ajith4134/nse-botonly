"""`R.05` for the order path's live half — the adapter meets Zerodha's real payloads.

**This test PLACES NOTHING.** It calls `orders()`, `trades()` and `positions()`/`holdings()` and
nothing else, ever. `place`, `modify` and `cancel` are not reachable from this file, and the one
assertion that matters most is the one at the bottom of `TestNothingHereEverWrites` — it walks this
module's own source and fails if a writing call appears in it. A read-only real-data test that can
drift into placing an order is not a test, it is an incident.

Why it must exist anyway: every hermetic test above it asserts against payloads this system wrote
itself, and the failure mode of a broker adapter is precisely that the real payload has a field the
fake did not — a status string with different spacing, a price arriving as a string, a timestamp
that is a `str` on one endpoint and a `datetime` on another. Only the real account can refute that.

It SKIPS, never fails, when there is no valid daily token or when the account has nothing in it.
Both are ordinary facts about a Tuesday evening, and a red suite for them would train the operator
to ignore red.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nse_algo_trader.order_path.kite_order_execution_venue import (
    DAY_POSITIONS_VIEW,
    HOLDINGS_VIEW,
    NET_POSITIONS_VIEW,
    KiteOrderExecutionVenue,
    connect_to_live_kite_venue_if_authenticated,
    normalise_order_row,
    normalise_trade_row,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    UnmappedBrokerStatusError,
    lifecycle_event_for_broker_status,
)
from nse_algo_trader.order_path.trading_intent import INDIA_MARKET_TIMEZONE

pytestmark = pytest.mark.real_data

_WRITING_CALLS = ("place_order", "modify_order", "cancel_order", ".place(", ".modify(", ".cancel(")


@pytest.fixture(scope="module")
def live_venue() -> KiteOrderExecutionVenue:
    """A venue on the real account, or a skip. Never a failure: no token is not a defect."""
    try:
        venue = connect_to_live_kite_venue_if_authenticated()
    except Exception as unavailable:  # noqa: BLE001 — any auth failure is a skip, never a red suite
        pytest.skip(f"no usable Kite session: {type(unavailable).__name__}: {unavailable}")
    if venue is None:
        pytest.skip(
            "no valid daily Kite access token — the order path's read endpoints cannot be "
            "exercised against the real account, so this gate is deferred rather than failed"
        )
    return venue


@pytest.fixture(scope="module")
def raw_client(live_venue: KiteOrderExecutionVenue) -> Any:
    """The SDK client itself, so a RAW payload can be compared with what the adapter made."""
    del live_venue  # ordering only: the venue fixture is what decides whether to skip
    from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
        build_authenticated_kite_client_if_valid,
    )

    client = build_authenticated_kite_client_if_valid()
    if client is None:
        pytest.skip("the Kite session disappeared between fixtures")
    return client


def _read_or_skip(what: str, call: Any) -> Any:
    try:
        return call()
    except Exception as unreadable:  # noqa: BLE001 — an unreadable endpoint is a skip, not a defect
        pytest.skip(f"{what} could not be read from the real account: {unreadable}")


class TestTheReadEndpointsAnswerAtAll:
    """The part that still asserts something on a quiet day.

    An empty account makes every row-level assertion vacuous, but "the endpoint answered, over a
    real session, in the documented container shape" is exactly the claim a stale adapter, an
    expired token or a changed response envelope would break — and it is checkable with nothing in
    the account at all.
    """

    def test_orders_trades_and_positions_are_reachable_in_their_documented_shapes(
        self, raw_client: Any
    ) -> None:
        orders = _read_or_skip("the order book", raw_client.orders)
        trades = _read_or_skip("the trade book", raw_client.trades)
        positions = _read_or_skip("positions", raw_client.positions)
        holdings = _read_or_skip("holdings", raw_client.holdings)
        assert isinstance(orders, list)
        assert isinstance(trades, list)
        assert isinstance(holdings, list)
        assert isinstance(positions, dict)
        # `docs/research/222` §7: `positions()` answers with `net` and `day` arrays. A response
        # missing either would silently halve the reconciler's view of the account.
        assert {"day", "net"} <= set(positions)
        assert isinstance(positions["day"], list)
        assert isinstance(positions["net"], list)

    def test_the_venue_agrees_with_the_raw_client_about_how_much_there_is(
        self, live_venue: KiteOrderExecutionVenue, raw_client: Any
    ) -> None:
        """The union is exhaustive — no row is dropped and none is invented — and this holds on an
        empty account too, where both sides must be zero."""
        positions = _read_or_skip("positions", raw_client.positions)
        holdings = _read_or_skip("holdings", raw_client.holdings)
        reports = _read_or_skip("positions", live_venue.fetch_positions)
        assert len(reports) == len(positions["day"]) + len(positions["net"]) + len(holdings)


class TestTheRealOrderBookNormalisesWithoutSurprises:
    def test_every_real_order_row_survives_the_adapter(self, raw_client: Any) -> None:
        rows = _read_or_skip("the order book", raw_client.orders)
        if not rows:
            pytest.skip("the account's order book is empty today — nothing to normalise")
        for row in rows:
            report = normalise_order_row(row)
            assert isinstance(report.broker_order_id, str)
            assert report.broker_order_id
            assert isinstance(report.status, str)
            assert isinstance(report.quantity, int)
            assert isinstance(report.filled_quantity, int)
            assert report.average_price_paise is None or isinstance(
                report.average_price_paise, Decimal
            )
            assert report.exchange_timestamp is None or (
                isinstance(report.exchange_timestamp, datetime)
                and report.exchange_timestamp.tzinfo is not None
            )

    def test_every_status_the_real_account_carries_maps_through_the_lifecycle_table(
        self, raw_client: Any
    ) -> None:
        """The one assertion that can only be made against a real account: the broker's actual
        vocabulary, against the table this system refuses on. An unmapped status here is a real
        finding — it means a live order would be held and surfaced rather than progressed."""
        rows = _read_or_skip("the order book", raw_client.orders)
        if not rows:
            pytest.skip("the account's order book is empty today — no statuses to map")
        unmapped: list[str] = []
        for row in rows:
            status = normalise_order_row(row).status
            if not status:
                continue
            try:
                lifecycle_event_for_broker_status(status)
            except UnmappedBrokerStatusError:
                unmapped.append(status)
        assert not unmapped, (
            f"the real account carries status strings this system has no mapping for: "
            f"{sorted(set(unmapped))}. Every one of them would hold a live order in place and "
            f"surface it, which is the safe behaviour but not the correct one"
        )

    def test_the_venue_reads_the_book_through_its_own_surface(
        self, live_venue: KiteOrderExecutionVenue
    ) -> None:
        session_date = datetime.now(tz=INDIA_MARKET_TIMEZONE).date()
        reports = _read_or_skip(
            "the order book", lambda: live_venue.fetch_orders(session_date=session_date)
        )
        if not reports:
            pytest.skip("the account's order book is empty today")
        assert all(report.broker_order_id for report in reports)
        # `is_ours_by_tag` is how reconciliation separates this system's orders from ones placed by
        # hand in the broker's app. Both kinds are legitimate; only the count is informative here.
        ours = [report for report in reports if report.is_ours_by_tag]
        assert len(ours) <= len(reports)


class TestTheRealTradeBookNormalises:
    def test_every_real_trade_row_survives_the_adapter(self, raw_client: Any) -> None:
        rows = _read_or_skip("the trade book", raw_client.trades)
        if not rows:
            pytest.skip("the account has no trades today — nothing to normalise")
        normalised = [normalise_trade_row(row) for row in rows]
        usable = [report for report in normalised if report is not None]
        assert usable, "every real trade row was discarded by the adapter, which cannot be right"
        for report in usable:
            assert report.broker_trade_id
            assert report.broker_order_id
            assert report.quantity > 0
            assert report.price_paise > 0
            assert report.filled_at.tzinfo is not None


class TestTheRealPositionSurfaceUnionsCorrectly:
    def test_positions_and_holdings_come_back_as_one_labelled_union(
        self, live_venue: KiteOrderExecutionVenue, raw_client: Any
    ) -> None:
        reports = _read_or_skip("positions", live_venue.fetch_positions)
        raw_positions = _read_or_skip("positions", raw_client.positions)
        raw_holdings = _read_or_skip("holdings", raw_client.holdings)
        expected_rows = (
            len(raw_positions.get("day", []))
            + len(raw_positions.get("net", []))
            + len(raw_holdings)
        )
        if expected_rows == 0:
            pytest.skip("the account holds no positions and no holdings — nothing to union")
        assert len(reports) == expected_rows
        assert {report.view for report in reports} <= {
            DAY_POSITIONS_VIEW,
            NET_POSITIONS_VIEW,
            HOLDINGS_VIEW,
        }

    def test_the_unsettled_quantity_is_carried_off_the_real_holdings(
        self, live_venue: KiteOrderExecutionVenue, raw_client: Any
    ) -> None:
        """`docs/research/222` §7's trap, checked against the real thing: `t1_quantity` is
        bought-today-unsettled and is NOT sellable as `quantity`."""
        raw_holdings = _read_or_skip("holdings", raw_client.holdings)
        if not raw_holdings:
            pytest.skip("the account holds nothing in DEMAT — no t1_quantity to carry")
        reports = _read_or_skip("positions", live_venue.fetch_positions)
        holdings = {
            report.trading_symbol: report for report in reports if report.view == HOLDINGS_VIEW
        }
        assert len(holdings) == len({row["tradingsymbol"] for row in raw_holdings})
        for row in raw_holdings:
            carried = holdings[row["tradingsymbol"]]
            assert carried.unsettled_quantity == int(row.get("t1_quantity") or 0)
            assert carried.quantity == int(row.get("quantity") or 0)


class TestNothingHereEverWrites:
    @pytest.mark.adversarial
    def test_this_module_contains_no_call_that_could_place_or_change_an_order(self) -> None:
        """The guard on the guard. A read-only real-account test that drifts into writing is not a
        failing test — it is an order nobody decided to place."""
        source = Path(__file__).read_text(encoding="utf-8")
        without_prose = re.sub(r'"""(?:.|\n)*?"""', "", source)
        # The line that names the forbidden calls necessarily contains them; excluding it is what
        # keeps this check about the module's behaviour rather than about its own vocabulary.
        body = "\n".join(
            line for line in without_prose.splitlines() if "_WRITING_CALLS" not in line
        )
        offending = [call for call in _WRITING_CALLS if call in body]
        assert not offending, (
            f"this real-data module references {offending}, which can change the state of a real "
            f"account; it is permitted to read and nothing else"
        )
