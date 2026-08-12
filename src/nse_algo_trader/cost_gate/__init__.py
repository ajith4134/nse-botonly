"""`L1.02` + `L1.03` — the gate every signal must clear before it can become an order.

The layer below prices things; this one refuses things. `L1.01` says what the exchange and the
broker take, `L1.05`/`L1.06` say what the book takes, and neither changes a decision on its own.
The gate is where a cost stops being a report and starts being a veto.

Its safety margin is derived rather than chosen: a signal must clear the PESSIMISTIC end of the
measured cost interval, so uncertainty raises the bar in proportion to how uncertain the estimate
actually is — no multiplier, and no single constant that is necessarily wrong for both the liquid
and the illiquid case at once.
"""
