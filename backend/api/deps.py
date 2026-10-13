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
from backend.core.vault import AngelCredentials

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
    """Pick the account to act on, refusing cross-account access.

    An admin may address any account. Everyone else gets their own, and falls
    back to the single environment-configured account only when the deployment
    has no stored broker accounts at all — otherwise "the default account"
    would hand any signed-in user the first account somebody else connected.
    """
    if not user.is_admin:
        account_id = requested or user.account_id
        if account_id:
            if account_id == user.account_id:
                return account_id
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that account")
        if not accounts.list_accounts():
            fallback = accounts.default_account_id()
            if fallback:
                return fallback
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Kotak account is connected to your profile. Add your credentials on the Connect broker page.",
        )

    account_id = requested or user.account_id or accounts.default_account_id()
    if not account_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Kotak account is configured. Add credentials, or set the KOTAK_* environment variables.",
        )
    return account_id


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


# -------------------------------------------------------- Angel One data layer


def resolve_angel_account_id(preferred: str | None = None) -> str | None:
    """Pick the Angel account used for market data.

    Market data comes from Angel One. The account is chosen from the Angel
    accounts that exist, or the single-account environment fallback. It is
    deliberately *not* tied to a user: two users read the same public tape,
    and only orders are scoped to the account that owns them.
    """
    if preferred:
        return preferred
    return accounts.default_account_id(accounts.ANGEL)


def angel_call(user: CurrentUser | None = None, account_id: str | None = None) -> Callable[..., Any]:
    """A callable that runs one SmartAPI request for the market-data account.

    Mirrors :func:`broker_call`, but for Angel One. When no Angel account is
    configured the caller gets a 400 explaining exactly what to do, rather
    than an opaque connection error.
    """
    from backend.angel.auth import AngelAuthError, angel_session_manager

    # Market data belongs to whichever Angel account exists, never to the
    # caller's Kotak account. A requested/account-bound id is only honoured when
    # it really is an Angel account; anything else (e.g. a Kotak UCC) is dropped
    # in favour of the resolved Angel account so we raise the accurate
    # "no Angel account configured" error instead of a per-account one.
    target = account_id or (user.account_id if user and user.account_id else None)
    if not target or accounts.broker_of(target) != accounts.ANGEL:
        target = resolve_angel_account_id()

    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Angel One account is configured. Add Angel One SmartAPI credentials on the "
            "Connect broker page, or set the ANGEL_API_KEY / ANGEL_CLIENT_CODE / ANGEL_PIN / "
            "ANGEL_TOTP_KEY environment variables.",
        )

    credentials = accounts.get_credentials(target)
    if not isinstance(credentials, AngelCredentials):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Account '{target}' has no Angel One credentials. Market data requires an Angel One account.",
        )

    def call(method: str, path: str, **kwargs: Any) -> Any:
        try:
            return angel_session_manager.call(target, credentials, method, path, **kwargs)
        except AngelAuthError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    return call


def angel_session_health(account_id: str | None = None) -> dict[str, Any]:
    from backend.angel.auth import angel_session_manager

    target = account_id or resolve_angel_account_id()
    if not target:
        return {"broker": "angel", "account_id": None, "authenticated": False, "status": "unconfigured"}
    health = angel_session_manager.health(target)
    health["credentials"] = accounts.account_status(target)
    return health
