# 223 · What the retail-algo framework requires of this system

Gathered 2026-08-13 for `F02` (`A.99`). Primary PDFs pulled and read in full: SEBI
**SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013** (2025-02-04), SEBI
**SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132** (2025-09-30), NSE **NSE/INVG/67858** (2025-05-05) and the
NSE retail-algo FAQ (2025-11-03). Facts are recorded here; nothing in this file is legal advice, and
`R.03` treats every number below as a **dated, sourced regulatory fact** — the one category of
constant the engines are allowed to carry, and only with its citation attached.

---

## 1. Status: in force

Original effective date 2025-08-01, extended to 2025-10-01 (circular …/2025/108), then a phased
glide path (…/2025/132): brokers ready 2025-10-01, registration milestones through 2025-11-30, mock
session by 2026-01-03, and **universal applicability to all stock brokers from 2026-04-01**, verbatim:

> "W.e.f. April 01, 2026, algo framework specified in circular dated February 04, 2025 along with
> implementation standards and detailed operational modalities … will be applicable for all stock
> brokers."

As of today (2026-08-13) no later circular reversing or extending it was found. That is absence of
evidence, and is recorded as such rather than as proof.

## 2. Where this system sits

> "Algos developed by tech-savvy retail investors themselves, using programming knowledge, shall
> also be registered with the Exchange, through their broker, **only if they cross the specified
> order per second threshold**." — SEBI circular, para I.c

This is a self-developed algo, used for one account. Therefore:

- **White-box by definition** (para V): the logic is known and replicable by its user. The black-box
  category, with its Research Analyst registration, governs providers selling opaque strategies to
  others and does not reach here.
- **No registration is required while the flow stays below the threshold**, and the same registered
  algo — if it ever were registered — may be used for "self, spouse, dependent children and
  dependent parents", and for nobody else.
- A tech-savvy client running their own algo is **exempt from the monthly mock trading session**
  (NSE FAQ item 9), and hosts the strategy **client-side**, not on the broker's servers — the
  opposite of the requirement placed on empanelled providers.

**The design consequence, which is the whole reason this matters to `L3.06`:** staying under the
threshold is not a performance choice, it is what keeps this system in the registration-free
category. The rate limiter's default budget is therefore set *below* the threshold, and crossing it
is a refusal, never a burst.

## 3. The threshold — 10 orders per second

NSE/INVG/67858, Annexure paras B.2 and F, verbatim:

> "The Threshold Order Per Second (TOPS) is initially set at not exceeding 10 orders per second …
> If the flow of algo orders from the client to the broker via API is below the defined Threshold
> Order Per Second (TOPS) i.e. 10 OPS **per exchange**, the client will not be required to register
> … The threshold will be applied basis the **calendar clock second of the broker server**."

- Unit: orders per **calendar second on the broker's clock**, not a rolling window. A limiter that
  smooths over a rolling second can still put eleven orders inside one calendar second, so the
  bucket must be aligned to the calendar second to be measuring the same thing the broker measures.
- Scope: **per exchange**; para F adds "per exchange /segment". The primary text is not perfectly
  crisp between the two readings and both are recorded here rather than one being picked silently.
  The engine takes the **stricter** reading (per exchange), because being wrong in the loose
  direction changes the regulatory category of the flow.
- The number is exchange-adjustable "after due notice to the market" — so it is carried as a dated
  fact with an effective-from, not as a literal.
- A broker may set a **stricter** client-level limit, never a looser one. Kite's own published
  ceilings (10/sec, 400/min, 5,000/day — `docs/research/222` §5) therefore bind simultaneously with
  this one, and the tightest bucket wins.

## 4. Tagging — every algo order, not only registered ones

NSE/INVG/67858, para G, verbatim:

> "Exchanges shall issue appropriate tagging mechanisms for registered and registration free algo
> orders. **All algo orders (Below and above the threshold) shall be tagged** with a unique
> identifier provided by the Exchange in order to establish audit trail."

- The **exchange** assigns the identifier; the broker relays it. It is not something this system
  invents.
- Sub-threshold API flow is covered by a standardised convention rather than a per-strategy
  registration: per the NSE FAQ, the first twelve digits are `444444444444` and the thirteenth is
  `0`, `2` or `4`.
- **This is a different field and a different purpose from the `tag` this system computes.** The
  exchange identifier establishes the audit trail for the regulator; the intent tag
  (`docs/research/222` §1) exists so that a crashed process can find its own order. Conflating them
  would mean losing one of the two obligations, so `L3.19` carries both and the code says which is
  which.

## 5. Order types the framework forbids

**NSE/MSD/67753 (2025-04-29) and the NSE FAQ: "Algo orders with order type as Market Order are not
permitted."** IOC and Market are likewise barred for algos in the commodity segment.

This is the single fact in this file that changes what the system may build. `L9.02`'s taxonomy is
still modelled in full — but **MARKET is a dated, sourced refusal for algo-originated flow**, not an
option the expression selector may pick, and the refusal names the circular. The practical
substitute is a marketable LIMIT (a limit priced through the touch by a derived, not typed,
tolerance), which is also the version whose cost `F01` can actually price — a market order's cost is
unknowable in advance by construction.

`market_protection` (Kite's own parameter, mandatory non-zero for MARKET/SL-M) exists for the same
underlying reason and stays relevant for SL-M.

## 6. Static IP — a precondition, not a detail

NSE/INVG/67858 paras A.1 and A.5: **static IP is mandatory for API access** by a tech-savvy investor
running their own algo. One primary address, an optional secondary for redundancy, changeable at
most once per calendar week, shareable only among "family" as SEBI defines it, with written and
2FA-verified consent.

**Design consequence:** the box's egress address is part of the order path's preconditions. It is
asserted before the first order of a session and refused loudly if it has moved, rather than
discovered as a rejection at 09:20.

## 7. What the broker must do to us

NSE/INVG/67858 para B.5, verbatim:

> "If the broker receives orders that exceed the Threshold OPS limit, the broker shall
> **reject/not accept/not process** any orders exceeding the OPS limit, in accordance with their
> policy."

Para B.6 requires the broker to be able to monitor and control the limit; para I.f makes the broker
"fully responsible and liable for all orders emanating through their … Client API".

So the failure mode of crossing the limit is **silent order loss at the broker**, not an error the
system can reason about. That is the argument for the limiter blocking locally: the alternative is
discovering the limit by having orders disappear.

The exchanges also retain a kill switch "for orders emanating from a particular algo id" — an
external actor can stop this system's flow without warning, which is one more reason reconciliation
can never assume that what it sent is what exists.

## 8. Also relevant, lower confidence

- **Order-to-trade ratio penalties** continue under the pre-existing master-circular mechanism, with
  a 2026-04-06 amendment easing the treatment of equity-option orders within ±40% of LTP-premium or
  ±₹20 (whichever is higher), and excluding designated market makers. VERIFIED-secondary only; the
  circular number style looked atypical and the PDF was not pulled.
- **Freeze quantities** per instrument are revised periodically by NSE (Nifty 1,800 / Bank Nifty 600
  / Fin Nifty 1,200 from 2025-12-01 / Midcap Nifty 2,800 / Nifty Next 50 600, as reported late
  2025). VERIFIED-secondary and time-sensitive: the engine must read the live figure rather than
  carry these, and they appear here only so the shape of the constraint is on record.

## 9. What this file does NOT establish

The first extension circular (…/2025/108) was triangulated from two secondary sources rather than
its own PDF. The exchange-versus-segment granularity of the threshold is genuinely ambiguous in the
primary text. Nothing here has been checked against a post-2026-04-01 amendment beyond a search that
found none.
