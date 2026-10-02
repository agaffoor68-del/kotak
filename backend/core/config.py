"""AlphaTradePro runtime configuration.

Every setting is environment-driven so the same image runs on a laptop and in
production. Nothing here contains a secret; credentials are read from the
encrypted store in :mod:`backend.core.security`.
"""

from __future__ import annotations

import os
from functools import lru_cache


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    """Resolved configuration for one process."""

    def __init__(self) -> None:
        self.app_name = os.getenv("APP_NAME", "AlphaTradePro")
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.debug = _flag("DEBUG", self.environment == "development")

        # --- storage -----------------------------------------------------
        self.postgres_dsn = os.getenv("POSTGRES_DSN", "postgresql://trader:trader@localhost:5432/alphatrade")
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.data_dir = os.getenv("DATA_DIR", os.path.join(os.getcwd(), "var"))
        self.state_file = os.path.join(self.data_dir, "master.sqlite3")
        self.tick_db_file = os.path.join(self.data_dir, "ticks.sqlite3")

        # --- security ----------------------------------------------------
        # Fernet key protecting credentials at rest. Generate with:
        #   python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
        self.secret_key = os.getenv("SECRET_KEY", "")
        self.credential_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
        self.jwt_secret = os.getenv("JWT_SECRET", "")
        self.jwt_algorithm = os.getenv("JWT_ALGORITHM", "HS256")
        self.jwt_ttl_minutes = _int("JWT_TTL_MINUTES", 720)
        self.bootstrap_admin_email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@alphatrade.local")
        self.bootstrap_admin_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
        self.bootstrap_viewer_email = os.getenv("BOOTSTRAP_VIEWER_EMAIL", "")
        self.bootstrap_viewer_password = os.getenv("BOOTSTRAP_VIEWER_PASSWORD", "")

        # --- broker ------------------------------------------------------
        # Falls back to the legacy single-account env vars when set.
        self.legacy_env_fallback = _flag("KOTAK_LEGACY_ENV_FALLBACK", True)
        self.neo_environment = os.getenv("KOTAK_ENVIRONMENT", "prod")
        self.session_ttl_seconds = _int("KOTAK_SESSION_TTL_SECONDS", 45 * 60)
        self.keepalive_seconds = _int("KOTAK_KEEPALIVE_SECONDS", 300)
        self.keepalive_enabled = _flag("KOTAK_KEEPALIVE", True)
        self.login_attempts = _int("KOTAK_LOGIN_ATTEMPTS", 3)

        # --- market data -------------------------------------------------
        self.broker_quote_workers = _int("BROKER_QUOTE_WORKERS", 4)
        self.max_tokens_per_quote_call = _int("MAX_TOKENS_PER_QUOTE_CALL", 100)
        self.feed_poll_seconds = _float("FEED_POLL_SECONDS", 1.0)
        self.candle_intervals = [
            value.strip() for value in os.getenv("CANDLE_INTERVALS", "1m,3m,5m,15m,30m,60m,1d").split(",")
            if value.strip()
        ]
        self.record_ticks = _flag("RECORD_TICKS", True)
        self.tick_retention_days = _int("TICK_RETENTION_DAYS", 30)
        self.breadth_universe_size = _int("BREADTH_UNIVERSE_SIZE", 200)

        # --- websocket fan-out -------------------------------------------
        self.broadcast_queue_size = _int("BROADCAST_QUEUE_SIZE", 512)

        # --- alerts ------------------------------------------------------
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.whatsapp_api_key = os.getenv("WHATSAPP_API_KEY", "")
        self.whatsapp_sender = os.getenv("WHATSAPP_SENDER", "alphatrade")
        self.whatsapp_recipient = os.getenv("WHATSAPP_RECIPIENT", "")
        self.whatsapp_api_url = os.getenv("WHATSAPP_API_URL", "")
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = _int("SMTP_PORT", 587)
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_from = os.getenv("SMTP_FROM", "alerts@alphatrade.local")
        self.smtp_use_tls = _flag("SMTP_USE_TLS", True)

        # --- AI coach ----------------------------------------------------
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # --- CORS / public URL ------------------------------------------
        self.allowed_origins = [
            origin.strip()
            for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
            if origin.strip()
        ]
        self.public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
        self.frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")

        # Outside production the terminal is routinely opened from a phone or a
        # LAN address rather than localhost, and that address moves with DHCP.
        # Pinning one origin works until the lease renews, and then every request
        # fails as "Failed to fetch" with nothing in the browser console to
        # explain it. Development therefore accepts any http(s) origin, which is
        # safe here because auth is a bearer token in sessionStorage (a foreign
        # site cannot read it) and no cookie is ever issued. Production stays on
        # the strict ALLOWED_ORIGINS list above.
        self.allowed_origin_regex = (
            None
            if self.is_production
            else os.getenv("ALLOWED_ORIGIN_REGEX", r"https?://.*").strip() or None
        )

        # --- feature gates -----------------------------------------------
        self.enable_live_trading = _flag("ENABLE_LIVE_TRADING", False)
        self.enable_paper_trading = _flag("ENABLE_PAPER_TRADING", True)
        self.enable_backtest = _flag("ENABLE_BACKTEST", True)
        self.enable_algo_execution = _flag("ENABLE_ALGO_EXECUTION", False)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def ensure_directories(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


settings = get_settings()
