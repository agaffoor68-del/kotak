"""Tests for the one-click backtest pipeline and the liveness probe."""

from __future__ import annotations

import time

from backend.api.routes import strategies
from backend.core import database
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.marketdata import ticks

DEFINITION = {
    "name": "EMA cross",
    "timeframe": "1d",
    "universe": [{"token": "26000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
    "entry": {
        "all": [
            {
                "indicator": "ema",
                "params": {"period": 5},
                "compare": "gt",
                "against": {"indicator": "ema", "params": {"period": 10}, "field": "value"},
            }
        ]
    },
    "exit": {"all": [{"indicator": "rsi", "params": {"period": 5}, "compare": "gt", "value": 70}]},
    "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0},
    "position_sizing": {"mode": "fixed", "quantity": 1},
}


def _seed_instrument(token: str = "26000", symbol: str = "NIFTY 50") -> None:
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO instruments (token, exchange_segment, symbol, trading_symbol, name, series,
                                     expiry, option_type, strike, lot_size, tick_size, precision,
                                     instrument_type, underlying, multiplier, is_index, source, updated_at)
            VALUES (?, 'nse_cm', ?, ?, ?, NULL, NULL, NULL, NULL, 75, 0.05, 2, 'IDX', ?, 1, 1, 'angel', ?)
            """,
            (token, symbol, symbol, symbol, symbol, now()),
        )


def _write_bars(token: str, segment: str, interval: str, count: int = 60) -> None:
    last = int(time.time() // 86400)
    rows = [
        (token, segment, interval, last - count + 1 + index, 100.0 + index, 102.0 + index, 99.0 + index, 101.0 + index, 1000, 1.0)
        for index in range(count)
    ]
    ticks.upsert_candles(token, segment, interval, rows)


def test_health_probe_answers_without_any_dependency(tmp_path, monkeypatch):
    from backend.main import health_ping

    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()

    payload = health_ping()

    assert payload["status"] == "ok"
    assert payload["uptime_seconds"] >= 0


def test_run_backtest_downloads_the_missing_history_and_returns_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    _seed_instrument()

    calls: dict[str, object] = {}

    def fake_download(symbol_token, exchange_segment, interval, start, end, *, angel_call=None):
        calls["token"] = symbol_token
        calls["segment"] = exchange_segment
        _write_bars(symbol_token, exchange_segment, interval)
        return {"rows": 60, "source": "yfinance", "yf_ticker": "^NSEI"}

    monkeypatch.setattr("backend.marketdata.yf_historical_loader.download_history", fake_download)

    payload = strategies.AutoBacktestRequest(
        symbol="NIFTY 50",
        definition=DEFINITION,
        timeframe="1d",
        history_days=90,
    )
    result = strategies.run_backtest(payload, user=None)

    assert calls["token"] == "26000"
    assert result["download"]["performed"] is True
    assert result["download"]["bars"] == 60
    assert result["symbol_token"] == "26000"
    assert result["bars"] > 0


def test_run_backtest_reports_the_token_when_there_is_no_history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    _seed_instrument()

    def failing_download(symbol_token, exchange_segment, interval, start, end, *, angel_call=None):
        from backend.marketdata.yf_historical_loader import HistoricalDataError

        raise HistoricalDataError(f"No historical data available for {symbol_token}")

    monkeypatch.setattr("backend.marketdata.yf_historical_loader.download_history", failing_download)

    payload = strategies.AutoBacktestRequest(
        symbol="NIFTY 50",
        definition=DEFINITION,
        timeframe="1d",
        history_days=30,
    )
    result = strategies.run_backtest(payload, user=None)

    assert result["status"] == "no_data"
    assert "No data in DB for token 26000" in result["error"]
    assert result["download"]["performed"] is False
    assert result["download"]["error"]
