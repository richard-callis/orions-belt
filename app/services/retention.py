"""
Orion's Belt — Data Retention Service

Background thread that purges records older than Config.LOG_RETENTION_DAYS
from audit_logs, pii_logs, agent_logs, llm_logs, memories, pii_hash_map,
and archived sessions (with cascade delete of messages/compactions).

Runs every 6 hours.
"""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from config import Config

log = logging.getLogger("orions_belt.retention")

_shutdown_event = threading.Event()
_retention_thread = None


def _now():
    return datetime.now(timezone.utc)


def enforce_retention():
    """Purge records older than LOG_RETENTION_DAYS from all purgeable tables.

    Called in the app context so SQLAlchemy session works.
    """
    from app import db
    from app.models.logs import AuditLog, PIILog, AgentLog, LLMLog
    from app.models.memory import Memory
    from app.models.pii import PIIHashEntry
    from app.models.chat import Session

    cutoff = _now() - timedelta(days=Config.LOG_RETENTION_DAYS)
    total_purged = 0

    tables = [
        (AuditLog, "audit_logs", "created_at"),
        (PIILog, "pii_logs", "created_at"),
        (AgentLog, "agent_logs", "created_at"),
        (LLMLog, "llm_logs", "created_at"),
        (PIIHashEntry, "pii_hash_map", "created_at"),
    ]

    for model, name, date_col_name in tables:
        try:
            count = db.session.query(model).filter(
                model.created_at < cutoff
            ).delete(synchronize_session=False)
            total_purged += count
            db.session.commit()
            log.info("Retention: purged %d old %s records (older than %s)",
                     count, name, cutoff.isoformat())
        except Exception as e:
            db.session.rollback()
            log.error("Retention purge failed for %s: %s", name, e)

    # Memories: purge old non-pinned records. Pinned memories are intentionally
    # permanent and must survive retention sweeps.
    try:
        count = db.session.query(Memory).filter(
            Memory.created_at < cutoff,
            Memory.pinned != True,
        ).delete(synchronize_session=False)
        total_purged += count
        db.session.commit()
        log.info("Retention: purged %d old memories records (older than %s)", count, cutoff.isoformat())
    except Exception as e:
        db.session.rollback()
        log.error("Retention purge failed for memories: %s", e)

    # Sessions: only purge archived ones older than cutoff.
    # Non-archived sessions are preserved; cascade auto-deletes their messages.
    try:
        count = db.session.query(Session).filter(
            Session.archived == True,
            Session.archived_at != None,
            Session.archived_at < cutoff,
        ).delete(synchronize_session=False)
        total_purged += count
        db.session.commit()
        log.info("Retention: purged %d archived sessions (older than %s)",
                 count, cutoff.isoformat())
    except Exception as e:
        db.session.rollback()
        log.error("Retention purge failed for sessions: %s", e)

    if total_purged > 0:
        log.info("Retention sweep complete: %d total records purged", total_purged)


def start_retention_service(interval_hours: float = 6.0) -> None:
    """Start background thread that enforces data retention periodically.

    Args:
        interval_hours: How often to run retention (default 6).
    """
    global _retention_thread

    def _retention_loop():
        while not _shutdown_event.is_set():
            try:
                # Run in app context
                from app import create_app, db
                app = create_app()
                with app.app_context():
                    enforce_retention()
            except Exception as e:
                log.error("Retention thread error: %s", e, exc_info=True)

            _shutdown_event.wait(interval_hours * 3600)

        log.info("Retention service thread stopped")

    _retention_thread = threading.Thread(
        target=_retention_loop, daemon=True, name="retention-scheduler"
    )
    _retention_thread.start()
    log.info("Retention service started (interval=%.1fh)", interval_hours)


def stop_retention_service() -> None:
    """Signal the retention service thread to stop."""
    global _retention_thread
    if _retention_thread and _retention_thread.is_alive():
        _shutdown_event.set()
        _retention_thread.join(timeout=30)
        log.info("Retention service thread stopped")
