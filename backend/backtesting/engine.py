"""Event-driven backtester.

Runs a :mod:`backend.strategies.dsl` document over recorded candles, with
realistic Indian charges, configurable slippage, and no look-ahead: a signal
evaluated on bar *i* can only fill on bar *i+1* at that bar's open or worse.

**Data provenance matters here.** Kotak Neo publishes no historical candles, so
`candles` must come from the platform's own tick recording
(:mod:`backend.marketdata.ticks`). If the series is too short for the strategy's
warm-up, the backtest refuses to invent history and says so.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.analytics import metrics
from backend.execution.charges import compute as compute_charges
from backend.marketdata.ticks import INTERVAL_SECONDS
from backend.strategies import dsl

log = logging.getLogger("alphatrade.backtest")

#: Bar-to-bar slippage models, applied against the trader.
SLIPPAGE_MODELS = {
    "none": 0.0,
    "fixed_bps": 0.0005,      # 5 bps
    "moderate": 0.0015,       # 15 bps
    "high": 0.003,            # 30 bps
}


@dataclass
class BacktestTrade:
    symbol: str
    token: str
    exchange_segment: str
    side: str
    quantity: int
    entry_time: float
    entry_price: float
    exit_time: float | None = None
    exit_price: float | None = None
    reason: str = ""
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    pnl_percent: float = 0.0
    holding_bars: int = 0
    trailing_peak: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: (round(value, 4) if isinstance(value, float) else value) for key, value in self.__dict__.items()}


@dataclass
class BacktestResult:
    status: str
    metrics: dict[str, Any] = field(default_factory=dict)
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    equity_points: list[dict[str, Any]] = field(default_factory=list)
    drawdown_curve: list[float] = field(default_factory=list)
    bars: int = 0
    note: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "metrics": self.metrics,
            "trades": [trade.to_dict() for trade in self.trades],
            "equity_curve": [round(value, 2) for value in self.equity_curve],
            "equity_points": self.equity_points,
            "drawdown_curve": self.drawdown_curve,
            "bars": self.bars, "note": self.note,
            "warnings": self.warnings, "error": self.error,
        }


def _warmup_bars(definition: dict[str, Any]) -> int:
    """Longest indicator period the strategy needs before it can fire."""
    required = 30
    periods: list[int] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "all" in node:
                for child in node["all"]:
                    walk(child)
            elif "any" in node:
                for child in node["any"]:
                    walk(child)
            elif "not" in node:
                walk(node["not"])
            else:
                params = node.get("params") or {}
                for key in ("period", "fast", "slow", "signal", "length", "stoch_period", "rsi_period"):
                    value = params.get(key)
                    if isinstance(value, int):
                        periods.append(value)
                against = node.get("against")
                if isinstance(against, dict):
                    for value in (against.get("params") or {}).values():
                        if isinstance(value, int):
                            periods.append(value)

    walk(definition.get("entry"))
    walk(definition.get("exit"))
    if periods:
        required = max(required, max(periods) + 15)
    return required


def _slippage_rate(model: str | float) -> float:
    if isinstance(model, (int, float)):
        return float(model)
    return SLIPPAGE_MODELS.get(str(model).lower(), 0.0)


def _apply_slippage(price: float, side: str, rate: float) -> float:
    """Worse price for the trader: buys higher, sells lower."""
    if rate <= 0:
        return round(price, 2)
    multiplier = (1 + rate) if side == "B" else (1 - rate)
    return round(price * multiplier, 2)


def run(
    definition: dict[str, Any],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    initial_capital: float = 500_000.0,
    slippage: str | float = "moderate",
    timeframe: str | None = None,
) -> BacktestResult:
    """Backtest one strategy across a set of instruments.

    `candles_by_symbol` maps a display symbol to its OHLCV candles, oldest first.
    """
    validation = dsl.validate(definition)
    if not validation["valid"]:
        return BacktestResult(status="invalid", error="; ".join(validation["errors"]),
                              note="The strategy definition is invalid.")

    interval = timeframe or definition.get("timeframe") or "5m"
    if interval not in INTERVAL_SECONDS:
        return BacktestResult(
            status="invalid",
            error=f"Unsupported timeframe '{interval}'. Available: {', '.join(INTERVAL_SECONDS)}",
        )

    warnings: list[str] = []
    if not candles_by_symbol:
        return BacktestResult(
            status="no_data",
            note=(
                "No recorded candles for this strategy's universe. Kotak Neo does not provide "
                "historical data, so history begins when the market feed starts recording."
            ),
        )

    required = _warmup_bars(definition)
    tradable = {
        symbol: candles
        for symbol, candles in candles_by_symbol.items()
        if len(candles) >= required
    }
    skipped = {
        symbol: len(candles)
        for symbol, candles in candles_by_symbol.items()
        if len(candles) < required
    }
    if skipped:
        warnings.append(
            f"Skipped {len(skipped)} instrument(s) with fewer than {required} bars "
            f"(need warm-up): {skipped}"
        )
    if not tradable:
        return BacktestResult(
            status="no_data", warnings=warnings,
            note=(
                f"Every instrument has fewer than {required} recorded bars, which is the warm-up "
                f"this strategy needs. Keep the feed recording and try again."
            ),
        )

    slippage_rate = _slippage_rate(slippage)
    risk = definition.get("risk") or {}
    time_exit = int(risk.get("timeframe_exit_bars") or 0)

    equity = initial_capital
    trades: list[BacktestTrade] = []
    equity_points: list[dict[str, Any]] = []
    open_trade: BacktestTrade | None = None
    bars = 0

    for symbol, candles in tradable.items():
        instrument = (definition.get("universe") or [{}])[0]
        token = str(instrument.get("token") or "")
        segment = str(instrument.get("exchange_segment") or "nse_cm")
        product = str(instrument.get("product") or "CNC")
        cache = dsl.IndicatorCache(candles)

        for index in range(required, len(candles)):
            bars += 1
            bar = candles[index]
            open_price = bar.get("open")
            low = bar.get("low")
            high = bar.get("high")
            close = bar.get("close")
            if close is None:
                continue

            # 1. Manage an open position using this bar's range.
            if open_trade is not None:
                exited = False
                if open_trade.side == "B":
                    if open_trade.stop_price and low is not None and low <= open_trade.stop_price:
                        open_trade.exit_price = _apply_slippage(open_trade.stop_price, "S", slippage_rate)
                        open_trade.reason = "stop-loss"
                        exited = True
                    elif open_trade.target_price and high is not None and high >= open_trade.target_price:
                        open_trade.exit_price = _apply_slippage(open_trade.target_price, "S", slippage_rate)
                        open_trade.reason = "target"
                        exited = True
                else:
                    if open_trade.stop_price and high is not None and high >= open_trade.stop_price:
                        open_trade.exit_price = _apply_slippage(open_trade.stop_price, "B", slippage_rate)
                        open_trade.reason = "stop-loss"
                        exited = True
                    elif open_trade.target_price and low is not None and low <= open_trade.target_price:
                        open_trade.exit_price = _apply_slippage(open_trade.target_price, "B", slippage_rate)
                        open_trade.reason = "target"
                        exited = True

                if not exited:
                    # Trailing stop ratchets in the direction of profit.
                    trailing_pct = float(risk.get("trailing_stop_pct") or 0)
                    if trailing_pct and high is not None and low is not None:
                        if open_trade.side == "B":
                            peak = max(open_trade.trailing_peak or open_trade.entry_price, high)
                            open_trade.trailing_peak = peak
                            ratchet = peak * (1 - trailing_pct / 100)
                            if ratchet > (open_trade.stop_price or 0) and low <= ratchet:
                                open_trade.exit_price = _apply_slippage(ratchet, "S", slippage_rate)
                                open_trade.reason = "trailing-stop"
                                exited = True
                        else:
                            peak = min(open_trade.trailing_peak or open_trade.entry_price, low)
                            open_trade.trailing_peak = peak
                            ratchet = peak * (1 + trailing_pct / 100)
                            if ratchet < (open_trade.stop_price or 10**9) and high >= ratchet:
                                open_trade.exit_price = _apply_slippage(ratchet, "B", slippage_rate)
                                open_trade.reason = "trailing-stop"
                                exited = True

                if not exited:
                    if time_exit and open_trade.holding_bars >= time_exit:
                        open_trade.exit_price = _apply_slippage(
                            close, "S" if open_trade.side == "B" else "B", slippage_rate
                        )
                        open_trade.reason = "time-exit"
                        exited = True
                    elif dsl.evaluate(definition.get("exit"), cache, index):
                        open_trade.exit_price = _apply_slippage(
                            close, "S" if open_trade.side == "B" else "B", slippage_rate
                        )
                        open_trade.reason = "exit-signal"
                        exited = True

                if exited and open_trade.exit_price is not None:
                    _close_trade(open_trade, equity, segment, product, trades, slippage_rate)
                    equity = initial_capital + sum(trade.net_pnl for trade in trades)
                    open_trade = None

            # 2. Open a position on a confirmed signal, filling at the next open.
            if open_trade is None and dsl.evaluate(definition.get("entry"), cache, index):
                next_bar = candles[index + 1] if index + 1 < len(candles) else None
                if next_bar is None or next_bar.get("open") is None:
                    continue
                quantity = dsl.position_size(definition, equity, next_bar["open"])
                if quantity <= 0:
                    continue
                side = "B"
                entry_price = _apply_slippage(next_bar["open"], side, slippage_rate)
                stop, target = dsl.stop_and_target(entry_price, risk, side)
                open_trade = BacktestTrade(
                    symbol=symbol, token=token, exchange_segment=segment, side=side,
                    quantity=quantity, entry_time=next_bar.get("time", bar.get("time", 0)),
                    entry_price=entry_price, stop_price=stop, target_price=target,
                    trailing_peak=entry_price,
                )

            equity_points.append({
                "time": bar.get("time"), "equity": round(equity, 2),
                "symbol": symbol, "price": close,
            })

    # Close anything still open at the final close so results are complete.
    if open_trade is not None:
        last_symbol = list(tradable)[-1]
        last_close = tradable[last_symbol][-1].get("close")
        if last_close is not None:
            open_trade.exit_price = _apply_slippage(
                last_close, "S" if open_trade.side == "B" else "B", slippage_rate
            )
            open_trade.exit_time = tradable[last_symbol][-1].get("time")
            open_trade.reason = "end-of-data"
            _close_trade(open_trade, equity, segment, product, trades, slippage_rate)
            equity = initial_capital + sum(trade.net_pnl for trade in trades)

    equity_curve = [initial_capital] + [
        initial_capital + sum(trade.net_pnl for trade in trades[: index + 1]) for index in range(len(trades))
    ]

    period_seconds = INTERVAL_SECONDS[interval]
    summary = metrics.summarise(
        equity_curve, [trade.to_dict() for trade in trades],
        period_seconds=period_seconds, initial_capital=initial_capital,
    )
    summary["slippage_model"] = slippage
    summary["slippage_rate"] = slippage_rate
    summary["charges_included"] = True
    summary["warmup_bars"] = required

    if not trades:
        warnings.append(
            "The strategy produced no trades over this data. Check the thresholds, or wait for more bars."
        )

    return BacktestResult(
        status="ok", metrics=summary, trades=trades, equity_curve=equity_curve,
        equity_points=equity_points, drawdown_curve=metrics.drawdown_series(equity_curve),
        bars=bars, warnings=warnings,
        note=(
            f"Backtested {bars} bars across {len(tradable)} instrument(s) using candles recorded "
            f"by this platform. Kotak Neo publishes no historical data, so results cover only the "
            f"recorded period."
        ),
    )


def _close_trade(
    trade: BacktestTrade, equity: float, segment: str, product: str,
    trades: list[BacktestTrade], slippage_rate: float,
) -> None:
    """Realise P&L and attach both legs' charges."""
    if trade.exit_price is None:
        return
    direction = 1 if trade.side == "B" else -1
    trade.gross_pnl = (trade.exit_price - trade.entry_price) * trade.quantity * direction
    trade.pnl_percent = ((trade.exit_price - trade.entry_price) / trade.entry_price) * 100 * direction

    buy = compute_charges(side="B", segment=segment, product=product,
                          quantity=trade.quantity, price=trade.entry_price)
    sell = compute_charges(side="S", segment=segment, product=product,
                           quantity=trade.quantity, price=trade.exit_price)
    trade.charges = round(buy.total + sell.total, 4)
    trade.net_pnl = round(trade.gross_pnl - trade.charges, 4)
    trade.holding_bars = int((trade.exit_time or time.time()) - (trade.entry_time or time.time())) // 60
    trades.append(trade)
