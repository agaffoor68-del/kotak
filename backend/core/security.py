"""Authentication primitives: password hashing, JWT issue/verify, API keys.

Deliberately dependency-light — PBKDF2 from the standard library for passwords
and HS256 JWTs built with `hmac`, so there is no risk of a misconfigured
third-party library silently accepting a weak token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import settings

PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------------- passwords


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return `pbkdf2_sha256$rounds$salt$hash`, all base64url without padding."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${encode(salt)}${encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_part, hash_part = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        padding = lambda value: value + "=" * (-len(value) % 4)
        salt = base64.urlsafe_b64decode(padding(salt_part))
        expected = base64.urlsafe_b64decode(padding(hash_part))
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(candidate, expected)


# -------------------------------------------------------------------------- JWT


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _jwt_secret() -> bytes:
    if settings.jwt_secret:
        return settings.jwt_secret.encode("utf-8")
    if settings.secret_key:
        return hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    raise RuntimeError("JWT_SECRET is not configured; refusing to mint tokens")


@dataclass
class TokenClaims:
    subject: str
    role: str
    account_id: str
    expires_at: int
    issued_at: int

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


def create_access_token(*, subject: str, role: str, account_id: str, ttl_minutes: int | None = None) -> tuple[str, TokenClaims]:
    """Mint a signed JWT. Returns the token and its claims."""
    issued_at = int(time.time())
    expires_at = issued_at + (ttl_minutes or settings.jwt_ttl_minutes) * 60
    header = {"alg": settings.jwt_algorithm, "typ": "JWT"}
    payload = {
        "sub": subject,
        "role": role,
        "aid": account_id,
        "iat": issued_at,
        "exp": expires_at,
        "jti": secrets.token_hex(8),
    }
    signing_input = ".".join(
        (
            _b64encode(json.dumps(header, separators=(",", ":")).encode()),
            _b64encode(json.dumps(payload, separators=(",", ":")).encode()),
        )
    )
    signature = hmac.new(_jwt_secret(), signing_input.encode(), hashlib.sha256).digest()
    claims = TokenClaims(subject=subject, role=role, account_id=account_id, expires_at=expires_at, issued_at=issued_at)
    return f"{signing_input}.{_b64encode(signature)}", claims


def decode_access_token(token: str) -> TokenClaims:
    """Verify signature and expiry, raising `ValueError` on any problem."""
    try:
        signing_input, signature_part = token.rsplit(".", 1)
        header_part, payload_part = signing_input.split(".", 1)
    except ValueError as error:
        raise ValueError("Malformed token") from error

    expected = hmac.new(_jwt_secret(), signing_input.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64decode(signature_part)):
        raise ValueError("Invalid token signature")

    payload: dict[str, Any] = json.loads(_b64decode(payload_part))
    claims = TokenClaims(
        subject=str(payload.get("sub", "")),
        role=str(payload.get("role", "")),
        account_id=str(payload.get("aid", "")),
        expires_at=int(payload.get("exp", 0)),
        issued_at=int(payload.get("iat", 0)),
    )
    if claims.is_expired:
        raise ValueError("Token has expired")
    return claims


# -------------------------------------------------------------------- API keys


def generate_api_key() -> tuple[str, str]:
    """Return `(plaintext_key, stored_hash)`. Only the plaintext is shown once."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(f"{settings.secret_key}:{raw}".encode()).hexdigest()


def verify_api_key(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw), stored_hash)


def random_session_id() -> str:
    return os.urandom(24).hex()
