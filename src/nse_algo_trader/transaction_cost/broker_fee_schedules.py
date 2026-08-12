"""Broker commercial schedules, loaded from data and validated on the way in.

Brokerage is the one charge with no circular behind it, so it does not belong in the
point-in-time rule store: it changes when the BROKER changes, not when the law does. It is
still effective-dated and cited, because a backtest over 2023 priced with 2026 brokerage is
the same look-ahead error as one priced with 2026 STT.

The shape that matters is `BrokeragePiece`. A per-order cap (`min(0.03%, Rs 20)`) makes
brokerage piecewise-linear in turnover, which is what gives the breakeven solve its kinks and
gives cost-in-bps its staircase in quantity. Representing it as a single average rate would
erase both, and the staircase is the thing that tells a small account which segments are
arithmetically closed to it.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from nse_algo_trader.market_rules.point_in_time_market_rule_store import EvidenceGrade
from nse_algo_trader.transaction_cost.chargeable_market_segments import (
    ChargeableSegment,
    Depository,
    RoundingRule,
)

SCHEDULE_DATA_PATH = Path(__file__).with_suffix(".toml")
"""The schedules live beside this module so the two version together."""


class BrokerFeeScheduleError(Exception):
    """The schedule data is unusable, and using it anyway would misprice trades."""


class BrokerFeeScheduleCoverageError(BrokerFeeScheduleError):
    """No schedule for this broker at this date — refused rather than defaulted."""


@dataclass(frozen=True, slots=True)
class BrokeragePiece:
    """`flat_paise + rate * turnover` over `[from_paise, to_paise)`.

    Both fields are `Decimal` rather than `int` because the boundary between a percentage leg
    and a flat cap is generally not a whole paisa (Zerodha's is Rs 66,666.67), and rounding it
    to one would put trades on the wrong side of the kink.
    """

    turnover_from_paise: Decimal
    turnover_to_paise: Decimal | None
    rate: Decimal
    flat_paise: Decimal

    def __post_init__(self) -> None:
        if self.turnover_from_paise < 0:
            raise BrokerFeeScheduleError(f"piece starts below zero turnover: {self}")
        upper = self.turnover_to_paise
        if upper is not None and upper <= self.turnover_from_paise:
            raise BrokerFeeScheduleError(f"piece covers no turnover: {self}")
        if self.rate < 0 or self.flat_paise < 0:
            raise BrokerFeeScheduleError(f"piece charges a negative amount: {self}")

    def contains(self, turnover_paise: Decimal) -> bool:
        if turnover_paise < self.turnover_from_paise:
            return False
        return self.turnover_to_paise is None or turnover_paise < self.turnover_to_paise

    def charge_paise(self, turnover_paise: Decimal) -> Decimal:
        return self.flat_paise + self.rate * turnover_paise


@dataclass(frozen=True, slots=True)
class BrokerFeeSchedule:
    """One broker's published prices over one effective window."""

    broker: str
    effective_from: date
    effective_to: date | None
    brokerage_pieces: Mapping[ChargeableSegment, tuple[BrokeragePiece, ...]]
    depository: Depository
    depository_markup_paise: Decimal
    auto_square_off_paise: Decimal
    call_and_trade_paise: Decimal
    physical_settlement_rate: Decimal
    physical_settlement_netted_rate: Decimal
    statutory_rounding: RoundingRule
    brokerage_rounding: RoundingRule
    source_reference: str
    source_date: date
    grade: EvidenceGrade

    def covers_date(self, as_of: date) -> bool:
        if as_of < self.effective_from:
            return False
        return self.effective_to is None or as_of < self.effective_to

    def pieces_for(self, segment: ChargeableSegment) -> tuple[BrokeragePiece, ...]:
        try:
            return self.brokerage_pieces[segment]
        except KeyError as error:
            raise BrokerFeeScheduleCoverageError(
                f"{self.broker} publishes no brokerage for {segment}; refusing to assume one"
            ) from error

    def brokerage_paise(self, segment: ChargeableSegment, turnover_paise: Decimal) -> Decimal:
        """Brokerage for ONE order of this turnover.

        Turnover below the first piece cannot happen (pieces start at zero) and turnover above
        the last cannot either (the last piece is open-ended); both are checked rather than
        assumed, because a gap in the data would otherwise surface as a silent zero.
        """
        for piece in self.pieces_for(segment):
            if piece.contains(turnover_paise):
                return piece.charge_paise(turnover_paise)
        raise BrokerFeeScheduleError(
            f"{self.broker}'s {segment} schedule has a hole at turnover {turnover_paise} paise"
        )


def _decimal(raw: object, field: str) -> Decimal:
    if isinstance(raw, str):
        return Decimal(raw)
    if isinstance(raw, int):
        return Decimal(raw)
    raise BrokerFeeScheduleError(
        f"{field} must be a string or integer so it converts to an exact Decimal, got "
        f"{type(raw).__name__} — a float here would put binary rounding error into money"
    )


def _piece(raw: Mapping[str, object], broker: str, segment: str) -> BrokeragePiece:
    where = f"{broker}/{segment}"
    upper = raw.get("to_paise")
    return BrokeragePiece(
        turnover_from_paise=_decimal(raw["from_paise"], f"{where} from_paise"),
        turnover_to_paise=None if upper is None else _decimal(upper, f"{where} to_paise"),
        rate=_decimal(raw["rate"], f"{where} rate"),
        flat_paise=_decimal(raw["flat_paise"], f"{where} flat_paise"),
    )


def _validated_pieces(
    pieces: Sequence[BrokeragePiece], broker: str, segment: ChargeableSegment
) -> tuple[BrokeragePiece, ...]:
    """Contiguous from zero, in order, ending open — anything else is a hole or an overlap."""
    if not pieces:
        raise BrokerFeeScheduleError(f"{broker}/{segment} has no brokerage pieces")
    ordered = tuple(sorted(pieces, key=lambda piece: piece.turnover_from_paise))
    if ordered[0].turnover_from_paise != 0:
        raise BrokerFeeScheduleError(
            f"{broker}/{segment} does not start at zero turnover, so small orders are unpriced"
        )
    if ordered[-1].turnover_to_paise is not None:
        raise BrokerFeeScheduleError(
            f"{broker}/{segment} does not end open, so large orders are unpriced"
        )
    for earlier, later in pairwise(ordered):
        if earlier.turnover_to_paise != later.turnover_from_paise:
            raise BrokerFeeScheduleError(
                f"{broker}/{segment} pieces do not meet at {earlier.turnover_to_paise}: a gap "
                f"prices nothing and an overlap prices twice"
            )
    return ordered


def _schedule(raw: Mapping[str, object]) -> BrokerFeeSchedule:
    broker = str(raw["broker"])
    pieces_by_segment: dict[ChargeableSegment, tuple[BrokeragePiece, ...]] = {}
    brokerage_entries = raw["brokerage"]
    if not isinstance(brokerage_entries, list):
        raise BrokerFeeScheduleError(f"{broker} has no brokerage table")
    for entry in brokerage_entries:
        segment = ChargeableSegment(str(entry["segment"]))
        if segment in pieces_by_segment:
            raise BrokerFeeScheduleError(f"{broker} declares {segment} twice")
        raw_pieces = entry["pieces"]
        if not isinstance(raw_pieces, list):
            raise BrokerFeeScheduleError(f"{broker}/{segment} pieces is not a list")
        pieces_by_segment[segment] = _validated_pieces(
            [_piece(piece, broker, str(entry["segment"])) for piece in raw_pieces],
            broker,
            segment,
        )
    missing = set(ChargeableSegment) - set(pieces_by_segment)
    if missing:
        raise BrokerFeeScheduleError(
            f"{broker} is missing brokerage for {sorted(segment.value for segment in missing)} "
            f"— a segment with no published price must be refused, not priced at zero"
        )
    effective_to = raw.get("effective_to")
    return BrokerFeeSchedule(
        broker=broker,
        effective_from=_as_date(raw["effective_from"], f"{broker} effective_from"),
        effective_to=None if effective_to is None else _as_date(effective_to, f"{broker} to"),
        brokerage_pieces=pieces_by_segment,
        depository=Depository(str(raw["depository"])),
        depository_markup_paise=_decimal(raw["depository_markup_paise"], f"{broker} dp markup"),
        auto_square_off_paise=_decimal(raw["auto_square_off_paise"], f"{broker} square-off"),
        call_and_trade_paise=_decimal(raw["call_and_trade_paise"], f"{broker} call and trade"),
        physical_settlement_rate=_decimal(raw["physical_settlement_rate"], f"{broker} physical"),
        physical_settlement_netted_rate=_decimal(
            raw["physical_settlement_netted_rate"], f"{broker} physical netted"
        ),
        statutory_rounding=RoundingRule(str(raw["statutory_rounding"])),
        brokerage_rounding=RoundingRule(str(raw["brokerage_rounding"])),
        source_reference=str(raw["source_reference"]),
        source_date=_as_date(raw["source_date"], f"{broker} source_date"),
        grade=EvidenceGrade(str(raw["evidence_grade"])),
    )


def _as_date(raw: object, field: str) -> date:
    if isinstance(raw, date):
        return raw
    raise BrokerFeeScheduleError(f"{field} must be a TOML date, got {type(raw).__name__}")


@lru_cache(maxsize=1)
def _loaded_schedules() -> tuple[BrokerFeeSchedule, ...]:
    with SCHEDULE_DATA_PATH.open("rb") as handle:
        document = tomllib.load(handle)
    entries = document.get("schedule")
    if not isinstance(entries, list) or not entries:
        raise BrokerFeeScheduleError(f"{SCHEDULE_DATA_PATH} declares no schedules")
    return tuple(_schedule(entry) for entry in entries)


def known_brokers() -> tuple[str, ...]:
    return tuple(sorted({schedule.broker for schedule in _loaded_schedules()}))


def broker_fee_schedule(broker: str, as_of: date) -> BrokerFeeSchedule:
    """The schedule in force for this broker on this date, or a refusal."""
    candidates = [
        schedule
        for schedule in _loaded_schedules()
        if schedule.broker == broker and schedule.covers_date(as_of)
    ]
    if not candidates:
        raise BrokerFeeScheduleCoverageError(
            f"no fee schedule for {broker!r} at {as_of}; known brokers are {known_brokers()}"
        )
    if len(candidates) > 1:
        raise BrokerFeeScheduleError(
            f"{len(candidates)} overlapping schedules for {broker!r} at {as_of}"
        )
    return candidates[0]


DEFAULT_BROKER = "zerodha"
"""Zerodha, because Kite is the single execution path (`A.25`).

Default, not assumption: every entry point takes the broker as a parameter, and the
quantity-economics solve is deliberately broker-aware because the turnover at which the flat
cap binds differs by more than 3x across the three schedules carried here.
"""
