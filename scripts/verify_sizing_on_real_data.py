"""`R.05` for `F03` — run the sizer and the gate over the REAL universe and report what happened.

Not a test. The suite is hermetic by design, so `R.05` counts it as functional verification only;
this is the pass that puts real bars, real lot sizes and real calibrations through the same code and
reports the answer honestly, including every refusal and every input that could not be found.

`R.09`: every instrument that has both a lot size and recorded bars, not a chosen few. A sizer that
works on RELIANCE and divides by zero on an illiquid scrip is not verified, and the only way to know
which one this is, is to run all of them.

What a PASS looks like — stated before running, so the bar cannot move afterwards:

1. **No unhandled exception on any instrument.** A refusal is a result; a traceback is a defect.
2. **No sized position costs more than the capital it was sized against.** The one invariant that
   must never break.
3. **Every quantity is a whole multiple of the instrument's own lot size.**
4. **Every zero quantity carries a stated reason**, and every non-zero one carries none.
5. **Every failure to assemble inputs names the store that could not answer**, so the gaps are a
   work-list rather than a mystery.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from nse_algo_trader.broker_credentials import load_env_file_into_environ
from nse_algo_trader.capital_configuration import load_trading_capital_from_environment
from nse_algo_trader.sizing.sizing_inputs_from_real_stores import (
    SizingInputAssemblyError,
    assemble_sizing_inputs,
    tradeable_symbols,
)
from nse_algo_trader.sizing.volatility_targeted_position_sizer import (
    PositionSizingError,
    VolatilityTargetedPositionSizer,
)

IST = ZoneInfo("Asia/Kolkata")

SEGMENTS = 6
"""`R.10` — the six segments are equal by default, so each may hold one concurrent position."""


def main() -> int:
    load_env_file_into_environ()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=datetime.now(IST).date(),
        help="the session to size as of; reads every store point-in-time at this date",
    )
    parser.add_argument("--horizon-bars", type=int, default=5)
    parser.add_argument(
        "--limit", type=int, default=None, help="cap the universe (for a quick smoke run only)"
    )
    arguments = parser.parse_args()

    capital = load_trading_capital_from_environment()
    as_of = datetime.combine(arguments.as_of, datetime.min.time(), tzinfo=IST).replace(
        hour=15, minute=30
    )

    universe = tradeable_symbols(as_of=arguments.as_of, limit=arguments.limit)
    print(f"universe: {len(universe):,} instrument(s) with both a lot size and recorded bars")
    if not universe:
        print("FAIL: nothing to size — the instrument master and the bar store do not overlap")
        return 1

    sizer = VolatilityTargetedPositionSizer()
    sized_count = 0
    zero_count = 0
    missing: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    sizing_refusals: Counter[str] = Counter()
    violations: list[str] = []
    tracebacks: list[str] = []
    largest_notional = Decimal(0)
    largest_symbol = ""

    for token, symbol, lot_size in universe:
        try:
            inputs = assemble_sizing_inputs(
                instrument_token=token,
                trading_symbol=symbol,
                lot_size=lot_size,
                deployable_rupees=capital.total_rupees,
                concurrent_position_capacity=SEGMENTS,
                horizon_bars=arguments.horizon_bars,
                as_of=as_of,
            )
        except SizingInputAssemblyError as failure:
            for store in failure.missing:
                missing[store] += 1
            continue
        except Exception as failure:  # noqa: BLE001 — an unhandled type IS the finding
            tracebacks.append(f"{symbol}: assembly raised {type(failure).__name__}: {failure}")
            continue

        try:
            sized = sizer.size(inputs)
        except PositionSizingError as failure:
            sizing_refusals[type(failure).__name__] += 1
            refusals[str(failure)[:60]] += 1
            continue
        except Exception as failure:  # noqa: BLE001 — an unhandled type IS the finding
            tracebacks.append(f"{symbol}: sizing raised {type(failure).__name__}: {failure}")
            continue

        # Claim 2 — the invariant that must never break.
        cost = Decimal(sized.quantity) * inputs.reference_price_rupees
        if cost > inputs.deployable_rupees:
            violations.append(
                f"{symbol}: {sized.quantity} x {inputs.reference_price_rupees} = {cost} exceeds "
                f"deployable {inputs.deployable_rupees}"
            )
        # Claim 3 — whole lots.
        if sized.quantity % lot_size != 0:
            violations.append(
                f"{symbol}: quantity {sized.quantity} is not a multiple of {lot_size}"
            )
        # Claim 4 — zero and refused agree.
        if (sized.quantity == 0) != (sized.refusal_reason is not None):
            violations.append(
                f"{symbol}: quantity {sized.quantity} disagrees with refusal "
                f"{sized.refusal_reason!r}"
            )

        if sized.quantity > 0:
            sized_count += 1
            if sized.notional_rupees > largest_notional:
                largest_notional = sized.notional_rupees
                largest_symbol = symbol
        else:
            zero_count += 1
            refusals[(sized.refusal_reason or "")[:60]] += 1

    print(f"sized a position: {sized_count:,}")
    print(f"sized zero with a stated reason: {zero_count:,}")
    print(f"could not assemble inputs: {sum(missing.values()):,}")
    for store, count in missing.most_common():
        print(f"    missing {store}: {count:,}")
    print(f"sizer refused outright: {sum(sizing_refusals.values()):,}")
    for reason, count in refusals.most_common(8):
        print(f"    {count:>6,}  {reason}")
    if largest_symbol:
        print(f"largest position: {largest_symbol} at Rs {largest_notional:,.2f}")

    failed = False
    if tracebacks:
        failed = True
        print(f"\nFAIL claim 1 — {len(tracebacks)} unhandled exception(s):")
        for line in tracebacks[:10]:
            print(f"    {line}")
    if violations:
        failed = True
        print(f"\nFAIL claims 2-4 — {len(violations)} invariant violation(s):")
        for line in violations[:10]:
            print(f"    {line}")

    if failed:
        return 1
    print("\nPASS: no unhandled exception, no unfundable position, no part lot, no silent zero")
    return 0


if __name__ == "__main__":
    sys.exit(main())
