"""`L0.11` — the firewall's refusals, and the ways a firewall can be quietly useless.

A leakage guard that admits too much produces a backtest that looks BETTER than reality,
so these tests are written from the attacker's side: every case is an attempt to get a
row through that should not pass.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from nse_algo_trader.causal_leakage_firewall import (
    MAXIMUM_CREDIBLE_PUBLICATION_LAG_DAYS,
    CausalLeakageFirewall,
    CausalOrderingError,
    FutureLeakageError,
    LeakageReason,
    ObservableRow,
    SourcePublicationLag,
    derive_publication_lags,
)

SESSION_OPEN = datetime(2026, 8, 11, 9, 15, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 8, 11, 15, 30, tzinfo=UTC)
TODAY = date(2026, 8, 11)

SAME_DAY = {"live": SourcePublicationLag("live", days=0, observations_used=100)}
NEXT_DAY = {"bhavcopy": SourcePublicationLag("bhavcopy", days=1, observations_used=100)}
UNKNOWN = {"static": SourcePublicationLag("static", days=None, observations_used=328)}


def _firewall(lags: dict[str, SourcePublicationLag], **kwargs: bool) -> CausalLeakageFirewall:
    return CausalLeakageFirewall(SESSION_OPEN, SESSION_CLOSE, lags, **kwargs)


def _row(source: str, effective: date) -> ObservableRow:
    # observed_at is our FETCH time and deliberately late, because guarding on it would
    # block legitimate backfill — the firewall must ignore it when judging availability.
    return ObservableRow(source, effective, datetime(2026, 8, 11, 23, 0, tzinfo=UTC))


@pytest.mark.unit
def test_a_future_event_is_refused() -> None:
    firewall = _firewall(SAME_DAY)
    with pytest.raises(FutureLeakageError) as raised:
        firewall.assert_observable(_row("live", date(2026, 8, 12)))
    assert raised.value.reason is LeakageReason.EVENT_IN_THE_FUTURE


@pytest.mark.unit
def test_todays_event_is_refused_when_the_source_only_publishes_tomorrow() -> None:
    """The leak the archived firewall could not see.

    The event is not in the future — it is today — so an event-date guard admits it. But
    a next-day source had not published it yet, so no trader could have held it.
    """
    firewall = _firewall(NEXT_DAY)
    with pytest.raises(FutureLeakageError) as raised:
        firewall.assert_observable(_row("bhavcopy", TODAY))
    assert raised.value.reason is LeakageReason.NOT_YET_PUBLISHED


@pytest.mark.unit
def test_yesterdays_event_from_a_next_day_source_is_admitted() -> None:
    """The firewall must not be so strict that honest replay becomes impossible."""
    firewall = _firewall(NEXT_DAY)
    firewall.assert_observable(_row("bhavcopy", date(2026, 8, 10)))
    assert firewall.ledger.admitted == 1


@pytest.mark.unit
def test_backfilled_data_is_admitted_despite_a_late_fetch_time() -> None:
    """`observed_at` is when WE fetched, not when it became public.

    90.6% of the real corpus was fetched more than a day after the event. Guarding on
    fetch time would block essentially the entire history from ever being replayed.
    """
    firewall = _firewall(NEXT_DAY)
    ancient = ObservableRow("bhavcopy", date(2020, 1, 2), datetime(2026, 8, 11, 23, 0, tzinfo=UTC))
    firewall.assert_observable(ancient)
    assert firewall.ledger.admitted == 1


@pytest.mark.unit
def test_an_unknown_publication_schedule_is_blocked_by_default() -> None:
    """Default CLOSED — the unknown source is the one most likely to leak."""
    firewall = _firewall(UNKNOWN)
    with pytest.raises(FutureLeakageError) as raised:
        firewall.assert_observable(_row("static", date(2020, 1, 1)))
    assert raised.value.reason is LeakageReason.PUBLICATION_SCHEDULE_UNKNOWN


@pytest.mark.unit
def test_an_unregistered_source_is_blocked_rather_than_waved_through() -> None:
    """A source the firewall has never heard of must not be the easy way past it."""
    firewall = _firewall(SAME_DAY)
    with pytest.raises(FutureLeakageError):
        firewall.assert_observable(_row("never_seen_before", date(2020, 1, 1)))


@pytest.mark.unit
def test_unknown_sources_can_be_admitted_only_by_explicit_opt_in() -> None:
    firewall = _firewall(UNKNOWN, block_unknown_sources=False)
    firewall.assert_observable(_row("static", date(2020, 1, 1)))
    assert firewall.ledger.admitted == 1


@pytest.mark.unit
def test_the_clock_never_rewinds() -> None:
    firewall = _firewall(SAME_DAY)
    firewall.advance_to(datetime(2026, 8, 11, 12, 0, tzinfo=UTC))
    with pytest.raises(CausalOrderingError):
        firewall.advance_to(datetime(2026, 8, 11, 11, 0, tzinfo=UTC))


@pytest.mark.unit
def test_the_clock_clamps_at_the_close_rather_than_running_past_it() -> None:
    firewall = _firewall(SAME_DAY)
    firewall.advance_to(datetime(2026, 8, 11, 23, 0, tzinfo=UTC))
    assert firewall.virtual_now == SESSION_CLOSE


@pytest.mark.unit
def test_filtering_counts_every_refusal_instead_of_dropping_it() -> None:
    """The archived version `continue`d past blocked data, which is a silent skip.

    A replay that saw nothing because everything was blocked must be distinguishable
    from one that legitimately had nothing to see.
    """
    firewall = _firewall(NEXT_DAY)
    rows = [
        _row("bhavcopy", date(2026, 8, 10)),
        _row("bhavcopy", TODAY),
        _row("bhavcopy", date(2026, 8, 12)),
    ]
    admitted = list(firewall.filter_observable(rows))
    assert len(admitted) == 1
    assert firewall.ledger.total_blocked == 2
    assert firewall.ledger.blocked[LeakageReason.EVENT_IN_THE_FUTURE] == 1
    assert firewall.ledger.blocked[LeakageReason.NOT_YET_PUBLISHED] == 1
    assert "blocked" in firewall.ledger.describe()


@pytest.mark.unit
def test_the_lag_is_the_minimum_observed_not_the_median() -> None:
    """Backfill inflates every other statistic; the fastest sighting is the honest bound."""
    lags = derive_publication_lags(
        [
            ("bhavcopy", date(2026, 8, 10), datetime(2026, 8, 11, 19, 0, tzinfo=UTC)),
            ("bhavcopy", date(2020, 1, 2), datetime(2026, 8, 11, 19, 0, tzinfo=UTC)),
            ("bhavcopy", date(2020, 1, 3), datetime(2026, 8, 11, 19, 0, tzinfo=UTC)),
        ]
    )
    assert lags["bhavcopy"].days == 1
    assert lags["bhavcopy"].observations_used == 3


@pytest.mark.unit
def test_a_source_never_seen_fresh_yields_no_schedule_rather_than_a_huge_one() -> None:
    """`delisted_securities_master` really does show a 2,101-day minimum in the corpus.

    Treating that as a publication schedule would let 2,101-day-old rows through under a
    fabricated rule. It is an absence of knowledge and must read as one.
    """
    event = date(2020, 1, 1)
    seen = datetime.combine(
        event + timedelta(days=MAXIMUM_CREDIBLE_PUBLICATION_LAG_DAYS + 1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    lags = derive_publication_lags([("static", event, seen)])
    assert lags["static"].days is None
    assert not lags["static"].is_derivable
    assert lags["static"].knowable_from(date(2020, 1, 1)) is None
