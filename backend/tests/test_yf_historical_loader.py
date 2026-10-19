from __future__ import annotations

from backend.core import database
from backend.core.config import settings
from backend.marketdata import ticks
from backend.marketdata.yf_historical_loader import resolve_yf_ticker


def test_resolve_yf_ticker_maps_indian_indices_without_a_master_row():
    assert resolve_yf_ticker("26000")[1] == "^NSEI"
    assert resolve_yf_ticker("SENSEX")[1] == "^BSESN"


def test_stored_history_reads_the_shared_candle_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    database.initialise()
    ticks.upsert_candles("26000", "nse_cm", "1d", [
        ("26000", "nse_cm", "1d", 100, 200.0, 205.0, 198.0, 204.0, 1234.0, 1.0),
    ])

    assert ticks.stored_history("26000", "nse_cm", "1d") == [{
        "time": 100 * 86400, "open": 200.0, "high": 205.0, "low": 198.0,
        "close": 204.0, "volume": 1234.0, "trades": 1,
    }]
