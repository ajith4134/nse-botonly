"""`L1.01` — what a trade actually costs, priced at the date it happened.

The layer below this one (`L0`) established what the market did. This one establishes what
taking part in it costs, which is the filter every signal above has to clear: SEBI's own study
found loss-making intraday traders spend 57% of their losses on transaction costs.

Nothing here carries a rate as a literal. Rates are resolved out of the `L0.31` point-in-time
rule store at the trade's own date, and a date whose rate is not known is REFUSED rather than
priced with today's.
"""
