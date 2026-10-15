"""FYERS API v3 login flow and daily token lifecycle.

FYERS authorises a **two-step OAuth dance**, and the access token dies at the
end of each trading day:

1. ``/fyers/login`` redirects the operator to FYERS' login page
   (``/api/v3/generate-authcode?client_id&redirect_uri&response_type=code&state``).
2. FYERS redirects back to ``/fyers/callback`` with a one-time ``auth_code``.
3. The code is exchanged (official SDK ``SessionModel.generate_token()``, i.e.
   ``POST /api/v3/validate-authcode`` with ``appIdHash = sha256(app_id:secret)``)
   for an ``access_token`` (+ ``refresh_token``).

The token is persisted **encrypted** (Fernet under ``CREDENTIAL_ENCRYPTION_KEY``,
the same key the broker vault uses) in ``FYERS_TOKEN_FILE`` so a Render restart
does not force a fresh login. When the token ages out, the auth module tries the
documented refresh-token flow once (needs ``FYERS_PIN``) and otherwise reports
exactly what to do: open ``/fyers/login`` again.

Credentials (``FYERS_APP_ID``, ``FYERS_SECRET_ID``, ``FYERS_REDIRECT_URI``,
``FYERS_PIN``) are read from the environment only — never from code.

The routes that drive this live in :mod:`backend.api.routes.fyers`; this module
holds no HTTP server concerns so it can be unit-tested on its own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

import requests

from backend.core.config import settings
from backend.providers.base import ProviderAuthError, ProviderError
from .client import describe_failure, ensure_payload, is_transient

log = logging.getLogger("alphatrade.providers.fyers.auth")

#: REST host for the refresh dance (SDK's Config.API, stated explicitly).
AUTH_BASE = "https://api-t1.fyers.in/api/v3"
REFRESH_PATH = "/validate-refresh-token"
#: How long a generated ``state`` stays valid for the browser round-trip.
STATE_TTL_SECONDS = 900
#: Fallback lifetime when a token carries no decodable expiry.
FALLBACK_TTL_SECONDS = 14 * 3600


def jwt_expiry(token: str) -> float | None:
    """Epoch seconds when a FYERS JWT expires, if it can be decoded."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        exp = claims.get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 - not a JWT (or truncated): caller falls back
        return None


def _fernet():
    """Fernet under the platform key, or ``None`` when no key is configured."""
    if not settings.credential_key:
        return None
    from backend.core.vault import _fernet as vault_fernet

    return vault_fernet()


class FyersAuth:
    """Builds the login URL, exchanges the code, and serves the current token."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, float] = {}
        self._cache: tuple[int, dict[str, Any]] | None = None
        self.last_error: str | None = None
        self._warned_plaintext = False

    # -- configuration ----------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(settings.fyers_app_id and settings.fyers_secret_id)

    @property
    def redirect_uri(self) -> str:
        """The URI registered on the FYERS app (Render callback by default)."""
        explicit = settings.fyers_redirect_uri
        if explicit:
            return explicit
        base = (settings.public_url or "").rstrip("/")
        if not base:
            raise ProviderAuthError(
                "FYERS redirect URI is not configured. Set FYERS_REDIRECT_URI (or PUBLIC_URL) "
                "to the public callback URL registered on your FYERS app."
            )
        return f"{base}{settings.fyers_redirect_path}"

    def _require_configured(self) -> None:
        if not self.configured:
            raise ProviderAuthError(
                "FYERS app credentials are missing. Set FYERS_APP_ID and FYERS_SECRET_ID "
                "in the environment (Render → Environment) and redeploy."
            )

    def app_id_hash(self) -> str:
        self._require_configured()
        raw = f"{settings.fyers_app_id}:{settings.fyers_secret_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # -- CSRF state -------------------------------------------------------

    def new_state(self) -> str:
        """One short-lived state token per login attempt."""
        state = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._states = {k: v for k, v in self._states.items() if v > now}
            self._states[state] = now + STATE_TTL_SECONDS
        return state

    def check_state(self, state: str | None) -> bool:
        with self._lock:
            expiry = self._states.pop(str(state or ""), None)
        return bool(expiry and expiry > time.time())

    # -- step 1: the URL the operator opens -------------------------------

    def authorization_url(self, state: str) -> str:
        """FYERS login URL built with the official SDK (no network call)."""
        self._require_configured()
        from fyers_apiv3 import fyersModel

        session = fyersModel.SessionModel(
            client_id=settings.fyers_app_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            state=state,
            secret_key=settings.fyers_secret_id,
            grant_type="authorization_code",
        )
        return session.generate_authcode()

    # -- step 2: exchange the code for a token ----------------------------

    def exchange_code(self, auth_code: str) -> dict[str, Any]:
        """Trade the one-time ``auth_code`` for an access token and store it."""
        self._require_configured()
        if not str(auth_code or "").strip():
            raise ProviderAuthError("FYERS callback arrived without an auth_code parameter.")

        from fyers_apiv3 import fyersModel

        session = fyersModel.SessionModel(
            client_id=settings.fyers_app_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            state="",
            secret_key=settings.fyers_secret_id,
            grant_type="authorization_code",
        )
        session.set_token(auth_code.strip())

        attempts = max(1, int(settings.fyers_retries))
        backoff = max(0.0, float(settings.fyers_retry_backoff_seconds))
        last: BaseException | None = None
        for attempt in range(attempts):
            try:
                payload = session.generate_token()  # POST /api/v3/validate-authcode
                ensure_payload(payload, "token exchange")
                record = self._record_from(payload)
                self.save(record)
                self.last_error = None
                log.info("FYERS session authorised; access token stored encrypted.")
                return record
            except ProviderAuthError as error:
                self.last_error = str(error)
                raise
            except BaseException as error:  # noqa: BLE001 - classified below
                last = error
                if not is_transient(error) or attempt >= attempts - 1:
                    message = describe_failure("token exchange", error, attempt + 1)
                    self.last_error = message
                    raise ProviderError(message) from error
                delay = backoff * (2 ** attempt)
                if delay:
                    time.sleep(delay)
        raise ProviderError(describe_failure("token exchange", last or RuntimeError("unknown"), attempts))

    def _record_from(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("access_token") or "")
        if not token:
            raise ProviderAuthError(
                f"FYERS token exchange returned no access_token (code {payload.get('code')}: "
                f"{payload.get('message') or payload})"
            )
        now = time.time()
        return {
            "access_token": token,
            "refresh_token": str(payload.get("refresh_token") or ""),
            "obtained_at": now,
            "expires_at": jwt_expiry(token) or (now + FALLBACK_TTL_SECONDS),
            "client_id": settings.fyers_app_id,
        }

    # -- persistence (encrypted at rest) ----------------------------------

    def save(self, record: dict[str, Any]) -> None:
        path = settings.fyers_token_file
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        raw = json.dumps(record, separators=(",", ":")).encode("utf-8")
        cipher = _fernet()
        blob = cipher.encrypt(raw) if cipher else raw
        if cipher is None and not self._warned_plaintext:
            self._warned_plaintext = True
            log.warning(
                "CREDENTIAL_ENCRYPTION_KEY is not set — the FYERS token file will be stored "
                "unencrypted. Configure the key before running on a shared host."
            )
        with open(path, "wb") as handle:
            handle.write(blob)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - platforms without POSIX modes
            pass
        self._cache = None

    def load(self) -> dict[str, Any] | None:
        """The stored token record, decrypted; ``None`` when absent/unreadable."""
        path = settings.fyers_token_file
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            if self._cache and self._cache[0] == mtime:
                return self._cache[1]
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
            cipher = _fernet()
            try:
                raw = cipher.decrypt(blob) if cipher else blob
            except Exception:  # noqa: BLE001 - maybe a plaintext file from a key-less boot
                raw = blob
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict) or not record.get("access_token"):
                raise ValueError("token file has no access_token")
        except Exception as error:  # noqa: BLE001 - an unreadable file is "no session"
            log.warning("Could not read the FYERS token file %s: %s", path, error)
            self.last_error = f"Stored FYERS session could not be read: {error}"
            return None
        with self._lock:
            self._cache = (mtime, record)
        return record

    # -- refresh (best effort) --------------------------------------------

    def refresh(self, record: dict[str, Any]) -> bool:
        """Mint a new access token from the refresh token + PIN, if configured.

        FYERS documents this flow as possibly being discontinued; when it fails
        the caller simply asks for a fresh ``/fyers/login``.
        """
        refresh_token = str(record.get("refresh_token") or "")
        if not refresh_token or not settings.fyers_pin:
            return False
        payload = {
            "grant_type": "refresh_token",
            "appIdHash": self.app_id_hash(),
            "refresh_token": refresh_token,
            "pin": settings.fyers_pin,
        }
        attempts = max(1, int(settings.fyers_retries))
        backoff = max(0.0, float(settings.fyers_retry_backoff_seconds))
        for attempt in range(attempts):
            try:
                response = requests.post(AUTH_BASE + REFRESH_PATH, json=payload, timeout=30)
                data = response.json()  # a non-JSON body is retried below
                ensure_payload(data, "refresh")
                token = str(data.get("access_token") or "")
                if not token:
                    raise ProviderAuthError("FYERS refresh returned no access_token.")
                self.save({
                    "access_token": token,
                    "refresh_token": refresh_token,
                    "obtained_at": time.time(),
                    "expires_at": jwt_expiry(token) or (time.time() + FALLBACK_TTL_SECONDS),
                    "client_id": settings.fyers_app_id,
                })
                self.last_error = None
                log.info("FYERS access token refreshed without a new login.")
                return True
            except ProviderAuthError as error:
                self.last_error = str(error)
                log.warning("FYERS refresh rejected: %s", error)
                return False
            except BaseException as error:  # noqa: BLE001 - classified below
                if not is_transient(error) or attempt >= attempts - 1:
                    self.last_error = describe_failure("refresh", error, attempt + 1)
                    log.warning("FYERS refresh failed: %s", self.last_error)
                    return False
                delay = backoff * (2 ** attempt)
                if delay:
                    time.sleep(delay)
        return False

    # -- current token -----------------------------------------------------

    def get_token(self) -> str | None:
        """The access token to use right now, refreshing/re-logging as needed."""
        record = self.load()
        if record is None and settings.fyers_access_token_env:
            token = settings.fyers_access_token_env
            now = time.time()
            record = {
                "access_token": token,
                "refresh_token": "",
                "obtained_at": now,
                "expires_at": jwt_expiry(token) or (now + FALLBACK_TTL_SECONDS),
                "client_id": settings.fyers_app_id,
            }
        if record is None:
            if self.configured:
                self.last_error = (
                    "FYERS session missing — open /fyers/login once to authorise "
                    "the read-only market-data feed."
                )
            return None

        expires = float(record.get("expires_at") or 0)
        if expires and time.time() < expires:
            return str(record["access_token"])

        # Expired (or about to): try the refresh path, then report re-login.
        if self.refresh(record):
            fresh = self.load()
            if fresh:
                return str(fresh["access_token"])
        stamp = time.strftime("%H:%M IST", time.localtime(expires)) if expires else "earlier today"
        self.last_error = (
            f"FYERS access token expired at {stamp} (tokens reset daily). "
            "Open /fyers/login to re-authorise — takes one click."
        )
        return None

    # -- status for the UI -------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Secret-free session status consumed by ``/fyers/status`` and health."""
        record = self.load()
        now = time.time()
        expires = float(record.get("expires_at") or 0) if record else 0.0
        stale = bool(expires and now >= expires)
        return {
            "name": "fyers",
            "configured": self.configured,
            "authenticated": bool(record) and not stale,
            "token_present": bool(record),
            "expires_at": expires or None,
            "expires_in": int(expires - now) if expires else None,
            "stale": stale,
            "obtained_at": float(record.get("obtained_at") or 0) if record else None,
            "refresh_available": bool(record and record.get("refresh_token") and settings.fyers_pin),
            "redirect_uri": self._redirect_uri_or_none(),
            "login_url": "/fyers/login" if self.configured else None,
            "last_error": self.last_error,
        }

    def _redirect_uri_or_none(self) -> str | None:
        try:
            return self.redirect_uri
        except ProviderAuthError:
            return None


#: One shared auth object per process (the token store is the real state).
fyers_auth = FyersAuth()


