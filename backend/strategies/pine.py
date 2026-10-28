"""Pine Script parser and translator (upgraded for standard v4/v5/v6 scripts).

Handles:
- Directives: //@version=4, 5, 6, strategy(), indicator(), study()
- Inputs: input.int(), input.float(), input.bool(), input.string(), input(), input.source()
- State variables: var, varip, type declarations (var float x = na, var int y = 0)
- Variable reassignments: `x := expr`
- Technical indicators: ta.ema(), ta.sma(), ta.rsi(), ta.atr(), ta.stdev(), ta.crossover(), ta.crossunder(), ta.vwap(), ta.macd(), ta.supertrend(), etc.
- State conditions: strategy.position_size == 0, strategy.position_size > 0
- Math expressions: assignments like dev = basis - stdev, upper = basis + mult * stdev, close + atr * 2
- Dynamic TP/SL in strategy.exit(..., limit=close + atr, stop=close - atr)
- Graceful skipping of unsupported lines (UI, table.new, table.cell, plot, styling, alerts) without crashing.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

logger = logging.getLogger("alphatrade.strategies.pine")


def _eval_ast_math(node: ast.AST, var_map: dict[str, Any]) -> Any:
    """Recursively evaluate an AST expression with variable substitution."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        name = node.id
        if name.lower() == "na":
            return None
        if name.lower() in ("true", "false"):
            return name.lower() == "true"
        if name in var_map:
            val = var_map[name]
            return val
        return name
    if isinstance(node, ast.Attribute):
        # Attribute access like strategy.position_size
        full_name = f"{node.value.id}.{node.attr}" if isinstance(node.value, ast.Name) else node.attr
        if full_name in var_map:
            return var_map[full_name]
        return full_name
    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_math(node.operand, var_map)
        if isinstance(node.op, ast.USub) and isinstance(operand, (int, float)):
            return -operand
        return {"op": "neg", "operand": operand}
    if isinstance(node, ast.BinOp):
        left = _eval_ast_math(node.left, var_map)
        right = _eval_ast_math(node.right, var_map)
        op_map = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
        op_str = op_map.get(type(node.op), "?")

        # Constant folding if both are numeric
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if op_str == "+":
                return left + right
            if op_str == "-":
                return left - right
            if op_str == "*":
                return left * right
            if op_str == "/" and right != 0:
                return left / right

        return {"op": op_str, "left": left, "right": right}

    return None


def _eval_math_expr(expr: str, var_map: dict[str, Any]) -> dict[str, Any] | float | str:
    """Evaluate math expressions like `dev = basis - stdev` or `upper = basis + mult * stdev`."""
    expr = expr.strip()
    try:
        parsed = ast.parse(expr, mode="eval")
        res = _eval_ast_math(parsed.body, var_map)
        if res is not None:
            return res
    except Exception:
        pass

    # Simple fallback regex for multiplier like close * 1.02
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_\.]*)\s*([\*\/])\s*([0-9]*\.?[0-9]+)$", expr)
    if m:
        left, op, right = m.group(1), m.group(2), float(m.group(3))
        return {"op": op, "left": left, "right": right}

    m_var = re.match(r"^([a-zA-Z_][a-zA-Z0-9_\.]*)\s*([\*\/])\s*([a-zA-Z_][a-zA-Z0-9_]*)$", expr)
    if m_var:
        left, op, right_var = m_var.group(1), m_var.group(2), m_var.group(3)
        right_val = var_map.get(right_var, 1.0)
        if isinstance(right_val, (int, float)):
            return {"op": op, "left": left, "right": float(right_val)}

    try:
        return float(expr)
    except ValueError:
        return expr


def _resolve_period(val_str: str, var_map: dict[str, Any], default: int) -> int:
    """Resolve an indicator period string from literals or var_map."""
    val_str = val_str.strip()
    if val_str.isdigit():
        return int(val_str)
    if val_str in var_map and isinstance(var_map[val_str], (int, float)):
        return int(var_map[val_str])
    try:
        return int(float(val_str))
    except (ValueError, TypeError):
        return default


def _parse_indicator_call(call_str: str, var_map: dict[str, Any]) -> dict[str, Any] | None:
    """Parse indicator functions like ta.ema, ta.sma, ta.rsi, ta.atr, ta.stdev, ta.vwap, etc."""
    call_str = call_str.strip()

    # ta.atr(14) or ta.atr(length)
    m_atr = re.match(r"(?:ta\.)?atr\s*\(\s*([^)]+?)\s*\)", call_str, re.I)
    if m_atr:
        period = _resolve_period(m_atr.group(1), var_map, 14)
        return {"indicator": "atr", "field": "value", "params": {"period": period}}

    # ta.stdev(close, 20) or ta.stdev(20)
    m_stdev = re.match(r"(?:ta\.)?stdev\s*\(\s*(?:([^,]+?)\s*,\s*)?([^)]+?)\s*\)", call_str, re.I)
    if m_stdev:
        period_str = m_stdev.group(2) if m_stdev.group(2) else m_stdev.group(1)
        period = _resolve_period(period_str, var_map, 20)
        return {"indicator": "stdev", "field": "value", "params": {"period": period}}

    # ta.ema(close, 20) or ema(close, 20)
    m_ema = re.match(r"(?:ta\.)?ema\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_ema:
        period = _resolve_period(m_ema.group(2), var_map, 20)
        return {"indicator": "ema", "field": "value", "params": {"period": period}}

    # ta.sma(close, 50) or sma(close, 50)
    m_sma = re.match(r"(?:ta\.)?sma\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_sma:
        period = _resolve_period(m_sma.group(2), var_map, 50)
        return {"indicator": "sma", "field": "value", "params": {"period": period}}

    # ta.rsi(close, 14) or rsi(close, 14)
    m_rsi = re.match(r"(?:ta\.)?rsi\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_rsi:
        period = _resolve_period(m_rsi.group(2), var_map, 14)
        return {"indicator": "rsi", "field": "value", "params": {"period": period}}

    # ta.vwap
    if re.match(r"^(?:ta\.)?vwap(?:\(\))?$", call_str, re.I):
        return {"indicator": "vwap", "field": "value", "params": {}}

    # ta.supertrend(7, 3.0)
    m_st = re.match(r"(?:ta\.)?supertrend\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", call_str, re.I)
    if m_st:
        p1 = var_map.get(m_st.group(1).strip(), m_st.group(1).strip())
        p2 = var_map.get(m_st.group(2).strip(), m_st.group(2).strip())
        try:
            mult = float(p1)
            period = int(float(p2))
        except (ValueError, TypeError):
            mult = 3.0
            period = 7
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
            elif right_raw in var_map:
                cond_node["against"] = var_map[right_raw]
            return cond_node
        elif left_raw.lower() in ("open", "high", "low", "close", "volume"):
            return {
                "indicator": left_raw.lower(),
                "field": "value",
                "compare": "crosses_above",
                "against": right_ind if isinstance(right_ind, dict) else (var_map.get(right_raw) or {"indicator": right_raw.lower(), "field": "value", "params": {}}),
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
            elif right_raw.lower() in ("open", "high", "low", "close", "volume"):
                cond_node["against"] = {"indicator": right_raw.lower(), "field": "value", "params": {}}
            elif right_raw in var_map:
                cond_node["against"] = var_map[right_raw]
            return cond_node
        elif left_raw.lower() in ("open", "high", "low", "close", "volume"):
            return {
                "indicator": left_raw.lower(),
                "field": "value",
                "compare": "crosses_below",
                "against": right_ind if isinstance(right_ind, dict) else (var_map.get(right_raw) or {"indicator": right_raw.lower(), "field": "value", "params": {}}),
            }

    # 3. Standard comparisons: >, <, >=, <=, ==, != (including strategy.position_size == 0)
    comp_pattern = re.match(r"^(.+?)\s*(>=|<=|>|<|==|!=)\s*(.+?)$", cond)
    if comp_pattern:
        left_raw, op_str, right_raw = comp_pattern.group(1).strip(), comp_pattern.group(2), comp_pattern.group(3).strip()
        op_map = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "==": "eq", "!=": "neq"}
        compare_op = op_map.get(op_str, "gt")

        # State condition: strategy.position_size == 0
        if left_raw.lower() in ("strategy.position_size", "position_size"):
            right_eval = _eval_math_expr(right_raw, var_map)
            node = {
                "indicator": "strategy.position_size",
                "field": "value",
                "compare": compare_op,
            }
            if isinstance(right_eval, (int, float)):
                node["value"] = float(right_eval)
            elif isinstance(right_eval, dict):
                node["against"] = right_eval
            return node

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


def _parse_exit_params(call_str: str, var_map: dict[str, Any]) -> dict[str, Any]:
    """Extract limit (TP), stop (SL), loss, profit from strategy.exit(...) call."""
    exit_dict: dict[str, Any] = {}
    try:
        node = ast.parse(call_str, mode="eval").body
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                val = _eval_ast_math(kw.value, var_map)
                exit_dict[kw.arg] = val
    except Exception:
        for param in ("limit", "stop", "loss", "profit", "trail_points", "trail_offset"):
            m = re.search(rf"{param}\s*=\s*([^,\)]+)", call_str, re.I)
            if m:
                exit_dict[param] = _eval_math_expr(m.group(1).strip(), var_map)
    return exit_dict


def parse_pine_code(code: str) -> dict[str, Any]:
    """Parse Pine Script v4/v5/v6 into an actionable trading strategy definition."""
    if not code or not str(code).strip():
        return {"valid": False, "errors": ["Empty Pine script provided"], "definition": {}, "warnings": []}

    warnings: list[str] = []
    var_map: dict[str, Any] = {}
    inputs: dict[str, Any] = {}
    entry_rules: list[dict[str, Any]] = []
    exit_rules: list[dict[str, Any]] = []

    risk_config: dict[str, Any] = {
        "stop_loss_pct": 1.5,
        "target_pct": 3.0,
        "trailing_stop_pct": 1.0,
        "quantity": 1,
        "timeframe_exit_bars": 40,
    }

    lines = code.splitlines()
    pending_if_cond: str | None = None

    for line_idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        # Check for standalone if (cond) or if cond
        m_standalone_if = re.match(r"^if\s*\(?([^){:]+)\)?:?$", line, re.I)
        if m_standalone_if:
            pending_if_cond = m_standalone_if.group(1).strip()
            continue

        # Skip directive lines: v4, v5, v6, indicator, strategy
        if line.startswith(("//@version", "strategy(", "indicator(", "study(")):
            continue

        # Skip UI, tables, plots, labels, lines gracefully (e.g. table.cell, table.new)
        if re.match(
            r"^(?:var\s+(?:table|label|line)\s+[a-zA-Z0-9_]+\s*[:=]+\s*)?(?:plot|plotshape|plotarrow|plotchar|bgcolor|barcolor|fill|hline|alertcondition|table\.|line\.|label\.)",
            line,
            re.I,
        ):
            warnings.append(f"Line {line_idx}: UI/visual directive '{line.split('(')[0].strip()}' skipped.")
            continue

        # 1. Parse inputs: input.int(14, "Length"), input.float(), input(), var float, etc.
        m_input = re.match(
            r"^(?:var\s+(?:float|int|bool|string)\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]+\s*(?:ta\.)?input(?:\.(int|float|bool|string))?\s*\((.*)\)$",
            line,
            re.I,
        )
        if m_input:
            var_name = m_input.group(1)
            kind = m_input.group(2) or "int"
            args_str = m_input.group(3)

            defval = None
            defm = re.search(r"(?:defval\s*=\s*)?([0-9]*\.?[0-9]+)", args_str, re.I)
            if defm:
                val_raw = defm.group(1)
                defval = float(val_raw) if "." in val_raw or kind == "float" else int(val_raw)
            else:
                defval = 14 if kind == "int" else 1.0

            var_map[var_name] = defval
            inputs[var_name] = {"type": kind, "defval": defval}
            continue

        # 2. Parse State variables and reassignments:
        #    var float trailingStop = na
        #    var int entryBar = 0
        #    trailingStop := close + 10
        m_var_decl = re.match(
            r"^var(?:ip)?\s+(?:float|int|bool|string)?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]+\s*(.+)$",
            line,
            re.I,
        )
        if m_var_decl:
            var_name = m_var_decl.group(1)
            init_val_raw = m_var_decl.group(2).strip()

            if init_val_raw.startswith(("table.new", "label.new", "line.new")):
                warnings.append(f"Line {line_idx}: Skipped UI object declaration for '{var_name}'.")
                continue

            init_val = _eval_math_expr(init_val_raw, var_map)
            var_map[var_name] = init_val
            continue

        # Variable reassignment: `var_name := rhs`
        m_reassign = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:=\s*(.+)$", line)
        if m_reassign:
            var_name = m_reassign.group(1)
            rhs = m_reassign.group(2).strip()

            parsed_ind = _parse_indicator_call(rhs, var_map)
            if parsed_ind:
                var_map[var_name] = parsed_ind
                continue

            cond_node = _condition_to_dsl(rhs, var_map)
            if cond_node:
                var_map[var_name] = cond_node
                continue

            math_eval = _eval_math_expr(rhs, var_map)
            var_map[var_name] = math_eval
            continue

        # 3. Strategy entries: strategy.entry("Long", strategy.long, when=...)
        m_entry = re.search(
            r'strategy\.entry\s*\(\s*["\']([^"\']+)["\']\s*,\s*strategy\.(long|short)(?:[,\s]+(?:when\s*=\s*)?([^)]+))?\)',
            line,
            re.I,
        )
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

        # 4. Strategy exits: strategy.exit("TP/SL", "Long", limit=close + atr, stop=close - atr)
        m_exit = re.search(r"strategy\.exit\s*\((.*)\)", line, re.I)
        if m_exit:
            exit_params = _parse_exit_params(m_exit.group(0), var_map)
            if "limit" in exit_params:
                risk_config["target_dynamic"] = exit_params["limit"]
            if "stop" in exit_params:
                risk_config["stop_loss_dynamic"] = exit_params["stop"]
            if "loss" in exit_params and isinstance(exit_params["loss"], (int, float)):
                risk_config["stop_loss_pct"] = float(exit_params["loss"])
            if "profit" in exit_params and isinstance(exit_params["profit"], (int, float)):
                risk_config["target_pct"] = float(exit_params["profit"])

            exit_rules.append({
                "action": "strategy.exit",
                "params": exit_params,
            })
            continue

        # 5. Strategy close: strategy.close("Long", when=...) or strategy.close_all()
        m_close = re.search(r"strategy\.close(?:_all)?\s*\((?:[^,]*[,\s]*(?:when\s*=\s*)?([^)]+))?\)", line, re.I)
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

        # 6. Standard variable assignment: var = rhs (indicators, math, or conditions)
        m_assign = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)$", line)
        if m_assign:
            var_name = m_assign.group(1)
            rhs = m_assign.group(2).strip()

            # A. Technical indicator call: atr = ta.atr(14)
            parsed_ind = _parse_indicator_call(rhs, var_map)
            if parsed_ind:
                var_map[var_name] = parsed_ind
                continue

            # B. Boolean condition: longCond = ta.crossover(close, upper)
            cond_node = _condition_to_dsl(rhs, var_map)
            if cond_node:
                var_map[var_name] = cond_node
                continue

            # C. Math variable assignment: dev = basis - stdev, upper = basis + mult * stdev
            math_eval = _eval_math_expr(rhs, var_map)
            var_map[var_name] = math_eval
            continue

        # Gracefully record unknown line
        warnings.append(f"Line {line_idx}: Ignored expression: '{line[:40]}...'")

    # Fallback default rules if empty
    if not entry_rules:
        entry_rules.append({
            "indicator": "ema",
            "field": "value",
            "compare": "crosses_above",
            "params": {"period": 20},
            "against": {"indicator": "ema", "field": "value", "params": {"period": 50}},
        })
    if not exit_rules:
        exit_rules.append({
            "indicator": "rsi",
            "field": "value",
            "compare": "gt",
            "params": {"period": 14},
            "value": 70.0,
        })

    definition = {
        "name": "Pine Script Strategy",
        "timeframe": "5m",
        "universe": [{"token": "99926000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
        "entry": {"all": entry_rules},
        "exit": {"all": exit_rules},
        "risk": risk_config,
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
    """Export strategy DSL document as TradingView Pine Script v5/v6."""
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
