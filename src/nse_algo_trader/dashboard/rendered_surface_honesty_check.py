"""Defects that survive a green test suite because nothing LOOKS at the rendered page — `R.08`.

On 2026-08-17 three defects shipped to a live surface with every unit test passing and the palette
validator green (`A.137`):

* decision times printed in **UTC** — `04:10` for a decision taken at 09:40 IST, on a dashboard for
  a single exchange;
* an explained share of **99.98% rendered as "100.0%"**, while the tile beside it reported four
  unexplained — the flattering direction on the one number that must not flatter;
* rupee thresholds printed as `138893.7766666666666666666666`, then as `1.389e+05` after a first
  attempt at fixing it.

None was catchable by a test that did not read the output. The `dataviz` procedure ends with
*"render it and look at it — the validator checks color, not layout"*, and that step is what caught
all three. This module is the part of that step a machine can do every night, so the human eye is
spent on layout and judgement rather than on scanning for decimal places.

**What it deliberately does NOT flag, because a check that fires on correct output teaches its
reader to skim.** A sweep of all sixteen live surfaces produced two false positives before these
rules were narrowed: `/orders` renders `17:33:30.630940+05:30`, a microsecond timestamp that is
correctly zone-labelled; and `/regime` renders `vol=0.000746`, a volatility of 7.46 basis points
where rounding to two places would display `0.00` and destroy the number. Precision is only a defect
in a MONEY quantity, and a time is only a defect when the page never says which clock it is on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MONEY_WITH_EXCESS_PRECISION = re.compile(r"(?:Rs|₹)\s?-?[\d,]+\.\d{3,}")
"""Rupees to three or more decimal places.

Two is the resolution money has. `Decimal` arithmetic over rupee quantities readily produces
twenty-two, and the first live render of the decision-trace explanation printed exactly that.
"""

SCIENTIFIC_NOTATION_MONEY = re.compile(r"(?:Rs|₹)\s?-?\d\.\d+e[+-]?\d+", re.IGNORECASE)
"""`Rs 1.389e+05`. Money is read in groups of three digits; scientific notation is for physics.

This was the FIRST fix for the excess-precision defect, which is why it has its own rule: the
obvious repair for one of these introduces the other.
"""

BARE_CLOCK_TIME = re.compile(r">\s*([0-2]\d:[0-5]\d)\s*<")
NAMES_A_TIMEZONE = re.compile(r"IST|\+05:30|Asia/Kolkata|UTC", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RenderedSurfaceFinding:
    """One way a rendered page misleads a reader who is not checking it against the source."""

    route: str
    rule: str
    evidence: str
    why_it_misleads: str

    def describe(self) -> str:
        return f"{self.route}: [{self.rule}] {self.evidence} — {self.why_it_misleads}"


def inspect_rendered_surface(route: str, html: str) -> list[RenderedSurfaceFinding]:
    """Every rendered-honesty defect in one page's HTML."""
    findings: list[RenderedSurfaceFinding] = []

    excess = MONEY_WITH_EXCESS_PRECISION.findall(html)
    if excess:
        findings.append(
            RenderedSurfaceFinding(
                route=route,
                rule="money-precision",
                evidence=", ".join(sorted(set(excess))[:3]),
                why_it_misleads=(
                    "money has two decimal places; more is Decimal arithmetic leaking onto the "
                    "page, and a reader cannot tell a real sub-paisa quantity from a formatting "
                    "failure"
                ),
            )
        )

    scientific = SCIENTIFIC_NOTATION_MONEY.findall(html)
    if scientific:
        findings.append(
            RenderedSurfaceFinding(
                route=route,
                rule="money-scientific-notation",
                evidence=", ".join(sorted(set(scientific))[:3]),
                why_it_misleads=(
                    "a rupee amount in scientific notation cannot be compared at a glance against "
                    "one that is not, and every other amount on these pages is written out"
                ),
            )
        )

    if BARE_CLOCK_TIME.search(html) and not NAMES_A_TIMEZONE.search(html):
        findings.append(
            RenderedSurfaceFinding(
                route=route,
                rule="unlabelled-clock",
                evidence=", ".join(sorted(set(BARE_CLOCK_TIME.findall(html)))[:3]),
                why_it_misleads=(
                    "this dashboard serves one exchange, and a clock time with no timezone beside "
                    "it was printed in UTC for five hours before anyone noticed the offset"
                ),
            )
        )

    return findings
