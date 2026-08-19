"""The six autonomous segment bots and the protocol they share — `L5.29`, `A.130`.

`L5.25` defines a segment holon as an autonomous bot owning its strategies, relevance models, risk
sub-limits, memory and track record. This package holds the contract all six implement and the
conformance suite that proves an implementation actually honours it; the bots themselves are
`L5.26`-`L5.28` and their three siblings.
"""

from nse_algo_trader.segment_bots.segment_bot_conformance import (
    ConformanceViolation,
    run_segment_bot_conformance,
)
from nse_algo_trader.segment_bots.segment_bot_protocol import (
    BotGraduationRefusedError,
    BotMaturity,
    BotMaturityRung,
    SegmentBot,
    SegmentBotContext,
    SegmentBotProtocolError,
    SegmentRelevance,
    TradeableInstrument,
)
from nse_algo_trader.segment_bots.segment_trading_taxonomy import (
    SEGMENT_INSTRUMENT_FACTS,
    SegmentInstrumentFacts,
    SegmentTaxonomyError,
    SettlementStyle,
    TradeableUnitDenominator,
    TradingSegment,
    VenueCalendar,
    instrument_facts_for,
)

__all__ = [
    "SEGMENT_INSTRUMENT_FACTS",
    "BotGraduationRefusedError",
    "BotMaturity",
    "BotMaturityRung",
    "ConformanceViolation",
    "SegmentBot",
    "SegmentBotContext",
    "SegmentBotProtocolError",
    "SegmentInstrumentFacts",
    "SegmentRelevance",
    "SegmentTaxonomyError",
    "SettlementStyle",
    "TradeableInstrument",
    "TradeableUnitDenominator",
    "TradingSegment",
    "VenueCalendar",
    "instrument_facts_for",
    "run_segment_bot_conformance",
]
