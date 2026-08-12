"""One measurement of "how late did this packet look", and the rules for admitting it.

Every depth packet carries two clocks — the exchange's stamp and this host's receipt
stamp — and their difference is the only window onto the offset between them. It is a
*contaminated* window: the difference is `offset + network delay + truncation residue`,
never the offset alone (`docs/research/216` §1). This module is the boundary that turns
raw packets into admissible measurements and COUNTS what it refused, because the two
refusal reasons are themselves findings:

- **An absent exchange stamp** arrives as epoch 0, not as `None`, in roughly one packet in
  1,500 on the real tape (10,518 of 15,902,625 measured 2026-08-12). Kept, it would claim
  a 56-year offset.
- **A negative lag** — receipt before the exchange second — is physically impossible for a
  correctly-set clock, so it is not noise to average away; it is the alarm condition this
  engine exists to raise. Measured on the real tape: zero, once epoch 0 is excluded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ABSENT_STAMP_CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
"""Any exchange stamp older than this is the SDK's zero value, not a timestamp.

Not a tunable: the NSE feed cannot legitimately stamp a packet before this project's
data begins, and the values actually seen are epoch 0. A cutoff rather than an equality
check because `kiteconnect` has produced both `None` and 1970 for the same condition.
"""


class AbsentExchangeStampError(RuntimeError):
    """Every packet in a batch was unusable.

    Raised only when the caller asked for at least one usable observation. A silent empty
    return would be indistinguishable from a quiet market, and the daily runner would
    record "no drift detected" for a session in which nothing was measurable at all.
    """


@dataclass(frozen=True, slots=True)
class ExchangeFeedDelayObservation:
    """One admissible (exchange stamp, host receipt) pair.

    `exchange_second` is deliberately named for what it is: a stamp TRUNCATED to the
    second by the SDK, not an instant. Code that forgets this attributes up to a full
    second of truncation residue to the clock offset.
    """

    instrument_token: int
    exchange_second: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("exchange_second", self.exchange_second),
            ("received_at", self.received_at),
        ):
            if value.tzinfo is None:
                raise ValueError(
                    f"{name} has no timezone; a naive instant here would be read as UTC on "
                    f"this host and as IST on another, silently shifting every offset by 5h30m"
                )

    @property
    def apparent_lag_seconds(self) -> float:
        """`receipt - exchange stamp`: offset + delay + truncation residue, never one of them."""
        return (self.received_at - self.exchange_second).total_seconds()


@dataclass(frozen=True, slots=True)
class DelayObservationExtraction:
    """What survived, and an itemised account of what did not."""

    observations: tuple[ExchangeFeedDelayObservation, ...]
    considered: int
    absent_exchange_stamp: int
    negative_lag: int

    @property
    def admitted(self) -> int:
        return len(self.observations)

    @property
    def admitted_fraction(self) -> float:
        return self.admitted / self.considered if self.considered else 0.0


def _as_aware_utc(value: Any) -> datetime | None:
    """Accept what the tape and the live seam actually hand over, refuse the rest."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def observations_from_depth_packets(
    packets: Iterable[Mapping[str, Any] | Any],
    *,
    require_any: bool = False,
) -> DelayObservationExtraction:
    """Admit the usable packets and count the rest by reason.

    Accepts either the tape's row mappings or live `DepthPacket` objects — both carry
    `exchange_time` and `receipt_time`, and duplicating this boundary for each would be
    two places to forget the epoch-0 rule.
    """
    admitted: list[ExchangeFeedDelayObservation] = []
    considered = absent = negative = 0
    for packet in packets:
        considered += 1
        if isinstance(packet, Mapping):
            token = packet.get("instrument_token")
            exchange_time = _as_aware_utc(packet.get("exchange_time"))
            receipt_time = _as_aware_utc(packet.get("receipt_time"))
        else:
            token = getattr(packet, "instrument_token", None)
            exchange_time = _as_aware_utc(getattr(packet, "exchange_time", None))
            receipt_time = _as_aware_utc(getattr(packet, "receipt_time", None))
        if exchange_time is None or receipt_time is None or exchange_time < ABSENT_STAMP_CUTOFF:
            absent += 1
            continue
        observation = ExchangeFeedDelayObservation(
            instrument_token=int(token) if token is not None else 0,
            exchange_second=exchange_time,
            received_at=receipt_time,
        )
        if observation.apparent_lag_seconds < 0.0:
            negative += 1
            continue
        admitted.append(observation)
    if require_any and not admitted:
        raise AbsentExchangeStampError(
            f"no admissible delay observation in {considered} packets "
            f"({absent} absent exchange stamps, {negative} negative lags) — refusing rather "
            f"than reporting a clock as undrifted on evidence that does not exist"
        )
    return DelayObservationExtraction(
        observations=tuple(admitted),
        considered=considered,
        absent_exchange_stamp=absent,
        negative_lag=negative,
    )


def negative_lag_observations(
    observations: Sequence[ExchangeFeedDelayObservation],
) -> tuple[ExchangeFeedDelayObservation, ...]:
    """The impossible ones, kept addressable for the alert path rather than only counted."""
    return tuple(o for o in observations if o.apparent_lag_seconds < 0.0)
