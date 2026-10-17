"""Broker connection panel: connect a Kotak Neo or Angel One account.

This is deliberately a separate surface from `settings` — it is where a user
pastes their own broker credentials, verifies them against the broker, and sees
which address the endpoints resolve to. Secrets go straight into the vault; only
masked views ever leave this module.

Kotak is used only for order execution. Angel One serves the market data, so an
Angel account is what makes quotes, candles and the instrument master work; the
routes here mirror the Kotak ones so both brokers are connected the same way.
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from backend.angel.auth import AngelAuthError, angel_session_manager
from backend.api.deps import (
    CurrentUser,
    angel_session_health,
    current_user,
    require_trader,
    session_health,
)
from backend.broker import accounts
from backend.broker.neo_auth import NeoAuthError, session_manager
from backend.core.config import settings
from backend.core.database import app_cursor, row_to_dict
from backend.core.vault import AngelCredentials, BrokerCredentials, VaultLocked, vault

router = APIRouter(prefix="/api/v1/broker", tags=["broker"])

#: Hostnames the Neo client talks to. Probing these is what tells a user
#: whether the failure is their credentials or their network path. The UAT
#: gateway is reported but never fails the verdict — it is absent from most
#: networks and irrelevant to a prod login.
PROBE_HOSTS: tuple[tuple[str, str, bool], ...] = (
    ("Trade API (login/orders)", "mis.kotaksecurities.com", True),
    ("Session API (prod)", "mnapi.kotaksecurities.com", True),
    ("Session API (ADC)", "cnapi.kotaksecurities.com", True),
    ("Session API (UAT)", "nsbxapi.kotaksecurities.com", False),
    ("Market data WebSocket", "mlhsm.kotaksecurities.com", True),
)

_PROBE_PORT = 443
_PROBE_TIMEOUT = 3.0


class ConnectRequest(BaseModel):
    """The credential set Kotak needs to log one account in."""

    label: str = Field(default="My Kotak account", min_length=1, max_length=80)
    consumer_key: str = Field(min_length=1, max_length=256)
    totp_key: str = Field(min_length=1, max_length=256)
    mpin: str = Field(min_length=1, max_length=32)
    mobile_number: str = Field(min_length=6, max_length=20)
    ucc: str = Field(min_length=3, max_length=40)
    neo_fin_key: str | None = Field(default=None, max_length=128)
    environment: str = "prod"
    #: Verify with Kotak before persisting. On by default: storing credentials
    #: that do not work only to fail later is the confusing path.
    test_before_save: bool = True

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in {"prod", "uat"}:
            raise ValueError("environment must be 'prod' or 'uat'")
        return value

    @field_validator("neo_fin_key")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        stripped = (value or "").strip()
        return stripped or None

    def to_credentials(self) -> BrokerCredentials:
        return BrokerCredentials(
            consumer_key=self.consumer_key.strip(),
            totp_key=self.totp_key.strip(),
            mpin=self.mpin.strip(),
            mobile_number=self.mobile_number.strip(),
            ucc=self.ucc.strip(),
            neo_fin_key=self.neo_fin_key,
            environment=self.environment,
        )


def _owned_account_ids(user: CurrentUser) -> set[str]:
    """Accounts this caller may act on: their own, plus all of them if admin."""
    if user.is_admin:
        return {row["account_id"] for row in accounts.list_accounts()}
    rows: set[str] = set()
    if user.account_id:
        rows.add(user.account_id)
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT b.account_id FROM broker_accounts b
            JOIN users u ON u.account_id = b.account_id
            WHERE u.id = ?
            """,
            (user.id,),
        )
        rows.update(row_to_dict(row)["account_id"] for row in cursor.fetchall())
    return rows


def _account_view(account_id: str) -> dict[str, Any]:
    return {**accounts.account_status(account_id), "session": session_health(account_id)}


# ------------------------------------------------------------------- accounts


@router.get("/accounts")
def list_accounts_route(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Every account this user is allowed to see, with live session state."""
    allowed = _owned_account_ids(user)
    visible = [row for row in accounts.list_accounts() if row["account_id"] in allowed]

    def health_for(row: dict[str, Any]) -> dict[str, Any]:
        if str(row.get("broker") or accounts.KOTAK).lower() == accounts.ANGEL:
            return angel_session_health(row["account_id"])
        return session_health(row["account_id"])

    return {
        "is_admin": user.is_admin,
        "accounts": [
            {
                "account_id": row["account_id"],
                "broker": str(row.get("broker") or accounts.KOTAK).lower(),
                "label": row["label"],
                "environment": row["environment"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "session": health_for(row),
            }
            for row in visible
        ],
        "default_account_id": accounts.default_account_id(),
        "vault_enabled": vault.enabled,
        "legacy_env_fallback": settings.legacy_env_fallback,
    }


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def connect_account(payload: ConnectRequest, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Store this user's Kotak credentials, encrypted, bound to their profile.

    The UCC becomes the account id, so a user's own credentials always resolve
    back to their own account.
    """
    try:
        credentials = payload.to_credentials()
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    if payload.test_before_save:
        outcome = session_manager.probe(credentials)
        if not outcome["ok"]:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, outcome["error"])

    account_id = credentials.ucc
    try:
        saved = accounts.save_account(
            account_id=account_id,
            label=payload.label.strip(),
            credentials=credentials,
            environment=credentials.environment,
        )
    except VaultLocked as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

    with app_cursor() as cursor:
        cursor.execute("UPDATE users SET account_id = ? WHERE id = ?", (account_id, user.id))

    session_manager.invalidate(account_id)
    return {
        "account": _account_view(account_id),
        "masked": saved["credentials"],
        "verified": bool(payload.test_before_save),
        "bound_to_user": user.id,
    }


class TestRequest(BaseModel):
    """Verify credentials that have not been saved yet."""

    consumer_key: str = Field(min_length=1, max_length=256)
    totp_key: str = Field(min_length=1, max_length=256)
    mpin: str = Field(min_length=1, max_length=32)
    mobile_number: str = Field(min_length=6, max_length=20)
    ucc: str = Field(min_length=3, max_length=40)
    neo_fin_key: str | None = Field(default=None, max_length=128)
    environment: str = "prod"

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in {"prod", "uat"}:
            raise ValueError("environment must be 'prod' or 'uat'")
        return value


@router.post("/test")
def test_unsaved(payload: TestRequest, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Check credentials before storing them, so a typo is caught at the form."""
    try:
        credentials = BrokerCredentials(
            consumer_key=payload.consumer_key.strip(),
            totp_key=payload.totp_key.strip(),
            mpin=payload.mpin.strip(),
            mobile_number=payload.mobile_number.strip(),
            ucc=payload.ucc.strip(),
            neo_fin_key=(payload.neo_fin_key or "").strip() or None,
            environment=payload.environment,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    return session_manager.probe(credentials)


@router.post("/accounts/{account_id}/test")
def test_account(account_id: str, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Log in once with a *saved* account and report exactly what Kotak said."""
    if account_id not in _owned_account_ids(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that account")
    credentials = accounts.get_credentials(account_id)
    if credentials is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No credentials stored for account '{account_id}'.")
    return session_manager.probe(credentials)


def _reconnect_account(account_id: str, credentials: BrokerCredentials | AngelCredentials) -> dict[str, Any]:
    """Reconnect through the account's declared broker and matching credential type."""
    broker = accounts.broker_of(account_id)
    if broker == accounts.ANGEL:
        if not isinstance(credentials, AngelCredentials):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No Angel One credentials stored for '{account_id}'.")
        angel_session_manager.invalidate(account_id)
        try:
            angel_session_manager.get(account_id, credentials)
        except AngelAuthError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return angel_session_health(account_id)

    if not isinstance(credentials, BrokerCredentials):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No Kotak Neo credentials stored for '{account_id}'.")
    session_manager.invalidate(account_id)
    try:
        session_manager.get(account_id, credentials)
    except NeoAuthError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    return session_health(account_id)


@router.post("/accounts/{account_id}/reconnect")
def reconnect_account(account_id: str, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Drop the cached session and log in again with the stored broker credentials."""
    if account_id not in _owned_account_ids(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that account")
    credentials = accounts.get_credentials(account_id)
    if credentials is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No credentials stored for account '{account_id}'.")
    return _reconnect_account(account_id, credentials)



# ------------------------------------------------------------------ network


def _resolve(host: str) -> tuple[list[str], list[str], str | None]:
    """A and AAAA records for `host`, split into IPv4 and IPv6."""
    try:
        infos = socket.getaddrinfo(host, _PROBE_PORT, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        return [], [], str(error)
    ipv4: list[str] = []
    ipv6: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if family == socket.AF_INET:
            if address not in ipv4:
                ipv4.append(address)
        elif family == socket.AF_INET6 and address not in ipv6:
            ipv6.append(address)
    return ipv4, ipv6, None


def _connect(address: str, family: int) -> dict[str, Any]:
    """TCP handshake latency to one address, or the reason it failed."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(_PROBE_TIMEOUT)
    started = time.perf_counter()
    try:
        sock.connect((address, _PROBE_PORT) if family == socket.AF_INET else (address, _PROBE_PORT, 0, 0))
        return {
            "address": address,
            "family": "ipv6" if family == socket.AF_INET6 else "ipv4",
            "reachable": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": None,
        }
    except OSError as error:
        return {"address": address, "family": "ipv6" if family == socket.AF_INET6 else "ipv4",
                "reachable": False, "latency_ms": None, "error": str(error)}
    finally:
        sock.close()


@router.get("/network")
def network_report(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Resolved IPv4/IPv6 for every Kotak host, plus reachability and latency.

    Pure diagnostics: it shows whether the ISP is serving the IPv6 the platform
    prefers and whether that path actually connects — the difference between
    "my key is wrong" and "my network is broken".
    """
    with ThreadPoolExecutor(max_workers=len(PROBE_HOSTS)) as pool:
        resolutions = list(pool.map(lambda item: _resolve(item[1]), PROBE_HOSTS))

    hosts: list[dict[str, Any]] = []
    targets: list[tuple[int, str, int]] = []
    for (label, host, required), (ipv4, ipv6, dns_error) in zip(PROBE_HOSTS, resolutions):
        hosts.append({"label": label, "host": host, "required": required, "dns_error": dns_error,
                      "ipv4": ipv4, "ipv6": ipv6, "results": []})
        index = len(hosts) - 1
        targets.extend((index, address, socket.AF_INET) for address in ipv4)
        targets.extend((index, address, socket.AF_INET6) for address in ipv6)

    if targets:
        with ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
            results = list(pool.map(lambda t: _connect(t[1], t[2]), targets))
        for (index, _address, _family), result in zip(targets, results):
            hosts[index]["results"].append(result)

    total_v4 = sum(len(host["ipv4"]) for host in hosts)
    total_v6 = sum(len(host["ipv6"]) for host in hosts)
    reachable_v4 = sum(1 for host in hosts for r in host["results"] if r["reachable"] and r["family"] == "ipv4")
    reachable_v6 = sum(1 for host in hosts for r in host["results"] if r["reachable"] and r["family"] == "ipv6")

    # A required host with neither a resolved address nor a single reachable
    # one is a hard outage, so it must not be hidden behind a verdict computed
    # from the hosts that happen to work. Optional hosts are listed too.
    down = [
        host["host"]
        for host in hosts
        if host["dns_error"] or not any(result["reachable"] for result in host["results"])
    ]
    required_down = [host["host"] for host in hosts if host["required"] and host["host"] in down]
    reachable_any = reachable_v4 + reachable_v6

    if not reachable_any:
        verdict = "unreachable"
    elif required_down:
        verdict = "degraded"
    elif total_v6 and not reachable_v6:
        verdict = "ipv6_unreachable"
    elif total_v6 and reachable_v6:
        verdict = "dual_stack"
    else:
        verdict = "ipv4_only"

    return {
        "checked_at": time.time(),
        "verdict": verdict,
        "hosts_down": down,
        "required_hosts_down": required_down,
        "summary": {
            "ipv4_resolved": total_v4,
            "ipv4_reachable": reachable_v4,
            "ipv6_resolved": total_v6,
            "ipv6_reachable": reachable_v6,
            "timeout_seconds": _PROBE_TIMEOUT,
            "local_ipv6": bool(socket.has_ipv6),
        },
        "hosts": hosts,
        "note": (
            "IPv6 answers first when the OS prefers it. A verdict of "
            "'ipv6_unreachable' means the login fails before Kotak ever sees the "
            "request; 'degraded' lists the hosts that did not answer."
        ),
    }


@router.delete("/accounts/{account_id}")
def disconnect_account(account_id: str, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Remove a stored account and unbind it from whoever owned it."""
    if account_id not in _owned_account_ids(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that account")
    session_manager.invalidate(account_id)
    removed = accounts.delete_account(account_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No stored account '{account_id}'")
    with app_cursor() as cursor:
        cursor.execute("UPDATE users SET account_id = NULL WHERE account_id = ?", (account_id,))
    return {"removed": True, "account_id": account_id}


@router.post("/session/reconnect")
def reconnect_own(user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Re-login whichever account the caller is bound to.

    The header's "Reconnect broker" button calls this. It exists as its own
    route so that button needs no account id — a user only ever reconnects the
    account they own.
    """
    account_id = user.account_id or accounts.default_account_id()
    if not account_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Kotak account is connected. Add your credentials on the Connect broker page.",
        )
    credentials = accounts.get_credentials(account_id)
    if credentials is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No credentials stored for account '{account_id}'.")
    return _reconnect_account(account_id, credentials)


@router.get("/session")
def own_session(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Session state for whichever account this caller is bound to."""
    account_id = user.account_id or accounts.default_account_id()
    if not account_id:
        return {
            "account_id": None,
            "status": "disconnected",
            "message": "No broker account connected yet.",
            "angel": angel_session_health(),
        }
    view = _account_view(account_id)
    view["angel"] = angel_session_health()
    return view


# --------------------------------------------------------------- Angel One


class AngelConnectRequest(BaseModel):
    """The credential set Angel One SmartAPI needs to log one account in."""

    label: str = Field(default="My Angel One account", min_length=1, max_length=80)
    api_key: str = Field(min_length=1, max_length=256)
    client_code: str = Field(min_length=3, max_length=40)
    pin: str = Field(min_length=1, max_length=32)
    totp_key: str = Field(min_length=1, max_length=256)
    environment: str = "prod"
    #: Verify with Angel before persisting. On by default, so a typo is caught
    #: at the form rather than surfacing later as a silent data outage.
    test_before_save: bool = True

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in {"prod", "uat"}:
            raise ValueError("environment must be 'prod' or 'uat'")
        return value

    def to_credentials(self) -> AngelCredentials:
        return AngelCredentials(
            api_key=self.api_key.strip(),
            client_code=self.client_code.strip(),
            pin=self.pin.strip(),
            totp_key=self.totp_key.strip(),
            environment=self.environment,
        )


@router.post("/angel/accounts", status_code=status.HTTP_201_CREATED)
def connect_angel_account(
    payload: AngelConnectRequest, user: CurrentUser = Depends(require_trader)
) -> dict[str, Any]:
    """Store this user's Angel One credentials, encrypted.

    The client code becomes the account id. Angel is the market-data broker, so
    this is the account that makes quotes and candles work platform-wide.
    """
    try:
        credentials = payload.to_credentials()
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    if payload.test_before_save:
        outcome = angel_session_manager.probe(credentials)
        if not outcome["ok"]:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, outcome["error"])

    account_id = credentials.client_code
    try:
        saved = accounts.save_account(
            account_id=account_id,
            label=payload.label.strip(),
            credentials=credentials,
            broker=accounts.ANGEL,
            environment=credentials.environment,
        )
    except VaultLocked as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

    with app_cursor() as cursor:
        cursor.execute("UPDATE users SET account_id = ? WHERE id = ? AND (account_id IS NULL OR account_id = '')",
                       (account_id, user.id))

    angel_session_manager.invalidate(account_id)
    return {
        "account": {**accounts.account_status(account_id), "session": angel_session_health(account_id)},
        "masked": saved["credentials"],
        "verified": bool(payload.test_before_save),
        "bound_to_user": user.id,
    }


class AngelTestRequest(BaseModel):
    """Verify Angel credentials that have not been saved yet."""

    api_key: str = Field(min_length=1, max_length=256)
    client_code: str = Field(min_length=3, max_length=40)
    pin: str = Field(min_length=1, max_length=32)
    totp_key: str = Field(min_length=1, max_length=256)
    environment: str = "prod"


@router.post("/angel/test")
def test_angel_unsaved(payload: AngelTestRequest, user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Check Angel credentials before storing them."""
    try:
        credentials = AngelCredentials(
            api_key=payload.api_key.strip(),
            client_code=payload.client_code.strip(),
            pin=payload.pin.strip(),
            totp_key=payload.totp_key.strip(),
            environment=payload.environment,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    return angel_session_manager.probe(credentials)


@router.post("/angel/session/reconnect")
def reconnect_angel(user: CurrentUser = Depends(require_trader)) -> dict[str, Any]:
    """Drop the cached Angel session and log in again."""
    account_id = accounts.default_account_id(accounts.ANGEL)
    if not account_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Angel One account is connected. Add your SmartAPI credentials first.",
        )
    credentials = accounts.get_credentials(account_id)
    if not isinstance(credentials, AngelCredentials):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No Angel One credentials stored for '{account_id}'.")
    angel_session_manager.invalidate(account_id)
    try:
        angel_session_manager.get(account_id, credentials)
    except AngelAuthError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    return angel_session_health(account_id)


@router.get("/angel/session")
def angel_session(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Session state of the Angel One market-data account."""
    return angel_session_health()
