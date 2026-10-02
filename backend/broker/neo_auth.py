"""Kotak Neo authentication: TOTP login, MPIN validation, session lifecycle.

Neo authenticates in two steps and returns errors inside HTTP 200 bodies, so the
whole module treats "a body containing an error" as a hard failure. Without
that, the client looks logged in while every data call is rejected.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from neo_api_client import NeoAPI

from backend.core.config import settings
from backend.core.vault import BrokerCredentials

log = logging.getLogger("alphatrade.neo")


class NeoAuthError(RuntimeError):
    """Any failure while authenticating with Kotak Neo."""


class NeoSessionExpired(NeoAuthError):
    """The trade token is no longer usable and the session must be rebuilt."""


def extract_error(payload: Any) -> str | None:
    """Read an error out of a Neo response body, whatever shape it uses.

    Kotak mixes: plain strings, `{"Error Message": ...}`,
    `{"error": [{"code": ..., "message": ...}]}` and
    `{"error": [{"message": ...}]}`.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        for key in ("Error Message", "error_message", "message", "error_msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = payload.get("error")
        if nested is not None:
            return extract_error(nested)
        return None
    if isinstance(payload, list) and payload:
        parts = [extract_error(item) for item in payload]
        joined = "; ".join(part for part in parts if part)
        return joined or None
    return None


def _is_session_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "2fa", "session", "token", "unauthorised", "unauthorized", "expired",
            "complete the 2fa", "e838", "401", "invalid sessionid", "invalid view token",
        )
    )


@dataclass
class NeoSession:
    """One authenticated Neo client plus the state needed to refresh it."""

    account_id: str
    client: Any
    authenticated_at: float = field(default_factory=time.time)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.authenticated_at

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > settings.session_ttl_seconds

    def status(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "authenticated_at": self.authenticated_at,
            "age_seconds": round(self.age_seconds),
            "expires_in_seconds": max(0, round(settings.session_ttl_seconds - self.age_seconds)),
            "state": self.state,
        }


class NeoSessionManager:
    """Thread-safe, self-refreshing pool of Neo sessions, one per account."""

    def __init__(self) -> None:
        self._sessions: dict[str, NeoSession] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._last_error: dict[str, str] = {}

    # -- internals -------------------------------------------------------
    def _lock_for(self, account_id: str) -> threading.RLock:
        with self._guard:
            if account_id not in self._locks:
                self._locks[account_id] = threading.RLock()
            return self._locks[account_id]

    def _authenticate(self, credentials: BrokerCredentials) -> NeoSession:
        try:
            import pyotp
        except ImportError:  # pragma: no cover - declared in requirements
            raise NeoAuthError("The `pyotp` package is required for Kotak Neo TOTP login") from None

        client = NeoAPI(
            environment=credentials.environment or "prod",
            access_token=None,
            neo_fin_key=credentials.neo_fin_key,
            consumer_key=credentials.consumer_key,
        )

        view = client.totp_login(
            mobile_number=credentials.mobile_number,
            ucc=credentials.ucc,
            totp=pyotp.TOTP(credentials.totp_key).now(),
        )
        if failure := extract_error(view):
            raise NeoAuthError(f"totp_login rejected: {failure}")

        trade = client.totp_validate(mpin=credentials.mpin)
        if failure := extract_error(trade):
            raise NeoAuthError(f"totp_validate rejected: {failure}")

        state: dict[str, Any] = {}
        if isinstance(trade, dict):
            data = trade.get("data")
            if isinstance(data, dict):
                state = {"server_id": data.get("hsServerId"), "data_center": data.get("dataCenter")}

        return NeoSession(account_id=credentials.ucc, client=client, state=state)

    def _establish(self, account_id: str, credentials: BrokerCredentials) -> NeoSession:
        last: Exception | None = None
        for attempt in range(1, max(1, settings.login_attempts) + 1):
            try:
                session = self._authenticate(credentials)
                self._last_error.pop(account_id, None)
                log.info("Kotak Neo session established for %s", account_id)
                return session
            except Exception as error:  # noqa: BLE001 - re-raised below
                last = error
                self._last_error[account_id] = str(error)
                log.warning("Kotak login attempt %s for %s failed: %s", attempt, account_id, error)
                if attempt < settings.login_attempts:
                    time.sleep(min(2 ** attempt, 8))
        raise NeoAuthError(f"Kotak Neo login failed for {account_id} after {settings.login_attempts} attempts: {last}")

    # -- public API ------------------------------------------------------
    def get(self, account_id: str, credentials: BrokerCredentials | None = None) -> NeoSession:
        """Return a valid session, logging in or refreshing when required."""
        lock = self._lock_for(account_id)
        with lock:
            session = self._sessions.get(account_id)
            if session is not None and not session.is_expired:
                return session
            if credentials is None:
                raise NeoAuthError(f"No credentials available for account {account_id}")
            session = self._establish(account_id, credentials)
            self._sessions[account_id] = session
            return session

    def call(self, account_id: str, credentials: BrokerCredentials | None, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a client method, rebuilding the session once if it expired.

        Neo answers expired sessions with HTTP 200 and an error body, so both the
        raised exceptions *and* the returned payload are inspected.
        """
        lock = self._lock_for(account_id)
        with lock:
            for attempt in (1, 2):
                if credentials is None:
                    raise NeoAuthError(f"No credentials available for account {account_id}")
                session = self.get(account_id, credentials)
                try:
                    result = getattr(session.client, method)(*args, **kwargs)
                except Exception as error:  # noqa: BLE001
                    message = str(error)
                    if attempt == 2 or not _is_session_error(message):
                        raise NeoAuthError(f"Kotak Neo {method} failed: {error}") from error
                    log.info("Session for %s expired during %s; re-authenticating", account_id, method)
                    self.invalidate(account_id)
                    continue

                failure = extract_error(result)
                if failure is None:
                    return result
                if attempt == 2 or not _is_session_error(failure):
                    raise NeoAuthError(f"Kotak Neo {method} failed: {failure}")
                log.info("Stale session reported by %s for %s; re-authenticating", method, account_id)
                self.invalidate(account_id)
            return result

    def invalidate(self, account_id: str) -> None:
        with self._lock_for(account_id):
            self._sessions.pop(account_id, None)

    def logout(self, account_id: str, credentials: BrokerCredentials | None) -> Any:
        with self._lock_for(account_id):
            session = self._sessions.get(account_id)
            if session is None:
                return {"status": "no-session"}
            try:
                return session.client.logout()
            except Exception as error:  # noqa: BLE001
                log.info("Neo logout for %s failed (ignored): %s", account_id, error)
                return {"status": "error", "message": str(error)}
            finally:
                self._sessions.pop(account_id, None)

    def health(self, account_id: str) -> dict[str, Any]:
        session = self._sessions.get(account_id)
        if session is None:
            return {
                "account_id": account_id, "authenticated": False, "status": "disconnected",
                "last_error": self._last_error.get(account_id),
            }
        payload = session.status()
        payload.update({"authenticated": True, "status": "live", "last_error": self._last_error.get(account_id)})
        return payload

    def keepalive(self, resolver) -> None:
        """Background loop that keeps every known account logged in."""
        while True:
            for account_id, credentials in resolver().items():
                try:
                    self.get(account_id, credentials)
                except NeoAuthError as error:
                    log.warning("Keepalive could not refresh %s: %s", account_id, error)
                except Exception:  # noqa: BLE001
                    log.exception("Unexpected keepalive failure for %s", account_id)
            time.sleep(settings.keepalive_seconds)

    def start_keepalive(self, resolver) -> threading.Thread:
        if not settings.keepalive_enabled:
            return None
        thread = threading.Thread(target=self.keepalive, args=(resolver,), name="neo-keepalive", daemon=True)
        thread.start()
        return thread


session_manager = NeoSessionManager()
