"""Backtest package.

Production-ready historical backtester for the AlphaTradePro platform.

Core entry point: :func:`backend.backtest.engine.run_backtest`.
Data access: :func:`backend.backtest.engine.load_candles` and
:func:`backend.backtest.engine.data_exists`.
"""

from .engine import (  # noqa: F401
    BacktestError,
    NO_DATA_ERROR,
    data_exists,
    load_candles,
    run_backtest,
)

__all__ = [
    "BacktestError",
    "NO_DATA_ERROR",
    "data_exists",
    "load_candles",
    "run_backtest",
]
