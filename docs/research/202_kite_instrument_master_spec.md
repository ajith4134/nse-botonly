# 202 — Kite instrument master + token-reuse guard

**Tasks:** todo `1.1` and `1.2` — one slice, because the token-reuse guard is a correctness property of
the master, not a separate feature. **Plan:** `L0.01`, `L0.02` (key corrected by `A.34`).

**R.23(b) classification:** an **ingest and store**, not an engine. There is no solver and no inference
procedure — it fetches a file, validates it, and persists it. Named accordingly so the vocabulary does not
inflate. It takes the full loop regardless, because it is decision-path: it defines what the entire system
believes is tradeable, and every downstream universe, scan and order references it.

---

## 1. What it does

Fetch the Kite instrument dump once per trading day (~08:30 IST, before the open), validate it, and persist
it as the authoritative record of every tradeable contract, keyed so that a contract's identity is stable
across days.

**Measured against the live endpoint 2026-08-10:** `https://api.kite.trade/instruments` returns HTTP 200,
9,289,898 bytes, **113,955 contracts**, with **no authentication** — verified by an unauthenticated
`curl`. This matters more than it sounds: it means `1.1` gets a genuine R.05 real-data pass immediately,
with no market session, no valid token, and no dependency on the daily TOTP login.

Columns: `instrument_token · exchange_token · tradingsymbol · name · last_price · expiry · strike ·
tick_size · lot_size · instrument_type · segment · exchange`.

## 2. The primary key — corrected by measurement before any code was written

`L0.02` originally specified `(exchange, tradingsymbol)`. Measured across all 113,955 rows:

| Candidate key | Collisions |
|---|---|
| `instrument_token` | 0 *within* one dump — but **reused across dumps** once a contract expires, so unusable as an identity |
| `(exchange, tradingsymbol)` | **1** |
| **`(exchange, segment, tradingsymbol)`** | **0** |
| `(exchange, segment, tradingsymbol, expiry, strike, instrument_type)` | 0 |

The single collision is real and instructive:

```
BSE:INFRA  token 139444228  "MIRAE ASSET MUTUAL FUND"  segment=BSE
BSE:INFRA  token    282377  "BSE INDEX INFRA"          segment=INDICES
```

Two different instruments, same exchange, same symbol, distinguished only by `segment`. Building on the
documented key would have **silently dropped one row on every daily ingest** while looking entirely
healthy — no error, no warning, one fewer instrument.

**Decision:** key on `(exchange, segment, tradingsymbol)`. `instrument_token` is stored and indexed for
fast subscription lookups, but is **never** the identity — it is Kite's transient handle, not the
contract's name. Also measured: `tradingsymbol` alone collides **8,910** times across exchanges, which is
why exchange is in the key at all.

## 3. The token-reuse guard

Kite reuses `instrument_token` after a contract expires. A store keyed on it would silently merge an
expired contract's history into a brand-new one — the same identity pointing at two different instruments
across time. The guard: on every ingest, detect any `instrument_token` that now maps to a **different**
`(exchange, segment, tradingsymbol)` than it did previously, and record the reassignment explicitly rather
than overwriting. A reassignment is a normal lifecycle event, not an error, but it must be *visible* —
silently accepting it is what corrupts history.

## 4. Design

- **Fetch** — plain HTTPS GET, no auth. Retries with backoff; a failed fetch never leaves a partial file.
- **Parse** — strict typing at the boundary: `expiry` to a date, `strike`/`tick_size`/`last_price` to
  `Decimal` (never float — R.03's money reasoning applies to strikes too), `lot_size` to int.
- **Validate** — reject a dump that is implausibly small, has lost a required column, or has zero rows for
  a previously-populated exchange. A truncated dump that parses is more dangerous than one that fails.
- **Persist** — SQLite, keyed as above, with the ingest date recorded so the master is queryable
  *as of a date* — the point-in-time property `L0.05`/`L0.06` build on.
- **Expose** — the tradeable universe per segment, so the six holons ask the master rather than
  each hand-rolling a contract list.

## 5. Acceptance criteria

1. Ingests the real 113,955-row dump end to end.
2. `(exchange, segment, tradingsymbol)` is enforced as unique; the `BSE:INFRA` pair survives as two rows.
3. A token reassignment is detected and recorded, not silently applied.
4. A truncated or column-missing dump is rejected, not partially ingested.
5. Strikes and tick sizes are `Decimal`; no float anywhere in the price path.
6. The master is queryable as-of a date, with all five index-option underlyings and 213 NFO underlyings
   present.

## 6. Sourcing record (R.16 / R.17 — mechanical, run 2026-08-10)

| Candidate | Verdict | Mechanical evidence |
|---|---|---|
| **Raw `https://api.kite.trade/instruments`** | **ADOPT** | Read `KiteConnect.instruments()` source and its route table: it hits `/instruments/{exchange}` through the authenticated client. The **raw endpoint needs no auth** — verified by unauthenticated curl returning HTTP 200 and 9.29 MB. Using it directly decouples the master from the daily TOTP login, so the universe is available even when the token has expired. This is the Kite-decoupled rule (`L3.28`) applied at the data layer. |
| `kiteconnect.instruments()` | **REJECT for ingest** | Tier-1: requires an authenticated client, which makes the whole universe unavailable whenever the daily token lapses — the exact failure the offline-diagnostics work (`L13.10`) was built to avoid. Retained for the *authenticated* paths where it is unavoidable. |
| `nsepython` 2.97 | **REJECT** | Tier-1 scope mismatch: it wraps NSE's own website endpoints, which are anti-bot gated (cookie + ~3 req/s) and do not carry Kite's `instrument_token` — the field the WebSocket subscription actually needs. Wrong source for this job. |
| `jugaad-data` 0.35.2 | **REJECT** | Same scope mismatch: NSE-website bhavcopy and historical data, no Kite instrument identity. Reconsider for `L0.23` bhavcopy ingest, not here. |

**SOTA analogs (R.23a):** Zipline's adjustments database and LEAN's Security Master — both keep a
point-in-time instrument record with explicit identity handling across corporate actions and symbol
changes. This slice is the identity-and-universe half of that; the corporate-action half is `L0.07`.

## 7. Deliberately out of scope

No corporate-action adjustment (`L0.07`), no point-in-time universe reconstruction (`L0.05`), no liquidity
tiering (`L5.21c`). Those consume this master; they are not part of it.
