"""Unified Kotak Neo client facade.

Routes all Kotak Neo calls through this facade to consolidate client logic.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("alphatrade.broker.neo_client")


class NeoClient:
    """Thin facade over neo_api_client.NeoAPI."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._session.place_order(payload)
        except Exception:
            logger.exception("neo_client.place_order failed")
            raise

    def modify_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._session.modify_order(payload)
        except Exception:
            logger.exception("neo_client.modify_order failed")
            raise

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._session.cancel_order(payload)
        except Exception:
            logger.exception("neo_client.cancel_order failed")
            raise

    def order_report(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.order_report(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.order_report failed")
            raise

    def trade_report(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.trade_report(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.trade_report failed")
            raise

    def positions(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.positions(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.positions failed")
            raise

    def holdings(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.holdings(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.holdings failed")
            raise

    def limits(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.limits(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.limits failed")
            raise

    def scrip_master(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._session.scrip_master(*args, **kwargs)
        except Exception:
            logger.exception("neo_client.scrip_master failed")
            raise


def get_neo_client(session: Any) -> NeoClient:
    return NeoClient(session)
