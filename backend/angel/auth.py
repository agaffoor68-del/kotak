"""Angel One SmartAPI authentication and session lifecycle.

Angel's login is a three-step, TOTP-based flow:

1. ``POST /rest/auth/angelbroking/generate/token`` with
   ``{clientcode, password, totp}`` returns a JWT *and* a refresh token.
   The "password" is the 4-digit account PIN on the current API.
2. ``POST /rest/auth/angelbroking/jwt/v1/generate/token`` (the *refresh*
   endpoint) with ``{refreshToken}`` mints a new JWT with no TOTP, so the
   session can be kept alive cheaply until the refresh token itself expires.
3. ``GET /rest/secure/angelbroking/user/v1/logout`` invalidates the session.

The access and refresh tokens are persisted to ``ANGEL_SESSION_FILE`` so a
process restart does not force a fresh TOTP login, and a keepalive thread
refreshes the JWT on an interval so a long-running market-data feed never
lapses mid-session.

The headers Angel requires on every secured call are built here too, because
getting them wrong (`X-UserType`, `X-SourceID`, `X-ClientLocalIP`, …) produces
opaque failures that look like credential problems.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from backend.core.config import settings
from backend.core.vault import AngelCredentials


def _public_ip() -> str:
    """Return the host's real egress IP for SmartAPI's required header."""
    try:
        from backend.deployment import detect_public_ip

        detected = detect_public_ip().get("ip")
        if detected:
            return str(detected)
    except Exception:  # noqa: BLE001 - header generation must remain best-effort
        log.debug("Could not detect public egress IP", exc_info=True)
    return _local_ip()

log = logging.getLogger("alphatrade.angel")

#: SmartAPI REST bases. Angel serves one prod host; `uat` is for sandbox keys.
ENDPOINTS = {
    "prod": "https://apiconnect.angelone.in",
    "uat": "https://apiconnect.angelbroking.com",
}

LOGIN_PATH = "/rest/auth/angelbroking/generate/token"
REFRESH_PATH = "/rest/auth/angelbroking/jwt/v1/generate/token"
LOGOUT_PATH = "/rest/secure/angelbroking/user/v1/logout"
PROFILE_PATH = "/rest/secure/angelbroking/user/v1/getProfile"
RMS_PATH = "/rest/secure/angelbroking/user/v1/getRMS"

DEFAULT_TIMEOUT = 30.0
#: A SmartAPI access token is documented as valid for ~24h; refresh well inside.
JWT_REFRESH_SECONDS = 55 * 60
KEEPALIVE_SECONDS = 15 * 60
#: SmartAPI can answer 200 with an empty/non-JSON body or an HTML WAF page.
#: Every request path retries the initial request plus three delayed attempts.
BLANK_BODY_RETRIES = 3
BLANK_BODY_BACKOFF_SECONDS = 2.0


def _proxy_kwargs() -> dict[str, Any]:
    """Pass the configured HTTP(S) proxies through to every SmartAPI request."""
    http_proxy = os.getenv("HTTP_PROXY", "").strip()
    https_proxy = os.getenv("HTTPS_PROXY", "").strip()
    legacy_proxy = os.getenv("ANGEL_PROXY_URL", "").strip()
    if legacy_proxy:
        http_proxy = http_proxy or legacy_proxy
        https_proxy = https_proxy or legacy_proxy
    if not http_proxy and not https_proxy:
        return {}
    return {
        "proxies": {
            "http": http_proxy or https_proxy,
            "https": https_proxy or http_proxy,
        }
    }


def _common_headers(private_key: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientPublicIP": "200.141.10.11",
        "X-ClientLocalIP": "127.0.0.1",
        "X-MACAddress": "00:1A:2B:3C:4D:5E",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if private_key:
        headers["X-PrivateKey"] = private_key
    return headers


class AngelAuthError(RuntimeError):
    """Any failure while authenticating with Angel One SmartAPI."""


class AngelSessionExpired(AngelAuthError):
    """The token is no longer usable and the session must be rebuilt."""


def _explode(message: str) -> bool:
    """Heuristic: does Angel's message mean the session/token has lapsed?"""
    lowered = (message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "invalid token", "token expired", "session expired", "ag8001", "ag8002",
            "ag8003", "unauthor", "jwt", "access denied", "session", "not a valid token",
        )
    )


def _describe_non_json(response: requests.Response) -> str:
    """Explain a SmartAPI reply that was not JSON, with enough to act on.

    Angel's edge answers some rejections (a bad ``X-PrivateKey``, a WAF block)
    with ``HTTP 200`` and a non-JSON body — commonly ``text/html`` and empty. That
    used to collapse into a bare "non-JSON body", which hid the real cause and
    also defeated the token-expiry heuristic in :meth:`AngelSessionManager.call`.
    Surface the status, the content type and a snippet of the body instead.
    """
    content_type = (response.headers.get("content-type") or "unknown").split(";")[0].strip()
    body = (response.text or "").strip()
    if not body:
        detail = (
            "the body was empty (an empty HTML reply is usually Angel's edge/WAF "
            "answering instead of the API — transient at market open, or a "
            "blocked request/IP)"
        )
    else:
        detail = f"the body began with {body[:120]!r}"
    return (
        f"SmartAPI returned a non-JSON body (HTTP {response.status_code}, "
        f"content-type {content_type}); {detail}"
    )


def _is_transient_blank(response: requests.Response) -> bool:
    """Is this the flaky "HTTP 200 but no JSON" reply that is worth retrying?

    Angel's WAF can return ``200`` with an HTML page instead of JSON. Empty,
    HTML, and otherwise unparseable 200 replies are retried because the caller
    explicitly requested recovery from this edge response.
    """
    if response.status_code != 200:
        return False
    body = (response.text or "").strip()
    if not body:
        return True
    content_type = (response.headers.get("content-type") or "").lower()
    if "html" in content_type or "<html" in body[:400].lower():
        return True
    try:
        json.loads(body)
    except ValueError:
        return True
    return False


def _send_with_blank_retry(send: Callable[[], requests.Response], label: str) -> requests.Response:
    """Run one SmartAPI request, retrying the flaky "200 but no JSON" reply.
    Shared by login, refresh, profile and secured calls. The final response is
    returned as-is so the caller can build the descriptive error.

    """
    response = send()
    for attempt in range(BLANK_BODY_RETRIES):
        if not _is_transient_blank(response):
            return response
        log.warning(
            "SmartAPI %s returned a blank/non-JSON body; retry %s/%s in %.2fs",
            label, attempt + 1, BLANK_BODY_RETRIES, BLANK_BODY_BACKOFF_SECONDS,
        )
        time.sleep(BLANK_BODY_BACKOFF_SECONDS)
        response = send()
    return response


def _local_ip() -> str:
    """Best-effort private address, as SmartAPI wants it in a header."""
    try:
        socket_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            socket_.connect(("8.8.8.8", 80))
            return socket_.getsockname()[0]
        finally:
            socket_.close()
    except OSError:
        return "127.0.0.1"


@dataclass
class AngelSession:
    """One authenticated SmartAPI session plus its lifecycle metadata."""

    cred: AngelCredentials
    access_token: str
    refresh_token: str | None
    feed_token: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    authenticated_at: float = field(default_factory=time.time)
    refreshed_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.authenticated_at

    @property
    def seconds_since_refresh(self) -> float:
        return time.time() - self.refreshed_at

    def headers(self) -> dict[str, str]:
        """Return the required SmartAPI headers plus this session's credentials."""
        headers = _common_headers(self.cred.api_key)
        headers.update({
            "Authorization": f"Bearer {self.access_token}",
        })
        return headers

    def status(self) -> dict[str, Any]:
        return {
            "client_code": self.cred.client_code,
            "environment": self.cred.environment,
            "authenticated_at": self.authenticated_at,
            "age_seconds": round(self.age_seconds),
            "feed_token": bool(self.feed_token),
            "profile": self.profile or None,
        }


def base_url(environment: str | None = None) -> str:
    return ENDPOINTS.get((environment or settings.angel_environment or "prod").lower(), ENDPOINTS["prod"])


class AngelSessionManager:
    """Thread-safe, self-refreshing pool of SmartAPI sessions, one per account."""

    def __init__(self) -> None:
        self._sessions: dict[str, AngelSession] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._last_error: dict[str, str] = {}
        self._session_file = settings.angel_session_file
        self._persisted = self._load_persisted()

    # -- persistence -----------------------------------------------------
    def _load_persisted(self) -> dict[str, Any]:
        try:
            with open(self._session_file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _persist(self) -> None:
        payload = {
            key: {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "feed_token": session.feed_token,
                "environment": session.cred.environment,
                "saved_at": time.time(),
            }
            for key, session in self._sessions.items()
        }
        try:
            os.makedirs(os.path.dirname(self._session_file), exist_ok=True)
            temp = f"{self._session_file}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temp, self._session_file)
        except OSError as error:  # a read-only disk must not break trading
            log.debug("Could not persist the Angel session: %s", error)

    # -- internals -------------------------------------------------------
    def _lock_for(self, account_id: str) -> threading.RLock:
        with self._guard:
            if account_id not in self._locks:
                self._locks[account_id] = threading.RLock()
            return self._locks[account_id]

    @staticmethod
    def _unwrap(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise AngelAuthError(_describe_non_json(response)) from None
        if not isinstance(payload, dict):
            raise AngelAuthError(f"SmartAPI returned an unexpected payload: {str(payload)[:200]}")
        if payload.get("status") is False or payload.get("success") is False:
            message = payload.get("message") or payload.get("errorcode") or "SmartAPI rejected the request"
            raise AngelAuthError(str(message))
        return payload

    def _data(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _establish(self, credentials: AngelCredentials) -> AngelSession:
        try:
            import pyotp
        except ImportError:  # pragma: no cover - declared in requirements
            raise AngelAuthError("The `pyotp` package is required for Angel One TOTP login") from None

        url = f"{base_url(credentials.environment)}{LOGIN_PATH}"
        body = {
            "clientcode": credentials.client_code,
            "password": credentials.pin,
            "totp": pyotp.TOTP(credentials.totp_key).now(),
        }
        headers = _common_headers(credentials.api_key)
        try:
            response = _send_with_blank_retry(
                lambda: requests.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT, **_proxy_kwargs()),
                "login",
            )
        except requests.RequestException as error:
            raise AngelAuthError(f"Could not reach SmartAPI at {url}: {error}") from error

        if response.status_code >= 400:
            raise AngelAuthError(f"SmartAPI login failed (HTTP {response.status_code}): {response.text[:200]}")

        data = self._data(self._unwrap(response))
        access_token = data.get("jwtToken") or data.get("access_token")
        if not access_token:
            raise AngelAuthError("SmartAPI login returned no JWT")

        session = AngelSession(
            cred=credentials,
            access_token=str(access_token),
            refresh_token=data.get("refreshToken") or data.get("refresh_token"),
            feed_token=data.get("feedToken") or data.get("feed_token"),
        )
        session.profile = self._fetch_profile(session)
        return session

    def _fetch_profile(self, session: AngelSession) -> dict[str, Any]:
        try:
            response = _send_with_blank_retry(
                lambda: requests.get(
                    f"{base_url(session.cred.environment)}{PROFILE_PATH}",
                    headers=session.headers(),
                    timeout=DEFAULT_TIMEOUT,
                    **_proxy_kwargs(),
                ),
                "profile",
            )
            return self._data(self._unwrap(response))
        except (AngelAuthError, requests.RequestException) as error:
            log.debug("Angel profile lookup skipped: %s", error)
            return {}

    def _refresh(self, session: AngelSession) -> AngelSession:
        """Mint a new JWT from the refresh token, without a fresh TOTP."""
        if not session.refresh_token:
            raise AngelSessionExpired("No refresh token; a full login is required")
        url = f"{base_url(session.cred.environment)}{REFRESH_PATH}"
        headers = _common_headers(session.cred.api_key)
        try:
            response = _send_with_blank_retry(
                    lambda: requests.post(
                    url, json={"refreshToken": session.refresh_token}, headers=headers, timeout=DEFAULT_TIMEOUT,
                    **_proxy_kwargs(),
                ),
                "refresh",
            )
            data = self._data(self._unwrap(response))
        except (AngelAuthError, requests.RequestException) as error:
            raise AngelSessionExpired(f"SmartAPI refresh failed: {error}") from error
        token = data.get("jwtToken") or data.get("access_token")
        if not token:
            raise AngelSessionExpired("SmartAPI refresh returned no JWT")
        session.access_token = str(token)
        session.refreshed_at = time.time()
        return session

    # -- public API ------------------------------------------------------
    def get(self, account_id: str, credentials: AngelCredentials | None = None) -> AngelSession:
        """Return a valid session, logging in or refreshing when required."""
        with self._lock_for(account_id):
            session = self._sessions.get(account_id)
            if session is not None and session.seconds_since_refresh < JWT_REFRESH_SECONDS:
                return session
            if session is not None:
                try:
                    return self._refresh(session)
                except AngelSessionExpired:
                    log.info("Angel refresh token for %s lapsed; logging in again", account_id)
                    self._sessions.pop(account_id, None)

            if credentials is None:
                raise AngelAuthError(f"No credentials available for Angel account {account_id}")
            session = self._establish(credentials)
            self._sessions[account_id] = session
            self._last_error.pop(account_id, None)
            self._persist()
            log.info("Angel One session established for %s", account_id)
            return session

    def call(
        self,
        account_id: str,
        credentials: AngelCredentials | None,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        http_method: str = "POST",
        retry: bool = True,
    ) -> dict[str, Any]:
        """Perform one authenticated SmartAPI request, re-logging in on expiry."""
        with self._lock_for(account_id):
            for attempt in (1, 2):
                if credentials is None:
                    raise AngelAuthError(f"No credentials available for Angel account {account_id}")
                session = self.get(account_id, credentials)
                url = f"{base_url(session.cred.environment)}{path}"
                # A 200 with an empty/unparseable body is the flaky market-open
                # reply; the helper retries it rather than surfacing a phantom failure.
                try:
                    response = _send_with_blank_retry(
                        lambda: requests.request(
                            http_method.upper(),
                            url,
                            headers=session.headers(),
                            params=params,
                            json=payload,
                            timeout=DEFAULT_TIMEOUT,
                            **_proxy_kwargs(),
                        ),
                        method,
                    )
                except requests.RequestException as error:
                    raise AngelAuthError(f"SmartAPI {method} transport failed: {error}") from error

                if response.status_code in (401, 403) or not response.ok:
                    text = response.text[:200]
                    if retry and attempt == 1 and _explode(text):
                        log.info("Angel session lapsed during %s; re-authenticating", method)
                        self._sessions.pop(account_id, None)
                        continue
                    raise AngelAuthError(f"SmartAPI {method} failed (HTTP {response.status_code}): {text}")

                try:
                    return self._unwrap(response)
                except AngelAuthError as error:
                    if retry and attempt == 1 and _explode(str(error)):
                        log.info("Angel rejected %s as expired; re-authenticating", method)
                        self._sessions.pop(account_id, None)
                        continue
                    raise
            raise AngelAuthError(f"SmartAPI {method} failed after re-authentication")

    def invalidate(self, account_id: str) -> None:
        with self._lock_for(account_id):
            self._sessions.pop(account_id, None)

    def logout(self, account_id: str) -> dict[str, Any]:
        """Invalidate the SmartAPI session (credentials are not needed again)."""
        with self._lock_for(account_id):
            session = self._sessions.get(account_id)
            if session is None:
                return {"status": "no-session"}
            try:
                return self.call(account_id, session.cred, "logout", LOGOUT_PATH, payload={}, http_method="POST", retry=False)
            except AngelAuthError as error:
                log.info("Angel logout for %s failed (ignored): %s", account_id, error)
                return {"status": "error", "message": str(error)}
            finally:
                self._sessions.pop(account_id, None)

    def health(self, account_id: str) -> dict[str, Any]:
        session = self._sessions.get(account_id)
        if session is None:
            return {
                "broker": "angel", "account_id": account_id, "authenticated": False,
                "status": "disconnected", "last_error": self._last_error.get(account_id),
            }
        payload = session.status()
        payload.update({"broker": "angel", "account_id": account_id, "authenticated": True, "status": "live"})
        return payload

    def probe(self, credentials: AngelCredentials) -> dict[str, Any]:
        """Log in once, outside the pool, to verify credentials.

        Used by the connect panel so a typo is reported at the form with
        Angel's own message, rather than surfacing later as a data outage.
        """
        started = time.time()
        try:
            session = self._establish(credentials)
        except AngelAuthError as error:
            return {"ok": False, "error": str(error), "seconds": round(time.time() - started, 2)}
        except Exception as error:  # noqa: BLE001 - a transport failure must not become a 500
            return {"ok": False, "error": f"{type(error).__name__}: {error}", "seconds": round(time.time() - started, 2)}
        return {"ok": True, "error": None, "seconds": round(time.time() - started, 2), "profile": session.profile or None}

    def keepalive(self, resolver: Callable[[], dict[str, Any]]) -> None:
        """Background loop that keeps every known Angel session fresh."""
        while True:
            for account_id, credentials in resolver().items():
                try:
                    self.get(account_id, credentials)
                except AngelAuthError as error:
                    log.warning("Angel keepalive could not refresh %s: %s", account_id, error)
                except Exception:  # noqa: BLE001
                    log.exception("Unexpected Angel keepalive failure for %s", account_id)
            time.sleep(KEEPALIVE_SECONDS)

    def start_keepalive(self, resolver: Callable[[], dict[str, Any]]) -> threading.Thread | None:
        if not settings.keepalive_enabled:
            return None
        thread = threading.Thread(target=self.keepalive, args=(resolver,), name="angel-keepalive", daemon=True)
        thread.start()
        return thread


angel_session_manager = AngelSessionManager()
