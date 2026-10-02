"""Execution routes: portfolio, live orders, paper trading, risk, algo runs."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, broker_call, current_user, require_trader, resolve_account_id, session_health
from backend.core.database import app_cursor, now
from backend.execution import algo, live
from backend.execution.charges import DEFAULT_PLANS, round_trip_cost
from backend.marketdata.market_hours import is_market_open
from backend.papertrading import engine as paper
from backend.risk import engine as risk_engine

router = APIRouter(prefix="/api/v1", tags=["execution"])


class OrderModel(BaseModel):
    exchange_segment: str
    product: str
    trading_symbol: str
    transaction_type: str = Field(pattern="^[BS]$")
    order_type: str = Field(default="MKT", pattern="^(L|MKT|SL|SL-M)$")
    quantity: float = Field(gt=0)
    price: float = 0
    trigger_price: float = 0
    validity: str = "DAY"
    instrument_token: str | None = None
    disclosed_quantity: str = "0"
    amo: str = "NO"
    confirmation: str = ""
    equity: float | None = None


class ModifyModel(BaseModel):
    order_id: str
    price: str
    quantity: str
    order_type: str = Field(pattern="^(L|MKT|SL|SL-M)$")
    validity: str = "DAY"
    trigger_price: str = "0"
    disclosed_quantity: str = "0"
    confirmation: str = ""


class CancelModel(BaseModel):
    order_id: str
    confirmation: str = ""


class CloseModel(BaseModel):
    trading_symbol: str
    quantity: float | None = None
    reason: str = "manual close"
    price: float | None = None


class JournalModel(BaseModel):
    symbol: str
    side: str = Field(pattern="^[BS]$")
    quantity: float
    entry_price: float
    exit_price: float | None = None
    trade_id: str | None = None
    setup: str = ""
    emotion: str = ""
    followed_plan: bool = True
    notes: str = ""


# ------------------------------------------------------------------ portfolio


@router.get("/portfolio")
def portfolio(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Live holdings, positions, limits and books from Kotak Neo."""
    account_id = resolve_account_id(user)
    try:
        return {**live.portfolio(account_id), "session": session_health(account_id)}
    except live.OrderRejected as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error.message) from error


@router.get("/orders")
def list_orders(
    limit: int = Query(default=100, ge=1, le=1000),
    mode: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> list[dict[str, Any]]:
    account_id = resolve_account_id(user)
    sql = "SELECT * FROM orders WHERE account_id = ?"
    params: list[Any] = [account_id]
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with app_cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


@router.get("/margin")
def margin(payload: dict[str, Any] = Body(...), user: CurrentUser = Depends(current_user)) -> Any:
    """Margin Kotak requires for an order, before placing it."""
    account_id = resolve_account_id(user)
    try:
        return live.margin_required(payload, account_id)
    except live.OrderRejected as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error.message) from error


@router.get("/charges")
def charges_preview(
    exchange_segment: str = Query(default="nse_cm"),
    product: str = Query(default="MIS"),
    quantity: float = Query(gt=0),
    price: float = Query(gt=0),
    is_option: bool = False,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Brokerage and statutory charges for a prospective trade."""
    from backend.execution.charges import compute

    buy = compute(side="B", segment=exchange_segment, product=product, quantity=quantity, price=price, is_option=is_option)
    sell = compute(side="S", segment=exchange_segment, product=product, quantity=quantity, price=price, is_option=is_option)
    round_trip = round_trip_cost(
        segment=exchange_segment, product=product, quantity=quantity, price=price, is_option=is_option
    )
    notional = price * quantity
    return {
        "buy": buy.to_dict(), "sell": sell.to_dict(),
        "round_trip": round(round_trip, 2),
        # The price move needed before the trade breaks even.
        "break_even_move_percent": round((round_trip / notional) * 100, 3) if notional else None,
        "plans": [
            {"name": p.name, "segment": p.segment, "product": p.product, "rate": p.rate,
             "per_order": p.per_order, "cap_per_order": p.cap_per_order, "min_per_order": p.min_per_order}
            for p in DEFAULT_PLANS
        ],
    }


# ----------------------------------------------------------------- live orders


@router.post("/orders/live", status_code=status.HTTP_201_CREATED)
def place_live_order(payload: OrderModel, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Place a real order with Kotak Neo.

    Requires the typed confirmation phrase, the live-trading feature gate, and a
    passing risk check.
    """
    account_id = resolve_account_id(user)
    try:
        return live.place_order(
            payload.model_dump(), account_id=account_id, confirmation=payload.confirmation,
            equity=payload.equity, is_market_open=is_market_open(),
        )
    except live.OrderRejected as error:
        code = status.HTTP_400_BAD_REQUEST
        if error.reason in {"DISABLED", "CONFIRMATION"}:
            code = status.HTTP_403_FORBIDDEN
        elif error.reason.startswith("BROKER") or error.reason == "REJECTED":
            code = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, {"reason": error.reason, "message": error.message, "detail": error.detail}) from error


@router.post("/orders/modify")
def modify_order(payload: ModifyModel, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    try:
        return live.modify_order(
            account_id=account_id, order_id=payload.order_id, price=payload.price,
            quantity=payload.quantity, order_type=payload.order_type, validity=payload.validity,
            trigger_price=payload.trigger_price, disclosed_quantity=payload.disclosed_quantity,
            confirmation=payload.confirmation,
        )
    except live.OrderRejected as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error.message) from error


@router.post("/orders/cancel")
def cancel_order(payload: CancelModel, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    try:
        return live.cancel_order(
            account_id=account_id, order_id=payload.order_id, confirmation=payload.confirmation
        )
    except live.OrderRejected as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error.message) from error


@router.post("/session/reconnect")
def reconnect(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Force a fresh Kotak Neo login."""
    account_id = resolve_account_id(user)
    return live.reconnect(account_id)


# --------------------------------------------------------------- paper trading


@router.post("/paper/orders", status_code=status.HTTP_201_CREATED)
def paper_order(payload: OrderModel, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Simulate an order against live Kotak quotes."""
    account_id = resolve_account_id(user)
    try:
        return paper.place_paper_order(payload.model_dump(), account_id=account_id)
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error


@router.post("/paper/close")
def paper_close(payload: CloseModel, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    try:
        return paper.close_position(
            account_id=account_id, trading_symbol=payload.trading_symbol,
            quantity=payload.quantity, reason=payload.reason, price=payload.price,
        )
    except RuntimeError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error


@router.get("/paper/portfolio")
def paper_portfolio(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    return paper.mark_to_market(resolve_account_id(user))


@router.get("/paper/journal")
def paper_journal(limit: int = Query(default=200, ge=1, le=1000), user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    return paper.journal(resolve_account_id(user), limit)


@router.post("/paper/journal")
def add_journal(payload: JournalModel, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    entry_id = paper.add_journal_note(account_id=account_id, **payload.model_dump())
    return {"id": entry_id, "created": True}


@router.post("/paper/reset")
def paper_reset(user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    return paper.reset(resolve_account_id(user))


# ------------------------------------------------------------------------ risk


@router.get("/risk")
def risk_status(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    equity = paper.capital(account_id)
    return risk_engine.RiskEngine(account_id).status(equity)


@router.put("/risk")
def update_risk(payload: dict[str, Any], user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    account_id = resolve_account_id(user)
    try:
        return risk_engine.update_config(account_id, payload)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


@router.post("/risk/kill-switch")
def kill_switch(payload: dict[str, Any], user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Engage the emergency kill switch: no further orders will be sent."""
    account_id = resolve_account_id(user)
    reason = str(payload.get("reason") or "engaged by operator")
    risk_engine.RiskEngine(account_id).engage_kill_switch(reason)
    stopped = algo.supervisor.stop_all_for(account_id, f"kill switch: {reason}")
    return {"kill_switch": True, "reason": reason, "algo_runs_stopped": stopped}


@router.post("/risk/release")
def release_kill_switch(user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    return risk_engine.RiskEngine(resolve_account_id(user)).release_kill_switch()


@router.get("/risk/events")
def risk_events(limit: int = Query(default=50, ge=1, le=500), user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    return risk_engine.recent_events(resolve_account_id(user), limit)


# ------------------------------------------------------------------ algo runs


@router.get("/algo/runs")
def list_runs(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    return algo.supervisor.list(resolve_account_id(user))


@router.post("/algo/runs", status_code=status.HTTP_201_CREATED)
def start_run(strategy_id: str, mode: str = Query(default="paper", pattern="^(paper|live)$"),
               user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Deploy a strategy to trade paper or live."""
    account_id = resolve_account_id(user)
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    import json

    strategy = {"id": strategy_id, "name": row["name"], "definition": json.loads(row["definition"])}
    try:
        run = algo.supervisor.start(strategy, account_id, mode)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    return run.to_dict()


@router.get("/algo/runs/{run_id}")
def get_run(run_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    run = algo.supervisor.get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run.to_dict()


@router.post("/algo/runs/{run_id}/stop")
def stop_run(run_id: str, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    return algo.supervisor.stop(run_id)
