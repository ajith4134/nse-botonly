"""The name a decision gives itself, so that one decision can only ever become one order.

`L3.01` asks for idempotent client order IDs. Kite Connect does not have them: `place_order` accepts
no client-supplied identifier and returns only the broker's own `order_id`
(`docs/research/222` §1). When the HTTP call times out, the caller cannot tell whether the order
reached the OMS, and Zerodha's own error text says so in as many words — *"Order request timed out.
Please check the order book and confirm before placing again."* That is an instruction to reconcile,
and reconciliation needs a name for the thing being looked for.

**The name is computed from the decision itself, never handed out by a store.** A process that has
just restarted and remembers nothing can recompute the identity of the intent it was in the middle
of submitting, and can therefore ask the broker "do you already have this?" instead of guessing.
An identity issued by a counter or a UUID cannot do that: the crash takes the mapping with it.

**What is in the name, and what is deliberately left out.** In: what makes the decision the
decision — the strategy that made it, the instrument, the side, the quantity, the session, and the
instant. Out: everything that is a *claim about* the trade rather than the trade — the expected edge
and the reference price. A recalibration or a price tick between the crash and the recovery must not
rename an order that is already sitting at the broker, and that is exactly how one intent would
become two.

**The consequence of that choice, stated rather than hidden:** two genuinely independent decisions
by the same strategy, on the same instrument, same side, same quantity, in the same microsecond, are
one intent as far as this system is concerned. That is the correct default for a system whose whole
purpose here is de-duplication, and any strategy that legitimately needs two identical clips in the
same microsecond must express that as one intent of twice the size — which is also the version that
prices correctly through `F01`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from nse_algo_trader.transaction_cost.chargeable_market_segments import ChargeableSegment, TradeLeg

INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

# Broker fact, sourced: Kite's `tag` field is alphanumeric and at most 20 characters
# (`docs/research/222` §1). It is the only field on the order that this system controls, so the
# whole of the wire-level identity has to fit inside it.
BROKER_TAG_LENGTH = 20
_TAG_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Layout of those 20 characters: one namespace character, four characters of session date, and the
# remaining fifteen carrying the intent's own digest. The session is on the wire because
# reconciliation's first act is to reduce the broker's whole order book to the orders this system
# could plausibly have placed today, before it tries to match any of them.
_NAMESPACE_WIDTH = 1
_SESSION_WIDTH = 4
_DIGEST_WIDTH = BROKER_TAG_LENGTH - _NAMESPACE_WIDTH - _SESSION_WIDTH

# An arbitrary but fixed origin for the compact session encoding. Four base-36 characters span
# 1,679,616 days, so the encoding does not run out for ~4,600 years; the assertion below is what
# makes that claim rather than the comment.
_SESSION_EPOCH = date(2020, 1, 1)
_SESSION_RADIX = 36

# Changing what goes into the digest changes every identity the system has ever computed, which
# would orphan in-flight orders across a deployment. The version makes that a deliberate act with a
# name rather than an accident of editing a tuple.
_IDENTITY_SCHEMA_VERSION = "intent-v1"
_FIELD_SEPARATOR = "\x1f"
_DIGEST_BYTES = 16


class OrderPathError(Exception):
    """The base of every refusal in the order path."""


class IntentIdentityError(OrderPathError):
    """An intent cannot be named, and naming it anyway would put a wrong order on the wire."""


class OrderNamespace(StrEnum):
    """Whose order this is, carried on the wire so the two can never be confused.

    A simulated order and a live order must be distinguishable by looking at the order alone.
    They share every line of code above the venue seam (`L9.03`), which is the point of the
    parity design, and the one thing that must NOT be shared is the answer to "did real money
    move".
    """

    LIVE = "N"
    SIMULATED = "S"


@dataclass(frozen=True, slots=True)
class TradingIntent:
    """A decision to trade that has already cleared `F01`, named by its own content."""

    strategy_identity: str
    instrument_token: int
    trading_symbol: str
    segment: ChargeableSegment
    side: TradeLeg
    quantity: int
    decided_at: datetime
    reference_price_paise: Decimal
    expected_edge_bps: Decimal
    horizon_minutes: int | None = None
    """How long the claimed edge is expected to last, when the strategy states one.

    Outside the identity for the same reason the edge itself is: it is a claim ABOUT the trade.
    It is carried here because the order path needs it — an intent queued past its own horizon is
    not the same intent, and sending it late is worse than not sending it at all.
    """

    def __post_init__(self) -> None:
        if not self.strategy_identity.strip():
            raise IntentIdentityError(
                "an intent with no strategy identity cannot be attributed, and an order nobody "
                "owns is an order nobody can be held to"
            )
        if self.quantity <= 0:
            raise IntentIdentityError(f"quantity must be positive, got {self.quantity}")
        if self.reference_price_paise <= 0:
            raise IntentIdentityError(
                f"reference price must be positive, got {self.reference_price_paise}"
            )
        if self.horizon_minutes is not None and self.horizon_minutes <= 0:
            raise IntentIdentityError(
                f"a horizon of {self.horizon_minutes} minutes is not a horizon"
            )
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise IntentIdentityError(
                "the decision time must carry a timezone: a naive timestamp is read differently "
                "by the host and by the exchange, and the difference is the size of a session"
            )

    @property
    def session_date(self) -> date:
        """The session this intent belongs to, DERIVED from when it was decided.

        Not a stored field. A stored session date next to a decision timestamp is a pair that can
        disagree, and the disagreement would file an intent against a session whose reconciliation
        never looks at it — which is precisely where a lost order becomes a lost position. Both NSE
        and MCX sessions, including MCX's evening session, close inside their own IST calendar day,
        so the IST date of the decision IS the session.
        """
        return self.decided_at.astimezone(INDIA_MARKET_TIMEZONE).date()

    @property
    def notional_paise(self) -> Decimal:
        """What the intent is worth at the price it was decided on."""
        return Decimal(self.quantity) * self.reference_price_paise

    @property
    def intent_id(self) -> str:
        """The decision's name — recomputable, stable, and independent of any store."""
        payload = _FIELD_SEPARATOR.join(
            (
                _IDENTITY_SCHEMA_VERSION,
                self.strategy_identity.strip(),
                str(self.instrument_token),
                self.trading_symbol,
                str(self.segment),
                str(self.side),
                str(self.quantity),
                self.session_date.isoformat(),
                self.decided_at.astimezone(INDIA_MARKET_TIMEZONE).isoformat(),
            )
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def _to_base(value: int, width: int, radix: int) -> str:
    if value < 0:
        raise IntentIdentityError(f"cannot encode a negative value: {value}")
    digits = []
    remaining = value
    for _ in range(width):
        digits.append(_TAG_CHARSET[remaining % radix])
        remaining //= radix
    if remaining:
        raise IntentIdentityError(
            f"value {value} does not fit in {width} characters at radix {radix}"
        )
    return "".join(reversed(digits))


def _from_base(text: str, radix: int) -> int | None:
    value = 0
    for character in text:
        position = _TAG_CHARSET.find(character)
        if position < 0 or position >= radix:
            return None
        value = value * radix + position
    return value


def broker_tag_for(intent: TradingIntent, namespace: OrderNamespace) -> str:
    """Encode the intent's identity into the twenty characters the broker will carry.

    The digest is truncated, so this is a lossy name and the loss has to be quantified rather
    than assumed harmless — see `tag_collision_probability`.
    """
    session_offset = (intent.session_date - _SESSION_EPOCH).days
    if session_offset < 0:
        raise IntentIdentityError(
            f"session {intent.session_date} precedes the tag encoding's origin {_SESSION_EPOCH}"
        )
    digest_value = int(intent.intent_id, 16)
    encoded_digest = _to_base(
        digest_value % (len(_TAG_CHARSET) ** _DIGEST_WIDTH), _DIGEST_WIDTH, len(_TAG_CHARSET)
    )
    tag = (
        namespace.value
        + _to_base(session_offset, _SESSION_WIDTH, _SESSION_RADIX)
        + encoded_digest
    )
    if len(tag) != BROKER_TAG_LENGTH or not tag.isalnum():
        raise IntentIdentityError(
            f"encoded tag {tag!r} is not a {BROKER_TAG_LENGTH}-character alphanumeric string, "
            f"which the broker would reject"
        )
    return tag


def session_and_namespace_from_tag(tag: str) -> tuple[date, OrderNamespace] | None:
    """Read a tag back, or report that it is not one of ours.

    Returning `None` rather than raising is deliberate: reconciliation walks the WHOLE order book,
    which legitimately contains orders placed by hand in the broker's own app, and a foreign tag is
    an ordinary fact about that book rather than an error condition. What must never happen is a
    foreign tag being parsed into a plausible-looking session and matched against one of ours.
    """
    if len(tag) != BROKER_TAG_LENGTH:
        return None
    try:
        namespace = OrderNamespace(tag[0])
    except ValueError:
        return None
    session_offset = _from_base(
        tag[_NAMESPACE_WIDTH : _NAMESPACE_WIDTH + _SESSION_WIDTH], _SESSION_RADIX
    )
    if session_offset is None:
        return None
    digest_part = tag[_NAMESPACE_WIDTH + _SESSION_WIDTH :]
    if _from_base(digest_part, len(_TAG_CHARSET)) is None:
        return None
    return _SESSION_EPOCH.fromordinal(_SESSION_EPOCH.toordinal() + session_offset), namespace


def tag_collision_probability(orders_in_session: int) -> Decimal:
    """The chance that two distinct intents in one session encode to the same tag.

    Derived rather than asserted (`R.03`): fifteen characters of a 62-character alphabet is
    `62**15` distinct tags, and the birthday bound over `n` orders is `n(n-1)/2` pairs against that
    space. Kite's own ceiling of 5,000 orders per day (`docs/research/222` §5) is the largest `n`
    this system can reach in a session, so the number below is an upper bound on a real risk, not a
    hypothetical one.
    """
    if orders_in_session < 0:
        raise IntentIdentityError(f"cannot have {orders_in_session} orders in a session")
    space = Decimal(len(_TAG_CHARSET)) ** _DIGEST_WIDTH
    pairs = Decimal(orders_in_session) * Decimal(orders_in_session - 1) / Decimal(2)
    return pairs / space
