"""Built-in strategy templates.

These are starting points, not recommendations: each is a rule set the user
inspects, edits and backtests. No template carries a market opinion.
"""

from __future__ import annotations

from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "EMA crossover with volume confirmation",
        "description": "Buys a fast EMA crossing above a slow EMA while volume expands. A classic trend entry.",
        "kind": "rule",
        "definition": {
            "name": "EMA crossover with volume confirmation",
            "timeframe": "15m",
            "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
            "entry": {
                "all": [
                    {"indicator": "ema", "params": {"period": 20}, "field": "value", "compare": "crosses_above",
                     "against": {"indicator": "ema", "params": {"period": 50}, "field": "value"}},
                    {"indicator": "cmf", "params": {"period": 20}, "field": "value", "compare": "gt", "value": 0.0},
                ]
            },
            "exit": {
                "all": [
                    {"indicator": "ema", "params": {"period": 20}, "field": "value", "compare": "crosses_below",
                     "against": {"indicator": "ema", "params": {"period": 50}, "field": "value"}}
                ]
            },
            "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0, "trailing_stop_pct": 0.8,
                     "timeframe_exit_bars": 40},
            "position_sizing": {"mode": "pct_risk", "risk_pct": 0.5, "quantity": 1},
        },
    },
    {
        "name": "RSI mean reversion",
        "description": "Buys oversold RSI readings that are reversing, filtered by a SuperTrend direction check.",
        "kind": "rule",
        "definition": {
            "name": "RSI mean reversion",
            "timeframe": "5m",
            "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
            "entry": {
                "all": [
                    {"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 32},
                    {"indicator": "rsi", "params": {"period": 14}, "compare": "crosses_above",
                     "against": {"value": 30}},
                ]
            },
            "exit": {
                "any": [
                    {"indicator": "rsi", "params": {"period": 14}, "compare": "gt", "value": 60},
                    {"indicator": "bollinger", "params": {"period": 20}, "field": "middle", "compare": "crosses_above",
                     "against": {"indicator": "bollinger", "params": {"period": 20}, "field": "middle"}},
                ]
            },
            "risk": {"stop_loss_pct": 0.6, "target_pct": 1.2, "timeframe_exit_bars": 24},
            "position_sizing": {"mode": "fixed", "quantity": 1},
        },
    },
    {
        "name": "SuperTrend trend following",
        "description": "Rides SuperTrend flips in a confirmed directional regime, with ADX filtering weak trends.",
        "kind": "rule",
        "definition": {
            "name": "SuperTrend trend following",
            "timeframe": "15m",
            "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
            "entry": {
                "all": [
                    {"indicator": "supertrend", "params": {"period": 10, "multiplier": 3.0},
                     "field": "direction", "compare": "gt", "value": 0},
                    {"indicator": "adx", "params": {"period": 14}, "field": "adx", "compare": "gt", "value": 22},
                ]
            },
            "exit": {
                "any": [
                    {"indicator": "supertrend", "params": {"period": 10, "multiplier": 3.0},
                     "field": "direction", "compare": "lt", "value": 0},
                    {"indicator": "adx", "params": {"period": 14}, "field": "adx", "compare": "lt", "value": 16},
                ]
            },
            "risk": {"stop_loss_pct": 1.2, "target_pct": 2.5, "trailing_stop_pct": 1.0},
            "position_sizing": {"mode": "pct_risk", "risk_pct": 0.75, "quantity": 1},
        },
    },
    {
        "name": "Bollinger band breakout",
        "description": "Enters on a close above the upper Bollinger band with expanding bandwidth, exits back inside.",
        "kind": "rule",
        "definition": {
            "name": "Bollinger band breakout",
            "timeframe": "5m",
            "universe": [{"token": "26009", "exchange_segment": "nse_cm", "label": "NIFTY BANK"}],
            "entry": {
                "all": [
                    {"indicator": "close", "params": {}, "field": "value", "compare": "gt",
                     "against": {"indicator": "bollinger", "params": {"period": 20, "deviations": 2.0}, "field": "upper"}},
                    {"indicator": "obv", "params": {}, "field": "value", "compare": "gt", "value": 0},
                ]
            },
            "exit": {
                "all": [
                    {"indicator": "close", "params": {}, "field": "value", "compare": "lt",
                     "against": {"indicator": "bollinger", "params": {"period": 20, "deviations": 2.0}, "field": "middle"}}
                ]
            },
            "risk": {"stop_loss_pct": 0.8, "target_pct": 1.6, "timeframe_exit_bars": 20},
            "position_sizing": {"mode": "fixed", "quantity": 1},
        },
    },
    {
        "name": "MACD momentum with multi-timeframe filter",
        "description": "Requires MACD momentum agreement on the signal timeframe and trend agreement on a slower one.",
        "kind": "rule",
        "definition": {
            "name": "MACD momentum with multi-timeframe filter",
            "timeframe": "5m",
            "universe": [{"token": "26037", "exchange_segment": "nse_cm", "label": "FIN NIFTY"}],
            "entry": {
                "all": [
                    {"indicator": "macd", "params": {"fast": 12, "slow": 26, "signal": 9},
                     "field": "histogram", "compare": "crosses_above", "against": {"value": 0}},
                    {"indicator": "sma", "params": {"period": 50}, "field": "value", "compare": "gt",
                     "against": {"indicator": "sma", "params": {"period": 200}, "field": "value"}},
                ]
            },
            "exit": {
                "all": [
                    {"indicator": "macd", "params": {"fast": 12, "slow": 26, "signal": 9},
                     "field": "histogram", "compare": "lt", "value": 0}
                ]
            },
            "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0, "trailing_stop_pct": 0.75},
            "position_sizing": {"mode": "pct_risk", "risk_pct": 0.5, "quantity": 1},
        },
    },
    {
        "name": "VWAP reversion with VWMA confirmation",
        "description": "Trades price back through VWAP when the volume-weighted moving average agrees with the direction.",
        "kind": "rule",
        "definition": {
            "name": "VWAP reversion with VWMA confirmation",
            "timeframe": "5m",
            "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
            "entry": {
                "all": [
                    {"indicator": "close", "params": {}, "field": "value", "compare": "crosses_above",
                     "against": {"indicator": "vwap", "params": {}, "field": "value"}},
                    {"indicator": "close", "params": {}, "field": "value", "compare": "gt",
                     "against": {"indicator": "sma", "params": {"period": 20}, "field": "value"}},
                ]
            },
            "exit": {
                "all": [
                    {"indicator": "close", "params": {}, "field": "value", "compare": "crosses_below",
                     "against": {"indicator": "vwap", "params": {}, "field": "value"}}
                ]
            },
            "risk": {"stop_loss_pct": 0.5, "target_pct": 1.0, "timeframe_exit_bars": 16},
            "position_sizing": {"mode": "fixed", "quantity": 1},
        },
    },
]


def by_name() -> list[dict[str, Any]]:
    return TEMPLATES


def get(name: str) -> dict[str, Any] | None:
    for template in TEMPLATES:
        if template["name"] == name:
            return template
    return None
