"""How much of the book a measured edge justifies — Kelly, shrunk by its own reliability (`L7.01`).

Specification: `docs/research/227_position_sizing_and_risk_gate_spec.md` §3.2.

Kelly answers a question volatility-targeting cannot: **is this edge worth the risk that sizing it
would take on.** It is used here as a CAP on a volatility-targeted size, never as the size itself,
because Kelly's failure mode is asymmetric and points the wrong way — a 2-fold overestimate of the
edge
is a 2-fold oversize, and the estimate is precisely the thing a thin sample gets wrong.

**The Kelly fraction is not multiplied by a chosen number.** "Half-Kelly" and "quarter-Kelly" are
the folk answer to that fragility and they are exactly the magic constant `R.03` forbids: the ½ is
nobody's measurement. Instead the estimate is shrunk toward zero by its own measured reliability,

    shrinkage = edge² / (edge² + se²)   ==   t² / (t² + 1)

which is the standard James-Stein / empirical-Bayes weight, and every term is already published by
`F01`'s calibrator (`ReversionCapture.mean_captured_bps`, `.standard_error_bps`). The behaviour is
the whole point:

| t = edge/se | shrinkage | effect                          |
|-------------|-----------|---------------------------------|
| 5           | 0.96      | sized at ~full Kelly            |
| 2           | 0.80      | sized down a fifth              |
| 1           | 0.50      | halved                          |
| 0.5         | 0.20      | all but eliminated              |

An edge measured badly therefore sizes small **by construction** rather than by anybody's caution,
and an edge measured well is not penalised for the existence of bad ones elsewhere.

**A consequence worth stating plainly:** this makes the calibration's standard error a direct input
to real position size, so the `M10` open caveat (optimistic standard errors) stops being a
statistical footnote and becomes a sizing defect. It is recorded in `BACKLOG.md` against this
module.

**The dispersion is passed IN, and that is the whole point of this signature.** The first version
read it from `ReversionCapture.mean_captured_sigma`, which is not a dispersion at all: the
calibrator defines it as the mean captured move EXPRESSED IN sigma units — the numerator rescaled,
signed, and negative on nine of the operator's forty real calibrations. Substituting it made
`raw_kelly = sigma_price² / (1e4 · edge)`, so **the Kelly cap fell as the edge rose**: the best
measured edge in the database (203.5 bps at t=2.88) was sized 6.5 times SMALLER than a 22.7 bps one,
and seventeen of forty real rows hit the whole-book cap. Found by the `R.23(c)` review (`A.105`).
Kelly's denominator is the variance of the RETURN this position is exposed to, so the caller hands
in the instrument's own realised volatility over the same horizon and this module refuses to invent
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow

from nse_algo_trader.cost_gate.mean_reversion_edge_calibrator import ReversionCapture

BASIS_POINTS_IN_ONE = Decimal(10_000)
"""A basis point is one ten-thousandth. A definitional constant, not a threshold."""

FULL_CAPITAL_FRACTION = Decimal(1)
"""The most of the book a single position may be sized to by this scaler.

Full Kelly on a large edge over a small dispersion exceeds 1 — a 300 bps edge against 2% dispersion
is 7.5x capital — and a fraction above one means BORROWING. Whether this book may borrow is the risk
gate's decision (`L7.02`, margin and leverage), never a side effect of the sizer's arithmetic
running past one. Capping here makes the sizer's output always a fraction of capital that exists.
"""


class KellyScalingError(Exception):
    """The Kelly fraction cannot be formed, and any substituted number would size a real order."""


@dataclass(frozen=True, slots=True)
class ScaledKellyFraction:
    """The fraction of capital this edge justifies, with everything needed to argue with it."""

    edge_bps: Decimal
    standard_error_bps: Decimal
    dispersion: Decimal
    """The standard deviation of the RETURN, as a fraction of price, over the decision horizon.

    Passed in by the caller from the instrument's own realised volatility. Never read from the
    calibration — see the module docstring for what that cost.
    """

    raw_kelly_fraction: Decimal
    shrinkage: Decimal
    kelly_fraction: Decimal
    was_capped_at_full_capital: bool
    refusal_reason: str | None = None

    @property
    def t_statistic(self) -> Decimal:
        """How many standard errors the edge sits from zero — the number sizing really turns on."""
        return self.edge_bps / self.standard_error_bps

    @property
    def is_distinguishable_from_zero(self) -> bool:
        """Two standard errors, the same one-sided bar the calibrator applies."""
        return self.t_statistic > Decimal(2)

    def describe(self) -> str:
        if self.refusal_reason is not None:
            return f"no Kelly size: {self.refusal_reason}"
        return (
            f"edge {self.edge_bps} bps +/- {self.standard_error_bps} (t="
            f"{self.t_statistic.quantize(Decimal('0.01'))}) over dispersion {self.dispersion} "
            f"gives raw Kelly {self.raw_kelly_fraction.quantize(Decimal('0.0001'))}, shrunk by "
            f"{self.shrinkage.quantize(Decimal('0.0001'))} to "
            f"{self.kelly_fraction.quantize(Decimal('0.0001'))} of capital"
            + (" (capped at the whole book)" if self.was_capped_at_full_capital else "")
        )


def shrinkage_weight(*, edge_bps: Decimal, standard_error_bps: Decimal) -> Decimal:
    """`edge² / (edge² + se²)` — the weight an estimate earns by being measured well.

    Equivalently `t²/(t²+1)`, which is why it is scale-free: doubling both the edge and its error
    leaves the weight unchanged, as it should, because nothing about the evidence improved.
    """
    if not isinstance(edge_bps, Decimal) or not isinstance(standard_error_bps, Decimal):
        raise KellyScalingError(
            "edge and standard error must be Decimal; a float here would put binary rounding "
            "between a calibration and a real order size"
        )
    if standard_error_bps <= 0:
        raise KellyScalingError(
            "a standard error of zero would make the shrinkage weight 1.0 — perfect knowledge of "
            "the edge — which no finite sample earns. A degenerate sample must not become maximum "
            "leverage"
        )
    try:
        edge_squared = edge_bps * edge_bps
        error_squared = standard_error_bps * standard_error_bps
        return edge_squared / (edge_squared + error_squared)
    except (Overflow, InvalidOperation) as failure:
        raise KellyScalingError(
            f"the shrinkage weight overflowed for an edge of {edge_bps} bps with a standard error "
            f"of {standard_error_bps}: {failure}"
        ) from failure


def _finite_decimal(value: Decimal, description: str) -> Decimal:
    """A real, finite `Decimal`. A float here would put binary rounding into a position size."""
    if not isinstance(value, Decimal):
        raise KellyScalingError(
            f"{description} must be a Decimal, got {type(value).__name__}"
        )
    if not value.is_finite():
        raise KellyScalingError(f"{description} must be finite, and {value} is not")
    return value


@dataclass(frozen=True, slots=True)
class KellyEdgeScaler:
    """Turns one calibrated edge into the fraction of capital it justifies."""

    def scale(
        self, capture: ReversionCapture, *, return_dispersion: Decimal
    ) -> ScaledKellyFraction:
        """The fraction of capital this calibration supports, shrunk by its own reliability.

        Args:
            capture: the calibrated edge and its standard error.
            return_dispersion: the standard deviation of the return over the SAME horizon, as a
                fraction of price. Keyword-only and with no default on purpose: a default here
                would be a dispersion nobody measured, and reading one off the calibration is
                exactly the defect `A.105` recorded.

        Raises:
            KellyScalingError: no fraction can be formed — a non-positive dispersion (infinite
                Kelly), a zero standard error (claimed certainty), or a magnitude that overflows.
                Refusals rather than clamps, because a substituted number would size a real order.
        """
        dispersion = _finite_decimal(return_dispersion, "return dispersion")
        if dispersion <= 0:
            raise KellyScalingError(
                f"a return dispersion of {dispersion} implies an infinite Kelly fraction, which is "
                "the single worst answer this module could return; an instrument that does not "
                "move is not an infinitely attractive one"
            )
        weight = shrinkage_weight(
            edge_bps=capture.mean_captured_bps, standard_error_bps=capture.standard_error_bps
        )

        if capture.mean_captured_bps <= 0:
            # A mean-reversion LONG whose measured capture is negative says the edge is ABSENT.
            # It does not say the mirror trade works: nobody fitted that strategy, and sizing one
            # off this number would be inventing it.
            return ScaledKellyFraction(
                edge_bps=capture.mean_captured_bps,
                standard_error_bps=capture.standard_error_bps,
                dispersion=dispersion,
                raw_kelly_fraction=Decimal(0),
                shrinkage=weight,
                kelly_fraction=Decimal(0),
                was_capped_at_full_capital=False,
                refusal_reason=(
                    f"the calibrated edge is negative ({capture.mean_captured_bps} bps), which "
                    "means this edge is absent rather than reversed; no position is sized"
                ),
            )

        try:
            raw = (capture.mean_captured_bps / BASIS_POINTS_IN_ONE) / (dispersion * dispersion)
        except (DivisionByZero, InvalidOperation, Overflow) as failure:
            raise KellyScalingError(
                f"the Kelly fraction could not be formed from an edge of "
                f"{capture.mean_captured_bps} bps over a dispersion of {dispersion}: {failure}"
            ) from failure

        scaled = raw * weight
        capped = scaled > FULL_CAPITAL_FRACTION
        return ScaledKellyFraction(
            edge_bps=capture.mean_captured_bps,
            standard_error_bps=capture.standard_error_bps,
            dispersion=dispersion,
            raw_kelly_fraction=raw,
            shrinkage=weight,
            kelly_fraction=FULL_CAPITAL_FRACTION if capped else scaled,
            was_capped_at_full_capital=capped,
        )
