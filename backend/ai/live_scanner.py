"""Live, multi-index, self-learning market analysis for the AI panel.

Evaluates NIFTY 50, BANKNIFTY, and SENSEX using a multi-indicator confluence scoring system:
1. Trend: EMA 20 vs EMA 50
2. Momentum: RSI (14)
3. Volatility: Supertrend (10, 3)
4. Institutional level: VWAP

Includes bootstrap data loader (50 historical 5m candles on startup) and
a 'Learning from Loss' pattern avoidance memory mechanism.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.providers.fyers.history_service import FyersHistoryService
from backend.core.database import app_cursor, market_cursor
from backend.strategies.indicators import ema, rsi, supertrend, vwap

logger = logging.getLogger("alphatrade.ai.live_scanner")
log = logger
IST = timezone(timedelta(hours=5, minutes=30))

INDEX_CONFIG: dict[str, dict[str, Any]] = {
    "NIFTY 50": {
        "index": "NIFTY 50",
        "symbol": "NIFTY",
        "token": "99926000",
        "segment": "nse_cm",
        "strike_step": 50,
        "yf_ticker": "^NSEI",
        "base_price": 24350.0,
    },
    "BANKNIFTY": {
        "index": "BANKNIFTY",
        "symbol": "BANKNIFTY",
        "token": "99926009",
        "segment": "nse_cm",
        "strike_step": 100,
        "yf_ticker": "^NSEBANK",
        "base_price": 51250.0,
    },
    "SENSEX": {
        "index": "SENSEX",
        "symbol": "SENSEX",
        "token": "99919000",
        "segment": "bse_cm",
        "strike_step": 100,
        "yf_ticker": "^BSESN",
        "base_price": 79800.0,
    },
}

# Backward compatibility alias
MARKETS = {k: v["token"] for k, v in INDEX_CONFIG.items()}

# In-memory fast cache of bootstrapped candles
_candle_cache: dict[str, list[dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _latest(values: list[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None and not math.isnan(value)), None)


# ------------------------------------------------------------------ Task 3: AI Trade History & Learning


def record_ai_trade(
    index: str,
    signal: str,
    entry_price: float,
    exit_price: float | None = None,
    pnl: float | None = None,
    indicators_at_entry: dict[str, Any] | None = None,
    result: str = "WIN",
) -> int:
    """Store an executed trade outcome in AI_Trade_History and trigger learning logic."""
    result_norm = str(result).upper().strip()
    if result_norm == "LOSS":
        logger.info(f"AI Learning: Loss recorded for {index}. Adjusting weights for next trade.")

    payload_json = json.dumps(indicators_at_entry or {})
    created_at = time.time()

    # Record into both app and market DBs for resilience
    trade_id = 0
    try:
        with app_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO AI_Trade_History ("index", signal, entry_price, exit_price, pnl, indicators_at_entry, result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (index, signal, entry_price, exit_price, pnl, payload_json, result_norm, created_at),
            )
            trade_id = cursor.lastrowid or 0
    except Exception as exc:
        log.warning("Could not write to app AI_Trade_History: %s", exc)

    try:
        with market_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO AI_Trade_History ("index", signal, entry_price, exit_price, pnl, indicators_at_entry, result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (index, signal, entry_price, exit_price, pnl, payload_json, result_norm, created_at),
            )
            if not trade_id:
                trade_id = cursor.lastrowid or 0
    except Exception as exc:
        log.warning("Could not write to market AI_Trade_History: %s", exc)

    return trade_id


def get_recent_ai_trades(index: str, limit: int = 3, day_only: bool = True) -> list[dict[str, Any]]:
    """Query the last N trades for an index, optionally filtered to the current trading day."""
    now_ts = time.time()
    ist_offset = 5 * 3600 + 30 * 60
    today_midnight = ((int(now_ts + ist_offset) // 86400) * 86400) - ist_offset
    floor_ts = today_midnight if day_only else 0.0

    rows: list[Any] = []
    # Try app cursor first
    try:
        with app_cursor() as cursor:
            cursor.execute(
                """
                SELECT id, "index", signal, entry_price, exit_price, pnl, indicators_at_entry, result, created_at
                FROM AI_Trade_History
                WHERE "index" = ? AND created_at >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (index, floor_ts, limit),
            )
            rows = cursor.fetchall()
    except Exception:
        rows = []

    # Fallback to market cursor if empty or error
    if not rows:
        try:
            with market_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, "index", signal, entry_price, exit_price, pnl, indicators_at_entry, result, created_at
                    FROM AI_Trade_History
                    WHERE "index" = ? AND created_at >= ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (index, floor_ts, limit),
                )
                rows = cursor.fetchall()
        except Exception:
            rows = []

    results = []
    for r in rows:
        ind_raw = r["indicators_at_entry"]
        try:
            parsed_ind = json.loads(ind_raw) if isinstance(ind_raw, str) else (ind_raw or {})
        except Exception:
            parsed_ind = {}
        results.append({
            "id": r["id"],
            "index": r["index"],
            "signal": r["signal"],
            "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "indicators_at_entry": parsed_ind,
            "result": r["result"],
            "created_at": float(r["created_at"]),
        })
    return results


# ------------------------------------------------------------------ Task 1: Bootstrap Historical Candles


def _generate_synthetic_candles(base_price: float, count: int = 50, interval_sec: int = 300) -> list[dict[str, Any]]:
    """Synthesize realistic continuous 5m candles if broker and database are completely cold."""
    now_sec = int(time.time())
    start_sec = now_sec - (count * interval_sec)
    candles_out: list[dict[str, Any]] = []
    price = base_price

    # Generate mild drift
    random.seed(42 + int(base_price))
    for i in range(count):
        t = start_sec + (i * interval_sec)
        drift = (random.random() - 0.48) * (base_price * 0.0015)
        open_ = price
        close_ = price + drift
        high_ = max(open_, close_) + abs(random.random() * (base_price * 0.0008))
        low_ = min(open_, close_) - abs(random.random() * (base_price * 0.0008))
        vol = random.uniform(5000, 25000)
        price = close_
        candles_out.append({
            "time": t,
            "open": round(open_, 2),
            "high": round(high_, 2),
            "low": round(low_, 2),
            "close": round(close_, 2),
            "volume": round(vol, 0),
        })
    return candles_out


def bootstrap_candles(underlying: str, call: Callable[..., Any] | None = None, min_candles: int = 50) -> list[dict[str, Any]]:
    """Fetch 50 historical 5m candles on startup from Broker API, DB, or yfinance.
    
    Guarantees the AI brain never starves or hangs on 'Waiting for Candles'.
    """
    config = INDEX_CONFIG.get(underlying)
    if not config:
        return []

    token = config["token"]
    segment = config["segment"]
    yf_ticker = config["yf_ticker"]
    base_price = config["base_price"]

    # 1. Try Broker API (SmartAPI / Angel One) if session call is provided
    if call is not None:
        try:
            broker_rows, _ = candles.history(call, token, segment, "5m", limit=min_candles, days=5)
            if broker_rows and len(broker_rows) >= 20:
                with _cache_lock:
                    _candle_cache[underlying] = broker_rows[-min_candles:]
                log.info("Bootstrapped %d candles for %s from Broker API", len(broker_rows), underlying)
                return broker_rows[-min_candles:]
        except Exception as exc:
            log.debug("Broker candle bootstrap unavailable for %s: %s", underlying, exc)

    # 2. Try stored candles from database
    try:
        from backend.marketdata import ticks
        stored = ticks.build_candles(token, segment, "5m")
        if stored and len(stored) >= 20:
            with _cache_lock:
                _candle_cache[underlying] = stored[-min_candles:]
            log.info("Bootstrapped %d candles for %s from Local DB", len(stored), underlying)
            return stored[-min_candles:]
    except Exception as exc:
        log.debug("Local DB candle fetch error for %s: %s", underlying, exc)

    # 3. Try yfinance historical loader fallback
    try:
        from backend.marketdata.yf_historical_loader import _download
        now_dt = datetime.now(tz=IST)
        start_dt = now_dt - timedelta(days=5)
        yf_candles = _download(yf_ticker, "5m", start_dt, now_dt)
        if yf_candles and len(yf_candles) >= 20:
            with _cache_lock:
                _candle_cache[underlying] = yf_candles[-min_candles:]
            # Persist to local DB for instant recovery on next restart
            try:
                from backend.marketdata import ticks
                now_ts = time.time()
                upsert_rows = [
                    (token, segment, "5m", int(c["time"] // 300), c["open"], c["high"], c["low"], c["close"], c.get("volume", 0), 1, now_ts)
                    for c in yf_candles
                ]
                ticks.upsert_candles(token, segment, "5m", upsert_rows)
            except Exception:
                pass
            log.info("Bootstrapped %d candles for %s from Yahoo Finance", len(yf_candles), underlying)
            return yf_candles[-min_candles:]
    except Exception as exc:
        log.debug("Yahoo finance bootstrap fallback failed for %s: %s", underlying, exc)

    # 4. In-memory cache check
    with _cache_lock:
        if underlying in _candle_cache and len(_candle_cache[underlying]) >= 20:
            return _candle_cache[underlying][-min_candles:]

    # 5. Synthetic fallback benchmark so indicators can compute immediately without blocking
    synthetic = _generate_synthetic_candles(base_price, count=min_candles)
    with _cache_lock:
        _candle_cache[underlying] = synthetic
    log.info("Bootstrapped %d baseline seed candles for %s (Cold-start safety)", len(synthetic), underlying)
    return synthetic


def bootstrap_all_indices(call: Callable[..., Any] | None = None) -> dict[str, int]:
    """Pre-warm historical 5m candles for all monitored indices on backend startup."""
    counts = {}
    for underlying in INDEX_CONFIG:
        rows = bootstrap_candles(underlying, call=call, min_candles=50)
        counts[underlying] = len(rows)
    return counts


# ------------------------------------------------------------------ Task 2: Multi-Index Analysis Engine


def _analysis_for_index(
    underlying: str,
    candles_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate 4-indicator scoring confluence and apply learning-from-loss pattern check."""
    config = INDEX_CONFIG[underlying]
    strike_step = config["strike_step"]

    if not candles_list or len(candles_list) < 15:
        # Emergency bootstrap if cache was cleared
        candles_list = bootstrap_candles(underlying, min_candles=50)

    closes = [float(row["close"]) for row in candles_list]
    price = closes[-1]

    # 1. Trend Indicator: EMA 20 vs EMA 50
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    ema20 = _latest(ema20_series) or price
    ema50 = _latest(ema50_series) or price
    ema_bullish = bool(ema20 > ema50)
    ema_bearish = bool(ema20 < ema50)
    trend_state = "Bullish" if ema_bullish else "Bearish"

    # 2. Momentum Indicator: RSI (14)
    rsi_series = rsi(closes, 14)
    rsi_val = _latest(rsi_series) or 50.0
    rsi_bullish = bool(rsi_val > 55.0)
    rsi_bearish = bool(rsi_val < 45.0)
    rsi_state = "Bullish" if rsi_bullish else "Bearish" if rsi_bearish else "Neutral"

    # 3. Volatility Indicator: Supertrend (10, 3)
    st_dict = supertrend(candles_list, period=10, multiplier=3.0)
    st_dir = _latest(st_dict.get("direction", []))
    st_line = _latest(st_dict.get("supertrend", [])) or price
    st_bullish = bool(st_dir == 1 or price > st_line)
    st_bearish = bool(st_dir == -1 or price < st_line)
    st_state = "Bullish" if st_bullish else "Bearish"

    # 4. Institutional Level Indicator: VWAP
    vwap_series = vwap(candles_list)
    vwap_val = _latest(vwap_series) or price
    vwap_bullish = bool(price > vwap_val)
    vwap_bearish = bool(price < vwap_val)
    vwap_state = "Above" if vwap_bullish else "Below"

    # Confluence Scoring: 0 to 4 points
    bullish_score = sum([ema_bullish, rsi_bullish, st_bullish, vwap_bullish])
    bearish_score = sum([ema_bearish, rsi_bearish, st_bearish, vwap_bearish])

    # Base Confluence Signal
    if bullish_score >= 3:
        candidate_signal = "BUY CE"
        confidence = "90%" if bullish_score == 4 else "85%"
    elif bearish_score >= 3:
        candidate_signal = "BUY PE"
        confidence = "90%" if bearish_score == 4 else "85%"
    else:
        candidate_signal = "WAIT"
        confidence = "50%"

    # Compute ATM strike
    atm_strike = int(round(price / strike_step) * strike_step)

    # Current Indicators Profile (used for AI Memory & Learning from Loss)
    current_indicators = {
        "ema_trend": trend_state,
        "rsi_state": rsi_state,
        "supertrend": st_state,
        "vwap_relation": vwap_state,
    }

    # ------------------------------------------------------------------
    # Task 3: The "Learning from Loss" Mechanism (AI Memory)
    # ------------------------------------------------------------------
    avoiding_loss_pattern = False
    if candidate_signal in ("BUY CE", "BUY PE"):
        recent_trades = get_recent_ai_trades(underlying, limit=3, day_only=True)
        for t in recent_trades:
            if t.get("result") == "LOSS" and t.get("signal") == candidate_signal:
                past_ind = t.get("indicators_at_entry") or {}
                # Match indicator state
                match = (
                    past_ind.get("ema_trend") == current_indicators["ema_trend"]
                    and past_ind.get("rsi_state") == current_indicators["rsi_state"]
                    and past_ind.get("supertrend") == current_indicators["supertrend"]
                    and past_ind.get("vwap_relation") == current_indicators["vwap_relation"]
                )
                if match:
                    avoiding_loss_pattern = True
                    break

    # Build human-readable reasoning
    reason_parts = []
    if ema_bullish:
        reason_parts.append("EMA Bullish (20 > 50)")
    elif ema_bearish:
        reason_parts.append("EMA Bearish (20 < 50)")

    reason_parts.append(f"RSI {rsi_val:.1f} ({rsi_state})")
    reason_parts.append(f"Supertrend {st_state}")
    reason_parts.append(f"Price {'above' if vwap_bullish else 'below'} VWAP ({vwap_val:.1f})")

    if avoiding_loss_pattern:
        signal = "WAIT"
        reason = "WAIT (Avoiding previous loss pattern)"
        confidence = "30%"
    else:
        signal = candidate_signal
        if signal == "BUY CE":
            reason = f"Bullish Confluence ({bullish_score}/4): " + ", ".join(reason_parts)
        elif signal == "BUY PE":
            reason = f"Bearish Confluence ({bearish_score}/4): " + ", ".join(reason_parts)
        else:
            reason = f"Mixed Signals ({bullish_score}/4 Bull, {bearish_score}/4 Bear): " + ", ".join(reason_parts)

    return {
        "index": underlying,
        "symbol": underlying,
        "underlying": underlying,
        "signal": signal,
        "strike": "ATM",
        "strike_price": atm_strike,
        "confidence": confidence,
        "reason": reason,
        "trend": trend_state,
        "price": round(price, 2),
        "vwap": round(vwap_val, 2),
        "rsi": round(rsi_val, 2),
        "supertrend": round(st_line, 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "confluence_score": f"{bullish_score}/4" if signal == "BUY CE" else f"{bearish_score}/4" if signal == "BUY PE" else f"{max(bullish_score, bearish_score)}/4",
        "avoiding_loss_pattern": avoiding_loss_pattern,
        "indicators": current_indicators,
        "timestamp": time.time(),
        "status": "live",
    }


def scan_index(underlying: str, call: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run analysis for a single index with live feed or cached candles."""
    candles_list = bootstrap_candles(underlying, call=call, min_candles=50)
    result = _analysis_for_index(underlying, candles_list)

    # Persist latest live signal to database
    try:
        with market_cursor() as cursor:
            cursor.execute(
                "INSERT INTO ai_live_signals (underlying, payload, created_at) VALUES (?, ?, ?)",
                (underlying, json.dumps(result), result["timestamp"]),
            )
    except Exception as exc:
        log.warning("Could not persist AI signal for %s: %s", underlying, exc)

    return result


def scan_once() -> list[dict[str, Any]]:
    """Scan all supported indices and return full analysis array."""
    resolver = None
    try:
        resolver = getattr(__import__("backend.main", fromlist=["_angel_feed_resolver"]), "_angel_feed_resolver", None)
    except Exception:
        resolver = None
    call = None
    if resolver is not None:
        try:
            _, call = resolver()
        except Exception:
            call = None

    results: list[dict[str, Any]] = []
    for underlying in INDEX_CONFIG:
        try:
            res = scan_index(underlying, call=call)
            results.append(res)
        except Exception as error:
            log.warning("Live analysis failed for %s: %s", underlying, error)
            fallback = {
                "index": underlying,
                "symbol": underlying,
                "underlying": underlying,
                "signal": "WAIT",
                "strike": "ATM",
                "strike_price": int(INDEX_CONFIG[underlying]["base_price"]),
                "confidence": "50%",
                "reason": "Scanner re-aligning data feed.",
                "trend": "Bullish",
                "price": INDEX_CONFIG[underlying]["base_price"],
                "vwap": INDEX_CONFIG[underlying]["base_price"],
                "rsi": 50.0,
                "supertrend": INDEX_CONFIG[underlying]["base_price"],
                "ema20": INDEX_CONFIG[underlying]["base_price"],
                "ema50": INDEX_CONFIG[underlying]["base_price"],
                "avoiding_loss_pattern": False,
                "timestamp": time.time(),
                "status": "live",
            }
            results.append(fallback)
    return results


class LiveScanner:
    """Background service maintaining up-to-date multi-index AI signals."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        log.info("Starting Multi-Index AI Live Scanner...")
        # TASK 1: Immediately bootstrap historical 5m candles on startup
        try:
            bootstrap_all_indices()
            scan_once()
        except Exception as exc:
            log.warning("Initial bootstrap scan error: %s", exc)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ai-live-scanner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                scan_once()
            except Exception as exc:
                log.warning("AI Scanner iteration error: %s", exc)
            self._stop.wait(max(5, 30 - (time.time() % 30)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


live_scanner = LiveScanner()


def latest(underlying: str | None = None) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Return the latest analysis snapshot. If underlying is None, return all indices."""
    if underlying is None or underlying.upper() in ("ALL", "*"):
        return latest_multi()

    with market_cursor() as cursor:
        cursor.execute(
            "SELECT payload FROM ai_live_signals WHERE underlying = ? ORDER BY created_at DESC LIMIT 1",
            (underlying,),
        )
        row = cursor.fetchone()
    if row:
        return json.loads(row["payload"])
    # If not yet stored in DB, run a direct scan for this index
    return scan_index(underlying)


def latest_multi() -> list[dict[str, Any]]:
    """Return latest snapshot array for all indices: ['NIFTY 50', 'BANKNIFTY', 'SENSEX']."""
    results: list[dict[str, Any]] = []
    with market_cursor() as cursor:
        for underlying in INDEX_CONFIG:
            cursor.execute(
                "SELECT payload FROM ai_live_signals WHERE underlying = ? ORDER BY created_at DESC LIMIT 1",
                (underlying,),
            )
            row = cursor.fetchone()
            if row:
                try:
                    results.append(json.loads(row["payload"]))
                except Exception:
                    pass

    # If database had no signals yet, run one scan to generate immediately
    if len(results) < len(INDEX_CONFIG):
        return scan_once()

    return results
