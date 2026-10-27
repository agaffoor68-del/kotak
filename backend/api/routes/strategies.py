"""Strategy builder, backtesting and algo-run routes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser, angel_call, current_user, require_trader, resolve_account_id
from backend.backtesting import engine
from backend.core.config import settings
from backend.core.database import app_cursor, now, row_to_dict
from backend.execution import algo
from backend.marketdata import master, ticks
from backend.marketdata.timeutils import ist_stamp, now_ist, parse_ist, window as ist_window
from backend.strategies import dsl, lifecycle, pine
from backend.strategies.dsl import COMPARATORS, CROSS_OPS, PRICE_FIELDS
from backend.strategies.indicators import available
from backend.strategies.templates import TEMPLATES

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])
backtest_router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])
log = logging.getLogger("alphatrade.api.strategies")

#: How long one history download is allowed to run. Server-side, never the
#: caller's: a request that decides its own timeout can hold a worker thread
#: for as long as it likes, and the reverse proxy in front of the API cuts the
#: request off at 120s anyway.
DOWNLOAD_TIMEOUT_SECONDS = 90

#: The most history one request may ask for, in days. Ten years of daily bars
#: is far more than any strategy needs, and the download is written to the
#: persistent volume.
MAX_HISTORY_DAYS = 3650

#: The history downloads run here, not in a pool per request. One bounded
#: pool means the number of downloads in flight is capped no matter how many
#: requests arrive, and a download that outlives its request cannot multiply.
_download_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backtest-download")


class StrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    definition: dict[str, Any]
    is_template: bool = False


class HistoricalDownloadRequest(BaseModel):
    symbol_token: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=120)
    start_date: date
    end_date: date
    timeframe: str = Field(default="5m", pattern="^(1m|3m|5m|15m|30m|60m|1d)$")

    @field_validator("symbol_token", "symbol", mode="before")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()


class BacktestRequest(BaseModel):
    strategy_id: str | None = None
    definition: dict[str, Any] | None = None
    initial_capital: float = Field(default=500_000.0, gt=0)
    slippage: str = Field(default="moderate")
    timeframe: str | None = None
    symbol_token: str | None = Field(default=None, min_length=1, max_length=64)
    start_date: str | None = None
    end_date: str | None = None
    history_days: int = Field(default=5, ge=1, le=1800)

    @field_validator("strategy_id", "symbol_token", mode="before")
    @classmethod
    def reject_blank_identifiers(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()


class AutoBacktestRequest(BacktestRequest):
    """A one-click backtest: a symbol in, the result out.

    The symbol is resolved against the instrument master, the database is
    checked for the requested window, and whatever is missing is downloaded
    before the simulation runs - all inside one request.
    """

    symbol: str | None = Field(default=None, min_length=1, max_length=120)
    exchange_segment: str | None = Field(default=None, max_length=32)
    auto_download: bool = True


class StartRunRequest(BaseModel):
    mode: str = Field(default="paper", pattern="^(paper|live)$")

class PineImportRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str | None = None
    timeframe: str | None = None
    token: str = "26000"
    label: str = "NIFTY 50"


class PineExportRequest(BaseModel):
    definition: dict[str, Any] | None = None
    strategy_id: str | None = None


@router.get("/indicators")
def list_indicators(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Everything the no-code builder can offer."""
    return {
        "indicators": available(),
        "price_fields": list(PRICE_FIELDS),
        "comparators": sorted(COMPARATORS),
        "cross_comparators": sorted(CROSS_OPS),
        "logical": ["all", "any", "not"],
        "timeframes": ticks.supported_intervals(),
    }


@router.get("/templates")
def list_templates(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, description, kind, definition FROM strategies WHERE is_template = 1 ORDER BY name"
        )
        rows = [row_to_dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["valid"] = dsl.validate(row["definition"])["valid"]
    return rows


@router.post("/validate")
def validate_strategy(definition: dict[str, Any], user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Validate a strategy document, as the builder types."""
    return dsl.validate(definition)


@router.post("/explain")
def explain_strategy(payload: dict[str, Any], user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Evaluate a rule tree against the most recent recorded candles."""
    definition = payload.get("definition") or payload
    node = payload.get("node")
    token = str(payload.get("token") or "")
    segment = str(payload.get("exchange_segment") or "nse_cm")
    interval = str(payload.get("interval") or definition.get("timeframe") or "5m")

    candles = ticks.history(token, segment, interval, 400) if token else []
    if not candles:
        return {
            "available": False,
            "note": (
                "No recorded candles for this instrument yet. Kotak Neo publishes no historical data, "
                "so the tape builds from when the feed starts recording."
            ),
        }
    cache = dsl.IndicatorCache(candles)
    return {
        "available": True,
        "bar_index": len(candles) - 1,
        "result": dsl.explain(node or definition.get("entry"), cache, len(candles) - 1),
        "exit_result": dsl.explain(definition.get("exit"), cache, len(candles) - 1),
    }


# ---------------------------------------------------------------- pine script


@router.post("/pine/import")
def import_pine(payload: PineImportRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Convert a supported subset of Pine Script into a strategy document.

    Only the rules this platform can evaluate are translated; anything else is
    reported in ``warnings`` rather than being guessed at.
    """
    return pine.import_pine(
        payload.code,
        name=payload.name,
        timeframe=payload.timeframe,
        token=payload.token,
        label=payload.label,
    )


@router.post("/pine/export")
def export_pine(payload: PineExportRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Render a strategy document (or a saved strategy) as Pine Script v5."""
    definition = payload.definition
    if definition is None and payload.strategy_id:
        with app_cursor() as cursor:
            cursor.execute("SELECT definition FROM strategies WHERE id = ?", (payload.strategy_id,))
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        definition = json.loads(row["definition"])
    if definition is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide a definition or a strategy_id")
    return pine.export_pine(definition)


@router.get("")
def list_strategies(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    account_id = resolve_account_id(user)
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM strategies WHERE account_id = ? AND is_template = 0 ORDER BY updated_at DESC",
            (account_id,),
        )
        return [row_to_dict(row) for row in cursor.fetchall()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(payload: StrategyRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    validation = dsl.validate(payload.definition)
    if not validation["valid"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": validation["errors"]})
    account_id = resolve_account_id(user)
    strategy_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO strategies (id, account_id, name, description, kind, definition, is_template, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (strategy_id, account_id, payload.name, payload.description,
             payload.definition.get("kind", "rule"), json.dumps(payload.definition),
             1 if payload.is_template else 0, now(), now()),
        )
    return {"id": strategy_id, "name": payload.name}


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        row = row_to_dict(cursor.fetchone())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    row["validation"] = dsl.validate(row["definition"])
    row["resolved_universe"] = algo.instrument_label(row["definition"])
    return row


@router.put("/{strategy_id}")
def update_strategy(strategy_id: str, payload: StrategyRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    validation = dsl.validate(payload.definition)
    if not validation["valid"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": validation["errors"]})
    with app_cursor() as cursor:
        cursor.execute(
            "UPDATE strategies SET name = ?, description = ?, definition = ?, updated_at = ? WHERE id = ?",
            (payload.name, payload.description, json.dumps(payload.definition), now(), strategy_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return {"id": strategy_id, "updated": True}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM strategies WHERE id = ? AND is_template = 0", (strategy_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return {"deleted": True, "id": strategy_id}


# -------------------------------------------------------------- approval flow


class PromoteRequest(BaseModel):
    to_state: str
    reason: str = Field(default="", max_length=500)


def _load_strategy(strategy_id: str, user: CurrentUser) -> dict[str, Any]:
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        row = row_to_dict(cursor.fetchone())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    if row["account_id"] != resolve_account_id(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That strategy belongs to another account")
    return row


@router.get("/lifecycle/states")
def lifecycle_states(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """The pipeline and its thresholds, so the UI renders the same rules."""
    return {
        "states": list(lifecycle.STATES),
        "transitions": {k: sorted(v) for k, v in lifecycle.TRANSITIONS.items()},
        "forward_requirements": {
            "min_seconds": lifecycle.MIN_FORWARD_SECONDS,
            "min_trades": lifecycle.FORWARD_MIN_TRADES,
            "max_drawdown_pct": lifecycle.FORWARD_MAX_DRAWDOWN_PCT,
        },
    }


@router.get("/{strategy_id}/lifecycle")
def strategy_lifecycle(strategy_id: str, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    row = _load_strategy(strategy_id, user)
    state = lifecycle.current_state(row)
    return {
        "strategy_id": strategy_id,
        "state": state,
        "approved_by": row.get("approved_by"),
        "approved_at": row.get("approved_at"),
        "allowed_transitions": sorted(lifecycle.TRANSITIONS.get(state, set())),
        "backtest": lifecycle.backtest_evidence(strategy_id),
        "forward": lifecycle.forward_evidence(strategy_id),
        "history": lifecycle.history(strategy_id),
    }


@router.post("/{strategy_id}/lifecycle")
def promote_strategy(
    strategy_id: str,
    payload: PromoteRequest,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Advance a strategy through backtest -> forward test -> approval -> live."""
    row = _load_strategy(strategy_id, user)
    try:
        return lifecycle.promote(row, payload.to_state, user.email, payload.reason or None)
    except lifecycle.LifecycleError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error


# ---------------------------------------------------------------- backtesting


def _angel_bridge(user: CurrentUser) -> Callable[..., dict[str, Any]] | None:
    """The SmartAPI request bridge, or ``None`` when Angel is not configured.

    yfinance alone is enough for the normal path, so a deployment without
    Angel credentials must still be able to download history.
    """
    try:
        return angel_call(user)
    except HTTPException as error:
        log.info("Angel fallback unavailable: %s", error.detail)
        return None


def _load_definition(strategy_id: str | None, definition: dict[str, Any] | None) -> dict[str, Any]:
    """The strategy document to simulate, from the request or the database."""
    if definition is not None:
        return definition
    if strategy_id:
        with app_cursor() as cursor:
            cursor.execute("SELECT definition FROM strategies WHERE id = ?", (strategy_id,))
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        return json.loads(row["definition"])
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide a strategy_id or a definition")


def _resolve_interval(timeframe: str | None, definition: dict[str, Any]) -> str:
    interval = timeframe or definition.get("timeframe") or "5m"
    if interval not in ticks.INTERVAL_SECONDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unsupported timeframe '{interval}'.")
    return interval


def _parse_window(start_date: str | None, end_date: str | None) -> tuple[float | None, float | None]:
    """Epoch bounds for a user-supplied date range, read on the IST clock.

    The bars are stamped from IST wall-clock readings, so the window must be
    built on the same clock: reading a naive date as UTC shifts the whole
    window by five and a half hours and drops the session.
    """

    def parse(value: str | None, end: bool = False) -> float | None:
        if not value:
            return None
        try:
            parsed = parse_ist(value)
            if end and len(value) == 10:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999_999)
            return parsed.timestamp()
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid date: {value}") from error

    start_ts = parse(start_date)
    end_ts = parse(end_date, end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date must be before end_date")
    return start_ts, end_ts


def _universe_for(
    definition: dict[str, Any],
    token: str | None,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """The instruments the backtest runs over.

    A selected token is authoritative: it replaces the strategy's own
    universe, so the same strategy can be replayed on any instrument.
    """
    universe = definition.get("universe") or []
    if token:
        instrument = master.get_instrument(token, "nse_cm") or master.get_instrument(token, "nse_fo")
        universe = [{
            "token": token,
            "exchange_segment": (instrument or {}).get("exchange_segment", "nse_cm"),
            "label": label or (instrument or {}).get("trading_symbol") or (instrument or {}).get("name") or token,
        }]
    return universe


def _collect_candles(
    universe: list[dict[str, Any]],
    interval: str,
    start_ts: float | None,
    end_ts: float | None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read the stored candles for every instrument in the universe."""
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    coverage: list[dict[str, Any]] = []
    for item in universe:
        token = str(item["token"])
        segment = item.get("exchange_segment", "nse_cm")
        # The token is the key: the rows are stored under exactly the token the
        # downloader was given, so the query reads them under the same string.
        candles = ticks.stored_history(token, segment, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)
        if not candles:
            # The download may have filed the bars under another exchange
            # segment. The token still identifies them.
            candles = ticks.stored_history(token, None, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)
        log.info(f"Backtest query returned {len(candles)} rows for token {token}")
        instrument = master.get_instrument(token, segment)
        label = (item.get("label") or (instrument or {}).get("name") or token)
        candles_by_symbol[label] = candles
        coverage.append({"label": label, "token": token, "bars": len(candles), "coverage": {**ticks.coverage(token, segment), "stored_bars": len(candles)}})
    return candles_by_symbol, coverage


def _simulate(
    definition: dict[str, Any],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    coverage: list[dict[str, Any]],
    interval: str,
    initial_capital: float,
    slippage: str,
    strategy_id: str | None,
    user: CurrentUser,
) -> dict[str, Any]:
    """Run the engine and record the run for the approval lifecycle."""
    result = engine.run(
        definition, candles_by_symbol,
        initial_capital=initial_capital, slippage=slippage, timeframe=interval,
    )
    payload_dict = result.to_dict()
    if result.status == "ok":
        payload_dict["saved_backtest_trades"] = engine.save_backtest_trades(
            resolve_account_id(user), result.trades, strategy_id
        )

    # Persist the run so the approval lifecycle has evidence to check. Without
    # this a strategy could never legitimately reach "backtested".
    if strategy_id:
        with app_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strategy_runs
                    (id, strategy_id, account_id, mode, status, params, metrics, started_at, finished_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex, strategy_id, resolve_account_id(user), "backtest", "completed",
                    json.dumps({
                        "initial_capital": initial_capital,
                        "slippage": slippage,
                        "interval": interval,
                    }),
                    json.dumps(payload_dict.get("metrics") or {}),
                    now(), now(),
                ),
            )

    return {**payload_dict, "coverage": coverage, "interval": interval}


def _no_data_result(
    coverage: list[dict[str, Any]],
    interval: str,
    token: str | None,
    error: str,
) -> dict[str, Any]:
    return {
        "status": "no_data", "error": error, "note": "", "metrics": {}, "trades": [],
        "equity_curve": [], "drawdown_curve": [], "bars": 0, "warnings": [],
        "coverage": coverage, "interval": interval, "symbol_token": token,
    }


@router.post("/backtest")
def backtest(payload: BacktestRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Backtest a strategy over persisted historical candles."""
    if not settings.enable_backtest:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Backtesting is disabled.")

    definition = _load_definition(payload.strategy_id, payload.definition)
    interval = _resolve_interval(payload.timeframe, definition)
    start_ts, end_ts = _parse_window(payload.start_date, payload.end_date)

    requested = payload.symbol_token or None
    universe = _universe_for(definition, requested)
    candles_by_symbol, coverage = _collect_candles(universe, interval, start_ts, end_ts)

    if requested and not candles_by_symbol.get(next(iter(candles_by_symbol), "")):
        label = (coverage[0]["label"] if coverage else requested)
        stored = ticks.stored_range(requested, interval)
        if stored["bars"]:
            error = (
                f"No historical data found for {label} between {payload.start_date or 'the start'} and "
                f"{payload.end_date or 'the end'} (IST). Stored: {stored['bars']} bars from "
                f"{ist_stamp(stored['first_bar'])} to {ist_stamp(stored['last_bar'])}."
            )
        else:
            error = f"No data in DB for token {requested}. Run the downloader first."
        return _no_data_result(coverage, interval, requested, error)

    return _simulate(
        definition, candles_by_symbol, coverage, interval,
        payload.initial_capital, payload.slippage, payload.strategy_id, user,
    )


@backtest_router.post("/run")
def run_backtest(payload: AutoBacktestRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Run a backtest end to end: resolve the symbol, fetch what is missing, simulate.

    One click, one answer. The database is checked for the requested window
    first; whatever is missing is downloaded (yfinance, with Angel One as the
    fallback) and written under the instrument's exact token, and the
    simulation then runs over those candles. The response carries the
    result - win rate, P&L, trades - and what the download step did.
    """
    if not settings.enable_backtest:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Backtesting is disabled.")

    definition = _load_definition(payload.strategy_id, payload.definition)
    interval = _resolve_interval(payload.timeframe, definition)

    instrument = _resolve_instrument(payload, definition)
    token = str(instrument["token"])
    segment = str(instrument.get("exchange_segment") or "nse_cm")
    label = str(instrument.get("trading_symbol") or instrument.get("symbol") or token)

    # The window: the dates the user typed, or the last N days.
    if payload.start_date or payload.end_date:
        start_ts, end_ts = _parse_window(payload.start_date, payload.end_date)
        start, end = ist_window(payload.start_date or payload.end_date, payload.end_date or payload.start_date)
    else:
        end = now_ist()
        start = end - timedelta(days=max(1, payload.history_days))
        start_ts, end_ts = start.timestamp(), end.timestamp()

    # 1. What the database already holds.
    candles = ticks.stored_history(token, segment, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)
    if not candles:
        candles = ticks.stored_history(token, None, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)

    download: dict[str, Any] = {
        "attempted": False, "performed": False, "bars": 0,
        "source": None, "error": None, "seconds": 0.0,
    }

    # 2. Anything missing is downloaded, in a worker thread, and waited for.
    if payload.auto_download and _needs_download(candles, start_ts, end_ts, interval):
        download = _download_missing(token, segment, interval, start, end, payload, user)

    # 3. The freshly written bars are what the simulation reads.
    candles = ticks.stored_history(token, segment, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)
    if not candles:
        candles = ticks.stored_history(token, None, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)

    universe = _universe_for(definition, token, label)
    candles_by_symbol, coverage = _collect_candles(universe, interval, start_ts, end_ts)

    if not candles_by_symbol.get(next(iter(candles_by_symbol), "")):
        stored = ticks.stored_range(token, interval)
        if stored["bars"]:
            error = (
                f"No historical data found for {label} between {payload.start_date or 'the start'} and "
                f"{payload.end_date or 'the end'} (IST). Stored: {stored['bars']} bars from "
                f"{ist_stamp(stored['first_bar'])} to {ist_stamp(stored['last_bar'])}."
            )
        else:
            error = f"No data in DB for token {token}. Run the downloader first."
        return {**_no_data_result(coverage, interval, token, error), "download": download, "symbol": label}

    result = _simulate(
        definition, candles_by_symbol, coverage, interval,
        payload.initial_capital, payload.slippage, payload.strategy_id, user,
    )
    return {
        **result,
        "download": download,
        "symbol": label,
        "symbol_token": token,
        "exchange_segment": segment,
    }


def _resolve_instrument(payload: AutoBacktestRequest, definition: dict[str, Any]) -> dict[str, Any]:
    """The instrument the backtest runs over, from a symbol or a token."""
    if payload.symbol_token:
        token = str(payload.symbol_token).strip()
        instrument = (
            master.get_instrument(token, "nse_cm")
            or master.get_instrument(token, "nse_fo")
            or master.find_instrument(token)
        )
        if instrument is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instrument in the master for token {token}")
        return instrument

    if payload.symbol:
        instrument = master.find_instrument(payload.symbol, payload.exchange_segment)
        if instrument is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No instrument in the master for '{payload.symbol}'. Search the symbol list to pick one.",
            )
        return instrument

    universe = definition.get("universe") or []
    if universe:
        token = str(universe[0].get("token") or "")
        instrument = (
            master.get_instrument(token, "nse_cm")
            or master.get_instrument(token, "nse_fo")
            or master.find_instrument(token)
        )
        if instrument is not None:
            return instrument

    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select the symbol to backtest")


def _needs_download(
    candles: list[dict[str, Any]],
    start_ts: float | None,
    end_ts: float | None,
    interval: str,
) -> bool:
    """True when the stored bars do not cover the requested window."""
    if not candles:
        return True
    if start_ts is None or end_ts is None:
        return False
    seconds = ticks.INTERVAL_SECONDS[interval]
    first = float(candles[0]["time"])
    last = float(candles[-1]["time"])
    return first > start_ts + seconds or last < end_ts - seconds


def _download_missing(
    token: str,
    segment: str,
    interval: str,
    start: Any,
    end: Any,
    payload: AutoBacktestRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    """Download the missing history in a worker thread and wait for it.

    The thread keeps the API responsive while the download runs, and the
    request waits for it: the whole point of this endpoint is that one click
    produces one result.
    """
    from backend.marketdata.yf_historical_loader import HistoricalDataError, download_history

    summary: dict[str, Any] = {"attempted": True, "performed": False, "bars": 0, "source": None, "error": None, "seconds": 0.0}
    started = time.time()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest-download")
    try:
        future = pool.submit(
            download_history, token, segment, interval, start, end,
            angel_call=_angel_bridge(user),
        )
        try:
            result = future.result(timeout=max(30, 90))  # TODO: review fix
        except TimeoutError:
            summary["error"] = f"The historical download did not finish within {90}s."  # TODO: review fix
            log.warning("Historical download timed out for %s", token)
        except HistoricalDataError as error:
            summary["error"] = str(error)
            log.warning("Historical download failed for %s: %s", token, error)
        else:
            summary["performed"] = True
            summary["source"] = result.get("source")
            summary["bars"] = int(result.get("rows") or 0)
            summary["yf_ticker"] = result.get("yf_ticker")
    finally:
        # The download keeps running in the background if it timed out; the
        # request does not wait for it.
        pool.shutdown(wait=False)
    summary["seconds"] = round(time.time() - started, 2)
    return summary


@backtest_router.post("/download-data")
def download_historical_data(
    payload: HistoricalDownloadRequest,
    user: CurrentUser = Depends(require_trader),
) -> dict[str, Any]:
    """Download a bounded Yahoo Finance range and persist it as durable candles.

    The token the frontend sends is the token the rows are written under, so
    the backtest query finds them under exactly the same key. The window is
    interpreted in IST, which is the clock the bars are stamped with.
    """
    if payload.start_date > payload.end_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date must be before end_date")
    if (payload.end_date - payload.start_date).days > 3650:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Date range cannot exceed 10 years")

    instrument = master.get_instrument(payload.symbol_token, "nse_cm") or master.get_instrument(payload.symbol_token, "nse_fo")
    exchange_segment = (instrument or {}).get("exchange_segment", "nse_cm")
    from backend.marketdata.yf_historical_loader import HistoricalDataError, download_history

    # The same IST bounds the backtest query will use.
    start, end = ist_window(payload.start_date, payload.end_date)

    try:
        summary = download_history(
            payload.symbol_token,
            exchange_segment,
            payload.timeframe,
            start,
            end,
            angel_call=_angel_bridge(user),
        )
    except HistoricalDataError as error:
        log.warning("Historical download failed for %s: %s", payload.symbol_token, error)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No historical data found for {payload.symbol}. Please check the symbol and date range.",
        ) from error

    rows = summary["rows"]
    log.info(
        f"Downloaded {rows} rows for {payload.symbol_token} from {start} to {end}"
    )

    saved = ticks.stored_history(
        payload.symbol_token, exchange_segment, payload.timeframe,
        limit=50000,
        start_ts=start.timestamp(),
        end_ts=end.timestamp(),
    )
    if not saved:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No historical data found for {payload.symbol}. Please check the symbol and date range.")
    return {
        "status": "ok",
        "message": "Data downloaded successfully",
        "symbol": payload.symbol,
        "symbol_token": payload.symbol_token,
        "yf_ticker": summary["yf_ticker"],
        "source": summary["source"],
        "timeframe": payload.timeframe,
        "bars": len(saved),
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
    }


