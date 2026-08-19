"""The live half of the venue seam — `L9.01`, `L9.02` and the live side of `L9.03`.

This module is the ONLY place where an order this system decided on becomes bytes on Zerodha's
wire, and the only place where Zerodha's answer becomes something the order path is allowed to
believe. Everything it does is a mapping; nothing it does is a policy. The policies live above it —
the latch, the rate gate, the retry rule and the reconciler — and each of them is unable to do its
job if this module lies about what happened.

**Prices are PAISE everywhere inside this system and RUPEES on Kite's wire.** The conversion happens
here, at this boundary, and nowhere else. `rupees_to_paise` and `paise_to_rupees` are the only two
functions permitted to know the wire uses a different unit, and both go through `Decimal(str(…))`
because Kite hands back binary floats: `Decimal(2811.35)` is `2811.34999999999990905052982…`,
and multiplying that by a hundred and rounding is how a price becomes wrong by a paise in the
direction nobody notices until a reconciliation breaks.

**Four refusals happen BEFORE the wire**, because a refusal after it is a rejection this system paid
for:

* a MARKET order, for algo-originated flow, on any date on or after NSE/MSD/67753 (2025-04-29) —
  refused with the circular named, out of `broker_order_facility_facts`, never out of an `if` here
  (`docs/research/223` §5);
* any variety the dated fact table says was withdrawn — Bracket Orders since March 2020;
* an expression that cannot be encoded onto the wire honestly (an iceberg whose legs do not divide
  its quantity, an auction order with no auction number, a `market_protection` of zero which Kite
  rejects outright);
* an order carrying the SIMULATED namespace. The latch is supposed to catch that (`L3.07`), and this
  is the second lock on the same door, because the cost of the two paths being confused once is
  unbounded.

**The exception classification is the contract.** `docs/research/222` §8 gives the SDK's exception →
HTTP map, and the three venue exceptions are not interchangeable: `VenueRejectedError` says no
order exists, `VenueOutcomeUnknownError` says one may exist and must be looked for, and
`VenueUnavailableError` says the call never arrived and may be sent again. Anything this module
cannot classify becomes `VenueOutcomeUnknownError` — never `VenueUnavailableError` — because the
failure mode of guessing "retryable" is a duplicated live order, while the failure mode of guessing
"unknown" is a reconciliation pass that finds nothing.

**Classification is by exception NAME walked over the MRO, not by `isinstance`.** Two reasons, both
deliberate. First, `kiteconnect` must not appear in the order path's import graph — the whole system
is built to run with no broker session at all, and an `import kiteconnect` here would drag the SDK
into every module that touches an order. Second, SDK 5.2.1 builds its exception class from the
server's own `error_type` string (`connect.py:991`, `getattr(ex, error_type, GeneralException)`), so
the set of names Zerodha can send is NOT bounded by the classes the installed SDK defines —
`MarginException` is documented but absent from 5.2.1, and a margin rejection therefore arrives as
`GeneralException` today. A name table degrades correctly across that; an `isinstance` table cannot
even be written.

**No retries live here.** `crash_safe_order_placer` owns the retry rule and applies it only to
`VenueUnavailableError`. A second retry layer underneath it would multiply the attempts, and would
do so invisibly.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from nse_algo_trader.order_path.broker_order_facility_facts import (
    OrderFacilityError,
    OrderOrigin,
    OrderType,
    OrderVariety,
    availability_of_order_type,
    availability_of_variety,
)
from nse_algo_trader.order_path.order_execution_venue import (
    VenueOrderReport,
    VenueOutcomeUnknownError,
    VenuePositionReport,
    VenueRejectedError,
    VenueTradeReport,
    VenueUnavailableError,
)
from nse_algo_trader.order_path.order_record import OrderExpression, OrderRecord
from nse_algo_trader.order_path.trading_intent import (
    INDIA_MARKET_TIMEZONE,
    OrderNamespace,
    OrderPathError,
)

KITE_VENUE_NAME = "zerodha_kite"

# The wire's unit against this system's unit. Kite quotes and accepts rupees; every price inside
# this system is an integral number of paise held as a Decimal.
PAISE_PER_RUPEE = Decimal(100)

# `docs/research/223` §4: the exchange assigns this identifier and the broker relays it. This system
# does not invent it, which is why it is read from the environment and why ABSENT MEANS ABSENT — a
# fabricated audit-trail identifier is worse than none, because it is indistinguishable from a real
# one in the exchange's records.
EXCHANGE_ALGO_IDENTIFIER_ENV_VAR = "NSE_EXCHANGE_ALGO_IDENTIFIER"

# `docs/research/222` §6: `market_protection` must be non-zero for MARKET and SL-M; `-1` asks for
# the broker's own default rather than asserting a percentage this system has no basis to choose.
BROKER_DEFAULT_MARKET_PROTECTION = -1
_ORDER_TYPES_REQUIRING_MARKET_PROTECTION = frozenset({OrderType.MARKET, OrderType.STOP_LOSS_MARKET})

# `docs/research/222` §4. Counted locally as well as by the broker: a modification refused by Kite
# for crossing the cap comes back as an opaque rejection on an order that is still live, whereas a
# local refusal costs nothing and names the reason. The local count can only ever UNDER-count (a
# restart forgets it), so it can refuse late but never early.
MAXIMUM_MODIFICATIONS_PER_ORDER = 25

# Kite's own vocabulary for the transaction side, kept here rather than on `TradeLeg` because it is
# a fact about this broker's wire and not about the trade.
_KITE_TRANSACTION_TYPE_BY_SIDE = {"buy": "BUY", "sell": "SELL"}

_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_CLIENT_ERROR_FLOOR = 400
_HTTP_SERVER_ERROR_FLOOR = 500


class FacilityNotAvailableToAlgoFlowError(VenueRejectedError):
    """The facility asked for is not available to this origin on this date — a dated, sourced fact.

    A `VenueRejectedError` rather than a new family because the consequence is identical to a broker
    rejection: no order exists. The difference is that this one costs nothing and names its
    circular, while its counterpart costs a round trip and returns an opaque RMS string.
    """


class OrderExpressionNotEncodableError(VenueRejectedError):
    """The expression cannot be put on Kite's wire without this module inventing part of it."""


class SimulatedOrderRefusedByLiveVenueError(VenueRejectedError):
    """A SIMULATED-namespace order reached the live venue

    The second lock on the paper/live door.
    ."""


class PastSessionNotAvailableFromLiveVenueError(OrderPathError):
    """A past session's book was asked of Kite, which only ever answers for the current day.

    Not one of the three venue outcomes, because it is neither a rejection nor an unknown outcome
    of a call — it is a question this venue cannot be asked. Answering it with today's book under
    another session's name would put the wrong day into reconciliation, which is the one place a
    wrong answer becomes a wrong position.
    """


def rupees_to_paise(rupees: float | str | Decimal | None) -> Decimal | None:
    """Kite's unit into this system's. `None` and zero both come back as `None`.

    Zero is not a price. Kite reports `average_price: 0` for an order that has not traded, and a
    zero standing in for "nothing yet" is the value that later divides a notional and produces a
    plausible, wrong answer.
    """
    if rupees is None:
        return None
    value = rupees if isinstance(rupees, Decimal) else Decimal(str(rupees))
    if value == 0:
        return None
    return value * PAISE_PER_RUPEE


def paise_to_rupees(paise: Decimal) -> float:
    """This system's unit onto Kite's wire.

    Returned as `float` because that is what the SDK serialises into the form body; the arithmetic
    is done in `Decimal` first so the float is the last step rather than the first.
    """
    return float(paise / PAISE_PER_RUPEE)


def exchange_algo_identifier_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """The exchange's algo identifier, or `None` when the operator has not configured one.

    Absent means absent (`docs/research/223` §4). The identifier belongs to the exchange; this
    system relays it and never manufactures one, so an unset variable means the `algo_id` parameter
    is omitted from the order rather than filled with a guess.
    """
    source = os.environ if environ is None else environ
    configured = source.get(EXCHANGE_ALGO_IDENTIFIER_ENV_VAR, "").strip()
    return configured or None


# --- exception classification -------------------------------------------------------------------
#
# `docs/research/222` §8 for the SDK map, plus the `requests`/`urllib3` names the SDK re-raises
# untouched (`connect.py:970`). Matched by name over the MRO, so a class the installed SDK does not
# define — `MarginException`, documented but absent from 5.2.1 — is still classified if Zerodha
# names it, and an exception from a library this module deliberately does not import is still
# reachable.

_VENUE_UNAVAILABLE_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "NetworkException",  # kite, HTTP 503 — OMS communications failure
        "ConnectTimeout",  # requests — the TCP handshake never completed
        "NewConnectionError",  # urllib3
        "NameResolutionError",  # urllib3
        "gaierror",  # socket — DNS never resolved
        "ProxyError",
        "SSLError",  # TLS never established, so nothing was ever sent
    }
)

_VENUE_OUTCOME_UNKNOWN_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "DataException",  # kite, HTTP 502 — the OMS answered, and the answer was garbled
        # `requests` raises ConnectionError for BOTH "connection refused" (never sent) and
        # urllib3's ProtocolError/RemoteDisconnected and ConnectionResetError — the server took the
        # request and then died or reset before answering, which is the canonical "your order is
        # already at the OMS" failure. The class name cannot tell the two apart, so it belongs
        # here: the R.23(c) review reproduced the previous classification sending ONE decision to
        # the venue THREE times, because unavailable is the only class the placer retries.
        "ConnectionError",
        "ConnectionResetError",
        "ProtocolError",
        "RemoteDisconnected",
        "ReadTimeout",  # requests — the request WAS sent; the answer never came
        "ReadTimeoutError",  # urllib3
        "Timeout",  # requests' base timeout, when nothing more specific is raised
        "TimeoutError",  # builtin, and socket.timeout which aliases it
        "ChunkedEncodingError",  # the response body was truncated mid-flight
        "ContentDecodingError",
        "JSONDecodeError",
    }
)

_VENUE_REJECTED_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "InputException",  # kite, HTTP 400 — the request was malformed and was not processed
        "OrderException",  # kite, HTTP 500 — the OMS declined the order itself
        "MarginException",  # documented; absent from SDK 5.2.1, reachable via `error_type`
        "TokenException",  # kite, HTTP 403 — the session, not the order; nothing was placed
        "PermissionException",  # kite, HTTP 403 — likewise
    }
)


def classify_kite_exception(error: BaseException, *, attempted: str) -> OrderPathError:
    """Turn whatever the SDK raised into exactly one of the three venue outcomes.

    Returns the exception to raise rather than raising it, so the classification can be tested
    directly against every SDK exception type without a call frame in the way.
    """
    raw_message = str(error)
    origin = f"{type(error).__name__} while {attempted}"
    for ancestor in type(error).__mro__:
        name = ancestor.__name__
        if name in _VENUE_UNAVAILABLE_EXCEPTION_NAMES:
            return VenueUnavailableError(
                f"{origin}: the call did not reach Zerodha, so no order was created and the "
                f"request may be sent again — {raw_message}"
            )
        if name in _VENUE_OUTCOME_UNKNOWN_EXCEPTION_NAMES:
            return VenueOutcomeUnknownError(
                f"{origin}: the request was sent and the outcome is unreadable, so an order MAY "
                f"exist; resolve it by looking in the order book, never by sending again — "
                f"{raw_message}"
            )
        if name in _VENUE_REJECTED_EXCEPTION_NAMES:
            return VenueRejectedError(
                f"{origin}: Zerodha declined — {raw_message}", raw_message=raw_message
            )

    status_code = getattr(error, "code", None)
    if isinstance(status_code, int) and _is_kite_exception(error):
        if status_code == _HTTP_TOO_MANY_REQUESTS:
            # A rate-limited request is refused at the gateway before the OMS sees it, so no order
            # exists and the request may be sent again. It is `VenueUnavailableError` rather than
            # `VenueRejectedError` for exactly that reason — `docs/research/222` §5.
            return VenueUnavailableError(
                f"{origin}: refused for rate limiting (HTTP {status_code}) before the OMS saw it, "
                f"so no order exists — {raw_message}"
            )
        if _HTTP_CLIENT_ERROR_FLOOR <= status_code < _HTTP_SERVER_ERROR_FLOOR:
            return VenueRejectedError(
                f"{origin}: Zerodha refused the request with HTTP {status_code}, which is decided "
                f"before the order is accepted, so no order exists — {raw_message}",
                raw_message=raw_message,
            )

    # The deliberate default. An exception this module has no name and no status code for is NOT
    # made retryable: `VenueUnavailableError` is the only outcome the placer sends again, and
    # sending an order again on the strength of a guess is how one intent becomes two live
    # positions. The honest statement about an unrecognised failure is "an order may exist" —
    # which is `VenueOutcomeUnknownError`, and which resolves by looking rather than by acting.
    return VenueOutcomeUnknownError(
        f"{origin}: unclassified failure, so this system cannot say whether an order was created; "
        f"treated as unknown rather than retryable, and resolved by reconciliation — {raw_message}"
    )


def _is_kite_exception(error: BaseException) -> bool:
    return any(ancestor.__name__ == "KiteException" for ancestor in type(error).__mro__)


# --- normalisation of Kite's payloads -------------------------------------------------------------


def _as_aware_ist(value: Any) -> datetime | None:
    """Kite's timestamps are IST and arrive naive. A naive timestamp compared against an aware one
    raises; a naive one silently read as UTC is wrong by five and a half hours, which spans the
    whole session."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=INDIA_MARKET_TIMEZONE)
    return parsed


def _as_int(value: Any, *, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def normalise_order_row(row: Mapping[str, Any]) -> VenueOrderReport:
    """One row of `orders()` or `order_history()` as this system's report. No interpretation.

    The status string is carried through VERBATIM. Translating it into a lifecycle event is
    `order_lifecycle_state_machine`'s job, and it refuses on a word it does not know — a refusal
    that only works if this module has not already normalised the word away.
    """
    return VenueOrderReport(
        broker_order_id=_as_text(row.get("order_id")),
        tag=_as_text(row.get("tag")),
        status=_as_text(row.get("status")),
        trading_symbol=_as_text(row.get("tradingsymbol")),
        quantity=_as_int(row.get("quantity")),
        filled_quantity=_as_int(row.get("filled_quantity")),
        pending_quantity=_as_int(row.get("pending_quantity")),
        average_price_paise=rupees_to_paise(row.get("average_price")),
        status_message=_as_text(row.get("status_message")),
        status_message_raw=_as_text(row.get("status_message_raw")),
        exchange_timestamp=_as_aware_ist(row.get("exchange_timestamp"))
        or _as_aware_ist(row.get("order_timestamp")),
        placed_by=_as_text(row.get("placed_by")),
    )


def normalise_trade_row(row: Mapping[str, Any]) -> VenueTradeReport | None:
    """One row of `trades()`. `None` when the row carries no usable price or timestamp.

    Dropped rather than defaulted: a trade at a zero price would be applied to the fill ledger as
    a real execution and would drag the average fill price of the whole order towards zero.
    """
    price_paise = rupees_to_paise(row.get("average_price") or row.get("price"))
    filled_at = _as_aware_ist(row.get("fill_timestamp")) or _as_aware_ist(
        row.get("exchange_timestamp")
    )
    if price_paise is None or filled_at is None:
        return None
    return VenueTradeReport(
        broker_trade_id=_as_text(row.get("trade_id")),
        broker_order_id=_as_text(row.get("order_id")),
        trading_symbol=_as_text(row.get("tradingsymbol")),
        quantity=_as_int(row.get("quantity")),
        price_paise=price_paise,
        filled_at=filled_at,
    )


def normalise_position_row(row: Mapping[str, Any], *, view: str) -> VenuePositionReport:
    """One row of a `positions()` array, tagged with WHICH array it came from."""
    return VenuePositionReport(
        trading_symbol=_as_text(row.get("tradingsymbol")),
        product=_as_text(row.get("product")),
        quantity=_as_int(row.get("quantity")),
        average_price_paise=rupees_to_paise(row.get("average_price")),
        view=view,
    )


def normalise_holding_row(row: Mapping[str, Any]) -> VenuePositionReport:
    """One row of `holdings()`, carrying `t1_quantity` as `unsettled_quantity`.

    `docs/research/222` §7: `t1_quantity` is bought-today-unsettled and is NOT sellable as
    `quantity`. It is the trap that makes a sell fail after a buy earlier the same week, and it is
    carried on the report so a reconciler cannot fail to see it.
    """
    return VenuePositionReport(
        trading_symbol=_as_text(row.get("tradingsymbol")),
        product=_as_text(row.get("product")),
        quantity=_as_int(row.get("quantity")),
        average_price_paise=rupees_to_paise(row.get("average_price")),
        view=HOLDINGS_VIEW,
        unsettled_quantity=_as_int(row.get("t1_quantity")),
    )


DAY_POSITIONS_VIEW = "positions.day"
NET_POSITIONS_VIEW = "positions.net"
HOLDINGS_VIEW = "holdings"

_ReturnValue = TypeVar("_ReturnValue")


class KiteOrderExecutionVenue:
    """The real broker, behind `OrderExecutionVenue`. A mapping, and nothing more.

    Holds a client rather than building one: `build_authenticated_kite_client_if_valid()` is the
    only way a session is created in this system, and a venue that could build its own would be a
    second way.
    """

    def __init__(
        self,
        kite_client: Any,
        *,
        exchange_algo_identifier: str | None = None,
    ) -> None:
        self._kite_client = kite_client
        self._exchange_algo_identifier = (
            exchange_algo_identifier
            if exchange_algo_identifier is not None
            else exchange_algo_identifier_from_environment()
        )
        self._modifications_by_order_id: dict[str, int] = {}

    @property
    def venue_name(self) -> str:
        return KITE_VENUE_NAME

    @property
    def exchange_algo_identifier(self) -> str | None:
        """The exchange's identifier if one is configured, `None` if none is. Never invented."""
        return self._exchange_algo_identifier

    @property
    def carries_exchange_algo_identifier(self) -> bool:
        """Whether orders from this venue will carry the regulatory audit-trail identifier.

        Read by the operations wall: an unconfigured identifier is an operator gap
        (`docs/research/223` §4), and it should be visible as one rather than discovered in an
        exchange query months later.
        """
        return self._exchange_algo_identifier is not None

    # --- placement --------------------------------------------------------------------------

    def place(self, order: OrderRecord) -> str:
        """Send the order, return Zerodha's order id, or refuse before the wire."""
        self._refuse_simulated_order(order)
        session_date = session_date_of(order)
        self._refuse_unavailable_facility(order.expression, session_date=session_date)
        parameters = self.wire_parameters_for(order)
        order_id = self._call_kite(
            "placing an order", lambda: self._kite_client.place_order(**parameters)
        )
        return _as_text(order_id)

    def wire_parameters_for(self, order: OrderRecord) -> dict[str, Any]:
        """Exactly what will be handed to `place_order`, built and inspectable separately.

        Separated from `place` so the encoding can be asserted without a client being called at
        all — the wire format is the part of this module most likely to be wrong and least likely
        to fail loudly when it is.
        """
        expression = order.expression
        parameters: dict[str, Any] = {
            "variety": expression.variety.value,
            "exchange": exchange_of(order),
            "tradingsymbol": order.trading_symbol,
            "transaction_type": _KITE_TRANSACTION_TYPE_BY_SIDE[str(order.side)],
            "quantity": order.ordered_quantity,
            "product": expression.product.value,
            "order_type": expression.order_type.value,
            "validity": expression.validity.value,
            # OUR correlation handle, and the only field on the order this system controls
            # (`docs/research/222` §1). Passed through byte for byte: it is what a crashed process
            # searches the order book for, and a tag this module edited would not be found.
            "tag": order.broker_tag,
        }
        if expression.limit_price_paise is not None:
            parameters["price"] = paise_to_rupees(expression.limit_price_paise)
        if expression.trigger_price_paise is not None:
            parameters["trigger_price"] = paise_to_rupees(expression.trigger_price_paise)
        if expression.disclosed_quantity is not None:
            parameters["disclosed_quantity"] = expression.disclosed_quantity
        if expression.validity_minutes is not None:
            parameters["validity_ttl"] = expression.validity_minutes
        if expression.order_type in _ORDER_TYPES_REQUIRING_MARKET_PROTECTION:
            parameters["market_protection"] = self._market_protection_for(expression)
        if expression.variety is OrderVariety.ICEBERG:
            parameters["iceberg_legs"] = expression.iceberg_legs
            parameters["iceberg_quantity"] = self._iceberg_quantity_for(order)
        if expression.variety is OrderVariety.AUCTION:
            raise OrderExpressionNotEncodableError(
                "an auction order needs the exchange's auction number, which the order expression "
                "does not carry; sending one without it would place an ordinary order into a "
                "settlement-shortage session"
            )
        if self._exchange_algo_identifier is not None:
            # The regulator's field, NOT ours (`docs/research/223` §4). It establishes the
            # exchange's audit trail; `tag` above lets this system find its own order. Conflating
            # the two would satisfy neither obligation.
            parameters["algo_id"] = self._exchange_algo_identifier
        return parameters

    def _market_protection_for(self, expression: OrderExpression) -> int:
        stated = expression.market_protection_percent
        if stated is None:
            return BROKER_DEFAULT_MARKET_PROTECTION
        if stated == 0:
            raise OrderExpressionNotEncodableError(
                f"{expression.order_type} requires a non-zero market_protection and Kite rejects "
                f"zero outright (docs/research/222 §6); leave it unset to ask for the broker's "
                f"default rather than sending a value that cannot be accepted"
            )
        return stated

    def _iceberg_quantity_for(self, order: OrderRecord) -> int:
        legs = order.expression.iceberg_legs or 0
        quantity, remainder = divmod(order.ordered_quantity, legs)
        if remainder:
            raise OrderExpressionNotEncodableError(
                f"an iceberg of {order.ordered_quantity} across {legs} legs does not divide "
                f"evenly; Kite takes a per-leg quantity, and this module will not round one off "
                f"its own initiative because the rounding changes the size actually sent"
            )
        return quantity

    def _refuse_simulated_order(self, order: OrderRecord) -> None:
        if order.namespace is OrderNamespace.SIMULATED:
            raise SimulatedOrderRefusedByLiveVenueError(
                f"order {order.intent_id} carries the SIMULATED namespace and reached the live "
                f"venue; the control latch is supposed to stop this, and this is the second lock "
                f"on the same door because the cost of paper and live being confused once is "
                f"unbounded"
            )

    def _refuse_unavailable_facility(
        self, expression: OrderExpression, *, session_date: date
    ) -> None:
        """Every order this system places is algo-originated by construction, so the origin is not
        a parameter — it is a fact about this venue."""
        try:
            variety_fact = availability_of_variety(expression.variety, on=session_date)
            order_type_fact = availability_of_order_type(
                expression.order_type, OrderOrigin.ALGO_API, on=session_date
            )
        except OrderFacilityError as unknown_era:
            raise FacilityNotAvailableToAlgoFlowError(
                f"cannot place {expression.order_type} as {expression.variety} on {session_date}: "
                f"{unknown_era}",
                raw_message=str(unknown_era),
            ) from unknown_era
        for facility, fact in (
            (expression.variety, variety_fact),
            (expression.order_type, order_type_fact),
        ):
            if not fact.available:
                raise FacilityNotAvailableToAlgoFlowError(
                    f"{facility} is not available to {OrderOrigin.ALGO_API} on {session_date}: "
                    f"{fact.reason} [{fact.source}]",
                    raw_message=fact.reason,
                )

    # --- amendment --------------------------------------------------------------------------

    def modify(self, broker_order_id: str, expression: OrderExpression, quantity: int) -> None:
        """Amend a live order. The caller MUST re-read `fetch_order_history` afterwards.

        `docs/research/222` §4: there is no published atomicity guarantee for a modify racing a
        fill, so a modify that returns without error has not established that the order is now what
        was asked for. This method deliberately returns nothing, so there is no value to mistake
        for a confirmation.
        """
        already_modified = self._modifications_by_order_id.get(broker_order_id, 0)
        if already_modified >= MAXIMUM_MODIFICATIONS_PER_ORDER:
            raise VenueRejectedError(
                f"order {broker_order_id} has already been modified "
                f"{already_modified} times and Kite permits {MAXIMUM_MODIFICATIONS_PER_ORDER} "
                f"(docs/research/222 §4); the order is still live and needs cancelling and "
                f"replacing rather than amending again",
                raw_message="modification cap reached",
            )
        parameters: dict[str, Any] = {
            "variety": expression.variety.value,
            "order_id": broker_order_id,
            "quantity": quantity,
            "order_type": expression.order_type.value,
            "validity": expression.validity.value,
        }
        if expression.limit_price_paise is not None:
            parameters["price"] = paise_to_rupees(expression.limit_price_paise)
        if expression.trigger_price_paise is not None:
            parameters["trigger_price"] = paise_to_rupees(expression.trigger_price_paise)
        if expression.disclosed_quantity is not None:
            parameters["disclosed_quantity"] = expression.disclosed_quantity
        if expression.order_type in _ORDER_TYPES_REQUIRING_MARKET_PROTECTION:
            parameters["market_protection"] = self._market_protection_for(expression)
        self._call_kite(
            f"modifying order {broker_order_id}",
            lambda: self._kite_client.modify_order(**parameters),
        )
        self._modifications_by_order_id[broker_order_id] = already_modified + 1

    def modifications_made_to(self, broker_order_id: str) -> int:
        """How many modifications this process has sent for that order. Under-counts after a
        restart, which makes it refuse late rather than early."""
        return self._modifications_by_order_id.get(broker_order_id, 0)

    def cancel(self, broker_order_id: str, *, variety: str) -> None:
        """Ask for a cancellation. Like `modify`, this establishes only that the request was
        accepted — `CANCEL PENDING` followed by `COMPLETE` is a documented outcome."""
        self._call_kite(
            f"cancelling order {broker_order_id}",
            lambda: self._kite_client.cancel_order(variety=variety, order_id=broker_order_id),
        )

    # --- reconciliation surface ---------------------------------------------------------------

    def fetch_orders(self, *, session_date: date) -> tuple[VenueOrderReport, ...]:
        """The day's order book. `session_date` is carried for the caller's own assertion.

        Kite's `orders()` takes no date and always answers for the current trading day, so the
        parameter cannot filter anything here. It is on the Protocol because the simulator CAN
        answer for an arbitrary session, and because a caller asking for a past session against
        the live venue is asking for something this venue cannot give — which is worth refusing
        loudly rather than answering with today's book under yesterday's name.
        """
        today_ist = datetime.now(tz=INDIA_MARKET_TIMEZONE).date()
        if session_date != today_ist:
            raise PastSessionNotAvailableFromLiveVenueError(
                f"Kite's order book only ever answers for the current trading day ({today_ist}); "
                f"it was asked for {session_date}, and returning today's orders under that name "
                f"would put the wrong session's book into reconciliation"
            )
        rows = self._call_kite("reading the order book", self._kite_client.orders)
        return tuple(normalise_order_row(row) for row in _as_rows(rows))

    def fetch_order_history(self, broker_order_id: str) -> tuple[VenueOrderReport, ...]:
        """Every status hop for one order — the only place the transient statuses are visible."""
        rows = self._call_kite(
            f"reading the history of order {broker_order_id}",
            lambda: self._kite_client.order_history(order_id=broker_order_id),
        )
        return tuple(normalise_order_row(row) for row in _as_rows(rows))

    def fetch_trades(self, *, session_date: date) -> tuple[VenueTradeReport, ...]:
        """The day's executions. A row with no usable price or time is dropped, never defaulted."""
        today_ist = datetime.now(tz=INDIA_MARKET_TIMEZONE).date()
        if session_date != today_ist:
            raise PastSessionNotAvailableFromLiveVenueError(
                f"Kite's trade book only ever answers for the current trading day ({today_ist}); "
                f"it was asked for {session_date}"
            )
        rows = self._call_kite("reading the trade book", self._kite_client.trades)
        normalised = (normalise_trade_row(row) for row in _as_rows(rows))
        return tuple(report for report in normalised if report is not None)

    def fetch_positions(self) -> tuple[VenuePositionReport, ...]:
        """The UNION of `positions()` (day and net) and `holdings()` — never one of them alone.

        `docs/research/222` §7: the `day` array resets each session and equity carried overnight
        moves into `holdings()`, where `t1_quantity` is bought-today-unsettled and cannot be sold.
        A reconciler that reads one view and calls it "the position" is wrong on exactly the days
        that matter, so all three views come back, each labelled with where it came from, and the
        caller decides which question it is asking.
        """
        positions = self._call_kite("reading positions", self._kite_client.positions)
        holdings = self._call_kite("reading holdings", self._kite_client.holdings)
        reports: list[VenuePositionReport] = []
        views: Any = positions if isinstance(positions, Mapping) else {}
        for key, view in (("day", DAY_POSITIONS_VIEW), ("net", NET_POSITIONS_VIEW)):
            for row in _as_rows(views.get(key)):
                reports.append(normalise_position_row(row, view=view))
        for row in _as_rows(holdings):
            reports.append(normalise_holding_row(row))
        return tuple(reports)

    # --- the one place the SDK's exceptions are caught -----------------------------------------

    def _call_kite(self, attempted: str, call: Callable[[], _ReturnValue]) -> _ReturnValue:
        try:
            return call()
        # Deliberately broad: EVERY failure of an SDK call has to end up as exactly one of the
        # three venue outcomes, and an unanticipated exception type escaping unclassified would
        # reach the placer as something it has no rule for. Nothing is swallowed — the original is
        # chained onto the classified one.
        except Exception as error:
            raise classify_kite_exception(error, attempted=attempted) from error


def _as_rows(payload: Any) -> tuple[Mapping[str, Any], ...]:
    """Kite answers with a list of dicts. Anything else is a payload this module will not guess at.

    It RAISES rather than returning an empty tuple. Returning `()` made an unreadable answer
    indistinguishable from "the broker has no orders", and the reconciler then declared a live,
    AMBIGUOUS order abandoned — a terminal state it never revisits. The R.23(c) review reproduced
    exactly that against a client whose `orders()` returned `None`.
    """
    if payload is None or not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
        raise VenueOutcomeUnknownError(
            f"the broker's answer was not a list of rows but {type(payload).__name__}; this is not "
            f"evidence that there are no orders, and treating it as such is how a live order gets "
            f"written off"
        )
    rows = tuple(row for row in payload if isinstance(row, Mapping))
    if len(rows) != len(payload):
        raise VenueOutcomeUnknownError(
            f"{len(payload) - len(rows)} of {len(payload)} rows in the broker's answer were not "
            f"readable as records; a partially readable order book is not an order book"
        )
    return rows


def session_date_of(order: OrderRecord) -> date:
    """The session an order belongs to, DERIVED from when it was created, in IST.

    The same derivation `TradingIntent.session_date` makes, for the same reason: a stored session
    date beside a timestamp is a pair that can disagree, and the facility facts are dated.
    """
    return order.created_at.astimezone(INDIA_MARKET_TIMEZONE).date()


def exchange_of(order: OrderRecord) -> str:
    """The exchange Kite wants, read off the segment rather than stored a second time.

    `ChargeableSegment`'s value already encodes it — `NSE-MIS`, `NFO-OPT`, `MCX-FUT` — so the
    exchange is derived, exactly as `crash_safe_order_placer` derives it for the rate gate.
    """
    return str(order.segment).split("-", maxsplit=1)[0]


def connect_to_live_kite_venue_if_authenticated() -> KiteOrderExecutionVenue | None:
    """A venue on a live session, or `None` when no valid daily token exists.

    The `None` is the whole point of the Kite-decoupled architecture: every caller must be able to
    run without a broker session, and the way it finds out is by being handed nothing rather than
    by catching an exception.
    """
    from nse_algo_trader.broker_sessions.authenticated_kite_client_builder import (
        build_authenticated_kite_client_if_valid,
    )

    kite_client = build_authenticated_kite_client_if_valid()
    if kite_client is None:
        return None
    return KiteOrderExecutionVenue(kite_client)
