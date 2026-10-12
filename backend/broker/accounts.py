"""Account registry: resolves an account id to decrypted broker credentials.

One registry serves both brokers. A row in ``broker_accounts`` records which
broker it belongs to; the credential record itself (Kotak's consumer key / UCC,
Angel's API key / client code) is decrypted from the vault and returned as the
matching ``BrokerCredentials`` or ``AngelCredentials`` class.

Keeping one table means "which accounts exist" and "who owns which account"
stay a single query, while the vault never has to guess the broker — the
encrypted payload carries its own ``broker`` tag and is only readable through
:func:`backend.core.vault.load_credentials`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from backend.core.database import app_cursor, now, row_to_dict
from backend.core.vault import (
    AngelCredentials,
    BrokerCredentials,
    VaultLocked,
    load_credentials,
    vault,
)

log = logging.getLogger("alphatrade.accounts")

KOTAK = "kotak"
ANGEL = "angel"
SUPPORTED_BROKERS = (KOTAK, ANGEL)

Credentials = BrokerCredentials | AngelCredentials

_cache: dict[str, tuple[float, Any]] = {}
_cache_ttl = 60.0
_lock = threading.RLock()


def _new_id() -> str:
    return uuid.uuid4().hex


def normalise_broker(broker: str | None) -> str:
    value = str(broker or KOTAK).strip().lower()
    if value not in SUPPORTED_BROKERS:
        raise ValueError(f"Unsupported broker '{broker}'. Expected one of: {', '.join(SUPPORTED_BROKERS)}")
    return value


def save_account(
    *,
    account_id: str,
    label: str,
    credentials: Credentials,
    broker: str = KOTAK,
    environment: str = "prod",
) -> dict[str, Any]:
    """Encrypt and persist a broker account. Raises `VaultLocked` without a key."""
    broker = normalise_broker(broker)
    try:
        ciphertext = vault.encrypt(credentials)
    except VaultLocked as error:
        raise VaultLocked(str(error)) from error

    timestamp = now()
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO broker_accounts (id, account_id, broker, label, encrypted_secrets, environment, is_active, created_at, updated_at)
            VALUES (?,?,?,?,?,?,1,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                broker = excluded.broker,
                label = excluded.label,
                encrypted_secrets = excluded.encrypted_secrets,
                environment = excluded.environment,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (_new_id(), account_id, broker, label, ciphertext, environment, timestamp, timestamp),
        )
    with _lock:
        _cache.pop(account_id, None)
    return {
        "account_id": account_id, "broker": broker, "label": label,
        "environment": environment, "credentials": credentials.masked(),
    }


def list_accounts(broker: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM broker_accounts WHERE is_active = 1"
    params: list[Any] = []
    if broker:
        sql += " AND broker = ?"
        params.append(normalise_broker(broker))
    sql += " ORDER BY created_at"
    with app_cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def broker_of(account_id: str) -> str | None:
    with app_cursor() as cursor:
        cursor.execute("SELECT broker FROM broker_accounts WHERE account_id = ? AND is_active = 1", (account_id,))
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row["broker"] or KOTAK).lower()


def delete_account(account_id: str) -> bool:
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM broker_accounts WHERE account_id = ?", (account_id,))
        deleted = cursor.rowcount > 0
    with _lock:
        _cache.pop(account_id, None)
    return deleted


def get_credentials(account_id: str) -> Any | None:
    """Decrypted credentials for an account, or the env fallback, or `None`."""
    with _lock:
        cached = _cache.get(account_id)
        if cached and time.time() - cached[0] < _cache_ttl:
            return cached[1]

    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM broker_accounts WHERE account_id = ? AND is_active = 1", (account_id,)
        )
        row = row_to_dict(cursor.fetchone())

    if row is not None:
        try:
            credentials = load_credentials(row)
        except Exception as error:  # noqa: BLE001
            log.error("Could not decrypt credentials for %s: %s", account_id, error)
            raise
    else:
        # No stored account: fall back to environment variables, preferring
        # Kotak's (the legacy single-account path) and then Angel's.
        credentials = BrokerCredentials.from_environment() or AngelCredentials.from_environment()
        if credentials is None:
            return None

    with _lock:
        _cache[account_id] = (time.time(), credentials)
    return credentials


def resolve_all(broker: str | None = None) -> dict[str, Any]:
    """Every account with usable credentials, for the keepalive loops."""
    resolved: dict[str, Any] = {}
    for account in list_accounts(broker):
        try:
            credentials = get_credentials(account["account_id"])
        except Exception as error:  # noqa: BLE001
            log.warning("Skipping account %s: %s", account["account_id"], error)
            continue
        if credentials is not None:
            resolved[account["account_id"]] = credentials
    return resolved


def resolve_angel() -> dict[str, Any]:
    """Angel accounts only, filtered by credential type for the Angel loops."""
    resolved: dict[str, Any] = {}
    for account in list_accounts(ANGEL):
        try:
            credentials = get_credentials(account["account_id"])
        except Exception as error:  # noqa: BLE001
            log.warning("Skipping Angel account %s: %s", account["account_id"], error)
            continue
        if isinstance(credentials, AngelCredentials):
            resolved[account["account_id"]] = credentials
    if not resolved:
        fallback = AngelCredentials.from_environment()
        if fallback is not None:
            resolved[fallback.client_code] = fallback
    return resolved


def resolve_kotak() -> dict[str, Any]:
    """Kotak accounts only, for the Neo keepalive loop."""
    resolved: dict[str, Any] = {}
    for account in list_accounts(KOTAK):
        try:
            credentials = get_credentials(account["account_id"])
        except Exception as error:  # noqa: BLE001
            log.warning("Skipping Kotak account %s: %s", account["account_id"], error)
            continue
        if isinstance(credentials, BrokerCredentials):
            resolved[account["account_id"]] = credentials
    if not resolved:
        fallback = BrokerCredentials.from_environment()
        if fallback is not None:
            resolved[fallback.ucc] = fallback
    return resolved


def default_account_id(broker: str | None = None) -> str | None:
    """The account to use when a request does not name one."""
    if broker:
        accounts = list_accounts(broker)
        if accounts:
            return accounts[0]["account_id"]
        if normalise_broker(broker) == ANGEL:
            fallback = AngelCredentials.from_environment()
            return fallback.client_code if fallback else None
        fallback = BrokerCredentials.from_environment()
        return fallback.ucc if fallback else None

    accounts = list_accounts()
    if accounts:
        return accounts[0]["account_id"]
    fallback = BrokerCredentials.from_environment() or AngelCredentials.from_environment()
    if fallback is None:
        return None
    return getattr(fallback, "ucc", None) or getattr(fallback, "client_code", None)


def account_status(account_id: str) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT account_id, broker, label, environment, is_active, created_at, updated_at "
            "FROM broker_accounts WHERE account_id = ?",
            (account_id,),
        )
        row = row_to_dict(cursor.fetchone())
    if row is None:
        return {"account_id": account_id, "configured": False}
    row["broker"] = str(row.get("broker") or KOTAK).lower()
    return {**row, "configured": True}
