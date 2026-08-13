# 222 · Kite Connect order API — the facts the order path is built against

Gathered 2026-08-13 for `F02` (`A.99`). Primary sources are kite.trade/docs, Zerodha support and the
`pykiteconnect` source; anything that could not be verified from a primary source is marked
**UNVERIFIED** and is treated in code as an assumption to be refuted, never as a fact.

**Mechanically confirmed locally** against the installed SDK (`kiteconnect` **5.2.1**, which is the
latest on PyPI as of today) — the signatures and constant tables below were read out of the installed
package, not copied from a page:

```
place_order(variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
            price=None, validity=None, validity_ttl=None, disclosed_quantity=None,
            trigger_price=None, iceberg_legs=None, iceberg_quantity=None, auction_number=None,
            algo_id=None, tag=None, market_protection=None)
modify_order(variety, order_id, parent_order_id=None, quantity=None, price=None, order_type=None,
             trigger_price=None, validity=None, disclosed_quantity=None, market_protection=None)
VARIETY: regular · amo · co · iceberg · auction        (no `bo` — Bracket Orders are gone)
PRODUCT: CNC · MIS · NRML · CO                          (no MTF constant in 5.2.1)
ORDER_TYPE: MARKET · LIMIT · SL · SL-M
VALIDITY: DAY · IOC · TTL
STATUS constants exposed: COMPLETE · CANCELLED · REJECTED  (only the terminal three)
GTT: GTT_TYPE_SINGLE · GTT_TYPE_OCO; statuses active/triggered/disabled/expired/cancelled/rejected/deleted
```

Two discrepancies between the docs and the installed SDK, recorded rather than resolved by
preference:

1. The documentation lists **MTF** as a product; SDK 5.2.1 has no `PRODUCT_MTF` constant. `product`
   is a plain string on the wire, so MTF is reachable without an SDK constant. The order path
   therefore carries its own product vocabulary and does not depend on SDK constants existing.
2. The SDK exposes only the three **terminal** statuses as constants. The transient statuses are
   documentation-only strings. This is exactly why the status map in the engine is keyed on the
   broker's literal string and refuses on an unrecognised one.

---

## 1. There is no idempotency key. This is the central fact of the feature.

`place_order` accepts no client-supplied order id, and returns only `{"order_id": ...}`. On a
timeout the caller cannot tell whether the order reached the OMS. Zerodha's own error string on that
path is verbatim:

> "Order request timed out. Please check the order book and confirm before placing again."

which is an instruction to reconcile, not to retry. Forum reports of a `503` returned while the
order was in fact placed are repeated and consistent (VERIFIED-secondary, multiple independent
threads).

**Consequence for the design:** the ambiguous outcome is not an error, it is a state
(`AMBIGUOUS` in §4 of `221`), and the recovery is a lookup keyed on our own tag, never a retry.

### `tag` — the only field we control

- **Alphanumeric, maximum 20 characters** (VERIFIED-primary).
- Not unique-enforced: Kite will happily accept two orders carrying the same tag. It is a
  correlation handle, not a constraint.
- 20 alphanumeric characters cannot hold a 32-hex-character UUID, so the intent identity on the wire
  is a truncated encoding and the **collision probability must be computed and stated in the code**
  rather than waved away.
- `algo_id` is a separate parameter and is the regulatory identifier, not ours. Whether it is
  required for every retail order or only for a registered >10-orders/sec algo is **UNVERIFIED** and
  is resolved in `docs/research/223`.

---

## 2. Lifecycle statuses

Transient: `PUT ORDER REQ RECEIVED` · `AMO REQ RECEIVED` · `VALIDATION PENDING` · `OPEN PENDING` ·
`MODIFY VALIDATION PENDING` · `MODIFY PENDING` · `TRIGGER PENDING` · `CANCEL PENDING` · `OPEN` ·
`MODIFIED`.

Terminal: **`COMPLETE` · `CANCELLED` · `REJECTED`**.

`status_message` is the cleaned reason (populated mainly on rejection); `status_message_raw` is the
exchange's own text. Both are kept — the raw one is the only version that survives Zerodha changing
its cleaning.

---

## 3. Postbacks are best-effort. Polling is the source of truth.

- Payload carries 31 fields including `order_id`, `status`, `filled_quantity`, `pending_quantity`,
  `average_price`, `exchange_timestamp`, `tag` and a `checksum`.
- **Checksum = `SHA256(order_id + order_timestamp + api_secret)`** and must be recomputed before the
  payload is believed (VERIFIED-primary).
- **Delivery guarantees are UNVERIFIED** — no documented retry count, ordering or at-least-once
  semantics, and failures to deliver are reported silently. The engine therefore treats a postback
  as a **low-latency hint** and `orders()`/`order_history()` as truth. Fills are applied idempotently
  by trade id precisely because at-least-once cannot be ruled out.
- For a single-user account the docs steer toward **order updates over the WebSocket ticker**;
  postback URLs are aimed at multi-user platforms.

---

## 4. Modify and cancel

- Regular order modifiable fields: `order_type`, `quantity`, `price`, `trigger_price`,
  `disclosed_quantity`, `validity`.
- **25 modifications per order** (VERIFIED-primary).
- The **modify-versus-fill race has no published atomicity guarantee** (UNVERIFIED as documented,
  repeatedly reported in practice): a modify or cancel in flight while the exchange fills the order
  yields `MODIFY PENDING`/`CANCEL PENDING` followed by `COMPLETE`, sometimes at a different quantity
  than requested. The engine must therefore re-read `order_history()` after every modify/cancel and
  never assume the requested state took effect — this is a state machine obligation, not an
  optimisation.

---

## 5. Rate limits

| Bucket | Limit |
|---|---|
| order placement | **10 orders/sec per trading account** — HTTP **429** when exceeded |
| orders per minute | **400** |
| orders per day | **5,000** |
| modifications per order | **25** |
| quote endpoint | 1 req/sec |
| historical candles | 3 req/sec |
| all other endpoints | 10 req/sec |

The 429 response **body shape is UNVERIFIED**; only the status code is documented, so the client
keys on the status code and records the body verbatim when it first sees one.

The 10/sec ceiling is not a courtesy limit — it is the regulatory threshold (`223`), which is why
the limiter blocks rather than drops.

---

## 6. Varieties, and what has been withdrawn

- **Bracket Orders (BO): discontinued March 2020.** No replacement. Absent from the variety list in
  SDK 5.2.1, confirmed locally.
- **Cover Orders (CO): still live.** Intraday only, NSE equity only (not BSE, not F&O), mandatory
  stop-loss leg within a trigger range, SL leg not independently cancellable once armed, auto
  square-off before close. (VERIFIED-primary for API presence, VERIFIED-secondary for constraints.)
- **AMO, iceberg (2–50 legs), auction** are current varieties. Iceberg requires both
  `iceberg_legs` and `iceberg_quantity`.
- **MTF** exists as a product per the docs but not as an SDK constant (see the discrepancy above).
- **`market_protection` must be non-zero for MARKET and SL-M** under the current algo rules;
  `market_protection=0` is rejected. `-1` means the broker default.

Each of these is carried in the engine as a **dated availability fact with its source**, so a
withdrawn variety is a named refusal and a returning one needs no code change (`R.03`).

---

## 7. Reconciliation surface

| Call | What it gives | Caveat that matters |
|---|---|---|
| `orders()` | the day's order book, latest state per order | one row per order, not per transition |
| `order_history(order_id)` | every status hop for one order | the only place the transient states are visible |
| `trades()` | the day's executions | no order filter |
| `order_trades(order_id)` | executions of one order | the partial-fill source |
| `positions()` | `net` and `day` arrays | `day` resets each session; equity carried overnight moves into `holdings()` next day |
| `holdings()` | DEMAT holdings | `t1_quantity` is bought-today-unsettled and is NOT sellable as `quantity` |

**Propagation latency is undocumented (UNVERIFIED).** The reconciler must therefore treat "absent
from the order book" as inconclusive until a **visibility horizon** has passed, and that horizon is
measured from real observations rather than assumed.

Reconciling positions requires the **union of `positions()` and `holdings()`**; either alone is a
partial view, and `t1_quantity` is the trap that makes a sell fail after a buy the same week.

---

## 8. Documented rejection surface

- Freeze quantity (F&O and cash): verbatim pattern observed —
  `"RMS:Rule: Check freeze quantity for NSE CASH"`. Limits are revised by NSE periodically (most
  recent bulletin found: 2026-02-01).
- Single-order cap: **100,000 shares** rejected above (VERIFIED-secondary).
- Price band / circuit rejections: category confirmed; **exact string UNVERIFIED**.
- Margin shortfall: maps to `MarginException` in the SDK; **exact string UNVERIFIED** and appears to
  vary by scenario.
- **`MarginException` does not exist in SDK 5.2.1** — found by building against it (2026-08-13). The
  SDK resolves an exception class by `getattr(exceptions, error_type, GeneralException)`, so a real
  margin rejection arrives today as a `GeneralException` with HTTP 500. The name is kept in the
  venue's classification table so it classifies correctly the day Zerodha starts sending it, and
  until then a margin rejection is classified UNKNOWN and reconciled rather than treated as a fact.
- SDK exception → HTTP: `TokenException` 403, `PermissionException` 403, `InputException` 400,
  `OrderException` 500, `DataException` 502 (garbled OMS response), `NetworkException` 503
  (OMS comms failure), `GeneralException` 500.

Because three of the five strings are unverified, the engine **classifies rejections by SDK
exception type and records the message verbatim**, and the string-matching table is built up from
observed rejections rather than authored from guesses. A rejection it cannot classify is
`UNCLASSIFIED_REJECTION` — surfaced, never silently bucketed.

---

## 9. Static IP

Order-placement endpoints require a whitelisted static IP under the current framework; other
endpoints do not. Configured account-wide, up to two addresses, one change per calendar week.
Effective date is reported inconsistently (2025-04-01 against a 2024-04-01 mention); the later date
is the credible one and the discrepancy is left visible here. **Operationally this means the box's
egress IP is part of the order path's preconditions and must be asserted before the first order of
the day, not discovered by a rejection.**

---

## 10. Not covered in this pass

WebSocket order-update payload schema field-by-field; the `auction` variety's full parameter set;
`place_gtt` request/response JSON in full; iceberg partial-fill semantics beyond the leg constraint.
Recorded here so the gap is visible rather than assumed closed.
