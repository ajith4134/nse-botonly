# 249 · Two sourcing evaluations, both with mechanical evidence — 2026-08-17

`R.17`: an OSS rejection needs mechanical evidence — does it install, is it maintained, do the real
signatures work, did it run on real input? README prose is not evidence. Both evaluations below were
run by installing the candidates on this machine and pointing them at this project's real files.

---

## Part 1 — Can an existing tool enforce "the code matches the plan"?

Asked before writing `scripts/check_work_conforms_to_plan.py`, because a bespoke checker that
duplicates a maintained tool is waste.

| Tool | Installs (ARM64) | Last activity | Works on freeform markdown? | Extracted our IDs? | Verdict |
|---|---|---|---|---|---|
| doorstop | yes | 2026-08-15 | **No** — one YAML file per item; `import` wants a doorstop export or CSV/XLSX | n/a | REJECT |
| sphinx-needs | yes | 2026-08-17 | **No** — `NeedDirective` is its only ingestion path; no directive, no item | n/a | REJECT |
| StrictDoc | yes | 2026-08-17 | **No** — **crashed** on the real plan: `StrictDocSemanticError` → `BrokenProcessPool` | n/a | REJECT |
| OpenFastTrace | yes (needs Java 21) | 2026-08-07 | **No** — needs its own `[[id]]` tags | **0 of 682** | REJECT |
| lobster (BMW) | yes | 2026-08-14 | **No** — needs `lobster-trace:` comments in code *and* TRLC on the requirement side | n/a | REJECT |
| **vale** | yes (Go binary, ARM64 native) | 2026-08-05 | **Yes** — ran on the unmodified 463 KB plan | n/a (prose linter) | **ACCEPT for vocabulary drift** |
| textlint | yes | 2026-08-16 | yes | n/a | viable but Node-based; vale already proven |
| semgrep | yes | 2026-08-17 | operates on code, not the plan | n/a | REJECT — wrong target |
| pytest-bdd, reqif, mkdocs-{requirements,traceability,needs} | — | — | wrong shape / **404 on PyPI, do not exist** | n/a | REJECT |
| Capella, Papyrus | — | — | Eclipse desktop MBSE apps, no CLI/pip; the PyPI hits are unrelated name-squats | n/a | REJECT |

**The decisive fact, and it is the same one five times:** every dedicated traceability tool requires
the plan to be **rewritten into its own format** — doorstop's per-item YAML, sphinx-needs's
`.. need::` directives, StrictDoc's SDoc grammar, OpenFastTrace's `[[id]]` tags, lobster's magic
comments plus TRLC. None can passively read IDs out of prose it did not design. That was confirmed by
running them, not by reading about them: **StrictDoc crashes** on `ajith_final_plan.md`, and
**OpenFastTrace reports `ok - 0 total`** against a file containing 682 entries.

Rewriting a 457 KB hand-maintained narrative plan into any of those formats is a larger and riskier
project than the checker, and it would fight how the plan is actually used — freeform prose, edited
every session.

**Verdict: bespoke checker, plus vale as a possible future substitution engine.** Vale was tested
properly: a synthetic drift line was appended to a copy of the real plan and a `substitution` style
flagged it at the correct line with zero reformatting. It is **not vendored yet** — the vocabulary
check currently ships as a regex table inside the checker, which needs no Go binary in the repo and
keeps every rule next to the plan entry that justifies it. Recorded in `BACKLOG.md` as the upgrade
path if the vocabulary table outgrows a regex list.

## Part 2 — The Greeks / IV-surface engine for `B1`

Blocks arming both option segments (`docs/research/247`). Sourced now because it is the largest gap.

**Market fact verified first:** NSE index options **and** NSE stock options are **European-style**.
SEBI circular CIR/DNPD/6/2010 let exchanges choose; NSE moved all stock options to European exercise
for expiries from 2011-01-27. **No American-exercise machinery is needed** — Black-Scholes-Merton and
Black-76 are sufficient and correct. That single fact removes the hardest part of the engine.

| Library | Installs | Last release | Greeks | IV solve | Surface fit | 10k IV solves | Verdict |
|---|---|---|---|---|---|---|---|
| **py_vollib / vollib** | yes | 2026-06-01 | full | Jäckel rational | no | **0.28 s** | **RECOMMEND — pricing/Greeks/IV** |
| **QuantLib-Python** | yes | 2026-07-14 | full + vanna/volga/charm | yes | **SVI, NoArbSABR, ZABR, Kahale, BlackVarianceSurface** | 0.46 s | **RECOMMEND — surface** |
| PyFENG | yes | 2026-05-26 | no rho | yes, vectorised | no | **0.0078 s** | runner-up (35× faster, rougher API) |
| mibian | yes | **2016** | full | Newton | no | 8.9 s (prices only) | REJECT — 10 years stale, unusably slow |
| financepy | yes | 2026-08-07 | full | yes | no | 0.46 s | REJECT — **downgrades numpy 2.4.6→2.3.5 and breaks vectorbt/tclf** |
| pysabr | yes | **2022** | none | n/a | SABR only | n/a | REJECT — 4 years unmaintained, no pricer |
| tf-quant-finance | **no** | stuck at `0.0.1.dev34` since 2022 | — | — | — | — | REJECT — `ModuleNotFoundError: tensorflow` |
| optlib | **no** | 2022 | — | — | — | — | REJECT — `NameError: CCompiler` (distutils gone in 3.12) |
| volsurface | yes | 2026-03-07 | none | none | scaffold only, no model | n/a | REJECT — 2 stars, one commit, empty shell |

**Real-input agreement** (NIFTY-like: S=24500, K=24500, T=7/365, r=6.5%, σ=12%, call). All three
survivors agree to ~6 decimals: **price 178.0421164271**, δ 0.53320216, Γ 0.00097646. IV solved back
from the premium: py_vollib **0.12000000000000001** (exact to float precision), PyFENG max abs error
**7.4e-6** over a 10k-strike batch, QuantLib 0.11999974832 (Newton residual). Stress-tested 10% OTM,
10% ITM and 1-DTE 3% OTM — all converge.

**Recommendation: combine two.** No single package covers the spec. `py_vollib` for
pricing/Greeks/IV — it is the standard implementation of Jäckel's *Let's Be Rational*, exact on the
verified case, fast enough for a whole NSE chain without vectorisation, and **zero dependency
conflicts** with this venv. `QuantLib-Python` for surface construction — it is the **only** candidate
shipping arbitrage-aware smile machinery (SVI, no-arbitrage SABR, Kahale repair, and
`SmileSectionRNDCalculator` for butterfly-arbitrage checks via risk-neutral density).

**Surfaced for the operator's double-check (`R.17` / rejection transparency):** the rejection that
would most repay a second opinion is **financepy** — it is actively developed and numerically correct,
and was rejected *purely* on dependency pins (numpy<2.4, scipy<1.17, numba<0.63, llvmlite<0.46) that
conflict with vectorbt. If vectorbt is ever dropped, financepy becomes viable. **PyFENG** is the other
one worth a look: 35× faster than the recommendation, and if the option universe grows to full-ladder
scale across 209 underlyings that speed may matter more than py_vollib's maturity.

**Venv note:** the evaluation left `py-vollib`, `vollib`, `pyfeng`, `QuantLib`, `mibian`, `pysabr`,
`volsurface`, `tf-quant-finance` installed and unwired; `financepy` was uninstalled to remove its
conflicting pins. None is imported by project code yet.
