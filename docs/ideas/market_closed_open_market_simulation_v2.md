# Market-closed open-market simulation (v2) — the rebuild of §53

**Seed (operator, 2026-08-10):** *"The important feature that we failed in the previous project on this
VPS — related to off-market, or when the market is closed, creating an open-market simulation which
creates an open-market environment and opens and closes trades just like in the open market."*

**Verdict: ② SUPERIOR VERSION of L10.02 / L5.41–L5.45 (the §53 programme).** The feature existed and was
marked "CONFIRMED WORKING". It was working. **It still failed — and the measured reason is not the one
anybody would guess.**

---

## 1. What the retained data actually says

Computed 2026-08-10 from the 3,481 surviving closed trades in `experience_memory.sqlite3`:

| provenance | n | return/trade | win rate | predicted win | actual win | calibration gap |
|---|---|---|---|---|---|---|
| **live** | 2,620 | −0.230% | 39.0% | 50.8% | 39.0% | **+11.8pp overconfident** |
| **replay_faithful** | 861 | −0.458% | 30.3% | 30.7% | 30.3% | **+0.4pp — near perfect** |

Same strategy through both paths:

| strategy | live | replay |
|---|---|---|
| `opening_range_breakout_v1` | −0.390% (n=2,212) | **−0.475%** (n=837) |
| `directional_option_orb_v1` | +0.144% (n=287) | +0.138% (n=24) |
| **`credit_spread_v1`** | **+1.816%** (n=121) | **n = 0** |

### The three findings

**① The simulation did NOT lie.** This is the usual way paper trading fails — optimistic fills that
evaporate live. It did not happen here. Replay was *slightly pessimistic* versus live on the same
strategy (−0.475% vs −0.390%) and near-identical on options. **Fill realism was sound and should be
carried forward unchanged.**

**② Replay-trained calibration was excellent; live was badly overconfident.** A 0.4pp gap on replay
against 11.8pp live. The simulated environment was producing *better-calibrated beliefs* than the live
path. That is an argument for using it more, not less.

**③ THE FAILURE: it practised the losing strategy and never practised the winning one.**
97% of all replay experience (837 of 861 trades) went to `opening_range_breakout`, which loses −0.475%
per trade. **`credit_spread_v1`, the single strategy with a real measured edge at +1.816% per trade,
received exactly zero replay reps.** The 24/7 experience generator ran thousands of repetitions of the
thing that did not work and none of the thing that did.

**It was not a fidelity failure. It was an allocation failure.** And it worked precisely as designed,
which is why it passed its own verification and nobody caught it. *Practice without selection is just
losing faster.*

---

## 2. What v2 must change

Fidelity is kept. **Allocation is rebuilt.**

| Requirement | Why |
|---|---|
| **The curriculum decides what is practised, not availability** | The old loop replayed whatever was easy to run. The teacher/curriculum bot (L14.21, Trunk XII — **0 of 11 branches built**) must own simulation allocation and target where uncertainty is highest, not where data is handiest |
| **Per-strategy and per-holon experience quotas** | No single strategy may consume more than a bounded share of simulated experience. The 97%/0% split must be structurally impossible |
| **Winners get practised too — deliberately** | An instruction showing early positive edge on thin data is *exactly* what needs more reps to reach its graduation sample N. The old system starved its only winner of evidence |
| **Simulation feeds the proving ground** | The consumer is now L11.115: six holons × ~110 instructions, each needing sample N, regime coverage and holdout. Simulation is how those numbers accrue overnight instead of over years |
| **Trial registration** | Every simulated trade counts toward the honest trial registry and effective-trials DSR (L11.116). Cheap experience must not silently inflate the search size |
| **Curriculum diversity, not day repetition** | r/53's own caveat ①: replaying the same days teaches those days. Deficit-driven session selection (L5.39) targets the least-covered regimes |
| **Market-impact fills stay on** | r/53's caveat ②: in open-loop replay the bot's orders do not move the tape. The impact model (L1.06) is what keeps fills honest — and the measurements above suggest it was already working |

## 3. What it is, restated

A **continuous market-experience generator**: when NSE is closed, rewind to a past session at 09:15 and
replay it as if live — prices, book, depth, bid/ask, volume profile — opening and closing trades exactly as
in a live session, cycling until the next real open. The bot is idle roughly 18 hours a day; this is the
single largest lever on learning speed available.

**With MCX now in scope the idle window shrinks** — the box is busy until 23:30 IST (L14.17b), so
simulation contends with a live commodity session for the conductor's resources (L14.11l). Simulation is
an `opportunistic`-class consumer: it fills genuinely idle capacity and is the first thing evicted.

## 4. Base → Advanced → Ultra

- **BASE 🟩** — faithful replay of past sessions at full fidelity (carried over unchanged, it worked), with
  **curriculum-allocated** practice: quotas per strategy and per holon, deficit-driven session selection,
  every simulated trade registered as a trial.
- **ADVANCED 🚀** — counterfactual perturbation (replay a day with the prices shifted, so the bot learns
  the *pattern* rather than memorising the day) · regime-targeted curriculum that hunts the states where
  an instruction has the thinnest evidence · simulated experience feeding calibration directly, given
  finding ② · cross-holon scheduling so all six accrue evidence overnight rather than one hogging the box.
- **ULTRA 🌌** — synthetic day generation beyond the historical record (stress days, gap days, regime
  transitions that have not occurred yet) · a reactive market where the bot's own orders move the tape,
  closing r/53's open-loop blindness · adversarial curriculum where the red-team bot generates the
  sessions most likely to break a candidate instruction.

## 5. The lesson worth carrying beyond this feature

The old simulation **passed its own verification** — it was marked "CONFIRMED WORKING" and it genuinely
was, mechanically. It replayed faithfully, filled realistically and calibrated well. It still destroyed
value, because *nothing asked what it should be practising.*

A verification that checks "does it run correctly" and never checks "is it running on the right thing" will
sign off on a machine that is efficiently doing the wrong work. That belongs in the proving ground's
graduation criteria and in the cockpit's definition of done — **correctness of execution is not evidence
of correctness of allocation.**
