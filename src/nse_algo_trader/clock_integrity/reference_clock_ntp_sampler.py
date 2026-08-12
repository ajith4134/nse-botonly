"""The other arm of the measurement: what the host's clock is worth against a REFERENCE.

The feed envelope (`exchange_clock_offset_estimator`) can only bound the host-minus-exchange
offset from ABOVE, because an arbitrarily large minimum one-way delay is indistinguishable
from a host clock that is behind. NTP does not have that problem: it measures a bounded
ROUND TRIP, so it brackets the host's error from both sides —

    offset = ((T2 - T1) + (T3 - T4)) / 2 ,   error <= round-trip / 2

and the true offset is therefore known to lie in `[offset - rtt/2, offset + rtt/2]`.

**Several servers, and the majority decides.** A single server that is itself wrong is
indistinguishable from a correct one, so the sampler queries N and combines their intervals
with **Marzullo's algorithm** (the intersection algorithm NTP itself uses): the returned
interval is the tightest one consistent with the largest number of servers, and a server
whose interval misses that consensus is a *falseticker* and is discarded by name. With
fewer than two responders no consensus exists and the sampler returns `None` — trusting a
single server here would be the same mistake as trusting a single circular in the rule
store.

**chrony is read too, and it is not the same measurement.** `chronyc tracking` reports what
the local daemon believes about its own discipline — including the frequency correction it
is already applying (this host: 6.917 ppm slow, RMS offset 39 us). That is the host's own
account of itself; the NTP samples are an independent check on it. Where they disagree, the
disagreement is the finding.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import ntplib  # type: ignore[import-untyped]

_LOGGER = logging.getLogger(__name__)

DEFAULT_NTP_SERVERS = (
    "169.254.169.254",  # the OCI metadata NTP service — the one chrony is disciplined by
    "time.cloudflare.com",
    "pool.ntp.org",
)
"""Servers to sample. Not a tuning constant: the list is the measurement's sample frame,
and it is deliberately mixed (the local provider plus two independent public sources) so a
provider-wide fault cannot pass as consensus."""


RESPONDERS_NEEDED_FOR_A_MAJORITY = 2
"""One server agreeing with itself is a claim; a majority needs at least two to exist."""


class NoReferenceConsensusError(RuntimeError):
    """Fewer than two servers answered, so no majority can exist.

    Surfaced rather than degraded to a single-server answer: one responder is a claim, not
    a measurement, and the trust budget must be able to tell "the clock is fine" apart from
    "nothing could be reached".
    """


@dataclass(frozen=True, slots=True)
class ReferenceClockSample:
    """One server's answer, with the bound its own round trip earns it."""

    server: str
    offset_seconds: float
    round_trip_seconds: float
    stratum: int
    root_dispersion_seconds: float
    sampled_at: datetime

    @property
    def lower_bound_seconds(self) -> float:
        return self.offset_seconds - self.round_trip_seconds / 2.0

    @property
    def upper_bound_seconds(self) -> float:
        return self.offset_seconds + self.round_trip_seconds / 2.0


@dataclass(frozen=True, slots=True)
class ReferenceClockConsensus:
    """The Marzullo intersection over the responders, and who was excluded."""

    lower_bound_seconds: float
    upper_bound_seconds: float
    agreeing_servers: tuple[str, ...]
    falsetickers: tuple[str, ...]
    unreachable_servers: tuple[str, ...]
    sampled_at: datetime

    @property
    def midpoint_seconds(self) -> float:
        """The point estimate of host-minus-UTC. The interval is the honest form."""
        return (self.lower_bound_seconds + self.upper_bound_seconds) / 2.0

    @property
    def half_width_seconds(self) -> float:
        """How much the host's own clock error is uncertain by. Feeds the trust budget."""
        return (self.upper_bound_seconds - self.lower_bound_seconds) / 2.0


@dataclass(frozen=True, slots=True)
class ChronyTracking:
    """What the local daemon says about itself, parsed from `chronyc tracking`."""

    reference_id: str
    stratum: int
    system_time_offset_seconds: float
    last_offset_seconds: float
    rms_offset_seconds: float
    frequency_ppm: float
    skew_ppm: float

    @property
    def is_disciplined(self) -> bool:
        """A stratum of 0 or a missing reference means the daemon has no source at all."""
        return self.stratum > 0 and self.reference_id not in {"", "00000000"}


def marzullo_intersection(
    samples: Sequence[ReferenceClockSample],
) -> tuple[float, float, tuple[str, ...], tuple[str, ...]]:
    """The tightest interval agreeing with the most servers, and the outliers by name.

    The classic sweep: sort the interval endpoints, walk them counting overlaps, and keep
    the widest-agreement region. Returns `(low, high, agreeing, falsetickers)`.
    """
    if len(samples) < RESPONDERS_NEEDED_FOR_A_MAJORITY:
        raise NoReferenceConsensusError(
            f"{len(samples)} reference sample(s): a majority needs at least two, and one "
            f"server agreeing with itself is not a measurement"
        )
    endpoints: list[tuple[float, int]] = []
    for sample in samples:
        endpoints.append((sample.lower_bound_seconds, -1))  # an interval opens
        endpoints.append((sample.upper_bound_seconds, +1))  # an interval closes
    endpoints.sort(key=lambda point: (point[0], point[1]))

    best_count = 0
    best_low = best_high = 0.0
    current = 0
    for index, (position, kind) in enumerate(endpoints):
        current -= kind  # opening (-1) increments, closing (+1) decrements
        if current > best_count:
            best_count = current
            best_low = position
            best_high = endpoints[index + 1][0] if index + 1 < len(endpoints) else position
    agreeing = tuple(
        sample.server
        for sample in samples
        if sample.lower_bound_seconds <= best_high and sample.upper_bound_seconds >= best_low
    )
    required = len(samples) // 2 + 1
    if len(agreeing) < required:
        # Measured on the real host, 2026-08-12: the OCI metadata server said +0.19 ms and
        # Cloudflare said -9 ms, intervals disjoint. With two responders and one vote each
        # there is no majority, and picking the tighter interval would have promoted an
        # arbitrary choice to a consensus. NTP's own rule is a majority or nothing.
        raise NoReferenceConsensusError(
            f"no majority among {len(samples)} responders: the largest agreeing set is "
            f"{len(agreeing)} ({', '.join(agreeing)}), short of the {required} required. "
            f"Two servers that disagree do not average into a bracket"
        )
    falsetickers = tuple(sample.server for sample in samples if sample.server not in agreeing)
    return best_low, best_high, agreeing, falsetickers


class ReferenceClockNtpSampler:
    """Samples NTP servers and reduces them to one consensus interval."""

    def __init__(
        self,
        servers: Sequence[str] = DEFAULT_NTP_SERVERS,
        *,
        timeout_seconds: float = 5.0,
        ntp_version: int = 4,
        client: ntplib.NTPClient | None = None,
    ) -> None:
        self._servers = tuple(servers)
        self._timeout_seconds = timeout_seconds
        self._ntp_version = ntp_version
        self._client = client or ntplib.NTPClient()

    def sample_all(self, *, now: datetime | None = None) -> list[ReferenceClockSample]:
        """Query every server once. A server that fails is omitted, never faked."""
        sampled_at = now or datetime.now(UTC)
        samples: list[ReferenceClockSample] = []
        for server in self._servers:
            try:
                response = self._client.request(
                    server, version=self._ntp_version, timeout=self._timeout_seconds
                )
            except Exception as failure:  # noqa: BLE001 - every failure here is "no answer"
                # Logged, not swallowed: which servers were unreachable is part of the
                # measurement, and `unreachable_servers` carries it onto the surface.
                _LOGGER.info("ntp sample failed for %s: %s", server, failure)
                continue
            samples.append(
                ReferenceClockSample(
                    server=server,
                    offset_seconds=float(response.offset),
                    round_trip_seconds=float(response.delay),
                    stratum=int(response.stratum),
                    root_dispersion_seconds=float(response.root_dispersion),
                    sampled_at=sampled_at,
                )
            )
        return samples

    def consensus(self, *, now: datetime | None = None) -> ReferenceClockConsensus:
        """One bracketed estimate of host-minus-UTC, or a raised refusal."""
        sampled_at = now or datetime.now(UTC)
        samples = self.sample_all(now=sampled_at)
        reachable = {sample.server for sample in samples}
        low, high, agreeing, falsetickers = marzullo_intersection(samples)
        return ReferenceClockConsensus(
            lower_bound_seconds=low,
            upper_bound_seconds=high,
            agreeing_servers=agreeing,
            falsetickers=falsetickers,
            unreachable_servers=tuple(s for s in self._servers if s not in reachable),
            sampled_at=sampled_at,
        )


def read_chrony_tracking(*, command: str = "chronyc") -> ChronyTracking | None:
    """Parse `chronyc tracking`, or `None` when chrony is not installed here.

    `None` rather than an exception: a host without chrony is a deployment fact, and the
    NTP arm still works. What must never happen is inventing values for it.
    """
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 — fixed executable, no shell, no user input
            [executable, "tracking"], capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    fields = {}
    for line in completed.stdout.splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    if "Reference ID" not in fields:
        return None
    return ChronyTracking(
        reference_id=fields.get("Reference ID", "").split()[0]
        if fields.get("Reference ID")
        else "",
        stratum=int(fields.get("Stratum", "0") or 0),
        system_time_offset_seconds=_leading_float(fields.get("System time", "0")),
        last_offset_seconds=_leading_float(fields.get("Last offset", "0")),
        rms_offset_seconds=_leading_float(fields.get("RMS offset", "0")),
        frequency_ppm=_signed_frequency_ppm(fields.get("Frequency", "0")),
        skew_ppm=_leading_float(fields.get("Skew", "0")),
    )


def _leading_float(text: str) -> float:
    """The first number in a chrony value line, ignoring its trailing prose."""
    for token in text.split():
        try:
            return float(token)
        except ValueError:
            continue
    return 0.0


def _signed_frequency_ppm(text: str) -> float:
    """chrony writes the SIGN as a word: `6.917 ppm slow` means the clock runs slow.

    Parsing only the number would report a slow clock and a fast one identically, which is
    the one thing a drift engine must never do.
    """
    magnitude = _leading_float(text)
    return -magnitude if "slow" in text.lower() else magnitude
