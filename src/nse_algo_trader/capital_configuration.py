"""Configured trading capital, and the conversion between policy and rupees.

**This is configuration, not an engine** (R.23b). It has no solver and carries no
state between decisions. It is named for what it is so that no reader mistakes it
for one — the failure `docs/research/155` catalogues is precisely a labelling
layer wearing engine vocabulary.

It is nonetheless decision-path code: every position size in the system is derived
from the value it holds, which is why it took the full R.23 loop.

**The invariant it exists to enforce (R.03 / A.23):** no rupee quantity is ever a
literal. A threshold correct at ₹1 lakh is wrong by two orders of magnitude at
₹1 crore — a ₹5,000 cap is 5% of one account and 0.05% of the other, so the same
literal encodes two different risk policies. Limits are therefore expressed as
*fractions of capital*, and rupees are computed at runtime.

Design record: ``docs/research/200_capital_configuration_and_rupee_literal_guard.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from typing import Final

# The operator-declared range this system claims to support (decision A.23).
#
# Honesty note, from adversarial review 2026-08-10: R.23(e) exempts *physical or
# regulatory* facts, and an operator preference is neither. These are exempt on a
# third, narrower ground — they are not a tuning parameter but the declared
# ENVELOPE the system has been verified against, and their only use is to reject
# configuration outside it. They are deliberately not tunable at runtime: widening
# the range means re-verifying against the new range, which is a decision (A.xx),
# not a config edit. Genuine R.23(e) facts — NSE tick size, lot sizes, SEBI margin
# percentages — will live with their regulatory citation when their engines land.
MINIMUM_SUPPORTED_CAPITAL_RUPEES: Final[Decimal] = Decimal("100000")  # A.23: ₹1 lakh
MAXIMUM_SUPPORTED_CAPITAL_RUPEES: Final[Decimal] = Decimal("10000000")  # A.23: ₹1 crore

CAPITAL_ENVIRONMENT_VARIABLE: Final[str] = "NSE_TRADING_CAPITAL_RUPEES"

# 1 rupee = 100 paise — a property of INR itself, not a policy choice (R.23e).
_PAISE = Decimal("0.01")
# Wide enough that a capital-times-fraction product is exact before quantising;
# the default context (prec=28, ROUND_HALF_EVEN) rounds up and breaks ROUND_DOWN.
_EXACT_MONEY_PRECISION = 60
_NO_ALLOCATION = Decimal(0)
_WHOLE_CAPITAL = Decimal(1)


class CapitalConfigurationError(ValueError):
    """Configured capital is absent, malformed, or outside the supported range.

    Raised rather than defaulted, deliberately: a guessed capital would size real
    orders, and would do so plausibly enough to go unnoticed.
    """


def _require_finite_decimal(value: object, description: str) -> Decimal:
    """Return ``value`` as a finite ``Decimal``, or raise.

    NaN and Infinity are rejected explicitly rather than left to ordered
    comparison, because comparing against a NaN raises ``InvalidOperation`` — a
    different exception than every caller is told to expect — and a NaN that
    survives validation propagates silently into an order size.
    """
    if not isinstance(value, Decimal):
        raise CapitalConfigurationError(
            f"{description} must be an exact Decimal, not {type(value).__name__}: "
            "binary floats cannot represent paise exactly and the error compounds "
            "across a session's sizing calls"
        )
    if not value.is_finite():
        raise CapitalConfigurationError(
            f"{description} must be a finite amount; got {value}"
        )
    return value


@dataclass(frozen=True, slots=True)
class TradingCapital:
    """The capital the system is configured to trade, as an exact decimal amount.

    Frozen because capital changing underneath a sizing decision — between the
    check and the order — is a class of bug worth making impossible rather than
    testing for.

    Validation lives in ``__post_init__``, not only in :meth:`of_rupees`, so that
    the plain constructor cannot be used to smuggle an invalid amount past the
    checks. A NaN reaching a sizing path is the worst failure this module has.
    """

    total_rupees: Decimal

    def __post_init__(self) -> None:
        amount = _require_finite_decimal(self.total_rupees, "capital")
        if not MINIMUM_SUPPORTED_CAPITAL_RUPEES <= amount <= MAXIMUM_SUPPORTED_CAPITAL_RUPEES:
            raise CapitalConfigurationError(
                f"configured capital {amount} is outside the supported range "
                f"{MINIMUM_SUPPORTED_CAPITAL_RUPEES}-{MAXIMUM_SUPPORTED_CAPITAL_RUPEES} "
                "(A.23). Widen the declared range deliberately rather than trading "
                "outside what the system has been verified against."
            )
        quantised = amount.quantize(_PAISE, rounding=ROUND_DOWN)
        object.__setattr__(self, "total_rupees", quantised)

    @classmethod
    def of_rupees(cls, amount: Decimal) -> TradingCapital:
        """Build from an exact rupee amount. Validation happens in ``__post_init__``."""
        return cls(total_rupees=amount)

    def rupees_for_fraction(self, fraction: Decimal) -> Decimal:
        """Convert a policy fraction of capital into rupees at the configured size.

        Rounds **down** to paise, and does the multiplication under an explicit
        high-precision context first. Without that local context the product is
        computed under the default ``prec=28, ROUND_HALF_EVEN`` and can round *up*
        before ``quantize`` ever sees it — allocating a paisa that does not exist.
        Truncating an already-inflated product is not truncation.

        Raises:
            CapitalConfigurationError: the fraction is not a finite Decimal in
                ``[0, 1]``.
        """
        checked = _require_finite_decimal(fraction, "a fraction of capital")
        if not _NO_ALLOCATION <= checked <= _WHOLE_CAPITAL:
            raise CapitalConfigurationError(
                f"a fraction of capital must lie in [0, 1]; got {checked}"
            )
        with localcontext() as exact:
            exact.prec = _EXACT_MONEY_PRECISION
            product = self.total_rupees * checked
        return product.quantize(_PAISE, rounding=ROUND_DOWN)

    def fraction_of_capital(self, rupees: Decimal) -> Decimal:
        """Express an observed rupee amount as a fraction of configured capital.

        The inverse of :meth:`rupees_for_fraction`, and validated over the same
        domain — an inverse that accepts inputs its forward direction rejects is
        not an inverse.

        Raises:
            CapitalConfigurationError: the amount is not a finite Decimal, or lies
                outside ``[0, total_rupees]``.
        """
        checked = _require_finite_decimal(rupees, "a rupee amount")
        if not _NO_ALLOCATION <= checked <= self.total_rupees:
            raise CapitalConfigurationError(
                f"a rupee amount must lie in [0, {self.total_rupees}]; got {checked}"
            )
        with localcontext() as exact:
            exact.prec = _EXACT_MONEY_PRECISION
            return checked / self.total_rupees


def load_trading_capital_from_environment(
    environment: dict[str, str] | None = None,
) -> TradingCapital:
    """Load configured capital from the environment (R.02 — never from a file).

    Args:
        environment: injected mapping for tests; defaults to the real environment.

    Raises:
        CapitalConfigurationError: the variable is unset, blank, not an exact
            decimal string, or outside the supported range. Scientific notation
            and digit separators are refused because both are easy to mistype by
            an order of magnitude, and this value sizes real orders.
    """
    source = os.environ if environment is None else environment
    raw = source.get(CAPITAL_ENVIRONMENT_VARIABLE, "").strip()
    if not raw:
        raise CapitalConfigurationError(
            f"{CAPITAL_ENVIRONMENT_VARIABLE} is not set. It has no default: a "
            "guessed capital would size real orders."
        )
    candidate = raw.replace(".", "", 1)
    if not (candidate.isascii() and candidate.isdigit()) or raw.endswith("."):
        raise CapitalConfigurationError(
            f"{CAPITAL_ENVIRONMENT_VARIABLE}={raw!r} must be plain digits with an "
            "optional decimal point — no separators, no scientific notation, no "
            "currency symbol, no non-ASCII digits, no trailing point. A Devanagari or "
            "Arabic-Indic digit pasted among ASCII ones is invisible in a terminal "
            "and is exactly the order-of-magnitude typo this rejects"
        )
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise CapitalConfigurationError(
            f"{CAPITAL_ENVIRONMENT_VARIABLE}={raw!r} is not an exact decimal amount"
        ) from exc
    return TradingCapital.of_rupees(amount)
