"""
Orion's Belt — System administration routes

Backup, restore, health, and other system-level operations.
"""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify

from app.services.backup import (
    backup_database,
    get_backup_path,
    get_db_path,
    has_valid_backup,
    recover_if_needed,
    restore_database,
)

bp = Blueprint("system", __name__)


def _now():
    return datetime.now(timezone.utc)


@bp.route("/api/system/backup", methods=["POST"])
def trigger_backup():
    """Trigger a manual database backup.

    Returns:
        {"ok": true, "backup_path": "...", "size_bytes": 12345}
    """
    success = backup_database()
    if not success:
        return jsonify({"ok": False, "error": "Backup failed"}), 500

    backup_path = get_backup_path()
    return jsonify({
        "ok": True,
        "backup_path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
    })


@bp.route("/api/system/backup/status", methods=["GET"])
def backup_status():
    """Check if a valid backup exists.

    Returns:
        {
            "has_backup": true/false,
            "backup_path": "...",
            "backup_age_seconds": 123,
            "db_size_bytes": 12345,
            "backup_size_bytes": 12345,
        }
    """
    db_path = get_db_path()
    bak = get_backup_path()

    result = {
        "has_backup": has_valid_backup(),
        "backup_path": str(bak),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }

    if bak.exists():
        age = time.time() - bak.stat().st_mtime
        result["backup_age_seconds"] = int(age)
        result["backup_size_bytes"] = bak.stat().st_size
    else:
        result["backup_age_seconds"] = None
        result["backup_size_bytes"] = 0

    return jsonify(result)


@bp.route("/api/system/backup/restore", methods=["POST"])
def trigger_restore():
    """Restore the database from the latest backup.

    The database must be stopped before this is called in production.
    In testing, this copies the backup over the live DB.

    Returns:
        {"ok": true, "message": "Restored from ..."}
        {"ok": false, "error": "No backup available"}
    """
    success = restore_database()
    if not success:
        return jsonify({"ok": False, "error": "No backup available for restore"}), 404

    return jsonify({"ok": True, "message": "Database restored from backup"})


@bp.route("/api/system/backup/recover", methods=["POST"])
def trigger_recover():
    """Attempt automated recovery from backup.

    Checks DB integrity and restores from .bak if corrupted.

    Returns:
        {"ok": true, "recovery_needed": false}
        {"ok": true, "recovery_needed": true, "recovered": true}
        {"ok": false, "error": "Recovery failed"}
    """
    success = recover_if_needed()
    if success:
        return jsonify({"ok": True, "recovery_needed": False})

    # If recover_if_needed returns False, the DB is bad and recovery failed
    # Try again explicitly to get a better result
    db_path = get_db_path()
    has_corruption = not db_path.exists() or not _verify_db(db_path)

    if has_corruption:
        success = recover_if_needed()
        if success:
            return jsonify({"ok": True, "recovery_needed": True, "recovered": True})

    return jsonify({"ok": False, "error": "Database corruption detected and recovery failed"}), 500


def _verify_db(path: Path) -> bool:
    """Quick SQLite integrity check."""
    try:
        conn = sqlite3.connect(str(path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result[0] == "ok"
    except Exception:
        return False


@bp.route("/api/system/health", methods=["GET"])
def health():
    """Health check with backup status."""
    from config import Config

    db_path = get_db_path()
    db_size = db_path.stat().st_size if db_path.exists() else 0

    return jsonify({
        "status": "ok",
        "version": Config.APP_VERSION,
        "db_path": str(db_path),
        "db_size_bytes": db_size,
        "has_valid_backup": has_valid_backup(),
    })
