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


@dataclass
class AngelCredentials:
    """Secrets for one Angel One SmartAPI account.

    Angel's TOTP flow needs the ``api_key`` (SmartAPI key), the ``client_code``
    (Angel user id), the account PIN and the TOTP seed — the same shape as
    Kotak's consumer key / UCC / MPIN / TOTP. Kept in its own class so the two
    brokers never share a credential record and a vault key rotation cannot mix
    them up.
    """

    api_key: str
    client_code: str
    pin: str
    totp_key: str
    environment: str = "prod"

    def __post_init__(self) -> None:
        # Values copied from Angel's portal or an authenticator often contain
        # surrounding whitespace; TOTP seeds may also be displayed in groups.
        # Normalise them before SmartAPI signs the login request so the generated
        # code is based on the actual base32 secret, not its presentation format.
        self.api_key = str(self.api_key or "").strip()
        self.client_code = str(self.client_code or "").strip().upper()
        self.pin = str(self.pin or "").strip()
        self.totp_key = "".join(str(self.totp_key or "").split()).replace("-", "").upper()
        self.environment = str(self.environment or "prod").strip().lower()

        missing = [
            name
            for name in ("api_key", "client_code", "pin", "totp_key")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError("Missing Angel One credentials: " + ", ".join(missing))
        if self.environment not in {"prod", "uat"}:
            raise ValueError("Angel environment must be 'prod' or 'uat'")

    def to_ciphertext(self) -> str:
        import json

        payload = json.dumps(
            {
                "broker": "angel",
                "api_key": self.api_key,
                "client_code": self.client_code,
                "pin": self.pin,
                "totp_key": self.totp_key,
                "environment": self.environment,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return _fernet().encrypt(payload).decode("ascii")

    @classmethod
    def from_ciphertext(cls, token: str) -> "AngelCredentials":
        import json

        data = json.loads(_fernet().decrypt(token.encode("ascii")))
        if data.get("broker") not in (None, "angel"):
            raise ValueError("This credential record does not belong to Angel One")
        return cls(**{key: data.get(key) for key in ("api_key", "client_code", "pin", "totp_key", "environment")})

    @classmethod
    def from_environment(cls) -> "AngelCredentials | None":
        """Fallback for a single-account deployment using plain env vars."""
        if not settings.legacy_env_fallback:
            return None
        values = {
            "api_key": os.getenv("ANGEL_API_KEY", ""),
            "client_code": os.getenv("ANGEL_CLIENT_CODE", ""),
            "pin": os.getenv("ANGEL_PIN", ""),
            "totp_key": os.getenv("ANGEL_TOTP_KEY", ""),
        }
        if not all(values.values()):
            return None
        return cls(**values, environment=settings.angel_environment)

    def masked(self) -> dict[str, str]:
        tail = self.api_key[-4:] if self.api_key else ""
        return {
            "api_key": f"••••{tail}" if tail else "",
            "client_code": self.client_code,
            "pin": "configured",
            "totp_key": "configured",
            "environment": self.environment,
        }


def load_credentials(row: dict[str, Any]) -> "BrokerCredentials | AngelCredentials":
    """Decode a stored credential record, whichever broker it belongs to.

    Records written before Angel existed carry no ``broker`` field and are
    read as Kotak, so an upgrade never invalidates a working account.
    """
    token = row["encrypted_secrets"]
    if str(row.get("broker") or "kotak").lower() == "angel":
        return AngelCredentials.from_ciphertext(token)
    return BrokerCredentials.from_ciphertext(token)


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

    def encrypt(self, credentials: "BrokerCredentials | AngelCredentials") -> str:
        if not self._enabled:
            raise VaultLocked("Set CREDENTIAL_ENCRYPTION_KEY before storing broker credentials")
        with self._lock:
            return credentials.to_ciphertext()

    def decrypt(self, token: str) -> "BrokerCredentials | AngelCredentials":
        with self._lock:
            return BrokerCredentials.from_ciphertext(token)

    def rotate(self, token: str) -> str:
        """Re-encrypt under the current master key; used after a key rotation."""
        return self.encrypt(self.decrypt(token))


vault = Vault()
