"""`R.05` for `A.113`'s fix — replay REAL recorded order arrivals through the rate limiter.

Not a test. The suite's property generates arrival gaps, which is the right way to search for a
counterexample and the wrong way to claim the limiter holds on this system's own traffic. This
replays the arrival pattern the paper sessions actually produced — every intent in every
`paper_verification` journal, at the instant it was decided — and checks the same invariant the
exchange would check: **no window ever holds more grants than it permits, counted from the
observer's clock rather than the limiter's own.**

`A.113` is the defect this exists to keep fixed: the limiter stamped items with its ratcheted
dispatch clock, which can read one millisecond ahead of the wall clock, so its window sat one tick
later than an observer's and admitted one extra order at the boundary. The fix widens the enforced
window by exactly one clock tick. A synthetic search found it; real flow is what proves it stays
found.

Usage:
    python scripts/verify_rate_limiter_on_recorded_order_flow.py
    python scripts/verify_rate_limiter_on_recorded_order_flow.py --session-root ~/somewhere/else
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from nse_algo_trader.order_path.broker_order_facility_facts import order_rate_limits
from nse_algo_trader.order_path.order_submission_rate_limiter import (
    OrderSubmissionRateLimiter,
)

DEFAULT_SESSION_ROOT = Path("~/.nse_algo_trader/paper_verification").expanduser()
MILLISECONDS_PER_SECOND = 1_000

INTENT_VALIDITY = timedelta(milliseconds=1)
"""How long each replayed intent stays valid — one clock tick, the smallest amount that is not
already expired when the limiter reads its own clock."""


@dataclass
class ReplayedArrivalClock:
    """Drives both of the limiter's clock seams from the recorded arrival instants.

    The limiter must see the flow at the instants it HAPPENED, not at the instants this script
    runs: replaying a day's orders in a second would measure this machine rather than the day.
    """

    epoch_seconds: float

    def __post_init__(self) -> None:
        self._monotonic_ns = 1_000_000_000

    def wall_clock_epoch_seconds(self) -> float:
        return self.epoch_seconds

    def monotonic_nanoseconds(self) -> int:
        return self._monotonic_ns

    def advance_to(self, epoch_seconds: float) -> None:
        step = max(epoch_seconds - self.epoch_seconds, 0.0)
        self.epoch_seconds += step
        self._monotonic_ns += int(step * 1_000_000_000)


def recorded_arrivals(journal_path: Path) -> list[datetime]:
    """Every intent this journal holds, in the order it was decided."""
    with sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT decided_at FROM order_intent ORDER BY decided_at"
        ).fetchall()
    return [datetime.fromisoformat(str(row[0])) for row in rows]


def replay_one_session(
    session_date: date, arrivals: list[datetime]
) -> tuple[int, int, list[str]]:
    """Push one session's arrivals through a real limiter; return grants, refusals and breaches."""
    if not arrivals:
        return 0, 0, []
    clock = ReplayedArrivalClock(epoch_seconds=arrivals[0].timestamp())
    with tempfile.TemporaryDirectory() as scratch:
        limiter = OrderSubmissionRateLimiter(
            session_date,
            published_limits=order_rate_limits(on=session_date),
            store_path=Path(scratch) / "rate.sqlite3",
            wall_clock_epoch_seconds=clock.wall_clock_epoch_seconds,
            monotonic_nanoseconds=clock.monotonic_nanoseconds,
        )
        try:
            granted_at: list[int] = []
            refused = 0
            for arrival in arrivals:
                clock.advance_to(arrival.timestamp())
                # One tick of validity, not zero: a deadline EQUAL to the arrival instant has
                # already passed by the time the limiter reads its own clock, so every order would
                # expire before queueing and the invariant would hold vacuously over zero grants.
                # One tick means nothing ever waits, so the pattern put to the limiter is exactly
                # the one the session produced.
                outcome = limiter.acquire("NSE", arrival + INTENT_VALIDITY)
                if outcome.granted:
                    granted_at.append(int(clock.wall_clock_epoch_seconds() * 1_000))
                else:
                    refused += 1

            breaches: list[str] = []
            for window in limiter.binding_windows:
                for index, stamp in enumerate(granted_at):
                    inside = sum(
                        1
                        for earlier in granted_at[: index + 1]
                        if earlier >= stamp - window.window_milliseconds
                    )
                    if inside > window.permitted_orders:
                        breaches.append(
                            f"{inside} grants inside a {window.window_seconds}s window that "
                            f"permits {window.permitted_orders}, ending at {stamp}"
                        )
            return len(granted_at), refused, breaches
        finally:
            limiter.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", type=Path, default=DEFAULT_SESSION_ROOT)
    arguments = parser.parse_args()

    if not arguments.session_root.exists():
        print(f"no recorded sessions under {arguments.session_root}")
        return 1

    total_breaches = 0
    sessions_checked = 0
    for directory in sorted(arguments.session_root.iterdir()):
        journal = directory / "journal.sqlite3"
        if not journal.exists():
            continue
        try:
            session_date = date.fromisoformat(directory.name)
        except ValueError:
            continue
        arrivals = recorded_arrivals(journal)
        granted, refused, breaches = replay_one_session(session_date, arrivals)
        sessions_checked += 1
        total_breaches += len(breaches)
        span = (
            f"{(arrivals[-1] - arrivals[0])}"
            if len(arrivals) > 1
            else "a single instant"
        )
        print(
            f"{session_date.isoformat()}: {len(arrivals)} recorded arrivals over {span} — "
            f"{granted} granted, {refused} refused, {len(breaches)} breach(es)"
        )
        for breach in breaches:
            print(f"    BREACH {breach}")

    if sessions_checked == 0:
        print(f"no journal found under {arguments.session_root}")
        return 1
    print(
        f"\n{sessions_checked} session(s) replayed through the real limiter, "
        f"{total_breaches} breach(es)"
    )
    return 0 if total_breaches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
