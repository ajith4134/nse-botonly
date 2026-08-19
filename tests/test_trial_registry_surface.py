"""`L2.01`'s surface — the count every `F06` gate reads, made visible (`R.08`).

The test that carries the design is `test_an_unwritten_registry_is_not_a_search_of_size_zero`.
A page that rendered "0 trials" for a machine that has never recorded one would be the most
flattering number in the system rendered as a fact.
"""

from __future__ import annotations

from nse_algo_trader.dashboard.trial_registry_surface_renderer import render_trial_registry_page
from nse_algo_trader.validation.honest_trial_registry import TrialOutcome

_SPREAD = {
    TrialOutcome.COMPLETED: 40,
    TrialOutcome.ABANDONED: 7,
    TrialOutcome.ERRORED: 12,
    TrialOutcome.DISCARDED: 5,
}


def test_an_unwritten_registry_is_not_a_search_of_size_zero() -> None:
    """`A.41`. "No trials recorded" and "no registry" are different facts, and reading the second
    as the first would hand every gate the most flattering count there is."""
    page = render_trial_registry_page(
        exists=False, cumulative=0, by_outcome=dict.fromkeys(TrialOutcome, 0), broken_at=None
    )
    assert "NOT RECORDED" in page
    assert "not a search of size zero" in page
    assert "absence of a record" in page


def test_the_four_outcomes_are_shown_separately() -> None:
    """One total would hide the half of the population the engine exists to keep: a search reads
    honest only when what it abandoned and discarded is on the page beside what it completed."""
    page = render_trial_registry_page(
        exists=True, cumulative=64, by_outcome=_SPREAD, broken_at=None
    )
    assert "64" in page
    for outcome in TrialOutcome:
        assert outcome.value in page
    assert "40" in page and "12" in page and "5" in page


def test_an_all_completed_registry_is_called_suspicious_not_clean() -> None:
    """The shape of an under-recorded search: a real one abandons, errors and discards. Rendering
    that as a clean sweep would be the page agreeing with the flattering reading."""
    page = render_trial_registry_page(
        exists=True,
        cumulative=40,
        by_outcome={
            TrialOutcome.COMPLETED: 40,
            TrialOutcome.ABANDONED: 0,
            TrialOutcome.ERRORED: 0,
            TrialOutcome.DISCARDED: 0,
        },
        broken_at=None,
    )
    assert "more likely under-recorded than unusually lucky" in page


def test_a_realistic_spread_carries_no_suspicion_note() -> None:
    """The converse, so the warning cannot be decoration that is always on."""
    page = render_trial_registry_page(
        exists=True, cumulative=64, by_outcome=_SPREAD, broken_at=None
    )
    assert "more likely under-recorded" not in page


def test_a_clean_chain_is_not_reported_as_proof_of_completeness() -> None:
    """The claim the engine actually earns, and no more. `verify_chain() is None` means "no casual
    tampering"; anyone with the module and the store can recompute the chain, so a reader who took
    a green tick as proof of completeness would have been misled by the tick."""
    page = render_trial_registry_page(
        exists=True, cumulative=64, by_outcome=_SPREAD, broken_at=None
    )
    assert "No casual tampering detected" in page
    assert "NOT proof of completeness" in page
    assert "not a determined author" in page


def test_a_broken_chain_says_every_gate_above_it_is_invalid() -> None:
    page = render_trial_registry_page(exists=True, cumulative=64, by_outcome=_SPREAD, broken_at=17)
    assert "BROKEN AT SEQUENCE 17" in page
    assert "invalid until the break is explained" in page
    assert "No casual tampering" not in page
