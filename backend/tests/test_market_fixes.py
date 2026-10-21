"""Tests for the market-data fixes: breadth counting, token lookup and search."""

from __future__ import annotations

from backend.api.routes import market
from backend.core import database
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.marketdata import market_hours, master, search_index, ticks
from backend.marketdata.quotes import Quote


def _quote(token: str, last: float | None, previous_close: float | None, **extra) -> Quote:
    quote = Quote(token=token, exchange_segment="nse_cm")
    quote.last = last
    quote.previous_close = previous_close
    for key, value in extra.items():
        setattr(quote, key, value)
    return quote


def test_breadth_counts_advances_from_the_last_price_and_previous_close():
    quotes = [
        _quote("1", 110.0, 100.0),
        _quote("2", 90.0, 100.0),
        _quote("3", 100.0, 100.0),
        _quote("4", 101.0, 100.0),
    ]

    breadth = market_hours.breadth(quotes)

    assert breadth["priced"] == 4
    assert breadth["advances"] == 2
    assert breadth["declines"] == 1
    assert breadth["unchanged"] == 1


def test_breadth_ignores_quotes_without_a_price():
    breadth = market_hours.breadth([_quote("1", None, 100.0), _quote("2", 101.0, 100.0)])

    assert breadth["priced"] == 1
    assert breadth["advances"] == 1


def test_rank_uses_the_change_derived_from_the_previous_close():
    quotes = [
        _quote("1", 130.0, 100.0),
        _quote("2", 95.0, 100.0),
        _quote("3", 120.0, 100.0),
    ]

    ranked = market_hours.rank(quotes, limit=2, reverse=True)

    assert [row["token"] for row in ranked] == ["1", "3"]


def test_stored_history_reads_a_token_from_any_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    database.initialise()
    ticks.upsert_candles("99926000", "nse_fo", "1d", [
        ("99926000", "nse_fo", "1d", 100, 200.0, 205.0, 198.0, 204.0, 1234.0, 1.0),
    ])

    assert ticks.stored_history("99926000", "nse_cm", "1d") == []
    assert len(ticks.stored_history("99926000", None, "1d")) == 1


def _insert_instrument(cursor, token: str, symbol: str, trading_symbol: str, name: str, source: str) -> None:
    cursor.execute(
        """
        INSERT INTO instruments (token, exchange_segment, symbol, trading_symbol, name, series,
                                 expiry, option_type, strike, lot_size, tick_size, precision,
                                 instrument_type, underlying, multiplier, is_index, source, updated_at)
        VALUES (?, 'nse_cm', ?, ?, ?, NULL, NULL, NULL, NULL, 1, 0.05, 2, 'EQ', ?, 1, 0, ?, ?)
        """,
        (token, symbol, trading_symbol, name, symbol, source, now()),
    )


def test_search_finds_an_instrument_by_its_company_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    with app_cursor() as cursor:
        _insert_instrument(
            cursor, "2885", "RELIANCE INDUSTRIES LTD.", "RELIANCE-EQ", "RELIANCE INDUSTRIES LTD.", "angel",
        )
    search_index.refresh()

    by_symbol = search_index.search("RELIANCE", limit=5)
    by_name = search_index.search("Reliance Industries", limit=5)

    assert by_symbol and by_symbol[0]["token"] == "2885"
    assert by_name and by_name[0]["token"] == "2885"
    assert by_symbol[0]["symbol"] == "RELIANCE"


def test_instrument_display_prefers_the_trading_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    with app_cursor() as cursor:
        _insert_instrument(
            cursor, "2885", "RELIANCE INDUSTRIES LTD.", "RELIANCE-EQ", "RELIANCE INDUSTRIES LTD.", "angel",
        )

    symbol, name = master.instrument_display("2885", "nse_cm")

    assert symbol == "RELIANCE"
    assert name == "RELIANCE INDUSTRIES LTD."


def test_instrument_display_returns_an_unknown_token_unchanged():
    symbol, name = master.instrument_display("does-not-exist", "nse_cm")

    assert symbol == "does-not-exist"
    assert name is None


def test_a_kotak_row_never_overwrites_the_angel_row(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    with app_cursor() as cursor:
        _insert_instrument(
            cursor, "2885", "RELIANCE INDUSTRIES LTD.", "RELIANCE-EQ", "RELIANCE INDUSTRIES LTD.", "angel",
        )
        master._upsert(cursor, {
            "token": "2885", "exchange_segment": "nse_cm", "symbol": "KOTAK RELIANCE",
            "trading_symbol": "RELIANCE-EQ", "name": "KOTAK RELIANCE", "series": None,
            "expiry": None, "option_type": None, "strike": None, "lot_size": 1,
            "tick_size": 0.05, "precision": 2, "instrument_type": "EQ",
            "underlying": "KOTAK RELIANCE", "multiplier": 1, "is_index": 0, "updated_at": now(),
        })

    with app_cursor() as cursor:
        cursor.execute(
            "SELECT symbol, source FROM instruments WHERE token = ? AND exchange_segment = ?",
            ("2885", "nse_cm"),
        )
        row = cursor.fetchone()

    assert row["symbol"] == "RELIANCE INDUSTRIES LTD."
    assert row["source"] == "angel"


def test_search_refreshes_the_master_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_file", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(settings, "tick_db_file", str(tmp_path / "ticks.sqlite3"))
    database.initialise()
    with app_cursor() as cursor:
        _insert_instrument(
            cursor, "2885", "RELIANCE INDUSTRIES LTD.", "RELIANCE-EQ", "RELIANCE INDUSTRIES LTD.", "angel",
        )
    search_index.refresh()

    syncs = {"count": 0}

    def fake_sync(**_kwargs):
        syncs["count"] += 1
        with app_cursor() as cursor:
            _insert_instrument(
                cursor, "500325", "TATA MOTORS LTD.", "TATA MOTORS-EQ", "TATA MOTORS LTD.", "angel",
            )
        search_index.invalidate()
        return {"status": "ready", "counts": {"nse_cm": 1}, "total": 1}

    monkeypatch.setattr(master, "sync_angel", fake_sync)

    payload = market.search_instruments(q="TATA", limit=5, scope="equity", user=None)

    assert syncs["count"] == 1
    assert payload["master_refreshed"] is True
    assert payload["results"]
    assert payload["results"][0]["token"] == "500325"
