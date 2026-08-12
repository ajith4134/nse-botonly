"""`L1.01` surface — what a segment costs to trade, and how much the number is worth.

Built against the dataviz procedure, and it lands on THREE forms because the page asks three
different kinds of question:

- **Four stat tiles.** "What is the cheapest round trip available anywhere", "what is the
  worst-sourced rate holding this page up", "how many contract notes has any of it been
  checked against", "how many components are drifting" are single scalars. A scalar has no
  shape, so plotting it invents one; the number IS the visualisation.
- **One worst-first table, per segment.** "Can I afford to trade NFO-OPT" is answered by
  finding one row and reading across it. Eight segments as bars would make that lookup harder,
  not easier — the same argument the `L0.31` coverage surface already makes.
- **One genuine chart per segment, and only here.** Cost in basis points as a function of
  quantity is a STEP FUNCTION over an ordered numeric domain, and the reader's question about
  it is "where does it flatten out" — a question about *shape*, which a column of twenty
  numbers answers only by making the reader difference them in their head. That is the one
  question on this page a chart answers better than a table, so it is the one chart. It is
  inline SVG because a strict CSP forbids external libraries, single-series (so no legend —
  the heading names it), and every chart ships a caption stating its first and last value plus
  a `<details>` table, so nothing here is readable only by eye.

**Colour does exactly one job: status.** Evidence grades and reconciliation verdicts are
states, not series, so they use the reserved status palette and never the categorical set.
Every badge carries a WORD as well as a colour, because a status shown in colour alone fails
for a colourblind reader and in a printout. The single chart series is categorical slot 1
(blue), validated against both surfaces.

Grades collapse onto three status steps rather than four, deliberately: an exchange-observed
fact and a primary circular are both *citable*, so they share the good step and are told apart
by their word; a triangulated secondary and an unverified snippet are not citable, and the
difference between those two — whether anything was checked at all — is worth a colour.

**Worst first, and refusals above everything.** A segment the engine REFUSES to price is worse
news than an expensive one, so it sorts to the top of the table rather than being dropped from
it. The refused-era panel exists for the same reason: the engine cannot price exchange charges
before the flat-rate era began, nor stamp duty before it was federalised, and a page that
showed only what it *can* price would read as full coverage. An absence that is invisible
reads as coverage.

**`F01` added a second question — what the cost DECIDES — and it needs four more forms.**
`L1.02`'s gate, `L1.04`'s per-segment floors and `L11.99/106/107/108`'s ticket preconditions
turn a cost into a verdict, and each of them asks the reader something the forms above cannot
answer:

- **A stacked bar per size, grouped into ladders.** The required hurdle is a SUM — statutory
  charges, plus the expected execution cost, plus a margin — and the question is what share each
  part contributes, which is part-to-whole. The margin is the one that matters: the gate carries
  no safety multiplier, so the gap between the expected cost and the hurdle IS the measured
  uncertainty of the execution estimate, and a reader has to be able to see it widen. That is a
  question about SHAPE across sizes, so the sizes of one instrument are drawn ascending as a
  ladder on ONE axis shared by every ladder on the page — a per-card axis would let two cards
  with different scales look alike. Between ladders the worst-first rule still holds; inside one
  it cannot, because rank ordering would destroy the very trend the ladder exists to show.
- **A range strip per segment for the `L1.04` floors**, on a LOG axis, because hurdles measured
  across the real universe run from single-digit basis points to several hundred and a linear
  axis crushes the cheap end — which is precisely the end a floor lives at. The floor carries a
  ±1 standard-error band derived from how many instruments actually stand behind the quantile,
  so a thinly-measured floor LOOKS less certain by its geometry. That is `R.04` made visible,
  and it is deliberately geometry rather than colour: on this page colour means status, and the
  width of the band is the finding itself.
- **Four verdict counts as tiles, and the fourth one is kept out of the row.** PASS, RESIZE and
  VETO are judgements and wear the status palette. UNPRICEABLE is the ABSENCE of a judgement, so
  it wears no status colour at all, sits outside the row of judgements in its own panel, and is
  never counted into a rejection total. A veto says "not worth taking"; unpriceable says "I do
  not know", and a page that painted them the same red would teach the reader to read an outage
  as a decision.
- **One bar chart of precondition failures by name.** Four named checks ranked by how often each
  killed a ticket is a magnitude comparison over few classes, which bars answer at a glance and
  a table answers only by making the reader scan a column. Checks that never fired are drawn at
  zero rather than dropped, because a missing bar reads as "not a problem" and an absent check
  is a different statement from a satisfied one.

**The one place categorical colour appears.** The hurdle stack is identity, not status — its
three parts are components of a sum — so it uses categorical slots 1-3 (blue, orange, aqua),
which pass every all-pairs gate in both modes (`scripts/validate_palette.js`, worst CVD ΔE 9.2
light / 9.4 dark, worst normal-vision ΔE 24.0 light / 20.9 dark). Light-mode aqua sits at 2.74:1
against the light surface, below 3:1, so the relief rule applies and is honoured: every stack
ships a legend, a direct label at the bar tip and a `<details>` table of the same numbers. Every
other colour on the page is still status, still paired with a word.

Pure renderer: `render_transaction_cost_page` takes a frozen state and returns HTML. It opens
no database, constructs no engine and prices nothing. `build_transaction_cost_surface_state`
does all of that, once, so the page cannot accidentally become a trading-cost calculator that
runs on every refresh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from html import escape
from itertools import pairwise

from nse_algo_trader.cost_gate.per_segment_edge_floor import SegmentEdgeFloor
from nse_algo_trader.cost_gate.pre_trade_cost_gate import GateDecision, GateVerdict
from nse_algo_trader.cost_gate.tradeable_ticket_preconditions import PreconditionName
from nse_algo_trader.market_rules.point_in_time_market_rule_store import (
    EvidenceGrade,
    MarketRuleRecord,
    PointInTimeMarketRuleStore,
    RuleFamily,
    evidence_grade_rank,
)
from nse_algo_trader.transaction_cost.breakeven_move_solver import solve_breakeven_move
from nse_algo_trader.transaction_cost.charge_reconciliation_ledger import (
    ChargeReconciliationLedger,
    ReconciliationVerdict,
)
from nse_algo_trader.transaction_cost.charge_structure_history import (
    ChargeStructureHistory,
    ChargeStructureRecord,
    nse_charge_structure_history,
)
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    ChargeComponent,
    RoundingRule,
)
from nse_algo_trader.transaction_cost.nse_transaction_cost_engine import (
    NseTransactionCostEngine,
    OptionRight,
    TradeSpecification,
    TransactionCostError,
)
from nse_algo_trader.transaction_cost.quantity_cost_economics import (
    cost_curve,
    minimum_viable_quantity,
)

_STATUS_CRITICAL = "#c0392b"
_STATUS_WARNING = "#fab219"
_STATUS_GOOD = "#0ca30c"
"""The reserved status palette, never borrowed for a series. Each is paired with a word."""

_SERIES_LIGHT = "#2a78d6"
_SERIES_DARK = "#3987e5"
"""Categorical slot 1 — the staircase line, the precondition bars, the floor strips.

Both steps clear the validator's lightness band, chroma floor and 3:1 contrast against their
own surface (`scripts/validate_palette.js`, light `#fcfcfb` / dark `#1a1a19`).
"""

_SERIES_TWO_LIGHT = "#eb6834"
_SERIES_TWO_DARK = "#d95926"
_SERIES_THREE_LIGHT = "#1baf7a"
_SERIES_THREE_DARK = "#199e70"
"""Categorical slots 2 and 3 — used ONLY for the two other parts of the hurdle stack.

Slots 1-3 are the set the validator clears on the all-pairs list in both modes, which is the
list that applies here because the three parts of a stack are compared against each other rather
than only against their neighbours. Light-mode slot 3 is 2.74:1 against the light surface, below
the 3:1 bar, so the stack carries the relief the validator demands: a legend, a direct label at
each bar tip, and the whole thing again as a table.
"""

_TRANSACTION_TAX_COMPONENTS = frozenset(
    {
        ChargeComponent.SECURITIES_TRANSACTION_TAX,
        ChargeComponent.COMMODITIES_TRANSACTION_TAX,
    }
)
"""Which components follow the broker's coarse statutory rounding rule.

Mirrors the engine's own set. It is restated rather than imported because the ledger needs it
to decide how much residual rounding alone can explain, and a component reconciled against the
wrong granularity reports drift on noise — or, worse, agreement on a real error.
"""

_GRADE_BADGES: dict[EvidenceGrade, tuple[str, str]] = {
    EvidenceGrade.OBSERVED_FROM_EXCHANGE_DATA: ("badge-good", "observed"),
    EvidenceGrade.PRIMARY_CIRCULAR: ("badge-good", "primary circular"),
    EvidenceGrade.SECONDARY_TRIANGULATED: ("badge-warning", "secondary"),
    EvidenceGrade.UNVERIFIED_SNIPPET: ("badge-critical", "unverified"),
}

_VERDICT_BADGES: dict[ReconciliationVerdict, tuple[str, str]] = {
    ReconciliationVerdict.UNVERIFIED: ("badge-warning", "UNVERIFIED"),
    ReconciliationVerdict.AGREES: ("badge-good", "AGREES"),
    ReconciliationVerdict.DRIFTS: ("badge-critical", "DRIFTS"),
}

_GATE_VERDICT_BADGES: dict[GateVerdict, tuple[str, str]] = {
    GateVerdict.PASS: ("badge-good", "PASS"),
    GateVerdict.RESIZE: ("badge-warning", "RESIZE"),
    GateVerdict.VETO: ("badge-critical", "VETO"),
    GateVerdict.UNPRICEABLE: ("badge-absent", "UNPRICEABLE"),
}
"""Three judgements in the status palette, and one deliberate hole in it.

`UNPRICEABLE` gets `badge-absent` — no fill, a dashed outline, muted ink — because it is not a
rejection and must never be read as one. The absence of colour is doing the same work the colours
are: there is no judgement here to paint.
"""

_LEGS_PER_ROUND_TRIP = Decimal(2)
"""Entering and leaving. The gate charges execution cost on both legs, so the page shows both.

Restated here rather than imported for the same reason `_TRANSACTION_TAX_COMPONENTS` is: a figure
printed beside a hurdle has to stand on the hurdle's own footing. A one-leg spread shown next to
a round-trip hurdle would read as half the cost the gate actually charged.
"""

_PERCENT = Decimal(100)
_MEDIAN_QUANTILE = Decimal("0.5")
"""The quantile `derive_segment_floor` itself uses for the median it publishes."""

_FLOOR_RANK_OF_A_LONE_OBSERVATION = 1
"""Below this the quantile is not a quantile: the floor IS one instrument, not a property."""

_MINIMUM_ROWS_FOR_A_SIZE_LADDER = 2
"""One size cannot show a trend, so a single-size ladder reports no widening rather than zero."""

_PAGE_CSS = """
:root{
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --border:#e2e1dc;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
  --series-1:__SERIES_LIGHT__; --series-2:__SERIES_TWO_LIGHT__;
  --series-3:__SERIES_THREE_LIGHT__; --gridline:#e1e0d9; --axis:#c3c2b7;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
    --series-1:__SERIES_DARK__; --series-2:__SERIES_TWO_DARK__;
    --series-3:__SERIES_THREE_DARK__; --gridline:#2c2c2a; --axis:#383835;
  }
}
[data-theme="dark"]{
  --surface-0:#111110; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a8a80;
  --series-1:__SERIES_DARK__; --series-2:__SERIES_TWO_DARK__;
  --series-3:__SERIES_THREE_DARK__; --gridline:#2c2c2a; --axis:#383835;
}
*{box-sizing:border-box;}
body{margin:0;padding:32px;background:var(--surface-0);color:var(--text-primary);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;}
h1{font-size:20px;margin:0 0 4px;}
h2{font-size:14px;margin:26px 0 10px;}
.sub{color:var(--text-secondary);margin:0 0 24px;max-width:88ch;}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:26px;}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:170px;flex:1 1 170px;}
.tile-value{font-size:24px;font-weight:700;}
.tile-label{color:var(--text-secondary);margin-top:2px;}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:4px 18px 18px;overflow-x:auto;}
.note{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;overflow-x:auto;}
table{border-collapse:collapse;width:100%;}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);
  white-space:nowrap;}
th{color:var(--text-secondary);font-weight:600;}
td.figure,th.figure{text-align:right;font-variant-numeric:tabular-nums;}
td.reason{white-space:normal;color:var(--text-secondary);}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
  color:#ffffff;}
.badge-critical{background:__CRITICAL__;}
.badge-warning{background:__WARNING__;color:#0b0b0b;}
.badge-good{background:__GOOD__;}
.muted{color:var(--text-muted);}
/* min() so a 440px column collapses on a narrow screen instead of forcing the BODY to
   scroll sideways — wide content scrolls inside its own box, never the page. */
.charts{display:grid;gap:14px;
  grid-template-columns:repeat(auto-fit,minmax(min(440px,100%),1fr));}
.chart{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:12px 16px 14px;overflow-x:auto;}
.chart h3{font-size:13px;margin:0 0 6px;}
figure{margin:0;}
figcaption{color:var(--text-secondary);font-size:12px;margin-top:6px;}
svg{display:block;width:100%;height:auto;}
.tick{fill:var(--text-muted);font-size:11px;}
.gridline{stroke:var(--gridline);stroke-width:1;}
.axis-rule{stroke:var(--axis);stroke-width:1;}
.step{fill:none;stroke:var(--series-1);stroke-width:2;stroke-linejoin:round;
  stroke-linecap:round;}
.tread{fill:var(--series-1);stroke:var(--surface-1);stroke-width:2;}
.tread-label{fill:var(--text-secondary);font-size:11px;
  font-variant-numeric:tabular-nums;}
.hit{fill:transparent;stroke:none;}
details{margin-top:8px;}
summary{cursor:pointer;color:var(--text-secondary);font-size:12px;}
details table{margin-top:6px;}
details th,details td{font-size:12px;padding:3px 8px;}
footer{color:var(--text-muted);font-size:12px;margin-top:26px;max-width:88ch;}
/* ---- F01: the gate ---- */
/* No fill and a dashed edge: UNPRICEABLE is the absence of a judgement, and painting it in
   any status colour would file it under one. */
.badge-absent{background:transparent;color:var(--text-muted);
  border:1px dashed var(--axis);padding:0 7px;}
.tile-absent{border-style:dashed;}
.tile-absent .tile-value{color:var(--text-muted);}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:0 0 10px;
  color:var(--text-secondary);font-size:12px;}
.legend span{display:inline-flex;align-items:center;gap:6px;}
.swatch{width:12px;height:12px;border-radius:3px;display:inline-block;}
.swatch-1{background:var(--series-1);}
.swatch-2{background:var(--series-2);}
.swatch-3{background:var(--series-3);}
.stack-statutory{fill:var(--series-1);}
.stack-execution{fill:var(--series-2);}
.stack-uncertainty{fill:var(--series-3);}
.bar{fill:var(--series-1);}
.bar-label{fill:var(--text-secondary);font-size:11px;font-variant-numeric:tabular-nums;}
.row-label{fill:var(--text-secondary);font-size:11px;}
/* A wash, never a saturated block — the track is context, the floor mark is the finding. */
.range-track{fill:var(--series-1);opacity:0.14;}
.floor-band{fill:var(--series-1);opacity:0.32;}
.floor-rule{stroke:var(--series-1);stroke-width:2;stroke-linecap:round;}
.range-dot{fill:var(--series-1);stroke:var(--surface-1);stroke-width:2;}
.range-ring{fill:var(--surface-1);stroke:var(--series-1);stroke-width:2;}
.ladder-note{color:var(--text-secondary);font-size:12px;margin:0 0 8px;}
/* A full-panel SVG scales its 11px type up with the panel — at 1400px a tick label renders
   three times the size of the page's own text. The cap keeps chart type near body size, the
   same size the staircase cards get from their grid column. */
.plot{max-width:620px;}
/* The page-wide rule makes an SVG a block that fills its column; a legend swatch is neither. */
.legend svg{display:inline-block;width:14px;height:14px;}
"""
# Substituted rather than %-formatted: a CSS stylesheet is full of `%` units, and
# %-formatting a stylesheet is how `width:100%` becomes a format-string error.
_PAGE_CSS = (
    _PAGE_CSS.replace("__CRITICAL__", _STATUS_CRITICAL)
    .replace("__WARNING__", _STATUS_WARNING)
    .replace("__GOOD__", _STATUS_GOOD)
    .replace("__SERIES_LIGHT__", _SERIES_LIGHT)
    .replace("__SERIES_DARK__", _SERIES_DARK)
    .replace("__SERIES_TWO_LIGHT__", _SERIES_TWO_LIGHT)
    .replace("__SERIES_TWO_DARK__", _SERIES_TWO_DARK)
    .replace("__SERIES_THREE_LIGHT__", _SERIES_THREE_LIGHT)
    .replace("__SERIES_THREE_DARK__", _SERIES_THREE_DARK)
)


# --------------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class SegmentPricingAssumption:
    """The size and price a segment is SHOWN at — the caller's assumption, never this module's.

    Every money figure on the page is a function of these, and they are parameters rather than
    constants precisely because they are assumptions: a representative NIFTY option premium is
    not a fact about the cost engine, and freezing one here would turn a display choice into a
    silent policy that every reader would then take for a measurement.
    """

    segment: ChargeableSegment
    price_paise: Decimal
    lot_size: int
    representative_lots: int
    maximum_lots: int
    strike_paise: Decimal | None = None
    option_right: OptionRight | None = None

    @property
    def representative_quantity(self) -> int:
        return self.lot_size * self.representative_lots


@dataclass(frozen=True, slots=True)
class SegmentCostRow:
    """One segment's affordability, or the reason it has none."""

    segment_value: str
    representative_quantity: int
    round_trip_cost_bps: Decimal | None
    cheapest_cost_bps: Decimal | None
    breakeven_move_bps: Decimal | None
    minimum_viable_quantity: int | None
    weakest_evidence_grade: EvidenceGrade | None
    refusal_reason: str

    @property
    def is_priced(self) -> bool:
        return not self.refusal_reason


@dataclass(frozen=True, slots=True)
class CostStaircaseTread:
    """One step: what a round trip costs, in bps, at exactly this quantity."""

    quantity: int
    cost_bps: Decimal


@dataclass(frozen=True, slots=True)
class CostStaircase:
    """The step function for one segment, plus the invariant that says it is well formed.

    `largest_cost_rise_bps` is carried alongside the invariant because the invariant alone
    cannot be acted on. Cost in bps must never RISE with quantity, but an exact-Decimal
    comparison also trips on the broker rounding a levy to a whole rupee, which moves the
    fourth decimal place. The SIZE of the largest rise is what separates the two: a rise far
    below the cost itself is rounding, a rise comparable to it is a schedule whose pieces
    overlap. Reporting the invariant without the size would put a defect badge on arithmetic
    noise, and a badge that cries wolf stops being read.
    """

    segment_value: str
    lot_size: int
    treads: tuple[CostStaircaseTread, ...]
    is_monotonically_cheaper: bool
    largest_cost_rise_bps: Decimal


@dataclass(frozen=True, slots=True)
class ComponentReconciliationRow:
    """What the contract notes say about one modelled component."""

    component_value: str
    segment_value: str
    verdict: ReconciliationVerdict
    observation_count: int
    mean_residual_paise: Decimal
    explanation: str


@dataclass(frozen=True, slots=True)
class RefusedEraRow:
    """The date before which one component cannot be priced at all, and which half binds.

    A component needs TWO facts before it can be priced: a structural record saying the levy
    applies to this segment on this base and leg, and a rate record saying how much. They have
    different histories, and the later of the two is the real boundary. Splitting them matters
    because they refuse for different reasons: the exchange transaction charge has a structural
    record back to 2004 and no usable RATE before the flat-rate era, so a page that reported
    only the structure would claim two decades of coverage that does not exist.
    """

    component_value: str
    earliest_priceable_date: date
    structure_known_from: date
    rate_known_from: date | None
    binding_constraint: str
    reason: str


@dataclass(frozen=True, slots=True)
class HurdleDecompositionRow:
    """One priced ticket's hurdle, split into what it costs and what it is unsure about.

    Every figure is read off the gate's own `CostHurdle` — `point_bps`, `required_bps` and
    `uncertainty_bps` are that object's properties, copied, not recomputed. The page must not be
    able to disagree with the arithmetic the decision was actually made with, and the only way to
    guarantee that is to never do the arithmetic twice.
    """

    trading_symbol: str
    segment_value: str
    quantity: int
    statutory_bps: Decimal
    execution_point_bps: Decimal
    execution_upper_bps: Decimal
    point_bps: Decimal
    required_bps: Decimal
    uncertainty_bps: Decimal
    is_execution_censored: bool
    verdict_value: str
    round_trip_spread_bps: Decimal | None
    execution_interval_width_bps: Decimal | None

    @property
    def uncertainty_percent_of_required(self) -> Decimal | None:
        """How much of the bar a signal has to clear is margin rather than expected cost."""
        if self.required_bps <= 0:
            return None
        return self.uncertainty_bps / self.required_bps * _PERCENT


@dataclass(frozen=True, slots=True)
class HurdleSizeLadder:
    """One instrument priced at one or more sizes, ascending — the shape of the margin.

    Ascending rather than worst-first, alone on this page. The ladder exists to answer whether
    the margin widens with size, and sorting it by cost would destroy exactly the ordering that
    question is asked in.
    """

    trading_symbol: str
    segment_value: str
    rows: tuple[HurdleDecompositionRow, ...]

    @property
    def label(self) -> str:
        return f"{self.trading_symbol} · {self.segment_value}"

    @property
    def largest_required_bps(self) -> Decimal:
        return max((row.required_bps for row in self.rows), default=Decimal(0))

    @property
    def uncertainty_widening_bps(self) -> Decimal | None:
        """How much wider the margin is at the largest size than at the smallest.

        `None` when the instrument was priced at one size only, which is the honest answer: a
        single point cannot show a trend, and reporting zero would claim a flat one.
        """
        if len(self.rows) < _MINIMUM_ROWS_FOR_A_SIZE_LADDER:
            return None
        return self.rows[-1].uncertainty_bps - self.rows[0].uncertainty_bps

    @property
    def has_censored_size(self) -> bool:
        """Whether any size on this ladder exceeded the visible book and was extrapolated."""
        return any(row.is_execution_censored for row in self.rows)


class FloorEvidenceVerdict(StrEnum):
    """How much of a floor is measurement and how much is one instrument having a bad day.

    Derived from the floor's own quantile arithmetic, never from a chosen instrument count: a
    threshold like "thin below fifty" is the kind of magic constant `R.03` forbids, and it would
    be wrong at both ends anyway. What is structural is that a nearest-rank quantile whose rank
    is one IS a single observation, and that a floor whose standard error reaches the segment's
    own median cannot be told apart from the middle of the distribution.
    """

    SINGLE_OBSERVATION = "single observation"
    OVERLAPS_MEDIAN = "overlaps median"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class SegmentEdgeFloorRow:
    """One `L1.04` floor, the distribution behind it, and how firmly it is pinned.

    `floor_uncertainty_bps` is the part `R.04` is about. The floor is a nearest-rank quantile, so
    its sampling error is a rank error: the rank of the true quantile has standard deviation
    sqrt(n·q·(1-q)), and converting that into basis points at the local slope between the floor
    and the median turns "how many instruments stand behind this" into a width the reader can
    see. Two hundred instruments pin a floor to a fraction of a basis point; eleven do not.
    """

    segment_value: str
    session_date: date
    floor_bps: Decimal
    median_hurdle_bps: Decimal
    cheapest_hurdle_bps: Decimal
    dearest_hurdle_bps: Decimal
    floor_quantile: Decimal
    instrument_count: int
    supporting_instrument_rank: int
    floor_uncertainty_bps: Decimal
    evidence_verdict: FloorEvidenceVerdict
    description: str

    @property
    def spread_of_hurdles_bps(self) -> Decimal:
        return self.dearest_hurdle_bps - self.cheapest_hurdle_bps

    @property
    def floor_lower_bps(self) -> Decimal:
        """The optimistic end of the floor's own error band, never below zero."""
        return max(Decimal(0), self.floor_bps - self.floor_uncertainty_bps)

    @property
    def floor_upper_bps(self) -> Decimal:
        return self.floor_bps + self.floor_uncertainty_bps


@dataclass(frozen=True, slots=True)
class GateVerdictCensus:
    """What the gate decided, counted — with the fourth count held apart from the other three.

    `unpriceable_count` is deliberately NOT part of `rejected_count`. A veto is a judgement that
    a trade is not worth taking; an unpriceable is the absence of any judgement at all. Summing
    them would produce a "rejections" figure that quietly grows every time a data feed breaks.
    """

    pass_count: int
    resize_count: int
    veto_count: int
    unpriceable_count: int

    @property
    def judged_count(self) -> int:
        """Decisions the gate actually formed an opinion on."""
        return self.pass_count + self.resize_count + self.veto_count

    @property
    def evaluated_count(self) -> int:
        return self.judged_count + self.unpriceable_count

    @property
    def tradeable_count(self) -> int:
        return self.pass_count + self.resize_count

    @property
    def unpriceable_percent_of_evaluations(self) -> Decimal | None:
        if self.evaluated_count <= 0:
            return None
        return Decimal(self.unpriceable_count) / Decimal(self.evaluated_count) * _PERCENT


@dataclass(frozen=True, slots=True)
class PreconditionFailureRow:
    """One named ticket precondition, and how often it was the thing that killed the trade."""

    precondition_name: str
    failure_count: int
    evaluated_count: int
    example_failure_reason: str

    @property
    def failure_percent_of_evaluations(self) -> Decimal | None:
        if self.evaluated_count <= 0:
            return None
        return Decimal(self.failure_count) / Decimal(self.evaluated_count) * _PERCENT


@dataclass(frozen=True, slots=True)
class TransactionCostSurfaceState:
    """Everything the page shows, already resolved. The renderer reads nothing else."""

    priced_on: date
    broker: str
    cost_bps_ceiling: Decimal
    segment_rows: tuple[SegmentCostRow, ...]
    staircases: tuple[CostStaircase, ...]
    reconciliation_rows: tuple[ComponentReconciliationRow, ...]
    refused_eras: tuple[RefusedEraRow, ...]
    ledger_observation_count: int
    # `F01`. Every one of these is optional and empty by default, so a caller built against the
    # `L1.01`-only page keeps working and simply renders the gate sections as "not supplied"
    # rather than as "nothing was rejected", which is the failure this whole page is about.
    hurdle_ladders: tuple[HurdleSizeLadder, ...] = ()
    edge_floor_rows: tuple[SegmentEdgeFloorRow, ...] = ()
    verdict_census: GateVerdictCensus | None = None
    precondition_rows: tuple[PreconditionFailureRow, ...] = ()

    @property
    def hurdle_rows(self) -> tuple[HurdleDecompositionRow, ...]:
        return tuple(row for ladder in self.hurdle_ladders for row in ladder.rows)

    @property
    def hurdle_ceiling_bps(self) -> Decimal:
        """The axis every ladder shares. One scale, so two cards can be read against each other."""
        return max((row.required_bps for row in self.hurdle_rows), default=Decimal(0))

    @property
    def dearest_hurdle_row(self) -> HurdleDecompositionRow | None:
        rows = self.hurdle_rows
        return max(rows, key=lambda row: row.required_bps) if rows else None

    @property
    def widest_uncertainty_row(self) -> HurdleDecompositionRow | None:
        """Where the margin is largest — the ticket whose cost is least well known."""
        rows = self.hurdle_rows
        return max(rows, key=lambda row: row.uncertainty_bps) if rows else None

    @property
    def censored_hurdle_count(self) -> int:
        """Tickets whose size exceeded the visible book, so the impact term is extrapolated."""
        return sum(1 for row in self.hurdle_rows if row.is_execution_censored)

    @property
    def thinnest_floor_row(self) -> SegmentEdgeFloorRow | None:
        """The floor with the fewest instruments behind it — the one to trust least."""
        rows = self.edge_floor_rows
        return min(rows, key=lambda row: row.instrument_count) if rows else None

    @property
    def failing_precondition_count(self) -> int:
        return sum(row.failure_count for row in self.precondition_rows)

    @property
    def priced_rows(self) -> tuple[SegmentCostRow, ...]:
        return tuple(row for row in self.segment_rows if row.is_priced)

    @property
    def refused_segment_count(self) -> int:
        return len(self.segment_rows) - len(self.priced_rows)

    @property
    def cheapest_achievable_cost_bps(self) -> Decimal | None:
        """The floor of the cheapest staircase — the best this account can do anywhere."""
        achievable = [
            row.cheapest_cost_bps for row in self.priced_rows if row.cheapest_cost_bps is not None
        ]
        return min(achievable) if achievable else None

    @property
    def cheapest_segment_value(self) -> str:
        """Which segment owns that floor — a bare number cannot be acted on."""
        candidates = [
            (row.cheapest_cost_bps, row.segment_value)
            for row in self.priced_rows
            if row.cheapest_cost_bps is not None
        ]
        return min(candidates)[1] if candidates else "—"

    @property
    def weakest_evidence_grade(self) -> EvidenceGrade | None:
        """A page of costs is exactly as trustworthy as the worst-sourced rate under any of them."""
        grades = [
            row.weakest_evidence_grade
            for row in self.priced_rows
            if row.weakest_evidence_grade is not None
        ]
        return min(grades, key=evidence_grade_rank) if grades else None

    @property
    def drifting_component_count(self) -> int:
        return sum(
            1
            for row in self.reconciliation_rows
            if row.verdict is ReconciliationVerdict.DRIFTS
        )

    @property
    def has_rate_era_evidence(self) -> bool:
        """Whether the refused-era panel saw a rule store, or only the structure history.

        Without one the panel knows when a levy STARTED APPLYING and not when its rate was
        first compiled, which is a strictly weaker claim. The page has to say which of the two
        it is making, because the weaker one silently overstates coverage.
        """
        return any(row.rate_known_from is not None for row in self.refused_eras)

    @property
    def earliest_priceable_date(self) -> date | None:
        """Nothing on this page can be computed before the LATEST of the era starts."""
        if not self.refused_eras:
            return None
        return max(row.earliest_priceable_date for row in self.refused_eras)


# --------------------------------------------------------------------------- assembly


def build_transaction_cost_surface_state(
    engine: NseTransactionCostEngine,
    ledger: ChargeReconciliationLedger,
    assumptions: Sequence[SegmentPricingAssumption],
    *,
    priced_on: date,
    cost_bps_ceiling: Decimal,
    structure_history: ChargeStructureHistory | None = None,
    rule_store: PointInTimeMarketRuleStore | None = None,
    known_as_of: date | None = None,
    gate_decisions: Sequence[GateDecision] = (),
    edge_floors: Sequence[SegmentEdgeFloor] = (),
) -> TransactionCostSurfaceState:
    """Price every segment once, read the ledger once, and freeze the result.

    Kept apart from the renderer because this is the half that can fail, can be slow and can
    touch a database. A refusal from the engine becomes a ROW here, never an exception that
    takes the page down: a segment nobody can price is the most important thing on the page.

    `rule_store` is the same store the engine resolves rates from. Passing it is what lets the
    refused-era panel report the RATE boundary as well as the structural one; without it the
    panel reports only what the structure history knows, and says so rather than implying the
    earlier dates are priceable.

    `gate_decisions` are `L1.02` decisions the caller has ALREADY taken — this function never
    runs the gate. Three of the page's four `F01` sections are read out of them (the hurdle
    decomposition, the verdict census, the precondition failures) precisely because they are one
    object: a page that recomputed any of the three would be able to show a hurdle that never
    decided anything. `edge_floors` are `L1.04` floors as derived and stored, for the same
    reason. Both default to empty, so every existing caller keeps the `L1.01` page unchanged.
    """
    history = structure_history or nse_charge_structure_history()
    rows: list[SegmentCostRow] = []
    staircases: list[CostStaircase] = []
    for assumption in assumptions:
        row, staircase = _price_one_segment(
            engine,
            assumption,
            priced_on=priced_on,
            cost_bps_ceiling=cost_bps_ceiling,
            known_as_of=known_as_of,
        )
        rows.append(row)
        if staircase is not None:
            staircases.append(staircase)
    rows.sort(key=_worst_first_sort_key)
    staircase_order = {row.segment_value: index for index, row in enumerate(rows)}
    staircases.sort(key=lambda staircase: staircase_order.get(staircase.segment_value, 0))
    return TransactionCostSurfaceState(
        priced_on=priced_on,
        broker=engine.broker,
        cost_bps_ceiling=cost_bps_ceiling,
        segment_rows=tuple(rows),
        staircases=tuple(staircases),
        reconciliation_rows=_reconciliation_rows(engine, ledger, priced_on=priced_on),
        refused_eras=_refused_era_rows(history, rule_store),
        ledger_observation_count=ledger.observation_count(),
        hurdle_ladders=_hurdle_size_ladders(gate_decisions),
        edge_floor_rows=_edge_floor_rows(edge_floors),
        verdict_census=_verdict_census(gate_decisions),
        precondition_rows=_precondition_failure_rows(gate_decisions),
    )


def _trade_template(assumption: SegmentPricingAssumption, priced_on: date) -> TradeSpecification:
    """One lot, entered and exited at the same price — the size ladder scales from here.

    Entry and exit at the same price is not a claim that the trade is flat: the breakeven
    solver replaces the exit price with the one it solves for, and the cost curve replaces the
    quantity. What this fixes is the only thing both of them need held still.
    """
    return TradeSpecification(
        segment=assumption.segment,
        quantity=assumption.lot_size,
        entry_price_paise=assumption.price_paise,
        exit_price_paise=assumption.price_paise,
        trade_date=priced_on,
        strike_paise=assumption.strike_paise,
        option_right=assumption.option_right,
    )


def _price_one_segment(
    engine: NseTransactionCostEngine,
    assumption: SegmentPricingAssumption,
    *,
    priced_on: date,
    cost_bps_ceiling: Decimal,
    known_as_of: date | None,
) -> tuple[SegmentCostRow, CostStaircase | None]:
    """Everything one row and one chart need, or one refusal explaining why neither exists."""
    try:
        template = _trade_template(assumption, priced_on)
        curve = cost_curve(
            engine,
            template,
            lot_size=assumption.lot_size,
            maximum_lots=assumption.maximum_lots,
            known_as_of=known_as_of,
        )
        representative = replace(template, quantity=assumption.representative_quantity)
        priced = engine.price_round_trip(representative, known_as_of=known_as_of)
        breakeven = solve_breakeven_move(engine, representative, known_as_of=known_as_of)
        viable = minimum_viable_quantity(
            engine,
            template,
            cost_bps_ceiling=cost_bps_ceiling,
            lot_size=assumption.lot_size,
            maximum_lots=assumption.maximum_lots,
            known_as_of=known_as_of,
        )
    except TransactionCostError as error:
        return (
            SegmentCostRow(
                segment_value=assumption.segment.value,
                representative_quantity=assumption.representative_quantity,
                round_trip_cost_bps=None,
                cheapest_cost_bps=None,
                breakeven_move_bps=None,
                minimum_viable_quantity=None,
                weakest_evidence_grade=None,
                refusal_reason=str(error),
            ),
            None,
        )
    treads = tuple(
        CostStaircaseTread(quantity=point.quantity, cost_bps=point.cost_bps)
        for point in curve.points
    )
    staircase = CostStaircase(
        segment_value=assumption.segment.value,
        lot_size=assumption.lot_size,
        treads=treads,
        is_monotonically_cheaper=curve.is_monotonically_cheaper,
        largest_cost_rise_bps=_largest_cost_rise_bps(treads),
    )
    row = SegmentCostRow(
        segment_value=assumption.segment.value,
        representative_quantity=assumption.representative_quantity,
        round_trip_cost_bps=priced.total_bps_of_turnover,
        cheapest_cost_bps=curve.cheapest.cost_bps,
        breakeven_move_bps=breakeven.move_bps,
        minimum_viable_quantity=viable,
        weakest_evidence_grade=priced.weakest_evidence_grade,
        refusal_reason="",
    )
    return row, staircase


def _largest_cost_rise_bps(treads: Sequence[CostStaircaseTread]) -> Decimal:
    """How far cost in bps ever goes UP as quantity grows. Zero on a well-formed staircase."""
    rises = [
        later.cost_bps - earlier.cost_bps
        for earlier, later in pairwise(treads)
        if later.cost_bps > earlier.cost_bps
    ]
    return max(rises) if rises else Decimal(0)


def _worst_first_sort_key(row: SegmentCostRow) -> tuple[int, Decimal, str]:
    """Refusals above everything, then the most expensive first."""
    if not row.is_priced:
        return (0, Decimal(0), row.segment_value)
    cost = row.round_trip_cost_bps if row.round_trip_cost_bps is not None else Decimal(0)
    return (1, -cost, row.segment_value)


def _reconciliation_rows(
    engine: NseTransactionCostEngine,
    ledger: ChargeReconciliationLedger,
    *,
    priced_on: date,
) -> tuple[ComponentReconciliationRow, ...]:
    """Reconcile against the rounding the BROKER actually applies, not a default.

    A component compared against the wrong granularity reports drift on rounding noise, or
    reports agreement on a real rate error. The schedule in force on the page's own date is
    where the true granularity lives, so it is read from there and passed through.
    """
    reconciliations = ledger.reconcile(
        rounding_by_component=_rounding_by_component(engine, priced_on)
    )
    rows = [
        ComponentReconciliationRow(
            component_value=reconciliation.component.value,
            segment_value=reconciliation.segment.value,
            verdict=reconciliation.verdict,
            observation_count=reconciliation.observation_count,
            mean_residual_paise=reconciliation.mean_residual_paise,
            explanation=reconciliation.explanation,
        )
        for reconciliation in reconciliations
    ]
    verdict_rank = {
        ReconciliationVerdict.DRIFTS: 0,
        ReconciliationVerdict.UNVERIFIED: 1,
        ReconciliationVerdict.AGREES: 2,
    }
    rows.sort(key=lambda row: (verdict_rank[row.verdict], row.component_value, row.segment_value))
    return tuple(rows)


def _rounding_by_component(
    engine: NseTransactionCostEngine, priced_on: date
) -> dict[ChargeComponent, RoundingRule] | None:
    """The broker's rounding per component, or `None` when the schedule itself is uncovered."""
    try:
        schedule = engine.schedule_for(priced_on)
    except TransactionCostError:
        return None
    return {
        component: (
            schedule.statutory_rounding
            if component in _TRANSACTION_TAX_COMPONENTS
            else schedule.brokerage_rounding
        )
        for component in ChargeComponent
    }


def _earliest_rate_records(
    rule_store: PointInTimeMarketRuleStore | None,
) -> dict[RuleFamily, MarketRuleRecord]:
    """The oldest compiled rate per family — the date before which every resolve refuses."""
    if rule_store is None:
        return {}
    earliest: dict[RuleFamily, MarketRuleRecord] = {}
    for record in rule_store.records():
        held = earliest.get(record.family)
        if held is None or record.effective_from < held.effective_from:
            earliest[record.family] = record
    return earliest


def _refused_era_rows(
    history: ChargeStructureHistory, rule_store: PointInTimeMarketRuleStore | None
) -> tuple[RefusedEraRow, ...]:
    """Derive the refusal boundaries from the same records the engine itself resolves against.

    Not typed in by hand: the earliest structural record is the date before which a levy has no
    base and no leg, the earliest rate record is the date before which it has no amount, and
    the LATER of the two is the date before which the engine refuses. Reading both from the
    live objects keeps the page from drifting out of step the next time an era is compiled
    backwards — a hardcoded boundary would go on claiming a refusal that had been fixed, or
    worse, stop reporting one that had not.
    """
    earliest_structures: dict[ChargeComponent, ChargeStructureRecord] = {}
    for record in history.records:
        held = earliest_structures.get(record.component)
        if held is None or record.effective_from < held.effective_from:
            earliest_structures[record.component] = record
    earliest_rates = _earliest_rate_records(rule_store)
    rows = [
        _refused_era_row_for(component, structure, earliest_rates)
        for component, structure in earliest_structures.items()
    ]
    rows.sort(key=lambda row: (-row.earliest_priceable_date.toordinal(), row.component_value))
    return tuple(rows)


def _refused_era_row_for(
    component: ChargeComponent,
    structure: ChargeStructureRecord,
    earliest_rates: dict[RuleFamily, MarketRuleRecord],
) -> RefusedEraRow:
    family = component.rule_family
    rate = earliest_rates.get(family) if family is not None else None
    if rate is not None and rate.effective_from > structure.effective_from:
        return RefusedEraRow(
            component_value=component.value,
            earliest_priceable_date=rate.effective_from,
            structure_known_from=structure.effective_from,
            rate_known_from=rate.effective_from,
            binding_constraint="no rate compiled",
            reason=rate.source_reference,
        )
    # Equal dates are the common case and are NOT a missing structural record: the levy and
    # its first rate simply begin together. Saying "no structural record" there would invent a
    # gap, which is the same failure this panel exists to prevent, pointed the other way.
    binds = (
        "both begin here"
        if rate is not None and rate.effective_from == structure.effective_from
        else "no structural record"
    )
    return RefusedEraRow(
        component_value=component.value,
        earliest_priceable_date=structure.effective_from,
        structure_known_from=structure.effective_from,
        rate_known_from=rate.effective_from if rate is not None else None,
        binding_constraint=binds,
        reason=structure.source_reference,
    )


# --------------------------------------------------------------------------- F01 assembly


def _priced_quantity(decision: GateDecision) -> int:
    """The size the hurdle on this decision was actually computed at.

    On a RESIZE that is the approved quantity and not the proposed one — the gate re-prices at
    the size it solved for, and showing the proposed size beside a resized hurdle would put the
    wrong denominator under every basis point on the row. The fill knows its own quantity, so it
    is asked first; the verdict is only consulted when there is no fill to ask.
    """
    fill = decision.expected_fill
    if fill is not None:
        return fill.quantity
    if decision.verdict is GateVerdict.RESIZE:
        return decision.approved_quantity
    return decision.signal.proposed_quantity


def _hurdle_decomposition_row(decision: GateDecision) -> HurdleDecompositionRow | None:
    """One decision's hurdle as a display row, or `None` when it never got one.

    An `UNPRICEABLE` decision has no hurdle at all, and that is the point of it: there is nothing
    to decompose, so it contributes to the verdict census and to nothing else. Inventing a zero
    row for it would draw an empty bar that reads as "free to trade".
    """
    hurdle = decision.hurdle
    if hurdle is None:
        return None
    fill = decision.expected_fill
    return HurdleDecompositionRow(
        trading_symbol=decision.signal.trading_symbol,
        segment_value=decision.signal.segment.value,
        quantity=_priced_quantity(decision),
        statutory_bps=hurdle.statutory_bps,
        execution_point_bps=hurdle.execution_point_bps,
        execution_upper_bps=hurdle.execution_upper_bps,
        point_bps=hurdle.point_bps,
        required_bps=hurdle.required_bps,
        uncertainty_bps=hurdle.uncertainty_bps,
        is_execution_censored=hurdle.is_execution_censored,
        verdict_value=decision.verdict.value,
        round_trip_spread_bps=(
            None if fill is None else fill.spread_cost_bps * _LEGS_PER_ROUND_TRIP
        ),
        execution_interval_width_bps=(
            None if fill is None else fill.cost_interval_width_bps * _LEGS_PER_ROUND_TRIP
        ),
    )


def _hurdle_size_ladders(decisions: Sequence[GateDecision]) -> tuple[HurdleSizeLadder, ...]:
    """Group priced hurdles by instrument, order the sizes inside each, worst ladder first."""
    grouped: dict[tuple[str, str], list[HurdleDecompositionRow]] = {}
    for decision in decisions:
        row = _hurdle_decomposition_row(decision)
        if row is None:
            continue
        grouped.setdefault((row.trading_symbol, row.segment_value), []).append(row)
    ladders = [
        HurdleSizeLadder(
            trading_symbol=trading_symbol,
            segment_value=segment_value,
            rows=tuple(sorted(rows, key=lambda row: row.quantity)),
        )
        for (trading_symbol, segment_value), rows in grouped.items()
    ]
    ladders.sort(key=lambda ladder: (-ladder.largest_required_bps, ladder.label))
    return tuple(ladders)


def _verdict_census(decisions: Sequence[GateDecision]) -> GateVerdictCensus | None:
    """Count the four verdicts, or `None` when no decision was supplied at all.

    `None` rather than four zeros, deliberately. Zero vetoes out of zero evaluations and zero
    vetoes out of a thousand are opposite findings, and a census of zeros renders as the second.
    """
    if not decisions:
        return None
    counts = dict.fromkeys(GateVerdict, 0)
    for decision in decisions:
        counts[decision.verdict] += 1
    return GateVerdictCensus(
        pass_count=counts[GateVerdict.PASS],
        resize_count=counts[GateVerdict.RESIZE],
        veto_count=counts[GateVerdict.VETO],
        unpriceable_count=counts[GateVerdict.UNPRICEABLE],
    )


def _precondition_failure_rows(
    decisions: Sequence[GateDecision],
) -> tuple[PreconditionFailureRow, ...]:
    """Count failures per named check, keeping one real reason per name.

    Every `PreconditionName` gets a row even when it never failed: the four checks are a fixed
    set, and a name that vanishes from the table reads as "not applicable" when what actually
    happened is "checked, and fine". Rows carry their own evaluated count because the checks do
    not all run on the same tickets — the range-width check only fires for a caller proposing a
    range, and a share computed against the wrong denominator would understate it.
    """
    failure_counts = dict.fromkeys(PreconditionName, 0)
    evaluated_counts = dict.fromkeys(PreconditionName, 0)
    example_reasons: dict[PreconditionName, str] = {}
    reports = [
        decision.preconditions for decision in decisions if decision.preconditions is not None
    ]
    if not reports:
        return ()
    for report in reports:
        for result in report.results:
            evaluated_counts[result.name] += 1
            if not result.is_satisfied:
                failure_counts[result.name] += 1
                example_reasons.setdefault(result.name, result.reason)
    rows = [
        PreconditionFailureRow(
            precondition_name=name.value,
            failure_count=failure_counts[name],
            evaluated_count=evaluated_counts[name],
            example_failure_reason=example_reasons.get(name, ""),
        )
        for name in PreconditionName
    ]
    rows.sort(key=lambda row: (-row.failure_count, row.precondition_name))
    return tuple(rows)


def _nearest_rank(instrument_count: int, quantile: Decimal) -> int:
    """The rank `derive_segment_floor` itself selects, restated so the page can reason about it.

    Restated rather than imported because the private helper it mirrors returns a VALUE and this
    needs the RANK — the position in the sorted universe that the published floor came from. That
    position is the whole evidence story: rank 10 of 200 is a quantile, rank 1 of 11 is one
    instrument wearing a quantile's name.
    """
    return int((Decimal(instrument_count) * quantile).to_integral_value(rounding=ROUND_CEILING))


def _floor_uncertainty_bps(floor: SegmentEdgeFloor, floor_rank: int) -> Decimal:
    """The floor's own standard error, in basis points, from the depth of evidence behind it.

    A nearest-rank quantile's sampling error is an error in the RANK it selects: over repeated
    samples of the same segment, the rank of the true quantile has standard deviation
    sqrt(n·q·(1-q)). That is a count, not a cost, so it is converted at the local slope of the
    hurdle distribution — the basis points per rank between the floor and the median, the two
    order statistics the floor actually publishes. The result is what `R.04` asks a surface to
    show: the same algorithm at every maturity, with the confidence visibly different.

    Zero when the floor and the median select the same rank. There is then no measured slope to
    convert with, and inventing one would put a confident-looking band on nothing.
    """
    median_rank = _nearest_rank(floor.instrument_count, _MEDIAN_QUANTILE)
    ranks_between = median_rank - floor_rank
    if ranks_between <= 0:
        return Decimal(0)
    bps_per_rank = (floor.median_hurdle_bps - floor.floor_bps) / Decimal(ranks_between)
    rank_standard_deviation = (
        Decimal(floor.instrument_count) * floor.floor_quantile * (Decimal(1) - floor.floor_quantile)
    ).sqrt()
    return rank_standard_deviation * bps_per_rank


def _floor_evidence_verdict(
    floor: SegmentEdgeFloor, floor_rank: int, uncertainty_bps: Decimal
) -> FloorEvidenceVerdict:
    """Both tests are comparisons between measured quantities, never against a chosen count."""
    if floor_rank <= _FLOOR_RANK_OF_A_LONE_OBSERVATION:
        return FloorEvidenceVerdict.SINGLE_OBSERVATION
    if floor.floor_bps + uncertainty_bps >= floor.median_hurdle_bps:
        return FloorEvidenceVerdict.OVERLAPS_MEDIAN
    return FloorEvidenceVerdict.RESOLVED


def _edge_floor_rows(floors: Sequence[SegmentEdgeFloor]) -> tuple[SegmentEdgeFloorRow, ...]:
    """Cheapest floor first: the tightest screen is the one a reader reaches for."""
    rows = []
    for floor in floors:
        floor_rank = _nearest_rank(floor.instrument_count, floor.floor_quantile)
        uncertainty_bps = _floor_uncertainty_bps(floor, floor_rank)
        rows.append(
            SegmentEdgeFloorRow(
                segment_value=floor.segment.value,
                session_date=floor.session_date,
                floor_bps=floor.floor_bps,
                median_hurdle_bps=floor.median_hurdle_bps,
                cheapest_hurdle_bps=floor.cheapest_hurdle_bps,
                dearest_hurdle_bps=floor.dearest_hurdle_bps,
                floor_quantile=floor.floor_quantile,
                instrument_count=floor.instrument_count,
                supporting_instrument_rank=floor_rank,
                floor_uncertainty_bps=uncertainty_bps,
                evidence_verdict=_floor_evidence_verdict(floor, floor_rank, uncertainty_bps),
                description=floor.describe(),
            )
        )
    rows.sort(key=lambda row: (row.floor_bps, row.segment_value))
    return tuple(rows)


# --------------------------------------------------------------------------- chart geometry

_VIEW_WIDTH = Decimal(480)
_VIEW_HEIGHT = Decimal(214)
_PLOT_LEFT = Decimal(48)
_PLOT_RIGHT = Decimal(470)
_PLOT_TOP = Decimal(18)
_PLOT_BOTTOM = Decimal(172)
_TICK_LABEL_BASELINE = Decimal(190)
_TREAD_RADIUS = Decimal(4)
_HIT_RADIUS = Decimal(13)
_LABEL_LIFT = Decimal(10)
_AXIS_HEADROOM_FRACTION = Decimal("0.14")
_GRIDLINE_FRACTIONS = (Decimal(0), Decimal("0.5"), Decimal(1))
_COORDINATE_QUANTUM = Decimal("0.1")
_BPS_QUANTUM = Decimal("0.01")
_RISE_QUANTUM = Decimal("0.000001")
_LABEL_INSET = Decimal(6)
_MINIMUM_TREADS_FOR_A_STEP = 2
_PERCENT_QUANTUM = Decimal("0.1")

# `F01` geometry. Three more charts, each with its own row height and gutters, all drawn into
# the same 480-unit viewBox so cards sitting side by side in the grid share a visual scale.
_HURDLE_PLOT_LEFT = Decimal(66)
_HURDLE_PLOT_RIGHT = Decimal(404)
_HURDLE_ROW_HEIGHT = Decimal(32)
_HURDLE_BAR_THICKNESS = Decimal(18)
_HURDLE_TOP_PADDING = Decimal(14)
_HURDLE_AXIS_BAND = Decimal(34)
_SEGMENT_GAP = Decimal(2)
"""The surface gap that separates touching fills. One width, everywhere, never a stroke."""
_BAR_CORNER_RADIUS = Decimal(4)
_LABEL_GAP = Decimal(6)
_TEXT_CENTRING_LIFT = Decimal(4)
"""Half an 11px cap-height — what a baseline needs to sit a label on a mark's centre line."""

_FLOOR_PLOT_LEFT = Decimal(94)
_FLOOR_PLOT_RIGHT = Decimal(446)
_FLOOR_ROW_HEIGHT = Decimal(54)
_FLOOR_TOP_PADDING = Decimal(20)
_FLOOR_AXIS_BAND = Decimal(36)
_FLOOR_TRACK_THICKNESS = Decimal(8)
_FLOOR_BAND_THICKNESS = Decimal(20)
_FLOOR_RULE_HALF_HEIGHT = Decimal(14)
_FLOOR_MARKER_RADIUS = Decimal(4)
_FLOOR_LABEL_LIFT = Decimal(16)
_FLOOR_LABEL_DROP = Decimal(18)
_DECADE = Decimal(10)

_PRECONDITION_PLOT_LEFT = Decimal(198)
_PRECONDITION_PLOT_RIGHT = Decimal(432)
_PRECONDITION_ROW_HEIGHT = Decimal(28)
_PRECONDITION_BAR_THICKNESS = Decimal(14)
_PRECONDITION_TOP_PADDING = Decimal(12)
_PRECONDITION_AXIS_BAND = Decimal(30)

_HURDLE_STACK_PARTS: tuple[tuple[str, str], ...] = (
    ("stack-statutory", "statutory charges"),
    ("stack-execution", "execution, expected"),
    ("stack-uncertainty", "uncertainty margin"),
)
"""The stack, in the order it is drawn: known cost, estimated cost, then the margin on top.

That order is the argument. Statutory charges are read from circulars, execution is estimated
from the book, and the margin is what the estimate does not know — so the bar reads left to
right from the most certain part of the hurdle to the least, and the reader can see how much of
the far end is not a cost at all.
"""


def _coordinate(value: Decimal) -> str:
    return str(value.quantize(_COORDINATE_QUANTUM))


def _format_bps(value: Decimal | None) -> str:
    return "—" if value is None else str(value.quantize(_BPS_QUANTUM))


def _format_residual_in_paise(value: Decimal) -> str:
    return str(value.quantize(_BPS_QUANTUM))


def _format_rise_bps(value: Decimal) -> str:
    """A rounding-scale rise is invisible at two decimals, so this one keeps six."""
    return str(value.quantize(_RISE_QUANTUM))


def _format_percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value.quantize(_PERCENT_QUANTUM)}%"


def _format_count(value: int) -> str:
    """Grouped, because a five-figure quantity read as a four-figure one is a sizing error."""
    return f"{value:,}"


def _interpolate(
    value: Decimal, low: Decimal, high: Decimal, start: Decimal, end: Decimal
) -> Decimal:
    """Map a value from its own domain onto a screen span, degenerate domains included."""
    if high == low:
        return (start + end) / 2
    return start + (value - low) / (high - low) * (end - start)


def _staircase_svg(staircase: CostStaircase) -> str:
    """One step function, drawn step-AFTER because a cost holds until the next whole lot.

    Step-after rather than a smoothed line is the honest shape: there is no such thing as the
    cost of half a lot, and a line drawn between treads would invite the reader to interpolate
    a size that cannot be traded.
    """
    treads = staircase.treads
    if len(treads) < _MINIMUM_TREADS_FOR_A_STEP:
        return ""
    quantity_low = Decimal(treads[0].quantity)
    quantity_high = Decimal(treads[-1].quantity)
    axis_top = max(tread.cost_bps for tread in treads) * (Decimal(1) + _AXIS_HEADROOM_FRACTION)

    def horizontal(quantity: int) -> Decimal:
        return _interpolate(
            Decimal(quantity), quantity_low, quantity_high, _PLOT_LEFT, _PLOT_RIGHT
        )

    def vertical(cost_bps: Decimal) -> Decimal:
        return _interpolate(cost_bps, Decimal(0), axis_top, _PLOT_BOTTOM, _PLOT_TOP)

    gridlines = "".join(
        f'<line class="gridline" x1="{_coordinate(_PLOT_LEFT)}" '
        f'y1="{_coordinate(vertical(axis_top * fraction))}" '
        f'x2="{_coordinate(_PLOT_RIGHT)}" '
        f'y2="{_coordinate(vertical(axis_top * fraction))}"></line>'
        f'<text class="tick" text-anchor="end" x="{_coordinate(_PLOT_LEFT - Decimal(6))}" '
        f'y="{_coordinate(vertical(axis_top * fraction) + Decimal(4))}">'
        f"{escape(_format_bps(axis_top * fraction))}</text>"
        for fraction in _GRIDLINE_FRACTIONS
    )
    def vertex(quantity: int, cost_bps: Decimal) -> str:
        return f"{_coordinate(horizontal(quantity))} {_coordinate(vertical(cost_bps))}"

    path = [f"M {vertex(treads[0].quantity, treads[0].cost_bps)}"]
    for earlier, later in pairwise(treads):
        path.append(f"L {vertex(later.quantity, earlier.cost_bps)}")
        path.append(f"L {vertex(later.quantity, later.cost_bps)}")
    marks = "".join(
        f'<g><title>{escape(str(tread.quantity))} units — '
        f"{escape(_format_bps(tread.cost_bps))} bps round trip</title>"
        f'<circle class="hit" cx="{_coordinate(horizontal(tread.quantity))}" '
        f'cy="{_coordinate(vertical(tread.cost_bps))}" r="{_coordinate(_HIT_RADIUS)}"></circle>'
        f'<circle class="tread" cx="{_coordinate(horizontal(tread.quantity))}" '
        f'cy="{_coordinate(vertical(tread.cost_bps))}" r="{_coordinate(_TREAD_RADIUS)}"></circle>'
        f"</g>"
        for tread in treads
    )
    # Selective direct labels: the two treads the reader is actually comparing. A number on
    # every tread turns the shape back into the table the chart exists to replace.
    # Inset from the marks, not centred on them: the first tread sits on the y-axis, where a
    # centred label lands on top of the topmost gridline tick.
    direct_labels = (
        f'<text class="tread-label" text-anchor="start" '
        f'x="{_coordinate(horizontal(treads[0].quantity) + _LABEL_INSET)}" '
        f'y="{_coordinate(vertical(treads[0].cost_bps) - _LABEL_LIFT)}">'
        f"{escape(_format_bps(treads[0].cost_bps))}</text>"
        f'<text class="tread-label" text-anchor="end" '
        f'x="{_coordinate(horizontal(treads[-1].quantity) - _LABEL_INSET)}" '
        f'y="{_coordinate(vertical(treads[-1].cost_bps) - _LABEL_LIFT)}">'
        f"{escape(_format_bps(treads[-1].cost_bps))}</text>"
    )
    middle = treads[len(treads) // 2]
    quantity_ticks = "".join(
        f'<text class="tick" text-anchor="{anchor}" x="{_coordinate(horizontal(quantity))}" '
        f'y="{_coordinate(_TICK_LABEL_BASELINE)}">{escape(str(quantity))}</text>'
        for quantity, anchor in (
            (treads[0].quantity, "start"),
            (middle.quantity, "middle"),
            (treads[-1].quantity, "end"),
        )
    )
    label = (
        f"{staircase.segment_value} round-trip cost in basis points against quantity: "
        f"{_format_bps(treads[0].cost_bps)} bps at {treads[0].quantity} units, falling to "
        f"{_format_bps(treads[-1].cost_bps)} bps at {treads[-1].quantity} units"
    )
    return (
        f'<svg viewBox="0 0 {_coordinate(_VIEW_WIDTH)} {_coordinate(_VIEW_HEIGHT)}" '
        f'role="img" aria-label="{escape(label)}">'
        f"{gridlines}"
        f'<line class="axis-rule" x1="{_coordinate(_PLOT_LEFT)}" '
        f'y1="{_coordinate(_PLOT_TOP)}" x2="{_coordinate(_PLOT_LEFT)}" '
        f'y2="{_coordinate(_PLOT_BOTTOM)}"></line>'
        f'<path class="step" d="{escape(" ".join(path))}"></path>'
        f"{marks}{direct_labels}{quantity_ticks}"
        f'<text class="tick" text-anchor="middle" '
        f'x="{_coordinate((_PLOT_LEFT + _PLOT_RIGHT) / 2)}" '
        f'y="{_coordinate(_VIEW_HEIGHT - Decimal(4))}">quantity (units)</text>'
        f"</svg>"
    )


def _staircase_table(staircase: CostStaircase) -> str:
    """The chart's table view — the same treads, readable without seeing the shape."""
    body = "".join(
        f"<tr><td class=figure>{escape(str(tread.quantity))}</td>"
        f"<td class=figure>{escape(_format_bps(tread.cost_bps))}</td></tr>"
        for tread in staircase.treads
    )
    return (
        f"<details><summary>{escape(staircase.segment_value)} treads as a table</summary>"
        f"<table><thead><tr><th class=figure>quantity</th>"
        f"<th class=figure>bps</th></tr></thead><tbody>{body}</tbody></table></details>"
    )


def _staircase_figure(staircase: CostStaircase) -> str:
    treads = staircase.treads
    if not treads:
        return ""
    caption = (
        f"Starts at {_format_bps(treads[0].cost_bps)} bps on {treads[0].quantity} units and "
        f"ends at {_format_bps(treads[-1].cost_bps)} bps on {treads[-1].quantity} units — "
        f"flat charges diluting over a larger base, in steps because the brokerage schedule "
        f"is itself piecewise."
    )
    # Not a defect badge. Cost in bps must never rise with quantity, but the comparison is
    # exact and the broker rounds a levy to a whole rupee, which moves the far decimals — so
    # the SIZE of the rise is shown and the reader is told which explanation it fits, rather
    # than being handed a red flag on arithmetic noise.
    non_monotone = (
        ""
        if staircase.is_monotonically_cheaper
        else '<p><span class="badge badge-warning">NON-MONOTONE</span> cost rises by up to '
        f"{escape(_format_rise_bps(staircase.largest_cost_rise_bps))} bps somewhere on this "
        f"curve. A rise this far below the cost itself is the broker's rounding quantum; a "
        f"rise comparable to the cost would mean the brokerage pieces overlap.</p>"
    )
    return (
        f'<div class="chart"><h3>{escape(staircase.segment_value)} '
        f'<span class="muted">· lot {escape(str(staircase.lot_size))}</span></h3>'
        f"{non_monotone}<figure>{_staircase_svg(staircase)}"
        f"<figcaption>{escape(caption)}</figcaption></figure>"
        f"{_staircase_table(staircase)}</div>"
    )


# --------------------------------------------------------------------------- F01 charts


def _bar_path(
    x_start: Decimal, x_end: Decimal, y_top: Decimal, thickness: Decimal, *, round_data_end: bool
) -> str:
    """A bar as a path: square where it meets the baseline, rounded at the data end.

    The rounding is dropped on a segment narrower than the radius rather than scaled down —
    a 1px-wide bar with a 4px corner is a blob, and a blob is a value the reader cannot measure.
    """
    y_bottom = y_top + thickness
    if not round_data_end or x_end - x_start <= _BAR_CORNER_RADIUS:
        return (
            f"M {_coordinate(x_start)} {_coordinate(y_top)} H {_coordinate(x_end)} "
            f"V {_coordinate(y_bottom)} H {_coordinate(x_start)} Z"
        )
    radius = _BAR_CORNER_RADIUS
    return (
        f"M {_coordinate(x_start)} {_coordinate(y_top)} "
        f"H {_coordinate(x_end - radius)} "
        f"A {_coordinate(radius)} {_coordinate(radius)} 0 0 1 "
        f"{_coordinate(x_end)} {_coordinate(y_top + radius)} "
        f"V {_coordinate(y_bottom - radius)} "
        f"A {_coordinate(radius)} {_coordinate(radius)} 0 0 1 "
        f"{_coordinate(x_end - radius)} {_coordinate(y_bottom)} "
        f"H {_coordinate(x_start)} Z"
    )


def _hurdle_part_values(row: HurdleDecompositionRow) -> tuple[Decimal, ...]:
    """The three stacked parts, in `_HURDLE_STACK_PARTS` order. They sum to `required_bps`."""
    return (row.statutory_bps, row.execution_point_bps, row.uncertainty_bps)


def _hurdle_ladder_svg(ladder: HurdleSizeLadder, ceiling_bps: Decimal) -> str:
    """One instrument's sizes as stacked bars, on the axis every ladder on the page shares."""
    rows = ladder.rows
    if not rows or ceiling_bps <= 0:
        return ""
    view_height = _HURDLE_TOP_PADDING + _HURDLE_ROW_HEIGHT * len(rows) + _HURDLE_AXIS_BAND
    plot_bottom = _HURDLE_TOP_PADDING + _HURDLE_ROW_HEIGHT * len(rows)

    def horizontal(value: Decimal) -> Decimal:
        return _interpolate(value, Decimal(0), ceiling_bps, _HURDLE_PLOT_LEFT, _HURDLE_PLOT_RIGHT)

    gridlines = "".join(
        f'<line class="gridline" x1="{_coordinate(horizontal(ceiling_bps * fraction))}" '
        f'y1="{_coordinate(_HURDLE_TOP_PADDING)}" '
        f'x2="{_coordinate(horizontal(ceiling_bps * fraction))}" '
        f'y2="{_coordinate(plot_bottom)}"></line>'
        f'<text class="tick" text-anchor="middle" '
        f'x="{_coordinate(horizontal(ceiling_bps * fraction))}" '
        f'y="{_coordinate(plot_bottom + Decimal(14))}">'
        f"{escape(_format_bps(ceiling_bps * fraction))}</text>"
        for fraction in _GRIDLINE_FRACTIONS
    )
    marks: list[str] = []
    for index, row in enumerate(rows):
        bar_top = (
            _HURDLE_TOP_PADDING
            + _HURDLE_ROW_HEIGHT * index
            + (_HURDLE_ROW_HEIGHT - _HURDLE_BAR_THICKNESS) / 2
        )
        centre = bar_top + _HURDLE_BAR_THICKNESS / 2
        marks.append(
            f'<text class="row-label" text-anchor="end" '
            f'x="{_coordinate(_HURDLE_PLOT_LEFT - _LABEL_GAP)}" '
            f'y="{_coordinate(centre + _TEXT_CENTRING_LIFT)}">'
            f"{escape(_format_count(row.quantity))}</text>"
        )
        drawn = [
            (css_class, word, value)
            for (css_class, word), value in zip(
                _HURDLE_STACK_PARTS, _hurdle_part_values(row), strict=True
            )
            if value > 0
        ]
        cumulative = Decimal(0)
        for position, (css_class, word, value) in enumerate(drawn):
            start_x = horizontal(cumulative)
            cumulative += value
            end_x = horizontal(cumulative)
            is_data_end = position == len(drawn) - 1
            if not is_data_end:
                end_x -= _SEGMENT_GAP
            if end_x <= start_x:
                continue
            path = _bar_path(
                start_x, end_x, bar_top, _HURDLE_BAR_THICKNESS, round_data_end=is_data_end
            )
            marks.append(
                f"<g><title>{escape(_format_count(row.quantity))} units — {escape(word)} "
                f"{escape(_format_bps(value))} bps of a {escape(_format_bps(row.required_bps))} "
                f'bps hurdle</title><path class="{css_class}" d="{path}"></path></g>'
            )
        # One label per bar, at the tip, outside the mark: the total the signal must clear.
        marks.append(
            f'<text class="bar-label" text-anchor="start" '
            f'x="{_coordinate(horizontal(row.required_bps) + _LABEL_GAP)}" '
            f'y="{_coordinate(centre + _TEXT_CENTRING_LIFT)}">'
            f"{escape(_format_bps(row.required_bps))}</text>"
        )
    label = (
        f"{ladder.label}: required hurdle in basis points at {len(rows)} sizes, each bar split "
        f"into statutory charges, expected execution cost and the uncertainty margin"
    )
    return (
        f'<svg viewBox="0 0 {_coordinate(_VIEW_WIDTH)} {_coordinate(view_height)}" '
        f'role="img" aria-label="{escape(label)}">'
        f"{gridlines}"
        f'<line class="axis-rule" x1="{_coordinate(_HURDLE_PLOT_LEFT)}" '
        f'y1="{_coordinate(_HURDLE_TOP_PADDING)}" x2="{_coordinate(_HURDLE_PLOT_LEFT)}" '
        f'y2="{_coordinate(plot_bottom)}"></line>'
        f"{''.join(marks)}"
        f'<text class="tick" text-anchor="middle" '
        f'x="{_coordinate((_HURDLE_PLOT_LEFT + _HURDLE_PLOT_RIGHT) / 2)}" '
        f'y="{_coordinate(view_height - Decimal(4))}">required hurdle (bps), by quantity</text>'
        f"</svg>"
    )


def _hurdle_ladder_table(ladder: HurdleSizeLadder) -> str:
    """The stack's table view — every number in the chart, plus the two the chart cannot hold."""
    body = "".join(
        f"<tr><td class=figure>{escape(_format_count(row.quantity))}</td>"
        f"<td>{escape(row.verdict_value.upper())}</td>"
        f"<td class=figure>{escape(_format_bps(row.statutory_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.execution_point_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.uncertainty_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.required_bps))}</td>"
        f"<td class=figure>{escape(_format_percent(row.uncertainty_percent_of_required))}</td>"
        f"<td class=figure>{escape(_format_bps(row.round_trip_spread_bps))}</td>"
        f"<td>{'extrapolated' if row.is_execution_censored else 'within book'}</td></tr>"
        for row in ladder.rows
    )
    return (
        f"<details><summary>{escape(ladder.label)} hurdles as a table</summary>"
        f"<table><thead><tr><th class=figure>quantity</th><th>verdict</th>"
        f"<th class=figure>statutory</th><th class=figure>execution</th>"
        f"<th class=figure>margin</th><th class=figure>required</th>"
        f"<th class=figure>margin share</th><th class=figure>spread, both legs</th>"
        f"<th>depth</th></tr></thead><tbody>{body}</tbody></table></details>"
    )


def _hurdle_ladder_figure(ladder: HurdleSizeLadder, ceiling_bps: Decimal) -> str:
    rows = ladder.rows
    if not rows:
        return ""
    widening = ladder.uncertainty_widening_bps
    if widening is None:
        widening_note = (
            "Priced at one size only, so this card cannot show whether the margin widens — one "
            "point is not a trend, and it is not being reported as a flat one."
        )
    elif widening > 0:
        widening_note = (
            f"The margin is {_format_bps(widening)} bps WIDER at "
            f"{_format_count(rows[-1].quantity)} units than at "
            f"{_format_count(rows[0].quantity)} — nobody chose that; it is what the execution "
            f"estimate stops being sure of once the order outgrows the visible book."
        )
    else:
        widening_note = (
            f"The margin does not widen across these sizes "
            f"({_format_bps(widening)} bps end to end), which is what a liquid instrument well "
            f"inside the visible book looks like."
        )
    depth_note = (
        " At least one size here exceeds the visible book, so its impact term is extrapolated "
        "rather than walked — that is exactly where the margin is supposed to grow."
        if ladder.has_censored_size
        else ""
    )
    caption = (
        "Bars are cumulative and end at the hurdle the signal must clear. The last part of "
        "each is not a cost at all: it is what the execution estimate does not know, and it is "
        "the entire safety margin this gate has."
    )
    return (
        f'<div class="chart"><h3>{escape(ladder.label)}</h3>'
        f'<p class="ladder-note">{escape(widening_note + depth_note)}</p>'
        f"<figure>{_hurdle_ladder_svg(ladder, ceiling_bps)}"
        f"<figcaption>{escape(caption)}</figcaption></figure>"
        f"{_hurdle_ladder_table(ladder)}</div>"
    )


_HURDLE_LEGEND = (
    '<p class="legend">'
    '<span><i class="swatch swatch-1"></i>statutory charges, from circulars</span>'
    '<span><i class="swatch swatch-2"></i>execution cost, expected from the book</span>'
    '<span><i class="swatch swatch-3"></i>uncertainty margin — the pessimistic end</span>'
    "</p>"
)


def _decade_ticks(low: Decimal, high: Decimal) -> tuple[Decimal, ...]:
    """Powers of ten inside the domain — the only tick values a log axis can be read off."""
    if low <= 0 or high <= low:
        return ()
    first = int(low.log10().to_integral_value(rounding=ROUND_CEILING))
    last = int(high.log10().to_integral_value(rounding=ROUND_FLOOR))
    return tuple(_DECADE**exponent for exponent in range(first, last + 1))


def _floor_strip_svg(rows: Sequence[SegmentEdgeFloorRow]) -> str:
    """Each segment's measured hurdle range, with the floor and its own error band on it."""
    if not rows:
        return ""
    values = [
        value
        for row in rows
        for value in (
            row.cheapest_hurdle_bps,
            row.floor_bps,
            row.median_hurdle_bps,
            row.dearest_hurdle_bps,
            row.floor_lower_bps,
            row.floor_upper_bps,
        )
        if value > 0
    ]
    if not values:
        return ""
    low, high = min(values), max(values)
    view_height = _FLOOR_TOP_PADDING + _FLOOR_ROW_HEIGHT * len(rows) + _FLOOR_AXIS_BAND
    plot_bottom = _FLOOR_TOP_PADDING + _FLOOR_ROW_HEIGHT * len(rows)

    def horizontal(value: Decimal) -> Decimal:
        """Log position, clamped into the plot so a zero-clamped band end cannot escape it."""
        if value <= 0:
            return _FLOOR_PLOT_LEFT
        placed = _interpolate(
            value.ln(), low.ln(), high.ln(), _FLOOR_PLOT_LEFT, _FLOOR_PLOT_RIGHT
        )
        return max(_FLOOR_PLOT_LEFT, min(_FLOOR_PLOT_RIGHT, placed))

    gridlines = "".join(
        f'<line class="gridline" x1="{_coordinate(horizontal(tick))}" '
        f'y1="{_coordinate(_FLOOR_TOP_PADDING)}" x2="{_coordinate(horizontal(tick))}" '
        f'y2="{_coordinate(plot_bottom)}"></line>'
        f'<text class="tick" text-anchor="middle" x="{_coordinate(horizontal(tick))}" '
        f'y="{_coordinate(plot_bottom + Decimal(14))}">{escape(_format_bps(tick))}</text>'
        for tick in _decade_ticks(low, high)
    )
    marks: list[str] = []
    for index, row in enumerate(rows):
        centre = _FLOOR_TOP_PADDING + _FLOOR_ROW_HEIGHT * index + _FLOOR_ROW_HEIGHT / 2
        track_left = horizontal(row.cheapest_hurdle_bps)
        track_right = horizontal(row.dearest_hurdle_bps)
        band_left = horizontal(row.floor_lower_bps)
        band_right = horizontal(row.floor_upper_bps)
        floor_x = horizontal(row.floor_bps)
        marks.append(
            f'<text class="row-label" text-anchor="end" '
            f'x="{_coordinate(_FLOOR_PLOT_LEFT - _LABEL_GAP)}" '
            f'y="{_coordinate(centre)}">{escape(row.segment_value)}</text>'
            f'<text class="tick" text-anchor="end" '
            f'x="{_coordinate(_FLOOR_PLOT_LEFT - _LABEL_GAP)}" '
            # Abbreviated: the gutter is one label wide, and "170 instruments" set at chart
            # type overruns the viewBox and loses its first characters to the clip.
            f'y="{_coordinate(centre + Decimal(14))}">'
            f"n={escape(_format_count(row.instrument_count))}</text>"
            f'<rect class="range-track" x="{_coordinate(track_left)}" '
            f'y="{_coordinate(centre - _FLOOR_TRACK_THICKNESS / 2)}" '
            f'width="{_coordinate(max(Decimal(1), track_right - track_left))}" '
            f'height="{_coordinate(_FLOOR_TRACK_THICKNESS)}" rx="4"></rect>'
        )
        if band_right > band_left:
            marks.append(
                f'<rect class="floor-band" x="{_coordinate(band_left)}" '
                f'y="{_coordinate(centre - _FLOOR_BAND_THICKNESS / 2)}" '
                f'width="{_coordinate(band_right - band_left)}" '
                f'height="{_coordinate(_FLOOR_BAND_THICKNESS)}" rx="3"></rect>'
            )
        marks.append(
            f'<circle class="range-dot" cx="{_coordinate(track_left)}" '
            f'cy="{_coordinate(centre)}" r="{_coordinate(_FLOOR_MARKER_RADIUS)}"></circle>'
            f'<circle class="range-ring" cx="{_coordinate(horizontal(row.median_hurdle_bps))}" '
            f'cy="{_coordinate(centre)}" r="{_coordinate(_FLOOR_MARKER_RADIUS)}"></circle>'
            f'<circle class="range-dot" cx="{_coordinate(track_right)}" '
            f'cy="{_coordinate(centre)}" r="{_coordinate(_FLOOR_MARKER_RADIUS)}"></circle>'
            f'<line class="floor-rule" x1="{_coordinate(floor_x)}" '
            f'y1="{_coordinate(centre - _FLOOR_RULE_HALF_HEIGHT)}" '
            f'x2="{_coordinate(floor_x)}" '
            f'y2="{_coordinate(centre + _FLOOR_RULE_HALF_HEIGHT)}"></line>'
            # The floor above the mark, the two extremes below it: three labels that cannot
            # collide with each other however close the values sit on a log axis.
            f'<text class="bar-label" text-anchor="middle" x="{_coordinate(floor_x)}" '
            f'y="{_coordinate(centre - _FLOOR_LABEL_LIFT)}">'
            f"{escape(_format_bps(row.floor_bps))}</text>"
            # Dropped when the floor IS the cheapest instrument, which is what a single-rank
            # quantile means: printing the same number twice on one row reads as two findings.
            + (
                ""
                if row.cheapest_hurdle_bps == row.floor_bps
                else f'<text class="tick" text-anchor="start" x="{_coordinate(track_left)}" '
                f'y="{_coordinate(centre + _FLOOR_LABEL_DROP)}">'
                f"{escape(_format_bps(row.cheapest_hurdle_bps))}</text>"
            )
            + f'<text class="tick" text-anchor="end" x="{_coordinate(track_right)}" '
            f'y="{_coordinate(centre + _FLOOR_LABEL_DROP)}">'
            f"{escape(_format_bps(row.dearest_hurdle_bps))}</text>"
            f"<g><title>{escape(row.description)}</title>"
            f'<rect class="hit" x="{_coordinate(_FLOOR_PLOT_LEFT)}" '
            f'y="{_coordinate(centre - _FLOOR_ROW_HEIGHT / 2)}" '
            f'width="{_coordinate(_FLOOR_PLOT_RIGHT - _FLOOR_PLOT_LEFT)}" '
            f'height="{_coordinate(_FLOOR_ROW_HEIGHT)}"></rect></g>'
        )
    label = (
        "Measured hurdle range per segment on a logarithmic basis-point axis: cheapest "
        "instrument, the screening floor with its standard-error band, the median, and the "
        "dearest instrument"
    )
    return (
        f'<svg viewBox="0 0 {_coordinate(_VIEW_WIDTH)} {_coordinate(view_height)}" '
        f'role="img" aria-label="{escape(label)}">'
        f"{gridlines}{''.join(marks)}"
        f'<text class="tick" text-anchor="middle" '
        f'x="{_coordinate((_FLOOR_PLOT_LEFT + _FLOOR_PLOT_RIGHT) / 2)}" '
        f'y="{_coordinate(view_height - Decimal(4))}">'
        f"measured round-trip hurdle (bps) — LOG scale</text>"
        f"</svg>"
    )


_FLOOR_LEGEND = (
    '<p class="legend">'
    '<span><svg viewBox="0 0 14 14"><circle class="range-dot" cx="7" cy="7" r="4"></circle>'
    "</svg>cheapest and dearest instrument</span>"
    '<span><svg viewBox="0 0 14 14"><line class="floor-rule" x1="7" y1="1" x2="7" y2="13">'
    "</line></svg>screening floor</span>"
    '<span><svg viewBox="0 0 14 14"><rect class="floor-band" x="0" y="3" width="14" '
    'height="8" rx="2"></rect></svg>floor ±1 standard error</span>'
    '<span><svg viewBox="0 0 14 14"><circle class="range-ring" cx="7" cy="7" r="4"></circle>'
    "</svg>median instrument</span>"
    "</p>"
)


def _precondition_bars_svg(rows: Sequence[PreconditionFailureRow]) -> str:
    """Four named checks ranked by how often each one killed a ticket. One series, so no legend."""
    if not rows:
        return ""
    ceiling = max((Decimal(row.failure_count) for row in rows), default=Decimal(0))
    view_height = (
        _PRECONDITION_TOP_PADDING + _PRECONDITION_ROW_HEIGHT * len(rows) + _PRECONDITION_AXIS_BAND
    )
    plot_bottom = _PRECONDITION_TOP_PADDING + _PRECONDITION_ROW_HEIGHT * len(rows)

    def horizontal(value: Decimal) -> Decimal:
        if ceiling <= 0:
            return _PRECONDITION_PLOT_LEFT
        return _interpolate(
            value, Decimal(0), ceiling, _PRECONDITION_PLOT_LEFT, _PRECONDITION_PLOT_RIGHT
        )

    marks: list[str] = []
    for index, row in enumerate(rows):
        bar_top = (
            _PRECONDITION_TOP_PADDING
            + _PRECONDITION_ROW_HEIGHT * index
            + (_PRECONDITION_ROW_HEIGHT - _PRECONDITION_BAR_THICKNESS) / 2
        )
        centre = bar_top + _PRECONDITION_BAR_THICKNESS / 2
        end_x = horizontal(Decimal(row.failure_count))
        marks.append(
            f'<text class="row-label" text-anchor="end" '
            f'x="{_coordinate(_PRECONDITION_PLOT_LEFT - _LABEL_GAP)}" '
            f'y="{_coordinate(centre + _TEXT_CENTRING_LIFT)}">'
            f"{escape(row.precondition_name)}</text>"
        )
        if end_x > _PRECONDITION_PLOT_LEFT:
            path = _bar_path(
                _PRECONDITION_PLOT_LEFT,
                end_x,
                bar_top,
                _PRECONDITION_BAR_THICKNESS,
                round_data_end=True,
            )
            marks.append(
                f"<g><title>{escape(row.precondition_name)} failed "
                f"{escape(_format_count(row.failure_count))} of "
                f"{escape(_format_count(row.evaluated_count))} tickets</title>"
                f'<path class="bar" d="{path}"></path></g>'
            )
        marks.append(
            f'<text class="bar-label" text-anchor="start" '
            f'x="{_coordinate(end_x + _LABEL_GAP)}" '
            f'y="{_coordinate(centre + _TEXT_CENTRING_LIFT)}">'
            f"{escape(_format_count(row.failure_count))}</text>"
        )
    label = "Ticket precondition failures by name, most frequent first"
    return (
        f'<svg viewBox="0 0 {_coordinate(_VIEW_WIDTH)} {_coordinate(view_height)}" '
        f'role="img" aria-label="{escape(label)}">'
        f'<line class="axis-rule" x1="{_coordinate(_PRECONDITION_PLOT_LEFT)}" '
        f'y1="{_coordinate(_PRECONDITION_TOP_PADDING)}" '
        f'x2="{_coordinate(_PRECONDITION_PLOT_LEFT)}" '
        f'y2="{_coordinate(plot_bottom)}"></line>'
        f"{''.join(marks)}"
        f'<text class="tick" text-anchor="middle" '
        f'x="{_coordinate((_PRECONDITION_PLOT_LEFT + _PRECONDITION_PLOT_RIGHT) / 2)}" '
        f'y="{_coordinate(view_height - Decimal(4))}">tickets killed by this check</text>'
        f"</svg>"
    )


# --------------------------------------------------------------------------- table rows


def _grade_badge(grade: EvidenceGrade | None) -> str:
    if grade is None:
        return '<span class="muted">—</span>'
    badge_class, word = _GRADE_BADGES[grade]
    return f'<span class="badge {badge_class}">{escape(word)}</span>'


def _segment_row(row: SegmentCostRow, cost_bps_ceiling: Decimal) -> str:
    if not row.is_priced:
        return (
            f"<tr><td>{escape(row.segment_value)}</td>"
            f'<td><span class="badge badge-critical">REFUSED</span></td>'
            f'<td class=reason colspan="5">{escape(row.refusal_reason)}</td></tr>'
        )
    viable = (
        f"{row.minimum_viable_quantity} units"
        if row.minimum_viable_quantity is not None
        else f'<span class="muted">none within {escape(_format_bps(cost_bps_ceiling))} bps</span>'
    )
    return (
        f"<tr><td>{escape(row.segment_value)}</td>"
        f'<td><span class="badge badge-good">priced</span></td>'
        f"<td class=figure>{escape(_format_bps(row.round_trip_cost_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.breakeven_move_bps))}</td>"
        f"<td class=figure>{escape(str(row.representative_quantity))}</td>"
        f"<td>{viable}</td>"
        f"<td>{_grade_badge(row.weakest_evidence_grade)}</td></tr>"
    )


def _reconciliation_row(row: ComponentReconciliationRow) -> str:
    badge_class, word = _VERDICT_BADGES[row.verdict]
    return (
        f"<tr><td>{escape(row.component_value)}</td>"
        f"<td>{escape(row.segment_value)}</td>"
        f'<td><span class="badge {badge_class}">{escape(word)}</span></td>'
        f"<td class=figure>{escape(str(row.observation_count))}</td>"
        f"<td class=figure>{escape(_format_residual_in_paise(row.mean_residual_paise))}</td>"
        f"<td class=reason>{escape(row.explanation)}</td></tr>"
    )


def _refused_era_row(row: RefusedEraRow) -> str:
    rate_from = row.rate_known_from.isoformat() if row.rate_known_from is not None else "no rate"
    # Only a ONE-SIDED gap earns a badge. Where the levy and its first rate begin together
    # there is nothing to flag, and badging it would spend the reader's attention on the rows
    # that are not the finding.
    binds = (
        f'<span class="muted">{escape(row.binding_constraint)}</span>'
        if row.rate_known_from == row.structure_known_from
        else f'<span class="badge badge-warning">{escape(row.binding_constraint)}</span>'
    )
    return (
        f"<tr><td>{escape(row.component_value)}</td>"
        f"<td>{escape(row.earliest_priceable_date.isoformat())}</td>"
        f"<td>{binds}</td>"
        f"<td>{escape(row.structure_known_from.isoformat())}</td>"
        f"<td>{escape(rate_from)}</td>"
        f"<td class=reason>{escape(row.reason)}</td></tr>"
    )


_FLOOR_EVIDENCE_BADGES: dict[FloorEvidenceVerdict, tuple[str, str]] = {
    FloorEvidenceVerdict.SINGLE_OBSERVATION: ("badge-critical", "SINGLE OBSERVATION"),
    FloorEvidenceVerdict.OVERLAPS_MEDIAN: ("badge-warning", "OVERLAPS MEDIAN"),
    FloorEvidenceVerdict.RESOLVED: ("badge-good", "RESOLVED"),
}


def _edge_floor_row(row: SegmentEdgeFloorRow) -> str:
    badge_class, word = _FLOOR_EVIDENCE_BADGES[row.evidence_verdict]
    return (
        f"<tr><td>{escape(row.segment_value)}</td>"
        f"<td>{escape(row.session_date.isoformat())}</td>"
        f"<td class=figure>{escape(_format_count(row.instrument_count))}</td>"
        f"<td class=figure>{escape(_format_count(row.supporting_instrument_rank))}</td>"
        f"<td class=figure>{escape(_format_bps(row.cheapest_hurdle_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.floor_bps))}</td>"
        f"<td class=figure>±{escape(_format_bps(row.floor_uncertainty_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.median_hurdle_bps))}</td>"
        f"<td class=figure>{escape(_format_bps(row.dearest_hurdle_bps))}</td>"
        f'<td><span class="badge {badge_class}">{escape(word)}</span></td>'
        f"<td class=reason>{escape(row.description)}</td></tr>"
    )


def _precondition_failure_table_row(row: PreconditionFailureRow) -> str:
    reason = row.example_failure_reason or "never failed on the decisions shown here"
    return (
        f"<tr><td>{escape(row.precondition_name)}</td>"
        f"<td class=figure>{escape(_format_count(row.failure_count))}</td>"
        f"<td class=figure>{escape(_format_count(row.evaluated_count))}</td>"
        f"<td class=figure>{escape(_format_percent(row.failure_percent_of_evaluations))}</td>"
        f"<td class=reason>{escape(reason)}</td></tr>"
    )


# --------------------------------------------------------------------------- F01 sections


def _empty_note(sentence: str) -> str:
    """An explicit 'not supplied', never a blank space that reads as 'nothing was found'."""
    return f'<div class="note muted">{escape(sentence)}</div>'


def _hurdle_tiles(state: TransactionCostSurfaceState) -> str:
    dearest = state.dearest_hurdle_row
    widest = state.widest_uncertainty_row
    if dearest is None or widest is None:
        return ""
    return (
        f'<div class="tiles">'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_bps(dearest.required_bps))}</div>"
        f'<div class="tile-label">dearest hurdle to clear, bps '
        f"({escape(dearest.trading_symbol)} at {escape(_format_count(dearest.quantity))} "
        f"units)</div></div>"
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_percent(dearest.uncertainty_percent_of_required))}</div>"
        f'<div class="tile-label">of that hurdle is margin, not expected cost</div></div>'
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_bps(widest.uncertainty_bps))}</div>"
        f'<div class="tile-label">widest margin anywhere, bps '
        f"({escape(widest.trading_symbol)} at {escape(_format_count(widest.quantity))} "
        f"units)</div></div>"
        f'<div class="tile"><div class="tile-value">'
        f"{escape(_format_count(state.censored_hurdle_count))}</div>"
        f'<div class="tile-label">tickets priced beyond the visible book</div></div>'
        f"</div>"
    )


def _hurdle_section(state: TransactionCostSurfaceState) -> str:
    """The hurdle, decomposed — and the one section that explains where the margin comes from."""
    if not state.hurdle_ladders:
        # Two different absences, and they must not share a sentence. Nothing supplied means the
        # page was built without the gate; everything unpriced means the gate ran and could not
        # form an opinion, which is a finding rather than a gap in the page.
        census = state.verdict_census
        if census is None:
            return _empty_note(
                "No gate decision was supplied to this page, so there is no hurdle to "
                "decompose. This is not a claim that nothing was rejected: the page was built "
                "without the gate, and it says so rather than drawing an empty chart that "
                "would read as calm."
            )
        return _empty_note(
            f"{_format_count(census.evaluated_count)} decisions were supplied and not one of "
            f"them could be priced, so there is no hurdle to decompose. That is an absence of "
            f"judgement, not an absence of cost — see the verdict panel below."
        )
    ceiling = state.hurdle_ceiling_bps
    cards = "".join(
        _hurdle_ladder_figure(ladder, ceiling) for ladder in state.hurdle_ladders
    )
    return (
        f"{_hurdle_tiles(state)}{_HURDLE_LEGEND}"
        f'<p class="sub">Every ladder is drawn on the SAME axis, ending at '
        f"{escape(_format_bps(ceiling))} bps, so two cards can be compared directly. Sizes run "
        f"upward inside a card and the dearest instrument comes first between cards.</p>"
        f'<div class="charts">{cards}</div>'
    )


def _floor_section(state: TransactionCostSurfaceState) -> str:
    if not state.edge_floor_rows:
        return _empty_note(
            "No segment floor was supplied, so no segment can be screened out cheaply here. A "
            "missing floor is not an open door: `L1.04` itself refuses rather than defaulting, "
            "and this page reports the absence for the same reason."
        )
    rows = "".join(_edge_floor_row(row) for row in state.edge_floor_rows)
    return (
        f'{_FLOOR_LEGEND}<div class="panel">'
        f'<div class="plot">{_floor_strip_svg(state.edge_floor_rows)}</div>'
        f"<table><thead><tr><th>Segment</th><th>Session</th>"
        f"<th class=figure>instruments</th><th class=figure>behind the floor</th>"
        f"<th class=figure>cheapest</th><th class=figure>floor</th>"
        f"<th class=figure>±1 s.e.</th><th class=figure>median</th>"
        f"<th class=figure>dearest</th><th>Evidence</th><th>As derived</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _verdict_section(state: TransactionCostSurfaceState) -> str:
    """Three judgements in a row, and the fourth count deliberately kept out of it."""
    census = state.verdict_census
    if census is None:
        return _empty_note(
            "No gate decision was supplied, so there is no verdict to count. Zero vetoes out of "
            "zero evaluations and zero out of a thousand are opposite findings, and this page "
            "will not render the first as though it were the second."
        )
    judgements = (
        (GateVerdict.PASS, census.pass_count, "cleared the hurdle at the proposed size"),
        (GateVerdict.RESIZE, census.resize_count, "cleared only at a smaller, solved-for size"),
        (GateVerdict.VETO, census.veto_count, "priced, and judged not worth taking"),
    )
    tiles = "".join(
        f'<div class="tile"><div class="tile-value">{escape(_format_count(count))}</div>'
        f'<div class="tile-label">'
        f'<span class="badge {_GATE_VERDICT_BADGES[verdict][0]}">'
        f"{escape(_GATE_VERDICT_BADGES[verdict][1])}</span> {escape(sentence)}</div></div>"
        for verdict, count, sentence in judgements
    )
    unpriceable_class, unpriceable_word = _GATE_VERDICT_BADGES[GateVerdict.UNPRICEABLE]
    return (
        f'<div class="tiles">{tiles}</div>'
        f'<div class="note"><div class="tiles">'
        f'<div class="tile tile-absent"><div class="tile-value">'
        f"{escape(_format_count(census.unpriceable_count))}</div>"
        f'<div class="tile-label"><span class="badge {unpriceable_class}">'
        f"{escape(unpriceable_word)}</span> no opinion could be formed</div></div>"
        f'<div class="tile tile-absent"><div class="tile-value">'
        f"{escape(_format_percent(census.unpriceable_percent_of_evaluations))}</div>"
        f'<div class="tile-label">of all {escape(_format_count(census.evaluated_count))} '
        f"evaluations</div></div></div>"
        f"<p>This count sits outside the row above and wears no status colour, on purpose. A "
        f"veto is a judgement — the trade was priced and is not worth taking. UNPRICEABLE is "
        f"the absence of one: a book that could not be read, a date with no compiled rate, an "
        f"instrument never measured. Adding the two together would produce a rejection total "
        f"that grows every time a data feed breaks, and a system that treats an outage as a "
        f"decision has stopped measuring anything. "
        f"{escape(_format_count(census.judged_count))} of "
        f"{escape(_format_count(census.evaluated_count))} evaluations were actually judged, and "
        f"{escape(_format_count(census.tradeable_count))} of those are tradeable.</p></div>"
    )


def _precondition_section(state: TransactionCostSurfaceState) -> str:
    if not state.precondition_rows:
        census = state.verdict_census
        if census is not None:
            return _empty_note(
                f"None of the {_format_count(census.evaluated_count)} decisions supplied got as "
                f"far as a precondition check — the checks run only once a ticket can be priced "
                f"at all. An empty table here is never 'every ticket passed'."
            )
        return _empty_note(
            "No precondition report reached this page. The four ticket checks run inside the "
            "gate, so this section is empty exactly when the gate was not run — not when every "
            "ticket passed."
        )
    rows = "".join(_precondition_failure_table_row(row) for row in state.precondition_rows)
    caption = (
        "Which economic fact killed the ticket, by name. A denominator error, a ticket too "
        "small to carry a flat charge, a spread wider than the edge and a range narrower than "
        "its own cost are four different problems, and only one of them is fixed by finding a "
        "better signal."
    )
    return (
        f'<div class="panel"><figure class="plot">'
        f"{_precondition_bars_svg(state.precondition_rows)}"
        f"<figcaption>{escape(caption)}</figcaption></figure>"
        f"<table><thead><tr><th>Precondition</th><th class=figure>failed</th>"
        f"<th class=figure>evaluated</th><th class=figure>failure rate</th>"
        f"<th>One real refusal, in its own words</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


# --------------------------------------------------------------------------- the page


def render_transaction_cost_page(state: TransactionCostSurfaceState) -> str:
    """The whole `/costs` surface, self-contained. Pure: no I/O, no engine, no pricing."""
    grade_badge = _grade_badge(state.weakest_evidence_grade)
    cheapest_bps = escape(_format_bps(state.cheapest_achievable_cost_bps))
    ledger_note = (
        "no contract note has been recorded yet, so every component is UNVERIFIED by "
        "construction — the ledger is fully built and arms itself as notes accrue"
        if not state.reconciliation_rows
        else f"{len(state.reconciliation_rows)} components carry observations"
    )
    reconciliation_body = (
        "".join(_reconciliation_row(row) for row in state.reconciliation_rows)
        or f'<tr><td colspan="6" class="muted">{escape(ledger_note)}</td></tr>'
    )
    earliest = state.earliest_priceable_date
    earliest_text = escape(earliest.isoformat()) if earliest is not None else "—"
    # The specific claim is only true when a rule store was supplied. Printing it regardless
    # would have the page assert a rate boundary its own table does not show.
    rate_era_sentence = (
        "The exchange transaction charge is the case that matters: it has been payable since "
        "2004, but until SEBI's 'True to Label' era it was a turnover SLAB whose breakpoints "
        f"are published nowhere in aggregate, so the RATE is refused before {earliest_text} "
        "even though the structure is not. Stamp duty is refused before it was federalised in "
        "2020 for the same reason on the other half."
        if state.has_rate_era_evidence
        else "No rule store was supplied to this page, so the dates below are STRUCTURAL only "
        "— when each levy began to apply, not when its rate was first compiled. The real "
        "boundary is at least this late and may be later; the exchange transaction charge in "
        "particular has applied since 2004 and has no usable rate until the flat-rate era."
    )
    # A heading over an empty grid reads as a chart that failed to draw. It did not: there was
    # nothing priceable to draw, which is a different statement and the reader needs that one.
    staircase_body = "".join(
        _staircase_figure(staircase) for staircase in state.staircases
    ) or (
        '<div class="note muted">No segment could be priced on this date, so there is no '
        "staircase to draw. Every refusal is listed above with the fact it is missing.</div>"
    )
    hurdle_section = _hurdle_section(state)
    floor_section = _floor_section(state)
    verdict_section = _verdict_section(state)
    precondition_section = _precondition_section(state)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transaction costs</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Transaction costs</h1>
<p class="sub">What a round trip costs per segment on {escape(state.priced_on.isoformat())}
through {escape(state.broker)}, and how much the number is worth. A row shown as
<span class="badge badge-critical">REFUSED</span> is not a gap in this page — it is the engine
declining to substitute a rate it has not compiled, which is the only reason the other rows can
be trusted. Costs are basis points of the ENTRY turnover, one-sided, because that is the
fraction of the capital a sizing decision is about to commit.</p>

<div class="tiles">
<div class="tile"><div class="tile-value">{cheapest_bps}</div>
<div class="tile-label">cheapest achievable round trip, bps
({escape(state.cheapest_segment_value)})</div></div>
<div class="tile"><div class="tile-value">{grade_badge}</div>
<div class="tile-label">weakest evidence under any live rate</div></div>
<div class="tile"><div class="tile-value">{escape(str(state.ledger_observation_count))}</div>
<div class="tile-label">reconciliation observations recorded</div></div>
<div class="tile"><div class="tile-value">{escape(str(state.drifting_component_count))}</div>
<div class="tile-label">components DRIFTING from billed charges</div></div>
</div>

<h2>Per segment — worst first, refusals above everything</h2>
<div class="panel">
<table><thead><tr><th>Segment</th><th>State</th><th class=figure>round trip bps</th>
<th class=figure>breakeven bps</th><th class=figure>at quantity</th>
<th>minimum viable quantity</th><th>weakest rate</th></tr></thead>
<tbody>{"".join(_segment_row(row, state.cost_bps_ceiling) for row in state.segment_rows)}</tbody>
</table>
</div>

<h2>The cost staircase — where does it flatten out?</h2>
<div class="charts">
{staircase_body}
</div>

<h2>The hurdle, decomposed — and how much of it is not a cost</h2>
<p class="sub">A signal does not have to beat the expected cost of trading; it has to beat the
PESSIMISTIC end of it. That is the whole safety margin in this system: there is no 1.5x
multiplier anywhere, because a single multiplier is necessarily wrong in both directions at once
— too timid on a liquid instrument at small size, far too brave on an illiquid one at size. The
third part of every bar below is that margin, and it is measured, not chosen: it widens exactly
when the execution estimate stops being able to see the depth it needs.</p>
{hurdle_section}

<h2>Per-segment edge floors — the cheap question, asked first</h2>
<p class="sub">Below a segment's floor there is nothing to discuss, and finding that out costs
one comparison instead of a full costing run per instrument. A floor is a low quantile of the
hurdles measured across the real universe, so it moves when the market, the rates or the
liquidity move, and nobody edits a number. It is a screening bound and never an approval:
clearing it says a trade is CONCEIVABLE somewhere in the segment, never that this trade at this
size is worth taking — only the gate can say that. The band on each floor is its own standard
error, from the number of instruments actually behind the quantile; a wide band is the page
telling you the floor is a number, not yet a property of the segment.</p>
{floor_section}

<h2>What the gate decided</h2>
{verdict_section}

<h2>Which economic fact killed the ticket</h2>
{precondition_section}

<h2>Reconciliation against real contract notes</h2>
<div class="panel">
<table><thead><tr><th>Component</th><th>Segment</th><th>Verdict</th>
<th class=figure>observations</th><th class=figure>mean residual, paise</th>
<th>What the residuals support</th></tr></thead>
<tbody>{reconciliation_body}</tbody></table>
</div>

<h2>What this page CANNOT price</h2>
<div class="note">
<p>Nothing here can be computed before <strong>{earliest_text}</strong>, and every date
earlier than a component's own era start is REFUSED rather than priced from today's rate. A
levy needs both halves before it can be charged — a structural record saying what it applies to
and a compiled rate saying how much — so each row shows both and which one binds.
{rate_era_sentence}
A flat rate applied to a slab era returns a plausible number and a wrong conclusion, and
nothing downstream can tell that it did.</p>
<table><thead><tr><th>Component</th><th>Earliest priceable</th><th>What binds</th>
<th>Structure from</th><th>Rate from</th><th>Why earlier is refused</th>
</tr></thead>
<tbody>{"".join(_refused_era_row(row) for row in state.refused_eras)}</tbody></table>
</div>

<footer>Worst first, deliberately. Colour on this page does one job — status — so evidence
grades, reconciliation verdicts, gate verdicts and floor maturity use the reserved palette and
always carry a word. The exception is the hurdle stack, whose three parts are components of a
sum rather than states, and which therefore uses categorical slots 1-3 with a legend, a labelled
tip and a table of the same numbers. UNPRICEABLE wears no colour at all, because it is the
absence of a judgement rather than a bad one, and it is never added to a rejection total.
{escape(str(state.refused_segment_count))} of
{escape(str(len(state.segment_rows)))} segments are refused outright. Prices and sizes on this
page are the caller's stated assumptions, not measurements: they are parameters of
<code>build_transaction_cost_surface_state</code> precisely so nobody can mistake a display
choice for a fact.</footer>
</body></html>"""
