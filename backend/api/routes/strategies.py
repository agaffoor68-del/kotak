"""Strategy builder, backtesting and algo-run routes."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, current_user, require_trader, resolve_account_id
from backend.backtesting import engine
from backend.core.config import settings
from backend.core.database import app_cursor, now, row_to_dict
from backend.execution import algo
from backend.marketdata import master, ticks
from backend.strategies import dsl
from backend.strategies.dsl import COMPARATORS, CROSS_OPS, PRICE_FIELDS
from backend.strategies.indicators import available
from backend.strategies.templates import TEMPLATES

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


class StrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    definition: dict[str, Any]
    is_template: bool = False


class BacktestRequest(BaseModel):
    strategy_id: str | None = None
    definition: dict[str, Any] | None = None
    initial_capital: float = Field(default=500_000.0, gt=0)
    slippage: str = Field(default="moderate")
    timeframe: str | None = None


class StartRunRequest(BaseModel):
    mode: str = Field(default="paper", pattern="^(paper|live)$")


@router.get("/indicators")
def list_indicators(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Everything the no-code builder can offer."""
    return {
        "indicators": available(),
        "price_fields": list(PRICE_FIELDS),
        "comparators": sorted(COMPARATORS),
        "cross_comparators": sorted(CROSS_OPS),
        "logical": ["all", "any", "not"],
        "timeframes": ticks.supported_intervals(),
    }


@router.get("/templates")
def list_templates(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, description, kind, definition FROM strategies WHERE is_template = 1 ORDER BY name"
        )
        rows = [row_to_dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["valid"] = dsl.validate(row["definition"])["valid"]
    return rows


@router.post("/validate")
def validate_strategy(definition: dict[str, Any], user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Validate a strategy document, as the builder types."""
    return dsl.validate(definition)


@router.post("/explain")
def explain_strategy(payload: dict[str, Any], user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Evaluate a rule tree against the most recent recorded candles."""
    definition = payload.get("definition") or payload
    node = payload.get("node")
    token = str(payload.get("token") or "")
    segment = str(payload.get("exchange_segment") or "nse_cm")
    interval = str(payload.get("interval") or definition.get("timeframe") or "5m")

    candles = ticks.history(token, segment, interval, 400) if token else []
    if not candles:
        return {
            "available": False,
            "note": (
                "No recorded candles for this instrument yet. Kotak Neo publishes no historical data, "
                "so the tape builds from when the feed starts recording."
            ),
        }
    cache = dsl.IndicatorCache(candles)
    return {
        "available": True,
        "bar_index": len(candles) - 1,
        "result": dsl.explain(node or definition.get("entry"), cache, len(candles) - 1),
        "exit_result": dsl.explain(definition.get("exit"), cache, len(candles) - 1),
    }


@router.get("")
def list_strategies(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    account_id = resolve_account_id(user)
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM strategies WHERE account_id = ? AND is_template = 0 ORDER BY updated_at DESC",
            (account_id,),
        )
        return [row_to_dict(row) for row in cursor.fetchall()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(payload: StrategyRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    validation = dsl.validate(payload.definition)
    if not validation["valid"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": validation["errors"]})
    account_id = resolve_account_id(user)
    strategy_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO strategies (id, account_id, name, description, kind, definition, is_template, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (strategy_id, account_id, payload.name, payload.description,
             payload.definition.get("kind", "rule"), json.dumps(payload.definition),
             1 if payload.is_template else 0, now(), now()),
        )
    return {"id": strategy_id, "name": payload.name}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        row = row_to_dict(cursor.fetchone())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    row["validation"] = dsl.validate(row["definition"])
    row["resolved_universe"] = algo.instrument_label(row["definition"])
    return row


@router.put("/{strategy_id}")
def update_strategy(strategy_id: str, payload: StrategyRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    validation = dsl.validate(payload.definition)
    if not validation["valid"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": validation["errors"]})
    with app_cursor() as cursor:
        cursor.execute(
            "UPDATE strategies SET name = ?, description = ?, definition = ?, updated_at = ? WHERE id = ?",
            (payload.name, payload.description, json.dumps(payload.definition), now(), strategy_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return {"id": strategy_id, "updated": True}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM strategies WHERE id = ? AND is_template = 0", (strategy_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return {"deleted": True, "id": strategy_id}


# ---------------------------------------------------------------- backtesting


@router.post("/backtest")
def backtest(payload: BacktestRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Backtest a strategy over candles recorded by this platform."""
    if not settings.enable_backtest:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Backtesting is disabled.")

    definition = payload.definition
    if definition is None and payload.strategy_id:
        with app_cursor() as cursor:
            cursor.execute("SELECT definition FROM strategies WHERE id = ?", (payload.strategy_id,))
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        definition = json.loads(row["definition"])
    if definition is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide a strategy_id or a definition")

    interval = payload.timeframe or definition.get("timeframe") or "5m"
    if interval not in ticks.INTERVAL_SECONDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported timeframe '{interval}'.",
        )

    # Candles come only from recorded ticks — nothing is synthesised.
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    coverage: list[dict[str, Any]] = []
    for item in definition.get("universe") or []:
        token = str(item["token"])
        segment = item.get("exchange_segment", "nse_cm")
        candles = ticks.history(token, segment, interval, 5000)
        instrument = master.get_instrument(token, segment)
        label = (item.get("label") or (instrument or {}).get("name") or token)
        candles_by_symbol[label] = candles
        coverage.append({
            "label": label, "token": token, "bars": len(candles),
            "coverage": ticks.coverage(token, segment),
        })

    result = engine.run(
        definition, candles_by_symbol,
        initial_capital=payload.initial_capital, slippage=payload.slippage, timeframe=interval,
    )
    return {**result.to_dict(), "coverage": coverage, "interval": interval}

