"""Deterministic, explainable intraday trade advisor."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from backend.angel import candles as angel_candles
from backend.angel.quotes import fetch as fetch_quotes
from backend.api.deps import CurrentUser, angel_call, current_user
from backend.marketdata import search_index
from backend.strategies.indicators import ema, rsi, supertrend, vwap

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


def _instrument(query: str, fallback_token: str | None = None) -> dict[str, Any] | None:
    matches = search_index.search_all(query, limit=10)
    return matches[0] if matches else (search_index.find_instrument(fallback_token, "nse_cm") if fallback_token else None)


@router.get("/suggest_trade")
def suggest_trade(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Read live NIFTY/VIX data and apply the published advisor rules."""
    nifty = _instrument("NIFTY 50", "26000")
    vix = _instrument("INDIA VIX")
    if not nifty:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "NIFTY 50 is not available in the instrument master")

    call = angel_call(user)
    quotes = fetch_quotes(call, [(str(nifty["token"]), str(nifty.get("exchange_segment") or "nse_cm")), *(
        [(str(vix["token"]), str(vix.get("exchange_segment") or "nse_cm"))] if vix else []
    )], quote_type="all")
    quote_by_token = {str(quote.token): quote for quote in quotes}
    spot_quote = quote_by_token.get(str(nifty["token"]))
    spot = spot_quote.last if spot_quote else None
    if spot is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Live NIFTY spot price is unavailable")

    try:
        end = datetime.now(tz=angel_candles.IST)
        candles = angel_candles.fetch(call, str(nifty["token"]), str(nifty.get("exchange_segment") or "nse_cm"), "5m", start=end - timedelta(days=3), end=end)
    except angel_candles.CandleError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    if len(candles) < 55:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Not enough 5-minute candles to calculate the advisor")

    closes = [candle["close"] for candle in candles]
    latest_rsi = rsi(closes, 14)[-1]
    latest_ema20 = ema(closes, 20)[-1]
    latest_ema50 = ema(closes, 50)[-1]
    latest_vwap = vwap(candles)[-1]
    trend = supertrend(candles, 10, 3)["direction"][-1]
    if None in (latest_rsi, latest_ema20, latest_ema50, latest_vwap, trend):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Indicators are still warming up")

    if 45 <= latest_rsi <= 55:
        signal, confidence, reason = "WAIT", "Medium", "RSI is in the 45–55 sideways zone"
    elif spot > latest_vwap and latest_rsi > 55 and trend == 1:
        signal, confidence, reason = "BUY CE", "High", f"Price above VWAP, RSI momentum {latest_rsi:.0f}"
    elif spot < latest_vwap and latest_rsi < 45 and trend == -1:
        signal, confidence, reason = "BUY PE", "High", f"Price below VWAP, RSI momentum {latest_rsi:.0f}"
    else:
        signal, confidence, reason = "WAIT", "Low", "Signals are not aligned across VWAP, RSI and Supertrend"

    atm = round(spot / 50) * 50
    vix_quote = quote_by_token.get(str(vix["token"])) if vix else None
    return {
        "signal": signal,
        "confidence": confidence,
        "reason": reason,
        "strike": atm,
        "underlying": "NIFTY",
        "spot": spot,
        "india_vix": vix_quote.last if vix_quote else None,
        "indicators": {"rsi14": latest_rsi, "ema20": latest_ema20, "ema50": latest_ema50, "vwap": latest_vwap, "supertrend": "Green" if trend == 1 else "Red"},
        "as_of": datetime.now(tz=angel_candles.IST).isoformat(),
    }
