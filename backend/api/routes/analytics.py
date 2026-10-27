"""Analytics, AI coach and alerts routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.ai import coach
from backend.alerts import service as alerts
from backend.analytics import metrics
from backend.api.deps import CurrentUser, current_user, resolve_account_id
from backend.core.database import app_cursor
from backend.marketdata.quotes import quote_engine
from backend.marketdata import ticks
from backend.papertrading import engine as paper

router = APIRouter(prefix="/api/v1", tags=["analytics"])


class AlertModel(BaseModel):
    kind: str = Field(default="price")
    symbol: str | None = None
    condition: dict[str, Any]
    channels: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- analytics


@router.get("/analytics/performance")
def performance(mode: str = Query(default="paper", pattern="^(paper|backtesting)$"), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Full performance report for paper or persisted backtest trades."""
    account_id = resolve_account_id(user)
    table = "trades" if mode == "paper" else "backtest_trades"
    with app_cursor() as cursor:
        cursor.execute(
            f"SELECT *, net_pnl AS net_pnl FROM {table} WHERE account_id = ? AND exit_time IS NOT NULL AND net_pnl IS NOT NULL ORDER BY exit_time ASC LIMIT 5000",
            (account_id,),
        )
        trades = [dict(row) for row in cursor.fetchall()]

    equity = [paper.DEFAULT_PAPER_CAPITAL]
    for trade in trades:
        equity.append(equity[-1] + float(trade.get("net_pnl") or 0))

    return {
        "mode": mode,
        "has_data": bool(trades),
        "summary": metrics.summarise(equity, trades, period_seconds=86400),
        "daily": metrics.daily_pnl(trades, days=60),
        "monthly": metrics.monthly_pnl(trades),
        "drawdown_curve": metrics.drawdown_series(equity),
        "note": None if trades else f"No closed {mode} trades recorded yet.",
    }


@router.get("/analytics/strategies")
def strategy_analytics(mode: str = Query(default="paper", pattern="^(paper|backtesting)$"), user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    """Per-strategy performance for the selected analytics source."""
    account_id = resolve_account_id(user)
    table = "trades" if mode == "paper" else "backtest_trades"
    strategy_join = "LEFT JOIN strategies s ON s.id = t.strategy_run_id" if mode == "paper" else "LEFT JOIN strategies s ON s.id = t.strategy_id"
    strategy_column = "t.strategy_run_id" if mode == "paper" else "t.strategy_id"
    with app_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT s.id, s.name, {strategy_column} AS strategy_id, t.net_pnl, t.entry_time, t.exit_time
            FROM {table} t {strategy_join}
            WHERE t.account_id = ? AND t.exit_time IS NOT NULL AND t.net_pnl IS NOT NULL
            """,
            (account_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["name"] or row.get("strategy_id") or "manual"
        bucket = grouped.setdefault(key, {"strategy": key, "trades": [], "pnls": [], "names": set()})
        bucket["pnls"].append(float(row["net_pnl"] or 0))
        if row["name"]:
            bucket["names"].add(row["name"])

    results = []
    for key, bucket in grouped.items():
        pnls = bucket["pnls"]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        results.append({
            "strategy": key,
            "trades": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "win_rate_percent": metrics.win_rate(len(wins), len(pnls)),
            "profit_factor": metrics.profit_factor(pnls, pnls),
            "average_pnl": round(metrics.mean(pnls), 2),
        })
    return sorted(results, key=lambda item: item["net_pnl"], reverse=True)


@router.get("/analytics/heatmap")
def performance_heatmap(mode: str = Query(default="paper", pattern="^(paper|backtesting)$"), days: int = Query(default=60, ge=1, le=365),
                         user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Day-of-week / month P&L grid, for the dashboard heatmap."""
    account_id = resolve_account_id(user)
    with app_cursor() as cursor:
        table = "trades" if mode == "paper" else "backtest_trades"
        cursor.execute(
            f"SELECT exit_time, net_pnl FROM {table} WHERE account_id = ? AND exit_time IS NOT NULL AND net_pnl IS NOT NULL ORDER BY exit_time DESC LIMIT 5000",
            (account_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]

    from datetime import datetime, timezone

    grid: dict[str, dict[str, float]] = {}
    for row in rows:
        timestamp = row.get("exit_time")
        if not timestamp:
            continue
        moment = datetime.fromtimestamp(float(timestamp), timezone.utc)
        month = moment.strftime("%Y-%m")
        weekday = moment.strftime("%a")
        grid.setdefault(month, {})
        grid[month][weekday] = round(grid[month].get(weekday, 0.0) + float(row["net_pnl"] or 0), 2)
    return {"grid": grid, "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"], "days": days}


# ---------------------------------------------------------------- AI coach


@router.get("/coach")
def trading_coach(
    mode: str = Query(default="loss_analysis", pattern="^(loss_analysis|strategy_suggestions)$"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    return coach.review(resolve_account_id(user), mode=mode)


@router.get("/coach/report")
def coach_report(days: int = Query(default=90, ge=1, le=365), user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    return coach.performance_report(resolve_account_id(user), days=days)


# ------------------------------------------------------------------- alerts


@router.get("/alerts")
def list_alerts(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    return {
        "alerts": alerts.list_alerts(resolve_account_id(user)),
        "channels": alerts.channel_status(),
    }


@router.post("/alerts", status_code=status.HTTP_201_CREATED)
def create_alert(payload: AlertModel, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    try:
        return alerts.create_alert(
            account_id=resolve_account_id(user), kind=payload.kind, symbol=payload.symbol,
            condition=payload.condition, channels=payload.channels,
        )
    except alerts.AlertError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


@router.delete("/alerts/{alert_id}")
def delete_alert(alert_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    if not alerts.delete_alert(resolve_account_id(user), alert_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    return {"deleted": True, "id": alert_id}


@router.post("/alerts/{alert_id}/test")
def test_alert(alert_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Send the alert's message now, to verify channel configuration."""
    account_id = resolve_account_id(user)
    try:
        alert = alerts.get_alert(account_id, alert_id)
    except alerts.AlertError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    message = alerts.format_message(alert, 0.0)
    results = {}
    for channel in alert["channels"]:
        if channel == "email":
            results[channel] = alerts.send_email("AlphaTradePro test alert", message)
        else:
            results[channel] = alerts.CHANNELS[channel](message)
    return {"message": message, "delivery": results}
