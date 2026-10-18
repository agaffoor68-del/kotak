"""User settings: profile update endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, current_user
from backend.core.database import app_cursor, row_to_dict

router = APIRouter(prefix="/api/v1/users", tags=["users"])

class UserSettingsRequest(BaseModel):
    whatsapp_number: str | None = Field(
        default=None,
        max_length=20,
        description="WhatsApp number with country code, e.g. +919876543210",
    )

@router.put("/settings", response_model=dict)
def update_user_settings(
    payload: UserSettingsRequest,
    user: CurrentUser = Depends(current_user),
) -> dict[str, any]:
    """Save the current user's WhatsApp number (and other settings)."""
    with app_cursor() as cursor:
        cursor.execute(
            "UPDATE users SET whatsapp_number = ? WHERE id = ?",
            (payload.whatsapp_number, user.id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        cursor.execute(
            "SELECT id, email, role, whatsapp_number FROM users WHERE id = ?",
            (user.id,),
        )
        updated = row_to_dict(cursor.fetchone())

    return {
        "id": updated["id"],
        "email": updated["email"],
        "role": updated["role"],
        "whatsapp_number": updated.get("whatsapp_number"),
    }
