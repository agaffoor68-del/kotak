"""WebSocket endpoints: live tick stream and session events.

Clients connect with a JWT, optionally naming instruments to subscribe to. Ticks
are pushed from the market feed; a slow client has its queue dropped rather than
being allowed to back-pressure the feed for everyone else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from backend.angel.feed import angel_feed
from backend.core.security import decode_access_token
from backend.marketdata import market_hours, master
from backend.angel.breadth import market_breadth_worker
from backend.api.models.market import BreadthData, IndexData, MarketOverview, quote_to_mover
from backend.marketdata.quotes import quote_engine

INDEX_KEYS = [("26000", "nse_cm", "NIFTY"), ("26009", "nse_cm", "BANKNIFTY"), ("26037", "nse_cm", "FINNIFTY"), ("26074", "nse_cm", "MIDCPNIFTY"), ("99919000", "bse_cm", "SENSEX")]


def _market_overview() -> dict[str, Any]:
    indices = []
    for token, segment, symbol in INDEX_KEYS:
        quote = quote_engine.cached(token, segment)
        data = quote.to_dict() if quote else {}
        indices.append(IndexData(symbol=symbol, token=token, exchange_segment=segment, last=data.get("last"), change=data.get("change"), change_percent=data.get("change_percent"), updated_at=data.get("updated_at")))
    snapshot = market_breadth_worker.snapshot() or {}
    raw = snapshot.get("breadth") or {}
    overview = MarketOverview(as_of=float(snapshot.get("as_of") or time.time()), indices=indices, breadth=BreadthData(**{key: value for key, value in raw.items() if key in BreadthData.model_fields}), top_gainers=[quote_to_mover(item) for item in snapshot.get("top_gainers", [])], top_losers=[quote_to_mover(item) for item in snapshot.get("top_losers", [])])
    return overview.model_dump(mode="json")

log = logging.getLogger("alphatrade.ws")

router = APIRouter(tags=["websocket"])

HEARTBEAT_SECONDS = 5


def _authenticate(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        claims = decode_access_token(token)
    except ValueError:
        return None
    return {"user_id": claims.subject, "role": claims.role, "account_id": claims.account_id}


@router.websocket("/ws/market")
async def market_stream(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """Live tick stream.

    Connect as `/ws/market?token=<jwt>`, then send
    `{"action": "subscribe", "tokens": [{"instrument_token": "26000",
    "exchange_segment": "nse_cm"}]}` to start receiving ticks.
    """
    user = _authenticate(token)
    if user is None:
        await websocket.close(code=4401, reason="Authentication required")
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    unregister = angel_feed.register(queue)
    angel_feed.bind_loop(asyncio.get_running_loop())

    async def pump() -> None:
        """Forward broker ticks to this client."""
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # A periodic frame lets the client detect a dead connection.
                await websocket.send_json({
                    "type": "market_overview",
                    "data": _market_overview(),
                })
                await websocket.send_json({
                    "type": "heartbeat",
                    "session": market_hours.session_state(),
                    "feed": angel_feed.health(),
                    "at": time.time(),
                })
                continue
            await websocket.send_json({"type": "quote", "data": payload})

    async def receive() -> None:
        while True:
            message = await websocket.receive_json()
            action = message.get("action")

            if action == "subscribe":
                added = angel_feed.subscribe(message.get("tokens") or [])
                await websocket.send_json({
                    "type": "subscribed", "added": added,
                    "total": len(angel_feed.subscription_tokens()),
                })
            elif action == "unsubscribe":
                removed = angel_feed.unsubscribe(message.get("tokens") or [])
                await websocket.send_json({"type": "unsubscribed", "removed": removed})
            elif action == "status":
                await websocket.send_json({
                    "type": "status", "session": market_hours.session_state(),
                    "feed": angel_feed.health(), "scrip_master": master.status(),
                })
            elif action == "ping":
                await websocket.send_json({"type": "pong"})

    pump_task = asyncio.create_task(pump())
    try:
        await receive()
    except WebSocketDisconnect:
        pass
    except Exception as error:  # noqa: BLE001
        log.debug("WebSocket closed: %s", error)
    finally:
        pump_task.cancel()
        unregister()
