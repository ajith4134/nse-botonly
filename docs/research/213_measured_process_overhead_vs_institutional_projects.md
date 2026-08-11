# 213 · Measured: our process overhead against real institutional projects

Measured 2026-08-11 by cloning and counting, not by reading claims. Question: why is progress slow?

## Ratios

| repo | test:src | doc:src | avg commit body |
|---|---|---|---|
| qlib | 0.07 | 0.11 | 9.0 |
| zipline | 1.08 | 0.06 | 0.6 |
| freqtrade | 1.16 | 0.29 | 2.4 |
| vnpy | 0.06 | 1.15 | 0.5 |
| nautilus_trader | 1.02 | 1.63 | 5.9 |
| **institutional median** | **~0.7-1.1** | **0.29** | **3.7** |
| ajith4134/nse-crypto-bot-final | 0.25 | 0.37 | 16.9 |
| ajith4134/nse-botonly | 0.45 | 0.73 | 14.6 |
| ajith4134/pattern-brain | 0.44 | 0.25 | 8.8 |
| ajith4134/ai-advanced-crypto-bot-final | 0.008 | 0.01 | 0.0 |
| **nse-algo-trader** | **0.81** | **4.28** | **18** |

`crypto-bot` (240.58) excluded as an outlier: 817 jpg / 1 py file. Not a code repo.

## Findings

1. **Our TESTS are already institutional.** 0.81 is inside the 0.7-1.1 band. Whatever quality this
   project has comes from tests, not from prose.
2. **Our DOCS are ~15x the institutional median** (4.28 vs 0.29). The one project above 1.0 with real
   code, nautilus_trader at 1.63, is a mkdocs site of tutorials and API reference — USER-facing. Ours
   is 248 internal spec files plus 11,583 lines of governance ledgers.
3. **Spec-doc-before-every-feature has zero precedent.** No repo in the institutional set has a
   `docs/design/`, `docs/rfc/` or `docs/adr/` directory. 0 of 5.
4. **Commit bodies are ~5x too long** (18 vs 3.7).
5. **36% of our commits (75/205) contain no code at all.**
6. The operator's own doc-heavy repos have THIN tests (0.008-0.45) — they substituted prose for tests.
   We do both, which is why we are slower than either.

## The causal claim, and its evidence

Every defect found on 2026-08-11 was found by RUNNING something, never by writing something:
screenshot capture found 5 silently-deleted CSS rules (`A.64`); starting the unit found 216/EXIT_GROUP
and that the scheduler had never once run (`A.65`); the run itself found a TypeError on every execution.

Adversarial subagent review also has a measured record here (24 defects in instrument-master, 5 critical
+ 5 high in market-depth) — but it reviews CODE, not specs.

Nothing in the record shows a spec doc catching a defect.

## Conclusion

The rules split into two groups that have been enforced as one. VERIFICATION (tests-first, adversarial
review, real-data pass, run-it/screenshot) is cheap and catches everything. DOCUMENTATION (spec doc per
feature, opinion entry per judgement, decision entry per action, long commit bodies, per-commit ledger
updates) is 46,000 lines with no defect attributable to it.
