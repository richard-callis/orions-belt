"""Tests for data retention service."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from app import create_app, db


@pytest.fixture
def app():
    """Create app with in-memory test database."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"

    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app

    with app.app_context():
        db.drop_all()


class TestEnforceRetention:
    def test_purges_old_audit_logs(self, app):
        """Records older than LOG_RETENTION_DAYS should be purged."""
        from app.models.logs import AuditLog
        from app.services.retention import enforce_retention

        old_date = datetime.now(timezone.utc) - timedelta(days=60)

        with app.app_context():
            old_entry = AuditLog(
                tool_name="read_file", tier=0, caller="test",
                outcome="auto", result_summary="ok", created_at=old_date
            )
            db.session.add(old_entry)

            recent_entry = AuditLog(
                tool_name="read_file", tier=0, caller="test",
                outcome="auto", result_summary="ok",
                created_at=datetime.now(timezone.utc) - timedelta(days=1)
            )
            db.session.add(recent_entry)
            db.session.commit()

        with app.app_context():
            assert AuditLog.query.count() == 2
            enforce_retention()

        with app.app_context():
            assert AuditLog.query.count() == 1
            assert AuditLog.query.first().tool_name == "read_file"

    def test_keeps_recent_records(self, app):
        """Recent records should not be purged."""
        from app.models.logs import AuditLog
        from app.services.retention import enforce_retention

        with app.app_context():
            recent = AuditLog(
                tool_name="list_directory", tier=0, caller="test",
                outcome="auto", result_summary="ok",
                created_at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            db.session.add(recent)
            db.session.commit()

        with app.app_context():
            count_before = AuditLog.query.count()
            enforce_retention()
            assert AuditLog.query.count() == count_before

    def test_purges_old_llm_logs(self, app):
        """Old LLM logs should be purged."""
        from app.models.logs import LLMLog
        from app.services.retention import enforce_retention

        old_date = datetime.now(timezone.utc) - timedelta(days=90)

        with app.app_context():
            old_log = LLMLog(
                provider="openai", model="gpt-4", created_at=old_date
            )
            db.session.add(old_log)
            db.session.commit()

        with app.app_context():
            enforce_retention()

        with app.app_context():
            assert LLMLog.query.count() == 0

    def test_purges_old_memories(self, app):
        """Old memories should be purged."""
        from app.models.memory import Memory
        from app.services.retention import enforce_retention

        old_date = datetime.now(timezone.utc) - timedelta(days=60)

        with app.app_context():
            old_mem = Memory(
                title="Old memory", content="data", created_at=old_date
            )
            db.session.add(old_mem)
            db.session.commit()

        with app.app_context():
            enforce_retention()

        with app.app_context():
            assert Memory.query.count() == 0

    def test_purges_archived_sessions(self, app):
        """Archived sessions older than cutoff should be purged."""
        from app.models.chat import Session, Message
        from app.services.retention import enforce_retention

        cutoff = datetime.now(timezone.utc) - timedelta(days=60)

        with app.app_context():
            archived = Session(
                name="Old archived", archived=True, archived_at=cutoff
            )
            db.session.add(archived)
            db.session.commit()

            not_archived = Session(
                name="Active session", archived=False
            )
            db.session.add(not_archived)
            db.session.commit()

        with app.app_context():
            enforce_retention()

        with app.app_context():
            assert Session.query.count() == 1
            assert Session.query.first().name == "Active session"
