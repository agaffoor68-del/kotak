"""Boots the AlphaTradePro API and exercises the auth and read-only routes.

Live trading stays off and no Kotak account is configured, so every broker
endpoint must fail with a clear message rather than a stack trace.
"""

import os
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-local-verification")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key-for-local-verification")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-local-verification")
os.environ.setdefault("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "Correct-Horse-9")
os.environ.setdefault("ENABLE_LIVE_TRADING", "0")
os.environ.setdefault("KOTAK_KEEPALIVE", "0")

# Use a throwaway database so a developer's real state is never read or
# overwritten by the test run.
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="alphatrade-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok  {label}")


with TestClient(app) as client:
    print("Health and meta")
    health = client.get("/api/v1/system/health")
    check("Health returns 200", health.status_code == 200, str(health.status_code))
    body = health.json()
    check("Health names Kotak as the broker", body["broker"] == "kotak_neo")
    check("Health reports feature gates", "live_trading" in body["features"])
    check("Live trading is off by default", body["features"]["live_trading"] is False)
    check("Root lists the service", client.get("/").json()["service"] == "AlphaTradePro")

    print("Authentication")
    check("Protected route rejects an anonymous caller",
          client.get("/api/v1/portfolio").status_code == 401)
    check("Login rejects a bad password",
          client.post("/api/v1/auth/login", json={"email": "admin@test.local", "password": "wrong"}).status_code == 401)
    check("Login rejects an unknown user with the same message as a bad password",
          client.post("/api/v1/auth/login", json={"email": "nobody@test.local", "password": "wrong"}).json()
          ["detail"] == "Invalid email or password")

    login = client.post("/api/v1/auth/login", json={"email": "admin@test.local", "password": "Correct-Horse-9"})
    check("Bootstrap admin can log in", login.status_code == 200, login.text[:200])
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    check("Login returns the user role", login.json()["user"]["role"] == "admin")
    check("A tampered token is rejected",
          client.get("/api/v1/portfolio", headers={"Authorization": f"Bearer {token[:-4]}AAAA"}).status_code == 401)
    check("/me resolves the caller", client.get("/api/v1/auth/me", headers=headers).json()["email"] == "admin@test.local")

    print("Read-only market routes")
    status = client.get("/api/v1/market/status", headers=headers)
    check("Market status returns 200", status.status_code == 200, status.text[:200])
    check("Status includes the session state", status.json()["session"]["state"] in {"open", "closed", "pre_open", "post_close"})
    check("Status includes feed health", "feed" in status.json())
    check("Status reports the scrip-master state", "scrip_master" in status.json())
    check("Status lists candle intervals", "5m" in status.json()["intervals"])

    search = client.get("/api/v1/market/search?q=RELIANCE", headers=headers)
    check("Search on an empty master explains itself", search.status_code == 503, str(search.status_code))
    check("The explanation mentions the sync", "sync" in search.json()["detail"].lower())

    print("Broker-dependent routes fail honestly")
    # With no account and no scrip master, each of these must explain the actual
    # blocker. 400 = no credentials, 404 = no such instruments, 502 = broker
    # refused, 503 = master not synced. None may be a 500.
    for path in ("/api/v1/portfolio", "/api/v1/market/breadth", "/api/v1/options/chain?underlying=NIFTY"):
        response = client.get(path, headers=headers)
        check(f"{path} reports a configuration problem, not a crash",
              response.status_code in {400, 404, 502, 503} and "detail" in response.json(),
              f"{response.status_code} {response.text[:160]}")

    print("Strategy routes work without a broker")
    indicators = client.get("/api/v1/strategies/indicators", headers=headers)
    check("Indicators are listed", indicators.status_code == 200)
    names = {item["name"] for item in indicators.json()["indicators"]}
    check("All 15 required indicators are present",
          {"ema", "sma", "wma", "vwap", "supertrend", "rsi", "macd", "bollinger", "atr", "adx",
           "stochastic_rsi", "ichimoku", "obv", "cmf", "volume_profile"} <= names, str(sorted(names)))
    check("Scalar comparators are exposed", {"gt", "lt", "gte", "lte", "eq", "neq"} <= set(indicators.json()["comparators"]))
    check("Cross comparators are exposed", set(indicators.json()["cross_comparators"]) == {"crosses_above", "crosses_below"})
    check("Logical operators are exposed", set(indicators.json()["logical"]) == {"all", "any", "not"})

    templates = client.get("/api/v1/strategies/templates", headers=headers)
    check("Strategy templates are installed", len(templates.json()) >= 5, str(len(templates.json())))
    check("Every installed template validates", all(t["valid"] for t in templates.json()),
          str([t["name"] for t in templates.json() if not t["valid"]]))

    bad = client.post("/api/v1/strategies", headers=headers, json={
        "name": "Broken", "definition": {"name": "", "timeframe": "5m", "universe": [],
                                        "entry": {"all": []}, "exit": {"all": []}},
    })
    check("An invalid strategy is rejected with 422", bad.status_code == 422, str(bad.status_code))

    good_definition = {
        "name": "Test", "timeframe": "5m",
        "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY"}],
        "entry": {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 35}]},
        "exit": {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "gt", "value": 65}]},
        "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0},
        "position_sizing": {"mode": "fixed", "quantity": 1},
    }
    created = client.post("/api/v1/strategies", headers=headers,
                          json={"name": "Test strategy", "definition": good_definition})
    check("A valid strategy is created", created.status_code == 201, created.text[:200])
    strategy_id = created.json()["id"]

    backtest = client.post("/api/v1/strategies/backtest", headers=headers,
                           json={"strategy_id": strategy_id, "initial_capital": 500000})
    check("Backtest runs without a broker", backtest.status_code == 200, backtest.text[:200])
    check("Backtest reports no-data honestly with no recorded tape",
          backtest.json()["status"] in {"no_data", "ok"}, backtest.json()["status"])
    if backtest.json()["status"] == "no_data":
        note = backtest.json()["note"]
        # Either there are no candles at all, or too few for the strategy warm-up.
        # Both notes must make clear the data comes from the recorded tape.
        check("The no-data note explains the recorded-tape limitation",
              "Kotak Neo does not provide" in note or "warm-up" in note, note)
    check("Backtest includes per-instrument coverage", "coverage" in backtest.json())

    print("Risk engine")
    account_id = uuid.uuid4().hex
    risk = client.get("/api/v1/risk", headers=headers)
    check("Risk status is available", risk.status_code in {200, 400}, str(risk.status_code))
    if risk.status_code == 200:
        payload = risk.json()
        check("Risk status exposes the config", "config" in payload)
        check("Daily loss limit is set", payload["config"]["max_daily_loss"] > 0)
        check("Trading is not halted by default", payload["trading_halted"] is False)

    print("Live orders are refused while the feature gate is off")
    refused = client.post("/api/v1/orders/live", headers=headers, json={
        "exchange_segment": "nse_cm", "product": "CNC", "trading_symbol": "RELIANCE-EQ",
        "transaction_type": "B", "order_type": "L", "quantity": 1, "price": 100,
        "confirmation": "PLACE LIVE ORDER",
    })
    check("A live order is blocked with a clear reason",
          refused.status_code == 403 and "DISABLED" in str(refused.json()), refused.text[:200])
    check("The block message explains how to enable it", "ENABLE_LIVE_TRADING" in str(refused.json()))

    print("Paper trading refuses to invent a price")
    paper = client.post("/api/v1/paper/orders", headers=headers, json={
        "exchange_segment": "nse_cm", "product": "CNC", "trading_symbol": "RELIANCE-EQ",
        "transaction_type": "B", "order_type": "MKT", "quantity": 1,
    })
    check("A paper order with no live quote is refused", paper.status_code == 400, str(paper.status_code))
    check("The refusal names the real cause", "live Kotak quote" in paper.json()["detail"], paper.text[:200])

    print("Analytics and coach degrade gracefully")
    performance = client.get("/api/v1/analytics/performance", headers=headers)
    check("Performance returns 200", performance.status_code == 200)
    check("Performance reports no data honestly", performance.json()["has_data"] is False)
    coach_response = client.get("/api/v1/coach", headers=headers)
    check("Coach returns 200", coach_response.status_code == 200)
    check("Coach reports no history honestly", coach_response.json()["has_data"] is False)
    check("Coach always carries a disclaimer", "not investment advice" in coach_response.json()["disclaimer"])

    print("Alerts")
    alert = client.post("/api/v1/alerts", headers=headers, json={
        "kind": "price", "symbol": "NIFTY",
        "condition": {"type": "above", "value": 25000}, "channels": ["telegram"],
    })
    check("An alert is created", alert.status_code == 201, alert.text[:200])
    check("Alert channels report as unconfigured when unset",
          alert.json() and client.get("/api/v1/alerts", headers=headers).json()["channels"]["telegram"] is False)
    check("An unknown channel is rejected", client.post("/api/v1/alerts", headers=headers, json={
        "kind": "price", "condition": {"type": "above", "value": 1}, "channels": ["carrier-pigeon"],
    }).status_code == 422)

    print("Role-based access")
    from backend.core.database import app_cursor  # noqa: E402
    from backend.core.security import hash_password  # noqa: E402
    from backend.core.database import now  # noqa: E402

    viewer_email = f"viewer-{uuid.uuid4().hex[:8]}@test.local"
    with app_cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
            (uuid.uuid4().hex, viewer_email, hash_password("Viewer-Pass-123"), "viewer", now()),
        )
    viewer_token = client.post("/api/v1/auth/login",
                               json={"email": viewer_email, "password": "Viewer-Pass-123"}).json()["access_token"]
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
    check("A viewer can read the market status",
          client.get("/api/v1/market/status", headers=viewer_headers).status_code == 200)
    check("A viewer cannot create an API key (trader required)",
          client.post("/api/v1/auth/api-keys", json={"name": "x"}, headers=viewer_headers).status_code == 403)
    check("A viewer cannot place an order",
          client.post("/api/v1/orders/live", json={}, headers=viewer_headers).status_code == 403)
    check("A viewer cannot reach the admin user list",
          client.get("/api/v1/auth/users", headers=viewer_headers).status_code == 403)
    check("An admin can list users", isinstance(client.get("/api/v1/auth/users", headers=headers).json(), list))
    check("Self-registration is closed once a user exists",
          client.post("/api/v1/auth/register", json={
              "email": "new@test.local", "password": "Another-Pass-99",
          }).status_code == 403)

print(f"\nAll {CHECKS} API checks passed.")
shutil.rmtree(_TMP, ignore_errors=True)
