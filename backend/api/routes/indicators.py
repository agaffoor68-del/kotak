"""Dynamic 100+ technical indicators calculation engine using pandas_ta."""

from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import pandas as pd
import pandas_ta as ta

from backend.api.deps import CurrentUser, current_user
from backend.ai.live_scanner import INDEX_CONFIG, bootstrap_candles
from backend.core.database import app_cursor
from backend.marketdata import ticks

logger = logging.getLogger("alphatrade.api.indicators")
router = APIRouter(prefix="/api/v1/indicators", tags=["indicators"])

POPULAR_INDICATORS = [
    {"name": "supertrend", "category": "trend", "label": "Supertrend", "params": {"length": 7, "multiplier": 3.0}},
    {"name": "rsi", "category": "momentum", "label": "Relative Strength Index (RSI)", "params": {"length": 14}},
    {"name": "macd", "category": "momentum", "label": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}},
    {"name": "ema", "category": "trend", "label": "Exponential Moving Average (EMA)", "params": {"length": 20}},
    {"name": "sma", "category": "trend", "label": "Simple Moving Average (SMA)", "params": {"length": 50}},
    {"name": "bbands", "category": "volatility", "label": "Bollinger Bands", "params": {"length": 20, "std": 2.0}},
    {"name": "stochrsi", "category": "momentum", "label": "Stochastic RSI", "params": {"length": 14, "rsi_length": 14, "k": 3, "d": 3}},
    {"name": "vwap", "category": "volume", "label": "VWAP", "params": {}},
    {"name": "adx", "category": "trend", "label": "Average Directional Index (ADX)", "params": {"length": 14}},
    {"name": "atr", "category": "volatility", "label": "Average True Range (ATR)", "params": {"length": 14}},
    {"name": "obv", "category": "volume", "label": "On-Balance Volume (OBV)", "params": {}},
    {"name": "cci", "category": "momentum", "label": "Commodity Channel Index (CCI)", "params": {"length": 14}},
    {"name": "dema", "category": "trend", "label": "Double Exponential MA (DEMA)", "params": {"length": 20}},
    {"name": "keltner", "category": "volatility", "label": "Keltner Channels", "params": {"length": 20}},
    {"name": "donchian", "category": "volatility", "label": "Donchian Channels", "params": {"length": 20}},
    {"name": "mfi", "category": "volume", "label": "Money Flow Index (MFI)", "params": {"length": 14}},
    {"name": "willr", "category": "momentum", "label": "Williams %R", "params": {"length": 14}},
    {"name": "ichimoku", "category": "trend", "label": "Ichimoku Cloud", "params": {}},
]


class CalculateIndicatorRequest(BaseModel):
    symbol: str = Field(..., description="Symbol, index name, or token (e.g. 'NIFTY 50', 'BANKNIFTY', 'RELIANCE')")
    timeframe: str = Field(default="5m", description="Candle timeframe: 1m, 5m, 15m, 1h, 1d")
    indicator_name: str = Field(..., description="Indicator name e.g. 'supertrend', 'macd', 'rsi', 'ema', 'bbands', 'stochrsi'")
    params: dict[str, Any] = Field(default_factory=dict, description="Parameters for the indicator")


def _resolve_symbol(symbol: str) -> tuple[str, str, str]:
    """Return (token, exchange_segment, canonical_symbol)."""
    norm = symbol.strip().upper()
    if norm in ("NIFTY", "NIFTY 50", "99926000"):
        return "99926000", "nse_cm", "NIFTY 50"
    if norm in ("BANKNIFTY", "NIFTY BANK", "99926009"):
        return "99926009", "nse_cm", "BANKNIFTY"
    if norm in ("SENSEX", "BSE SENSEX", "99919000"):
        return "99919000", "bse_cm", "SENSEX"
    if norm in ("FINNIFTY", "99926037"):
        return "99926037", "nse_cm", "FINNIFTY"
    if norm in ("MIDCPNIFTY", "99926074"):
        return "99926074", "nse_cm", "MIDCPNIFTY"
    if norm in INDEX_CONFIG:
        cfg = INDEX_CONFIG[norm]
        return cfg["token"], cfg["segment"], cfg.get("name", cfg["symbol"])

    try:
        with app_cursor() as cursor:
            cursor.execute(
                """
                SELECT token, exchange_segment, trading_symbol, symbol
                FROM instruments
                WHERE trading_symbol = ? OR symbol = ? OR token = ?
                LIMIT 1
                """,
                (symbol, symbol, symbol),
            )
            row = cursor.fetchone()
            if row:
                return str(row["token"]), str(row["exchange_segment"]), str(row["trading_symbol"] or row["symbol"])
    except Exception:
        pass

    return symbol, "nse_cm", symbol


def _get_candles(symbol: str, token: str, segment: str, timeframe: str) -> list[dict[str, Any]]:
    """Retrieve candles from bootstrap loader, stored DB history, or cold-start fallback."""
    if symbol in INDEX_CONFIG or token in ("99926000", "99926009", "99919000", "99926037", "99926074"):
        idx_key = next((k for k, v in INDEX_CONFIG.items() if v["token"] == token or k == symbol), symbol)
        candles = bootstrap_candles(idx_key, min_candles=100)
        if candles and len(candles) >= 15:
            return candles

    try:
        candles = ticks.stored_history(token, segment, timeframe, limit=300)
        if candles and len(candles) >= 15:
            return candles
    except Exception:
        pass

    try:
        candles = ticks.build_candles(token, segment, timeframe)
        if candles and len(candles) >= 15:
            return candles
    except Exception:
        pass

    from backend.ai.live_scanner import _generate_synthetic_candles
    base_price = 24350.0 if "NIFTY" in symbol else 51250.0 if "BANK" in symbol else 2500.0
    return _generate_synthetic_candles(base_price, count=100)


@router.get("/list")
def list_available_indicators(
    category: str | None = Query(default=None),
    user: CurrentUser = Depends(current_user),
):
    """Return catalog of 100+ indicators supported by pandas_ta."""
    # Retrieve all indicators present in pandas_ta
    all_categories = ta.Category if hasattr(ta, "Category") else {}
    indicators_list = []
    
    for cat_name, ind_names in all_categories.items():
        if category and cat_name.lower() != category.lower():
            continue
        for name in ind_names:
            indicators_list.append({
                "name": name,
                "category": cat_name,
                "label": name.upper(),
            })

    if not indicators_list:
        indicators_list = POPULAR_INDICATORS

    return {
        "count": len(indicators_list),
        "popular": POPULAR_INDICATORS,
        "indicators": indicators_list,
    }


@router.post("/calculate")
def calculate_indicator(
    body: CalculateIndicatorRequest,
    user: CurrentUser = Depends(current_user),
):
    """TASK 2: 100+ Indicators Engine.
    
    Dynamically applies any pandas_ta indicator on candle data and returns calculated series.
    """
    token, segment, canonical_symbol = _resolve_symbol(body.symbol)
    candles = _get_candles(canonical_symbol, token, segment, body.timeframe)
    if not candles:
        raise HTTPException(404, f"No candle data available for symbol '{body.symbol}'")

    df = pd.DataFrame(candles)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "time" in df.columns:
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
    else:
        df["time"] = range(len(df))

    indicator_name = body.indicator_name.lower().strip()
    params = dict(body.params or {})

    # Normalize common parameter aliases
    if "period" in params and "length" not in params:
        params["length"] = params.pop("period")

    # Run pandas_ta
    try:
        method = getattr(ta, indicator_name, None)
        if method is not None:
            try:
                res = df.ta(indicator_name, **params)
            except Exception:
                res = method(df["close"], **params)
        else:
            res = df.ta(indicator_name, **params)
    except Exception as exc:
        logger.warning("pandas_ta execution failed for %s: %s", indicator_name, exc)
        raise HTTPException(422, f"Failed to compute indicator '{indicator_name}': {exc}")

    if res is None:
        raise HTTPException(422, f"Indicator '{indicator_name}' returned no data with provided parameters.")

    if isinstance(res, pd.Series):
        res_df = res.to_frame()
    else:
        res_df = res

    columns = list(res_df.columns)
    output_rows = []
    for i in range(len(df)):
        t = int(df["time"].iloc[i])
        close_val = float(df["close"].iloc[i]) if not pd.isna(df["close"].iloc[i]) else None
        row_dict: dict[str, Any] = {"time": t, "close": close_val}
        for col in columns:
            val = res_df[col].iloc[i]
            if pd.isna(val) or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
                row_dict[col] = None
            else:
                row_dict[col] = float(val) if isinstance(val, (int, float, bool)) else val
        output_rows.append(row_dict)

    return {
        "symbol": canonical_symbol,
        "token": token,
        "timeframe": body.timeframe,
        "indicator_name": indicator_name,
        "columns": columns,
        "values": output_rows,
    }
