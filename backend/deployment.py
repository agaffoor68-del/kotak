"""Deployment facts an operator must register with a broker: static IP + redirect URL.

Two things about a deployment cannot be inferred from the code, and both are
prerequisites for a broker to accept live trading:

1. **A static public IP.** SEBI requires brokers to see a fixed source address
   on order APIs. An AWS Elastic IP, a DigitalOcean Reserved IP or a GCP static
   IP gives one; a plain VM address does not survive a stop/start. This module
   detects the address the host is actually egressing from and compares it with
   the address recorded in ``STATIC_IP``, so a mismatch (a re-issued dynamic
   address, or a box that moved) is visible in the admin panel *before* orders
   start failing at the broker.

2. **A redirect URL.** Broker OAuth / API authorisation flows send the user
   back to a URL that must be pre-registered and publicly resolvable. It is
   derived from configuration and, when nothing is set, from the request's own
   forwarded host — the same resolution the share links use, so a proxy, a Fly
   hostname or a LAN address all work without being spelled out.

Nothing here guesses. When the egress IP cannot be determined the result says so
rather than reporting the private address as if it were public.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

from backend.core.config import settings

log = logging.getLogger("alphatrade.deployment")

_cache_lock = threading.Lock()
_cached_ip: tuple[float, str | None, str | None] = (0.0, None, None)

#: Addresses that can never be a registered static IP.
PRIVATE_MARKERS = ("10.", "192.168.", "127.", "169.254.", "172.16.", "172.17.", "172.18.")


def _is_public(address: str | None) -> bool:
    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved)


def _local_address() -> str | None:
    """The host's own address on its primary interface (not the egress one)."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None


def detect_public_ip(*, force: bool = False) -> dict[str, Any]:
    """The address this host reaches the internet from, cached briefly.

    Returns ``{ip, source, error, checked_at}``. `source` is one of
    ``override`` (set explicitly), ``remote`` (asked a public echo service) or
    ``unavailable`` — never a private address dressed up as public.
    """
    global _cached_ip

    with _cache_lock:
        cached_at, cached_value, cached_error = _cached_ip
        if not force and cached_value and time.time() - cached_at < settings.public_ip_cache_seconds:
            return {"ip": cached_value, "source": "remote", "error": None, "checked_at": cached_at}

    if settings.public_ip_override:
        result = {
            "ip": settings.public_ip_override,
            "source": "override",
            "error": None,
            "checked_at": time.time(),
        }
    elif not settings.public_ip_check_enabled:
        result = {"ip": None, "source": "disabled", "error": None, "checked_at": time.time()}
    else:
        try:
            response = requests.get(settings.public_ip_check_url, timeout=5)
            response.raise_for_status()
            address = response.text.strip()
            if _is_public(address):
                result = {"ip": address, "source": "remote", "error": None, "checked_at": time.time()}
            else:
                result = {
                    "ip": None, "source": "unavailable", "checked_at": time.time(),
                    "error": f"{settings.public_ip_check_url} returned {address!r}, which is not a public address",
                }
        except requests.RequestException as error:
            result = {
                "ip": None, "source": "unavailable", "checked_at": time.time(),
                "error": f"Could not determine the public IP: {error}",
            }

    with _cache_lock:
        _cached_ip = (result["checked_at"], result["ip"], result["error"])
    return result


def _scheme_for(request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return forwarded or request.url.scheme or "http"


def _host_for(request) -> str:
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    return forwarded or request.headers.get("host") or "127.0.0.1:8000"


def public_base_url(request) -> str:
    """The origin a broker should be told about."""
    if settings.public_url:
        return settings.public_url
    return f"{_scheme_for(request)}://{_host_for(request)}"


def redirect_url(request=None) -> str:
    """The redirect endpoint a broker must be given.

    Explicit ``REDIRECT_URL`` wins; otherwise it is ``PUBLIC_URL`` + path, and
    failing that the request's own origin + path — so a proxy deployment needs
    no extra configuration.
    """
    if settings.redirect_url:
        return settings.redirect_url
    if request is not None:
        return f"{public_base_url(request)}{settings.redirect_path}"
    if settings.public_url:
        return f"{settings.public_url}{settings.redirect_path}"
    return settings.redirect_path


def redirect_urls(request=None) -> dict[str, Any]:
    """Every redirect target, split by what the broker needs to register.

    Production is the HTTPS origin; development is the loopback one. A broker
    portal usually accepts one, so the panel shows both and says which to paste.
    """
    production = redirect_url(request)
    parsed = urlparse(production)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    development = f"http://127.0.0.1:{8000 if port in (80, 443) else port}{settings.redirect_path}"
    return {
        "path": settings.redirect_path,
        "production": production,
        "development": development,
        "allowlist": settings.redirect_allowlist,
        "scheme": parsed.scheme,
        "host": host,
        "https": parsed.scheme == "https",
        "configured": bool(settings.redirect_url or settings.public_url),
    }


def static_ip_report(request=None, *, force: bool = False) -> dict[str, Any]:
    """Everything the admin panel needs to register the IP with a broker."""
    detected = detect_public_ip(force=force)
    registered = settings.registered_static_ip or None
    observed = detected["ip"]

    if not registered:
        status = "unregistered"
        message = (
            "No static IP is recorded. Register this host's public IP with the broker, "
            "then set STATIC_IP to that address so the platform can watch for drift."
        )
    elif observed is None:
        status = "unknown"
        message = (
            "A static IP is recorded but the platform could not verify it right now. "
            f"Broker sees: {registered}. {detected['error'] or ''}".strip()
        )
    elif observed == registered:
        status = "verified"
        message = f"The public IP matches the registered address ({registered})."
    else:
        status = "mismatch"
        message = (
            f"The host is egressing from {observed} but {registered} is registered with the broker. "
            "Orders will be rejected until they match — check the Elastic/Reserved IP is still attached."
        )

    return {
        "registered_ip": registered,
        "detected_ip": observed,
        "detection_source": detected["source"],
        "detection_error": detected["error"],
        "checked_at": detected["checked_at"],
        "reserved": settings.static_ip_is_reserved,
        "status": status,
        "message": message,
        "private_address": _local_address(),
        "request_origin": public_base_url(request) if request is not None else None,
        "note": (
            "Attach an Elastic IP (AWS), Reserved IP (DigitalOcean) or static IP (GCP) so the "
            "address survives a stop/start and a redeploy. A dynamic address breaks SEBI compliance "
            "the first time the instance moves."
        ),
    }


def sebi_checklist(request=None, *, force: bool = False) -> list[dict[str, Any]]:
    """The deployment prerequisites for live order APIs, as pass/fail rows."""
    ip = static_ip_report(request, force=force)
    redirects = redirect_urls(request)
    origin = public_base_url(request) if request is not None else settings.public_url

    return [
        {
            "id": "static_ip_registered",
            "label": "Static IP recorded",
            "ok": bool(ip["registered_ip"]),
            "detail": ip["registered_ip"] or "Set STATIC_IP to the address registered with the broker.",
        },
        {
            "id": "static_ip_reserved",
            "label": "Address is reserved (survives stop/start)",
            "ok": settings.static_ip_is_reserved,
            "detail": "Set STATIC_IP_RESERVED=1 once the Elastic/Reserved IP is attached.",
        },
        {
            "id": "static_ip_matches",
            "label": "Detected IP matches the registered one",
            "ok": ip["status"] == "verified",
            "detail": ip["message"],
        },
        {
            "id": "redirect_configured",
            "label": "Redirect URL configured",
            "ok": redirects["configured"],
            "detail": f"Set PUBLIC_URL or REDIRECT_URL. Currently: {redirects['production']}",
        },
        {
            "id": "redirect_https",
            "label": "Redirect URL is HTTPS",
            "ok": redirects["https"],
            "detail": redirects["production"] if redirects["https"] else "Broker callbacks must be HTTPS in production.",
        },
        {
            "id": "https_required",
            "label": "HTTPS enforced",
            "ok": settings.require_https,
            "detail": "Set REQUIRE_HTTPS=1 so the redirect is rejected over plain HTTP.",
        },
    ]


def deployment_report(request=None, *, force: bool = False) -> dict[str, Any]:
    """The single payload the admin panel renders."""
    checklist = sebi_checklist(request, force=force)
    return {
        "static_ip": static_ip_report(request, force=force),
        "redirect": redirect_urls(request),
        "checklist": checklist,
        "ready_for_live": all(item["ok"] for item in checklist),
        "environment": settings.environment,
        "generated_at": time.time(),
    }


def clear_cache() -> None:
    global _cached_ip
    with _cache_lock:
        _cached_ip = (0.0, None, None)