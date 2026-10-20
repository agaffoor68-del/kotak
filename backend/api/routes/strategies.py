"""Strategy builder, backtesting and algo-run routes."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from backend.api.deps import CurrentUser, angel_call, current_user, require_trader, resolve_account_id
from backend.backtesting import engine
from backend.core.config import settings
from backend.core.database import app_cursor, now, row_to_dict
from backend.execution import algo
from backend.marketdata import master, ticks
from backend.marketdata.timeutils import ist_stamp, parse_ist, window as ist_window
from backend.strategies import dsl, lifecycle, pine
from backend.strategies.dsl import COMPARATORS, CROSS_OPS, PRICE_FIELDS
from backend.strategies.indicators import available
from backend.strategies.templates import TEMPLATES

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])
backtest_router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])
log = logging.getLogger("alphatrade.api.strategies")


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


@router.post("/backtest")
def backtest(payload: BacktestRequest, user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Backtest a strategy over persisted historical candles."""
    if not settings.enable_backtest:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Backtesting is disabled.")

    definition = payload.definition
    if definition is None and payload.strategy_id:
        with app_cursor() as cursor:
            cursor.execute("SELECT definition FROM strategies WHERE id = ?", (payload.strategy_id,))
            row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        definition = json.loads(row["definition"])
    if definition is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide a strategy_id or a definition")

    interval = payload.timeframe or definition.get("timeframe") or "5m"
    if interval not in ticks.INTERVAL_SECONDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported timeframe '{interval}'.",
        )

    def parse_date(value: str | None, end: bool = False) -> float | None:
        """Epoch seconds for a user-supplied date, in **IST**.

        The bars are stamped from IST wall-clock readings, so the window must
        be built on the same clock: reading a naive date as UTC shifts the
        whole window by five and a half hours and drops the session.
        """
        if not value:
            return None
        try:
            parsed = parse_ist(value)
            if end and len(value) == 10:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999_999)
            return parsed.timestamp()
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid date: {value}") from error

    start_ts = parse_date(payload.start_date)
    end_ts = parse_date(payload.end_date, end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date must be before end_date")

    # A selected token is authoritative and is read directly from the durable candle DB.
    requested = payload.symbol_token or None
    universe = definition.get("universe") or []
    if requested:
        instrument = master.get_instrument(requested, "nse_cm") or master.get_instrument(requested, "nse_fo")
        universe = [{"token": requested, "exchange_segment": (instrument or {}).get("exchange_segment", "nse_cm"), "label": (instrument or {}).get("trading_symbol") or (instrument or {}).get("name") or requested}]

    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    coverage: list[dict[str, Any]] = []
    for item in universe:
        token = str(item["token"])
        segment = item.get("exchange_segment", "nse_cm")
        candles = ticks.stored_history(token, segment, interval, limit=50000, start_ts=start_ts, end_ts=end_ts)
        instrument = master.get_instrument(token, segment)
        label = (item.get("label") or (instrument or {}).get("name") or token)
        candles_by_symbol[label] = candles
        coverage.append({"label": label, "token": token, "bars": len(candles), "coverage": {**ticks.coverage(token, segment), "stored_bars": len(candles)}})

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
            error = f"No historical data found for {label}. Please download data first."
        return {"status": "no_data", "error": error, "note": "", "metrics": {}, "trades": [], "equity_curve": [], "drawdown_curve": [], "bars": 0, "warnings": [], "coverage": coverage, "interval": interval}

    result = engine.run(
        definition, candles_by_symbol,
        initial_capital=payload.initial_capital, slippage=payload.slippage, timeframe=interval,
    )
    payload_dict = result.to_dict()

    # Persist the run so the approval lifecycle has evidence to check. Without
    # this a strategy could never legitimately reach "backtested".
    if payload.strategy_id:
        with app_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strategy_runs
                    (id, strategy_id, account_id, mode, status, params, metrics, started_at, finished_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex, payload.strategy_id, resolve_account_id(user), "backtest", "completed",
                    json.dumps({
                        "initial_capital": payload.initial_capital,
                        "slippage": payload.slippage,
                        "interval": interval,
                    }),
                    json.dumps(payload_dict.get("metrics") or {}),
                    now(), now(),
                ),
            )

    return {**payload_dict, "coverage": coverage, "interval": interval}

