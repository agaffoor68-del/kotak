"""Deployment routes: the static IP and redirect URL an operator must register.

Two audiences, deliberately separated:

* ``/api/v1/system/deployment`` — the admin panel. Reports the detected public
  IP, the configured static IP, the SEBI checklist and the redirect URLs.
* ``/api/v1/system/deployment/refresh`` — re-detect the egress IP on demand,
  for when a Reserved IP was just attached and the cached value is stale.
* ``/callback`` — the redirect endpoint itself. Broker OAuth / authorisation
  flows land here; it is unauthenticated by necessity (the broker is the
  caller) and therefore validates the target against the allowlist before
  redirecting anywhere, so it cannot be used as an open redirect.

Nothing here mutates infrastructure. Creating an Elastic/Reserved IP is a
cloud-provider action; this surface exists so the address that *resulted* is
visible, verifiable and copy-pasteable onto a broker portal.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response

from backend import deployment
from backend.api.deps import CurrentUser, current_user, require_admin
from backend.core.config import settings

log = logging.getLogger("alphatrade.api.deployment")

router = APIRouter(prefix="/api/v1/system/deployment", tags=["deployment"])
callback_router = APIRouter(tags=["deployment"])


@router.get("")
def deployment_report(
    request: Request,
    refresh: bool = Query(default=False, description="Re-detect the public IP instead of using the cached value"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Static IP + redirect URL status, with the SEBI readiness checklist."""
    return deployment.deployment_report(request, force=refresh)


@router.post("/refresh")
def refresh_detection(
    request: Request,
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Drop the cached IP and detect again (e.g. right after attaching an EIP)."""
    deployment.clear_cache()
    report = deployment.deployment_report(request, force=True)
    log.info("Deployment detection refreshed: static IP status=%s", report["static_ip"]["status"])
    return report


@router.get("/redirect")
def redirect_configuration(
    request: Request,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Just the redirect URLs — the ones to paste into a broker portal."""
    return deployment.redirect_urls(request)


@callback_router.get("/callback", include_in_schema=True)
def broker_callback(
    request: Request,
    code: str | None = Query(default=None, description="Authorization code from the broker"),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    next: str | None = Query(default=None, description="Where to send the browser afterwards"),
) -> Response:
    """The redirect endpoint broker OAuth / authorisation flows return to.

    A broker that only needs to know the URL is registered can be pointed here
    and will see a plain 200. A broker that returns an authorization code gets
    the browser forwarded back into the terminal with the code preserved, so
    the frontend can finish the exchange.

    `next` is validated against ``REDIRECT_ALLOWLIST`` (or the site's own
    origin) before it is honoured: an unvalidated redirect target here would
    let anyone bounce a user to an arbitrary site from a trusted domain.
    """
    origin = deployment.public_base_url(request)

    if error:
        target = _safe_target(next, origin)
        query = urlencode({"error": error, "error_description": error_description or ""})
        return RedirectResponse(f"{target}/connect?{query}", status_code=status.HTTP_302_FOUND)

    if not code:
        # A broker probing the URL, or a human checking it resolves.
        return Response(
            content=(
                "AlphaTradePro redirect endpoint.\n\n"
                "Register this exact URL with the broker. When a broker returns an\n"
                "authorization code here it is forwarded to the terminal automatically.\n"
            ),
            media_type="text/plain",
        )

    target = _safe_target(next, origin)
    query = urlencode({k: v for k, v in (("code", code), ("state", state)) if v})
    log.info("Broker callback received an authorization code; forwarding to %s", target)
    return RedirectResponse(f"{target}/connect?{query}", status_code=status.HTTP_302_FOUND)


def _safe_target(candidate: str | None, origin: str) -> str:
    """Resolve where to send the browser, refusing anything not allowlisted.

    Allowed: an explicit ``REDIRECT_ALLOWLIST`` entry, or the site's own origin
    (so the default flow works with zero configuration).
    """
    origin = origin.rstrip("/")
    if not candidate:
        return origin

    allowed = [origin, *[entry.rstrip("/") for entry in settings.redirect_allowlist]]
    normalised = candidate.rstrip("/")
    if normalised in allowed:
        return normalised

    # Allow a path on the site's own origin (a relative `next`).
    if normalised.startswith("/") and not normalised.startswith("//"):
        return f"{origin}{normalised}"

    log.warning("Refused a callback redirect to %r: not in REDIRECT_ALLOWLIST", candidate)
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "The redirect target is not in REDIRECT_ALLOWLIST. Add it explicitly if it is intended.",
    )
