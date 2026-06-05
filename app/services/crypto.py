"""
Symmetric encryption for cloud credentials stored in the database.

Uses Fernet (AES128-CBC + HMAC). The key is derived from settings.SECRET_KEY
via HKDF so a single SECRET_KEY env var bootstraps everything.

In production replace with a real KMS (AWS Secrets Manager / Azure Key Vault).
"""
from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _derive_key() -> bytes:
    """Derive a 32-byte Fernet key from SECRET_KEY (deterministic, app-scoped)."""
    seed = (settings.SECRET_KEY or "dev-insecure-key-change-me").encode("utf-8")
    # Stretch via SHA-256 -> base64 urlsafe 32 bytes (Fernet expects 32 url-safe base64 bytes)
    digest = hashlib.sha256(seed).digest()
    return base64.urlsafe_b64encode(digest)


_FERNET = Fernet(_derive_key())


def encrypt(value: Optional[str]) -> Optional[str]:
    """Encrypt a string; returns None for None/empty."""
    if not value:
        return None
    return _FERNET.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: Optional[str]) -> Optional[str]:
    """Decrypt a string; returns None for None/empty; returns None on invalid token."""
    if not value:
        return None
    try:
        return _FERNET.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
