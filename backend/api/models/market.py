from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class IndexData(StrictModel):
    symbol: str
    token: str
    exchange_segment: str
    last: float | None = None
    change: float | None = None
    change_percent: float | None = None
    updated_at: float | None = None


class BreadthData(StrictModel):
    advances: int = 0
    declines: int = 0
    unchanged: int = 0
    priced: int = 0
    advance_decline_ratio: float | None = None
    volume_ratio: float | None = None
    limit_up: int = 0
    limit_down: int = 0


class MoverData(StrictModel):
    token: str
    exchange_segment: str
    last: float | None = None
    change_percent: float | None = None
    volume: float | None = None


class MarketOverview(StrictModel):
    type: str = "market_overview"
    as_of: float
    indices: list[IndexData] = Field(default_factory=list)
    breadth: BreadthData = Field(default_factory=BreadthData)
    top_gainers: list[MoverData] = Field(default_factory=list)
    top_losers: list[MoverData] = Field(default_factory=list)


class StrikeData(StrictModel):
    strike: float
    call_oi: float | None = None
    put_oi: float | None = None
    call_ltp: float | None = None
    put_ltp: float | None = None
    call_volume: float | None = None
    put_volume: float | None = None
    call_iv: float | None = None
    put_iv: float | None = None
    call_delta: float | None = None
    put_delta: float | None = None


class OptionChain(StrictModel):
    type: str = "option_chain"
    underlying: str
    spot_price: float | None = None
    expiry: str | None = None
    atm_strike: float | None = None
    strikes: list[StrikeData] = Field(default_factory=list)


def quote_to_mover(quote: Any) -> MoverData:
    data = quote.to_dict() if hasattr(quote, "to_dict") else quote
    return MoverData(
        token=str(data.get("token", "")), exchange_segment=str(data.get("exchange_segment", "")),
        last=data.get("last"), change_percent=data.get("change_percent"), volume=data.get("volume"),
    )
