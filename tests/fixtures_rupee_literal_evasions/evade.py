from decimal import Decimal
from dataclasses import dataclass, field

# --- 1. name lacks a marker word (marker list is 6 words) ---
MAX_DAILY_LOSS = 50000
stop_loss_amount = 5000
minimum_order_value = 100
brokerage_per_order = 20
account_balance = 250000
notional_cap = 500000
funds_available = 100000
cash_floor = 25000
equity_at_risk = 10000
option_premium = 150            # 'premium_amount' is the marker, bare 'premium' is not
inr_limit = 5000                # marker is '_inr', not 'inr'
INR_HARD_STOP = 7500
rs_max_loss = 5000
rupiah = 1                      # (control)

# --- 2. instance/class attribute targets are ast.Attribute, not ast.Name ---
class RiskLimits:
    def __init__(self):
        self.max_loss_rupees = 5000
        self.margin_rupees = Decimal("25000")

# --- 3. function default arguments are not Assign nodes ---
def size_position(max_loss_rupees: Decimal = Decimal("5000")) -> Decimal:
    return max_loss_rupees

# --- 4. return / call-argument literals ---
def hard_capital_floor() -> Decimal:
    return Decimal("100000")

# --- 5. container values ---
LIMITS = {"max_loss_rupees": 5000, "margin_rupees": Decimal("25000")}
BANDS = [5000, 25000, 100000]

# --- 6. dataclass field(default=...) ---
@dataclass
class Book:
    margin_rupees: Decimal = field(default=Decimal("25000"))

# --- 7. arithmetic / non-Constant expressions ---
max_loss_rupees = 5 * 1000
daily_capital_cap = Decimal("5") * 1000
margin_buffer_rupees = int("5000")
premium_amount_cap = Decimal(5000).scaleb(0)

# --- 8. tuple / starred / augmented assignment ---
first_margin_rupees, second_margin_rupees = 5000, 25000
running_margin_rupees = Decimal(0)
running_margin_rupees += 5000

# --- 9. walrus ---
if (walrus_margin_rupees := 5000) > 0:
    pass

# --- 10. name-based exemption is global, not module-scoped ---
_PAISE = Decimal("999999")
MINIMUM_SUPPORTED_CAPITAL_RUPEES = Decimal("42")

# --- 11. subscript target ---
CONFIG = {}
CONFIG["max_loss_rupees"] = 5000

# --- 12. comparison against a bare number (spec CLAIMS this is detected) ---
def breached(pnl_rupees: Decimal) -> bool:
    return pnl_rupees < Decimal("-5000")
