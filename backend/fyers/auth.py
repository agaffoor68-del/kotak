"""Fyers OAuth2 authentication."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("alphatrade.fyers.auth")

try:
    from fyers_apiv3 import fyersModel  # type: ignore
    _HAS_FYERS = True
except Exception:
    fyersModel = None  # type: ignore
    _HAS_FYERS = False
    logger.warning("fyers-apiv3 not installed")


class FyersAuth:
    def __init__(self) -> None:
        self.client_id = os.getenv("FYERS_APP_ID", "")
        self.secret_key = os.getenv("FYERS_SECRET_KEY", "")
        self.redirect_uri = os.getenv("FYERS_REDIRECT_URI", "")
        self.access_token = os.getenv("FYERS_ACCESS_TOKEN", "")
        self.refresh_token = os.getenv("FYERS_REFRESH_TOKEN", "")

    def get_login_url(self) -> str:
        if not _HAS_FYERS or fyersModel is None:
            return ""
        try:
            session = fyersModel.SessionModel(client_id=self.client_id, secret_key=self.secret_key, redirect_uri=self.redirect_uri, response_type="code", grant_type="authorization_code")
            return session.generate_authcode()
        except Exception:
            logger.exception("Failed to generate Fyers auth URL")
            return ""

    def generate_token(self, auth_code: str) -> dict[str, Any]:
        if not _HAS_FYERS or fyersModel is None:
            return {"error": "fyers-apiv3 not installed"}
        try:
            session = fyersModel.SessionModel(client_id=self.client_id, secret_key=self.secret_key, redirect_uri=self.redirect_uri, response_type="code", grant_type="authorization_code")
            session.set_token(auth_code)
            response = session.generate_token()
            return response
        except Exception as e:
            logger.exception("Failed to generate Fyers token")
            return {"error": str(e)}

    def refresh_access_token(self) -> dict[str, Any]:
        if not _HAS_FYERS or fyersModel is None:
            return {"error": "fyers-apiv3 not installed"}
        try:
            session = fyersModel.SessionModel(client_id=self.client_id, secret_key=self.secret_key, redirect_uri=self.redirect_uri, response_type="code", grant_type="refresh_token")
            session.set_refresh_token(self.refresh_token)
            response = session.generate_token()
            return response
        except Exception as e:
            logger.exception("Failed to refresh Fyers token")
            return {"error": str(e)}


fyers_auth = FyersAuth()
