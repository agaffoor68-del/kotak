from fastapi import APIRouter, Depends, HTTPException, Query
from backend.api.deps import current_user, CurrentUser
from backend.ai.live_scanner import latest

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

@router.get("/suggest_trade")
def suggest_trade(user: CurrentUser = Depends(current_user)):
    return live_analysis(user=user)


@router.get("/live-analysis")
def live_analysis(
    underlying: str | None = Query(default=None, description="NIFTY 50 or BANKNIFTY"),
    user: CurrentUser = Depends(current_user),
):
    result = latest(underlying)
    if result is None:
        raise HTTPException(503, "Live scanner is warming up; no market snapshot is available yet.")
    return result
