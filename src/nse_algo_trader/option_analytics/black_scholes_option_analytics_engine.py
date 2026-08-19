"""Option pricing, implied volatility and Greeks — the analytics the two option bots decide on.

**Why this exists at all.** `L5.27` and `L5.28` (the index-option and stock-option bots) cannot
propose anything honest without knowing what a contract is worth, how far its market price sits from
that, and what risk the position carries. Nothing in this project could answer any of those: a grep
for `black_scholes`, `implied_vol`, `greeks` or `delta` over `src/` returned only unrelated matches.

**Sourced, not written from scratch** (`R.17`, and the search is recorded in
`docs/research/263`). `vollib` 1.0.12 is installed and was exercised on NSE-shaped inputs before
being adopted: a 7-day at-the-money NIFTY-like call at S=K=24,800, r=6.5%, sigma=12% prices at
Rs 180.2222 and round-trips through its implied-volatility solver to 0.11999998. It carries Jäckel's
*Let's Be Rational* underneath, which is the reference analytic IV inversion, and it behaves
correctly at the boundary this project actually hits — a zero premium returns an implied volatility
of **0.0**, where the deprecated `py_vollib` shim returned the last value it was given.

**QuantLib 1.43 was also installed and run, and is rejected with the measurement** (`R.17` again,
never README prose): it prices the same contract identically (Rs 180.2222, delta 0.5332), so the
rejection is not about correctness. It is that its Greeks require rebuilding a pricing engine per
evaluation (2,000 prices in 18 ms against `vollib`'s 9 ms), its `India(NSE)` calendar duplicates
`NseTradingSessionCalendar` — which is this project's own sourced authority and the thing every
other module already agrees with — and its vega is quoted per 100% of volatility while `vollib`'s is
per 1 percentage point. Two calendars and two vega conventions in one codebase is a defect waiting
for a bad afternoon.

**Every Greek convention below was measured against a numerical derivative, not read off a README:**

| Greek | `vollib` reports | numerical check |
|---|---|---|
| delta | 0.5332021587 | 0.5332021197 per 1 unit of underlying |
| vega | 13.6539 | 13.6568 per **1 percentage point** of volatility |
| theta | -14.0261 | -14.4759 per **calendar day** |
| rho | 2.5014 | per 1 percentage point of rate |

Theta's 3% gap against the one-day finite difference is the curvature of theta itself over that day,
not an error — the analytic value is the instantaneous rate at `t`, the difference is the average
across the day. Stated because a reader checking this later will find the same gap.

**Time to expiry is measured in TRADING sessions, not calendar days** (`R.03`). NSE closes for
weekends and for a dozen holidays a year, and a calendar-day year fraction prices a Thursday-expiry
weekly as though the market breathed through the weekend. The denominator is this project's own
`NseTradingSessionCalendar`, whose session count per year IS the annualisation factor — derived from
the exchange's published calendar rather than asserted as 252.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from vollib.black_scholes import black_scholes
from vollib.black_scholes.greeks.analytical import delta, gamma, rho, theta, vega
from vollib.black_scholes.implied_volatility import implied_volatility

from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar

PAISE_PER_RUPEE = Decimal("100")
"""A unit conversion in the currency itself. This project stores money in paise; `vollib` is
dimensionless and is fed rupees, because floating-point paise on an index at 24,800 wastes six
digits of mantissa on a unit that options are not quoted in."""


class OptionAnalyticsError(Exception):
    """The contract cannot be analysed.

    Inventing the missing quantity would fabricate an edge, so every path raises or answers `None`.
    """


class OptionRight(StrEnum):
    """Call or put, in the one-letter form `vollib` takes, so no translation layer can drift."""

    CALL = "c"
    PUT = "p"


class Moneyness(StrEnum):
    """Where the strike sits relative to spot. A classification, not a threshold.

    The boundaries are the two facts that actually exist: a strike is in the money when exercising
    it now would pay something, and at the money only when it equals spot exactly. Everything
    between is decided by the strike ladder, which is an exchange fact, so this enum never invents a
    band width (`R.03`).
    """

    IN_THE_MONEY = "in_the_money"
    AT_THE_MONEY = "at_the_money"
    OUT_OF_THE_MONEY = "out_of_the_money"


@dataclass(frozen=True, slots=True)
class OptionContractTerms:
    """One option contract, in the units this project stores.

    Prices are paise because that is what `PricedSignal`, the cost engine and the bar store all
    carry; the conversion to rupees happens once, here, at the boundary with `vollib`.
    """

    underlying_price_paise: Decimal
    strike_paise: Decimal
    expiry: date
    right: OptionRight
    lot_size: int

    def __post_init__(self) -> None:
        if self.underlying_price_paise <= 0:
            raise OptionAnalyticsError(
                f"underlying price is {self.underlying_price_paise} paise; an option on a "
                f"non-positive underlying has no Black-Scholes value and the log would be undefined"
            )
        if self.strike_paise <= 0:
            raise OptionAnalyticsError(
                f"strike is {self.strike_paise} paise; a strike is a price and a non-positive one "
                f"cannot be exercised at"
            )
        if self.lot_size <= 0:
            raise OptionAnalyticsError(
                f"lot size is {self.lot_size}; a substituted lot size is worse than an absent one "
                f"(`L1.09`), because it silently changes what a position is worth"
            )

    @property
    def underlying_price_rupees(self) -> float:
        return float(self.underlying_price_paise / PAISE_PER_RUPEE)

    @property
    def strike_rupees(self) -> float:
        return float(self.strike_paise / PAISE_PER_RUPEE)

    def moneyness(self) -> Moneyness:
        """Where this strike sits. Exact equality is the only honest at-the-money test."""
        if self.strike_paise == self.underlying_price_paise:
            return Moneyness.AT_THE_MONEY
        if self.right is OptionRight.CALL:
            return (
                Moneyness.IN_THE_MONEY
                if self.strike_paise < self.underlying_price_paise
                else Moneyness.OUT_OF_THE_MONEY
            )
        return (
            Moneyness.IN_THE_MONEY
            if self.strike_paise > self.underlying_price_paise
            else Moneyness.OUT_OF_THE_MONEY
        )

    def intrinsic_value_rupees(self) -> float:
        """What exercising now would pay. The hard lower bound on any European premium.

        Load-bearing rather than decorative: a market premium below intrinsic is not a cheap option,
        it is a stale or crossed quote, and handing it to an IV solver produces either a raise or a
        number with no meaning. `implied_volatility_of` refuses it by name.
        """
        if self.right is OptionRight.CALL:
            return max(0.0, self.underlying_price_rupees - self.strike_rupees)
        return max(0.0, self.strike_rupees - self.underlying_price_rupees)


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    """The risk of one contract, in the units measured against numerical derivatives above.

    Per CONTRACT, never per lot. Multiplying by the lot size is the caller's job because the caller
    is the only one that knows how many lots it is proposing, and a Greek silently pre-multiplied by
    140 is the kind of quantity that looks fine until it is added to another one that was not.
    """

    delta_per_unit_of_underlying: float
    gamma_per_unit_of_underlying: float
    vega_per_volatility_point: float
    theta_per_calendar_day: float
    rho_per_rate_point: float

    def scaled_to_position(self, lots: int, lot_size: int) -> OptionGreeks:
        """The same risk for a real position. Explicit, so the units are never ambiguous."""
        if lots <= 0 or lot_size <= 0:
            raise OptionAnalyticsError(
                f"cannot scale Greeks to {lots} lots of {lot_size}; a non-positive position is not "
                f"a position and would silently flip every sign"
            )
        multiplier = float(lots * lot_size)
        return OptionGreeks(
            delta_per_unit_of_underlying=self.delta_per_unit_of_underlying * multiplier,
            gamma_per_unit_of_underlying=self.gamma_per_unit_of_underlying * multiplier,
            vega_per_volatility_point=self.vega_per_volatility_point * multiplier,
            theta_per_calendar_day=self.theta_per_calendar_day * multiplier,
            rho_per_rate_point=self.rho_per_rate_point * multiplier,
        )


class BlackScholesOptionAnalyticsEngine:
    """Prices contracts, inverts premia to volatility, and reports risk — with a real calendar.

    Carries the session calendar and the risk-free rate as state because both are properties of the
    market rather than of a single call, and passing them per call is how two decisions in the same
    session end up annualised differently.

    **SOTA analog:** `QuantLib`'s `AnalyticEuropeanEngine`, installed and measured to agree with
    this to the fourth decimal on the same contract. The difference is the calendar and the
    Greek conventions, both documented in the module docstring.
    """

    def __init__(
        self,
        *,
        risk_free_rate: float,
        calendar: NseTradingSessionCalendar | None = None,
    ) -> None:
        if not math.isfinite(risk_free_rate) or risk_free_rate < 0.0:
            raise OptionAnalyticsError(
                f"risk-free rate {risk_free_rate!r} is not a usable rate; a negative or non-finite "
                f"discount factor makes every forward price meaningless"
            )
        self._risk_free_rate = risk_free_rate
        self._calendar = calendar or NseTradingSessionCalendar()
        self._sessions_per_year_cache: dict[int, int] = {}

    @property
    def risk_free_rate(self) -> float:
        return self._risk_free_rate

    def sessions_per_year(self, year: int) -> int:
        """The exchange's own session count — the annualisation denominator, measured not assumed.

        `R.03`: 252 is a folklore constant. NSE publishes its holiday list, this project already
        parses it, and the answer differs by year.
        """
        if year not in self._sessions_per_year_cache:
            sessions = self._calendar.sessions_between(date(year, 1, 1), date(year, 12, 31))
            count = len(tuple(sessions))
            if count <= 0:
                raise OptionAnalyticsError(
                    f"the calendar reports {count} trading sessions in {year}; an annualisation "
                    f"denominator of zero would divide every volatility by nothing"
                )
            self._sessions_per_year_cache[year] = count
        return self._sessions_per_year_cache[year]

    def trading_sessions_to_expiry(self, decision_instant: datetime, expiry: date) -> int:
        """Sessions remaining, expiry INCLUDED — it trades on its own expiry day.

        Zero when the decision instant is on or after the expiry session, which the caller must
        treat as unpriceable rather than as a very short option: at zero time to expiry the option
        is its intrinsic value and there is no volatility to speak of.
        """
        if decision_instant.tzinfo is None:
            raise OptionAnalyticsError(
                "the decision instant carries no timezone; a naive instant is read as UTC and "
                "moves every expiry in this project by five and a half hours"
            )
        today = decision_instant.date()
        if expiry < today:
            return 0
        return len(tuple(self._calendar.sessions_between(today, expiry)))

    def year_fraction_to_expiry(self, decision_instant: datetime, expiry: date) -> float:
        """Time to expiry as a fraction of a TRADING year.

        The one modelling choice in this module that is not forced, and it is made deliberately:
        volatility accrues while the market is open, so both the numerator and the denominator are
        counted in sessions. Using calendar days for one and sessions for the other is the mistake
        that makes every weekly option look mispriced on a Friday.
        """
        sessions = self.trading_sessions_to_expiry(decision_instant, expiry)
        if sessions <= 0:
            return 0.0
        return sessions / self.sessions_per_year(expiry.year)

    def price_rupees(
        self, terms: OptionContractTerms, *, volatility: float, decision_instant: datetime
    ) -> float:
        """Black-Scholes value of one contract, in rupees."""
        self._require_usable_volatility(volatility)
        years = self.year_fraction_to_expiry(decision_instant, terms.expiry)
        if years <= 0.0:
            return terms.intrinsic_value_rupees()
        return float(
            black_scholes(
                terms.right.value,
                terms.underlying_price_rupees,
                terms.strike_rupees,
                years,
                self._risk_free_rate,
                volatility,
            )
        )

    def implied_volatility_of(
        self,
        terms: OptionContractTerms,
        *,
        market_premium_paise: Decimal,
        decision_instant: datetime,
    ) -> float | None:
        """Invert a traded premium to volatility, or `None` when the quote cannot carry one.

        `None` rather than a raise, and rather than a fallback number: an unpriceable quote is a
        finding about the quote, and the bots treat it as "no opinion on this strike" — the same
        distinction `GateVerdict` draws between `VETO` and `UNPRICEABLE`. Three real cases return
        it, each of which occurs daily in the NSE option chain:

        1. **expiry reached** — no time value left to invert;
        2. **premium below intrinsic** — a stale or crossed quote, not a cheap option;
        3. **the solver cannot bracket it** — a deep out-of-the-money strike quoted at the 5-paise
           tick, where the whole premium is smaller than one tick of the underlying.
        """
        if market_premium_paise <= 0:
            return None
        premium = float(market_premium_paise / PAISE_PER_RUPEE)
        years = self.year_fraction_to_expiry(decision_instant, terms.expiry)
        if years <= 0.0:
            return None
        if premium < terms.intrinsic_value_rupees():
            return None
        try:
            solved = float(
                implied_volatility(
                    premium,
                    terms.underlying_price_rupees,
                    terms.strike_rupees,
                    years,
                    self._risk_free_rate,
                    terms.right.value,
                )
            )
        except Exception:  # noqa: BLE001 — the solver raises several unrelated types on no-bracket
            return None
        if not math.isfinite(solved) or solved <= 0.0:
            return None
        return solved

    def greeks_of(
        self, terms: OptionContractTerms, *, volatility: float, decision_instant: datetime
    ) -> OptionGreeks | None:
        """Risk of one contract, or `None` at or past expiry where the derivatives are undefined."""
        self._require_usable_volatility(volatility)
        years = self.year_fraction_to_expiry(decision_instant, terms.expiry)
        if years <= 0.0:
            return None
        arguments = (
            terms.right.value,
            terms.underlying_price_rupees,
            terms.strike_rupees,
            years,
            self._risk_free_rate,
            volatility,
        )
        return OptionGreeks(
            delta_per_unit_of_underlying=float(delta(*arguments)),
            gamma_per_unit_of_underlying=float(gamma(*arguments)),
            vega_per_volatility_point=float(vega(*arguments)),
            theta_per_calendar_day=float(theta(*arguments)),
            rho_per_rate_point=float(rho(*arguments)),
        )

    @staticmethod
    def _require_usable_volatility(volatility: float) -> None:
        if not math.isfinite(volatility) or volatility <= 0.0:
            raise OptionAnalyticsError(
                f"volatility {volatility!r} is not usable; zero or negative volatility collapses "
                f"the log-normal distribution to a point and every Greek to zero, which would read "
                f"as a risk-free position rather than as an unpriceable one"
            )
