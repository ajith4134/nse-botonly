"""The shared conformance suite every segment bot must pass, unmodified — `L5.29`.

**This is the artifact that makes `A.130` safe.** Six bots built concurrently by six authors is an
acceptable risk only if "did the author understand the contract?" is a test result rather than a
review opinion. One suite, parameterised over all six bots, run unmodified against each: a bot is
not done because its author says so, it is done when this is green against it.

The suite is deliberately **runnable outside pytest** — it returns violations rather than asserting
— so the same checks can gate bot registration at runtime, not only at build time. A contract
enforced only in CI is a contract that stops being enforced the moment a bot is added anywhere else.

**Two refusals in the suite itself**, both guarding against the failure mode where a suite passes
because it checked nothing: it refuses to run with no contexts, and it refuses to run without a
context holding an **empty** tradeable universe. The second is not fussiness — "can this bot answer
*nothing today*?" is the cheapest real bug it catches, and a suite run only on busy mornings never
asks the question.

`R.23b`: a conformance suite is a check, not an engine. Named for what it is.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotGraduationRefusedError,
    BotMaturity,
    BotMaturityRung,
    SegmentBot,
    SegmentBotContext,
    SegmentBotProtocolError,
    SegmentRelevance,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import (
    TradingSegment,
    instrument_facts_for,
)

_IDENTITY_SHAPE = re.compile(r"^[a-z][a-z0-9_]{6,63}$")
"""`R.14`: lowercase, underscore-separated, long enough to say something.

Seven characters minimum is not a style preference — it is the shortest string that can hold a
segment name and a noun, and `bot_a` filed against a track record tells a later reader nothing about
which of six bots earned it.
"""


@dataclass(frozen=True, slots=True)
class ConformanceViolation:
    """One way a bot fails the shared contract.

    Frozen and hashable so violations from repeated context runs deduplicate cleanly — the same
    defect seen at three instants is one finding, not three.
    """

    bot_identity: str
    check: str
    detail: str

    def describe(self) -> str:
        return f"{self.bot_identity}: [{self.check}] {self.detail}"


def _check_identity(bot: SegmentBot) -> list[ConformanceViolation]:
    identity = getattr(bot, "bot_identity", "")
    if isinstance(identity, str) and _IDENTITY_SHAPE.match(identity.strip()):
        segment_word = bot.trading_segment.value
        if segment_word in identity:
            return []
        return [
            ConformanceViolation(
                bot_identity=identity.strip() or "<unnamed>",
                check="identity-is-self-describing",
                detail=(
                    f"does not contain {segment_word!r}. Matching only the FIRST WORD was the "
                    f"original check and it licensed exactly the mislabelling `R.14` forbids: "
                    f"an index_futures bot named index_options_greek_scalper passed, and a "
                    f"cash_intraday bot named cashew_nut_arbitrage_bot passed on the substring "
                    f"'cash' (review finding M4)"
                ),
            )
        ]
    return [
        ConformanceViolation(
            bot_identity=str(identity).strip() or "<unnamed>",
            check="identity-is-self-describing",
            detail=(
                "is empty or malformed; `R.14` requires a lowercase underscore-separated name of "
                "at "
                "least seven characters, because it is the key a track record is filed under and a "
                "bot nobody can name is a record nobody can attribute"
            ),
        )
    ]


def _check_signals(bot: SegmentBot, signals: Sequence[PricedSignal]) -> list[ConformanceViolation]:
    facts = instrument_facts_for(bot.trading_segment)
    violations: list[ConformanceViolation] = []
    for signal in signals:
        if signal.segment is not facts.chargeable_segment:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="signal-matches-own-segment",
                    detail=(
                        f"emitted a {signal.segment.value} signal for {signal.trading_symbol}, but "
                        f"this bot is {bot.trading_segment.value}, whose charge scope is "
                        f"{facts.chargeable_segment.value}. Six bots share one order path, so a "
                        f"bot "
                        f"reaching outside its own segment is the cross-talk failure that makes "
                        f"per-segment arming (`A.01`) meaningless"
                    ),
                )
            )
        if facts.strike_required and signal.strike_paise is None:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="option-signal-carries-a-strike",
                    detail=(
                        f"{signal.trading_symbol} has no strike; the SEBI turnover fee is charged "
                        f"on notional, so the cost of this signal cannot be computed and the cost "
                        f"gate would have to guess"
                    ),
                )
            )
        if signal.source.strip() != bot.bot_identity.strip():
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="signal-is-attributable-to-its-bot",
                    detail=(
                        f"{signal.trading_symbol} names source {signal.source!r}, which is not "
                        f"this bot's identity; an edge nobody can attribute is an edge nobody can "
                        f"disprove, and the track record would accrue to the wrong bot"
                    ),
                )
            )
    return violations


def _check_relevance_is_stable(
    bot: SegmentBot, first: SegmentRelevance, second: SegmentRelevance
) -> list[ConformanceViolation]:
    """Review finding C1: a bot whose relevance differed on every call passed cleanly."""
    if first == second:
        return []
    return [
        ConformanceViolation(
            bot_identity=str(bot.bot_identity),
            check="relevance-is-deterministic",
            detail=(
                f"answered {first.applicability} then {second.applicability} for the same context; "
                f"the supervisor allocates on this number, so one that moves between reads makes "
                f"the allocation unexplainable"
            ),
        )
    ]


def _check_relevance(bot: SegmentBot, relevance: object) -> list[ConformanceViolation]:
    """Whether the relevance score is a bounded, comparable number.

    The bounds are re-validated HERE rather than trusted from the constructor. Review finding C1/7b:
    `object.__setattr__` on a frozen `SegmentRelevance` produced `applicability=10000`, which passed
    the suite and would take the entire allocation from five sibling bots — the same bypass the
    suite already anticipates for `BotMaturity` and did not for this.
    """
    if isinstance(relevance, SegmentRelevance):
        if math.isfinite(relevance.applicability) and 0.0 <= relevance.applicability <= 1.0:
            return []
        return [
            ConformanceViolation(
                bot_identity=str(bot.bot_identity),
                check="relevance-is-a-bounded-score",
                detail=(
                    f"reports applicability {relevance.applicability!r}, outside [0, 1]. The "
                    f"constructor refuses this, so the value was written past it — and an "
                    f"unbounded "
                    f"score wins every allocation by magnitude rather than by being right"
                ),
            )
        ]
    return [
        ConformanceViolation(
            bot_identity=bot.bot_identity,
            check="relevance-is-a-bounded-score",
            detail=(
                f"returned {type(relevance).__name__} rather than a SegmentRelevance; the "
                f"supervisor compares six of these directly and cannot compare an unbounded value"
            ),
        )
    ]


def _call_maturity(bot: SegmentBot) -> object:
    """Indirection that stops mypy narrowing the return type back to `BotMaturity`."""
    return bot.maturity()


def _check_maturity(bot: SegmentBot) -> list[ConformanceViolation]:
    """Whether this bot respects the one refusal `R.22` makes absolute.

    ``maturity`` is deliberately typed ``object`` here rather than ``BotMaturity``. mypy trusts the
    protocol's annotation and therefore proves the isinstance branch unreachable — but a conformance
    suite exists precisely to check bots that do NOT honour their annotations, and a suite that
    believes the contract it is verifying checks nothing. Widening the type is what keeps the
    runtime check honest and the type-checker satisfied at the same time.
    """
    maturity: object
    try:
        maturity = _call_maturity(bot)
    except SegmentBotProtocolError as unrelated:
        # Review finding M6: catching every SegmentBotProtocolError here reported an unrelated bug
        # (a malformed SegmentRelevance built inside maturity()) as an attempted self-graduation —
        # the single most alarming thing the suite can say. That is how a real R.22 violation gets
        # triaged as noise. Only the graduation refusal raises the graduation alarm.
        if isinstance(unrelated, BotGraduationRefusedError):
            raise
        return [
            ConformanceViolation(
                bot_identity=str(bot.bot_identity),
                check="maturity-does-not-raise",
                detail=(
                    f"raised {type(unrelated).__name__} reporting its maturity: {unrelated}. This "
                    f"is "
                    f"NOT a self-graduation attempt — it is an unrelated defect, reported under "
                    f"its "
                    f"own name so the R.22 alarm keeps its meaning"
                ),
            )
        ]
    except BotGraduationRefusedError as refused:
        # The constructor refusing GRADUATED is the protocol working, not the bot conforming: the
        # bot still TRIED, and a bot that tries to arm itself must be reported, not quietly excused.
        return [
            ConformanceViolation(
                bot_identity=bot.bot_identity,
                check="never-self-graduates",
                detail=f"attempted a maturity the protocol refuses to construct: {refused}",
            )
        ]
    if not isinstance(maturity, BotMaturity):
        return [
            ConformanceViolation(
                bot_identity=bot.bot_identity,
                check="never-self-graduates",
                detail=f"returned {type(maturity).__name__} rather than a BotMaturity",
            )
        ]
    if maturity.rung is BotMaturityRung.GRADUATED:
        return [
            ConformanceViolation(
                bot_identity=bot.bot_identity,
                check="never-self-graduates",
                detail=(
                    "reported itself GRADUATED. `R.22` requires two keys for live capital — a "
                    "passed "
                    "graduation AND an explicit operator arm — so a bot holding both has defeated "
                    "the rule. Constructing the value is refused; bypassing that and returning it "
                    "anyway is caught here"
                ),
            )
        ]
    return []


MINUTES_IN_A_TRADING_DAY = 375
"""09:15 to 15:30 IST. A horizon longer than this cannot be closed inside one session.

A regulatory/physical fact about the NSE session, which is the `R.23e` exemption — it is the session
length, not a tuned threshold.
"""


def _check_emitted_carry(
    bot: SegmentBot, signals: Sequence[PricedSignal]
) -> list[ConformanceViolation]:
    """Whether the bot's own signals respect `R.01`.

    The previous version of this check tested the FACT TABLE — `facts.strike_required and
    facts.overnight_carry_permitted` — which `SegmentInstrumentFacts.__post_init__` already refuses
    to construct, so it was provably unreachable dead code and deleting it survived the whole test
    suite (review finding C2). `R.01` therefore had **zero** enforcement against any bot, while
    `A.130` claimed it was "enforced by the bot rather than the operator". This checks the thing
    that can actually be wrong: the horizon a bot puts on a signal it emits.
    """
    facts = instrument_facts_for(bot.trading_segment)
    if facts.overnight_carry_permitted:
        return []
    violations: list[ConformanceViolation] = []
    for signal in signals:
        horizon = signal.horizon_minutes
        if horizon is not None and horizon > MINUTES_IN_A_TRADING_DAY:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="honours-the-segment-carry-rule",
                    detail=(
                        f"{signal.trading_symbol} claims a {horizon}-minute horizon, which cannot "
                        f"close inside the {MINUTES_IN_A_TRADING_DAY}-minute session, in a segment "
                        f"that may not carry overnight. `R.01` admits no exception for options — "
                        f"index AND stock — and square-off is the failure mode, not the plan"
                    ),
                )
            )
    return violations


MAXIMUM_SIGNALS_PER_CONTEXT = 500
"""A bot proposing more than this at one instant is malfunctioning, not opinionated.

The full NSE equity universe is ~10,000 instruments and the sizer divides the book by the position
capacity, so a proposal naming every instrument is not a view — it is an absent filter. Chosen as an
order-of-magnitude backstop, not a tuned threshold: its only job is to make a runaway visible.
"""


def _check_signals_are_in_the_universe(
    bot: SegmentBot, context: SegmentBotContext, signals: Sequence[PricedSignal]
) -> list[ConformanceViolation]:
    """A bot may only decide about instruments it was GIVEN.

    Review finding C1: a bot fabricating instruments outside its universe passed cleanly, including
    when the universe was empty. `R.09`'s full-universe obligation is enforced at the call site that
    assembles the universe, so a bot that invents instruments routes around it entirely — and
    per-segment arming (`A.01`) means nothing if a bot can name instruments nobody armed it for.
    """
    permitted = {instrument.instrument_token for instrument in context.tradeable_universe}
    return [
        ConformanceViolation(
            bot_identity=bot.bot_identity,
            check="signal-is-inside-the-given-universe",
            detail=(
                f"proposed {signal.trading_symbol} (token {signal.instrument_token}), which is not "
                f"among the {len(permitted)} instrument(s) it was given. The universe is assembled "
                f"by the caller so `R.09` is visible there; a bot that invents instruments routes "
                f"around both that and per-segment arming"
            ),
        )
        for signal in signals
        if signal.instrument_token not in permitted
    ]


def _check_quantities_are_legal(
    bot: SegmentBot, context: SegmentBotContext, signals: Sequence[PricedSignal]
) -> list[ConformanceViolation]:
    """A quantity that is not a whole number of lots cannot be sent to the exchange.

    Review finding C1: an option bot proposing quantity 1 against a lot size of 75 passed. The sizer
    may REDUCE a proposed quantity, but it cannot legalise one — a proposal the exchange would
    reject is a defect in the bot, surfaced here rather than at the broker.
    """
    lots = {
        instrument.instrument_token: instrument.lot_size
        for instrument in context.tradeable_universe
    }
    violations: list[ConformanceViolation] = []
    for signal in signals:
        lot_size = lots.get(signal.instrument_token)
        if lot_size is None or lot_size <= 0:
            continue  # an unknown instrument is already reported by the universe check
        if signal.proposed_quantity % lot_size:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="quantity-is-a-whole-number-of-lots",
                    detail=(
                        f"{signal.trading_symbol} proposes {signal.proposed_quantity} against a "
                        f"lot "
                        f"size of {lot_size}; the exchange would reject it. The sizer may reduce a "
                        f"quantity but cannot make an illegal one legal (`L1.09`)"
                    ),
                )
            )
    return violations


def _check_signals_are_distinct(
    bot: SegmentBot, signals: Sequence[PricedSignal]
) -> list[ConformanceViolation]:
    """The same idea emitted twice becomes two fills against one conviction.

    Review finding C1: a bot returning the same `PricedSignal` object three times passed. Identity
    is checked by value, since two equal signals are the same idea however they were constructed.
    """
    seen: set[tuple[int, str, str]] = set()
    violations: list[ConformanceViolation] = []
    for signal in signals:
        key = (signal.instrument_token, str(signal.side), str(signal.decided_at))
        if key in seen:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="signals-are-distinct",
                    detail=(
                        f"emitted {signal.trading_symbol} {signal.side} at {signal.decided_at} "
                        f"more "
                        f"than once in one proposal; the order path would treat each as its own "
                        f"intent and fill one conviction several times"
                    ),
                )
            )
        seen.add(key)
    return violations


def _check_proposal_size(
    bot: SegmentBot, signals: Sequence[PricedSignal]
) -> list[ConformanceViolation]:
    """Review finding C1: a bot emitting 5,000 signals per context passed, including when idle."""
    if len(signals) <= MAXIMUM_SIGNALS_PER_CONTEXT:
        return []
    return [
        ConformanceViolation(
            bot_identity=bot.bot_identity,
            check="proposal-size-is-sane",
            detail=(
                f"proposed {len(signals):,} signals at one instant, over the "
                f"{MAXIMUM_SIGNALS_PER_CONTEXT} backstop. A proposal naming most of the universe "
                f"is "
                f"an absent filter rather than a view, and the sizer would divide the book into "
                f"unfillable slivers"
            ),
        )
    ]


def _check_context_is_unmutated(
    bot: SegmentBot, before: str, after: str
) -> list[ConformanceViolation]:
    """A bot must not write into the context it is handed.

    Review finding C1: a bot mutating `context.carried_memory` passed. The context is shared across
    a replay and, under `A.130`, conceptually across sibling bots — a bot that writes into it
    poisons both, and does so invisibly.
    """
    if before == after:
        return []
    return [
        ConformanceViolation(
            bot_identity=bot.bot_identity,
            check="context-is-not-mutated",
            detail=(
                f"mutated the context it was given: carried memory went from {before} to {after}. "
                f"The context is replayed and shared; a bot that writes into it poisons its own "
                f"replay and any sibling reading the same state"
            ),
        )
    ]


def run_segment_bot_conformance(
    bot: SegmentBot, contexts: Sequence[SegmentBotContext]
) -> list[ConformanceViolation]:
    """Run every shared check against one bot and return what it failed.

    Returns rather than asserts, so the same suite gates a bot at registration time as well as in
    the test run. An empty list is the only passing result.

    Raises :class:`SegmentBotProtocolError` when the suite has been given inputs that would let it
    pass vacuously — no contexts at all, or no context with an empty universe.
    """
    if not contexts:
        raise SegmentBotProtocolError(
            "the conformance suite needs at least one context; run over zero contexts it passes "
            "vacuously, which is a false reassurance and worse than no suite at all"
        )
    if not any(not context.tradeable_universe for context in contexts):
        raise SegmentBotProtocolError(
            "the conformance suite needs a context with an EMPTY tradeable universe; 'can this bot "
            "answer nothing today?' is the cheapest real bug it catches, and a suite run only on "
            "busy mornings never asks"
        )
    if not isinstance(bot.trading_segment, TradingSegment):
        raise SegmentBotProtocolError(
            f"{bot!r} declares trading_segment {bot.trading_segment!r}, which is not one of the six"
        )

    violations: list[ConformanceViolation] = []
    violations.extend(_check_identity(bot))
    violations.extend(_check_maturity(bot))

    # The identity is read ONCE and compared later: review finding C1 included a bot whose
    # `bot_identity` changed on every read, so its records would file under a second name.
    stable_identity = str(bot.bot_identity)

    for context in contexts:
        try:
            bot.observe(context)
            bot.observe(context)  # idempotent: the same instant twice must not double-count
        except Exception as failure:  # noqa: BLE001 — any failure shape is a contract violation
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="observe-is-idempotent",
                    detail=(
                        f"raised {type(failure).__name__} observing {context.decision_instant} "
                        f"twice: {failure}. A replay re-observes instants, so a bot that cannot "
                        f"tolerate it cannot be replayed"
                    ),
                )
            )
            continue

        memory_before = repr(context.carried_memory)
        try:
            first = bot.propose(context)
        except Exception as failure:  # noqa: BLE001
            check = (
                "empty-universe-is-answerable"
                if not context.tradeable_universe
                else "propose-does-not-raise"
            )
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check=check,
                    detail=(
                        f"raised {type(failure).__name__} proposing at {context.decision_instant} "
                        f"over {len(context.tradeable_universe)} instrument(s): {failure}. An "
                        f"empty proposal is a real answer and must never raise"
                    ),
                )
            )
            continue

        try:
            second = bot.propose(context)
        except Exception as failure:  # noqa: BLE001
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="deterministic-given-the-same-context",
                    detail=f"raised {type(failure).__name__} on a repeated proposal: {failure}",
                )
            )
            continue

        if first != second:
            violations.append(
                ConformanceViolation(
                    bot_identity=bot.bot_identity,
                    check="deterministic-given-the-same-context",
                    detail=(
                        f"returned {len(first)} signal(s) then {len(second)} for the same context "
                        f"at "
                        f"{context.decision_instant}. A bot whose answer depends on hidden state "
                        f"cannot be replayed, cannot be reviewed, and cannot have a decision trace "
                        f"reconstructed from it (`A.29`)"
                    ),
                )
            )

        violations.extend(_check_signals(bot, first))
        violations.extend(_check_signals_are_in_the_universe(bot, context, first))
        violations.extend(_check_quantities_are_legal(bot, context, first))
        violations.extend(_check_signals_are_distinct(bot, first))
        violations.extend(_check_proposal_size(bot, first))
        violations.extend(_check_emitted_carry(bot, first))
        memory_after = repr(context.carried_memory)
        violations.extend(_check_context_is_unmutated(bot, memory_before, memory_after))

        # Relevance is read TWICE, and outside any assumption that it will not raise. Review
        # finding M5: an exception here crashed the suite instead of rejecting the bot, defeating
        # the runtime-gate purpose the module docstring claims.
        try:
            first_relevance = bot.relevance(context)
            second_relevance = bot.relevance(context)
        except Exception as failure:  # noqa: BLE001
            violations.append(
                ConformanceViolation(
                    bot_identity=stable_identity,
                    check="relevance-does-not-raise",
                    detail=(
                        f"raised {type(failure).__name__} answering relevance at "
                        f"{context.decision_instant}: {failure}. A bot that cannot say how "
                        f"relevant "
                        f"it is must be rejected at registration, not take registration down"
                    ),
                )
            )
        else:
            violations.extend(_check_relevance(bot, first_relevance))
            violations.extend(_check_relevance_is_stable(bot, first_relevance, second_relevance))

        if str(bot.bot_identity) != stable_identity:
            violations.append(
                ConformanceViolation(
                    bot_identity=stable_identity,
                    check="identity-is-stable",
                    detail=(
                        f"changed its identity to {bot.bot_identity!r} during the run; a track "
                        f"record filed under a moving name accrues to nobody"
                    ),
                )
            )

    return list(dict.fromkeys(violations))
