"""The contract every segment bot implements — `L5.29`.

`L5.25` defines a segment holon as an autonomous **bot** owning its strategies, relevance models,
risk sub-limits, memory and track record. `A.130` builds all six of them concurrently on one shared
spine, and the whole safety of that decision rests on a single division:

* **the bot owns** its strategy set, its relevance model, its carried memory, its track record, and
  the instrument facts of its segment;
* **the spine owns** everything between a decision and money — cost clearance, sizing, the pre-trade
  risk gate, session risk state, the order path, order expression, the halt latch, the trade-quality
  floor, the decision trace, and the maturity-ladder machinery itself.

Six bots therefore multiply *strategy* code, not *decision-path* code. One proof of the spine is a
proof for all six, which is what makes six concurrent authors an acceptable risk rather than six
unproven decision paths.

**A bot emits `PricedSignal`s.** That type already exists and already carries everything the spine
needs — instrument, side, reference price, expected edge, `EdgeBasis`, conviction, optional strike,
horizon. The protocol does not invent a new currency; it names the one that is already there.

**A bot performs no I/O.** Everything it may look at arrives in a `SegmentBotContext`. That is what
lets it be tested on recorded data with no broker, replayed deterministically, and built by six
concurrent authors without shared state.

`R.23b`: this module is a **protocol** — a contract and its value types. It has no solver and
carries no state, so it does not wear engine vocabulary. The engines are the bots that implement it,
and each gets the full `R.23c` loop in its own right.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.regime.market_regime_state import RegimeDistribution
from nse_algo_trader.segment_bots.segment_trading_taxonomy import TradingSegment


class SegmentBotProtocolError(Exception):
    """A bot, or the value it produced, violates the contract all six share."""


class BotGraduationRefusedError(SegmentBotProtocolError):
    """A bot tried to construct its own `GRADUATED` maturity — the one absolute `R.22` refusal.

    Its own class because the conformance suite raises the loudest alarm it has on this, and
    adversarial review (2026-08-17, finding M6) found that catching the general protocol error there
    reported unrelated bugs as attempted self-graduation. An alarm that cries wolf is an alarm that
    gets triaged as noise, and this is the one that must never be."""


class BotMaturityRung(Enum):
    """Where a bot stands on its own evidence — `R.04`'s activation ladder.

    `R.04` is explicit that thin data gates **activation**, never the algorithm: a bot with six
    closed trades runs the same full strategy as a bot with six hundred, and only its permission to
    act differs. The rungs are ordered because a portfolio supervisor allocating across six bots has
    to compare them, and an unordered label cannot be compared.

    `GRADUATED` exists as a rung so the ladder has a top, and is the one value a bot may never
    report about itself — see :class:`BotMaturity`.
    """

    RETIRED = ("retired", -1)
    """Its own record says it loses money. Below `COLD_START`, deliberately.

    Added 2026-08-17 after an adversarial review pointed out that a bot proven to lose Rs 3.5 lakh
    and a bot that has never traded were both `COLD_START` — indistinguishable to every consumer,
    since `BotMaturity` carries only the rung, the count and the evidence string. "No evidence" and
    "evidence against" are opposite states and a ladder that cannot say so can only ever promote.
    """

    COLD_START = ("cold_start", 0)
    OBSERVING = ("observing", 1)
    PAPER_QUALIFIED = ("paper_qualified", 2)
    GRADUATION_CANDIDATE = ("graduation_candidate", 3)
    GRADUATED = ("graduated", 4)

    def __init__(self, label: str, ladder_position: int) -> None:
        self.label = label
        self.ladder_position = ladder_position


@dataclass(frozen=True, slots=True)
class BotMaturity:
    """A bot's own report of where it stands, and the evidence for it.

    **`GRADUATED` is refused at construction.** `R.22` requires two keys for live capital — a passed
    graduation *and* an explicit operator arm — and states that the system can NEVER self-promote.
    That is enforced here rather than in a review checklist, because a bot that can construct its
    own graduation has already defeated the rule no matter what any document says. The graduation
    transition is performed by the supervisor against the bot's evidence, never by the bot.
    """

    rung: BotMaturityRung
    closed_trades_observed: int
    evidence: str

    def __post_init__(self) -> None:
        if self.rung is BotMaturityRung.GRADUATED:
            raise BotGraduationRefusedError(
                "a bot cannot report itself GRADUATED: R.22 requires a passed graduation AND an "
                "explicit operator arm, and a system that can promote itself has both keys. The "
                "supervisor performs this transition against the bot's evidence, never the bot"
            )
        if self.closed_trades_observed < 0:
            raise SegmentBotProtocolError(
                f"closed trades observed must not be negative, got {self.closed_trades_observed}"
            )
        if not self.evidence.strip():
            raise SegmentBotProtocolError(
                "a rung claimed with no evidence is exactly the shape R.13 exists to catch — "
                "correctness of execution is not evidence of correctness of allocation, and a "
                "position on the ladder asserted without a reason is neither"
            )

    @property
    def is_armable_without_operator(self) -> bool:
        """Always false, and named so the answer is visible at every call site.

        A property that can only return one value looks redundant until you notice that the
        alternative is every caller writing its own comparison against `GRADUATED` — and one of them
        eventually writing `>=`.
        """
        return False


@dataclass(frozen=True, slots=True)
class SegmentRelevance:
    """How applicable a bot's strategies are right now, and why.

    `L5.25` gives every holon a relevance model. The supervisor compares six of these against each
    other, so the scale is fixed to [0, 1]: an unbounded score wins by magnitude rather than by
    being right, which is a silent allocation bug rather than a loud one.
    """

    applicability: float
    reason: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.applicability) or not 0.0 <= self.applicability <= 1.0:
            raise SegmentBotProtocolError(
                f"applicability must be a finite value in [0, 1], got {self.applicability!r}; six "
                f"bots' scores are compared directly, so an unbounded one wins by scale"
            )
        if not self.reason.strip():
            raise SegmentBotProtocolError(
                "a relevance score with no reason cannot be argued with, and the supervisor's "
                "allocation would then be unexplainable at the point it matters most"
            )


@dataclass(frozen=True, slots=True)
class TradeableInstrument:
    """One instrument a bot may decide about, with the facts that make a quantity legal.

    Assembled by the caller rather than fetched by the bot, so `R.09`'s full-universe obligation is
    visible at the call site — the same reason `PaperTradingSessionRunner` takes its universe as an
    argument.
    """

    instrument_token: int
    trading_symbol: str
    lot_size: int
    tick_size_paise: int
    strike_paise: Decimal | None = None
    expiry: date | None = None

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise SegmentBotProtocolError(
                f"{self.trading_symbol} has a lot size of {self.lot_size}; a substituted lot size "
                f"is worse than an absent one (`L1.09`), so this instrument is not tradeable"
            )
        if self.tick_size_paise <= 0:
            raise SegmentBotProtocolError(
                f"{self.trading_symbol} has a tick size of {self.tick_size_paise} paise; a price "
                f"cannot be rounded to a legal value without it"
            )
        if not self.trading_symbol.strip():
            raise SegmentBotProtocolError("an instrument with no symbol cannot be ordered")
        if self.instrument_token <= 0:
            raise SegmentBotProtocolError(
                f"{self.trading_symbol} has instrument token {self.instrument_token}; a token is a "
                f"positive exchange identifier and a non-positive one names nothing"
            )
        # Review finding m15: every one of these was accepted, so the record could describe
        # instruments that cannot exist — an expiry with no strike, a strike on a cash instrument,
        # a negative strike, an expiry already past.
        if self.strike_paise is not None and self.strike_paise <= 0:
            raise SegmentBotProtocolError(
                f"{self.trading_symbol} has strike {self.strike_paise}; a strike is a price and a "
                f"non-positive one cannot be exercised at"
            )
        if self.strike_paise is not None and self.expiry is None:
            raise SegmentBotProtocolError(
                f"{self.trading_symbol} carries a strike but no expiry; an option with no expiry "
                f"is "
                f"not an option, and the cost of it cannot be computed"
            )


@dataclass(frozen=True, slots=True)
class SegmentBotContext:
    """Everything a bot is allowed to look at, at one decision instant.

    A bot opens no socket and no database; the spine does. That purity is what makes a bot testable
    on recorded data with no broker, replayable deterministically, and safe to build concurrently
    with five siblings — no two authors can race on state neither of them holds.
    """

    decision_instant: datetime
    tradeable_universe: tuple[TradeableInstrument, ...]
    regime: RegimeDistribution
    carried_memory: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.decision_instant.tzinfo is None:
            raise SegmentBotProtocolError(
                "the decision instant carries no timezone; a naive instant is silently read as UTC "
                "and moves every decision in this project by five and a half hours"
            )


@runtime_checkable
class SegmentBot(Protocol):
    """What all six segment bots implement.

    Six members, and each exists because a bot that cannot answer it cannot be compared against its
    five siblings, gated by the spine, or held to a track record.

    **What a bot may NOT do**, stated as refusals because these are the failure modes six concurrent
    authors would each otherwise invent: size a position (it proposes a quantity as a *preference*
    and the sizer may reduce it); clear its own cost hurdle; place an order; touch the halt latch;
    propose an overnight-carrying position in a segment whose facts forbid it (`R.01`); propose an
    option signal with no strike; or report itself `GRADUATED` (`R.22`).
    """

    @property
    def trading_segment(self) -> TradingSegment:
        """Which of the six this bot is. Fixed for the life of the bot."""

    @property
    def bot_identity(self) -> str:
        """Stable, self-describing (`R.14`) — the key its track record is filed under."""

    def observe(self, context: SegmentBotContext) -> None:
        """Advance carried state on new market data.

        Separate from deciding on purpose: a bot that only learns when it trades learns from a
        sample selected by its own past decisions, which is the cleanest way to build a model that
        confirms itself.
        """

    def propose(self, context: SegmentBotContext) -> tuple[PricedSignal, ...]:
        """This bot's decisions, already priced.

        May be empty. An empty proposal is a real answer — "nothing today" — and must never raise,
        because a bot that cannot say nothing takes the whole session down on a quiet morning.
        """

    def relevance(self, context: SegmentBotContext) -> SegmentRelevance:
        """How applicable this bot's strategies are right now (`L5.25`)."""

    def maturity(self) -> BotMaturity:
        """This bot's own position on the activation ladder, from its own evidence (`R.04`)."""
