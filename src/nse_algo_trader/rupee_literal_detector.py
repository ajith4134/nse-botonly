"""Detects rupee amounts written as literals — the executable half of R.03.

Moved out of the test file after adversarial review (2026-08-10) found the
original detector caught essentially one syntactic shape and missed thirty-odd
working evasions. A guard that cannot fail provides confidence proportional to
nothing, which is worse than no guard, because it is *cited* as enforcement.

**Why this is not "just a lint rule":** the invariant is semantic, not syntactic.
A ₹5,000 max-loss cap is 5% of a ₹1 lakh account and 0.05% of a ₹1 crore one
(A.23), so the same literal encodes two incompatible risk policies. No
general-purpose linter expresses that — ruff's ``PLR2004`` covers magic values in
*comparisons* only and is not currency-aware (verified, ``docs/research/200``).

Design: walk the AST and pair every **money-named binding site** with every
**numeric literal**, across the node shapes an author actually writes, rather
than the single shape the first version happened to test.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Substrings that mark a name as holding a rupee amount. Deliberately broad:
# a false positive costs one allowlist entry and a justification; a false
# negative is a hardcoded risk limit shipping to production.
MONEY_NAME_MARKERS: tuple[str, ...] = (
    "rupee",
    "inr",
    "paise",
    "capital",
    "margin",
    "premium",
    "notional",
    "loss",
    "profit",
    "balance",
    "brokerage",
    "fee",
    "cost",
    "amount",
    "price",
    "cash",
    "collateral",
    "payout",
    "credit",
    "debit",
)

# Suffixes that mark a name as POLICY rather than money. A fraction, ratio or
# percentage is dimensionless and scales across every account size — it is exactly
# what R.03 asks for, so it must never be flagged. Checked before the money
# markers, since "maximum_daily_loss_fraction" contains "loss".
POLICY_NAME_MARKERS: tuple[str, ...] = (
    "fraction",
    "ratio",
    # A quantile is dimensionless BY DEFINITION — it names a position in a distribution, and the
    # distribution supplies the unit. `PRICE_COLLAR_QUANTILE` tripped this detector on the word
    # "price" while holding 0.95, which is not an amount of anything.
    "quantile",
    "percentile",
    "percent",
    "_pct",
    "multiple",
    "multiplier",
    "count",
    "quantity",
    "lots",
    "bps",
    "basis_points",
)

# Names permitted to hold a money literal, each exempt for a stated reason.
# Adding one is a deliberate act; the reason is required, not decorative.
SOURCED_CONSTANT_REASONS: dict[str, str] = {
    "MINIMUM_SUPPORTED_CAPITAL_RUPEES": "operator-declared supported envelope (A.23)",
    "MAXIMUM_SUPPORTED_CAPITAL_RUPEES": "operator-declared supported envelope (A.23)",
    "_PAISE": "1 rupee = 100 paise — a property of INR itself, not a policy choice",
    "PAISE_PER_RUPEE": "the same INR property, named for the conversion it performs",
    "RUPEES_PER_LAKH": (
        "1 lakh = 100,000 — a unit of the Indian numbering system, and the conversion "
        "legacy F&O bhavcopy requires because it reports traded value in lakhs (`L0.34`)"
    ),
}

# Values carrying no policy: identity elements and the empty allocation.
_POLICY_FREE_VALUES = frozenset({0, 1, -1})

_DECIMAL_CONSTRUCTORS = frozenset({"Decimal", "D"})


@dataclass(frozen=True, slots=True)
class RupeeLiteral:
    """A rupee amount found written as a constant."""

    line: int
    name: str
    rendered_value: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.name} = {self.rendered_value}"


def _is_money_name(name: str) -> bool:
    """True when a name holds a rupee amount rather than a dimensionless policy."""
    lowered = name.lower()
    if any(marker in lowered for marker in POLICY_NAME_MARKERS):
        return False
    return any(marker in lowered for marker in MONEY_NAME_MARKERS)


def _numeric_literal(node: ast.expr | None) -> str | None:
    """Render ``node`` if it is a numeric literal, however it is spelled.

    Covers the shapes the first detector missed: negation (max-loss caps are
    conventionally negative), constant folding, module-qualified and aliased
    ``Decimal`` constructors, and string arguments to them.
    """
    match node:
        case ast.Constant(value=bool()):
            return None
        case ast.Constant(value=int() | float() as value):
            return repr(value)
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            inner = _numeric_literal(operand)
            return None if inner is None else f"-{inner}"
        case ast.BinOp(left=left, right=right):
            # e.g. 5 * 1000 — folded so arithmetic cannot launder a literal.
            if _numeric_literal(left) is not None and _numeric_literal(right) is not None:
                return ast.unparse(node)
            return None
        case ast.Call(func=func, args=[ast.Constant(value=argument), *_]):
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if called in _DECIMAL_CONSTRUCTORS and isinstance(argument, (str, int, float)):
                return f"{called}({argument!r})"
            return None
        case _:
            return None


def _bound_name(target: ast.expr) -> str | None:
    """The name a value is being bound to, across target shapes.

    ``x``, ``self.x``, ``config["x"]`` — the first detector saw only the first.
    """
    match target:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attribute):
            return attribute
        case ast.Subscript(slice=ast.Constant(value=str() as key)):
            return key
        case _:
            return None


def find_rupee_literals(source: str, *, filename: str = "<source>") -> list[RupeeLiteral]:
    """Return every rupee amount written as a literal in ``source``."""
    tree = ast.parse(source, filename=filename)
    found: list[RupeeLiteral] = []

    def record(name: str | None, value_node: ast.expr | None, line: int) -> None:
        if name is None or not _is_money_name(name):
            return
        if name in SOURCED_CONSTANT_REASONS:
            return
        rendered = _numeric_literal(value_node)
        if rendered is None:
            return
        try:
            if float(rendered.strip("-").split("(")[-1].strip("')\"")) in _POLICY_FREE_VALUES:
                return
        except ValueError:
            pass
        found.append(RupeeLiteral(line=line, name=name, rendered_value=rendered))

    for node in ast.walk(tree):
        match node:
            case ast.Assign(targets=targets, value=value):
                for target in targets:
                    if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
                        for element, element_value in zip(target.elts, value.elts, strict=False):
                            record(_bound_name(element), element_value, node.lineno)
                    else:
                        record(_bound_name(target), value, node.lineno)
            case ast.AnnAssign(target=target, value=value):
                record(_bound_name(target), value, node.lineno)
            case ast.AugAssign(target=target, value=value):
                record(_bound_name(target), value, node.lineno)
            case ast.Dict(keys=keys, values=values):
                for key, value in zip(keys, values, strict=False):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        record(key.value, value, node.lineno)
            case ast.Compare(left=left, comparators=comparators):
                name = _bound_name(left)
                for comparator in comparators:
                    record(name, comparator, node.lineno)
            case ast.FunctionDef(args=arguments) | ast.AsyncFunctionDef(args=arguments):
                positional = arguments.posonlyargs + arguments.args
                padded: list[ast.expr | None] = [None] * (
                    len(positional) - len(arguments.defaults)
                ) + list(arguments.defaults)
                for argument, default in zip(positional, padded, strict=False):
                    record(argument.arg, default, node.lineno)
                for argument, default in zip(
                    arguments.kwonlyargs, arguments.kw_defaults, strict=False
                ):
                    record(argument.arg, default, node.lineno)
            case ast.Return(value=value):
                record(_enclosing_money_name(tree, node), value, node.lineno)
            case _:
                pass
    return found


def _enclosing_money_name(tree: ast.Module, target: ast.Return) -> str | None:
    """The name of the function a ``return`` belongs to, if money-named."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            child is target for child in ast.walk(node)
        ):
            return node.name
    return None


def scan_source_tree(root: Path) -> dict[Path, list[RupeeLiteral]]:
    """Scan every ``.py`` file under ``root``. Returns only files with findings."""
    return {
        path: literals
        for path in sorted(root.rglob("*.py"))
        if (literals := find_rupee_literals(path.read_text(encoding="utf-8"), filename=str(path)))
    }
