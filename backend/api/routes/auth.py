"""Authentication and user-management routes."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser, current_user, require_admin, require_trader
from backend.core.database import app_cursor, now, row_to_dict
from backend.core.security import create_access_token, generate_api_key, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=256)
    role: str = "trader"

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in {"admin", "trader", "viewer"}:
            raise ValueError("role must be admin, trader or viewer")
        return value


class ApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    account_id: str | None = None


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "email": row["email"], "role": row["role"],
        "account_id": row.get("account_id"), "is_active": bool(row["is_active"]),
        "created_at": row.get("created_at"), "last_login_at": row.get("last_login_at"),
    }


@router.post("/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE email = ?", (payload.email.strip().lower(),))
        user = row_to_dict(cursor.fetchone())

    # A single generic message for both "no such user" and "wrong password" so
    # the endpoint cannot be used to enumerate accounts.
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled")

    token, claims = create_access_token(subject=user["id"], role=user["role"], account_id=user.get("account_id") or "")
    with app_cursor() as cursor:
        cursor.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))
    return {
        "access_token": token, "token_type": "bearer",
        "expires_at": claims.expires_at, "expires_in": claims.expires_at - int(time.time()),
        "user": _public_user(user),
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest) -> dict[str, Any]:
    """Open self-registration.

    The first account to register is always an admin; afterwards only an admin may
    create additional users, so an open endpoint cannot be used to escalate.
    """
    with app_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM users")
        total = int(cursor.fetchone()["total"] or 0)

    if total > 0:
        with app_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS admins FROM users WHERE role = 'admin' AND is_active = 1")
            if int(cursor.fetchone()["admins"] or 0) > 0:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Self-registration is disabled. Ask an admin to create your account.",
                )
        role = "admin"
    else:
        role = "admin"

    user_id = uuid.uuid4().hex
    try:
        with app_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, payload.email, hash_password(payload.password), role, now()),
            )
    except Exception as error:  # sqlite unique violation
        if "UNIQUE" in str(error):
            raise HTTPException(status.HTTP_409_CONFLICT, "That email is already registered") from error
        raise
    return {"id": user_id, "email": payload.email, "role": role}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    return user.to_dict()


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_key(payload: ApiKeyRequest, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Mint an API key for automation. The plaintext is returned only once.

    Requires the trader role: an API key bypasses the browser session, so a
    read-only account must not be able to mint one.
    """
    raw_key, key_hash = generate_api_key()
    key_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            "INSERT INTO api_keys (id, user_id, account_id, name, key_hash, prefix, created_at) VALUES (?,?,?,?,?,?,?)",
            (key_id, user.id, payload.account_id or user.account_id, payload.name, key_hash, raw_key[:8], now()),
        )
    return {
        "id": key_id, "name": payload.name, "api_key": raw_key, "prefix": raw_key[:8],
        "warning": "Store this key now — it is not retrievable later.",
    }


@router.get("/api-keys")
def list_keys(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, prefix, account_id, created_at, last_used_at, revoked_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user.id,),
        )
        return [dict(row) for row in cursor.fetchall()]


@router.delete("/api-keys/{key_id}")
def revoke_key(key_id: str, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ?", (now(), key_id, user.id))
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    return {"revoked": True, "id": key_id}


@router.get("/users")
def list_users(user: CurrentUser = Depends(require_admin)) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM users ORDER BY created_at")
        return [_public_user(row_to_dict(row)) for row in cursor.fetchall()]


@router.post("/users")
def create_user(payload: RegisterRequest, user: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    user_id = uuid.uuid4().hex
    try:
        with app_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, payload.email, hash_password(payload.password), payload.role, now()),
            )
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status.HTTP_409_CONFLICT, "That email is already registered") from error
        raise
    return {"id": user_id, "email": payload.email, "role": payload.role}
