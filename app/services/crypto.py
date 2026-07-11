"""
Orion's Belt — Core Encryption Service

Fernet symmetric encryption using .secret_key as the key source.
Key derivation: read 64-char hex .secret_key -> 32 raw bytes -> base64url encode
for Fernet-compatible key.
"""
import base64
import logging
import os
from cryptography.fernet import Fernet
from pathlib import Path

log = logging.getLogger("orions_belt.crypto")

# ── Key derivation ───────────────────────────────────────────────────────────

_SECRET_KEY_PATH = Path(__file__).parent.parent.parent / ".secret_key"
_fernet: Fernet | None = None


def _load_key() -> Fernet:
    """Load and cache the Fernet key from .secret_key or SECRET_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet

    try:
        if _SECRET_KEY_PATH.exists():
            raw = _SECRET_KEY_PATH.read_text().strip()
            log.info("Encryption key loaded from %s", _SECRET_KEY_PATH)
        else:
            env_key = os.environ.get("SECRET_KEY", "").strip()
            if not env_key:
                raise FileNotFoundError(
                    f"{_SECRET_KEY_PATH} not found and SECRET_KEY env var is not set"
                )
            # Derive a stable 32-byte key from the env var string
            raw = env_key.encode("utf-8").hex()[:64].ljust(64, "0")
            log.info("Encryption key derived from SECRET_KEY env var")
        # 64 hex chars -> 32 raw bytes -> base64url -> Fernet key
        raw_bytes = bytes.fromhex(raw)
        key = base64.urlsafe_b64encode(raw_bytes)
        _fernet = Fernet(key)
        return _fernet
    except Exception as e:
        log.error("Failed to load encryption key: %s", e)
        raise


# ── Public API ────────────────────────────────────────────────────────────────

def encrypt_data(plaintext: str | None) -> str | None:
    """Encrypt a string value with Fernet.

    Returns None for None input.
    """
    if plaintext is None:
        return None
    fernet = _load_key()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_data(ciphertext: str | None) -> str | None:
    """Decrypt a Fernet-encrypted string.

    Returns None for None input.
    """
    if ciphertext is None:
        return None
    fernet = _load_key()
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception as e:
        log.error("Decryption failed: %s", e)
        return None
