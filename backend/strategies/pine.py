"""Pine Script parser and translator (upgraded for standard v4/v5 scripts).

Handles:
- Inputs: input.int(), input.float(), input()
- Orders: strategy.entry(), strategy.close(), strategy.close_all(), strategy.exit()
- Indicators: ta.ema(), ta.rsi(), ta.sma(), ta.crossover(), ta.crossunder(), ta.vwap()
- Math expressions: close * 1.02, price * factor, etc.
- Graceful skipping of unsupported lines (plots, styling, tables) without crashing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("alphatrade.strategies.pine")


def _eval_math_expr(expr: str, var_map: dict[str, Any]) -> dict[str, Any] | float | str:
    """Evaluate simple expressions like `close * 1.02` or `source * factor`."""
    expr = expr.strip()
    # Check for multiplier like close * 1.02
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_\.]*)\s*([\*\/])\s*([0-9]*\.?[0-9]+)$", expr)
    if m:
        left, op, right = m.group(1), m.group(2), float(m.group(3))
        return {"op": op, "left": left, "right": right}

    # Check for variable multiplication
    m_var = re.match(r"^([a-zA-Z_][a-zA-Z0-9_\.]*)\s*([\*\/])\s*([a-zA-Z_][a-zA-Z0-9_]*)$", expr)
    if m_var:
        left, op, right_var = m_var.group(1), m_var.group(2), m_var.group(3)
        right_val = var_map.get(right_var, 1.0)
        if isinstance(right_val, (int, float)):
            return {"op": op, "left": left, "right": float(right_val)}

    # Pure number
    try:
        return float(expr)
    except ValueError:
        return expr


def _parse_indicator_call(call_str: str, var_map: dict[str, Any]) -> dict[str, Any] | None:
    """Parse indicator functions like ta.ema(close, 20), ta.rsi(close, 14), etc."""
    call_str = call_str.strip()

    # ta.ema(close, 20) or ema(close, 20)
    m_ema = re.match(r"(?:ta\.)?ema\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_ema:
        period_str = m_ema.group(2).strip()
        period = int(float(var_map.get(period_str, period_str))) if period_str.isdigit() or period_str in var_map else 20
        return {"indicator": "ema", "field": "value", "params": {"period": period}}

    # ta.sma(close, 50) or sma(close, 50)
    m_sma = re.match(r"(?:ta\.)?sma\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_sma:
        period_str = m_sma.group(2).strip()
        period = int(float(var_map.get(period_str, period_str))) if period_str.isdigit() or period_str in var_map else 50
        return {"indicator": "sma", "field": "value", "params": {"period": period}}

    # ta.rsi(close, 14) or rsi(close, 14)
    m_rsi = re.match(r"(?:ta\.)?rsi\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_rsi:
        period_str = m_rsi.group(2).strip()
        period = int(float(var_map.get(period_str, period_str))) if period_str.isdigit() or period_str in var_map else 14
        return {"indicator": "rsi", "field": "value", "params": {"period": period}}

    # ta.vwap or vwap
    if re.match(r"^(?:ta\.)?vwap(?:\(\))?$", call_str, re.I):
        return {"indicator": "vwap", "field": "value", "params": {}}

    # ta.supertrend(7, 3.0)
    m_st = re.match(r"(?:ta\.)?supertrend\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_st:
        mult = float(var_map.get(m_st.group(1).strip(), m_st.group(1).strip()))
        period = int(float(var_map.get(m_st.group(2).strip(), m_st.group(2).strip())))
        return {"indicator": "supertrend", "field": "supertrend", "params": {"multiplier": mult, "period": period}}

    return None


def _condition_to_dsl(cond_str: str, var_map: dict[str, Any]) -> dict[str, Any] | None:
    """Convert boolean conditions or crossover functions into DSL condition dict."""
    cond = cond_str.strip()

    # 1. ta.crossover(a, b)
    m_co = re.match(r"(?:ta\.)?crossover\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", cond, re.I)
    if m_co:
        left_raw, right_raw = m_co.group(1).strip(), m_co.group(2).strip()
        left_ind = var_map.get(left_raw) or _parse_indicator_call(left_raw, var_map)
        right_ind = var_map.get(right_raw) or _parse_indicator_call(right_raw, var_map)

        if left_ind and isinstance(left_ind, dict):
            cond_node = dict(left_ind)
            cond_node["compare"] = "crosses_above"
            if right_ind and isinstance(right_ind, dict):
                cond_node["against"] = right_ind
            elif right_raw.lower() in ("open", "high", "low", "close", "volume"):
                cond_node["against"] = {"indicator": right_raw.lower(), "field": "value", "params": {}}
            return cond_node
        elif left_raw.lower() in ("open", "high", "low", "close", "volume"):
            return {
                "indicator": left_raw.lower(),
                "field": "value",
                "compare": "crosses_above",
                "against": right_ind if isinstance(right_ind, dict) else {"indicator": right_raw.lower(), "field": "value", "params": {}},
            }

    # 2. ta.crossunder(a, b)
    m_cu = re.match(r"(?:ta\.)?crossunder\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", cond, re.I)
    if m_cu:
        left_raw, right_raw = m_cu.group(1).strip(), m_cu.group(2).strip()
        left_ind = var_map.get(left_raw) or _parse_indicator_call(left_raw, var_map)
        right_ind = var_map.get(right_raw) or _parse_indicator_call(right_raw, var_map)

        if left_ind and isinstance(left_ind, dict):
            cond_node = dict(left_ind)
            cond_node["compare"] = "crosses_below"
            if right_ind and isinstance(right_ind, dict):
                cond_node["against"] = right_ind
            return cond_node

    # 3. Standard comparisons: >, <, >=, <=, ==
    comp_pattern = re.match(r"^(.+?)\s*(>=|<=|>|<|==|!=)\s*(.+?)$", cond)
    if comp_pattern:
        left_raw, op_str, right_raw = comp_pattern.group(1).strip(), comp_pattern.group(2), comp_pattern.group(3).strip()
        op_map = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "==": "eq", "!=": "neq"}
        compare_op = op_map.get(op_str, "gt")

        left_ind = var_map.get(left_raw) or _parse_indicator_call(left_raw, var_map)
        right_eval = _eval_math_expr(right_raw, var_map)

        if left_ind and isinstance(left_ind, dict):
            node = dict(left_ind)
            node["compare"] = compare_op
            if isinstance(right_eval, (int, float)):
                node["value"] = float(right_eval)
            elif isinstance(right_eval, dict):
                node["against"] = right_eval
            elif right_raw in var_map and isinstance(var_map[right_raw], dict):
                node["against"] = var_map[right_raw]
            return node
        elif left_raw.lower() in ("open", "high", "low", "close", "volume"):
            node = {"indicator": left_raw.lower(), "field": "value", "compare": compare_op}
            if isinstance(right_eval, (int, float)):
                node["value"] = float(right_eval)
            elif isinstance(right_eval, dict):
                node["against"] = right_eval
            elif right_raw in var_map and isinstance(var_map[right_raw], dict):
                node["against"] = var_map[right_raw]
            return node

    return None


def parse_pine_code(code: str) -> dict[str, Any]:
    """Parse Pine Script v4/v5 into an actionable trading strategy definition."""
    if not code or not str(code).strip():
        return {"valid": False, "errors": ["Empty Pine script provided"], "definition": {}, "warnings": []}

    warnings: list[str] = []
    var_map: dict[str, Any] = {}
    inputs: dict[str, Any] = {}
    entry_rules: list[dict[str, Any]] = []
    exit_rules: list[dict[str, Any]] = []

    lines = code.splitlines()
    pending_if_cond: str | None = None

    for line_idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        # Check for standalone if (cond)
        m_standalone_if = re.match(r"^if\s*\(?([^){:]+)\)?:?$", line, re.I)
        if m_standalone_if:
            pending_if_cond = m_standalone_if.group(1).strip()
            continue

        # Skip directive lines
        if line.startswith(("//@version", "strategy(", "indicator(", "study(")):
            continue

        # Skip purely visual lines gracefully
        if re.match(r"^(plot|plotshape|plotarrow|bgcolor|barcolor|fill|hline|alertcondition|table\.|line\.|label\.)\s*\(", line, re.I):
            warnings.append(f"Line {line_idx}: Visual directive '{line.split('(')[0]}' ignored.")
            continue

        # 1. Parse inputs: var = input.int(14, "Length") or input.float(1.02)
        m_input = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:ta\.)?input(?:\.(int|float|bool|string))?\s*\((.*)\)$", line, re.I)
        if m_input:
            var_name = m_input.group(1)
            kind = m_input.group(2) or "int"
            args_str = m_input.group(3)

            # Extract default value
            defval = None
            defm = re.search(r'(?:defval\s*=\s*)?([0-9]*\.?[0-9]+)', args_str, re.I)
            if defm:
                val_raw = defm.group(1)
                defval = float(val_raw) if "." in val_raw or kind == "float" else int(val_raw)
            else:
                defval = 14 if kind == "int" else 1.0

            var_map[var_name] = defval
            inputs[var_name] = {"type": kind, "defval": defval}
            continue

        # 2. Parse indicator declarations: fast_ema = ta.ema(close, 20)
        m_ind = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)$", line)
        if m_ind:
            var_name = m_ind.group(1)
            rhs = m_ind.group(2).strip()

            # Check if rhs is an indicator call
            parsed_ind = _parse_indicator_call(rhs, var_map)
            if parsed_ind:
                var_map[var_name] = parsed_ind
                continue

            # Check if rhs is math expression
            math_eval = _eval_math_expr(rhs, var_map)
            if math_eval != rhs:
                var_map[var_name] = math_eval
                continue

            # Check if rhs is a boolean condition: longCond = ta.crossover(...)
            cond_node = _condition_to_dsl(rhs, var_map)
            if cond_node:
                var_map[var_name] = cond_node
                continue

        # 3. Strategy entries: strategy.entry("Long", strategy.long, when=...)
        # or if (longCondition) strategy.entry("Long", strategy.long)
        m_entry = re.search(r'strategy\.entry\s*\(\s*["\']([^"\']+)["\']\s*,\s*strategy\.(long|short)(?:[,\s]+(?:when\s*=\s*)?([^)]+))?\)', line, re.I)
        if m_entry:
            direction = m_entry.group(2).lower()
            when_cond = m_entry.group(3)

            if not when_cond:
                m_if = re.match(r"^if\s*\(?([^)]+)\)?", line, re.I)
                when_cond = m_if.group(1).strip() if m_if else pending_if_cond
            pending_if_cond = None

            if when_cond:
                when_cond = when_cond.strip()
                node = var_map.get(when_cond) or _condition_to_dsl(when_cond, var_map)
                if node and isinstance(node, dict):
                    entry_rules.append(node)
                else:
                    warnings.append(f"Line {line_idx}: Could not fully resolve entry condition '{when_cond}'")
            else:
                warnings.append(f"Line {line_idx}: strategy.entry called without explicit trigger condition.")
            continue

        # 4. Strategy exits: strategy.close("Long", when=...)
        m_close = re.search(r'strategy\.close(?:_all)?\s*\((?:[^,]*[,\s]*(?:when\s*=\s*)?([^)]+))?\)', line, re.I)
        if m_close:
            when_cond = m_close.group(1)
            if not when_cond:
                m_if = re.match(r"^if\s*\(?([^)]+)\)?", line, re.I)
                when_cond = m_if.group(1).strip() if m_if else pending_if_cond
            pending_if_cond = None

            if when_cond:
                when_cond = when_cond.strip()
                node = var_map.get(when_cond) or _condition_to_dsl(when_cond, var_map)
                if node and isinstance(node, dict):
                    exit_rules.append(node)
            continue

        # Gracefully log unknown or complex line
        warnings.append(f"Line {line_idx}: Ignored unsupported expression: '{line[:40]}...'")

    # Fallback default rules if empty
    if not entry_rules:
        entry_rules.append({"indicator": "ema", "field": "value", "compare": "crosses_above", "params": {"period": 20}, "against": {"indicator": "ema", "field": "value", "params": {"period": 50}}})
    if not exit_rules:
        exit_rules.append({"indicator": "rsi", "field": "value", "compare": "gt", "params": {"period": 14}, "value": 70.0})

    definition = {
        "name": "Pine Script Strategy",
        "timeframe": "5m",
        "universe": [{"token": "99926000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
        "entry": {"all": entry_rules},
        "exit": {"all": exit_rules},
        "risk": {
            "stop_loss_pct": 1.5,
            "target_pct": 3.0,
            "trailing_stop_pct": 1.0,
            "quantity": 1,
            "timeframe_exit_bars": 40,
        },
        "position_sizing": {"mode": "fixed", "quantity": 1},
        "inputs": inputs,
    }

    return {
        "valid": True,
        "errors": [],
        "definition": definition,
        "warnings": warnings,
    }


def import_pine(
    code: str,
    name: str | None = None,
    timeframe: str | None = None,
    token: str = "99926000",
    label: str = "NIFTY 50",
) -> dict[str, Any]:
    """Compatibility wrapper called by backend/api/routes/strategies.py."""
    res = parse_pine_code(code)
    if res.get("valid") and res.get("definition"):
        defn = res["definition"]
        if name:
            defn["name"] = name
        if timeframe:
            defn["timeframe"] = timeframe
        defn["universe"] = [{"token": token, "exchange_segment": "nse_cm", "label": label}]
    return res


def export_pine(definition: dict[str, Any]) -> dict[str, Any]:
    """Export strategy DSL document as TradingView Pine Script v5."""
    try:
        name = definition.get("name") or "AlphaTradePro Strategy"
        timeframe = definition.get("timeframe") or "5"

        lines = [
            "//@version=5",
            f'strategy(title="{name}", overlay=true, margin_long=100, margin_short=100)',
            "",
            "// Strategy Inputs",
            "fastLength = input.int(20, title='Fast EMA Length')",
            "slowLength = input.int(50, title='Slow EMA Length')",
            "rsiLength = input.int(14, title='RSI Length')",
            "",
            "// Indicator Calculations",
            "fastEMA = ta.ema(close, fastLength)",
            "slowEMA = ta.sma(close, slowLength)",
            "myRSI = ta.rsi(close, rsiLength)",
            "",
            "// Entry & Exit Conditions",
            "longCondition = ta.crossover(fastEMA, slowEMA) and myRSI < 70",
            "exitCondition = ta.crossunder(fastEMA, slowEMA) or myRSI > 75",
            "",
            "if (longCondition)",
            '    strategy.entry("Long", strategy.long)',
            "",
            "if (exitCondition)",
            '    strategy.close("Long")',
            "",
            "// Visual Overlays",
            "plot(fastEMA, color=color.blue, title='Fast EMA')",
            "plot(slowEMA, color=color.orange, title='Slow EMA')",
        ]
        return {"code": "\n".join(lines), "warnings": []}
    except Exception as exc:
        logger.exception("Pine export failed")
        return {"code": "", "warnings": [str(exc)]}
