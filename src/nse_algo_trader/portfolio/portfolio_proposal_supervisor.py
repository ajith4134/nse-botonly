"""Allocate one paper book across the six segment bots (`L3.05`, `L7.02`, `B39`, `A.146`).

**The measured gap this closes.** On 2026-07-31 the per-bot net-directional rule was working exactly
as written and the PORTFOLIO still ended at **43.1%** directional against a 25% bound:
`index_futures` sat at **100%** — a book of one, which no per-bot rule can balance — and
`stock_futures` at 0.0%, and nothing aggregated across the six. `B39` recorded it as belonging to
`L3.05` (the pre-trade risk gate) and `L7.02` (position and leverage limits); this is that level.

**The second failure it is built around.** `docs/research/261` measured a repair that over-corrected
in exactly one shape: a net-directional bound stated as a fraction of GROSS cannot be met by the
FIRST position, so it refused 23 of 23 real proposals while looking like working caution. The bound
here is on the **direction of travel** — a trade that reduces the book's net exposure is admitted
whatever the current net is, so the book always converges and the first position is never refused.

**The solver, and why it is `cvxpy` rather than a portfolio library** (`R.17`, sourcing run
2026-08-19). `cvxpy` 1.9.2 is already installed here with five working backends
(`CLARABEL`, `SCS`, `SCIPY`, `HIGHS`, `OSQP`) and no new dependency. `riskfolio-lib` 7.x,
`PyPortfolioOpt`, `cvxportfolio` and `skfolio` were each checked MECHANICALLY, not on their README:
all four take `returns` / `expected_returns` + `cov_matrix` as constructor arguments, because they
model an asset-allocation problem over a return time series. This is not that problem — the inputs
are discrete per-tick order proposals with a linear budget and an asymmetric linear exposure
constraint — and using them would mean fabricating a covariance matrix the problem never produces.
Three of the four also force `pandas` 2.3.3 to 3.0.5, a major bump, plus transitive `vectorbt` and
`astropy`. Rejections surfaced to the operator rather than buried.

**The programme.** One continuous `x_i` in `[0, 1]` per proposal — the fraction of it admitted:

    maximise    Σ value_i · x_i
    subject to  Σ capital_i · x_i               ≤ deployable
                Σ capital_i · x_i  (per bot)    ≤ deployable / bots_proposing
                |book_net + Σ signed_i · x_i|   ≤ deployable · net_directional_fraction
                0 ≤ x_i ≤ 1

`value_i` is the proposal's own expected edge in basis points times the capital it consumes — the
rupees it claims it will make — scaled by conviction where the bot stated one. The per-bot row is
`R.10`'s "the six segments equal by default" made mechanical: without it one bot with a wide
universe takes the whole book from five bots with narrow ones, which is not a preference the
allocator should be free to express.

**Whole lots, after the solve, not inside it.** An integer programme over thousands of proposals is
the wrong shape for a five-minute tick; the continuous solution is rounded DOWN to whole lots, which
can only ever relax a constraint the solver satisfied. A proposal that cannot afford one whole lot
is REFUSED with that reason rather than admitted at a fractional size that no exchange would accept.

Spec: `docs/research/267_continuous_six_segment_paper_trading_spec.md`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import cvxpy
import numpy

from nse_algo_trader.cost_gate.priced_signal import PricedSignal
from nse_algo_trader.transaction_cost.chargeable_market_segments import TradeLeg

BASIS_POINTS_IN_ONE: Final = Decimal("10000")

INTERIOR_POINT_RESIDUAL: Final = 1e-6
"""How close to 1.0 an interior-point solution counts as 1.0 when it is turned into a quantity.

Not a market number and not a preference — it is the solver's own convergence residual. `CLARABEL`
returns `x = 0.9999999...` for a proposal it admitted whole, and truncating that gives 49 units of a
50-unit proposal: a position silently one unit smaller than the solver decided, on every trade.
"""

SOLVER_PREFERENCE: Final = ("CLARABEL", "HIGHS", "SCS")
"""Tried in order. `CLARABEL` is the interior-point default and is ARM64-native here; `HIGHS` is a
simplex fallback that answers the same LP; `SCS` is first-order and answers when both refuse."""


class PortfolioSupervisionError(Exception):
    """The supervisor was configured with a book it cannot allocate."""


@dataclass(frozen=True, slots=True)
class BotProposal:
    """One bot's intent, plus what it would actually consume.

    `margin_rupees` is the difference between the futures bots trading and not trading. Bounding by
    NOTIONAL made a single stock-futures lot consume half a Rs 10,00,000 book, so 23 of 23 real
    proposals were refused on 2026-07-31; futures consume MARGIN, roughly a tenth of that. `None`
    means notional — honest rather than convenient, because a capital bound that silently guessed a
    leverage multiple is the invented number `R.03` forbids.
    """

    bot_identity: str
    signal: PricedSignal
    margin_rupees: Decimal | None = None
    lot_size: int = 1

    @property
    def notional_rupees(self) -> Decimal:
        return (
            self.signal.reference_price_paise * Decimal(self.signal.proposed_quantity)
        ) / Decimal(100)

    @property
    def capital_rupees(self) -> Decimal:
        """What this proposal takes out of the book if admitted whole."""
        return self.margin_rupees if self.margin_rupees is not None else self.notional_rupees

    @property
    def signed_notional_rupees(self) -> Decimal:
        """Positive for a long, negative for a short — the book's direction of travel."""
        sign = Decimal(1) if self.signal.side is TradeLeg.BUY else Decimal(-1)
        return sign * self.notional_rupees

    @property
    def claimed_rupees(self) -> Decimal:
        """What the proposal says it will earn, scaled by the conviction the bot stated.

        Edge in basis points times the capital at risk. A proposal with no stated conviction is
        taken at its edge alone rather than at a default conviction — a stand-in number here would
        rank one bot's proposals against another's on something neither bot said.
        """
        edge = self.signal.expected_edge_bps / BASIS_POINTS_IN_ONE * self.capital_rupees
        conviction = self.signal.conviction
        return edge if conviction is None else edge * conviction


@dataclass(frozen=True, slots=True)
class AdmittedProposal:
    """A proposal as the supervisor admits it — possibly at a smaller whole-lot quantity."""

    bot_identity: str
    signal: PricedSignal
    quantity: int
    capital_rupees: Decimal
    signed_notional_rupees: Decimal

    @property
    def was_trimmed(self) -> bool:
        return self.quantity < self.signal.proposed_quantity


@dataclass(frozen=True)
class PortfolioPlan:
    """What the book will do this tick, and what it refused and why."""

    admitted: tuple[AdmittedProposal, ...]
    refusals_by_reason: Mapping[str, int]
    book_net_before_rupees: Decimal
    net_after_rupees: Decimal
    gross_admitted_rupees: Decimal
    deployed_by_identity: Mapping[str, Decimal]
    solver: str | None = None
    proposals_seen: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def admitted_count(self) -> int:
        return len(self.admitted)

    @property
    def refused_count(self) -> int:
        return sum(self.refusals_by_reason.values())

    @property
    def admitted_by_identity(self) -> dict[str, tuple[AdmittedProposal, ...]]:
        grouped: dict[str, list[AdmittedProposal]] = {}
        for admitted in self.admitted:
            grouped.setdefault(admitted.bot_identity, []).append(admitted)
        return {identity: tuple(items) for identity, items in grouped.items()}

    def describe(self) -> str:
        if not self.proposals_seen:
            return "nothing proposed"
        per_bot = " · ".join(
            f"{identity} {len(items)}"
            for identity, items in sorted(self.admitted_by_identity.items())
        )
        line = (
            f"{self.admitted_count} of {self.proposals_seen} admitted [{per_bot or 'none'}] · "
            f"gross Rs {self.gross_admitted_rupees:,.0f} · net "
            f"Rs {self.book_net_before_rupees:,.0f} -> Rs {self.net_after_rupees:,.0f}"
        )
        if self.refusals_by_reason:
            worst = ", ".join(
                f"{reason} x{count}"
                for reason, count in sorted(
                    self.refusals_by_reason.items(), key=lambda item: -item[1]
                )[:3]
            )
            line += f" · refused {self.refused_count} ({worst})"
        if self.solver:
            line += f" · {self.solver}"
        return line


class PortfolioProposalSupervisor:
    """Splits one book across every bot that proposed, under a portfolio-wide directional bound."""

    def __init__(
        self,
        *,
        deployable_rupees: Decimal,
        net_directional_fraction: Decimal,
    ) -> None:
        if deployable_rupees <= 0:
            raise PortfolioSupervisionError(
                f"deployable capital must be positive, got {deployable_rupees}; a book with "
                f"nothing in it cannot be allocated and reporting an empty plan would read as "
                f"caution rather than as misconfiguration"
            )
        if not 0 < net_directional_fraction <= 1:
            raise PortfolioSupervisionError(
                f"net_directional_fraction must be in (0, 1], got {net_directional_fraction}"
            )
        self._deployable = deployable_rupees
        self._net_fraction = net_directional_fraction

    @property
    def deployable_rupees(self) -> Decimal:
        return self._deployable

    def supervise(
        self, proposals: Sequence[BotProposal], *, book_net_rupees: Decimal
    ) -> PortfolioPlan:
        """Admit what the book can carry, bounded on the direction of travel."""
        if not proposals:
            return PortfolioPlan(
                admitted=(),
                refusals_by_reason={},
                book_net_before_rupees=book_net_rupees,
                net_after_rupees=book_net_rupees,
                gross_admitted_rupees=Decimal("0"),
                deployed_by_identity={},
                proposals_seen=0,
            )

        refusals: dict[str, int] = {}
        priced = [
            proposal
            for proposal in proposals
            if self._is_priceable(proposal, refusals)
        ]
        if not priced:
            return PortfolioPlan(
                admitted=(),
                refusals_by_reason=refusals,
                book_net_before_rupees=book_net_rupees,
                net_after_rupees=book_net_rupees,
                gross_admitted_rupees=Decimal("0"),
                deployed_by_identity={},
                proposals_seen=len(proposals),
            )

        fractions, solver, notes = self._solve(priced, book_net_rupees)
        admitted, deployed = self._round_to_whole_lots(priced, fractions, refusals)

        net_after = book_net_rupees + sum(
            (item.signed_notional_rupees for item in admitted), Decimal("0")
        )
        gross = sum((item.capital_rupees for item in admitted), Decimal("0"))
        return PortfolioPlan(
            admitted=tuple(admitted),
            refusals_by_reason=refusals,
            book_net_before_rupees=book_net_rupees,
            net_after_rupees=net_after,
            gross_admitted_rupees=gross,
            deployed_by_identity=deployed,
            solver=solver,
            proposals_seen=len(proposals),
            notes=notes,
        )

    # -- the programme --------------------------------------------------------------------

    def _solve(
        self, proposals: Sequence[BotProposal], book_net_rupees: Decimal
    ) -> tuple[list[float], str | None, tuple[str, ...]]:
        capital = numpy.array([float(p.capital_rupees) for p in proposals])
        signed = numpy.array([float(p.signed_notional_rupees) for p in proposals])
        value = numpy.array([float(p.claimed_rupees) for p in proposals])
        identities = [p.bot_identity for p in proposals]

        fraction = cvxpy.Variable(len(proposals), name="admitted_fraction")
        budget = float(self._deployable)
        bound = float(self._deployable * self._net_fraction)
        net_before = float(book_net_rupees)

        bots_proposing = len(set(identities))
        per_bot_budget = budget / bots_proposing

        constraints = [fraction >= 0, fraction <= 1, capital @ fraction <= budget]
        for identity in sorted(set(identities)):
            mask = numpy.array([1.0 if name == identity else 0.0 for name in identities])
            constraints.append((capital * mask) @ fraction <= per_bot_budget)

        # The direction-of-travel bound. Both halves are stated so the book is squeezed toward zero
        # from whichever side it is on, and a converging trade is never the thing that is refused.
        net_after = net_before + signed @ fraction
        constraints.append(net_after <= max(bound, net_before))
        constraints.append(net_after >= min(-bound, net_before))

        problem = cvxpy.Problem(cvxpy.Maximize(value @ fraction), constraints)
        notes: list[str] = []
        for solver in SOLVER_PREFERENCE:
            try:
                problem.solve(solver=solver)  # type: ignore[no-untyped-call]
            except Exception as failure:  # noqa: BLE001 — try the next backend, then refuse all
                notes.append(f"{solver} raised {type(failure).__name__}")
                continue
            if fraction.value is not None and problem.status in (
                cvxpy.OPTIMAL,
                cvxpy.OPTIMAL_INACCURATE,
            ):
                if problem.status == cvxpy.OPTIMAL_INACCURATE:
                    notes.append(f"{solver} returned an inaccurate optimum")
                return [float(v) for v in fraction.value], solver, tuple(notes)
            notes.append(f"{solver} returned {problem.status}")
        # Nothing solved. Admitting everything would be the opposite of what this class is for.
        return [0.0] * len(proposals), None, tuple(notes)

    def _round_to_whole_lots(
        self,
        proposals: Sequence[BotProposal],
        fractions: Sequence[float],
        refusals: dict[str, int],
    ) -> tuple[list[AdmittedProposal], dict[str, Decimal]]:
        """Round DOWN to whole lots. Rounding down can only relax a constraint already satisfied."""
        admitted: list[AdmittedProposal] = []
        deployed: dict[str, Decimal] = {}
        for proposal, share in zip(proposals, fractions, strict=True):
            lot = max(int(proposal.lot_size), 1)
            proposed = int(proposal.signal.proposed_quantity)
            wanted = math.floor(
                max(0.0, min(1.0, share)) * proposed + INTERIOR_POINT_RESIDUAL
            )
            quantity = (wanted // lot) * lot
            if quantity <= 0:
                reason = (
                    "no whole lot fits the allocated capital"
                    if wanted > 0
                    else "the portfolio bound left no room"
                )
                refusals[reason] = refusals.get(reason, 0) + 1
                continue
            scale = Decimal(quantity) / Decimal(proposed)
            admitted.append(
                AdmittedProposal(
                    bot_identity=proposal.bot_identity,
                    signal=proposal.signal,
                    quantity=quantity,
                    capital_rupees=proposal.capital_rupees * scale,
                    signed_notional_rupees=proposal.signed_notional_rupees * scale,
                )
            )
            deployed[proposal.bot_identity] = deployed.get(
                proposal.bot_identity, Decimal("0")
            ) + proposal.capital_rupees * scale
        return admitted, deployed

    @staticmethod
    def _is_priceable(proposal: BotProposal, refusals: dict[str, int]) -> bool:
        if proposal.signal.proposed_quantity <= 0:
            refusals["a proposal of zero quantity"] = (
                refusals.get("a proposal of zero quantity", 0) + 1
            )
            return False
        if proposal.capital_rupees <= 0:
            refusals["a proposal consuming no capital"] = (
                refusals.get("a proposal consuming no capital", 0) + 1
            )
            return False
        return True
