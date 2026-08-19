"""The futures basis and carry engine — what the three futures bots decide on.

**The claim being tested.** A future's price and its underlying's price are tied together by an
arbitrage: hold the spot, sell the future, and the difference is a financing cost that has to equal
the risk-free carry to expiry, or someone is paid to do nothing. The difference is the **basis**;
annualised over the remaining life it is the **implied carry rate**. When that rate departs from
what the same contract's own carry usually is, something is dislocated — most often a squeeze, a
dividend
the market has just learned about, or a rollover crowd.

**Why basis rather than price.** A futures bot that traded price direction would be a cash bot with
a worse cost structure. The tradeable content of a futures contract that the underlying does not
already have is precisely the basis, and everything here is a statement about that difference.

**Three quantities, each measured:**

1. **basis** = future price minus spot price, in the instrument's own units;
2. **implied carry** = `(basis / spot) x (sessions per year / sessions to expiry)`, an annualised
   rate, so a 20-point basis eight sessions out and the same basis forty sessions out are not
   confused with each other — the naive version of this is the mistake that makes every near-expiry
   contract look dislocated;
3. **standardised carry**, the departure of today's implied carry from that contract's own history
   of the same quantity, via Welford's online moments. `R.03` end to end: nothing is compared
   against a typed rate.

**Open interest is carried but never traded on alone.** Rising open interest with a rising basis is
a long buildup and with a falling basis a short buildup, which is real information about who is
on the
other side — but it is a modifier of conviction here rather than a signal, because open interest is
published once a day and this project has 36 sessions of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

OBSERVATIONS_NEEDED_FOR_A_CARRY_DISTRIBUTION = 5
"""Below this the standardising distribution is noise. Same reasoning as the premium engine: the
quantity standardised is itself a ratio of two estimates."""

OBSERVATIONS_NEEDED_FOR_A_VARIANCE = 2
"""One observation has no deviation from its own mean. A definition, not a knob."""


class BasisDislocation(StrEnum):
    """What the standardised carry says.

    `UNMEASURABLE` is a first-class answer: a contract at expiry, a missing spot, or a contract with
    no carry history all produce it, and all three are ordinary.
    """

    EXPENSIVE = "expensive"
    FAIR = "fair"
    CHEAP = "cheap"
    UNMEASURABLE = "unmeasurable"


class OpenInterestBuildup(StrEnum):
    """Who is arriving, read off the joint move of price and open interest.

    The standard four-way futures taxonomy. `UNKNOWN` when either series is missing, which is most
    of the time on this project's 36 sessions of F&O bhavcopy.
    """

    LONG_BUILDUP = "long_buildup"
    SHORT_BUILDUP = "short_buildup"
    LONG_UNWINDING = "long_unwinding"
    SHORT_COVERING = "short_covering"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BasisCarryReading:
    """One contract's basis state, with every input kept visible for the decision trace."""

    basis: float | None
    implied_carry: float | None
    standardised_carry: float | None
    dislocation: BasisDislocation
    buildup: OpenInterestBuildup
    sessions_to_expiry: int
    observations: int

    @property
    def is_actionable(self) -> bool:
        return self.dislocation in (BasisDislocation.EXPENSIVE, BasisDislocation.CHEAP)


@dataclass(slots=True)
class _OnlineMoments:
    """Welford's online mean and variance. Stable where the naive sum-of-squares form is not."""

    count: int = 0
    mean: float = 0.0
    sum_of_squared_deviations: float = 0.0

    def update(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.sum_of_squared_deviations += delta * (value - self.mean)

    def standard_deviation(self) -> float | None:
        if self.count < OBSERVATIONS_NEEDED_FOR_A_VARIANCE:
            return None
        variance = self.sum_of_squared_deviations / (self.count - 1)
        if not math.isfinite(variance) or variance <= 0.0:
            return None
        return math.sqrt(variance)


@dataclass(slots=True)
class _ContractMemory:
    """What the engine remembers about one futures contract."""

    carry_moments: _OnlineMoments = field(default_factory=_OnlineMoments)
    last_future_price: float | None = None
    last_open_interest: int | None = None


class FuturesBasisCarryEngine:
    """Carries per-contract carry history and open-interest state; reports today's dislocation.

    **SOTA analog:** the cost-of-carry relation as implemented in every index-arbitrage desk's
    monitor, with the risk-free leg replaced by the contract's OWN historical carry. That
    substitution is deliberate and is the engine: comparing implied carry against a theoretical
    risk-free rate measures the market's structural basis — dividends, borrow cost, financing
    spreads — every single day, and would report a permanent dislocation on names that simply carry
    rich. Comparing it against its own history measures what CHANGED, which is the only part a trade
    can be built on.
    """

    def __init__(self) -> None:
        self._contracts: dict[str, _ContractMemory] = {}

    def observe(
        self,
        contract_key: str,
        *,
        future_price: float,
        spot_price: float,
        sessions_to_expiry: int,
        sessions_per_year: int,
        open_interest: int | None = None,
    ) -> None:
        """Record one session's basis for this contract, and advance its open-interest state."""
        memory = self._contracts.get(contract_key)
        if memory is None:
            memory = _ContractMemory()
            self._contracts[contract_key] = memory
        carry = implied_carry_rate(
            future_price=future_price,
            spot_price=spot_price,
            sessions_to_expiry=sessions_to_expiry,
            sessions_per_year=sessions_per_year,
        )
        if carry is not None:
            memory.carry_moments.update(carry)
        memory.last_future_price = future_price
        if open_interest is not None and open_interest >= 0:
            memory.last_open_interest = open_interest

    def read(
        self,
        contract_key: str,
        *,
        future_price: float,
        spot_price: float,
        sessions_to_expiry: int,
        sessions_per_year: int,
        dislocation_cut: float,
        open_interest: int | None = None,
    ) -> BasisCarryReading:
        """How dislocated this contract's carry is right now, against its own history."""
        carry = implied_carry_rate(
            future_price=future_price,
            spot_price=spot_price,
            sessions_to_expiry=sessions_to_expiry,
            sessions_per_year=sessions_per_year,
        )
        basis = (
            future_price - spot_price
            if math.isfinite(future_price) and math.isfinite(spot_price)
            else None
        )
        memory = self._contracts.get(contract_key)
        observations = 0 if memory is None else memory.carry_moments.count
        buildup = self._buildup_for(memory, future_price, open_interest)
        spread = None if memory is None else memory.carry_moments.standard_deviation()
        if (
            carry is None
            or memory is None
            or spread is None
            or observations < OBSERVATIONS_NEEDED_FOR_A_CARRY_DISTRIBUTION
        ):
            return BasisCarryReading(
                basis=basis,
                implied_carry=carry,
                standardised_carry=None,
                dislocation=BasisDislocation.UNMEASURABLE,
                buildup=buildup,
                sessions_to_expiry=sessions_to_expiry,
                observations=observations,
            )
        standardised = (carry - memory.carry_moments.mean) / spread
        if not math.isfinite(standardised):
            dislocation = BasisDislocation.UNMEASURABLE
        elif standardised >= dislocation_cut:
            dislocation = BasisDislocation.EXPENSIVE
        elif standardised <= -dislocation_cut:
            dislocation = BasisDislocation.CHEAP
        else:
            dislocation = BasisDislocation.FAIR
        return BasisCarryReading(
            basis=basis,
            implied_carry=carry,
            standardised_carry=standardised,
            dislocation=dislocation,
            buildup=buildup,
            sessions_to_expiry=sessions_to_expiry,
            observations=observations,
        )

    @staticmethod
    def _buildup_for(
        memory: _ContractMemory | None, future_price: float, open_interest: int | None
    ) -> OpenInterestBuildup:
        """The four-way taxonomy, or `UNKNOWN` when either series is missing."""
        if (
            memory is None
            or open_interest is None
            or memory.last_open_interest is None
            or memory.last_future_price is None
        ):
            return OpenInterestBuildup.UNKNOWN
        price_rose = future_price > memory.last_future_price
        interest_rose = open_interest > memory.last_open_interest
        if interest_rose:
            return (
                OpenInterestBuildup.LONG_BUILDUP if price_rose else
                OpenInterestBuildup.SHORT_BUILDUP
            )
        return (
            OpenInterestBuildup.SHORT_COVERING if price_rose else OpenInterestBuildup.LONG_UNWINDING
        )

    @property
    def contracts_tracked(self) -> int:
        return len(self._contracts)

    def contracts_with_a_carry_distribution(self) -> int:
        """How many contracts can actually be classified — the engine's own maturity."""
        return sum(
            1
            for memory in self._contracts.values()
            if memory.carry_moments.count >= OBSERVATIONS_NEEDED_FOR_A_CARRY_DISTRIBUTION
            and memory.carry_moments.standard_deviation() is not None
        )


def implied_carry_rate(
    *,
    future_price: float,
    spot_price: float,
    sessions_to_expiry: int,
    sessions_per_year: int,
) -> float | None:
    """The basis as an ANNUALISED rate, or `None` when it cannot be one.

    Annualising is the whole point and the most common thing to get wrong. A 20-point basis on a
    24,800 index is 0.08% — trivial over forty sessions and a 2.5% annual rate over eight. Comparing
    raw bases across expiries compares two different quantities that happen to share a unit.

    `None` at or past expiry rather than a very large number: dividing by zero sessions is not a
    very
    short horizon, it is the absence of one.
    """
    if sessions_to_expiry <= 0 or sessions_per_year <= 0:
        return None
    if not (math.isfinite(future_price) and math.isfinite(spot_price)) or spot_price <= 0:
        return None
    fraction_of_a_year = sessions_to_expiry / sessions_per_year
    if fraction_of_a_year <= 0.0:
        return None
    rate = ((future_price - spot_price) / spot_price) / fraction_of_a_year
    return rate if math.isfinite(rate) else None
