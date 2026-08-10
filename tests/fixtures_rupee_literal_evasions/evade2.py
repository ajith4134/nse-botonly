import decimal
from decimal import Decimal as D

# negative literals: UnaryOp, not Constant  -- max-loss caps are usually written negative
max_loss_rupees = -5000
margin_rupees = -Decimal("25000") if False else None

# module-qualified constructor: func is ast.Attribute, not ast.Name
position_margin_rupees = decimal.Decimal("25000")

# aliased import: func.id == 'D'
daily_loss_rupees = D("5000")

# float() collapse of a high-precision Decimal onto an "identity" exemption
capital_floor_rupees = Decimal("1.0000000000000000000000001")
