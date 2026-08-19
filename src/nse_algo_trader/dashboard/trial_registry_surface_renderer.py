"""`L2.01`'s surface (`R.08`) — how large the search really was, and whether the record still holds.

Engine: `nse_algo_trader.validation.honest_trial_registry`. Spec: `docs/research/245`.

**Why this needs a surface at all.** The registry's number is the input to every `F06` gate —
Deflated Sharpe deflates by it, PBO resamples over it, Benjamini-Yekutieli corrects for it. A count
nobody looks at is a count nobody notices going wrong, and the failure this engine exists to
prevent is a count that quietly shrinks. So the page shows the four-way outcome split rather than a
single total: a registry where `discarded` and `errored` are zero after a long search is not a
clean search, it is an unrecorded one.

**The chain verdict is stated in the words the engine actually earns.** `verify_chain()` returning
`None` means "no casual tampering", never "provably complete" — a determined author with this
module can recompute the chain, and `docs/research/246` §2 records exactly that. The panel says so,
because a reader who takes a green tick as proof of completeness has been misled by the tick.
"""

from __future__ import annotations

from html import escape

from nse_algo_trader.validation.honest_trial_registry import TrialOutcome

_PAGE = """<main>
<h1>Search size</h1>
<p class="sub">{subtitle}</p>
{body}
<footer>{footer}</footer>
</main>"""


def _stat_tile(value: str, label: str, note: str) -> str:
    return (
        '<div class="tile">'
        f'<div class="tile-value">{escape(value)}</div>'
        f'<div class="tile-label">{escape(label)}</div>'
        f'<div class="tile-note">{escape(note)}</div>'
        "</div>"
    )


_OUTCOME_NOTES: dict[TrialOutcome, str] = {
    TrialOutcome.COMPLETED: "ran to the end and produced a fitness",
    TrialOutcome.ABANDONED: "stopped before it finished — still a hypothesis tested",
    TrialOutcome.ERRORED: "raised; a traceback consumed a hypothesis exactly as a result does",
    TrialOutcome.DISCARDED: "ran, scored, and the score was thrown away",
}


def render_trial_registry_page(
    *,
    exists: bool,
    cumulative: int,
    by_outcome: dict[TrialOutcome, int],
    broken_at: int | None,
) -> str:
    """The page. Every number is passed in, so the renderer cannot disagree with the store.

    Args:
        exists: whether a registry has been written at all. `A.41` — "no trials recorded" and "no
            registry" are different facts, and the second must not render as a clean search of
            size zero, which is the most flattering count there is.
        cumulative: every trial, whatever its outcome.
        by_outcome: the four-way split.
        broken_at: the sequence where the chain first fails, or `None`.
    """
    if not exists:
        return _PAGE.format(
            subtitle="No trial registry has been written on this machine.",
            body=(
                '<div class="panel"><p class="empty">NOT RECORDED. No search has been registered, '
                "so there is no honest cumulative N and no <code>F06</code> gate may be computed. "
                "This is not a search of size zero — it is the absence of a record, and the two "
                "must never be read as the same thing.</p></div>"
            ),
            footer=(
                "Run a search through <code>HonestTrialRegistry</code>, or "
                "<code>scripts/verify_trial_registry_on_real_data.py</code> to reconstruct the "
                "floor the retained trades can prove."
            ),
        )

    unrecorded_shape = (
        '<p class="empty">Every recorded trial completed. After a real search that is unusual '
        "rather than reassuring: a search that abandoned nothing, errored on nothing and "
        "discarded nothing is more likely under-recorded than unusually lucky.</p>"
        if cumulative and by_outcome.get(TrialOutcome.COMPLETED, 0) == cumulative
        else ""
    )

    chain = (
        '<div class="panel"><h2>Chain</h2>'
        '<p class="sub">No casual tampering detected — every link holds and every trial the '
        "witness saw is still present. This is NOT proof of completeness: anyone with this "
        "module and the store can recompute the chain, so the guarantee covers accidental or "
        "sqlite-shell mutation, not a determined author.</p></div>"
        if broken_at is None
        else (
            '<div class="panel"><h2>Chain</h2>'
            f'<p class="empty">BROKEN AT SEQUENCE {broken_at}. A trial was removed, edited or '
            "truncated away. Every gate computed over this count is invalid until the break is "
            "explained — the count is the input to all of them.</p></div>"
        )
    )

    tiles = _stat_tile(
        f"{cumulative:,}",
        "trials",
        "hypotheses tested, whatever the outcome",
    ) + "".join(
        _stat_tile(f"{by_outcome.get(outcome, 0):,}", outcome.value, _OUTCOME_NOTES[outcome])
        for outcome in TrialOutcome
    )

    return _PAGE.format(
        subtitle=(
            "Every gate above this reads the number below. Deflated Sharpe deflates by it, PBO "
            "resamples over it, Benjamini-Yekutieli corrects for it — so an undercount flatters "
            "all of them, and always in the direction that lets a bad strategy through."
        ),
        body=f'<div class="panel"><div class="tiles">{tiles}</div>{unrecorded_shape}</div>{chain}',
        footer=(
            "A trial is one hypothesis tested. Two identical parameterisations tried twice are "
            "two trials — that is exactly what a multiple-testing correction corrects for."
        ),
    )
