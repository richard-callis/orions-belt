"""
Orion's Belt — Database Encryption Helpers

File permission enforcement and connector auth encryption via Fernet.
"""
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("orions_belt.db_crypto")

_db_path: Path | None = None


def set_db_path(path: Path) -> None:
    """Set the path to the active database for permission enforcement."""
    global _db_path
    _db_path = path


def enforce_file_permissions() -> None:
    """Set 0600 permissions on the database file.

    Ensures only the owning user can read/write the database.
    Safe to call multiple times.
    """
    if _db_path is None:
        log.warning("No DB path set — skipping file permission enforcement")
        return

    try:
        if _db_path.exists():
            current_mode = _db_path.stat().st_mode & 0o777
            if current_mode != 0o600:
                os.chmod(_db_path, 0o600)
                log.info("Database file permissions set to 0600: %s", _db_path)
            else:
                log.info("Database file already has 0600 permissions: %s", _db_path)
    except Exception as e:
        log.error("Failed to set DB file permissions: %s", e)


def encrypt_connector_auth(connector) -> None:
    """Encrypt a connector's auth_config using Fernet.

    Reads the raw auth_config from connector.auth_config (plain JSON string),
    encrypts it, and writes back to connector.auth_config.

    Safe to call multiple times — detects already-encrypted values.
    """
    from app.services.crypto import encrypt_data

    if not connector.auth_config:
        return

    try:
        import json
        data = json.loads(connector.auth_config)
        # If it's already encrypted, it won't parse as JSON with expected keys
        if not isinstance(data, dict):
            return
        # Check if it looks like already-encrypted (Fernet tokens are base64 strings)
        # We encrypt the entire JSON string, not individual fields
        encrypted = encrypt_data(connector.auth_config)
        if encrypted:
            connector.auth_config = encrypted
            log.info("Connector auth encrypted for: %s", connector.name)
    except (json.JSONDecodeError, TypeError):
        # Already encrypted or invalid — Fernet ciphertext is base64, not valid JSON
        pass
