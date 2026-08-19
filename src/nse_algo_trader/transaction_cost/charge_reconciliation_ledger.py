"""The engine measuring its own error against real contract notes.

A cost model that only ever compares itself to its own arithmetic is a tautology. This is the
part that can be wrong out loud: every executed order's real broker charges are recorded
against what the engine said they would be, per component, and the residuals accrue.

**It does not correct the rates from the residuals, deliberately.** Fitting a statutory rate
to observed billing would launder a stale circular into a tuned parameter and destroy the
provenance chain that makes the rest of this engine worth anything. A drifting component is a
DEFECT SIGNAL about the rate table, and the fix is a new dated `MarketRuleRecord`, not a
correction factor. What the ledger produces is the evidence that one is needed.

Per `R.04`, thin data gates the VERDICT, never the algorithm: with zero contract notes the
ledger is fully built and every component reports `UNVERIFIED`, and it arms itself as notes
accrue with no code change. The threshold for "enough" is derived rather than declared — a
component becomes verifiable when the confidence interval around its mean residual is narrower
than the rounding granularity of the component itself, which is precisely the point at which
drift becomes distinguishable from rounding noise.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from statistics import NormalDist

from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    ChargeComponent,
    RoundingRule,
    TradeLeg,
)

DEFAULT_LEDGER_PATH = Path("~/.nse_algo_trader/transaction_cost.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS charge_observation (
    broker TEXT NOT NULL,
    order_reference TEXT NOT NULL,
    leg TEXT NOT NULL,
    component TEXT NOT NULL,
    segment TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    modelled_paise TEXT NOT NULL,
    actual_paise TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (broker, order_reference, leg, component)
);
CREATE INDEX IF NOT EXISTS charge_observation_by_component
    ON charge_observation (broker, segment, component);
"""

_ROUNDING_GRANULARITY_PAISE: dict[RoundingRule, Decimal] = {
    RoundingRule.EXACT: Decimal("0.5"),
    RoundingRule.NEAREST_PAISA: Decimal("0.5"),
    RoundingRule.NEAREST_RUPEE: Decimal(50),
}
"""Half the rounding step — the largest residual rounding alone can explain.

`EXACT` still gets half a paisa rather than zero: a broker that rounds nothing still bills in
whole paise somewhere downstream, and demanding a residual of literally zero would report
drift on arithmetic noise forever.
"""


_OBSERVATIONS_NEEDED_FOR_A_DISPERSION = 2
"""One observation has no spread, so no interval can be put around its mean."""


class ReconciliationVerdict(StrEnum):
    """What the accrued residuals support saying."""

    UNVERIFIED = "unverified"
    AGREES = "agrees"
    DRIFTS = "drifts"


@dataclass(frozen=True, slots=True)
class ChargeObservation:
    """One component of one leg of one real order, modelled against billed."""

    broker: str
    order_reference: str
    leg: TradeLeg
    component: ChargeComponent
    segment: ChargeableSegment
    trade_date: date
    modelled_paise: Decimal
    actual_paise: Decimal
    recorded_at: date

    @property
    def residual_paise(self) -> Decimal:
        """Modelled minus actual. Positive means the engine OVERSTATES the charge."""
        return self.modelled_paise - self.actual_paise


@dataclass(frozen=True, slots=True)
class ComponentReconciliation:
    """What the ledger can say about one component, and how sure it is."""

    broker: str
    segment: ChargeableSegment
    component: ChargeComponent
    observation_count: int
    mean_residual_paise: Decimal
    residual_dispersion_paise: Decimal
    worst_residual_paise: Decimal
    worst_order_reference: str
    confidence_half_width_paise: Decimal
    explainable_by_rounding_paise: Decimal

    @property
    def verdict(self) -> ReconciliationVerdict:
        """Where the confidence interval sits relative to the band rounding can explain.

        Three positions, three verdicts, and the middle one is the honest majority case:

        - Entirely INSIDE the band: the mean residual is rounding, whatever else it might
          also be. `AGREES`.
        - Entirely OUTSIDE it: no amount of rounding accounts for this. `DRIFTS`.
        - Straddling: the data does not yet separate the two. `UNVERIFIED`.

        Testing the interval's POSITION rather than its width is what makes a large drift
        visible while it is still noisy. An earlier version required the interval to be
        narrower than the noise floor before saying anything, which reported a 137-paise mean
        offset as "not enough data" — a defect that hid the exact signal the ledger exists to
        raise. Either way there is no fixed observation count anywhere: `n` enters only
        through the standard error.
        """
        if self.observation_count < _OBSERVATIONS_NEEDED_FOR_A_DISPERSION:
            return ReconciliationVerdict.UNVERIFIED
        magnitude = abs(self.mean_residual_paise)
        if magnitude + self.confidence_half_width_paise <= self.explainable_by_rounding_paise:
            return ReconciliationVerdict.AGREES
        if magnitude - self.confidence_half_width_paise > self.explainable_by_rounding_paise:
            return ReconciliationVerdict.DRIFTS
        return ReconciliationVerdict.UNVERIFIED

    @property
    def explanation(self) -> str:
        match self.verdict:
            case ReconciliationVerdict.UNVERIFIED:
                return (
                    f"{self.observation_count} observations: the mean residual is known only "
                    f"to +/-{self.confidence_half_width_paise:.3f} paise, wider than the "
                    f"{self.explainable_by_rounding_paise} paise rounding can explain"
                )
            case ReconciliationVerdict.AGREES:
                return (
                    f"{self.observation_count} observations, mean residual "
                    f"{self.mean_residual_paise:.3f} paise — within rounding"
                )
            case _:
                return (
                    f"{self.observation_count} observations, mean residual "
                    f"{self.mean_residual_paise:.3f} paise exceeds the "
                    f"{self.explainable_by_rounding_paise} paise rounding floor; worst "
                    f"{self.worst_residual_paise:.3f} on order {self.worst_order_reference}. "
                    f"This is evidence the RATE is wrong, not a factor to divide out"
                )


def _sample_standard_deviation(values: Sequence[Decimal], mean: Decimal) -> Decimal:
    """Exact sample standard deviation, Bessel-corrected, without touching `float`."""
    if len(values) < _OBSERVATIONS_NEEDED_FOR_A_DISPERSION:
        return Decimal(0)
    squared = sum(((value - mean) ** 2 for value in values), Decimal(0))
    return (squared / (len(values) - 1)).sqrt()


class ChargeReconciliationLedger:
    """Accrues modelled-vs-billed residuals and reports what they support."""

    def __init__(
        self,
        database_path: Path = DEFAULT_LEDGER_PATH,
        *,
        confidence: float = 0.95,
        rounding_by_component: Mapping[ChargeComponent, RoundingRule] | None = None,
    ) -> None:
        """`rounding_by_component` says what each component's billing granularity IS.

        Held on the ledger rather than passed per call because it is a property of the broker
        being reconciled against, and because the alternative was measured to fail: with the
        map available only as a `reconcile()` argument, `drifting_components()` could not
        supply it and defaulted to a half-paisa band for every component — so for the default
        broker, which rounds STT to the whole rupee, it reported pure rounding as evidence
        that a statutory RATE was wrong. Two callers of the same ledger disagreed.
        """
        self._path = database_path
        self._confidence = confidence
        self._rounding_by_component: Mapping[ChargeComponent, RoundingRule] = (
            rounding_by_component or {}
        )
        if not 0 < confidence < 1:
            raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    @property
    def critical_value(self) -> Decimal:
        """The normal quantile for the chosen confidence, computed rather than written down."""
        return Decimal(str(NormalDist().inv_cdf(1 - (1 - self._confidence) / 2)))

    def record(self, observations: Iterable[ChargeObservation]) -> int:
        """Store observations idempotently. Re-recording the same order changes nothing."""
        rows = [
            (
                observation.broker,
                observation.order_reference,
                observation.leg.value,
                observation.component.value,
                observation.segment.value,
                observation.trade_date.isoformat(),
                str(observation.modelled_paise),
                str(observation.actual_paise),
                observation.recorded_at.isoformat(),
            )
            for observation in observations
        ]
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            cursor = connection.executemany(
                "INSERT OR REPLACE INTO charge_observation (broker, order_reference, leg, "
                "component, segment, trade_date, modelled_paise, actual_paise, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
            return cursor.rowcount

    def observation_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM charge_observation").fetchone()
        return int(row[0])

    def reconcile(
        self, *, rounding_by_component: Mapping[ChargeComponent, RoundingRule] | None = None
    ) -> tuple[ComponentReconciliation, ...]:
        """One verdict per (broker, segment, component) with observations behind it.

        The per-call map overrides the ledger's own, for asking what the same residuals would
        say under a different broker's rounding.
        """
        rounding = dict(self._rounding_by_component) | dict(rounding_by_component or {})
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT broker, segment, component, order_reference, modelled_paise, "
                "actual_paise FROM charge_observation"
            ).fetchall()
        grouped: dict[tuple[str, str, str], list[tuple[str, Decimal]]] = {}
        for broker, segment, component, order_reference, modelled, actual in rows:
            residual = Decimal(modelled) - Decimal(actual)
            grouped.setdefault((broker, segment, component), []).append((order_reference, residual))
        results = [
            self._reconciliation_for(key, entries, rounding)
            for key, entries in sorted(grouped.items())
        ]
        return tuple(results)

    def _confidence_half_width(
        self, count: int, dispersion: Decimal, key: tuple[str, str, str]
    ) -> Decimal:
        """The half-width of the interval around the mean residual.

        With fewer than two observations there is no interval at all, so the answer is
        infinite and every verdict is `UNVERIFIED`.

        With a dispersion of exactly zero the naive interval is zero wide, which would let two
        identical observations DECIDE that a component drifts. That case is not rare — an
        algorithm trading the same size repeatedly produces identical residuals by
        construction — and a zero-width interval from two samples is a statement the data
        cannot support. So the dispersion is floored at the rounding granularity, which is the
        smallest spread the billing process itself can produce, and the interval shrinks from
        there as observations accrue.
        """
        if count < _OBSERVATIONS_NEEDED_FOR_A_DISPERSION:
            return Decimal("Infinity")
        floor = _ROUNDING_GRANULARITY_PAISE[self._rounding_for(key)]
        return self.critical_value * max(dispersion, floor) / Decimal(count).sqrt()

    def _rounding_for(self, key: tuple[str, str, str]) -> RoundingRule:
        return self._rounding_by_component.get(ChargeComponent(key[2]), RoundingRule.NEAREST_PAISA)

    def _reconciliation_for(
        self,
        key: tuple[str, str, str],
        entries: Sequence[tuple[str, Decimal]],
        rounding: Mapping[ChargeComponent, RoundingRule],
    ) -> ComponentReconciliation:
        broker, segment_value, component_value = key
        component = ChargeComponent(component_value)
        residuals = [residual for _, residual in entries]
        count = len(residuals)
        # Exact throughout. Routing these through `float` cost the last bits of a value the
        # verdict then compares with `<=` against a band — measured: residuals of 0.1, 0.2
        # and 0.3 gave a mean of 0.19999999999999998 rather than 0.2, which can flip
        # AGREES/DRIFTS on a boundary case.
        mean = sum(residuals, Decimal(0)) / count
        dispersion = _sample_standard_deviation(residuals, mean)
        half_width = self._confidence_half_width(count, dispersion, key)
        worst_order, worst_residual = max(entries, key=lambda entry: abs(entry[1]))
        granularity = _ROUNDING_GRANULARITY_PAISE[
            rounding.get(component, RoundingRule.NEAREST_PAISA)
        ]
        return ComponentReconciliation(
            broker=broker,
            segment=ChargeableSegment(segment_value),
            component=component,
            observation_count=count,
            mean_residual_paise=mean,
            residual_dispersion_paise=dispersion,
            worst_residual_paise=worst_residual,
            worst_order_reference=worst_order,
            confidence_half_width_paise=half_width,
            explainable_by_rounding_paise=granularity,
        )

    def drifting_components(
        self, *, rounding_by_component: Mapping[ChargeComponent, RoundingRule] | None = None
    ) -> tuple[ComponentReconciliation, ...]:
        """Only the ones whose evidence says a RATE is wrong — the actionable subset."""
        return tuple(
            reconciliation
            for reconciliation in self.reconcile(rounding_by_component=rounding_by_component)
            if reconciliation.verdict is ReconciliationVerdict.DRIFTS
        )
