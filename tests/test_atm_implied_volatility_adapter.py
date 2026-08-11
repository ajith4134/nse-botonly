"""`L0.27` ATM implied volatility, certified on real captured `option-chain-v3` payloads.

`research/207` marked this source BLOCKED (404 on the now-retired `option-chain-indices`
endpoint). Every payload here is real bytes fetched live from `www.nseindia.com` on 2026-08-11
against the successor endpoint, `option-chain-v3` — including a real "wrong expiry guessed"
response, which is the shape a stale calendar guess produces and must never be mistaken for a
quiet day.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from nse_algo_trader.nse_ingest.atm_implied_volatility_adapter import (
    ATM_IMPLIED_VOLATILITY_ROLLING_SOURCE,
    EQUITY_UNDERLYINGS,
    INDEX_UNDERLYINGS,
    OPTION_CHAIN_V3_URL,
    AtmImpliedVolatilityAdapter,
    candidate_expiry_dates,
    format_expiry_query_value,
)
from nse_algo_trader.nse_ingest.ingest_source_adapter import (
    IngestAdapterError,
    NseIngestSourceAdapter,
)
from nse_algo_trader.nse_ingest.nse_source_fetcher import FetchTarget
from tests.nse_ingest_conformance import NseIngestAdapterConformance

FIXTURES = Path(__file__).parent / "fixtures_nse_ingest"

NIFTY_SNAPSHOT_DATE = date(2026, 8, 11)
NIFTY_EXPIRY = date(2026, 8, 11)
NIFTY_WRONG_EXPIRY = date(2026, 8, 12)
RELIANCE_SNAPSHOT_DATE = date(2026, 8, 11)
RELIANCE_EXPIRY = date(2026, 8, 25)
EXPECTED_ROWS_PER_SNAPSHOT = 1


def _fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real payload {name} not captured on this machine")
    return path.read_bytes()


def _target(instrument_type: str, symbol: str, expiry: date, for_date: date) -> FetchTarget:
    query = urlencode(
        {"type": instrument_type, "symbol": symbol, "expiry": format_expiry_query_value(expiry)}
    )
    return FetchTarget(
        url=f"{OPTION_CHAIN_V3_URL}?{query}",
        source_name="atm_implied_volatility",
        expects=for_date.isoformat(),
    )


class TestAtmImpliedVolatilityAdapterCertifies(NseIngestAdapterConformance):
    """Certified on the real NIFTY snapshot — a small single-symbol universe keeps the
    conformance suite fast; `test_the_full_universe_is_embedded_and_not_a_sample` below
    proves the full 213-symbol universe separately."""

    def build_adapter(self) -> NseIngestSourceAdapter:
        return AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])

    def sample_payloads(self) -> Sequence[tuple[FetchTarget, bytes, int]]:
        return [
            (
                _target("Indices", "NIFTY", NIFTY_EXPIRY, NIFTY_SNAPSHOT_DATE),
                _fixture("option_chain_v3_nifty_20260811.json"),
                EXPECTED_ROWS_PER_SNAPSHOT,
            )
        ]


# ------------------------------------------------- beyond the shared contract


@pytest.mark.unit
def test_the_real_nifty_snapshot_carries_nses_own_implied_volatility() -> None:
    """The core finding this adapter is built on: NSE computes IV itself, we just read it."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    target = _target("Indices", "NIFTY", NIFTY_EXPIRY, NIFTY_SNAPSHOT_DATE)
    rows = adapter.parse(_fixture("option_chain_v3_nifty_20260811.json"), target)
    assert len(rows) == EXPECTED_ROWS_PER_SNAPSHOT
    row = rows[0]
    assert row.values["underlying"] == "NIFTY"
    assert row.values["atm_implied_volatility"] > 0
    assert row.values["call_implied_volatility"] is not None
    assert row.values["put_implied_volatility"] is not None
    assert row.natural_key == ("NIFTY", "11-Aug-2026")
    assert row.effective_date == NIFTY_SNAPSHOT_DATE


@pytest.mark.unit
def test_equity_underlyings_use_the_same_endpoint_with_type_equity() -> None:
    """Confirmed live: `type=Equity&symbol=RELIANCE&expiry=25-Aug-2026` -> HTTP 200, real IV."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("RELIANCE", "Equity")])
    target = _target("Equity", "RELIANCE", RELIANCE_EXPIRY, RELIANCE_SNAPSHOT_DATE)
    rows = adapter.parse(_fixture("option_chain_v3_reliance_20260811.json"), target)
    assert rows[0].values["underlying"] == "RELIANCE"
    assert rows[0].values["atm_implied_volatility"] > 0
    assert rows[0].natural_key == ("RELIANCE", "25-Aug-2026")


@pytest.mark.adversarial
def test_a_wrong_expiry_guess_is_a_real_http_200_with_empty_data() -> None:
    """The exact trap this adapter's candidate ladder produces on every wrong guess: NOT a
    404, NOT a block page — HTTP 200 carrying a validly-shaped, genuinely empty chain. Real
    bytes, fetched live for `expiry=12-Aug-2026`, a real-format date NSE was not quoting."""
    payload = _fixture("option_chain_v3_nifty_wrong_expiry_20260811.json")
    document = json.loads(payload)
    assert document["records"]["data"] == []
    assert document["records"]["expiryDates"], "still a real, well-formed payload"


@pytest.mark.adversarial
def test_a_wrong_expiry_guess_is_flagged_as_a_content_mismatch() -> None:
    """`content_mismatch_reason` must catch this BEFORE the runner ever calls `parse()` —
    that is the only thing preventing a stale calendar guess from being ingested as a
    genuinely quiet day (there is no other signal; the HTTP status is 200 either way)."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    payload = _fixture("option_chain_v3_nifty_wrong_expiry_20260811.json")
    target = _target("Indices", "NIFTY", NIFTY_WRONG_EXPIRY, NIFTY_SNAPSHOT_DATE)
    reason = adapter.content_mismatch_reason(payload, target)
    assert reason is not None
    assert "12-Aug-2026" in reason


@pytest.mark.adversarial
def test_parse_refuses_an_empty_chain_even_if_the_mismatch_guard_is_bypassed() -> None:
    """Defence in depth: even called directly (skipping `content_mismatch_reason`), `parse()`
    must never turn an empty chain into zero rows silently accepted as a holiday."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    payload = _fixture("option_chain_v3_nifty_wrong_expiry_20260811.json")
    target = _target("Indices", "NIFTY", NIFTY_WRONG_EXPIRY, NIFTY_SNAPSHOT_DATE)
    with pytest.raises(IngestAdapterError, match="zero option-chain entries"):
        adapter.parse(payload, target)


@pytest.mark.unit
def test_a_past_date_is_refused_because_this_is_a_live_only_feed() -> None:
    """Same honesty `FoBanListAdapter` applies: a rolling snapshot cannot answer for a day
    already gone, and the only proof is the snapshot's own embedded timestamp."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    payload = _fixture("option_chain_v3_nifty_20260811.json")
    stale_target = _target("Indices", "NIFTY", NIFTY_EXPIRY, date(2026, 1, 1))
    reason = adapter.content_mismatch_reason(payload, stale_target)
    assert reason is not None
    assert "live rolling feed" in reason


@pytest.mark.unit
def test_candidate_expiry_dates_cover_both_verified_real_cadences() -> None:
    """Weekly (index) and monthly (equity) — both verified real dates for 2026-08-11 must be
    reachable by the ladder without hardcoding either as a fixed weekday."""
    candidates = candidate_expiry_dates(date(2026, 8, 11))
    assert date(2026, 8, 11) in candidates, "the real weekly NIFTY expiry must be covered"
    assert date(2026, 8, 25) in candidates, "the real monthly equity expiry must be covered"
    assert all(day >= date(2026, 8, 11) for day in candidates), "no candidate before for_date"


@pytest.mark.unit
def test_candidate_ladder_rolls_to_next_month_near_month_end() -> None:
    """If the current month's trailing window is already behind `for_date`, the monthly
    candidates must roll forward rather than silently offering nothing."""
    near_month_end = date(2026, 8, 30)
    candidates = candidate_expiry_dates(near_month_end)
    assert any(day.month == 9 for day in candidates), "must roll into September"


@pytest.mark.unit
def test_format_expiry_query_value_matches_nses_verified_wire_format() -> None:
    assert format_expiry_query_value(date(2026, 8, 11)) == "11-Aug-2026"
    assert format_expiry_query_value(date(2026, 1, 1)) == "01-Jan-2026"


@pytest.mark.unit
def test_fetch_targets_collapse_to_the_single_most_recent_date() -> None:
    """A multi-date request must not multiply the ~2,700-target full-universe sweep per date —
    only the latest date could conceivably be 'now', so only it is worth guessing against."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    single = adapter.fetch_targets([date(2026, 8, 11)])
    multi = adapter.fetch_targets([date(2026, 8, 1), date(2026, 8, 11)])
    assert len(single) == len(multi)


@pytest.mark.unit
def test_fetch_targets_are_empty_before_the_coverage_floor() -> None:
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])
    assert adapter.fetch_targets([date(1999, 1, 1)]) == []


@pytest.mark.unit
def test_the_full_universe_is_embedded_and_not_a_sample() -> None:
    """Rule: full universe, never a sample. 5 index + 208 equity underlyings, each verified
    live against `underlying-information` on 2026-08-11 — not just NIFTY/RELIANCE."""
    assert set(INDEX_UNDERLYINGS) == {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    assert len(EQUITY_UNDERLYINGS) == 208
    assert len(set(EQUITY_UNDERLYINGS)) == 208, "no duplicate symbols in the embedded universe"
    assert "RELIANCE" in EQUITY_UNDERLYINGS
    adapter = AtmImpliedVolatilityAdapter()
    targets = adapter.fetch_targets([date(2026, 8, 11)])
    covered_symbols = {
        parse_qs(urlparse(t.url).query)["symbol"][0] for t in targets
    }
    assert covered_symbols == set(INDEX_UNDERLYINGS) | set(EQUITY_UNDERLYINGS)


@pytest.mark.unit
def test_two_different_expiries_for_the_same_underlying_do_not_collide() -> None:
    """The natural key must include the expiry, or a genuinely different expiry's ATM IV would
    look like a revision of a different expiry's ATM IV and one would be lost. Constructed
    payloads here (not a certification fixture) — both shaped exactly like the real captured
    NIFTY response, only the expiry/strike/IV numbers changed, purely to isolate this one
    behaviour."""
    adapter = AtmImpliedVolatilityAdapter(universe=[("NIFTY", "Indices")])

    def _payload(expiry_text: str) -> bytes:
        return json.dumps(
            {
                "records": {
                    "data": [
                        {
                            "expiryDates": expiry_text,
                            "strikePrice": 24450,
                            "CE": {"impliedVolatility": 11.0, "underlying": "NIFTY"},
                            "PE": {"impliedVolatility": 9.0, "underlying": "NIFTY"},
                        }
                    ],
                    "timestamp": "11-Aug-2026 12:00:00",
                    "underlyingValue": 24445.0,
                },
                "filtered": {},
            }
        ).encode("utf-8")

    near = adapter.parse(
        _payload("11-Aug-2026"), _target("Indices", "NIFTY", date(2026, 8, 11), NIFTY_SNAPSHOT_DATE)
    )
    far = adapter.parse(
        _payload("18-Aug-2026"), _target("Indices", "NIFTY", date(2026, 8, 18), NIFTY_SNAPSHOT_DATE)
    )
    assert near[0].natural_key != far[0].natural_key


@pytest.mark.unit
def test_rolling_source_marker_documents_why_there_is_no_history() -> None:
    assert ATM_IMPLIED_VOLATILITY_ROLLING_SOURCE.source_name == "atm_implied_volatility"
    assert "live" in ATM_IMPLIED_VOLATILITY_ROLLING_SOURCE.reason
