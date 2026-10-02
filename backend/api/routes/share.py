"""Share links and QR codes: open the terminal on any device.

The API resolves the public URL for itself rather than trusting configuration:

1. ``ALPHATRADE_PUBLIC_URL`` when set — the canonical cloud address.
2. Otherwise the request's own forwarded host/proto, so an nginx reverse proxy,
   a Fly/Railway hostname, or a LAN address all work with no configuration.

The QR payload is a plain URL. Scanning it opens the site in the phone's own
camera or browser, and because the app is a PWA it can then be installed to the
home screen.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response

from backend.core.config import settings

log = logging.getLogger("alphatrade.share")

router = APIRouter(prefix="/api/v1/share", tags=["share"])


def public_base_url(request: Request) -> str:
    """Best guess of the address a phone should open.

    Behind a reverse proxy the host the client actually used arrives in
    ``X-Forwarded-Host``; trusting that means a phone on mobile data and a laptop
    on the LAN both get a link that works.
    """
    configured = os.getenv("ALPHATRADE_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured

    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host") or "127.0.0.1:8000"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme
    return f"{scheme}://{host}"


def _web_url(base: str, path: str = "") -> str:
    """Resolve a terminal page URL, honouring a separately hosted web app."""
    target = os.getenv("ALPHATRADE_WEB_URL", "").strip().rstrip("/")
    if not target:
        return f"{base}{path or '/'}"
    return f"{target}{'' if path in ('', '/') else path}"


def _qr_svg(payload: str) -> str:
    """QR code as SVG.

    The SVG image factory is pure Python, so this never needs Pillow.
    """
    try:
        import qrcode  # type: ignore
        import qrcode.image.svg  # type: ignore  # noqa: F401 - registers the factory
    except ImportError:
        return ""
    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgImage, box_size=12, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def _qr_png(payload: str) -> bytes:
    import qrcode  # type: ignore

    buffer = io.BytesIO()
    qrcode.make(payload, box_size=12, border=2).save(buffer, format="PNG")
    return buffer.getvalue()


@router.get("")
def share(
    request: Request,
    symbol: str | None = Query(default=None),
    origin: str | None = Query(default=None),
) -> dict[str, Any]:
    """Device-agnostic links plus the QR payload for this deployment.

    `origin` is the terminal's own address, supplied by the browser. It is
    preferred when the web app and the API are on different hosts, so the QR
    always points at the page the user is actually looking at rather than at
    the API's port.
    """
    api_base = public_base_url(request)
    base = (origin or "").strip().rstrip("/") or api_base
    web = _web_url(base, "")

    query: dict[str, str] = {}
    if symbol:
        query["symbol"] = symbol.strip().upper()

    links: dict[str, str] = {
        "terminal": web,
        "sign_in": _web_url(base, "/login"),
        "dashboard": _web_url(base, "/dashboard"),
        "market": _web_url(base, "/market"),
        "orders": _web_url(base, "/trade"),
        "option_chain": _web_url(base, "/options"),
        "api_health": f"{api_base}/api/v1/system/health",
    }
    if query:
        links["deep_link"] = f"{web}?{urlencode(query)}"

    payload = links.get("deep_link", links["sign_in"])
    if origin:
        source = "browser"
    elif os.getenv("ALPHATRADE_PUBLIC_URL"):
        source = "env"
    else:
        source = "request"

    return {
        "base_url": base,
        "api_base_url": api_base,
        "links": links,
        "query": query,
        "qr_payload": payload,
        "qr_endpoint": f"{api_base}/api/v1/share/qr.png" + (f"?{urlencode(query)}" if query else ""),
        "configured_from": source,
        "note": (
            "Scan with the phone camera to open the terminal in the browser, then use "
            "Add to Home Screen to install it as an app."
        ),
    }


@router.get("/qr.png")
def share_qr(
    request: Request,
    symbol: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    svg: bool = Query(default=False),
) -> Response:
    """Scan-to-open QR image. `?svg=1` returns inline SVG instead of PNG."""
    payload = share(request, symbol, origin)["qr_payload"]

    if svg:
        markup = _qr_svg(payload)
        if markup:
            return Response(
                content=markup,
                media_type="image/svg+xml",
                headers={"Cache-Control": "no-store", "X-QR-Payload": payload},
            )

    try:
        png = _qr_png(payload)
    except ImportError:
        # Keep the route valid without the optional dependency rather than 500ing.
        return Response(
            content=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
                '<rect width="200" height="200" fill="#0E131C" stroke="#3B82F6" stroke-width="4"/>'
                '<text x="100" y="104" font-size="11" text-anchor="middle" fill="#E8EDF6">'
                "install qrcode</text></svg>"
            ),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store", "X-QR-Fallback": "1", "X-QR-Payload": payload},
        )
    except Exception as error:  # noqa: BLE001
        log.warning("QR rendering failed: %s", error)
        return Response(content=b"", status_code=500, media_type="text/plain")

    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-QR-Payload": payload},
    )
