"""Institutional-grade options strategy, backtest, and execution primitives.

The module is intentionally dependency-light: market data and broker adapters are
injected, which makes the exact same risk rules usable in replay and live mode.
All timestamps are interpreted as Asia/Kolkata unless a timezone-aware datetime
is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from math import floor
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from backend.execution.charges import compute as compute_charges
from backend.marketdata import master
from backend.strategies.indicators import adx, atr_series

IST = ZoneInfo("Asia/Kolkata")


def _ist(value: datetime | date) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, IST)
    return value if value.tzinfo else value.replace(tzinfo=IST)


def calculate_atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    """Return the latest Wilder ATR, or ``None`` until the series is warm."""
    values = atr_series(candles, period)
    return values[-1] if values and values[-1] is not None else None


def apply_slippage(price: float, side: str, points: float = 1.0) -> float:
    """Apply fixed point slippage against the trader (buy higher, sell lower)."""
    return round(price + points if side.upper() in {"B", "BUY"} else price - points, 2)


def position_size(capital: float, risk_percent: float, entry: float, stop: float,
                  lot_size: int = 1, max_lots: int | None = None) -> int:
    """Size whole lots from rupee risk, never exceeding the configured risk."""
    risk_per_unit = abs(entry - stop)
    if capital <= 0 or risk_per_unit <= 0 or lot_size <= 0:
        return 0
    lots = floor((capital * risk_percent / 100) / (risk_per_unit * lot_size))
    if max_lots is not None:
        lots = min(lots, max_lots)
    return max(0, lots * lot_size)


@dataclass(frozen=True)
class EventWindow:
    start: datetime
    end: datetime


@dataclass
class InstitutionalConfig:
    capital: float
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_trades_per_day: int = 3
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 30)
    expiry_entry_end: time = time(13, 30)
    expiry_exit: time = time(14, 30)
    hard_exit: time = time(15, 15)
    vix_max: float = 20.0
    adx_min: float = 20.0
    atr_period: int = 14
    atr_multiplier: float = 1.5
    premium_stop_pct: float = 1.0
    slippage_points: float = 1.0
    lot_size: int = 1
    is_paper_trade: bool = True
    no_trade_windows: list[EventWindow] = field(default_factory=list)


class OptionsStrategyEngine:
    """Pure decision/risk layer. It does not place orders or access a broker."""
    def __init__(self, config: InstitutionalConfig):
        self.config = config
        self.trades_today = 0
        self.daily_pnl = 0.0

    def event_blocked(self, now: datetime) -> bool:
        current = _ist(now)
        return any(window.start <= current <= window.end for window in self.config.no_trade_windows)

    def can_enter(self, now: datetime, *, expiry_date: date | None, india_vix: float,
                  adx_value: float) -> tuple[bool, str]:
        current = _ist(now)
        if self.daily_pnl <= -(self.config.capital * self.config.max_daily_loss_pct / 100):
            return False, "daily-loss-circuit-breaker"
        if self.trades_today >= self.config.max_trades_per_day:
            return False, "max-trades-per-day"
        if self.event_blocked(current):
            return False, "high-impact-event-window"
        if not (self.config.entry_start <= current.time() <= self.config.entry_end):
            return False, "outside-entry-window"
        if expiry_date and current.date() == expiry_date and current.time() > self.config.expiry_entry_end:
            return False, "expiry-day-entry-cutoff"
        if india_vix > self.config.vix_max:
            return False, "india-vix-too-high"
        if adx_value < self.config.adx_min:
            return False, "adx-sideways-market"
        return True, "approved"

    def force_exit(self, now: datetime, expiry_date: date | None) -> bool:
        current = _ist(now)
        return current.time() >= (self.config.expiry_exit if expiry_date == current.date() else self.config.hard_exit)

    def stop_and_target(self, entry: float, atr: float, side: str) -> tuple[float, float, float]:
        """Use the tighter of 1.5 ATR and 1% premium; target is exactly 2R."""
        distance = min(atr * self.config.atr_multiplier, entry * self.config.premium_stop_pct / 100)
        if distance <= 0:
            raise ValueError("stop distance must be positive")
        sign = 1 if side.upper() in {"B", "BUY"} else -1
        stop = round(entry - sign * distance, 2)
        target = round(entry + sign * distance * 2, 2)
        return stop, target, distance

    def trailing_stop(self, side: str, current_stop: float, entry: float, last_price: float,
                      risk_distance: float, candle_low: float, candle_high: float) -> float:
        sign = 1 if side.upper() in {"B", "BUY"} else -1
        reached_r = (last_price - entry) * sign >= risk_distance
        if not reached_r:
            return current_stop
        breakeven = entry
        candle_stop = candle_low if sign == 1 else candle_high
        return round(max(current_stop, breakeven, candle_stop) if sign == 1 else
                     min(current_stop, breakeven, candle_stop), 2)

    def select_contract(self, spot: float, underlying: str, expiry: str, option_type: str,
                       strike_mode: str = "ATM", offset: int = 0,
                       exchange_segment: str = "nse_fo") -> dict[str, Any]:
        chain = master.option_contracts(underlying, exchange_segment, expiry)
        rows = chain.get("rows", {}).get(expiry, {})
        strikes = sorted(float(strike) for strike in rows)
        if not strikes:
            raise LookupError(f"No option contracts for {underlying} {expiry}")
        atm = min(strikes, key=lambda strike: abs(strike - spot))
        step = strikes[1] - strikes[0] if len(strikes) > 1 else 0
        mode = strike_mode.upper()
        steps = offset if offset else (1 if mode in {"ITM", "OTM"} else 0)
        direction = 1 if option_type.upper() == "CE" else -1
        if mode == "ITM": direction *= -1
        elif mode == "OTM": direction *= 1
        strike = atm + direction * steps * step
        leg = rows.get(strike) or next((v for k, v in rows.items() if float(k) == strike), None)
        leg = (leg or {}).get("call" if option_type.upper() == "CE" else "put") or {}
        if not leg.get("token"):
            raise LookupError(f"Missing {option_type} token for strike {strike}")
        return {"instrument_token": str(leg["token"]), "trading_symbol": leg["trading_symbol"],
                "strike": strike, "expiry": expiry, "option_type": option_type.upper(),
                "lot_size": self.config.lot_size}


class BacktestEngine:
    def __init__(self, strategy: OptionsStrategyEngine, *, charge_product: str = "NRML",
                 exchange_segment: str = "nse_fo"):
        self.strategy, self.product, self.segment = strategy, charge_product, exchange_segment

    def round_trip_cost(self, quantity: int, entry: float, exit: float) -> float:
        buy = compute_charges(side="B", segment=self.segment, product=self.product,
                              quantity=quantity, price=entry, is_option=True)
        sell = compute_charges(side="S", segment=self.segment, product=self.product,
                               quantity=quantity, price=exit, is_option=True)
        return buy.total + sell.total

    def execute_fill(self, price: float, side: str) -> float:
        return apply_slippage(price, side, self.strategy.config.slippage_points)


class ExecutionEngine:
    """Kotak adapter with paper mode and a mandatory pre-trade margin check."""
    def __init__(self, strategy: OptionsStrategyEngine, broker_call: Callable[..., Any],
                 telegram_alert: Callable[[str], None] | None = None):
        self.strategy, self.broker_call, self.telegram_alert = strategy, broker_call, telegram_alert

    def place(self, order: dict[str, Any], *, available_margin: float | None = None) -> dict[str, Any]:
        required = float(order.get("quantity", 0)) * float(order.get("price", 0))
        if available_margin is None:
            margin = self.broker_call("limits")
            available_margin = float((margin.get("availableMargin") or margin.get("available_cash") or
                                      margin.get("data", {}).get("availableMargin") or 0)) if isinstance(margin, dict) else 0
        if available_margin < required:
            message = f"Insufficient Kotak margin: required ₹{required:.2f}, available ₹{available_margin:.2f}"
            if self.telegram_alert: self.telegram_alert(message)
            return {"status": "skipped", "reason": "insufficient-margin", "required": required}
        if self.strategy.config.is_paper_trade:
            return {"status": "paper-filled", "fill_price": order.get("ltp", order.get("price")), "order": order}
        return {"status": "live", "response": self.broker_call("place_order", **order)}
