"""Performance metrics for a trade or equity series.

Every metric states its own assumptions (period, risk-free rate, whether costs
are included) because a Sharpe ratio without them is meaningless.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

#: Annualisation factors.
TRADING_DAYS = 252
SECONDS_PER_DAY = 86400

RISK_FREE_ANNUAL = 0.065


def _returns(equity: Sequence[float]) -> list[float]:
    out: list[float] = []
    for index in range(1, len(equity)):
        previous = equity[index - 1]
        if previous and previous > 0:
            out.append((equity[index] - previous) / previous)
    return out


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def standard_deviation(values: Sequence[float], population: bool = False) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    divisor = len(values) if population else len(values) - 1
    return math.sqrt(sum((value - average) ** 2 for value in values) / divisor)


def cagr(start_equity: float, end_equity: float, days: float) -> float | None:
    """Compound annual growth rate as a percentage."""
    if start_equity <= 0 or days <= 0:
        return None
    years = days / 365.0
    if years <= 0 or end_equity <= 0:
        return None
    return round(((end_equity / start_equity) ** (1 / years) - 1) * 100, 4)


def annualisation_factor(points: int, period_seconds: float) -> float:
    """Scale a per-period volatility/return to an annual figure."""
    if points < 2 or period_seconds <= 0:
        return 0.0
    periods_per_year = (365.0 * SECONDS_PER_DAY) / period_seconds
    return math.sqrt(periods_per_year)


def sharpe_ratio(returns: Sequence[float], period_seconds: float, risk_free_annual: float = RISK_FREE_ANNUAL) -> float | None:
    """Annualised Sharpe ratio. Returns None when there is no dispersion."""
    if len(returns) < 2:
        return None
    deviation = standard_deviation(returns)
    if deviation == 0:
        return None
    scale = annualisation_factor(len(returns), period_seconds)
    risk_free_per_period = risk_free_annual / ((365.0 * SECONDS_PER_DAY) / period_seconds)
    return round((mean(returns) - risk_free_per_period) / deviation * scale, 4)


def sortino_ratio(returns: Sequence[float], period_seconds: float, risk_free_annual: float = RISK_FREE_ANNUAL) -> float | None:
    """Annualised Sortino ratio — only downside deviation is penalised."""
    if len(returns) < 2:
        return None
    downside = [value for value in returns if value < 0]
    if not downside:
        return None
    deviation = math.sqrt(sum(value * value for value in downside) / len(returns))
    if deviation == 0:
        return None
    scale = annualisation_factor(len(returns), period_seconds)
    risk_free_per_period = risk_free_annual / ((365.0 * SECONDS_PER_DAY) / period_seconds)
    return round((mean(returns) - risk_free_per_period) / deviation * scale, 4)


def profit_factor(wins: Sequence[float], losses: Sequence[float]) -> float | None:
    """Gross profit / gross loss. `None` when there are no losses to divide by."""
    gross_profit = sum(value for value in wins if value > 0)
    gross_loss = abs(sum(value for value in losses if value < 0))
    if gross_loss == 0:
        return None if gross_profit == 0 else float("inf")
    return round(gross_profit / gross_loss, 4)


def win_rate(wins: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(wins / total * 100, 2)


def max_drawdown(equity: Sequence[float]) -> dict[str, Any]:
    """Largest peak-to-trough decline, in currency and percent."""
    if not equity:
        return {"absolute": 0.0, "percent": 0.0, "peak_index": None, "trough_index": None}
    peak = equity[0]
    peak_index = 0
    worst_absolute = 0.0
    worst_percent = 0.0
    worst_peak_index = 0
    worst_trough_index = 0

    for index, value in enumerate(equity):
        if value > peak:
            peak = value
            peak_index = index
        decline = peak - value
        percent = (decline / peak * 100) if peak > 0 else 0.0
        if decline > worst_absolute:
            worst_absolute = decline
            worst_percent = percent
            worst_peak_index = peak_index
            worst_trough_index = index

    return {
        "absolute": round(worst_absolute, 2),
        "percent": round(worst_percent, 2),
        "peak_index": worst_peak_index,
        "trough_index": worst_trough_index,
    }


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """Percent below the running peak at each point."""
    out: list[float] = []
    peak = 0.0
    for value in equity:
        peak = max(peak, value)
        out.append(round((value - peak) / peak * 100, 4) if peak > 0 else 0.0)
    return out


def recovery_factor(equity: Sequence[float], period_seconds: float) -> float | None:
    """CAGR divided by max drawdown — return per unit of pain."""
    if len(equity) < 2:
        return None
    span_days = (len(equity) - 1) * period_seconds / SECONDS_PER_DAY
    growth = cagr(equity[0], equity[-1], span_days)
    drawdown = max_drawdown(equity)["percent"]
    if growth is None or drawdown == 0:
        return None
    return round(growth / drawdown, 4)


def expectancy(trades: Sequence[dict[str, Any]]) -> float | None:
    """Average P&L per trade, and the expectancy per unit risked."""
    pnls = [float(trade.get("net_pnl") or 0) for trade in trades if trade.get("net_pnl") is not None]
    if not pnls:
        return None
    average = mean(pnls)
    losers = [value for value in pnls if value < 0]
    if not losers:
        return round(average, 2)
    win_rate_fraction = len([v for v in pnls if v > 0]) / len(pnls)
    average_loss = abs(mean(losers))
    if average_loss == 0:
        return round(average, 2)
    return round((win_rate_fraction * average - (1 - win_rate_fraction) * average_loss), 2)


def summarise(
    equity: Sequence[float],
    trades: Sequence[dict[str, Any]],
    *,
    period_seconds: float = SECONDS_PER_DAY,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """The full performance report shown on the analytics dashboard."""
    if not equity:
        return {
            "has_data": False,
            "note": "No equity series to analyse.",
        }

    returns = _returns(equity)
    pnls = [float(trade.get("net_pnl") or 0) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    drawdown = max_drawdown(equity)
    span_days = max(1.0, (len(equity) - 1) * period_seconds / SECONDS_PER_DAY)

    return {
        "has_data": True,
        "initial_capital": round(initial_capital if initial_capital is not None else equity[0], 2),
        "final_equity": round(equity[-1], 2),
        "total_return": round(equity[-1] - equity[0], 2),
        "total_return_percent": round(((equity[-1] - equity[0]) / equity[0]) * 100, 4) if equity[0] > 0 else None,
        "cagr_percent": cagr(equity[0], equity[-1], span_days),
        "period_days": round(span_days, 2),
        "sharpe_ratio": sharpe_ratio(returns, period_seconds),
        "sortino_ratio": sortino_ratio(returns, period_seconds),
        "profit_factor": profit_factor(pnls, pnls),
        "recovery_factor": recovery_factor(equity, period_seconds),
        "max_drawdown": drawdown,
        "daily_volatility_percent": round(standard_deviation(returns) * 100, 4),
        "trades": {
            "count": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_percent": win_rate(len(wins), len(trades)),
            "gross_profit": round(sum(wins), 2),
            "gross_loss": round(sum(losses), 2),
            "net_profit": round(sum(pnls), 2),
            "average_win": round(mean(wins), 2) if wins else None,
            "average_loss": round(mean(losses), 2) if losses else None,
            "largest_win": round(max(pnls), 2) if pnls else None,
            "largest_loss": round(min(pnls), 2) if pnls else None,
            "expectancy": expectancy(trades),
        },
        "assumptions": {
            "risk_free_rate_annual": RISK_FREE_ANNUAL,
            "period_seconds": period_seconds,
            "annualisation": "trading-day basis (252 days) where bars are daily, else sqrt-time scaling",
            "returns_include_costs": True,
        },
    }


def daily_pnl(trades: Sequence[dict[str, Any]], days: int = 30) -> list[dict[str, Any]]:
    """P&L grouped by calendar day, for the daily/monthly views."""
    from datetime import datetime, timezone

    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        timestamp = trade.get("exit_time") or trade.get("entry_time")
        if not timestamp:
            continue
        try:
            day = datetime.fromtimestamp(float(timestamp), timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        bucket = buckets.setdefault(day, {"date": day, "trades": 0, "net_pnl": 0.0, "wins": 0})
        bucket["trades"] += 1
        pnl = float(trade.get("net_pnl") or 0)
        bucket["net_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    ordered = sorted(buckets.values(), key=lambda item: item["date"], reverse=True)[:max(1, days)]
    for bucket in ordered:
        bucket["net_pnl"] = round(bucket["net_pnl"], 2)
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"] * 100, 2) if bucket["trades"] else 0.0
    return ordered


def monthly_pnl(trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """P&L grouped by calendar month."""
    from datetime import datetime, timezone

    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        timestamp = trade.get("exit_time") or trade.get("entry_time")
        if not timestamp:
            continue
        try:
            month = datetime.fromtimestamp(float(timestamp), timezone.utc).strftime("%Y-%m")
        except (TypeError, ValueError, OSError):
            continue
        bucket = buckets.setdefault(month, {"month": month, "trades": 0, "net_pnl": 0.0, "wins": 0})
        bucket["trades"] += 1
        pnl = float(trade.get("net_pnl") or 0)
        bucket["net_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    ordered = sorted(buckets.values(), key=lambda item: item["month"], reverse=True)
    for bucket in ordered:
        bucket["net_pnl"] = round(bucket["net_pnl"], 2)
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"] * 100, 2) if bucket["trades"] else 0.0
    return ordered
