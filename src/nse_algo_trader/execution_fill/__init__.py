"""`L1.05` + `L1.06` — what a fill actually costs beyond the statutory charges.

`L1.01` prices what the exchange, the government and the broker take. That is the part written
down in circulars. This is the part written in the order book: the spread you cross, and the
distance your own size pushes the price.

Which of the two dominates depends on size, and on real NSE data it is frequently this one — so
a breakeven from `L1.01` alone is a FLOOR, and only becomes a hurdle when combined with what
this package produces.

The design is shaped by what a five-level snapshot tape can honestly support, which is less
than the literature assumes. `docs/research/220` records what was ruled out and the
measurement that ruled it out; the short version is that anything needing trade prints or a
signed-volume series is unavailable here, and that walking the visible book answers a smaller
question than it appears to.
"""
