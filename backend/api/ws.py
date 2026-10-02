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

from backend.core.security import decode_access_token
from backend.marketdata import market_hours, master
from backend.marketdata.feed import market_feed

log = logging.getLogger("alphatrade.ws")

router = APIRouter(tags=["websocket"])

HEARTBEAT_SECONDS = 20


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
    unregister = market_feed.register(queue)
    market_feed.bind_loop(asyncio.get_running_loop())

    async def pump() -> None:
        """Forward broker ticks to this client."""
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # A periodic frame lets the client detect a dead connection.
                await websocket.send_json({
                    "type": "heartbeat",
                    "session": market_hours.session_state(),
                    "feed": market_feed.health(),
                    "at": time.time(),
                })
                continue
            await websocket.send_json({"type": "quote", "data": payload})

    async def receive() -> None:
        while True:
            message = await websocket.receive_json()
            action = message.get("action")

            if action == "subscribe":
                added = market_feed.subscribe(message.get("tokens") or [])
                await websocket.send_json({
                    "type": "subscribed", "added": added,
                    "total": len(market_feed.subscription_tokens()),
                })
            elif action == "unsubscribe":
                removed = market_feed.unsubscribe(message.get("tokens") or [])
                await websocket.send_json({"type": "unsubscribed", "removed": removed})
            elif action == "status":
                await websocket.send_json({
                    "type": "status", "session": market_hours.session_state(),
                    "feed": market_feed.health(), "scrip_master": master.status(),
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
