"""Tests for database backup service."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import app.services.backup as backup_mod
from app.services.backup import (
    backup_database,
    get_backup_path,
    get_db_path,
    has_valid_backup,
    restore_database,
    recover_if_needed,
    _verify_backup,
)

# Save originals at import time
_original_get_db_path = backup_mod.get_db_path
_original_get_backup_path = backup_mod.get_backup_path


def _set_mock_paths(db_path: Path, bak_path: Path) -> None:
    """Patch get_db_path and get_backup_path on the backup module."""
    backup_mod.get_db_path = lambda: db_path
    backup_mod.get_backup_path = lambda: bak_path


def _restore_mock_paths() -> None:
    """Restore original get_db_path and get_backup_path."""
    backup_mod.get_db_path = _original_get_db_path
    backup_mod.get_backup_path = _original_get_backup_path


def _make_db(path: Path) -> None:
    """Create a minimal SQLite DB with a 't' table and ('hello') row."""
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()


# ── _verify_backup ────────────────────────────────────────────────────────────

class TestVerifyBackup:
    def test_valid_db(self, tmp_path):
        db_path = tmp_path / "valid.db"
        _make_db(db_path)
        assert _verify_backup(db_path) is True

    def test_corrupted_db(self, tmp_path):
        db_path = tmp_path / "bad.db"
        db_path.write_text("corrupted")
        assert _verify_backup(db_path) is False

    def test_missing_file(self, tmp_path):
        assert _verify_backup(tmp_path / "missing.db") is False

    def test_non_sqlite(self, tmp_path):
        db_path = tmp_path / "text.txt"
        db_path.write_text("not sqlite")
        assert _verify_backup(db_path) is False


# ── backup_database ───────────────────────────────────────────────────────────

class TestBackupDatabase:
    def setup_method(self):
        _restore_mock_paths()

    def teardown_method(self):
        _restore_mock_paths()

    def test_creates_bak(self, tmp_path):
        db_path = tmp_path / "test.db"
        bak_path = tmp_path / "test.bak"
        _make_db(db_path)
        _set_mock_paths(db_path, bak_path)
        result = backup_database(dest=bak_path, keep_history=0)
        assert result is True
        assert _verify_backup(bak_path) is True
        conn = sqlite3.connect(str(bak_path))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row == ("hello",)

    def test_no_source_db(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        _set_mock_paths(missing, tmp_path / "out.bak")
        assert backup_database(keep_history=0) is False

    def test_preserves_data(self, tmp_path):
        db_path = tmp_path / "test.db"
        bak_path = tmp_path / "test.bak"
        db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('world')")
        conn.commit()
        conn.close()
        _set_mock_paths(db_path, bak_path)
        backup_database(dest=bak_path, keep_history=0)
        conn = sqlite3.connect(str(bak_path))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row == ("world",)

    def test_overwrites_old_bak(self, tmp_path):
        db_path = tmp_path / "test.db"
        bak_path = tmp_path / "test.bak"
        db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('new')")
        conn.commit()
        conn.close()
        bak_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(bak_path))
        conn.execute("CREATE TABLE old (x TEXT)")
        conn.execute("INSERT INTO old VALUES ('old')")
        conn.commit()
        conn.close()
        _set_mock_paths(db_path, bak_path)
        backup_database(dest=bak_path, keep_history=0)
        conn = sqlite3.connect(str(bak_path))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row == ("new",)


# ── has_valid_backup ──────────────────────────────────────────────────────────

class TestHasValidBackup:
    def setup_method(self):
        _restore_mock_paths()

    def teardown_method(self):
        _restore_mock_paths()

    def test_valid_bak(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        bak_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(bak_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.commit()
        conn.close()
        _set_mock_paths(db_path, bak_path)
        assert has_valid_backup() is True

    def test_missing_bak(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        _set_mock_paths(db_path, bak_path)
        assert has_valid_backup() is False

    def test_corrupted_bak(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        bak_path.write_text("not sqlite")
        _set_mock_paths(db_path, bak_path)
        assert has_valid_backup() is False


# ── restore_database ──────────────────────────────────────────────────────────

class TestRestoreDatabase:
    def setup_method(self):
        _restore_mock_paths()

    def teardown_method(self):
        _restore_mock_paths()

    def test_restores_from_bak(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"

        db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('current')")
        conn.commit()
        conn.close()

        bak_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(bak_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('restored')")
        conn.commit()
        conn.close()

        _set_mock_paths(db_path, bak_path)
        result = restore_database()
        assert result is True
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row == ("restored",)

    def test_no_backup_available(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        _make_db(db_path)
        _set_mock_paths(db_path, tmp_path / "missing.bak")
        assert restore_database() is False


# ── recover_if_needed ─────────────────────────────────────────────────────────

class TestRecoverIfNeeded:
    def setup_method(self):
        _restore_mock_paths()

    def teardown_method(self):
        _restore_mock_paths()

    def test_no_recovery_needed(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        _make_db(db_path)
        _set_mock_paths(db_path, bak_path)
        assert recover_if_needed() is True

    def test_recovery_restores_from_bak(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        # No DB — it's "missing"
        bak_path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(bak_path))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('recovered')")
        conn.commit()
        conn.close()
        _set_mock_paths(db_path, bak_path)
        assert recover_if_needed() is True
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row == ("recovered",)

    def test_recovery_fails_no_backup(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        db_path.write_text("corrupted")
        _set_mock_paths(db_path, bak_path)
        assert recover_if_needed() is False

    def test_recovery_fails_corrupted_backup(self, tmp_path):
        db_path = tmp_path / "orions_belt.db"
        bak_path = tmp_path / "orions_belt.bak"
        db_path.write_text("corrupted db")
        bak_path.write_text("corrupted backup")
        _set_mock_paths(db_path, bak_path)
        assert recover_if_needed() is False
