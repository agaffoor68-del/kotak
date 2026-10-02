"""SQLite state store.

PostgreSQL holds the relational, multi-tenant state (users, strategies, orders,
journals). SQLite holds the high-frequency, append-only market data — ticks and
derived candles — because writing every tick to Postgres would be a bottleneck
and losing a few seconds of tape on restart is acceptable.

Both are wrapped here so the rest of the code never sees a raw connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

_local = threading.local()


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _thread_connection(path: str) -> sqlite3.Connection:
    cache: dict[str, sqlite3.Connection] = getattr(_local, "connections", None) or {}
    if path not in cache:
        cache[path] = _connect(path)
        _local.connections = cache
    return cache[path]


@contextmanager
def app_cursor() -> Iterator[sqlite3.Cursor]:
    """Cursor on the application state database."""
    connection = _thread_connection(settings.state_file)
    cursor = connection.cursor()
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


@contextmanager
def market_cursor() -> Iterator[sqlite3.Cursor]:
    """Cursor on the tick/candle database."""
    connection = _thread_connection(settings.tick_db_file)
    cursor = connection.cursor()
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def initialise() -> None:
    """Create every table. Safe to run on each boot."""
    settings.ensure_directories()
    with app_cursor() as cursor:
        cursor.executescript(APP_SCHEMA)
    with market_cursor() as cursor:
        cursor.executescript(MARKET_SCHEMA)


# ---------------------------------------------------------------- app schema

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL,
    account_id     TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL,
    last_login_at  REAL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id   TEXT,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL UNIQUE,
    label              TEXT NOT NULL,
    encrypted_secrets  TEXT NOT NULL,
    environment        TEXT NOT NULL DEFAULT 'prod',
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    token            TEXT NOT NULL,
    exchange_segment TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    trading_symbol   TEXT NOT NULL,
    name             TEXT,
    series           TEXT,
    expiry           TEXT,
    option_type      TEXT,
    strike           REAL,
    lot_size         INTEGER,
    tick_size        REAL,
    precision        INTEGER,
    instrument_type  TEXT,
    underlying       TEXT,
    multiplier       REAL,
    is_index         INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (token, exchange_segment)
);
CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol);
CREATE INDEX IF NOT EXISTS idx_instruments_underlying ON instruments(underlying);
CREATE INDEX IF NOT EXISTS idx_instruments_expiry ON instruments(exchange_segment, expiry);

CREATE TABLE IF NOT EXISTS strategies (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    kind        TEXT NOT NULL DEFAULT 'rule',
    definition  TEXT NOT NULL,
    is_template INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategies_account ON strategies(account_id);

CREATE TABLE IF NOT EXISTS strategy_runs (
    id          TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    params      TEXT NOT NULL,
    metrics     TEXT,
    equity_curve TEXT,
    trades      TEXT,
    error       TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON strategy_runs(strategy_id);

CREATE TABLE IF NOT EXISTS orders (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,
    strategy_run_id    TEXT,
    mode               TEXT NOT NULL,
    broker_order_id    TEXT,
    exchange_segment   TEXT NOT NULL,
    product            TEXT NOT NULL,
    trading_symbol     TEXT NOT NULL,
    instrument_token   TEXT,
    transaction_type   TEXT NOT NULL,
    order_type         TEXT NOT NULL,
    quantity           REAL NOT NULL,
    price              REAL,
    trigger_price      REAL,
    average_price      REAL,
    status             TEXT NOT NULL,
    rejection_reason   TEXT,
    realised_pnl       REAL NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(strategy_run_id);

CREATE TABLE IF NOT EXISTS trades (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    strategy_run_id TEXT,
    mode          TEXT NOT NULL,
    order_id      TEXT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    quantity      REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    entry_time    REAL NOT NULL,
    exit_time     REAL,
    gross_pnl     REAL,
    charges       REAL,
    net_pnl       REAL,
    reason        TEXT,
    tags          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account_id, entry_time DESC);

CREATE TABLE IF NOT EXISTS journal (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    trade_id      TEXT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    quantity      REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    setup         TEXT,
    emotion       TEXT,
    followed_plan INTEGER,
    notes         TEXT,
    tags          TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_account ON journal(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    symbol        TEXT,
    condition     TEXT NOT NULL,
    channels      TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    triggered_at  REAL,
    trigger_count INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_account ON alerts(account_id, is_active);

CREATE TABLE IF NOT EXISTS risk_config (
    account_id             TEXT PRIMARY KEY,
    max_daily_loss         REAL NOT NULL DEFAULT 0,
    max_drawdown           REAL NOT NULL DEFAULT 0,
    max_position_pct       REAL NOT NULL DEFAULT 0,
    max_positions          INTEGER NOT NULL DEFAULT 0,
    max_orders_per_minute  INTEGER NOT NULL DEFAULT 0,
    max_exposure_pct       REAL NOT NULL DEFAULT 0,
    kill_switch            INTEGER NOT NULL DEFAULT 0,
    kill_reason            TEXT,
    updated_at             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_account ON risk_events(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS equity_curve (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    mode       TEXT NOT NULL,
    ts         REAL NOT NULL,
    equity     REAL NOT NULL,
    realised   REAL NOT NULL DEFAULT 0,
    unrealised REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_account ON equity_curve(account_id, mode, ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# -------------------------------------------------------------- market schema

MARKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    token             TEXT NOT NULL,
    exchange_segment  TEXT NOT NULL,
    ts                REAL NOT NULL,
    last_price        REAL,
    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL,
    volume            REAL,
    oi                REAL,
    bid               REAL,
    ask               REAL,
    change_pct        REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_lookup ON ticks(token, exchange_segment, ts);

CREATE TABLE IF NOT EXISTS candles (
    token             TEXT NOT NULL,
    exchange_segment  TEXT NOT NULL,
    interval          TEXT NOT NULL,
    bucket           INTEGER NOT NULL,
    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL,
    volume            REAL,
    trades            INTEGER NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL,
    PRIMARY KEY (token, exchange_segment, interval, bucket)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(token, exchange_segment, interval, bucket DESC);
"""


# ----------------------------------------------------------------- helpers

def meta_get(cursor: sqlite3.Cursor, key: str, default: str | None = None) -> str | None:
    cursor.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else default


def meta_set(cursor: sqlite3.Cursor, key: str, value: str) -> None:
    cursor.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in ("definition", "params", "metrics", "equity_curve", "trades", "channels", "condition", "tags"):
        if key in value and isinstance(value[key], str):
            try:
                value[key] = json.loads(value[key])
            except (TypeError, ValueError):
                pass
    return value
