"""`L5.31`'s surface (`R.08`) — what the pre-trade quality floor admitted, refused, and why.

Engine: `nse_algo_trader.trade_quality.trade_quality_floor_engine`. Spec `docs/research/260`.

**Why this needs a surface.** A floor that only ever admits, or only ever refuses, is broken in a
way no test catches — and the single most useful thing this page shows is the REFUSALS, because
they are the control group. Every card the engine writes is here, admissions and refusals alike,
with the binding floor named. A reader who sees the same floor binding on every row learns that the
other two are decoration.

**Why it is a table and not a chart.** The reader's question is per-row — "why was this one turned
away?" — and each row carries a verdict, a probability, a binding constraint and a reason. That is a
record to be read, not a distribution to be seen. The one genuinely visual element is the verdict
dot, because a verdict has a polarity that a word alone does not put on a side.

**Colour carries no meaning alone, and no new colour is introduced here.** The three verdict steps
reuse the reserved status steps already validated for `bot_maturity_surface_renderer` — the same
house palette, the same protan/deutan/tritan separation — and every dot ships its verdict in text
ink beside it. `UNASSESSABLE` takes the neutral grey deliberately: "no opinion" is not a status, and
giving it one would put a proposal nobody could judge on the same footing as one that was judged.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.trade_quality.trade_quality_evidence_card import (
    QualityVerdict,
    TradeQualityEvidenceCard,
)

_PAGE = """<main>
<h1>Trade-quality floor</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""

_VERDICT_COLOUR: dict[QualityVerdict, str] = {
    QualityVerdict.ADMIT: "#0ca30c",
    QualityVerdict.REFUSE: "#b3261e",
    QualityVerdict.UNASSESSABLE: "#8a8a80",
}
"""Reserved status steps, taken from the same house palette the ladder surface uses.

Nothing new is defined here. A second palette for a second surface is how two pages start meaning
different things by the same colour.
"""

_VERDICT_MEANING: dict[QualityVerdict, str] = {
    QualityVerdict.ADMIT: "clears its binding floor at the stated confidence",
    QualityVerdict.REFUSE: "assessed, and does not clear",
    QualityVerdict.UNASSESSABLE: "no opinion — something needed could not be estimated",
}


def _verdict_cell(verdict: QualityVerdict) -> str:
    dot = (
        f'<span aria-hidden="true" style="display:inline-block;width:.6em;height:.6em;'
        f'border-radius:50%;background:{_VERDICT_COLOUR[verdict]};margin-right:.4em"></span>'
    )
    return f"{dot}{escape(verdict.value.upper())}"


def _card_row(card: TradeQualityEvidenceCard) -> str:
    expectancy = card.gross_expectancy
    confidence = (
        "—" if expectancy is None else f"{expectancy.probability_exceeding_floor:.3f}"
    )
    margin = "—" if card.margin_rupees is None else f"{card.margin_rupees:,.2f}"
    floor = card.binding_floor
    return (
        "<tr>"
        f"<td>{_verdict_cell(card.verdict)}</td>"
        f"<td>{escape(card.bot_identity)}</td>"
        f"<td>{escape(card.trading_symbol)}</td>"
        f"<td>{escape(card.trading_segment.value)}</td>"
        f"<td>{card.scan_breadth}</td>"
        f"<td>{card.calibrated_probability.stated:.3f}"
        f" &rarr; {card.calibrated_probability.calibrated:.3f}</td>"
        f"<td>{card.calibrated_probability.fitted_on_trades}</td>"
        f"<td>{confidence}</td>"
        f"<td>{escape(floor.derivation.value)}</td>"
        f"<td>&#8377;{floor.rupees:,.2f}</td>"
        f"<td>&#8377;{margin}</td>"
        "</tr>"
    )


def render_trade_quality_page(cards: tuple[TradeQualityEvidenceCard, ...]) -> str:
    """The page, measured from the evidence store on every request.

    An empty store is reported as an empty store, not as a clean bill of health: a gate that has
    never assessed anything is not a gate that has admitted everything, and the two must not read
    the same.
    """
    if not cards:
        return _PAGE.format(
            subtitle="no proposal has been assessed yet",
            body=(
                "<p>The evidence store is empty. That is an absence of assessments, not an "
                "absence of problems — until the gate has judged a proposal there is nothing "
                "here to be right or wrong about.</p>"
            ),
            footer="L5.31 &middot; spec docs/research/260",
        )

    counts = {
        verdict: sum(1 for card in cards if card.verdict is verdict) for verdict in QualityVerdict
    }
    binding = {card.binding_floor.derivation.value for card in cards}
    rows = "".join(_card_row(card) for card in cards)
    legend = " &middot; ".join(
        f"{verdict.value.upper()}: {escape(meaning)}"
        for verdict, meaning in _VERDICT_MEANING.items()
    )
    body = (
        "<table>"
        "<thead><tr>"
        "<th>verdict</th><th>bot</th><th>instrument</th><th>segment</th><th>scan</th>"
        "<th>stated &rarr; calibrated</th><th>fitted on</th><th>P(&gt; floor)</th>"
        "<th>binding floor</th><th>floor</th><th>margin</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        f"<p class='legend'>{legend}</p>"
    )
    subtitle = (
        f"{counts[QualityVerdict.ADMIT]} admitted &middot; "
        f"{counts[QualityVerdict.REFUSE]} refused &middot; "
        f"{counts[QualityVerdict.UNASSESSABLE]} unassessable &middot; "
        f"{len(binding)} of 3 floors ever bound"
    )
    return _PAGE.format(
        subtitle=subtitle,
        body=body,
        footer="L5.31 &middot; spec docs/research/260",
    )
