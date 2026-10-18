"""Admin-scoped user management: role changes for non-admin users."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, require_admin
from backend.core.database import app_cursor, row_to_dict

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin users"])

ROLE_LEVEL = {"viewer": 1, "trader": 2, "admin": 3}

class RoleChangeRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|trader|viewer)$")


@router.patch("/{user_id}/role")
def update_admin_user_role(
    user_id: str,
    payload: RoleChangeRequest,
    current_user: CurrentUser = Depends(require_admin),
):
    """
    Change a user's role.

    - Admin only.
    - Cannot demote another admin.
    - Cannot change your own role.
    - Only downgraders allowed (no role upgrades).
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own role",
        )

    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        target = row_to_dict(cursor.fetchone())
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        if target["role"] == "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot demote an admin through this endpoint",
            )

        if ROLE_LEVEL[target["role"]] > ROLE_LEVEL[payload.role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only downgrades are allowed via this endpoint",
            )

        cursor.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (payload.role, user_id),
        )
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        updated = row_to_dict(cursor.fetchone())

    return {
        "id": updated["id"],
        "email": updated["email"],
        "role": updated["role"],
        "is_active": bool(updated["is_active"]),
    }
