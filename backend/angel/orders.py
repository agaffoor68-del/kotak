"""Angel One SmartAPI order surface: place, modify, cancel, read.

This is one half of the dual-broker execution path. The payload shape is
Angel's, and mirrors Kotak's closely enough that the order-service layer can
route the same high-level order to either broker:

* ``placeOrder``  — ``{variety, tradingsymbol, symboltoken, transactiontype,
  exchange, ordertype, producttype, duration, price, squareoff, stoploss,
  quantity}``. ``variety`` is ``NORMAL`` for a plain order, ``STOPLOSS`` when a
  trigger price exists.
* ``modifyOrder`` — ``{variety, orderid, ordertype, producttype, duration,
  price, quantity, ...}``.
* ``cancelOrder`` — ``{variety, orderid}``.

Angel expresses everything with its own vocabularies (``exchange`` uses the
*exch_seg* short codes, ``producttype`` is ``DELIVERY``/``INTRADAY``/…, not
``CNC``/``MIS``), so translation lives in :mod:`backend.execution.brokers` and
the values arrive here already in Angel's language.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("alphatrade.angel.orders")

PLACE_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
MODIFY_PATH = "/rest/secure/angelbroking/order/v1/modifyOrder"
CANCEL_PATH = "/rest/secure/angelbroking/order/v1/cancelOrder"
ORDER_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getOrderBook"
TRADE_BOOK_PATH = "/rest/secure/angelbroking/order/v1/getTradeBook"
POSITIONS_PATH = "/rest/secure/angelbroking/order/v1/getPosition"
HOLDINGS_PATH = "/rest/secure/angelbroking/portfolio/v1/getAllHolding"
RMS_PATH = "/rest/secure/angelbroking/user/v1/getRMS"

#: Angel product vocabularies.
PRODUCT_MAP = {
    "CNC": "DELIVERY",
    "MIS": "INTRADAY",
    "INTRADAY": "INTRADAY",
    "NRML": "CARRYFORWARD",
    "MTF": "DELIVERY",
    "CO": "INTRADAY",
    "BO": "INTRADAY",
}

#: Angel order-type vocabulary. ``L`` is limit, ``MKT`` market.
ORDER_TYPE_MAP = {
    "L": "LIMIT",
    "MKT": "MARKET",
    "SL": "STOPLOSS_LIMIT",
    "SL-M": "STOPLOSS_MARKET",
}

VALIDITY_MAP = {"DAY": "DAY", "IOC": "IOC", "GTD": "GTD"}


class AngelOrderError(RuntimeError):
    """SmartAPI refused an order request."""


def angel_order_type(order_type: str) -> str:
    return ORDER_TYPE_MAP.get(str(order_type).upper(), "MARKET")


def angel_product(product: str) -> str:
    return PRODUCT_MAP.get(str(product).upper(), "INTRADAY")


def angel_validity(validity: str) -> str:
    return VALIDITY_MAP.get(str(validity).upper(), "DAY")


def angel_variety(order: dict[str, Any]) -> str:
    """``STOPLOSS`` when the order carries a trigger, else ``NORMAL``."""
    trigger = order.get("trigger_price")
    try:
        has_trigger = trigger not in (None, "", 0, "0") and float(trigger) > 0
    except (TypeError, ValueError):
        has_trigger = False
    return "STOPLOSS" if has_trigger else "NORMAL"


def build_payload(order: dict[str, Any], *, symbol_token: str, trading_symbol: str) -> dict[str, Any]:
    """Translate a platform order into SmartAPI's placeOrder body."""
    order_type = angel_order_type(order.get("order_type", "MKT"))
    price = float(order.get("price") or 0)
    trigger = float(order.get("trigger_price") or 0)
    payload = {
        "variety": angel_variety(order),
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": "BUY" if str(order.get("transaction_type", "B")).upper() == "B" else "SELL",
        "exchange": _exchange_short(order.get("exchange_segment")),
        "ordertype": order_type,
        "producttype": angel_product(order.get("product", "MIS")),
        "duration": angel_validity(order.get("validity", "DAY")),
        "price": "0" if order_type == "MARKET" else f"{price:.2f}",
        "quantity": str(int(float(order.get("quantity") or 0))),
    }
    if payload["variety"] == "STOPLOSS":
        payload["triggerprice"] = f"{trigger:.2f}"
    if order.get("disclosed_quantity"):
        payload["squareoff"] = "0"
        payload["stoploss"] = "0"
    return payload


def _exchange_short(segment: str | None) -> str:
    from .instruments import SEGMENT_EXCHANGE

    return SEGMENT_EXCHANGE.get(str(segment or "nse_cm").lower(), "NSE")


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def place(
    call: Callable[..., dict[str, Any]],
    order: dict[str, Any],
    *,
    symbol_token: str,
    trading_symbol: str,
) -> dict[str, Any]:
    payload = build_payload(order, symbol_token=symbol_token, trading_symbol=trading_symbol)
    result = call("placeOrder", PLACE_PATH, payload=payload, http_method="POST")
    return {"request": payload, "response": result, "order_id": extract_order_id(result)}


def modify(
    call: Callable[..., dict[str, Any]],
    *,
    order_id: str,
    variety: str = "NORMAL",
    order_type: str = "LIMIT",
    product_type: str = "INTRADAY",
    duration: str = "DAY",
    price: str = "0",
    quantity: str = "0",
    trigger_price: str | None = None,
    symbol_token: str | None = None,
    trading_symbol: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "variety": variety,
        "orderid": order_id,
        "ordertype": order_type,
        "producttype": product_type,
        "duration": duration,
        "price": price,
        "quantity": quantity,
    }
    if trigger_price and float(trigger_price) > 0:
        payload["triggerprice"] = trigger_price
        payload["variety"] = "STOPLOSS"
    result = call("modifyOrder", MODIFY_PATH, payload=payload, http_method="POST")
    return {"request": payload, "response": result}


def cancel(call: Callable[..., dict[str, Any]], *, order_id: str, variety: str = "NORMAL") -> dict[str, Any]:
    payload = {"variety": variety, "orderid": order_id}
    result = call("cancelOrder", CANCEL_PATH, payload=payload, http_method="POST")
    return {"request": payload, "response": result}


def order_book(call: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return _rows(call("orderBook", ORDER_BOOK_PATH, http_method="GET"))


def trade_book(call: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return _rows(call("tradeBook", TRADE_BOOK_PATH, http_method="GET"))


def positions(call: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return _rows(call("positions", POSITIONS_PATH, http_method="GET"))


def holdings(call: Callable[..., dict[str, Any]]) -> Any:
    payload = call("holdings", HOLDINGS_PATH, http_method="GET")
    data = _data(payload)
    return data.get("holdings") or _rows(payload)


def limits(call: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    return _data(call("limits", RMS_PATH, http_method="GET"))


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("orders", "trades", "net", "positions"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def extract_order_id(payload: dict[str, Any]) -> str | None:
    data = _data(payload)
    for key in ("orderid", "orderId", "order_id", "uniqueorderid"):
        if data.get(key):
            return str(data[key])
    for key in ("orderid", "orderId"):
        if isinstance(payload, dict) and payload.get(key):
            return str(payload[key])
    return None


def extract_error(payload: dict[str, Any]) -> str | None:
    """Read an error out of a SmartAPI body, treating `status: false` as one."""
    if not isinstance(payload, dict):
        return str(payload) if payload else None
    if payload.get("status") is False or payload.get("success") is False:
        return str(payload.get("message") or payload.get("errorcode") or "SmartAPI rejected the request")
    return None
