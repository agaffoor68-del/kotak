"""Encrypted credential storage for Kotak Neo accounts.

Consumer keys, TOTP seeds and MPINs are secrets: they are encrypted at rest with
AES (Fernet) and never written back in plaintext, logged, or returned by an API.
The master key comes from `CREDENTIAL_ENCRYPTION_KEY`; when that is absent the
vault refuses to persist rather than silently falling back to plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from .config import settings

log = logging.getLogger("alphatrade.vault")


class VaultLocked(RuntimeError):
    """Raised when secrets would be written without an encryption key."""


def _key_from_secret(secret: str) -> bytes:
    """Derive a valid Fernet key from an arbitrary passphrase.

    Lets an operator configure any memorable string; the key is never stored.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet(key: bytes | None = None):
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - dependency is declared in requirements
        raise RuntimeError("The `cryptography` package is required for credential storage") from None
    return Fernet(key or _key_from_secret(settings.credential_key))


@dataclass
class BrokerCredentials:
    """Secrets for one Kotak Neo account. Never serialised directly."""

    consumer_key: str
    totp_key: str
    mpin: str
    mobile_number: str
    ucc: str
    neo_fin_key: str | None = None
    environment: str = "prod"

    def __post_init__(self) -> None:
        missing = [
            name
            for name in ("consumer_key", "totp_key", "mpin", "mobile_number", "ucc")
            if not str(getattr(self, name) or "").strip()
        ]
        if missing:
            raise ValueError("Missing Kotak credentials: " + ", ".join(missing))

    # -- at-rest protection ---------------------------------------------
    def to_ciphertext(self) -> str:
        from cryptography.fernet import Fernet  # noqa: F401 - ensures the dependency is present

        import json

        payload = json.dumps(
            {
                "consumer_key": self.consumer_key,
                "totp_key": self.totp_key,
                "mpin": self.mpin,
                "mobile_number": self.mobile_number,
                "ucc": self.ucc,
                "neo_fin_key": self.neo_fin_key,
                "environment": self.environment,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return _fernet().encrypt(payload).decode("ascii")

    @classmethod
    def from_ciphertext(cls, token: str) -> "BrokerCredentials":
        import json

        raw = _fernet().decrypt(token.encode("ascii"))
        data = json.loads(raw)
        return cls(**{key: data.get(key) for key in (
            "consumer_key", "totp_key", "mpin", "mobile_number", "ucc", "neo_fin_key", "environment"
        )})

    @classmethod
    def from_environment(cls) -> "BrokerCredentials | None":
        """Fallback for a single-account deployment using plain env vars."""
        if not settings.legacy_env_fallback:
            return None
        values = {
            "consumer_key": os.getenv("KOTAK_CONSUMER_KEY", ""),
            "totp_key": os.getenv("KOTAK_TOTP_KEY", ""),
            "mpin": os.getenv("KOTAK_MPIN", ""),
            "mobile_number": os.getenv("KOTAK_MOBILE_NUMBER", ""),
            "ucc": os.getenv("KOTAK_UCC", ""),
        }
        if not all(values.values()):
            return None
        return cls(**values, environment=settings.neo_environment)

    def masked(self) -> dict[str, str]:
        """A redacted view that is safe to return from an API."""
        tail = self.consumer_key[-4:] if self.consumer_key else ""
        return {
            "consumer_key": f"••••{tail}" if tail else "",
            "totp_key": "configured",
            "mpin": "configured",
            "mobile_number": self.mobile_number,
            "ucc": self.ucc,
            "environment": self.environment,
        }


class Vault:
    """Encrypt/decrypt helper bound to the configured master key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = bool(settings.credential_key)
        if not self._enabled:
            log.warning(
                "CREDENTIAL_ENCRYPTION_KEY is not set — encrypted credential storage is unavailable. "
                "Configure it before adding broker accounts in production."
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def encrypt(self, credentials: BrokerCredentials) -> str:
        if not self._enabled:
            raise VaultLocked("Set CREDENTIAL_ENCRYPTION_KEY before storing broker credentials")
        with self._lock:
            return credentials.to_ciphertext()

    def decrypt(self, token: str) -> BrokerCredentials:
        with self._lock:
            return BrokerCredentials.from_ciphertext(token)

    def rotate(self, token: str) -> str:
        """Re-encrypt under the current master key; used after a key rotation."""
        return self.encrypt(self.decrypt(token))


vault = Vault()
