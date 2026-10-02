"""Account registry: resolves an account id to decrypted Kotak credentials.

Keeps the vault the only place secrets are handled. The rest of the platform
asks for a `BrokerCredentials` by account id and never sees ciphertext.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from backend.core.database import app_cursor, now, row_to_dict
from backend.core.vault import BrokerCredentials, VaultLocked, vault

log = logging.getLogger("alphatrade.accounts")

_cache: dict[str, tuple[float, BrokerCredentials]] = {}
_cache_ttl = 60.0
_lock = threading.RLock()


def _new_id() -> str:
    return uuid.uuid4().hex


def save_account(
    *,
    account_id: str,
    label: str,
    credentials: BrokerCredentials,
    environment: str = "prod",
) -> dict[str, Any]:
    """Encrypt and persist a broker account. Raises `VaultLocked` without a key."""
    try:
        ciphertext = vault.encrypt(credentials)
    except VaultLocked as error:
        raise VaultLocked(str(error)) from error

    timestamp = now()
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO broker_accounts (id, account_id, label, encrypted_secrets, environment, is_active, created_at, updated_at)
            VALUES (?,?,?,?,?,1,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                label = excluded.label,
                encrypted_secrets = excluded.encrypted_secrets,
                environment = excluded.environment,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (_new_id(), account_id, label, ciphertext, environment, timestamp, timestamp),
        )
    with _lock:
        _cache.pop(account_id, None)
    return {"account_id": account_id, "label": label, "environment": environment, "credentials": credentials.masked()}


def list_accounts() -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM broker_accounts WHERE is_active = 1 ORDER BY created_at")
        return [dict(row) for row in cursor.fetchall()]


def delete_account(account_id: str) -> bool:
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM broker_accounts WHERE account_id = ?", (account_id,))
        deleted = cursor.rowcount > 0
    with _lock:
        _cache.pop(account_id, None)
    return deleted


def get_credentials(account_id: str) -> BrokerCredentials | None:
    """Decrypted credentials for an account, or the env fallback, or `None`."""
    with _lock:
        cached = _cache.get(account_id)
        if cached and time.time() - cached[0] < _cache_ttl:
            return cached[1]

    with app_cursor() as cursor:
        cursor.execute("SELECT encrypted_secrets FROM broker_accounts WHERE account_id = ? AND is_active = 1", (account_id,))
        row = cursor.fetchone()

    if row is not None:
        try:
            credentials = vault.decrypt(row["encrypted_secrets"])
        except Exception as error:  # noqa: BLE001
            log.error("Could not decrypt credentials for %s: %s", account_id, error)
            raise
    else:
        credentials = BrokerCredentials.from_environment()
        if credentials is None:
            return None

    with _lock:
        _cache[account_id] = (time.time(), credentials)
    return credentials


def resolve_all() -> dict[str, BrokerCredentials]:
    """Every account with usable credentials, for the keepalive loop."""
    resolved: dict[str, BrokerCredentials] = {}
    for account in list_accounts():
        try:
            credentials = get_credentials(account["account_id"])
        except Exception as error:  # noqa: BLE001
            log.warning("Skipping account %s: %s", account["account_id"], error)
            continue
        if credentials is not None:
            resolved[account["account_id"]] = credentials
    if not resolved:
        fallback = BrokerCredentials.from_environment()
        if fallback is not None:
            resolved[fallback.ucc] = fallback
    return resolved


def default_account_id() -> str | None:
    """The account to use when a request does not name one."""
    accounts = list_accounts()
    if accounts:
        return accounts[0]["account_id"]
    fallback = BrokerCredentials.from_environment()
    return fallback.ucc if fallback else None


def account_status(account_id: str) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT account_id, label, environment, is_active, created_at, updated_at FROM broker_accounts WHERE account_id = ?", (account_id,))
        row = row_to_dict(cursor.fetchone())
    if row is None:
        return {"account_id": account_id, "configured": False}
    return {**row, "configured": True}
