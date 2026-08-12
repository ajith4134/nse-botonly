"""Tests for capital configuration — written before the implementation (R.23 step 3).

Covers the three kinds R.23 requires: unit, property, adversarial. The real-data
pass (R.05) does not apply here: this module reads operator configuration, not
market data, so there is no market observation to verify against.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as hypothesis_strategies

from nse_algo_trader.capital_configuration import (
    MAXIMUM_SUPPORTED_CAPITAL_RUPEES,
    MINIMUM_SUPPORTED_CAPITAL_RUPEES,
    CapitalConfigurationError,
    TradingCapital,
    load_trading_capital_from_environment,
)

ONE_LAKH = Decimal("100000")
ONE_CRORE = Decimal("10000000")


# --------------------------------------------------------------------------- unit


@pytest.mark.unit
def test_the_declared_supported_range_is_one_lakh_to_one_crore() -> None:
    """A.23 — the operator-declared range this system claims to support."""
    assert MINIMUM_SUPPORTED_CAPITAL_RUPEES == ONE_LAKH
    assert MAXIMUM_SUPPORTED_CAPITAL_RUPEES == ONE_CRORE


@pytest.mark.unit
def test_money_is_decimal_never_float() -> None:
    """Binary floats cannot represent paise exactly; drift accumulates over a session."""
    capital = TradingCapital.of_rupees(ONE_LAKH)
    assert isinstance(capital.total_rupees, Decimal)
    assert isinstance(capital.rupees_for_fraction(Decimal("0.01")), Decimal)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("total", "fraction", "expected"),
    [
        (ONE_LAKH, Decimal("0.01"), Decimal("1000.00")),
        (ONE_CRORE, Decimal("0.01"), Decimal("100000.00")),
        (ONE_LAKH, Decimal("1"), Decimal("100000.00")),
        (ONE_LAKH, Decimal("0"), Decimal("0.00")),
    ],
)
def test_a_fraction_of_capital_converts_to_rupees(
    total: Decimal, fraction: Decimal, expected: Decimal
) -> None:
    assert TradingCapital.of_rupees(total).rupees_for_fraction(fraction) == expected


@pytest.mark.unit
def test_the_same_policy_fraction_scales_exactly_with_capital() -> None:
    """The point of the whole module (A.23): one policy, any account size.

    A 2% risk budget must mean 2% at both ends of the supported range — the rupee
    figures differ by exactly the capital ratio, and the policy does not change.
    """
    risk_budget = Decimal("0.02")
    small = TradingCapital.of_rupees(ONE_LAKH).rupees_for_fraction(risk_budget)
    large = TradingCapital.of_rupees(ONE_CRORE).rupees_for_fraction(risk_budget)
    assert large / small == ONE_CRORE / ONE_LAKH == Decimal(100)


@pytest.mark.unit
def test_rupees_convert_back_to_a_fraction_of_capital() -> None:
    capital = TradingCapital.of_rupees(ONE_LAKH)
    assert capital.fraction_of_capital(Decimal("2500")) == Decimal("0.025")


@pytest.mark.unit
def test_allocation_rounds_down_so_it_can_never_over_allocate() -> None:
    """Rounding up would allocate capital that does not exist."""
    capital = TradingCapital.of_rupees(Decimal("100001"))
    # 1/3 of 100001 = 33333.6666...; must land at 33333.66, never 33333.67.
    assert capital.rupees_for_fraction(Decimal(1) / Decimal(3)) == Decimal("33333.66")


# --------------------------------------------------------------------------- adversarial


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "rejected",
    [
        Decimal("99999.99"),  # a paisa below the floor
        Decimal("10000000.01"),  # a paisa above the ceiling
        Decimal("0"),
        Decimal("-100000"),
    ],
)
def test_capital_outside_the_supported_range_is_rejected(rejected: Decimal) -> None:
    with pytest.raises(CapitalConfigurationError):
        TradingCapital.of_rupees(rejected)


@pytest.mark.adversarial
def test_a_fraction_outside_zero_to_one_is_rejected() -> None:
    capital = TradingCapital.of_rupees(ONE_LAKH)
    for invalid in (Decimal("-0.01"), Decimal("1.01")):
        with pytest.raises(CapitalConfigurationError):
            capital.rupees_for_fraction(invalid)


@pytest.mark.adversarial
def test_float_capital_is_refused_outright() -> None:
    """Accepting a float here would silently reintroduce binary rounding."""
    with pytest.raises(CapitalConfigurationError):
        TradingCapital.of_rupees(100000.0)  # type: ignore[arg-type]


@pytest.mark.adversarial
def test_missing_configuration_raises_and_never_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guessed default would size real orders. It must fail loudly instead."""
    monkeypatch.delenv("NSE_TRADING_CAPITAL_RUPEES", raising=False)
    with pytest.raises(CapitalConfigurationError):
        load_trading_capital_from_environment()


@pytest.mark.adversarial
@pytest.mark.parametrize("malformed", ["", "  ", "abc", "1e5", "100_000", "₹100000"])
def test_malformed_configuration_raises(monkeypatch: pytest.MonkeyPatch, malformed: str) -> None:
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", malformed)
    with pytest.raises(CapitalConfigurationError):
        load_trading_capital_from_environment()


@pytest.mark.unit
def test_wellformed_configuration_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", "250000.50")
    assert load_trading_capital_from_environment().total_rupees == Decimal("250000.50")


# --------------------------------------------------------------------------- property


@pytest.mark.property
@given(
    total=hypothesis_strategies.decimals(
        min_value=MINIMUM_SUPPORTED_CAPITAL_RUPEES,
        max_value=MAXIMUM_SUPPORTED_CAPITAL_RUPEES,
        places=2,
    ),
    fraction=hypothesis_strategies.decimals(min_value=Decimal(0), max_value=Decimal(1), places=30),
)
def test_an_allocation_never_exceeds_the_exact_truncated_product(
    total: Decimal, fraction: Decimal
) -> None:
    """The invariant, stated so that it can actually fail.

    The earlier form asserted ``allocation <= total``, which is true under EVERY
    rounding mode and so could not fail — a mutation to ROUND_UP passed it. The
    real claim is stronger: the allocation equals the exact product truncated to
    paise. Computed here at 200 digits, independently of the implementation's own
    context, so a rounding-direction mutant is caught.
    """
    with localcontext() as exact:
        exact.prec = 200
        expected = (total * fraction).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assert TradingCapital.of_rupees(total).rupees_for_fraction(fraction) == expected


@pytest.mark.property
@given(
    total=hypothesis_strategies.decimals(
        min_value=MINIMUM_SUPPORTED_CAPITAL_RUPEES,
        max_value=MAXIMUM_SUPPORTED_CAPITAL_RUPEES,
        places=6,  # sub-paise digits, or quantisation is a no-op and the test is blind
    ),
)
def test_configured_capital_is_never_rounded_upward(total: Decimal) -> None:
    """Capital must never be invented at the point every size derives from it.

    Directly targets the mutant that previously survived: with ROUND_UP in
    ``__post_init__``, ``of_rupees(Decimal("100000.001"))`` returned 100000.01 —
    a paisa from nowhere — and the whole suite stayed green.
    """
    assert TradingCapital.of_rupees(total).total_rupees <= total


@pytest.mark.property
@given(
    total=hypothesis_strategies.decimals(
        min_value=MINIMUM_SUPPORTED_CAPITAL_RUPEES,
        max_value=MAXIMUM_SUPPORTED_CAPITAL_RUPEES,
        places=2,
    ),
    fraction=hypothesis_strategies.decimals(
        min_value=Decimal("0.000001"), max_value=Decimal(1), places=6
    ),
)
def test_converting_to_rupees_and_back_never_overstates_the_fraction(
    total: Decimal, fraction: Decimal
) -> None:
    """Round-trip must lose value downward only, never gain it.

    The earlier tolerance was symmetric and therefore blind to direction; this
    asserts the sign of the error as well as its size.
    """
    capital = TradingCapital.of_rupees(total)
    recovered = capital.fraction_of_capital(capital.rupees_for_fraction(fraction))
    assert recovered <= fraction
    assert fraction - recovered <= Decimal("0.01") / capital.total_rupees


@pytest.mark.adversarial
def test_the_plain_constructor_cannot_smuggle_an_invalid_amount() -> None:
    """Validation must live in __post_init__, not only in the classmethod.

    Found in adversarial review: TradingCapital(Decimal("NaN")) previously
    constructed cleanly and produced a NaN allocation with no exception.
    """
    for invalid in (
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-999999999"),
        Decimal(0),
    ):
        with pytest.raises(CapitalConfigurationError):
            TradingCapital(invalid)


@pytest.mark.adversarial
def test_non_ascii_digits_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Devanagari digit pasted among ASCII ones is invisible in a terminal.

    ``str.isdigit()`` is true for every Unicode Nd character and ``Decimal``
    accepts them, so the original validator read १०००००  as 100000.
    """
    deceptive_inputs = (
        "\u0967\u0966\u0966\u0966\u0966\u0966",  # Devanagari one-zero-zero-zero-zero-zero
        "\u0661\u0660\u0660\u0660\u0660\u0660",  # Arabic-Indic
        "1000\u0c660",  # a Telugu zero hidden among ASCII digits
        "10\u2075",  # superscript: isdigit() true, Decimal() raises
        "100000.",  # trailing bare decimal point
    )
    for deceptive in deceptive_inputs:
        monkeypatch.setenv("NSE_TRADING_CAPITAL_RUPEES", deceptive)
        with pytest.raises(CapitalConfigurationError):
            load_trading_capital_from_environment()


@pytest.mark.unit
def test_the_injected_environment_seam_is_exercised() -> None:
    """The DI parameter existed but no test used it; an unused seam is untested code."""
    loaded = load_trading_capital_from_environment({"NSE_TRADING_CAPITAL_RUPEES": "500000"})
    assert loaded.total_rupees == Decimal("500000.00")


@pytest.mark.adversarial
def test_a_high_precision_fraction_does_not_over_allocate() -> None:
    """Regression, from adversarial review 2026-08-10.

    Without an explicit high-precision context the product is computed under the
    default ``prec=28, ROUND_HALF_EVEN`` and rounds UP before ``quantize`` runs,
    so truncation is applied to an already-inflated number. This exact input
    returned 1234567.89 against an exact truncated value of 1234567.88 — one
    paisa of capital that does not exist.
    """
    capital = TradingCapital.of_rupees(ONE_CRORE)
    over_precise = Decimal("0.1234567889999999999999999999999")
    assert capital.rupees_for_fraction(over_precise) == Decimal("1234567.88")


@pytest.mark.adversarial
def test_sub_paise_capital_is_truncated_never_rounded_up() -> None:
    """Regression: capital must not be invented at the point sizes derive from it.

    With ROUND_UP in ``__post_init__`` this returned 100000.01 — a paisa from
    nowhere — and the suite stayed green because every generated value was
    already exact to paise.
    """
    assert TradingCapital.of_rupees(Decimal("100000.001")).total_rupees == Decimal("100000.00")
    assert TradingCapital.of_rupees(Decimal("100000.999")).total_rupees == Decimal("100000.99")
