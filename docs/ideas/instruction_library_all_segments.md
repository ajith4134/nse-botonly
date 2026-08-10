# The instruction library — all six segments, all regimes

**Seed (operator, 2026-08-10):** *"More of this — instructions in options, and the same for cash, futures
index and stock, and commodities."*

Every entry is a candidate **instruction object** (L11.97): it names its **mechanism** (why it should
work), its **trigger**, its **exit discipline**, and its **denominator** (L11.106). None is a licence to
trade — each is a hypothesis that must clear the central gatekeeper (DSR · CPCV · net-EV) before arming,
and each carries a TTL and a track record that retires it when its edge stops clearing costs.

Markers: ⭐ strong mechanism, high prior · ✅ sound, standard · 🚀 advanced, needs supporting engines ·
🌌 frontier · ⚠️ known trap or hard precondition.

---

# 1 · INDEX OPTIONS

*Denominator: **ticket** = premium × lot. NIFTY is the only NSE weekly; BANKNIFTY / FINNIFTY / MIDCPNIFTY
are monthly-only since Nov 2024. Every instruction below carries the minimum-ticket gate (L11.107) and the
live-spread gate (L11.108).*

## 1a · Flat / range-bound

| # | Instruction | Mechanism |
|---|---|---|
| IO-01 ⭐ | **ATM premium oscillation scalp** | premium mean-reverts intraday while the index chops; 5% swings on a ₹100 premium clear a ~1% cost floor |
| IO-02 ⭐ | **Short strangle / iron condor theta harvest** | the variance risk premium — IV exceeds subsequent realized vol on average. The home engine of this regime |
| IO-03 ✅ | **Iron butterfly at the range centre** | tighter body, larger credit, when the range is well-defined |
| IO-04 🚀 | **Calendar spread when front IV > back IV** | sell the expensive near expiry, own the cheaper far one |
| IO-05 🚀 | **Butterfly centred on the pinning strike** | dealer gamma hedging pins price near high-OI strikes into expiry |
| IO-06 ✅ | **Intraday decay ride on 0-DTE** | theta is hyperbolic on expiry day; sell morning, cover afternoon |
| IO-07 ⚠️ | **Naked short strangle** | same VRP mechanism, but short gamma means unbounded tail. **Defined-risk versions only** — use IO-02 |

## 1b · Volatile / expansion

| # | Instruction | Mechanism |
|---|---|---|
| IO-08 ⭐ | **Long straddle on compression break** | vol clusters; a squeeze resolving into expansion pays both gamma and vega |
| IO-09 🚀 | **Delta-neutral gamma scalp** | hedge delta with index futures, harvest realized-vs-implied vol. The professional form of "trade the oscillation" — needs the futures holon |
| IO-10 ✅ | **Long strangle pre-scheduled-event** | ⚠️ counter-evidence: straddles lose ~8% per event because options overprice known moves (D.04). Only when IV is *below* forecast RV |
| IO-11 🚀 | **Ratio backspread when skew is cheap** | asymmetric payoff financed by the expensive wing |
| IO-12 ✅ | **Premium breakout on the contract itself** | the option's own price breaks its intraday range — trades the ticket, not the index |

## 1c · Bull trend

| # | Instruction | Mechanism |
|---|---|---|
| IO-13 ⭐ | **Bull call spread** | defined risk, cheaper than long CE, caps the vega bleed |
| IO-14 ✅ | **Long CE on trend confirmation** | pure delta; ⚠️ theta bleeds if the trend stalls, so it needs a time-stop |
| IO-15 ✅ | **Bull put spread (credit)** | bullish *and* short-vol — pays if price rises or merely does not fall |
| IO-16 🚀 | **Call ratio spread** | finances upside with an extra short call; ⚠️ short-gamma tail above the ratio strike |
| IO-17 🚀 | **Delta-rolling up the ladder** | as the trend extends, roll strikes up to keep delta in the paying zone |

## 1d · Bear trend

| # | Instruction | Mechanism |
|---|---|---|
| IO-18 ⭐ | **Bear put spread** | defined risk; **and bear regimes raise IV, so long premium is doubly favoured** |
| IO-19 ✅ | **Long PE on breakdown** | pure delta plus rising vega — the one regime where both work together |
| IO-20 ✅ | **Bear call spread (credit)** | short-vol bearish; pays if price falls or stalls |
| IO-21 🚀 | **Put ratio backspread** | long convexity into a crash, financed by a nearer short put |

## 1e · Structural / regime-agnostic

| # | Instruction | Mechanism |
|---|---|---|
| IO-22 ⭐ | **Expiry-day max-pain pinning** | market makers hedging gamma mechanically pull price toward high-OI strikes. A real dealer-flow mechanism, not a pattern |
| IO-23 ⭐ | **Post-event IV crush** | IV inflates before scheduled events and collapses immediately after, independent of direction |
| IO-24 🚀 | **Weekly-to-monthly roll dislocation** | the NIFTY weekly / monthly term structure distorts around the roll |
| IO-25 🚀 | **Strike-ladder relative value** | adjacent strikes mispriced against each other on the same surface |
| IO-26 ✅ | **PCR extreme fade** | positioning extremes precede reversals; ⚠️ weak alone, use as a filter |
| IO-27 🚀 | **OI-buildup classification** | long buildup vs short covering vs short buildup vs long unwinding — price-and-OI together say what the flow is |
| IO-28 🌌 | **Maker-side premium capture** | quote both sides on a liquid strike and *earn* the spread. The single durable small-edge lever (L1.14) |
| IO-29 🚀 | **Opening-minutes IV overshoot fade** | IV spikes at 09:15 and normalises within minutes |
| IO-30 ⚠️ | **Far-OTM lottery buying** | **DISPROVED for scalping** — ₹40 flat brokerage on a ₹650 ticket is 6.15% (L11.107). The worst vehicle in the universe |

---

# 2 · STOCK OPTIONS

*Physically settled since Oct 2019. **No naked premium selling (A.15).** Liquid in only ~20–30 names, so
the spread gate kills most of the universe before anything else applies.*

| # | Instruction | Mechanism |
|---|---|---|
| SO-01 ⭐ | **Directional CE/PE on relative-strength leaders** | single-name momentum is stronger than index momentum |
| SO-02 ⭐ | **Debit verticals only** | defined risk; the only structure that respects the no-naked rule |
| SO-03 🚀 | **Covered call against a carried cash position** | **newly possible** — cash can now carry overnight (R.01), so the stock leg can exist. Harvests premium on a held position |
| SO-04 ✅ | **Earnings long premium, pre-announcement** | ⚠️ only when IV has not already inflated past forecast RV |
| SO-05 🚀 | **Post-earnings IV crush via defined-risk credit spread** | the crush is reliable; the structure must cap the tail |
| SO-06 ⭐ | **Expiry-week close-out at T-1/T-2** | **mandatory discipline**, not a play — avoids forced physical delivery and escalating delivery margin |
| SO-07 ⚠️ | **Anything in an illiquid strike** | 4–10% spreads make every instruction unprofitable regardless of merit. Hard liquidity-tier gate |

---

# 3 · CASH INTRADAY

*Denominator: the stock price itself — the one segment where instrument and reference coincide. Cost floor
6–11 bps; range-width precondition L11.99 applies.*

## 3a · Flat / range-bound

| # | Instruction | Mechanism |
|---|---|---|
| CE-01 ⭐ | **VWAP ±2σ reversion** | VWAP is the institutional execution benchmark, so deviation invites reversion. ⚠️ skip when ADX is high |
| CE-02 ✅ | **Opening-range fade** | the first-hour range often caps the day when no catalyst exists |
| CE-03 ✅ | **Pivot / CPR bounce** | widely watched levels become self-fulfilling within the session |
| CE-04 ✅ | **Connors RSI(2) mean-reversion** | short-horizon oversold bounce in a non-trending tape |
| CE-05 ⚠️ | **Sub-cost-floor range scalping** | **DISPROVED (D.01)** — requires `range_width_bps > cost × 1.5` or it accumulates cost, not profit |

## 3b · Volatile / expansion

| # | Instruction | Mechanism |
|---|---|---|
| CE-06 ⭐ | **Opening-range breakout with volume confirmation** | genuine information arrival expands the range; volume separates it from noise |
| CE-07 ✅ | **Gap-and-go** | large gaps with follow-through volume continue |
| CE-08 ✅ | **Gap fade** | small gaps with no news mean-revert to prior close |
| CE-09 ✅ | **Momentum ignition on relative-volume surge** | 3–10× RVOL marks real participation |
| CE-10 ✅ | **NR7 / Bollinger-squeeze breakout** | volatility compression precedes expansion |
| CE-11 🚀 | **Size *down* as ATR rises** | keeps rupee risk constant across regimes — a sizing instruction, not an entry |

## 3c · Bull trend

| # | Instruction | Mechanism |
|---|---|---|
| CE-12 ⭐ | **Pullback to VWAP / rising EMA in an uptrend** | best risk-reward entry in a trend; stop is structurally close |
| CE-13 ⭐ | **Relative-strength leader rotation** | leaders keep leading intraday |
| CE-14 ✅ | **Sector-leader confirmation** | trade the strongest name in the strongest sector |
| CE-15 🚀 | **Overnight carry promotion** | strong close + trend + no overnight event ⇒ evaluate promotion to multi-day (R.01, L5.47) |
| CE-16 🚀 | **Delivery-percentage confirmation** | high delivery% signals genuine accumulation rather than intraday churn |

## 3d · Bear trend

| # | Instruction | Mechanism |
|---|---|---|
| CE-17 ⭐ | **Breakdown short with volume** | ⚠️ **verify intraday short-selling and square-off constraints in cash equity before relying on this** — index futures may be the better bearish expression (L14.29) |
| CE-18 ✅ | **Failed-breakout fade** | trapped longs become forced sellers |
| CE-19 ✅ | **Relative-weakness laggard short** | weakest names fall fastest in a broad decline |

## 3e · Structural / session-time

| # | Instruction | Mechanism |
|---|---|---|
| CE-20 ⭐ | **First-hour range definition** | the 09:15–10:15 range sets the day's structure; many instructions key off it |
| CE-21 ✅ | **Midday lull mean-reversion** | volume ebbs 11:30–14:00, so ranges hold and breakouts fail more often |
| CE-22 ✅ | **Last-hour momentum** | institutional completion flow concentrates near the close |
| CE-23 🚀 | **F&O-ban entry effect** | a name entering the ban list loses derivative hedging, changing its cash behaviour |
| CE-24 🚀 | **Bulk / block deal follow** | disclosed large trades signal informed participation |
| CE-25 ⚠️ | **Circuit-band proximity** | a stock near its circuit is a **liquidity trap where stops can fail** — an exclusion instruction, not an entry |

---

# 4 · INDEX FUTURES

*Denominator: **margin**. ≈₹15.6 lakh notional on ~₹1.2–1.5 lakh margin = **10–12× leverage**, so a 0.5%
index move ≈ 5–6% return on capital posted.*

| # | Instruction | Mechanism |
|---|---|---|
| IF-01 ⭐ | **Margin-efficient index momentum** | the cheapest bps-per-rupee directional expression available |
| IF-02 ⭐ | **The bearish expression of choice** | no borrow constraint, unlike shorting cash equity intraday |
| IF-03 ⭐ | **Hedge overlay for the carried cash book** | short futures to delta-neutralise overnight cash exposure — newly essential now that cash can carry |
| IF-04 🚀 | **Cash-futures basis convergence** | basis must converge to zero at expiry — a real structural mechanism, near-arbitrage |
| IF-05 🚀 | **Roll-week calendar spread** | systematic roll pressure distorts the near-far spread predictably |
| IF-06 ✅ | **Opening-gap fade or follow** | conditioned on GIFT Nifty and overnight global cues |
| IF-07 🚀 | **Index-vs-constituent lead-lag** | the future often leads the cash index; the basket lags the future |
| IF-08 🚀 | **Futures-implied vs option-implied direction divergence** | when the futures basis and the option skew disagree, one is wrong |
| IF-09 ✅ | **Gamma-scalp hedge leg** | the delta-hedging instrument for IO-09 |

---

# 5 · STOCK FUTURES

*Physically settled, escalating expiry-week margin, genuinely liquid in only a few dozen names.*

| # | Instruction | Mechanism |
|---|---|---|
| SF-01 ⭐ | **Liquid-name momentum with margin leverage** | same leverage benefit as index futures, on single names |
| SF-02 🚀 | **Per-name cash-futures basis** | wider and less efficient than index basis, so more opportunity — and more risk |
| SF-03 🚀 | **Roll-over OI analysis** | how much OI rolls to the next series signals conviction in the position |
| SF-04 ⭐ | **Expiry-week close-out at T-1/T-2** | **mandatory** — avoids forced delivery and escalating margin |
| SF-05 ✅ | **Pair trade against the index future** | isolates single-name alpha from market beta |
| SF-06 ⚠️ | **Anything outside the top liquidity tier** | hard gate; thin stock futures punish every instruction |

---

# 6 · COMMODITIES / MCX

*A second venue: own instrument master, own margin regime, own calendar, session to 23:30 IST.*

| # | Instruction | Mechanism |
|---|---|---|
| MC-01 ⭐ | **09:00 open-gap follow-through** | MCX opens after COMEX/international markets have moved overnight. **Unlike the NSE equity gap, this is a genuine information gap rather than a priced-in one** — the strongest structural edge in this segment |
| MC-02 ⭐ | **Scheduled inventory-release volatility** | crude inventory data produces repeatable, timetabled volatility expansion |
| MC-03 🚀 | **US-overlap session regime (17:00–23:30)** | a structurally different regime from the morning session, with different volatility and participants — needs its own models, not the same ones |
| MC-04 🚀 | **Gold-silver ratio mean-reversion** | a long-documented relative-value pair |
| MC-05 🚀 | **Dollar-index inverse on gold** | gold is priced in dollars, so DXY moves mechanically transmit |
| MC-06 ✅ | **Crude–natural-gas divergence** | correlated energy complex with periodic dislocations |
| MC-07 🚀 | **Seasonality** | natural gas in winter, agricultural harvest cycles — genuine physical-demand seasonality, unlike equity "seasonality" |
| MC-08 🚀 | **Contango / backwardation roll** | the curve's shape determines whether rolling costs or pays |
| MC-09 ✅ | **Rupee-adjusted international parity** | MCX prices are international price × USDINR; a dislocation between the two legs is tradeable |

---

# 7 · Cross-segment instructions

*These require more than one holon and are where the six-bot architecture earns its complexity.*

| # | Instruction | Mechanism |
|---|---|---|
| XS-01 ⭐ | **Expression selection** | the same conviction routed to whichever vehicle converts it best per rupee of capital (L14.29) |
| XS-02 ⭐ | **Cash-futures arbitrage** | requires the cash and futures holons acting together |
| XS-03 🚀 | **Delta-neutral gamma scalp** | options holon plus futures holon (IO-09 + IF-09) |
| XS-04 🚀 | **Covered call on carried stock** | cash holon plus stock-options holon (SO-03) |
| XS-05 🚀 | **Dispersion** | index options versus constituent options — index vol rich against the basket |
| XS-06 ⭐ | **Portfolio hedge overlay** | index futures short against the aggregate carried book (IF-03) |
| XS-07 ⚠️ | **Double-counted conviction** | ⚠️ an **anti-instruction**: long cash + long futures + long calls on one underlying is one bet sized three times. Blocked in live by L14.31; permitted in paper as the experiment that builds the conversion table |

---

## Summary

**≈110 instructions**: index options 30 · stock options 7 · cash intraday 25 · index futures 9 ·
stock futures 6 · commodities 9 · cross-segment 7, plus the regime and structural variants inside each.

**How to read the markers:** ⭐ entries have the strongest mechanism and should be built first within their
segment. ⚠️ entries are traps, hard gates or anti-instructions and are catalogued precisely so they are not
rediscovered as ideas later.

**None of these is a strategy yet.** Each is a hypothesis with a stated mechanism, awaiting the gatekeeper.
The expected outcome — stated plainly so it is not a disappointment later — is that **most will fail
validation**. That is the system working, not the system failing. The value of the library is that the
survivors are known to be survivors, and the failures are recorded so they are not tried twice.
