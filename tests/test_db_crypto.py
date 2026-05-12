"""Tests for database encryption helpers."""
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestEnforceFilePermissions:
    def test_sets_0600_permissions(self, tmp_path):
        """enforce_file_permissions should set 0600 on the DB file."""
        from app.services.db_crypto import set_db_path, enforce_file_permissions

        db_file = tmp_path / "orions_belt.db"
        db_file.write_text("test db")
        # Set a permissive initial mode
        db_file.chmod(0o644)

        set_db_path(db_file)
        enforce_file_permissions()

        mode = db_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_noop_if_file_missing(self, tmp_path):
        """Should not raise if DB file doesn't exist."""
        from app.services.db_crypto import set_db_path, enforce_file_permissions

        missing = tmp_path / "missing.db"
        set_db_path(missing)
        # Should not raise
        enforce_file_permissions()

    def test_already_0600_no_change(self, tmp_path):
        """Should not raise if file already has 0600."""
        from app.services.db_crypto import set_db_path, enforce_file_permissions

        db_file = tmp_path / "orions_belt.db"
        db_file.write_text("test db")
        db_file.chmod(0o600)

        set_db_path(db_file)
        enforce_file_permissions()

        mode = db_file.stat().st_mode & 0o777
        assert mode == 0o600


class TestConnectorAuthEncryption:
    def test_encrypts_plain_auth(self, tmp_path):
        """encrypt_connector_auth should encrypt JSON auth config."""
        from app.services.crypto import encrypt_data
        from app.services.db_crypto import encrypt_connector_auth

        connector = MagicMock()
        connector.name = "test_connector"
        connector.auth_config = '{"token": "abc123", "type": "bearer"}'

        encrypt_connector_auth(connector)

        # auth_config should now be encrypted (Fernet ciphertext)
        assert connector.auth_config != '{"token": "abc123", "type": "bearer"}'
        assert len(connector.auth_config) > 20  # Fernet ciphertext is longer

    def test_doesnt_double_encrypt_json(self, tmp_path):
        """Should detect JSON and re-encrypt it, not fail."""
        from app.services.db_crypto import encrypt_connector_auth

        connector = MagicMock()
        connector.name = "test"
        # First encryption
        from app.services.crypto import encrypt_data
        connector.auth_config = encrypt_data('{"key": "value"}')

        # Call again — Fernet ciphertext is not valid JSON
        # Should detect it's not JSON and skip
        original = connector.auth_config
        encrypt_connector_auth(connector)
        # Should remain unchanged (already encrypted)
        assert connector.auth_config == original

    def test_skips_empty_auth(self, tmp_path):
        """Should do nothing if auth_config is empty."""
        from app.services.db_crypto import encrypt_connector_auth

        connector = MagicMock()
        connector.name = "test"
        connector.auth_config = ""

        encrypt_connector_auth(connector)
        assert connector.auth_config == ""
