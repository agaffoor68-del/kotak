from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.ai.live_scanner import (
    bootstrap_all_indices,
    get_recent_ai_trades,
    latest_multi,
    record_ai_trade,
    scan_once,
)
from backend.api.deps import CurrentUser, current_user

log = logging.getLogger("alphatrade.api.ai")
router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class RecordTradeRequest(BaseModel):
    index: str = Field(..., description="Index name, e.g. 'NIFTY 50', 'BANKNIFTY', 'SENSEX'")
    signal: str = Field(..., description="Signal at entry, e.g. 'BUY CE' or 'BUY PE'")
    entry_price: float = Field(..., description="Execution entry price")
    exit_price: float | None = Field(default=None, description="Exit price if closed")
    pnl: float | None = Field(default=None, description="Profit/loss amount")
    indicators_at_entry: dict[str, Any] | None = Field(
        default=None, description="State of indicators when the signal was generated"
    )
    result: str = Field(..., description="'WIN' or 'LOSS'")


@router.get("/suggest_trade")
def suggest_trade(user: CurrentUser = Depends(current_user)):
    """Return top actionable recommendation from multi-index confluence analysis."""
    multi = latest_multi()
    if not multi:
        multi = scan_once()
    if not multi:
        raise HTTPException(503, "Live scanner is warming up; no market snapshot is available yet.")

    # Prioritize active signals over WAIT
    actionable = [item for item in multi if item.get("signal") in ("BUY CE", "BUY PE")]
    if actionable:
        return actionable[0]
    return multi[0]


@router.get("/live-analysis")
def live_analysis(
    underlying: str | None = Query(default=None, description="Optional filter: 'NIFTY 50', 'BANKNIFTY', 'SENSEX'"),
    single: bool = Query(default=False, description="If true and underlying provided, return single object"),
    user: CurrentUser = Depends(current_user),
):
    """Task 2: Multi-Index Analysis Engine.
    
    Loops through indices: ["NIFTY 50", "BANKNIFTY", "SENSEX"] and returns a JSON array.
    """
    multi = latest_multi()
    if not multi:
        multi = scan_once()
    if not multi:
        raise HTTPException(503, "Live scanner is warming up; no market snapshot is available yet.")

    if underlying and underlying.upper() not in ("ALL", "*"):
        norm = underlying.upper().strip()
        matched = [
            item for item in multi
            if item.get("index", "").upper() == norm or item.get("symbol", "").upper() == norm or item.get("underlying", "").upper() == norm
        ]
        if matched and single:
            return matched[0]
        if matched:
            return matched

    return multi


@router.get("/trade-history")
def get_trade_history(
    index: str | None = Query(default=None, description="Filter by index name"),
    limit: int = Query(default=10, ge=1, le=100),
    day_only: bool = Query(default=False, description="Only today's trades"),
    user: CurrentUser = Depends(current_user),
):
    """Task 3: Retrieve recorded trades and pattern memory."""
    if index:
        return get_recent_ai_trades(index=index, limit=limit, day_only=day_only)
    
    # Return recent trades across all indices
    all_trades = []
    for idx in ("NIFTY 50", "BANKNIFTY", "SENSEX"):
        all_trades.extend(get_recent_ai_trades(index=idx, limit=limit, day_only=day_only))
    all_trades.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return all_trades[:limit]


@router.post("/trade-history")
def save_ai_trade(
    body: RecordTradeRequest,
    user: CurrentUser = Depends(current_user),
):
    """Task 3: Record an AI trade outcome to trigger learning from loss."""
    trade_id = record_ai_trade(
        index=body.index,
        signal=body.signal,
        entry_price=body.entry_price,
        exit_price=body.exit_price,
        pnl=body.pnl,
        indicators_at_entry=body.indicators_at_entry,
        result=body.result,
    )
    return {"status": "recorded", "id": trade_id, "index": body.index, "result": body.result.upper()}


@router.post("/bootstrap")
def trigger_bootstrap(user: CurrentUser = Depends(current_user)):
    """Task 1: Force bootstrap 50 historical 5m candles for all indices."""
    counts = bootstrap_all_indices()
    signals = scan_once()
    return {"bootstrapped_candles": counts, "signals_generated": len(signals)}
