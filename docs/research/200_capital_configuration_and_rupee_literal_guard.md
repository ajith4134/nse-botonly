# 200 — Capital configuration + the rupee-literal guard

**Task:** todo `0.8`. **Rules:** R.03 (no hardcoded values), A.23 (capital is a parameter from ₹1 lakh to
₹1 crore), R.23 (this is decision-path code, so it takes the full loop).

**R.23(b) classification — stated up front so the vocabulary does not inflate.** This is **not an
engine**. It has no solver and carries no state between decisions. It is a **configuration value object
plus an executable guard**. Calling it a "capital engine" would be exactly the labeling failure
`research/155` catches. It still takes the full loop because it sits in the decision path: every position
size in the system is derived from it.

---

## 1. The problem it solves

The prior build hardcoded rupee amounts. That is fatal to A.23 for a specific reason: **a threshold that is
correct at ₹1 lakh is wrong by two orders of magnitude at ₹1 crore.** A ₹5,000 max-loss cap is 5% of a
₹1 lakh account and 0.05% of a ₹1 crore one — the same literal encodes two completely different risk
policies. Any rupee constant in the codebase silently pins the system to one account size.

**The invariant:** *no rupee quantity is ever written as a literal. Every rupee amount is derived at
runtime from configured capital.* Limits, floors and budgets are expressed as **fractions of capital**;
rupees are computed, never typed.

## 2. Design

### `TradingCapital` — a frozen value object

- Holds the configured total as **`Decimal`**, never `float`. Money precision is a correctness
  requirement: binary floats cannot represent ₹0.05 exactly, and rounding drift accumulates across the
  thousands of sizing calls a trading day makes.
- `rupees_for_fraction(fraction)` — converts a policy fraction into rupees at the configured size.
- `fraction_of_capital(rupees)` — the inverse, for expressing an observed amount as policy.
- Both quantise to paise, with explicit `ROUND_DOWN` on allocation so rounding can never allocate more
  capital than exists.

### The configured bounds are *sourced*, not magic

`MINIMUM_SUPPORTED_CAPITAL_RUPEES` and `MAXIMUM_SUPPORTED_CAPITAL_RUPEES` are the only rupee literals
permitted in the module, and they are legitimate under R.23(e) because they are **an operator decision
(A.23), cited in the comment** — not a tuning parameter. They define the range the system claims to
support, and configuration outside it is rejected loudly rather than silently mis-sized.

### Loading

From the environment (`NSE_TRADING_CAPITAL_RUPEES`), per R.02 — never from a committed file. Absent or
malformed configuration raises; it never defaults to a guessed amount, because a silently-defaulted
capital would size real orders.

### The guard — the part that actually enforces R.03

A test that **scans every source file for rupee literals** and fails on them. Prose rules decay; an
executable check does not. This is the same reasoning as R.23: verification loops beat exhortation.

Detected patterns: `₹` followed by digits · `_rupees = <number>` · `_inr = <number>` ·
comparisons against bare numbers in names containing `rupee`/`inr`/`capital`/`margin`. Allowances are
explicit and narrow: the bounds in this module (sourced to A.23), test fixtures, and `0`/`1` identity
values.

## 3. Acceptance criteria

1. A capital of ₹1 lakh and one of ₹1 crore both configure cleanly and produce sizing quantities that
   differ by exactly the capital ratio — the same policy fraction, different rupees.
2. Out-of-range or malformed configuration raises rather than defaulting.
3. `Decimal` throughout; no `float` in any money path.
4. Allocation rounding is `ROUND_DOWN` — never allocates more than exists.
5. The rupee-literal guard fails on a deliberately planted literal and passes on the real tree.

## 4. What this deliberately does NOT do

No position sizing, no risk limits, no Kelly, no vol-targeting. Those are `L1.10`, `L7.01` and `L8.02–03`
in later phases, and each is a genuine engine. This module is the seam they consume so that none of them
can hardcode rupees.

---

## 5. Sourcing record (R.16 / R.17 — mechanical evidence, run 2026-08-10)

| Candidate | Verdict | Mechanical evidence |
|---|---|---|
| **`py-moneyed` 3.0** | **REJECTED ON REFLECTION** (was ADOPT) | Installed on ARM64/py3.12 first try. `Money("100000.00","INR")` → `₹100,000.00`, `.amount` is **`Decimal`**, not float. `moneyed.INR` present (`INR`, "Indian Rupee"). Arithmetic verified: `×2 → ₹200,000.00`, `/4 → ₹25,000.00`. Gives the money type and INR formatting so neither is hand-rolled. |
| `money` 1.3.0 | REJECT (tier-1) | Superseded by py-moneyed; no reason to carry a second money type. |
| `pymoney` 1.2.31 | REJECT (tier-1) | Single stale release, no INR handling worth the dependency. |
| ruff **`PLR2004`** magic-value-comparison | **ADOPT, PARTIAL** | Read the rule: it flags unnamed numeric constants **in comparisons only** — not assignments, and it is not currency-aware. Enabled, but it does **not** cover `max_loss_rupees = 5000`, which is exactly the failure this task exists to prevent. |

**What is built rather than borrowed, and why:** `py-moneyed` has **no fraction-of-capital concept**
(verified: no `fraction` member on `Money`), and no linter found expresses "no rupee literal anywhere."
The capital-fraction layer and the rupee-literal guard are therefore ours — the borrowed piece is the
money *type*, not the policy.


---

## 6. Corrections after adversarial review (2026-08-10)

A fresh-context reviewer found 14 defects, most reproduced empirically. The three
that were live money bugs — over-allocation under the default decimal context, a
constructor that bypassed all validation, and NaN escaping the documented
exception contract — are fixed and now have regression tests. Two corrections to
*this document* were also required:

**`py-moneyed` was recorded as ADOPT but never imported.** The record described an
adoption that did not happen — a documentation defect, and exactly the kind the
sourcing rule exists to prevent. Corrected to **REJECTED ON REFLECTION**, with the
honest reason: `Money` wraps an amount with a currency, and this module holds a
single INR scalar whose currency is never in question. The wrapper buys formatting
we do not use and adds a dependency to every downstream sizing call. `Decimal` with
an explicit `localcontext` is the right primitive here. The dependency is removed
from `pyproject.toml`.

**The stated detector coverage did not match the code.** The doc claimed `₹`-prefixed
literals and comparison-operand checks that did not exist. The detector has since been
rebuilt (`src/nse_algo_trader/rupee_literal_detector.py`) and now genuinely covers
comparisons, negation, constant folding, attribute and subscript targets, tuple
unpacking, augmented assignment, dict values, argument defaults, returns, and
module-qualified or aliased `Decimal` constructors — verified against a 26-case
evasion corpus written by the reviewer, kept as a permanent regression fixture.

**Measured before and after:** the original detector caught **0** of the reviewer's
evasions. The rebuilt one catches **26**.

## 7. Rule G / R.06 — the queued consumer

This module currently has **no production consumer**; only tests import it. That is
permitted under R.06 solely because its consumer is named and queued: the position
sizer (`L1.10`) and the risk-based sizer (`L7.01`), both in Phases 1–2, which is
where a rupee amount is first computed for a real order. **Until then this task is
not "done" in the R.11 sense** — a feature whose primary consumer is still queued is
not finished, and it is recorded as such in the todo rather than silently ticked.
