"""FastAPI dependencies: authentication, roles and the broker call bridge."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, Request, status

from backend.broker import accounts
from backend.broker.neo_auth import NeoAuthError, session_manager
from backend.core.config import settings
from backend.core.security import TokenClaims, decode_access_token, verify_api_key
from backend.core.database import app_cursor, row_to_dict

log = logging.getLogger("alphatrade.api")

#: Role hierarchy. Higher roles include the permissions of the lower ones.
ROLE_LEVEL = {"viewer": 1, "trader": 2, "admin": 3}


class CurrentUser:
    """The authenticated caller, resolved from a bearer token or an API key."""

    def __init__(self, user_id: str, email: str, role: str, account_id: str | None) -> None:
        self.id = user_id
        self.email = email
        self.role = role
        self.account_id = account_id

    @property
    def can_trade(self) -> bool:
        return ROLE_LEVEL.get(self.role, 0) >= ROLE_LEVEL["trader"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "role": self.role, "account_id": self.account_id}


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def _user_from_api_key(raw_key: str) -> dict[str, Any] | None:
    from backend.core.security import hash_api_key

    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT u.* FROM api_keys k JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.is_active = 1
            """,
            (hash_api_key(raw_key),),
        )
        row = row_to_dict(cursor.fetchone())
    if row is None:
        return None
    with app_cursor() as cursor:
        cursor.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                       (__import__("time").time(), hash_api_key(raw_key)))
    return row


async def current_user(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> CurrentUser:
    """Resolve the caller, or raise 401."""
    if x_api_key:
        row = _user_from_api_key(x_api_key)
        if row:
            return CurrentUser(row["id"], row["email"], row["role"], row.get("account_id"))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    try:
        claims: TokenClaims = decode_access_token(token)
    except ValueError as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error

    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (claims.subject,))
        row = row_to_dict(cursor.fetchone())
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is not active")
    return CurrentUser(row["id"], row["email"], row["role"], row.get("account_id") or claims.account_id)


async def require_trader(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.can_trade:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This action requires the trader or admin role")
    return user


async def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This action requires the admin role")
    return user


def resolve_account_id(user: CurrentUser, requested: str | None = None) -> str:
    """Pick the account to act on, refusing cross-account access."""
    account_id = requested or user.account_id or accounts.default_account_id()
    if not account_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Kotak account is configured. Add credentials, or set the KOTAK_* environment variables.",
        )
    if user.is_admin or user.account_id == account_id:
        return account_id
    if accounts.default_account_id() == account_id:
        return account_id
    raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that account")


def broker_call(account_id: str) -> Callable[..., Any]:
    """A callable that runs a Kotak Neo method for this account."""
    credentials = accounts.get_credentials(account_id)
    if credentials is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No Kotak credentials are available for account '{account_id}'.",
        )

    def call(method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return session_manager.call(account_id, credentials, method, *args, **kwargs)
        except NeoAuthError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    return call


def session_health(account_id: str) -> dict[str, Any]:
    health = session_manager.health(account_id)
    health["configured"] = accounts.account_status(account_id).get("configured", False)
    health["credentials"] = accounts.account_status(account_id)
    return health
