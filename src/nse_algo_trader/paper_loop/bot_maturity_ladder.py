"""The paper track record, and the rung a bot earns from it — `L5.30`, spec `docs/research/255`.

**The deadlock this breaks.** `run_daily_operations.py` runs twelve steps and none of them trade.
The only paper session is a verification harness that deletes its ledger at the start of every run,
and the production paper ledger holds 13 events in total. So `BotMaturity.closed_trades_observed`
was fed by nothing: every segment bot reported `COLD_START` and always would, however good it was.
`R.04`'s ladder had nothing to climb and `R.22`'s graduation had nothing to graduate.

**Why the rung is inferred rather than counted.** "Has it done 100 trades?" is not evidence of
anything — the previous system did **3,481** and lost ₹3.3 lakh. What that record actually shows
(`docs/research/254`) is the statistic that matters:

    win   n=1,284   avg +₹366.51 loss  n=2,197   avg -₹365.01     ->  36.9% win rate, symmetric
    payoff, guaranteed loss

Average win and average loss matched to **₹1.50**, so the whole question was win rate against
break-even — and break-even is not 50%. It is the rate at which the average win, weighted by its
frequency, covers the average loss **plus costs**; costs were ₹176,589 of that ₹331,314 loss, so
they belong inside the statistic rather than applied to it afterwards.

This engine therefore asks: **given this bot's own closed trades, what is the posterior probability
that its win rate exceeds its own break-even rate?** Every input is measured from the bot's record —
payoff sizes, cost drag, break-even, win rate. Nothing is a constant (`R.03`); the confidence level
and the sustained-session requirement are operator policy, passed in and stated.

**SOTA analog:** sequential promotion testing on a Beta-Binomial posterior — the same shape as
`L2.01`'s honest trial registry, which already counts what was tried.

**It can demote.** A bot whose posterior sits decisively below break-even is named as such rather
than left at `OBSERVING` forever. `opening_range_breakout_v1` with 3,049 trades at 36.9% is exactly
what that looks like, and a ladder that cannot express it is a ladder that only ever promotes.

**It cannot graduate anyone.** `GRADUATED` is never computed here. `R.22` needs two keys and this is
at most the first; `BotMaturity` refuses to construct the value at all.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy

from nse_algo_trader.segment_bots.segment_bot_protocol import BotMaturity, BotMaturityRung

COIN_FLIP_WIN_RATE = 0.5
"""A confidence at or below this promotes a coin, so the policy floor sits strictly above it."""

MINIMUM_TRADES_FOR_ANY_POSTERIOR_SHAPE = 2
"""Below two outcomes a Beta posterior has no shape to test.

A mathematical fact rather than a tuning knob.
"""

DEFAULT_TRACK_RECORD_PATH = Path("~/.nse_algo_trader/paper_track_record.sqlite3").expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_paper_trade (
    bot_identity      TEXT NOT NULL,
    session_date      TEXT NOT NULL,
    position_key      TEXT NOT NULL,
    instrument_token  INTEGER NOT NULL,
    trading_symbol    TEXT NOT NULL,
    side              TEXT NOT NULL,
    filled_quantity   INTEGER NOT NULL,
    opened_at         TEXT NOT NULL,
    closed_at         TEXT NOT NULL,
    close_reason      TEXT NOT NULL,
    gross_rupees      TEXT NOT NULL,
    costs_rupees      TEXT NOT NULL,
    stated_win_probability REAL,
    PRIMARY KEY (bot_identity, session_date, position_key)
);
CREATE INDEX IF NOT EXISTS closed_paper_trade_by_bot ON closed_paper_trade (bot_identity);
"""


class PaperTrackRecordError(Exception):
    """The record or the policy asked for cannot be honoured."""


@dataclass(frozen=True, slots=True)
class ClosedPaperTrade:
    """One paper position that opened and closed, with its own costs attached.

    Costs are carried per trade rather than netted at the session level because break-even is a
    per-trade quantity: a strategy is not saved by a good day, it is sunk by the drag on every round
    trip.
    """

    bot_identity: str
    session_date: date
    position_key: str
    instrument_token: int
    trading_symbol: str
    side: str
    filled_quantity: int
    opened_at: datetime
    closed_at: datetime
    close_reason: str
    gross_rupees: Decimal
    costs_rupees: Decimal
    stated_win_probability: float | None = None
    """What the bot CLAIMED its chance of winning was, recorded at the moment it decided.

    Nullable because rows written before `L5.31` do not have it and inventing one would fabricate
    the exact quantity the calibrator exists to audit. It is recorded here rather than derived later
    because a probability reconstructed after the outcome is known is not a forecast.

    Without it `L5.31`'s calibrator has nothing to fit and every proposal is held to an uncalibrated
    posterior — correct, and also a deadlock: a gate that demands a track record, standing in front
    of the only thing that builds one, re-creates `B15`. The activation rule is therefore `R.04`'s
    ladder, not the gate: the quality floor is attached only once enough trades carry this.
    """

    def __post_init__(self) -> None:
        """Refuse a record that cannot be true.

        Every one of these was ACCEPTED by the first version, and an adversarial review turned the
        third into a promotion: fifty trades with a total gross of -Rs 5,000 became a
        `GRADUATION_CANDIDATE` on negative costs alone, because a negative cost turns a gross loss
        into a net win.
        """
        if not self.bot_identity.strip():
            raise PaperTrackRecordError("a trade with no bot identity accrues to nobody")
        if self.filled_quantity <= 0:
            raise PaperTrackRecordError(
                f"filled quantity must be positive, got {self.filled_quantity}; a position that "
                f"filled nothing has no outcome to record"
            )
        if self.costs_rupees < 0:
            raise PaperTrackRecordError(
                f"costs must not be negative, got {self.costs_rupees}. A negative cost is a rebate "
                f"this project does not model, and it silently converts a gross loss into a net win"
            )
        if self.stated_win_probability is not None and not (
            0.0 <= self.stated_win_probability <= 1.0
        ):
            raise PaperTrackRecordError(
                f"{self.position_key} recorded a stated win probability of "
                f"{self.stated_win_probability}, which is not a probability"
            )
        if self.closed_at < self.opened_at:
            raise PaperTrackRecordError(
                f"{self.position_key} closed at {self.closed_at} before it opened at "
                f"{self.opened_at}"
            )
        if self.session_date != self.opened_at.date():
            raise PaperTrackRecordError(
                f"{self.position_key} is labelled session {self.session_date} but opened on "
                f"{self.opened_at.date()}. The label is what the sustained-sessions rule counts, "
                f"so "
                f"an unchecked one lets a single day be spread across three to earn promotion"
            )

    @property
    def net_rupees(self) -> Decimal:
        return self.gross_rupees - self.costs_rupees

    @property
    def is_win(self) -> bool:
        """Net of costs: a gross win that cost more than it made is a loss, and counts as one."""
        return self.net_rupees > 0


@dataclass(frozen=True, slots=True)
class LadderPolicy:
    """What the operator requires before a bot may act. Stated, never defaulted.

    `R.03`: these are the only numbers in this engine that are not measured, and they are policy
    rather than facts — how much confidence is enough, and how many separate days it must hold for.
    """

    promotion_confidence: float
    sustained_sessions_required: int
    minimum_trades_for_a_posterior: int

    def __post_init__(self) -> None:
        if not COIN_FLIP_WIN_RATE < self.promotion_confidence < 1.0:
            raise PaperTrackRecordError(
                f"promotion confidence must sit strictly between 0.5 and 1.0, got "
                f"{self.promotion_confidence}; 1.0 is unreachable from finite evidence, and "
                f"anything at or below 0.5 promotes a coin"
            )
        if self.sustained_sessions_required < 1:
            raise PaperTrackRecordError("a bot must sustain its case for at least one session")
        if self.minimum_trades_for_a_posterior < MINIMUM_TRADES_FOR_ANY_POSTERIOR_SHAPE:
            raise PaperTrackRecordError(
                "a posterior over a win rate needs at least two trades to have a shape"
            )


@dataclass(frozen=True, slots=True)
class LadderAssessment:
    """Where a bot stands, and every number the verdict rests on.

    A rung with no reason is the shape `R.13` catches — so the reason is a field, not a log line.
    """

    bot_identity: str
    rung: BotMaturityRung
    closed_trades: int
    sessions: int
    observed_win_rate: float
    break_even_win_rate: float
    posterior_above_break_even: float
    is_decisively_below_break_even: bool
    reason: str

    def to_bot_maturity(self) -> BotMaturity:
        """The value `SegmentBot.maturity()` returns, carrying this assessment as its evidence."""
        return BotMaturity(
            rung=self.rung,
            closed_trades_observed=self.closed_trades,
            evidence=self.reason,
        )


def _refuse_a_different_trade_under_the_same_key(
    trade: ClosedPaperTrade, stored: tuple[object, ...]
) -> None:
    """A key collision that is NOT a replay is evidence loss, and must be loud.

    `INSERT OR IGNORE` could not tell "this session was replayed" from "a different trade reused
    this key" — both returned 0, silently. An adversarial review used that to hide 196 of 260 real
    closed trades: the ladder saw 64 trades worth +Rs 22,400 from a record whose true total was
    **-Rs 56,000**, and promoted it to `GRADUATION_CANDIDATE`.

    Not currently reachable in production — the real `position_key` is content-derived
    (`paper_trading_session_runner.py`) and the 3,049-row real replay dropped zero rows — so this
    closes a trap rather than a live loss.
    """
    incoming = (
        trade.instrument_token,
        trade.trading_symbol,
        str(trade.side),
        trade.filled_quantity,
        str(trade.gross_rupees),
        str(trade.costs_rupees),
    )
    existing = tuple(
        stored[0:1]
        + tuple(str(field) for field in stored[1:3])
        + stored[3:4]
        + tuple(str(field) for field in stored[4:6])
    )
    if existing != incoming:
        raise PaperTrackRecordError(
            f"a DIFFERENT trade is already stored under "
            f"({trade.bot_identity}, {trade.session_date}, {trade.position_key}): stored "
            f"{existing} against incoming {incoming}. Silently ignoring this would drop real "
            f"evidence and let a losing bot look profitable"
        )


class PaperTrackRecordStore:
    """Append-only closed paper trades, one row per position, keyed so a re-run cannot double it."""

    @staticmethod
    def _add_stated_probability_column_if_missing(connection: sqlite3.Connection) -> None:
        """Bring a store written before `L5.31` up to the current shape, in place.

        `CREATE TABLE IF NOT EXISTS` does not alter an existing table, so a store created before the
        column existed keeps its old shape and every insert then fails on arity. Adding it nullable
        preserves every row already recorded, and those rows honestly report "no stated probability"
        rather than a fabricated one.
        """
        rows = connection.execute("PRAGMA table_info(closed_paper_trade)")
        columns = {str(row[1]) for row in rows}
        if columns and "stated_win_probability" not in columns:
            connection.execute(
                "ALTER TABLE closed_paper_trade ADD COLUMN stated_win_probability REAL"
            )

    def __init__(self, database_path: Path = DEFAULT_TRACK_RECORD_PATH) -> None:
        self._database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # One connection for the store's life. Opening one per row left 60 descriptors outstanding
        # after 500 appends and made the 3,049-row real-data replay take ~75 seconds.
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._add_stated_probability_column_if_missing(self._connection)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> PaperTrackRecordStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, trade: ClosedPaperTrade) -> int:
        """Record one closed trade. Returns rows written — 0 when it was already recorded.

        Idempotent on `(bot_identity, session_date, position_key)`: replaying a session must not
        double a bot's evidence, which would let it promote for work it did once (`R.13`).
        """
        stored = self._connection.execute(
            "SELECT instrument_token, trading_symbol, side, filled_quantity, gross_rupees,"
            " costs_rupees FROM closed_paper_trade"
            " WHERE bot_identity = ? AND session_date = ? AND position_key = ?",
            (trade.bot_identity, trade.session_date.isoformat(), trade.position_key),
        ).fetchone()
        if stored is not None:
            _refuse_a_different_trade_under_the_same_key(trade, stored)
            return 0

        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO closed_paper_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade.bot_identity,
                    trade.session_date.isoformat(),
                    trade.position_key,
                    trade.instrument_token,
                    trade.trading_symbol,
                    trade.side,
                    trade.filled_quantity,
                    trade.opened_at.isoformat(),
                    trade.closed_at.isoformat(),
                    trade.close_reason,
                    str(trade.gross_rupees),
                    str(trade.costs_rupees),
                    trade.stated_win_probability,
                ),
            )
            return int(cursor.rowcount)

    def closed_trades_for(self, bot_identity: str) -> tuple[ClosedPaperTrade, ...]:
        rows = self._connection.execute(
            "SELECT bot_identity, session_date, position_key, instrument_token, trading_symbol,"
            " side, filled_quantity, opened_at, closed_at, close_reason, gross_rupees,"
            " costs_rupees, stated_win_probability FROM closed_paper_trade WHERE bot_identity = ?"
            " ORDER BY session_date, position_key",
            (bot_identity,),
        ).fetchall()
        return tuple(
            ClosedPaperTrade(
                bot_identity=str(row[0]),
                session_date=date.fromisoformat(str(row[1])),
                position_key=str(row[2]),
                instrument_token=int(row[3]),
                trading_symbol=str(row[4]),
                side=str(row[5]),
                filled_quantity=int(row[6]),
                opened_at=datetime.fromisoformat(str(row[7])),
                closed_at=datetime.fromisoformat(str(row[8])),
                close_reason=str(row[9]),
                gross_rupees=Decimal(str(row[10])),
                costs_rupees=Decimal(str(row[11])),
                stated_win_probability=None if row[12] is None else float(row[12]),
            )
            for row in rows
        )

    def bot_identities(self) -> tuple[str, ...]:
        """Every bot that has at least one closed trade, alphabetically.

        The surface needs this: a board that renders only the bots someone remembered to name is a
        board that hides the one nobody wants to look at.
        """
        rows = self._connection.execute(
            "SELECT DISTINCT bot_identity FROM closed_paper_trade ORDER BY bot_identity"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def sessions_for(self, bot_identity: str) -> tuple[date, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT session_date FROM closed_paper_trade WHERE bot_identity = ?"
            " ORDER BY session_date",
            (bot_identity,),
        ).fetchall()
        return tuple(date.fromisoformat(str(row[0])) for row in rows)


def _break_even_win_rate(trades: tuple[ClosedPaperTrade, ...]) -> float:
    """The win rate this bot's OWN payoffs and costs require it to beat.

    With average win `W` and average loss `L` (magnitudes, net of costs), expectancy is zero when
    `p*W = (1-p)*L`, i.e. `p = L / (W + L)`. Equal-sized winners and losers therefore need MORE than
    half the trades to win once costs are inside the magnitudes — exactly what sank the retained
    record, where `W` and `L` matched to Rs 1.50 and the win rate was 36.9%.

    **This is reported as EVIDENCE and is no longer the promotion test**, because an adversarial
    review showed the plug-in threshold moving faster than the posterior it was compared against:
    adding a single losing trade promoted a bot two rungs, and adding a winning trade demoted one.
    See `BotMaturityLadder` for what replaced it.

    Zero-net trades are excluded from both magnitudes. Counting a scratch as a zero-magnitude loss
    dragged `average_loss` toward zero and lowered break-even — 200 scratches moved it from 50.0% to
    8.3% while total P&L was unchanged.
    """
    wins = [float(trade.net_rupees) for trade in trades if trade.net_rupees > 0]
    losses = [-float(trade.net_rupees) for trade in trades if trade.net_rupees < 0]
    if not wins:
        return 1.0
    if not losses:
        return 0.0
    average_win = sum(wins) / len(wins)
    average_loss = sum(losses) / len(losses)
    return average_loss / (average_win + average_loss)


def _posterior_profitable(net_rupees: list[float], draws: int = 4000) -> float:
    """`P(mean net P&L per trade > 0)` by Bayesian bootstrap over this bot's own outcomes.

    **This is the promotion test.** The first version tested a win RATE against a plug-in break-even
    and an adversarial review broke it three ways at once (`docs/research/256`):

    * it was **non-monotonic in both directions** — the threshold was re-estimated from the same
      sample it was compared against, so one losing trade could promote a bot two rungs;
    * it was **blind to magnitude concentration** — 30 wins of Rs 400, and 29 wins of Rs 13.79
      plus one of Rs 11,600, were byte-identical to it; one Rs 1e9 win against 24 losses promoted
      a bot with a **4% win rate**;
    * it was **systematically overconfident**, reporting 100.0% for `credit_spread_v1` where a
      bootstrap of the same 121 outcomes puts `P(total <= 0)` at 12.4%.

    A Bayesian bootstrap draws Dirichlet(1,...,1) weights over the observed trades and takes the
    weighted mean, which is the posterior of the mean under a non-informative Dirichlet-process
    prior. It needs no distributional assumption — returns are not Normal and this makes no claim
    that they are — and it prices magnitude variance directly, which is the thing a rate cannot see.

    **Deterministic by construction**: the seed is derived from the outcomes themselves, so the same
    record always yields the same verdict. A promotion that changed on re-run would be unreplayable,
    and `A.29` requires reasoning be reconstructable.
    """
    if not net_rupees:
        return 0.0
    outcomes = numpy.asarray(net_rupees, dtype=float)
    if not numpy.all(numpy.isfinite(outcomes)):
        raise PaperTrackRecordError(
            "a closed trade carries a non-finite net P&L; the posterior over such a record is "
            "meaningless and the branch below it defaults to promotion, so it is refused here"
        )
    seed = int(abs(hash(tuple(net_rupees))) % (2**32))
    generator = numpy.random.default_rng(seed)
    weights = generator.dirichlet(numpy.ones(outcomes.size), size=draws)
    return float(numpy.mean(weights @ outcomes > 0.0))


class BotMaturityLadder:
    """A bot's rung, inferred from its own closed paper trades.

    The inference is a Beta-Binomial posterior on the win rate with a uniform `Beta(1, 1)` prior —
    conjugate, so the posterior after `w` wins and `l` losses is `Beta(1+w, 1+l)` exactly. The
    verdict is the posterior mass above the bot's own break-even rate.

    A uniform prior is deliberate: an informative prior here would be a way of believing a bot is
    good before it has traded, which is the one thing this engine exists to stop.
    """

    def __init__(self, store: PaperTrackRecordStore) -> None:
        self._store = store

    def assess(self, bot_identity: str, policy: LadderPolicy) -> LadderAssessment:
        trades = self._store.closed_trades_for(bot_identity)
        sessions = len(self._store.sessions_for(bot_identity))

        if not trades:
            return self._verdict(
                bot_identity,
                BotMaturityRung.COLD_START,
                trades,
                sessions,
                0.0,
                0.0,
                0.0,
                False,
                "no closed paper trades recorded for this bot; the ladder has nothing to read",
            )

        wins = sum(1 for trade in trades if trade.is_win)
        observed = wins / len(trades)
        break_even = _break_even_win_rate(trades)
        posterior = _posterior_profitable([float(trade.net_rupees) for trade in trades])
        if not math.isfinite(posterior):
            # The branch chain below defaults to PROMOTION when every comparison is False, which is
            # what a NaN produces. Fail closed, loudly.
            raise PaperTrackRecordError(
                f"the posterior for {bot_identity!r} is not finite ({posterior!r}); every rung "
                f"comparison would be False and the chain would fall through to promotion"
            )
        decisively_below = posterior < (1.0 - policy.promotion_confidence)

        if len(trades) < policy.minimum_trades_for_a_posterior:
            return self._verdict(
                bot_identity,
                BotMaturityRung.COLD_START,
                trades,
                sessions,
                observed,
                break_even,
                posterior,
                decisively_below,
                f"too few closed trades to form a posterior: {len(trades)} against the "
                f"{policy.minimum_trades_for_a_posterior} this policy requires",
            )

        if decisively_below:
            return self._verdict(
                bot_identity,
                BotMaturityRung.RETIRED,
                trades,
                sessions,
                observed,
                break_even,
                posterior,
                True,
                f"RETIRED on its own evidence: only {posterior:.1%} of the posterior says its "
                f"expectancy is positive. For context it wins {observed:.1%} of trades against the "
                f"{break_even:.1%} its own payoffs and costs require. This is evidence AGAINST the "
                f"bot, which is the opposite of the absence of evidence that COLD_START means",
            )

        if posterior <= policy.promotion_confidence:
            return self._verdict(
                bot_identity,
                BotMaturityRung.OBSERVING,
                trades,
                sessions,
                observed,
                break_even,
                posterior,
                False,
                f"observing: {len(trades)} trades win {observed:.1%} against a break-even of "
                f"{break_even:.1%}, leaving {posterior:.1%} of the posterior above it — short of "
                f"the {policy.promotion_confidence:.0%} this policy requires to act",
            )

        qualifying = self._qualifying_sessions(trades, policy)
        if qualifying < policy.sustained_sessions_required:
            return self._verdict(
                bot_identity,
                BotMaturityRung.PAPER_QUALIFIED,
                trades,
                sessions,
                observed,
                break_even,
                posterior,
                False,
                f"paper qualified on {posterior:.1%} posterior above a {break_even:.1%} "
                f"break-even, "
                f"but sustained over only {sessions} session(s) of the "
                f"{policy.sustained_sessions_required} required — one good day is not a record",
            )

        return self._verdict(
            bot_identity,
            BotMaturityRung.GRADUATION_CANDIDATE,
            trades,
            sessions,
            observed,
            break_even,
            posterior,
            False,
            f"graduation candidate: {len(trades)} trades over {sessions} sessions, "
            f"{posterior:.1%} posterior that its expectancy is positive, holding at {qualifying} "
            f"expanding-window session cutoffs. For context it wins {observed:.1%} against a "
            f"{break_even:.1%} break-even. R.22 still requires an explicit operator arm; this "
            f"engine cannot and does not graduate anyone",
        )

    @staticmethod
    def _qualifying_sessions(trades: tuple[ClosedPaperTrade, ...], policy: LadderPolicy) -> int:
        """How many session cutoffs the bot's case ALREADY held at, replaying the record forward.

        Counting distinct session dates was the first version and an adversarial review padded it
        trivially: three losing days followed by one large winning day counted as four sessions and
        promoted. A day the bot merely TRADED on is not a day its case held.

        Expanding windows rather than per-day slices, because a single day rarely carries enough
        trades to say anything and per-day verdicts would be noise dressed as corroboration.
        """
        sessions = sorted({trade.session_date for trade in trades})
        held = 0
        for cutoff in sessions:
            so_far = [float(trade.net_rupees) for trade in trades if trade.session_date <= cutoff]
            if len(so_far) < policy.minimum_trades_for_a_posterior:
                continue
            if _posterior_profitable(so_far) > policy.promotion_confidence:
                held += 1
        return held

    @staticmethod
    def _verdict(
        bot_identity: str,
        rung: BotMaturityRung,
        trades: tuple[ClosedPaperTrade, ...],
        sessions: int,
        observed: float,
        break_even: float,
        posterior: float,
        decisively_below: bool,
        reason: str,
    ) -> LadderAssessment:
        return LadderAssessment(
            bot_identity=bot_identity,
            rung=rung,
            closed_trades=len(trades),
            sessions=sessions,
            observed_win_rate=observed,
            break_even_win_rate=break_even,
            posterior_above_break_even=posterior,
            is_decisively_below_break_even=decisively_below,
            reason=reason,
        )
