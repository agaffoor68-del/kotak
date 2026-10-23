from fastapi import APIRouter, Depends
from backend.api.deps import current_user, CurrentUser

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

@router.get("/suggest_trade")
def suggest_trade(user: CurrentUser = Depends(current_user)):
    return {"signal": "BUY CE", "confidence": "High", "reason": "Price above VWAP, RSI momentum 65", "strike": "ATM"}