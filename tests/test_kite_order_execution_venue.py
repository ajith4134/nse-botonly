"""The live venue's obligations — the classification, the units, and the refusals before the wire.

Everything here runs against a FAKE client. No network, no session, no order. The exception
classification is tested against the SDK's REAL exception classes (importing `kiteconnect` is safe;
constructing one of its exceptions is not a network call), because the whole value of the
classification is that it is right about the objects Zerodha's SDK actually raises.

The load-bearing tests are the ones that pin the three-way distinction. A rejection wrongly called
retryable is a duplicated live order; an unknown outcome wrongly called a rejection is a live
position this system has forgotten about. Both are silent, and both are what the table under test
exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import requests
from kiteconnect.exceptions import (
    DataException,
    GeneralException,
    InputException,
    KiteException,
    NetworkException,
    OrderException,
    PermissionException,
    TokenException,
)

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderProduct,
    OrderType,
    OrderValidity,
    OrderVariety,
)
from nse_algo_trader.order_path.kite_order_execution_venue import (
    BROKER_DEFAULT_MARKET_PROTECTION,
    DAY_POSITIONS_VIEW,
    EXCHANGE_ALGO_IDENTIFIER_ENV_VAR,
    HOLDINGS_VIEW,
    MAXIMUM_MODIFICATIONS_PER_ORDER,
    NET_POSITIONS_VIEW,
    FacilityNotAvailableToAlgoFlowError,
    KiteOrderExecutionVenue,
    OrderExpressionNotEncodableError,
    PastSessionNotAvailableFromLiveVenueError,
    SimulatedOrderRefusedByLiveVenueError,
    classify_kite_exception,
    exchange_algo_identifier_from_environment,
    normalise_order_row,
    normalise_trade_row,
    paise_to_rupees,
    rupees_to_paise,
)
from nse_algo_trader.order_path.order_execution_venue import (
    OrderExecutionVenue,
    VenueOutcomeUnknownError,
    VenueRejectedError,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_lifecycle_state_machine import (
    lifecycle_event_for_broker_status,
)
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import OrderNamespace, TradingIntent
from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

pytestmark = [pytest.mark.unit, pytest.mark.hermetic]

_DECIDED_AT = datetime(2026, 8, 13, 4, 15, tzinfo=UTC)  # 09:45 IST — inside the session
_A_REAL_KITE_ORDER_ID = "250813001234567"


# SDK 5.2.1 does not define `MarginException`, but `connect.py` builds its exception class from the
# server's own `error_type` string, so Zerodha can still name it. Built here with `type()` — rather
# than as a `class` statement — because the SDK ships no type information, so its classes are `Any`
# to mypy and cannot be subclassed under `--strict`. Its purpose is to prove the venue classifies by
# NAME and therefore handles a class the installed SDK has never heard of.
MarginException = type("MarginException", (KiteException,), {})


class _FakeKiteClient:
    """A stand-in for `KiteConnect`, recording what it was asked and answering what it was told to.

    Deliberately not a `Mock`: an auto-speccing mock answers every attribute, so a venue calling a
    method that does not exist on the real client would pass. This object only has the methods the
    real one has.
    """

    def __init__(
        self,
        *,
        order_id: str = _A_REAL_KITE_ORDER_ID,
        raises: Exception | None = None,
        orders_payload: Any = (),
        trades_payload: Any = (),
        positions_payload: Any = None,
        holdings_payload: Any = (),
    ) -> None:
        self.order_id = order_id
        self.raises = raises
        self.orders_payload = orders_payload
        self.trades_payload = trades_payload
        self.positions_payload = positions_payload if positions_payload is not None else {}
        self.holdings_payload = holdings_payload
        self.place_order_calls: list[dict[str, Any]] = []
        self.modify_order_calls: list[dict[str, Any]] = []
        self.cancel_order_calls: list[dict[str, Any]] = []
        self.order_history_calls: list[str] = []

    def place_order(self, **parameters: Any) -> str:
        self.place_order_calls.append(parameters)
        if self.raises is not None:
            raise self.raises
        return self.order_id

    def modify_order(self, **parameters: Any) -> str:
        self.modify_order_calls.append(parameters)
        if self.raises is not None:
            raise self.raises
        return self.order_id

    def cancel_order(self, **parameters: Any) -> str:
        self.cancel_order_calls.append(parameters)
        if self.raises is not None:
            raise self.raises
        return self.order_id

    def orders(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.orders_payload

    def order_history(self, order_id: str) -> Any:
        self.order_history_calls.append(order_id)
        if self.raises is not None:
            raise self.raises
        return self.orders_payload

    def trades(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.trades_payload

    def positions(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.positions_payload

    def holdings(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.holdings_payload


def _intent(quantity: int = 100, symbol: str = "RELIANCE") -> TradingIntent:
    return TradingIntent(
        strategy_identity="calibrated_mean_reversion.v1",
        instrument_token=738561,
        trading_symbol=symbol,
        segment=ChargeableSegment.EQUITY_INTRADAY,
        side=TradeLeg.BUY,
        quantity=quantity,
        decided_at=_DECIDED_AT,
        reference_price_paise=Decimal("281135"),
        expected_edge_bps=Decimal("34.1"),
    )


def _marketable_limit() -> OrderExpression:
    return OrderExpression(
        variety=OrderVariety.REGULAR,
        product=OrderProduct.INTRADAY,
        order_type=OrderType.LIMIT,
        validity=OrderValidity.DAY,
        limit_price_paise=Decimal("281135"),
        chosen_because="MARKET is barred for algo flow (research/223 §5)",
    )


def _order(
    expression: OrderExpression | None = None,
    *,
    namespace: OrderNamespace = OrderNamespace.LIVE,
    quantity: int = 100,
) -> OrderRecord:
    return OrderRecord.from_intent(
        _intent(quantity=quantity),
        expression or _marketable_limit(),
        namespace,
        at=_DECIDED_AT,
    )


class TestTheVenueSatisfiesTheOneInterface:
    def test_it_is_an_order_execution_venue(self) -> None:
        venue = KiteOrderExecutionVenue(_FakeKiteClient())
        assert isinstance(venue, OrderExecutionVenue)
        assert venue.venue_name == "zerodha_kite"


class TestPaiseAndRupeesConvertAtThisBoundaryAndNowhereElse:
    def test_rupees_become_paise_without_binary_float_noise(self) -> None:
        """`Decimal(2811.35)` is 2811.34999999999990905…; going through `str` is what keeps the
        hundredth of a rupee exact."""
        assert rupees_to_paise(2811.35) == Decimal("281135")
        assert rupees_to_paise("2811.35") == Decimal("281135")
        assert rupees_to_paise(Decimal("0.05")) == Decimal("5")

    def test_paise_become_rupees_for_the_wire(self) -> None:
        assert paise_to_rupees(Decimal("281135")) == 2811.35
        assert paise_to_rupees(Decimal("5")) == 0.05

    def test_the_round_trip_is_the_identity(self) -> None:
        for paise in (Decimal("1"), Decimal("5"), Decimal("281135"), Decimal("99999995")):
            assert rupees_to_paise(paise_to_rupees(paise)) == paise

    @pytest.mark.adversarial
    def test_a_zero_price_is_absence_and_never_a_number(self) -> None:
        """Kite reports `average_price: 0` for an order that has not traded. A zero carried into
        the fill ledger as a price would drag the whole order's average towards zero."""
        assert rupees_to_paise(0) is None
        assert rupees_to_paise(0.0) is None
        assert rupees_to_paise(None) is None

    def test_the_limit_price_reaches_the_wire_in_rupees(self) -> None:
        client = _FakeKiteClient()
        KiteOrderExecutionVenue(client).place(_order())
        assert client.place_order_calls[0]["price"] == 2811.35


class TestTheRefusalsThatHappenBeforeTheWire:
    def test_a_market_order_is_refused_here_and_no_call_is_made(self) -> None:
        """`docs/research/223` §5 — NSE/MSD/67753: algo orders may not be MARKET. The refusal is
        read out of the dated fact table, so the circular is in the message rather than in a
        comment beside an `if`."""
        client = _FakeKiteClient()
        market = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.MARKET,
            validity=OrderValidity.DAY,
        )
        with pytest.raises(FacilityNotAvailableToAlgoFlowError) as refusal:
            KiteOrderExecutionVenue(client).place(_order(market))
        assert client.place_order_calls == []
        assert "NSE/MSD/67753" in str(refusal.value)
        assert "market orders may not carry algo-originated flow" in str(refusal.value)

    def test_the_market_refusal_is_a_rejection_so_the_placer_treats_it_as_a_fact(self) -> None:
        """It must NOT be retryable and must NOT be ambiguous: no order exists, and the placer's
        `except VenueRejectedError` is what records that."""
        assert issubclass(FacilityNotAvailableToAlgoFlowError, VenueRejectedError)

    @pytest.mark.adversarial
    def test_a_withdrawn_variety_is_refused_with_its_source(self) -> None:
        """Bracket orders were withdrawn in March 2020 (`docs/research/222` §6)."""
        client = _FakeKiteClient()
        bracket = OrderExpression(
            variety=OrderVariety.BRACKET,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=Decimal("281135"),
        )
        with pytest.raises(FacilityNotAvailableToAlgoFlowError) as refusal:
            KiteOrderExecutionVenue(client).place(_order(bracket))
        assert client.place_order_calls == []
        assert "withdrawn" in str(refusal.value)

    @pytest.mark.adversarial
    def test_a_simulated_order_never_reaches_the_live_wire(self) -> None:
        client = _FakeKiteClient()
        with pytest.raises(SimulatedOrderRefusedByLiveVenueError):
            KiteOrderExecutionVenue(client).place(_order(namespace=OrderNamespace.SIMULATED))
        assert client.place_order_calls == []

    @pytest.mark.adversarial
    def test_an_iceberg_whose_legs_do_not_divide_its_quantity_is_refused(self) -> None:
        """Kite takes a per-leg quantity. Rounding one off this module's own initiative would
        change the size actually sent."""
        client = _FakeKiteClient()
        iceberg = OrderExpression(
            variety=OrderVariety.ICEBERG,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=Decimal("281135"),
            iceberg_legs=7,
        )
        with pytest.raises(OrderExpressionNotEncodableError):
            KiteOrderExecutionVenue(client).place(_order(iceberg, quantity=100))
        assert client.place_order_calls == []

    def test_an_iceberg_that_divides_evenly_carries_both_leg_fields(self) -> None:
        client = _FakeKiteClient()
        iceberg = OrderExpression(
            variety=OrderVariety.ICEBERG,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=Decimal("281135"),
            iceberg_legs=5,
        )
        KiteOrderExecutionVenue(client).place(_order(iceberg, quantity=100))
        sent = client.place_order_calls[0]
        assert sent["iceberg_legs"] == 5
        assert sent["iceberg_quantity"] == 20

    @pytest.mark.adversarial
    def test_a_zero_market_protection_is_refused_rather_than_sent(self) -> None:
        """`docs/research/222` §6: Kite rejects `market_protection=0` outright."""
        client = _FakeKiteClient()
        stop_market = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.STOP_LOSS_MARKET,
            validity=OrderValidity.DAY,
            trigger_price_paise=Decimal("280000"),
            market_protection_percent=0,
        )
        with pytest.raises(OrderExpressionNotEncodableError):
            KiteOrderExecutionVenue(client).place(_order(stop_market))
        assert client.place_order_calls == []


class TestWhatGoesOnTheWire:
    def test_the_tag_is_passed_through_unchanged(self) -> None:
        """The tag is the only field this system controls and the only handle a crashed process
        has for finding its own order. A tag this module edited would not be found."""
        client = _FakeKiteClient()
        order = _order()
        KiteOrderExecutionVenue(client).place(order)
        assert client.place_order_calls[0]["tag"] == order.broker_tag
        assert len(order.broker_tag) == 20
        assert order.broker_tag.startswith(OrderNamespace.LIVE.value)

    def test_the_ordinary_fields_are_kites_own_vocabulary(self) -> None:
        client = _FakeKiteClient()
        KiteOrderExecutionVenue(client).place(_order())
        sent = client.place_order_calls[0]
        assert sent["variety"] == "regular"
        assert sent["exchange"] == "NSE"
        assert sent["tradingsymbol"] == "RELIANCE"
        assert sent["transaction_type"] == "BUY"
        assert sent["product"] == "MIS"
        assert sent["order_type"] == "LIMIT"
        assert sent["validity"] == "DAY"
        assert sent["quantity"] == 100

    def test_the_venue_returns_the_brokers_own_order_id(self) -> None:
        venue = KiteOrderExecutionVenue(_FakeKiteClient(order_id=_A_REAL_KITE_ORDER_ID))
        assert venue.place(_order()) == _A_REAL_KITE_ORDER_ID

    def test_the_algo_identifier_is_sent_when_one_is_configured(self) -> None:
        client = _FakeKiteClient()
        venue = KiteOrderExecutionVenue(client, exchange_algo_identifier="4444444444440")
        venue.place(_order())
        assert client.place_order_calls[0]["algo_id"] == "4444444444440"
        assert venue.carries_exchange_algo_identifier

    def test_absent_means_absent_and_the_field_is_omitted(self) -> None:
        """`docs/research/223` §4: the EXCHANGE assigns the identifier. A fabricated one is worse
        than none, because it is indistinguishable from a real one in the exchange's records."""
        client = _FakeKiteClient()
        venue = KiteOrderExecutionVenue(client, exchange_algo_identifier=None)
        venue.place(_order())
        assert "algo_id" not in client.place_order_calls[0]
        assert not venue.carries_exchange_algo_identifier

    def test_the_identifier_is_read_from_the_environment_and_never_hardcoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR, raising=False)
        assert exchange_algo_identifier_from_environment() is None
        monkeypatch.setenv(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR, " 4444444444442 ")
        assert exchange_algo_identifier_from_environment() == "4444444444442"
        client = _FakeKiteClient()
        KiteOrderExecutionVenue(client).place(_order())
        assert client.place_order_calls[0]["algo_id"] == "4444444444442"

    @pytest.mark.adversarial
    def test_an_empty_environment_value_is_absence_not_an_empty_identifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR, "   ")
        assert exchange_algo_identifier_from_environment() is None

    def test_market_protection_is_set_where_the_order_type_requires_it(self) -> None:
        """`docs/research/222` §6 — mandatory non-zero for SL-M; `-1` asks for the broker default
        rather than asserting a percentage this system has no basis to choose."""
        client = _FakeKiteClient()
        stop_market = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.STOP_LOSS_MARKET,
            validity=OrderValidity.DAY,
            trigger_price_paise=Decimal("280000"),
        )
        KiteOrderExecutionVenue(client).place(_order(stop_market))
        sent = client.place_order_calls[0]
        assert sent["market_protection"] == BROKER_DEFAULT_MARKET_PROTECTION
        assert sent["trigger_price"] == 2800.0

    def test_a_limit_order_carries_no_market_protection(self) -> None:
        client = _FakeKiteClient()
        KiteOrderExecutionVenue(client).place(_order())
        assert "market_protection" not in client.place_order_calls[0]

    def test_a_time_to_live_validity_carries_its_minutes(self) -> None:
        client = _FakeKiteClient()
        ttl = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.TIME_TO_LIVE,
            limit_price_paise=Decimal("281135"),
            validity_minutes=7,
        )
        KiteOrderExecutionVenue(client).place(_order(ttl))
        assert client.place_order_calls[0]["validity_ttl"] == 7


class TestEverySdkExceptionIsClassifiedIntoExactlyOneOutcome:
    """The map in `docs/research/222` §8, and the failure modes of getting it wrong.

    `VenueUnavailableError` is the ONLY outcome the placer retries. Anything mis-filed into it is
    an order that can be sent twice.
    """

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            # never reached the venue — safe to send again
            (NetworkException("OMS is down", code=503), VenueUnavailableError),
            (requests.exceptions.ConnectionError("connection refused"), VenueUnavailableError),
            (requests.exceptions.ConnectTimeout("handshake timed out"), VenueUnavailableError),
            (requests.exceptions.SSLError("tls failed"), VenueUnavailableError),
            (ConnectionResetError("reset by peer"), VenueUnavailableError),
            # sent, and the answer is unreadable — an order MAY exist
            (DataException("garbled response", code=502), VenueOutcomeUnknownError),
            (requests.exceptions.ReadTimeout("read timed out"), VenueOutcomeUnknownError),
            (requests.exceptions.Timeout("timed out"), VenueOutcomeUnknownError),
            (TimeoutError("socket timeout"), VenueOutcomeUnknownError),
            (requests.exceptions.ChunkedEncodingError("truncated"), VenueOutcomeUnknownError),
            # the venue declined — a fact, and no order exists
            (InputException("missing field", code=400), VenueRejectedError),
            (OrderException("RMS:Rule: Check freeze quantity", code=500), VenueRejectedError),
            (MarginException("insufficient margin", code=400), VenueRejectedError),
            (TokenException("token expired", code=403), VenueRejectedError),
            (PermissionException("not permitted", code=403), VenueRejectedError),
        ],
    )
    def test_the_sdk_exception_maps_to_the_documented_outcome(
        self, error: Exception, expected: type[Exception]
    ) -> None:
        assert isinstance(classify_kite_exception(error, attempted="placing an order"), expected)

    def test_a_rejection_carries_the_brokers_words_verbatim(self) -> None:
        """Three of the five documented rejection strings are UNVERIFIED (`§8`), so the raw text is
        the only durable evidence of what actually happened."""
        raw = "RMS:Rule: Check freeze quantity for NSE CASH"
        classified = classify_kite_exception(OrderException(raw, code=500), attempted="placing")
        assert isinstance(classified, VenueRejectedError)
        assert classified.raw_message == raw

    @pytest.mark.adversarial
    def test_an_unclassifiable_exception_is_unknown_and_never_retryable(self) -> None:
        """The deliberate default. Guessing 'retryable' duplicates a live order; guessing 'unknown'
        costs a reconciliation pass that finds nothing."""
        classified = classify_kite_exception(RuntimeError("something new"), attempted="placing")
        assert isinstance(classified, VenueOutcomeUnknownError)
        assert not isinstance(classified, VenueUnavailableError)

    @pytest.mark.adversarial
    def test_a_general_exception_is_unknown_because_a_500_may_have_reached_the_oms(self) -> None:
        """SDK 5.2.1 has no `MarginException`; `connect.py` falls back to `GeneralException`, so
        this class is a grab-bag and a 500 from it says nothing about whether an order exists."""
        assert isinstance(
            classify_kite_exception(GeneralException("unclassified", code=500), attempted="x"),
            VenueOutcomeUnknownError,
        )

    @pytest.mark.adversarial
    def test_an_unnamed_client_error_is_a_rejection_because_4xx_precedes_acceptance(self) -> None:
        unnamed = type("SomeNewException", (KiteException,), {})("bad request", code=422)
        assert isinstance(classify_kite_exception(unnamed, attempted="x"), VenueRejectedError)

    @pytest.mark.adversarial
    def test_rate_limiting_is_retryable_because_the_gateway_refused_before_the_oms(self) -> None:
        """`docs/research/222` §5 — HTTP 429 beyond ten orders a second. The order was not created,
        so this is the one 4xx that may be sent again."""
        throttled = type("SomeNewException", (KiteException,), {})("too many", code=429)
        assert isinstance(classify_kite_exception(throttled, attempted="x"), VenueUnavailableError)

    def test_the_classification_reaches_the_caller_through_place(self) -> None:
        client = _FakeKiteClient(raises=NetworkException("OMS down", code=503))
        with pytest.raises(VenueUnavailableError):
            KiteOrderExecutionVenue(client).place(_order())

    def test_a_timeout_on_place_is_unknown_and_names_the_reconciliation_instruction(self) -> None:
        client = _FakeKiteClient(raises=requests.exceptions.ReadTimeout("timed out"))
        with pytest.raises(VenueOutcomeUnknownError) as unknown:
            KiteOrderExecutionVenue(client).place(_order())
        assert "never by sending again" in str(unknown.value)

    def test_the_original_exception_is_chained_and_never_swallowed(self) -> None:
        original = InputException("bad field", code=400)
        client = _FakeKiteClient(raises=original)
        with pytest.raises(VenueRejectedError) as rejection:
            KiteOrderExecutionVenue(client).place(_order())
        assert rejection.value.__cause__ is original


class TestModifyAndCancel:
    def test_a_modification_carries_the_new_expression_in_rupees(self) -> None:
        client = _FakeKiteClient()
        repriced = OrderExpression(
            variety=OrderVariety.REGULAR,
            product=OrderProduct.INTRADAY,
            order_type=OrderType.LIMIT,
            validity=OrderValidity.DAY,
            limit_price_paise=Decimal("280050"),
        )
        KiteOrderExecutionVenue(client).modify(_A_REAL_KITE_ORDER_ID, repriced, 50)
        sent = client.modify_order_calls[0]
        assert sent["order_id"] == _A_REAL_KITE_ORDER_ID
        assert sent["price"] == 2800.5
        assert sent["quantity"] == 50

    @pytest.mark.adversarial
    def test_the_twenty_fifth_modification_is_the_last(self) -> None:
        """`docs/research/222` §4. Refused locally because the broker's refusal is an opaque
        rejection on an order that is still live."""
        client = _FakeKiteClient()
        venue = KiteOrderExecutionVenue(client)
        for _ in range(MAXIMUM_MODIFICATIONS_PER_ORDER):
            venue.modify(_A_REAL_KITE_ORDER_ID, _marketable_limit(), 100)
        assert venue.modifications_made_to(_A_REAL_KITE_ORDER_ID) == MAXIMUM_MODIFICATIONS_PER_ORDER
        with pytest.raises(VenueRejectedError):
            venue.modify(_A_REAL_KITE_ORDER_ID, _marketable_limit(), 100)
        assert len(client.modify_order_calls) == MAXIMUM_MODIFICATIONS_PER_ORDER

    def test_a_failed_modification_is_not_counted_against_the_cap(self) -> None:
        client = _FakeKiteClient(raises=NetworkException("OMS down", code=503))
        venue = KiteOrderExecutionVenue(client)
        with pytest.raises(VenueUnavailableError):
            venue.modify(_A_REAL_KITE_ORDER_ID, _marketable_limit(), 100)
        assert venue.modifications_made_to(_A_REAL_KITE_ORDER_ID) == 0

    def test_cancellation_routes_by_variety_because_kite_does(self) -> None:
        client = _FakeKiteClient()
        KiteOrderExecutionVenue(client).cancel(_A_REAL_KITE_ORDER_ID, variety="regular")
        assert client.cancel_order_calls == [
            {"variety": "regular", "order_id": _A_REAL_KITE_ORDER_ID}
        ]


class TestReadingTheBrokersOwnAccount:
    def _order_row(self, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "order_id": _A_REAL_KITE_ORDER_ID,
            "tag": "N0MP7abcdefghijklmno",
            "status": "COMPLETE",
            "tradingsymbol": "RELIANCE",
            "quantity": 100,
            "filled_quantity": 100,
            "pending_quantity": 0,
            "average_price": 2811.35,
            "status_message": None,
            "status_message_raw": None,
            # Naive ON PURPOSE: this is exactly what the SDK hands back, and reading it as
            # anything other than IST is the defect the normaliser exists to prevent.
            "exchange_timestamp": datetime(2026, 8, 13, 9, 45, 12),  # noqa: DTZ001
            "order_timestamp": datetime(2026, 8, 13, 9, 45, 11),  # noqa: DTZ001
            "placed_by": "AB1234",
        }
        row.update(overrides)
        return row

    def test_an_order_row_normalises_without_being_interpreted(self) -> None:
        report = normalise_order_row(self._order_row())
        assert report.broker_order_id == _A_REAL_KITE_ORDER_ID
        assert report.status == "COMPLETE"
        assert report.average_price_paise == Decimal("281135")
        assert report.filled_quantity == 100
        assert report.status_message == ""

    def test_the_status_string_survives_verbatim_so_the_state_machine_can_refuse_it(self) -> None:
        """Normalising the word away would disable the one refusal that stops a wrong state landing
        on a real order."""
        report = normalise_order_row(self._order_row(status="TRIGGER PENDING"))
        assert report.status == "TRIGGER PENDING"
        assert lifecycle_event_for_broker_status(report.status).value == "trigger_armed"

    def test_a_naive_broker_timestamp_is_read_as_ist(self) -> None:
        """Read as UTC it would be wrong by five and a half hours, which spans the whole session."""
        report = normalise_order_row(self._order_row())
        assert report.exchange_timestamp is not None
        assert report.exchange_timestamp.utcoffset() is not None
        assert report.exchange_timestamp.isoformat().endswith("+05:30")

    @pytest.mark.adversarial
    def test_an_unfilled_order_has_no_average_price_rather_than_a_zero(self) -> None:
        report = normalise_order_row(
            self._order_row(status="OPEN", filled_quantity=0, average_price=0)
        )
        assert report.average_price_paise is None

    def test_the_order_book_is_read_and_normalised(self) -> None:
        client = _FakeKiteClient(orders_payload=[self._order_row()])
        venue = KiteOrderExecutionVenue(client)
        reports = venue.fetch_orders(session_date=_today_in_india())
        assert len(reports) == 1
        assert reports[0].tag == "N0MP7abcdefghijklmno"

    @pytest.mark.adversarial
    def test_a_past_session_is_refused_rather_than_answered_with_todays_book(self) -> None:
        venue = KiteOrderExecutionVenue(_FakeKiteClient(orders_payload=[self._order_row()]))
        with pytest.raises(PastSessionNotAvailableFromLiveVenueError):
            venue.fetch_orders(session_date=date(2020, 1, 2))

    def test_order_history_is_every_hop(self) -> None:
        client = _FakeKiteClient(
            orders_payload=[
                self._order_row(status="PUT ORDER REQ RECEIVED", filled_quantity=0),
                self._order_row(status="OPEN", filled_quantity=0),
                self._order_row(status="COMPLETE"),
            ]
        )
        history = KiteOrderExecutionVenue(client).fetch_order_history(_A_REAL_KITE_ORDER_ID)
        assert [report.status for report in history] == [
            "PUT ORDER REQ RECEIVED",
            "OPEN",
            "COMPLETE",
        ]
        assert client.order_history_calls == [_A_REAL_KITE_ORDER_ID]

    def test_a_trade_row_normalises_into_paise(self) -> None:
        report = normalise_trade_row(
            {
                "trade_id": "70000000",
                "order_id": _A_REAL_KITE_ORDER_ID,
                "tradingsymbol": "RELIANCE",
                "quantity": 40,
                "average_price": 2811.35,
                "fill_timestamp": datetime(2026, 8, 13, 9, 45, 12),  # noqa: DTZ001 — as the SDK returns it
            }
        )
        assert report is not None
        assert report.price_paise == Decimal("281135")
        assert report.quantity == 40

    @pytest.mark.adversarial
    def test_a_trade_row_with_no_usable_price_is_dropped_not_defaulted(self) -> None:
        """A zero-priced trade applied to the fill ledger is a real execution as far as the ledger
        is concerned, and it drags the order's average fill price towards zero."""
        assert (
            normalise_trade_row(
                {
                    "trade_id": "70000001",
                    "order_id": _A_REAL_KITE_ORDER_ID,
                    "quantity": 40,
                    "average_price": 0,
                    "fill_timestamp": datetime(2026, 8, 13, 9, 45, 12),  # noqa: DTZ001
                }
            )
            is None
        )


class TestPositionsAreTheUnionOfThreeViews:
    """`docs/research/222` §7 — either view alone is a partial picture, and `t1_quantity` is the
    trap that makes a sell fail after a buy earlier the same week."""

    def _client(self) -> _FakeKiteClient:
        return _FakeKiteClient(
            positions_payload={
                "day": [
                    {
                        "tradingsymbol": "RELIANCE",
                        "product": "MIS",
                        "quantity": 100,
                        "average_price": 2811.35,
                    }
                ],
                "net": [
                    {
                        "tradingsymbol": "RELIANCE",
                        "product": "MIS",
                        "quantity": 100,
                        "average_price": 2811.35,
                    },
                    {
                        "tradingsymbol": "INFY",
                        "product": "NRML",
                        "quantity": -50,
                        "average_price": 1520.0,
                    },
                ],
            },
            holdings_payload=[
                {
                    "tradingsymbol": "TCS",
                    "product": "CNC",
                    "quantity": 10,
                    "t1_quantity": 4,
                    "average_price": 3900.25,
                }
            ],
        )

    def test_all_three_views_come_back_each_labelled_with_its_origin(self) -> None:
        reports = KiteOrderExecutionVenue(self._client()).fetch_positions()
        views = [report.view for report in reports]
        assert views.count(DAY_POSITIONS_VIEW) == 1
        assert views.count(NET_POSITIONS_VIEW) == 2
        assert views.count(HOLDINGS_VIEW) == 1

    def test_the_unsettled_quantity_is_carried_off_t1_quantity(self) -> None:
        reports = KiteOrderExecutionVenue(self._client()).fetch_positions()
        holding = next(report for report in reports if report.view == HOLDINGS_VIEW)
        assert holding.trading_symbol == "TCS"
        assert holding.quantity == 10
        assert holding.unsettled_quantity == 4
        assert holding.average_price_paise == Decimal("390025")

    def test_a_short_position_keeps_its_sign(self) -> None:
        reports = KiteOrderExecutionVenue(self._client()).fetch_positions()
        short = next(report for report in reports if report.trading_symbol == "INFY")
        assert short.quantity == -50

    @pytest.mark.adversarial
    def test_an_empty_account_is_an_empty_tuple_and_not_an_error(self) -> None:
        venue = KiteOrderExecutionVenue(_FakeKiteClient(positions_payload={}, holdings_payload=[]))
        assert venue.fetch_positions() == ()

    @pytest.mark.adversarial
    def test_a_payload_that_is_not_a_list_of_rows_is_not_guessed_at(self) -> None:
        venue = KiteOrderExecutionVenue(
            _FakeKiteClient(positions_payload={"day": None, "net": "unexpected"})
        )
        assert venue.fetch_positions() == ()


def _today_in_india() -> date:
    from nse_algo_trader.order_path.trading_intent import INDIA_MARKET_TIMEZONE

    return datetime.now(tz=INDIA_MARKET_TIMEZONE).date()
