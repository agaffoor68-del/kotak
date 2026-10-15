"""FYERS API v3 REST client built on the official ``fyers-apiv3`` SDK.

Everything the provider asks of FYERS — quotes, historical candles, profile
probes — goes through :class:`FyersClient`, which adds three things the SDK
does not provide on its own:

* **Retry with backoff.** Transient failures (a dropped connection, an HTML
  error page, an empty body) are retried up to ``FYERS_RETRIES`` times with
  exponential backoff before the error reaches the caller.
* **Strict response validation.** FYERS' envelope is ``{"s": "ok" | "error",
  ...}``. A body that is not JSON at all (an HTML page, an empty 200) becomes a
  clear, retryable :class:`ProviderError` instead of a cryptic ``KeyError``.
* **Auth failures are never retried.** An expired access token needs a fresh
  login, not another attempt, so those raise :class:`ProviderAuthError`
  immediately with a message naming the login route.

The client is read-only: it only ever calls the data and profile endpoints.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import requests

from backend.core.config import settings
from backend.providers.base import ProviderAuthError, ProviderError

log = logging.getLogger("alphatrade.providers.fyers")

#: FYERS rejects an access token with these codes; none of them heal on retry.
TOKEN_ERROR_CODES = {-8, -15, -16, -17, 401, 4401}
#: Messages that mean "your session is gone", whatever the code was.
TOKEN_ERROR_MARKERS = (
    "invalid access token",
    "token expired",
    "access token is not valid",
    "unauthorized",
    "invalid token",
    "session expired",
)


def is_transient(error: BaseException) -> bool:
    """Can this failure plausibly succeed on the next attempt?"""
    if isinstance(error, ProviderAuthError):
        return False
    if isinstance(error, ProviderError):
        # Logical FYERS rejections (bad symbol, permission) do not self-heal;
        # only a non-JSON body is worth another try.
        return "non-JSON" in str(error)
    if isinstance(error, requests.exceptions.RequestException):
        return True  # includes JSONDecodeError raised on an HTML body
    if isinstance(error, (json.JSONDecodeError, ValueError)):
        return True  # what requests/SDK raise when the body is not JSON
    return False


def describe_failure(what: str, error: BaseException, attempts: int) -> str:
    """Turn a raw failure into a message the UI can show as-is."""
    if isinstance(error, requests.exceptions.JSONDecodeError) or isinstance(error, json.JSONDecodeError):
        return (
            f"FYERS {what} returned a non-JSON response (an HTML page or an empty body) "
            f"after {attempts} attempts — the request may be blocked or the service degraded."
        )
    if isinstance(error, requests.exceptions.RequestException):
        return f"FYERS {what} could not reach the API after {attempts} attempts: {error}"
    if isinstance(error, ProviderError):
        return f"{error} (after {attempts} attempts)" if attempts > 1 else str(error)
    return f"FYERS {what} failed after {attempts} attempts: {type(error).__name__}: {error}"


def ensure_payload(payload: Any, what: str) -> dict[str, Any]:
    """Validate the FYERS response envelope, raising the right error type."""
    if not isinstance(payload, dict):
        snippet = str(payload)[:160] if payload is not None else ""
        raise ProviderError(
            f"FYERS {what} returned a non-JSON response "
            f"({type(payload).__name__}{': ' + repr(snippet) if snippet else ', empty body'})"
        )
    if payload.get("s") == "ok":
        return payload

    code = payload.get("code")
    message = str(payload.get("message") or payload.get("errmsg") or "").strip()
    lowered = message.lower()
    tokenish = (
        code in TOKEN_ERROR_CODES
        or any(marker in lowered for marker in TOKEN_ERROR_MARKERS)
        or "authoriz" in lowered
    )
    if tokenish:
        raise ProviderAuthError(
            f"FYERS {what}: session expired or invalid (code {code}: {message or 'no detail'}). "
            "Open /fyers/login to re-authorise the read-only market-data session."
        )
    raise ProviderError(f"FYERS {what} failed (code {code}): {message or payload}")


class _RateLimiter:
    """A minimal token bucket so data calls never exceed the configured rate."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / max(0.1, per_second)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self._interval


class FyersClient:
    """Read-only FYERS data calls with retry, backoff and validation."""

    def __init__(self, token_source: Callable[[], str | None]) -> None:
        self._token_source = token_source
        self._limiter = _RateLimiter(settings.fyers_rate_limit_per_second)
        self._lock = threading.Lock()
        self._model: Any = None
        self._model_token: str | None = None
        self._last_error: str | None = None
        self._last_success: float | None = None
        self.calls = 0

    # -- SDK lifecycle ----------------------------------------------------

    def _sdk(self, token: str) -> Any:
        """Build (or rebuild) the SDK client whenever the token changes."""
        from fyers_apiv3 import fyersModel

        if self._model is None or self._model_token != token:
            self._model = fyersModel.FyersModel(
                client_id=settings.fyers_app_id,
                token=token,
                is_async=False,
                log_path=settings.data_dir,
            )
            self._model_token = token
        return self._model

    # -- call machinery ---------------------------------------------------

    def call(self, invoke: Callable[[Any], Any], what: str) -> dict[str, Any]:
        """Run one SDK call: rate-limited, retried, validated.

        ``invoke(model)`` performs the SDK method call. Returns the validated
        FYERS payload; raises :class:`ProviderError` / :class:`ProviderAuthError`
        with a message fit for the UI.
        """
        attempts = max(1, int(settings.fyers_retries))
        backoff = max(0.0, float(settings.fyers_retry_backoff_seconds))
        last_error: BaseException | None = None

        for attempt in range(attempts):
            try:
                token = self._token_source()
                if not token:
                    raise ProviderAuthError(
                        "FYERS access token is missing or expired. "
                        "Open /fyers/login to authorise the read-only market-data session."
                    )
                with self._lock:
                    model = self._sdk(token)
                self._limiter.wait()
                payload = invoke(model)
                validated = ensure_payload(payload, what)
                self.calls += 1
                self._last_success = time.time()
                self._last_error = None
                return validated
            except ProviderAuthError as error:
                self._last_error = str(error)
                log.warning("FYERS %s auth failed: %s", what, error)
                raise
            except BaseException as error:  # noqa: BLE001 - classified below
                last_error = error
                if not is_transient(error) or attempt >= attempts - 1:
                    message = describe_failure(what, error, attempt + 1)
                    self._last_error = message
                    log.warning("FYERS %s failed: %s", what, message)
                    raise ProviderError(message) from error
                delay = backoff * (2 ** attempt)
                log.info("FYERS %s attempt %d failed (%s); retrying in %.1fs", what, attempt + 1, error, delay)
                if delay:
                    time.sleep(delay)

        raise ProviderError(describe_failure(what, last_error or RuntimeError("unknown"), attempts))

    # -- endpoints --------------------------------------------------------

    def quotes(self, symbols: list[str]) -> dict[str, Any]:
        """``GET /data/quotes`` — up to 50 comma-separated symbols."""
        if not symbols:
            return {"s": "ok", "d": []}
        return self.call(
            lambda model: model.quotes(data={"symbols": ",".join(symbols)}),
            what="quotes",
        )

    def history(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``GET /data/history`` — one chunk of OHLCV candles."""
        return self.call(lambda model: model.history(data=payload), what="history")

    def profile(self) -> dict[str, Any]:
        """``GET /api/v3/profile`` — cheap session probe (read-only)."""
        return self.call(lambda model: model.get_profile(), what="profile")

    def health(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "last_success": self._last_success,
            "last_error": self._last_error,
            "rate_limit_per_second": settings.fyers_rate_limit_per_second,
            "max_symbols_per_call": settings.fyers_max_symbols_per_call,
        }
