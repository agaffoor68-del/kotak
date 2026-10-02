"""Build the compact scrip index used by the trading terminal frontend.

The Kotak Neo Python client ships the full NSE scrip masters (``nse_cm.csv`` for
the cash segment, ``nse_fo.csv`` for derivatives).  They are far too large to
ship to a browser, so this script distils them into one small JSON file that the
Next.js app fetches at runtime:

    web/public/scrip-index.json

Generated shape::

    {
      "source":    "neo_api_client/api/{nse_cm,nse_fo}.csv",
      "generated": "2026-09-28T10:31:24+00:00",
      "shape": {
        "indices":  ["name", "token"],
        "equities": ["name", "tradingSymbol", "token", "series", "lotSize", "tickSize", "precision"],
        "fno":      ["underlying", "lotSize", "tickSize"]
      },
      "indices":  [["NIFTY", "26000"], ...],
      "equities": [["IDEA", "IDEA-EQ", "14366", "EQ", "1", "0.01", "2"], ...],
      "fno":      [["NIFTY", "50", "0.05"], ...]
    }

Notes on the source data:

* ``pSymbolName`` is the plain symbol (``IDEA``), ``pTrdSymbol`` carries the
  series suffix (``IDEA-EQ``) and ``pSymbol`` is the instrument token that the
  Kotak quote API expects.
* Rows with an empty ``pGroup`` are index instruments (``NIFTY``, ``NIFTY BANK``)
  rather than tradable cash scrips, so they are split into ``indices``.
* ``dTickSize`` is expressed in paise, so it is divided by 100.
* ``NSETEST`` scrips are Kotak exchange-conformance symbols, not quotable
  instruments, and are dropped.

Run from the repository root::

    python web/scripts/build-scrip-index.py
"""

from __future__ import annotations

import collections
import csv
import json
import os
from datetime import datetime, timezone

csv.field_size_limit(64 * 1024 * 1024)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CASH_MASTER = os.path.join(REPO_ROOT, "neo_api_client", "api", "nse_cm.csv")
DERIVATIVE_MASTER = os.path.join(REPO_ROOT, "neo_api_client", "api", "nse_fo.csv")
OUTPUT = os.path.join(REPO_ROOT, "web", "public", "scrip-index.json")

# A cash symbol can appear in several series (EQ, BE, BL, ...).  The plain
# equity series is the one the terminal quotes by default.
SERIES_PREFERENCE = ["EQ", "BE", "BZ", "BL", "IV", "N1", "N2", "N3", "MF", "MFU"]
DERIVATIVE_INSTRUMENTS = {"FUTIDX", "OPTIDX", "FUTSTK", "OPTSTK"}
SKIP_SYMBOL_MARKERS = ("NSETEST",)


def read_header(handle) -> dict[str, int]:
    """Return a header lookup with the trailing spaces Kotak includes removed."""
    reader = csv.reader(handle)
    header = [name.strip() for name in next(reader)]
    return {name: position for position, name in enumerate(header)}


def cell(row: list[str], header: dict[str, int], name: str, default: str = "") -> str:
    position = header.get(name)
    if position is None or position >= len(row):
        return default
    return row[position].strip()


def tick_size(raw: str, default: str = "0.05") -> str:
    """Convert a tick size expressed in paise into a rupee value."""
    try:
        return f"{int(float(raw)) / 100:g}"
    except (TypeError, ValueError):
        return default


def build_cash_index() -> tuple[list[list[str]], list[list[str]]]:
    """Return ``(indices, equities)`` derived from the cash scrip master."""
    best: dict[str, tuple[int, list[str]]] = {}
    indices: dict[str, list[str]] = {}

    with open(CASH_MASTER, newline="", encoding="utf-8", errors="replace") as handle:
        header = read_header(handle)
        for row in csv.reader(handle):
            name = cell(row, header, "pSymbolName")
            token = cell(row, header, "pSymbol")
            if not name or not token:
                continue
            if any(marker in name for marker in SKIP_SYMBOL_MARKERS):
                continue
            series = cell(row, header, "pGroup")
            if not series:
                indices.setdefault(name, [name, token])
                continue
            trading_symbol = cell(row, header, "pTrdSymbol", name)
            rank = (
                SERIES_PREFERENCE.index(series)
                if series in SERIES_PREFERENCE
                else len(SERIES_PREFERENCE)
            )
            entry = [
                name,
                trading_symbol,
                token,
                series,
                cell(row, header, "lLotSize", "1") or "1",
                tick_size(cell(row, header, "dTickSize")),
                cell(row, header, "lPrecision", "2") or "2",
            ]
            current = best.get(name)
            if current is None or rank < current[0]:
                best[name] = (rank, entry)

    equities = sorted((value for _, value in best.values()), key=lambda item: item[0])
    index_rows = sorted(indices.values(), key=lambda item: item[0])
    return index_rows, equities


def build_derivative_index() -> list[list[str]]:
    """Return distinct F&O underlyings with their lot and tick sizes."""
    lots: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    ticks: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    with open(DERIVATIVE_MASTER, newline="", encoding="utf-8", errors="replace") as handle:
        header = read_header(handle)
        for row in csv.reader(handle):
            if cell(row, header, "pInstType") not in DERIVATIVE_INSTRUMENTS:
                continue
            underlying = cell(row, header, "pSymbolName")
            if not underlying:
                continue
            lots[underlying][cell(row, header, "lLotSize", "1") or "1"] += 1
            ticks[underlying][tick_size(cell(row, header, "dTickSize"))] += 1
    return [
        [underlying, lots[underlying].most_common(1)[0][0], ticks[underlying].most_common(1)[0][0]]
        for underlying in sorted(lots)
    ]


def main() -> None:
    indices, equities = build_cash_index()
    derivatives = build_derivative_index()
    payload = {
        "source": "neo_api_client/api/{nse_cm,nse_fo}.csv",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shape": {
            "indices": ["name", "token"],
            "equities": ["name", "tradingSymbol", "token", "series", "lotSize", "tickSize", "precision"],
            "fno": ["underlying", "lotSize", "tickSize"],
        },
        "indices": indices,
        "equities": equities,
        "fno": derivatives,
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
    print(f"indices   : {len(indices)}")
    print(f"equities  : {len(equities)}")
    print(f"fno names : {len(derivatives)}")
    print(f"bytes     : {os.path.getsize(OUTPUT)}")
    print(f"written   : {OUTPUT}")


if __name__ == "__main__":
    main()
