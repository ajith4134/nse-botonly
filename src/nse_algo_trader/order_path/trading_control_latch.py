"""The durable flag the order path consults before every submission, and the only thing that can
say "no" to a trader that has decided "yes".

`L3.07`. `docs/research/221` §9 splits the kill switch in two on purpose, and this module is the
half that has no moving parts: **a latch, not a loop.** The other half — the process that decides
*when* to pull it — is `trading_halt_watchdog.py`, and the separation is load-bearing: a trader that
is wedged, looping, or blocked on a socket cannot stop itself, so anything that lives inside its
process is a label over the same failure rather than a defence against it.

**Why a file on disk and not a boolean in memory.** The event the kill switch exists for is the one
where the process dies and comes back. An in-memory flag is reset by exactly the failure it was
meant to survive, and the restarted trader would resume submitting into whatever condition caused
the halt. The latch therefore lives in SQLite with `synchronous=FULL`, so `commit()` returns only
after the write is on the platter, and a halt survives `kill -9`, a reboot, and an OOM kill.

**Why the default is refusal.** Every read path here answers the same question — *may this
submission go to a broker?* — and every failure mode of a store must answer it `no`. A missing file,
a zero-byte file, a file truncated by a full disk, a file that is not a database at all, and a row
whose contents do not match their digest all read as **latched, paper**. The alternative — an
unreadable store that reads as "armed and live" — is the single worst behaviour this module could
have, because it converts an infrastructure fault into unsupervised real-money trading. There is no
code path in this file that can produce `LIVE` from an absent or damaged store.

**Why `LIVE` needs two keys and cannot be reached by the system.** `R.22`: no instruction reaches
real money without both a graduation and an *explicit operator arm*, and the system can never
self-promote. That is enforced structurally rather than by convention. `arm_live_trading` demands an
`OperatorAuthorization`, which cannot be constructed by calling it — the dataclass refuses any
instance that did not come from `authorize_operator_action`, and that factory needs two independent
things a running process does not have:

1. an **arming-key file** whose secret this repository contains no code to create, so an autonomous
   agent cannot manufacture one without an operator putting it there by hand, and
2. a **typed confirmation phrase** that must contain that secret verbatim, so a leaked call site
   with a hard-coded string is useless on any other machine.

**Mode changes are deliberately awkward.** `arm_live_trading` refuses unless the latch is already
LATCHED and does not release it, so going live is always three separate acts: halt, arm, release.
Nothing can flip a running system from paper to live mid-session. The reverse direction —
`revert_to_paper_trading` — needs no authority at all and latches on the way, because every
safe-direction move must be available to anyone, including a panicking operator and the watchdog.

**The transition log is a hash chain.** Each row carries a digest over its own fields *and its
predecessor's digest*, so deleting the row that halted trading, or editing the reason after the
fact, breaks verification and the store reads SAFE from that point on. An audit trail that can be
silently rewritten is not evidence, and the one question this store will be asked after a bad day is
*who armed it, when, and what did they say the reason was*.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from nse_algo_trader.order_path.trading_intent import OrderNamespace, OrderPathError

DEFAULT_TRADING_CONTROL_LATCH_PATH: Final[Path] = Path(
    "~/.nse_algo_trader/trading_control_latch.sqlite3"
).expanduser()

# The operator's half of the two-key rule. Nothing in this repository writes this file; it exists
# only because a human created it, which is precisely what makes it a second key rather than a
# second copy of the first.
DEFAULT_OPERATOR_ARMING_KEY_PATH: Final[Path] = Path(
    "~/.nse_algo_trader/operator_arming_key.txt"
).expanduser()

_SCHEMA_VERSION: Final[str] = "trading-control-latch-v1"
_FIELD_SEPARATOR: Final[str] = "\x1f"
_DIGEST_BYTES: Final[int] = 16

# The chain has to start somewhere, and it starts at a value derived from the schema name rather
# than at an empty string, so a row forged with an empty predecessor does not verify.
_CHAIN_ORIGIN_DIGEST: Final[str] = hashlib.blake2b(
    f"{_SCHEMA_VERSION}-origin".encode(), digest_size=_DIGEST_BYTES
).hexdigest()

# An arming key readable by anyone on the host is not a key. The mask is the POSIX group+other bits.
_GROUP_AND_OTHER_PERMISSION_BITS: Final[int] = stat.S_IRWXG | stat.S_IRWXO

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS trading_control_transition (
    sequence_number   INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at_utc    TEXT NOT NULL,
    trading_mode      TEXT NOT NULL,
    latch_state       TEXT NOT NULL,
    changed_by        TEXT NOT NULL,
    change_authority  TEXT NOT NULL,
    reason            TEXT NOT NULL,
    previous_digest   TEXT NOT NULL,
    record_digest     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trading_control_transition_by_time
    ON trading_control_transition (changed_at_utc);
-- The head pointer. A hash chain alone is tamper-evident only in its MIDDLE: deleting the newest
-- rows, or truncating the file, leaves a shorter chain that still verifies perfectly and whose last
-- row is an EARLIER, more permissive state — which is exactly how "delete the halt" would read as
-- "still released". The head is written in the same transaction as the row it names, so a missing,
-- extra, or disagreeing head is a broken store, and a broken store reads HALTED.
CREATE TABLE IF NOT EXISTS trading_control_head (
    singleton_id         INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    head_sequence_number INTEGER NOT NULL,
    head_digest          TEXT NOT NULL
);
"""

_TRANSITION_COLUMNS: Final[str] = (
    "sequence_number, changed_at_utc, trading_mode, latch_state, changed_by,"
    " change_authority, reason, previous_digest, record_digest"
)


class TradingControlError(OrderPathError):
    """The base of every refusal this module makes."""


class TradingHaltedError(TradingControlError):
    """The latch is closed, so no submission may leave. Carries who closed it and why."""


class LiveTradingNotArmedError(TradingControlError):
    """A live submission was attempted while the durable mode says paper."""


class OperatorAuthorityError(TradingControlError):
    """An act reserved for an operator was attempted without a valid operator authorization."""


class TradingControlStoreError(TradingControlError):
    """The latch store could not be written, so the change did NOT happen and cannot be claimed."""


class TradingMode(StrEnum):
    """Whether the system's orders reach real money. Never inferred, never defaulted to LIVE."""

    PAPER = "paper"
    LIVE = "live"


class LatchState(StrEnum):
    """Whether submissions may leave at all.

    `LATCHED` is the safe pole and is what every degraded read produces. `RELEASED` is reachable
    only from a verified store plus an explicit operator act.
    """

    LATCHED = "latched"
    RELEASED = "released"


class ChangeAuthority(StrEnum):
    """Who made a transition. Recorded because the two carry different weight forever after.

    A `SYSTEM` transition can only ever move toward safety — latching, or reverting to paper. Every
    transition that widens what the system may do is `OPERATOR`, and that asymmetry is what makes
    the log answer "did anything promote itself" by inspection rather than by argument.
    """

    SYSTEM = "system"
    OPERATOR = "operator"


class LatchEvidence(StrEnum):
    """Where a disposition came from — read, defaulted, or defaulted *because something is wrong*.

    Callers that only ask "may I submit" never need this. Everything that reports to a human does:
    "latched because an operator latched it" and "latched because the store is unreadable" are the
    same refusal and completely different incidents.
    """

    RECORDED_TRANSITION = "recorded_transition"
    FRESH_STORE_SAFE_DEFAULT = "fresh_store_safe_default"
    UNREADABLE_STORE_SAFE_DEFAULT = "unreadable_store_safe_default"
    TAMPERED_CHAIN_SAFE_DEFAULT = "tampered_chain_safe_default"


class OperatorAuthorityScope(StrEnum):
    """Exactly what one operator authorization permits. Scoped so a release cannot arm live."""

    RELEASE_TRADING_HALT = "release_trading_halt"
    ARM_LIVE_TRADING = "arm_live_trading"


_SCOPE_CONFIRMATION_PREFIXES: Final[dict[OperatorAuthorityScope, str]] = {
    OperatorAuthorityScope.RELEASE_TRADING_HALT: "RELEASE TRADING HALT",
    OperatorAuthorityScope.ARM_LIVE_TRADING: "ARM LIVE TRADING",
}

# Only this module may mint an authorization. A sentinel rather than a name-mangled attribute
# because the check has to survive `dataclasses.replace`, pickling and subclassing attempts.
_AUTHORIZATION_MINT_TOKEN: Final[object] = object()


@dataclass(frozen=True, slots=True)
class OperatorAuthorization:
    """Proof that a human, not a process, asked for something that widens what the system may do.

    Construct it with `authorize_operator_action`. Calling this class directly raises: the type
    exists to be *unforgeable inside the process*, and a dataclass anyone can instantiate would
    make the whole two-key design a naming convention.
    """

    operator_name: str
    authority_scope: OperatorAuthorityScope
    authorized_at: datetime
    arming_key_path: Path
    mint_token: object = None

    def __post_init__(self) -> None:
        if self.mint_token is not _AUTHORIZATION_MINT_TOKEN:
            raise OperatorAuthorityError(
                "an OperatorAuthorization cannot be constructed directly; it is minted only by "
                "authorize_operator_action, which requires the operator's arming-key file AND the "
                "typed confirmation phrase (R.22 two-key). If this was reached from automation, "
                "the automation is trying to promote itself and must not be allowed to"
            )
        if not self.operator_name.strip():
            raise OperatorAuthorityError(
                "an authorization with no operator name names nobody, and the whole point of the "
                "record is that somebody can be asked about it afterwards"
            )
        if self.authorized_at.tzinfo is None or self.authorized_at.utcoffset() is None:
            raise OperatorAuthorityError(
                "the authorization time must carry a timezone: a naive timestamp in an audit trail "
                "is read differently by the host and by the reader"
            )


def read_operator_arming_secret(arming_key_path: Path = DEFAULT_OPERATOR_ARMING_KEY_PATH) -> str:
    """Read the operator's arming secret, refusing every way it can fail to be a secret."""
    try:
        file_status = arming_key_path.stat()
    except OSError as failure:
        raise OperatorAuthorityError(
            f"no operator arming key at {arming_key_path}: the second key of the R.22 pair does "
            f"not exist, so no operator act can be authorized on this host ({failure})"
        ) from failure
    if file_status.st_mode & _GROUP_AND_OTHER_PERMISSION_BITS:
        raise OperatorAuthorityError(
            f"the operator arming key at {arming_key_path} is readable or writable beyond its "
            f"owner (mode {stat.filemode(file_status.st_mode)}); a key every process on the host "
            f"can read is not a second key. Fix with: chmod 600 {arming_key_path}"
        )
    try:
        secret = arming_key_path.read_text(encoding="utf-8").strip()
    except OSError as failure:
        raise OperatorAuthorityError(
            f"the operator arming key at {arming_key_path} could not be read ({failure})"
        ) from failure
    if not secret:
        raise OperatorAuthorityError(
            f"the operator arming key at {arming_key_path} is empty; an empty secret would make "
            f"the confirmation phrase guessable from this source file alone"
        )
    return secret


def required_confirmation_phrase(scope: OperatorAuthorityScope, arming_secret: str) -> str:
    """The exact sentence an operator must type for `scope` on this host.

    It embeds the host's own secret, so a confirmation phrase copied out of a log, a script or a
    model's context is worthless anywhere else — which is the property that makes typing it an act
    rather than a formality.
    """
    return f"{_SCOPE_CONFIRMATION_PREFIXES[scope]} {arming_secret}"


def authorize_operator_action(
    *,
    operator_name: str,
    authority_scope: OperatorAuthorityScope,
    typed_confirmation: str,
    arming_key_path: Path = DEFAULT_OPERATOR_ARMING_KEY_PATH,
    authorized_at: datetime | None = None,
) -> OperatorAuthorization:
    """Mint an authorization, or refuse. The only door into `OperatorAuthorization`.

    Both keys are checked here: the arming-key file (which nothing in this repository creates) and
    the typed phrase that must contain its secret verbatim.
    """
    arming_secret = read_operator_arming_secret(arming_key_path)
    expected = required_confirmation_phrase(authority_scope, arming_secret)
    if not hmac.compare_digest(typed_confirmation.strip(), expected):
        raise OperatorAuthorityError(
            f"the typed confirmation does not match what {authority_scope.value} requires on this "
            f"host. The phrase is the arming key's secret prefixed by "
            f"{_SCOPE_CONFIRMATION_PREFIXES[authority_scope]!r}, and it is deliberately not "
            f"printed here — read it from the arming key file if you are the operator"
        )
    return OperatorAuthorization(
        operator_name=operator_name.strip(),
        authority_scope=authority_scope,
        authorized_at=authorized_at or datetime.now(UTC),
        arming_key_path=arming_key_path,
        mint_token=_AUTHORIZATION_MINT_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class LatchTransitionRecord:
    """One row of the tamper-evident log: a state, who put it there, when, and why."""

    sequence_number: int
    changed_at: datetime
    trading_mode: TradingMode
    latch_state: LatchState
    changed_by: str
    change_authority: ChangeAuthority
    reason: str
    previous_digest: str
    record_digest: str

    def expected_digest(self) -> str:
        """Recompute this row's digest from its own fields and its predecessor's."""
        payload = _FIELD_SEPARATOR.join(
            (
                _SCHEMA_VERSION,
                str(self.sequence_number),
                self.changed_at.astimezone(UTC).isoformat(),
                str(self.trading_mode),
                str(self.latch_state),
                self.changed_by,
                str(self.change_authority),
                self.reason,
                self.previous_digest,
            )
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


@dataclass(frozen=True, slots=True)
class LatchDisposition:
    """What the order path is allowed to do right now, and the evidence behind that answer."""

    trading_mode: TradingMode
    latch_state: LatchState
    changed_by: str
    change_authority: ChangeAuthority
    changed_at: datetime
    reason: str
    evidence: LatchEvidence

    @property
    def is_submission_permitted(self) -> bool:
        """The one question the order path asks. Nothing else in this type is on the hot path."""
        return self.latch_state is LatchState.RELEASED

    @property
    def broker_order_namespace(self) -> OrderNamespace:
        """The namespace this mode puts on the wire, so a live and a simulated order never mix.

        Derived from the durable mode rather than from a caller's argument: `L9.03`'s parity design
        deliberately shares every line of code above the venue seam, and the one fact that must not
        be shared is whether real money moved.
        """
        return (
            OrderNamespace.LIVE
            if self.trading_mode is TradingMode.LIVE
            else OrderNamespace.SIMULATED
        )

    def describe(self) -> str:
        """One line for a human — the operations wall, a log, or an exception message."""
        return (
            f"{self.latch_state.value}/{self.trading_mode.value} since "
            f"{self.changed_at.astimezone(UTC).isoformat()} by {self.changed_by} "
            f"({self.change_authority.value}, evidence={self.evidence.value}): {self.reason}"
        )


def _safe_default_disposition(evidence: LatchEvidence, reason: str) -> LatchDisposition:
    """The answer every degraded path gives. Latched, paper, and honest about why."""
    return LatchDisposition(
        trading_mode=TradingMode.PAPER,
        latch_state=LatchState.LATCHED,
        changed_by="trading_control_latch",
        change_authority=ChangeAuthority.SYSTEM,
        changed_at=datetime.now(UTC),
        reason=reason,
        evidence=evidence,
    )


@dataclass(slots=True)
class TradingControlLatchStore:
    """The durable latch. Opened per operation, so a corrupt file never poisons a long-lived handle.

    Deliberately NOT a long-lived connection like `BitemporalBarStore`: this store is read by the
    order path, written by a *different process* (the watchdog), and read again by the dashboard,
    and the failure it must survive is the file being replaced or damaged underneath a running
    trader. Connecting per call costs microseconds against a submission that costs milliseconds.
    """

    database_path: Path = DEFAULT_TRADING_CONTROL_LATCH_PATH
    busy_timeout_seconds: float = 30.0
    _initialisation_failure: str | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                connection.commit()
        except (sqlite3.Error, OSError) as failure:
            # A store that cannot be created is not a reason to refuse to start: it is a reason to
            # read as latched forever until a human fixes it, which is what `read_disposition`
            # will now do. Raising here would take down the trader instead of stopping it safely.
            self._initialisation_failure = f"{type(failure).__name__}: {failure}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=self.busy_timeout_seconds)
        connection.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL. The whole value of this store is that a halt written one microsecond
        # before the power failed is still a halt afterwards; NORMAL leaves that write in the OS
        # page cache, which is exactly the window this module exists to close.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    # -- reading -----------------------------------------------------------------------------

    def read_disposition(self) -> LatchDisposition:
        """What the latch says. **Never raises.**

        Every caller of this method is about to decide whether real money may move, and an
        exception escaping into that path would be handled by whatever `except` happened to be
        nearest — which is how an infrastructure fault becomes an unsupervised trade. So every
        failure resolves to the same value a closed latch resolves to.
        """
        if self._initialisation_failure is not None:
            return _safe_default_disposition(
                LatchEvidence.UNREADABLE_STORE_SAFE_DEFAULT,
                f"the latch store at {self.database_path} could not be opened or created "
                f"({self._initialisation_failure}); an unreadable control store reads as HALTED",
            )
        try:
            records = self._read_transition_chain()
        except (sqlite3.Error, OSError, ValueError) as failure:
            return _safe_default_disposition(
                LatchEvidence.UNREADABLE_STORE_SAFE_DEFAULT,
                f"the latch store at {self.database_path} is unreadable "
                f"({type(failure).__name__}: {failure}); an unreadable control store reads as "
                f"HALTED rather than as whatever it last said",
            )
        if records is None:
            return _safe_default_disposition(
                LatchEvidence.TAMPERED_CHAIN_SAFE_DEFAULT,
                f"the transition chain in {self.database_path} does not verify: a row has been "
                f"edited or removed. The log is the only evidence of who armed this system, so a "
                f"broken chain reads as HALTED until a human reconstructs it",
            )
        if not records:
            return _safe_default_disposition(
                LatchEvidence.FRESH_STORE_SAFE_DEFAULT,
                "no operator has ever armed this store; a system that has never been armed has "
                "never been given permission to trade",
            )
        latest = records[-1]
        return LatchDisposition(
            trading_mode=latest.trading_mode,
            latch_state=latest.latch_state,
            changed_by=latest.changed_by,
            change_authority=latest.change_authority,
            changed_at=latest.changed_at,
            reason=latest.reason,
            evidence=LatchEvidence.RECORDED_TRANSITION,
        )

    def transition_history(self, *, limit: int | None = None) -> tuple[LatchTransitionRecord, ...]:
        """The log, oldest first. Returns empty rather than raising when the store is unreadable."""
        try:
            records = self._read_transition_chain(verify=False)
        except (sqlite3.Error, OSError, ValueError):
            return ()
        if records is None:  # pragma: no cover - unreachable with verify=False
            return ()
        return tuple(records) if limit is None else tuple(records[-limit:])

    def verify_transition_chain(self) -> bool:
        """Whether the audit trail is intact. False for a damaged store as for a tampered one."""
        try:
            return self._read_transition_chain() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

    def _read_transition_chain(self, *, verify: bool = True) -> list[LatchTransitionRecord] | None:
        connection = self._connect()
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT {_TRANSITION_COLUMNS} FROM trading_control_transition "  # noqa: S608
                "ORDER BY sequence_number"
            ).fetchall()
            head_rows = connection.execute(
                "SELECT head_sequence_number, head_digest FROM trading_control_head"
            ).fetchall()
        finally:
            connection.close()
        records = [_record_from_row(row) for row in rows]
        if not verify:
            return records
        if not _head_agrees_with_chain(head_rows, records):
            return None
        previous_digest = _CHAIN_ORIGIN_DIGEST
        for record in records:
            if record.previous_digest != previous_digest:
                return None
            if not hmac.compare_digest(record.record_digest, record.expected_digest()):
                return None
            previous_digest = record.record_digest
        return records

    # -- the hot path ------------------------------------------------------------------------

    def refusal_for_submission(self, *, intended_mode: TradingMode) -> str | None:
        """Why this submission may not go out, or `None` if it may. The non-raising form.

        Exists for the surfaces that must *display* the refusal — the operations wall, a pre-trade
        report — without an exception being the control flow.
        """
        disposition = self.read_disposition()
        if not disposition.is_submission_permitted:
            return (
                f"trading is halted and every submission is refused — {disposition.describe()}. "
                f"Releasing the halt is an explicit operator act (R.22); nothing in the system "
                f"can release it on the system's own judgement"
            )
        if intended_mode is TradingMode.LIVE and disposition.trading_mode is not TradingMode.LIVE:
            return (
                f"a LIVE submission was attempted while the durable trading mode is "
                f"{disposition.trading_mode.value}. The mode is the authority, not the caller's "
                f"argument, and it is raised to live only by an operator arm (R.22)"
            )
        return None

    def assert_submission_permitted(self, *, intended_mode: TradingMode) -> LatchDisposition:
        """The call the order path makes before EVERY submission. Raises, or returns the mode.

        Returning the disposition rather than `None` is deliberate: the caller needs the durable
        mode to pick its venue and its wire namespace, and a caller that re-reads the mode from
        somewhere else could act on a value this check never saw.
        """
        disposition = self.read_disposition()
        if not disposition.is_submission_permitted:
            raise TradingHaltedError(
                f"trading is halted; this submission is refused. {disposition.describe()}"
            )
        if intended_mode is TradingMode.LIVE and disposition.trading_mode is not TradingMode.LIVE:
            raise LiveTradingNotArmedError(
                f"a LIVE submission was attempted while the durable trading mode is "
                f"{disposition.trading_mode.value}; live is reachable only through "
                f"arm_live_trading with an operator authorization (R.22). "
                f"{disposition.describe()}"
            )
        return disposition

    # -- writing -----------------------------------------------------------------------------

    def latch(
        self,
        *,
        latched_by: str,
        reason: str,
        authority: ChangeAuthority = ChangeAuthority.SYSTEM,
        latched_at: datetime | None = None,
    ) -> LatchDisposition:
        """Close the latch. Needs no authority, because stopping is always allowed.

        Idempotent by design: re-latching an already-latched store with the same reason appends
        nothing. A watchdog polling every few seconds would otherwise write a row per poll and bury
        the transition that matters under a million identical ones.
        """
        current = self.read_disposition()
        if (
            current.latch_state is LatchState.LATCHED
            and current.reason == reason
            and current.evidence is LatchEvidence.RECORDED_TRANSITION
        ):
            return current
        return self._append_transition(
            trading_mode=current.trading_mode,
            latch_state=LatchState.LATCHED,
            changed_by=latched_by,
            change_authority=authority,
            reason=reason,
            changed_at=latched_at,
            require_verified_chain=False,
        )

    def release_latch(
        self,
        *,
        authorization: OperatorAuthorization,
        reason: str,
        released_at: datetime | None = None,
    ) -> LatchDisposition:
        """Open the latch. An operator act with the matching scope, never anything else."""
        _require_scope(authorization, OperatorAuthorityScope.RELEASE_TRADING_HALT)
        current = self.read_disposition()
        return self._append_transition(
            trading_mode=current.trading_mode,
            latch_state=LatchState.RELEASED,
            changed_by=authorization.operator_name,
            change_authority=ChangeAuthority.OPERATOR,
            reason=reason,
            changed_at=released_at,
            require_verified_chain=True,
        )

    def arm_live_trading(
        self,
        *,
        authorization: OperatorAuthorization,
        reason: str,
        armed_at: datetime | None = None,
    ) -> LatchDisposition:
        """Raise the durable mode to LIVE. The system itself has no path to this state.

        Refuses unless the latch is already closed, and leaves it closed. Arming and releasing are
        two acts on purpose: a single call that armed live *and* opened the gate would let one
        mistyped command take a system from idle to live-fire in one step.
        """
        _require_scope(authorization, OperatorAuthorityScope.ARM_LIVE_TRADING)
        current = self.read_disposition()
        if current.latch_state is not LatchState.LATCHED:
            raise OperatorAuthorityError(
                "live trading can only be armed while the halt latch is CLOSED, so that going "
                "live is always halt -> arm -> release and never a mode flip underneath a running "
                f"trader. The latch is currently {current.latch_state.value}"
            )
        return self._append_transition(
            trading_mode=TradingMode.LIVE,
            latch_state=LatchState.LATCHED,
            changed_by=authorization.operator_name,
            change_authority=ChangeAuthority.OPERATOR,
            reason=reason,
            changed_at=armed_at,
            require_verified_chain=True,
        )

    def revert_to_paper_trading(
        self,
        *,
        reverted_by: str,
        reason: str,
        authority: ChangeAuthority = ChangeAuthority.SYSTEM,
        reverted_at: datetime | None = None,
    ) -> LatchDisposition:
        """Drop the durable mode back to paper and latch on the way. Always available to anyone."""
        return self._append_transition(
            trading_mode=TradingMode.PAPER,
            latch_state=LatchState.LATCHED,
            changed_by=reverted_by,
            change_authority=authority,
            reason=reason,
            changed_at=reverted_at,
            require_verified_chain=False,
        )

    def _append_transition(
        self,
        *,
        trading_mode: TradingMode,
        latch_state: LatchState,
        changed_by: str,
        change_authority: ChangeAuthority,
        reason: str,
        changed_at: datetime | None,
        require_verified_chain: bool,
    ) -> LatchDisposition:
        if not changed_by.strip():
            raise TradingControlStoreError(
                "a control transition with no author cannot be audited, and the first question "
                "after an incident is who changed it"
            )
        if not reason.strip():
            raise TradingControlStoreError(
                "a control transition with no reason is a change nobody can review; the reason is "
                "the payload, the state is only the consequence"
            )
        moment = changed_at or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise TradingControlStoreError(
                f"the transition time {moment!r} carries no timezone; an audit trail with "
                f"ambiguous instants cannot be replayed against a session"
            )
        if self._initialisation_failure is not None:
            raise TradingControlStoreError(
                f"the latch store at {self.database_path} is unusable "
                f"({self._initialisation_failure}), so this change did NOT happen. Reads continue "
                f"to answer HALTED, which is the safe half of this failure"
            )
        try:
            chain = self._read_transition_chain()
        except (sqlite3.Error, OSError, ValueError) as failure:
            raise TradingControlStoreError(
                f"the latch store at {self.database_path} could not be read before writing "
                f"({type(failure).__name__}: {failure}); the change did NOT happen"
            ) from failure
        if chain is None:
            if require_verified_chain:
                raise OperatorAuthorityError(
                    f"the transition chain in {self.database_path} does not verify, so this "
                    f"system cannot be armed or released until the log is reconstructed. Moves "
                    f"toward safety remain available"
                )
            chain = self._read_transition_chain(verify=False) or []
            previous_digest = chain[-1].record_digest if chain else _CHAIN_ORIGIN_DIGEST
        else:
            previous_digest = chain[-1].record_digest if chain else _CHAIN_ORIGIN_DIGEST
        sequence_number = (chain[-1].sequence_number + 1) if chain else 1
        record = LatchTransitionRecord(
            sequence_number=sequence_number,
            changed_at=moment,
            trading_mode=trading_mode,
            latch_state=latch_state,
            changed_by=changed_by.strip(),
            change_authority=change_authority,
            reason=reason.strip(),
            previous_digest=previous_digest,
            record_digest="",
        )
        digest = record.expected_digest()
        try:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO trading_control_transition "  # noqa: S608 — a module constant
                    f"({_TRANSITION_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        record.sequence_number,
                        record.changed_at.astimezone(UTC).isoformat(),
                        str(record.trading_mode),
                        str(record.latch_state),
                        record.changed_by,
                        str(record.change_authority),
                        record.reason,
                        record.previous_digest,
                        digest,
                    ),
                )
                # Same transaction as the row above, so the pointer and the row it names can never
                # be observed disagreeing except by tampering or by damage.
                connection.execute(
                    "INSERT OR REPLACE INTO trading_control_head "
                    "(singleton_id, head_sequence_number, head_digest) VALUES (1, ?, ?)",
                    (record.sequence_number, digest),
                )
                connection.commit()
                # Fold the WAL back into the database file itself. Measured on this host: without
                # it the `-wal` sidecar holds the newest transitions indefinitely, so the
                # `.sqlite3` file an operator copies, inspects or restores is NOT the control
                # state — a backup of it alone would silently reinstate an older, more permissive
                # latch. Control transitions are rare (operator acts and halts), so the extra
                # checkpoint costs nothing that matters.
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as failure:
            raise TradingControlStoreError(
                f"the control transition could not be committed to {self.database_path} "
                f"({type(failure).__name__}: {failure}); the change did NOT happen and must not "
                f"be reported as though it had"
            ) from failure
        return LatchDisposition(
            trading_mode=record.trading_mode,
            latch_state=record.latch_state,
            changed_by=record.changed_by,
            change_authority=record.change_authority,
            changed_at=record.changed_at,
            reason=record.reason,
            evidence=LatchEvidence.RECORDED_TRANSITION,
        )


def _require_scope(
    authorization: OperatorAuthorization, expected_scope: OperatorAuthorityScope
) -> None:
    if authorization.authority_scope is not expected_scope:
        raise OperatorAuthorityError(
            f"this authorization permits {authorization.authority_scope.value}, not "
            f"{expected_scope.value}; an authorization that stretched to cover a neighbouring act "
            f"would make the scope decorative"
        )


def _head_agrees_with_chain(
    head_rows: list[sqlite3.Row], records: list[LatchTransitionRecord]
) -> bool:
    """Whether the head pointer names exactly the newest row that is actually present.

    Four ways to disagree, and all four are the same verdict: more than one head, a head with no
    chain, a chain with no head, and a head naming a sequence or digest the newest row does not
    have. The last of those is the one that matters — it is what a tail truncation looks like.
    """
    if len(head_rows) > 1:
        return False
    head = head_rows[0] if head_rows else None
    if head is None:
        return not records
    if not records:
        return False
    newest = records[-1]
    if int(head["head_sequence_number"]) != newest.sequence_number:
        return False
    return hmac.compare_digest(str(head["head_digest"]), newest.record_digest)


def _record_from_row(row: sqlite3.Row) -> LatchTransitionRecord:
    return LatchTransitionRecord(
        sequence_number=int(row["sequence_number"]),
        changed_at=datetime.fromisoformat(str(row["changed_at_utc"])),
        trading_mode=TradingMode(str(row["trading_mode"])),
        latch_state=LatchState(str(row["latch_state"])),
        changed_by=str(row["changed_by"]),
        change_authority=ChangeAuthority(str(row["change_authority"])),
        reason=str(row["reason"]),
        previous_digest=str(row["previous_digest"]),
        record_digest=str(row["record_digest"]),
    )
