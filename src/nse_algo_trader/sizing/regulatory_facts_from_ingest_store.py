"""What the exchange has published about an instrument today — read, never inferred (`L7.06`).

The risk gate's tier 1 is regulatory walls, and until this module existed nothing assembled them:
every verdict reported `fo_ban_list`, `mwpl_position_limits` and `circuit_bands` as **UNCHECKED**,
which was honest and useless in equal measure. The readers were already there
(`BitemporalIngestStore.rows_for`) and the data is already ingested — eight symbols are on the ban
list today — so the gate could not refuse a banned scrip for want of forty lines of joining.

**The distinction this module exists to preserve.** `None` and `False` are different answers:

* the source was **not read at all** for this date → `None`, and the gate shouts UNCHECKED;
* the source **was read** and the symbol is not in it → `False`, and the gate is satisfied.

Collapsing those is how a system trades a banned scrip confidently on the morning the ban file
failed to download. Every field here is `None` unless its source produced rows for the date asked
for, and the presence of ANY row for a source is what makes that source "read".

**Point-in-time, always.** `rows_for(..., known_by=)` is passed through, so a decision replayed for
a past session sees the ban list as it was known THEN. A ban declared after the decision must not
retro-refuse the trade that preceded it, or every backtest quietly improves.

**Absent is not unchecked, either.** A scrip that is not in the F&O segment has no MWPL row, and
that is a legitimate absence rather than a failure to look: the threshold is published whenever the
source was read, so `RegulatoryFacts.unread_sources` keys off THAT and every cash-only scrip stops
reporting an unchecked wall it does not have.

**What this module does NOT provide, stated rather than left to be discovered.** The
`circuit_band_asm_gsm` source carries ASM/GSM **surveillance** rows — stage, code, description —
and does **not** carry the day's upper and lower circuit prices. So `surveillance_stage` is
populated and the two circuit price bounds stay `None`, and the gate keeps reporting
`circuit_bands` unchecked.
That is the truth about the data; the named consumer for real band prices is the bhavcopy's own
price bands, tracked in `BACKLOG.md`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import (
    BitemporalIngestStore,
    StoredObservation,
)
from nse_algo_trader.sizing.pre_trade_risk_gate import RegulatoryFacts

FO_BAN_LIST_SOURCE = "fo_ban_list"
MWPL_SOURCE = "mwpl_position_limits"
SURVEILLANCE_SOURCE = "circuit_band_asm_gsm"

MWPL_BAN_THRESHOLD_FRACTION = Decimal("0.95")
"""A scrip enters the F&O ban period when market-wide open interest crosses 95% of the MWPL.

A REGULATORY fact, not a derived threshold — it is the exchange's own trigger, published in NSE's
market-wide position limit framework, and `R.03` exempts a sourced regulatory constant precisely so
that numbers like this are written down rather than fitted. It is stated here so that a system
reading 94% knows it is close rather than merely below something.
"""

_MWPL_UTILISATION_NUMERATOR = "Future Equivalent Open Interest"
_MWPL_DENOMINATOR = "MWPL"
_MWPL_SYMBOL = "NSE Symbol"


def assemble_regulatory_facts(
    *,
    trading_symbol: str,
    ingest_store: BitemporalIngestStore,
    effective_date: date,
    known_by: datetime | None = None,
) -> RegulatoryFacts:
    """Everything tier 1 needs about one instrument, or `None` for each source that was not read.

    Never raises for an absent source. An absent source is a FACT the gate must report, and turning
    it into an exception would push the caller toward a bare `except` that ends in a default of
    "not banned" — the exact substitution this module exists to prevent.
    """
    symbol = trading_symbol.strip().upper()

    ban_rows = _rows(ingest_store, FO_BAN_LIST_SOURCE, effective_date, known_by)
    is_banned: bool | None = None
    if ban_rows is not None:
        is_banned = any(
            str(row.values.get("symbol", "")).strip().upper() == symbol for row in ban_rows
        )

    mwpl_rows = _rows(ingest_store, MWPL_SOURCE, effective_date, known_by)
    utilisation: Decimal | None = None
    threshold: Decimal | None = None
    if mwpl_rows is not None:
        threshold = MWPL_BAN_THRESHOLD_FRACTION
        for row in mwpl_rows:
            if str(row.values.get(_MWPL_SYMBOL, "")).strip().upper() != symbol:
                continue
            numerator = _as_decimal(row.values.get(_MWPL_UTILISATION_NUMERATOR))
            denominator = _as_decimal(row.values.get(_MWPL_DENOMINATOR))
            if numerator is not None and denominator is not None and denominator > 0:
                utilisation = numerator / denominator
            break
        else:
            # The source was read and this symbol is not in it. For MWPL that means the scrip is
            # not in the F&O segment at all, which is not zero utilisation — it is no utilisation.
            # Reported as read-but-absent by leaving the fraction None while the threshold stands.
            utilisation = None

    surveillance_rows = _rows(ingest_store, SURVEILLANCE_SOURCE, effective_date, known_by)
    stage: str | None = None
    if surveillance_rows is not None:
        for row in surveillance_rows:
            key = row.natural_key
            if key and str(key[0]).strip().upper() == symbol:
                stage = str(row.values.get("asm_stage") or row.values.get("asm_term") or "listed")
                break

    return RegulatoryFacts(
        trading_symbol=symbol,
        is_fo_banned=is_banned,
        mwpl_utilisation_fraction=utilisation,
        mwpl_breach_threshold_fraction=threshold,
        # Deliberately absent: the ASM/GSM feed carries surveillance stages, not the day's price
        # bands. See the module docstring — the gate keeps saying `circuit_bands` UNCHECKED, which
        # is true.
        lower_circuit_price_rupees=None,
        upper_circuit_price_rupees=None,
        surveillance_stage=stage,
    )


def _rows(
    store: BitemporalIngestStore, source: str, effective_date: date, known_by: datetime | None
) -> list[StoredObservation] | None:
    """The source's rows for the date, or `None` when the source produced none at all.

    `None` means UNREAD; an empty list would mean "read and empty", and no NSE source publishes an
    empty file on a trading day — so the two are collapsed here deliberately and the conservative
    reading (unread) wins.
    """
    rows = store.rows_for(source, effective_date, known_by)
    return list(rows) if rows else None


def _as_decimal(value: object) -> Decimal | None:
    """A `Decimal` or nothing. A malformed figure is absent, never zero."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
