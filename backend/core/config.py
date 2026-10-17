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

        # --- Angel One SmartAPI (market data + optional execution) --------
        # The data layer is Angel One, not Kotak: Kotak Neo publishes no
        # historical candles, so candles and the instrument master come from
        # SmartAPI. Credentials live in the encrypted store, these are the
        # non-secret knobs.
        self.angel_environment = os.getenv("ANGEL_ENVIRONMENT", "prod")
        # SmartAPI edge headers are deployment-specific; never bake an IP or
        # MAC address into the client. Leave the public IP empty to detect the
        # current egress address, or set it when the deployment has a reserved IP.
        self.angel_client_public_ip = os.getenv("ANGEL_CLIENT_PUBLIC_IP", "").strip()
        self.angel_client_local_ip = os.getenv("ANGEL_CLIENT_LOCAL_IP", "").strip()
        self.angel_mac_address = os.getenv("ANGEL_MAC_ADDRESS", "").strip()
        self.angel_user_agent = os.getenv(
            "ANGEL_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ).strip()
        self.angel_session_ttl_seconds = _int("ANGEL_SESSION_TTL_SECONDS", 8 * 60 * 60)
        self.angel_session_file = os.getenv("ANGEL_SESSION_FILE", os.path.join(self.data_dir, "angel_session.json"))
        self.angel_instrument_file = os.getenv(
            "ANGEL_INSTRUMENT_FILE", os.path.join(self.data_dir, "angel_instruments.json")
        )
        self.angel_sync_url = os.getenv(
            "ANGEL_SYNC_URL",
            "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
        )
        self.angel_rate_limit_per_second = _int("ANGEL_RATE_LIMIT_PER_SECOND", 3)
        #: SmartAPI refuses more than a handful of symbols per quote/LTP call.
        self.angel_max_tokens_per_call = _int("ANGEL_MAX_TOKENS_PER_CALL", 50)
        #: SmartAPI caps a single historical call; long ranges are chunked.
        self.angel_history_days_default = _int("ANGEL_HISTORY_DAYS_DEFAULT", 30)
        self.angel_history_days_max = _int("ANGEL_HISTORY_DAYS_MAX", 1800)

        # --- market data provider ----------------------------------------
        # Which broker serves market data (quotes, LTP, candles, live feed).
        # Read-only by design: no provider in this setting places orders.
        self.market_data_provider = os.getenv("MARKET_DATA_PROVIDER", "fyers").strip().lower()

        # --- FYERS API v3 (market data) -----------------------------------
        # Credentials come from the environment (or the encrypted store), never
        # from code. Create the app at https://myapi.fyers.in/dashboard/ and
        # register the Render callback as its redirect URI.
        self.fyers_app_id = os.getenv("FYERS_APP_ID", "").strip()
        self.fyers_secret_id = os.getenv("FYERS_SECRET_ID", "").strip()
        #: Falls back to PUBLIC_URL + FYERS_REDIRECT_PATH when empty.
        self.fyers_redirect_uri = os.getenv("FYERS_REDIRECT_URI", "").strip()
        self.fyers_redirect_path = os.getenv("FYERS_REDIRECT_PATH", "/fyers/callback").strip() or "/fyers/callback"
        #: Optional: enables the refresh-token path (15-day validity). FYERS
        #: documents this flow as possibly being discontinued, so a missing or
        #: rejected refresh simply asks the operator to visit /fyers/login.
        self.fyers_pin = os.getenv("FYERS_PIN", "").strip()
        #: Operator override for a token minted elsewhere; the encrypted file
        #: store wins once /fyers/callback has run.
        self.fyers_access_token_env = os.getenv("FYERS_ACCESS_TOKEN", "").strip()
        self.fyers_token_file = os.getenv(
            "FYERS_TOKEN_FILE", os.path.join(self.data_dir, "fyers_token.json")
        )
        #: Rate limit and batching for the FYERS data API (quotes cap at 50).
        self.fyers_rate_limit_per_second = _float("FYERS_RATE_LIMIT_PER_SECOND", 3.0)
        self.fyers_max_symbols_per_call = _int("FYERS_MAX_SYMBOLS_PER_CALL", 50)
        #: Every data call retries transient failures this many extra times
        #: with exponential backoff before surfacing the error.
        self.fyers_retries = _int("FYERS_RETRIES", 3)
        self.fyers_retry_backoff_seconds = _float("FYERS_RETRY_BACKOFF_SECONDS", 0.5)
        self.fyers_history_days_default = _int("FYERS_HISTORY_DAYS_DEFAULT", 30)
        self.fyers_history_days_max = _int("FYERS_HISTORY_DAYS_MAX", 1800)
        #: Live feed behaviour: REST priming/fallback cadence (seconds).
        self.fyers_poll_seconds_open = _float("FYERS_POLL_SECONDS_OPEN", 1.0)
        self.fyers_poll_seconds_closed = _float("FYERS_POLL_SECONDS_CLOSED", 60.0)
        #: Recycle the socket when the feed has been silent this long in-session.
        self.fyers_socket_stale_seconds = _int("FYERS_SOCKET_STALE_SECONDS", 180)
        #: Download + cache FYERS' public symbol master for validation.
        self.fyers_symbol_master_url = os.getenv(
            "FYERS_SYMBOL_MASTER_URL", "https://public.fyers.in/sym_details/{}.json"
        )
        self.fyers_symbol_master_ttl = _int("FYERS_SYMBOL_MASTER_TTL", 12 * 60 * 60)


        # --- execution routing -------------------------------------------
        # Which broker receives an order when the caller does not name one.
        # "kotak", "angel" or "both" (fan the same signal out to both).
        self.default_execution_broker = os.getenv("DEFAULT_EXECUTION_BROKER", "kotak").strip().lower()
        self.execution_brokers = [
            value.strip().lower()
            for value in os.getenv("EXECUTION_BROKERS", "kotak,angel").split(",")
            if value.strip()
        ]
        #: When two brokers hold a copy of the same position this is the size
        #: multiplier applied to the second leg, so a signal is not silently
        #: doubled in the book (1.0 = every listed broker gets the full size).
        self.dual_execution_size = _float("DUAL_EXECUTION_SIZE", 1.0)

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

        # --- deployment: static IP + redirect URL (SEBI compliance) -------
        # SEBI requires brokers to see a fixed, registered source IP on order
        # APIs, so the host must sit behind a stable public address (an AWS
        # Elastic IP, a DigitalOcean Reserved IP, a GCP static IP, ...). The
        # platform cannot create that address for you, but it can report the
        # one it is actually using so it can be registered on the broker
        # portal, and warn when the observed egress IP no longer matches what
        # was registered.
        #
        # The address registered with the broker. Empty means "not registered
        # yet"; the admin panel then shows the detected IP and asks for it to
        # be recorded here.
        self.registered_static_ip = os.getenv("STATIC_IP", "").strip()
        # Whether that address is actually static (Elastic/Reserved/static IP,
        # not a dynamic one that survives only until the next reboot).
        self.static_ip_is_reserved = _flag("STATIC_IP_RESERVED", False)
        #: Optional override for egress-IP detection, for hosts that cannot
        #: reach a public "what is my IP" service (air-gapped or locked-down).
        self.public_ip_override = os.getenv("PUBLIC_IP_OVERRIDE", "").strip()
        self.public_ip_check_url = os.getenv("PUBLIC_IP_CHECK_URL", "https://api.ipify.org").strip()
        self.public_ip_check_enabled = _flag("PUBLIC_IP_CHECK", True)
        self.public_ip_cache_seconds = _int("PUBLIC_IP_CACHE_SECONDS", 300)

        # OAuth / broker-API redirect. This is where a broker sends the user
        # back after they authorise the app, so it must be a URL the broker
        # has been told about and that resolves from the public internet.
        # Falls back to PUBLIC_URL + REDIRECT_PATH when not set explicitly.
        self.redirect_path = os.getenv("REDIRECT_PATH", "/callback").strip() or "/callback"
        self.redirect_url = os.getenv("REDIRECT_URL", "").strip().rstrip("/")
        #: Extra redirect targets a broker may have been given, comma-separated
        #: (e.g. a localhost one for development alongside the production URL).
        self.redirect_allowlist = [
            value.strip().rstrip("/")
            for value in os.getenv("REDIRECT_ALLOWLIST", "").split(",")
            if value.strip()
        ]
        self.require_https = _flag("REQUIRE_HTTPS", False)

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
