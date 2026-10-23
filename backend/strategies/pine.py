"""Pine Script parser (upgraded)."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("alphatrade.strategies.pine")


def _eval_expr(expr: str) -> Any:
    expr = expr.strip()
    m = re.match(r"^(.+?)\s*\*\s*([0-9]*\.?[0-9]+)$", expr)
    if m:
        base = m.group(1).strip()
        val = float(m.group(2))
        return {"op": "*", "left": base, "right": val}
    return expr


def parse_pine_code(code: str) -> dict[str, Any]:
    try:
        if not code or not str(code).strip():
            return {"valid": False, "errors": ["Empty Pine script"], "definition": {}, "warnings": []}
        
        warnings: list[str] = []
        definition: dict[str, Any] = {
            "name": "Imported Pine Script",
            "rules": {"entry_long": [], "entry_short": [], "exit_long": [], "exit_short": []},
            "inputs": {},
        }
        
        # inputs
        for m in re.finditer(r'input\.(int|float)\s*\(([^)]+)\)', code, re.I):
            kind, rest = m.group(1), m.group(2)
            defm = re.search(r'defval\s*=\s*([0-9]*\.?[0-9]+)', rest, re.I)
            if defm:
                val = int(float(defm.group(1))) if kind == "int" else float(defm.group(1))
                definition["inputs"][f"input_{len(definition['inputs'])}"] = {"type": kind, "defval": val}
        
        # strategy entries
        for m in re.finditer(r'strategy\.entry\s*\([^)]+?,\s*([^)]+?)\)', code, re.I):
            cond = m.group(1).strip()
            if "strategy.short" in cond.lower() or "short" in code[m.start():m.end()+50].lower():
                definition["rules"]["entry_short"].append({"cond": cond})
            else:
                definition["rules"]["entry_long"].append({"cond": cond})
        
        # ignore plot/bgcolor
        if re.search(r'plot\s*\(|bgcolor\s*\(', code, re.I):
            warnings.append("plot/bgcolor ignored for logic")
        
        return {"valid": True, "errors": [], "definition": definition, "warnings": warnings}
    except Exception as e:
        logger.exception("Pine parse failed")
        return {"valid": False, "errors": [str(e)], "definition": {}, "warnings": []}


def export_pine(definition: dict[str, Any]) -> dict[str, Any]:
    try:
        name = definition.get("name") or "AlphaTradePro Strategy"
        code = f'//@version=5\nstrategy(title="{name}", overlay=true)\n'
        return {"code": code, "warnings": ["Export is a skeleton"]}
    except Exception as e:
        logger.exception("Pine export failed")
        return {"code": "", "warnings": [str(e)]}
