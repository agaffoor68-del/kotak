"""Alerting: price, indicator and option-chain triggers, delivered by channel.

Channels are optional and independently configured. An alert whose channel is
not set is reported as `skipped` with the reason rather than silently dropped, so
the UI never shows a notification that was never actually sent.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

from backend.core.config import settings
from backend.core.database import app_cursor, now

log = logging.getLogger("alphatrade.alerts")

CONDITION_TYPES = {"above", "below", "crosses_above", "crosses_below", "percent_change", "oi_change"}
KIND_INDICATOR = "indicator"
KIND_OPTION = "option_chain"


class AlertError(ValueError):
    """The alert definition is invalid."""


def _validate(condition: dict[str, Any]) -> None:
    kind = condition.get("type")
    if kind not in CONDITION_TYPES:
        raise AlertError(f"Unknown condition type '{kind}'. Expected one of {sorted(CONDITION_TYPES)}")
    if condition.get("type") in {"above", "below"} and condition.get("value") is None:
        raise AlertError("A price condition needs a 'value' threshold")
    if condition.get("type") in {"percent_change", "oi_change"} and condition.get("percent") is None:
        raise AlertError("A percentage condition needs a 'percent' threshold")


# ------------------------------------------------------------------- channels


def send_telegram(message: str) -> dict[str, Any]:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {"status": "skipped", "reason": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID is not configured"}
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": settings.telegram_chat_id, "text": message}).encode()
    return _post(url, data, content_type="application/x-www-form-urlencoded")


def send_whatsapp(message: str) -> dict[str, Any]:
    if not settings.whatsapp_api_key or not settings.whatsapp_sender:
        return {"status": "skipped", "reason": "WHATSAPP_API_KEY is not configured"}
    # WhatsApp Cloud API shape. The recipient and API host are both configurable
    # so a gateway or a different number works without code changes.
    recipient = settings.whatsapp_recipient or settings.telegram_chat_id
    if not recipient:
        return {"status": "skipped", "reason": "no WhatsApp recipient configured (WHATSAPP_RECIPIENT)"}
    host = settings.whatsapp_api_url or "https://graph.facebook.com/v18.0"
    data = json.dumps({
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": message},
    }).encode()
    return _post(
        f"{host}/{settings.whatsapp_sender}/messages",
        data, content_type="application/json",
        headers={"Authorization": f"Bearer {settings.whatsapp_api_key}"},
    )


def send_email(subject: str, body: str, to: str | None = None) -> dict[str, Any]:
    if not settings.smtp_host or not settings.smtp_user:
        return {"status": "skipped", "reason": "SMTP_HOST / SMTP_USER is not configured"}
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to or settings.smtp_user
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        log.warning("Email alert failed: %s", error)
        return {"status": "failed", "reason": str(error)}
    return {"status": "sent"}


def _post(url: str, data: bytes, *, content_type: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {"status": "sent", "code": response.status}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
        log.warning("Alert delivery to %s failed: %s", url.split("/")[2], error)
        return {"status": "failed", "reason": str(error)}


CHANNELS: dict[str, Callable[[str], dict[str, Any]]] = {
    "telegram": send_telegram,
    "whatsapp": send_whatsapp,
}


# -------------------------------------------------------------------- storage


def create_alert(*, account_id: str, kind: str, condition: dict[str, Any], channels: list[str],
                 symbol: str | None = None) -> dict[str, Any]:
    if kind not in {KIND_INDICATOR, KIND_OPTION, "price"}:
        raise AlertError(f"Unknown alert kind '{kind}'")
    unknown = [channel for channel in channels if channel not in {*CHANNELS, "email"}]
    if unknown:
        raise AlertError(f"Unknown channels: {', '.join(unknown)}")
    _validate(condition)

    alert_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            "INSERT INTO alerts (id, account_id, kind, symbol, condition, channels, is_active, created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (alert_id, account_id, kind, symbol, json.dumps(condition), json.dumps(channels), now()),
        )
    return {**get_alert(account_id, alert_id)}


def get_alert(account_id: str, alert_id: str) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM alerts WHERE id = ? AND account_id = ?", (alert_id, account_id))
        row = cursor.fetchone()
    if row is None:
        raise AlertError("Alert not found")
    value = dict(row)
    for key in ("condition", "channels"):
        try:
            value[key] = json.loads(value[key])
        except (TypeError, ValueError):
            pass
    return value


def list_alerts(account_id: str, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alerts WHERE account_id = ?"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY created_at DESC"
    with app_cursor() as cursor:
        cursor.execute(sql, (account_id,))
        rows = [dict(row) for row in cursor.fetchall()]
    for value in rows:
        for key in ("condition", "channels"):
            try:
                value[key] = json.loads(value[key])
            except (TypeError, ValueError):
                pass
    return rows


def delete_alert(account_id: str, alert_id: str) -> bool:
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM alerts WHERE id = ? AND account_id = ?", (alert_id, account_id))
        return cursor.rowcount > 0


# ---------------------------------------------------------------- evaluation


def _matches(condition: dict[str, Any], current: float, previous: float | None) -> bool:
    kind = condition.get("type")
    if current is None:
        return False
    if kind == "above":
        return current > float(condition["value"])
    if kind == "below":
        return current < float(condition["value"])
    if kind == "crosses_above":
        return previous is not None and previous <= float(condition["value"]) and current > float(condition["value"])
    if kind == "crosses_below":
        return previous is not None and previous >= float(condition["value"]) and current < float(condition["value"])
    if kind == "percent_change":
        if previous in (None, 0):
            return False
        change = (current - previous) / previous * 100
        target = float(condition["percent"])
        return abs(change) >= abs(target) and (change > 0) == (target > 0)
    if kind == "oi_change":
        target = float(condition.get("percent") or 0)
        if previous in (None, 0):
            return False
        change = (current - previous) / previous * 100
        return abs(change) >= abs(target) and (change > 0) == (target > 0)
    return False


def evaluate_alerts(account_id: str, resolver) -> list[dict[str, Any]]:
    """Check every active alert against current values.

    `resolver(alert)` returns `(current_value, previous_value)` or `None` when the
    value cannot be determined, in which case the alert is skipped this cycle.
    """
    fired: list[dict[str, Any]] = []
    for alert in list_alerts(account_id, active_only=True):
        try:
            values = resolver(alert)
        except Exception as error:  # noqa: BLE001
            log.debug("Alert %s could not be evaluated: %s", alert["id"], error)
            continue
        if not values:
            continue
        current, previous = values
        if _matches(alert["condition"], current, previous):
            message = format_message(alert, current)
            results = {}
            for channel in alert["channels"]:
                if channel == "email":
                    results[channel] = send_email(f"AlphaTradePro: {alert.get('symbol') or 'alert'}", message)
                else:
                    results[channel] = CHANNELS[channel](message)
            with app_cursor() as cursor:
                cursor.execute(
                    "UPDATE alerts SET triggered_at = ?, trigger_count = trigger_count + 1 WHERE id = ?",
                    (time.time(), alert["id"]),
                )
            fired.append({"alert": alert, "message": message, "delivery": results, "value": current})
    return fired


def format_message(alert: dict[str, Any], value: float) -> str:
    symbol = alert.get("symbol") or "instrument"
    condition = alert["condition"]
    return (
        f"AlphaTradePro alert\n"
        f"{symbol} {condition.get('type')} {condition.get('value') or condition.get('percent')}\n"
        f"Current: {value}\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


def channel_status() -> dict[str, Any]:
    """Which channels are actually usable, for the settings screen."""
    return {
        "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "whatsapp": bool(settings.whatsapp_api_key),
        "email": bool(settings.smtp_host and settings.smtp_user),
    }
