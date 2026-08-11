"""Restate historical prices and strikes so a corporate action is not read as a crash.

**This is an engine** (R.23b): the corporate-action feed and the price archive in;
a subject-text parser plus ratio-to-factor arithmetic and a backwards cumulative
adjustment as the solver; per-symbol adjustment history as carried state; and a
restated series out that changes every indicator, signal and stop computed on it.

**SOTA analog** (R.23a): Zipline's adjustments database — separate splits/mergers/
dividends tables with multiplicative factors applied backwards from the ex-date.
This is the NSE-specific half of that. (Zipline is read, never imported: `A.39`.)

**Why it matters.** `TATASTEEL` closed 959.40 on 2022-07-27 and 100.35 on
2022-07-28. Unadjusted that is a 90% overnight crash that never happened, and
every volatility, momentum and stop calculation across the pair is wrong.

**The premise that had to be abandoned first (`D.28`).** `CLOSE(t-1)` versus
`PREVCLOSE(t)` in the cash bhavcopy does **not** detect corporate actions: on both
the `TATASTEEL` split and the `IOC` 1:2 bonus the two values are exactly equal,
because `PREVCLOSE` is a raw carry-forward that is never adjusted. An external feed
is mandatory.

**The real hazard is the text, not the arithmetic.** The feed carries no numeric
ratio — it lives in free-form English. Measured across the 41,885 acquired actions:
**8,074 distinct subject strings, 2,321 distinct shapes, 1,573 of them appearing
exactly once**. So the parser is explicit and ordered, and anything it does not
recognise becomes ``UNPARSED`` carrying its raw text. A dropped split is
indistinguishable from no split, which is precisely the failure above.

**Four outcomes, because "cannot tell" is real (`A.41`).** Beyond adjusting and
inert there is a third class that matters more than either: `Demerger` and
`Scheme Of Arrangement` — **189 real actions** that move the price materially and
contain no number anywhere. Folding those into "no adjustment" would leave a true
discontinuity in the series while the engine reported success.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final

# NSE quotes and lists strikes on a 5-paise tick. A regulatory fact, not a tuned
# value, which is the only kind of constant R.03e permits.
_STRIKE_TICK: Final = Decimal("0.05")
# Belief bound for "everything known", so reads without an explicit as-of see all.
_FAR_FUTURE_BELIEF: Final = "9999-12-31"


class CorporateActionError(Exception):
    """The engine was asked for something the evidence cannot support."""


class ActionClass(StrEnum):
    """What the engine could establish about an action."""

    ADJUSTING_QUANTIFIED = "adjusting_quantified"
    NON_ADJUSTING = "non_adjusting"
    ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED = "adjustment_required_but_unquantified"
    UNPARSED = "unparsed"


class AdjustmentKind(StrEnum):
    RATIO_SPLIT = "ratio_split"
    RATIO_CONSOLIDATION = "ratio_consolidation"
    COMPOUND = "compound"
    RATIO_BONUS = "ratio_bonus"
    RATIO_RIGHTS = "ratio_rights"
    ADDITIVE_DIVIDEND = "additive_dividend"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One announced action, exactly as the feed published it."""

    symbol: str
    ex_date: date
    subject: str
    isin: str | None
    face_value: str | None
    series: str | None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise CorporateActionError("symbol must be a non-empty string")
        if not isinstance(self.ex_date, date):
            raise CorporateActionError(
                f"ex_date must be a date, not {type(self.ex_date).__name__}"
            )


@dataclass(frozen=True, slots=True)
class ParsedAction:
    """What the parser could establish, with the evidence that decided it."""

    action: CorporateAction
    classification: ActionClass
    kind: AdjustmentKind
    price_factor: Decimal | None
    quantity_factor: Decimal | None
    evidence: tuple[tuple[str, str], ...]

    def evidence_summary(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.evidence)


# One parsed ratio inside a subject line: kind, price factor, quantity factor, evidence.
_Component = tuple[AdjustmentKind, Decimal, Decimal, tuple[tuple[str, str], ...]]


@dataclass(frozen=True, slots=True)
class SeriesTrust:
    """Whether a restated series over a window can be relied on."""

    symbol: str
    window_start: date
    window_end: date
    unquantified_actions: tuple[ParsedAction, ...]
    unparsed_actions: tuple[ParsedAction, ...]

    @property
    def is_trustworthy(self) -> bool:
        """False if anything in the window could not be quantified.

        Both unresolved classes taint equally: an unparsed subject is an open
        question, not a non-event.
        """
        return not (self.unquantified_actions or self.unparsed_actions)

    def describe(self) -> str:
        if self.is_trustworthy:
            return f"{self.symbol}: no unresolved actions in the window"
        parts = [
            f"{len(self.unquantified_actions)} unquantified",
            f"{len(self.unparsed_actions)} unparsed",
        ]
        return f"{self.symbol}: {', '.join(parts)} — series must not be trusted"


# Ordered, explicit, and deliberately narrow. Every pattern here was taken from a
# shape actually present in the acquired feed; nothing is anticipated. Order
# matters: the inert patterns run last so a "Bonus" inside a longer AGM string is
# still seen as a bonus.
# Deliberately typo-tolerant. Measured in the real feed: `Fv Splt Frm Rs 10 To Rs 2`
# (14 actions), `Face Valus Split` (2), `Fv Spl-Rs10tors2/Bon-1:1` (10, with no
# spaces at all). These are real splits that a tidy regex silently drops, and a
# dropped split is indistinguishable from no split.
_FACE_VALUE_LEAD: Final = (
    r"(?:face\s*val\w*\s*spl\w*|fv\s*spl\w*|\bsplit\b|\bconsolidat\w*)"
)
_SPLIT_PATTERNS: Final = (
    re.compile(
        r"(?P<lead>" + _FACE_VALUE_LEAD + r")"
        r"[^0-9]*?(?:rs|re)\.?\s*(?P<before>\d+(?:\.\d+)?)"
        r"\s*/?-?[^0-9]*?to\s*(?:rs|re)\.?\s*(?P<after>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
)
# `Bon-1:1` and `Bonus1:1` both occur in the real feed alongside the tidy form.
_BONUS_PATTERNS: Final = (
    re.compile(r"\bbon(?:us)?\b[^0-9]*?(?P<new>\d+)\s*:\s*(?P<held>\d+)", re.IGNORECASE),
    re.compile(r"bonus\s*(?P<new>\d+)\s*:\s*(?P<held>\d+)", re.IGNORECASE),
)
_RIGHTS_PATTERNS: Final = (
    re.compile(r"\brights\b[^0-9]*?(?P<new>\d+)\s*:\s*(?P<held>\d+)", re.IGNORECASE),
)
# A "bonus" of debentures, preference shares, NCRPS, DVR shares or warrants is a
# distribution of a DIFFERENT instrument — it does not dilute the equity and the
# exchange does not divide the equity price for it. Measured: 11 such actions in
# the feed, 7 of which the first implementation quantified. `DRREDDY` 2011 got a
# factor of 1/7 on a day the stock moved 2.8%, and `ZEEL` 2014 got 1/22 on a flat
# day — the engine injecting exactly the fake crash it exists to prevent.
_NON_EQUITY_BONUS_MARKERS: Final = (
    "debenture", "ncrps", "ncd", "preference", "pref share", "dvr", "warrant",
    "bond", "unit", "ncrp",
)
# Real price impact, no number anywhere in the text.
_UNQUANTIFIED_MARKERS: Final = (
    "demerger", "de-merger", "de merger", "scheme of arrangement", "spin off", "spin-off",
    "amalgamation", "reduction of capital", "capital reduction", "composite scheme",
)
# Inert. Buyback is deliberately here: it changes share count but the exchange does
# not adjust the price series for it.
_INERT_MARKERS: Final = (
    "annual general meeting", "agm", "dividend", "interest payment", "buy back", "buyback",
    "extra ordinary general meeting", "extra-ordinary general meeting", "egm",
    "postal ballot", "book closure", "court convened", "distribution -", "int div",
    "board meeting", "results", "interest", "redemption",
)

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS corporate_action (
    symbol      TEXT NOT NULL,
    ex_date     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    isin        TEXT,
    face_value  TEXT,
    series      TEXT,
    known_as_of TEXT NOT NULL,
    PRIMARY KEY (symbol, ex_date, subject, known_as_of)
);
CREATE INDEX IF NOT EXISTS corporate_action_by_symbol ON corporate_action (symbol, ex_date);
"""


@dataclass(slots=True)
class CorporateActionAdjustmentEngine:
    """Parses corporate actions and restates history in today's terms."""

    database_path: Path
    busy_timeout_seconds: float = 30.0
    _connection: sqlite3.Connection = field(init=False, repr=False)
    _timeline_cache: dict[tuple[str, str], tuple[tuple[date, Decimal], ...]] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database_path, timeout=self.busy_timeout_seconds, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._timeline_cache = {}

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> CorporateActionAdjustmentEngine:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ parsing

    def parse(self, action: CorporateAction) -> ParsedAction:
        """Classify one action. Never guesses; an unrecognised subject is reported."""
        subject = action.subject.strip()
        lowered = subject.lower()

        # ORDER IS LOAD-BEARING. The unquantified check runs FIRST, before any
        # component is computed. `MONNETISPA` 2018 reads "Capital Reduction Rs 10 To
        # Rs 3.30 / Consolidation Rs 3.30 To Rs.10": the split matcher happily finds
        # the consolidation leg and returns a factor of 3.03 computed from half a
        # compound sentence, silently discarding the capital reduction. When a
        # subject contains an action we cannot quantify, nothing else in it can be
        # trusted either.
        if any(marker in lowered for marker in _UNQUANTIFIED_MARKERS):
            return ParsedAction(
                action, ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED, AdjustmentKind.NONE,
                None, None,
                (("subject", subject),
                 ("reason", "this action moves the price but the feed carries no usable ratio; "
                            "any ratio elsewhere in the same subject describes only part of it")),
            )

        # Components are COMPOSED, not raced. Ten real actions carry a split and a
        # bonus in one line (`Fv Spl-Rs10tors2/Bon-1:1`); returning on the first
        # match would apply half the adjustment and look successful.
        components = [
            found
            for found in (self._match_split(subject), self._match_bonus(subject))
            if found is not None
        ]
        rights = self._match_rights(action, subject)
        if rights is not None:
            return rights
        if components:
            price = Decimal(1)
            quantity = Decimal(1)
            evidence: list[tuple[str, str]] = [("subject", subject)]
            for _kind, price_part, quantity_part, detail in components:
                price *= price_part
                quantity *= quantity_part
                evidence.extend(detail)
            resolved_kind = (
                components[0][0] if len(components) == 1 else AdjustmentKind.COMPOUND
            )
            if len(components) > 1:
                evidence.append(("components", str(len(components))))
            return ParsedAction(
                action, ActionClass.ADJUSTING_QUANTIFIED, resolved_kind,
                price, quantity, tuple(evidence),
            )

        if any(marker in lowered for marker in _INERT_MARKERS):
            return ParsedAction(
                action, ActionClass.NON_ADJUSTING, AdjustmentKind.NONE, None, None,
                (("subject", subject), ("reason", "no price adjustment for this action type")),
            )
        return ParsedAction(
            action, ActionClass.UNPARSED, AdjustmentKind.NONE, None, None,
            (("subject", subject),
             ("reason", "no pattern matched; reported rather than assumed inert, because a "
                        "dropped split is indistinguishable from no split")),
        )

    def _match_split(self, subject: str) -> _Component | None:
        """A face-value change. Downward is a split, upward is a consolidation.

        Both are real and both adjust price — the earlier version refused the
        upward case outright, which silently dropped every reverse split
        (`Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share`
        is a real action in the feed).
        """
        for pattern in _SPLIT_PATTERNS:
            found = pattern.search(subject)
            if not found:
                continue
            before = _decimal(found.group("before"))
            after = _decimal(found.group("after"))
            if before is None or after is None or before <= 0 or after <= 0:
                # Zero or negative face value is nonsense and would divide by zero.
                return None
            if before == after:
                # A face value that does not change is not an adjustment; computing
                # a factor of 1 would mark a real unparsed oddity as handled.
                return None
            says_consolidation = "consolidat" in found.group("lead").lower()
            if (after > before) != says_consolidation:
                # The words and the numbers disagree: a "split" whose face value
                # RISES, or a "consolidation" whose face value FALLS. One of the
                # two is wrong and there is no way to tell which, so this is
                # reported rather than resolved. Refusing beats applying a 10x
                # expansion to history on the strength of a typo.
                return None
            kind = (
                AdjustmentKind.RATIO_CONSOLIDATION if says_consolidation
                else AdjustmentKind.RATIO_SPLIT
            )
            return (
                kind, after / before, before / after,
                (("face_value_before", str(before)), ("face_value_after", str(after))),
            )
        return None

    def _match_bonus(self, subject: str) -> _Component | None:
        for pattern in _BONUS_PATTERNS:
            found = pattern.search(subject)
            if not found:
                continue
            if any(marker in subject.lower() for marker in _NON_EQUITY_BONUS_MARKERS):
                return None  # a bonus of something other than equity; falls through
            new = _decimal(found.group("new"))
            held = _decimal(found.group("held"))
            if new is None or held is None or new <= 0 or held <= 0:
                return None
            # `Bonus a:b` — a new shares for every b held, so b becomes a+b.
            total = new + held
            return (
                AdjustmentKind.RATIO_BONUS, held / total, total / held,
                (("new_per_held", f"{new}:{held}"),),
            )
        return None

    def _match_rights(self, action: CorporateAction, subject: str) -> ParsedAction | None:
        for pattern in _RIGHTS_PATTERNS:
            found = pattern.search(subject)
            if not found:
                continue
            new = _decimal(found.group("new"))
            held = _decimal(found.group("held"))
            if new is None or held is None or new <= 0 or held <= 0:
                return None
            # A rights factor depends on the cum-rights price and the issue price,
            # neither of which is in the subject line. The ratio is recorded; the
            # factor is deliberately withheld rather than approximated.
            return ParsedAction(
                action, ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED,
                AdjustmentKind.RATIO_RIGHTS, None, None,
                (("subject", subject), ("new_per_held", f"{new}:{held}"),
                 ("reason", "a rights factor needs the cum-rights close and the issue price; "
                            "the ratio alone does not determine it")),
            )
        return None

    # ------------------------------------------------------------------ ingest

    def ingest(
        self, actions: list[CorporateAction], *, known_as_of: date | None = None
    ) -> int:
        if not actions:
            return 0
        try:
            with self._connection:
                self._connection.executemany(
                    "INSERT OR REPLACE INTO corporate_action (symbol, ex_date, subject, isin,"
                    " face_value, series, known_as_of) VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            item.symbol, item.ex_date.isoformat(), item.subject, item.isin,
                            item.face_value, item.series,
                            (known_as_of or item.ex_date).isoformat(),
                        )
                        for item in actions
                    ],
                )
        except sqlite3.Error as error:
            raise CorporateActionError(
                f"failed to ingest {len(actions)} actions: {error}"
            ) from error
        self._timeline_cache.clear()  # new beliefs invalidate every cached timeline
        return len(actions)

    def ingest_raw(
        self, rows: list[tuple[str, str, str, str | None, str | None, str | None]]
    ) -> int:
        """Ingest the acquired feed's own rows, whose ex-dates are `DD-Mon-YYYY`."""
        actions = []
        for symbol, ex_text, subject, isin, face_value, series in rows:
            parsed_date = _feed_date(ex_text)
            if parsed_date is None:
                continue
            actions.append(
                CorporateAction(symbol, parsed_date, subject, isin, face_value, series)
            )
        return self.ingest(actions)

    def _actions_for(
        self, symbol: str, known_as_of: date | None = None
    ) -> list[CorporateAction]:
        """Actions for a symbol as believed at ``known_as_of``.

        The belief filter was previously absent from every read path: the column
        was written and never consulted, so a late-announced action leaked into a
        backtest dated before anyone could have known about it.
        """
        rows = self._connection.execute(
            "SELECT symbol, ex_date, subject, isin, face_value, series"
            " FROM corporate_action WHERE symbol = ? AND known_as_of <= ? ORDER BY ex_date",
            (symbol, self._belief_bound(known_as_of)),
        ).fetchall()
        return [
            CorporateAction(str(r[0]), date.fromisoformat(str(r[1])), str(r[2]),
                            r[3], r[4], r[5])
            for r in rows
        ]

    @staticmethod
    def _belief_bound(known_as_of: date | None) -> str:
        return known_as_of.isoformat() if known_as_of else _FAR_FUTURE_BELIEF

    def _factor_timeline(
        self, symbol: str, known_as_of: date | None
    ) -> tuple[tuple[date, Decimal], ...]:
        """Parsed (ex_date, price_factor) pairs, computed once per symbol and belief.

        Measured before caching: 0.67 ms per `cumulative_price_factor` call for the
        busiest symbol, which is ~28 minutes of pure factor lookup across a
        2,000-symbol five-year backtest — the full universe this project requires.
        The per-symbol action list and its parse results do not vary within a
        belief, so recomputing them per bar was pure waste.
        """
        key = (symbol, self._belief_bound(known_as_of))
        cached = self._timeline_cache.get(key)
        if cached is None:
            cached = tuple(
                (action.ex_date, parsed.price_factor)
                for action in self._actions_for(symbol, known_as_of)
                if (parsed := self.parse(action)).price_factor is not None
            )
            self._timeline_cache[key] = cached
        return cached

    # ------------------------------------------------------------------ factors

    def cumulative_price_factor(
        self, symbol: str, *, as_of: date, known_as_of: date | None = None
    ) -> Decimal:
        """What to multiply a price quoted on ``as_of`` by to restate it in today's terms.

        Only actions with an ex-date **strictly after** ``as_of`` apply: a price
        already quoted post-split must not be divided again, and the ex-date's own
        close is already adjusted.
        """
        factor = Decimal(1)
        for ex_date, price_factor in self._factor_timeline(symbol, known_as_of):
            if ex_date > as_of:
                factor *= price_factor
        return factor

    def series_trust(
        self, symbol: str, *, start: date, end: date, known_as_of: date | None = None
    ) -> SeriesTrust:
        """Whether a restated series over ``[start, end]`` can be relied on."""
        if end < start:
            raise CorporateActionError(
                f"end {end.isoformat()} is before start {start.isoformat()}"
            )
        unquantified, unparsed = [], []
        for action in self._actions_for(symbol, known_as_of):
            if not start <= action.ex_date <= end:
                continue
            parsed = self.parse(action)
            if parsed.classification is ActionClass.ADJUSTMENT_REQUIRED_BUT_UNQUANTIFIED:
                unquantified.append(parsed)
            elif parsed.classification is ActionClass.UNPARSED:
                unparsed.append(parsed)
        return SeriesTrust(symbol, start, end, tuple(unquantified), tuple(unparsed))

    def coverage_report(self, *, known_as_of: date | None = None) -> dict[ActionClass, int]:
        """How every stored action classified. The honest denominator for the parser."""
        counts: Counter[ActionClass] = Counter()
        rows = self._connection.execute(
            "SELECT symbol, ex_date, subject, isin, face_value, series FROM corporate_action"
            " WHERE known_as_of <= ?",
            (self._belief_bound(known_as_of),),
        )
        for row in rows:
            action = CorporateAction(
                str(row[0]), date.fromisoformat(str(row[1])), str(row[2]), row[3], row[4], row[5]
            )
            counts[self.parse(action).classification] += 1
        return {klass: counts.get(klass, 0) for klass in ActionClass}

    # ------------------------------------------------------------------ strikes

    @staticmethod
    def adjust_strike(
        strike: Decimal,
        *,
        price_factor: Decimal | None = None,
        dividend_rupees: Decimal | None = None,
    ) -> Decimal:
        """Restate an option strike for a corporate action.

        The two modes are different events and must not be mixed (`O.28`): a ratio
        action *divides* the strike and shortens the ladder step, a dividend
        adjustment *subtracts* a constant and leaves the step alone.

        Raises:
            CorporateActionError: neither or both modes were given, or the result
                would fall to or below zero.
        """
        if (price_factor is None) == (dividend_rupees is None):
            raise CorporateActionError(
                "adjust_strike needs exactly one of price_factor or dividend_rupees; "
                "a ratio adjustment and a dividend adjustment are different events"
            )
        if price_factor is not None:
            if price_factor <= 0:  # zero would collapse every strike to zero
                raise CorporateActionError(f"price_factor must be positive; got {price_factor}")
            adjusted = strike * price_factor
        else:
            assert dividend_rupees is not None
            if dividend_rupees < 0:
                raise CorporateActionError(
                    f"dividend_rupees cannot be negative; got {dividend_rupees}"
                )
            adjusted = strike - dividend_rupees
        if adjusted <= 0:
            raise CorporateActionError(
                f"adjusting {strike} would put the strike at or below zero ({adjusted})"
            )
        return (adjusted / _STRIKE_TICK).quantize(Decimal(1)) * _STRIKE_TICK


def _decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _feed_date(text: str) -> date | None:
    """The feed publishes `DD-Mon-YYYY`; ISO appears in re-exported rows."""
    for pattern in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.strip(), pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None
