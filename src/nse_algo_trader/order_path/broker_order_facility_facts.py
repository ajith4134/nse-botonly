"""The order taxonomy, and what is actually permitted on it — point-in-time, with sources.

`L9.02` asks for the full order-type taxonomy and the operator's instruction (`A.99`) was to build
all of it and use each member where it is genuinely best. Two things get in the way of that, and
both are facts rather than opinions:

* **A market order may not carry algo-originated flow.** NSE/MSD/67753 (2025-04-29) and the NSE
  retail-algo FAQ: *"Algo orders with order type as Market Order are not permitted."* Every order
  this system places is algo-originated by construction. So MARKET is modelled, named, and refused
  with its circular attached — see `docs/research/223` §5. The substitute is a marketable limit,
  which is also the only version whose cost `F01` can price in advance; a market order's cost is
  unknowable before the fact by construction.
* **Bracket Orders were withdrawn in March 2020** and never replaced (`docs/research/222` §6).

The reason these live in a fact table rather than in `if` statements is `R.03`. A withdrawn facility
that reappears, a threshold the exchange revises "after due notice to the market", or a broker that
tightens its own ceiling are all ordinary events, and none of them should require editing an
engine. They are also **point-in-time**: a 2019 replay must see 2019's facilities, or the replay is
measuring a market that never existed.

**What this module is not.** It holds no policy about which facility to *choose* — that is `L9.14`'s
selector, which optimises over whatever this module says is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

DAY_SECONDS = 24 * 60 * 60


class OrderFacilityError(Exception):
    """A facility was asked about outside the period any recorded fact covers."""


class OrderOrigin(StrEnum):
    """Who produced the order — the axis the regulator's prohibition actually turns on.

    The framework restricts *algo* flow, not instruments. A system that models the restriction as a
    property of the order type alone becomes unable to describe the market it trades in: the market
    order exists, other participants use it, and the fill model has to reason about it.
    """

    ALGO_API = "algo_api"
    MANUAL = "manual"


class OrderVariety(StrEnum):
    """The broker's `variety` — the shape of the order rather than its price condition."""

    REGULAR = "regular"
    AFTER_MARKET = "amo"
    COVER = "co"
    ICEBERG = "iceberg"
    AUCTION = "auction"
    BRACKET = "bo"


class OrderProduct(StrEnum):
    """How the position is carried, which is also what it costs (`L1.15`)."""

    DELIVERY = "CNC"
    INTRADAY = "MIS"
    NORMAL = "NRML"
    COVER = "CO"
    MARGIN_TRADING = "MTF"


class OrderType(StrEnum):
    """The price condition."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS_LIMIT = "SL"
    STOP_LOSS_MARKET = "SL-M"


class OrderValidity(StrEnum):
    """How long the order lives if it does not fill."""

    DAY = "DAY"
    IMMEDIATE_OR_CANCEL = "IOC"
    TIME_TO_LIVE = "TTL"


class RateLimitScope(StrEnum):
    """Whose limit this is, which decides what breaching it costs.

    A broker ceiling breached returns an HTTP 429 and costs an order. The regulatory threshold
    breached changes the category of the flow and obliges registration (`docs/research/223` §2-3).
    They are kept apart even while they happen to agree at ten per second, because the day they
    diverge a merged fact would silently take the looser one.
    """

    REGULATORY_THRESHOLD = "regulatory_threshold"
    BROKER_CEILING = "broker_ceiling"


@dataclass(frozen=True, slots=True)
class FacilityAvailability:
    """Whether a facility may be used, since when, and on whose authority."""

    available: bool
    effective_from: date
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class OrderRateLimit:
    """One ceiling on order flow, aligned to the window its author measures."""

    maximum_orders: int
    window_seconds: int
    scope: RateLimitScope
    effective_from: date
    source: str
    aligned_to_calendar_window: bool
    note: str = ""


# --- the facts -------------------------------------------------------------------------------
#
# Each entry is (effective_from, availability). A facility's history reads oldest-first, and the
# applicable fact is the latest one not after the date being asked about. An era before the first
# recorded fact is a refusal, not a default: this system has no evidence about 1990 and inventing
# some would let a replay trade facilities that did not exist.

# Kite Connect v3's public availability; the earliest era any fact here covers.
_KITE_API_ERA = date(2016, 1, 1)

_VARIETY_HISTORY: dict[OrderVariety, tuple[tuple[date, FacilityAvailability], ...]] = {
    OrderVariety.REGULAR: (
        (
            _KITE_API_ERA,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders; confirmed against installed SDK 5.2.1",
                reason="the ordinary order",
            ),
        ),
    ),
    OrderVariety.AFTER_MARKET: (
        (
            _KITE_API_ERA,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders; confirmed against installed SDK 5.2.1",
                reason="queued outside market hours and released at the open",
            ),
        ),
    ),
    OrderVariety.COVER: (
        (
            _KITE_API_ERA,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source=(
                    "kite.trade/docs/connect/v3/orders (variety=co present in SDK 5.2.1); "
                    "support.zerodha.com for the constraints"
                ),
                reason=(
                    "intraday only, NSE equity only, mandatory stop-loss leg that cannot be "
                    "cancelled independently once armed"
                ),
            ),
        ),
    ),
    OrderVariety.ICEBERG: (
        (
            date(2021, 4, 1),
            FacilityAvailability(
                available=True,
                effective_from=date(2021, 4, 1),
                source="kite.trade/docs/connect/v3/orders; confirmed against installed SDK 5.2.1",
                reason="2 to 50 legs, disclosing one leg at a time",
            ),
        ),
    ),
    OrderVariety.AUCTION: (
        (
            date(2023, 1, 1),
            FacilityAvailability(
                available=True,
                effective_from=date(2023, 1, 1),
                source="kite.trade/docs/connect/v3/orders; confirmed against installed SDK 5.2.1",
                reason="the settlement-shortage auction session only",
            ),
        ),
    ),
    OrderVariety.BRACKET: (
        (
            _KITE_API_ERA,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders, historical",
                reason="entry with attached target and stop-loss legs",
            ),
        ),
        (
            date(2020, 3, 1),
            FacilityAvailability(
                available=False,
                effective_from=date(2020, 3, 1),
                source="support.zerodha.com — 'Why has Zerodha stopped Bracket Orders (BO)?'",
                reason=(
                    "bracket orders were withdrawn in March 2020 after their simultaneous "
                    "target and stop legs left unintended residual positions in volatile "
                    "sessions, and the July 2020 peak-margin rules removed the leverage that "
                    "made them attractive; there is no replacement, and the variety is absent "
                    "from SDK 5.2.1"
                ),
            ),
        ),
    ),
}

_ALGO_ORDER_TYPE_PROHIBITION_FROM = date(2025, 4, 29)

_ORDER_TYPE_HISTORY: dict[
    OrderType, tuple[tuple[date, OrderOrigin | None, FacilityAvailability], ...]
] = {
    OrderType.MARKET: (
        (
            _KITE_API_ERA,
            None,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders",
                reason="fills at whatever the book offers",
            ),
        ),
        (
            _ALGO_ORDER_TYPE_PROHIBITION_FROM,
            OrderOrigin.ALGO_API,
            FacilityAvailability(
                available=False,
                effective_from=_ALGO_ORDER_TYPE_PROHIBITION_FROM,
                source=(
                    "NSE/MSD/67753 (2025-04-29) and the NSE retail-algo FAQ (2025-11-03): "
                    "'Algo orders with order type as Market Order are not permitted'"
                ),
                reason=(
                    "market orders may not carry algo-originated flow; the substitute is a "
                    "marketable limit priced through the touch, which is also the only form "
                    "whose cost F01 can bound in advance"
                ),
            ),
        ),
    ),
    OrderType.LIMIT: (
        (
            _KITE_API_ERA,
            None,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders",
                reason="fills at the stated price or better, or not at all",
            ),
        ),
    ),
    OrderType.STOP_LOSS_LIMIT: (
        (
            _KITE_API_ERA,
            None,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source="kite.trade/docs/connect/v3/orders",
                reason="rests until the trigger, then becomes a limit order",
            ),
        ),
    ),
    OrderType.STOP_LOSS_MARKET: (
        (
            _KITE_API_ERA,
            None,
            FacilityAvailability(
                available=True,
                effective_from=_KITE_API_ERA,
                source=(
                    "kite.trade/docs/connect/v3/orders; market_protection must be non-zero "
                    "(docs/research/222 §6)"
                ),
                reason=(
                    "rests until the trigger, then takes the book; the prohibition on market "
                    "orders is written against the MARKET order type, and SL-M is not withdrawn "
                    "by it — recorded as read, and the ambiguity is noted in docs/research/223"
                ),
            ),
        ),
    ),
}

_RATE_LIMITS: tuple[OrderRateLimit, ...] = (
    OrderRateLimit(
        maximum_orders=10,
        window_seconds=1,
        scope=RateLimitScope.REGULATORY_THRESHOLD,
        effective_from=date(2025, 10, 1),
        source=(
            "NSE/INVG/67858 (2025-05-05) paras B.2 and F; SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132 "
            "for the effective date"
        ),
        aligned_to_calendar_window=True,
        note=(
            "'applied basis the calendar clock second of the broker server' — a rolling-window "
            "limiter can still place eleven orders inside one calendar second, so the bucket must "
            "align to the same second the broker counts. Crossing it obliges registration rather "
            "than merely failing, so it is the binding constraint even when a broker is looser."
        ),
    ),
    OrderRateLimit(
        maximum_orders=10,
        window_seconds=1,
        scope=RateLimitScope.BROKER_CEILING,
        effective_from=_KITE_API_ERA,
        source="kite.trade/docs/connect/v3/exceptions — HTTP 429 beyond it",
        aligned_to_calendar_window=False,
        note="happens to equal the regulatory threshold today; kept separate so a divergence shows",
    ),
    OrderRateLimit(
        maximum_orders=400,
        window_seconds=60,
        scope=RateLimitScope.BROKER_CEILING,
        effective_from=_KITE_API_ERA,
        source="kite.trade/docs/connect/v3/exceptions",
        aligned_to_calendar_window=False,
    ),
    OrderRateLimit(
        maximum_orders=5_000,
        window_seconds=DAY_SECONDS,
        scope=RateLimitScope.BROKER_CEILING,
        effective_from=_KITE_API_ERA,
        source="kite.trade/docs/connect/v3/exceptions",
        aligned_to_calendar_window=True,
        note="the ceiling the tag collision bound is computed against (trading_intent.py)",
    ),
)


def availability_of_variety(variety: OrderVariety, *, on: date) -> FacilityAvailability:
    """What was true of this variety on that date."""
    history = _VARIETY_HISTORY.get(variety, ())
    applicable = [fact for effective_from, fact in history if effective_from <= on]
    if not applicable:
        raise OrderFacilityError(
            f"no recorded fact covers {variety} on {on}: this system has no evidence about that "
            f"era, and assuming today's facilities would let a replay trade instruments that did "
            f"not exist"
        )
    return applicable[-1]


def availability_of_order_type(
    order_type: OrderType, origin: OrderOrigin, *, on: date
) -> FacilityAvailability:
    """What was true of this order type, for flow of that origin, on that date."""
    history = _ORDER_TYPE_HISTORY.get(order_type, ())
    applicable = [
        fact
        for effective_from, restricted_origin, fact in history
        if effective_from <= on and restricted_origin in (None, origin)
    ]
    if not applicable:
        raise OrderFacilityError(
            f"no recorded fact covers {order_type} for {origin} on {on}"
        )
    return applicable[-1]


def varieties_available_to(origin: OrderOrigin, *, on: date) -> frozenset[OrderVariety]:
    """Every variety the selector may consider — the permitted set, never a preference list."""
    # No variety is origin-restricted today; the parameter keeps the seam where one will go.
    del origin
    return frozenset(
        variety
        for variety in OrderVariety
        if _has_fact(variety, on) and availability_of_variety(variety, on=on).available
    )


def order_types_available_to(origin: OrderOrigin, *, on: date) -> frozenset[OrderType]:
    """Every order type the selector may consider for flow of this origin."""
    permitted = set()
    for order_type in OrderType:
        try:
            fact = availability_of_order_type(order_type, origin, on=on)
        except OrderFacilityError:
            continue
        if fact.available:
            permitted.add(order_type)
    return frozenset(permitted)


def order_rate_limits(*, on: date) -> tuple[OrderRateLimit, ...]:
    """Every ceiling in force on that date. They bind simultaneously; the tightest wins."""
    return tuple(limit for limit in _RATE_LIMITS if limit.effective_from <= on)


def _has_fact(variety: OrderVariety, on: date) -> bool:
    return any(effective_from <= on for effective_from, _ in _VARIETY_HISTORY.get(variety, ()))
