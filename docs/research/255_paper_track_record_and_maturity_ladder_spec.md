# 255 · `L5.30` — the paper track record and the maturity ladder computed from it

**Idea-intake verdict: ALREADY EXISTS as `L5.30`** ("pod paper lifecycle engine — breaks the
cold-start deadlock so the board populates"), todo 6.6. No new plan entry. Opened by `B15`.

## 1. The deadlock, measured

`run_daily_operations.py` runs twelve steps and **none of them trade**. The only paper session is
`scripts/verify_paper_session_on_real_data.py`, a verification harness that writes to a scratch
directory and **deletes its ledger at the start of every run**. The production
`paper_capital_ledger.sqlite3` holds **13 events, total, ever**.

`BotMaturity.closed_trades_observed` is therefore fed by nothing. Every segment bot reports
`COLD_START` and always will, however good it is. The ladder built into `L5.29` this morning is
today a function that can only return its first value — `R.04` has nothing to climb, `R.22` has
nothing to graduate.

## 2. Why this is an engine and not a counter (`R.23a`)

| `R.23a` requirement | What supplies it |
|---|---|
| Raw input pipeline | Closed positions from real `PaperSessionReport`s, per session, per bot |
| Real inference procedure | A Beta-Binomial posterior on the bot's own net-of-cost win rate against its **break-even** rate, plus a sequential rule for when the evidence is decisive |
| Carried state | An append-only per-bot record accumulating across sessions |
| Output that changes behaviour | The rung, which gates activation (`R.04`) and supplies the first of `R.22`'s two keys |

**SOTA analog:** sequential probability-ratio testing as used for online A/B promotion — the same
shape as `L2.01`'s honest trial registry, which already exists here and counts what was tried.

**A counter would fail the difference test.** "Has it done 100 trades?" is not evidence of anything;
the previous system did **3,481** and lost ₹3.3 lakh.

## 3. What the retained trades tell this design (`docs/research/254`)

The 3,481-trade record is the strongest available guidance, and it points at one statistic:

```
win   n=1,284   avg +₹366.51
loss  n=2,197   avg −₹365.01      →  36.9% win rate, symmetric payoff, guaranteed loss
```

Average win and average loss matched to **₹1.50**. So for a symmetric-payoff strategy the whole
question is **win rate against break-even**, and break-even is not 50% — it is the rate at which
average win × p equals average loss × (1−p) **plus costs**. Costs were ₹176,589 of a ₹331,314 loss,
so they move that break-even materially and must be inside the statistic, not applied afterwards.

**Therefore the ladder tests `P(net-of-cost expectancy > 0 | this bot's own trades)`,** with the
break-even rate derived per bot from its own realised win/loss magnitudes and its own recorded
costs. Nothing is hardcoded (`R.03`): the payoff ratio, the cost drag and the break-even rate are all
measured from the bot's record.

## 4. The rungs, and what each requires

| Rung | Requires |
|---|---|
| `COLD_START` | fewer closed trades than the posterior needs to say anything |
| `OBSERVING` | enough trades to compute a posterior, which is not yet decisive either way |
| `PAPER_QUALIFIED` | posterior mass above break-even exceeds the operator's confidence level |
| `GRADUATION_CANDIDATE` | `PAPER_QUALIFIED` sustained across a required number of distinct sessions, so one lucky day cannot promote |
| `GRADUATED` | **never computed here** — `R.22` requires an operator arm, and `BotMaturity` refuses to construct it |

The confidence level and the sustained-session count are **operator policy**, passed in and stated,
not defaults invented by the engine — the same treatment `PaperSessionPolicy` already gives its
regime thresholds.

**A bot whose posterior is decisively BELOW break-even falls to a `RETIRED`-equivalent** — recorded
as evidence against, not silently left at `OBSERVING`. `opening_range_breakout_v1` with 3,049 trades
at 36.9% is what that looks like, and a ladder that cannot express it is a ladder that only ever
promotes.

## 5. Signatures

```python
class PaperTrackRecordStore:
    def record_session(self, bot_identity: str, report: PaperSessionReport) -> int: ...
    def closed_trades_for(self, bot_identity: str) -> tuple[ClosedPaperTrade, ...]: ...
    def sessions_for(self, bot_identity: str) -> tuple[date, ...]: ...

class BotMaturityLadder:
    def assess(self, bot_identity: str, policy: LadderPolicy) -> LadderAssessment: ...
```

`LadderAssessment` carries the rung, the posterior, the derived break-even rate, the sample size, the
session count, and **the reason in words** — a rung with no reason is the shape `R.13` catches.

Errors: an unknown bot returns `COLD_START` with a reason, never raises. A session recorded twice is
idempotent on `(bot_identity, session_date, position_key)` — a re-run must not double the evidence.

## 6. Verification

- **Unit** — every rung reachable; break-even derived, not assumed; idempotent re-recording.
- **Property** (Hypothesis) — the rung is monotonic in evidence: adding a winning trade never lowers
  it, adding a loser never raises it.
- **Adversarial** — a bot cannot reach `GRADUATED`; a single lucky session cannot reach
  `GRADUATION_CANDIDATE`; a 36.9%-win-rate record must land BELOW break-even, checked against the
  real 3,049-trade `opening_range_breakout_v1` slice.
- **Real data (`R.05`)** — replay the 3,481 retained trades through the ladder as three synthetic
  "bots" (one per `strategy_tag`) and confirm it declines to promote the one that lost ₹3.3 lakh.
  That is the strongest available test: a ladder that would have promoted the known loser is wrong.

## 7. Sourcing (`R.17`)

The posterior is a Beta-Binomial conjugate update — three lines of arithmetic, already available via
`scipy.stats.beta` which is installed and used elsewhere in this project. No library is sourced for
the store (SQLite over local types) or the ladder (the inference is smaller than its own import
statement would be). `river` and `PyMC` were considered and are wrong-shaped: `river` is for
streaming feature learning, `PyMC` for models that need sampling, and a conjugate posterior needs
neither.
