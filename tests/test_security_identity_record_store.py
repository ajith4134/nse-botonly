"""`L0.08` — identity resolution, and the refusals that make it trustworthy.

The store's value is not that it answers; it is that it declines to answer when answering
would mean choosing which company's price history to return.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from nse_algo_trader.security_identity_record_store import (
    AmbiguousSymbolError,
    IdentityEventKind,
    SecurityIdentityError,
    SecurityIdentityObservation,
    SecurityIdentityRecordStore,
    UnknownIdentityError,
    derive_identity_events,
    observations_from_bhavcopy_rows,
)

ADANI_GAS_ISIN = "INE399L01023"
OTHER_ISIN = "INE081A01012"


def _observation(isin: str, symbol: str, day: int) -> SecurityIdentityObservation:
    return SecurityIdentityObservation(isin, symbol, date(2026, 1, day))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SecurityIdentityRecordStore]:
    with SecurityIdentityRecordStore(tmp_path / "identity.sqlite3") as opened:
        yield opened


@pytest.mark.unit
def test_a_rename_is_the_same_isin_under_a_new_symbol(
    store: SecurityIdentityRecordStore,
) -> None:
    store.record(
        [
            _observation(ADANI_GAS_ISIN, "ADANIGAS", 1),
            _observation(ADANI_GAS_ISIN, "ATGL", 5),
        ]
    )
    (event,) = store.identity_events()
    assert event.kind is IdentityEventKind.SYMBOL_RENAMED
    assert (event.previous_value, event.new_value) == ("ADANIGAS", "ATGL")
    # Both dates, because the rename happened SOMEWHERE between them. Observation is
    # sparse — the real corpus has 13 dates across six years — so claiming the later date
    # as the effective date would be inventing precision.
    assert event.last_seen_before == date(2026, 1, 1)
    assert event.first_seen_after == date(2026, 1, 5)


@pytest.mark.unit
def test_resolution_is_point_in_time_on_both_sides_of_a_rename(
    store: SecurityIdentityRecordStore,
) -> None:
    """The whole point: one stable key, the right symbol for the date asked about."""
    store.record(
        [
            _observation(ADANI_GAS_ISIN, "ADANIGAS", 1),
            _observation(ADANI_GAS_ISIN, "ATGL", 5),
        ]
    )
    assert store.resolve_symbol_for_isin(ADANI_GAS_ISIN, date(2026, 1, 3)) == "ADANIGAS"
    assert store.resolve_symbol_for_isin(ADANI_GAS_ISIN, date(2026, 1, 5)) == "ATGL"
    assert store.resolve_isin_for_symbol("ADANIGAS", date(2026, 1, 3)) == ADANI_GAS_ISIN


@pytest.mark.unit
def test_resolution_never_reads_a_later_observation(
    store: SecurityIdentityRecordStore,
) -> None:
    """Using a future observation to answer a past question is lookahead."""
    store.record([_observation(ADANI_GAS_ISIN, "ATGL", 10)])
    with pytest.raises(UnknownIdentityError):
        store.resolve_symbol_for_isin(ADANI_GAS_ISIN, date(2026, 1, 9))


@pytest.mark.unit
def test_an_ambiguous_symbol_is_refused_rather_than_guessed(
    store: SecurityIdentityRecordStore,
) -> None:
    """`SRTRANSFIN` really does carry 28 ISINs on one date in the real corpus.

    Picking one would silently return a bond's series where the caller meant the equity.
    """
    store.record(
        [
            _observation(ADANI_GAS_ISIN, "SHARED", 1),
            _observation(OTHER_ISIN, "SHARED", 1),
        ]
    )
    with pytest.raises(AmbiguousSymbolError) as raised:
        store.resolve_isin_for_symbol("SHARED", date(2026, 1, 1))
    assert "2 securities" in str(raised.value)


@pytest.mark.unit
def test_a_symbol_returning_to_a_previous_isin_is_still_two_events() -> None:
    """A -> B -> A must not collapse to nothing.

    Comparing only first-to-last would report no change at all, and a symbol that came
    back to an earlier ISIN is exactly the case that corrupts a series keyed on it.
    """
    events = derive_identity_events(
        [
            _observation(ADANI_GAS_ISIN, "SHARED", 1),
            _observation(OTHER_ISIN, "SHARED", 5),
            _observation(ADANI_GAS_ISIN, "SHARED", 9),
        ]
    )
    reassignments = [e for e in events if e.kind is IdentityEventKind.SYMBOL_REASSIGNED]
    assert len(reassignments) == 2


@pytest.mark.unit
def test_recording_the_same_observation_twice_changes_nothing(
    store: SecurityIdentityRecordStore,
) -> None:
    """Append-only: a re-ingested file must never rewrite what was believed before."""
    observations = [_observation(ADANI_GAS_ISIN, "ADANIGAS", 1)]
    assert store.record(observations) == 1
    assert store.record(observations) == 0


@pytest.mark.unit
def test_a_malformed_isin_is_rejected_at_construction() -> None:
    with pytest.raises(SecurityIdentityError):
        SecurityIdentityObservation("TOOSHORT", "ADANIGAS", date(2026, 1, 1))
    with pytest.raises(SecurityIdentityError):
        SecurityIdentityObservation(ADANI_GAS_ISIN, "", date(2026, 1, 1))


@pytest.mark.unit
def test_bhavcopy_parsing_survives_both_schemas_and_skips_junk() -> None:
    """The corpus spans two layouts — `SYMBOL` in the old one, `TckrSymb` in UDiFF.

    One unparseable row must not cost the other forty thousand.
    """
    parsed = observations_from_bhavcopy_rows(
        [
            ("2026-01-01", {"ISIN": ADANI_GAS_ISIN, "SYMBOL": "ADANIGAS"}),
            ("2026-01-05", {"ISIN": OTHER_ISIN, "TckrSymb": "TATASTEEL"}),
            ("2026-01-05", {"ISIN": "SHORT", "SYMBOL": "BAD"}),
            ("2026-01-05", {"SYMBOL": "NOISIN"}),
            ("2026-01-05", {"ISIN": ADANI_GAS_ISIN}),
        ]
    )
    assert [o.symbol for o in parsed] == ["ADANIGAS", "TATASTEEL"]


@pytest.mark.unit
def test_ambiguous_symbols_are_reported_worst_first(
    store: SecurityIdentityRecordStore,
) -> None:
    third = "INE010B01027"
    store.record(
        [
            _observation(ADANI_GAS_ISIN, "MANY", 1),
            _observation(OTHER_ISIN, "MANY", 2),
            _observation(third, "MANY", 3),
            _observation(ADANI_GAS_ISIN, "TWO", 1),
            _observation(OTHER_ISIN, "TWO", 2),
        ]
    )
    ambiguities = store.ambiguous_symbols()
    assert [a.symbol for a in ambiguities] == ["MANY", "TWO"]
    assert all(a.is_ambiguous for a in ambiguities)
