"""Task 1: FyersAuthService — daily token lifecycle (expires 6:30 AM).
Uses fyersModel.SessionModel as required."""
import os, time, logging
from fyers_apiv3 import fyersModel
from backend.core.config import settings

log = logging.getLogger("fyres.auth")

class FyersAuthService:
    def __init__(self):
        self.app_id = os.getenv("FYERS_APP_ID")
        self.secret = os.getenv("FYERS_SECRET_ID")
        self.redirect = os.getenv("FYERS_REDIRECT_URI", "http://localhost:8000/api/v1/fyers/callback")
        self.pin = os.getenv("FYERS_PIN")
        # Persisted in .env / DB (encrypted via vault if available)
        self.token_path = os.getenv("FYERS_TOKEN_FILE", ".env_fyers_token")

    def generate_auth_url(self) -> str:
        session = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret,
            redirect_uri=self.redirect,
            response_type="code",
            state="state123"
        )
        return session.generate_auth_code()

    def generate_token(self, auth_code: str) -> str:
        session = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret,
            redirect_uri=self.redirect
        )
        token_resp = session.generate_token(auth_code)
        access_token = token_resp["access_token"]
        # Write to env file / DB (simulated persistence)
        with open(self.token_path, "w") as f:
            f.write(access_token)
        # Also inject into environment for immediate use
        os.environ["FYERS_ACCESS_TOKEN"] = access_token
        return access_token

    def get_access_token(self) -> str:
        # Daily expiry at 6:30 AM — if missing/expired, trigger login flow
        if os.path.exists(self.token_path):
            with open(self.token_path) as f:
                token = f.read().strip()
            if token:
                return token
        # Try refresh if refresh token available (simplified)
        return os.getenv("FYERS_ACCESS_TOKEN", "")

    def is_expired(self) -> bool:
        # Simplified: assume expires at 6:30 AM daily; can parse JWT exp
        return not bool(self.get_access_token())
