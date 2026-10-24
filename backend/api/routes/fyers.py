"""Fyers routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import CurrentUser, current_user
from backend.fyers.auth import fyers_auth

logger = logging.getLogger("alphatrade.api.fyers")

router = APIRouter(prefix="/api/v1/fyers", tags=["fyers"])


@router.get("/login")
def fyers_login(user: CurrentUser = Depends(current_user)) -> dict[str, str]:
    url = fyers_auth.get_login_url()
    if not url:
        raise HTTPException(status_code=500, detail="Failed to generate Fyers login URL")
    return {"login_url": url}


@router.get("/callback")
def fyers_callback(auth_code: str = Query(...), user: CurrentUser = Depends(current_user)) -> dict[str, object]:
    result = fyers_auth.generate_token(auth_code)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
