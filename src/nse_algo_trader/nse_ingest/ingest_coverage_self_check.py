"""What a source actually has, per year — reported by the store rather than assumed.

The trading calendar (`1.30`) already established the shape this follows: an engine that
reports its own reliability is usable, and one that quietly answers from thin data is
not. There the finding was that `pandas_market_calendars` recognises **zero** NSE
holidays for 1990-1996; the calendar surfaces that per year instead of pretending.

The same question is asked here. A source with rows for 40% of a year's sessions is not
"working" — it is a source whose gaps a consumer must know about before drawing a
conclusion from it. Crucially, a gap is only meaningful against the sessions that
actually existed, so coverage is computed against the calendar rather than against
calendar days: a source is not missing Republic Day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nse_algo_trader.nse_ingest.bitemporal_ingest_store import BitemporalIngestStore
from nse_algo_trader.nse_trading_session_calendar import NseTradingSessionCalendar


@dataclass(frozen=True)
class SourceYearCoverage:
    """One source's coverage of one year, with the evidence behind the number."""

    source_name: str
    year: int
    trading_sessions_in_year: int
    effective_dates_present: int
    missing_dates: tuple[date, ...]

    @property
    def coverage_fraction(self) -> float:
        if not self.trading_sessions_in_year:
            return 0.0
        return self.effective_dates_present / self.trading_sessions_in_year

    def describe(self) -> str:
        return (
            f"{self.source_name} {self.year}: {self.effective_dates_present}"
            f"/{self.trading_sessions_in_year} sessions "
            f"({self.coverage_fraction:.0%}), {len(self.missing_dates)} missing"
        )


@dataclass(frozen=True)
class SourceCoverageReport:
    """A source's coverage across every year it claims, plus its fetch record."""

    source_name: str
    years: tuple[SourceYearCoverage, ...]
    total_rows: int
    successful_fetches: int
    failed_fetches: int

    @property
    def has_any_data(self) -> bool:
        return self.total_rows > 0

    def years_below(self, coverage_fraction: float) -> tuple[SourceYearCoverage, ...]:
        """Years a consumer should not trust without knowing why."""
        return tuple(
            year for year in self.years if year.coverage_fraction < coverage_fraction
        )

    def worst_year(self) -> SourceYearCoverage | None:
        return min(self.years, key=lambda year: year.coverage_fraction, default=None)


def build_source_coverage_report(
    store: BitemporalIngestStore,
    source_name: str,
    calendar: NseTradingSessionCalendar | None = None,
    maximum_missing_dates_listed: int = 25,
) -> SourceCoverageReport:
    """Measure what `source_name` holds, per year, against real trading sessions."""
    trading_calendar = calendar or NseTradingSessionCalendar()
    present_dates = store.effective_dates_present(source_name)
    fetches = store.fetch_history(source_name)
    successful = sum(1 for record in fetches if record["fetch_status"] == "retrieved")

    years: list[SourceYearCoverage] = []
    dates_by_year: dict[int, set[date]] = {}
    for effective_date in present_dates:
        dates_by_year.setdefault(effective_date.year, set()).add(effective_date)

    for year in sorted(dates_by_year):
        sessions = set(
            trading_calendar.sessions_between(date(year, 1, 1), date(year, 12, 31))
        )
        present = dates_by_year[year]
        missing = sorted(sessions - present)
        years.append(
            SourceYearCoverage(
                source_name=source_name,
                year=year,
                trading_sessions_in_year=len(sessions),
                effective_dates_present=len(present & sessions),
                missing_dates=tuple(missing[:maximum_missing_dates_listed]),
            )
        )

    return SourceCoverageReport(
        source_name=source_name,
        years=tuple(years),
        total_rows=store.row_count(source_name),
        successful_fetches=successful,
        failed_fetches=len(fetches) - successful,
    )
