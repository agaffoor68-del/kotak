"""Live, data-backed market analysis for the AI panel.

The scanner never invents prices. It reads Angel One candles, evaluates the
same auditable indicator functions used by strategies, and stores the latest
snapshot in the market database.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.angel import candles
from backend.api.deps import angel_call
from backend.core.database import market_cursor
from backend.marketdata import master
from backend.strategies.indicators import adx, ema, rsi, vwap

log = logging.getLogger("alphatrade.ai.live_scanner")
IST = timezone(timedelta(hours=5, minutes=30))
MARKETS = {
    "NIFTY 50": "99926000",
    "BANKNIFTY": "99919000",
}


def _latest(values: list[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None), None)


def _analysis(underlying: str, token: str, call: Callable[..., Any]) -> dict[str, Any]:
    segment = "nse_cm"
    trend_candles = candles.history(call, token, segment, "15m", limit=160, days=5)[0]
    momentum_candles = candles.history(call, token, segment, "5m", limit=300, days=3)[0]
    if len(trend_candles) < 60 or len(momentum_candles) < 60:
        # Warm up from Angel One history so the scanner does not wait for live ticks.
        try:
            candles.history(call, token, segment, "15m", limit=160, days=5)
            candles.history(call, token, segment, "5m", limit=300, days=3)
        except Exception:  # noqa: BLE001
            pass
        trend_candles = candles.history(call, token, segment, "15m", limit=160, days=5)[0]
        momentum_candles = candles.history(call, token, segment, "5m", limit=300, days=3)[0]
    if len(trend_candles) < 60 or len(momentum_candles) < 60:
        return {
            "underlying": underlying, "symbol": underlying, "signal": "WAIT", "strike": "ATM",
            "confidence": "0%", "reason": "Waiting for enough live candles to calculate indicators.",
            "trend": "Unknown", "timestamp": time.time(), "status": "warming_up",
        }

    trend_closes = [float(row["close"]) for row in trend_candles]
    momentum_closes = [float(row["close"]) for row in momentum_candles]
    trend_ema20 = _latest(ema(trend_closes, 20))
    trend_ema50 = _latest(ema(trend_closes, 50))
    momentum_rsi = _latest(rsi(momentum_closes, 14))
    current_vwap = _latest(vwap(momentum_candles))
    current_adx = _latest(adx(momentum_candles, 14)["adx"])
    price = momentum_closes[-1]

    if None in (trend_ema20, trend_ema50, momentum_rsi, current_vwap, current_adx):
        signal, reason, confidence = "WAIT", "Indicators are still warming up.", "0%"
    elif 45 <= momentum_rsi <= 55 or current_adx < 20:
        signal, reason, confidence = "WAIT", f"Sideways market: RSI {momentum_rsi:.0f}, ADX {current_adx:.0f}.", "45%"
    elif price > current_vwap and trend_ema20 > trend_ema50 and momentum_rsi > 55:
        signal, reason, confidence = "BUY CE", f"Price above VWAP, EMA20 above EMA50, RSI momentum at {momentum_rsi:.0f}.", "85%"
    elif price < current_vwap and trend_ema20 < trend_ema50 and momentum_rsi < 45:
        signal, reason, confidence = "BUY PE", f"Price below VWAP, EMA20 below EMA50, RSI momentum at {momentum_rsi:.0f}.", "85%"
    else:
        signal, reason, confidence = "WAIT", f"Signals are mixed: price {price:.2f}, VWAP {current_vwap:.2f}, RSI {momentum_rsi:.0f}.", "55%"

    return {
        "underlying": underlying, "symbol": underlying, "signal": signal, "strike": "ATM", "confidence": confidence,
        "reason": reason, "trend": "Bullish" if trend_ema20 > trend_ema50 else "Bearish",
        "price": round(price, 2), "vwap": round(current_vwap, 2), "rsi": round(momentum_rsi, 2),
        "adx": round(current_adx, 2), "ema20": round(trend_ema20, 2), "ema50": round(trend_ema50, 2),
        "timestamp": time.time(), "status": "live",
    }


def scan_once() -> list[dict[str, Any]]:
    resolver = getattr(__import__("backend.main", fromlist=["_angel_feed_resolver"]), "_angel_feed_resolver", None)
    if resolver is None:
        return []
    _, call = resolver()
    if call is None:
        return []
    results = []
    for underlying, token in MARKETS.items():
        try:
            result = _analysis(underlying, token, call)
        except Exception as error:  # broker outages must not kill the worker
            log.warning("Live analysis failed for %s: %s", underlying, error)
            result = {"underlying": underlying, "symbol": underlying, "signal": "WAIT", "strike": "ATM", "confidence": "0%", "reason": "Live market data is temporarily unavailable.", "trend": "Unknown", "timestamp": time.time(), "status": "unavailable"}
        with market_cursor() as cursor:
            cursor.execute("INSERT INTO ai_live_signals (underlying, payload, created_at) VALUES (?, ?, ?)", (underlying, __import__("json").dumps(result), result["timestamp"]))
        results.append(result)
    return results


class LiveScanner:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ai-live-scanner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            scan_once()
            self._stop.wait(max(5, 60 - (time.time() % 60)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


live_scanner = LiveScanner()


def latest(underlying: str | None = None) -> dict[str, Any] | None:
    import json
    with market_cursor() as cursor:
        if underlying:
            cursor.execute("SELECT payload FROM ai_live_signals WHERE underlying = ? ORDER BY created_at DESC LIMIT 1", (underlying,))
        else:
            cursor.execute("SELECT payload FROM ai_live_signals ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
    return json.loads(row["payload"]) if row else None
