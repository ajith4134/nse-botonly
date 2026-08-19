"""Tests for the rendered-surface honesty check — `A.137`.

Two halves matter equally: it must catch the three defects that actually shipped, and it must NOT
fire on the two correct renders that a first, looser version flagged. A check that fires on correct
output teaches its reader to skim, which costs more than the defect it catches.
"""

from __future__ import annotations

from nse_algo_trader.dashboard.rendered_surface_honesty_check import inspect_rendered_surface


def _rules(html: str) -> set[str]:
    return {finding.rule for finding in inspect_rendered_surface("/x", html)}


# ---------------------------------------------------------------- the defects that shipped


def test_money_printed_to_twenty_two_decimal_places_is_caught() -> None:
    """The live decision-trace page rendered a threshold as Rs 138893.7766666666666666666666."""
    page = "<p>against a threshold of Rs 138893.7766666666666666666666</p>"
    assert "money-precision" in _rules(page)


def test_money_in_scientific_notation_is_caught() -> None:
    """This was the FIRST fix for the precision defect, which is why it has its own rule."""
    assert "money-scientific-notation" in _rules("<p>a threshold of Rs 1.389e+05</p>")


def test_a_clock_time_with_no_timezone_anywhere_on_the_page_is_caught() -> None:
    """`04:10` was a 09:40 IST decision, printed in UTC on a single-exchange dashboard."""
    assert "unlabelled-clock" in _rules("<table><tr><td>04:10</td></tr></table>")


# ---------------------------------------------------------------- the correct renders it must allow


def test_a_zone_labelled_microsecond_timestamp_is_not_flagged() -> None:
    """`/orders` renders `17:33:30.630940+05:30` — a timestamp, correctly labelled.

    A first version of this check flagged it, because the microsecond field matched a
    "too many decimals" rule that was not scoped to money.
    """
    assert _rules("<p>as at 2026-08-17T17:33:30.630940+05:30</p>") == set()


def test_a_genuinely_small_quantity_is_not_flagged() -> None:
    """`/regime` renders `vol=0.000746` — 7.46 basis points.

    Rounding that to two places displays `0.00` and destroys the number. Precision is a defect in
    MONEY, not in every small value.
    """
    assert _rules('<p class="evidence">vol=0.000746 q25=0.001053 q75=0.001973</p>') == set()


def test_money_to_two_places_is_not_flagged() -> None:
    assert _rules("<p>Rs 138,893.78 against Rs 1,00,000.00</p>") == set()


def test_a_clock_time_on_a_page_that_names_its_timezone_is_not_flagged() -> None:
    assert _rules("<p>shown in market time (IST)</p><td>09:40</td>") == set()


# ---------------------------------------------------------------- the check must be able to fail


def test_a_clean_page_produces_no_findings() -> None:
    assert inspect_rendered_surface("/x", "<p>Rs 1,234.56 at 09:40 IST</p>") == []


def test_a_finding_says_why_it_misleads_not_merely_what_matched() -> None:
    """A finding a reader cannot act on gets triaged as a lint nit."""
    (finding,) = inspect_rendered_surface("/x", "<p>Rs 1.2345678</p>")
    assert len(finding.why_it_misleads) > 60
    assert "/x" in finding.describe()
