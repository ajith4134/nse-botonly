#!/usr/bin/env python
"""`M14` `R.05` — run the bar/tape join verification on the real recorded sessions.

Engine: `nse_algo_trader.market_depth.bar_tape_join_verification_engine`. Spec:
`docs/research/236`.

**Why this is a script and not a test.** The comparison it makes is between two real stores on
this machine — 1.3 GiB of parquet and a million bars — and its answer is a finding about those
stores, not a property of the code. The engine's own properties are asserted in
`tests/test_bar_tape_join_verification_engine.py`; what runs here is the `R.05` pass.

The verdicts are written to a SQLite store so the paper loop and the dashboard read the SAME
answer this run produced, rather than each recomputing it from a 1.3 GiB tape.

Usage:
    python scripts/verify_bar_tape_join_on_real_data.py 2026-08-12 --significance 0.01 \
        --staleness-quantile 0.95 --minimum-detectable-disagreement-rate 0.5
    python scripts/verify_bar_tape_join_on_real_data.py --all --significance 0.01 \
        --staleness-quantile 0.95 --minimum-detectable-disagreement-rate 0.5

    # A probe. Prints and exits WITHOUT persisting — a sample must never read as a verification.
    python scripts/verify_bar_tape_join_on_real_data.py --all --significance 0.01 \
        --staleness-quantile 0.95 --minimum-detectable-disagreement-rate 0.5 --limit 40

Three policy inputs, none with a default (`R.03`): how surprised to be before refusing
(`--significance`), which book counts as current (`--staleness-quantile`, and `B6` is what happens
when it disagrees with the fill path), and what a verified join CLAIMS
(`--minimum-detectable-disagreement-rate`, `A.124`).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from nse_algo_trader.market_depth.bar_tape_join_verdict_store import (
    DEFAULT_VERDICT_STORE,
    write_session_verdicts,
)
from nse_algo_trader.market_depth.bar_tape_join_verification_engine import (
    BarComparisonClass,
    BarTapeJoinVerificationEngine,
    InstrumentJoinVerdict,
    SessionJoinVerificationReport,
    VolumeReconciliationClass,
    preload_session_snapshots,
    stored_bars_for_session,
)
from nse_algo_trader.market_depth.market_depth_tape_store import MarketDepthTapeReader
from nse_algo_trader.replay_session_clock import session_for

DEFAULT_DEPTH_TAPE_ROOT = Path("~/nse_archive/depth_tape").expanduser()

# B6 (`docs/research/240`): this WAS a hardcoded 0.99 carrying a comment claiming it matched the
# paper loop. The paper loop runs at 0.95 (`scripts/verify_paper_session_on_real_data.py`), so the
# join was being verified at a LOOSER threshold than fills obey — verbatim the failure the engine's
# own docstring says must never happen, and the reason that R.05 pass was invalid. It is now a
# required argument with no default, exactly like `--significance`: both are policy (`R.03`).


def verify_one_session(
    *,
    session_date: date,
    significance: float,
    minimum_detectable_disagreement_rate: float,
    staleness_quantile: float,
    tape_root: Path,
    market_data: Path | None,
    instrument_limit: int | None,
) -> SessionJoinVerificationReport:
    """Compare one session's two stores and return the report.

    The instrument set is the tape's, not the bar store's: a bar with no tape cannot be verified
    and would only inflate the unverifiable count with instruments that were never in scope.
    """
    reader = MarketDepthTapeReader(tape_root)
    tokens = sorted(reader.instrument_tokens(session_date))
    if instrument_limit is not None:
        tokens = tokens[:instrument_limit]
    if not tokens:
        raise SystemExit(f"the tape holds no instrument for {session_date.isoformat()}")

    bars_by_instrument = (
        stored_bars_for_session(
            session_date=session_date, instrument_tokens=tokens, market_data=market_data
        )
        if market_data is not None
        else stored_bars_for_session(session_date=session_date, instrument_tokens=tokens)
    )
    # An instrument the tape recorded and the bar store missed is reported, not dropped: a
    # silent omission would make the coverage read better than it is (`R.11`).
    for token in tokens:
        bars_by_instrument.setdefault(token, ())

    # ONE streamed pass over the session (`M25`). Reading per instrument re-scans the day's parquet
    # for every one of them, which did not finish 2026-08-12's 1,420 instruments in ninety minutes.
    session = session_for(session_date)
    snapshots = preload_session_snapshots(
        reader,
        session_date=session_date,
        instrument_tokens=tokens,
        bars_by_instrument=bars_by_instrument,
        staleness_quantile=staleness_quantile,
        # Widened either side of the session: the recorder starts before the open on a good day and
        # a packet stamped outside the session is still evidence about the instrument.
        window_start=session.opens_at - timedelta(hours=4),
        window_end=session.closes_at + timedelta(hours=4),
    )

    engine = BarTapeJoinVerificationEngine(
        snapshots,
        session_date=session_date,
        significance=significance,
        minimum_detectable_disagreement_rate=minimum_detectable_disagreement_rate,
    )
    return engine.verify_session(bars_by_instrument)


B1_EVIDENCE_REPORT_BUCKETS = (2, 5, 10)
"""The counts `A.122` cited when it raised `B1` — 14 instruments verified on 2 comparisons and 130
on 10 or fewer. Reported at the same edges so the new run is directly comparable with the old
finding. A reporting choice, not a threshold: nothing downstream reads these."""


def report_lines(report: SessionJoinVerificationReport) -> list[str]:
    lines = [report.describe()]
    lines.append("  classes:")
    for comparison_class in BarComparisonClass:
        count = report.class_counts[comparison_class]
        if count:
            share = count / report.bars_total if report.bars_total else 0.0
            lines.append(f"    {comparison_class.value:<38} {count:>8}  {share:6.2%}")
    lines.append("  volume:")
    for volume_class in VolumeReconciliationClass:
        count = report.volume_class_counts[volume_class]
        if count:
            lines.append(f"    {volume_class.value:<38} {count:>8}")
    if report.deviation_in_tolerances_deciles:
        deciles = " ".join(f"{value:.2f}" for value in report.deviation_in_tolerances_deciles)
        lines.append(f"  |deviation| in spread units, deciles: {deciles}")
    # `B1`, measured rather than argued (`A.123`, `docs/research/241` §1.1). The beta-binomial null
    # raises the evidence bar only where the pooled rate and the dispersion are BOTH large, so
    # whether `JOIN_VERIFIED` is still reachable on a handful of bars is a question about THIS
    # session's numbers. Printing the thin tail is how the run answers it.
    verified_evidence = sorted(
        item.comparisons_verifiable
        for item in report.instruments
        if item.verdict is InstrumentJoinVerdict.JOIN_VERIFIED
    )
    if verified_evidence:
        buckets = " ".join(
            f"on<={edge}={sum(1 for count in verified_evidence if count <= edge)}"
            for edge in B1_EVIDENCE_REPORT_BUCKETS
        )
        lines.append(
            f"  B1 evidence behind {len(verified_evidence)} VERIFIED instruments: "
            f"fewest={verified_evidence[0]}  "
            f"median={verified_evidence[len(verified_evidence) // 2]}  {buckets}"
        )
    refused = sorted(report.refuted_instruments())
    if refused:
        lines.append(f"  REFUTED tokens ({len(refused)}): {refused[:25]}")
    divergent = report.price_basis_divergences()
    if divergent:
        lines.append(f"  PRICE-BASIS DIVERGENCE ({len(divergent)}) — the bar series is rescaled:")
        for item in sorted(divergent, key=lambda row: row.price_basis_ratio or 0.0):
            lines.append(
                f"    token {item.instrument_token:<12} ratio={item.price_basis_ratio:.5f} "
                f"on {item.disagreements}/{item.comparisons_verifiable} bars"
            )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_date", nargs="?", help="the trading day to verify, YYYY-MM-DD")
    parser.add_argument(
        "--all", action="store_true", help="verify every session the depth tape holds"
    )
    parser.add_argument(
        "--staleness-quantile",
        type=float,
        required=True,
        help="operator policy: which quantile of an instrument's own packet gaps counts as stale. "
        "MUST match the value the fill path runs at, or the join is verified in a market the loop "
        "never trades in (`R.05`)",
    )
    parser.add_argument(
        "--significance",
        type=float,
        required=True,
        help="operator policy: the tail below which an instrument's join is refused (`R.03` — "
        "the estimator is derived, the risk appetite is yours)",
    )
    parser.add_argument(
        "--minimum-detectable-disagreement-rate",
        type=float,
        required=True,
        help="operator policy: what JOIN_VERIFIED CLAIMS (`A.124`). A verified instrument is one "
        "this session held enough evidence to have CAUGHT, had it disagreed at this rate. Not a "
        "threshold on the data — the definition of the claim, which is why it has no default. "
        "Measured across the three recorded sessions: 1.0 needs 2 comparisons (verifies only that "
        "an instrument is not a token collision or a rescaled series), 0.5 needs 19-25, 0.25 needs "
        "136-525 and is unaffordable against a median of 57",
    )
    parser.add_argument("--tape-root", type=Path, default=DEFAULT_DEPTH_TAPE_ROOT)
    parser.add_argument("--market-data", type=Path, default=None)
    parser.add_argument("--verdict-store", type=Path, default=DEFAULT_VERDICT_STORE)
    parser.add_argument(
        "--limit", type=int, default=None, help="probe only this many instruments, reported as one"
    )
    arguments = parser.parse_args()

    if arguments.all:
        sessions = MarketDepthTapeReader(arguments.tape_root).session_dates()
    elif arguments.session_date:
        sessions = [date.fromisoformat(arguments.session_date)]
    else:
        parser.error("give a session date or --all")

    for session_date in sessions:
        report = verify_one_session(
            session_date=session_date,
            significance=arguments.significance,
            minimum_detectable_disagreement_rate=(arguments.minimum_detectable_disagreement_rate),
            staleness_quantile=arguments.staleness_quantile,
            tape_root=arguments.tape_root,
            market_data=arguments.market_data,
            instrument_limit=arguments.limit,
        )
        if arguments.limit is not None:
            # B7 (`docs/research/240`): a probe used to be written into the same table with no
            # marker, so `verification_coverage_for` returned a non-None coverage and the paper
            # loop took the VERIFIED branch — trading 9,000 instruments on 40 sampled ones. The
            # "probe only" warning went to stderr, where nothing reads it. A probe is now never
            # persisted: it prints and exits, so it cannot be mistaken for a verification.
            print(
                "  PROBE ONLY — verdicts NOT written to the store. Re-run without --limit to "
                "produce a verification the loop may rely on.",
                file=sys.stderr,
            )
        else:
            write_session_verdicts(
                report,
                arguments.verdict_store,
                staleness_quantile=arguments.staleness_quantile,
            )
        for line in report_lines(report):
            print(line, flush=True)
        if arguments.limit is not None:
            recorded = len(
                MarketDepthTapeReader(arguments.tape_root).instrument_tokens(session_date)
            )
            print(
                f"  NOTE: probe only — {arguments.limit} instruments of {recorded} recorded. "
                f"Not a session verdict.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
