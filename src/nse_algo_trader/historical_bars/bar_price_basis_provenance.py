"""`L0.37` — what the prices in `price_bars` actually MEAN, recorded rather than assumed.

Spec: `docs/research/239`. Defect: `docs/research/237`, `238`, backlog `M26`, operator decision
`A.123` decision 3.

**The defect, exactly.** `price_bars` holds ten columns and none of them says what the prices are
denominated in. Kite's historical endpoint returns a series adjusted for corporate actions **as of
the moment it is asked**, and `scripts/backfill_five_minute_bars.py` writes that series verbatim.
So two rows that look identical can mean different things: a bar fetched on its own session holds
what actually TRADED, while the same bar re-fetched after an ex-date holds what would have traded
on a later adjusted basis. `HINDPETRO` was measured at **0.95099 of the traded price** on 150 of
150 comparable bars across three sessions, triangulated against NSE bhavcopy — which agreed with
the depth tape, not with the bar store.

The paper loop takes its SIGNAL from these bars and every FILL from the depth tape. When the two
are on different bases the strategy is being measured against a market that did not happen, and a
uniform 4.9% shift leaves every deviation, every fitted reversion and every chart looking entirely
normal while the fill happens 4.9% away. Nothing else in this system would have caught it.

**`availability_time` does not answer this.** It records when a bar became KNOWABLE, which is a
statement about time travel, not about denomination. A bar can be perfectly available and still be
quoted in a basis that did not exist on the day it describes.

**Why this is a provenance record and not an engine (`R.23(b)`).** It carries no solver and no
state between decisions: it annotates a write, classifies from two dates, and refuses a read. It is
named for what it is. The thing it feeds — the paper loop's admissibility — is where the behaviour
lives.

**What this deliberately does NOT do**, per the spec:

- *Repair the adjusted series by dividing the factor out.* The factor is fitted from the bars that
  DISAGREED, on the sessions the depth tape happens to cover. Applying it to a whole history would
  extrapolate beyond its evidence, create a third basis that is neither traded nor adjusted, and
  destroy the only record that anything was ever wrong. The store records what it has.
- *Interpret a mismatch.* Saying WHICH corporate action caused a rescaling needs a feed
  (`corporate_action` holds 0 rows). Detecting and recording the basis does not, which is the whole
  reason this is buildable today.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

FIVE_MINUTE_BAR_INTERVAL = "5m"
"""The label `price_bars.bar_interval` actually carries, measured against the live store rather
than assumed — a first draft of this module defaulted to `"five_minute"` and every read returned
nothing, silently, because a store with no matching rows is indistinguishable from a session that
was never backfilled. `scripts/backfill_five_minute_bars.py` imports this rather than defining its
own, so the writer and the readers cannot drift apart."""

PRICE_BASIS_COLUMN = "adjustment_basis_as_of"
"""The date the source's adjustments were current as of — for a Kite fetch, the fetch date.

Knowable at write time with no corporate-action feed, which is what makes the defect visible at
all: a bar is on the traded basis IFF this equals its session date.
"""


class PriceBasisError(Exception):
    """A basis that cannot describe the bars it is attached to."""


class PriceBasis(Enum):
    """Which series a stored price belongs to — three states, because "cannot tell" is one.

    Folding `UNKNOWN` into `TRADED` is precisely the defect (`A.41` inverted): the store would be
    asserting a fact about prices nobody recorded a basis for.
    """

    TRADED = "traded"
    """Fetched on its own session, so no corporate action can have fallen between the trade and
    the fetch. These are the prices a fill would actually have happened at."""

    ADJUSTED_AFTER_THE_SESSION = "adjusted_after_the_session"
    """Fetched later, so the source may have rescaled it for an action in between.

    Deliberately not called `RESCALED`: whether an action actually fell in the gap is a question
    for a corporate-action feed this project does not have. What the dates alone establish is that
    it COULD have, which is enough to stop treating the price as traded."""

    UNKNOWN = "unknown"
    """No basis recorded. True of all 1,022,751 rows written before this feature, and PERMANENTLY
    so — the backfill dates were never stored and Kite will not re-serve those sessions
    unadjusted. Unknown is not a defect to be cleared; it is a fact to be respected."""


@dataclass(frozen=True, slots=True)
class SessionPriceBasisCoverage:
    """What one session's bars are denominated in — the dashboard's row and the loop's warning.

    Three counts rather than one fraction, because the two shortfalls need different actions:
    bars fetched late can be fetched on the right day going forward, and bars with no basis at all
    never can.
    """

    session_date: date
    bars_on_the_traded_basis: int
    bars_adjusted_after_the_session: int
    bars_on_an_unknown_basis: int
    bars_on_an_impossible_basis: int
    """Rows whose basis PREDATES the session they describe — a store defect, not an old fetch.

    Counted rather than raised (`H3` of the `A.126` review). `classify_price_basis` is right to
    raise: it judges one bar and an impossible one is a defect. But this aggregate feeds
    `/microstructure` and the paper loop's admission, and letting a single bad row propagate made
    209,912 honest rows unreportable and returned HTTP 500 from the surface. A store defect must be
    SHOWN, not hidden behind an outage."""
    instruments_at_risk: frozenset[int]
    """Instruments holding at least one bar adjusted after its own session — the condition, stated
    over dates, so it fires on the next occurrence without being told about `HINDPETRO`."""
    latest_adjustment_basis: date | None
    """The furthest-out basis date any bar in this session carries. The size of the gap is the size
    of the exposure: an action anywhere in it may have rescaled the series."""

    @property
    def bars_total(self) -> int:
        return (
            self.bars_on_the_traded_basis
            + self.bars_adjusted_after_the_session
            + self.bars_on_an_unknown_basis
            + self.bars_on_an_impossible_basis
        )

    @property
    def traded_fraction(self) -> float:
        """Share of this session's bars known to hold what actually traded.

        Never `None`: this type is only constructed for a session that HAS bars, and a session with
        none returns `None` from `price_basis_coverage_for` instead — an unbackfilled session has
        no fraction, and reporting 0% would read as a measured failure of a run that never happened.
        """
        return self.bars_on_the_traded_basis / self.bars_total

    def describe(self) -> str:
        return (
            f"{self.session_date.isoformat()}: {self.traded_fraction:.1%} of "
            f"{self.bars_total:,} bars on the traded basis "
            f"({self.bars_adjusted_after_the_session:,} adjusted later, "
            f"{self.bars_on_an_unknown_basis:,} unknown, "
            f"{self.bars_on_an_impossible_basis:,} impossible) · "
            f"{len(self.instruments_at_risk)} instrument(s) at risk"
        )


def classify_price_basis(session_date: date, adjustment_basis_as_of: date | None) -> PriceBasis:
    """Which series a bar belongs to, from the two dates alone.

    Raises:
        PriceBasisError: the basis predates the session. A source cannot have adjusted a series as
            of a date before the bars existed, so this is a store defect rather than an old fetch,
            and classifying it `TRADED` would hide it behind a reassuring word.
    """
    if adjustment_basis_as_of is None:
        return PriceBasis.UNKNOWN
    if adjustment_basis_as_of < session_date:
        raise PriceBasisError(
            f"a bar for {session_date.isoformat()} cannot be adjusted as of "
            f"{adjustment_basis_as_of.isoformat()}, which is before the session it describes"
        )
    if adjustment_basis_as_of == session_date:
        return PriceBasis.TRADED
    return PriceBasis.ADJUSTED_AFTER_THE_SESSION


def ensure_price_basis_column(connection: sqlite3.Connection) -> None:
    """Add `adjustment_basis_as_of` if this store predates it. Idempotent.

    Nullable on purpose and with no default: the existing rows' basis is genuinely unknown, and a
    default of any date would be the store inventing a fact about prices it cannot recover.
    """
    present = {row[1] for row in connection.execute("PRAGMA table_info(price_bars)")}
    if PRICE_BASIS_COLUMN not in present:
        connection.execute(f"ALTER TABLE price_bars ADD COLUMN {PRICE_BASIS_COLUMN} TEXT")


def _has_price_basis_column(connection: sqlite3.Connection) -> bool:
    """Whether this store has been migrated. Reads open READ-ONLY and cannot migrate, so a reader
    must answer from an older store rather than raise — the lesson the `A.123` review taught on the
    verdict store, applied before it can be repeated here."""
    return PRICE_BASIS_COLUMN in {
        row[1] for row in connection.execute("PRAGMA table_info(price_bars)")
    }


def traded_basis_tokens_for(
    session_date: date, market_data: Path, *, bar_interval: str = FIVE_MINUTE_BAR_INTERVAL
) -> frozenset[int]:
    """Instruments whose bars for this session are KNOWN to hold what traded.

    The consumer read. An instrument appears only if every one of its bars for the session carries
    a basis equal to the session date — one bar on an unknown or later basis is enough to make the
    series unusable as a record of what a fill would have cost.

    Returns an empty set for an absent store, an unmigrated store, or an unbackfilled session. That
    is "nothing is KNOWN to be traded", never "nothing is fine" — the caller distinguishes them
    with `price_basis_coverage_for`, exactly as `refuted_instruments_for` and
    `verification_coverage_for` divide that work on the join store.
    """
    if not market_data.exists():
        return frozenset()
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        if not _has_price_basis_column(connection):
            return frozenset()
        rows = connection.execute(
            # `S608` on the next line only: `PRICE_BASIS_COLUMN` is a module constant and nothing
            # caller-supplied reaches the SQL text.
            "SELECT instrument_token FROM price_bars "  # noqa: S608
            "WHERE bar_interval = ? AND substr(bar_timestamp, 1, 10) = ? "
            "GROUP BY instrument_token "
            f"HAVING SUM(CASE WHEN {PRICE_BASIS_COLUMN} IS NOT ? THEN 1 ELSE 0 END) = 0",
            (bar_interval, session_date.isoformat(), session_date.isoformat()),
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


def price_basis_coverage_for(
    session_date: date, market_data: Path, *, bar_interval: str = FIVE_MINUTE_BAR_INTERVAL
) -> SessionPriceBasisCoverage | None:
    """What this session's bars are denominated in, or `None` if it holds no bars at all.

    `None` rather than a zeroed row: a session that was never backfilled has no basis mix, and
    reporting 0% traded would read as a measured failure of a run that never ran.
    """
    if not market_data.exists():
        return None
    with sqlite3.connect(f"file:{market_data}?mode=ro", uri=True) as connection:
        basis = PRICE_BASIS_COLUMN if _has_price_basis_column(connection) else "NULL"
        rows = connection.execute(
            # `S608` suppressed on the next line only: `basis` is one of two module constants and
            # nothing caller-supplied reaches the SQL.
            f"SELECT instrument_token, {basis} FROM price_bars "  # noqa: S608
            "WHERE bar_interval = ? AND substr(bar_timestamp, 1, 10) = ?",
            (bar_interval, session_date.isoformat()),
        ).fetchall()
    if not rows:
        return None

    traded = adjusted = unknown = impossible = 0
    at_risk: set[int] = set()
    latest: date | None = None
    for token, stored in rows:
        recorded = date.fromisoformat(str(stored)) if stored is not None else None
        try:
            classified = classify_price_basis(session_date, recorded)
        except PriceBasisError:
            # `H3`. A bar adjusted as of BEFORE its own session is a store defect. Counted and
            # surfaced; raising here would make one bad row hide every good one behind a 500.
            impossible += 1
            at_risk.add(int(token))
            continue
        if classified is PriceBasis.TRADED:
            traded += 1
        elif classified is PriceBasis.ADJUSTED_AFTER_THE_SESSION:
            adjusted += 1
            at_risk.add(int(token))
        else:
            unknown += 1
        if recorded is not None and (latest is None or recorded > latest):
            latest = recorded
    return SessionPriceBasisCoverage(
        session_date=session_date,
        bars_on_the_traded_basis=traded,
        bars_adjusted_after_the_session=adjusted,
        bars_on_an_unknown_basis=unknown,
        bars_on_an_impossible_basis=impossible,
        instruments_at_risk=frozenset(at_risk),
        latest_adjustment_basis=latest,
    )


@dataclass(frozen=True, slots=True)
class PriceBasisAdmission:
    """Which instruments a capital path may take signal from, and whether the rule is even ARMED.

    **`R.04` — thin data must not shrink the feature, only gate its activation.** Every bar written
    before `L0.37` is `UNKNOWN`, so a blanket "traded basis only" rule would withhold the entire
    universe and the loop would trade nothing. The rule is therefore built in full and ARMS ITSELF
    on evidence: it activates for a session the moment that session holds ANY bar known to be on
    the traded basis, and reports itself unarmed otherwise.

    The ladder needs no threshold, because the underlying quantity is close to binary in practice:
    a session backfilled on its own day is entirely traded-basis, and one backfilled later is
    entirely not. "Is there any positive evidence at all" is the whole rule, and it is the same
    shape as the join verification's "an unrun verification reads as unrun, never as clean".
    """

    session_date: date
    armed: bool
    """`True` only when the session records a basis for EVERY bar, so an instrument that is not on
    the traded basis is a genuine finding rather than an incomplete backfill.

    **The first version armed on the existence of a single traded-basis bar, and that was wrong by
    a factor of six hundred** (`H2` of the `A.126` review). The backfill commits PER INSTRUMENT and
    collects per-instrument failures, so a partial session is the normal outcome, not the exotic
    one — and a session with five instruments stamped and 3,322 unstamped reported itself *armed*
    at 0.1% coverage and withheld 3,322 names the loop had been trading. The docstring's premise
    that the quantity is "close to binary in practice" was simply false."""
    instruments: tuple[int, ...]
    """The instruments the caller may proceed with, in the order it supplied them."""
    withheld: tuple[int, ...]
    """Instruments withheld. Two rules, and only the second waits for arming:

    - **always**, an instrument holding a bar POSITIVELY known to be on another basis (adjusted
      after its own session, or impossible). That is evidence, and evidence acts immediately;
    - **only when armed**, an instrument whose basis is merely UNRECORDED. Withholding on absence
      of evidence is what dropped 3,322 names on an incomplete backfill."""
    coverage: SessionPriceBasisCoverage | None

    def describe(self) -> str:
        if self.coverage is None:
            return (
                f"price basis ({self.session_date.isoformat()}): no five-minute bars stored — "
                f"nothing to say about what they are denominated in"
            )
        if not self.armed:
            return (
                f"price basis ({self.session_date.isoformat()}): UNARMED — "
                f"{self.coverage.describe()}. This session does not record a basis for every bar, "
                f"so an unrecorded one cannot be told from an unfinished backfill and is NOT "
                f"withheld; {len(self.withheld)} instrument(s) are withheld on positive evidence "
                f"of another basis. This is NOT a clean bill of health "
                f"(`R.05` open blocker for this session)."
            )
        return (
            f"price basis ({self.session_date.isoformat()}): armed — {self.coverage.describe()}; "
            f"{len(self.withheld)} of {len(self.instruments) + len(self.withheld)} instrument(s) "
            f"withheld for not being on the traded basis"
        )


def admit_on_traded_price_basis(
    session_date: date,
    instrument_tokens: Sequence[int],
    market_data: Path,
    *,
    bar_interval: str = FIVE_MINUTE_BAR_INTERVAL,
) -> PriceBasisAdmission:
    """Apply the traded-basis rule to one run's instruments, arming it only on evidence.

    Args:
        session_date: the session being replayed.
        instrument_tokens: the run's candidates, in its own order.
        market_data: the bar store.
        bar_interval: the stored interval label; defaults to the one the writer uses.

    Returns:
        A `PriceBasisAdmission` whose `armed` flag says whether the rule could be applied at all.
        A caller must report `armed=False` rather than treating the full instrument list as
        cleared — that is the `A.41` distinction this whole feature exists to preserve.
    """
    coverage = price_basis_coverage_for(session_date, market_data, bar_interval=bar_interval)
    traded = traded_basis_tokens_for(session_date, market_data, bar_interval=bar_interval)
    # ARMED means the session records a basis for every bar, so "not traded" is a finding rather
    # than an unfinished backfill. Threshold-free: it asks whether anything is unrecorded at all.
    armed = coverage is not None and coverage.bars_on_an_unknown_basis == 0 and bool(traded)
    known_bad = coverage.instruments_at_risk if coverage is not None else frozenset()

    def is_withheld(token: int) -> bool:
        if token in known_bad:
            return True  # positive evidence of another basis — acts whether armed or not
        return armed and token not in traded

    return PriceBasisAdmission(
        session_date=session_date,
        armed=armed,
        instruments=tuple(token for token in instrument_tokens if not is_withheld(token)),
        withheld=tuple(token for token in instrument_tokens if is_withheld(token)),
        coverage=coverage,
    )
