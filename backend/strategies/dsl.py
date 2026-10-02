"""No-code strategy DSL.

A strategy is a JSON document. The visual builder produces exactly this shape,
and the same evaluator runs it in backtest, paper and live modes — so what is
tested is what trades.

```json
{
  "name": "EMA crossover with volume filter",
  "timeframe": "15m",
  "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY"}],
  "entry": {
    "all": [
      {"indicator": "ema", "params": {"period": 20}, "field": "value", "compare": "crosses_above",
       "against": {"indicator": "ema", "params": {"period": 50}, "field": "value"}},
      {"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 70}
    ]
  },
  "exit": { "all": [ {"indicator": "rsi", "params": {"period": 14}, "compare": "gt", "value": 75} ] },
  "risk": {"stop_loss_pct": 1.5, "target_pct": 3.0, "trailing_stop_pct": 1.0,
            "quantity": 1, "timeframe_exit_bars": 20},
  "position_sizing": {"mode": "fixed", "quantity": 1}
}
```

Every node is validated before it runs, so a malformed rule is reported to the
user rather than silently never firing.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from backend.strategies.indicators import CANDLE_INDICATORS, INDICATORS

#: Raw price fields usable as if they were indicators, so a rule can read
#: "close above the upper band" without a special-case in the evaluator.
PRICE_FIELDS = ("open", "high", "low", "close", "volume")

COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: math.isclose(a, b, rel_tol=1e-9),
    "neq": lambda a, b: not math.isclose(a, b, rel_tol=1e-9),
}

CROSS_OPS = {"crosses_above", "crosses_below"}

RULE_TYPES = {"all", "any", "not", "condition"}


class StrategyError(ValueError):
    """The strategy document is invalid."""


# ------------------------------------------------------------------ validation


def validate(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate a strategy document, returning a list of human-readable errors."""
    errors: list[str] = []
    if not isinstance(definition, dict):
        return {"valid": False, "errors": ["Strategy must be a JSON object"]}

    if not str(definition.get("name") or "").strip():
        errors.append("Strategy needs a name")
    if not str(definition.get("timeframe") or "").strip():
        errors.append("Strategy needs a timeframe")
    if not definition.get("universe"):
        errors.append("Strategy needs at least one instrument to trade")

    for section in ("entry", "exit"):
        node = definition.get(section)
        if node is None:
            errors.append(f"Missing '{section}' conditions")
        else:
            errors.extend(_validate_node(node, section))

    risk = definition.get("risk") or {}
    if risk:
        stop = risk.get("stop_loss_pct")
        if stop is not None and (not isinstance(stop, (int, float)) or stop <= 0 or stop > 100):
            errors.append("risk.stop_loss_pct must be between 0 and 100")
        target = risk.get("target_pct")
        if target is not None and (not isinstance(target, (int, float)) or target <= 0):
            errors.append("risk.target_pct must be positive")

    return {"valid": not errors, "errors": errors}


def _validate_node(node: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(node, dict):
        return [f"{path}: expected an object"]

    if "all" in node:
        children = node["all"]
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.all: needs a non-empty list")
        else:
            for index, child in enumerate(children):
                errors.extend(_validate_node(child, f"{path}.all[{index}]"))
        return errors

    if "any" in node:
        children = node["any"]
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.any: needs a non-empty list")
        else:
            for index, child in enumerate(children):
                errors.extend(_validate_node(child, f"{path}.any[{index}]"))
        return errors

    if "not" in node:
        return _validate_node(node["not"], f"{path}.not")

    indicator = node.get("indicator")
    if not indicator:
        errors.append(f"{path}: missing 'indicator'")
        return errors
    if indicator not in INDICATORS and indicator not in PRICE_FIELDS:
        errors.append(
            f"{path}: unknown indicator '{indicator}'. "
            f"Available: {', '.join(sorted(set(INDICATORS) | set(PRICE_FIELDS)))}"
        )
        return errors

    compare = node.get("compare")
    if compare not in COMPARATORS and compare not in CROSS_OPS:
        errors.append(
            f"{path}: unknown compare '{compare}'. "
            f"Use one of {sorted(COMPARATORS)} or {sorted(CROSS_OPS)}"
        )
        return errors

    params = node.get("params") or {}
    if not isinstance(params, dict):
        errors.append(f"{path}.params: expected an object")
    elif indicator not in CANDLE_INDICATORS and indicator not in PRICE_FIELDS:
        for key in ("period", "fast", "slow", "signal", "length"):
            value = params.get(key)
            if value is not None and (not isinstance(value, int) or value < 1):
                errors.append(f"{path}.params.{key}: must be a positive integer")

    if compare in CROSS_OPS:
        if not node.get("against"):
            errors.append(f"{path}: '{compare}' needs an 'against' reference")
    elif node.get("value") is None and not node.get("against"):
        # A comparison needs either a literal threshold or another series.
        errors.append(f"{path}: comparison needs a 'value' or an 'against' reference")

    return errors


# ------------------------------------------------------------------ evaluation


class IndicatorCache:
    """Computes each indicator once per evaluation, at the bar being judged.

    A strategy referencing RSI in both entry and exit must not recompute it for
    every node, and every value must come from the same bar so conditions cannot
    disagree about "now".
    """

    def __init__(self, candles: list[dict[str, Any]], *, index: int | None = None) -> None:
        self.candles = candles
        self.index = len(candles) - 1 if index is None else index
        self.closes = [candle.get("close") for candle in candles]
        self._cache: dict[str, Any] = {}

    def _compute(self, name: str, params: dict[str, Any]) -> Any:
        if name in PRICE_FIELDS:
            return [candle.get(name) for candle in self.candles]
        key = f"{name}:{sorted(params.items())}"
        if key in self._cache:
            return self._cache[key]
        function = INDICATORS[name]
        if name in CANDLE_INDICATORS:
            result = function(self.candles)
        else:
            result = function(self.closes, **params)
        self._cache[key] = result
        return result

    def value(self, name: str, params: dict[str, Any], field: str, index: int) -> float | None:
        result = self._compute(name, params)
        if index < 0 or index >= len(result):
            return None
        if isinstance(result, dict):
            series = result.get(field)
            if series is None or index >= len(series):
                return None
            value = series[index]
        else:
            value = result[index]
        return float(value) if value is not None else None

    def values(self, name: str, params: dict[str, Any], field: str) -> list[float | None]:
        result = self._compute(name, params)
        if isinstance(result, dict):
            return list(result.get(field) or [])
        return list(result)


def _reference_value(reference: dict[str, Any], cache: IndicatorCache, index: int) -> float | None:
    """Resolve a literal number or another indicator to a value at `index`."""
    if "value" in reference and reference["value"] is not None:
        return float(reference["value"])
    if reference.get("indicator"):
        return cache.value(
            reference["indicator"], reference.get("params") or {},
            reference.get("field", "value"), index,
        )
    return None


def evaluate(node: Any, cache: IndicatorCache, index: int) -> bool:
    """True when the rule tree holds at bar `index`."""
    if not isinstance(node, dict):
        return False

    if "all" in node:
        return all(evaluate(child, cache, index) for child in node["all"])
    if "any" in node:
        return any(evaluate(child, cache, index) for child in node["any"])
    if "not" in node:
        return not evaluate(node["not"], cache, index)

    indicator = node.get("indicator")
    if not indicator:
        return False
    params = node.get("params") or {}
    field = node.get("field", "value")
    compare = node.get("compare")

    current = cache.value(indicator, params, field, index)
    if current is None:
        return False

    if compare in CROSS_OPS:
        previous = cache.value(indicator, params, field, index - 1)
        other = _reference_value(node.get("against") or {}, cache, index)
        if previous is None or other is None:
            return False
        if compare == "crosses_above":
            return previous <= other and current > other
        return previous >= other and current < other

    target = _reference_value(node, cache, index)
    if target is None:
        return False
    return COMPARATORS[compare](current, target)


def explain(node: Any, cache: IndicatorCache, index: int) -> dict[str, Any]:
    """Evaluate and describe the tree, for the strategy tester UI."""
    if isinstance(node, dict) and "all" in node:
        children = [explain(child, cache, index) for child in node["all"]]
        return {"type": "all", "result": all(c["result"] for c in children), "children": children}
    if isinstance(node, dict) and "any" in node:
        children = [explain(child, cache, index) for child in node["any"]]
        return {"type": "any", "result": any(c["result"] for c in children), "children": children}
    if isinstance(node, dict) and "not" in node:
        child = explain(node["not"], cache, index)
        return {"type": "not", "result": not child["result"], "children": [child]}
    if not isinstance(node, dict) or not node.get("indicator"):
        return {"type": "invalid", "result": False}

    params = node.get("params") or {}
    field = node.get("field", "value")
    current = cache.value(node["indicator"], params, field, index)
    target = _reference_value(
        node.get("against") if node.get("compare") in CROSS_OPS else node, cache, index
    )
    result = evaluate(node, cache, index)
    return {
        "type": "condition",
        "indicator": node["indicator"],
        "params": params,
        "field": field,
        "compare": node.get("compare"),
        "value": current,
        "target": target,
        "result": result,
        "description": f"{node['indicator']}({field}) {node.get('compare')} {target}",
    }


# ------------------------------------------------------------------ risk sizing


def position_size(definition: dict[str, Any], equity: float, price: float) -> int:
    """Quantity to trade, from the strategy's sizing block.

    `fixed` trades a constant quantity; `pct_risk` sizes from the stop distance
    so each trade risks a constant fraction of equity; `fixed_capital` uses a
    rupee budget.
    """
    sizing = definition.get("position_sizing") or {"mode": "fixed", "quantity": 1}
    mode = sizing.get("mode", "fixed")

    if price <= 0:
        return 0
    if mode == "pct_risk":
        risk_pct = float(sizing.get("risk_pct") or 1.0) / 100
        stop_pct = float((definition.get("risk") or {}).get("stop_loss_pct") or 0)
        if risk_pct <= 0 or stop_pct <= 0:
            return int(sizing.get("quantity") or 1)
        risk_budget = equity * risk_pct
        stop_distance = price * (stop_pct / 100)
        if stop_distance <= 0:
            return 0
        return max(0, int(risk_budget // stop_distance))
    if mode == "fixed_capital":
        budget = float(sizing.get("capital") or 0)
        return max(0, int(budget // price))
    return max(0, int(sizing.get("quantity") or 1))


def stop_and_target(entry_price: float, risk: dict[str, Any], side: str) -> tuple[float | None, float | None]:
    """Stop-loss and target prices for an entry, respecting the trade side."""
    stop_pct = float(risk.get("stop_loss_pct") or 0)
    target_pct = float(risk.get("target_pct") or 0)
    is_long = str(side).upper() in {"B", "BUY", "LONG"}

    if is_long:
        stop = entry_price * (1 - stop_pct / 100) if stop_pct else None
        target = entry_price * (1 + target_pct / 100) if target_pct else None
    else:
        stop = entry_price * (1 + stop_pct / 100) if stop_pct else None
        target = entry_price * (1 - target_pct / 100) if target_pct else None
    return (round(stop, 2) if stop else None, round(target, 2) if target else None)
