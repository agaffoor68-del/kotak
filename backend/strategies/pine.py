"""Pine Script parser (hardened)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("alphatrade.strategies.pine")


def parse_pine_code(code: str) -> dict[str, Any]:
    try:
        if not code or not str(code).strip():
            return {"valid": False, "errors": ["Empty Pine script"], "definition": {}, "warnings": []}
        return {"valid": True, "errors": [], "definition": {"type": "pine", "raw": code}, "warnings": []}
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
