"""
Orion's Belt — Database Backup Service

Uses SQLite's built-in backup API for hot, consistent backups without
locking the database. Provides:
- On-shutdown backup to .bak file
- Periodic scheduled backups (every 30 minutes)
- Manual backup via API
- Automated recovery: on startup, if backup is newer, use it
- Backup verification: validates .bak is a valid SQLite database

Backup strategy:
- Periodic backup every 30 minutes
- On shutdown: final backup
- On startup: restore from .bak if it's newer than the DB and the DB is corrupted
- Archive: old backups rotated into a back/ directory
"""
import atexit
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("orions_belt.backup")

# ── Backup paths ──────────────────────────────────────────────────────────────

def get_db_path() -> Path:
    """Return the path to the active database file."""
    from config import Config
    return Path(Config.DATABASE_PATH)


def get_backup_path() -> Path:
    """Return the path to the .bak backup file."""
    return get_db_path().parent / (get_db_path().stem + ".bak")


def get_archive_dir() -> Path:
    """Return the directory for old backup archives."""
    backup_dir = get_db_path().parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    return backup_dir


# ── Hot backup using SQLite backup API ────────────────────────────────────────

def backup_database(dest: Optional[Path] = None, *, keep_history: int = 5) -> bool:
    """Perform a hot backup using sqlite3's built-in backup API.

    This creates a consistent snapshot without locking the database.

    Args:
        dest: Where to save the backup. Defaults to get_backup_path().
        keep_history: Maximum number of archived backups to keep.

    Returns:
        True if backup succeeded and verified, False on error.
    """
    src = get_db_path()
    if dest is None:
        dest = get_backup_path()

    if not src.exists():
        log.warning("No database file to back up at %s", src)
        return False

    try:
        # Use SQLite's backup API for a consistent hot backup
        src_conn = sqlite3.connect(str(src))
        dest_conn = sqlite3.connect(str(dest))
        src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()

        # Verify the backup is a valid SQLite database
        if not _verify_backup(dest):
            log.error("Backup verification FAILED for %s", dest)
            dest.unlink(missing_ok=True)
            return False

        log.info("Database backup written to %s (%d bytes) — verified OK",
                 dest, dest.stat().st_size)

        # Rotate old backups if keep_history is set
        if keep_history > 0:
            _rotate_backups(keep_history)

        return True

    except Exception as e:
        log.error("Backup failed: %s", e, exc_info=True)
        # Clean up partial backup
        if dest.exists():
            dest.unlink()
        return False


def _verify_backup(path: Path) -> bool:
    """Verify a file is a valid SQLite database.

    Opens the file and runs PRAGMA integrity_check.
    Returns True if the database is valid.
    """
    if not path.exists():
        return False

    try:
        conn = sqlite3.connect(str(path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        valid = result[0] == "ok"
        if not valid:
            log.error("Backup integrity check failed: %s", result[0])
        return valid
    except Exception as e:
        log.error("Cannot verify backup %s: %s", path, e)
        return False


def _rotate_backups(max_keep: int = 5) -> None:
    """Move old .bak files into the archive directory and prune."""
    archive_dir = get_archive_dir()
    bak = get_backup_path()

    if not bak.exists():
        return

    # Move current backup to archive with timestamp
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = archive_dir / f"orions_belt_{timestamp}.bak"

    try:
        # Remove oldest archives if we'd exceed max_keep
        existing = sorted(archive_dir.glob("orions_belt_*.bak"))
        while len(existing) >= max_keep:
            oldest = existing.pop(0)
            oldest.unlink()
            log.info("Pruned old backup: %s", oldest)

        bak.rename(archive_path)
    except Exception as e:
        log.warning("Could not archive backup: %s", e)


# ── Automated recovery ───────────────────────────────────────────────────────

def recover_if_needed() -> bool:
    """Attempt automated recovery from backup.

    Checks if the current database is corrupted or missing,
    and if a valid backup exists that is newer.

    Returns:
        True if recovery was performed or not needed,
        False if recovery failed.
    """
    db_path = get_db_path()
    bak_path = get_backup_path()

    # Check if DB exists and is valid
    db_valid = False
    if db_path.exists():
        if _verify_backup(db_path):
            db_valid = True
        else:
            log.warning("Database integrity check FAILED — attempting recovery")
    else:
        log.warning("Database file not found at %s — attempting recovery", db_path)

    if db_valid:
        log.info("Database integrity OK — no recovery needed")
        return True

    # DB is bad or missing — try to restore from backup
    if not bak_path.exists():
        log.error("No backup available for recovery — data may be lost!")
        return False

    if not _verify_backup(bak_path):
        log.error("Backup file is not valid — cannot recover")
        return False

    try:
        shutil.copy2(str(bak_path), str(db_path))
        log.info("Database recovered from %s", bak_path)

        # Verify the recovered database
        if _verify_backup(db_path):
            log.info("Recovered database integrity check PASSED")
            return True
        else:
            log.error("Recovered database integrity check FAILED")
            return False

    except Exception as e:
        log.error("Recovery failed: %s", e, exc_info=True)
        return False


# ── Restore (manual) ─────────────────────────────────────────────────────────

def restore_database() -> bool:
    """Restore from the latest .bak backup.

    Copies the .bak file back over the active database.
    Database must be stopped before calling this.

    Returns:
        True if restored, False if no backup exists.
    """
    src = get_backup_path()
    dst = get_db_path()

    if not src.exists():
        log.warning("No backup file found at %s", src)
        return False

    try:
        shutil.copy2(str(src), str(dst))
        log.info("Database restored from %s", src)
        return True
    except Exception as e:
        log.error("Restore failed: %s", e, exc_info=True)
        return False


def has_valid_backup() -> bool:
    """Check if a valid backup exists."""
    bak = get_backup_path()
    return bak.exists() and bak.stat().st_size > 0 and _verify_backup(bak)


# ── Periodic scheduled backups ────────────────────────────────────────────────

_backup_thread: Optional[threading.Thread] = None
_shutdown_event = threading.Event()


def start_periodic_backups(interval_minutes: int = 30) -> None:
    """Start a background thread that performs backups at a fixed interval.

    Args:
        interval_minutes: How often to run a backup (default 30).
    """
    global _backup_thread

    def _backup_loop():
        while not _shutdown_event.is_set():
            try:
                success = backup_database(keep_history=3)
                if not success:
                    log.warning("Periodic backup failed")
            except Exception as e:
                log.error("Periodic backup thread error: %s", e)

            # Sleep in small increments so shutdown is responsive
            _shutdown_event.wait(interval_minutes * 60)

        log.info("Periodic backup thread stopped")

    _backup_thread = threading.Thread(target=_backup_loop, daemon=True,
                                      name="backup-scheduler")
    _backup_thread.start()
    log.info("Periodic backup scheduler started (interval=%dmin)", interval_minutes)


def stop_periodic_backups() -> None:
    """Signal the periodic backup thread to stop."""
    global _backup_thread
    if _backup_thread and _backup_thread.is_alive():
        _shutdown_event.set()
        _backup_thread.join(timeout=10)
        log.info("Periodic backup thread stopped")


# ── On-shutdown integration ──────────────────────────────────────────────────

def register_shutdown_backup():
    """Register backup handlers for Flask shutdown."""
    atexit.register(_shutdown_backup)
    # Also register periodic backup thread stop
    atexit.register(stop_periodic_backups)


def _shutdown_backup():
    """Perform final backup just before the process exits."""
    log.info("Running on-shutdown database backup...")
    backup_database(keep_history=3)
