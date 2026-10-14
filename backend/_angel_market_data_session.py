"""Angel One MARKET-DATA session — read-only, no order placement.

Connects a SmartAPI session (API key + client code + PIN + TOTP), prints the
session state, syncs the instrument master if needed, then polls live quotes
for a small watchlist and prints them to this terminal.

This runner deliberately touches only the data layer:

* ``backend.angel.auth``        — login / refresh / logout
* ``backend.angel.instruments`` — scrip-master sync (public SmartAPI JSON)
* ``backend.angel.quotes``      — ``getLTP`` + ``market/v1/quote`` (read-only)

It never imports ``backend.angel.orders`` and never calls any place / modify /
cancel endpoint. There is no static-IP or SEBI order validation here: those
apply to order APIs, not to market data, and ``ENABLE_LIVE_TRADING`` stays off.

Credentials come from the environment (loaded from the project ``.env`` by
``backend.app_bootstrap``):

* ``ANGEL_API_KEY``      SmartAPI app key (smartapi.angelone.in -> My Apps)
* ``ANGEL_CLIENT_CODE``  Angel login id, e.g. ``S53557129``
* ``ANGEL_PIN``          4-digit account PIN
* ``ANGEL_TOTP_KEY``     Base32 secret behind the authenticator QR
                         (NOT the rotating 6-digit code)
* ``ANGEL_ENVIRONMENT``  ``prod`` (default) or ``uat`` for sandbox keys

Usage (from the repo root, inside the venv):

    python backend/_angel_market_data_session.py
    python backend/_angel_market_data_session.py --symbols NIFTY,BANKNIFTY,RELIANCE --seconds 60
    python backend/_angel_market_data_session.py --logout
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import backend.app_bootstrap  # noqa: F401  — loads .env before `settings` is built

from backend.angel import instruments as angel_instruments
from backend.angel import quotes as angel_quotes
from backend.angel.auth import AngelAuthError, angel_session_manager
from backend.core.config import settings
from backend.core.database import initialise
from backend.core.vault import AngelCredentials
from backend.marketdata import master

#: A tiny liquid watchlist so the session proves itself immediately.
DEFAULT_SYMBOLS = ("NIFTY", "BANKNIFTY", "RELIANCE")

REQUIRED_ENV_VARS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_KEY")


def _credentials_from_env() -> AngelCredentials:
    """Build the SmartAPI credentials from the environment, or explain what is missing."""
    credentials = AngelCredentials.from_environment()
    if credentials is not None:
        return credentials
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]
    detail = (
        ", ".join(missing)
        if missing
        else "all four are set but KOTAK_LEGACY_ENV_FALLBACK=0 disables the environment fallback"
    )
    raise SystemExit(
        "Angel One credentials are not configured (" + detail + "). "
        "Set them in the project .env and re-run."
    )


def _resolve_watchlist(symbols: list[str]) -> list[tuple[str, str, str]]:
    """Map each requested symbol to ``(token, trading_symbol, segment)`` via the master."""
    if not master.status()["instruments"]:
        print("Instrument master is empty — syncing from SmartAPI (public JSON, no auth needed) ...")
        result = angel_instruments.bootstrap_if_empty()
        print(f"  sync: {result.get('status')}  {result.get('counts') or result.get('error') or ''}")

    resolved: list[tuple[str, str, str]] = []
    for symbol in symbols:
        # `master.search` orders index-first then cash-before-options, so the
        # first row is what a trader means by "NIFTY" or "RELIANCE".
        rows = master.search(symbol, limit=10)
        if not rows:
            print(f"  ! {symbol}: not in the Angel instrument master — skipped")
            continue
        row = rows[0]
        resolved.append((str(row["token"]), str(row["trading_symbol"]), str(row["exchange_segment"])))
        print(
            f"  {symbol:<12} -> {str(row['trading_symbol']):<22} "
            f"token={row['token']} segment={row['exchange_segment']}"
        )
    return resolved


def _format(value: float | None, digits: int = 2) -> str:
    return "--" if value is None else f"{value:,.{digits}f}"


def _print_quotes(quotes: list[Any], labels: dict[str, str]) -> None:
    for quote in sorted(quotes, key=lambda item: labels.get(item.token, item.token)):
        label = labels.get(quote.token, quote.token)
        print(
            f"  {label:<22} LTP {_format(quote.last):>10}  "
            f"O {_format(quote.open):>9} H {_format(quote.high):>9} L {_format(quote.low):>9}  "
            f"CHG% {_format(quote.change_percent):>7}  "
            f"VOL {_format(quote.volume, 0):>12}  "
            f"B/A {_format(quote.bid)}/{_format(quote.ask)}"
        )


def _market_note() -> str:
    try:
        from backend.marketdata import market_hours

        state = market_hours.session_state()
        return f"{state.get('state')} — {state.get('reason')}"
    except Exception:  # noqa: BLE001 - market-hours awareness is a nicety, never fatal
        return "unknown"

def _base_url() -> str:
    from backend.angel.auth import base_url

    return base_url()


def run(symbols: list[str], seconds: float, interval: float, logout: bool) -> int:
    credentials = _credentials_from_env()
    masked = credentials.masked()  # never prints the api_key/pin/totp values
    print("=== Angel One MARKET-DATA session (read-only) ===")
    print(f"  client code  : {masked['client_code']}")
    print(f"  api key      : {masked['api_key']}")
    print(f"  pin / totp   : {masked['pin']} / {masked['totp_key']}")
    print(f"  environment  : {masked['environment']}  (base {_base_url()})")
    print(f"  market state : {_market_note()}")
    print("  order placement: DISABLED — this session only reads market data")
    print()

    # Tables must exist before the master is queried. No user seeding, no
    # trading setup: `initialise()` only creates the local sqlite schema.
    initialise()

    account_id = credentials.client_code

    # 1. Login: PIN + TOTP -> JWT (+ refresh token, persisted under DATA_DIR).
    try:
        session = angel_session_manager.get(account_id, credentials)
    except AngelAuthError as error:
        print(f"LOGIN FAILED: {error}")
        print(
            "Hints: ANGEL_API_KEY is the SmartAPI app key; ANGEL_TOTP_KEY is the "
            "Base32 SECRET (not the rotating 6-digit code); ANGEL_PIN is the 4-digit PIN."
        )
        return 1

    profile = session.profile or {}
    print("LOGIN OK — session established:")
    print(f"  client code : {profile.get('clientcode') or account_id}")
    print(f"  user name   : {profile.get('name') or '(not returned)'}")
    print(f"  email/phone : {profile.get('email') or '--'} / {profile.get('mobileno') or '--'}")
    print(f"  feed token  : {'present' if session.feed_token else 'not issued (REST quotes do not need it)'}")
    print()

    def call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """One authenticated SmartAPI request — the same bridge the feed uses."""
        return angel_session_manager.call(account_id, credentials, method, path, **kwargs)

    # 2. Instrument master (public JSON) so tokens can be resolved.
    print("Watchlist resolution:")
    watchlist = _resolve_watchlist(symbols)
    if not watchlist:
        print("No requested symbol could be resolved; nothing to quote.")
        return 1
    keys = [(token, segment) for token, _symbol, segment in watchlist]
    labels = {token: trading_symbol for token, trading_symbol, _segment in watchlist}
    print()

    # 3. Poll the read-only quote endpoints and print real values only.
    deadline = time.time() + seconds
    cycle = 0
    failures = 0
    print(f"Polling SmartAPI quotes every {interval:.1f}s for {seconds:.0f}s (Ctrl+C to stop):")
    try:
        while time.time() < deadline:
            cycle += 1
            try:
                quotes = angel_quotes.fetch(call, keys, quote_type="all")
            except (AngelAuthError, angel_quotes.QuoteError) as error:
                failures += 1
                print(f"  [{time.strftime('%H:%M:%S')}] quote poll failed: {error}")
            else:
                print(f"  [{time.strftime('%H:%M:%S')}] cycle {cycle}: {len(quotes)} quote(s)")
                _print_quotes(quotes, labels)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped by user.")

    status = angel_session_manager.health(account_id).get("status")
    print(f"\nDone: {cycle} poll cycle(s), {failures} failure(s). Session status: {status}")

    if logout:
        angel_session_manager.logout(account_id)
        print("Session logged out and invalidated on SmartAPI.")
    else:
        print("Session left open (refresh token cached in DATA_DIR); use --logout to invalidate it.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Angel One market-data session (read-only, no order placement)"
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help=f"comma-separated symbols to quote (default: {','.join(DEFAULT_SYMBOLS)})",
    )
    parser.add_argument("--seconds", type=float, default=30.0, help="how long to poll (default: 30)")
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between polls (default: rate-limit aware)",
    )
    parser.add_argument("--logout", action="store_true", help="invalidate the SmartAPI session at the end")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    interval = (
        args.interval
        if args.interval and args.interval > 0
        else max(1.0, 1.0 / max(1, settings.angel_rate_limit_per_second))
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return run(symbols, max(0.0, args.seconds), interval, args.logout)


if __name__ == "__main__":
    sys.exit(main())

