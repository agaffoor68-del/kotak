"""Application bootstrap: load .env, validate config, create tables, seed users."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("alphatrade.bootstrap")

#: Load the project .env before anything reads `settings`.
ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / ".env", ROOT / "backend" / ".env"):
    if candidate.exists():
        load_dotenv(candidate, override=False)
        log.debug("Loaded environment from %s", candidate)


def configure_logging() -> None:
    from backend.core.config import settings

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("neo_api_client").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def seed_users() -> None:
    """Create the bootstrap admin (and optional viewer) on first boot.

    No-op once a user exists, so restarts never reset credentials.
    """
    from backend.core.config import settings
    from backend.core.database import app_cursor, now
    from backend.core.security import hash_password

    with app_cursor() as cursor:
        existing = cursor.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if existing:
        return

    if not settings.bootstrap_admin_password:
        log.warning(
            "No users exist and BOOTSTRAP_ADMIN_PASSWORD is not set — "
            "set it and restart to create the first admin."
        )
        return

    with app_cursor() as cursor:
        _ensure_user(
            cursor,
            email=settings.bootstrap_admin_email.lower(),
            password=settings.bootstrap_admin_password,
            role="admin",
        )
        log.info("Ensured bootstrap admin %s", settings.bootstrap_admin_email)

        if settings.bootstrap_viewer_email and settings.bootstrap_viewer_password:
            _ensure_user(
                cursor,
                email=settings.bootstrap_viewer_email.lower(),
                password=settings.bootstrap_viewer_password,
                role="viewer",
            )
            log.info("Ensured bootstrap viewer %s", settings.bootstrap_viewer_email)


def _ensure_user(cursor, *, email: str, password: str, role: str) -> None:
    """Insert-or-noop per email, so reordered/retried boots never violate
    the UNIQUE constraint on users.email.

    The old code did one COUNT(*) check for the whole table, then inserted
    admin + viewer in a single transaction: if the admin insert committed on
    a previous crashed boot (or admin email == viewer email), the second
    INSERT raised sqlite3.IntegrityError and killed the boot loop forever.
    """
    from backend.core.database import now
    from backend.core.security import hash_password

    cursor.execute("SELECT 1 FROM users WHERE email = ?", (email,))
    if cursor.fetchone():
        return
    cursor.execute(
        "INSERT INTO users (id, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,1,?)",
        (uuid.uuid4().hex, email, hash_password(password), role, now()),
    )


def seed_risk_defaults() -> None:
    """Give every known account a sane, explicit risk profile.

    Limits are deliberately conservative but **enabled**, so the risk engine
    blocks reckless orders from the first trade rather than after a loss.
    """
    from backend.broker import accounts
    from backend.core.database import app_cursor, now
    from backend.risk.engine import DEFAULT_RISK_CONFIG

    targets = {row["account_id"] for row in accounts.list_accounts()}
    default_account = accounts.default_account_id()
    if default_account:
        targets.add(default_account)

    with app_cursor() as cursor:
        for account_id in targets:
            cursor.execute("SELECT 1 FROM risk_config WHERE account_id = ?", (account_id,))
            if cursor.fetchone():
                continue
            cursor.execute(
                """
                INSERT INTO risk_config (account_id, max_daily_loss, max_drawdown, max_position_pct,
                                         max_positions, max_orders_per_minute, max_exposure_pct,
                                         kill_switch, updated_at)
                VALUES (?,?,?,?,?,?,?,0,?)
                """,
                (
                    account_id,
                    DEFAULT_RISK_CONFIG["max_daily_loss"],
                    DEFAULT_RISK_CONFIG["max_drawdown"],
                    DEFAULT_RISK_CONFIG["max_position_pct"],
                    DEFAULT_RISK_CONFIG["max_positions"],
                    DEFAULT_RISK_CONFIG["max_orders_per_minute"],
                    DEFAULT_RISK_CONFIG["max_exposure_pct"],
                    now(),
                ),
            )


def seed_strategy_templates() -> None:
    """Install the built-in strategy templates once."""
    from backend.core.database import app_cursor, now
    from backend.strategies.templates import TEMPLATES

    with app_cursor() as cursor:
        for template in TEMPLATES:
            cursor.execute("SELECT 1 FROM strategies WHERE name = ? AND is_template = 1", (template["name"],))
            if cursor.fetchone():
                continue
            import json

            cursor.execute(
                """
                INSERT INTO strategies (id, account_id, name, description, kind, definition, is_template, created_at, updated_at)
                VALUES (?,?,?,?,?,?,1,?,?)
                """,
                (uuid.uuid4().hex, "system", template["name"], template.get("description", ""),
                 template.get("kind", "rule"), json.dumps(template["definition"]), now(), now()),
            )
            log.debug("Installed strategy template %s", template["name"])


def seed_instrument_master() -> None:
    """Populate the instrument master from Angel One on first boot.

    Market data is Angel's, so the shared `instruments` table starts empty and
    nothing (search, quotes, candles) works until it is filled. Doing it here
    means a fresh deploy is usable without a manual sync; failures are logged
    and never fatal, because a network blip must not stop the API booting.
    """
    from backend.angel import instruments as angel_instruments
    try:
        result = angel_instruments.bootstrap_if_empty()
        log.info("Instrument master bootstrap: %s", result.get("status"))
    except Exception as error:  # noqa: BLE001 - a data fetch must not break boot
        log.warning("Could not bootstrap the Angel One instrument master: %s", error)


def warm_search_index() -> None:
    """Build the local instrument search index so the first keystroke is fast.

    The index mirrors the instrument master in memory. Building it here means
    the search dropdown is instant from the first character instead of paying
    the build cost on the operator's first search.
    """
    from backend.marketdata import search_index
    try:
        count = search_index.refresh()
        log.info("Instrument search index warmed: %d instruments", count)
    except Exception as error:  # noqa: BLE001 - a warm-up must never break boot
        log.warning("Could not build the instrument search index: %s", error)


def startup() -> None:
    """Full boot sequence. Safe to call more than once."""
    configure_logging()

    from backend.core.config import settings
    from backend.core.database import initialise

    # Validate secrets FIRST, before touching the disk: on Render a missing
    # SECRET_KEY / CREDENTIAL_ENCRYPTION_KEY / JWT_SECRET is the #1 boot
    # failure, and it must say so instead of dying later in seed_users with
    # an unrelated sqlite traceback.
    missing = [
        name
        for name, value in (
            ("SECRET_KEY", settings.secret_key),
            ("CREDENTIAL_ENCRYPTION_KEY", settings.credential_key),
            ("JWT_SECRET", settings.jwt_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required configuration: " + ", ".join(missing) + ". "
            "Set them in the Render dashboard under Environment, then redeploy. "
            "Generate values with: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )

    try:
        initialise()
    except Exception as error:
        raise RuntimeError(
            f"Could not initialise the database in DATA_DIR={settings.data_dir!r}: {error}. "
            "On Render this means the disk is not mounted there or DATA_DIR is wrong "
            "(must match the disk mount path, e.g. /app/var)."
        ) from error
    seed_users()
    seed_risk_defaults()
    seed_strategy_templates()
    seed_instrument_master()
    warm_search_index()

    os.makedirs(settings.data_dir, exist_ok=True)
    log.info("AlphaTradePro backend ready (env=%s)", settings.environment)
