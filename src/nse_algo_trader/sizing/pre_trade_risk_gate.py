"""Whether this order may be sent at all — three tiers, and the operator key only tightens
(`L3.05`, `L7.02`).

Specification: `docs/research/227_position_sizing_and_risk_gate_spec.md` §4.

This is the first thing in the rebuild that can say **no**. It sits in front of the order path, so
`quantity == 0` and `refused` are the same answer arriving by two routes, and the sizer's zero and
this gate's refusal are recorded together rather than checked against each other.

**Every tier is evaluated even after one refuses.** The first refusal decides the outcome, but "why
was this refused" must not depend on evaluation order: an order that breaches a regulatory wall AND
exceeds the notional cap should say both, because fixing only the one that happened to be checked
first would leave the operator surprised the second time.

**Tier 1 — regulatory walls.** The F&O ban list, MWPL, circuit bands and exchange position limits.
These are **published facts, not statistics**, and they are never derived and never overridable. An
F&O ban is binary and a system that inferred it from its own price history is a system that
confidently trades a banned scrip. They are read point-in-time (`known_by`), so a backtest cannot
see a ban declared after the decision it is judging.

**Tier 2 — derived limits.** Maximum notional, price collar, maximum leverage, order rate — each a
percentile or ratio computed from the instrument's own history or the segment's own margin regime,
never a chosen number (`R.03`).

**Tier 3 — the operator override, which can only TIGHTEN.** Applied as `min` for a ceiling and `max`
for a floor: the direction that shrinks the permission. A loosening override is **refused at
construction** with the derived figure named, because a limit that can be loosened is not a limit,
and `R.22`'s two-key property survives only if one of the keys cannot be turned permanently.

**The latches are checked first of all.** A halted session refuses everything, before any tier runs,
because a book that has hit its daily loss limit does not need a per-order opinion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from nse_algo_trader.sizing.session_risk_state_store import (
    DailyLossLimit,
    RiskLatch,
    SessionRiskState,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import SizedPosition


class RiskGateError(Exception):
    """The gate cannot be constructed or evaluated, and permitting the order would be a guess."""


def _tighter(derived: Decimal, override: Decimal | None) -> Decimal:
    """The operator's figure when it shrinks the permission, the derived one otherwise."""
    return derived if override is None else min(derived, override)


class GateTier(Enum):
    """Which authority refused. Ordered from the one nobody may override."""

    SESSION_LATCH = "session_latch"
    REGULATORY = "regulatory"
    DERIVED = "derived"
    SIZER = "sizer"


@dataclass(frozen=True, slots=True)
class GateRefusal:
    """One reason this order may not be sent."""

    tier: GateTier
    rule: str
    detail: str

    def describe(self) -> str:
        return f"[{self.tier.value}] {self.rule}: {self.detail}"


@dataclass(frozen=True, slots=True)
class RegulatoryFacts:
    """What the exchange has published about this instrument today.

    Assembled by the caller from `BitemporalIngestStore.rows_for(...)` with a `known_by` instant, so
    this module reads facts rather than fetching them and a backtest cannot see a ban declared after
    the decision. Every field is `None` when the source has not been read, which is DIFFERENT from
    `False`: an unread ban list is not an absence of bans.
    """

    trading_symbol: str
    is_fo_banned: bool | None = None
    mwpl_utilisation_fraction: Decimal | None = None
    mwpl_breach_threshold_fraction: Decimal | None = None
    lower_circuit_price_rupees: Decimal | None = None
    upper_circuit_price_rupees: Decimal | None = None
    surveillance_stage: str | None = None

    @property
    def unread_sources(self) -> tuple[str, ...]:
        """Which walls could not be checked. Reported, never assumed clear."""
        missing: list[str] = []
        if self.is_fo_banned is None:
            missing.append("fo_ban_list")
        if self.mwpl_utilisation_fraction is None:
            missing.append("mwpl_position_limits")
        if self.upper_circuit_price_rupees is None or self.lower_circuit_price_rupees is None:
            missing.append("circuit_bands")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class DerivedLimits:
    """Tier-2 limits, each computed from history rather than chosen.

    Constructed by `derive_limits`, which is where the percentiles are taken. Kept as a value object
    so the gate can be evaluated against limits computed at a point in time and the pair recorded
    together — a verdict whose limits cannot be recovered is a verdict nobody can audit.
    """

    maximum_notional_rupees: Decimal
    maximum_leverage: Decimal
    price_collar_fraction: Decimal
    maximum_orders_per_rate_window: int

    def __post_init__(self) -> None:
        for name, value in (
            ("maximum notional", self.maximum_notional_rupees),
            ("maximum leverage", self.maximum_leverage),
            ("price collar", self.price_collar_fraction),
        ):
            # The sizer and the session store both refuse floats; the gate was the hole. A float
            # limit survived construction, propagated through `tighten`, and turned a refusal into
            # an `AttributeError` from `.quantize` — a refusal that was never recorded at all
            # (`A.105` finding 7).
            if not isinstance(value, Decimal):
                raise RiskGateError(
                    f"{name} must be a Decimal, got {type(value).__name__}; a float limit puts "
                    "binary rounding between a rule and the order it is supposed to refuse"
                )
            if value <= 0:
                raise RiskGateError(f"{name} must be positive, got {value}")
        if self.maximum_orders_per_rate_window < 0:
            raise RiskGateError(
                f"an order-rate limit of {self.maximum_orders_per_rate_window} is not a count"
            )
        # ZERO is permitted, and deliberately. It refuses every order, which is exactly what an
        # operator asking for it means — the tightest position the key can be turned to. `R.22`'s
        # key must be able to reach "stop"; `derive_limits` never produces zero on its own.


@dataclass(frozen=True, slots=True)
class OperatorLimitOverrides:
    """The operator's key. It turns ONE way.

    Each field is optional; a supplied value replaces the derived one only if it is TIGHTER. A
    looser value is refused at construction rather than silently ignored, because an override the
    operator believes is in force and is not is worse than one that is rejected out loud.
    """

    maximum_notional_rupees: Decimal | None = None
    maximum_leverage: Decimal | None = None
    price_collar_fraction: Decimal | None = None
    maximum_orders_per_rate_window: int | None = None

    def tighten(self, derived: DerivedLimits) -> DerivedLimits:
        """Apply the overrides, refusing any that would widen a permission."""
        for name, override, derived_value in (
            (
                "maximum_notional_rupees",
                self.maximum_notional_rupees,
                derived.maximum_notional_rupees,
            ),
            ("maximum_leverage", self.maximum_leverage, derived.maximum_leverage),
            (
                "price_collar_fraction",
                self.price_collar_fraction,
                derived.price_collar_fraction,
            ),
            (
                "maximum_orders_per_rate_window",
                Decimal(self.maximum_orders_per_rate_window)
                if self.maximum_orders_per_rate_window is not None
                else None,
                Decimal(derived.maximum_orders_per_rate_window),
            ),
        ):
            if override is not None and override > derived_value:
                raise RiskGateError(
                    f"the operator override for {name} ({override}) is LOOSER than the derived "
                    f"limit ({derived_value}). This key only tightens: a limit that can be widened "
                    "is not a limit, and R.22's two-key rule survives only if one of the keys "
                    "cannot be turned permanently. Raise the derived limit's evidence instead"
                )
        # `is not None`, never `or`: `Decimal(0)` and `0` are FALSY, so an `or` fell through to the
        # derived value and silently discarded the TIGHTEST override an operator can give. Turning
        # the key to "stop trading" returned the full derived permission and raised nothing
        # (`A.105` finding 4).
        return DerivedLimits(
            maximum_notional_rupees=_tighter(
                derived.maximum_notional_rupees, self.maximum_notional_rupees
            ),
            maximum_leverage=_tighter(derived.maximum_leverage, self.maximum_leverage),
            price_collar_fraction=_tighter(
                derived.price_collar_fraction, self.price_collar_fraction
            ),
            maximum_orders_per_rate_window=int(
                _tighter(
                    Decimal(derived.maximum_orders_per_rate_window),
                    Decimal(self.maximum_orders_per_rate_window)
                    if self.maximum_orders_per_rate_window is not None
                    else None,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskGateVerdict:
    """Whether the order may be sent, and every reason it may not."""

    trading_symbol: str
    quantity: int
    notional_rupees: Decimal
    refusals: tuple[GateRefusal, ...]
    limits_applied: DerivedLimits
    unchecked_regulatory_sources: tuple[str, ...] = field(default=())

    @property
    def is_allowed(self) -> bool:
        return not self.refusals

    @property
    def first_refusal(self) -> GateRefusal | None:
        """The one that decides the outcome — tiers are evaluated in authority order."""
        return self.refusals[0] if self.refusals else None

    def describe(self) -> str:
        if self.is_allowed:
            unchecked = (
                f" (UNCHECKED: {', '.join(self.unchecked_regulatory_sources)})"
                if self.unchecked_regulatory_sources
                else ""
            )
            return (
                f"{self.trading_symbol}: {self.quantity} units, Rs {self.notional_rupees} "
                f"ALLOWED{unchecked}"
            )
        reasons = " | ".join(refusal.describe() for refusal in self.refusals)
        return f"{self.trading_symbol}: REFUSED — {reasons}"


def derive_limits(
    *,
    deployable_rupees: Decimal,
    traded_value_percentile_rupees: Decimal,
    realised_move_percentile_fraction: Decimal,
    segment_margin_fraction: Decimal,
    registration_threshold_orders_per_second: int,
    rate_safety_divisor: int = 2,
) -> DerivedLimits:
    """Tier-2 limits, each computed rather than chosen (`R.03`).

    Args:
        traded_value_percentile_rupees: a percentile of the instrument's OWN traded value. An order
            that would be a visible fraction of the day's volume is refused before impact modelling
            is asked to excuse it.
        realised_move_percentile_fraction: a percentile of the instrument's OWN realised move over
            the decision horizon. A limit price further out than the scrip actually travels is not a
            price, it is a hope.
        segment_margin_fraction: the fraction of notional this segment requires as margin. Its
            reciprocal IS the maximum leverage — the exchange's own number, not a chosen multiple.
        registration_threshold_orders_per_second: the regulatory threshold above which a
            self-developed algo must register (`NSE/INVG/67858` para B.5, 10/sec — `A.101`
            decision 2 keeps this system below it by measurement rather than hope).
        rate_safety_divisor: how far below the threshold to sit. Two, because a limit set AT a
            regulatory threshold is breached by any burst, and the margin must be visible rather
            than implied.
    """
    if segment_margin_fraction <= 0 or segment_margin_fraction > 1:
        raise RiskGateError(
            f"a segment margin fraction of {segment_margin_fraction} is not a fraction of "
            "notional; leverage is its reciprocal and would be meaningless"
        )
    if deployable_rupees <= 0:
        raise RiskGateError(
            "limits cannot be derived against no capital; the sizer already refuses this case and "
            "the gate must not paper over it with a limit of zero"
        )
    return DerivedLimits(
        maximum_notional_rupees=min(traded_value_percentile_rupees, deployable_rupees),
        maximum_leverage=Decimal(1) / segment_margin_fraction,
        price_collar_fraction=realised_move_percentile_fraction,
        maximum_orders_per_rate_window=max(
            registration_threshold_orders_per_second // rate_safety_divisor, 1
        ),
    )


@dataclass(frozen=True, slots=True)
class PreTradeRiskGate:
    """The three tiers, in authority order, with the session's latches ahead of all of them."""

    def evaluate(
        self,
        *,
        sized: SizedPosition,
        state: SessionRiskState,
        facts: RegulatoryFacts,
        limits: DerivedLimits,
        daily_loss_limit: DailyLossLimit,
        limit_price_rupees: Decimal | None = None,
    ) -> RiskGateVerdict:
        """Every tier is evaluated; the first refusal decides, and all of them are reported."""
        refusals: list[GateRefusal] = []
        refusals.extend(self._session_refusals(state, daily_loss_limit))
        refusals.extend(self._regulatory_refusals(facts, limit_price_rupees))
        refusals.extend(self._derived_refusals(sized, state, limits, limit_price_rupees))
        if sized.refusal_reason is not None:
            refusals.append(
                GateRefusal(
                    tier=GateTier.SIZER,
                    rule="no_size",
                    detail=sized.refusal_reason,
                )
            )
        return RiskGateVerdict(
            trading_symbol=sized.trading_symbol,
            quantity=sized.quantity,
            notional_rupees=sized.notional_rupees,
            refusals=tuple(refusals),
            limits_applied=limits,
            unchecked_regulatory_sources=facts.unread_sources,
        )

    @staticmethod
    def _session_refusals(
        state: SessionRiskState, daily_loss_limit: DailyLossLimit
    ) -> list[GateRefusal]:
        """A halted session refuses everything before any per-order opinion is formed."""
        refusals = [
            GateRefusal(
                tier=GateTier.SESSION_LATCH,
                rule=latch.value,
                detail=(
                    f"the {latch.value} latch is tripped for {state.session_date.isoformat()} and "
                    "only an operator may clear it (R.22)"
                ),
            )
            for latch in state.tripped_latches
        ]
        if (
            daily_loss_limit.is_active
            and daily_loss_limit.limit_rupees is not None
            and state.realised_pnl_rupees < -daily_loss_limit.limit_rupees
        ):
            refusals.append(
                GateRefusal(
                    tier=GateTier.SESSION_LATCH,
                    rule="DAILY_LOSS_BREACHED",
                    detail=(
                        f"realised P&L Rs {state.realised_pnl_rupees} is beyond the derived daily "
                        f"loss limit of Rs {daily_loss_limit.limit_rupees} "
                        f"({daily_loss_limit.describe()}); the latch must be tripped"
                    ),
                )
            )
        return refusals

    @staticmethod
    def _regulatory_refusals(
        facts: RegulatoryFacts, limit_price_rupees: Decimal | None
    ) -> list[GateRefusal]:
        """Published facts. Never derived, never overridable, and never assumed when unread."""
        refusals: list[GateRefusal] = []
        if facts.is_fo_banned:
            refusals.append(
                GateRefusal(
                    tier=GateTier.REGULATORY,
                    rule="FO_BAN",
                    detail=(
                        f"{facts.trading_symbol} is on the exchange's F&O ban list today. This "
                        "is a published binary fact and no tier below may override it"
                    ),
                )
            )
        if (
            facts.mwpl_utilisation_fraction is not None
            and facts.mwpl_breach_threshold_fraction is not None
            and facts.mwpl_utilisation_fraction >= facts.mwpl_breach_threshold_fraction
        ):
            refusals.append(
                GateRefusal(
                    tier=GateTier.REGULATORY,
                    rule="MWPL",
                    detail=(
                        f"market-wide position limit utilisation is "
                        f"{facts.mwpl_utilisation_fraction} against a breach threshold of "
                        f"{facts.mwpl_breach_threshold_fraction}"
                    ),
                )
            )
        price = limit_price_rupees
        if price is not None:
            above_upper = (
                facts.upper_circuit_price_rupees is not None
                and price > facts.upper_circuit_price_rupees
            )
            if above_upper:
                refusals.append(
                    GateRefusal(
                        tier=GateTier.REGULATORY,
                        rule="CIRCUIT_BAND",
                        detail=(
                            f"limit price Rs {price} is above the upper circuit of Rs "
                            f"{facts.upper_circuit_price_rupees}; the exchange would reject it"
                        ),
                    )
                )
            below_lower = (
                facts.lower_circuit_price_rupees is not None
                and price < facts.lower_circuit_price_rupees
            )
            if below_lower:
                refusals.append(
                    GateRefusal(
                        tier=GateTier.REGULATORY,
                        rule="CIRCUIT_BAND",
                        detail=(
                            f"limit price Rs {price} is below the lower circuit of Rs "
                            f"{facts.lower_circuit_price_rupees}; the exchange would reject it"
                        ),
                    )
                )
        return refusals

    @staticmethod
    def _derived_refusals(
        sized: SizedPosition,
        state: SessionRiskState,
        limits: DerivedLimits,
        limit_price_rupees: Decimal | None,
    ) -> list[GateRefusal]:
        refusals: list[GateRefusal] = []
        if sized.notional_rupees > limits.maximum_notional_rupees:
            refusals.append(
                GateRefusal(
                    tier=GateTier.DERIVED,
                    rule="MAX_NOTIONAL",
                    detail=(
                        f"Rs {sized.notional_rupees} exceeds the derived maximum of Rs "
                        f"{limits.maximum_notional_rupees}"
                    ),
                )
            )
        if sized.deployable_rupees > 0:
            leverage = (
                state.open_exposure_rupees + sized.notional_rupees
            ) / sized.deployable_rupees
            if leverage > limits.maximum_leverage:
                refusals.append(
                    GateRefusal(
                        tier=GateTier.DERIVED,
                        rule="MAX_LEVERAGE",
                        detail=(
                            f"total exposure would reach {leverage.quantize(Decimal('0.001'))}x "
                            f"deployable capital against a segment maximum of "
                            f"{limits.maximum_leverage.quantize(Decimal('0.001'))}x"
                        ),
                    )
                )
        if state.orders_in_rate_window >= limits.maximum_orders_per_rate_window:
            refusals.append(
                GateRefusal(
                    tier=GateTier.DERIVED,
                    rule="ORDER_RATE",
                    detail=(
                        f"{state.orders_in_rate_window} order(s) already sent in the rate window "
                        f"against a limit of {limits.maximum_orders_per_rate_window}. This refuses "
                        "rather than queueing: an order deferred past its decision horizon is a "
                        "different trade"
                    ),
                )
            )
        reference = sized.reference_price_rupees
        if limit_price_rupees is not None and reference > 0:
            distance = abs(limit_price_rupees - reference) / reference
            if distance > limits.price_collar_fraction:
                refusals.append(
                    GateRefusal(
                        tier=GateTier.DERIVED,
                        rule="PRICE_COLLAR",
                        detail=(
                            f"limit price Rs {limit_price_rupees} sits "
                            f"{(distance * 100).quantize(Decimal('0.01'))}% from the reference "
                            f"against a derived collar of "
                            f"{(limits.price_collar_fraction * 100).quantize(Decimal('0.01'))}%"
                        ),
                    )
                )
        return refusals


def latches_to_trip(
    *, state: SessionRiskState, daily_loss_limit: DailyLossLimit, maximum_drawdown_fraction: Decimal
) -> tuple[RiskLatch, ...]:
    """Which halts the current state has earned. The gate reports; the caller trips.

    Separated deliberately: evaluating a gate must never have the side effect of halting a session,
    because a read that halts is a read nobody can perform safely to find out where they stand.
    """
    earned: list[RiskLatch] = []
    if (
        daily_loss_limit.is_active
        and daily_loss_limit.limit_rupees is not None
        and state.realised_pnl_rupees < -daily_loss_limit.limit_rupees
    ):
        earned.append(RiskLatch.DAILY_LOSS)
    if state.drawdown_fraction > maximum_drawdown_fraction:
        earned.append(RiskLatch.DRAWDOWN)
    return tuple(earned)


def segment_margin_fractions() -> Mapping[str, Decimal]:
    """Placeholder for the per-segment margin regime this gate divides into leverage.

    Deliberately EMPTY rather than populated with plausible numbers. The margin a segment
    requires is an exchange fact that changes, and a hardcoded table here would be the `L1.09`
    lot-size defect repeated one layer up: right until it is quietly wrong. The named consumer is
    the margin ingest (`L7.08`), tracked in `BACKLOG.md`; until it exists the caller must supply the
    fraction and this module refuses to invent one.
    """
    return {}
